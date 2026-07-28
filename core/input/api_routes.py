"""业务 API 路由 — Providers, 图片上传, Chat REST, 消息归档。"""

import logging
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


def register_api_routes(app: web.Application) -> None:
    """注册所有业务 API 路由到 aiohttp Application。"""

    # ---- Provider 管理 ----

    async def providers_list_handler(request: web.Request) -> web.Response:
        """返回可用 provider 列表（注册表 + custom_providers.yaml）。

        Args:
            request: aiohttp Request。

        Returns:
            web.Response: {"llm": ..., "stt": ..., "tts": ..., "custom": ...}。
        """
        from core.input.custom_providers import load as load_custom

        llm_registry = {
            "deepseek": {"models": ["deepseek-v4-flash", "deepseek-v4-pro"]},
            "openai": {"models": ["gpt-4", "gpt-4o", "gpt-5"]},
            "ollama": {"models": ["qwen2.5:3b"]},
            "mimo": {"models": ["mimo-v2.5", "mimo-v2.5-pro"]},
            "gemini": {"models": []},
            "custom": {"models": []},
        }
        stt_registry = {
            "sensevoice": {"models": ["sense-voice"]},
            "custom": {"models": []},
        }
        tts_registry = {
            "gpt-sovits": {"models": ["default"]},
            "custom": {"models": []},
            "minimax": {"models": ["speech-2.8-hd"]},
        }

        custom_data = load_custom()
        for entry in custom_data.get("llm", []):
            llm_registry["custom"]["models"].append(entry["name"])
        for entry in custom_data.get("stt", []):
            stt_registry["custom"]["models"].append(entry["name"])
        for entry in custom_data.get("tts", []):
            tts_registry["custom"]["models"].append(entry["name"])

        return web.json_response({
            "llm": llm_registry,
            "stt": stt_registry,
            "tts": tts_registry,
            "custom": custom_data,
        })

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
        """测试自定义 provider 连通性。

        Args:
            request: aiohttp Request，JSON body 包含 service、base_url、api_key、model。

        Returns:
            web.Response: 后端 test() 返回的结果或错误信息。
        """
        try:
            body = await request.json()
            service = body.get("service", "")
            base_url = body.get("base_url", "")
            api_key = body.get("api_key", "")
            model = body.get("model", "")
        except Exception:
            return web.json_response({"ok": False, "error": "无效的请求体"}, status=400)

        try:
            if service == "llm":
                from Module.llm import LLMFactory
                backend = LLMFactory.create(f"custom/{model}", base_url=base_url, api_key=api_key)
            elif service == "stt":
                from Module.stt import STTFactory
                backend = STTFactory.create(f"custom/{model}", base_url=base_url, api_key=api_key)
            elif service == "tts":
                from Module.tts import TTSFactory
                backend = TTSFactory.create(f"custom/{model}", base_url=base_url, api_key=api_key)
            else:
                return web.json_response({"ok": False, "error": f"未知 service: {service}"}, status=400)

            result = await backend.test()
            try:
                await backend.close()
            except Exception:
                pass
            return web.json_response(result)
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

    app.router.add_get("/api/providers", providers_list_handler)
    app.router.add_post("/api/providers", providers_save_handler)
    app.router.add_delete("/api/providers/{service}/{name}", providers_delete_handler)
    app.router.add_post("/api/providers/test", providers_test_handler)

    # ---- 图片上传 ----

    _IMAGES_DIR = Path(__file__).parent.parent.parent / "data" / "uploads" / "images"

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
        cfg = getattr(agent._config, "server", None)
        mb = getattr(cfg, "image_max_mb", None)
        max_bytes = (mb if mb is not None and mb > 0 else 10) * 1024 * 1024
        allowed = getattr(cfg, "image_allowed_types", None) or ["image/png", "image/jpeg", "image/gif", "image/webp"]

        try:
            reader = await request.multipart()
            field = await reader.next()
            if field is None:
                return web.json_response({"error": "未找到图片字段"}, status=400)

            data = await cast(BodyPartReader, field).read()
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
            user_name = body.get("user_name", agent._user_name)
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
        repo = agent._message_repo

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
