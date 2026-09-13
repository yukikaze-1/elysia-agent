# 测试文件：core/input/tests/test_error_envelope.py
"""未处理异常 → 统一错误信封（S2-F4）。

**缺陷原状**：`create_app` 只挂 payload/nosniff/rate/auth 四个中间件，**没有**
"异常 → 信封"的兜底。于是 `[1,2]` 这种合法 JSON 但非 object 的 body 会让
`body.get(...)` 抛 `AttributeError` → 纯文本 500（实测 5 处：`POST /api/providers/test`、
`PATCH /api/sessions/{id}`、`PUT /debug/api/ltm/{id}` 等）。客户端拿到的是 HTML/纯文本，
无法区分"我请求写错了"与"服务器有 bug"，54 码错误体系在这条路径上完全失效。
"""

from unittest.mock import MagicMock

import pytest
from aiohttp import web

pytestmark = pytest.mark.service


def _app(agent: MagicMock | None = None):
    from core.input.server import create_app
    return create_app(agent or MagicMock(), serve_static=False)


# ═══════════════════════════════════════════════════════════════════
# 中间件本身
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_unhandled_exception_becomes_envelope():
    """兜底中间件把任意未处理异常变成 INTERNAL 信封（而非 aiohttp 纯文本 500）。"""
    from core.input.errors import _error_envelope_middleware

    async def _boom(request):
        raise AttributeError("'list' object has no attribute 'get'")

    req = MagicMock(spec=web.Request)
    req.method = "POST"
    req.path = "/x"
    resp = await _error_envelope_middleware(req, _boom)

    assert isinstance(resp, web.Response)
    assert resp.status == 500
    body = resp.body.decode()
    assert "INTERNAL" in body and '"requestId"' in body, body


@pytest.mark.asyncio
async def test_http_exception_passes_through():
    """`web.HTTPException` 是显式控制流：必须原样抛出，不得被包成 500。"""
    from core.input.errors import _error_envelope_middleware

    async def _not_found(request):
        raise web.HTTPNotFound()

    req = MagicMock(spec=web.Request)
    req.method = "GET"
    req.path = "/x"
    with pytest.raises(web.HTTPNotFound):
        await _error_envelope_middleware(req, _not_found)


@pytest.mark.asyncio
async def test_cancelled_error_passes_through():
    """取消同样原样传播——吞掉它会挂死关停。"""
    import asyncio

    from core.input.errors import _error_envelope_middleware

    async def _cancelled(request):
        raise asyncio.CancelledError

    req = MagicMock(spec=web.Request)
    req.method = "GET"
    req.path = "/x"
    with pytest.raises(asyncio.CancelledError):
        await _error_envelope_middleware(req, _cancelled)


@pytest.mark.asyncio
async def test_normal_response_untouched():
    from core.input.errors import _error_envelope_middleware

    async def _ok(request):
        return web.json_response({"ok": True})

    req = MagicMock(spec=web.Request)
    resp = await _error_envelope_middleware(req, _ok)
    assert resp.status == 200


# ═══════════════════════════════════════════════════════════════════
# _json_object：形态错误是 400，不是 500
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("payload", ["[1,2]", '"abc"', "null", "123"])
@pytest.mark.asyncio
async def test_non_object_body_is_400_envelope(payload):
    """合法 JSON 但非 object → 400 CONTRACT_VALIDATION（原先是非结构化 500）。"""
    from core.input.errors import _json_object

    req = MagicMock(spec=web.Request)

    async def _json():
        import json
        return json.loads(payload)

    req.json = _json
    with pytest.raises(Exception) as ei:
        await _json_object(req)
    assert type(ei.value).__name__ == "_JsonBodyError"


@pytest.mark.asyncio
async def test_malformed_json_is_json_body_error():
    from core.input.errors import _json_object

    req = MagicMock(spec=web.Request)

    async def _json():
        raise ValueError("not json")

    req.json = _json
    with pytest.raises(Exception) as ei:
        await _json_object(req)
    assert type(ei.value).__name__ == "_JsonBodyError"


@pytest.mark.asyncio
async def test_object_body_returned_as_is():
    from core.input.errors import _json_object

    req = MagicMock(spec=web.Request)

    async def _json():
        return {"a": 1}

    req.json = _json
    assert await _json_object(req) == {"a": 1}


@pytest.mark.asyncio
async def test_request_entity_too_large_passes_through():
    """F13 语义：chunked 超限的 413 必须原样放行——包成 400 会把"太大"说成"格式错"。"""
    from core.input.errors import _json_object

    req = MagicMock(spec=web.Request)

    async def _json():
        raise web.HTTPRequestEntityTooLarge(max_size=1, actual_size=2)

    req.json = _json
    with pytest.raises(web.HTTPRequestEntityTooLarge):
        await _json_object(req)


# ═══════════════════════════════════════════════════════════════════
# 端到端：真实端点
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_middleware_is_actually_installed_in_create_app(aiohttp_client):
    """**接线验证**：`create_app` 必须真的装上兜底中间件。

    临时挂一个必炸的路由——这是唯一能证明"中间件在装配链里"的方式（单测只证明
    中间件本身对；忘了 append 就全绿）。
    """
    app = _app()

    async def _boom(request):
        raise AttributeError("'list' object has no attribute 'get'")

    app.router.add_post("/__boom", _boom)
    client = await aiohttp_client(app)

    resp = await client.post("/__boom")
    assert resp.status == 500
    data = await resp.json()
    assert data.get("code") == "INTERNAL", data


@pytest.mark.asyncio
async def test_non_object_body_is_client_error_not_server_error(aiohttp_client):
    """对真实端点发数组 body：必须是 **4xx**（客户端错误），不是 5xx。

    端点自带 `except Exception → 400`（ApiFail 形状）；无论走端点自处理还是兜底
    中间件，关键语义是"这不是服务器 bug"。此前的行为是**纯文本 500**。
    """
    agent = MagicMock()
    agent.output = MagicMock()
    agent.bus = MagicMock()
    from core.agent import RunState
    agent._state = RunState.RUNNING
    agent.state = RunState.RUNNING
    app = _app(agent)
    client = await aiohttp_client(app)

    resp = await client.post("/api/providers/test", json=[1, 2])
    assert resp.status == 400, f"应为 4xx，实得 {resp.status}"
    assert (await resp.json()), "必须是结构化 body，不能是纯文本"
