"""D5：空响应兜底——v2 发 segment.completed 非 message 帧（spec §3.5）。"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.cognitive.generation import GenerationContext
from core.cognitive.loop import CognitiveLoop
from core.schema import GenerationId, MessageId, SessionId
from frontend_contract.ws_message import MessageChannel, SegmentKind

pytestmark = pytest.mark.unit


def _make_loop():
    loop = CognitiveLoop(
        llm=MagicMock(), fsm=MagicMock(), event_bus=MagicMock(),
        output=MagicMock(), prompt=MagicMock(), session_resolver=MagicMock(),
    )
    loop._generation_ctx = GenerationContext(
        generation_id=GenerationId("gen_1"), user_message_id=MessageId("msg_1"),
        assistant_message_id=MessageId("msg_a"), conversation_id=SessionId("default"),
        protocol_version=2,
    )
    loop._output.segment_started = AsyncMock()
    loop._output.segment_completed = AsyncMock()
    loop._output.typing = AsyncMock()
    return loop


@pytest.mark.asyncio
async def test_v2_fallback_emits_segment_not_message():
    """v2 空响应兜底 → segment_started + segment_completed，不发 MessageObj。"""
    loop = _make_loop()
    sent = []
    loop._output.send = AsyncMock(side_effect=lambda m: sent.append(m))
    await loop._finalize_turn(
        "你好", [], "", False, "用户", 1, [],
        "你好",
        conversation_id="default",
        session=MagicMock(),
    )
    loop._output.segment_completed.assert_awaited()
    assert sent == []  # 未发 v1 message 帧


@pytest.mark.asyncio
async def test_v2_fallback_completes_existing_segment():
    """F4：已有活动段（delta 已开段无 SENTENCE）→ 直接完成现有段，不重复 open_segment。"""
    loop = _make_loop()
    loop._generation_ctx.open_segment(MessageChannel.PUBLIC_REPLY, SegmentKind.TEXT)   # 模拟 delta 已开段
    loop._output.segment_started = AsyncMock()
    loop._output.segment_completed = AsyncMock()
    loop._output.send = AsyncMock()
    await loop._finalize_turn(
        "你好", [], "", False, "用户", 1, [],
        "你好",
        conversation_id="default",
        session=MagicMock(),
    )
    loop._output.segment_started.assert_not_awaited()          # 不重复 open
    assert loop._generation_ctx.current_segment_id is None     # 段已关闭


@pytest.mark.asyncio
async def test_v1_fallback_still_emits_message():
    """v1 保持 MessageObj 行为。"""
    loop = _make_loop()
    loop._generation_ctx = GenerationContext(
        generation_id=GenerationId("gen_1"), user_message_id=MessageId("msg_1"),
        assistant_message_id=MessageId("msg_a"), conversation_id=SessionId("default"),
        protocol_version=1,
    )
    sent = []
    loop._output.send = AsyncMock(side_effect=lambda m: sent.append(m))
    await loop._finalize_turn(
        "你好", [], "", False, "用户", 1, [],
        "你好",
        conversation_id="default",
        session=MagicMock(),
    )
    assert len(sent) == 1  # v1 仍发 MessageObj
