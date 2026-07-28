# core/schema/tool.py
"""工具调用类型 — OpenAI 兼容 tool_calls 结构。"""

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from .types import ToolCallId


class ToolCallFunction(BaseModel):
    """工具调用的 function 块。"""
    name: str = ""
    arguments: str = ""  # JSON 字符串

    @property
    def parsed(self) -> dict[str, Any]:
        """将 arguments JSON 字符串解析为 dict。

        Returns:
            解析后的参数字典，解析失败返回空字典。
        """
        try:
            return json.loads(self.arguments)
        except json.JSONDecodeError:
            return {}


class ToolCall(BaseModel):
    """单个工具调用 — OpenAI compatible。

    对应 OpenAI API 响应中的 tool_calls[] 元素：
      {"id": "call_xxx", "type": "function", "function": {"name": "...", "arguments": "..."}}
    """
    id: ToolCallId
    type: Literal["function"] = "function"
    function: ToolCallFunction = Field(default_factory=ToolCallFunction)
