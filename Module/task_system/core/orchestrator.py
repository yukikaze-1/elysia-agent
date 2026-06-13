"""Orchestrator — Plan 驱动的执行循环。

严格按照 Plan 中的 DAG 拓扑推进节点：找 ready → 执行 → 评估 → 更新状态。
遇到结构性失败（DAG 走不通）时主动退出，返回 NEEDS_REPLAN，
由 Sub-Agent 层调 Planner.replan() 后再恢复执行。

不调 Planner，不修改 Plan——只负责"在给定 Plan 的框架内尽最大努力推进"。

图操作委托给 PlanGraph（NetworkX），Orchestrator 不再手搓 DAG 遍历。

TaskState（任务级）归 SubAgent 管理，ExecutionState（执行层）归 Orchestrator 管理。
Orchestrator 读写 task_state.status 做终态转换，读写 exec_state 做循环内追踪。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from Module.task_system.schema import (
    DAGNode,
    EvaluatorAction,
    ExecutionPhase,
    ExecutionState,
    ExecutionStatus,
    BusinessStatus,
    NodeId,
    NodeResult,
    NodeResultEvaluation,
    NodeState,
    NodeStatus,
    OrchestratorResult,
    OrchestratorRunStatus,
    TaskErrorCode,
    TaskState,
    TaskStatus,
    TaskId,
    Tool
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
        - 生成 Plan / Replan（Planner —— Sub-Agent 层拥有）
          TODO: v2 考虑拆分为 Planner（首次规划）和 Replanner（增量修复），
                两者输入/输出/LLM prompt 差异大，但 v1 均未实现，暂不拆。
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

        # ── 扩展点（v1 不启用）─────────────────────────────────────────────
        # self._scheduler: Scheduler | None = None
        # self._event_log: EventLog | None = None

    # ── 主循环 ─────────────────────────────────────────────────────────────────

    async def run(
        self,
        task_state: TaskState,
        graph: PlanGraph,
        mailbox: TaskMailbox,
    ) -> OrchestratorResult:
        """驱动 PlanGraph 直到完成或遇到结构性障碍。

        task_state: SubAgent 创建的任务级状态（Orchestrator 只读写 status）。
        exec_state 由 Orchestrator 内部从 graph 构建，调用方无需传入。

        SUCCESS  → 所有 required 节点完成。
        FAILED   → 不可恢复。
        NEEDS_REPLAN → 结构性失败，携带 error_context 和 checkpoint_state。
        BLOCKED  → 等待用户审批。
        PAUSED   → 收到外部暂停请求，已保存 checkpoint。

        Sub-Agent 收到 NEEDS_REPLAN 后调 Planner.replan()，得到 DAGPatch，
        应用到 PlanGraph 后可再次调用 run() 恢复执行。

        TODO: 增加 resume_from: ExecutionState | None = None 参数。
              从 checkpoint 恢复时 SubAgent 传入，Orchestrator 跳过 _init_exec_state。
        """
        task_id = task_state.task_id
        # TODO: if resume_from is not None: exec_state = resume_from
        #       else:
        exec_state = self._init_exec_state(graph)
        logger.info("Orchestrator.run 开始: task_id=%s, nodes=%d", task_id, graph.node_count)

        try:
            while not task_state.is_terminal():
                exec_state.updated_at = self._now()

                # 0. 检查外部中断信号
                if exec_state.pause_requested:
                    logger.info("Orchestrator: task_id=%s 收到暂停请求", task_id)
                    task_state.status = TaskStatus.PAUSED
                    await self._save_checkpoint(task_state, exec_state)
                    return OrchestratorResult(
                        status=OrchestratorRunStatus.PAUSED,
                        checkpoint_state=self._build_checkpoint(task_state, exec_state),
                    )

                # 1. 找就绪节点（PlanGraph 负责图遍历）
                ready_ids = graph.get_ready_nodes(exec_state.node_states)

                if not ready_ids:
                    if graph.is_terminal(exec_state.node_states):
                        break

                    if self._has_blocked_nodes(exec_state):
                        task_state.status = TaskStatus.WAITING_APPROVAL
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
                    return OrchestratorResult(
                        status=OrchestratorRunStatus.FAILED,
                        error_context={"reason": "检测到死锁——无可执行节点但任务未完成"},
                    )

                # 2. 审批检查（NodeId → DAGNode）
                ready_nodes: list[DAGNode] = []
                for nid in ready_ids:
                    node = graph.nodes.get(nid)
                    if node is None:
                        continue
                    ready_nodes.append(node)
                    if self._needs_approval(node):
                        exec_state.waiting_for_approval = True
                        logger.warning(
                            "Orchestrator: node=%s 需要审批，骨架阶段跳过",
                            node.node_id,
                        )

                # 3. 执行就绪节点（Executor 产出 NodeResult）
                exec_state.current_phase = ExecutionPhase.EXECUTING
                results = await self._execute_nodes(
                    ready_nodes, exec_state, task_state.task_id, task_state.tools,
                )

                # 4. 评估每个结果（Evaluator 包裹为 NodeResultEvaluation）
                exec_state.current_phase = ExecutionPhase.EVALUATING
                for result in results:
                    node = graph.nodes.get(result.node_id)
                    if node is None:
                        logger.error("Orchestrator: node=%s 在 graph 中不存在，跳过评估", result.node_id)
                        continue
                    evaluation = await self._evaluate_node(result, node, task_state)
                    action = await self._apply_evaluation(
                        task_state, exec_state, evaluation, mailbox, graph,
                    )

                    if action == "needs_replan":
                        failed_info = graph.collect_failure_info(exec_state.node_states)
                        await self._save_checkpoint(task_state, exec_state)
                        return OrchestratorResult(
                            status=OrchestratorRunStatus.NEEDS_REPLAN,
                            error_context=failed_info,
                            checkpoint_state=self._build_checkpoint(task_state, exec_state),
                        )
                    elif action == "blocked":
                        task_state.status = TaskStatus.WAITING_APPROVAL
                        await self._save_checkpoint(task_state, exec_state)
                        return OrchestratorResult(
                            status=OrchestratorRunStatus.BLOCKED,
                            checkpoint_state=self._build_checkpoint(task_state, exec_state),
                        )
                    elif action == "abort":
                        task_state.status = TaskStatus.FAILED
                        await self._save_checkpoint(task_state, exec_state)
                        return OrchestratorResult(
                            status=OrchestratorRunStatus.FAILED,
                            error_context={"reason": evaluation.reason},
                        )

                # 5. 写 checkpoint
                exec_state.current_phase = ExecutionPhase.EXECUTING
                await self._save_checkpoint(task_state, exec_state)

            # ── 终态 ──
            if task_state.status not in (TaskStatus.FAILED, TaskStatus.CANCELLED):
                exec_state.current_phase = ExecutionPhase.FINALIZING
                await self._finalize(task_state, exec_state, mailbox, graph)

            return OrchestratorResult(
                status=OrchestratorRunStatus.SUCCESS,
                checkpoint_state=self._build_checkpoint(task_state, exec_state),
            )

        except Exception:
            logger.exception("Orchestrator 异常终止: task_id=%s", task_id)
            task_state.status = TaskStatus.FAILED
            await self._push_mailbox(mailbox, "failed", {
                "error": "Orchestrator 内部异常",
                "error_type": TaskErrorCode.INTERNAL_ERROR.value,
            })
            return OrchestratorResult(
                status=OrchestratorRunStatus.FAILED,
                error_context={"reason": "内部异常"},
                checkpoint_state=self._build_checkpoint(task_state, exec_state),
            )

    # ── 状态初始化 ───────────────────────────────────────────────────────────────

    def _init_exec_state(self, graph: PlanGraph) -> ExecutionState:
        """从 PlanGraph 构建初始 ExecutionState。

        遍历所有 DAG 节点，为每个节点创建初始 NodeState（全部 PENDING）。
        新任务和 Replan 后均调用此方法重建。
        """
        node_states: dict[NodeId, NodeState] = {}
        for nid in graph.nodes:
            node_states[nid] = NodeState(node_id=nid)

        return ExecutionState(
            node_states=node_states,
            current_phase=ExecutionPhase.EXECUTING,
            updated_at=self._now(),
        )

    # ── 阻塞判定 ──────────────────────────────────────────────────────────────

    def _has_blocked_nodes(self, exec_state: ExecutionState) -> bool:
        """是否有节点因等待审批而阻塞。"""
        return exec_state.waiting_for_approval

    # ── 执行与评估 ────────────────────────────────────────────────────────────

    async def _execute_nodes(
        self,
        nodes: list[DAGNode],
        exec_state: ExecutionState,
        task_id: TaskId,
        tools: list[Tool],
    ) -> list[NodeResult]:
        """执行就绪节点——v1 逐个调用 Executor.execute()。

        对每个就绪节点：更新 NodeState → 调 Executor → 收集 NodeResult。
        v1 不做并发（顺序执行），v2 考虑 Worker Agent 池并发。
        NodeResult 仅包含 Executor 产出（ArtifactStore 引用 + 错误码 + 耗时），
        不含 Evaluator 判定——判定由 _evaluate_node 产出 NodeResultEvaluation。
        """
        results: list[NodeResult] = []
        for node in nodes:
            ns = exec_state.node_states[node.node_id]
            ns.status = NodeStatus.RUNNING
            ns.attempt += 1
            ns.started_at = self._now()

            logger.info("Orchestrator: 执行 node=%s attempt=%d", node.node_id, ns.attempt)
            result = await self._executor.execute(node, task_id, tools)
            # Executor 返回的 attempt=0，用 Orchestrator 追踪的实际 attempt 覆盖
            result.attempt = ns.attempt
            results.append(result)
        return results

    async def _evaluate_node(
        self,
        result: NodeResult,
        node: DAGNode,
        task_state: TaskState,
    ) -> NodeResultEvaluation:
        """评估单个节点结果——调 Evaluator 产出 NodeResultEvaluation。

        传入 DAGNode（含 output_contract）和 Plan（上下文），
        Evaluator 内部做双重判定：ExecutionStatus + BusinessStatus。
        """
        plan = task_state.plan
        if plan is None:
            # Plan 缺失时 fallback：直接 CONTINUE（不应发生，但防御性处理）
            logger.error("Orchestrator: node=%s Plan 为 None，无法评估", result.node_id)
            return NodeResultEvaluation(
                node_id=result.node_id,
                execution_status=ExecutionStatus.OK,
                business_status=BusinessStatus.GOAL_ACHIEVED,
                reason="[Plan 缺失，fallback]",
                suggested_action=EvaluatorAction.CONTINUE,
                result=result,
            )
        return await self._evaluator.evaluate(result, node, plan)

    async def _apply_evaluation(
        self,
        task_state: TaskState,
        exec_state: ExecutionState,
        evaluation: NodeResultEvaluation,
        mailbox: TaskMailbox,
        graph: PlanGraph,
    ) -> str:
        """根据 NodeResultEvaluation 更新 NodeState + 推送 Mailbox 事件。

        Returns:
            "continue" | "skip" | "retry" | "needs_replan" | "blocked" | "abort"
        """
        node_id = evaluation.node_id
        ns = exec_state.node_states[node_id]
        ns.finished_at = self._now()
        action = evaluation.suggested_action

        if action == EvaluatorAction.CONTINUE:
            ns.status = NodeStatus.SUCCESS
            ns.summary = evaluation.reason
            ns.artifact_refs = evaluation.result.artifact_refs
            await self._push_mailbox(mailbox, "progress", {
                "task_id": str(task_state.task_id),
                "node_id": node_id,
                "node_description": f"节点 {node_id} 完成",
                "percent": graph.compute_progress(exec_state.node_states),
                "message": ns.summary,
            })
            return "continue"

        elif action == EvaluatorAction.SKIP:
            ns.status = NodeStatus.SKIPPED
            ns.summary = evaluation.reason
            logger.info("Orchestrator: node=%s 跳过（非必需节点）", node_id)
            return "continue"

        elif action == EvaluatorAction.RETRY:
            ns.status = NodeStatus.PENDING
            ns.error = evaluation.reason
            ns.error_code = evaluation.result.error
            logger.info("Orchestrator: node=%s 评估 retry, attempt=%d", node_id, ns.attempt)
            return "retry"

        elif action == EvaluatorAction.REPLAN:
            ns.status = NodeStatus.FAILED
            ns.error = evaluation.reason
            ns.error_code = evaluation.result.error
            logger.info("Orchestrator: node=%s 需要 Replan, reason=%s", node_id, ns.error)
            return "needs_replan"

        elif action == EvaluatorAction.ASK_APPROVAL:
            exec_state.waiting_for_approval = True
            ns.status = NodeStatus.WAITING_APPROVAL
            return "blocked"

        elif action == EvaluatorAction.ABORT:
            ns.status = NodeStatus.FAILED
            ns.error = evaluation.reason
            ns.error_code = evaluation.result.error
            return "abort"

        return "continue"

    # ── 终止处理 ───────────────────────────────────────────────────────────────

    async def _finalize(
        self,
        task_state: TaskState,
        exec_state: ExecutionState,
        mailbox: TaskMailbox,
        graph: PlanGraph,
    ) -> None:
        """任务终态处理——推送 completed 事件 + 写最终 checkpoint。"""
        task_state.status = TaskStatus.COMPLETED
        await self._save_checkpoint(task_state, exec_state)

        completed_nodes = sum(
            1 for ns in exec_state.node_states.values()
            if ns.status == NodeStatus.SUCCESS
        )
        failed_nodes = sum(
            1 for ns in exec_state.node_states.values()
            if ns.status in (NodeStatus.FAILED, NodeStatus.UNRECOVERABLE)
        )

        await self._push_mailbox(mailbox, "completed", {
            "result": f"任务完成。{completed_nodes} 个节点成功"
                      + (f"，{failed_nodes} 个节点失败" if failed_nodes else ""),
            "summary": f"共 {len(exec_state.node_states)} 个节点，"
                       f"成功 {completed_nodes}，失败 {failed_nodes}",
        })
        logger.info(
            "Orchestrator: task_id=%s 完成。nodes: %d/%d success",
            task_state.task_id, completed_nodes, len(exec_state.node_states),
        )

    # ── 审批判定 ────────────────────────────────────────────────────────────────

    def _needs_approval(self, node: DAGNode) -> bool:
        """判断节点是否需要用户审批。

        TODO: 工具由 Executor 做 DBT 生成时匹配，DAGNode 不直接持有工具信息。
              审批判定需要 Executor 先回传工具名，或 Planner 在 DAGNode 上预先标注 risk_level。
        """
        return False

    # ── 工具方法 ───────────────────────────────────────────────────────────────

    def _build_checkpoint(
        self, task_state: TaskState, exec_state: ExecutionState,
    ) -> dict[str, Any]:
        """组合 TaskState + ExecutionState 为 checkpoint 快照。"""
        return {
            "task": task_state.model_dump(),
            "exec": exec_state.model_dump(),
        }

    async def _save_checkpoint(
        self, task_state: TaskState, exec_state: ExecutionState,
    ) -> None:
        """保存当前状态快照。"""
        checkpoint = self._build_checkpoint(task_state, exec_state)
        await self._checkpoint_mgr.save(task_state.task_id, checkpoint)

    async def _push_mailbox(
        self, mailbox: TaskMailbox, event_type: str, data: dict,
    ) -> None:
        """推送事件到 TaskMailbox。

        TODO: 调用 mailbox._push(TaskEvent(type=..., data=...))。
        """
        logger.debug(
            "Orchestrator: 推送 Mailbox type=%s data_keys=%s",
            event_type, list(data.keys()),
        )

    @staticmethod
    def _now() -> str:
        """返回当前时间 ISO 8601 字符串。"""
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()
