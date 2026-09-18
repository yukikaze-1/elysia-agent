# 测试文件：core/cognitive/tests/test_cancellation_safe_finalization.py
"""Batch 11（KB-25 / Cancellation-Safe Turn Finalization）：收口链的取消安全性。

## 缺陷形状（KB-25，canonical scope）

一个回合的**终态收口**是一条长 await 链：

```
process() 捕获取消/异常
  → _handle_interrupt() / _handle_turn_exception()
      → output.turn_cancelled(...)        ← await（输出帧，~5s 上界）
      → generation_registry.mark(...)     ← await（Mongo 写入，**无上界**）
      → _cancel_desktop_calls()           ← await（桥 RPC，**无上界**）
      → output.typing(False)              ← await
      → FSMBackedPhaseController.handle_interrupt()
          → fsm.transition(INTERRUPTED) + bus.post   ← 同步
          → _notify("interrupted")                    ← await
          → asyncio.sleep(0.05)                       ← await（UI 可观测延迟）
          → fsm.transition(IDLE) + bus.post(IDLE)     ← ★ 逻辑终态 ★ 同步
          → _notify("idle")                           ← await
```

`asyncio.CancelledError` 是 `BaseException`（实测 `__mro__ = (CancelledError,
BaseException, object)`），既不被各处的 `except Exception` 接住，也不被
`process()` 里兄弟 `except` 子句接住。于是**第二发** `task.cancel()`（生产形态 =
`Agent.stop()` 第 3 步 `_turns.drain(timeout=3.0)` 预算耗尽后的直接取消，
`Module/_common/task_scope.py:277`）只要落在上面**任何**一个 await 上，CancelledError
就会一路穿过收口链逃出 `process()`：

- `_process_lock` 由 `async with` 退出时**释放**；
- 回合任务以 cancelled 结束；
- 相位**留在 THINKING 或 INTERRUPTED**（活跃区）——没有任何 owner 再来推它。

## 本批要证明的东西（§1，不是"最终还能回 IDLE"）

> 一旦某个回合已经进入终态收口，后续重复取消不得阻止它达到一个**稳定、逻辑一致**
> 的 terminal lifecycle state；同时取消意图仍然要传播、停机仍然要有界。

判据（每条用例都断言**完整状态序列**，不是 `final != THINKING`）：

- FSM 必须经 canonical 边落到 IDLE（`["THINKING","INTERRUPTED","IDLE"]`，不重不漏）；
- 锁已释放、无存活 owner（锁/任务/FSM 三者一致，CF2）；
- 回合任务仍然以 `cancelled()` 结束（§9/CF3：第二发取消**不是无效**的）；
- 同一个 terminal event 不重复广播（§50）。

## 不用 sleep 造 race（§46）

每条用例都用屏障证明三件事：①第一发取消真的已被处理（`cancelling()` 递增）；
②收口**确实已经开始**（屏障被命中）；③第二发取消落在目标 await 上。
回合被钉在 LLM 流的 await 上（FSM 确定是 THINKING）才开始取消，
因此状态序列不含 SPEAKING 抖动。
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
from core.schema.event import StateChangeData
from core.schema.types import SessionId
from core.status.agent_fsm import AgentFSM
from core.status.fsm import FSMBackedPhaseController, FSMBackedStatus

pytestmark = pytest.mark.service

#: 死锁探测器（不是判据本身，§79）：任何正常路径实测 < 200ms。
_PROBE_S = 10.0
_REPLY = "好呀，我在听。<mood>平静</mood>"

#: 活跃区相位：仍宣称"有回合在跑"的相位集合。稳定终态必须离开它。
_ACTIVE_PHASES = {
    AgentState.THINKING,
    AgentState.EXECUTING,
    AgentState.SPEAKING,
    AgentState.INTERRUPTED,
}


async def _wait_true(predicate) -> None:
    """等到谓词为真（只让出事件循环步，不用固定 sleep 造 race）。"""
    while not predicate():
        await asyncio.sleep(0)


class RecordingFSM(AgentFSM):
    """记录每次 transition（含被拒），供"确实进入过 THINKING"与序列断言（§47/§77）。"""

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
        return sum(1 for _src, dst, kind in self.history if dst is state and kind == "ok")

    def states(self) -> list[str]:
        return [dst.name for _src, dst, kind in self.history if kind == "ok"]

    def rejected(self) -> list[str]:
        return [f"{src.name}->{dst.name}" for src, dst, kind in self.history
                if kind == "rejected"]


class Gate:
    """按需阻塞一次的屏障：`arm()` 只挡下一次，`open()` 放行。"""

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


class BusRecorder:
    """包住 `EventBus.post`：记录 STATE_CHANGE 的相位序列（§50 duplicate guard）。"""

    def __init__(self, bus: EventBus) -> None:
        self._inner = bus.post
        self.states: list[str] = []
        bus.post = self.post  # type: ignore[method-assign]

    def post(self, data, priority=None, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if isinstance(data, StateChangeData):
            self.states.append(str(data.state))
        if priority is None:
            return self._inner(data, **kwargs)
        return self._inner(data, priority, **kwargs)

    def count(self, state: str) -> int:
        return sum(1 for s in self.states if s == state)


class FinalizationHarness:
    """一条真实循环 + 真实的 session resolver / registry / phase controller 接线。"""

    def __init__(self, *, generation_registry=None) -> None:
        self.fsm = RecordingFSM()
        self.bus = EventBus()
        self.output = OutputManager()
        self.session = SessionMemory()
        self.generation_registry = generation_registry
        self.registry = make_default_registry()
        self.stream_gate = Gate()
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
            session_resolver=self._resolver,
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
        self.bus_recorder = BusRecorder(self.bus)
        self.notified: list[str] = []
        status.on_change(self._record_notify)

    async def _record_notify(self, name: str) -> None:
        self.notified.append(name)

    async def _resolver(self, _conversation_id, _protocol_version) -> SessionMemory:
        return self.session

    # ── 诊断 ──

    def diagnose(self) -> str:
        return (f"fsm={self.fsm.state} lock={self.loop.process_lock.locked()} "  # noqa: SLF001
                f"states={self.fsm.states()} rejected={self.fsm.rejected()} "
                f"bus={self.bus_recorder.states} notified={self.notified}")

    # ── 驱动 ──

    async def start_turn(self, text: str = "你好", *, failing: bool = False,
                         hold_stream: bool = True) -> asyncio.Task:
        """在独立任务里跑一轮真实 `process()`，并像 Agent 一样登记任务句柄。

        `hold_stream=True` 时把 LLM 流的第一个 await 钉在 `stream_gate` 上——
        回合此时**确定**处于 THINKING（首句尚未派发），使取消点与状态序列确定。
        """
        if failing:
            async def _stream(*_args, **_kwargs):
                raise RuntimeError("provider exploded")
                yield {}  # pragma: no cover — 保持异步生成器
        elif hold_stream:
            async def _stream(*_args, **_kwargs):
                await self.stream_gate.pass_through()
                yield {"content": _REPLY, "tool_calls": None}
        else:
            async def _stream(*_args, **_kwargs):
                yield {"content": _REPLY, "tool_calls": None}

        self.loop._llm.stream_think = _stream  # noqa: SLF001
        if hold_stream and not failing:
            self.stream_gate.arm()
        task = asyncio.create_task(self.loop.process(text))
        self.loop.current_task = task
        if hold_stream and not failing:
            await self.stream_gate.wait_started("LLM 流")
            assert self.fsm.state is AgentState.THINKING, (
                "用例前提不成立：回合被钉在 LLM 流上时相位应为 THINKING：" + self.diagnose())
        return task

    async def settle(self, task: asyncio.Task) -> None:
        try:
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True),
                                   timeout=_PROBE_S)
        except asyncio.TimeoutError:  # pragma: no cover
            pytest.fail(f"回合任务未在 {_PROBE_S}s 内落定：" + self.diagnose())

    async def aclose(self) -> None:
        self.stream_gate.open()
        if self.generation_registry is not None:
            self.generation_registry.register_gate.open()
            self.generation_registry.mark_gate.open()
        await self.output.close()

    # ── 断言（second-cancel 用例共用） ──

    def assert_stable_terminal(self, task: asyncio.Task, *,
                               expect_cancelled: bool = True) -> None:
        """稳定逻辑终态：相位离开活跃区 + 锁释放 + canonical 序列 + 无重复事件。"""
        assert self.fsm.state not in _ACTIVE_PHASES, (
            "收口被打断后相位留在活跃区（KB-25：生命周期失去 owner）：" + self.diagnose())
        assert self.fsm.state is AgentState.IDLE, (
            "中断收口的终态只能是 IDLE：" + self.diagnose())
        assert self.fsm.rejected() == [], (
            "收口路径上出现了被拒的 FSM 转换：" + self.diagnose())
        assert self.fsm.states() == ["THINKING", "INTERRUPTED", "IDLE"], (
            "收口必须走 canonical 边且不重不漏（THINKING→INTERRUPTED→IDLE）："
            + self.diagnose())
        assert self.notified == ["thinking", "interrupted", "idle"], (
            "语义通知序列被重复取消改动：" + self.diagnose())
        assert self.bus_recorder.count("interrupted") == 1, (
            "terminal event 被重复广播（interrupted）：" + self.diagnose())
        assert self.bus_recorder.count("idle") == 1, (
            "terminal event 被重复广播（idle）：" + self.diagnose())
        assert self.loop.process_lock.locked() is False, (  # noqa: SLF001
            "回合结束后 process lock 不得仍被持有：" + self.diagnose())
        assert task.cancelled() is expect_cancelled, (
            f"取消语义不符合预期（期望 cancelled={expect_cancelled}）："
            + self.diagnose())


@pytest_asyncio.fixture
async def harness_factory():
    made: list[FinalizationHarness] = []

    def _make(*, generation_registry=None) -> FinalizationHarness:
        h = FinalizationHarness(generation_registry=generation_registry)
        made.append(h)
        return h

    try:
        yield _make
    finally:
        for h in made:
            await h.aclose()


def _patch_output_method(h: FinalizationHarness, name: str, gate: Gate, *,
                         only_false: bool = False, block_at: int = 1):
    """把 `OutputManager.<name>` 换成"武装时阻塞一次"的版本（原实现照常执行）。

    `only_false=True` 时只挡 `flag is False` 的调用；`block_at=N` 表示挡第 N 次
    命中（用于跳过 `_think_loop` 自己的清理调用，精确落到 `_handle_interrupt` 里那次）。
    返回被调用次数的计数器（list[0]）。
    """
    inner = getattr(h.output, name)
    hits: list[int] = [0]

    if only_false:
        async def _blocked(flag, **kwargs):
            if flag is False:
                hits[0] += 1
                if hits[0] == block_at:
                    await gate.pass_through()
            return await inner(flag, **kwargs)
    else:
        async def _blocked(*args, **kwargs):
            hits[0] += 1
            await gate.pass_through()
            return await inner(*args, **kwargs)

    setattr(h.output, name, _blocked)
    return hits


async def _first_cancel_at(h: FinalizationHarness, gate: Gate, what: str) -> asyncio.Task:
    """把回合推到"第一发取消已消费、收口停在 `gate` 上"，返回回合任务。"""
    task = await h.start_turn()
    assert h.loop.cancel_current(source="button") is True, (  # noqa: SLF001
        "第一发取消未生效（回合句柄不在）：" + h.diagnose())
    await gate.wait_started(what)
    assert task.cancelling() == 1, (
        "用例前提不成立：第一发取消尚未送达：" + h.diagnose())
    return task


# ── Suspension Map：第二发取消逐 await 注入（§37 CF-T1 / §44 / §61） ─────────


@pytest.mark.asyncio
async def test_second_cancel_during_turn_cancelled_frame_settles(harness_factory):
    """C2 落在 `output.turn_cancelled`（收口链第一个 await，相位仍 THINKING）。

    PRE_FIX_RED：修复前第二发取消从这里穿出 `_handle_interrupt` ⇒ 相位永久停在
    THINKING（`states() == ["THINKING"]`，连 INTERRUPTED 都没到）。
    """
    registry = _GateRegistry()
    h = harness_factory(generation_registry=registry)
    gate = Gate()
    _patch_output_method(h, "turn_cancelled", gate)
    gate.arm()

    task = await _first_cancel_at(h, gate, "output.turn_cancelled")
    assert h.fsm.state is AgentState.THINKING, h.diagnose()

    # ★ 第二发：与 AsyncTaskScope.drain 超时后的动作逐字相同（直接 task.cancel()）
    task.cancel()
    assert task.cancelling() == 2
    gate.open()
    await h.settle(task)

    h.assert_stable_terminal(task, expect_cancelled=True)
    # persisted 终态是 **best-effort**（§17/§18：runtime truth 优先于 persisted truth）。
    # 第二发取消落在 `turn_cancelled` 上 ⇒ `generation.mark` 从未执行，那一行停在
    # `running`，由启动期 `GenerationRegistry.mark_stale_interrupted(cutoff)`
    # （`core/agent/_lifecycle_persistence.py:173/575`）兜底清扫为 interrupted。
    # 这里只断言"不得写出**错误**的终态"（例如谎报 completed）。
    assert all(status in ("cancelled", "interrupted") for _gid, status in registry.marked), (
        f"被打断的收口写出了错误的 generation 终态：{registry.marked}")


@pytest.mark.asyncio
async def test_second_cancel_during_registry_mark_settles(harness_factory):
    """C2 落在 `generation_registry.mark`（KB-25 的命名挂起点；Mongo await 无上界）。

    PRE_FIX_RED：修复前相位停在 THINKING。本用例**只**证明 runtime 终态（相位/锁/任务）；
    第二发取消恰好落在 `mark` 上时，那次 `mark` **已被取消**（`marked` 里能看到它被调用过，
    但写没写进库由真库延迟决定）——persisted 终态是 best-effort，见 §17/§18 与
    `tests/real_mongo/test_real_finalization_cancellation.py`。
    """
    registry = _GateRegistry()
    h = harness_factory(generation_registry=registry)
    registry.mark_gate.arm()

    task = await _first_cancel_at(h, registry.mark_gate, "generation_registry.mark")
    assert h.fsm.state is AgentState.THINKING, h.diagnose()
    assert registry.registered, "generation 建档未发生（用例前提不成立）"
    assert registry.marked and registry.marked[-1][1] == "cancelled", registry.marked

    task.cancel()
    registry.mark_gate.open()
    await h.settle(task)

    h.assert_stable_terminal(task, expect_cancelled=True)


@pytest.mark.asyncio
async def test_second_cancel_during_typing_cleanup_settles(harness_factory):
    """C2 落在 `output.typing(False)`（`_handle_interrupt` 里唯一未被 try 包住的 await）。

    第 1 次 `typing(False)` 来自 `_think_loop` 自己的异常清理（取消路径必经），
    第 2 次才在 `_handle_interrupt` 里——屏障钉在第 2 次上。
    """
    h = harness_factory(generation_registry=_GateRegistry())
    gate = Gate()
    hits = _patch_output_method(h, "typing", gate, only_false=True, block_at=2)
    gate.arm()

    task = await _first_cancel_at(h, gate, "output.typing(False) @ _handle_interrupt")
    assert h.fsm.state is AgentState.THINKING, h.diagnose()
    assert hits[0] >= 2, (
        f"用例前提不成立：屏障应命中 _handle_interrupt 里那次 typing(False)，实测 {hits[0]} 次")

    task.cancel()
    gate.open()
    await h.settle(task)

    h.assert_stable_terminal(task, expect_cancelled=True)


@pytest.mark.asyncio
async def test_second_cancel_during_desktop_cleanup_settles(harness_factory):
    """C2 落在 `_cancel_desktop_calls()` 的桥 RPC 上（KB-23 的命名挂起点）。

    用真实 `ToolAdapter.tool_context.services["desktop_bridge"]` 注入一个会挂起的
    桥替身：`pending_calls()` 无上界，正是 KB-23 登记的形态。

    本用例钉住的是**分类边界**（§5）：desktop 在途调用的取消属于 **BEST_EFFORT
    资源清理**，它被第二发取消放弃是**允许**的（§43：允许资源 cleanup 被放弃，
    但 logical state 必须已稳定）；**不允许**的是它把相位一起带走。因此断言
    "清理确实被尝试过" + "相位仍然落到稳定终态"，而不要求 `cancel_call` 一定送达。
    """

    h = harness_factory(generation_registry=_GateRegistry())
    gate = Gate()
    cancelled_calls: list[str] = []
    entered = asyncio.Event()

    class _BlockingBridge:
        async def pending_calls(self):
            entered.set()
            await gate.pass_through()
            return [{"call_id": "call-1", "status": "pending"}]

        async def cancel_call(self, call_id):
            cancelled_calls.append(call_id)

    assert h.loop._tool_adapter is not None  # noqa: SLF001
    h.loop._tool_adapter.tool_context.services["desktop_bridge"] = _BlockingBridge()  # noqa: SLF001
    gate.arm()

    task = await _first_cancel_at(h, gate, "bridge.pending_calls()")
    assert entered.is_set(), "desktop 清理未被尝试（用例前提不成立）"
    assert h.fsm.state is AgentState.THINKING, h.diagnose()

    task.cancel()
    gate.open()
    await h.settle(task)

    h.assert_stable_terminal(task, expect_cancelled=True)
    assert cancelled_calls == [], (
        "本用例的前提是**取消恰好落在 pending_calls 上**（cancel_call 尚未送达）："
        f"{cancelled_calls}")


@pytest.mark.asyncio
async def test_desktop_cleanup_completes_without_second_cancel(harness_factory):
    """ALREADY_GREEN_GUARD：**单发**取消时 desktop 在途调用必须被真正取消（KB-23 的合同）。

    与上一条配对：上一条证明"第二发取消允许放弃资源清理"，这一条证明"放弃"
    只发生在被取消的那一刻——正常收口路径上 `cancel_call` 必须逐条送达。
    """

    h = harness_factory(generation_registry=_GateRegistry())
    cancelled_calls: list[str] = []

    class _Bridge:
        async def pending_calls(self):
            return [
                {"call_id": "call-1", "status": "pending"},
                {"call_id": "call-2", "status": "offline"},   # D-o：offline 跳过
            ]

        async def cancel_call(self, call_id):
            cancelled_calls.append(call_id)

    assert h.loop._tool_adapter is not None  # noqa: SLF001
    h.loop._tool_adapter.tool_context.services["desktop_bridge"] = _Bridge()  # noqa: SLF001

    task = await h.start_turn()
    assert h.loop.cancel_current(source="button") is True  # noqa: SLF001
    await h.settle(task)

    h.assert_stable_terminal(task, expect_cancelled=False)
    assert cancelled_calls == ["call-1"], (
        f"单发取消下 desktop 在途调用未被取消（offline 应跳过）：{cancelled_calls}")


@pytest.mark.asyncio
async def test_second_cancel_during_interrupted_notify_settles(harness_factory):
    """C2 落在 `phases.handle_interrupt()` 的 `_notify("interrupted")`（相位已 INTERRUPTED）。

    这是"已离开 THINKING、还没到 IDLE"的窗口——修复前停在 INTERRUPTED，
    仍是活跃区（`is_accepting_input` 恒假 ⇒ 用户面永久失聪）。
    """
    h = harness_factory(generation_registry=_GateRegistry())
    gate = Gate()

    async def _suspending_listener(name: str) -> None:
        if name == "interrupted":
            await gate.pass_through()

    h.loop.status.on_change(_suspending_listener)  # noqa: SLF001
    gate.arm()

    task = await _first_cancel_at(h, gate, "_notify('interrupted')")
    assert h.fsm.state is AgentState.INTERRUPTED, h.diagnose()

    task.cancel()
    gate.open()
    await h.settle(task)

    h.assert_stable_terminal(task, expect_cancelled=True)


@pytest.mark.asyncio
async def test_second_cancel_during_interrupt_delay_settles(harness_factory):
    """C2 落在 UI 可观测延迟 `asyncio.sleep(0.05)` 上（§20 的分类结论：可取消但不保护）。

    50ms 延迟是**可被取消的 cosmetic/observability** 段，不得为了它把整个 handler
    shield 起来；保护的是它**之后**那次同步落位。
    """
    h = harness_factory(generation_registry=_GateRegistry())
    task = await h.start_turn()
    assert h.loop.cancel_current(source="button") is True  # noqa: SLF001

    # 结构化对齐到 50ms 窗口：等 INTERRUPTED 被广播（同步发生在延迟之前）
    await asyncio.wait_for(
        _wait_true(lambda: h.fsm.state is AgentState.INTERRUPTED), timeout=_PROBE_S)
    task.cancel()
    await h.settle(task)

    h.assert_stable_terminal(task, expect_cancelled=True)


@pytest.mark.asyncio
async def test_second_cancel_after_terminal_transition_keeps_terminal_state(harness_factory):
    """C2 落在**逻辑终态之后**（`_notify("idle")`）：划出临界区的右边界。

    ALREADY_GREEN_GUARD：修复前后都必须绿——越过 `transition(IDLE)` 之后，
    取消只影响资源清理，不再影响终态真相。
    """
    h = harness_factory(generation_registry=_GateRegistry())
    gate = Gate()

    async def _suspending_listener(name: str) -> None:
        if name == "idle":
            await gate.pass_through()

    h.loop.status.on_change(_suspending_listener)  # noqa: SLF001
    gate.arm()

    task = await _first_cancel_at(h, gate, "_notify('idle')")
    assert h.fsm.state is AgentState.IDLE, h.diagnose()

    task.cancel()
    gate.open()
    await h.settle(task)

    assert h.fsm.state is AgentState.IDLE, h.diagnose()
    assert h.fsm.states() == ["THINKING", "INTERRUPTED", "IDLE"], h.diagnose()
    assert task.cancelled() is True, h.diagnose()
    assert h.loop.process_lock.locked() is False, h.diagnose()  # noqa: SLF001


@pytest.mark.asyncio
async def test_triple_cancel_is_still_bounded(harness_factory):
    """连发三发（C1 收口开始 / C2+C3 同一 await）仍必须稳定收口（§42/§43）。"""
    h = harness_factory(generation_registry=_GateRegistry())
    gate = Gate()

    async def _suspending_listener(name: str) -> None:
        if name == "interrupted":
            await gate.pass_through()

    h.loop.status.on_change(_suspending_listener)  # noqa: SLF001
    gate.arm()

    task = await _first_cancel_at(h, gate, "_notify('interrupted')")

    task.cancel()   # C2
    task.cancel()   # C3 —— 同一 await 上的连发（asyncio 只投递一次，计数为 3）
    assert task.cancelling() == 3, h.diagnose()
    gate.open()
    await h.settle(task)

    h.assert_stable_terminal(task, expect_cancelled=True)


# ── 同一边界的另一半：异常收口自身被取消（§33） ─────────────────────────────


@pytest.mark.asyncio
async def test_cancel_during_error_finalization_settles(harness_factory):
    """取消落在**异常收口**（`_handle_turn_exception`）的 await 上（think 路径）。

    PRE_FIX_RED：这是与 KB-25 **同一 logical boundary** 的另一半——think 路径的
    `except Exception` 分支不在任何取消边界内，所以在它里面落下的取消会一路逃出
    `process()`（在 `except` 块里抛出的异常不会被兄弟 `except` 子句接住）⇒
    同样的无主 THINKING。生产形态：provider 失败正在收尾时用户按下停止按钮。
    """
    h = harness_factory(generation_registry=_GateRegistry())
    gate = Gate()
    _patch_output_method(h, "turn_failed", gate)
    gate.arm()

    task = await h.start_turn(failing=True)
    await gate.wait_started("output.turn_failed")
    assert h.fsm.state is AgentState.THINKING, h.diagnose()
    assert task.cancelling() == 0, "前置条件：此刻还没有任何取消"

    # 生产取消源：用户按停止按钮（此刻回合正在做**异常**收口）
    assert h.loop.cancel_current(source="button") is True  # noqa: SLF001
    assert task.cancelling() == 1
    gate.open()
    await h.settle(task)

    assert h.fsm.state is AgentState.IDLE, (
        "异常收口被取消 ⇒ 相位永久停在 THINKING（与 KB-25 同一边界）：" + h.diagnose())
    assert h.fsm.states() == ["THINKING", "INTERRUPTED", "IDLE"], h.diagnose()
    assert h.fsm.rejected() == [], h.diagnose()
    assert h.loop.process_lock.locked() is False, h.diagnose()  # noqa: SLF001


@pytest.mark.asyncio
async def test_normal_error_finalization_is_untouched(harness_factory):
    """ALREADY_GREEN_GUARD：**无取消**的异常收口逐项不变（终态标记 + 相位）。"""
    registry = _GateRegistry()
    h = harness_factory(generation_registry=registry)
    task = await h.start_turn(failing=True)
    await h.settle(task)

    assert h.fsm.state is AgentState.IDLE, h.diagnose()
    assert h.fsm.states() == ["THINKING", "INTERRUPTED", "IDLE"], h.diagnose()
    assert registry.marked and registry.marked[-1][1] == "failed", (
        f"错误路径的 generation 终态应为 failed：{registry.marked}")
    assert task.cancelled() is False, h.diagnose()


# ── 单发取消 / 正常回合：行为保持（§39/§40） ────────────────────────────────


@pytest.mark.asyncio
async def test_single_cancel_is_unchanged(harness_factory):
    """ALREADY_GREEN_GUARD：一发取消（Batch 10 语义）逐项不变。"""
    registry = _GateRegistry()
    h = harness_factory(generation_registry=registry)
    registry.mark_gate.arm()

    task = await _first_cancel_at(h, registry.mark_gate, "generation_registry.mark")
    registry.mark_gate.open()
    await h.settle(task)

    h.assert_stable_terminal(task, expect_cancelled=False)


@pytest.mark.asyncio
async def test_normal_turn_is_untouched(harness_factory):
    """ALREADY_GREEN_GUARD：正常回合状态序列逐字不变（无 interrupted）。"""
    h = harness_factory(generation_registry=_GateRegistry())
    h.stream_gate.open()
    task = await h.start_turn(hold_stream=False)
    await h.settle(task)

    assert h.fsm.state is AgentState.IDLE, h.diagnose()
    assert h.fsm.states() == ["THINKING", "SPEAKING", "IDLE"], h.diagnose()
    assert "interrupted" not in h.notified, h.diagnose()
    assert h.bus_recorder.count("idle") == 1, h.diagnose()


# ── 逻辑终态的完整性（CF2/CF4，§33/§64） ────────────────────────────────────


@pytest.mark.asyncio
async def test_ordinary_exception_in_cleanup_still_settles_state(harness_factory):
    """资源清理抛出**普通异常**时，相位同样必须落到 IDLE（CF4 / §64）。

    PRE_FIX_RED：`await self._output.typing(False)` 抛出的 RuntimeError 会直接穿出
    `_handle_interrupt`——`phases.handle_interrupt()` 根本不会被调用，相位停在
    THINKING。"外部清理失败不得 strand 核心生命周期"就是本批的 CF4。
    """
    registry = _GateRegistry()
    h = harness_factory(generation_registry=registry)

    inner_typing = h.output.typing

    async def _typing(flag, **kwargs):
        if flag is False:
            raise RuntimeError("output channel down")
        return await inner_typing(flag, **kwargs)

    h.output.typing = _typing  # type: ignore[method-assign]

    # 取消点选在 `generation_registry.register`（THINKING 入口之后、唯一未被 try 包住的
    # 挂起点）：这样 `typing(False)` 只会在 `_handle_interrupt` 里被调用一次。
    registry.register_gate.arm()
    task = await h.start_turn(hold_stream=False)
    await registry.register_gate.wait_started("generation_registry.register")
    assert h.fsm.state is AgentState.THINKING, h.diagnose()
    assert h.loop.cancel_current(source="button") is True  # noqa: SLF001
    registry.register_gate.open()
    await h.settle(task)

    assert h.fsm.state is AgentState.IDLE, (
        "资源清理抛出普通异常后相位停在活跃区：" + h.diagnose())
    assert h.fsm.states() == ["THINKING", "INTERRUPTED", "IDLE"], h.diagnose()
    assert h.loop.process_lock.locked() is False, h.diagnose()  # noqa: SLF001


@pytest.mark.asyncio
async def test_terminal_settle_does_not_duplicate_idle_when_phase_already_forced(harness_factory):
    """`finally` 里的 `if state != IDLE` 守卫：相位已被别的写入者强制成 IDLE 时**不得**再广播 IDLE。

    这是 §50（duplicate terminal event）的可执行判据，也是 Challenger B11 的 J/M5 指出的
    "未被覆盖的那一行"：删掉该守卫后，正常路径的 15 条用例**全都仍然绿**
    （legal path 上 `INTERRUPTED → IDLE` 永远是"需要"的那一次转换）。只有
    "期间已有别的写入者把相位推成 IDLE"（这里用订阅者在 `interrupted` 通知里病态地
    `fsm.reset()` 模拟）才能把这一行区分出来：没有守卫 ⇒ 收口会**再发一次** `bus.post(IDLE)`。

    语义边界（如实钉住）：这种情况下 IDLE **广播**由那个强制 IDLE 的写入者负责——
    `AgentFSM.reset()` 按设计不发事件（`agent_fsm.py`），所以本路径上收口不再补发，
    避免重复 terminal event；**后置条件仍成立**（相位确实是 IDLE、锁会释放、任务会落定）。
    """

    h = harness_factory(generation_registry=_GateRegistry())
    forced = asyncio.Event()

    async def _forcing_listener(name: str) -> None:
        if name == "interrupted" and not forced.is_set():
            forced.set()
            h.fsm.reset()          # 病态订阅者：在通知里强推 IDLE

    h.loop.status.on_change(_forcing_listener)  # noqa: SLF001

    task = await h.start_turn()
    assert h.loop.cancel_current(source="button") is True  # noqa: SLF001
    await h.settle(task)

    assert forced.is_set(), "用例前提不成立：强制 IDLE 的订阅者没有被调用"
    assert h.fsm.state is AgentState.IDLE, h.diagnose()
    assert h.bus_recorder.states == ["thinking", "interrupted"], (
        "相位已被强制成 IDLE，收口又广播了一次 terminal event：" + h.diagnose())
    assert h.loop.process_lock.locked() is False, h.diagnose()  # noqa: SLF001
    assert task.cancelled() is False, (
        "本例只有**一发**取消（无第二发）⇒ 由 canonical owner 消费，任务正常结束：" + h.diagnose())


@pytest.mark.asyncio
async def test_handle_interrupt_is_idempotent(harness_factory):
    """`handle_interrupt()` 重复调用必须是无副作用的 no-op（§24/§25 idempotency）。

    覆盖范围**明确限定**为"收口已完成（相位 IDLE）后再调用一次"这一形态——
    生产里它由 `_handle_interrupt` 的 `finally` 与 `handle_interrupt` 自身的
    `finally` 叠加触发（取消落在 `_notify("interrupted")` 时内层先落 IDLE）。

    PRE_FIX_RED：修复前第二次调用会走 `IDLE → INTERRUPTED`（转换表里的**非法边**）
    抛 `ValueError`。修复后它是 IDEMPOTENT 的 terminal operation，且**不重复广播**
    terminal event（§50）。

    **不声称**"任意入口状态下都幂等"：并发（两个 owner 同时进入）不在本判据内——
    它由 `_process_lock` 排除（见 `docs/architecture/invariants.md` §4.4）。
    """
    h = harness_factory(generation_registry=_GateRegistry())
    await h.loop.phases.enter_thinking()  # noqa: SLF001
    await h.loop.phases.handle_interrupt()  # noqa: SLF001
    assert h.fsm.state is AgentState.IDLE, h.diagnose()
    assert h.bus_recorder.states == ["thinking", "interrupted", "idle"], h.diagnose()

    await h.loop.phases.handle_interrupt()  # noqa: SLF001 — 第二次（幂等）

    assert h.fsm.state is AgentState.IDLE, h.diagnose()
    assert h.fsm.rejected() == [], (
        "重复收口走了非法转换边（IDLE→INTERRUPTED）：" + h.diagnose())
    assert h.bus_recorder.states == ["thinking", "interrupted", "idle"], (
        "重复收口广播了重复的 terminal event：" + h.diagnose())
    assert h.fsm.states() == ["THINKING", "INTERRUPTED", "IDLE"], h.diagnose()
