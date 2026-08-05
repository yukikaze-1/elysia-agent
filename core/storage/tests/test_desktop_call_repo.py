"""DesktopCallRepository 单元测试（批次 4 Task 3，marker=unit）。

覆盖（spec §4.1 + brief Step 1）：
- upsert → get round-trip（离线 mongodb=None 也命中进程内缓存）
- mark 迁移守卫表（spec §4.1）：pending→终态直达合法、offline→终态合法、
  终态不可再迁移（succeeded→failed 拒绝）、非终态非法迁移拒绝
- 终态晚到 status 仅更新 result/error、不覆盖终态枚举（spec §4.1 脚注）
- mark_offline_by_connection 只标目标连接的 pending/accepted/running
- mark_stale_expired 覆盖 pending+accepted/running 超时
- query_pending 按状态 + expires_at 过滤
- Mongo 可达时写同步（fake db 写入）、离线全走 cache 不抛
- connect() 建索引 + 全量 reload 入 cache
"""

import asyncio

import pytest

from core.storage.desktop_call_repo import COLLECTION, DesktopCallRepository
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

pytestmark = pytest.mark.unit

_NOW = "2026-08-04T00:00:00+00:00"


def _call(*, call_id: str, status: str = "pending", target_connection: str = "conn_1",
          expires_at: str = "2099-01-01T00:00:00+00:00", updated_at: str = _NOW,
          **extra) -> dict:
    """构造最小 desktop_calls 文档（对齐 spec §4.1 schema）。"""
    doc = {
        "call_id": call_id,
        "tool_name": "desktop.file.read",
        "tool_version": "1.0.0",
        "runtime": "desktop",
        "arguments": {"path": "/tmp/a.txt"},
        "arguments_hash": "sha256-abc",
        "reason": "测试",
        "risk_level": "medium",
        "permission_required": True,
        "approval_authority": "client",
        "status": status,
        "target_connection": target_connection,
        "created_at": _NOW,
        "expires_at": expires_at,
        "updated_at": updated_at,
        "result": None,
        "error": None,
    }
    doc.update(extra)
    return doc


def _upsert(repo: DesktopCallRepository, **kw) -> None:
    asyncio.run(repo.upsert(_call(**kw)))


class TestRoundTrip:
    def test_upsert_then_get(self):
        repo = DesktopCallRepository(mongodb=FakeDocumentDBBackend())
        _upsert(repo, call_id="call_1")
        doc = asyncio.run(repo.get("call_1"))
        assert doc is not None
        assert doc["call_id"] == "call_1"
        assert doc["status"] == "pending"
        assert doc["tool_name"] == "desktop.file.read"
        assert doc["target_connection"] == "conn_1"

    def test_get_missing_returns_none(self):
        repo = DesktopCallRepository(mongodb=None)
        assert asyncio.run(repo.get("nope")) is None

    def test_upsert_offline_hits_cache(self):
        """mongodb=None 离线：upsert→get 全走进程内缓存命中。"""
        repo = DesktopCallRepository(mongodb=None)
        _upsert(repo, call_id="call_1")
        doc = asyncio.run(repo.get("call_1"))
        assert doc is not None
        assert doc["status"] == "pending"

    def test_get_returns_copy_does_not_mutate_cache(self):
        repo = DesktopCallRepository(mongodb=None)
        _upsert(repo, call_id="call_1")
        doc = asyncio.run(repo.get("call_1"))
        doc["status"] = "hacked"
        assert asyncio.run(repo.get("call_1"))["status"] == "pending"

    def test_upsert_writes_through_to_mongo(self):
        """Mongo 可达（fake db）：upsert/mark 同步写入 collection。"""
        repo = DesktopCallRepository(mongodb=FakeDocumentDBBackend())
        _upsert(repo, call_id="call_1")
        docs = asyncio.run(repo._db.find(COLLECTION, {}))
        assert len(docs) == 1
        assert docs[0]["call_id"] == "call_1"
        asyncio.run(repo.mark("call_1", status="accepted"))
        docs = asyncio.run(repo._db.find(COLLECTION, {"call_id": "call_1"}))
        assert docs[0]["status"] == "accepted"


class TestMarkMigrationGuard:
    @pytest.mark.parametrize("current,new,expected", [
        # pending → 客户端接手
        ("pending", "accepted", True),
        ("pending", "running", True),
        # pending → 客户端终态直达（先登记后投递，Electron 极速响应）
        ("pending", "succeeded", True),
        ("pending", "failed", True),
        ("pending", "rejected", True),
        ("pending", "cancelled", True),
        # pending → expired / offline
        ("pending", "expired", True),
        ("pending", "offline", True),
        # accepted/running → 客户端结果
        ("accepted", "succeeded", True),
        ("accepted", "failed", True),
        ("accepted", "rejected", True),
        ("accepted", "cancelled", True),
        ("running", "succeeded", True),
        ("running", "failed", True),
        ("running", "rejected", True),
        ("running", "cancelled", True),
        # accepted ↔ running（客户端执行状态往返，Electron 回 tool.status{running}）
        ("accepted", "running", True),
        ("running", "accepted", True),
        # accepted/running → expired / offline
        ("accepted", "expired", True),
        ("accepted", "offline", True),
        ("running", "expired", True),
        ("running", "offline", True),
        # offline → 终态（晚到结果/重传结果闭环）
        ("offline", "succeeded", True),
        ("offline", "failed", True),
        ("offline", "rejected", True),
        ("offline", "cancelled", True),
        ("offline", "expired", True),
        # 终态不可再迁移（spec §4.1：终态枚举不可覆盖）
        ("succeeded", "failed", False),
        ("succeeded", "expired", False),
        ("succeeded", "succeeded", True),   # 同状态幂等重标（非迁移）
        ("failed", "succeeded", False),
        ("rejected", "succeeded", False),
        ("cancelled", "succeeded", False),
        ("expired", "succeeded", False),
        ("expired", "offline", False),
        # 非终态非法迁移（不在迁移表内）
        ("offline", "accepted", False),
        ("offline", "running", False),
        ("offline", "pending", False),
        ("accepted", "pending", False),
        ("running", "pending", False),
        ("accepted", "accepted", True),     # 同状态幂等重标
        ("pending", "pending", True),       # 同状态幂等重标
    ])
    def test_mark_migration_guard_table(self, current, new, expected):
        repo = DesktopCallRepository(mongodb=None)
        _upsert(repo, call_id="call_1", status=current)
        ok = asyncio.run(repo.mark("call_1", status=new))
        assert ok is expected
        doc = asyncio.run(repo.get("call_1"))
        assert doc["status"] == (new if expected else current)

    def test_mark_terminal_rejects_enum_overwrite(self):
        """succeeded → failed 拒绝：返回 False 且终态枚举不被覆盖。"""
        repo = DesktopCallRepository(mongodb=None)
        _upsert(repo, call_id="call_1", status="succeeded")
        ok = asyncio.run(repo.mark("call_1", status="failed"))
        assert ok is False
        assert asyncio.run(repo.get("call_1"))["status"] == "succeeded"

    def test_mark_terminal_late_status_records_result_error_only(self):
        """终态晚到 status 仅更新 result/error，不覆盖终态枚举（spec §4.1 脚注）。"""
        repo = DesktopCallRepository(mongodb=None)
        _upsert(repo, call_id="call_1", status="expired")
        ok = asyncio.run(repo.mark(
            "call_1", status="succeeded", result={"output": "late"}, error=None))
        assert ok is False  # 枚举迁移被拒绝
        doc = asyncio.run(repo.get("call_1"))
        assert doc["status"] == "expired"                       # 终态枚举保持
        assert doc["result"] == {"output": "late"}              # 真实结果被记录
        assert doc["error"] is None

    def test_mark_offline_to_succeeded_legal(self):
        """offline → succeeded 合法（重传结果闭环）。"""
        repo = DesktopCallRepository(mongodb=None)
        _upsert(repo, call_id="call_1", status="offline")
        ok = asyncio.run(repo.mark("call_1", status="succeeded", result={"output": "resume"}))
        assert ok is True
        doc = asyncio.run(repo.get("call_1"))
        assert doc["status"] == "succeeded"
        assert doc["result"] == {"output": "resume"}

    def test_mark_unknown_call_returns_false(self):
        repo = DesktopCallRepository(mongodb=None)
        assert asyncio.run(repo.mark("nope", status="succeeded")) is False

    def test_mark_without_status_updates_fields(self):
        """mark 不带 status：仅更新字段，不动状态（如已终态补记 result/error）。"""
        repo = DesktopCallRepository(mongodb=None)
        _upsert(repo, call_id="call_1", status="accepted")
        ok = asyncio.run(repo.mark("call_1", result={"output": "x"}))
        assert ok is True
        doc = asyncio.run(repo.get("call_1"))
        assert doc["status"] == "accepted"
        assert doc["result"] == {"output": "x"}


class TestBulkOps:
    def test_mark_offline_by_connection_scoped(self):
        """只标目标连接的 pending/accepted/running；其他连接与终态不动。"""
        repo = DesktopCallRepository(mongodb=None)
        _upsert(repo, call_id="c1", status="pending", target_connection="conn_1")
        _upsert(repo, call_id="c2", status="accepted", target_connection="conn_1")
        _upsert(repo, call_id="c3", status="running", target_connection="conn_2")
        _upsert(repo, call_id="c4", status="succeeded", target_connection="conn_1")
        n = asyncio.run(repo.mark_offline_by_connection("conn_1"))
        assert n == 2
        assert asyncio.run(repo.get("c1"))["status"] == "offline"
        assert asyncio.run(repo.get("c2"))["status"] == "offline"
        assert asyncio.run(repo.get("c3"))["status"] == "running"    # 其他连接不动
        assert asyncio.run(repo.get("c4"))["status"] == "succeeded"  # 终态不动

    def test_mark_stale_expired_covers_pending_and_accepted_running(self):
        """启动清理：pending + accepted/running 中 updated_at < cutoff 批量标 expired。"""
        repo = DesktopCallRepository(mongodb=None)
        old = "2026-08-01T00:00:00+00:00"
        _upsert(repo, call_id="c1", status="pending", updated_at=old)
        _upsert(repo, call_id="c2", status="accepted", updated_at=old)
        _upsert(repo, call_id="c3", status="running", updated_at=old)
        _upsert(repo, call_id="c4", status="pending", updated_at=_NOW)
        _upsert(repo, call_id="c5", status="succeeded", updated_at=old)
        n = asyncio.run(repo.mark_stale_expired("2026-08-03T00:00:00+00:00"))
        assert n == 3
        assert asyncio.run(repo.get("c1"))["status"] == "expired"
        assert asyncio.run(repo.get("c2"))["status"] == "expired"
        assert asyncio.run(repo.get("c3"))["status"] == "expired"
        assert asyncio.run(repo.get("c4"))["status"] == "pending"    # 未过期不动
        assert asyncio.run(repo.get("c5"))["status"] == "succeeded"  # 终态不动

    def test_mark_stale_expired_advances_updated_at(self):
        repo = DesktopCallRepository(mongodb=None)
        old = "2026-08-01T00:00:00+00:00"
        _upsert(repo, call_id="c1", status="pending", updated_at=old)
        asyncio.run(repo.mark_stale_expired("2026-08-03T00:00:00+00:00"))
        doc = asyncio.run(repo.get("c1"))
        assert doc["status"] == "expired"
        assert doc["updated_at"] > old  # 状态变更推进 updated_at（TTL 基准）

    def test_query_pending_filters_status_and_expiry(self):
        repo = DesktopCallRepository(mongodb=None)
        cutoff = "2026-08-05T00:00:00+00:00"
        _upsert(repo, call_id="c1", status="pending", expires_at="2099-01-01T00:00:00+00:00")
        _upsert(repo, call_id="c2", status="accepted", expires_at="2099-01-01T00:00:00+00:00")
        _upsert(repo, call_id="c3", status="running", expires_at="2099-01-01T00:00:00+00:00")
        _upsert(repo, call_id="c4", status="succeeded", expires_at="2099-01-01T00:00:00+00:00")
        _upsert(repo, call_id="c5", status="pending", expires_at="2026-01-01T00:00:00+00:00")
        docs = asyncio.run(repo.query_pending(cutoff))
        assert sorted(d["call_id"] for d in docs) == ["c1", "c2", "c3"]

    def test_offline_all_operations_no_throw(self):
        """mongodb=None：upsert/get/query_pending/mark 全走 cache 不抛。"""
        repo = DesktopCallRepository(mongodb=None)
        asyncio.run(repo.upsert(_call(call_id="c1")))
        assert asyncio.run(repo.get("c1")) is not None
        docs = asyncio.run(repo.query_pending("now"))
        assert isinstance(docs, list)
        assert asyncio.run(repo.mark("c1", status="accepted")) is True
        assert asyncio.run(repo.mark_offline_by_connection("conn_1")) >= 0
        assert asyncio.run(repo.mark_stale_expired("now")) >= 0
        assert asyncio.run(repo.health_check()) is False


class TestConnect:
    def test_connect_with_fake_db_reloads_from_db(self):
        """connect() 建索引 + 全量 reload 入 cache。"""
        repo = DesktopCallRepository(mongodb=FakeDocumentDBBackend())
        _upsert(repo, call_id="c1")
        # 直接往 db 插入一条不在 cache 的记录
        asyncio.run(repo._db.insert_one(COLLECTION, _call(call_id="c2", status="accepted")))
        assert asyncio.run(repo.get("c2")) is None  # 尚未 reload
        ok = asyncio.run(repo.connect())
        assert ok is True
        assert asyncio.run(repo.get("c1")) is not None
        assert asyncio.run(repo.get("c2")) is not None
        assert asyncio.run(repo.get("c2"))["status"] == "accepted"

    def test_reload_from_db_paginates_beyond_find_default_limit(self):
        """reload_from_db 分页载入：超过 find 默认 limit=100 的记录不被截断。"""
        repo = DesktopCallRepository(mongodb=FakeDocumentDBBackend())
        for i in range(150):
            asyncio.run(repo._db.insert_one(
                COLLECTION, _call(call_id=f"call_{i}", status="pending")))
        asyncio.run(repo.connect())
        assert len(repo._cache) == 150          # 全量载入（修复前仅前 100 条）
        assert asyncio.run(repo.get("call_0")) is not None
        assert asyncio.run(repo.get("call_149")) is not None

    def test_health_check_reflects_db(self):
        repo = DesktopCallRepository(mongodb=None)
        assert asyncio.run(repo.health_check()) is False
        repo2 = DesktopCallRepository(mongodb=FakeDocumentDBBackend())
        assert asyncio.run(repo2.health_check()) is True
