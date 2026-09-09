"""TurnCharacterContext — 单个生成回合的不可变角色/用户/状态/版本快照。

架构稿 §7.3：_prepare_turn() 一次性解析并冻结。此后 Prompt/TTS/mood/记忆/generation
全用同一 context，不能在回合中重新读取『当前活动角色』。

P0-2：用户身份同样按回合冻结——Prompt 的用户名/称呼/代词/时区一律来自本轮快照，
不再读 config.agent.user_name。这些是后端内部字段，不改变前端 wire contract。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.profile.user_identity import (
    LOCAL_DEFAULT_LOCALE,
    LOCAL_DEFAULT_PRONOMS,
    LOCAL_DEFAULT_TIMEZONE,
    LOCAL_DEFAULT_USER_ID,
)
from core.schema.character import CharacterRuntimeState


@dataclass(frozen=True)
class TurnCharacterContext:
    """不可变回合上下文（M3：memoryScope 预埋 F + voice/displayName/stateSnapshot）。"""

    conversationId: str
    characterInstanceId: str
    characterId: str
    characterRevision: int
    userPronouns: str = LOCAL_DEFAULT_PRONOMS   # UserIdentity.pronouns（B1 全局唯一「她」）
    memoryScope: str = ""
    voice: dict = field(default_factory=dict)
    displayName: str = ""
    stateSnapshot: CharacterRuntimeState | None = None
    # ---- P0-2：本轮权威用户身份快照（Mongo 权威，回合内冻结）----
    userId: str = LOCAL_DEFAULT_USER_ID
    userDisplayName: str = ""
    userPreferredAddress: str | None = None
    userLocale: str = LOCAL_DEFAULT_LOCALE
    userTimezone: str = LOCAL_DEFAULT_TIMEZONE
    userIdentityRevision: int = 0

    @property
    def effectiveUserAddress(self) -> str:
        """有效称呼：preferredAddress 非空优先，否则回退 displayName。

        两者都为空时回退「用户」（中性占位，不泄漏任何内部标识）。
        """
        address = (self.userPreferredAddress or "").strip()
        if address:
            return address
        name = (self.userDisplayName or "").strip()
        return name or "用户"
