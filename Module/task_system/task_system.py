"""TaskSystem 主类——对外 API 入口。

持有 Sub-Agent 池、TaskMailbox 注册表、基础设施引用。
所有对外方法都是此类的公共接口。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

from Module.task_system.mailbox import TaskMailbox
from Module.task_system.mailbox import push_mailbox_event
from Module.task_system.schema import (
    AgentId,
    AgentInfo,
    AgentList,
    AgentStatus,
    AgentType,
    DelegationContext,
    NodeStatus,
    now_iso,
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
        """供 TaskDebugAPI 内部轮询使用。

        线程安全说明：
            本方法在 TaskDebugAPI._poll_loop() 中调用，与 Orchestrator 主循环
            运行在同一个 asyncio event loop 中。CPython + asyncio 的协作式调度
            保证本方法在遍历 _tasks / _exec_state 期间不会在非 await 点被挂起——
            因此读取 exec_state.node_states 等可变 dict 时不需显式加锁。

            约束：本方法内禁止加 await。若将来需要异步 I/O，必须在 await 之前
            复制快照，await 之后重新获取 exec_state 引用。
        """
        tasks = []
        for task_id, sub_agent in self._tasks.items():
            tasks.append({
                "task_state": sub_agent._task_state,
                "graph": sub_agent._graph,
                "exec_state": sub_agent._orchestrator._exec_state,
                "mailbox_depth": self._mailboxes[task_id].count(),
            })
        return {
            "tasks": tasks,
            "slots_total": self._max_sub_agents,
            "slots_used": len(self._tasks),
        }

    # ── 任务控制 ─────────────────────────────────────────────────────────

    async def pause(self, task_id: TaskId) -> None:
        """暂停任务——在下一个安全点停止执行并保存 checkpoint。

        Orchestrator 在当前节点执行完成后检测到 pause_requested，
        保存 checkpoint 后返回 PAUSED，SubAgent 推送 PAUSED 事件后退出循环。

        Raises:
            KeyError: task_id 不存在（已结束或从未创建）
            RuntimeError: 任务不在可暂停状态（已终态 / 已暂停 / 尚未开始执行）
        """
        sub_agent = self._tasks.get(task_id)
        if sub_agent is None:
            raise KeyError(f"task_id={task_id} 不存在或已清理")

        ts = sub_agent._task_state
        if ts.is_terminal():
            raise RuntimeError(
                f"task_id={task_id} 已终态 ({ts.status.value})，无法暂停"
            )
        if ts.status == TaskStatus.PAUSED:
            raise RuntimeError(f"task_id={task_id} 已经暂停")

        exec_state = sub_agent._orchestrator._exec_state
        if exec_state is None:
            raise RuntimeError(
                f"task_id={task_id} 尚未开始执行（Orchestrator 未启动）"
            )

        exec_state.pause_requested = True
        logger.info("TaskSystem.pause: task_id=%s 暂停请求已发送", task_id)

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
        """软取消任务。二次确认由 CognitiveLoop 在上层处理。

        幂等——所有路径返回 True：
        - RUNNING → 设置 exec_state.cancel_requested → Orchestrator 检测 → CANCELLED
        - PAUSED  → 直接标记 CANCELLED，已有 checkpoint
        - 已终态   → True（无需操作）
        - 已清理   → True（任务已不存在）
        """
        # TODO: v1 只设标志，无超时反馈。
        #   若 Orchestrator 因异常永远不到达下一个循环迭代，
        #   cancel 信号可能永远不被处理。后续考虑 await + 超时机制。
        sub_agent = self._tasks.get(task_id)
        if sub_agent is None:
            return True  # 已清理 = 已取消

        ts = sub_agent._task_state
        if ts is None or ts.is_terminal():
            return True  # 已终态 = 已取消

        # ── 分支 1: PAUSED → 直接 CANCELLED ──
        if ts.status == TaskStatus.PAUSED:
            ts.status = TaskStatus.CANCELLED
            ts.updated_at = now_iso()
            mailbox = self._mailboxes.get(task_id)
            if mailbox:
                push_mailbox_event(mailbox, TaskEventType.CANCELLED, {
                    "task_id": str(task_id),
                    "reason": "用户取消",
                })
            # cleanup 由 _run_and_cleanup finally 块负责
            return True

        # ── 分支 2: RUNNING / PLANNING → 设取消标志 ──
        exec_state = sub_agent._orchestrator._exec_state
        if exec_state is None:
            # 尚未进入 Orchestrator（规划/校验阶段）
            ts.status = TaskStatus.CANCELLED
            ts.updated_at = now_iso()
            return True

        exec_state.cancel_requested = True
        return True

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
        """查询任务当前状态。最轻量——用户问"做完了吗？"时 LLM 调用。

        只查活跃任务（仍在 _tasks 注册表中）。任务终态清理后不可查询——
        此时应通过上层缓存（如 CognitiveLoop 的任务摘要）获取历史状态。
        """
        sub_agent = self._tasks.get(task_id)
        if sub_agent is None:
            raise KeyError(
                f"task_id={task_id} 不存在或已完成清理。"
                f"活跃任务: {list(self._tasks.keys())}"
            )
        ts = sub_agent._task_state
        if ts is None:
            # SubAgent.run() 尚未完成初始化——task_state 为 None
            return TaskStatus.PENDING
        return ts.status

    async def get_task_summary(self, task_id: TaskId) -> TaskSummary | None:
        """获取单个任务摘要。"""
        summaries = await self.list_active_tasks()
        for s in summaries:
            if s.task_id == task_id:
                return s
        return None

    async def list_active_tasks(self) -> list[TaskSummary]:
        """列出所有活跃任务。"""
        summaries: list[TaskSummary] = []
        for task_id, sub_agent in self._tasks.items():
            ts = sub_agent._task_state
            if ts is None:
                continue  # 尚未完成 run() 初始化，跳过
            checkpoint = (
                await sub_agent._checkpoint_mgr.load(task_id)
                if sub_agent._checkpoint_mgr else None
            )
            exec_state = checkpoint.exec if checkpoint else None

            nodes_total = 0
            nodes_completed = 0
            nodes_failed = 0
            nodes_skipped = 0
            if exec_state:
                nodes_total = len(exec_state.node_states)
                for ns in exec_state.node_states.values():
                    if ns.status == NodeStatus.SUCCESS:
                        nodes_completed += 1
                    elif ns.status in (NodeStatus.FAILED, NodeStatus.UNRECOVERABLE):
                        nodes_failed += 1
                    elif ns.status == NodeStatus.SKIPPED:
                        nodes_skipped += 1

            # extract intent from plan
            intent = ""
            if ts.plan and ts.plan.dag.nodes:
                intent = ts.plan.dag.nodes[0].description

            # calculate duration
            duration_ms = 0
            if ts.created_at:
                try:
                    created = datetime.fromisoformat(ts.created_at)
                    # 兼容无时区的 datetime——默认当作 UTC
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    duration_ms = int((datetime.now(timezone.utc) - created).total_seconds() * 1000)
                except (ValueError, TypeError):
                    pass

            summaries.append(TaskSummary(
                task_id=ts.task_id,
                status=ts.status,
                intent=intent,
                created_at=ts.created_at,
                duration_ms=duration_ms,
                nodes={"total": nodes_total, "completed": nodes_completed,
                       "failed": nodes_failed, "skipped": nodes_skipped},
                tools_used=[n.description for n in ts.plan.dag.nodes] if ts.plan else [],
                token_usage=ts.token_usage,
                token_total=sum(ts.token_usage.values()),
                approvals={"requested": 0, "approved": 0, "denied": 0},
                error=ts.error,
                error_code=ts.error_code,
                replans=ts.plan.plan_version - 1 if ts.plan else 0,
            ))
        return summaries

    def get_progress(self, task_id: TaskId) -> TaskProgress | None:
        """查询任务执行进度。用户问"到哪一步了？"时 LLM 调用。

        返回当前执行节点、完成数/总数、耗时、预估剩余时间。
        任务不存在或尚未开始执行时返回 None。
        """
        sub_agent = self._tasks.get(task_id)
        if sub_agent is None:
            return None

        ts = sub_agent._task_state
        if ts is None:
            return None

        exec_state = sub_agent._orchestrator._exec_state
        if exec_state is None:
            # 尚未进入 Orchestrator（规划/校验阶段）
            return TaskProgress(
                task_id=task_id,
                status=ts.status,
                total_nodes=len(
                    ts.plan.dag.nodes) if ts.plan else 0,
            )

        # 统计节点完成数
        completed = 0
        failed = 0
        current_node_id: NodeId | None = None
        for nid, ns in exec_state.node_states.items():
            if ns.status == NodeStatus.SUCCESS:
                completed += 1
            elif ns.status in (NodeStatus.FAILED, NodeStatus.UNRECOVERABLE):
                failed += 1
            elif ns.status == NodeStatus.RUNNING:
                current_node_id = nid

        total = len(exec_state.node_states)

        # 耗时
        elapsed_ms = 0
        if ts.created_at:
            try:
                from datetime import datetime, timezone as tz
                created = datetime.fromisoformat(ts.created_at)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=tz.utc)
                elapsed_ms = int(
                    (datetime.now(tz.utc) - created).total_seconds() * 1000)
            except (ValueError, TypeError):
                pass

        # 预估剩余时间（简单线性估算）
        estimated_remaining_ms: int | None = None
        done = completed + failed
        if done > 0 and elapsed_ms > 0 and total > done:
            avg_per_node = elapsed_ms / done
            estimated_remaining_ms = int(avg_per_node * (total - done))

        return TaskProgress(
            task_id=task_id,
            status=ts.status,
            current_node_id=current_node_id,
            completed_nodes=completed,
            total_nodes=total,
            elapsed_ms=elapsed_ms,
            estimated_remaining_ms=estimated_remaining_ms,
        )

    async def get_task_dag(self, task_id: TaskId) -> TaskDAG | None:
        """获取任务的 DAG 可视化数据。"""
        sub_agent = self._tasks.get(task_id)
        if sub_agent is None:
            # CheckpointManager 现为 per-task（由 SubAgent.run() 创建），
            # 任务不活跃时无对应 CheckpointManager，直接返回 None
            return None

        graph = sub_agent._graph
        if graph is None:
            return None
        exec_state = sub_agent._orchestrator._exec_state
        return graph.to_dag_view(exec_state.node_states if exec_state else {})

    def get_token_usage(self, task_id: TaskId) -> TokenUsage | None:
        """查询单任务的 token 消耗明细（含完整审计日志）。

        返回 TokenUsage（含 components 列表），每次 LLM 调用一条记录。
        任务不存在或尚未初始化时返回 None。
        """
        sub_agent = self._tasks.get(task_id)
        ts = sub_agent._task_state if sub_agent else None
        if ts is None:
            return None
        return TokenUsage(
            task_id=task_id,
            components=list(ts.token_entries),
        )

    def list_agents(self) -> AgentList:
        """当前活跃的 Sub-Agent 和 Worker Agent 列表。Debug 用。

        v1 只有 SubAgent（无 Worker），worker_agents 固定为空列表。
        """
        from datetime import datetime, timezone as tz

        now = datetime.now(tz.utc)
        sub_agents: list[AgentInfo] = []

        for task_id, sub_agent in self._tasks.items():
            ts = sub_agent._task_state

            # 映射 TaskStatus → AgentStatus
            if ts is None:
                agent_status = AgentStatus.RUNNING
            elif ts.status in (TaskStatus.RUNNING, TaskStatus.PLANNING,
                               TaskStatus.PAUSE_REQUESTED, TaskStatus.RESUMING):
                agent_status = AgentStatus.RUNNING
            elif ts.status == TaskStatus.PAUSED:
                agent_status = AgentStatus.PAUSED
            elif ts.status == TaskStatus.WAITING_APPROVAL:
                agent_status = AgentStatus.AWAITING_APPROVAL
            else:
                # 终态（COMPLETED/FAILED/CANCELLED）——理论上已清理，
                # 但如果在清理窗口期内仍可见，标记为 RUNNING
                agent_status = AgentStatus.RUNNING

            # 耗时
            duration_ms = 0
            started_at = ""
            if ts and ts.created_at:
                started_at = ts.created_at
                try:
                    created = datetime.fromisoformat(ts.created_at)
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=tz.utc)
                    duration_ms = int(
                        (now - created).total_seconds() * 1000)
                except (ValueError, TypeError):
                    pass

            sub_agents.append(AgentInfo(
                agent_id=AgentId(str(task_id)),
                agent_type=AgentType.SUB,
                task_id=task_id,
                status=agent_status,
                started_at=started_at,
                last_seen_at=now.isoformat(),
                duration_ms=duration_ms,
            ))

        return AgentList(
            sub_agents=sub_agents,
            worker_agents=[],  # v1 无 Worker Agent
            total_capacity=self._max_sub_agents,
            available_slots=(
                self._max_sub_agents - len(self._tasks)
                if self._semaphore is not None else 0
            ),
        )

    def get_token_stats(self, period: TokenPeriod = TokenPeriod.TODAY) -> TokenStats:
        """聚合 token 统计（按时间段 + 组件 + 模型）。

        遍历所有活跃任务，按 period 过滤，按 component/model 聚合。
        只统计活跃任务（_tasks dict 中的），已清理的任务不可查询。
        """
        now = datetime.now(timezone.utc)
        cutoff: datetime | None = None
        if period == TokenPeriod.TODAY:
            cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == TokenPeriod.WEEK:
            cutoff = now - timedelta(days=7)
        elif period == TokenPeriod.MONTH:
            cutoff = now - timedelta(days=30)
        # ALL: cutoff = None（不过滤）

        total = 0
        by_component: dict[str, int] = {}
        by_model: dict[str, int] = {}

        for sub_agent in self._tasks.values():
            ts = sub_agent._task_state
            if ts is None:
                continue
            # 按时间段过滤
            if cutoff is not None and ts.created_at:
                try:
                    created = datetime.fromisoformat(ts.created_at)
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if created < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass
            for entry in ts.token_entries:
                total += entry.total_tokens
                key = entry.component.value
                by_component[key] = by_component.get(key, 0) + entry.total_tokens
                if entry.model:
                    by_model[entry.model] = by_model.get(entry.model, 0) + entry.total_tokens

        return TokenStats(
            period=period,
            total_tokens=total,
            by_component=by_component,
            by_model=by_model,
        )

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
