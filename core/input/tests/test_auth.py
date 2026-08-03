"""批次 1 认证测试：access_token 保护 /api/* /uploads/* /ws，无 token 环境匿名回归（marker=service）。"""

import json

import pytest
import pytest_asyncio

from frontend_contract.rest import ProtocolError


@pytest.fixture
def make_auth_app():
    """构造带 access_token 的 app（access_token 作为 create_app 显式参数传入）。"""
    from unittest.mock import MagicMock
    from core.agent import RunState
    from core.input.server import create_app

    def _make(access_token: str = ""):
        agent = MagicMock()
        agent.output = MagicMock()
        agent.bus = MagicMock()
        agent._state = RunState.RUNNING
        agent.request_shutdown = MagicMock()
        agent.request_restart = MagicMock()
        # /health handler 序列化需要的属性 stub
        agent.status.current_name.return_value = "idle"
        agent.llm.model = "fake/test"
        agent.session._messages = []
        agent.long_term.count = 0
        agent.tool_adapter._manager.tool_names = ["t1"]
        return create_app(agent, serve_static=False, access_token=access_token)

    return _make


async def _parse_401(resp) -> dict:
    """解析 401 响应体为 ProtocolError 信封。"""
    body = json.loads((await resp.read()).decode())
    return ProtocolError.model_validate(body).wire_dict()


@pytest.mark.service
class TestAuthMiddleware:
    @pytest.mark.asyncio
    async def test_anonymous_api_401_auth_required(self, make_auth_app, aiohttp_client):
        """有 token 环境：匿名 /api/* → 401 AUTH_REQUIRED。"""
        app = make_auth_app(access_token="secret")
        client = await aiohttp_client(app)
        resp = await client.get("/api/providers")
        assert resp.status == 401
        env = await _parse_401(resp)
        assert env["code"] == "AUTH_REQUIRED"
        assert env["error"] == env["message"]  # 兼容别名
        assert env["retryable"] is False
        assert env["requestId"]  # 独立 UUID

    @pytest.mark.asyncio
    async def test_wrong_token_401_auth_invalid(self, make_auth_app, aiohttp_client):
        """有 token 环境：错误 token → 401 AUTH_INVALID。"""
        app = make_auth_app(access_token="secret")
        client = await aiohttp_client(app)
        resp = await client.get("/api/providers", headers={"Authorization": "Bearer wrong"})
        assert resp.status == 401
        env = await _parse_401(resp)
        assert env["code"] == "AUTH_INVALID"

    @pytest.mark.asyncio
    async def test_valid_token_passes(self, make_auth_app, aiohttp_client):
        """有 token 环境：有效 token → 请求通过。"""
        app = make_auth_app(access_token="secret")
        client = await aiohttp_client(app)
        resp = await client.get("/api/providers", headers={"Authorization": "Bearer secret"})
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_uploads_anonymous_401(self, make_auth_app, aiohttp_client):
        """有 token 环境：/uploads/* GET 匿名 → 401（匿名资源读取被拒）。"""
        app = make_auth_app(access_token="secret")
        client = await aiohttp_client(app)
        resp = await client.get("/uploads/images/nonexistent.png")
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_uploads_valid_token_passes_static(self, make_auth_app, aiohttp_client, tmp_path, monkeypatch):
        """有 token 环境：/uploads/* 带有效 token → 静态服务（文件不存在则 404 而非 401）。"""
        import os
        monkeypatch.setenv("ELYSIA_PATH_DATA_UPLOADS", str(tmp_path))
        app = make_auth_app(access_token="secret")
        client = await aiohttp_client(app)
        resp = await client.get("/uploads/x.png", headers={"Authorization": "Bearer secret"})
        # 通过认证 → 静态处理器接管（文件不存在 → 404；关键断言是非 401）
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_ws_anonymous_handshake_rejected(self, make_auth_app, aiohttp_client):
        """有 token 环境：/ws 握手匿名 → 401 拒绝升级（非 101）。"""
        import aiohttp
        app = make_auth_app(access_token="secret")
        client = await aiohttp_client(app)
        with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
            async with client.session.ws_connect(client.make_url("/ws")):
                pass
        assert exc.value.status == 401

    @pytest.mark.asyncio
    async def test_health_not_protected(self, make_auth_app, aiohttp_client):
        """/health 不受产品认证保护（探活端点）。"""
        app = make_auth_app(access_token="secret")
        client = await aiohttp_client(app)
        resp = await client.get("/health")
        assert resp.status == 200


@pytest.mark.service
class TestAuthAnonymousRegression:
    @pytest.mark.asyncio
    async def test_no_token_env_anonymous(self, make_auth_app, aiohttp_client):
        """无 token 环境：/api/* 匿名全通（v1 兼容回归）。"""
        app = make_auth_app(access_token="")
        client = await aiohttp_client(app)
        resp = await client.get("/api/providers")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_no_token_env_ws_connect(self, make_auth_app, aiohttp_client):
        """无 token 环境：/ws 匿名可升级。"""
        app = make_auth_app(access_token="")
        client = await aiohttp_client(app)
        async with client.session.ws_connect(client.make_url("/ws")) as ws:
            assert ws.closed is False


@pytest.mark.service
class TestStartupGuard:
    def test_non_loopback_no_token_logs_error(self, caplog):
        """非回环 host + 无 access_token → ERROR 级显著阻塞告警。"""
        import logging
        from core.input.server import warn_if_unsecured
        with caplog.at_level(logging.ERROR, logger="core.input.server"):
            warn_if_unsecured("0.0.0.0", "")
        assert any("安全警告" in r.message for r in caplog.records)

    def test_loopback_no_token_no_warning(self, caplog):
        """回环 host + 无 token → 不告警（本地开发）。"""
        import logging
        from core.input.server import warn_if_unsecured
        with caplog.at_level(logging.ERROR, logger="core.input.server"):
            warn_if_unsecured("127.0.0.1", "")
        assert not any("安全警告" in r.message for r in caplog.records)

    def test_non_loopback_with_token_no_warning(self, caplog):
        """非回环 + 有 access_token → 不告警（已认证）。"""
        import logging
        from core.input.server import warn_if_unsecured
        with caplog.at_level(logging.ERROR, logger="core.input.server"):
            warn_if_unsecured("0.0.0.0", "secret")
        assert not any("安全警告" in r.message for r in caplog.records)


@pytest.mark.service
class TestErrorEnvelopeConsistency:
    @pytest.mark.asyncio
    async def test_request_id_is_uuid_not_counter(self, make_auth_app, aiohttp_client):
        """requestId 是 req_+hex 独立 UUID（非 agent.next_request_id 的三位计数器）。"""
        import re
        app = make_auth_app(access_token="secret")
        client = await aiohttp_client(app)
        resp = await client.get("/api/providers")
        env = await _parse_401(resp)
        assert re.fullmatch(r"req_[0-9a-f]{24}", env["requestId"]), env["requestId"]

    @pytest.mark.asyncio
    async def test_details_public_safe(self, make_auth_app, aiohttp_client):
        """错误信封 details 默认空 dict（不含密钥/正文/路径）。"""
        app = make_auth_app(access_token="secret")
        client = await aiohttp_client(app)
        resp = await client.get("/api/providers")
        env = await _parse_401(resp)
        assert env["details"] == {}
        # 信封整体不含密钥痕迹
        body = (await resp.read()).decode()
        assert "sk-" not in body and "AGENT_ACCESS_TOKEN" not in body
