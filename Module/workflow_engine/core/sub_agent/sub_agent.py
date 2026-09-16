"""Sub-Agent —— WorkflowEngine 池中的任务级执行单元。

接收 DelegationContext，负责：
    1. 初始规划（Planner.plan()）
    2. 校验（PlanValidator.validate()）
    3. 构建 PlanGraph（NetworkX DAG）
    4. 创建 WorkflowState（任务级）
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

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from Module.workflow_contract import (
    DelegationContext,
    WorkflowEventType,
    WorkflowStatus,
)
from Module.workflow_engine.schema import (
    ExecutionState,
    NodeState,
    OrchestratorResult,
    OrchestratorRunStatus,
    WorkflowCheckpoint,
    WorkflowErrorCode,
    WorkflowState,
    ValidationIssue,
    ValidationIssueType,
    now_iso,
)

from Module.workflow_engine.core.sub_agent import (
    planning,
    execution_loop,
    result_handlers,
    replan,
)

if TYPE_CHECKING:
    from Module.workflow_engine.artifact_store import ArtifactStore
    from Module.workflow_engine.checkpoint import CheckpointManager
    from Module.workflow_engine.core.evaluator import Evaluator
    from Module.workflow_engine.core.executor import Executor
    from Module.workflow_engine.core.graph import PlanGraph
    from Module.workflow_engine.core.orchestrator import Orchestrator
    from Module.workflow_engine.core.planner import Planner, Replanner
    from Module.workflow_engine.core.plan_validator import PlanValidator
    from Module.workflow_contract import AgentInfra
    from Module.workflow_engine.mailbox import WorkflowMailbox
    from Module.workflow_contract import HITLNotifier
    from Module.workflow_engine.token_ledger import TokenLedger

logger = logging.getLogger(__name__)


class SubAgent:
    """任务级执行单元。

    职责层次：
        Sub-Agent  → 决策层（规划 / Replan / 熔断 / 审批等待 / 暂停）
        Orchestrator → 执行层（按 Plan 推进 DAG，遇到结构性障碍时返回中断信号）

    WorkflowState 归 SubAgent 管理（任务生命周期），ExecutionState 归 Orchestrator（执行追踪）。
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
        self._task_state: WorkflowState | None = None
        self._graph: PlanGraph | None = None
        self._context: DelegationContext | None = None
        self._mailbox: WorkflowMailbox | None = None

    def _require(self, *attrs: str) -> None:
        """确保 run() 已调用——所有入口方法的前置条件。"""
        for attr in attrs:
            if getattr(self, attr, None) is None:
                raise RuntimeError(
                    f"SubAgent.{attr} 未初始化，请先调用 run()"
                )

    # ── 主入口 ─────────────────────────────────────────────────────────────────

    async def run(
        self,
        context: DelegationContext,
        mailbox: WorkflowMailbox,
    ) -> None:
        """任务主入口——创建 WorkflowState → 规划 → 校验 → 执行-决策循环。

        1. 创建 WorkflowState（任务级）
        2. _plan_and_validate() → PlanGraph
        3. _execute() → 执行-决策循环

        Args:
            context: 委托上下文，包含 intent / tools / constraints。
            mailbox: 任务邮箱，用于推送生命周期事件。
        """
        task_id = mailbox.task_id

        # 0. 创建/复用 WorkflowState
        if self._task_state is None:
            # 原创建逻辑（保留）——直接使用 SubAgent 的测试路径
            task_state = WorkflowState(
                task_id=task_id,
                status=WorkflowStatus.PENDING,
                tools=context.tools,
                session_id=context.session_id,
                created_at=now_iso(),
                updated_at=now_iso(),
            )
            self._task_state = task_state
        else:
            # E2：submit() 已预创建 QUEUED 状态，复用并转入 PENDING
            task_state = self._task_state                              # 关键：本地 task_state 必须绑定（下游使用）
            if task_state.is_terminal():                               # 终态守卫：drain 复核后、run() 执行前被 cancel → 不再复活
                raise asyncio.CancelledError()
            task_state.status = WorkflowStatus.PENDING
            task_state.tools = context.tools
            task_state.session_id = context.session_id
            task_state.updated_at = now_iso()
        self._context = context   # resume 时需要 intent/tools/constraints
        self._mailbox = mailbox   # resume 时复用同一个 mailbox

        # ── 创建 per-task 组件 ──
        from Module.workflow_engine.artifact_store import ArtifactStore
        from Module.workflow_engine.checkpoint import CheckpointManager
        from Module.workflow_engine.core.evaluator import Evaluator
        from Module.workflow_engine.core.executor import Executor
        from Module.workflow_engine.core.orchestrator import Orchestrator
        from Module.workflow_engine.core.planner import Planner, Replanner
        from Module.workflow_engine.core.plan_validator import PlanValidator
        from Module.workflow_engine.token_ledger import TokenLedger

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
            token_ledger=self._token_ledger,   # S2-D4：每轮预算检查要读它
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
        if ts is None or ts.status != WorkflowStatus.PAUSED:
            raise RuntimeError(
                f"task_id={self._mailbox.task_id if self._mailbox else 'N/A'} 非 PAUSED 状态"
                f"（{ts.status if ts else 'None'}），无法 resume"
            )

        ts.status = WorkflowStatus.RESUMING
        ts.updated_at = now_iso()

        self._require("_checkpoint_mgr", "_mailbox")
        # _require 只做运行时守卫（返回 None），pyright 不窄化——逐组件 assert 窄化类型
        assert self._checkpoint_mgr is not None
        assert self._mailbox is not None
        checkpoint = await self._checkpoint_mgr.load(self._mailbox.task_id)
        if checkpoint is None:
            ts.status = WorkflowStatus.FAILED
            ts.updated_at = now_iso()
            raise RuntimeError("checkpoint 缺失，无法恢复")

        if self._graph is None:
            ts.status = WorkflowStatus.FAILED
            ts.updated_at = now_iso()
            raise RuntimeError("graph 缺失，无法恢复")

        self._require("_context")
        assert self._context is not None

        # 清零中断标志
        checkpoint.exec.pause_requested = False
        checkpoint.exec.cancel_requested = False

        mailbox = self._mailbox
        if mailbox is None:
            raise RuntimeError("mailbox 未初始化，请先调用 run()")
        await self._execute(
            ts, self._graph, mailbox, self._context,
            resume_from=checkpoint.exec,
        )

    # ── _dispatch_result 保留在类中（42 行 if/elif 不提取） ─────────────────

    async def _dispatch_result(
        self,
        result: OrchestratorResult,
        task_state: WorkflowState,
        graph: PlanGraph,
        mailbox: WorkflowMailbox,
        context: DelegationContext,
    ) -> tuple[bool, ExecutionState | None]:
        """按 OrchestratorRunStatus 分发到对应 handler。

        Args:
            result: Orchestrator 执行结果。
            task_state: 当前任务状态。
            graph: 当前执行计划图。
            mailbox: 任务邮箱。
            context: 委托上下文。

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
        task_state: WorkflowState,
        mailbox: WorkflowMailbox,
    ) -> PlanGraph | None:
        """规划 + 校验——委托到 planning.plan_and_validate。

        Args:
            context: 委托上下文。
            task_state: 当前任务状态。
            mailbox: 任务邮箱。

        Returns:
            校验通过时返回 PlanGraph，ABORT 时返回 None。
        """
        self._require("_planner", "_plan_validator")
        # _require 只做运行时守卫（返回 None），pyright 不窄化——逐组件 assert 窄化类型
        assert self._planner is not None
        assert self._plan_validator is not None
        return await planning.plan_and_validate(
            self._planner, self._plan_validator, context, task_state, mailbox,
            self._infra.constraints, self._push_mailbox, self._abort_error_code,
        )

    # ── 薄封装：执行循环 ─────────────────────────────────────────────────────

    async def _execute(
        self,
        task_state: WorkflowState,
        graph: PlanGraph,
        mailbox: WorkflowMailbox,
        context: DelegationContext,
        resume_from: ExecutionState | None = None,
    ) -> None:
        """执行-决策循环——委托到 execution_loop.execute_loop。

        Args:
            task_state: 当前任务状态。
            graph: 执行计划图。
            mailbox: 任务邮箱。
            context: 委托上下文。
            resume_from: 恢复执行时传入的 ExecutionState，首次执行为 None。
        """
        self._require("_orchestrator")
        assert self._orchestrator is not None
        return await execution_loop.execute_loop(
            self._orchestrator, task_state, graph, mailbox, context, resume_from,
            self._dispatch_result, self._push_mailbox,
        )

    # ── 薄封装：结果处理 ─────────────────────────────────────────────────────

    async def _handle_success(
        self, result: OrchestratorResult, task_state: WorkflowState,
    ) -> None:
        """处理执行成功结果——委托到 result_handlers.handle_success。

        Args:
            result: Orchestrator 执行结果。
            task_state: 当前任务状态。
        """
        return await result_handlers.handle_success(result, task_state)

    async def _handle_failed(
        self, result: OrchestratorResult, task_state: WorkflowState, mailbox: WorkflowMailbox,
    ) -> None:
        """处理执行失败结果——委托到 result_handlers.handle_failed。

        Args:
            result: Orchestrator 执行结果。
            task_state: 当前任务状态。
            mailbox: 任务邮箱。
        """
        return await result_handlers.handle_failed(
            result, task_state, mailbox, self._push_mailbox,
        )

    async def _handle_cancelled(
        self, result: OrchestratorResult, task_state: WorkflowState, mailbox: WorkflowMailbox,
    ) -> None:
        """处理取消结果——委托到 result_handlers.handle_cancelled。

        Args:
            result: Orchestrator 执行结果。
            task_state: 当前任务状态。
            mailbox: 任务邮箱。
        """
        return await result_handlers.handle_cancelled(
            result, task_state, mailbox, self._push_mailbox,
        )

    async def _handle_blocked(
        self, result: OrchestratorResult, task_state: WorkflowState, mailbox: WorkflowMailbox,
    ) -> None:
        """处理阻塞结果——委托到 result_handlers.handle_blocked。

        Args:
            result: Orchestrator 执行结果。
            task_state: 当前任务状态。
            mailbox: 任务邮箱。
        """
        return await result_handlers.handle_blocked(
            result, task_state, mailbox, self._push_mailbox,
        )

    async def _handle_paused(
        self, result: OrchestratorResult, task_state: WorkflowState, mailbox: WorkflowMailbox,
    ) -> None:
        """处理暂停结果——委托到 result_handlers.handle_paused。

        Args:
            result: Orchestrator 执行结果。
            task_state: 当前任务状态。
            mailbox: 任务邮箱。
        """
        self._require("_checkpoint_mgr")
        assert self._checkpoint_mgr is not None
        return await result_handlers.handle_paused(
            result, task_state, mailbox, self._checkpoint_mgr, self._push_mailbox,
        )

    # ── 薄封装：Replan ────────────────────────────────────────────────────────

    async def _handle_replan(
        self,
        result: OrchestratorResult,
        task_state: WorkflowState,
        graph: PlanGraph,
        mailbox: WorkflowMailbox,
        context: DelegationContext,
    ) -> tuple[bool, ExecutionState | None]:
        """处理需要重新规划的结果——委托到 replan.handle_replan。

        Args:
            result: Orchestrator 执行结果。
            task_state: 当前任务状态。
            graph: 当前执行计划图。
            mailbox: 任务邮箱。
            context: 委托上下文。

        Returns:
            (True, resume_state)  — 继续执行循环
            (False, None)         — 终止循环
        """
        self._require("_replanner", "_plan_validator", "_orchestrator", "_checkpoint_mgr")
        # _require 只做运行时守卫（返回 None），pyright 不窄化——逐组件 assert 窄化类型
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

    def _check_meltdown(self, task_state: WorkflowState) -> str | None:
        """检查任务是否触发熔断——委托到 replan.check_meltdown。

        Args:
            task_state: 当前任务状态。

        Returns:
            熔断原因字符串，未触发时返回 None。
        """
        if task_state.plan is None:
            raise RuntimeError("熔断检查前 plan 必须已赋值")
        return replan.check_meltdown(
            task_state.plan.plan_version,
            self._infra.constraints.max_failure_count,
            self._infra.constraints.max_task_duration_s,
            task_state.created_at,
            self._token_ledger,
            self._infra.constraints.max_total_tokens,
        )

    def _extract_node_results(
        self, checkpoint: WorkflowCheckpoint,
    ) -> dict[str, NodeState]:
        """从 checkpoint 提取已完成节点的结果——委托到 replan.extract_node_results。

        Args:
            checkpoint: 任务检查点。

        Returns:
            node_id → NodeState 的映射字典。
        """
        return replan.extract_node_results(checkpoint)

    # ── static helpers ────────────────────────────────────────────────────────

    @staticmethod
    async def _push_mailbox(
        mailbox: WorkflowMailbox, event_type: WorkflowEventType, data: dict[str, Any],
    ) -> None:
        """推送事件到 WorkflowMailbox——委托 push_mailbox_event。

        Args:
            mailbox: 目标任务邮箱。
            event_type: 事件类型。
            data: 事件数据字典。
        """
        from Module.workflow_engine.mailbox import push_mailbox_event
        push_mailbox_event(mailbox, event_type, data, caller="SubAgent")

    @staticmethod
    def _abort_error_code(issues: list[ValidationIssue]) -> str:
        """从校验问题列表中推导最合适的 WorkflowErrorCode。

        PLAN_TOO_LARGE：节点数/深度/结构问题（Planner 产出质量）
        PLANNING_FAILED：其余**规划期**校验失败（如 required 节点缺 output_contract）

        Batch 1.7（Challenger C3）：兜底值由 `INTERNAL_ERROR` 改为 `PLANNING_FAILED`。
        `INTERNAL_ERROR` 的语义是"引擎自己出故障"，而这些全部是**规划器产出不合格**
        ——报 internal_error 会让"规划失败"在用户/调试面上冒充"执行期内部错误"，
        正是 T4 要消除的错误归因（实测：planner 漏写 output_contract → fix_plan 耗尽
        后会推 `internal_error`）。

        Args:
            issues: 校验问题列表。

        Returns:
            WorkflowErrorCode 字符串值。
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
            return WorkflowErrorCode.PLAN_TOO_LARGE.value
        return WorkflowErrorCode.PLANNING_FAILED.value
