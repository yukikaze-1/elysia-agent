"""Debug overview routes: aggregated info + database status."""

import logging

from aiohttp import web

from core.debug.info import collect, asdict

logger = logging.getLogger(__name__)


def register_overview_routes(app: web.Application) -> None:
    """注册 overview 路由。"""
    app.router.add_get("/debug/api", debug_api_handler)
    app.router.add_get("/debug/api/db-status", db_status_handler)


async def debug_api_handler(request: web.Request) -> web.Response:
    """Return aggregated debug info for the overview dashboard.

    Args:
        request: aiohttp Request。

    Returns:
        web.Response: JSON with all debug panels (system, memory, scheduler, etc.).
    """
    info = await collect(request.app["agent"])
    return web.json_response(asdict(info))


async def db_status_handler(request: web.Request) -> web.Response:
    """Return database status (LTM count, session count, archive count, health checks).

    Args:
        request: aiohttp Request。

    Returns:
        web.Response: JSON with ltm/session/archive status and health_check results.
    """
    agent = request.app["agent"]
    ltm = agent.long_term
    return web.json_response({
        "ltm": {
            "count": ltm.count,
            "milvus_ok": await agent.memory_repo.health_check(),
            "dirty_count": len(ltm._dirty_ids),
        },
        "session": {
            "count": 1,  # 当前只有单个 session
            "current_id": str(agent.session.session_id),
            "mongodb_ok": await agent.session_repo.health_check(),
        },
        "archive": {
            "count": await agent.message_repo.count(),
            "mongodb_ok": await agent.message_repo.health_check(),
        },
    })
