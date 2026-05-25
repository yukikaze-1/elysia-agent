"""Priority-based event bus for the Agent system.

Priority 0: INTERRUPT — cancel everything, return to IDLE
Priority 1: STATE_CHANGE — FSM transitions for UI
Priority 2: USER_INPUT, SYSTEM_TICK — normal I/O
"""

import asyncio
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class EventPriority(IntEnum):
    INTERRUPT = 0
    STATE_CHANGE = 1
    NORMAL = 2


class EventType(str):
    INTERRUPT = "interrupt"
    STATE_CHANGE = "state_change"
    USER_INPUT = "user_input"
    SYSTEM_TICK = "system_tick"


@dataclass(order=True)
class Event:
    priority: int = field(compare=True)
    type: str = field(compare=False)
    data: Any = field(compare=False, default=None)
    seq: int = field(compare=False, default=0)


class EventBus:
    """Priority-based event bus backed by asyncio.PriorityQueue.

    Lower priority value = higher urgency. INTERRUPT(0) events jump the queue.
    """

    def __init__(self):
        self._queue: asyncio.PriorityQueue[Event] = asyncio.PriorityQueue()
        self._seq = 0

    def post(self, type: str, data: Any = None, priority: int = EventPriority.NORMAL) -> None:
        """Post an event to the bus. Non-blocking, safe to call from any task."""
        self._seq += 1
        event = Event(priority=priority, type=type, data=data, seq=self._seq)
        self._queue.put_nowait(event)

    async def next(self) -> Event:
        """Await the next event. Blocks until an event is available."""
        return await self._queue.get()

    def drain(self) -> list[Event]:
        """Drain all pending events from the queue. Non-blocking."""
        events: list[Event] = []
        while not self._queue.empty():
            events.append(self._queue.get_nowait())
        return events

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()
