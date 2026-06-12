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

from Module.task_system.core.planner import Planner
from Module.task_system.schema import (
    ConstraintConfig,
    DelegationContext,
    SessionId,
    TaskConstraints,
    Tool,
)


def make_tools() -> list[Tool]:
    """构造测试用的工具列表。"""
    return [
        Tool(name="web_search", description="搜索互联网获取实时信息"),
        Tool(name="weather", description="查询指定城市指定日期的天气，包含温度、湿度、降水概率"),
        Tool(name="send_email", description="发送邮件到指定地址，支持 HTML 格式"),
        Tool(name="file_read", description="读取指定路径的文件内容"),
    ]


@pytest.mark.asyncio
async def test_simple_task():
    """测试：简单单步任务。"""
    print("=" * 60)
    print("测试 1: 简单单步任务")
    print("=" * 60)

    planner = Planner()
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

    planner = Planner()
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


async def main():
    import logging

    if "--debug" in sys.argv:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s: %(message)s")
        logging.getLogger("Module.task_system.core.planner").setLevel(logging.DEBUG)

    print("Planner 集成测试\n")
    await test_simple_task()
    await test_multi_step_task()
    print("测试完成")


if __name__ == "__main__":
    asyncio.run(main())
