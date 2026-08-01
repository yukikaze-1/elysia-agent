"""WorkflowEngine 单元测试。

覆盖：
    - stop()  graceful shutdown（正常 / 超时 / 空转）
    - pause() 暂停入口（正常 / 不存在 / 已终态 / 已暂停 / 未启动）
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from Module.workflow_engine.schema import (
    AgentId,
    AgentInfo,
    AgentList,
    AgentStatus,
    AgentType,
    ConstraintConfig,
    ExecutionState,
    ExecutionPhase,
    NodeState,
    NodeStatus,
    NodeId,
    ResumeError,
    ResumeErrorCode,
    SubmitError,
    WorkflowId,
    WorkflowState,
    WorkflowStatus,
    TokenComponent,
    TokenComponentType,
    TokenPeriod,
    TokenStats,
    TokenUsage,
    now_iso,
)
from Module.workflow_engine.mailbox import WorkflowMailbox

pytestmark = pytest.mark.unit

# ── 工厂函数 ────────────────────────────────────────────────────────────────


def _make_task_state(task_id: str = "t1", status: WorkflowStatus = WorkflowStatus.RUNNING) -> WorkflowState:
    return WorkflowState(
        task_id=WorkflowId(task_id),
        status=status,
        created_at=now_iso(),
        updated_at=now_iso(),
    )


def _make_exec_state() -> ExecutionState:
    return ExecutionState(
        node_states={
            NodeId("a"): NodeState(
                node_id=NodeId("a"),
                status=NodeStatus.PENDING,
            ),
        },
        current_phase=ExecutionPhase.EXECUTING,
    )


def _make_mock_sub_agent(task_state, exec_state):
    """构造最小 mock SubAgent——暴露 _task_state / _orchestrator / _graph。"""
    sub_agent = MagicMock()
    sub_agent._task_state = task_state

    orch = MagicMock()
    orch._exec_state = exec_state
    sub_agent._orchestrator = orch

    graph = MagicMock()
    graph._graph = MagicMock()
    graph._graph.edges = []
    sub_agent._graph = graph

    return sub_agent


async def _build_task_system():
    """构造一个已 start 的 WorkflowEngine（mock 内部组件）。"""
    from Module.workflow_engine.workflow_engine import WorkflowEngine
    from Module.workflow_engine.infra import AgentInfra

    mock_llm = MagicMock()
    mock_llm.chat = MagicMock()
    mock_llm.chat.completions = MagicMock()
    mock_llm.chat.completions.create = AsyncMock()

    infra = AgentInfra(
        llm=mock_llm,
        milvus=None,
        http=None,
        tool_registry=MagicMock(),
        constraints=ConstraintConfig(),
    )
    with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}):
        ts = WorkflowEngine(infra=infra, hitl_notifier=MagicMock(), max_sub_agents=5)
        await ts.start()
    return ts


def _build_task_system_raw():
    """构造 WorkflowEngine + mock ToolManager，供 submit_approval 同步测试使用。"""
    from Module.workflow_engine.workflow_engine import WorkflowEngine
    from Module.workflow_engine.infra import AgentInfra

    tool_mgr = MagicMock()
    tool_mgr.submit_approval = MagicMock(return_value=True)

    mock_llm = MagicMock()
    mock_llm.chat = MagicMock()
    mock_llm.chat.completions = MagicMock()
    mock_llm.chat.completions.create = AsyncMock()

    infra = AgentInfra(
        llm=mock_llm,
        milvus=None,
        http=None,
        tool_registry=tool_mgr,
        constraints=ConstraintConfig(),
    )
    with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}):
        ts = WorkflowEngine(infra=infra, hitl_notifier=MagicMock(), max_sub_agents=5)
    return {"ts": ts, "tool_mgr": tool_mgr}


# ═══════════════════════════════════════════════════════════════════════════
# stop()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_stop_no_active_tasks():
    """无活跃任务时 stop() 直接返回。"""
    ts = await _build_task_system()
    await ts.stop()
    # 不抛异常 = 通过


@pytest.mark.asyncio
async def test_stop_waits_for_completion():
    """有活跃任务时 stop() 等待任务完成。"""
    ts = await _build_task_system()

    tid = WorkflowId("t1")
    task_state = _make_task_state("t1", WorkflowStatus.RUNNING)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)

    # 注入到 WorkflowEngine 内部注册表
    ts._tasks[tid] = sub_agent

    # 创建 fake asyncio.Task——立即完成的 coroutine
    async def _fast_complete():
        pass

    future = asyncio.create_task(_fast_complete())
    ts._task_futures[tid] = future
    await future  # 等它先完成，这样 stop 看到的就是已完成的 task

    # stop 应该立即返回（无 pending）
    await ts.stop(timeout=1.0)

    # 注册表应该已清理（兜底 clear）
    assert tid not in ts._tasks
    assert tid not in ts._task_futures


@pytest.mark.asyncio
async def test_stop_cancels_pending():
    """超时后 stop() 取消未完成的任务。"""
    ts = await _build_task_system()

    tid = WorkflowId("t1")
    task_state = _make_task_state("t1", WorkflowStatus.RUNNING)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)

    ts._tasks[tid] = sub_agent

    # 创建一个永不完成的 coroutine——模拟卡死的任务
    async def _never_complete():
        await asyncio.Event().wait()  # 永远不会 set

    future = asyncio.create_task(_never_complete())
    ts._task_futures[tid] = future

    # stop(timeout=0.1) —— 应该取消 future
    await ts.stop(timeout=0.1)

    # future 应该被取消
    assert future.cancelled() or future.done()

    # 兜底清理
    assert tid not in ts._tasks


@pytest.mark.asyncio
async def test_stop_rejects_new_submissions():
    """stop() 后 submit() 被拒绝。"""
    ts = await _build_task_system()
    await ts.stop()

    from Module.workflow_engine.schema import DelegationContext, SessionId, SubmitRejectionCode

    ctx = DelegationContext(intent="测试", session_id=SessionId("s1"))
    result = await ts.submit(ctx)

    assert isinstance(result, SubmitError)
    assert result.code == SubmitRejectionCode.NOT_READY


@pytest.mark.asyncio
async def test_stop_cleans_up_remaining_tasks():
    """stop() 兜底清理所有未清理的任务。"""
    ts = await _build_task_system()

    tid = WorkflowId("t1")
    task_state = _make_task_state("t1", WorkflowStatus.RUNNING)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)

    ts._tasks[tid] = sub_agent

    # 无 future——模拟 _task_futures 已 pop 但 _tasks 还残留的情况
    await ts.stop(timeout=0.1)

    # 兜底 clear 会清理 _tasks
    assert tid not in ts._tasks


# ═══════════════════════════════════════════════════════════════════════════
# pause()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_pause_sets_pause_requested():
    """正常暂停——设置 exec_state.pause_requested = True。"""
    ts = await _build_task_system()

    tid = WorkflowId("t1")
    task_state = _make_task_state("t1", WorkflowStatus.RUNNING)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)

    ts._tasks[tid] = sub_agent

    await ts.pause(tid)

    assert exec_state.pause_requested is True


@pytest.mark.asyncio
async def test_pause_missing_task_no_error():
    """不存在的任务 → 无操作（幂等——已清理即已暂停）。"""
    ts = await _build_task_system()
    await ts.pause(WorkflowId("nonexistent"))  # 不抛异常


@pytest.mark.asyncio
async def test_pause_terminal_task_no_error():
    """已终态任务 → 无操作（幂等）。"""
    ts = await _build_task_system()

    tid = WorkflowId("t1")
    task_state = _make_task_state("t1", WorkflowStatus.COMPLETED)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    await ts.pause(tid)  # 不抛异常
    # pause_requested 不应被设置
    assert exec_state.pause_requested is False


@pytest.mark.asyncio
async def test_pause_already_paused_no_error():
    """已暂停任务 → 无操作（幂等）。"""
    ts = await _build_task_system()

    tid = WorkflowId("t1")
    task_state = _make_task_state("t1", WorkflowStatus.PAUSED)
    exec_state = _make_exec_state()
    exec_state.pause_requested = True
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    await ts.pause(tid)  # 不抛异常


@pytest.mark.asyncio
async def test_pause_not_started_no_error():
    """尚未启动执行（_exec_state=None）→ 无操作。"""
    ts = await _build_task_system()

    tid = WorkflowId("t1")
    task_state = _make_task_state("t1", WorkflowStatus.RUNNING)
    sub_agent = MagicMock()
    sub_agent._task_state = task_state
    orch = MagicMock()
    orch._exec_state = None
    sub_agent._orchestrator = orch
    ts._tasks[tid] = sub_agent

    await ts.pause(tid)  # 不抛异常


# ═══════════════════════════════════════════════════════════════════════════
# pause() 超时确认
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_pause_confirmed():
    """pause() 在 orchestrator 确认 PAUSED 后返回 True（≤1s）。"""
    ts = await _build_task_system()
    tid = WorkflowId("t1")
    task_state = _make_task_state("t1", WorkflowStatus.RUNNING)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    # 模拟 orchestrator 在检测到 pause_requested 后切换状态
    async def simulate_orch():
        await asyncio.sleep(0.05)
        task_state.status = WorkflowStatus.PAUSED
    asyncio.create_task(simulate_orch())

    t0 = time.monotonic()
    result = await ts.pause(tid, timeout=5.0)
    elapsed = time.monotonic() - t0

    assert result is True
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_pause_timeout():
    """pause() 在 orchestrator 不响应时超时返回 False。"""
    ts = await _build_task_system()
    tid = WorkflowId("t2")
    task_state = _make_task_state("t2", WorkflowStatus.RUNNING)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    # 不切换状态——模拟 orchestrator 卡在长 LLM 调用中
    t0 = time.monotonic()
    result = await ts.pause(tid, timeout=0.5)
    elapsed = time.monotonic() - t0

    assert result is False
    assert elapsed >= 0.4   # 应等待接近 timeout


@pytest.mark.asyncio
async def test_pause_terminal_during_wait():
    """pause() 等待期间任务进入终态——提前返回 False。"""
    ts = await _build_task_system()
    tid = WorkflowId("t3")
    task_state = _make_task_state("t3", WorkflowStatus.RUNNING)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    # 模拟：orchestrator 在 pause 轮询期间完成执行
    async def delayed_complete():
        await asyncio.sleep(0.15)
        task_state.status = WorkflowStatus.COMPLETED
    asyncio.create_task(delayed_complete())

    t0 = time.monotonic()
    result = await ts.pause(tid, timeout=2.0)
    elapsed = time.monotonic() - t0

    assert result is False
    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_pause_idempotent():
    """对已 PAUSED 的任务——立即返回 True，不进入轮询。"""
    ts = await _build_task_system()
    tid = WorkflowId("t4")
    task_state = _make_task_state("t4", WorkflowStatus.PAUSED)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    t0 = time.monotonic()
    result = await ts.pause(tid, timeout=5.0)
    elapsed = time.monotonic() - t0

    assert result is True
    assert elapsed < 0.1


# ═══════════════════════════════════════════════════════════════════════════
# get_token_usage() / get_token_stats()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_token_usage_returns_none_for_missing_task():
    """不存在的 task_id 返回 None。"""
    ts = await _build_task_system()
    assert ts.get_token_usage(WorkflowId("nonexistent")) is None


@pytest.mark.asyncio
async def test_get_token_usage_returns_usage():
    """活跃任务的 token_usage 可通过查询 API 获取。"""
    ts = await _build_task_system()
    # 手动构造一个有 token 数据的 task_state
    task_state = _make_task_state("t1")
    task_state.token_entries.append(TokenComponent(
        component=TokenComponentType.PLANNER,
        prompt_tokens=100, completion_tokens=50, total_tokens=150, model="m",
    ))
    task_state.token_usage["planner"] = 150
    sub_agent = MagicMock()
    sub_agent._task_state = task_state
    ts._tasks[WorkflowId("t1")] = sub_agent

    usage = ts.get_token_usage(WorkflowId("t1"))
    assert usage is not None
    assert len(usage.components) == 1
    assert usage.components[0].total_tokens == 150


@pytest.mark.asyncio
async def test_get_token_stats_aggregates():
    """get_token_stats 跨任务聚合 token 数据。"""
    ts = await _build_task_system()
    task_state = _make_task_state("t1")
    task_state.created_at = "2026-06-16T10:00:00"
    task_state.token_entries.append(TokenComponent(
        component=TokenComponentType.PLANNER,
        prompt_tokens=100, completion_tokens=50, total_tokens=150, model="deepseek",
    ))
    task_state.token_usage["planner"] = 150
    sub_agent = MagicMock()
    sub_agent._task_state = task_state
    ts._tasks[WorkflowId("t1")] = sub_agent

    stats = ts.get_token_stats(TokenPeriod.ALL)
    assert stats.total_tokens == 150
    assert stats.by_component["planner"] == 150
    assert stats.by_model["deepseek"] == 150


@pytest.mark.asyncio
async def test_get_token_stats_filters_by_period():
    """TODAY period 过滤掉昨天的任务。"""
    from datetime import datetime, timezone, timedelta

    ts = await _build_task_system()

    # 昨天的任务
    task_state = _make_task_state("old")
    task_state.created_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    task_state.token_entries.append(TokenComponent(
        component=TokenComponentType.PLANNER,
        prompt_tokens=10, completion_tokens=5, total_tokens=15, model="m",
    ))
    task_state.token_usage["planner"] = 15
    sub_agent = MagicMock()
    sub_agent._task_state = task_state
    ts._tasks[WorkflowId("old")] = sub_agent

    stats = ts.get_token_stats(TokenPeriod.TODAY)
    assert stats.total_tokens == 0  # 昨天的被过滤


# ═══════════════════════════════════════════════════════════════════════════
# get_task_status()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_task_status_returns_status():
    """活跃任务——返回当前状态。"""
    ts = await _build_task_system()

    tid = WorkflowId("t1")
    task_state = _make_task_state("t1", WorkflowStatus.RUNNING)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    assert ts.get_task_status(tid) == WorkflowStatus.RUNNING


@pytest.mark.asyncio
async def test_get_task_status_missing_task():
    """任务不存在或已清理 → None。"""
    ts = await _build_task_system()

    assert ts.get_task_status(WorkflowId("nonexistent")) is None


@pytest.mark.asyncio
async def test_get_task_status_pending_before_init():
    """SubAgent.run() 尚未初始化 _task_state → 返回 PENDING。"""
    ts = await _build_task_system()

    tid = WorkflowId("t1")
    sub_agent = MagicMock()
    sub_agent._task_state = None  # 尚未初始化
    ts._tasks[tid] = sub_agent

    assert ts.get_task_status(tid) == WorkflowStatus.PENDING


# ═══════════════════════════════════════════════════════════════════════════
# get_progress()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_progress_active_task():
    """活跃任务——返回完整进度快照。"""
    ts = await _build_task_system()

    tid = WorkflowId("t1")
    task_state = _make_task_state("t1", WorkflowStatus.RUNNING)
    exec_state = ExecutionState(
        node_states={
            NodeId("a"): NodeState(
                node_id=NodeId("a"),
                status=NodeStatus.SUCCESS,
            ),
            NodeId("b"): NodeState(
                node_id=NodeId("b"),
                status=NodeStatus.RUNNING,
            ),
            NodeId("c"): NodeState(
                node_id=NodeId("c"),
                status=NodeStatus.PENDING,
            ),
        },
        current_phase=ExecutionPhase.EXECUTING,
    )
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    progress = ts.get_progress(tid)
    assert progress is not None
    assert progress.task_id == tid
    assert progress.status == WorkflowStatus.RUNNING
    assert progress.completed_nodes == 1
    assert progress.total_nodes == 3
    assert progress.current_node_id == NodeId("b")
    assert progress.elapsed_ms >= 0
    # 1 节点完成, 3 total → 预估剩余 = elapsed / 1 * 2
    if progress.elapsed_ms > 0:
        assert progress.estimated_remaining_ms is not None
        assert progress.estimated_remaining_ms >= 0


@pytest.mark.asyncio
async def test_get_progress_missing_task():
    """任务不存在 → None。"""
    ts = await _build_task_system()

    assert ts.get_progress(WorkflowId("nonexistent")) is None


@pytest.mark.asyncio
async def test_get_progress_no_exec_state():
    """尚未进入 Orchestrator（_exec_state=None）→ 基础进度，无节点统计。"""
    ts = await _build_task_system()

    tid = WorkflowId("t1")
    task_state = _make_task_state("t1", WorkflowStatus.PLANNING)

    # exec_state 为 None
    sub_agent = MagicMock()
    sub_agent._task_state = task_state
    orch = MagicMock()
    orch._exec_state = None
    sub_agent._orchestrator = orch
    ts._tasks[tid] = sub_agent

    progress = ts.get_progress(tid)
    assert progress is not None
    assert progress.task_id == tid
    assert progress.status == WorkflowStatus.PLANNING
    assert progress.total_nodes == 0  # 无 plan，默认 0
    assert progress.completed_nodes == 0
    assert progress.current_node_id is None


@pytest.mark.asyncio
async def test_get_progress_no_estimation_when_none_done():
    """所有节点 PENDING——无节点完成时预估为 None。"""
    ts = await _build_task_system()

    tid = WorkflowId("t1")
    task_state = _make_task_state("t1", WorkflowStatus.RUNNING)
    exec_state = ExecutionState(
        node_states={
            NodeId("a"): NodeState(
                node_id=NodeId("a"),
                status=NodeStatus.PENDING,
            ),
        },
        current_phase=ExecutionPhase.EXECUTING,
    )
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    progress = ts.get_progress(tid)
    assert progress is not None
    assert progress.completed_nodes == 0
    assert progress.total_nodes == 1
    # 没完成任何节点 → 无法估算
    assert progress.estimated_remaining_ms is None


# ═══════════════════════════════════════════════════════════════════════════
# list_agents()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_agents_empty():
    """无活跃任务时——sub_agents 为空，capacity 正确。"""
    ts = await _build_task_system()

    agents = ts.list_agents()
    assert isinstance(agents, AgentList)
    assert len(agents.sub_agents) == 0
    assert len(agents.worker_agents) == 0  # v1 无 Worker
    assert agents.total_capacity == 5
    assert agents.available_slots == 5


@pytest.mark.asyncio
async def test_list_agents_with_tasks():
    """有活跃任务时——每个任务对应一个 AgentInfo。"""
    ts = await _build_task_system()

    tid1 = WorkflowId("t1")
    task_state1 = _make_task_state("t1", WorkflowStatus.RUNNING)
    exec_state1 = _make_exec_state()
    sub_agent1 = _make_mock_sub_agent(task_state1, exec_state1)
    ts._tasks[tid1] = sub_agent1

    tid2 = WorkflowId("t2")
    task_state2 = _make_task_state("t2", WorkflowStatus.PAUSED)
    exec_state2 = _make_exec_state()
    sub_agent2 = _make_mock_sub_agent(task_state2, exec_state2)
    ts._tasks[tid2] = sub_agent2

    agents = ts.list_agents()
    assert len(agents.sub_agents) == 2
    assert agents.available_slots == 3  # 5 - 2

    # 按 task_id 排序验证
    agents_sorted = sorted(agents.sub_agents, key=lambda a: str(a.task_id))
    assert agents_sorted[0].task_id == tid1
    assert agents_sorted[0].agent_type == AgentType.SUB
    assert agents_sorted[0].status == AgentStatus.RUNNING

    assert agents_sorted[1].task_id == tid2
    assert agents_sorted[1].status == AgentStatus.PAUSED


@pytest.mark.asyncio
async def test_list_agents_task_state_none():
    """_task_state=None（尚未初始化）→ RUNNING（默认）。"""
    ts = await _build_task_system()

    tid = WorkflowId("t1")
    sub_agent = MagicMock()
    sub_agent._task_state = None
    ts._tasks[tid] = sub_agent

    agents = ts.list_agents()
    assert len(agents.sub_agents) == 1
    assert agents.sub_agents[0].status == AgentStatus.RUNNING


@pytest.mark.asyncio
async def test_list_agents_waiting_approval():
    """WAITING_APPROVAL → AgentStatus.AWAITING_APPROVAL。"""
    ts = await _build_task_system()

    tid = WorkflowId("t1")
    task_state = _make_task_state("t1", WorkflowStatus.WAITING_APPROVAL)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    agents = ts.list_agents()
    assert len(agents.sub_agents) == 1
    assert agents.sub_agents[0].status == AgentStatus.AWAITING_APPROVAL


# ═══════════════════════════════════════════════════════════════════════════
# cancel()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cancel_missing_task_returns_true():
    """不存在的 task_id → True（幂等——已清理 = 已取消）。"""
    ts = await _build_task_system()
    assert await ts.cancel(WorkflowId("nonexistent")) is True


@pytest.mark.asyncio
async def test_cancel_terminal_task_returns_true():
    """已终态任务 → True（幂等——已完成/已失败 = 已取消）。"""
    ts = await _build_task_system()

    tid = WorkflowId("t1")
    task_state = _make_task_state("t1", WorkflowStatus.COMPLETED)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    assert await ts.cancel(tid) is True
    # 状态不变
    assert task_state.status == WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_cancel_running_sets_cancel_requested():
    """RUNNING 任务 → 设 exec_state.cancel_requested=True，不立即改 status。"""
    ts = await _build_task_system()

    tid = WorkflowId("t1")
    task_state = _make_task_state("t1", WorkflowStatus.RUNNING)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    assert await ts.cancel(tid) is True
    assert exec_state.cancel_requested is True
    # RUNNING → 状态不变（由 Orchestrator 改为 CANCELLED）
    assert task_state.status == WorkflowStatus.RUNNING


@pytest.mark.asyncio
async def test_cancel_paused_sets_cancelled_directly():
    """PAUSED 任务 → 直接 CANCELLED + 显式清理（不依赖 _run_and_cleanup）。"""
    ts = await _build_task_system()

    tid = WorkflowId("t1")
    task_state = _make_task_state("t1", WorkflowStatus.PAUSED)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    # PAUSED→CANCELED 分支需要 AsyncMock checkpoint/artifact_store
    sub_agent._checkpoint_mgr = MagicMock()
    sub_agent._checkpoint_mgr.delete = AsyncMock()
    sub_agent._artifact_store = MagicMock()
    sub_agent._artifact_store.delete_by_task = AsyncMock()

    # BoundedSemaphore 需要至少 1 个槽位被占用才能 release
    # _build_task_system 创建了 max=5 的 semaphore，当前无占用（值=5）
    # cancel 会 release → 从 5 到 6 超上限
    # 模拟占用一个槽位：acquire 后 release 就会回到 5
    await ts._semaphore.acquire()  # type: ignore[reportOptionalMemberAccess]

    assert await ts.cancel(tid) is True
    assert task_state.status == WorkflowStatus.CANCELLED
    # 验证清理被调用
    sub_agent._checkpoint_mgr.delete.assert_called_once()


@pytest.mark.asyncio
async def test_cancel_before_orchestrator_sets_cancelled():
    """尚未进入 Orchestrator（_exec_state=None）→ 直接 CANCELLED。"""
    ts = await _build_task_system()

    tid = WorkflowId("t1")
    task_state = _make_task_state("t1", WorkflowStatus.PLANNING)

    # exec_state 为 None（尚未 run）
    sub_agent = MagicMock()
    sub_agent._task_state = task_state
    orch = MagicMock()
    orch._exec_state = None
    sub_agent._orchestrator = orch
    ts._tasks[tid] = sub_agent

    assert await ts.cancel(tid) is True
    assert task_state.status == WorkflowStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_task_state_none_returns_true():
    """_task_state=None（尚未初始化）→ True（幂等）。"""
    ts = await _build_task_system()

    tid = WorkflowId("t1")
    sub_agent = MagicMock()
    sub_agent._task_state = None
    ts._tasks[tid] = sub_agent

    assert await ts.cancel(tid) is True


# ═══════════════════════════════════════════════════════════════════════════
# resume()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_resume_missing_task_returns_error():
    """不存在的 task_id → ResumeError(CHECKPOINT_NOT_FOUND)。"""
    ts = await _build_task_system()
    result = await ts.resume(WorkflowId("nonexistent"))
    assert isinstance(result, ResumeError)
    assert result.code == ResumeErrorCode.CHECKPOINT_NOT_FOUND


@pytest.mark.asyncio
async def test_resume_terminal_task_returns_error():
    """已终态任务 → ResumeError(INVALID_STATE)。"""
    ts = await _build_task_system()

    tid = WorkflowId("t1")
    task_state = _make_task_state("t1", WorkflowStatus.COMPLETED)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    result = await ts.resume(tid)
    assert isinstance(result, ResumeError)
    assert result.code == ResumeErrorCode.INVALID_STATE


@pytest.mark.asyncio
async def test_resume_running_is_idempotent():
    """RUNNING 任务 → 直接返回 mailbox（幂等）。"""
    ts = await _build_task_system()

    tid = WorkflowId("t1")
    task_state = _make_task_state("t1", WorkflowStatus.RUNNING)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent
    ts._mailboxes[tid] = WorkflowMailbox(tid)

    result = await ts.resume(tid)
    # 幂等——返回 mailbox 而非 ResumeError
    assert isinstance(result, WorkflowMailbox)


@pytest.mark.asyncio
async def test_resume_not_paused_returns_error():
    """非 PAUSED/非 RUNNING 状态（PLANNING）→ ResumeError。"""
    ts = await _build_task_system()

    tid = WorkflowId("t1")
    task_state = _make_task_state("t1", WorkflowStatus.PLANNING)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    result = await ts.resume(tid)
    assert isinstance(result, ResumeError)
    assert result.code == ResumeErrorCode.INVALID_STATE


@pytest.mark.asyncio
async def test_resume_paused_returns_mailbox():
    """PAUSED 任务 → 返回 mailbox（恢复任务异步创建）。"""
    ts = await _build_task_system()

    tid = WorkflowId("t1")
    task_state = _make_task_state("t1", WorkflowStatus.PAUSED)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent
    ts._mailboxes[tid] = WorkflowMailbox(tid)

    result = await ts.resume(tid)
    assert isinstance(result, WorkflowMailbox)
    # resume() 返回后恢复任务已创建
    assert tid in ts._task_futures


# ═══════════════════════════════════════════════════════════════════════════
# PAUSED 查询可见性
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_paused_task_visible_in_query_apis():
    """PAUSED 任务在查询 API 中可见——注册表未清理。

    验证 pause+cleanup guard 正确工作——PAUSED 不被清理，
    get_task_status / list_active_tasks / list_agents 都能看到。
    """
    ts = await _build_task_system()

    tid = WorkflowId("t1")
    task_state = _make_task_state("t1", WorkflowStatus.PAUSED)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)

    # 设置 plan 使其在 list_active_tasks 中可见
    from Module.workflow_engine.schema import DAG, DAGNode, NodeId, Plan
    task_state.plan = Plan(
        dag=DAG(nodes=[DAGNode(
            node_id=NodeId("n1"),
            description="获取时间",
            output_as="output:time",  # type: ignore[reportArgumentType]
        )]),
        plan_version=1,
    )
    ts._tasks[tid] = sub_agent

    # list_active_tasks 会 await checkpoint_mgr.load()，需要 AsyncMock
    sub_agent._checkpoint_mgr = MagicMock()
    sub_agent._checkpoint_mgr.load = AsyncMock(return_value=None)

    # get_task_status 可见
    assert ts.get_task_status(tid) == WorkflowStatus.PAUSED

    # list_active_tasks 可见
    summaries = await ts.list_active_tasks()
    paused_summaries = [s for s in summaries if s.task_id == tid]
    assert len(paused_summaries) == 1, \
        f"PAUSED 任务应在活跃列表中，实际: {[s.task_id for s in summaries]}"
    assert paused_summaries[0].status == WorkflowStatus.PAUSED

    # list_agents 可见
    agents = ts.list_agents()
    paused_agents = [a for a in agents.sub_agents if a.task_id == tid]
    assert len(paused_agents) == 1
    assert paused_agents[0].status == AgentStatus.PAUSED


# ═══════════════════════════════════════════════════════════════════════════
# submit_approval()
# ═══════════════════════════════════════════════════════════════════════════


def test_submit_approval_delegates_to_tool_manager():
    """submit_approval 将 agent_id 作为 request_id 委托给 ToolManager。"""
    ts_wrapper = _build_task_system_raw()
    tool_mgr = ts_wrapper["tool_mgr"]
    ts = ts_wrapper["ts"]

    # 预设 SafetyPipeline 中有等待的审批
    tool_mgr.submit_approval = MagicMock(return_value=True)

    result = ts.submit_approval(
        task_id=WorkflowId("t1"),
        agent_id=AgentId("req-001"),
        approved=True,
    )
    assert result is True
    tool_mgr.submit_approval.assert_called_once_with(
        request_id="req-001",
        approved=True,
    )


def test_submit_approval_returns_false_when_not_found():
    """审批请求不存在 → 返回 False。"""
    ts_wrapper = _build_task_system_raw()
    tool_mgr = ts_wrapper["tool_mgr"]
    ts = ts_wrapper["ts"]

    tool_mgr.submit_approval = MagicMock(return_value=False)

    result = ts.submit_approval(
        task_id=WorkflowId("t1"),
        agent_id=AgentId("unknown"),
        approved=True,
    )
    assert result is False


def test_submit_approval_no_tool_manager():
    """tool_registry 没有 submit_approval 方法 → 返回 False。"""
    ts_wrapper = _build_task_system_raw()
    ts = ts_wrapper["ts"]

    # 替换为没有 submit_approval 的对象
    ts._infra.tool_registry = object()

    result = ts.submit_approval(
        task_id=WorkflowId("t1"),
        agent_id=AgentId("req-001"),
        approved=True,
    )
    assert result is False
