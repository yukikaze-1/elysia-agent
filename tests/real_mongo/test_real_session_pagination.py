# 测试层：tests/real_mongo/test_real_session_pagination.py
"""真实 MongoDB 层：会话列表分页（批次 8 / P2-01r）的**驱动级**证明（E3）。

**为什么必须来这一层**（对齐本层既有分工）：内存 `FakeDocumentDBBackend` 的排序是
Python Timsort（同值稳定 ⇒ 保留插入顺序），而真实 MongoDB 对相等的排序键**不作任何
顺序保证**。于是"keyset 分页在真驱动上不重不漏"这件事只能由本层回答：

  ① `$or` 谓词 + 双键排序（`updated_at DESC, session_id ASC`）+ `limit` 在真驱动上
     确实构成严格递进的游标；
  ② 大量记录 `updated_at` 相同时，顺序由**存储**的 tie-break 决定（插入顺序被刻意
     打乱，期望顺序是字典序）——内存 fake 无法证伪这一点；
  ③ 第 201+ 条记录在真库上可达（修前 `limit=200` 预取让它们永远出不来）；
  ④ 单文档摘要读（`get_summary`）与列表窗口无关。

播种一律经 `real_mongo_backend`（进入即 drop 测试库）写入 sessions doc；被测仓储一律
经 `real_mongo_repo` 工厂构造（强制 `database_name=<测试库>` 并核验落点）。
"""

from __future__ import annotations

import pytest

from core.schema.types import SessionId
from core.storage.session_repo import SessionRepository

pytestmark = [
    pytest.mark.external,      # opt-in 层：默认任何档位都不跑（Normal = not external）
    pytest.mark.real_mongo,    # 本层专属 marker：CI 的 real-mongo job 用 -m real_mongo 选中
]

#: 本文件所有测试共用的页大小。
PAGE = 50


def _ts(index: int) -> str:
    """确定性时间戳（UTC，字典序 = 时间序，与 `now_iso()` 同形）。"""
    return f"2026-08-01T00:{index // 60:02d}:{index % 60:02d}.000000+00:00"


async def _seed(backend, count: int, *, prefix: str = "conv_rp", same_ts: bool = False,
                title: str = "实库会话") -> list[str]:
    """直接写 sessions doc（与 `SessionRepository.create` 同形）。"""
    ids: list[str] = []
    for i in range(count):
        sid = f"{prefix}_{i:04d}"
        await backend.insert_one("sessions", {
            "session_id": sid,
            "title": f"{title}{i:04d}",
            "messages": [],
            "character_binding": {},
            "updated_at": _ts(0 if same_ts else i),
        })
        ids.append(sid)
    return ids


async def _walk(repo: SessionRepository, *, page_size: int = PAGE,
                search: str = "") -> list[str]:
    """只用返回记录推进 keyset 游标，直到拿不满一页。"""
    seen: list[str] = []
    after: tuple[str, str] | None = None
    for _ in range(200):
        page = await repo.list_page(limit=page_size, after=after, search=search)
        seen.extend(str(s.session_id) for s in page)
        if len(page) < page_size:
            return seen
        after = (page[-1].updated_at, str(page[-1].session_id))
    raise AssertionError("真库分页不收敛")


class TestRealMongoKeysetPagination:
    @pytest.mark.asyncio
    async def test_every_record_beyond_two_hundred_is_reachable(self, real_mongo_backend,
                                                               real_mongo_repo):
        """201 条：真驱动上逐页走遍，第 201 条（最旧）必须可达、且不重不漏。"""
        seeded = await _seed(real_mongo_backend, 201)
        repo = await real_mongo_repo(SessionRepository)

        seen = await _walk(repo)

        assert len(seen) == 201, f"真库分页只拿到 {len(seen)}/201"
        assert len(set(seen)) == 201, "真库分页出现重复记录"
        assert set(seen) == set(seeded)

    @pytest.mark.asyncio
    async def test_two_hundred_and_twenty_five_pages_without_loss(self, real_mongo_backend,
                                                                 real_mongo_repo):
        """225 条 / 每页 20（11 页）：任意深度都还能继续翻。"""
        seeded = await _seed(real_mongo_backend, 225)
        repo = await real_mongo_repo(SessionRepository)

        seen = await _walk(repo, page_size=20)

        assert set(seen) == set(seeded)
        assert len(seen) == 225

    @pytest.mark.asyncio
    async def test_identical_timestamps_use_storage_level_tie_break(self, real_mongo_backend,
                                                                   real_mongo_repo):
        """全部 `updated_at` 相同且插入顺序被打乱：顺序必须由查询的 tie-break 决定。

        期望顺序是**字典序**（`session_id ASC`）；若实现只按 `updated_at` 排序，
        真 Mongo 可以按任意顺序返回（本用例即为那条路径的唯一证据）。
        """
        scrambled = ["conv_rp_zz", "conv_rp_aa", "conv_rp_mm", "conv_rp_bb", "conv_rp_yy"]
        for sid in scrambled:
            await real_mongo_backend.insert_one("sessions", {
                "session_id": sid, "title": sid, "messages": [],
                "character_binding": {}, "updated_at": _ts(0),
            })
        repo = await real_mongo_repo(SessionRepository)

        seen = await _walk(repo, page_size=2)

        assert seen == ["conv_rp_aa", "conv_rp_bb", "conv_rp_mm", "conv_rp_yy", "conv_rp_zz"]

    @pytest.mark.asyncio
    async def test_cursor_predicate_is_strictly_exclusive(self, real_mongo_backend,
                                                         real_mongo_repo):
        """游标谓词排他：同一游标两次请求结果一致，且与上一页零重叠。"""
        await _seed(real_mongo_backend, 60)
        repo = await real_mongo_repo(SessionRepository)

        page1 = await repo.list_page(limit=20)
        last = page1[-1]
        after = (last.updated_at, str(last.session_id))
        page2a = await repo.list_page(limit=20, after=after)
        page2b = await repo.list_page(limit=20, after=after)

        ids1 = [str(s.session_id) for s in page1]
        ids2 = [str(s.session_id) for s in page2a]
        assert ids2 == [str(s.session_id) for s in page2b], "同一游标两次结果不一致"
        assert set(ids1).isdisjoint(ids2), "游标未排他——记录跨页重复"
        assert ids2[0] == "conv_rp_0039", "下一页应从游标的下一条开始"
        assert ids1 == [f"conv_rp_{i:04d}" for i in range(59, 39, -1)]

    @pytest.mark.asyncio
    async def test_single_document_summary_is_window_independent(self, real_mongo_backend,
                                                                real_mongo_repo):
        """第 201+ 条的详情摘要必须可读（详情路径不再依赖 ≤200 列表窗口）。"""
        seeded = await _seed(real_mongo_backend, 201)
        repo = await real_mongo_repo(SessionRepository)

        oldest = await repo.get_summary(SessionId(seeded[0]))

        assert oldest is not None
        assert oldest.updated_at == _ts(0)
        assert oldest.title == "实库会话0000"

    @pytest.mark.asyncio
    async def test_search_is_pushed_down_and_pageable_on_real_driver(self, real_mongo_backend,
                                                                    real_mongo_repo):
        """搜索下推真驱动：大小写不敏感（ASCII）子串 + 55 条命中跨 3 页仍完整。"""
        seeded = await _seed(real_mongo_backend, 60, title="别的")
        for sid in seeded[:55]:
            await real_mongo_backend.update_one(
                "sessions", {"session_id": sid}, {"$set": {"title": "AlphaTopic"}})
        repo = await real_mongo_repo(SessionRepository)

        seen = await _walk(repo, page_size=20, search="alphatopic")

        assert set(seen) == set(seeded[:55]), f"搜索分页只命中 {len(seen)}/55"
        assert len(seen) == 55

    @pytest.mark.asyncio
    async def test_batch_summary_read_handles_more_than_backend_default_limit(
            self, real_mongo_backend, real_mongo_repo):
        """`summaries_by_ids` 覆盖层批量读：后端 `find` 默认 limit=100，必须显式放宽。"""
        seeded = await _seed(real_mongo_backend, 150)
        repo = await real_mongo_repo(SessionRepository)

        got = await repo.summaries_by_ids([SessionId(s) for s in seeded])

        assert len(got) == 150
