"""prompt 预算：估算与截断策略（S2-H9 的**唯一执行点**）。

## 为什么这个模块在 `Module/` 而不是 `core/`

两个消费方分属两层：`Module/system_prompt/system_prompt_manager.py`（section 级裁剪）
与 `core/prompt/*`（历史裁剪）。而架构门禁 R1 禁止 `Module/` 生产代码依赖 `core/`
（实测：把它放 core 会让 `make check-arch` 红），故共享判据留在 Module 侧，
core 反向引用（core → Module 是允许的方向）。

## 为什么需要

组装后的 prompt 此前**没有任何总长/预算/截断**（`grep max_prompt|prompt_budget|
context_window` 在 core/ 与 Module/llm/ 下 0 命中）。超 context 不是被截断而是变成
provider **400**，而 400 不在可重试集合里 ⇒ 该会话对该角色**永久失败**，日志还只显示
`GENERATION_PROVIDER_UNAVAILABLE`。触发面很现成：`save_memory` 与
`LongTermMemory.insert` 都不校验长度，角色档案上限本身很大（corePersonality 12000 +
relationshipStyle 6000 + …），空数据下 system prompt 已 ≈6.5K 字符。

## 预算从哪来

`providers.yaml::llm.<provider>.context_window`（模型上下文，单位 token）→ 乘
`CONTEXT_HEADROOM_RATIO` 留余量（输出 token、工具定义、估算误差都吃这份余量）。
未声明时用 `DEFAULT_CONTEXT_WINDOW`（保守值）并打一次 warning。

## 截断优先级（owner 2026-09-16 裁定）

    F1/F2 永不截断 → C4 记忆逐条截 → 历史由旧到新裁

- **system prompt 侧**（`SystemPromptManager`）：先减 C4 记忆条数，再按
  `DROPPABLE_SECTIONS` 顺序整节丢弃；`PROTECTED_SECTIONS` 里的节**永不**丢
  （冲突规则/输出格式/TTS 标记/人格/关系/说话风格/主动性——它们是"她是谁、
  怎么说话"的契约，丢了就不是同一个角色了）。
- **历史侧**（`MessageBuilder`）：**由旧到新逐条丢**（丢到装得下为止）。
- **当前用户消息永不丢**：它是这一轮的输入，丢了等于这一轮没发生。

### 为什么历史侧不需要"成对"约束（一条实测过的负结论）

报告曾要求"assistant `tool_calls` 与 `role=tool` 结果必须成对丢弃，否则拆开就是 400"。
**实测不成立**：`_history_message_content` 对 `role="tool"` 直接返回 None（不发射），
assistant 消息也只带 `content`+工具摘要文本、**不带 `tool_calls` 键**——工具流量被
汇总成 `"[已调用: web_search]"` 这样的一行文本进 prompt。故 messages 里既没有
`role="tool"` 也没有 `tool_calls`，不存在可拆的对。

这个前提**不是靠记性维持**：`core/prompt/tests/test_prompt_budget.py::TestToolTrafficPremise`
把它钉成断言——哪天有人开始发射工具流量，那条用例会红，并在消息里指回本节
（届时必须实现成对截断）。写一个"永远配不到对"的成对逻辑才是真风险：它看起来像守卫，
实际上一次都不会触发。

## 估算的边界（诚实声明）

没有 tokenizer（不能引入依赖），故用字符启发式：**CJK 1 字 ≈ 1 token**（保守上界），
其余 4 字符 ≈ 1 token；每条消息再加少量固定开销。它是**预算**不是**账本**——
真实用量以 provider 返回的 usage 为准（`TokenTracker`）。宁可多截一点，也不要再回到
"provider 400 且日志看不出原因"。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 未声明 context_window 时的保守默认（token）。取 32K：足以装下 system prompt +
# 若干轮历史，又不至于在 8K/16K 的本地模型上必然超限。
DEFAULT_CONTEXT_WINDOW: int = 32768

# 预算占上下文窗口的比例：留 25% 给输出 token、工具定义与估算误差。
CONTEXT_HEADROOM_RATIO: float = 0.75

# system prompt 最多吃掉预算的这一份（其余留给历史 + 当前消息）。定 0.5 的理由：
# F1/F2/C1 是身份与格式契约必须完整，而它们在空数据下已占 ~3.2K token；给一半
# 意味着"记忆再多也不会把历史挤没"。
SYSTEM_PROMPT_BUDGET_SHARE: float = 0.5

# 每条消息的固定开销（role/分隔符等），粗略但方向正确。
_PER_MESSAGE_OVERHEAD_TOKENS: int = 4

# CJK 统一表意文字 + 常用中文标点 + 假名/谚文：按 1 字 1 token 保守计。
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3000, 0x303F),    # CJK 标点
    (0x3040, 0x30FF),    # 平假名/片假名
    (0x3400, 0x4DBF),    # CJK 扩展 A
    (0x4E00, 0x9FFF),    # CJK 基本区
    (0xAC00, 0xD7AF),    # 谚文
    (0xF900, 0xFAFF),    # CJK 兼容表意
    (0xFF00, 0xFFEF),    # 全角字符
)


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """保守估算一段文本的 token 数（见模块 docstring 的边界声明）。"""
    if not text:
        return 0
    cjk = 0
    other = 0
    for char in text:
        if _is_cjk(char):
            cjk += 1
        else:
            other += 1
    return cjk + (other + 3) // 4


def _content_tokens(content: Any) -> int:
    """OpenAI message content 的 token 估算（str 或多模态块列表）。"""
    if isinstance(content, str):
        return estimate_tokens(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if not isinstance(block, dict):
                total += estimate_tokens(str(block))
                continue
            block_type = block.get("type")
            if block_type == "text":
                total += estimate_tokens(str(block.get("text", "")))
            elif block_type == "image_url":
                # 图片按固定上界计（真实值取决于分辨率，OpenAI 口径约 85–1105 token）
                total += 1024
            else:
                total += estimate_tokens(str(block))
        return total
    return estimate_tokens(str(content))


def estimate_messages_tokens(messages: list[dict]) -> int:
    """估算整份 messages[] 的 token 数。"""
    total = 0
    for msg in messages:
        total += _PER_MESSAGE_OVERHEAD_TOKENS
        total += estimate_tokens(str(msg.get("role", "")))
        total += _content_tokens(msg.get("content"))
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            total += estimate_tokens(str(tool_calls))
    return total


def budget_from_context_window(context_window: int | None) -> int:
    """上下文窗口 → prompt 预算（token）。未声明窗口时用保守默认。"""
    window = context_window or DEFAULT_CONTEXT_WINDOW
    return max(1, int(window * CONTEXT_HEADROOM_RATIO))


def system_prompt_budget(prompt_budget_tokens: int) -> int:
    """system prompt 的子预算（总预算的一份，见 `SYSTEM_PROMPT_BUDGET_SHARE`）。"""
    return max(1, int(prompt_budget_tokens * SYSTEM_PROMPT_BUDGET_SHARE))


