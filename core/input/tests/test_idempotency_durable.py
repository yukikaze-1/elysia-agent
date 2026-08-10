"""D6c 幂等存储持久化 — 产品 fail-closed / dev 内存降级 / capabilities 暴露。

覆盖 plan Task D6c：
- 未配置（无 repo）→ 纯内存，语义不变（重放/冲突/删除重入）
- 产品模式 + Mongo 离线 → lookup/store raise StorageUnavailableError（fail-closed）
- dev 模式 + Mongo 离线 → 内存降级（durable=False，capabilities 暴露非 durable）
- 持久化（Mongo 可用）→ claim 落库、durable=True
- ws_handler 产品模式离线 → ACK rejected STORAGE_UNAVAILABLE retryable
- build_server_info 反映 idempotency_durable

Run: python -m pytest core/input/tests/test_idempotency_durable.py -v --asyncio-mode=auto
"""

import pytest

from core.input.idempotency import configure_store, get_store, reset_store
from core.input.rate_limiter import RateLimiter
from core.storage.idempotency_repo import IdempotencyRepository, StorageUnavailableError
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend


class _DownFake(FakeDocumentDBBackend):
    """模拟 Mongo 断连：所有后端操作抛普通异常（_call 转 StorageUnavailableError）。"""

    async def find(self, *a, **kw):
        raise RuntimeError("Mongo down")

    async def insert_one(self, *a, **kw):
        raise RuntimeError("Mongo down")

    async def update_one(self, *a, **kw):
        raise RuntimeError("Mongo down")

    async def delete_many(self, *a, **kw):
        raise RuntimeError("Mongo down")


def _down_repo() -> IdempotencyRepository:
    return IdempotencyRepository(db=_DownFake())


def _up_repo() -> IdempotencyRepository:
    return IdempotencyRepository(db=FakeDocumentDBBackend())


def _entry(message_id: str = "msg_1", digest: str = "abc") -> dict:
    import time
    from core.input.idempotency import IdempotencyEntry
    # created_at 是 monotonic 基准（内存 TTL 依据）——不能用远古固定值，否则立即过期
    return IdempotencyEntry(user_message_id=message_id, content_digest=digest,
                            created_at=time.monotonic())


@pytest.fixture
def fresh_store():
    reset_store()
    yield
    reset_store()


class TestMemoryDefault:
    """未配置（无 repo）→ 纯内存，语义不变。"""

    @pytest.mark.asyncio
    async def test_lookup_miss_then_store_replay(self, fresh_store):
        store = get_store()
        assert await store.lookup("default", "crid") is None
        await store.store("default", "crid", _entry())
        entry = await store.lookup("default", "crid")
        assert entry is not None
        assert entry.user_message_id == "msg_1"

    @pytest.mark.asyncio
    async def test_delete_allows_reentry(self, fresh_store):
        store = get_store()
        await store.store("default", "crid", _entry())
        await store.delete("default", "crid")
        assert await store.lookup("default", "crid") is None

    @pytest.mark.asyncio
    async def test_conversation_scoped(self, fresh_store):
        """同 client_request_id 不同会话 → 独立（对齐 Mongo 唯一约束维度）。"""
        store = get_store()
        await store.store("conv_a", "crid", _entry("m_a"))
        await store.store("conv_b", "crid", _entry("m_b"))
        assert (await store.lookup("conv_a", "crid")).user_message_id == "m_a"
        assert (await store.lookup("conv_b", "crid")).user_message_id == "m_b"


class TestProductFailClosed:
    """产品模式 + Mongo 离线 → lookup/store fail-closed。"""

    @pytest.mark.asyncio
    async def test_lookup_raises_in_product_mode(self, fresh_store):
        configure_store(_down_repo(), product_mode=True)
        with pytest.raises(StorageUnavailableError):
            await get_store().lookup("default", "crid")

    @pytest.mark.asyncio
    async def test_store_raises_in_product_mode(self, fresh_store):
        configure_store(_down_repo(), product_mode=True)
        with pytest.raises(StorageUnavailableError):
            await get_store().store("default", "crid", _entry())


class TestDevDegradation:
    """dev 模式 + Mongo 离线 → 内存降级，durable=False（capabilities 暴露）。"""

    @pytest.mark.asyncio
    async def test_falls_back_to_memory(self, fresh_store):
        configure_store(_down_repo(), product_mode=False)
        store = get_store()
        await store.store("default", "crid", _entry())
        entry = await store.lookup("default", "crid")
        assert entry is not None and entry.user_message_id == "msg_1"
        assert store.durable is False  # 非 durable 窗口

    @pytest.mark.asyncio
    async def test_durable_false_when_no_repo(self, fresh_store):
        assert get_store().durable is False


class TestDurable:
    """Mongo 可用 → claim 落库，durable=True。"""

    @pytest.mark.asyncio
    async def test_store_persists_to_repo(self, fresh_store):
        configure_store(_up_repo(), product_mode=True)
        store = get_store()
        await store.store("default", "crid", _entry())
        assert store.durable is True
        # 落库：repo 直接可查（模拟跨重启恢复）
        doc = await store._repo.get("default", "crid")  # noqa: SLF001
        assert doc["message_id"] == "msg_1"

    @pytest.mark.asyncio
    async def test_lookup_from_repo_after_restart(self, fresh_store):
        repo = _up_repo()  # 同一 repo 实例——重启后仍持记录（模拟持久化）
        configure_store(repo, product_mode=True)
        store = get_store()
        await store.store("default", "crid", _entry("msg_orig"))
        # 模拟重启：store 内存被清（reset_store），但 repo 的 Mongo 记录仍在
        reset_store()
        configure_store(repo, product_mode=True)
        entry = await get_store().lookup("default", "crid")
        assert entry is not None and entry.user_message_id == "msg_orig"


class TestWsHandlerProductMode:
    """ws_handler：产品模式 + Mongo 离线 → ACK rejected STORAGE_UNAVAILABLE retryable。"""

    @pytest.mark.asyncio
    async def test_text_ack_storage_unavailable(self, fresh_store):
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeAgent, FakeWs

        configure_store(_down_repo(), product_mode=True)
        ws = FakeWs()
        agent = FakeAgent(ws)
        agent.bus = type("Bus", (), {"post": lambda self, d: None})()
        await handle_json_message(
            {"type": "text", "content": "你好", "clientRequestId": "r1"},
            agent, None, ws, voice_mgr=None)
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks, f"无 ACK: {ws.sent}"
        assert acks[-1]["status"] == "rejected"
        assert acks[-1]["error"]["code"] == "STORAGE_UNAVAILABLE"
        assert acks[-1]["error"]["retryable"] is True


class TestServerInfoExposure:
    """build_server_info.features.idempotency_durable 反映实际持久化能力。"""

    @pytest.mark.asyncio
    async def test_features_reflect_durability(self, fresh_store):
        from core.input.capabilities import build_server_info
        from core.input.tests.test_server import _make_agent

        agent = _make_agent()
        await agent.start()
        try:
            # 未配置 → durable=False
            info = build_server_info(agent)
            assert info.features.idempotency_durable is False
            # 配置持久化 repo → durable=True
            configure_store(_up_repo(), product_mode=True)
            info2 = build_server_info(agent)
            assert info2.features.idempotency_durable is True
        finally:
            await agent.stop()
