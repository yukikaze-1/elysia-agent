"""CharacterInstance — 用户正在相处并持续演化的角色（架构稿 §5）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from core.schema.character import CharacterRuntimeState
from core.schema.utils import now_iso


class CharacterInstance(BaseModel):
    instanceId: str
    characterId: str
    revision: int = Field(default=1, ge=1)   # 创建时 1
    ownerUserId: str = "usr_local_default"   # 拥有者
    canonical: bool = True                    # (user, character) 默认连续人格实例
    stateVersion: int = Field(default=1, ge=1)  # 状态版本，CAS
    state: CharacterRuntimeState | None = None   # 阶段 E：instance-scoped 状态权威持久化落点
    createdAt: str = Field(default_factory=now_iso)
    updatedAt: str = Field(default_factory=now_iso)
