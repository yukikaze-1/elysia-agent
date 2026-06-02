"""SetReminderPlugin 单元测试。"""

import pytest
from unittest.mock import AsyncMock

from Module.tool.protocol import ToolContext
from core.tools.reminder.plugin import SetReminderPlugin, ReminderParams


class TestSetReminderPlugin:
    """SetReminderPlugin execute() 测试。"""

    @pytest.mark.asyncio
    async def test_no_scheduler_returns_unavailable(self):
        """ctx.services 中无 scheduler 时返回不可用消息。"""
        plugin = SetReminderPlugin()
        ctx = ToolContext(services={})
        params = ReminderParams(message="测试提醒", delay_seconds=60)
        result = await plugin.execute(ctx=ctx, params=params)
        assert result == "提醒功能当前不可用（调度器未加载）。"

    @pytest.mark.asyncio
    async def test_dispatch_background_returns_task_id(self):
        """scheduler.dispatch_background() 返回 task ID 时，返回已设置提醒消息。"""
        plugin = SetReminderPlugin()
        mock_scheduler = AsyncMock()
        mock_scheduler.dispatch_background.return_value = "task-123"
        ctx = ToolContext(services={"scheduler": mock_scheduler})
        params = ReminderParams(message="喝水", delay_seconds=300)
        result = await plugin.execute(ctx=ctx, params=params)
        assert "已设置提醒" in result
        assert "300 秒" in result
        assert "喝水" in result
        mock_scheduler.dispatch_background.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_background_raises_runtime_error(self):
        """scheduler.dispatch_background() 抛出 RuntimeError 时返回失败消息。"""
        plugin = SetReminderPlugin()
        mock_scheduler = AsyncMock()
        mock_scheduler.dispatch_background.side_effect = RuntimeError("调度器繁忙")
        ctx = ToolContext(services={"scheduler": mock_scheduler})
        params = ReminderParams(message="测试", delay_seconds=10)
        result = await plugin.execute(ctx=ctx, params=params)
        assert "失败" in result
        assert "调度器繁忙" in result
        mock_scheduler.dispatch_background.assert_called_once()
