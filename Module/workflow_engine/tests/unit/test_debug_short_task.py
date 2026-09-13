# 测试文件：Module/workflow_engine/tests/unit/test_debug_short_task.py
"""S3-17：debug SSE 必须能看到**短任务**，且终态必须是真的终态。

`WorkflowDebugAPI._poll_loop` 每 **500ms** 采一次快照，而快照来源是引擎的 `_tasks`
字典——任务一进终态就被 `_cleanup_task` 摘除。于是：

1. 在一个轮询窗口内走完全程的任务**从未出现在任何快照里**（前端看不到它存在过）；
2. 即便被看到过一次，摘除时补发的 `task_done` 带的是 `old_by_id` 里那份**旧状态**
   （`ot["status"]`）——前端把 running 的任务归入"已完成"，`error` 一并丢失。

修法：引擎保留最近结束任务的**终态快照**（环形缓冲），快照把它一并带上。这样
短任务在下一轮就作为 `task_new` + `task_done` 出现，且状态是真的终态。
"""

from __future__ import annotations

from collections import deque
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


def _sub_agent(*, status: str, error: str = "", error_code: str = ""):
    sa = MagicMock()
    ts = MagicMock()
    ts.task_id = "t1"
    ts.status.value = status
    ts.plan.plan_version = 1
    ts.created_at = "2026-01-01T00:00:00+00:00"
    ts.error = error
    ts.error_code.value = error_code if error_code else None
    sa._task_state = ts
    sa._graph = None
    sa._orchestrator = None
    return sa


def test_snapshot_one_extracts_terminal_state():
    from Module.workflow_engine.query import snapshot_one

    entry = snapshot_one(_sub_agent(status="completed"), None)
    assert entry is not None
    assert entry["task_state"].status.value == "completed"
    assert entry["mailbox_depth"] == 0        # 邮箱已清理也不丢快照


def test_snapshot_one_returns_none_on_half_init():
    from Module.workflow_engine.query import snapshot_one

    sa = MagicMock()
    sa._task_state = None
    assert snapshot_one(sa, None) is None


def test_terminal_task_is_visible_after_removal():
    """**核心**：任务从 `_tasks` 摘除后，仍要出现在快照里（带终态）。"""
    from Module.workflow_engine.query import get_debug_snapshot, snapshot_one

    terminal_entry = snapshot_one(
        _sub_agent(status="failed", error="工具崩了", error_code="tool_error"), None)

    snap = get_debug_snapshot(
        tasks={}, mailboxes={}, max_sub_agents=5,
        active=set(), queue=None, recent_terminal=[terminal_entry])

    assert len(snap["tasks"]) == 1, "短任务被摘除后彻底消失——debug 永远看不到它"
    ts = snap["tasks"][0]["task_state"]
    assert ts.status.value == "failed"
    assert ts.error == "工具崩了"


def test_cleanup_remembers_terminal_before_removal():
    """接线：`_cleanup_task` 必须**先归档再摘除**（顺序反了就采不到快照）。"""
    import ast
    import pathlib

    src = pathlib.Path("Module/workflow_engine/workflow_engine.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_cleanup_task")
    body = ast.dump(fn)
    assert "_remember_terminal" in body, "清理时未归档终态快照"
    # 归档必须早于 pop
    lines = src.splitlines()
    start = fn.lineno - 1
    seg = "\n".join(lines[start:start + 25])
    assert seg.index("_remember_terminal") < seg.index("self._tasks.pop"), (
        "_remember_terminal 必须在 _tasks.pop **之前**——否则采不到 task_state")


def test_snapshot_includes_terminal_buffer():
    """接线：`_get_debug_snapshot` 要把环形缓冲传下去。"""
    import pathlib

    src = pathlib.Path("Module/workflow_engine/workflow_engine.py").read_text(encoding="utf-8")
    assert "recent_terminal=" in src, "快照未带上终态缓冲"


def test_terminal_buffer_is_bounded():
    """上界：环形缓冲不得无界增长（长跑进程里 debug 面板会一直变大）。"""
    import pathlib

    src = pathlib.Path("Module/workflow_engine/workflow_engine.py").read_text(encoding="utf-8")
    assert "deque(maxlen=" in src, "终态快照缓冲没有上界"
    assert deque(maxlen=20).maxlen == 20
