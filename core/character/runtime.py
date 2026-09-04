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
    from core.personality.engine import CharacterEngine


class CharacterRuntime:
    """单实例运行时容器（M3：engine per-instance + state 真实化）。"""

    def __init__(
        self,
        instance: CharacterInstance,
        definition: CharacterDefinition,
        revision: CharacterRevision,
        user_identity: UserIdentity,
    ):
        self.instance = instance
        self.definition = definition
        self.revision = revision
        self.user_identity = user_identity
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

    async def flush(self) -> None:
        if self._persist_fn is not None:
            await self._persist_fn(self.state)
        self.dirty = False
