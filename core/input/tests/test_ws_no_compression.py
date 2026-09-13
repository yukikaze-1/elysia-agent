# 测试文件：core/input/tests/test_ws_no_compression.py
"""S2-E4：服务端必须**不协商** `permessage-deflate`。

aiohttp 的 `WebSocketResponse` 默认 `compress=True`。该扩展与部分客户端组合时，
**空闲约 75s 后的第一帧**会被判 `Received frame with non-zero reserved bits` 并以
关闭码 1002 断连（真实联调存档：
`docs/review/archive/2026-08/2026-08-14-Windows-Electron-第三次真实联调结果.md`）。
A/B 实测：默认压缩 → 1002；客户端 `perMessageDeflate:false` → 1000。

若那一帧是 `text`，用户消息**从未到达后端**，前端只会看到 15s 后的 ACK 超时，
连接被 1002 关闭后重连，语音会话与 desktop 注册全部重建。

此前只在 Electron 侧规避——后端纵深防护一直没落地。这条测试锁的是**服务端握手**，
不再依赖任一客户端记得关。
"""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp import web

pytestmark = pytest.mark.service


@pytest.mark.asyncio
async def test_handshake_does_not_negotiate_deflate(aiohttp_client):
    """客户端主动请求压缩 → 服务端响应头**不得**出现 `permessage-deflate`。"""
    from core.input.ws_handler import _ws_response_kwargs

    async def handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(**_ws_response_kwargs())
        await ws.prepare(request)
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/ws", handler)
    client = await aiohttp_client(app)

    # compress=15 = 客户端请求 permessage-deflate（浏览器/Node ws 默认行为）
    async with client.ws_connect("/ws", compress=15) as ws:
        negotiated = ws._response.headers.get("Sec-WebSocket-Extensions")  # noqa: SLF001
        assert not negotiated or "permessage-deflate" not in negotiated, (
            f"服务端协商了压缩扩展：{negotiated!r} —— 空闲后首帧会以 1002 断连")


@pytest.mark.asyncio
async def test_handshake_without_compression_request(aiohttp_client):
    """回归网：客户端不请求压缩时行为不变。"""
    from core.input.ws_handler import _ws_response_kwargs

    async def handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(**_ws_response_kwargs())
        await ws.prepare(request)
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/ws", handler)
    client = await aiohttp_client(app)

    async with client.ws_connect("/ws", compress=False) as ws:
        assert ws.closed is False
