# 测试文件：core/cognitive/tests/test_speaking_lifecycle.py
"""Batch 9（KB-6 / Speaking State Recovery）：SPEAKING 生命周期的退出不变量。

## 背景

KB-6 的原始症状是「TTS 密集短句时 `wait_all()` 无超时 ⇒ FSM 卡在 SPEAKING」。
它的**机制**由 S2-G3（9f323f07）定位并修掉两条挂起源：

1. `TTSStreamManager._download_and_forward` 的两个**早退分支**（`not self._enabled`
   / `not self._tts`）在 `try/finally` **之外**直接 `return` ⇒ 后继门控永不释放，
   句 N+1 永久挂在 `gate N+1` 上；而 `wait_all()` 没有超时 ⇒ `finish_turn` 卡住
   ⇒ FSM 停在 SPEAKING。
2. `reset_for_turn` 直接 `clear()` 门控 dict ⇒ 已持有旧 `Event` 对象的等待者
   永久挂起（重建 dict 无从唤醒它）。

两处都在 HEAD 上验证为**已修复**（`_release_gate` + `_turn_epoch`）。本模块补的是
**当时缺的那半**：既有 S2-G3 守卫全在 `TTSStreamManager` 层（manager 级），
没有任何用例证明「这条链一旦再断，**FSM 会被留在 SPEAKING**」——即
`wait_all` 永不返回 ⇒ `FSMBackedPhaseController.finish_turn` 永不推进。

## 本模块建立的判据（Batch 9 §3 / §99）

**非法 SPEAKING**：当前 utterance 已无任何仍然存活、仍然合法的输出工作
（TTS 任务已结束、门控没有未释放的等待者、epoch 没有残留），而 FSM 仍保持
SPEAKING 且没有生命周期事件能推进它。

**合法 SPEAKING**：TTS 正在合成、音频正在转发、queue 仍有当前 epoch 的 item、
输出 backend 正在 flush、当前 utterance 的 completion 尚未发生。

因此本模块**只用结构化终态信号**作为观察窗口（task done / 门控全开 /
`process()` 返回 / `cancel_current()` 的取消已被回合任务消费），
`asyncio.wait_for` 的超时只是**死锁探测器**，不是判据本身（§4/§36）。

## 假绿防护（§73）

每条用例都断言 `SPEAKING` **确实出现过**（`RecordingFSM.history` 里的
`(THINKING → SPEAKING, ok)`），再断言终态。只断言 `state != SPEAKING`
会在「根本没进过 SPEAKING」时假绿。

## 走的是真实入口（§31/§32）

所有用例经 **`CognitiveLoop.process()` → `_finalize_turn` → `_transition_idle`
→ `FSMBackedPhaseController.finish_turn` → 真实 `AgentFSM.transition(SPEAKING)`
/ `transition(IDLE)`**，不经 `fsm._state = …` 私有赋值，也不用手工构造的
"SPEAKING 前置状态"充当证据。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from Module.output import OutputManager
from Module.tts.protocol import TTSBackend
from core.adapter import ToolAdapter
from core.adapter.llm_adapter import LLMAdapter
from core.cognitive import CognitiveLoop
from core.cognitive.loop import _EMPTY_RESPONSE_FALLBACK
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

# 观察窗口：只在"没有任何结构化信号"时兜底，不是判据本身（§4）。
# 5s 远大于本模块任何一条正常路径的实际耗时（实测 < 50ms），
# 因此它只会在真回归（无界 await）时触发。
_DEADLOCK_PROBE_S = 5.0


# ── 测试替身 ─────────────────────────────────────────────────────────────────


class RecordingFSM(AgentFSM):
    """记录**每一次** transition 尝试（含被拒），供"确实进过 SPEAKING"断言。

    不引入生产侧 history 子系统（§74）：只是子类覆写公开方法。
    """

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

    def entered(self, state: AgentState) -> bool:
        """是否真的沿合法边进入过 `state`。"""
        return any(dst is state and kind == "ok" for _src, dst, kind in self.history)


class FakeTTS(TTSBackend):
    """遵守真实 `TTSBackend` contract 的确定性替身（§33）。

    模式：success / raise（合成期抛异常）/ empty（返回空 audio）/
    block（挂在 `release` 事件上，可被取消——复制真实 worker 在 await 中
    接收 `CancelledError` 的语义，§34）。
    """

    def __init__(self, mode: str = "success") -> None:
        self.mode = mode
        self.calls: list[str] = []
        self.blocked = asyncio.Event()   # 首次进入 synthesize
        self.release = asyncio.Event()
        self.release.set()

    # ---- TTSBackend 协议 ----

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def stream_format(self) -> str:
        return "wav"

    def get_preprocess(self):
        return None

    def default_voice(self, character: str = "") -> str:
        return "fake-voice"

    async def list_voices(self) -> list[dict]:
        return []

    async def test(self) -> dict:
        return {"ok": True}

    async def synthesize(self, text: str, *, character: str,
                         emotion: str | None = None,
                         voice_id: str | None = None) -> bytes:
        self.calls.append(text)
        self.blocked.set()
        await self.release.wait()
        if self.mode == "raise":
            raise RuntimeError("fake TTS 合成失败")
        if self.mode == "empty":
            return b""
        return b"RIFF" + b"\x00" * 60

    async def synthesize_stream(self, text: str, *, character: str,
                                emotion: str | None = None,
                                voice_id: str | None = None):
        audio = await self.synthesize(
            text, character=character, emotion=emotion, voice_id=voice_id)
        for offset in range(0, len(audio), 4096):
            yield audio[offset:offset + 4096]

    async def close(self) -> None:
        return None


def make_harness_for(tts: "FakeTTS") -> "SpeakingHarness":
    """直接构造 harness（不经 fixture）——交错类用例需要自己管理任务生命周期。"""
    return SpeakingHarness(tts)


def h_diag(loop, fsm) -> str:
    """交错类用例的诊断串（与 `SpeakingHarness.diagnose` 同信息，但不需要 harness 引用）。"""
    mgr = loop.tts_manager
    open_gates = sorted(seq for seq, ev in mgr._release_events.items() if not ev.is_set())  # noqa: SLF001
    alive = [t.get_name() for t in mgr._tasks if not t.done()]  # noqa: SLF001
    return (f"fsm={fsm.state} history={[(a.name, b.name, k) for a, b, k in fsm.history]} "
            f"epoch={mgr._turn_epoch} seq={mgr._seq} open_gates={open_gates} "  # noqa: SLF001
            f"alive_tts_tasks={alive} process_lock={loop.process_lock.locked()}")


def _one_chunk_stream(text: str):
    """假 LLM 流：逐字符吐 content（与真实流式 LLM 的 chunk 形状一致）。"""

    async def mock_stream(*_args, **_kwargs):
        for ch in text:
            yield {"content": ch, "tool_calls": None}

    return mock_stream


# ── harness ──────────────────────────────────────────────────────────────────


class SpeakingHarness:
    """一条真实循环 + 可读的说话生命周期现场。"""

    def __init__(self, tts: FakeTTS) -> None:
        self.tts = tts
        self.fsm = RecordingFSM()
        self.bus = EventBus()
        self.output = OutputManager()
        self.session = SessionMemory()
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
            tts=tts,
            runtime_registry=make_default_registry(),
        )
        status = FSMBackedStatus(self.fsm)
        self.loop.status = status
        self.loop.phases = FSMBackedPhaseController(
            fsm=self.fsm, bus=self.bus, output=self.output,
            tts_mgr=self.loop.tts_manager, status=status,
        )
        self.loop._session = self.session  # noqa: SLF001 — 与 core/tests/test_phase3.py 既有 harness 同形

    async def _resolver(self, _conversation_id, _protocol_version) -> SessionMemory:
        return self.session

    # ---- 现场诊断（断言失败时打印，§36）----

    def diagnose(self) -> str:
        mgr = self.loop.tts_manager
        open_gates = sorted(
            seq for seq, ev in mgr._release_events.items() if not ev.is_set())  # noqa: SLF001
        alive = [
            t.get_name() for t in mgr._tasks if not t.done()]  # noqa: SLF001
        return (
            f"fsm={self.fsm.state} history="
            f"{[(a.name, b.name, k) for a, b, k in self.fsm.history]} "
            f"epoch={mgr._turn_epoch} seq={mgr._seq} "  # noqa: SLF001
            f"open_gates={open_gates} alive_tts_tasks={alive}"
        )

    def assert_entered_speaking(self) -> None:
        """假绿防护：本轮必须**真的**沿合法边进过 SPEAKING（§73）。"""
        assert self.fsm.entered(AgentState.SPEAKING), (
            "用例前提不成立：本轮从未进入 SPEAKING —— 断言无判别力。"
            + self.diagnose())

    def assert_no_speaking_work_left(self) -> None:
        """非法 SPEAKING 的"没有任何存活输出工作"半句（§3）。"""
        mgr = self.loop.tts_manager
        alive = [t for t in mgr._tasks if not t.done()]  # noqa: SLF001
        assert not alive, f"存在未落定的 TTS 任务：{[t.get_name() for t in alive]}"
        open_gates = [
            seq for seq, ev in mgr._release_events.items() if not ev.is_set()]  # noqa: SLF001
        assert not open_gates, f"存在永不释放的门控等待者：{open_gates}"

    async def start_turn(self, text: str = "你好。<mood>平静</mood>") -> asyncio.Task:
        """在独立任务里跑一轮真实 `process()`（中断/取消需要外部取消者）。"""
        self.loop._llm.stream_think = _one_chunk_stream(text)  # noqa: SLF001
        return asyncio.create_task(self.loop.process("hi"))

    async def wait_tts_blocked(self) -> None:
        """等 TTS worker 真的停在 synthesize 上（结构化屏障，不是 sleep）。"""
        await asyncio.wait_for(self.tts.blocked.wait(), timeout=_DEADLOCK_PROBE_S)

    def block_tts(self) -> None:
        """让下一句 TTS 停在 synthesize 里，直到 `release_tts()`。

        **每次都重清 `blocked`**：它是"事件已发生过"的粘滞标志，跨轮复用会让
        `wait_tts_blocked()` 立刻返回（前置条件不成立 ⇒ 断言失去判别力）。
        """
        self.tts.blocked.clear()
        self.tts.release.clear()

    def release_tts(self) -> None:
        self.tts.release.set()

    async def finish(self, task: asyncio.Task) -> None:
        """等回合任务落定——结构化终态信号，超时只是死锁探测。"""
        try:
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True),
                                   timeout=_DEADLOCK_PROBE_S)
        except asyncio.TimeoutError:  # pragma: no cover — 只在真回归时触发
            pytest.fail(f"回合任务未在 {_DEADLOCK_PROBE_S}s 内落定（疑似无界 await）："
                        + self.diagnose())

    async def aclose(self) -> None:
        self.release_tts()
        pending = [
            t for t in self.loop.tts_manager._tasks if not t.done()]  # noqa: SLF001
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await self.output.close()


@pytest_asyncio.fixture
async def harness_factory():
    """工厂 fixture：每个用例自建 harness，结束统一收尾（§37 任务泄漏守卫）。"""
    made: list[SpeakingHarness] = []

    def _make(mode: str = "success") -> SpeakingHarness:
        h = SpeakingHarness(FakeTTS(mode))
        made.append(h)
        return h

    try:
        yield _make
    finally:
        for h in made:
            await h.aclose()


# ── SSR-T2：正常完成 ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_normal_speaking_leaves_speaking(harness_factory):
    """SSR-T2 / 矩阵 A：正常一轮 → `wait_all()` 返回 → 离开 SPEAKING 到 IDLE。"""
    h = harness_factory("success")
    await h.finish(await h.start_turn())
    h.assert_entered_speaking()
    assert h.fsm.state is AgentState.IDLE, h.diagnose()
    h.assert_no_speaking_work_left()
    assert h.tts.calls == ["你好。"], "TTS 未收到待合成句"


# ── SSR-T1：TTS 失败 ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tts_failure_leaves_speaking(harness_factory):
    """SSR-T1 / 矩阵 B：TTS worker 抛异常 → 任务仍未落定则不得留在 SPEAKING。"""
    h = harness_factory("raise")
    await h.finish(await h.start_turn())
    h.assert_entered_speaking()
    assert h.fsm.state is AgentState.IDLE, (
        "TTS 失败后 FSM 仍宣称 SPEAKING —— 失败真相与状态真相互相矛盾（SSR-6）。"
        + h.diagnose())
    h.assert_no_speaking_work_left()


@pytest.mark.asyncio
async def test_llm_stream_failure_after_first_sentence_leaves_speaking(harness_factory):
    """矩阵 B'：**已经进入 SPEAKING 之后** LLM 流抛异常 → 必须离开 SPEAKING。

    与 TTS 失败不同，这条异常会穿透 `_stream_llm_response`（它 `except BaseException`
    留痕后 `raise`）一路冒到 `process()`，由 `_handle_turn_exception` →
    `_handle_interrupt()` 收口。判别力在于：此时 FSM **确实**已经是 SPEAKING，
    而收口路径必须由**异常分支**（`transition(INTERRUPTED)`）完成——正常收尾路径
    `finish_turn` 在这条链上根本不会被执行到。
    """
    h = harness_factory("success")

    async def exploding_stream(*_args, **_kwargs):
        # 必须喂**三句**才能进入 SPEAKING：分句由后续输入驱动（句 N 的 SENTENCE
        # 在句 N+1 的字符到达时才 dispatch），而异常路径不 flush。探针实测：
        #   1 句 + raise → 从未进 SPEAKING（THINKING→INTERRUPTED→IDLE），断言无判别力；
        #   2 句 + raise → 同样没进（第 1 句还没被 dispatch）；
        #   3 句 + raise → 第 1 句已 dispatch ⇒ 真的从 SPEAKING 走失败分支。
        yield {"content": "第一句。", "tool_calls": None}
        yield {"content": "第二句。", "tool_calls": None}
        yield {"content": "第三句", "tool_calls": None}
        raise RuntimeError("LLM 流中途炸了")

    h.loop._llm.stream_think = exploding_stream  # noqa: SLF001
    task = asyncio.create_task(h.loop.process("hi"))
    await h.finish(task)

    h.assert_entered_speaking()
    assert h.fsm.state is AgentState.IDLE, (
        "流中途失败后 FSM 仍留在 SPEAKING（错误分支漏了生命周期收口）。" + h.diagnose())
    h.assert_no_speaking_work_left()


@pytest.mark.asyncio
async def test_output_sink_failure_leaves_speaking(harness_factory):
    """SSR-T6 / 矩阵 D：output sink（audio_chunk）抛异常 → 不得 stranded。"""
    h = harness_factory("success")
    boom = AsyncMock(side_effect=RuntimeError("output sink down"))
    h.output.audio_chunk = boom  # type: ignore[method-assign]

    await h.finish(await h.start_turn())

    h.assert_entered_speaking()
    assert boom.await_count >= 1, "用例前提不成立：audio_chunk 从未被调用（sink 没被走到）"
    assert h.fsm.state is AgentState.IDLE, (
        "output 失败后 FSM 仍宣称 SPEAKING（SSR-2：terminal output path）。" + h.diagnose())
    h.assert_no_speaking_work_left()


@pytest.mark.asyncio
async def test_output_sink_failure_before_first_chunk(harness_factory):
    """SSR-T6 变体：`audio_start` 就失败（部分输出之前）。"""
    h = harness_factory("success")
    boom = AsyncMock(side_effect=RuntimeError("audio_start down"))
    h.output.audio_start = boom  # type: ignore[method-assign]

    await h.finish(await h.start_turn())

    h.assert_entered_speaking()
    assert boom.await_count >= 1
    assert h.fsm.state is AgentState.IDLE, h.diagnose()
    h.assert_no_speaking_work_left()


# ── SSR-T5：empty / 无可输出内容 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_audio_leaves_speaking(harness_factory):
    """SSR-T5 / 矩阵 C：TTS 返回空 audio → 仍须离开 SPEAKING。"""
    h = harness_factory("empty")
    await h.finish(await h.start_turn())
    h.assert_entered_speaking()
    assert h.fsm.state is AgentState.IDLE, h.diagnose()
    h.assert_no_speaking_work_left()


@pytest.mark.asyncio
async def test_empty_llm_response_does_not_strand_speaking(harness_factory):
    """矩阵 K：LLM 只回 `<mood>`（无文本、无工具调用）→ 空响应守卫介入，不得 stranded。

    空响应守卫（`_think_loop::_resolve_empty_response`）在重试仍空后注入**兜底回复**
    （`_EMPTY_RESPONSE_FALLBACK`），那是一条真实的对用户可见文本 —— 所以本用例的
    判据不是"空响应不进 SPEAKING"（那由 `_handle_sentence_event` 决定，与守卫无关），
    而是：**无论这条分支走不走 TTS，都不得把状态留在 SPEAKING**。
    因此这里对 TTS 调用只断言"不包含用户原话以外的内容"，不锁定守卫的内部轮次形状。
    """
    h = harness_factory("success")
    await h.finish(await h.start_turn("<mood>平静</mood>"))

    assert h.fsm.state is AgentState.IDLE, h.diagnose()
    h.assert_no_speaking_work_left()
    assert all(call == _EMPTY_RESPONSE_FALLBACK for call in h.tts.calls), (
        f"只有兜底回复可能被送去合成，实际：{h.tts.calls}")


@pytest.mark.asyncio
async def test_markup_only_response_does_not_strand_speaking(harness_factory):
    """矩阵 K（无可朗读内容）：只有 mood 标签 + PAUSE 停顿标记。

    `PAUSE(<#0.5#>)` 是 TTS 停顿标记、不是可朗读内容；这种轮次即使被判成一个
    "句子"（`speaking_started=True`），也**没有**任何 TTS 合成请求。
    这正是 §13 的情形：**进了 SPEAKING 却没有 TTS work** —— 判据是它仍必须退出。
    """
    h = harness_factory("success")
    await h.finish(await h.start_turn("<mood>平静</mood><#0.5#>"))

    assert h.fsm.state is AgentState.IDLE, (
        "无可朗读内容的一轮把状态留在了 SPEAKING（无任何 TTS work）。" + h.diagnose())
    h.assert_no_speaking_work_left()
    assert h.tts.calls == [], f"无可朗读内容却发起了合成：{h.tts.calls}"


# ── SSR-T3 / SSR-T5：中断与取消 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_interrupt_during_speaking_leaves_speaking(harness_factory):
    """SSR-T3 / 矩阵 E：SPEAKING 期间用户打断 → 取消是终态生命周期事件。"""
    h = harness_factory("success")
    h.block_tts()                         # 让 TTS 停在 synthesize（确定性屏障）
    task = await h.start_turn("第一句。第二句。第三句。<mood>平静</mood>")
    await h.wait_tts_blocked()

    h.assert_entered_speaking()
    assert h.fsm.state is AgentState.SPEAKING, "用例前提不成立：打断时不在 SPEAKING"

    h.loop.cancel_current(source="vad")
    await h.finish(task)

    assert h.fsm.state is AgentState.IDLE, (
        "打断后 FSM 仍停在 SPEAKING（SSR-5：cancellation 是终态生命周期事件）。"
        + h.diagnose())
    h.assert_no_speaking_work_left()


@pytest.mark.asyncio
async def test_cancel_stream_during_speaking_leaves_speaking(harness_factory):
    """矩阵 G：单句 playback cancel（`cancel_stream`）后整轮仍须正常收口。"""
    h = harness_factory("success")
    h.block_tts()
    task = await h.start_turn("第一句。第二句。<mood>平静</mood>")
    await h.wait_tts_blocked()
    h.assert_entered_speaking()

    # worker 尚未进入转发（audio_start 之前）→ cancel_stream 返回 False 属契约内
    for sid in list(h.loop.tts_manager._task_by_sid):  # noqa: SLF001
        h.loop.tts_manager.cancel_stream(sid)

    h.release_tts()
    await h.finish(task)

    assert h.fsm.state is AgentState.IDLE, h.diagnose()
    h.assert_no_speaking_work_left()


@pytest.mark.asyncio
async def test_shutdown_during_speaking_leaves_speaking(harness_factory):
    """矩阵 J：SPEAKING 期间停机（cancel_current + cancel_all + drain）→ 不留误导性真相。"""
    h = harness_factory("success")
    h.block_tts()
    task = await h.start_turn("第一句。第二句。<mood>平静</mood>")
    await h.wait_tts_blocked()
    h.assert_entered_speaking()

    # Agent.stop() 的 `_stop_cognitive_and_tts` + `_drain_cognitive_turns` 顺序
    h.loop.mark_shutting_down()
    h.loop.cancel_current()
    h.loop.tts_manager.cancel_all()
    await h.finish(task)

    assert h.fsm.state is AgentState.IDLE, (
        "停机后 FSM 仍宣称 SPEAKING（误导性 active-speaking 真相）。" + h.diagnose())
    h.assert_no_speaking_work_left()


@pytest.mark.asyncio
async def test_speaking_does_not_exit_while_output_work_is_alive(harness_factory):
    """合法 SPEAKING 的另一半：输出工作还活着时，**不得**提前退出 SPEAKING（§3/§45）。

    这条守卫的是「退出由生命周期完成驱动，而不是队列空/任务创建了」：
    多句 utterance 里句 1 还在合成时，`finish_turn` 若不等 `wait_all()` 就直接转
    IDLE，用户看到的状态就成了"说完了"——而音频此刻才开始推给前端（语音与状态、
    与下一轮准入判定全部错位）。

    观察窗口仍是结构化信号：用一个"继续阻塞 / 放行"的事件屏障，断言 SPEAKING 在
    阻塞期间保持、在放行后由真实 `process()` 收口处退出。
    """
    h = harness_factory("success")
    h.block_tts()
    task = await h.start_turn("第一句。第二句。<mood>平静</mood>")
    await h.wait_tts_blocked()
    h.assert_entered_speaking()
    assert h.fsm.state is AgentState.SPEAKING, h.diagnose()

    await asyncio.sleep(0)          # 只做事件循环让步：给"提前退出"留出发生的机会
    assert h.fsm.state is AgentState.SPEAKING, (
        "输出工作尚未结束就离开了 SPEAKING —— 退出不是由说话生命周期完成驱动的。"
        + h.diagnose())

    h.release_tts()
    await h.finish(task)
    assert h.fsm.state is AgentState.IDLE, h.diagnose()
    h.assert_no_speaking_work_left()


@pytest.mark.asyncio
async def test_tts_toggle_mid_turn_does_not_strand_speaking(harness_factory):
    """**Batch 9 主复现**（KB-6 根因③）：本轮播放中把 TTS 关掉再打开 ⇒ 曾经永久 SPEAKING。

    真实用户操作：聊天中途点掉 TTS（`Agent.set_service("tts", False)` →
    `_sync_tts_gate()` → `TTSStreamManager.set_enabled(False)`），随后又打开。副作用链：

    - `stream_sentence()` 只把该句的 Task 交给事件循环排队，**首个 step 要等下一批**；
      `set_enabled(False)` 的同步段（`_enabled=False` → `flush_on_cancel` →
      `cancel_all()`，段内无 `await`）能在那个 step 之前把它取消。
    - Python 3.12 对**未启动**协程是"在起点抛出"：早退分支（含它显式的
      `_release_gate`）与 `try/finally` **一行都没进** ⇒ 后继门控永不 set。
    - 之后同一轮再有句子入队 → 它建出的门控没人放行 → 永久停在 `await gate.wait()`
      ⇒ `wait_all()`（无超时）永不返回 ⇒ `finish_turn` 卡住 ⇒ **FSM 停在 SPEAKING**。

    这条时序不需要 sleep 或巧合：`create_task` 把首 step 推到下一批，而 WS handler
    的 step 能与本轮的入队 step 落在同一批（`BaseEventLoop._run_once` 先 poll I/O
    再跑本批 ready）。判据是本用例的核心断言——**进了 SPEAKING 就必须能出来**。

    修复前：本用例在 `_DEADLOCK_PROBE_S` 处红（隔离 worktree 复验）。
    """
    h = make_harness_for(FakeTTS("success"))
    loop, fsm = h.loop, h.fsm
    handler_go = asyncio.Event()
    chunk_go = asyncio.Event()

    async def ws_handler() -> None:
        """模拟 WS handler：`Agent.set_service("tts", False/True)`。"""
        await handler_go.wait()
        await loop.tts_manager.set_enabled(False)   # 同步段 cancel 掉"未启动"的 TTS 任务
        await loop.tts_manager.set_enabled(True)
        chunk_go.set()

    async def mock_stream(*_args, **_kwargs):
        # 分句由后续输入驱动：喂两块后第一句的 SENTENCE 才 dispatch（→ stream_sentence）。
        handler_go.set()                    # handler 的 step 排在 TTS 任务首 step 之前
        yield {"content": "第一句。", "tool_calls": None}
        yield {"content": "第二句。", "tool_calls": None}
        await chunk_go.wait()               # 回合在此挂起，此时 TTS 任务尚未 step
        yield {"content": "第三句。", "tool_calls": None}
        yield {"content": "<mood>平静</mood>", "tool_calls": None}

    loop._llm.stream_think = mock_stream           # noqa: SLF001
    handler = asyncio.create_task(ws_handler(), name="fake-ws-handler")
    turn = asyncio.create_task(loop.process("hi"), name="turn")

    try:
        await asyncio.wait_for(asyncio.gather(turn, handler, return_exceptions=True),
                               timeout=_DEADLOCK_PROBE_S)
    except asyncio.TimeoutError:  # pragma: no cover — 只在真回归时触发
        turn.cancel()
        handler.cancel()
        await asyncio.gather(turn, handler, return_exceptions=True)
        await h.aclose()
        pytest.fail("TTS 中途关→开后回合未能收口（门控孤儿 ⇒ wait_all 永不返回）："
                    + h_diag(loop, fsm))

    try:
        assert fsm.entered(AgentState.SPEAKING), "用例前提不成立：本轮从未进入 SPEAKING"
        assert fsm.state is AgentState.IDLE, (
            "开关 TTS 后 FSM 停在 SPEAKING（新回合将拿不到 process lock）。"
            + h_diag(loop, fsm))
        mgr = loop.tts_manager
        alive = [t for t in mgr._tasks if not t.done()]  # noqa: SLF001
        assert not alive, f"残留 TTS 任务：{[t.get_name() for t in alive]}"
    finally:
        await h.aclose()


# ── SSR-T4：迟到完成不得覆盖新一轮 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_completion_from_interrupted_turn_cannot_overwrite_next_turn(
        harness_factory):
    """SSR-T4 / 矩阵 M：A 被打断 → B 已开始 → A 的迟到完成不得把 B 切回 IDLE。

    判别方式：在 B 的**说话窗口内**手工推进 A 的迟到收尾（同一段完成代码），
    断言 B 的 SPEAKING 没有被这次迟到收尾改成 IDLE，且 B 最终以 IDLE 正常收口。
    这正是 §99 的判据——「旧 speaking lifecycle 不得错误覆盖当前新生命周期」，
    比「最终变成 IDLE」强。
    """
    h = harness_factory("success")

    # ── A：被打断的说话轮 ──
    h.block_tts()
    task_a = await h.start_turn("A1。A2。<mood>平静</mood>")
    await h.wait_tts_blocked()
    h.assert_entered_speaking()
    h.loop.cancel_current(source="vad")
    await h.finish(task_a)
    assert h.fsm.state is AgentState.IDLE, h.diagnose()
    h.release_tts()
    await asyncio.sleep(0)          # 只做一次事件循环让步，让 A 的残留任务看到新 epoch

    # ── B：新一轮说话（同一 FSM，真实 entry path）──
    h.block_tts()
    task_b = await h.start_turn("B1。B2。<mood>平静</mood>")
    await h.wait_tts_blocked()
    assert h.fsm.state is AgentState.SPEAKING, "用例前提不成立：B 未在 SPEAKING"

    # A 的迟到完成路径（与 B 并发）——若 A 的生命周期还能推进状态，这里就会露出来
    stale = asyncio.create_task(h.loop._transition_idle())  # noqa: SLF001
    await asyncio.sleep(0)
    assert h.fsm.state is AgentState.SPEAKING, (
        "旧说话生命周期的迟到完成把 B 的 SPEAKING 覆盖成了 IDLE（SSR-4）。" + h.diagnose())
    stale.cancel()
    await asyncio.gather(stale, return_exceptions=True)

    # ── B 正常收口 ──
    h.release_tts()
    await h.finish(task_b)
    assert h.fsm.state is AgentState.IDLE, h.diagnose()
    h.assert_no_speaking_work_left()


# ── 语义边界：TTS 关闭 / TTS 不可用 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_speaking_semantics_is_server_output_not_audio(harness_factory):
    """矩阵 N（§14/§15/§16）：SPEAKING 的产品语义是"正在向用户输出 response"。

    即使 TTS 被关闭 / 运行期后端为 None（纯文字回复），首句到达时仍进入 SPEAKING
    —— 由 `_handle_sentence_event` 无条件 `enter_speaking()` 决定。这里的守卫是
    **该语义下同样必须退出**：谁进谁出，不能因为"没有 TTS 工作可等"就少一次 exit。
    """
    for label, mutate in (
        ("tts-disabled", lambda h: h.loop.tts_manager.set_enabled(False)),
        ("tts-unavailable", lambda h: h.loop.tts_manager.set_backend(None)),
    ):
        h = harness_factory("success")
        result = mutate(h)
        if asyncio.iscoroutine(result):
            await result
        await h.finish(await h.start_turn())

        h.assert_entered_speaking()
        assert h.fsm.state is AgentState.IDLE, f"[{label}] " + h.diagnose()
        h.assert_no_speaking_work_left()
        assert h.tts.calls == [], f"[{label}] 不该有 TTS 合成调用"


# ── 后台任务分支 ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_speaking_with_background_task_lands_in_idle_background(harness_factory):
    """矩阵：有后台任务时 SPEAKING → IDLE_BACKGROUND_TASK（不是 IDLE，也不是 SPEAKING）。"""
    h = harness_factory("success")

    class _TaskSystem:
        async def list_active_tasks(self) -> list[dict]:
            return [{"id": "t1"}]

    h.loop._task_system = _TaskSystem()          # noqa: SLF001
    h.loop._on_background_task = lambda *_a, **_k: None  # noqa: SLF001

    await h.finish(await h.start_turn())

    h.assert_entered_speaking()
    assert h.fsm.state is AgentState.IDLE_BACKGROUND_TASK, h.diagnose()
    h.assert_no_speaking_work_left()
