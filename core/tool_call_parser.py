"""流式工具调用解析 — 合并增量 + 归一化。

CognitiveLoop 流式接收 tool_call deltas 时，用本模块的函数
增量合并和归一化。从 cognitive_loop.py 提取，纯 stdlib，零依赖。
"""


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


def normalize_tool_calls(raw: list[dict]) -> list[dict]:
    """过滤不完整的工具调用——只有 id 和 name 都有的才算有效。

    Args:
        raw: merge_stream_tool_calls 产出的原始工具调用列表

    Returns:
        仅包含 id 和 function.name 都非空的工具调用
    """
    result = []
    for tc in raw:
        if tc.get("id") and tc["function"].get("name"):
            result.append(tc)
    return result
