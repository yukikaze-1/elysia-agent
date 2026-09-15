# core/schema/tool.py — 工具调用类型
"""工具调用类型 — OpenAI 兼容 tool_calls 结构。"""

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.schema.types import ToolCallId


def _parse_arguments(raw: str) -> dict[str, Any] | None:
    """解析 arguments；**失败或不是对象** → None（`parsed` 与 `arguments_invalid` 共用）。

    空串按"没有参数"处理（返回 `{}` 形状），不算损坏——见 `arguments_invalid` 的边界说明。
    """
    if raw == "":
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


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
        return _parse_arguments(self.arguments) or {}

    @property
    def arguments_invalid(self) -> bool:
        """`arguments` 是否**不是**一个可用的 JSON 对象（S3-G1）。

        为什么需要这个信号：`parsed` 对解析失败返回 `{}`，于是"参数坏了"与"本来就没有
        参数"在数据上**长得一模一样**。必填参数的工具会被 pydantic 挡住（安全），但
        **全默认参数**的工具（`list_directory` 的 `path="."`、`get_time` 的时区、
        `get_runtime_config` 等）会以默认参数**真的执行**，LLM 再基于错误前提继续推理
        ——全程无异常、无日志。

        **边界（有意）**：空串算"没有参数"而不是损坏——否则每个 `ToolCallFunction()`
        的默认构造都会被判损坏，把"调用方没填"与"模型传了坏 JSON"混成一类。
        截断/畸形/非对象（如 `"[1,2]"`）都算损坏。
        """
        return _parse_arguments(self.arguments) is None


class ToolCall(BaseModel):
    """单个工具调用 — OpenAI compatible。

    对应 OpenAI API 响应中的 tool_calls[] 元素：
      {"id": "call_xxx", "type": "function", "function": {"name": "...", "arguments": "..."}}
    """
    id: ToolCallId
    type: Literal["function"] = "function"
    function: ToolCallFunction = Field(default_factory=ToolCallFunction)
