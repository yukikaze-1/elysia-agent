"""实例/状态写链：演化重置、revision 对账、传播/挂起与运行时 busy 判定。

自 core/character/service.py 拆出。定义写链发布新 revision 后由本链把变更落到
instance：`follow_latest` 原子推进、`pinned` 保持；中断留下的 pending 标记由
`reconcile_instance_revision` 的懒对账收敛（`runtime_loader` 也走这条路）。

`_clear_evolution` / `_clear_runtime_components` 是本链自己的原语（带副作用，
故不进 `helpers.py` 的纯函数区）：前者清状态字段，后者经公开契约复位引擎/记忆反射器。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Literal

from core.character.service.base import CharacterServiceBase
from core.character.service.errors import (
    CharacterBusyError,
    CharacterInstanceNotFoundError,
    CharacterNotFoundError,
    CharacterStateConflictError,
    CharacterStorageUnavailableError,
    RevisionConflictError,
)
from core.character.service.helpers import (
    INSTANCE_RECONCILE_RETRIES,
    MAX_CHARACTER_LIST_SCAN,
)
from core.schema.utils import now_iso
from core.storage.base import StorageUnavailableError

if TYPE_CHECKING:
    from core.character.instance import CharacterInstance
    from core.character.runtime import CharacterRuntime
    from core.profile.user_identity import UserIdentity
    from core.schema.character import CharacterRuntimeState

# 日志名固定为拆分前的模块名：`%(name)s` 出现在每行日志里，拆分不改日志分流。
logger = logging.getLogger("core.character.service")


class CharacterInstanceStateWriteChain(CharacterServiceBase):
    """实例/状态写链混入：演化重置 + revision 传播/对账。"""

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
