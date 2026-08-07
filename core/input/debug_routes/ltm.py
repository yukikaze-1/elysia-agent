"""Debug LTM routes: CRUD for long-term memory entries。

响应构造 `frontend_contract.debug` 契约模型 + `wire_dict()`；`agent.long_term._entries`
内部 MemoryEntry → 契约 `LtmEntry` 视图（timestamp/poignancy 保留原始数值）。
"""

import json
import logging

from aiohttp import web

from frontend_contract import debug as contract

logger = logging.getLogger(__name__)


def register_ltm_routes(app: web.Application) -> None:
    """注册 LTM CRUD 路由。"""
    app.router.add_get("/debug/api/ltm", ltm_list_handler)
    app.router.add_post("/debug/api/ltm", ltm_insert_handler)
    app.router.add_delete("/debug/api/ltm/{entry_id}", ltm_delete_single_handler)
    app.router.add_put("/debug/api/ltm/{entry_id}", ltm_update_handler)
    app.router.add_delete("/debug/api/ltm", ltm_clear_handler)


async def ltm_list_handler(request: web.Request) -> web.Response:
    """List LTM entries with optional search and type filter.

    Args:
        request: aiohttp Request, query params: search, type, limit, offset。

    Returns:
        web.Response: LtmListResponse 契约 wire（total + entries）。
    """
    agent = request.app["agent"]
    search = request.query.get("search", "").strip()
    mem_type = request.query.get("type", "").strip()
    try:
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
    except ValueError:
        from core.input.server import _protocol_error_response  # 懒导入绕环
        return _protocol_error_response("CONTRACT_VALIDATION", "无效的分页参数", 400)

    if agent.long_term is None:
        from core.input.server import _protocol_error_response  # 懒导入绕环
        return _protocol_error_response("STORAGE_UNAVAILABLE", "长期记忆未初始化", 503,
                                        retryable=True)
    entries = agent.long_term._entries  # noqa: SLF001
    if search:
        entries = [e for e in entries if search in e.text]
    if mem_type:
        entries = [e for e in entries if e.memory_type == mem_type]

    total = len(entries)
    page = entries[offset:offset + limit]
    data = contract.LtmListResponse(
        total=total,
        entries=[
            contract.LtmEntry(
                id=e.id,
                text=e.text,
                memory_type=e.memory_type,
                poignancy=e.poignancy,
                timestamp=e.timestamp,
                metadata=e.metadata,
            )
            for e in page
        ],
    )
    return web.json_response(data.wire_dict())


async def ltm_insert_handler(request: web.Request) -> web.Response:
    """Insert a new LTM entry.

    Args:
        request: aiohttp Request, JSON body: text, memory_type, poignancy。

    Returns:
        web.Response: DebugResult 契约 wire（ok 恒 true）或错误。
    """
    agent = request.app["agent"]
    try:
        body = await request.json()
        text = body.get("text", "").strip()
        if not text:
            from core.input.server import _protocol_error_response  # 懒导入绕环
            return _protocol_error_response("CONTRACT_VALIDATION", "text 不能为空", 400)
        mem_type = body.get("memory_type", "note")
        poignancy = int(body.get("poignancy", 5))
    except web.HTTPRequestEntityTooLarge:
        raise  # chunked 超限放行——交中间件统一 413 信封（F13）
    except (json.JSONDecodeError, ValueError):
        from core.input.server import _protocol_error_response  # 懒导入绕环
        return _protocol_error_response("CONTRACT_VALIDATION", "无效的请求体", 400)

    await agent.long_term.insert(text, memory_type=mem_type, poignancy=poignancy)
    logger.info("DEBUG API: ltm_insert type=%s, text_len=%d, poignancy=%d", mem_type, len(text), poignancy)
    return web.json_response(contract.DebugResult(ok=True).wire_dict())


async def ltm_delete_single_handler(request: web.Request) -> web.Response:
    """Delete a single LTM entry by ID.

    Args:
        request: aiohttp Request, URL path: /{entry_id}。

    Returns:
        web.Response: DebugResult 契约 wire（ok 布尔）或错误。
    """
    agent = request.app["agent"]
    entry_id = request.match_info.get("entry_id", "")
    if not entry_id:
        from core.input.server import _protocol_error_response  # 懒导入绕环
        return _protocol_error_response("CONTRACT_VALIDATION", "缺少 entry_id", 400)
    ok = await agent.long_term.delete(entry_id)
    logger.warning("DEBUG API: ltm_delete entry_id=%s, ok=%s", entry_id, ok)
    return web.json_response(contract.DebugResult(ok=ok).wire_dict())


async def ltm_update_handler(request: web.Request) -> web.Response:
    """Update an LTM entry's text by ID.

    Args:
        request: aiohttp Request, URL path: /{entry_id}, JSON body: text。

    Returns:
        web.Response: DebugResult 契约 wire（ok 布尔）或错误。
    """
    agent = request.app["agent"]
    entry_id = request.match_info.get("entry_id", "")
    if not entry_id:
        from core.input.server import _protocol_error_response  # 懒导入绕环
        return _protocol_error_response("CONTRACT_VALIDATION", "缺少 entry_id", 400)
    body = await request.json()
    new_text = (body.get("text") or "").strip()
    if not new_text:
        from core.input.server import _protocol_error_response  # 懒导入绕环
        return _protocol_error_response("CONTRACT_VALIDATION", "text 不能为空", 400)
    ok = await agent.long_term.update_text(entry_id, new_text)
    logger.info("DEBUG API: ltm_update entry_id=%s, text_len=%d, ok=%s", entry_id, len(new_text), ok)
    return web.json_response(contract.DebugResult(ok=ok).wire_dict())


async def ltm_clear_handler(request: web.Request) -> web.Response:
    """Clear all LTM entries.

    Args:
        request: aiohttp Request。

    Returns:
        web.Response: DebugResult 契约 wire（ok true + deleted 计数）。
    """
    agent = request.app["agent"]
    count = await agent.long_term.clear_all()
    logger.warning("DEBUG API: ltm_clear deleted=%d", count)
    return web.json_response(contract.DebugResult(ok=True, deleted=count).wire_dict())
