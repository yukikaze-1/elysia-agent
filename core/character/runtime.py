"""CharacterRuntime — 单个角色实例的运行时容器（架构稿 §7.1/§7.2）。

装载 Definition/Revision 快照 + UserIdentity 快照 + CharacterRuntimeState，
持有 per-instance Lock + dirty/stateVersion。
engine 懒建（M3：每 instance 一个 engine；state 权威已切换至
runtime.state，flush 经 persist_fn 引擎持久化）。
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Callable, Awaitable

from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.character.instance import CharacterInstance
from core.profile.user_identity import UserIdentity
from core.schema.character import CharacterRuntimeState
from core.schema.types import SessionId

if TYPE_CHECKING:
    from frontend_contract.rest import CharacterPublicStateView
    from core.character.runtime_profile import RuntimeCharacterProfile
    from core.personality.engine import CharacterEngine
    from core.memory.scoped_long_term import ScopedLongTermMemory
    from core.memory.consolidator import MemoryConsolidator
    from core.memory.memory_reflector import MemoryReflector


class CharacterRuntime:
    """单实例运行时容器（M3：engine per-instance + state 真实化）。"""

    def __init__(
        self,
        instance: CharacterInstance,
        definition: CharacterDefinition,
        revision: CharacterRevision,
        user_identity: UserIdentity,
        memory_view: "ScopedLongTermMemory | None" = None,
        consolidator: "MemoryConsolidator | None" = None,
        reflector: "MemoryReflector | None" = None,
        profile: "RuntimeCharacterProfile | None" = None,
    ):
        self.instance = instance
        self.definition = definition
        self.revision = revision
        self.user_identity = user_identity
        self.memory_view = memory_view
        self.consolidator = consolidator
        self.reflector = reflector
        self.profile = profile
        self._engine: "CharacterEngine | None" = None
        self.lock = asyncio.Lock()
        self.dirty = False
        # BE-STAGEH-01：generation 与破坏性变更（reset-evolution）的互斥 lease。
        # 判定 + 登记必须落在同一原子步内，故用同步 _op_guard——asyncio.Lock 的
        # acquire 是 await 点，会让「检查」与「登记」之间被其他任务插入。
        self._op_guard = threading.Lock()
        self._generation_ops = 0
        self._destructive_ops = 0
        self._busy = False
        self.stateVersion = instance.stateVersion
        self.state: CharacterRuntimeState = instance.state or CharacterRuntimeState(
            instance_id=instance.instanceId,
            session_id=SessionId(instance.instanceId),
            source_revision=revision.revision,
        )
        self._persist_fn: Callable[[CharacterRuntimeState], Awaitable[None]] | None = None
        self._last_public_state: "CharacterPublicStateView | None" = None

    @property
    def engine(self) -> "CharacterEngine | None":
        return self._engine

    def ensure_engine(self, factory: Callable[[], "CharacterEngine"]) -> "CharacterEngine":
        if self._engine is None:
            self._engine = factory()
        engine = self._engine
        assert engine is not None
        return engine

    def bind_engine(self, engine: "CharacterEngine") -> None:
        self._engine = engine

    # ---- BE-STAGEH-01：generation / 破坏性变更 lease ----

    @property
    def is_generation_busy(self) -> bool:
        return self._generation_ops > 0

    @property
    def is_destructive_mutation_active(self) -> bool:
        return self._destructive_ops > 0

    def try_begin_generation(self) -> bool:
        """登记一次 generation 生命周期；破坏性变更在途时拒绝（对称于 reset）。

        与 ``try_begin_destructive_mutation`` 共用同一把同步锁与同一对计数器，
        构成两个方向互斥的原子边界——generation 不能只依赖 HTTP route 的
        ``_process_lock`` 与 reset 互斥（领域层须独立守住不变量）。

        Returns:
            True 表示已登记（调用方负责在 finally 中 ``end_generation()``）；
            False 表示破坏性变更正在执行，调用方不得开始 generation。
        """
        with self._op_guard:
            if self._destructive_ops > 0:
                return False
            self._generation_ops += 1
            self._busy = True
            return True

    def begin_generation_tail(self) -> None:
        """为回合的后台收尾（``_after_response``）追加一份 lease。

        主回合的 lease 在 ``process()`` 返回时释放，但记忆沉淀/性格反思/
        MemoryReflector/会话保存仍在后台 task 中运行。tail lease 由该 task 在
        finally 中释放，使 busy 覆盖到「最后一次相关状态持久化」完成。
        """
        with self._op_guard:
            self._generation_ops += 1
            self._busy = True

    def end_generation(self) -> None:
        with self._op_guard:
            if self._generation_ops > 0:
                self._generation_ops -= 1
            self._busy = self._generation_ops > 0 or self._destructive_ops > 0

    def try_begin_destructive_mutation(self) -> bool:
        """破坏性变更（reset-evolution）的原子 check-and-set。

        generation 在途或反思/压缩持锁时返回 False——调用方抛
        CharacterBusyError。检查与登记同处一把同步锁内，即 BE-STAGEH-01
        不变量 #3 要求的原子边界。

        并发 reset 之间不做互斥（两个都可通过）：它们由 runtime.lock +
        storage CAS 串行化，后者负责把第二个判为 STATE_CONFLICT。
        """
        with self._op_guard:
            if self._generation_ops > 0:
                return False
            if self.evolution_locks_busy():
                return False
            self._destructive_ops += 1
            self._busy = True
            return True

    def end_destructive_mutation(self) -> None:
        with self._op_guard:
            if self._destructive_ops > 0:
                self._destructive_ops -= 1
            self._busy = self._generation_ops > 0 or self._destructive_ops > 0

    def evolution_locks_busy(self) -> bool:
        """反思/压缩锁是否被持有——reset 的第二个 busy 来源。"""
        engine = self._engine
        locks = [
            getattr(getattr(engine, "_reflector", None), "_reflect_lock", None),
            getattr(getattr(engine, "_compression", None), "_compression_lock", None),
            getattr(getattr(self, "reflector", None), "_lock", None),
        ]
        return any(lock is not None and lock.locked() for lock in locks)

    def set_persist_fn(self, fn: Callable[[CharacterRuntimeState], Awaitable[None]] | None) -> None:
        self._persist_fn = fn

    def snapshot_state(self) -> CharacterRuntimeState:
        return self.state.model_copy(deep=True)

    def public_state(self) -> "CharacterPublicStateView":
        """当前产品态视图(经 make_public_state)。不做变化检测——仅计算视图。"""
        from core.character.public_state import make_public_state
        return make_public_state(self)

    def has_public_state_changed(self, new_view: "CharacterPublicStateView") -> bool:
        """对比 _last_public_state,仅 mood.label/mood.intensity 视为变化信号。

        不比较 updated_at(展示字段,每次 flush 都变)、proactiveEnabled(immutable 配置常量)。
        首次(_last_public_state None)视为有变化(供第一次发射)。
        """
        last = self._last_public_state
        if last is None:
            return True
        if last.mood is None or new_view.mood is None:
            return last.mood is not new_view.mood
        return (last.mood.label != new_view.mood.label
                or last.mood.intensity != new_view.mood.intensity)

    async def flush(self) -> None:
        if self._persist_fn is not None:
            await self._persist_fn(self.state)
        self.dirty = False
