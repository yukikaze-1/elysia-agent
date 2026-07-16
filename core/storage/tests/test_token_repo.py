"""TokenRepository 单元测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from core.storage.token_repo import TokenRepository
from core.schema.token import TokenUsageEntry
from core.schema.types import SessionId


@pytest.mark.unit
class TestTokenRepository:
    def test_record_calls_insert_one(self):
        import asyncio
        repo = TokenRepository(db=MagicMock())
        repo._db.insert_one = AsyncMock()
        entry = TokenUsageEntry(
            session_id=SessionId("s1"), model="m", prompt_tokens=10,
            completion_tokens=20, total_tokens=30, round_num=1,
        )
        asyncio.run(repo.record(entry))
        repo._db.insert_one.assert_called_once()

    def test_returns_empty_when_db_none(self):
        import asyncio
        repo = TokenRepository(db=None)
        result = asyncio.run(repo.query_by_session("s1"))
        assert result == []
