"""CharacterRuntime — 单个角色实例的运行时容器（架构稿 §7.1/§7.2）。

装载 Definition/Revision 快照 + UserIdentity 快照 + CharacterRuntimeState，
持有 per-instance Lock + dirty/stateVersion。
engine 懒建（M3：每 instance 一个 engine；state 权威已切换至
runtime.state，flush 经 persist_fn 引擎持久化）。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable, Awaitable

from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.character.instance import CharacterInstance
from core.profile.user_identity import UserIdentity
from core.schema.character import CharacterRuntimeState
from core.schema.types import SessionId

if TYPE_CHECKING:
    from frontend_contract.rest import CharacterPublicStateView
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
    ):
        self.instance = instance
        self.definition = definition
        self.revision = revision
        self.user_identity = user_identity
        self.memory_view = memory_view
        self.consolidator = consolidator
        self.reflector = reflector
        self._engine: "CharacterEngine | None" = None
        self.lock = asyncio.Lock()
        self.dirty = False
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
