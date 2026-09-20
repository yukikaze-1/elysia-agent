# 测试文件：core/prompt/tests/test_round_budget.py
"""KB-77 / Batch 19：ReAct 工具轮的**回合内再预算**（`enforce_round_budget`）。

守的是什么：首轮由 `_apply_prompt_budget` 裁到预算内之后，工具轮会把 assistant
tool_calls 与 `role="tool"` 结果**原地**追加进同一个 messages 数组，而下一轮会把整个
数组重发。此前**没有任何再预算**，于是第 2..N 轮可以远超 context window——而 provider
（Ollama，Batch 17 实测）是 HTTP 200 + **静默截断**，不是报错 ⇒ silent loss。

本文件的判据全部是**行为**判据（总量、组完整性、保护集），不是"调用了某个函数"。
"""

from __future__ import annotations

import json

import pytest

from Module.system_prompt.budget import (
    estimate_messages_tokens,
    estimate_tools_tokens,
)
from core.prompt.message_builder import (
    RoundBudgetOutcome,
    enforce_round_budget,
    turn_tail_start,
)

pytestmark = pytest.mark.unit

MODEL = "ollama/qwen2.5:3b"

# 真实形状的 tools（12 个内置工具的 schema 量级；Batch 18 实测 1126 token）
_TOOLS = [{"type": "function", "function": {
    "name": f"tool_{i}", "description": "做一件事情的说明文字" * 3,
    "parameters": {"type": "object", "properties": {
        p: {"type": "string", "description": "参数说明"} for p in ("a", "b", "c")},
        "required": ["a"]}}} for i in range(12)]


def _big_text(tokens: int) -> str:
    """接近单条工具结果上限（`max_result_tokens = 2000`）的一段文本。"""
    unit = "这是一段工具返回的长内容，模拟网页抓取或文件读取的真实结果。"
    text = unit
    while estimate_messages_tokens([{"role": "tool", "content": text}],
                                   model=MODEL) < tokens:
        text += unit
    return text


def _system(chars: int = 4000) -> dict:
    return {"role": "system", "content": "人设与规则说明。" * (chars // 8)}


def _history(n: int) -> list[dict]:
    out: list[dict] = []
    for i in range(n):
        out.append({"role": "user", "content": f"历史用户 {i}：" + "随便聊聊。" * 20})
        out.append({"role": "assistant", "content": f"历史回复 {i}：" + "好的呀。" * 20})
    return out


def _exchange(round_no: int, results: int = 1, tokens: int = 1990) -> list[dict]:
    """一组工具往返：assistant(tool_calls) + N 条 tool 结果 + 轮次标记。"""
    calls = [{"id": f"c{round_no}_{i}", "type": "function",
              "function": {"name": "web_fetch", "arguments": "{}"}}
             for i in range(results)]
    group: list[dict] = [{"role": "assistant", "content": None, "tool_calls": calls}]
    for c in calls:
        group.append({"role": "tool", "tool_call_id": c["id"], "content": _big_text(tokens)})
    group.append({"role": "system", "content": "[TOOL_EXECUTED]"})
    return group


def _round1(*, budget: int | None = None) -> list[dict]:
    """首轮形状（system + 历史 + 当前用户消息）。

    与生产首轮同一条路径：超预算时由旧到新丢历史（丢到只剩 system + 当前消息为止）。
    `budget=None` 表示"不裁"，用于只关心形状的用例。
    """
    messages = [_system(), *_history(8), {"role": "user", "content": "帮我查一下最新的消息。"}]
    if budget is not None:
        while len(messages) > 2 and estimate_messages_tokens(
                messages, model=MODEL, tools=_TOOLS) > budget:
            del messages[1]
    return messages


def _fits(messages: list[dict]) -> int:
    return estimate_messages_tokens(messages, model=MODEL, tools=_TOOLS)


def _has_orphan_tool(messages: list[dict]) -> bool:
    """是否存在孤儿 `role="tool"`（其前没有带 tool_calls 的 assistant）。"""
    for i, msg in enumerate(messages):
        if msg.get("role") != "tool":
            continue
        j = i - 1
        while j >= 0 and messages[j].get("role") == "tool":
            j -= 1
        if j < 0 or not (messages[j].get("role") == "assistant"
                         and messages[j].get("tool_calls")):
            return True
    return False


class TestEstimatorAdditivityPremise:
    """本模块用"逐条可加"把裁剪循环降到 O(n)——这条前提必须被钉住。"""

    def test_message_costs_are_additive(self):
        messages = [_system(), *_history(3), {"role": "user", "content": "在吗"},
                    {"role": "assistant", "content": None,
                     "tool_calls": [{"id": "c", "type": "function",
                                     "function": {"name": "f", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "c", "content": "结果"}]
        per_entry = sum(estimate_messages_tokens([m], model=MODEL) for m in messages)
        assert per_entry + estimate_tools_tokens(_TOOLS, model=MODEL) == \
            estimate_messages_tokens(messages, model=MODEL, tools=_TOOLS)


class TestEnforceRoundBudget:
    def test_over_budget_round_is_brought_back_into_budget(self):
        """**核心**：追加一组近上限工具结果后超窗 ⇒ 再预算后必须回到预算内。"""
        budget = 6144
        messages = _round1(budget=budget)
        assert _fits(messages) <= budget, "首轮本来就该在预算内（既有行为）"
        messages += _exchange(1)
        before = _fits(messages)
        assert before > budget, "前提不成立：这一组工具结果并没有把 prompt 顶出预算"

        outcome = enforce_round_budget(messages, budget, turn_start=turn_tail_start(messages),
                                       model_id=MODEL, tools=_TOOLS)

        after = estimate_messages_tokens(messages, model=MODEL, tools=_TOOLS)
        assert after <= budget, f"再预算后仍超预算：{after} > {budget}"
        assert after == outcome.after
        assert outcome.trimmed and not outcome.over_budget

    def test_history_is_dropped_before_the_current_exchange(self):
        """优先级：先丢历史（旧），最新一组工具往返必须完整保留。"""
        budget = 6144
        messages = _round1(budget=budget)
        assert _fits(messages) <= budget
        user_entry = messages[-1]
        messages += _exchange(1)
        tool_entries = [m for m in messages if m.get("role") == "tool"]

        enforce_round_budget(messages, budget, turn_start=turn_tail_start(messages),
                             model_id=MODEL, tools=_TOOLS)

        assert user_entry in messages, "当前用户消息永不丢"
        assert all(m in messages for m in tool_entries), "最新一组工具结果永不丢"
        # 尾部结构逐字保留：assistant(tool_calls) + tool 结果 + 轮次标记
        assert messages[-1] == {"role": "system", "content": "[TOOL_EXECUTED]"}
        assert messages[-2] is tool_entries[-1]
        assert messages[-3]["tool_calls"]

    def test_older_exchanges_are_dropped_as_whole_groups(self):
        """累积型：多轮之后丢**较早**的整组，且不产生孤儿 tool 消息。"""
        budget = 6144
        messages = _round1(budget=budget)
        assert _fits(messages) <= budget
        for r in range(1, 6):
            messages += _exchange(r)
        before = _fits(messages)
        assert before > budget

        outcome = enforce_round_budget(messages, budget, turn_start=turn_tail_start(messages),
                                       model_id=MODEL, tools=_TOOLS)

        after = estimate_messages_tokens(messages, model=MODEL, tools=_TOOLS)
        assert after <= budget, f"{after} > {budget}"
        assert outcome.dropped_exchanges >= 1
        assert not _has_orphan_tool(messages), "裁剪不得制造孤儿 tool 消息（严格 provider 400）"
        # 最新一轮的 tool_call_id 必须仍在
        last_id = f"c5_0"
        assert any(m.get("tool_call_id") == last_id for m in messages)

    def test_newest_exchange_is_truncated_when_dropping_is_not_enough(self):
        """三档：历史与旧组都不够时，截**最新一组**的 tool 结果（结构不动）。"""
        # 只装得下 system+tools+user+一条被截短的结果：历史丢光也不够 ⇒ 必须截结果
        messages = _round1()
        user_only = messages[:1] + messages[-1:]
        budget = _fits(user_only) + 200   # 骨架 + 截断标记 + 少量内容
        assert _fits(_round1(budget=budget)) <= budget, "首轮必须仍在预算内"
        messages = _round1(budget=budget)
        messages += _exchange(1)
        enforce_round_budget(messages, budget, turn_start=turn_tail_start(messages),
                             model_id=MODEL, tools=_TOOLS)
        after = _fits(messages)
        tool_entries = [m for m in messages if m.get("role") == "tool"]
        assert tool_entries, "最新一组不得被整组丢掉"
        assert "已截断" in tool_entries[-1]["content"], "截断必须留可见标记"
        assert len(tool_entries[-1]["content"]) < len(_big_text(1990))
        assert after <= budget, f"{after} > {budget}"
        assert not _has_orphan_tool(messages)

    def test_system_tools_and_current_user_are_never_dropped(self):
        budget = 1200  # 极端：连 system+tools 都装不下 ⇒ 如实报 over_budget，但不丢保护内容
        messages = _round1(budget=budget)
        assert _fits(messages) > budget, "前提：该预算下连保护内容都装不下"
        system = messages[0]
        user = messages[-1]
        messages += _exchange(1)
        outcome = enforce_round_budget(messages, budget, turn_start=turn_tail_start(messages),
                                       model_id=MODEL, tools=_TOOLS)
        assert messages[0] is system
        assert user in messages
        assert outcome.over_budget, "装不下时必须如实报告，而不是假装成功"

    def test_no_op_when_within_budget(self):
        budget = 100_000
        messages = _round1(budget=budget) + _exchange(1)
        snapshot = json.dumps(messages, ensure_ascii=False, sort_keys=True)
        outcome = enforce_round_budget(messages, budget, turn_start=turn_tail_start(messages),
                                       model_id=MODEL, tools=_TOOLS)
        assert outcome == RoundBudgetOutcome(budget=budget, before=outcome.before,
                                             after=outcome.before)
        assert not outcome.trimmed
        assert json.dumps(messages, ensure_ascii=False, sort_keys=True) == snapshot

    def test_orphan_tool_entry_is_never_grouped(self):
        """孤立 tool 条目不成组 ⇒ 宁可不裁，也不制造半截配对。"""
        budget = 900
        messages = [_system(200), {"role": "user", "content": "hi"},
                    {"role": "tool", "tool_call_id": "x", "content": _big_text(600)}]
        enforce_round_budget(messages, budget, turn_start=1, model_id=MODEL, tools=None)
        assert any(m.get("role") == "tool" for m in messages), "孤立 tool 条目不得被删"

    def test_multi_result_exchange_keeps_pairing(self):
        """一组里多条结果：截断由旧到新，配对结构完整。"""
        messages = _round1()
        user_only = messages[:1] + messages[-1:]
        budget = _fits(user_only) + 400
        messages = _round1(budget=budget)
        messages += _exchange(1, results=3)
        enforce_round_budget(messages, budget, turn_start=turn_tail_start(messages),
                             model_id=MODEL, tools=_TOOLS)
        tool_entries = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_entries) == 3
        # 由旧到新：较早的那条先被截得更狠（或至少不短于后一条）
        assert len(tool_entries[0]["content"]) <= len(tool_entries[-1]["content"])
        assert not _has_orphan_tool(messages)
