"""批次 1 认证测试：access_token 保护 /api/* /uploads/* /ws，无 token 环境匿名回归（marker=service）。"""

import json

import pytest
import pytest_asyncio

from frontend_contract.rest import ProtocolError


@pytest.fixture
def make_auth_app():
    """构造带 token 的 app（access_token/auth_token 作为 create_app 显式参数传入）。"""
    from unittest.mock import AsyncMock, MagicMock
    from core.agent import RunState
    from core.input.server import create_app

    def _make(access_token: str = "", auth_token: str = ""):
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
        agent.session.health_check = AsyncMock(return_value=True)
        agent.long_term.count = 0
        agent.long_term.health_check = AsyncMock(return_value=True)
        # 走 ToolAdapter 公开门面（批次 A）：路由/探针/DebugInfo 都从门面读，
        # 故 stub 必须挂在 adapter 上而非其内部 _manager（2026-09-12）
        agent.tool_adapter.tool_names = ["t1"]
        # /debug/api handler（collect → DebugData 契约）需要的属性 stub——批次 8a D1
        # 测试断言管理端点带 token 时 200，MagicMock agent 需满足 DebugInfo.collect
        # 全部读取路径（缺省则 async 字段 TypeError / 契约 MagicMock 校验失败 → 500）。
        agent.status.previous_name.return_value = "idle"
        agent.started_at = 0.0
        agent.cognitive._phase = None
        agent.cognitive._current_task = None
        agent.cognitive._timing_history = []
        agent.idle._character.proactive.enabled = False
        agent.idle._character.proactive.interval = 60.0
        agent.idle._last_interaction = None
        agent.session.messages_for_client.return_value = []
        agent.session.character_state.model_dump.return_value = {}
        agent.session.character_state.mood = "calm"
        agent.long_term._entries = []
        agent.long_term._repo = MagicMock()
        agent.long_term._embedding_provider._backend = None
        agent.memory_repo.health_check = AsyncMock(return_value=True)
        agent.session_repo.health_check = AsyncMock(return_value=True)
        agent.tool_adapter.plugin_list = []
        agent.tool_adapter.executing_now = []
        agent.tool_adapter.history = []
        agent.consolidator._history = []
        agent.consolidator._buffer = []
        agent.consolidator._threshold = 6
        agent.character.name = "mock"
        agent.character.base_personality = "mock"
        agent.character_engine.render_c1.return_value.self_personality = "mock"
        agent.character_engine.state.state_l3 = "mock"
        agent.character_engine.state.state_history = []
        agent.character_engine.state.last_awareness_time = 0.0
        agent.character_engine.state.self_perception_history = []
        # Task 12（§6.5 读端迁移）：debug/info.py 读当前 instance engine.state 的
        # mood / model_dump（替代旧 session.character_state 读路径）——stub 补齐。
        agent.character_engine.state.mood = "calm"
        agent.character_engine.state.model_dump.return_value = {}
        agent.character_engine.version_repo = None
        agent.character_reflector.buffer_size = 50
        agent.character_reflector.last_reflection_at = 0.0
        agent.character_reflector.locked = False
        agent.character_reflector.last_prompt_system = ""
        agent.character_reflector.last_prompt_user = ""
        agent.character_reflector.last_success = False
        agent.character_reflector.last_duration_ms = 0.0
        agent.character_reflector.last_text_len = 0
        agent.stt_ok = False
        agent.scheduler.pending_count = 0
        agent.scheduler.running_count = 0
        agent.scheduler.total_count = 0
        agent.memory_reflector._round_count = 0
        agent.memory_reflector.ROUND_THRESHOLD = 10
        agent.memory_reflector._new_memory_count = 0
        agent.memory_reflector.NEW_MEMORY_THRESHOLD = 5
        agent.memory_reflector._lock = MagicMock()
        agent.memory_reflector._lock.locked.return_value = False
        agent.memory_reflector._last_summary = None
        agent.token_tracker = None
        agent.prompt_recorder = None
        agent.response_recorder = None
        return create_app(agent, serve_static=False,
                          access_token=access_token, auth_token=auth_token)

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

    @pytest.mark.asyncio
    async def test_auth_failure_logged_without_token(self, make_auth_app, aiohttp_client, caplog):
        """认证失败记日志（code+path），且不含 token 原文（敏感信息纪律）。"""
        app = make_auth_app(access_token="secret")
        client = await aiohttp_client(app)
        await client.get("/api/providers")
        assert any("AUTH_REQUIRED" in r.message or "AUTH_INVALID" in r.message
                   for r in caplog.records)
        # 日志不含 token 原文
        assert not any("secret" in r.message for r in caplog.records)


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
        """非回环 + 双 token（access+auth）→ 不告警（已认证，语义防反转）。"""
        import logging
        from core.input.server import warn_if_unsecured
        with caplog.at_level(logging.ERROR, logger="core.input.server"):
            warn_if_unsecured("0.0.0.0", "secret", "admin")
        assert not any("安全警告" in r.message for r in caplog.records)

    def test_non_loopback_one_token_warns(self, caplog):
        """非回环 + 仅配置一侧 token → 提示同时配置另一侧（批次 8a D1）。"""
        import logging
        from core.input.server import warn_if_unsecured
        with caplog.at_level(logging.WARNING, logger="core.input.server"):
            warn_if_unsecured("0.0.0.0", "secret", "")
        assert any("建议同时配置" in r.message for r in caplog.records)


@pytest.mark.service
class TestAdminAuthStrictSeparation:
    """CR-03（2026-09-12）：管理面**只认 `SERVER_AUTH_TOKEN`**，不再回退产品 token。

    本类原名 `TestAdminAuthFallback`，断言的是「批次 8a D1：auth_token 空 +
    access_token 非空 → 管理端点 fallback 用 access_token」。评测报告 CR-03 判定
    该行为是**授权域塌缩**：任何持有前端产品 token 的客户端都能触达 `/shutdown`
    `/restart` `/tools/*` 与全部 `/debug/api/*`（含三个不可逆清空端点），与
    CR-09/CR-04 叠加即"客户端 token → 不可逆销毁"。故改为 fail-closed。

    注意这是**行为变更**而非纯 bugfix——原行为是有意为之且有测试锁定。
    """

    @pytest.mark.asyncio
    async def test_admin_anonymous_401_when_only_access_token(self, make_auth_app, aiohttp_client):
        """仅配 access_token：匿名访问 /debug/api → 401。"""
        app = make_auth_app(access_token="secret")
        client = await aiohttp_client(app)
        resp = await client.get("/debug/api")
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_admin_with_access_token_rejected(self, make_auth_app, aiohttp_client):
        """仅配 access_token：**产品 token 不得进入管理面**（CR-03 核心断言）。"""
        app = make_auth_app(access_token="secret")
        client = await aiohttp_client(app)
        resp = await client.get("/debug/api", headers={"Authorization": "Bearer secret"})
        assert resp.status == 401, "产品 token 不授予管理面（原 fallback 行为已废止）"

    @pytest.mark.asyncio
    async def test_admin_wrong_token_401(self, make_auth_app, aiohttp_client):
        """仅配 access_token：错误 token 访问 /debug/api → 401。"""
        app = make_auth_app(access_token="secret")
        client = await aiohttp_client(app)
        resp = await client.get("/debug/api", headers={"Authorization": "Bearer wrong"})
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_shutdown_with_access_token_rejected(self, make_auth_app, aiohttp_client):
        """仅配 access_token：产品 token 不得触发 /shutdown（不可逆破坏端点）。"""
        app = make_auth_app(access_token="secret")
        client = await aiohttp_client(app)
        resp = await client.post("/shutdown", headers={"Authorization": "Bearer secret"})
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_restart_with_access_token_rejected(self, make_auth_app, aiohttp_client):
        """仅配 access_token：产品 token 不得触发 /restart。"""
        app = make_auth_app(access_token="secret")
        client = await aiohttp_client(app)
        resp = await client.post("/restart", headers={"Authorization": "Bearer secret"})
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_destructive_debug_endpoint_rejected(self, make_auth_app, aiohttp_client):
        """仅配 access_token：产品 token 不得触达不可逆清空端点（CR-04 的放大器）。"""
        app = make_auth_app(access_token="secret")
        client = await aiohttp_client(app)
        resp = await client.delete(
            "/debug/api/archive?confirm=archive",
            headers={"Authorization": "Bearer secret"})
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_admin_open_when_no_token_configured(self, make_auth_app, aiohttp_client):
        """两个 token 都未配置 → 管理面放行（纯回环本地开发，由 assert_secure_bind 守非回环）。"""
        app = make_auth_app()
        client = await aiohttp_client(app)
        resp = await client.get("/debug/api")
        assert resp.status != 401

    @pytest.mark.asyncio
    async def test_two_tokens_strict_separation(self, make_auth_app, aiohttp_client):
        """双 token：product token 不能访问管理端点；admin token 不能访问产品 API。"""
        app = make_auth_app(access_token="prod", auth_token="admin")
        client = await aiohttp_client(app)
        assert (await client.get("/debug/api", headers={"Authorization": "Bearer prod"})).status == 401
        assert (await client.get("/debug/api", headers={"Authorization": "Bearer admin"})).status != 401
        assert (await client.get("/api/providers", headers={"Authorization": "Bearer admin"})).status == 401


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


@pytest.mark.service
class TestDeploymentSecurityPosture:
    """远程部署安全姿势（TLS 由反代终止，后端认证决策可重复测试）。"""

    @pytest.mark.asyncio
    async def test_remote_posture_anonymous_rejected_valid_works(self, make_auth_app, aiohttp_client):
        """有 token 环境（模拟远程）：匿名全拒、有效身份可用、/health 探活不受限。"""
        app = make_auth_app(access_token="remote-token")
        client = await aiohttp_client(app)
        # 匿名全拒
        assert (await client.get("/api/providers")).status == 401
        # 有效身份可用
        assert (await client.get("/api/providers", headers={"Authorization": "Bearer remote-token"})).status == 200
        # 探活不受产品认证保护
        assert (await client.get("/health")).status == 200

    @pytest.mark.asyncio
    async def test_logs_do_not_contain_token(self, make_auth_app, aiohttp_client, caplog):
        """认证失败/成功路径日志不含 token（敏感信息纪律）。"""
        import logging
        app = make_auth_app(access_token="super-secret-token")
        client = await aiohttp_client(app)
        with caplog.at_level(logging.DEBUG):
            await client.get("/api/providers")  # 匿名 → 401
            await client.get("/api/providers", headers={"Authorization": "Bearer super-secret-token"})
        joined = "\n".join(r.message for r in caplog.records)
        assert "super-secret-token" not in joined


@pytest.mark.service
class TestValidTokenWsChat:
    @pytest.mark.asyncio
    async def test_valid_token_ws_handshake_and_chat(self, aiohttp_client):
        """有 token 环境：有效 Bearer 的 /ws 握手成功且可聊天（"有效身份可完整聊天"闭环）。"""
        import asyncio
        from unittest.mock import AsyncMock
        from core.input.server import create_app
        from core.input.tests.test_server import _make_agent

        agent = _make_agent()
        await agent.start()
        agent.process_message = AsyncMock()
        app = create_app(agent, serve_static=False, access_token="secret")
        client = await aiohttp_client(app)
        try:
            async with client.session.ws_connect(
                client.make_url("/ws"), headers={"Authorization": "Bearer secret"},
            ) as ws:
                assert ws.closed is False
                await ws.send_json({"type": "text", "content": "hello"})
                await asyncio.sleep(0.1)
            agent.process_message.assert_called()
        finally:
            await agent.stop()


@pytest.mark.service
class TestRateLimitAuthPrincipal:
    """BE-P2-02：限流按认证主体分桶——合法 token（access/admin）独立配额互不挤占。"""

    @pytest.mark.asyncio
    async def test_two_valid_tokens_independent_rate_quotas(self, aiohttp_client):
        """两个合法 token → 独立限流桶：一个耗尽是另一个不受影响（互不挤占）。"""
        from core.input.server import create_app
        from core.input.tests.test_server import _make_agent

        agent = _make_agent()
        await agent.start()
        app = create_app(agent, serve_static=False,
                         access_token="token-a", auth_token="token-b",
                         rate_limits={"provider_test": 2})
        client = await aiohttp_client(app)
        try:
            hdr_a = {"Authorization": "Bearer token-a"}
            hdr_b = {"Authorization": "Bearer token-b"}
            # token-b（管理面）先耗尽自己的 2 次配额（业务 API 401，但限流桶独立消耗）
            for _ in range(2):
                assert (await client.post(
                    "/api/providers/test", json={"service": "llm"}, headers=hdr_b)).status != 429
            # token-a 配额独立：仍放行自己的 2 次（互不挤占）
            for _ in range(2):
                assert (await client.post(
                    "/api/providers/test", json={"service": "llm"}, headers=hdr_a)).status != 429
            # 各自第 3 次 → 429（独立桶已耗）
            assert (await client.post(
                "/api/providers/test", json={"service": "llm"}, headers=hdr_a)).status == 429
            assert (await client.post(
                "/api/providers/test", json={"service": "llm"}, headers=hdr_b)).status == 429
        finally:
            await agent.stop()


@pytest.mark.service
class TestSecureBind:
    """D6a：非回环绑定无 token → fail-closed 拒绝启动。

    隔离 ALLOW_INSECURE_REMOTE——开发者 .env 可能配了它（开发期远程直连），
    否则 raise 断言在宿主环境会失效（bind 守卫被 env 放行）。
    """

    def test_nonloopback_no_token_raises(self, monkeypatch):
        monkeypatch.delenv("ALLOW_INSECURE_REMOTE", raising=False)
        from core.input.server import assert_secure_bind
        with pytest.raises(RuntimeError, match="拒绝启动"):
            assert_secure_bind("0.0.0.0", "")

    def test_loopback_ok(self):
        from core.input.server import assert_secure_bind
        assert_secure_bind("127.0.0.1", "")
        assert_secure_bind("localhost", "")
        assert_secure_bind("::1", "")

    def test_with_access_token_ok(self):
        from core.input.server import assert_secure_bind
        assert_secure_bind("0.0.0.0", "secret-token")

    def test_auth_token_only_nonloopback_raises(self, monkeypatch):
        """仅配 SERVER_AUTH_TOKEN（管理面）不足以放行远程绑定——业务端点
        /api /ws /uploads 只受 AGENT_ACCESS_TOKEN 保护，auth_token 单独配置时
        业务接口仍匿名可访问（review 复核 #1）。"""
        monkeypatch.delenv("ALLOW_INSECURE_REMOTE", raising=False)
        from core.input.server import assert_secure_bind
        with pytest.raises(RuntimeError, match="拒绝启动"):
            assert_secure_bind("0.0.0.0", "", "auth-token")

    def test_allow_insecure_remote_ok(self, monkeypatch):
        from core.input.server import assert_secure_bind
        monkeypatch.setenv("ALLOW_INSECURE_REMOTE", "1")
        assert_secure_bind("0.0.0.0", "")
