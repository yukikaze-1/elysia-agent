# 测试文件：core/cognitive/tests/test_tool_round_limit.py
"""ReAct 轮次边界测试（CR-11）：工具轮触顶必须响亮失败，回合必须收尾。

覆盖两类静默丢弃：
1. **触顶** —— `range(MAX_TOOL_ROUNDS + 1)` 的收尾轮若仍请求工具，这些调用
   不会被执行；原先无任何 else 分支，工具名/轮次零痕迹，且该轮无文本时用户
   收到彻底的空回复（空响应守卫的条件是 `not raw_tool_calls`，此处不成立）。
2. **循环耗尽** —— 末轮的空响应守卫若返回 retry，其 `continue` 会耗尽 for 循环，
   `process()` 无收尾地返回 None（无输出、无 typing 关闭）。
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.cognitive.loop import (
    CognitiveLoop,
    _EMPTY_RESPONSE_FALLBACK,
    _TOOL_LIMIT_FALLBACK,
)

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════════

def _make_loop() -> CognitiveLoop:
    """最小 mock 的 CognitiveLoop，仅驱动 ReAct 循环的轮次边界。"""
    loop = CognitiveLoop(
        llm=MagicMock(),
        fsm=MagicMock(),
        event_bus=MagicMock(),
        output=MagicMock(),
        prompt=MagicMock(),
        session_resolver=AsyncMock(),
    )
    loop._character_engine = None
    loop._prompt_recorder = None
    loop._token_tracker = None
    loop._output = MagicMock()
    loop._output.typing = AsyncMock()
    loop._build_turn_prompt = AsyncMock(return_value=([], []))
    loop._execute_tool_round = AsyncMock()
    loop._finalize_turn = AsyncMock()
    return loop


def _tool_call(name: str, call_id: str = "c1") -> dict:
    """raw_tool_calls 的元素形状（merge_stream_tool_calls 的产出）。"""
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


def _resp(text: str = "", tools: list[dict] | None = None) -> tuple:
    """_stream_llm_response 的返回元组（7 元）。"""
    tools = tools or []
    sentences = [text] if text else []
    return (text, tools, bool(text), sentences, "", [], text)


# ═══════════════════════════════════════════════════════════════════
# 1. 触顶：工具轮次边界
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_only_max_rounds_execute_tools():
    """LLM 连续每轮都请求工具 → 只有 MAX_TOOL_ROUNDS 轮真的执行。"""
    loop = _make_loop()
    loop._stream_llm_response = AsyncMock(
        side_effect=[_resp("", [_tool_call("web_search")])
                     for _ in range(loop.MAX_TOOL_ROUNDS + 1)])

    await loop._think_loop("hello", "user")

    assert loop._execute_tool_round.await_count == loop.MAX_TOOL_ROUNDS
    assert loop._finalize_turn.await_count == 1


@pytest.mark.asyncio
async def test_tool_limit_logs_warning_with_tool_names(caplog):
    """触顶必须留痕：WARNING 含轮次与**被丢弃的工具名**（原先零痕迹）。"""
    loop = _make_loop()
    loop._stream_llm_response = AsyncMock(
        side_effect=[_resp("", [_tool_call("web_search", "c1")])
                     for _ in range(loop.MAX_TOOL_ROUNDS + 1)])

    with caplog.at_level(logging.WARNING, logger="core.cognitive.loop"):
        await loop._think_loop("hello", "user")

    hit = [r for r in caplog.records if "工具调用轮次触顶" in r.getMessage()]
    assert len(hit) == 1, caplog.text
    msg = hit[0].getMessage()
    assert str(loop.MAX_TOOL_ROUNDS) in msg
    assert "web_search" in msg, msg


@pytest.mark.asyncio
async def test_tool_limit_never_yields_empty_reply(caplog):
    """触顶且该轮无文本 → 用户拿到兜底文案，不是空回复。"""
    loop = _make_loop()
    loop._stream_llm_response = AsyncMock(
        side_effect=[_resp("", [_tool_call("web_search")])
                     for _ in range(loop.MAX_TOOL_ROUNDS + 1)])

    with caplog.at_level(logging.WARNING, logger="core.cognitive.loop"):
        await loop._think_loop("hello", "user")

    public_text = loop._finalize_turn.await_args.args[7]
    assert public_text.strip(), "触顶回合不得产出空回复"
    assert public_text == _TOOL_LIMIT_FALLBACK
    # all_sentences 必须同步，否则 session 归档与前端显示不一致
    assert loop._finalize_turn.await_args.args[1] == [_TOOL_LIMIT_FALLBACK]


@pytest.mark.asyncio
async def test_tool_limit_keeps_real_text_when_present():
    """触顶但该轮**有文本** → 保留 LLM 的真实回复，不被兜底覆盖。"""
    loop = _make_loop()
    rounds = [_resp("", [_tool_call("web_search")])
              for _ in range(loop.MAX_TOOL_ROUNDS)]
    rounds.append(_resp("我查到了一些线索。", [_tool_call("web_search")]))
    loop._stream_llm_response = AsyncMock(side_effect=rounds)

    await loop._think_loop("hello", "user")

    assert loop._finalize_turn.await_args.args[7] == "我查到了一些线索。"


@pytest.mark.asyncio
async def test_tool_limit_signal_is_reachable_when_text_and_tools(caplog):
    """有文本的触顶同样要留 warning —— 用户没察觉不代表没发生。"""
    loop = _make_loop()
    rounds = [_resp("", [_tool_call("web_search")])
              for _ in range(loop.MAX_TOOL_ROUNDS)]
    rounds.append(_resp("先这样。", [_tool_call("web_search")]))
    loop._stream_llm_response = AsyncMock(side_effect=rounds)

    with caplog.at_level(logging.WARNING, logger="core.cognitive.loop"):
        await loop._think_loop("hello", "user")

    assert any("工具调用轮次触顶" in r.getMessage() for r in caplog.records), caplog.text
    # 有文本时不覆盖 LLM 的真实回复
    assert loop._finalize_turn.await_args.args[7] == "先这样。"


# ═══════════════════════════════════════════════════════════════════
# 2. 循环耗尽：末轮空响应不得吞掉整个回合
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_last_round_empty_gets_fallback_not_silence(caplog):
    """末轮空响应（无文本无工具）→ 直接兜底，不重试（重试会耗尽循环）。

    注意断言必须能区分两条都产出同一兜底文案的路径：
    「末轮直接兜底」（正确）与「重试 → 循环耗尽 → 防御网」（缺陷）。
    区分点是**没有**防御网的 error 日志，且 `_resolve_empty_response` 走了
    兜底分支（`_empty_response_count` +1）。
    """
    loop = _make_loop()
    tool_rounds = [_resp("", [_tool_call("web_search")])
                   for _ in range(loop.MAX_TOOL_ROUNDS)]
    tool_rounds.append(_resp("", []))          # 末轮空
    loop._stream_llm_response = AsyncMock(side_effect=tool_rounds)

    with caplog.at_level(logging.ERROR, logger="core.cognitive.loop"):
        await loop._think_loop("hello", "user")

    assert loop._stream_llm_response.await_count == loop.MAX_TOOL_ROUNDS + 1
    assert loop._finalize_turn.await_count == 1
    assert loop._finalize_turn.await_args.args[7] == _EMPTY_RESPONSE_FALLBACK
    assert not any("ReAct 循环耗尽" in r.getMessage() for r in caplog.records), \
        "末轮不得靠防御网收尾——说明 can_retry 门失效了"
    assert loop._empty_response_count == 1


@pytest.mark.asyncio
async def test_non_final_round_still_retries_empty(caplog):
    """非末轮空响应仍走 nudge 重试一次（原行为不变）。"""
    loop = _make_loop()
    loop._stream_llm_response = AsyncMock(
        side_effect=[_resp("", []), _resp("我在。", [])])

    with caplog.at_level(logging.WARNING, logger="core.cognitive.loop"):
        await loop._think_loop("hello", "user")

    assert loop._stream_llm_response.await_count == 2
    assert loop._finalize_turn.await_args.args[7] == "我在。"


@pytest.mark.asyncio
async def test_loop_exhaustion_defensive_net_finalizes(caplog):
    """防御网：任何绕开轮次预算的 `continue` 都不得让回合凭空消失。

    模拟未来的回归——把 `_resolve_empty_response` 换成"永远 retry"，
    即等同于在循环里加了一条无条件 `continue`。
    """
    loop = _make_loop()
    loop._stream_llm_response = AsyncMock(
        side_effect=[_resp("", []) for _ in range(loop.MAX_TOOL_ROUNDS + 1)])
    loop._resolve_empty_response = MagicMock(return_value=("", "", [], True))

    with caplog.at_level(logging.ERROR, logger="core.cognitive.loop"):
        result = await loop._think_loop("hello", "user")

    assert result is None
    # 关键：仍然收尾了一次，且用户拿到兜底文案
    assert loop._finalize_turn.await_count == 1
    assert loop._finalize_turn.await_args.args[7] == _EMPTY_RESPONSE_FALLBACK
    assert any("ReAct 循环耗尽" in r.getMessage() for r in caplog.records), caplog.text
