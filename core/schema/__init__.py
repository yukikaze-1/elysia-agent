# core/schema/ — 系统基础类型定义层
#
# 子模块:
#   agent    — AgentState, EventType, EventPriority, VALID_TRANSITIONS
#   _utils   — now_iso（时间戳工厂）
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
from core.schema._utils import now_iso  # noqa: F401
from core.schema.types import (  # noqa: F401
    IsoTimestamp,
    SessionId,
    MessageId,
    ToolCallId,
)
from core.schema.part import (  # noqa: F401
    PartType,
    ImageMime,
    ImageDetail,
    FileMime,
    AudioMime,
    VideoMime,
    TextPart,
    ImagePart,
    FilePart,
    AudioPart,
    VideoPart,
    Part,
)
from core.schema.tool import ToolCall, ToolCallFunction  # noqa: F401
from core.schema.message import (  # noqa: F401
    ClientAssistantMessage,
    ClientMessageDict,
    ClientToolMessage,
    ClientUserMessage,
    Message,
    MessageRole,
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from core.schema.token import TokenUsageEntry, TokenUsageRecord, TokenUsageSummary  # noqa: F401
from core.schema.character import CharacterState, StateHistoryEntry  # noqa: F401
from core.schema.memory_reflector import (  # noqa: F401
    MemoryReflectorState,
    MergeItem,
    UpdateItem,
    DeleteItem,
    ReflectorActions,
)
from core.schema.mood import Mood, stt_emotion_emoji, stt_event_emoji  # noqa: F401
from core.schema.memory import (  # noqa: F401
    MemoryType,
    ExtractedMemory,
    SkipAction,
    InsertAction,
    UpdateAction,
    MergeAction,
    CompareAction,
)
from core.schema.event import (  # noqa: F401
    EventData,
    InterruptData,
    StateChangeData,
    UserInputData,
    SystemTickData,
    TaskResultData,
)
