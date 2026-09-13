"""提醒工具插件 — 设置定时提醒。"""

import asyncio
import logging
import uuid

from pydantic import BaseModel, Field
from Module._common.task_scope import AsyncTaskScope
from Module._common.error_type import ErrorType
from Module.tool.protocol import ToolPlugin, ToolContext, ToolEmitter, tool_failure, ToolResult
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

    def __init__(self) -> None:
        # ARCH-INC-P2-03：到期提醒是**跨工具调用存活**的后台协程（最长 24h）。
        # 此前裸 ensure_future 无强引用/无观察/无取消点：Agent 关机时任务被直接
        # 销毁，用户收到过「已设置提醒」却永远等不到提醒，且无任何日志。
        # 现在由插件实例持有，teardown() 统一收尾（ToolManager.shutdown 会调用）。
        self._reminders = AsyncTaskScope("reminder")

    async def teardown(self) -> None:
        """插件卸载时取消未到期的提醒（ARCH-INC-P2-03）。

        提醒依赖进程内 EventBus 投递，Agent 关停后即使留着也投不出去——
        显式取消并记录，好过静默销毁一个已向用户承诺过的任务。

        用 ``aclose()`` 而非 ``cancel_all()``：teardown 后本实例即终结
        （ToolManager 卸载时清出 ``_plugins``，reload 也是换新实例），
        之后再 spawn 属于生命周期错误，应当响亮失败而不是继续接受提醒。
        """
        cancelled = self._reminders.cancel_all()
        if cancelled:
            logger.info("reminder 插件卸载：取消 %d 个未到期提醒", cancelled)
        await self._reminders.aclose(timeout=2.0)

    async def execute(
        self, *, ctx: ToolContext, emit: ToolEmitter | None = None, params: ReminderParams,
    ) -> str | ToolResult:
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
            self._reminders.spawn(_remind(), name=f"reminder-{task_id}")
        except Exception as e:
            # spawn 在被拒（作用域已关闭）或无事件循环时抛——两种情况都不该对
            # 用户谎报「已设置」：工具返回值是用户唯一能看到的结果面。
            return tool_failure(f"设置提醒失败: {e}", error_type=ErrorType.TOOL_ERROR)
        return f"已设置提醒，{params.delay_seconds} 秒后通知: {params.message}"
