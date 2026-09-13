"""Agent 生命周期 — WorkflowEngine + mailbox 启停。"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from Module._common.task_scope import AsyncTaskScope, cancel_and_drain

if TYPE_CHECKING:
    from core.monitoring.event_bus import EventBus
    from core.cognitive import CognitiveLoop
    from core.adapter.tool_adapter import ToolAdapter
    from Module.workflow_engine.workflow_engine import WorkflowEngine
    from core.cognitive.delegation import DelegationPreparer
    from core.adapter.llm_adapter import LLMAdapter
    from core.schema.types import IsoTimestamp

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
        _lifecycle_tasks: AsyncTaskScope
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

        from core.hitl import EventBusHITLNotifier

        _hitl = EventBusHITLNotifier(self._bus)
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
        self._mailbox_task = self._lifecycle_tasks.spawn(
            self._poll_mailboxes(), name="agent-mailbox")

    async def _stop_mailbox(self) -> None:
        """停止后台任务 mailbox 轮询——cancel 后必须 await 消耗 CancelledError。"""
        await cancel_and_drain(
            self._mailbox_task, timeout=3.0, label="agent-mailbox")
        self._mailbox_task = None

    # 外层预算必须 ≥ `WorkflowEngine.stop()` 的**整条最坏路径**，不是它的单个
    # `timeout` 参数。S2-A3 的缺陷正是外层 5.0s < 内层 10.0s：只要有任意任务仍在
    # RUNNING（后台任务引擎的常态），外层必然先超时，把 stop() 从中间掐断，于是
    # 后续 `task.cancel()`、孤儿工具收尾、兜底清理**全部不执行**，运行中的任务在
    # 已关闭的 ToolAdapter/LLM/Mongo 上继续 await，semaphore 也永不释放。
    #
    # 内部算术（`Module/workflow_engine/workflow_engine.py::stop`）：
    #   等任务自然完成 10s + cancel 后二次等待 3s + 孤儿工具 3s + cancel 落地 1s
    #   ≈ 17s
    # 取 20s 留余量。**改内层任何一段都要同步改这里**——两条预算是一组不变量。
    _TASK_SYSTEM_STOP_BUDGET = 20.0

    async def _stop_task_system(self) -> None:
        """停止 WorkflowEngine（外层预算 ≥ 其内部预算，S2-A3）。"""
        if self._task_system:
            try:
                await asyncio.wait_for(
                    self._task_system.stop(),
                    timeout=self._TASK_SYSTEM_STOP_BUDGET)
            except asyncio.TimeoutError:
                # 超时说明 stop() 自身卡住（内部预算已用尽仍未返回）——留可见痕迹，
                # 但不改流程：_SHUTDOWN_STEPS 会继续走后面的清理。
                logger.error(
                    "WorkflowEngine stop 超时（>%.0fs，超过其内部最坏路径）——"
                    "后台任务未收尾，semaphore 可能未释放",
                    self._TASK_SYSTEM_STOP_BUDGET)
        # 关闭 DelegationPreparer 内部 AsyncOpenAI 客户端，避免 /restart 泄漏 httpx 连接池
        if self._delegation_preparer:
            try:
                await self._delegation_preparer.aclose()
            except Exception:
                logger.warning("DelegationPreparer close failed", exc_info=True)
