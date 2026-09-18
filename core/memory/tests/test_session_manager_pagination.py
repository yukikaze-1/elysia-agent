"""Batch 8 / P2-01r：`SessionManager.list()` 的分页完整性（service 层）。

与 route 层（`core/input/tests/test_sessions_pagination.py`）分工：
- route 层证明**用户入口**能走到每一条；
- 本层证明**合并语义**（持久层 keyset 分页 ⊕ 进程内本地摘要覆盖层）在跨页时仍然
  精确——这是"把分页下推到存储"最容易出错的地方（本地摘要的**有效排序键** =
  `max(持久键, 本地键)`，改名会把记录"抬"到更前，若实现直接拿持久键当游标，
  就会出现重复或漏项）。

仓储用**真实** `SessionRepository` + 内存 `FakeDocumentDBBackend`：keyset 谓词 /
sort / limit / `$regex` / `$in` 全部走真实仓储代码路径，只有后端是内存实现。
driver 只依据 `has_more` / `next_cursor` 继续翻页（不读库、不预先知道总数）。
"""

from __future__ import annotations

from typing import Any

import pytest

from core.memory.session_manager import SessionManager
from core.schema.types import SessionId
from core.storage.message_repo import MessageRepository
from core.storage.session_repo import SessionRepository
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

pytestmark = pytest.mark.unit

PAGE_LIMIT = 50
DEFAULT_SESSION = SessionId("default")


# ── 装配与播种 ─────────────────────────────────────────────────────────────


def _make_manager(backend: FakeDocumentDBBackend | None = None, *, offline: bool = False
                  ) -> tuple[SessionManager, SessionRepository, FakeDocumentDBBackend]:
    """真实 SessionManager + 真实 SessionRepository + 内存后端。"""
    be = backend or FakeDocumentDBBackend()
    repo = SessionRepository(db=None if offline else be)
    manager = SessionManager(repo=repo, msg_repo=MessageRepository(db=be))
    return manager, repo, be


def _ts(second: int, *, day: int = 1) -> str:
    return f"2026-08-{day:02d}T00:{second // 60:02d}:{second % 60:02d}+00:00"


async def _seed(backend: FakeDocumentDBBackend, count: int, *, prefix: str = "conv_seed",
                day: int = 1, binding: dict[str, Any] | None = None) -> list[str]:
    """直接播种 sessions doc（确定性 updated_at；末条最新）。"""
    ids: list[str] = []
    for i in range(count):
        sid = f"{prefix}_{i:03d}"
        await backend.insert_one("sessions", {
            "session_id": sid,
            "title": f"会话{i:03d}",
            "messages": [],
            "character_binding": binding or {},
            "updated_at": _ts(i, day=day),
        })
        ids.append(sid)
    return ids


async def _paginate(manager: SessionManager, *, limit: int = PAGE_LIMIT, search: str = "",
                    max_pages: int = 100) -> dict[str, Any]:
    """只依据公开合同（has_more / next_cursor）翻页。"""
    ids: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        items, has_more, next_cursor = await manager.list(cursor=cursor, limit=limit,
                                                          search=search)
        pages += 1
        ids.extend(str(s.session_id) for s in items)
        if not has_more:
            assert next_cursor is None, "has_more=False 时不得返回 next_cursor"
            break
        assert next_cursor, "has_more=True 时必须返回 next_cursor"
        assert next_cursor != cursor, "cursor 未推进"
        cursor = next_cursor
        assert pages < max_pages, "翻页不收敛"
    return {"ids": ids, "pages": pages}


# ── 核心：越过 200（PRE_FIX_RED） ────────────────────────────────────────────


class TestReachabilityBeyondTwoHundred:
    @pytest.mark.asyncio
    async def test_two_hundred_and_five_sessions_all_reachable(self):
        """205 条持久记录：完整分页拿到全部（+clean-start 本地 default）。"""
        manager, _repo, backend = _make_manager()
        seeded = await _seed(backend, 205, prefix="conv_p205")

        result = await _paginate(manager)

        assert len(result["ids"]) == len(set(result["ids"]))
        assert set(result["ids"]) == set(seeded) | {str(DEFAULT_SESSION)}

    @pytest.mark.asyncio
    async def test_oldest_records_beyond_two_hundred_are_reachable(self):
        """第 201+ 条（最旧记录）必须可达——修前 `list_all(limit=200)` 让它们消失。"""
        manager, _repo, backend = _make_manager()
        seeded = await _seed(backend, 201, prefix="conv_old")

        result = await _paginate(manager)

        assert set(seeded) - set(result["ids"]) == set(), "最旧记录不可达"

    @pytest.mark.asyncio
    async def test_list_does_not_prefetch_the_whole_dataset(self):
        """结构性守卫：`list()` 不得回退成"取全量再切片"（§20/§44）。

        把 `list_all` 换成"一调用就炸"的实现——用户列表路径只要碰它就红。
        """
        manager, _repo, backend = _make_manager()
        seeded = await _seed(backend, 60, prefix="conv_nopre")

        class _NoPrefetchRepo:
            """只允许分页读的仓储门面（list_all 视为回归）。"""

            def __init__(self, inner: SessionRepository) -> None:
                self._inner = inner

            def __getattr__(self, name: str) -> Any:
                if name == "list_all":
                    raise AssertionError("用户列表路径不得预取全量（list_all）")
                return getattr(self._inner, name)

        manager._repo = _NoPrefetchRepo(manager._repo)  # noqa: SLF001  (注入门面；同包测试)

        result = await _paginate(manager)
        assert set(result["ids"]) == set(seeded) | {str(DEFAULT_SESSION)}

    @pytest.mark.asyncio
    async def test_list_reads_at_most_one_page_from_storage(self):
        """PG2 的第二半（Challenger G1）：**每请求读量**必须与页大小同阶。

        上一个守卫只毒化 `list_all`——于是把 `list_page(limit=…)` 改成 `limit=10**6`
        （读全表、语义仍然正确）时，全部用例照样绿。这里直接记录下推到存储的
        `limit` 并断言上界。
        """
        manager, repo, backend = _make_manager()
        await _seed(backend, 500, prefix="conv_read")
        reads: list[int] = []
        original = repo.list_page

        async def _recording_list_page(**kwargs):
            reads.append(int(kwargs.get("limit", -1)))
            return await original(**kwargs)

        repo.list_page = _recording_list_page  # type: ignore[method-assign]

        items, has_more, _cursor = await manager.list(limit=50)

        assert len(items) == 50 and has_more is True
        assert reads and max(reads) <= 51, f"单页读取超量：{reads}"


# ── 本地摘要覆盖层（有效排序键 = max(持久键, 本地键)） ────────────────────────


class TestLocalOverlayExactness:
    @pytest.mark.asyncio
    async def test_renamed_old_session_moves_to_top_and_stays_unique(self):
        """改名把**最旧**会话的本地 updated_at 抬到 now：它应在第一页且全程只出现一次。

        这是"分页下推 + 覆盖层"最容易漏的形态：若下一页游标直接用持久层的键，
        被抬高的记录会在后续页**重复**；若实现把覆盖层从流里彻底剔除，
        它又会**丢失**。
        """
        manager, _repo, backend = _make_manager()
        seeded = await _seed(backend, 205, prefix="conv_ren")
        oldest = seeded[0]
        assert await manager.rename(SessionId(oldest), "改名到最新") is True

        result = await _paginate(manager)

        assert result["ids"][0] == oldest, "被改名（本地 updated_at=now）应排在最前"
        assert result["ids"].count(oldest) == 1, "覆盖层记录在后续页重复出现"
        assert set(result["ids"]) == set(seeded) | {str(DEFAULT_SESSION)}

    @pytest.mark.asyncio
    async def test_overlay_merges_fields_with_persisted_record(self):
        """覆盖层只抬排序键/标题，**不丢**持久字段（message_count 取 max、绑定沿用）。"""
        manager, _repo, backend = _make_manager()
        await backend.insert_one("sessions", {
            "session_id": "conv_ovl",
            "title": "持久标题",
            "messages": [{"role": "user"}, {"role": "assistant"}, {"role": "user"}],
            "character_binding": {"character_id": "chr_a", "instance_id": "chi_a"},
            "updated_at": _ts(0),
        })
        assert await manager.rename(SessionId("conv_ovl"), "改名标题") is True

        summary = await manager.get_summary(SessionId("conv_ovl"))

        assert summary is not None
        assert summary.title == "改名标题"
        assert summary.message_count == 3, "message_count 必须取 max（持久 3 > 本地 0）"
        assert summary.character_binding is not None
        assert summary.character_binding.get("character_id") == "chr_a"

    @pytest.mark.asyncio
    async def test_local_only_session_survives_reconnect(self):
        """离线创建的会话在 Mongo 恢复后仍可达（本地-only 条目按本地键插入有序位置）。"""
        manager, repo, backend = _make_manager(offline=True)
        offline_summary = await manager.create(title="离线会话")
        await _seed(backend, 205, prefix="conv_rec")
        repo._db = backend  # noqa: SLF001  (模拟 Mongo 恢复；仓储为 injected 时不重连)

        result = await _paginate(manager)

        assert str(offline_summary.session_id) in result["ids"]
        assert len(result["ids"]) == 207  # 205 持久 + default + 离线创建

    @pytest.mark.asyncio
    async def test_character_binding_survives_pagination(self):
        """§48：跨页读取不得丢 character_binding（覆盖层合并路径同断言）。"""
        manager, _repo, backend = _make_manager()
        await _seed(backend, 205, prefix="conv_cb",
                    binding={"character_id": "chr_builtin_cyrene",
                             "instance_id": "chi_builtin_cyrene"})

        ids: list[str] = []
        cursor: str | None = None
        for _ in range(20):                 # 有界：游标不推进时**失败**而不是挂死
            items, has_more, next_cursor = await manager.list(cursor=cursor,
                                                              limit=PAGE_LIMIT)
            for item in items:
                if str(item.session_id).startswith("conv_cb"):
                    assert item.character_binding is not None
                    assert item.character_binding.get("character_id") == "chr_builtin_cyrene"
                ids.append(str(item.session_id))
            if not has_more:
                break
            assert next_cursor and next_cursor != cursor, "游标未推进"
            cursor = next_cursor
        else:
            pytest.fail("分页不收敛（20 页仍未结束）")
        assert len([i for i in ids if i.startswith("conv_cb")]) == 205


# ── 排序稳定性 / 边界 ──────────────────────────────────────────────────────


class TestOrderingAndBoundaries:
    @pytest.mark.asyncio
    async def test_identical_timestamps_across_page_boundary(self):
        """主排序键重复：必须用 session_id ASC tiebreak，跨页不重不漏。"""
        manager, _repo, backend = _make_manager()
        for sid in ("conv_f", "conv_a", "conv_d", "conv_b", "conv_e", "conv_c"):
            await backend.insert_one("sessions", {
                "session_id": sid, "title": sid, "messages": [],
                "character_binding": {}, "updated_at": _ts(0),
            })

        # 页大小 2：边界恰好落在相同 updated_at 的记录之间
        result = await _paginate(manager, limit=2)

        assert result["ids"] == [str(DEFAULT_SESSION), "conv_a", "conv_b", "conv_c",
                                 "conv_d", "conv_e", "conv_f"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("count", [0, 1, PAGE_LIMIT, PAGE_LIMIT + 1, 199, 200, 201])
    async def test_boundary_sizes_are_complete(self, count: int):
        manager, _repo, backend = _make_manager()
        seeded = await _seed(backend, count, prefix=f"conv_b{count}")

        result = await _paginate(manager)

        assert set(result["ids"]) == set(seeded) | {str(DEFAULT_SESSION)}
        assert len(result["ids"]) == len(set(result["ids"]))

    @pytest.mark.asyncio
    async def test_search_and_cursor_apply_on_every_page(self):
        """§51：filter 不得在后续页丢失；搜索命中集合完整。"""
        manager, _repo, backend = _make_manager()
        hits = await _seed(backend, 120, prefix="conv_hit")
        misses = await _seed(backend, 120, prefix="conv_miss", day=2)
        for sid in misses:
            await backend.update_one("sessions", {"session_id": sid},
                                     {"$set": {"title": "别的标题"}})

        result = await _paginate(manager, search="会话")

        assert set(result["ids"]) == set(hits)

    @pytest.mark.asyncio
    async def test_get_summary_is_authoritative_beyond_the_old_cap(self):
        """单会话详情同样不得依赖 ≤200 的预取列表（第 201+ 条要能打开）。"""
        manager, _repo, backend = _make_manager()
        seeded = await _seed(backend, 201, prefix="conv_sum")
        oldest = seeded[0]

        summary = await manager.get_summary(SessionId(oldest))

        assert summary is not None, "最旧会话的详情摘要不可达"
        assert summary.message_count == 0
        assert str(summary.updated_at) == _ts(0)

    @pytest.mark.asyncio
    async def test_offline_repo_falls_back_to_local_summaries_with_pagination(self):
        """Mongo 离线：退化为进程内摘要，但仍遵守 cursor/limit 合同。"""
        manager, _repo, _backend = _make_manager(offline=True)
        created = [await manager.create(title=f"离线{i}") for i in range(3)]

        result = await _paginate(manager, limit=2)

        assert set(result["ids"]) == {str(c.session_id) for c in created} | {
            str(DEFAULT_SESSION)}


class TestAdversarialRegressions:
    """Challenger（批次 8）找到的、**由分页下推引入**的回归——逐条钉住。

    这些形态在修前的"预取全量 + 内存分页"里要么天然免疫（字典去重），
    要么是"响亮的失败"；下推分页后若不复现这些守卫，就会变成
    "静默说谎"或"整表静默退化"。
    """

    @pytest.mark.asyncio
    async def test_concurrent_create_during_page_build_is_not_duplicated(self):
        """F2：`summaries_by_ids` 与覆盖层条目**必须取自同一次快照**。

        若两次读取之间插进一次 `create()`，新会话的持久文档会被持久层流吐一次，
        而它的本地摘要又不在覆盖层快照里 —— 同一条记录在一页里出现两次。
        """
        manager, repo, backend = _make_manager()
        seeded = await _seed(backend, 5, prefix="conv_race")
        original = repo.summaries_by_ids
        state = {"injected": False}

        async def _creating_during_fetch(ids):
            result = await original(ids)
            if not state["injected"]:
                state["injected"] = True
                await manager.create(title="竞态会话")   # 落在两次读取之间
            return result

        repo.summaries_by_ids = _creating_during_fetch  # type: ignore[method-assign]

        items, _has_more, _cursor = await manager.list(limit=50)
        ids = [str(s.session_id) for s in items]

        assert len(ids) == len(set(ids)), f"同一记录在一页里出现两次：{ids}"
        assert set(seeded) <= set(ids)

    @pytest.mark.asyncio
    async def test_duplicate_session_docs_are_emitted_once(self):
        """F5：`sessions.session_id` 没有唯一索引 ⇒ 并发 upsert 可产生重复文档。

        修前按 session_id 建字典天然去重；下推分页后必须显式去重，否则列表里
        同一 conversationId 出现两条。
        """
        manager, _repo, backend = _make_manager()
        for _ in range(2):  # 两条同 session_id 的文档（不同时间戳）
            await backend.insert_one("sessions", {
                "session_id": "conv_dup", "title": "重复", "messages": [],
                "character_binding": {}, "updated_at": _ts(1),
            })
        await backend.insert_one("sessions", {
            "session_id": "conv_other", "title": "正常", "messages": [],
            "character_binding": {}, "updated_at": _ts(0),
        })

        # 页大小要够大，使两份重复文档落在**同一页**（limit=1 时第二份会被游标滤掉，
        # 于是去重逻辑的真假都绿——这正是测试鉴别力的坑）。
        result = await _paginate(manager, limit=10)

        assert result["ids"].count("conv_dup") == 1
        assert set(result["ids"]) == {"conv_dup", "conv_other", str(DEFAULT_SESSION)}

    @pytest.mark.asyncio
    async def test_malformed_updated_at_does_not_collapse_the_list(self):
        """F1/F6：缺 `updated_at` / 为 null 的**历史文档**不得让整份列表静默退化。

        修前形态：`None > str`（归并比较）或 `max(None, str)`（覆盖层合并）抛
        `TypeError`，被 `list()` 的兜底 `except Exception` 吞成"降级为进程内摘要"
        ——用户看到 has_more=false 与 2 条本地记录，40 条健康会话整片消失。
        投影层把非 str 归一为 ""，使比较全序化：坏文档最多自己不可达，
        **不得**连坐其余记录。

        注意 `max()` 那条路径需要坏文档**同时有本地覆盖层**（改名/绑定的会话），
        所以这里显式改名——只放一条裸坏文档是测不出来的。
        """
        manager, _repo, backend = _make_manager()
        healthy = await _seed(backend, 40, prefix="conv_ok")
        await backend.insert_one("sessions", {
            "session_id": "conv_bad_missing", "title": "无时间戳", "messages": [],
            "character_binding": {},
        })
        await backend.insert_one("sessions", {
            "session_id": "conv_bad_null", "title": "空时间戳", "messages": [],
            "character_binding": {}, "updated_at": None,
        })
        assert await manager.rename(SessionId("conv_bad_null"), "改名的坏文档") is True
        await manager.create(title="本地覆盖层")  # 覆盖层合并路径

        result = await _paginate(manager)

        assert set(healthy) <= set(result["ids"]), "坏文档连坐了健康记录"
        assert len(result["ids"]) == len(set(result["ids"]))
        # 有本地覆盖层的 null 时间戳文档：有效键取 max("", now) → 可达（不再是"漏项"）
        assert "conv_bad_null" in result["ids"]

    @pytest.mark.xfail(reason="已登记缺口（NEW_FINDING）：缺 updated_at 的历史文档在 "
                             "keyset 谓词下不可达；本批只保证它不连坐其余记录，"
                             "修它需要数据归一化或查询形态变更",
                       strict=False)
    @pytest.mark.asyncio
    async def test_missing_updated_at_row_is_unreachable_registered_gap(self):
        """把"坏文档不可达"这一**已知缺口**显式钉住（修好时本用例会 XPASS）。"""
        manager, _repo, backend = _make_manager()
        await _seed(backend, 40, prefix="conv_ok")
        await backend.insert_one("sessions", {
            "session_id": "conv_bad_missing", "title": "无时间戳", "messages": [],
            "character_binding": {},
        })

        result = await _paginate(manager, limit=10)

        assert "conv_bad_missing" in result["ids"]

    @pytest.mark.asyncio
    async def test_mid_walk_storage_failure_is_not_reported_as_end_of_list(self):
        """F7：翻页**中途**读失败不得降级成"短页 + has_more=false"。

        退化只在首页（既有语义）；中途失败如实抛出，客户端能看到并重试，
        而不是把一次存储故障当成"列表到底了"。
        """
        from core.storage.base import StorageUnavailableError

        manager, repo, backend = _make_manager()
        await _seed(backend, 30, prefix="conv_fail")
        original = repo.list_page
        state = {"calls": 0}

        async def _fail_after_first(**kwargs):
            state["calls"] += 1
            if state["calls"] > 1:
                raise StorageUnavailableError("模拟中途读失败")
            return await original(**kwargs)

        repo.list_page = _fail_after_first  # type: ignore[method-assign]

        items, has_more, cursor = await manager.list(limit=10)
        assert items and has_more and cursor

        with pytest.raises(StorageUnavailableError):
            await manager.list(cursor=cursor, limit=10)

    @pytest.mark.asyncio
    async def test_overlay_only_tail_is_not_reported_as_end_of_list(self):
        """PG1/PG4（Challenger G2）：`has_more` 必须把**覆盖层剩余的尾部**算进去。

        形态：持久层正好一页（50 条），另有一条**比它们都旧**的本地-only 会话。
        若 `has_more` 只看持久层是否翻完，就会在这一页谎报"没有更多"，
        那条本地会话永久不可达。

        （探针用未来时间戳把持久记录放到覆盖层之前——真实时间戳恒为 now，
        该形态正是"最旧的一条只存在于进程内"的等价缩微模型。）
        """
        manager, _repo, backend = _make_manager()
        offline = await manager.create(title="进程内最旧")   # 本地-only（repo 在线后仍不在库）
        for i in range(50):
            await backend.insert_one("sessions", {
                "session_id": f"conv_tail_{i:03d}", "title": f"较新{i:03d}", "messages": [],
                "character_binding": {}, "updated_at": f"2027-08-01T00:{i // 60:02d}:{i % 60:02d}+00:00",
            })

        result = await _paginate(manager, limit=50)

        assert str(offline.session_id) in result["ids"], "覆盖层尾部被谎报成列表结束"
        # 50 持久（一页）+ 2 条覆盖层尾部（offline 创建 + clean-start default）
        assert len(result["ids"]) == 52

    @pytest.mark.asyncio
    async def test_storage_failure_mid_page_build_raises_on_later_page(self):
        """PG6（Challenger G7）：中途失败是**响亮失败**，不是挂死或空页。

        与 `TestAdversarialRegressions::test_mid_walk_storage_failure_...` 互补：
        这里让**首页**成功、第二页的仓储调用抛错，断言异常类型与不可吞掉。
        """
        from core.storage.base import StorageUnavailableError

        manager, repo, backend = _make_manager()
        await _seed(backend, 120, prefix="conv_mid")
        original = repo.list_page
        state = {"calls": 0}

        async def _fail_on_second(**kwargs):
            state["calls"] += 1
            if state["calls"] >= 2:
                raise StorageUnavailableError("模拟翻页中途失败")
            return await original(**kwargs)

        repo.list_page = _fail_on_second  # type: ignore[method-assign]

        _items, _has_more, cursor = await manager.list(limit=50)
        with pytest.raises(StorageUnavailableError):
            await manager.list(cursor=cursor, limit=50)

    @pytest.mark.asyncio
    async def test_oversized_search_term_returns_empty_metadata_coherently(self):
        """PG4（Challenger G4）：manager 级超长搜索词分支也必须自洽（空页 + 无游标）。"""
        manager, _repo, backend = _make_manager()
        await _seed(backend, 3, prefix="conv_long")

        items, has_more, cursor = await manager.list(search="x" * 300)

        assert items == [] and has_more is False and cursor is None

    @pytest.mark.asyncio
    async def test_get_summary_hides_deleting_and_deleted_sessions(self):
        """PG5（Challenger G5）：详情路径的 tombstone 早退分支（删中的会话不得复活）。"""
        manager, _repo, backend = _make_manager()
        seeded = await _seed(backend, 2, prefix="conv_tomb")
        manager._deleting.add(SessionId(seeded[0]))  # noqa: SLF001  (同包测试构造删中态)

        assert await manager.get_summary(SessionId(seeded[0])) is None
        assert await manager.get_summary(SessionId(seeded[1])) is not None

    @pytest.mark.asyncio
    async def test_limit_below_one_keeps_metadata_coherent(self):
        """F3：limit<=0 不得给出"has_more=True 却没有游标"的自相矛盾元数据。"""
        manager, _repo, backend = _make_manager()
        await _seed(backend, 3, prefix="conv_l0")

        items, has_more, cursor = await manager.list(limit=0)

        assert items
        assert has_more is bool(cursor)

    @pytest.mark.asyncio
    async def test_search_semantics_agree_between_overlay_and_persisted(self):
        """F4：覆盖层与持久层必须用**同一族**搜索语义（大小写不敏感正则）。

        反例（修前）：`str.lower()` 判定覆盖层、`$regex i` 判定持久层——同一个标题
        "ſ"（U+017F）在持久层命中 's'、在覆盖层不命中，同一份列表里两种答案。
        """
        manager, _repo, backend = _make_manager()
        await backend.insert_one("sessions", {
            "session_id": "conv_case_persisted", "title": "ſ", "messages": [],
            "character_binding": {}, "updated_at": _ts(1),
        })
        created = await manager.create(title="临时")
        await manager.rename(created.session_id, "ſ")   # 覆盖层标题
        await backend.insert_one("sessions", {
            "session_id": str(created.session_id), "title": "临时", "messages": [],
            "character_binding": {}, "updated_at": _ts(0),
        })

        items, _, _ = await manager.list(search="s")

        assert {str(s.session_id) for s in items} == {
            "conv_case_persisted", str(created.session_id)}
