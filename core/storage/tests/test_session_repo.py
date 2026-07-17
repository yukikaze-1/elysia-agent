"""SessionRepository 单元测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.schema.character import CharacterState
from core.schema.memory_reflector import MemoryReflectorState
from core.schema.types import SessionId
from core.storage.session_repo import SessionRepository


@pytest.mark.unit
class TestSessionRepository:
    def test_save_calls_update_one(self):
        import asyncio
        repo = SessionRepository(db=MagicMock())
        repo._db.update_one = AsyncMock()
        cs = CharacterState(session_id=SessionId("sid"))
        rs = MemoryReflectorState()
        asyncio.run(repo.save(SessionId("sid"), [], cs, rs))
        repo._db.update_one.assert_called_once()

    def test_save_without_reflector_state(self):
        import asyncio
        repo = SessionRepository(db=MagicMock())
        repo._db.update_one = AsyncMock()
        cs = CharacterState(session_id=SessionId("sid"))
        asyncio.run(repo.save(SessionId("sid"), [], cs))
        repo._db.update_one.assert_called_once()

    def test_save_returns_none_when_db_none(self):
        import asyncio
        repo = SessionRepository(db=None)
        cs = CharacterState(session_id=SessionId("sid"))
        result = asyncio.run(repo.save(SessionId("sid"), [], cs))
        assert result is None

    def test_returns_empty_list_when_db_none(self):
        import asyncio
        repo = SessionRepository(db=None)
        result = asyncio.run(repo.list_all())
        assert result == []

    def test_load_returns_none_when_db_none(self):
        import asyncio
        repo = SessionRepository(db=None)
        result = asyncio.run(repo.load(SessionId("sid")))
        assert result is None

    def test_delete_returns_zero_when_db_none(self):
        import asyncio
        repo = SessionRepository(db=None)
        result = asyncio.run(repo.delete(SessionId("sid")))
        assert result == 0
