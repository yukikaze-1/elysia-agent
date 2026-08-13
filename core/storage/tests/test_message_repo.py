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

    def test_append_rejects_empty_message_id(self):
        """空 message_id → ValueError（守卫在 db-None 分支之前，与存储可用性无关）。

        D4 守卫的真实对象是 UserMessage/AssistantMessage 显式 MessageId("")——
        ToolResultMessage 已被 Task 2 模型校验器拒绝，构造即抛，到不了 append。
        """
        repo = MessageRepository(db=MagicMock())
        msg = UserMessage(
            content=[TextPart(content="hi")],
            message_id=MessageId(""),
            session_id=SessionId("sid"),
        )
        with pytest.raises(ValueError):
            asyncio.run(repo.append(SessionId("sid"), msg))

    def test_append_guard_before_db_none_branch(self):
        """Mongo 离线（db=None）且空 ID → 仍抛 ValueError，不被「DB 不可用」warning 掩盖。"""
        repo = MessageRepository(db=None)
        msg = UserMessage(
            content=[TextPart(content="hi")],
            message_id=MessageId(""),
            session_id=SessionId("sid"),
        )
        with pytest.raises(ValueError):
            asyncio.run(repo.append(SessionId("sid"), msg))

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
        from unittest.mock import AsyncMock

        fake = MagicMock()
        fake.ping = AsyncMock(return_value=True)  # B1/P1-05：真实 ping
        repo = MessageRepository(db=fake)
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


def _archive_doc(role: str, content: object, msg_id: str, timestamp: str,
                 session_id: str = "sid", **extra) -> dict:
    """构造 message_archive 文档（content 为 str 时转成 text part）。"""
    if isinstance(content, str):
        content = [{"type": "text", "content": content}]
    doc = {
        "role": role,
        "content": content,
        "message_id": msg_id,
        "timestamp": timestamp,
        "session_id": session_id,
    }
    doc.update(extra)
    return doc


def _seed(fake, docs) -> None:
    """批量灌入 message_archive 集合。"""
    for d in docs:
        asyncio.run(fake.insert_one("message_archive", d))


@pytest.mark.unit
class TestMessageRepositoryBatch3:
    """批次 3：query_page / query_after / find_context / query_search。"""

    def test_query_page_limit_probe_has_more(self):
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
        fake = FakeDocumentDBBackend()
        repo = MessageRepository(db=fake)
        docs = [_archive_doc("user", f"m{i}", f"msg-{i}",
                             f"2026-08-01T10:{i:02d}:00+00:00") for i in range(25)]
        _seed(fake, docs)
        messages, has_more = asyncio.run(repo.query_page("sid", limit=20))
        assert len(messages) == 20
        assert has_more is True
        # _id DESC：最新一条在最前（插序 m24 最新）
        assert messages[0].message_id == "msg-24"
        # 下一页用 before 游标
        before = messages[-1].metadata["_mongo_id"]
        page2, has_more2 = asyncio.run(repo.query_page("sid", before=before, limit=20))
        assert len(page2) == 5
        assert has_more2 is False

    def test_query_page_exact_limit_no_more(self):
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
        fake = FakeDocumentDBBackend()
        repo = MessageRepository(db=fake)
        _seed(fake, [_archive_doc("user", f"m{i}", f"msg-{i}", f"2026-08-01T10:{i:02d}:00+00:00") for i in range(20)])
        messages, has_more = asyncio.run(repo.query_page("sid", limit=20))
        assert len(messages) == 20
        assert has_more is False

    def test_query_page_returns_empty_when_db_none(self):
        repo = MessageRepository(db=None)
        messages, has_more = asyncio.run(repo.query_page("sid"))
        assert messages == []
        assert has_more is False

    def test_query_after_anchor_ascending(self):
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
        fake = FakeDocumentDBBackend()
        repo = MessageRepository(db=fake)
        _seed(fake, [_archive_doc("user", f"m{i}", f"msg-{i}", f"2026-08-01T10:{i:02d}:00+00:00") for i in range(5)])
        # 9A-2：锚点传 ObjectId（Mongo _id），过滤器 _id > oid
        anchor = next(d for d in fake.collections["message_archive"] if d["message_id"] == "msg-1")
        messages, has_more = asyncio.run(repo.query_after("sid", anchor["_id"]))
        assert [m.message_id for m in messages] == ["msg-2", "msg-3", "msg-4"]
        assert has_more is False

    def test_query_after_limit_probe_has_more(self):
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
        fake = FakeDocumentDBBackend()
        repo = MessageRepository(db=fake)
        _seed(fake, [_archive_doc("user", f"m{i}", f"msg-{i}", f"2026-08-01T10:{i:02d}:00+00:00") for i in range(8)])
        anchor = next(d for d in fake.collections["message_archive"] if d["message_id"] == "msg-0")
        messages, has_more = asyncio.run(repo.query_after("sid", anchor["_id"], limit=3))
        assert [m.message_id for m in messages] == ["msg-1", "msg-2", "msg-3"]
        assert has_more is True

    def test_query_after_oid_anchor_pages(self):
        """9A-2：query_after 锚点改 _id——正序分页，两页无重无漏；锚点为会话最新 → 空页 + False。"""
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
        fake = FakeDocumentDBBackend()
        repo = MessageRepository(db=fake)
        _seed(fake, [_archive_doc("user", f"m{i}", f"msg-{i}", f"2026-08-01T10:{i:02d}:00+00:00") for i in range(6)])
        docs = fake.collections["message_archive"]
        # 第一页：after=ObjectId(msg-2._id)，limit=2 → msg-3, msg-4（正序）
        page1, has_more1 = asyncio.run(repo.query_after(
            "sid", next(d for d in docs if d["message_id"] == "msg-2")["_id"], limit=2))
        assert [m.message_id for m in page1] == ["msg-3", "msg-4"]
        assert has_more1 is True
        # 第二页：after=ObjectId(msg-4._id) → msg-5，两页无重无漏
        page2, has_more2 = asyncio.run(repo.query_after(
            "sid", next(d for d in docs if d["message_id"] == "msg-4")["_id"], limit=2))
        assert [m.message_id for m in page2] == ["msg-5"]
        assert has_more2 is False
        # 无更近消息（锚点已是会话最新，或所有更近消息已删）→ ([], False)，不抛
        page3, has_more3 = asyncio.run(repo.query_after(
            "sid", next(d for d in docs if d["message_id"] == "msg-5")["_id"], limit=2))
        assert page3 == []
        assert has_more3 is False

    def test_query_after_missing_anchor(self):
        """Q8：锚点改 ObjectId 后无 message_id 查找特判——「无更近消息」→ 空页 + False。"""
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
        fake = FakeDocumentDBBackend()
        repo = MessageRepository(db=fake)
        _seed(fake, [_archive_doc("user", "x", "msg-0", "2026-08-01T10:00:00+00:00")])
        # 锚点取最新一条 _id（会话最新）→ 无更近消息 → 空页 + False
        latest = fake.collections["message_archive"][-1]["_id"]
        messages, has_more = asyncio.run(repo.query_after("sid", latest))
        assert messages == []
        assert has_more is False

    def test_find_context_window(self):
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
        fake = FakeDocumentDBBackend()
        repo = MessageRepository(db=fake)
        _seed(fake, [_archive_doc("user", f"m{i}", f"msg-{i}", f"2026-08-01T10:{i:02d}:00+00:00") for i in range(7)])
        target, pre, post = asyncio.run(repo.find_context("sid", "msg-3", before=2, after=2))
        assert target is not None and target.message_id == "msg-3"
        assert [m.message_id for m in pre] == ["msg-1", "msg-2"]  # 锚点前，正序
        assert [m.message_id for m in post] == ["msg-4", "msg-5"]  # 锚点后，正序

    def test_find_context_missing_anchor(self):
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
        fake = FakeDocumentDBBackend()
        repo = MessageRepository(db=fake)
        _seed(fake, [_archive_doc("user", "x", "msg-0", "2026-08-01T10:00:00+00:00")])
        target, pre, post = asyncio.run(repo.find_context("sid", "nope"))
        assert target is None
        assert pre == [] and post == []

    def test_find_context_edge_short_history(self):
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
        fake = FakeDocumentDBBackend()
        repo = MessageRepository(db=fake)
        _seed(fake, [_archive_doc("user", f"m{i}", f"msg-{i}", f"2026-08-01T10:{i:02d}:00+00:00") for i in range(2)])
        target, pre, post = asyncio.run(repo.find_context("sid", "msg-0", before=5, after=5))
        assert target.message_id == "msg-0"
        assert pre == []
        assert [m.message_id for m in post] == ["msg-1"]

    def test_query_search_text_case_insensitive(self):
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
        fake = FakeDocumentDBBackend()
        repo = MessageRepository(db=fake)
        _seed(fake, [
            _archive_doc("user", "我喜欢吃 pizza", "msg-0", "2026-08-01T10:00:00+00:00"),
            _archive_doc("assistant", "pizza 很好吃", "msg-1", "2026-08-01T10:01:00+00:00"),
            _archive_doc("user", "今天天气不错", "msg-2", "2026-08-01T10:02:00+00:00"),
        ])
        messages, has_more, next_cursor = asyncio.run(
            repo.query_search(query="PIZZA"))
        assert {m.message_id for m in messages} == {"msg-0", "msg-1"}
        assert has_more is False
        assert next_cursor is None

    def test_query_search_session_filter(self):
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
        fake = FakeDocumentDBBackend()
        repo = MessageRepository(db=fake)
        _seed(fake, [
            _archive_doc("user", "a", "msg-0", "2026-08-01T10:00:00+00:00", session_id="s1"),
            _archive_doc("user", "b", "msg-1", "2026-08-01T10:01:00+00:00", session_id="s2"),
        ])
        messages, *_ = asyncio.run(repo.query_search(session_id="s2"))
        assert [m.message_id for m in messages] == ["msg-1"]

    def test_query_search_all_sessions_when_session_none(self):
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
        fake = FakeDocumentDBBackend()
        repo = MessageRepository(db=fake)
        _seed(fake, [
            _archive_doc("user", "a", "msg-0", "2026-08-01T10:00:00+00:00", session_id="s1"),
            _archive_doc("user", "b", "msg-1", "2026-08-01T10:01:00+00:00", session_id="s2"),
        ])
        messages, *_ = asyncio.run(repo.query_search())
        assert len(messages) == 2

    def test_query_search_date_range_local_timezone(self):
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
        fake = FakeDocumentDBBackend()
        repo = MessageRepository(db=fake)
        # Asia/Shanghai = UTC+8。自然日 08-02：[08-02T00:00+08:00, 08-03T00:00+08:00)
        # = UTC [08-01T16:00, 08-02T16:00)
        _seed(fake, [
            _archive_doc("user", "before", "msg-0", "2026-08-01T15:59:59+00:00"),
            _archive_doc("user", "start", "msg-1", "2026-08-01T16:00:00+00:00"),
            _archive_doc("user", "mid", "msg-2", "2026-08-02T15:00:00+00:00"),
            _archive_doc("user", "end", "msg-3", "2026-08-02T16:00:00+00:00"),
        ])
        messages, *_ = asyncio.run(repo.query_search(
            date_from="2026-08-02", date_to="2026-08-02", timezone="Asia/Shanghai"))
        assert {m.message_id for m in messages} == {"msg-1", "msg-2"}

    def test_query_search_date_from_only(self):
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
        fake = FakeDocumentDBBackend()
        repo = MessageRepository(db=fake)
        _seed(fake, [
            _archive_doc("user", "old", "msg-0", "2026-08-01T23:59:59+00:00"),
            _archive_doc("user", "new", "msg-1", "2026-08-02T00:00:00+00:00"),
        ])
        messages, *_ = asyncio.run(repo.query_search(date_from="2026-08-02"))
        assert [m.message_id for m in messages] == ["msg-1"]

    def test_query_search_bad_timezone_returns_empty(self):
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
        fake = FakeDocumentDBBackend()
        repo = MessageRepository(db=fake)
        _seed(fake, [_archive_doc("user", "x", "msg-0", "2026-08-01T10:00:00+00:00")])
        messages, has_more, next_cursor = asyncio.run(repo.query_search(
            date_from="2026-08-01", timezone="Not/AZone"))
        assert messages == []
        assert has_more is False
        assert next_cursor is None

    def test_query_search_category_image(self):
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
        fake = FakeDocumentDBBackend()
        repo = MessageRepository(db=fake)
        _seed(fake, [
            _archive_doc("user", [{"type": "image", "src": "https://x/img.png"}], "msg-0", "2026-08-01T10:00:00+00:00"),
            _archive_doc("user", "纯文本", "msg-1", "2026-08-01T10:01:00+00:00"),
        ])
        messages, *_ = asyncio.run(repo.query_search(category="image"))
        assert [m.message_id for m in messages] == ["msg-0"]

    def test_query_search_category_tool_uses_role(self):
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
        fake = FakeDocumentDBBackend()
        repo = MessageRepository(db=fake)
        _seed(fake, [
            _archive_doc("user", "u", "msg-0", "2026-08-01T10:00:00+00:00"),
            _archive_doc("tool", "ok", "msg-1", "2026-08-01T10:01:00+00:00",
                         tool_call_id="t1", tool_name="web_search"),
        ])
        messages, *_ = asyncio.run(repo.query_search(category="tool"))
        assert [m.message_id for m in messages] == ["msg-1"]

    def test_query_search_category_link_matches_url_text(self):
        # 含 https 链接的消息 → category=link 命中（链接在 TextPart 内，archive 无独立 link part）
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
        fake = FakeDocumentDBBackend()
        repo = MessageRepository(db=fake)
        _seed(fake, [
            _archive_doc("user", "看 https://example.com 这里", "msg-0", "2026-08-01T10:00:00+00:00"),
            _archive_doc("user", "www.example.org 也是链接", "msg-1", "2026-08-01T10:01:00+00:00"),
            _archive_doc("user", "纯文本没有链接", "msg-2", "2026-08-01T10:02:00+00:00"),
        ])
        messages, *_ = asyncio.run(repo.query_search(category="link"))
        assert {m.message_id for m in messages} == {"msg-0", "msg-1"}

    def test_query_search_keyword_plus_category_and(self):
        # query + category 组合（不同 part 元素）：关键词命中 text part、分类命中 image part
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
        fake = FakeDocumentDBBackend()
        repo = MessageRepository(db=fake)
        _seed(fake, [
            _archive_doc("user", [{"type": "text", "content": "这是一张配图"},
                                  {"type": "image", "src": "https://x/a.png"}],
                         "msg-0", "2026-08-01T10:00:00+00:00"),
            _archive_doc("user", [{"type": "text", "content": "配图"},
                                  {"type": "text", "content": "但没有图片"}],
                         "msg-1", "2026-08-01T10:01:00+00:00"),
        ])
        messages, *_ = asyncio.run(repo.query_search(query="配图", category="image"))
        assert [m.message_id for m in messages] == ["msg-0"]

    def test_query_search_cursor_pagination_desc(self):
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
        fake = FakeDocumentDBBackend()
        repo = MessageRepository(db=fake)
        _seed(fake, [
            _archive_doc("user", "t1", "msg-1", "2026-08-01T10:01:00+00:00"),
            _archive_doc("user", "t2", "msg-2", "2026-08-01T10:02:00+00:00"),
            _archive_doc("user", "t3", "msg-3", "2026-08-01T10:03:00+00:00"),
            _archive_doc("user", "t4", "msg-4", "2026-08-01T10:04:00+00:00"),
            _archive_doc("user", "t5", "msg-5", "2026-08-01T10:05:00+00:00"),
        ])
        page1, has_more1, c1 = asyncio.run(repo.query_search(limit=2))
        assert [m.message_id for m in page1] == ["msg-5", "msg-4"]
        assert has_more1 is True and c1 is not None
        page2, has_more2, c2 = asyncio.run(repo.query_search(cursor=c1, limit=2))
        assert [m.message_id for m in page2] == ["msg-3", "msg-2"]
        assert has_more2 is True and c2 is not None
        page3, has_more3, c3 = asyncio.run(repo.query_search(cursor=c2, limit=2))
        assert [m.message_id for m in page3] == ["msg-1"]
        assert has_more3 is False and c3 is None

    def test_query_search_cursor_tie_break_same_timestamp(self):
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
        fake = FakeDocumentDBBackend()
        repo = MessageRepository(db=fake)
        ts = "2026-08-01T10:00:00+00:00"
        _seed(fake, [
            _archive_doc("user", "a", "msg-a", ts),
            _archive_doc("user", "b", "msg-b", ts),
            _archive_doc("user", "c", "msg-c", ts),
        ])
        page1, has_more1, c1 = asyncio.run(repo.query_search(limit=2))
        # 同 timestamp 下按 _id DESC（后插的排前面）
        assert [m.message_id for m in page1] == ["msg-c", "msg-b"]
        assert has_more1 is True
        page2, has_more2, _ = asyncio.run(repo.query_search(cursor=c1, limit=2))
        assert [m.message_id for m in page2] == ["msg-a"]
        assert has_more2 is False

    def test_query_search_returns_empty_when_db_none(self):
        repo = MessageRepository(db=None)
        messages, has_more, next_cursor = asyncio.run(repo.query_search())
        assert messages == []
        assert has_more is False
        assert next_cursor is None


class TestQuerySearchLiteral:
    """D3/P1-16：搜索字面化——raw regex 注入不再造成 500/ReDoS/广泛误匹配。"""

    @staticmethod
    def _repo(docs):
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
        fake = FakeDocumentDBBackend()
        repo = MessageRepository(db=fake)
        _seed(fake, docs)
        return repo

    def test_dot_is_literal_not_wildcard(self):
        """query='.' 只命中含字面句点的消息（修复前正则 '.' 匹配全部）。"""
        repo = self._repo([
            _archive_doc("user", "你好，世界", "m0", "2026-08-01T00:00:00+00:00"),
            _archive_doc("user", "版本 1.2 发布", "m1", "2026-08-01T00:01:00+00:00"),
        ])
        messages, *_ = asyncio.run(repo.query_search(query="."))
        assert [m.message_id for m in messages] == ["m1"]  # 只含句点那条

    def test_open_bracket_is_literal_no_regex_error(self):
        """query='[' 不再触发未闭合字符集 500。"""
        repo = self._repo([
            _archive_doc("user", "标签 [重要]", "m0", "2026-08-01T00:00:00+00:00"),
            _archive_doc("user", "普通消息", "m1", "2026-08-01T00:01:00+00:00"),
        ])
        messages, *_ = asyncio.run(repo.query_search(query="["))
        assert [m.message_id for m in messages] == ["m0"]

    def test_overlong_query_rejected(self):
        """query 超 200 字符 → 返回空（repo 层防御）。"""
        repo = self._repo([
            _archive_doc("user", "x", "m0", "2026-08-01T00:00:00+00:00"),
        ])
        messages, has_more, _ = asyncio.run(repo.query_search(query="a" * 201))
        assert messages == [] and has_more is False
