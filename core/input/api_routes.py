"""业务 API 路由 — Providers, 图片上传, Chat REST, 消息归档。"""

import asyncio
import logging
import time
from pathlib import Path

from aiohttp import web
from aiohttp.multipart import BodyPartReader
from typing import cast

from core.schema.message import Message

logger = logging.getLogger(__name__)


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


async def _run_usable_test(backend, service: str, request: web.Request, body: dict) -> dict:
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


def register_api_routes(app: web.Application) -> None:
    """注册所有业务 API 路由到 aiohttp Application。"""

    # ---- Provider 管理 ----

    async def providers_list_handler(request: web.Request) -> web.Response:
        """返回过滤后的 provider 列表（providers.yaml 单一来源 + env 过滤 + custom）。"""
        from core.input.custom_providers import load as load_custom
        from core.input.provider_registry import build_providers_response
        return web.json_response(build_providers_response(load_custom()))

    async def providers_save_handler(request: web.Request) -> web.Response:
        """保存一个新的自定义 provider。

        Args:
            request: aiohttp Request，JSON body 包含 service 和 entry。

        Returns:
            web.Response: {"ok": True, "data": ...} 或错误信息。
        """
        from core.input.custom_providers import add_provider
        try:
            body = await request.json()
            service = body.get("service", "")
            entry = body.get("entry", {})
        except Exception:
            return web.json_response({"ok": False, "error": "无效的请求体"}, status=400)
        if service not in ("llm", "stt", "tts"):
            return web.json_response({"ok": False, "error": f"未知 service: {service}"}, status=400)
        if not entry.get("name") or not entry.get("base_url"):
            return web.json_response({"ok": False, "error": "缺少 name 或 base_url"}, status=400)
        try:
            data = add_provider(service, entry)
            return web.json_response({"ok": True, "data": data})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def providers_delete_handler(request: web.Request) -> web.Response:
        """删除一个自定义 provider。

        Args:
            request: aiohttp Request，URL 路径参数包含 service 和 name。

        Returns:
            web.Response: {"ok": True, "data": ...} 或错误信息。
        """
        from core.input.custom_providers import remove_provider
        service = request.match_info.get("service", "")
        name = request.match_info.get("name", "")
        if service not in ("llm", "stt", "tts"):
            return web.json_response({"ok": False, "error": f"未知 service: {service}"}, status=400)
        try:
            data = remove_provider(service, name)
            return web.json_response({"ok": True, "data": data})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)

    async def providers_test_handler(request: web.Request) -> web.Response:
        """测试 provider 连通性（level=connectivity）或可使用性（level=usable）。

        body: {service, provider, model, level, base_url?, api_key?, character?}
        参数解析顺序见 spec「/api/providers/test 扩展」：provider+model 均缺省 → 回退配置模型；
        仅 provider 缺省 → "custom"；level 缺省 → "connectivity"。
        base_url/api_key 优先级：请求体非空 > custom_providers.yaml > env。
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "无效的请求体"}, status=400)

        service = body.get("service", "")
        if service not in ("llm", "stt", "tts"):
            return web.json_response({"ok": False, "error": f"未知 service: {service}"}, status=400)

        level = body.get("level", "connectivity")
        provider = body.get("provider", "")
        model = body.get("model", "")
        base_url = body.get("base_url", "")
        api_key = body.get("api_key", "")

        # provider+model 均缺省 → 回退该服务当前模型（完整 provider/model，直接 split）
        if not provider and not model:
            agent = request.app.get("agent")
            if agent is None:
                return web.json_response({"ok": False, "error": "agent 未注入"}, status=500)
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
                return web.json_response({"ok": False, "error": f"当前模型格式无效: {model_id}"})
        elif not provider:
            provider = "custom"

        # base_url/api_key：请求体非空 > custom_providers.yaml（resolve_model_params）
        if not base_url and provider == "custom":
            from core.input.custom_providers import resolve_model_params
            params = resolve_model_params(service, f"custom/{model}")
            base_url = params.get("base_url", "")
            if not api_key:
                api_key = params.get("api_key", "")

        create_kwargs = {}
        if base_url:
            create_kwargs["base_url"] = base_url
        if api_key:
            create_kwargs["api_key"] = api_key

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
            return web.json_response({"ok": False, "error": str(e)})

        try:
            if level == "usable":
                result = await _run_usable_test(backend, service, request, body)
            else:
                result = await asyncio.wait_for(backend.test(), timeout=5.0)
            return web.json_response(result)
        except asyncio.TimeoutError:
            return web.json_response({"ok": False, "error": "测试超时"})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})
        finally:
            try:
                await backend.close()
            except Exception:
                pass

    app.router.add_get("/api/providers", providers_list_handler)
    app.router.add_post("/api/providers", providers_save_handler)
    app.router.add_delete("/api/providers/{service}/{name}", providers_delete_handler)
    app.router.add_post("/api/providers/test", providers_test_handler)

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
        cfg = getattr(agent.config, "server", None)
        mb = getattr(cfg, "image_max_mb", None)
        max_bytes = (mb if mb is not None and mb > 0 else 10) * 1024 * 1024
        allowed = getattr(cfg, "image_allowed_types", None) or ["image/png", "image/jpeg", "image/gif", "image/webp"]

        try:
            reader = await request.multipart()
            field = await reader.next()
            if field is None:
                return web.json_response({"error": "未找到图片字段"}, status=400)

            # read(max_bytes + 1) 限制单次读取——multipart 原始流不受
            # client_max_size 约束，若整块 read() 会允许超大 body 造成内存尖峰（DoS 向量）
            data = await cast(BodyPartReader, field).read(max_bytes + 1)
            if len(data) > max_bytes:
                return web.json_response({"error": f"图片过大，上限 {max_bytes // 1024 // 1024}MB"}, status=413)

            filename = getattr(field, "filename", "") or "image.png"
            ext = _os.path.splitext(filename)[1].lower()
            ext_to_mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                           ".gif": "image/gif", ".webp": "image/webp"}
            mime = ext_to_mime.get(ext, "image/png")

            if mime not in allowed:
                return web.json_response({"error": f"不支持的图片类型: {mime}"}, status=415)

            _os.makedirs(str(_IMAGES_DIR), exist_ok=True)
            saved_name = f"{_uuid.uuid4().hex[:12]}{ext}"
            saved_path = _IMAGES_DIR / saved_name
            saved_path.write_bytes(data)

            url = f"/uploads/images/{saved_name}"
            return web.json_response({"url": url, "mime": mime, "size": len(data)})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    app.router.add_post("/api/images", image_upload_handler)

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
        except Exception:
            return web.json_response({"ok": False, "error": "无效的 JSON 请求体"}, status=400)

        if not content and not text:
            return web.json_response({"ok": False, "error": "缺少 content 或 text"}, status=400)

        await agent.process_message(text=text, user_name=user_name, content=content)
        return web.json_response({"ok": True})

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
            return web.json_response({
                "messages": client_msgs,
                "has_more": has_more,
                "total": total,
            })
        except Exception as e:
            logger.warning("Failed to query message archive: %s", e)
            return web.json_response(
                {"messages": [], "has_more": False, "total": 0, "error": str(e)},
                status=500,
            )

    app.router.add_get("/api/messages", messages_handler)
