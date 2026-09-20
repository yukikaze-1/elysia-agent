# 测试文件：core/agent/tests/test_system_event_preemption.py
"""KB-41（Residual Correctness Closure）：系统事件不得抢占**尚未接管输入**的回合。

## 缺陷形状

用户输入路径有一道 ownership 闸门（C1/P1-06 → KB-24/Batch 12）：

```python
not_owning = (prev is not None and not prev.done()
              and (prev not in self._started_cognitive_tasks
                   or not self._cognitive.current_turn_owns_input))
```

`_handle_user_input` 用它决定"排队 vs 抢占"。但**非用户来源**的两条事件处理
（`_handle_system_tick` 的主动搭话、`_handle_task_result` 的后台任务终态唤醒）
**没有**这道判据，直接 `self._cognitive.cancel_current()`。

于是：一条用户输入已经 `accepted`（WS 已回 ACK），回合已 first-step 但还停在
`_prepare_turn` 的挂起点（会话加载 / 角色 runtime registry）——这正是
`current_turn_owns_input is False` 的 admission 窗口——此时一个后台任务恰好完成
或一个 proactive tick 到达，就把那个回合连人带输入一起取消。取消落在窗口内的后果
与 KB-24 完全一样：`cancelled_pre_ownership`、不进 session、不进 pending、
EventBus 不重投 ⇒ **已 ACK 的用户输入静默消失**。

窗口窄（tick 门是相位 IDLE + 1s 轮询；task result 要恰好完成）但真实，故等级
INTERNAL/LOW。

## 本文件钉住的合同（SE1/SE2）

- **SE1（no silent loss）**：已 accepted 的用户输入在系统事件抢占下必须有归宿——
  被处理、进 pending，或有**显式终态**；不得无声消失。
- **SE2（liveness）**：修法不得让系统事件饿死。SYSTEM_TICK 可重新生成
  （`IdleMonitor` 1s 轮询 + 相位门），故窗口内**放弃**是安全的；TASK_RESULT 必须
  送达（durable 语义），故窗口内**推迟**并在用户回合落定后重新入队。

**两类的差别是刻意的，不是不对称的疏漏**：tick 可再生、结果不可再生。

## 不用 sleep 造 race

复用 Batch 12 的 `_Gate` 屏障（装在**真实**挂起点
`CharacterRuntimeRegistry.get_or_load` 上），把回合钉死在窗口里再投系统事件。
`asyncio.wait_for` 只作死锁探测器，不是判据。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from core.agent.agent import Agent
from core.config.app_config import AppConfig
from core.schema.agent import EventPriority
from core.schema.event import SystemTickData, TaskResultData

pytestmark = [pytest.mark.service, pytest.mark.usefixtures("fake_storage")]

# ---- 复用 Batch 12 的屏障与推进工具（同一份实现，避免两份各自漂移的替身） ----

_TESTS_DIR = Path(__file__).resolve().parent


def _load_batch12() -> Any:
    """按文件路径加载 `test_admission_input_delivery`（tests 目录不是包）。"""
    name = "core.agent.tests.test_admission_input_delivery"
    if name in sys.modules:
        return sys.modules[name]
    path = _TESTS_DIR / "test_admission_input_delivery.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_B12 = _load_batch12()
_Gate = _B12._Gate
_install_fake_llm = _B12._install_fake_llm
_arm_admission_gap = _B12._arm_admission_gap
_settle = _B12._settle
_pump_until = _B12._pump_until
_drain_turns = _B12._drain_turns
_user_texts = _B12._user_texts
_pending_texts = _B12._pending_texts
_PROBE_S = _B12._PROBE_S

_TICK_TEXT = "用户已经"          # proactive tick 注入文本的特征片段
_TASK_RESULT_TEXT = "任务结果正文"


@pytest_asyncio.fixture
async def llm(monkeypatch: pytest.MonkeyPatch):
    return _install_fake_llm(monkeypatch)


@pytest_asyncio.fixture
async def agent(llm):
    """真 Agent + 真 `start()` + 假存储/假 LLM + 一条暖场回合（与 Batch 12 同构）。"""
    a = Agent(config=AppConfig())
    await a.start()
    try:
        assert await a.process_message("（暖场）") is True
        await _drain_turns(a)
        assert a.status.is_completely_idle, a.status.current_name()
        yield a
    finally:
        await a.stop()


@pytest_asyncio.fixture
async def admission_gate(agent: Agent) -> Any:
    registry = agent._runtime_registry  # noqa: SLF001 — 与 Batch 12 同一处包装
    gate = _Gate(registry.get_or_load)
    registry.get_or_load = gate  # type: ignore[method-assign]
    return gate


def _post_tick(agent: Agent, elapsed: float = 120.0) -> None:
    agent.bus.post(SystemTickData(reason="silence_timeout", elapsed=elapsed))


def _post_task_result(agent: Agent, task_id: str = "wf-kb41") -> None:
    agent.bus.post(TaskResultData(
        task_id=task_id,
        result="任务结果正文",
        original_intent="帮我查一下",
        dispatched_at="2026-09-20T00:00:00+00:00",
    ))


def _llm_saw(llm: Any, needle: str) -> int:
    """LLM 收到过多少次含 `needle` 的提示（判定"这个回合真的跑过"）。

    与 Batch 12 同源：确定性替身记录每次流式调用的提示文本（`llm.seen_prompts`）。
    """
    return sum(1 for prompt in llm.seen_prompts if needle in prompt)


def _deferred(agent: Agent) -> list:
    """推迟队列的**容错读取**：修复前的代码没有这个字段。

    用例必须能在修复前的代码上跑到**断言**（红在缺陷上），而不是当场 AttributeError
    （红在一个不存在的符号上）——与 Batch 12 的 `_UNKNOWN` 同一纪律。
    """
    return list(getattr(agent, "_deferred_task_results", []))


async def _wait_deferred(agent: Agent, count: int) -> None:
    await _pump_until(
        lambda: len(_deferred(agent)) >= count, f"deferred_task_results 达到 {count} 条")


async def _run_one_turn(agent: Agent, *, what: str) -> None:
    """等一个**自然产生**的回合（tick / 推迟结果）跑完。

    不能只用 `_pump_until`：它每次只 `sleep(0)` 让出一步，而一个回合要跨
    `_process_lock` → 会话解析 → runtime registry → `_stream_llm_response` 好几段
    await（实测 12 次里偶发 2 次在预算内还没走到流式调用）。这里用**结构化信号**：
    先等"出现可等待的回合任务"，再 `_settle` 它——判据是任务落定，不是让出次数。
    """
    loop = agent._cognitive  # noqa: SLF001 — 只读任务句柄
    await _pump_until(
        lambda: loop.current_task is not None and not loop.current_task.done(), what)
    await _settle(loop.current_task)


# ---- SE-T1 / SE-T2：窗口内的系统事件不得吃掉已 ACK 的输入 ----


@pytest.mark.asyncio
async def test_se_t1_accepted_input_survives_system_tick_in_window(
        agent: Agent, admission_gate: Any, llm):
    """SE-T1（PRE_FIX_RED）：admission 窗口内的 proactive tick 不得取消用户回合。

    修前：`_handle_system_tick` 无条件 `cancel_current()` ⇒ 用户那条已 ACK 的输入以
    `cancelled_pre_ownership` 收场（session 里查不到、pending 里也没有）。
    """
    first = await _arm_admission_gap(agent, admission_gate, "窗口内的用户消息")

    _post_tick(agent)
    # 让事件消费者真的派发到这条 tick（无固定 sleep：等"总线被消费 + tick 已处置"）
    await _pump_until(lambda: agent.bus.pending_user_inputs == 0, "总线被消费")
    await _pump_until(lambda: _llm_saw(llm, _TICK_TEXT) == 0, "tick 未被当回合执行")

    assert first.cancelled() is False, (
        "窗口内的 SYSTEM_TICK 取消了尚未接管输入的回合 —— 已 ACK 的输入随之静默消失")
    assert first.done() is False, "窗口内回合不得提前结束"

    admission_gate.open()
    await _settle(first)
    await _drain_turns(agent)

    users = _user_texts(agent)
    assert any("窗口内的用户消息" in t for t in users), (
        f"已 ACK 的用户输入从未进入会话（静默丢失）：{users}")
    assert not agent.pending_messages, _pending_texts(agent)
    assert agent.status.is_completely_idle, agent.status.current_name()


@pytest.mark.asyncio
async def test_se_t2_accepted_input_survives_task_result_in_window(
        agent: Agent, admission_gate: Any, llm):
    """SE-T2（PRE_FIX_RED）：admission 窗口内的 task result 不得取消用户回合。

    与 SE-T1 同根（都是"非用户来源的无条件抢占"），但**处置不同**：结果必须送达 ⇒
    窗口内**推迟**（进 `_deferred_task_results`）、用户回合落定后重新入队并真的跑起来。
    """
    first = await _arm_admission_gap(agent, admission_gate, "窗口内的用户消息")

    _post_task_result(agent, "wf-kb41-t2")
    await _wait_deferred(agent, 1)

    assert first.cancelled() is False, (
        "窗口内的 TASK_RESULT 取消了尚未接管输入的回合 —— 已 ACK 的输入随之静默消失")
    assert first.done() is False

    admission_gate.open()
    await _settle(first)
    await _drain_turns(agent)
    await _drain_turns(agent)          # 推迟的结果会在回合落定后的回调里重新入队

    users = _user_texts(agent)
    assert any("窗口内的用户消息" in t for t in users), (
        f"已 ACK 的用户输入从未进入会话（静默丢失）：{users}")
    # 推迟的结果最终被处理（唤醒回合真的跑过）
    assert _llm_saw(llm, _TASK_RESULT_TEXT) >= 1, (
        "被推迟的后台任务结果没有被重新投递 —— 用户永远等不到结果")
    assert not _deferred(agent), "推迟队列未排空"


# ---- SE-T3 / SE-T4：正常路径与多输入 ----


@pytest.mark.asyncio
async def test_se_t3_system_event_first_then_user_input(agent: Agent, llm):
    """SE-T3：系统事件先到（无在途回合）⇒ 照常处理，不被新判据误伤。"""
    _post_tick(agent, elapsed=300.0)
    await _run_one_turn(agent, what="proactive 回合出现")
    await _drain_turns(agent)
    assert _llm_saw(llm, _TICK_TEXT) >= 1, "tick 没有被当回合执行（系统事件被饿死）"
    assert agent.status.is_completely_idle, agent.status.current_name()


@pytest.mark.asyncio
async def test_se_t4_multi_input_with_system_event_keeps_fifo(
        agent: Agent, admission_gate: Any, llm):
    """SE-T4：M1 在窗口内、M2 排队、期间来一个 tick ⇒ M1/M2 都进历史且顺序不变。"""
    first = await _arm_admission_gap(agent, admission_gate, "M1")
    assert await agent.process_message("M2") is True
    await _pump_until(lambda: _pending_texts(agent) == ["M2"], "M2 进 pending")

    _post_tick(agent)
    await _pump_until(lambda: agent.bus.pending_user_inputs == 0, "总线被消费")
    await _pump_until(lambda: _llm_saw(llm, _TICK_TEXT) == 0, "tick 未被当回合执行")

    assert first.cancelled() is False
    admission_gate.open()
    await _settle(first)
    await _drain_turns(agent)

    users = _user_texts(agent)
    m1 = next((i for i, t in enumerate(users) if "M1" in t), None)
    m2 = next((i for i, t in enumerate(users) if "M2" in t), None)
    assert m1 is not None and m2 is not None, f"M1/M2 未都进历史：{users}"
    assert m1 < m2, f"FIFO 被破坏：{users}"


# ---- SE-T5 / SE-T6 / SE-T7：不回归 + liveness ----


@pytest.mark.asyncio
async def test_se_t5_pending_fifo_not_regressed(agent: Agent, admission_gate: Any, llm):
    """SE-T5：pending FIFO 合同（PO1–PO6）不因 KB-41 的改动而回归。"""
    first = await _arm_admission_gap(agent, admission_gate, "P1")
    assert await agent.process_message("P2") is True
    assert await agent.process_message("P3") is True
    await _pump_until(lambda: len(agent.pending_messages) == 2, "P2/P3 都进 pending")
    assert _pending_texts(agent) == ["P2", "P3"], _pending_texts(agent)

    admission_gate.open()
    await _settle(first)
    await _drain_turns(agent)

    users = _user_texts(agent)
    order = [t for t in users if any(k in t for k in ("P1", "P2", "P3"))]
    assert [k for t in order for k in ("P1", "P2", "P3") if k in t] == ["P1", "P2", "P3"], (
        f"pending FIFO 被破坏：{order}")


@pytest.mark.asyncio
async def test_se_t6_no_silent_loss_under_event_flood(
        agent: Agent, admission_gate: Any, llm):
    """SE-T6：窗口内连投多个系统事件，已 ACK 的输入仍不得消失。"""
    first = await _arm_admission_gap(agent, admission_gate, "洪流中的用户消息")
    for i in range(4):
        _post_tick(agent, elapsed=100.0 + i)
    _post_task_result(agent, "wf-kb41-t6")

    await _pump_until(lambda: agent.bus.pending_user_inputs == 0, "总线被消费")
    assert first.cancelled() is False, "系统事件洪流吃掉了已 ACK 的用户输入"

    admission_gate.open()
    await _settle(first)
    await _drain_turns(agent)
    await _drain_turns(agent)

    users = _user_texts(agent)
    assert any("洪流中的用户消息" in t for t in users), f"输入消失：{users}"
    assert _llm_saw(llm, _TASK_RESULT_TEXT) >= 1, "被推迟的任务结果始终没被投递"


@pytest.mark.asyncio
async def test_se_t7_system_event_not_starved(agent: Agent, llm):
    """SE-T7（liveness）：系统事件不得因为 KB-41 的判据而永远得不到执行。

    形态：一个 proactive 回合**真的跑起来**（证明 tick 不被饿死），再在其后投一条
    用户输入（证明两条都能落地）。修法若"永远推迟/永远放弃 tick"，这条会红。
    """
    _post_tick(agent, elapsed=240.0)
    await _run_one_turn(agent, what="proactive 回合出现")
    assert _llm_saw(llm, _TICK_TEXT) >= 1, "tick 没有被当回合执行（系统事件被饿死）"
    assert await agent.process_message("系统事件之后的用户输入") is True
    await _drain_turns(agent)

    users = _user_texts(agent)
    assert any("系统事件之后的用户输入" in t for t in users), users
    # liveness 判据：tick 回合真的跑过（读真实 LLM 调用记录，不看计数器）
    assert _llm_saw(llm, _TICK_TEXT) >= 1, "proactive 回合被饿死"
    assert agent.status.is_completely_idle, agent.status.current_name()


@pytest.mark.asyncio
async def test_se_t8_stop_clears_deferred_with_attribution(agent: Agent, caplog):
    """SE-T8：停机时推迟队列必须有归因地结束（不静默丢、也不跨 epoch 复活）。"""
    import logging

    _post_task_result(agent, "wf-kb41-t8")
    getattr(agent, "_deferred_task_results", []).append(
        TaskResultData(task_id="wf-kb41-t8-parked", result="r", original_intent="i",
                       dispatched_at="2026-09-20T00:00:00+00:00"))
    with caplog.at_level(logging.INFO):
        await agent.stop()
    assert "deferred_by_shutdown" in caplog.text, (
        "停机清空推迟队列时没有归因日志")
    assert not _deferred(agent)


@pytest.mark.asyncio
async def test_se_t9_interrupt_source_unchanged(agent: Agent, admission_gate: Any, llm):
    """SE-T9：INTERRUPT（用户停止按钮）仍**照常**抢占——KB-41 不改变它的优先级。"""
    from core.schema.event import InterruptData

    first = await _arm_admission_gap(agent, admission_gate, "会被停止按钮取消的输入")
    agent.bus.post(InterruptData(source="websocket"), priority=EventPriority.INTERRUPT)
    await _settle(first)
    admission_gate.open()
    await _drain_turns(agent)

    assert first.cancelled() is True or first.done() is True, (
        "INTERRUPT 应当照常终止窗口内的回合（KB-41 只收窄**非用户来源**的抢占）")
    assert agent.status.is_completely_idle, agent.status.current_name()
