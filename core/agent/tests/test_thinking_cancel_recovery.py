# 测试文件：core/agent/tests/test_thinking_cancel_recovery.py
"""Batch 10（KB-22 / Thinking State Cancellation Recovery）：E2 复现与恢复守卫。

## 缺陷形状（KB-22）

`CognitiveLoop._prepare_turn` 在 `await self.phases.enter_thinking()`（`loop.py:591`）
**之后**仍有一个真挂起的 await——`generation_registry.register(...)`（`:610`，Mongo
upsert）。它只被 `except Exception` 包着（`:618`），而 `asyncio.CancelledError` 在
Python 3.8+ 是 `BaseException` 子类、**不**被 `except Exception` 捕获；`process()`
里包住 `_prepare_turn` 的 `try`（`:714`）只 `except (CharacterBusyError,
StorageUnavailableError)`。于是这个窗口里落下的取消一路逃出 `process()`：

- 唯一会做 `INTERRUPTED → IDLE` 的 `_handle_interrupt()`（`:1249`）永不执行；
- `async with self._process_lock`（`:699`）退出时**释放**锁；
- FSM 永久停在 **THINKING**。

## 用户影响（本模块的判据所在）

FSM 停在 THINKING ⇒ `status.is_accepting_input` 恒假 ⇒ 之后**每一条**用户消息都走
`process_message` 的排队分支（`agent.py:675-697`）进 `_pending_messages`，而
pending 只在 `STATE_CHANGE → IDLE` 时出队（`_handle_state_change`，`agent.py:1054`）
——那个事件再也不会来。进程活着、EventBus 活着、新消息能进队列，但**没有任何回合
再启动**：用户感知为 Agent 永久失聪，只有 `Agent.start()` 能救。

## 本模块走真实入口（§7/§31）

- 真 `Agent` + 真 `Agent.start()`（含真 `_process_events` 消费者）；
- 真 `agent.process_message()` → 真 EventBus USER_INPUT → 真 `_handle_user_input`
  → 真 `CognitiveLoop.process()`；
- 真 `AgentFSM.transition()`（经 `FSMBackedPhaseController`），**不**做
  `fsm._state = …` 私有赋值；
- 取消源是**生产路径**：WS 停止按钮/语音取消走的那条
  `bus.post(InterruptData(source=…), priority=INTERRUPT)`（`ws_handlers/chat.py:136`）；
- 唯一的替身是**存储依赖**（`Agent._generation_repo.upsert` 用屏障阻塞）——Mongo
  写入本来就是一个会挂起数毫秒到数秒的真实挂起点（§44）。

## 不用 sleep（§45）

所有等待都是结构化信号：屏障 `Event`、`asyncio.Event`（FSM 再次进入 THINKING）、
任务句柄落定。`asyncio.wait_for` 的超时只是**死锁探测器**，不是判据本身（§46）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import pytest_asyncio

from core.agent.agent import Agent
from core.config import AppConfig
from core.monitoring.event_bus import EventPriority
from core.schema.event import InterruptData

#: 「本测试不碰真实库」的显式声明（见根 conftest 的真实库防线）。
pytestmark = [pytest.mark.service, pytest.mark.usefixtures("fake_storage")]

#: 死锁探测器——只在本模块真的挂死（无界 await）时触发。任何正常路径实测 < 100ms。
_PROBE_S = 10.0

#: 假 LLM 的固定回复。空响应会触发 nudge 重试 + 兜底文案，徒增噪声，故给一句正常回复。
_REPLY = "好呀，我在听。<mood>平静</mood>"


class _RawClient:
    """`Agent.start()` 的 task-system 步骤会读 `raw_client`（热切换判据）。"""

    def __init__(self, model_id: str) -> None:
        self.model = model_id

    def is_closed(self) -> bool:
        return False

    async def close(self) -> None:
        return None


class _ThinkingFakeLLM:
    """够用的 LLM 后端替身：流式吐一句带 `<mood>` 标签的回复。"""

    def __init__(self, response: str = _REPLY) -> None:
        self.response = response
        self.raw_client = _RawClient("fake/test")

    async def test(self) -> dict:
        return {"ok": True}

    def get_preprocess(self) -> None:
        """TTS 后端协议的一部分（`_cognitive.configure(tts=…)` 会读它）。"""
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


def _install_fake_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 LLM 工厂指向确定性替身（不连任何 provider）。"""
    from Module.llm import LLMFactory

    monkeypatch.setattr(
        LLMFactory, "create", staticmethod(lambda model_id, **kw: _ThinkingFakeLLM()))


class _BlockingUpsert:
    """**按需阻塞** upsert 的屏障——精确复现"回合挂在 Mongo 写入上"。

    每次 `arm()` 只挡下一次调用，`open()` 放行。用它钉死两个真实挂起点：
    ① 回合自己的 `GenerationRegistry.register`；② 取消恢复路径里的
    `GenerationRegistry.mark(cancelled)`（用它把恢复过程**停在 THINKING 上**，
    复现"用户按下停止后又立刻输入"的生产交错）。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.started = asyncio.Event()   # 已进入 upsert 且尚未返回
        self.release = asyncio.Event()   # 放行
        self.hits = 0
        self._armed = False

    def arm(self) -> None:
        """武装屏障（下一次 upsert 阻塞）。"""
        self._armed = True
        self.started.clear()
        self.release.clear()

    def open(self) -> None:
        """放行当前与之后被武装的调用。"""
        self.release.set()

    async def __call__(self, doc: dict) -> Any:
        self.hits += 1
        if self._armed:
            self._armed = False
            self.started.set()
            await self.release.wait()
        return await self._inner(doc)


class _TransitionRecorder:
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


async def _drain_task(task: asyncio.Task | None) -> None:
    """等回合任务落定（结构化终态信号）。超时 = 死锁探测。"""
    if task is None:
        return
    try:
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True),
                               timeout=_PROBE_S)
    except asyncio.TimeoutError:  # pragma: no cover — 只在真回归时触发
        raise AssertionError(f"回合任务未在 {_PROBE_S}s 内落定（疑似无界 await）") from None


async def _stop_interrupt(agent: Agent) -> None:
    """走**生产**取消源：WS 停止按钮 / 语音取消所用的 INTERRUPT 事件。"""
    agent.bus.post(InterruptData(source="websocket"), priority=EventPriority.INTERRUPT)


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


@pytest.mark.asyncio
async def test_cancelled_thinking_turn_recovers_and_next_message_is_processed(agent):
    """TSR-T1 + TSR-T2（PRE_FIX_RED）：THINKING 入口后的取消必须收口，且下一条消息能被处理。

    判据链（§20/§21）：
    1. 回合**确实**进入过 THINKING（不是断言 `state != THINKING` 的假绿）；
    2. 取消落在 `generation_registry.register` 这个挂起点上（屏障证明）；
    3. 回合任务落定后：FSM 不得停在 THINKING，且 `_process_lock` 不得仍被持有；
    4. 之后第二条**真实用户消息**必须能开始一个新回合并拿到回复。
    """
    recorder = _TransitionRecorder()
    agent.status.on_change(recorder)
    gate = _BlockingUpsert(agent._generation_repo)  # noqa: SLF001 — 只替存储依赖
    gate.arm()
    agent._generation_repo.upsert = gate  # type: ignore[method-assign]  # noqa: SLF001

    # ── 1. 第一条消息：真实入口 → 真 USER_INPUT → 真 process() ──
    assert await agent.process_message("第一条") is True
    await asyncio.wait_for(gate.started.wait(), timeout=_PROBE_S)

    assert "thinking" in recorder.names, (
        "用例前提不成立：回合从未进入 THINKING —— 断言无判别力。" + recorder.diagnose())
    assert agent.status.current_name() == "thinking", recorder.diagnose()
    assert agent._cognitive.process_lock.locked() is True, "回合在 THINKING 期间必须持有 process lock"  # noqa: SLF001

    turn_a = agent._cognitive.current_task  # noqa: SLF001
    assert turn_a is not None

    # ── 2. 生产取消源：INTERRUPT 事件（真消费者 → 真 cancel_current） ──
    await _stop_interrupt(agent)
    await _drain_task(turn_a)

    # ── 3. 取消后不得留下无主的 THINKING（TSR-1/TSR-2/TSR-4） ──
    assert agent.status.current_name() != "thinking", (
        "回合已被取消，FSM 仍停在 THINKING（KB-22：生命周期失去 owner）。"
        + recorder.diagnose())
    assert agent.status.is_completely_idle, (
        "取消后 FSM 未回到空闲——后续输入无法推进。" + recorder.diagnose())
    assert "interrupted" in recorder.names, (
        "取消没有走 canonical 中断路径（THINKING → INTERRUPTED → IDLE）。"
        + recorder.diagnose())
    assert agent._cognitive.process_lock.locked() is False, "回合结束后 process lock 不得仍被持有"  # noqa: SLF001

    # ── 4. 第二条用户消息必须能开始新回合并拿到回复（TSR-5） ──
    second_thinking = recorder.watch("thinking")
    assert await agent.process_message("第二条") is True
    try:
        await asyncio.wait_for(second_thinking.wait(), timeout=_PROBE_S)
    except asyncio.TimeoutError:  # pragma: no cover — PRE_FIX_RED 命中处
        raise AssertionError(
            "第二条用户消息没有被处理（Agent 永久失聪）：FSM="
            f"{agent.status.current_name()} pending={len(agent.pending_messages)} "
            + recorder.diagnose()) from None

    await _drain_task(agent._cognitive.current_task)  # noqa: SLF001
    assert agent.status.is_completely_idle, recorder.diagnose()
    assert not agent.pending_messages, (
        f"第二条消息被积压在 pending 队列：{[p.text for p in agent.pending_messages]}")
    user_texts = [m.content for m in agent.session.messages]  # type: ignore[union-attr]
    assert any("第二条" in str(t) for t in user_texts), (
        "第二条消息没有进入会话（未被真正处理）。" + recorder.diagnose())


@pytest.mark.asyncio
async def test_message_queued_during_recovery_window_is_processed(agent):
    """TSR-T2 / TSR-T5（PRE_FIX_RED）：恢复窗口里到达的消息必须重新获得处理权。

    这条钉住 KB-22 的**用户影响机制**：`process_message` 在相位忙时把消息放进
    `_pending_messages` 并返回 True（已 ACK），而 pending 只在
    `STATE_CHANGE → IDLE` 时出队（`Agent._handle_state_change`，`agent.py:1054`）。
    FSM 若永远回不到 IDLE，这条已 ACK 的消息就永远得不到处理——"永久失聪"的
    可观测形态。

    生产交错（全部真实入口，取消源是**用户可见的模型切换**）：

    1. 回合 A 停在 THINKING 窗口（generation 建档的 Mongo upsert）；
    2. 用户在**恢复尚未完成**时切换模型——`Agent.switch_llm`
       （`core/agent/server.py:392`，前端模型下拉框走 `switch_model`）
       → `_cognitive.cancel_current()`。该路径**不**清空 `_pending_messages`
       （与显式 INTERRUPT / stop 的前置清空不同），是 pending 能在取消后存活的
       生产来源；
    3. 同一窗口内用户又发一条 → 相位仍忙 → 已 ACK 地进 pending；
    4. 恢复完成 → `STATE_CHANGE → IDLE` → **既有** drain 机制出队 → 新回合处理它。

    修前：第 2 步的取消逃出 `process()`，FSM 永久停在 THINKING ⇒ 第 3 步的消息
    永远停在 pending（永久失聪），只有 `Agent.start()` 能救。
    """
    recorder = _TransitionRecorder()
    agent.status.on_change(recorder)
    gate = _BlockingUpsert(agent._generation_repo)  # noqa: SLF001
    agent._generation_repo.upsert = gate  # type: ignore[method-assign]  # noqa: SLF001

    # ── 1. 回合 A 停在 THINKING 窗口 ──
    gate.arm()
    assert await agent.process_message("第一条") is True
    await asyncio.wait_for(gate.started.wait(), timeout=_PROBE_S)
    assert agent.status.current_name() == "thinking", recorder.diagnose()
    turn_a = agent._cognitive.current_task  # noqa: SLF001
    assert turn_a is not None

    # ── 2. 用户切模型 → cancel_current；把恢复过程停在 generation mark 上 ──
    gate.arm()
    assert (await agent.switch_llm("deepseek/deepseek-v4-pro"))["ok"] is True
    try:
        await asyncio.wait_for(gate.started.wait(), timeout=_PROBE_S)
    except asyncio.TimeoutError:  # pragma: no cover — PRE_FIX_RED 命中处
        raise AssertionError(
            "取消没有进入 canonical 恢复路径（THINKING 仍在，恢复未发生）：FSM="
            f"{agent.status.current_name()} lock={agent._cognitive.process_lock.locked()} "  # noqa: SLF001
            f"turn_done={turn_a.done()} " + recorder.diagnose()) from None
    assert agent.status.current_name() == "thinking", (
        "恢复尚未走完（仍在 generation mark 的 await 上）时相位应仍是 THINKING。"
        + recorder.diagnose())

    # ── 3. 同一窗口内再发一条：相位忙 → 排队（已 ACK） ──
    assert await agent.process_message("第二条") is True
    assert [p.text for p in agent.pending_messages] == ["第二条"], (
        "前置条件不成立：恢复窗口内到达的消息没有进 pending 队列")

    # ── 4. 放行恢复：pending 必须经既有 drain 机制重新获得处理权 ──
    second_thinking = recorder.watch("thinking")
    gate.open()
    await _drain_task(turn_a)
    assert "interrupted" in recorder.names, (
        "恢复没有走 canonical 中断路径。" + recorder.diagnose())
    try:
        await asyncio.wait_for(second_thinking.wait(), timeout=_PROBE_S)
    except asyncio.TimeoutError:  # pragma: no cover — PRE_FIX_RED 命中处
        raise AssertionError(
            f"pending 消息未被推进（永久失聪）：FSM={agent.status.current_name()} "
            f"pending={[p.text for p in agent.pending_messages]} " + recorder.diagnose()) from None

    await _drain_task(agent._cognitive.current_task)  # noqa: SLF001
    assert agent.status.is_completely_idle, recorder.diagnose()
    assert not agent.pending_messages, (
        f"pending 队列未被排空：{[p.text for p in agent.pending_messages]}")
    user_texts = [m.content for m in agent.session.messages]  # type: ignore[union-attr]
    assert any("第二条" in str(t) for t in user_texts), (
        "排在 pending 里的消息没有进入会话（未被真正处理）。" + recorder.diagnose())


@pytest.mark.asyncio
async def test_normal_turn_is_untouched(agent):
    """TSR-T4（ALREADY_GREEN_GUARD）：正常一轮不得因为补齐取消边界而改变。

    `process()` 的 prepare 阶段被包进外层 try 之后，正常路径必须逐字保持：
    THINKING → SPEAKING → IDLE，且**不得**出现任何中断收口
    （`interrupted` 不在状态序列里）。
    """
    recorder = _TransitionRecorder()
    agent.status.on_change(recorder)
    finished = recorder.watch("idle")

    assert await agent.process_message("你好") is True
    await asyncio.wait_for(finished.wait(), timeout=_PROBE_S)
    await _drain_task(agent._cognitive.current_task)  # noqa: SLF001

    assert agent.status.is_completely_idle, recorder.diagnose()
    assert recorder.names == ["thinking", "speaking", "idle"], (
        f"正常回合的状态序列被改动：{recorder.names}")
    assert not agent.pending_messages, "正常回合不该留下 pending 消息"


@pytest.mark.asyncio
async def test_stop_during_thinking_window_does_not_strand_the_agent(monkeypatch):
    """TSR-T5（PRE_FIX_RED）：停机取消落在 THINKING 窗口时，不得留下卡死的相位。

    合法目标**不是**"恢复成可接受新输入的 IDLE"：停机期间 `run_state` 已是
    `STOPPED`，生命周期轴本就拒收（`process_message` 返回 False）。本用例断言的是
    两件事同时成立：相位不得停在 THINKING（否则下次 `start()` 之前任何检查都看到
    一个无主的 THINKING），且停机语义不被取消收口破坏。
    """
    from core.agent._types import RunState

    _install_fake_llm(monkeypatch)
    a = Agent(config=AppConfig())
    await a.start()
    recorder = _TransitionRecorder()
    a.status.on_change(recorder)
    stopped = False
    try:
        gate = _BlockingUpsert(a._generation_repo)  # noqa: SLF001
        gate.arm()
        a._generation_repo.upsert = gate  # type: ignore[method-assign]  # noqa: SLF001

        assert await a.process_message("停机前的最后一条") is True
        await asyncio.wait_for(gate.started.wait(), timeout=_PROBE_S)
        assert a.status.current_name() == "thinking", recorder.diagnose()
        turn_a = a._cognitive.current_task  # noqa: SLF001
        assert turn_a is not None

        await a.stop()
        stopped = True
        await _drain_task(turn_a)

        assert a.status.current_name() != "thinking", (
            "停机取消后相位仍停在 THINKING（下次 start() 之前的无主状态）。"
            + recorder.diagnose())
        assert a.state is RunState.STOPPED
        assert "interrupted" in recorder.names, recorder.diagnose()
        assert a._cognitive.process_lock.locked() is False, recorder.diagnose()  # noqa: SLF001
        assert await a.process_message("停机窗口内的消息") is False, (
            "停机语义被破坏：生命周期轴应当拒收")
    finally:
        gate = a._generation_repo  # noqa: SLF001
        if isinstance(gate, _BlockingUpsert):
            gate.open()
        if not stopped:
            await a.stop()
