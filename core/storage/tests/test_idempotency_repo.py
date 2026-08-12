"""IdempotencyRepository 单元测试（D6c，marker=service）。

使用内存版 FakeDocumentDBBackend，不触碰真实 Mongo。覆盖：
- db=None（Mongo 离线）→ 所有操作 fail-closed raise StorageUnavailableError
- claim 新键 → ClaimResult.acquired=True（获得所有权）
- claim 已存在键 → ClaimResult.acquired=False + 既有记录（并发/重放，保留首写 message_id/digest）
- 不同 conversation_id 同 client_request_id → 独立 claim
- complete → status=accepted
- release → 删除记录
- connect 建唯一索引（conversation_id+client_request_id）+ TTL 索引（expires_at）

Run: python -m pytest core/storage/tests/test_idempotency_repo.py -q
"""

import asyncio
import time
from datetime import datetime, timezone

import pytest

from core.storage.idempotency_repo import (
    COLLECTION,
    IdempotencyRepository,
    STATUS_ACCEPTED,
    STATUS_CLAIMED,
    STATUS_REJECTED,
    StorageUnavailableError,
)
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

pytestmark = pytest.mark.service


def _run(coro):
    return asyncio.run(coro)


def _expires() -> datetime:
    """TTL 字段值（BSON Date，aware UTC）。"""
    return datetime.now(timezone.utc)


class TestFailClosed:
    """db=None（Mongo 离线）时幂等操作 fail-closed——全部 raise StorageUnavailableError。"""

    def test_claim_offline_raises(self):
        repo = IdempotencyRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            _run(repo.claim("default", "crid", content_digest="abc", message_id="m1",
                            status=STATUS_CLAIMED, claimed_at=time.time(),
                            expires_at=_expires()))

    def test_get_offline_raises(self):
        repo = IdempotencyRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            _run(repo.get("default", "crid"))

    def test_complete_offline_raises(self):
        repo = IdempotencyRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            _run(repo.complete("default", "crid"))

    def test_release_offline_raises(self):
        repo = IdempotencyRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            _run(repo.release("default", "crid"))


class TestClaim:
    def test_claim_new_acquires(self):
        """新 claim → acquired=True（所有权归本调用方）。"""
        repo = IdempotencyRepository(db=FakeDocumentDBBackend())
        r = _run(repo.claim("default", "crid", content_digest="abc", message_id="m1",
                            status=STATUS_CLAIMED, claimed_at=time.time(),
                            expires_at=_expires()))
        assert r.acquired is True  # 新 claim 获得所有权
        assert r.entry["message_id"] == "m1"
        doc = _run(repo.get("default", "crid"))
        assert doc is not None
        assert doc["message_id"] == "m1"
        assert doc["content_digest"] == "abc"
        assert doc["status"] == STATUS_CLAIMED
        assert doc["conversation_id"] == "default"
        assert doc["client_request_id"] == "crid"
        assert doc["claimed_at"] > 0  # 墙钟 unix 秒（TTL/接管判 stale 依据）
        assert doc["expires_at"]  # BSON Date TTL 字段

    def test_claim_existing_loses(self):
        """同键二次 claim（并发/重放）→ acquired=False + 既有记录，不覆盖首写。"""
        repo = IdempotencyRepository(db=FakeDocumentDBBackend())
        _run(repo.claim("default", "crid", content_digest="abc", message_id="m1",
                        status=STATUS_CLAIMED, claimed_at=time.time(),
                        expires_at=_expires()))
        existing = _run(repo.claim("default", "crid", content_digest="DIFFERENT",
                                   message_id="m2", status=STATUS_CLAIMED,
                                   claimed_at=time.time(), expires_at=_expires()))
        assert existing.acquired is False  # 并发 loser：不进入业务处理
        assert existing.entry["message_id"] == "m1"
        assert existing.entry["content_digest"] == "abc"

    def test_claim_scoped_by_conversation(self):
        """不同会话同 client_request_id → 独立新 claim（唯一约束按会话维度）。"""
        repo = IdempotencyRepository(db=FakeDocumentDBBackend())
        _run(repo.claim("conv_a", "crid", content_digest="abc", message_id="m1",
                        status=STATUS_CLAIMED, claimed_at=time.time(),
                        expires_at=_expires()))
        r = _run(repo.claim("conv_b", "crid", content_digest="abc", message_id="m2",
                            status=STATUS_CLAIMED, claimed_at=time.time(),
                            expires_at=_expires()))
        assert r.acquired is True
        assert _run(repo.get("conv_b", "crid"))["message_id"] == "m2"

    def test_complete_updates_status(self):
        repo = IdempotencyRepository(db=FakeDocumentDBBackend())
        _run(repo.claim("default", "crid", content_digest="abc", message_id="m1",
                        status=STATUS_CLAIMED, claimed_at=time.time(),
                        expires_at=_expires()))
        _run(repo.complete("default", "crid"))
        assert _run(repo.get("default", "crid"))["status"] == STATUS_ACCEPTED

    def test_release_deletes_record(self):
        repo = IdempotencyRepository(db=FakeDocumentDBBackend())
        _run(repo.claim("default", "crid", content_digest="abc", message_id="m1",
                        status=STATUS_CLAIMED, claimed_at=time.time(),
                        expires_at=_expires()))
        _run(repo.release("default", "crid"))
        assert _run(repo.get("default", "crid")) is None


class TestConnect:
    def test_connect_sets_db_and_creates_indexes(self):
        class _RecordingFake(FakeDocumentDBBackend):
            def __init__(self):
                super().__init__()
                self.index_calls = []

            async def ensure_index(self, collection, keys, **kwargs):
                self.index_calls.append((collection, keys, kwargs))
                return "idx"

        fake = _RecordingFake()
        repo = IdempotencyRepository(db=fake)
        assert _run(repo.connect()) is True
        assert repo._db is fake  # noqa: SLF001
        # 唯一索引（防并发双写）：{conversation_id, client_request_id}
        assert any(
            ("conversation_id", 1) in keys and ("client_request_id", 1) in keys
            and kwargs.get("unique") is True
            for _, keys, kwargs in fake.index_calls
        ), f"缺唯一索引: {fake.index_calls}"
        # TTL 索引（真到期删除）：expires_at expireAfterSeconds=0
        assert any(
            ("expires_at", 1) in keys and kwargs.get("expireAfterSeconds") == 0
            for _, keys, kwargs in fake.index_calls
        ), f"缺 TTL 索引: {fake.index_calls}"

    def test_connect_failure_returns_false(self, monkeypatch):
        """建连失败 → False + _db=None（后续操作 fail-closed）。"""
        # 指向不可达端口：DocumentDBFactory 在 base_url=None 时读 MONGODB_URI env。
        # monkeypatch 而非 setdefault——防进程 env 被其他测试预设污染。
        monkeypatch.setenv("MONGODB_URI", "mongodb://127.0.0.1:1")
        repo = IdempotencyRepository(db=None)
        result = _run(repo.connect())
        assert result is False
        assert repo._db is None  # noqa: SLF001


class DuplicateKeyError(RuntimeError):
    """模拟 pymongo.errors.DuplicateKeyError（repo 按异常类名匹配，驱动无关）。"""


class _UpsertDuplicateFake(FakeDocumentDBBackend):
    """update_one upsert 对已存在同键 doc 抛 DuplicateKeyError（模拟唯一索引竞态）。

    与 _DuplicateOnceFake（拦 insert_one）不同——本 fake 拦 upsert 的 update_one，
    覆盖 restore() 的 E11000 防御层路径（终态同键 doc 过滤器失配 → upsert 尝试插入
    → 唯一索引冲突）。
    """

    def __init__(self, winner: dict | None = None) -> None:
        super().__init__()
        self._winner = winner
        self.duplicate_key_raised = 0  # E11000 触发计数（断言防御层真实走到的信号）

    async def update_one(self, collection, filter_expr, update, upsert=False):
        coll = self._coll(collection)
        key = (filter_expr.get("conversation_id"), filter_expr.get("client_request_id"))
        if upsert and any(
            d.get("conversation_id") == key[0] and d.get("client_request_id") == key[1]
            for d in coll
        ):
            self.duplicate_key_raised += 1
            raise DuplicateKeyError("E11000 duplicate key error")
        return await super().update_one(collection, filter_expr, update, upsert=upsert)

    async def find(self, collection, filter_expr, **kwargs):
        if self._winner is not None:
            if (self._winner.get("conversation_id") == filter_expr.get("conversation_id")
                    and self._winner.get("client_request_id") == filter_expr.get("client_request_id")):
                return [dict(self._winner)]
        return await super().find(collection, filter_expr, **kwargs)


class _RecordingUpdateFake(FakeDocumentDBBackend):
    """记录 update_one 调用（filter/$set/upsert）供断言 restore 升级形态。"""

    def __init__(self) -> None:
        super().__init__()
        self.updates: list[tuple[dict, dict, bool]] = []

    async def update_one(self, collection, filter_expr, update, upsert=False):
        self.updates.append((filter_expr, update, upsert))
        return await super().update_one(collection, filter_expr, update, upsert=upsert)


class TestRestore:
    """IdempotencyRepository.restore——完整记录条件 upsert（BE-P1-06）。"""

    pytestmark = pytest.mark.service

    def _full_doc(self, *, status: str = STATUS_CLAIMED, message_id: str = "msg_1") -> dict:
        """对齐生产 _build_full_doc 形态：终态才含 ACK 负载键，None 键不写入。"""
        doc = {
            "conversation_id": "c1",
            "client_request_id": "r1",
            "content_digest": "d1",
            "message_id": message_id,
            "status": status,
            "claimed_at": time.time(),
            "expires_at": _expires(),
        }
        if status != STATUS_CLAIMED:
            doc["received_at"] = "2026-08-12T00:00:00Z"
            doc["client_message_id"] = "cm1"
            doc["conversation_id_wire"] = "wire-1"
            if status == STATUS_REJECTED:
                doc["error"] = {"code": "RATE_LIMITED", "message": "限流",
                                "retryable": True, "retryAfterMs": 30000}
        return doc

    def test_restore_missing_identity_keys_raises(self):
        """缺 conversation_id/client_request_id → ValueError（编程错误，非存储故障）。"""
        repo = IdempotencyRepository(db=FakeDocumentDBBackend())
        with pytest.raises(ValueError):
            _run(repo.restore({"content_digest": "d1", "message_id": "m"}))

    def test_restore_inserts_full_doc(self):
        """Mongo 缺失 → upsert 插入完整文档（含终态 ACK 负载）。"""
        repo = IdempotencyRepository(db=FakeDocumentDBBackend())
        doc = self._full_doc(status=STATUS_ACCEPTED)
        _run(repo.restore(doc))
        got = _run(repo.get("c1", "r1"))
        assert got is not None
        assert got["status"] == STATUS_ACCEPTED
        assert got["message_id"] == "msg_1"
        assert got["received_at"] == "2026-08-12T00:00:00Z"
        assert got["client_message_id"] == "cm1"
        assert got["conversation_id_wire"] == "wire-1"

    def test_restore_upgrades_claimed_to_terminal(self):
        """Mongo 已有 claimed → $set 升级为完整终态（保留 claimed 的身份键）。"""
        fake = _RecordingUpdateFake()
        fake.collections[COLLECTION] = [{
            "conversation_id": "c1", "client_request_id": "r1",
            "content_digest": "old", "message_id": "old-msg",
            "status": STATUS_CLAIMED, "claimed_at": time.time() - 60.0,
            "expires_at": _expires(),
        }]
        repo = IdempotencyRepository(db=fake)
        doc = self._full_doc(status=STATUS_ACCEPTED)
        _run(repo.restore(doc))
        # 过滤器含 status∈{claimed,in_progress}（命中升级）
        filt, update, upsert = fake.updates[0]
        assert upsert is True
        assert filt["status"] == {"$in": [STATUS_CLAIMED, "in_progress"]}
        set_expr = update["$set"]
        assert set_expr["status"] == STATUS_ACCEPTED
        assert set_expr["received_at"] == "2026-08-12T00:00:00Z"
        assert "error" not in set_expr  # accepted 无 error 键（对齐 _build_full_doc）
        # Mongo 已升级（$set 生效）
        got = _run(repo.get("c1", "r1"))
        assert got["status"] == STATUS_ACCEPTED
        assert got["received_at"] == "2026-08-12T00:00:00Z"

    def test_restore_terminal_winner_not_overwritten(self):
        """Mongo 已终态（另一进程 winner）→ 过滤器失配 → upsert 触发 E11000 →
        防御层读回 winner，不覆盖、repo 不判死。"""
        winner = self._full_doc(status=STATUS_REJECTED, message_id="winner-msg")
        fake = _UpsertDuplicateFake(winner=winner)
        fake.collections[COLLECTION] = [dict(winner)]
        repo = IdempotencyRepository(db=fake)
        repo._injected_db = False  # noqa: SLF001  # 强制非注入路径：E11000 必须不 _mark_unavailable
        loser = self._full_doc(status=STATUS_CLAIMED, message_id="loser-msg")
        _run(repo.restore(loser))
        # 不抛异常（E11000 防御层读回后返回）；断言防御层真实触发（非 upsert 漏掉后
        # 的 no-op 假绿——filter 对终态失配 + 无 upsert → update_one 无操作即过）
        assert fake.duplicate_key_raised >= 1, "E11000 防御层必须真实触发（upsert 插入同键）"
        assert repo.is_connected is True  # 不判死
        assert _run(repo.health_check()) is True  # 后续可用
        # Mongo 仍是 winner（未被 loser 覆盖）
        got = _run(repo.get("c1", "r1"))
        assert got["message_id"] == "winner-msg"
        assert got["status"] == STATUS_REJECTED

    def test_restore_offline_raises(self):
        """Mongo 离线（db=None）→ StorageUnavailableError（fail-closed）。"""
        repo = IdempotencyRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            _run(repo.restore(self._full_doc()))

    def test_restore_inserts_terminal_status_from_set(self):
        """Mongo 缺失 + filter 含 status $in → upsert 插入文档的 status 来自 $set
        （非等值条件不并入插入文档）。"""
        fake = _RecordingUpdateFake()
        repo = IdempotencyRepository(db=fake)
        doc = self._full_doc(status=STATUS_REJECTED)
        _run(repo.restore(doc))
        _, update, upsert = fake.updates[0]
        assert upsert is True
        # 插入文档含 filter 等值字段（conversation_id/client_request_id）——upsert 语义
        got = _run(repo.get("c1", "r1"))
        assert got["status"] == STATUS_REJECTED
        assert got["error"]["code"] == "RATE_LIMITED"
        assert got["error"]["retryAfterMs"] == 30000
