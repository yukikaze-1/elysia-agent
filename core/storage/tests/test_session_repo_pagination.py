"""Batch 8 / P2-01r：`SessionRepository` **存储层分页**（unit）。

固定 root cause 层：修前用户列表的数据源是 `list_all()` 的
`find({}, sort=[("updated_at", -1)], limit=200)` ——**单页上限变成了数据集上限**，
第 201+ 条永远出不了存储层；service 层在 ≤200 的列表里做游标/搜索只是在这个
被截断的窗口内部分页。

本文件钉住存储层原语（`list_page` / `get_summary` / `summaries_by_ids`）：
  - keyset 谓词与排序必须**下推**到存储（`updated_at DESC, session_id ASC`，
    tie-break 也必须在查询里——fake 的稳定排序会掩盖真实 Mongo 在
    `updated_at` 相同值上的不确定性）；
  - 单页读**不得**退化成全量读（断言传给后端的 `limit`）；
  - 读路径 fail-closed 与同仓其余读一致。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.schema.types import SessionId
from core.storage.base import StorageUnavailableError
from core.storage.session_repo import SessionRepository
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

pytestmark = pytest.mark.unit


def _ts(second: int, *, day: int = 1) -> str:
    return f"2026-08-{day:02d}T00:{second // 60:02d}:{second % 60:02d}+00:00"


async def _seed(backend: FakeDocumentDBBackend, count: int, *,
                prefix: str = "conv_seed", day: int = 1) -> list[str]:
    ids: list[str] = []
    for i in range(count):
        sid = f"{prefix}_{i:03d}"
        await backend.insert_one("sessions", {
            "session_id": sid,
            "title": f"会话{i:03d}",
            "messages": [],
            "character_binding": {},
            "updated_at": _ts(i, day=day),
        })
        ids.append(sid)
    return ids


async def _walk(repo: SessionRepository, *, page_size: int = 50,
                search: str = "") -> list[SessionId]:
    """按 keyset 合同逐页读到底（只用返回的记录推进游标）。"""
    seen: list[SessionId] = []
    after: tuple[str, str] | None = None
    for _ in range(1000):
        page = await repo.list_page(limit=page_size, after=after, search=search)
        seen.extend(s.session_id for s in page)
        if len(page) < page_size:
            return seen
        last = page[-1]
        after = (last.updated_at, str(last.session_id))
    raise AssertionError("分页不收敛")


# ── 完整性 ─────────────────────────────────────────────────────────────────


class TestStorageLevelPagination:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("count", [0, 1, 50, 51, 199, 200, 201, 225])
    async def test_walk_reaches_every_record(self, count: int):
        """N ∈ {0,1,page,page+1,199,200,201,>201}：逐页走遍，不重不漏。"""
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        seeded = await _seed(backend, count, prefix=f"conv_{count}")

        seen = await _walk(repo)

        assert [str(s) for s in seen] == sorted(seeded, reverse=True) or count == 0
        assert set(str(s) for s in seen) == set(seeded)
        assert len(seen) == len(set(seen))

    @pytest.mark.asyncio
    async def test_page_boundary_never_becomes_dataset_boundary(self):
        """201 条：第一页之后**每一页**都必须继续给数据，直到第 201 条。"""
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        seeded = await _seed(backend, 201, prefix="conv_b201")

        page1 = await repo.list_page(limit=100)
        last = page1[-1]
        page2 = await repo.list_page(limit=100, after=(last.updated_at, str(last.session_id)))
        last2 = page2[-1]
        page3 = await repo.list_page(limit=100, after=(last2.updated_at, str(last2.session_id)))

        assert [len(page1), len(page2), len(page3)] == [100, 100, 1]
        assert set(str(s.session_id) for s in page1 + page2 + page3) == set(seeded)

    @pytest.mark.asyncio
    async def test_identical_timestamps_tie_break_across_boundaries(self):
        """主排序键重复：tie-break（session_id ASC）必须在页边界同样成立。"""
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        for sid in ("conv_x", "conv_a", "conv_m", "conv_b", "conv_z"):
            await backend.insert_one("sessions", {
                "session_id": sid, "title": sid, "messages": [],
                "character_binding": {}, "updated_at": _ts(0),
            })

        seen = await _walk(repo, page_size=2)

        assert [str(s) for s in seen] == ["conv_a", "conv_b", "conv_m", "conv_x", "conv_z"]

    @pytest.mark.asyncio
    async def test_search_is_pushed_down_and_pageable(self):
        """search 下推到存储（设计文档 §4「search 对 title $regex」），且可继续翻页。"""
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        hits = await _seed(backend, 150, prefix="conv_hit")
        misses = await _seed(backend, 150, prefix="conv_other", day=2)
        for sid in misses:
            await backend.update_one("sessions", {"session_id": sid},
                                     {"$set": {"title": "别的标题"}})

        seen = await _walk(repo, page_size=40, search="会话")

        assert set(str(s) for s in seen) == set(hits)
        assert len(seen) == 150

    @pytest.mark.asyncio
    async def test_oversized_search_term_matches_nothing(self):
        """超长搜索词按"无匹配"处理（与消息/角色搜索同一口径）——**不能忽略过滤**。"""
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        await _seed(backend, 3, prefix="conv_long")

        assert await repo.list_page(limit=10, search="x" * 201) == []
        assert len(await repo.list_page(limit=10, search="会话")) == 3


# ── 单页上限 / 不整表扫描（PAG-2，§20/§44） ─────────────────────────────────


class TestQueryShape:
    @pytest.mark.asyncio
    async def test_page_read_never_fetches_more_than_the_page(self):
        """传给后端的 `limit` 必须与页大小同阶——**不得**退化成"取全量再切片"。"""
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        await _seed(backend, 500, prefix="conv_many")
        calls: list[dict[str, Any]] = []
        original = backend.find

        async def _recording_find(collection: str, filter_expr: dict, **kwargs: Any):
            calls.append({"filter": filter_expr, "sort": kwargs.get("sort"),
                          "limit": kwargs.get("limit")})
            return await original(collection, filter_expr, **kwargs)

        backend.find = _recording_find  # type: ignore[method-assign]

        await repo.list_page(limit=50)

        assert calls, "分页读必须真的查后端"
        assert max(c["limit"] for c in calls) <= 50, f"单页读取超量：{calls}"

    @pytest.mark.asyncio
    async def test_sort_and_cursor_predicate_are_pushed_down(self):
        """排序（含 tie-break）与游标谓词必须在**查询**里——fake 的稳定排序掩盖不了。

        批次 16 / KB-15 起，有序桶的查询还带**桶判据**（`ORDERED_TS_FILTER`：
        `$type: "string"` + `$gt: ""` + `$not: {$type: "array"}`）——它是排序键
        **全域定义**的一部分，必须和谓词一起下推，不能在 Python 里筛。
        `$not: {$type: "array"}` 是承重条件：Mongo 的 `$type`/比较算子会遍历数组
        元素，不排除数组就会出现"有序桶文档投影成 Sentinel"与"游标 Sentinel 表示
        legacy 桶"撞车（Challenger A 的数组反证）。
        """
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        await _seed(backend, 5, prefix="conv_push")
        calls: list[dict[str, Any]] = []
        original = backend.find

        async def _recording_find(collection: str, filter_expr: dict, **kwargs: Any):
            calls.append({"filter": filter_expr, "sort": kwargs.get("sort")})
            return await original(collection, filter_expr, **kwargs)

        backend.find = _recording_find  # type: ignore[method-assign]

        await repo.list_page(limit=2, after=("2026-08-01T00:00:02+00:00", "conv_push_02"))

        assert calls[0]["sort"] == [("updated_at", -1), ("session_id", 1)]
        assert calls[0]["filter"] == {
            "updated_at": {"$type": "string", "$gt": "",
                           "$not": {"$type": "array"}},
            "$or": [
                {"updated_at": {"$lt": "2026-08-01T00:00:02+00:00"}},
                {"updated_at": "2026-08-01T00:00:02+00:00",
                 "session_id": {"$gt": "conv_push_02"}},
            ],
        }

    @pytest.mark.asyncio
    async def test_search_clause_is_pushed_down_escaped(self):
        """搜索下推且**转义**（用户输入不得当正则执行）。"""
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        await backend.insert_one("sessions", {
            "session_id": "conv_re", "title": "a.b", "messages": [],
            "character_binding": {}, "updated_at": _ts(0),
        })
        await backend.insert_one("sessions", {
            "session_id": "conv_re2", "title": "axb", "messages": [],
            "character_binding": {}, "updated_at": _ts(1),
        })

        page = await repo.list_page(limit=10, search="a.b")

        assert [str(s.session_id) for s in page] == ["conv_re"]


# ── 单文档 / 批量读（P2-01r 的第二出口） ───────────────────────────────────


class TestSummaryReads:
    @pytest.mark.asyncio
    async def test_get_summary_reads_beyond_the_old_window(self):
        """第 201+ 条会话的单文档摘要必须可读（详情页不再兜底成 updatedAt=""）。"""
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        seeded = await _seed(backend, 201, prefix="conv_sum")

        oldest = await repo.get_summary(SessionId(seeded[0]))
        missing = await repo.get_summary(SessionId("conv_nope"))

        assert oldest is not None
        assert oldest.updated_at == _ts(0)
        assert oldest.title == "会话000"
        assert missing is None

    @pytest.mark.asyncio
    async def test_summaries_by_ids_only_returns_matching(self):
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        await _seed(backend, 5, prefix="conv_by")

        got = await repo.summaries_by_ids(
            [SessionId("conv_by_000"), SessionId("conv_by_004"), SessionId("conv_missing")])

        assert set(str(k) for k in got) == {"conv_by_000", "conv_by_004"}

    @pytest.mark.asyncio
    async def test_summaries_by_ids_handles_more_than_backend_default_limit(self):
        """批量读必须显式传 limit：后端默认 limit=100，覆盖层多于 100 条时会被截断。"""
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        seeded = await _seed(backend, 150, prefix="conv_many")

        got = await repo.summaries_by_ids([SessionId(s) for s in seeded])

        assert len(got) == 150

    @pytest.mark.asyncio
    async def test_summaries_by_ids_empty_input_does_not_query(self):
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        calls: list[str] = []
        original = backend.find

        async def _recording_find(collection: str, filter_expr: dict, **kwargs: Any):
            calls.append(collection)
            return await original(collection, filter_expr, **kwargs)

        backend.find = _recording_find  # type: ignore[method-assign]

        assert await repo.summaries_by_ids([]) == {}
        assert calls == []

    @pytest.mark.asyncio
    async def test_list_all_stays_a_bounded_diagnostic_read(self):
        """`list_all` 保持 ≤200 的**有界诊断读**（debug 面板专用，非用户列表）。

        用户列表不得再依赖它——分页走 `list_page`（否则 200 又变成数据集上限）。
        """
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        await _seed(backend, 205, prefix="conv_diag")

        assert len(await repo.list_all()) == 200


# ── fail-closed（与同仓其余读同向） ────────────────────────────────────────


class TestFailClosed:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("method,kwargs", [
        ("list_page", {"limit": 10}),
        ("get_summary", {"session_id": SessionId("s1")}),
        ("summaries_by_ids", {"session_ids": [SessionId("s1")]}),
    ])
    async def test_new_reads_are_fail_closed_when_offline(self, method: str,
                                                          kwargs: dict[str, Any]):
        """离线 ≠ 空结果：三条新读路径都必须抛 StorageUnavailableError。"""
        repo = SessionRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            await getattr(repo, method)(**kwargs)

    @pytest.mark.asyncio
    async def test_zero_limit_returns_empty_without_query(self):
        """limit<=0 直接返回空列表：真后端 `.limit(0)` 语义是"不限制"、`.limit(-5)`
        是"取 5 条并立即关闭游标"，两者都不是"空页"——所以必须在进后端**之前**兜住。
        """
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        await _seed(backend, 3, prefix="conv_zero")

        assert await repo.list_page(limit=0) == []
        assert await repo.list_page(limit=-5) == []


# ── 批次 16 / KB-15：排序键的 legacy 桶（无可用 updated_at 的文档） ─────────


def _legacy_docs() -> list[dict]:
    """"没有可用排序键"的历史文档（真库 E3 里各形态的代表）。

    **数组型是承重用例**（Challenger A 反证）：Mongo 的比较/`$type` 算子会**遍历
    数组元素**，`updated_at: ["1990-…","2040-…"]` 因此既匹配 `$gt: ""`（按最大元素
    排序）又被投影成 Sentinel `""` —— 桶判据必须用 `$not: {$type: "array"}` 把它
    挡在有序桶之外，否则游标 Sentinel 会与"legacy 桶位置"撞车。
    """
    return [
        {"session_id": "conv_leg_missing", "title": "缺字段", "messages": []},
        {"session_id": "conv_leg_null", "title": "null", "messages": [],
         "updated_at": None},
        {"session_id": "conv_leg_empty", "title": "空串", "messages": [],
         "updated_at": ""},
        {"session_id": "conv_leg_array", "title": "数组", "messages": [],
         "updated_at": ["1990-01-01T00:00:00+00:00", "2040-01-01T00:00:00+00:00"]},
    ]


class TestLegacyOrderingBucket:
    """`updated_at` 不是非空字符串的文档，必须在**同一套排序契约**里可达。

    修前：`(updated_at < U)` 对缺失/null 永不匹配（类型括号）⇒ 静默漏项；
    BSON Date 型又排在字符串之前 ⇒ 落在页边界时游标解码不了（下一页 400）。
    现在两个桶拼起来覆盖整个集合，且桶内顺序只依赖持久化的 `session_id`。
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("count", [0, 1, 49, 50, 51, 205])
    async def test_legacy_rows_are_reachable_for_every_dataset_size(self, count: int):
        """N ∈ {0,1,49,50,51,205} 有序列 + 3 条 legacy：完整遍历不重不漏。"""
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        seeded = await _seed(backend, count, prefix=f"conv_leg{count}")
        for doc in _legacy_docs():
            await backend.insert_one("sessions", dict(doc))

        seen = [str(s) for s in await _walk(repo, page_size=50)]
        legacy_ids = [str(d["session_id"]) for d in _legacy_docs()]

        assert set(seen) == set(seeded) | set(legacy_ids)
        assert len(seen) == len(set(seen)), "遍历出现重复"
        # legacy 桶整体排在有序桶之后，桶内 `session_id ASC`
        assert seen[-len(legacy_ids):] == sorted(legacy_ids)

    @pytest.mark.asyncio
    async def test_legacy_bucket_is_deterministic_across_runs(self):
        """同一数据集连续两次完整遍历：顺序必须逐项一致（禁止 now() 之类 fallback）。"""
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        await _seed(backend, 120, prefix="conv_det")
        for doc in reversed(_legacy_docs()):     # 插入序与目标序相反
            await backend.insert_one("sessions", dict(doc))

        first = [str(s) for s in await _walk(repo, page_size=25)]
        second = [str(s) for s in await _walk(repo, page_size=25)]

        assert first == second

    @pytest.mark.asyncio
    async def test_legacy_row_at_page_boundary_does_not_poison_the_cursor(self):
        """页边界正好落在一篇"无排序键"的文档上时，续页不得漏项/报错（KB-15 核心）。"""
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        seeded = await _seed(backend, 2, prefix="conv_pb")
        for doc in _legacy_docs():
            await backend.insert_one("sessions", dict(doc))

        # limit=3：有序列只有 2 条 ⇒ 第 3 条正好是 legacy 桶的第一条
        # （Sentinel 键 "" 落在页边界上——修前这里会产生解码不了的游标）
        page1 = await repo.list_page(limit=3)
        assert [str(s.session_id) for s in page1] == [
            "conv_pb_001", "conv_pb_000", "conv_leg_array"]
        boundary = page1[-1]
        assert boundary.updated_at == "", "legacy 文档的排序键必须是 Sentinel ''"

        page2 = await repo.list_page(
            limit=3, after=(boundary.updated_at, str(boundary.session_id)))

        seen = [str(s.session_id) for s in page1 + page2]
        assert seen == [
            "conv_pb_001", "conv_pb_000",
            "conv_leg_array", "conv_leg_empty", "conv_leg_missing", "conv_leg_null"]
        assert set(seen) == set(seeded) | {str(d["session_id"]) for d in _legacy_docs()}
        assert len(seen) == len(set(seen))

    @pytest.mark.asyncio
    async def test_wrong_bson_type_rows_join_the_legacy_bucket(self):
        """BSON Date / 数字型 `updated_at`（外部工具可能写出）同样可达且排最后。

        修前它们更危险：Date 在 BSON 序里**排在字符串之前**，会占住第一页的
        边界并把游标写成解码不了的空时间戳。
        """
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        seeded = await _seed(backend, 60, prefix="conv_type")
        from datetime import datetime, timezone
        await backend.insert_one("sessions", {
            "session_id": "conv_type_date", "title": "Date 型", "messages": [],
            "updated_at": datetime(2026, 9, 1, tzinfo=timezone.utc)})
        await backend.insert_one("sessions", {
            "session_id": "conv_type_int", "title": "数字型", "messages": [],
            "updated_at": 1756735200})

        seen = [str(s) for s in await _walk(repo, page_size=16)]

        assert set(seen) == set(seeded) | {"conv_type_date", "conv_type_int"}
        assert seen[-2:] == ["conv_type_date", "conv_type_int"]
        assert len(seen) == len(set(seen))

    @pytest.mark.asyncio
    async def test_legacy_rows_are_reachable_through_search(self):
        """搜索路径同样覆盖 legacy 桶（§73：search 不得只覆盖有序桶）。

        命中数必须**多于一页**：只有一条命中时它天然落在第一页，修前的缺陷看不见。
        """
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        hits = await _seed(backend, 30, prefix="conv_s")
        for sid in hits:
            await backend.update_one("sessions", {"session_id": sid},
                                     {"$set": {"title": "找得到我"}})
        await backend.insert_one("sessions", {
            "session_id": "conv_leg_hit", "title": "找得到我", "messages": []})

        seen = [str(s) for s in await _walk(repo, page_size=7, search="找得到我")]

        assert set(seen) == set(hits) | {"conv_leg_hit"}
        assert seen[-1] == "conv_leg_hit"

    @pytest.mark.asyncio
    async def test_ordered_bucket_order_is_unchanged_for_healthy_data(self):
        """ALREADY_GREEN_GUARD：全部健康时顺序与修前**逐项相同**（不许顺手改序）。"""
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        await _seed(backend, 205, prefix="conv_same")

        seen = [str(s) for s in await _walk(repo, page_size=50)]

        assert seen == [f"conv_same_{i:03d}" for i in range(204, -1, -1)]

    @pytest.mark.asyncio
    async def test_list_all_includes_legacy_rows_without_exceeding_cap(self):
        """诊断读同样不把 legacy 文档藏起来，且上界仍是 200（两件事分开断言）。"""
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        await _seed(backend, 30, prefix="conv_diag2")
        for doc in _legacy_docs():
            await backend.insert_one("sessions", dict(doc))

        listed = [str(s.session_id) for s in await repo.list_all()]

        assert listed[-3:] == ["conv_leg_empty", "conv_leg_missing", "conv_leg_null"]

        big = FakeDocumentDBBackend()
        big_repo = SessionRepository(db=big)
        await _seed(big, 205, prefix="conv_cap")
        for doc in _legacy_docs():
            await big.insert_one("sessions", dict(doc))

        capped = await big_repo.list_all()

        assert len(capped) == 200
        assert [str(s.session_id) for s in capped][:2] == ["conv_cap_204", "conv_cap_203"]

    @pytest.mark.asyncio
    async def test_small_dataset_shows_legacy_rows_in_the_same_page(self):
        """数据集装得下一页时（修前唯一能看到它们的形态）：顺序仍是"有序列+legacy 尾"。"""
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        seeded = await _seed(backend, 3, prefix="conv_small")
        for doc in _legacy_docs():
            await backend.insert_one("sessions", dict(doc))

        page = await repo.list_page(limit=50)

        assert [str(s.session_id) for s in page] == [
            "conv_small_002", "conv_small_001", "conv_small_000",
            "conv_leg_array", "conv_leg_empty", "conv_leg_missing", "conv_leg_null",
        ]
        assert set(seeded) <= {str(s.session_id) for s in page}

    @pytest.mark.asyncio
    async def test_legacy_only_dataset_terminates_deterministically(self):
        """Challenger Attack B：**全部**都是 legacy 文档也必须确定性收敛，不空转。"""
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        ids = [f"conv_only_{i:02d}" for i in range(7)]
        for sid in reversed(ids):
            await backend.insert_one("sessions", {"session_id": sid, "messages": []})

        first = [str(s) for s in await _walk(repo, page_size=2)]
        second = [str(s) for s in await _walk(repo, page_size=2)]

        assert first == sorted(ids)
        assert first == second


class TestArrayValuedOrderingMetadata:
    """Challenger A 反证：**数组型** `updated_at` 必须落 legacy 桶（承重用例）。

    Mongo 的比较/`$type` 算子会**遍历数组元素**：`updated_at: ["1990-…","2040-…"]`
    既匹配 `$gt: ""`（并按**最大元素**排序，于是排在最前），又被投影层归一成
    Sentinel `""`。若不排除数组，游标里的 `""` 就会被误读成"已在 legacy 桶"
    ⇒ **整个有序桶被跳过**（漏项 + 重复 + `has_more` 撒谎），比修前的 400 更隐蔽。
    """

    @pytest.mark.asyncio
    async def test_array_row_goes_to_the_legacy_bucket(self):
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        await _seed(backend, 3, prefix="conv_arr")
        await backend.insert_one("sessions", {
            "session_id": "conv_arr_bad", "title": "数组型", "messages": [],
            "updated_at": ["1990-01-01T00:00:00+00:00", "2040-01-01T00:00:00+00:00"]})

        page = await repo.list_page(limit=50)

        assert [str(s.session_id) for s in page] == [
            "conv_arr_002", "conv_arr_001", "conv_arr_000", "conv_arr_bad"]
        assert page[-1].updated_at == "", "数组型必须投影成 Sentinel 且排在最后"

    @pytest.mark.asyncio
    async def test_array_row_at_page_boundary_does_not_skip_the_ordered_bucket(self):
        """页边界正好落在数组型文档上时：续页必须为空，且有序桶**已经全部拿到**。

        修前（Challenger A 实测）：`limit=1` 只拿到数组型文档、`has_more` 撒谎；
        本用例把它钉死——有序桶的三条必须在前，数组型必须最后。
        """
        backend = FakeDocumentDBBackend()
        repo = SessionRepository(db=backend)
        seeded = await _seed(backend, 3, prefix="conv_ab")
        await backend.insert_one("sessions", {
            "session_id": "conv_ab_bad", "messages": [],
            "updated_at": ["1990-01-01T00:00:00+00:00", "2040-01-01T00:00:00+00:00"]})

        seen: list[str] = []
        after: tuple[str, str] | None = None
        for _ in range(10):
            page = await repo.list_page(limit=1, after=after)
            seen.extend(str(s.session_id) for s in page)
            if not page:
                break
            last = page[-1]
            after = (last.updated_at, str(last.session_id))

        assert seen == ["conv_ab_002", "conv_ab_001", "conv_ab_000", "conv_ab_bad"]
        assert set(seen) == set(seeded) | {"conv_ab_bad"}
        assert len(seen) == len(set(seen)), "数组型文档不得让记录重复出现"
