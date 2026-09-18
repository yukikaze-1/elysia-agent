# 测试层：tests/real_mongo —— 真实 MongoDB 测试层
"""Batch 11（KB-25）真库旁证：**真实持久化延迟/行为**下的取消安全收口。

## 为什么需要真库（§89）

本批修复的核心正是"收口链上的外部 I/O 被第二发取消打断时，runtime 终态仍必须稳定"，
而收口链里**唯一无上界**的外部 I/O 就是 `generation_registry.mark` → 真实 Mongo
upsert（`core/storage/generation_repo.py` 全文件零 `wait_for`/`timeout=`；唯一的界
是 server-selection 3s，`socketTimeoutMS` 默认 `None`）。内存 fake 是同步返回的，
**它无法回答"真驱动的写入延迟/失败形态会不会改变结论"**。

本层因此只做**旁证**（不是核心判据）：

1. `register` 真的在真库写下 `status="running"` 一行（证明链路真的走了 Mongo，
   不是被 fake 短路——否则本文件是假绿）；
2. 第二发取消**确定性地**落在收口链的屏障上（不依赖时序精度），收口结束后：
   - runtime 终态稳定（FSM IDLE + 锁释放 + 任务 cancelled）；
   - 真库那一行的终态**要么**是 terminal status，**要么**仍是 `running` 并被启动期
     `mark_stale_interrupted(cutoff)` 兜底清扫成 `interrupted`——这正是
     §17/§18 的「runtime terminal truth 优先于 persisted terminal truth」。
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
from core.cognitive.generation_registry import GenerationRegistry
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

#: marker + external tier：默认任何档位都不跑，只有 `make test-real-mongo` 会跑。
pytestmark = [pytest.mark.real_mongo, pytest.mark.external]

_PROBE_S = 20.0
_REPLY = "好呀，我在听。<mood>平静</mood>"
_ACTIVE = {AgentState.THINKING, AgentState.EXECUTING, AgentState.SPEAKING,
           AgentState.INTERRUPTED}


class Gate:
    """按需阻塞一次的屏障（结构化信号）。"""

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


@pytest_asyncio.fixture
async def loop_with_real_registry(real_mongo_repo):
    """一条真实 `CognitiveLoop`，其 generation 持久化走**真库**。"""
    from core.storage.generation_repo import GenerationRepository

    repo = await real_mongo_repo(GenerationRepository)
    # 真库写入必须真的生效（否则本文件的"旁证"退化成 fake 短路）
    assert repo._db is not None  # noqa: SLF001 — 本层已由 conftest 核验落点

    fsm = AgentFSM()
    bus = EventBus()
    output = OutputManager()
    session = SessionMemory()
    char = CharacterConfig(name="TestBot")
    lt = LongTermMemory()
    prompt = PromptAssembler(char, session, lt)
    llm = MagicMock(spec=LLMAdapter)
    llm.model = "deepseek/deepseek-v4-flash"
    output.expression = MagicMock(return_value=MagicMock())
    engine = CharacterEngine(
        llm=llm, state=CharacterState(session_id=SessionId("real-mongo-sid")),
        character_name="TestBot", user_name="妖梦",
        base_personality="", character_config=char, output=output,
    )

    async def _resolver(_cid, _pv) -> SessionMemory:
        return session

    registry = GenerationRegistry(repo=repo)
    loop = CognitiveLoop(
        llm=llm, fsm=fsm, event_bus=bus, output=output, prompt=prompt,
        session_resolver=_resolver, tool_adapter=ToolAdapter([]),
        character_engine=engine,
        consolidator=MemoryConsolidator(llm, lt, "TestBot"),
        tts=None, generation_registry=registry,
        runtime_registry=make_default_registry(),
    )
    status = FSMBackedStatus(fsm)
    loop.status = status
    loop.phases = FSMBackedPhaseController(
        fsm=fsm, bus=bus, output=output, tts_mgr=loop.tts_manager, status=status)
    loop._session = session  # noqa: SLF001 — 与既有 harness 同形
    try:
        yield loop, registry, fsm
    finally:
        await output.close()


@pytest.mark.asyncio
async def test_second_cancel_keeps_runtime_terminal_truth_with_real_mongo(
    loop_with_real_registry,
):
    """真库写入路径 + 确定性第二发取消 ⇒ runtime 终态仍稳定，persisted 有兜底。"""
    loop, registry, fsm = loop_with_real_registry
    stream, gate = Gate(), Gate()

    async def _stream(*_args, **_kwargs):
        await stream.pass_through()
        yield {"content": _REPLY, "tool_calls": None}

    loop._llm.stream_think = _stream  # noqa: SLF001
    stream.arm()

    inner_tc = loop._output.turn_cancelled  # noqa: SLF001

    async def _turn_cancelled(ctx):
        await gate.pass_through()
        return await inner_tc(ctx)

    loop._output.turn_cancelled = _turn_cancelled  # type: ignore[method-assign]  # noqa: SLF001
    gate.arm()

    # 回合任务：第二发取消与生产同源（`AsyncTaskScope.drain` 预算耗尽后直接
    # `task.cancel()`，`Module/_common/task_scope.py:277`）
    turn = asyncio.create_task(loop.process("真库旁证"))
    loop.current_task = turn

    # ① 等回合真的钉在 LLM 流上（此时 FSM 确定是 THINKING、generation 已建档）
    await asyncio.wait_for(stream.started.wait(), timeout=_PROBE_S)
    assert fsm.state is AgentState.THINKING
    gen_id = loop._generation_ctx.generation_id  # noqa: SLF001
    row = await registry.get(gen_id)
    assert row is not None and row.get("status") == "running", (
        f"真库未写下 running 行——本文件的真库旁证前提不成立：{row}")

    # ② 第一发取消（生产路径）→ 收口开始，停在屏障上
    assert loop.cancel_current(source="button") is True
    await asyncio.wait_for(gate.started.wait(), timeout=_PROBE_S)
    assert turn.cancelling() == 1

    # ③ 第二发取消：与 AsyncTaskScope.drain 超时后的动作逐字相同
    turn.cancel()
    assert turn.cancelling() == 2
    gate.open()
    await asyncio.wait_for(asyncio.gather(turn, return_exceptions=True), timeout=_PROBE_S)

    # ④ runtime terminal truth 必须稳定（与持久化是否赶上无关）
    assert fsm.state not in _ACTIVE, f"真库写入路径下相位留在活跃区：{fsm.state}"
    assert fsm.state is AgentState.IDLE
    assert loop.process_lock.locked() is False
    assert turn.cancelled() is True

    # ⑤ persisted truth：要么已 terminal，要么仍是 running —— 后者由启动期兜底清扫
    row = await registry.get(gen_id)
    assert row is not None, "generation 行凭空消失（register 已成功过）"
    status = row.get("status")
    if status == "running":
        swept = await registry.mark_stale_interrupted(cutoff_iso="2999-01-01T00:00:00+00:00")
        assert swept >= 1, "兜底清扫没有覆盖残留的 running 行"
        row = await registry.get(gen_id)
        assert row is not None and row["status"] == "interrupted", (
            f"兜底清扫未把残留行收敛为 interrupted：{row}")
    else:
        assert status in ("cancelled", "interrupted", "failed"), (
            f"persisted 终态不是合法 terminal status：{status}")


@pytest.mark.asyncio
async def test_normal_turn_persists_completed_with_real_mongo(loop_with_real_registry):
    """ALREADY_GREEN_GUARD：正常回合在真库上仍以 `completed` 收口（修复未损主路径）。"""
    loop, registry, _fsm = loop_with_real_registry
    turn = asyncio.create_task(loop.process("正常回合"))
    loop.current_task = turn
    await asyncio.wait_for(asyncio.gather(turn, return_exceptions=True), timeout=_PROBE_S)

    gen_id = loop._generation_ctx.generation_id  # noqa: SLF001
    row = await registry.get(gen_id)
    assert row is not None and row["status"] == "completed", (
        f"真库上的正常回合终态不是 completed：{row}")
