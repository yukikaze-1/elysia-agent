"""SubAgent 单元测试。

覆盖 SubAgent 决策层所有路径：
    - run() 主流程（成功 / 校验 abort）
    - _execute() / _dispatch_result() 5 条 Orchestrator 返回路径
    - _check_meltdown() 熔断判断
    - _extract_node_results() 从 checkpoint 提取节点状态
    - _abort_error_code() 校验问题 → 错误码映射
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from Module.task_system.mailbox import TaskMailbox
from Module.task_system.schema import (
    ArtifactId,
    ConstraintConfig,
    DAG,
    DAGNode,
    DAGPatch,
    DAGPatchType,
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


# ── _execute() / _dispatch_result() 路径 ────────────────────────────────────


@pytest.mark.asyncio
async def test_run_loop_success():
    """Orchestrator 返回 SUCCESS → 循环结束。"""
    agent, mock_orch = _make_sub_agent()
    mock_orch.run.return_value = OrchestratorResult(
        status=OrchestratorRunStatus.SUCCESS,
    )

    from Module.task_system.core.graph import PlanGraph
    graph = PlanGraph.from_plan(_mock_plan())

    await agent._execute(_task_state(), graph, TaskMailbox(TaskId("t1")), _delegation_context())

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

    await agent._execute(_task_state(), graph, mailbox, _delegation_context())

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

    await agent._execute(_task_state(), graph, mailbox, _delegation_context())

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

    await agent._execute(_task_state(), graph, mailbox, _delegation_context())

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
    await agent._execute(ts, graph, mailbox, _delegation_context())

    events = _collect_mailbox_events(mailbox)
    assert len(events) == 1
    assert events[0].type == TaskEventType.FAILED
    assert "failure_meltdown" in events[0].data.get("error_type", "")


# ── _handle_replan 路径 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_replan_dagpatch_success():
    """replan 返回 DAGPatch → apply_patch + version += 1 + 返回 (True, resume_state)。"""
    agent, mock_orch = _make_sub_agent()

    patch = DAGPatch(
        patch_type=DAGPatchType.ADD_NODES,
        reason="需要前置步骤获取时间",
        add_nodes=[
            DAGNode(node_id=NodeId("get_time"), description="获取当前时间",
                    output_as=ArtifactId("output:get_time"))
        ],
        add_edges=[(NodeId("get_time"), NodeId("a"))],
    )
    agent._planner.replan = AsyncMock(return_value=patch)

    # 第一次 run() 返回 NEEDS_REPLAN，第二次返回 SUCCESS
    mock_orch.run = AsyncMock(side_effect=[
        OrchestratorResult(
            status=OrchestratorRunStatus.NEEDS_REPLAN,
            error_context={"failed_nodes": [NodeId("a")], "reason": "工具超时"},
            checkpoint_state=_checkpoint("t1"),
        ),
        OrchestratorResult(status=OrchestratorRunStatus.SUCCESS),
    ])

    from Module.task_system.core.graph import PlanGraph
    graph = PlanGraph.from_plan(_mock_plan())
    ts = _task_state()
    ts.plan = graph.to_plan(_mock_plan())
    ts.plan.plan_version = 3

    mailbox = TaskMailbox(TaskId("t1"))
    await agent._execute(ts, graph, mailbox, _delegation_context())

    # 验证 DAGPatch 已应用
    assert graph.node_count == 2  # 原 1 个节点 + 1 个新增
    assert NodeId("get_time") in graph.nodes

    # 版本号正确递增
    assert ts.plan.plan_version == 4  # 3 + 1

    # 没有 FAILED 事件
    events = _collect_mailbox_events(mailbox)
    event_types = [e.type for e in events]
    assert TaskEventType.FAILED not in event_types


@pytest.mark.asyncio
async def test_handle_replan_dagpatch_validation_failure():
    """replan 返回 DAGPatch 引入结构问题（如孤立节点）→ FAILED。"""
    agent, mock_orch = _make_sub_agent()

    # 构造一个会引入孤立节点的 patch（add_nodes 不加边，>1 节点时触发 ORPHAN）
    patch = DAGPatch(
        patch_type=DAGPatchType.ADD_NODES,
        reason="添加孤立节点",
        add_nodes=[
            DAGNode(node_id=NodeId("orphan"), description="无依赖无被依赖的节点")
        ],
    )
    agent._planner.replan = AsyncMock(return_value=patch)

    mock_orch.run.return_value = OrchestratorResult(
        status=OrchestratorRunStatus.NEEDS_REPLAN,
        error_context={"failed_nodes": [NodeId("a")], "reason": "失败"},
        checkpoint_state=_checkpoint("t1"),
    )

    from Module.task_system.core.graph import PlanGraph
    graph = PlanGraph.from_plan(_mock_plan())
    ts = _task_state()
    ts.plan = graph.to_plan(_mock_plan())

    mailbox = TaskMailbox(TaskId("t1"))
    await agent._execute(ts, graph, mailbox, _delegation_context())

    events = _collect_mailbox_events(mailbox)
    assert len(events) == 1
    assert events[0].type == TaskEventType.FAILED
    assert "DAGPatch 校验失败" in events[0].data["error"]


@pytest.mark.asyncio
async def test_handle_replan_full_replan_success():
    """replan 返回 Plan → 校验通过 → graph.replace() + version 继承 + resume_from=None。"""
    agent, mock_orch = _make_sub_agent()

    new_plan = Plan(
        dag=DAG(nodes=[
            DAGNode(node_id=NodeId("new_search"), description="新搜索方案",
                    output_as=ArtifactId("output:new_search")),
        ], edges=[]),
        plan_version=0,  # sentinel——应被 SubAgent 覆写
    )
    agent._planner.replan = AsyncMock(return_value=new_plan)

    # PlanValidator 返回 valid
    from Module.task_system.core.graph import PlanGraph
    valid_graph = PlanGraph.from_plan(new_plan)
    agent._plan_validator.validate = MagicMock(
        return_value=(ValidationResult(valid=True, action=ValidationAction.PROCEED), valid_graph)
    )

    # 第一次 run() 返回 NEEDS_REPLAN，第二次返回 SUCCESS
    mock_orch.run = AsyncMock(side_effect=[
        OrchestratorResult(
            status=OrchestratorRunStatus.NEEDS_REPLAN,
            error_context={"failed_nodes": [NodeId("a")], "reason": "DAG 结构根本错了"},
            checkpoint_state=_checkpoint("t1"),
        ),
        OrchestratorResult(status=OrchestratorRunStatus.SUCCESS),
    ])

    graph = PlanGraph.from_plan(_mock_plan())
    ts = _task_state()
    ts.plan = graph.to_plan(_mock_plan())
    old_version = 5
    ts.plan.plan_version = old_version

    mailbox = TaskMailbox(TaskId("t1"))
    await agent._execute(ts, graph, mailbox, _delegation_context())

    # 图已被替换
    assert graph.node_count == 1
    assert NodeId("new_search") in graph.nodes
    assert NodeId("a") not in graph.nodes  # 旧节点不存在

    # 版本号从旧 plan 继承 + 1
    assert ts.plan.plan_version == old_version + 1  # 5 + 1 = 6

    # 没有 FAILED 事件
    events = _collect_mailbox_events(mailbox)
    event_types = [e.type for e in events]
    assert TaskEventType.FAILED not in event_types


@pytest.mark.asyncio
async def test_handle_replan_full_replan_validation_failure():
    """replan 返回 Plan 但校验不通过 → FAILED。"""
    agent, mock_orch = _make_sub_agent()

    new_plan = Plan(
        dag=DAG(nodes=[
            DAGNode(node_id=NodeId("bad"), description="缺少 output_contract 的 required 节点"),
        ], edges=[]),
    )
    agent._planner.replan = AsyncMock(return_value=new_plan)

    # PlanValidator 返回 invalid
    agent._plan_validator.validate = MagicMock(
        return_value=(
            ValidationResult(
                valid=False,
                issues=[ValidationIssue(
                    type=ValidationIssueType.MISSING_CONTRACT,
                    node_id=NodeId("bad"),
                    message="缺少 output_contract",
                )],
            ),
            MagicMock(),  # graph
        )
    )

    mock_orch.run.return_value = OrchestratorResult(
        status=OrchestratorRunStatus.NEEDS_REPLAN,
        error_context={"failed_nodes": [NodeId("a")], "reason": "失败"},
        checkpoint_state=_checkpoint("t1"),
    )

    from Module.task_system.core.graph import PlanGraph
    graph = PlanGraph.from_plan(_mock_plan())
    ts = _task_state()
    ts.plan = graph.to_plan(_mock_plan())

    mailbox = TaskMailbox(TaskId("t1"))
    await agent._execute(ts, graph, mailbox, _delegation_context())

    events = _collect_mailbox_events(mailbox)
    assert len(events) == 1
    assert events[0].type == TaskEventType.FAILED
    assert "全量重建校验失败" in events[0].data["error"]


@pytest.mark.asyncio
async def test_handle_replan_checkpoint_none():
    """NEEDS_REPLAN 但 checkpoint_state=None → FAILED。"""
    agent, mock_orch = _make_sub_agent()

    mock_orch.run.return_value = OrchestratorResult(
        status=OrchestratorRunStatus.NEEDS_REPLAN,
        error_context={"failed_nodes": [NodeId("a")], "reason": "失败"},
        checkpoint_state=None,  # 缺少快照
    )

    from Module.task_system.core.graph import PlanGraph
    graph = PlanGraph.from_plan(_mock_plan())
    ts = _task_state()
    ts.plan = graph.to_plan(_mock_plan())

    mailbox = TaskMailbox(TaskId("t1"))
    await agent._execute(ts, graph, mailbox, _delegation_context())

    events = _collect_mailbox_events(mailbox)
    assert len(events) == 1
    assert events[0].type == TaskEventType.FAILED
    assert "checkpoint 缺失" in events[0].data["error"]


@pytest.mark.asyncio
async def test_handle_replan_not_implemented():
    """replan 抛出 NotImplementedError → 优雅降级 FAILED。"""
    agent, mock_orch = _make_sub_agent()
    agent._planner.replan = AsyncMock(side_effect=NotImplementedError("replan 未实现"))

    mock_orch.run.return_value = OrchestratorResult(
        status=OrchestratorRunStatus.NEEDS_REPLAN,
        error_context={"failed_nodes": [NodeId("a")], "reason": "工具超时"},
        checkpoint_state=_checkpoint("t1"),
    )

    from Module.task_system.core.graph import PlanGraph
    graph = PlanGraph.from_plan(_mock_plan())
    ts = _task_state()
    ts.plan = graph.to_plan(_mock_plan())

    mailbox = TaskMailbox(TaskId("t1"))
    await agent._execute(ts, graph, mailbox, _delegation_context())

    events = _collect_mailbox_events(mailbox)
    assert len(events) == 1
    assert events[0].type == TaskEventType.FAILED
    assert "Replan 尚未实现" in events[0].data["error"]


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
    """从 TaskCheckpoint 提取 node_states（NodeState 对象含 evaluation 字段）。"""
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

    assert isinstance(results, dict)
    assert results[NodeId("a")].summary == "搜索完成"
    assert results[NodeId("a")].error is None
    assert results[NodeId("b")].summary == ""
    assert results[NodeId("b")].error == "工具超时"


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
    """非结构问题（MISSING_CONTRACT）→ INTERNAL_ERROR。"""
    agent, _ = _make_sub_agent()
    issues = [
        ValidationIssue(
            type=ValidationIssueType.MISSING_CONTRACT,
            node_id=NodeId("a"),
            message="缺少 output_contract",
        ),
    ]
    assert agent._abort_error_code(issues) == TaskErrorCode.INTERNAL_ERROR.value


def test_abort_error_code_cycle():
    """CYCLE → PLAN_TOO_LARGE（结构不合法）。"""
    agent, _ = _make_sub_agent()
    issues = [
        ValidationIssue(
            type=ValidationIssueType.CYCLE,
            message="DAG 有环: A → B → A",
        ),
    ]
    assert agent._abort_error_code(issues) == TaskErrorCode.PLAN_TOO_LARGE.value


def test_abort_error_code_duplicate_id():
    """DUPLICATE_ID → PLAN_TOO_LARGE（结构不合法）。"""
    agent, _ = _make_sub_agent()
    issues = [
        ValidationIssue(
            type=ValidationIssueType.DUPLICATE_ID,
            node_id=NodeId("a"),
            message="node_id 重复: a",
        ),
    ]
    assert agent._abort_error_code(issues) == TaskErrorCode.PLAN_TOO_LARGE.value


def test_abort_error_code_missing_dependency():
    """MISSING_DEPENDENCY → PLAN_TOO_LARGE（结构不合法）。"""
    agent, _ = _make_sub_agent()
    issues = [
        ValidationIssue(
            type=ValidationIssueType.MISSING_DEPENDENCY,
            node_id=NodeId("b"),
            message="依赖的节点 x 不存在",
        ),
    ]
    assert agent._abort_error_code(issues) == TaskErrorCode.PLAN_TOO_LARGE.value


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
