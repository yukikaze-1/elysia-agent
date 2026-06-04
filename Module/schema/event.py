from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from datetime import datetime
import time

from core.message_id import new_message_id


class EventType(str, Enum):
    USER_INPUT = "user_input"
    SYSTEM_TICK = "system_tick"
    MICRO_REFLECTION_DONE = "micro_reflection_done"
    MACRO_REFLECTION_DONE = "macro_reflection_done"
    REFLECTION_DONE = "reflection_done"


class EventSource(str, Enum):
    L0 = "L0"
    L0_SENSOR = "L0_SENSOR"
    L0_CLOCK = "L0_CLOCK"
    L1 = "L1"
    REFLECTOR = "REFLECTOR"
    SYSTEM = "SYSTEM"
    WEB_CLIENT = "WEB_CLIENT"
    ACTIVE_SENSOR = "ACTIVE_SENSOR"
    SYSTEM_CLOCK = "SYSTEM_CLOCK"


class EventContentType(str, Enum):
    USERMESSAGE = "UserMessage"
    TEXT = "Text"
    STRUCTURED_DATA = "StructuredData"
    TIME = "Time"


@dataclass
class Event:
    type: EventType
    content_type: EventContentType
    content: Any
    source: EventSource
    timestamp: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: new_message_id())
    metadata: dict[str, Any] | None = None

    def __str__(self):
        ts = datetime.fromtimestamp(self.timestamp).strftime('%H:%M:%S')
        return f"[{ts}] {self.type} from {self.source}: {str(self.content)[:50]}..."
