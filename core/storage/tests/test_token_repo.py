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

    def test_record_raises_when_db_none(self):
        """db=None 时 record 抛 StorageUnavailableError——触发 _persist 的 JSONL 兜底（不静默丢弃）。

        修复前 record() 静默 return → token_tracker._persist 视为成功 → 数据既不入 Mongo
        也不落 JSONL（永久丢失）。抛错让 _persist 的 except 路径写 pending 文件。
        """
        import asyncio
        from core.storage.attachment_repo import StorageUnavailableError
        repo = TokenRepository(db=None)
        entry = TokenUsage(
            session_id=SessionId("s1"), model="m", input_tokens=1,
            output_tokens=1, total_tokens=2, round_num=1,
        )
        with pytest.raises(StorageUnavailableError):
            asyncio.run(repo.record(entry))

    def test_flush_raises_when_db_none(self):
        """db=None 时 flush 抛异常——flush_pending 不 os.remove pending 文件（数据保留下次重试）。"""
        import asyncio
        from core.storage.attachment_repo import StorageUnavailableError
        repo = TokenRepository(db=None)
        entry = TokenUsage(
            session_id=SessionId("s1"), model="m", input_tokens=1,
            output_tokens=1, total_tokens=2, round_num=1,
        )
        with pytest.raises(StorageUnavailableError):
            asyncio.run(repo.flush([entry]))

    def test_flush_empty_list_is_noop_when_db_none(self):
        """空列表 + db=None → 仍 no-op（不抛）——flushed 空是合法状态。"""
        import asyncio
        repo = TokenRepository(db=None)
        asyncio.run(repo.flush([]))


@pytest.mark.unit
class TestTokenRepositoryConnect:
    """TokenRepository.connect/health_check 建连与自愈（修复：此前从未被 connect）。"""

    def _patch_create(self, monkeypatch, backend):
        def fake_create(provider_id, **kwargs):
            return backend
        monkeypatch.setattr(
            "core.storage.token_repo.DocumentDBFactory.create", staticmethod(fake_create))

    def test_connect_creates_backend_and_ensures_index(self, monkeypatch):
        """connect 建立后端并 ensure_index(session_id)（与 message/session repo 对称）。"""
        import asyncio
        backend = MagicMock()
        backend.ensure_index = AsyncMock()
        self._patch_create(monkeypatch, backend)
        repo = TokenRepository(database_name="default", base_url="mongodb://x")

        ok = asyncio.run(repo.connect())

        assert ok is True
        assert repo._db is backend
        backend.ensure_index.assert_awaited_once()
        args = backend.ensure_index.call_args.args
        assert args[0] == "token_usage"
        assert args[1] == [("session_id", 1)]

    def test_connect_returns_false_when_create_fails(self, monkeypatch):
        """create 抛异常 → 返回 False、db 保持 None（建连失败可被上层感知）。"""
        import asyncio

        def fake_create(provider_id, **kwargs):
            raise RuntimeError("mongo unreachable")
        monkeypatch.setattr(
            "core.storage.token_repo.DocumentDBFactory.create", staticmethod(fake_create))
        repo = TokenRepository(database_name="default", base_url="mongodb://x")

        ok = asyncio.run(repo.connect())

        assert ok is False
        assert repo._db is None

    def test_health_check_reconnects_when_db_none(self, monkeypatch):
        """db=None 时 health_check 尝试重连（与 message/session repo 对称，健康循环自愈）。"""
        import asyncio
        backend = MagicMock()
        backend.ensure_index = AsyncMock()
        self._patch_create(monkeypatch, backend)
        repo = TokenRepository(database_name="default", base_url="mongodb://x")

        ok = asyncio.run(repo.health_check())

        assert ok is True
        assert repo._db is backend

    def test_health_check_true_when_db_set(self):
        """已建连 → health_check 直接 True（不重复建连）。"""
        import asyncio
        repo = TokenRepository(db=MagicMock())
        assert asyncio.run(repo.health_check()) is True
