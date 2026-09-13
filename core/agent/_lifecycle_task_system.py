"""Agent 生命周期 — WorkflowEngine + mailbox 启停。"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from Module._common.task_scope import AsyncTaskScope, cancel_and_drain
from core.schema.event import TaskResultData

if TYPE_CHECKING:
    from core.monitoring.event_bus import EventBus
    from core.cognitive import CognitiveLoop
    from core.adapter.tool_adapter import ToolAdapter
    from Module.workflow_engine.workflow_engine import WorkflowEngine
    from core.cognitive.delegation import DelegationPreparer
    from core.adapter.llm_adapter import LLMAdapter
    from core.schema.types import IsoTimestamp
    from core.agent._types import RunState, _BackgroundTaskEntry
    from Module.workflow_contract.schema import WorkflowId

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
        _background_tasks: dict[WorkflowId, _BackgroundTaskEntry]
        _state: "RunState"

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

    # ---- mailbox 排空与投递（唯一实现点）----

    def drain_mailboxes_once(self) -> int:
        """同步排空所有活跃后台任务的 mailbox 并投递终态事件，返回投递条数。

        **轮询路径（`_poll_mailboxes`）与关停路径（`_stop_mailbox`）共用本方法**。
        两条路径各写一份必然漂移——本项目反复出现的「两条路径不对称」缺陷族。

        S2-A4：为什么关停路径也必须调它。`stop()` 第一行就把 `_state` 置 STOPPED，
        轮询循环条件随即为假；而 `WorkflowEngine.stop()` 排在关停步骤的后段，中间
        隔着 drain_cognitive + 4 个 flush + db_health。该窗口内完成/失败/取消的后台
        任务，其终态事件落在 mailbox 里**无人读**，而重启路径会新建 WorkflowEngine、
        旧 mailbox 一起丢弃——用户永远等不到「任务完成」唤醒。

        mailbox 本身是**有界保留**队列（终态事件优先保留，只淘汰最旧的 progress），
        所以"晚一点统一读"不会丢事件，只要求读的时候注册表还在。
        """
        delivered = 0
        for task_id, info in list(self._background_tasks.items()):
            mailbox = info.mailbox
            if mailbox is None:
                continue
            try:
                while True:
                    # 两种 mailbox 协议都要容忍，且语义不同：`asyncio.Queue` 空时
                    # **抛 QueueEmpty**，`WorkflowMailbox`（生产实现）空时**返回
                    # None**。只处理一种会让另一种走进外层 except 刷假 warning。
                    try:
                        event = mailbox.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if event is None:
                        break
                    data = event.data or {}
                    kind = event.type.value
                    if kind == "completed":
                        self._bus.post(TaskResultData(
                            task_id=task_id,
                            result=data.get("result", ""),
                            original_intent=info.intent,
                            dispatched_at=info.dispatched_at,
                        ))
                    elif kind == "failed":
                        self._bus.post(TaskResultData(
                            task_id=task_id,
                            result=f"任务执行失败: {data.get('error', '未知错误')}",
                            original_intent=info.intent,
                            dispatched_at=info.dispatched_at,
                            is_error=True,
                        ))
                        logger.warning(
                            "后台任务失败: %s — %s",
                            info.intent, data.get('error', '未知错误'),
                        )
                    elif kind == "cancelled":
                        # E3：取消事件改推 CANCELLED——消费侧须 pop 条目 + 呈现结果，
                        # 否则 _background_tasks 条目永久驻留（直到下次 start 清理）。
                        self._bus.post(TaskResultData(
                            task_id=task_id,
                            result=f"任务已取消: {data.get('reason', '用户取消')}",
                            original_intent=info.intent,
                            dispatched_at=info.dispatched_at,
                            is_error=True,
                        ))
                        logger.info(
                            "后台任务已取消: %s — %s",
                            info.intent, data.get('reason', '用户取消'),
                        )
                    else:
                        continue  # progress / approval_needed 等非终态：不投递、不 pop
                    delivered += 1
                    self._background_tasks.pop(task_id, None)
                    break  # task 已终态，处理下一个 task
            except Exception:
                # 处理单个事件时的意外错误（event.data 访问 / TaskResultData
                # 构造 / bus.post 等）——不中断整体排空，但必须留日志诊断。
                logger.warning(
                    "mailbox 投递事件异常（task_id=%s）", task_id, exc_info=True,
                )
        return delivered

    async def _stop_mailbox(self) -> None:
        """停止后台任务 mailbox 轮询。

        S2-A4：顺序是 **① 同步排空并投递 → ② 取消轮询任务 → ③ 清注册表**。
        - `stop_task_system` 已在本步之前完成（`_SHUTDOWN_STEPS` 顺序），所以此刻
          所有后台任务都已终态、终态事件都已写入各自 mailbox；
        - 轮询任务早在 `stop()` 入口 `_state` 置 STOPPED 时就停转了，停机窗口内到达
          的事件只有这一次同步排空能读到；
        - 注册表**必须**留到本步才清——在 `stop()` 入口清会让上面这次排空无条目可查
          （这正是 S2-A4 的原缺陷）。
        """
        delivered = self.drain_mailboxes_once()
        if delivered:
            logger.info(
                "mailbox 收尾排空：投递 %d 条后台任务终态事件（停机窗口内完成）", delivered)
        await cancel_and_drain(
            self._mailbox_task, timeout=3.0, label="agent-mailbox")
        self._mailbox_task = None
        # 仍未终态的任务：终态事件不会再有了（引擎已停）。不留静默丢失——逐条记
        # warning，至少让"用户等不到唤醒"这件事在日志里可归因。
        stranded = [(tid, e.intent) for tid, e in self._background_tasks.items()]
        if stranded:
            logger.warning(
                "停机时仍有 %d 个后台任务未见终态——本次进程内不会再投递"
                "（/restart 会新建 WorkflowEngine，旧任务与 mailbox 一并丢弃）: %s",
                len(stranded), stranded)
        self._background_tasks.clear()

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
