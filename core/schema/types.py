"""core/schema 基础类型 — 时间戳、标识符别名。"""

from datetime import datetime, timezone
from typing import Annotated, NewType

from pydantic import BeforeValidator


def _normalize_iso_ts(ts: str) -> str:
    """归一化 ISO 8601 时间戳：补时区、校验格式。"""
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def now_iso() -> str:
    """返回当前 UTC 时间 ISO 8601 字符串。"""
    return datetime.now(timezone.utc).isoformat()


IsoTimestamp = Annotated[str, BeforeValidator(_normalize_iso_ts)]
"""ISO 8601 时间戳，构造时自动校验格式并归一化到 UTC。

使用 Pydantic BeforeValidator：空字符串和格式错误会抛出 ValidationError。
"""

SessionId = NewType("SessionId", str)
"""会话唯一标识符。

注意：与 Module/task_system/schema/types.py 的 SessionId 同名共存（Phase 1）。
两者是相同的 NewType，运行时完全兼容（都是 str）。Phase 2 统一迁移到此。
"""

MessageId = NewType("MessageId", str)
"""消息唯一标识符。"""

ToolCallId = NewType("ToolCallId", str)
"""工具调用唯一标识符。"""
