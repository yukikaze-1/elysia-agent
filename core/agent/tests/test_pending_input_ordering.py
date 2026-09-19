# 测试文件：core/agent/tests/test_pending_input_ordering.py
"""Batch 13（**KB-38** / Pending Input Ordering Correctness）。

## 缺陷形状

同一个 Agent 上，「已被 accepted 的输入」在一段时间里可能同时存在于**两个**没有共同顺序的
容器里：

```text
process_message(M2) → bus.post(UserInputData(M2))     ← 快速路径：M2 的 owner 是 EventBus
process_message(M3) → _pending_messages.append(M3)    ← 判据：bus.pending_user_inputs > 0
   ⋮
事件消费者派发 M2 → _handle_user_input(M2) → append(M2)  ← 到这里 M2 才进 pending
```

于是 pending 里出现 `["M3", "M2"]`——**两个都有 owner，只是次序错**。出队是 `pop(0)`
（FIFO），所以最终**处理顺序**是 `M1 → M3 → M2`，与 admission 顺序相反（Batch 13 独立
E2 复现：admission `[M1,M2,M3]` / claim `[M1,M3,M2]` / session `[M1,M3,M2]`）。

根因不是 EventBus：总线对同优先级事件按 `seq` 严格 FIFO。根因是**两个 append 权威**
（`process_message` 在 admission 时刻 append，`_handle_user_input` 在派发时刻 append）
按**写入时刻**而不是**admission 时刻**排序。

第二条通道（PO3）：`ws_handler.py:733`（STT 转写）/ `ws_v2.py:787`（voice.commit）/
`voice_sessions.py:702`（REALTIME）**直接 `bus.post(UserInputData)`**、不经过
`process_message`，因此既不过忙判定也不看 pending ⇒ 它会在 `_handle_user_input` 的
fall-through 分支里抢占当前回合并**越过**更早排队的输入。

## 合同（Phase 0 结论，见执行记录 §3）

FIFO 是**既有**产品承诺，只是此前只写在 `_pending_messages` 的 docstring 里、没有跨过
bus→pending 的交接：`process_message`（"Agent 忙碌时消息入队（FIFO），IDLE 时出队处理"）、
`pending_messages`（"FIFO 队列中暂存的消息"）、`_dispatch_pending_message`（"队首……
（FIFO 出队）"）、AD3（"新输入排队（FIFO 不抢）"）＋ 前端按发送顺序渲染、无 priority/
reorder 指示。本批不新增合同，只把既有 FIFO 承诺**贯通到整条 handoff 链**。

- **ordering domain**：单个 Agent 实例的 accepted 输入流（一个 FSM / 一个
  `_pending_messages` / 一个 `_cognitive.current_task` / 一把 `_process_lock` ⇒ 回合
  全 Agent 串行，与 session 无关）。
- **admission order**：`process_message` 提交准入决策的那一刻（`reserve_work` 之后、
  分支判定之前，中间**无 await**）——`request_id` 是该时刻的单调见证。
- **processing order（canonical）**：active-turn ownership commit
  （`_generation_ctx` 建立，即 `CognitiveLoop.current_turn_owns_input` 变真），与
  Batch 12 的 admission commit / Batch 10 的 ownership 判据同一点。

## 判据与层次（§67：至少记录三层身份）

每条用例都同时记录并断言：admission IDs / pending IDs / claim IDs / session 顺序 /
LLM 实际回合顺序。顺序断言一律 `actual == expected`（不用 set 相等），另配
`set(actual) == set(expected)`（无遗漏）与 `len(actual) == len(set(actual))`（无重复）。

## 不用 sleep 造 race

全部用 `asyncio.Event` 屏障把回合钉在**生产代码本来就会挂起**的调用上（角色 runtime
`get_or_load` / LLM 流式入口），`asyncio.wait_for` 只作死锁探测器。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import pytest_asyncio

from core.agent.agent import Agent
from core.agent.tests.test_admission_input_delivery import (
    _arm_admission_gap,
    _drain_turns,
    _Gate,
    _install_fake_llm,
    _pending_texts,
    _RecordingFakeLLM,
    _settle,
    _user_texts,
    _wait_pending,
)
from core.config import AppConfig
from core.monitoring.event_bus import EventPriority
from core.schema.event import InterruptData, UserInputData

pytestmark = [pytest.mark.service, pytest.mark.usefixtures("fake_storage")]

#: 死锁探测器——只在真的挂死（无界 await）时触发。
_PROBE_S = 10.0

#: burst 身份 token（§6：用稳定身份比较，不用重复文本）。
_TOKENS = ("M1", "M2", "M3", "M4", "M5", "V1", "V3", "T1", "T2")


# ---------------------------------------------------------------- harness


@pytest_asyncio.fixture
async def llm(monkeypatch: pytest.MonkeyPatch) -> _RecordingFakeLLM:
    """确定性 LLM 替身（不连任何 provider）。"""
    return _install_fake_llm(monkeypatch)


@pytest_asyncio.fixture
async def agent(llm: _RecordingFakeLLM):
    """真 Agent + 真 `start()`（含真 `_process_events` 消费者）＋假存储/假 LLM。

    与 Batch 12 的 fixture 同构：先跑一条**完整暖场消息**，让开局状态（缓存热、
    无在途回合、pending 空）显式且确定。
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


@pytest.fixture
def admission_gate(agent: Agent) -> _Gate:
    """把屏障装在角色 runtime registry 的 `get_or_load` 上（`_prepare_turn` 里真实的
    第二个挂起点）：调用时 `_generation_ctx` 仍是 None、相位仍是 IDLE ⇒ 精确落在
    "已 first-step、尚未接管输入、仍 accepting"的窗口里。"""
    registry = agent._runtime_registry  # noqa: SLF001 — 只取实例的绑定方法做包装
    gate = _Gate(registry.get_or_load)
    registry.get_or_load = gate  # type: ignore[method-assign]
    return gate


class _TurnRecorder:
    """轻量 recorder（§50）：spy **既有**方法，记录 admission/pending/claim/processing。

    不新增 production API、不直接改 `_pending_messages`。
    """

    def __init__(self) -> None:
        #: 本用例发出的输入（admission 顺序 = 调用顺序）。
        self.admitted: list[str] = []
        #: active-turn ownership commit 顺序（`_generation_ctx` 建立）。
        self.claims: list[str] = []
        #: 同上，但记的是身份（`_generation_ctx.user_message_id`）——第二层证据。
        self.claim_ids: list[str] = []
        #: 同上，但记的是**本条自己的 admission 见证**（`_generation_ctx.request_id`）。
        #: 有了它，"处理顺序 == admission 顺序"可以在**任何交错**下现算期望值，
        #: 而不是靠测试假定的调用顺序（并发投递时尤其重要）。
        self.claim_rids: list[str] = []
        #: LLM 实际被喂的**当前**用户输入顺序（端到端第三层证据）。
        self.llm_turns: list[str] = []

    @staticmethod
    def _burst(seq: list[str]) -> list[str]:
        return [t for t in seq if t in _TOKENS]


def _install_recorder(agent: Agent, llm: _RecordingFakeLLM) -> _TurnRecorder:
    """spy `CognitiveLoop._prepare_turn`（claim）与假 LLM 的流式入口（processing）。"""
    loop = agent._cognitive  # noqa: SLF001 — 只取实例绑定方法做包装（测试惯例）
    rec = _TurnRecorder()
    orig_prepare = loop._prepare_turn
    orig_stream = llm.stream_think

    async def spy_prepare(user_message: str, *args: Any, **kwargs: Any) -> Any:
        setup = await orig_prepare(user_message, *args, **kwargs)
        ctx = loop._generation_ctx
        if ctx is not None:
            # admission commit point：本回合已接管这条输入
            rec.claims.append(user_message)
            rec.claim_ids.append(str(ctx.user_message_id or "-"))
            rec.claim_rids.append(str(ctx.request_id or ""))
        return setup

    async def spy_stream(messages: list[dict], **kwargs: Any) -> Any:
        # `messages` 的最后一条就是**本回合**的用户输入（system → 历史 → 当前）
        current = str((messages[-1] or {}).get("content", "")).strip() if messages else ""
        if current in _TOKENS:
            rec.llm_turns.append(current)
        async for chunk in orig_stream(messages, **kwargs):
            yield chunk

    loop._prepare_turn = spy_prepare  # type: ignore[method-assign]
    llm.stream_think = spy_stream  # type: ignore[method-assign]
    return rec


def _session_order(agent: Agent) -> list[str]:
    """session 历史里的 user 消息顺序（第四层证据；不作为唯一判据）。"""
    return [t for u in _user_texts(agent) for t in _TOKENS if t in u]


def _assert_full_order(rec: _TurnRecorder, agent: Agent) -> None:
    """§68/§69/§70：顺序相等 + 无遗漏 + 无重复（跨三层证据）。"""
    expected = rec.admitted
    claims = rec._burst(rec.claims)
    assert claims == expected, (
        f"处理顺序被破坏（应 {expected}，实为 {claims}）"
        f"；pending={_pending_texts(agent)} session={_session_order(agent)}")
    assert set(claims) == set(expected), (
        f"有 accepted 输入**从未**获得 active-turn ownership：{set(expected) - set(claims)}")
    assert len(claims) == len(set(claims)), f"同一条输入被处理两次：{claims}"
    assert len(rec.claim_ids) == len(rec.admitted), (
        f"claim 身份数不匹配：{rec.claim_ids}")
    session = _session_order(agent)
    assert session == expected, f"session 顺序被破坏（应 {expected}，实为 {session}）"
    if rec.llm_turns:  # LLM 替身被 spy 到才断言（它是最外层的端到端证据）
        assert rec.llm_turns == expected, (
            f"LLM 实际回合顺序被破坏（应 {expected}，实为 {rec.llm_turns}）")
    # 最强形式：每条输入**自己的** admission 见证（`request_id`）必须单调递增。
    # 它不依赖测试假定的调用顺序，因此对并发投递（两个入口同时进 `process_message`）
    # 同样成立——这正是 PO1 在"到达顺序未定义"时的可判定形式。
    rids = [int(r) for r in rec.claim_rids if str(r).isdigit()]
    if rids and len(rids) == len(rec.claim_rids):
        assert rids == sorted(rids), (
            "处理顺序与各条自己的 admission 顺序不一致："
            f"{list(zip(rec._burst(rec.claims), rec.claim_rids))}")
        assert len(set(rids)) == len(rids), f"同一条输入被处理两次（相同 rid）：{rids}"


async def _wait_settled(agent: Agent, task: asyncio.Task | None = None) -> None:
    """等**全部**回合落定（含 pending 出队 spawn 的那些）。

    比 `_drain_turns` 更严的两点，都是本批真实踩到的：
    ① 收口链里有一段 **50ms UI 可观测延迟**（`FSMBackedPhaseController.handle_interrupt`
       的 `asyncio.sleep(0.05)`），纯 `sleep(0)` 泵不会让它到期；
    ② 回合 spawn 之后、coroutine 第一步之前，`current_task` 已指向新任务——必须把
       "在途任务"也算作未落定，否则会在两回合之间提前返回。

    这里的时间等待是**活性探测**（等价于 `_settle` 的 `wait_for`），不是用 sleep 造顺序：
    顺序全部由上面的 Event 屏障决定。
    """
    if task is not None:
        await _settle(task)
    for _ in range(1000):
        current = agent._cognitive.current_task  # noqa: SLF001 — 只读任务句柄
        if current is not None and not current.done():
            await _settle(current)
            continue
        if agent.pending_messages or agent.bus.pending_user_inputs:
            await asyncio.sleep(0.01)
            continue
        if not agent.status.is_completely_idle:
            await asyncio.sleep(0.01)
            continue
        return
    raise AssertionError(
        f"回合未落定：pending={_pending_texts(agent)} "
        f"bus={agent.bus.pending_user_inputs} phase={agent.status.current_name()}")


async def _pump_until(predicate: Any, what: str) -> None:
    """让出事件循环直到谓词为真（结构化推进；超时 = 缺陷，不是判据）。"""
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"等待超时：{what}")


class _SpySpawnScope:
    """`AsyncTaskScope` 的透明代理：只多记一次 `spawn` 的任务名。

    `AsyncTaskScope` 有 `__slots__`，实例属性 `spawn` 只读 ⇒ 用代理换掉
    `agent._turns`（`Agent` 无 `__slots__`）。其余方法（`drain` / `cancel_all` /
    `aclose` / `pending`…）原样转发，停机路径行为不变。
    """

    def __init__(self, inner: Any, sink: list[str]) -> None:
        self._inner = inner
        self._sink = sink

    def spawn(self, coro: Any, **kw: Any) -> Any:
        self._sink.append(str(kw.get("name") or "?"))
        return self._inner.spawn(coro, **kw)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)

    def __len__(self) -> int:
        return len(self._inner)

    def __bool__(self) -> bool:
        return bool(self._inner)


def _post_direct_input(agent: Agent, text: str) -> None:
    """复刻**不经 `process_message`** 的生产输入路径。

    三个真实生产调用点：`core/input/ws_handler.py:733`（STT 转写）、
    `core/input/ws_v2.py:787`（voice.commit）、`core/input/voice_sessions.py:702`
    （REALTIME 段提交）——它们都直接 `bus.post(UserInputData(...))`。
    这里发出的是与之**逐字段同形**的事件（`request_id` 同样取 `next_request_id()`）。
    """
    agent.bus.post(UserInputData(
        text=text, user_name=agent.user_name,
        request_id=agent.next_request_id(),
    ))


# ---------------------------------------------------------------- guards


@pytest.mark.asyncio
async def test_three_message_burst_keeps_admission_order(
        agent: Agent, admission_gate: _Gate, llm: _RecordingFakeLLM):
    """PO1 / PO2 / PO6（**PRE_FIX_RED**）：连发三条的处理顺序 == admission 顺序。

    生产交错：第一条消息停在会话/角色装载窗口里，用户又连发两条（前端连发/快速双击）。
    修前实测 pending `["M3","M2"]`、处理顺序 `M1 → M3 → M2`。
    """
    rec = _install_recorder(agent, llm)
    first = await _arm_admission_gap(agent, admission_gate, "M1")
    rec.admitted.append("M1")

    assert await agent.process_message("M2") is True
    rec.admitted.append("M2")
    assert await agent.process_message("M3") is True
    rec.admitted.append("M3")

    # 层 1：M2/M3 确实进过 pending（否则这条用例没覆盖真实问题）
    await _wait_pending(agent, 2)
    assert _pending_texts(agent) == ["M2", "M3"], (
        f"pending 顺序 != admission 顺序：{_pending_texts(agent)}")

    admission_gate.open()
    await _wait_settled(agent, first)

    _assert_full_order(rec, agent)
    assert not agent.pending_messages, _pending_texts(agent)
    assert agent.status.is_completely_idle, agent.status.current_name()


@pytest.mark.asyncio
async def test_five_message_burst_keeps_admission_order(
        agent: Agent, admission_gate: _Gate, llm: _RecordingFakeLLM):
    """PO1 / PO2（**PRE_FIX_RED**）：burst 5——不是三条特例。"""
    rec = _install_recorder(agent, llm)
    first = await _arm_admission_gap(agent, admission_gate, "M1")
    rec.admitted.append("M1")
    for tok in ("M2", "M3", "M4", "M5"):
        assert await agent.process_message(tok) is True
        rec.admitted.append(tok)

    await _wait_pending(agent, 4)
    assert _pending_texts(agent) == ["M2", "M3", "M4", "M5"], _pending_texts(agent)

    admission_gate.open()
    await _wait_settled(agent, first)

    _assert_full_order(rec, agent)
    assert not agent.pending_messages, _pending_texts(agent)


@pytest.mark.asyncio
async def test_error_in_admission_window_then_drain_keeps_order(
        agent: Agent, admission_gate: _Gate, llm: _RecordingFakeLLM):
    """PO1 / PO4（**PRE_FIX_RED**）：当前回合在窗口内**失败**后，排队的输入仍按序处理。

    生产形态：会话/角色存储在窗口内 fail-closed（P0-3 `StorageUnavailableError`）。
    M1 从未接管 ⇒ 不在期望顺序里；M2/M3 必须 M2 → M3。
    """
    from core.storage.base import StorageUnavailableError

    rec = _install_recorder(agent, llm)
    first = await _arm_admission_gap(agent, admission_gate, "M1")
    assert await agent.process_message("M2") is True
    rec.admitted.append("M2")
    assert await agent.process_message("M3") is True
    rec.admitted.append("M3")
    await _wait_pending(agent, 2)

    admission_gate.fail_with = StorageUnavailableError("测试：权威存储不可用")
    admission_gate.open()
    await _wait_settled(agent, first)

    # M1 从未接管输入（失败终态），因此**不**出现在 claim 顺序里
    rec.admitted = ["M2", "M3"]
    _assert_full_order(rec, agent)
    assert "M1" not in rec._burst(rec.claims), "窗口内失败的输入不得获得 ownership"


@pytest.mark.asyncio
async def test_newer_direct_input_cannot_bypass_older_pending_input(
        agent: Agent, llm: _RecordingFakeLLM):
    """PO3（**PRE_FIX_RED**）：更晚的**直投**输入不得越过更早排队的输入。

    §79/§80 的规则：存在更早的 pending 输入时，新 admitted 输入不得 direct-fast-path
    越过它。这里用的是**生产真实存在的第二条入口**（STT/voice 直投 `bus.post`，见
    `_post_direct_input` 的调用点清单）——它不经过 `process_message`，所以既不看忙判定
    也不看 pending，修前会在 `_handle_user_input` 的 fall-through 分支里抢占当前回合并
    越过已排队的 M2。

    修前实测：`turn_a` 被取消（`cancelling() > 0`），处理顺序 `M1(残) → V1 → M2`；
    修后：V1 排队，顺序 `M1 → M2 → V1`。
    """
    rec = _install_recorder(agent, llm)
    llm.arm_stream_gate()
    assert await agent.process_message("M1") is True
    await asyncio.wait_for(llm.stream_entered.wait(), timeout=_PROBE_S)
    rec.admitted.append("M1")
    turn_a = agent._cognitive.current_task  # noqa: SLF001 — 只读任务句柄
    assert turn_a is not None and not turn_a.done()
    assert agent._cognitive.current_turn_owns_input is True  # noqa: SLF001
    assert agent.status.is_accepting_input is False, agent.status.current_name()

    # 输入 2：走公开入口，因"已接管 + 忙"而排队
    assert await agent.process_message("M2") is True
    rec.admitted.append("M2")
    await _wait_pending(agent, 1)
    assert _pending_texts(agent) == ["M2"], _pending_texts(agent)

    # 输入 3：生产形态的**直投**输入（STT/voice），比 M2 更晚 admitted
    _post_direct_input(agent, "V1")
    rec.admitted.append("V1")
    # 等"V1 已被处置"：修后它进 pending（2 条）；修前它抢占当前回合（任务被取消）。
    # 两种结局都推进到断言——PRE_FIX_RED 必须红在**行为断言**上，不是红在等待超时上。
    await _pump_until(
        lambda: len(agent.pending_messages) >= 2 or turn_a.cancelling() > 0,
        "V1 被消费者处置（排队或抢占）")

    assert turn_a.cancelling() == 0 and not turn_a.done(), (
        "更晚的直投输入抢占了正在产出、且已接管输入的回合，并越过更早排队的 M2")
    assert _pending_texts(agent) == ["M2", "V1"], (
        f"更晚的直投输入越过了更早排队的输入：{_pending_texts(agent)}")

    llm.open_stream_gate()
    await _wait_settled(agent)
    _assert_full_order(rec, agent)
    assert not agent.pending_messages, _pending_texts(agent)


@pytest.mark.asyncio
async def test_single_message_fast_path_unchanged(
        agent: Agent, llm: _RecordingFakeLLM):
    """ALREADY_GREEN_GUARD（AD6 / §43）：单条输入仍走既有快速路径，零行为变化。"""
    rec = _install_recorder(agent, llm)
    assert await agent.process_message("M1") is True
    assert agent.bus.pending_user_inputs == 1, "单条输入应直发 EventBus"
    assert _pending_texts(agent) == [], "快速路径不得经过 pending"
    rec.admitted.append("M1")
    await _wait_settled(agent)
    _assert_full_order(rec, agent)
    assert agent.status.is_completely_idle, agent.status.current_name()


@pytest.mark.asyncio
async def test_two_message_boundary_unchanged(
        agent: Agent, admission_gate: _Gate, llm: _RecordingFakeLLM):
    """ALREADY_GREEN_GUARD（§44/§53）：两条消息的既有边界行为不变（M1 → M2 原序）。"""
    rec = _install_recorder(agent, llm)
    first = await _arm_admission_gap(agent, admission_gate, "M1")
    rec.admitted.append("M1")
    assert await agent.process_message("M2") is True
    rec.admitted.append("M2")
    await _wait_pending(agent, 1)
    assert _pending_texts(agent) == ["M2"], _pending_texts(agent)

    admission_gate.open()
    await _wait_settled(agent, first)
    _assert_full_order(rec, agent)


@pytest.mark.asyncio
async def test_concurrent_entries_processed_in_admission_order(
        agent: Agent, admission_gate: _Gate, llm: _RecordingFakeLLM):
    """PO1 跨入口/跨连接（§36/§37）：并发投递时"谁先提交准入"就按谁先处理。

    合同**不**承诺"网络到达顺序"（那取决于 TCP 与调度，本批不扩大），只承诺**已
    accepted 的先后**。因此期望顺序由每条输入**自己的** admission 见证现算
    （`_assert_full_order` 的 rid 单调性），而不是测试假定的调用顺序——两种交错都成立。

    用 v2 投递（`reserve_work` 会挂起）才能真正让两个入口在 `process_message` 里
    交错；v1 没有 await、两个调用必然顺序执行。
    """
    rec = _install_recorder(agent, llm)
    first = await _arm_admission_gap(agent, admission_gate, "M1")
    rec.admitted.append("M1")

    results = await asyncio.gather(
        agent.process_message("M2", protocol_version=2, conversation_id="default"),
        agent.process_message("M3", protocol_version=2, conversation_id="default"),
    )
    assert results == [True, True], results
    rec.admitted.extend(["M2", "M3"])
    await _wait_pending(agent, 2)

    admission_gate.open()
    await _wait_settled(agent, first)
    _assert_full_order(rec, agent)
    assert not agent.pending_messages, _pending_texts(agent)


@pytest.mark.asyncio
async def test_newer_input_cannot_enter_bus_while_older_pending_waits(
        agent: Agent, llm: _RecordingFakeLLM):
    """PO1/PO3（**PRE_FIX_RED**，Challenger A 反证的最小交错）：更早的排队输入
    "正在从 pending 交给总线"时，更晚的输入不得抢到更小的总线 `seq`。

    ```text
    T0 已接管、产出中；M2（更早）忙时入队 ⇒ pending=[M2]
    T0 收口：transition(IDLE) + post STATE_CHANGE(IDLE)   ← 相位已是 IDLE
    两个出队点都被延迟（生产里等价于 notify_state / mark 等真实 await 变慢，
      以及 status 订阅者在 idle 通知上的挂起——后者正是 AD7 用例的既有技术）
    M3（更晚）admitted：相位 IDLE ＋ 总线无 USER_INPUT ⇒ 修前走**快速路径**进总线
    出队口把 M2 交给总线 ⇒ M2 的 seq 更大 ⇒ 总线顺序 [M3, M2]
    ⇒ `_handle_user_input(M3)` 时 M2 已不在 pending（判据看不见"交接中"的那条）
      ⇒ M3 先成为 active turn；M2 反被排在后面
    ```

    修法（PO3 的完整判据面）：`process_message` 把**非空 pending** 也视为等待者 ⇒ M3 入队，
    与 M2 保持 admission 顺序。这条只用既有挂起点（output 的 `notify_state` 等待 +
    `status` 的 idle 通知订阅者）与对既有方法的 spy，事件本身全是生产事件。
    """
    rec = _install_recorder(agent, llm)
    llm.arm_stream_gate()
    assert await agent.process_message("M1") is True
    await asyncio.wait_for(llm.stream_entered.wait(), timeout=_PROBE_S)
    rec.admitted.append("M1")

    assert await agent.process_message("M2") is True
    rec.admitted.append("M2")
    await _wait_pending(agent, 1)
    assert _pending_texts(agent) == ["M2"], _pending_texts(agent)

    # 延迟点 ①：消费者停在**这条 IDLE 事件**上（不处理 ⇒ 不出队）。
    parked = asyncio.Event()
    release = asyncio.Event()
    orig_dispatch = agent._dispatch_event

    async def spy_dispatch(data: Any) -> None:
        is_idle_change = (data.__class__.__name__ == "StateChangeData"
                          and str(getattr(data, "state", "")).lower().endswith("idle"))
        if is_idle_change and not parked.is_set():
            parked.set()
            await release.wait()
        await orig_dispatch(data)

    # 延迟点 ②：回合停在 idle 通知上（任务未结束 ⇒ done callback 的兜底出队也跑不到）。
    # 与 AD7 用例同一技术：`AgentStatus._notify` 逐个 await 订阅者。
    notified = asyncio.Event()
    release_notify = asyncio.Event()

    async def parking_subscriber(name: str) -> None:
        if name == "idle" and not notified.is_set():
            notified.set()
            await release_notify.wait()

    agent._dispatch_event = spy_dispatch  # type: ignore[method-assign]  # noqa: SLF001
    agent.status.on_change(parking_subscriber)
    llm.open_stream_gate()
    await asyncio.wait_for(parked.wait(), timeout=_PROBE_S)
    await asyncio.wait_for(notified.wait(), timeout=_PROBE_S)

    # 生产前提（断言，不是假设）：相位已 IDLE、更早者仍在 pending、总线无未派发输入
    assert agent.status.is_completely_idle, agent.status.current_name()
    assert _pending_texts(agent) == ["M2"], _pending_texts(agent)
    assert agent.bus.pending_user_inputs == 0, "用例前提：总线上不应有未派发的输入"

    # 更晚的输入：修前走快速路径（总线），修后必须入队（更早者仍在等）
    assert await agent.process_message("M3") is True
    rec.admitted.append("M3")
    assert agent.bus.pending_user_inputs == 0, (
        "更晚的输入在更早者仍排队时抢到了总线的更小 seq——"
        f"pending={_pending_texts(agent)}")
    assert _pending_texts(agent) == ["M2", "M3"], _pending_texts(agent)

    release_notify.set()
    release.set()
    await _wait_settled(agent)
    _assert_full_order(rec, agent)
    assert not agent.pending_messages, _pending_texts(agent)


@pytest.mark.asyncio
async def test_direct_input_cannot_overtake_when_older_pending_is_in_flight(
        agent: Agent, llm: _RecordingFakeLLM):
    """PO1/PO3（**PRE_FIX_RED**，Challenger A 反证的**直投变体**）：更早的排队输入
    "已出队交给总线、尚未被 claim"时，更晚的直投输入不得越过它。

    与上一条的区别：那条的新输入走 `process_message`（可被入队判据挡住），这条走
    **直投**（`ws_handler.py` STT / `ws_v2.py` voice.commit / `voice_sessions.py` REALTIME，
    不经 `process_message`）——它**只**能被 claim 侧判据挡住，而 claim 侧当时只看
    `_pending_messages`：更早者已被 `pop(0)` 搬走 ⇒ 判据失明 ⇒ 反序。

    ```text
    T0 已接管、产出中；M2（更早）忙时入队 ⇒ pending=[M2]
    V1（更晚）直投 ⇒ bus=[V1]
    消费者被延迟（生产里等价于 notify_state / mark 等真实 await 变慢）
    T0 收口 ⇒ 出队口把 M2 交给总线 ⇒ bus=[V1(seq 小), M2(seq 大)]
    ⇒ 派发 V1 时 M2 已不在 pending ⇒ 修前 spawn V1（反序）
    ```

    修法：claim 判据的等待集必须是 **pending ∪ 总线驻留**（`_has_older_waiting_input`
    读 `EventBus.pending_user_input_request_ids()`）。
    """
    rec = _install_recorder(agent, llm)
    llm.arm_stream_gate()
    assert await agent.process_message("M1") is True
    await asyncio.wait_for(llm.stream_entered.wait(), timeout=_PROBE_S)
    rec.admitted.append("M1")

    assert await agent.process_message("M2") is True
    rec.admitted.append("M2")
    await _wait_pending(agent, 1)
    assert _pending_texts(agent) == ["M2"], _pending_texts(agent)

    # 延迟消费者：停在任何一条事件上（用 INTERRUPT 之外的真实事件不好控制 ⇒ 用
    # **同一时刻** M1 收口会投出的 STATE_CHANGE(IDLE)；这里延迟它即可）。
    parked = asyncio.Event()
    release = asyncio.Event()
    orig_dispatch = agent._dispatch_event
    seen: list[str] = []

    async def spy_dispatch(data: Any) -> None:
        seen.append(data.__class__.__name__)
        if not parked.is_set():
            parked.set()
            await release.wait()
        await orig_dispatch(data)

    agent._dispatch_event = spy_dispatch  # type: ignore[method-assign]  # noqa: SLF001

    # 更晚的**直投**输入（与三个生产调用点逐字段同形）
    _post_direct_input(agent, "V1")
    rec.admitted.append("V1")
    await asyncio.wait_for(parked.wait(), timeout=_PROBE_S)

    # 放行 M1 的收口：出队口会把 M2 交给总线（此时 V1 已在总线上，seq 更早）。
    # 等待条件刻意只用**既有**可观测状态（`pending` 被清空 = 出队已发生），
    # 不引用本批新增的任何 API——否则修前会红在 AttributeError 上，而不是红在行为上。
    llm.open_stream_gate()
    await _pump_until(
        lambda: not agent.pending_messages,
        "出队口把 M2 交给总线")
    # 前提断言（防假绿，Challenger A 第二轮）：此刻更早者**必须已不在 pending**
    # （= 已交给总线、尚未被 claim 的交接态）；否则这条用例只是又测了一遍 pending 那一半。
    assert not agent.pending_messages, (
        f"用例前提未达成：更早者仍在 pending={_pending_texts(agent)}")
    assert agent._cognitive.current_task is None or agent._cognitive.current_task.done(), (  # noqa: SLF001
        "用例前提：当前不应有在途回合（更早者必须停在被 claim 之前）")
    release.set()
    await _wait_settled(agent)

    assert rec._burst(rec.claims) == rec._burst(rec.admitted), (
        "更晚的直投输入越过了更早的排队输入（等待集缺了'已出队交给总线'那一半）："
        f"claims={rec.claims} pending={_pending_texts(agent)}")
    _assert_full_order(rec, agent)


@pytest.mark.asyncio
async def test_queue_never_loses_its_drain_trigger(
        agent: Agent, admission_gate: _Gate, llm: _RecordingFakeLLM):
    """PO-liveness（**PRE_FIX_RED**，Challenger A 第二轮反证的停滞）：pending 非空
    时**必须**始终存在一个出队触发，否则整条输入流永久静音。

    pending 没有自己的触发器——它只靠两个出队口：① `STATE_CHANGE→IDLE`；② 回合任务的
    done callback。**主动搭话**（`_handle_system_tick`）与**后台任务唤醒**
    （`_handle_task_result`）这两条回合来源此前**不带 done callback**。于是一个
    "停在 admission 窗口里失败、从未离开 IDLE"的回合结束时：

    ```text
    回合停在 get_or_load（FSM 仍 IDLE）时，用户 T1 被 not_owning 排队 ⇒ pending=[T1]
    回合在 _prepare_turn 内 fail-closed ⇒ handle_interrupt() 见 FSM 已 IDLE 直接 return
      ⇒ 不投 STATE_CHANGE(IDLE) ⇒ 出队口 ① 不触发
    该回合没有 done callback ⇒ 出队口 ② 不触发
    ⇒ T1 永久留在 pending（而 Agent 显示完全空闲）；越队判据又把后续输入
      （公开入口 / 直投）全算成"更晚"而排队 ⇒ 整条输入流静音
    ```

    修法：兜底出队挂在**每一个**回合 spawn 点上（`_on_auxiliary_turn_done`），复用既有
    `_drain_pending_after_turn`，不新增调度器/轮询/计时器。
    """
    from core.schema.event import SystemTickData
    from core.storage.base import StorageUnavailableError

    rec = _install_recorder(agent, llm)
    # ① 主动搭话回合：停在 admission 窗口（FSM 仍 IDLE）
    admission_gate.arm()
    agent.bus.post(SystemTickData(reason="silence_timeout", elapsed=120))
    await asyncio.wait_for(admission_gate.entered.wait(), timeout=_PROBE_S)
    proactive = agent._cognitive.current_task  # noqa: SLF001 — 只读任务句柄
    assert proactive is not None and not proactive.done()

    # ② 用户输入：被排队（在途回合尚未接管输入）
    assert await agent.process_message("T1") is True
    rec.admitted.append("T1")
    await _wait_pending(agent, 1)
    assert _pending_texts(agent) == ["T1"], _pending_texts(agent)

    # ③ 主动搭话回合在窗口内 fail-closed（P0-3 生产形态）⇒ 从未离开 IDLE
    admission_gate.fail_with = StorageUnavailableError("测试：权威存储不可用")
    admission_gate.open()
    await _settle(proactive)

    # ④ 队列必须仍然活着：T1 重新获得处理权
    await _pump_until(lambda: not agent.pending_messages, "T1 重新获得处理权（队列未失去触发器）")
    await _wait_settled(agent)
    assert "T1" in rec._burst(rec.claims), (
        f"已 accepted 的排队输入永久静音：claims={rec.claims} pending={_pending_texts(agent)}")
    _assert_full_order(rec, agent)

    # ⑤ 后续输入（公开入口 + 直投）必须继续按序被处理，而不是被静音
    rec.admitted = ["T1"]
    assert await agent.process_message("T2") is True
    rec.admitted.append("T2")
    _post_direct_input(agent, "V3")
    rec.admitted.append("V3")
    await _wait_settled(agent)
    _assert_full_order(rec, agent)
    assert agent.status.is_completely_idle, agent.status.current_name()


@pytest.mark.asyncio
async def test_shutdown_does_not_drain_pending(
        agent: Agent, llm: _RecordingFakeLLM):
    """PO6 / §41（PRODUCT_CONTRACT_GUARD）：停机不得为了 FIFO 复活 pending。

    M1 在产出、M2/M3 已 accepted 并排队；此时 `Agent.stop()`。顺序不变量**不**覆盖
    停机后的复活：M2/M3 必须保持未处理终态（pending 被清空并释放 lease），
    不得在 STOPPED 之后再起回合——**连回合都不许起**。
    """
    rec = _install_recorder(agent, llm)
    llm.arm_stream_gate()
    assert await agent.process_message("M1") is True
    await asyncio.wait_for(llm.stream_entered.wait(), timeout=_PROBE_S)
    rec.admitted.append("M1")
    for tok in ("M2", "M3"):
        assert await agent.process_message(tok) is True
        rec.admitted.append(tok)
    await _wait_pending(agent, 2)
    assert _pending_texts(agent) == ["M2", "M3"], _pending_texts(agent)

    # 记录"停止之后还起没起新的回合"——只看 claim 不够：被停止取消的输入即使起了回合
    # 也会在取 `_process_lock` 时被取消，claim 判据看不见它（变异 M6a"停机也 drain"
    # 因此逃过一版判据）。这里直接 spy 回合 spawn（`Agent._turns.spawn`）。
    spawned: list[str] = []
    agent._turns = _SpySpawnScope(agent._turns, spawned)  # type: ignore[assignment]  # noqa: SLF001
    llm.open_stream_gate()
    await agent.stop()

    await _pump_until(lambda: not agent.pending_messages, "停机清空 pending")
    assert not agent.pending_messages, (
        f"停机后 pending 未清空（lease 泄漏 / 可能被复活）：{_pending_texts(agent)}")
    processed = rec._burst(rec.claims) + rec._burst(rec.llm_turns)
    assert "M2" not in processed and "M3" not in processed, (
        f"停机后被取消的排队输入被复活处理：claims={rec.claims} llm={rec.llm_turns}")
    for name in spawned:
        assert "M2" not in name and "M3" not in name, (
            f"停机为了 FIFO 把已取消的排队输入起成了回合：spawned={spawned}")
    assert agent.status.is_completely_idle, agent.status.current_name()


@pytest.mark.asyncio
async def test_stop_button_clears_pending_without_reordering_survivors(
        agent: Agent, llm: _RecordingFakeLLM):
    """PO5（PRODUCT_CONTRACT_GUARD）：显式停止只移除被取消的输入，不重排幸存者。

    既有合同（Batch 12 已确证）：停止按钮 = 放弃尚未开始的输入（bus + pending 都清），
    且**可归因**。这条钉住"清除"不得变成"重排"：停止之后新 admitted 的输入仍按序处理，
    被清除的输入不得复活。
    """
    rec = _install_recorder(agent, llm)
    llm.arm_stream_gate()
    assert await agent.process_message("M1") is True
    await asyncio.wait_for(llm.stream_entered.wait(), timeout=_PROBE_S)
    rec.admitted.append("M1")
    turn_a = agent._cognitive.current_task  # noqa: SLF001 — 只读任务句柄
    assert await agent.process_message("M2") is True
    await _wait_pending(agent, 1)

    agent.bus.post(InterruptData(source="websocket"), priority=EventPriority.INTERRUPT)
    await _pump_until(lambda: not agent.pending_messages, "停止按钮清空 pending")
    assert not agent.pending_messages, (
        f"停止按钮未清空排队输入（既有合同）：{_pending_texts(agent)}")

    llm.open_stream_gate()
    await _wait_settled(agent, turn_a)
    assert "M2" not in rec._burst(rec.claims), (
        f"被停止取消的输入被复活：{rec.claims}")

    # 停止之后 admitted 的输入仍按序处理（M3 → M4）
    rec.admitted = ["M1"]
    for tok in ("M3", "M4"):
        assert await agent.process_message(tok) is True
        rec.admitted.append(tok)
    await _wait_settled(agent)
    _assert_full_order(rec, agent)


@pytest.mark.asyncio
async def test_admission_order_key_is_monotone(agent: Agent):
    """ORDERING_KEY 合同守卫：admission 顺序的见证必须**实例内单调**。

    排序键复用既有的 `request_id`（`next_request_id()` 的 `{n:03d}` 计数器）：它在
    `process_message` 的 admission 提交点分配（`reserve_work` 之后、分支判定之前，
    中间无 await），因此 `int(request_id)` 就是 admission order。

    这条把"依赖它的单调性"从注释变成机制：将来有人把 request_id 改成 UUID/每会话
    计数/随机值，这里会红，而不是让顺序静默错乱。
    """
    ids = [agent.next_request_id() for _ in range(5)]
    values = [int(rid) for rid in ids]
    assert values == sorted(values) and len(set(values)) == len(values), (
        f"request_id 不再是实例内严格单调的整数序列：{ids}")
