"""MessageRepository 单元测试。"""
import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock
from core.storage.message_repo import MessageRepository


@pytest.mark.unit
class TestMessageRepository:
    def test_append_calls_insert_one(self):
        repo = MessageRepository(db=MagicMock())
        repo._db.insert_one = AsyncMock()
        # fire-and-forget 在 append 中是 asyncio.create_task，这里直接调
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

    def test_query_with_before(self):
        """基于游标的分页查询：before 游标 + has_more 检测。"""
        from bson import ObjectId

        repo = MessageRepository(db=MagicMock())
        now = ObjectId()
        earlier = ObjectId()

        fake_messages: list[dict] = [
            {"_id": now, "role": "user", "content": "latest"},
            {"_id": ObjectId(), "role": "assistant", "content": "middle"},
        ]

        repo._db.find = AsyncMock(return_value=fake_messages)

        # 请求 limit=1，返回 2 条（内部请求 limit+1），has_more=True
        messages, has_more = asyncio.run(repo.query(before=None, limit=1))
        assert len(messages) == 1
        assert has_more is True
        assert messages[0]["_id"] == now

        # before 游标过滤
        repo._db.find = AsyncMock(return_value=[{"_id": earlier, "role": "user", "content": "older"}])
        messages, has_more = asyncio.run(repo.query(before=str(now), limit=20))
        assert has_more is False

        # 验证 filter 包含 $lt 条件
        call_args = repo._db.find.call_args
        assert call_args[0][1]["_id"] == {"$lt": now}

    def test_query_returns_empty_when_db_none(self):
        repo = MessageRepository(db=None)
        messages, has_more = asyncio.run(repo.query(limit=20))
        assert messages == []
        assert has_more is False
