"""TokenRepository 单元测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from core.storage.token_repo import TokenRepository
from core.schema.token import TokenUsage
from core.schema.types import SessionId


@pytest.mark.unit
class TestTokenRepository:
    def test_record_calls_insert_one(self):
        import asyncio
        repo = TokenRepository(db=MagicMock())
        repo._db.insert_one = AsyncMock()  # type: ignore[reportOptionalMemberAccess]
        entry = TokenUsage(
            session_id=SessionId("s1"), model="m", input_tokens=10,
            output_tokens=20, total_tokens=30, round_num=1,
        )
        asyncio.run(repo.record(entry))
        repo._db.insert_one.assert_called_once()  # type: ignore[reportOptionalMemberAccess]
        doc = repo._db.insert_one.call_args[0][1]
        assert doc["input_tokens"] == 10
        assert doc["output_tokens"] == 20

    @pytest.mark.asyncio
    async def test_query_by_session_skips_legacy_docs(self):
        """旧格式文档（prompt/completion 字段）被跳过。"""
        repo = TokenRepository(db=AsyncMock())
        repo._db.find = AsyncMock(return_value=[
            {
                "session_id": "default", "model": "m",
                "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
                "round_num": 0, "timestamp": "2026-07-01T00:00:00+00:00",
            },
            {
                "session_id": "default", "model": "m",
                "input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
                "round_num": 0, "timestamp": "2026-07-01T00:00:00+00:00",
            },
        ])
        result = await repo.query_by_session("default")
        assert len(result) == 1
        assert result[0].input_tokens == 100

    def test_returns_empty_when_db_none(self):
        import asyncio
        repo = TokenRepository(db=None)
        result = asyncio.run(repo.query_by_session("s1"))
        assert result == []
