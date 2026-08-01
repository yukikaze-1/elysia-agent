"""规划 + 校验——Planner.plan() → PlanValidator.validate()。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from Module.workflow_engine.schema import (
    ConstraintConfig,
    DelegationContext,
    TaskEventType,
    TaskState,
    TaskStatus,
    ValidationAction,
    now_iso,
)

if TYPE_CHECKING:
    from Module.workflow_engine.core.planner import Planner
    from Module.workflow_engine.core.plan_validator import PlanValidator
    from Module.workflow_engine.core.graph import PlanGraph
    from Module.workflow_engine.mailbox import TaskMailbox

logger = logging.getLogger(__name__)


async def plan_and_validate(
    planner: Planner,
    plan_validator: PlanValidator,
    context: DelegationContext,
    task_state: TaskState,
    mailbox: TaskMailbox,
    constraints: ConstraintConfig,
    push_mailbox_fn: Callable[..., Awaitable[None]],
    abort_error_code_fn: Callable[..., str],
) -> PlanGraph | None:
    """规划 + 校验。

    Planner.plan() → PlanValidator.validate() → 构建 PlanGraph。
    校验通过 → 写入 task_state.plan，设 RUNNING，返回 PlanGraph。
    校验 ABORT → 设 FAILED，push 事件，返回 None。
    校验 FIX_PLAN → 带错误反馈重试规划（v1 最多 2 次）。

    Args:
        planner: Planner 实例，用于生成初始计划。
        plan_validator: PlanValidator 实例，用于校验计划并构建 PlanGraph。
        context: DelegationContext，包含任务意图、提示、可用工具等。
        task_state: TaskState，用于状态追踪和计划存储。
        mailbox: TaskMailbox，用于推送生命周期事件。
        constraints: ConstraintConfig，规划/校验的全局约束。
        push_mailbox_fn: 异步回调，向邮箱推送事件。
        abort_error_code_fn: 可调用对象，从校验问题列表生成错误码字符串。

    Returns:
        PlanGraph: 校验通过，调用方继续执行
        None:     校验失败且已处理，调用方应 return
    """
    task_id = task_state.task_id

    # 1. 初始规划
    task_state.status = TaskStatus.PLANNING
    task_state.updated_at = now_iso()
    logger.info("SubAgent: 开始规划 task_id=%s", task_id)
    plan = await planner.plan(
        context=context,
        global_constraints=constraints,
    )

    # 2. 校验（同时构建 PlanGraph，避免后续重复构造）
    validation, graph = plan_validator.validate(
        plan=plan,
        global_constraints=constraints,
        task_constraints=context.task_constraints,
    )

    if not validation.valid:
        if validation.action == ValidationAction.ABORT:
            issue_messages = [i.message for i in validation.issues]
            logger.error(
                "SubAgent: Plan 校验 abort task_id=%s: %s",
                task_id, issue_messages,
            )
            task_state.status = TaskStatus.FAILED
            task_state.updated_at = now_iso()
            await push_mailbox_fn(mailbox, TaskEventType.FAILED, {
                "task_id": str(task_id),
                "error": f"Plan 校验失败: {issue_messages}",
                "error_type": abort_error_code_fn(validation.issues),
            })
            return None

        # fix_plan: 带错误反馈重试规划（v1 最多 2 次）
        issue_msgs = [i.message for i in validation.issues]
        for retry_n in range(1, 3):
            logger.warning(
                "SubAgent: Plan 校验 fix_plan task_id=%s retry=%d: %s",
                task_id, retry_n, issue_msgs,
            )
            # 把校验错误追加到 hints，让 Planner 知道哪里不对
            retry_context = DelegationContext(
                intent=context.intent,
                hints=(context.hints or "") + f"\n[上轮 Plan 校验失败: {'; '.join(issue_msgs)}]",
                tools=context.tools,
                summarized_context=context.summarized_context,
                task_constraints=context.task_constraints,
                session_id=context.session_id,
                return_mode=context.return_mode,
            )
            plan = await planner.plan(
                context=retry_context,
                global_constraints=constraints,
            )
            validation, graph = plan_validator.validate(
                plan=plan,
                global_constraints=constraints,
                task_constraints=context.task_constraints,
            )
            if validation.valid:
                task_state.plan = graph.to_plan(plan)
                task_state.status = TaskStatus.RUNNING
                task_state.updated_at = now_iso()
                logger.info(
                    "SubAgent: Plan 校验 fix_plan 修复成功 task_id=%s retry=%d",
                    task_id, retry_n,
                )
                return graph

        # 所有重试耗尽
        logger.error(
            "SubAgent: Plan 校验 fix_plan 耗尽 task_id=%s: %s",
            task_id, issue_msgs,
        )
        task_state.status = TaskStatus.FAILED
        task_state.updated_at = now_iso()
        await push_mailbox_fn(mailbox, TaskEventType.FAILED, {
            "task_id": str(task_id),
            "error": f"Plan 校验失败且重试耗尽: {issue_msgs}",
            "error_type": abort_error_code_fn(validation.issues),
        })
        return None

    # 3. 存入 TaskState（graph 由 validate 返回，无需重复构造）
    task_state.plan = graph.to_plan(plan)
    task_state.status = TaskStatus.RUNNING
    task_state.updated_at = now_iso()

    return graph
