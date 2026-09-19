"""批次 16 / KB-16：`session_id` **逻辑主键唯一性**（unit，无需真实 Mongo）。

canonical 事实（真库 E3 实测，脚本 `tmp/b16/main/probe_kb16_e3.py`）：

  - `update_one({"session_id": X}, …, upsert=True)` 在**没有唯一索引**时不是原子的：
    两个并发 upsert 都看不到匹配就会各自插入 ⇒ 同一 session_id 出现两份文档
    （实测 20 轮 × 两个独立 client：`create` 1–3 轮出现重复、`ensure_exists` 1–2 轮）。
  - 库里已有非唯一 `session_id_1` 时，直接给它加 `unique=True` 会抛
    `OperationFailure`(86 IndexKeySpecsConflict)——而它在 `connect()` 的同一个
    `try` 里会被吞成"MongoDB 不可用"（整个 sessions 域 `_db=None`）。
  - 已有重复文档时 build unique 会抛 `DuplicateKeyError`(11000)，且若升级路径
    已经 drop 过旧索引，集合会退化成**只剩 `_id_`**。

本文件钉住仓库层的处置：可用性优先 + 降级显式 + 写路径冲突收敛。
真实驱动侧（索引真的建成、并发真的只留 1 篇）由 `tests/real_mongo` 承担。
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from Module.database.documentdb import DocumentDBFactory
from core.schema.types import SessionId
from core.storage.session_repo import COLLECTION, SESSION_ID_INDEX, SessionRepository
from core.storage.tests._fake_documentdb import DuplicateKeyError, FakeDocumentDBBackend

pytestmark = pytest.mark.unit


def _mock_backend(*, indexes: list[dict] | None = None,
                  duplicates: list[dict] | None = None,
                  duplicates_error: Exception | None = None,
                  indexes_result: Any = "default") -> MagicMock:
    """全 async 的 mock 文档库后端（`connect()` 会 await 每一个调用）。"""
    backend = MagicMock()
    backend.list_indexes = AsyncMock(
        return_value=(_indexes() if indexes is None else indexes)
        if indexes_result == "default" else indexes_result)
    if duplicates_error is not None:
        backend.aggregate = AsyncMock(side_effect=duplicates_error)
    else:
        backend.aggregate = AsyncMock(return_value=list(duplicates or []))
    backend.ensure_index = AsyncMock(return_value="idx")
    backend.drop_index = AsyncMock(return_value=True)
    return backend


def _repo_with_backend(monkeypatch: pytest.MonkeyPatch,
                       backend: Any) -> SessionRepository:
    """装配"真实 connect() 路径 + mock 后端"的仓储（不触真实 Mongo）。"""
    monkeypatch.setattr(DocumentDBFactory, "create", lambda *a, **kw: backend)
    return SessionRepository()


def _indexes(*specs: tuple[str, bool]) -> list[dict]:
    """构造 `list_indexes()` 的库侧实况。

    每项都带真实 `key`：升级/幂等判定按**键模式**（而不是名字）识别现状，
    没有 `key` 的 mock 会让判定退化成"库里没有同键索引"（测试与被测对象
    各持一份真相）。
    """
    out = [{"name": "_id_", "key": {"_id": 1}, "unique": True}]
    out.extend({"name": name, "key": {"session_id": 1}, "unique": unique}
               for name, unique in specs)
    return out


def _session_id_ensure_calls(backend: MagicMock) -> list[dict]:
    """`ensure_index` 里针对 sessions 的调用（排除 updated_at 两条）。"""
    return [kw for args, kw in backend.ensure_index.await_args_list
            if args[0] == COLLECTION and args[1] == [("session_id", 1)]]


# ── 索引引导：升级 / 幂等 / 降级 ───────────────────────────────────────────


class TestSessionIdentityIndexBootstrap:
    @pytest.mark.asyncio
    async def test_clean_database_creates_unique_index(self, monkeypatch):
        """全新库：直接建唯一索引（不需要 drop）。"""
        backend = _mock_backend()
        repo = _repo_with_backend(monkeypatch, backend)

        assert await repo.connect() is True

        assert [kw for kw in _session_id_ensure_calls(backend)] == [{"unique": True}]
        backend.drop_index.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_unique_index_is_upgraded_by_drop_then_create(self, monkeypatch):
        """**升级路径**：已有非唯一 `session_id_1` + 数据干净 ⇒ drop 后重建为唯一。

        不能对同名索引直接 `create_index(unique=True)`：真库抛 code 86，而它会被
        `connect()` 吞成"仓储不可用"。
        """
        backend = _mock_backend(indexes=_indexes((SESSION_ID_INDEX, False)))
        repo = _repo_with_backend(monkeypatch, backend)

        assert await repo.connect() is True

        assert backend.drop_index.await_args.args == (COLLECTION, SESSION_ID_INDEX)
        assert [kw for kw in _session_id_ensure_calls(backend)] == [{"unique": True}]

    @pytest.mark.asyncio
    async def test_already_unique_is_idempotent(self, monkeypatch):
        """已建成唯一索引：第二次（及以后）connect() 不做任何索引变更。"""
        backend = _mock_backend(indexes=_indexes((SESSION_ID_INDEX, True)))
        repo = _repo_with_backend(monkeypatch, backend)

        for _ in range(2):
            assert await repo.connect() is True
            repo._db = None      # 模拟重连：让 connect() 再走一遍引导  # noqa: SLF001
        # 第一次 connect 前 _db 为 None；第二次前手动置 None。两次都不该动索引。

        backend.drop_index.assert_not_awaited()
        assert _session_id_ensure_calls(backend) == []

    @pytest.mark.asyncio
    async def test_duplicates_keep_availability_and_skip_unique_build(self, monkeypatch,
                                                                     caplog):
        """**有重复文档**：不建唯一索引、不 drop、不删数据，但降级必须显式可见。"""
        backend = _mock_backend(indexes=_indexes((SESSION_ID_INDEX, False)),
                                duplicates=[{"_id": "dup", "n": 2}])
        repo = _repo_with_backend(monkeypatch, backend)

        with caplog.at_level(logging.WARNING, logger="core.storage.session_repo"):
            assert await repo.connect() is True

        assert repo._db is not None, "有历史脏数据不得让 sessions 域不可用"  # noqa: SLF001
        backend.drop_index.assert_not_awaited()
        assert _session_id_ensure_calls(backend) == [], "有重复时不得请求唯一索引"
        assert "唯一性未强制" in caplog.text, "降级必须显著告警，不能静默"

    @pytest.mark.asyncio
    async def test_duplicate_probe_failure_counts_as_possible_duplicates(self, monkeypatch):
        """检测失败 ≠ 无重复（S8 纪律）：按"可能有重复"降级，不赌一把建唯一索引。"""
        backend = _mock_backend(indexes=_indexes((SESSION_ID_INDEX, False)),
                                duplicates_error=RuntimeError("network down"))
        repo = _repo_with_backend(monkeypatch, backend)

        assert await repo.connect() is True

        backend.drop_index.assert_not_awaited()
        assert _session_id_ensure_calls(backend) == []

    @pytest.mark.asyncio
    async def test_index_listing_failure_skips_all_changes(self, monkeypatch):
        """`list_indexes` 查询失败（三态里的 None）：状态未知 ⇒ 不做任何索引变更。"""
        backend = _mock_backend(indexes_result=None)
        repo = _repo_with_backend(monkeypatch, backend)

        assert await repo.connect() is True

        backend.drop_index.assert_not_awaited()
        assert _session_id_ensure_calls(backend) == []

    @pytest.mark.asyncio
    async def test_unique_build_failure_falls_back_to_non_unique(self, monkeypatch):
        """唯一索引建不出来（如磁盘/权限）⇒ 退回非唯一，且**绝不让 connect() 失败**。"""
        backend = _mock_backend(indexes=_indexes((SESSION_ID_INDEX, False)))

        calls: list[dict] = []

        async def _ensure_index(collection: str, keys: list, **kwargs: Any) -> str:
            calls.append(kwargs)
            if kwargs.get("unique"):
                raise RuntimeError("Index build failed")
            return "session_id_1"

        backend.ensure_index = AsyncMock(side_effect=_ensure_index)
        repo = _repo_with_backend(monkeypatch, backend)

        assert await repo.connect() is True
        assert repo._db is not None  # noqa: SLF001

        assert calls[-1] == {}, "唯一建失败后必须退回非唯一（否则 session_id 没索引）"


# ── 写路径：并发同键冲突的收敛语义（§58/§59/§60） ─────────────────────────


class _ConflictOnceBackend(FakeDocumentDBBackend):
    """第一次 upsert 抛 `DuplicateKeyError`，之后正常（模拟并发 loser）。"""

    def __init__(self, *, key_pattern: dict | None = None) -> None:
        super().__init__()
        self._conflict_key_pattern = key_pattern or {"session_id": 1}
        self.upsert_calls = 0

    async def update_one(self, collection: str, filter_expr: dict, update: dict,
                         upsert: bool = False) -> int:
        self.upsert_calls += 1
        if upsert and self.upsert_calls == 1:
            raise DuplicateKeyError(
                "E11000 duplicate key error", 11000,
                {"keyPattern": self._conflict_key_pattern,
                 "keyValue": {"session_id": filter_expr.get("session_id")}})
        return await super().update_one(collection, filter_expr, update, upsert=upsert)


class TestWritePathConflictConvergence:
    @pytest.mark.asyncio
    async def test_session_id_conflict_converges_and_keeps_repository_alive(self):
        """确认是 `session_id` 约束的冲突 ⇒ 重跑一次 upsert 收敛，且不判死仓储。"""
        backend = _ConflictOnceBackend()
        repo = SessionRepository(db=backend)

        await repo.create(SessionId("conv_race"))

        assert backend.upsert_calls == 2, "应恰好重试一次（无循环）"
        assert repo.backend is not None, "并发冲突不得 _mark_unavailable() 判死仓储"
        assert await backend.count(COLLECTION, {"session_id": "conv_race"}) == 1

    @pytest.mark.asyncio
    async def test_foreign_unique_conflict_is_not_swallowed(self):
        """§60：不是 `session_id` 约束的 E11000 必须原样冒泡（不猜、不吞）。"""
        backend = _ConflictOnceBackend(key_pattern={"title": 1})
        repo = SessionRepository(db=backend)

        with pytest.raises(DuplicateKeyError):
            await repo.create(SessionId("conv_other_constraint"))

        assert backend.upsert_calls == 1, "非身份约束冲突不得触发重试"


class TestBootstrapRobustness:
    """挑战者 B 反证的三个健壮性缺口（都已修，各配守卫）。"""

    @pytest.mark.asyncio
    async def test_non_canonical_unique_index_is_recognized(self, monkeypatch, caplog):
        """同键唯一索引在**非规范名**下也必须被认作"已强制"（不建第二个同键索引）。

        只认规范名 ⇒ 对 `session_id_unique` 视而不见 ⇒ 建 `session_id_1` unique
        抛 code 85 ⇒ 退回非唯一 ⇒ 同键双索引 + 每次 connect 注定重试失败。
        """
        backend = _mock_backend(indexes=_indexes(("session_id_unique", True)))
        repo = _repo_with_backend(monkeypatch, backend)

        with caplog.at_level(logging.WARNING, logger="core.storage.session_repo"):
            assert await repo.connect() is True

        backend.drop_index.assert_not_awaited()
        assert _session_id_ensure_calls(backend) == [], "约束已生效，不得再建同键索引"
        assert "非规范名" in caplog.text, "非规范名要显式告警（schema_drift 会报 missing）"

    @pytest.mark.asyncio
    async def test_non_canonical_non_unique_index_is_upgraded(self, monkeypatch):
        """非规范名的**非唯一**同键索引同样可以被升级（按实际名字 drop）。"""
        backend = _mock_backend(indexes=_indexes(("session_id_unique", False)))
        repo = _repo_with_backend(monkeypatch, backend)

        assert await repo.connect() is True

        assert backend.drop_index.await_args.args == (COLLECTION, "session_id_unique")
        assert [kw for kw in _session_id_ensure_calls(backend)] == [{"unique": True}]

    @pytest.mark.asyncio
    async def test_list_indexes_raising_does_not_break_connect(self, monkeypatch):
        """后端**抛异常**（而非按协议返回 None）时同样不得毁掉 `connect()`。"""
        backend = _mock_backend()
        backend.list_indexes = AsyncMock(side_effect=RuntimeError("driver exploded"))
        repo = _repo_with_backend(monkeypatch, backend)

        assert await repo.connect() is True
        assert repo._db is not None  # noqa: SLF001
        assert _session_id_ensure_calls(backend) == []

    @pytest.mark.asyncio
    async def test_already_unique_skips_the_duplicate_probe(self, monkeypatch):
        """幂等早退在**昂贵探测之前**：已唯一的库上重复 connect() 不得跑 `$group`。

        真库实测该探测是全集合 COLLSCAN（20k 文档 71ms / 100k 351ms），而健康重连
        每轮都会 `connect()`。
        """
        backend = _mock_backend(indexes=_indexes((SESSION_ID_INDEX, True)))
        repo = _repo_with_backend(monkeypatch, backend)

        assert await repo.connect() is True

        backend.aggregate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rename_survives_a_foreign_unique_conflict(self):
        """`rename` 的写路径同样装 `non_fatal`：唯一冲突不得判死整个仓储。

        挑战者 B 反证（D1）：不带 `non_fatal` 时 `_call` 会 `_mark_unavailable()`
        ⇒ 整个 sessions 域 `_db=None`，而 `SessionManager.rename` 把它吞成
        "改名成功" ⇒ 用户看到 200、随后全站 503。装上护栏后冲突仍**原样冒泡**
        （不猜、不吞），但仓储活着。
        """

        class _RenameConflictBackend(FakeDocumentDBBackend):
            """`update_one` 恒抛**外部**唯一约束冲突（模拟 sessions 上有第二个唯一索引）。"""

            async def update_one(self, collection: str, filter_expr: dict, update: dict,
                                 upsert: bool = False) -> int:
                raise DuplicateKeyError("E11000 duplicate key error", 11000,
                                        {"keyPattern": {"title": 1}, "keyValue": {}})

        backend = _RenameConflictBackend()
        repo = SessionRepository(db=backend)

        with pytest.raises(DuplicateKeyError):
            await repo.rename(SessionId("conv_rn"), "新")

        assert repo.backend is not None, "写冲突不得把 sessions 域判死"


class TestDegradedPathOnTheInMemoryFake:
    """用**内存 fake** 端到端验证降级分支（此前 fake 结构性走不到这条）。

    挑战者 B（D6）：fake 的 `aggregate()` 恒 `[]` 且 `ensure_index` 不校验既有文档 ⇒
    "有重复 ⇒ 降级为非唯一"在 fake 上**永远不可达**（假绿通道）。补上 build 期校验后，
    这条路径在 fake 上也能被真实走到：探测说"无重复"→ 建唯一时被既有文档顶回
    ⇒ 落 `except` ⇒ 退回非唯一 ⇒ `connect()` 仍 True、仓储仍可用。
    """

    @pytest.mark.asyncio
    async def test_unique_build_rejected_by_existing_duplicates_falls_back(self, monkeypatch,
                                                                         caplog):
        backend = FakeDocumentDBBackend()
        for _ in range(2):      # 同 session_id 的两篇（真库并发 upsert 的产物）
            await backend.insert_one(COLLECTION, {"session_id": "conv_dup",
                                                  "messages": [],
                                                  "updated_at": "2026-08-01T00:00:00+00:00"})
        repo = _repo_with_backend(monkeypatch, backend)

        with caplog.at_level(logging.ERROR, logger="core.storage.session_repo"):
            assert await repo.connect() is True

        assert repo._db is not None, "唯一建失败不得让仓储不可用"  # noqa: SLF001
        live = {i["name"]: i for i in (await backend.list_indexes(COLLECTION) or [])}
        assert live[SESSION_ID_INDEX].get("unique") in (False, None), "必须退回非唯一"
        assert await backend.count(COLLECTION, {"session_id": "conv_dup"}) == 2, \
            "降级路径不得动数据"
