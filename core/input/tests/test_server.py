"""core/input/server.py 测试 —— WebSocket 服务器。

Run: python -m pytest core/input/tests/test_server.py -v --asyncio-mode=auto
"""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web, WSMsgType
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

from core.agent import RunState
from Module.stt.protocol import STTBackend


# ── helpers ────────────────────────────────────────────────────────────────

class _FakeSTTBackend(STTBackend):
    """capabilities 测试假 STT 后端——model 必须等于 config.stt.model。

    实现 STTBackend 协议（transcribe/transcribe_stream/test/close/provider_name/model）。
    build_capabilities 读 agent.stt.model，缺 model 会 AttributeError。
    """

    def __init__(self, model: str = "sensevoice/sense-voice") -> None:
        self._model = model

    @property
    def provider_name(self) -> str:
        return "sensevoice"

    @property
    def model(self) -> str:
        return self._model

    async def transcribe(self, audio: bytes, **kwargs) -> str:
        return "test"

    async def transcribe_stream(self, audio: bytes, **kwargs):
        yield ""

    async def test(self) -> dict:
        return {"ok": True, "latency_ms": 0}

    async def close(self) -> None:
        pass


def _make_agent():
    """为服务器测试创建最小 mock agent。"""
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


def _make_static_app(static_dir: Path, serve_chat: bool, files: tuple[str, ...]):
    """serve_static=True 的 app——_STATIC_DIR 指向 tmp 目录（含指定文件）。"""
    from core.input.server import create_app
    for name in files:
        (static_dir / name).write_text(f"<html>{name}</html>", encoding="utf-8")
    agent = MagicMock()
    agent.output = MagicMock()
    agent.bus = MagicMock()
    with patch("core.input.server._STATIC_DIR", static_dir):
        return create_app(agent, serve_static=True, serve_chat=serve_chat)


def _find_routes_by_name(app, name_fragment: str):
    """查找资源名包含 name_fragment 的路由。"""
    return [
        r for r in app.router.routes()
        if hasattr(r, 'resource') and r.resource and name_fragment in str(r.resource)
    ]


# ── tests ──────────────────────────────────────────────────────────────────

@pytest.mark.service
class TestAttachmentSigStartupWarning:
    """附件签名 fail-open 启动警告（spec §3.4）：SIG 未配 + 无鉴权 → warning。"""

    def test_sig_empty_no_token_logs_warning(self, caplog):
        """SIG 未配 + access_token 空 → WARNING「附件签名未启用」。"""
        import logging
        from core.input.server import warn_if_attachment_signature_disabled
        with caplog.at_level(logging.WARNING, logger="core.input.server"):
            warn_if_attachment_signature_disabled("", "")
        assert any("附件签名未启用" in r.message for r in caplog.records)

    def test_sig_configured_no_warning(self, caplog):
        """SIG 已配 → 不提示。"""
        import logging
        from core.input.server import warn_if_attachment_signature_disabled
        with caplog.at_level(logging.WARNING, logger="core.input.server"):
            warn_if_attachment_signature_disabled("test-secret", "")
        assert not any("附件签名未启用" in r.message for r in caplog.records)

    def test_token_configured_no_warning(self, caplog):
        """SIG 未配但有 access_token（fail-closed 无签名下载被拒）→ 不提示。"""
        import logging
        from core.input.server import warn_if_attachment_signature_disabled
        with caplog.at_level(logging.WARNING, logger="core.input.server"):
            warn_if_attachment_signature_disabled("", "secret-token")
        assert not any("附件签名未启用" in r.message for r in caplog.records)


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
    """使用真实 aiohttp 测试客户端的集成测试。"""

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
            # 给事件循环处理时间
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
            # 应已添加 WebSocketChannel
            assert len(agent.output._channels) == initial_count + 1

        # 断开后应移除通道
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

        # 测试 app 不注入 STT 后端 → STT 未连接；默认 config tts 未启用 → TTS 未连接
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
            assert cap["services"]["llm"]["model"] == agent.llm.model
            # STT/TTS 未连接 → model 必须为 None（避免前端显示"选中"不可用模型）
            assert cap["services"]["stt"]["available"] is False
            assert cap["services"]["stt"]["model"] is None
            assert cap["services"]["tts"]["available"] is False
            assert cap["services"]["tts"]["model"] is None

        await agent.stop()

    @pytest.mark.asyncio
    async def test_ws_connect_capabilities_stt_model_when_connected(self, aiohttp_client):
        """STT 连接时 stt model 应携带当前模型，供前端回填下拉选中值。"""
        import json

        from core.input.server import create_app

        agent = _make_agent()
        await agent.start()
        # STT 注入改走 agent.stt 槽位（create_app 已移除 stt_transcribe 参数）
        agent._stt = _FakeSTTBackend(agent.config.stt.model)

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
            assert cap["services"]["stt"]["available"] is True
            assert cap["services"]["stt"]["model"] == agent.config.stt.model

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
            # 不应崩溃 —— 错误响应或静默处理
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
        """switch_llm 切到 custom 时 resolve 解析 base_url/api_key、剥离 runtime 并注入守卫 http_client。"""
        import socket as _socket
        from unittest.mock import patch
        from core.input import custom_providers as cp
        from core.input.ssrf import _SSRFTransport
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

        # mock getaddrinfo 返回公网 IP——myllm.local 为不可解析 host，SSRF 校验会 NXDOMAIN 拒绝
        public_infos = [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]
        with patch.object(cp, "_DEFAULT_PATH", yaml_path), \
             patch.dict(os.environ, {"MY_LLM_KEY": "test-key"}, clear=True), \
             patch("core.input.ssrf.socket.getaddrinfo", return_value=public_infos), \
             patch.object(agent.llm, "switch_model") as mock_switch:
            async with client.session.ws_connect(client.make_url('/ws')) as ws:
                await ws.send_json({"type": "switch_model", "service": "llm", "model": "custom/myllm"})
                await asyncio.sleep(0.2)
        # 剥离 runtime 透传（Task 4 交接）——runtime 键不传给 switch_model（assert_called_once 严格匹配）
        mock_switch.assert_called_once_with("custom/myllm", base_url="http://myllm.local",
                                            api_key="test-key",
                                            http_client_factory=mock_switch.call_args.kwargs["http_client_factory"])
        _, kwargs = mock_switch.call_args
        assert "runtime" not in kwargs                     # runtime 已剥离
        # 出站守卫工厂注入（review 修正：工厂而非实例——reconnect 可重建全新守卫 client）
        assert isinstance(kwargs["http_client_factory"]()._transport, _SSRFTransport)
        await agent.stop()


# ── _do_interrupt skip_if tests ──────────────────────────────────────────────

@pytest.mark.unit
class TestDoInterruptSkipIf:
    """VAD 中断在 THINKING/EXECUTING 时应被跳过，但在
    SPEAKING 及未设置 skip_if 时（显式按钮）应允许中断。"""

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
        # 不应崩溃

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
        # 不应崩溃 —— 默认 "" → 走 else 分支


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

    @pytest.mark.asyncio
    async def test_set_service_rebroadcasts_capabilities(self, aiohttp_client):
        """set_service 成功后经 agent.output 向所有 WS 客户端重播嵌套 capabilities。"""
        from core.input.server import create_app

        agent = _make_agent()  # AppConfig 默认 config.tts.enabled=False
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        async with client.session.ws_connect(client.make_url("/ws")) as ws:
            # 排空建连时的 capabilities，确保后续收到的是重播帧
            for _ in range(10):
                msg = await ws.receive(timeout=5)
                if msg.type == WSMsgType.TEXT and json.loads(msg.data).get("type") == "capabilities":
                    break
            await ws.send_json({"type": "set_service", "service": "tts", "enabled": False})
            cap = None
            for _ in range(8):
                msg = await ws.receive(timeout=5)
                if msg.type == WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("type") == "capabilities":
                        cap = data
                        break
            assert cap is not None, "set_service 成功后未收到 capabilities 重播"
            assert cap["services"]["tts"]["enabled"] is False
            assert cap["services"]["stt"]["available"] is False

    @pytest.mark.asyncio
    async def test_set_service_stt_enable_build_success_persists(self, aiohttp_client):
        """热插拔"开"路径：后端 None + switch_stt 补建成功 → 持久化 enabled +
        capabilities 重播 available:true。"""
        from core.input.server import create_app

        agent = _make_agent()  # AppConfig 默认 config.stt.enabled=False
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)

        def _mount_stt(model: str) -> dict:
            # 模拟 switch_stt 补建成功：挂载假后端（capabilities available 由此判定）
            agent._stt = _FakeSTTBackend(agent.config.stt.model)
            agent._stt_ok = True
            return {"ok": True}

        agent.switch_stt = AsyncMock(side_effect=_mount_stt)

        async with client.session.ws_connect(client.make_url("/ws")) as ws:
            # 排空建连时的 capabilities，确保后续收到的是重播帧
            for _ in range(10):
                msg = await ws.receive(timeout=5)
                if msg.type == WSMsgType.TEXT and json.loads(msg.data).get("type") == "capabilities":
                    break
            await ws.send_json({"type": "set_service", "service": "stt", "enabled": True})
            resp = None
            cap = None
            for _ in range(8):
                msg = await ws.receive(timeout=5)
                if msg.type == WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("type") == "service_mode_result":
                        resp = data
                    elif data.get("type") == "capabilities":
                        cap = data
                if resp is not None and cap is not None:
                    break
            assert resp is not None and resp["ok"] is True and resp["enabled"] is True
            assert cap is not None, "set_service 成功后未收到 capabilities 重播"
            assert cap["services"]["stt"]["available"] is True
            assert cap["services"]["stt"]["enabled"] is True
        assert agent.session.service_state.stt_enabled is True  # 补建成功 → 持久化
        assert agent._stt_enabled is True  # 运行时槽位同步（原始 bool）

    @pytest.mark.asyncio
    async def test_set_service_tts_enable_build_failure_not_persisted(self, aiohttp_client):
        """热插拔"开"路径：后端 None + switch_tts 补建失败 → 不持久化 enabled +
        service_mode_result ok:false。"""
        from core.input.server import create_app

        agent = _make_agent()  # AppConfig 默认 config.tts.enabled=False
        agent.switch_tts = AsyncMock(return_value={"ok": False, "error": "boom"})
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        async with client.session.ws_connect(client.make_url("/ws")) as ws:
            await ws.send_json({"type": "set_service", "service": "tts", "enabled": True})
            resp = None
            for _ in range(5):
                msg = await ws.receive(timeout=5)
                if msg.type == WSMsgType.TEXT and json.loads(msg.data).get("type") == "service_mode_result":
                    resp = json.loads(msg.data)
                    break
            assert resp is not None and resp["ok"] is False
            assert resp["error"] == "boom"
        assert agent.session.service_state.tts_enabled is None  # 补建失败不持久化
        assert agent._tts_enabled is None  # 运行时槽位未动（未挂载成功）


# ============================================================
# WS wire 格式专项（迁移步 A 行为等价守护）
# ============================================================

@pytest.mark.service
class TestWireFormatContract:
    """WS 消息 wire 契约：switch_model_result 成功无 error 键；service_mode_result 成功 error 恒 null。

    改造前手写 dict 已符合；切到契约模型 + wire_dict 后须保持——"行为等价"守护。
    """

    @pytest.mark.asyncio
    async def test_switch_model_result_success_no_error_key(self, aiohttp_client):
        """switch_model_result 成功：wire 无 error 键（缺省省略，非 null）。"""
        from core.input.server import create_app

        agent = _make_agent()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        async with client.session.ws_connect(client.make_url("/ws")) as ws:
            await ws.send_json({"type": "switch_model", "service": "llm", "model": "deepseek/deepseek-v4-flash"})
            frame = None
            for _ in range(5):
                msg = await ws.receive(timeout=5)
                if msg.type == WSMsgType.TEXT and json.loads(msg.data).get("type") == "switch_model_result":
                    frame = json.loads(msg.data)
                    break
            assert frame is not None and frame["ok"] is True
            assert "error" not in frame  # 成功路径不得出现 "error": null（ts_null 过度标记会破坏）

    @pytest.mark.asyncio
    async def test_service_mode_result_success_error_null(self, aiohttp_client):
        """service_mode_result 成功：wire 恒有 error 键且为 null（ts_null）。"""
        from core.input.server import create_app

        agent = _make_agent()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        async with client.session.ws_connect(client.make_url("/ws")) as ws:
            await ws.send_json({"type": "set_service", "service": "tts", "enabled": False})
            frame = None
            for _ in range(5):
                msg = await ws.receive(timeout=5)
                if msg.type == WSMsgType.TEXT and json.loads(msg.data).get("type") == "service_mode_result":
                    frame = json.loads(msg.data)
                    break
            assert frame is not None and frame["ok"] is True
            assert frame["error"] is None  # 键恒存在，成功为 null


# ============================================================
# Session 回放契约（I1'） + 逐条回放兜底（M2）
# ============================================================

@pytest.mark.unit
class TestSessionReplayContract:
    """session 回放 wire 契约。

    I1'：messages_for_client 输出经 SessionMessage.wire_dict() 往返不得丢弃
    未建模键（tool_calls.type / token_usage.rounds.session_id / image
    detail/width/height）。M2：单条无法契约化的消息仅丢弃该条，不整段丢失回放。
    """

    def test_roundtrip_preserves_unmodeled_keys(self):
        """tool_calls.type / token_usage.rounds.session_id / image detail/width/height 往返不丢。"""
        from core.memory.session import SessionMemory
        from core.schema.message import AssistantMessage
        from core.schema.part import ImagePart, TextPart
        from core.schema.token import TokenUsage, TokenUsageSummary
        from core.schema.tool import ToolCall, ToolCallFunction
        from core.schema.types import MessageId
        from frontend_contract import SessionMessage

        s = SessionMemory()
        s.add_message(AssistantMessage(
            content=[
                TextPart(content="hi"),
                ImagePart(src="http://x/i.png", mime="image/png", detail="high",
                          width=800, height=600),
            ],
            message_id=MessageId("asst-1"),
            tool_calls=[ToolCall(id="call_1",
                                 function=ToolCallFunction(name="web_search", arguments="{}"))],
            token_usage=TokenUsageSummary(
                rounds=[TokenUsage(model="m", session_id="s1", round_num=1,
                                   input_tokens=10, output_tokens=5, total_tokens=15)],
                turn_total=15,
            ),
            inner_voice="thinking",
            block_index={"text": [0]},
        ))

        frame = SessionMessage(messages=s.messages_for_client(), token_stats=None,
                               conversationId="conv_replay").wire_dict()
        assert frame["conversationId"] == "conv_replay"  # 批次 9a1：必发 conversationId
        asst = next(m for m in frame["messages"] if m["role"] == "assistant")
        # I1'：tool_calls[].type（"function"）不被丢弃
        assert asst["tool_calls"][0]["type"] == "function"
        # I1'：token_usage.rounds[].session_id 不被丢弃
        assert asst["token_usage"]["rounds"][0]["session_id"] == "s1"
        # I1'：image detail/width/height 不被丢弃
        img = next(p for p in asst["content"] if p["type"] == "image")
        assert img["detail"] == "high"
        assert img["width"] == 800
        assert img["height"] == 600
        # message_id/timestamp 保留（前端去重/时间分隔依赖）
        assert asst["message_id"] == "asst-1"
        assert asst["timestamp"]

    def test_build_session_items_drops_invalid_keeps_valid(self):
        """单条无法契约化的消息（content 含未知 type part）仅丢弃该条，其余保留。"""
        from core.input.ws_handler import _build_session_items

        msgs = [
            {"role": "user", "content": [{"type": "text", "content": "hi"}],
             "message_id": "u1", "timestamp": "t1"},
            # content 含未知 type → ContentPart 判别联合只认 text/image/file/audio/video/sticker → 契约校验失败
            {"role": "assistant", "content": [{"type": "unknown"}],
             "message_id": "a1", "timestamp": "t2"},
            {"role": "tool", "tool_call_id": "c1", "name": "web", "content": "ok",
             "success": True, "duration_ms": 1.0, "truncated": False},
        ]
        items = _build_session_items(msgs)
        assert len(items) == 2
        assert [i.role for i in items] == ["user", "tool"]


@pytest.mark.service
@pytest.mark.asyncio
async def test_session_replay_token_stats_camel_key(aiohttp_client):
    """v1 建连回放 session 帧 token_stats wire 用 byModel 键（rename 后回放不崩）。"""
    from core.input.server import create_app
    from core.cognitive.tests.test_v2_generation import _make_agent_with, _UsageLLM

    agent = _make_agent_with(_UsageLLM())
    await agent.start()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    try:
        ws = await client.session.ws_connect(client.make_url("/ws"), headers={})
        seen = None
        for _ in range(10):
            m = await asyncio.wait_for(ws.receive(), timeout=5)
            if m.type == WSMsgType.TEXT:
                d = json.loads(m.data)
                if d.get("type") == "session":
                    seen = d
                    break
        assert seen is not None, "未收到 session 回放帧"
        ts = seen.get("token_stats") or {}
        assert "byModel" in ts      # rename 后 wire 用 camel
        assert "by_model" not in ts
        await ws.close()
    finally:
        await agent.stop()



# ============================================================
# Health / ready 端点
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
        agent.session.health_check = AsyncMock(return_value=True)
        agent.long_term.count = 42
        agent.long_term.health_check = AsyncMock(return_value=True)
        # P1-05：/ready 聚合 message/attachment repo 真实健康
        agent.message_repo.health_check = AsyncMock(return_value=True)
        agent.attachment_repo.health_check = AsyncMock(return_value=True)
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
        assert data["chatReady"] is True
        assert data["fullReady"] is True

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
# 工具插件端点
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
        agent.tool_adapter._manager.reload_plugin = AsyncMock(return_value=(True, ""))
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
        app["agent"].tool_adapter._manager.reload_plugin = AsyncMock(return_value=(False, "some reason"))
        client = await aiohttp_client(app)
        resp = await client.post("/tools/reload", json={"name": "unknown"})
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is False
        assert "some reason" in data["error"]


# ============================================================
# 图片上传端点
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

    @pytest.mark.asyncio
    async def test_upload_write_error_envelope_no_exception_text(
            self, image_app, monkeypatch):
        """写盘抛 OSError（含绝对路径）→ 500 信封不含路径/异常原文（Task 9，:584）。"""
        import os as _os

        def boom(*args, **kwargs):
            raise OSError("EACCES: /home/user/secret.png")

        monkeypatch.setattr(_os, "makedirs", boom)
        status, body = await self._post(
            image_app(), "test.png", _PNG_HEADER + b"\x00" * 100, "image/png")
        assert status == 500, f"expected 500, got {status}: {body.decode()}"
        text = body.decode()
        assert "/home/" not in text and "EACCES" not in text
        assert "secret.png" not in text
        data = json.loads(body)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_upload_html_extension_rejected_415(self, image_app):
        """D5/P2-03：.html 扩展拒绝（不默认为 image/png）。"""
        status, body = await self._post(
            image_app(), "evil.html", b"<html><script>alert(1)</script></html>",
            "text/html")
        assert status == 415, f"expected 415, got {status}: {body.decode()}"

    @pytest.mark.asyncio
    async def test_upload_content_type_mismatch_rejected_415(self, image_app):
        """D5/P2-03：.png 扩展但内容是 HTML（polyglot）→ 魔数不符拒绝。"""
        import json as _json
        status, body = await self._post(
            image_app(), "fake.png", b"<html><script>alert(1)</script></html>",
            "image/png")
        assert status == 415, f"expected 415, got {status}: {body.decode()}"
        assert "不符" in _json.loads(body.decode())["error"]

    @pytest.mark.asyncio
    async def test_uploads_static_response_has_nosniff(self, image_app, tmp_path):
        """D5/P2-03：legacy /uploads/ 静态响应带 X-Content-Type-Options: nosniff。"""
        app = image_app()
        img_dir = tmp_path / "uploads" / "images"
        img_dir.mkdir(parents=True, exist_ok=True)
        (img_dir / "x.png").write_bytes(_PNG_HEADER + b"\x00" * 20)

        from aiohttp import FormData
        from aiohttp.test_utils import TestClient, TestServer
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.get("/uploads/images/x.png")
            assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        finally:
            await client.close()


# ── Task 9（批次 8a A 簇 minor）：错误信封不泄漏异常文本/绝对路径 ────────────

@pytest.mark.service
class TestErrorEnvelopeNoExceptionText:
    """Task 9（批次 8a）：错误信封只含通用文案，str(e) 只进日志。

    四个信封点：api_routes.py:299（provider save）/ :330（provider delete）/
    :584（image 上传 ImageUploadError）/ :960（desktop 上传 _tool_error）。
    """

    @pytest.mark.asyncio
    async def test_provider_save_error_envelope_no_exception_text(
            self, aiohttp_client, monkeypatch):
        """add_provider 抛 OSError（含绝对路径）→ 500 ApiFail 信封不含路径/异常原文。"""
        from core.input import custom_providers as cp
        from core.input.server import create_app

        def boom(*args, **kwargs):
            raise OSError("EACCES: /home/user/secret.txt")

        monkeypatch.setattr(cp, "add_provider", boom)
        app = create_app(_make_agent(), serve_static=False)
        client = await aiohttp_client(app)
        resp = await client.post("/api/providers", json={
            "service": "llm",
            "entry": {"name": "x", "runtime": "desktop"},
        })
        body = (await resp.read()).decode()
        assert resp.status == 500, f"expected 500, got {resp.status}: {body}"
        assert "/home/" not in body and "EACCES" not in body
        assert "secret.txt" not in body
        assert "error" in json.loads(body)

    @pytest.mark.asyncio
    async def test_provider_delete_error_envelope_no_exception_text(
            self, aiohttp_client, monkeypatch):
        """remove_provider 抛 OSError（含绝对路径）→ 500 ApiFail 信封不含路径/异常原文。"""
        from core.input import custom_providers as cp
        from core.input.server import create_app

        def boom(*args, **kwargs):
            raise OSError("EACCES: /home/user/secret.txt")

        monkeypatch.setattr(cp, "remove_provider", boom)
        app = create_app(_make_agent(), serve_static=False)
        client = await aiohttp_client(app)
        resp = await client.delete("/api/providers/llm/x")
        body = (await resp.read()).decode()
        assert resp.status == 500, f"expected 500, got {resp.status}: {body}"
        assert "/home/" not in body and "EACCES" not in body
        assert "secret.txt" not in body
        assert "error" in json.loads(body)

    @pytest.mark.asyncio
    async def test_desktop_upload_error_envelope_no_exception_text(
            self, aiohttp_client, monkeypatch, tmp_path):
        """desktop 附件写盘抛 OSError（含绝对路径）→ 500 信封不含路径/异常原文。"""
        from aiohttp import FormData
        from core.input.server import create_app

        agent = MagicMock()
        agent.output = MagicMock()
        agent.bus = MagicMock()
        agent.config = MagicMock()
        agent.config.server.attachment_sig_secret = "test-secret"
        agent.config.server.image_max_mb = 10
        agent.config.server.image_allowed_types = ["image/png"]
        agent.desktop_bridge = MagicMock()
        agent.desktop_bridge.get_call = AsyncMock(return_value={
            "target_connection": "conn_test",
            "status": "running",
        })
        monkeypatch.setenv("ELYSIA_PATH_DATA_UPLOADS_DESKTOP",
                           str(tmp_path / "uploads" / "desktop"))

        def boom(*args, **kwargs):
            raise OSError("EACCES: /home/user/secret.bin")

        monkeypatch.setattr(os, "makedirs", boom)
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)

        form = FormData()
        form.add_field("file", _PNG_HEADER + b"\x00" * 64,
                       filename="shot.png", content_type="image/png")
        resp = await client.post("/api/uploads/desktop/call_test",
                                 data=form,
                                 headers={"X-Client-Connection": "conn_test"})
        body = (await resp.read()).decode()
        assert resp.status == 500, f"expected 500, got {resp.status}: {body}"
        assert "/home/" not in body and "EACCES" not in body
        assert "secret.bin" not in body
        data = json.loads(body)
        assert "error" in data


# ============================================================
# 修复无断言的测试
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


# ============================================================
# 批次 8c F2：admin/debug 裸 error 统一 ProtocolError 信封
# ============================================================

@pytest.mark.service
class TestAdminDebugEnvelope:
    @pytest.mark.asyncio
    async def test_admin_unauthorized_unified_envelope(self, aiohttp_client):
        """无 token 调 /shutdown → 401 ProtocolError 信封（code/retryable/requestId/error 别名）。"""
        from core.input.server import create_app
        agent = _make_agent()  # 文件内既有 stub
        app = create_app(agent, serve_static=False, auth_token="secret")
        client = await aiohttp_client(app)
        resp = await client.post("/shutdown")
        assert resp.status == 401
        data = await resp.json()
        assert data["code"] == "AUTH_REQUIRED"
        assert data["retryable"] is False
        assert data["requestId"]
        assert data["error"] == data["message"]  # 兼容别名

    @pytest.mark.asyncio
    async def test_debug_ltm_bad_pagination_envelope(self, aiohttp_client):
        """debug 业务错误（非法 limit 参数）→ 信封含 code。"""
        from core.input.server import create_app
        agent = _make_agent()  # 文件内既有 stub
        app = create_app(agent, serve_static=False, auth_token="secret")
        client = await aiohttp_client(app)
        # debug 路由受 auth_token 保护——带正确 token 才进业务 handler
        headers = {"Authorization": "Bearer secret"}
        resp = await client.get("/debug/api/ltm?limit=abc", headers=headers)
        assert resp.status == 400
        data = await resp.json()
        assert data.get("code") == "CONTRACT_VALIDATION"
        assert data.get("error") == data.get("message")  # 兼容别名


# ============================================================
# serve_chat 开关 + 拆 debug 耦合（停用 v1 聊天 / 保留 /debug）
# ============================================================

@pytest.mark.service
class TestServeChat:
    @pytest.mark.asyncio
    async def test_serve_chat_false_root_404_debug_200(self, aiohttp_client, tmp_path):
        """serve_chat=False（默认）：/ 404、/debug 200。"""
        app = _make_static_app(tmp_path, serve_chat=False, files=("index.html", "debug.html"))
        client = await aiohttp_client(app)
        assert (await client.get("/")).status == 404
        assert (await client.get("/debug")).status == 200

    @pytest.mark.asyncio
    async def test_serve_chat_true_root_200_debug_200(self, aiohttp_client, tmp_path):
        """serve_chat=True：/ 200、/debug 200。"""
        app = _make_static_app(tmp_path, serve_chat=True, files=("index.html", "debug.html"))
        client = await aiohttp_client(app)
        assert (await client.get("/")).status == 200
        assert (await client.get("/debug")).status == 200

    @pytest.mark.asyncio
    async def test_debug_served_without_index(self, aiohttp_client, tmp_path):
        """只有 debug.html（无 index.html）+ serve_chat=False：/debug 仍 200（拆耦合回归）。"""
        app = _make_static_app(tmp_path, serve_chat=False, files=("debug.html",))
        client = await aiohttp_client(app)
        assert (await client.get("/debug")).status == 200

    @pytest.mark.asyncio
    async def test_serve_chat_true_no_index_root_404(self, aiohttp_client, tmp_path):
        """serve_chat=True 但无 index.html：/ 404（无聊天页自然 404）。"""
        app = _make_static_app(tmp_path, serve_chat=True, files=("debug.html",))
        client = await aiohttp_client(app)
        assert (await client.get("/")).status == 404

    @pytest.mark.asyncio
    async def test_serve_chat_false_no_files_both_404(self, aiohttp_client, tmp_path):
        """serve_chat=False 且 static 空目录：/ 404、/debug 404（无文件不误挂）。"""
        app = _make_static_app(tmp_path, serve_chat=False, files=())
        client = await aiohttp_client(app)
        assert (await client.get("/")).status == 404
        assert (await client.get("/debug")).status == 404
