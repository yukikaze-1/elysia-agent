"""Agent 生命周期 — WorkflowEngine + mailbox 启停。"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable, Coroutine

if TYPE_CHECKING:
    from ..monitoring.event_bus import EventBus
    from ..cognitive import CognitiveLoop
    from ..adapter.tool_adapter import ToolAdapter
    from Module.workflow_engine.workflow_engine import WorkflowEngine
    from core.cognitive.delegation import DelegationPreparer
    from ..adapter.llm_adapter import LLMAdapter
    from core.schema.types import IsoTimestamp
    from Module.workflow_contract import HITLData
    from Module.workflow_contract import ApprovalId

logger = logging.getLogger(__name__)


class _LifecycleTaskSystem:
    """WorkflowEngine + mailbox 生命周期 mixin。"""

    if TYPE_CHECKING:
        _llm: LLMAdapter
        _tool_adapter: ToolAdapter
        _bus: EventBus
        _task_system: WorkflowEngine | None
        _delegation_preparer: DelegationPreparer | None
        _cognitive: CognitiveLoop
        _user_name: str
        _register_background_task: Callable[[str, Any, str, IsoTimestamp], None]
        _mailbox_task: asyncio.Task[None] | None
        _poll_mailboxes: Callable[[], Coroutine[Any, Any, None]]

    async def _start_task_system(self) -> None:
        """创建 WorkflowEngine + DelegationPreparer + HITLNotifier，注入 CognitiveLoop。"""
        from Module.workflow_engine.workflow_engine import WorkflowEngine
        from core.cognitive.delegation import DelegationPreparer
        from Module.workflow_contract.infra import AgentInfra
        from Module.workflow_contract import ConstraintConfig

        _ts_infra = AgentInfra(
            llm=self._llm.get_raw_client(),  # type: ignore[arg-type]
            milvus=None,
            http=None,
            tool_registry=self._tool_adapter,
            constraints=ConstraintConfig(),
        )

        from Module.workflow_contract import HITLNotifier

        class _EventBusHITLNotifier(HITLNotifier):
            def __init__(self, bus: EventBus) -> None:
                """初始化 EventBus 桥接的 HITL 通知器。

                Args:
                    bus: 事件总线实例，用于发布 HITL 审批事件。
                """
                self._bus = bus

            async def request_approval(self, event_data: "HITLData") -> None:
                """推送审批请求到前端收件箱（v1：不经过此通道）。

                Args:
                    event_data: HITLData 审批事件数据。
                """
                pass  # v1: 审批由 ToolManager SafetyPipeline 直接处理，不经过此通道

            async def cancel_approval(self, approval_ids: "list[ApprovalId]") -> None:
                """批量取消已发送的审批请求（v1：不经过此通道）。

                Args:
                    approval_ids: 需要取消的审批 ID 列表。
                """
                pass  # v1: 同上，审批全流程走 ToolManager，不经过此通道

        _hitl = _EventBusHITLNotifier(self._bus)
        self._task_system = WorkflowEngine(infra=_ts_infra, hitl_notifier=_hitl)
        await self._task_system.start()
        logger.info("WorkflowEngine started")
        # 注入 WorkflowEngine 到 ToolContext，供 get_runtime_config 查询活跃任务
        self._tool_adapter.tool_context.services["_task_system"] = self._task_system
        self._delegation_preparer = DelegationPreparer()

        # 注入到 CognitiveLoop
        from core.cognitive.user_profile import UserProfile
        self._cognitive.configure(
            task_system=self._task_system,
            delegation_preparer=self._delegation_preparer,
            user_profile=UserProfile(display_name=self._user_name),
            on_background_task=self._register_background_task,
        )

    def _start_mailbox_polling(self) -> None:
        """启动后台任务 mailbox 轮询（独立 asyncio.Task）。"""
        self._mailbox_task = asyncio.create_task(self._poll_mailboxes())

    async def _stop_mailbox(self) -> None:
        """停止后台任务 mailbox 轮询——cancel 后必须 await 消耗 CancelledError。"""
        if self._mailbox_task:
            self._mailbox_task.cancel()
            try:
                await asyncio.wait_for(self._mailbox_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._mailbox_task = None

    async def _stop_task_system(self) -> None:
        """停止 WorkflowEngine（5s 超时）。"""
        if self._task_system:
            try:
                await asyncio.wait_for(self._task_system.stop(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("WorkflowEngine stop timed out")
        # 关闭 DelegationPreparer 内部 AsyncOpenAI 客户端，避免 /restart 泄漏 httpx 连接池
        if self._delegation_preparer:
            try:
                await self._delegation_preparer.aclose()
            except Exception:
                logger.warning("DelegationPreparer close failed", exc_info=True)
