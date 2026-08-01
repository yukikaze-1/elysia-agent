"""WorkflowContract 契约层 — 无状态纯函数工具。

core 世界与 Module/workflow_engine 世界之间共享的纯函数：
format_tools / strip_markdown_fence / parse_iso。

零业务逻辑、零 I/O，不 import core.*，不 import Module.workflow_engine。
Tool 仅用于类型注解（from __future__ import annotations 惰性化，无运行时依赖）。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Module.tool.tool_descriptor import Tool


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


def parse_iso(ts: str) -> datetime:
    """将 IsoTimestamp 字符串解析为 offset-aware UTC datetime。

    替代各处重复的 try/except + fromisoformat + replace(tzinfo=...) 样板代码。
    供 core 侧 plugin.py 与编排层共用。

    Args:
        ts: ISO 8601 时间戳字符串。空字符串返回 epoch。

    Returns:
        datetime: offset-aware UTC datetime，空字符串返回 1970-01-01 UTC。
    """
    if not ts:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
