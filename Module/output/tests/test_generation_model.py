"""批次 5 Task 8：generation.model 快照捕获（D8）——OutputManager 透传 ctx.model_id（marker=unit）。

覆盖 manager 三处广播：
- turn_started → generation.started 带 model（== ctx.model_id，loop 侧为 llm.model 快照）
- turn_failed → generation.failed 带 model（快照）
- turn_cancelled → generation.failed{CANCELLED} 带 model（快照）
- model_id 缺省空串 → wire 缺省省略 model（v2 增量最小化，既有帧不破坏）
"""

import pytest
from unittest.mock import AsyncMock

from Module.output.manager import OutputManager
from Module.output.websocket import WebSocketChannel
from core.cognitive.generation import GenerationContext


def _make_both():
    """v1 + v2 双通道，返回 (mgr, v1_ws, v2_ws)。"""
    mgr = OutputManager()
    v1_ws = AsyncMock()
    v2_ws = AsyncMock()
    mgr.add_channel(WebSocketChannel(v1_ws, protocol_version=1))
    mgr.add_channel(WebSocketChannel(v2_ws, protocol_version=2))
    return mgr, v1_ws, v2_ws


def _ctx(model_id: str = "") -> GenerationContext:
    ctx = GenerationContext(
        generation_id="gen_1", user_message_id="msg_u1", assistant_message_id="msg_a1",
        model_id=model_id,
    )
    ctx.open_segment("public_reply", "text")
    return ctx


@pytest.mark.unit
class TestGenerationModelSnapshot:
    @pytest.mark.asyncio
    async def test_turn_started_carries_snapshot_model(self):
        """turn_started：generation.started 的 model == ctx.model_id（llm.model 快照）。"""
        mgr, v1_ws, v2_ws = _make_both()
        await mgr.turn_started(_ctx(model_id="llm/fake-1"))
        v2_sent = v2_ws.send_json.await_args.args[0]
        assert v2_sent["type"] == "generation.started"
        assert v2_sent["model"] == "llm/fake-1"
        # v1 不接收 generation.*（版本拆分保持）
        assert v1_ws.send_json.await_args_list == []

    @pytest.mark.asyncio
    async def test_turn_failed_carries_snapshot_model(self):
        """failed 事件：generation.failed 帧含 model（快照，非缺省）。"""
        mgr, v1_ws, v2_ws = _make_both()
        await mgr.turn_failed("GENERATION_PROVIDER_UNAVAILABLE", "服务不可用", True,
                              "LLM 调用失败", _ctx(model_id="llm/fake-1"))
        v2_sent = v2_ws.send_json.await_args.args[0]
        assert v2_sent["type"] == "generation.failed"
        assert v2_sent["code"] == "GENERATION_PROVIDER_UNAVAILABLE"
        assert v2_sent["model"] == "llm/fake-1"

    @pytest.mark.asyncio
    async def test_turn_cancelled_carries_snapshot_model(self):
        """打断：generation.failed{CANCELLED} 帧含 model（快照）。"""
        mgr, v1_ws, v2_ws = _make_both()
        await mgr.turn_cancelled(_ctx(model_id="llm/fake-1"))
        v2_sent = v2_ws.send_json.await_args.args[0]
        assert v2_sent["type"] == "generation.failed"
        assert v2_sent["code"] == "GENERATION_CANCELLED"
        assert v2_sent["model"] == "llm/fake-1"

    @pytest.mark.asyncio
    async def test_empty_model_id_omitted_from_wire(self):
        """model_id 缺省空串 → wire 缺省省略 model（v2 增量最小化）。"""
        mgr, v1_ws, v2_ws = _make_both()
        await mgr.turn_started(_ctx())
        v2_sent = v2_ws.send_json.await_args.args[0]
        assert v2_sent["type"] == "generation.started"
        assert "model" not in v2_sent
