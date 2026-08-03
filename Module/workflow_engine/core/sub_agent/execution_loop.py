"""执行-决策主循环。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from Module.workflow_contract import (
    DelegationContext,
    WorkflowEventType,
    WorkflowStatus,
)
from Module.workflow_engine.schema import (
    ExecutionState,
    WorkflowErrorCode,
    WorkflowState,
    now_iso,
)

if TYPE_CHECKING:
    from Module.workflow_engine.core.graph import PlanGraph
    from Module.workflow_engine.core.orchestrator import Orchestrator
    from Module.workflow_engine.mailbox import WorkflowMailbox

logger = logging.getLogger(__name__)

# 执行循环防御性上限——正常任务循环次数极少（每次 NEEDS_REPLAN 才多一轮，
# 熔断在 replan.py 独立限制 ≤5 次）。达到此值说明 dispatch_fn 死循环 bug。
_MAX_EXECUTION_LOOP_ITERATIONS = 1000


async def execute_loop(
    orchestrator: Orchestrator,
    task_state: WorkflowState,
    graph: PlanGraph,
    mailbox: WorkflowMailbox,
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

    Args:
        orchestrator: Orchestrator 实例，管理执行状态并驱动执行。
        task_state: 任务状态对象，用于状态追踪和更新。
        graph: 计划图（PlanGraph），包含执行 DAG。
        mailbox: 任务邮箱，用于推送事件。
        context: 委托上下文，包含任务意图、提示、可用工具等。
        resume_from: 恢复执行的旧 ExecutionState（Replan/暂停后），\n            全新执行时为 None。
        dispatch_fn: 异步回调，处理 Orchestrator.run() 结果，\n            返回 (should_continue, resume_from)。
        push_mailbox_fn: 异步回调，向邮箱推送生命周期事件。

    Returns:
        None: 函数通过修改 task_state 和推送邮箱事件产生副作用。
    """
    iterations = 0
    while True:
        iterations += 1
        if iterations > _MAX_EXECUTION_LOOP_ITERATIONS:
            # 防御性上限——终止依赖 check_meltdown 约定不可靠，
            # dispatch_fn 死循环时必须有兜底（O 提级项）。
            logger.critical(
                "execute_loop: 迭代次数超上限 %d，任务强制失败（防死循环）",
                _MAX_EXECUTION_LOOP_ITERATIONS,
            )
            task_state.status = WorkflowStatus.FAILED
            task_state.updated_at = now_iso()
            await push_mailbox_fn(
                mailbox, WorkflowEventType.FAILED, {
                    "task_id": str(task_state.task_id),
                    "error": f"执行循环超过 {_MAX_EXECUTION_LOOP_ITERATIONS} 次迭代（内部错误）",
                    "error_type": WorkflowErrorCode.INTERNAL_ERROR.value,
                },
            )
            return
        # 检查外部中断信号——拦截 Orchestrator.run() 入口。
        # _handle_replan 后 _merge_exec_state 会创建新 exec_state 丢失标志位，
        # 此处在每次循环顶检查旧 exec_state 兜底。
        if orchestrator._exec_state is not None:
            if getattr(orchestrator._exec_state,
                       "cancel_requested", False) is True:
                task_state.status = WorkflowStatus.CANCELLED
                task_state.updated_at = now_iso()
                await push_mailbox_fn(
                    mailbox, WorkflowEventType.CANCELLED, {
                        "task_id": str(task_state.task_id),
                        "reason": "用户取消",
                    },
                )
                return
            if getattr(orchestrator._exec_state,
                       "pause_requested", False) is True:
                task_state.status = WorkflowStatus.PAUSED
                task_state.updated_at = now_iso()
                await push_mailbox_fn(
                    mailbox, WorkflowEventType.PAUSED, {
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
