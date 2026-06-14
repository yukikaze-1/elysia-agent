"""SubAgent 单元测试。

覆盖 SubAgent 决策层所有路径：
    - run() 主流程（成功 / 校验 abort）
    - _run_loop() 5 条 Orchestrator 返回路径
    - _check_meltdown() 熔断判断
    - _extract_node_results() 从 checkpoint 提取节点状态
    - _abort_error_code() 校验问题 → 错误码映射
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from Module.task_system.mailbox import TaskMailbox
from Module.task_system.schema import (
    ConstraintConfig,
    DAG,
    DAGNode,
    DelegationContext,
    ExecutionState,
    NodeId,
    NodeState,
    NodeStatus,
    OrchestratorResult,
    OrchestratorRunStatus,
    Plan,
    SessionId,
    TaskCheckpoint,
    TaskConstraints,
    TaskErrorCode,
    TaskEventType,
    TaskId,
    TaskState,
    TaskStatus,
    ValidationAction,
    ValidationIssue,
    ValidationIssueType,
    ValidationResult,
    now_iso,
)


# ── 工厂函数 ────────────────────────────────────────────────────────────────


def _task_state(task_id: str = "t1") -> TaskState:
    """最小 TaskState。"""
    return TaskState(
        task_id=TaskId(task_id),
        status=TaskStatus.RUNNING,
        plan=Plan(dag=DAG(nodes=[], edges=[]), plan_version=1),
        created_at=now_iso(),
        updated_at=now_iso(),
    )


def _delegation_context() -> DelegationContext:
    """最小 DelegationContext。"""
    return DelegationContext(
        intent="测试任务",
        session_id=SessionId("s1"),
    )


def _checkpoint(task_id: str = "t1") -> TaskCheckpoint:
    """最小 TaskCheckpoint（含一个 NodeState）。"""
    ts = _task_state(task_id)
    es = ExecutionState(
        node_states={
            NodeId("a"): NodeState(
                node_id=NodeId("a"),
                status=NodeStatus.SUCCESS,
                summary="节点 a 完成",
                error=None,
            ),
        },
    )
    return TaskCheckpoint(task=ts, exec=es)


def _mock_plan() -> Plan:
    """最小合法 Plan。"""
    return Plan(
        dag=DAG(nodes=[DAGNode(node_id=NodeId("a"), description="步骤 a")]),
        plan_version=1,
    )


# ── SubAgent 构造辅助 ───────────────────────────────────────────────────────


def _make_sub_agent(
    *,
    infra=None,
    hitl=None,
    planner=None,
    validator=None,
    executor=None,
    evaluator=None,
    checkpoint_mgr=None,
    artifact_store=None,
):
    """构造 SubAgent，自动填充未指定的 mock 依赖。"""
    from Module.task_system.core.sub_agent import SubAgent

    if infra is None:
        infra = MagicMock()
        infra.constraints = ConstraintConfig(max_failure_count=5)
    if hitl is None:
        hitl = MagicMock()
    if planner is None:
        planner = MagicMock()
        planner.plan = AsyncMock(return_value=_mock_plan())
    if validator is None:
        validator = MagicMock()
    if executor is None:
        executor = MagicMock()
    if evaluator is None:
        evaluator = MagicMock()
    if checkpoint_mgr is None:
        checkpoint_mgr = MagicMock()
        checkpoint_mgr.save = AsyncMock()
        checkpoint_mgr.load = AsyncMock()
        checkpoint_mgr.delete = AsyncMock()
    if artifact_store is None:
        artifact_store = MagicMock()

    # Mock Orchestrator 构造——SubAgent.__init__ 内部 local import
    with patch("Module.task_system.core.orchestrator.Orchestrator") as mock_orch_cls:
        mock_orch = MagicMock()
        mock_orch.run = AsyncMock()
        mock_orch_cls.return_value = mock_orch
        agent = SubAgent(
            infra=infra,
            hitl_notifier=hitl,
            planner=planner,
            plan_validator=validator,
            executor=executor,
            evaluator=evaluator,
            checkpoint_manager=checkpoint_mgr,
            artifact_store=artifact_store,
        )

    return agent, mock_orch


# ── run() 主流程 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_success():
    """完整成功路径——规划 → 校验通过 → 执行完成。"""
    agent, mock_orch = _make_sub_agent()
    mock_orch.run.return_value = OrchestratorResult(
        status=OrchestratorRunStatus.SUCCESS,
    )
    # PlanValidator 返回 (ValidationResult, PlanGraph)
    from Module.task_system.core.graph import PlanGraph
    graph = PlanGraph.from_plan(_mock_plan())
    agent._plan_validator.validate.return_value = (
        ValidationResult(valid=True, action=ValidationAction.PROCEED),
        graph,
    )

    mailbox = TaskMailbox(TaskId("t1"))
    await agent.run(_delegation_context(), mailbox)

    # 验证 Planner 被调用
    agent._planner.plan.assert_called_once()
    # 验证 Orchestrator 被调用（task_state + graph + mailbox）
    mock_orch.run.assert_called_once()
    call_args = mock_orch.run.call_args[0]
    assert call_args[0].task_id == TaskId("t1")  # task_state
    assert call_args[1] is graph                 # graph


@pytest.mark.asyncio
async def test_run_validation_abort():
    """校验 ABORT → 任务 FAILED + 推送 FAILED 事件。"""
    agent, mock_orch = _make_sub_agent()

    issues = [
        ValidationIssue(
            type=ValidationIssueType.MAX_NODES_EXCEEDED,
            message="DAG 节点数 25 超过上限 20",
        )
    ]
    agent._plan_validator.validate.return_value = (
        ValidationResult(valid=False, issues=issues, action=ValidationAction.ABORT),
        MagicMock(),  # graph——校验失败但 graph 仍返回
    )

    mailbox = TaskMailbox(TaskId("t1"))
    await agent.run(_delegation_context(), mailbox)

    # 不应调用 Orchestrator——校验失败直接返回
    mock_orch.run.assert_not_called()

    # 验证 FAILED 事件推送到 mailbox
    events = _collect_mailbox_events(mailbox)
    assert len(events) == 1
    assert events[0].type == TaskEventType.FAILED
    assert "plan_too_large" in events[0].data.get("error_type", "")


# ── _run_loop() 路径 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_loop_success():
    """Orchestrator 返回 SUCCESS → 循环结束。"""
    agent, mock_orch = _make_sub_agent()
    mock_orch.run.return_value = OrchestratorResult(
        status=OrchestratorRunStatus.SUCCESS,
    )

    from Module.task_system.core.graph import PlanGraph
    graph = PlanGraph.from_plan(_mock_plan())

    await agent._run_loop(_task_state(), graph, TaskMailbox(TaskId("t1")))

    mock_orch.run.assert_called_once()


@pytest.mark.asyncio
async def test_run_loop_blocked():
    """Orchestrator 返回 BLOCKED → 推送 APPROVAL_NEEDED → 返回。"""
    agent, mock_orch = _make_sub_agent()
    mock_orch.run.return_value = OrchestratorResult(
        status=OrchestratorRunStatus.BLOCKED,
        checkpoint_state=_checkpoint("t1"),
    )

    from Module.task_system.core.graph import PlanGraph
    graph = PlanGraph.from_plan(_mock_plan())
    mailbox = TaskMailbox(TaskId("t1"))

    await agent._run_loop(_task_state(), graph, mailbox)

    events = _collect_mailbox_events(mailbox)
    assert len(events) == 1
    assert events[0].type == TaskEventType.APPROVAL_NEEDED


@pytest.mark.asyncio
async def test_run_loop_paused():
    """Orchestrator 返回 PAUSED → 保存 checkpoint + 推送 PAUSED → 返回。"""
    agent, mock_orch = _make_sub_agent()
    cp = _checkpoint("t1")
    mock_orch.run.return_value = OrchestratorResult(
        status=OrchestratorRunStatus.PAUSED,
        checkpoint_state=cp,
    )

    from Module.task_system.core.graph import PlanGraph
    graph = PlanGraph.from_plan(_mock_plan())
    mailbox = TaskMailbox(TaskId("t1"))

    await agent._run_loop(_task_state(), graph, mailbox)

    # 验证 checkpoint 被保存
    agent._checkpoint_mgr.save.assert_called_once_with(TaskId("t1"), cp)

    # 验证 PAUSED 事件
    events = _collect_mailbox_events(mailbox)
    assert len(events) == 1
    assert events[0].type == TaskEventType.PAUSED


@pytest.mark.asyncio
async def test_run_loop_failed():
    """Orchestrator 返回 FAILED → 推送 FAILED → 返回。"""
    agent, mock_orch = _make_sub_agent()
    mock_orch.run.return_value = OrchestratorResult(
        status=OrchestratorRunStatus.FAILED,
        error_context={"reason": "检测到死锁"},
    )

    from Module.task_system.core.graph import PlanGraph
    graph = PlanGraph.from_plan(_mock_plan())
    mailbox = TaskMailbox(TaskId("t1"))

    await agent._run_loop(_task_state(), graph, mailbox)

    events = _collect_mailbox_events(mailbox)
    assert len(events) == 1
    assert events[0].type == TaskEventType.FAILED
    assert "检测到死锁" in events[0].data["error"]


@pytest.mark.asyncio
async def test_run_loop_needs_replan_meltdown():
    """Orchestrator 返回 NEEDS_REPLAN + 达到熔断上限 → FAILED。"""
    agent, mock_orch = _make_sub_agent()
    # 设置极低的熔断上限
    agent._infra.constraints = ConstraintConfig(max_failure_count=1)
    mock_orch.run.return_value = OrchestratorResult(
        status=OrchestratorRunStatus.NEEDS_REPLAN,
        error_context={"failed_nodes": [NodeId("a")], "reason": "节点失败"},
        checkpoint_state=_checkpoint("t1"),
    )

    from Module.task_system.core.graph import PlanGraph
    graph = PlanGraph.from_plan(_mock_plan())
    # plan_version > max_failure_count → 熔断
    ts = _task_state()
    ts.plan = Plan(dag=DAG(nodes=[], edges=[]), plan_version=2)  # 2 > 1

    mailbox = TaskMailbox(TaskId("t1"))
    await agent._run_loop(ts, graph, mailbox)

    events = _collect_mailbox_events(mailbox)
    assert len(events) == 1
    assert events[0].type == TaskEventType.FAILED
    assert "failure_meltdown" in events[0].data.get("error_type", "")


# ── _check_meltdown ─────────────────────────────────────────────────────────


def test_check_meltdown_ok():
    """plan_version ≤ max_failure_count → 不触发熔断。"""
    agent, _ = _make_sub_agent()
    agent._infra.constraints = ConstraintConfig(max_failure_count=5)

    ts = _task_state()
    ts.plan.plan_version = 5  # 5 ≤ 5，不触发

    assert agent._check_meltdown(ts) is None


def test_check_meltdown_triggers():
    """plan_version > max_failure_count → 触发熔断。"""
    agent, _ = _make_sub_agent()
    agent._infra.constraints = ConstraintConfig(max_failure_count=3)

    ts = _task_state()
    ts.plan.plan_version = 4  # 4 > 3，触发

    reason = agent._check_meltdown(ts)
    assert reason is not None
    assert "熔断" in reason


# ── _extract_node_results ───────────────────────────────────────────────────


def test_extract_node_results():
    """从 TaskCheckpoint 提取 node summary + error。"""
    agent, _ = _make_sub_agent()
    cp = TaskCheckpoint(
        task=_task_state(),
        exec=ExecutionState(
            node_states={
                NodeId("a"): NodeState(
                    node_id=NodeId("a"),
                    summary="搜索完成",
                    error=None,
                ),
                NodeId("b"): NodeState(
                    node_id=NodeId("b"),
                    summary="",
                    error="工具超时",
                ),
            },
        ),
    )

    results = agent._extract_node_results(cp)

    assert results[NodeId("a")]["summary"] == "搜索完成"
    assert results[NodeId("a")]["error"] is None
    assert results[NodeId("b")]["summary"] == ""
    assert results[NodeId("b")]["error"] == "工具超时"


# ── _abort_error_code ───────────────────────────────────────────────────────


def test_abort_error_code_max_nodes():
    """MAX_NODES_EXCEEDED → PLAN_TOO_LARGE。"""
    agent, _ = _make_sub_agent()
    issues = [
        ValidationIssue(
            type=ValidationIssueType.MAX_NODES_EXCEEDED,
            message="节点数超限",
        ),
    ]
    assert agent._abort_error_code(issues) == TaskErrorCode.PLAN_TOO_LARGE.value


def test_abort_error_code_max_depth():
    """MAX_DEPTH_EXCEEDED → PLAN_TOO_LARGE。"""
    agent, _ = _make_sub_agent()
    issues = [
        ValidationIssue(
            type=ValidationIssueType.MAX_DEPTH_EXCEEDED,
            message="深度超限",
        ),
    ]
    assert agent._abort_error_code(issues) == TaskErrorCode.PLAN_TOO_LARGE.value


def test_abort_error_code_other():
    """非 fatal issue → INTERNAL_ERROR。"""
    agent, _ = _make_sub_agent()
    issues = [
        ValidationIssue(
            type=ValidationIssueType.MISSING_CONTRACT,
            node_id=NodeId("a"),
            message="缺少 output_contract",
        ),
    ]
    assert agent._abort_error_code(issues) == TaskErrorCode.INTERNAL_ERROR.value


# ── 辅助函数 ────────────────────────────────────────────────────────────────


def _collect_mailbox_events(mailbox: TaskMailbox) -> list:
    """从 mailbox 中收集所有已推送事件（非阻塞）。"""
    events: list = []
    while True:
        ev = mailbox.get_nowait()
        if ev is None:
            break
        events.append(ev)
    return events
