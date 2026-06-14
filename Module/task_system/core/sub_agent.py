"""Sub-Agent —— TaskSystem 池中的任务级执行单元。

接收 DelegationContext，负责：
    1. 初始规划（Planner.plan()）
    2. 校验（PlanValidator.validate()）
    3. 构建 PlanGraph（NetworkX DAG）
    4. 创建 TaskState（任务级）
    5. 驱动执行（Orchestrator.run()，内部自建 ExecutionState）
    6. 决策仲裁——Replan / 熔断 / 等待审批 / 暂停

一个用户任务一个 Sub-Agent。完成后 AgentState 丢弃，wrapper 归还池。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from Module.task_system.schema import (
    DelegationContext,
    ExecutionState,
    OrchestratorRunStatus,
    TaskCheckpoint,
    TaskErrorCode,
    TaskEventType,
    TaskId,
    TaskState,
    TaskStatus,
    ValidationAction,
    ValidationIssue,
    ValidationIssueType,
    NodeId,
    now_iso,
)

if TYPE_CHECKING:
    from Module.task_system.artifact_store import ArtifactStore
    from Module.task_system.checkpoint import CheckpointManager
    from Module.task_system.core.evaluator import Evaluator
    from Module.task_system.core.executor import Executor
    from Module.task_system.core.graph import PlanGraph
    from Module.task_system.core.orchestrator import Orchestrator
    from Module.task_system.core.planner import Planner
    from Module.task_system.core.plan_validator import PlanValidator
    from Module.task_system.infra import AgentInfra
    from Module.task_system.mailbox import TaskMailbox
    from Module.task_system.protocol import HITLNotifier

logger = logging.getLogger(__name__)


class SubAgent:
    """任务级执行单元。

    职责层次：
        Sub-Agent  → 决策层（规划 / Replan / 熔断 / 审批等待 / 暂停）
        Orchestrator → 执行层（按 Plan 推进 DAG，遇到结构性障碍时返回中断信号）

    TaskState 归 SubAgent 管理（任务生命周期），ExecutionState 归 Orchestrator（执行追踪）。
    两者合并为 checkpoint 持久化。

    依赖（8 个）:
        infra, hitl_notifier, planner, plan_validator, executor, evaluator,
        checkpoint_manager, artifact_store
        Orchestrator 由 SubAgent 内部自建（per-SubAgent）
    """

    def __init__(  # noqa: PLR0913
        self,
        infra: AgentInfra,
        hitl_notifier: HITLNotifier,
        planner: Planner,
        plan_validator: PlanValidator,
        executor: Executor,
        evaluator: Evaluator,
        checkpoint_manager: CheckpointManager,
        artifact_store: ArtifactStore,
    ) -> None:
        from Module.task_system.core.orchestrator import Orchestrator

        self._infra = infra
        self._hitl = hitl_notifier
        self._planner = planner
        self._plan_validator = plan_validator
        self._checkpoint_mgr = checkpoint_manager
        self._artifact_store = artifact_store

        # Orchestrator 是 per-SubAgent 的——面向单个 Plan 的执行循环
        # Executor/Evaluator 由 TaskSystem 共享注入
        self._orchestrator = Orchestrator(
            infra=infra,
            hitl_notifier=hitl_notifier,
            executor=executor,
            evaluator=evaluator,
            checkpoint_manager=checkpoint_manager,
            artifact_store=artifact_store,
        )

        # ── 扩展点（v1 不启用）─────────────────────────────────────────────
        # self._state_manager: StateManager | None = None
        # self._scheduler: Scheduler | None = None
        # self._event_log: EventLog | None = None
        # self._synthesizer: Synthesizer | None = None
        # self._reducer: Reducer | None = None

    # ── 主入口 ─────────────────────────────────────────────────────────────────

    async def run(
        self,
        context: DelegationContext,
        mailbox: TaskMailbox,
    ) -> None:
        """任务主入口——规划 → 构建 PlanGraph → 执行 → 决策循环。

        1. 创建 TaskState（任务级）
        2. Planner.plan() → Plan
        3. PlanValidator.validate() → 校验 + 构建 PlanGraph（返回 (result, graph)）
        4. graph.to_plan(plan) → 存入 TaskState
        5. Orchestrator.run(task_state, graph, mailbox) → 驱动执行
        6. 根据返回值决策：
            SUCCESS       → 终态
            NEEDS_REPLAN  → Planner.replan() → PlanGraph.apply_patch() → 回到 5
            BLOCKED       → 等待审批
            PAUSED        → 保存 checkpoint，等待 resume
            FAILED        → 终态
        """
        task_id = mailbox.task_id

        # 0. 创建 TaskState
        task_state = TaskState(
            task_id=task_id,
            status=TaskStatus.PENDING,
            tools=context.tools,
            created_at=now_iso(),
            updated_at=now_iso(),
        )

        # 1. 初始规划
        task_state.status = TaskStatus.PLANNING
        task_state.updated_at = now_iso()
        logger.info("SubAgent: 开始规划 task_id=%s", task_id)
        plan = await self._planner.plan(
            context=context,
            global_constraints=self._infra.constraints,
        )

        # 2. 校验（同时构建 PlanGraph，避免后续重复构造）
        validation, graph = self._plan_validator.validate(
            plan=plan,
            global_constraints=self._infra.constraints,
            task_constraints=context.task_constraints,
        )

        if not validation.valid:
            if validation.action == ValidationAction.ABORT:
                issue_messages = [i.message for i in validation.issues]
                logger.error(
                    "SubAgent: Plan 校验 abort task_id=%s: %s",
                    task_id, issue_messages,
                )
                task_state.status = TaskStatus.FAILED
                task_state.updated_at = now_iso()
                await self._push_mailbox(mailbox, TaskEventType.FAILED, {
                    "task_id": str(task_id),
                    "error": f"Plan 校验失败: {issue_messages}",
                    "error_type": self._abort_error_code(validation.issues),
                })
                return
            # fix_plan: 重试规划
            logger.warning("SubAgent: Plan 校验失败，重试规划 task_id=%s", task_id)
            raise NotImplementedError("Plan 校验 retry 尚未实现")

        # 3. 存入 TaskState（graph 由 validate 返回，无需重复构造）
        task_state.plan = graph.to_plan(plan)
        task_state.status = TaskStatus.RUNNING
        task_state.updated_at = now_iso()

        # 4. 执行-决策循环
        await self._run_loop(task_state, graph, mailbox)

    async def _run_loop(
        self,
        task_state: TaskState,
        graph: PlanGraph,
        mailbox: TaskMailbox,
    ) -> None:
        """执行-决策主循环。

        Orchestrator 内部自建 ExecutionState，SubAgent 不感知。
        大部分任务 Orchestrator 内部一次跑完，这个循环只跑一次。
        只在结构性失败（NEEDS_REPLAN）或审批阻塞（BLOCKED）时循环多次。
        """
        while True:
            result = await self._orchestrator.run(task_state, graph, mailbox)

            if result.status == OrchestratorRunStatus.SUCCESS:
                logger.info("SubAgent: task_id=%s 成功完成", task_state.task_id)
                return

            elif result.status == OrchestratorRunStatus.NEEDS_REPLAN:
                # 熔断检查
                meltdown_reason = self._check_meltdown(task_state)
                if meltdown_reason:
                    logger.warning(
                        "SubAgent: task_id=%s 熔断: %s",
                        task_state.task_id, meltdown_reason,
                    )
                    task_state.status = TaskStatus.FAILED
                    task_state.updated_at = now_iso()
                    await self._push_mailbox(mailbox, TaskEventType.FAILED, {
                        "error": meltdown_reason,
                        "error_type": TaskErrorCode.FAILURE_MELTDOWN.value,
                    })
                    return

                # Replan —— 调 Planner.replan()，产出 DAGPatch
                assert task_state.plan is not None, "NEEDS_REPLAN 时 plan 不应为 None"
                task_state.plan.plan_version += 1

                error_ctx = result.error_context
                logger.info(
                    "SubAgent: task_id=%s 触发 Replan, failed_nodes=%s",
                    task_state.task_id, error_ctx.get("failed_nodes", []),
                )

                # TODO: v1 Planner.replan() 尚未实现（抛 NotImplementedError）。
                #       当前 Orchestrator 可按 RETRY 超限 / required SKIP 等路径
                #       返回 NEEDS_REPLAN，到达此处会崩溃。v2 实现后移除此注释。
                checkpoint = result.checkpoint_state
                if checkpoint is None:
                    logger.error(
                        "SubAgent: task_id=%s NEEDS_REPLAN 但 checkpoint_state 为 None，"
                        "跳过 Replan", task_state.task_id,
                    )
                    task_state.status = TaskStatus.FAILED
                    task_state.updated_at = now_iso()
                    return
                plan = graph.to_plan(task_state.plan)
                failed_raw = (error_ctx.get("failed_nodes") or [None])[0]
                patch = await self._planner.replan(
                    original_plan=plan,
                    failed_node_id=failed_raw if failed_raw is not None else NodeId("unknown"),
                    failure_diagnosis=error_ctx.get("reason", "节点执行失败"),
                    node_results=self._extract_node_results(checkpoint),
                )

                # 应用 DAGPatch 到 PlanGraph
                graph.apply_patch(patch)
                task_state.plan = graph.to_plan(plan)
                logger.info(
                    "SubAgent: task_id=%s Replan 完成, patch_type=%s, nodes=%d",
                    task_state.task_id, patch.patch_type, graph.node_count,
                )
                # Orchestrator 下次 run() 时从新 graph 重建 ExecutionState
                continue

            elif result.status == OrchestratorRunStatus.BLOCKED:
                logger.info("SubAgent: task_id=%s 等待审批", task_state.task_id)
                await self._push_mailbox(mailbox, TaskEventType.APPROVAL_NEEDED, {
                    "task_id": str(task_state.task_id),
                    "message": "任务等待用户审批",
                })
                # Orchestrator 已将 task_state.status 设为 WAITING_APPROVAL。
                # 返回后由 TaskSystem 主类接管：
                #   1. 等待 HITLNotifier.submit_approval() 回调
                #   2. 审批通过后从 checkpoint 重建 exec_state → 继续 run()
                return

            elif result.status == OrchestratorRunStatus.PAUSED:
                if result.checkpoint_state is None:
                    logger.error(
                        "SubAgent: task_id=%s PAUSED 但 checkpoint_state 为 None",
                        task_state.task_id,
                    )
                    return
                logger.info(
                    "SubAgent: task_id=%s 收到暂停信号，已保存 checkpoint",
                    task_state.task_id,
                )
                await self._checkpoint_mgr.save(
                    task_state.task_id, result.checkpoint_state,
                )
                await self._push_mailbox(mailbox, TaskEventType.PAUSED, {
                    "task_id": str(task_state.task_id),
                    "message": "任务已暂停",
                })
                return

            elif result.status == OrchestratorRunStatus.FAILED:
                error_reason = result.error_context.get("reason", "未知")
                logger.error(
                    "SubAgent: task_id=%s 失败: %s",
                    task_state.task_id, error_reason,
                )
                await self._push_mailbox(mailbox, TaskEventType.FAILED, {
                    "task_id": str(task_state.task_id),
                    "error": error_reason,
                    "error_type": TaskErrorCode.INTERNAL_ERROR.value,
                })
                return

    def _extract_node_results(self, checkpoint: TaskCheckpoint) -> dict:
        """从 TaskCheckpoint 中提取 node_states，供 Planner.replan() 使用。

        遍历 checkpoint.exec.node_states，提取每个节点的 summary + error
        作为 Planner.replan() 的诊断输入。
        """
        return {
            str(nid): {
                "summary": ns.summary,
                "error": ns.error,
            }
            for nid, ns in checkpoint.exec.node_states.items()
        }

    # ── 熔断检查 ───────────────────────────────────────────────────────────────

    def _check_meltdown(self, task_state: TaskState) -> str | None:
        """检查是否触发熔断。

        plan_version 从 1 开始，每次 Replan 递增。用 > 比较意味着
        max_failure_count=5 时允许多达 5 次 Replan（version 2→6），
        version 7 触发熔断。

        Returns:
            None → 可以 Replan
            str  → 熔断原因（不能 Replan）
        """
        constraints = self._infra.constraints
        assert task_state.plan is not None, "熔断检查前 plan 必须已赋值"

        if task_state.plan.plan_version > constraints.max_failure_count:
            return (
                f"已 Replan {task_state.plan.plan_version - 1} 次（上限 {constraints.max_failure_count}），"
                "已触发熔断"
            )

        # TODO: 检查剩余 token
        # TODO: 检查剩余时间（max_task_duration_s）
        return None

    # ── 工具方法 ───────────────────────────────────────────────────────────────

    @staticmethod
    def _abort_error_code(issues: list[ValidationIssue]) -> str:
        """从校验问题列表中推导最合适的 TaskErrorCode。

        MAX_NODES_EXCEEDED / MAX_DEPTH_EXCEEDED → PLAN_TOO_LARGE，
        其余 fatal 类型回退 INTERNAL_ERROR。
        """
        fatal_types = {i.type for i in issues}
        if fatal_types & {ValidationIssueType.MAX_NODES_EXCEEDED, ValidationIssueType.MAX_DEPTH_EXCEEDED}:
            return TaskErrorCode.PLAN_TOO_LARGE.value
        return TaskErrorCode.INTERNAL_ERROR.value

    @staticmethod
    async def _push_mailbox(
        mailbox: TaskMailbox, event_type: TaskEventType, data: dict,
    ) -> None:
        """推送事件到 TaskMailbox——委托 push_mailbox_event（mailbox.py 公共函数）。"""
        from Module.task_system.mailbox import push_mailbox_event
        push_mailbox_event(mailbox, event_type, data, caller="SubAgent")
