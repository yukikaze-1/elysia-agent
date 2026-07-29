"""AgentLifecycle flush 方法 mock 测试。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest
from core.agent.lifecycle import AgentLifecycle

pytestmark = pytest.mark.service


class TestFlushConsolidator:
    @pytest.mark.asyncio
    async def test_flush_with_items(self, fake_agent):
        fake_agent._consolidator = AsyncMock()
        fake_agent._consolidator.flush.return_value = ["item1", "item2"]
        await AgentLifecycle._flush_consolidator(fake_agent)
        fake_agent._consolidator.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_flush_timeout_does_not_raise(self, fake_agent):
        fake_agent._consolidator = AsyncMock()
        fake_agent._consolidator.flush.side_effect = asyncio.TimeoutError
        await AgentLifecycle._flush_consolidator(fake_agent)
        # 不应抛异常

    @pytest.mark.asyncio
    async def test_no_consolidator_does_not_raise(self, fake_agent):
        fake_agent._consolidator = None
        await AgentLifecycle._flush_consolidator(fake_agent)
        # 不应抛异常


class TestFlushReflector:
    @pytest.mark.asyncio
    async def test_flush_delegates_to_engine(self, fake_agent):
        fake_agent._character_engine = MagicMock()
        fake_agent._character_engine.flush_reflector = AsyncMock()
        await AgentLifecycle._flush_reflector(fake_agent)
        fake_agent._character_engine.flush_reflector.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_engine_does_not_raise(self, fake_agent):
        fake_agent._character_engine = None
        await AgentLifecycle._flush_reflector(fake_agent)
        # 不应抛异常


class TestFlushCompression:
    @pytest.mark.asyncio
    async def test_flush_delegates_to_engine(self, fake_agent):
        fake_agent._character_engine = MagicMock()
        fake_agent._character_engine.flush_compression = AsyncMock()
        await AgentLifecycle._flush_compression(fake_agent)
        fake_agent._character_engine.flush_compression.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_engine_does_not_raise(self, fake_agent):
        fake_agent._character_engine = None
        await AgentLifecycle._flush_compression(fake_agent)
        # 不应抛异常
