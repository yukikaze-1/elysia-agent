"""WebSocket server — bridges browser clients to the agent pipeline.

Each client gets an independent WebSocketChannel (outbound) and AudioBuffer
(inbound audio). Text and STT results are routed to the agent's EventBus.
"""

import json
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from core.agent import Agent

from core.input.ws_handler import handle_ws
from core.input.api_routes import register_api_routes
from core.input.debug_routes import register_debug_routes
from frontend_contract.rest import ProtocolError

if TYPE_CHECKING:
    from Module.workflow_engine.workflow_engine import WorkflowEngine

logger = logging.getLogger(__name__)

from paths import ProjectPath

_STATIC_DIR = ProjectPath.STATIC.resolve()
_LIVE2D_DIR = ProjectPath.LIVE2D_ASSETS.resolve()


def _make_access_auth_middleware(access_token: str):
    """产品认证中间件：护 `/ws`、`/api/*`、`/uploads/*`（Bearer 校验）。

    与 admin `_check_auth`（auth_token）权限域分离——本中间件只处理产品 access_token。
    无 token 环境不挂载（create_app 仅 access_token 非空时 append），匿名回归零影响。
    """

    @web.middleware
    async def _auth(request: web.Request, handler):
        path = request.path
        if not (path.startswith("/api/") or path.startswith("/uploads") or path == "/ws"):
            return await handler(request)
        auth_header = request.headers.get("Authorization", "")
        if auth_header == f"Bearer {access_token}":
            return await handler(request)
        code = "AUTH_REQUIRED" if not auth_header else "AUTH_INVALID"
        msg = "需要认证" if not auth_header else "认证失败"
        env = ProtocolError(
            code=code, message=msg, retryable=False,
            requestId=f"req_{uuid.uuid4().hex[:24]}", error=msg,
        ).wire_dict()
        return web.json_response(env, status=401)

    return _auth


def create_app(
    agent: "Agent",
    serve_static: bool = True,
    wake_word_enabled: bool = False,
    task_system: "WorkflowEngine | None" = None,
    auth_token: str = "",
    access_token: str = "",
) -> web.Application:
    """Create an aiohttp Application with WebSocket handler and optional static files.

    STT 不再经参数注入——handle_ws 直接读 ``agent.stt`` 槽位（含 stt_transcribe /
    stt_emotion_state），支持热切换。

    Args:
        agent: Agent instance (needs _output, _bus, process_message())
        serve_static: serve static/index.html at /
        wake_word_enabled: enable wake word detection for audio input.
        task_system: WorkflowEngine instance for debug routes (optional)
        auth_token: if non-empty, /shutdown, /restart, /tools/reload, and
            /debug/api/* require Authorization: Bearer <auth_token> header.
        access_token: if non-empty, /ws, 业务 /api/*, /uploads/* require
            Authorization: Bearer <access_token>（产品认证，与 auth_token 权限域分离）。

    Returns:
        web.Application: Configured aiohttp Application instance.
    """
    # 产品认证：access_token 为空 → 全匿名（本地开发，v1 兼容）；
    # 非空 → /ws、/api/*、/uploads/* 要求 Bearer 头（401 + ProtocolError 信封）。
    app = web.Application()
    if access_token:
        app.middlewares.append(_make_access_auth_middleware(access_token))

    # Inject agent + stt + task_system into app context for handler access
    app["agent"] = agent
    if task_system is not None:
        app["task_system"] = task_system

    def _check_auth(request: web.Request) -> bool:
        """Return True if the request is authorized for sensitive endpoints.

        Args:
            request: aiohttp Request with optional Authorization header.

        Returns:
            bool: True if authorized (no token set, or token matches).
        """
        if not auth_token:
            return True
        auth_header = request.headers.get("Authorization", "")
        expected = f"Bearer {auth_token}"
        return auth_header == expected

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        """WebSocket 连接处理入口。

        Args:
            request: aiohttp Request。

        Returns:
            web.WebSocketResponse: WebSocket 连接对象。
        """
        return await handle_ws(request, agent, wake_word_enabled)

    app.router.add_get("/ws", ws_handler)

    if serve_static and _STATIC_DIR.is_dir():
        app.router.add_static("/static/", _STATIC_DIR, show_index=False)

    if _LIVE2D_DIR.is_dir():
        app.router.add_static("/live2d/", _LIVE2D_DIR, show_index=False)

    # Serve index.html at /
    index_path = _STATIC_DIR / "index.html"
    if index_path.is_file():
        async def index_handler(_request: web.Request) -> web.FileResponse:
            """Serve the main chat application HTML page.

            Args:
                _request: aiohttp Request (unused).

            Returns:
                web.FileResponse: index.html file response.
            """
            return web.FileResponse(index_path)
        app.router.add_get("/", index_handler)

        # Debug page
        debug_path = _STATIC_DIR / "debug.html"
        if debug_path.is_file():
            async def debug_handler(_request: web.Request) -> web.FileResponse:
                """Serve the debug dashboard HTML page.

                Args:
                    _request: aiohttp Request (unused).

                Returns:
                    web.FileResponse: debug.html file response.
                """
                return web.FileResponse(debug_path)
            app.router.add_get("/debug", debug_handler)

    # Health check endpoint — for Docker/Kubernetes probes
    async def health_handler(request: web.Request) -> web.Response:
        """Health/readiness check endpoint for Docker/Kubernetes probes.

        Args:
            request: aiohttp Request。

        Returns:
            web.Response: JSON status including FSM state, LLM model, memory counts, tool list.
        """
        agent = request.app["agent"]
        try:
            fsm_state = agent.status.current_name()
        except Exception:
            fsm_state = "unknown"
        return web.json_response({
            "status": "ok",
            "fsm": fsm_state,
            "llm": getattr(agent.llm, 'model', 'unknown'),
            "memory": {
                "session_count": len(getattr(agent.session, '_messages', [])),
                "long_term_count": getattr(agent.long_term, 'count', 0),
            },
            "tools": getattr(agent.tool_adapter._manager, 'tool_names', ["<loading>"]),  # noqa: SLF001
        })
    app.router.add_get("/health", health_handler)
    app.router.add_get("/ready", health_handler)

    # Shutdown endpoint — triggers graceful agent stop
    async def shutdown_handler(request: web.Request) -> web.Response:
        """Graceful shutdown endpoint. Sets agent state to STOPPING.

        Args:
            request: aiohttp Request (requires auth if token is set).

        Returns:
            web.Response: {"status": "shutting_down"} or 401 unauthorized.
        """
        if not _check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        import logging
        _log = logging.getLogger(__name__)
        _log.info("Shutdown requested via HTTP")
        request.app["agent"].request_shutdown()
        return web.json_response({"status": "shutting_down"})
    app.router.add_post("/shutdown", shutdown_handler)
    app.router.add_get("/shutdown", shutdown_handler)  # 也接受浏览器直接访问

    # Restart endpoint — triggers graceful restart with config reload
    async def restart_handler(request: web.Request) -> web.Response:
        """Graceful restart endpoint. Sets agent state to RESTARTING.

        Args:
            request: aiohttp Request (requires auth if token is set).

        Returns:
            web.Response: {"status": "restarting"} or 401 unauthorized.
        """
        if not _check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        import logging
        _log = logging.getLogger(__name__)
        _log.info("Restart requested via HTTP")
        request.app["agent"].request_restart()
        return web.json_response({"status": "restarting"})
    app.router.add_post("/restart", restart_handler)

    # Tools endpoint — list loaded plugins
    async def tools_handler(request: web.Request) -> web.Response:
        """List loaded tool plugins.

        Args:
            request: aiohttp Request (requires auth if token is set).

        Returns:
            web.Response: {"plugins": [...]} or 401 unauthorized.
        """
        if not _check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        agent = request.app["agent"]
        return web.json_response({
            "plugins": agent.tool_adapter._manager.plugin_list,  # noqa: SLF001
        })
    app.router.add_get("/tools", tools_handler)

    # Tool reload endpoint — hot-reload a single plugin
    async def tool_reload_handler(request: web.Request) -> web.Response:
        """Hot-reload a single tool plugin.

        Args:
            request: aiohttp Request (requires auth if token is set),
                JSON body with plugin "name".

        Returns:
            web.Response: {"ok": True, "name": ...} or error.
        """
        if not _check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        agent = request.app["agent"]
        try:
            body = await request.json()
            name = body.get("name", "")
        except Exception:
            return web.json_response(
                {"ok": False, "error": "无效的请求体"}, status=400,
            )
        if not name:
            return web.json_response(
                {"ok": False, "error": "缺少插件名称"}, status=400,
            )
        ok = await agent.tool_adapter._manager.reload_plugin(name)  # noqa: SLF001
        if ok:
            return web.json_response({"ok": True, "name": name})
        else:
            return web.json_response(
                {"ok": False, "name": name, "error": f"插件 '{name}' 重载失败"},
            )
    app.router.add_post("/tools/reload", tool_reload_handler)

    # 子模块路由
    register_api_routes(app)
    register_debug_routes(app, _check_auth)

    # ---- 用户上传文件目录 ----
    _UPLOADS_DIR = ProjectPath.DATA_UPLOADS.resolve()
    _UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    app.router.add_static("/uploads/", str(_UPLOADS_DIR), show_index=False)

    return app
