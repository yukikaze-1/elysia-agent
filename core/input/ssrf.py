"""SSRFGuard — 自定义模型 Endpoint 的防 SSRF 核心（静态校验 + 出站守卫传输层）。

保存/切换/测试三入口调用 :func:`validate_endpoint`；custom backend 出站经
:func:`build_guarded_http_client` 注入校验传输层，重定向每跳过校验 + 跨源剥头。
"""

import asyncio
import ipaddress
import logging
import re
import socket
import urllib.parse
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# 统一拒绝消息（不泄露 host/IP，防内网拓扑泄漏）
_BLOCKED_MESSAGE = "目标地址被拒绝或无法解析"

# scheme 探测：仅当 URL 已带 "scheme://" 前缀才不补缺省
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")

# 字面量黑名单：云元数据端点与特殊主机（host 已由 urlsplit 归一化为小写）
_BLOCKED_HOSTS = frozenset(
    {
        "169.254.169.254",  # AWS/GCP/Azure 云元数据
        "metadata.google.internal",  # GCP 元数据
        "100.100.100.200",  # 阿里云元数据
        "metadata",  # 常见内网主机名
    }
)

# Header 允许名单：自定义 backend 出站仅允许这些头；跨源重定向时剥除名单外全部 Header（含 Authorization）
_ALLOWED_HEADERS = frozenset(
    {
        "Authorization",
        "Content-Type",
        "Accept",
        "User-Agent",
    }
)
# 允许名单的小写形态（httpx Headers 以全小写存储/迭代）
_ALLOWED_HEADERS_CI = frozenset(h.upper() for h in _ALLOWED_HEADERS)

# CGNAT 共享地址空间（100.64.0.0/10）——ipaddress 标准属性不判此类，须显式覆盖
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


class SSRFError(ValueError):
    """SSRF 校验失败。message 统一「目标地址被拒绝或无法解析」，不泄露 host/IP。"""

    def __init__(self, message: str = _BLOCKED_MESSAGE):
        super().__init__(message)


def _is_blocked_ip(ip: str) -> bool:
    """判断 IP 是否命中 SSRF 拦截规则（ipaddress 全地址族 + CGNAT 显式判类）。

    拦截：回环 / 链路本地 / 私网 / 保留 / 多播 / 未指定 / CGNAT；无法解析的 IP 形态一律拒绝。
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        # 非合法 IP 形态 → 拒绝（防解析产物异常形态绕过）
        return True
    # IPv4-mapped IPv6（::ffff:x.x.x.x）归一为 IPv4——否则私网/CGNAT/保留属性全失守
    # （ipaddress 对 mapped 地址 is_private 等恒 False，`in _CGNAT_NETWORK` 跨族恒 False）。
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return (
        addr.is_loopback
        or addr.is_link_local
        or getattr(addr, "is_site_local", False)  # 批次 8a：site-local IPv6 fec0::/10（仅 IPv6 有该属性）
        or addr.is_private
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
        or addr in _CGNAT_NETWORK
    )


def validate_endpoint(url: str) -> None:
    """校验 endpoint URL。非法 → 抛 :class:`SSRFError`。

    规则：
    - 空串/空白 → 直接放行（内置 provider 无 body base_url 时是空串，不校验）。
    - scheme 缺省视为 https；仅允许 http/https。
    - 字面量黑名单 + ``socket.getaddrinfo`` 解析逐 IP 判类双保险。
    - NXDOMAIN/解析失败 → 抛 :class:`SSRFError`（合法 URL 需可解析才能保存）。
    """
    if not url or not url.strip():
        return
    url = url.strip()
    # scheme 缺省视为 https（如 "example.com" → "https://example.com"）
    if not _SCHEME_RE.match(url):
        url = f"https://{url}"
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise SSRFError()
    host = parsed.hostname
    if not host:
        raise SSRFError()
    # 字面量黑名单（云元数据等特殊主机）
    if host in _BLOCKED_HOSTS:
        raise SSRFError()
    # host 本身是字面量 IP → 直接判类（不依赖 getaddrinfo 对字面量的处理）
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if _is_blocked_ip(host):
            raise SSRFError()
        return
    # DNS 解析 → 逐候选 IP 判类（防 hostname 解析到内网）
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        infos = socket.getaddrinfo(host, port)
    except socket.gaierror:
        # NXDOMAIN/解析失败 → 拒绝（消息统一含「无法解析」）
        raise SSRFError()
    except (OSError, ValueError) as exc:
        # 其他解析/端口异常 → 拒绝
        logger.warning("SSRF 校验解析失败: %s", exc)
        raise SSRFError()
    for info in infos:
        if _is_blocked_ip(info[4][0]):
            raise SSRFError()


class _SSRFTransport(httpx.AsyncHTTPTransport):
    """出站守卫传输层：连接前校验目标 host（覆盖重定向每跳）+ 跨源重定向剥敏感头。"""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        # 首次请求 origin（scheme://netloc），用于跨源判定
        self._origin = None

    async def handle_async_request(self, request: httpx.Request):
        # 每跳校验（连接前，覆盖 follow_redirects 的后续 hop）。
        # to_thread：同步 getaddrinfo 可能挂起解析，放在 worker 线程避免阻塞事件循环
        # （auth 后 DoS 缓解；保存/切换/测试的冷路径同步调用仍接受，见 review 记录）。
        await asyncio.to_thread(validate_endpoint, str(request.url))
        self._strip_cross_origin_headers(request)
        return await super().handle_async_request(request)

    def _strip_cross_origin_headers(self, request: httpx.Request):
        """跨源重定向：剥除允许名单外全部 Header（含 Authorization），防敏感头泄漏到重定向目标。"""
        origin = f"{request.url.scheme}://{request.url.netloc.decode('ascii')}"
        if self._origin is None:
            self._origin = origin
            return
        if origin == self._origin:
            return
        for name in list(request.headers):
            if name.upper() not in _ALLOWED_HEADERS_CI or name.upper() == "AUTHORIZATION":
                del request.headers[name]


def build_guarded_http_client(
    base_url: str | None = None,
    headers: dict | None = None,
    timeout: httpx.Timeout | None = None,
) -> httpx.AsyncClient:
    """构造带出站守卫的 httpx.AsyncClient（校验传输层 + 跨源剥头 + follow_redirects=True）。

    Args:
        base_url: 目标端点（可空——仅对实际发出的请求做每跳校验）。
        headers: 客户端级默认 Header。
        timeout: 超时配置；None 时用 httpx 默认。

    Returns:
        校验传输层的 httpx.AsyncClient，可作 ``http_client`` 注入自定义后端。
    """
    kwargs: dict = {
        "base_url": base_url or "",
        "transport": _SSRFTransport(),
        "follow_redirects": True,
    }
    if headers is not None:
        kwargs["headers"] = headers
    if timeout is not None:
        kwargs["timeout"] = timeout
    return httpx.AsyncClient(**kwargs)
