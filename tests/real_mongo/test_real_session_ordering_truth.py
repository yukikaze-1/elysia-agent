"""批次 16 / KB-15 —— **真实 MongoDB** 上的会话排序可达性（可达性 = 数据完整性的一半）。

本层是这一批的**核心证据层**，不是旁证：两个 finding 的机理都长在 Mongo 自己的
语义上，fake 后端结构性地给不出结论——

  - **BSON 类型括号**：`{updated_at: {$lt: "<str>"}}` 对缺失 / `null` / Date / 数字
    **永不匹配**；`{"$gt": ""}` 只匹配"非空字符串"；`$not` 的补集连"字段不存在"一起覆盖。
  - **BSON 排序序**：Date > Bool > ObjectId > String > Number > Null > missing
    ——所以修前一篇 Date 型 `updated_at` 会排在所有健康记录**之前**，占住页边界。
  - **upsert 并发**：没有唯一索引时两个并发 upsert 会各自插入（真库实测出现重复）；
    有唯一索引时服务端把 loser 的插入重试为 update（最终恒 1 篇，0 异常）。
  - **索引选项不能原地改**：同名请求 `unique=True` 抛 code 86；已有重复时 build
    unique 抛 code 11000，且会把已 drop 的索引留在"没了"的状态。

隔离：沿用本层约定（`conftest.py`）——独立测试库、进入/退出各 drop 一次，
库名必须含 `test`，仓储一律经 `real_mongo_repo` 工厂构造。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.memory.session_manager import SessionManager
from core.schema.types import SessionId
from core.storage.message_repo import MessageRepository
from core.storage.session_repo import SessionRepository
from tests.real_mongo._session_data_support import (
    COLL,
    seed_legacy as _seed_legacy,
)
from tests.real_mongo._session_data_support import ts as _ts
from tests.real_mongo._session_data_support import walk as _walk

pytestmark = [pytest.mark.external, pytest.mark.real_mongo]


# ── KB-15：可达性（真实 BSON 排序 / 类型括号） ─────────────────────────────


class TestLegacyOrderingReachabilityRealMongo:
    @pytest.mark.asyncio
    async def test_mixed_metadata_dataset_is_fully_enumerable(self, real_mongo_backend,
                                                             real_mongo_repo):
        """205 健康 + 3 无排序键：逐页走遍必须不重不漏（KB-15 核心，真库）。"""
        valid = [f"conv_e3_v{i:04d}" for i in range(205)]
        for i, sid in enumerate(valid):
            await real_mongo_backend.insert_one(COLL, {
                "session_id": sid, "title": sid, "messages": [],
                "updated_at": _ts(i)})
        legacy = await _seed_legacy(real_mongo_backend)
        repo = await real_mongo_repo(SessionRepository)

        seen = await _walk(repo)

        assert set(seen) == set(valid) | set(legacy), "真库上仍有不可达的会话"
        assert len(seen) == len(set(seen))
        assert seen[-4:] == ["conv_e3_array", "conv_e3_empty",
                             "conv_e3_missing", "conv_e3_null"]

    @pytest.mark.asyncio
    async def test_wrong_bson_type_rows_are_reachable_and_ordered_last(
            self, real_mongo_backend, real_mongo_repo):
        """BSON Date / 数字型 `updated_at`（外部工具可能写出）也必须可达、排最后。

        修前 Date 型排在字符串**之前**（BSON 序），落在页边界时游标无法解码。
        """
        for i in range(60):
            await real_mongo_backend.insert_one(COLL, {
                "session_id": f"conv_e3_t{i:03d}", "messages": [],
                "updated_at": _ts(i)})
        await real_mongo_backend.insert_one(COLL, {
            "session_id": "conv_e3_date", "messages": [],
            "updated_at": datetime(2026, 9, 1, tzinfo=timezone.utc)})
        await real_mongo_backend.insert_one(COLL, {
            "session_id": "conv_e3_int", "messages": [], "updated_at": 1756735200})
        repo = await real_mongo_repo(SessionRepository)

        seen = await _walk(repo, page_size=16)

        assert seen[-2:] == ["conv_e3_date", "conv_e3_int"]
        assert len(seen) == 62

    @pytest.mark.asyncio
    async def test_legacy_row_on_page_boundary_does_not_truncate_the_list(
            self, real_mongo_backend, real_mongo_repo):
        """页边界落在无排序键的记录上：续页必须继续（修前 400 / 截断）。"""
        for i in range(4):
            await real_mongo_backend.insert_one(COLL, {
                "session_id": f"conv_e3_b{i}", "messages": [], "updated_at": _ts(i)})
        await _seed_legacy(real_mongo_backend)
        repo = await real_mongo_repo(SessionRepository)

        page1 = await repo.list_page(limit=6)
        # 4 条有序 + legacy 桶前 2 条（桶内 `session_id ASC`：array < empty < missing < null）
        assert [str(s.session_id) for s in page1] == [
            "conv_e3_b3", "conv_e3_b2", "conv_e3_b1", "conv_e3_b0",
            "conv_e3_array", "conv_e3_empty"]
        assert page1[-1].updated_at == ""
        boundary = page1[-1]

        page2 = await repo.list_page(
            limit=6, after=(boundary.updated_at, str(boundary.session_id)))

        assert [str(s.session_id) for s in page2] == ["conv_e3_missing", "conv_e3_null"]
        page3 = await repo.list_page(limit=6, after=(
            page2[-1].updated_at, str(page2[-1].session_id)))
        assert page3 == []

    @pytest.mark.asyncio
    async def test_traversal_order_is_stable_across_runs(self, real_mongo_backend,
                                                        real_mongo_repo):
        """同一稳定数据集两次完整遍历：顺序逐项一致（禁止读时 now() 之类 fallback）。"""
        for i in range(80):
            await real_mongo_backend.insert_one(COLL, {
                "session_id": f"conv_e3_s{i:03d}", "messages": [],
                "updated_at": _ts(i % 60)})
        await _seed_legacy(real_mongo_backend)
        repo = await real_mongo_repo(SessionRepository)

        assert await _walk(repo, page_size=13) == await _walk(repo, page_size=13)

    @pytest.mark.asyncio
    async def test_direct_lookup_still_works_for_legacy_rows(self, real_mongo_backend,
                                                            real_mongo_repo):
        """SD-T5：无排序键不得影响 `get_summary` / `load` 这类**直读**路径。"""
        await _seed_legacy(real_mongo_backend)
        repo = await real_mongo_repo(SessionRepository)

        summary = await repo.get_summary(SessionId("conv_e3_missing"))

        assert summary is not None
        assert str(summary.session_id) == "conv_e3_missing"
        assert summary.updated_at == "", "无排序键在投影层必须是 Sentinel ''"
        loaded = await repo.load(SessionId("conv_e3_null"))
        assert loaded is not None, "直读不受分页排序域影响"

    @pytest.mark.asyncio
    async def test_search_reaches_legacy_rows(self, real_mongo_backend, real_mongo_repo):
        """搜索路径同样覆盖 legacy 桶（真库 `$regex` + 桶判据的组合）。"""
        for i in range(30):
            await real_mongo_backend.insert_one(COLL, {
                "session_id": f"conv_e3_q{i:02d}", "title": "普通标题",
                "messages": [], "updated_at": _ts(i)})
        await real_mongo_backend.insert_one(COLL, {
            "session_id": "conv_e3_qq", "title": "唯一关键词喵", "messages": []})
        repo = await real_mongo_repo(SessionRepository)

        seen = await _walk(repo, page_size=7)
        hits = [str(s.session_id) for s in await repo.list_page(limit=50, search="唯一关键词喵")]

        assert "conv_e3_qq" in seen
        assert hits == ["conv_e3_qq"]

    @pytest.mark.asyncio
    async def test_ordered_query_still_uses_the_compound_index(self, real_mongo_backend,
                                                              real_mongo_repo):
        """§72：加桶判据后，正常路径仍必须是**索引序**（无阻塞排序、无全表扫描）。"""
        for i in range(300):
            await real_mongo_backend.insert_one(COLL, {
                "session_id": f"conv_e3_x{i:04d}", "messages": [],
                "updated_at": _ts(i % 60)})
        await _seed_legacy(real_mongo_backend)
        await real_mongo_repo(SessionRepository)

        coll = real_mongo_backend._client[real_mongo_backend._db.name][COLL]  # noqa: SLF001
        cutoff = "2026-08-01T30:00:00+00:00"
        plan = await coll.find({
            "updated_at": {"$gt": ""},
            "$or": [{"updated_at": {"$lt": cutoff}},
                    {"updated_at": cutoff, "session_id": {"$gt": "conv_e3_x0000"}}],
        }).sort([("updated_at", -1), ("session_id", 1)]).limit(51).explain()
        text = str(plan.get("queryPlanner", {}).get("winningPlan", {}))

        assert "IXSCAN" in text and "updated_at_-1_session_id_1" in text
        assert "'SORT'" not in text and '"SORT"' not in text, "退化成阻塞排序"
        stats = plan.get("executionStats", {})
        assert stats.get("totalDocsExamined") == 51, "应只读一页的文档"


    @pytest.mark.asyncio
    async def test_session_manager_pagination_over_real_driver(self, real_mongo_backend,
                                                              real_mongo_repo):
        """端到端（manager 层）：真驱动 + 混合元数据 + 完整遍历。"""
        for i in range(120):
            await real_mongo_backend.insert_one(COLL, {
                "session_id": f"conv_e3_m{i:03d}", "title": f"会话{i:03d}",
                "messages": [], "updated_at": _ts(i % 60)})
        legacy = await _seed_legacy(real_mongo_backend)
        repo = await real_mongo_repo(SessionRepository)
        manager = SessionManager(repo, MessageRepository(db=real_mongo_backend))
        seen: list[str] = []
        cursor: str | None = None
        for _ in range(50):
            items, more, cursor = await manager.list(cursor=cursor, limit=17)
            seen.extend(str(s.session_id) for s in items)
            if not more:
                break
        else:
            raise AssertionError("分页不收敛")

        # manager 层还带 clean-start 的本地 `default` 摘要（刻意留在覆盖层）
        assert set(seen) - {"default"} == {f"conv_e3_m{i:03d}" for i in range(120)} | set(legacy)
        assert len(seen) == len(set(seen))


class TestArrayValuedOrderingMetadataRealMongo:
    """Challenger A 反证的真库版本：**数组型** `updated_at` 不得跳过有序桶。

    修前/中间态（未排除数组时）真库实测：数组文档排在最前、落在页边界 ⇒
    游标 Sentinel 被误读成"已在 legacy 桶" ⇒ 有序桶整段不可达（`has_more` 撒谎）。
    """

    @pytest.mark.asyncio
    async def test_array_row_lands_in_the_legacy_bucket(self, real_mongo_backend,
                                                       real_mongo_repo):
        for i in range(3):
            await real_mongo_backend.insert_one(COLL, {
                "session_id": f"conv_e3_ar{i}", "messages": [], "updated_at": _ts(i)})
        await real_mongo_backend.insert_one(COLL, {
            "session_id": "conv_e3_arr", "messages": [],
            "updated_at": ["1990-01-01T00:00:00+00:00", "2040-01-01T00:00:00+00:00"]})
        repo = await real_mongo_repo(SessionRepository)

        page = await repo.list_page(limit=50)

        assert [str(s.session_id) for s in page] == [
            "conv_e3_ar2", "conv_e3_ar1", "conv_e3_ar0", "conv_e3_arr"]
        assert page[-1].updated_at == ""

    @pytest.mark.asyncio
    async def test_array_row_on_page_boundary_does_not_skip_the_ordered_bucket(
            self, real_mongo_backend, real_mongo_repo):
        """`limit=1` 逐页走到底：有序桶三条必须都在数组文档**之前**出现。"""
        for i in range(3):
            await real_mongo_backend.insert_one(COLL, {
                "session_id": f"conv_e3_ap{i}", "messages": [], "updated_at": _ts(i)})
        await real_mongo_backend.insert_one(COLL, {
            "session_id": "conv_e3_arrb", "messages": [],
            "updated_at": ["1990-01-01T00:00:00+00:00", "2040-01-01T00:00:00+00:00"]})
        repo = await real_mongo_repo(SessionRepository)

        seen: list[str] = []
        after: tuple[str, str] | None = None
        for _ in range(10):
            page = await repo.list_page(limit=1, after=after)
            seen.extend(str(s.session_id) for s in page)
            if not page:
                break
            last = page[-1]
            after = (last.updated_at, str(last.session_id))

        assert seen == ["conv_e3_ap2", "conv_e3_ap1", "conv_e3_ap0", "conv_e3_arrb"]
        assert len(seen) == len(set(seen))
