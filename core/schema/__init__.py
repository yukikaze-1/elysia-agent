# core/schema/ — 系统基础类型定义层
#
# 子模块:
#   agent    — AgentState, EventType, EventPriority, VALID_TRANSITIONS
#   types    — IsoTimestamp, SessionId, MessageId, ToolCallId
#   part     — TextPart, ImagePart, ..., Part
#   tool     — ToolCall, ToolCallFunction
#   message  — UserMessage, AssistantMessage, ToolResultMessage, Message
#   adapters — content_to_openai, content_text, ...（按需从子模块显式 import）

from core.schema.agent import (  # noqa: F401
    AgentState,
    EventPriority,
    EventType,
    VALID_TRANSITIONS,
)
from core.schema.types import (  # noqa: F401
    IsoTimestamp,
    now_iso,
    SessionId,
    MessageId,
    ToolCallId,
)
from core.schema.part import (  # noqa: F401
    PartType,
    ImageMime,
    ImageDetail,
    TextPart,
    ImagePart,
    FilePart,
    AudioPart,
    VideoPart,
    Part,
)
from core.schema.tool import ToolCall, ToolCallFunction  # noqa: F401
from core.schema.message import (  # noqa: F401
    MessageRole,
    UserMessage,
    AssistantMessage,
    ToolResultMessage,
    Message,
)
