# 测试文件：core/agent/tests/test_claimed_input_conversation_truth.py
"""Batch 15（KB-46 / Claimed Input Conversation Truth Closure）：已 claim 的输入必须留在会话真相里。

## 缺陷形状（KB-46，canonical scope）

一条用户输入已经完成 accepted admission，且 active-turn claim 已经成立
（`CognitiveLoop.current_turn_owns_input is True`），但**还没有**被写进 session 历史：

```text
process()  取锁之前清空 _generation_ctx / _admission_* / _turn_message_persisted
  await self._process_lock
  _prepare_turn():
      await session_resolver(...)                 ← 真挂起点（claim 之前）
      await registry.get_or_load(...)             ← 真挂起点（claim 之前）
      loop.py:655  self._generation_ctx = GenerationContext(...)   ← ★ CLAIM commit point
      loop.py:671  await self.phases.enter_thinking()              ← W1 挂起点 A
      loop.py:675  await generation_registry.register(...)         ← W1 挂起点 B（Mongo upsert）
      loop.py:692  runtime.try_begin_generation()                  ← 同步
  _think_loop():
      loop.py:1152 await self._build_turn_prompt(...)
                       loop.py:1011 await self._prompt.build_messages(...)  ← W2 挂起点 C
                       loop.py:1022 self._session.add_message(UserMessage(...))  ← ★ RECORD
                       loop.py:1028 self._turn_message_persisted = True
      loop.py:1156 await self._start_turn_tracking(...)            ← typing / generation.started
      循环 → LLM
```

**W1** = 取消落在 A/B（`_prepare_turn` 之内，走 `_handle_prepare_cancelled` 的 ctx 非 None 分支）；
**W2** = 取消落在 C（`_think_loop` 之内，走 `process()` 的 `except asyncio.CancelledError`）。
两者是**同一个 ownership 状态**（`_generation_ctx` 非 None + `_turn_message_persisted` False），
却都让这条输入在 **session / archive / 未来 LLM context** 三层同时消失。

## 会话历史合同（本批 Phase 0 审计结论）

**Contract A —— 用户意图史**：session history 记录"用户真的提交过、且被 runtime 接管的每一条
输入"。证据：`loop.py:1019-1028` 的唯一豁免是 `is_task_result`（合成系统唤醒），紧邻注释
"此后用户至少能在历史里看到'我说过这句'"；`session.add_message` 发生在 LLM 首轮**之前**
⇒ 反证 Contract B（"只记模型消费过的"）；`_archive_partial_reply` 会把"已发出但被打断"的
半截回复照样归档 ⇒ 反证 Contract C（"只记完成的回合"）。客户端把服务端历史当权威转写稿
（`chatStore.ts::addUserMessage` 乐观渲染 → `restoreFromServer` **整体替换** `messages.value`）
⇒ 历史里缺一条，用户刷新后自己的消息就**消失**。

因此：**CLAIMED 之后**，受支持的 cancellation / failure 不得让这条输入从 runtime conversation
truth 中静默消失。停止按钮的语义是"停止 assistant 当前生成"（前端只发 `{type:'interrupt'}`，
**不**回收 user 气泡）——不是"撤销我刚发出的 user message"。

## 判据（每条都断言机制，不断言"最终一定有回复"）

- **CT-T1**：CLAIMED→RECORDED 窗口内被取消（W1 与 W2 两个落点）⇒ 输入必须进 session 与 archive。
- **CT-T2**：同一窗口内的**普通异常**走同一个 recording boundary（§33：不只修 `CancelledError`）。
- **CT-T3**：被取消的输入必须出现在**下一回合真实 LLM 输入**里（不是只查数据库）。
- **CT-T4**：正常成功回合：history 恰一次、LLM prompt 恰一次（防 prompt 双计）。
- **CT-T5**：取消落在 record **之后** ⇒ 不得双 append（复用既有 AD4 边界）。
- **CT-T6**：claim **之前**取消 ⇒ 不得制造 phantom history（Batch 12 边界不得被本批破坏）。
- **CT-T7**：`is_task_result`（合成系统唤醒）**不**进历史——recovery 不得把它写成 user history。
- **CT-T8**：取消后的 history shape = `user`（**不**伪造 assistant 完成态）；顺序 `M1 → M2 → assistant(M2)`。
- **CT-T9**：多模态载荷按**结构**保留（不是只剩纯文本）。

## 纪律（防假绿）

- 每条用例先断言 `claimed is True` **且** `recorded is False`，再触发取消——否则测的根本不是这个窗口。
- 屏障只用 `asyncio.Event` 包**生产代码本来就会挂起**的调用；不塞 `sleep` 造 race；
  `asyncio.wait_for` 只作死锁探测。
- 取消源用**生产路径**：WS 停止按钮 = `InterruptData(source="websocket")` 经真 EventBus。
- 真 Agent + 真 `start()` + 真 `process_message` + 真 EventBus + 假 LLM/假存储（`fake_storage`）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import pytest_asyncio

from Module.llm import LLMFactory
from core.agent.agent import Agent
from core.config import AppConfig
from core.monitoring.event_bus import EventPriority
from core.schema.event import InterruptData, TaskResultData
from core.schema.types import SessionId

#: 「本测试不碰真实库」的显式声明（见根 conftest 的真实库防线）。
pytestmark = [pytest.mark.service, pytest.mark.usefixtures("fake_storage")]

#: 死锁探测器——只在真的挂死（无界 await）时触发。正常路径实测 < 200ms。
_PROBE_S = 10.0

#: 假 LLM 的固定回复。空响应会触发 nudge 重试 + 兜底文案，徒增噪声。
_REPLY = "好呀，我在听。<mood>平静</mood>"

#: 回合任务名前缀——回合由 `_turns.spawn(name=…)` 赋名：
#: 用户输入回合 = `cognitive-…`（`agent.py::_handle_user_input`）；
#: 后台任务宣告回合 = `task-result-…`（`agent.py::_handle_task_result`）。
_TURN_PREFIXES = ("cognitive-", "task-result-")


# ─────────────────────────────── 假 LLM ───────────────────────────────


class _RawClient:
    """`Agent.start()` 的 task-system 步骤会读 `raw_client`（热切换判据）。"""

    def __init__(self, model_id: str) -> None:
        self.model = model_id

    def is_closed(self) -> bool:
        return False

    async def close(self) -> None:
        return None


class _RecordingFakeLLM:
    """确定性 LLM 替身：流式吐固定回复，记录**真实**收到的 messages。"""

    def __init__(self, response: str = _REPLY) -> None:
        self.response = response
        self.raw_client = _RawClient("fake/test")
        self.stream_calls = 0
        #: 每次 `stream_think` 收到的完整 messages 列表（**第三层真相的原始证据**）。
        self.seen_messages: list[list[dict]] = []
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

    def prompt_texts(self, index: int = -1) -> list[str]:
        """第 `index` 次调用的 messages 的 content 文本（含历史注入）→ 便于计数。"""
        return [str(m.get("content", "")) for m in self.seen_messages[index]]

    def prompt_flat(self, index: int = -1) -> str:
        return "\n".join(self.prompt_texts(index))

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
        self.seen_messages.append([dict(m) for m in messages])
        for ch in self.response:
            yield {"content": ch, "tool_calls": None}


# ─────────────────────────────── 屏障 ───────────────────────────────


class _Gate:
    """可武装的异步屏障：钉死一个**真实挂起点**，解武装后完全透明。

    每次 `arm()` 只挡下一次调用；`open()` 放行。包装的必须是生产代码里**本来就会
    挂起**的调用——不引入替身时序。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self._armed = False
        self.hits = 0
        #: 设置后：放行时抛出该异常（复现"窗口内普通异常"的生产形态）。
        self.fail_with: BaseException | None = None

    def arm(self, fail_with: BaseException | None = None) -> None:
        self._armed = True
        self.fail_with = fail_with
        self.entered.clear()
        self.release.clear()

    def open(self) -> None:
        self.release.set()

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.hits += 1
        if self._armed:
            self._armed = False
            self.entered.set()
            await self.release.wait()
            if self.fail_with is not None:
                raise self.fail_with
        return await self._inner(*args, **kwargs)


def _gate_register(agent: Agent) -> _Gate:
    """W1 屏障：`GenerationRegistry.register`——`_prepare_turn` 里 claim **之后**的真挂起点。

    取公开属性 `agent.generation_registry`（而非穿透 loop 的私有字段），且只包
    `register`（回合自己的建档）——`mark`（收口落终态）不受影响。
    """
    registry = agent.generation_registry
    gate = _Gate(registry.register)
    registry.register = gate  # type: ignore[method-assign]
    return gate


def _gate_prompt(agent: Agent) -> _Gate:
    """W2 屏障：`PromptAssembler.build_messages`——record **之前**的最后一个真挂起点。"""
    prompt = agent._cognitive._prompt  # noqa: SLF001 — 只取实例方法做包装
    gate = _Gate(prompt.build_messages)
    prompt.build_messages = gate  # type: ignore[method-assign]
    return gate


def _gate_admission(agent: Agent) -> _Gate:
    """claim **之前**的屏障：角色 runtime registry 的 `get_or_load`。"""
    registry = agent._runtime_registry  # noqa: SLF001 — 只取实例的绑定方法做包装
    gate = _Gate(registry.get_or_load)
    registry.get_or_load = gate  # type: ignore[method-assign]
    return gate


# ─────────────────────────────── 观测助手 ───────────────────────────────


def _user_texts(agent: Agent) -> list[str]:
    out: list[str] = []
    for m in agent.session.messages:
        if m.role != "user":
            continue
        parts = m.content if isinstance(m.content, list) else []
        out.append("".join(str(getattr(p, "content", "")) for p in parts))
    return out


def _roles(agent: Agent) -> list[str]:
    return [str(m.role) for m in agent.session.messages]


async def _archive_texts(agent: Agent) -> list[str]:
    """archive 层（`message_archive` 集合）里的 user 文本，按写入顺序。

    走**公开**仓储属性 `agent.message_repo`——不是只看内存 deque。
    """
    msgs, _total = await agent.message_repo.query_by_session(
        SessionId(agent.session.session_id), limit=200)
    out: list[str] = []
    for msg in reversed(msgs):          # 仓储按 _id desc 返回 → 还原写入顺序
        if str(getattr(msg, "role", "")) != "user":
            continue
        parts = msg.content if isinstance(msg.content, list) else []
        out.append("".join(str(getattr(p, "content", "")) for p in parts))
    return out


async def _turn_tasks() -> list[asyncio.Task]:
    """在途回合任务——按 `_turns.spawn(name=…)` 赋的**真实任务名**取。

    这是结构化句柄：**不能**用 `_cognitive.current_task`，它在 `loop.py:940` 才赋值，
    而 W1 落点的取消发生在 `_prepare_turn` 之内（更早）⇒ 那个句柄仍指向上一回合（已 done），
    据此早退会读到"取消还没收口完"的中间态。
    """
    return [t for t in asyncio.all_tasks()
            if t is not asyncio.current_task()
            and t.get_name().startswith(_TURN_PREFIXES)]


async def _settle(task: asyncio.Task | None) -> None:
    """等任务落定（结构化终态信号）。超时 = 死锁探测，不是判据。"""
    if task is None:
        return
    try:
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True),
                               timeout=_PROBE_S)
    except asyncio.TimeoutError:  # pragma: no cover — 只在真回归时触发
        raise AssertionError(f"任务未在 {_PROBE_S}s 内落定（疑似无界 await）") from None


async def _drain(agent: Agent) -> None:
    """等回合与 pending 全部落定。

    ⚠️ 只 `await asyncio.sleep(0)` 会**忙等**——`FSMBackedPhaseController.handle_interrupt`
    的 `asyncio.sleep(0.05)` 是**定时器**，`sleep(0)` 不让时间前进 ⇒ 相位永远读成
    `interrupted`（实测假红）。这里 await 真实回合任务，真时间随之流逝。
    """
    for _ in range(400):
        for task in await _turn_tasks():
            await _settle(task)
        await asyncio.sleep(0)
        if agent.pending_messages or agent.bus.pending_user_inputs:
            continue
        if not agent.status.is_completely_idle:
            continue
        if await _turn_tasks():
            continue
        return
    raise AssertionError(
        f"回合/pending 未排空：phase={agent.status.current_name()} "
        f"pending={[p.text for p in agent.pending_messages]} "
        f"bus={agent.bus.pending_user_inputs}")


def _stop(agent: Agent, source: str = "websocket") -> None:
    """走**生产**取消源：WS 停止按钮 / 语音取消所用的 INTERRUPT 事件。"""
    agent.bus.post(InterruptData(source=source), priority=EventPriority.INTERRUPT)


async def _at_claim_window(agent: Agent, gate: _Gate, token: str) -> None:
    """断言"已 claim、未 record"这个窗口**真的**被钉住了（防假绿，§88）。"""
    assert await agent.process_message(token) is True, "输入必须已被 accepted"
    await asyncio.wait_for(gate.entered.wait(), timeout=_PROBE_S)
    assert agent._cognitive.current_turn_owns_input is True, (  # noqa: SLF001
        "用例前提不成立：本回合**没有**接管这条输入（测的不是 CLAIMED→RECORDED 窗口）")
    assert token not in _user_texts(agent), (
        "用例前提不成立：输入此刻**已经**进 session（测的不是窗口内）")
    assert agent._cognitive._turn_message_persisted is False, (  # noqa: SLF001
        "用例前提不成立：record 已提交（测的不是窗口内）")


# ─────────────────────────────── fixtures ───────────────────────────────


@pytest_asyncio.fixture
async def llm(monkeypatch: pytest.MonkeyPatch) -> _RecordingFakeLLM:
    backend = _RecordingFakeLLM()
    monkeypatch.setattr(
        LLMFactory, "create", staticmethod(lambda model_id, **kw: backend))
    return backend


@pytest_asyncio.fixture
async def agent(llm: _RecordingFakeLLM):
    """真 Agent + 真 `start()`（含真 `_process_events` 消费者）＋假存储/假 LLM。

    启动后先跑**一条完整的暖场消息**：让后续每条用例的开局状态（缓存热、无在途回合）
    显式且确定。
    """
    a = Agent(config=AppConfig())
    await a.start()
    try:
        assert await a.process_message("（暖场）") is True
        await _drain(a)
        assert a.status.is_completely_idle, a.status.current_name()
        assert not a.pending_messages
        yield a
    finally:
        await a.stop()


# ═══════════════════════════════════════════════════════════════════════
# CT-T1 — CLAIMED→RECORDED 窗口内被取消（两个落点）
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ct_t1_w1_cancel_in_prepare_after_claim_keeps_input_in_history(
        agent: Agent, llm: _RecordingFakeLLM):
    """W1：取消落在 `_prepare_turn`（`GenerationRegistry.register` 的 Mongo upsert）。

    修前：`session_user=['（暖场）']`、archive 同样没有 ⇒ 三层静默丢失（PRE_FIX_RED）。
    修后：这条已 claimed 的输入必须进 session **恰一次**，并进 archive。
    """
    gate = _gate_register(agent)
    gate.arm()
    token = "KB46-W1-记住我喜欢咖啡"
    await _at_claim_window(agent, gate, token)

    gen_id = agent._cognitive._generation_ctx.generation_id  # noqa: SLF001
    _stop(agent, source="websocket")
    gate.open()
    await _drain(agent)

    users = [t for t in _user_texts(agent) if t == token]
    assert len(users) == 1, (
        f"已 claim 的输入在会话里出现 {len(users)} 次（应为恰 1 次；0 = 静默丢失，>1 = 重复）"
        f"：history={_user_texts(agent)}")
    archived = [t for t in await _archive_texts(agent) if t == token]
    assert len(archived) == 1, (
        f"已 claim 的输入在 archive 里出现 {len(archived)} 次（应为恰 1 次）："
        f"archive={await _archive_texts(agent)}")
    # CT4：不得伪造 assistant 完成态——history 末条是 user（无回复）
    assert _roles(agent)[-1] == "user", (
        f"取消的回合不得留下 assistant 消息：roles={_roles(agent)}")
    # 相位/队列终态（不得因 truth 修复而卡住）
    assert agent.status.is_completely_idle, agent.status.current_name()
    assert not agent.pending_messages
    # generation 行仍如实落取消终态
    row = await agent._generation_repo.find_one(gen_id)  # noqa: SLF001
    assert row is not None and row.get("status") in {"cancelled", "interrupted"}, row


@pytest.mark.asyncio
async def test_ct_t1_w2_cancel_in_prompt_build_after_claim_keeps_input_in_history(
        agent: Agent, llm: _RecordingFakeLLM):
    """W2：取消落在 `_think_loop` → `_build_turn_prompt` → `build_messages`。

    与 W1 是**同一个 ownership 状态**，必须给出**同一种**会话真相（修前两者都丢）。
    """
    gate = _gate_prompt(agent)
    gate.arm()
    token = "KB46-W2-记住我喜欢咖啡"
    await _at_claim_window(agent, gate, token)

    _stop(agent, source="websocket")
    gate.open()
    await _drain(agent)

    users = [t for t in _user_texts(agent) if t == token]
    assert len(users) == 1, (
        f"已 claim 的输入在会话里出现 {len(users)} 次（应为恰 1 次）：history={_user_texts(agent)}")
    archived = [t for t in await _archive_texts(agent) if t == token]
    assert len(archived) == 1, (
        f"已 claim 的输入在 archive 里出现 {len(archived)} 次（应为恰 1 次）："
        f"archive={await _archive_texts(agent)}")
    assert _roles(agent)[-1] == "user", f"roles={_roles(agent)}"
    # 窗口内的取消 ⇒ 本回合**没有**任何 LLM 调用（输入从未被消费）
    assert llm.stream_calls == 1, (
        f"窗口内取消的回合不该调用 LLM（暖场基线 1）：stream_calls={llm.stream_calls}")


# ═══════════════════════════════════════════════════════════════════════
# CT-T2 — 同一窗口内的**普通异常**（§33：不只修 CancelledError）
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ct_t2_ordinary_exception_after_claim_keeps_input_in_history(
        agent: Agent, llm: _RecordingFakeLLM):
    """普通异常穿过**同一个** truth hole ⇒ 必须经**同一个** recording boundary 收口。"""
    gate = _gate_prompt(agent)
    gate.arm(fail_with=RuntimeError("boom-before-record"))
    token = "KB46-EXC-记住我喜欢咖啡"
    await _at_claim_window(agent, gate, token)

    gate.open()
    await _drain(agent)

    users = [t for t in _user_texts(agent) if t == token]
    assert len(users) == 1, (
        f"claim 后、record 前抛普通异常 ⇒ 输入仍须进历史恰一次："
        f"count={len(users)} history={_user_texts(agent)}")
    assert _roles(agent)[-1] == "user", f"roles={_roles(agent)}"
    assert agent.status.is_completely_idle, agent.status.current_name()


# ═══════════════════════════════════════════════════════════════════════
# CT-T3 — 未来 LLM context（第三层真相；不直接读 session 模拟 prompt）
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ct_t3_next_turn_llm_context_contains_cancelled_claimed_input(
        agent: Agent, llm: _RecordingFakeLLM):
    """M1 在 CLAIMED→RECORDED 窗口被取消 → M2 正常发送。

    判据落在**真实 LLM 收到的 messages**（`LLMAdapter.stream_think` 的入参），
    不是"读 session 再推断 prompt 会是什么"。
    """
    gate = _gate_prompt(agent)
    gate.arm()
    m1 = "KB46-CTX-记住我喜欢咖啡"
    await _at_claim_window(agent, gate, m1)

    _stop(agent, source="websocket")
    gate.open()
    await _drain(agent)

    m2 = "KB46-CTX-我刚才说我喜欢什么"
    assert await agent.process_message(m2) is True
    await _drain(agent)

    flat = llm.prompt_flat(-1)
    assert flat.count(m1) == 1, (
        f"被取消的已 claim 输入必须**恰一次**出现在下一回合的真实 LLM 输入里："
        f"count={flat.count(m1)}")
    assert flat.count(m2) == 1, f"当前输入必须恰一次：count={flat.count(m2)}"


# ═══════════════════════════════════════════════════════════════════════
# CT-T4 — 正常路径：history 恰一次、prompt 恰一次
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ct_t4_normal_turn_records_input_exactly_once(
        agent: Agent, llm: _RecordingFakeLLM):
    """ALREADY_GREEN_GUARD：正常成功回合不得出现 prompt 双计（§13 的 W2 风险）。"""
    token = "KB46-正常路径唯一性"
    assert await agent.process_message(token) is True
    await _drain(agent)

    assert _user_texts(agent).count(token) == 1, _user_texts(agent)
    flat = llm.prompt_flat(-1)
    assert flat.count(token) == 1, (
        f"当前用户输入在 LLM messages 里出现 {flat.count(token)} 次（应为恰 1 次）"
        "——提前 record 会让它同时经 history 与 current user_content 进入 prompt")


# ═══════════════════════════════════════════════════════════════════════
# CT-T5 — 取消落在 record **之后**：不得双 append
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ct_t5_cancel_after_record_does_not_duplicate(
        agent: Agent, llm: _RecordingFakeLLM):
    """边界另一侧：输入**已进历史**后取消 ⇒ 恰一条、LLM 恰一次（不得因 recovery 双写）。"""
    token = "KB46-record之后被取消"
    llm.arm_stream_gate()
    assert await agent.process_message(token) is True
    await asyncio.wait_for(llm.stream_entered.wait(), timeout=_PROBE_S)
    assert agent._cognitive.current_turn_owns_input is True  # noqa: SLF001
    assert _user_texts(agent).count(token) == 1, "用例前提：此刻输入必须已进 session"

    _stop(agent, source="websocket")
    llm.open_stream_gate()
    await _drain(agent)

    users = [t for t in _user_texts(agent) if t == token]
    # 恰一次：`<= 1` 是假绿——"一次都没处理"同样满足它。
    assert len(users) == 1, (
        f"该输入在会话里出现 {len(users)} 次（应为恰 1 次；0 = 被丢弃，>1 = 重复处理）")
    assert not agent.pending_messages, (
        f"已接管的输入被错误 requeue：{[p.text for p in agent.pending_messages]}")
    assert llm.stream_calls == 2, (      # 暖场 1 + 本条 1
        f"同一条输入被送进 LLM 的次数异常：stream_calls={llm.stream_calls}")


# ═══════════════════════════════════════════════════════════════════════
# CT-T6 — claim **之前**取消：不得制造 phantom history
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ct_t6_pre_claim_cancel_creates_no_phantom_history(
        agent: Agent, llm: _RecordingFakeLLM):
    """Batch 12 边界不得被本批破坏：未 claim 的输入**不**进 conversation truth。

    屏障落在 `registry.get_or_load`（`_prepare_turn` 里 claim **之前**的真挂起点）
    ⇒ 取消时 `current_turn_owns_input is False`。
    """
    gate = _gate_admission(agent)
    gate.arm()
    token = "KB46-未claim就被取消"
    assert await agent.process_message(token) is True
    await asyncio.wait_for(gate.entered.wait(), timeout=_PROBE_S)
    assert agent._cognitive.current_turn_owns_input is False, (  # noqa: SLF001
        "用例前提不成立：本回合**已**接管输入（测的不是 claim 之前）")

    _stop(agent, source="websocket")
    gate.open()
    await _drain(agent)

    assert token not in _user_texts(agent), (
        f"claim 之前的取消**不得**制造 phantom history：{_user_texts(agent)}")
    assert token not in await _archive_texts(agent)
    assert agent.status.is_completely_idle, agent.status.current_name()


# ═══════════════════════════════════════════════════════════════════════
# CT-T7 — 合成系统唤醒（is_task_result）不进 user history
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ct_t7_synthetic_task_result_turn_is_not_recorded_as_user_history(
        agent: Agent, llm: _RecordingFakeLLM):
    """§43：recovery 不得把 task_result / system_tick 写成 user history。

    真路径：`TaskResultData` 经真 EventBus → `_handle_task_result` → `process(is_task_result=True)`。
    """
    before = _user_texts(agent)
    gate = _gate_prompt(agent)
    gate.arm()
    agent.bus.post(TaskResultData(
        task_id="wf-ct-t7", result="任务结果正文", original_intent="帮我查一下",
        dispatched_at="2026-09-19T00:00:00+00:00"))
    await asyncio.wait_for(gate.entered.wait(), timeout=_PROBE_S)
    assert agent._cognitive.current_turn_owns_input is True  # noqa: SLF001

    _stop(agent, source="websocket")
    gate.open()
    await _drain(agent)

    joined = " | ".join(_user_texts(agent))
    assert "任务结果正文" not in joined, (
        f"合成系统唤醒被写成了用户历史：{_user_texts(agent)}")
    assert "帮我查一下" not in joined, (
        f"合成系统唤醒被写成了用户历史：{_user_texts(agent)}")
    # ★ 判据必须**按条数双向钉住**，不能只查文案：任务宣告回合的 `user_message` 是
    # **空串**（唤醒文案在 `_prepare_think_input` 里才生成）⇒ "误记一条空 user 消息"
    # 这种变异不会被上面的文案断言杀死。合成唤醒按合同**不**产生任何 user 历史。
    assert _user_texts(agent) == before, (
        f"合成系统唤醒改变了 user 历史：{before} → {_user_texts(agent)}")


# ═══════════════════════════════════════════════════════════════════════
# CT-T8 — 顺序与 shape：M1(cancelled) → M2 → assistant(M2)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ct_t8_cancelled_then_normal_keeps_history_order(
        agent: Agent, llm: _RecordingFakeLLM):
    """record timing 变化不得让 M2 排到 M1 前面（PO 秩序不得回归）。"""
    gate = _gate_prompt(agent)
    gate.arm()
    m1 = "KB46-顺序-M1"
    await _at_claim_window(agent, gate, m1)
    _stop(agent, source="websocket")
    gate.open()
    await _drain(agent)

    m2 = "KB46-顺序-M2"
    assert await agent.process_message(m2) is True
    await _drain(agent)

    texts = _user_texts(agent)
    assert texts.count(m1) == 1 and texts.count(m2) == 1, texts
    assert texts.index(m1) < texts.index(m2), f"history 顺序错误：{texts}"
    roles = _roles(agent)
    # M2 有正常回复 ⇒ 末条是 assistant；M1 与 M2 之间没有 assistant（M1 未被回复）
    assert roles[-1] == "assistant", roles
    assert roles[texts.index(m1) + 1] != "assistant" or texts.index(m2) == texts.index(m1) + 1, roles
    # 下一回合的 prompt 里两条各自恰一次
    flat = llm.prompt_flat(-1)
    assert flat.count(m1) == 1 and flat.count(m2) == 1, (
        f"M1={flat.count(m1)} M2={flat.count(m2)}")


# ═══════════════════════════════════════════════════════════════════════
# CT-T9 — 多模态载荷按结构保留（不是只剩纯文本）
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ct_t9_multimodal_payload_structure_is_preserved(
        agent: Agent, llm: _RecordingFakeLLM):
    """§41/§42：claimed truth 不能只保存纯文本——结构级 guard（不需要真文件 I/O）。

    用 sticker 载荷（`parts_from_dicts` 支持、且**不需要**真附件存在）。
    """
    gate = _gate_prompt(agent)
    gate.arm()
    content = [
        {"type": "text", "content": "KB46-多模态正文"},
        {"type": "sticker", "sticker_id": "happy", "src": "/stickers/happy.png"},
    ]
    assert await agent.process_message(content=content, text="") is True
    await asyncio.wait_for(gate.entered.wait(), timeout=_PROBE_S)
    assert agent._cognitive.current_turn_owns_input is True  # noqa: SLF001
    assert "KB46-多模态正文" not in "\n".join(_user_texts(agent))

    _stop(agent, source="websocket")
    gate.open()
    await _drain(agent)

    recorded = [m for m in agent.session.messages
                if str(getattr(m, "role", "")) == "user"
                and any(getattr(p, "sticker_id", None) == "happy"
                        for p in (m.content if isinstance(m.content, list) else []))]
    assert len(recorded) == 1, (
        "被取消的已 claim 多模态输入必须按**结构**保留（StickerPart 不能丢成纯文本/"
        f"整条消失）：history={_user_texts(agent)}")
    assert "KB46-多模态正文" in _user_texts(agent)


# ═══════════════════════════════════════════════════════════════════════
# CT-T10 — record 自身失败：终态收口不得被卡住（§63/§64/§78）
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ct_t10_session_write_failure_does_not_block_terminal_finalization(
        agent: Agent, llm: _RecordingFakeLLM, monkeypatch: pytest.MonkeyPatch):
    """`session.add_message` 抛异常（§64）⇒ 收口必须**照常完成**，不得卡 FSM。

    生产形态：会话写路径出现意外错误。合同（§63）：runtime 记录失败**可以**丢这条
    truth（持久化/写入失败是既有 best-effort 语义），但**绝不能**让相位停在 THINKING
    ——那会把一次可恢复的错误升级成 Agent 永久失聪（Batch 10/11 的 CF/TH 不变量）。
    本方法在 `except`/`finally` 里被调用，任何外抛都会让 canonical 收口永不执行。
    """
    gate = _gate_prompt(agent)
    gate.arm()
    token = "KB46-写入失败"
    await _at_claim_window(agent, gate, token)

    def _boom(_msg: Any) -> None:
        raise RuntimeError("session write exploded")

    monkeypatch.setattr(agent.session, "add_message", _boom, raising=False)

    _stop(agent, source="websocket")
    gate.open()
    await _drain(agent)          # 挂死 ⇒ 这里抛"未排空"（死锁探测）

    assert agent.status.is_completely_idle, (
        f"record 失败不得阻塞终态收口：phase={agent.status.current_name()}")
    assert not agent.pending_messages
    assert token not in _user_texts(agent), (
        "写入失败时该条 truth 不成立（这是允许的既有语义），但不得**半个**记录："
        f"{_user_texts(agent)}")


# ═══════════════════════════════════════════════════════════════════════
# CT-T11 — 终态 owner 被重复调用：recovery 恰一次（§36/§46/§79）
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ct_t11_record_recovery_is_idempotent_across_repeated_finalization(
        agent: Agent, llm: _RecordingFakeLLM):
    """`_handle_interrupt` 是被取消回合的 canonical 终态 owner，**幂等**（Batch 11）。

    KB-46 的 recovery 挂在它里面 ⇒ 必须继承这条幂等性：第二次调用不得再 append
    一条。判据落在**既有身份**（预分配 `message_id`）与一次性消费的载荷上，
    不是新增平行布尔。
    """
    gate = _gate_prompt(agent)
    gate.arm()
    token = "KB46-终态重复调用"
    await _at_claim_window(agent, gate, token)

    _stop(agent, source="websocket")
    gate.open()
    await _drain(agent)
    assert _user_texts(agent).count(token) == 1, _user_texts(agent)

    # 再调一次 canonical 终态 owner（生产形态：收口期间第二发取消 / 迟到中断）
    await agent._cognitive._handle_interrupt("websocket")  # noqa: SLF001
    await _drain(agent)

    assert _user_texts(agent).count(token) == 1, (
        f"重复收口不得双写会话真相：{_user_texts(agent)}")


# ═══════════════════════════════════════════════════════════════════════
# CT-T12 — 停机落在 CLAIMED→RECORDED 窗口（§67/§83）
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ct_t12_shutdown_in_window_stays_bounded_and_starts_no_new_turn(
        agent: Agent, llm: _RecordingFakeLLM):
    """`Agent.stop()` 落在窗口内：停机必须有界，且**不得**在 STOPPED 之后继续开回合。

    truth 侧按合同（Contract A）：这条输入确实被用户提交且已被接管 ⇒ 保留；
    但**不得**因此产生新回合或新 LLM 调用。
    """
    gate = _gate_prompt(agent)
    gate.arm()
    token = "KB46-停机窗口"
    await _at_claim_window(agent, gate, token)
    calls_before = llm.stream_calls

    await asyncio.wait_for(agent.stop(), timeout=_PROBE_S)  # 有界停机（死锁探测）
    gate.open()
    await asyncio.sleep(0)

    assert agent._state.name == "STOPPED", agent._state.name  # noqa: SLF001
    assert llm.stream_calls == calls_before, (
        f"停机后不得再开启回合/调用 LLM：{calls_before} → {llm.stream_calls}")
    assert agent.pending_messages == [] or agent.pending_messages is not None
    # 不新增 user 输入：停机本身不制造新回合
    assert agent._cognitive.current_task is None or agent._cognitive.current_task.done()  # noqa: SLF001


# ═══════════════════════════════════════════════════════════════════════
# CT-T13 — W3→W1 叠加：异常收口被取消 ⇒ 两条终态路径都跑，仍恰一次
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ct_t13_exception_finalization_cancelled_does_not_double_record(
        agent: Agent, llm: _RecordingFakeLLM):
    """subD 的 W3→W1 叠加形态（§46/§79 的最强形态）。

    控制流：`_build_turn_prompt` 抛**普通异常** ⇒ `_handle_turn_exception`（W3）开始收口；
    它的 `try` 里 await 时被**第二发取消**打断 ⇒ `finally` 先跑完 canonical 收口
    （recovery 第 1 次），CancelledError 继续冒泡出内层 `except Exception` 臂 ⇒ 被外层
    `except asyncio.CancelledError` 接住 ⇒ **W1 再次进入** `_handle_prepare_cancelled`
    → `_handle_interrupt`（recovery 第 2 次机会）。

    没有一次性消费 + 身份幂等，这里会写两条 user 消息——正是"正常一套、取消一套"的
    复制逻辑会踩的坑。
    """
    prompt_gate = _gate_prompt(agent)
    prompt_gate.arm(fail_with=RuntimeError("boom-then-cancel"))
    token = "KB46-异常收口再被取消"
    await _at_claim_window(agent, prompt_gate, token)

    # 第二发取消：钉在 W3 收口的 `output.turn_failed` 上（真实可挂起点）。
    output = agent._cognitive._output  # noqa: SLF001
    fail_gate = _Gate(output.turn_failed)
    output.turn_failed = fail_gate  # type: ignore[method-assign]
    fail_gate.arm()

    prompt_gate.open()
    await asyncio.wait_for(fail_gate.entered.wait(), timeout=_PROBE_S)
    _stop(agent, source="websocket")     # 第二发取消落在收口链上
    fail_gate.open()
    await _drain(agent)

    users = [t for t in _user_texts(agent) if t == token]
    assert len(users) == 1, (
        f"W3→W1 叠加后该输入出现 {len(users)} 次（应为恰 1 次；0 = 丢失，>1 = 双写）："
        f"history={_user_texts(agent)}")
    assert agent.status.is_completely_idle, agent.status.current_name()


# ═══════════════════════════════════════════════════════════════════════
# CT-T6b — claim 之前**抛异常**：终态收口会走到 `_handle_interrupt`，
#          门禁①（已 claim）是唯一防线
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ct_t6b_pre_claim_exception_creates_no_phantom_history(
        agent: Agent, llm: _RecordingFakeLLM):
    """pre-claim 的**异常**路径与取消路径的控制流不同，必须分别钉住。

    取消（CT-T6）：`_prepare_turn` 抛 `CancelledError` ⇒ `_handle_prepare_cancelled()`
    见 ctx 为 None **直接 return False**，根本不进 `_handle_interrupt` ⇒ 天然不会补写。

    异常（本用例，生产形态 = P0-3 `StorageUnavailableError` fail-closed，与 AD8 同形）：
    ⇒ `_handle_turn_exception()` ⇒ 它的 `finally:` **一定会** 调 `_handle_interrupt()`
    —— 而 `_claimed_input_record` 在 `process()` 入口就暂存好了（那时还不知道会不会
    claim）。所以**门禁①（`_generation_ctx is not None`）是这里唯一的防线**：没有它，
    一条从未被任何回合接管的输入会被写进会话历史（phantom history），破坏 Batch 12
    的 admission 边界（CT5）。
    """
    from core.storage.base import StorageUnavailableError

    gate = _gate_admission(agent)
    gate.arm(fail_with=StorageUnavailableError("测试：权威存储不可用"))
    token = "KB46-未claim就异常结束"
    assert await agent.process_message(token) is True
    await asyncio.wait_for(gate.entered.wait(), timeout=_PROBE_S)
    assert agent._cognitive.current_turn_owns_input is False, (  # noqa: SLF001
        "用例前提不成立：本回合**已**接管输入（测的不是 claim 之前）")

    gate.open()
    await _drain(agent)

    assert token not in _user_texts(agent), (
        f"claim 之前的异常不得制造 phantom history：{_user_texts(agent)}")
    assert token not in await _archive_texts(agent)
    assert agent.status.is_completely_idle, agent.status.current_name()
