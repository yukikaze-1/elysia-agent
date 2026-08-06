"""WebSocket server — bridges browser clients to the agent pipeline.

Each client gets an independent WebSocketChannel (outbound) and AudioBuffer
(inbound audio). Text and STT results are routed to the agent's EventBus.
"""

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from core.agent import Agent

from core.input.ws_handler import handle_ws
from core.input.api_routes import register_api_routes
from core.input.attachment_routes import register_attachment_routes
from core.input.session_routes import register_session_routes
from core.input.debug_routes import register_debug_routes
from core.version import build_server_version, get_build_sha
from frontend_contract.rest import ProtocolError

if TYPE_CHECKING:
    from Module.workflow_engine.workflow_engine import WorkflowEngine

logger = logging.getLogger(__name__)

from paths import ProjectPath

# 依赖健康检查超时（秒）——挂起/慢依赖视为 down，探活请求不无限阻塞
_HEALTH_TIMEOUT = 2.0
# 进程启动时刻（monotonic），/health uptime 用
_PROCESS_START = time.monotonic()

_STATIC_DIR = ProjectPath.STATIC.resolve()
_LIVE2D_DIR = ProjectPath.LIVE2D_ASSETS.resolve()


def warn_if_unsecured(host: str, access_token: str) -> None:
    """非回环绑定 + 无产品 access_token → ERROR 级显著阻塞告警。

    满足 PRD「绑定非回环地址且未启用认证时应拒绝启动或输出显著阻塞告警」的
    "或"分支——不拒绝启动（config 默认 host=0.0.0.0，拒绝会破坏本地开发）。

    Args:
        host: 服务绑定地址。
        access_token: 产品 access token（AGENT_ACCESS_TOKEN），空 = 未启用认证。
    """
    if access_token:
        return
    if host in ("127.0.0.1", "localhost", "::1"):
        return
    logger.error(
        "\n"
        "==================================================================\n"
        "  安全警告：服务绑定非回环地址 %r 且未配置产品认证。\n"
        "  当前所有 /ws、/api/*、/uploads/* 匿名可访问，仅适用于本机开发。\n"
        "  远程部署请设置环境变量 AGENT_ACCESS_TOKEN，并使用 HTTPS/WSS。\n"
        "==================================================================",
        host,
    )


def warn_if_attachment_signature_disabled(sig_secret: str, access_token: str) -> None:
    """附件签名未启用 → 启动 warning（spec §3.4 fail-open 提示）。

    提示条件：ATTACHMENT_SIG_SECRET 空 + AGENT_ACCESS_TOKEN 空（无鉴权本地开发）。
    签名已配置 → 无需提示；已启用产品认证（fail-closed 语义下无签名下载会被拒）→
    同样无需提示。

    Args:
        sig_secret: 附件签名密钥（ATTACHMENT_SIG_SECRET），空 = 未配置。
        access_token: 产品 access token（AGENT_ACCESS_TOKEN），空 = 未启用认证。
    """
    if sig_secret or access_token:
        return
    logger.warning(
        "\n"
        "==================================================================\n"
        "  安全警告：附件签名未启用（ATTACHMENT_SIG_SECRET 未配置）。\n"
        "  附件下载 URL 无签名保护，仅限无鉴权本地开发。\n"
        "  远程部署请设置环境变量 ATTACHMENT_SIG_SECRET（配合 AGENT_ACCESS_TOKEN）。\n"
        "==================================================================",
    )


def _protocol_error_response(code: str, message: str, status: int = 401,
                             retryable: bool = False) -> web.Response:
    """构造统一错误信封响应（code/message/retryable/requestId/error/details）。

    Args:
        code: 稳定错误码。
        message: 公开说明。
        status: HTTP 状态码。
        retryable: 是否可重试（错误码目录定义；CONNECTION_LIMITED=true，AUTH/REQUEST 等 false）。
    """
    env = ProtocolError(
        code=code, message=message, retryable=retryable,
        requestId=f"req_{uuid.uuid4().hex[:24]}", error=message,
    ).wire_dict()
    return web.json_response(env, status=status)


def _make_access_auth_middleware(access_token: str):
    """产品认证中间件：护 `/ws`、`/api/*`、`/uploads/*`（Bearer 校验）。

    与 admin `_check_auth`（auth_token）权限域分离——本中间件只处理产品 access_token。
    无 token 环境不挂载（create_app 仅 access_token 非空时 append），匿名回归零影响。
    """

    @web.middleware
    async def _auth(request: web.Request, handler):
        path = request.path
        # 附件下载免 Bearer（签名即授权，spec §3.3）；/sign 签发端点保持 Bearer。
        # 须先于 /api/ 判断——否则下载请求会被 Bearer 校验拦下。
        if path.startswith("/api/attachments/") and not path.endswith("/sign"):
            return await handler(request)
        if not (path.startswith("/api/") or path.startswith("/uploads") or path == "/ws"):
            return await handler(request)
        auth_header = request.headers.get("Authorization", "")
        if auth_header == f"Bearer {access_token}":
            return await handler(request)
        code = "AUTH_REQUIRED" if not auth_header else "AUTH_INVALID"
        msg = "需要认证" if not auth_header else "认证失败"
        return _protocol_error_response(code, msg, 401)

    return _auth


def _make_payload_middleware(cms: int):
    """请求体超限（client_max_size）→ 413 REQUEST_TOO_LARGE 统一信封。

    aiohttp 默认 413 是纯文本；且 handler 的 `except Exception` 会吞掉
    HTTPRequestEntityTooLarge（变 400）。故在进 handler 前按 Content-Length 提前 413；
    chunked（无 Content-Length）场景保留异常兜底。
    """

    @web.middleware
    async def _payload(request: web.Request, handler):
        if request.content_length is not None and request.content_length > cms:
            return _protocol_error_response("REQUEST_TOO_LARGE", "请求体过大", 413)
        try:
            return await handler(request)
        except web.HTTPRequestEntityTooLarge:
            # aiohttp 3.12 将 413 异常类更名：HTTPPayloadTooLarge 已不存在，
            # 须用 HTTPRequestEntityTooLarge（chunked 无 Content-Length 场景兜底）。
            return _protocol_error_response("REQUEST_TOO_LARGE", "请求体过大", 413)

    return _payload


async def _collect_health_payload(agent: "Agent") -> dict:
    """聚合健康探针负载：依赖检查（wait_for 超时/异常视为 down）+ 运行信息。

    Args:
        agent: Agent 实例（公开接口 session.health_check / long_term.health_check）。

    Returns:
        探针 JSON 负载：status/fsm/llm/memory/tools/version/build_sha/uptime/
        request_id/dependencies{`mongo`, `milvus`}。
    """
    async def _probe(factory) -> bool:
        try:
            return bool(await asyncio.wait_for(factory(), timeout=_HEALTH_TIMEOUT))
        except Exception:
            # 超时 / 依赖抛异常 / mock 不可 await → 一律视为 down
            return False

    mongo_ok = await _probe(agent.session.health_check)
    milvus_ok = await _probe(agent.long_term.health_check)
    dependencies = {"mongo": mongo_ok, "milvus": milvus_ok}
    status = "ok" if all(dependencies.values()) else "degraded"

    try:
        fsm_state = agent.status.current_name()
    except Exception:
        fsm_state = "unknown"

    try:
        rid = agent.next_request_id()
        request_id = rid if isinstance(rid, str) else f"req_{uuid.uuid4().hex[:24]}"
    except Exception:
        request_id = f"req_{uuid.uuid4().hex[:24]}"

    return {
        "status": status,
        "fsm": fsm_state,
        "llm": getattr(agent.llm, "model", "unknown"),
        "memory": {
            "session_count": len(getattr(agent.session, "_messages", [])),
            "long_term_count": getattr(agent.long_term, "count", 0),
        },
        "tools": getattr(agent.tool_adapter._manager, "tool_names", ["<loading>"]),  # noqa: SLF001
        "version": build_server_version(),
        "build_sha": get_build_sha(),
        "uptime": round(time.monotonic() - _PROCESS_START, 1),
        "request_id": request_id,
        "dependencies": dependencies,
    }


async def health_handler(request: web.Request) -> web.Response:
    """Liveness 探针——进程存活探测，恒 200；status 反映依赖健康度（ok/degraded）。

    Args:
        request: aiohttp Request。

    Returns:
        web.Response: 200 JSON 探针负载（依赖异常不阻断存活上报）。
    """
    agent = request.app["agent"]
    return web.json_response(await _collect_health_payload(agent))


async def ready_handler(request: web.Request) -> web.Response:
    """Readiness 探针——依赖不 ok 时 503（流量路由/滚动发布用）。

    Args:
        request: aiohttp Request。

    Returns:
        web.Response: 依赖全 ok → 200，任一 down → 503（负载 status 同形状）。
    """
    agent = request.app["agent"]
    payload = await _collect_health_payload(agent)
    status_code = 200 if payload["status"] == "ok" else 503
    return web.json_response(payload, status=status_code)


def create_app(
    agent: "Agent",
    serve_static: bool = True,
    wake_word_enabled: bool = False,
    task_system: "WorkflowEngine | None" = None,
    auth_token: str = "",
    access_token: str = "",
    max_ws_connections: int | None = None,
    max_request_bytes: int | None = None,
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
        max_ws_connections: 并发 WS 连接上限（0/None 不限）。超出拒绝升级（503 CONNECTION_LIMITED）。
        max_request_bytes: 业务请求体上限（client_max_size，app 级；multipart 除外）。

    Returns:
        web.Application: Configured aiohttp Application instance.
    """
    # 请求体上限：app 级 client_max_size（aiohttp 无按路由选项；multipart 手工限制不受其约束）
    cms = max_request_bytes if max_request_bytes else 64 * 1024 * 1024
    app = web.Application(client_max_size=cms)
    # 请求体超限 → 413 REQUEST_TOO_LARGE 信封（恒挂载）
    app.middlewares.append(_make_payload_middleware(cms))
    # 产品认证：access_token 为空 → 全匿名（本地开发，v1 兼容）
    if access_token:
        app.middlewares.append(_make_access_auth_middleware(access_token))

    # 附件签名 fail-open 启动警告（spec §3.4）：SIG 未配置 + 无鉴权（access_token 空）
    # → 提示仅限本地开发（有鉴权时无签名下载被 fail-closed 拒，无需提示）。
    # 逐级 getattr 兜底（测试用 _FakeAgent 无 config 属性，容错跳过）。
    sig_secret = getattr(
        getattr(getattr(agent, "config", None), "server", None),
        "attachment_sig_secret", "") or ""
    warn_if_attachment_signature_disabled(sig_secret, access_token)

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
        """WebSocket 连接处理入口（含并发上限检查）。

        Args:
            request: aiohttp Request。

        Returns:
            web.WebSocketResponse: WebSocket 连接对象。
        """
        if max_ws_connections and max_ws_connections > 0:
            from Module.output import WebSocketChannel
            channels = getattr(agent.output, "_channels", None)
            if isinstance(channels, (list, tuple)):
                count = sum(1 for ch in channels if isinstance(ch, WebSocketChannel))
                if count >= max_ws_connections:
                    return _protocol_error_response("CONNECTION_LIMITED", "并发连接超限", 503,
                                                    retryable=True)
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

    # Health check endpoints — Docker/Kubernetes liveness(/health) + readiness(/ready)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/ready", ready_handler)

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

    # 内置表情注册表：create_app 启动时扫描 static/stickers/*.png 构建（批次 7 Task 6）
    from core.input.sticker_registry import load_stickers
    load_stickers()

    # 子模块路由
    register_api_routes(app)
    register_attachment_routes(app)
    register_session_routes(app)
    register_debug_routes(app, _check_auth)

    # ---- 用户上传文件目录（批次 7 静态挂载收敛） ----
    # 只公开 legacy images/desktop 目录（既有 /api/images 与批次 4 桌面截图链路兼容）；
    # 新 kind 单数目录 data/uploads/{image,video,audio,file} 不公开——走 /api/attachments/{id} 签名下载。
    _IMAGES_DIR = ProjectPath.DATA_UPLOADS_IMAGES.resolve()
    _IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    app.router.add_static("/uploads/images/", str(_IMAGES_DIR), show_index=False)
    _DESKTOP_DIR = ProjectPath.DATA_UPLOADS_DESKTOP.resolve()
    _DESKTOP_DIR.mkdir(parents=True, exist_ok=True)
    app.router.add_static("/uploads/desktop/", str(_DESKTOP_DIR), show_index=False)

    return app
