"""CharacterInstance — 用户正在相处并持续演化的角色（架构稿 §5）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from core.schema.character import CharacterRuntimeState
from core.schema.utils import now_iso


class CharacterInstance(BaseModel):
    instanceId: str
    characterId: str
    revision: int = Field(default=1, ge=1)   # 创建时 1
    revisionPolicy: Literal["follow_latest", "pinned"] = "follow_latest"
    # Definition 更新是跨集合写入。pending 字段让 statePolicy 在 revision 指针
    # 已发布但 instance 尚未安全升级时仍然可恢复，不会成为无人知晓的半完成状态。
    pendingRevision: int | None = Field(default=None, ge=1)
    pendingStatePolicy: Literal["preserve", "reset_evolution"] | None = None
    ownerUserId: str = "usr_local_default"   # 拥有者
    canonical: bool = True                    # (user, character) 默认连续人格实例
    stateVersion: int = Field(default=1, ge=1)  # 状态版本，CAS
    state: CharacterRuntimeState | None = None   # 阶段 E：instance-scoped 状态权威持久化落点
    createdAt: str = Field(default_factory=now_iso)
    updatedAt: str = Field(default_factory=now_iso)
