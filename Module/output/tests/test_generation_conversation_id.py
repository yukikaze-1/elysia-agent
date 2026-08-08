"""批次 3 Task 10：Output 层 conversation_id/conversationUsage 贯通（marker=unit）。

- WebSocketChannel 6 类 generation.* wire 方法：传入 conversation_id 填 conversationId。
- on_generation_completed：传入 conversation_usage 填 conversationUsage。
- OutputManager 8 个统一回合事件：从 GenerationContext 透传 conversation_id 到 v2 wire。
- v1 channel 不因新参数改变 wire（版本拆分保持 v1 不变）。
"""

import pytest
from unittest.mock import AsyncMock

from Module.output.manager import OutputManager
from Module.output.websocket import WebSocketChannel
from core.cognitive.generation import GenerationContext
from frontend_contract.ws_message import MessageChannel, SegmentKind


def _make_ch(protocol_version: int = 2):
    """构造单通道 WebSocketChannel（AsyncMock ws）。"""
    ws = AsyncMock()
    ch = WebSocketChannel(ws, protocol_version=protocol_version)
    return ch, ws


def _ctx(conversation_id: str | None = "conv_x") -> GenerationContext:
    ctx = GenerationContext(
        generation_id="gen_1", user_message_id="msg_u1", assistant_message_id="msg_a1",
        conversation_id=conversation_id,
    )
    ctx.open_segment(MessageChannel.PUBLIC_REPLY, SegmentKind.TEXT)
    return ctx


# ============================================================
# WebSocketChannel wire：6 类 generation.* 填 conversationId
# ============================================================

@pytest.mark.unit
class TestWireConversationId:
    @pytest.mark.asyncio
    async def test_on_generation_started_wire_conversation_id(self):
        ch, ws = _make_ch()
        await ch.on_generation_started(
            generation_id="gen_1", message_id="msg_a1", conversation_id="conv_x",
            request_id="req_1", model="fake/test",
        )
        assert ws.send_json.await_args.args[0]["conversationId"] == "conv_x"

    @pytest.mark.asyncio
    async def test_on_generation_segment_started_wire_conversation_id(self):
        ch, ws = _make_ch()
        await ch.on_generation_segment_started(
            generation_id="gen_1", segment_id="seg_1", segment_index=0,
            channel="public_reply", kind="text", conversation_id="conv_x",
        )
        sent = ws.send_json.await_args.args[0]
        assert sent["type"] == "generation.segment.started"
        assert sent["conversationId"] == "conv_x"

    @pytest.mark.asyncio
    async def test_on_generation_delta_wire_conversation_id(self):
        ch, ws = _make_ch()
        await ch.on_generation_delta(
            generation_id="gen_1", segment_id="seg_1", segment_index=0,
            delta_sequence=0, delta="你", conversation_id="conv_x",
        )
        sent = ws.send_json.await_args.args[0]
        assert sent["type"] == "generation.delta"
        assert sent["conversationId"] == "conv_x"

    @pytest.mark.asyncio
    async def test_on_generation_segment_completed_wire_conversation_id(self):
        ch, ws = _make_ch()
        await ch.on_generation_segment_completed(
            generation_id="gen_1", segment_id="seg_1", segment_index=0,
            channel="public_reply", kind="text", content="第一句。",
            conversation_id="conv_x",
        )
        sent = ws.send_json.await_args.args[0]
        assert sent["type"] == "generation.segment.completed"
        assert sent["conversationId"] == "conv_x"

    @pytest.mark.asyncio
    async def test_on_generation_completed_wire_conversation_id(self):
        ch, ws = _make_ch()
        from frontend_contract import TokenUsage
        usage = TokenUsage(rounds=[], turn_total=0)
        await ch.on_generation_completed(
            generation_id="gen_1", message_id="msg_a1", finish_reason="stop",
            usage=usage, model="fake/test", mood="开心", conversation_id="conv_x",
        )
        assert ws.send_json.await_args.args[0]["conversationId"] == "conv_x"

    @pytest.mark.asyncio
    async def test_on_generation_failed_wire_conversation_id(self):
        ch, ws = _make_ch()
        await ch.on_generation_failed(
            generation_id="gen_1", message_id="msg_a1",
            code="GENERATION_CANCELLED", message="已取消", retryable=False,
            conversation_id="conv_x",
        )
        sent = ws.send_json.await_args.args[0]
        assert sent["type"] == "generation.failed"
        assert sent["conversationId"] == "conv_x"

    @pytest.mark.asyncio
    async def test_on_generation_completed_wire_conversation_usage(self):
        ch, ws = _make_ch()
        from frontend_contract import TokenUsage
        usage = TokenUsage(rounds=[], turn_total=0)
        await ch.on_generation_completed(
            generation_id="gen_1", message_id="msg_a1", finish_reason="stop",
            usage=usage, model="fake/test", mood="开心",
            conversation_usage={"total": 100, "by_model": {"fake/test": 100}},
        )
        sent = ws.send_json.await_args.args[0]
        assert sent["type"] == "generation.completed"
        assert sent["conversationUsage"] == {"total": 100, "byModel": {"fake/test": 100}}

    @pytest.mark.asyncio
    async def test_on_generation_completed_conversation_usage_none_omitted(self):
        ch, ws = _make_ch()
        from frontend_contract import TokenUsage
        usage = TokenUsage(rounds=[], turn_total=0)
        await ch.on_generation_completed(
            generation_id="gen_1", message_id="msg_a1", finish_reason="stop",
            usage=usage, model="fake/test", mood="开心",
            conversation_id="conv_x", conversation_usage=None,
        )
        sent = ws.send_json.await_args.args[0]
        assert sent["conversationId"] == "conv_x"
        assert "conversationUsage" not in sent


# ============================================================
# OutputManager：8 个统一回合事件透传 conversation_id（从 ctx 取）
# ============================================================

def _make_both():
    """v1 + v2 双通道，返回 (mgr, v1_ws, v2_ws)。"""
    mgr = OutputManager()
    v1_ws = AsyncMock()
    v2_ws = AsyncMock()
    mgr.add_channel(WebSocketChannel(v1_ws, protocol_version=1))
    mgr.add_channel(WebSocketChannel(v2_ws, protocol_version=2))
    return mgr, v1_ws, v2_ws


@pytest.mark.unit
class TestManagerPassthrough:
    @pytest.mark.asyncio
    async def test_turn_started_passes_conversation_id(self):
        mgr, v1_ws, v2_ws = _make_both()
        await mgr.turn_started(_ctx())
        v2_sent = v2_ws.send_json.await_args.args[0]
        assert v2_sent["type"] == "generation.started"
        assert v2_sent["conversationId"] == "conv_x"
        assert v1_ws.send_json.await_args_list == []

    @pytest.mark.asyncio
    async def test_segment_started_passes_conversation_id(self):
        mgr, v1_ws, v2_ws = _make_both()
        await mgr.segment_started(_ctx())
        v2_sent = v2_ws.send_json.await_args.args[0]
        assert v2_sent["type"] == "generation.segment.started"
        assert v2_sent["conversationId"] == "conv_x"
        assert v1_ws.send_json.await_args_list == []

    @pytest.mark.asyncio
    async def test_text_delta_passes_conversation_id(self):
        mgr, v1_ws, v2_ws = _make_both()
        await mgr.text_delta("你", _ctx())
        v2_sent = v2_ws.send_json.await_args.args[0]
        assert v2_sent["type"] == "generation.delta"
        assert v2_sent["conversationId"] == "conv_x"
        # v1 仍发 delta（wire 不变）
        assert v1_ws.send_json.await_args.args[0]["type"] == "delta"

    @pytest.mark.asyncio
    async def test_segment_completed_passes_conversation_id(self):
        mgr, v1_ws, v2_ws = _make_both()
        await mgr.segment_completed("第一句。", _ctx(), tts_active=False)
        v2_sent = v2_ws.send_json.await_args.args[0]
        assert v2_sent["type"] == "generation.segment.completed"
        assert v2_sent["conversationId"] == "conv_x"

    @pytest.mark.asyncio
    async def test_inner_voice_passes_conversation_id(self):
        mgr, v1_ws, v2_ws = _make_both()
        await mgr.inner_voice("我想想。", _ctx())
        v2_sent = [c.args[0] for c in v2_ws.send_json.await_args_list]
        assert [s["type"] for s in v2_sent] == [
            "generation.segment.started", "generation.segment.completed",
        ]
        assert v2_sent[0]["conversationId"] == "conv_x"
        assert v2_sent[1]["conversationId"] == "conv_x"

    @pytest.mark.asyncio
    async def test_turn_completed_passes_conversation_id_and_usage(self):
        mgr, v1_ws, v2_ws = _make_both()
        from frontend_contract import TokenUsage
        usage = TokenUsage(rounds=[], turn_total=0)
        await mgr.turn_completed(
            "stop", usage, "fake/test", "开心", _ctx(),
            conversation_id="conv_x",
            conversation_usage={"total": 100, "by_model": {"fake/test": 100}},
        )
        v2_sent = v2_ws.send_json.await_args.args[0]
        assert v2_sent["type"] == "generation.completed"
        assert v2_sent["conversationId"] == "conv_x"
        assert v2_sent["conversationUsage"] == {
            "total": 100, "byModel": {"fake/test": 100},
        }
        # v1 NO-OP 不变
        assert v1_ws.send_json.await_args_list == []

    @pytest.mark.asyncio
    async def test_turn_failed_passes_conversation_id(self):
        mgr, v1_ws, v2_ws = _make_both()
        await mgr.turn_failed("GENERATION_PROVIDER_UNAVAILABLE", "服务不可用", True,
                              "LLM 调用失败", _ctx())
        v2_sent = v2_ws.send_json.await_args.args[0]
        assert v2_sent["type"] == "generation.failed"
        assert v2_sent["conversationId"] == "conv_x"

    @pytest.mark.asyncio
    async def test_turn_cancelled_passes_conversation_id(self):
        mgr, v1_ws, v2_ws = _make_both()
        await mgr.turn_cancelled(_ctx())
        v2_sent = v2_ws.send_json.await_args.args[0]
        assert v2_sent["type"] == "generation.failed"
        assert v2_sent["code"] == "GENERATION_CANCELLED"
        assert v2_sent["conversationId"] == "conv_x"
        # v1 不发（现状打断无 ❌ delta）
        assert v1_ws.send_json.await_args_list == []

    @pytest.mark.asyncio
    async def test_v1_channel_unchanged_by_new_params(self):
        """v1 通道不因 conversation_id/usage 新参数改变 wire——版本拆分不泄漏 v2 字段。"""
        mgr = OutputManager()
        v1_ws = AsyncMock()
        mgr.add_channel(WebSocketChannel(v1_ws, protocol_version=1))
        # 全部 8 个统一方法以带 conversation_id 的 ctx 调用
        ctx = _ctx()
        await mgr.turn_started(ctx)
        await mgr.segment_started(ctx)
        await mgr.text_delta("你", ctx)
        await mgr.segment_completed("第一句。", ctx, tts_active=False)
        await mgr.inner_voice("我想想。", ctx)
        await mgr.turn_completed(
            "stop", __import__("frontend_contract").TokenUsage(rounds=[], turn_total=0),
            "fake/test", "开心", ctx,
            conversation_id="conv_x",
            conversation_usage={"total": 100, "by_model": {"fake/test": 100}},
        )
        await mgr.turn_failed("X", "m", True, "标签", ctx)
        await mgr.turn_cancelled(ctx)
        v1_sent = [c.args[0] for c in v1_ws.send_json.await_args_list]
        for s in v1_sent:
            assert "conversationId" not in s
            assert "conversationUsage" not in s
