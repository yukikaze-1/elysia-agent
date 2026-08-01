"""TaskMailbox — 单播异步事件队列。

TaskSystem → Agent 的实时事件流。消费者只有 Agent。
WebUI 通过查询 API + SSE 拉数据，不从 Mailbox 消费。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque

from Module.workflow_engine.schema import TaskEvent, TaskEventType, TaskId

logger = logging.getLogger(__name__)

# 有界队列上限。溢出时丢弃最旧的 progress 事件，保留关键事件（approval_needed/
# completed/failed/cancelled/paused）。
_MAXSIZE = 100


class TaskMailbox:
    """单播事件队列——TaskSystem 写入，Agent 消费。

    内部用 deque + asyncio.Event 实现 SPSC 队列。
    不用 asyncio.Queue——因为溢出时需要精准删除特定类型事件（最旧的 progress），
    asyncio.Queue 不支持从中间移除元素。

    溢出策略：
        队列满时找最旧的 progress 删除。progress 丢失可接受（进度快照，后续会覆盖）。
        若无 progress 可删且新事件非 progress，删最旧事件腾位。
        若无 progress 可删且新事件也是 progress，静默丢弃。

    生命周期：
        - submit()/resume() 返回即用，调用方直接 await mailbox.get()
        - 任务终态后 GC 回收，无 close()——调用方检测到终态事件后停止消费
    """

    def __init__(self, task_id: TaskId) -> None:
        """Initialize a mailbox for a single task.

        Args:
            task_id: The task this mailbox belongs to.
        """
        self.task_id: TaskId = task_id
        self._deque: deque[TaskEvent] = deque()
        self._event = asyncio.Event()

    # ── 消费者 API ─────────────────────────────────────────────────────────

    async def get(self, timeout: float | None = None) -> TaskEvent | None:
        """阻塞等待下一个事件。

        Args:
            timeout: 超时秒数，None 表示无限等待。

        Returns:
            TaskEvent | None: 下一个事件，超时返回 None。
        """
        self._event.clear()  # 先清再检查——避免"if 后 clear 前"的视觉竞态窗口
        if not self._deque:
            try:
                await asyncio.wait_for(self._event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                return None
        return self._deque.popleft()

    def get_nowait(self) -> TaskEvent | None:
        """非阻塞获取。

        Returns:
            TaskEvent | None: 下一个事件，无事件返回 None。
        """
        return self._deque.popleft() if self._deque else None

    def count(self) -> int:
        """待消费事件数量。

        Returns:
            int: 队列中待消费的事件数。
        """
        return len(self._deque)

    # ── 内部 ────────────────────────────────────────────────────────────────

    def _evict_one(self, new_type: TaskEventType) -> None:
        """队列满时腾出一个位置。

        优先删除最旧的 progress。若没有 progress 可删：
        - 新事件非 progress → 删最旧事件（关键事件不能丢）
        - 新事件是 progress → 不放入，静默丢弃（进度丢失可接受）

        Args:
            new_type: 待写入的新事件类型。

        Raises:
            _EvictNewEvent: 新事件被丢弃时抛出内部信号。
        """
        for i, item in enumerate(self._deque):
            if item.type == TaskEventType.PROGRESS:
                del self._deque[i]
                logger.debug(
                    "TaskMailbox: task_id=%s 溢出，丢弃旧 progress 事件",
                    self.task_id,
                )
                return

        # 队列里全是关键事件，没有 progress 可牺牲
        if new_type == TaskEventType.PROGRESS:
            # 新事件也是 progress，丢弃新事件（不放入）
            # 通过抛出特殊异常让 _push 跳过 append
            raise _EvictNewEvent
        else:
            # 新事件是关键事件，丢弃最旧事件
            old = self._deque.popleft()
            logger.warning(
                "TaskMailbox: task_id=%s 队列满且全是关键事件，"
                "丢弃最旧事件 type=%s，为新事件 type=%s 腾位",
                self.task_id, old.type.value, new_type.value,
            )

    def _push(self, event: TaskEvent) -> None:  # noqa: F811
        """写入事件——TaskSystem 内部调用。

        无线程安全问题——TaskSystem 全部 asyncio 协程，单生产者单消费者，
        _push 和 get_nowait 都是同步方法（无 await），不存在竞态。
        CPU 密集工具走按需子进程，通过 JSON stdin/stdout 通信，不直接 push。

        Args:
            event: 要写入的 TaskEvent。
        """
        if len(self._deque) >= _MAXSIZE:
            try:
                self._evict_one(event.type)
            except _EvictNewEvent:
                logger.debug(
                    "TaskMailbox: task_id=%s 队列满且全是关键事件，"
                    "丢弃新 progress 事件",
                    self.task_id,
                )
                return
        self._deque.append(event)
        self._event.set()


class _EvictNewEvent(Exception):
    """内部信号：溢出时新事件被丢弃（而非旧事件被置换）。"""
    pass


def push_mailbox_event(
    mailbox: TaskMailbox,
    event_type: TaskEventType,
    data: dict,
    *,
    caller: str = "",
) -> None:
    """构造 TaskEvent 并写入 Mailbox 队列——Orchestrator / SubAgent 共用。

    生产者（单协程，无竞态）。消费者是 CognitiveLoop 的 Agent。

    Args:
        mailbox: 目标任务的邮箱。
        event_type: 事件类型。
        data: 事件数据 dict。
        caller: 调用方标识（用于日志，如 "Orchestrator" / "SubAgent"）。
    """
    from Module.workflow_engine.schema import TaskEvent

    event = TaskEvent(type=event_type, data=data)
    mailbox._push(event)
    logger.debug(
        "%s: 推送 Mailbox type=%s data_keys=%s",
        caller or "push_mailbox_event",
        event_type.value,
        list(data.keys()),
    )
