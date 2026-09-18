# 测试文件：core/agent/tests/test_turn_finalization_cancellation_safety.py
"""Batch 11（KB-25 / Cancellation-Safe Turn Finalization）：整只 Agent 的 E2 证据。

本模块走**真实入口**（与 Batch 10 的 E2 模块同形）：

- 真 `Agent` + 真 `Agent.start()`（含真 `_process_events` 消费者）；
- 真 `agent.process_message()` → 真 EventBus USER_INPUT → 真 `_handle_user_input`
  → 真 `CognitiveLoop.process()`；
- 真 `AgentFSM.transition()`（经 `FSMBackedPhaseController`）；
- 第一发取消是**生产路径**：WS 停止按钮/语音取消走的那条
  `bus.post(InterruptData(source="websocket"), priority=INTERRUPT)`；
- 第二发取消也是**生产路径**：`Agent._turns`（`AsyncTaskScope`）的 `drain()` 在预算
  耗尽后对仍未落定的回合直接 `task.cancel()`（`Module/_common/task_scope.py:277`）——
  这正是 `Agent.stop()` 第 3 步 `_drain_cognitive_turns` 做的事。

唯一替身是**挂起点本身**（LLM 流 / 出站帧 / Mongo upsert 的屏障）：它们本来就是会
挂起数毫秒到数秒的真实 await。

判据（§16「stable terminal state」）：FSM 离开活跃区 + `_process_lock` 释放 +
generation 终态 + 后续输入恢复处理权 + 取消意图仍然传播 + 停机仍有界。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
import pytest_asyncio

from core.agent.agent import Agent
from core.agent._types import RunState
from core.config import AppConfig
from core.monitoring.event_bus import EventPriority
from core.schema.event import InterruptData

#: 「本测试不碰真实库」的显式声明（见根 conftest 的真实库防线）。
pytestmark = [pytest.mark.service, pytest.mark.usefixtures("fake_storage")]

#: 死锁探测器——只在本模块真的挂死（无界 await）时触发。
_PROBE_S = 20.0

#: `Agent.stop()` 第 3 步的 drain 预算（`core/agent/lifecycle.py:320` 的实参）。
_DRAIN_BUDGET_S = 3.0

#: 活跃区相位（仍宣称"有回合在跑"）。稳定终态必须离开它。
_ACTIVE = {"thinking", "executing", "speaking", "interrupted"}

_REPLY = "好呀，我在听。<mood>平静</mood>"


class _RawClient:
    """`Agent.start()` 的 task-system 步骤会读 `raw_client`（热切换判据）。"""

    def __init__(self, model_id: str) -> None:
        self.model = model_id

    def is_closed(self) -> bool:
        return False

    async def close(self) -> None:
        return None


class _FakeLLM:
    """够用的 LLM 后端替身：流式吐一句带 `<mood>` 标签的回复。"""

    def __init__(self, response: str = _REPLY) -> None:
        self.response = response
        self.raw_client = _RawClient("fake/test")

    async def test(self) -> dict:
        return {"ok": True}

    def get_preprocess(self) -> None:
        return None

    async def close(self) -> None:
        await self.raw_client.close()

    async def chat(self, messages: list[dict], **kwargs: Any) -> str:
        return self.response

    async def chat_stream(self, messages: list[dict], **kwargs: Any):
        yield self.response

    async def stream_think(self, messages: list[dict], **kwargs: Any):
        for ch in self.response:
            yield {"content": ch, "tool_calls": None}


class _FailingLLM(_FakeLLM):
    """流式阶段抛普通异常（provider 失败的形状）。"""

    async def chat_stream(self, messages: list[dict], **kwargs: Any):
        raise RuntimeError("provider exploded")
        yield ""  # pragma: no cover

    async def stream_think(self, messages: list[dict], **kwargs: Any):
        raise RuntimeError("provider exploded")
        yield {}  # pragma: no cover


def _install_fake_llm(monkeypatch: pytest.MonkeyPatch, *, failing: bool = False) -> None:
    from Module.llm import LLMFactory

    cls = _FailingLLM if failing else _FakeLLM
    monkeypatch.setattr(LLMFactory, "create", staticmethod(lambda model_id, **kw: cls()))


class Gate:
    """按需阻塞一次的屏障（结构化信号：`started` / `release` 两个 Event）。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._armed = False

    def arm(self) -> None:
        self._armed = True
        self.started.clear()
        self.release.clear()

    def open(self) -> None:
        self.release.set()

    async def pass_through(self) -> None:
        if self._armed:
            self._armed = False
            self.started.set()
            await self.release.wait()

    async def wait_started(self, what: str) -> None:
        try:
            await asyncio.wait_for(self.started.wait(), timeout=_PROBE_S)
        except asyncio.TimeoutError:  # pragma: no cover — 只在真回归时触发
            raise AssertionError(
                f"屏障未被命中（{what}）：收口没有走到目标 await，用例前提不成立"
            ) from None


class TransitionRecorder:
    """经 `AgentStatus.on_change`（生产公开订阅点）记录语义状态名。"""

    def __init__(self) -> None:
        self.names: list[str] = []
        self._events: dict[str, asyncio.Event] = {}

    async def __call__(self, name: str) -> None:
        self.names.append(name)
        event = self._events.get(name)
        if event is not None:
            event.set()

    def watch(self, name: str) -> asyncio.Event:
        event = asyncio.Event()
        self._events[name] = event
        return event

    def diagnose(self) -> str:
        return f"transitions={self.names}"


async def _wait_true(predicate) -> None:
    while not predicate():
        await asyncio.sleep(0)


async def _drain_task(task: asyncio.Task | None) -> None:
    if task is None:
        return
    try:
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True),
                               timeout=_PROBE_S)
    except asyncio.TimeoutError:  # pragma: no cover
        raise AssertionError(f"回合任务未在 {_PROBE_S}s 内落定（疑似无界 await）") from None


async def _stop_interrupt(agent: Agent, *, source: str = "websocket") -> None:
    """生产取消源：WS 停止按钮 / 语音取消所用的 INTERRUPT 事件。"""
    agent.bus.post(InterruptData(source=source), priority=EventPriority.INTERRUPT)


async def _block_output(agent: Agent, name: str, gate: Gate) -> None:
    """把 `Agent.output.<name>` 换成"武装时阻塞一次"的版本。"""
    inner = getattr(agent.output, name)

    async def _blocked(*args, **kwargs):
        await gate.pass_through()
        return await inner(*args, **kwargs)

    setattr(agent.output, name, _blocked)


def _block_upsert(agent: Agent, gate: Gate, *, status: str | None = None) -> None:
    """把 `Agent._generation_repo.upsert` 换成可阻塞的版本（可选按 status 过滤）。"""
    inner = agent._generation_repo.upsert  # noqa: SLF001

    async def _blocked(doc: dict) -> Any:
        if status is None or doc.get("status") == status:
            await gate.pass_through()
        return await inner(doc)

    agent._generation_repo.upsert = _blocked  # type: ignore[method-assign]  # noqa: SLF001


def _hold_stream(agent: Agent, gate: Gate, *, reply: str = _REPLY) -> None:
    """把 LLM 流钉在 `gate` 上——回合此刻确定处于 THINKING（首句尚未派发）。"""

    async def _stream(*_args, **_kwargs):
        await gate.pass_through()
        yield {"content": reply, "tool_calls": None}

    agent._cognitive._llm.stream_think = _stream  # noqa: SLF001


async def _turn_in_thinking(agent: Agent, text: str = "第一条", *,
                            hold: Gate | None = None) -> asyncio.Task:
    """起一个真回合并等它真的进入 THINKING；返回回合任务句柄。"""
    if hold is not None:
        _hold_stream(agent, hold)
        hold.arm()
    thinking = asyncio.Event()

    async def _watch(name: str) -> None:
        if name == "thinking":
            thinking.set()

    agent.status.on_change(_watch)
    assert await agent.process_message(text) is True
    try:
        await asyncio.wait_for(thinking.wait(), timeout=_PROBE_S)
    except asyncio.TimeoutError:  # pragma: no cover
        raise AssertionError("回合未进入 THINKING（用例前提不成立）") from None
    if hold is not None:
        # 把回合**确实钉在** LLM 流的 await 上：取消点才是确定的（否则取消可能落在
        # 流之前的某个挂起点，屏障就不会被命中，且屏障留给下一个回合误伤）。
        await hold.wait_started("LLM 流")
    turn = agent._cognitive.current_task  # noqa: SLF001
    assert turn is not None, "在途回合句柄未登记"
    return turn


@pytest_asyncio.fixture
async def agent(monkeypatch: pytest.MonkeyPatch):
    """真 Agent + 真 start()（假存储、假 LLM 后端）。"""
    _install_fake_llm(monkeypatch)
    a = Agent(config=AppConfig())
    await a.start()
    try:
        yield a
    finally:
        await a.stop()


# ── CF-T1 / CF-T2：第二发取消后仍达到稳定终态，且 pending 恢复 ───────────────


@pytest.mark.asyncio
async def test_second_cancel_during_finalization_recovers(agent):
    """CF-T1（PRE_FIX_RED）：第二发取消打在收口链上，回合仍必须落到稳定终态。

    时间线（全部真实入口，无 sleep 造 race）：

    1. 真 `process_message` → 真 `process()` → FSM=THINKING（流被钉住）；
    2. 生产取消源 = WS 停止按钮（INTERRUPT 事件）→ `cancel_current()`（第一发）；
    3. 收口停在 `output.turn_cancelled` 的屏障上（**cleanup 确实已开始**）；
    4. 第二发 = `Agent._turns.drain(timeout=0)`（`AsyncTaskScope` 预算耗尽后的
       直接 `task.cancel()`，与 `Agent.stop()` 第 3 步同一条代码路径）；
    5. 放行 → 断言：相位离开活跃区 / 锁释放 / 任务仍以 cancelled 结束。
    """
    rec = TransitionRecorder()
    agent.status.on_change(rec)
    stream, gate = Gate(), Gate()
    await _block_output(agent, "turn_cancelled", gate)
    gate.arm()

    turn = await _turn_in_thinking(agent, hold=stream)
    await _stop_interrupt(agent)
    await gate.wait_started("output.turn_cancelled")
    assert turn.cancelling() == 1, "第一发取消尚未送达（用例前提不成立）"

    leftover = await agent._turns.drain(timeout=0)  # noqa: SLF001 — 生产第二发
    assert leftover == 1, "第二发取消没有命中在途回合（用例前提不成立）"
    assert turn.cancelling() == 2, "第二发取消未送达"

    gate.open()
    await _drain_task(turn)

    assert agent.status.current_name() not in _ACTIVE, (
        "第二发取消后相位留在活跃区（KB-25：生命周期失去 owner）：" + rec.diagnose())
    assert agent.status.is_completely_idle, rec.diagnose()
    assert rec.names == ["thinking", "interrupted", "idle"], (
        "收口必须走 canonical 边且不重不漏：" + rec.diagnose())
    assert agent._cognitive.process_lock.locked() is False, rec.diagnose()  # noqa: SLF001
    assert turn.cancelled() is True, (
        "第二发取消的意图被吞掉了（必须仍以 cancelled 结束）：" + rec.diagnose())


@pytest.mark.asyncio
async def test_pending_message_is_processed_after_second_cancel(agent):
    """CF-T2（PRE_FIX_RED）：收口期间到达的 pending 消息必须重新获得处理权。

    生产形态：用户按停止按钮 → 收口途中再发一条（相位忙 ⇒ 已 ACK 进 pending）
    → 收口完成 → `STATE_CHANGE → IDLE` → **既有** drain 机制出队。
    修复前收口被打断 ⇒ 永远没有 IDLE ⇒ 那条已 ACK 的消息永久卡在队列里。
    """
    rec = TransitionRecorder()
    agent.status.on_change(rec)
    stream, gate = Gate(), Gate()
    await _block_output(agent, "turn_cancelled", gate)
    gate.arm()

    turn = await _turn_in_thinking(agent, hold=stream)
    await _stop_interrupt(agent)
    await gate.wait_started("output.turn_cancelled")

    # 收口尚未走完时再发一条：相位忙 ⇒ 排队（已 ACK）
    assert await agent.process_message("第二条") is True
    assert [p.text for p in agent.pending_messages] == ["第二条"], (
        "前置条件不成立：收口窗口内到达的消息没有进 pending 队列")
    # 在第二发取消**之前**挂上监听：B 的 THINKING 可能就发生在 drain 的等待里。
    second_thinking = rec.watch("thinking")

    await agent._turns.drain(timeout=0)  # noqa: SLF001 — 第二发
    gate.open()
    await _drain_task(turn)

    try:
        await asyncio.wait_for(second_thinking.wait(), timeout=_PROBE_S)
    except asyncio.TimeoutError:  # pragma: no cover — PRE_FIX_RED 命中处
        raise AssertionError(
            f"pending 消息未被推进（永久失聪）：FSM={agent.status.current_name()} "
            f"pending={[p.text for p in agent.pending_messages]} " + rec.diagnose()) from None

    await _drain_task(agent._cognitive.current_task)  # noqa: SLF001
    assert agent.status.is_completely_idle, rec.diagnose()
    assert not agent.pending_messages, (
        f"pending 队列未被排空：{[p.text for p in agent.pending_messages]}")
    user_texts = [m.content for m in agent.session.messages]  # type: ignore[union-attr]
    assert any("第二条" in str(t) for t in user_texts), (
        "排在 pending 里的消息没有进入会话（未被真正处理）。" + rec.diagnose())


# ── 同一边界的另一半：异常收口期间按下停止按钮（RUNNING 中用户可达） ────────


@pytest.mark.asyncio
async def test_stop_button_during_error_finalization_does_not_deafen_agent(monkeypatch):
    """CF-T1（PRE_FIX_RED，**用户可见**）：异常收口期间按停止按钮不得让 Agent 失聪。

    这是 KB-25 同一边界上真正**用户可达且用户可见**的形态：回合因 provider 失败
    进入异常收口（`_handle_turn_exception`）时用户按下停止按钮——取消落在收口的
    await 上会一路逃出 `process()`（在 `except` 块里抛出的异常不会被兄弟 `except`
    子句接住）⇒ 相位永久停在 THINKING、`run_state` 仍是 RUNNING ⇒ 之后每条消息
    都被 ACK accepted 后进 pending，**永远没有回合再启动**。

    修复前：第二条 `accepted=True` 但 `被处理=False`。修复后：恢复正常。
    """
    _install_fake_llm(monkeypatch, failing=True)
    a = Agent(config=AppConfig())
    await a.start()
    try:
        rec = TransitionRecorder()
        a.status.on_change(rec)
        gate = Gate()
        await _block_output(a, "turn_failed", gate)
        gate.arm()

        turn = await _turn_in_thinking(a)
        await gate.wait_started("output.turn_failed")
        assert a.status.current_name() == "thinking", rec.diagnose()
        assert turn.cancelling() == 0, "前置条件：此刻还没有任何取消"

        await _stop_interrupt(a)          # 用户按停止按钮
        # 消费者的处理是异步的：结构化等到取消**确实送达**（不是 sleep 猜时序）
        await asyncio.wait_for(_wait_true(lambda: turn.cancelling() == 1), timeout=_PROBE_S)
        gate.open()
        await _drain_task(turn)

        assert a.state is RunState.RUNNING, "停机语义被破坏"
        assert a.status.current_name() not in _ACTIVE, (
            "异常收口被取消 ⇒ 相位停在活跃区（Agent 永久失聪）：" + rec.diagnose())
        assert a.status.is_completely_idle, rec.diagnose()

        # 用户面判据：再来一条必须真的被处理
        _install_fake_llm(monkeypatch, failing=False)
        second_thinking = rec.watch("thinking")
        assert await a.process_message("第二条") is True
        try:
            await asyncio.wait_for(second_thinking.wait(), timeout=_PROBE_S)
        except asyncio.TimeoutError:  # pragma: no cover — PRE_FIX_RED 命中处
            raise AssertionError(
                f"第二条消息没有被处理（永久失聪）：FSM={a.status.current_name()} "
                f"pending={[p.text for p in a.pending_messages]} " + rec.diagnose()) from None
    finally:
        _install_fake_llm(monkeypatch, failing=False)
        await a.stop()


# ── CF-T5 / CF6：停机仍有界，且不因收口而启动新工作 ─────────────────────────


@pytest.mark.asyncio
async def test_shutdown_second_cancel_keeps_shutdown_bounded(monkeypatch):
    """CF-T5 / CF6（PRE_FIX_RED）：停机 drain 的第二发取消不得让停机失去上界。

    真实时序（不缩短任何生产预算）：把 `generation_registry.mark` 的 Mongo 写入卡住
    （真实形态：Mongo upsert 无 `wait_for`，只有 server-selection 3s 上界）⇒
    `Agent.stop()` 第 3 步 `_turns.drain(timeout=3.0)` 预算耗尽 ⇒ 第二发
    `task.cancel()` 落在收口链上。

    断言三件事同时成立：①停机仍在既有预算 + settle 预算内结束；
    ②相位离开活跃区（旧回合达到稳定终态）；③生命周期轴是 STOPPED，
    **不再产生任何新回合**。

    ③ 的判据刻意是**回合派发轴**（`rec.names.count("thinking")` 与在途任务句柄），
    不是 `pending_messages`——后者由 `Agent.stop()` 在入口**同步清空**，且
    `_SHUTDOWN_STEPS[0]` 就是 `stop_event_task`（IDLE 广播在停机期间根本不会被消费），
    所以"pending 为空"这条断言即使把收口整个换成 no-op 也照样成立（Challenger J3）。
    """
    _install_fake_llm(monkeypatch)
    a = Agent(config=AppConfig())
    await a.start()
    stopped = False
    stream, mark_gate = Gate(), Gate()
    try:
        rec = TransitionRecorder()
        a.status.on_change(rec)
        _block_upsert(a, mark_gate, status="cancelled")
        mark_gate.arm()

        turn = await _turn_in_thinking(a, hold=stream)
        assert await a.process_message("停机窗口内到达的消息") is True, (
            "前置条件：相位忙时消息应被 ACK 并排队")
        assert [p.text for p in a.pending_messages] == ["停机窗口内到达的消息"]

        started_at = time.monotonic()
        await a.stop()
        stopped = True
        elapsed = time.monotonic() - started_at
        mark_gate.open()
        await _drain_task(turn)
        # 停机后再放几拍，给"如果真有新回合"留出被观察到的机会
        for _ in range(50):
            await asyncio.sleep(0)

        assert elapsed < 2 * (_DRAIN_BUDGET_S + 1.0), (
            f"停机失去了上界：elapsed={elapsed:.2f}s（预算 {_DRAIN_BUDGET_S}s + settle 1.0s）")
        assert a.state is RunState.STOPPED, rec.diagnose()
        assert a.status.current_name() not in _ACTIVE, (
            "停机后相位仍留在活跃区（无主状态，只有下一次 start() 能救）：" + rec.diagnose())
        assert a.status.is_completely_idle, rec.diagnose()
        assert turn.cancelled() is True, rec.diagnose()
        # ③ 回合派发轴：停机期间**零**新回合（不是"pending 恰好为空"）
        assert rec.names == ["thinking", "interrupted", "idle"], (
            "停机期间产生了新的回合生命周期：" + rec.diagnose())
        assert a._cognitive.current_task is None, (  # noqa: SLF001
            "停机后仍有在途回合句柄：" + rec.diagnose())
        assert not a.pending_messages, "停机期间 pending 不得残留"
        assert await a.process_message("停机窗口内的消息") is False, (
            "停机语义被破坏：生命周期轴应当拒收")
    finally:
        stream.open()
        mark_gate.open()
        if not stopped:
            await a.stop()


# ── CF-T4 / CF5：旧回合收口不得影响新回合 ──────────────────────────────────


@pytest.mark.asyncio
async def test_old_turn_finalization_does_not_interleave_with_new_turn(agent):
    """CF-T4（PRE_FIX_RED）：A 的第二发收口与新回合 B 之间不得发生状态交错。

    A 收起口时会广播 `STATE_CHANGE → IDLE`（这正是 pending drain 的触发点），
    因此 B 可能被派发并**卡在 `_process_lock` 上**——但它绝不能在 A 释放锁之前
    进入 THINKING。断言完整状态序列里 A 的 `interrupted/idle` 严格早于 B 的 `thinking`。
    """
    rec = TransitionRecorder()
    agent.status.on_change(rec)
    stream, gate = Gate(), Gate()
    await _block_output(agent, "turn_cancelled", gate)
    gate.arm()

    turn_a = await _turn_in_thinking(agent, "A", hold=stream)
    await _stop_interrupt(agent)
    await gate.wait_started("output.turn_cancelled")

    assert await agent.process_message("B") is True
    assert [p.text for p in agent.pending_messages] == ["B"]

    await agent._turns.drain(timeout=0)  # noqa: SLF001 — A 的第二发取消
    gate.open()
    await _drain_task(turn_a)

    expected = ["thinking", "interrupted", "idle", "thinking", "speaking", "idle"]
    try:
        await asyncio.wait_for(_wait_true(lambda: rec.names == expected), timeout=_PROBE_S)
    except asyncio.TimeoutError:  # pragma: no cover — PRE_FIX_RED 命中处
        raise AssertionError(
            f"旧回合收口与新回合状态交错 / B 未被推进：{rec.names} "
            f"pending={[p.text for p in agent.pending_messages]}") from None

    assert rec.names == expected, "旧回合收口与新回合状态发生了交错：" + rec.diagnose()
    assert agent.status.is_completely_idle, rec.diagnose()
    assert not agent.pending_messages, rec.diagnose()
