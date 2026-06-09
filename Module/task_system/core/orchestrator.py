"""Orchestrator — Plan 驱动的执行循环。

严格按照 Plan 中的 DAG 拓扑推进节点：找 ready → 执行 → 评估 → 更新状态。
遇到结构性失败（DAG 走不通）时主动退出，返回 NEEDS_REPLAN，
由 Sub-Agent 层调 Planner.replan() 后再恢复执行。

不调 Planner，不修改 Plan——只负责"在给定 Plan 的框架内尽最大努力推进"。

图操作委托给 PlanGraph（NetworkX），Orchestrator 不再手搓 DAG 遍历。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from Module.task_system.schema import (
    DAGNode,
    NodeId,
    NodeResult,
    NodeState,
    NodeStatus,
    OrchestratorResult,
    Plan,
    RunStatus,
    TaskId,
    TaskState,
    TaskStatus,
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
        - 校验 Plan（PlanValidator —— Sub-Agent 层拥有）
        - 生成 DBT（Executor）
        - 校验输出（Evaluator —— 但 Orchestrator 调它）
        - 推送审批卡片（HITLNotifier —— 但 Orchestrator 决定"何时需要审批"）

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
        task_id: TaskId,
        graph: PlanGraph,
        mailbox: TaskMailbox,
    ) -> OrchestratorResult:
        """驱动 PlanGraph 直到完成或遇到结构性障碍。

        SUCCESS  → 所有 required 节点完成。
        FAILED   → 不可恢复。
        NEEDS_REPLAN → 结构性失败，携带 error_context 和 checkpoint_state。
        BLOCKED  → 等待用户审批。

        Sub-Agent 收到 NEEDS_REPLAN 后调 Planner.replan()，得到 DAGPatch，
        应用到 PlanGraph 后可再次调用 run() 恢复执行。
        """
        state = self._init_state(task_id, graph)
        logger.info("Orchestrator.run 开始: task_id=%s, nodes=%d", task_id, graph.node_count)

        try:
            while not state.is_terminal():
                state.updated_at = self._now()

                # 1. 找就绪节点（PlanGraph 负责图遍历）
                ready_ids = graph.get_ready_nodes(state.node_states)

                if not ready_ids:
                    if graph.is_terminal(state.node_states):
                        break

                    if self._has_blocked_nodes(state):
                        await self._save_checkpoint(state)
                        return OrchestratorResult(
                            status=RunStatus.BLOCKED,
                            checkpoint_state=state.model_dump(),
                        )

                    if graph.has_failed_required(state.node_states):
                        failed_info = graph.collect_failure_info(state.node_states)
                        await self._save_checkpoint(state)
                        return OrchestratorResult(
                            status=RunStatus.NEEDS_REPLAN,
                            error_context=failed_info,
                            checkpoint_state=state.model_dump(),
                        )

                    logger.error("Orchestrator 死锁: task_id=%s", task_id)
                    state.status = TaskStatus.FAILED
                    return OrchestratorResult(
                        status=RunStatus.FAILED,
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
                        state.waiting_for_user = True
                        logger.warning(
                            "Orchestrator: node=%s 需要审批，骨架阶段跳过",
                            node.node_id,
                        )

                # 3. 执行就绪节点
                state.current_phase = "executing"
                results = await self._execute_nodes(ready_nodes, state)

                # 4. 评估每个结果
                state.current_phase = "evaluating"
                for result in results:
                    evaluation = await self._evaluate_node(result, state)
                    action = await self._apply_evaluation(
                        state, result, evaluation, mailbox, graph,
                    )

                    if action == "needs_replan":
                        failed_info = graph.collect_failure_info(state.node_states)
                        await self._save_checkpoint(state)
                        return OrchestratorResult(
                            status=RunStatus.NEEDS_REPLAN,
                            error_context=failed_info,
                            checkpoint_state=state.model_dump(),
                        )
                    elif action == "blocked":
                        await self._save_checkpoint(state)
                        return OrchestratorResult(
                            status=RunStatus.BLOCKED,
                            checkpoint_state=state.model_dump(),
                        )
                    elif action == "abort":
                        state.status = TaskStatus.FAILED
                        await self._save_checkpoint(state)
                        return OrchestratorResult(
                            status=RunStatus.FAILED,
                            error_context={"reason": evaluation.get("reason", "评估判定终止")},
                        )

                # 5. 写 checkpoint
                state.current_phase = "executing"
                await self._save_checkpoint(state)

            # ── 终态 ──
            if state.status not in (TaskStatus.FAILED, TaskStatus.CANCELLED):
                state.current_phase = "finalizing"
                await self._finalize(state, mailbox, graph)

            return OrchestratorResult(
                status=RunStatus.SUCCESS,
                checkpoint_state=state.model_dump(),
            )

        except Exception:
            logger.exception("Orchestrator 异常终止: task_id=%s", task_id)
            state.status = TaskStatus.FAILED
            await self._push_mailbox(mailbox, "failed", {
                "error": "Orchestrator 内部异常",
                "error_type": "internal_error",
            })
            return OrchestratorResult(
                status=RunStatus.FAILED,
                error_context={"reason": "内部异常"},
                checkpoint_state=state.model_dump(),
            )

    # ── 状态初始化 ────────────────────────────────────────────────────────────

    def _init_state(self, task_id: TaskId, graph: PlanGraph) -> TaskState:
        """从 PlanGraph 构建初始 TaskState。

        同时导出 Plan 存入 TaskState（用于 checkpoint 序列化）。
        """
        node_states: dict[NodeId, NodeState] = {}
        for nid in graph.nodes:
            node_states[nid] = NodeState(node_id=nid)

        temp_plan = Plan(task_id=task_id)
        graph.to_plan(temp_plan)

        return TaskState(
            task_id=task_id,
            status=TaskStatus.RUNNING,
            plan=temp_plan,
            node_states=node_states,
            current_phase="executing",
            created_at=self._now(),
            updated_at=self._now(),
        )

    # ── 阻塞判定 ──────────────────────────────────────────────────────────────

    def _has_blocked_nodes(self, state: TaskState) -> bool:
        """是否有节点因等待审批而阻塞。"""
        return state.waiting_for_user

    # ── 执行与评估 ────────────────────────────────────────────────────────────

    async def _execute_nodes(
        self, nodes: list[DAGNode], state: TaskState,
    ) -> list[NodeResult]:
        """执行就绪节点——生成 DBT → 分配 Worker → 收集结果。

        TODO: 调用 Executor.generate_dbt() + Worker Agent 池执行。
        """
        results: list[NodeResult] = []
        for node in nodes:
            ns = state.node_states[node.node_id]
            ns.status = NodeStatus.RUNNING
            ns.attempt += 1
            ns.started_at = self._now()

            logger.info("Orchestrator: 骨架执行 node=%s", node.node_id)
            results.append(NodeResult(
                node_id=node.node_id,
                execution_status="ok",
                business_status="goal_achieved",
                evidence="[骨架占位]",
            ))
        return results

    async def _evaluate_node(
        self, result: NodeResult, state: TaskState,
    ) -> dict:
        """评估单个节点结果。

        TODO: 调用 Evaluator.evaluate(result, node, plan)。
        """
        return {"action": "continue", "reason": "[骨架占位]"}

    async def _apply_evaluation(
        self,
        state: TaskState,
        result: NodeResult,
        evaluation: dict,
        mailbox: TaskMailbox,
        graph: PlanGraph,
    ) -> str:
        """根据评估结果更新 NodeState + 推送 Mailbox 事件。

        Returns:
            "continue" | "retry" | "needs_replan" | "blocked" | "abort"
        """
        node_id = result.node_id
        ns = state.node_states[node_id]
        ns.finished_at = self._now()
        action = evaluation.get("action", "continue")

        if action == "continue":
            ns.status = NodeStatus.SUCCESS
            ns.summary = result.evidence
            ns.artifact_refs = result.artifact_refs
            await self._push_mailbox(mailbox, "progress", {
                "task_id": str(state.task_id),
                "node_id": node_id,
                "node_description": f"节点 {node_id} 完成",
                "percent": graph.compute_progress(state.node_states),
                "message": ns.summary,
            })
            return "continue"

        elif action == "retry":
            ns.status = NodeStatus.PENDING
            logger.info("Orchestrator: node=%s 评估 retry, attempt=%d", node_id, ns.attempt)
            return "retry"

        elif action == "replan":
            ns.status = NodeStatus.FAILED
            ns.error = evaluation.get("reason", "评估判定需要重新规划")
            logger.info("Orchestrator: node=%s 需要 Replan, reason=%s", node_id, ns.error)
            return "needs_replan"

        elif action == "ask_user":
            state.waiting_for_user = True
            ns.status = NodeStatus.RUNNING
            return "blocked"

        elif action == "abort":
            ns.status = NodeStatus.FAILED
            ns.error = evaluation.get("reason", "评估判定终止")
            return "abort"

        return "continue"

    # ── 终止处理 ───────────────────────────────────────────────────────────────

    async def _finalize(
        self, state: TaskState, mailbox: TaskMailbox, graph: PlanGraph,
    ) -> None:
        """任务终态处理——推送 completed 事件 + 写最终 checkpoint。"""
        state.status = TaskStatus.COMPLETED
        await self._save_checkpoint(state)

        completed_nodes = sum(
            1 for ns in state.node_states.values()
            if ns.status == NodeStatus.SUCCESS
        )
        failed_nodes = sum(
            1 for ns in state.node_states.values()
            if ns.status in (NodeStatus.FAILED, NodeStatus.UNRECOVERABLE)
        )

        await self._push_mailbox(mailbox, "completed", {
            "result": f"任务完成。{completed_nodes} 个节点成功"
                      + (f"，{failed_nodes} 个节点失败" if failed_nodes else ""),
            "summary": f"共 {len(state.node_states)} 个节点，"
                       f"成功 {completed_nodes}，失败 {failed_nodes}",
        })
        logger.info(
            "Orchestrator: task_id=%s 完成。nodes: %d/%d success",
            state.task_id, completed_nodes, len(state.node_states),
        )

    # ── 审批判定 ────────────────────────────────────────────────────────────────

    def _needs_approval(self, node: DAGNode) -> bool:
        """判断节点是否需要用户审批。

        TODO: 基于工具的 RBACLevel 判定。
        """
        return False

    # ── 工具方法 ───────────────────────────────────────────────────────────────

    async def _save_checkpoint(self, state: TaskState) -> None:
        """保存当前状态快照。

        TODO: 调用 CheckpointManager.save(state)。
        """
        logger.debug("Orchestrator: checkpoint 骨架跳过 task_id=%s", state.task_id)

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
