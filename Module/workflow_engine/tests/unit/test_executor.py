"""Executor 单元测试——不依赖 LLM，使用 mock 测试核心流程。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion_message_tool_call import (
    ChatCompletionMessageToolCall,
    Function,
)

from Module.workflow_engine.core.executor import Executor, _check_cancelled
from Module.workflow_engine.tests.helpers.fake_llm import FakeLLM
from Module.workflow_contract import (
    AgentInfra,
    ConstraintConfig,
    NodeId,
    SessionId,
    WorkflowId,
    WorkflowStatus,
)
from Module.tool.tool_descriptor import Tool
from Module.workflow_engine.schema import (
    Artifact,
    ArtifactId,
    DAGNode,
    ExecutionMode,
    NodeErrorCode,
    NodeResult,
    OutputContract,
    TokenComponentType,
    WorkflowState,
)
from Module.tool.protocol import ErrorType, ToolResult

pytestmark = pytest.mark.unit

# ── 测试固件 ────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_artifact_store():
    """Mock ArtifactStore——支持 put/get 操作。"""
    store = MagicMock()
    store._artifacts: dict[str, Artifact] = {}  # type: ignore[reportInvalidTypeForm]
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
            choices=[{  # type: ignore[reportArgumentType]
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
            usage={  # type: ignore[reportArgumentType]
                "prompt_tokens": 150,
                "completion_tokens": 50,
                "total_tokens": 200,
            },
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
        token_ledger=MagicMock(),
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


@pytest.fixture
def running_state():
    """E3：RUNNING 任务级状态——Executor.execute 的取消检查边界 token。

    现有测试（快乐路径）均传此状态——_check_cancelled 不抛。
    """
    return WorkflowState(
        task_id=WorkflowId("task_1"),
        status=WorkflowStatus.RUNNING,
        tools=[],
        session_id=SessionId("s1"),
    )


# ── 测试用例 ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_success(executor, sample_node, sample_tools, mock_tool_registry, running_state):
    """工具匹配成功 + 执行成功 → NodeResult 包含 artifact_refs。"""
    result = await executor.execute(sample_node, WorkflowId("task_1"), sample_tools, SessionId("s1"), running_state)

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
async def test_execute_tool_match_failure(executor, sample_node, sample_tools, mock_llm_client, running_state):
    """LLM 返回空 tool_calls 且空 content → NodeErrorCode.TOOL_NOT_FOUND。"""
    async def _no_tool_calls(**kwargs):
        return ChatCompletion(
            id="test-id",
            choices=[{  # type: ignore[reportArgumentType]
                "index": 0,
                "message": ChatCompletionMessage(
                    role="assistant",
                    content=None,  # 既无 tool_call 也无文本 → 真正的失败
                    tool_calls=None,
                ),
                "finish_reason": "stop",
            }],
            created=1234567890,
            model="deepseek-v4-pro",
            object="chat.completion",
        )

    mock_llm_client.chat.completions.create = AsyncMock(side_effect=_no_tool_calls)

    result = await executor.execute(sample_node, WorkflowId("task_1"), sample_tools, SessionId("s1"), running_state)

    assert result.error == NodeErrorCode.TOOL_NOT_FOUND
    assert len(result.artifact_refs) == 0


@pytest.mark.asyncio
async def test_execute_llm_direct_text(executor, sample_node, sample_tools, mock_llm_client, running_state):
    """LLM 不调工具直接返回文本 → 文本作为有效产物（汇总/分析类节点）。"""
    async def _direct_text(**kwargs):
        return ChatCompletion(
            id="test-id",
            choices=[{  # type: ignore[reportArgumentType]
                "index": 0,
                "message": ChatCompletionMessage(
                    role="assistant",
                    content="汇总结果：AI 安全领域最新进展包括...",
                    tool_calls=None,
                ),
                "finish_reason": "stop",
            }],
            created=1234567890,
            model="deepseek-v4-pro",
            object="chat.completion",
        )

    mock_llm_client.chat.completions.create = AsyncMock(side_effect=_direct_text)

    result = await executor.execute(sample_node, WorkflowId("task_1"), sample_tools, SessionId("s1"), running_state)

    assert result.error is None, f"LLM 直出文本不应为错误，实际: {result.error}"
    assert len(result.artifact_refs) == 1


@pytest.mark.asyncio
async def test_execute_tool_error(executor, sample_node, sample_tools, mock_tool_registry, running_state):
    """工具执行失败 → NodeErrorCode 正确映射。"""
    mock_tool_registry.execute = AsyncMock(return_value=ToolResult.fail(
        name="web_search",
        error="搜索服务暂时不可用",
        duration_ms=200.0,
        error_type=ErrorType.UNAVAILABLE,
    ))

    result = await executor.execute(sample_node, WorkflowId("task_1"), sample_tools, SessionId("s1"), running_state)

    assert result.error == NodeErrorCode.TOOL_ERROR
    assert result.duration_ms >= 0


@pytest.mark.asyncio
async def test_upstream_inputs_flow(executor, mock_artifact_store, sample_tools, running_state):
    """上游产物通过 input_bindings 正确解析。"""
    upstream_artifact = Artifact(
        artifact_id=ArtifactId("upstream_1"),
        artifact_type="text",
        node_id=NodeId("node_0"),
        task_id=WorkflowId("task_1"),
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

    result = await executor.execute(node, WorkflowId("task_1"), sample_tools, SessionId("s1"), running_state)

    assert result.error is None
    mock_artifact_store.get.assert_any_call(ArtifactId("upstream_1"))


@pytest.mark.asyncio
async def test_execute_exception_isolation(executor, sample_node, sample_tools, mock_tool_registry, running_state):
    """Executor 内部异常不抛出——捕获并返回 NodeErrorCode.TOOL_ERROR。"""
    mock_tool_registry.execute = AsyncMock(side_effect=RuntimeError("工具执行崩溃"))

    result = await executor.execute(sample_node, WorkflowId("task_1"), sample_tools, SessionId("s1"), running_state)

    assert result.error == NodeErrorCode.TOOL_ERROR
    assert result.duration_ms >= 0


@pytest.mark.asyncio
async def test_node_without_output_as(executor, sample_tools, running_state):
    """节点没有 output_as → 不存储产物，但执行照常。"""
    node = DAGNode(
        node_id=NodeId("node_no_output"),
        description="执行一个临时操作",
        output_contract=OutputContract(description="临时数据"),
        required=False,
        input_bindings=[],
        output_as=None,
    )

    result = await executor.execute(node, WorkflowId("task_1"), sample_tools, SessionId("s1"), running_state)

    assert result.error is None
    assert len(result.artifact_refs) == 0


# ── TokenLedger 注入与 record 测试 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_token_ledger_record_called(executor, sample_node, sample_tools, running_state):
    """LLM 调用后 token_ledger.record() 被调用，参数正确。"""
    result = await executor.execute(sample_node, WorkflowId("task_1"), sample_tools, SessionId("s1"), running_state)

    assert result.error is None
    # 验证 record 被调用
    token_ledger = executor._token_ledger
    token_ledger.record.assert_called_once()
    call_kwargs = token_ledger.record.call_args.kwargs
    assert call_kwargs["component"] == TokenComponentType.EXECUTOR
    assert call_kwargs["node_id"] == sample_node.node_id
    assert call_kwargs["prompt_tokens"] == 150
    assert call_kwargs["completion_tokens"] == 50
    assert call_kwargs["model"] is not None


@pytest.mark.asyncio
async def test_token_ledger_none_skips_record(mock_infra, mock_artifact_store, mock_hitl, sample_node, sample_tools, running_state):
    """token_ledger 为 None 时不调用 record（不传 ledger 的向后兼容路径）。"""
    executor_no_ledger = Executor(
        infra=mock_infra,
        artifact_store=mock_artifact_store,
        hitl_notifier=mock_hitl,
        token_ledger=None,
    )
    result = await executor_no_ledger.execute(sample_node, WorkflowId("task_1"), sample_tools, SessionId("s1"), running_state)

    assert result.error is None
    assert len(result.artifact_refs) == 1


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


@pytest.mark.asyncio
async def test_execute_llm_direct_mode(executor, sample_tools, running_state):
    """execution_mode=llm_direct 时跳过 tool matching，直接调 LLM。"""
    from Module.workflow_engine.schema import ExecutionMode

    node = DAGNode(
        node_id=NodeId("summarize"),
        description="汇总搜索结果，生成报告",
        output_contract=OutputContract(description="结构化的汇总报告"),
        execution_mode=ExecutionMode.LLM_DIRECT,
        input_bindings=[ArtifactId("output:search")],
        output_as=ArtifactId("output:summarize"),
    )
    result = await executor.execute(node, WorkflowId("t1"), sample_tools, SessionId("s1"), running_state)
    assert result.error is None
    # llm_direct 路径不应调用 tool_registry
    executor._infra.tool_registry.execute.assert_not_called()


# ── _check_cancelled 取消检查（E3 Task 1）──────────────────────────────────


class TestCheckCancelled:
    def test_raises_when_cancelled(self):
        ts = WorkflowState(
            task_id=WorkflowId("t1"),
            status=WorkflowStatus.CANCELLED,
            tools=[],
            session_id=SessionId("s1"),
        )
        with pytest.raises(asyncio.CancelledError):
            _check_cancelled(ts)

    def test_passes_when_not_cancelled(self):
        ts = WorkflowState(
            task_id=WorkflowId("t1"),
            status=WorkflowStatus.RUNNING,
            tools=[],
            session_id=SessionId("s1"),
        )
        _check_cancelled(ts)   # 不抛


# ── Executor 取消语义（E3 Task 3）───────────────────────────────────────────


class TestExecutorCancellation:
    @pytest.mark.asyncio
    async def test_tool_not_called_when_cancelled_before(self):
        # fake LLM 返回 tool 匹配；工具调用前把 ts.status 置 CANCELLED
        fake = FakeLLM()                              # E4 helpers
        infra = AgentInfra(llm=fake, milvus=None, http=None,
                           tool_registry=MagicMock(), constraints=ConstraintConfig())
        executor = Executor(infra=infra, artifact_store=MagicMock(), hitl_notifier=MagicMock())
        ts = WorkflowState(task_id=WorkflowId("t1"), status=WorkflowStatus.RUNNING,
                           tools=[], session_id=SessionId("s1"))
        node = DAGNode(node_id=NodeId("n1"), description="do something",
                       input_bindings=[], output_as=ArtifactId("a1"), execution_mode=ExecutionMode.TOOL_CALL)
        tool = Tool(name="get_time", description="获取当前时间")

        def handler(msgs):
            ts.status = WorkflowStatus.CANCELLED       # 工具调用前取消（在 LLM 匹配返回后、②检查点前）
            return {"type": "tool", "name": "get_time", "arguments": {}}
        fake.handler = handler
        with pytest.raises(asyncio.CancelledError):
            await executor.execute(node, WorkflowId("t1"), [tool], SessionId("s1"), ts)
        infra.tool_registry.execute.assert_not_called()   # 工具未调（②检查点拦截）

    @pytest.mark.asyncio
    async def test_tool_shielded_completes_on_cancel(self):
        # 工具调用中取消外层任务 → shield 让工具完成、CancelledError 上抛
        fake = FakeLLM()
        fake.handler = lambda msgs: {"type": "tool", "name": "get_time", "arguments": {}}
        infra = AgentInfra(llm=fake, milvus=None, http=None,
                           tool_registry=MagicMock(), constraints=ConstraintConfig())
        gate = asyncio.Event()

        async def slow_tool(tool_name, **kw):        # _call_tool 以位置参数 tool_name + kwargs 调 execute
            await gate.wait()
            return "done"
        infra.tool_registry.execute = AsyncMock(side_effect=slow_tool)   # AsyncMock——await_count 断言需要
        executor = Executor(infra=infra, artifact_store=MagicMock(), hitl_notifier=MagicMock())
        ts = WorkflowState(task_id=WorkflowId("t1"), status=WorkflowStatus.RUNNING,
                           tools=[], session_id=SessionId("s1"))
        node = DAGNode(node_id=NodeId("n1"), description="do",
                       input_bindings=[], output_as=ArtifactId("a1"), execution_mode=ExecutionMode.TOOL_CALL)
        tool = Tool(name="get_time", description="获取当前时间")
        outer = asyncio.create_task(executor.execute(node, WorkflowId("t1"), [tool], SessionId("s1"), ts))
        await asyncio.sleep(0.05)                       # 工具已开始（shield 内）
        ts.status = WorkflowStatus.CANCELLED
        outer.cancel()                                  # 取消外层任务
        with pytest.raises(asyncio.CancelledError):
            await outer
        gate.set()                                      # 放行——工具完成（shield），结果弃用
        await asyncio.sleep(0.05)
        assert infra.tool_registry.execute.await_count == 1
