"""Agent 工具集 — 统一注册入口。

用法:
    from Module.tool import ToolRegistry, ToolContext
    from core.tools import register_all

    registry = ToolRegistry()
    ctx = ToolContext(session_id="...", services={"memory": long_term_memory})
    register_all(registry)

    # 执行时 ctx 自动注入
    result = await registry.execute("search_memory", {"query": "rust"}, ctx=ctx)
"""

from Module.tool import ToolRegistry

from .memory import search_memory, save_memory
from .time import get_time

__all__ = [
    "search_memory",
    "save_memory",
    "get_time",
    "register_all",
]


def register_all(registry: ToolRegistry) -> None:
    """将所有工具注册到给定的 ToolRegistry 实例。"""
    registry.register_many([
        search_memory._tool_definition,
        save_memory._tool_definition,
        get_time._tool_definition,
    ])
