"""Orchestrator — Plan 驱动的执行循环。

严格按照 Plan 中的 DAG 拓扑推进节点：找 ready → 执行 → 评估 → 更新状态。
遇到结构性失败（DAG 走不通）时主动退出，返回 NEEDS_REPLAN，
由 Sub-Agent 层调 Replanner.replan() 后再恢复执行。

不调 Planner，不修改 Plan——只负责"在给定 Plan 的框架内尽最大努力推进"。

图操作委托给 PlanGraph（NetworkX），Orchestrator 不再手搓 DAG 遍历。

TaskState（任务级）归 SubAgent 管理，ExecutionState（执行层）归 Orchestrator 管理。
Orchestrator 读写 task_state.status 做终态转换，读写 exec_state 做循环内追踪。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from Module.task_system.core import execution, finalize

from Module.task_system.schema import (
    DAGNode,
    EvaluatorAction,
    ExecutionPhase,
    ExecutionState,
    NodeResult,
    NodeResultEvaluation,
    OrchestratorResult,
    OrchestratorRunStatus,
    TaskErrorCode,
    TaskEventType,
    TaskState,
    TaskStatus,
    TaskCheckpoint,
    SessionId,
    TaskId,
    Tool,
    NodeId,
    now_iso,
)

if TYPE_CHECKING:
    from Module.task_system.artifact_store import ArtifactStore
    from Module.task_system.checkpoint import CheckpointManager
    from Module.task_system.core.evaluator import Evaluator
    from Module.task_system.core.executor import Executor
    from Module.task_system.core.graph import PlanGraph
    from Module.task_system.infra import AgentInfra
    from Module.task_system.mailbox import TaskMailbox
    from Module.task_system.protocol import HITLNotifier

logger = logging.getLogger(__name__)


class Orchestrator:
    """Plan 驱动的执行循环。

    职责：
        1. 循环：找 ready nodes → Executor 执行 → Evaluator 评估 → 更新状态
        2. 处理 retry / approval / 局部容错（节点级）
        3. 每轮迭代后调 CheckpointManager 写快照
        4. 遇到结构性失败 → 返回 NEEDS_REPLAN，由 Sub-Agent 决策
        5. 终态时推送 completed/failed 到 Mailbox

    不负责：
        - 生成 Plan / Replan（Planner + Replanner —— Sub-Agent 层拥有）
        - 校验 Plan（PlanValidator —— Sub-Agent 层拥有）
        - 生成 DBT（Executor）
        - 校验输出（Evaluator —— 但 Orchestrator 调它）
        - 推送审批卡片（HITLNotifier —— 但 Orchestrator 决定"何时需要审批"）
        - 管理 TaskState 生命周期（SubAgent 拥有 TaskState）

    依赖（6 个）:
        infra, hitl_notifier, executor, evaluator, checkpoint_manager, artifact_store
        ↑ 注意：没有 Planner 和 PlanValidator —— 它们属于 Sub-Agent 层。
    """

    def __init__(  # noqa: PLR0913
        self,
        infra: AgentInfra,
        hitl_notifier: HITLNotifier,
        executor: Executor,
        evaluator: Evaluator,
        checkpoint_manager: CheckpointManager,
        artifact_store: ArtifactStore,
    ) -> None:
        self._infra = infra
        self._hitl = hitl_notifier
        self._executor = executor
        self._evaluator = evaluator
        self._checkpoint_mgr = checkpoint_manager
        self._artifact_store = artifact_store

        # 运行时状态——run() 入口创建，循环中原地更新。
        # None = Orchestrator 尚未启动（或已结束），非 None = 执行中。
        self._exec_state: ExecutionState | None = None

        # ── 扩展点（v1 不启用）─────────────────────────────────────────────
        # self._scheduler: Scheduler | None = None
        # self._event_log: EventLog | None = None

    # ── 主循环 ─────────────────────────────────────────────────────────────────

    async def run(
        self,
        task_state: TaskState,
        graph: PlanGraph,
        mailbox: TaskMailbox,
        resume_from: ExecutionState | None = None,
    ) -> OrchestratorResult:
        """驱动 PlanGraph 直到完成或遇到结构性障碍。

        run() 是薄编排层——每个步骤委托给私有方法。
        """
        task_id = task_state.task_id
        exec_state = self._init_exec(graph, resume_from, task_state)
        self._exec_state = exec_state
        logger.info("Orchestrator.run 开始: task_id=%s, nodes=%d", task_id, graph.node_count)

        try:
            while not task_state.is_terminal():
                exec_state.updated_at = now_iso()

                # 0. 中断信号检查（pause / cancel）
                if result := await self._check_interrupts(task_state, exec_state, task_id):
                    return result

                # 1. 找就绪节点
                ready_ids = graph.get_ready_nodes(exec_state.node_states)
                if not ready_ids:
                    if graph.is_terminal(exec_state.node_states):
                        break                         # 终态 → 跳出循环，走 COMPLETED
                    if result := await self._handle_no_ready(
                        task_state, exec_state, graph, task_id,
                    ):
                        return result                 # BLOCKED / NEEDS_REPLAN / FAILED

                # 2. 审批过滤（排除需审批节点，设 waiting 标志）
                ready_nodes = self._filter_approval(exec_state, graph, ready_ids)

                # 3. 执行就绪节点 → NodeResult
                exec_state.current_phase = ExecutionPhase.EXECUTING
                results = await self._execute_nodes(
                    ready_nodes, exec_state, task_id, task_state.tools,
                    task_state.session_id,
                )

                # 4. 评估 + 分发 → 根据 action 返回或继续
                exec_state.current_phase = ExecutionPhase.EVALUATING
                if result := await self._evaluate_and_dispatch(
                    task_state, exec_state, results, graph, mailbox,
                ):
                    return result                 # REPLAN / BLOCKED / ABORT

                # 5. 保存 checkpoint
                exec_state.current_phase = ExecutionPhase.EXECUTING
                await self._save_checkpoint(task_state, exec_state)

            # ── 终态：COMPLETED ──
            if task_state.status == TaskStatus.RUNNING:
                exec_state.current_phase = ExecutionPhase.FINALIZING
                await self._finalize(task_state, exec_state, mailbox)

            return OrchestratorResult(
                status=OrchestratorRunStatus.SUCCESS,
                checkpoint_state=self._build_checkpoint(task_state, exec_state),
            )

        except asyncio.CancelledError:
            logger.warning("Orchestrator: task_id=%s 被取消", task_id)
            task_state.status = TaskStatus.CANCELLED
            task_state.updated_at = now_iso()
            await self._save_checkpoint(task_state, exec_state)
            raise
        except Exception:
            logger.exception("Orchestrator 异常终止: task_id=%s", task_id)
            task_state.status = TaskStatus.FAILED
            task_state.updated_at = now_iso()
            await self._push_mailbox(mailbox, TaskEventType.FAILED, {
                "error": "Orchestrator 内部异常",
                "error_type": TaskErrorCode.INTERNAL_ERROR.value,
            })
            return OrchestratorResult(
                status=OrchestratorRunStatus.FAILED,
                error_context={"reason": "内部异常"},
                checkpoint_state=self._build_checkpoint(task_state, exec_state),
            )

    # ── run() 子步骤 ──────────────────────────────────────────────────────────

    def _init_exec(
        self,
        graph: PlanGraph,
        resume_from: ExecutionState | None,
        task_state: TaskState,
    ) -> ExecutionState:
        """初始化或合并 ExecutionState。复位审批标志 + RESUMING→RUNNING。"""
        if resume_from is not None:
            exec_state = self._merge_exec_state(resume_from, graph)
        else:
            exec_state = self._init_exec_state(graph)
        exec_state.waiting_for_approval = False
        if task_state.status == TaskStatus.RESUMING:
            task_state.status = TaskStatus.RUNNING
            task_state.updated_at = now_iso()
        return exec_state

    async def _check_interrupts(
        self,
        task_state: TaskState,
        exec_state: ExecutionState,
        task_id: TaskId,
    ) -> OrchestratorResult | None:
        """检查外部中断信号（pause / cancel）。返回结果或 None。"""
        if exec_state.pause_requested:
            logger.info("Orchestrator: task_id=%s 收到暂停请求", task_id)
            task_state.status = TaskStatus.PAUSED
            task_state.updated_at = now_iso()
            await self._save_checkpoint(task_state, exec_state)
            return OrchestratorResult(
                status=OrchestratorRunStatus.PAUSED,
                checkpoint_state=self._build_checkpoint(task_state, exec_state),
            )
        if exec_state.cancel_requested:
            logger.info("Orchestrator: task_id=%s 收到取消请求", task_id)
            task_state.status = TaskStatus.CANCELLED
            task_state.updated_at = now_iso()
            await self._save_checkpoint(task_state, exec_state)
            return OrchestratorResult(
                status=OrchestratorRunStatus.CANCELED,
                checkpoint_state=self._build_checkpoint(task_state, exec_state),
            )
        return None

    async def _handle_no_ready(
        self,
        task_state: TaskState,
        exec_state: ExecutionState,
        graph: PlanGraph,
        task_id: TaskId,
    ) -> OrchestratorResult | None:
        """无就绪节点且未到终态——判定阻塞/失败/死锁并返回结果。

        is_terminal 由 run() 外层 break 处理，不进入此方法。
        """
        if self._has_blocked_nodes(exec_state):
            task_state.status = TaskStatus.WAITING_APPROVAL
            task_state.updated_at = now_iso()
            await self._save_checkpoint(task_state, exec_state)
            return OrchestratorResult(
                status=OrchestratorRunStatus.BLOCKED,
                checkpoint_state=self._build_checkpoint(task_state, exec_state),
            )
        if graph.has_failed_required(exec_state.node_states):
            failed_info = graph.collect_failure_info(exec_state.node_states)
            await self._save_checkpoint(task_state, exec_state)
            return OrchestratorResult(
                status=OrchestratorRunStatus.NEEDS_REPLAN,
                error_context=failed_info,
                checkpoint_state=self._build_checkpoint(task_state, exec_state),
            )
        logger.error("Orchestrator 死锁: task_id=%s", task_id)
        task_state.status = TaskStatus.FAILED
        task_state.updated_at = now_iso()
        return OrchestratorResult(
            status=OrchestratorRunStatus.FAILED,
            error_context={"reason": "检测到死锁——无可执行节点但任务未完成"},
        )

    def _filter_approval(
        self,
        exec_state: ExecutionState,
        graph: PlanGraph,
        ready_ids: list[NodeId],
    ) -> list[DAGNode]:
        """审批检查 + 构建就绪节点列表。需要审批的节点排除并设 waiting 标志。"""
        ready_nodes: list[DAGNode] = []
        for nid in ready_ids:
            node = graph.nodes[nid]
            if self._needs_approval(node):
                exec_state.waiting_for_approval = True
                logger.warning(
                    "Orchestrator: node=%s 需要审批，跳过执行等待批准", node.node_id,
                )
                continue
            ready_nodes.append(node)
        return ready_nodes

    async def _evaluate_and_dispatch(
        self,
        task_state: TaskState,
        exec_state: ExecutionState,
        results: list[NodeResult],
        graph: PlanGraph,
        mailbox: TaskMailbox,
    ) -> OrchestratorResult | None:
        """评估每个 NodeResult → apply_evaluation → 根据 action 分发返回。"""
        for result in results:
            node = graph.nodes[result.node_id]
            evaluation = await self._evaluate_node(result, node, task_state)
            action = await self._apply_evaluation(
                task_state, exec_state, evaluation, mailbox, graph,
            )
            if action == EvaluatorAction.REPLAN:
                failed_info = graph.collect_failure_info(exec_state.node_states)
                await self._save_checkpoint(task_state, exec_state)
                return OrchestratorResult(
                    status=OrchestratorRunStatus.NEEDS_REPLAN,
                    error_context=failed_info,
                    checkpoint_state=self._build_checkpoint(task_state, exec_state),
                )
            elif action == EvaluatorAction.ASK_APPROVAL:
                task_state.status = TaskStatus.WAITING_APPROVAL
                task_state.updated_at = now_iso()
                await self._save_checkpoint(task_state, exec_state)
                return OrchestratorResult(
                    status=OrchestratorRunStatus.BLOCKED,
                    checkpoint_state=self._build_checkpoint(task_state, exec_state),
                )
            elif action == EvaluatorAction.ABORT:
                task_state.status = TaskStatus.FAILED
                task_state.updated_at = now_iso()
                await self._save_checkpoint(task_state, exec_state)
                return OrchestratorResult(
                    status=OrchestratorRunStatus.FAILED,
                    error_context={"reason": evaluation.reason},
                )
        return None

    # ── 状态初始化 ───────────────────────────────────────────────────────────────

    def _init_exec_state(self, graph: PlanGraph) -> ExecutionState:
        return finalize.init_exec_state(graph)

    def _merge_exec_state(
        self, resume_from: ExecutionState, graph: PlanGraph,
    ) -> ExecutionState:
        return finalize.merge_exec_state(resume_from, graph)

    # ── 阻塞判定 ──────────────────────────────────────────────────────────────

    def _has_blocked_nodes(self, exec_state: ExecutionState) -> bool:
        return finalize.has_blocked_nodes(exec_state)

    # ── 执行与评估 ────────────────────────────────────────────────────────────

    async def _execute_nodes(
        self,
        nodes: list[DAGNode],
        exec_state: ExecutionState,
        task_id: TaskId,
        tools: list[Tool],
        session_id: SessionId = SessionId(""),
    ) -> list[NodeResult]:
        return await execution.execute_nodes(
            self._executor, exec_state, nodes, task_id, tools, session_id,
        )

    async def _evaluate_node(
        self,
        result: NodeResult,
        node: DAGNode,
        task_state: TaskState,
    ) -> NodeResultEvaluation:
        return await execution.evaluate_node(
            self._evaluator, node, result, task_state.plan,
        )

    async def _apply_evaluation(
        self,
        task_state: TaskState,
        exec_state: ExecutionState,
        evaluation: NodeResultEvaluation,
        mailbox: TaskMailbox,
        graph: PlanGraph,
    ) -> EvaluatorAction:
        return execution.apply_evaluation(
            evaluation.node_id, evaluation, exec_state, graph,
            self._infra.constraints, task_state, mailbox,
        )

    # ── 终止处理 ───────────────────────────────────────────────────────────────

    async def _finalize(
        self,
        task_state: TaskState,
        exec_state: ExecutionState,
        mailbox: TaskMailbox,
    ) -> None:
        return await finalize.finalize(
            task_state, exec_state, mailbox, self._checkpoint_mgr,
        )

    # ── 审批判定 ────────────────────────────────────────────────────────────────

    def _needs_approval(self, node: DAGNode) -> bool:
        return finalize.needs_approval(node)

    # ── 工具方法 ───────────────────────────────────────────────────────────────

    def _build_checkpoint(
        self, task_state: TaskState, exec_state: ExecutionState,
    ) -> TaskCheckpoint:
        return finalize.build_checkpoint(task_state, exec_state)

    async def _save_checkpoint(
        self, task_state: TaskState, exec_state: ExecutionState,
    ) -> None:
        return await finalize.save_checkpoint(
            self._checkpoint_mgr, task_state, exec_state,
        )

    async def _push_mailbox(
        self,
        mailbox: TaskMailbox,
        event_type: TaskEventType,
        data: dict[str, Any],
    ) -> None:
        finalize.push_mailbox(mailbox, event_type, data)
