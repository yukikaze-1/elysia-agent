"""CharacterService — 角色 CRUD 业务编排 + CAS 冲突 + 合并内置/custom。"""

from __future__ import annotations

import uuid

from core.character.catalog import CharacterCatalog
from core.character.definition import CharacterDefinition
from core.character.draft import CharacterDraft, Presentation, PromptProfile, RuntimeDefaults
from core.character.revision import CharacterRevision
from core.profile.profile_store import UserIdentityStore
from core.storage.base import StorageUnavailableError
from core.storage.character_repo import CharacterRepository
from core.storage.character_revision_repo import CharacterRevisionRepository
from core.schema.utils import now_iso
from frontend_contract.rest import (
    CharacterArchiveResult, CharacterDetail, CharacterListResponse, CharacterMutationResult,
    CharacterRevisionResponse, CharacterRevisionSummary, CharacterStateImpact, CharacterSummary,
    CharacterValidationIssue, CharacterValidationResult,
)


class CharacterError(RuntimeError): ...
class CharacterNotFoundError(CharacterError): ...
class CharacterImmutableError(CharacterError): ...
class CharacterArchivedError(CharacterError): ...
class CharacterValidationError(CharacterError): ...
class RevisionConflictError(CharacterError): ...
# 继承 StorageUnavailableError（而非仅 CharacterError）——使 _character_error_response 的
# isinstance(exc, StorageUnavailableError) 分支能映射 connect 失败 → 503（不变式 #7）。
# 若仅继承 CharacterError，connect 失败会落到 500 INTERNAL_ERROR，违反 fail-closed。
class CharacterStorageUnavailableError(StorageUnavailableError): ...


def _summary(defn: CharacterDefinition, display_name: str | None = None) -> CharacterSummary:
    """构造 CharacterSummary。

    CharacterDefinition 无 displayName 字段——displayName 来自当前 Revision（自定义）
    或 catalog 的内置定义（内置 displayName 由 revision 携带）。经 display_name 参数注入。
    """
    return CharacterSummary(characterId=defn.characterId,
                            displayName=display_name or defn.slug,
                            source=defn.source, status=defn.status)


def _detail(defn: CharacterDefinition, rev: CharacterRevision) -> CharacterDetail:
    """构造 CharacterDetail。

    current 是契约层 CharacterRevisionResponse（非 domain CharacterRevision），
    field names 虽与 domain 一致，但 Pydantic 不把"另一个 BaseModel 实例"coerce 进
    CharacterRevisionResponse 字段（会拒绝）。必须经 model_dump() 显式桥接为 dict。
    """
    return CharacterDetail(
        summary=_summary(defn, display_name=rev.displayName),
        current=CharacterRevisionResponse(**rev.model_dump(mode="json")),
    )


class CharacterService:
    """角色 CRUD 业务编排。

    合并内置（catalog）+ custom（repo）经 list/get。create/update/rollback 用
    Revision-first 写序（先 append_revision 再 CAS 指针；CAS 失败回滚 append）。
    """

    def __init__(self, *, repo: CharacterRepository, rev_repo: CharacterRevisionRepository,
                 catalog: CharacterCatalog, user_identity_store: UserIdentityStore):
        self.repo = repo
        self.rev_repo = rev_repo
        self.catalog = catalog
        self.user_identity_store = user_identity_store
        self._connected = False

    async def _ensure_connected(self) -> None:
        """懒连接 repo（B1 修复：create_app 是同步函数不能 await，首次操作时建连）。

        对齐 `_lifecycle_persistence._start_persistence` 对 message/token/version 等 repo
        的 connect() 语义：建真实 Mongo + ensure_index；失败抛 StorageUnavailableError
        （fail-closed → 503），不静默降级。注入 fake 的 connect() 短接返回 True 保留测试行为。
        """
        if self._connected:
            return
        if not await self.repo.connect():
            raise CharacterStorageUnavailableError("CharacterRepository 连接失败")
        if not await self.rev_repo.connect():
            raise CharacterStorageUnavailableError("CharacterRevisionRepository 连接失败")
        self._connected = True

    async def validate(self, draft: CharacterDraft) -> CharacterValidationResult:
        """纯校验（无副作用）；供 POST /api/characters/validate 复用。"""
        issues: list[CharacterValidationIssue] = []
        # 第一层 Pydantic 已抛；此处收第二层语义
        profile = draft.promptProfile
        for field, text in (("corePersonality", profile.corePersonality),
                            ("relationshipStyle", profile.relationshipStyle),
                            ("speakingStyle", profile.speakingStyle),
                            ("emotionStyle", profile.emotionStyle),
                            ("conversationInitiative", profile.conversationInitiative)):
            for iss in _semantic_issues(text, f"/promptProfile/{field}"):
                issues.append(iss)
        valid = not any(i.severity == "error" for i in issues)
        return CharacterValidationResult(valid=valid, issues=issues)

    async def create(self, draft: CharacterDraft, *, owner_user_id: str,
                     created_by: str = "user") -> CharacterDetail:
        await self._ensure_connected()
        err = await self._raise_on_validation(draft)
        if err:
            raise err
        character_id = f"chr_custom_{uuid.uuid4().hex[:16]}"
        rev = self._revision_from_draft(character_id, draft, revision=1, created_by=created_by)
        # Revision-first：先写孤儿 Revision，再插 Definition；失败回滚删除孤儿
        await self.rev_repo.append_revision(rev)
        try:
            defn = CharacterDefinition(characterId=character_id, slug=draft.displayName,
                                       source="custom", mutable=True, status="active",
                                       currentRevision=1, ownerUserId=owner_user_id)
            await self.repo.insert_definition(defn)
        except Exception:
            await self.rev_repo.delete_revision(character_id, 1)
            raise
        return _detail(defn, rev)

    async def get(self, character_id: str) -> CharacterDetail | None:
        await self._ensure_connected()
        pair = await self._get_definition_and_revision(character_id)
        if pair is None:
            return None
        defn, rev = pair
        return _detail(defn, rev)

    async def list(self, *, source: str | None = None, status: str = "active",
                   search: str | None = None, cursor: str | None = None,
                   limit: int = 50) -> CharacterListResponse:
        await self._ensure_connected()
        # 自定义：Definition 无 displayName——需读 current Revision 解析 displayName 做摘要。
        # builtin：catalog.list() 返回内置 Definition；displayName 从 BuiltinCharacterSource.load 的 revision 取。
        custom = await self.repo.list_definitions(source=source, status=status,
                                                    owner_user_id="usr_local_default",
                                                    search=search, limit=limit)
        items: list[CharacterSummary] = []
        for d in custom:
            rev = await self.rev_repo.get_revision(d.characterId, d.currentRevision)
            items.append(_summary(d, display_name=rev.displayName if rev else d.slug))
        if source is None or source == "builtin":
            for d in self.catalog.list():
                if d.status == status:
                    try:
                        # A5 修复：用 catalog 公开的 get_revision()，不摸私有 _source。
                        pair = self.catalog.get_revision(d.characterId)
                        rev = pair[1] if pair else None
                        items.append(_summary(d, display_name=rev.displayName if rev else d.slug))
                    except Exception:
                        items.append(_summary(d, display_name=d.slug))
        return CharacterListResponse(items=items, has_more=False)

    async def update(self, character_id: str, draft: CharacterDraft, *,
                     expected_revision: int, state_policy: str = "preserve") -> CharacterMutationResult:
        await self._ensure_connected()
        err = await self._raise_on_validation(draft)
        if err:
            raise err
        defn = await self._get_definition(character_id)
        if defn is None:
            raise CharacterNotFoundError(character_id)
        if not defn.mutable:
            raise CharacterImmutableError(character_id)
        if defn.status == "archived":
            raise CharacterArchivedError(character_id)
        new_rev = await self.rev_repo.max_revision(character_id) + 1
        new_content = self._revision_from_draft(character_id, draft, revision=new_rev)
        await self.rev_repo.append_revision(new_content)
        ok = await self.repo.cas_revision_pointer(character_id, expected_revision, new_rev,
                                                   updated_at=now_iso())
        if not ok:
            await self.rev_repo.delete_revision(character_id, new_rev)
            raise RevisionConflictError(character_id)
        defn.currentRevision = new_rev
        return CharacterMutationResult(
            character=_detail(defn, new_content),
            stateImpact=CharacterStateImpact(statePolicy=state_policy, affectedInstances=0,
                                              pendingInstances=0, resetFields=[]),
        )

    async def clone(self, character_id: str, *, display_name: str | None = None) -> CharacterDetail:
        src = await self.get(character_id)
        if src is None:
            raise CharacterNotFoundError(character_id)
        profile = src.current.promptProfile
        # 内置 revision 的 relationshipStyle/speakingStyle 可能留空（builtin_source.py:53
        # relationshipStyle/emotionStyle 本批留空）——CharacterDraft 要求这些字段 min_length=1，
        # 克隆时对空字段给占位默认，避免 ValidationError。
        new_draft = CharacterDraft(
            displayName=display_name or src.summary.displayName,
            description=src.current.description,
            aliases=src.current.aliases,
            promptProfile=PromptProfile(
                corePersonality=profile.get("corePersonality") or "暂无核心人格。",
                relationshipStyle=profile.get("relationshipStyle") or "保持友好而自然的互动。",
                speakingStyle=profile.get("speakingStyle") or "用日常口语对话。",
                emotionStyle=profile.get("emotionStyle", ""),
                conversationInitiative=profile.get("conversationInitiative", ""),
                examples=profile.get("examples", []),
            ),
            runtimeDefaults=RuntimeDefaults(**src.current.runtimeDefaults),
            presentation=Presentation(**src.current.presentation),
            openingMessage=src.current.openingMessage,
        )
        return await self.create(new_draft, owner_user_id="usr_local_default")

    async def archive(self, character_id: str) -> CharacterArchiveResult:
        await self._ensure_connected()
        defn = await self._get_definition(character_id)
        if defn is None:
            raise CharacterNotFoundError(character_id)
        if not defn.mutable:
            raise CharacterImmutableError(character_id)
        await self.repo.archive(character_id)
        defn.status = "archived"
        return CharacterArchiveResult(character=_summary(defn))

    async def rollback(self, character_id: str, *, target_revision: int,
                       expected_revision: int, state_policy: str = "preserve") -> CharacterMutationResult:
        await self._ensure_connected()
        old = await self.rev_repo.get_revision(character_id, target_revision)
        if old is None:
            raise CharacterNotFoundError(character_id)
        new_rev = await self.rev_repo.max_revision(character_id) + 1
        # C2 修复：rollback 复制旧版本为新 revision——必须透传 description/aliases/
        # schemaVersion/compilerVersion，否则回滚丢失这些字段（数据丢失 + 版本元数据错误）。
        content = CharacterRevision(characterId=character_id, revision=new_rev,
                                    schemaVersion=old.schemaVersion,
                                    displayName=old.displayName,
                                    description=old.description,
                                    aliases=old.aliases,
                                    promptProfile=old.promptProfile,
                                    runtimeDefaults=old.runtimeDefaults,
                                    presentation=old.presentation,
                                    openingMessage=old.openingMessage,
                                    checksum=old.checksum,
                                    compilerVersion=old.compilerVersion,
                                    createdBy="user")
        # Revision-first：先 append 新 revision（durable），再 CAS 指针
        await self.rev_repo.append_revision(content)
        ok = await self.repo.cas_revision_pointer(character_id, expected_revision, new_rev,
                                                   updated_at=now_iso())
        if not ok:
            await self.rev_repo.delete_revision(character_id, new_rev)
            raise RevisionConflictError(character_id)
        defn = await self.repo.get_definition(character_id)
        if defn is None:  # CAS 成功但 definition 缺失（异常态）——防御
            raise CharacterNotFoundError(character_id)
        defn.currentRevision = new_rev
        return CharacterMutationResult(
            character=_detail(defn, content),
            stateImpact=CharacterStateImpact(statePolicy=state_policy, affectedInstances=0,
                                              pendingInstances=0, resetFields=[]),
        )

    # ---- helpers ----

    async def _get_definition(self, character_id: str) -> CharacterDefinition | None:
        """取 Definition（custom：repo；builtin：catalog）——不含 revision。

        update/archive 用它判断 mutable/status（builtin 会因 mutable=False 抛 immutable）。
        """
        defn = await self.repo.get_definition(character_id)
        if defn is not None:
            return defn
        return self.catalog.get(character_id)

    async def _get_definition_and_revision(
        self, character_id: str,
    ) -> tuple[CharacterDefinition, CharacterRevision] | None:
        """取 Definition + 当前 Revision。

        custom：repo.get_definition + rev_repo.get_revision(currentRevision)。
        builtin：catalog.get_revision（BuiltinCharacterSource.load 的 revision，
        不受 custom 版本 repo 影响——B9 修复：内置角色的 revision 不在 character_revisions）。
        """
        defn = await self._get_definition(character_id)
        if defn is None:
            return None
        if defn.source == "builtin":
            rev_pair = self.catalog.get_revision(character_id)
            if rev_pair is None:
                return None
            _, rev = rev_pair
            return (defn, rev)
        rev = await self.rev_repo.get_revision(character_id, defn.currentRevision)
        return (defn, rev) if rev is not None else None

    async def _raise_on_validation(self, draft: CharacterDraft) -> CharacterValidationError | None:
        result = await self.validate(draft)
        if not result.valid:
            return CharacterValidationError(str(result.issues))
        return None

    def _revision_from_draft(self, character_id: str, draft: CharacterDraft, *,
                             revision: int, created_by: str = "user") -> CharacterRevision:
        examples = [e.model_dump() for e in draft.promptProfile.examples]
        profile = {
            "corePersonality": draft.promptProfile.corePersonality,
            "relationshipStyle": draft.promptProfile.relationshipStyle,
            "emotionStyle": draft.promptProfile.emotionStyle,
            "speakingStyle": draft.promptProfile.speakingStyle,
            "conversationInitiative": draft.promptProfile.conversationInitiative,
            "examples": examples,
        }
        return CharacterRevision(
            characterId=character_id, revision=revision, displayName=draft.displayName,
            description=draft.description, aliases=draft.aliases, promptProfile=profile,
            runtimeDefaults=draft.runtimeDefaults.model_dump(),
            presentation=draft.presentation.model_dump(),
            openingMessage=draft.openingMessage, createdBy=created_by,
        )


def _semantic_issues(text: str, path: str) -> list[CharacterValidationIssue]:
    """把校验判定的 issue 转为契约 CharacterValidationIssue（path JSON Pointer）。"""
    from core.character.draft import validate_semantic
    return [CharacterValidationIssue(severity=i.severity, code=i.code, path=path, message=i.message)
            for i in validate_semantic(text)]
