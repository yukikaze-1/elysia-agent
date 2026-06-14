"""Executor 单元测试——不依赖 LLM，使用 mock 测试核心流程。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion_message_tool_call import (
    ChatCompletionMessageToolCall,
    Function,
)

from Module.task_system.core.executor import Executor
from Module.task_system.schema import (
    Artifact,
    ArtifactId,
    DAGNode,
    NodeErrorCode,
    NodeId,
    NodeResult,
    OutputContract,
    TaskId,
    Tool,
)
from Module.tool.protocol import ErrorType, ToolResult


# ── 测试固件 ────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_artifact_store():
    """Mock ArtifactStore——支持 put/get 操作。"""
    store = MagicMock()
    store._artifacts: dict[str, Artifact] = {}
    store.put = AsyncMock(side_effect=lambda a: store._artifacts.update({a.artifact_id: a}))
    store.get = AsyncMock(side_effect=lambda aid: store._artifacts.get(aid))
    store.get_by_node = AsyncMock(return_value=[])
    store.get_by_task = AsyncMock(return_value=[])
    return store


@pytest.fixture
def mock_llm_client():
    """Mock AsyncOpenAI client——返回预设的 tool_call。"""
    client = MagicMock()

    async def _create_response(**kwargs):
        """根据 tools 参数生成模拟的 function-calling 响应。"""
        tools = kwargs.get("tools", [])
        tool_name = tools[0]["function"]["name"] if tools else "unknown"

        return ChatCompletion(
            id="test-id",
            choices=[{
                "index": 0,
                "message": ChatCompletionMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ChatCompletionMessageToolCall(
                            id="call_test_1",
                            type="function",
                            function=Function(
                                name=tool_name,
                                arguments='{"query": "test query", "limit": 5}',
                            ),
                        )
                    ],
                ),
                "finish_reason": "tool_calls",
            }],
            created=1234567890,
            model="deepseek-v4-pro",
            object="chat.completion",
        )

    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=_create_response)
    return client


@pytest.fixture
def mock_tool_registry():
    """Mock ToolRegistry——支持 definitions() 和 execute()。"""
    registry = MagicMock()
    registry.definitions = MagicMock(return_value=[
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "搜索互联网获取实时信息",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词"},
                        "limit": {"type": "integer", "description": "返回条数"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "send_email",
                "description": "发送电子邮件",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string"},
                        "subject": {"type": "string"},
                        "body": {"type": "string"},
                    },
                    "required": ["to", "subject", "body"],
                },
            },
        },
    ])
    registry.execute = AsyncMock(return_value=ToolResult.ok(
        name="web_search",
        content="搜索完成：找到 5 条结果",
        duration_ms=150.0,
    ))
    return registry


@pytest.fixture
def mock_infra(mock_llm_client, mock_tool_registry):
    """Mock AgentInfra——聚合 llm + tool_registry。"""
    infra = MagicMock()
    infra.llm = mock_llm_client
    infra.tool_registry = mock_tool_registry
    infra.constraints = MagicMock()
    return infra


@pytest.fixture
def mock_hitl():
    """Mock HITLNotifier——v1 不调用，仅占位。"""
    return MagicMock()


@pytest.fixture
def executor(mock_infra, mock_artifact_store, mock_hitl):
    """创建被测 Executor 实例。"""
    return Executor(
        infra=mock_infra,
        artifact_store=mock_artifact_store,
        hitl_notifier=mock_hitl,
    )


@pytest.fixture
def sample_node():
    """创建一个示例 DAGNode。"""
    return DAGNode(
        node_id=NodeId("node_1"),
        description="搜索今天的天气",
        output_contract=OutputContract(
            description="天气信息的 JSON 对象",
        ),
        required=True,
        input_bindings=[],
        output_as=ArtifactId("artifact_weather"),
    )


@pytest.fixture
def sample_tools():
    """创建示例工具列表。"""
    return [
        Tool(name="web_search", description="搜索互联网获取实时信息"),
        Tool(name="send_email", description="发送电子邮件"),
    ]


# ── 测试用例 ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_success(executor, sample_node, sample_tools, mock_tool_registry):
    """工具匹配成功 + 执行成功 → NodeResult 包含 artifact_refs。"""
    result = await executor.execute(sample_node, TaskId("task_1"), sample_tools)

    assert isinstance(result, NodeResult)
    assert result.node_id == NodeId("node_1")
    assert result.error is None
    assert result.duration_ms >= 0
    assert len(result.artifact_refs) == 1
    assert result.artifact_refs[0] == ArtifactId("artifact_weather")

    mock_tool_registry.execute.assert_called_once()
    call_args = mock_tool_registry.execute.call_args
    assert call_args[0][0] == "web_search"
    assert call_args[1]["query"] == "test query"
    assert call_args[1]["limit"] == 5


@pytest.mark.asyncio
async def test_execute_tool_match_failure(executor, sample_node, sample_tools, mock_llm_client):
    """LLM 返回空 tool_calls → NodeErrorCode.TOOL_NOT_FOUND。"""
    async def _no_tool_calls(**kwargs):
        return ChatCompletion(
            id="test-id",
            choices=[{
                "index": 0,
                "message": ChatCompletionMessage(
                    role="assistant",
                    content="I cannot find a suitable tool.",
                    tool_calls=None,
                ),
                "finish_reason": "stop",
            }],
            created=1234567890,
            model="deepseek-v4-pro",
            object="chat.completion",
        )

    mock_llm_client.chat.completions.create = AsyncMock(side_effect=_no_tool_calls)

    result = await executor.execute(sample_node, TaskId("task_1"), sample_tools)

    assert result.error == NodeErrorCode.TOOL_NOT_FOUND
    assert len(result.artifact_refs) == 0


@pytest.mark.asyncio
async def test_execute_tool_error(executor, sample_node, sample_tools, mock_tool_registry):
    """工具执行失败 → NodeErrorCode 正确映射。"""
    mock_tool_registry.execute = AsyncMock(return_value=ToolResult.fail(
        name="web_search",
        error="搜索服务暂时不可用",
        duration_ms=200.0,
        error_type=ErrorType.UNAVAILABLE,
    ))

    result = await executor.execute(sample_node, TaskId("task_1"), sample_tools)

    assert result.error == NodeErrorCode.TOOL_ERROR
    assert result.duration_ms >= 0


@pytest.mark.asyncio
async def test_upstream_inputs_flow(executor, mock_artifact_store, sample_tools):
    """上游产物通过 input_bindings 正确解析。"""
    upstream_artifact = Artifact(
        artifact_id=ArtifactId("upstream_1"),
        artifact_type="text",
        node_id=NodeId("node_0"),
        task_id=TaskId("task_1"),
        content={"city": "北京", "date": "2026-06-13"},
    )
    mock_artifact_store._artifacts["upstream_1"] = upstream_artifact

    node = DAGNode(
        node_id=NodeId("node_2"),
        description="根据城市信息发送邮件",
        output_contract=OutputContract(description="邮件发送结果"),
        required=True,
        input_bindings=[ArtifactId("upstream_1")],
        output_as=ArtifactId("artifact_email"),
    )

    result = await executor.execute(node, TaskId("task_1"), sample_tools)

    assert result.error is None
    mock_artifact_store.get.assert_any_call(ArtifactId("upstream_1"))


@pytest.mark.asyncio
async def test_execute_exception_isolation(executor, sample_node, sample_tools, mock_tool_registry):
    """Executor 内部异常不抛出——捕获并返回 NodeErrorCode.TOOL_ERROR。"""
    mock_tool_registry.execute = AsyncMock(side_effect=RuntimeError("工具执行崩溃"))

    result = await executor.execute(sample_node, TaskId("task_1"), sample_tools)

    assert result.error == NodeErrorCode.TOOL_ERROR
    assert result.duration_ms >= 0


@pytest.mark.asyncio
async def test_node_without_output_as(executor, sample_tools):
    """节点没有 output_as → 不存储产物，但执行照常。"""
    node = DAGNode(
        node_id=NodeId("node_no_output"),
        description="执行一个临时操作",
        output_contract=OutputContract(description="临时数据"),
        required=False,
        input_bindings=[],
        output_as=None,
    )

    result = await executor.execute(node, TaskId("task_1"), sample_tools)

    assert result.error is None
    assert len(result.artifact_refs) == 0


# ── 错误映射单元测试 ────────────────────────────────────────────────────────


def test_error_mapping():
    """静态方法 _map_error 覆盖所有 ErrorType → NodeErrorCode 映射。"""
    from Module.tool.protocol import ErrorType, ToolResult

    test_cases = [
        (ErrorType.INVALID_ARGUMENT, NodeErrorCode.TOOL_ERROR),
        (ErrorType.TIMEOUT, NodeErrorCode.TOOL_TIMEOUT),
        (ErrorType.PERMISSION_DENIED, NodeErrorCode.PERMISSION_DENIED),
        (ErrorType.RATE_LIMITED, NodeErrorCode.TOOL_ERROR),
        (ErrorType.NETWORK_ERROR, NodeErrorCode.NETWORK_ERROR),
        (ErrorType.UNAVAILABLE, NodeErrorCode.TOOL_ERROR),
        (ErrorType.CANCELLED, NodeErrorCode.TOOL_ERROR),
        (ErrorType.TOOL_ERROR, NodeErrorCode.TOOL_ERROR),
        (ErrorType.UNKNOWN_TOOL, NodeErrorCode.TOOL_NOT_FOUND),
    ]

    for tool_error, expected_node_error in test_cases:
        tr = ToolResult.fail(
            name="test_tool",
            error="test error",
            error_type=tool_error,
        )
        mapped = Executor._map_error(tr)
        assert mapped == expected_node_error, (
            f"ErrorType.{tool_error.name} → {mapped}, 期望 {expected_node_error}"
        )


def test_error_mapping_none_error_type():
    """error_type 为 None 时默认映射为 TOOL_ERROR。"""
    from Module.tool.protocol import ToolResult

    tr = ToolResult.fail(name="test_tool", error="unknown error", error_type=None)
    mapped = Executor._map_error(tr)
    assert mapped == NodeErrorCode.TOOL_ERROR
