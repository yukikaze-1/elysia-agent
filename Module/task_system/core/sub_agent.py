"""Sub-Agent —— TaskSystem 池中的任务级执行单元。

接收 DelegationContext，负责：
    1. 初始规划（Planner.plan()）
    2. 校验（PlanValidator.validate()）
    3. 构建 PlanGraph（NetworkX DAG）
    4. 驱动执行（Orchestrator.run()）
    5. 决策仲裁——Replan / 熔断 / 等待审批

一个用户任务一个 Sub-Agent。完成后 AgentState 丢弃，wrapper 归还池。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from Module.task_system.schema import (
    DelegationContext,
    RunStatus,
    TaskErrorCode,
    TaskId,
)

if TYPE_CHECKING:
    from Module.task_system.artifact_store import ArtifactStore
    from Module.task_system.checkpoint import CheckpointManager
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
        Sub-Agent  → 决策层（规划 / Replan / 熔断 / 审批等待）
        Orchestrator → 执行层（按 Plan 推进 DAG，遇到结构性障碍时返回中断信号）

    依赖（7 个）:
        infra, hitl_notifier, planner, plan_validator, orchestrator,
        checkpoint_manager, artifact_store
    """

    def __init__(
        self,
        infra: AgentInfra,
        hitl_notifier: HITLNotifier,
        planner: Planner,
        plan_validator: PlanValidator,
        orchestrator: Orchestrator,
        checkpoint_manager: CheckpointManager,
        artifact_store: ArtifactStore,
    ) -> None:
        self._infra = infra
        self._hitl = hitl_notifier
        self._planner = planner
        self._plan_validator = plan_validator
        self._orchestrator = orchestrator
        self._checkpoint_mgr = checkpoint_manager
        self._artifact_store = artifact_store

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

        1. Planner.plan() → Plan
        2. PlanValidator.validate() → 校验
        3. PlanGraph.from_plan(plan) → NetworkX DAG
        4. Orchestrator.run(graph, mailbox) → 驱动执行
        5. 根据返回值决策：
            SUCCESS       → 终态
            NEEDS_REPLAN  → Planner.replan() → PlanGraph.apply_patch() → 回到 4
            BLOCKED       → 等待审批 → 回到 4
            FAILED        → 终态
        """
        from Module.task_system.core.graph import PlanGraph

        task_id = mailbox.task_id

        # 1. 初始规划
        logger.info("SubAgent: 开始规划 task_id=%s", task_id)
        plan = await self._planner.plan(
            context=context,
            global_constraints=self._infra.constraints,
        )

        # 2. 校验
        validation = self._plan_validator.validate(
            plan=plan,
            global_constraints=self._infra.constraints,
            task_constraints=context.task_constraints,
        )

        if not validation.valid:
            if validation.action == "abort":
                logger.error(
                    "SubAgent: Plan 校验 abort task_id=%s: %s",
                    task_id, [i.message for i in validation.issues],
                )
                # TODO: SubAgent.run() 需要返回结果类型，
                # 供 TaskSystem.submit() 转换后返回给 CognitiveLoop。
                await self._push_mailbox(mailbox, "failed", {
                    "error": f"Plan 校验失败: {[i.message for i in validation.issues]}",
                    "error_type": TaskErrorCode.PLAN_TOO_LARGE.value,
                })
                return
            # fix_plan: 重试规划
            logger.warning("SubAgent: Plan 校验失败，重试规划 task_id=%s", task_id)
            raise NotImplementedError("Plan 校验 retry 尚未实现")

        # 3. 构建 PlanGraph（NetworkX DAG）
        graph = PlanGraph.from_plan(plan)

        # 4. 执行-决策循环
        await self._run_loop(task_id, graph, mailbox)

    async def _run_loop(
        self,
        task_id: TaskId,
        graph: PlanGraph,
        mailbox: TaskMailbox,
    ) -> None:
        """执行-决策主循环。

        大部分任务 Orchestrator 内部一次跑完，这个循环只跑一次。
        只在结构性失败（NEEDS_REPLAN）或审批阻塞（BLOCKED）时循环多次。
        """
        while True:
            result = await self._orchestrator.run(task_id, graph, mailbox)

            if result.status == RunStatus.SUCCESS:
                logger.info("SubAgent: task_id=%s 成功完成", task_id)
                return

            elif result.status == RunStatus.NEEDS_REPLAN:
                # 熔断检查
                plan = graph.to_plan(None)  # 导出当前 Plan 用于熔断检查
                meltdown_reason = self._check_meltdown(plan)
                if meltdown_reason:
                    logger.warning(
                        "SubAgent: task_id=%s 熔断: %s", task_id, meltdown_reason,
                    )
                    await self._push_mailbox(mailbox, "failed", {
                        "error": meltdown_reason,
                        "error_type": TaskErrorCode.FAILURE_MELTDOWN.value,
                    })
                    return

                # Replan —— 调 Planner.replan()，产出 DAGPatch
                error_ctx = result.error_context
                logger.info(
                    "SubAgent: task_id=%s 触发 Replan, failed_nodes=%s",
                    task_id, error_ctx.get("failed_nodes", []),
                )

                patch = await self._planner.replan(
                    original_plan=plan,
                    failed_node_id=error_ctx.get("failed_nodes", [None])[0],
                    failure_diagnosis=error_ctx.get("reason", "节点执行失败"),
                    node_results={},  # TODO: 从 OrchestratorResult 恢复
                )

                # 应用 DAGPatch 到 PlanGraph
                graph.apply_patch(patch)
                logger.info(
                    "SubAgent: task_id=%s Replan 完成, patch_type=%s, nodes=%d",
                    task_id, patch.patch_type, graph.node_count,
                )
                continue

            elif result.status == RunStatus.BLOCKED:
                logger.info("SubAgent: task_id=%s 等待审批", task_id)
                # TODO: await approval_future
                return

            elif result.status == RunStatus.FAILED:
                logger.error(
                    "SubAgent: task_id=%s 失败: %s",
                    task_id, result.error_context.get("reason", "未知"),
                )
                return

    # ── 熔断检查 ───────────────────────────────────────────────────────────────

    def _check_meltdown(self, plan) -> str | None:
        """检查是否触发熔断。

        Returns:
            None → 可以 Replan
            str  → 熔断原因（不能 Replan）
        """
        constraints = self._infra.constraints

        if plan.replan_count >= constraints.max_failure_count:
            return (
                f"连续失败 {plan.replan_count} 次（上限 {constraints.max_failure_count}），"
                "已触发熔断"
            )

        # TODO: 检查剩余 token
        # TODO: 检查剩余时间（max_task_duration_s）
        return None

    # ── 工具方法 ───────────────────────────────────────────────────────────────

    async def _push_mailbox(
        self, mailbox: TaskMailbox, event_type: str, data: dict,
    ) -> None:
        """推送事件到 TaskMailbox。"""
        logger.debug(
            "SubAgent: 推送 Mailbox type=%s data_keys=%s",
            event_type, list(data.keys()),
        )
        # TODO: event = TaskEvent(type=event_type, data=data)
        # TODO: mailbox._push(event)
