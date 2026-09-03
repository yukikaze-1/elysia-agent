"""CharacterRevision — 不可变角色设定快照（架构稿 §5.3，精简到当前系统所需字段）。

本批渐进式：promptProfile 只承载有源字段（corePersonality/speakingStyle），
relationshipStyle/emotionStyle/conversationInitiative 本批显式留空（渲染走模板）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from core.schema.utils import now_iso

REVISION_PROMPT_PROFILE_KEYS = ("corePersonality", "relationshipStyle", "emotionStyle",
                              "speakingStyle", "conversationInitiative")


class CharacterRevision(BaseModel):
    characterId: str
    revision: int = Field(default=1, ge=1)
    schemaVersion: int = Field(default=1, ge=1)
    displayName: str
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    promptProfile: dict[str, object] = Field(default_factory=dict)  # 见 REVISION_PROMPT_PROFILE_KEYS
    runtimeDefaults: dict[str, object] = Field(default_factory=dict)  # 声音/表情/唤醒词/主动触发
    presentation: dict[str, object] = Field(default_factory=dict)     # 头像/Live2D 等引用
    openingMessage: str | None = None
    checksum: str = ""
    compilerVersion: int = Field(default=1, ge=1)
    createdBy: str = "system"              # system/user/migration
    createdAt: str = Field(default_factory=now_iso)
