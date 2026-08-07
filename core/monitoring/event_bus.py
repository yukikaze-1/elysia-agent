"""Agent 系统的优先级事件总线。

Priority 0: INTERRUPT — 取消一切，回到 IDLE
Priority 1: STATE_CHANGE — FSM 转换（供 UI）
Priority 2: USER_INPUT, SYSTEM_TICK — 常规 I/O
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from core.schema import EventPriority, EventType  # noqa: F401
from core.schema.event import EventData  # noqa: F401


@dataclass(order=True)
class Event:
    priority: int = field(compare=True)
    type: str = field(compare=False)
    data: EventData | None = field(compare=False, default=None)
    seq: int = field(compare=True, default=0)


class EventBus:
    """基于 asyncio.PriorityQueue 的优先级事件总线。

    优先级值越小越紧急。INTERRUPT(0) 事件插队。
    """

    def __init__(self):
        """初始化事件总线，创建空优先队列。"""
        self._queue: asyncio.PriorityQueue[Event] = asyncio.PriorityQueue()
        self._seq = 0

    def post(
        self, data: EventData, priority: int = EventPriority.NORMAL,
        *, type: str | None = None,
    ) -> None:
        """投递一个带类型的事件到队列中。

        Args:
            data: 事件数据，必须包含 event_type 字段。
            priority: 事件优先级，值越小越紧急。默认 NORMAL。
            type: 事件类型字符串。默认从 data.event_type 推导；显式传 type 覆盖。
        """
        self._seq += 1
        event_type = type if type is not None else data.event_type  # noqa: SIM108
        event = Event(priority=priority, type=event_type, data=data, seq=self._seq)
        self._queue.put_nowait(event)

    async def next(self) -> Event:
        """等待下一个事件。阻塞直到有事件可用。"""
        return await self._queue.get()

    def drain(self) -> list[Event]:
        """排空队列中所有待处理事件。非阻塞。

        可与 next() 并发调用——若其他消费者在 empty() 与 get_nowait() 之间
        抢走事件，捕获 QueueEmpty。
        """
        events: list[Event] = []
        while not self._queue.empty():
            try:
                events.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return events

    @property
    def pending_count(self) -> int:
        """队列中待处理的事件数量。

        Returns:
            当前优先队列中的事件数量。
        """
        return self._queue.qsize()
