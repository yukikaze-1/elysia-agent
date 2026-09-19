"""批次 16 / KB-16 —— **真实 MongoDB** 上的会话身份唯一性（一个逻辑 ID 只能有一篇文档）。

本层是这一 finding 的**核心证据层**：机理全在 Mongo 自己的语义上，fake 后端结构性
地给不出结论——

  - **upsert 并发**：没有唯一索引时两个并发 upsert 会各自插入（真库实测出现重复）；
    有唯一索引时服务端把 loser 的插入重试为 update（最终恒 1 篇，0 异常）。
  - **索引选项不能原地改**：同名请求 `unique=True` 抛 code 86；已有重复时 build
    unique 抛 code 11000，且会把已 drop 的索引留在"没了"的状态。
  - **索引探测本身也会失败**：`list_indexes()` / `aggregate` 返回 None/异常时必须
    按"可能有重复"处理，不能赌一把建唯一索引。

隔离：沿用本层约定（`conftest.py`）——独立测试库、进入/退出各 drop 一次，
库名必须含 `test`，仓储一律经 `real_mongo_repo` 工厂构造。
"""

from __future__ import annotations

import asyncio

import pytest

from Module.database.documentdb import DocumentDBFactory
from core.schema.types import SessionId
from core.storage.schema_drift import check_schema_drift
from core.storage.session_repo import SessionRepository
from tests.real_mongo._session_data_support import COLL, SESSION_ID_INDEX
from tests.real_mongo._session_data_support import ts as _ts

pytestmark = [pytest.mark.external, pytest.mark.real_mongo]


class _Rendezvous:
    """N 方会合点（先到的等后到的，全部到齐才放行）。"""

    def __init__(self, expect: int) -> None:
        self._expect = expect
        self.arrived = 0
        self.event = asyncio.Event()

    def arrive(self) -> None:
        self.arrived += 1
        if self.arrived >= self._expect:
            self.event.set()


class _GatedFindBackend:
    """把 `find` 卡在"两边都没查到"之后，**确定性**复现 find→upsert 的 TOCTOU 窗口。

    这是 KB-16 最锋利的真库证据：修前两个 client 都能通过存在性检查（`find` 返回空），
    随后各自 `update_one(..., upsert=True)` 插入 ⇒ 同一 session_id **两份文档**——
    不依赖调度运气。修后唯一索引让第二篇插入失败，服务端按 Mongo 的 upsert 语义
    把它重试为 update（`$setOnInsert` 变 no-op），最终恰好 1 篇。
    """

    def __init__(self, inner, rendezvous: _Rendezvous, sid: str) -> None:
        self._inner = inner
        self._rendezvous = rendezvous
        self._sid = sid

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def find(self, collection: str, filter_expr: dict, **kwargs):
        docs = await self._inner.find(collection, filter_expr, **kwargs)
        if collection == COLL and filter_expr == {"session_id": self._sid} and not docs:
            self._rendezvous.arrive()
            await asyncio.wait_for(self._rendezvous.event.wait(), timeout=5)
        return docs

# ── KB-16：身份唯一性（真实索引 / 并发 / 升级路径） ────────────────────────


class TestSessionIdentityUniquenessRealMongo:
    @pytest.mark.asyncio
    async def test_connect_creates_the_unique_index(self, real_mongo_repo):
        """SD-T10：`connect()` 之后库里真有 `session_id` 唯一约束（结构断言）。"""
        repo = await real_mongo_repo(SessionRepository)

        live = await repo.backend.list_indexes(COLL)  # type: ignore[union-attr]
        by_name = {i["name"]: i for i in (live or [])}

        assert by_name[SESSION_ID_INDEX].get("unique") is True

    @pytest.mark.asyncio
    async def test_existing_non_unique_index_is_upgraded_in_place(self, real_mongo_backend,
                                                                 real_mongo_repo):
        """升级路径：旧部署的非唯一 `session_id_1` 被 drop 后重建为唯一（无残留重复索引）。"""
        await real_mongo_backend.ensure_index(COLL, [("session_id", 1)])
        before = {i["name"]: i for i in (await real_mongo_backend.list_indexes(COLL) or [])}
        assert before[SESSION_ID_INDEX].get("unique") in (False, None)

        repo = await real_mongo_repo(SessionRepository)

        after = {i["name"]: i for i in (await repo.backend.list_indexes(COLL) or [])}  # type: ignore[union-attr]
        assert after[SESSION_ID_INDEX].get("unique") is True
        same_key = [n for n, i in after.items()
                    if dict(i.get("key", {})) == {"session_id": 1}]
        assert same_key == [SESSION_ID_INDEX], "不得留下两个同键索引"

    @pytest.mark.asyncio
    async def test_duplicates_do_not_destroy_availability(self, real_mongo_backend,
                                                         real_mongo_repo, caplog):
        """SD-T9（部署策略）：库里已有重复 ⇒ 保持非唯一 + 显著告警 + 应用照常可用。"""
        import logging

        for second in (1, 2):
            await real_mongo_backend.insert_one(COLL, {
                "session_id": "conv_e3_dup", "messages": [], "updated_at": _ts(second)})
        await real_mongo_backend.ensure_index(COLL, [("session_id", 1)])

        with caplog.at_level(logging.WARNING, logger="core.storage.session_repo"):
            repo = await real_mongo_repo(SessionRepository)

        live = {i["name"]: i for i in (await repo.backend.list_indexes(COLL) or [])}  # type: ignore[union-attr]
        assert live[SESSION_ID_INDEX].get("unique") in (False, None), "有重复时不得建唯一"
        assert repo.backend is not None
        assert await repo.get_summary(SessionId("conv_e3_dup")) is not None
        assert "唯一性未强制" in caplog.text

        # 运行时 schema_drift 必须把"代码声明 unique、库里非唯一"报成选项冲突
        report = await check_schema_drift(real_mongo_backend)
        assert report.scanned is True
        assert SESSION_ID_INDEX in report.option_conflicts.get("sessions", ())

    @pytest.mark.asyncio
    async def test_building_unique_index_with_duplicates_fails_loudly(
            self, real_mongo_backend):
        """Challenger Attack E：已有重复时 build unique 的真实异常（不猜、留证据）。"""
        from pymongo.errors import DuplicateKeyError

        for second in (1, 2):
            await real_mongo_backend.insert_one(COLL, {
                "session_id": "conv_e3_dup2", "messages": [], "updated_at": _ts(second)})

        with pytest.raises(DuplicateKeyError) as excinfo:
            await real_mongo_backend.ensure_index(COLL, [("session_id", 1)], unique=True)

        assert getattr(excinfo.value, "code", None) == 11000

    @pytest.mark.asyncio
    async def test_concurrent_same_session_creation_keeps_one_authority(
            self, real_mongo_uri, real_mongo_db_name, real_mongo_repo):
        """SD-T7：20 个独立 client 并发 create 同一个 session_id ⇒ 最终恰好 1 篇。

        修前（无唯一索引）实测 20 轮里会出现重复文档——本用例是那条路径的
        PRE_FIX_RED 真库版本。
        """
        await real_mongo_repo(SessionRepository)      # 先让唯一索引建成
        clients = [DocumentDBFactory.create(f"mongodb/{real_mongo_db_name}",
                                           base_url=real_mongo_uri) for _ in range(20)]
        repos = [SessionRepository(db=c) for c in clients]
        barrier = asyncio.Event()
        errors: list[BaseException] = []

        async def _create(repo: SessionRepository) -> None:
            await barrier.wait()
            try:
                await repo.create(SessionId("conv_e3_race"))
            except BaseException as exc:      # noqa: BLE001 - 记录后统一断言
                errors.append(exc)

        tasks = [asyncio.create_task(_create(r)) for r in repos]
        await asyncio.sleep(0)
        barrier.set()
        await asyncio.gather(*tasks)
        try:
            count = await clients[0].count(COLL, {"session_id": "conv_e3_race"})
            assert count == 1, f"同一 session_id 出现了 {count} 篇文档"
            assert errors == [], f"并发创建不得抛异常：{errors[:2]}"
        finally:
            for c in clients:
                await c.close()

    @pytest.mark.asyncio
    async def test_concurrent_ensure_exists_keeps_one_authority(
            self, real_mongo_uri, real_mongo_db_name, real_mongo_repo):
        """clean-start 的 `find → $setOnInsert` upsert 同样只能留下 1 篇。"""
        await real_mongo_repo(SessionRepository)
        clients = [DocumentDBFactory.create(f"mongodb/{real_mongo_db_name}",
                                           base_url=real_mongo_uri) for _ in range(20)]
        repos = [SessionRepository(db=c) for c in clients]
        barrier = asyncio.Event()

        async def _ensure(repo: SessionRepository) -> None:
            await barrier.wait()
            await repo.ensure_exists(SessionId("conv_e3_race2"))

        tasks = [asyncio.create_task(_ensure(r)) for r in repos]
        await asyncio.sleep(0)
        barrier.set()
        await asyncio.gather(*tasks)
        try:
            assert await clients[0].count(COLL, {"session_id": "conv_e3_race2"}) == 1
        finally:
            for c in clients:
                await c.close()

    @pytest.mark.asyncio
    async def test_find_then_upsert_window_cannot_produce_two_authorities(
            self, real_mongo_uri, real_mongo_db_name, real_mongo_repo):
        """**确定性** PRE_FIX_RED**：卡住存在性检查，两边都走插入分支。

        修前：两篇文档（`ensure_exists` 的 find→$setOnInsert upsert 不是原子的）；
        修后：唯一索引 + Mongo 的 upsert 重试 ⇒ 恰好 1 篇，且不抛异常。
        """
        await real_mongo_repo(SessionRepository)      # 建成唯一索引
        sid = "conv_e3_window"
        rendezvous = _Rendezvous(expect=2)
        raw = [DocumentDBFactory.create(f"mongodb/{real_mongo_db_name}",
                                       base_url=real_mongo_uri) for _ in range(2)]
        repos = [SessionRepository(db=_GatedFindBackend(b, rendezvous, sid))
                 for b in raw]
        try:
            await asyncio.gather(*(r.ensure_exists(SessionId(sid)) for r in repos))
            count = await raw[0].count(COLL, {"session_id": sid})
            assert count == 1, f"find→upsert 窗口产出了 {count} 篇同 ID 文档"
        finally:
            for b in raw:
                await b.close()

    @pytest.mark.asyncio
    async def test_unique_collision_does_not_kill_the_repository(self, real_mongo_backend,
                                                                real_mongo_repo):
        """唯一索引带来的失败语义：冲突**不得**把仓储判死（那是全站 503 的形态）。"""
        repo = await real_mongo_repo(SessionRepository)
        await repo.create(SessionId("conv_e3_twice"))

        await repo.create(SessionId("conv_e3_twice"))     # 顺序重复 create：upsert 语义

        assert repo.backend is not None, "冲突后仓储必须仍可用"
        assert await real_mongo_backend.count(COLL, {"session_id": "conv_e3_twice"}) == 1

    @pytest.mark.asyncio
    async def test_schema_drift_is_clean_after_connect(self, real_mongo_backend,
                                                       real_mongo_repo):
        """干净库 + `connect()` ⇒ 索引与声明一致（唯一约束真的被 schema_drift 认到）。"""
        await real_mongo_repo(SessionRepository)

        report = await check_schema_drift(real_mongo_backend)

        assert report.scanned is True
        assert report.option_conflicts.get("sessions", ()) == ()
        assert report.missing.get("sessions", ()) == ()
        assert report.orphans.get("sessions", ()) == ()
