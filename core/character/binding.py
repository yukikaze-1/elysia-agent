"""会话与角色实例绑定的内部规范化边界。

持久层和 cognitive loop 只使用 snake_case；camelCase 仅作为旧 Stage C/E
迁移数据的兼容输入，读入后立即归一，不继续向内部传播。
"""

from __future__ import annotations

from typing import TypedDict


class CharacterBinding(TypedDict):
    character_id: str
    instance_id: str


def normalize_character_binding(value: object) -> CharacterBinding | None:
    """把持久层/legacy binding 归一为严格的两字段内部形状。"""
    if not isinstance(value, dict):
        return None
    character_id = value.get("character_id", value.get("characterId"))
    instance_id = value.get("instance_id", value.get("instanceId"))
    if not isinstance(character_id, str) or not character_id.strip():
        return None
    if not isinstance(instance_id, str) or not instance_id.strip():
        return None
    return CharacterBinding(
        character_id=character_id.strip(),
        instance_id=instance_id.strip(),
    )
