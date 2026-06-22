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
    ├─ _plan_and_validate()     — 委托到 planning.py
    └─ _execute()               — 委托到 execution_loop.py
        └─ _dispatch_result()   — 按 OrchestratorRunStatus 分发（保留在类中）
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from Module.task_system.schema import (
    DelegationContext,
    ExecutionState,
    NodeState,
    OrchestratorResult,
    OrchestratorRunStatus,
    TaskCheckpoint,
    TaskErrorCode,
    TaskEventType,
    TaskState,
    TaskStatus,
    ValidationIssue,
    ValidationIssueType,
    now_iso,
)

from Module.task_system.core.sub_agent import (
    planning,
    execution_loop,
    result_handlers,
    replan,
)

if TYPE_CHECKING:
    from Module.task_system.artifact_store import ArtifactStore
    from Module.task_system.checkpoint import CheckpointManager
    from Module.task_system.core.evaluator import Evaluator
    from Module.task_system.core.executor import Executor
    from Module.task_system.core.graph import PlanGraph
    from Module.task_system.core.orchestrator import Orchestrator
    from Module.task_system.core.planner import Planner, Replanner
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
        TokenLedger / Planner / Replanner / PlanValidator / Executor /
        Evaluator / ArtifactStore / CheckpointManager / Orchestrator
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
        self._replanner: Replanner | None = None
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
        self._context: DelegationContext | None = None
        self._mailbox: TaskMailbox | None = None

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
        self._context = context   # resume 时需要 intent/tools/constraints
        self._mailbox = mailbox   # resume 时复用同一个 mailbox

        # ── 创建 per-task 组件 ──
        from Module.task_system.artifact_store import ArtifactStore
        from Module.task_system.checkpoint import CheckpointManager
        from Module.task_system.core.evaluator import Evaluator
        from Module.task_system.core.executor import Executor
        from Module.task_system.core.orchestrator import Orchestrator
        from Module.task_system.core.planner import Planner, Replanner
        from Module.task_system.core.plan_validator import PlanValidator
        from Module.task_system.token_ledger import TokenLedger

        self._token_ledger = TokenLedger(task_state)
        self._artifact_store = ArtifactStore()
        self._checkpoint_mgr = CheckpointManager()
        self._plan_validator = PlanValidator()
        self._planner = Planner(infra=self._infra, token_ledger=self._token_ledger)
        self._replanner = Replanner(infra=self._infra, token_ledger=self._token_ledger)
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

    # ── 恢复 ─────────────────────────────────────────────────────────────────

    async def resume_from_pause(self) -> None:
        """从 PAUSED 恢复——CAS 检查 → 加载 checkpoint → _execute。

        Raises:
            RuntimeError: 非 PAUSED 状态 / checkpoint 缺失 / graph 缺失
        """
        ts = self._task_state
        if ts is None or ts.status != TaskStatus.PAUSED:
            raise RuntimeError(
                f"task_id={self._mailbox.task_id if self._mailbox else 'N/A'} 非 PAUSED 状态"
                f"（{ts.status if ts else 'None'}），无法 resume"
            )

        ts.status = TaskStatus.RESUMING
        ts.updated_at = now_iso()

        assert self._checkpoint_mgr is not None, "run() 必须先于 resume_from_pause()"
        assert self._mailbox is not None, "run() 必须先于 resume_from_pause()"
        checkpoint = await self._checkpoint_mgr.load(self._mailbox.task_id)
        if checkpoint is None:
            ts.status = TaskStatus.FAILED
            ts.updated_at = now_iso()
            raise RuntimeError("checkpoint 缺失，无法恢复")

        if self._graph is None:
            ts.status = TaskStatus.FAILED
            ts.updated_at = now_iso()
            raise RuntimeError("graph 缺失，无法恢复")

        assert self._context is not None, "run() 必须先于 resume_from_pause()"

        # 清零中断标志
        checkpoint.exec.pause_requested = False
        checkpoint.exec.cancel_requested = False

        mailbox = self._mailbox
        assert mailbox is not None, "run() 必须先于 resume_from_pause()"
        await self._execute(
            ts, self._graph, mailbox, self._context,
            resume_from=checkpoint.exec,
        )

    # ── _dispatch_result 保留在类中（42 行 if/elif 不提取） ─────────────────

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
            (True, resume_state)  — 继续执行循环
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

    # ── 薄封装：规划 ──────────────────────────────────────────────────────────

    async def _plan_and_validate(
        self,
        context: DelegationContext,
        task_state: TaskState,
        mailbox: TaskMailbox,
    ) -> PlanGraph | None:
        assert self._planner is not None
        assert self._plan_validator is not None
        return await planning.plan_and_validate(
            self._planner, self._plan_validator, context, task_state, mailbox,
            self._infra.constraints, self._push_mailbox, self._abort_error_code,
        )

    # ── 薄封装：执行循环 ─────────────────────────────────────────────────────

    async def _execute(
        self,
        task_state: TaskState,
        graph: PlanGraph,
        mailbox: TaskMailbox,
        context: DelegationContext,
        resume_from: ExecutionState | None = None,
    ) -> None:
        assert self._orchestrator is not None
        return await execution_loop.execute_loop(
            self._orchestrator, task_state, graph, mailbox, context, resume_from,
            self._dispatch_result, self._push_mailbox,
        )

    # ── 薄封装：结果处理 ─────────────────────────────────────────────────────

    async def _handle_success(
        self, result: OrchestratorResult, task_state: TaskState,
    ) -> None:
        return await result_handlers.handle_success(result, task_state)

    async def _handle_failed(
        self, result: OrchestratorResult, task_state: TaskState, mailbox: TaskMailbox,
    ) -> None:
        return await result_handlers.handle_failed(
            result, task_state, mailbox, self._push_mailbox,
        )

    async def _handle_cancelled(
        self, result: OrchestratorResult, task_state: TaskState, mailbox: TaskMailbox,
    ) -> None:
        return await result_handlers.handle_cancelled(
            result, task_state, mailbox, self._push_mailbox,
        )

    async def _handle_blocked(
        self, result: OrchestratorResult, task_state: TaskState, mailbox: TaskMailbox,
    ) -> None:
        return await result_handlers.handle_blocked(
            result, task_state, mailbox, self._push_mailbox,
        )

    async def _handle_paused(
        self, result: OrchestratorResult, task_state: TaskState, mailbox: TaskMailbox,
    ) -> None:
        assert self._checkpoint_mgr is not None
        return await result_handlers.handle_paused(
            result, task_state, mailbox, self._checkpoint_mgr, self._push_mailbox,
        )

    # ── 薄封装：Replan ────────────────────────────────────────────────────────

    async def _handle_replan(
        self,
        result: OrchestratorResult,
        task_state: TaskState,
        graph: PlanGraph,
        mailbox: TaskMailbox,
        context: DelegationContext,
    ) -> tuple[bool, ExecutionState | None]:
        assert self._replanner is not None
        assert self._plan_validator is not None
        assert self._orchestrator is not None
        assert self._checkpoint_mgr is not None
        return await replan.handle_replan(
            result, task_state, graph, mailbox, context,
            self._replanner, self._plan_validator, self._orchestrator,
            self._checkpoint_mgr, self._infra.constraints, self._push_mailbox,
            self._token_ledger,
        )

    def _check_meltdown(self, task_state: TaskState) -> str | None:
        assert task_state.plan is not None, "熔断检查前 plan 必须已赋值"
        return replan.check_meltdown(
            task_state.plan.plan_version,
            self._infra.constraints.max_failure_count,
            self._infra.constraints.max_task_duration_s,
            task_state.created_at,
            self._token_ledger,
            self._infra.constraints.max_total_tokens,
        )

    def _extract_node_results(
        self, checkpoint: TaskCheckpoint,
    ) -> dict[str, NodeState]:
        return replan.extract_node_results(checkpoint)

    # ── static helpers ────────────────────────────────────────────────────────

    @staticmethod
    async def _push_mailbox(
        mailbox: TaskMailbox, event_type: TaskEventType, data: dict[str, Any],
    ) -> None:
        """推送事件到 TaskMailbox——委托 push_mailbox_event。"""
        from Module.task_system.mailbox import push_mailbox_event
        push_mailbox_event(mailbox, event_type, data, caller="SubAgent")

    @staticmethod
    def _abort_error_code(issues: list[ValidationIssue]) -> str:
        """从校验问题列表中推导最合适的 TaskErrorCode。

        PLAN_TOO_LARGE：节点数/深度/结构问题（Planner 产出质量）
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
