"""SSRFGuard 测试 — validate_endpoint + 守卫传输层（固定样例，mock socket.getaddrinfo 不触网）。"""
import ipaddress
import socket
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from core.input.ssrf import (
    SSRFError,
    _ALLOWED_HEADERS,
    _SSRFTransport,
    _is_blocked_ip,
    build_guarded_http_client,
    validate_endpoint,
)


def fake_getaddrinfo(host, port, *args, **kwargs):
    """mock socket.getaddrinfo：host 为字面量 IP → 解析到自身；否则解析到公网 IP（不触网）。"""
    try:
        ipaddress.ip_address(host)
        resolved = host
    except ValueError:
        resolved = "8.8.8.8"
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (resolved, port))]


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",  # 回环
        "http://10.0.0.1",  # 私网
        "http://192.168.1.1",  # 私网
        "http://172.16.0.1",  # 私网
        "http://169.254.169.254",  # 链路本地（云元数据）
        "http://[::1]",  # IPv6 回环
        "http://fe80::1",  # IPv6 链路本地
        "http://0.0.0.0",  # 受限
        "http://255.255.255.255",  # 保留
        "http://224.0.0.1",  # 多播
        "http://100.64.0.1",  # CGNAT
        "169.254.169.254",  # 无 scheme → https 后仍拒绝
        "http://metadata",  # host=metadata 字面量黑名单
        # review 修正：IPv4-mapped IPv6（::ffff:x.x.x.x）必须归一为 IPv4 判类——
        # 否则 ipaddress 对 mapped 地址 is_private/is_link_local 恒 False、CGNAT 跨族恒 False
        "http://[::ffff:127.0.0.1]",  # mapped 回环
        "http://[::ffff:10.0.0.1]",  # mapped 私网
        "http://[::ffff:169.254.169.254]",  # mapped 链路本地（云元数据）
        "http://[::ffff:100.100.100.200]",  # mapped CGNAT/元数据（阿里云 ECS 元数据端点）
        "http://[::ffff:100.64.0.1]",  # mapped CGNAT
    ],
)
def test_validate_rejects_blocked(url):
    """非法 endpoint → SSRFError。"""
    with patch.object(socket, "getaddrinfo", side_effect=fake_getaddrinfo):
        with pytest.raises(SSRFError):
            validate_endpoint(url)


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",  # 解析公网
        "example.com",  # 无 scheme → https
        "http://example.com",  # 显式 http 公网
        "http://[2001:4860:4860::8888]",  # 公网 IPv6
    ],
)
def test_validate_passes_public(url):
    """合法公网 endpoint → 不抛。"""
    with patch.object(socket, "getaddrinfo", side_effect=fake_getaddrinfo):
        validate_endpoint(url)


@pytest.mark.unit
@pytest.mark.parametrize("url", ["", "   ", "\n\t "])
def test_validate_empty_or_whitespace_passes(url):
    """空串/空白 → 直接放行（内置 provider 无 body base_url 不误伤）。"""
    validate_endpoint(url)


@pytest.mark.unit
def test_validate_nxdomain_raises_unresolvable():
    """getaddrinfo 抛 gaierror（NXDOMAIN）→ SSRFError，消息含「无法解析」。"""

    def nxdomain(host, port, *args, **kwargs):
        raise socket.gaierror(8, "Name or service not known")

    with patch.object(socket, "getaddrinfo", side_effect=nxdomain):
        with pytest.raises(SSRFError) as exc:
            validate_endpoint("https://nope.invalid")
    assert "无法解析" in str(exc.value)


@pytest.mark.unit
def test_is_blocked_ip_covers_cgnat_and_keeps_public():
    """CGNAT（100.64/10）不在标准属性集内，须显式判类；公网 IP 放行。"""
    assert _is_blocked_ip("100.64.0.1") is True
    assert _is_blocked_ip("100.127.255.255") is True
    assert _is_blocked_ip("8.8.8.8") is False
    assert _is_blocked_ip("2001:4860:4860::8888") is False


@pytest.mark.unit
def test_is_blocked_ip_covers_site_local_ipv6():
    from core.input.ssrf import _is_blocked_ip

    assert _is_blocked_ip("fec0::1") is True  # site-local（当前漏判 → 放行）
    assert _is_blocked_ip("fe80::1") is True  # link-local（已有）
    assert _is_blocked_ip("2001:4860:4860::8888") is False  # 公网（既有 test_ssrf.py 用址）


@pytest.mark.unit
def test_allowed_headers_exact_set():
    """Header 允许名单：仅 Authorization/Content-Type/Accept/User-Agent。"""
    assert _ALLOWED_HEADERS == {
        "Authorization",
        "Content-Type",
        "Accept",
        "User-Agent",
    }


@pytest.mark.unit
def test_build_guarded_http_client_transport_is_guard():
    """build_guarded_http_client → 传输层是校验子类 + follow_redirects=True。"""
    client = build_guarded_http_client()
    assert isinstance(client._transport, _SSRFTransport)
    assert client.follow_redirects is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transport_rejects_internal_ip_each_hop():
    """每跳校验：请求目标解析到内网 IP → SSRFError（连接前拦截，不触网）。"""
    transport = _SSRFTransport()
    req = httpx.Request("GET", "http://10.0.0.1/")
    with patch.object(socket, "getaddrinfo", side_effect=fake_getaddrinfo), patch.object(
        httpx.AsyncHTTPTransport, "handle_async_request", new=AsyncMock()
    ):
        with pytest.raises(SSRFError):
            await transport.handle_async_request(req)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transport_strips_cross_origin_headers():
    """跨源剥头：X-API-Key/Authorization 跨 host 被剥、允许名单头保留；同 host 保留。"""
    transport = _SSRFTransport()
    resp = httpx.Response(200)
    with patch.object(socket, "getaddrinfo", side_effect=fake_getaddrinfo), patch.object(
        httpx.AsyncHTTPTransport, "handle_async_request", new=AsyncMock(return_value=resp)
    ):
        # 首次请求 → 记录 origin，头保留
        req1 = httpx.Request(
            "GET",
            "http://example.com/api",
            headers={"X-API-Key": "k1", "Authorization": "Bearer t", "Accept": "application/json"},
        )
        await transport.handle_async_request(req1)
        # 跨源重定向 → 剥非允许名单头 + Authorization
        req2 = httpx.Request(
            "GET",
            "http://other.example.com/x",
            headers={"X-API-Key": "k1", "Authorization": "Bearer t", "Accept": "application/json"},
        )
        await transport.handle_async_request(req2)
        assert "X-API-Key" not in req2.headers
        assert "Authorization" not in req2.headers
        assert req2.headers["Accept"] == "application/json"
        # 同 host → 保留
        req3 = httpx.Request(
            "GET",
            "http://example.com/other",
            headers={"X-API-Key": "k1", "Authorization": "Bearer t"},
        )
        await transport.handle_async_request(req3)
        assert "X-API-Key" in req3.headers
        assert "Authorization" in req3.headers
