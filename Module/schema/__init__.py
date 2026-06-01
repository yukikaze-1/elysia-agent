"""
DEPRECATED: 本模块为旧版预留架构，当前未被 core/ 使用。
core/ 使用自己的内部数据结构。保留本模块仅作为未来架构演进参考。

core/ 中的等价或替代:
- core/schema.py          — AgentState, EventType 等枚举
- core/memory/session.py   — SessionMemory
- core/memory/long_term.py — MemoryEntry
"""

from Module.schema.constants import (
    DEFAULT_ERROR_PUBLIC_REPLY,
    DEFAULT_ERROR_INNER_THOUGHT,
    DEFAULT_ERROR_MOOD,
)
from Module.schema.event import (
    Event,
    EventType,
    EventSource,
    EventContentType,
)
from Module.schema.media import (
    AudioData,
    VideoData,
    ImageData,
    FileData,
)
from Module.schema.message import (
    MessageType,
    UserMessage,
    EnvironmentMessage,
    ChatMessage,
    ConversationSegment,
)
from Module.schema.io_models import (
    L0InputSourceType,
    WebClientMessage,
    TelegramMessage,
    ExternalInputEvent,
    L0InternalQueueItem,
)

__all__ = [
    # constants
    "DEFAULT_ERROR_PUBLIC_REPLY",
    "DEFAULT_ERROR_INNER_THOUGHT",
    "DEFAULT_ERROR_MOOD",
    # event
    "Event",
    "EventType",
    "EventSource",
    "EventContentType",
    # media
    "AudioData",
    "VideoData",
    "ImageData",
    "FileData",
    # message
    "MessageType",
    "UserMessage",
    "EnvironmentMessage",
    "ChatMessage",
    "ConversationSegment",
    # io_models
    "L0InputSourceType",
    "WebClientMessage",
    "TelegramMessage",
    "ExternalInputEvent",
    "L0InternalQueueItem",
]
