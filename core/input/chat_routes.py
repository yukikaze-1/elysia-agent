"""Chat API + 消息归档 REST 路由（2026-08-14 批次 C 从 api_routes 拆出）。

模块级 handler（对齐 session_routes 模式），末尾 register_<group>_routes(app)。
"""

import logging
from typing import Any

from aiohttp import web

from core.input._common import _archive_msg_to_contract, _archive_to_client
from core.memory.session_manager import SessionBusyError, SessionNotFoundError
from core.schema.message import Message
from core.schema.types import AttachmentId, SessionId
from core.storage.message_repo import MessageRepository
from core.storage.session_repo import SessionRepository
from frontend_contract import rest as rest_contract
from core.input._common import _tool_error

logger = logging.getLogger(__name__)


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

    # D3/P2-02：检查 process_message 返回值——队列满不得固定返回成功
    accepted = await agent.process_message(
        text=text, user_name=user_name, content=content)
    if not accepted:
        return web.json_response(
            rest_contract.ApiFail(error="Agent 忙，消息队列已满").wire_dict(),
            status=429)
    return web.json_response(rest_contract.ApiOk().wire_dict())


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

def register_chat_routes(app: web.Application) -> None:
    """注册 Chat API + 消息归档路由（2 端点）。"""
    app.router.add_post("/api/chat", chat_api_handler)
    app.router.add_get("/api/messages", messages_handler)
