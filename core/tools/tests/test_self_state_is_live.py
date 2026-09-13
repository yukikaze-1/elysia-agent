# 测试文件：core/tools/tests/test_self_state_is_live.py
"""S3-16：`get_self_state` 必须报告**当前**状态，而不是启动时的快照。

`services["_fsm_state"]` 原先只在 `_lifecycle_tools` 里写一次（Agent 启动时），
于是工具恒返回 `idle`——**工具系统性给出错误自述，比缺失更糟**：排障的人会采信它。

同期的 `_session_msg_count` 是每轮刷新的（`loop.py` 换绑 `services["session"]` 时一并
更新），只有 `_fsm_state` 漏了——**同一份 services 里两个相似字段一个实时一个不实时**。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.tools.self_state.plugin import SelfStatePlugin

pytestmark = pytest.mark.service


def _ctx(fsm_state=None, **services):
    from Module.tool.protocol import ToolContext

    ctx = ToolContext(session_id="s1", services=dict(services))
    if fsm_state is not None:
        ctx.services["_fsm_state"] = fsm_state
    return ctx


@pytest.mark.asyncio
async def test_plugin_reports_whatever_is_in_ctx():
    """回归网：插件本身只是如实回显 ctx 里的值（缺陷不在它）。"""
    plugin = SelfStatePlugin()
    result = await plugin.execute(ctx=_ctx("thinking"), params=None)
    assert "thinking" in str(result)


def test_fsm_state_is_refreshed_on_every_tool_call():
    """**核心**：每次工具调用都要把 `_fsm_state` 刷新成实时值。

    直接 AST 断言调用点存在，而不是驱动整个 CognitiveLoop（后者要起 Agent）。
    断言的是"刷新这件事接在 clone_for_call 之后"，位置即正确性——若放在别处
    （例如每轮一次），一轮内多次工具调用仍会看到过期状态。
    """
    import ast
    import pathlib

    src = pathlib.Path("core/cognitive/tools.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    clones: list[int] = []
    refreshes: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "clone_for_call":
                clones.append(node.lineno)
            if (node.func.attr == "current_name"
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "status"):
                refreshes.append(node.lineno)

    assert clones, "没有找到 clone_for_call 调用点——用例前提失效"
    assert refreshes, "没有刷新 _fsm_state ——工具会报告启动时的状态（恒 idle）"
    assert min(refreshes) > min(clones), (
        "刷新必须在 clone_for_call **之后**（写的是本次调用的 services 副本）")


@pytest.mark.asyncio
async def test_refresh_writes_into_call_local_copy():
    """刷新写进本次调用的副本 → 不污染共享 services（与 `_safe_path` 同约定）。"""
    from Module.tool.protocol import ToolContext

    shared = ToolContext(session_id="s1", services={"_fsm_state": "idle"})
    call_ctx = shared.clone_for_call(call_id="c1", session_id="s1")
    call_ctx.services["_fsm_state"] = "thinking"

    assert shared.services["_fsm_state"] == "idle", "共享 services 被污染"
    assert call_ctx.services["_fsm_state"] == "thinking"
