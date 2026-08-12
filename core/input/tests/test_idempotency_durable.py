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

import asyncio
import time
from datetime import datetime, timezone

import pytest

from core.input.idempotency import (
    _IN_PROGRESS_RETRY_AFTER_MS,
    IdempotencyEntry,
    StoreResult,
    configure_store,
    get_store,
    reset_store,
)
from core.input.rate_limiter import RateLimiter
from core.input.voice_sessions import VoiceSessionError
from core.storage.idempotency_repo import (
    COLLECTION,
    ClaimResult,
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

        configure_store(_FlipRepo(available=False), product_mode=True)
        ws = FakeWs()
        agent = FakeAgent(ws)
        agent.bus = type("Bus", (), {"post": lambda d: None})()
        await handle_json_message(
            {"type": "text", "content": "你好", "clientRequestId": "r1"},
            agent, None, ws, voice_mgr=None)
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks, f"无 ACK: {ws.sent}"
        assert acks[-1]["status"] == "rejected"
        assert acks[-1]["error"]["code"] == "STORAGE_UNAVAILABLE"
        assert acks[-1]["error"]["retryable"] is True
        # D3：STORAGE_UNAVAILABLE rejected ACK 的 details 携带当前 idempotency_durable
        assert acks[-1]["error"]["details"]["idempotency_durable"] is get_store().durable
        assert get_store().durable is False


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


class _RacingTakeoverFake(FakeDocumentDBBackend):
    """stale 接管竞态 fake：update_one 加 barrier，强制两个 claim 都先读到 stale 快照
    （find 不设障——此时尚无人 apply），再同时进入条件 update_one——只有先写者
    modified_count>0，后写者因 claimed_at 已刷新而失配。用于并发接管回归测试。"""

    def __init__(self, stale_doc: dict) -> None:
        super().__init__()
        self.collections[COLLECTION] = [dict(stale_doc)]
        self._update_barrier = asyncio.Event()
        self._update_count = 0

    async def update_one(self, collection: str, filter_expr: dict,
                         update: dict, upsert: bool = False) -> int:
        self._update_count += 1
        if self._update_count == 1:
            # update_one 汇合点：等第二个 claim 也到达再 apply（此时文档仍为 stale，
            # 两方都持有 stale 快照——真正的竞态窗口）
            await self._update_barrier.wait()
        elif self._update_count == 2:
            self._update_barrier.set()
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
        repo._injected_db = False  # noqa: SLF001  # 强制走非注入路径——若 non_fatal 短路缺失，
        # _call 会 _mark_unavailable 把 _db 置 None，以下断言即失败（钉死生产不判死保证）
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
    async def test_concurrent_stale_takeover_single_winner(self):
        """并发接管竞态：两 claim 同读同一 stale CLAIMED → 恒恰一个 acquired=True。

        条件接管 update_one（filter 含 stale 状态）保证只有先写者 modified_count>0；
        后写者因 claimed_at 已刷新而失配，重新读回 winner 得 acquired=False——不得
        双双进入业务处理（D6c 失败模式回归）。
        """
        stale = time.time() - 60.0
        stale_doc = {
            "conversation_id": "c1",
            "client_request_id": "r1",
            "content_digest": "old",
            "message_id": "old-msg",
            "status": STATUS_CLAIMED,
            "claimed_at": stale,
            "expires_at": None,
        }
        fake = _RacingTakeoverFake(stale_doc=stale_doc)
        repo = IdempotencyRepository(db=fake)
        r1, r2 = await asyncio.gather(
            repo.claim("c1", "r1", content_digest="new1", message_id="new1-msg",
                       status=STATUS_CLAIMED, claimed_at=time.time(),
                       expires_at=_expires()),
            repo.claim("c1", "r1", content_digest="new2", message_id="new2-msg",
                       status=STATUS_CLAIMED, claimed_at=time.time(),
                       expires_at=_expires()),
        )
        acquired = [r.acquired for r in (r1, r2)]
        assert sum(acquired) == 1, f"须恰一个接管者，got acquired={acquired}"
        loser = r1 if r1.acquired is False else r2
        assert loser.acquired is False
        assert loser.entry["claimed_at"] > stale  # 输家读回的是 winner 更新后的新鲜记录

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

        # payload 不得覆盖显式 status（显式 status 最后写入恒优先）
        fake.updates.clear()
        await repo.complete("c1", "r1", payload={"status": STATUS_REJECTED, "received_at": "x"})
        assert fake.updates
        set1b = fake.updates[0][1]["$set"]
        assert set1b["status"] == STATUS_ACCEPTED  # 显式 status 恒优先
        assert set1b["received_at"] == "x"  # 其余负载字段正常落库

        fake.updates.clear()
        await repo.reject("c1", "r1",
                          error={"code": "CONTRACT_VALIDATION", "message": "digest mismatch"})
        assert fake.updates
        set2 = fake.updates[0][1]["$set"]
        assert set2["status"] == STATUS_REJECTED
        assert set2["error"]["code"] == "CONTRACT_VALIDATION"

        # reject(payload=...) 落完整 ACK 负载；显式 status/error 恒优先（payload 不得覆盖）
        fake.updates.clear()
        await repo.reject("c1", "r1",
                          error={"code": "X", "message": "m", "retryable": False},
                          payload={"status": STATUS_ACCEPTED, "error": {"code": "hack"},
                                   "received_at": "2026-08-11T00:00:00Z",
                                   "client_message_id": "cm1",
                                   "conversation_id_wire": "w"})
        assert fake.updates
        set3 = fake.updates[0][1]["$set"]
        assert set3["status"] == STATUS_REJECTED        # 显式 status 恒优先
        assert set3["error"]["code"] == "X"              # 显式 error 恒优先
        assert set3["received_at"] == "2026-08-11T00:00:00Z"
        assert set3["client_message_id"] == "cm1"
        assert set3["conversation_id_wire"] == "w"

    @pytest.mark.asyncio
    async def test_is_connected_reflects_db(self):
        """is_connected：_db 非空 True；close() 后 False。"""
        repo = IdempotencyRepository(db=FakeDocumentDBBackend())
        assert repo.is_connected is True
        await repo.close()
        assert repo.is_connected is False
        assert IdempotencyRepository(db=None).is_connected is False


# ---------------------------------------------------------------------------
# Task 2：store 层内存权威 overlay（StoreResult / 内存优先 / recover reconcile /
# 完整 ACK 负载 / durable 初值）。marker=service。
# ---------------------------------------------------------------------------

class _FlipRepo:
    """可用性可翻转的幂等 repo fake（Task 2 store 层用例）。

    available=True → 委托真实 IdempotencyRepository（内存 FakeDocumentDBBackend）；
    False → 所有操作 raise StorageUnavailableError（模拟 Mongo 断连）。翻转可用性
    可复现 up→down→up / 离线启动→恢复 等 Mongo 状态切换。
    """

    def __init__(self, available: bool = True) -> None:
        self.available = available
        self._backend = FakeDocumentDBBackend()
        self._impl = IdempotencyRepository(db=self._backend)

    @property
    def is_connected(self) -> bool:
        return self.available

    async def health_check(self) -> bool:
        return self.available

    async def claim(self, conversation_id: str, client_request_id: str, *,
                    content_digest: str, message_id: str, status: str,
                    claimed_at: float | None = None,
                    expires_at: object) -> ClaimResult:
        if not self.available:
            raise StorageUnavailableError("Mongo down")
        return await self._impl.claim(
            conversation_id, client_request_id,
            content_digest=content_digest, message_id=message_id, status=status,
            claimed_at=claimed_at, expires_at=expires_at)

    async def get(self, conversation_id: str, client_request_id: str) -> dict | None:
        if not self.available:
            raise StorageUnavailableError("Mongo down")
        return await self._impl.get(conversation_id, client_request_id)

    async def complete(self, conversation_id: str, client_request_id: str, *,
                       status: str = STATUS_ACCEPTED,
                       payload: dict | None = None) -> None:
        if not self.available:
            raise StorageUnavailableError("Mongo down")
        await self._impl.complete(conversation_id, client_request_id,
                                  status=status, payload=payload)

    async def reject(self, conversation_id: str, client_request_id: str, *,
                     error: dict, payload: dict | None = None) -> None:
        if not self.available:
            raise StorageUnavailableError("Mongo down")
        await self._impl.reject(conversation_id, client_request_id,
                                error=error, payload=payload)

    async def release(self, conversation_id: str, client_request_id: str) -> None:
        if not self.available:
            raise StorageUnavailableError("Mongo down")
        await self._impl.release(conversation_id, client_request_id)


class TestStoreOverlay:
    """内存权威 overlay（D5）：内存优先 / 写穿 / 降级去重 / recover reconcile。"""

    pytestmark = pytest.mark.service

    @pytest.mark.asyncio
    async def test_lookup_memory_first_even_when_mongo_ok(self, fresh_store):
        """内存有 entry 时 Mongo 在线也命中内存（不覆盖内存事实）。"""
        repo = _FlipRepo()
        configure_store(repo, product_mode=True)
        store = get_store()
        await store.store("default", "crid", _entry("msg_1", "abc"))
        # Mongo 状态被外部改写为 accepted——内存事实（claimed）不应被覆盖
        await repo.complete("default", "crid", payload={"received_at": "x"})
        entry = await store.lookup("default", "crid")
        assert entry is not None
        assert entry.user_message_id == "msg_1"
        assert entry.status == STATUS_CLAIMED  # 内存权威（未被 Mongo 覆盖）

    @pytest.mark.asyncio
    async def test_store_returns_store_result_and_writes_shadow(self, fresh_store):
        """store 成功写穿 Mongo 也写内存；返回 StoreResult；durable=True。"""
        repo = _FlipRepo()
        configure_store(repo, product_mode=True)
        store = get_store()
        result = await store.store("default", "crid", _entry("msg_1", "abc"))
        assert isinstance(result, StoreResult)
        assert result.acquired is True
        assert result.entry.user_message_id == "msg_1"
        assert store.durable is True
        # 写穿 Mongo + 内存镜像双写
        assert (await repo.get("default", "crid"))["message_id"] == "msg_1"
        assert store._entries[("default", "crid")].user_message_id == "msg_1"  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_failover_up_down_up_no_dedup_loss(self, fresh_store):
        """up→down→up：同 clientRequestId 重试恒同一 entry（process 不重复）。"""
        repo = _FlipRepo()
        configure_store(repo, product_mode=False)
        store = get_store()
        r1 = await store.store("default", "crid", _entry("msg_1", "abc"))
        assert r1.acquired is True
        repo.available = False  # Mongo down（dev 内存降级）
        r2 = await store.store("default", "crid", _entry("msg_2", "abc"))
        assert r2.acquired is False
        repo.available = True   # Mongo up（写穿 claim → 既有记录 winner）
        r3 = await store.store("default", "crid", _entry("msg_3", "abc"))
        assert r3.acquired is False
        # 恒同一 entry：message_id 不漂移（process 不重复生成）
        assert r1.entry.user_message_id == "msg_1"
        assert r2.entry.user_message_id == "msg_1"
        assert r3.entry.user_message_id == "msg_1"

    @pytest.mark.asyncio
    async def test_down_up_before_recover_no_dedup_loss(self, fresh_store):
        """down→up 且 recover() 未跑：同键 store() 走内存短路返回 (entry, False)，
        不新生成 message_id / 不二次 acquired（review Important#2 回归）。"""
        repo = _FlipRepo()
        configure_store(repo, product_mode=False)
        store = get_store()
        repo.available = False  # Mongo down → dev 降级 claim（仅内存）
        r1 = await store.store("default", "crid", _entry("msg_1", "abc"))
        assert r1.acquired is True
        assert store.durable is False  # 进入内存降级窗口（_in_memory_fallback=True）
        repo.available = True   # Mongo up，但 recover() 尚未调用
        # 同键 store()：内存短路 → 返回既有 entry，不触碰 Mongo（不重复处理）
        r2 = await store.store("default", "crid", _entry("msg_2", "abc"))
        assert r2.acquired is False
        assert r2.entry.user_message_id == "msg_1"  # 既有 entry，不新生成 message_id
        # 内存 claim 从未落 Mongo（短路径不写库）→ 无第二条 doc
        assert (await repo.get("default", "crid")) is None
        # 恢复后仍可去重（后续 recover 或再 store 仍返回既有）
        r3 = await store.store("default", "crid", _entry("msg_3", "abc"))
        assert r3.acquired is False
        assert r3.entry.user_message_id == "msg_1"

    @pytest.mark.asyncio
    async def test_recover_reconciles_memory_to_mongo_and_durable(self, fresh_store):
        """recover()：Mongo 已有→内存刷新为其状态；Mongo 无→插入内存 claim；
        结束后 _in_memory_fallback=False → durable=True。"""
        repo = _FlipRepo()
        # Mongo 已有 crid_mongo（另一进程已 claim + complete → accepted）
        await repo.claim("default", "crid_mongo", content_digest="mongo-d",
                         message_id="msg_mongo", status=STATUS_ACCEPTED,
                         claimed_at=time.time(), expires_at=_expires())
        configure_store(repo, product_mode=False)
        store = get_store()
        # dev 降级窗口：两条内存 entry（Mongo 不可写）
        repo.available = False
        await store.store("default", "crid_dev", _entry("msg_dev", "d1"))
        await store.store("default", "crid_mongo", _entry("msg_mem", "d2"))
        assert store.durable is False
        # Mongo 恢复 → recover reconcile
        repo.available = True
        await store.recover()
        # Mongo 已有 → 内存刷新为 Mongo 状态（Mongo 优先、读时一致）
        mem_mongo = store._entries[("default", "crid_mongo")]  # noqa: SLF001
        assert mem_mongo.user_message_id == "msg_mongo"
        assert mem_mongo.status == STATUS_ACCEPTED
        # Mongo 无 → 插入内存 claim（内存权威补落库）
        assert store._entries[("default", "crid_dev")].user_message_id == "msg_dev"  # noqa: SLF001
        assert (await repo.get("default", "crid_dev"))["message_id"] == "msg_dev"
        # 操作级已恢复（_in_memory_fallback 翻 False）；但 needs_recovery 仍 True——
        # durable 强化下不因 recover 翻 True（本批次 Task 3 的 recover 清位后才翻回）
        assert store._in_memory_fallback is False  # noqa: SLF001
        assert store.needs_recovery is True
        assert store.durable is False

    def test_durable_false_before_connect(self, fresh_store):
        """repo 未连接（is_connected=False）→ configure 后 durable=False（初值不误报）。"""
        repo = _FlipRepo(available=False)  # 未连接
        configure_store(repo, product_mode=False)
        assert get_store().durable is False
        # 纯内存（repo=None）同样 durable=False
        reset_store()
        assert get_store().durable is False

    @pytest.mark.asyncio
    async def test_complete_reject_persist_full_ack_payload(self, fresh_store):
        """complete/reject 持久化 received_at+client_message_id+conversation_id_wire
        (+error)；重放可逐字重建。"""
        repo = _FlipRepo()
        configure_store(repo, product_mode=False)
        store = get_store()
        await store.store("default", "crid", _entry("msg_1", "abc"))

        await store.complete(
            "default", "crid",
            received_at="2026-08-11T00:00:00Z",
            client_message_id="cm1", conversation_id_wire="wire-1")
        mem = store._entries[("default", "crid")]  # noqa: SLF001
        assert mem.status == STATUS_ACCEPTED
        assert mem.received_at == "2026-08-11T00:00:00Z"
        assert mem.client_message_id == "cm1"
        assert mem.conversation_id_wire == "wire-1"
        doc = await repo.get("default", "crid")
        assert doc["status"] == STATUS_ACCEPTED
        assert doc["received_at"] == "2026-08-11T00:00:00Z"
        assert doc["client_message_id"] == "cm1"
        assert doc["conversation_id_wire"] == "wire-1"

        await store.reject(
            "default", "crid",
            code="CONTRACT_VALIDATION", message="digest mismatch", retryable=False,
            received_at="2026-08-11T00:00:00Z",
            client_message_id="cm1", conversation_id_wire="wire-1",
            details={"k": "v"}, retry_after_ms=5000)
        mem2 = store._entries[("default", "crid")]  # noqa: SLF001
        assert mem2.status == STATUS_REJECTED
        assert mem2.error == {
            "code": "CONTRACT_VALIDATION", "message": "digest mismatch",
            "retryable": False, "details": {"k": "v"}, "retryAfterMs": 5000}
        assert mem2.received_at == "2026-08-11T00:00:00Z"
        doc2 = await repo.get("default", "crid")
        assert doc2["status"] == STATUS_REJECTED
        assert doc2["error"]["code"] == "CONTRACT_VALIDATION"
        assert doc2["error"]["retryAfterMs"] == 5000
        # rejected 完整负载也经 repo.reject(payload=...) 落 Mongo（重放逐字一致，spec §1.1）
        assert doc2["received_at"] == "2026-08-11T00:00:00Z"
        assert doc2["client_message_id"] == "cm1"
        assert doc2["conversation_id_wire"] == "wire-1"

    @pytest.mark.asyncio
    async def test_pure_memory_path_keeps_memory_first_dedup(self, fresh_store):
        """repo=None：store 同键二次返回既有 entry（acquired=False），不生成新 message_id。"""
        store = get_store()  # 未配置 → repo=None 纯内存
        r1 = await store.store("default", "crid", _entry("msg_1", "abc"))
        r2 = await store.store("default", "crid", _entry("msg_2", "abc"))
        assert r1.acquired is True
        assert r1.entry.user_message_id == "msg_1"
        assert r2.acquired is False
        assert r2.entry.user_message_id == "msg_1"  # 既有 entry（去重，不新生成）
        assert (await store.lookup("default", "crid")).user_message_id == "msg_1"


class TestNeedsRecovery:
    """BE-P1-07 store 状态：_needs_recovery 显式标志 + durable 强化（S2 修复）。"""

    pytestmark = pytest.mark.service

    @pytest.mark.asyncio
    async def test_degraded_write_sets_needs_recovery(self, fresh_store):
        """dev 降级写内存 → _needs_recovery=True、durable=False。"""
        repo = _FlipRepo(available=False)
        configure_store(repo, product_mode=False)
        store = get_store()
        await store.store("default", "crid", _entry("msg_1", "abc"))
        assert store.needs_recovery is True
        assert store.durable is False
        assert store.configured is True

    @pytest.mark.asyncio
    async def test_unrelated_success_does_not_clear_needs_recovery(self, fresh_store):
        """S2：降级记录后另一 key 的 store() 成功只清 fallback、不清 needs_recovery →
        durable 仍 False（无关操作不静默清零）。"""
        repo = _FlipRepo(available=False)
        configure_store(repo, product_mode=False)
        store = get_store()
        await store.store("default", "crid_1", _entry("msg_1", "abc"))  # 降级
        assert store.needs_recovery is True
        repo.available = True  # Mongo 恢复
        # 另一 key 的 store() 成功 → repo.claim 成功 → fallback=False，但 needs_recovery 保持
        result = await store.store("default", "crid_2", _entry("msg_2", "abc"))
        assert result.acquired is True
        assert store._in_memory_fallback is False  # noqa: SLF001  # 操作级已恢复
        assert store.needs_recovery is True        # 未 reconcile 记录仍在
        assert store.durable is False              # durable 强化：不因无关操作翻 True

    @pytest.mark.asyncio
    async def test_complete_write_through_failure_sets_needs_recovery(self, fresh_store):
        """complete() 写穿失败（best-effort 吞错）→ _needs_recovery=True（内存终态超前 Mongo）。"""
        repo = _FlipRepo()
        configure_store(repo, product_mode=False)
        store = get_store()
        await store.store("default", "crid", _entry("msg_1", "abc"))  # Mongo=claimed
        repo.available = False
        await store.complete("default", "crid", received_at="2026-08-12T00:00:00Z",
                             client_message_id="cm1", conversation_id_wire="wire-1")
        assert store.needs_recovery is True
        assert store.durable is False
        # 内存仍为 accepted（写穿失败不丢内存）
        mem = store._entries[("default", "crid")]  # noqa: SLF001
        assert mem.status == STATUS_ACCEPTED
        assert mem.received_at == "2026-08-12T00:00:00Z"

    @pytest.mark.asyncio
    async def test_reject_write_through_failure_sets_needs_recovery(self, fresh_store):
        """reject() 写穿失败（best-effort 吞错）→ _needs_recovery=True（内存 rejected 超前 Mongo）。"""
        repo = _FlipRepo()
        configure_store(repo, product_mode=False)
        store = get_store()
        await store.store("default", "crid", _entry("msg_1", "abc"))  # Mongo=claimed
        repo.available = False
        await store.reject("default", "crid", code="CONTRACT_VALIDATION",
                           message="digest mismatch", retryable=False,
                           received_at="2026-08-12T00:00:00Z",
                           client_message_id="cm1", conversation_id_wire="wire-1")
        assert store.needs_recovery is True
        assert store.durable is False
        mem = store._entries[("default", "crid")]  # noqa: SLF001
        assert mem.status == STATUS_REJECTED
        assert mem.error["code"] == "CONTRACT_VALIDATION"
        # Mongo 仍 claimed（写穿失败未落库）→ 需 recover 升级
        repo.available = True  # 最小修正：_FlipRepo.get 在 available=False 时抛 StorageUnavailableError
        doc = await repo.get("default", "crid")
        assert doc["status"] == STATUS_CLAIMED

    @pytest.mark.asyncio
    async def test_reset_clears_needs_recovery(self, fresh_store):
        """reset() 清空 _needs_recovery（测试隔离，防顺序污染）。"""
        repo = _FlipRepo(available=False)
        configure_store(repo, product_mode=False)
        await get_store().store("default", "crid", _entry("msg_1", "abc"))
        assert get_store().needs_recovery is True
        reset_store()
        assert get_store().needs_recovery is False
        assert get_store().durable is False  # repo=None → durable False

    @pytest.mark.asyncio
    async def test_configured_property(self, fresh_store):
        """configured：repo 非 None True；repo=None False。"""
        assert get_store().configured is False
        configure_store(_FlipRepo(), product_mode=False)
        assert get_store().configured is True


# ---------------------------------------------------------------------------
# Task 4：voice.commit 顺序修正（spec §1.1 BE-P0-01#4）+ D3 durable 详情。marker=service。
# ---------------------------------------------------------------------------


class _FakeGenRegistry:
    """find_by_user_message 证据 fake（stale CLAIMED 恢复判定；None = 无证据）。"""

    def __init__(self, doc=None):
        self.doc = doc
        self.calls: list[str] = []

    async def find_by_user_message(self, user_message_id):
        self.calls.append(str(user_message_id))
        return self.doc


class _FakeVoiceMgr:
    """可控 voice.commit 的 fake：commit_recording 可抛错、记录调用（防「不消费录音」断言）。"""

    def __init__(self, commit_error: VoiceSessionError | None = None):
        self.commit_error = commit_error
        self.commit_calls: list[tuple[str, str]] = []

    def commit_recording(self, recording_id: str, text: str) -> tuple[str, str, list[str]]:
        self.commit_calls.append((recording_id, text))
        if self.commit_error is not None:
            raise self.commit_error
        return text, "", []


class _CountingSessionMgr:
    """记录 reserve_work/release_work 调用次数（voice 顺序/泄漏断言）。"""

    def __init__(self, known: set | None = None):
        self._known = set(known or {"default", "conv_a"})
        self.reserve_calls = 0
        self.release_calls = 0

    async def has_session(self, sid) -> bool:
        return str(sid) in self._known

    async def resolve(self, conversation_id, protocol_version):
        return type("S", (), {"session_id": str(conversation_id or "default")})()

    async def ensure_loaded(self, sid):
        return type("S", (), {"session_id": str(sid)})()

    async def reserve_work(self, conversation_id, protocol_version):
        from core.memory.session_manager import SessionNotFoundError
        self.reserve_calls += 1
        sid = str(conversation_id or "default")
        if sid not in self._known:
            raise SessionNotFoundError(sid)
        return type("S", (), {"session_id": sid})()

    def release_work(self, sid):
        self.release_calls += 1
        return None


class _VoiceFakeAgent:
    """voice.commit 的 fake agent（输出 v2 channel + 计数 session manager + 记录 bus）。"""

    def __init__(self, ws, *, session_manager: _CountingSessionMgr | None = None,
                 gen_registry: _FakeGenRegistry | None = None):
        from Module.output import WebSocketChannel
        self.output = type("O", (), {
            "_channels": [WebSocketChannel(ws, protocol_version=2)]})()
        self.user_name = "用户"
        self._rid = 0
        self.idle = type("Idle", (), {"touch": lambda self: None})()
        self.bus_posts = []
        bus_posts = self.bus_posts
        self.bus = type("Bus", (), {
            "post": lambda self, d: bus_posts.append(d)})()
        self.session_manager = session_manager or _CountingSessionMgr()
        self._gen_registry = gen_registry
        self.stt_enabled = True

    @property
    def generation_registry(self):
        return self._gen_registry

    def next_request_id(self):
        self._rid += 1
        return f"req_{self._rid}"


def _voice_commit_msg(client_request_id: str, text: str = "你好") -> dict:
    return {"type": "voice.commit", "recordingId": "rec_1", "text": text,
            "clientRequestId": client_request_id, "conversationId": None}


def _voice_digest(text: str = "你好") -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TestVoiceCommitOrder:
    """voice.commit：claim 前 reserve_work；acquired=False/claim 失败 → release_work +
    对应 ACK（不消费录音）；commit_recording 失败 → reject + release_work；TAKEOVER
    保持既有 reserve_work 从 commit_recording 续跑（不重复 reserve）。"""

    pytestmark = pytest.mark.service

    @pytest.mark.asyncio
    async def test_claim_failure_releases_work_no_consume(self, fresh_store):
        """claim 失败（产品模式 fail-closed）→ 先 reserve_work、后 release_work；
        不调 commit_recording（不消费录音）。"""
        from core.input.idempotency import get_store
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs
        from core.storage.idempotency_repo import IdempotencyRepository

        class _ClaimFailFake(FakeDocumentDBBackend):
            async def find(self, *a, **kw):
                return []  # lookup miss（get 走 find）
            async def insert_one(self, *a, **kw):
                raise RuntimeError("Mongo down")  # claim 写穿失败 → StorageUnavailableError

        configure_store(IdempotencyRepository(db=_ClaimFailFake()), product_mode=True)
        store = get_store()
        ws = FakeWs()
        mgr = _CountingSessionMgr()
        agent = _VoiceFakeAgent(ws, session_manager=mgr)
        vmg = _FakeVoiceMgr()
        await handle_json_message(_voice_commit_msg("vc1"), agent, None, ws, voice_mgr=vmg)
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks and acks[-1]["status"] == "rejected", f"实际 {ws.sent}"
        assert acks[-1]["error"]["code"] == "STORAGE_UNAVAILABLE"
        assert mgr.reserve_calls == 1 and mgr.release_calls == 1, \
            "claim 前 reserve、claim 失败后 release（不泄漏 work）"
        assert vmg.commit_calls == [], "claim 失败不消费录音"

    @pytest.mark.asyncio
    async def test_acquired_false_releases_work_no_consume(self, fresh_store):
        """并发 loser（store 返回 acquired=False）→ release_work + IN_PROGRESS 拒绝，
        不消费录音（claim 未获取不进入 commit）。"""
        import core.input.ws_handler as _m
        from core.input.idempotency import IdempotencyEntry, IdempotencyStore, StoreResult
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs

        class _LoserStore(IdempotencyStore):
            """lookup 恒 miss；store 返回既有新鲜 CLAIMED winner（acquired=False）。"""

            def __init__(self, winner: IdempotencyEntry):
                super().__init__()
                self.winner = winner

            async def lookup(self, conversation_id, client_request_id):
                return None

            async def store(self, conversation_id, client_request_id, entry):
                return StoreResult(entry=self.winner, acquired=False)

        winner = IdempotencyEntry(
            user_message_id="msg_winner", content_digest=_voice_digest(),
            created_at=time.monotonic(), conversation_id="default")
        original = _m.get_store
        _m.get_store = lambda: _LoserStore(winner)
        try:
            ws = FakeWs()
            mgr = _CountingSessionMgr()
            agent = _VoiceFakeAgent(ws, session_manager=mgr)
            vmg = _FakeVoiceMgr()
            await handle_json_message(_voice_commit_msg("vc2"), agent, None, ws, voice_mgr=vmg)
        finally:
            _m.get_store = original
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks and acks[-1]["status"] == "rejected", f"实际 {ws.sent}"
        assert acks[-1]["error"]["code"] == "IN_PROGRESS"
        assert acks[-1]["error"]["retryable"] is True
        assert acks[-1]["error"]["retryAfterMs"] == _IN_PROGRESS_RETRY_AFTER_MS == 30000
        assert mgr.reserve_calls == 1 and mgr.release_calls == 1
        assert vmg.commit_calls == [], "loser 不消费录音"

    @pytest.mark.asyncio
    async def test_recording_failure_rejects_and_releases(self, fresh_store):
        """commit_recording 抛 VoiceSessionError → store.reject + release_work + rejected ACK。"""
        from core.input.idempotency import get_store
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs
        from core.input.voice_sessions import VoiceSessionError
        from core.storage.idempotency_repo import STATUS_REJECTED

        ws = FakeWs()
        mgr = _CountingSessionMgr()
        agent = _VoiceFakeAgent(ws, session_manager=mgr)
        vmg = _FakeVoiceMgr(commit_error=VoiceSessionError(
            "VOICE_RECORDING_NOT_FOUND", "无待提交录音"))
        await handle_json_message(_voice_commit_msg("vc3"), agent, None, ws, voice_mgr=vmg)
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks and acks[-1]["status"] == "rejected", f"实际 {ws.sent}"
        assert acks[-1]["error"]["code"] == "VOICE_RECORDING_NOT_FOUND"
        assert mgr.reserve_calls == 1 and mgr.release_calls == 1
        assert vmg.commit_calls == [("rec_1", "你好")], "录音已被消费（进入 commit）"
        entry = await get_store().lookup("default", "vc3")
        assert entry is not None and entry.status == STATUS_REJECTED
        assert entry.error["code"] == "VOICE_RECORDING_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_takeover_keeps_existing_reserve_work(self, fresh_store):
        """TAKEOVER（stale CLAIMED + 无证据，经 store 路径）→ 保持既有 reserve_work，
        从 commit_recording 续跑（不重复 reserve、不释放 work）。"""
        import core.input.ws_handler as _m
        from core.input.idempotency import IdempotencyEntry, IdempotencyStore, StoreResult
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs

        class _TakeoverStore(IdempotencyStore):
            """首个 store 返回 stale CLAIMED loser；二次 store 接管返回 acquired=True。"""

            def __init__(self, stale: IdempotencyEntry):
                super().__init__()
                self.stale = stale
                self.store_calls = 0

            async def lookup(self, conversation_id, client_request_id):
                return None

            async def store(self, conversation_id, client_request_id, entry):
                self.store_calls += 1
                if self.store_calls == 1:
                    return StoreResult(entry=self.stale, acquired=False)
                return StoreResult(entry=self.stale, acquired=True)

        stale = IdempotencyEntry(
            user_message_id="msg_voice", content_digest=_voice_digest(),
            created_at=time.monotonic(), conversation_id="default",
            claimed_at=time.time() - 60.0)  # 过期 CLAIMED
        fake_store = _TakeoverStore(stale)
        original = _m.get_store
        _m.get_store = lambda: fake_store
        try:
            ws = FakeWs()
            mgr = _CountingSessionMgr()
            agent = _VoiceFakeAgent(ws, session_manager=mgr,
                                    gen_registry=_FakeGenRegistry(doc=None))  # 无证据
            vmg = _FakeVoiceMgr()
            await handle_json_message(_voice_commit_msg("vc4"), agent, None, ws, voice_mgr=vmg)
        finally:
            _m.get_store = original
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks and acks[-1]["status"] == "accepted", f"实际 {ws.sent}"
        assert acks[-1]["messageId"] == "msg_voice", "接管沿用原 messageId"
        assert mgr.reserve_calls == 1, "TAKEOVER 不重复 reserve"
        assert mgr.release_calls == 0, "TAKEOVER 保持既有 work，不释放"
        assert fake_store.store_calls == 2, "接管 = 二次 claim"
        assert vmg.commit_calls == [("rec_1", "你好")], "从 commit_recording 续跑"
        assert len(agent.bus_posts) == 1, "续跑后正常入队"

    @pytest.mark.asyncio
    async def test_takeover_reclaim_storage_unavailable_releases_work(self, fresh_store):
        """TAKEOVER 二次 claim 抛 StorageUnavailableError → release_work + rejected
        STORAGE_UNAVAILABLE（不泄漏会话 busy 计数、不消费录音）。"""
        import core.input.ws_handler as _m
        from core.input.idempotency import IdempotencyEntry, IdempotencyStore, StoreResult, get_store
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs
        from core.storage.attachment_repo import StorageUnavailableError

        class _ReclaimFailStore(IdempotencyStore):
            """lookup 恒 miss；store 1 → stale CLAIMED loser；store 2 → raise。"""

            def __init__(self, stale: IdempotencyEntry):
                super().__init__()
                self.stale = stale
                self.store_calls = 0

            async def lookup(self, conversation_id, client_request_id):
                return None

            async def store(self, conversation_id, client_request_id, entry):
                self.store_calls += 1
                if self.store_calls == 1:
                    return StoreResult(entry=self.stale, acquired=False)
                raise StorageUnavailableError("Mongo down")

        stale = IdempotencyEntry(
            user_message_id="msg_voice_su", content_digest=_voice_digest(),
            created_at=time.monotonic(), conversation_id="default",
            claimed_at=time.time() - 60.0)  # 过期 CLAIMED
        reclaim_fail = _ReclaimFailStore(stale)  # 单实例——get_store() 恒返回同一 fake
        original = _m.get_store
        _m.get_store = lambda: reclaim_fail
        try:
            ws = FakeWs()
            mgr = _CountingSessionMgr()
            agent = _VoiceFakeAgent(ws, session_manager=mgr,
                                    gen_registry=_FakeGenRegistry(doc=None))  # 无证据
            vmg = _FakeVoiceMgr()
            await handle_json_message(_voice_commit_msg("vc_su"), agent, None, ws, voice_mgr=vmg)
        finally:
            _m.get_store = original
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks and acks[-1]["status"] == "rejected", f"实际 {ws.sent}"
        assert acks[-1]["error"]["code"] == "STORAGE_UNAVAILABLE"
        assert acks[-1]["error"]["retryable"] is True
        assert acks[-1]["error"]["details"]["idempotency_durable"] is get_store().durable
        assert mgr.reserve_calls == 1, "claim 前已 reserve"
        assert mgr.release_calls == 1, "接管 claim 失败必须 release_work（不泄漏 busy 计数）"
        assert vmg.commit_calls == [], "接管 claim 失败不消费录音"

    @pytest.mark.asyncio
    async def test_voice_accepted_completes_before_ack(self, fresh_store):
        """voice 正常路径：complete 在 accepted ACK 前（complete-before-ack 不变量）。"""
        from core.input.idempotency import get_store
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs
        from core.storage.idempotency_repo import STATUS_ACCEPTED

        ws = FakeWs()
        mgr = _CountingSessionMgr()
        agent = _VoiceFakeAgent(ws, session_manager=mgr)
        vmg = _FakeVoiceMgr()
        await handle_json_message(_voice_commit_msg("vc5"), agent, None, ws, voice_mgr=vmg)
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks and acks[-1]["status"] == "accepted", f"实际 {ws.sent}"
        assert acks[-1]["messageId"]
        assert len(agent.bus_posts) == 1
        # complete 已落库（accepted ACK 前）→ 重放命中 accepted
        entry = await get_store().lookup("default", "vc5")
        assert entry is not None and entry.status == STATUS_ACCEPTED
        assert entry.received_at == acks[-1]["receivedAt"], "complete 与 ack 同一 receivedAt"
        # 同键重放 → 重放原 accepted（receivedAt 持久化原值，逐字一致）
        ws.sent.clear()
        await handle_json_message(_voice_commit_msg("vc5"), agent, None, ws, voice_mgr=vmg)
        acks2 = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks2 and acks2[-1]["status"] == "accepted"
        assert acks2[-1]["receivedAt"] == entry.received_at
        assert len(agent.bus_posts) == 1, "重放不重复入队"


class TestD3DurableInDetails:
    """D3：STORAGE_UNAVAILABLE rejected ACK/error 的 details 携带当前 idempotency_durable。"""

    pytestmark = pytest.mark.service

    @pytest.mark.asyncio
    async def test_voice_storage_unavailable_ack_carries_durable(self, fresh_store):
        """voice.commit 的 STORAGE_UNAVAILABLE rejected ACK error.details 含 durable。"""
        from core.input.idempotency import get_store
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs

        configure_store(_FlipRepo(available=False), product_mode=True)
        store = get_store()
        ws = FakeWs()
        mgr = _CountingSessionMgr()
        agent = _VoiceFakeAgent(ws, session_manager=mgr)
        vmg = _FakeVoiceMgr()
        await handle_json_message(_voice_commit_msg("vc_d3"), agent, None, ws, voice_mgr=vmg)
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks and acks[-1]["status"] == "rejected", f"实际 {ws.sent}"
        assert acks[-1]["error"]["code"] == "STORAGE_UNAVAILABLE"
        assert acks[-1]["error"]["details"]["idempotency_durable"] is store.durable
        assert store.durable is False
        assert mgr.reserve_calls == 0, "lookup fail-closed 在 reserve 前返回"
