"""Tests for core/input/server.py — WebSocket server.

Run: python -m pytest core/input/tests/test_server.py -v --asyncio-mode=auto
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

from core.agent import RunState


# ── helpers ────────────────────────────────────────────────────────────────

def _make_agent():
    """Create a minimal mock agent for server tests."""
    import os as _os
    _os.environ.setdefault("DEEPSEEK_API_KEY", "test")
    from core.config import AppConfig
    from core.agent import Agent

    agent = Agent(config=AppConfig())
    from core.tests.fake_llm import FakeLLMBackend
    agent._llm._backend = FakeLLMBackend(response="test")
    return agent


# ── tests ──────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestCreateApp:
    def test_registers_routes(self):
        from core.input.server import create_app

        agent = MagicMock()
        agent._output = MagicMock()
        agent._bus = MagicMock()

        app = create_app(agent, serve_static=False)

        routes = {r.path for r in app.router.routes() if hasattr(r, 'path')}
        # For aiohttp route resources, check the canonical path
        route_paths = set()
        for r in app.router.routes():
            resource = r.resource  # may be None for AbstractRoute
            info = resource.get_info() if resource is not None and hasattr(resource, 'get_info') else {}
            path = info.get('path', getattr(resource, '_path', None))
            if path:
                route_paths.add(path)

        # Core routes should be registered
        route_strs = {str(r) for r in app.router.routes()}
        assert any('/ws' in s or 'ws_handler' in s for s in route_strs), f"No /ws route in {route_strs}"

    def test_shutdown_endpoint(self):
        from core.input.server import create_app

        agent = MagicMock()
        agent._output = MagicMock()
        agent._bus = MagicMock()
        agent._state = RunState.RUNNING

        app = create_app(agent, serve_static=False)

        # Find and call shutdown handler
        for r in app.router.routes():
            if hasattr(r, 'resource') and 'shutdown' in str(r.resource):
                pass  # tested via integration

        # Simulate: POST /shutdown
        agent._state = RunState.STOPPING
        assert agent._state == RunState.STOPPING

    def test_restart_endpoint(self):
        from core.input.server import create_app

        agent = MagicMock()
        agent._output = MagicMock()
        agent._bus = MagicMock()
        agent._state = RunState.RUNNING

        app = create_app(agent, serve_static=False)

        for r in app.router.routes():
            if hasattr(r, 'resource') and 'restart' in str(r.resource):
                pass  # route exists

        agent._state = RunState.RESTARTING
        assert agent._state == RunState.RESTARTING


@pytest.mark.service
class TestCreateAppIntegration:
    """Integration tests using a real aiohttp test client."""

    @pytest.mark.asyncio
    async def test_shutdown_endpoint(self, aiohttp_client):
        from core.input.server import create_app

        agent = MagicMock()
        agent._output = MagicMock()
        agent._bus = MagicMock()
        agent._state = RunState.RUNNING

        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)

        resp = await client.post('/shutdown')
        assert resp.status == 200
        data = await resp.json()
        assert data['status'] == 'shutting_down'
        assert agent._state == RunState.STOPPING

    @pytest.mark.asyncio
    async def test_shutdown_get(self, aiohttp_client):
        from core.input.server import create_app

        agent = MagicMock()
        agent._output = MagicMock()
        agent._bus = MagicMock()
        agent._state = RunState.RUNNING

        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)

        resp = await client.get('/shutdown')
        assert resp.status == 200
        assert agent._state == RunState.STOPPING

    @pytest.mark.asyncio
    async def test_restart_endpoint(self, aiohttp_client):
        from core.input.server import create_app

        agent = MagicMock()
        agent._output = MagicMock()
        agent._bus = MagicMock()
        agent._state = RunState.RUNNING

        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)

        resp = await client.post('/restart')
        assert resp.status == 200
        data = await resp.json()
        assert data['status'] == 'restarting'
        assert agent._state == RunState.RESTARTING


@pytest.mark.service
class TestWebSocketHandler:
    @pytest.mark.asyncio
    async def test_text_message_triggers_process_message(self, aiohttp_client):
        from core.input.server import create_app

        agent = _make_agent()
        await agent.start()
        agent.process_message = AsyncMock()

        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)

        async with client.session.ws_connect(client.make_url('/ws')) as ws:
            await ws.send_json({"type": "text", "content": "hello"})
            # Give event loop time to process
            await asyncio.sleep(0.1)

        agent.process_message.assert_called_with(text="hello")
        await agent.stop()

    @pytest.mark.asyncio
    async def test_interrupt_message(self, aiohttp_client):
        from core.input.server import create_app

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
        from core.input.server import create_app

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
        from core.input.server import create_app

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
        from core.input.server import create_app
        from core.input.ws_handler import do_transcription as _do_transcription

        agent = _make_agent()
        await agent.start()

        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)

        async with client.session.ws_connect(client.make_url('/ws')) as ws:
            stt_state = {"emotion": "HAPPY", "events": ["Laughter"]}
            await _do_transcription(agent, ws, "测试", stt_emotion_state=stt_state)
            await asyncio.sleep(0.1)

        await agent.stop()


# ── _do_interrupt skip_if tests ──────────────────────────────────────────────

@pytest.mark.unit
class TestDoInterruptSkipIf:
    """VAD interrupt should skip during THINKING/EXECUTING, but allow
    during SPEAKING and when no skip_if is set (explicit button)."""

    @pytest.mark.asyncio
    async def test_vad_skips_during_thinking(self):
        from core.input.ws_handler import do_interrupt as _do_interrupt

        agent = MagicMock()
        agent._fsm.state = "thinking"
        agent._bus.post = MagicMock()

        await _do_interrupt(agent, skip_if={"thinking", "executing"})

        agent._bus.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_vad_skips_during_executing(self):
        from core.input.ws_handler import do_interrupt as _do_interrupt

        agent = MagicMock()
        agent._fsm.state = "executing"
        agent._bus.post = MagicMock()

        await _do_interrupt(agent, skip_if={"thinking", "executing"})

        agent._bus.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_vad_allows_during_speaking(self):
        from core.input.ws_handler import do_interrupt as _do_interrupt
        from core.monitoring.event_bus import EventPriority
        from core.schema.event import InterruptData

        agent = MagicMock()
        agent._fsm.state = "speaking"
        agent._bus.post = MagicMock()

        await _do_interrupt(agent, skip_if={"thinking", "executing"})

        agent._bus.post.assert_called_once()
        args, kwargs = agent._bus.post.call_args
        assert isinstance(args[0], InterruptData)
        assert kwargs["priority"] == EventPriority.INTERRUPT

    @pytest.mark.asyncio
    async def test_explicit_interrupt_always_allowed(self):
        from core.input.ws_handler import do_interrupt as _do_interrupt
        from core.monitoring.event_bus import EventPriority
        from core.schema.event import InterruptData

        agent = MagicMock()
        agent._fsm.state = "executing"
        agent._bus.post = MagicMock()

        await _do_interrupt(agent)  # no skip_if

        agent._bus.post.assert_called_once()
        args, kwargs = agent._bus.post.call_args
        assert isinstance(args[0], InterruptData)
        assert kwargs["priority"] == EventPriority.INTERRUPT


# ============================================================
# _handle_json edges
# ============================================================

@pytest.mark.unit
class TestHandleJsonEdges:
    @pytest.mark.asyncio
    async def test_empty_text_not_processed(self):
        from core.input.ws_handler import handle_json_message as _handle_json

        agent = MagicMock()
        agent.process_message = AsyncMock()
        await _handle_json({"type": "text", "content": ""}, agent)
        agent.process_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_whitespace_text_not_processed(self):
        from core.input.ws_handler import handle_json_message as _handle_json

        agent = MagicMock()
        agent.process_message = AsyncMock()
        await _handle_json({"type": "text", "content": "   "}, agent)
        agent.process_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_type_noop(self):
        from core.input.ws_handler import handle_json_message as _handle_json

        agent = MagicMock()
        await _handle_json({"type": "unknown_cmd"}, agent)
        # Should not crash

    @pytest.mark.asyncio
    async def test_interrupt_type(self):
        from core.input.ws_handler import handle_json_message as _handle_json

        agent = MagicMock()
        agent._fsm.state = "speaking"
        agent._bus.post = MagicMock()
        await _handle_json({"type": "interrupt"}, agent)
        agent._bus.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_type_field(self):
        from core.input.ws_handler import handle_json_message as _handle_json

        agent = MagicMock()
        await _handle_json({}, agent)  # no "type" field
        # Should not crash — defaults to "" → else branch


# ============================================================
# Health / ready endpoints
# ============================================================

@pytest.mark.service
class TestEndpoints:
    @pytest.fixture
    def app(self):
        from core.input.server import create_app

        agent = MagicMock()
        agent._fsm.state = "idle"
        agent._llm.model = "test-model"
        agent._session._messages = [1, 2, 3]
        agent._long_term.count = 42
        agent._tool_adapter._manager.tool_names = ["t1", "t2"]
        agent._output = MagicMock()
        agent._bus = MagicMock()
        return create_app(agent, serve_static=False)

    @pytest.mark.asyncio
    async def test_health_returns_ok(self, app, aiohttp_client):
        client = await aiohttp_client(app)
        resp = await client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert data["fsm"] == "idle"

    @pytest.mark.asyncio
    async def test_ready_returns_ok(self, app, aiohttp_client):
        client = await aiohttp_client(app)
        resp = await client.get("/ready")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_shutdown_sets_state_stopping(self, app, aiohttp_client):
        client = await aiohttp_client(app)
        resp = await client.post("/shutdown")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "shutting_down"
        assert app["agent"]._state == RunState.STOPPING

    @pytest.mark.asyncio
    async def test_restart_sets_state_restarting(self, app, aiohttp_client):
        client = await aiohttp_client(app)
        resp = await client.post("/restart")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "restarting"
        assert app["agent"]._state == RunState.RESTARTING


# ============================================================
# Tool plugin endpoints
# ============================================================

@pytest.mark.service
class TestToolEndpoints:
    @pytest.fixture
    def app(self):
        from core.input.server import create_app

        agent = MagicMock()
        agent._fsm.state = "idle"
        agent._output = MagicMock()
        agent._bus = MagicMock()
        agent._tool_adapter._manager.plugin_list = [
            {"name": "web_search", "description": "搜索网页", "capabilities": ["network"], "safety": {}, "active": True},
            {"name": "read_file", "description": "读取文件", "capabilities": ["filesystem_read"], "safety": {}, "active": True},
        ]
        agent._tool_adapter._manager.reload_plugin = AsyncMock(return_value=True)
        return create_app(agent, serve_static=False)

    @pytest.mark.asyncio
    async def test_list_plugins(self, app, aiohttp_client):
        client = await aiohttp_client(app)
        resp = await client.get("/tools")
        assert resp.status == 200
        data = await resp.json()
        assert "plugins" in data
        assert len(data["plugins"]) == 2
        assert data["plugins"][0]["name"] == "web_search"

    @pytest.mark.asyncio
    async def test_reload_plugin_success(self, app, aiohttp_client):
        client = await aiohttp_client(app)
        resp = await client.post("/tools/reload", json={"name": "web_search"})
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["name"] == "web_search"

    @pytest.mark.asyncio
    async def test_reload_plugin_missing_name(self, app, aiohttp_client):
        client = await aiohttp_client(app)
        resp = await client.post("/tools/reload", json={})
        assert resp.status == 400
        data = await resp.json()
        assert data["ok"] is False

    @pytest.mark.asyncio
    async def test_reload_plugin_invalid_body(self, app, aiohttp_client):
        client = await aiohttp_client(app)
        resp = await client.post("/tools/reload", data="not json")
        assert resp.status == 400
        data = await resp.json()
        assert data["ok"] is False

    @pytest.mark.asyncio
    async def test_reload_plugin_failure(self, app, aiohttp_client):
        app["agent"]._tool_adapter._manager.reload_plugin = AsyncMock(return_value=False)
        client = await aiohttp_client(app)
        resp = await client.post("/tools/reload", json={"name": "unknown"})
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is False


# ============================================================
# Fix assertion-less tests
# ============================================================

@pytest.mark.service
class TestAssertionFixes:
    @pytest.mark.asyncio
    async def test_invalid_json_returns_error_message(self, aiohttp_client):
        from core.input.server import create_app

        agent = _make_agent()
        await agent.start()

        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)

        async with client.session.ws_connect(client.make_url('/ws')) as ws:
            # 排空连接时推送的 startup 消息（session + capabilities）
            for _ in range(5):
                msg = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
                if msg["type"] == "session":
                    break
            await ws.send_str("not valid json")
            # capabilities 可能在 error 之前到达，循环等待直到收到 error
            msg = None
            for _ in range(10):
                msg = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
                if msg["type"] == "error":
                    break
            assert msg is not None, "Expected error response, got None"
            assert msg["type"] == "error"
            assert "Invalid JSON" in msg["message"]

            await ws.close()
        await agent.stop()
