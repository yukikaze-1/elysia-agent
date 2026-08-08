"""业务 API 路由 — Providers, 图片上传, Chat REST, 消息归档, Desktop 注册表/附件。"""

import asyncio
import base64
import hashlib
import logging
import os
import re
import time
import uuid
from pathlib import Path

import filetype
from aiohttp import web
from aiohttp.multipart import BodyPartReader
from typing import Any, TYPE_CHECKING, cast

if TYPE_CHECKING:
    from core.agent import Agent

from core.schema.message import Message
from core.input.tts_profile import _active_tts_profile, _backend_default_voice
from frontend_contract import rest as rest_contract
from frontend_contract.rest import RepoCallStatus

logger = logging.getLogger(__name__)

# TTS 试听固定短文本（Task 10，BE-051）：永不读聊天内容，仅用于音色试听合成。
_PREVIEW_TEXT = "你好，我是声音测试。"

# ---- 通用上传（批次 7，spec §4.1）：filetype 魔数白名单 + 大小上限 ----

_ALLOWED_KINDS = ("image", "video", "audio", "file")

# 各 kind 大小上限（字节）；归位后按最终 kind 复核（Minor #6）
_KIND_LIMITS = {
    "image": 10 * 1024 * 1024,
    "video": 50 * 1024 * 1024,
    "audio": 20 * 1024 * 1024,
    "file": 20 * 1024 * 1024,
}

# 非 webm mime → 唯一 kind（webm 歧义走客户端 kind tiebreaker）
_MIME_KIND = {
    "image/png": "image",
    "image/jpeg": "image",
    "image/gif": "image",
    "image/webp": "image",
    "video/mp4": "video",
    "audio/x-wav": "audio",
    "audio/mpeg": "audio",
    "audio/ogg": "audio",
    "application/pdf": "file",
    "application/zip": "file",
    "text/plain": "file",
    "text/markdown": "file",
    "application/json": "file",
}

# mime → 允许扩展名集合（扩展名一致性校验，堵改名绕过）
_MIME_EXTS = {
    "image/png": {"png"},
    "image/jpeg": {"jpg", "jpeg"},
    "image/gif": {"gif"},
    "image/webp": {"webp"},
    "video/mp4": {"mp4"},
    "video/webm": {"webm"},
    "audio/x-wav": {"wav"},
    "audio/mpeg": {"mp3"},
    "audio/ogg": {"ogg"},
    "application/pdf": {"pdf"},
    "application/zip": {"zip"},
    "text/plain": {"txt"},
    "text/markdown": {"md"},
    "application/json": {"json"},
}

# 文本类扩展名（带点）→ mime（spec §4.1 I6）：filetype 魔数对纯文本/markdown/JSON
# 返回 None → 走扩展名白名单 + UTF-8 内容 sanity，而非直接 415。
_TEXT_EXT_TO_MIME = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".json": "application/json",
}


def _sign_url(secret: str, attachment_id: str) -> tuple[str, int]:
    """HMAC 签名 URL（spec §3.3）。返回 (url, expires_unix)。"""
    import hmac

    expires_unix = int(time.time()) + 86400  # 24h
    sig = hmac.new(secret.encode(), f"{attachment_id}:{expires_unix}".encode(),
                   hashlib.sha256).hexdigest()
    url = f"/api/attachments/{attachment_id}?sig={sig}&exp={expires_unix}"
    return url, expires_unix


# ---- 共享 helper（debug_routes.py 也需要） ----

def _archive_to_client(msgs: list[Message]) -> list[dict]:
    """将 Message 列表转为前端兼容格式（补 parts + _id 字段）。

    Args:
        msgs: Message 对象列表。

    Returns:
        list[dict]: 前端兼容的消息字典列表，每项包含 _id、parts 等字段。
    """
    result = []
    for msg in msgs:
        entry = msg.model_dump(mode="json")
        mongo_id = entry.get("metadata", {}).pop("_mongo_id", None)
        if mongo_id:
            entry["_id"] = mongo_id
        content = entry.get("content", [])
        if isinstance(content, list):
            entry["parts"] = [
                {"content": p["content"]}
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
        elif isinstance(content, str):
            entry["parts"] = [{"content": content}]
        else:
            entry["parts"] = [{"content": ""}]
        result.append(entry)
    return result


def _archive_msg_to_contract(d: dict) -> rest_contract.Message | None:
    """客户端归档消息 dict → 契约 Message 视图（block_index → content_type 归一化）。

    保留 _archive_to_client 的全部输出键（parts/_id/timestamp 等，前端分页游标与渲染依赖），
    仅重命名 block_index 键对齐 session 回放 wire（SessionMessageItem.content_type）。
    单条校验失败返回 None（调用方丢弃该条并告警，避免整页归档加载失败——与 Task 3 M2
    逐条契约化兜底同一策略）。
    """
    wire = dict(d)
    if "block_index" in wire:
        wire["content_type"] = wire.pop("block_index")
    try:
        return rest_contract.Message.model_validate(wire)
    except Exception:
        logger.warning("Failed to contract-validate archive message: %s", d.get("message_id", "?"))
        return None


def _custom_profile_in_use(agent: "Agent", service: str, name: str) -> bool:
    """判定 custom/{name} Profile 是否正被使用（活动会话 service_state / config / vision）。

    批次 5（Task 6）删除激活约束：活动会话 service_state.{svc}_model、config.{svc}.model、
    vision.model（LLM 场景——vision 工具走 LLM provider）任一以 custom/{name} 开头 →
    该 Profile 正被使用 → 409 PROFILE_IN_USE 拒绝删除。

    Args:
        agent: Agent 组合根（读 agent.session.service_state + agent.config）。
        service: "llm"/"stt"/"tts"。
        name: 自定义 provider 名（无 custom/ 前缀）。

    Returns:
        bool: True = 正在使用。
    """
    prefix = f"custom/{name}"
    if service == "llm":
        candidates = (
            agent.session.service_state.llm_model,
            agent.config.llm.model,
            agent.config.vision.model,
        )
    elif service == "stt":
        candidates = (agent.session.service_state.stt_model, agent.config.stt.model)
    else:  # tts
        candidates = (agent.session.service_state.tts_model, agent.config.tts.model)
    return any(c.startswith(prefix) for c in candidates if c)


async def _run_usable_test(backend: Any, service: str, request: web.Request,
                           body: dict) -> dict:
    """usable 级真实产出验证。LLM 发文本、TTS 合成音频、STT 转写样例。超时 30s。

    STT 成功标准为"transcribe 调用无异常"——内置 tone 样例未必产生非空文本，
    但能证明服务接受并处理音频（真实语音样本会返回文本，detail 报告实际识别结果）。
    """
    start = time.perf_counter()

    if service == "llm":
        text = await asyncio.wait_for(
            backend.chat([{"role": "user", "content": "你好，请回一句话"}]),
            timeout=30.0,
        )
        ok = bool(text.strip())
        return {
            "ok": ok,
            "latency_ms": int((time.perf_counter() - start) * 1000),
            "detail": text.strip()[:20] if ok else "",
        }

    if service == "tts":
        # character 缺省 → agent 角色名（agent.character.name，注意不是 config.agent.character——
        # 那是角色配置路径 "character/elysia/config.yaml"，传给 synthesize 当音色名必然失败）。
        # 该角色在 provider 无对应音色时后端抛"未知发音人"，detail 带出（spec 允许）。
        character = body.get("character", "")
        if not character:
            agent = request.app.get("agent")
            if agent is not None:
                character = getattr(getattr(agent, "character", None), "name", "") or ""
        audio = await asyncio.wait_for(
            backend.synthesize("你好，测试", character=character),
            timeout=30.0,
        )
        ok = bool(audio)
        return {
            "ok": ok,
            "latency_ms": int((time.perf_counter() - start) * 1000),
            "detail": f"audio {len(audio)} bytes" if ok else "",
        }

    # stt — 内置样例。路径走 ProjectPath（全项目路径唯一定义点，不用 Path(__file__) 硬编码跨目录）
    from paths import ProjectPath
    sample_path = ProjectPath.STT_SAMPLE_WAV.resolve()
    audio_bytes = sample_path.read_bytes()
    text = await asyncio.wait_for(backend.transcribe(audio_bytes), timeout=30.0)
    return {
        "ok": True,
        "latency_ms": int((time.perf_counter() - start) * 1000),
        "detail": text.strip()[:20],
    }


def _strip_secret_keys(data: dict) -> dict:
    """响应数据剔除 api_key 键（验收 LOW#1 加固）。

    save/delete 响应的 data 是 load() 原始 custom 列表（裸 dict，不经契约白名单），
    防遗留条目（批次 5 前误存过 api_key 字段）回显密钥——与 GET 经 model_validate
    白名单丢弃的行为同口径。只处理 llm/stt/tts 三个已知 service 键，其余透传。
    """
    for svc in ("llm", "stt", "tts"):
        for entry in data.get(svc, []):
            if isinstance(entry, dict):
                entry.pop("api_key", None)
    return data


def register_api_routes(app: web.Application) -> None:
    """注册所有业务 API 路由到 aiohttp Application。"""

    # ---- Provider 管理 ----

    async def providers_list_handler(request: web.Request) -> web.Response:
        """返回过滤后的 provider 列表（providers.yaml 单一来源 + env 过滤 + custom）。"""
        from core.input.custom_providers import load as load_custom
        from core.input.provider_registry import build_providers_response
        return web.json_response(
            rest_contract.ProvidersResponse.model_validate(
                build_providers_response(load_custom())).wire_dict())

    async def providers_save_handler(request: web.Request) -> web.Response:
        """保存一个新的自定义 provider（批次 5：runtime 门控 + SSRF + 密钥入 Store）。

        body: {service, entry:{name, base_url?, api_key?, api_key_env?, models?,
               runtime?, capabilities?}}。
        - runtime 非法枚举 → 400 PROFILE_INVALID_RUNTIME（ProtocolError）。
        - base_url 门控按 runtime：backend 缺 → v1 ApiFail（既有检查）；desktop/local
          缺 → 放行（D13）；backend → validate_endpoint（非空时，SSRF）。
        - 非 backend 带 api_key → 400 PROFILE_INVALID_RUNTIME（密钥仅 backend 后端执行可收）。
        - api_key → add_provider 内部直存 Secret Store（不入 YAML）；D18 清理。
        - 响应 = 全量 custom 列表（spec §3.3 步骤 8，前批次 wire 形状）；credentialConfigured
          经 GET /api/providers 获取（验收 F2：save 响应不含该布尔，避免偏离既有契约）。

        Returns:
            web.Response: {"ok": True, "data": 全量 custom 列表（api_key 键已剔除）} 或错误信封。
        """
        from core.input.custom_providers import add_provider
        from core.input.ssrf import SSRFError, validate_endpoint
        try:
            body = await request.json()
            service = body.get("service", "")
            entry = body.get("entry", {})
        except web.HTTPRequestEntityTooLarge:
            raise  # chunked 超限放行——交中间件统一 413 信封（F13）
        except Exception:
            return web.json_response(rest_contract.ApiFail(error="无效的请求体").wire_dict(), status=400)
        if service not in ("llm", "stt", "tts"):
            return web.json_response(rest_contract.ApiFail(error=f"未知 service: {service}").wire_dict(), status=400)
        if not entry.get("name"):
            return web.json_response(rest_contract.ApiFail(error="缺少 name 或 base_url").wire_dict(), status=400)
        # runtime 解析：非法枚举 → PROFILE_INVALID_RUNTIME（新错误码走 ProtocolError 信封）
        runtime = entry.get("runtime", "backend")
        if runtime not in ("backend", "desktop", "local_worker"):
            return _tool_error("PROFILE_INVALID_RUNTIME", f"非法 runtime: {runtime}", 400)
        base_url = entry.get("base_url", "")
        if runtime == "backend":
            # backend 缺 base_url → v1 ApiFail（既有检查）；非空时 SSRF 校验
            if not base_url:
                return web.json_response(rest_contract.ApiFail(error="缺少 name 或 base_url").wire_dict(), status=400)
            try:
                validate_endpoint(base_url)
            except SSRFError as e:
                return _tool_error("SSRF_BLOCKED", str(e), 400)
        elif entry.get("api_key"):
            # desktop/local 由 Electron 执行，后端不收密钥（D18）
            return _tool_error("PROFILE_INVALID_RUNTIME", "非 backend runtime 不接受 api_key", 400)
        try:
            data = add_provider(service, entry)
        except Exception as e:
            # Task 9（批次 8a）：信封只给通用文案，str(e) 只进日志——防绝对路径/内部细节泄漏
            logger.warning("保存 provider 失败: %s", e)
            return web.json_response(rest_contract.ApiFail(error="保存失败，请稍后重试").wire_dict(), status=500)
        # review 修正：响应恢复全量 custom 列表（spec §3.3 步骤 8 + 前批次 wire 形状），
        # 不再返回单条目——避免偏离既有契约；credentialConfigured 经 GET /api/providers 获取。
        # 验收 LOW#1 加固：响应数据剔除 api_key 键——防遗留条目（批次 5 前误存过 api_key
        # 字段）回显密钥（GET 经契约白名单丢弃、save/delete 是裸 dict，须同口径）。
        return web.json_response(rest_contract.ApiOk(data=_strip_secret_keys(data)).wire_dict())

    async def providers_delete_handler(request: web.Request) -> web.Response:
        """删除一个自定义 provider（批次 5：激活约束——活动会话/配置正在使用的不可删）。

        URL 路径参数包含 service 和 name。激活判定（_custom_profile_in_use）：活动会话
        service_state.{svc}_model / config.{svc}.model / vision.model 任一以 custom/{name}
        开头 → 409 PROFILE_IN_USE（ProtocolError）；非激活 → remove_provider（条目 +
        Secret Store 密钥一并清理）。

        Returns:
            web.Response: {"ok": True, "data": ...}、409 或 v1 ApiFail。
        """
        from core.input.custom_providers import remove_provider
        service = request.match_info.get("service", "")
        name = request.match_info.get("name", "")
        if service not in ("llm", "stt", "tts"):
            return web.json_response(rest_contract.ApiFail(error=f"未知 service: {service}").wire_dict(), status=400)
        agent = request.app.get("agent")
        if agent is not None and _custom_profile_in_use(agent, service, name):
            return _tool_error("PROFILE_IN_USE", f"custom/{name} 正在使用，无法删除", 409)
        try:
            data = remove_provider(service, name)
            # 验收 LOW#1：同 save，响应剔除 api_key 键（防遗留条目回显密钥）
            return web.json_response(rest_contract.ApiOk(data=_strip_secret_keys(data)).wire_dict())
        except Exception as e:
            # Task 9（批次 8a）：信封只给通用文案，str(e) 只进日志——防绝对路径/内部细节泄漏
            logger.warning("删除 provider 失败: %s", e)
            return web.json_response(rest_contract.ApiFail(error="删除失败，请稍后重试").wire_dict(), status=500)

    async def providers_test_handler(request: web.Request) -> web.Response:
        """测试 provider 连通性（level=connectivity）或可使用性（level=usable）。

        body: {service, provider, model, level, base_url?, api_key?, character?}
        参数解析顺序见 spec「/api/providers/test 扩展」：provider+model 均缺省 → 回退配置模型；
        仅 provider 缺省 → "custom"；level 缺省 → "connectivity"。
        base_url/api_key 优先级：请求体非空 > custom_providers.yaml > env。
        """
        try:
            body = await request.json()
        except web.HTTPRequestEntityTooLarge:
            raise  # chunked 超限放行——交中间件统一 413 信封（F13）
        except Exception:
            return web.json_response(rest_contract.ApiFail(error="无效的请求体").wire_dict(), status=400)

        service = body.get("service", "")
        if service not in ("llm", "stt", "tts"):
            return web.json_response(rest_contract.ApiFail(error=f"未知 service: {service}").wire_dict(), status=400)

        level = body.get("level", "connectivity")
        provider = body.get("provider", "")
        model = body.get("model", "")
        base_url = body.get("base_url", "")
        api_key = body.get("api_key", "")

        # provider+model 均缺省 → 回退该服务当前模型（完整 provider/model，直接 split）
        if not provider and not model:
            agent = request.app.get("agent")
            if agent is None:
                return web.json_response(rest_contract.ApiFail(error="agent 未注入").wire_dict(), status=500)
            if service == "llm":
                model_id = agent.llm.model
            elif service == "tts":
                # disabled 态即 agent.tts is None——必须守卫，否则在 Factory.create 前抛 AttributeError
                tts = agent.tts
                model_id = tts.model if tts is not None else agent.config.tts.model
            else:  # stt
                model_id = agent.config.stt.model
            try:
                provider, model = model_id.split("/", 1)
            except ValueError:
                return web.json_response(rest_contract.ApiFail(error=f"当前模型格式无效: {model_id}").wire_dict())
        elif not provider:
            provider = "custom"

        # base_url/api_key：请求体非空 > custom_providers.yaml（resolve_model_params）
        # runtime 仅 custom 条目有（内置 resolve 返回 {} → 缺省 backend，容错）。
        runtime = "backend"
        if provider == "custom":
            from core.input.custom_providers import resolve_model_params
            params = resolve_model_params(service, f"custom/{model}")
            runtime = params.get("runtime", "backend")
            if not base_url:
                base_url = params.get("base_url", "")
            if not api_key:
                api_key = params.get("api_key", "")

        # runtime 守卫（review 修正：先于 SSRF——desktop/local 允许本地 base_url，
        # 若先 SSRF 会误报 SSRF_BLOCKED；后端不代理本机 Profile，直接 400）
        if runtime != "backend":
            return _tool_error("PROFILE_RUNTIME_NOT_BACKEND", "该 Profile 由 Electron 执行，后端不发起测试", 400)

        # body base_url 非空时无论 provider 一律 SSRF 校验（空 base_url 跳过——
        # 内置无 body base_url 是空串，不误伤既有 connectivity 测试）。
        from core.input.ssrf import SSRFError, validate_endpoint
        if base_url:
            try:
                validate_endpoint(base_url)
            except SSRFError as e:
                return _tool_error("SSRF_BLOCKED", str(e), 400)

        create_kwargs = {}
        if base_url:
            create_kwargs["base_url"] = base_url
        if api_key:
            create_kwargs["api_key"] = api_key

        # custom+backend → 注入出站守卫**工厂**（review 修正：工厂而非实例——create
        # 失败时不产生待清理 client；后端关闭连带关闭守卫 client，finally 无需单独 aclose）
        if provider == "custom" and base_url:
            from core.input.ssrf import build_guarded_http_client
            create_kwargs["http_client_factory"] = (
                lambda b=base_url: build_guarded_http_client(b))

        try:
            if service == "llm":
                from Module.llm import LLMFactory
                backend = LLMFactory.create(f"{provider}/{model}", **create_kwargs)
            elif service == "stt":
                from Module.stt import STTFactory
                backend = STTFactory.create(f"{provider}/{model}", **create_kwargs)
            else:
                from Module.tts import TTSFactory
                backend = TTSFactory.create(f"{provider}/{model}", **create_kwargs)
        except Exception as e:
            return web.json_response(rest_contract.ApiFail(error=str(e)).wire_dict())

        try:
            if level == "usable":
                result = await _run_usable_test(backend, service, request, body)
            else:
                result = await asyncio.wait_for(backend.test(), timeout=5.0)
            return web.json_response(rest_contract.ProviderTestResult(**result).wire_dict())
        except asyncio.TimeoutError:
            return web.json_response(rest_contract.ProviderTestResult(ok=False, error="测试超时").wire_dict())
        except Exception as e:
            return web.json_response(rest_contract.ProviderTestResult(ok=False, error=str(e)).wire_dict())
        finally:
            try:
                await backend.close()
            except Exception:
                pass

    app.router.add_get("/api/providers", providers_list_handler)
    app.router.add_post("/api/providers", providers_save_handler)
    app.router.add_delete("/api/providers/{service}/{name}", providers_delete_handler)
    app.router.add_post("/api/providers/test", providers_test_handler)

    # ---- TTS 声音（BE-051，Task 9 / Task 10） ----

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

    app.router.add_get("/api/tts/voices", tts_voices_handler)

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

    app.router.add_post("/api/tts/preview", tts_preview_handler)

    # ---- 图片上传 ----

    from paths import ProjectPath

    _IMAGES_DIR = ProjectPath.DATA_UPLOADS_IMAGES.resolve()

    async def image_upload_handler(request: web.Request) -> web.Response:
        """处理图片上传请求。

        Args:
            request: aiohttp Request，multipart form data 含图片字段。

        Returns:
            web.Response: {"url": ..., "mime": ..., "size": ...} 或错误信息。
        """
        import uuid as _uuid
        import os as _os

        agent = request.app["agent"]
        cfg = agent.config.server
        mb = cfg.image_max_mb
        max_bytes = (mb if mb is not None and mb > 0 else 10) * 1024 * 1024
        allowed = cfg.image_allowed_types or ["image/png", "image/jpeg", "image/gif", "image/webp"]

        try:
            reader = await request.multipart()
            field = await reader.next()
            if field is None:
                return web.json_response(
                    rest_contract.ImageUploadError(error="未找到图片字段").wire_dict(), status=400)

            # read_chunk() 逐块读取以限制内存——aiohttp 3.x 的 BodyPartReader.read()
            # 不接受位置 size 参数（仅关键字 decode），整块 read() 会把超大 body
            # 全部读入内存（multipart 原始流不受 client_max_size 约束，属 DoS 向量）。
            # 读到超限即中断返回 413，避免继续读完整个 body。
            field = cast(BodyPartReader, field)
            data = bytearray()
            while not field.at_eof():
                data.extend(await field.read_chunk(max_bytes + 1))
                if len(data) > max_bytes:
                    return web.json_response(
                        rest_contract.ImageUploadError(
                            error=f"图片过大，上限 {max_bytes // 1024 // 1024}MB").wire_dict(),
                        status=413)

            filename = getattr(field, "filename", "") or "image.png"
            ext = _os.path.splitext(filename)[1].lower()
            ext_to_mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                           ".gif": "image/gif", ".webp": "image/webp"}
            mime = ext_to_mime.get(ext, "image/png")

            if mime not in allowed:
                return web.json_response(
                    rest_contract.ImageUploadError(error=f"不支持的图片类型: {mime}").wire_dict(),
                    status=415)

            _os.makedirs(str(_IMAGES_DIR), exist_ok=True)
            saved_name = f"{_uuid.uuid4().hex[:12]}{ext}"
            saved_path = _IMAGES_DIR / saved_name
            saved_path.write_bytes(data)

            url = f"/uploads/images/{saved_name}"
            return web.json_response(
                rest_contract.ImageUploadResponse(url=url, mime=mime, size=len(data)).wire_dict())
        except web.HTTPRequestEntityTooLarge:
            raise  # chunked 超限放行——交中间件统一 413 信封（F13）
        except Exception as e:
            # Task 9（批次 8a）：信封只给通用文案，str(e) 只进日志——防绝对路径/内部细节泄漏
            logger.warning("图片上传失败: %s", e)
            return web.json_response(rest_contract.ImageUploadError(error="图片上传失败，请稍后重试").wire_dict(), status=500)

    app.router.add_post("/api/images", image_upload_handler)

    # ---- 通用上传（批次 7，spec §4.1，BE-070） ----

    async def upload_handler(request: web.Request) -> web.Response:
        """POST /api/uploads — 通用附件上传（多文件 + kind 意图）。

        multipart 字段：files[]（多个）+ kind（可选，客户端意图**不信任**）。
        校验顺序（spec §4.1）：kind 枚举预检（Task 14：read_chunk(65) 限长 64，超长即拒
        不读入内存）→ 数量 ≤10 → 每文件 filetype 魔数白名单（文本类 txt/md/json
        无魔数 → 扩展名白名单 + UTF-8 sanity，I6）→ 扩展名一致性 → kind 归位
        （webm 以客户端 kind tiebreaker）+ 归位后大小复核 → 写盘
        data/uploads/{kind}/{uuid}.{ext} + sha256 + attachments 落记录
        （fail-closed 离线 503，失败删已落盘文件不留孤儿）→ AttachmentFileResolver.register。
        Task 14 两阶段：先全部校验（阶段 A，失败不写盘）→ 再统一写盘/建记录/注册
        （阶段 B，任一失败清理本轮已写文件 + 已建记录 + 已注册映射，不留孤儿）。
        整体聚合上限 64MB：有 Content-Length 由 payload 中间件预检（server.py
        _make_payload_middleware），chunked（无 Content-Length）由本 handler 的
        total_bytes 累计强制（Critical #4）。
        """
        from core.input.server import _protocol_error_response
        from core.schema.adapters import attachment_resolver
        from core.schema.utils import now_iso
        from core.storage.attachment_repo import STATUS_ACTIVE, StorageUnavailableError
        from core.utils.message_id import _new_id

        agent = request.app["agent"]
        cfg = agent.config.server
        sig_secret = cfg.attachment_sig_secret or ""
        access_token_cfg = cfg.access_token or ""
        repo = agent.attachment_repo
        # 聚合上限（spec §4.1 Critical #4 强制）：chunked（无 Content-Length）时中间件
        # Content-Length 预检失效 → handler 内累计字节强制整体 ≤ cms。取 agent 配置同源
        # （create_app 的 client_max_size 也是它，缺省 64MB）。is not None 语义：0 → 0
        # 不限（与 server cms 同步），None/缺省 → 64MB（falsy 回落会误吞 0）。
        cms = cfg.max_request_bytes
        if cms is None:
            cms = 64 * 1024 * 1024

        # 收集 multipart 字段（kind 文本 + files[] 文件；顺序无关，FormData 不保证）
        kind = ""
        files: list[tuple[str, bytes]] = []  # (原始文件名, 内容)
        # 收集阶段 read_chunk 上限取各 kind 上限最大值（50MB）——kind 缺省分类前防 DoS
        max_read = max(_KIND_LIMITS.values())
        total_bytes = 0  # 聚合字节累计（多文件跨 read_chunk 相加，超 cms 即中断）
        try:
            reader = await request.multipart()
        except web.HTTPRequestEntityTooLarge:
            raise  # chunked 超限放行——交中间件统一 413 信封（F13）
        except Exception:
            return _protocol_error_response("INVALID_REQUEST", "无效的 multipart 请求", 400)
        while True:
            try:
                field = await reader.next()
            except web.HTTPRequestEntityTooLarge:
                raise  # chunked 超限放行——交中间件统一 413 信封（F13）
            except Exception:
                return _protocol_error_response("INVALID_REQUEST", "multipart 解析失败", 400)
            if field is None:
                break
            field = cast(BodyPartReader, field)
            name = field.name or ""
            if name == "kind":
                # kind 限长（Task 14）：read_chunk(65) 读上限 65 字节才能检出 >64 超长
                # （read() 整读无上限）——不读入超长字段内存即拒，限长分支在未知 kind
                # 预检之前。
                raw = await field.read_chunk(65)
                if len(raw) > 64:
                    return _protocol_error_response("INVALID_REQUEST", "kind 字段超长", 400)
                if isinstance(raw, (bytes, bytearray)):
                    kind = bytes(raw).decode("utf-8", errors="replace").strip()
                else:
                    kind = str(raw or "").strip()
            elif name.startswith("files"):
                # read_chunk 逐块读取自限内存（每文件 ≤50MB，整体由 total_bytes 累计兜底）
                data = bytearray()
                while not field.at_eof():
                    data.extend(await field.read_chunk(max_read + 1))
                    if len(data) > max_read:
                        return _protocol_error_response(
                            "REQUEST_TOO_LARGE", "单个文件过大", 413)
                total_bytes += len(data)
                # cms=0 不限（与 app 级 client_max_size 同源语义）；cms>0 时累计强制整体上限
                if cms and total_bytes > cms:
                    # 聚合超限：立即 413 中断，不继续读完 body（chunked 无 Content-Length
                    # 时中间件拦不住，这里才是整体上限的强制点）
                    return _protocol_error_response(
                        "REQUEST_TOO_LARGE", "整体上传超过聚合上限", 413)
                files.append((getattr(field, "filename", "") or "attachment", bytes(data)))
            # 其他字段忽略

        # 步骤 0：kind 枚举预检（缺省通过；sticker 与任意未知 kind 一视同仁）
        if kind and kind not in _ALLOWED_KINDS:
            return _protocol_error_response(
                "CONTRACT_VALIDATION", f"未知 kind: {kind}", 400)
        # 步骤 1：数量 ≤10
        if len(files) > 10:
            return _protocol_error_response("TOO_MANY_FILES", "单次上传文件数超限（≤10）", 400)

        # ---- 阶段 A：先全部校验（步骤 2-5：尺寸/魔数/扩展名/kind 归位）——
        # 任一失败立即 4xx，此时未写盘、未建记录，无需清理（Task 14 两阶段）。
        validated: list[dict] = []
        for filename_raw, data in files:
            # 步骤 2：初步大小（kind 缺省跳过此步，仅以归位 kind 复核）
            if kind and len(data) > _KIND_LIMITS[kind]:
                return _protocol_error_response(
                    "REQUEST_TOO_LARGE", f"文件过大，超过 {kind} 上限", 413)
            # 文件扩展名（步骤 3/4 复用；带点形式供文本白名单查表）
            ext_raw = os.path.splitext(filename_raw)[1].lower()
            ext = ext_raw.lstrip(".")
            # 步骤 3：类型判定（不信任扩展名/content_type，堵伪造）
            ft = filetype.guess(data)
            if ft is None:
                # 文本类（txt/md/json）无魔数：扩展名白名单 + UTF-8 内容 sanity（spec §4.1 I6）。
                # 非文本扩展名且魔数未知 → 仍 415。
                text_mime = _TEXT_EXT_TO_MIME.get(ext_raw)
                if text_mime is None:
                    return _protocol_error_response("UNSUPPORTED_MEDIA_TYPE", "不支持的文件类型", 415)
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError:
                    # UTF-8 sanity 失败（内容非合法 UTF-8 文本）→ 拒绝
                    return _protocol_error_response("UNSUPPORTED_MEDIA_TYPE", "不支持的文件类型", 415)
                mime = text_mime
            else:
                mime = ft.mime
                if mime != "video/webm" and mime not in _MIME_KIND:
                    return _protocol_error_response("UNSUPPORTED_MEDIA_TYPE", "不支持的文件类型", 415)
            # 步骤 4：扩展名与判定一致（堵改名绕过；jpg/jpeg 归一化；文本类同样受白名单约束）
            if ext not in _MIME_EXTS.get(mime, set()):
                return _protocol_error_response(
                    "UNSUPPORTED_MEDIA_TYPE", "扩展名与文件内容不一致", 415)
            # 步骤 5：kind 归位 + 归位后大小复核（webm tiebreaker）
            if mime == "video/webm":
                final_kind = "audio" if kind == "audio" else "video"
            else:
                final_kind = _MIME_KIND[mime]
            if len(data) > _KIND_LIMITS[final_kind]:
                return _protocol_error_response(
                    "REQUEST_TOO_LARGE", f"文件过大，超过 {final_kind} 上限", 413)
            # filename 脱敏（Minor 9）：去路径分隔符、截断 255、剥控制字符；仅展示用
            filename = re.sub(r"[\\/]", "", filename_raw)
            filename = re.sub(r"[\x00-\x1f\x7f]", "", filename)
            filename = filename[:255] or "attachment"
            # 步骤 6 前半段：预计算写盘信息 + 记录文档（真正落盘留阶段 B 统一做）
            stored_name = f"{uuid.uuid4().hex[:16]}.{ext}"
            upload_dir = ProjectPath.DATA_UPLOADS.resolve() / final_kind
            stored_path = upload_dir / stored_name
            attachment_id = _new_id("att")
            doc = {
                "attachment_id": attachment_id,
                "kind": final_kind,
                "mime": mime,
                "size": len(data),
                "filename": filename,
                "stored_name": stored_name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "ref_count": 0,
                "refs": [],
                "status": STATUS_ACTIVE,
                "created_at": now_iso(),
            }
            validated.append({
                "data": data,
                "stored_path": stored_path,
                "stored_name": stored_name,
                "final_kind": final_kind,
                "mime": mime,
                "attachment_id": attachment_id,
                "doc": doc,
            })

        # ---- 阶段 B：统一持久化（全部校验通过后才写盘 + 建记录 + 注册）——
        # 任一文件失败 → 清理本轮已写文件 + 已建记录 + 已注册映射（不留孤儿）。
        class _PersistAbort(Exception):
            """持久化中止信号：携带公开错误信封字段（str(e) 只进日志）。"""

            def __init__(self, code: str, message: str, status: int,
                         retryable: bool = False) -> None:
                super().__init__(message)
                self.code = code
                self.message = message
                self.status = status
                self.retryable = retryable

        results = []
        written: list[Path] = []   # 已写盘文件（失败清理用）
        created: list[str] = []    # 已建记录 attachment_id（失败清理用）

        async def _cleanup() -> None:
            """失败清理：删本轮已写文件 + 已注册映射 + 已建记录。"""
            for stored_path in written:
                stored_path.unlink(missing_ok=True)
            for aid in created:
                attachment_resolver.unregister(aid)
                try:
                    await repo.delete_record(aid)
                except Exception as ce:
                    logger.warning("失败清理删除附件记录 %s 失败: %s", aid, ce)

        try:
            for it in validated:
                stored_path = it["stored_path"]
                os.makedirs(str(stored_path.parent), exist_ok=True)
                # 先登记再写盘：write_bytes 抛异常时本文件也在清理清单（review Minor）
                written.append(stored_path)
                stored_path.write_bytes(it["data"])
                try:
                    await repo.create(it["doc"])
                except StorageUnavailableError:
                    # fail-closed：离线 → 走统一清理（已写文件 + 已建记录）+ 503
                    raise _PersistAbort(
                        "STORAGE_UNAVAILABLE", "附件存储不可用", 503, retryable=True)
                except Exception as e:
                    logger.warning("附件记录写入失败: %s", e)
                    raise _PersistAbort("INTERNAL", "附件记录写入失败", 500)
                created.append(it["attachment_id"])
                # 进程内映射注册（content_to_openai 同步定位落盘路径用，spec §4.5）
                attachment_resolver.register(
                    it["attachment_id"], it["final_kind"], it["stored_name"])
                # signedUrl（fail-closed 省略）：AGENT_ACCESS_TOKEN 配 + SIG 未配 → 省略
                signed_url = None
                if not ((not sig_secret) and access_token_cfg):
                    signed_url, _ = _sign_url(sig_secret, it["attachment_id"])
                results.append(rest_contract.AttachmentInfo(
                    attachmentId=it["attachment_id"],
                    kind=it["final_kind"],
                    mime=it["mime"],
                    size=len(it["data"]),
                    url=f"/api/attachments/{it['attachment_id']}",
                    signedUrl=signed_url,
                    filename=it["doc"]["filename"],
                ))
        except _PersistAbort as e:
            await _cleanup()
            return _protocol_error_response(e.code, e.message, e.status, retryable=e.retryable)
        except Exception as e:
            # 兜底：写盘等意外失败同样清理不留孤儿，str(e) 只进日志
            logger.warning("附件持久化失败，已清理本轮写入: %s", e)
            await _cleanup()
            return _protocol_error_response("INTERNAL", "附件持久化失败", 500)

        return web.json_response(
            rest_contract.AttachmentUploadResponse(attachments=results).wire_dict())

    app.router.add_post("/api/uploads", upload_handler)

    # ---- 内置表情（批次 7 Task 6，BE-071） ----

    async def stickers_handler(request: web.Request) -> web.Response:
        """GET /api/stickers — 内置表情列表（契约单源）。

        sticker_id = 文件名去扩展名；src 走公开静态路径 /static/stickers/{id}.png
        （表情是公共资源，不落 attachments 集合）。列表按 id 排序保证稳定顺序。
        """
        from core.input.sticker_registry import load_stickers
        stickers = [
            rest_contract.StickerEntry(
                stickerId=sticker_id,
                src=f"/static/stickers/{sticker_id}.png",
            )
            for sticker_id in sorted(load_stickers())
        ]
        return web.json_response(
            rest_contract.StickerListResponse(stickers=stickers).wire_dict())

    app.router.add_get("/api/stickers", stickers_handler)

    # ---- Desktop 工具注册表 + 附件上传（批次 4，BE-042/043） ----

    def _tool_error(code: str, message: str, status: int) -> web.Response:
        """统一错误信封（ProtocolError 形状，与 session_routes._error_response 同款）。

        Args:
            code: 稳定错误码（CALL_NOT_FOUND / INVALID_REQUEST / INTERNAL）。
            message: 公开说明（禁本地路径/参数值，敏感纪律 D-p）。
            status: HTTP 状态码。

        Returns:
            web.Response: ProtocolError 信封 JSON。
        """
        env = rest_contract.ProtocolError(
            code=code, message=message, retryable=False,
            requestId=f"req_{uuid.uuid4().hex[:24]}", error=message,
        ).wire_dict()
        return web.json_response(env, status=status)

    def _tool_metadata_to_contract(meta: Any) -> rest_contract.ToolMetadata:
        """Module.tool.manager.ToolMetadata（snake_case dataclass）→ 契约 ToolMetadata（camelCase）。

        Args:
            meta: ToolManager.registry_metadata() 的单条元数据。

        Returns:
            rest_contract.ToolMetadata：wire 形状（camelCase）。
        """
        return rest_contract.ToolMetadata(
            name=meta.name,
            description=meta.description,
            runtime=meta.runtime,
            riskLevel=meta.risk_level,
            permissionRequired=meta.permission_required,
            approvalAuthority=meta.approval_authority,
            version=meta.version,
            inputSchema=meta.input_schema,
            outputSchema=meta.output_schema,
            available=meta.available,
        )

    def _desktop_call_to_status(doc: dict) -> rest_contract.DesktopCallStatus:
        """repo 调用文档 → 契约 DesktopCallStatus（status 用 repo 枚举，spec §9 映射权威）。

        result 为 REST 边界转换：repo 存 snake 键（output/attachments/execution_summary，
        attachments[].mime_type），wire 归一 camelCase（executionSummary/mimeType）——
        repo 存储不改，仅出站转换。

        Args:
            doc: DesktopCallRepository.get() 返回的调用文档。

        Returns:
            rest_contract.DesktopCallStatus：查询响应。
        """
        result = doc.get("result")
        if isinstance(result, dict):
            result = dict(result)
            if "execution_summary" in result:
                result["executionSummary"] = result.pop("execution_summary")
            attachments = result.get("attachments")
            if isinstance(attachments, list):
                result["attachments"] = [
                    {"url": a.get("url"),
                     "mimeType": a.get("mime_type", a.get("mimeType")),
                     "label": a.get("label")}
                    for a in attachments if isinstance(a, dict)
                ]
        return rest_contract.DesktopCallStatus(
            callId=doc.get("call_id") or "",
            toolName=doc.get("tool_name") or "",
            toolVersion=doc.get("tool_version") or "",
            runtime=doc.get("runtime") or "",
            paramHash=doc.get("arguments_hash") or "",
            status=RepoCallStatus(doc.get("status") or "pending"),
            createdAt=doc.get("created_at") or "",
            expiresAt=doc.get("expires_at") or "",
            result=result,
            error=doc.get("error"),
        )

    async def tools_registry_handler(request: web.Request) -> web.Response:
        """GET /api/tools — 工具注册表导出（BE-042）。

        desktopAvailable 取 bridge.is_available()（同步布尔）；desktop 工具的
        available 随其翻转（ToolManager.registry_metadata 派生，BE-040 §3.2）。
        """
        agent = request.app["agent"]
        bridge = agent.desktop_bridge
        desktop_available = bridge.is_available()
        manager = agent.tool_adapter._manager  # noqa: SLF001 — 与 server.py/ws_handler 同款深度访问先例
        metadata = manager.registry_metadata(desktop_available=desktop_available)
        tools = [_tool_metadata_to_contract(m) for m in metadata]
        return web.json_response(
            rest_contract.ToolRegistryResponse(
                tools=tools, desktopAvailable=desktop_available).wire_dict())

    async def tools_call_status_handler(request: web.Request) -> web.Response:
        """GET /api/tools/calls/{callId} — 桌面调用状态查询（BE-043）。

        未知 callId → 404 CALL_NOT_FOUND；超时/过期态如实返回 status=expired，
        不抛 408（CALL_EXPIRED 语义在桥内以 error_type=TIMEOUT 呈现给 Agent，spec §6）。
        """
        call_id = request.match_info.get("callId", "")
        agent = request.app["agent"]
        doc = await agent.desktop_bridge.get_call(call_id)
        if doc is None:
            return _tool_error("CALL_NOT_FOUND", f"调用不存在: {call_id}", 404)
        return web.json_response(_desktop_call_to_status(doc).wire_dict())

    async def desktop_upload_handler(request: web.Request) -> web.Response:
        """POST /api/uploads/desktop/{callId} — desktop 附件字节上传（spec §5.4）。

        持有者校验（防伪附件/孤儿文件，review 修正）：上传前校验
        ① callId 存在（否则 404 CALL_NOT_FOUND）；
        ② 上传连接是 target_connection（否则 403 INVALID_REQUEST）；
        ③ call 处于可收结果状态（pending/accepted/running/offline，终态拒绝——
        防向已结束调用投伪附件）。校验失败不落盘。

        传输方式落地（brief 未定义）：用 `X-Client-Connection: <connection_id>` 头——
        Electron 上传时携带；后端在 WS 握手时已把 conn_id 记到 bridge._connection_id
        并写入 call 的 target_connection（spec §4.2），本 handler 校验头 == target_connection。

        类型/尺寸白名单：沿用 image_max_mb / image_allowed_types + application/octet-stream
        （desktop.file.* 非截图结果）；仿 image_upload_handler 结构 read_chunk 限流反 DoS。
        """
        from core.storage.desktop_call_repo import (
            STATUS_ACCEPTED, STATUS_OFFLINE, STATUS_PENDING, STATUS_RUNNING,
        )

        agent = request.app["agent"]
        bridge = agent.desktop_bridge
        call_id = request.match_info.get("callId", "")

        # ① callId 存在
        doc = await bridge.get_call(call_id)
        if doc is None:
            return _tool_error("CALL_NOT_FOUND", f"调用不存在: {call_id}", 404)

        # ② 上传连接是 target_connection
        conn_id = request.headers.get("X-Client-Connection", "")
        if conn_id != (doc.get("target_connection") or ""):
            return _tool_error("INVALID_REQUEST", "上传连接与调用目标连接不一致", 403)

        # ③ call 非终态（spec §5.4：pending/accepted/running/offline 可收，终态拒绝）
        accepting = {STATUS_PENDING, STATUS_ACCEPTED, STATUS_RUNNING, STATUS_OFFLINE}
        if (doc.get("status") or "") not in accepting:
            return _tool_error("INVALID_REQUEST", "调用已结束，拒绝接收附件", 403)

        # 类型/尺寸约束（对齐 image_upload_handler）
        cfg = agent.config.server
        mb = cfg.image_max_mb
        max_bytes = (mb if mb is not None and mb > 0 else 10) * 1024 * 1024
        allowed = list(cfg.image_allowed_types
                       or ["image/png", "image/jpeg", "image/gif", "image/webp"])
        allowed.append("application/octet-stream")

        try:
            reader = await request.multipart()
            field = await reader.next()
            if field is None:
                return _tool_error("INVALID_REQUEST", "未找到附件字段", 400)

            # read_chunk() 逐块读取以限制内存——整块 read() 会把超大 body 全部读入内存
            # （multipart 原始流不受 client_max_size 约束，属 DoS 向量），读到超限即 413。
            field = cast(BodyPartReader, field)
            data = bytearray()
            while not field.at_eof():
                data.extend(await field.read_chunk(max_bytes + 1))
                if len(data) > max_bytes:
                    return _tool_error(
                        "INVALID_REQUEST",
                        f"附件过大，上限 {max_bytes // 1024 // 1024}MB", 413)

            filename = getattr(field, "filename", "") or f"{call_id}.bin"
            ext = os.path.splitext(filename)[1].lower()
            ext_to_mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                           ".gif": "image/gif", ".webp": "image/webp",
                           ".txt": "text/plain", ".json": "application/json"}
            mime = ext_to_mime.get(ext, "application/octet-stream")
            if mime not in allowed:
                return _tool_error("INVALID_REQUEST", f"不支持的附件类型: {mime}", 415)

            upload_dir = ProjectPath.DATA_UPLOADS_DESKTOP.resolve()
            os.makedirs(str(upload_dir), exist_ok=True)
            # 落盘名用 callId（basename 兜底防路径穿越；callId 为服务端 call_ 前缀生成）
            saved_name = f"{os.path.basename(call_id)}{ext}"
            saved_path = upload_dir / saved_name
            saved_path.write_bytes(data)

            url = f"/uploads/desktop/{saved_name}"
            return web.json_response({"url": url})
        except web.HTTPRequestEntityTooLarge:
            raise  # chunked 超限放行——交中间件统一 413 信封（F13）
        except Exception as e:
            # Task 9（批次 8a）：信封只给通用文案，str(e) 只进日志——防绝对路径/内部细节泄漏
            logger.warning("desktop 附件上传失败: %s", e)
            return _tool_error("INTERNAL", "上传失败，请稍后重试", 500)

    app.router.add_get("/api/tools", tools_registry_handler)
    app.router.add_get("/api/tools/calls/{callId}", tools_call_status_handler)
    app.router.add_post("/api/uploads/desktop/{callId}", desktop_upload_handler)

    # ---- Chat API（curl 测试用） ----

    async def chat_api_handler(request: web.Request) -> web.Response:
        """REST Chat API（curl 测试用）。

        Args:
            request: aiohttp Request，JSON body 包含 content/text 和可选的 user_name。

        Returns:
            web.Response: {"ok": True} 或 400 错误。
        """
        agent = request.app["agent"]
        try:
            body = await request.json()
            content = body.get("content")
            user_name = body.get("user_name", agent.user_name)
            text = body.get("text", "")
        except web.HTTPRequestEntityTooLarge:
            raise  # chunked 超限放行——交中间件统一 413 信封（F13）
        except Exception:
            return web.json_response(rest_contract.ApiFail(error="无效的 JSON 请求体").wire_dict(), status=400)

        if not content and not text:
            return web.json_response(rest_contract.ApiFail(error="缺少 content 或 text").wire_dict(), status=400)

        await agent.process_message(text=text, user_name=user_name, content=content)
        return web.json_response(rest_contract.ApiOk().wire_dict())

    app.router.add_post("/api/chat", chat_api_handler)

    # ---- 消息归档 API ----

    async def messages_handler(request: web.Request) -> web.Response:
        """分页查询消息归档。

        Args:
            request: aiohttp Request，query 参数包含 before、limit。

        Returns:
            web.Response: {"messages": [...], "has_more": ..., "total": ...}。
        """
        agent = request.app["agent"]
        repo = agent.message_repo

        before = request.query.get("before", "").strip() or None
        try:
            limit = int(request.query.get("limit", "50"))
            limit = max(1, min(limit, 200))
        except ValueError:
            limit = 50

        try:
            messages, has_more = await repo.query(before=before, limit=limit)
            total = await repo.count()
            client_msgs = _archive_to_client(messages)
            # 逐条转契约视图，单条失败丢弃（_archive_msg_to_contract 返回 None），避免整页加载失败
            contract_msgs = [m for m in (_archive_msg_to_contract(d) for d in client_msgs) if m]
            resp = rest_contract.MessagesResponse(
                messages=contract_msgs, has_more=has_more, total=total)
            return web.json_response(resp.wire_dict())
        except Exception as e:
            logger.warning("Failed to query message archive: %s", e)
            # 信封不携带异常文本（含路径/内部细节，review Minor）——只出通用消息
            return web.json_response(
                rest_contract.MessagesResponse(
                    messages=[], has_more=False, total=0,
                    error="查询消息归档失败").wire_dict(),
                status=500,
            )

    app.router.add_get("/api/messages", messages_handler)
