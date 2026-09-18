# 测试文件：core/agent/tests/test_admission_input_delivery.py
"""Batch 12（KB-24 / Accepted Input Delivery Closure）：ACK 之后的输入必须有 owner。

## 缺陷形状（KB-24，canonical scope）

一条用户输入从"已被接受"到"回合真正接管"之间有一段**没有任何 owner** 的窗口：

```text
WS/EventBus 入口
  → Agent.process_message()            ← 这里 bus.post(UserInputData) 并 return True
  → WS 层回 accepted=true（v2，且幂等表已落 ACCEPTED）
  → Agent._handle_user_input()          ← 同步；spawn cognitive task + 登记 _current_task
  → 任务 first-step → CognitiveLoop.process()
  → await self._process_lock            ← 可能挂起（上一回合收口尚未放锁）
  → _prepare_turn():
        _generation_ctx = None          ← admission 尚未提交
        await session_resolver(...)     ← ★ 真挂起点（SessionMemory.load：Mongo/JSONL）
        await registry.get_or_load(...) ← ★ 真挂起点
        self._generation_ctx = ...      ← ★ admission commit point（与 enter_thinking 间无 await）
  → enter_thinking() → _think_loop()
  → _build_turn_prompt(): session.add_message(UserMessage(...))   ← 到这一步输入才进历史
```

窗口内这条输入在**任何地方都没有 owner**：不在 session/archive（append 在 `_think_loop`
之内的 `_build_turn_prompt`）、不在 `_pending_messages`、EventBus 对 USER_INPUT **没有
重投**（`_handle_interrupt` 只搬移不复制），`generation.*` 也一个都没发。

**取消落在窗口内**时的修前行为（本批独立 E2 复现）：整个回合任务被取消、消息连同回合
一起消失——两条消息都 `accepted=True`，最终只有第二条有回应，第一条既没进历史也没有
任何终态；而 v2 幂等表已经落成 ACCEPTED，客户端拿同一 `clientRequestId` 重试只会被
**逐字重放那个 accepted**。触发它不需要任何异常条件——用户在窗口内**再发一条消息**
即可（前端连发两条 = 生产形态）。

## `accepted` 的真实合同（本批 Phase 0 审计结论，见执行记录 §3）

`accepted` = **Transport Accepted**：`process_message` 返回 True 的两种情形（`bus.post`
成功、或有界 `_pending_messages` append 成功）+ 决策幂等（同 `clientRequestId` 重放同一
ACK）。它**不**承诺：消息已持久化、一定会被回复、失败一定会告知。因此本批**不**把
`accepted` 升级成 durable guarantee，也不试图"保证每条 accepted 都得到回复"。

本批要建立的是**更弱但更硬**的那一条：一旦 ACK 了，这条输入就必须始终有一个
lifecycle owner（active turn / pending queue）或一个**显式终态**；取消不得让它在窗口里
无声消失，而 recovery 也不得让它被处理两次。

## 判据（每条用例都断言具体机制，不断言"最终一定有回复"）

- **AD1**：窗口内到达的新输入**不得抢占**尚未接管输入的回合（新输入排队，FIFO 不抢）。
- **AD2**：窗口内被取消的已接受输入必须落**显式终态**（可归因 disposition），
  不得无声消失，也不得被"复活"成稍后的回合。
- **AD3**：已接管输入的回合被取消时，**不得重复处理**同一条输入（原子交接）。
- **AD4**：停止按钮清空 pending 时同样必须可归因（不能把交付失败伪装成"没发生"）。
- **AD5**：窗口边界两侧行为必须不同且与合同一致（接管前排队 vs 接管后打断）。

## 不用 sleep 造 race（§17/§91）

每条用例都用 `asyncio.Event` 屏障把回合**钉死**在指定挂起点（会话加载 / generation 建档）
上，再发下一个动作。`asyncio.wait_for` 只作死锁探测器，不是判据本身。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
import pytest_asyncio

from core.agent.agent import Agent
from core.config import AppConfig
from core.monitoring.event_bus import EventPriority
from core.schema.event import InterruptData

#: 「本测试不碰真实库」的显式声明（见根 conftest 的真实库防线）。
pytestmark = [pytest.mark.service, pytest.mark.usefixtures("fake_storage")]

#: 死锁探测器——只在真的挂死（无界 await）时触发。正常路径实测 < 200ms。
_PROBE_S = 10.0

#: 假 LLM 的固定回复。空响应会触发 nudge 重试 + 兜底文案，徒增噪声。
_REPLY = "好呀，我在听。<mood>平静</mood>"


class _RawClient:
    """`Agent.start()` 的 task-system 步骤会读 `raw_client`（热切换判据）。"""

    def __init__(self, model_id: str) -> None:
        self.model = model_id

    def is_closed(self) -> bool:
        return False

    async def close(self) -> None:
        return None


class _RecordingFakeLLM:
    """确定性 LLM 替身：流式吐固定回复，并记录被调用次数（重复处理判据）。"""

    def __init__(self, response: str = _REPLY) -> None:
        self.response = response
        self.raw_client = _RawClient("fake/test")
        self.stream_calls = 0
        self.seen_prompts: list[str] = []
        #: 可选屏障：`arm_stream_gate()` 后**下一次**流式调用会在此挂起（用于把回合
        #: 钉在"已接管输入、正在产出"的状态上——边界另一侧）。
        self.stream_entered = asyncio.Event()
        self.stream_release = asyncio.Event()
        self._stream_gate_armed = False

    def arm_stream_gate(self) -> None:
        """武装流式屏障（下一次 `stream_think` 进入后阻塞）。"""
        self._stream_gate_armed = True
        self.stream_entered.clear()
        self.stream_release.clear()

    def open_stream_gate(self) -> None:
        self.stream_release.set()

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
        self.stream_calls += 1
        if self._stream_gate_armed:
            self._stream_gate_armed = False
            self.stream_entered.set()
            await self.stream_release.wait()
        self.seen_prompts.append(
            "\n".join(str(m.get("content", "")) for m in messages))
        for ch in self.response:
            yield {"content": ch, "tool_calls": None}


class _Gate:
    """可武装的异步屏障：钉死一个**真实挂起点**，解武装后完全透明。

    每次 `arm()` 只挡下一次调用（`__call__` 语义），`open()` 放行。
    包装的必须是生产代码里**本来就会挂起**的调用——不引入替身时序。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.armed_before = False
        self.hits = 0

    def arm(self) -> None:
        self.armed_before = True
        self.entered.clear()
        self.release.clear()

    def open(self) -> None:
        self.release.set()

    #: 设置后：放行时抛出该异常（用于复现"窗口内 fail-closed"的生产形态）。
    fail_with: BaseException | None = None

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.hits += 1
        if self.armed_before:
            self.armed_before = False
            self.entered.set()
            await self.release.wait()
            if self.fail_with is not None:
                raise self.fail_with
        return await self._inner(*args, **kwargs)


def _install_fake_llm(monkeypatch: pytest.MonkeyPatch) -> _RecordingFakeLLM:
    """把 LLM 工厂指向确定性替身（不连任何 provider），返回该实例供断言。"""
    from Module.llm import LLMFactory

    backend = _RecordingFakeLLM()
    monkeypatch.setattr(
        LLMFactory, "create", staticmethod(lambda model_id, **kw: backend))
    return backend


@pytest_asyncio.fixture
async def llm(monkeypatch: pytest.MonkeyPatch) -> _RecordingFakeLLM:
    return _install_fake_llm(monkeypatch)


@pytest_asyncio.fixture
async def agent(llm: _RecordingFakeLLM):
    """真 Agent + 真 `start()`（含真 `_process_events` 消费者）＋假存储/假 LLM。

    启动后先跑**一条完整的暖场消息**：`Agent.start()` 自己会把默认会话标成已加载、
    并把角色 runtime 塞进缓存，所以"第一个回合会去读库"这个前提在使用时已经不成立。
    暖场让后续每条用例的开局状态（缓存热、无在途回合）显式且确定。
    """
    a = Agent(config=AppConfig())
    await a.start()
    try:
        assert await a.process_message("（暖场）") is True
        await _drain_turns(a)
        assert a.status.is_completely_idle, a.status.current_name()
        assert not a.pending_messages
        yield a
    finally:
        await a.stop()


def _unused() -> None:
    """占位（防止上面的 fixture 被误改成 sync）。"""


async def _settle(task: asyncio.Task | None) -> None:
    """等任务落定（结构化终态信号）。超时 = 死锁探测，不是判据。"""
    if task is None:
        return
    try:
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True),
                               timeout=_PROBE_S)
    except asyncio.TimeoutError:  # pragma: no cover — 只在真回归时触发
        raise AssertionError(f"任务未在 {_PROBE_S}s 内落定（疑似无界 await）") from None


async def _pump_until(predicate: Any, what: str) -> None:
    """让出事件循环直到谓词为真（结构化推进，不用固定 sleep 造 race）。"""
    for _ in range(2000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"等待超时：{what}")


async def _drain_turns(agent: Agent) -> None:
    """等**所有**回合落定（含 pending 出队 spawn 的那些）。

    spawn 到 coroutine 真正开始之间没有任何信号量，所以不能用固定次数的
    `sleep(0)` 等 first-step；这里用真实可观测状态推进：先让出事件循环直到出现
    「在途回合」或「pending 非空」，再等任务落定，直到三者同时为空。
    """
    for _ in range(400):
        await asyncio.sleep(0)          # 先给事件消费者/任务一步推进的机会
        task = agent._cognitive.current_task  # noqa: SLF001 — 只读任务句柄
        if task is not None and not task.done():
            await _settle(task)
            continue
        if agent.pending_messages or agent.bus.pending_user_inputs:
            continue
        return
    raise AssertionError(
        f"回合/pending 未排空：pending={_pending_texts(agent)} "
        f"bus={agent.bus.pending_user_inputs} phase={agent.status.current_name()}")


async def _wait_pending(agent: Agent, count: int) -> None:
    """等到 pending 里恰好有 `count` 条（事件消费者把事件派发给 `_handle_user_input`）。

    `_handle_user_input` 运行在事件消费者任务里，`process_message` 返回时它可能还
    排在总线后面——所以"还没进 pending"≠"丢了"。这里等真实可观测状态，不用固定 sleep。
    """
    for _ in range(2000):
        if len(agent.pending_messages) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(
        f"pending 未在预期时间内达到 {count} 条：{_pending_texts(agent)} "
        f"(bus={agent.bus.pending_user_inputs})")


def _user_texts(agent: Agent) -> list[str]:
    return [str(m.content) for m in agent.session.messages if m.role == "user"]


def _pending_texts(agent: Agent) -> list[str]:
    return [p.text for p in agent.pending_messages]


def _stop(agent: Agent, source: str = "websocket") -> None:
    """走**生产**取消源：WS 停止按钮 / 语音取消所用的 INTERRUPT 事件。"""
    agent.bus.post(InterruptData(source=source), priority=EventPriority.INTERRUPT)


@pytest.fixture
def admission_gate(agent: Agent) -> _Gate:
    """把屏障装在角色 runtime registry 的 `get_or_load` 上——`_prepare_turn` 里
    **真实**的第二个挂起点（`loop.py`：解析会话 → `await registry.get_or_load(...)`）。

    它在**每个**回合都会被调用，且调用时 `_generation_ctx` 仍是 None（admission
    尚未提交）、相位仍是 IDLE（`enter_thinking()` 还没走到）——精确就是 KB-24 窗口。
    用真实挂起点而不是替身时序：这条 await 在生产上要读角色实例/修订的 Mongo 文档
    （缓存未命中时），实测 1ms~数秒。

    只替换 **Agent 实例**上的绑定方法（不动类），因此不会波及其它 registry 使用者。
    """
    registry = agent._runtime_registry  # noqa: SLF001 — 只取实例的绑定方法做包装
    gate = _Gate(registry.get_or_load)
    registry.get_or_load = gate  # type: ignore[method-assign]
    return gate


#: 修复前的代码没有这个属性。用例必须能在修复前的代码上跑到**断言**（红在缺陷上），
#: 而不是当场 AttributeError（红在一个不存在的符号上）——所以缺失时按"尚未接管"处理，
#: 旧语义下这正是"窗口内回合还没 claim 输入"。
_UNKNOWN = object()


def _turn_owns(agent: Agent) -> bool:
    """本回合是否已**接管**它那条输入（`CognitiveLoop.current_turn_owns_input`）。"""
    loop = agent._cognitive  # noqa: SLF001 — 公开属性的宿主
    value = getattr(loop, "current_turn_owns_input", _UNKNOWN)
    return False if value is _UNKNOWN else bool(value)


def _has_ownership_property(agent: Agent) -> bool:
    """当前代码是否实现了「输入已被接管」这一判据（修复后的代码才有）。"""
    return getattr(agent._cognitive, "current_turn_owns_input", _UNKNOWN) is not _UNKNOWN


async def _arm_admission_gap(agent: Agent, admission_gate: _Gate, text: str) -> asyncio.Task:
    """把一条**真实入口**消息钉死在 admission 窗口里，返回它的回合任务。

    返回时的状态（每条用例都以此为前提，断言在调用方）：
    - `process_message` 已返回 True ⇒ 等于 WS 层已回 `accepted=true`；
    - 回合任务已 first-step 且停在 `CharacterRuntimeRegistry.get_or_load` 上；
    - `_generation_ctx is None` ⇒ **尚未接管**这条输入（admission 未提交）；
    - 相位仍是 IDLE（`enter_thinking()` 还没走到）⇒ 下一条消息会走 fast path。
    """
    admission_gate.arm()
    assert await agent.process_message(text) is True, "用例前提：消息必须已 ACK accepted"
    await asyncio.wait_for(admission_gate.entered.wait(), timeout=_PROBE_S)
    task = agent._cognitive.current_task  # noqa: SLF001 — 只读任务句柄
    assert task is not None and not task.done(), "用例前提：回合任务必须在途"
    assert _turn_owns(agent) is False, (
        "用例前提：窗口内回合**尚未**接管输入（否则测的不是 admission 窗口）")
    assert agent.status.is_completely_idle, "用例前提：窗口内相位仍是 IDLE"
    if not _has_ownership_property(agent):
        # 修复前的代码**没有**"输入已被接管"这一判据（`_handle_user_input` 只有
        # pre-start 集合判定），因此"已 first-step 但停在 prepare"的回合会被判成
        # "真正在跑"。窗口前提在这种代码上无法用一种判据表达——但这正是被测缺陷本身，
        # 所以此处**不 skip**：下面每条用例的断言（排队 / 显式终态）在修复前必然失败。
        pass
    return task
    assert agent.status.is_completely_idle, "用例前提：窗口内相位仍是 IDLE"
    return task


@pytest.mark.asyncio
async def test_admission_gap_incoming_input_queues_and_nothing_is_lost(
        agent: Agent, admission_gate: _Gate, llm: _RecordingFakeLLM):
    """AD1 / AD5（PRE_FIX_RED）：窗口内到达的新输入不得抢占，两条都必须被处理。

    生产交错（全部真实入口，无替身时序）：用户在第一条消息的会话加载窗口内**再发一条**
    （前端连发两条 / 快速双击发送）。

    修前：`_handle_user_input` 的 pre-start 判据只覆盖"任务还没 first-step"，
    已 first-step 但停在 `_prepare_turn` 的回合被判为"真正在跑" ⇒ `cancel_current()`
    把第一条连同它的输入一起取消。两条消息都 `accepted=True`，最终只有第二条有回应。
    修后：新输入排队（FIFO 不抢），第一条正常完成，第二条随后被处理。
    """
    first = await _arm_admission_gap(agent, admission_gate, "第一条")

    # ── 窗口内第二条真实入口消息 ──
    assert await agent.process_message("第二条") is True
    await _wait_pending(agent, 1)
    # 排队而不是抢占：第一条的回合不得被取消。
    assert first.cancelled() is False, (
        "窗口内的新输入抢占了尚未接管输入的回合（KB-24：第一条已 ACK 的输入会随之消失）")
    assert first.done() is False, "窗口内回合不得提前结束"
    assert _pending_texts(agent) == ["第二条"], (
        f"新输入没有被排队：pending={_pending_texts(agent)}")

    # ── 放行并让两条都跑完 ──
    admission_gate.open()
    await _settle(first)
    await _drain_turns(agent)

    users = _user_texts(agent)
    assert any("第一条" in t for t in users), (
        f"第一条已 ACK 的消息从未进入会话（静默消失）：messages={users}")
    assert any("第二条" in t for t in users), (
        f"第二条已 ACK 的消息从未进入会话：messages={users}")
    assert not agent.pending_messages, f"pending 未排空：{_pending_texts(agent)}"
    assert agent.status.is_completely_idle, agent.status.current_name()
    assert agent._cognitive.process_lock.locked() is False, (  # noqa: SLF001
        "回合结束后 process lock 不得仍被持有")


@pytest.mark.asyncio
async def test_admission_gap_preserves_fifo_order(
        agent: Agent, admission_gate: _Gate, llm: _RecordingFakeLLM):
    """AD1 / AI5：M1 在窗口内被"排队保护"后，处理顺序仍是 M1 → M2。

    修前真实发生的正是**反序**：M2 抢占、先被处理，M1 连同它的输入一起消失。
    这条用例钉住"没有抢占、原序推进"这一最小合同。
    """
    first = await _arm_admission_gap(agent, admission_gate, "M1")
    assert await agent.process_message("M2") is True
    await _wait_pending(agent, 1)
    assert _pending_texts(agent) == ["M2"], _pending_texts(agent)

    admission_gate.open()
    await _settle(first)
    await _drain_turns(agent)

    users = _user_texts(agent)
    # 顺序断言必须按**实际处理顺序**取，不能按期望顺序去匹配（那样 ['M2','M1'] 也会通过）
    ordered = [t for u in users for t in ("M1", "M2") if t in u]
    assert ordered == ["M1", "M2"], (
        f"处理顺序被破坏（应为 M1→M2，实为 {ordered}）：messages={users}")


@pytest.mark.asyncio
async def test_third_message_arriving_while_second_is_still_on_the_bus(
        agent: Agent, admission_gate: _Gate, llm: _RecordingFakeLLM):
    """FALSIFICATION_GUARD（Batch 12 新发现，非本批修法引入）：连发三条时的既有乱序。

    这条**不**断言"顺序正确"——它钉住并公开一个本批审计发现的**既有**缺陷：
    当上一条消息还排在 **EventBus 队列**里（事件消费者尚未把它交给
    `_handle_user_input`）时，`process_message` 因 `bus.pending_user_inputs > 0`
    走队列分支、把**新**消息直接 append 到 `_pending_messages`；随后消费者才把
    **旧**消息经 `_handle_user_input` 也 append 进去 ⇒ 两条在 pending 里**反序**
    （实测 `['M3','M2']`）。

    根因与 KB-24 同族（"事件在总线里排队时没有 pending 归属"）但**不同根**：
    KB-24 是"已接受输入无 owner"，这里两条都**有** owner（pending），只是**次序**错了；
    且它在修法前后完全一致（不是本批引入）。按 §1/§102 只登记（→ 回归守卫 + 新登记），
    不在本批修（修它会动 `process_message` 的入队判据与 pending 语义，属 Batch 12 明确
    排除的范围）。
    """
    first = await _arm_admission_gap(agent, admission_gate, "M1")
    assert await agent.process_message("M2") is True
    assert await agent.process_message("M3") is True
    await _wait_pending(agent, 2)

    # 现状（缺陷形状）：pending 里 M3 在 M2 之前 —— 既有行为，本批不修。
    assert _pending_texts(agent) == ["M3", "M2"], (
        "既有乱序行为发生变化：若这里红了，说明有人修好了它——"
        f"请把本用例改成断言正确顺序并更新 todo。pending={_pending_texts(agent)}")

    admission_gate.open()
    await _settle(first)
    await _drain_turns(agent)

    # 不论 pending 里的次序如何，三条都不得丢失（AI1：每条都有 owner）。
    users = _user_texts(agent)
    for text in ("M1", "M2", "M3"):
        assert any(text in u for u in users), (
            f"{text} 丢失（已 ACK 的输入无 owner）：messages={users}")


@pytest.mark.asyncio
async def test_admission_gap_stop_button_cancels_with_explicit_disposition(
        agent: Agent, admission_gate: _Gate, caplog: pytest.LogCaptureFixture):
    """AD2（PRE_FIX_RED）：停止按钮落在窗口内 ⇒ 显式终态，而不是无声消失。

    产品语义（既有合同，本批**不**改）：停止按钮 = 取消当前工作、**放弃**尚未开始的
    输入。所以这里**不**要求 requeue/复活——要求的只是这条已 ACK 的输入必须落一个
    可归因的终态（`cancelled_pre_ownership` + 来源 + message/session 身份）。

    修前：只有一句 "准备阶段被取消（THINKING 入口之前）— 无状态需要收口"，
    既没有"这条已 ACK 的输入被放弃了"的语义，也没有任何身份信息——与"什么都没发生"
    在观测面上无法区分。
    """
    first = await _arm_admission_gap(agent, admission_gate, "会被停止按钮取消的消息")

    with caplog.at_level(logging.INFO, logger="core.cognitive.loop"):
        _stop(agent, source="websocket")
        await _settle(first)

    admission_gate.open()
    await asyncio.sleep(0)
    await _settle(first)

    disposition = [r.getMessage() for r in caplog.records
                   if "cancelled_pre_ownership" in r.getMessage()]
    assert disposition, (
        "窗口内取消没有落显式终态（disposition 缺失）——已 ACK 的输入是静默消失的。"
        f"logs={[r.getMessage() for r in caplog.records]}")
    text = disposition[0]
    assert "websocket" in text, f"取消来源没有被归因：{text}"

    # 终态语义：不复活——这条输入不得在本回合之后被重新处理。
    assert _pending_texts(agent) == [], (
        f"被显式取消的输入被错误地重新排队（错误的 requeue）：{_pending_texts(agent)}")
    await _drain_turns(agent)
    assert not any("会被停止按钮取消的消息" in t for t in _user_texts(agent)), (
        "被停止按钮取消的输入在之后被复活处理了")
    assert agent.status.is_completely_idle, agent.status.current_name()


@pytest.mark.asyncio
async def test_stop_button_clearing_pending_is_attributable(
        agent: Agent, admission_gate: _Gate, caplog: pytest.LogCaptureFixture):
    """AD4：停止按钮清空 pending 必须可归因（既有语义保留，只是不再静默）。

    `_handle_interrupt` 丢弃排队输入是**有意的产品合同**（用户已取消这条输入），
    但"丢弃"与"交付失败"在观测面上此前完全一样：零日志、零计数。
    """
    first = await _arm_admission_gap(agent, admission_gate, "在途")
    assert await agent.process_message("排队的") is True
    await _wait_pending(agent, 1)
    assert _pending_texts(agent) == ["排队的"]

    with caplog.at_level(logging.INFO, logger="core.agent.agent"):
        _stop(agent, source="websocket")
        await _settle(first)

    admission_gate.open()
    await asyncio.sleep(0)
    await _settle(first)

    dropped = [r.getMessage() for r in caplog.records
               if "cancelled_by_interrupt" in r.getMessage()]
    assert dropped, (
        "停止按钮清空排队输入时没有任何可归因记录（静默丢弃）"
        f"logs={[r.getMessage() for r in caplog.records]}")
    assert _pending_texts(agent) == [], _pending_texts(agent)


#: 窗口内本回合**从未产出过**的用户可见帧——收口可以安全地补终态（见下面的用例）。
_NO_FRAME_YET = "没有帧"


@pytest.mark.asyncio
async def test_cancel_after_commit_but_before_history_gets_terminal_disposition(
        agent: Agent, llm: _RecordingFakeLLM, caplog: pytest.LogCaptureFixture):
    """AD2 第二形态 / PRE_FIX_RED：commit point 之后、**输入进历史之前**被取消。

    这是 §9.2 复现的那条窗口：屏障落在 `generation_registry.register`（Mongo upsert），
    此时本回合已越过 admission commit point（`current_turn_owns_input is True`），
    收口走 canonical 中断路径、**相位安全**（Batch 10 的成果），但输入**仍然**没有进
    session/archive（那条 append 在 `_build_turn_prompt`，即 `_think_loop` 之内）——
    所以用户拿到的是一个"已 accepted 且没有任何终态"的输入。

    判据：本回合**确实已接管输入**（不是 AD1 那个窗口），且取消后必须落可归因终态
    （`cancelled_pre_ownership`）。修前该路径只有一句"准备阶段被取消（THINKING 入口
    之后）"，没有任何关于这条输入被放弃的语义。
    """
    from core.agent.tests.test_thinking_cancel_recovery import _BlockingUpsert

    blocker = _BlockingUpsert(agent._generation_repo)  # noqa: SLF001 — 只替存储依赖
    blocker.arm()
    agent._generation_repo.upsert = blocker  # type: ignore[method-assign]  # noqa: SLF001

    assert await agent.process_message("commit 之后被取消") is True
    await asyncio.wait_for(blocker.started.wait(), timeout=_PROBE_S)
    turn = agent._cognitive.current_task  # noqa: SLF001
    assert _turn_owns(agent) is True, (
        "用例前提：本回合必须已越过 admission commit point（否则测的是 AD1 那个窗口）")
    assert not any("commit 之后被取消" in t for t in _user_texts(agent)), (
        "用例前提：此刻输入**尚未**进 session（否则已不是窗口）")

    with caplog.at_level(logging.INFO, logger="core.cognitive.loop"):
        _stop(agent, source="websocket")
        blocker.open()          # 放行 register，让取消落在收口链上
        await _settle(turn)

    dispositions = [r.getMessage() for r in caplog.records
                    if "cancelled_pre_ownership" in r.getMessage()]
    assert dispositions, (
        "commit point 之后、进历史之前被取消，没有落任何关于这条输入的终态——"
        f"它在观测面上与'从未被处理'无法区分。logs="
        f"{[r.getMessage() for r in caplog.records]}")
    assert "websocket" in dispositions[0], dispositions[0]

    await _drain_turns(agent)
    assert agent.status.is_completely_idle, agent.status.current_name()
    assert not agent.pending_messages, _pending_texts(agent)


@pytest.mark.asyncio
async def test_cancel_after_history_append_does_not_duplicate_processing(
        agent: Agent, llm: _RecordingFakeLLM):
    """AD3 / AI4：输入**已经进入历史**之后取消，不得被重复处理，也不得被 requeue。

    边界另一侧（[11] 之后）：输入已 `session.add_message`，owner 与终态由 Batch 10/11
    的取消安全收口负责——本批的职责只是**不插手**（不 requeue、不重新投递）。
    判据按 message identity：该输入在 session 里**恰一条**、LLM 恰好被喂**一次**。
    """
    from core.agent.tests.test_thinking_cancel_recovery import _BlockingUpsert

    baseline = llm.stream_calls
    blocker = _BlockingUpsert(agent._generation_repo)  # noqa: SLF001 — 只替存储依赖
    blocker.arm()
    agent._generation_repo.upsert = blocker  # type: ignore[method-assign]  # noqa: SLF001

    # 让本回合走完 `_prepare_turn`（越过 admission 窗口与 register），再在**流式产出**
    # 期间取消——此时输入早已写进 session。
    gate = _Gate(agent._runtime_registry.get_or_load)  # noqa: SLF001 — 真实挂起点
    blocker.open()                      # 不阻塞 register（本用例不需要）
    agent._runtime_registry.get_or_load = gate  # type: ignore[method-assign]  # noqa: SLF001

    llm.arm_stream_gate()
    assert await agent.process_message("只该被处理一次") is True
    await asyncio.wait_for(llm.stream_entered.wait(), timeout=_PROBE_S)
    turn = agent._cognitive.current_task  # noqa: SLF001
    assert _turn_owns(agent) is True, "用例前提：本回合必须已接管输入"
    assert any("只该被处理一次" in t for t in _user_texts(agent)), (
        "用例前提：输入此刻必须已进 session（边界另一侧）")

    _stop(agent, source="websocket")
    llm.open_stream_gate()              # 放行流，让取消落在收口链上
    await _settle(turn)
    await _drain_turns(agent)

    users = [t for t in _user_texts(agent) if "只该被处理一次" in t]
    # 恰一次：`<= 1` 是假绿——"一次都没处理"同样满足它。这里必须双向钉住。
    assert len(users) == 1, (
        f"该输入在会话里出现 {len(users)} 次（应为恰 1 次；0 = 被丢弃，>1 = 重复处理）")
    assert not agent.pending_messages, (
        f"已接管的输入被错误 requeue：{_pending_texts(agent)}")
    assert llm.stream_calls - baseline == 1, (
        f"同一条输入被送进 LLM {llm.stream_calls - baseline} 次（应为恰 1 次）")


@pytest.mark.asyncio
async def test_normal_fast_path_unchanged(agent: Agent, llm: _RecordingFakeLLM):
    """ALREADY_GREEN_GUARD：无取消时正常快速路径不变。

    本批的修法只增加一条"未接管 ⇒ 排队"的判据，不得给正常路径加一次调度跳变：
    单条消息仍然直发 EventBus、立即被处理、进会话、拿到回复，pending 保持为空。
    """
    baseline = llm.stream_calls
    assert await agent.process_message("单条消息") is True
    # `process_message` 在 v1 路径上没有 await ⇒ 返回时消费者还没跑，"pending 为空"
    # 在那一点上恒真、没有判别力。真正能区分 fast path 与排队分支的是**它去了总线**：
    assert agent.bus.pending_user_inputs == 1, (
        "正常快速路径应当直发 EventBus（而不是进 pending）")
    assert _pending_texts(agent) == [], "正常快速路径不得经过 pending"
    await _drain_turns(agent)

    assert any("单条消息" in t for t in _user_texts(agent)), _user_texts(agent)
    assert llm.stream_calls - baseline == 1, (
        f"LLM 调用次数异常：{llm.stream_calls - baseline}（暖场基线 {baseline}）")
    assert agent.status.is_completely_idle, agent.status.current_name()


@pytest.mark.asyncio
async def test_ownership_judgement_is_never_stale_for_a_new_turn(
        agent: Agent, llm: _RecordingFakeLLM):
    """AD3 补强（Challenger C）：**判据不得陈旧**——新回合一 first-step 就不再冒领上一回合的 ctx。

    这是 Challenger 反证（`test_production_lock_holder_stale_ctx_loses_message`）的
    回归守卫：修前 `_generation_ctx` 只在 `_prepare_turn` 里清空，而 `await
    self._process_lock` 是本回合的**第一个挂起点**——停在锁上的新回合会沿用**上一个**
    回合的 `_generation_ctx` ⇒ `current_turn_owns_input` 误报 True ⇒ 第三条消息抢占
    一个什么都没接管的回合 ⇒ 已 ACK 的输入静默丢失，且取消落在取锁的 `__aenter__`
    （在取消边界之外）⇒ **连 disposition 都没有**。

    这条把那一整类形状钉死成一个**结构性**判据（不依赖"把回合精确停在锁上"这种
    需要测试持有锁的编排）：新回合的 coroutine **一旦真的跑起来**，判据就必须先变成
    False（它的 `_prepare_turn` 还没开始），直到它自己 commit 为止。
    """
    llm.arm_stream_gate()
    assert await agent.process_message("前一回合") is True
    await asyncio.wait_for(llm.stream_entered.wait(), timeout=_PROBE_S)
    prev_ctx = agent._cognitive._generation_ctx  # noqa: SLF001 — 诊断用
    assert prev_ctx is not None, "前提：前一回合已越过 commit point（ctx 非 None）"

    # 让收口停在**公开订阅点**上：FSM 已回 IDLE，但锁还没释放（KB-28 的窗口）。
    release_cleanup = asyncio.Event()
    cleanup_entered = asyncio.Event()

    async def _parking_subscriber(name: str) -> None:
        if name == "idle" and not cleanup_entered.is_set():
            cleanup_entered.set()
            await release_cleanup.wait()

    agent.status.on_change(_parking_subscriber)

    llm.open_stream_gate()
    assert await agent.process_message("新回合") is True
    await asyncio.wait_for(cleanup_entered.wait(), timeout=_PROBE_S)

    # 新回合只要**真的开始执行**，判据就必须先不再冒领上一回合的 ctx：
    # `_generation_ctx` 一旦不再是 `prev_ctx`，要么是 None（已清空、未接管 ⇒ 判据必须是
    # False），要么是新回合自己的 ctx（已接管 ⇒ True）。**只有前者是陈旧冒领地**——
    # 修前这里会一直是 `prev_ctx`（非 None）⇒ 判据 True ⇒ 抢占。
    saw_cleared = False
    for _ in range(2000):
        ctx = agent._cognitive._generation_ctx  # noqa: SLF001 — 诊断用
        if ctx is None:
            saw_cleared = True
            assert _turn_owns(agent) is False, (
                "判据陈旧：新回合的 ctx 已清空、尚未接管输入，却被报成'已接管'"
                "（Challenger C 的复现路径：第三条消息会抢占这个回合）")
            break
        if ctx is not prev_ctx:
            saw_cleared = True        # 新回合已 commit 自己的 ctx ⇒ 判据 True 是正确的
            assert _turn_owns(agent) is True
            break
        await asyncio.sleep(0)

    assert saw_cleared, (
        "判据一直是上一回合的 ctx 且新回合从未开始执行——若这持续存在，"
        "说明 `process()` 的取锁前清空没有生效（本批 Challenger C 的修复点）")

    release_cleanup.set()
    await _drain_turns(agent)

    users = _user_texts(agent)
    assert any("新回合" in t for t in users), (
        f"新回合的输入丢失：messages={users}")
    assert agent.status.is_completely_idle, agent.status.current_name()


@pytest.mark.asyncio
async def test_turn_ending_without_state_change_still_drains_pending(
        agent: Agent, admission_gate: _Gate, llm: _RecordingFakeLLM):
    """AD1 补强（Challenger A，**PRE_FIX_RED**）：回合在**从未离开 IDLE** 的情况下结束。

    被本批守卫保护而排队的消息，靠 `STATE_CHANGE → IDLE` 出队。但窗口内的取消/异常
    恰恰发生在相位**仍是 IDLE** 时：`handle_interrupt` 因"已 IDLE"而 no-op，
    于是没有 STATE_CHANGE ⇒ 已 ACK 的排队消息会一直躺在 `_pending_messages` 里，
    而 Agent 对外显示完全空闲（实测：`pending=['M2'] fsm=idle current_task=None bus=0`）。

    生产形态：会话/角色存储在窗口内 fail-closed（P0-3 `StorageUnavailableError`）。
    """
    from core.storage.base import StorageUnavailableError

    admission_gate.arm()
    assert await agent.process_message("M1") is True
    await asyncio.wait_for(admission_gate.entered.wait(), timeout=_PROBE_S)
    turn_a = agent._cognitive.current_task  # noqa: SLF001
    assert turn_a is not None and not turn_a.done()

    # 窗口内第二条 → 被守卫排队（这正是修法的新行为）
    assert await agent.process_message("M2") is True
    await _wait_pending(agent, 1)
    assert _pending_texts(agent) == ["M2"], _pending_texts(agent)

    # 让 M1 的回合在窗口内以**异常**结束（fail-closed：权威存储不可用）
    admission_gate.fail_with = StorageUnavailableError("测试：权威存储不可用")
    admission_gate.open()
    await _settle(turn_a)

    assert agent.status.is_completely_idle, (
        "前提：窗口内失败后相位应仍是 IDLE（因此不会有 STATE_CHANGE→IDLE）")
    # 关键判据：排队的已 ACK 消息必须重新获得处理权，而不是躺在队列里等"下一次"
    await _drain_turns(agent)
    assert not agent.pending_messages, (
        f"回合结束后排队消息无人出队（Agent 显示空闲却永不处理）：{_pending_texts(agent)}")
    assert any("M2" in t for t in _user_texts(agent)), (
        f"M2 从未被处理：messages={_user_texts(agent)}")


@pytest.mark.asyncio
async def test_new_input_does_not_preempt_a_turn_that_owns_its_input(
        agent: Agent, llm: _RecordingFakeLLM):
    """AD6 / PRODUCT_CONTRACT_GUARD：**已接管输入**的回合不被新消息抢占（既有语义）。

    这条钉住边界另一侧的真实合同（Challenger E 的 M2 变异体就是冲着它来的：

    > 只有当回合**尚未接管输入**时才排队；一旦接管了，新输入走既有的
    > `process_message` 忙时排队分支（`is_accepting_input` 为假 ⇒ 直接进
    > `_pending_messages`），**不是**抢占。

    即"用户连发两条"的语义是**排队**（第二条在第一条说完之后处理），不是"打断"。
    真正的打断走 INTERRUPT 事件（停止按钮 / VAD / 语音取消）。
    若有人把修法写成"一律抢占"（或反过来"一律排队导致第一条被丢弃"），这条会红。
    """
    llm.arm_stream_gate()
    assert await agent.process_message("先说的这句") is True
    await asyncio.wait_for(llm.stream_entered.wait(), timeout=_PROBE_S)
    turn_a = agent._cognitive.current_task  # noqa: SLF001
    assert _turn_owns(agent) is True, "用例前提：回合 A 必须已接管输入"
    assert agent.status.is_accepting_input is False, (
        "用例前提：回合 A 正在产出，相位应已离开 IDLE")

    # 第二条：应进 pending（忙时排队），且不得取消正在产出的回合 A。
    assert await agent.process_message("第二条排队") is True
    await _wait_pending(agent, 1)
    assert _pending_texts(agent) == ["第二条排队"], _pending_texts(agent)
    assert turn_a.cancelling() == 0 and not turn_a.done(), (
        "已接管输入的回合被新消息抢占（既有中断语义被改成了抢占）")

    llm.open_stream_gate()
    await _drain_turns(agent)

    users = _user_texts(agent)
    ordered = [t for u in users for t in ("先说的这句", "第二条排队") if t in u]
    assert ordered == ["先说的这句", "第二条排队"], (
        f"顺序被破坏（应为先说的这句→第二条排队，实为 {ordered}）：messages={users}")
    assert not agent.pending_messages, _pending_texts(agent)
    assert agent.status.is_completely_idle, agent.status.current_name()


@pytest.mark.asyncio
async def test_busy_agent_still_queues_after_fix(
        agent: Agent, admission_gate: _Gate, llm: _RecordingFakeLLM):
    """ALREADY_GREEN_GUARD：Agent 真忙（已接管且在跑）时仍走既有 pending 路径。

    边界另一侧的排队分支（`process_message` 的 `is_accepting_input` 判定）保持原样：
    相位忙 ⇒ 直接进 pending ⇒ IDLE 时经既有 drain 机制处理。本批不重写 pending。
    """
    from core.agent.tests.test_thinking_cancel_recovery import _BlockingUpsert

    blocker = _BlockingUpsert(agent._generation_repo)  # noqa: SLF001
    blocker.arm()
    agent._generation_repo.upsert = blocker  # type: ignore[method-assign]  # noqa: SLF001

    assert await agent.process_message("在途") is True
    await asyncio.wait_for(blocker.started.wait(), timeout=_PROBE_S)
    assert _turn_owns(agent) is True

    assert await agent.process_message("忙时排队的") is True
    await _wait_pending(agent, 1)
    assert _pending_texts(agent) == ["忙时排队的"], _pending_texts(agent)

    blocker.open()
    await _drain_turns(agent)
    assert any("忙时排队的" in t for t in _user_texts(agent)), _user_texts(agent)
    assert agent.status.is_completely_idle, agent.status.current_name()
