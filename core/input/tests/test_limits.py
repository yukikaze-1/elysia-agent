"""批次 1 限制保护测试：请求体超限 413 REQUEST_TOO_LARGE + 并发 WS 超限 503 CONNECTION_LIMITED（marker=service）。

批次 8c（F14）：并发握手硬上限——闭包计数 check+自增原子，非 TOCTOU 预检。
"""

import asyncio
import json

import pytest

from frontend_contract.rest import ProtocolError
from core.input.tests.test_server import _make_agent


async def _open_ws(client):
    """打开 WS 连接（成功返回 ws，超限 503 返回 None）。"""
    import aiohttp
    try:
        return await client.session.ws_connect(client.make_url("/ws"))
    except aiohttp.WSServerHandshakeError:
        return None


@pytest.mark.service
class TestRequestSizeLimit:
    @pytest.mark.asyncio
    async def test_oversize_json_body_413(self, aiohttp_client):
        """超限 body → 413 REQUEST_TOO_LARGE（统一信封）。"""
        from unittest.mock import MagicMock
        from core.agent import RunState
        from core.input.server import create_app

        agent = MagicMock()
        agent.output = MagicMock()
        agent.bus = MagicMock()
        agent._state = RunState.RUNNING
        agent.process_message = None  # /api/chat 需要，但不会走到（413 拦截）
        app = create_app(agent, serve_static=False, max_request_bytes=100)
        client = await aiohttp_client(app)

        big_body = json.dumps({"content": "x" * 500})
        resp = await client.post("/api/chat", data=big_body, headers={"Content-Type": "application/json"})
        assert resp.status == 413
        env = ProtocolError.model_validate(json.loads((await resp.read()).decode())).wire_dict()
        assert env["code"] == "REQUEST_TOO_LARGE"

    @pytest.mark.asyncio
    async def test_small_body_passes(self, aiohttp_client):
        """小 body 不受影响（通过 client_max_size 校验）。"""
        from unittest.mock import MagicMock
        from core.agent import RunState
        from core.input.server import create_app

        agent = MagicMock()
        agent.output = MagicMock()
        agent.bus = MagicMock()
        agent._state = RunState.RUNNING
        agent.process_message = None
        app = create_app(agent, serve_static=False, max_request_bytes=1024)
        client = await aiohttp_client(app)

        small = json.dumps({"content": "hi"})
        resp = await client.post("/api/chat", data=small, headers={"Content-Type": "application/json"})
        # 通过大小校验 → 走到 handler（无 process_message → 400/500，但非 413）
        assert resp.status != 413


@pytest.mark.service
class TestWsConcurrencyLimit:
    @pytest.mark.asyncio
    async def test_ws_over_limit_rejected_503(self, aiohttp_client):
        """并发 WS 超 max_ws_connections → 503 CONNECTION_LIMITED（握手拒绝）。"""
        from core.input.server import create_app

        agent = _make_agent()
        await agent.start()
        app = create_app(agent, serve_static=False, max_ws_connections=1)
        client = await aiohttp_client(app)
        try:
            # 第一个连接占用唯一槽位
            ws1 = await client.session.ws_connect(client.make_url("/ws"))
            assert ws1.closed is False
            # 第二个连接超限 → 拒绝升级（503 CONNECTION_LIMITED）
            import aiohttp
            with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
                async with client.session.ws_connect(client.make_url("/ws")):
                    pass
            assert exc.value.status == 503
            await ws1.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_ws_concurrent_handshake_hard_limit(self, aiohttp_client):
        """max=1 下并发两个握手 → 恰一个成功（硬上限非 TOCTOU 预检）。"""
        from core.input.server import create_app

        agent = _make_agent()
        await agent.start()
        app = create_app(agent, serve_static=False, max_ws_connections=1)
        client = await aiohttp_client(app)
        try:
            # 两连接保持打开到两个握手都结束（避免首个 close 释放槽位引入竞态）
            ws1, ws2 = await asyncio.gather(_open_ws(client), _open_ws(client))
            ok = sum(1 for ws in (ws1, ws2) if ws is not None)
            assert ok == 1
            for ws in (ws1, ws2):
                if ws is not None:
                    await ws.close()
        finally:
            await agent.stop()
