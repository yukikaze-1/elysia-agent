# 测试文件：core/agent/tests/test_runtime_epoch_input_ownership.py
"""Batch 14（**KB-47 / 原 N8**）：停机边界必须结束"非 durable 已接受输入"的 ownership。

## 缺陷形状

`EventBus` 的生命周期与 `Agent` 实例相同（`EventBus()` 在 `Agent.__init__` 构造），
而同一实例的 `stop() → start()` 是**新的 runtime epoch**（`start()` 会 `fsm.reset()`、
`reset_lifecycle_state()`、重开 `_started_cognitive_tasks`、重新 spawn `_process_events`）。
`Agent.stop()` 清 `_pending_messages`，却**不清总线**：

```text
事件消费者被钉在某个真实挂起点（如 _handle_state_change → output.notify_state）
  → process_message(M1) → bus.post(UserInputData(M1)) → return True（= 已 accepted）
  → Agent.stop()：_state=STOPPED / pending 清空 / 消费者取消 —— 总线里 M1 仍在
  → Agent.start()：新消费者取到的第一条事件就是 M1 ⇒ 被当作**新输入**处理
```

后果是**旧 epoch 里已被放弃的非 durable 输入在新 epoch 复活**：它进入 session 历史、
被喂给 LLM、并可能再次触发工具副作用（用户重启后重发 ⇒ 同一条指令执行两次）。
既有不变量 AD5（"显式取消与**停机**不得复活本应保持取消的输入"）与
`docs/architecture/invariants.md` §4.6 已把「`Agent.stop()` 清空 bus + pending」写成
既有终态合同，但代码只做了 pending 那一半。

## 合同（Phase 0 结论）

- `Agent.stop()` 是**runtime epoch 边界**：`reset_lifecycle_state` /
  `clear_work`（"避免同一 Agent restart 残留"）/ `reopen_all_archives` 都按
  "同一实例可重启"写；`core/input/ws_handlers/chat.py` 与 `voice_sessions.py` 的
  S2-R2-4 判据明说"残留事件跨运行被消费**本身就是缺陷族**"。
- 因此：**停机时仍驻留总线的 USER_INPUT = 未被任何回合接管的非 durable accepted
  work**，其 ownership 必须在边界处结束（可归因丢弃），而**不得**跨 epoch 复活。
- **硬边界**：`TaskResultData` 是 durable 终态（§T3/T6、`_redeliver_pending_task_results`），
  停机清扫**只按 event type 分类**、不得变成 `clear()`——否则用户永久等不到后台任务结果。

## 判据与层次

每条用例都用 `asyncio.Event` 屏障把消费者钉在**生产代码本来就会挂起**的调用上
（`OutputManager.notify_state` ← `Agent._handle_state_change`），不用 sleep 造 race；
`asyncio.wait_for` 只作死锁探测器。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
import pytest_asyncio

from core.agent.agent import Agent
from core.agent.tests.test_admission_input_delivery import (
    _Gate,
    _install_fake_llm,
    _RecordingFakeLLM,
    _settle,
)
from core.config import AppConfig
from core.monitoring.event_bus import EventPriority
from core.schema.event import TaskResultData, UserInputData

pytestmark = [pytest.mark.service, pytest.mark.usefixtures("fake_storage")]

#: 死锁探测器——只在真的挂死（无界 await）时触发。
_PROBE_S = 10.0

#: 本文件使用的稳定身份 token（用身份比较，不用重复文本）。
_TOKEN = "M1-before-stop"


# ---------------------------------------------------------------- harness


@pytest_asyncio.fixture
async def llm(monkeypatch: pytest.MonkeyPatch) -> _RecordingFakeLLM:
    """确定性 LLM 替身（不连任何 provider）。"""
    return _install_fake_llm(monkeypatch)


@pytest_asyncio.fixture
async def agent(llm: _RecordingFakeLLM):
    """真 Agent + 真 `start()`（含真 `_process_events` 消费者）＋假存储/假 LLM。

    与 Batch 12/13 的 fixture 同构：先跑一条**完整暖场消息**，让开局状态（缓存热、
    无在途回合、pending 空、总线空）显式且确定。
    """
    a = Agent(config=AppConfig())
    await a.start()
    try:
        assert await a.process_message("（暖场）") is True
        await _drain_turns(a)
        assert a.status.is_completely_idle, a.status.current_name()
        assert not a.pending_messages
        assert a.bus.pending_user_inputs == 0
        yield a
    finally:
        await a.stop()


async def _drain_turns(a: Agent) -> None:
    """等**所有**回合落定（含 pending 出队 spawn 的那些）。"""
    for _ in range(400):
        await asyncio.sleep(0)
        task = a._cognitive.current_task  # noqa: SLF001 — 只读任务句柄
        if task is not None and not task.done():
            await _settle(task)
            continue
        if a.pending_messages or a.bus.pending_user_inputs:
            continue
        return
    raise AssertionError(
        f"回合/pending 未排空：pending={[p.text for p in a.pending_messages]} "
        f"bus={a.bus.pending_user_inputs} phase={a.status.current_name()}")


async def _pump_until(predicate: Any, what: str) -> None:
    """让出事件循环直到谓词为真（结构化推进，不用固定 sleep 造 race）。"""
    for _ in range(4000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"等待超时：{what}")


def _user_texts(a: Agent) -> list[str]:
    """会话里 user 消息的**纯文本**（不是 parts 的 repr）。"""
    out: list[str] = []
    for m in a.session.messages:
        if m.role != "user":
            continue
        parts = m.content if isinstance(m.content, list) else []
        out.append("".join(str(getattr(p, "content", "")) for p in parts))
    return out


def _install_blocked_consumer(a: Agent) -> _Gate:
    """把**事件消费者**钉在 `_handle_state_change → output.notify_state` 上。

    这是生产代码里本来就会挂起的调用（对每个通道 await 一次出站帧；`_CHANNEL_SEND_TIMEOUT`
    之下单帧可占数百毫秒到数秒），因此"消费者落后于总线"是**真实**形态，不是替身时序。
    钉住之后，任何被 accepted 的输入都只能驻留在总线里（消费者不再出队）。
    """
    gate = _Gate(a.output.notify_state)
    a.output.notify_state = gate  # type: ignore[method-assign]
    gate.arm()
    return gate


async def _park_consumer_with_bus_resident_input(a: Agent) -> _Gate:
    """把消费者钉住，然后从**真实公共入口**接受一条输入，断言它驻留总线。

    Returns:
        仍处于挂起状态的 `_Gate`（消费者被钉住）。
    """
    gate = _install_blocked_consumer(a)
    # 真实回合：消费者会在派发它的 STATE_CHANGE(THINKING) 时挂住。
    assert await a.process_message("W") is True
    await asyncio.wait_for(gate.entered.wait(), timeout=_PROBE_S)
    await _drain_turns(a)                    # 回合本身可以在消费者挂起时正常跑完
    assert a.status.is_completely_idle, a.status.current_name()

    assert await a.process_message(_TOKEN) is True
    assert a.bus.pending_user_inputs == 1, (
        f"用例前提未达成：输入未驻留总线 (bus={a.bus.pending_user_inputs})")
    assert not a.pending_messages
    return gate


# ---------------------------------------------------------------- guards


@pytest.mark.asyncio
async def test_stop_boundary_ends_input_ownership_and_prevents_resurrection(
        agent: Agent, llm: _RecordingFakeLLM, caplog: pytest.LogCaptureFixture):
    """KB-47（**PRE_FIX_RED**）：停机边界结束未接管输入的 ownership，重启后不得复活。

    修前实测（同一用例的探针形态）：`stop()` 之后 `bus.pending_user_inputs == 1`；
    `start()` 之后该输入被当作新输入处理——进入 session 历史（第三层）、被喂给 LLM
    （第四层），且**零 disposition**（第五层）。
    """
    await _park_consumer_with_bus_resident_input(agent)
    # 计数基准取在**驻留已成立之后**（`W` 那条真实回合已经跑完）。
    calls_before = llm.stream_calls
    assert _TOKEN not in "".join(llm.seen_prompts)

    with caplog.at_level(logging.INFO):
        await agent.stop()
    assert not agent.status.is_service_running

    # ★ 第一层：ownership 在边界处结束（不是"留着等 restart 再过滤"）。
    assert agent.bus.pending_user_inputs == 0, (
        "停机边界未结束未接管输入的 ownership：总线仍驻留 "
        f"{agent.bus.pending_user_inputs} 条 UserInputData "
        f"(ids={agent.bus.pending_user_input_request_ids()})")

    # ★ 第二层：丢弃必须**可归因**（不得静默；AD2 同口径）。
    dispositions = [r.getMessage() for r in caplog.records if "disposition=" in r.getMessage()]
    assert any("cancelled_by_shutdown" in msg for msg in dispositions), (
        f"停机丢弃已接受的输入却没有可归因终态：{dispositions}")

    # ★ 第三/四层：新 epoch 不得复活它（不进 session、不喂 LLM）。
    await agent.start()
    await _pump_until(
        lambda: not agent.bus.pending_user_inputs
        and (agent._cognitive.current_task is None or agent._cognitive.current_task.done()),  # noqa: SLF001
        "新 epoch 落定")
    await _drain_turns(agent)

    assert _TOKEN not in _user_texts(agent), (
        f"停机丢弃的输入在新 epoch 复活并写进了会话历史：{_user_texts(agent)}")
    assert llm.stream_calls == calls_before, (
        f"停机丢弃的输入在新 epoch 被喂给了 LLM（{calls_before} → {llm.stream_calls}）")
    assert _TOKEN not in "".join(llm.seen_prompts), (
        "停机丢弃的输入出现在新 epoch 的 LLM prompt 里")


@pytest.mark.asyncio
async def test_stop_boundary_preserves_durable_events(agent: Agent):
    """KB-47 负向守卫（§34/§59 硬边界）：清扫**只**按 event type 分类，不得成为全清。

    这条守卫杀的是"顺手把总线清空"的修法：`TaskResultData` 是 durable 终态
    （T3/T6 与 `_redeliver_pending_task_results` 的语义都依赖它不被丢），
    停机清扫必须把它**原位保留**。
    """
    gate = _install_blocked_consumer(agent)
    assert await agent.process_message("W") is True
    await asyncio.wait_for(gate.entered.wait(), timeout=_PROBE_S)
    await _drain_turns(agent)

    # 手工放两条事件：这里测的是**分类语义**，不声称这两条的生产可达性
    # （UserInputData 的生产形态由上面那条用例用真实入口覆盖）。
    agent.bus.post(UserInputData(
        text="stale", user_name="u", request_id="999", protocol_version=1))
    agent.bus.post(TaskResultData(
        task_id="t-1", result="done", original_intent="intent",
        dispatched_at="2026-09-19T00:00:00Z"), priority=EventPriority.NORMAL)
    assert agent.bus.pending_user_inputs == 1

    await agent.stop()

    assert agent.bus.pending_user_inputs == 0, "停机清扫必须移除未接管的 user input"
    # 幸存者里必须**仍有** durable 终态、且**不再有** user input（分类语义）。
    survivors = [type(ev.data).__name__ for ev in agent.bus.drain()]
    assert "TaskResultData" in survivors, (
        f"停机清扫误伤了 durable 事件（T3/T6 的用户级终态）：survivors={survivors}")
    assert "UserInputData" not in survivors, (
        f"停机清扫漏掉了 user input：survivors={survivors}")


@pytest.mark.asyncio
async def test_stop_boundary_attributes_queued_pending_input(
        agent: Agent, llm: _RecordingFakeLLM, caplog: pytest.LogCaptureFixture):
    """RO1 的另一半（**PRE_FIX_RED**，Challenger 反证补全）：停机清空 pending 必须可归因。

    停机边界有两个容器要结束 ownership：**总线驻留**（上面那条用例）与
    `_pending_messages`。后者一直是清空的，但修前**零日志**——而停止按钮路径
    （`_handle_interrupt`）对同一类处置记 `disposition=cancelled_by_interrupt`。
    于是同一个"被放弃的已接受输入"在观测面上一半可归因、一半不可归因。

    这条也是本族里**唯一生产可达**的静默丢弃形态：用户在回合进行中连发两条，
    然后 `/restart` 或 SIGTERM ⇒ 排队那条消失且无任何日志。
    """
    llm.arm_stream_gate()
    assert await agent.process_message("M0-streaming") is True
    await asyncio.wait_for(llm.stream_entered.wait(), timeout=_PROBE_S)

    # 回合在途（相位 THINKING）⇒ 第二条走忙时排队路径，同步进 pending。
    assert await agent.process_message("M-queued") is True
    assert [p.text for p in agent.pending_messages] == ["M-queued"], (
        f"用例前提未达成：{agent.pending_messages}")

    with caplog.at_level(logging.INFO):
        llm.open_stream_gate()
        await agent.stop()

    assert not agent.pending_messages, "停机必须清空 pending（PO6）"
    dispositions = [r.getMessage() for r in caplog.records if "disposition=" in r.getMessage()]
    assert any("cancelled_by_shutdown" in msg and "排队消息" in msg for msg in dispositions), (
        f"停机清空排队消息没有可归因终态（与停止按钮路径的处置不对称）：{dispositions}")


@pytest.mark.asyncio
async def test_new_epoch_still_accepts_and_processes_fresh_input(
        agent: Agent, llm: _RecordingFakeLLM):
    """ALREADY_GREEN_GUARD / 活性：边界清扫不得让新 epoch 失去处理能力。

    清扫只作用于**停机那一刻**总线里的残留；`start()` 之后的新输入走的是同一条
    既有快速路径（`process_message` → `bus.post` → 消费者 → 回合），必须照常被处理。
    """
    await _park_consumer_with_bus_resident_input(agent)
    await agent.stop()
    await agent.start()

    assert await agent.process_message("M2-after-restart") is True
    await _pump_until(
        lambda: "M2-after-restart" in _user_texts(agent), "新 epoch 处理新输入")
    await _drain_turns(agent)

    assert "M2-after-restart" in _user_texts(agent)
    assert agent.status.is_completely_idle, agent.status.current_name()
