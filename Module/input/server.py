"""WebSocket server — bridges browser clients to the agent pipeline.

Each client gets an independent WebSocketChannel (outbound) and AudioBuffer
(inbound audio). Text and STT results are routed to the agent's EventBus.
"""

import json
import logging
from pathlib import Path

from aiohttp import web

from core.agent import RunState
from core.schema.message import Message, MessageRole
from core.schema.types import SessionId
from core.schema.memory import MemoryType

from .ws_handler import handle_ws
from .ws_handler import handle_json_message as _handle_json
from .ws_handler import do_interrupt as _do_interrupt
from .ws_handler import do_transcription as _do_transcription
from .ws_handler import send_wake as _send_wake
from .api_routes import register_api_routes, _archive_to_client

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
        return await handle_ws(request, agent, stt_transcribe, stt_emotion_state, wake_word_enabled)

    app.router.add_get("/ws", ws_handler)

    register_api_routes(app)

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
            from core.debug.info import collect, asdict
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
        return web.json_response({
            "ltm": {
                "count": ltm.count,
                "milvus_ok": await agent._memory_repo.health_check(),
                "dirty_count": len(ltm._dirty_ids),
            },
            "session": {
                "count": 1,  # 当前只有单个 session
                "current_id": str(agent._session.session_id),
                "mongodb_ok": await agent._session_repo.health_check(),
            },
            "archive": {
                "count": await agent._message_repo.count(),
                "mongodb_ok": await agent._message_repo.health_check(),
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

    async def ltm_update_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        entry_id = request.match_info.get("entry_id", "")
        if not entry_id:
            return web.json_response({"error": "缺少 entry_id"}, status=400)
        body = await request.json()
        new_text = (body.get("text") or "").strip()
        if not new_text:
            return web.json_response({"error": "text 不能为空"}, status=400)
        ok = await agent._long_term.update_text(entry_id, new_text)
        return web.json_response({"ok": ok})
    app.router.add_put("/debug/api/ltm/{entry_id}", ltm_update_handler)

    async def ltm_clear_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        count = await agent._long_term.clear_all()
        return web.json_response({"ok": True, "deleted": count})
    app.router.add_delete("/debug/api/ltm", ltm_clear_handler)

    # ---- Debug: 消息归档管理 ----

    async def archive_list_handler(request: web.Request) -> web.Response:
        """分页浏览归档消息（debug 专用，offset 分页 + 搜索）。"""
        agent = request.app["agent"]
        repo = agent._message_repo

        try:
            limit = int(request.query.get("limit", "100"))
            limit = max(1, min(limit, 500))
            skip = int(request.query.get("skip", "0"))
            skip = max(0, skip)
        except ValueError:
            limit = 100; skip = 0
        search = request.query.get("search", "").strip()
        role_str = request.query.get("role", "").strip()
        role = MessageRole(role_str) if role_str else None

        try:
            messages, total = await repo.query_offset(skip=skip, limit=limit, search=search, role=role)
            client_msgs = _archive_to_client(messages)
            return web.json_response({
                "messages": client_msgs,
                "total": total,
                "limit": limit,
                "skip": skip,
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    app.router.add_get("/debug/api/archive", archive_list_handler)

    async def archive_delete_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        repo = agent._message_repo
        msg_id = request.match_info.get("msg_id", "")
        if not msg_id:
            return web.json_response({"error": "缺少 msg_id"}, status=400)
        ok = await repo.delete_one(msg_id)
        return web.json_response({"ok": ok})
    app.router.add_delete("/debug/api/archive/{msg_id}", archive_delete_handler)

    async def archive_update_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        repo = agent._message_repo
        msg_id = request.match_info.get("msg_id", "")
        if not msg_id:
            return web.json_response({"error": "缺少 msg_id"}, status=400)
        body = await request.json()
        new_text = (body.get("text") or "").strip()
        if not new_text:
            return web.json_response({"error": "text 不能为空"}, status=400)
        ok = await repo.update_text(msg_id, new_text)
        return web.json_response({"ok": ok})
    app.router.add_put("/debug/api/archive/{msg_id}", archive_update_handler)

    async def archive_clear_handler(request: web.Request) -> web.Response:
        """清空全部归档（debug 专用）。"""
        agent = request.app["agent"]
        deleted = await agent._message_repo.clear_all()
        return web.json_response({"ok": True, "deleted": deleted})

    app.router.add_delete("/debug/api/archive", archive_clear_handler)

    async def sessions_list_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        summaries = await agent._session_repo.list_all()
        if not summaries:
            # MongoDB 不可用，返回内存中的当前 session
            return web.json_response({
                "sessions": [{
                    "session_id": str(agent._session.session_id),
                    "message_count": len(agent._session.messages),
                    "updated_at": "",
                    "token_total": 0,
                }],
            })
        try:
            # 聚合 token 用量（按 session_id）
            from collections import defaultdict
            token_totals: dict[str, int] = defaultdict(int)
            for s in summaries[:50]:  # 限制最多查询 50 条，避免过多 DB 往返
                sid = str(s.session_id)
                try:
                    entries = await agent._token_repo.query_by_session(sid, limit=200)
                    for entry in entries:
                        token_totals[sid] += entry.total_tokens
                except Exception:
                    pass
            return web.json_response({
                "sessions": [
                    {
                        "session_id": str(s.session_id),
                        "message_count": s.message_count,
                        "updated_at": s.updated_at,
                        "token_total": token_totals.get(str(s.session_id), 0),
                    }
                    for s in summaries
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

        # 辅助函数：从 session repo 取 character_state（目标 session，非当前 session）
        async def _get_character_state(sid: SessionId) -> dict:
            result = await agent._session_repo.load(sid)
            if result is not None:
                _, character_state, _ = result  # (messages, CharacterState, MemoryReflectorState)
                return character_state.model_dump(mode="json")
            return {}

        if await agent._message_repo.health_check():
            # 从 message archive（权威源）读取消息
            try:
                msgs, total = await agent._message_repo.query_by_session(sid, limit=20)
                formatted = _sessions_to_client(msgs)
                character_state = await _get_character_state(SessionId(sid))
                return web.json_response({
                    "session_id": sid,
                    "messages": formatted,
                    "params": character_state,
                    "total": total,
                })
            except Exception as e:
                logger.warning("message_repo query_by_session failed: %s", e)

        # MessageRepo 不可用 — 降级读 sessions 快照（或内存）
        result = await agent._session_repo.load(sid)
        if result is None:
            # MongoDB 不可用，返回当前内存 session（仅当 sid 匹配时）
            if sid == str(agent._session.session_id):
                return web.json_response({
                    "session_id": sid,
                    "messages": agent._session.messages_for_client(),
                    "params": agent._session.character_state.model_dump(mode="json"),
                })
            return web.json_response({"error": "会话不存在"}, status=404)
        raw_msgs, character_state, _ = result
        formatted = _sessions_to_client(raw_msgs)
        return web.json_response({
            "session_id": sid,
            "messages": formatted,
            "params": character_state.model_dump(mode="json"),
        })
    app.router.add_get("/debug/api/sessions/{session_id}", session_detail_handler)

    async def session_delete_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        sid = request.match_info.get("session_id", "")
        if not sid:
            return web.json_response({"error": "缺少 session_id"}, status=400)
        if not await agent._session_repo.health_check():
            return web.json_response(
                {"error": "MongoDB 不可用"}, status=503)
        try:
            deleted = await agent._session_repo.delete(sid)
            if sid == str(agent._session.session_id):
                agent._session.clear()
            return web.json_response({"ok": True, "deleted": deleted})
        except Exception as e:
            return web.json_response(
                {"error": f"删除失败: {e}"}, status=500)
    app.router.add_delete("/debug/api/sessions/{session_id}", session_delete_handler)

    def _sessions_to_client(msgs: list[Message] | list[dict]) -> list[dict]:
        """将消息列表转为 session 详情前端兼容格式。接受 Message 或原始 dict。"""
        result = []
        for m in msgs:
            if isinstance(m, Message):
                entry = m.model_dump(mode="json")
            else:
                entry = dict(m)
            item: dict = {"role": entry.get("role", "?"),
                          "timestamp": entry.get("timestamp", ""),
                          "content_type": entry.get("content_type", {})}
            content = entry.get("content", "")
            if isinstance(content, list):
                item["parts"] = [
                    {"content": p.get("content", "") if isinstance(p, dict) else getattr(p, "content", "")}
                    for p in content
                    if (isinstance(p, dict) and p.get("type") == "text")
                       or (not isinstance(p, dict) and getattr(p, "type", None) == "text")
                ]
                item["content"] = content
            elif isinstance(content, str):
                item["parts"] = [{"content": content}]
            elif "parts" in entry:
                item["parts"] = entry["parts"]
            else:
                item["parts"] = [{"content": str(content)}]
            result.append(item)
        return result

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
        if not await agent._session_repo.health_check():
            return web.json_response(
                {"error": "MongoDB 不可用"}, status=503)
        try:
            deleted = await agent._session_repo.clear_all()
            agent._session.clear()
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
