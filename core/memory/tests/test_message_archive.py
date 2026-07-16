"""MessageArchive 单元测试 — mock MessageRepository。"""

import pytest
from unittest.mock import AsyncMock

from core.memory.message_archive import MessageArchive
from core.schema.message import UserMessage, MessageId
from core.schema.part import TextPart
from core.schema.types import SessionId


def _make_user_msg(content: str = "hello", msg_id: str = "msg-1", session_id: str = "sid") -> UserMessage:
    """创建最小 UserMessage 测试辅助函数。"""
    return UserMessage(
        content=[TextPart(content=content)],
        message_id=MessageId(msg_id),
        session_id=SessionId(session_id),
    )


@pytest.mark.service
class TestMessageArchiveInit:
    @pytest.mark.asyncio
    async def test_init_stores_repo(self):
        """__init__ 应保存 repo 引用。"""
        repo = AsyncMock()
        archive = MessageArchive(repo=repo)
        assert archive._repo is repo

    @pytest.mark.asyncio
    async def test_init_defaults_repo_none(self):
        """不带参数构造时 _repo 为 None。"""
        archive = MessageArchive()
        assert archive._repo is None


@pytest.mark.service
class TestMessageArchiveAppend:
    @pytest.mark.asyncio
    async def test_append_calls_repo(self):
        """append 将 dict 转为 Message 后调用 repo.append。"""
        repo = AsyncMock()
        repo.append = AsyncMock()
        archive = MessageArchive(repo=repo)
        msg = {
            "role": "user",
            "content": [{"type": "text", "content": "hello"}],
            "session_id": "sid",
            "message_id": "msg-1",
        }
        result = await archive.append(msg)
        # 验证 repo.append 被调用，且参数是 Message 对象
        repo.append.assert_called_once()
        call_args = repo.append.call_args
        # session_id 参数
        assert call_args[0][0] == "sid"  # SessionId is str at runtime
        # msg 参数应为 Message (UserMessage)
        assert isinstance(call_args[0][1], UserMessage)
        assert result == ""  # msg 没有 _id

    @pytest.mark.asyncio
    async def test_append_raises_when_not_connected(self):
        archive = MessageArchive()
        with pytest.raises(RuntimeError, match="not connected"):
            await archive.append({"role": "user"})


@pytest.mark.service
class TestMessageArchiveQuery:
    @pytest.mark.asyncio
    async def test_query_returns_messages_and_has_more(self):
        repo = AsyncMock()
        msgs = []
        for i in range(51):
            msg = _make_user_msg(content=f"msg{i}", msg_id=f"{i:024x}")
            msg.metadata["_mongo_id"] = f"{i:024x}"
            msgs.append(msg)
        repo.query = AsyncMock(return_value=(msgs[:50], True))
        archive = MessageArchive(repo=repo)
        messages, has_more = await archive.query(limit=50)
        assert len(messages) == 50
        assert has_more is True
        # 验证 _mongo_id 被提升为顶层 _id
        assert messages[0].get("_id") == f"{0:024x}"
        repo.query.assert_called_once_with(before=None, limit=50)

    @pytest.mark.asyncio
    async def test_query_no_more_pages(self):
        repo = AsyncMock()
        msg = _make_user_msg(msg_id="1")
        msg.metadata["_mongo_id"] = "1"
        repo.query = AsyncMock(return_value=([msg], False))
        archive = MessageArchive(repo=repo)
        messages, has_more = await archive.query(limit=50)
        assert has_more is False
        assert messages[0].get("_id") == "1"
        repo.query.assert_called_once_with(before=None, limit=50)

    @pytest.mark.asyncio
    async def test_query_pagination_cursor(self):
        repo = AsyncMock()
        repo.query = AsyncMock(return_value=([], False))
        archive = MessageArchive(repo=repo)
        await archive.query(before="abc123", limit=30)
        repo.query.assert_called_once_with(before="abc123", limit=30)

    @pytest.mark.asyncio
    async def test_query_returns_empty_when_not_connected(self):
        archive = MessageArchive()
        messages, has_more = await archive.query(limit=50)
        assert messages == []
        assert has_more is False


@pytest.mark.service
class TestMessageArchiveCount:
    @pytest.mark.asyncio
    async def test_count_returns_zero_when_empty(self):
        repo = AsyncMock()
        repo.count = AsyncMock(return_value=0)
        archive = MessageArchive(repo=repo)
        c = await archive.count()
        assert c == 0

    @pytest.mark.asyncio
    async def test_count_returns_total(self):
        repo = AsyncMock()
        repo.count = AsyncMock(return_value=42)
        archive = MessageArchive(repo=repo)
        c = await archive.count()
        assert c == 42

    @pytest.mark.asyncio
    async def test_count_returns_zero_when_not_connected(self):
        archive = MessageArchive()
        c = await archive.count()
        assert c == 0


@pytest.mark.service
class TestMessageArchiveClearAll:
    @pytest.mark.asyncio
    async def test_clear_all_deletes_and_returns_count(self):
        repo = AsyncMock()
        repo.clear_all = AsyncMock(return_value=10)
        archive = MessageArchive(repo=repo)
        deleted = await archive.clear_all()
        assert deleted == 10
        repo.clear_all.assert_called_once()

    @pytest.mark.asyncio
    async def test_clear_all_returns_zero_when_not_connected(self):
        archive = MessageArchive()
        deleted = await archive.clear_all()
        assert deleted == 0
