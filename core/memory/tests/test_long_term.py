"""LongTermMemory: connect 日志级别 + health_check 重连 + PK匹配 + clear_all 测试。"""
import logging
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from core.memory.long_term import LongTermMemory, MemoryEntry
from core.memory.scope import MemoryScope
from core.schema.memory import MemoryType
from core.schema.types import MemoryId


@pytest.fixture
def memory_scope() -> MemoryScope:
    """测试用的固定 scope（Task 9：无 scope 调用点迁移）。"""
    return MemoryScope(user_id="usr_a", character_instance_id="chi_a")


@pytest.mark.service
class TestLongTermMemoryConnectLogLevel:
    """connect() 失败时必须使用 logger.warning。"""

    @pytest.mark.asyncio
    async def test_connect_failure_uses_warning(self, caplog):
        ltm = LongTermMemory(max_entries=10)
        # 通过 patch factory 让 repo.connect() 正常捕获异常并返回 False
        with patch(
            "Module.database.vectordb.VectorDBFactory.create",
            side_effect=RuntimeError("connection refused"),
        ):
            with caplog.at_level(logging.WARNING):
                result = await ltm.connect()

        assert result is False
        # repo 内部记录 warning
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) >= 1
        assert "Milvus unavailable" in warnings[0].message


@pytest.mark.service
class TestLongTermMemoryHealthCheck:
    """health_check() 在连接丢失后应尝试重连。"""

    @pytest.mark.asyncio
    async def test_delegates_to_repo_when_connected(self):
        """当 repo 存在时，health_check 委托给 repo.health_check。"""
        ltm = LongTermMemory(max_entries=10)
        mock_repo = MagicMock()
        mock_repo.health_check = AsyncMock(return_value=True)
        ltm._repo = mock_repo
        result = await ltm.health_check()
        assert result is True
        mock_repo.health_check.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_false_when_repo_unhealthy(self):
        ltm = LongTermMemory(max_entries=10)
        mock_repo = MagicMock()
        mock_repo.health_check = AsyncMock(return_value=False)
        ltm._repo = mock_repo
        result = await ltm.health_check()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_repo(self):
        ltm = LongTermMemory(max_entries=10)
        ltm._repo = None
        result = await ltm.health_check()
        assert result is False


@pytest.mark.service
class TestPKMatching:
    """PK 匹配: 同文异构条目各自获得正确 distance。"""

    @pytest.mark.asyncio
    async def test_only_matching_pk_appears(self, memory_scope):
        """Milvus 只返回一个 PK 时，仅该 PK 对应条目出现（旧代码 text 匹配会两个都出现）。"""

        ltm = LongTermMemory(max_entries=10)

        # 两个同文异构条目（scope 字段与 memory_scope 对齐，参与 scope 过滤）
        e1 = MemoryEntry(
            id="id-1", text="我喜欢吃披萨", memory_type="note",
            poignancy=5, timestamp=1000.0, _milvus_pk=101,
            user_id="usr_a", character_instance_id="chi_a",
        )
        e2 = MemoryEntry(
            id="id-2", text="我喜欢吃披萨", memory_type="note",
            poignancy=8, timestamp=2000.0, _milvus_pk=102,
            user_id="usr_a", character_instance_id="chi_a",
        )
        ltm._entries = [e1, e2]

        # Milvus 只返回 PK 101，不返回 102
        mock_repo = MagicMock()
        mock_repo.search = AsyncMock(return_value=[
            {"id": 101, "text": "我喜欢吃披萨", "distance": 0.3},
        ])
        mock_repo.health_check = AsyncMock(return_value=True)
        ltm._repo = mock_repo

        results = await ltm.search(query_embedding=[0.1] * 896, top_k=5, scope=memory_scope)

        # 只有 e1 应该出现（PK 匹配），e2 不应出现（其 PK 不在 Milvus 结果中）
        ids_in_results = {e.id for e in results}
        assert "id-1" in ids_in_results
        assert "id-2" not in ids_in_results, (
            "e2 的 PK 102 不在 Milvus 结果中，不应被匹配到"
        )

    @pytest.mark.asyncio
    async def test_both_pks_in_results_both_appear(self, memory_scope):
        """两个不同 PK 的条目都在 Milvus 结果中时，两个都应出现且各自正确 distance。"""

        ltm = LongTermMemory(max_entries=10)

        e1 = MemoryEntry(
            id="id-1", text="同文", memory_type="note",
            poignancy=5, timestamp=1000.0, _milvus_pk=101,
            user_id="usr_a", character_instance_id="chi_a",
        )
        e2 = MemoryEntry(
            id="id-2", text="同文", memory_type="note",
            poignancy=5, timestamp=1000.0, _milvus_pk=102,
            user_id="usr_a", character_instance_id="chi_a",
        )
        ltm._entries = [e1, e2]

        mock_repo = MagicMock()
        mock_repo.search = AsyncMock(return_value=[
            {"id": 101, "text": "同文", "distance": 0.3},
            {"id": 102, "text": "同文", "distance": 0.7},
        ])
        mock_repo.health_check = AsyncMock(return_value=True)
        ltm._repo = mock_repo

        results = await ltm.search(query_embedding=[0.1] * 896, top_k=5, scope=memory_scope)

        # 两个 PK 都在 Milvus 结果中 → 两个都出现
        ids_in_results = {e.id for e in results}
        assert "id-1" in ids_in_results
        assert "id-2" in ids_in_results
        # e1 (distance=0.3, sim=1/1.3≈0.769) 应排在 e2 (distance=0.7, sim=1/1.7≈0.588) 前面
        # 因 poignancy/timestamp 相同，sim 是唯一差异
        idx1 = next(i for i, e in enumerate(results) if e.id == "id-1")
        idx2 = next(i for i, e in enumerate(results) if e.id == "id-2")
        assert idx1 < idx2, f"e1 (distance=0.3) 应排在 e2 (distance=0.7) 之前，实际: {idx1} vs {idx2}"

    @pytest.mark.asyncio
    async def test_text_fallback_when_no_milvus_id(self, memory_scope):
        """Milvus 返回结果中无 id 字段或无 PK 时，走 text fallback 匹配。"""

        ltm = LongTermMemory(max_entries=10)

        # 两个条目：一个有 _milvus_pk=None，另一个也无 PK
        e1 = MemoryEntry(
            id="old-1", text="历史记忆", memory_type="note",
            poignancy=5, timestamp=1000.0, _milvus_pk=None,
            user_id="usr_a", character_instance_id="chi_a",
        )
        e2 = MemoryEntry(
            id="e2", text="无ID字段测试", memory_type="note",
            poignancy=5, timestamp=1000.0, _milvus_pk=None,
            user_id="usr_a", character_instance_id="chi_a",
        )
        ltm._entries = [e1, e2]

        mock_repo = MagicMock()
        # Milvus 返回无 id 字段的结果
        mock_repo.search = AsyncMock(return_value=[
            {"text": "历史记忆", "distance": 0.5},
            {"text": "无ID字段测试", "distance": 0.2},
        ])
        mock_repo.health_check = AsyncMock(return_value=True)
        ltm._repo = mock_repo

        results = await ltm.search(query_embedding=[0.1] * 896, top_k=5, scope=memory_scope)

        assert len(results) == 2
        result_ids = {r.id for r in results}
        assert result_ids == {"old-1", "e2"}


@pytest.mark.service
class TestLongTermMemoryClearAll:
    """clear_all() 可独立调用（无需先 connect()），失败时优雅降级。"""

    @pytest.mark.asyncio
    async def test_clear_all_without_connect_creates_temp_backend(self):
        """无 _repo 时，clear_all() 自行创建临时 repo 完成清理。"""
        ltm = LongTermMemory(max_entries=10)
        # 添加一些内存条目以确保 count > 0
        ltm._entries = [MagicMock(), MagicMock(), MagicMock()]

        mock_backend = AsyncMock()
        mock_backend.collection_exists = AsyncMock(return_value=True)
        mock_backend.drop_collection = AsyncMock()
        mock_backend.create_collection = AsyncMock()
        mock_backend.close = AsyncMock()

        with patch(
            "Module.database.vectordb.VectorDBFactory.create",
            return_value=mock_backend,
        ) as mock_create:
            deleted = await ltm.clear_all()

        assert deleted == 3
        assert len(ltm._entries) == 0
        # 临时 repo 创建时调用 factory
        mock_create.assert_called_once_with("milvus/memory", base_url=None, token=None)
        mock_backend.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clear_all_with_existing_backend_reuses_it(self):
        """_repo 已设置时，clear_all() 复用它，不创建新连接。"""
        ltm = LongTermMemory(max_entries=10)
        ltm._entries = [MagicMock()]
        mock_repo = MagicMock()
        mock_repo.clear_all = AsyncMock()
        mock_repo.health_check = AsyncMock(return_value=True)  # 模拟已连接
        ltm._repo = mock_repo

        deleted = await ltm.clear_all()

        assert deleted == 1
        mock_repo.clear_all.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clear_all_without_connect_collection_not_exists(self):
        """临时路径：collection 不存在时，临时 repo 正常创建。"""
        ltm = LongTermMemory(max_entries=10)

        mock_backend = AsyncMock()
        mock_backend.collection_exists = AsyncMock(return_value=False)
        mock_backend.drop_collection = AsyncMock()
        mock_backend.create_collection = AsyncMock()
        mock_backend.close = AsyncMock()

        with patch(
            "Module.database.vectordb.VectorDBFactory.create",
            return_value=mock_backend,
        ):
            deleted = await ltm.clear_all()

        assert deleted == 0
        mock_backend.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clear_all_propagates_collection_exception(self):
        """临时路径：collection_exists 失败时异常被 repo.connect() 捕获，
        repo 内部清理 (_db=None)，LTM 的 clear_all 静默降级并调用 close()（no-op）。"""
        ltm = LongTermMemory(max_entries=10)

        mock_backend = AsyncMock()
        mock_backend.collection_exists = AsyncMock(side_effect=RuntimeError("connection refused"))
        mock_backend.close = AsyncMock()

        with patch(
            "Module.database.vectordb.VectorDBFactory.create",
            return_value=mock_backend,
        ):
            deleted = await ltm.clear_all()

        # repo.connect() 在异常时将 _db 置为 None，因此 close() 不会调用 mock_backend.close()
        # 这是 repo 内部清理行为 — mock_backend 资源在 connect 异常时由 repo 丢弃
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_clear_all_factory_create_fails_returns_count(self):
        """MemoryRepository 创建失败（极端情况）时静默降级，返回已清空的内存条目数。"""
        ltm = LongTermMemory(max_entries=10)
        ltm._entries = [MagicMock(), MagicMock()]  # 2 entries

        with patch(
            "core.storage.memory_repo.MemoryRepository",
            side_effect=RuntimeError("Cannot create repo"),
        ):
            deleted = await ltm.clear_all()

        assert deleted == 2
        assert len(ltm._entries) == 0


@pytest.mark.unit
class TestMemoryTypeNormalization:
    """MemoryEntry 构造归一化 + search 过滤 lowercase。"""

    def test_constructor_normalizes_lowercase(self):
        from core.memory.long_term import MemoryEntry
        e = MemoryEntry(id="x", text="t", memory_type="fact")
        assert e.memory_type is MemoryType.FACT

    def test_constructor_unknown_falls_back_note(self):
        from core.memory.long_term import MemoryEntry
        e = MemoryEntry(id="x", text="t", memory_type="bogus")
        assert e.memory_type is MemoryType.NOTE

    @pytest.mark.asyncio
    async def test_search_filter_case_insensitive(self, memory_scope):
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        await mem.insert("工作计划A", memory_type="plan",
                         user_id=memory_scope.user_id,
                         character_instance_id=memory_scope.character_instance_id)
        hits = await mem.search(query_text="工作", memory_type="PLAN", top_k=10,
                                scope=memory_scope)
        assert len(hits) == 1
        assert hits[0].memory_type is MemoryType.PLAN

    @pytest.mark.asyncio
    async def test_search_filter_unknown_returns_empty(self, memory_scope):
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        await mem.insert("工作计划A", memory_type="plan",
                         user_id=memory_scope.user_id,
                         character_instance_id=memory_scope.character_instance_id)
        hits = await mem.search(query_text="工作", memory_type="bogus", top_k=10,
                                scope=memory_scope)
        assert hits == []


class FakeMemoryRepo:
    """B5/P1-12 专用 fake：可配置 insert PK / delete 返回值。"""

    def __init__(self, insert_pks=None, delete_result=1):
        self.insert_pks = list(insert_pks or [])
        self.delete_result = delete_result
        self.insert_calls = 0
        self.deleted: list[list[int]] = []
        self.query_results: list[dict] = []

    async def insert(self, vectors, metadata):
        self.insert_calls += 1
        if not self.insert_pks:
            return []
        return [self.insert_pks.pop(0)]

    async def delete(self, ids):
        self.deleted.append(list(ids))
        return self.delete_result

    async def query(self, *, filter_expr=None, output_fields=None, limit=100):
        return self.query_results

    async def search(self, *a, **kw):
        return []

    async def connect(self):
        return True

    async def health_check(self):
        return True

    async def count(self):
        return 0

    async def clear_all(self):
        return 0

    async def close(self):
        pass


@pytest.mark.unit
def test_memory_entry_scope_fields():
    """MemoryEntry 必须有 scope 顶层字段（user_id/character_instance_id/visibility）。"""
    from core.memory.long_term import MemoryEntry
    e = MemoryEntry(
        id="m_1", text="hi", user_id="usr_a",
        character_instance_id="chi_a", visibility="character_private",
    )
    assert e.user_id == "usr_a"
    assert e.character_instance_id == "chi_a"
    assert e.visibility == "character_private"


@pytest.mark.service
class TestMemoryOfflineConsistency:
    """B5/P1-12：离线 insert/sync/delete 不丢数据、不复活旧数据。"""

    async def _ltm_with_embedding(self, repo):
        from unittest.mock import AsyncMock
        ltm = LongTermMemory(repo=repo)
        ltm._embedding_provider.embed = AsyncMock(return_value=[0.1, 0.2])  # noqa: SLF001
        return ltm

    @pytest.mark.asyncio
    async def test_insert_empty_pk_marks_dirty(self, memory_scope):
        """repo 离线（insert 返回空 PK）→ 不能视为成功，标记 dirty 待同步。"""
        repo = FakeMemoryRepo()  # insert 返回 []
        ltm = await self._ltm_with_embedding(repo)

        await ltm.insert("离线记忆", source="consolidation",
                         user_id=memory_scope.user_id,
                         character_instance_id=memory_scope.character_instance_id)

        entry = ltm._entries[0]  # noqa: SLF001
        assert entry._milvus_pk is None  # noqa: SLF001
        assert entry.id in ltm._dirty_ids  # noqa: SLF001 — 未假成功

    @pytest.mark.asyncio
    async def test_sync_dirty_keeps_dirty_on_empty_pk(self, memory_scope):
        """sync 遇空 PK（仍离线）→ 保留 dirty 不计数；拿到 PK 后才移除。"""
        repo = FakeMemoryRepo()  # 第一轮 []，第二轮可补
        ltm = await self._ltm_with_embedding(repo)
        await ltm.insert("x", source="consolidation",
                         user_id=memory_scope.user_id,
                         character_instance_id=memory_scope.character_instance_id)
        eid = ltm._entries[0].id  # noqa: SLF001

        first = await ltm.sync_dirty_entries()
        assert first == 0
        assert eid in ltm._dirty_ids  # noqa: SLF001 — 空 PK 不丢弃

        repo.insert_pks.append(42)
        second = await ltm.sync_dirty_entries()
        assert second == 1
        assert eid not in ltm._dirty_ids  # noqa: SLF001
        assert ltm._entries[0]._milvus_pk == 42  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_delete_offline_records_pending_tombstone(self, memory_scope):
        """离线删除（repo.delete 返回 0）→ 记持久 tombstone，不复活旧行。"""
        repo = FakeMemoryRepo(delete_result=0)
        ltm = await self._ltm_with_embedding(repo)
        await ltm.insert("x", source="consolidation",
                         user_id=memory_scope.user_id,
                         character_instance_id=memory_scope.character_instance_id)
        entry = ltm._entries[0]  # noqa: SLF001
        entry._milvus_pk = 7  # noqa: SLF001 — 已同步
        await ltm.sync_dirty_entries()

        assert await ltm.delete(entry.id, scope=memory_scope) is True
        assert entry.id in ltm._pending_deletes  # noqa: SLF001 — tombstone
        assert ltm._pending_deletes[entry.id] == 7  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_reload_dedup_no_duplicates(self):
        """连续 connect 幂等 reload：按 entry_id 去重，不重复 append。"""
        repo = FakeMemoryRepo()
        repo.query_results = [
            {"id": 1, "entry_id": "mem_a", "text": "t", "memory_type": "Note",
             "poignancy": 5, "timestamp": 0.0},
        ]
        ltm = LongTermMemory(repo=repo)
        await ltm.connect()
        assert ltm.count == 1
        await ltm.connect()  # 再连一次
        assert ltm.count == 1  # 不倍增

    @pytest.mark.asyncio
    async def test_pending_delete_applied_on_connect(self):
        """重启后 pending tombstone 应用：重连清理旧行。"""
        repo = FakeMemoryRepo()
        ltm = LongTermMemory(repo=repo)
        ltm._pending_deletes = {MemoryId("mem_gone"): 5}  # noqa: SLF001

        await ltm.connect()

        assert repo.deleted and 5 in repo.deleted[0]
        assert MemoryId("mem_gone") not in ltm._pending_deletes  # noqa: SLF001
