"""Debug API 路由 — 数据库状态, LTM CRUD, Session 管理, 消息归档, LLM 录制。

所有增删改操作均记录日志：info（资源级修改）/ warning（批量删除、回滚）。
"""

import logging
from collections.abc import Callable

from aiohttp import web

logger = logging.getLogger(__name__)


def register_debug_routes(
    app: web.Application,
    check_auth: Callable[[web.Request], bool] | None = None,
) -> None:
    """注册所有 /debug/api/* 路由到 aiohttp Application。

    Args:
        app: aiohttp Application 实例。
        check_auth: 可选认证函数，接收 Request 返回 bool。
            提供时，所有 /debug/api/* 请求需通过认证。
    """

    if check_auth is not None:
        @web.middleware
        async def _debug_auth_middleware(
            request: web.Request, handler: Callable,
        ) -> web.StreamResponse:
            if request.path.startswith("/debug/api") and not check_auth(request):
                code = "AUTH_REQUIRED" if not request.headers.get("Authorization") else "AUTH_INVALID"
                msg = "需要认证" if code == "AUTH_REQUIRED" else "认证失败"
                logger.warning("认证失败 code=%s path=%s", code, request.path)
                from core.input.server import _protocol_error_response  # 懒导入绕环
                return _protocol_error_response(code, msg, 401)
            return await handler(request)
        app.middlewares.append(_debug_auth_middleware)

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

    # WorkflowEngine debug API — SSE 实时推送任务状态
    from Module.workflow_engine.debug_api import WorkflowDebugAPI

    ts = app.get('task_system')
    if ts is not None:
        task_debug_api = WorkflowDebugAPI(ts)
        app['task_debug_api'] = task_debug_api
        app.router.add_get("/debug/api/tasks", task_debug_api.snapshot_handler)
        app.router.add_get("/debug/api/tasks/stream", task_debug_api.sse_handler)
    else:
        app['task_debug_api'] = None

    app.on_startup.append(_on_startup)
    app.on_shutdown.append(_on_shutdown)


# WorkflowDebugAPI 生命周期管理

async def _on_startup(app: web.Application) -> None:
    """应用启动时启动 WorkflowDebugAPI。

    Args:
        app: aiohttp Application 实例。
    """
    task_api = app.get('task_debug_api')
    if task_api:
        await task_api.start()


async def _on_shutdown(app: web.Application) -> None:
    """应用关闭时停止 WorkflowDebugAPI。

    Args:
        app: aiohttp Application 实例。
    """
    task_api = app.get('task_debug_api')
    if task_api:
        await task_api.stop()
