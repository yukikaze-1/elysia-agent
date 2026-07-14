"""LongTermMemory: connect 日志级别 + health_check 重连 测试。"""
import logging
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from core.memory.long_term import LongTermMemory


@pytest.mark.service
class TestLongTermMemoryConnectLogLevel:
    """connect() 失败时必须使用 logger.warning。"""

    @pytest.mark.asyncio
    async def test_connect_failure_uses_warning(self, caplog):
        ltm = LongTermMemory(max_entries=10)
        with patch(
            "Module.database.vectordb.VectorDBFactory.create",
            side_effect=RuntimeError("connection refused"),
        ):
            with caplog.at_level(logging.WARNING):
                result = await ltm.connect()

        assert result is False
        assert ltm._connected is False
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) >= 1
        assert "Milvus unavailable" in warnings[0].message


@pytest.mark.service
class TestLongTermMemoryHealthCheck:
    """health_check() 在连接丢失后应尝试重连。"""

    @pytest.mark.asyncio
    async def test_returns_true_when_already_connected(self):
        ltm = LongTermMemory(max_entries=10)
        ltm._connected = True
        result = await ltm.health_check()
        assert result is True

    @pytest.mark.asyncio
    async def test_reconnects_when_disconnected(self):
        ltm = LongTermMemory(max_entries=10)
        ltm._connected = False
        mock_db = MagicMock()
        mock_db.collection_exists = AsyncMock(return_value=True)
        with patch(
            "Module.database.vectordb.VectorDBFactory.create",
            return_value=mock_db,
        ):
            result = await ltm.health_check()

        assert result is True
        assert ltm._connected is True
        assert ltm._vector_db is mock_db

    @pytest.mark.asyncio
    async def test_returns_false_when_reconnect_fails(self):
        ltm = LongTermMemory(max_entries=10)
        ltm._connected = False
        with patch(
            "Module.database.vectordb.VectorDBFactory.create",
            side_effect=RuntimeError("still down"),
        ):
            result = await ltm.health_check()

        assert result is False
        assert ltm._connected is False


@pytest.mark.service
class TestPKMatching:
    """PK 匹配: 同文异构条目各自获得正确 distance。"""

    @pytest.mark.asyncio
    async def test_only_matching_pk_appears(self):
        """Milvus 只返回一个 PK 时，仅该 PK 对应条目出现（旧代码 text 匹配会两个都出现）。"""
        from core.memory.long_term import MemoryEntry

        ltm = LongTermMemory(max_entries=10)
        ltm._connected = True

        # 两个同文异构条目
        e1 = MemoryEntry(
            id="id-1", text="我喜欢吃披萨", memory_type="note",
            poignancy=5, timestamp=1000.0, _milvus_pk=101,
        )
        e2 = MemoryEntry(
            id="id-2", text="我喜欢吃披萨", memory_type="note",
            poignancy=8, timestamp=2000.0, _milvus_pk=102,
        )
        ltm._entries = [e1, e2]

        # Milvus 只返回 PK 101，不返回 102
        mock_db = MagicMock()
        mock_db.search = AsyncMock(return_value=[
            {"id": 101, "text": "我喜欢吃披萨", "distance": 0.3},
        ])
        ltm._vector_db = mock_db

        results = await ltm.search(query_embedding=[0.1] * 896, top_k=5)

        # 只有 e1 应该出现（PK 匹配），e2 不应出现（其 PK 不在 Milvus 结果中）
        ids_in_results = {e.id for e in results}
        assert "id-1" in ids_in_results
        assert "id-2" not in ids_in_results, (
            "e2 的 PK 102 不在 Milvus 结果中，不应被匹配到"
        )

    @pytest.mark.asyncio
    async def test_both_pks_in_results_both_appear(self):
        """两个不同 PK 的条目都在 Milvus 结果中时，两个都应出现且各自正确 distance。"""
        from core.memory.long_term import MemoryEntry

        ltm = LongTermMemory(max_entries=10)
        ltm._connected = True

        e1 = MemoryEntry(
            id="id-1", text="同文", memory_type="note",
            poignancy=5, timestamp=1000.0, _milvus_pk=101,
        )
        e2 = MemoryEntry(
            id="id-2", text="同文", memory_type="note",
            poignancy=5, timestamp=1000.0, _milvus_pk=102,
        )
        ltm._entries = [e1, e2]

        mock_db = MagicMock()
        mock_db.search = AsyncMock(return_value=[
            {"id": 101, "text": "同文", "distance": 0.3},
            {"id": 102, "text": "同文", "distance": 0.7},
        ])
        ltm._vector_db = mock_db

        results = await ltm.search(query_embedding=[0.1] * 896, top_k=5)

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
    async def test_text_fallback_when_no_pk(self):
        """_milvus_pk=None 的历史条目仍能通过 text fallback 匹配。"""
        from core.memory.long_term import MemoryEntry

        ltm = LongTermMemory(max_entries=10)
        ltm._connected = True

        e1 = MemoryEntry(
            id="old-1", text="历史记忆", memory_type="note",
            poignancy=5, timestamp=1000.0, _milvus_pk=None,  # 无 PK
        )
        ltm._entries = [e1]

        mock_db = MagicMock()
        # Milvus 返回无 id 字段的结果（模拟旧数据）
        mock_db.search = AsyncMock(return_value=[
            {"text": "历史记忆", "distance": 0.5},
        ])
        ltm._vector_db = mock_db

        results = await ltm.search(query_embedding=[0.1] * 896, top_k=5)

        assert len(results) == 1
        assert results[0].id == "old-1"

    @pytest.mark.asyncio
    async def test_no_id_field_in_raw_falls_back_to_text(self):
        """Milvus 返回结果中无 id 字段时，走 text fallback 不崩溃。"""
        from core.memory.long_term import MemoryEntry

        ltm = LongTermMemory(max_entries=10)
        ltm._connected = True

        e1 = MemoryEntry(
            id="e1", text="无ID字段测试", memory_type="note",
            poignancy=5, timestamp=1000.0, _milvus_pk=None,
        )
        ltm._entries = [e1]

        mock_db = MagicMock()
        # raw 中没有 "id" 字段
        mock_db.search = AsyncMock(return_value=[
            {"text": "无ID字段测试", "distance": 0.2},
        ])
        ltm._vector_db = mock_db

        results = await ltm.search(query_embedding=[0.1] * 896, top_k=5)

        # 不应崩溃，应通过 text fallback 找到
        assert len(results) == 1
        assert results[0].id == "e1"


@pytest.mark.service
class TestLongTermMemoryClearAll:
    """clear_all() 可独立调用（无需先 connect()），失败时优雅降级。"""

    @pytest.mark.asyncio
    async def test_clear_all_without_connect_creates_temp_backend(self):
        """无 _vector_db 时，clear_all() 自行创建临时连接完成清理。"""
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
        mock_create.assert_called_once_with("milvus/memory")
        mock_backend.collection_exists.assert_awaited_once_with(LongTermMemory.COLLECTION)
        mock_backend.drop_collection.assert_awaited_once_with(LongTermMemory.COLLECTION)
        mock_backend.create_collection.assert_awaited_once_with(
            LongTermMemory.COLLECTION, dimension=LongTermMemory.DIMENSION)
        mock_backend.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clear_all_with_existing_backend_reuses_it(self):
        """_vector_db 已设置时，clear_all() 复用它，不创建新连接。"""
        ltm = LongTermMemory(max_entries=10)
        ltm._entries = [MagicMock()]
        mock_backend = AsyncMock()
        mock_backend.drop_collection = AsyncMock()
        mock_backend.create_collection = AsyncMock()
        ltm._vector_db = mock_backend
        ltm._connected = True

        with patch(
            "Module.database.vectordb.VectorDBFactory.create",
        ) as mock_create:
            deleted = await ltm.clear_all()

        assert deleted == 1
        mock_create.assert_not_called()
        mock_backend.drop_collection.assert_awaited_once_with(LongTermMemory.COLLECTION)
        mock_backend.create_collection.assert_awaited_once_with(
            LongTermMemory.COLLECTION, dimension=LongTermMemory.DIMENSION)

    @pytest.mark.asyncio
    async def test_clear_all_without_connect_collection_not_exists(self):
        """临时路径：collection 不存在时跳过 drop，直接 create。"""
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
        mock_backend.drop_collection.assert_not_called()
        mock_backend.create_collection.assert_awaited_once_with(
            LongTermMemory.COLLECTION, dimension=LongTermMemory.DIMENSION)
        mock_backend.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clear_all_propagates_collection_exception(self):
        """临时路径：collection_exists 失败时异常传播，close() 在 finally 中调用。"""
        ltm = LongTermMemory(max_entries=10)

        mock_backend = AsyncMock()
        mock_backend.collection_exists = AsyncMock(side_effect=RuntimeError("connection refused"))
        mock_backend.close = AsyncMock()

        with patch(
            "Module.database.vectordb.VectorDBFactory.create",
            return_value=mock_backend,
        ):
            with pytest.raises(RuntimeError, match="connection refused"):
                await ltm.clear_all()

        # 即使失败，close() 也在 finally 中被调用
        mock_backend.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clear_all_factory_create_fails_returns_count(self):
        """Factory.create 失败时静默降级，返回已清空的内存条目数（向后兼容）。"""
        ltm = LongTermMemory(max_entries=10)
        ltm._entries = [MagicMock(), MagicMock()]  # 2 entries

        with patch(
            "Module.database.vectordb.VectorDBFactory.create",
            side_effect=RuntimeError("Milvus unavailable"),
        ):
            deleted = await ltm.clear_all()

        assert deleted == 2
        assert len(ltm._entries) == 0
