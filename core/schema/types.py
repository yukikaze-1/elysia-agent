"""core/schema 基础类型 — 时间戳、标识符别名。"""

from typing import Annotated, NewType

from pydantic import BeforeValidator

from .utils import _normalize_iso_ts


IsoTimestamp = Annotated[str, BeforeValidator(_normalize_iso_ts)]
"""ISO 8601 时间戳，构造时自动校验格式并归一化到 UTC。

使用 Pydantic BeforeValidator：空字符串和格式错误会抛出 ValidationError。
"""

SessionId = NewType("SessionId", str)
"""会话唯一标识符。

注意：与 Module/workflow_engine/schema/types.py 的 SessionId 同名共存（Phase 1）。
两者是相同的 NewType，运行时完全兼容（都是 str）。Phase 2 统一迁移到此。
"""

MessageId = NewType("MessageId", str)
"""消息唯一标识符。"""

ToolCallId = NewType("ToolCallId", str)
"""工具调用唯一标识符。"""
