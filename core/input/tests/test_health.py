"""Tests for core/input/server.py — /health 与 /ready 探活端点（批次 9 Task 9 探针分层）。

/health 纯 liveness：进程/FSM 存活检查 + 运行信息，零网络探针，恒 200；
/ready readiness：HTTP 门禁 = 核心 chat（mongo 并行 ping + product 模式 idempotency
ping），Milvus 仅上报不门禁；payload 带 chatReady/fullReady/dependencies。

Run: python -m pytest core/input/tests/test_health.py -v --asyncio-mode=auto
"""

import asyncio
import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_mock_agent() -> MagicMock:
    """构造健康端点测试用 mock agent（依赖 health_check 全部可翻转）。"""
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
    return agent


def _idem_repo(ok: bool) -> MagicMock:
    """idempotency 仓库 fake——health_check 可翻转。"""
    repo = MagicMock()
    repo.health_check = AsyncMock(return_value=ok)
    return repo


@pytest.mark.service
class TestHealthEndpoints:
    @pytest.fixture
    def app(self):
        from core.input.server import create_app
        return create_app(_make_mock_agent(), serve_static=False)

    @pytest.mark.asyncio
    async def test_deps_ok_health_and_ready(self, app, aiohttp_client):
        """依赖全 ok → /health 200 status ok，/ready 200 chatReady/fullReady true。"""
        client = await aiohttp_client(app)
        resp = await client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert data["fsm"] == "idle"
        ready = await client.get("/ready")
        assert ready.status == 200
        ready_data = await ready.json()
        assert ready_data["status"] == "ok"
        assert ready_data["chatReady"] is True
        assert ready_data["fullReady"] is True

    @pytest.mark.asyncio
    async def test_milvus_down_ready_200_chat_ready(self, app, aiohttp_client):
        """Milvus down → /health 200（liveness 不探依赖）；/ready 200（chatReady=true、
        fullReady=false）+ payload 解释（Milvus 不门禁 HTTP，文本可降级）。"""
        agent = app["agent"]
        agent.long_term.health_check = AsyncMock(return_value=False)
        client = await aiohttp_client(app)
        resp = await client.get("/health")
        assert resp.status == 200
        ready = await client.get("/ready")
        assert ready.status == 200
        data = await ready.json()
        assert data["chatReady"] is True
        assert data["fullReady"] is False
        assert data["dependencies"]["milvus"] is False
        assert data["dependencies"]["mongo"] is True
        # 可观测：全依赖非 ok → degraded（即便 HTTP 200）
        assert data["status"] == "degraded"

    @pytest.mark.asyncio
    async def test_ready_mongo_down_503(self, app, aiohttp_client):
        """mongo（session）down → /ready 503（核心 chat 门禁失败），chatReady=false。"""
        agent = app["agent"]
        agent.session.health_check = AsyncMock(return_value=False)
        client = await aiohttp_client(app)
        resp = await client.get("/ready")
        assert resp.status == 503
        data = await resp.json()
        assert data["chatReady"] is False
        assert data["dependencies"]["mongo"] is False

    @pytest.mark.asyncio
    async def test_ready_product_idempotency_down_503(self, aiohttp_client):
        """product 模式 + idempotency down → /ready 503（fail-closed），chatReady=false。"""
        from core.input.idempotency import reset_store
        reset_store()  # 防跨用例 _needs_recovery 泄漏（BE-P1-07 D4）
        from core.input.server import create_app
        app = create_app(_make_mock_agent(), serve_static=False, product_mode=True,
                         idempotency_repo=_idem_repo(ok=False))
        client = await aiohttp_client(app)
        resp = await client.get("/ready")
        assert resp.status == 503
        data = await resp.json()
        assert data["chatReady"] is False
        assert data["dependencies"]["idempotency"] is False
        assert data["dependencies"]["mongo"] is True

    @pytest.mark.asyncio
    async def test_ready_dev_idempotency_down_200(self, aiohttp_client):
        """dev 模式 + idempotency down → /ready 200（内存降级不门禁），chatReady=true。"""
        from core.input.idempotency import reset_store
        reset_store()  # 防跨用例 _needs_recovery 泄漏（BE-P1-07 D4）
        from core.input.server import create_app
        app = create_app(_make_mock_agent(), serve_static=False, product_mode=False,
                         idempotency_repo=_idem_repo(ok=False))
        client = await aiohttp_client(app)
        resp = await client.get("/ready")
        assert resp.status == 200
        data = await resp.json()
        assert data["chatReady"] is True
        assert data["fullReady"] is True
        assert data["dependencies"]["idempotency"] is False
        assert data["status"] == "degraded"  # 可观测：idempotency 非 ok

    @pytest.mark.asyncio
    async def test_health_response_fields(self, app, aiohttp_client):
        """/health 响应含 fsm/uptime/build 等运行信息，且无 dependencies（零网络探针）。"""
        client = await aiohttp_client(app)
        resp = await client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        for key in ("status", "fsm", "llm", "memory", "tools",
                    "version", "build_sha", "uptime", "request_id"):
            assert key in data, f"缺少字段 {key}"
        assert "dependencies" not in data
        assert isinstance(data["version"], str) and data["version"]
        assert isinstance(data["request_id"], str) and data["request_id"]
        assert isinstance(data["uptime"], (int, float))

    @pytest.mark.asyncio
    async def test_ready_response_fields(self, app, aiohttp_client):
        """/ready 响应含 chatReady / fullReady / dependencies（mongo/milvus/idempotency）。"""
        client = await aiohttp_client(app)
        resp = await client.get("/ready")
        assert resp.status == 200
        data = await resp.json()
        for key in ("status", "chatReady", "fullReady", "fsm", "llm", "memory",
                    "tools", "version", "build_sha", "uptime", "request_id",
                    "dependencies"):
            assert key in data, f"缺少字段 {key}"
        assert set(data["dependencies"]) == {"mongo", "milvus", "idempotency"}
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
    async def test_health_no_network_probes_fast(self, app, aiohttp_client):
        """依赖全挂起 → /health 零网络探针：<0.5s 返回 200 且不触碰任何 health_check（BE-P2-01）。"""
        agent = app["agent"]

        async def _hang() -> bool:
            await asyncio.sleep(3600)
            return True

        for name in ("session", "message_repo", "attachment_repo", "long_term"):
            getattr(agent, name).health_check = AsyncMock(side_effect=_hang)
        client = await aiohttp_client(app)
        start = time.monotonic()
        resp = await client.get("/health")
        elapsed = time.monotonic() - start
        assert resp.status == 200
        assert elapsed < 0.5, f"/health 应零网络探针快速返回，实际 {elapsed:.2f}s"
        # 零网络探针：4 个 health_check 均未被调用
        for name in ("session", "message_repo", "attachment_repo", "long_term"):
            assert getattr(agent, name).health_check.await_count == 0, \
                f"/health 不应探测 {name}"

    @pytest.mark.asyncio
    async def test_ready_hanging_mongo_parallel_timeout_503(self, app, aiohttp_client):
        """/ready mongo 三 repo 并行探测（asyncio.gather）：全挂起约 1×超时内返回 503，
        而非串行 3×超时。"""
        agent = app["agent"]

        async def _hang() -> bool:
            await asyncio.sleep(3600)
            return True

        for name in ("session", "message_repo", "attachment_repo"):
            getattr(agent, name).health_check = AsyncMock(side_effect=_hang)
        client = await aiohttp_client(app)
        with patch("core.input.health._HEALTH_TIMEOUT", 0.2):
            start = time.monotonic()
            resp = await client.get("/ready")
            elapsed = time.monotonic() - start
        assert resp.status == 503
        assert elapsed < 0.5, \
            f"mongo 三探针应并行（约 0.2s），实际 {elapsed:.2f}s（串行约 0.6s）"
        assert (await resp.json())["chatReady"] is False


@pytest.mark.service
class TestReadyIdempotencyNeedsRecovery:
    """D4：product 模式 /ready 幂等门禁含补偿状态——needs_recovery 时 503，补偿后 200。"""

    @pytest.fixture
    def _clean_store(self):
        from core.input.idempotency import reset_store
        reset_store()
        yield
        reset_store()

    def _server_info_agent(self):
        """build_server_info 可用的最小 agent stub（config/services 齐备，无需真实 Agent）。"""
        class _Cfg:
            class _L:
                static_image = ""
            class _M:
                model = "test-model"
            live2d = _L()
            llm = _M()
            stt = _M()
            tts = _M()

        class _Agent:
            config = _Cfg()
            llm = _Cfg._M()
            stt = None
            stt_enabled = False
            tts = None
            tts_enabled = False

        return _Agent()

    @pytest.mark.asyncio
    async def test_ready_503_until_recover_then_200(self, _clean_store, aiohttp_client):
        from core.input.capabilities import build_server_info
        from core.input.idempotency import configure_store, get_store
        from core.input.tests.test_idempotency_durable import _FlipRepo
        from core.input.tests.test_idempotency_durable import _entry as _make_entry
        from core.input.server import create_app

        repo = _FlipRepo(available=False)
        configure_store(repo, product_mode=False)
        store = get_store()
        # 降级窗口写一条记录 → needs_recovery=True
        await store.store("default", "crid", _make_entry("msg_1", "abc"))
        assert store.needs_recovery is True
        repo.available = True  # Mongo 恢复，但 recover 未跑
        assert get_store().durable is False
        # create_app 会以 product_mode=True 重配 store（configure 不清 needs_recovery）
        app = create_app(_make_mock_agent(), serve_static=False,
                         product_mode=True, idempotency_repo=repo)
        client = await aiohttp_client(app)
        # 补偿未完成 → /ready 503（chatReady=false）+ server_info durable=False
        resp = await client.get("/ready")
        assert resp.status == 503
        data = await resp.json()
        assert data["chatReady"] is False
        assert data["dependencies"]["idempotency"] is False
        assert build_server_info(self._server_info_agent()).features.idempotency_durable is False
        # 补偿完成 → /ready 200（durable=true，server_info 同步反映）
        await get_store().recover()
        assert get_store().durable is True
        assert build_server_info(self._server_info_agent()).features.idempotency_durable is True
        resp2 = await client.get("/ready")
        assert resp2.status == 200
        data2 = await resp2.json()
        assert data2["chatReady"] is True
        assert data2["dependencies"]["idempotency"] is True

    @pytest.mark.asyncio
    async def test_ready_dev_mode_unaffected(self, _clean_store, aiohttp_client):
        """dev 模式：needs_recovery 不门禁（内存降级允许），仍 200。"""
        from core.input.idempotency import configure_store, get_store
        from core.input.tests.test_idempotency_durable import _FlipRepo
        from core.input.tests.test_idempotency_durable import _entry as _make_entry
        from core.input.server import create_app

        repo = _FlipRepo(available=False)
        configure_store(repo, product_mode=False)
        await get_store().store("default", "crid", _make_entry("msg_1", "abc"))
        repo.available = True
        app = create_app(_make_mock_agent(), serve_static=False,
                         product_mode=False, idempotency_repo=repo)
        client = await aiohttp_client(app)
        resp = await client.get("/ready")
        assert resp.status == 200
        data = await resp.json()
        assert data["chatReady"] is True
