"""SetReminderPlugin 单元测试。"""

import pytest

from Module.tool.protocol import ToolContext
from core.tools.reminder.plugin import SetReminderPlugin, ReminderParams

pytestmark = pytest.mark.unit


class TestSetReminderPlugin:
    """SetReminderPlugin execute() 测试。"""

    @pytest.mark.asyncio
    async def test_returns_success_no_scheduler_needed(self):
        """无需 scheduler 即可设置提醒（使用 asyncio.ensure_future）。"""
        plugin = SetReminderPlugin()
        ctx = ToolContext(services={})
        params = ReminderParams(message="测试提醒", delay_seconds=60)
        result = await plugin.execute(ctx=ctx, params=params)
        assert "已设置提醒" in result
        assert "60 秒" in result
        assert "测试提醒" in result

    @pytest.mark.asyncio
    async def test_returns_success_with_message(self):
        """确认返回消息包含延迟秒数和提醒内容。"""
        plugin = SetReminderPlugin()
        ctx = ToolContext(services={})
        params = ReminderParams(message="喝水", delay_seconds=300)
        result = await plugin.execute(ctx=ctx, params=params)
        assert "已设置提醒" in result
        assert "300 秒" in result
        assert "喝水" in result
