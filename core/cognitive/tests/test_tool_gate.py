"""Action Gate 单元测试 — _gate_tool_calls() 工具校验。"""
import json
from typing import cast

import pytest
from core.adapter.tool_adapter import ToolAdapter
from core.cognitive.tools import CognitiveTools
from core.schema.tool import ToolCall, ToolCallFunction
from core.schema.types import ToolCallId


pytestmark = pytest.mark.unit


class _FakeExecutor:
    """Mock ToolExecutor — 返回固定的 definitions 列表。"""
    def __init__(self, names: list[str] | None = None):
        self._names = names or ["search_memory", "save_memory", "get_time"]

    def definitions(self) -> list[dict]:
        return [
            {"type": "function", "function": {"name": n}}
            for n in self._names
        ]

    def execute(self, name, ctx=None, **kwargs):
        raise NotImplementedError("gate test should not execute")


def _make_tc(name: str, call_id: str = "call_1", args: dict | None = None) -> ToolCall:
    return ToolCall(
        id=ToolCallId(call_id),
        function=ToolCallFunction(name=name, arguments=json.dumps(args or {})),
    )


def _gate(calls: list[ToolCall], executor_names: list[str] | None = None,
          blocked: set[str] | None = None) -> tuple[bool, list[dict]]:
    """Helper: 创建 CognitiveTools 实例，调用 _gate_tool_calls。"""
    tools = CognitiveTools()
    tools._tool_adapter = cast(ToolAdapter, _FakeExecutor(executor_names))
    messages: list[dict] = []
    ok = tools._gate_tool_calls(calls, messages, blocked_tools=blocked)
    return ok, messages


# ---- 合法路径 ----

def test_all_known_tools_pass():
    """全部是已知工具 → 返回 True，不注入 messages。"""
    ok, msgs = _gate([
        _make_tc("search_memory", "c1"),
        _make_tc("get_time", "c2"),
    ])
    assert ok is True
    assert len(msgs) == 0


def test_only_submit_task_passes():
    """submit_task 被跳过，即使不在 registry 也通过。"""
    ok, msgs = _gate([_make_tc("submit_task", "c1")])
    assert ok is True
    assert len(msgs) == 0


def test_empty_calls_passes():
    """空列表直接通过。"""
    ok, msgs = _gate([])
    assert ok is True
    assert len(msgs) == 0


def test_all_known_plus_submit_task_passes():
    """submit_task + 已知工具混合 → 全部通过。"""
    ok, msgs = _gate([
        _make_tc("search_memory", "c1"),
        _make_tc("submit_task", "c2"),
    ])
    assert ok is True


# ---- 拒绝路径 ----

def test_unknown_tool_rejected():
    """幻觉工具名 → 返回 False，注入 GATE_REJECTED message。"""
    ok, msgs = _gate([_make_tc("delete_everything", "c1")])
    assert ok is False
    assert len(msgs) == 1
    assert msgs[0]["role"] == "tool"
    assert msgs[0]["tool_call_id"] == "c1"
    assert "未知工具" in msgs[0]["content"]


def test_unknown_tool_cascades_to_all():
    """一个未知工具 → 同轮所有工具（含合法的）都被注入 GATE_REJECTED。"""
    ok, msgs = _gate([
        _make_tc("delete_everything", "c1"),  # 未知
        _make_tc("get_time", "c2"),           # 已知
    ])
    assert ok is False
    assert len(msgs) == 2
    # c1: 直接被拒
    c1_msg = next(m for m in msgs if m["tool_call_id"] == "c1")
    assert "未知工具" in c1_msg["content"]
    # c2: 连坐被拒
    c2_msg = next(m for m in msgs if m["tool_call_id"] == "c2")
    assert "整轮取消" in c2_msg["content"]


def test_unknown_tool_does_not_affect_submit_task():
    """未知工具被拒时，submit_task 不受连坐影响。"""
    ok, msgs = _gate([
        _make_tc("delete_everything", "c1"),
        _make_tc("submit_task", "c2"),
    ])
    assert ok is False
    # submit_task 被跳过，不应出现在 messages 中
    submit_msgs = [m for m in msgs if m["tool_call_id"] == "c2"]
    assert len(submit_msgs) == 0


def test_multiple_unknown_tools():
    """多个未知工具 → 全部标记。"""
    ok, msgs = _gate([
        _make_tc("bad_tool_1", "c1"),
        _make_tc("bad_tool_2", "c2"),
        _make_tc("get_time", "c3"),
    ])
    assert ok is False
    assert len(msgs) == 3  # 2 rejected + 1 cascade


# ---- 黑名单预留 ----

def test_blocked_tool_rejected():
    """黑名单中的工具被拒。"""
    ok, msgs = _gate(
        [_make_tc("search_memory", "c1")],
        blocked={"search_memory"},
    )
    assert ok is False
    assert len(msgs) == 1
    assert "已禁用" in msgs[0]["content"]


# ---- executor 为 None ----

def test_executor_none_all_unknown():
    """_tool_adapter 为 None → 所有非 submit_task 都是未知。"""
    tools = CognitiveTools()
    tools._tool_adapter = None
    messages: list[dict] = []
    ok = tools._gate_tool_calls([_make_tc("get_time", "c1")], messages)
    assert ok is False
    assert len(messages) == 1
    assert "未知工具" in messages[0]["content"]
