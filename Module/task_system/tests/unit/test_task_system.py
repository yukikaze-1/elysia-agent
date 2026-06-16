"""TaskSystem 单元测试。

覆盖：
    - stop()  graceful shutdown（正常 / 超时 / 空转）
    - pause() 暂停入口（正常 / 不存在 / 已终态 / 已暂停 / 未启动）
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from Module.task_system.schema import (
    ConstraintConfig,
    ExecutionState,
    ExecutionPhase,
    NodeState,
    NodeStatus,
    NodeId,
    TaskId,
    TaskState,
    TaskStatus,
    now_iso,
)


# ── 工厂函数 ────────────────────────────────────────────────────────────────


def _make_task_state(task_id: str = "t1", status: TaskStatus = TaskStatus.RUNNING) -> TaskState:
    return TaskState(
        task_id=TaskId(task_id),
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
    """构造一个已 start 的 TaskSystem（mock 内部组件）。"""
    from Module.task_system.task_system import TaskSystem
    from Module.task_system.infra import AgentInfra

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
        ts = TaskSystem(infra=infra, hitl_notifier=MagicMock(), max_sub_agents=5)
        await ts.start()
    return ts


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

    tid = TaskId("t1")
    task_state = _make_task_state("t1", TaskStatus.RUNNING)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)

    # 注入到 TaskSystem 内部注册表
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

    tid = TaskId("t1")
    task_state = _make_task_state("t1", TaskStatus.RUNNING)
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

    from Module.task_system.schema import DelegationContext, SessionId, SubmitRejectionCode

    ctx = DelegationContext(intent="测试", session_id=SessionId("s1"))
    result = await ts.submit(ctx)

    assert hasattr(result, 'code')
    assert result.code == SubmitRejectionCode.NOT_READY


@pytest.mark.asyncio
async def test_stop_cleans_up_remaining_tasks():
    """stop() 兜底清理所有未清理的任务。"""
    ts = await _build_task_system()

    tid = TaskId("t1")
    task_state = _make_task_state("t1", TaskStatus.RUNNING)
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

    tid = TaskId("t1")
    task_state = _make_task_state("t1", TaskStatus.RUNNING)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)

    ts._tasks[tid] = sub_agent

    await ts.pause(tid)

    assert exec_state.pause_requested is True


@pytest.mark.asyncio
async def test_pause_raises_keyerror_for_missing_task():
    """暂停不存在的任务 → KeyError。"""
    ts = await _build_task_system()

    with pytest.raises(KeyError, match="不存在"):
        await ts.pause(TaskId("nonexistent"))


@pytest.mark.asyncio
async def test_pause_raises_for_terminal_task():
    """暂停已终态的任务 → RuntimeError。"""
    ts = await _build_task_system()

    tid = TaskId("t1")
    task_state = _make_task_state("t1", TaskStatus.COMPLETED)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)

    ts._tasks[tid] = sub_agent

    with pytest.raises(RuntimeError, match="已终态"):
        await ts.pause(tid)


@pytest.mark.asyncio
async def test_pause_raises_for_already_paused():
    """暂停已暂停的任务 → RuntimeError。"""
    ts = await _build_task_system()

    tid = TaskId("t1")
    task_state = _make_task_state("t1", TaskStatus.PAUSED)
    exec_state = _make_exec_state()
    exec_state.pause_requested = True  # 模拟已暂停
    sub_agent = _make_mock_sub_agent(task_state, exec_state)

    ts._tasks[tid] = sub_agent

    with pytest.raises(RuntimeError, match="已经暂停"):
        await ts.pause(tid)


@pytest.mark.asyncio
async def test_pause_raises_for_not_started():
    """暂停尚未启动执行的任务 → RuntimeError。"""
    ts = await _build_task_system()

    tid = TaskId("t1")
    task_state = _make_task_state("t1", TaskStatus.RUNNING)
    # sub_agent 没有 _orchestrator——模拟 Orchestrator 尚未 run()
    sub_agent = MagicMock()
    sub_agent._task_state = task_state
    # 不设 _orchestrator 属性
    del sub_agent._orchestrator  # 确保 AttributeError

    ts._tasks[tid] = sub_agent

    # 需要给 sub_agent 加一个 _orchestrator 不存在的情况
    # MagicMock 默认对任何属性访问返回新的 MagicMock，所以需要用 spec 或手动处理
    # 这里换个方式：直接构造一个没有 _exec_state 的 orchestrator

    # 重新构造：有 orchestrator 但没有 _exec_state
    sub_agent2 = MagicMock()
    sub_agent2._task_state = task_state
    orch = MagicMock(spec=[])  # spec=[] 意味着没有任何属性
    sub_agent2._orchestrator = orch
    ts._tasks[tid] = sub_agent2

    with pytest.raises(RuntimeError, match="尚未开始执行"):
        await ts.pause(tid)
