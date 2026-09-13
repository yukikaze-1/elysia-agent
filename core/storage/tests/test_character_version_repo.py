"""CharacterVersionRepository 服务测试（内存 fake 存储，不连真实 MongoDB）。

仓储 `connect()` 走 `DocumentDBFactory.create("mongodb/default")`——默认配置下那是
**开发者本机生产库**。故请求根 conftest 的 `fake_storage` fixture 把该连接重定向到内存
`FakeDocumentDBBackend`（见 core/tests/_real_db_guard.py）。
"""
import asyncio

import pytest
import pytest_asyncio
from core.schema.types import SessionId
from core.storage.character_version_repo import CharacterVersionRepository
from core.schema.compression import CompressionTrigger

COLLECTION = "character_versions"

pytestmark = [pytest.mark.service, pytest.mark.usefixtures("fake_storage")]


@pytest.mark.service
class TestCharacterVersionRepo:
    @pytest_asyncio.fixture
    async def repo(self):
        from core.storage.character_version_repo import CharacterVersionRepository
        r = CharacterVersionRepository()
        await r.connect()
        # 清理上次运行的残留数据，确保测试隔离
        if r._db is not None:
            try:
                await r._db.delete_many(COLLECTION, {"session_id": {"$regex": "^test"}})
            except Exception:
                pass
        yield r
        await r.close()

    @pytest.mark.asyncio
    async def test_append_and_get(self, repo):
        sid = SessionId("test-append")
        v = await repo.append_version(
            session_id=sid, text="我是一个测试",
            timestamp="2026-07-21T00:00:00.000Z",
            trigger=CompressionTrigger.COMPRESSION,
            change_summary=["新增: 测试条目"],
        )
        assert v == 1
        entry = await repo.get_version(sid, 1)
        assert entry is not None
        assert entry["text"] == "我是一个测试"
        assert entry["trigger"] == "compression"
        assert entry["change_summary"] == ["新增: 测试条目"]

    @pytest.mark.asyncio
    async def test_append_auto_increment(self, repo):
        sid = SessionId("test-increment")
        v1 = await repo.append_version(
            session_id=sid, text="v1",
            timestamp="2026-07-21T00:00:00.000Z",
            trigger=CompressionTrigger.COMPRESSION,
            change_summary=[],
        )
        v2 = await repo.append_version(
            session_id=sid, text="v2",
            timestamp="2026-07-21T01:00:00.000Z",
            trigger=CompressionTrigger.FLUSH,
            change_summary=["新增: v2"],
        )
        assert v1 == 1
        assert v2 == 2

    @pytest.mark.asyncio
    async def test_list_versions_pagination(self, repo):
        sid = SessionId("test-list")
        for i in range(5):
            await repo.append_version(
                session_id=sid, text=f"版本{i}",
                timestamp=f"2026-07-21T0{i}:00:00.000Z",
                trigger=CompressionTrigger.COMPRESSION,
                change_summary=[f"改动{i}"],
            )
        # 默认返回最近 20 条
        versions = await repo.list_versions(sid)
        assert len(versions) == 5
        assert versions[0]["version"] == 5  # 最新在前
        assert versions[4]["version"] == 1

        # before_version 分页
        page2 = await repo.list_versions(sid, before_version=3)
        assert len(page2) == 2
        assert page2[0]["version"] == 2
        assert page2[1]["version"] == 1

    @pytest.mark.asyncio
    async def test_count(self, repo):
        sid = SessionId("test-count")
        for i in range(3):
            await repo.append_version(
                session_id=sid, text=f"版本{i}",
                timestamp=f"2026-07-21T0{i}:00:00.000Z",
                trigger=CompressionTrigger.COMPRESSION,
                change_summary=[],
            )
        assert await repo.count_versions(sid) == 3

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, repo):
        result = await repo.get_version(SessionId("no-such"), 999)
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_list(self, repo):
        versions = await repo.list_versions(SessionId("no-such"))
        assert versions == []


@pytest.mark.service
class TestCharacterVersionRepoContentType:
    """content_type 过滤和独立计数。"""

    @pytest_asyncio.fixture
    async def repo(self):
        from core.storage.character_version_repo import CharacterVersionRepository
        r = CharacterVersionRepository()
        await r.connect()
        # 清理上次运行的残留数据，确保测试隔离
        if r._db is not None:
            try:
                await r._db.delete_many(COLLECTION, {"session_id": {"$regex": "^test"}})
            except Exception:
                pass
        yield r
        await r.close()

    @pytest.mark.asyncio
    async def test_append_version_c6_type(self, repo):
        """c6 类型 append → version=1（独立于 L2）。"""
        v = await repo.append_version(
            session_id="test_ct", text="C6 text",
            timestamp="2026-07-22T00:00:00+08:00",
            trigger=CompressionTrigger.C6_REFRESH,
            change_summary=["调整"], content_type="c6",
        )
        assert v == 1

    @pytest.mark.asyncio
    async def test_l2_c6_independent_versions(self, repo):
        """L2 和 C6 版本号互不干扰。"""
        sid = "test_independent"
        await repo.append_version(sid, "l2_v1", "2026-07-22T00:00:00+08:00",
                                  CompressionTrigger.COMPRESSION, [],
                                  content_type="l2")
        v_c6 = await repo.append_version(sid, "c6_v1", "2026-07-22T00:00:00+08:00",
                                         CompressionTrigger.C6_REFRESH, [],
                                         content_type="c6")
        assert v_c6 == 1  # C6 从 1 开始
        v_l2_2 = await repo.append_version(sid, "l2_v2", "2026-07-22T00:00:00+08:00",
                                            CompressionTrigger.COMPRESSION, [],
                                            content_type="l2")
        assert v_l2_2 == 2  # L2 继续增长
        v_c6_2 = await repo.append_version(sid, "c6_v2", "2026-07-22T00:00:00+08:00",
                                            CompressionTrigger.C6_REFRESH, [],
                                            content_type="c6")
        assert v_c6_2 == 2  # C6 也独立增长

    @pytest.mark.asyncio
    async def test_list_versions_filtered_by_type(self, repo):
        """content_type 过滤：只返回匹配类型的版本。"""
        sid = "test_filter_type"
        await repo.append_version(sid, "l2_v1", "2026-07-22T00:00:00+08:00",
                                  CompressionTrigger.COMPRESSION, [], content_type="l2")
        await repo.append_version(sid, "c6_v1", "2026-07-22T00:00:00+08:00",
                                  CompressionTrigger.C6_REFRESH, [], content_type="c6")

        l2_versions = await repo.list_versions(sid, content_type="l2")
        assert len(l2_versions) == 1
        assert l2_versions[0]["content_type"] == "l2"

        c6_versions = await repo.list_versions(sid, content_type="c6")
        assert len(c6_versions) == 1
        assert c6_versions[0]["content_type"] == "c6"

    @pytest.mark.asyncio
    async def test_count_versions_by_type(self, repo):
        """count_versions 按 content_type 分别计数。"""
        sid = "test_count_by_type"
        await repo.append_version(sid, "l2_v1", "2026-07-22T00:00:00+08:00",
                                  CompressionTrigger.COMPRESSION, [], content_type="l2")
        await repo.append_version(sid, "c6_v1", "2026-07-22T00:00:00+08:00",
                                  CompressionTrigger.C6_REFRESH, [], content_type="c6")
        await repo.append_version(sid, "c6_v2", "2026-07-22T00:00:00+08:00",
                                  CompressionTrigger.C6_REFRESH, [], content_type="c6")

        assert await repo.count_versions(sid, content_type="l2") == 1
        assert await repo.count_versions(sid, content_type="c6") == 2

    @pytest.mark.asyncio
    async def test_default_content_type_is_l2(self, repo):
        """默认 content_type="l2" 向后兼容。"""
        sid = "test_default_type"
        v = await repo.append_version(sid, "text", "2026-07-22T00:00:00+08:00",
                                       CompressionTrigger.COMPRESSION, [])
        assert v == 1
        versions = await repo.list_versions(sid)
        assert len(versions) == 1
        assert versions[0].get("content_type", "l2") == "l2"


@pytest.mark.unit
class TestCharacterVersionRepoConnectFallback:
    """连接期重复检测的保守降级（单元，mock DocumentDBFactory，无需 MongoDB）。"""

    def _make_repo(self, monkeypatch, backend, *, existing_indexes=None):
        """装配 repo + mock backend。

        Args:
            existing_indexes: `list_indexes()` 的返回值（默认空 = 全新库无既有索引）。
                新增于 2026-09-11——`_ensure_version_index` 会先读现有索引以规避
                「同名不同选项 → OperationFailure」（见该方法纪律 2）。
        """
        from unittest.mock import AsyncMock

        from Module.database.documentdb import DocumentDBFactory
        from core.storage.character_version_repo import CharacterVersionRepository

        backend.ensure_index = AsyncMock()
        backend.list_indexes = AsyncMock(return_value=list(existing_indexes or []))
        monkeypatch.setattr(DocumentDBFactory, "create", lambda *a, **kw: backend)
        return CharacterVersionRepository(), backend

    @pytest.mark.asyncio
    async def test_option_conflict_skips_create_index(self, monkeypatch):
        """**回归防护（2026-09-11 实测确认的哑弹）**：既有索引为 unique 时，
        检测失败会算出 desired=False；若照旧调用 `create_index(unique=False)`，
        Mongo 抛 `OperationFailure`（"An existing index has the same name..."）
        → 外层 except 把 `_db` 置 None → **整个 repo 不可用**。

        现在改为：先 `list_indexes()` 比对，选项冲突则**保留现有索引**并告警，
        **绝不**重复调用 `create_index`，且 connect() 仍返回 True。
        """
        from unittest.mock import AsyncMock, MagicMock

        name = "session_id_1_content_type_1_version_-1"
        backend = MagicMock()
        # 检测失败 → desired=False；但库里已是 unique=True → 冲突
        backend.aggregate = AsyncMock(side_effect=RuntimeError("network down"))
        repo, backend = self._make_repo(
            monkeypatch, backend,
            existing_indexes=[{"name": "_id_", "unique": False},
                              {"name": name, "unique": True}],
        )

        ok = await repo.connect()

        assert ok is True, "选项冲突不得让 connect() 失败"
        assert repo._db is not None, "repo 必须保持可用（这正是该哑弹的伤害）"
        backend.ensure_index.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_matching_option_does_not_conflict(self, monkeypatch):
        """既有索引选项与期望一致 → 照常调用 ensure_index（幂等，正常路径）。"""
        from unittest.mock import AsyncMock, MagicMock

        name = "session_id_1_content_type_1_version_-1"
        backend = MagicMock()
        backend.aggregate = AsyncMock(return_value=[])
        repo, backend = self._make_repo(
            monkeypatch, backend,
            existing_indexes=[{"name": "_id_", "unique": False},
                              {"name": name, "unique": True}],
        )

        ok = await repo.connect()
        assert ok is True
        backend.ensure_index.assert_awaited_once()
        assert backend.ensure_index.call_args.kwargs.get("unique") is True

    @pytest.mark.asyncio
    async def test_unique_restored_after_index_dropped(self, monkeypatch):
        """索引被 drop 后（冲突解除）→ 正常重建唯一索引（唯一性可恢复）。"""
        from unittest.mock import AsyncMock, MagicMock

        backend = MagicMock()
        backend.aggregate = AsyncMock(return_value=[])  # 数据已干净
        repo, backend = self._make_repo(monkeypatch, backend, existing_indexes=[])

        ok = await repo.connect()
        assert ok is True
        assert backend.ensure_index.call_args.kwargs.get("unique") is True

    @pytest.mark.asyncio
    async def test_aggregate_failure_falls_back_to_non_unique_index(self, monkeypatch):
        """aggregate 失败（网络/API 不支持）→ 保守建非唯一索引，repo 不被拖垮（S8）。

        原实现把 aggregate 失败当成「无重复」→ 建唯一索引 → ensure_index 失败
        → 整个 repo 降级不可用。现在区分：检测失败时保守用非唯一索引。
        """
        from unittest.mock import AsyncMock, MagicMock

        backend = MagicMock()
        backend.aggregate = AsyncMock(side_effect=RuntimeError("network down"))
        repo, backend = self._make_repo(monkeypatch, backend)

        ok = await repo.connect()
        assert ok is True
        assert repo._db is not None
        call_kwargs = backend.ensure_index.call_args.kwargs
        assert call_kwargs.get("unique") is False

    @pytest.mark.asyncio
    async def test_real_duplicates_still_use_non_unique_index(self, monkeypatch):
        """aggregate 返回真实重复 → 非唯一索引（既有行为保留）。"""
        from unittest.mock import AsyncMock, MagicMock

        backend = MagicMock()
        backend.aggregate = AsyncMock(return_value=[{"_id": {"sid": "s"}, "count": 2}])
        repo, backend = self._make_repo(monkeypatch, backend)

        ok = await repo.connect()
        assert ok is True
        assert backend.ensure_index.call_args.kwargs.get("unique") is False

    @pytest.mark.asyncio
    async def test_no_duplicates_uses_unique_index(self, monkeypatch):
        """无重复 → 唯一索引（正常路径）。"""
        from unittest.mock import AsyncMock, MagicMock

        backend = MagicMock()
        backend.aggregate = AsyncMock(return_value=[])
        repo, backend = self._make_repo(monkeypatch, backend)

        ok = await repo.connect()
        assert ok is True
        assert backend.ensure_index.call_args.kwargs.get("unique") is True


# ══════════════════════════════════════════════════════════════════
# S3-11：append_version 必须 fail-closed
# ══════════════════════════════════════════════════════════════════

class TestAppendVersionFailClosed:
    """`append_version` 原先 `_db is None → return 0`（静默）。

    调用方（`CompressionManager` 的 L2 演化链）据此无法区分"没写进去"与"版本号就是
    0"，于是 **L2 已变、无版本记录、C6 不刷新，且处理计数已推进 → 永不重试**。
    同仓的 `list_versions` / `get_version` 读路径本来就返回空，写路径必须 fail-closed。
    """

    def test_append_version_raises_when_db_none(self):
        from core.storage.base import StorageUnavailableError

        repo = CharacterVersionRepository(db=None)
        with pytest.raises(StorageUnavailableError):
            asyncio.run(repo.append_version(
                session_id=SessionId("sid"), text="t", timestamp="2026-01-01T00:00:00Z",
                trigger=CompressionTrigger.COMPRESSION, change_summary=[]))

    def test_write_path_uses_connection_guard(self):
        """结构性：写路径必须走守卫，而不是自己判 None 后返回默认值。"""
        import ast
        import pathlib

        src = pathlib.Path("core/storage/character_version_repo.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "append_version")
        body = ast.dump(fn)
        assert "_require_connected" in body, (
            "append_version 未走连接守卫——又会退回静默返回 0")


# ══════════════════════════════════════════════════════════════════
# #27：版本号分配必须原子（并发抢占 → 重算，而不是重复 / 500）
# ══════════════════════════════════════════════════════════════════

class _RaceGate:
    """并发闸门：等 N 个写者都到达后一起放行（之后立即放行）。

    用来把"两个 append 读到同一个 max"这件事**确定性**地复现出来——fake 后端内部
    没有 await 交错点，单靠 `asyncio.gather` 两个协程其实是顺序跑完的，测不到竞态。
    """

    def __init__(self, participants: int = 2) -> None:
        self._need = participants
        self._arrived = 0
        self._open = asyncio.Event()

    async def wait(self) -> None:
        self._arrived += 1
        if self._arrived >= self._need:
            self._open.set()
        await self._open.wait()


class _RaceDocumentDB:
    """把 `insert_one` 挂到闸门上的 fake 代理——其余操作原样转发。"""

    def __init__(self, inner, gate: _RaceGate) -> None:
        self._inner = inner
        self._gate = gate

    def __getattr__(self, name):  # noqa: ANN001, ANN204
        return getattr(self._inner, name)

    async def insert_one(self, collection: str, document: dict) -> str:
        await self._gate.wait()
        return await self._inner.insert_one(collection, document)


async def _run_concurrent_appends(*, with_unique_index: bool) -> list[int]:
    """两个并发 append 打同一 (session_id, content_type)，返回拿到的版本号。

    Args:
        with_unique_index: 是否先声明 `(session_id, content_type, version)` 唯一索引。
            False 用来复现"索引已降级为非唯一"这一**受支持的运维状态**
            （见 `_ensure_version_index` 纪律 3：降级永不自我修复）。
    """
    from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

    backend = FakeDocumentDBBackend()
    if with_unique_index:
        await backend.ensure_index(
            COLLECTION,
            [("session_id", 1), ("content_type", 1), ("version", -1)],
            unique=True,
        )
    repo = CharacterVersionRepository(db=backend)
    gate = _RaceGate(participants=2)
    repo._db = _RaceDocumentDB(backend, gate)
    repo._injected_db = True

    sid = SessionId("test-race")
    kwargs = {
        "timestamp": "2026-07-22T00:00:00+08:00",
        "trigger": CompressionTrigger.COMPRESSION,
        "change_summary": ["并发"],
    }
    versions = await asyncio.gather(
        repo.append_version(session_id=sid, text="A", **kwargs),
        repo.append_version(session_id=sid, text="B", **kwargs),
    )
    stored = await backend.find(COLLECTION, {"session_id": sid}, limit=10)
    return list(versions) + [len(stored)]


class TestAppendVersionAtomicity:
    """#27：`append_version` 的 `max+1` 原先非原子（S2-B7(a)），两条出路都坏——
    自建唯一索引健康时 `DuplicateKeyError` 未捕获冒泡 500；索引已降级为非唯一时
    **静默**写入重复版本号，污染 L2/C6 历史（`list_versions` 按版本号排序展示，
    重复版本会让回滚选错目标）。
    """

    @pytest.mark.asyncio
    async def test_concurrent_appends_get_distinct_versions_with_unique_index(self):
        """唯一索引健康：两个并发 append 拿到 1 与 2，无 500、无重复。"""
        v_a, v_b, stored_count = await _run_concurrent_appends(with_unique_index=True)
        assert sorted([v_a, v_b]) == [1, 2], f"并发版本号未去重/未重算: {v_a}, {v_b}"
        assert stored_count == 2

    @pytest.mark.asyncio
    async def test_concurrent_appends_distinct_even_without_unique_index(self):
        """唯一索引**已降级**（受支持的运维状态）：仍须拿到 1 与 2。

        这是"两条出路都坏"的第二条：没有自建唯一索引时，重复写入不会报错，
        旧实现会**静默**落两条 `version=1`。修复后仲裁者是文档 `_id`（Mongo 内建
        唯一索引恒存在），与 `ensure_index` 健康与否无关。
        """
        v_a, v_b, stored_count = await _run_concurrent_appends(with_unique_index=False)
        assert sorted([v_a, v_b]) == [1, 2], (
            f"唯一索引降级后并发版本号重复（静默污染版本历史）: {v_a}, {v_b}")
        assert stored_count == 2

    @pytest.mark.asyncio
    async def test_version_slot_id_is_injective(self):
        """`_version_slot_id` 对 `(sid, content_type, version)` 必须是单射。"""
        sid = SessionId("s")
        ids = {
            CharacterVersionRepository._version_slot_id(sid, "l2", 1),
            CharacterVersionRepository._version_slot_id(sid, "c6", 1),
            CharacterVersionRepository._version_slot_id(sid, "l2", 2),
            CharacterVersionRepository._version_slot_id(SessionId("s2"), "l2", 1),
        }
        assert len(ids) == 4, "版本槽位 _id 冲突——不同版本会互相抢占"

    @pytest.mark.asyncio
    async def test_append_version_routes_backend_failure_through_call(self):
        """写入后端真实故障 → `StorageUnavailableError`（而不是裸 driver 异常 500）。

        旧实现裸调 `db.insert_one`，绕过 `_call` 的统一失败语义（S2-B7(a) 的
        "裸 insert_one 绕过 `_call`"）。
        """
        from core.storage.base import StorageUnavailableError
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend

        backend = FakeDocumentDBBackend()
        repo = CharacterVersionRepository(db=backend)
        repo._injected_db = True

        async def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("mongo 挂了")

        backend.insert_one = _boom  # type: ignore[method-assign]
        with pytest.raises(StorageUnavailableError):
            await repo.append_version(
                session_id=SessionId("sid"), text="t",
                timestamp="2026-07-22T00:00:00+08:00",
                trigger=CompressionTrigger.COMPRESSION, change_summary=[])
