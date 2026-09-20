# 测试文件：core/cognitive/tests/test_react_round_budget.py
"""KB-77 / Batch 19：ReAct **每一轮**的 prompt 都必须在同一份预算内。

真实路径驱动（不是"调用了某个函数"）：真实 `CognitiveLoop._think_loop` + 真实
`_execute_tool_round`/`_dispatch_tool_calls`（工具结果**真的**进入 messages）+ 真实
`MessageBuilder` 首轮裁剪 + canonical 估算权威 `Module.system_prompt.budget`。

修前实测（Qwen2.5-3b / 8192 窗口 / 6144 预算）：首轮 6123（预算内），追加一条接近
单条上限（`max_result_tokens = 2000`）的工具结果后第 2 轮 **8205 > 8192 窗口**。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.cognitive.loop import CognitiveLoop
from core.cognitive.timing import TimingClock
from core.prompt.message_builder import MessageBuilder
from core.schema.message import AssistantMessage, UserMessage
from core.schema.part import TextPart
from core.schema.types import MessageId
from Module.llm.registry import LLMFactory
from Module.system_prompt.budget import (
    budget_from_context_window,
    estimate_messages_tokens,
    estimate_tokens,
)
from Module.tool.protocol import ToolResult, to_llm_name

pytestmark = pytest.mark.unit

MODEL = "ollama/qwen2.5:3b"
WINDOW = LLMFactory.context_window(MODEL)
BUDGET = budget_from_context_window(WINDOW)

_SYSTEM = "你是 Elysia。\n" + "这是一段用于填充 system prompt 的角色设定与输出格式说明。" * 60
_TOOL_NAMES = tuple(f"web_{i}" for i in range(12))
_TOOLS = [{"type": "function", "function": {
    "name": f"web_{i}", "description": "说明文字" * 4,
    "parameters": {"type": "object",
                   "properties": {"q": {"type": "string", "description": "参数"}},
                   "required": ["q"]}}} for i in range(12)]
assert [t["function"]["name"] for t in _TOOLS] == list(_TOOL_NAMES)


def _big_result() -> str:
    unit = "工具返回的长内容，模拟网页抓取的真实结果。"
    text = unit
    while estimate_tokens(text, model=MODEL) < 1990:
        text += unit
    return text


def _history() -> list:
    out: list = []
    for i in range(8):
        out.append(UserMessage(content=[TextPart(content=f"历史用户 {i}：" + "随便聊聊。" * 20)],
                               message_id=MessageId(f"h_u{i}")))
        out.append(AssistantMessage(content=[TextPart(content=f"历史回复 {i}：" + "好的呀。" * 20)],
                                    message_id=MessageId(f"h_a{i}")))
    return out


class _FakeOutput:
    def __init__(self) -> None:
        self.frames: list[tuple[str, str]] = []

    async def tool_started(self, **kw: object) -> None:
        self.frames.append(("started", str(kw.get("tool_name"))))

    async def tool_completed(self, **kw: object) -> None:
        self.frames.append(("completed", str(kw.get("tool_name"))))

    async def typing(self, *_a: object, **_k: object) -> None:
        pass

    async def turn_started(self, *_a: object, **_k: object) -> None:
        pass


class _FakeSession:
    def __init__(self) -> None:
        self.session_id = "default"
        self.added: list = []

    def add_message(self, msg: object) -> None:
        self.added.append(msg)


class _FakePhases:
    async def enter_executing(self) -> None:
        pass

    async def return_to_thinking(self) -> None:
        pass


class _FakeToolAdapter:
    def runtime_for(self, _n: str) -> str:
        return "backend"

    def resolve_llm_name(self, llm_name: str) -> str | None:
        for n in _TOOL_NAMES:
            if to_llm_name(n) == llm_name:
                return n
        return None

    def definitions(self) -> list[dict]:
        return _TOOLS


def _make_loop(rounds: int, results_per_round: int, recorder: list[dict]) -> CognitiveLoop:
    loop = CognitiveLoop(
        llm=MagicMock(), fsm=MagicMock(), event_bus=MagicMock(), output=MagicMock(),
        prompt=MagicMock(), session_resolver=AsyncMock())
    loop._llm.model = MODEL
    loop._character_engine = None
    loop._prompt_recorder = None
    loop._token_tracker = None
    loop._output = _FakeOutput()
    loop._clock = TimingClock()
    loop._session = _FakeSession()
    loop._tool_adapter = _FakeToolAdapter()
    loop._delegation_preparer = None
    loop._executing_tools = []
    loop._generation_ctx = None
    loop._task_system = None
    loop._on_background_task = None
    loop.phases = _FakePhases()
    loop._turn_character_snapshot = lambda: None
    loop._tts_mgr = MagicMock()
    loop._tts_mgr.cancel_all = MagicMock()

    async def _build(user_message, user_name, content, stt_emotion, stt_events,
                     is_task_result, prompt_budget=None, user_message_id=None,
                     conversation_id=None):
        # 修前 `_build_turn_prompt` 自己派生预算（无该形参）——两种调用形状都要能跑，
        # 这样同一份用例在**修前**失败的理由是"第 2 轮超预算"，而不是签名不匹配。
        if prompt_budget is None:
            prompt_budget = budget_from_context_window(LLMFactory.context_window(MODEL))
        assert prompt_budget == BUDGET, "首轮预算必须来自 canonical 派生"
        msgs = MessageBuilder().build_messages(
            system_prompt=_SYSTEM, history=_history(), user_content=user_message,
            prompt_budget_tokens=prompt_budget, model_id=MODEL, tools=_TOOLS)
        return msgs, list(_TOOLS)

    loop._build_turn_prompt = _build
    loop._start_turn_tracking = AsyncMock()
    loop._finalize_turn = AsyncMock()

    async def _stream(messages, tool_defs, round_num, session_id=None, session=None):
        recorder.append({
            "round": round_num,
            "est": estimate_messages_tokens(messages, model=MODEL, tools=tool_defs),
            "roles": [m.get("role") for m in messages],
        })
        if round_num < rounds:
            return "", [{"id": f"c{round_num}_{i}", "type": "function",
                         "function": {"name": "web_0", "arguments": "{}"}}
                        for i in range(results_per_round)], False, [], "", [], ""
        return "好的，总结如下。", [], True, ["好的，总结如下。"], "", [], "好的，总结如下。"

    loop._stream_llm_response = _stream

    async def _run(dispatch_tcs):
        return [(tc, ToolResult.ok(name=tc.function.name, content=_big_result(),
                                   duration_ms=1.0, truncated=True))
                for tc in dispatch_tcs]

    loop._execute_tools_directly = _run
    return loop


@pytest.mark.asyncio
async def test_every_react_round_stays_within_budget():
    """**PRE_FIX_RED**：修前第 2 轮就 8205 > 6144（且 > 8192 窗口）。"""
    recorder: list[dict] = []
    loop = _make_loop(rounds=5, results_per_round=1, recorder=recorder)
    await loop._think_loop("帮我上网查一下。", "user", session=loop._session)

    assert len(recorder) == 6, recorder
    assert recorder[0]["est"] <= BUDGET, "首轮本来就该在预算内（既有行为）"
    for entry in recorder:
        assert entry["est"] <= BUDGET, (
            f"第 {entry['round']} 轮超预算：{entry['est']} > {BUDGET}（窗口 {WINDOW}）")
        assert entry["est"] <= WINDOW, f"第 {entry['round']} 轮超**窗口**：{entry['est']}"


@pytest.mark.asyncio
async def test_multi_result_rounds_stay_within_window():
    """一轮多个近上限结果（并行工具调用）同样不得超窗。"""
    recorder: list[dict] = []
    loop = _make_loop(rounds=4, results_per_round=3, recorder=recorder)
    await loop._think_loop("并行查三个来源。", "user", session=loop._session)
    worst = max(r["est"] for r in recorder)
    assert worst <= BUDGET, f"最坏一轮 {worst} > 预算 {BUDGET}"


@pytest.mark.asyncio
async def test_latest_tool_result_is_visible_to_the_next_round():
    """裁剪不得吃掉**刚刚**执行的那一组：下一轮必须看得到它的 tool_call_id。"""
    seen: list[list[str]] = []
    recorder: list[dict] = []
    loop = _make_loop(rounds=5, results_per_round=1, recorder=recorder)
    original = loop._stream_llm_response

    async def _stream(messages, tool_defs, round_num, session_id=None, session=None):
        seen.append([str(m.get("tool_call_id")) for m in messages
                     if m.get("role") == "tool"])
        return await original(messages, tool_defs, round_num, session_id, session)

    loop._stream_llm_response = _stream
    await loop._think_loop("查一下。", "user", session=loop._session)

    for round_num in range(1, 6):
        assert f"c{round_num - 1}_0" in seen[round_num], (
            f"第 {round_num} 轮看不到上一轮的 tool 结果：{seen[round_num]}")


@pytest.mark.asyncio
async def test_tool_pairs_stay_intact_and_never_orphaned():
    """wire 有效性：任何一轮都不得出现孤儿 `role="tool"`。"""
    recorder: list[dict] = []
    loop = _make_loop(rounds=5, results_per_round=2, recorder=recorder)
    snapshots: list[list[dict]] = []
    original = loop._stream_llm_response

    async def _stream(messages, tool_defs, round_num, session_id=None, session=None):
        snapshots.append([dict(m) for m in messages])
        return await original(messages, tool_defs, round_num, session_id, session)

    loop._stream_llm_response = _stream
    await loop._think_loop("查两件事。", "user", session=loop._session)

    for i, snapshot in enumerate(snapshots):
        for j, msg in enumerate(snapshot):
            if msg.get("role") != "tool":
                continue
            k = j - 1
            while k >= 0 and snapshot[k].get("role") == "tool":
                k -= 1
            assert k >= 0 and snapshot[k].get("role") == "assistant" \
                and snapshot[k].get("tool_calls"), f"第 {i} 轮出现孤儿 tool 消息"


@pytest.mark.asyncio
async def test_session_and_frames_are_unaffected_by_prompt_trimming():
    """CT/SP 不受影响：会话里存的是**完整**结果，帧集合与字段不变。"""
    recorder: list[dict] = []
    loop = _make_loop(rounds=3, results_per_round=1, recorder=recorder)
    await loop._think_loop("查一下。", "user", session=loop._session)

    stored = [m for m in loop._session.added if getattr(m, "role", "") == "tool"]
    assert len(stored) == 3, "每条工具结果都必须进会话（真相不因 prompt 裁剪而改变）"
    full = _big_result()
    for msg in stored:
        text = "".join(p.content for p in msg.content if isinstance(p, TextPart))
        assert text == full, "会话里必须保留完整结果（裁剪只作用于 prompt 副本）"
    names = [n for kind, n in loop._output.frames if kind == "completed"]
    assert names == ["web_0"] * 3


@pytest.mark.asyncio
async def test_rebudget_never_raises_on_malformed_entries():
    """热路径安全：畸形条目不得让**回合**失败（只允许不裁）。

    取消语义本身由既有 CF/TH 套件覆盖（`test_thinking_cancellation` /
    `test_cancellation_safe_finalization`）——本再预算步骤是**同步**的，不持有也不会
    吞掉任何 await 点。
    """
    recorder: list[dict] = []
    loop = _make_loop(rounds=2, results_per_round=1, recorder=recorder)
    original = loop._execute_tools_directly

    async def _weird(dispatch_tcs):
        tc = dispatch_tcs[0]
        return [(tc, ToolResult.ok(name=tc.function.name, content="x" * 10,
                                   duration_ms=1.0))]

    loop._execute_tools_directly = _weird
    await loop._think_loop("查一下。", "user", session=loop._session)
    assert max(r["est"] for r in recorder) <= BUDGET

    # 直接调用：畸形 content（list/None/缺 content）不得抛
    from core.prompt.message_builder import enforce_round_budget, turn_tail_start
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c"}]},
        {"role": "tool", "tool_call_id": "c", "content": [{"type": "text", "text": "块"}]},
        {"role": "tool", "tool_call_id": "c2", "content": None},
        {"role": "assistant", "content": "无 tool_calls"},
        {"role": "system", "content": "[TOOL_EXECUTED]"},
    ]
    enforce_round_budget(messages, 10, turn_start=turn_tail_start(messages),
                         model_id=MODEL, tools=_TOOLS)
    _ = original


@pytest.mark.asyncio
async def test_round_budget_disabled_when_there_are_no_tools():
    """无工具回合：`_build_turn_prompt` 之后不再有任何再预算调用（零额外成本）。"""
    recorder: list[dict] = []
    loop = _make_loop(rounds=0, results_per_round=0, recorder=recorder)
    await loop._think_loop("你好。", "user", session=loop._session)
    assert len(recorder) == 1
    assert recorder[0]["est"] <= BUDGET
    assert json.dumps(recorder[0]["roles"], ensure_ascii=False)  # 形状可序列化（诊断用）
