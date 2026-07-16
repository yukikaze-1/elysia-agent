"""SessionMemory 测试 — connect_db / health_check / save / load。"""
import logging
import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.memory.session import SessionMemory
from core.schema.types import MessageId, SessionId


@pytest.mark.unit
def test_collection_constant_in_repo():
    """COLLECTION 常量已迁移到 SessionRepository。"""
    from core.storage.session_repo import COLLECTION as REPO_COLLECTION
    assert REPO_COLLECTION == "sessions"


@pytest.mark.service
class TestSessionMemoryConnect:
    """connect_db() 创建 SessionRepository 并委托连接。"""

    @pytest.mark.asyncio
    async def test_connect_db_creates_repo_and_connects(self):
        sm = SessionMemory(session_id=SessionId("test-connect"))
        mock_repo = MagicMock()
        mock_repo.connect = AsyncMock(return_value=True)
        with patch(
            "core.memory.session.SessionRepository",
            return_value=mock_repo,
        ):
            result = await sm.connect_db()
        assert result is True
        assert sm._repo is mock_repo
        mock_repo.connect.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_db_with_explicit_repo(self):
        """传入 repo 参数时直接使用，不创建新的。"""
        mock_repo = MagicMock()
        mock_repo.connect = AsyncMock(return_value=True)
        sm = SessionMemory(session_id=SessionId("test-explicit"), repo=mock_repo)
        result = await sm.connect_db()
        assert result is True
        assert sm._repo is mock_repo
        mock_repo.connect.assert_called_once()


@pytest.mark.service
class TestSessionMemoryConnectLogLevel:
    """connect_db() 失败时必须使用 logger.warning，不是 info。"""

    @pytest.mark.asyncio
    async def test_connect_failure_uses_warning(self, caplog):
        sm = SessionMemory(session_id=SessionId("test-log"))
        with patch(
            "core.storage.session_repo.DocumentDBFactory.create",
            side_effect=RuntimeError("connection refused"),
        ):
            with caplog.at_level(logging.WARNING):
                result = await sm.connect_db()

        assert result is False
        assert sm._repo is not None
        # 必须有 WARNING 级别的日志
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) >= 1
        assert "MongoDB unavailable" in warnings[0].message


@pytest.mark.service
class TestSessionMemoryHealthCheck:
    """health_check() 委托给 SessionRepository。"""

    @pytest.mark.asyncio
    async def test_returns_true_when_repo_connected(self):
        mock_repo = MagicMock()
        mock_repo.health_check = AsyncMock(return_value=True)
        sm = SessionMemory(session_id=SessionId("test-hc"), repo=mock_repo)
        result = await sm.health_check()
        assert result is True
        mock_repo.health_check.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_false_when_repo_disconnected(self):
        mock_repo = MagicMock()
        mock_repo.health_check = AsyncMock(return_value=False)
        sm = SessionMemory(session_id=SessionId("test-hc"), repo=mock_repo)
        result = await sm.health_check()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_repo(self):
        sm = SessionMemory(session_id=SessionId("test-hc"))
        result = await sm.health_check()
        assert result is False


@pytest.mark.service
class TestSessionMemorySaveLoad:
    """save() / load() 委托给 SessionRepository，保留 JSONL 降级。"""

    @pytest.mark.asyncio
    async def test_save_delegates_to_repo(self):
        mock_repo = MagicMock()
        mock_repo._db = object()  # 模拟已连接
        mock_repo.save = AsyncMock()
        sm = SessionMemory(session_id=SessionId("test-save"), repo=mock_repo)
        result = await sm.save()
        assert result is True
        mock_repo.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_save_falls_back_to_jsonl_when_no_repo(self, tmp_path):
        pending = os.path.join(str(tmp_path), "session_sid.json")
        sm = SessionMemory(session_id=SessionId("sid"))
        sm._pending_file = pending
        result = await sm.save()
        assert result is True
        assert os.path.exists(pending)

    @pytest.mark.asyncio
    async def test_load_delegates_to_repo(self):
        from core.schema.message import UserMessage
        from core.schema.part import TextPart
        msg = UserMessage(
            message_id=MessageId("m1"),
            content=[TextPart(content="hello")],
        )

        mock_repo = MagicMock()
        mock_repo._db = object()  # 模拟已连接
        mock_repo.load = AsyncMock(return_value=([msg], {"mood": "happy"}))
        sm = SessionMemory(session_id=SessionId("test-load"), repo=mock_repo)
        result = await sm.load("test-load")
        assert result is True
        assert len(sm._messages) == 1
        assert sm._messages[0].message_id == "m1"
        assert sm.get_param("mood") == "happy"

    @pytest.mark.asyncio
    async def test_load_returns_false_when_not_found(self):
        mock_repo = MagicMock()
        mock_repo._db = object()
        mock_repo.load = AsyncMock(return_value=None)
        sm = SessionMemory(session_id=SessionId("test-miss"), repo=mock_repo)
        result = await sm.load("test-miss")
        assert result is False

    @pytest.mark.asyncio
    async def test_load_falls_back_to_jsonl_when_no_repo(self, tmp_path):
        pending = os.path.join(str(tmp_path), "session_sid.json")
        # Write a minimal JSON doc
        with open(pending, "w") as f:
            json.dump({"messages": [], "params": {}}, f)
        sm = SessionMemory(session_id=SessionId("sid"))
        sm._pending_file = pending
        result = await sm.load()
        assert result is True
        # Clean up
        os.remove(pending)
