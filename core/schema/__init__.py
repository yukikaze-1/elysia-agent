"""core/schema/ — 系统基础类型定义层。

按子领域分组导出：
  - 基础类型:  SessionId, MessageId, ToolCallId, IsoTimestamp, now_iso
  - 消息:      Message, UserMessage, AssistantMessage, ToolResultMessage, Client* 等
  - Part:      TextPart, ImagePart, FilePart, AudioPart, VideoPart, Part + 枚举
  - 工具:      ToolCall, ToolCallFunction
  - 事件/状态: EventData, StateChangeData, UserInputData 等 + AgentState / EventPriority
  - 记忆:      MemoryType, ExtractedMemory, CompareAction 等 + ReflectorActions
  - 角色:      CharacterState, StateHistoryEntry, CompressionTrigger, VersionEntry
  - Token:     TokenUsageEntry, TokenUsageRecord, TokenUsageSummary
  - 情绪:      stt_emotion_emoji, stt_event_emoji
"""

# ── 基础类型（ID、时间戳、工具函数） ──
from core.schema._utils import now_iso  # noqa: F401
from core.schema.types import (  # noqa: F401
    IsoTimestamp,
    MessageId,
    SessionId,
    ToolCallId,
)

# ── 消息 ──
from core.schema.message import (  # noqa: F401
    AssistantMessage,
    ClientAssistantMessage,
    ClientMessageDict,
    ClientToolMessage,
    ClientUserMessage,
    Message,
    MessageRole,
    ToolResultMessage,
    UserMessage,
)

# ── Part / 多模态 ──
from core.schema.part import (  # noqa: F401
    AudioMime,
    AudioPart,
    FileMime,
    FilePart,
    ImageDetail,
    ImageMime,
    ImagePart,
    Part,
    PartType,
    TextPart,
    VideoMime,
    VideoPart,
)

# ── 工具 ──
from core.schema.tool import ToolCall, ToolCallFunction  # noqa: F401

# ── 事件 + Agent 状态机 ──
from core.schema.agent import (  # noqa: F401
    AgentState,
    EventPriority,
    EventType,
    VALID_TRANSITIONS,
)
from core.schema.event import (  # noqa: F401
    EventData,
    InterruptData,
    StateChangeData,
    SystemTickData,
    TaskResultData,
    UserInputData,
)

# ── 记忆（提取 + 去重） ──
from core.schema.memory import (  # noqa: F401
    CompareAction,
    ExtractedMemory,
    InsertAction,
    MemoryType,
    MergeAction,
    SkipAction,
    UpdateAction,
)
from core.schema.memory_reflector import (  # noqa: F401
    DeleteItem,
    MemoryReflectorState,
    MergeItem,
    ReflectorActions,
    UpdateItem,
)

# ── 角色 ──
from core.schema.character import CharacterState, StateHistoryEntry  # noqa: F401
from core.schema.compression import CompressionTrigger, VersionEntry  # noqa: F401

# ── Token ──
from core.schema.token import TokenUsageEntry, TokenUsageRecord, TokenUsageSummary  # noqa: F401

# ── 情绪 ──
from core.schema.mood import stt_emotion_emoji, stt_event_emoji  # noqa: F401
