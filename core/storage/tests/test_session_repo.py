"""SessionRepository 单元测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.storage.session_repo import SessionRepository


@pytest.mark.unit
class TestSessionRepository:
    def test_save_calls_update_one(self):
        import asyncio
        repo = SessionRepository(db=MagicMock())
        repo._db.update_one = AsyncMock()
        asyncio.run(repo.save("sid", [], {"mood": "neutral"}))
        repo._db.update_one.assert_called_once()

    def test_returns_empty_list_when_db_none(self):
        import asyncio
        repo = SessionRepository(db=None)
        result = asyncio.run(repo.list_all())
        assert result == []

    def test_load_returns_none_when_db_none(self):
        import asyncio
        repo = SessionRepository(db=None)
        result = asyncio.run(repo.load("sid"))
        assert result is None

    def test_delete_returns_zero_when_db_none(self):
        import asyncio
        repo = SessionRepository(db=None)
        result = asyncio.run(repo.delete("sid"))
        assert result == 0
