"""规划 + 校验——Planner.plan() → PlanValidator.validate()。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from Module.workflow_contract import (
    ConstraintConfig,
    DelegationContext,
    WorkflowEventType,
    WorkflowStatus,
)
from Module.workflow_engine.cancel import check_cancelled
from Module.workflow_engine.schema import (
    WorkflowErrorCode,
    WorkflowState,
    ValidationAction,
    now_iso,
)

if TYPE_CHECKING:
    from Module.workflow_engine.core.planner import Planner
    from Module.workflow_engine.core.plan_validator import PlanValidator
    from Module.workflow_engine.core.graph import PlanGraph
    from Module.workflow_engine.mailbox import WorkflowMailbox

logger = logging.getLogger(__name__)


_PLAN_ATTEMPTS = 2


def _coerce_code(raw: str) -> "WorkflowErrorCode | None":
    """字符串 → WorkflowErrorCode；未知值返回 None（保留既有 error_code，不硬编）。"""
    try:
        return WorkflowErrorCode(raw)
    except ValueError:
        return None


async def _plan_with_retry(
    planner: "Planner",
    context: DelegationContext,
    constraints: ConstraintConfig,
    task_state: WorkflowState,
    task_id: Any,
) -> Any:
    """调 Planner，捕获**解析与校验两类异常**并重试（S2-D6）。

    容错不对称是这条缺陷的实质：Evaluator 侧有重试，Planner 侧没有——一次 LLM 抖动
    （返回截断 JSON、空 content、或 `DAGNode` 字段非法）就判死整个任务。

    Args:
        planner: Planner 实例。
        context: 委托上下文。
        constraints: 全局约束。
        task_state: 任务状态（重试耗尽时写失败原因）。
        task_id: 任务 ID（日志用）。

    Returns:
        成功时返回 Plan；重试耗尽返回 None（调用方直接 return，不再继续）。
    """
    from pydantic import ValidationError

    from Module.workflow_engine.core.planner.parser import PlanParseError

    last_error: str = ""
    for attempt in range(1, _PLAN_ATTEMPTS + 1):
        try:
            return await planner.plan(context=context, global_constraints=constraints)
        except (PlanParseError, ValidationError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "SubAgent: 规划失败 task_id=%s attempt=%d/%d: %s",
                task_id, attempt, _PLAN_ATTEMPTS, last_error,
            )
        except Exception as exc:                       # noqa: BLE001
            # 非解析类异常（网络/超时）同样不应让整个任务以"内部错误"收场——
            # 但错误码要如实：归为 PLANNING_FAILED 并保留原始信息。
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "SubAgent: 规划异常 task_id=%s attempt=%d/%d: %s",
                task_id, attempt, _PLAN_ATTEMPTS, last_error, exc_info=True,
            )

    task_state.fail(
        f"规划失败（{_PLAN_ATTEMPTS} 次尝试均未产出可用 Plan）: {last_error}",
        WorkflowErrorCode.PLANNING_FAILED,
    )
    return None


async def plan_and_validate(
    planner: Planner,
    plan_validator: PlanValidator,
    context: DelegationContext,
    task_state: WorkflowState,
    mailbox: WorkflowMailbox,
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
        task_state: WorkflowState，用于状态追踪和计划存储。
        mailbox: WorkflowMailbox，用于推送生命周期事件。
        constraints: ConstraintConfig，规划/校验的全局约束。
        push_mailbox_fn: 异步回调，向邮箱推送事件。
        abort_error_code_fn: 可调用对象，从校验问题列表生成错误码字符串。

    Returns:
        PlanGraph: 校验通过，调用方继续执行
        None:     校验失败且已处理，调用方应 return
    """
    task_id = task_state.task_id

    # 1. 初始规划
    task_state.status = WorkflowStatus.PLANNING
    task_state.updated_at = now_iso()
    logger.info("SubAgent: 开始规划 task_id=%s", task_id)
    # S2-D6：规划异常原先**没有 try/except**——`PlanParseError`（LLM 返回非 JSON /
    # 空 content）冒泡到 `_run_and_cleanup` 的兜底分支被统一报成"任务执行异常"+
    # `internal_error`，用户/调试面看不到"这是规划失败"。而 `DAGNode(**...)` 非法字段
    # 抛的是 `pydantic.ValidationError`，会**穿透** `PlanParseError` 的捕获。
    # 更糟的是不对称容错：Evaluator 有重试、Planner 没有，一次 LLM 抖动直接判死整个任务。
    plan = await _plan_with_retry(planner, context, constraints, task_state, task_id)
    if plan is None:
        return None

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
            abort_code = abort_error_code_fn(validation.issues)
            task_state.fail(f"Plan 校验失败: {issue_messages}",
                            _coerce_code(abort_code))
            await push_mailbox_fn(mailbox, WorkflowEventType.FAILED, {
                "task_id": str(task_id),
                "error": task_state.error,
                "error_type": abort_code,
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
                # E3 §4 终态不可逆——写 RUNNING 前检查终态（cancel 已同步置 CANCELLED → 终止）
                check_cancelled(task_state)
                task_state.plan = graph.to_plan(plan)
                task_state.status = WorkflowStatus.RUNNING
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
        abort_code = abort_error_code_fn(validation.issues)
        task_state.fail(f"Plan 校验失败且重试耗尽: {issue_msgs}",
                        _coerce_code(abort_code))
        await push_mailbox_fn(mailbox, WorkflowEventType.FAILED, {
            "task_id": str(task_id),
            "error": task_state.error,
            "error_type": abort_code,
        })
        return None

    # 3. 存入 WorkflowState（graph 由 validate 返回，无需重复构造）
    # E3 §4 终态不可逆——写 RUNNING 前检查终态（cancel 已同步置 CANCELLED → 终止，不覆写）
    check_cancelled(task_state)
    task_state.plan = graph.to_plan(plan)
    task_state.status = WorkflowStatus.RUNNING
    task_state.updated_at = now_iso()

    return graph
