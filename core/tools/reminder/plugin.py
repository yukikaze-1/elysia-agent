"""提醒工具插件 — 设置定时提醒。"""

import asyncio
import logging
import uuid

from pydantic import BaseModel, Field
from Module.tool.protocol import ToolPlugin, ToolContext, ToolEmitter
from Module.tool.capability import Capability
from Module.tool.safety import SafetyProfile
from Module.workflow_contract.schema import WorkflowId

logger = logging.getLogger(__name__)


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
        self, *, ctx: ToolContext, emit: ToolEmitter | None = None, params: ReminderParams,
    ) -> str:
        """设置定时提醒，在指定秒数后通知用户。

        延迟到期后通过 EventBus 投递 TaskResultData 事件，
        Agent 事件循环拾取后唤醒 CognitiveLoop 显示提醒消息。

        Args:
            ctx: 工具调用上下文（需含 "_bus" EventBus 服务）。
            emit: 进度上报器（未使用）。
            params: 提醒参数，包含提醒内容和延迟秒数。

        Returns:
            设置结果字符串。
        """
        bus = ctx.services.get("_bus")
        task_id = f"reminder-{uuid.uuid4().hex[:8]}"
        reminder_msg = f"⏰ 提醒: {params.message}"

        async def _remind():
            await asyncio.sleep(params.delay_seconds)
            if bus is not None:
                from core.schema.event import TaskResultData
                bus.post(
                    TaskResultData(
                        # reminder 自生成 ID 适配 TaskResultData.task_id（WorkflowId 语义）
                        task_id=WorkflowId(task_id),
                        result=reminder_msg,
                        original_intent=f"在 {params.delay_seconds} 秒后提醒用户: {params.message}",
                        dispatched_at="",
                    ),
                    type="task_result",
                )
            else:
                logger.warning(
                    "提醒到期但 EventBus 不可用，消息丢弃: %s", params.message,
                )

        try:
            asyncio.ensure_future(_remind())
            return f"已设置提醒，{params.delay_seconds} 秒后通知: {params.message}"
        except Exception as e:
            return f"设置提醒失败: {e}"
