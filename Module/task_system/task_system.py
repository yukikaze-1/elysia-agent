"""TaskSystem 主类——对外 API 入口。

持有 Sub-Agent 池、TaskMailbox 注册表、基础设施引用。
所有对外方法都是此类的公共接口。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from Module.task_system.mailbox import TaskMailbox
from Module.task_system.schema import (
    AgentId,
    AgentList,
    DelegationContext,
    ResumeError,
    SubmitError,
    SubmitRejectionCode,
    TaskDAG,
    TaskId,
    TaskProgress,
    TaskStatus,
    TaskSummary,
    TokenPeriod,
    TokenStats,
    TokenUsage,
)

from Module.task_system.core.sub_agent import SubAgent

if TYPE_CHECKING:
    from Module.task_system.artifact_store import ArtifactStore
    from Module.task_system.checkpoint import CheckpointManager
    from Module.task_system.core.evaluator import Evaluator
    from Module.task_system.core.executor import Executor
    from Module.task_system.core.planner import Planner
    from Module.task_system.core.plan_validator import PlanValidator
    from Module.task_system.infra import AgentInfra
    from Module.task_system.protocol import HITLNotifier

logger = logging.getLogger(__name__)


class TaskSystem:
    """任务编排系统主类。

    职责：
        - 持有 Sub-Agent 并发控制（BoundedSemaphore）
        - 管理 TaskId → (SubAgent, TaskMailbox) 注册表
        - 提供 submit / pause / resume / cancel / submit_approval 公共 API
        - 提供只读查询方法（Pull 模式，供 CognitiveLoop + Debug WebUI）

    不负责：
        - 委派上下文准备（DelegationPreparer——CognitiveLoop 调用）
        - 审批前端推送（HITLNotifier——注入的外部实现）
        - 工具管理（ToolRegistry——CognitiveLoop 持有）

    依赖（注入）:
        infra:         跨任务共享的底层资源（LLM/Milvus/HTTP/ToolRegistry/ConstraintConfig）
        hitl_notifier: 审批快速通道实现
    """

    def __init__(
        self,
        infra: AgentInfra,
        hitl_notifier: HITLNotifier,
        *,
        max_sub_agents: int = 5,
    ) -> None:
        self._infra = infra
        self._hitl = hitl_notifier
        self._max_sub_agents = max_sub_agents

        # ── 并发控制（start() 时创建）──
        self._semaphore: asyncio.BoundedSemaphore | None = None

        # ── 运行时注册表 ──
        self._tasks: dict[TaskId, SubAgent] = {}
        self._mailboxes: dict[TaskId, TaskMailbox] = {}

        # ── 内部组件（start() 时实例化）────────────────────────────────
        self._planner: Planner | None = None
        self._plan_validator: PlanValidator | None = None
        self._executor: Executor | None = None
        self._evaluator: Evaluator | None = None
        self._checkpoint_mgr: CheckpointManager | None = None
        self._artifact_store: ArtifactStore | None = None

        # 防误用：submit() 前必须调 start()
        self._started = False

    # ── 生命周期 ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动 TaskSystem——初始化所有内部组件。

        调用方必须在 submit() 之前调用 start()。
        幂等——重复调用无副作用。
        """
        if self._started:
            logger.warning("TaskSystem.start: 已启动，跳过重复调用")
            return

        from Module.task_system.artifact_store import ArtifactStore
        from Module.task_system.checkpoint import CheckpointManager
        from Module.task_system.core.evaluator import Evaluator
        from Module.task_system.core.executor import Executor
        from Module.task_system.core.planner import Planner
        from Module.task_system.core.plan_validator import PlanValidator

        self._semaphore = asyncio.BoundedSemaphore(self._max_sub_agents)

        # 共享组件（无状态服务 + 存储层）
        self._artifact_store = ArtifactStore()
        self._checkpoint_mgr = CheckpointManager()
        self._planner = Planner()
        self._plan_validator = PlanValidator()
        self._executor = Executor(
            infra=self._infra,
            artifact_store=self._artifact_store,
            hitl_notifier=self._hitl,
        )
        self._evaluator = Evaluator(
            infra=self._infra,
            artifact_store=self._artifact_store,
        )
        # Orchestrator 不在此构造——per-SubAgent，SubAgent.__init__ 自建

        self._started = True
        logger.info("TaskSystem.start: max_sub_agents=%d, 组件初始化完成",
                     self._max_sub_agents)

    async def stop(self) -> None:
        """软关停。

        等所有运行中的 Sub-Agent 完成当前节点 → 写 checkpoint → 清理。
        """
        logger.info("TaskSystem.stop: 活跃任务数=%d", len(self._tasks))
        # TODO: 遍历 self._tasks，逐个通知暂停
        # TODO: 等待所有任务 checkpoint 完成后 cleanup
        self._started = False

    # ── 核心入口 ─────────────────────────────────────────────────────────

    async def submit(
        self,
        context: DelegationContext,
    ) -> TaskMailbox | SubmitError:
        """提交新任务。fire-and-forget——立即返回 Mailbox。

        DelegationPreparer.prepare() 已在 CognitiveLoop 侧完成，
        context 是干净的委派沙盒（筛选后的工具 + 摘要上下文 + 任务约束）。

        Returns:
            TaskMailbox — 调用方直接 await mailbox.get() 消费事件流
            SubmitError — 参数校验不通过时直接拒绝，不创建 Sub-Agent
        """
        if not self._started:
            return SubmitError(
                code=SubmitRejectionCode.NOT_READY,
                message="TaskSystem 尚未 start()，请先调用 start()",
            )

        # start() 已调用，所有组件和信号量必定初始化
        assert self._planner is not None
        assert self._plan_validator is not None
        assert self._executor is not None
        assert self._evaluator is not None
        assert self._checkpoint_mgr is not None
        assert self._artifact_store is not None
        assert self._semaphore is not None

        # ── 参数校验 ──
        if not context.intent.strip():
            return SubmitError(
                code=SubmitRejectionCode.INVALID_PARAM,
                message="intent 不能为空",
            )

        # ── 创建任务标识 ──
        task_id = TaskId(uuid.uuid4().hex[:8])

        # ── 创建 Mailbox（先于 SubAgent——保证返回时已有队列可用）──
        mailbox: TaskMailbox = TaskMailbox(task_id)

        # ── 构造 SubAgent ──
        sub_agent = SubAgent(
            infra=self._infra,
            hitl_notifier=self._hitl,
            planner=self._planner,
            plan_validator=self._plan_validator,
            executor=self._executor,
            evaluator=self._evaluator,
            checkpoint_manager=self._checkpoint_mgr,
            artifact_store=self._artifact_store,
        )

        # ── 获取并发槽位 + 注册 ──
        await self._semaphore.acquire()
        self._tasks[task_id] = sub_agent
        self._mailboxes[task_id] = mailbox

        # ── fire-and-forget 启动 ──
        async def _run_and_cleanup() -> None:
            try:
                await sub_agent.run(context, mailbox)
            except Exception:
                logger.exception("TaskSystem: task_id=%s 执行异常", task_id)
                # 补推送 FAILED 事件——SubAgent 在 run() 前期阶段
                # （Planner/Validator）抛异常时可能来不及推送，
                # TaskSystem 在此兜底，确保 mailbox 消费者不会永久阻塞
                from Module.task_system.mailbox import push_mailbox_event
                from Module.task_system.schema import TaskErrorCode, TaskEventType
                push_mailbox_event(
                    mailbox, TaskEventType.FAILED, {
                        "task_id": str(task_id),
                        "error": "任务执行异常",
                        "error_type": TaskErrorCode.INTERNAL_ERROR.value,
                    },
                    caller="TaskSystem",
                )
            finally:
                self._cleanup_task(task_id)
                await self._cleanup_storage(task_id)
                assert self._semaphore is not None
                self._semaphore.release()

        asyncio.create_task(_run_and_cleanup())
        logger.info("TaskSystem.submit: task_id=%s, intent=%.60s...", task_id, context.intent)
        return mailbox

    # ── 任务控制 ─────────────────────────────────────────────────────────

    async def pause(self, task_id: TaskId) -> bool:
        """暂停任务。RUNNING → PAUSE_REQUESTED → 当前节点完成后 → PAUSED。

        v1 限制：不支持注入新参数。如需修改任务参数，cancel → 重新 submit。
        """
        # TODO: 查找 self._tasks[task_id]，设置 exec_state.pause_requested = True
        raise NotImplementedError("TaskSystem.pause() 尚未实现")

    async def resume(self, task_id: TaskId) -> TaskMailbox | ResumeError:
        """从 checkpoint 恢复暂停的任务。

        仅 PAUSED 状态可 resume（CAS 防双 resume）。
        成功返回已有的 TaskMailbox（pause 时不销毁）。
        v1 限制：使用原始 DelegationContext，不支持注入新上下文。
        """
        # TODO: 从 CheckpointManager 加载 checkpoint
        # TODO: CAS 检查 task_state.status == PAUSED
        # TODO: 重建 SubAgent → 注入 checkpoint → 恢复执行
        raise NotImplementedError("TaskSystem.resume() 尚未实现")

    async def cancel(self, task_id: TaskId) -> bool:
        """软取消任务。二次确认由 CognitiveLoop 在上层处理。

        流程：写 checkpoint → 安全停止 Worker → 清理 Mailbox → GC 回收。
        """
        # TODO: 设置 task_state.status = CANCEL_REQUESTED
        # TODO: 等待当前节点完成 → 写 checkpoint → 清理
        raise NotImplementedError("TaskSystem.cancel() 尚未实现")

    async def submit_approval(
        self,
        task_id: TaskId,
        agent_id: AgentId,
        approved: bool,
        feedback: str | None = None,
    ) -> bool:
        """审批结果回传。路由到等待审批的 Worker。

        approved=True  → Worker 继续执行 MUTATION。
        approved=False → Worker 收到 PERMISSION_DENIED → Evaluator → 三层降级。
        feedback:       拒绝审批时的修正建议，注入 Planner prompt。
        """
        # TODO: 查找等待审批的 Worker，set approval_future 结果
        raise NotImplementedError("TaskSystem.submit_approval() 尚未实现")

    # ── 查询（Pull 模式，供 CognitiveLoop + Debug WebUI）────────────────

    def get_task_status(self, task_id: TaskId) -> TaskStatus:
        """查询任务当前状态。最轻量——用户问"做完了吗？"时 LLM 调用。"""
        # TODO: 从 self._tasks[task_id].task_state.status 读取
        raise NotImplementedError

    def get_task_summary(self, task_id: TaskId) -> TaskSummary | None:
        """查询任务摘要。用户问"刚才那个任务做了什么？"时 LLM 调用。"""
        raise NotImplementedError

    def list_active_tasks(self) -> list[TaskSummary]:
        """列出所有活跃任务。用户问"我有几个后台任务？"时 LLM 调用。"""
        raise NotImplementedError

    def get_progress(self, task_id: TaskId) -> TaskProgress | None:
        """查询任务进度。用户问"到哪一步了？"时 LLM 调用。"""
        raise NotImplementedError

    def get_task_dag(self, task_id: TaskId) -> TaskDAG | None:
        """查询 DAG 图。用户问"分了几个步骤？"时 LLM 调用。"""
        raise NotImplementedError

    def get_token_usage(self, task_id: TaskId) -> TokenUsage | None:
        """查询单任务的 token 消耗。LLM 报成本 / WebUI 展示。"""
        raise NotImplementedError

    def list_agents(self) -> AgentList:
        """当前活跃的 Sub-Agent 和 Worker Agent 列表。Debug 用。"""
        raise NotImplementedError

    def get_token_stats(self, period: TokenPeriod = TokenPeriod.TODAY) -> TokenStats:
        """聚合 token 统计。Debug 仪表盘用。"""
        raise NotImplementedError

    # ── 内部方法 ─────────────────────────────────────────────────────────

    def _cleanup_task(self, task_id: TaskId) -> None:
        """任务终态后清理注册表 + 存储层。

        调用方：_run_and_cleanup() finally 块。
        """
        self._tasks.pop(task_id, None)
        self._mailboxes.pop(task_id, None)
        # 异步存储清理由调用方在 await 后执行，此处仅标记
        logger.info("TaskSystem: task_id=%s 注册表清理完成", task_id)

    async def _cleanup_storage(self, task_id: TaskId) -> None:
        """清理任务关联的 checkpoint 和 artifact 存储。

        必须在 _cleanup_task() 之后调用——存储清理可能涉及 I/O（v2），
        不阻塞注册表释放。
        """
        if self._checkpoint_mgr:
            await self._checkpoint_mgr.delete(task_id)
        if self._artifact_store:
            await self._artifact_store.delete_by_task(task_id)
        logger.debug("TaskSystem: task_id=%s 存储清理完成", task_id)
