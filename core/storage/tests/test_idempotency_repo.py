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
    IdempotencyRepository,
    STATUS_ACCEPTED,
    STATUS_CLAIMED,
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
