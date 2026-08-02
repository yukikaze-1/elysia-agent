"""Tests for core/input/server.py — WebSocket server.

Run: python -m pytest core/input/tests/test_server.py -v --asyncio-mode=auto
"""

import asyncio
import json
import os
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
    async def test_ws_connect_capabilities_includes_models(self, aiohttp_client):
        """capabilities 携带三个服务当前模型；未连接的服务 model 为 None。"""
        import json

        from core.input.server import create_app

        agent = _make_agent()
        await agent.start()

        # 测试 app 不传 stt_transcribe → STT 未连接；默认 config tts 未启用 → TTS 未连接
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)

        async with client.session.ws_connect(client.make_url('/ws')) as ws:
            cap = None
            for _ in range(5):
                msg = await ws.receive(timeout=5)
                if msg.type == WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("type") == "capabilities":
                        cap = data
                        break
            assert cap is not None, "连接时未收到 capabilities 消息"
            assert cap["llm_model"] == agent.llm.model
            # STT/TTS 未连接 → model 必须为 None（避免前端显示"选中"不可用模型）
            assert cap["stt_model"] is None
            assert cap["tts_model"] is None

        await agent.stop()

    @pytest.mark.asyncio
    async def test_ws_connect_capabilities_stt_model_when_connected(self, aiohttp_client):
        """STT 连接时 stt_model 应携带当前模型，供前端回填下拉选中值。"""
        import json

        from core.input.server import create_app

        agent = _make_agent()
        await agent.start()

        async def fake_transcribe(audio: bytes) -> str:
            return "test"

        app = create_app(agent, serve_static=False, stt_transcribe=fake_transcribe)
        client = await aiohttp_client(app)

        async with client.session.ws_connect(client.make_url('/ws')) as ws:
            cap = None
            for _ in range(5):
                msg = await ws.receive(timeout=5)
                if msg.type == WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("type") == "capabilities":
                        cap = data
                        break
            assert cap is not None, "连接时未收到 capabilities 消息"
            assert cap["stt"] is True
            assert cap["stt_model"] == agent.config.stt.model

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

    @pytest.mark.asyncio
    async def test_switch_llm_resolves_custom_params(self, tmp_path, aiohttp_client):
        """switch_llm 切到 custom 时 resolve_model_params 解析 base_url 并传给 Factory。"""
        from unittest.mock import patch
        from core.input import custom_providers as cp
        from core.input.server import create_app
        import json as _json

        agent = _make_agent()
        await agent.start()

        yaml_path = tmp_path / "custom_providers.yaml"
        yaml_path.write_text(
            "llm:\n  - name: myllm\n    base_url: http://myllm.local\n    api_key_env: MY_LLM_KEY\n    models: [m1]\n",
            encoding="utf-8",
        )

        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)

        # clear=True + 置空 MY_LLM_KEY → resolve 不返回 api_key，断言确定性
        with patch.object(cp, "_DEFAULT_PATH", yaml_path), \
             patch.dict(os.environ, {"MY_LLM_KEY": ""}, clear=True), \
             patch.object(agent.llm, "switch_model") as mock_switch:
            async with client.session.ws_connect(client.make_url('/ws')) as ws:
                await ws.send_json({"type": "switch_model", "service": "llm", "model": "custom/myllm"})
                await asyncio.sleep(0.2)
        mock_switch.assert_called_once_with("custom/myllm", base_url="http://myllm.local")
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
# set_service 消息（agent.set_service + service_mode_result）
# ============================================================

@pytest.mark.service
class TestSetService:
    @pytest.mark.asyncio
    async def test_set_service_tts_toggles_and_broadcasts(self, aiohttp_client):
        from core.input.server import create_app

        agent = _make_agent()  # AppConfig 默认 config.tts.enabled=False
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        async with client.session.ws_connect(client.make_url("/ws")) as ws:
            await ws.send_json({"type": "set_service", "service": "tts", "enabled": False})
            resp = None
            for _ in range(5):
                msg = await ws.receive(timeout=5)
                if msg.type == WSMsgType.TEXT and json.loads(msg.data).get("type") == "service_mode_result":
                    resp = json.loads(msg.data)
                    break
            assert resp is not None and resp["ok"] is True and resp["enabled"] is False
            assert agent.session.service_state.tts_enabled is None  # False == config(False) → 写 None
            assert agent._tts_enabled is False  # 运行时槽位同步（原始 bool，非 None）

    @pytest.mark.asyncio
    async def test_set_service_llm_rejected(self, aiohttp_client):
        from core.input.server import create_app

        agent = _make_agent()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        async with client.session.ws_connect(client.make_url("/ws")) as ws:
            await ws.send_json({"type": "set_service", "service": "llm", "enabled": False})
            resp = None
            for _ in range(5):
                msg = await ws.receive(timeout=5)
                if msg.type == WSMsgType.TEXT and json.loads(msg.data).get("type") == "service_mode_result":
                    resp = json.loads(msg.data)
                    break
            assert resp is not None and resp["ok"] is False  # llm 拒绝


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
# Image upload endpoint
# ============================================================

_PNG_HEADER = b"\x89PNG\r\n\x1a\n"


@pytest.mark.service
class TestImageUpload:
    """POST /api/images multipart 图片上传。

    回归点：aiohttp 3.x 的 BodyPartReader.read() 只接受关键字参数
    ``decode``，不接受位置 size 参数——之前 ``read(max_bytes + 1)``
    直接 TypeError（BodyPartReader.read() takes 1 positional argument but 2 were given）。

    注：不使用 pytest-aiohttp 的 aiohttp_client fixture（本环境未安装），
    直接用 aiohttp.test_utils.TestClient 起真实服务器。
    """

    @pytest.fixture
    def image_app(self, monkeypatch, tmp_path):
        """构建带图片上传路由的最小 app，图片落盘重定向到 tmp_path。"""
        from core.input.server import create_app

        def _make(image_max_mb: int = 10,
                  allowed_types: list[str] | None = None) -> web.Application:
            agent = MagicMock()
            agent.config = MagicMock()
            agent.config.server.image_max_mb = image_max_mb
            agent.config.server.image_allowed_types = allowed_types or [
                "image/png", "image/jpeg", "image/gif", "image/webp",
            ]
            agent.output = MagicMock()
            agent.bus = MagicMock()
            monkeypatch.setenv(
                "ELYSIA_PATH_DATA_UPLOADS_IMAGES",
                str(tmp_path / "uploads" / "images"),
            )
            return create_app(agent, serve_static=False)

        return _make

    @pytest.mark.asyncio
    async def _post(self, app: web.Application, filename: str,
                    content: bytes, content_type: str) -> tuple[int, bytes]:
        """起一个临时服务器并 POST multipart 到 /api/images。

        在关闭连接前读取完整响应体，返回 (status, body)。
        aiohttp ClientSession 的 dict data= 不保证编码为 multipart，
        显式用 FormData 构造浏览器同款 multipart body。
        """
        from aiohttp import FormData
        from aiohttp.test_utils import TestClient, TestServer

        form = FormData()
        form.add_field("image", content, filename=filename, content_type=content_type)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post("/api/images", data=form)
            body = await resp.read()
            return resp.status, body
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_upload_png_returns_url(self, image_app):
        """正常 PNG 上传应返回 200 + url（回归：修复前 500 TypeError）。"""
        content = _PNG_HEADER + b"\x00" * 100
        status, body = await self._post(image_app(), "test.png", content, "image/png")
        assert status == 200, f"expected 200, got {status}: {body.decode()}"
        data = json.loads(body)
        assert "url" in data
        assert data["mime"] == "image/png"
        assert data["size"] == len(content)

    @pytest.mark.asyncio
    async def test_upload_oversize_rejected_413(self, image_app):
        """超过 image_max_mb 的图片应返回 413。"""
        big = _PNG_HEADER + b"\x00" * (1 * 1024 * 1024 + 1)
        status, body = await self._post(image_app(image_max_mb=1), "big.png", big, "image/png")
        assert status == 413, f"expected 413, got {status}: {body.decode()}"
        data = json.loads(body)
        assert "过大" in data["error"]

    @pytest.mark.asyncio
    async def test_upload_unsupported_type_rejected_415(self, image_app):
        """非允许类型的文件应返回 415（仅允许 png 时上传 jpg）。"""
        status, body = await self._post(
            image_app(allowed_types=["image/png"]), "photo.jpg", b"\xff\xd8\xff", "image/jpeg",
        )
        assert status == 415, f"expected 415, got {status}: {body.decode()}"


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
