"""Chat API + 消息归档 REST 路由（2026-08-14 批次 C 从 api_routes 拆出）。

模块级 handler（对齐 session_routes 模式），末尾 register_<group>_routes(app)。
"""

import logging

from aiohttp import web

from core.input._common import (
    _archive_msg_to_contract,
    _archive_to_client,
    _sanitize_client_content,
)
from core.memory.session_manager import SessionBusyError, SessionNotFoundError
from core.schema.message import Message
from core.schema.types import AttachmentId, SessionId
from core.storage.message_repo import MessageRepository
from core.storage.session_repo import SessionRepository
from frontend_contract import rest as rest_contract
from core.input._common import _tool_error

logger = logging.getLogger(__name__)


# 客户端 content 收口（CR-09，2026-09-12）：规则与实现在
# `core/input/_common.py::_sanitize_client_content`——REST `/api/chat` 与
# v1 WS content-as-parts **共用同一函数**，避免"同一语义在两条入口上做法相反"（RC-13）。
#
# 纵深防御三层：入口收口（本处）→ `_validate_src` 只按形态白名单放行 →
# `resolve_image_source` 只从附件注册表与白名单目录读文件。


async def chat_api_handler(request: web.Request) -> web.Response:
    """REST Chat API（curl 测试用）。

    Args:
        request: aiohttp Request，JSON body 包含 content/text 和可选的 user_name。
            content 中的 `src` 由服务端按 `attachment_id`/`sticker_id` 构造（CR-09）。

    Returns:
        web.Response: {"ok": True} 或 400 错误。
    """
    agent = request.app["agent"]
    try:
        body = await request.json()
    except web.HTTPRequestEntityTooLarge:
        raise  # chunked 超限放行——交中间件统一 413 信封（F13）
    except Exception:
        return web.json_response(rest_contract.ApiFail(error="无效的 JSON 请求体").wire_dict(), status=400)

    if not isinstance(body, dict):
        return web.json_response(
            rest_contract.ApiFail(error="请求体必须是 JSON 对象").wire_dict(), status=400)

    user_name = body.get("user_name", agent.user_name)
    text = body.get("text", "")

    # CR-09：content 收口——客户端 src 不参与决策
    content, content_err = _sanitize_client_content(body.get("content"))
    if content_err:
        logger.warning("REST /api/chat 拒绝非法 content: %s", content_err)
        return web.json_response(rest_contract.ApiFail(error=content_err).wire_dict(), status=400)

    if not content and not text:
        return web.json_response(rest_contract.ApiFail(error="缺少 content 或 text").wire_dict(), status=400)

    # D3/P2-02：检查 process_message 返回值——队列满不得固定返回成功
    accepted = await agent.process_message(
        text=text, user_name=user_name, content=content)
    if not accepted:
        # CR-08：False 有两个成因，必须报对——停机窗口回 429「队列已满」是
        # 观测面说谎（队列没满，且不会再被消费）。停机是 503，队列满是 429。
        from core.agent import RunState
        if agent.state != RunState.RUNNING:
            return web.json_response(
                rest_contract.ApiFail(error="Agent 正在关停，未接受该消息").wire_dict(),
                status=503)
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
