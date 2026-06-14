"""Executor 集成测试——需要 DEEPSEEK_API_KEY，真实 LLM + ToolManager。"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from openai import AsyncOpenAI

from Module.task_system.core.executor import Executor
from Module.task_system.artifact_store import ArtifactStore
from Module.task_system.infra import AgentInfra
from Module.task_system.schema import (
    ArtifactId,
    ConstraintConfig,
    DAGNode,
    NodeErrorCode,
    NodeId,
    OutputContract,
    TaskId,
    Tool,
)
from Module.tool.manager import ToolManager
from Module.tool.protocol import PluginContext
from core.tools.time.plugin import TimePlugin


async def _build_integration_executor():
    """构建集成测试 Executor——真实 LLM + 加载 TimePlugin 的 ToolManager。

    需要 DEEPSEEK_API_KEY 环境变量。如果未设置，返回 None。
    """
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return None

    llm = AsyncOpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
    )

    tm = ToolManager()
    await tm.load_all([TimePlugin], PluginContext(session_id="test"))

    infra = AgentInfra(
        llm=llm,
        milvus=None,
        http=None,
        tool_registry=tm,
        constraints=ConstraintConfig(),
    )

    artifact_store = ArtifactStore()
    hitl_notifier = MagicMock()

    return Executor(
        infra=infra,
        artifact_store=artifact_store,
        hitl_notifier=hitl_notifier,
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_integration_execute_time_tool():
    """集成测试：真实 LLM 匹配 "获取当前时间" → TimePlugin 执行 → 返回结果。

    验证完整链路：LLM function-calling 选工具 + 填参数 → ToolManager 调用 → 产物存储。
    需要 DEEPSEEK_API_KEY 环境变量。
    """
    executor = await _build_integration_executor()
    if executor is None:
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    node = DAGNode(
        node_id=NodeId("node_time"),
        description="获取当前北京时间，human 格式",
        output_contract=OutputContract(description="当前时间字符串"),
        required=True,
        input_bindings=[],
        output_as=ArtifactId("current_time"),
    )

    tools = [
        Tool(name="get_time", description="获取当前日期和时间，支持指定时区和格式"),
    ]

    result = await executor.execute(node, TaskId("task_int_1"), tools)

    assert result.error is None, f"执行失败: {result.error}"
    assert result.duration_ms > 0
    assert len(result.artifact_refs) == 1

    artifact = await executor._artifact_store.get(result.artifact_refs[0])
    assert artifact is not None
    assert artifact.artifact_id == ArtifactId("current_time")
    assert artifact.content is not None
    print(f"\n    LLM 匹配工具 → TimePlugin 执行成功")
    print(f"    产物内容: {artifact.content}")
    print(f"    耗时: {result.duration_ms}ms")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_integration_no_matching_tool():
    """集成测试：没有匹配工具时 LLM 行为验证。

    只提供 get_time 工具，但要求发送邮件——验证 Executor 如何处理。
    需要 DEEPSEEK_API_KEY 环境变量。
    """
    executor = await _build_integration_executor()
    if executor is None:
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    node = DAGNode(
        node_id=NodeId("node_unknown"),
        description="发送一封邮件给 boss@example.com，标题'会议纪要'，内容'明天下午3点开会'",
        output_contract=OutputContract(description="邮件发送结果"),
        required=True,
        input_bindings=[],
        output_as=ArtifactId("email_result"),
    )

    tools = [
        Tool(name="get_time", description="获取当前日期和时间，支持指定时区和格式"),
    ]

    result = await executor.execute(node, TaskId("task_int_2"), tools)

    assert result.error is not None, "没有匹配工具时应返回错误"
    assert result.error in (NodeErrorCode.TOOL_NOT_FOUND, NodeErrorCode.TOOL_ERROR)
    print(f"\n    LLM 响应: error={result.error}, duration={result.duration_ms}ms")
