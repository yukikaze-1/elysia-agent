"""提醒工具插件 — 设置定时提醒。"""

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
        self, *, ctx: ToolContext, emit=None, params: ReminderParams,
    ) -> str:
        scheduler = ctx.get("scheduler")
        if scheduler is None:
            return "提醒功能当前不可用（调度器未加载）。"

        async def _remind():
            import asyncio
            await asyncio.sleep(params.delay_seconds)
            return f"⏰ 提醒: {params.message}"

        try:
            tid = await scheduler.dispatch_background(_remind())
            return f"已设置提醒，{params.delay_seconds} 秒后通知: {params.message}"
        except Exception as e:
            return f"设置提醒失败: {e}"
