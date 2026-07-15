"""
DEPRECATED: 本模块为旧版预留架构，当前未被 core/ 使用。
保留本模块仅作为未来架构演进参考。

core/ 中的等价或替代:
- core/schema/agent.py    — AgentState, EventType 等枚举
- core/memory/session.py   — SessionMemory
- core/memory/long_term.py — MemoryEntry
- Module/schema/message_format.py — TextPart, ImagePart, ChatMessageV2 等（当前主力）
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
    # io_models
    "L0InputSourceType",
    "WebClientMessage",
    "TelegramMessage",
    "ExternalInputEvent",
    "L0InternalQueueItem",
]
