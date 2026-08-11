"""WorkflowEngine 主类——对外 API 入口。

持有 Sub-Agent 池、WorkflowMailbox 注册表、基础设施引用。
所有对外方法都是此类的公共接口。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from Module.workflow_engine import lifecycle
from Module.workflow_engine import query
from Module.workflow_engine.mailbox import WorkflowMailbox
from Module.workflow_engine.mailbox import push_mailbox_event
from Module.workflow_contract import (
    AgentId,
    DelegationContext,
    SubmitError,
    SubmitRejectionCode,
    WorkflowEventType,
    WorkflowId,
    WorkflowStatus,
)
from Module.workflow_engine.schema import (
    AgentList,
    ResumeError,
    ResumeErrorCode,
    WorkflowDAG,
    WorkflowErrorCode,
    WorkflowProgress,
    WorkflowState,
    WorkflowSummary,
    TokenPeriod,
    TokenStats,
    TokenUsage,
    now_iso,
)

from Module.workflow_engine.core.sub_agent import SubAgent

if TYPE_CHECKING:
    from Module.workflow_engine.core.execution import ExecutionState
    from Module.workflow_contract import AgentInfra
    from Module.workflow_contract import HITLNotifier

logger = logging.getLogger(__name__)


class QueuedItemKind(StrEnum):
    """排队条目判别——新提交 vs 恢复暂停。

    队列必须携带 kind——_drain_queue 无法从 WorkflowId 判别
    「新提交」还是「恢复暂停」，二者派发不同的执行包装。
    """
    SUBMIT = "submit"    # 新任务：派发 _run_and_cleanup
    RESUME = "resume"    # 恢复暂停：派发 _run_resume


@dataclass(frozen=True)
class QueuedItem:
    """有界队列元素——(task_id, kind) 判别条目。

    由 submit()/resume() 写入、_drain_queue 后台 worker 消费。
    """
    task_id: WorkflowId
    kind: QueuedItemKind


class WorkflowEngine:
    """任务编排系统主类。

    职责：
        - 持有 Sub-Agent 并发控制（BoundedSemaphore）
        - 管理 WorkflowId → (SubAgent, WorkflowMailbox) 注册表
        - 提供 submit / pause / resume / cancel / submit_approval 公共 API
        - 提供只读查询方法（Pull 模式，供 CognitiveLoop + Debug WebUI）

    不负责：
        - 委派上下文准备（DelegationPreparer 在 core/cognitive/delegation.py，由 CognitiveLoop 调用）
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
        max_queue_size: int = 5,
    ) -> None:
        """初始化 WorkflowEngine。

        Args:
            infra: 跨任务共享的底层资源容器。
            hitl_notifier: 审批快速通道实现。
            max_sub_agents: 最大并发 Sub-Agent 数量。
            max_queue_size: 排队队列最大长度（E2 非阻塞有界排队）。
        """
        self._infra = infra
        self._hitl = hitl_notifier
        self._max_sub_agents = max_sub_agents
        self._max_queue_size = max_queue_size

        # ── 并发控制（start() 时创建）──
        self._semaphore: asyncio.BoundedSemaphore | None = None

        # ── 运行时注册表 ──
        self._tasks: dict[WorkflowId, SubAgent] = {}
        self._mailboxes: dict[WorkflowId, WorkflowMailbox] = {}
        self._task_futures: dict[WorkflowId, asyncio.Task[None]] = {}  # 追踪 fire-and-forget 协程，供 stop() 等待/取消
        self._contexts: dict[WorkflowId, DelegationContext] = {}  # E2：委派上下文注册表（run 包装取参）

        # ── E2 有界排队（start() 时激活）──
        self._queue: asyncio.Queue["QueuedItem"] | None = None
        self._queue_worker: asyncio.Task[None] | None = None
        self._active: set[WorkflowId] = set()  # 已持有执行槽的任务（slots 精确计数）

        # ── 内部组件（per-task 化——由 SubAgent.run() 内部创建）──
        # 不再在此持有组件引用。移除了以下字段：
        # self._planner, self._plan_validator, self._executor,  # 旧字段（已移除）
        # self._evaluator, self._checkpoint_mgr, self._artifact_store  # 旧字段（已移除）

        # 防误用：submit() 前必须调 start()
        self._started = False

    # ── 生命周期 ─────────────────────────────────────────────────────────

    @property
    def constraints(self):
        """全局约束配置（只读），来自 AgentInfra.constraints。

        CognitiveLoop 的 submit_task 工具路径通过此属性获取约束，
        传给 core/cognitive/delegation.py 的 DelegationPreparer.prepare()，
        避免直接访问私有 _infra。
        """
        return self._infra.constraints

    async def start(self) -> None:
        """启动 WorkflowEngine——初始化并发控制。

        调用方必须在 submit() 之前调用 start()。
        幂等——重复调用无副作用。
        """
        if self._started:
            logger.warning("WorkflowEngine.start: 已启动，跳过重复调用")
            return

        self._semaphore = asyncio.BoundedSemaphore(self._max_sub_agents)
        self._queue = asyncio.Queue(maxsize=self._max_queue_size)
        self._queue_worker = asyncio.create_task(self._drain_queue())
        self._started = True
        logger.info("WorkflowEngine.start: max_sub_agents=%d", self._max_sub_agents)

    async def _drain_queue(self) -> None:
        """后台派发循环——单任务，非阻塞聊天轮。

        从队列取出条目 → 终态复核 → acquire 槽位 → 派发执行。
        槽位所有权：已派发 → 归执行包装（_run_and_cleanup/_run_resume 的 finally）所有；
        未派发被 stop() 取消 → 由本循环释放，防泄漏。
        """
        # start() 创建 semaphore/queue 后才创建本 worker——两者必定已初始化
        assert self._semaphore is not None
        assert self._queue is not None
        while True:
            item: QueuedItem = await self._queue.get()   # 阻塞等排队条目
            task_id = item.task_id
            dispatched = False
            held_permit = False
            try:
                sub_agent = self._tasks.get(task_id)
                if sub_agent is None:
                    continue                          # 已 cancel/stop 清理，跳过（不 acquire）
                ts = sub_agent._task_state
                if ts is None or ts.is_terminal():
                    self._cleanup_task(task_id)        # 兜底清理（cancel 侧通常已清）
                    continue
                await self._semaphore.acquire()        # acquire 移到此处——不阻塞聊天轮
                held_permit = True
                self._active.add(task_id)
                if task_id not in self._tasks:         # acquire 等待期间被 cancel/stop 清理
                    self._active.discard(task_id)
                    self._semaphore.release()
                    held_permit = False
                    continue
                try:
                    if item.kind is QueuedItemKind.SUBMIT:
                        task = asyncio.create_task(self._run_and_cleanup(sub_agent, task_id))
                    else:
                        task = asyncio.create_task(self._run_resume(sub_agent, task_id))
                except Exception:
                    # create_task 罕见失败（如事件循环关闭）：释放槽位 + 清理注册表，防槽位泄漏
                    self._active.discard(task_id)
                    self._semaphore.release()
                    held_permit = False
                    self._cleanup_task(task_id)
                    continue
                self._task_futures[task_id] = task     # 必须登记——stop() 靠它等待/取消
                dispatched = True                      # 已派发：槽位归执行包装所有
            except asyncio.CancelledError:
                # 注：held_permit=True→dispatched=True 之间无任何 await，本分支（已 acquire 未派发
                # 时被取消）在协作式调度下不可达——取消只送达 acquire 阻塞处（held_permit=False）或
                # 下一轮 get()。保留防御性释放，防未来引入中断点导致槽位泄漏。
                if held_permit and not dispatched:
                    self._active.discard(task_id)
                    self._semaphore.release()
                raise
            finally:
                self._queue.task_done()

    async def stop(self, timeout: float = 10.0) -> None:
        """优雅关闭。

        1. 拒绝新提交（_started = False）
        2. 停后台队列 worker（cancel + gather）——worker 的 except CancelledError
           已处理「已 acquire 未派发」的槽位释放；未 acquire 的条目留在队列，
           由第 3 步的 _tasks 扫描终态化覆盖（条目无论仍在队列 / 已被取走 / 已取未派发）。
        3. 终态化未派发/暂停任务：扫描 _tasks——task_state 为空、status in
           (QUEUED, PAUSED)、或 task_id 不在 _task_futures → 置 CANCELLED +
           推 CANCELLED 事件 + _cleanup_task（+ best-effort _cleanup_storage）。
        4. 等待活跃任务自然完成（最多 timeout 秒）
        5. 超时后 cancel 剩余任务——CancelledError 传播到 Orchestrator 的
           下一个 await 点，finally 块自动执行 cleanup + semaphore.release
        6. 兜底清理（防止 finally 未执行的情况）

        Args:
            timeout: 等待活跃任务自然完成的超时秒数。
        """
        logger.info("WorkflowEngine.stop: 活跃任务数=%d, timeout=%.0fs", len(self._tasks), timeout)
        self._started = False

        # 停后台队列 worker（cancel 触发 _drain_queue 的 CancelledError 槽位归还）
        if self._queue_worker is not None and not self._queue_worker.done():
            self._queue_worker.cancel()
            await asyncio.gather(self._queue_worker, return_exceptions=True)

        # 终态化未派发/暂停任务（以 _tasks 为唯一权威扫描，不依赖队列归还）
        for task_id in list(self._tasks.keys()):
            sub_agent = self._tasks.get(task_id)
            if sub_agent is None:
                continue
            ts = sub_agent._task_state
            if (ts is None or ts.status in (WorkflowStatus.QUEUED, WorkflowStatus.PAUSED)
                    or task_id not in self._task_futures):
                if ts is not None:
                    ts.status = WorkflowStatus.CANCELLED
                    ts.updated_at = now_iso()
                mailbox = self._mailboxes.get(task_id)
                if mailbox:
                    push_mailbox_event(mailbox, WorkflowEventType.CANCELLED,
                                       {"task_id": str(task_id), "reason": "服务停止"})
                self._cleanup_task(task_id)
                try:
                    await self._cleanup_storage(task_id, sub_agent)
                except Exception:
                    logger.exception("WorkflowEngine.stop: task_id=%s 清理失败", task_id)

        futures = list(self._task_futures.values())
        if futures:
            # 等待自然完成
            done, pending = await asyncio.wait(futures, timeout=timeout)

            if pending:
                logger.warning(
                    "WorkflowEngine.stop: %d/%d 个任务超时，强制取消",
                    len(pending), len(futures),
                )
                for task in pending:
                    task.cancel()

                # 等 cancel 在 finally 块中传播（cleanup + semaphore.release）
                # 给一个较短的等待——cancel 依赖下一个 await 点，通常 <1s
                cancelled_done, cancelled_pending = await asyncio.wait(pending, timeout=3.0)
                if cancelled_pending:
                    logger.error(
                        "WorkflowEngine.stop: %d 个任务 cancel 后仍未完成，强制清理",
                        len(cancelled_pending),
                    )

        # 兜底清理——防止 finally 块因异常未执行（即使无 futures 也要跑）
        for task_id in list(self._tasks.keys()):
            self._cleanup_task(task_id)
        logger.info("WorkflowEngine.stop: 完成")

    # ── 核心入口 ─────────────────────────────────────────────────────────

    async def submit(
        self,
        context: DelegationContext,
    ) -> WorkflowMailbox | SubmitError:
        """提交新任务。fire-and-forget——立即返回 Mailbox。

        DelegationPreparer.prepare() 已在 core/cognitive 侧完成
        （core/cognitive/delegation.py，由 CognitiveLoop 调用），
        context 是干净的委派沙盒（筛选后的工具 + 摘要上下文 + 任务约束）。

        Args:
            context: 委派上下文（筛选后的工具 + 摘要上下文 + 任务约束）。

        Returns:
            WorkflowMailbox — 调用方直接 await mailbox.get() 消费事件流
            SubmitError — 参数校验不通过时直接拒绝，不创建 Sub-Agent
        """
        if not self._started:
            return SubmitError(
                code=SubmitRejectionCode.NOT_READY,
                message="WorkflowEngine 尚未 start()，请先调用 start()",
            )

        # start() 已调用，执行队列必定初始化
        if self._queue is None:
            raise RuntimeError("WorkflowEngine 执行队列未初始化，请先调用 start()")

        # ── 参数校验 ──
        if not context.intent.strip():
            return SubmitError(
                code=SubmitRejectionCode.INVALID_PARAM,
                message="intent 不能为空",
            )

        # ── 创建任务标识 ──
        task_id = WorkflowId(uuid.uuid4().hex[:8])

        # ── 创建 Mailbox（先于 SubAgent——保证返回时已有队列可用）──
        mailbox: WorkflowMailbox = WorkflowMailbox(task_id)

        # ── 构造 SubAgent + 预创建 QUEUED 状态（不等待槽位）──
        sub_agent = SubAgent(
            infra=self._infra,
            hitl_notifier=self._hitl,
        )
        task_state = WorkflowState(
            task_id=task_id,
            status=WorkflowStatus.QUEUED,
            tools=context.tools,
            session_id=context.session_id,
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        sub_agent._task_state = task_state

        # ── 注册（_tasks/_mailboxes/_contexts 就位，QUEUED 对外可见）──
        self._tasks[task_id] = sub_agent
        self._mailboxes[task_id] = mailbox
        self._contexts[task_id] = context

        # ── 推 QUEUED 事件到 mailbox（排队可见）──
        push_mailbox_event(
            mailbox, WorkflowEventType.QUEUED, {"task_id": str(task_id)},
            caller="WorkflowEngine",
        )

        # ── 入队（有界，满则快速失败）──
        try:
            self._queue.put_nowait(QueuedItem(task_id, QueuedItemKind.SUBMIT))
        except asyncio.QueueFull:
            self._cleanup_task(task_id)
            return SubmitError(
                code=SubmitRejectionCode.QUEUE_FULL,
                message="任务队列已满，请稍后再试",
            )

        logger.info("WorkflowEngine.submit: task_id=%s, intent=%.60s...", task_id, context.intent)
        return mailbox

    async def _run_and_cleanup(self, sub_agent: SubAgent, task_id: WorkflowId) -> None:
        """执行新提交任务的包装——从注册表取 context/mailbox，处理异常 + 终态清理。

        从 submit() 闭包提取为方法（E2），供 _drain_queue 后台派发。
        context/mailbox 从 self._contexts/self._mailboxes 取参，不再闭包捕获。
        取参用 .get() 且移入 try——派发后、运行前被 cancel/stop 终态化清理时
        （_cleanup_task 弹出 _contexts/_mailboxes）缺失索引抛 KeyError 会跳过 finally
        导致槽位永久泄漏；此处缺失时早退，由 finally 兜底释放槽位。
        """
        try:
            context = self._contexts.get(task_id)
            mailbox = self._mailboxes.get(task_id)
            if context is None or mailbox is None:
                # 派发后、运行前已被 cancel/stop 清理——不再执行，finally 兜底释放槽位
                return
            await sub_agent.run(context, mailbox)
        except asyncio.CancelledError:
            if mailbox is not None:
                # stop() 触发的取消——推送 FAILED 后重新抛出，让 cancel 正确传播
                push_mailbox_event(
                    mailbox, WorkflowEventType.FAILED, {
                        "task_id": str(task_id),
                        "error": "任务被取消",
                        "error_type": WorkflowErrorCode.INTERNAL_ERROR.value,
                    },
                    caller="WorkflowEngine",
                )
            raise
        except Exception:
            logger.exception("WorkflowEngine: task_id=%s 执行异常", task_id)
            if mailbox is not None:
                # 补推送 FAILED 事件——SubAgent 在 run() 前期阶段
                # （Planner/Validator）抛异常时可能来不及推送，
                # WorkflowEngine 在此兜底，确保 mailbox 消费者不会永久阻塞
                push_mailbox_event(
                    mailbox, WorkflowEventType.FAILED, {
                        "task_id": str(task_id),
                        "error": "任务执行异常",
                        "error_type": WorkflowErrorCode.INTERNAL_ERROR.value,
                    },
                    caller="WorkflowEngine",
                )
        finally:
            # PAUSED 任务释放执行槽（E2 §5）——checkpoint/mailbox/SubAgent/注册表全部保留，
            # 等待 resume() 恢复执行。slot 释放不变式：每取得一次 semaphore 恰释放一次。
            if (sub_agent._task_state is not None and
                    sub_agent._task_state.status == WorkflowStatus.PAUSED):
                if self._semaphore:
                    self._semaphore.release()          # 释放执行槽，供 runner 派发下一个排队任务
                self._active.discard(task_id)          # slot 持有集合同步
                return                                 # checkpoint/mailbox/注册表保留
            self._cleanup_task(task_id)
            if self._semaphore:
                self._semaphore.release()              # 终态分支 release 先于 cleanup_storage（E3 §3.4）
            self._active.discard(task_id)
            try:
                await self._cleanup_storage(task_id, sub_agent)
            except Exception:
                logger.exception(
                    "WorkflowEngine: task_id=%s 存储清理失败，槽位仍会释放", task_id,
                )

    def _get_debug_snapshot(self) -> dict:
        """供 WorkflowDebugAPI 内部轮询使用。

        Returns:
            dict: 调试快照字典。
        """
        return query.get_debug_snapshot(
            self._tasks, self._mailboxes, self._max_sub_agents,
            active=self._active, queue=self._queue,
        )

    # ── 任务控制 ─────────────────────────────────────────────────────────

    async def pause(self, task_id: WorkflowId, timeout: float = 30.0) -> bool:
        """暂停任务——委托给 lifecycle.pause()。

        等待 orchestrator 确认 PAUSED 状态，带超时。

        Args:
            task_id: 要暂停的任务 ID。
            timeout: 等待确认的超时秒数。

        Returns:
            True:  暂停已确认
            False: 超时——orchestrator 未在 timeout 秒内到达检查点
        """
        return await lifecycle.pause(self._tasks, task_id, timeout=timeout)

    async def resume(self, task_id: WorkflowId) -> WorkflowMailbox | ResumeError:
        """从 PAUSED 恢复执行。

        幂等——已在 RUNNING/RESUMING 的任务直接返回 mailbox；resume_pending
        排队/恢复进行中重复 resume 短路返回同一 mailbox（不二次入队）。
        仅 PAUSED 状态经过 CAS 检查后进入实际恢复流程。
        E2 有界恢复：执行槽空闲 → 直接恢复；槽满 → 入队（保持 PAUSED，公平 FIFO）；
        队列满 → ResumeError(QUEUE_FULL) 快速失败。
        v1 限制：使用原始 DelegationContext，不支持注入新上下文。

        Args:
            task_id: 要恢复的任务 ID。

        Returns:
            WorkflowMailbox: 恢复成功，返回 mailbox 继续消费事件流。
            ResumeError: 恢复失败（任务不存在、状态不允许、队列满等）。
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
        if ts.status in (WorkflowStatus.RUNNING, WorkflowStatus.RESUMING):
            # 幂等——已经在跑了
            mailbox = self._mailboxes.get(task_id)
            if mailbox is not None:
                return mailbox
            return ResumeError(
                code=ResumeErrorCode.INVALID_STATE,
                message="任务正在运行但 mailbox 缺失，内部状态不一致",
            )
        if ts.status != WorkflowStatus.PAUSED:
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

        if ts.resume_pending:
            return mailbox                                  # 幂等短路——已在排队/恢复中

        if self._semaphore is None or self._queue is None:
            return ResumeError(
                code=ResumeErrorCode.INVALID_STATE,
                message="WorkflowEngine 尚未 start()，无法 resume",
            )

        if not self._semaphore.locked():                    # 槽空闲 → 直接恢复
            await self._semaphore.acquire()
            self._active.add(task_id)
            ts.resume_pending = True                        # 直接路径也置标记——防 spawn 后重复 resume 二次入队
            try:
                task = asyncio.create_task(self._run_resume(sub_agent, task_id))
            except Exception:
                self._active.discard(task_id)
                self._semaphore.release()
                ts.resume_pending = False
                raise
            self._task_futures[task_id] = task
        else:                                               # 槽满 → 入队（保持 PAUSED，公平 FIFO）
            ts.resume_pending = True
            try:
                self._queue.put_nowait(QueuedItem(task_id, QueuedItemKind.RESUME))
            except asyncio.QueueFull:
                ts.resume_pending = False                   # 入队失败清除标记
                return ResumeError(
                    code=ResumeErrorCode.QUEUE_FULL,
                    message="执行队列已满，请稍后再试",
                )
            push_mailbox_event(mailbox, WorkflowEventType.QUEUED, {"task_id": str(task_id)})  # 排队可见（§7）
        logger.info("WorkflowEngine.resume: task_id=%s 已恢复", task_id)
        return mailbox

    async def _run_resume(self, sub_agent: SubAgent, task_id: WorkflowId) -> None:
        """恢复暂停任务的包装——处理异常 + 终态清理。

        和 _run_and_cleanup 对称——resume 后的终态
        （COMPLETED/FAILED/CANCELLED）由此 finally 块负责清理。
        从 resume() 闭包提取为方法（E2），供 _drain_queue 后台派发。
        """
        if sub_agent._task_state is not None:
            sub_agent._task_state.resume_pending = False   # 统一清理点：已开始执行，清除排队标记
        mailbox = self._mailboxes.get(task_id)             # .get() + 移入 try——防派发后/运行前被清理 → KeyError 跳过 finally 泄漏槽位
        try:
            if mailbox is None:
                # 派发后、运行前已被 cancel/stop 清理——不再执行，finally 兜底释放槽位
                return
            await sub_agent.resume_from_pause()
        except asyncio.CancelledError:
            if mailbox is not None:
                push_mailbox_event(
                    mailbox, WorkflowEventType.FAILED, {
                        "task_id": str(task_id),
                        "error": "任务被取消",
                        "error_type": WorkflowErrorCode.INTERNAL_ERROR.value,
                    },
                    caller="WorkflowEngine",
                )
            raise
        except Exception:
            logger.exception("WorkflowEngine: task_id=%s resume 异常", task_id)
            if mailbox is not None:
                push_mailbox_event(
                    mailbox, WorkflowEventType.FAILED, {
                        "task_id": str(task_id),
                        "error": "resume 执行异常",
                        "error_type": WorkflowErrorCode.INTERNAL_ERROR.value,
                    },
                    caller="WorkflowEngine",
                )
        finally:
            # PAUSED 任务释放执行槽（与 _run_and_cleanup 对称，E2 §5）——resume 已 acquire，
            # 再次 PAUSED 时释放 slot + 移出 _active，checkpoint/mailbox/注册表保留。
            if (sub_agent._task_state is not None and
                    sub_agent._task_state.status == WorkflowStatus.PAUSED):
                if self._semaphore:
                    self._semaphore.release()          # 释放执行槽（resume 已 acquire，对称）
                self._active.discard(task_id)
                return                                 # checkpoint/mailbox/注册表保留
            self._cleanup_task(task_id)
            if self._semaphore:
                self._semaphore.release()
            self._active.discard(task_id)
            try:
                await self._cleanup_storage(task_id, sub_agent)
            except Exception:
                logger.exception(
                    "WorkflowEngine: task_id=%s resume 后存储清理失败", task_id,
                )

    async def cancel(self, task_id: WorkflowId) -> bool:
        """软取消任务——委托给 lifecycle.cancel()。

        PAUSED 分支的 semaphore 释放 + 清理已由 lifecycle.cancel() 内部完成。
        调用方不得再次释放。

        Args:
            task_id: 要取消的任务 ID。

        Returns:
            True: 取消信号已发送（幂等）。
        """
        return await lifecycle.cancel(
            self._tasks, self._mailboxes, self._semaphore,
            self._cleanup_task, self._cleanup_storage, task_id,
        )

    def submit_approval(
        self,
        task_id: WorkflowId,
        agent_id: AgentId,
        approved: bool,
        feedback: str | None = None,
    ) -> bool:
        """审批结果回传——委托给 lifecycle.submit_approval()。

        task_id / feedback 为 v2 预留，v1 不使用。

        Args:
            task_id: 任务 ID（v2 预留）。
            agent_id: 审批请求 ID。
            approved: 审批通过为 True，拒绝为 False。
            feedback: 审批反馈文本（v2 预留）。

        Returns:
            True: 审批已找到并处理；False: 审批不存在。
        """
        return lifecycle.submit_approval(
            self._infra.tool_registry, str(agent_id), approved,
        )

    # ── 查询（Pull 模式，供 CognitiveLoop + Debug WebUI）────────────────
    #
    # 注意：所有查询方法只在活跃任务（仍在 _tasks 注册表中的）上工作。
    # 任务终态清理后不可查询——需要上层（CognitiveLoop / Debug WebUI）
    # 缓存历史数据，或在 v2 引入 TaskSummaryStore 持久化。

    def get_task_status(self, task_id: WorkflowId) -> WorkflowStatus | None:
        """查询任务当前状态。最轻量——用户问"做完了吗？"时 LLM 调用。

        只查活跃任务（仍在 _tasks 注册表中）。任务终态清理后不可查询——
        此时返回 None，调用方应通过上层缓存获取历史状态。
        """
        return query.get_task_status(self._tasks, task_id)

    def get_task_mailbox(self, task_id: WorkflowId) -> WorkflowMailbox | None:
        """获取指定任务的 mailbox（供 Agent 轮询消费）。

        任务不存在或已清理时返回 None。
        """
        return query.get_task_mailbox(self._mailboxes, task_id)

    async def get_task_summary(self, task_id: WorkflowId) -> WorkflowSummary | None:
        """获取单个任务摘要。"""
        return await query.get_task_summary(self._tasks, task_id)

    async def list_active_tasks(self) -> list[WorkflowSummary]:
        """列出所有活跃任务。"""
        return await query.list_active_tasks(self._tasks)

    def get_progress(self, task_id: WorkflowId) -> WorkflowProgress | None:
        """查询任务执行进度。用户问"到哪一步了？"时 LLM 调用。

        返回当前执行节点、完成数/总数、耗时、预估剩余时间。
        任务不存在或尚未开始执行时返回 None。
        """
        return query.get_progress(self._tasks, task_id)

    async def get_task_dag(self, task_id: WorkflowId) -> WorkflowDAG | None:
        """获取任务的 DAG 可视化数据。"""
        return await query.get_task_dag(self._tasks, task_id)

    def get_token_usage(self, task_id: WorkflowId) -> TokenUsage | None:
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
        exec_state: "ExecutionState | None",
    ) -> list[str]:
        """从执行状态 + ArtifactStore 收集实际被调用的工具名。

        遍历已完成节点的 artifact_refs → ArtifactStore.get() →
        读取 metadata.tool_name，生成真实的工具审计列表。

        Args:
            sub_agent: SubAgent 实例。
            exec_state: 执行状态对象，None 时返回空列表。

        Returns:
            list[str]: 实际被调用的工具名列表（去重）。
        """
        return await query.collect_tools_used(sub_agent, exec_state)

    # ── 内部方法 ─────────────────────────────────────────────────────────

    def _cleanup_task(self, task_id: WorkflowId) -> None:
        """任务终态后清理注册表。

        调用方：_run_and_cleanup() finally 块、stop() 兜底。

        Args:
            task_id: 要清理的任务 ID。
        """
        self._tasks.pop(task_id, None)
        self._mailboxes.pop(task_id, None)
        self._contexts.pop(task_id, None)
        self._task_futures.pop(task_id, None)  # 允许 asyncio.Task 被 GC
        logger.info("WorkflowEngine: task_id=%s 注册表清理完成", task_id)

    async def _cleanup_storage(self, task_id: WorkflowId, sub_agent: SubAgent) -> None:
        """清理任务关联的 checkpoint 和 artifact 存储。

        必须在 _cleanup_task() 之后调用——存储清理可能涉及 I/O（v2），
        不阻塞注册表释放。

        Args:
            task_id: 要清理的任务 ID。
            sub_agent: SubAgent 实例，用于访问 checkpoint/artifact store。
        """
        if sub_agent._checkpoint_mgr:
            await sub_agent._checkpoint_mgr.delete(task_id)
        if sub_agent._artifact_store:
            await sub_agent._artifact_store.delete_by_task(task_id)
        logger.debug("WorkflowEngine: task_id=%s 存储清理完成", task_id)
