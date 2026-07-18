"""记忆提取类型 — Consolidator LLM 输出的 Pydantic 模型。"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints


class MemoryType(StrEnum):
    """记忆类型枚举。值与 ExtractMemory.j2 / ConsolidationCompare.j2 的大写词表一致。"""
    FACT = "Fact"
    PREFERENCE = "Preference"
    EVENT = "Event"
    PLAN = "Plan"
    NOTE = "Note"


class ExtractedMemory(BaseModel):
    """LLM 提取的单条记忆。ExtractMemory.j2 输出格式。

    poignancy 保留默认值 5——与现状 .get("poignancy", 5) 一致，防止 LLM 偶发性缺失
    字段时整条记忆被丢弃。
    """
    type: MemoryType
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    poignancy: int = Field(default=5, ge=1, le=10)


class SkipAction(BaseModel):
    action: Literal["skip"] = "skip"


class InsertAction(BaseModel):
    action: Literal["insert"] = "insert"
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    type: MemoryType
    poignancy: int = Field(ge=1, le=10)


class UpdateAction(BaseModel):
    """type/poignancy 在执行分支中从不消费，设为可选保留降级兼容。"""
    action: Literal["update"] = "update"
    target_id: str = Field(min_length=1)
    new_content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    type: MemoryType | None = None
    poignancy: int | None = Field(default=None, ge=1, le=10)


class MergeAction(BaseModel):
    """type/poignancy 传入 update_text()（其签名接受 None 表示"不更新此字段"）。
    可选以保留 LTM 的 None-语义。"""
    action: Literal["merge"] = "merge"
    target_id: str = Field(min_length=1)
    merged_content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    type: MemoryType | None = None
    poignancy: int | None = Field(default=None, ge=1, le=10)


CompareAction = Annotated[
    SkipAction | InsertAction | UpdateAction | MergeAction,
    Field(discriminator="action"),
]
