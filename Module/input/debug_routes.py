"""Debug API 路由 — 数据库状态, LTM CRUD, Session 管理, 消息归档, LLM 录制。"""

import json
import logging
import os
from aiohttp import web

from core.debug.info import collect, asdict
from core.schema._utils import now_iso
from core.schema.compression import CompressionTrigger
from core.schema.message import Message, MessageRole
from core.schema.types import SessionId
from .api_routes import _archive_to_client

logger = logging.getLogger(__name__)


def register_debug_routes(app: web.Application) -> None:
    """注册所有 /debug/api/* 路由到 aiohttp Application。"""

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

    async def debug_api_handler(request: web.Request) -> web.Response:
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
            # 聚合 token 用量（MongoDB $group，单次查询）
            token_totals = await agent._token_repo.total_tokens_by_session()
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

    async def prompts_list_handler(request: web.Request) -> web.Response:
        """返回最近 N 次 LLM 调用的 prompt 摘要列表。"""
        agent = request.app["agent"]
        recorder = getattr(agent, "_prompt_recorder", None)
        if recorder is None:
            return web.json_response({"records": [], "total": 0, "max": 0})
        return web.json_response(recorder.list_summaries())
    app.router.add_get("/debug/api/prompts", prompts_list_handler)

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
    app.router.add_get(r"/debug/api/prompts/{index:\d+}", prompt_detail_handler)

    async def responses_list_handler(request: web.Request) -> web.Response:
        """返回最近 N 次 LLM 调用的原始回复摘要列表。"""
        agent = request.app["agent"]
        recorder = getattr(agent, "_response_recorder", None)
        if recorder is None:
            return web.json_response({"records": [], "total": 0, "max": 0})
        return web.json_response(recorder.list_summaries())
    app.router.add_get("/debug/api/responses", responses_list_handler)

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
    app.router.add_get(r"/debug/api/responses/{index:\d+}", response_detail_handler)

    # ---- Character 版本历史 API ----

    async def _list_character_versions(agent, content_type: str,
                                        limit: int, before_version: int | None):
        engine = getattr(agent, "_character_engine", None)
        if not engine or not getattr(engine, "_version_repo", None):
            return None, "版本历史不可用", 503
        sid = agent._session.session_id
        versions = await engine._version_repo.list_versions(
            sid, limit=limit, before_version=before_version,
            content_type=content_type,
        )
        total = await engine._version_repo.count_versions(sid, content_type=content_type)
        return {
            "versions": versions,
            "total": total,
            "has_more": len(versions) == limit and versions[-1]["version"] > 1,
        }, None, 200

    async def _character_rollback(agent, content_type: str, target_version: int):
        engine = getattr(agent, "_character_engine", None)
        if not engine or not getattr(engine, "_version_repo", None):
            return None, "版本历史不可用", 503
        sid = agent._session.session_id
        entry = await engine._version_repo.get_version(sid, target_version,
                                                        content_type=content_type)
        if entry is None:
            return None, f"版本 {target_version} 不存在", 404

        # 恢复文本
        if content_type == "l2":
            engine.state.self_l2 = entry["text"]
        elif content_type == "c6":
            engine.state.speaking_style_c6 = entry["text"]

        new_version = await engine._version_repo.append_version(
            session_id=sid, text=entry["text"], timestamp=now_iso(),
            trigger=CompressionTrigger.MANUAL,
            change_summary=[f"回滚到版本 {target_version}"],
            content_type=content_type,
        )

        # L2 rollback 后触发 C6 刷新
        if content_type == "l2":
            try:
                await engine._refresh_c6()
            except Exception:
                pass

        return {"status": "ok", "new_version": new_version, "text": entry["text"]}, None, 200

    # 旧 L2 路由（兼容）
    async def l2_versions_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        limit = int(request.query.get("limit", "20"))
        before = request.query.get("before")
        before_version = int(before) if before else None
        result, error, status = await _list_character_versions(
            agent, "l2", limit, before_version)
        if error:
            return web.json_response({"error": error}, status=status)
        return web.json_response(result)

    app.router.add_get("/debug/api/l2-versions", l2_versions_handler)

    async def l2_rollback_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        try:
            body = await request.json()
            target_version = int(body["version"])
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "缺少 version 或格式错误"}, status=400)
        result, error, status = await _character_rollback(
            agent, "l2", target_version)
        if error:
            return web.json_response({"error": error}, status=status)
        return web.json_response(result)

    app.router.add_post("/debug/api/l2-rollback", l2_rollback_handler)

    # 新通用路由
    async def character_versions_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        content_type = request.query.get("type", "l2")
        if content_type not in ("l2", "c6"):
            return web.json_response({"error": "type 必须为 l2 或 c6"}, status=400)
        limit = int(request.query.get("limit", "20"))
        before = request.query.get("before")
        before_version = int(before) if before else None
        result, error, status = await _list_character_versions(
            agent, content_type, limit, before_version)
        if error:
            return web.json_response({"error": error}, status=status)
        return web.json_response(result)

    app.router.add_get("/debug/api/character-versions", character_versions_handler)

    async def character_rollback_handler(request: web.Request) -> web.Response:
        agent = request.app["agent"]
        try:
            body = await request.json()
            content_type = body["type"]
            target_version = int(body["version"])
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "缺少 type/version 或格式错误"}, status=400)
        if content_type not in ("l2", "c6"):
            return web.json_response({"error": "type 必须为 l2 或 c6"}, status=400)
        result, error, status = await _character_rollback(
            agent, content_type, target_version)
        if error:
            return web.json_response({"error": error}, status=status)
        return web.json_response(result)

    app.router.add_post("/debug/api/character-rollback", character_rollback_handler)

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
