"""批次 1 版本协商测试：X-Protocol-Version 握手 → v2 收 server_info（无 capabilities），v1 收 capabilities（marker=service）。"""

import asyncio
import json

import pytest
from aiohttp import WSMsgType

from core.input.tests.test_server import _make_agent


async def _read_until_type(ws, want: str, max_frames: int = 8) -> dict | None:
    """读取帧直到指定 type；未出现则读完 max_frames 或接收超时返回 None（负向断言用）。"""
    for _ in range(max_frames):
        try:
            msg = await ws.receive(timeout=2.0)
        except asyncio.TimeoutError:
            return None
        if msg.type == WSMsgType.TEXT:
            data = json.loads(msg.data)
            if data.get("type") == want:
                return data
    return None


@pytest.mark.service
class TestVersionNegotiation:
    @pytest.mark.asyncio
    async def test_v2_handshake_gets_server_info_no_capabilities(self, aiohttp_client):
        """带 X-Protocol-Version: 2 握手 → 收到 server_info 且无 capabilities。"""
        from core.input.server import create_app

        agent = _make_agent()
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            async with client.session.ws_connect(
                client.make_url("/ws"), headers={"X-Protocol-Version": "2"},
            ) as ws:
                info = await _read_until_type(ws, "server_info")
                caps = await _read_until_type(ws, "capabilities")
            assert info is not None, "v2 握手未收到 server_info"
            assert caps is None, "v2 握手不应收到 capabilities（server_info 取代）"
            assert info["protocol_version"] == "2.0"
            assert info["server_version"]
            # features 如实反映能力：structured_generation 批次 2、multi_conversation 批次 3、
            # desktop_runtime 批次 4、dictation/voice_recording/voice_realtime 批次 6、
            # media_upload 批次 7 已实现（全 true）
            assert info["features"] == {
                "structured_generation": True,
                "multi_conversation": True,
                "desktop_runtime": True,
                "dictation": True,
                "voice_recording": True,
                "voice_realtime": True,
                "media_upload": True,
            }
            # services 复用 per-service 形状
            assert "llm" in info["services"]
            assert info["services"]["llm"]["model"] == agent.llm.model
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_v1_handshake_gets_capabilities_no_server_info(self, aiohttp_client):
        """不带版本头（v1）→ 收到 capabilities 且无 server_info。"""
        from core.input.server import create_app

        agent = _make_agent()
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            async with client.session.ws_connect(client.make_url("/ws")) as ws:
                caps = await _read_until_type(ws, "capabilities")
                info = await _read_until_type(ws, "server_info")
            assert caps is not None, "v1 握手应收到 capabilities"
            assert info is None, "v1 握手不应收到 server_info"
            assert caps["services"]["llm"]["model"] == agent.llm.model
        finally:
            await agent.stop()
