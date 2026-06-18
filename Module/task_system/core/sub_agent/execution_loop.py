"""执行-决策主循环。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from Module.task_system.schema import (
    DelegationContext,
    ExecutionState,
    TaskEventType,
    TaskState,
    TaskStatus,
    now_iso,
)

if TYPE_CHECKING:
    from Module.task_system.core.graph import PlanGraph
    from Module.task_system.core.orchestrator import Orchestrator
    from Module.task_system.mailbox import TaskMailbox

logger = logging.getLogger(__name__)


async def execute_loop(
    orchestrator: Orchestrator,
    task_state: TaskState,
    graph: PlanGraph,
    mailbox: TaskMailbox,
    context: DelegationContext,
    resume_from: ExecutionState | None,
    dispatch_fn: Callable[..., Awaitable[tuple[bool, ExecutionState | None]]],
    push_mailbox_fn: Callable[..., Awaitable[None]],
) -> None:
    """执行-决策主循环。

    Orchestrator 内部自建 ExecutionState，SubAgent 不感知。
    大部分任务 Orchestrator 内部一次跑完，这个循环只跑一次。
    只在结构性失败（NEEDS_REPLAN）时循环多次。

    resume_from 在 Replan 后携带旧 ExecutionState，
    Orchestrator 合并后保留已成功节点的状态，避免重复执行。
    resume 时传入 pause checkpoint 的 ExecutionState。
    """
    while True:
        # 检查外部中断信号——拦截 Orchestrator.run() 入口。
        # _handle_replan 后 _merge_exec_state 会创建新 exec_state 丢失标志位，
        # 此处在每次循环顶检查旧 exec_state 兜底。
        if orchestrator._exec_state is not None:
            if getattr(orchestrator._exec_state,
                       "cancel_requested", False) is True:
                task_state.status = TaskStatus.CANCELLED
                task_state.updated_at = now_iso()
                await push_mailbox_fn(
                    mailbox, TaskEventType.CANCELLED, {
                        "task_id": str(task_state.task_id),
                        "reason": "用户取消",
                    },
                )
                return
            if getattr(orchestrator._exec_state,
                       "pause_requested", False) is True:
                task_state.status = TaskStatus.PAUSED
                task_state.updated_at = now_iso()
                await push_mailbox_fn(
                    mailbox, TaskEventType.PAUSED, {
                        "task_id": str(task_state.task_id),
                        "message": "任务已暂停",
                    },
                )
                return

        result = await orchestrator.run(
            task_state, graph, mailbox, resume_from=resume_from,
        )
        should_continue, resume_from = await dispatch_fn(
            result, task_state, graph, mailbox, context,
        )
        if not should_continue:
            return
