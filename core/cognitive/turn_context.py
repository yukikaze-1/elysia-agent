"""TurnCharacterContext — 单个生成回合的不可变角色/用户/状态/版本快照。

架构稿 §7.3：_prepare_turn() 一次性解析并冻结。此后 Prompt/TTS/mood/记忆/generation
全用同一 context，不能在回合中重新读取『当前活动角色』。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.schema.character import CharacterRuntimeState


@dataclass(frozen=True)
class TurnCharacterContext:
    """不可变回合上下文（M3：memoryScope 预埋 F + voice/displayName/stateSnapshot）。"""

    conversationId: str
    characterInstanceId: str
    characterId: str
    characterRevision: int
    userPronoun: str   # 来自 UserIdentity.pronouns（B1 全局唯一「她」）
    memoryScope: str = ""
    voice: dict = field(default_factory=dict)
    displayName: str = ""
    stateSnapshot: CharacterRuntimeState | None = None
