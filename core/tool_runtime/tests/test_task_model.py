"""QueuedToolCall 模型单元测试 — ID/retry/serialization/factory。"""

import pytest
from core.tool_runtime import QueuedToolCall, QueuedToolCallKind, QueuedToolCallState

pytestmark = pytest.mark.unit


def test_new_tool_call_has_valid_id():
    t = QueuedToolCall.new_tool_call("parent-1", "web_search", {"query": "hello"})
    assert t.id.startswith("task_")
    assert len(t.id) == 31
    assert t.type == QueuedToolCallKind.TOOL_CALL
    assert t.request_id == "parent-1"
    assert t.payload["tool_name"] == "web_search"
    assert t.payload["arguments"] == {"query": "hello"}


def test_new_tool_call_status_is_pending():
    t = QueuedToolCall.new_tool_call("p1", "test", {})
    assert t.status == QueuedToolCallState.PENDING


def test_new_tool_call_default_priority():
    t = QueuedToolCall.new_tool_call("p1", "test", {})
    assert t.priority == 5


def test_can_retry_success():
    t = QueuedToolCall.new_tool_call("p1", "x", {})
    t.status = QueuedToolCallState.FAILED
    t.retry_count = 0
    t.max_retries = 2
    assert t.can_retry() is True


def test_can_retry_exceeded():
    t = QueuedToolCall.new_tool_call("p1", "x", {})
    t.status = QueuedToolCallState.FAILED
    t.retry_count = 2
    t.max_retries = 2
    assert t.can_retry() is False


def test_retry_backoff_first():
    t = QueuedToolCall.new_tool_call("p1", "x", {})
    t.retry_count = 0
    assert t.retry_backoff_seconds() == 1.0  # 2^0 = 1


def test_retry_backoff_second():
    t = QueuedToolCall.new_tool_call("p1", "x", {})
    t.retry_count = 1
    assert t.retry_backoff_seconds() == 2.0  # 2^1 = 2


def test_retry_backoff_third():
    t = QueuedToolCall.new_tool_call("p1", "x", {})
    t.retry_count = 2
    assert t.retry_backoff_seconds() == 4.0  # 2^2 = 4


def test_retry_backoff_capped():
    t = QueuedToolCall.new_tool_call("p1", "x", {})
    t.retry_count = 10
    assert t.retry_backoff_seconds() == 30.0  # 2^10 > 30, cap at 30


def test_roundtrip_dict():
    t = QueuedToolCall.new_tool_call("parent-1", "echo", {"msg": "hi"})
    t.status = QueuedToolCallState.FAILED
    t.error = "timeout"
    t.retry_count = 1
    d = t.to_dict()
    assert d["type"] == "tool_call"
    assert d["payload"]["tool_name"] == "echo"

    restored = QueuedToolCall.from_dict(d)
    assert restored.id == t.id
    assert restored.type == QueuedToolCallKind.TOOL_CALL
    assert restored.payload["arguments"] == {"msg": "hi"}
    assert restored.error == "timeout"


def test_touch_updates_timestamp():
    import time
    t = QueuedToolCall.new_tool_call("p1", "x", {})
    old = t.updated_at
    time.sleep(0.001)
    t.touch()
    assert t.updated_at > old


def test_chaining_children():
    parent = QueuedToolCall(id="root", type=QueuedToolCallKind.TOOL_CALL)
    child = QueuedToolCall.new_tool_call("root", "echo", {})
    parent.children_ids.append(child.id)
    assert child.request_id == "root"


def test_depends_on_reserved():
    t = QueuedToolCall.new_tool_call("p1", "x", {})
    assert t.depends_on == []
    t.depends_on = ["task-a", "task-b"]
    assert len(t.depends_on) == 2


def test_can_retry_requires_non_terminal_status():
    """can_retry() 应对终态任务返回 False。"""
    from core.tool_runtime import QueuedToolCall, QueuedToolCallKind, QueuedToolCallState

    # COMPLETED 且还有重试次数 —— 应仍返回 False
    t = QueuedToolCall.new_tool_call(request_id="p1", tool_name="echo", arguments={})
    t.status = QueuedToolCallState.COMPLETED
    assert not t.can_retry(), "COMPLETED task should not be retryable"

    # FAILED 且还有重试次数 —— 应返回 True
    t2 = QueuedToolCall.new_tool_call(request_id="p1", tool_name="echo", arguments={})
    t2.status = QueuedToolCallState.FAILED
    assert t2.can_retry(), "FAILED task with retries remaining should be retryable"

    # CANCELLED —— 应返回 False
    t3 = QueuedToolCall.new_tool_call(request_id="p1", tool_name="echo", arguments={})
    t3.status = QueuedToolCallState.CANCELLED
    assert not t3.can_retry(), "CANCELLED task should not be retryable"

    # PENDING —— 未达 max_retries 时应返回 True
    t4 = QueuedToolCall.new_tool_call(request_id="p1", tool_name="echo", arguments={})
    assert t4.can_retry(), "PENDING task should be retryable"
