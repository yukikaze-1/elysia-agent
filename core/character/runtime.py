"""CharacterRuntime — 单个角色实例的运行时容器（架构稿 §7.1/§7.2）。

装载 Definition/Revision 快照 + UserIdentity 快照 + CharacterRuntimeState，
持有 per-instance Lock + dirty/stateVersion。
engine 懒建（M1/M2：不绑 runtime，engine 仍全局单例；「每 instance 一个 engine、
去掉 attach_session」是 M3 语义）。M1/M2 阶段 state 写入仍走
session.character_state（本 runtime 的 state 只做快照/读对比，不切换权威）。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.character.instance import CharacterInstance
from core.profile.user_identity import UserIdentity

if TYPE_CHECKING:
    from core.personality.engine import CharacterEngine

# CharacterRuntimeState：本批占位（M1 阶段权威仍是 session.character_state）
# 架构稿 §7 / §15.4：backfill + 读对比（不启用），M3 才切换权威。


class CharacterRuntime:
    """单实例运行时容器。"""

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

    @property
    def engine(self) -> "CharacterEngine | None":
        return self._engine

    def bind_engine(self, engine: "CharacterEngine") -> None:
        self._engine = engine

    def snapshot_state(self) -> dict:
        """返回运行时状态快照（本批占位，M1 权威在 session.character_state）。"""
        return {
            "instanceId": self.instance.instanceId,
            "stateVersion": self.stateVersion,
            "dirty": self.dirty,
        }

    async def flush(self) -> None:
        # M1 阶段：state 权威仍在 session.character_state，runtime 不写独立 state。
        # 仅标记 dirty=False（真实 flush 逻辑留 M3）。
        self.dirty = False
