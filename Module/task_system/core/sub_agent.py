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
    TaskErrorCode,
    TaskEventType,
    TaskId,
    TaskState,
    TaskStatus,
    ValidationAction,
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
        3. PlanValidator.validate() → 校验
        4. PlanGraph.from_plan(plan) → NetworkX DAG
        5. Orchestrator.run(task_state, exec_state, graph, mailbox) → 驱动执行
        6. 根据返回值决策：
            SUCCESS       → 终态
            NEEDS_REPLAN  → Planner.replan() → PlanGraph.apply_patch() → 回到 5
            BLOCKED       → 等待审批
            PAUSED        → 保存 checkpoint，等待 resume
            FAILED        → 终态
        """
        from Module.task_system.core.graph import PlanGraph

        task_id = mailbox.task_id

        # 0. 创建 TaskState
        task_state = TaskState(
            task_id=task_id,
            status=TaskStatus.PENDING,
            tools=context.tools,
            created_at=self._now(),
            updated_at=self._now(),
        )

        # 1. 初始规划
        task_state.status = TaskStatus.PLANNING
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
            if validation.action == ValidationAction.ABORT:
                logger.error(
                    "SubAgent: Plan 校验 abort task_id=%s: %s",
                    task_id, [i.message for i in validation.issues],
                )
                task_state.status = TaskStatus.FAILED
                await self._push_mailbox(mailbox, TaskEventType.FAILED, {
                    "error": f"Plan 校验失败: {[i.message for i in validation.issues]}",
                    "error_type": TaskErrorCode.PLAN_TOO_LARGE.value,
                })
                return
            # fix_plan: 重试规划
            logger.warning("SubAgent: Plan 校验失败，重试规划 task_id=%s", task_id)
            raise NotImplementedError("Plan 校验 retry 尚未实现")

        # 3. 构建 PlanGraph（NetworkX DAG）→ 存入 TaskState
        graph = PlanGraph.from_plan(plan)
        task_state.plan = graph.to_plan(plan)
        task_state.status = TaskStatus.RUNNING

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
                    await self._push_mailbox(mailbox, TaskEventType.FAILED, {
                        "error": meltdown_reason,
                        "error_type": TaskErrorCode.FAILURE_MELTDOWN.value,
                    })
                    return

                # Replan —— 调 Planner.replan()，产出 DAGPatch
                task_state.plan.plan_version += 1

                error_ctx = result.error_context
                logger.info(
                    "SubAgent: task_id=%s 触发 Replan, failed_nodes=%s",
                    task_state.task_id, error_ctx.get("failed_nodes", []),
                )

                plan = graph.to_plan(task_state.plan)
                patch = await self._planner.replan(
                    original_plan=plan,
                    failed_node_id=error_ctx.get("failed_nodes", [None])[0],
                    failure_diagnosis=error_ctx.get("reason", "节点执行失败"),
                    node_results=self._extract_node_results(result.checkpoint_state),
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
                logger.error(
                    "SubAgent: task_id=%s 失败: %s",
                    task_state.task_id,
                    result.error_context.get("reason", "未知"),
                )
                return

    def _extract_node_results(self, checkpoint_state: dict) -> dict:
        """从 checkpoint_state 中提取 node_states，供 Planner.replan() 使用。"""
        exec_dict = checkpoint_state.get("exec", {})
        node_states_raw: dict = exec_dict.get("node_states", {})
        # 提取已完成节点的 summary + error（供 Planner.replan() 参考）
        return {
            nid: {
                "summary": ns.get("summary", ""),
                "error": ns.get("error"),
            }
            for nid, ns in node_states_raw.items()
        }

    # ── 熔断检查 ───────────────────────────────────────────────────────────────

    def _check_meltdown(self, task_state: TaskState) -> str | None:
        """检查是否触发熔断。

        Returns:
            None → 可以 Replan
            str  → 熔断原因（不能 Replan）
        """
        constraints = self._infra.constraints

        if task_state.plan.plan_version >= constraints.max_failure_count:
            return (
                f"已 Replan {task_state.plan.plan_version} 次（上限 {constraints.max_failure_count}），"
                "已触发熔断"
            )

        # TODO: 检查剩余 token
        # TODO: 检查剩余时间（max_task_duration_s）
        return None

    # ── 工具方法 ───────────────────────────────────────────────────────────────

    @staticmethod
    async def _push_mailbox(
        mailbox: TaskMailbox, event_type: TaskEventType, data: dict,
    ) -> None:
        """推送事件到 TaskMailbox——委托 push_mailbox_event（mailbox.py 公共函数）。"""
        from Module.task_system.mailbox import push_mailbox_event
        push_mailbox_event(mailbox, event_type, data, caller="SubAgent")

    @staticmethod
    def _now() -> str:
        """返回当前时间 ISO 8601 字符串。"""
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()
