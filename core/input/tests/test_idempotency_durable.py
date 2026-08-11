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

import time
from datetime import datetime, timezone

import pytest

from core.input.idempotency import configure_store, get_store, reset_store
from core.input.rate_limiter import RateLimiter
from core.storage.idempotency_repo import (
    COLLECTION,
    IdempotencyRepository,
    STATUS_ACCEPTED,
    STATUS_CLAIMED,
    STATUS_REJECTED,
    StorageUnavailableError,
)
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


# ---------------------------------------------------------------------------
# Task 1：repo 层 claim 所有权契约（ClaimResult / non-fatal DuplicateKey /
# is_connected / stale 接管 / complete/reject 负载）。marker=service。
# ---------------------------------------------------------------------------

class DuplicateKeyError(RuntimeError):
    """模拟 pymongo.errors.DuplicateKeyError（repo 按异常类名匹配，驱动无关）。"""


class _DuplicateOnceFake(FakeDocumentDBBackend):
    """首 insert 抛 DuplicateKeyError（模拟并发同键双写窗口），随后恢复正常。

    find 在冲突读回阶段返回 winner（模拟并发写者先落库）；其余走真实内存 fake。
    """

    def __init__(self, winner: dict | None = None) -> None:
        super().__init__()
        self._winner = winner
        self._winner_visible = False
        self.insert_attempts = 0

    async def insert_one(self, collection: str, document: dict) -> str:
        self.insert_attempts += 1
        if self.insert_attempts == 1:
            self._winner_visible = True  # 并发窗口：winner 已由「另一写者」落库
            raise DuplicateKeyError("E11000 duplicate key error")
        return await super().insert_one(collection, document)

    async def find(self, collection: str, filter_expr: dict, **kwargs):
        if self._winner_visible and self._winner is not None:
            if (self._winner.get("conversation_id") == filter_expr.get("conversation_id")
                    and self._winner.get("client_request_id") == filter_expr.get("client_request_id")):
                return [dict(self._winner)]
        return await super().find(collection, filter_expr, **kwargs)


class _RecordingUpdateFake(FakeDocumentDBBackend):
    """记录 update_one 调用（$set 内容）供断言 payload/error 已落库。"""

    def __init__(self) -> None:
        super().__init__()
        self.updates: list[tuple[dict, dict, dict]] = []

    async def update_one(self, collection: str, filter_expr: dict,
                         update: dict, upsert: bool = False) -> int:
        self.updates.append((filter_expr, update, {"upsert": upsert}))
        return await super().update_one(collection, filter_expr, update, upsert=upsert)


def _expires() -> datetime:
    """TTL 字段值（BSON Date，aware UTC）。"""
    return datetime.now(timezone.utc)


class TestRepoClaimContract:
    """repo 层 claim 所有权契约（Task 1）。"""

    pytestmark = pytest.mark.service

    @pytest.mark.asyncio
    async def test_claim_returns_acquired_flag_for_new(self):
        """新 claim → acquired=True。"""
        repo = IdempotencyRepository(db=FakeDocumentDBBackend())
        result = await repo.claim("c1", "r1", content_digest="d", message_id="m",
                                  status=STATUS_CLAIMED, claimed_at=time.time(),
                                  expires_at=_expires())
        assert result.acquired is True
        assert result.entry["message_id"] == "m"
        assert result.entry["claimed_at"] > 0  # 墙钟 unix 秒

    @pytest.mark.asyncio
    async def test_claim_loser_acquired_false(self):
        """并发同键 → 后写者 acquired=False 且 entry=winner。"""
        winner = {
            "conversation_id": "c1",
            "client_request_id": "r1",
            "content_digest": "winner-digest",
            "message_id": "winner-msg",
            "status": STATUS_CLAIMED,
            "claimed_at": time.time(),
            "expires_at": _expires(),
        }
        fake = _DuplicateOnceFake(winner=winner)
        repo = IdempotencyRepository(db=fake)
        result = await repo.claim("c1", "r1", content_digest="loser-digest",
                                  message_id="loser-msg", status=STATUS_CLAIMED,
                                  claimed_at=time.time(), expires_at=_expires())
        assert result.acquired is False
        assert result.entry["message_id"] == "winner-msg"
        assert result.entry["content_digest"] == "winner-digest"

    @pytest.mark.asyncio
    async def test_duplicate_key_does_not_invalidate_repo(self):
        """DuplicateKey 业务冲突不判死：health_check 仍 true、下一条可用。"""
        winner = {
            "conversation_id": "c1",
            "client_request_id": "r1",
            "content_digest": "winner-digest",
            "message_id": "winner-msg",
            "status": STATUS_CLAIMED,
            "claimed_at": time.time(),
            "expires_at": _expires(),
        }
        fake = _DuplicateOnceFake(winner=winner)
        repo = IdempotencyRepository(db=fake)
        result = await repo.claim("c1", "r1", content_digest="d1", message_id="m1",
                                  status=STATUS_CLAIMED, claimed_at=time.time(),
                                  expires_at=_expires())
        assert result.acquired is False  # 冲突读回 winner
        assert repo.is_connected is True  # _db 未被置 None（不判死）
        assert await repo.health_check() is True
        # 下一条可用（不同键 → 正常 insert）
        r2 = await repo.claim("c2", "r2", content_digest="d2", message_id="m2",
                              status=STATUS_CLAIMED, claimed_at=time.time(),
                              expires_at=_expires())
        assert r2.acquired is True

    @pytest.mark.asyncio
    async def test_claim_stale_claimed_takeover(self):
        """过期 CLAIMED（claimed_at < now-GRACE_SECONDS）→ 接管：acquired=True 且 timestamps 已延长。"""
        stale = time.time() - 60.0  # 远大于 GRACE_SECONDS（30s）
        fake = _RecordingUpdateFake()
        fake.collections[COLLECTION] = [{
            "conversation_id": "c1",
            "client_request_id": "r1",
            "content_digest": "old",
            "message_id": "old-msg",
            "status": STATUS_CLAIMED,
            "claimed_at": stale,
            "expires_at": None,
        }]
        repo = IdempotencyRepository(db=fake)
        result = await repo.claim("c1", "r1", content_digest="new", message_id="new-msg",
                                  status=STATUS_CLAIMED, claimed_at=time.time(),
                                  expires_at=_expires())
        assert result.acquired is True  # 接管
        assert result.entry["message_id"] == "old-msg"  # 既有记录身份保留
        assert result.entry["claimed_at"] > stale  # timestamps 已延长
        assert fake.updates, "接管须 update_one 延长 claimed_at/expires_at"
        set_expr = fake.updates[0][1]["$set"]
        assert set_expr["claimed_at"] > stale
        assert set_expr["expires_at"] is not None

    @pytest.mark.asyncio
    async def test_claim_stale_in_progress_takeover(self):
        """legacy in_progress 文档视同 CLAIMED：过期 → 接管（生产历史数据）。"""
        stale = time.time() - 60.0
        fake = _RecordingUpdateFake()
        fake.collections[COLLECTION] = [{
            "conversation_id": "c1",
            "client_request_id": "r1",
            "content_digest": "old",
            "message_id": "old-msg",
            "status": "in_progress",  # legacy 状态（D6c 早期文档）
            "claimed_at": stale,
            "expires_at": None,
        }]
        repo = IdempotencyRepository(db=fake)
        result = await repo.claim("c1", "r1", content_digest="new", message_id="new-msg",
                                  status=STATUS_CLAIMED, claimed_at=time.time(),
                                  expires_at=_expires())
        assert result.acquired is True

    @pytest.mark.asyncio
    async def test_claim_fresh_claimed_not_taken_over(self):
        """新鲜 CLAIMED（claimed_at 在 GRACE 内）→ 不接管：acquired=False。"""
        fresh = time.time()
        fake = _RecordingUpdateFake()
        fake.collections[COLLECTION] = [{
            "conversation_id": "c1",
            "client_request_id": "r1",
            "content_digest": "old",
            "message_id": "old-msg",
            "status": STATUS_CLAIMED,
            "claimed_at": fresh,
            "expires_at": None,
        }]
        repo = IdempotencyRepository(db=fake)
        result = await repo.claim("c1", "r1", content_digest="new", message_id="new-msg",
                                  status=STATUS_CLAIMED, claimed_at=time.time(),
                                  expires_at=_expires())
        assert result.acquired is False
        assert fake.updates == []  # 不接管 → 无 update_one

    @pytest.mark.asyncio
    async def test_complete_reject_persist_payload(self):
        """complete() 存 received_at/client_message_id/conversation_id_wire；reject() 存 error。"""
        fake = _RecordingUpdateFake()
        repo = IdempotencyRepository(db=fake)
        await repo.complete(
            "c1", "r1",
            payload={"received_at": "2026-08-11T00:00:00Z",
                     "client_message_id": "cm1",
                     "conversation_id_wire": "wire-1"},
        )
        assert fake.updates
        set1 = fake.updates[0][1]["$set"]
        assert set1["status"] == STATUS_ACCEPTED
        assert set1["received_at"] == "2026-08-11T00:00:00Z"
        assert set1["client_message_id"] == "cm1"
        assert set1["conversation_id_wire"] == "wire-1"

        fake.updates.clear()
        await repo.reject("c1", "r1",
                          error={"code": "CONTRACT_VALIDATION", "message": "digest mismatch"})
        assert fake.updates
        set2 = fake.updates[0][1]["$set"]
        assert set2["status"] == STATUS_REJECTED
        assert set2["error"]["code"] == "CONTRACT_VALIDATION"

    @pytest.mark.asyncio
    async def test_is_connected_reflects_db(self):
        """is_connected：_db 非空 True；close() 后 False。"""
        repo = IdempotencyRepository(db=FakeDocumentDBBackend())
        assert repo.is_connected is True
        await repo.close()
        assert repo.is_connected is False
        assert IdempotencyRepository(db=None).is_connected is False
