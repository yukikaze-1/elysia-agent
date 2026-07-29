"""Debug API 路由 — 数据库状态, LTM CRUD, Session 管理, 消息归档, LLM 录制。

所有增删改操作均记录日志：info（资源级修改）/ warning（批量删除、回滚）。
"""

import logging

from aiohttp import web

logger = logging.getLogger(__name__)


def register_debug_routes(app: web.Application) -> None:
    """注册所有 /debug/api/* 路由到 aiohttp Application。"""

    from .overview import register_overview_routes
    from .ltm import register_ltm_routes
    from .archive import register_archive_routes
    from .recording import register_recording_routes
    from .character import register_character_routes

    register_overview_routes(app)
    register_ltm_routes(app)
    register_archive_routes(app)
    register_recording_routes(app)
    register_character_routes(app)

    # TaskSystem debug API — SSE 实时推送任务状态
    from Module.task_system.debug_api import TaskDebugAPI

    ts = app.get('task_system')
    if ts is not None:
        task_debug_api = TaskDebugAPI(ts)
        app['task_debug_api'] = task_debug_api
        app.router.add_get("/debug/api/tasks", task_debug_api.snapshot_handler)
        app.router.add_get("/debug/api/tasks/stream", task_debug_api.sse_handler)
    else:
        app['task_debug_api'] = None

    app.on_startup.append(_on_startup)
    app.on_shutdown.append(_on_shutdown)


# TaskDebugAPI 生命周期管理

async def _on_startup(app: web.Application) -> None:
    """Start TaskDebugAPI on application startup.

    Args:
        app: aiohttp Application instance.
    """
    task_api = app.get('task_debug_api')
    if task_api:
        await task_api.start()


async def _on_shutdown(app: web.Application) -> None:
    """Stop TaskDebugAPI on application shutdown.

    Args:
        app: aiohttp Application instance.
    """
    task_api = app.get('task_debug_api')
    if task_api:
        await task_api.stop()
