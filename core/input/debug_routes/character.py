"""Debug character routes: L2 / C6 version history + rollback."""

import logging
from typing import TYPE_CHECKING

from aiohttp import web

from core.schema._utils import now_iso
from core.schema.compression import CompressionTrigger

if TYPE_CHECKING:
    from core.agent import Agent

logger = logging.getLogger(__name__)


def register_character_routes(app: web.Application) -> None:
    """注册 Character 版本历史路由。"""
    app.router.add_get("/debug/api/l2-versions", l2_versions_handler)
    app.router.add_post("/debug/api/l2-rollback", l2_rollback_handler)
    app.router.add_get("/debug/api/character-versions", character_versions_handler)
    app.router.add_post("/debug/api/character-rollback", character_rollback_handler)


async def _list_character_versions(agent: "Agent", content_type: str,
                                    limit: int, before_version: int | None):
    """List character version history (L2 or C6) for the current session.

    Args:
        agent: Agent instance with character engine.
        content_type: "l2" or "c6".
        limit: Maximum number of versions to return.
        before_version: Optional upper bound version number (exclusive).

    Returns:
        tuple: (result_dict | None, error_str | None, http_status_code)
    """
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


async def _character_rollback(agent: "Agent", content_type: str, target_version: int):
    """Rollback character content (L2 or C6) to a specific version.

    Args:
        agent: Agent instance with character engine.
        content_type: "l2" or "c6".
        target_version: Version number to rollback to.

    Returns:
        tuple: (result_dict | None, error_str | None, http_status_code)
    """
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


async def l2_versions_handler(request: web.Request) -> web.Response:
    """List L2 self-image version history (legacy route).

    Args:
        request: aiohttp Request, query params: limit, before。

    Returns:
        web.Response: {"versions": [...], "total": ..., "has_more": ...}。
    """
    agent = request.app["agent"]
    limit = int(request.query.get("limit", "20"))
    before = request.query.get("before")
    before_version = int(before) if before else None
    result, error, status = await _list_character_versions(
        agent, "l2", limit, before_version)
    if error:
        return web.json_response({"error": error}, status=status)
    return web.json_response(result)


async def l2_rollback_handler(request: web.Request) -> web.Response:
    """Rollback L2 self-image to a specific version (legacy route).

    Args:
        request: aiohttp Request, JSON body: version。

    Returns:
        web.Response: {"status": "ok", "new_version": ..., "text": ...} or error.
    """
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
    if result is None:
        return web.json_response({"error": "内部错误"}, status=500)
    logger.warning("DEBUG API: l2_rollback version=%d, new_version=%s",
                  target_version, result.get("new_version"))
    return web.json_response(result)


async def character_versions_handler(request: web.Request) -> web.Response:
    """List character version history (L2 or C6).

    Args:
        request: aiohttp Request, query params: type (l2/c6), limit, before。

    Returns:
        web.Response: {"versions": [...], "total": ..., "has_more": ...}。
    """
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


async def character_rollback_handler(request: web.Request) -> web.Response:
    """Rollback character content (L2 or C6) to a specific version.

    Args:
        request: aiohttp Request, JSON body: type (l2/c6), version。

    Returns:
        web.Response: {"status": "ok", "new_version": ..., "text": ...} or error.
    """
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
    if result is None:
        return web.json_response({"error": "内部错误"}, status=500)
    logger.warning("DEBUG API: character_rollback type=%s, version=%d, new_version=%s",
                  content_type, target_version, result.get("new_version"))
    return web.json_response(result)
