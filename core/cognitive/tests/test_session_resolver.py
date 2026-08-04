# core/cognitive/tests/test_session_resolver.py
"""批次 3 Task 8：CognitiveLoop session_resolver + target 归属 + conversation_id 贯通。

覆盖 spec §3.4（D-m）：
- process(conversation_id=..., protocol_version=2) → resolver 目标会话收到 add_message
- 无 conversation_id + protocol_version=1 → resolver(None, 1) → default 会话
- _after_response 用捕获的 target（resolver 切走后异步延续仍写发起会话）
- cancel_current(source) 贯通 source（供 Task 9 mark 拆分）
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.status.agent_fsm import AgentFSM
from core.monitoring.event_bus import EventBus
from Module.output import OutputManager
from core.memory.session import SessionMemory
from core.cognitive import CognitiveLoop
from core.adapter.llm_adapter import LLMAdapter

pytestmark = pytest.mark.service


# ═══════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════

def _make_loop(sessions: dict, default_session: SessionMemory) -> CognitiveLoop:
    """构造最小 CognitiveLoop，注入 fake resolver（记录调用 + 按 conversation_id 路由）。

    resolver 语义对齐 SessionManager.resolve：
      protocol_version < 2 → default；>=2 且 conversation_id → 该会话。
    """
    llm = MagicMock(spec=LLMAdapter)
    llm.model = "mock"

    async def fake_stream(messages, tools=None, **kw):
        for c in "你好":
            yield {"content": c, "tool_calls": None}
    llm.stream_think = fake_stream

    output = OutputManager()
    fsm = AgentFSM()
    bus = EventBus()
    prompt = MagicMock()
    prompt._character = MagicMock(name="TestBot")
    prompt._character.traits = []
    # 注意：用 MagicMock + 显式 AsyncMock 的 build_messages——不能整对象 AsyncMock，
    # 否则 set_session（sync）也会变成 async，产生未 await 的 coroutine 警告。
    prompt.build_messages = AsyncMock(return_value=[
        {"role": "system", "content": "test"},
        {"role": "user", "content": "hello"},
    ])

    calls = []

    async def resolver(conversation_id, protocol_version):
        calls.append((conversation_id, protocol_version))
        if protocol_version < 2 or conversation_id is None:
            return default_session
        return sessions.get(conversation_id, default_session)

    loop = CognitiveLoop(
        llm=llm, fsm=fsm, event_bus=bus, output=output,
        prompt=prompt, session_resolver=resolver,
    )
    loop._resolver_calls = calls
    return loop


async def _await_pending(loop: CognitiveLoop) -> None:
    """等待所有 pending after_response task 完成。"""
    pending = [t for t in loop._pending_after_response if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# ═══════════════════════════════════════════════════════════════════
# 测试
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_process_routes_to_conversation_session():
    """process(conversation_id="conv_x", protocol_version=2) → add_message 落在 conv_x 实例。"""
    conv_x = SessionMemory(session_id="conv_x")
    default = SessionMemory(session_id="default")
    loop = _make_loop({"conv_x": conv_x}, default)

    await loop.process("hello", conversation_id="conv_x", protocol_version=2)
    await _await_pending(loop)

    assert loop._resolver_calls == [("conv_x", 2)]
    # 用户消息 + assistant 消息都落在 conv_x（目标会话），default 无消息
    assert len(conv_x.recent_messages()) >= 2
    assert len(default.recent_messages()) == 0
    await loop._output.close()


@pytest.mark.asyncio
async def test_no_conversation_id_v1_routes_to_default():
    """无 conversation_id + protocol_version=1 → resolver(None, 1) → default 会话。"""
    conv_x = SessionMemory(session_id="conv_x")
    default = SessionMemory(session_id="default")
    loop = _make_loop({"conv_x": conv_x}, default)

    await loop.process("hello")  # 默认 protocol_version=1
    await _await_pending(loop)

    assert loop._resolver_calls == [(None, 1)]
    assert len(default.recent_messages()) >= 2
    assert len(conv_x.recent_messages()) == 0
    await loop._output.close()


@pytest.mark.asyncio
async def test_after_response_writes_captured_target():
    """resolver 切走后，异步 _after_response 仍写发起会话（局部捕获，不读 self._session）。"""
    conv_a_repo = AsyncMock()
    conv_b_repo = AsyncMock()
    conv_a = SessionMemory(session_id="conv_a", repo=conv_a_repo)
    conv_b = SessionMemory(session_id="conv_b", repo=conv_b_repo)
    default = SessionMemory(session_id="default")
    loop = _make_loop({"conv_a": conv_a, "conv_b": conv_b}, default)

    # after_response#1 阻塞在 consolidator.consolidate——保证其后续 save 的
    # self._session 读取发生在第二轮（self._session 已换绑为 conv_b）之后。
    # 正确实现：session 参数在函数开头捕获（conv_a），save 落 conv_a；
    # 错误实现：save 行读 self._session（=conv_b），conv_a_repo.save 恒 0。
    gate = asyncio.Event()
    consolidator = MagicMock()

    async def gated_consolidate(user, assistant):
        await gate.wait()
        return []

    consolidator.consolidate = gated_consolidate
    loop._consolidator = consolidator

    # 第一轮：conversation_id="conv_a" → after_response#1 阻塞在 consolidate
    await loop.process("msg1", conversation_id="conv_a", protocol_version=2)
    # 第二轮：conversation_id="conv_b" → self._session 已被换绑为 conv_b
    await loop.process("msg2", conversation_id="conv_b", protocol_version=2)
    # 释放 gate——after_response#1/#2 继续到 save
    gate.set()
    await _await_pending(loop)

    # after_response#1 落 conv_a（发起会话），after_response#2 落 conv_b
    assert conv_a_repo.save.await_count == 1
    assert conv_b_repo.save.await_count == 1
    await loop._output.close()


@pytest.mark.asyncio
async def test_cancel_current_accepts_and_stores_source():
    """cancel_current(source) 接受 source 参数、取消当前任务，并把 source 贯通到中断路径。"""
    conv_x = SessionMemory(session_id="conv_x")
    loop = _make_loop({"conv_x": conv_x}, SessionMemory(session_id="default"))

    task = asyncio.create_task(asyncio.sleep(60))
    loop._current_task = task

    assert loop.cancel_current("disconnect") is True
    assert loop._current_task is None
    # source 贯通：存到中断路径可见的字段（Task 9 mark 拆分消费）
    assert loop._interrupt_source == "disconnect"
    with pytest.raises(asyncio.CancelledError):
        await task
    await loop._output.close()


@pytest.mark.asyncio
async def test_cancel_current_preserves_disconnect_source():
    """F6: disconnect 来源不被后续 cancel_current() 覆盖；无来源 cancel 不清掉待消费来源。"""
    loop = _make_loop({}, SessionMemory(session_id="default"))
    loop.cancel_current("disconnect")
    assert loop._interrupt_source == "disconnect"
    loop.cancel_current()  # 后续无来源 cancel（如 system tick / 新任务）不清掉 disconnect
    assert loop._interrupt_source == "disconnect"
    loop.cancel_current("websocket")  # 非 disconnect 来源不覆盖已有 disconnect（断开是终态）
    assert loop._interrupt_source == "disconnect"
    await loop._output.close()


@pytest.mark.asyncio
async def test_process_start_clears_stale_generation_ctx():
    """F5: process 开头（resolve 前）清掉上一轮残留 ctx——resolve 期间打断不会把上一轮
    completed generation 重标 cancelled。"""
    conv_x = SessionMemory(session_id="conv_x")
    default = SessionMemory(session_id="default")
    loop = _make_loop({"conv_x": conv_x}, default)
    marks: list[tuple[str, str | None]] = []

    class FakeReg:
        async def register(self, *a, **k):
            pass

        async def mark(self, gid, **k):
            marks.append((gid, k.get("status")))

        async def get(self, gid):
            return None

    loop._generation_registry = FakeReg()

    # 第一轮正常完成 → ctx 残留指向该轮 generation
    await loop.process("first", protocol_version=2)
    await _await_pending(loop)
    gen1 = loop._generation_ctx.generation_id
    assert gen1 and loop._generation_ctx is not None

    # 第二轮：resolver 阻塞期间 ctx 应已清空（fix：process 开头 resolve 前清）
    gate = asyncio.Event()

    async def blocking_resolver(cid, ver):
        await gate.wait()
        return default

    loop._session_resolver = blocking_resolver
    task = asyncio.create_task(loop.process("second", protocol_version=2))
    await asyncio.sleep(0.02)
    assert loop._generation_ctx is None, "resolve 期间 ctx 应已清空（F5）"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(s != "cancelled" for _, s in marks), "不应把上一轮 completed 重标 cancelled"
    await loop._output.close()


@pytest.mark.asyncio
async def test_tts_window_interrupt_keeps_registry_completed():
    """F9: mark(completed) 已在 turn_completed 前落位 → TTS 播放窗口打断不再标 cancelled。"""
    default = SessionMemory(session_id="default")
    loop = _make_loop({}, default)
    marks: list[tuple[str, str | None]] = []

    class FakeReg:
        async def register(self, *a, **k):
            pass

        async def mark(self, gid, **k):
            marks.append((gid, k.get("status")))

        async def get(self, gid):
            return None

    loop._generation_registry = FakeReg()
    # 模拟 TTS 播放窗口：finish_turn → wait_all 阻塞（长回复 5-30s）
    gate = asyncio.Event()

    async def _blocking_wait():
        await gate.wait()

    loop._tts_mgr.wait_all = _blocking_wait

    task = asyncio.create_task(loop.process("你好"))
    # 等生成完成并进入 TTS 窗口（wait_all 阻塞）
    for _ in range(100):
        if getattr(loop, "_generation_completed", False):
            break
        await asyncio.sleep(0.01)
    assert getattr(loop, "_generation_completed", False) is True, "mark(completed) 应在 TTS 窗口前落位"
    assert not task.done(), "process 应阻塞在 TTS 窗口"
    assert marks and marks[-1][1] == "completed"

    # TTS 窗口打断 → registry 保持 completed，不再标 cancelled
    # （process 捕获 CancelledError → _handle_interrupt 后正常返回，不抛）
    loop.cancel_current("websocket")
    await task
    assert not any(s == "cancelled" for _, s in marks), "completed 后打断不应重标 cancelled"
    assert marks and marks[-1][1] == "completed"
    await _await_pending(loop)
    await loop._output.close()
