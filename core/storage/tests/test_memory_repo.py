"""MemoryRepository 单元测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from core.storage.memory_repo import MemoryRepository


@pytest.mark.unit
class TestMemoryRepository:
    def test_insert_calls_db_insert(self):
        import asyncio
        repo = MemoryRepository(db=MagicMock())
        repo._db.insert = AsyncMock(return_value=[1, 2])  # type: ignore[reportOptionalMemberAccess]
        result = asyncio.run(repo.insert([[0.1]*1024], [{"text": "test"}]))
        assert result == [1, 2]
        repo._db.insert.assert_called_once()  # type: ignore[reportOptionalMemberAccess]

    def test_search_returns_empty_when_db_none(self):
        import asyncio
        repo = MemoryRepository(db=None)
        result = asyncio.run(repo.search([0.1]*1024, top_k=5))
        assert result == []

    def test_delete_returns_zero_when_db_none(self):
        import asyncio
        repo = MemoryRepository(db=None)
        result = asyncio.run(repo.delete([1, 2]))
        assert result == 0
