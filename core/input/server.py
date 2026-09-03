"""WebSocket 服务 —— 将浏览器客户端桥接到 agent 管线。

每个客户端获得独立的 WebSocketChannel（出站）和 AudioBuffer（入站音频）。
文本与 STT 结果路由到 agent 的 EventBus。
"""

import asyncio
import hmac
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, TYPE_CHECKING

from aiohttp import web

from core.agent import Agent
from core.utils.server_guard import (
    assert_secure_bind,
    is_loopback_host,
    warn_if_unsecured,
)

from core.input.ws_handler import handle_ws, resolve_protocol_version
from core.input.provider_routes import register_provider_routes
from core.input.tts_routes import register_tts_routes
from core.input.upload_routes import register_upload_routes
from core.input.tools_routes import register_tools_routes
from core.input.chat_routes import register_chat_routes
from core.input.attachment_routes import register_attachment_routes
from core.input.session_routes import register_session_routes
from core.input.debug_routes import register_debug_routes
from core.input.errors import _protocol_error_response
from core.input.health import health_handler, ready_handler
from core.input.middleware import (
    _make_access_auth_middleware, _make_payload_middleware, _make_rate_limit_middleware,
    _nosniff_uploads,
)
from core.network.rate_limiter import RateLimiter, rate_key_for_request

if TYPE_CHECKING:
    from Module.workflow_engine.workflow_engine import WorkflowEngine
    from core.storage.idempotency_repo import IdempotencyRepository

logger = logging.getLogger(__name__)

from paths import ProjectPath

_STATIC_DIR = ProjectPath.STATIC.resolve()
_LIVE2D_DIR = ProjectPath.LIVE2D_ASSETS.resolve()


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


def _make_ws_handler(
    agent: "Agent",
    wake_word_enabled: bool,
    max_ws_connections: int | None,
) -> Callable[[web.Request], Awaitable[web.Response | web.WebSocketResponse]]:
    """构建 WS 路由 handler（含并发上限硬检查 + 协议版本协商 + 限流）。"""
    _ws_active = 0

    async def ws_handler(request: web.Request) -> web.Response | web.WebSocketResponse:
        """WebSocket 连接处理入口。"""
        nonlocal _ws_active
        limiter = request.app.get("rate_limiter")
        if limiter is not None and not limiter.check(
                "ws", rate_key_for_request(
                    request, request.app.get("rate_valid_tokens", frozenset()))):
            resp = _protocol_error_response(
                "RATE_LIMITED", "连接过于频繁，请稍后重试", 429, True)
            resp.headers["Retry-After"] = "60"
            return resp
        proto_header = request.headers.get("X-Protocol-Version", "")
        if resolve_protocol_version(proto_header) is None:
            logger.warning("不支持的协议版本拒绝: %r", proto_header)
            return _protocol_error_response(
                "CONTRACT_VALIDATION",
                "不支持的协议版本 X-Protocol-Version（支持: 缺省/\"1\"→v1, \"2\"→v2）",
                400, False)
        if max_ws_connections and max_ws_connections > 0:
            if _ws_active >= max_ws_connections:
                return _protocol_error_response("CONNECTION_LIMITED", "并发连接超限", 503,
                                                retryable=True)
            _ws_active += 1
            try:
                return await handle_ws(request, agent, wake_word_enabled)
            finally:
                _ws_active -= 1
        return await handle_ws(request, agent, wake_word_enabled)

    return ws_handler


def _register_static_routes(app: web.Application, *, serve_static: bool,
                            serve_chat: bool) -> None:
    """注册静态资源 / debug / chat 页面路由。"""
    if serve_static and _STATIC_DIR.is_dir():
        app.router.add_static("/static/", _STATIC_DIR, show_index=False)

    if _LIVE2D_DIR.is_dir():
        app.router.add_static("/live2d/", _LIVE2D_DIR, show_index=False)

    debug_path = _STATIC_DIR / "debug.html"
    if debug_path.is_file():
        async def debug_handler(_request: web.Request) -> web.FileResponse:
            """提供 Debug 仪表盘 HTML 页面。"""
            return web.FileResponse(debug_path)
        app.router.add_get("/debug", debug_handler)

    if serve_chat:
        index_path = _STATIC_DIR / "index.html"
        if index_path.is_file():
            async def index_handler(_request: web.Request) -> web.FileResponse:
                """提供主聊天应用 HTML 页面。"""
                return web.FileResponse(index_path)
            app.router.add_get("/", index_handler)


def _register_management_routes(
    app: web.Application,
    agent: "Agent",
    check_auth: Callable[[web.Request], bool],
) -> None:
    """注册 shutdown / restart / tools / tools.reload 管理端点。"""

    async def shutdown_handler(request: web.Request) -> web.Response:
        """优雅关闭端点。"""
        if not check_auth(request):
            code = "AUTH_REQUIRED" if not request.headers.get("Authorization") else "AUTH_INVALID"
            msg = "需要认证" if code == "AUTH_REQUIRED" else "认证失败"
            return _protocol_error_response(code, msg, 401)
        logger.info("Shutdown requested via HTTP")
        agent.request_shutdown()
        return web.json_response({"status": "shutting_down"})
    app.router.add_post("/shutdown", shutdown_handler)
    app.router.add_get("/shutdown", shutdown_handler)

    async def restart_handler(request: web.Request) -> web.Response:
        """优雅重启端点。"""
        if not check_auth(request):
            code = "AUTH_REQUIRED" if not request.headers.get("Authorization") else "AUTH_INVALID"
            msg = "需要认证" if code == "AUTH_REQUIRED" else "认证失败"
            return _protocol_error_response(code, msg, 401)
        logger.info("Restart requested via HTTP")
        agent.request_restart()
        return web.json_response({"status": "restarting"})
    app.router.add_post("/restart", restart_handler)

    async def tools_handler(request: web.Request) -> web.Response:
        """列出已加载的工具插件。"""
        if not check_auth(request):
            code = "AUTH_REQUIRED" if not request.headers.get("Authorization") else "AUTH_INVALID"
            msg = "需要认证" if code == "AUTH_REQUIRED" else "认证失败"
            return _protocol_error_response(code, msg, 401)
        return web.json_response({
            "plugins": agent.tool_adapter._manager.plugin_list,  # noqa: SLF001
        })
    app.router.add_get("/tools", tools_handler)

    async def tool_reload_handler(request: web.Request) -> web.Response:
        """热重载单个工具插件。"""
        if not check_auth(request):
            code = "AUTH_REQUIRED" if not request.headers.get("Authorization") else "AUTH_INVALID"
            msg = "需要认证" if code == "AUTH_REQUIRED" else "认证失败"
            return _protocol_error_response(code, msg, 401)
        try:
            body = await request.json()
            name = body.get("name", "")
        except web.HTTPRequestEntityTooLarge:
            raise
        except Exception:
            return web.json_response({"ok": False, "error": "无效的请求体"}, status=400)
        if not name:
            return web.json_response({"ok": False, "error": "缺少插件名称"}, status=400)
        ok, reason = await agent.tool_adapter._manager.reload_plugin(name)  # noqa: SLF001
        if ok:
            return web.json_response({"ok": True, "name": name})
        return web.json_response({"ok": False, "name": name, "error": reason})
    app.router.add_post("/tools/reload", tool_reload_handler)


def create_app(
    agent: "Agent",
    serve_static: bool = True,
    serve_chat: bool = False,
    wake_word_enabled: bool = False,
    task_system: "WorkflowEngine | None" = None,
    auth_token: str = "",
    access_token: str = "",
    max_ws_connections: int | None = None,
    max_request_bytes: int | None = None,
    rate_limits: dict[str, int] | None = None,
    product_mode: bool = False,
    idempotency_repo: "IdempotencyRepository | None" = None,
) -> web.Application:
    """创建一个带 WebSocket handler 和可选静态文件的 aiohttp Application。

    STT 不再经参数注入——handle_ws 直接读 ``agent.stt`` 槽位（含 stt_transcribe /
    stt_emotion_state），支持热切换。
    """
    cms = max_request_bytes if max_request_bytes is not None else 64 * 1024 * 1024
    app = web.Application(client_max_size=cms)
    app.middlewares.append(_make_payload_middleware(cms))
    app.middlewares.append(_nosniff_uploads)

    valid_tokens = frozenset({auth_token, access_token} - {""})
    limiter: RateLimiter | None = None
    if rate_limits:
        limiter = RateLimiter(rate_limits)
        app.middlewares.append(_make_rate_limit_middleware(limiter, valid_tokens))
        app["rate_limiter"] = limiter

    from core.storage.idempotency import configure_store
    configure_store(idempotency_repo, product_mode=product_mode)
    if access_token:
        app.middlewares.append(_make_access_auth_middleware(access_token))

    sig_secret = getattr(
        getattr(getattr(agent, "config", None), "server", None),
        "attachment_sig_secret", "") or ""
    warn_if_attachment_signature_disabled(sig_secret, access_token)

    app["agent"] = agent
    app["rate_valid_tokens"] = valid_tokens
    app["product_mode"] = product_mode
    app["idempotency_repo"] = idempotency_repo
    if task_system is not None:
        app["task_system"] = task_system

    def _check_auth(request: web.Request) -> bool:
        """管理端点认证（/shutdown /restart /tools /debug/api/*）。"""
        if auth_token:
            expected = f"Bearer {auth_token}"
        elif access_token:
            expected = f"Bearer {access_token}"
        else:
            return True
        return hmac.compare_digest(
            request.headers.get("Authorization", ""), expected)

    app.router.add_get("/ws", _make_ws_handler(
        agent, wake_word_enabled, max_ws_connections))
    _register_static_routes(app, serve_static=serve_static, serve_chat=serve_chat)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/ready", ready_handler)
    _register_management_routes(app, agent, _check_auth)

    from core.input.sticker_registry import load_stickers
    load_stickers()

    register_provider_routes(app)
    register_tts_routes(app)
    register_upload_routes(app)
    register_tools_routes(app)
    register_chat_routes(app)
    register_attachment_routes(app)
    register_session_routes(app)

    # ---- 阶段 D Custom Character 装配（插在 register_session_routes 之后、register_debug_routes 之前）----
    # 注意：create_app 是同步函数（def，非 async def）——不能 await。repo 连接是惰性的：
    # connect() 由首次 CRUD 操作的 fail-closed 路径触发（未连接即抛 StorageUnavailableError → 503）。
    from core.character.service import CharacterService
    from core.profile.profile_store import UserIdentityStore
    from core.storage.character_repo import CharacterRepository
    from core.storage.character_revision_repo import CharacterRevisionRepository
    from core.character.catalog import CharacterCatalog
    from core.character.builtin_source import BuiltinCharacterSource
    from core.input.character_routes import register_character_routes

    # 复用 agent 已建的用户身份存储（agent._user_identity_store）——
    # 不能用新的 UserIdentityStore()（会让 /api/me 更新与 cognitive loop 渲染用的 store 脱节，
    # 用户改别名/代词后实际聊天不生效）。server 装配层允许 # noqa: SLF001 访私有（同 loop.configure 的用法）。
    app["user_identity_store"] = getattr(agent, "_user_identity_store", None) or UserIdentityStore()
    if app["user_identity_store"].get() is None:
        agent_config = getattr(agent, "config", None)
        app["user_identity"] = app["user_identity_store"].bootstrap(
            (agent_config.agent.user_name if agent_config and getattr(agent_config, "agent", None)
             else "用户"))
    # CharacterService：repo 无 db 参数时 connect 建真实 Mongo（首次操作 fail-closed）；
    # 与 idempotency_repo 一样在 create_app 注入。
    app["character_service"] = CharacterService(
        repo=CharacterRepository(), rev_repo=CharacterRevisionRepository(),
        catalog=CharacterCatalog(source=BuiltinCharacterSource()),
        user_identity_store=getattr(agent, "_user_identity_store", None) or UserIdentityStore(),
    )
    register_character_routes(app, _check_auth)

    register_debug_routes(app, _check_auth)

    _IMAGES_DIR = ProjectPath.DATA_UPLOADS_IMAGES.resolve()
    _IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    app.router.add_static("/uploads/images/", str(_IMAGES_DIR), show_index=False)
    _DESKTOP_DIR = ProjectPath.DATA_UPLOADS_DESKTOP.resolve()
    _DESKTOP_DIR.mkdir(parents=True, exist_ok=True)
    app.router.add_static("/uploads/desktop/", str(_DESKTOP_DIR), show_index=False)

    return app
