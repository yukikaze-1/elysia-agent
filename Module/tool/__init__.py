"""Tool use 模块 — 可复用的工具定义、注册、执行系统。

核心 API:
    @tool 装饰器                        # 将 async function 标记为工具
    ToolDefinition / ToolParameter      # 工具定义与参数
    ToolRegistry                        # 注册、执行、Schema 导出
    ToolResult                          # 执行结果（success/content/error/truncated）

使用:
    from Module.tool import tool, ToolRegistry

    @tool(description="搜索记忆")
    async def search_memory(query: str, limit: int = 5) -> str:
        ...

    registry = ToolRegistry()
    registry.register(search_memory._tool_definition)

    result = await registry.execute("search_memory", {"query": "Rust"})
    print(result.content)
"""

from .protocol import (
    ErrorType,
    MediaItem,
    ResultType,
    ToolContext,
    ToolDefinition,
    ToolEmitter,
    ToolParameter,
    ToolProgress,
    ToolResult,
)
from .registry import ToolRegistry
from .decorator import tool, get_tool_definition
from .schema import type_hints_to_params, pydantic_to_params

__all__ = [
    "tool",
    "get_tool_definition",
    "ErrorType",
    "MediaItem",
    "ResultType",
    "ToolContext",
    "ToolDefinition",
    "ToolEmitter",
    "ToolParameter",
    "ToolProgress",
    "ToolResult",
    "ToolRegistry",
    "type_hints_to_params",
    "pydantic_to_params",
]
