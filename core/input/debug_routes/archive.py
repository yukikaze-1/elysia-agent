"""Debug 归档 + 会话路由：消息归档 CRUD + 会话管理。

响应构造 `frontend_contract.debug` 契约模型 + `wire_dict()`；归档消息经
`_shared._archive_to_client`（内部 core.schema.Message → 客户端 dict，含 character
快照 camelCase 转换）再经 `_archive_msg_to_contract`（block_index → content_type 归一化）
→ 契约 `rest.Message` 视图；session 详情消息经 `_common.sessions_to_client` →
契约 `SessionDetailMessage` 视图。
"""

import logging

from aiohttp import web

from core.schema.message import MessageRole
from core.schema.types import SessionId
from core.input.debug_routes._shared import (
    _archive_msg_to_contract,
    _archive_to_client,
    _protocol_error_response,
)
from core.input.debug_routes._common import sessions_to_client
from core.storage.attachment_release import (
    release_all_attachments,
    release_message_attachments,
    release_session_attachments,
)
from core.storage.base import StorageUnavailableError
from core.storage.destructive_guard import DestructiveConfirmRequired, require_confirm
from core.storage.message_repo import CONFIRM_TOKEN as MESSAGE_CONFIRM_TOKEN
from core.storage.session_repo import CONFIRM_TOKEN as SESSION_CONFIRM_TOKEN
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
        return _protocol_error_response("CONTRACT_VALIDATION", f"非法 role: {role_str}", 400)

    try:
        messages, total = await repo.query_offset(skip=skip, limit=limit, search=search, role=role)
        client_msgs = _archive_to_client(messages)
        contract_msgs = [m for d in client_msgs if (m := _archive_msg_to_contract(d)) is not None]
        data = contract.ArchiveListResponse(
            messages=contract_msgs, total=total, limit=limit, skip=skip)
        return web.json_response(data.wire_dict())
    except Exception:
        logger.warning("DEBUG API: 归档查询失败", exc_info=True)
        return _protocol_error_response("INTERNAL", "查询失败", 500)


async def archive_delete_handler(request: web.Request) -> web.Response:
    """按 ID 删除单条归档消息，同时从当前 session 中移除。

    Args:
        request: aiohttp Request, URL path: /{msg_id}。

    Returns:
        web.Response: DebugResult 契约 wire（ok + session_purged 计数）。
    """
    agent = request.app["agent"]
    repo = agent.message_repo
    msg_id = request.match_info.get("msg_id", "")
    if not msg_id:
        return _protocol_error_response("CONTRACT_VALIDATION", "缺少 msg_id", 400)
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
    """按 ID 更新归档消息的文本。

    Args:
        request: aiohttp Request, URL path: /{msg_id}, JSON body: text。

    Returns:
        web.Response: DebugResult 契约 wire（ok 布尔）。
    """
    agent = request.app["agent"]
    repo = agent.message_repo
    msg_id = request.match_info.get("msg_id", "")
    if not msg_id:
        return _protocol_error_response("CONTRACT_VALIDATION", "缺少 msg_id", 400)
    body = await request.json()
    new_text = (body.get("text") or "").strip()
    if not new_text:
        return _protocol_error_response("CONTRACT_VALIDATION", "text 不能为空", 400)
    ok = await repo.update_text(msg_id, new_text)
    logger.info("DEBUG API: archive_update msg_id=%s, text_len=%d, ok=%s", msg_id, len(new_text), ok)
    return web.json_response(contract.DebugResult(ok=ok).wire_dict())


async def archive_clear_handler(request: web.Request) -> web.Response:
    """清空全部归档并同步清空当前 session（**破坏性**，需 `?confirm=archive` 类型化确认）。

    Args:
        request: aiohttp Request；query `confirm` 必填，须等于 MessageRepository.CONFIRM_TOKEN。

    Returns:
        web.Response: DebugResult 契约 wire（ok true + deleted 计数）；
        缺 confirm → 400，confirm 不匹配 → 400，存储不可用 → 503。
    """
    agent = request.app["agent"]
    confirm = (request.query.get("confirm") or "").strip()
    if not confirm:
        return _protocol_error_response(
            "CONTRACT_VALIDATION",
            "清空归档是破坏性且不可逆的操作，需 ?confirm=<目标资源名> 显式确认",
            400,
        )
    # CR-04：类型化确认必须在**任何副作用之前**完成。
    # 此前只在 clear_all() 内校验，而 release_all_attachments()（不可逆的附件软删）
    # 排在它之前 —— confirm 写错时先软删全部附件、再返回 400，即"防误删守卫触发了
    # 不可逆删除"。存储层仍会再校验一次（权威），此处是**顺序**修复。
    try:
        require_confirm(target=MESSAGE_CONFIRM_TOKEN,
                        confirm=confirm, operation="清空归档")
    except DestructiveConfirmRequired as e:
        return _protocol_error_response("CONTRACT_VALIDATION", str(e), 400)
    # 批次 7：清空前列出全部消息释放附件 refs（附件软删联动）
    await release_all_attachments(agent)
    try:
        deleted = await agent.message_repo.clear_all(confirm=confirm)
    except DestructiveConfirmRequired as e:
        return _protocol_error_response("CONTRACT_VALIDATION", str(e), 400)
    except StorageUnavailableError as e:
        return _protocol_error_response("STORAGE_UNAVAILABLE", str(e), 503, retryable=True)
    # 同步清空当前 session：archive 删光后 session 也要保持一致
    session = agent.session
    n_session = len(session.messages)
    if n_session:
        session.clear()
        await session.save()
    logger.warning("DEBUG API: archive_clear deleted=%d, session_cleared=%d", deleted, n_session)
    return web.json_response(contract.DebugResult(ok=True, deleted=deleted).wire_dict())


async def sessions_list_handler(request: web.Request) -> web.Response:
    """列出所有会话及其消息数与 token 总量。

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
    except Exception:
        logger.warning("DEBUG API: 消息查询失败", exc_info=True)
        return _protocol_error_response(
            "INTERNAL", "查询失败", 500)


async def session_detail_handler(request: web.Request) -> web.Response:
    """获取会话详情：消息、character state 参数与总数。

    Args:
        request: aiohttp Request, URL path: /{session_id}。

    Returns:
        web.Response: SessionDetailResponse 契约 wire（session_id/messages/params/total）。
    """
    agent = request.app["agent"]
    sid = request.match_info.get("session_id", "")
    if not sid:
        return _protocol_error_response("CONTRACT_VALIDATION", "缺少 session_id", 400)

    # 辅助函数：从 session repo 取 character_state（目标 session，非当前 session）
    async def _get_character_state(sid: SessionId) -> dict:
        result = await agent.session_repo.load(sid)
        if result is not None:
            _, character_state, _, _, _ = result  # (messages, CharacterState, MemoryReflectorState, ServiceState, character_binding)
            return character_state.model_dump(mode="json")
        return {}

    if await agent.message_repo.health_check():
        # 从 message archive（权威源）读取消息
        try:
            msgs, total = await agent.message_repo.query_by_session(SessionId(sid), limit=20)
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
    result = await agent.session_repo.load(SessionId(sid))
    if result is None:
        # MongoDB 不可用，返回当前内存 session（仅当 sid 匹配时）
        if sid == str(agent.session.session_id):
            msgs = agent.session.messages_for_client()
            messages = [contract.SessionDetailMessage.model_validate(m) for m in msgs]
            # Task 12（§6.5 读端迁移）：params 读当前 instance engine.state（实例权威）。
            # 此前读 session.character_state——M3 后 state 权威在 runtime.state，
            # session.character_state 不再被写（stale）。
            data = contract.SessionDetailResponse(
                session_id=sid, messages=messages,
                params=agent.character_engine.state.model_dump(mode="json"))
            return web.json_response(data.wire_dict())
        return _protocol_error_response("SESSION_NOT_FOUND", "会话不存在", 404)
    raw_msgs, character_state, _, _, _ = result
    formatted = sessions_to_client(raw_msgs)
    messages = [contract.SessionDetailMessage.model_validate(m) for m in formatted]
    data = contract.SessionDetailResponse(
        session_id=sid, messages=messages,
        params=character_state.model_dump(mode="json"))
    return web.json_response(data.wire_dict())


async def session_delete_handler(request: web.Request) -> web.Response:
    """按 ID 删除会话。若与当前会话匹配，则同时清空内存中的会话。

    Args:
        request: aiohttp Request, URL path: /{session_id}。

    Returns:
        web.Response: DebugResult 契约 wire（ok true + deleted 计数）或错误。
    """
    agent = request.app["agent"]
    sid = request.match_info.get("session_id", "")
    if not sid:
        return _protocol_error_response("CONTRACT_VALIDATION", "缺少 session_id", 400)
    if not await agent.session_repo.health_check():
        return _protocol_error_response(
            "STORAGE_UNAVAILABLE", "MongoDB 不可用", 503, retryable=True)
    try:
        # 批次 7：删除前释放该会话归档消息的附件 refs（附件软删联动）
        try:
            await release_session_attachments(agent, SessionId(sid))
        except Exception as e:
            logger.warning("session_delete 附件释放失败 sid=%s: %s", sid, e)
        deleted = await agent.session_repo.delete(SessionId(sid))
        logger.warning("DEBUG API: session_delete sid=%s, current=%s, deleted=%d",
                      sid, str(agent.session.session_id), deleted)
        if sid == str(agent.session.session_id):
            agent.session.clear()
        return web.json_response(contract.DebugResult(ok=True, deleted=deleted).wire_dict())
    except Exception:
        logger.warning("DEBUG API: 删除归档消息失败", exc_info=True)
        return _protocol_error_response(
            "INTERNAL", "删除失败", 500)


async def sessions_clear_handler(request: web.Request) -> web.Response:
    """清空所有会话及内存中的会话状态（**破坏性**，需 `?confirm=sessions` 类型化确认）。

    Args:
        request: aiohttp Request；query `confirm` 必填，须等于 SessionRepository.CONFIRM_TOKEN。

    Returns:
        web.Response: DebugResult 契约 wire（ok true + deleted 计数）；
        缺 confirm → 400，confirm 不匹配 → 400，MongoDB 不可用 → 503。
    """
    agent = request.app["agent"]
    # 校验先于存储健康检查：请求本身不合法与后端状态无关
    confirm = (request.query.get("confirm") or "").strip()
    if not confirm:
        return _protocol_error_response(
            "CONTRACT_VALIDATION",
            "清空会话是破坏性且不可逆的操作，需 ?confirm=<目标资源名> 显式确认",
            400,
        )
    if not await agent.session_repo.health_check():
        return _protocol_error_response(
            "STORAGE_UNAVAILABLE", "MongoDB 不可用", 503, retryable=True)
    # CR-04：类型化确认先于副作用（同 archive_clear_handler——此前
    # release_all_attachments 排在 clear_all 的 confirm 校验之前）。
    try:
        require_confirm(target=SESSION_CONFIRM_TOKEN,
                        confirm=confirm, operation="清空会话")
    except DestructiveConfirmRequired as e:
        return _protocol_error_response("CONTRACT_VALIDATION", str(e), 400)
    try:
        # 批次 7：清空前释放全部归档消息的附件 refs（附件软删联动）
        try:
            await release_all_attachments(agent)
        except Exception as e:
            logger.warning("sessions_clear 附件释放失败: %s", e)
        deleted = await agent.session_repo.clear_all(confirm=confirm)
        logger.warning("DEBUG API: sessions_clear deleted=%d", deleted)
        agent.session.clear()
        return web.json_response(contract.DebugResult(ok=True, deleted=deleted).wire_dict())
    except DestructiveConfirmRequired as e:
        return _protocol_error_response("CONTRACT_VALIDATION", str(e), 400)
    except StorageUnavailableError as e:
        return _protocol_error_response("STORAGE_UNAVAILABLE", str(e), 503, retryable=True)
    except Exception:
        logger.warning("DEBUG API: 清空会话失败", exc_info=True)
        return _protocol_error_response(
            "INTERNAL", "清空失败", 500)
