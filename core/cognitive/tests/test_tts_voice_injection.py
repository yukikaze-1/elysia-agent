"""批次 5 Task 11：TTS 声音每 turn 注入（marker=service）。

覆盖 loop 侧接线（核心接线）：
- turn 启动解析「发起会话」（resolve_session 目标会话，非全局活动会话）的
  service_state.tts_voice → tts_manager.set_voice(voice)
- 发起会话 A tts_voice="vA"、活动会话 B="vB" → A 的 turn 合成用 vA
- v2 缺省 conversationId（→ 活动会话 B）→ 注入 B 的 voice

使用 v2 WebSocketChannel（AsyncMock ws）记录 wire 帧 + 内存 FakeDocumentDBBackend（不碰真实 Mongo）。
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.status.agent_fsm import AgentFSM
from core.monitoring.event_bus import EventBus
from Module.output import OutputManager
from Module.output.websocket import WebSocketChannel
from core.memory.session import SessionMemory
from core.cognitive import CognitiveLoop
from core.cognitive.generation_registry import GenerationRegistry
from core.storage.generation_repo import GenerationRepository
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
from core.adapter.llm_adapter import LLMAdapter

pytestmark = pytest.mark.service

_MODEL = "fake/test"


def _build_loop(session_a: SessionMemory, session_b: SessionMemory):
    """构造最小 CognitiveLoop + v2 WebSocketChannel（AsyncMock ws 记录 wire 帧）。

    resolver 仿批次 3 语义：v2 显式 conversationId → 该会话；None → 活动会话 B。

    Returns:
        (loop, ws)：loop 供 process；ws 供帧断言。
    """
    ws = AsyncMock()
    llm = MagicMock(spec=LLMAdapter)
    llm.model = _MODEL

    async def fake_stream(messages, tools=None, **kw):
        for c in "你好":
            yield {"content": c, "tool_calls": None}

    llm.stream_think = fake_stream

    output = OutputManager()
    output.add_channel(WebSocketChannel(ws, protocol_version=2))
    fsm = AgentFSM()
    bus = EventBus()
    prompt = MagicMock()
    prompt._character = MagicMock(name="TestBot")
    prompt._character.traits = []
    prompt.build_messages = AsyncMock(return_value=[
        {"role": "system", "content": "test"},
        {"role": "user", "content": "hello"},
    ])
    registry = GenerationRegistry(repo=GenerationRepository(db=FakeDocumentDBBackend()))

    async def resolver(conversation_id, protocol_version):
        if conversation_id == "A":
            return session_a
        return session_b  # v2 缺省 → 活动会话 B

    loop = CognitiveLoop(
        llm=llm, fsm=fsm, event_bus=bus, output=output,
        prompt=prompt, session_resolver=resolver,
        generation_registry=registry,
    )
    return loop, ws


async def _await_pending(loop: CognitiveLoop) -> None:
    """等待所有 pending after_response task 完成。"""
    pending = [t for t in loop._pending_after_response if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


class TestTtsVoicePerTurnInjection:
    @pytest.mark.asyncio
    async def test_initiating_session_voice_injected_over_active(self):
        """发起会话 A tts_voice='vA'、活动会话 B='vB' → A 的 turn 注入 vA。"""
        session_a = SessionMemory(session_id="A")
        session_a.service_state.tts_voice = "vA"
        session_b = SessionMemory(session_id="B")
        session_b.service_state.tts_voice = "vB"

        loop, ws = _build_loop(session_a, session_b)
        await loop.process("hello", conversation_id="A", protocol_version=2)
        await _await_pending(loop)
        # 每 turn 注入：合成使用发起会话 A 的声音（非活动会话 B）
        assert loop._tts_mgr._voice == "vA"  # noqa: SLF001 — 测试读取每 turn 可变状态
        await loop._output.close()

    @pytest.mark.asyncio
    async def test_active_session_voice_used_when_default_resolve(self):
        """v2 缺省 conversationId → resolve 到活动会话 B → 注入 B 的 voice。"""
        session_a = SessionMemory(session_id="A")
        session_a.service_state.tts_voice = "vA"
        session_b = SessionMemory(session_id="B")
        session_b.service_state.tts_voice = "vB"

        loop, ws = _build_loop(session_a, session_b)
        await loop.process("hello", protocol_version=2)
        await _await_pending(loop)
        assert loop._tts_mgr._voice == "vB"  # noqa: SLF001 — 缺省走活动会话
        await loop._output.close()

    @pytest.mark.asyncio
    async def test_no_voice_injects_none(self):
        """发起会话未设 tts_voice → set_voice(None)（走后端默认）。"""
        session_a = SessionMemory(session_id="A")
        session_b = SessionMemory(session_id="B")
        session_b.service_state.tts_voice = "vB"

        loop, ws = _build_loop(session_a, session_b)
        await loop.process("hello", conversation_id="A", protocol_version=2)
        await _await_pending(loop)
        assert loop._tts_mgr._voice is None  # noqa: SLF001 — 未设 → 后端默认
        await loop._output.close()
