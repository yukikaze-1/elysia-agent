"""CharacterDefinition — 稳定角色身份（架构稿 §5.2）。不存 Prompt 正文/状态/session。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from core.schema.utils import now_iso


class CharacterDefinition(BaseModel):
    characterId: str                       # 稳定 ID，如 chr_builtin_elysia
    slug: str = ""                         # 文件/检索友好标识
    source: str = "builtin"                # builtin/custom
    mutable: bool = False                  # 内置 false
    status: str = "active"                 # active/archived
    currentRevision: int = Field(default=1, ge=1)
    ownerUserId: str | None = None         # custom 所有者；builtin 为空
    createdAt: str = Field(default_factory=now_iso)
    updatedAt: str = Field(default_factory=now_iso)
