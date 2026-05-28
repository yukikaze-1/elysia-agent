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

from .filesystem import list_directory, read_file
from .memory import save_memory, search_memory
from .time import get_time
from .web import web_fetch, web_search

__all__ = [
    "search_memory",
    "save_memory",
    "get_time",
    "web_search",
    "web_fetch",
    "read_file",
    "list_directory",
    "register_all",
]


def register_all(registry: ToolRegistry) -> None:
    """将所有工具注册到给定的 ToolRegistry 实例。"""
    registry.register_many([
        # 记忆
        search_memory._tool_definition,
        save_memory._tool_definition,
        # 时间
        get_time._tool_definition,
        # 网络
        web_search._tool_definition,
        web_fetch._tool_definition,
        # 文件系统
        read_file._tool_definition,
        list_directory._tool_definition,
    ])
