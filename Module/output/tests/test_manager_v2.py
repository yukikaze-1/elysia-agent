"""批次 2 Task 5：OutputManager 版本感知扇出——统一回合事件按 channel.protocol_version 分发（marker=unit/service）。"""

import pytest
from unittest.mock import AsyncMock

from Module.output.manager import OutputManager
from Module.output.websocket import WebSocketChannel
from core.cognitive.generation import GenerationContext


def _make_channels():
    """构造 v1 + v2 两个 WebSocketChannel（AsyncMock ws），返回 (mgr, v1_ws, v2_ws)。"""
    mgr = OutputManager()
    v1_ws = AsyncMock()
    v2_ws = AsyncMock()
    mgr.add_channel(WebSocketChannel(v1_ws, protocol_version=1))
    mgr.add_channel(WebSocketChannel(v2_ws, protocol_version=2))
    return mgr, v1_ws, v2_ws


def _ctx() -> GenerationContext:
    ctx = GenerationContext(
        generation_id="gen_1", user_message_id="msg_u1", assistant_message_id="msg_a1",
    )
    ctx.open_segment("public_reply", "text")
    return ctx


@pytest.mark.unit
class TestVersionFanOut:
    @pytest.mark.asyncio
    async def test_text_delta_v1_delta_v2_generation_delta(self):
        mgr, v1_ws, v2_ws = _make_channels()
        await mgr.text_delta("你", _ctx())
        # v1 → delta；v2 → generation.delta
        assert v1_ws.send_json.await_args.args[0]["type"] == "delta"
        v2_sent = v2_ws.send_json.await_args.args[0]
        assert v2_sent["type"] == "generation.delta"
        assert v2_sent["generationId"] == "gen_1"
        assert v2_sent["deltaSequence"] == 0

    @pytest.mark.asyncio
    async def test_segment_completed_tts_off_v1_send_empty_v2_segment(self):
        mgr, v1_ws, v2_ws = _make_channels()
        ctx = _ctx()
        await mgr.segment_completed("第一句。", ctx, tts_active=False)
        assert v1_ws.send_json.await_args.args[0] == {"type": "message", "content": ""}
        v2_sent = v2_ws.send_json.await_args.args[0]
        assert v2_sent["type"] == "generation.segment.completed"
        assert v2_sent["content"] == "第一句。"

    @pytest.mark.asyncio
    async def test_segment_completed_tts_on_v1_noop_v2_segment(self):
        mgr, v1_ws, v2_ws = _make_channels()
        await mgr.segment_completed("第一句。", _ctx(), tts_active=True)
        # v1：TTS 开时不发 send("")（现状 loop.py:1015-1019）
        v1_sent = [c.args[0] for c in v1_ws.send_json.await_args_list]
        assert all(s.get("type") != "message" for s in v1_sent)
        # v2：恒发 segment.completed
        assert v2_ws.send_json.await_args.args[0]["type"] == "generation.segment.completed"

    @pytest.mark.asyncio
    async def test_inner_voice_v1_command_v2_segment(self):
        mgr, v1_ws, v2_ws = _make_channels()
        await mgr.inner_voice("我想想。", _ctx())
        # v1 → command inner_voice
        v1_sent = v1_ws.send_json.await_args.args[0]
        assert v1_sent["type"] == "command" and v1_sent["command"] == "inner_voice"
        # v2 → generation.segment.started + completed（inner_voice 通道，含文本）
        v2_sent = [c.args[0] for c in v2_ws.send_json.await_args_list]
        assert v2_sent[0]["type"] == "generation.segment.started"
        assert v2_sent[0]["channel"] == "inner_voice"
        assert v2_sent[1]["type"] == "generation.segment.completed"
        assert v2_sent[1]["content"] == "我想想。"

    @pytest.mark.asyncio
    async def test_mood_v1_expression_v2_suppressed(self):
        mgr, v1_ws, v2_ws = _make_channels()
        await mgr.mood("开心", 0.8)
        assert v1_ws.send_json.await_args.args[0]["type"] == "expression"
        # v2 不发 expression（mood 由 turn_completed 落位）
        v2_sent = [c.args[0] for c in v2_ws.send_json.await_args_list]
        assert all(s.get("type") != "expression" for s in v2_sent)

    @pytest.mark.asyncio
    async def test_turn_completed_v1_noop_v2_completed(self):
        mgr, v1_ws, v2_ws = _make_channels()
        from frontend_contract import TokenUsage
        usage = TokenUsage(rounds=[], turn_total=0)
        await mgr.turn_completed("stop", usage, "fake/test", "开心", _ctx())
        # v1 NO-OP（token_usage 由 TokenTracker.record 实时广播，防双发）
        assert v1_ws.send_json.await_args_list == []
        v2_sent = v2_ws.send_json.await_args.args[0]
        assert v2_sent["type"] == "generation.completed"
        assert v2_sent["mood"] == "开心"

    @pytest.mark.asyncio
    async def test_turn_failed_v1_delta_error_v2_failed(self):
        mgr, v1_ws, v2_ws = _make_channels()
        await mgr.turn_failed("GENERATION_PROVIDER_UNAVAILABLE", "服务不可用", True,
                             "LLM 调用失败", _ctx())
        # v1 → 逐字节复刻现状：delta("\n❌ LLM 调用失败: ...") + message("")
        v1_sent = [c.args[0] for c in v1_ws.send_json.await_args_list]
        assert v1_sent[0]["type"] == "delta"
        assert v1_sent[0]["content"].startswith("\n❌ LLM 调用失败")
        assert v1_sent[1] == {"type": "message", "content": ""}
        # v2 → generation.failed
        v2_sent = v2_ws.send_json.await_args.args[0]
        assert v2_sent["type"] == "generation.failed"
        assert v2_sent["code"] == "GENERATION_PROVIDER_UNAVAILABLE"
