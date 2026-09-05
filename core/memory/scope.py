"""MemoryScope — 记忆作用域（架构稿 §8.1）。

任意记忆写/读必须显式带 scope；不存在「漏传即全局」默认值。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MemoryVisibility(StrEnum):
    CHARACTER_PRIVATE = "character_private"  # 角色私有（产品记忆一律此值）
    USER_SHARED = "user_shared"              # 预留枚举，不提供自动升级路径


@dataclass(frozen=True)
class MemoryScope:
    user_id: str
    character_instance_id: str
    visibility: MemoryVisibility = MemoryVisibility.CHARACTER_PRIVATE
