# core/schema/message.py
"""消息类型 — 按 role 拆分的 Pydantic BaseModel。

UserMessage / AssistantMessage / ToolResultMessage 各管各的字段。
role 字段 frozen，构造时自动填值。
"""

from enum import StrEnum
from typing import Literal, NotRequired, Sequence, TypedDict, Union

from pydantic import BaseModel, Field

from Module.tool.protocol import ErrorType

from .part import Part
from .token import TokenUsageSummary
from .tool import ToolCall, ToolCallId
from ._utils import now_iso
from .types import IsoTimestamp, MessageId, SessionId


class MessageRole(StrEnum):
    """消息角色枚举。"""
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


class UserMessage(BaseModel):
    """用户消息。"""
    role: MessageRole = Field(default=MessageRole.USER, frozen=True)
    content: Sequence[Part]
    message_id: MessageId
    timestamp: IsoTimestamp = Field(default_factory=now_iso)
    session_id: SessionId = SessionId("")
    metadata: dict = Field(default_factory=dict)


class AssistantMessage(BaseModel):
    """Assistant 回复消息。"""
    role: MessageRole = Field(default=MessageRole.ASSISTANT, frozen=True)
    content: Sequence[Part]
    message_id: MessageId
    timestamp: IsoTimestamp = Field(default_factory=now_iso)
    tool_calls: list[ToolCall] | None = None
    block_index: dict[str, list[int]] | None = None  # {"markdown": [3,7], "code": [1]}
    inner_voice: str | None = None
    token_usage: TokenUsageSummary | None = None
    metadata: dict = Field(default_factory=dict)


class ToolResultMessage(BaseModel):
    """工具执行结果消息。"""
    role: MessageRole = Field(default=MessageRole.TOOL, frozen=True)
    content: Sequence[Part]
    tool_call_id: ToolCallId
    tool_name: str
    success: bool = True
    error: str | None = None
    error_type: ErrorType | None = None  # 复用 Module/tool/protocol.py 的 ErrorType
    duration_ms: float = 0
    truncated: bool = False
    message_id: MessageId = Field(default=MessageId(""))
    timestamp: IsoTimestamp = Field(default_factory=now_iso)
    metadata: dict = Field(default_factory=dict)


Message = Union[UserMessage, AssistantMessage, ToolResultMessage]
"""消息联合类型。"""


# ---- 客户端展示格式（TypedDict）----
# messages_for_client() 使用，描述 Message 序列化后的 JSON 兼容 dict shape。
# 仅用于类型提示，不做运行时校验。


class _ClientMessageBase(TypedDict):
    """用户/Assistant 消息的客户端格式（公共字段）。"""
    role: str
    parts: list[dict[str, str]]
    content: list[dict]
    message_id: str
    timestamp: str


class ClientUserMessage(_ClientMessageBase):
    """用户消息客户端格式。（type checker 用——运行时与 ClientAssistantMessage 相同 shape）"""
    pass


class ClientAssistantMessage(_ClientMessageBase, total=False):
    """Assistant 消息客户端格式。inner_voice/tool_calls/token_usage/content_type 可选。"""
    inner_voice: str
    tool_calls: list[dict]
    token_usage: dict
    content_type: dict


class ClientToolMessage(TypedDict):
    """工具执行结果客户端格式。"""
    role: Literal["tool"]
    tool_call_id: str
    name: str
    content: str
    success: bool
    error: str | None
    error_type: str | None
    duration_ms: float
    truncated: bool


ClientMessageDict = ClientUserMessage | ClientAssistantMessage | ClientToolMessage
"""客户端消息联合类型。"""
