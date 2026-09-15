"""WorkflowMailbox — 单播异步事件队列。

WorkflowEngine → Agent 的实时事件流。消费者只有 Agent。
WebUI 通过查询 API + SSE 拉数据，不从 Mailbox 消费。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Callable

from Module.workflow_contract import (
    WorkflowEvent,
    WorkflowEventType,
    WorkflowId,
    WorkflowStatus,
)

logger = logging.getLogger(__name__)

# 有界队列上限。溢出时丢弃最旧的 progress 事件，保留关键事件（approval_needed/
# completed/failed/cancelled/paused 等终态事件）。
_MAXSIZE = 100

# 终态事件——任务进入终态后只允许推一次（E3 §3.5 终态单调性）。
# QUEUED/PROGRESS/PAUSED 等非终态不入（暂停后任务仍可继续）。
_TERMINAL_EVENTS = {
    WorkflowEventType.COMPLETED,
    WorkflowEventType.FAILED,
    WorkflowEventType.CANCELLED,
}

# 终态事件 → 对应 WorkflowStatus 映射（供 _terminal_status 记录首个终态）。
_TERMINAL_STATUS_MAP: dict[WorkflowEventType, WorkflowStatus] = {
    WorkflowEventType.COMPLETED: WorkflowStatus.COMPLETED,
    WorkflowEventType.FAILED: WorkflowStatus.FAILED,
    WorkflowEventType.CANCELLED: WorkflowStatus.CANCELLED,
}


class WorkflowMailbox:
    """单播事件队列——WorkflowEngine 写入，Agent 消费。

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

    def __init__(self, task_id: WorkflowId) -> None:
        """为单个任务初始化 mailbox。

        Args:
            task_id: 该 mailbox 所属的任务。
        """
        self.task_id: WorkflowId = task_id
        self._deque: deque[WorkflowEvent] = deque()
        self._event = asyncio.Event()
        # E3 §3.5 终态唯一性守卫——记录首个终态事件，后续终态静默丢弃。
        self._terminal_pushed: bool = False
        self._terminal_status: WorkflowStatus | None = None
        # #6 硬杀窗口：终态落盘回调（core 经 AgentInfra → submit() 注入）。
        # None = 不落盘（单测 / 未接线）。签名 (task_id, event, data) → None，
        # **必须同步**——它的全部价值就在于"push_mailbox_event 返回时记录已在盘上"。
        self.terminal_sink: Callable[[str, str, dict], None] | None = None

    # ── 消费者 API ─────────────────────────────────────────────────────────

    async def get(self, timeout: float | None = None) -> WorkflowEvent | None:
        """阻塞等待下一个事件。

        Args:
            timeout: 超时秒数，None 表示无限等待。

        Returns:
            WorkflowEvent | None: 下一个事件，超时返回 None。
        """
        self._event.clear()  # 先清再检查——避免"if 后 clear 前"的视觉竞态窗口
        if not self._deque:
            try:
                await asyncio.wait_for(self._event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                return None
        return self._deque.popleft()

    def get_nowait(self) -> WorkflowEvent | None:
        """非阻塞获取。

        Returns:
            WorkflowEvent | None: 下一个事件，无事件返回 None。
        """
        return self._deque.popleft() if self._deque else None

    def count(self) -> int:
        """待消费事件数量。

        Returns:
            int: 队列中待消费的事件数。
        """
        return len(self._deque)

    # ── 内部 ────────────────────────────────────────────────────────────────

    def _evict_one(self, new_type: WorkflowEventType) -> None:
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
            if item.type == WorkflowEventType.PROGRESS:
                del self._deque[i]
                logger.debug(
                    "WorkflowMailbox: task_id=%s 溢出，丢弃旧 progress 事件",
                    self.task_id,
                )
                return

        # 队列里全是关键事件，没有 progress 可牺牲
        if new_type == WorkflowEventType.PROGRESS:
            # 新事件也是 progress，丢弃新事件（不放入）
            # 通过抛出特殊异常让 _push 跳过 append
            raise _EvictNewEvent
        else:
            # 新事件是关键事件，丢弃最旧事件
            old = self._deque.popleft()
            logger.warning(
                "WorkflowMailbox: task_id=%s 队列满且全是关键事件，"
                "丢弃最旧事件 type=%s，为新事件 type=%s 腾位",
                self.task_id, old.type.value, new_type.value,
            )

    def _push(self, event: WorkflowEvent) -> None:  # noqa: F811
        """写入事件——WorkflowEngine 内部调用。

        无线程安全问题——WorkflowEngine 全部 asyncio 协程，单生产者单消费者，
        _push 和 get_nowait 都是同步方法（无 await），不存在竞态。
        CPU 密集工具走按需子进程，通过 JSON stdin/stdout 通信，不直接 push。

        Args:
            event: 要写入的 WorkflowEvent。
        """
        if len(self._deque) >= _MAXSIZE:
            try:
                self._evict_one(event.type)
            except _EvictNewEvent:
                logger.debug(
                    "WorkflowMailbox: task_id=%s 队列满且全是关键事件，"
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
    mailbox: WorkflowMailbox,
    event_type: WorkflowEventType,
    data: dict,
    *,
    caller: str = "",
) -> None:
    """构造 WorkflowEvent 并写入 Mailbox 队列——Orchestrator / SubAgent 共用。

    生产者（单协程，无竞态）。消费者是 CognitiveLoop 的 Agent。

    #6：**终态事件**额外经 `mailbox.terminal_sink` 同步落盘（core 注入，未接线则
    不落盘）——把"持久化点"从 Agent 的 mailbox 轮询（0.5s 周期）前移到终态产生的
    这一刻，消掉"轮询前被硬杀即永久丢失"的窗口。详见 `core/storage/task_result_wal.py`。

    Args:
        mailbox: 目标任务的邮箱。
        event_type: 事件类型。
        data: 事件数据 dict。
        caller: 调用方标识（用于日志，如 "Orchestrator" / "SubAgent"）。
    """
    from Module.workflow_contract import WorkflowEvent

    # E3 §3.5 终态唯一性守卫——已推过终态事件则静默丢弃，
    # 防止 orchestrator FAILED + engine 兜底 FAILED + CancelledError 分支多路径重复推。
    # 用 `is True` 身份判断而非真值——mock mailbox（MagicMock）的 _terminal_pushed
    # 属性访问会返回真值 MagicMock，真值判断会把首个终态误判为已推送而丢弃。
    if event_type in _TERMINAL_EVENTS:
        if mailbox._terminal_pushed is True:
            logger.debug(
                "%s: task_id=%s 已推过终态事件（%s），丢弃重复终态 %s",
                caller or "push_mailbox_event",
                mailbox.task_id,
                mailbox._terminal_status.value if mailbox._terminal_status else "?",
                event_type.value,
            )
            return
        mailbox._terminal_pushed = True
        mailbox._terminal_status = _TERMINAL_STATUS_MAP[event_type]
        # #6 硬杀窗口：终态产生的**这一刻**同步落盘（core 注入的 sink）。
        # 位置在唯一性守卫之后——被守卫丢弃的重复终态不该进 WAL。
        # 位置在 `_push` 之前：`_push` 若抛（理论上 queue 操作不会，但轮询/消费是
        # 另一条路径），事件永远不会被消费，而 WAL 里已有记录，启动 replay 仍能补投。
        _wal_terminal(mailbox, event_type, data)

    event = WorkflowEvent(type=event_type, data=data)
    mailbox._push(event)
    logger.debug(
        "%s: 推送 Mailbox type=%s data_keys=%s",
        caller or "push_mailbox_event",
        event_type.value,
        list(data.keys()),
    )


def _wal_terminal(
    mailbox: WorkflowMailbox,
    event_type: WorkflowEventType,
    data: dict,
) -> None:
    """把终态事件同步写进 core 注入的 WAL sink（未接线 → no-op）。

    `getattr` 兜底是**契约层不依赖 core** 的代价：本函数在 `Module/` 里，不能 import
    core 的 WAL。sink 由 core 在 `AgentInfra.terminal_result_sink` 提供，engine 在
    `submit()` 时挂到 mailbox 上（一个任务一个，随 mailbox 生命周期结束）。

    `callable()` 只挡"未接线（None）/ 接了非可调用物"两种情况。**MagicMock mailbox
    挡不住**——它的属性本身就是可调用的 mock，于是 mock sink 被调用一次（无副作用，
    只多一条调用记录）。这里不做 `is True` 式的身份判断，因为真实 sink 是 bound
    method，身份判断会把真接线也挡掉。sink 自己吞写失败（一条磁盘问题不能把任务终态
    推送弄挂），这里再包一层是防"接线方违约抛出"——契约层不假设消费方一定守约。
    """
    sink = getattr(mailbox, "terminal_sink", None)
    if not callable(sink):
        return
    try:
        sink(str(mailbox.task_id), event_type.value, dict(data))
    except Exception:
        logger.warning(
            "终态 WAL sink 抛出（跨硬杀保障降级，任务终态不受影响）task_id=%s",
            mailbox.task_id, exc_info=True,
        )
