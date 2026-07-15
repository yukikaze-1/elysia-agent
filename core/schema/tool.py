# core/schema/tool.py
"""工具调用类型 — OpenAI 兼容 tool_calls 结构。"""

from typing import Literal

from pydantic import BaseModel, Field

from .types import ToolCallId


class ToolCallFunction(BaseModel):
    """工具调用的 function 块。"""
    name: str = ""
    arguments: str = ""  # JSON 字符串


class ToolCall(BaseModel):
    """单个工具调用 — OpenAI compatible。

    对应 OpenAI API 响应中的 tool_calls[] 元素：
      {"id": "call_xxx", "type": "function", "function": {"name": "...", "arguments": "..."}}
    """
    id: ToolCallId
    type: Literal["function"] = "function"
    function: ToolCallFunction = Field(default_factory=ToolCallFunction)
