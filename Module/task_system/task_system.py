"""TaskSystem 主类——对外 API 入口。

持有 Sub-Agent 池、TaskMailbox 注册表、基础设施引用。
所有对外方法都是此类的公共接口。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from Module.task_system.schema import (
    AgentId,
    AgentList,
    DelegationContext,
    ResumeError,
    SubmitError,
    TaskDAG,
    TaskId,
    TaskProgress,
    TaskStatus,
    TaskSummary,
    TokenPeriod,
    TokenStats,
    TokenUsage,
)

if TYPE_CHECKING:
    from Module.task_system.artifact_store import ArtifactStore
    from Module.task_system.checkpoint import CheckpointManager
    from Module.task_system.core.orchestrator import Orchestrator
    from Module.task_system.core.planner import Planner
    from Module.task_system.core.plan_validator import PlanValidator
    from Module.task_system.core.sub_agent import SubAgent
    from Module.task_system.infra import AgentInfra
    from Module.task_system.mailbox import TaskMailbox
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

        # ── 并发控制 ──
        self._semaphore = asyncio.BoundedSemaphore(max_sub_agents)

        # ── 运行时注册表 ──
        self._tasks: dict[TaskId, SubAgent] = {}       # 活跃任务 → SubAgent
        self._mailboxes: dict[TaskId, TaskMailbox] = {}  # 活跃任务 → Mailbox

        # ── 内部组件（骨架占位，后续逐步实例化）─────────────────────
        self._planner: Planner | None = None
        self._plan_validator: PlanValidator | None = None
        self._orchestrator: Orchestrator | None = None
        self._checkpoint_mgr: CheckpointManager | None = None
        self._artifact_store: ArtifactStore | None = None

    # ── 生命周期 ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动 TaskSystem。

        初始化内部组件（Planner/Orchestrator/CheckpointManager/ArtifactStore），
        恢复 pending checkpoints（服务重启后的任务恢复）。
        """
        # TODO: 实例化 Planner、PlanValidator、Orchestrator、
        #       CheckpointManager、ArtifactStore
        # TODO: 扫描 checkpoint 目录，恢复 PAUSED 状态的任务
        logger.info("TaskSystem.start: max_sub_agents=%d", self._semaphore._bound_value)

    async def stop(self) -> None:
        """软关停。

        等所有运行中的 Sub-Agent 完成当前节点 → 写 checkpoint → 清理。
        """
        # TODO: 遍历 self._tasks，逐个通知暂停
        # TODO: 等待所有任务 checkpoint 完成后 cleanup
        logger.info("TaskSystem.stop: 活跃任务数=%d", len(self._tasks))

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
        # TODO: 参数校验（context.intent 非空、context.tools 非空等）
        # TODO: 创建 TaskId、TaskMailbox
        # TODO: 构造 SubAgent 实例（注入 infra + hitl + planner + orchestrator + ...）
        # TODO: 获取 semaphore → asyncio.create_task(sub_agent.run(context, mailbox))
        # TODO: 注册到 self._tasks / self._mailboxes
        raise NotImplementedError("TaskSystem.submit() 尚未实现")

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
        """任务终态后清理注册表。

        SubAgent 调用此方法通知 TaskSystem 任务已结束。
        """
        self._tasks.pop(task_id, None)
        self._mailboxes.pop(task_id, None)
        logger.info("TaskSystem: task_id=%s 清理完成", task_id)
