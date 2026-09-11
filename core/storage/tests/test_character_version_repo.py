"""CharacterVersionRepository 服务测试（内存 fake 存储，不连真实 MongoDB）。

仓储 `connect()` 走 `DocumentDBFactory.create("mongodb/default")`——默认配置下那是
**开发者本机生产库**。故请求根 conftest 的 `fake_storage` fixture 把该连接重定向到内存
`FakeDocumentDBBackend`（见 core/tests/_real_db_guard.py）。
"""
import pytest
import pytest_asyncio
from core.schema.types import SessionId
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
