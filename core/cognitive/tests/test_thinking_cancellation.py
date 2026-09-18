# 测试文件：core/cognitive/tests/test_thinking_cancellation.py
"""Batch 10（KB-22 / Thinking State Cancellation Recovery）：回合级取消边界的不变量。

`core/agent/tests/test_thinking_cancel_recovery.py` 走的是**整只 Agent** 的 E2 路径
（真 `process_message` → 真 EventBus → 真 `process()`）。本模块补的是循环层的三件事：

1. **Suspension Map 的参数化覆盖**（§22/§63）：`_prepare_turn` 里 4 个真挂起点
   （`session_resolver` / `registry.get_or_load` / `phases.enter_thinking` /
   `generation_registry.register`）逐点注入取消，给出分类：
   `SAFE`（取消落在 THINKING 入口之前——FSM 未被改动）/ `RECOVERS`（入口之后——
   必须经 canonical 中断路径收口）/ `NOT_CANCELLABLE`（`enter_thinking` 自身在生产
   接线里没有挂起点：唯一 `on_change` 注册者是 `wait_until_idle`，其回调无 await）。
2. **ownership 判据**（§58/§59）：cleanup 的依据必须是"**本回合**是否进过 THINKING"
   （`_generation_ctx`），而不是 `fsm.state == THINKING`——后者分不清"我的 THINKING"
   与"别人的 THINKING"，也分不清"本回合还没进 THINKING、相位上留着上一回合的
   `IDLE_BACKGROUND_TASK`"。
3. **stale cleanup / new turn 竞态**（§23/§67）：A 的 cleanup 与 B 的启动之间由
   `_process_lock` 串行化——B 不可能在 A 的收口走完之前进入 THINKING，因此
   "旧回合的迟到 cleanup 覆盖新回合状态"在结构上不可达；本模块用屏障把 A 卡在
   收口中间来**证明**这个序，而不是假设它。
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from Module.output import OutputManager
from core.adapter import ToolAdapter
from core.adapter.llm_adapter import LLMAdapter
from core.cognitive import CognitiveLoop
from core.cognitive.loop import DEFAULT_INSTANCE_ID
from core.cognitive.tests.conftest import make_default_registry
from core.config import CharacterConfig
from core.memory.consolidator import MemoryConsolidator
from core.memory.long_term import LongTermMemory
from core.memory.session import SessionMemory
from core.monitoring.event_bus import EventBus
from core.personality import CharacterEngine
from core.prompt.prompt_assembler import PromptAssembler
from core.schema.agent import AgentState
from core.schema.character import CharacterState
from core.schema.types import SessionId
from core.status.agent_fsm import AgentFSM
from core.status.fsm import FSMBackedPhaseController, FSMBackedStatus

pytestmark = pytest.mark.service

#: 死锁探测器（不是判据本身，§46）：正常路径实测 < 100ms。
_PROBE_S = 10.0
_REPLY = "好呀，我在听。<mood>平静</mood>"


class RecordingFSM(AgentFSM):
    """记录每次 transition 尝试（含被拒），供"确实进入过 THINKING"断言（§38/§77）。"""

    def __init__(self) -> None:
        super().__init__()
        self.history: list[tuple[AgentState, AgentState, str]] = []

    def transition(self, to_state: AgentState) -> None:
        src = self.state
        try:
            super().transition(to_state)
        except ValueError:
            self.history.append((src, to_state, "rejected"))
            raise
        self.history.append((src, to_state, "ok"))

    def entered(self, state: AgentState) -> int:
        """沿合法边进入 `state` 的次数。"""
        return sum(1 for _src, dst, kind in self.history if dst is state and kind == "ok")

    def states(self) -> list[str]:
        return [dst.name for _src, dst, kind in self.history if kind == "ok"]


def _one_chunk_stream(text: str = _REPLY):
    async def mock_stream(*_args, **_kwargs):
        for ch in text:
            yield {"content": ch, "tool_calls": None}

    return mock_stream


class Gate:
    """按需阻塞的依赖屏障（§44）：`arm()` 只挡下一次，`open()` 放行。"""

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
        """被武装时阻塞一次（其余调用直接透传）。"""
        if self._armed:
            self._armed = False
            self.started.set()
            await self.release.wait()

    async def wait_started(self) -> None:
        try:
            await asyncio.wait_for(self.started.wait(), timeout=_PROBE_S)
        except asyncio.TimeoutError:  # pragma: no cover — 只在真回归时触发
            raise AssertionError("屏障未被命中：目标挂起点没有走到（用例前提不成立）") from None


class _GateRegistry:
    """`GenerationRegistry` 替身：`register`（回合建档）与 `mark`（终态）各可单独卡住。"""

    def __init__(self) -> None:
        self.register_gate = Gate()
        self.mark_gate = Gate()
        self.registered: list[str] = []
        self.marked: list[tuple[str, str]] = []

    async def register(self, generation_id, **_kwargs):
        self.registered.append(str(generation_id))
        await self.register_gate.pass_through()

    async def mark(self, generation_id, *, status: str, **_kwargs):
        self.marked.append((str(generation_id), status))
        await self.mark_gate.pass_through()


class ThinkingHarness:
    """一条真实循环 + 真实的 session resolver / registry / phase controller 接线。"""

    def __init__(self, *, resolver=None, generation_registry=None) -> None:
        self.fsm = RecordingFSM()
        self.bus = EventBus()
        self.output = OutputManager()
        self.session = SessionMemory()
        self.generation_registry = generation_registry
        self.registry = make_default_registry()
        char = CharacterConfig(name="TestBot")
        lt = LongTermMemory()
        self.prompt = PromptAssembler(char, self.session, lt)
        llm = MagicMock(spec=LLMAdapter)
        llm.model = "deepseek/deepseek-v4-flash"
        state = CharacterState(session_id=SessionId("test-sid"))
        self.output.expression = MagicMock(return_value=MagicMock())
        engine = CharacterEngine(
            llm=llm, state=state, character_name="TestBot", user_name="妖梦",
            base_personality="", character_config=char, output=self.output,
        )
        self.loop = CognitiveLoop(
            llm=llm, fsm=self.fsm, event_bus=self.bus, output=self.output,
            prompt=self.prompt,
            session_resolver=resolver or self._resolver,
            tool_adapter=ToolAdapter([]),
            character_engine=engine,
            consolidator=MemoryConsolidator(llm, lt, "TestBot"),
            tts=None,
            generation_registry=generation_registry,
            runtime_registry=self.registry,
        )
        status = FSMBackedStatus(self.fsm)
        self.loop.status = status
        self.loop.phases = FSMBackedPhaseController(
            fsm=self.fsm, bus=self.bus, output=self.output,
            tts_mgr=self.loop.tts_manager, status=status,
        )
        self.loop._session = self.session  # noqa: SLF001 — 与既有 harness 同形

    async def _resolver(self, _conversation_id, _protocol_version) -> SessionMemory:
        return self.session

    def diagnose(self) -> str:
        return (f"fsm={self.fsm.state} lock={self.loop.process_lock.locked()} "
                f"states={self.fsm.states()} ctx={self.loop._generation_ctx is not None}")  # noqa: SLF001

    async def start_turn(self, text: str = "你好") -> asyncio.Task:
        """在独立任务里跑一轮真实 `process()`，并像 Agent 一样登记任务句柄。"""
        self.loop._llm.stream_think = _one_chunk_stream()  # noqa: SLF001
        task = asyncio.create_task(self.loop.process(text))
        self.loop.current_task = task
        return task

    async def settle(self, task: asyncio.Task) -> None:
        """等回合任务落定（结构化终态信号；超时只是死锁探测）。"""
        try:
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True),
                                   timeout=_PROBE_S)
        except asyncio.TimeoutError:  # pragma: no cover
            pytest.fail(f"回合任务未在 {_PROBE_S}s 内落定：" + self.diagnose())

    async def aclose(self) -> None:
        for gate in (getattr(self, "generation_registry", None),):
            if gate is not None:
                gate.register_gate.open()
                gate.mark_gate.open()
        await self.output.close()


@pytest_asyncio.fixture
async def harness_factory():
    made: list[ThinkingHarness] = []

    def _make(*, resolver=None, generation_registry=None) -> ThinkingHarness:
        h = ThinkingHarness(resolver=resolver, generation_registry=generation_registry)
        made.append(h)
        return h

    try:
        yield _make
    finally:
        for h in made:
            await h.aclose()


# ── Suspension Map：逐挂起点注入取消（§22/§63） ──────────────────────────────


@pytest.mark.asyncio
async def test_cancel_before_thinking_entry_is_safe(harness_factory):
    """挂起点 ①（`session_resolver`，loop.py:494）：取消落在 THINKING 入口之前。

    分类 SAFE：本回合还没建立任何对外状态，FSM **不得**被这次取消改动
    （既不进入 THINKING，也不把别人的相位推成 IDLE）。
    """
    gate = Gate()
    h = harness_factory(resolver=lambda *_: _blocked_resolve(gate))
    task = await h.start_turn()
    await gate.wait_started()

    assert h.fsm.state is AgentState.IDLE, h.diagnose()
    assert h.loop.cancel_current(source="button") is True
    gate.open()
    await h.settle(task)

    assert h.fsm.entered(AgentState.THINKING) == 0, (
        "取消落在 THINKING 入口之前，本回合不该进入 THINKING：" + h.diagnose())
    assert h.fsm.state is AgentState.IDLE, h.diagnose()
    assert h.loop.process_lock.locked() is False, h.diagnose()
    assert task.cancelled(), (
        "入口之前的取消必须**保持取消语义**（本回合没有状态可收口，不得吞掉）："
        + h.diagnose())


@pytest.mark.asyncio
async def test_cancel_after_thinking_entry_recovers_via_canonical_interrupt(harness_factory):
    """挂起点 ②（`generation_registry.register`，loop.py:610）：入口之后唯一的挂起点。

    分类 RECOVERS：取消必须经 canonical 中断路径收口（THINKING → INTERRUPTED →
    IDLE），且 `generation.mark(cancelled)` 必须落位（回合终态不得留在 running）。
    """
    registry = _GateRegistry()
    h = harness_factory(generation_registry=registry)
    registry.register_gate.arm()
    task = await h.start_turn()
    await registry.register_gate.wait_started()

    assert h.fsm.state is AgentState.THINKING, h.diagnose()
    # 回合级计时器在本回合进行中是活的（真相），收口必须释放它（不然诊断面会
    # 继续报一个已经死掉的回合的 clock）。
    assert h.loop._clock is not None, h.diagnose()  # noqa: SLF001
    assert h.loop.cancel_current(source="button") is True
    registry.register_gate.open()
    await h.settle(task)

    assert h.fsm.state is AgentState.IDLE, (
        "取消后 FSM 必须回到 IDLE（不是停在 THINKING）：" + h.diagnose())
    assert h.fsm.states() == ["THINKING", "INTERRUPTED", "IDLE"], (
        "收口必须走 canonical 边（THINKING→INTERRUPTED→IDLE）：" + h.diagnose())
    assert registry.marked and registry.marked[-1][1] in ("cancelled", "interrupted"), (
        f"取消后的回合终态没有落位：{registry.marked}")
    assert h.loop.process_lock.locked() is False, h.diagnose()
    assert task.cancelled() is False, (
        "入口之后的取消由 canonical owner 消费（与 `_think_loop` 窗口一致）："
        + h.diagnose())
    assert h.loop._clock is None, (  # noqa: SLF001
        "收口必须释放本回合的 TimingClock 引用：" + h.diagnose())


@pytest.mark.asyncio
async def test_enter_thinking_has_no_suspension_point_in_production_wiring(harness_factory):
    """挂起点 ③（`phases.enter_thinking`）：**生产接线现状** = 无挂起点（NOT_CANCELLABLE）。

    本用例记录的只是一个**接线事实**：`fsm.transition` 与 `bus.post` 同步，唯一的
    await（`AgentStatus._notify`）在仓内唯一注册者（`wait_until_idle`）那里只做
    `event.set()`（无 await），所以 `enter_thinking()` 一个事件循环步都不让出
    （用 `call_soon` 的标记证明）。

    **但它不是 ownership 判据的证明**——`on_change` 是公开订阅点，签名就是
    `Callable[[str], Awaitable[None]]`：只要有人注册一个会挂起的订阅者，
    "FSM 已 THINKING / ctx 还没建"的缝隙就出现了（Challenger A3b）。
    判据本身对这件事免疫，由下一条用例
    `test_cancel_inside_enter_thinking_notify_still_recovers` 证明。
    """
    h = harness_factory()
    notified: list[str] = []
    yielded: list[str] = []
    h.loop.status.on_change(lambda _name: _mark(notified))

    asyncio.get_running_loop().call_soon(yielded.append, "loop-step")
    await h.loop.phases.enter_thinking()
    assert notified == ["notified"], "监听者没有被通知（用例前提不成立）"
    assert yielded == [], (
        "enter_thinking 让出了事件循环——ownership 判据需要重新设计：" + h.diagnose())
    assert h.fsm.state is AgentState.THINKING, h.diagnose()


async def _mark(marker: list[str]) -> None:
    """`wait_until_idle` 形状的监听者：同步 set，无 await。"""
    marker.append("notified")


async def _blocked_resolve(gate: Gate) -> SessionMemory:
    gate.started.set()
    await gate.release.wait()
    return SessionMemory()


# ── ownership 判据（§58/§59：state == THINKING 不是 ownership proof） ────────


@pytest.mark.asyncio
async def test_cleanup_scoped_by_ownership_not_by_fsm_value(harness_factory):
    """"别人的 THINKING" + 本回合没有进入过 THINKING ⇒ cleanup 必须无操作。

    这是 §59 的可执行判据：`state == THINKING` 不能作为 ownership proof。
    这里让相位**真的**处于 THINKING（模拟新回合 B 已经进入），而本回合的
    `_generation_ctx` 为 None（= 本回合没进过 THINKING），再调用收口——FSM 必须
    原样不动。一个写成 `if state == THINKING: set_idle()` 的实现会在这里红。
    """
    h = harness_factory()
    await h.loop.phases.enter_thinking()          # 造出"别人的 THINKING"
    h.loop._generation_ctx = None                 # noqa: SLF001 — 本回合未进入过 THINKING
    assert h.fsm.state is AgentState.THINKING, h.diagnose()

    await h.loop._handle_prepare_cancelled()      # noqa: SLF001 — 被测的收口入口

    assert h.fsm.state is AgentState.THINKING, (
        "cleanup 用 FSM 取值当 ownership proof，误伤了别的回合：" + h.diagnose())


@pytest.mark.asyncio
async def test_pre_thinking_cancel_preserves_other_turn_background_phase(harness_factory):
    """取消落在入口之前时，**不得**把相位上遗留的 `IDLE_BACKGROUND_TASK` 推成 IDLE。

    生产可达形态：上一个回合留下了后台任务提示（`IDLE_BACKGROUND_TASK`），本回合
    刚被接受、还没走到 `enter_thinking` 就被取消（挂起点 ①）。此时"收口"只能是
    no-op——收口别人的相位就是 stale cleanup 覆盖。
    """
    gate = Gate()
    h = harness_factory(resolver=lambda *_: _blocked_resolve(gate))
    # 合法路径构造 IDLE_BACKGROUND_TASK：IDLE→THINKING→SPEAKING→IDLE_BACKGROUND_TASK
    await h.loop.phases.enter_thinking()
    await h.loop.phases.enter_speaking()
    await h.loop.phases.finish_turn(has_background_tasks=True)
    assert h.fsm.state is AgentState.IDLE_BACKGROUND_TASK, h.diagnose()

    task = await h.start_turn()
    await gate.wait_started()
    assert h.loop.cancel_current(source="button") is True
    gate.open()
    await h.settle(task)

    assert h.fsm.state is AgentState.IDLE_BACKGROUND_TASK, (
        "取消落在 THINKING 入口之前，却改动了相位的既有状态：" + h.diagnose())
    assert h.fsm.entered(AgentState.THINKING) == 1, h.diagnose()  # 只有前置构造那一次


# ── stale cleanup / new turn 竞态（§23/§67/§94） ─────────────────────────────


@pytest.mark.asyncio
async def test_old_turn_cleanup_cannot_overwrite_newer_turn(harness_factory):
    """A 在收口中间被屏障卡住时，B **不可能**已经进入 THINKING（`_process_lock` 串行化）。

    这条把 TSR-3 的成立条件变成可执行断言：不是"A 的 cleanup 恰好没覆盖 B"，
    而是"A 的 cleanup 走完之前 B 根本进不来"——旧回合的迟到 cleanup 与新回合的
    状态之间没有交错点。
    """
    registry = _GateRegistry()
    h = harness_factory(generation_registry=registry)

    # A：进入 THINKING 后取消，收口停在 generation mark 上（把 cleanup 窗口撑开）
    registry.register_gate.arm()
    turn_a = await h.start_turn("A")
    await registry.register_gate.wait_started()
    registry.mark_gate.arm()
    assert h.loop.cancel_current(source="button") is True
    await registry.mark_gate.wait_started()
    assert h.fsm.state is AgentState.THINKING, (
        "收口尚未走完（mark 被卡住）时，A 仍持有 THINKING：" + h.diagnose())
    assert turn_a.done() is False, h.diagnose()

    # B：此刻启动新回合 —— 必须被 `_process_lock` 挡住，不得进入 THINKING
    turn_b = await h.start_turn("B")
    await asyncio.sleep(0)  # 让 B 跑到锁上（不是 sleep-based race，只让出一次事件循环）
    assert turn_b.done() is False, "B 不应在 A 收口完成前跑完：" + h.diagnose()
    assert h.fsm.entered(AgentState.THINKING) == 1, (
        "B 在 A 的 cleanup 走完之前就进入了 THINKING —— 旧 cleanup 会覆盖新回合："
        + h.diagnose())

    # 放行 A 的 cleanup → A 结束 → B 才能进入 THINKING 并跑完自己的一轮
    registry.mark_gate.open()
    await h.settle(turn_a)
    await h.settle(turn_b)

    assert h.fsm.state is AgentState.IDLE, h.diagnose()
    assert h.fsm.entered(AgentState.THINKING) == 2, h.diagnose()
    # A 的收口（INTERRUPTED→IDLE）必须整体早于 B 的 THINKING：中间没有交错
    assert h.fsm.states() == ["THINKING", "INTERRUPTED", "IDLE", "THINKING", "SPEAKING", "IDLE"], (
        "旧回合 cleanup 与新回合状态发生了交错：" + h.diagnose())


@pytest.mark.asyncio
async def test_double_cancel_is_idempotent(harness_factory):
    """同一回合连续两次取消：收口恰好一次，第二次无害（§48/§71）。

    两发 `cancel()` 在同一挂起点前送达时，asyncio 只投递一次 `CancelledError`；
    收口完成后 `cancel_current()` 对已结束的任务返回 False，不再产生任何状态变化。
    """
    registry = _GateRegistry()
    h = harness_factory(generation_registry=registry)
    registry.register_gate.arm()
    turn = await h.start_turn()
    await registry.register_gate.wait_started()

    assert h.loop.cancel_current(source="button") is True
    assert h.loop.cancel_current(source="button") is False, (
        "第二发取消不应再对同一任务生效（句柄已在第一发里让出）")
    registry.register_gate.open()
    await h.settle(turn)

    assert h.fsm.state is AgentState.IDLE, h.diagnose()
    assert h.fsm.states() == ["THINKING", "INTERRUPTED", "IDLE"], (
        "重复取消造成了重复/非法转换：" + h.diagnose())
    assert h.loop.cancel_current(source="button") is False, "已结束的回合不该再被取消"
    assert h.fsm.state is AgentState.IDLE, h.diagnose()


@pytest.mark.asyncio
async def test_cancel_during_error_recovery_is_also_covered(harness_factory):
    """同源第二处（KB-22 的 E1 半）：取消落在**异常收口自身**的 await 上也必须收口。

    真实现里 `CharacterBusyError` 在 `enter_thinking()`（:591）**之后**才抛
    （:627-629）⇒ `_handle_turn_exception` 的 awaits（output 广播 / registry mark）
    全部发生在相位已是 THINKING 之后。取消边界若只包住 `_prepare_turn`、不包住这个
    handler，在 handler 里落下的取消照样逃出 `process()`，FSM 一样停在 THINKING。
    本用例把 `try_begin_generation()` 打成 False 触发真实抛点，再把 output 广播卡住，
    让取消精确落在 handler 内部。
    """
    gate = Gate()
    h = harness_factory()
    runtime = h.registry.get(DEFAULT_INSTANCE_ID, None)
    assert runtime is not None, "harness 未预置 default runtime"
    runtime.try_begin_generation = lambda: False  # noqa: SLF001 — 触发真实抛点（:627-629）

    async def _blocked_turn_failed(*_args, **_kwargs):
        gate.started.set()
        await gate.release.wait()

    h.output.turn_failed = _blocked_turn_failed  # type: ignore[method-assign]

    task = await h.start_turn()
    await gate.wait_started()
    assert h.fsm.state is AgentState.THINKING, (
        "CharacterBusyError 必须在 THINKING 入口之后抛出（用例前提）：" + h.diagnose())

    assert h.loop.cancel_current(source="button") is True
    gate.open()
    await h.settle(task)

    assert h.fsm.state is AgentState.IDLE, (
        "取消落在异常收口内部时 FSM 被留在 THINKING：" + h.diagnose())
    assert h.fsm.states() == ["THINKING", "INTERRUPTED", "IDLE"], h.diagnose()


@pytest.mark.asyncio
async def test_cancel_inside_enter_thinking_notify_still_recovers(harness_factory):
    """Challenger A3b（PRE_FIX_RED）：取消落在 `enter_thinking` 的 `_notify` 内部也必须收口。

    `AgentStatus.on_change` 是**公开**订阅点（签名 `Callable[[str], Awaitable[None]]`）。
    只要某个订阅者在 `thinking` 通知上挂起，取消就会落在"FSM 已经 transition 到
    THINKING、而旧实现里 `_generation_ctx` 还没建立"的缝隙上——旧判据会误判成
    "本回合没进过 THINKING"并放任取消逃逸（永久失聪，一字不差）。

    修法把 `_generation_ctx` 的建立挪到 `enter_thinking()` **之前**（两者之间没有
    await）⇒ 判据与"FSM 已经/即将是 THINKING"严格等价，对订阅者行为免疫。
    """
    h = harness_factory()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _suspending_listener(name: str) -> None:
        if name == "thinking":
            entered.set()
            await release.wait()

    h.loop.status.on_change(_suspending_listener)

    task = await h.start_turn()
    await asyncio.wait_for(entered.wait(), timeout=_PROBE_S)
    assert h.fsm.state is AgentState.THINKING, (
        "用例前提不成立：取消必须落在 enter_thinking 的 _notify 上：" + h.diagnose())

    assert h.loop.cancel_current(source="button") is True
    release.set()
    await h.settle(task)

    assert h.fsm.state is AgentState.IDLE, (
        "取消落在 enter_thinking 的通知 await 上时相位被永久留在 THINKING："
        + h.diagnose())
    assert h.fsm.states() == ["THINKING", "INTERRUPTED", "IDLE"], h.diagnose()
    assert task.cancelled() is False, (
        "收口已由 canonical owner 消费这次取消：" + h.diagnose())


@pytest.mark.asyncio
async def test_generic_exception_after_turn_started_still_recovers(harness_factory):
    """Challenger I3b（PRE_FIX_RED）：回合开始之后的**普通异常**同样不能留下无主相位。

    窗口里除了取消，还可能抛出普通异常（领域 op-guard 异常、审计字段校验失败……）。
    它们此前**没有任何 owner**：`process()` 只补了取消边界，异常一路逃出 ⇒ 相位同样
    永久停在 THINKING（病症与 KB-22 一字不差）。异常边界与取消边界同根、共用
    "本回合是否已开始"的判据。
    """
    h = harness_factory()
    runtime = h.registry.get(DEFAULT_INSTANCE_ID, None)
    assert runtime is not None, "harness 未预置 default runtime"

    def _boom() -> bool:
        raise RuntimeError("op guard broken")

    runtime.try_begin_generation = _boom  # noqa: SLF001

    task = await h.start_turn()
    await h.settle(task)

    assert h.fsm.state is AgentState.IDLE, (
        "回合开始之后抛出的普通异常把相位永久留在 THINKING：" + h.diagnose())
    assert h.fsm.states() == ["THINKING", "INTERRUPTED", "IDLE"], h.diagnose()
    assert task.cancelled() is False, h.diagnose()
