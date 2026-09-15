"""定义写链：create / update / clone / archive / rollback。

自 core/character/service.py 拆出。Revision-first 写序（先 `append_revision` 再
`cas_revision_pointer`，CAS 失败回滚已 append 的 revision——`current_revision` 永远
指向 durable revision）与归档前的默认角色改指都在这里。

本混入**显式继承**它真正依赖的两条链：读链（`clone` 读源角色）与实例/状态写链
（`update`/`rollback` 的 revision 传播与 busy 判定、`archive` 的默认角色改指）。
"""

from __future__ import annotations

import uuid

from core.character.definition import CharacterDefinition
from core.character.draft import (
    CharacterDraft,
    ConversationExample,
    Presentation,
    PromptProfile,
    RuntimeDefaults,
)
from core.character.service.errors import (
    CharacterArchivedError,
    CharacterImmutableError,
    CharacterNotFoundError,
    RevisionConflictError,
)
from core.character.service.helpers import (
    _checksum,
    _detail,
    _normalise_revision,
    _parse_state_policy,
    _state_impact,
    _summary,
)
from core.character.service.instances import CharacterInstanceStateWriteChain
from core.character.service.queries import CharacterQueryChain
from core.schema.utils import now_iso
from frontend_contract.rest import (
    CharacterArchiveResult,
    CharacterCreatedBy,
    CharacterDetail,
    CharacterMutationResult,
)


async def _append_revision_or_conflict(rev_repo: object, revision: object,
                                        character_id: str) -> None:
    """写 revision，把**良性并发冲突**映射成 `RevisionConflictError`（S2-B2）。

    为什么需要：`max_revision() + 1` 不是原子的，编辑器双击保存 / 两进程并发
    会产生同一 `(character_id, revision)` 双写。唯一索引正常工作时会有一个 loser
    抛 `DuplicateKeyError`——那是**竞争信号**，不是存储故障：

    - 不映射 → 冒泡成 500（用户看到"服务器错误"而不是"已被他人修改，请刷新"）；
    - 更糟的是若仓储把它当存储故障判死（`_mark_unavailable`），整个仓储最长 60s
      不可用。

    storage 层只裸抛原始 `DuplicateKeyError`（不 import domain 异常，避免
    storage→character 反向依赖），语义归属在这一层。

    Args:
        rev_repo: `CharacterRevisionRepository`。
        revision: 待写入的 `CharacterRevision`。
        character_id: 冲突时回填给异常的角色 ID。

    Raises:
        RevisionConflictError: 同一 (character_id, revision) 已存在（并发双写）。
    """
    from core.storage.base import is_duplicate_key

    try:
        await rev_repo.append_revision(revision)     # type: ignore[attr-defined]
    except Exception as exc:
        if is_duplicate_key(exc):
            raise RevisionConflictError(character_id, None) from exc
        raise


class CharacterDefinitionWriteChain(CharacterQueryChain, CharacterInstanceStateWriteChain):
    """定义写链混入：角色定义的创建/更新/克隆/归档/回滚。"""

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
        await _append_revision_or_conflict(self.rev_repo, revision, character_id)
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
        await _append_revision_or_conflict(self.rev_repo, content, character_id)
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
        await _append_revision_or_conflict(self.rev_repo, content, character_id)
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
