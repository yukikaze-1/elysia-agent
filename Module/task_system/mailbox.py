"""TaskMailbox — 单播异步事件队列。

TaskSystem → Agent 的实时事件流。消费者只有 Agent。
WebUI 通过查询 API + SSE 拉数据，不从 Mailbox 消费。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Module.task_system.schema import TaskEvent, TaskId

logger = logging.getLogger(__name__)


class TaskMailbox:
    """单播事件队列——TaskSystem 写入，Agent 消费。

    内部 asyncio.Queue(maxsize=100)。
    溢出策略：丢最旧的 progress，保留 approval_needed/completed/failed。

    生命周期：
        - submit()/resume() 返回即用，调用方直接 await mailbox.get()
        - 任务终态后 GC 回收，无 close()——调用方检测到 completed/failed 后停止消费
    """

    # TODO: 实现 _push() 溢出策略
    # TODO: 队列满时丢弃旧 progress 事件，保留关键事件

    def __init__(self, task_id: TaskId) -> None:
        self.task_id = task_id
        self._queue: asyncio.Queue[TaskEvent] = asyncio.Queue(maxsize=100)

    async def get(self, timeout: float | None = None) -> TaskEvent | None:
        """阻塞等待下一个事件。timeout 秒后返回 None。"""
        raise NotImplementedError("TaskMailbox.get() 尚未实现")

    def get_nowait(self) -> TaskEvent | None:
        """非阻塞获取，无事件时返回 None。"""
        raise NotImplementedError("TaskMailbox.get_nowait() 尚未实现")

    def count(self) -> int:
        """待消费事件数量。"""
        return self._queue.qsize()

    def _push(self, event: TaskEvent) -> None:
        """TaskSystem 内部写入事件。"""
        raise NotImplementedError("TaskMailbox._push() 尚未实现")
