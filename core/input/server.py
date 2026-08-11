"""WebSocket 服务 —— 将浏览器客户端桥接到 agent 管线。

每个客户端获得独立的 WebSocketChannel（出站）和 AudioBuffer（入站音频）。
文本与 STT 结果路由到 agent 的 EventBus。
"""

import asyncio
import hmac
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, TYPE_CHECKING

from aiohttp import web

from core.agent import Agent

from core.input.ws_handler import handle_ws, resolve_protocol_version
from core.input.api_routes import register_api_routes
from core.input.attachment_routes import register_attachment_routes
from core.input.session_routes import register_session_routes
from core.input.debug_routes import register_debug_routes
from core.input.rate_limiter import RateLimiter, rate_key_for_request
from core.version import build_server_version, get_build_sha
from frontend_contract.rest import ProtocolError

if TYPE_CHECKING:
    from Module.workflow_engine.workflow_engine import WorkflowEngine
    from core.storage.idempotency_repo import IdempotencyRepository

logger = logging.getLogger(__name__)

from paths import ProjectPath

# 依赖健康检查超时（秒）——挂起/慢依赖视为 down，探活请求不无限阻塞
_HEALTH_TIMEOUT = 2.0
# 进程启动时刻（monotonic），/health uptime 用
_PROCESS_START = time.monotonic()

_STATIC_DIR = ProjectPath.STATIC.resolve()
_LIVE2D_DIR = ProjectPath.LIVE2D_ASSETS.resolve()


def is_loopback_host(host: str) -> bool:
    """host 是否为回环地址（127.0.0.1 / localhost / ::1）。

    产品/dev 模式判定依据：回环 → 本地 dev（幂等可内存降级）；非回环 → 产品
    （幂等 Mongo 离线 fail-closed）。与 D6a bind 守卫同一判定。
    """
    return host in ("127.0.0.1", "localhost", "::1")


def assert_secure_bind(host: str, access_token: str, auth_token: str = "") -> None:
    """D6a：非回环绑定 + 无产品认证 token + 未显式 ALLOW_INSECURE_REMOTE → 拒绝启动。

    fail-closed：默认配置无法匿名监听非回环地址。仅以下之一放行：
    - host 是 loopback（127.0.0.1 / localhost / ::1）；
    - 已配置 access_token（AGENT_ACCESS_TOKEN）——业务 /api、/ws、/uploads 的
      唯一鉴权；SERVER_AUTH_TOKEN（管理面）**不**单独放行远程绑定（review 复核
      #1：仅配管理 token 时业务端点仍匿名可访问）；
    - 显式设 ALLOW_INSECURE_REMOTE=1（仅本地开发开关，日志/health 明确 insecure）。

    Raises:
        RuntimeError: fail-closed 条件成立。
    """
    if os.getenv("ALLOW_INSECURE_REMOTE", "") in ("1", "true", "True", "yes"):
        return
    if is_loopback_host(host):
        return
    if access_token:
        return
    raise RuntimeError(
        f"拒绝启动：服务绑定非回环地址 {host!r} 且未配置产品认证 token。\n"
        "  - 远程部署：设置 AGENT_ACCESS_TOKEN（护 /ws /api/* /uploads/*，唯一放行凭据），"
        "建议同时配 SERVER_AUTH_TOKEN（管理面），使用 HTTPS/WSS；\n"
        "  - 本地开发：改用 127.0.0.1 绑定，或显式设置 ALLOW_INSECURE_REMOTE=1 并知悉风险。"
    )


def warn_if_unsecured(host: str, access_token: str, auth_token: str = "") -> None:
    """非回环绑定 + 未配置认证 token → 显著告警；仅配置一侧 → 提示同时配置。

    满足 PRD「绑定非回环地址且未启用认证时应拒绝启动或输出显著阻塞告警」的
    "或"分支——不拒绝启动（config 默认 host=0.0.0.0，拒绝会破坏本地开发）。

    Args:
        host: 服务绑定地址。
        access_token: 产品 access token（AGENT_ACCESS_TOKEN），空 = 未启用认证。
        auth_token: 管理面 token（SERVER_AUTH_TOKEN），空 = 未启用管理认证。
    """
    if is_loopback_host(host):
        return
    if auth_token and access_token:
        return
    if access_token or auth_token:
        logger.warning(
            "\n"
            "==================================================================\n"
            "  安全建议：服务绑定非回环地址 %r 且仅配置了部分认证。\n"
            "  远程部署建议同时配置 AGENT_ACCESS_TOKEN（产品 API /ws /uploads）与\n"
            "  SERVER_AUTH_TOKEN（管理面 /shutdown /debug/api），二者缺一会导致\n"
            "  部分端点匿名可访问。\n"
            "==================================================================",
            host,
        )
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
    async def _auth(request: web.Request,
                    handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
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
        logger.warning("认证失败 code=%s path=%s", code, request.path)
        return _protocol_error_response(code, msg, 401)

    return _auth


def _make_payload_middleware(cms: int):
    """请求体超限（client_max_size）→ 413 REQUEST_TOO_LARGE 统一信封。

    aiohttp 默认 413 是纯文本；且 handler 的 `except Exception` 会吞掉
    HTTPRequestEntityTooLarge（变 400）。故在进 handler 前按 Content-Length 提前 413；
    chunked（无 Content-Length）场景保留异常兜底。
    """

    @web.middleware
    async def _payload(request: web.Request,
                       handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
        # cms=0 时不预检（不限语义；aiohttp client_max_size=0 原生不限，亦无异常可兜）
        if cms and request.content_length is not None and request.content_length > cms:
            return _protocol_error_response("REQUEST_TOO_LARGE", "请求体过大", 413)
        try:
            return await handler(request)
        except web.HTTPRequestEntityTooLarge:
            # aiohttp 3.12 将 413 异常类更名：HTTPPayloadTooLarge 已不存在，
            # 须用 HTTPRequestEntityTooLarge（chunked 无 Content-Length 场景兜底）。
            return _protocol_error_response("REQUEST_TOO_LARGE", "请求体过大", 413)

    return _payload


# D6b：REST 高成本入口 → 限流类别映射（path 前缀，先长后短避免 /api/uploads 吞 /api/uploads/desktop）。
# 覆盖 plan Task D6b 的 provider test / upload / search / chat（generation）。
_REST_RATE_CATEGORY: tuple[tuple[str, str], ...] = (
    ("/api/providers/test", "provider_test"),
    ("/api/uploads/desktop/", "upload"),
    ("/api/uploads", "upload"),
    ("/api/images", "upload"),
    ("/api/search/", "search"),
    ("/api/chat", "messages"),
)


def _rest_rate_category(path: str) -> str | None:
    """REST 路径 → 限流类别；未映射路径不限制。"""
    for prefix, category in _REST_RATE_CATEGORY:
        if path.startswith(prefix):
            return category
    return None


def _make_rate_limit_middleware(limiter: RateLimiter):
    """公开 REST 入口限流中间件（D6b）：超限 → 429 RATE_LIMITED + Retry-After。

    先于 auth 挂载——认证失败/匿名 401 洪水同样被限（防凭据爆破）。/ws 不走此
    中间件（WS 建连限流在 ws_handler 升级前单独检查）。
    """

    @web.middleware
    async def _rate(request: web.Request,
                    handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
        if request.path != "/ws":
            category = _rest_rate_category(request.path)
            if category is not None and not limiter.check(
                    category, rate_key_for_request(request)):
                resp = _protocol_error_response(
                    "RATE_LIMITED", "请求过于频繁，请稍后重试", 429, True)
                resp.headers["Retry-After"] = "60"
                return resp
        return await handler(request)

    return _rate


async def _collect_health_payload(agent: "Agent") -> dict:
    """聚合健康探针负载：依赖检查（wait_for 超时/异常视为 down）+ 运行信息。

    Args:
        agent: Agent 实例（公开接口 session.health_check / long_term.health_check）。

    Returns:
        探针 JSON 负载：status/fsm/llm/memory/tools/version/build_sha/uptime/
        request_id/dependencies{`mongo`, `milvus`}。
    """
    async def _probe(factory: Callable[[], Any]) -> bool:
        try:
            return bool(await asyncio.wait_for(factory(), timeout=_HEALTH_TIMEOUT))
        except Exception:
            # 超时 / 依赖抛异常 / mock 不可 await → 一律视为 down
            return False

    # P1-05：/ready 必须真实反映 mandatory 依赖。mongo = session + message + attachment
    # 三个 repo 全部可达（各自真实 ping）；milvus = long_term。桌面/版本/生成降级不门禁。
    mongo_ok = bool(
        await _probe(agent.session.health_check)
        and await _probe(agent.message_repo.health_check)
        and await _probe(agent.attachment_repo.health_check)
    )
    milvus_ok = await _probe(agent.long_term.health_check)
    dependencies = {"mongo": mongo_ok, "milvus": milvus_ok}
    status = "ok" if all(dependencies.values()) else "degraded"

    try:
        fsm_state = agent.status.current_name()
    except Exception:
        fsm_state = "unknown"

    # 探活不应扰动 request_id 计数器（批次 8a Task 15）：request_id 独立 uuid 生成，
    # 不触碰 agent.next_request_id（该计数器由真实业务请求/生成链路驱动）。
    request_id = f"req_{uuid.uuid4().hex[:24]}"

    return {
        "status": status,
        "fsm": fsm_state,
        "llm": getattr(agent.llm, "model", "unknown"),
        "memory": {
            # 走公开接口 session.messages，不摸私有 _messages（改名时 getattr 会静默降级）
            "session_count": len(agent.session.messages),
            "long_term_count": agent.long_term.count,
        },
        # 双跳守卫：startup 早期 _manager 可能未就绪，getattr 兜底 "<loading>"
        "tools": getattr(getattr(agent.tool_adapter, "_manager", None),  # noqa: SLF001
                          "tool_names", ["<loading>"]),
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

    Args:
        agent: Agent 实例（需具备 _output、_bus、process_message()）。
        serve_static: 是否在 /static/ 挂载静态目录。
        serve_chat: 是否注册 / 聊天路由（默认关——v1 网页聊天停用，/ 404；
            SERVE_CHAT=1 可恢复）。/debug 独立于本开关（拆耦合，仅受 debug.html
            存在性约束）。
        wake_word_enabled: 是否对音频输入启用唤醒词检测。
        task_system: WorkflowEngine 实例，供 debug 路由使用（可选）。
        auth_token: 非空时，/shutdown、/restart、/tools/reload 与 /debug/api/*
            需要 Authorization: Bearer <auth_token> 请求头。
        access_token: 非空时，/ws、业务 /api/*、/uploads/* 需要
            Authorization: Bearer <access_token>（产品认证，与 auth_token 权限域分离）。
        max_ws_connections: 并发 WS 连接上限（0/None 不限）。超出拒绝升级（503 CONNECTION_LIMITED）。
        max_request_bytes: 业务请求体上限（client_max_size，app 级；0=不限，None/缺省 64MB；
            multipart 除外——upload_handler 同源同步）。
        rate_limits: D6b 限流规则 {类别: rpm}（0 关闭）。None/空 → 不挂载限流。
            REST 高成本入口（provider test/upload/search/chat）429 RATE_LIMITED；
            WS 建连按「ws」类别、消息按类别（text/voice→messages、switch_model→provider_test、
            tool_approval→tools）→ error RATE_LIMITED。key 认证主体优先、匿名回退 IP。
        product_mode: D6c 产品/dev 模式——非回环绑定为 True。True + 幂等 Mongo
            离线 → v2 幂等 fail-closed（503）；False + 离线 → 内存降级（capabilities
            暴露非 durable 窗口）。
        idempotency_repo: D6c 幂等 claim 仓库（agent.idempotency_repo），None → 纯内存。

    Returns:
        web.Application: 配置好的 aiohttp Application 实例。
    """
    # 请求体上限：app 级 client_max_size（aiohttp 无按路由选项；multipart 手工限制不受其约束）。
    # is not None 语义：0 → 0（aiohttp 原生不限），None/缺省 → 64MB（falsy 回落会误吞 0）。
    cms = max_request_bytes if max_request_bytes is not None else 64 * 1024 * 1024
    app = web.Application(client_max_size=cms)
    # 请求体超限 → 413 REQUEST_TOO_LARGE 信封（恒挂载）
    app.middlewares.append(_make_payload_middleware(cms))
    # D5/P2-03：legacy /uploads/ 静态响应加 nosniff（防浏览器按实际扩展嗅探执行活动内容）
    @web.middleware
    async def _nosniff_uploads(request: web.Request,
                               handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
        resp = await handler(request)
        if request.path.startswith("/uploads/"):
            resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp
    app.middlewares.append(_nosniff_uploads)
    # D6b：公开入口限流中间件（先于 auth——认证失败/匿名 401 洪水同样被限）。
    # 单进程语义（per-process），不假装全局限流。
    limiter: RateLimiter | None = None
    if rate_limits:
        limiter = RateLimiter(rate_limits)
        app.middlewares.append(_make_rate_limit_middleware(limiter))
        app["rate_limiter"] = limiter
    # D6c：幂等存储后端配置（product_mode 由绑定 host 判定——非回环=产品）。
    # configure_store 幂等（多次 create_app/restart 安全）；测试经 reset_store 隔离。
    from core.input.idempotency import configure_store
    configure_store(idempotency_repo, product_mode=product_mode)
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

    # 将 agent + stt + task_system 注入 app context，供 handler 访问
    app["agent"] = agent
    if task_system is not None:
        app["task_system"] = task_system

    def _check_auth(request: web.Request) -> bool:
        """管理端点认证（/shutdown /restart /tools /debug/api/*）。

        批次 8a（D1）fallback：auth_token 空 + access_token 非空 → 用 access_token 校验
        （Demo 单 token 语义，docs/14 §6）；两者都空 → 匿名本地开发。

        Args:
            request: aiohttp Request with optional Authorization header.

        Returns:
            bool: True if authorized (no token set, or token matches).
        """
        if auth_token:
            expected = f"Bearer {auth_token}"
        elif access_token:
            expected = f"Bearer {access_token}"
        else:
            return True
        return hmac.compare_digest(
            request.headers.get("Authorization", ""), expected)

    # 并发 WS 硬上限（批次 8c F14）：闭包计数 check+自增在同一事件循环调度内原子
    # （无 await 分隔），杜绝 TOCTOU 预检（旧实现对 output._channels 计数，并发握手
    # 或陈旧 channel 会误判）；连接关闭（handle_ws 返回）finally 自减。
    _ws_active = 0

    async def ws_handler(request: web.Request) -> web.Response | web.WebSocketResponse:
        """WebSocket 连接处理入口（含并发上限硬检查 + 协议版本协商）。

        9B-3：升级前协议版本协商——`X-Protocol-Version` 显式不支持的版本
        （如 "2.0" 展示版本 / "3" / 乱拼写）→ HTTP 400 ProtocolError
        （`CONTRACT_VALIDATION`，message 明示支持集合），不静默降级 v1；
        拒绝优先于 platform 解析（未知版本不解析 X-Client-Platform）。
        `handle_ws` 内 `resolve_protocol_version` 返回 None 理论上不可达（防御处理）。

        Args:
            request: aiohttp Request。

        Returns:
            web.Response | web.WebSocketResponse: 版本拒绝/并发超限错误响应或
            WebSocket 连接对象。
        """
        nonlocal _ws_active
        # D6b：WS 建连限流——per-key（认证主体优先、匿名回退 IP），升级前 429 拒绝
        # （防重连风暴/滥用；配合 max_ws_connections 并发硬上限，二者正交）。
        limiter = request.app.get("rate_limiter")
        if limiter is not None and not limiter.check("ws", rate_key_for_request(request)):
            return _protocol_error_response(
                "RATE_LIMITED", "连接过于频繁，请稍后重试", 429, True)
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

    app.router.add_get("/ws", ws_handler)

    if serve_static and _STATIC_DIR.is_dir():
        app.router.add_static("/static/", _STATIC_DIR, show_index=False)

    if _LIVE2D_DIR.is_dir():
        app.router.add_static("/live2d/", _LIVE2D_DIR, show_index=False)

    # Debug 页面——独立于聊天，index.html 存在与否不影响（拆耦合）
    debug_path = _STATIC_DIR / "debug.html"
    if debug_path.is_file():
        async def debug_handler(_request: web.Request) -> web.FileResponse:
            """提供 Debug 仪表盘 HTML 页面。

            Args:
                _request: aiohttp Request（未使用）。

            Returns:
                web.FileResponse: debug.html 文件响应。
            """
            return web.FileResponse(debug_path)
        app.router.add_get("/debug", debug_handler)

    # 聊天页面——serve_chat 关闭则不注册（/ 404）
    if serve_chat:
        index_path = _STATIC_DIR / "index.html"
        if index_path.is_file():
            async def index_handler(_request: web.Request) -> web.FileResponse:
                """提供主聊天应用 HTML 页面。

                Args:
                    _request: aiohttp Request（未使用）。

                Returns:
                    web.FileResponse: index.html 文件响应。
                """
                return web.FileResponse(index_path)
            app.router.add_get("/", index_handler)

    # 健康检查端点 —— Docker/Kubernetes liveness(/health) + readiness(/ready)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/ready", ready_handler)

    # 关闭端点 —— 触发优雅的 agent 停止
    async def shutdown_handler(request: web.Request) -> web.Response:
        """优雅关闭端点。将 agent 状态置为 STOPPING。

        Args:
            request: aiohttp Request（设置 token 时需要认证）。

        Returns:
            web.Response: {"status": "shutting_down"} 或 401 unauthorized。
        """
        if not _check_auth(request):
            code = "AUTH_REQUIRED" if not request.headers.get("Authorization") else "AUTH_INVALID"
            msg = "需要认证" if code == "AUTH_REQUIRED" else "认证失败"
            return _protocol_error_response(code, msg, 401)
        import logging
        _log = logging.getLogger(__name__)
        _log.info("Shutdown requested via HTTP")
        request.app["agent"].request_shutdown()
        return web.json_response({"status": "shutting_down"})
    app.router.add_post("/shutdown", shutdown_handler)
    app.router.add_get("/shutdown", shutdown_handler)  # 也接受浏览器直接访问

    # 重启端点 —— 触发带配置重新加载的优雅重启
    async def restart_handler(request: web.Request) -> web.Response:
        """优雅重启端点。将 agent 状态置为 RESTARTING。

        Args:
            request: aiohttp Request（设置 token 时需要认证）。

        Returns:
            web.Response: {"status": "restarting"} 或 401 unauthorized。
        """
        if not _check_auth(request):
            code = "AUTH_REQUIRED" if not request.headers.get("Authorization") else "AUTH_INVALID"
            msg = "需要认证" if code == "AUTH_REQUIRED" else "认证失败"
            return _protocol_error_response(code, msg, 401)
        import logging
        _log = logging.getLogger(__name__)
        _log.info("Restart requested via HTTP")
        request.app["agent"].request_restart()
        return web.json_response({"status": "restarting"})
    app.router.add_post("/restart", restart_handler)

    # Tools 端点 —— 列出已加载的插件
    async def tools_handler(request: web.Request) -> web.Response:
        """列出已加载的工具插件。

        Args:
            request: aiohttp Request（设置 token 时需要认证）。

        Returns:
            web.Response: {"plugins": [...]} 或 401 unauthorized。
        """
        if not _check_auth(request):
            code = "AUTH_REQUIRED" if not request.headers.get("Authorization") else "AUTH_INVALID"
            msg = "需要认证" if code == "AUTH_REQUIRED" else "认证失败"
            return _protocol_error_response(code, msg, 401)
        agent = request.app["agent"]
        return web.json_response({
            "plugins": agent.tool_adapter._manager.plugin_list,  # noqa: SLF001
        })
    app.router.add_get("/tools", tools_handler)

    # Tool 重载端点 —— 热重载单个插件
    async def tool_reload_handler(request: web.Request) -> web.Response:
        """热重载单个工具插件。

        Args:
            request: aiohttp Request（设置 token 时需要认证），
                JSON 请求体需含插件 "name"。

        Returns:
            web.Response: {"ok": True, "name": ...} 或错误。
        """
        if not _check_auth(request):
            code = "AUTH_REQUIRED" if not request.headers.get("Authorization") else "AUTH_INVALID"
            msg = "需要认证" if code == "AUTH_REQUIRED" else "认证失败"
            return _protocol_error_response(code, msg, 401)
        agent = request.app["agent"]
        try:
            body = await request.json()
            name = body.get("name", "")
        except web.HTTPRequestEntityTooLarge:
            raise  # chunked 超限放行——交中间件统一 413 信封（F13）
        except Exception:
            return web.json_response(
                {"ok": False, "error": "无效的请求体"}, status=400,
            )
        if not name:
            return web.json_response(
                {"ok": False, "error": "缺少插件名称"}, status=400,
            )
        ok, reason = await agent.tool_adapter._manager.reload_plugin(name)  # noqa: SLF001
        if ok:
            return web.json_response({"ok": True, "name": name})
        else:
            return web.json_response(
                {"ok": False, "name": name, "error": reason},
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
