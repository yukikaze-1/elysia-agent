"""Planner 单元测试——不依赖 LLM，测试解析和格式化逻辑。

覆盖 _parse_response（JSON → Plan）、_format_tools（Tool → prompt 文本）、
_parse_replan_response（JSON → DAGPatch | Plan）、_format_nodes_by_status、
token_ledger 注入与 record 调用。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from Module.workflow_engine.core.planner import Planner, Replanner, PlanParseError
from Module.workflow_contract.utils import strip_markdown_fence, format_tools
from Module.workflow_contract import (
    ConstraintConfig,
    DelegationContext,
    NodeId,
    SessionId,
)
from Module.tool.tool_descriptor import Tool
from Module.workflow_engine.schema import (
    ArtifactId,
    DAG,
    DAGPatch,
    DAGPatchType,
    DAGNode,
    NodeState,
    NodeStatus,
    Plan,
    TokenComponentType,
)

pytestmark = pytest.mark.unit

# ── _format_tools 测试 ──────────────────────────────────────────────────────


def test_format_tools_empty():
    """空列表 → 占位文本。"""
    result = format_tools([], include_capabilities=False)
    assert "无可用工具" in result


def test_format_tools_single():
    """单个工具 → 一行 name: description。"""
    tools = [Tool(name="web_search", description="搜索互联网")]
    result = format_tools(tools, include_capabilities=False)
    assert result == "- web_search: 搜索互联网"


def test_format_tools_multiple():
    """多个工具 → 每行一个。"""
    tools = [
        Tool(name="web_search", description="搜索互联网"),
        Tool(name="send_email", description="发送邮件"),
    ]
    result = format_tools(tools, include_capabilities=False)
    lines = result.split("\n")
    assert len(lines) == 2
    assert lines[0] == "- web_search: 搜索互联网"
    assert lines[1] == "- send_email: 发送邮件"


def test_format_tools_no_description():
    """工具无 description → 只显示名称。"""
    tools = [Tool(name="get_time", description="")]
    result = format_tools(tools, include_capabilities=False)
    assert result == "- get_time"


# ── _strip_markdown_fence 测试 ──────────────────────────────────────────────


def test_strip_no_fence():
    """无代码块 → 原样返回。"""
    result = strip_markdown_fence('{"nodes": []}')
    assert result == '{"nodes": []}'


def test_strip_json_fence():
    """"```json ... ```" → 去掉包裹。"""
    result = strip_markdown_fence('```json\n{"nodes": []}\n```')
    assert result == '{"nodes": []}'


def test_strip_plain_fence():
    """"``` ... ```"（无语言标记）→ 去掉包裹。"""
    result = strip_markdown_fence('```\n{"nodes": []}\n```')
    assert result == '{"nodes": []}'


def test_strip_fence_with_trailing_spaces():
    """尾随空白 → 正确处理。"""
    result = strip_markdown_fence('```json\n{"nodes": []}\n```   ')
    assert result == '{"nodes": []}'


def test_strip_fence_with_trailing_newlines():
    """末尾有空行 → 正确处理。"""
    result = strip_markdown_fence('```\n{"nodes": []}\n```\n\n')
    assert result == '{"nodes": []}'


def test_strip_leading_whitespace():
    """开头空白 → 先 strip 再处理。"""
    result = strip_markdown_fence('  \n  ```json\n{"nodes": []}\n```')
    assert result == '{"nodes": []}'


# ── _parse_response 测试 ────────────────────────────────────────────────────


@pytest.fixture
def mock_infra():
    """创建 mock AgentInfra——仅用于 Planner 构造。"""
    from unittest.mock import MagicMock
    infra = MagicMock()
    infra.llm_model = "deepseek-v4"
    return infra


@pytest.fixture
def planner(mock_infra):
    """创建 Planner 实例（注入 mock infra，解析不需要真实 LLM）。"""
    return Planner(infra=mock_infra, token_ledger=None)


@pytest.fixture
def replanner(mock_infra):
    """创建 Replanner 实例（注入 mock infra，解析不需要真实 LLM）。"""
    return Replanner(infra=mock_infra, token_ledger=None)


def _make_plan_json(nodes=None, edges=None):
    """构造 Plan JSON 字符串。"""
    data = {}
    if nodes is not None:
        data["nodes"] = nodes
    if edges is not None:
        data["edges"] = edges
    return json.dumps(data)


def test_parse_minimal(planner):
    """最小合法 Plan：一个节点 + 空 edges。"""
    raw = _make_plan_json(
        nodes=[{"node_id": "step1", "description": "第一步"}],
        edges=[],
    )
    plan = planner._parse_response(raw)

    assert plan.plan_version == 1
    assert len(plan.dag.nodes) == 1
    assert plan.dag.nodes[0].node_id == NodeId("step1")
    assert plan.dag.nodes[0].description == "第一步"
    assert plan.dag.nodes[0].required is True  # 默认值
    assert plan.dag.edges == []


def test_parse_multiple_nodes_with_edges(planner):
    """两个节点 + 一条依赖边。"""
    raw = _make_plan_json(
        nodes=[
            {"node_id": "search", "description": "搜索"},
            {"node_id": "send", "description": "发送邮件", "required": False},
        ],
        edges=[["search", "send"]],
    )
    plan = planner._parse_response(raw)

    assert len(plan.dag.nodes) == 2
    assert len(plan.dag.edges) == 1
    assert plan.dag.edges[0] == (NodeId("search"), NodeId("send"))
    # required=False 被正确解析
    send_node = [n for n in plan.dag.nodes if n.node_id == NodeId("send")][0]
    assert send_node.required is False


def test_parse_output_contract(planner):
    """output_contract.description 被正确解析。"""
    raw = _make_plan_json(
        nodes=[{
            "node_id": "step1",
            "description": "查询天气",
            "output_contract": {"description": "天气数据 JSON"},
        }],
        edges=[],
    )
    plan = planner._parse_response(raw)

    node = plan.dag.nodes[0]
    assert node.output_contract.description == "天气数据 JSON"


def test_parse_input_bindings(planner):
    """input_bindings 和 output_as 被正确解析。"""
    raw = _make_plan_json(
        nodes=[{
            "node_id": "step2",
            "description": "第二步",
            "input_bindings": ["output:step1"],  # type: ignore[reportArgumentType]
            "output_as": "output:step2",  # type: ignore[reportArgumentType]
        }],
        edges=[],
    )
    plan = planner._parse_response(raw)

    node = plan.dag.nodes[0]
    assert node.input_bindings == [ArtifactId("output:step1")]
    assert node.output_as == ArtifactId("output:step2")


def test_parse_markdown_wrapped(planner):
    """LLM 常见输出——JSON 被 ```json ... ``` 包裹。"""
    raw = '```json\n' + _make_plan_json(
        nodes=[{"node_id": "s1", "description": "test"}],
        edges=[],
    ) + '\n```'
    plan = planner._parse_response(raw)

    assert plan.dag.nodes[0].node_id == NodeId("s1")


def test_parse_edges_missing_defaults_to_empty(planner):
    """JSON 没有 edges 键 → 默认为空列表。"""
    raw = json.dumps({"nodes": [{"node_id": "s1", "description": "x"}]})
    plan = planner._parse_response(raw)

    assert plan.dag.edges == []


def test_parse_missing_output_contract(planner):
    """节点没有 output_contract 字段 → 默认空 OutputContract。"""
    raw = _make_plan_json(
        nodes=[{"node_id": "s1", "description": "test"}],
        edges=[],
    )
    plan = planner._parse_response(raw)

    node = plan.dag.nodes[0]
    assert node.output_contract.description == ""


# ── _parse_response 错误处理测试 ────────────────────────────────────────────


def test_parse_empty_string(planner):
    """空字符串 → PlanParseError。"""
    with pytest.raises(PlanParseError, match="JSON 解析失败"):
        planner._parse_response("")


def test_parse_not_json(planner):
    """非 JSON 文本 → PlanParseError。"""
    with pytest.raises(PlanParseError, match="JSON 解析失败"):
        planner._parse_response("这不是 JSON")


def test_parse_not_object(planner):
    """JSON 数组而非对象 → PlanParseError。"""
    with pytest.raises(PlanParseError, match="JSON object"):
        planner._parse_response('[{"node_id": "x"}]')


def test_parse_nodes_not_list(planner):
    """nodes 不是数组 → PlanParseError。"""
    with pytest.raises(PlanParseError, match="nodes 为空或不是数组"):
        planner._parse_response('{"nodes": "not a list"}')


def test_parse_nodes_empty(planner):
    """nodes 为空数组 → PlanParseError。"""
    with pytest.raises(PlanParseError, match="nodes 为空或不是数组"):
        planner._parse_response('{"nodes": []}')


def test_parse_node_not_object(planner):
    """nodes 中某项不是 object → PlanParseError。"""
    with pytest.raises(PlanParseError, match="不是 object"):
        planner._parse_response('{"nodes": ["string_instead_of_object"]}')


def test_parse_node_missing_id(planner):
    """节点缺少 node_id → PlanParseError。"""
    with pytest.raises(PlanParseError, match="node_id"):
        planner._parse_response('{"nodes": [{"description": "no id"}]}')


def test_parse_node_id_not_string(planner):
    """节点 node_id 是数字 → PlanParseError。"""
    with pytest.raises(PlanParseError, match="不是字符串"):
        planner._parse_response('{"nodes": [{"node_id": 123, "description": "x"}]}')


def test_parse_edges_not_list(planner):
    """edges 不是数组 → PlanParseError。"""
    with pytest.raises(PlanParseError, match="edges 不是数组"):
        planner._parse_response(
            '{"nodes": [{"node_id": "s1", "description": "x"}], "edges": "bad"}'
        )


def test_parse_edge_not_pair(planner):
    """edges 中某项不是二元组 → PlanParseError。"""
    with pytest.raises(PlanParseError, match="二元组"):
        planner._parse_response(
            '{"nodes": [{"node_id": "s1", "description": "x"}], "edges": [["only_one"]]}'
        )


# ── _format_nodes_by_status 测试 ────────────────────────────────────────────


def test_format_nodes_by_status_success(replanner):
    """成功节点 → 包含 node_id + description + summary + evaluation。"""
    from Module.workflow_engine.schema import (
        BusinessStatus,
        EvaluatorAction,
        ExecutionStatus,
        NodeResult,
        NodeResultEvaluation,
    )

    node_results = {
        "search": NodeState(
            node_id=NodeId("search"),
            status=NodeStatus.SUCCESS,
            summary="找到 5 条结果",
            evaluation=NodeResultEvaluation(
                node_id=NodeId("search"),
                execution_status=ExecutionStatus.OK,
                business_status=BusinessStatus.GOAL_ACHIEVED,
                reason="搜索成功",
                suggested_action=EvaluatorAction.CONTINUE,
                result=NodeResult(node_id=NodeId("search")),
            ),
        ),
    }
    plan = Plan(dag=DAG(nodes=[
        DAGNode(node_id=NodeId("search"), description="搜索最新 AI 新闻"),
    ], edges=[]), plan_version=1)

    result = replanner._format_nodes_by_status(
        node_results, plan, {NodeStatus.SUCCESS, NodeStatus.SKIPPED},
    )
    assert "search" in result
    assert "搜索最新 AI 新闻" in result
    assert "找到 5 条结果" in result
    assert "goal_achieved" in result


def test_format_nodes_by_status_failed(replanner):
    """失败节点 → 包含 error + evaluation 信息。"""
    from Module.workflow_engine.schema import (
        BusinessStatus,
        EvaluatorAction,
        ExecutionStatus,
        NodeResult,
        NodeResultEvaluation,
    )

    node_results = {
        "search": NodeState(
            node_id=NodeId("search"),
            status=NodeStatus.FAILED,
            error="工具超时",
            evaluation=NodeResultEvaluation(
                node_id=NodeId("search"),
                execution_status=ExecutionStatus.TIMEOUT,
                business_status=BusinessStatus.GOAL_FAILED,
                reason="搜索超时",
                suggested_action=EvaluatorAction.REPLAN,
                result=NodeResult(node_id=NodeId("search")),
            ),
        ),
    }
    plan = Plan(dag=DAG(nodes=[
        DAGNode(node_id=NodeId("search"), description="搜索最新 AI 新闻"),
    ], edges=[]), plan_version=1)

    result = replanner._format_nodes_by_status(
        node_results, plan, {NodeStatus.FAILED, NodeStatus.UNRECOVERABLE},
    )
    assert "工具超时" in result
    assert "timeout" in result
    assert "goal_failed" in result


def test_format_nodes_by_status_empty(replanner):
    """无匹配状态的节点 → 返回空文本。"""
    node_results: dict = {}
    plan = Plan(dag=DAG(nodes=[
        DAGNode(node_id=NodeId("search"), description="搜索"),
    ], edges=[]), plan_version=1)

    result = replanner._format_nodes_by_status(
        node_results, plan, {NodeStatus.SUCCESS},
    )
    assert result == ""


def test_format_nodes_by_status_truncates_long_summary(replanner):
    """超过 200 字符的 summary → 截断。"""
    node_results = {
        "search": NodeState(
            node_id=NodeId("search"),
            status=NodeStatus.SUCCESS,
            summary="A" * 300,
        ),
    }
    plan = Plan(dag=DAG(nodes=[
        DAGNode(node_id=NodeId("search"), description="搜索"),
    ], edges=[]), plan_version=1)

    result = replanner._format_nodes_by_status(
        node_results, plan, {NodeStatus.SUCCESS},
    )
    assert "..." in result
    assert len(result) < 350  # 截断后不超过 200 + 前后缀


# ── _parse_replan_response 测试 ─────────────────────────────────────────────


def test_parse_replan_full_replan(replanner):
    """full_replan=true → 返回 Plan。"""
    data = {
        "full_replan": True,
        "nodes": [
            {
                "node_id": "new_search",
                "description": "用新关键词搜索",
                "output_contract": {"description": "搜索结果的 JSON 数组"},
                "required": True,
                "output_as": "output:new_search",
            },
        ],
        "edges": [],
    }
    result = replanner._parse_replan_response(json.dumps(data))
    assert isinstance(result, Plan)
    assert len(result.dag.nodes) == 1
    assert result.dag.nodes[0].node_id == NodeId("new_search")


def test_parse_replan_full_replan_with_edges(replanner):
    """全量重建含 edges → Plan 包含正确的依赖关系。"""
    data = {
        "full_replan": True,
        "nodes": [
            {"node_id": "a", "description": "第一步", "output_as": "output:a"},
            {"node_id": "b", "description": "第二步", "input_bindings": ["output:a"], "output_as": "output:b"},
        ],
        "edges": [["a", "b"]],
    }
    result = replanner._parse_replan_response(json.dumps(data))
    assert isinstance(result, Plan)
    assert len(result.dag.nodes) == 2
    assert len(result.dag.edges) == 1


def test_parse_replan_dagpatch_replace(replanner):
    """full_replan=false → 返回 DAGPatch（replace_nodes）。"""
    data = {
        "full_replan": False,
        "patch_type": "replace_nodes",
        "reason": "工具报错，换一个搜索工具",
        "trigger_nodes": ["old_search"],
        "replace_nodes": [
            ["old_search", {
                "node_id": "new_search",
                "description": "用 web_fetch 替代 web_search",
                "output_contract": {"description": "搜索结果"},
                "required": True,
                "output_as": "output:new_search",
            }]
        ],
    }
    result = replanner._parse_replan_response(json.dumps(data))
    assert isinstance(result, DAGPatch)
    assert result.patch_type == DAGPatchType.REPLACE_NODES
    assert result.reason == "工具报错，换一个搜索工具"
    assert len(result.trigger_nodes) == 1
    assert len(result.replace_nodes) == 1
    old_id, new_node = result.replace_nodes[0]
    assert old_id == NodeId("old_search")
    assert new_node.node_id == NodeId("new_search")


def test_parse_replan_dagpatch_add(replanner):
    """full_replan=false → 返回 DAGPatch（add_nodes + add_edges）。"""
    data = {
        "full_replan": False,
        "patch_type": "add_nodes",
        "reason": "缺少前置步骤",
        "trigger_nodes": ["failed_search"],
        "add_nodes": [
            {"node_id": "get_timestamp", "description": "获取当前时间",
             "output_as": "output:get_timestamp", "required": True},
        ],
        "add_edges": [["get_timestamp", "failed_search"]],
    }
    result = replanner._parse_replan_response(json.dumps(data))
    assert isinstance(result, DAGPatch)
    assert result.patch_type == DAGPatchType.ADD_NODES
    assert len(result.add_nodes) == 1
    assert len(result.add_edges) == 1


def test_parse_replan_invalid_json(replanner):
    """非法 JSON → PlanParseError。"""
    with pytest.raises(PlanParseError, match="JSON 解析失败"):
        replanner._parse_replan_response("not json")


def test_parse_replan_not_object(replanner):
    """JSON 数组而非对象 → PlanParseError。"""
    with pytest.raises(PlanParseError, match="JSON object"):
        replanner._parse_replan_response("[]")


def test_parse_replan_unknown_patch_type(replanner):
    """未知 patch_type → PlanParseError。"""
    data = {
        "full_replan": False,
        "patch_type": "unknown_type",
    }
    with pytest.raises(PlanParseError, match="未知 patch_type"):
        replanner._parse_replan_response(json.dumps(data))


def test_parse_replan_full_missing_nodes(replanner):
    """全量重建但 nodes 为空 → PlanParseError。"""
    data = {"full_replan": True, "nodes": []}
    with pytest.raises(PlanParseError, match="nodes 为空"):
        replanner._parse_replan_response(json.dumps(data))


# ── token_ledger 注入与 record 测试 ──────────────────────────────────────────


@pytest.fixture
def planner_with_mock_llm():
    """创建 Planner，mock infra.llm.chat.completions.create 以返回可控响应。"""
    mock_ledger = MagicMock()
    mock_infra = MagicMock()
    mock_infra.llm_model = "deepseek-v4"
    mock_infra.llm.chat.completions.create = AsyncMock()
    planner = Planner(infra=mock_infra, token_ledger=mock_ledger)
    return planner, mock_ledger


@pytest.fixture
def replanner_with_mock_llm():
    """创建 Replanner，mock infra.llm.chat.completions.create 以返回可控响应。"""
    mock_ledger = MagicMock()
    mock_infra = MagicMock()
    mock_infra.llm_model = "deepseek-v4"
    mock_infra.llm.chat.completions.create = AsyncMock()
    replanner = Replanner(infra=mock_infra, token_ledger=mock_ledger)
    return replanner, mock_ledger


def _set_mock_response(planner, content: str, prompt_tokens: int = 100, completion_tokens: int = 50):
    """设置 mock LLM 响应——返回指定 JSON content 和 usage。"""
    mock_choice = MagicMock()
    mock_choice.message.content = content
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    planner._infra.llm.chat.completions.create.return_value = mock_response


def test_plan_records_token_usage(planner_with_mock_llm):
    """plan() 中 LLM 调用成功后 token_ledger.record() 被调用，参数正确。"""
    planner, mock_ledger = planner_with_mock_llm

    valid_json = json.dumps({
        "nodes": [{"node_id": "step1", "description": "搜索"}],
        "edges": [],
    })
    _set_mock_response(planner, valid_json, prompt_tokens=100, completion_tokens=50)

    context = DelegationContext(
        intent="搜索最新 AI 新闻",
        hints="使用 web_search 工具",
        tools=[],
        summarized_context="无",
        session_id= SessionId("test-session-1"),
    )
    global_constraints = ConstraintConfig(max_dag_nodes=20, max_mutations=10)

    import asyncio
    plan = asyncio.run(planner.plan(context, global_constraints))

    assert plan is not None
    assert len(plan.dag.nodes) == 1

    # 验证 token_ledger.record 被调用
    mock_ledger.record.assert_called_once()

    call_kwargs = mock_ledger.record.call_args.kwargs
    assert call_kwargs["component"] == TokenComponentType.PLANNER
    assert call_kwargs["prompt_tokens"] == 100
    assert call_kwargs["completion_tokens"] == 50
    assert "model" in call_kwargs


def test_replan_records_token_usage(replanner_with_mock_llm):
    """replan() 中 LLM 调用成功后 token_ledger.record() 被调用，参数正确。"""
    replanner, mock_ledger = replanner_with_mock_llm

    valid_json = json.dumps({
        "full_replan": False,
        "patch_type": "replace_nodes",
        "reason": "换工具",
        "trigger_nodes": ["search"],
        "replace_nodes": [
            ["search", {"node_id": "new_search", "description": "新搜索",
             "output_as": "output:new_search", "required": True}]
        ],
    })
    _set_mock_response(replanner, valid_json, prompt_tokens=200, completion_tokens=80)

    original_plan = Plan(
        dag=DAG(nodes=[
            DAGNode(node_id=NodeId("search"), description="搜索"),
        ], edges=[]),
        plan_version=1,
    )
    node_results: dict[str, NodeState] = {
        "search": NodeState(
            node_id=NodeId("search"),
            status=NodeStatus.FAILED,
            error="工具超时",
        ),
    }

    import asyncio
    result = asyncio.run(replanner.replan(
        original_plan=original_plan,
        failed_node_ids=[NodeId("search")],
        failure_diagnosis="搜索超时",
        node_results=node_results,
        intent="搜索新闻",
        tools=[],
        max_nodes=10,
        max_mutations=5,
    ))

    assert isinstance(result, DAGPatch)
    assert result.patch_type == DAGPatchType.REPLACE_NODES

    # 验证 token_ledger.record 被调用
    mock_ledger.record.assert_called_once()

    call_kwargs = mock_ledger.record.call_args.kwargs
    assert call_kwargs["component"] == TokenComponentType.PLANNER
    assert call_kwargs["prompt_tokens"] == 200
    assert call_kwargs["completion_tokens"] == 80
    assert "model" in call_kwargs
