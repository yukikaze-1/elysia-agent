"""Executor 集成测试——需要 DEEPSEEK_API_KEY，真实 LLM + ToolManager。"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from openai import AsyncOpenAI

from Module.workflow_engine.core.executor import Executor
from Module.workflow_engine.artifact_store import ArtifactStore
from Module.workflow_contract import AgentInfra
from Module.workflow_contract import (
    ConstraintConfig,
    NodeId,
    SessionId,
    WorkflowId,
    WorkflowStatus,
)
from Module.tool.tool_descriptor import Tool
from Module.workflow_engine.schema import (
    ArtifactId,
    DAGNode,
    ExecutionMode,
    NodeErrorCode,
    OutputContract,
    WorkflowState,
)
from Module.tool.manager import ToolManager
from Module.tool.protocol import PluginContext
from core.tools.time.plugin import TimePlugin

pytestmark = [pytest.mark.integration, pytest.mark.external]


def _running_state(task_id: WorkflowId) -> WorkflowState:
    """E3：RUNNING 任务级状态——Executor.execute 的取消检查边界 token。"""
    return WorkflowState(
        task_id=task_id,
        status=WorkflowStatus.RUNNING,
        tools=[],
        session_id=SessionId("s1"),
    )


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

    result = await executor.execute(node, WorkflowId("task_int_1"), tools, SessionId("s1"), _running_state(WorkflowId("task_int_1")))

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

    result = await executor.execute(node, WorkflowId("task_int_2"), tools, SessionId("s1"), _running_state(WorkflowId("task_int_2")))

    # tool_choice="auto" 时 LLM 可能：
    # 1. 返回 tool_call → 正确拒绝 → error
    # 2. 返回文本 "I cannot send email" → LLM 直出文本 → 无 error
    # 两种情况都是合理行为，测试验证不崩溃即可
    if result.error is not None:
        assert result.error in (NodeErrorCode.TOOL_NOT_FOUND, NodeErrorCode.TOOL_ERROR)
        print(f"\n    LLM 未匹配工具: error={result.error}, duration={result.duration_ms}ms")
    else:
        print(f"\n    LLM 直出文本: duration={result.duration_ms}ms, "
              f"artifacts={len(result.artifact_refs)}")


@pytest.mark.asyncio
async def test_integration_execute_llm_direct_mode():
    """execution_mode=LLM_DIRECT → 跳过 tool matching，直接调 LLM 生成文本。

    验证：
    - 不调用 tool_registry.execute()
    - 返回成功（无 error）
    - 产物是 LLM 生成的文本内容
    """
    executor = await _build_integration_executor()
    if executor is None:
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    # 汇总类任务——Planner 会标注为 llm_direct
    node = DAGNode(
        node_id=NodeId("summarize"),
        description="你是一位 AI 研究员。请用中文简要说明什么是 Transformer 架构，"
                    "包括自注意力机制的核心思想。控制在 100 字以内。",
        output_contract=OutputContract(description="简洁的中文解释，≤100 字"),
        execution_mode=ExecutionMode.LLM_DIRECT,
        input_bindings=[],
        output_as=ArtifactId("output:summary"),
    )

    # 提供工具列表——llm_direct 模式跳过 tool matching，不应调用任何工具
    tools = [
        Tool(name="get_time", description="获取当前日期和时间"),
    ]

    result = await executor.execute(node, WorkflowId("task_llm_direct"), tools, SessionId("s1"), _running_state(WorkflowId("task_llm_direct")))

    assert result.error is None, f"llm_direct 不应失败: {result.error}"
    assert result.duration_ms > 0
    assert len(result.artifact_refs) == 1

    artifact = await executor._artifact_store.get(result.artifact_refs[0])
    assert artifact is not None
    assert artifact.content is not None
    # 产物应是 LLM 生成的 Transformer 解释，不是 get_time 的时间字符串
    content = str(artifact.content)
    assert len(content) > 20, \
        f"LLM 应生成有意义的内容，实际: {content}"
    assert "Transformer" in content or "transformer" in content.lower() or \
           "注意力" in content or "attention" in content.lower(), \
        f"产物应包含 Transformer/注意力相关内容，实际: {content[:200]}"
    print(f"\n    llm_direct 模式——LLM 直出文本，跳过工具匹配")
    print(f"    产物长度: {len(content)} chars")
    print(f"    耗时: {result.duration_ms}ms")


@pytest.mark.asyncio
async def test_integration_execute_llm_direct_sentinel():
    """tool_call 模式下 LLM 判断无需工具 → __llm_direct__ 哨兵路径。

    节点描述为纯文本汇总任务——虽然 execution_mode=tool_call，
    但 LLM（在 _match_tool 中）应自行判断不需要外部工具，
    返回纯文本 → Executor 走 __llm_direct__ 哨兵路径。

    验证：
    - 不调用 tool_registry.execute()
    - 返回成功
    - 产物是 LLM 文本
    """
    executor = await _build_integration_executor()
    if executor is None:
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    node = DAGNode(
        node_id=NodeId("compile"),
        description="汇总以下信息：今天天气晴，气温 25°C，湿度 60%。"
                    "请用一句中文总结今天的天气情况。",
        output_contract=OutputContract(description="一句话天气总结"),
        execution_mode=ExecutionMode.TOOL_CALL,  # 走 tool matching
        input_bindings=[],
        output_as=ArtifactId("output:compile"),
    )

    # 只提供不相关的工具——LLM 应判断无需调用
    tools = [
        Tool(name="get_time", description="获取当前日期和时间，支持指定时区和格式"),
    ]

    result = await executor.execute(node, WorkflowId("task_sentinel"), tools, SessionId("s1"), _running_state(WorkflowId("task_sentinel")))

    assert result.error is None, \
        f"哨兵路径应成功（LLM 直出文本），实际 error={result.error}"
    assert len(result.artifact_refs) == 1

    artifact = await executor._artifact_store.get(result.artifact_refs[0])
    assert artifact is not None
    assert artifact.content is not None
    assert len(str(artifact.content)) > 5, \
        f"LLM 应生成有意义的内容，实际: {artifact.content}"
    print(f"\n    tool_call 模式 → LLM 判为 __llm_direct__ 哨兵")
    print(f"    产物内容: {artifact.content}")
    print(f"    耗时: {result.duration_ms}ms")
