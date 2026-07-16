"""MessageRepository 单元测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from core.storage.message_repo import MessageRepository


@pytest.mark.unit
class TestMessageRepository:
    def test_append_calls_insert_one(self):
        repo = MessageRepository(db=MagicMock())
        repo._db.insert_one = AsyncMock()
        # fire-and-forget 在 append 中是 asyncio.create_task，这里直接调
        import asyncio
        asyncio.run(repo.append("sid", {"role": "user", "content": "hi"}))
        repo._db.insert_one.assert_called_once()

    def test_returns_empty_when_db_none(self):
        repo = MessageRepository(db=None)
        msgs, total = asyncio.run(repo.query_by_session("sid"))
        assert msgs == []
        assert total == 0

    def test_count_returns_zero_when_db_none(self):
        repo = MessageRepository(db=None)
        assert asyncio.run(repo.count()) == 0

    def test_clear_all_calls_delete_many(self):
        repo = MessageRepository(db=MagicMock())
        repo._db.delete_many = AsyncMock(return_value=5)
        result = asyncio.run(repo.clear_all())
        assert result == 5

import asyncio
