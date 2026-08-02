"""core/schema 基础类型 — 时间戳、标识符别名。"""

from typing import Annotated, NewType

from pydantic import BeforeValidator

from Module.workflow_contract.schema import SessionId

from core.schema.utils import _normalize_iso_ts


IsoTimestamp = Annotated[str, BeforeValidator(_normalize_iso_ts)]
"""ISO 8601 时间戳，构造时自动校验格式并归一化到 UTC。

使用 Pydantic BeforeValidator：空字符串和格式错误会抛出 ValidationError。
"""

# SessionId 由契约层（Module.workflow_contract.schema）拥有，core 侧重导出保持公开 API。
# 运行时即 str（NewType），与历史定义完全兼容。契约层不反向依赖 core。

MessageId = NewType("MessageId", str)
"""消息唯一标识符。"""

ToolCallId = NewType("ToolCallId", str)
"""工具调用唯一标识符。"""
