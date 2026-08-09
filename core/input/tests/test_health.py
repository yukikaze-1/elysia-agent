"""Tests for core/input/server.py — /health 与 /ready 探活端点（批次 7 Task 9）。

/health 恒 200（liveness），status 反映依赖健康度（ok/degraded）；
/ready 依赖不 ok 时 503（readiness）。依赖检查经 asyncio.wait_for 限时，
挂起/异常一律视为 down。

Run: python -m pytest core/input/tests/test_health.py -v --asyncio-mode=auto
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.service
class TestHealthEndpoints:
    @pytest.fixture
    def app(self):
        from core.input.server import create_app

        agent = MagicMock()
        agent.status.current_name.return_value = "idle"
        agent.llm.model = "test-model"
        agent.session._messages = [1, 2, 3]
        agent.session.health_check = AsyncMock(return_value=True)
        agent.long_term.count = 42
        agent.long_term.health_check = AsyncMock(return_value=True)
        # P1-05：/ready 聚合 message/attachment repo 真实健康
        agent.message_repo.health_check = AsyncMock(return_value=True)
        agent.attachment_repo.health_check = AsyncMock(return_value=True)
        agent.tool_adapter._manager.tool_names = ["t1", "t2"]
        agent.output = MagicMock()
        agent.bus = MagicMock()
        return create_app(agent, serve_static=False)

    @pytest.mark.asyncio
    async def test_deps_ok_health_and_ready(self, app, aiohttp_client):
        """依赖全 ok → /health status==ok 且 200，/ready 200。"""
        client = await aiohttp_client(app)
        resp = await client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert data["fsm"] == "idle"
        ready = await client.get("/ready")
        assert ready.status == 200
        assert (await ready.json())["status"] == "ok"

    @pytest.mark.asyncio
    async def test_dep_false_health_degraded_ready_503(self, app, aiohttp_client):
        """依赖一 false（long_term）→ /health status==degraded 且 200，/ready 503。"""
        agent = app["agent"]
        agent.long_term.health_check = AsyncMock(return_value=False)
        client = await aiohttp_client(app)
        resp = await client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "degraded"
        assert data["dependencies"]["milvus"] is False
        assert data["dependencies"]["mongo"] is True
        ready = await client.get("/ready")
        assert ready.status == 503
        assert (await ready.json())["status"] == "degraded"

    @pytest.mark.asyncio
    async def test_health_response_fields(self, app, aiohttp_client):
        """/health 响应含 version / build_sha / dependencies / fsm 等字段。"""
        client = await aiohttp_client(app)
        resp = await client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        for key in ("status", "fsm", "llm", "memory", "tools",
                    "version", "build_sha", "uptime", "request_id", "dependencies"):
            assert key in data, f"缺少字段 {key}"
        assert set(data["dependencies"]) == {"mongo", "milvus"}
        assert isinstance(data["version"], str) and data["version"]
        assert isinstance(data["request_id"], str) and data["request_id"]
        assert isinstance(data["uptime"], (int, float))

    @pytest.mark.asyncio
    async def test_health_does_not_advance_request_counter(self, app, aiohttp_client, monkeypatch):
        """探活不扰动 request_id 计数器——3 次 /health 后 next_request_id 零调用（批次 8a Task 15）。"""
        agent = app["agent"]
        calls = {"n": 0}
        orig = agent.next_request_id

        def spy(*_args, **_kwargs):
            calls["n"] += 1
            return orig()

        monkeypatch.setattr(agent, "next_request_id", spy)
        client = await aiohttp_client(app)
        for _ in range(3):
            resp = await client.get("/health")
            assert resp.status == 200
        assert calls["n"] == 0

    @pytest.mark.asyncio
    async def test_hanging_dep_timeout_treated_down(self, app, aiohttp_client):
        """依赖检查被 asyncio.wait_for 包裹——挂起依赖超时后按 down 处理，请求不阻塞。"""
        agent = app["agent"]

        async def _hang() -> bool:
            await asyncio.sleep(3600)
            return True

        agent.session.health_check = AsyncMock(side_effect=_hang)
        with patch("core.input.server._HEALTH_TIMEOUT", 0.1):
            client = await aiohttp_client(app)
            resp = await client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "degraded"
        assert data["dependencies"]["mongo"] is False
