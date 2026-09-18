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
        """排序（含 tie-break）与游标谓词必须在**查询**里——fake 的稳定排序掩盖不了。"""
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
        assert calls[0]["filter"] == {"$or": [
            {"updated_at": {"$lt": "2026-08-01T00:00:02+00:00"}},
            {"updated_at": "2026-08-01T00:00:02+00:00",
             "session_id": {"$gt": "conv_push_02"}},
        ]}

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
