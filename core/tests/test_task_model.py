"""Task 模型单元测试 — ID/retry/serialization/factory。"""

import pytest
from core.task import Task, TaskType, TaskStatus


def test_new_tool_call_has_valid_id():
    t = Task.new_tool_call("parent-1", "web_search", {"query": "hello"})
    assert len(t.id) == 12
    assert t.type == TaskType.TOOL_CALL
    assert t.parent_id == "parent-1"
    assert t.payload["tool_name"] == "web_search"
    assert t.payload["arguments"] == {"query": "hello"}


def test_new_tool_call_status_is_pending():
    t = Task.new_tool_call("p1", "test", {})
    assert t.status == TaskStatus.PENDING


def test_new_tool_call_default_priority():
    t = Task.new_tool_call("p1", "test", {})
    assert t.priority == 5


def test_can_retry_success():
    t = Task.new_tool_call("p1", "x", {})
    t.status = TaskStatus.FAILED
    t.retry_count = 0
    t.max_retries = 2
    assert t.can_retry() is True


def test_can_retry_exceeded():
    t = Task.new_tool_call("p1", "x", {})
    t.status = TaskStatus.FAILED
    t.retry_count = 2
    t.max_retries = 2
    assert t.can_retry() is False


def test_can_retry_wrong_status():
    """COMPLETED/CANCELLED 终态任务不可重试，即使 retry_count < max_retries。"""
    t = Task.new_tool_call("p1", "x", {})
    t.status = TaskStatus.COMPLETED
    t.retry_count = 0
    t.max_retries = 2
    assert t.can_retry() is False  # 终态不可重试


def test_retry_backoff_first():
    t = Task.new_tool_call("p1", "x", {})
    t.retry_count = 0
    assert t.retry_backoff_seconds() == 1.0  # 2^0 = 1


def test_retry_backoff_second():
    t = Task.new_tool_call("p1", "x", {})
    t.retry_count = 1
    assert t.retry_backoff_seconds() == 2.0  # 2^1 = 2


def test_retry_backoff_third():
    t = Task.new_tool_call("p1", "x", {})
    t.retry_count = 2
    assert t.retry_backoff_seconds() == 4.0  # 2^2 = 4


def test_retry_backoff_capped():
    t = Task.new_tool_call("p1", "x", {})
    t.retry_count = 10
    assert t.retry_backoff_seconds() == 30.0  # 2^10 > 30, cap at 30


def test_roundtrip_dict():
    t = Task.new_tool_call("parent-1", "echo", {"msg": "hi"})
    t.status = TaskStatus.FAILED
    t.error = "timeout"
    t.retry_count = 1
    d = t.to_dict()
    assert d["type"] == "tool_call"
    assert d["payload"]["tool_name"] == "echo"

    restored = Task.from_dict(d)
    assert restored.id == t.id
    assert restored.type == TaskType.TOOL_CALL
    assert restored.payload["arguments"] == {"msg": "hi"}
    assert restored.error == "timeout"


def test_touch_updates_timestamp():
    import time
    t = Task.new_tool_call("p1", "x", {})
    old = t.updated_at
    time.sleep(0.001)
    t.touch()
    assert t.updated_at > old


def test_chaining_children():
    parent = Task(id="root", type=TaskType.TOOL_CALL)
    child = Task.new_tool_call("root", "echo", {})
    parent.children_ids.append(child.id)
    assert child.parent_id == "root"


def test_depends_on_reserved():
    t = Task.new_tool_call("p1", "x", {})
    assert t.depends_on == []
    t.depends_on = ["task-a", "task-b"]
    assert len(t.depends_on) == 2


def test_can_retry_requires_non_terminal_status():
    """can_retry() 应对终态任务返回 False。"""
    from core.task import Task, TaskType, TaskStatus

    # COMPLETED with retries remaining — should still return False
    t = Task.new_tool_call(parent_id="p1", tool_name="echo", arguments={})
    t.status = TaskStatus.COMPLETED
    assert not t.can_retry(), "COMPLETED task should not be retryable"

    # FAILED with retries remaining — should return True
    t2 = Task.new_tool_call(parent_id="p1", tool_name="echo", arguments={})
    t2.status = TaskStatus.FAILED
    assert t2.can_retry(), "FAILED task with retries remaining should be retryable"

    # CANCELLED — should return False
    t3 = Task.new_tool_call(parent_id="p1", tool_name="echo", arguments={})
    t3.status = TaskStatus.CANCELLED
    assert not t3.can_retry(), "CANCELLED task should not be retryable"

    # PENDING — should return True if under max_retries
    t4 = Task.new_tool_call(parent_id="p1", tool_name="echo", arguments={})
    assert t4.can_retry(), "PENDING task should be retryable"
