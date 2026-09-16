"""Batch 1.7 — 后台任务终态闭环（Invariant T1 / T2 / T3 / T4）。

**原缺陷**：`SubAgent._plan_and_validate` 委托的 `planning.plan_and_validate` 在
**规划阶段判定失败**时，只把终态写进内存、**不推 mailbox 终态事件**：

    _plan_with_retry 耗尽 2 次重试 → task_state.fail(..., PLANNING_FAILED) → None
    plan_and_validate 见 plan is None → return None            ← 无任何 push
    SubAgent.run 见 graph is None → return                     ← **正常返回，不抛**
    _run_and_cleanup 的 except Exception 兜底不触发             ← 它只兜"抛异常"
    finally → _cleanup_task 摘除注册表

于是 mailbox 里**只剩 QUEUED**，终态事件从未存在。后果有两面，都在本文件钉住：

1. **用户永久收不到失败通知**——消费者等一个永不到来的终态（Test T1/T2）；
2. **归因错误**——该任务条目仍留在 `_background_tasks` 里，停机时被
   `_stop_mailbox` 落库为 `interrupted`（"任务因服务重启被中断，未产生结果"），
   而真实原因是**规划失败**、根本没有被重启打断（跨层回归在
   `core/agent/tests/test_task_terminal_closure.py`）。

覆盖：planner 抛异常 / planner 产出不可解析 / 校验 ABORT / 执行失败 handler /
cancel during planning / shutdown during planning / 无终态正常返回的安全网 /
唯一性（不 double-finalize）。
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock

import pytest

from Module.workflow_contract import (
    AgentInfra,
    ConstraintConfig,
    DelegationContext,
    SessionId,
    WorkflowEventType,
    WorkflowId,
    WorkflowStatus,
)
from Module.workflow_engine.mailbox import WorkflowMailbox
from Module.workflow_engine.schema import (
    OrchestratorResult,
    OrchestratorRunStatus,
    WorkflowErrorCode,
    WorkflowState,
)
from Module.workflow_engine.tests.helpers.fake_llm import FakeLLM, with_deadline
from Module.workflow_engine.workflow_engine import WorkflowEngine

pytestmark = pytest.mark.unit

_TERMINALS = (
    WorkflowEventType.COMPLETED,
    WorkflowEventType.FAILED,
    WorkflowEventType.CANCELLED,
)


# ── 基建 ────────────────────────────────────────────────────────────────────


def _engine(fake: FakeLLM, *, sink=None, max_sub_agents: int = 1) -> WorkflowEngine:
    """构造 WorkflowEngine——AgentInfra 用 FakeLLM（未 start，调用方负责 start）。"""
    infra = AgentInfra(
        llm=fake, milvus=None, http=None,
        tool_registry=MagicMock(), constraints=ConstraintConfig(),
        terminal_result_sink=sink,
    )
    return WorkflowEngine(
        infra=infra, hitl_notifier=MagicMock(), max_sub_agents=max_sub_agents)


def _drain(mailbox: WorkflowMailbox) -> list:
    """非阻塞取空 mailbox（保序）。"""
    out = []
    while True:
        ev = mailbox.get_nowait()
        if ev is None:
            break
        out.append(ev)
    return out


def _terminals(events: list) -> list[WorkflowEventType]:
    """事件流里的终态类型序列——唯一性与类型断言的唯一观测量。"""
    return [ev.type for ev in events if ev.type in _TERMINALS]


def _failed_data(events: list) -> dict:
    """取唯一 FAILED 事件的 data（不存在则断言失败并打印实际事件流）。"""
    kinds = [ev.type.value for ev in events]
    failed = [ev for ev in events if ev.type == WorkflowEventType.FAILED]
    assert len(failed) == 1, f"应有且仅有一个 FAILED 终态事件，实际事件流: {kinds}"
    return failed[0].data


async def _settle(engine: WorkflowEngine, tid: WorkflowId, timeout: float = 10.0) -> None:
    """等到执行包装的 finally 跑完（注册表摘除 ⇒ 终态已推、清理已完成）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tid not in engine._tasks:
            await asyncio.sleep(0)          # 让残留回调落地
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"任务 {tid} 未在 {timeout}s 内结束")


async def _run_to_terminal(engine: WorkflowEngine, intent: str = "测试任务"):
    """提交任务 → 等到终态 → 返回 (mailbox, 事件流)。"""
    ctx = DelegationContext(intent=intent, tools=[], session_id=SessionId("s1"))
    result = await engine.submit(ctx)
    assert isinstance(result, WorkflowMailbox), f"submit 被拒: {result}"
    await _settle(engine, result.task_id)
    return result, _drain(result)


# ══════════════════════════════════════════════════════════════════════════
# T1 —— Planning failure：planner 抛异常（重试耗尽）
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_planner_exception_reaches_failed_terminal():
    """planner 连续抛异常 → 重试耗尽 → **必须**产生 FAILED 终态（T1）。"""
    fake = FakeLLM()

    def boom(_msgs):
        raise ValueError("planner exploded（模拟 LLM/网络异常）")

    fake.handler = boom
    engine = _engine(fake)
    await engine.start()
    try:
        _mailbox, events = await _run_to_terminal(engine)
        assert _terminals(events) == [WorkflowEventType.FAILED], (
            "planner 抛异常后 mailbox 必须出现唯一 FAILED 终态，实际: "
            f"{[ev.type.value for ev in events]}"
        )
        assert fake.calls == 2, "重试语义不变：两次尝试后才判失败"
    finally:
        await engine.stop()


# ══════════════════════════════════════════════════════════════════════════
# T2 —— Planning failure：planner 产出不可解析（结构化失败，非异常）
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_planner_unparseable_output_reaches_failed_terminal():
    """planner 返回非 JSON（PlanParseError）→ 同样**必须**产生 FAILED 终态。

    只测异常会漏掉这一条：LLM 抖动最常见的形态是"返回了东西但不是合法 Plan"，
    它走的是 `PlanParseError` 而不是任意异常，且与 fix_plan 重试路径不同支。
    """
    fake = FakeLLM()
    fake.handler = lambda _msgs: {"type": "text", "content": "抱歉，我无法完成这个任务。"}
    engine = _engine(fake)
    await engine.start()
    try:
        _mailbox, events = await _run_to_terminal(engine)
        assert _terminals(events) == [WorkflowEventType.FAILED], (
            "planner 产出不可解析时 mailbox 必须出现唯一 FAILED 终态，实际: "
            f"{[ev.type.value for ev in events]}"
        )
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_plan_validation_abort_reaches_failed_terminal():
    """plan 校验 ABORT → FAILED **且归因指向规划阶段**，状态真正终态。

    **本用例曾经是假绿**（Challenger 证明）：它原先只断言"有一个 FAILED 事件"，
    而引擎级通用兜底在修复前**已经**会推一个 FAILED（`internal_error`）——
    于是断言满足、缺陷漏网。现在同时钉住 `error_type` 与 `task_state` 是否终态。
    """
    fake = FakeLLM()
    # 节点数超限 → 校验 ABORT（MAX_NODES_EXCEEDED → plan_too_large）
    many = [{"node_id": f"n{i}", "description": f"步骤 {i}",
             "output_contract": {"description": f"产出 {i}"}} for i in range(40)]
    fake.handler = lambda _msgs: {
        "type": "text",
        "content": json.dumps({"nodes": many, "edges": []}, ensure_ascii=False),
    }
    engine = _engine(fake)
    await engine.start()
    try:
        ctx = DelegationContext(intent="测试任务", tools=[], session_id=SessionId("s1"))
        result = await engine.submit(ctx)
        assert isinstance(result, WorkflowMailbox)
        tid = result.task_id
        for _ in range(500):
            if tid in engine._tasks:
                break
            await asyncio.sleep(0.005)
        sa = engine._tasks.get(tid)
        ts_ref = sa._task_state if sa is not None else None
        await _settle(engine, tid)

        events = _drain(result)
        data = _failed_data(events)
        assert _terminals(events) == [WorkflowEventType.FAILED]
        assert data.get("error_type") == WorkflowErrorCode.PLAN_TOO_LARGE.value
        assert ts_ref is not None and ts_ref.is_terminal() is True, (
            f"ABORT 后 task_state 必须终态，实际: "
            f"{ts_ref.status.value if ts_ref else '?'}"
        )
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_plan_validator_crash_is_planning_failure_not_internal():
    """校验器**自身抛异常**（重复 node_id）→ 归因 planning_failed 且状态终态。

    Challenger C1：`PlanGraph.from_plan()` 在 node_id 重复时直接 `raise ValueError`，
    而它排在 `DUPLICATE_ID` 那条校验**之前**；`plan_and_validate` 原先没有 try，
    异常冒到引擎级通用兜底 → 事件报 `internal_error`/"任务执行异常"，
    `task_state` 永远停在 `planning`、`error=None`（既非终态也无归因）。
    """
    fake = FakeLLM()
    fake.handler = lambda _msgs: {
        "type": "text",
        "content": json.dumps({
            "nodes": [
                {"node_id": "a", "description": "A",
                 "output_contract": {"description": "d"}},
                {"node_id": "a", "description": "重复 A",
                 "output_contract": {"description": "d"}},
            ],
            "edges": [],
        }, ensure_ascii=False),
    }
    engine = _engine(fake)
    await engine.start()
    try:
        ctx = DelegationContext(intent="测试任务", tools=[], session_id=SessionId("s1"))
        result = await engine.submit(ctx)
        assert isinstance(result, WorkflowMailbox)
        tid = result.task_id
        for _ in range(500):
            if tid in engine._tasks:
                break
            await asyncio.sleep(0.005)
        sa = engine._tasks.get(tid)
        ts_ref = sa._task_state if sa is not None else None
        await _settle(engine, tid)

        events = _drain(result)
        data = _failed_data(events)
        assert data.get("error_type") == WorkflowErrorCode.PLANNING_FAILED.value, (
            "校验器崩溃是**规划期**事故，不得冒充执行期内部错误；实际: "
            f"{data.get('error_type')!r}"
        )
        assert ts_ref is not None and ts_ref.is_terminal() is True, (
            f"校验器崩溃后状态必须终态（修复前停在 planning），实际: "
            f"{ts_ref.status.value if ts_ref else '?'}"
        )
        assert ts_ref.error_code == WorkflowErrorCode.PLANNING_FAILED
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_fix_plan_exhaustion_is_planning_failure_not_internal():
    """fix_plan 重试耗尽（planner 漏 output_contract）→ **planning_failed**。

    Challenger C3：`_abort_error_code` 的兜底原为 `INTERNAL_ERROR`，于是
    "规划器产出不合格"被报成"引擎故障"。同时该分支原先**手搓 data**、绕过
    规划失败的 push 实现点——两处口径各写一份。
    """
    fake = FakeLLM()
    fake.handler = lambda _msgs: {
        "type": "text",
        "content": json.dumps({
            "nodes": [{"node_id": "a", "description": "A"}],   # 缺 output_contract
            "edges": [],
        }, ensure_ascii=False),
    }
    engine = _engine(fake)
    await engine.start()
    try:
        _mailbox, events = await _run_to_terminal(engine)
        data = _failed_data(events)
        assert _terminals(events) == [WorkflowEventType.FAILED]
        assert data.get("error_type") == WorkflowErrorCode.PLANNING_FAILED.value, (
            f"fix_plan 耗尽属规划失败，不得报 internal_error；实际: "
            f"{data.get('error_type')!r}"
        )
        assert fake.calls == 3, "重试预算不变：1 次初判 + 2 次 fix_plan"
    finally:
        await engine.stop()


# ══════════════════════════════════════════════════════════════════════════
# T4 —— Failure attribution 必须匹配真实失败阶段
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_planning_failure_attribution_is_planning_not_execution():
    """planning 失败 → `error_type=planning_failed` 且文案指明规划，不得误报执行/超时（T4）。"""
    fake = FakeLLM()

    def boom(_msgs):
        raise ValueError("planner exploded")

    fake.handler = boom
    engine = _engine(fake)
    await engine.start()
    try:
        _mailbox, events = await _run_to_terminal(engine)
        data = _failed_data(events)
        assert data.get("error_type") == WorkflowErrorCode.PLANNING_FAILED.value, (
            f"机器可读阶段归因必须是 planning_failed，实际: {data.get('error_type')!r}"
        )
        error_text = str(data.get("error", ""))
        assert "规划" in error_text, f"人类可读归因必须指明规划阶段，实际: {error_text!r}"
        # 不得误报成执行器/超时
        assert data.get("error_type") != WorkflowErrorCode.TIMEOUT.value
        assert data.get("error_type") != WorkflowErrorCode.INTERNAL_ERROR.value, (
            "规划失败不得被兜底成 internal_error（那正是修复前的形态）"
        )
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_executor_failure_handler_pushes_failed_terminal():
    """T3：执行阶段失败 handler 同样推 FAILED 终态——与规划失败共享同一不变量。"""
    from Module.workflow_engine.core.sub_agent import result_handlers

    mailbox = WorkflowMailbox(WorkflowId("t-exec"))
    ts = WorkflowState(
        task_id=WorkflowId("t-exec"), status=WorkflowStatus.RUNNING,
        tools=[], session_id=SessionId("s1"))
    result = OrchestratorResult(
        status=OrchestratorRunStatus.FAILED,
        error_context={
            "reason": "节点 a 执行失败",
            "error_type": WorkflowErrorCode.TOOL_ERROR.value,
        },
    )
    pushed: list = []

    async def _push(mb, etype, data):
        from Module.workflow_engine.mailbox import push_mailbox_event
        push_mailbox_event(mb, etype, data, caller="test")
        pushed.append((etype, data))

    await result_handlers.handle_failed(result, ts, mailbox, _push)
    events = _drain(mailbox)
    assert _terminals(events) == [WorkflowEventType.FAILED]
    assert _failed_data(events).get("error_type") == WorkflowErrorCode.TOOL_ERROR.value


# ══════════════════════════════════════════════════════════════════════════
# T3 —— Terminal 必须进入 durable 投递链（同步 WAL sink）
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_planning_failure_reaches_terminal_wal_sink():
    """planning 失败的终态必须经 `terminal_sink` 同步落盘——进入既有 durable 链路（T3）。"""
    seen: list[tuple] = []

    def sink(task_id: str, event: str, data: dict) -> None:
        seen.append((task_id, event, data))

    fake = FakeLLM()

    def boom(_msgs):
        raise ValueError("planner exploded")

    fake.handler = boom
    engine = _engine(fake, sink=sink)
    await engine.start()
    try:
        mailbox, events = await _run_to_terminal(engine)
        terminal_writes = [s for s in seen if s[1] in ("completed", "failed", "cancelled")]
        assert len(terminal_writes) == 1, (
            f"终态必须**恰好一次**同步落盘（硬杀窗口），实际: {seen}"
        )
        task_id, event, data = terminal_writes[0]
        assert task_id == str(mailbox.task_id)
        assert event == "failed"
        assert data.get("error_type") == WorkflowErrorCode.PLANNING_FAILED.value
    finally:
        await engine.stop()


# ══════════════════════════════════════════════════════════════════════════
# T1 安全网 —— run() 正常返回但未推终态事件（通用不变量）
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_run_returning_without_terminal_is_finalized_by_engine():
    """`run()` 正常返回却既未推终态、状态仍非终态 → 引擎必须兜底终态化，不得静默消失（T1）。

    这是通用不变量而非某一条路径的补丁：`except Exception` 兜底只在**抛异常**时
    触发，而 `SubAgent.run()` 有多条**正常返回**的出口（规划判定失败、未知
    `OrchestratorRunStatus` 的防御分支）。任何一条漏推终态都会让消费者永久阻塞。
    """
    engine = WorkflowEngine(
        infra=MagicMock(), hitl_notifier=MagicMock(), max_sub_agents=1)
    await engine.start()
    tid = WorkflowId("t-quiet")
    ts = WorkflowState(
        task_id=tid, status=WorkflowStatus.RUNNING,
        tools=[], session_id=SessionId("s1"))
    sa = MagicMock()
    sa._task_state = ts
    sa._checkpoint_mgr = None
    sa._artifact_store = None

    async def quiet_run(_context, _mailbox):
        return  # 正常返回，不推任何事件、不改状态

    sa.run = quiet_run
    engine._tasks[tid] = sa
    engine._contexts[tid] = DelegationContext(
        intent="测试任务", session_id=SessionId("s1"))
    mailbox = engine._mailboxes[tid] = WorkflowMailbox(tid)
    await engine._semaphore.acquire()
    engine._active.add(tid)
    fut = asyncio.create_task(engine._run_and_cleanup(sa, tid))
    engine._task_futures[tid] = fut

    await with_deadline(fut, 5.0)
    try:
        events = _drain(mailbox)
        assert _terminals(events) == [WorkflowEventType.FAILED], (
            "无终态的正常返回必须被引擎兜底为唯一 FAILED 终态，实际: "
            f"{[ev.type.value for ev in events]}"
        )
        assert ts.status == WorkflowStatus.FAILED, (
            "兜底必须把任务状态真正推进到终态（T1：不得停在 RUNNING）"
        )
        assert ts.is_terminal() is True
    finally:
        await engine.stop()


# ══════════════════════════════════════════════════════════════════════════
# T4 / T5 —— Cancel 与 shutdown during planning
# ══════════════════════════════════════════════════════════════════════════


def _gated_engine(fake: FakeLLM) -> WorkflowEngine:
    """planner 挂在 LLM gate 上的引擎——用于 cancel/shutdown during planning。"""
    fake.gate = asyncio.Event()          # 恒不释放：模拟慢 planner
    return _engine(fake)


async def _wait_dispatched(engine: WorkflowEngine, tid: WorkflowId,
                           timeout: float = 5.0) -> asyncio.Task:
    """等 drain worker 派发并返回 owned task。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fut = engine._task_futures.get(tid)
        if fut is not None:
            return fut
        await asyncio.sleep(0.01)
    raise AssertionError("owned task 未就位——drain 未派发")


@pytest.mark.asyncio
async def test_cancel_during_planning_is_unique_cancelled_terminal():
    """planning 中 cancel → **唯一** CANCELLED 终态，且不得再补发 failed/completed（T4）。"""
    fake = FakeLLM()
    engine = _gated_engine(fake)
    await engine.start()
    try:
        ctx = DelegationContext(intent="测试任务", tools=[], session_id=SessionId("s1"))
        result = await engine.submit(ctx)
        assert isinstance(result, WorkflowMailbox)
        tid = result.task_id
        fut = await _wait_dispatched(engine, tid)
        await asyncio.sleep(0.05)                     # 让 planner 真正进入 LLM 等待

        assert await with_deadline(engine.cancel(tid), 5.0) is True
        with pytest.raises(asyncio.CancelledError):
            await with_deadline(fut, 5.0)

        await _settle(engine, tid)
        events = _drain(result)
        assert _terminals(events) == [WorkflowEventType.CANCELLED], (
            "planning 中 cancel 必须产生唯一 CANCELLED 终态，实际: "
            f"{[ev.type.value for ev in events]}"
        )
        # 二次 cancel 幂等，不追加终态
        assert await engine.cancel(tid) is True
        await asyncio.sleep(0)
        assert _terminals(_drain(result)) == [], "重复 cancel 不得再推终态事件"
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_shutdown_during_planning_is_unique_cancelled_terminal():
    """stop() 时任务正在 planner → **唯一** CANCELLED 终态（现状设计：取消而非消失）（T5）。"""
    fake = FakeLLM()
    engine = _gated_engine(fake)
    await engine.start()
    ctx = DelegationContext(intent="测试任务", tools=[], session_id=SessionId("s1"))
    result = await engine.submit(ctx)
    assert isinstance(result, WorkflowMailbox)
    tid = result.task_id
    await _wait_dispatched(engine, tid)
    await asyncio.sleep(0.05)                         # planner 挂在 LLM 等待上

    await engine.stop(timeout=0.1)
    events = _drain(result)
    assert _terminals(events) == [WorkflowEventType.CANCELLED], (
        "shutdown during planning 必须产生唯一 CANCELLED 终态（不得静默消失），实际: "
        f"{[ev.type.value for ev in events]}"
    )
    assert tid not in engine._tasks


# ══════════════════════════════════════════════════════════════════════════
# T1/T4 —— fix_plan 重试期间的规划异常（同一缺陷的第三条出口）
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_fix_plan_retry_exception_is_planning_failure_not_internal():
    """fix_plan 重试期间 planner 抛异常 → 终态是 **planning_failed**，且状态真到终态。

    这是唯一不走 `_plan_with_retry` 的 planner 调用。修复前它冒泡到引擎级
    `except Exception`：事件说 FAILED/`internal_error`/"任务执行异常"，而
    `task_state` 停在 `planning`、`error` 为 None——**同一件事两副面孔**：
    既违反 T4（真实阶段是规划，却报成执行期内部错误），又违反 T1
    （状态永远到不了终态，`is_terminal()` 恒 False）。
    """
    fake = FakeLLM()
    calls = {"n": 0}

    def handler(_msgs):
        calls["n"] += 1
        if calls["n"] == 1:
            # 可解析但校验判 FIX_PLAN 的 plan（边指向不存在的节点 → MISSING_DEPENDENCY）
            return {"type": "text", "content": json.dumps({
                "nodes": [{"node_id": "a", "description": "步骤 a"}],
                "edges": [["a", "ghost"]],
            }, ensure_ascii=False)}
        raise RuntimeError("fix_plan 期间 LLM 炸了")

    fake.handler = handler
    engine = _engine(fake)
    await engine.start()
    try:
        ctx = DelegationContext(intent="测试任务", tools=[], session_id=SessionId("s1"))
        result = await engine.submit(ctx)
        assert isinstance(result, WorkflowMailbox)
        tid = result.task_id
        for _ in range(500):                      # 抓 task_state 引用（清理后取不到）
            if tid in engine._tasks:
                break
            await asyncio.sleep(0.005)
        sa = engine._tasks.get(tid)
        ts_ref = sa._task_state if sa is not None else None
        await _settle(engine, tid)

        events = _drain(result)
        data = _failed_data(events)
        assert _terminals(events) == [WorkflowEventType.FAILED]
        assert data.get("error_type") == WorkflowErrorCode.PLANNING_FAILED.value, (
            "fix_plan 期间的规划异常必须归因为 planning_failed，而不是 internal_error；"
            f"实际: {data.get('error_type')!r}"
        )
        assert ts_ref is not None
        assert ts_ref.is_terminal() is True, (
            f"task_state 必须真正到达终态（修复前停在 planning），实际: {ts_ref.status.value}"
        )
        assert ts_ref.error_code == WorkflowErrorCode.PLANNING_FAILED
    finally:
        await engine.stop()


# ══════════════════════════════════════════════════════════════════════════
# T1 —— 派发失败（包装协程从未创建）
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_failure_still_yields_terminal_event(monkeypatch):
    """`_drain_queue` 派发失败 → 仍必须推终态（T1）；任务已被 submit 接受，不能静默消失。

    这条路径上 `_run_and_cleanup` **从未被创建**，所以 finally 里的安全网够不着；
    修复前 mailbox 永远只剩 QUEUED，任务却已从注册表摘除。
    """
    fake = FakeLLM()
    fake.handler = lambda _msgs: {"type": "text", "content": json.dumps({
        "nodes": [{"node_id": "a", "description": "步骤 a"}], "edges": [],
    }, ensure_ascii=False)}
    engine = _engine(fake)
    await engine.start()

    real_create_task = asyncio.create_task
    state = {"boom": False}

    def flaky_create_task(coro, **kwargs):
        if state["boom"]:
            coro.close()                          # 抑制 "never awaited" 警告
            raise RuntimeError("模拟 create_task 失败（事件循环关闭）")
        return real_create_task(coro, **kwargs)

    monkeypatch.setattr(asyncio, "create_task", flaky_create_task)
    try:
        state["boom"] = True
        ctx = DelegationContext(intent="测试任务", tools=[], session_id=SessionId("s1"))
        result = await engine.submit(ctx)
        assert isinstance(result, WorkflowMailbox)
        tid = result.task_id
        deadline = time.monotonic() + 5.0
        while tid in engine._tasks and time.monotonic() < deadline:
            await asyncio.sleep(0.01)             # 等 drain worker 走到派发失败分支
        await asyncio.sleep(0)

        events = _drain(result)
        assert _terminals(events) == [WorkflowEventType.FAILED], (
            "派发失败必须补一个 FAILED 终态，不得让已接受的任务静默消失，实际: "
            f"{[ev.type.value for ev in events]}"
        )
        assert engine._semaphore.locked() is False, "派发失败不得泄漏执行槽"
    finally:
        state["boom"] = False
        monkeypatch.setattr(asyncio, "create_task", real_create_task)
        await engine.stop()


# ══════════════════════════════════════════════════════════════════════════
# T1 —— RESUME 派发失败（Challenger C2：PAUSED 的"保留待恢复"前提不成立）
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_resume_dispatch_failure_still_yields_terminal_event(monkeypatch):
    """`resume()` 排队后的派发失败 → 仍必须推终态；PAUSED 不得让它静默消失（T1）。

    Challenger C2：派发失败分支随后就 `_cleanup_task()` 销毁任务，而安全网对
    PAUSED 提前返回（"保留 mailbox 等 resume"）——那个前提在这里**不成立**，
    于是任务连终态带注册表一起消失，调用方拿着 mailbox 永久阻塞。
    """
    fake = FakeLLM()
    fake.gate = asyncio.Event()               # 恒不释放：把 planner 挂住
    engine = _engine(fake)
    await engine.start()
    real_create_task = asyncio.create_task
    try:
        ctx = DelegationContext(intent="测试任务", tools=[], session_id=SessionId("s1"))
        result = await engine.submit(ctx)
        assert isinstance(result, WorkflowMailbox)
        tid = result.task_id
        await _wait_dispatched(engine, tid)
        await asyncio.sleep(0.05)

        # 造一个 PAUSED 任务（模拟已暂停、等待 resume）
        sa = engine._tasks[tid]
        sa._task_state.status = WorkflowStatus.PAUSED
        engine._active.discard(tid)
        engine._task_futures.pop(tid, None)
        engine._semaphore.release()

        await engine._semaphore.acquire()      # 占满槽 → resume() 走排队分支
        resumed = await engine.resume(tid)
        assert isinstance(resumed, WorkflowMailbox), f"resume 被拒: {resumed}"
        engine._semaphore.release()            # 让 drain worker 能 acquire

        state = {"boom": False}

        def flaky_create_task(coro, **kwargs):
            if state["boom"]:
                coro.close()
                raise RuntimeError("模拟 create_task 失败")
            return real_create_task(coro, **kwargs)

        monkeypatch.setattr(asyncio, "create_task", flaky_create_task)
        state["boom"] = True
        await asyncio.sleep(0.3)
        monkeypatch.setattr(asyncio, "create_task", real_create_task)
        state["boom"] = False

        events = _drain(result)
        assert _terminals(events) == [WorkflowEventType.FAILED], (
            "RESUME 派发失败必须补终态（任务随即被销毁，不会再 resume），实际: "
            f"{[ev.type.value for ev in events]}"
        )
        assert sa._task_state.is_terminal() is True
        assert engine._semaphore.locked() is False, "派发失败不得泄漏执行槽"
    finally:
        monkeypatch.setattr(asyncio, "create_task", real_create_task)
        await engine.stop()


# ══════════════════════════════════════════════════════════════════════════
# T2 —— 取消不得覆写已落定的终态（Challenger R2：原先只是"恰好没有 await"）
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cancel_error_does_not_overwrite_completed_status():
    """`finalize()` 已写 COMPLETED、事件尚未推时被取消 → 终态仍是 **COMPLETED**。

    修复前 `except CancelledError` **无条件**写 CANCELLED，且此刻
    `_terminal_pushed` 还是 False（COMPLETED 事件没推），唯一性守卫拦不住——
    一个真正完成的任务会被改判成 CANCELLED。它能"看起来正确"只是因为
    `finalize()` 在写状态与推事件之间恰好没有 await（`CheckpointManager.save`
    是纯内存）。本用例把那个 await 变出来，钉住"先到终态者胜"。
    """
    engine = WorkflowEngine(
        infra=MagicMock(), hitl_notifier=MagicMock(), max_sub_agents=1)
    await engine.start()
    tid = WorkflowId("t-completed-then-cancel")
    ts = WorkflowState(
        task_id=tid, status=WorkflowStatus.RUNNING,
        tools=[], session_id=SessionId("s1"))
    sa = MagicMock()
    sa._task_state = ts
    sa._checkpoint_mgr = None
    sa._artifact_store = None

    async def run_then_cancel(_context, _mailbox):
        ts.status = WorkflowStatus.COMPLETED     # finalize() 的第一个同步写
        raise asyncio.CancelledError()           # 取消在"推事件前"落地

    sa.run = run_then_cancel
    engine._tasks[tid] = sa
    engine._contexts[tid] = DelegationContext(
        intent="测试任务", session_id=SessionId("s1"))
    mailbox = engine._mailboxes[tid] = WorkflowMailbox(tid)
    await engine._semaphore.acquire()
    engine._active.add(tid)
    fut = asyncio.create_task(engine._run_and_cleanup(sa, tid))
    engine._task_futures[tid] = fut

    with pytest.raises(asyncio.CancelledError):
        await with_deadline(fut, 5.0)
    try:
        events = _drain(mailbox)
        assert _terminals(events) == [WorkflowEventType.COMPLETED], (
            "已完成的终态不得被取消改判；实际: "
            f"{[ev.type.value for ev in events]}"
        )
        assert ts.status == WorkflowStatus.COMPLETED
    finally:
        await engine.stop()


# ══════════════════════════════════════════════════════════════════════════
# T7 —— 同一 task 只能有一个 logical terminal outcome
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_planning_failure_then_stop_does_not_double_finalize():
    """planning 失败已推 FAILED → 随后 stop() 不得再补一个不同的终态（T2/T7）。"""
    fake = FakeLLM()

    def boom(_msgs):
        raise ValueError("planner exploded")

    fake.handler = boom
    engine = _engine(fake)
    await engine.start()
    ctx = DelegationContext(intent="测试任务", tools=[], session_id=SessionId("s1"))
    result = await engine.submit(ctx)
    assert isinstance(result, WorkflowMailbox)
    await _settle(engine, result.task_id)

    await engine.stop()
    events = _drain(result)
    assert _terminals(events) == [WorkflowEventType.FAILED], (
        "终态唯一：planning 失败 + 停机不得产生两个不同 logical terminal，实际: "
        f"{[ev.type.value for ev in events]}"
    )
