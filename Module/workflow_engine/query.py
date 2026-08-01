"""WorkflowEngine 查询 API —— 只读访问函数。

所有函数接受显式参数替代 WorkflowEngine 内部状态，
供 WorkflowEngine 方法薄封装委托，也可被 Debug API 直接调用。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

from Module.workflow_contract import (
    AgentId,
    NodeId,
    WorkflowId,
    WorkflowStatus,
)
from Module.workflow_engine.schema import (
    AgentInfo,
    AgentList,
    AgentStatus,
    AgentType,
    NodeStatus,
    WorkflowDAG,
    WorkflowProgress,
    WorkflowSummary,
    TokenPeriod,
    TokenStats,
    TokenUsage,
    now_iso,
    parse_iso,
)

if TYPE_CHECKING:
    from Module.workflow_engine.core.execution import ExecutionState
    from Module.workflow_engine.core.sub_agent import SubAgent
    from Module.workflow_engine.mailbox import WorkflowMailbox

logger = logging.getLogger(__name__)


def get_task_status(
    tasks: dict[WorkflowId, SubAgent],
    task_id: WorkflowId,
) -> WorkflowStatus | None:
    """查询任务当前状态。最轻量——用户问"做完了吗？"时 LLM 调用。

    只查活跃任务（仍在 tasks 注册表中的）。任务终态清理后不可查询——
    此时返回 None，调用方应通过上层缓存获取历史状态。

    Args:
        tasks: 所有活跃任务的 SubAgent 映射。
        task_id: 要查询的任务 ID。

    Returns:
        WorkflowStatus | None: 任务状态，不存在返回 None。
    """
    sub_agent = tasks.get(task_id)
    if sub_agent is None:
        return None
    ts = sub_agent._task_state
    if ts is None:
        return WorkflowStatus.PENDING
    return ts.status


def get_task_mailbox(
    mailboxes: dict[WorkflowId, WorkflowMailbox],
    task_id: WorkflowId,
) -> WorkflowMailbox | None:
    """获取指定任务的 mailbox（供 Agent 轮询消费）。

    任务不存在或已清理时返回 None。

    Args:
        mailboxes: 所有活跃任务的事件邮箱映射。
        task_id: 要查询的任务 ID。

    Returns:
        WorkflowMailbox | None: 邮箱对象，不存在返回 None。
    """
    return mailboxes.get(task_id)


async def get_task_summary(
    tasks: dict[WorkflowId, SubAgent],
    task_id: WorkflowId,
) -> WorkflowSummary | None:
    """获取单个任务摘要。

    Args:
        tasks: 所有活跃任务的 SubAgent 映射。
        task_id: 要查询的任务 ID。

    Returns:
        WorkflowSummary | None: 任务摘要，不存在返回 None。
    """
    summaries = await list_active_tasks(tasks)
    for s in summaries:
        if s.task_id == task_id:
            return s
    return None


async def list_active_tasks(
    tasks: dict[WorkflowId, SubAgent],
) -> list[WorkflowSummary]:
    """列出所有活跃任务。

    Args:
        tasks: 所有活跃任务的 SubAgent 映射。

    Returns:
        list[WorkflowSummary]: 任务摘要列表。
    """
    summaries: list[WorkflowSummary] = []
    for task_id, sub_agent in tasks.items():
        ts = sub_agent._task_state
        if ts is None:
            continue  # 尚未完成 run() 初始化，跳过
        checkpoint = (
            await sub_agent._checkpoint_mgr.load(task_id)
            if sub_agent._checkpoint_mgr else None
        )
        exec_state = checkpoint.exec if checkpoint else None

        nodes_total = 0
        nodes_completed = 0
        nodes_failed = 0
        nodes_skipped = 0
        if exec_state:
            nodes_total = len(exec_state.node_states)
            for ns in exec_state.node_states.values():
                if ns.status == NodeStatus.SUCCESS:
                    nodes_completed += 1
                elif ns.status in (NodeStatus.FAILED, NodeStatus.UNRECOVERABLE):
                    nodes_failed += 1
                elif ns.status == NodeStatus.SKIPPED:
                    nodes_skipped += 1

        # extract intent from plan
        intent = ""
        if ts.plan and ts.plan.dag.nodes:
            intent = ts.plan.dag.nodes[0].description

        # calculate duration
        duration_ms = 0
        if ts.created_at:
            try:
                created = parse_iso(ts.created_at)
                duration_ms = int((datetime.now(timezone.utc) - created).total_seconds() * 1000)
            except (ValueError, TypeError):
                pass

        summaries.append(WorkflowSummary(
            task_id=ts.task_id,
            status=ts.status,
            intent=intent,
            created_at=ts.created_at,
            duration_ms=duration_ms,
            nodes={"total": nodes_total, "completed": nodes_completed,
                   "failed": nodes_failed, "skipped": nodes_skipped},
            tools_used=await collect_tools_used(sub_agent, exec_state),
            token_usage=ts.token_usage,
            token_total=sum(ts.token_usage.values()),
            approvals={"requested": 0, "approved": 0, "denied": 0},
            error=ts.error,
            error_code=ts.error_code,
            replans=ts.plan.plan_version - 1 if ts.plan else 0,
        ))
    return summaries


def get_progress(
    tasks: dict[WorkflowId, SubAgent],
    task_id: WorkflowId,
) -> WorkflowProgress | None:
    """查询任务执行进度。用户问"到哪一步了？"时 LLM 调用。

    返回当前执行节点、完成数/总数、耗时、预估剩余时间。

    Args:
        tasks: 所有活跃任务的 SubAgent 映射。
        task_id: 要查询的任务 ID。

    Returns:
        WorkflowProgress | None: 进度信息，任务不存在或尚未开始执行时返回 None。
    """
    sub_agent = tasks.get(task_id)
    if sub_agent is None:
        return None

    ts = sub_agent._task_state
    if ts is None:
        return None

    exec_state = (
        sub_agent._orchestrator._exec_state
        if sub_agent._orchestrator else None
    )
    if exec_state is None:
        # 尚未进入 Orchestrator（规划/校验阶段）
        return WorkflowProgress(
            task_id=task_id,
            status=ts.status,
            total_nodes=len(
                ts.plan.dag.nodes) if ts.plan else 0,
        )

    # 统计节点完成数
    completed = 0
    failed = 0
    current_node_id: NodeId | None = None
    for nid, ns in exec_state.node_states.items():
        if ns.status == NodeStatus.SUCCESS:
            completed += 1
        elif ns.status in (NodeStatus.FAILED, NodeStatus.UNRECOVERABLE):
            failed += 1
        elif ns.status == NodeStatus.RUNNING:
            current_node_id = nid

    total = len(exec_state.node_states)

    # 耗时
    elapsed_ms = 0
    if ts.created_at:
        try:
            created = parse_iso(ts.created_at)
            elapsed_ms = int(
                (datetime.now(timezone.utc) - created).total_seconds() * 1000)
        except (ValueError, TypeError):
            pass

    # 预估剩余时间（简单线性估算）
    estimated_remaining_ms: int | None = None
    done = completed + failed
    if done > 0 and elapsed_ms > 0 and total > done:
        avg_per_node = elapsed_ms / done
        estimated_remaining_ms = int(avg_per_node * (total - done))

    return WorkflowProgress(
        task_id=task_id,
        status=ts.status,
        current_node_id=current_node_id,
        completed_nodes=completed,
        total_nodes=total,
        elapsed_ms=elapsed_ms,
        estimated_remaining_ms=estimated_remaining_ms,
    )


async def get_task_dag(
    tasks: dict[WorkflowId, SubAgent],
    task_id: WorkflowId,
) -> WorkflowDAG | None:
    """获取任务的 DAG 可视化数据。

    Args:
        tasks: 所有活跃任务的 SubAgent 映射。
        task_id: 要查询的任务 ID。

    Returns:
        WorkflowDAG | None: DAG 可视化数据，不存在返回 None。
    """
    sub_agent = tasks.get(task_id)
    if sub_agent is None:
        # CheckpointManager 现为 per-task（由 SubAgent.run() 创建），
        # 任务不活跃时无对应 CheckpointManager，直接返回 None
        return None

    graph = sub_agent._graph
    if graph is None:
        return None
    exec_state = (
        sub_agent._orchestrator._exec_state
        if sub_agent._orchestrator else None
    )
    return graph.to_dag_view(exec_state.node_states if exec_state else {})


def get_token_usage(
    tasks: dict[WorkflowId, SubAgent],
    task_id: WorkflowId,
) -> TokenUsage | None:
    """查询单任务的 token 消耗明细（含完整审计日志）。

    返回 TokenUsage（含 components 列表），每次 LLM 调用一条记录。

    Args:
        tasks: 所有活跃任务的 SubAgent 映射。
        task_id: 要查询的任务 ID。

    Returns:
        TokenUsage | None: token 消耗明细，任务不存在或尚未初始化时返回 None。
    """
    sub_agent = tasks.get(task_id)
    ts = sub_agent._task_state if sub_agent else None
    if ts is None:
        return None
    return TokenUsage(
        task_id=task_id,
        components=list(ts.token_entries),
    )


def list_agents(
    tasks: dict[WorkflowId, SubAgent],
    max_sub_agents: int,
    has_slots: bool,
) -> AgentList:
    """当前活跃的 Sub-Agent 和 Worker Agent 列表。Debug 用。

    v1 只有 SubAgent（无 Worker），worker_agents 固定为空列表。

    Args:
        tasks: 所有活跃任务的 SubAgent 映射。
        max_sub_agents: 最大并发 Sub-Agent 数。
        has_slots: 是否还有可用槽位。

    Returns:
        AgentList: Sub-Agent 和 Worker Agent 列表。
    """

    now = datetime.now(timezone.utc)
    sub_agents: list[AgentInfo] = []

    for task_id, sub_agent in tasks.items():
        ts = sub_agent._task_state

        # 映射 WorkflowStatus → AgentStatus
        if ts is None:
            agent_status = AgentStatus.RUNNING
        elif ts.status in (WorkflowStatus.RUNNING, WorkflowStatus.PLANNING,
                           WorkflowStatus.PAUSE_REQUESTED, WorkflowStatus.RESUMING):
            agent_status = AgentStatus.RUNNING
        elif ts.status == WorkflowStatus.PAUSED:
            agent_status = AgentStatus.PAUSED
        elif ts.status == WorkflowStatus.WAITING_APPROVAL:
            agent_status = AgentStatus.AWAITING_APPROVAL
        else:
            # 终态（COMPLETED/FAILED/CANCELLED）——理论上已清理，
            # 但如果在清理窗口期内仍可见，标记为 RUNNING
            agent_status = AgentStatus.RUNNING

        # 耗时
        duration_ms = 0
        started_at = ""
        if ts and ts.created_at:
            started_at = ts.created_at
            try:
                created = parse_iso(ts.created_at)
                duration_ms = int(
                    (now - created).total_seconds() * 1000)
            except (ValueError, TypeError):
                pass

        sub_agents.append(AgentInfo(
            agent_id=AgentId(str(task_id)),
            agent_type=AgentType.SUB,
            task_id=task_id,
            status=agent_status,
            started_at=started_at,
            last_seen_at=now.isoformat(),
            duration_ms=duration_ms,
        ))

    return AgentList(
        sub_agents=sub_agents,
        worker_agents=[],  # v1 无 Worker Agent
        total_capacity=max_sub_agents,
        available_slots=(
            max_sub_agents - len(tasks)
            if has_slots else 0
        ),
    )


def get_token_stats(
    tasks: dict[WorkflowId, SubAgent],
    period: TokenPeriod = TokenPeriod.TODAY,
) -> TokenStats:
    """聚合 token 统计（按时间段 + 组件 + 模型）。

    遍历所有活跃任务，按 period 过滤，按 component/model 聚合。
    只统计活跃任务（tasks dict 中的），已清理的任务不可查询。

    Args:
        tasks: 所有活跃任务的 SubAgent 映射。
        period: 统计时间段。

    Returns:
        TokenStats: 聚合后的 token 统计。
    """
    now = datetime.now(timezone.utc)
    cutoff: datetime | None = None
    if period == TokenPeriod.TODAY:
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == TokenPeriod.WEEK:
        cutoff = now - timedelta(days=7)
    elif period == TokenPeriod.MONTH:
        cutoff = now - timedelta(days=30)
    # ALL: cutoff = None（不过滤）

    total = 0
    by_component: dict[str, int] = {}
    by_model: dict[str, int] = {}

    for sub_agent in tasks.values():
        ts = sub_agent._task_state
        if ts is None:
            continue
        # 按时间段过滤
        if cutoff is not None and ts.created_at:
            try:
                created = parse_iso(ts.created_at)
                if created < cutoff:
                    continue
            except (ValueError, TypeError):
                pass
        for entry in ts.token_entries:
            total += entry.total_tokens
            key = entry.component.value
            by_component[key] = by_component.get(key, 0) + entry.total_tokens
            if entry.model:
                by_model[entry.model] = by_model.get(entry.model, 0) + entry.total_tokens

    return TokenStats(
        period=period,
        total_tokens=total,
        by_component=by_component,
        by_model=by_model,
    )


def get_debug_snapshot(
    tasks: dict[WorkflowId, SubAgent],
    mailboxes: dict[WorkflowId, WorkflowMailbox],
    max_sub_agents: int,
) -> dict:
    """供 WorkflowDebugAPI 内部轮询使用。

    线程安全说明：
        本方法在 WorkflowDebugAPI._poll_loop() 中调用，与 Orchestrator 主循环
        运行在同一个 asyncio event loop 中。CPython + asyncio 的协作式调度
        保证本方法在遍历 tasks / exec_state 期间不会在非 await 点被挂起——
        因此读取 exec_state.node_states 等可变 dict 时不需显式加锁。

        约束：本方法内禁止加 await。若将来需要异步 I/O，必须在 await 之前
        复制快照，await 之后重新获取 exec_state 引用。

    Args:
        tasks: 所有活跃任务的 SubAgent 映射。
        mailboxes: 所有活跃任务的事件邮箱映射。
        max_sub_agents: 最大并发 Sub-Agent 数。

    Returns:
        dict: 包含 tasks 列表和 slots_total/slots_used 的调试快照。
    """
    task_list = []
    for task_id, sub_agent in tasks.items():
        task_list.append({
            "task_state": sub_agent._task_state,
            "graph": sub_agent._graph,
            "exec_state": sub_agent._orchestrator._exec_state if sub_agent._orchestrator else None,
            "mailbox_depth": mailboxes[task_id].count(),
        })
    return {
        "tasks": task_list,
        "slots_total": max_sub_agents,
        "slots_used": len(tasks),
    }


async def collect_tools_used(
    sub_agent: SubAgent,
    exec_state: "ExecutionState | None",
) -> list[str]:
    """从执行状态 + ArtifactStore 收集实际被调用的工具名。

    遍历已完成节点的 artifact_refs → ArtifactStore.get() →
    读取 metadata.tool_name，生成真实的工具审计列表。

    Args:
        sub_agent: SubAgent 实例。
        exec_state: 执行状态对象，None 时返回空列表。

    Returns:
        list[str]: 实际被调用的工具名列表（去重）。
    """
    tool_names: list[str] = []
    if exec_state is None:
        return tool_names
    store = sub_agent._artifact_store
    if store is None:
        return tool_names

    for ns in exec_state.node_states.values():
        if ns.status == NodeStatus.SUCCESS and ns.artifact_refs:
            for ref in ns.artifact_refs:
                try:
                    artifact = await store.get(ref)
                    if artifact is not None and artifact.metadata:
                        name = artifact.metadata.get("tool_name")
                        if name and name not in tool_names:
                            tool_names.append(str(name))
                except Exception:
                    logger.debug(
                        "_collect_tools_used: artifact %s 读取失败", ref,
                        exc_info=True,
                    )
    return tool_names
