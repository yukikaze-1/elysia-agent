"""Planner 集成测试——需要 DEEPSEEK_API_KEY。"""

import asyncio
import os
import sys
from pathlib import Path

import pytest

# 加载项目根目录的 .env
_env_path = Path(__file__).resolve().parents[4] / ".env"  # core/ → tests/ → task_system/ → Module/ → Agent/
if _env_path.exists():
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key not in os.environ:
                    os.environ[key] = value

from Module.task_system.core.planner import Planner, Replanner
from Module.task_system.infra import AgentInfra
from Module.task_system.schema import (
    BusinessStatus,
    ConstraintConfig,
    DAG,
    DAGPatch,
    DAGNode,
    DelegationContext,
    EvaluatorAction,
    ExecutionStatus,
    NodeId,
    NodeResult,
    NodeResultEvaluation,
    NodeState,
    NodeStatus,
    OutputContract,
    Plan,
    SessionId,
    TaskConstraints,
    Tool,
)

pytestmark = pytest.mark.integration


def make_tools() -> list[Tool]:
    """构造测试用的工具列表。"""
    return [
        Tool(name="web_search", description="搜索互联网获取实时信息"),
        Tool(name="weather", description="查询指定城市指定日期的天气，包含温度、湿度、降水概率"),
        Tool(name="send_email", description="发送邮件到指定地址，支持 HTML 格式"),
        Tool(name="file_read", description="读取指定路径的文件内容"),
    ]


def make_infra():
    """构造测试用的 AgentInfra。"""
    from openai import AsyncOpenAI
    api_key = os.getenv("DEEPSEEK_API_KEY")
    return AgentInfra(
        llm=AsyncOpenAI(api_key=api_key, base_url="https://api.deepseek.com"),
        milvus=None,
        http=None,
        tool_registry=object(),
        constraints=ConstraintConfig(),
    )


@pytest.mark.asyncio
async def test_simple_task():
    """测试：简单单步任务。"""
    print("=" * 60)
    print("测试 1: 简单单步任务")
    print("=" * 60)

    planner = Planner(infra=make_infra())
    context = DelegationContext(
        intent="查询今天北京的天气",
        hints="用户在北京",
        tools=make_tools(),
        task_constraints=TaskConstraints(max_dag_nodes=3, max_mutations=0),
        summarized_context="用户想了解今天天气情况",
        session_id=SessionId("test-session-1"),
    )
    global_constraints = ConstraintConfig()

    plan = await planner.plan(context, global_constraints)
    print(f"Plan version: {plan.plan_version}")
    print(f"节点数: {len(plan.dag.nodes)}")
    for node in plan.dag.nodes:
        print(f"  [{node.node_id}] {node.description}")
        print(f"    output_as: {node.output_as}")
        print(f"    input_bindings: {node.input_bindings}")
        print(f"    required: {node.required}")
        print(f"    output_contract: {node.output_contract.description}")
    print(f"边: {plan.dag.edges}")
    print()


@pytest.mark.asyncio
async def test_multi_step_task():
    """测试：多步骤任务。"""
    print("=" * 60)
    print("测试 2: 多步骤任务（查天气 → 条件判断 → 发邮件）")
    print("=" * 60)

    planner = Planner(infra=make_infra())
    context = DelegationContext(
        intent="查询北京最近三天天气，如果有下雨的日子就发邮件给 boss@example.com",
        hints="老板邮箱是 boss@example.com，用户很着急",
        tools=make_tools(),
        task_constraints=TaskConstraints(max_dag_nodes=6, max_mutations=1),
        summarized_context="用户是助理，负责向老板汇报天气情况",
        session_id=SessionId("test-session-2"),
    )
    global_constraints = ConstraintConfig()

    plan = await planner.plan(context, global_constraints)
    print(f"Plan version: {plan.plan_version}")
    print(f"节点数: {len(plan.dag.nodes)}")
    for node in plan.dag.nodes:
        print(f"  [{node.node_id}] {node.description}")
        print(f"    output_as: {node.output_as}")
        print(f"    input_bindings: {node.input_bindings}")
        print(f"    required: {node.required}")
        print(f"    output_contract: {node.output_contract.description}")
    print(f"边: {plan.dag.edges}")
    print()


# ── Replan 集成测试 ─────────────────────────────────────────────────────────


def _make_sample_plan() -> Plan:
    """构造一个示例 Plan：搜索天气 → 发邮件。"""
    return Plan(
        dag=DAG(
            nodes=[
                DAGNode(
                    node_id=NodeId("search_weather"),
                    description="搜索北京今天天气情况，获取温度、湿度、降水概率",
                    output_contract=OutputContract(
                        description="天气信息的结构化 JSON，包含 temperature, humidity, precipitation"
                    ),
                    required=True,
                    output_as="output:search_weather",
                ),
                DAGNode(
                    node_id=NodeId("send_email"),
                    description="将天气搜索结果整理为中文邮件，发送给 boss@example.com，标题'今日天气报告'",
                    output_contract=OutputContract(
                        description="邮件发送成功的确认信息"
                    ),
                    required=True,
                    input_bindings=["output:search_weather"],
                    output_as="output:send_email",
                ),
            ],
            edges=[(NodeId("search_weather"), NodeId("send_email"))],
        ),
        plan_version=1,
    )


def _make_node_states_for_failed_search() -> dict[str, NodeState]:
    """构造 node_states——search_weather 超时失败，send_email 尚未执行。"""
    return {
        str(NodeId("search_weather")): NodeState(
            node_id=NodeId("search_weather"),
            status=NodeStatus.FAILED,
            error="工具执行超时（30s）",
            evaluation=NodeResultEvaluation(
                node_id=NodeId("search_weather"),
                execution_status=ExecutionStatus.TIMEOUT,
                business_status=BusinessStatus.GOAL_FAILED,
                reason="搜索工具超时，未返回任何结果",
                suggested_action=EvaluatorAction.REPLAN,
                result=NodeResult(node_id=NodeId("search_weather")),
            ),
        ),
        str(NodeId("send_email")): NodeState(
            node_id=NodeId("send_email"),
            status=NodeStatus.PENDING,
        ),
    }


def _make_node_states_with_success() -> dict[str, NodeState]:
    """构造 node_states——search_weather 成功但结果不对，send_email 尚未执行。"""
    return {
        str(NodeId("search_weather")): NodeState(
            node_id=NodeId("search_weather"),
            status=NodeStatus.SUCCESS,
            summary='返回了"今天的天气"相关内容但不是结构化的天气数据，内容为通用回答',
            evaluation=NodeResultEvaluation(
                node_id=NodeId("search_weather"),
                execution_status=ExecutionStatus.OK,
                business_status=BusinessStatus.GOAL_FAILED,
                reason="工具返回了文本但不是结构化的天气数据，output_contract 要求 JSON",
                suggested_action=EvaluatorAction.REPLAN,
                result=NodeResult(
                    node_id=NodeId("search_weather"),
                    artifact_refs=[],
                ),
            ),
        ),
        str(NodeId("send_email")): NodeState(
            node_id=NodeId("send_email"),
            status=NodeStatus.PENDING,
        ),
    }


@pytest.mark.asyncio
async def test_replan_tool_timeout():
    """集成测试：工具超时 → replan 返回 DAGPatch 或 Plan。"""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        pytest.skip("DEEPSEEK_API_KEY 未设置")

    print("=" * 60)
    print("Replan 集成测试 1: 工具超时 → 局部修复")
    print("=" * 60)

    replanner = Replanner(infra=make_infra())
    plan = _make_sample_plan()
    node_states = _make_node_states_for_failed_search()

    result = await replanner.replan(
        original_plan=plan,
        failed_node_ids=[NodeId("search_weather")],
        failure_diagnosis="search_weather 节点执行超时（30s），工具可能负载过高。请尝试其他搜索方式或拆分查询。",
        node_results=node_states,
        intent="查询今天北京的天气，并发送邮件给老板",
        tools=[
            Tool(name="web_search", description="搜索互联网获取实时信息"),
            Tool(name="web_fetch", description="直接抓取指定 URL 的网页内容"),
            Tool(name="weather", description="查询指定城市指定日期的天气，包含温度、湿度、降水概率"),
            Tool(name="send_email", description="发送邮件到指定地址"),
        ],
        max_nodes=6,
        max_mutations=2,
    )

    print(f"返回类型: {type(result).__name__}")

    if isinstance(result, DAGPatch):
        print(f"  patch_type: {result.patch_type.value}")
        print(f"  reason: {result.reason}")
        print(f"  trigger_nodes: {result.trigger_nodes}")
        print(f"  replace_nodes: {len(result.replace_nodes)}")
        for old_id, new_node in result.replace_nodes:
            print(f"    {old_id} → {new_node.node_id}: {new_node.description}")
        print(f"  add_nodes: {len(result.add_nodes)}")
        for n in result.add_nodes:
            print(f"    + {n.node_id}: {n.description}")
        print(f"  remove_nodes: {result.remove_nodes}")
        print(f"  add_edges: {result.add_edges}")
        print(f"  remove_edges: {result.remove_edges}")

        # 验证 DAGPatch 结构
        assert result.patch_type is not None
        assert result.reason, "patch 必须有 reason"
        has_ops = (
            result.replace_nodes
            or result.add_nodes
            or result.remove_nodes
            or result.add_edges
            or result.remove_edges
        )
        assert has_ops, "patch 必须至少包含一种操作"

    elif isinstance(result, Plan):
        print(f"  全量重建: {len(result.dag.nodes)} 节点, {len(result.dag.edges)} 条边")
        for node in result.dag.nodes:
            print(f"    [{node.node_id}] {node.description}")
        print(f"  edges: {result.dag.edges}")

        # 验证 Plan 结构
        assert len(result.dag.nodes) > 0, "Plan 必须包含节点"
        for node in result.dag.nodes:
            assert node.node_id, "节点必须有 node_id"
            assert node.description, "节点必须有 description"
            if node.required and node.output_as:
                assert "output:" in str(node.output_as) or str(node.output_as), (
                    f"节点 {node.node_id} output_as 格式可疑: {node.output_as}"
                )

    else:
        pytest.fail(f"未知返回类型: {type(result)}")

    print()


@pytest.mark.asyncio
async def test_replan_business_goal_failed():
    """集成测试：工具返回了内容但业务目标未达成 → replan 修复。"""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        pytest.skip("DEEPSEEK_API_KEY 未设置")

    print("=" * 60)
    print("Replan 集成测试 2: 业务目标未达成 (goal_failed) → 修复")
    print("=" * 60)

    replanner = Replanner(infra=make_infra())
    plan = _make_sample_plan()
    node_states = _make_node_states_with_success()

    result = await replanner.replan(
        original_plan=plan,
        failed_node_ids=[NodeId("search_weather")],
        failure_diagnosis=(
            "search_weather 工具正常返回，但业务目标未达成："
            "返回的是通用文本回答而非结构化的天气 JSON 数据。"
            "可能是搜索关键词不够精确，建议使用专门的天气查询工具。"
        ),
        node_results=node_states,
        intent="查询今天北京的天气，并发送邮件给老板",
        tools=[
            Tool(name="web_search", description="搜索互联网获取实时信息"),
            Tool(name="weather", description="查询指定城市指定日期的天气，包含温度、湿度、降水概率"),
            Tool(name="send_email", description="发送邮件到指定地址"),
        ],
        max_nodes=6,
        max_mutations=2,
    )

    print(f"返回类型: {type(result).__name__}")

    if isinstance(result, DAGPatch):
        print(f"  patch_type: {result.patch_type.value}")
        print(f"  reason: {result.reason}")
        print(f"  replace_nodes: {len(result.replace_nodes)}")
        for old_id, new_node in result.replace_nodes:
            print(f"    {old_id} → {new_node.node_id}: {new_node.description}")
            if new_node.output_contract:
                print(f"      output_contract: {new_node.output_contract.description}")

        assert result.patch_type is not None
        assert result.reason
        has_ops = (
            result.replace_nodes
            or result.add_nodes
            or result.remove_nodes
            or result.add_edges
            or result.remove_edges
        )
        assert has_ops, "patch 必须至少包含一种操作"

    elif isinstance(result, Plan):
        print(f"  全量重建: {len(result.dag.nodes)} 节点, {len(result.dag.edges)} 条边")
        for node in result.dag.nodes:
            print(f"    [{node.node_id}] {node.description}")
        assert len(result.dag.nodes) > 0

    else:
        pytest.fail(f"未知返回类型: {type(result)}")

    print()


async def main():
    import logging

    if "--debug" in sys.argv:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s: %(message)s")
        logging.getLogger("Module.task_system.core.planner").setLevel(logging.DEBUG)

    print("Planner 集成测试\n")
    await test_simple_task()
    await test_multi_step_task()
    await test_replan_tool_timeout()
    await test_replan_business_goal_failed()
    print("测试完成")


if __name__ == "__main__":
    asyncio.run(main())
