"""Agent 系统的优先级事件总线。

Priority 0: INTERRUPT — 取消一切，回到 IDLE
Priority 1: STATE_CHANGE — FSM 转换（供 UI）
Priority 2: USER_INPUT, SYSTEM_TICK — 常规 I/O
"""

import asyncio
from dataclasses import dataclass, field

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
        # 待派发 USER_INPUT 事件计数（review 复核 #4）：post 增、next/drain 消费减。
        # process_message 在「FSM 仍 accepting 但 bus 已有未派发 USER_INPUT」时据此
        # 走排队路径并在接受时做容量检查——杜绝同 tick 突发「先 ACK 后丢弃」。
        self._pending_user_inputs = 0
        # 待派发 USER_INPUT 的 admission 见证（`UserInputData.request_id`）快照，
        # 与上面的计数**同源同步**维护（post 追加、离开队列时移除）。
        # Batch 13（KB-38）：顺序仲裁必须能看见"还在总线里等派发"的更早输入——
        # 只看 `Agent._pending_messages` 会在"队首已 pop(0)、尚未被 claim"的窗口里
        # 失明，而那个窗口里更晚的输入会拿到更小的 `seq` 从而先被派发（Challenger A
        # 反证：更晚 admitted 的输入越过更早的）。见 `pending_user_input_request_ids`。
        self._pending_user_input_ids: list[str] = []

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
        if event_type == EventType.USER_INPUT:
            self._pending_user_inputs += 1
            self._pending_user_input_ids.append(
                str(getattr(data, "request_id", "") or ""))
        self._queue.put_nowait(event)

    def _note_removed(self, event: Event) -> None:
        """事件离开队列时修正 USER_INPUT 计数（消费/中断 drain 共用）。"""
        if event.type == EventType.USER_INPUT:
            self._pending_user_inputs = max(0, self._pending_user_inputs - 1)
            rid = str(getattr(event.data, "request_id", "") or "")
            try:
                self._pending_user_input_ids.remove(rid)
            except ValueError:      # pragma: no cover — 与计数同源，理论上不可达
                pass

    @property
    def pending_user_inputs(self) -> int:
        """队列中待派发的 USER_INPUT 事件数。

        Returns:
            当前优先队列中未派发的 USER_INPUT 事件数量。
        """
        return self._pending_user_inputs

    def pending_user_input_request_ids(self) -> tuple[str, ...]:
        """队列中待派发 USER_INPUT 的 admission 见证快照（只读；顺序 = 出队顺序）。

        Batch 13（KB-38）：`Agent` 的越队判据据此把"还在总线里等派发"的更早输入一并
        算进**等待集**（`_pending_messages` ∪ 总线驻留）。缺了它，判据会在
        "pending 队首已 `pop(0)` 交给总线、但还没被 claim"的窗口里失明——那正是
        Challenger A 反证出的反序窗口。只读：不改变优先级/顺序，也不参与派发。

        Returns:
            未派发 USER_INPUT 的 `request_id` 元组（读不到时为空串），按入队顺序。
        """
        return tuple(self._pending_user_input_ids)

    async def next(self) -> Event:
        """等待下一个事件。阻塞直到有事件可用。"""
        event = await self._queue.get()
        self._note_removed(event)
        return event

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
        for ev in events:
            self._note_removed(ev)
        return events

    @property
    def pending_count(self) -> int:
        """队列中待处理的事件数量。

        Returns:
            当前优先队列中的事件数量。
        """
        return self._queue.qsize()
