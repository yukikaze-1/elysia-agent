"""FakeDocumentDBBackend 语义测试（marker=service）——替身不得对真后端说谎。

钉死两条与真 Mongo 对齐的语义：
1. `update_one` 返回**真实 modified_count**（命中且变=1 / 命中未变=0 / 未命中=0 /
   upsert 插入=0）——真实现 mongodb.py 返回 `result.modified_count`。
2. `ensure_index(unique=True)` 在写入路径强制唯一约束（冲突抛 DuplicateKeyError，
   类名与真 driver 一致）；`partialFilterExpression` 亦生效。TTL 索引仅记录 +
   `sweep_expired()` 显式出口（默认不急切删，详见 _fake_documentdb.py docstring）。

Run: python -m pytest core/storage/tests/test_fake_documentdb_semantics.py -q
"""

from datetime import datetime, timedelta, timezone

import pytest
from pymongo.errors import DuplicateKeyError

from core.storage.idempotency_repo import (
    COLLECTION as IDEMPOTENCY_COLLECTION,
    STATUS_CLAIMED,
    IdempotencyRepository,
    _is_duplicate_key,
)
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

pytestmark = pytest.mark.service


class TestRepoWiring:
    """唯一约束经真实 repo 端到端生效（不只是裸 fake）。"""

    @pytest.mark.asyncio
    async def test_repo_connect_declares_and_enforces_unique(self):
        """IdempotencyRepository.connect 声明唯一索引 → 同键二写抛 DuplicateKeyError，
        且异常类名被 repo 的 `_is_duplicate_key` 识别（真 driver 语义端到端贯通）。"""
        db = FakeDocumentDBBackend()
        repo = IdempotencyRepository(db=db)
        assert await repo.connect() is True
        assert [s["name"] for s in db.unique_indexes[IDEMPOTENCY_COLLECTION]] == [
            "conversation_id_1_client_request_id_1"]
        assert [s["name"] for s in db.ttl_indexes[IDEMPOTENCY_COLLECTION]] == ["expires_at_1"]

        await repo.claim(
            "c1", "r1", content_digest="d", message_id="m", status=STATUS_CLAIMED,
            claimed_at=1.0, expires_at=datetime.now(timezone.utc))
        with pytest.raises(DuplicateKeyError) as ei:
            await db.insert_one(
                IDEMPOTENCY_COLLECTION,
                {"conversation_id": "c1", "client_request_id": "r1"})
        assert _is_duplicate_key(ei.value) is True


class TestModifiedCount:
    """update_one 的 modified_count 三情形（对齐真 Mongo）。"""

    @pytest.mark.asyncio
    async def test_hit_and_changed_returns_one(self):
        db = FakeDocumentDBBackend()
        await db.insert_one("c", {"k": 1, "v": "a"})
        assert await db.update_one("c", {"k": 1}, {"$set": {"v": "b"}}) == 1

    @pytest.mark.asyncio
    async def test_hit_but_unchanged_returns_zero(self):
        """filter 命中但 `$set` 写入值与现值相同 → 真 Mongo modified_count=0。"""
        db = FakeDocumentDBBackend()
        await db.insert_one("c", {"k": 1, "v": "a"})
        assert await db.update_one("c", {"k": 1}, {"$set": {"v": "a"}}) == 0

    @pytest.mark.asyncio
    async def test_miss_without_upsert_returns_zero(self):
        db = FakeDocumentDBBackend()
        await db.insert_one("c", {"k": 1})
        assert await db.update_one("c", {"k": 999}, {"$set": {"v": "z"}}) == 0

    @pytest.mark.asyncio
    async def test_upsert_insert_returns_zero(self):
        """upsert 插入路径 → modified_count=0（真 Mongo 的 upsert-insert 语义）。"""
        db = FakeDocumentDBBackend()
        assert await db.update_one("c", {"k": 2}, {"$set": {"v": "q"}}, upsert=True) == 0
        assert await db.count("c", {"k": 2}) == 1

    @pytest.mark.asyncio
    async def test_addtoset_same_element_returns_zero(self):
        db = FakeDocumentDBBackend()
        await db.insert_one("c", {"k": 1, "lst": [1, 2]})
        assert await db.update_one("c", {"k": 1}, {"$addToSet": {"lst": 1}}) == 0
        assert await db.update_one("c", {"k": 1}, {"$addToSet": {"lst": 3}}) == 1

    @pytest.mark.asyncio
    async def test_pull_absent_value_returns_zero(self):
        db = FakeDocumentDBBackend()
        await db.insert_one("c", {"k": 1, "lst": [1, 2]})
        assert await db.update_one("c", {"k": 1}, {"$pull": {"lst": 99}}) == 0
        assert await db.update_one("c", {"k": 1}, {"$pull": {"lst": 1}}) == 1


class TestUniqueIndex:
    """唯一索引在写入路径强制（对齐真 Mongo E11000）。"""

    @pytest.mark.asyncio
    async def test_insert_duplicate_raises(self):
        db = FakeDocumentDBBackend()
        await db.ensure_index("u", [("x", 1)], unique=True)
        await db.insert_one("u", {"x": 1})
        with pytest.raises(DuplicateKeyError):
            await db.insert_one("u", {"x": 1})

    @pytest.mark.asyncio
    async def test_no_index_no_enforcement(self):
        """未声明唯一索引 → 不强制（与真 Mongo 一致：无索引即无约束）。"""
        db = FakeDocumentDBBackend()
        await db.insert_one("u", {"x": 1})
        await db.insert_one("u", {"x": 1})  # 不应抛
        assert await db.count("u", {"x": 1}) == 2

    @pytest.mark.asyncio
    async def test_compound_unique_index(self):
        db = FakeDocumentDBBackend()
        await db.ensure_index("u", [("a", 1), ("b", 1)], unique=True)
        await db.insert_one("u", {"a": 1, "b": 1})
        await db.insert_one("u", {"a": 1, "b": 2})  # 复合键不同 → 允许
        with pytest.raises(DuplicateKeyError):
            await db.insert_one("u", {"a": 1, "b": 1})

    @pytest.mark.asyncio
    async def test_insert_many_ordered_stops_at_conflict(self):
        """insert_many ordered 语义：首个冲突抛错，之前文档保留。"""
        db = FakeDocumentDBBackend()
        await db.ensure_index("u", [("x", 1)], unique=True)
        await db.insert_one("u", {"x": 9})
        with pytest.raises(DuplicateKeyError):
            await db.insert_many("u", [{"x": 1}, {"x": 2}, {"x": 9}])
        # 前两条已写入，第三条冲突
        assert await db.count("u", {"x": 1}) == 1
        assert await db.count("u", {"x": 2}) == 1

    @pytest.mark.asyncio
    async def test_upsert_insert_path_conflict_raises(self):
        """upsert 插入路径（filter 失配 → 建新文档）撞唯一键 → DuplicateKeyError。

        即 idempotency restore() 的 E11000 防御层真实场景。"""
        db = FakeDocumentDBBackend()
        await db.ensure_index("u", [("x", 1)], unique=True)
        await db.insert_one("u", {"x": 1})
        with pytest.raises(DuplicateKeyError):
            # filter 用 y=9 失配（走插入），但 $set 写入 x=1（撞既有唯一键）
            await db.update_one("u", {"y": 9}, {"$set": {"x": 1, "y": 9}}, upsert=True)

    @pytest.mark.asyncio
    async def test_update_that_violates_unique_raises_and_rolls_back(self):
        db = FakeDocumentDBBackend()
        await db.ensure_index("u", [("x", 1)], unique=True)
        await db.insert_one("u", {"id": "a", "x": 1})
        await db.insert_one("u", {"id": "b", "x": 2})
        with pytest.raises(DuplicateKeyError):
            await db.update_one("u", {"id": "b"}, {"$set": {"x": 1}})
        # 更新被拒绝 → b 仍是 x=2（原子失败，未落半截）
        assert (await db.find("u", {"id": "b"}))[0]["x"] == 2

    @pytest.mark.asyncio
    async def test_partial_filter_expression(self):
        """partialFilterExpression：仅满足过滤子句的文档入索引。"""
        db = FakeDocumentDBBackend()
        await db.ensure_index(
            "p", [("o", 1), ("c", 1), ("canon", 1)], unique=True,
            partialFilterExpression={"canon": True})
        await db.insert_one("p", {"o": "u", "c": "x", "canon": True})
        # canonical=False 不入索引 → 同键允许重复
        await db.insert_one("p", {"o": "u", "c": "x", "canon": False})
        await db.insert_one("p", {"o": "u", "c": "x", "canon": False})
        with pytest.raises(DuplicateKeyError):
            await db.insert_one("p", {"o": "u", "c": "x", "canon": True})

    @pytest.mark.asyncio
    async def test_id_is_always_unique(self):
        """_id 恒为唯一索引（无需显式声明）。"""
        db = FakeDocumentDBBackend()
        await db.insert_one("u", {"_id": "same", "x": 1})
        with pytest.raises(DuplicateKeyError):
            await db.insert_one("u", {"_id": "same", "x": 2})

    @pytest.mark.asyncio
    async def test_unique_indexes_property(self):
        db = FakeDocumentDBBackend()
        await db.ensure_index("u", [("x", 1)], unique=True)
        await db.ensure_index("u", [("y", 1)])  # 非唯一
        names = [s["name"] for s in db.unique_indexes["u"]]
        assert names == ["x_1"]


class TestTtl:
    """TTL 索引仅记录 + sweep_expired() 显式出口（默认不急切删，已知局限）。"""

    @pytest.mark.asyncio
    async def test_ttl_index_recorded(self):
        db = FakeDocumentDBBackend()
        await db.ensure_index("t", [("exp", 1)], expireAfterSeconds=0)
        assert [s["name"] for s in db.ttl_indexes["t"]] == ["exp_1"]

    @pytest.mark.asyncio
    async def test_default_does_not_expire(self):
        """默认不急切删（真 Mongo TTL 监控异步 ~60s；急切删会产生反向偏差）。"""
        db = FakeDocumentDBBackend()
        await db.ensure_index("t", [("exp", 1)], expireAfterSeconds=0)
        past = datetime.now(timezone.utc) - timedelta(days=1)
        await db.insert_one("t", {"id": "old", "exp": past})
        assert len(await db.find("t", {})) == 1

    @pytest.mark.asyncio
    async def test_sweep_expired_removes_only_past(self):
        db = FakeDocumentDBBackend()
        await db.ensure_index("t", [("exp", 1)], expireAfterSeconds=0)
        past = datetime.now(timezone.utc) - timedelta(days=1)
        future = datetime.now(timezone.utc) + timedelta(days=1)
        await db.insert_one("t", {"id": "old", "exp": past})
        await db.insert_one("t", {"id": "new", "exp": future})
        removed = db.sweep_expired()
        assert removed == 1
        docs = await db.find("t", {})
        assert [d["id"] for d in docs] == ["new"]


@pytest.mark.unit
class TestQueryOperatorFidelity:
    """查询算子保真度（批次 16 补强）：`$not` 与 **BSON 类型括号**。

    为什么这两条是"证据完整性"问题而不是实现细节：KB-15 的 legacy 桶判据是
    `{"updated_at": {"$not": {"$gt": ""}}}`，而 fake 对**未知算子**是一律忽略
    （match-all）——`$not` 若不被实现，所有基于 fake 的用例都会在错误的结果集上
    通过（假绿）；混类型数据集若不实现类型括号，则直接抛 `TypeError`，
    被 `Repository._call` 吞成 `StorageUnavailableError` 后还会伪装成"降级"。
    真库语义见 `tmp/b16/main/probe_kb15_e3.py`（mongo:8 实测）。
    """

    @pytest.mark.asyncio
    async def test_not_operator_matches_missing_and_null(self):
        db = FakeDocumentDBBackend()
        await db.insert_one("t", {"id": "valid", "ts": "2026-08-01T00:00:00+00:00"})
        await db.insert_one("t", {"id": "empty", "ts": ""})
        await db.insert_one("t", {"id": "null", "ts": None})
        await db.insert_one("t", {"id": "missing"})
        await db.insert_one("t", {"id": "date", "ts": datetime(2026, 9, 1, tzinfo=timezone.utc)})

        not_gt = await db.find("t", {"ts": {"$not": {"$gt": ""}}})
        gt = await db.find("t", {"ts": {"$gt": ""}})

        assert sorted(d["id"] for d in not_gt) == ["date", "empty", "missing", "null"]
        assert sorted(d["id"] for d in gt) == ["valid"]

    @pytest.mark.asyncio
    async def test_comparison_operators_are_type_bracketed(self):
        """跨 BSON 类型不匹配（真库类型括号）——且**不得抛 TypeError**。"""
        db = FakeDocumentDBBackend()
        await db.insert_one("t", {"id": "date", "ts": datetime(2026, 9, 1, tzinfo=timezone.utc)})
        await db.insert_one("t", {"id": "int", "ts": 1756735200})
        await db.insert_one("t", {"id": "bool", "ts": True})
        await db.insert_one("t", {"id": "str", "ts": "2026-08-01T00:00:00+00:00"})

        assert [d["id"] for d in await db.find("t", {"ts": {"$lt": "2026-09-01T00:00:00+00:00"}})] == ["str"]
        assert [d["id"] for d in await db.find("t", {"ts": {"$gt": 0}})] == ["int"]
        assert [d["id"] for d in await db.find("t", {"ts": {"$gt": ""}})] == ["str"]

    @pytest.mark.asyncio
    async def test_null_compares_equal_to_null_only(self):
        """`null` vs `null`：相等（`$lte`/`$gte` 命中，`$lt`/`$gt` 不命中），且不抛错。"""
        db = FakeDocumentDBBackend()
        await db.insert_one("t", {"id": "null", "ts": None})
        await db.insert_one("t", {"id": "missing"})
        await db.insert_one("t", {"id": "str", "ts": "x"})

        assert sorted(d["id"] for d in await db.find("t", {"ts": {"$lte": None}})) == [
            "missing", "null"]
        assert await db.find("t", {"ts": {"$lt": None}}) == []


@pytest.mark.unit
class TestUniqueIndexBuildFidelity:
    """`ensure_index(unique=True)` 必须像真库一样校验**既有文档**（批次 16 补强）。

    为什么这是"证据完整性"问题：fake 的 `aggregate()` 是 stub（恒 `[]`）⇒
    "库里有重复 ⇒ 唯一索引建不起来 / 必须降级"这条分支如果用 fake 验证，不补这一条
    就会得到**假绿**（fake 报 `unique=True`，真库抛 `DuplicateKeyError` 11000 后降级）。
    """

    @pytest.mark.asyncio
    async def test_build_with_existing_duplicates_raises(self):
        from core.storage.tests._fake_documentdb import DuplicateKeyError

        db = FakeDocumentDBBackend()
        for _ in range(2):
            await db.insert_one("t", {"session_id": "dup"})

        with pytest.raises(DuplicateKeyError):
            await db.ensure_index("t", [("session_id", 1)], unique=True)

        # 与真库一致：失败后索引**不存在**（不是"建了个非唯一的"）
        assert [s["name"] for s in db.indexes.get("t", [])] == []

    @pytest.mark.asyncio
    async def test_clean_data_builds_and_is_idempotent(self):
        db = FakeDocumentDBBackend()
        await db.insert_one("t", {"session_id": "a"})
        await db.insert_one("t", {"session_id": "b"})

        for _ in range(2):
            assert await db.ensure_index("t", [("session_id", 1)], unique=True) == "session_id_1"
        assert [s["name"] for s in db.indexes["t"]] == ["session_id_1"]

    @pytest.mark.asyncio
    async def test_same_name_with_different_options_raises(self):
        """同名不同选项：真库抛 `OperationFailure`(86)，fake 至少不得静默改选项。"""
        db = FakeDocumentDBBackend()
        await db.ensure_index("t", [("session_id", 1)])

        with pytest.raises(RuntimeError, match="IndexKeySpecsConflict"):
            await db.ensure_index("t", [("session_id", 1)], unique=True)
