"""server 中间件工厂（2026-08-14 批次 D 从 server.py 拆出）。

_make_access_auth_middleware / _make_payload_middleware / _rest_rate_category /
_make_rate_limit_middleware + _nosniff_uploads（原 create_app 内闭包）。函数体逐字不动。
"""

import logging
import re
from typing import Awaitable, Callable

from aiohttp import web

from core.input.errors import _protocol_error_response
from core.network.rate_limiter import RateLimiter, rate_key_for_request

logger = logging.getLogger(__name__)


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
# S2-F1：`/api/sessions/{id}/resources` 是**扫描型**入口（按需遍历归档消息提取资源），
# 原先不在这张表里 → 无限流兜底。它比 search 更贵（search 有索引查询，这条是全档案扫描），
# 故归入 search 类别共用配额。
_REST_RATE_REGEX: tuple[tuple[str, str], ...] = (
    (r"^/api/sessions/[^/]+/resources$", "search"),
)


def _rest_rate_category(path: str) -> str | None:
    """REST 路径 → 限流类别；未映射路径不限制。"""
    for prefix, category in _REST_RATE_CATEGORY:
        if path.startswith(prefix):
            return category
    for pattern, category in _REST_RATE_REGEX:
        if re.match(pattern, path):
            return category
    return None


def _make_rate_limit_middleware(limiter: RateLimiter, valid_tokens: frozenset[str]):
    """公开 REST 入口限流中间件（D6b）：超限 → 429 RATE_LIMITED + Retry-After。

    先于 auth 挂载——认证失败/匿名 401 洪水同样被限（防凭据爆破）。/ws 不走此
    中间件（WS 建连限流在 ws_handler 升级前单独检查）。

    Args:
        limiter: 单进程令牌桶限流器。
        valid_tokens: 合法 token 集合（BE-P2-02）——只有命中集合的 Bearer token
            才按 auth 主体分桶，缺失/无效统一 ip 桶（防伪造 Authorization 轮换
            获得 per-token 配额）。
    """

    @web.middleware
    async def _rate(request: web.Request,
                    handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
        if request.path != "/ws":
            category = _rest_rate_category(request.path)
            if category is not None and not limiter.check(
                    category, rate_key_for_request(request, valid_tokens)):
                resp = _protocol_error_response(
                    "RATE_LIMITED", "请求过于频繁，请稍后重试", 429, True)
                resp.headers["Retry-After"] = "60"
                return resp
        return await handler(request)

    return _rate


@web.middleware
async def _nosniff_uploads(request: web.Request,
                           handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
    resp = await handler(request)
    if request.path.startswith("/uploads/"):
        resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp
