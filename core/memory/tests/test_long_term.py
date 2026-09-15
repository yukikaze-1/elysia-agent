"""LongTermMemory: connect 日志级别 + health_check 重连 + PK匹配 + clear_all 测试。"""
import logging
import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

from core.memory.long_term import LongTermMemory, MemoryEntry
from core.memory.scope import MemoryScope
from core.schema.memory import MemoryType
from core.schema.types import MemoryId
from core.storage.base import StorageUnavailableError
from core.storage.destructive_guard import DestructiveConfirmRequired


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
    """clear_all() 可独立调用（无需先 connect()），且**破坏性操作 fail-closed**。

    2026-09-11 语义变更：此前存储失败**静默降级**（按内存条数返回，调用方误以为
    已清空），现改为向上抛出；顺序也改为「先动存储、后清内存」——drop 失败则内存
    保持不变（状态一致、可重试）。
    """

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
            deleted = await ltm.clear_all(confirm="ltm")

        assert deleted == 3
        assert len(ltm._entries) == 0
        # 临时 repo 创建时调用 factory
        mock_create.assert_called_once_with("milvus/memory", base_url=None, token=None)
        mock_backend.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clear_all_with_existing_backend_reuses_it(self):
        """_repo 已设置时，clear_all() 复用它，不创建新连接，并透传确认令牌。"""
        ltm = LongTermMemory(max_entries=10)
        ltm._entries = [MagicMock()]
        mock_repo = MagicMock()
        mock_repo.clear_all = AsyncMock()
        mock_repo.health_check = AsyncMock(return_value=True)  # 模拟已连接
        ltm._repo = mock_repo

        deleted = await ltm.clear_all(confirm="ltm")

        assert deleted == 1
        mock_repo.clear_all.assert_awaited_once_with(confirm="ltm")

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
            deleted = await ltm.clear_all(confirm="ltm")

        assert deleted == 0
        mock_backend.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_storage_failure_raises_and_keeps_entries(self):
        """存储不可达时**抛出**（不再静默降级），且内存条目保持不变（可重试）。

        此前 `except Exception: pass` 会按内存条数返回，debug UI 显示「已清空 N 条」
        而 Milvus 根本没动。
        """
        ltm = LongTermMemory(max_entries=10)
        ltm._entries = [MagicMock(), MagicMock()]

        mock_backend = AsyncMock()
        mock_backend.collection_exists = AsyncMock(side_effect=RuntimeError("connection refused"))
        mock_backend.close = AsyncMock()

        with patch(
            "Module.database.vectordb.VectorDBFactory.create",
            return_value=mock_backend,
        ):
            with pytest.raises(StorageUnavailableError):
                await ltm.clear_all(confirm="ltm")

        # 先动存储、后清内存：drop 未成功 → 内存未被清空
        assert len(ltm._entries) == 2

    @pytest.mark.asyncio
    async def test_repo_construction_failure_propagates(self):
        """MemoryRepository 构造失败（极端情况）不再吞掉，向上抛出。"""
        ltm = LongTermMemory(max_entries=10)
        ltm._entries = [MagicMock(), MagicMock()]  # 2 entries

        with patch(
            "core.storage.memory_repo.MemoryRepository",
            side_effect=RuntimeError("Cannot create repo"),
        ):
            with pytest.raises(RuntimeError, match="Cannot create repo"):
                await ltm.clear_all(confirm="ltm")

        assert len(ltm._entries) == 2

    @pytest.mark.asyncio
    async def test_wrong_confirm_rejected_without_touching_store(self):
        """确认令牌不匹配 → fail-closed，不 drop、不清内存。

        用**真实** MemoryRepository（mock 后端）以覆盖实际校验路径——mock repo
        不会执行校验，测不出这条不变量。
        """
        from core.storage.memory_repo import MemoryRepository

        repo = MemoryRepository(db=MagicMock())
        repo._db.drop_collection = AsyncMock()  # type: ignore[reportOptionalMemberAccess]
        repo._db.create_collection = AsyncMock()  # type: ignore[reportOptionalMemberAccess]

        ltm = LongTermMemory(max_entries=10)
        ltm._entries = [MagicMock()]
        ltm._repo = repo

        with pytest.raises(DestructiveConfirmRequired):
            await ltm.clear_all(confirm="long_term_memory_v2")

        repo._db.drop_collection.assert_not_awaited()  # type: ignore[reportOptionalMemberAccess]
        assert len(ltm._entries) == 1


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
    """B5/P1-12 专用 fake：可配置 insert PK / delete 返回值 / **离线 raise**。

    `offline=True` 复刻 `MemoryRepository` 在 #31 写路径收口之后的真实行为：
    离线时 **raise `StorageUnavailableError`**（而不是返回 `[]` / `0`）。两个收口点
    （`LongTermMemory._repo_insert` / `_repo_delete`）必须把它翻译成 dirty / tombstone
    语义——否则 Milvus 一抖就打断整个回合。
    """

    def __init__(self, insert_pks=None, delete_result=1, *, offline=False):
        self.insert_pks = list(insert_pks or [])
        self.delete_result = delete_result
        self.offline = offline
        self.insert_calls = 0
        self.delete_calls = 0
        self.deleted: list[list[int]] = []
        self.query_results: list[dict] = []

    async def insert(self, vectors, metadata):
        self.insert_calls += 1
        if self.offline:
            from core.storage.base import StorageUnavailableError
            raise StorageUnavailableError("milvus offline")
        if not self.insert_pks:
            return []
        return [self.insert_pks.pop(0)]

    async def delete(self, ids):
        self.delete_calls += 1
        if self.offline:
            from core.storage.base import StorageUnavailableError
            raise StorageUnavailableError("milvus offline")
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
    async def test_offline_repo_raise_is_translated_to_dirty(self, memory_scope):
        """#31：repo 离线 **raise**（不再返回空）→ 收口点翻译成 dirty + journal。

        反证：去掉 `LongTermMemory._repo_insert` 的 `except StorageUnavailableError`
        → 本用例红（异常冒泡进回合）。
        """
        from core.storage.base import StorageUnavailableError
        repo = FakeMemoryRepo(offline=True)
        ltm = await self._ltm_with_embedding(repo)

        # 不抛：离线是**预期状态**，语义是"记 dirty 待补写"，不是"打断回合"
        await ltm.insert("离线记忆", source="consolidation",
                         user_id=memory_scope.user_id,
                         character_instance_id=memory_scope.character_instance_id)

        entry = ltm._entries[0]  # noqa: SLF001
        assert entry._milvus_pk is None  # noqa: SLF001
        assert entry.id in ltm._dirty_ids  # noqa: SLF001
        assert repo.insert_calls == 1, "仍然要真的尝试过写"
        assert ltm._milvus_degraded_ops == 1  # noqa: SLF001 — 降级可见（不是静默）
        assert ltm._milvus_degraded_warned is True  # noqa: SLF001
        with pytest.raises(StorageUnavailableError):
            # 直接调仓储层仍然是 fail-closed（收口点在 LTM 一层，不是把 repo 改回静默）
            await repo.insert(vectors=[], metadata=[])

    @pytest.mark.asyncio
    async def test_offline_repo_raise_on_delete_records_tombstone(self, memory_scope):
        """删除收口点：repo 离线 raise → 记持久 tombstone（旧行不会重启复活）。"""
        repo = FakeMemoryRepo(offline=True)
        ltm = await self._ltm_with_embedding(repo)
        await ltm.insert("x", source="consolidation",
                         user_id=memory_scope.user_id,
                         character_instance_id=memory_scope.character_instance_id)
        entry = ltm._entries[0]  # noqa: SLF001
        entry._milvus_pk = 7  # noqa: SLF001 — 假装已同步过
        ltm._dirty_ids.discard(entry.id)  # noqa: SLF001

        assert await ltm.delete(entry.id, scope=memory_scope) is True

        assert entry.id in ltm._pending_deletes  # noqa: SLF001 — tombstone 已登记
        assert repo.delete_calls == 1, "仍然要真的尝试过删"
        # 2 次 = insert 1 次 + delete 1 次（都经收口点）；去掉 delete 收口点的翻译
        # 就只剩 1 次（调用点的宽 except 会把 raise 吞掉且不给降级标记）→ 用例红
        assert ltm._milvus_degraded_ops == 2  # noqa: SLF001 — 降级经收口点可见

    @pytest.mark.asyncio
    async def test_degraded_window_warns_once_then_debug(self, memory_scope, caplog):
        """降级告警**窗口内只 WARNING 一次**（Milvus 长离线不刷屏），但绝不静默。"""
        import logging
        repo = FakeMemoryRepo(offline=True)
        ltm = await self._ltm_with_embedding(repo)

        with caplog.at_level(logging.DEBUG, logger="core.memory.long_term"):
            for i in range(3):
                await ltm.insert(f"m{i}", source="consolidation",
                                 user_id=memory_scope.user_id,
                                 character_instance_id=memory_scope.character_instance_id)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, f"窗口内应只 WARNING 一次，实得 {len(warnings)}"
        assert ltm._milvus_degraded_ops == 3  # noqa: SLF001 — 次数仍然可见

    @pytest.mark.asyncio
    async def test_recovery_rearms_the_warning(self, memory_scope, caplog):
        """补写成功 = Milvus 恢复 → 重新武装：下一段故障仍会 WARNING 一次。"""
        import logging
        repo = FakeMemoryRepo(offline=True)
        ltm = await self._ltm_with_embedding(repo)
        await ltm.insert("x", source="consolidation",
                         user_id=memory_scope.user_id,
                         character_instance_id=memory_scope.character_instance_id)
        assert ltm._milvus_degraded_warned is True  # noqa: SLF001

        repo.offline = False
        repo.insert_pks.append(99)
        with caplog.at_level(logging.INFO, logger="core.memory.long_term"):
            assert await ltm.sync_dirty_entries() == 1

        assert ltm._milvus_degraded_warned is False  # noqa: SLF001 — 已重新武装
        assert ltm._milvus_degraded_ops == 0  # noqa: SLF001

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


# ---------------------------------------------------------------------------
# P2-1：embedding 维度与向量库集合维度对齐
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestEmbeddingDimensionAlignment:
    """集合固定维度必须透传到 embedding provider，避免维度不匹配导致写入被拒。

    背景：Milvus 集合固定 1024 维；embedding fallback 首选
    openai/text-embedding-3-small（原生 1536 维，且此前从不传 dimensions）——
    一旦设置 OPENAI_API_KEY，写入会被 Milvus 后端维度校验整体拒绝。
    """

    def test_repo_dimension_read_from_repo(self):
        """注入的 repo 声明 DIMENSION 时，provider 拿到该值。"""
        from core.storage.memory_repo import MemoryRepository
        ltm = LongTermMemory(repo=MemoryRepository())
        assert ltm._embedding_provider.expected_dimension == 1024  # noqa: SLF001

    def test_repo_dimension_none_when_repo_lacks_it(self):
        """repo 未声明 DIMENSION（纯内存/fake）→ None，不做维度限定。"""
        ltm = LongTermMemory(repo=FakeMemoryRepo())
        assert ltm._embedding_provider.expected_dimension is None  # noqa: SLF001

    def test_repo_dimension_none_without_repo(self):
        """未注入 repo → None（后续 connect() 时补对齐）。"""
        ltm = LongTermMemory()
        assert ltm._embedding_provider.expected_dimension is None  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_dimension_backfilled_on_connect_when_repo_lazy(self):
        """repo 缺失时 connect() 才创建 → 补一次维度对齐（embedding 尚未加载）。"""
        ltm = LongTermMemory()
        assert ltm._embedding_provider.expected_dimension is None  # noqa: SLF001

        with patch(
            "core.storage.memory_repo.MemoryRepository.connect",
            new=AsyncMock(return_value=False),
        ), patch.object(
            LongTermMemory, "_load_from_milvus", new=AsyncMock(return_value=0),
        ):
            await ltm.connect()

        assert ltm._embedding_provider.expected_dimension == 1024  # noqa: SLF001


@pytest.mark.unit
class TestEmbeddingProviderForwardsDimensions:
    """EmbeddingProvider 把 expected_dimension 透传给 OpenAI 兼容后端。

    注：core/memory/tests/conftest.py 的 autouse fixture 会整体替换
    `_try_load_sync`，故此处直接验证其构造参数的纯函数接缝
    `_openai_create_kwargs()`（_try_load_sync 内部即用它展开 kwargs）。
    """

    def test_dimensions_forwarded_when_expected_set(self):
        """声明期望维度 → 后端构造参数含 dimensions=1024。"""
        from core.memory.embedding_provider import EmbeddingProvider

        provider = EmbeddingProvider(embed_timeout=10.0, expected_dimension=1024)
        assert provider._openai_create_kwargs() == {"dimensions": 1024}  # noqa: SLF001

    def test_no_dimensions_when_expected_unset(self):
        """未声明期望维度 → 不传 dimensions（保持后端原生维度）。"""
        from core.memory.embedding_provider import EmbeddingProvider

        provider = EmbeddingProvider(embed_timeout=10.0)
        assert provider._openai_create_kwargs() == {}  # noqa: SLF001


# ---------------------------------------------------------------------------
# P2-4：max_age_days 有意不参与驱逐
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestMaxAgeDaysDeliberatelyInert:
    """固化 2026-07-28 设计决策：年龄驱逐已删除，仅数量上限生效。

    设计稿明确「删除年龄驱逐（cutoff + _max_age_days）全部逻辑；max_age_days 参数
    保留在构造函数和 config 中，但不参与驱逐逻辑」。若有人重新引入年龄淘汰，本用例
    会失败——届时须先过 spec（年龄淘汰会不可逆删除真实记忆）。
    """

    @staticmethod
    def _entry(mid: str, ts: float) -> MemoryEntry:
        return MemoryEntry(
            id=MemoryId(mid),
            text="t",
            memory_type=MemoryType("Note"),
            poignancy=5,
            timestamp=ts,
            user_id="usr_local_default",
            character_instance_id="chi_builtin_elysia",
        )

    @pytest.mark.asyncio
    async def test_old_entries_survive_when_under_count_limit(self):
        """远超 max_age_days 的旧条目，只要未超数量上限就保留。"""
        import time

        ltm = LongTermMemory(max_entries=100, max_age_days=1)
        ltm._entries.append(self._entry("mem_old", time.time() - 30 * 86400))  # noqa: SLF001

        removed = await ltm._evict_overflow()  # noqa: SLF001

        assert removed == 0
        assert ltm._get_by_id(MemoryId("mem_old")) is not None  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_count_limit_is_the_only_eviction_trigger(self):
        """驱逐唯一依据是数量：超限时淘汰最旧者，与是否「超龄」无关。"""
        import time

        ltm = LongTermMemory(max_entries=2, max_age_days=1)
        now = time.time()
        for mid, ts in [
            ("mem_newest", now),
            ("mem_older", now - 10 * 86400),
            ("mem_oldest", now - 30 * 86400),
        ]:
            ltm._entries.append(self._entry(mid, ts))  # noqa: SLF001

        removed = await ltm._evict_overflow()  # noqa: SLF001

        assert removed == 1
        assert ltm._get_by_id(MemoryId("mem_oldest")) is None  # noqa: SLF001
        assert ltm._get_by_id(MemoryId("mem_older")) is not None  # noqa: SLF001
        assert ltm._get_by_id(MemoryId("mem_newest")) is not None  # noqa: SLF001


# ══════════════════════════════════════════════════════════════════
# S3-10：更新文本时旧向量删不掉 → 必须记 tombstone
# ══════════════════════════════════════════════════════════════════

@pytest.mark.service
class TestUpdateTextTombstonesOldVector:
    """`update_text` 插入新向量后会删旧向量，但删除失败**只 `logger.debug`**——
    不记 tombstone、不落 journal。

    同仓另三条删除路径（`delete` / `mark_absorbed` / `_evict_overflow`）都记
    `_pending_deletes` 并持久化，**只有它漏了**。后果：旧向量带着**同一 entry_id**
    留在 Milvus → rerank 同时看到新旧两行，而 `_load_from_milvus` 按"先到者胜"去重
    → 重启后**旧文本静默复活**（记忆回滚）。
    """

    def _ltm(self, *, delete_ok: bool):
        from core.memory.long_term import LongTermMemory, MemoryEntry
        from core.schema.memory import MemoryType
        from core.schema.types import MemoryId

        ltm = LongTermMemory.__new__(LongTermMemory)
        ltm._repo = MagicMock()
        ltm._repo.insert = AsyncMock(return_value=[999])
        ltm._repo.delete = AsyncMock(return_value=(1 if delete_ok else 0))
        ltm._entries = [MemoryEntry(
            id=MemoryId("m1"), text="旧文本", memory_type=MemoryType.NOTE,
            poignancy=5, timestamp=0.0,
        )]
        ltm._entries[0]._milvus_pk = 111          # noqa: SLF001
        ltm._dirty_ids = set()
        ltm._pending_deletes = {}
        ltm._absorbed_ids = set()
        ltm._write_lock = asyncio.Lock()
        ltm._save_pending_deletes = MagicMock()
        ltm._save_pending_syncs = MagicMock()
        ltm._embedding_provider = MagicMock()
        ltm._embedding_provider.embed = AsyncMock(return_value=[0.1, 0.2])
        return ltm

    @pytest.mark.asyncio
    async def test_delete_failure_records_tombstone(self, caplog):
        """**核心**：删除返回 0（没删掉）→ 必须登记 tombstone + 持久化 + 可见告警。"""
        import logging

        ltm = self._ltm(delete_ok=False)
        with caplog.at_level(logging.WARNING):
            ok = await ltm.update_text("m1", "新文本")

        assert ok is True
        assert ltm._pending_deletes.get("m1") == 111, (      # noqa: SLF001
            "旧向量没删掉却没记账——它会在重启后把旧文本带回来")
        ltm._save_pending_deletes.assert_called()            # noqa: SLF001
        assert any(r.levelno >= logging.WARNING for r in caplog.records), \
            "删除失败必须在默认级别可见（原先是 logger.debug）"

    @pytest.mark.asyncio
    async def test_delete_exception_records_tombstone(self):
        """异常路径同样要记账（不只是返回值 < 1）。"""
        ltm = self._ltm(delete_ok=True)
        ltm._repo.delete = AsyncMock(side_effect=RuntimeError("milvus down"))

        await ltm.update_text("m1", "新文本")

        assert ltm._pending_deletes.get("m1") == 111        # noqa: SLF001
        ltm._save_pending_deletes.assert_called()           # noqa: SLF001

    @pytest.mark.asyncio
    async def test_successful_delete_records_nothing(self):
        """回归网：旧向量删干净 → 不记 tombstone（否则会误删已重建条目）。"""
        ltm = self._ltm(delete_ok=True)

        await ltm.update_text("m1", "新文本")

        assert ltm._pending_deletes == {}                   # noqa: SLF001
        ltm._save_pending_deletes.assert_not_called()       # noqa: SLF001

    @pytest.mark.asyncio
    async def test_all_four_delete_paths_record_tombstones(self):
        """**结构性守卫**：四条删除路径都必须在失败时记 `_pending_deletes`。

        逐路径断言而不是只测 `update_text`：这个缺陷的形状就是"三条有、一条没有"，
        只测被修的那条挡不住下一条被漏掉。
        """
        import ast
        import pathlib

        src = pathlib.Path("core/memory/long_term.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        # 收集每个函数里是否出现 _pending_deletes 赋值
        recorders: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.dump(node)
            if "_pending_deletes" in body:
                recorders.add(node.name)

        #: 可代为登记的 helper（调用它等于记账）。列出名字而不是只看直接赋值——
        #: 否则"把记账提取成 helper"这个**改进**反而会让守卫变红。
        recorder_helpers = {"_tombstone_old_pk"}

        def _records(fn: str) -> bool:
            node = next(
                (n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn),
                None)
            if node is None:
                return False
            body = ast.dump(node)
            if "_pending_deletes" in body:
                return True
            return any(h in body for h in recorder_helpers)

        for fn in ("delete", "mark_absorbed", "_evict_overflow", "update_text"):
            assert _records(fn), (
                f"{fn} 未登记 tombstone —— 同仓四条删除路径必须一致（S3-10 的形状是"
                f"「三条有、一条没有」）")
