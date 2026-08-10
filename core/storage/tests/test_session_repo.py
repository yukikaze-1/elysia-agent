"""SessionRepository 单元测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.schema.character import CharacterState
from core.schema.memory_reflector import MemoryReflectorState
from core.schema.message import UserMessage
from core.schema.part import TextPart
from core.schema.types import SessionId
from core.storage.session_repo import SessionRepository, SessionSummary

from core.storage.tests._fake_documentdb import FakeDocumentDBBackend


def _make_user_msg(content: str = "你好，这是一段用于测试自动标题的消息内容",
                   msg_id: str = "msg-u1", session_id: str = "sid") -> UserMessage:
    """构造最小 UserMessage。"""
    return UserMessage(
        content=[TextPart(content=content)],
        message_id=msg_id,
        session_id=SessionId(session_id),
    )


@pytest.mark.unit
class TestSessionRepository:
    def test_save_calls_update_one(self):
        import asyncio
        repo = SessionRepository(db=MagicMock())
        repo._db.find = AsyncMock(return_value=[])  # type: ignore[reportOptionalMemberAccess]
        repo._db.update_one = AsyncMock()  # type: ignore[reportOptionalMemberAccess]
        cs = CharacterState(session_id=SessionId("sid"))
        rs = MemoryReflectorState()
        asyncio.run(repo.save(SessionId("sid"), [], cs, rs))
        repo._db.update_one.assert_called_once()  # type: ignore[reportOptionalMemberAccess]

    def test_save_without_reflector_state(self):
        import asyncio
        repo = SessionRepository(db=MagicMock())
        repo._db.find = AsyncMock(return_value=[])  # type: ignore[reportOptionalMemberAccess]
        repo._db.update_one = AsyncMock()  # type: ignore[reportOptionalMemberAccess]
        cs = CharacterState(session_id=SessionId("sid"))
        asyncio.run(repo.save(SessionId("sid"), [], cs))
        repo._db.update_one.assert_called_once()  # type: ignore[reportOptionalMemberAccess]

    def test_save_returns_none_when_db_none(self):
        import asyncio
        from core.storage.base import StorageUnavailableError
        repo = SessionRepository(db=None)
        cs = CharacterState(session_id=SessionId("sid"))
        # B2/P1-01：未连接不得静默 return 当成功——抛 typed unavailable
        with pytest.raises(StorageUnavailableError):
            asyncio.run(repo.save(SessionId("sid"), [], cs))

    def test_returns_empty_list_when_db_none(self):
        import asyncio
        repo = SessionRepository(db=None)
        result = asyncio.run(repo.list_all())
        assert result == []

    def test_load_wraps_connection_error_as_storage_unavailable(self):
        """review 复核 #2：find 抛连接异常（如 Motor ServerSelectionTimeoutError）
        → 转换为 StorageUnavailableError 并置 _db=None，触发健康循环真正重连——
        而非以普通异常冒泡被 caller 当作「存储为空」。"""
        import asyncio
        from core.storage.base import StorageUnavailableError
        db = MagicMock()
        db.find = AsyncMock(side_effect=ConnectionError("server selection timeout"))
        repo = SessionRepository(db=None)
        repo._db = db  # 模拟已连接但断网（db=None 构造 → _injected_db=False）
        with pytest.raises(StorageUnavailableError):
            asyncio.run(repo.load(SessionId("sid")))
        assert repo._db is None  # 状态失效，下一轮健康检查重连

    def test_load_wraps_error_injected_db_kept(self):
        """注入 fake（_injected_db=True）连接错误同样转 StorageUnavailableError，
        但不置 _db=None（测试 fake 由测试控制生命周期）。"""
        import asyncio
        from core.storage.base import StorageUnavailableError
        db = MagicMock()
        db.find = AsyncMock(side_effect=ConnectionError("down"))
        repo = SessionRepository(db=db)
        with pytest.raises(StorageUnavailableError):
            asyncio.run(repo.load(SessionId("sid")))
        assert repo._db is db

    def test_load_returns_none_when_db_none(self):
        import asyncio
        from core.storage.base import StorageUnavailableError
        repo = SessionRepository(db=None)
        # B2/P1-01：存储不可用抛 typed unavailable（区分 not found），caller 走 JSONL
        with pytest.raises(StorageUnavailableError):
            asyncio.run(repo.load(SessionId("sid")))

    def test_delete_returns_zero_when_db_none(self):
        import asyncio
        from core.storage.base import StorageUnavailableError
        repo = SessionRepository(db=None)
        # B3/P1-03：离线删除不得静默 0 当成功（重启会复活）
        with pytest.raises(StorageUnavailableError):
            asyncio.run(repo.delete(SessionId("sid")))

    # ---- 批次 3：title / create / rename ----

    def test_create_writes_title(self):
        import asyncio
        fake = FakeDocumentDBBackend()
        repo = SessionRepository(db=fake)
        asyncio.run(repo.create(SessionId("s1"), title="我的会话"))
        docs = asyncio.run(fake.find("sessions", {}))
        assert len(docs) == 1
        assert docs[0]["session_id"] == "s1"
        assert docs[0]["title"] == "我的会话"
        assert docs[0]["messages"] == []

    def test_create_default_empty_title(self):
        import asyncio
        fake = FakeDocumentDBBackend()
        repo = SessionRepository(db=fake)
        asyncio.run(repo.create(SessionId("s1")))
        docs = asyncio.run(fake.find("sessions", {}))
        assert docs[0]["title"] == ""

    def test_rename_updates_title_returns_true(self):
        import asyncio
        fake = FakeDocumentDBBackend()
        repo = SessionRepository(db=fake)
        asyncio.run(repo.create(SessionId("s1"), title="旧标题"))
        ok = asyncio.run(repo.rename(SessionId("s1"), "新标题"))
        assert ok is True
        docs = asyncio.run(fake.find("sessions", {}))
        assert docs[0]["title"] == "新标题"

    def test_rename_returns_false_when_session_missing(self):
        import asyncio
        fake = FakeDocumentDBBackend()
        repo = SessionRepository(db=fake)
        ok = asyncio.run(repo.rename(SessionId("nope"), "标题"))
        assert ok is False

    def test_rename_returns_false_when_db_none(self):
        import asyncio
        repo = SessionRepository(db=None)
        assert asyncio.run(repo.rename(SessionId("s1"), "标题")) is False

    def test_save_derives_auto_title_from_first_user_message(self):
        import asyncio
        fake = FakeDocumentDBBackend()
        repo = SessionRepository(db=fake)
        cs = CharacterState(session_id=SessionId("s1"))
        rs = MemoryReflectorState()
        asyncio.run(repo.save(SessionId("s1"), [_make_user_msg(content="今天天气真不错我们去公园散步吧！")], cs, rs))
        docs = asyncio.run(fake.find("sessions", {}))
        assert docs[0]["title"] == "今天天气真不错我们去公园散步吧！"

    def test_save_derives_title_truncated_to_20_chars(self):
        import asyncio
        fake = FakeDocumentDBBackend()
        repo = SessionRepository(db=fake)
        cs = CharacterState(session_id=SessionId("s1"))
        rs = MemoryReflectorState()
        long_text = "一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十"
        assert len(long_text) >= 25
        asyncio.run(repo.save(SessionId("s1"), [_make_user_msg(content=long_text)], cs, rs))
        docs = asyncio.run(fake.find("sessions", {}))
        assert len(docs[0]["title"]) == 20
        assert docs[0]["title"] == long_text[:20]

    def test_save_preserves_existing_title(self):
        import asyncio
        fake = FakeDocumentDBBackend()
        repo = SessionRepository(db=fake)
        asyncio.run(repo.create(SessionId("s1"), title="显式标题"))
        cs = CharacterState(session_id=SessionId("s1"))
        rs = MemoryReflectorState()
        asyncio.run(repo.save(SessionId("s1"), [_make_user_msg()], cs, rs))
        docs = asyncio.run(fake.find("sessions", {}))
        assert docs[0]["title"] == "显式标题"

    def test_save_preserves_renamed_title(self):
        import asyncio
        fake = FakeDocumentDBBackend()
        repo = SessionRepository(db=fake)
        asyncio.run(repo.create(SessionId("s1")))
        asyncio.run(repo.rename(SessionId("s1"), "改名后"))
        cs = CharacterState(session_id=SessionId("s1"))
        rs = MemoryReflectorState()
        asyncio.run(repo.save(SessionId("s1"), [_make_user_msg(content="新消息进来了")], cs, rs))
        docs = asyncio.run(fake.find("sessions", {}))
        assert docs[0]["title"] == "改名后"

    def test_save_no_title_when_only_assistant_messages(self):
        import asyncio
        fake = FakeDocumentDBBackend()
        repo = SessionRepository(db=fake)
        cs = CharacterState(session_id=SessionId("s1"))
        rs = MemoryReflectorState()
        asyncio.run(repo.save(SessionId("s1"), [], cs, rs))
        docs = asyncio.run(fake.find("sessions", {}))
        assert docs[0]["title"] == ""

    def test_list_all_summary_includes_title(self):
        import asyncio
        fake = FakeDocumentDBBackend()
        repo = SessionRepository(db=fake)
        asyncio.run(repo.create(SessionId("s1"), title="标题一"))
        asyncio.run(repo.create(SessionId("s2"), title="标题二"))
        summaries = asyncio.run(repo.list_all())
        assert isinstance(summaries[0], SessionSummary)
        titles = {s.session_id: s.title for s in summaries}
        assert titles[SessionId("s1")] == "标题一"
        assert titles[SessionId("s2")] == "标题二"
