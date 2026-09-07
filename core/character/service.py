"""CharacterService — 角色/身份 REST 领域编排。

本模块是 Stage H REST 契约与领域存储之间的唯一桥接层：所有写入先完成结构/
语义校验，再以 revision CAS 发布；公开响应一律经过严格的 frontend_contract
模型构造，避免把 domain 中的松散 dict 漂移到 wire。
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

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
    from core.character.runtime import CharacterRuntime
    from core.schema.character import CharacterRuntimeState


MAX_CHARACTER_LIST_SCAN = 10_000
RESET_FIELDS = [
    CharacterResetField.SELF_L2,
    CharacterResetField.STATE_L3,
    CharacterResetField.SPEAKING_STYLE_C6,
    CharacterResetField.REFLECTOR,
    CharacterResetField.MOOD,
]
BUILTIN_FALLBACK_CHARACTER_ID = "chr_builtin_elysia"


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
    return CharacterRevisionContent.model_validate(normalised.model_dump(mode="json"))


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


def _state_impact(policy: str) -> CharacterStateImpact:
    try:
        parsed = CharacterStatePolicy(policy)
    except ValueError:
        raise CharacterValidationError("statePolicy 无效") from None
    fields = RESET_FIELDS if parsed is CharacterStatePolicy.RESET_EVOLUTION else []
    return CharacterStateImpact(
        statePolicy=parsed,
        affectedInstances=0,
        pendingInstances=0,
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
    ):
        self.repo = repo
        self.rev_repo = rev_repo
        self.catalog = catalog
        self.user_identity_store = user_identity_store
        self._connected = False
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
        self, draft: CharacterDraft, *, owner_user_id: str, created_by: str = "user"
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
        impact = _state_impact(state_policy)
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
        new_revision = await self.rev_repo.max_revision(character_id) + 1
        content = self._revision_from_draft(character_id, draft, revision=new_revision)
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
        return CharacterMutationResult(
            character=_detail(definition, content),
            stateImpact=impact,
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
        await self.repo.archive(character_id)
        definition.status = "archived"
        definition.updatedAt = now_iso()
        profile = self.user_identity_store.get()
        profile_wire = None
        if profile is not None and profile.defaultCharacterId == character_id:
            replacement = self._fallback_character_id(character_id)
            updated = self.user_identity_store.update_if_revision(
                profile.revision, {"defaultCharacterId": replacement}
            )
            profile_wire = updated or self.user_identity_store.get()
        from frontend_contract.rest import UserIdentityProfile

        return CharacterArchiveResult(
            character=_summary(definition, revision),
            userProfile=(
                None
                if profile_wire is None
                else UserIdentityProfile(**profile_wire.model_dump())
            ),
        )

    async def rollback(
        self,
        character_id: str,
        *,
        target_revision: int,
        expected_revision: int,
        state_policy: str = "preserve",
    ) -> CharacterMutationResult:
        impact = _state_impact(state_policy)
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
        old = await self.rev_repo.get_revision(character_id, target_revision)
        if old is None:
            raise CharacterNotFoundError(character_id)
        new_revision = await self.rev_repo.max_revision(character_id) + 1
        old = _normalise_revision(old)
        content = old.model_copy(
            deep=True,
            update={"revision": new_revision, "createdBy": "user", "createdAt": now_iso()},
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
        return CharacterMutationResult(
            character=_detail(definition, content),
            stateImpact=impact,
        )

    async def preview(
        self, draft: CharacterDraft, *, user_message: str
    ) -> CharacterPreviewResult:
        """Validate, then fail closed until a stateless adapter exists.

        Fabricating an answer would violate §13's no-session/no-memory guarantee. The
        route and stable error code are intentionally present so the frontend can gate
        the feature without treating an echo as AI output.
        """
        err = await self._raise_on_validation(draft)
        if err:
            raise err
        raise CharacterPreviewUnavailableError("角色预览暂未启用无状态推理适配器")

    async def reset_evolution(
        self,
        instance_id: str,
        *,
        expected_state_version: int,
        runtime: "CharacterRuntime | None",
        persist: Callable[["CharacterRuntimeState", int], Awaitable[bool]],
    ) -> CharacterRuntimeState:
        """Reset only evolution fields under runtime lock + storage CAS."""
        if runtime is None:
            raise CharacterInstanceNotFoundError(instance_id)
        if runtime.lock.locked():
            raise CharacterBusyError(instance_id)
        engine = runtime.engine
        locks = [
            getattr(getattr(engine, "_reflector", None), "_reflect_lock", None),
            getattr(getattr(engine, "_compression", None), "_compression_lock", None),
            getattr(getattr(runtime, "reflector", None), "_lock", None),
        ]
        if any(lock is not None and lock.locked() for lock in locks):
            raise CharacterBusyError(instance_id)
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

    def _revision_from_draft(
        self,
        character_id: str,
        draft: CharacterDraft,
        *,
        revision: int,
        created_by: str = "user",
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
    engine = runtime.engine
    if engine is not None:
        reflector = getattr(engine, "_reflector", None)
        if reflector is not None:
            reflector._buffer.clear()
            reflector._context.clear()
            reflector._last_reflection_at = None
            reflector._last_success = False
            reflector._last_duration_ms = 0.0
            reflector._last_text_len = 0
            reflector._last_prompt_system = ""
            reflector._last_prompt_user = ""
        awareness = getattr(engine, "_awareness", None)
        if awareness is not None:
            awareness._last_awareness_time = None
    memory_reflector = runtime.reflector
    if memory_reflector is not None:
        memory_reflector._round_count = 0
        memory_reflector._new_memory_count = 0
        memory_reflector._last_summary = {}


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
