"""收尾 + checkpoint 工具函数。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from Module.task_system.mailbox import push_mailbox_event

from Module.task_system.schema import (
    DAGNode,
    ExecutionPhase,
    ExecutionState,
    NodeId,
    NodeState,
    NodeStatus,
    TaskCheckpoint,
    TaskEventType,
    TaskState,
    TaskStatus,
    now_iso,
)

if TYPE_CHECKING:
    from Module.task_system.checkpoint import CheckpointManager
    from Module.task_system.core.graph import PlanGraph
    from Module.task_system.mailbox import TaskMailbox

logger = logging.getLogger(__name__)


def init_exec_state(graph: PlanGraph) -> ExecutionState:
    """从 PlanGraph 构建初始 ExecutionState。

    遍历所有 DAG 节点，为每个节点创建初始 NodeState（全部 PENDING）。
    新任务首次执行时调用。Replan 恢复时用 merge_exec_state 代替。
    """
    now = now_iso()
    node_states: dict[NodeId, NodeState] = {}
    for nid in graph.nodes:
        node_states[nid] = NodeState(node_id=nid)

    return ExecutionState(
        node_states=node_states,
        current_phase=ExecutionPhase.EXECUTING,
        created_at=now,
        updated_at=now,
    )


def merge_exec_state(
    old: ExecutionState,
    graph: PlanGraph,
) -> ExecutionState:
    """从旧 ExecutionState 合并到新 graph——Replan 后恢复执行。

    规则：
        1. 保留旧 graph 中存在节点的 SUCCESS / SKIPPED / PENDING 状态
        2. 移除旧 graph 中已不存在的节点（replan 删除的节点）
        3. 旧 graph 中不存在的节点 → 新建 PENDING
        4. FAILED / UNRECOVERABLE 节点 → 重置为 PENDING（replan 已处理，需重新执行）
    """
    now = now_iso()
    merged: dict[NodeId, NodeState] = {}

    for nid in graph.nodes:
        old_ns = old.node_states.get(nid)
        if old_ns is not None and old_ns.status in (
            NodeStatus.SUCCESS, NodeStatus.SKIPPED, NodeStatus.PENDING,
        ):
            # 已完成 / 已跳过 / 尚未开始 → 保留
            merged[nid] = old_ns
        else:
            # 新节点、失败节点、或旧状态不可用 → 新建 PENDING
            merged[nid] = NodeState(node_id=nid)

    return ExecutionState(
        node_states=merged,
        current_phase=ExecutionPhase.EXECUTING,
        created_at=old.created_at,
        updated_at=now,
    )


def has_blocked_nodes(exec_state: ExecutionState) -> bool:
    """是否有节点因等待审批而阻塞。

    v1 始终返回 False——waiting_for_approval 仅由 ASK_APPROVAL 设置，
    而 Evaluator v1 不产出 ASK_APPROVAL。v2 启用审批风暴检测后可达。
    """
    return exec_state.waiting_for_approval


def needs_approval(node: DAGNode) -> bool:
    """判断节点是否需要用户审批——v1 始终返回 False。

    设计决策：
        v1 DAGNode 不持有工具信息，审批由 ToolManager 内部 SafetyPipeline 处理：
        Executor 调用 tool_registry.execute() → SafetyPipeline 发现需要审批
        → 内部暂停等待前端 → 用户批准/拒绝 → 返回结果。
        Orchestrator 完全不感知审批流程。

        v2 方案（审批风暴检测）：
        Planner 生成 DAGNode 时标注 risk_level（"safe" | "mutation"），
        Planner prompt 保守策略：涉及文件写、邮件发送、数据库修改的操作强制标注 mutation。
        Orchestrator 在执行前扫描所有 ready nodes → 汇总 mutation 步骤
        → 通过 HITLNotifier 批量推送审批预览卡片 → 用户一次性批准。
        这样无需拆 Executor 为两阶段（match + execute），也无需 DAGNode 持有 tool_name。

        当前 needs_approval 只在审批检查循环中被调用，返回 True 时节点
        被排除在 ready_nodes 之外，由 wait_for_approval → BLOCKED 路径处理。
    """
    return False


async def finalize(
    task_state: TaskState,
    exec_state: ExecutionState,
    mailbox: TaskMailbox,
    checkpoint_mgr: CheckpointManager,
) -> None:
    """任务终态处理——推送 completed 事件（含节点产出摘要，供 CognitiveLoop 渐进式唤醒）+ 写最终 checkpoint。

    run() 在调用后自行构造 OrchestratorResult(status=SUCCESS)。
    """
    task_state.status = TaskStatus.COMPLETED
    task_state.updated_at = now_iso()
    await save_checkpoint(checkpoint_mgr, task_state, exec_state)

    completed_nodes = sum(
        1 for ns in exec_state.node_states.values()
        if ns.status in (NodeStatus.SUCCESS, NodeStatus.SKIPPED)
    )
    failed_nodes = sum(
        1 for ns in exec_state.node_states.values()
        if ns.status in (NodeStatus.FAILED, NodeStatus.UNRECOVERABLE)
    )
    total_nodes = len(exec_state.node_states)

    # 收集各节点的产出摘要——供 CognitiveLoop 渐进式唤醒时使用
    node_summaries: list[str] = []
    plan = task_state.plan
    if plan is None:
        plan_nodes = []
    else:
        plan_nodes = plan.dag.nodes
    for node in plan_nodes:
        ns = exec_state.node_states.get(node.node_id)
        if ns is None:
            continue
        if ns.status == NodeStatus.SUCCESS:
            desc = node.description or node.node_id
            # 优先用 evaluation.reason（Evaluator 判定摘要），
            # 其次用 result.summary（Executor 产出摘要）
            detail = ""
            if ns.evaluation and ns.evaluation.reason:
                detail = ns.evaluation.reason
            elif ns.summary:
                detail = ns.summary
            if detail:
                node_summaries.append(f"- {desc}: {detail}")
            else:
                node_summaries.append(f"- {desc}（已完成）")
        elif ns.status == NodeStatus.FAILED:
            desc = node.description or node.node_id
            reason = ns.error or "未知错误"
            node_summaries.append(f"- {desc}（失败: {reason}）")

    result_text = f"任务完成。{completed_nodes}/{total_nodes} 个节点成功"
    if failed_nodes:
        result_text += f"，{failed_nodes} 个失败"
    if node_summaries:
        result_text += "。\n\n节点产出:\n" + "\n".join(node_summaries)

    push_mailbox(mailbox, TaskEventType.COMPLETED, {
        "task_id": str(task_state.task_id),
        "result": result_text,
        "summary": f"共 {total_nodes} 个节点，成功 {completed_nodes}，失败 {failed_nodes}",
        "total_nodes": total_nodes,
        "completed_nodes": completed_nodes,
        "failed_nodes": failed_nodes,
        "token_usage": task_state.token_usage,
        "token_total": sum(task_state.token_usage.values()),
    })
    logger.info(
        "Orchestrator: task_id=%s 完成。nodes: %d/%d success",
        task_state.task_id, completed_nodes, total_nodes,
    )


def build_checkpoint(
    task_state: TaskState, exec_state: ExecutionState,
) -> TaskCheckpoint:
    """组合 TaskState + ExecutionState 为类型化 checkpoint 快照。"""
    return TaskCheckpoint(task=task_state, exec=exec_state)


async def save_checkpoint(
    checkpoint_mgr: CheckpointManager,
    task_state: TaskState,
    exec_state: ExecutionState,
) -> None:
    """保存当前状态快照。"""
    checkpoint = build_checkpoint(task_state, exec_state)
    await checkpoint_mgr.save(task_state.task_id, checkpoint)


def push_mailbox(
    mailbox: TaskMailbox,
    event_type: TaskEventType,
    data: dict[str, Any],
) -> None:
    """推送任务事件到 mailbox。"""
    push_mailbox_event(mailbox, event_type, data, caller="Orchestrator")
