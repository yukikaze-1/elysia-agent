"""
DEPRECATED: 本模块为旧版预留架构，当前未被 core/ 使用。
保留本模块仅作为未来架构演进参考。

core/ 中的等价或替代:
- core/schema/agent.py    — AgentState, EventType 等枚举
- core/schema/part.py     — TextPart, ImagePart 等 Part 类型
- core/schema/message.py  — UserMessage, AssistantMessage, ToolResultMessage
- core/memory/session.py  — SessionMemory
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
]
