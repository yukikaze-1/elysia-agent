"""流式工具调用解析 — 合并增量 + 归一化。

CognitiveLoop 流式接收 tool_call deltas 时，用本模块的函数
增量合并和归一化。从 cognitive_loop.py 提取。
normalize_tool_calls 依赖 pydantic（core 全域依赖）。
"""

import json

from core.schema.tool import ToolCall, ToolCallFunction
from core.schema.types import ToolCallId


def merge_stream_tool_calls(accumulated: list[dict], deltas: list[dict]) -> None:
    """合并流式 tool_call deltas 到累加列表（原地修改）。

    流式 LLM 响应的 tool_calls 是分片到达的：
    - 每个 delta 带 index（第几个工具调用）
    - id / function.name / function.arguments 可能跨多个 delta 到达

    Args:
        accumulated: 累加的目标列表，会被原地修改
        deltas: 从 LLM chunk 中取出的 tool_call delta 列表
    """
    for delta in deltas:
        idx = delta.get("index", 0)
        while len(accumulated) <= idx:
            accumulated.append({
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            })
        if delta.get("id"):
            accumulated[idx]["id"] = delta["id"]
        if "function" in delta:
            func = delta["function"]
            if func.get("name"):
                accumulated[idx]["function"]["name"] += func["name"]
            if func.get("arguments"):
                accumulated[idx]["function"]["arguments"] += func["arguments"]


def normalize_tool_calls(raw: list[dict]) -> list[ToolCall]:
    """过滤不完整的工具调用，转为 ToolCall 模型。

    Args:
        raw: merge_stream_tool_calls 产出的原始工具调用列表

    Returns:
        仅包含 id 和 function.name 都非空的 ToolCall
    """
    result: list[ToolCall] = []
    for tc in raw:
        func = tc.get("function", {})          # 防御 think() 路径的异常 dict 形状
        if tc.get("id") and func.get("name"):
            arguments = func.get("arguments", "")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments) if isinstance(arguments, dict) else ""
            result.append(ToolCall(
                id=ToolCallId(tc["id"]),
                function=ToolCallFunction(
                    name=func["name"],
                    arguments=arguments,
                ),
            ))
    return result
