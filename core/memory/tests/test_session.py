"""SessionMemory: connect_db 日志级别 + health_check 重连 测试。"""
import logging
import pytest
from unittest.mock import AsyncMock, patch

from core.memory.session import SessionMemory


@pytest.mark.service
class TestSessionMemoryConnectLogLevel:
    """connect_db() 失败时必须使用 logger.warning，不是 info。"""

    @pytest.mark.asyncio
    async def test_connect_failure_uses_warning(self, caplog):
        sm = SessionMemory(session_id="test-log")
        with patch(
            "Module.database.documentdb.DocumentDBFactory.create",
            side_effect=RuntimeError("connection refused"),
        ):
            with caplog.at_level(logging.WARNING):
                result = await sm.connect_db()

        assert result is False
        assert sm._db is None  # 降级为 None
        # 必须有 WARNING 级别的日志
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) >= 1
        assert "MongoDB unavailable" in warnings[0].message


@pytest.mark.service
class TestSessionMemoryHealthCheck:
    """health_check() 在连接丢失后应尝试重连。"""

    @pytest.mark.asyncio
    async def test_returns_true_when_already_connected(self):
        sm = SessionMemory(session_id="test-hc")
        # 模拟已连接状态：_db 非 None
        sm._db = object()  # 非 None 表示已连接
        result = await sm.health_check()
        assert result is True

    @pytest.mark.asyncio
    async def test_reconnects_when_disconnected(self):
        sm = SessionMemory(session_id="test-hc")
        sm._db = None  # 模拟断开
        mock_db = object()
        with patch(
            "Module.database.documentdb.DocumentDBFactory.create",
            return_value=mock_db,
        ):
            result = await sm.health_check()

        assert result is True
        assert sm._db is mock_db

    @pytest.mark.asyncio
    async def test_returns_false_when_reconnect_fails(self):
        sm = SessionMemory(session_id="test-hc")
        sm._db = None
        with patch(
            "Module.database.documentdb.DocumentDBFactory.create",
            side_effect=RuntimeError("still down"),
        ):
            result = await sm.health_check()

        assert result is False
        assert sm._db is None
