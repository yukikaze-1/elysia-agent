"""CharacterService — 角色/身份 REST 领域编排。

本模块是 Stage H REST 契约与领域存储之间的唯一桥接层：所有写入先完成结构/
语义校验，再以 revision CAS 发布；公开响应一律经过严格的 frontend_contract
模型构造，避免把 domain 中的松散 dict 漂移到 wire。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal, cast

from core.character.catalog import CharacterCatalog
from core.character.definition import CharacterDefinition
from core.character.draft import (
    CharacterDraft,
    ConversationExample,
    Presentation,
    PromptProfile,
    RuntimeDefaults,
)
from core.character.revision import CharacterRevision
from core.profile.profile_store import UserIdentityStore
from core.storage.base import StorageUnavailableError
from core.storage.character_repo import CharacterRepository
from core.storage.character_revision_repo import CharacterRevisionRepository
from core.schema.utils import now_iso
from frontend_contract.rest import (
    CharacterArchiveResult,
    CharacterCreatedBy,
    CharacterResetField,
    CharacterSource,
    CharacterStatus,
    CharacterValidationSeverity,
    CharacterDetail,
    CharacterListResponse,
    CharacterMutationResult,
    CharacterPreviewResult,
    CharacterRevisionContent,
    CharacterRevisionListResponse,
    CharacterRevisionSummary,
    CharacterStateImpact,
    CharacterStatePolicy,
    CharacterSummary,
    CharacterValidationIssue,
    CharacterValidationResult,
)

if TYPE_CHECKING:
    from core.character.instance import CharacterInstance
    from core.character.preview import CharacterPreviewPort
    from core.character.registry import CharacterRuntimeRegistry
    from core.character.runtime import CharacterRuntime
    from core.character.runtime_profile import RuntimeCharacterProfile
    from core.profile.user_identity import UserIdentity
    from core.schema.character import CharacterRuntimeState
    from core.storage.character_instance_repo import CharacterInstanceRepository


MAX_CHARACTER_LIST_SCAN = 10_000
INSTANCE_RECONCILE_RETRIES = 3
RESET_FIELDS = [
    CharacterResetField.SELF_L2,
    CharacterResetField.STATE_L3,
    CharacterResetField.SPEAKING_STYLE_C6,
    CharacterResetField.REFLECTOR,
    CharacterResetField.MOOD,
]
BUILTIN_FALLBACK_CHARACTER_ID = "chr_builtin_elysia"
logger = logging.getLogger(__name__)


class CharacterError(RuntimeError):
    """角色领域错误的基类。"""


class CharacterNotFoundError(CharacterError):
    pass


class CharacterInstanceNotFoundError(CharacterError):
    pass


class CharacterImmutableError(CharacterError):
    pass


class CharacterArchivedError(CharacterError):
    pass


class CharacterValidationError(CharacterError):
    def __init__(
        self,
        message: str = "角色内容校验失败",
        issues: list[CharacterValidationIssue] | None = None,
    ):
        super().__init__(message)
        self.issues = issues or []


class CharacterTooLargeError(CharacterError):
    pass


class CharacterQueryError(CharacterError):
    pass


class RevisionConflictError(CharacterError):
    def __init__(self, character_id: str, current_revision: int | None = None):
        super().__init__(character_id)
        self.character_id = character_id
        self.current_revision = current_revision


class CharacterStateConflictError(CharacterError):
    def __init__(self, instance_id: str, current_state_version: int | None = None):
        super().__init__(instance_id)
        self.instance_id = instance_id
        self.current_state_version = current_state_version


class CharacterBusyError(CharacterError):
    pass


class CharacterPreviewUnavailableError(CharacterError):
    """Preview provider is deliberately unavailable until a stateless adapter exists."""


# 继承 StorageUnavailableError，使连接/持久化失败统一 fail-closed 为 503。
class CharacterStorageUnavailableError(StorageUnavailableError):
    pass


def _encode_cursor(value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> dict[str, Any] | None:
    if cursor is None or cursor == "":
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (ValueError, UnicodeError, json.JSONDecodeError):
        raise CharacterQueryError("cursor 无效") from None
    if not isinstance(value, dict):
        raise CharacterQueryError("cursor 无效")
    return value


def _limit(value: int) -> int:
    if not 1 <= value <= 100:
        raise CharacterQueryError("limit 必须在 1 到 100 之间")
    return value


def _mapping(value: object) -> dict[str, Any]:
    """Treat only JSON objects as legacy maps at the domain/wire boundary."""
    return value if isinstance(value, dict) else {}


def _checksum(revision: CharacterRevision) -> str:
    payload = {
        "schemaVersion": revision.schemaVersion,
        "displayName": revision.displayName,
        "description": revision.description,
        "aliases": revision.aliases,
        "promptProfile": revision.promptProfile,
        "runtimeDefaults": revision.runtimeDefaults,
        "presentation": revision.presentation,
        "openingMessage": revision.openingMessage,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _normalise_revision(revision: CharacterRevision) -> CharacterRevision:
    """Fill only known legacy omissions; never copy arbitrary keys to wire."""
    raw_prompt = _mapping(revision.promptProfile)
    prompt = {
        key: raw_prompt[key]
        for key in (
            "corePersonality", "relationshipStyle", "emotionStyle",
            "speakingStyle", "conversationInitiative", "examples",
        )
        if key in raw_prompt
    }
    prompt.setdefault("corePersonality", "暂无核心人格设定。")
    prompt.setdefault("relationshipStyle", "保持自然、尊重的关系边界。")
    prompt.setdefault("emotionStyle", "根据上下文自然表达情绪。")
    prompt.setdefault("speakingStyle", "用清晰、自然的方式交流。")
    prompt.setdefault("conversationInitiative", "在合适时主动回应用户。")
    prompt.setdefault("examples", [])

    runtime = _mapping(revision.runtimeDefaults)
    voice_raw = _mapping(runtime.get("voice"))
    voice = {
        "providerId": voice_raw.get("providerId"),
        "voiceId": voice_raw.get("voiceId"),
    }
    emotion_raw = _mapping(runtime.get("emotion"))
    emotion = {
        "idleExpressions": list(emotion_raw.get("idleExpressions") or []),
    }
    proactive_raw = _mapping(runtime.get("proactive"))
    proactive = {
        "enabled": bool(proactive_raw.get("enabled", False)),
        "intervalSeconds": proactive_raw.get(
            "intervalSeconds", proactive_raw.get("interval", 1800)
        ),
    }
    trigger_values = proactive_raw.get("triggers") or []
    # The legacy builtin YAML used structured trigger objects.  The frozen wire
    # intentionally exposes only stable string trigger IDs, never arbitrary dicts.
    proactive["triggers"] = [
        value if isinstance(value, str) else str(value.get("type", ""))
        for value in trigger_values
        if isinstance(value, str) or isinstance(value, dict)
    ]
    runtime = {
        "voice": voice,
        "emotion": emotion,
        "wakeWords": list(runtime.get("wakeWords") or []),
        "proactive": proactive,
    }
    presentation_raw = _mapping(revision.presentation)
    presentation = cast(dict[str, object], {
        "avatarAssetId": presentation_raw.get("avatarAssetId"),
        "live2dModelId": presentation_raw.get("live2dModelId"),
        "themeColor": presentation_raw.get("themeColor"),
    })

    result = revision.model_copy(deep=True)
    result.promptProfile = prompt
    result.runtimeDefaults = runtime
    result.presentation = presentation
    if not result.checksum:
        result.checksum = _checksum(result)
    return result


def _revision_wire(revision: CharacterRevision) -> CharacterRevisionContent:
    """Construct the strict public revision model (the dict boundary)."""
    normalised = _normalise_revision(revision)
    return CharacterRevisionContent.model_validate(
        normalised.model_dump(mode="json", exclude={"statePolicy"})
    )


def _summary(defn: CharacterDefinition, revision: CharacterRevision) -> CharacterSummary:
    current = _revision_wire(revision)
    return CharacterSummary(
        characterId=defn.characterId,
        source=CharacterSource(defn.source),
        mutable=defn.mutable,
        status=CharacterStatus(defn.status),
        currentRevision=defn.currentRevision,
        displayName=current.displayName,
        description=current.description,
        aliases=current.aliases,
        presentation=current.presentation,
        updatedAt=defn.updatedAt,
    )


def _detail(defn: CharacterDefinition, revision: CharacterRevision) -> CharacterDetail:
    current = _revision_wire(revision)
    return CharacterDetail(
        summary=CharacterSummary(
            characterId=defn.characterId,
            source=CharacterSource(defn.source),
            mutable=defn.mutable,
            status=CharacterStatus(defn.status),
            currentRevision=defn.currentRevision,
            displayName=current.displayName,
            description=current.description,
            aliases=current.aliases,
            presentation=current.presentation,
            updatedAt=defn.updatedAt,
        ),
        current=current,
    )


def _parse_state_policy(policy: str) -> CharacterStatePolicy:
    try:
        return CharacterStatePolicy(policy)
    except ValueError:
        raise CharacterValidationError("statePolicy 无效") from None


def _state_impact(
    policy: str,
    *,
    affected_instances: int = 0,
    pending_instances: int = 0,
) -> CharacterStateImpact:
    parsed = _parse_state_policy(policy)
    fields = RESET_FIELDS if parsed is CharacterStatePolicy.RESET_EVOLUTION else []
    return CharacterStateImpact(
        statePolicy=parsed,
        affectedInstances=affected_instances,
        pendingInstances=pending_instances,
        resetFields=fields,
    )


class CharacterService:
    """角色 CRUD、分页、版本 CAS 与安全 preview/reset 编排。"""

    def __init__(
        self,
        *,
        repo: CharacterRepository,
        rev_repo: CharacterRevisionRepository,
        catalog: CharacterCatalog | None,
        user_identity_store: UserIdentityStore,
        instance_repo: "CharacterInstanceRepository | None" = None,
        runtime_registry: "CharacterRuntimeRegistry | None" = None,
        preview_port: "CharacterPreviewPort | None" = None,
    ):
        self.repo = repo
        self.rev_repo = rev_repo
        self.catalog = catalog
        self.user_identity_store = user_identity_store
        self.instance_repo = instance_repo
        self.runtime_registry = runtime_registry
        self.preview_port = preview_port
        self._connected = False
        self._instances_connected = False
        self._builtin_entries: list[
            tuple[CharacterDefinition, CharacterRevision]
        ] | None = None

    async def _ensure_connected(self) -> None:
        if self._connected:
            return
        if not await self.repo.connect():
            raise CharacterStorageUnavailableError("CharacterRepository 连接失败")
        if not await self.rev_repo.connect():
            raise CharacterStorageUnavailableError("CharacterRevisionRepository 连接失败")
        self._connected = True

    async def validate(self, draft: CharacterDraft) -> CharacterValidationResult:
        issues: list[CharacterValidationIssue] = []
        profile = draft.promptProfile
        for field, text in (
            ("corePersonality", profile.corePersonality),
            ("relationshipStyle", profile.relationshipStyle),
            ("speakingStyle", profile.speakingStyle),
            ("emotionStyle", profile.emotionStyle),
            ("conversationInitiative", profile.conversationInitiative),
        ):
            for issue in _semantic_issues(text, f"/promptProfile/{field}"):
                issues.append(issue)
        for index, example in enumerate(profile.examples):
            for field, text in (("user", example.user), ("assistant", example.assistant)):
                for issue in _semantic_issues(
                    text, f"/promptProfile/examples/{index}/{field}"
                ):
                    issues.append(issue)
        estimated = sum(
            len(text)
            for text in (
                profile.corePersonality,
                profile.relationshipStyle,
                profile.emotionStyle,
                profile.speakingStyle,
                profile.conversationInitiative,
            )
        ) + sum(len(example.user) + len(example.assistant) for example in profile.examples)
        return CharacterValidationResult(
            valid=not any(issue.severity == "error" for issue in issues),
            issues=issues,
            estimatedPromptChars=estimated,
        )

    async def create(
        self, draft: CharacterDraft, *, owner_user_id: str,
        created_by: CharacterCreatedBy = CharacterCreatedBy.USER,
    ) -> CharacterDetail:
        await self._ensure_connected()
        err = await self._raise_on_validation(draft)
        if err:
            raise err
        character_id = f"chr_custom_{uuid.uuid4().hex[:16]}"
        revision = self._revision_from_draft(
            character_id, draft, revision=1, created_by=created_by
        )
        await self.rev_repo.append_revision(revision)
        try:
            definition = CharacterDefinition(
                characterId=character_id,
                slug=draft.displayName,
                source="custom",
                mutable=True,
                status="active",
                currentRevision=1,
                ownerUserId=owner_user_id,
            )
            await self.repo.insert_definition(definition)
        except Exception:
            await self.rev_repo.delete_revision(character_id, 1)
            raise
        return _detail(definition, revision)

    async def get(self, character_id: str) -> CharacterDetail | None:
        builtin_pair = self._catalog_pair(character_id)
        if builtin_pair is not None:
            return _detail(*builtin_pair)
        await self._ensure_connected()
        pair = await self._get_definition_and_revision(character_id)
        if pair is None:
            return None
        return _detail(*pair)

    async def resolve_runtime_source(
        self, character_id: str, *, revision: int | None = None,
    ) -> tuple[CharacterDefinition, CharacterRevision]:
        """Resolve one exact immutable revision for runtime construction.

        This domain API deliberately returns internal models rather than REST
        DTOs.  A requested historical revision is never replaced with the current
        pointer, and a missing/corrupt revision fails closed.
        """
        builtin_pair = self._catalog_pair(character_id)
        if builtin_pair is not None:
            definition, current = builtin_pair
            if revision is not None and revision != current.revision:
                raise CharacterNotFoundError(character_id)
            return definition, _normalise_revision(current)

        await self._ensure_connected()
        definition = await self.repo.get_definition(character_id)
        if definition is None:
            raise CharacterNotFoundError(character_id)
        selected_revision = revision or definition.currentRevision
        content = await self.rev_repo.get_revision(character_id, selected_revision)
        if content is None:
            raise CharacterStorageUnavailableError(
                f"角色 revision 缺失: {character_id}@{selected_revision}"
            )
        return definition, _normalise_revision(content)

    async def list(
        self,
        *,
        source: str | None = None,
        status: str = "active",
        search: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> CharacterListResponse:
        if source not in {None, "builtin", "custom"}:
            raise CharacterQueryError("source 无效")
        if status not in {"active", "archived"}:
            raise CharacterQueryError("status 无效")
        page_size = _limit(limit)
        search_value = search.strip() if search else None
        if search_value is not None and not search_value:
            search_value = None
        if search_value and len(search_value) > 200:
            raise CharacterQueryError("search 过长")
        marker = _decode_cursor(cursor)
        if marker is not None and (
            not isinstance(marker.get("updatedAt"), str)
            or not isinstance(marker.get("characterId"), str)
        ):
            raise CharacterQueryError("cursor 无效")

        # Built-ins are file-backed and remain available when Mongo is offline.
        # A combined/custom list still needs the custom store and therefore fails
        # closed instead of silently returning an incomplete catalogue.
        if source in {None, "custom"}:
            await self._ensure_connected()

        summaries: list[CharacterSummary] = []
        if source in {None, "custom"}:
            definitions = await self.repo.list_definitions(
                source="custom",
                status=status,
                owner_user_id="usr_local_default",
                search=None,
                cursor=None,
                limit=MAX_CHARACTER_LIST_SCAN,
            )
            for definition in definitions:
                revision = await self.rev_repo.get_revision(
                    definition.characterId, definition.currentRevision
                )
                if revision is None:
                    raise CharacterStorageUnavailableError("角色 revision 缺失")
                summaries.append(_summary(definition, revision))

        if source in {None, "builtin"} and self.catalog is not None:
            if self._builtin_entries is None:
                self._builtin_entries = []
                for definition in self.catalog.list():
                    pair = self.catalog.get_revision(definition.characterId)
                    if pair is None:
                        raise CharacterStorageUnavailableError("内置角色 revision 缺失")
                    self._builtin_entries.append(pair)
            for definition, revision in self._builtin_entries:
                if definition.status != status:
                    continue
                summaries.append(_summary(definition, revision))

        if search_value:
            needle = search_value.casefold()
            summaries = [
                item
                for item in summaries
                if needle
                in " ".join(
                    [item.characterId, item.displayName, item.description, *item.aliases]
                ).casefold()
            ]
        summaries.sort(key=lambda item: (item.updatedAt, item.characterId), reverse=True)
        if marker is not None:
            summaries = [
                item
                for item in summaries
                if (item.updatedAt, item.characterId)
                < (marker["updatedAt"], marker["characterId"])
            ]
        has_more = len(summaries) > page_size
        page = summaries[:page_size]
        next_cursor = (
            _encode_cursor({"updatedAt": page[-1].updatedAt, "characterId": page[-1].characterId})
            if has_more and page
            else None
        )
        return CharacterListResponse(items=page, has_more=has_more, cursor=next_cursor)

    async def list_revisions(
        self, character_id: str, *, cursor: str | None = None, limit: int = 50
    ) -> CharacterRevisionListResponse:
        page_size = _limit(limit)
        marker = _decode_cursor(cursor)
        revision_marker = None
        if marker is not None:
            if not isinstance(marker.get("revision"), int) or marker["revision"] < 1:
                raise CharacterQueryError("cursor 无效")
            revision_marker = marker["revision"]

        builtin_pair = self._catalog_pair(character_id)
        if builtin_pair is not None:
            revisions = [] if revision_marker is not None else [builtin_pair[1]]
            items = [
                CharacterRevisionSummary(
                    revision=wire.revision,
                    checksum=wire.checksum,
                    createdBy=wire.createdBy,
                    createdAt=wire.createdAt,
                )
                for wire in (_revision_wire(revision) for revision in revisions)
            ]
            return CharacterRevisionListResponse(items=items, has_more=False, cursor=None)

        await self._ensure_connected()
        definition = await self._get_definition(character_id)
        if definition is None:
            raise CharacterNotFoundError(character_id)
        if hasattr(self.rev_repo, "list_revisions_page"):
            revisions, has_more = await self.rev_repo.list_revisions_page(
                character_id, cursor=revision_marker, limit=page_size
            )
        else:
            revisions = await self.rev_repo.list_revisions(
                character_id,
                cursor=str(revision_marker) if revision_marker else None,
                limit=page_size + 1,
            )
            has_more = len(revisions) > page_size
            revisions = revisions[:page_size]
        items = [
            CharacterRevisionSummary(
                revision=wire.revision,
                checksum=wire.checksum,
                createdBy=wire.createdBy,
                createdAt=wire.createdAt,
            )
            for wire in (_revision_wire(revision) for revision in revisions)
        ]
        next_cursor = (
            _encode_cursor({"revision": items[-1].revision})
            if has_more and items
            else None
        )
        return CharacterRevisionListResponse(
            items=items, has_more=has_more, cursor=next_cursor
        )

    async def update(
        self,
        character_id: str,
        draft: CharacterDraft,
        *,
        expected_revision: int,
        state_policy: str = "preserve",
    ) -> CharacterMutationResult:
        err = await self._raise_on_validation(draft)
        if err:
            raise err
        policy = _parse_state_policy(state_policy).value
        builtin_definition = self.catalog.get(character_id) if self.catalog is not None else None
        if builtin_definition is not None and not builtin_definition.mutable:
            raise CharacterImmutableError(character_id)
        await self._ensure_connected()
        definition = await self._get_definition(character_id)
        if definition is None:
            raise CharacterNotFoundError(character_id)
        if not definition.mutable:
            raise CharacterImmutableError(character_id)
        if definition.status == "archived":
            raise CharacterArchivedError(character_id)
        if definition.currentRevision != expected_revision:
            raise RevisionConflictError(character_id, definition.currentRevision)
        affected = await self._follow_latest_instances(character_id)
        self._raise_if_loaded_runtimes_busy(affected)
        new_revision = await self.rev_repo.max_revision(character_id) + 1
        content = self._revision_from_draft(
            character_id, draft, revision=new_revision, state_policy=policy
        )
        await self.rev_repo.append_revision(content)
        updated_at = now_iso()
        ok = await self.repo.cas_revision_pointer(
            character_id, expected_revision, new_revision, updated_at=updated_at
        )
        if not ok:
            await self.rev_repo.delete_revision(character_id, new_revision)
            raise RevisionConflictError(character_id, definition.currentRevision)
        definition.currentRevision = new_revision
        definition.updatedAt = updated_at
        pending = await self._propagate_revision(
            affected, new_revision=new_revision, state_policy=policy
        )
        return CharacterMutationResult(
            character=_detail(definition, content),
            stateImpact=_state_impact(
                policy,
                affected_instances=len(affected),
                pending_instances=pending,
            ),
        )

    async def clone(
        self, character_id: str, *, display_name: str | None = None
    ) -> CharacterDetail:
        source = await self.get(character_id)
        if source is None:
            raise CharacterNotFoundError(character_id)
        profile = source.current.promptProfile
        new_draft = CharacterDraft(
            displayName=display_name or source.summary.displayName,
            description=source.current.description,
            aliases=source.current.aliases,
            promptProfile=PromptProfile(
                corePersonality=profile.corePersonality or "暂无核心人格。",
                relationshipStyle=profile.relationshipStyle or "保持友好而自然的互动。",
                speakingStyle=profile.speakingStyle or "用日常口语对话。",
                emotionStyle=profile.emotionStyle,
                conversationInitiative=profile.conversationInitiative,
                examples=[
                    ConversationExample(user=example.user, assistant=example.assistant)
                    for example in profile.examples
                ],
            ),
            runtimeDefaults=RuntimeDefaults(
                **source.current.runtimeDefaults.model_dump(mode="python")
            ),
            presentation=Presentation(
                **source.current.presentation.model_dump(mode="python")
            ),
            openingMessage=source.current.openingMessage,
        )
        return await self.create(new_draft, owner_user_id="usr_local_default")

    async def archive(self, character_id: str) -> CharacterArchiveResult:
        builtin_definition = self.catalog.get(character_id) if self.catalog is not None else None
        if builtin_definition is not None and not builtin_definition.mutable:
            raise CharacterImmutableError(character_id)
        await self._ensure_connected()
        pair = await self._get_definition_and_revision(character_id)
        if pair is None:
            raise CharacterNotFoundError(character_id)
        definition, revision = pair
        if not definition.mutable:
            raise CharacterImmutableError(character_id)
        if definition.status == "archived":
            raise CharacterArchivedError(character_id)
        # P1（归档一致性）：**先**把默认角色指向改到可用回退角色，**再**归档。
        # 反序（先归档再改档案）在 profile CAS 冲突/存储失败时会留下
        # 「defaultCharacterId 仍指向已归档角色」的悬空引用；本序下 profile 更新
        # 失败则整个归档在写角色前失败——角色保持 active、档案未变，完全可恢复。
        profile_wire = await self._retarget_default_character(character_id)
        await self.repo.archive(character_id)
        definition.status = "archived"
        definition.updatedAt = now_iso()
        from frontend_contract.rest import UserIdentityProfile

        return CharacterArchiveResult(
            character=_summary(definition, revision),
            userProfile=(
                None
                if profile_wire is None
                else UserIdentityProfile(**profile_wire.model_dump())
            ),
        )

    # 默认角色回退的 CAS 重试上界（并发改档案时的有限重试；不引入无限循环）
    DEFAULT_RETARGET_ATTEMPTS = 3

    async def _retarget_default_character(
        self, character_id: str
    ) -> "UserIdentity | None":
        """归档前把 defaultCharacterId 从被归档角色改到可用回退角色。

        有限 CAS 重试（并发改档案时重新读取 revision 后重试）。存储不可用或重试
        耗尽 → 抛错让归档整体失败（角色保持 active，不留悬空默认角色）。
        """
        replacement = self._fallback_character_id(character_id)
        for _ in range(self.DEFAULT_RETARGET_ATTEMPTS):
            profile = self.user_identity_store.get()
            if profile is None:
                if self.user_identity_store.is_authoritative:
                    # 权威身份不可用 → 无法保证「归档后默认角色指向可用角色」，
                    # 整个归档 fail-closed（角色保持 active，可恢复）。
                    raise StorageUnavailableError("用户身份权威存储不可用")
                return None
            if profile.defaultCharacterId != character_id:
                # 默认角色不是被归档角色——无需改动，响应沿用既有 None 语义。
                return None
            updated = await self.user_identity_store.update_if_revision(
                profile.revision, {"defaultCharacterId": replacement}
            )
            if updated is not None:
                return updated
            # CAS 冲突（并发写档案）→ 重读后重试；耗尽则整体失败。
        raise RevisionConflictError(character_id)

    async def rollback(
        self,
        character_id: str,
        *,
        target_revision: int,
        expected_revision: int,
        state_policy: str = "preserve",
    ) -> CharacterMutationResult:
        policy = _parse_state_policy(state_policy).value
        builtin_definition = self.catalog.get(character_id) if self.catalog is not None else None
        if builtin_definition is not None and not builtin_definition.mutable:
            raise CharacterImmutableError(character_id)
        await self._ensure_connected()
        definition = await self._get_definition(character_id)
        if definition is None:
            raise CharacterNotFoundError(character_id)
        if not definition.mutable:
            raise CharacterImmutableError(character_id)
        if definition.status == "archived":
            raise CharacterArchivedError(character_id)
        if definition.currentRevision != expected_revision:
            raise RevisionConflictError(character_id, definition.currentRevision)
        affected = await self._follow_latest_instances(character_id)
        self._raise_if_loaded_runtimes_busy(affected)
        old = await self.rev_repo.get_revision(character_id, target_revision)
        if old is None:
            raise CharacterNotFoundError(character_id)
        new_revision = await self.rev_repo.max_revision(character_id) + 1
        old = _normalise_revision(old)
        content = old.model_copy(
            deep=True,
            update={
                "revision": new_revision,
                "statePolicy": policy,
                "createdBy": "user",
                "createdAt": now_iso(),
            },
        )
        if not content.checksum:
            content.checksum = _checksum(content)
        await self.rev_repo.append_revision(content)
        updated_at = now_iso()
        ok = await self.repo.cas_revision_pointer(
            character_id, expected_revision, new_revision, updated_at=updated_at
        )
        if not ok:
            await self.rev_repo.delete_revision(character_id, new_revision)
            raise RevisionConflictError(character_id, definition.currentRevision)
        definition.currentRevision = new_revision
        definition.updatedAt = updated_at
        pending = await self._propagate_revision(
            affected, new_revision=new_revision, state_policy=policy
        )
        return CharacterMutationResult(
            character=_detail(definition, content),
            stateImpact=_state_impact(
                policy,
                affected_instances=len(affected),
                pending_instances=pending,
            ),
        )

    async def preview(
        self, draft: CharacterDraft, *, user_message: str
    ) -> CharacterPreviewResult:
        """无状态预览：校验 draft → 一次性组装 prompt → 单次推理，零副作用。

        BE-STAGEH-03：不建 session/instance、不写 archive、不读会话/LTM/演化 state、
        不推进 revision/stateVersion、不触发 reflector/压缩/mood/TTS/工具。用量不并入
        任何会话 token 统计（结果不带 usage）。
        """
        err = await self._raise_on_validation(draft)
        if err:
            raise err
        port = self.preview_port
        if port is None:
            raise CharacterPreviewUnavailableError("角色预览未启用无状态推理适配器")
        identity = self.user_identity_store.get()
        if identity is None:
            # P0-3：权威身份不可用时不得用占位身份预览（fail-closed，稳定公开错误）。
            if (self.user_identity_store.is_authoritative
                    and not self.user_identity_store.is_available):
                raise CharacterStorageUnavailableError("用户身份权威存储不可用")
            raise CharacterPreviewUnavailableError("用户身份未初始化，无法预览")

        profile = self._compile_preview_profile(draft, owner_user_id=identity.userId)
        try:
            reply = await port.generate(
                profile=profile,
                user_identity=identity,
                user_message=user_message,
            )
        except CharacterPreviewUnavailableError:
            raise
        except Exception as exc:
            logger.warning("角色预览推理失败: %s", type(exc).__name__)
            raise CharacterPreviewUnavailableError("角色预览上游失败") from exc

        text = reply.text.strip()
        if not text:
            raise CharacterPreviewUnavailableError("角色预览返回空回复")
        return CharacterPreviewResult(
            ephemeral=True,
            reply=text,
            model=reply.model,
            usage=None,
        )

    def _compile_preview_profile(
        self, draft: CharacterDraft, *, owner_user_id: str
    ) -> "RuntimeCharacterProfile":
        """把 draft 编译成仅存在于内存的 profile（不落库、不建 instance）。"""
        from core.character.runtime_profile import compile_runtime_profile

        character_id = f"chr_preview_{uuid.uuid4().hex[:12]}"
        definition = CharacterDefinition(
            characterId=character_id,
            slug=draft.displayName,
            source="custom",
            mutable=True,
            status="active",
            currentRevision=1,
            ownerUserId=owner_user_id,
        )
        revision = self._revision_from_draft(character_id, draft, revision=1)
        return compile_runtime_profile(definition, revision)

    async def reset_evolution(
        self,
        instance_id: str,
        *,
        expected_state_version: int,
        runtime: "CharacterRuntime | None",
        persist: Callable[["CharacterRuntimeState", int], Awaitable[bool]],
    ) -> CharacterRuntimeState:
        """Reset only evolution fields under an atomic mutation lease + storage CAS.

        BE-STAGEH-01：busy 判定与取得写权限共用一个原子边界（runtime 的同步
        lease）——generation 在途时立即 409，不排队后静默执行。
        """
        if runtime is None:
            raise CharacterInstanceNotFoundError(instance_id)
        if not runtime.try_begin_destructive_mutation():
            raise CharacterBusyError(instance_id)
        try:
            async with runtime.lock:
                current = runtime.stateVersion
                if current != expected_state_version:
                    raise CharacterStateConflictError(instance_id, current)
                candidate = runtime.state.model_copy(deep=True)
                _clear_evolution(candidate)
                new_version = current + 1
                if not await persist(candidate, new_version):
                    raise CharacterStateConflictError(instance_id, current)
                # Keep object identity: CharacterEngine and its subcomponents hold this object.
                for field in type(candidate).model_fields:
                    setattr(runtime.state, field, getattr(candidate, field))
                runtime.stateVersion = new_version
                runtime.instance.state = runtime.state
                runtime.instance.stateVersion = new_version
                runtime.instance.updatedAt = now_iso()
                _clear_runtime_components(runtime)
                return runtime.state
        finally:
            runtime.end_destructive_mutation()

    async def reconcile_instance_revision(
        self, instance: "CharacterInstance"
    ) -> "CharacterInstance":
        """Apply the latest published revision to one follow_latest instance.

        This is called both eagerly after PUT/rollback and lazily by the runtime
        loader.  The lazy path is the recovery mechanism for an interruption between
        publishing the Definition pointer and writing an instance pending marker.
        """
        repo = await self._ensure_instance_repo_connected()
        if repo is None or instance.revisionPolicy == "pinned":
            return instance
        await self._ensure_connected()

        instance_id = instance.instanceId
        for _ in range(INSTANCE_RECONCILE_RETRIES):
            current = await repo.get(instance_id)
            if current is None:
                raise CharacterInstanceNotFoundError(instance_id)
            if current.revisionPolicy == "pinned":
                return current
            definition = await self._get_definition(current.characterId)
            if definition is None:
                raise CharacterNotFoundError(current.characterId)
            target_revision = definition.currentRevision
            if current.revision > target_revision:
                raise CharacterStorageUnavailableError(
                    f"instance revision 超前于 Definition: {instance_id}"
                )

            state = current.state
            needs_transition = (
                current.revision != target_revision
                or current.pendingRevision is not None
                or state is None
                or state.source_revision != target_revision
            )
            if not needs_transition:
                return current

            policy = await self._effective_transition_policy(
                current.characterId,
                from_revision=current.revision,
                to_revision=target_revision,
                pending_policy=current.pendingStatePolicy,
            )
            if state is None:
                from core.schema.character import CharacterRuntimeState
                from core.schema.types import SessionId

                candidate = CharacterRuntimeState(
                    instance_id=current.instanceId,
                    session_id=SessionId(current.instanceId),
                    source_revision=target_revision,
                )
            else:
                candidate = state.model_copy(deep=True)
                candidate.source_revision = target_revision
                candidate.updated_at = now_iso()
            if policy == "reset_evolution":
                _clear_evolution(candidate)

            transitioned_at = now_iso()
            candidate.updated_at = transitioned_at
            new_version = current.stateVersion + 1
            ok = await repo.cas_apply_revision(
                instance_id,
                expected_revision=current.revision,
                expected_state_version=current.stateVersion,
                expected_pending_revision=current.pendingRevision,
                new_revision=target_revision,
                new_state=candidate,
                new_version=new_version,
            )
            if ok:
                current.revision = target_revision
                current.state = candidate
                current.stateVersion = new_version
                current.pendingRevision = None
                current.pendingStatePolicy = None
                current.updatedAt = transitioned_at
                return current

        latest = await repo.get(instance_id)
        raise CharacterStateConflictError(
            instance_id,
            None if latest is None else latest.stateVersion,
        )

    async def _effective_transition_policy(
        self,
        character_id: str,
        *,
        from_revision: int,
        to_revision: int,
        pending_policy: str | None,
    ) -> Literal["preserve", "reset_evolution"]:
        """Combine skipped revision policies; reset_evolution always dominates."""
        if pending_policy == "reset_evolution":
            return "reset_evolution"
        for revision_number in range(from_revision + 1, to_revision + 1):
            revision = await self.rev_repo.get_revision(character_id, revision_number)
            if revision is None:
                raise CharacterStorageUnavailableError(
                    f"角色 revision 缺失: {character_id}@{revision_number}"
                )
            if revision.statePolicy == "reset_evolution":
                return "reset_evolution"
        return "preserve"

    async def _ensure_instance_repo_connected(
        self,
    ) -> "CharacterInstanceRepository | None":
        repo = self.instance_repo
        if repo is None:
            return None
        if not self._instances_connected:
            if not await repo.connect():
                raise CharacterStorageUnavailableError(
                    "CharacterInstanceRepository 连接失败"
                )
            self._instances_connected = True
        return repo

    async def _follow_latest_instances(
        self, character_id: str
    ) -> list["CharacterInstance"]:
        repo = await self._ensure_instance_repo_connected()
        if repo is None:
            return []
        instances = await repo.list_by_character(
            character_id, limit=MAX_CHARACTER_LIST_SCAN
        )
        return [
            instance
            for instance in instances
            if instance.revisionPolicy == "follow_latest"
        ]

    @staticmethod
    def _runtime_is_busy(runtime: "CharacterRuntime") -> bool:
        """busy = 破坏性变更在途 / 实例锁被持有 / 反思压缩持锁。

        generation 在途也算 busy——但它不改写 runtime.lock，故单列（BE-STAGEH-01
        的 lease 是判定来源，不再是 M3 那个无人写入的 _busy 占位）。
        """
        if (
            runtime.is_destructive_mutation_active
            or runtime.is_generation_busy
            or runtime.lock.locked()
        ):
            return True
        return runtime.evolution_locks_busy()

    def _raise_if_loaded_runtimes_busy(
        self, instances: list["CharacterInstance"]
    ) -> None:
        registry = self.runtime_registry
        if registry is None:
            return
        for instance in instances:
            runtime = registry.get_existing(instance.instanceId)
            if runtime is not None and self._runtime_is_busy(runtime):
                raise CharacterBusyError(instance.instanceId)

    async def _propagate_revision(
        self,
        instances: list["CharacterInstance"],
        *,
        new_revision: int,
        state_policy: str,
    ) -> int:
        """Eagerly apply a published revision; leave recoverable work pending."""
        pending = 0
        for instance in instances:
            try:
                applied = await self._apply_published_revision(
                    instance,
                    new_revision=new_revision,
                    state_policy=state_policy,
                )
            except (
                CharacterBusyError,
                CharacterStateConflictError,
                StorageUnavailableError,
            ):
                logger.warning(
                    "character revision propagation pending: %s -> %s",
                    instance.instanceId,
                    new_revision,
                    exc_info=True,
                )
                applied = False
            if not applied:
                pending += 1
        return pending

    async def _apply_published_revision(
        self,
        instance: "CharacterInstance",
        *,
        new_revision: int,
        state_policy: str,
    ) -> bool:
        repo = await self._ensure_instance_repo_connected()
        if repo is None:
            return True
        registry = self.runtime_registry
        runtime = (
            registry.get_existing(instance.instanceId)
            if registry is not None
            else None
        )
        if runtime is not None:
            assert registry is not None
            if self._runtime_is_busy(runtime):
                return False
            async with runtime.lock:
                if self._runtime_is_busy_except_instance_lock(runtime):
                    return False
                # Persist the exact old-revision runtime state before replacing its
                # engine/profile.  The subsequent CAS transition starts from this
                # freshly persisted stateVersion.
                await runtime.flush()
                await self._stage_pending_revision(
                    instance.instanceId,
                    new_revision=new_revision,
                    state_policy=state_policy,
                )
                reconciled = await self.reconcile_instance_revision(instance)
            registry.invalidate(instance.instanceId, "character revision changed")
            return reconciled.revision >= new_revision

        await self._stage_pending_revision(
            instance.instanceId,
            new_revision=new_revision,
            state_policy=state_policy,
        )
        reconciled = await self.reconcile_instance_revision(instance)
        return reconciled.revision >= new_revision

    @staticmethod
    def _runtime_is_busy_except_instance_lock(runtime: "CharacterRuntime") -> bool:
        if runtime.is_generation_busy or runtime.is_destructive_mutation_active:
            return True
        return runtime.evolution_locks_busy()

    async def _stage_pending_revision(
        self,
        instance_id: str,
        *,
        new_revision: int,
        state_policy: str,
    ) -> bool:
        repo = await self._ensure_instance_repo_connected()
        if repo is None:
            return True
        for _ in range(INSTANCE_RECONCILE_RETRIES):
            current = await repo.get(instance_id)
            if current is None or current.revisionPolicy == "pinned":
                return True
            if current.revision >= new_revision and current.pendingRevision is None:
                return True
            target = max(new_revision, current.pendingRevision or new_revision)
            effective_policy = (
                "reset_evolution"
                if "reset_evolution"
                in {state_policy, current.pendingStatePolicy}
                else "preserve"
            )
            if await repo.set_pending_revision(
                instance_id,
                expected_revision=current.revision,
                expected_pending_revision=current.pendingRevision,
                pending_revision=target,
                state_policy=effective_policy,
            ):
                return True
        return False

    # ---- internal helpers ----

    def _fallback_character_id(self, archived_id: str) -> str:
        if self.catalog is not None:
            for definition in self.catalog.list():
                if (
                    definition.characterId != archived_id
                    and definition.status == "active"
                    and definition.source == "builtin"
                ):
                    return definition.characterId
        return BUILTIN_FALLBACK_CHARACTER_ID

    def _catalog_pair(
        self, character_id: str
    ) -> tuple[CharacterDefinition, CharacterRevision] | None:
        return (
            self.catalog.get_revision(character_id)
            if self.catalog is not None
            else None
        )

    async def _get_definition(self, character_id: str) -> CharacterDefinition | None:
        definition = await self.repo.get_definition(character_id)
        if definition is not None:
            return definition
        return self.catalog.get(character_id) if self.catalog is not None else None

    async def _get_definition_and_revision(
        self, character_id: str
    ) -> tuple[CharacterDefinition, CharacterRevision] | None:
        definition = await self._get_definition(character_id)
        if definition is None:
            return None
        if definition.source == "builtin":
            pair = self.catalog.get_revision(character_id) if self.catalog is not None else None
            if pair is None:
                raise CharacterStorageUnavailableError("内置角色 revision 缺失")
            return pair
        revision = await self.rev_repo.get_revision(
            character_id, definition.currentRevision
        )
        if revision is None:
            raise CharacterStorageUnavailableError("角色 revision 缺失")
        return definition, revision

    async def _raise_on_validation(
        self, draft: CharacterDraft
    ) -> CharacterError | None:
        result = await self.validate(draft)
        if not result.valid:
            return CharacterValidationError("角色内容校验失败", result.issues)
        size = len(
            json.dumps(
                draft.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
            ).encode()
        )
        if size > 64 * 1024:
            return CharacterTooLargeError("角色内容超过 64KiB")
        return None

    @staticmethod
    def _revision_from_draft(
        character_id: str,
        draft: CharacterDraft,
        *,
        revision: int,
        created_by: CharacterCreatedBy = CharacterCreatedBy.USER,
        state_policy: Literal["preserve", "reset_evolution"] = "preserve",
    ) -> CharacterRevision:
        profile = {
            "corePersonality": draft.promptProfile.corePersonality,
            "relationshipStyle": draft.promptProfile.relationshipStyle,
            "emotionStyle": draft.promptProfile.emotionStyle,
            "speakingStyle": draft.promptProfile.speakingStyle,
            "conversationInitiative": draft.promptProfile.conversationInitiative,
            "examples": [example.model_dump() for example in draft.promptProfile.examples],
        }
        runtime = draft.runtimeDefaults.model_dump(mode="json")
        runtime.setdefault("voice", {"providerId": None, "voiceId": None})
        runtime.setdefault("emotion", {"idleExpressions": []})
        runtime.setdefault("wakeWords", [])
        runtime.setdefault(
            "proactive", {"enabled": False, "intervalSeconds": 1800, "triggers": []}
        )
        presentation = draft.presentation.model_dump(mode="json")
        for key in ("avatarAssetId", "live2dModelId", "themeColor"):
            presentation.setdefault(key, None)
        revision_model = CharacterRevision(
            characterId=character_id,
            revision=revision,
            displayName=draft.displayName,
            description=draft.description,
            aliases=draft.aliases,
            promptProfile=profile,
            runtimeDefaults=runtime,
            presentation=presentation,
            openingMessage=draft.openingMessage,
            statePolicy=state_policy,
            createdBy=created_by,
        )
        revision_model.checksum = _checksum(revision_model)
        return revision_model


def _clear_evolution(state: "CharacterRuntimeState") -> None:
    state.self_l2 = ""
    state.state_l3 = ""
    state.state_history = []
    state.last_awareness_time = None
    state.approved_awareness_text = ""
    state.approved_awareness_at = None
    state.awareness_show_count = 0
    state.compression_last_at = None
    state.compression_last_count = 0
    state.mood = "平静"
    state.mood_intensity = 0.5
    state.speaking_style_c6 = ""
    state.reflector_buffer = []
    state.reflector_context = []
    state.self_perception_history = []
    state.mr_round_count = 0
    state.mr_new_memory_count = 0
    state.mr_last_summary = {}
    state.updated_at = now_iso()


def _clear_runtime_components(runtime: "CharacterRuntime") -> None:
    """复位实例内演化组件（经公开契约，不戳子组件私有字段）。

    引擎侧经 `CharacterEngineProtocol.reset_evolution_state()`——哪些子组件算
    「演化」、各自要清什么，由引擎自己回答；记忆侧经
    `MemoryReflector.reset_state()`（`restore_state()` 的逆操作）。
    """
    engine = runtime.engine
    if engine is not None:
        engine.reset_evolution_state()
    memory_reflector = runtime.reflector
    if memory_reflector is not None:
        memory_reflector.reset_state()


def _semantic_issues(text: str, path: str) -> list[CharacterValidationIssue]:
    from core.character.draft import validate_semantic

    return [
        CharacterValidationIssue(
            severity=CharacterValidationSeverity(issue.severity),
            code=issue.code,
            path=path,
            message=issue.message,
        )
        for issue in validate_semantic(text)
    ]
