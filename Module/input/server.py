"""WebSocket server — bridges browser clients to the agent pipeline.

Each client gets an independent WebSocketChannel (outbound) and AudioBuffer
(inbound audio). Text and STT results are routed to the agent's EventBus.
"""

import asyncio
import json
import logging
from pathlib import Path

from aiohttp import web, WSMsgType

from core.agent import RunState
from Module.output import WebSocketChannel
from .audio_buffer import AudioBuffer

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent.parent.parent / "static"
_LIVE2D_DIR = Path(__file__).parent.parent.parent / "data" / "live2d-assets"


def create_app(
    agent,
    stt_transcribe=None,
    serve_static: bool = True,
    stt_emotion_state: dict | None = None,
    wake_word_enabled: bool = False,
    task_system=None,  # TaskSystem | None
) -> web.Application:
    """Create an aiohttp Application with WebSocket handler and optional static files.

    Args:
        agent: Agent instance (needs _output, _bus, process_message())
        stt_transcribe: async callable(bytes) -> str for STT (optional)
        serve_static: serve static/index.html at /
        stt_emotion_state: mutable dict with 'emotion' and 'events' keys,
            updated by the STT wrapper after each transcription.
        task_system: TaskSystem instance for debug routes (optional)
    """
    app = web.Application()

    # Inject agent + stt + task_system into app context for handler access
    app["agent"] = agent
    if task_system is not None:
        app["task_system"] = task_system

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        return await _handle_ws(request, agent, stt_transcribe, stt_emotion_state, wake_word_enabled)

    app.router.add_get("/ws", ws_handler)

    if serve_static and _STATIC_DIR.is_dir():
        app.router.add_static("/static/", _STATIC_DIR, show_index=True)

    if _LIVE2D_DIR.is_dir():
        app.router.add_static("/live2d/", _LIVE2D_DIR, show_index=False)

    # Serve index.html at /
    index_path = _STATIC_DIR / "index.html"
    if index_path.is_file():
        async def index_handler(_request: web.Request) -> web.FileResponse:
            return web.FileResponse(index_path)
        app.router.add_get("/", index_handler)

        # Debug page + API
        debug_path = _STATIC_DIR / "debug.html"
        if debug_path.is_file():
            async def debug_handler(_request: web.Request) -> web.FileResponse:
                return web.FileResponse(debug_path)
            app.router.add_get("/debug", debug_handler)

        async def debug_api_handler(request: web.Request) -> web.Response:
            from core.debug_info import collect, asdict
            info = await collect(request.app["agent"])
            return web.json_response(asdict(info))
        app.router.add_get("/debug/api", debug_api_handler)

        # TaskSystem debug API — SSE 实时推送任务状态
        # 通过 create_app(task_system=...) 参数注册，app['task_system'] 已设置
        from Module.task_system.debug_api import TaskDebugAPI

        ts = app.get('task_system')
        if ts is not None:
            task_debug_api = TaskDebugAPI(ts)
            app['task_debug_api'] = task_debug_api

            app.router.add_get("/debug/api/tasks", task_debug_api.snapshot_handler)
            app.router.add_get("/debug/api/tasks/stream", task_debug_api.sse_handler)
        else:
            app['task_debug_api'] = None

    # Health check endpoint — for Docker/Kubernetes probes
    async def health_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        try:
            fsm_state = agent._fsm.state
        except Exception:
            fsm_state = "unknown"
        return web.json_response({
            "status": "ok",
            "fsm": fsm_state,
            "llm": getattr(agent._llm, 'model', 'unknown'),
            "memory": {
                "session_count": len(getattr(agent._session, '_messages', [])),
                "long_term_count": getattr(agent._long_term, 'count', 0),
            },
            "tools": getattr(agent._tool_manager, 'tool_names', ["<loading>"]),
        })
    app.router.add_get("/health", health_handler)
    app.router.add_get("/ready", health_handler)

    # Shutdown endpoint — triggers graceful agent stop
    async def shutdown_handler(request: web.Request) -> web.Response:
        import logging
        _log = logging.getLogger(__name__)
        _log.info("Shutdown requested via HTTP")
        request.app["agent"]._state = RunState.STOPPING
        return web.json_response({"status": "shutting_down"})
    app.router.add_post("/shutdown", shutdown_handler)
    app.router.add_get("/shutdown", shutdown_handler)  # 也接受浏览器直接访问

    # Restart endpoint — triggers graceful restart with config reload
    async def restart_handler(request: web.Request) -> web.Response:
        import logging
        _log = logging.getLogger(__name__)
        _log.info("Restart requested via HTTP")
        request.app["agent"]._state = RunState.RESTARTING
        return web.json_response({"status": "restarting"})
    app.router.add_post("/restart", restart_handler)

    # Tools endpoint — list loaded plugins
    async def tools_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        return web.json_response({
            "plugins": agent._tool_manager.plugin_list,
        })
    app.router.add_get("/tools", tools_handler)

    # Tool reload endpoint — hot-reload a single plugin
    async def tool_reload_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        try:
            body = await request.json()
            name = body.get("name", "")
        except Exception:
            return web.json_response(
                {"ok": False, "error": "无效的请求体"}, status=400,
            )
        if not name:
            return web.json_response(
                {"ok": False, "error": "缺少插件名称"}, status=400,
            )
        ok = await agent._tool_manager.reload_plugin(name)
        if ok:
            return web.json_response({"ok": True, "name": name})
        else:
            return web.json_response(
                {"ok": False, "name": name, "error": f"插件 '{name}' 重载失败"},
            )
    app.router.add_post("/tools/reload", tool_reload_handler)

    # ---- Debug: 数据库管理 API ----

    async def db_status_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        ltm = agent._long_term
        session = agent._session
        archive = getattr(agent, "_archive", None)
        return web.json_response({
            "ltm": {
                "count": ltm.count,
                "milvus_ok": ltm._connected,
                "dirty_count": len(ltm._dirty_ids),
            },
            "session": {
                "count": 1,  # 当前只有单个 session
                "current_id": getattr(session, "_session_id", "default"),
                "mongodb_ok": session._db is not None,
            },
            "archive": {
                "count": await archive.count() if archive else 0,
                "mongodb_ok": archive is not None and archive._db is not None,
            },
        })
    app.router.add_get("/debug/api/db-status", db_status_handler)

    async def ltm_list_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        search = request.query.get("search", "").strip()
        mem_type = request.query.get("type", "").strip()
        try:
            limit = int(request.query.get("limit", "50"))
            offset = int(request.query.get("offset", "0"))
        except ValueError:
            return web.json_response({"error": "无效的分页参数"}, status=400)

        if agent._long_term is None:
            return web.json_response({"error": "长期记忆未初始化"}, status=503)
        entries = agent._long_term._entries
        if search:
            entries = [e for e in entries if search in e.text]
        if mem_type:
            entries = [e for e in entries if e.memory_type == mem_type]

        total = len(entries)
        page = entries[offset:offset + limit]
        return web.json_response({
            "total": total,
            "entries": [
                {
                    "id": e.id,
                    "text": e.text,
                    "memory_type": e.memory_type,
                    "poignancy": e.poignancy,
                    "timestamp": e.timestamp,
                    "metadata": e.metadata,
                }
                for e in page
            ],
        })
    app.router.add_get("/debug/api/ltm", ltm_list_handler)

    async def ltm_insert_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        try:
            body = await request.json()
            text = body.get("text", "").strip()
            if not text:
                return web.json_response({"error": "text 不能为空"}, status=400)
            mem_type = body.get("memory_type", "note")
            poignancy = int(body.get("poignancy", 5))
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "无效的请求体"}, status=400)

        await agent._long_term.insert(text, memory_type=mem_type, poignancy=poignancy)
        return web.json_response({"ok": True})
    app.router.add_post("/debug/api/ltm", ltm_insert_handler)

    async def ltm_delete_single_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        entry_id = request.match_info.get("entry_id", "")
        if not entry_id:
            return web.json_response({"error": "缺少 entry_id"}, status=400)
        ok = await agent._long_term.delete(entry_id)
        return web.json_response({"ok": ok})
    app.router.add_delete("/debug/api/ltm/{entry_id}", ltm_delete_single_handler)

    async def ltm_clear_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        count = await agent._long_term.clear_all()
        return web.json_response({"ok": True, "deleted": count})
    app.router.add_delete("/debug/api/ltm", ltm_clear_handler)

    # ---- Debug: 消息归档管理 ----

    async def archive_list_handler(request: web.Request) -> web.Response:
        """分页浏览归档消息（debug 专用）。"""
        agent = request.app["agent"]
        archive = getattr(agent, "_archive", None)
        if archive is None:
            return web.json_response({"messages": [], "has_more": False, "total": 0})

        before = request.query.get("before", "").strip() or None
        try:
            limit = int(request.query.get("limit", "100"))
            limit = max(1, min(limit, 500))
        except ValueError:
            limit = 100

        try:
            messages, has_more = await archive.query(before=before, limit=limit)
            total = await archive.count()
            client_msgs = _archive_to_client(messages)
            return web.json_response({
                "messages": client_msgs,
                "has_more": has_more,
                "total": total,
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def archive_clear_handler(request: web.Request) -> web.Response:
        """清空全部归档（debug 专用）。"""
        agent = request.app["agent"]
        archive = getattr(agent, "_archive", None)
        if archive is None:
            return web.json_response({"ok": False, "error": "archive not initialized"})
        deleted = await archive.clear_all()
        return web.json_response({"ok": True, "deleted": deleted})

    app.router.add_get("/debug/api/archive", archive_list_handler)
    app.router.add_delete("/debug/api/archive", archive_clear_handler)

    async def sessions_list_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        session = agent._session
        if session._db is None:
            # MongoDB 不可用，返回内存中的当前 session
            return web.json_response({
                "sessions": [{
                    "session_id": session._session_id,
                    "message_count": len(session._messages),
                    "updated_at": "",
                    "token_total": 0,
                }],
            })
        try:
            docs = await session._db.find(
                "sessions", {}, sort=[("updated_at", -1)], limit=500)
            # 查询 token_usage 集合，按 session_id 聚合
            from collections import defaultdict
            token_totals: dict[str, int] = defaultdict(int)
            try:
                token_docs = await session._db.find(
                    "token_usage", {}, limit=2000)
                for td in token_docs:
                    sid = td.get("session_id", "")
                    token_totals[sid] += td.get("total_tokens", 0)
            except Exception:
                pass  # token_usage 集合可能不存在，静默降级
            return web.json_response({
                "sessions": [
                    {
                        "session_id": d.get("session_id", "?"),
                        "message_count": len(d.get("messages", [])),
                        "updated_at": d.get("updated_at", ""),
                        "token_total": token_totals.get(d.get("session_id", ""), 0),
                    }
                    for d in docs
                ],
            })
        except Exception as e:
            return web.json_response(
                {"error": f"查询失败: {e}"}, status=500)
    app.router.add_get("/debug/api/sessions", sessions_list_handler)

    async def session_detail_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        sid = request.match_info.get("session_id", "")
        if not sid:
            return web.json_response({"error": "缺少 session_id"}, status=400)
        session = agent._session
        if session._db is None:
            return web.json_response({"error": "MongoDB 不可用"}, status=503)
        try:
            docs = await session._db.find(
                "sessions", {"session_id": sid}, limit=1)
            if not docs:
                return web.json_response({"error": "会话不存在"}, status=404)
            doc = docs[0]
            raw_msgs = doc.get("messages", [])
            # 格式化消息，确保前端渲染时有 parts 字段
            formatted = []
            for m in raw_msgs:
                entry = {"role": m.get("role", "?"),
                         "timestamp": m.get("timestamp", ""),
                         "content_type": m.get("content_type", {})}
                content = m.get("content", "")
                if isinstance(content, list):
                    # 多模态格式 — 提取 text parts + 保留完整 content
                    entry["parts"] = [
                        {"content": p.get("content", "")}
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    ]
                    entry["content"] = content
                elif isinstance(content, str):
                    entry["parts"] = [{"content": content}]
                elif "parts" in m:
                    entry["parts"] = m["parts"]
                else:
                    entry["parts"] = [{"content": str(content)}]
                formatted.append(entry)
            return web.json_response({
                "session_id": doc.get("session_id", sid),
                "messages": formatted,
                "params": doc.get("params", {}),
                "updated_at": doc.get("updated_at", ""),
            })
        except Exception as e:
            return web.json_response(
                {"error": f"查询失败: {e}"}, status=500)
    app.router.add_get("/debug/api/sessions/{session_id}", session_detail_handler)

    async def session_delete_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        sid = request.match_info.get("session_id", "")
        if not sid:
            return web.json_response({"error": "缺少 session_id"}, status=400)
        session = agent._session
        if session._db is None:
            return web.json_response(
                {"error": "MongoDB 不可用"}, status=503)
        try:
            deleted = await session._db.delete_many(
                "sessions", {"session_id": sid})
            if sid == session._session_id:
                session._messages.clear()
            return web.json_response({"ok": True, "deleted": deleted})
        except Exception as e:
            return web.json_response(
                {"error": f"删除失败: {e}"}, status=500)
    app.router.add_delete("/debug/api/sessions/{session_id}", session_delete_handler)

    # ---- 模型切换 API ----

    async def providers_list_handler(request: web.Request) -> web.Response:
        """返回可用 provider 列表（注册表 + custom_providers.yaml）。"""
        from core.custom_providers import load as load_custom

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
        # 合并 custom providers 到注册表
        for entry in custom_data.get("llm", []):
            name = entry["name"]
            llm_registry["custom"]["models"].append(name)
        for entry in custom_data.get("stt", []):
            name = entry["name"]
            stt_registry["custom"]["models"].append(name)
        for entry in custom_data.get("tts", []):
            name = entry["name"]
            tts_registry["custom"]["models"].append(name)

        return web.json_response({
            "llm": llm_registry,
            "stt": stt_registry,
            "tts": tts_registry,
            "custom": custom_data,
        })
    app.router.add_get("/api/providers", providers_list_handler)

    async def providers_save_handler(request: web.Request) -> web.Response:
        """保存自定义 provider 到 custom_providers.yaml。"""
        from core.custom_providers import add_provider
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
    app.router.add_post("/api/providers", providers_save_handler)

    async def providers_delete_handler(request: web.Request) -> web.Response:
        """删除一个自定义 provider。"""
        from core.custom_providers import remove_provider
        service = request.match_info.get("service", "")
        name = request.match_info.get("name", "")
        if service not in ("llm", "stt", "tts"):
            return web.json_response({"ok": False, "error": f"未知 service: {service}"}, status=400)
        try:
            data = remove_provider(service, name)
            return web.json_response({"ok": True, "data": data})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=500)
    app.router.add_delete("/api/providers/{service}/{name}", providers_delete_handler)

    async def providers_test_handler(request: web.Request) -> web.Response:
        """测试 provider 连通性。"""
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
    app.router.add_post("/api/providers/test", providers_test_handler)

    # ---- 图片上传 ----

    _IMAGES_DIR = Path(__file__).parent.parent.parent / "data" / "uploads" / "images"

    async def image_upload_handler(request: web.Request) -> web.Response:
        """接收图片上传，保存到 data/images/，返回 URL。"""
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

            data = await field.read()
            if len(data) > max_bytes:
                return web.json_response({"error": f"图片过大，上限 {max_bytes // 1024 // 1024}MB"}, status=413)

            # 从文件名推断扩展名
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
        """接收 JSON 消息并触发 agent 处理，返回 assistant 回复。

        请求: {"content": [{"type": "text", "content": "..."}, {"type": "image", "source": "...", "mime": "..."}], "user_name": "妖梦"}
        响应: {"ok": true, "reply": "..."}  — 注：当前为异步处理，不等待回复
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

    def _archive_to_client(msgs: list[dict]) -> list[dict]:
        """将 archive 原始 dict 转为前端兼容格式（补 parts 字段）。"""
        result = []
        for m in msgs:
            entry = dict(m)
            if "_id" in entry:
                entry["_id"] = str(entry["_id"])
            # 如果已有 parts，直接透传
            if "parts" in entry:
                result.append(entry)
                continue
            # 否则从 content (TextPart 数组) 提取 parts
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

    async def messages_handler(request: web.Request) -> web.Response:
        """分页获取归档消息。供前端聊天页滚动加载历史。"""
        agent = request.app["agent"]
        archive = getattr(agent, "_archive", None)
        if archive is None:
            return web.json_response({"messages": [], "has_more": False, "total": 0})

        before = request.query.get("before", "").strip() or None
        try:
            limit = int(request.query.get("limit", "50"))
            limit = max(1, min(limit, 200))
        except ValueError:
            limit = 50

        try:
            messages, has_more = await archive.query(before=before, limit=limit)
            total = await archive.count()
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

    # ---- 用户上传文件目录 ----
    _UPLOADS_DIR = Path(__file__).parent.parent.parent / "data" / "uploads"
    _UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    app.router.add_static("/uploads/", str(_UPLOADS_DIR), show_index=False)

    async def prompts_list_handler(request: web.Request) -> web.Response:
        """返回最近 N 次 LLM 调用的 prompt 摘要列表。"""
        agent = request.app["agent"]
        recorder = getattr(agent, "_prompt_recorder", None)
        if recorder is None:
            return web.json_response({"records": [], "total": 0, "max": 0})
        return web.json_response(recorder.list_summaries())


    async def prompt_detail_handler(request: web.Request) -> web.Response:
        """返回指定记录的完整 prompt 内容 + 模板源码。"""
        index = int(request.match_info["index"])
        agent = request.app["agent"]
        recorder = getattr(agent, "_prompt_recorder", None)
        if recorder is None:
            raise web.HTTPNotFound(text="PromptRecorder 未初始化")
        record = recorder.get_full(index)
        if record is None:
            raise web.HTTPNotFound(text=f"记录 #{index} 不存在")

        # 读取模板源码（从 Module/system_prompt/templates/）
        template_source = None
        templates_dir = "Module/system_prompt/templates"
        try:
            import os
            sources = {}
            for fname in sorted(os.listdir(templates_dir)):
                if fname.endswith(".j2") and not fname.startswith("_"):
                    fpath = os.path.join(templates_dir, fname)
                    with open(fpath) as f:
                        sources[fname] = f.read()
            if sources:
                template_source = {"path": templates_dir, "sections": sources}
        except Exception:
            pass

        return web.json_response({
            "index": record.index,
            "timestamp": record.timestamp,
            "round_num": record.round_num,
            "model": record.model,
            "messages": record.messages,
            "tools": record.tools,
            "template_source": template_source,
        })

    async def sessions_clear_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        session = agent._session
        if session._db is None:
            return web.json_response(
                {"error": "MongoDB 不可用"}, status=503)
        try:
            deleted = await session._db.delete_many("sessions", {})
            session._messages.clear()
            return web.json_response({"ok": True, "deleted": deleted})
        except Exception as e:
            return web.json_response(
                {"error": f"清空失败: {e}"}, status=500)
    app.router.add_delete("/debug/api/sessions", sessions_clear_handler)
    app.router.add_get("/debug/api/prompts", prompts_list_handler)
    app.router.add_get(r"/debug/api/prompts/{index:\d+}", prompt_detail_handler)

    async def responses_list_handler(request: web.Request) -> web.Response:
        """返回最近 N 次 LLM 调用的原始回复摘要列表。"""
        agent = request.app["agent"]
        recorder = getattr(agent, "_response_recorder", None)
        if recorder is None:
            return web.json_response({"records": [], "total": 0, "max": 0})
        return web.json_response(recorder.list_summaries())

    async def response_detail_handler(request: web.Request) -> web.Response:
        """返回指定记录的完整原始回复。"""
        index = int(request.match_info["index"])
        agent = request.app["agent"]
        recorder = getattr(agent, "_response_recorder", None)
        if recorder is None:
            raise web.HTTPNotFound(text="ResponseRecorder 未初始化")
        record = recorder.get_full(index)
        if record is None:
            raise web.HTTPNotFound(text=f"记录 #{index} 不存在")
        return web.json_response({
            "index": record.index,
            "timestamp": record.timestamp,
            "round_num": record.round_num,
            "raw_text": record.raw_text,
            "inner_voice": record.inner_voice,
            "text_len": record.text_len,
            "inner_voice_len": record.inner_voice_len,
        })

    app.router.add_get("/debug/api/responses", responses_list_handler)
    app.router.add_get(r"/debug/api/responses/{index:\d+}", response_detail_handler)

    # TaskDebugAPI 生命周期管理
    async def on_startup(app):
        task_api = app.get('task_debug_api')
        if task_api:
            await task_api.start()

    async def on_shutdown(app):
        task_api = app.get('task_debug_api')
        if task_api:
            await task_api.stop()

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    return app


async def _handle_ws(
    request: web.Request,
    agent,
    stt_transcribe,
    stt_emotion_state: dict | None = None,
    wake_word_enabled: bool = False,
) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024)
    await ws.prepare(request)

    from core.message_id import new_message_id
    client_id = new_message_id()
    channel = WebSocketChannel(ws)
    agent._output.add_channel(channel)

    # 发送会话历史给新连接的客户端
    try:
        session_msgs = agent._session.messages_for_client()
        token_stats = {}
        if hasattr(agent, '_token_tracker') and agent._token_tracker is not None:
            token_stats = await agent._token_tracker.get_session_stats()
        await ws.send_json({"type": "session", "messages": session_msgs, "token_stats": token_stats})
    except Exception:
        logger.warning("Failed to send session on connect", exc_info=True)

    # 发送服务能力状态（STT / TTS 是否可用）
    try:
        await ws.send_json({
            "type": "capabilities",
            "stt": stt_transcribe is not None,
            "tts": agent._tts is not None,
            "llm_model": agent._llm.model,
            "live2d_static_image": agent._config.live2d.static_image,
        })
    except Exception:
        pass

    # 重新广播挂起的 HITL 审批请求（前端重连后恢复审批弹窗）
    try:
        if hasattr(agent, '_tool_manager') and agent._tool_manager is not None:
            pending = agent._tool_manager.get_pending_approvals()
            for request_id, tool_name, params in pending:
                await ws.send_json({
                    "command": "tool_approval",
                    "params": {
                        "request_id": request_id,
                        "tool_name": tool_name,
                        "params": params,
                    },
                })
                logger.debug("Re-broadcast pending approval to new client: %s (%s)",
                             request_id, tool_name)
    except Exception:
        logger.debug("Failed to re-send pending approvals", exc_info=True)

    audio_buf: AudioBuffer | None = None
    monitor_task: asyncio.Task | None = None
    if stt_transcribe:
        wake_words = None
        if wake_word_enabled:
            try:
                wake_words = agent.get_wake_words()
            except Exception:
                logger.debug("Could not read wake_words from agent", exc_info=True)

        audio_buf = AudioBuffer(
            stt_transcribe=stt_transcribe,
            aggressiveness=3,
            on_interrupt=lambda: _do_interrupt(agent, skip_if={"thinking", "executing"}),
            on_transcription=lambda text: _do_transcription(
                agent, ws, text, audio_buf, stt_emotion_state,
            ),
            wake_words=wake_words,
            on_wake_notify=lambda: _send_wake(ws),
        )

        # Monitor FSM state to deafen mic when assistant is speaking (echo prevention)
        async def _monitor_state():
            prev_state = None
            while not ws.closed:
                try:
                    await asyncio.sleep(0.1)
                    current = agent._fsm.state
                    if current != prev_state and current == "speaking" and audio_buf:
                        audio_buf.deafen(2.0)  # Ignore mic for 2s while TTS plays
                    prev_state = current
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("State monitor error", exc_info=True)
        monitor_task = asyncio.create_task(_monitor_state(), name=f"state-monitor-{client_id}")

    logger.info("WS client connected: %s (STT=%s)", client_id, "yes" if audio_buf else "no")

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    await _handle_json(data, agent, audio_buf, ws)
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "message": "Invalid JSON"})

            elif msg.type == WSMsgType.BINARY:
                if audio_buf:
                    await audio_buf.feed(msg.data)

            elif msg.type == WSMsgType.ERROR:
                logger.warning("WS error: %s", ws.exception())

    except asyncio.CancelledError:
        pass
    finally:
        if monitor_task and not monitor_task.done():
            monitor_task.cancel()
        if audio_buf:
            await audio_buf.flush()
        agent._output.remove_channel(channel)
        logger.info("WS client disconnected: %s", client_id)

    return ws


async def _handle_json(data: dict, agent, audio_buf=None, ws=None) -> None:
    msg_type = data.get("type", "")
    if msg_type == "wake_toggle":
        enabled = data.get("enabled", False)
        if audio_buf:
            audio_buf.set_wake_enabled(enabled)
            logger.info("唤醒词开关: %s", "开启" if enabled else "关闭")
    elif msg_type == "text":
        text_content = data.get("content", "")
        if isinstance(text_content, list):
            # 多模态 content 数组
            from Module.schema.message_format import content_text
            await agent.process_message(text=content_text(text_content), user_name=agent._user_name, content=text_content)
            logger.info("WS 收到多模态消息")
        elif isinstance(text_content, str) and text_content.strip():
            await agent.process_message(text=text_content.strip())
            logger.info("WS 收到文本: %s", text_content.strip()[:100])
    elif msg_type == "interrupt":
        logger.info("WS 收到打断指令")
        await _do_interrupt(agent)
    elif msg_type == "switch_model":
        service = data.get("service", "")
        model = data.get("model", "")
        if service == "llm":
            result = await agent.switch_llm(model)
        elif service == "stt":
            result = await agent.switch_stt(model)
        elif service == "tts":
            result = await agent.switch_tts(model)
        else:
            result = {"ok": False, "error": f"未知 service: {service}"}
        from Module.output import WebSocketChannel
        for ch in agent._output._channels:
            if isinstance(ch, WebSocketChannel) and ch._ws is ws:
                try:
                    await ws.send_json({
                        "type": "switch_model_result",
                        "service": service,
                        "model": model,
                        **result,
                    })
                except Exception:
                    pass
                break
        logger.info("WS 切换模型: service=%s model=%s ok=%s", service, model, result.get("ok"))
    elif msg_type == "tool_approval":
        request_id = data.get("request_id", "")
        approved = data.get("approved", False)
        if request_id:
            ok = agent._tool_manager.submit_approval(request_id, approved)
            if ok:
                logger.info(
                    "WS 审批结果: request=%s approved=%s", request_id, approved,
                )
            else:
                logger.debug("WS 审批结果: request=%s 不存在或已过期", request_id)
    else:
        logger.debug("WS 未知消息类型: %s", msg_type)


async def _do_interrupt(agent, skip_if: set | None = None) -> None:
    """Post INTERRUPT event. skip_if 可阻止特定状态下的 VAD 误触发。"""
    if skip_if and agent._fsm.state in skip_if:
        return
    from core.event_bus import EventType, EventPriority
    agent._bus.post(EventType.INTERRUPT, {"source": "websocket"}, priority=EventPriority.INTERRUPT)


async def _do_transcription(agent, ws, text: str, audio_buf=None,
                           stt_emotion_state: dict | None = None) -> None:
    # 从 STT emotion state 读取情绪和事件
    emotion = ""
    events: list[str] = []
    if stt_emotion_state:
        emotion = stt_emotion_state.get("emotion", "")
        events = stt_emotion_state.get("events", [])

    # Send transcription back to client so it can display the user's speech
    try:
        msg: dict = {"type": "transcription", "text": text}
        if emotion:
            msg["emotion"] = emotion
        if events:
            msg["events"] = events
        await ws.send_json(msg)
    except Exception:
        pass

    if audio_buf:
        audio_buf.deafen(2.0)
    rid = agent.next_request_id()
    logger.info("STT 识别: %s%s", text[:100], f" [{emotion}]" if emotion and emotion != "NEUTRAL" else "")
    from core.event_bus import EventType
    agent._bus.post(EventType.USER_INPUT, {
        "text": text, "user_name": agent._user_name,
        "emotion": emotion, "events": events,
        "request_id": rid,
    })


async def _send_wake(ws) -> None:
    """Notify frontend that a wake word was detected."""
    try:
        await ws.send_json({"type": "wake"})
    except Exception:
        pass
