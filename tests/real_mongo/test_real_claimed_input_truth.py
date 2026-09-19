# 测试层：tests/real_mongo —— 真实 MongoDB 测试层
"""Batch 15（KB-46）真库旁证：**已 claim 的输入在被补写后，真的落进了两个存储**。

## 为什么需要真库（§105）

本批的修法是"在 terminal 收口链的零 await prologue 里补写一条 user message"。
它的持久化行为**完全继承** `SessionMemory.add_message`：

- 内存 deque append（同步）；
- `message_archive` 的真实 `insert_one`——经 **fire-and-forget** 任务
  （`session.py::_archive_with_limit`，Semaphore(8)），Mongo 离线时 **fail-open**
  （warning + return，不抛）；
- `sessions` 文档**不在** `add_message` 里写，只在既有的 `save()` / `save_all()`
  上做全量 `$set` upsert。

内存 fake 是同步返回的，**它无法回答**："真驱动的 `insert_one` 真的写下了吗""补写
N 次真的只留一行吗""`save()` 之后 `load()` 真的能读回这条吗"。本层因此只做**旁证**
（核心 race 仍由 `core/agent/tests/test_claimed_input_conversation_truth.py` 的
确定性屏障 E2 负责）。

## 本层**不**启动真 Agent（诚实声明）

根 conftest 的真实库防线按 marker 放行（`integration` / `slow` / `external`），而
`real_mongo` **不在**放行名单里 ⇒ 走 `DocumentDBFactory.create` 的 Agent 仓储会被
导流到内存 fake。要绕开只能 `no_storage_guard`，而那会让 Agent 的**其余全部仓储**
连上开发者**生产库**（该 fixture 自己的 docstring 已警告过）。

因此本层沿用既有 `test_real_finalization_cancellation.py` 的形状：直接构造一条真实
`CognitiveLoop`，其 session 绑定经 `real_mongo_repo` 工厂核验过落点的真仓储；回合
仍走**真实** `loop.process()` 与**真实** claim 路径（不手工赋 `_generation_ctx`），
只是不经过 AgentServer / EventBus 编排层。
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
from core.prompt.message_builder import MessageBuilder
from core.prompt.prompt_assembler import PromptAssembler
from core.schema.character import CharacterState
from core.schema.types import SessionId
from core.status.agent_fsm import AgentFSM
from core.status.fsm import FSMBackedPhaseController, FSMBackedStatus

#: marker + external tier：默认任何档位都不跑，只有 `make test-real-mongo` 会跑。
pytestmark = [pytest.mark.real_mongo, pytest.mark.external]

_PROBE_S = 20.0
_REPLY = "好呀，我在听。<mood>平静</mood>"
_SID = SessionId("real-mongo-kb46")


class Gate:
    """按需阻塞一次的屏障（结构化信号；包的是生产代码本来就会挂起的调用）。"""

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
async def loop_with_real_session(real_mongo_repo):
    """一条真实 `CognitiveLoop`，其 session 的两个存储都是**真库**。"""
    from core.storage.message_repo import MessageRepository
    from core.storage.session_repo import SessionRepository

    session_repo = await real_mongo_repo(SessionRepository)
    message_repo = await real_mongo_repo(MessageRepository)
    # 真库写入必须真的生效（否则本文件的"旁证"退化成 fake 短路）
    assert session_repo._db is not None  # noqa: SLF001 — 落点已由 conftest 核验
    assert message_repo._db is not None  # noqa: SLF001

    session = SessionMemory(session_id=_SID, repo=session_repo, msg_repo=message_repo)

    fsm = AgentFSM()
    bus = EventBus()
    output = OutputManager()
    char = CharacterConfig(name="TestBot")
    lt = LongTermMemory()
    prompt = PromptAssembler(char, session, lt)
    llm = MagicMock(spec=LLMAdapter)
    llm.model = "deepseek/deepseek-v4-flash"
    output.expression = MagicMock(return_value=MagicMock())

    async def _stream(*_args, **_kwargs):
        yield {"content": _REPLY, "tool_calls": None}

    llm.stream_think = _stream  # type: ignore[method-assign]

    engine = CharacterEngine(
        llm=llm, state=CharacterState(session_id=_SID),
        character_name="TestBot", user_name="妖梦",
        base_personality="", character_config=char, output=output,
    )

    async def _resolver(_cid, _pv) -> SessionMemory:
        return session

    registry = GenerationRegistry(repo=await real_mongo_repo(
        __import__("core.storage.generation_repo", fromlist=["GenerationRepository"])
        .GenerationRepository))
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
    loop._session = session  # noqa: SLF001 — 与既有真库 harness 同形
    try:
        yield loop, session, session_repo, message_repo
    finally:
        await output.close()


async def _settle(task: asyncio.Task) -> None:
    """等任务落定（超时 = 死锁探测，不是判据）。"""
    try:
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True),
                               timeout=_PROBE_S)
    except asyncio.TimeoutError:  # pragma: no cover
        raise AssertionError(f"回合未在 {_PROBE_S}s 内落定") from None


def _text_of(msg: object) -> str:
    """消息的纯文本（`content` 是 Part 列表，`str()` 会给出 repr——必须逐 Part 取）。"""
    parts = getattr(msg, "content", None)
    if not isinstance(parts, list):
        return str(parts or "")
    return "".join(str(getattr(p, "content", "")) for p in parts)


async def _archive_rows(message_repo, token: str) -> list[str]:
    """真库 `message_archive` 里含 token 的 user 消息（**真实驱动**查询）。"""
    msgs, _total = await message_repo.query_by_session(_SID, limit=200)
    out: list[str] = []
    for m in msgs:
        if str(getattr(m, "role", "")) != "user":
            continue
        text = _text_of(m)
        if token in text:
            out.append(text)
    return out


# ═══════════════════════════════════════════════════════════════════════
# RM1 — 窗口内取消 ⇒ 真库两个存储都**真的**有这条输入，且恰一条
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_rm1_recovered_claimed_input_is_really_persisted_once(
        loop_with_real_session):
    """W2 落点（`PromptAssembler.build_messages`）取消 ⇒ 真库 archive + sessions 各恰一条。"""
    loop, session, session_repo, message_repo = loop_with_real_session
    gate = Gate()
    inner = loop._prompt.build_messages  # noqa: SLF001

    async def _gated(**kwargs):
        await gate.pass_through()
        return await inner(**kwargs)

    loop._prompt.build_messages = _gated  # type: ignore[method-assign]  # noqa: SLF001
    gate.arm()

    token = "KB46-RM1-记住我喜欢咖啡"
    turn = asyncio.create_task(loop.process(token))
    loop.current_task = turn
    await asyncio.wait_for(gate.started.wait(), timeout=_PROBE_S)

    # ★ 前提断言（防假绿）：已 claim、未 record
    assert loop.current_turn_owns_input is True, "前提：已越过 claim commit point"
    assert loop._turn_message_persisted is False, "前提：record 尚未提交"  # noqa: SLF001
    assert token not in [_text_of(m) for m in session.messages], "前提：尚未进历史"

    assert loop.cancel_current(source="button") is True
    gate.open()
    await _settle(turn)

    # ① runtime conversation truth（内存窗口）
    in_mem = [_text_of(m) for m in session.messages if str(m.role) == "user"]
    assert in_mem.count(token) == 1, f"内存会话应恰一条：{in_mem}"

    # ② archive：drain 在途 fire-and-forget 归档任务后查**真库**
    await session.close_archive()
    rows = await _archive_rows(message_repo, token)
    assert len(rows) == 1, f"真库 message_archive 应恰一条，实测 {len(rows)}：{rows}"

    # ③ sessions 文档：save() 后经真仓储 load() 读回
    assert await session.save() is True
    loaded = await session_repo.load(_SID)
    assert loaded is not None, "真库 sessions 文档不存在——save/load 链路未生效"
    loaded_users = [m for m in loaded[0] if str(m.role) == "user"]
    texts = ["".join(str(getattr(p, "content", "")) for p in m.content)
             for m in loaded_users]
    assert texts.count(token) == 1, f"真库 sessions 应恰一条：{texts}"

    # ④ 幂等：再跑一次 canonical 终态 owner，两个存储都不得多出第二条
    assert loop._record_claimed_input("button") is False, (  # noqa: SLF001
        "已记录的输入不得被 recovery 再次补写")
    await session.close_archive()
    rows2 = await _archive_rows(message_repo, token)
    assert len(rows2) == 1, f"重复 recovery 后 archive 变成 {len(rows2)} 条：{rows2}"
    assert await session.save() is True
    loaded2 = await session_repo.load(_SID)
    assert loaded2 is not None
    texts2 = [_text_of(m) for m in loaded2[0] if str(m.role) == "user"]
    assert texts2.count(token) == 1, f"重复 recovery 后 sessions 变成：{texts2}"


# ═══════════════════════════════════════════════════════════════════════
# RM2 — 正常回合：真库两个存储各恰一条（不得因修复而双写）
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_rm2_normal_turn_persists_user_message_exactly_once(
        loop_with_real_session):
    """正常成功回合：内存 / archive / sessions 文档各恰一条（回归：修法不得双写）。"""
    loop, session, session_repo, message_repo = loop_with_real_session
    token = "KB46-RM2-正常路径唯一性"
    await loop.process(token)

    in_mem = [_text_of(m) for m in session.messages if str(m.role) == "user"]
    assert in_mem.count(token) == 1, f"内存会话应恰一条：{in_mem}"

    await session.close_archive()
    rows = await _archive_rows(message_repo, token)
    assert len(rows) == 1, f"真库 archive 应恰一条，实测 {len(rows)}：{rows}"

    assert await session.save() is True
    loaded = await session_repo.load(_SID)
    assert loaded is not None
    texts = [_text_of(m) for m in loaded[0] if str(m.role) == "user"]
    assert texts.count(token) == 1, f"真库 sessions 应恰一条：{texts}"


# ═══════════════════════════════════════════════════════════════════════
# RM3 — 下一回合 context 一致性：**从真库 load 回来**的历史仍喂进 prompt
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_rm3_reloaded_session_feeds_recovered_input_into_next_prompt(
        loop_with_real_session):
    """RM1 的结果经真库 round-trip 后，必须出现在下一回合的 history 通道里且恰一次。

    判据用真实 `MessageBuilder.build_messages`（历史通道 + 当前消息通道是两条独立
    通道——这正是"提前 record 会双计"的地方，故必须按**条数**钉住）。
    """
    loop, session, session_repo, _message_repo = loop_with_real_session
    gate = Gate()
    inner = loop._prompt.build_messages  # noqa: SLF001

    async def _gated(**kwargs):
        await gate.pass_through()
        return await inner(**kwargs)

    loop._prompt.build_messages = _gated  # type: ignore[method-assign]  # noqa: SLF001
    gate.arm()

    token = "KB46-RM3-记住我喜欢咖啡"
    turn = asyncio.create_task(loop.process(token))
    loop.current_task = turn
    await asyncio.wait_for(gate.started.wait(), timeout=_PROBE_S)
    assert loop.current_turn_owns_input is True and loop._turn_message_persisted is False  # noqa: SLF001
    loop.cancel_current(source="button")
    gate.open()
    await _settle(turn)

    assert await session.save() is True
    loaded = await session_repo.load(_SID)
    assert loaded is not None

    # 从**真库读回**的消息重建一个会话（模拟下一回合 / 重连回放）
    reloaded = SessionMemory(session_id=_SID)
    for m in loaded[0]:
        reloaded.add_message(m)

    history = reloaded.recent_messages(8)
    messages = MessageBuilder().build_messages(
        "（系统提示占位）", history, "KB46-RM3-下一个问题")
    flat = "\n".join(str(m.get("content", "")) for m in messages)
    assert flat.count(token) == 1, (
        f"被取消的已 claim 输入必须恰一次出现在下一回合 prompt 里："
        f"count={flat.count(token)} roles={[str(m.get('role')) for m in messages]}")
    assert flat.count("KB46-RM3-下一个问题") == 1
