"""TTS 声音 REST 路由（2026-08-14 批次 C 从 api_routes 拆出）。

模块级 handler（对齐 session_routes 模式），末尾 register_<group>_routes(app)。
"""

import logging

from aiohttp import web

from core.input.tts_profile import _active_tts_profile, _backend_default_voice
from frontend_contract import rest as rest_contract

logger = logging.getLogger(__name__)


async def tts_voices_handler(request: web.Request) -> web.Response:
    """GET /api/tts/voices — 活动会话激活 TTS profile 的声音列表。

    - TTS 未启用（_active_tts_profile → None）→ 200 ``{voices: [], current: None,
      source: "static", error: "TTS 未启用"}``（不炸，前端可据此隐藏声音面板）。
    - 否则 ``list_voices()``；source = api（MiniMax 动态拉取）/ static（静态声明）。
    - current = 活动会话 ``service_state.tts_voice``（Task 11 引入后）或后端默认音色。
    """
    agent = request.app.get("agent")
    backend = _active_tts_profile(agent)
    if backend is None:
        return web.json_response(rest_contract.TtsVoicesResponse(
            voices=[], current=None, source="static", error="TTS 未启用").wire_dict())
    try:
        voices = await backend.list_voices()
    except Exception as e:
        # 后端 list_voices 已容错（MiniMax 失败 → []），这里兜底防未知异常
        logger.warning("TTS list_voices 失败: %s", e)
        voices = []
    source = "api" if getattr(backend, "provider_name", "") == "minimax" else "static"
    character = getattr(getattr(agent, "character", None), "name", "") or ""
    # 防御：双跳守卫（与 tts_preview_handler 同款）——session 可能为 None（测试 Mock 会话）
    svc_state = getattr(getattr(agent, "session", None), "service_state", None)
    current = (getattr(svc_state, "tts_voice", None) if svc_state is not None else None) \
        or _backend_default_voice(backend, character)
    # F10：current 缺失时追加到列表，保证 current 恒 ∈ voices[]（角色默认音色等不在 list_voices 中）。
    # backend 原始 dict 先定型为契约模型（TtsVoiceEntry）再检查/追加——voices（list[dict]）仅内部消费。
    entries = [rest_contract.TtsVoiceEntry(**v) for v in voices]
    if current and not any(e.id == current for e in entries):
        entries.append(rest_contract.TtsVoiceEntry(id=current, name=current))
    return web.json_response(rest_contract.TtsVoicesResponse(
        voices=entries, current=current or None, source=source).wire_dict())


async def tts_preview_handler(request: web.Request) -> web.Response:
    """POST /api/tts/preview — 固定短文本音色试听（不读聊天）。

    body ``{voice_id?: str}``；合成 ``_PREVIEW_TEXT``（模块级固定常量）。
    - TTS 未启用（_active_tts_profile → None）→ ``{ok: False, error: "TTS 未启用"}``。
    - 否则 ``synthesize(_PREVIEW_TEXT, character=<角色名>, voice_id=voice_id)``
      → ``{ok: True, mime, audio_base64}``；异常 → ``{ok: False, error}``。
    """
    agent = request.app.get("agent")
    # body 容错：缺省/非法 JSON → 空 dict（voice_id 可选）
    try:
        payload = await request.json()
    except web.HTTPRequestEntityTooLarge:
        raise  # chunked 超限放行——交中间件统一 413 信封（F13）
    except Exception:
        payload = {}
    voice_id = payload.get("voice_id") if isinstance(payload, dict) else None
    if not voice_id:
        # review 修正：缺省 voice_id 回退活动会话当前 voice（service_state.tts_voice）——
        # 与 /api/tts/voices 的 current 一致，试听听到的是 bot 实际会用的声音。
        svc_state = getattr(getattr(agent, "session", None), "service_state", None)
        voice_id = getattr(svc_state, "tts_voice", None) if svc_state is not None else None

    backend = _active_tts_profile(agent)
    if backend is None:
        return web.json_response(rest_contract.TtsPreviewResult(
            ok=False, error="TTS 未启用").wire_dict())
    try:
        character = getattr(getattr(agent, "character", None), "name", "") or ""
        audio = await backend.synthesize(
            _PREVIEW_TEXT, character=character, voice_id=voice_id,
        )
        mime = f"audio/{getattr(backend, 'stream_format', 'wav')}"
        audio_base64 = base64.b64encode(audio).decode("utf-8")
        return web.json_response(rest_contract.TtsPreviewResult(
            ok=True, mime=mime, audio_base64=audio_base64).wire_dict())
    except Exception as e:
        logger.warning("TTS 试听合成失败: %s", e)
        return web.json_response(rest_contract.TtsPreviewResult(
            ok=False, error=str(e)).wire_dict())



import base64

_PREVIEW_TEXT = "你好，我是声音测试。"

def register_tts_routes(app: web.Application) -> None:
    """注册 TTS 声音路由（2 端点）。"""
    app.router.add_get("/api/tts/voices", tts_voices_handler)
    app.router.add_post("/api/tts/preview", tts_preview_handler)
