# core/schema/message.py — 消息类型
"""消息类型 — 按 role 拆分的 Pydantic BaseModel。

UserMessage / AssistantMessage / ToolResultMessage 各管各的字段。
role 字段 frozen，构造时自动填值。
"""

from enum import StrEnum
from typing import Literal, NotRequired, Sequence, TypedDict, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.schema.error_type import ErrorType

from core.schema.part import Part
from core.schema.token import TokenUsageSummary
from core.schema.tool import ToolCall, ToolCallId
from core.schema.utils import now_iso
from core.schema.types import IsoTimestamp, MessageId, SessionId


class MessageRole(StrEnum):
    """消息角色枚举。"""
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


class CharacterPresentationSnapshot(BaseModel):
    """assistant 历史审计所需的稳定展示引用（内部 snake_case）。"""

    model_config = ConfigDict(extra="forbid")
    avatar_asset_id: str | None = None
    live2d_model_id: str | None = None
    theme_color: str | None = None


class CharacterMessageSnapshot(BaseModel):
    """一次 assistant 生成实际使用的不可变角色身份快照。"""

    model_config = ConfigDict(extra="forbid")
    character_id: str = Field(min_length=1)
    character_instance_id: str = Field(min_length=1)
    character_revision: int = Field(ge=1)
    display_name: str = Field(min_length=1)
    presentation: CharacterPresentationSnapshot = Field(
        default_factory=CharacterPresentationSnapshot,
    )


class CharacterPresentationWire(TypedDict):
    avatarAssetId: str | None
    live2dModelId: str | None
    themeColor: str | None


class CharacterSnapshotWire(TypedDict):
    characterId: str
    characterInstanceId: str
    characterRevision: int
    displayName: str
    presentation: CharacterPresentationWire


def character_snapshot_to_wire(
    value: CharacterMessageSnapshot | dict | None,
) -> CharacterSnapshotWire | None:
    """把内部 assistant 快照显式翻译为 REST/WS camelCase wire。"""
    if value is None:
        return None
    snapshot = (
        value
        if isinstance(value, CharacterMessageSnapshot)
        else CharacterMessageSnapshot.model_validate(value)
    )
    presentation = snapshot.presentation
    return CharacterSnapshotWire(
        characterId=snapshot.character_id,
        characterInstanceId=snapshot.character_instance_id,
        characterRevision=snapshot.character_revision,
        displayName=snapshot.display_name,
        presentation=CharacterPresentationWire(
            avatarAssetId=presentation.avatar_asset_id,
            live2dModelId=presentation.live2d_model_id,
            themeColor=presentation.theme_color,
        ),
    )


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
    character: CharacterMessageSnapshot | None = None
    # 批次 3（multi-session）：可选归属会话 ID，空串 = 未归属（对齐 UserMessage）
    session_id: SessionId = SessionId("")
    metadata: dict = Field(default_factory=dict)


class ToolResultMessage(BaseModel):
    """工具执行结果消息。"""
    role: MessageRole = Field(default=MessageRole.TOOL, frozen=True)
    content: Sequence[Part]
    tool_call_id: ToolCallId
    tool_name: str
    success: bool = True
    error: str | None = None
    error_type: ErrorType | None = None  # core.schema.error_type.ErrorType
    duration_ms: float = 0
    truncated: bool = False
    message_id: MessageId
    timestamp: IsoTimestamp = Field(default_factory=now_iso)
    # 批次 3（multi-session）：可选归属会话 ID，空串 = 未归属（对齐 UserMessage）
    session_id: SessionId = SessionId("")
    metadata: dict = Field(default_factory=dict)

    @field_validator("message_id")
    @classmethod
    def _message_id_non_empty(cls, v: MessageId) -> MessageId:
        """归档消息 ID 非空不变量（spec D2）：空串拒绝，防坏记录写入 Mongo。"""
        if v is None or not str(v).strip():
            raise ValueError("ToolResultMessage.message_id 不能为空")
        return v


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
    character: CharacterSnapshotWire


class ClientToolMessage(TypedDict):
    """工具执行结果客户端格式。

    D1/D2：message_id/timestamp 必填——tool replay 输出消息自身身份，与 REST
    一致、前端据此去重/时间分隔。不继承 _ClientMessageBase（基类 parts:
    list[dict[str, str]] 与 content: list[dict] 与 tool 的 content: str 形状
    冲突，PEP 589 禁止不兼容覆盖）。
    """
    role: Literal[MessageRole.TOOL]
    message_id: str
    timestamp: str
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
