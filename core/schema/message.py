# core/schema/message.py
"""消息类型 — 按 role 拆分的 Pydantic BaseModel。

UserMessage / AssistantMessage / ToolResultMessage 各管各的字段。
role 字段 frozen，构造时自动填值。
"""

from enum import StrEnum
from typing import Union

from pydantic import BaseModel, Field

from Module.tool.protocol import ErrorType

from .part import Part
from .tool import ToolCall, ToolCallId
from .types import IsoTimestamp, now_iso, MessageId, SessionId


class MessageRole(StrEnum):
    """消息角色枚举。"""
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


class UserMessage(BaseModel):
    """用户消息。"""
    role: MessageRole = Field(default=MessageRole.USER, frozen=True)
    content: list[Part]
    message_id: MessageId
    timestamp: IsoTimestamp = Field(default_factory=now_iso)
    session_id: SessionId = SessionId("")
    metadata: dict = Field(default_factory=dict)


class AssistantMessage(BaseModel):
    """Assistant 回复消息。"""
    role: MessageRole = Field(default=MessageRole.ASSISTANT, frozen=True)
    content: list[Part]
    message_id: MessageId
    timestamp: IsoTimestamp = Field(default_factory=now_iso)
    tool_calls: list[ToolCall] | None = None
    block_index: dict[str, list[int]] | None = None  # {"markdown": [3,7], "code": [1]}
    inner_voice: str | None = None
    token_usage: dict | None = None
    metadata: dict = Field(default_factory=dict)


class ToolResultMessage(BaseModel):
    """工具执行结果消息。"""
    role: MessageRole = Field(default=MessageRole.TOOL, frozen=True)
    content: list[Part]
    tool_call_id: ToolCallId
    tool_name: str
    success: bool = True
    error: str | None = None
    error_type: ErrorType | None = None  # 复用 Module/tool/protocol.py 的 ErrorType
    duration_ms: float = 0
    truncated: bool = False
    message_id: MessageId = Field(default=MessageId(""))
    timestamp: IsoTimestamp = Field(default_factory=now_iso)


Message = Union[UserMessage, AssistantMessage, ToolResultMessage]
"""消息联合类型。"""
