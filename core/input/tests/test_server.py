"""Tests for core/input/server.py — WebSocket server.

Run: python -m pytest core/input/tests/test_server.py -v --asyncio-mode=auto
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web, WSMsgType
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
    agent.llm._backend = FakeLLMBackend(response="test")
    return agent


# ── helpers ─────────────────────────────────────────────────────────────────

def _make_app():
    """创建用于路由测试的最小 app（serve_static=False）。"""
    from core.input.server import create_app
    agent = MagicMock()
    agent.output = MagicMock()
    agent.bus = MagicMock()
    return create_app(agent, serve_static=False)


def _find_routes_by_name(app, name_fragment: str):
    """查找资源名包含 name_fragment 的路由。"""
    return [
        r for r in app.router.routes()
        if hasattr(r, 'resource') and r.resource and name_fragment in str(r.resource)
    ]


# ── tests ──────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestCreateApp:
    def test_ws_route_exists(self):
        """WebSocket /ws route 已注册。"""
        app = _make_app()
        route_strs = {str(r) for r in app.router.routes()}
        assert any('/ws' in s or 'ws_handler' in s for s in route_strs), \
            f"No /ws route in {route_strs}"

    def test_shutdown_route_exists(self):
        """POST /shutdown route 已注册。"""
        app = _make_app()
        assert len(_find_routes_by_name(app, 'shutdown')) > 0, "shutdown route 未注册"

    def test_restart_route_exists(self):
        """POST /restart route 已注册。"""
        app = _make_app()
        assert len(_find_routes_by_name(app, 'restart')) > 0, "restart route 未注册"


@pytest.mark.service
class TestCreateAppIntegration:
    """Integration tests using a real aiohttp test client."""

    @pytest.mark.asyncio
    async def test_shutdown_endpoint(self, aiohttp_client):
        from core.input.server import create_app

        agent = MagicMock()
        agent.output = MagicMock()
        agent.bus = MagicMock()
        agent._state = RunState.RUNNING

        def _set_shutting_down():
            agent._state = RunState.STOPPING

        agent.request_shutdown = MagicMock(side_effect=_set_shutting_down)

        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)

        resp = await client.post('/shutdown')
        assert resp.status == 200
        data = await resp.json()
        assert data['status'] == 'shutting_down'
        agent.request_shutdown.assert_called_once()
        assert agent._state == RunState.STOPPING

    @pytest.mark.asyncio
    async def test_shutdown_get(self, aiohttp_client):
        from core.input.server import create_app

        agent = MagicMock()
        agent.output = MagicMock()
        agent.bus = MagicMock()
        agent._state = RunState.RUNNING

        def _set_shutting_down():
            agent._state = RunState.STOPPING

        agent.request_shutdown = MagicMock(side_effect=_set_shutting_down)

        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)

        resp = await client.get('/shutdown')
        assert resp.status == 200
        agent.request_shutdown.assert_called_once()
        assert agent._state == RunState.STOPPING

    @pytest.mark.asyncio
    async def test_restart_endpoint(self, aiohttp_client):
        from core.input.server import create_app

        agent = MagicMock()
        agent.output = MagicMock()
        agent.bus = MagicMock()
        agent._state = RunState.RUNNING

        def _set_restarting():
            agent._state = RunState.RESTARTING

        agent.request_restart = MagicMock(side_effect=_set_restarting)

        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)

        resp = await client.post('/restart')
        assert resp.status == 200
        data = await resp.json()
        assert data['status'] == 'restarting'
        agent.request_restart.assert_called_once()
        assert agent._state == RunState.RESTARTING


@pytest.mark.slow
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
        initial_count = len(agent.output._channels)

        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)

        async with client.session.ws_connect(client.make_url('/ws')) as ws:
            await asyncio.sleep(0.1)
            # WebSocketChannel should be added
            assert len(agent.output._channels) == initial_count + 1

        # After disconnect, channel should be removed
        await asyncio.sleep(0.1)
        assert len(agent.output._channels) == initial_count

        await agent.stop()

    @pytest.mark.asyncio
    async def test_ws_connect_sends_current_mood(self, aiohttp_client):
        """连接后服务端应推送当前心情（expression 事件），前端据此渲染心情标签。"""
        import json

        from core.input.server import create_app

        agent = _make_agent()
        await agent.start()
        agent.character_engine.state.mood = "开心"
        agent.character_engine.state.mood_intensity = 0.8

        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)

        async with client.session.ws_connect(client.make_url('/ws')) as ws:
            got = None
            for _ in range(5):
                msg = await ws.receive(timeout=5)
                if msg.type == WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("type") == "expression":
                        got = data
                        break
            assert got is not None, "连接时未收到 expression 消息"
            assert got["expression"] == "开心"
            assert got["intensity"] == 0.8

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
        agent.status = MagicMock()
        agent.status.should_suppress_vad_interrupt = True
        agent.bus.post = MagicMock()

        await _do_interrupt(agent, skip_vad=True)

        agent.bus.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_vad_skips_during_executing(self):
        from core.input.ws_handler import do_interrupt as _do_interrupt

        agent = MagicMock()
        agent.status = MagicMock()
        agent.status.should_suppress_vad_interrupt = True
        agent.bus.post = MagicMock()

        await _do_interrupt(agent, skip_vad=True)

        agent.bus.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_vad_allows_during_speaking(self):
        from core.input.ws_handler import do_interrupt as _do_interrupt
        from core.monitoring.event_bus import EventPriority
        from core.schema.event import InterruptData

        agent = MagicMock()
        agent.status = MagicMock()
        agent.status.should_suppress_vad_interrupt = False
        agent.bus.post = MagicMock()

        await _do_interrupt(agent, skip_vad=True)

        agent.bus.post.assert_called_once()
        args, kwargs = agent.bus.post.call_args
        assert isinstance(args[0], InterruptData)
        assert kwargs["priority"] == EventPriority.INTERRUPT

    @pytest.mark.asyncio
    async def test_explicit_interrupt_always_allowed(self):
        from core.input.ws_handler import do_interrupt as _do_interrupt
        from core.monitoring.event_bus import EventPriority
        from core.schema.event import InterruptData

        agent = MagicMock()
        agent.status = MagicMock()
        agent.status.should_suppress_vad_interrupt = False
        agent.bus.post = MagicMock()

        await _do_interrupt(agent)

        agent.bus.post.assert_called_once()
        args, kwargs = agent.bus.post.call_args
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
        agent.status = MagicMock()
        agent.status.should_suppress_vad_interrupt = False
        agent.bus.post = MagicMock()
        await _handle_json({"type": "interrupt"}, agent)
        agent.bus.post.assert_called_once()

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
        agent.status.current_name.return_value = "idle"
        agent.llm.model = "test-model"
        agent.session._messages = [1, 2, 3]
        agent.long_term.count = 42
        agent.tool_adapter._manager.tool_names = ["t1", "t2"]
        agent.output = MagicMock()
        agent.bus = MagicMock()

        def _set_shutting_down():
            agent._state = RunState.STOPPING

        def _set_restarting():
            agent._state = RunState.RESTARTING

        agent.request_shutdown = MagicMock(side_effect=_set_shutting_down)
        agent.request_restart = MagicMock(side_effect=_set_restarting)
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
        agent.status.current_name.return_value = "idle"
        agent.output = MagicMock()
        agent.bus = MagicMock()
        agent.tool_adapter._manager.plugin_list = [
            {"name": "web_search", "description": "搜索网页", "capabilities": ["network"], "safety": {}, "active": True},
            {"name": "read_file", "description": "读取文件", "capabilities": ["filesystem_read"], "safety": {}, "active": True},
        ]
        agent.tool_adapter._manager.reload_plugin = AsyncMock(return_value=True)
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
        app["agent"].tool_adapter._manager.reload_plugin = AsyncMock(return_value=False)
        client = await aiohttp_client(app)
        resp = await client.post("/tools/reload", json={"name": "unknown"})
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is False


# ============================================================
# Fix assertion-less tests
# ============================================================

@pytest.mark.slow
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
