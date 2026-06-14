# task_system 测试拆分 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `test_executor.py` 和 `test_delegation_preparer.py` 中的单元测试拆分到独立的 `*_unit.py` 文件

**Architecture:** 纯文件拆分——单元测试移到 `_unit.py`，集成测试留在原文件。遵循已有的 `test_planner.py` + `test_planner_unit.py` 命名约定。

**Tech Stack:** pytest, unittest.mock

---

## 文件变更概览

| 操作 | 文件 |
|------|------|
| 新建 | `Module/task_system/tests/core/test_executor_unit.py` |
| 修改 | `Module/task_system/tests/core/test_executor.py` |
| 新建 | `Module/task_system/tests/core/test_delegation_preparer_unit.py` |
| 修改 | `Module/task_system/tests/core/test_delegation_preparer.py` |

---

### Task 1: 创建 test_executor_unit.py（新建文件）

**Files:**
- Create: `Module/task_system/tests/core/test_executor_unit.py`
- Read: `Module/task_system/tests/core/test_executor.py:1-356`

将 `test_executor.py` 的单元测试部分（第 1-356 行）移入新文件。这些是：
- mock-based fixtures（mock_artifact_store, mock_llm_client, mock_tool_registry, mock_infra, mock_hitl, executor, sample_node, sample_tools）
- 6 个 async 单元测试（test_execute_success, test_execute_tool_match_failure, test_execute_tool_error, test_upstream_inputs_flow, test_execute_exception_isolation, test_node_without_output_as）
- 2 个同步单元测试（test_error_mapping, test_error_mapping_none_error_type）

- [ ] **Step 1: 写入 test_executor_unit.py**

```python
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
```

- [ ] **Step 2: 验证新文件语法正确**

```bash
python -c "import py_compile; py_compile.compile('Module/task_system/tests/core/test_executor_unit.py', doraise=True)"
```

- [ ] **Step 3: 运行新文件单元测试确认通过**

```bash
python -m pytest Module/task_system/tests/core/test_executor_unit.py -v
```

Expected: 8 passed

- [ ] **Step 4: Commit**

```bash
git add Module/task_system/tests/core/test_executor_unit.py
git commit -m "test: 拆分 Executor 单元测试到 test_executor_unit.py"
```

---

### Task 2: 裁剪 test_executor.py 为纯集成测试

**Files:**
- Modify: `Module/task_system/tests/core/test_executor.py`

删除第 1-356 行的单元测试内容，保留第 357-479 行的集成测试。替换整个文件内容。

- [ ] **Step 1: 用集成专用内容重写 test_executor.py**

```python
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
```

- [ ] **Step 2: 验证修改后文件语法正确**

```bash
python -c "import py_compile; py_compile.compile('Module/task_system/tests/core/test_executor.py', doraise=True)"
```

- [ ] **Step 3: 运行集成测试确认跳过逻辑正确（无 API key 时 skip）**

```bash
python -m pytest Module/task_system/tests/core/test_executor.py -v
```

Expected: 2 skipped ("DEEPSEEK_API_KEY 未设置") 或 2 passed（有 key）

- [ ] **Step 4: Commit**

```bash
git add Module/task_system/tests/core/test_executor.py
git commit -m "test: 裁剪 test_executor.py 为纯集成测试"
```

---

### Task 3: 创建 test_delegation_preparer_unit.py（新建文件）

**Files:**
- Create: `Module/task_system/tests/core/test_delegation_preparer_unit.py`
- Read: `Module/task_system/tests/core/test_delegation_preparer.py:1-687`

将 `test_delegation_preparer.py` 的单元测试部分（第 1-687 行）移入新文件。包括：
- env 加载
- 3 个 fixtures（sample_tools, sample_session_messages, preparer）
- 7 个测试类：TestStripMarkdownFence, TestParseResponse, TestValidateOutput, TestBuildResult, TestFormatTools, TestFormatSessionMessages, TestFormatUserProfile
- TestPrepareIntegration 类（mock LLM 的 prepare() 测试）

- [ ] **Step 1: 写入 test_delegation_preparer_unit.py**

步骤如下：

```bash
# 提取第 1-687 行（单元测试部分）写入新文件
sed -n '1,687p' Module/task_system/tests/core/test_delegation_preparer.py > Module/task_system/tests/core/test_delegation_preparer_unit.py

# 修改第一行 docstring
sed -i '1c\"""DelegationPreparer 单元测试——不依赖真实 LLM，测试解析、校验、格式化、集成（mock LLM）。"""' Module/task_system/tests/core/test_delegation_preparer_unit.py
```

- [ ] **Step 2: 运行单元测试确认通过**

```bash
python -m pytest Module/task_system/tests/core/test_delegation_preparer_unit.py -v
```

Expected: ~30 passed

- [ ] **Step 3: Commit**

```bash
git add Module/task_system/tests/core/test_delegation_preparer_unit.py
git commit -m "test: 拆分 DelegationPreparer 单元测试到 test_delegation_preparer_unit.py"
```

**注意:** 由于此文件太大（686 行），实际执行时用 `sed -n '1,686p'` 提取 + 修改第一行 docstring，或直接复制原文件再截断。

---

### Task 4: 裁剪 test_delegation_preparer.py 为纯集成测试

**Files:**
- Modify: `Module/task_system/tests/core/test_delegation_preparer.py`

删除第 1-687 行的单元测试内容，保留第 688-1079 行的集成测试。替换整个文件内容。

- [ ] **Step 1: 用集成专用内容重写 test_delegation_preparer.py**

```python
"""DelegationPreparer 集成测试——需要 DEEPSEEK_API_KEY，真实 LLM 调用。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# 加载项目根目录的 .env（CI/本地开发环境）
_env_path = Path(__file__).resolve().parents[4] / ".env"
if _env_path.exists():
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _value = _line.partition("=")
                _key = _key.strip()
                _value = _value.strip().strip('"').strip("'")
                if _key not in os.environ:
                    os.environ[_key] = _value

from Module.task_system.delegation_preparer import DelegationPreparer
from Module.task_system.schema import (
    ConstraintConfig,
    DelegationStatus,
    SessionId,
    Tool,
    UserProfile,
)


def _make_integration_tools() -> list[Tool]:
    """构造集成测试用的工具列表——贴合真实工具描述风格。"""
    return [
        Tool(
            name="web_search",
            description="搜索互联网获取实时信息。返回网页标题、URL 和摘要",
            capabilities=["read"],
        ),
        Tool(
            name="weather",
            description="查询指定城市指定日期的天气，包含温度、湿度、降水概率、风速",
            capabilities=["read"],
        ),
        Tool(
            name="send_email",
            description="发送邮件到指定地址。支持纯文本和 HTML 格式，支持抄送和密送",
            capabilities=["mutation"],
        ),
        Tool(
            name="file_read",
            description="读取指定路径的文件内容，支持文本文件和常见编码",
            capabilities=["read"],
        ),
        Tool(
            name="file_write",
            description="向指定路径写入文件内容。支持新建文件和追加模式",
            capabilities=["write"],
        ),
        Tool(
            name="memory_search",
            description="搜索对话记忆，返回与关键词相关的历史信息片段",
            capabilities=["read"],
        ),
        Tool(
            name="get_time",
            description="获取当前日期和时间，支持指定时区（如 Asia/Shanghai）和格式",
            capabilities=["read"],
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_integration_simple_task_ok():
    """真实 LLM：简单清晰的任务 → 应返回 ok + 筛选工具 + 摘要上下文。"""
    dp = DelegationPreparer()
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    result = await dp.prepare(
        intent="查询北京今天和明天的天气",
        hints="用户在北京",
        session_messages=[
            {"role": "user", "content": "今天天气怎么样？"},
            {"role": "assistant", "content": "请问你想查哪个城市的天气？"},
            {"role": "user", "content": "北京的"},
        ],
        user_profile=UserProfile(preferences="简洁回答"),
        tools=_make_integration_tools(),
        global_constraints=ConstraintConfig(),
        session_id=SessionId("integration-test-1"),
    )

    print(f"\n    status: {result.status.value}")
    print(f"    reason: {result.reason}")
    if result.context:
        print(f"    tools: {[t.name for t in result.context.tools]}")
        print(f"    summarized_context: {result.context.summarized_context[:200]}...")
        tc = result.context.task_constraints
        print(f"    max_dag_nodes: {tc.max_dag_nodes}")
        print(f"    max_mutations: {tc.max_mutations}")

    assert result.status == DelegationStatus.OK, (
        f"期望 ok，实际 {result.status.value}，reason={result.reason}"
    )
    assert result.context is not None
    tool_names = [t.name for t in result.context.tools]
    assert "weather" in tool_names, f"weather 应该在筛选结果中，实际: {tool_names}"
    assert "send_email" not in tool_names and "file_write" not in tool_names, (
        f"简单查询不应该选 mutation 工具，实际: {tool_names}"
    )
    assert len(result.context.summarized_context) > 10, "上下文摘要不应过短"
    assert result.context.task_constraints.max_dag_nodes is not None
    assert result.context.task_constraints.max_dag_nodes <= 5, (
        f"简单任务不应估算太多 DAG 节点: {result.context.task_constraints.max_dag_nodes}"
    )
    assert result.context.intent == "查询北京今天和明天的天气"
    assert result.context.hints == "用户在北京"
    assert result.context.session_id == SessionId("integration-test-1")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_integration_multi_step_with_mutation():
    """真实 LLM：多步骤任务包含敏感操作 → 筛选相关工具 + 估算 mutation 约束。"""
    dp = DelegationPreparer()
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    result = await dp.prepare(
        intent="查询北京最近三天天气，如果有下雨的日子就发邮件给 boss@example.com 提醒带伞",
        hints="老板邮箱是 boss@example.com",
        session_messages=[
            {"role": "user", "content": "明天可能需要提醒老板带伞"},
        ],
        user_profile=UserProfile(),
        tools=_make_integration_tools(),
        global_constraints=ConstraintConfig(),
        session_id=SessionId("integration-test-2"),
    )

    print(f"\n    status: {result.status.value}")
    print(f"    reason: {result.reason}")
    if result.context:
        print(f"    tools: {[t.name for t in result.context.tools]}")
        print(f"    summarized_context: {result.context.summarized_context[:200]}...")
        tc = result.context.task_constraints
        print(f"    max_dag_nodes: {tc.max_dag_nodes}")
        print(f"    max_mutations: {tc.max_mutations}")
        print(f"    max_total_tokens: {tc.max_total_tokens}")

    assert result.status == DelegationStatus.OK, (
        f"期望 ok，实际 {result.status.value}，reason={result.reason}"
    )
    assert result.context is not None
    tool_names = [t.name for t in result.context.tools]
    assert "weather" in tool_names, f"应该包含 weather，实际: {tool_names}"
    assert "send_email" in tool_names, f"应该包含 send_email，实际: {tool_names}"
    assert result.context.task_constraints.max_mutations is not None
    assert result.context.task_constraints.max_mutations >= 1, (
        f"有发邮件操作，max_mutations 应 ≥ 1，实际: {result.context.task_constraints.max_mutations}"
    )
    assert result.context.task_constraints.max_dag_nodes is not None
    assert result.context.task_constraints.max_dag_nodes >= 2, (
        f"多步任务应有多个节点，实际: {result.context.task_constraints.max_dag_nodes}"
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_integration_too_vague():
    """真实 LLM：极其模糊的意图 → 应返回 too_vague + suggestion。"""
    dp = DelegationPreparer()
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    result = await dp.prepare(
        intent="帮我弄一下那个东西",
        hints=None,
        session_messages=[],
        user_profile=UserProfile(),
        tools=_make_integration_tools(),
        global_constraints=ConstraintConfig(),
        session_id=SessionId("integration-test-3"),
    )

    print(f"\n    status: {result.status.value}")
    print(f"    reason: {result.reason}")
    print(f"    suggestion: {result.suggestion}")

    assert result.status == DelegationStatus.TOO_VAGUE, (
        f"期望 too_vague，实际 {result.status.value}，reason={result.reason}"
    )
    assert result.context is None
    assert result.suggestion is not None, "too_vague 应有澄清建议"
    assert len(result.suggestion) > 5, "澄清建议不应过短"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_integration_no_matching_tools():
    """真实 LLM：意图清晰但工具库完全无法支持 → 应返回 no_matching_tools。"""
    dp = DelegationPreparer()
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    limited_tools = [
        Tool(name="get_time", description="获取当前时间", capabilities=["read"]),
        Tool(name="memory_search", description="搜索记忆", capabilities=["read"]),
    ]

    result = await dp.prepare(
        intent="创建一个 PostgreSQL 数据库，导入 CSV 数据，并建立索引",
        hints=None,
        session_messages=[],
        user_profile=UserProfile(),
        tools=limited_tools,
        global_constraints=ConstraintConfig(),
        session_id=SessionId("integration-test-4"),
    )

    print(f"\n    status: {result.status.value}")
    print(f"    reason: {result.reason}")
    print(f"    missing_tools: {result.missing_tools}")

    assert result.status == DelegationStatus.NO_MATCHING_TOOLS, (
        f"期望 no_matching_tools，实际 {result.status.value}，reason={result.reason}"
    )
    assert result.context is None
    assert len(result.missing_tools) > 0, "no_matching_tools 应列出缺少的工具"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_integration_context_summarization():
    """真实 LLM：验证从对话历史中提取上下文摘要的能力。"""
    dp = DelegationPreparer()
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    result = await dp.prepare(
        intent="查天气",
        hints=None,
        session_messages=[
            {"role": "user", "content": "我下周要去上海出差"},
            {"role": "assistant", "content": "好的，需要我帮你准备什么吗？"},
            {"role": "user", "content": "帮我查一下上海的天气，我还要订酒店"},
            {"role": "assistant", "content": "上海最近天气多变，建议带伞。你已经定了酒店吗？"},
            {"role": "user", "content": "还没订，先查天气吧，下雨的话就不去了"},
        ],
        user_profile=UserProfile(preferences="喜欢详细报告", city="北京"),
        tools=_make_integration_tools(),
        global_constraints=ConstraintConfig(),
        session_id=SessionId("integration-test-5"),
    )

    print(f"\n    status: {result.status.value}")
    print(f"    reason: {result.reason}")
    if result.context:
        print(f"    tools: {[t.name for t in result.context.tools]}")
        print(f"    summarized_context:\n{result.context.summarized_context}")

    assert result.status == DelegationStatus.OK, (
        f"期望 ok，实际 {result.status.value}，reason={result.reason}"
    )
    assert result.context is not None
    ctx = result.context.summarized_context
    assert "上海" in ctx, f"上下文摘要应提及上海: {ctx[:100]}"
    tool_names = [t.name for t in result.context.tools]
    assert "weather" in tool_names, f"应该包含 weather，实际: {tool_names}"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_integration_user_profile_influence():
    """真实 LLM：验证用户画像信息被纳入考量。"""
    dp = DelegationPreparer()
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    result = await dp.prepare(
        intent="搜索最新的 AI 安全新闻，整理成摘要写到一个文件里",
        hints="关注大模型安全和对齐方面的进展",
        session_messages=[],
        user_profile=UserProfile(
            role="技术总监",
            preferences="喜欢详细的报告，包含数据和图表",
            language="zh-CN",
        ),
        tools=_make_integration_tools(),
        global_constraints=ConstraintConfig(),
        session_id=SessionId("integration-test-6"),
    )

    print(f"\n    status: {result.status.value}")
    print(f"    reason: {result.reason}")
    if result.context:
        print(f"    tools: {[t.name for t in result.context.tools]}")
        tc = result.context.task_constraints
        print(f"    max_dag_nodes: {tc.max_dag_nodes}")

    assert result.status == DelegationStatus.OK, (
        f"期望 ok，实际 {result.status.value}，reason={result.reason}"
    )
    assert result.context is not None
    tool_names = [t.name for t in result.context.tools]
    assert len(tool_names) >= 1, f"至少应选出 1 个工具，实际: {tool_names}"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_integration_tool_object_integrity():
    """真实 LLM：验证返回的 Tool 对象保留完整的 description 和 capabilities。"""
    dp = DelegationPreparer()
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    result = await dp.prepare(
        intent="搜索一下 Python 异步编程最佳实践",
        hints=None,
        session_messages=[],
        user_profile=UserProfile(),
        tools=_make_integration_tools(),
        global_constraints=ConstraintConfig(),
        session_id=SessionId("integration-test-7"),
    )

    print(f"\n    status: {result.status.value}")
    print(f"    reason: {result.reason}")
    if result.context:
        for t in result.context.tools:
            print(f"    Tool: name={t.name}, desc={t.description[:50]}, caps={t.capabilities}")

    assert result.status == DelegationStatus.OK, (
        f"期望 ok，实际 {result.status.value}"
    )
    assert result.context is not None
    for tool in result.context.tools:
        assert tool.description, (
            f"Tool {tool.name} 的 description 不应为空（应保留原始 Tool 对象的描述）"
        )
        assert isinstance(tool.capabilities, list), (
            f"Tool {tool.name} 的 capabilities 应为 list"
        )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_integration_empty_session_messages():
    """真实 LLM：无对话历史时也能正常工作。"""
    dp = DelegationPreparer()
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    result = await dp.prepare(
        intent="现在几点了",
        hints=None,
        session_messages=[],
        user_profile=UserProfile(),
        tools=_make_integration_tools(),
        global_constraints=ConstraintConfig(),
        session_id=SessionId("integration-test-8"),
    )

    print(f"\n    status: {result.status.value}")
    print(f"    reason: {result.reason}")
    if result.context:
        print(f"    tools: {[t.name for t in result.context.tools]}")

    assert result.status == DelegationStatus.OK, (
        f"简单时间查询应返回 ok，实际 {result.status.value}"
    )
    assert result.context is not None
    tool_names = [t.name for t in result.context.tools]
    assert "get_time" in tool_names, f"应该包含 get_time，实际: {tool_names}"
```

- [ ] **Step 2: 运行集成测试确认跳过逻辑正确**

```bash
python -m pytest Module/task_system/tests/core/test_delegation_preparer.py -v
```

Expected: 8 skipped 或 8 passed（有 key）

- [ ] **Step 3: Commit**

```bash
git add Module/task_system/tests/core/test_delegation_preparer.py
git commit -m "test: 裁剪 test_delegation_preparer.py 为纯集成测试"
```

---

### Task 5: 全量验证

- [ ] **Step 1: 运行全部单元测试（不需要 API key）**

```bash
python -m pytest Module/task_system/tests/ -m "not integration" -v
```

Expected: ~200+ passed, 0 failed, 0 skipped

- [ ] **Step 2: 运行全部集成测试（无 API key 时全部 skip）**

```bash
python -m pytest Module/task_system/tests/ -m "integration" -v
```

Expected: 10 skipped（无 key）或 10 passed（有 key）

- [ ] **Step 3: 确认目录结构正确**

```bash
ls -1 Module/task_system/tests/core/
```

Expected: 12 个文件——在原有 10 个基础上新增 `test_executor_unit.py` 和 `test_delegation_preparer_unit.py`

- [ ] **Step 4: 最终 commit**

```bash
git add Module/task_system/tests/
git commit -m "test: task_system 测试拆分完成——单元/集成分离"
```
