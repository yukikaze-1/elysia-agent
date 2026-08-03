"""MessageRepository 单元测试。"""
import asyncio
import logging

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.storage.message_repo import MessageRepository
from core.schema.message import UserMessage, AssistantMessage, MessageId
from core.schema.part import TextPart
from core.schema.types import SessionId


def _make_user_msg(content: str = "hi", msg_id: str = "msg-1", session_id: str = "sid") -> UserMessage:
    """创建最小 UserMessage 测试辅助函数。"""
    return UserMessage(
        content=[TextPart(content=content)],
        message_id=MessageId(msg_id),
        session_id=SessionId(session_id),
    )


def _make_user_doc(content: str = "hi", msg_id: str = "msg-1", session_id: str = "sid", **extra) -> dict:
    """创建 UserMessage 形状的 MongoDB 文档（模拟 find 返回）。"""
    doc = {
        "role": "user",
        "content": [{"type": "text", "content": content}],
        "message_id": msg_id,
        "session_id": session_id,
        "timestamp": "2024-01-01T00:00:00Z",
    }
    doc.update(extra)
    return doc


def _make_assistant_doc(content: str = "hello", msg_id: str = "msg-2", **extra) -> dict:
    """创建 AssistantMessage 形状的 MongoDB 文档。"""
    doc = {
        "role": "assistant",
        "content": [{"type": "text", "content": content}],
        "message_id": msg_id,
        "timestamp": "2024-01-01T00:00:00Z",
    }
    doc.update(extra)
    return doc


@pytest.mark.unit
class TestMessageRepository:
    def test_append_calls_insert_one(self):
        repo = MessageRepository(db=MagicMock())
        repo._db.insert_one = AsyncMock()  # type: ignore[reportOptionalMemberAccess]
        msg = _make_user_msg()
        asyncio.run(repo.append(SessionId("sid"), msg))
        repo._db.insert_one.assert_called_once()  # type: ignore[reportOptionalMemberAccess]
        call_args = repo._db.insert_one.call_args  # type: ignore[reportOptionalMemberAccess]
        assert call_args[0][0] == "message_archive"  # collection name
        assert call_args[0][1]["session_id"] == "sid"

    def test_returns_empty_when_db_none(self):
        repo = MessageRepository(db=None)
        msgs, total = asyncio.run(repo.query_by_session(SessionId("sid")))
        assert msgs == []
        assert total == 0

    def test_count_returns_zero_when_db_none(self):
        repo = MessageRepository(db=None)
        assert asyncio.run(repo.count()) == 0

    def test_clear_all_calls_delete_many(self):
        repo = MessageRepository(db=MagicMock())
        repo._db.delete_many = AsyncMock(return_value=5)  # type: ignore[reportOptionalMemberAccess]
        result = asyncio.run(repo.clear_all())
        assert result == 5

    def test_query_with_before(self):
        """基于游标的分页查询：before 游标 + has_more 检测。"""
        from bson import ObjectId

        repo = MessageRepository(db=MagicMock())
        now = ObjectId()
        earlier = ObjectId()

        fake_docs: list[dict] = [
            _make_user_doc(content="latest", msg_id="msg-1", _id=now),
            _make_assistant_doc(content="middle", msg_id="msg-2", _id=ObjectId()),
        ]

        repo._db.find = AsyncMock(return_value=fake_docs)  # type: ignore[reportOptionalMemberAccess]

        # 请求 limit=1，返回 2 条（内部请求 limit+1），has_more=True
        messages, has_more = asyncio.run(repo.query(before=None, limit=1))
        assert len(messages) == 1
        assert has_more is True
        # _id 保存在 metadata._mongo_id 中
        assert messages[0].metadata["_mongo_id"] == str(now)

        # before 游标过滤
        repo._db.find = AsyncMock(return_value=[  # type: ignore[reportOptionalMemberAccess]
            _make_user_doc(content="older", msg_id="msg-3", _id=earlier),
        ])
        messages, has_more = asyncio.run(repo.query(before=str(now), limit=20))
        assert has_more is False

        # 验证 filter 包含 $lt 条件
        call_args = repo._db.find.call_args  # type: ignore[reportOptionalMemberAccess]
        assert call_args[0][1]["_id"] == {"$lt": now}

    def test_query_returns_empty_when_db_none(self):
        repo = MessageRepository(db=None)
        messages, has_more = asyncio.run(repo.query(limit=20))
        assert messages == []
        assert has_more is False

    def test_append_logs_warning_when_db_none(self, caplog):
        """归档在 _db is None 时不能静默——必须留下可见日志（S1）。"""
        repo = MessageRepository(db=None)
        with caplog.at_level(logging.WARNING, logger="core.storage.message_repo"):
            asyncio.run(repo.append(SessionId("sid"), _make_user_msg()))
        assert any("message_archive" in r.message for r in caplog.records)

    def test_health_check_returns_true_when_connected(self):
        repo = MessageRepository(db=MagicMock())
        assert asyncio.run(repo.health_check()) is True

    def test_health_check_reconnects_when_db_none(self, monkeypatch):
        """启动期 MongoDB 故障不应让归档永久失效——health_check 补重连（S1）。"""
        from Module.database.documentdb import DocumentDBFactory

        fake_backend = MagicMock()
        fake_backend.ensure_index = AsyncMock()
        monkeypatch.setattr(DocumentDBFactory, "create", lambda *a, **kw: fake_backend)

        repo = MessageRepository(db=None)
        ok = asyncio.run(repo.health_check())
        assert ok is True
        assert repo._db is fake_backend
        # 重连后重建两个索引（session_id / session_id+_id）
        assert fake_backend.ensure_index.call_count == 2
