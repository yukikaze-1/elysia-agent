"""MemoryReflectorState — MemoryReflector 计数器状态 + LLM 输出模型。"""

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from .memory import MemoryType


class MemoryReflectorState(BaseModel):
    mr_round_count: int = 0
    mr_new_memory_count: int = 0
    mr_last_summary: dict[str, object] = Field(default_factory=dict)


class MergeItem(BaseModel):
    """合并操作：ids[0] 保留并更新内容，其余标记 absorbed。"""
    ids: list[str] = Field(min_length=2)
    merged_content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    type: MemoryType | None = None
    poignancy: int | None = Field(default=None, ge=1, le=10)


class UpdateItem(BaseModel):
    """更新操作：改写单条记忆内容。"""
    id: str = Field(min_length=1)
    new_content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class DeleteItem(BaseModel):
    """删除操作。"""
    id: str = Field(min_length=1)


class ReflectorActions(BaseModel):
    """MemoryReflector.j2 输出格式。max_length 即原 _validate_actions 的 5/5/3 上限。"""
    merges: list[MergeItem] = Field(default_factory=list, max_length=5)
    updates: list[UpdateItem] = Field(default_factory=list, max_length=5)
    deletes: list[DeleteItem] = Field(default_factory=list, max_length=3)
