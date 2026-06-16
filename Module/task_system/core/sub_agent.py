"""Sub-Agent —— TaskSystem 池中的任务级执行单元。

接收 DelegationContext，负责：
    1. 初始规划（Planner.plan()）
    2. 校验（PlanValidator.validate()）
    3. 构建 PlanGraph（NetworkX DAG）
    4. 创建 TaskState（任务级）
    5. 驱动执行（Orchestrator.run()，内部自建 ExecutionState）
    6. 决策仲裁——Replan / 熔断 / 等待审批 / 暂停

一个用户任务一个 Sub-Agent。完成后 AgentState 丢弃，wrapper 归还池。

结构：
    run()                       — 入口：组装 planning → execution pipeline
    ├─ _plan_and_validate()     — 规划 + 校验 → PlanGraph（或 ABORT/FIX_PLAN 降级）
    └─ _execute(context)        — 执行-决策循环（透传 DelegationContext 供 replan 使用）
        └─ _dispatch_result()   — 按 OrchestratorRunStatus 分发到 handler
            ├─ _handle_success()
            ├─ _handle_failed()
            ├─ _handle_blocked()
            ├─ _handle_paused()
            └─ _handle_replan()    — 熔断 + replan + DAGPatch + 构建 resume_state
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from Module.task_system.schema import (
    DAGPatch,
    DelegationContext,
    ExecutionState,
    NodeId,
    NodeState,
    OrchestratorResult,
    OrchestratorRunStatus,
    Plan,
    TaskCheckpoint,
    TaskErrorCode,
    TaskEventType,
    TaskId,
    TaskState,
    TaskStatus,
    ValidationAction,
    ValidationIssue,
    ValidationIssueType,
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
    from Module.task_system.token_ledger import TokenLedger

logger = logging.getLogger(__name__)


class SubAgent:
    """任务级执行单元。

    职责层次：
        Sub-Agent  → 决策层（规划 / Replan / 熔断 / 审批等待 / 暂停）
        Orchestrator → 执行层（按 Plan 推进 DAG，遇到结构性障碍时返回中断信号）

    TaskState 归 SubAgent 管理（任务生命周期），ExecutionState 归 Orchestrator（执行追踪）。
    两者合并为 checkpoint 持久化。

    跨任务共享依赖（2 个）：
        infra, hitl_notifier
    Per-task 组件（run() 内部创建）：
        TokenLedger / Planner / PlanValidator / Executor / Evaluator /
        ArtifactStore / CheckpointManager / Orchestrator
    """

    def __init__(
        self,
        infra: AgentInfra,
        hitl_notifier: HITLNotifier,
    ) -> None:
        """SubAgent 构造——只接收两个跨任务共享的资源。

        所有 per-task 组件（TokenLedger / Planner / Executor / Evaluator /
        ArtifactStore / CheckpointManager / PlanValidator / Orchestrator）
        在 run() 中创建——因为 TokenLedger 需要 task_state 作为构造参数。
        """
        self._infra = infra
        self._hitl = hitl_notifier

        # ── per-task 组件——run() 入口赋值，初始为 None ──
        self._planner: Planner | None = None
        self._plan_validator: PlanValidator | None = None
        self._executor: Executor | None = None
        self._evaluator: Evaluator | None = None
        self._orchestrator: Orchestrator | None = None
        self._checkpoint_mgr: CheckpointManager | None = None
        self._artifact_store: ArtifactStore | None = None
        self._token_ledger: TokenLedger | None = None

        # ── 运行时状态——run() 入口赋值，初始为 None ──
        # None = 尚未启动 / 已结束
        self._task_state: TaskState | None = None
        self._graph: PlanGraph | None = None

    # ── 主入口 ─────────────────────────────────────────────────────────────────

    async def run(
        self,
        context: DelegationContext,
        mailbox: TaskMailbox,
    ) -> None:
        """任务主入口——创建 TaskState → 规划 → 校验 → 执行-决策循环。

        1. 创建 TaskState（任务级）
        2. _plan_and_validate() → PlanGraph
        3. _execute() → 执行-决策循环
        """
        task_id = mailbox.task_id

        # 0. 创建 TaskState
        task_state = TaskState(
            task_id=task_id,
            status=TaskStatus.PENDING,
            tools=context.tools,
            session_id=context.session_id,
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        self._task_state = task_state

        # ── 创建 per-task 组件 ──
        from Module.task_system.artifact_store import ArtifactStore
        from Module.task_system.checkpoint import CheckpointManager
        from Module.task_system.core.evaluator import Evaluator
        from Module.task_system.core.executor import Executor
        from Module.task_system.core.orchestrator import Orchestrator
        from Module.task_system.core.planner import Planner
        from Module.task_system.core.plan_validator import PlanValidator
        from Module.task_system.token_ledger import TokenLedger

        self._token_ledger = TokenLedger(task_state)
        self._artifact_store = ArtifactStore()
        self._checkpoint_mgr = CheckpointManager()
        self._plan_validator = PlanValidator()
        self._planner = Planner(infra=self._infra, token_ledger=self._token_ledger)
        self._executor = Executor(
            infra=self._infra,
            artifact_store=self._artifact_store,
            hitl_notifier=self._hitl,
            token_ledger=self._token_ledger,
        )
        self._evaluator = Evaluator(
            infra=self._infra,
            artifact_store=self._artifact_store,
            token_ledger=self._token_ledger,
        )
        self._orchestrator = Orchestrator(
            infra=self._infra,
            hitl_notifier=self._hitl,
            executor=self._executor,
            evaluator=self._evaluator,
            checkpoint_manager=self._checkpoint_mgr,
            artifact_store=self._artifact_store,
        )

        # 1. 规划 + 校验 → PlanGraph
        graph = await self._plan_and_validate(context, task_state, mailbox)
        if graph is None:
            return  # 校验 ABORT，已在 _plan_and_validate 中处理
        self._graph = graph

        # 2. 执行-决策循环
        await self._execute(task_state, graph, mailbox, context)

    # ── 规划阶段 ─────────────────────────────────────────────────────────────

    async def _plan_and_validate(
        self,
        context: DelegationContext,
        task_state: TaskState,
        mailbox: TaskMailbox,
    ) -> PlanGraph | None:
        """规划 + 校验。

        Planner.plan() → PlanValidator.validate() → 构建 PlanGraph。
        校验通过 → 写入 task_state.plan，设 RUNNING，返回 PlanGraph。
        校验 ABORT → 设 FAILED，push 事件，返回 None。
        校验 FIX_PLAN → 抛 NotImplementedError（v1 不支持重试规划）。

        Returns:
            PlanGraph: 校验通过，调用方继续执行
            None:     校验失败且已处理，调用方应 return
        """
        task_id = task_state.task_id

        # 1. 初始规划
        task_state.status = TaskStatus.PLANNING
        task_state.updated_at = now_iso()
        logger.info("SubAgent: 开始规划 task_id=%s", task_id)
        assert self._planner is not None
        assert self._plan_validator is not None
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
                return None

            # fix_plan: 带错误反馈重试规划（v1 最多 2 次）
            issue_msgs = [i.message for i in validation.issues]
            for retry_n in range(1, 3):
                logger.warning(
                    "SubAgent: Plan 校验 fix_plan task_id=%s retry=%d: %s",
                    task_id, retry_n, issue_msgs,
                )
                # 把校验错误追加到 hints，让 Planner 知道哪里不对
                retry_context = DelegationContext(
                    intent=context.intent,
                    hints=(context.hints or "") + f"\n[上轮 Plan 校验失败: {'; '.join(issue_msgs)}]",
                    tools=context.tools,
                    summarized_context=context.summarized_context,
                    task_constraints=context.task_constraints,
                    session_id=context.session_id,
                    return_mode=context.return_mode,
                )
                plan = await self._planner.plan(
                    context=retry_context,
                    global_constraints=self._infra.constraints,
                )
                validation, graph = self._plan_validator.validate(
                    plan=plan,
                    global_constraints=self._infra.constraints,
                    task_constraints=context.task_constraints,
                )
                if validation.valid:
                    task_state.plan = graph.to_plan(plan)
                    task_state.status = TaskStatus.RUNNING
                    task_state.updated_at = now_iso()
                    logger.info(
                        "SubAgent: Plan 校验 fix_plan 修复成功 task_id=%s retry=%d",
                        task_id, retry_n,
                    )
                    return graph

            # 所有重试耗尽
            logger.error(
                "SubAgent: Plan 校验 fix_plan 耗尽 task_id=%s: %s",
                task_id, issue_msgs,
            )
            task_state.status = TaskStatus.FAILED
            task_state.updated_at = now_iso()
            await self._push_mailbox(mailbox, TaskEventType.FAILED, {
                "task_id": str(task_id),
                "error": f"Plan 校验失败且重试耗尽: {issue_msgs}",
                "error_type": self._abort_error_code(validation.issues),
            })
            return None

        # 3. 存入 TaskState（graph 由 validate 返回，无需重复构造）
        task_state.plan = graph.to_plan(plan)
        task_state.status = TaskStatus.RUNNING
        task_state.updated_at = now_iso()

        return graph

    # ── 执行-决策循环 ─────────────────────────────────────────────────────────

    async def _execute(
        self,
        task_state: TaskState,
        graph: PlanGraph,
        mailbox: TaskMailbox,
        context: DelegationContext,
    ) -> None:
        """执行-决策主循环。

        Orchestrator 内部自建 ExecutionState，SubAgent 不感知。
        大部分任务 Orchestrator 内部一次跑完，这个循环只跑一次。
        只在结构性失败（NEEDS_REPLAN）时循环多次。

        resume_from 在 Replan 后携带旧 ExecutionState，
        Orchestrator 合并后保留已成功节点的状态，避免重复执行。
        """
        resume_from: ExecutionState | None = None
        assert self._orchestrator is not None
        while True:
            result = await self._orchestrator.run(
                task_state, graph, mailbox, resume_from=resume_from,
            )
            should_continue, resume_from = await self._dispatch_result(
                result, task_state, graph, mailbox, context,
            )
            if not should_continue:
                return

    async def _dispatch_result(
        self,
        result: OrchestratorResult,
        task_state: TaskState,
        graph: PlanGraph,
        mailbox: TaskMailbox,
        context: DelegationContext,
    ) -> tuple[bool, ExecutionState | None]:
        """按 OrchestratorRunStatus 分发到对应 handler。

        Returns:
            (True, resume_state)  — 继续执行循环，resume_state 用于下次 run()
            (False, None)         — 终止循环
        """
        if result.status == OrchestratorRunStatus.SUCCESS:
            await self._handle_success(result, task_state)
            return False, None

        elif result.status == OrchestratorRunStatus.NEEDS_REPLAN:
            return await self._handle_replan(result, task_state, graph, mailbox, context)

        elif result.status == OrchestratorRunStatus.BLOCKED:
            await self._handle_blocked(result, task_state, mailbox)
            return False, None

        elif result.status == OrchestratorRunStatus.PAUSED:
            await self._handle_paused(result, task_state, mailbox)
            return False, None

        elif result.status == OrchestratorRunStatus.CANCELED:
            await self._handle_cancelled(result, task_state, mailbox)
            return False, None

        elif result.status == OrchestratorRunStatus.FAILED:
            await self._handle_failed(result, task_state, mailbox)
            return False, None

        else:
            logger.error(
                "SubAgent: task_id=%s 未知 OrchestratorRunStatus: %s",
                task_state.task_id, result.status,
            )
            return False, None

    # ── 结果处理：SUCCESS ─────────────────────────────────────────────────────

    async def _handle_success(
        self,
        result: OrchestratorResult,
        task_state: TaskState,
    ) -> None:
        """所有 required 节点完成——任务成功。"""
        logger.info("SubAgent: task_id=%s 成功完成", task_state.task_id)

    # ── 结果处理：FAILED ──────────────────────────────────────────────────────

    async def _handle_failed(
        self,
        result: OrchestratorResult,
        task_state: TaskState,
        mailbox: TaskMailbox,
    ) -> None:
        """不可恢复失败——推送 FAILED 事件。"""
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

    # ── 结果处理：CANCELED ────────────────────────────────────────────────────

    async def _handle_cancelled(
        self,
        result: OrchestratorResult,
        task_state: TaskState,
        mailbox: TaskMailbox,
    ) -> None:
        """外部取消——推送 CANCELLED 事件。

        Orchestrator 已将 task_state.status 设为 CANCELLED 并保存了 checkpoint。
        推送事件后退出执行循环，_run_and_cleanup finally 块负责清理。
        """
        logger.info("SubAgent: task_id=%s 已取消", task_state.task_id)
        await self._push_mailbox(mailbox, TaskEventType.CANCELLED, {
            "task_id": str(task_state.task_id),
            "reason": "用户取消",
        })

    # ── 结果处理：BLOCKED ─────────────────────────────────────────────────────

    async def _handle_blocked(
        self,
        result: OrchestratorResult,
        task_state: TaskState,
        mailbox: TaskMailbox,
    ) -> None:
        """等待用户审批——推送 APPROVAL_NEEDED 事件。

        Orchestrator 已将 task_state.status 设为 WAITING_APPROVAL。
        返回后由 TaskSystem 主类接管：
            1. 等待 HITLNotifier.submit_approval() 回调
            2. 审批通过后从 checkpoint 重建 exec_state → 继续 run()
        """
        logger.info("SubAgent: task_id=%s 等待审批", task_state.task_id)
        await self._push_mailbox(mailbox, TaskEventType.APPROVAL_NEEDED, {
            "task_id": str(task_state.task_id),
            "message": "任务等待用户审批",
        })

    # ── 结果处理：PAUSED ──────────────────────────────────────────────────────

    async def _handle_paused(
        self,
        result: OrchestratorResult,
        task_state: TaskState,
        mailbox: TaskMailbox,
    ) -> None:
        """收到暂停请求——保存 checkpoint，推送 PAUSED 事件。"""
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
        assert self._checkpoint_mgr is not None
        await self._checkpoint_mgr.save(
            task_state.task_id, result.checkpoint_state,
        )
        await self._push_mailbox(mailbox, TaskEventType.PAUSED, {
            "task_id": str(task_state.task_id),
            "message": "任务已暂停",
        })

    # ── 结果处理：NEEDS_REPLAN ────────────────────────────────────────────────

    async def _handle_replan(
        self,
        result: OrchestratorResult,
        task_state: TaskState,
        graph: PlanGraph,
        mailbox: TaskMailbox,
        context: DelegationContext,
    ) -> tuple[bool, ExecutionState | None]:
        """结构性失败——熔断检查 → Replan → DAGPatch 应用 → 构建 resume_state。

        Returns:
            (True, resume_state)  — replan 成功，DAG 已更新，resume_state 供下次 run()
            (False, None)         — 熔断 / checkpoint 缺失 / replan 未实现，已降级为 FAILED
        """
        # 1. 熔断检查
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
            return False, None

        # 2. Replan——调 Planner.replan()，产出 DAGPatch
        assert task_state.plan is not None, "NEEDS_REPLAN 时 plan 不应为 None"

        error_ctx = result.error_context
        failed_node_ids: list[NodeId] = error_ctx.get("failed_nodes", [])
        logger.info(
            "SubAgent: task_id=%s 触发 Replan, failed_nodes=%s",
            task_state.task_id, failed_node_ids,
        )

        checkpoint = result.checkpoint_state
        if checkpoint is None:
            logger.error(
                "SubAgent: task_id=%s NEEDS_REPLAN 但 checkpoint_state 为 None，"
                "跳过 Replan", task_state.task_id,
            )
            task_state.status = TaskStatus.FAILED
            task_state.updated_at = now_iso()
            await self._push_mailbox(mailbox, TaskEventType.FAILED, {
                "task_id": str(task_state.task_id),
                "error": "NEEDS_REPLAN 但 checkpoint 缺失，无法恢复",
                "error_type": TaskErrorCode.INTERNAL_ERROR.value,
            })
            return False, None

        # 调 Planner.replan()，产出 DAGPatch（局部修复）或 Plan（全量重建）
        try:
            plan = graph.to_plan(task_state.plan)
            # 解析生效约束——与 plan() 一致的 resolve 逻辑
            assert self._planner is not None
            tc = context.task_constraints.resolve(self._infra.constraints)
            replan_result = await self._planner.replan(
                original_plan=plan,
                failed_node_ids=failed_node_ids,
                failure_diagnosis=error_ctx.get("reason", "节点执行失败"),
                node_results=self._extract_node_results(checkpoint),
                intent=context.intent,
                tools=context.tools,
                max_nodes=tc.max_dag_nodes or self._infra.constraints.max_dag_nodes,
                max_mutations=tc.max_mutations or self._infra.constraints.max_mutations,
            )
        except NotImplementedError:
            # 优雅降级：replan 未实现 → 任务失败
            logger.warning(
                "SubAgent: task_id=%s NEEDS_REPLAN 但 Planner.replan() 尚未实现，"
                "优雅降级为 FAILED。failed_nodes=%s, reason=%s",
                task_state.task_id, failed_node_ids,
                error_ctx.get("reason", "未知"),
            )
            task_state.status = TaskStatus.FAILED
            task_state.updated_at = now_iso()
            await self._push_mailbox(mailbox, TaskEventType.FAILED, {
                "task_id": str(task_state.task_id),
                "error": (
                    f"节点 {failed_node_ids} 执行失败，需要 Replan，"
                    "但 Replan 尚未实现（v2 规划中）"
                ),
                "error_type": TaskErrorCode.INTERNAL_ERROR.value,
            })
            return False, None
        except Exception as e:
            # LLM 返回空响应 / JSON 解析失败 / 网络错误 → 降级为 FAILED
            logger.warning(
                "SubAgent: task_id=%s replan 调用异常: %s，降级为 FAILED",
                task_state.task_id, e,
            )
            task_state.status = TaskStatus.FAILED
            task_state.updated_at = now_iso()
            await self._push_mailbox(mailbox, TaskEventType.FAILED, {
                "task_id": str(task_state.task_id),
                "error": f"Replan 调用异常: {e}",
                "error_type": TaskErrorCode.INTERNAL_ERROR.value,
            })
            return False, None

        # ── 分发：Plan（全量重建）vs DAGPatch（局部修复）──────────────

        if isinstance(replan_result, Plan):
            # 全量重建路径——校验新 Plan → 新建 PlanGraph → 不复用旧状态
            logger.info(
                "SubAgent: task_id=%s Replan 全量重建, nodes=%d",
                task_state.task_id, len(replan_result.dag.nodes),
            )
            assert self._plan_validator is not None
            validation, new_graph = self._plan_validator.validate(
                plan=replan_result,
                global_constraints=self._infra.constraints,
                task_constraints=context.task_constraints,
            )
            if not validation.valid:
                issue_messages = [i.message for i in validation.issues]
                logger.error(
                    "SubAgent: task_id=%s Replan 全量重建校验失败: %s",
                    task_state.task_id, issue_messages,
                )
                task_state.status = TaskStatus.FAILED
                task_state.updated_at = now_iso()
                await self._push_mailbox(mailbox, TaskEventType.FAILED, {
                    "task_id": str(task_state.task_id),
                    "error": f"Replan 全量重建校验失败: {issue_messages}",
                    "error_type": TaskErrorCode.PLAN_TOO_LARGE.value,
                })
                return False, None

            # 全量重建——用新 graph 替换旧 graph 内部状态
            graph.replace(new_graph)
            self._graph = graph
            # 从旧 plan 继承版本号 + 1，避免全量重建绕过熔断计数器
            task_state.plan = new_graph.to_plan(replan_result)
            task_state.plan.plan_version = plan.plan_version + 1
            logger.info(
                "SubAgent: task_id=%s 全量重建完成, version=%d, nodes=%d",
                task_state.task_id, task_state.plan.plan_version,
                new_graph.node_count,
            )
            # 全量重建不复用旧执行状态——已成功节点的新 node_id 可能不同
            return True, None

        else:
            # 局部修复路径——应用 DAGPatch
            replan_patch: DAGPatch = replan_result
            graph.apply_patch(replan_patch)
            self._graph = graph

            # 校验 patch 后的图结构——LLM 可能引入环、重复 ID 等
            structural_issues = graph.validate_structure()
            if structural_issues:
                issue_msgs = [i.message for i in structural_issues]
                logger.error(
                    "SubAgent: task_id=%s Replan DAGPatch 引入结构问题: %s",
                    task_state.task_id, issue_msgs,
                )
                task_state.status = TaskStatus.FAILED
                task_state.updated_at = now_iso()
                await self._push_mailbox(mailbox, TaskEventType.FAILED, {
                    "task_id": str(task_state.task_id),
                    "error": f"Replan DAGPatch 校验失败: {issue_msgs}",
                    "error_type": TaskErrorCode.PLAN_TOO_LARGE.value,
                })
                return False, None

            task_state.plan = graph.to_plan(plan)
            # plan_version 在 replan 成功后递增——不在调用前递增，
            # 避免 NotImplementedError 降级时虚增版本号
            task_state.plan.plan_version += 1
            logger.info(
                "SubAgent: task_id=%s Replan 局部修复完成, patch_type=%s, version=%d, nodes=%d",
                task_state.task_id, replan_patch.patch_type,
                task_state.plan.plan_version, graph.node_count,
            )
            # 构建 resume_state：从旧 checkpoint 保留已成功节点状态，
            # Orchestrator._merge_exec_state 会处理新/删/失败节点的合并
            return True, checkpoint.exec

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

    # ── 内部：节点结果提取 ────────────────────────────────────────────────────

    def _extract_node_results(self, checkpoint: TaskCheckpoint) -> dict[str, NodeState]:
        """从 TaskCheckpoint 中提取 node_states，供 Planner.replan() 使用。

        NodeState 现包含 evaluation 字段（Evaluator 完整判定），
        Planner.replan() 可直接读取每个节点的：
            - status（SUCCESS/FAILED/SKIPPED/PENDING）
            - summary（简短摘要）
            - error / error_code
            - evaluation（NodeResultEvaluation：execution_status + business_status + suggested_action）
        """
        return {
            str(nid): ns
            for nid, ns in checkpoint.exec.node_states.items()
        }

    # ── 工具方法 ───────────────────────────────────────────────────────────────

    @staticmethod
    def _abort_error_code(issues: list[ValidationIssue]) -> str:
        """从校验问题列表中推导最合适的 TaskErrorCode。

        PLAN_TOO_LARGE：节点数/深度/结构问题（Planner 产出质量）
            - MAX_NODES_EXCEEDED, MAX_DEPTH_EXCEEDED（规模超限）
            - CYCLE, DUPLICATE_ID, MISSING_DEPENDENCY（结构不合法）
        INTERNAL_ERROR：未知/预期外的错误类型
        """
        plan_quality_types = {
            ValidationIssueType.MAX_NODES_EXCEEDED,
            ValidationIssueType.MAX_DEPTH_EXCEEDED,
            ValidationIssueType.CYCLE,
            ValidationIssueType.DUPLICATE_ID,
            ValidationIssueType.MISSING_DEPENDENCY,
        }
        fatal_types = {i.type for i in issues}
        if fatal_types & plan_quality_types:
            return TaskErrorCode.PLAN_TOO_LARGE.value
        return TaskErrorCode.INTERNAL_ERROR.value

    @staticmethod
    async def _push_mailbox(
        mailbox: TaskMailbox, event_type: TaskEventType, data: dict,
    ) -> None:
        """推送事件到 TaskMailbox——委托 push_mailbox_event（mailbox.py 公共函数）。"""
        from Module.task_system.mailbox import push_mailbox_event
        push_mailbox_event(mailbox, event_type, data, caller="SubAgent")
