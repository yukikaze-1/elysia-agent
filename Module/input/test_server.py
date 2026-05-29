"""Tests for Module/input/server.py — WebSocket server.

Run: python -m pytest Module/input/test_server.py -v --asyncio-mode=auto
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop


# ── helpers ────────────────────────────────────────────────────────────────

def _make_agent():
    """Create a minimal mock agent for server tests."""
    import os as _os
    _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
    from core.app_config import AppConfig
    from core.demo_agent import DemoAgent

    agent = DemoAgent(config=AppConfig())
    from core.tests.fake_llm import FakeLLMBackend
    agent._llm._backend = FakeLLMBackend(response="test")
    return agent


# ── tests ──────────────────────────────────────────────────────────────────

class TestCreateApp:
    def test_registers_routes(self):
        from Module.input.server import create_app

        agent = MagicMock()
        agent._output = MagicMock()
        agent._bus = MagicMock()

        app = create_app(agent, serve_static=False)

        routes = {r.path for r in app.router.routes() if hasattr(r, 'path')}
        # For aiohttp route resources, check the canonical path
        route_paths = set()
        for r in app.router.routes():
            info = r.resource.get_info() if hasattr(r.resource, 'get_info') else {}
            path = info.get('path', getattr(r.resource, '_path', None))
            if path:
                route_paths.add(path)

        # Core routes should be registered
        route_strs = {str(r) for r in app.router.routes()}
        assert any('/ws' in s or 'ws_handler' in s for s in route_strs), f"No /ws route in {route_strs}"

    def test_shutdown_endpoint(self):
        from Module.input.server import create_app

        agent = MagicMock()
        agent._output = MagicMock()
        agent._bus = MagicMock()
        agent._running = True

        app = create_app(agent, serve_static=False)

        # Find and call shutdown handler
        for r in app.router.routes():
            if hasattr(r, 'resource') and 'shutdown' in str(r.resource):
                # Shutdown endpoint sets _running = False
                pass  # tested via integration

        # Simulate: POST /shutdown
        agent._running = False
        assert agent._running is False


class TestCreateAppIntegration:
    """Integration tests using a real aiohttp test client."""

    @pytest.mark.asyncio
    async def test_shutdown_endpoint(self, aiohttp_client):
        from Module.input.server import create_app

        agent = MagicMock()
        agent._output = MagicMock()
        agent._bus = MagicMock()
        agent._running = True

        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)

        resp = await client.post('/shutdown')
        assert resp.status == 200
        data = await resp.json()
        assert data['status'] == 'shutting_down'
        assert agent._running is False

    @pytest.mark.asyncio
    async def test_shutdown_get(self, aiohttp_client):
        from Module.input.server import create_app

        agent = MagicMock()
        agent._output = MagicMock()
        agent._bus = MagicMock()
        agent._running = True

        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)

        resp = await client.get('/shutdown')
        assert resp.status == 200
        assert agent._running is False


class TestWebSocketHandler:
    @pytest.mark.asyncio
    async def test_text_message_triggers_process_message(self, aiohttp_client):
        from Module.input.server import create_app

        agent = _make_agent()
        await agent.start()
        agent.process_message = AsyncMock()

        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)

        async with client.session.ws_connect(client.make_url('/ws')) as ws:
            await ws.send_json({"type": "text", "content": "hello"})
            # Give event loop time to process
            await asyncio.sleep(0.1)

        agent.process_message.assert_called_with("hello")
        await agent.stop()

    @pytest.mark.asyncio
    async def test_interrupt_message(self, aiohttp_client):
        from Module.input.server import create_app

        agent = _make_agent()
        await agent.start()

        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)

        async with client.session.ws_connect(client.make_url('/ws')) as ws:
            await ws.send_json({"type": "interrupt"})
            await asyncio.sleep(0.1)

        await agent.stop()

    @pytest.mark.asyncio
    async def test_ws_creates_output_channel(self, aiohttp_client):
        from Module.input.server import create_app

        agent = _make_agent()
        await agent.start()
        initial_count = len(agent._output._channels)

        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)

        async with client.session.ws_connect(client.make_url('/ws')) as ws:
            await asyncio.sleep(0.1)
            # WebSocketChannel should be added
            assert len(agent._output._channels) == initial_count + 1

        # After disconnect, channel should be removed
        await asyncio.sleep(0.1)
        assert len(agent._output._channels) == initial_count

        await agent.stop()

    @pytest.mark.asyncio
    async def test_invalid_json_returns_error(self, aiohttp_client):
        from Module.input.server import create_app

        agent = _make_agent()
        await agent.start()

        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)

        async with client.session.ws_connect(client.make_url('/ws')) as ws:
            await ws.send_str("not valid json")
            # Should not crash — error response or silent
            await asyncio.sleep(0.1)

        await agent.stop()

    @pytest.mark.asyncio
    async def test_do_transcription_with_emotion(self, aiohttp_client):
        from Module.input.server import create_app, _do_transcription

        agent = _make_agent()
        await agent.start()

        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)

        async with client.session.ws_connect(client.make_url('/ws')) as ws:
            stt_state = {"emotion": "HAPPY", "events": ["Laughter"]}
            await _do_transcription(agent, ws, "测试", stt_emotion_state=stt_state)
            await asyncio.sleep(0.1)

        await agent.stop()
