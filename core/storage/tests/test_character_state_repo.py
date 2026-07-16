"""CharacterStateRepository 单元测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from core.storage.character_state_repo import CharacterStateRepository
from core.schema.character import CharacterState
from core.schema.emotion import EmotionState
from core.schema.reflector import ReflectorState
from core.schema.types import SessionId


@pytest.mark.unit
class TestCharacterStateRepository:
    def test_save_character_calls_update_one(self):
        import asyncio
        repo = CharacterStateRepository(db=MagicMock())
        repo._db.update_one = AsyncMock()
        state = CharacterState(session_id=SessionId("sid"))
        asyncio.run(repo.save_character(state))
        repo._db.update_one.assert_called_once()

    def test_save_emotion_calls_update_one(self):
        import asyncio
        repo = CharacterStateRepository(db=MagicMock())
        repo._db.update_one = AsyncMock()
        state = EmotionState(session_id=SessionId("sid"))
        asyncio.run(repo.save_emotion(state))
        repo._db.update_one.assert_called_once()

    def test_save_reflector_calls_update_one(self):
        import asyncio
        repo = CharacterStateRepository(db=MagicMock())
        repo._db.update_one = AsyncMock()
        state = ReflectorState(session_id=SessionId("sid"))
        asyncio.run(repo.save_reflector(state))
        repo._db.update_one.assert_called_once()

    def test_load_character_returns_none_when_db_none(self):
        import asyncio
        repo = CharacterStateRepository(db=None)
        result = asyncio.run(repo.load_character("sid"))
        assert result is None
