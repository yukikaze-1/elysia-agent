"""Planner 单元测试——不依赖 LLM，测试解析和格式化逻辑。

覆盖 _parse_response（JSON → Plan）和 _format_tools（Tool → prompt 文本）。
"""

from __future__ import annotations

import json

import pytest

from Module.task_system.core.planner import Planner, _PlanParseError
from Module.task_system.schema import (
    ArtifactId,
    NodeId,
    Tool,
)


# ── _format_tools 测试 ──────────────────────────────────────────────────────


def test_format_tools_empty():
    """空列表 → 占位文本。"""
    result = Planner._format_tools([])
    assert "无可用工具" in result


def test_format_tools_single():
    """单个工具 → 一行 name: description。"""
    tools = [Tool(name="web_search", description="搜索互联网")]
    result = Planner._format_tools(tools)
    assert result == "- web_search: 搜索互联网"


def test_format_tools_multiple():
    """多个工具 → 每行一个。"""
    tools = [
        Tool(name="web_search", description="搜索互联网"),
        Tool(name="send_email", description="发送邮件"),
    ]
    result = Planner._format_tools(tools)
    lines = result.split("\n")
    assert len(lines) == 2
    assert lines[0] == "- web_search: 搜索互联网"
    assert lines[1] == "- send_email: 发送邮件"


def test_format_tools_no_description():
    """工具无 description → 只显示名称。"""
    tools = [Tool(name="get_time", description="")]
    result = Planner._format_tools(tools)
    assert result == "- get_time"


# ── _strip_markdown_fence 测试 ──────────────────────────────────────────────


def test_strip_no_fence():
    """无代码块 → 原样返回。"""
    result = Planner._strip_markdown_fence('{"nodes": []}')
    assert result == '{"nodes": []}'


def test_strip_json_fence():
    """"```json ... ```" → 去掉包裹。"""
    result = Planner._strip_markdown_fence('```json\n{"nodes": []}\n```')
    assert result == '{"nodes": []}'


def test_strip_plain_fence():
    """"``` ... ```"（无语言标记）→ 去掉包裹。"""
    result = Planner._strip_markdown_fence('```\n{"nodes": []}\n```')
    assert result == '{"nodes": []}'


def test_strip_fence_with_trailing_spaces():
    """尾随空白 → 正确处理。"""
    result = Planner._strip_markdown_fence('```json\n{"nodes": []}\n```   ')
    assert result == '{"nodes": []}'


def test_strip_fence_with_trailing_newlines():
    """末尾有空行 → 正确处理。"""
    result = Planner._strip_markdown_fence('```\n{"nodes": []}\n```\n\n')
    assert result == '{"nodes": []}'


def test_strip_leading_whitespace():
    """开头空白 → 先 strip 再处理。"""
    result = Planner._strip_markdown_fence('  \n  ```json\n{"nodes": []}\n```')
    assert result == '{"nodes": []}'


# ── _parse_response 测试 ────────────────────────────────────────────────────


@pytest.fixture
def planner():
    """创建 Planner 实例（设置 dummy API key，因为解析不需要真实 LLM）。"""
    import os
    key = os.environ.get("DEEPSEEK_API_KEY")
    os.environ["DEEPSEEK_API_KEY"] = "test-dummy-key"
    try:
        yield Planner()
    finally:
        if key is None:
            del os.environ["DEEPSEEK_API_KEY"]
        else:
            os.environ["DEEPSEEK_API_KEY"] = key


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
            "input_bindings": ["output:step1"],
            "output_as": "output:step2",
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
    """空字符串 → _PlanParseError。"""
    with pytest.raises(_PlanParseError, match="JSON 解析失败"):
        planner._parse_response("")


def test_parse_not_json(planner):
    """非 JSON 文本 → _PlanParseError。"""
    with pytest.raises(_PlanParseError, match="JSON 解析失败"):
        planner._parse_response("这不是 JSON")


def test_parse_not_object(planner):
    """JSON 数组而非对象 → _PlanParseError。"""
    with pytest.raises(_PlanParseError, match="JSON object"):
        planner._parse_response('[{"node_id": "x"}]')


def test_parse_nodes_not_list(planner):
    """nodes 不是数组 → _PlanParseError。"""
    with pytest.raises(_PlanParseError, match="nodes 为空或不是数组"):
        planner._parse_response('{"nodes": "not a list"}')


def test_parse_nodes_empty(planner):
    """nodes 为空数组 → _PlanParseError。"""
    with pytest.raises(_PlanParseError, match="nodes 为空或不是数组"):
        planner._parse_response('{"nodes": []}')


def test_parse_node_not_object(planner):
    """nodes 中某项不是 object → _PlanParseError。"""
    with pytest.raises(_PlanParseError, match="不是 object"):
        planner._parse_response('{"nodes": ["string_instead_of_object"]}')


def test_parse_node_missing_id(planner):
    """节点缺少 node_id → _PlanParseError。"""
    with pytest.raises(_PlanParseError, match="node_id"):
        planner._parse_response('{"nodes": [{"description": "no id"}]}')


def test_parse_node_id_not_string(planner):
    """节点 node_id 是数字 → _PlanParseError。"""
    with pytest.raises(_PlanParseError, match="不是字符串"):
        planner._parse_response('{"nodes": [{"node_id": 123, "description": "x"}]}')


def test_parse_edges_not_list(planner):
    """edges 不是数组 → _PlanParseError。"""
    with pytest.raises(_PlanParseError, match="edges 不是数组"):
        planner._parse_response(
            '{"nodes": [{"node_id": "s1", "description": "x"}], "edges": "bad"}'
        )


def test_parse_edge_not_pair(planner):
    """edges 中某项不是二元组 → _PlanParseError。"""
    with pytest.raises(_PlanParseError, match="二元组"):
        planner._parse_response(
            '{"nodes": [{"node_id": "s1", "description": "x"}], "edges": [["only_one"]]}'
        )
