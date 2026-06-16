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

import asyncio
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
    TaskEventType,
    TaskState,
    TaskStatus,
    TaskCheckpoint,
    TaskId,
    Tool,
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

        task_state: SubAgent 创建的任务级状态（Orchestrator 只读写 status）。

        resume_from 用于 Replan 后恢复：
            - None（首次执行）→ 从 graph 全量初始化 ExecutionState
            - 非 None（Replan 后）→ 保留已成功节点的状态，移除不存在于新 graph
              的节点，新节点 PENDING，失败节点重置为 PENDING 重新执行

        SUCCESS  → 所有 required 节点完成。
        FAILED   → 不可恢复。
        NEEDS_REPLAN → 结构性失败，携带 error_context 和 checkpoint_state。
        BLOCKED  → 等待用户审批。
        PAUSED   → 收到外部暂停请求，已保存 checkpoint。

        Sub-Agent 收到 NEEDS_REPLAN 后调 Planner.replan()，得到 DAGPatch，
        应用到 PlanGraph 后可再次调用 run() 恢复执行。
        """
        task_id = task_state.task_id
        if resume_from is not None:
            exec_state = self._merge_exec_state(resume_from, graph)
        else:
            exec_state = self._init_exec_state(graph)
        self._exec_state = exec_state
        # 每次 run() 调用视为"审批已解决"边界，显式复位。
        # 设计假设：调用 run() 前审批已由外部（TaskSystem/SubAgent）处理完毕。
        # 若审批通过 → 继续执行；若拒绝 → 节点已标记 FAILED，不会重新进入审批。
        # ⚠️ 后续实现 resume 时需重新评估：
        #   - resume 的 checkpoint 可能包含 waiting_for_approval=True（审批中途暂停）
        #   - 但暂停的唯一路径是 PAUSE_REQUESTED，审批阻塞是 BLOCKED 退出 run()
        #   - BLOCKED 退出后 TaskSystem 等待外部审批回传，不会调 resume()
        #   - 所以当前假设在 v1 设计下成立，v2 审批风暴检测时重新审视
        exec_state.waiting_for_approval = False
        logger.info("Orchestrator.run 开始: task_id=%s, nodes=%d", task_id, graph.node_count)

        # 恢复执行——将瞬时 RESUMING 转为 RUNNING
        if task_state.status == TaskStatus.RESUMING:
            task_state.status = TaskStatus.RUNNING
            task_state.updated_at = now_iso()

        try:
            while not task_state.is_terminal():
                exec_state.updated_at = now_iso()

                # 0. 检查外部中断信号
                if exec_state.pause_requested:
                    logger.info("Orchestrator: task_id=%s 收到暂停请求", task_id)
                    task_state.status = TaskStatus.PAUSED
                    task_state.updated_at = now_iso()
                    await self._save_checkpoint(task_state, exec_state)
                    return OrchestratorResult(
                        status=OrchestratorRunStatus.PAUSED,
                        checkpoint_state=self._build_checkpoint(task_state, exec_state),
                    )

                # 0. 检查外部取消信号
                if exec_state.cancel_requested:
                    logger.info(
                        "Orchestrator: task_id=%s 收到取消请求", task_id,
                    )
                    task_state.status = TaskStatus.CANCELLED
                    task_state.updated_at = now_iso()
                    await self._save_checkpoint(task_state, exec_state)
                    return OrchestratorResult(
                        status=OrchestratorRunStatus.CANCELED,
                        checkpoint_state=self._build_checkpoint(
                            task_state, exec_state,
                        ),
                    )

                # 1. 找就绪节点（PlanGraph 负责图遍历）
                ready_ids = graph.get_ready_nodes(exec_state.node_states)

                if not ready_ids:
                    if graph.is_terminal(exec_state.node_states):
                        break

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

                    # v1 不可达：① is_terminal ② has_blocked_nodes ③ has_failed_required
                    # 已覆盖所有导致 ready 为空的情况。若到达此处 → 内部状态损坏的 bug，让 FAILED 暴露。
                    logger.error("Orchestrator 死锁: task_id=%s", task_id)
                    task_state.status = TaskStatus.FAILED
                    task_state.updated_at = now_iso()
                    return OrchestratorResult(
                        status=OrchestratorRunStatus.FAILED,
                        error_context={"reason": "检测到死锁——无可执行节点但任务未完成"},
                    )

                # 2. 审批检查 + 构建就绪节点列表
                ready_nodes: list[DAGNode] = []
                for nid in ready_ids:
                    # graph.nodes 与 graph._g.nodes 同步构建，nid 必在其中
                    node = graph.nodes[nid]
                    if self._needs_approval(node):
                        # 需要审批的节点排除在执行列表之外，等待用户批准
                        exec_state.waiting_for_approval = True
                        logger.warning(
                            "Orchestrator: node=%s 需要审批，跳过执行等待批准",
                            node.node_id,
                        )
                        continue
                    ready_nodes.append(node)

                # 3. 执行就绪节点（Executor 产出 NodeResult）
                exec_state.current_phase = ExecutionPhase.EXECUTING
                results = await self._execute_nodes(
                    ready_nodes, exec_state, task_state.task_id, task_state.tools,
                    task_state.session_id,
                )

                # 4. 评估每个结果（Evaluator 包裹为 NodeResultEvaluation）
                exec_state.current_phase = ExecutionPhase.EVALUATING
                for result in results:
                    # result 来自 _execute_nodes → ready_nodes → graph.nodes[nid]，
                    # node_id 必在 graph.nodes 中；不在说明内部状态损坏，让 KeyError 暴露
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

                # 5. 写 checkpoint
                exec_state.current_phase = ExecutionPhase.EXECUTING
                await self._save_checkpoint(task_state, exec_state)

            # ── 终态 ──
            # 显式检查 RUNNING：防止因 bug 导致非终态（WAITING_APPROVAL/PAUSED）被覆盖为 COMPLETED
            if task_state.status == TaskStatus.RUNNING:
                exec_state.current_phase = ExecutionPhase.FINALIZING
                await self._finalize(task_state, exec_state, mailbox)

            return OrchestratorResult(
                status=OrchestratorRunStatus.SUCCESS,
                checkpoint_state=self._build_checkpoint(task_state, exec_state),
            )

        except asyncio.CancelledError:
            logger.warning("Orchestrator: task_id=%s 被取消", task_id)
            task_state.status = TaskStatus.FAILED
            task_state.updated_at = now_iso()
            await self._push_mailbox(mailbox, TaskEventType.FAILED, {
                "task_id": str(task_id),
                "error": "任务被取消",
                "error_type": TaskErrorCode.INTERNAL_ERROR.value,
            })
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

    # ── 状态初始化 ───────────────────────────────────────────────────────────────

    def _init_exec_state(self, graph: PlanGraph) -> ExecutionState:
        """从 PlanGraph 构建初始 ExecutionState。

        遍历所有 DAG 节点，为每个节点创建初始 NodeState（全部 PENDING）。
        新任务首次执行时调用。Replan 恢复时用 _merge_exec_state 代替。
        """
        now = now_iso()
        node_states: dict[NodeId, NodeState] = {}
        for nid in graph.nodes:
            node_states[nid] = NodeState(node_id=nid)

        return ExecutionState(
            node_states=node_states,
            current_phase=ExecutionPhase.EXECUTING,
            created_at=now,
            updated_at=now,
        )

    def _merge_exec_state(
        self,
        old: ExecutionState,
        graph: PlanGraph,
    ) -> ExecutionState:
        """从旧 ExecutionState 合并到新 graph——Replan 后恢复执行。

        规则：
            1. 保留旧 graph 中存在节点的 SUCCESS / SKIPPED / PENDING 状态
            2. 移除旧 graph 中已不存在的节点（replan 删除的节点）
            3. 旧 graph 中不存在的节点 → 新建 PENDING
            4. FAILED / UNRECOVERABLE 节点 → 重置为 PENDING（replan 已处理，需重新执行）
        """
        now = now_iso()
        merged: dict[NodeId, NodeState] = {}

        for nid in graph.nodes:
            old_ns = old.node_states.get(nid)
            if old_ns is not None and old_ns.status in (
                NodeStatus.SUCCESS, NodeStatus.SKIPPED, NodeStatus.PENDING,
            ):
                # 已完成 / 已跳过 / 尚未开始 → 保留
                merged[nid] = old_ns
            else:
                # 新节点、失败节点、或旧状态不可用 → 新建 PENDING
                merged[nid] = NodeState(node_id=nid)

        return ExecutionState(
            node_states=merged,
            current_phase=ExecutionPhase.EXECUTING,
            created_at=old.created_at,
            updated_at=now,
        )

    # ── 阻塞判定 ──────────────────────────────────────────────────────────────

    def _has_blocked_nodes(self, exec_state: ExecutionState) -> bool:
        """是否有节点因等待审批而阻塞。

        v1 始终返回 False——waiting_for_approval 仅由 ASK_APPROVAL 设置，
        而 Evaluator v1 不产出 ASK_APPROVAL。v2 启用审批风暴检测后可达。
        """
        return exec_state.waiting_for_approval

    # ── 执行与评估 ────────────────────────────────────────────────────────────

    async def _execute_nodes(
        self,
        nodes: list[DAGNode],
        exec_state: ExecutionState,
        task_id: TaskId,
        tools: list[Tool],
        session_id: str = "",
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
            ns.started_at = now_iso()

            logger.info("Orchestrator: 执行 node=%s attempt=%d", node.node_id, ns.attempt)
            result = await self._executor.execute(node, task_id, tools, session_id)
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
            # Plan 缺失——严重内部错误，不应发生。直接 ABORT 而非静默成功，
            # 防止掩盖上游 bug（如 TaskState 未正确初始化）。
            logger.critical(
                "Orchestrator: node=%s Plan 为 None——这是严重 bug，任务中止",
                result.node_id,
            )
            return NodeResultEvaluation(
                node_id=result.node_id,
                execution_status=ExecutionStatus.ERROR,
                business_status=BusinessStatus.GOAL_FAILED,
                reason="Plan 缺失——内部错误",
                suggested_action=EvaluatorAction.ABORT,
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
    ) -> EvaluatorAction:
        """根据 NodeResultEvaluation 更新 NodeState + 推送 Mailbox 事件。

        返回 suggested_action 原始值，调用方据此决定是否退出主循环。
        REPLAN / ASK_APPROVAL / ABORT 三种动作触发退出，其余继续。
        """
        node_id = evaluation.node_id
        ns = exec_state.node_states[node_id]
        ns.finished_at = now_iso()
        ns.evaluation = evaluation  # 保留 Evaluator 完整判定，供 Planner.replan() 诊断用
        action = evaluation.suggested_action

        if action == EvaluatorAction.CONTINUE:
            ns.status = NodeStatus.SUCCESS
            ns.summary = evaluation.reason
            ns.artifact_refs = evaluation.result.artifact_refs
            # 计算当前节点是第几个完成的（已完成节点数 / 总节点数）
            # ns.status 已在上方设为 SUCCESS，所以直接统计 SUCCESS 数量即可
            completed_count = sum(
                1 for s in exec_state.node_states.values()
                if s.status in (NodeStatus.SUCCESS, NodeStatus.SKIPPED)
            )
            total_count = graph.node_count
            await self._push_mailbox(mailbox, TaskEventType.PROGRESS, {
                "task_id": str(task_state.task_id),
                "node_id": node_id,
                "node_description": f"节点 {node_id} 完成",
                "node_index": f"{completed_count}/{total_count}",
                "percent": graph.compute_progress(exec_state.node_states),
                "message": ns.summary,
            })

        elif action == EvaluatorAction.SKIP:
            # 防御性检查：required 节点不应被 SKIP——否则 is_terminal 永远 False，
            # has_failed_required 也不匹配 SKIPPED，导致死锁。
            if graph.nodes[node_id].required:
                logger.error(
                    "Orchestrator: required 节点 %s 收到 SKIP——降级为 REPLAN",
                    node_id,
                )
                ns.status = NodeStatus.FAILED
                ns.error = f"required 节点被建议 SKIP: {evaluation.reason}"
                return EvaluatorAction.REPLAN
            ns.status = NodeStatus.SKIPPED
            ns.summary = evaluation.reason
            # SKIP 也是进度——通知前端可选节点被跳过
            completed_count = sum(
                1 for s in exec_state.node_states.values()
                if s.status in (NodeStatus.SUCCESS, NodeStatus.SKIPPED)
            )
            total_count = graph.node_count
            await self._push_mailbox(mailbox, TaskEventType.PROGRESS, {
                "task_id": str(task_state.task_id),
                "node_id": node_id,
                "node_description": f"节点 {node_id} 跳过（非必需）",
                "node_index": f"{completed_count}/{total_count}",
                "percent": graph.compute_progress(exec_state.node_states),
                "message": ns.summary,
            })
            logger.info("Orchestrator: node=%s 跳过（非必需节点）", node_id)

        elif action == EvaluatorAction.RETRY:
            # 检查 retry 上限——node_id 必在 graph.nodes 中（_evaluate_node 已验证），
            # 若不在说明内部状态损坏，直接索引让 KeyError 暴露
            node = graph.nodes[node_id]
            max_retries = (
                node.max_retries
                if node.max_retries is not None
                else self._infra.constraints.max_retries_per_node
            )
            if ns.attempt > max_retries:
                logger.warning(
                    "Orchestrator: node=%s 重试次数已达上限 attempt=%d max=%d",
                    node_id, ns.attempt, max_retries,
                )
                ns.status = NodeStatus.FAILED
                ns.error = f"重试次数已达上限 ({max_retries})"
                ns.error_code = evaluation.result.error
                # 重试耗竭 → REPLAN，但先发 progress 告知前端
                completed_count = sum(
                    1 for s in exec_state.node_states.values()
                    if s.status in (NodeStatus.SUCCESS, NodeStatus.SKIPPED)
                )
                total_count = graph.node_count
                await self._push_mailbox(mailbox, TaskEventType.PROGRESS, {
                    "task_id": str(task_state.task_id),
                    "node_id": node_id,
                    "node_description": f"节点 {node_id} 重试耗尽——触发 Replan",
                    "node_index": f"{completed_count}/{total_count}",
                    "percent": graph.compute_progress(exec_state.node_states),
                    "message": ns.error,
                })
                return EvaluatorAction.REPLAN
            ns.status = NodeStatus.PENDING
            ns.error = evaluation.reason
            ns.error_code = evaluation.result.error
            # 为每次重试发 progress 事件——防止 Mailbox 在长重试链中沉默超时
            completed_count = sum(
                1 for s in exec_state.node_states.values()
                if s.status in (NodeStatus.SUCCESS, NodeStatus.SKIPPED)
            )
            total_count = graph.node_count
            await self._push_mailbox(mailbox, TaskEventType.PROGRESS, {
                "task_id": str(task_state.task_id),
                "node_id": node_id,
                "node_description": f"节点 {node_id} 重试中 ({ns.attempt}/{max_retries})",
                "node_index": f"{completed_count}/{total_count}",
                "percent": graph.compute_progress(exec_state.node_states),
                "message": ns.error,
            })
            logger.info("Orchestrator: node=%s 评估 retry, attempt=%d", node_id, ns.attempt)

        elif action == EvaluatorAction.REPLAN:
            ns.status = NodeStatus.FAILED
            ns.error = evaluation.reason
            ns.error_code = evaluation.result.error
            # REPLAN 也是推进——通知前端 DAG 即将被修复
            completed_count = sum(
                1 for s in exec_state.node_states.values()
                if s.status in (NodeStatus.SUCCESS, NodeStatus.SKIPPED)
            )
            total_count = graph.node_count
            await self._push_mailbox(mailbox, TaskEventType.PROGRESS, {
                "task_id": str(task_state.task_id),
                "node_id": node_id,
                "node_description": f"节点 {node_id} 失败——触发 Replan",
                "node_index": f"{completed_count}/{total_count}",
                "percent": graph.compute_progress(exec_state.node_states),
                "message": ns.error,
            })
            logger.info("Orchestrator: node=%s 需要 Replan, reason=%s", node_id, ns.error)

        elif action == EvaluatorAction.ASK_APPROVAL:
            exec_state.waiting_for_approval = True
            ns.status = NodeStatus.WAITING_APPROVAL

        elif action == EvaluatorAction.ABORT:
            ns.status = NodeStatus.FAILED
            ns.error = evaluation.reason
            ns.error_code = evaluation.result.error

        return action

    # ── 终止处理 ───────────────────────────────────────────────────────────────

    async def _finalize(
        self,
        task_state: TaskState,
        exec_state: ExecutionState,
        mailbox: TaskMailbox,
    ) -> None:
        """任务终态处理——推送 completed 事件 + 写最终 checkpoint。"""
        task_state.status = TaskStatus.COMPLETED
        task_state.updated_at = now_iso()
        await self._save_checkpoint(task_state, exec_state)

        completed_nodes = sum(
            1 for ns in exec_state.node_states.values()
            if ns.status in (NodeStatus.SUCCESS, NodeStatus.SKIPPED)
        )
        failed_nodes = sum(
            1 for ns in exec_state.node_states.values()
            if ns.status in (NodeStatus.FAILED, NodeStatus.UNRECOVERABLE)
        )
        total_nodes = len(exec_state.node_states)

        await self._push_mailbox(mailbox, TaskEventType.COMPLETED, {
            "task_id": str(task_state.task_id),
            "result": f"任务完成。{completed_nodes} 个节点成功"
                      + (f"，{failed_nodes} 个节点失败" if failed_nodes else ""),
            "summary": f"共 {total_nodes} 个节点，成功 {completed_nodes}，失败 {failed_nodes}",
            "total_nodes": total_nodes,
            "completed_nodes": completed_nodes,
            "failed_nodes": failed_nodes,
            "token_usage": task_state.token_usage,
            "token_total": sum(task_state.token_usage.values()),
        })
        logger.info(
            "Orchestrator: task_id=%s 完成。nodes: %d/%d success",
            task_state.task_id, completed_nodes, total_nodes,
        )

    # ── 审批判定 ────────────────────────────────────────────────────────────────

    def _needs_approval(self, node: DAGNode) -> bool:
        """判断节点是否需要用户审批——v1 始终返回 False。

        设计决策：
            v1 DAGNode 不持有工具信息，审批由 ToolManager 内部 SafetyPipeline 处理：
            Executor 调用 tool_registry.execute() → SafetyPipeline 发现需要审批
            → 内部暂停等待前端 → 用户批准/拒绝 → 返回结果。
            Orchestrator 完全不感知审批流程。

            v2 方案（审批风暴检测）：
            Planner 生成 DAGNode 时标注 risk_level（"safe" | "mutation"），
            Planner prompt 保守策略：涉及文件写、邮件发送、数据库修改的操作强制标注 mutation。
            Orchestrator 在执行前扫描所有 ready nodes → 汇总 mutation 步骤
            → 通过 HITLNotifier 批量推送审批预览卡片 → 用户一次性批准。
            这样无需拆 Executor 为两阶段（match + execute），也无需 DAGNode 持有 tool_name。

            当前 _needs_approval 只在审批检查循环中被调用，返回 True 时节点
            被排除在 ready_nodes 之外，由 wait_for_approval → BLOCKED 路径处理。
        """
        return False

    # ── 工具方法 ───────────────────────────────────────────────────────────────

    def _build_checkpoint(
        self, task_state: TaskState, exec_state: ExecutionState,
    ) -> TaskCheckpoint:
        """组合 TaskState + ExecutionState 为类型化 checkpoint 快照。"""
        return TaskCheckpoint(task=task_state, exec=exec_state)

    async def _save_checkpoint(
        self, task_state: TaskState, exec_state: ExecutionState,
    ) -> None:
        """保存当前状态快照。"""
        checkpoint = self._build_checkpoint(task_state, exec_state)
        await self._checkpoint_mgr.save(task_state.task_id, checkpoint)

    @staticmethod
    async def _push_mailbox(
        mailbox: TaskMailbox, event_type: TaskEventType, data: dict,
    ) -> None:
        """推送事件到 TaskMailbox——委托 push_mailbox_event（mailbox.py 公共函数）。"""
        from Module.task_system.mailbox import push_mailbox_event
        push_mailbox_event(mailbox, event_type, data, caller="Orchestrator")
