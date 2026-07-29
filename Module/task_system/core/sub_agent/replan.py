"""Replan 决策——熔断检查 → Replanner.replan() → DAGPatch 应用。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from Module.task_system.schema import (
    ConstraintConfig,
    DAGPatch,
    DelegationContext,
    ExecutionState,
    NodeId,
    NodeState,
    OrchestratorResult,
    Plan,
    TaskCheckpoint,
    TaskErrorCode,
    TaskEventType,
    TaskState,
    TaskStatus,
    now_iso,
)

if TYPE_CHECKING:
    from Module.task_system.checkpoint import CheckpointManager
    from Module.task_system.core.graph import PlanGraph
    from Module.task_system.core.planner import Replanner
    from Module.task_system.core.plan_validator import PlanValidator
    from Module.task_system.core.orchestrator import Orchestrator
    from Module.task_system.mailbox import TaskMailbox
    from Module.task_system.token_ledger import TokenLedger

logger = logging.getLogger(__name__)


def check_meltdown(
    plan_version: int,
    max_failure_count: int,
    max_task_duration_s: float,
    created_at: str,
    token_ledger: "TokenLedger | None",
    max_total_tokens: int,
) -> str | None:
    """检查是否触发熔断——plan_version / 时间预算 / token 预算。

    plan_version 从 1 开始，每次 Replan 递增。用 > 比较意味着
    max_failure_count=5 时允许多达 5 次 Replan（version 2→6），
    version 7 触发熔断。

    Args:
        plan_version: 当前计划版本号，从 1 开始
        max_failure_count: 最大允许的 Replan 次数上限
        max_task_duration_s: 任务最大允许耗时（秒）
        created_at: 任务创建时间的 ISO 时间戳
        token_ledger: Token 记账器，用于检查 token 预算；None 时跳过 token 检查
        max_total_tokens: 任务最大允许消耗的 token 总数

    Returns:
        None → 可以继续
        str  → 熔断原因（不能继续）
    """
    # 1. 计划版本
    if plan_version > max_failure_count:
        return (
            f"已 Replan {plan_version - 1} 次（上限 {max_failure_count}），"
            "已触发熔断"
        )

    # 2. 时间预算
    elapsed_s = _elapsed_seconds(created_at)
    if elapsed_s > max_task_duration_s:
        return (
            f"任务耗时已超过 {elapsed_s:.0f} 秒"
            f"（上限 {max_task_duration_s:.0f} 秒），已触发熔断"
        )

    # 3. Token 预算
    if token_ledger is not None:
        used = token_ledger.total_tokens
        if used > max_total_tokens:
            return (
                f"任务已消耗 {used} tokens"
                f"（上限 {max_total_tokens}），已触发熔断"
            )

    return None


def _elapsed_seconds(created_at: str) -> float:
    """从 IsoTimestamp 计算已过去秒数。解析失败返回 0.0（优雅降级）。

    Args:
        created_at: ISO 格式的时间戳字符串

    Returns:
        从 created_at 到现在的秒数；解析失败时返回 0.0
    """
    from datetime import datetime, timezone

    try:
        created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created_dt).total_seconds()
    except (ValueError, TypeError):
        return 0.0


def extract_node_results(checkpoint: TaskCheckpoint) -> dict[str, NodeState]:
    """从 TaskCheckpoint 中提取 node_states，供 Replanner.replan() 使用。

    NodeState 现包含 evaluation 字段（Evaluator 完整判定），
    Replanner.replan() 可直接读取每个节点的：
        - status（SUCCESS/FAILED/SKIPPED/PENDING）
        - summary（简短摘要）
        - error / error_code
        - evaluation（NodeResultEvaluation：execution_status + business_status + suggested_action）

    Args:
        checkpoint: 任务检查点，包含所有节点的执行状态

    Returns:
        节点 ID 到 NodeState 的映射字典
    """
    return {
        str(nid): ns
        for nid, ns in checkpoint.exec.node_states.items()
    }


async def handle_replan(
    result: OrchestratorResult,
    task_state: TaskState,
    graph: PlanGraph,
    mailbox: TaskMailbox,
    context: DelegationContext,
    replanner: Replanner,
    plan_validator: PlanValidator,
    orchestrator: Orchestrator,         # 仅读取 _exec_state 检查中断信号
    checkpoint_mgr: CheckpointManager,
    constraints: ConstraintConfig,
    push_mailbox_fn: Callable[..., Awaitable[None]],
    token_ledger: "TokenLedger | None" = None,
) -> tuple[bool, ExecutionState | None]:
    """结构性失败——熔断检查 → Replan → DAGPatch 应用 → 构建 resume_state。

    Args:
        result: Orchestrator 执行结果，包含 error_context 和 checkpoint_state
        task_state: 当前任务状态
        graph: 任务 DAG 图
        mailbox: 任务邮箱，用于推送事件
        context: 委托上下文（意图、工具、约束等）
        replanner: Replan 引擎
        plan_validator: 计划校验器
        orchestrator: Orchestrator 实例，用于检查中断信号
        checkpoint_mgr: 检查点管理器
        constraints: 全局约束配置
        push_mailbox_fn: 推送邮箱事件的异步回调
        token_ledger: Token 记账器，用于熔断时的 token 预算检查

    Returns:
        (True, resume_state)  — replan 成功，DAG 已更新，resume_state 供下次 run()
        (False, None)         — 熔断 / checkpoint 缺失 / replan 异常，已降级为 FAILED
    """
    # 1. 熔断检查
    if task_state.plan is None:
        raise RuntimeError("NEEDS_REPLAN 时 plan 不应为 None")
    meltdown_reason = check_meltdown(
        task_state.plan.plan_version, constraints.max_failure_count,
        constraints.max_task_duration_s, task_state.created_at,
        token_ledger, constraints.max_total_tokens,
    )
    if meltdown_reason:
        logger.warning(
            "SubAgent: task_id=%s 熔断: %s",
            task_state.task_id, meltdown_reason,
        )
        task_state.status = TaskStatus.FAILED
        task_state.updated_at = now_iso()
        await push_mailbox_fn(mailbox, TaskEventType.FAILED, {
            "error": meltdown_reason,
            "error_type": TaskErrorCode.FAILURE_MELTDOWN.value,
        })
        return False, None

    # 1.5. 检查外部中断信号——在昂贵的 Replanner.replan() 之前拦截。
    exec_state = orchestrator._exec_state
    if exec_state is not None:
        if getattr(exec_state, "cancel_requested", False) is True:
            logger.info(
                "SubAgent: task_id=%s Replan 前检测到取消信号，短路",
                task_state.task_id,
            )
            task_state.status = TaskStatus.CANCELLED
            task_state.updated_at = now_iso()
            await push_mailbox_fn(mailbox, TaskEventType.CANCELLED, {
                "task_id": str(task_state.task_id),
                "reason": "用户取消",
            })
            return False, None
        if getattr(exec_state, "pause_requested", False) is True:
            logger.info(
                "SubAgent: task_id=%s Replan 前检测到暂停信号，短路",
                task_state.task_id,
            )
            task_state.status = TaskStatus.PAUSED
            task_state.updated_at = now_iso()
            # PAUSED 需要 checkpoint——复用上一次 run() 返回的 checkpoint
            checkpoint = result.checkpoint_state
            if checkpoint is not None:
                await checkpoint_mgr.save(
                    task_state.task_id, checkpoint,
                )
            await push_mailbox_fn(mailbox, TaskEventType.PAUSED, {
                "task_id": str(task_state.task_id),
                "message": "任务已暂停",
            })
            return False, None

    # 2. Replan——调 Replanner.replan()，产出 DAGPatch
    error_ctx = result.error_context
    failed_node_ids: list[NodeId] = error_ctx.get("failed_nodes", [])
    logger.info(
        "SubAgent: task_id=%s 触发 Replan, failed_nodes=%s",
        task_state.task_id, failed_node_ids,
    )

    checkpoint = result.checkpoint_state
    if checkpoint is None:
        logger.error(
            "SubAgent: task_id=%s NEEDS_REPLAN 但 checkpoint_state 为 None，"
            "跳过 Replan", task_state.task_id,
        )
        task_state.status = TaskStatus.FAILED
        task_state.updated_at = now_iso()
        await push_mailbox_fn(mailbox, TaskEventType.FAILED, {
            "task_id": str(task_state.task_id),
            "error": "NEEDS_REPLAN 但 checkpoint 缺失，无法恢复",
            "error_type": TaskErrorCode.INTERNAL_ERROR.value,
        })
        return False, None

    # 调 Replanner.replan()，产出 DAGPatch（局部修复）或 Plan（全量重建）
    try:
        plan = graph.to_plan(task_state.plan)
        tc = context.task_constraints.resolve(constraints)
        replan_result = await replanner.replan(
            original_plan=plan,
            failed_node_ids=failed_node_ids,
            failure_diagnosis=error_ctx.get("reason", "节点执行失败"),
            node_results=extract_node_results(checkpoint),
            intent=context.intent,
            tools=context.tools,
            max_nodes=tc.max_dag_nodes or constraints.max_dag_nodes,
            max_mutations=tc.max_mutations or constraints.max_mutations,
        )
    except Exception as e:
        logger.warning(
            "SubAgent: task_id=%s replan 调用异常: %s，降级为 FAILED",
            task_state.task_id, e,
        )
        task_state.status = TaskStatus.FAILED
        task_state.updated_at = now_iso()
        await push_mailbox_fn(mailbox, TaskEventType.FAILED, {
            "task_id": str(task_state.task_id),
            "error": f"Replan 调用异常: {e}",
            "error_type": TaskErrorCode.INTERNAL_ERROR.value,
        })
        return False, None

    # ── 分发：Plan（全量重建）vs DAGPatch（局部修复）──────────────

    if isinstance(replan_result, Plan):
        # 全量重建路径——校验新 Plan → 新建 PlanGraph → 不复用旧状态
        logger.info(
            "SubAgent: task_id=%s Replan 全量重建, nodes=%d",
            task_state.task_id, len(replan_result.dag.nodes),
        )
        validation, new_graph = plan_validator.validate(
            plan=replan_result,
            global_constraints=constraints,
            task_constraints=context.task_constraints,
        )
        if not validation.valid:
            issue_messages = [i.message for i in validation.issues]
            logger.error(
                "SubAgent: task_id=%s Replan 全量重建校验失败: %s",
                task_state.task_id, issue_messages,
            )
            task_state.status = TaskStatus.FAILED
            task_state.updated_at = now_iso()
            await push_mailbox_fn(mailbox, TaskEventType.FAILED, {
                "task_id": str(task_state.task_id),
                "error": f"Replan 全量重建校验失败: {issue_messages}",
                "error_type": TaskErrorCode.PLAN_TOO_LARGE.value,
            })
            return False, None

        # 全量重建——用新 graph 替换旧 graph 内部状态
        graph.replace(new_graph)
        task_state.plan = new_graph.to_plan(replan_result)
        task_state.plan.plan_version = plan.plan_version + 1
        logger.info(
            "SubAgent: task_id=%s 全量重建完成, version=%d, nodes=%d",
            task_state.task_id, task_state.plan.plan_version,
            new_graph.node_count,
        )
        return True, None

    else:
        # 局部修复路径——应用 DAGPatch
        replan_patch: DAGPatch = replan_result
        graph.apply_patch(replan_patch)

        # 校验 patch 后的图结构
        structural_issues = graph.validate_structure()
        if structural_issues:
            issue_msgs = [i.message for i in structural_issues]
            logger.error(
                "SubAgent: task_id=%s Replan DAGPatch 引入结构问题: %s",
                task_state.task_id, issue_msgs,
            )
            task_state.status = TaskStatus.FAILED
            task_state.updated_at = now_iso()
            await push_mailbox_fn(mailbox, TaskEventType.FAILED, {
                "task_id": str(task_state.task_id),
                "error": f"Replan DAGPatch 校验失败: {issue_msgs}",
                "error_type": TaskErrorCode.PLAN_TOO_LARGE.value,
            })
            return False, None

        task_state.plan = graph.to_plan(plan)
        task_state.plan.plan_version += 1
        logger.info(
            "SubAgent: task_id=%s Replan 局部修复完成, patch_type=%s, version=%d, nodes=%d",
            task_state.task_id, replan_patch.patch_type,
            task_state.plan.plan_version, graph.node_count,
        )
        return True, checkpoint.exec
