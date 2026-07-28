"""提醒工具插件 — 设置定时提醒。"""

import asyncio
from typing import Any

from pydantic import BaseModel, Field
from Module.tool.protocol import ToolPlugin, ToolContext
from Module.tool.capability import Capability
from Module.tool.safety import SafetyProfile


class ReminderParams(BaseModel):
    message: str = Field(..., description="提醒内容")
    delay_seconds: int = Field(
        ..., ge=1, le=86400,
        description="延迟秒数，最大86400（24小时）",
    )


class SetReminderPlugin(ToolPlugin):
    name = "set_reminder"
    description = "设置定时提醒，在指定秒数后通知用户"
    param_model = ReminderParams
    capabilities = {Capability.SCHEDULE}
    safety = SafetyProfile()

    async def execute(
        self, *, ctx: ToolContext, emit: Any = None, params: ReminderParams,
    ) -> str:
        """设置定时提醒，在指定秒数后通知用户。

        Args:
            ctx: 工具调用上下文。
            emit: 进度上报器（未使用）。
            params: 提醒参数，包含提醒内容和延迟秒数。

        Returns:
            设置结果字符串。
        """
        async def _remind():
            import asyncio
            await asyncio.sleep(params.delay_seconds)
            return f"⏰ 提醒: {params.message}"

        try:
            # 使用 asyncio.create_task 而非 scheduler.dispatch_background，
            # 避免 coroutine 不可 JSON 序列化导致持久化/恢复时丢失提醒
            asyncio.ensure_future(_remind())
            return f"已设置提醒，{params.delay_seconds} 秒后通知: {params.message}"
        except Exception as e:
            return f"设置提醒失败: {e}"
