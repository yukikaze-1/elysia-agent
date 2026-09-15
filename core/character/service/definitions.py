"""定义写链：create / update / clone / archive / rollback。

自 core/character/service.py 拆出。Revision-first 写序（先 `append_revision` 再
`cas_revision_pointer`，CAS 失败回滚已 append 的 revision——`current_revision` 永远
指向 durable revision）与归档前的默认角色改指都在这里。

本混入**显式继承**它真正依赖的两条链：读链（`clone` 读源角色）与实例/状态写链
（`update`/`rollback` 的 revision 传播与 busy 判定、`archive` 的默认角色改指）。
"""

from __future__ import annotations

import logging
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

logger = logging.getLogger("core.character.service")


async def _compensate_delete_revision(
    rev_repo: object, character_id: str, revision: int,
) -> None:
    """补偿删除刚追加、但没能发布的 revision——**失败只记 warning，绝不顶替原始异常**。

    #46 缺口行 8 · 6 号缺陷：原来的补偿是裸 `await delete_revision(...)`。补偿自己抛
    （比如存储已经从"写失败"恶化到"删也失败"）时会**顶替**正在抛出的那个异常——
    调用方看到的是补偿的错误，原始成因既不在异常链上（无 `__cause__`）也不在日志里。
    善后动作不该把病因挤掉；孤儿 revision 是可对账的（`list_revisions` 里看得见），
    所以这里降级成一条带 traceback 的 warning。

    Args:
        rev_repo: `CharacterRevisionRepository`。
        character_id: 角色 ID。
        revision: 要删的 revision 号。
    """
    try:
        await rev_repo.delete_revision(character_id, revision)  # type: ignore[attr-defined]
    except Exception:
        logger.warning(
            "角色写链补偿失败：revision 未被删除（孤儿，需对账）character_id=%s revision=%d",
            character_id, revision, exc_info=True,
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

    async def _fresh_current_revision(self, character_id: str) -> int | None:
        """**绕过缓存**回读库里的当前版本；读不到 → None（=未知，绝不猜）。

        缓存里的那份定义在 CAS 失败路径上按定义就是过期的（且它与本方法调用方手里的
        对象是同一个，见 `CharacterRepository.insert_definition` 的 `_cache_put`）。
        """
        try:
            fresh = await self.repo.get_definition(character_id, fresh=True)
        except Exception:
            logger.warning(
                "回读角色当前版本失败——上报「未知」而不是过期值 character_id=%s",
                character_id, exc_info=True,
            )
            return None
        return None if fresh is None else fresh.currentRevision

    async def _compensate_orphan_revision(self, character_id: str, new_revision: int) -> None:
        """删掉未发布的孤儿 revision——**仅当它仍是最高版本**。

        为什么需要这个前提（2026-09-15 只读分析实测）：`max_revision()` 读的是 revisions
        集合，而 CAS 比对的是 Definition 的 `currentRevision`——两者可以不同步。并发写者
        完全可能读到**我们已落库但尚未发布**的 revision N，于是它 append N+1 并 CAS
        `expected → N+1`（它的 expected 是它更早读到的旧指针）。此时我们的 CAS 落败，
        若照旧删掉 N，历史就变成 `{…, N-1, N+1}`——**中间缺一格**。

        缺格的后果不是"少一个版本"：`reconcile_instance_revision` 从实例当前版本走到目标
        版本时要逐格取 revision，缺一格拉 `CharacterStorageUnavailableError("角色 revision
        缺失")`——**每次都会再拉**（永久），而且 load 路径（`runtime_loader.py`）没有兜底，
        该实例从此**再也加载不出来**（会话创建/切换 503）。留一条孤儿只是泄漏
        （`list_revisions` 里看得见、可对账），挖洞是不可恢复的——与 #41 同一条纪律：
        选可恢复的那一侧。

        Args:
            character_id: 角色 ID。
            new_revision: 本次调用 append 的版本号。
        """
        try:
            highest = await self.rev_repo.max_revision(character_id)
        except Exception:
            logger.warning(
                "补偿前查最高版本失败——不删（宁可留孤儿，不可挖洞）"
                "character_id=%s revision=%d", character_id, new_revision, exc_info=True,
            )
            return
        if highest != new_revision:
            logger.warning(
                "保留孤儿 revision（库中已有更高版本 %s，删 %d 会在历史里挖洞、让实例"
                "永久无法对账）character_id=%s", highest, new_revision, character_id,
            )
            return
        await _compensate_delete_revision(self.rev_repo, character_id, new_revision)

    async def _commit_revision_pointer(
        self, character_id: str, *, expected_revision: int, new_revision: int,
    ) -> str:
        """推进 `currentRevision` 指针；失败时按**指针到哪了**决定补偿，未知时不猜。

        判据是"指针位置"而不是"有没有抛异常"——这正是 #46 缺口行 8 · 4 号缺陷的根因
        （旧代码只补偿 `ok is False` 分支，抛异常那条路径直接漏过）。三条路径：

        1. **CAS 返回 False**：filter 未命中 → 指针**确定**没到 `new_revision`。刚刚
           append 的 revision 不可能被别人发布（`(character_id, revision)` 唯一索引
           让并发写者拿到的是冲突，不是同一个号），故**确定是孤儿** → 删掉它 → 抛 409。
           409 里的当前版本**回读**得到；回读失败则报 `None`（路由层会省略
           `details.currentRevision`）——**绝不再回报内存里那个过期值**：客户端按它重试
           必然再次冲突（5 号缺陷）。
        2. **CAS 抛异常**：指针**未知**——写可能已经落库、只是响应丢了（驱动超时的
           典型形态）。回读判定：已在 `new_revision` → 视为成功（记 warning 说明那次
           异常是响应路径的）；仍在 `expected_revision` → 指针确定没动 → 补偿删除后
           抛出原始异常；**其余（回读失败 / 第三种值）→ 一个字节都不删**，留孤儿 + error。
        3. 补偿删除自身失败 → 只记 warning（见 `_compensate_delete_revision`），
           原始异常照常抛出。

        **为什么未知时宁可留孤儿**：删掉一条**已发布**的 revision 是数据丢失（指针指向
        不存在的版本 / 历史出现空洞 / 回滚目标消失），留孤儿只是泄漏——#41 已为这类
        「两个失败方向不对称」定过调：选可恢复的那一侧。

        Args:
            character_id: 角色 ID。
            expected_revision: CAS 的期望版本（调用方读到的）。
            new_revision: 已 append、待发布的版本号。

        Returns:
            CAS 使用的 `updated_at`（调用方回填内存定义时用同一个值）。

        Raises:
            RevisionConflictError: CAS 确定未命中。
            Exception: CAS 抛出的原始异常（指针未推进或无法判定时原样上抛）。
        """
        updated_at = now_iso()
        try:
            ok = await self.repo.cas_revision_pointer(
                character_id, expected_revision, new_revision, updated_at=updated_at
            )
        except Exception:
            current = await self._fresh_current_revision(character_id)
            if current == new_revision:
                # 写其实成功了，只是响应丢了。当成成功——否则调用方会以为失败而重试，
                # 而库里已经是新版本了（重试的 CAS 必然因为 expected 过期而失败）。
                logger.warning(
                    "角色指针 CAS 抛异常但指针已推进到目标版本（响应丢失）——按成功处理 "
                    "character_id=%s revision=%d", character_id, new_revision, exc_info=True,
                )
                return updated_at
            if current == expected_revision:
                await self._compensate_orphan_revision(character_id, new_revision)
            else:
                logger.error(
                    "角色指针 CAS 抛异常且指针位置无法判定（回读=%s）——保留孤儿 revision "
                    "待对账，不猜着删 character_id=%s revision=%d",
                    current, character_id, new_revision, exc_info=True,
                )
            raise
        if not ok:
            # 仅在"没有更高版本"时才删——否则会挖出 reconcile 永久修不好的洞。
            await self._compensate_orphan_revision(character_id, new_revision)
            raise RevisionConflictError(
                character_id, await self._fresh_current_revision(character_id)
            )
        return updated_at

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
            # 补偿走受保护版本：补偿自身失败只记 warning，不顶替这里的原始异常（6 号缺陷）。
            await _compensate_delete_revision(self.rev_repo, character_id, 1)
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
        updated_at = await self._commit_revision_pointer(
            character_id, expected_revision=expected_revision, new_revision=new_revision
        )
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
        updated_at = await self._commit_revision_pointer(
            character_id, expected_revision=expected_revision, new_revision=new_revision
        )
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
