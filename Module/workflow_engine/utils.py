"""TaskSystem 共享工具函数——供 Planner / DelegationPreparer / 其他组件使用。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Module.workflow_engine.schema import Tool


def strip_markdown_fence(text: str) -> str:
    """去除 LLM 响应中可能的 markdown 代码块包裹。

    处理 ```json ... ``` 和 ``` ... ``` 两种格式，
    容忍首尾空白、尾随空行等情况。

    Args:
        text: LLM 返回的原始文本。

    Returns:
        str: 去除代码块包裹后的纯文本。
    """
    text = text.strip()
    text = re.sub(r'^```[a-z]*\s*\n', '', text)
    text = re.sub(r'\n```\s*$', '', text)
    return text


def format_tools(tools: list[Tool], *, include_capabilities: bool = True) -> str:
    """将 Tool 列表格式化为 LLM prompt 可读的文本。

    Args:
        tools: 工具列表。
        include_capabilities: 是否在输出中包含能力标签。
            DelegationPreparer 需要（筛选相关工具），Planner 不需要。

    Returns:
        str: 格式化的工具列表文本，空列表返回"(无可用工具)"。
    """
    if not tools:
        return "（无可用工具）"
    lines: list[str] = []
    for t in tools:
        desc = t.description or ""
        line = f"- {t.name}: {desc}" if desc else f"- {t.name}"
        if include_capabilities and t.capabilities:
            caps = ", ".join(t.capabilities)
            line += f" [能力: {caps}]"
        lines.append(line)
    return "\n".join(lines)
