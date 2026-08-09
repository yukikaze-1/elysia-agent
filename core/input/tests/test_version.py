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


@pytest.mark.service
class TestProtocolVersionRejection:
    """9B-3 协议版本协商：不支持的显式版本 → HTTP 400 ProtocolError（升级前拒绝，非静默降级 v1）。"""

    async def _assert_rejected_400(self, client, header_value: str) -> dict:
        """断言不支持的版本头 → ws 握手 400（非 101）+ 响应体为 ProtocolError 信封。"""
        import aiohttp
        from frontend_contract.rest import ProtocolError

        with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
            async with client.session.ws_connect(
                client.make_url("/ws"), headers={"X-Protocol-Version": header_value}):
                pass
        assert exc.value.status == 400, \
            f"{header_value!r} 应被显式拒绝（400 而非 101 升级/静默 v1）"
        # 升级失败响应体 → ProtocolError 信封（code=CONTRACT_VALIDATION，明示支持集合）
        # 注意：TestClient.get 内部经 make_url 解析相对路径——须传 "/ws" 而非绝对 URL
        resp = await client.get("/ws", headers={"X-Protocol-Version": header_value})
        assert resp.status == 400
        env = ProtocolError.model_validate(
            json.loads((await resp.read()).decode())).wire_dict()
        assert env["code"] == "CONTRACT_VALIDATION"
        assert env["retryable"] is False
        assert "v1" in env["message"] and "v2" in env["message"]  # message 明示支持集合
        return env

    @pytest.mark.asyncio
    async def test_v2dot0_handshake_rejected_400(self, aiohttp_client):
        """X-Protocol-Version: "2.0"（展示版本，非握手可用值）→ HTTP 400 + ProtocolError。"""
        from core.input.server import create_app

        agent = _make_agent()
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            await self._assert_rejected_400(client, "2.0")
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_v3_handshake_rejected_400(self, aiohttp_client):
        """X-Protocol-Version: "3"（未来版本）→ HTTP 400。"""
        from core.input.server import create_app

        agent = _make_agent()
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            await self._assert_rejected_400(client, "3")
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_garbage_handshake_rejected_400(self, aiohttp_client):
        """乱拼写版本头 → HTTP 400（不静默降级 v1）。"""
        from core.input.server import create_app

        agent = _make_agent()
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            await self._assert_rejected_400(client, "latest")
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_explicit_v1_handshake_v1(self, aiohttp_client):
        """X-Protocol-Version: "1" → 走 v1 legacy 路径（capabilities，非 server_info）。"""
        from core.input.server import create_app

        agent = _make_agent()
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            async with client.session.ws_connect(
                client.make_url("/ws"), headers={"X-Protocol-Version": "1"},
            ) as ws:
                caps = await _read_until_type(ws, "capabilities")
                info = await _read_until_type(ws, "server_info")
            assert caps is not None, '"1" 头应走 v1（收到 capabilities）'
            assert info is None, '"1" 头不应收到 server_info'
            assert caps["services"]["llm"]["model"] == agent.llm.model
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_default_handshake_v1(self, aiohttp_client):
        """缺省版本头 → v1 legacy 路径（capabilities，既有回归）。"""
        from core.input.server import create_app

        agent = _make_agent()
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            async with client.session.ws_connect(client.make_url("/ws")) as ws:
                caps = await _read_until_type(ws, "capabilities")
                info = await _read_until_type(ws, "server_info")
            assert caps is not None, "缺省头应走 v1（收到 capabilities）"
            assert info is None, "缺省头不应收到 server_info"
        finally:
            await agent.stop()


@pytest.mark.unit
def test_get_build_sha_failure_cached(monkeypatch):
    """git 失败 → 连续两次调用 subprocess.run 只跑一次。"""
    import core.version as v
    calls = []
    def _fake_run(*a, **k):
        calls.append(1)
        raise OSError("no git")
    monkeypatch.setattr(v.subprocess, "run", _fake_run)
    monkeypatch.setattr(v, "_build_sha", None, raising=False)   # 红态无该属性
    monkeypatch.setattr(v, "_build_sha_tried", False, raising=False)  # 红态无该属性
    assert v.get_build_sha() is None
    assert v.get_build_sha() is None
    assert len(calls) == 1
