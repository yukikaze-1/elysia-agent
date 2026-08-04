# core/cognitive/tests/test_empty_response_guard.py
"""空 LLM 响应守卫单元测试 — _think_loop 对"成功但无内容"响应的兜底。

覆盖：无文本+无工具调用 → nudge 重试一次；仍空 → 兜底回复；正常路径零干扰；
纯工具调用轮（空文本）不触发守卫。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.cognitive.loop import (
    CognitiveLoop,
    _EMPTY_RESPONSE_FALLBACK,
    _EMPTY_RESPONSE_NUDGE,
)

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════

def _make_loop() -> CognitiveLoop:
    """创建最小 mock 的 CognitiveLoop，stub 掉重型依赖，仅驱动 _think_loop 守卫。"""
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


# 空响应元组形状：(acc_text, raw_tool_calls, speaking_started, all_sentences,
#                  iv_think, blocks, public_text)
_EMPTY = ("", [], False, [], "", [], "")


def _stream_side_effect(*responses):
    """构造 _stream_llm_response 的 side_effect：依次返回给定响应。"""
    return responses


# ═══════════════════════════════════════════════════════════════════
# 测试
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_normal_response_not_retried():
    """首轮即非空 → 不注入 nudge，_stream_llm_response 只调用一次。"""
    loop = _make_loop()
    valid = ("你好", [], True, ["你好"], "", [], "你好")
    loop._stream_llm_response = AsyncMock(side_effect=_stream_side_effect(valid))

    await loop._think_loop("hello", "user")

    assert loop._stream_llm_response.await_count == 1
    assert loop._finalize_turn.await_count == 1
    assert loop._finalize_turn.await_args.args[7] == "你好"
    # 未注入 nudge
    stream_args = loop._stream_llm_response.await_args.args
    assert all(m.get("content") != _EMPTY_RESPONSE_NUDGE for m in stream_args[0])


@pytest.mark.asyncio
async def test_empty_then_valid_retries_once_with_nudge():
    """首轮空 → 注入 nudge 重试一次；第二轮有内容 → 正常收尾。"""
    loop = _make_loop()
    valid = ("我在", [], True, ["我在"], "", [], "我在")
    loop._stream_llm_response = AsyncMock(
        side_effect=_stream_side_effect(_EMPTY, valid))

    await loop._think_loop("hello", "user")

    assert loop._stream_llm_response.await_count == 2
    assert loop._finalize_turn.await_count == 1
    assert loop._finalize_turn.await_args.args[7] == "我在"
    # 第二轮调用时 messages 里应已有 nudge（且仅一条）
    retry_messages = loop._stream_llm_response.await_args.args[0]
    nudges = [m for m in retry_messages if m.get("content") == _EMPTY_RESPONSE_NUDGE]
    assert len(nudges) == 1


@pytest.mark.asyncio
async def test_empty_twice_falls_back():
    """重试后仍空 → 兜底回复注入 public_text / acc_text / all_sentences。"""
    loop = _make_loop()
    loop._stream_llm_response = AsyncMock(
        side_effect=_stream_side_effect(_EMPTY, _EMPTY))

    await loop._think_loop("hello", "user")

    assert loop._stream_llm_response.await_count == 2
    assert loop._finalize_turn.await_count == 1
    args = loop._finalize_turn.await_args.args
    assert args[0] == _EMPTY_RESPONSE_FALLBACK      # acc_text
    assert args[1] == [_EMPTY_RESPONSE_FALLBACK]    # all_sentences
    assert args[7] == _EMPTY_RESPONSE_FALLBACK      # public_text


@pytest.mark.asyncio
async def test_empty_with_tool_calls_not_retried():
    """空文本但带工具调用 → 走工具轮，不触发空响应守卫。"""
    loop = _make_loop()
    tool_call = (
        "",                                    # acc_text
        [{"id": "call_1", "function": {"name": "get_time", "arguments": "{}"}}],
        False, [], "", [], "",                 # 空 public_text
    )
    valid = ("完成", [], True, ["完成"], "", [], "完成")
    loop._stream_llm_response = AsyncMock(
        side_effect=_stream_side_effect(tool_call, valid))

    await loop._think_loop("hello", "user")

    assert loop._stream_llm_response.await_count == 2
    assert loop._execute_tool_round.await_count == 1
    assert loop._finalize_turn.await_count == 1
    # 未注入 nudge（工具轮正常执行，第二轮的 [TOOL_EXECUTED] 与 nudge 不同）
    retry_messages = loop._stream_llm_response.await_args.args[0]
    assert all(m.get("content") != _EMPTY_RESPONSE_NUDGE for m in retry_messages)


@pytest.mark.asyncio
async def test_tags_only_retries_then_falls_back():
    """只输出 <mood>/<inner_voice> 标签、无 speech → 守卫触发 → 重试 → 兜底。

    acc_text 是含标签的非空输出（loop.py:425 'LLM 原始输出全文（含标签）'），
    钉死'守卫基于 public_text 而非 acc_text'——若误改 acc_text.strip() 判断，
    此测试立即失败（acc_text 非空 → 守卫不触发）。
    """
    loop = _make_loop()
    tags_only = (
        "<mood>开心</mood><inner_voice>我有点累</inner_voice>",  # acc_text（含标签）
        [],                       # raw_tool_calls
        False,                    # speaking_started
        [],                       # all_sentences
        "我有点累",               # iv_think
        [],                       # blocks
        "",                       # public_text（无 speech）
    )
    loop._stream_llm_response = AsyncMock(
        side_effect=_stream_side_effect(tags_only, tags_only))

    await loop._think_loop("hello", "user")

    assert loop._stream_llm_response.await_count == 2
    assert loop._finalize_turn.await_count == 1
    args = loop._finalize_turn.await_args.args
    assert args[0] == _EMPTY_RESPONSE_FALLBACK      # acc_text → 兜底
    assert args[1] == [_EMPTY_RESPONSE_FALLBACK]    # all_sentences
    assert args[2] == "我有点累"                    # iv_think 保留
    assert args[7] == _EMPTY_RESPONSE_FALLBACK      # public_text


@pytest.mark.asyncio
async def test_whitespace_only_triggers_guard():
    """纯空白 public_text → 守卫触发 → nudge 重试。"""
    loop = _make_loop()
    ws = ("", [], False, [], "", [], "\n\n  ")
    valid = ("我在", [], True, ["我在"], "", [], "我在")
    loop._stream_llm_response = AsyncMock(
        side_effect=_stream_side_effect(ws, valid))

    await loop._think_loop("hello", "user")

    assert loop._stream_llm_response.await_count == 2
    assert loop._finalize_turn.await_count == 1


@pytest.mark.asyncio
async def test_emoji_only_no_guard():
    """纯 emoji 是可见回复 → 守卫不触发 → 直接收尾。"""
    loop = _make_loop()
    emoji = ("😊", [], False, ["😊"], "", [], "😊")
    loop._stream_llm_response = AsyncMock(side_effect=_stream_side_effect(emoji))

    await loop._think_loop("hello", "user")

    assert loop._stream_llm_response.await_count == 1
    assert loop._finalize_turn.await_count == 1
    # 未注入 nudge
    stream_args = loop._stream_llm_response.await_args.args
    assert all(m.get("content") != _EMPTY_RESPONSE_NUDGE for m in stream_args[0])
