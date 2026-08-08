"""记忆提取类型 — Consolidator LLM 输出的 Pydantic 模型。"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

from core.schema.types import MemoryId


class MemoryType(StrEnum):
    """记忆类型枚举。值与 ExtractMemory.j2 / ConsolidationCompare.j2 的大写词表一致。"""
    FACT = "Fact"
    PREFERENCE = "Preference"
    EVENT = "Event"
    PLAN = "Plan"
    NOTE = "Note"

    @classmethod
    def try_coerce(cls, value: object) -> "MemoryType | None":
        """任意来源 → MemoryType；未知/None/非 str 返回 None（不抛）。

        精确匹配 → 大小写不敏感（.title()）匹配。过滤边界用它：未知值 → None → 不命中。
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            text = value.strip()
            if text:
                try:
                    return cls(text)          # 精确匹配（"Fact"）
                except ValueError:
                    pass
                try:
                    return cls(text.title())  # 大小写不敏感（"fact"/"FACT"/"preference"）
                except ValueError:
                    pass
        return None

    @classmethod
    def coerce(cls, value: object) -> "MemoryType":
        """try_coerce 的 NOTE 兜底版——存储/构造边界用（未知值归一为 Note，不崩）。"""
        return cls.try_coerce(value) or cls.NOTE


class CompareActionType(StrEnum):
    """Consolidator 比对动作枚举（CompareAction 判别字段值）。"""
    SKIP = "skip"
    INSERT = "insert"
    UPDATE = "update"
    MERGE = "merge"


class ExtractedMemory(BaseModel):
    """LLM 提取的单条记忆。ExtractMemory.j2 输出格式。

    poignancy 保留默认值 5——与现状 .get("poignancy", 5) 一致，防止 LLM 偶发性缺失
    字段时整条记忆被丢弃。
    """
    type: MemoryType
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    poignancy: int = Field(default=5, ge=1, le=10)


class SkipAction(BaseModel):
    action: Literal[CompareActionType.SKIP] = CompareActionType.SKIP


class InsertAction(BaseModel):
    action: Literal[CompareActionType.INSERT] = CompareActionType.INSERT
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    type: MemoryType
    poignancy: int = Field(ge=1, le=10)


class UpdateAction(BaseModel):
    """type/poignancy 在执行分支中从不消费，设为可选保留降级兼容。"""
    action: Literal[CompareActionType.UPDATE] = CompareActionType.UPDATE
    target_id: MemoryId = Field(min_length=1)
    new_content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    type: MemoryType | None = None
    poignancy: int | None = Field(default=None, ge=1, le=10)


class MergeAction(BaseModel):
    """type/poignancy 传入 update_text()（其签名接受 None 表示"不更新此字段"）。
    可选以保留 LTM 的 None-语义。"""
    action: Literal[CompareActionType.MERGE] = CompareActionType.MERGE
    target_id: MemoryId = Field(min_length=1)
    merged_content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    type: MemoryType | None = None
    poignancy: int | None = Field(default=None, ge=1, le=10)


CompareAction = Annotated[
    SkipAction | InsertAction | UpdateAction | MergeAction,
    Field(discriminator="action"),
]
