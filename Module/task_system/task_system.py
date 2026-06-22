"""TaskSystem 主类——对外 API 入口。

持有 Sub-Agent 池、TaskMailbox 注册表、基础设施引用。
所有对外方法都是此类的公共接口。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from Module.task_system import lifecycle
from Module.task_system import query
from Module.task_system.mailbox import TaskMailbox
from Module.task_system.mailbox import push_mailbox_event
from Module.task_system.schema import (
    AgentId,
    AgentList,
    DelegationContext,
    ResumeError,
    ResumeErrorCode,
    SubmitError,
    SubmitRejectionCode,
    TaskDAG,
    TaskErrorCode,
    TaskEventType,
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
        self._task_futures: dict[TaskId, asyncio.Task[None]] = {}  # 追踪 fire-and-forget 协程，供 stop() 等待/取消

        # ── 内部组件（per-task 化——由 SubAgent.run() 内部创建）──
        # 不再在此持有组件引用。移除了以下字段：
        # self._planner, self._plan_validator, self._executor,
        # self._evaluator, self._checkpoint_mgr, self._artifact_store

        # 防误用：submit() 前必须调 start()
        self._started = False

    # ── 生命周期 ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动 TaskSystem——初始化并发控制。

        调用方必须在 submit() 之前调用 start()。
        幂等——重复调用无副作用。
        """
        if self._started:
            logger.warning("TaskSystem.start: 已启动，跳过重复调用")
            return

        self._semaphore = asyncio.BoundedSemaphore(self._max_sub_agents)
        self._started = True
        logger.info("TaskSystem.start: max_sub_agents=%d", self._max_sub_agents)

    async def stop(self, timeout: float = 10.0) -> None:
        """Graceful shutdown。

        1. 拒绝新提交（_started = False）
        2. 等待活跃任务自然完成（最多 timeout 秒）
        3. 超时后 cancel 剩余任务——CancelledError 传播到 Orchestrator 的
           下一个 await 点，finally 块自动执行 cleanup + semaphore.release
        4. 兜底清理（防止 finally 未执行的情况）
        """
        logger.info("TaskSystem.stop: 活跃任务数=%d, timeout=%.0fs", len(self._tasks), timeout)
        self._started = False

        futures = list(self._task_futures.values())
        if futures:
            # 等待自然完成
            done, pending = await asyncio.wait(futures, timeout=timeout)

            if pending:
                logger.warning(
                    "TaskSystem.stop: %d/%d 个任务超时，强制取消",
                    len(pending), len(futures),
                )
                for task in pending:
                    task.cancel()

                # 等 cancel 在 finally 块中传播（cleanup + semaphore.release）
                # 给一个较短的等待——cancel 依赖下一个 await 点，通常 <1s
                cancelled_done, cancelled_pending = await asyncio.wait(pending, timeout=3.0)
                if cancelled_pending:
                    logger.error(
                        "TaskSystem.stop: %d 个任务 cancel 后仍未完成，强制清理",
                        len(cancelled_pending),
                    )

        # 兜底清理——防止 finally 块因异常未执行（即使无 futures 也要跑）
        for task_id in list(self._tasks.keys()):
            self._cleanup_task(task_id)
        logger.info("TaskSystem.stop: 完成")

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

        # start() 已调用，信号量必定初始化
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
        )

        # ── 获取并发槽位 + 注册 ──
        await self._semaphore.acquire()
        self._tasks[task_id] = sub_agent
        self._mailboxes[task_id] = mailbox

        # ── fire-and-forget 启动 ──
        async def _run_and_cleanup() -> None:
            try:
                await sub_agent.run(context, mailbox)
            except asyncio.CancelledError:
                # stop() 触发的取消——推送 FAILED 后重新抛出，让 cancel 正确传播
                push_mailbox_event(
                    mailbox, TaskEventType.FAILED, {
                        "task_id": str(task_id),
                        "error": "任务被取消",
                        "error_type": TaskErrorCode.INTERNAL_ERROR.value,
                    },
                    caller="TaskSystem",
                )
                raise
            except Exception:
                logger.exception("TaskSystem: task_id=%s 执行异常", task_id)
                # 补推送 FAILED 事件——SubAgent 在 run() 前期阶段
                # （Planner/Validator）抛异常时可能来不及推送，
                # TaskSystem 在此兜底，确保 mailbox 消费者不会永久阻塞
                push_mailbox_event(
                    mailbox, TaskEventType.FAILED, {
                        "task_id": str(task_id),
                        "error": "任务执行异常",
                        "error_type": TaskErrorCode.INTERNAL_ERROR.value,
                    },
                    caller="TaskSystem",
                )
            finally:
                # PAUSED 任务保持存活——checkpoint/mailbox/SubAgent/semaphore
                # 全部保留，等待 resume() 恢复执行。
                if (sub_agent._task_state is not None and
                    sub_agent._task_state.status == TaskStatus.PAUSED):
                    return
                self._cleanup_task(task_id)
                try:
                    await self._cleanup_storage(task_id, sub_agent)
                except Exception:
                    logger.exception(
                        "TaskSystem: task_id=%s 存储清理失败，槽位仍会释放", task_id,
                    )
                assert self._semaphore is not None
                self._semaphore.release()

        task = asyncio.create_task(_run_and_cleanup())
        self._task_futures[task_id] = task
        logger.info("TaskSystem.submit: task_id=%s, intent=%.60s...", task_id, context.intent)
        return mailbox

    def _get_debug_snapshot(self) -> dict:
        """供 TaskDebugAPI 内部轮询使用。"""
        return query.get_debug_snapshot(
            self._tasks, self._mailboxes, self._max_sub_agents,
        )

    # ── 任务控制 ─────────────────────────────────────────────────────────

    async def pause(self, task_id: TaskId, timeout: float = 30.0) -> bool:
        """暂停任务——委托给 lifecycle.pause()。

        等待 orchestrator 确认 PAUSED 状态，带超时。

        Returns:
            True:  暂停已确认
            False: 超时——orchestrator 未在 timeout 秒内到达检查点
        """
        return await lifecycle.pause(self._tasks, task_id, timeout=timeout)

    async def resume(self, task_id: TaskId) -> TaskMailbox | ResumeError:
        """从 PAUSED 恢复执行。

        幂等——已在 RUNNING/RESUMING 的任务直接返回 mailbox。
        仅 PAUSED 状态经过 CAS 检查后进入实际恢复流程。
        v1 限制：使用原始 DelegationContext，不支持注入新上下文。
        """
        sub_agent = self._tasks.get(task_id)
        if sub_agent is None:
            return ResumeError(
                code=ResumeErrorCode.CHECKPOINT_NOT_FOUND,
                message=f"task_id={task_id} 不存在或已清理",
            )

        ts = sub_agent._task_state
        if ts is None:
            return ResumeError(
                code=ResumeErrorCode.INVALID_STATE,
                message="任务尚未初始化",
            )
        if ts.is_terminal():
            return ResumeError(
                code=ResumeErrorCode.INVALID_STATE,
                message=f"任务已终态（{ts.status.value}），无法 resume",
            )
        if ts.status in (TaskStatus.RUNNING, TaskStatus.RESUMING):
            # 幂等——已经在跑了
            mailbox = self._mailboxes.get(task_id)
            if mailbox is not None:
                return mailbox
            return ResumeError(
                code=ResumeErrorCode.INVALID_STATE,
                message="任务正在运行但 mailbox 缺失，内部状态不一致",
            )
        if ts.status != TaskStatus.PAUSED:
            return ResumeError(
                code=ResumeErrorCode.INVALID_STATE,
                message=f"任务状态为 {ts.status.value}，仅 PAUSED 可 resume",
            )

        mailbox = self._mailboxes.get(task_id)
        if mailbox is None:
            return ResumeError(
                code=ResumeErrorCode.CHECKPOINT_NOT_FOUND,
                message="mailbox 缺失，内部状态不一致",
            )

        async def _run_resume() -> None:
            """包装 resume_from_pause，处理异常 + 终态清理。

            和 submit() 的 _run_and_cleanup 对称——resume 后的终态
            （COMPLETED/FAILED/CANCELLED）由此 finally 块负责清理。
            """
            try:
                await sub_agent.resume_from_pause()
            except asyncio.CancelledError:
                push_mailbox_event(
                    mailbox, TaskEventType.FAILED, {
                        "task_id": str(task_id),
                        "error": "任务被取消",
                        "error_type": TaskErrorCode.INTERNAL_ERROR.value,
                    },
                    caller="TaskSystem",
                )
                raise
            except Exception:
                logger.exception("TaskSystem: task_id=%s resume 异常", task_id)
                push_mailbox_event(
                    mailbox, TaskEventType.FAILED, {
                        "task_id": str(task_id),
                        "error": "resume 执行异常",
                        "error_type": TaskErrorCode.INTERNAL_ERROR.value,
                    },
                    caller="TaskSystem",
                )
            finally:
                self._cleanup_task(task_id)
                try:
                    await self._cleanup_storage(task_id, sub_agent)
                except Exception:
                    logger.exception(
                        "TaskSystem: task_id=%s resume 后存储清理失败", task_id,
                    )
                assert self._semaphore is not None
                self._semaphore.release()

        task = asyncio.create_task(_run_resume())
        self._task_futures[task_id] = task
        logger.info("TaskSystem.resume: task_id=%s 已恢复", task_id)
        return mailbox

    async def cancel(self, task_id: TaskId) -> bool:
        """软取消任务——委托给 lifecycle.cancel()。

        PAUSED 分支的 semaphore 释放 + 清理已由 lifecycle.cancel() 内部完成。
        调用方不得再次释放。
        """
        return await lifecycle.cancel(
            self._tasks, self._mailboxes, self._semaphore,
            self._cleanup_task, self._cleanup_storage, task_id,
        )

    def submit_approval(
        self,
        task_id: TaskId,
        agent_id: AgentId,
        approved: bool,
        feedback: str | None = None,
    ) -> bool:
        """审批结果回传——委托给 lifecycle.submit_approval()。

        task_id / feedback 为 v2 预留，v1 不使用。
        """
        return lifecycle.submit_approval(
            self._infra.tool_registry, str(agent_id), approved,
        )

    # ── 查询（Pull 模式，供 CognitiveLoop + Debug WebUI）────────────────
    #
    # 注意：所有查询方法只在活跃任务（仍在 _tasks 注册表中的）上工作。
    # 任务终态清理后不可查询——需要上层（CognitiveLoop / Debug WebUI）
    # 缓存历史数据，或在 v2 引入 TaskSummaryStore 持久化。

    def get_task_status(self, task_id: TaskId) -> TaskStatus | None:
        """查询任务当前状态。最轻量——用户问"做完了吗？"时 LLM 调用。

        只查活跃任务（仍在 _tasks 注册表中）。任务终态清理后不可查询——
        此时返回 None，调用方应通过上层缓存获取历史状态。
        """
        return query.get_task_status(self._tasks, task_id)

    def get_task_mailbox(self, task_id: TaskId) -> TaskMailbox | None:
        """获取指定任务的 mailbox（供 Agent 轮询消费）。

        任务不存在或已清理时返回 None。
        """
        return query.get_task_mailbox(self._mailboxes, task_id)

    async def get_task_summary(self, task_id: TaskId) -> TaskSummary | None:
        """获取单个任务摘要。"""
        return await query.get_task_summary(self._tasks, task_id)

    async def list_active_tasks(self) -> list[TaskSummary]:
        """列出所有活跃任务。"""
        return await query.list_active_tasks(self._tasks)

    def get_progress(self, task_id: TaskId) -> TaskProgress | None:
        """查询任务执行进度。用户问"到哪一步了？"时 LLM 调用。

        返回当前执行节点、完成数/总数、耗时、预估剩余时间。
        任务不存在或尚未开始执行时返回 None。
        """
        return query.get_progress(self._tasks, task_id)

    async def get_task_dag(self, task_id: TaskId) -> TaskDAG | None:
        """获取任务的 DAG 可视化数据。"""
        return await query.get_task_dag(self._tasks, task_id)

    def get_token_usage(self, task_id: TaskId) -> TokenUsage | None:
        """查询单任务的 token 消耗明细（含完整审计日志）。

        返回 TokenUsage（含 components 列表），每次 LLM 调用一条记录。
        任务不存在或尚未初始化时返回 None。
        """
        return query.get_token_usage(self._tasks, task_id)

    def list_agents(self) -> AgentList:
        """当前活跃的 Sub-Agent 和 Worker Agent 列表。Debug 用。

        v1 只有 SubAgent（无 Worker），worker_agents 固定为空列表。
        """
        return query.list_agents(
            self._tasks, self._max_sub_agents,
            self._semaphore is not None,
        )

    def get_token_stats(self, period: TokenPeriod = TokenPeriod.TODAY) -> TokenStats:
        """聚合 token 统计（按时间段 + 组件 + 模型）。

        遍历所有活跃任务，按 period 过滤，按 component/model 聚合。
        只统计活跃任务（_tasks dict 中的），已清理的任务不可查询。
        """
        return query.get_token_stats(self._tasks, period)

    # ── 内部辅助 ───────────────────────────────────────────────────────────

    async def _collect_tools_used(
        self,
        sub_agent: SubAgent,
        exec_state,  # ExecutionState | None
    ) -> list[str]:
        """从执行状态 + ArtifactStore 收集实际被调用的工具名。

        遍历已完成节点的 artifact_refs → ArtifactStore.get() →
        读取 metadata.tool_name，生成真实的工具审计列表。
        无 exec_state / 无 artifact_store 时回退为空列表。
        """
        return await query.collect_tools_used(sub_agent, exec_state)

    # ── 内部方法 ─────────────────────────────────────────────────────────

    def _cleanup_task(self, task_id: TaskId) -> None:
        """任务终态后清理注册表 + 存储层。

        调用方：_run_and_cleanup() finally 块、stop() 兜底。
        """
        self._tasks.pop(task_id, None)
        self._mailboxes.pop(task_id, None)
        self._task_futures.pop(task_id, None)  # 允许 asyncio.Task 被 GC
        logger.info("TaskSystem: task_id=%s 注册表清理完成", task_id)

    async def _cleanup_storage(self, task_id: TaskId, sub_agent: SubAgent) -> None:
        """清理任务关联的 checkpoint 和 artifact 存储。

        必须在 _cleanup_task() 之后调用——存储清理可能涉及 I/O（v2），
        不阻塞注册表释放。
        """
        if sub_agent._checkpoint_mgr:
            await sub_agent._checkpoint_mgr.delete(task_id)
        if sub_agent._artifact_store:
            await sub_agent._artifact_store.delete_by_task(task_id)
        logger.debug("TaskSystem: task_id=%s 存储清理完成", task_id)
