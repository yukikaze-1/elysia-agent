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

    # conv_a 的 save 阻塞在 gate——保证 after_response#1 的 save 在
    # self._session 已被第二轮回话覆盖之后才真正执行。
    gate = asyncio.Event()
    orig_save_a = conv_a.save

    async def gated_save():
        await gate.wait()
        return await orig_save_a()

    conv_a.save = gated_save

    # 第一轮：conversation_id="conv_a" → after_response#1 阻塞在 conv_a.save
    await loop.process("msg1", conversation_id="conv_a", protocol_version=2)
    # 第二轮：conversation_id="conv_b" → self._session 已被换绑为 conv_b
    await loop.process("msg2", conversation_id="conv_b", protocol_version=2)
    # 此刻释放 gate——after_response#1 的 save 执行。正确实现写 conv_a；
    # 错误实现（读 self._session=conv_b）会写 conv_b。
    gate.set()
    await _await_pending(loop)

    # after_response#1 落 conv_a（发起会话），after_response#2 落 conv_b
    assert conv_a_repo.save.await_count == 1
    assert conv_b_repo.save.await_count >= 1
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
