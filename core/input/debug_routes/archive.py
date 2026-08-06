"""Debug archive + sessions routes: message archive CRUD + session management。

响应构造 `frontend_contract.debug` 契约模型 + `wire_dict()`；归档消息经
`api_routes._archive_to_client`（内部 core.schema.Message → 客户端 dict）再经
`_archive_msg_to_contract`（block_index → content_type 归一化）→ 契约 `rest.Message` 视图；
session 详情消息经 `_common.sessions_to_client` → 契约 `SessionDetailMessage` 视图。
"""

import logging

from aiohttp import web

from core.schema.message import MessageRole
from core.schema.types import SessionId
from core.input.api_routes import _archive_to_client, _archive_msg_to_contract
from core.input.debug_routes._common import sessions_to_client
from core.storage.attachment_release import (
    release_all_attachments,
    release_message_attachments,
    release_session_attachments,
)
from frontend_contract import debug as contract

logger = logging.getLogger(__name__)


def register_archive_routes(app: web.Application) -> None:
    """注册 Archive + Sessions 路由。"""
    app.router.add_get("/debug/api/archive", archive_list_handler)
    app.router.add_delete("/debug/api/archive/{msg_id}", archive_delete_handler)
    app.router.add_put("/debug/api/archive/{msg_id}", archive_update_handler)
    app.router.add_delete("/debug/api/archive", archive_clear_handler)

    app.router.add_get("/debug/api/sessions", sessions_list_handler)
    app.router.add_get("/debug/api/sessions/{session_id}", session_detail_handler)
    app.router.add_delete("/debug/api/sessions/{session_id}", session_delete_handler)
    app.router.add_delete("/debug/api/sessions", sessions_clear_handler)


async def archive_list_handler(request: web.Request) -> web.Response:
    """分页浏览归档消息（debug 专用，offset 分页 + 搜索）。

    Args:
        request: aiohttp Request, query params: limit, skip, search, role。

    Returns:
        web.Response: ArchiveListResponse 契约 wire（messages/total/limit/skip）。
    """
    agent = request.app["agent"]
    repo = agent.message_repo

    try:
        limit = int(request.query.get("limit", "100"))
        limit = max(1, min(limit, 500))
        skip = int(request.query.get("skip", "0"))
        skip = max(0, skip)
    except ValueError:
        limit = 100; skip = 0
    search = request.query.get("search", "").strip()
    role_str = request.query.get("role", "").strip()
    try:
        role = MessageRole(role_str) if role_str else None
    except ValueError:
        return web.json_response({"error": f"非法 role: {role_str}"}, status=400)

    try:
        messages, total = await repo.query_offset(skip=skip, limit=limit, search=search, role=role)
        client_msgs = _archive_to_client(messages)
        contract_msgs = [m for d in client_msgs if (m := _archive_msg_to_contract(d)) is not None]
        data = contract.ArchiveListResponse(
            messages=contract_msgs, total=total, limit=limit, skip=skip)
        return web.json_response(data.wire_dict())
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def archive_delete_handler(request: web.Request) -> web.Response:
    """Delete a single archived message by ID，同时从当前 session 中移除。

    Args:
        request: aiohttp Request, URL path: /{msg_id}。

    Returns:
        web.Response: DebugResult 契约 wire（ok + session_purged 计数）。
    """
    agent = request.app["agent"]
    repo = agent.message_repo
    msg_id = request.match_info.get("msg_id", "")
    if not msg_id:
        return web.json_response({"error": "缺少 msg_id"}, status=400)
    # 批次 7：删除前列出消息释放附件 refs（附件软删联动）
    msg = await repo.get_by_id(msg_id)
    if msg is not None:
        await release_message_attachments(agent, msg)
    ok = await repo.delete_one(msg_id)
    # 同步清理当前 session —— archive 和 session 是独立存储，
    # 只删 archive 不改 session 会导致 LLM prompt 仍包含已删消息
    session_purged = agent.purge_message_from_session(msg_id)
    if session_purged:
        await agent.session.save()
    logger.info("DEBUG API: archive_delete msg_id=%s, ok=%s, session_purged=%d", msg_id, ok, session_purged)
    return web.json_response(contract.DebugResult(ok=ok, session_purged=session_purged).wire_dict())


async def archive_update_handler(request: web.Request) -> web.Response:
    """Update an archived message's text by ID.

    Args:
        request: aiohttp Request, URL path: /{msg_id}, JSON body: text。

    Returns:
        web.Response: DebugResult 契约 wire（ok 布尔）。
    """
    agent = request.app["agent"]
    repo = agent.message_repo
    msg_id = request.match_info.get("msg_id", "")
    if not msg_id:
        return web.json_response({"error": "缺少 msg_id"}, status=400)
    body = await request.json()
    new_text = (body.get("text") or "").strip()
    if not new_text:
        return web.json_response({"error": "text 不能为空"}, status=400)
    ok = await repo.update_text(msg_id, new_text)
    logger.info("DEBUG API: archive_update msg_id=%s, text_len=%d, ok=%s", msg_id, len(new_text), ok)
    return web.json_response(contract.DebugResult(ok=ok).wire_dict())


async def archive_clear_handler(request: web.Request) -> web.Response:
    """清空全部归档并同步清空当前 session（debug 专用）。

    Args:
        request: aiohttp Request。

    Returns:
        web.Response: DebugResult 契约 wire（ok true + deleted 计数）。
    """
    agent = request.app["agent"]
    # 批次 7：清空前列出全部消息释放附件 refs（附件软删联动）
    await release_all_attachments(agent)
    deleted = await agent.message_repo.clear_all()
    # 同步清空当前 session：archive 删光后 session 也要保持一致
    session = agent.session
    n_session = len(session.messages)
    if n_session:
        session.clear()
        await session.save()
    logger.warning("DEBUG API: archive_clear deleted=%d, session_cleared=%d", deleted, n_session)
    return web.json_response(contract.DebugResult(ok=True, deleted=deleted).wire_dict())


async def sessions_list_handler(request: web.Request) -> web.Response:
    """List all sessions with message counts and token totals.

    Args:
        request: aiohttp Request。

    Returns:
        web.Response: SessionListResponse 契约 wire（sessions 列表）。
    """
    agent = request.app["agent"]
    summaries = await agent.session_repo.list_all()
    if not summaries:
        # MongoDB 不可用，返回内存中的当前 session
        data = contract.SessionListResponse(
            sessions=[contract.SessionInfo(
                session_id=str(agent.session.session_id),
                message_count=len(agent.session.messages),
                updated_at="",
                token_total=0,
            )],
        )
        return web.json_response(data.wire_dict())
    try:
        # 聚合 token 用量（MongoDB $group，单次查询）
        token_totals = await agent.token_repo.total_tokens_by_session()
        data = contract.SessionListResponse(
            sessions=[
                contract.SessionInfo(
                    session_id=str(s.session_id),
                    message_count=s.message_count,
                    updated_at=s.updated_at,
                    token_total=token_totals.get(str(s.session_id), 0),
                )
                for s in summaries
            ],
        )
        return web.json_response(data.wire_dict())
    except Exception as e:
        return web.json_response(
            {"error": f"查询失败: {e}"}, status=500)


async def session_detail_handler(request: web.Request) -> web.Response:
    """Get session detail: messages, character state params, and total count.

    Args:
        request: aiohttp Request, URL path: /{session_id}。

    Returns:
        web.Response: SessionDetailResponse 契约 wire（session_id/messages/params/total）。
    """
    agent = request.app["agent"]
    sid = request.match_info.get("session_id", "")
    if not sid:
        return web.json_response({"error": "缺少 session_id"}, status=400)

    # 辅助函数：从 session repo 取 character_state（目标 session，非当前 session）
    async def _get_character_state(sid: SessionId) -> dict:
        result = await agent.session_repo.load(sid)
        if result is not None:
            _, character_state, _, _ = result  # (messages, CharacterState, MemoryReflectorState, ServiceState)
            return character_state.model_dump(mode="json")
        return {}

    if await agent.message_repo.health_check():
        # 从 message archive（权威源）读取消息
        try:
            msgs, total = await agent.message_repo.query_by_session(sid, limit=20)
            formatted = sessions_to_client(msgs)
            character_state = await _get_character_state(SessionId(sid))
            messages = [contract.SessionDetailMessage.model_validate(m) for m in formatted]
            data = contract.SessionDetailResponse(
                session_id=sid, messages=messages,
                params=character_state, total=total)
            return web.json_response(data.wire_dict())
        except Exception as e:
            logger.warning("message_repo query_by_session failed: %s", e)

    # MessageRepo 不可用 — 降级读 sessions 快照（或内存）
    result = await agent.session_repo.load(sid)
    if result is None:
        # MongoDB 不可用，返回当前内存 session（仅当 sid 匹配时）
        if sid == str(agent.session.session_id):
            msgs = agent.session.messages_for_client()
            messages = [contract.SessionDetailMessage.model_validate(m) for m in msgs]
            data = contract.SessionDetailResponse(
                session_id=sid, messages=messages,
                params=agent.session.character_state.model_dump(mode="json"))
            return web.json_response(data.wire_dict())
        return web.json_response({"error": "会话不存在"}, status=404)
    raw_msgs, character_state, _, _ = result
    formatted = sessions_to_client(raw_msgs)
    messages = [contract.SessionDetailMessage.model_validate(m) for m in formatted]
    data = contract.SessionDetailResponse(
        session_id=sid, messages=messages,
        params=character_state.model_dump(mode="json"))
    return web.json_response(data.wire_dict())


async def session_delete_handler(request: web.Request) -> web.Response:
    """Delete a session by ID. Clears in-memory session if it matches the current one.

    Args:
        request: aiohttp Request, URL path: /{session_id}。

    Returns:
        web.Response: DebugResult 契约 wire（ok true + deleted 计数）或错误。
    """
    agent = request.app["agent"]
    sid = request.match_info.get("session_id", "")
    if not sid:
        return web.json_response({"error": "缺少 session_id"}, status=400)
    if not await agent.session_repo.health_check():
        return web.json_response(
            {"error": "MongoDB 不可用"}, status=503)
    try:
        # 批次 7：删除前释放该会话归档消息的附件 refs（附件软删联动）
        try:
            await release_session_attachments(agent, sid)
        except Exception as e:
            logger.warning("session_delete 附件释放失败 sid=%s: %s", sid, e)
        deleted = await agent.session_repo.delete(sid)
        logger.warning("DEBUG API: session_delete sid=%s, current=%s, deleted=%d",
                      sid, str(agent.session.session_id), deleted)
        if sid == str(agent.session.session_id):
            agent.session.clear()
        return web.json_response(contract.DebugResult(ok=True, deleted=deleted).wire_dict())
    except Exception as e:
        return web.json_response(
            {"error": f"删除失败: {e}"}, status=500)


async def sessions_clear_handler(request: web.Request) -> web.Response:
    """Clear all sessions and in-memory session state.

    Args:
        request: aiohttp Request。

    Returns:
        web.Response: DebugResult 契约 wire（ok true + deleted 计数）或错误。
    """
    agent = request.app["agent"]
    if not await agent.session_repo.health_check():
        return web.json_response(
            {"error": "MongoDB 不可用"}, status=503)
    try:
        # 批次 7：清空前释放全部归档消息的附件 refs（附件软删联动）
        try:
            await release_all_attachments(agent)
        except Exception as e:
            logger.warning("sessions_clear 附件释放失败: %s", e)
        deleted = await agent.session_repo.clear_all()
        logger.warning("DEBUG API: sessions_clear deleted=%d", deleted)
        agent.session.clear()
        return web.json_response(contract.DebugResult(ok=True, deleted=deleted).wire_dict())
    except Exception as e:
        return web.json_response(
            {"error": f"清空失败: {e}"}, status=500)
