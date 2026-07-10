"""MessageArchive 单元测试 — mock MongoDB backend。"""

import pytest
from unittest.mock import AsyncMock

from core.memory.message_archive import MessageArchive


class TestMessageArchiveConnect:
    @pytest.mark.asyncio
    async def test_connect_stores_db(self):
        """connect 应保存 db 引用并返回 True。_id 字段使用 MongoDB 内置索引。"""
        db = AsyncMock()
        archive = MessageArchive()
        result = await archive.connect(db)
        assert result is True
        assert archive._db is db


class TestMessageArchiveAppend:
    @pytest.mark.asyncio
    async def test_append_inserts_and_returns_id(self):
        db = AsyncMock()
        db.insert_one = AsyncMock(return_value="abc123")
        archive = MessageArchive()
        await archive.connect(db)
        msg = {"role": "user", "content": [{"type": "text", "content": "hello"}]}
        _id = await archive.append(msg)
        assert _id == "abc123"
        db.insert_one.assert_called_once()

    @pytest.mark.asyncio
    async def test_append_raises_when_not_connected(self):
        archive = MessageArchive()
        with pytest.raises(RuntimeError, match="not connected"):
            await archive.append({"role": "user"})


class TestMessageArchiveQuery:
    @pytest.mark.asyncio
    async def test_query_returns_messages_and_has_more(self):
        db = AsyncMock()
        msgs = [{"_id": str(i), "role": "user", "text": f"msg{i}"} for i in range(51)]
        db.find = AsyncMock(return_value=msgs[:51])  # limit + 1 → 51
        archive = MessageArchive()
        await archive.connect(db)
        messages, has_more = await archive.query(limit=50)
        assert len(messages) == 50
        assert has_more is True

    @pytest.mark.asyncio
    async def test_query_no_more_pages(self):
        db = AsyncMock()
        db.find = AsyncMock(return_value=[{"_id": "1", "role": "user"}])
        archive = MessageArchive()
        await archive.connect(db)
        messages, has_more = await archive.query(limit=50)
        assert has_more is False

    @pytest.mark.asyncio
    async def test_query_with_before_cursor(self):
        db = AsyncMock()
        db.find = AsyncMock(return_value=[])
        archive = MessageArchive()
        await archive.connect(db)
        from bson import ObjectId
        valid_oid = str(ObjectId())
        await archive.query(before=valid_oid, limit=30)
        # 验证 filter 包含 _id < valid_oid
        call_args = db.find.call_args
        assert "_id" in call_args[0][1]
        assert call_args[1]["limit"] == 31  # limit + 1 用于判断 has_more

    @pytest.mark.asyncio
    async def test_query_returns_empty_when_not_connected(self):
        archive = MessageArchive()
        messages, has_more = await archive.query(limit=50)
        assert messages == []
        assert has_more is False


class TestMessageArchiveCount:
    @pytest.mark.asyncio
    async def test_count_returns_zero_when_empty(self):
        db = AsyncMock()
        db.count = AsyncMock(return_value=0)
        archive = MessageArchive()
        await archive.connect(db)
        c = await archive.count()
        assert c == 0

    @pytest.mark.asyncio
    async def test_count_returns_total(self):
        db = AsyncMock()
        db.count = AsyncMock(return_value=42)
        archive = MessageArchive()
        await archive.connect(db)
        c = await archive.count()
        assert c == 42

    @pytest.mark.asyncio
    async def test_count_returns_zero_when_not_connected(self):
        archive = MessageArchive()
        c = await archive.count()
        assert c == 0


class TestMessageArchiveClearAll:
    @pytest.mark.asyncio
    async def test_clear_all_deletes_and_returns_count(self):
        db = AsyncMock()
        db.delete_many = AsyncMock(return_value=10)
        archive = MessageArchive()
        await archive.connect(db)
        deleted = await archive.clear_all()
        assert deleted == 10
        db.delete_many.assert_called_once_with("message_archive", {})

    @pytest.mark.asyncio
    async def test_clear_all_returns_zero_when_not_connected(self):
        archive = MessageArchive()
        deleted = await archive.clear_all()
        assert deleted == 0
