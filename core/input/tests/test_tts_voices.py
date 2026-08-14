"""/api/tts/voices + /api/tts/preview 端点测试（Task 9 BE-051 声音列表 + Task 10 试听）。

覆盖 voices：
- TTS 启用 + 后端激活 → 200 含 voices（source=api for MiniMax / static 其余）；
- TTS 未启用 → 200 空列表 + error="TTS 未启用"（不炸）；
- current 随活动会话 service_state.tts_voice。

覆盖 preview：
- 激活后端 synthesize 返回 bytes → 200 {ok: True, mime, audio_base64} + 固定文本（非聊天）与 voice_id；
- voice_id 缺省 → synthesize 不带 voice_id；
- 后端抛错 → {ok: False, error}；
- TTS 未启用 → {ok: False, error: "TTS 未启用"}。
"""
import base64

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

    def default_voice(self, character: str = "") -> str:
        """对齐 TTSBackend.default_voice（假后端无 voice map → 空串）。"""
        return ""


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
async def test_tts_voices_current_not_in_list_gets_appended(aiohttp_client):
    """F10：current 不在列表 → 追加保证 current 恒 ∈ voices[]。"""
    agent = _make_app_agent()
    agent._tts = _FakeBackend([{"id": "alloy"}], provider_name="openai")  # noqa: SLF001
    agent._tts_enabled = True  # noqa: SLF001
    agent.session.service_state.tts_voice = "elysia"  # noqa: SLF001 — 注入 current（不在列表）
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    resp = await client.get("/api/tts/voices")
    data = await resp.json()
    ids = [v["id"] for v in data["voices"]]
    assert data["current"] == "elysia"
    assert "elysia" in ids         # 追加保证 current 在列表
    assert len(ids) == 2           # 原 1 + 追加 1


@pytest.mark.service
@pytest.mark.asyncio
async def test_tts_voices_current_in_list_not_duplicated(aiohttp_client):
    """F10：current 已在列表 → 不重复追加。"""
    agent = _make_app_agent()
    agent._tts = _FakeBackend([{"id": "elysia"}], provider_name="openai")  # noqa: SLF001
    agent._tts_enabled = True  # noqa: SLF001
    agent.session.service_state.tts_voice = "elysia"  # noqa: SLF001 — current 已在列表
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    resp = await client.get("/api/tts/voices")
    data = await resp.json()
    ids = [v["id"] for v in data["voices"]]
    assert data["current"] == "elysia"
    assert ids.count("elysia") == 1   # 不重复追加


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


# ────────────────────── POST /api/tts/preview（Task 10 试听） ──────────────────────


class _FakePreviewBackend:
    """可注入的假 TTS 后端：synthesize 记录调用并返回固定 bytes，stream_format 可配。"""

    def __init__(self, audio: bytes = b"RIFF\x00fake-wav", stream_format: str = "wav"):
        self._audio = audio
        self.stream_format = stream_format
        self.calls: list[dict] = []
        self.fail = False

    async def synthesize(self, text, *, character, emotion=None, voice_id=None):
        if self.fail:
            raise RuntimeError("合成失败")
        self.calls.append({"text": text, "character": character, "voice_id": voice_id})
        return self._audio


@pytest.mark.service
@pytest.mark.asyncio
async def test_tts_preview_ok_fixed_text_and_voice_id(aiohttp_client):
    """激活后端 synthesize 返回 bytes → 200 {ok: True, mime, audio_base64}；收固定文本（非聊天）+ voice_id。"""
    from core.input.tts_routes import _PREVIEW_TEXT

    agent = _make_app_agent()
    audio = b"\x00\x01\x02fake-wav-bytes"
    backend = _FakePreviewBackend(audio=audio)
    agent._tts = backend  # noqa: SLF001 — 测试注入后端
    agent._tts_enabled = True  # noqa: SLF001 — 测试注入有效开关
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    resp = await client.post("/api/tts/preview", json={"voice_id": "elysia_test_001"})
    data = await resp.json()
    assert resp.status == 200
    assert data["ok"] is True
    assert data["mime"] == "audio/wav"
    assert data["audio_base64"] == base64.b64encode(audio).decode()
    # 合成收到固定试听文本（非聊天内容）+ voice_id 透传 + 角色名
    assert backend.calls == [{
        "text": _PREVIEW_TEXT,
        "character": "Elysia",
        "voice_id": "elysia_test_001",
    }]
    # 常量逐字匹配 plan（防实现时改值）
    assert _PREVIEW_TEXT == "你好，我是声音测试。"


@pytest.mark.service
@pytest.mark.asyncio
async def test_tts_preview_no_voice_id_default(aiohttp_client):
    """body 缺省 voice_id → synthesize 不带 voice_id（None）。"""
    from core.input.tts_routes import _PREVIEW_TEXT

    agent = _make_app_agent()
    backend = _FakePreviewBackend()
    agent._tts = backend  # noqa: SLF001
    agent._tts_enabled = True  # noqa: SLF001
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    resp = await client.post("/api/tts/preview", json={})
    data = await resp.json()
    assert resp.status == 200
    assert data["ok"] is True
    assert backend.calls == [{
        "text": _PREVIEW_TEXT, "character": "Elysia", "voice_id": None,
    }]


@pytest.mark.service
@pytest.mark.asyncio
async def test_tts_preview_no_voice_falls_back_to_current(aiohttp_client):
    """review 修正：body 缺省 voice_id 且活动会话 tts_voice 已设置 → synthesize 收到该 voice
    （与 /api/tts/voices 的 current 一致，试听听到的是 bot 实际会用的声音）。"""
    from core.input.tts_routes import _PREVIEW_TEXT

    agent = _make_app_agent()
    backend = _FakePreviewBackend()
    agent._tts = backend  # noqa: SLF001
    agent._tts_enabled = True  # noqa: SLF001
    agent.session.service_state.tts_voice = "v-current"
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    resp = await client.post("/api/tts/preview", json={})
    data = await resp.json()
    assert resp.status == 200
    assert data["ok"] is True
    assert backend.calls == [{
        "text": _PREVIEW_TEXT, "character": "Elysia", "voice_id": "v-current",
    }]


@pytest.mark.service
@pytest.mark.asyncio
async def test_tts_preview_backend_error(aiohttp_client):
    """后端 synthesize 抛错 → 200 {ok: False, error}（不炸）。"""
    agent = _make_app_agent()
    backend = _FakePreviewBackend()
    backend.fail = True
    agent._tts = backend  # noqa: SLF001
    agent._tts_enabled = True  # noqa: SLF001
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    resp = await client.post("/api/tts/preview", json={"voice_id": "v1"})
    data = await resp.json()
    assert resp.status == 200
    assert data["ok"] is False
    assert data["error"]  # 非空错误信息
    assert data.get("audio_base64") is None


@pytest.mark.service
@pytest.mark.asyncio
async def test_tts_preview_disabled(aiohttp_client):
    """TTS 未启用（_active_tts_profile → None）→ {ok: False, error: "TTS 未启用"}。"""
    agent = _make_app_agent()
    agent._tts = None  # noqa: SLF001
    agent._tts_enabled = False  # noqa: SLF001
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)

    resp = await client.post("/api/tts/preview", json={"voice_id": "v1"})
    data = await resp.json()
    assert resp.status == 200
    assert data["ok"] is False
    assert data["error"] == "TTS 未启用"
    assert data.get("audio_base64") is None
