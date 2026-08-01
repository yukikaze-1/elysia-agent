"""Orchestrator — Plan 驱动的执行循环。

严格按照 Plan 中的 DAG 拓扑推进节点：找 ready → 执行 → 评估 → 更新状态。
遇到结构性失败（DAG 走不通）时主动退出，返回 NEEDS_REPLAN，
由 Sub-Agent 层调 Replanner.replan() 后再恢复执行。

不调 Planner，不修改 Plan——只负责"在给定 Plan 的框架内尽最大努力推进"。

图操作委托给 PlanGraph（NetworkX），Orchestrator 不再手搓 DAG 遍历。

WorkflowState（任务级）归 SubAgent 管理，ExecutionState（执行层）归 Orchestrator 管理。
Orchestrator 读写 task_state.status 做终态转换，读写 exec_state 做循环内追踪。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from Module.workflow_engine.core import execution, finalize

from Module.workflow_engine.schema import (
    DAGNode,
    EvaluatorAction,
    ExecutionPhase,
    ExecutionState,
    NodeResult,
    NodeResultEvaluation,
    OrchestratorResult,
    OrchestratorRunStatus,
    WorkflowErrorCode,
    WorkflowEventType,
    WorkflowState,
    WorkflowStatus,
    WorkflowCheckpoint,
    SessionId,
    WorkflowId,
    Tool,
    NodeId,
    now_iso,
)

if TYPE_CHECKING:
    from Module.workflow_engine.artifact_store import ArtifactStore
    from Module.workflow_engine.checkpoint import CheckpointManager
    from Module.workflow_engine.core.evaluator import Evaluator
    from Module.workflow_engine.core.executor import Executor
    from Module.workflow_engine.core.graph import PlanGraph
    from Module.workflow_engine.infra import AgentInfra
    from Module.workflow_engine.mailbox import WorkflowMailbox
    from Module.workflow_engine.protocol import HITLNotifier

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
        - 管理 WorkflowState 生命周期（SubAgent 拥有 WorkflowState）

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
        """初始化 Orchestrator。

        Args:
            infra: 代理基础设施（约束、工具注册等）。
            hitl_notifier: HITL 通知器，用于推送审批卡片。
            executor: 节点执行器（Executor），负责生成 DBT 并执行工具调用。
            evaluator: 节点评估器（Evaluator），负责校验节点输出。
            checkpoint_manager: 检查点管理器，负责读写快照。
            artifact_store: 产物存储，用于保存节点产出。
        """
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
        task_state: WorkflowState,
        graph: PlanGraph,
        mailbox: WorkflowMailbox,
        resume_from: ExecutionState | None = None,
    ) -> OrchestratorResult:
        """驱动 PlanGraph 直到完成或遇到结构性障碍。

        run() 是薄编排层——每个步骤委托给私有方法。

        Args:
            task_state: 任务级状态，由 Sub-Agent 层管理。
            graph: PlanGraph DAG 图，描述节点拓扑与依赖。
            mailbox: 任务邮箱，用于推送生命周期事件。
            resume_from: 可选的恢复快照；None 表示全新执行。

        Returns:
            OrchestratorResult：包含最终状态（SUCCESS/FAILED/PAUSED/
            CANCELED/BLOCKED/NEEDS_REPLAN）及可选的 checkpoint 和错误上下文。
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
            if task_state.status == WorkflowStatus.RUNNING:
                exec_state.current_phase = ExecutionPhase.FINALIZING
                await self._finalize(task_state, exec_state, mailbox)

            return OrchestratorResult(
                status=OrchestratorRunStatus.SUCCESS,
                checkpoint_state=self._build_checkpoint(task_state, exec_state),
            )

        except asyncio.CancelledError:
            logger.warning("Orchestrator: task_id=%s 被取消", task_id)
            task_state.status = WorkflowStatus.CANCELLED
            task_state.updated_at = now_iso()
            await self._save_checkpoint(task_state, exec_state)
            raise
        except Exception:
            logger.exception("Orchestrator 异常终止: task_id=%s", task_id)
            task_state.status = WorkflowStatus.FAILED
            task_state.updated_at = now_iso()
            await self._push_mailbox(mailbox, WorkflowEventType.FAILED, {
                "error": "Orchestrator 内部异常",
                "error_type": WorkflowErrorCode.INTERNAL_ERROR.value,
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
        task_state: WorkflowState,
    ) -> ExecutionState:
        """初始化或合并 ExecutionState。复位审批标志 + RESUMING→RUNNING。

        Args:
            graph: PlanGraph DAG 图。
            resume_from: 可选的恢复快照；None 表示全新初始化。
            task_state: 任务级状态，用于复位 RESUMMING → RUNNING。

        Returns:
            初始化或合并后的 ExecutionState。
        """
        if resume_from is not None:
            exec_state = self._merge_exec_state(resume_from, graph)
        else:
            exec_state = self._init_exec_state(graph)
        exec_state.waiting_for_approval = False
        if task_state.status == WorkflowStatus.RESUMING:
            task_state.status = WorkflowStatus.RUNNING
            task_state.updated_at = now_iso()
        return exec_state

    async def _check_interrupts(
        self,
        task_state: WorkflowState,
        exec_state: ExecutionState,
        task_id: WorkflowId,
    ) -> OrchestratorResult | None:
        """检查外部中断信号（pause / cancel）。返回结果或 None。

        Args:
            task_state: 任务级状态，用于写入 PAUSED/CANCELLED 终态。
            exec_state: 执行层状态，包含 pause_requested / cancel_requested 标志。
            task_id: 任务 ID，用于日志。

        Returns:
            若检测到中断信号返回对应的 OrchestratorResult，否则返回 None。
        """
        if exec_state.pause_requested:
            logger.info("Orchestrator: task_id=%s 收到暂停请求", task_id)
            task_state.status = WorkflowStatus.PAUSED
            task_state.updated_at = now_iso()
            await self._save_checkpoint(task_state, exec_state)
            return OrchestratorResult(
                status=OrchestratorRunStatus.PAUSED,
                checkpoint_state=self._build_checkpoint(task_state, exec_state),
            )
        if exec_state.cancel_requested:
            logger.info("Orchestrator: task_id=%s 收到取消请求", task_id)
            task_state.status = WorkflowStatus.CANCELLED
            task_state.updated_at = now_iso()
            await self._save_checkpoint(task_state, exec_state)
            return OrchestratorResult(
                status=OrchestratorRunStatus.CANCELED,
                checkpoint_state=self._build_checkpoint(task_state, exec_state),
            )
        return None

    async def _handle_no_ready(
        self,
        task_state: WorkflowState,
        exec_state: ExecutionState,
        graph: PlanGraph,
        task_id: WorkflowId,
    ) -> OrchestratorResult | None:
        """无就绪节点且未到终态——判定阻塞/失败/死锁并返回结果。

        is_terminal 由 run() 外层 break 处理，不进入此方法。

        Args:
            task_state: 任务级状态，用于写入 WAITING_APPROVAL / FAILED 终态。
            exec_state: 执行层状态，用于检查是否有被阻塞的节点。
            graph: PlanGraph DAG 图，用于检查是否有必需节点失败。
            task_id: 任务 ID，用于日志。

        Returns:
            若判定为阻塞/需要重规划/死锁则返回 OrchestratorResult，否则返回 None。
        """
        if self._has_blocked_nodes(exec_state):
            task_state.status = WorkflowStatus.WAITING_APPROVAL
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
        task_state.status = WorkflowStatus.FAILED
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
        """审批检查 + 构建就绪节点列表。需要审批的节点排除并设 waiting 标志。

        Args:
            exec_state: 执行层状态，用于设置 waiting_for_approval 标志。
            graph: PlanGraph DAG 图，用于按 node_id 查找 DAGNode。
            ready_ids: 当前就绪节点 ID 列表。

        Returns:
            过滤掉需审批节点后的 DAGNode 列表。
        """
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
        task_state: WorkflowState,
        exec_state: ExecutionState,
        results: list[NodeResult],
        graph: PlanGraph,
        mailbox: WorkflowMailbox,
    ) -> OrchestratorResult | None:
        """评估每个 NodeResult → apply_evaluation → 根据 action 分发返回。

        Args:
            task_state: 任务级状态，用于写入终态。
            exec_state: 执行层状态，传递给 apply_evaluation。
            results: 本轮执行的节点结果列表。
            graph: PlanGraph DAG 图，用于按 node_id 查找节点。
            mailbox: 任务邮箱，传递给 apply_evaluation。

        Returns:
            若某个节点的评估触发 REPLAN/BLOCKED/ABORT 则立即返回对应
            OrchestratorResult；所有节点正常通过则返回 None。
        """
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
                task_state.status = WorkflowStatus.WAITING_APPROVAL
                task_state.updated_at = now_iso()
                await self._save_checkpoint(task_state, exec_state)
                return OrchestratorResult(
                    status=OrchestratorRunStatus.BLOCKED,
                    checkpoint_state=self._build_checkpoint(task_state, exec_state),
                )
            elif action == EvaluatorAction.ABORT:
                task_state.status = WorkflowStatus.FAILED
                task_state.updated_at = now_iso()
                await self._save_checkpoint(task_state, exec_state)
                return OrchestratorResult(
                    status=OrchestratorRunStatus.FAILED,
                    error_context={"reason": evaluation.reason},
                )
        return None

    # ── 状态初始化 ───────────────────────────────────────────────────────────────

    def _init_exec_state(self, graph: PlanGraph) -> ExecutionState:
        """从 PlanGraph 初始化全新的 ExecutionState。

        Args:
            graph: PlanGraph DAG 图，用于提取节点信息。

        Returns:
            全新的 ExecutionState，所有节点状态为 PENDING。
        """
        return finalize.init_exec_state(graph)

    def _merge_exec_state(
        self, resume_from: ExecutionState, graph: PlanGraph,
    ) -> ExecutionState:
        """将恢复快照与当前 PlanGraph 合并，处理新增/删除节点。

        Args:
            resume_from: 从 checkpoint 恢复的执行状态快照。
            graph: 当前 PlanGraph DAG 图，可能与快照不匹配（replan 后）。

        Returns:
            合并后的 ExecutionState。
        """
        return finalize.merge_exec_state(resume_from, graph)

    # ── 阻塞判定 ──────────────────────────────────────────────────────────────

    def _has_blocked_nodes(self, exec_state: ExecutionState) -> bool:
        """检查是否有节点因等待审批而被阻塞。

        Args:
            exec_state: 执行层状态。

        Returns:
            True 表示存在被阻塞的节点（需要等待审批）。
        """
        return finalize.has_blocked_nodes(exec_state)

    # ── 执行与评估 ────────────────────────────────────────────────────────────

    async def _execute_nodes(
        self,
        nodes: list[DAGNode],
        exec_state: ExecutionState,
        task_id: WorkflowId,
        tools: list[Tool],
        session_id: SessionId = SessionId(""),
    ) -> list[NodeResult]:
        """执行一批就绪节点，生成 DBT 并调工具调用。

        Args:
            nodes: 待执行的 DAGNode 列表（已通过审批过滤）。
            exec_state: 执行层状态，用于追踪节点进度。
            task_id: 任务 ID。
            tools: 可用工具列表，传递给 Executor。
            session_id: 会话 ID，用于工具调用的上下文。

        Returns:
            节点执行结果列表（NodeResult）。
        """
        return await execution.execute_nodes(
            self._executor, exec_state, nodes, task_id, tools, session_id,
        )

    async def _evaluate_node(
        self,
        result: NodeResult,
        node: DAGNode,
        task_state: WorkflowState,
    ) -> NodeResultEvaluation:
        """对单个节点执行结果进行评估。

        Args:
            result: 节点执行结果。
            node: DAG 节点定义（含评估标准）。
            task_state: 任务级状态，提供 plan 上下文给评估器。

        Returns:
            节点结果评估（NodeResultEvaluation）。
        """
        return await execution.evaluate_node(
            self._evaluator, node, result, task_state.plan,
        )

    async def _apply_evaluation(
        self,
        task_state: WorkflowState,
        exec_state: ExecutionState,
        evaluation: NodeResultEvaluation,
        mailbox: WorkflowMailbox,
        graph: PlanGraph,
    ) -> EvaluatorAction:
        """将评估结果应用到执行状态，决定后续动作。

        Args:
            task_state: 任务级状态，用于记录重试/失败信息。
            exec_state: 执行层状态，用于更新节点状态机。
            evaluation: 节点结果评估。
            mailbox: 任务邮箱，用于推送事件（如审批请求）。
            graph: PlanGraph DAG 图，用于查找节点并更新拓扑。

        Returns:
            EvaluatorAction：RETRY / ASK_APPROVAL / REPLAN / ABORT / CONTINUE。
        """
        return execution.apply_evaluation(
            evaluation.node_id, evaluation, exec_state, graph,
            self._infra.constraints, task_state, mailbox,
        )

    # ── 终止处理 ───────────────────────────────────────────────────────────────

    async def _finalize(
        self,
        task_state: WorkflowState,
        exec_state: ExecutionState,
        mailbox: WorkflowMailbox,
    ) -> None:
        """任务正常完成，写终态并推送 COMPLETED 事件。

        Args:
            task_state: 任务级状态，写入 COMPLETED 终态。
            exec_state: 执行层状态，用于写入 FINALIZING 阶段。
            mailbox: 任务邮箱，用于推送 COMPLETED 事件。
        """
        return await finalize.finalize(
            task_state, exec_state, mailbox, self._checkpoint_mgr,
        )

    # ── 审批判定 ────────────────────────────────────────────────────────────────

    def _needs_approval(self, node: DAGNode) -> bool:
        """判断节点是否需要 HITL 审批。

        Args:
            node: DAG 节点定义。

        Returns:
            True 表示该节点执行前需要人工审批。
        """
        return finalize.needs_approval(node)

    # ── 工具方法 ───────────────────────────────────────────────────────────────

    def _build_checkpoint(
        self, task_state: WorkflowState, exec_state: ExecutionState,
    ) -> WorkflowCheckpoint:
        """构建任务检查点快照（纯数据，无 I/O）。

        Args:
            task_state: 任务级状态。
            exec_state: 执行层状态。

        Returns:
            任务检查点快照（WorkflowCheckpoint）。
        """
        return finalize.build_checkpoint(task_state, exec_state)

    async def _save_checkpoint(
        self, task_state: WorkflowState, exec_state: ExecutionState,
    ) -> None:
        """保存检查点快照到持久化存储。

        Args:
            task_state: 任务级状态。
            exec_state: 执行层状态。
        """
        return await finalize.save_checkpoint(
            self._checkpoint_mgr, task_state, exec_state,
        )

    async def _push_mailbox(
        self,
        mailbox: WorkflowMailbox,
        event_type: WorkflowEventType,
        data: dict[str, Any],
    ) -> None:
        """向任务邮箱推送生命周期事件。

        Args:
            mailbox: 任务邮箱实例。
            event_type: 事件类型（如 FAILED、COMPLETED）。
            data: 事件携带的数据字典。
        """
        finalize.push_mailbox(mailbox, event_type, data)
