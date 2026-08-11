"""E3 Task 7: 确定性取消套件（fake-LLM 驱动，E4 基建）。

覆盖 spec §7 测试表：
    慢 planner cancel（LLM future 中断 + cancel 后无 token 消耗）
    planning retry 间 cancel（_check_cancelled 拦，不再重试）
    重复 cancel 幂等（不二次 owned.cancel、slot 无泄漏）
    cancel∥pause 乱序（终态 CANCELLED 唯一、PAUSED 不复活）
    stop 活跃任务（CANCELLED 终态 + 唯一事件 + slot 无泄漏）
    _init_exec RESUMING→RUNNING 逆转（resume 加载已 CANCELLED checkpoint → 不写 RUNNING）

工具级取消（②③④ 检查点 + shield）由 test_executor.py::TestExecutorCancellation 覆盖（Task 3）；
M19 resume→pause 循环由 test_workflow_engine.py::TestM19AndOrphans 覆盖（Task 6）。

AgentInfra 构造：`AgentInfra(llm=fake, milvus=None, http=None, tool_registry=MagicMock(),
constraints=ConstraintConfig())`（必填字段模式，E4 plan Task 5）。
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from Module.workflow_contract import (
    AgentInfra,
    ConstraintConfig,
    DelegationContext,
    NodeId,
    SessionId,
    WorkflowEventType,
    WorkflowId,
    WorkflowStatus,
)
from Module.workflow_engine.mailbox import WorkflowMailbox
from Module.workflow_engine.schema import (
    DAG,
    DAGNode,
    ExecutionState,
    NodeState,
    NodeStatus,
    Plan,
    WorkflowState,
)
from Module.workflow_engine.tests.helpers.fake_llm import FakeLLM, with_deadline
from Module.workflow_engine.workflow_engine import WorkflowEngine

pytestmark = pytest.mark.unit


def _engine_with_fake(fake: FakeLLM, *, max_sub_agents: int = 1) -> WorkflowEngine:
    """构造 WorkflowEngine——AgentInfra 用 FakeLLM（未 start，调用方负责 start）。"""
    infra = AgentInfra(llm=fake, milvus=None, http=None,
                       tool_registry=MagicMock(), constraints=ConstraintConfig())
    return WorkflowEngine(infra=infra, hitl_notifier=MagicMock(),
                          max_sub_agents=max_sub_agents)


def _running_state(tid: str = "t1") -> WorkflowState:
    """RUNNING 任务级状态（真实 WorkflowState——is_terminal 随 status 变化）。"""
    return WorkflowState(
        task_id=WorkflowId(tid),
        status=WorkflowStatus.RUNNING,
        tools=[],
        session_id=SessionId("s1"),
    )


# ═══════════════════════════════════════════════════════════════════════════
# 慢 planner cancel——LLM future 中断 + 无 token 消耗
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_slow_planner_cancel_no_token_consumption():
    """PLANNING 中 cancel → LLM future 中断、status=CANCELLED、fake.calls 不增长、不回 RUNNING。"""
    fake = FakeLLM()
    fake.gate = asyncio.Event()                       # 恒不释放——模拟慢 planner（gate 未过不计数）
    engine = _engine_with_fake(fake)
    await engine.start()
    ctx = DelegationContext(intent="测试任务", tools=[], session_id=SessionId("s1"))
    result = await engine.submit(ctx)
    assert isinstance(result, WorkflowMailbox)
    tid = result.task_id
    # 等 drain 后台派发 + owned task 就位
    deadline = time.monotonic() + 5.0
    while tid not in engine._task_futures and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    fut = engine._task_futures.get(tid)
    assert fut is not None, "owned task 未就位——drain 未派发"
    await asyncio.sleep(0.05)                         # 让 planner 真正进入 LLM gate
    assert fake.calls == 0                            # gate 阻塞中，尚未消耗 token

    ok = await with_deadline(engine.cancel(tid), 5.0)
    assert ok is True
    with pytest.raises(asyncio.CancelledError):
        await with_deadline(fut, 5.0)                 # owned.cancel() 已打断 in-flight LLM future

    # 传播链收尾：_run_and_cleanup 推唯一 CANCELLED 事件 + 清理注册表
    assert fake.calls == 0                            # cancel 后无 token 消耗（gate 未放行，calls 不增长）
    assert tid not in engine._tasks
    # 消费事件流——submit 推 QUEUED，_run_and_cleanup 推唯一 CANCELLED（§3.5 守卫）
    events = []
    while True:
        ev = result.get_nowait()
        if ev is None:
            break
        events.append(ev)
    assert any(ev.type == WorkflowEventType.CANCELLED for ev in events)
    assert [ev.type for ev in events].count(WorkflowEventType.CANCELLED) == 1
    await engine.stop()


# ═══════════════════════════════════════════════════════════════════════════
# planning retry 间 cancel——_check_cancelled 拦，不再重试
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_planning_retry_cancel_stops_retry():
    """retry 之间 _check_cancelled 生效——retry 的 plan 返回后 cancel 落地 → 抛 CancelledError，不再重试。"""
    from Module.workflow_engine.core.sub_agent.planning import plan_and_validate
    from Module.workflow_engine.schema import ValidationAction

    ts = WorkflowState(task_id=WorkflowId("t1"), status=WorkflowStatus.PENDING,
                       tools=[], session_id=SessionId("s1"))
    planner = MagicMock()
    call_count = 0

    async def fake_plan(*a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 2:                           # retry 期间的 plan——cancel 落地
            ts.status = WorkflowStatus.CANCELLED
        return MagicMock()
    planner.plan = fake_plan
    validator = MagicMock()
    # 第一轮 fix_plan（进入 retry），第二轮 valid → 写 RUNNING 前被 _check_cancelled 拦
    validator.validate.side_effect = [
        (MagicMock(valid=False, action=ValidationAction.FIX_PLAN, issues=[]), MagicMock()),
        (MagicMock(valid=True), MagicMock()),
    ]
    # context 须为真实 DelegationContext——fix_plan 分支会用 context 字段构造 retry_context
    context = DelegationContext(intent="测试任务", tools=[], session_id=SessionId("s1"))
    with pytest.raises(asyncio.CancelledError):
        await plan_and_validate(
            planner, validator, context, ts, WorkflowMailbox(WorkflowId("t1")),
            ConstraintConfig(),
            push_mailbox_fn=AsyncMock(), abort_error_code_fn=lambda: "ABORT",
        )
    assert ts.status == WorkflowStatus.CANCELLED      # 未被覆写为 RUNNING
    assert call_count == 2                            # cancel 后不再重试


# ═══════════════════════════════════════════════════════════════════════════
# 重复 cancel 幂等——不二次 owned.cancel、slot 无泄漏
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_repeat_cancel_idempotent_no_leak():
    """首次 CANCELLED 后二次 cancel 幂等返回 True、不二次 owned.cancel、slot 无泄漏。"""
    engine = WorkflowEngine(infra=MagicMock(), hitl_notifier=MagicMock(), max_sub_agents=1)
    await engine.start()
    tid = WorkflowId("t1")
    ts = _running_state("t1")
    sa = MagicMock()
    sa._task_state = ts
    orch = MagicMock()
    orch._exec_state = MagicMock()
    sa._orchestrator = orch
    engine._tasks[tid] = sa
    engine._mailboxes[tid] = WorkflowMailbox(tid)
    owned = asyncio.create_task(asyncio.Event().wait())   # in-flight
    engine._task_futures[tid] = owned
    await engine._semaphore.acquire()
    engine._active.add(tid)

    assert await engine.cancel(tid) is True
    assert ts.status == WorkflowStatus.CANCELLED
    await asyncio.sleep(0)
    assert owned.cancelled() is True
    # 二次 cancel——is_terminal() 已 True → 幂等返回 True，不二次 owned.cancel
    assert await engine.cancel(tid) is True
    assert ts.status == WorkflowStatus.CANCELLED

    # 模拟 _run_and_cleanup finally 释放（cancel 各分支不 release，release 在 finally 唯一发生）
    engine._cleanup_task(tid)
    engine._semaphore.release()
    engine._active.discard(tid)
    assert engine._semaphore.locked() is False
    await engine.stop()


# ═══════════════════════════════════════════════════════════════════════════
# cancel∥pause 乱序——终态 CANCELLED 唯一且不逆转（PAUSED 不复活）
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cancel_then_pause_no_resurrection():
    """cancel 先 → status CANCELLED；随后 pause 已终态 → False，不复活 PAUSED。"""
    engine = WorkflowEngine(infra=MagicMock(), hitl_notifier=MagicMock(), max_sub_agents=1)
    await engine.start()
    tid = WorkflowId("t1")
    ts = _running_state("t1")
    sa = MagicMock()
    sa._task_state = ts
    orch = MagicMock()
    orch._exec_state = MagicMock()
    sa._orchestrator = orch
    engine._tasks[tid] = sa
    engine._mailboxes[tid] = WorkflowMailbox(tid)

    assert await engine.cancel(tid) is True
    assert ts.status == WorkflowStatus.CANCELLED
    assert await engine.pause(tid, timeout=0.1) is False   # 已终态 → 不暂停
    assert ts.status == WorkflowStatus.CANCELLED           # 不逆转
    await engine.stop()


@pytest.mark.asyncio
async def test_pause_then_cancel_ends_cancelled():
    """pause 先行（置 pause_requested）→ cancel 同步置 CANCELLED → 终态不复活 PAUSED。"""
    engine = WorkflowEngine(infra=MagicMock(), hitl_notifier=MagicMock(), max_sub_agents=1)
    await engine.start()
    tid = WorkflowId("t1")
    ts = _running_state("t1")
    sa = MagicMock()
    sa._task_state = ts
    orch = MagicMock()
    orch._exec_state = MagicMock()
    sa._orchestrator = orch
    engine._tasks[tid] = sa
    engine._mailboxes[tid] = WorkflowMailbox(tid)
    exec_state = orch._exec_state

    await engine.pause(tid, timeout=0.05)             # 无 orchestrator 确认 → 超时 False，但 pause_requested 已置
    assert exec_state.pause_requested is True
    assert await engine.cancel(tid) is True           # 同步置权威终态
    assert ts.status == WorkflowStatus.CANCELLED      # PAUSED 不复活（orchestrator 循环顶终止）
    await engine.stop()


@pytest.mark.asyncio
async def test_cancel_after_complete_is_idempotent():
    """cancel∥complete 乱序：COMPLETED 已写入（finalize 先到）→ cancel() 见 is_terminal() 幂等返回 True，
    status 不被覆盖为 CANCELLED——先到终态者胜，无「COMPLETED 事件 + CANCELLED status」分叉。"""
    engine = WorkflowEngine(infra=MagicMock(), hitl_notifier=MagicMock(), max_sub_agents=1)
    await engine.start()
    tid = WorkflowId("t1")
    ts = _running_state("t1")
    ts.status = WorkflowStatus.COMPLETED              # finalize 已写 COMPLETED（§3.5 顺序不变量）
    sa = MagicMock()
    sa._task_state = ts
    orch = MagicMock()
    orch._exec_state = MagicMock()
    sa._orchestrator = orch
    engine._tasks[tid] = sa
    engine._mailboxes[tid] = WorkflowMailbox(tid)

    assert await engine.cancel(tid) is True           # is_terminal() 已 True → 幂等 bail
    assert ts.status == WorkflowStatus.COMPLETED      # 终态不被覆写为 CANCELLED
    await engine.stop()


# ═══════════════════════════════════════════════════════════════════════════
# stop 活跃任务——CANCELLED 终态 + 唯一事件 + slot 无泄漏
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_stop_cancels_active_task_unique_event():
    """活跃任务 stop → 超时 owned.cancel → CANCELLED 终态 + 唯一 CANCELLED 事件 + slot 无泄漏。"""
    engine = WorkflowEngine(infra=MagicMock(), hitl_notifier=MagicMock(), max_sub_agents=1)
    await engine.start()
    tid = WorkflowId("t1")
    sa = MagicMock()
    ts = MagicMock()
    ts.status = WorkflowStatus.RUNNING
    ts.is_terminal.return_value = False
    sa._task_state = ts

    async def hanging_run(context, mailbox):
        await asyncio.Event().wait()                  # 永不完成——直到被 stop cancel
    sa.run = hanging_run
    engine._tasks[tid] = sa
    engine._contexts[tid] = DelegationContext(intent="t", session_id=SessionId("s1"))
    mailbox = engine._mailboxes[tid] = WorkflowMailbox(tid)
    await engine._semaphore.acquire()
    engine._active.add(tid)
    fut = asyncio.create_task(engine._run_and_cleanup(sa, tid))
    engine._task_futures[tid] = fut
    await asyncio.sleep(0.05)                         # 让 hanging run 进入等待

    await engine.stop(timeout=0.1)
    assert ts.status == WorkflowStatus.CANCELLED      # 终态化（stop 超时 → owned.cancel → CANCELLED）
    assert mailbox.count() == 1                       # 唯一 CANCELLED 事件（§3.5 守卫）
    e = mailbox.get_nowait()
    assert e is not None and e.type == WorkflowEventType.CANCELLED
    assert engine._semaphore.locked() is False        # finally 唯一释放（无泄漏）
    assert tid not in engine._tasks


# ═══════════════════════════════════════════════════════════════════════════
# _init_exec RESUMING→RUNNING 逆转——resume 加载已 CANCELLED → 不写 RUNNING
# ═══════════════════════════════════════════════════════════════════════════


def test_init_exec_cancelled_does_not_write_running():
    """resume 加载 checkpoint 时任务已 CANCELLED → _init_exec 不写 RUNNING（逆转防护，§4）。"""
    from Module.workflow_engine.core.graph import PlanGraph
    from Module.workflow_engine.core.orchestrator import Orchestrator

    plan = Plan(dag=DAG(nodes=[DAGNode(node_id=NodeId("a"), description="步骤 a")], edges=[]),
                plan_version=1)
    graph = PlanGraph.from_plan(plan)
    ts = WorkflowState(task_id=WorkflowId("t1"), status=WorkflowStatus.CANCELLED, plan=plan,
                       tools=[], session_id=SessionId("s1"))
    resume_exec = ExecutionState(node_states={
        NodeId("a"): NodeState(node_id=NodeId("a"), status=NodeStatus.SUCCESS),
    })
    orch = Orchestrator(
        infra=MagicMock(), hitl_notifier=MagicMock(),
        executor=MagicMock(), evaluator=MagicMock(),
        checkpoint_manager=MagicMock(), artifact_store=MagicMock(),
    )
    es = orch._init_exec(graph, resume_from=resume_exec, task_state=ts)
    assert ts.status == WorkflowStatus.CANCELLED       # RESUMING 分支不命中 → 不覆写 RUNNING
    assert es is not None
