"""/api/tts/voices 端点测试（Task 9，BE-051 声音列表路由）。

覆盖：
- TTS 启用 + 后端激活 → 200 含 voices（source=api for MiniMax / static 其余）；
- TTS 未启用 → 200 空列表 + error="TTS 未启用"（不炸）；
- current 随活动会话 service_state.tts_voice。
"""
import pytest

from core.input.server import create_app


def _make_app_agent():
    """真实 Agent（不 start，无 DB）。"""
    from core.config import AppConfig
    from core.agent import Agent
    return Agent(config=AppConfig())


class _FakeBackend:
    """可注入的假 TTS 后端（list_voices 返回固定列表，provider_name 可配）。"""

    def __init__(self, voices, provider_name="minimax"):
        self._voices = voices
        self.provider_name = provider_name

    async def list_voices(self):
        return self._voices


@pytest.mark.service
@pytest.mark.asyncio
async def test_tts_voices_returns_list(aiohttp_client):
    """TTS 启用 + 后端激活 → 200 含 voices。"""
    agent = _make_app_agent()
    agent._tts = _FakeBackend([{"id": "v1", "name": "音色一"}])  # noqa: SLF001 — 测试注入后端
    agent._tts_enabled = True  # noqa: SLF001 — 测试注入有效开关
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    resp = await client.get("/api/tts/voices")
    data = await resp.json()
    assert resp.status == 200
    # 契约 TtsVoiceEntry 缺省值 languages/capabilities 非 None → 必然上 wire
    assert data["voices"] == [{"id": "v1", "name": "音色一", "languages": [], "capabilities": {}}]
    assert data["source"] == "api"  # MiniMax 动态拉取


@pytest.mark.service
@pytest.mark.asyncio
async def test_tts_voices_static_source_for_gpt_sovits(aiohttp_client):
    """gpt-sovits（静态声明）→ source=static。"""
    agent = _make_app_agent()
    agent._tts = _FakeBackend([{"id": "Elysia"}], provider_name="gpt-sovits")  # noqa: SLF001
    agent._tts_enabled = True  # noqa: SLF001
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    resp = await client.get("/api/tts/voices")
    data = await resp.json()
    assert resp.status == 200
    assert data["voices"] == [{"id": "Elysia", "languages": [], "capabilities": {}}]
    assert data["source"] == "static"


@pytest.mark.service
@pytest.mark.asyncio
async def test_tts_voices_disabled_empty(aiohttp_client):
    """TTS 未启用 → 200 空列表 + error（不炸）。"""
    agent = _make_app_agent()
    agent._tts = None  # noqa: SLF001
    agent._tts_enabled = False  # noqa: SLF001
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    resp = await client.get("/api/tts/voices")
    data = await resp.json()
    assert resp.status == 200
    assert data["voices"] == []
    assert data["source"] == "static"
    assert data["error"] == "TTS 未启用"
    assert data.get("current") is None


@pytest.mark.service
@pytest.mark.asyncio
async def test_tts_voices_current_follows_service_state(aiohttp_client):
    """current 随活动会话 service_state.tts_voice。"""
    agent = _make_app_agent()
    agent._tts = _FakeBackend([{"id": "v1"}, {"id": "v2"}])  # noqa: SLF001
    agent._tts_enabled = True  # noqa: SLF001
    # tts_voice 字段由 Task 11 加入 ServiceState——此处经 __dict__ 注入模拟已设置（getattr 兜底读取）
    agent.session.service_state.__dict__["tts_voice"] = "v2"
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    resp = await client.get("/api/tts/voices")
    data = await resp.json()
    assert resp.status == 200
    assert data["current"] == "v2"


@pytest.mark.service
@pytest.mark.asyncio
async def test_tts_voices_default_current_no_service_state(aiohttp_client):
    """service_state.tts_voice 未设置 → current 回退后端默认（MiniMax 按角色解析默认音色）。"""
    from unittest.mock import AsyncMock, patch

    from Module.tts.minimax_tts import MiniMaxTTSBackend
    agent = _make_app_agent()
    backend = MiniMaxTTSBackend(
        api_key="sk-test", base_url="https://api.minimaxi.com", voice_id="fallback-voice",
    )
    agent._tts = backend  # noqa: SLF001 — 测试注入后端
    agent._tts_enabled = True  # noqa: SLF001
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    # list_voices 走真实现会触网 POST /v1/get_voice → patch 掉，专注验证 current 默认回退
    with patch.object(MiniMaxTTSBackend, "list_voices", AsyncMock(return_value=[])):
        resp = await client.get("/api/tts/voices")
        data = await resp.json()
    assert resp.status == 200
    # 角色 Elysia 在 minimax_voices.yaml 映射为 elysia_test_001 → 默认 current 为该映射音色
    assert data.get("current") == "elysia_test_001"
