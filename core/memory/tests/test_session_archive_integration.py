"""SessionMemory + MessageArchive 集成测试。"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.memory.session import SessionMemory
from core.memory.message_archive import MessageArchive


@pytest.mark.service
class TestSessionArchiveIntegration:
    @pytest.mark.asyncio
    async def test_add_message_writes_to_archive(self):
        """add_message 应触发 archive.append（fire-and-forget）。"""
        archive = MagicMock(spec=MessageArchive)
        archive.append = AsyncMock(return_value="abc123")
        session = SessionMemory(max_messages=20, archive=archive)

        session.add_message("user", "hello")
        # fire-and-forget — 等待 task 执行
        await asyncio.sleep(0.05)
        archive.append.assert_called_once()
        # 验证传入的消息格式
        called_msg = archive.append.call_args[0][0]
        assert called_msg["role"] == "user"
        assert called_msg["content"] == [{"type": "text", "content": "hello"}]

    @pytest.mark.asyncio
    async def test_add_message_skips_when_no_archive(self):
        """archive=None 时 add_message 不应报错。"""
        session = SessionMemory(max_messages=20, archive=None)
        session.add_message("user", "hello")
        assert len(session.messages) == 1

    @pytest.mark.asyncio
    async def test_add_message_does_not_block_on_archive_error(self):
        """archive.append 失败时不应阻塞 add_message 返回。"""
        archive = MagicMock(spec=MessageArchive)
        archive.append = AsyncMock(side_effect=Exception("MongoDB down"))
        session = SessionMemory(max_messages=20, archive=archive)

        session.add_message("user", "hello")
        await asyncio.sleep(0.05)
        # 消息仍然成功添加到内存
        assert len(session.messages) == 1
