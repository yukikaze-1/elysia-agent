"""节点执行管线——execute → evaluate → apply。"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from Module.workflow_contract import (
    ConstraintConfig,
    NodeId,
    SessionId,
    WorkflowEventType,
    WorkflowId,
)
from Module.tool.tool_descriptor import Tool
from Module.workflow_engine.schema import (
    BusinessStatus,
    DAGNode,
    EvaluatorAction,
    ExecutionState,
    ExecutionStatus,
    NodeErrorCode,
    NodeState,
    NodeResult,
    NodeResultEvaluation,
    NodeStatus,
    Plan,
    WorkflowState,
    now_iso,
)

from Module.workflow_engine.core import finalize as _finalize_mod

if TYPE_CHECKING:
    from Module.workflow_engine.core.evaluator import Evaluator
    from Module.workflow_engine.core.executor import Executor
    from Module.workflow_engine.core.graph import PlanGraph
    from Module.workflow_engine.mailbox import WorkflowMailbox

logger = logging.getLogger(__name__)


async def execute_nodes(
    executor: Executor,
    exec_state: ExecutionState,
    nodes: list[DAGNode],
    task_id: WorkflowId,
    tools: list[Tool],
    session_id: SessionId,
    ts: WorkflowState,
) -> list[NodeResult]:
    """执行就绪节点——v1 逐个调用 Executor.execute()。

    对每个就绪节点：更新 NodeState → 调 Executor → 收集 NodeResult。
    v1 不做并发（顺序执行），v2 考虑 Worker Agent 池并发。
    NodeResult 仅包含 Executor 产出（ArtifactStore 引用 + 错误码 + 耗时），
    不含 Evaluator 判定——判定由 evaluate_node 产出 NodeResultEvaluation。

    Args:
        executor: Executor 实例。
        exec_state: 当前执行状态（用于更新 NodeState）。
        nodes: 待执行的 DAGNode 列表（已按拓扑排序）。
        task_id: 任务 ID。
        tools: 可用工具列表。
        session_id: 会话 ID。
        ts: 任务级状态（E3——透传给 Executor 做取消检查）。

    Returns:
        list[NodeResult]: 所有节点的执行结果列表。
    """
    results: list[NodeResult] = []
    for node in nodes:
        ns = exec_state.node_states[node.node_id]
        ns.status = NodeStatus.RUNNING
        ns.attempt += 1
        ns.started_at = now_iso()

        logger.info("Orchestrator: 执行 node=%s attempt=%d", node.node_id, ns.attempt)
        # S2-D3：把上一轮的失败原因带进 prompt——否则重试与首次**逐字节相同**，
        # LLM 只能原样重选同一工具同一参数（盲重跑），对"副作用已产生但返回失败"
        # 的工具会造成重复执行。
        previous_error = ns.error if ns.attempt > 1 else None
        if node.max_duration_s is not None:
            # S2-D4④：`DAGNode.max_duration_s` 此前**只声明、无人读**（parser 不解析、
            # 引擎不读取）——与 `ConstraintConfig` 的"硬约束"同一类"声明与实现相反"。
            # 现在它是真的：单节点超过自身上限即判 TOOL_TIMEOUT。
            # 工具调用在 Executor 内被 asyncio.shield 保护（"副作用不打断"），
            # 故这里取消的只是**等待**，已开始的工具会作为孤儿被 stop() 采集收尾。
            try:
                result = await asyncio.wait_for(
                    executor.execute(node, task_id, tools, session_id, ts,
                                     previous_error=previous_error),
                    timeout=node.max_duration_s,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Orchestrator: node=%s 单节点超时（>%.0fs），判 TOOL_TIMEOUT",
                    node.node_id, node.max_duration_s)
                result = NodeResult(
                    node_id=node.node_id,
                    artifact_refs=[],
                    error=NodeErrorCode.TOOL_TIMEOUT,
                    attempt=0,
                    duration_ms=int(node.max_duration_s * 1000),
                )
        else:
            result = await executor.execute(node, task_id, tools, session_id, ts,
                                            previous_error=previous_error)
        # Executor 返回的 attempt=0，用 Orchestrator 追踪的实际 attempt 覆盖
        result.attempt = ns.attempt
        results.append(result)
    return results


async def evaluate_node(
    evaluator: Evaluator,
    node: DAGNode,
    result: NodeResult,
    plan: Plan | None
) -> NodeResultEvaluation:
    """评估单个节点结果——调 Evaluator 产出 NodeResultEvaluation。

    传入 DAGNode（含 output_contract）和 Plan（上下文），
    Evaluator 内部做双重判定：ExecutionStatus + BusinessStatus。

    Args:
        evaluator: Evaluator 实例。
        node: 原始 DAGNode。
        result: Executor 产出的节点执行结果。
        plan: 当前 Plan（可为 None，Plan 缺失时返回 ABORT 判定）。

    Returns:
        NodeResultEvaluation: 双重判定结果。
    """
    if plan is None:
        # Plan 缺失——严重内部错误，不应发生。直接 ABORT 而非静默成功，
        # 防止掩盖上游 bug（如 WorkflowState 未正确初始化）。
        logger.critical(
            "Orchestrator: node=%s Plan 为 None——这是严重 bug，任务中止",
            result.node_id,
        )
        return NodeResultEvaluation(
            node_id=result.node_id,
            execution_status=ExecutionStatus.ERROR,
            business_status=BusinessStatus.GOAL_FAILED,
            reason="Plan 缺失——内部错误",
            suggested_action=EvaluatorAction.ABORT,
            result=result,
        )
    return await evaluator.evaluate(result, node, plan)


def apply_evaluation(
    node_id: NodeId,
    evaluation: NodeResultEvaluation,
    exec_state: ExecutionState,
    graph: PlanGraph,
    constraints: ConstraintConfig,
    task_state: WorkflowState,
    mailbox: WorkflowMailbox,
) -> EvaluatorAction:
    """根据 action 分发到对应处理函数——更新 NodeState + 推送 Mailbox 事件。

    Args:
        node_id: 节点 ID。
        evaluation: Evaluator 产出的双重判定结果。
        exec_state: 当前执行状态。
        graph: DAG 图。
        constraints: 全局约束配置。
        task_state: 任务状态。
        mailbox: 任务事件邮箱。

    Returns:
        EvaluatorAction: suggested_action，调用方据此决定是否退出主循环。
    """
    ns = exec_state.node_states[node_id]
    ns.finished_at = now_iso()
    ns.evaluation = evaluation  # 保留 Evaluator 完整判定，供 Replanner 诊断
    action = evaluation.suggested_action

    # ── 按 action 分发 ──────────────────────────────────────────────────
    if action == EvaluatorAction.CONTINUE:
        return _handle_continue(ns, evaluation, node_id, exec_state, graph, task_state, mailbox)
    elif action == EvaluatorAction.SKIP:
        return _handle_skip(ns, evaluation, node_id, exec_state, graph, task_state, mailbox)
    elif action == EvaluatorAction.RETRY:
        return _handle_retry(ns, evaluation, node_id, exec_state, graph, constraints, task_state, mailbox)
    elif action == EvaluatorAction.REPLAN:
        return _handle_replan_progress(ns, evaluation, node_id, exec_state, graph, task_state, mailbox)
    elif action == EvaluatorAction.ASK_APPROVAL:
        # 审批阻塞——不推送 progress，仅标记状态
        exec_state.waiting_for_approval = True
        ns.status = NodeStatus.WAITING_APPROVAL
    elif action == EvaluatorAction.ABORT:
        # 不可恢复——不推送 progress，由 run() 统一推送 FAILED
        ns.status = NodeStatus.FAILED
        ns.error = evaluation.reason
        ns.error_code = evaluation.result.error

    return action


# ── apply_evaluation 的 5 个 action handler + 1 个共享 helper ──────────────
# 每个 handler 负责：更新 NodeState → 推送 PROGRESS 事件（若有）→ 返回 action


def _push_progress(
    node_id: NodeId,
    exec_state: ExecutionState,
    graph: PlanGraph,
    task_state: WorkflowState,
    mailbox: WorkflowMailbox,
    description: str,
    message: str,
) -> None:
    """推送 PROGRESS 事件到 mailbox——CONTINUE/SKIP/RETRY/REPLAN 四个分支复用。

    Args:
        node_id: 节点 ID。
        exec_state: 当前执行状态（用于计算完成比例）。
        graph: DAG 图（用于计算进度百分比）。
        task_state: 任务状态。
        mailbox: 任务事件邮箱。
        description: 节点描述文本。
        message: 进度消息。
    """
    completed_count = sum(
        1 for s in exec_state.node_states.values()
        if s.status in (NodeStatus.SUCCESS, NodeStatus.SKIPPED)
    )
    _finalize_mod.push_mailbox(mailbox, WorkflowEventType.PROGRESS, {
        "task_id": str(task_state.task_id),
        "node_id": node_id,
        "node_description": description,
        "node_index": f"{completed_count}/{graph.node_count}",
        "percent": graph.compute_progress(exec_state.node_states),
        "message": message,
    })


# ── I. CONTINUE：节点成功 ─────────────────────────────────────────────────

def _handle_continue(
    ns: NodeState,
    evaluation: NodeResultEvaluation,
    node_id: NodeId,
    exec_state: ExecutionState,
    graph: PlanGraph,
    task_state: WorkflowState,
    mailbox: WorkflowMailbox,
) -> EvaluatorAction:
    """节点执行成功——标记 SUCCESS + 收集 artifact 引用 + 推送进度。

    Args:
        ns: 节点运行时状态。
        evaluation: Evaluator 判定结果。
        node_id: 节点 ID。
        exec_state: 当前执行状态。
        graph: DAG 图。
        task_state: 任务状态。
        mailbox: 任务事件邮箱。

    Returns:
        EvaluatorAction.CONTINUE
    """
    ns.status = NodeStatus.SUCCESS
    ns.summary = evaluation.reason
    ns.artifact_refs = evaluation.result.artifact_refs
    _push_progress(
        node_id, exec_state, graph, task_state, mailbox,
        description=f"节点 {node_id} 完成",
        message=ns.summary,
    )
    return EvaluatorAction.CONTINUE


# ── II. SKIP：可选节点跳过 ───────────────────────────────────────────────

def _handle_skip(
    ns: NodeState,
    evaluation: NodeResultEvaluation,
    node_id: NodeId,
    exec_state: ExecutionState,
    graph: PlanGraph,
    task_state: WorkflowState,
    mailbox: WorkflowMailbox,
) -> EvaluatorAction:
    """跳过可选节点——若 required 节点被建议 SKIP 则降级为 REPLAN（防死锁）。

    Args:
        ns: 节点运行时状态。
        evaluation: Evaluator 判定结果。
        node_id: 节点 ID。
        exec_state: 当前执行状态。
        graph: DAG 图。
        task_state: 任务状态。
        mailbox: 任务事件邮箱。

    Returns:
        EvaluatorAction.SKIP 或 REPLAN（required 节点降级时）。
    """
    if graph.nodes[node_id].required:
        logger.error(
            "Orchestrator: required 节点 %s 收到 SKIP——降级为 REPLAN", node_id,
        )
        ns.status = NodeStatus.FAILED
        ns.error = f"required 节点被建议 SKIP: {evaluation.reason}"
        return EvaluatorAction.REPLAN
    ns.status = NodeStatus.SKIPPED
    ns.summary = evaluation.reason
    _push_progress(
        node_id, exec_state, graph, task_state, mailbox,
        description=f"节点 {node_id} 跳过（非必需）",
        message=ns.summary,
    )
    logger.info("Orchestrator: node=%s 跳过（非必需节点）", node_id)
    return EvaluatorAction.SKIP


# ── III. RETRY：重试（含上限检查） ──────────────────────────────────────

def _handle_retry(
    ns: NodeState,
    evaluation: NodeResultEvaluation,
    node_id: NodeId,
    exec_state: ExecutionState,
    graph: PlanGraph,
    constraints: ConstraintConfig,
    task_state: WorkflowState,
    mailbox: WorkflowMailbox,
) -> EvaluatorAction:
    """重试节点——检查上限：未达上限 → PENDING 重新入队；已达上限 → REPLAN。

    Args:
        ns: 节点运行时状态。
        evaluation: Evaluator 判定结果。
        node_id: 节点 ID。
        exec_state: 当前执行状态。
        graph: DAG 图。
        constraints: 全局约束配置（含 max_retries_per_node）。
        task_state: 任务状态。
        mailbox: 任务事件邮箱。

    Returns:
        EvaluatorAction.RETRY 或 REPLAN（重试耗竭时）。
    """
    node = graph.nodes[node_id]
    max_retries = (
        node.max_retries
        if node.max_retries is not None
        else constraints.max_retries_per_node
    )
    # 用 > 而非 >=：attempt 从 1 开始，max_retries=3 允许 attempt 1,2,3（共 3 次）
    if ns.attempt > max_retries:
        # ── 重试耗竭 → REPLAN ──
        logger.warning(
            "Orchestrator: node=%s 重试次数已达上限 attempt=%d max=%d",
            node_id, ns.attempt, max_retries,
        )
        ns.status = NodeStatus.FAILED
        ns.error = f"重试次数已达上限 ({max_retries})"
        ns.error_code = evaluation.result.error
        _push_progress(
            node_id, exec_state, graph, task_state, mailbox,
            description=f"节点 {node_id} 重试耗尽——触发 Replan",
            message=ns.error,
        )
        return EvaluatorAction.REPLAN

    # ── 未达上限 → PENDING（下一轮重新执行）──
    ns.status = NodeStatus.PENDING
    ns.error = evaluation.reason
    ns.error_code = evaluation.result.error
    _push_progress(
        node_id, exec_state, graph, task_state, mailbox,
        description=f"节点 {node_id} 重试中 ({ns.attempt}/{max_retries})",
        message=ns.error,
    )
    logger.info("Orchestrator: node=%s 评估 retry, attempt=%d", node_id, ns.attempt)
    return EvaluatorAction.RETRY


# ── IV. REPLAN：结构性失败 ───────────────────────────────────────────────

def _handle_replan_progress(
    ns: NodeState,
    evaluation: NodeResultEvaluation,
    node_id: NodeId,
    exec_state: ExecutionState,
    graph: PlanGraph,
    task_state: WorkflowState,
    mailbox: WorkflowMailbox,
) -> EvaluatorAction:
    """节点结构性失败——标记 FAILED + 推送进度（通知前端 DAG 即将修复）。

    Args:
        ns: 节点运行时状态。
        evaluation: Evaluator 判定结果。
        node_id: 节点 ID。
        exec_state: 当前执行状态。
        graph: DAG 图。
        task_state: 任务状态。
        mailbox: 任务事件邮箱。

    Returns:
        EvaluatorAction.REPLAN
    """
    ns.status = NodeStatus.FAILED
    ns.error = evaluation.reason
    ns.error_code = evaluation.result.error
    _push_progress(
        node_id, exec_state, graph, task_state, mailbox,
        description=f"节点 {node_id} 失败——触发 Replan",
        message=ns.error,
    )
    logger.info("Orchestrator: node=%s 需要 Replan, reason=%s", node_id, ns.error)
    return EvaluatorAction.REPLAN

# 注：ASK_APPROVAL 和 ABORT 的简单处理（均为标记状态、不推送 progress）
# 保留在 apply_evaluation 的 elif 分支中——不必提取为独立函数。
