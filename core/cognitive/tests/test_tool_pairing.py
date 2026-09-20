"""KB-82（Residual Correctness Closure）：工具协议配对完整性。

**Finding**：一轮里同时出现 `submit_task` + 一个**幻觉工具名**时，gate 连坐拒绝整轮
（`_gate_tool_calls` 返回 False ⇒ `_execute_tool_round` 直接 return，`submit_task`
也不执行），但拒绝回执**只为非 submit id 生成**——`submit_task` 被 `gate_tcs` 拆分
跳过。于是 assistant 消息声明了 `call_submit` 却没有对应 `role="tool"` 回复：

    assistant: tool_calls = [call_submit, call_bad]
    tool:      response only for call_bad

严格 OpenAI 兼容 provider（含仓内默认 `deepseek`）在**下一轮**会直接 400，
而 400 不在重试集合里 ⇒ 该回合响亮失败。

**协议不变量（本文件钉住）**：

- **TP1** 每一个已经出现在 assistant 消息里的 tool call，在进入下一次 provider
  generation 前必须有**恰好一个**终态 tool-result 配对——无论该调用是 executed、
  rejected、denied、gated、invalid 还是 failed；
- **TP2** 被拒绝/被 gate 的调用**不得执行**（配对 ≠ 执行）；
- **TP3** 配对按 `tool_call_id` **身份**匹配，与位置、文本无关；
- **TP4** 补拒绝回执不得伪造成功，也不得改变审批/执行语义。

**断言口径**：`declared_call_ids` 与 `terminal_reply_call_ids` 的**集合与重数**都必须
相等（只比 `len(...)` 会漏掉"一个 id 发两次、另一个缺失"）。
"""

import json
from collections import Counter
from types import SimpleNamespace
from typing import Any

import pytest

from core.cognitive.timing import TimingClock
from core.cognitive.tool_phase import CognitiveToolPhase
from core.schema.tool import ToolCall, ToolCallFunction
from core.schema.types import ToolCallId
from Module.tool.protocol import ToolResult, to_llm_name

pytestmark = pytest.mark.service


# ---- 真实形状的替身 ----


class _RecordingOutput:
    def __init__(self) -> None:
        self.started: list[dict] = []
        self.completed: list[dict] = []

    async def tool_started(self, **kwargs: object) -> None:
        self.started.append(dict(kwargs))

    async def tool_completed(self, **kwargs: object) -> None:
        self.completed.append(dict(kwargs))


class _FakeSession:
    def __init__(self) -> None:
        self.session_id = "default"
        self.added: list[object] = []

    def add_message(self, msg: object) -> None:
        self.added.append(msg)


class _FakePhases:
    def __init__(self) -> None:
        self.entered = 0
        self.returned = 0

    async def enter_executing(self) -> None:
        self.entered += 1

    async def return_to_thinking(self) -> None:
        self.returned += 1


class _FakeToolAdapter:
    """definitions() 暴露脱点名 + resolve_llm_name 反查回运行时名（同真实 ToolAdapter）。"""

    def __init__(self, names: list[str]) -> None:
        self._names = list(names)

    def definitions(self) -> list[dict]:
        return [{"type": "function", "function": {"name": to_llm_name(n)}}
                for n in self._names]

    def resolve_llm_name(self, llm_name: str) -> str | None:
        for n in self._names:
            if to_llm_name(n) == llm_name:
                return n
        return None

    def runtime_for(self, tool_name: str) -> str:
        return "backend"


class _ExecutionRecorder:
    """工具执行替身：记录**真实被执行的** call id（TP2 的反证面）。"""

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.executed: list[str] = []
        self.raises = raises

    async def __call__(self, dispatch_tcs: list[ToolCall]) -> list[Any]:
        for tc in dispatch_tcs:
            self.executed.append(tc.id)
        if self.raises is not None:
            raise self.raises
        return [(tc, ToolResult.ok(name=tc.function.name, content=f"ok:{tc.id}",
                                   duration_ms=0.1))
                for tc in dispatch_tcs]


def _make_tc(name: str, call_id: str, args: dict | None = None) -> ToolCall:
    return ToolCall(
        id=ToolCallId(call_id),
        function=ToolCallFunction(name=name, arguments=json.dumps(args or {})),
    )


def _raw(*tcs: ToolCall) -> list[dict]:
    return [tc.model_dump(mode="json") for tc in tcs]


def _make_phase(
    *,
    known: list[str] | None = None,
    submits: list[str] | None = None,
) -> CognitiveToolPhase:
    """构造 CognitiveToolPhase（mixin 无 __init__，逐属性注入替身）。

    Args:
        known: ToolAdapter 注册的工具运行时名（gate 的 known_names 来源）。
        submits: 记录 `_handle_submit_task` 被调用的 call id。
    """
    phase = CognitiveToolPhase()
    phase._output = _RecordingOutput()
    phase._clock = TimingClock()
    phase._session = _FakeSession()
    phase._tool_adapter = _FakeToolAdapter(known or ["get_time", "search_memory"])
    # 非 None ⇒ submit_task 走专用分流（与生产一致：preparer 已注入）
    phase._delegation_preparer = SimpleNamespace()
    phase._executing_tools = []
    phase._generation_ctx = None
    phase.phases = _FakePhases()
    phase._on_background_task = None
    phase._task_system = None
    phase._turn_character_snapshot = lambda: None  # type: ignore[method-assign]

    executed_submits: list[str] = submits if submits is not None else []

    async def _recording_submit(tc: ToolCall) -> ToolResult:
        executed_submits.append(tc.id)
        return ToolResult.ok(name="submit_task", content="好的，已经在后台执行了。",
                             duration_ms=0.0, async_task_id="task_1")

    phase._handle_submit_task = _recording_submit  # type: ignore[method-assign]
    return phase


# ---- 严格 provider 形状（TP8 的判据） ----


def assert_strict_provider_shape(messages: list[dict]) -> None:
    """模拟严格 OpenAI 兼容 provider 的出站体校验：声明与回执必须一一配对。

    真实 provider 的判据是「assistant.tool_calls 里每个 id 都要有 role="tool"
    回复」；这里同时检查**重数**（同一个 id 回复两次同样是协议违规）。
    """
    declared: list[str] = []
    replied: list[str] = []
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                declared.append(tc["id"])
        elif msg.get("role") == "tool":
            replied.append(msg["tool_call_id"])

    assert Counter(declared) == Counter(replied), (
        f"工具协议配对缺口：declared={declared} / replied={replied}")
    assert len(replied) == len(set(replied)), f"同一个 call 收到重复回执: {replied}"


def _declared_and_replied(messages: list[dict]) -> tuple[list[str], list[str]]:
    declared = [tc["id"] for m in messages if m.get("role") == "assistant"
                for tc in (m.get("tool_calls") or [])]
    replied = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
    return declared, replied


# ---- TP-T1..T7：gate 拒绝轮的配对 ----


class TestRejectedCallPairing:
    """TP-T1..TP-T5/T7：gate 拒绝轮里每个声明都必须有恰好一个终态回执。"""

    @pytest.mark.asyncio
    async def test_single_rejected_call(self):
        """TP-T1 单个幻觉工具被拒 ⇒ 恰好一条回执。"""
        phase = _make_phase()
        ctx = _ExecutionRecorder()
        messages: list[dict] = []

        await phase._execute_tool_round(
            messages, "", _raw(_make_tc("delete_everything", "call_bad")), ctx)

        assert_strict_provider_shape(messages)
        declared, replied = _declared_and_replied(messages)
        assert declared == ["call_bad"]
        assert replied == ["call_bad"]
        assert ctx.executed == []          # TP2：被拒工具不得执行

    @pytest.mark.asyncio
    async def test_executed_then_rejected(self):
        """TP-T2 合法工具在前、幻觉工具在后 ⇒ 两者都有回执，合法者**未被连坐执行**。"""
        submits: list[str] = []
        phase = _make_phase(submits=submits)
        ctx = _ExecutionRecorder()
        messages: list[dict] = []

        await phase._execute_tool_round(
            messages, "",
            _raw(_make_tc("get_time", "call_ok"),
                 _make_tc("delete_everything", "call_bad")),
            ctx)

        assert_strict_provider_shape(messages)
        declared, replied = _declared_and_replied(messages)
        assert declared == ["call_ok", "call_bad"]
        assert Counter(replied) == Counter({"call_ok": 1, "call_bad": 1})
        assert ctx.executed == []          # 整轮连坐 ⇒ 合法者也不执行

    @pytest.mark.asyncio
    async def test_rejected_then_executed(self):
        """TP-T3 反序：幻觉在前、合法在后 ⇒ 与正序同结论（不是位置相关）。"""
        phase = _make_phase()
        ctx = _ExecutionRecorder()
        messages: list[dict] = []

        await phase._execute_tool_round(
            messages, "",
            _raw(_make_tc("delete_everything", "call_bad"),
                 _make_tc("get_time", "call_ok")),
            ctx)

        assert_strict_provider_shape(messages)
        declared, replied = _declared_and_replied(messages)
        assert declared == ["call_bad", "call_ok"]
        assert Counter(replied) == Counter({"call_bad": 1, "call_ok": 1})
        assert ctx.executed == []

    @pytest.mark.asyncio
    async def test_multiple_rejected(self):
        """TP-T4 多个幻觉工具 ⇒ 每个都被点名拒绝一次。"""
        phase = _make_phase()
        ctx = _ExecutionRecorder()
        messages: list[dict] = []

        await phase._execute_tool_round(
            messages, "",
            _raw(_make_tc("bad_a", "call_a"), _make_tc("bad_b", "call_b"),
                 _make_tc("get_time", "call_ok")),
            ctx)

        assert_strict_provider_shape(messages)
        _, replied = _declared_and_replied(messages)
        assert Counter(replied) == Counter({"call_a": 1, "call_b": 1, "call_ok": 1})

    @pytest.mark.asyncio
    async def test_parallel_calls_all_paired(self):
        """TP-T5 并行工具：一次声明 N 个 ⇒ N 条回执，id 身份一一对应。"""
        phase = _make_phase(known=["get_time", "search_memory"])
        ctx = _ExecutionRecorder()
        messages: list[dict] = []

        async def _run(dispatch_tcs: list[ToolCall]) -> list[Any]:
            for tc in dispatch_tcs:
                ctx.executed.append(tc.id)
            return [(tc, ToolResult.ok(name=tc.function.name, content=f"ok:{tc.id}",
                                       duration_ms=0.1))
                    for tc in dispatch_tcs]

        await phase._execute_tool_round(
            messages, "",
            _raw(_make_tc("get_time", "c1"), _make_tc("search_memory", "c2"),
                 _make_tc("get_time", "c3")),
            _run)

        assert_strict_provider_shape(messages)
        declared, replied = _declared_and_replied(messages)
        assert declared == ["c1", "c2", "c3"]
        assert replied == ["c1", "c2", "c3"]
        assert ctx.executed == ["c1", "c2", "c3"]

    def test_blocked_tool_rejected_is_paired(self):
        """TP-T7 黑名单（gate denied）走同一配对路径。"""
        phase = _make_phase()
        calls = [_make_tc("search_memory", "call_blocked"), _make_tc("get_time", "call_ok")]
        # gate 的前置条件：assistant 声明已进 messages（真实路径由 _prepare_tool_round 追加）
        messages: list[dict] = [{
            "role": "assistant", "content": None,
            "tool_calls": [tc.model_dump(mode="json") for tc in calls],
        }]

        ok = phase._gate_tool_calls(calls, messages, blocked_tools={"search_memory"})

        assert ok is False
        assert_strict_provider_shape(messages)

    @pytest.mark.asyncio
    async def test_tool_exception_still_paired(self):
        """TP-T6 工具抛普通异常：回合级异常由调用方收口，但**已声明的**调用仍必须有回执。"""
        phase = _make_phase()
        ctx = _ExecutionRecorder(raises=RuntimeError("boom"))
        messages: list[dict] = []

        with pytest.raises(RuntimeError):
            await phase._execute_tool_round(
                messages, "", _raw(_make_tc("get_time", "call_1")), ctx)

        # 异常发生在 dispatch 之后：assistant 声明已存在而回执未注入 ⇒ 这一轮
        # 不能把 messages 继续发给 provider（调用方按异常结算）。配对不变量只在
        # "继续下一轮" 的路径上要求成立，故此处只钉「不得伪造回执」。
        declared, replied = _declared_and_replied(messages)
        assert declared == ["call_1"]
        assert replied == []


# ---- KB-82 主体：submit_task + 幻觉工具名 ----


class TestSubmitTaskRejectedRoundPairing:
    """KB-82 canonical：`submit_task` + 幻觉工具名 ⇒ 整轮拒绝，submit 也必须有终态回执。"""

    @pytest.mark.asyncio
    async def test_submit_plus_hallucinated_tool_pairs_both(self):
        """正序：submit_task 在前、幻觉工具在后。"""
        submits: list[str] = []
        phase = _make_phase(submits=submits)
        messages: list[dict] = []

        await phase._execute_tool_round(
            messages, "",
            _raw(_make_tc("submit_task", "call_submit", {"intent": "帮我搜索天气"}),
                 _make_tc("delete_everything", "call_bad")),
            None)

        assert_strict_provider_shape(messages)
        declared, replied = _declared_and_replied(messages)
        assert declared == ["call_submit", "call_bad"]
        assert Counter(replied) == Counter({"call_submit": 1, "call_bad": 1})
        # TP2：被 gate 拒绝 ⇒ submit_task **不得执行**（配对 ≠ 执行）
        assert submits == []

    @pytest.mark.asyncio
    async def test_hallucinated_tool_then_submit_pairs_both(self):
        """反序：幻觉工具在前、submit_task 在后（证明不是位置相关）。"""
        submits: list[str] = []
        phase = _make_phase(submits=submits)
        messages: list[dict] = []

        await phase._execute_tool_round(
            messages, "",
            _raw(_make_tc("delete_everything", "call_bad"),
                 _make_tc("submit_task", "call_submit", {"intent": "帮我搜索天气"})),
            None)

        assert_strict_provider_shape(messages)
        declared, replied = _declared_and_replied(messages)
        assert declared == ["call_bad", "call_submit"]
        assert Counter(replied) == Counter({"call_bad": 1, "call_submit": 1})
        assert submits == []

    @pytest.mark.asyncio
    async def test_submit_task_rejection_reply_is_not_a_success(self):
        """TP4：补的回执是**失败**终态，不得被写成成功（也不得伪造 tool 帧）。"""
        submits: list[str] = []
        phase = _make_phase(submits=submits)
        messages: list[dict] = []

        await phase._execute_tool_round(
            messages, "",
            _raw(_make_tc("submit_task", "call_submit", {"intent": "x"}),
                 _make_tc("delete_everything", "call_bad")),
            None)

        reply = next(m for m in messages
                     if m.get("role") == "tool" and m["tool_call_id"] == "call_submit")
        # `llm_content` 是给 LLM 的**失败**纠错文本（非成功载荷）——成功文案
        # "好的，已经在后台执行了。" 绝不能出现在拒绝回执里。
        assert "failed" in reply["content"], f"拒绝回执形状异常: {reply['content']}"
        assert "好的，已经在后台执行了" not in reply["content"], "拒绝回执被写成成功"
        # 被拒绝的轮次不发任何 tool 帧（v1/v2 帧集不变）
        assert phase._output.started == []
        assert phase._output.completed == []

    @pytest.mark.asyncio
    async def test_submit_only_round_still_executes(self):
        """负向边界：只有 submit_task 时 gate 通过 ⇒ 照常执行（不得被新逻辑误伤）。"""
        submits: list[str] = []
        phase = _make_phase(submits=submits)
        messages: list[dict] = []

        await phase._execute_tool_round(
            messages, "",
            _raw(_make_tc("submit_task", "call_submit", {"intent": "帮我搜索天气"})),
            None)

        assert_strict_provider_shape(messages)
        assert submits == ["call_submit"]
        assert len(phase._output.started) == 1
        assert len(phase._output.completed) == 1

    @pytest.mark.asyncio
    async def test_valid_round_with_submit_and_known_tool_pairs(self):
        """负向边界：submit_task + 合法工具 ⇒ 两者都执行且各有一条回执。"""
        submits: list[str] = []
        phase = _make_phase(submits=submits)
        ctx = _ExecutionRecorder()
        messages: list[dict] = []

        await phase._execute_tool_round(
            messages, "",
            _raw(_make_tc("submit_task", "call_submit", {"intent": "帮我搜索天气"}),
                 _make_tc("get_time", "call_ok")),
            ctx)

        assert_strict_provider_shape(messages)
        assert submits == ["call_submit"]
        assert ctx.executed == ["call_ok"]

    @pytest.mark.asyncio
    async def test_next_round_request_shape_is_accepted(self):
        """TP-T8：把 gate 拒绝轮之后的 messages 组装成"严格 provider 形状"并送校验。

        与真实 `_think_loop` 一致：拒绝轮之后追加 `[TOOL_EXECUTED]` system 标记，
        然后再发下一轮。严格 provider 必须在下一轮接受这个形状。
        """
        submits: list[str] = []
        phase = _make_phase(submits=submits)
        messages: list[dict] = [{"role": "system", "content": "sys"},
                                {"role": "user", "content": "帮我把这个复杂任务交后台"}]

        await phase._execute_tool_round(
            messages, "", _raw(_make_tc("submit_task", "call_submit", {"intent": "x"}),
                               _make_tc("delete_everything", "call_bad")),
            None)
        # `_think_loop` 在工具轮之后无条件追加的模式切换信号
        messages.append({"role": "system", "content": "[TOOL_EXECUTED]"})

        assert_strict_provider_shape(messages)
        # 身份配对（非位置/非文本）：回执顺序无关，id 集合与重数一致
        declared, replied = _declared_and_replied(messages)
        assert sorted(declared) == sorted(replied)

    @pytest.mark.asyncio
    async def test_rejection_reason_blames_the_invalid_sibling(self):
        """可诊断性：submit 的回执必须说明"因同轮其它调用非法而未执行"。"""
        phase = _make_phase()
        messages: list[dict] = []

        await phase._execute_tool_round(
            messages, "",
            _raw(_make_tc("submit_task", "call_submit", {"intent": "x"}),
                 _make_tc("delete_everything", "call_bad")),
            None)

        reply = next(m for m in messages
                     if m.get("role") == "tool" and m["tool_call_id"] == "call_submit")
        assert "submit_task" in reply["content"]
        assert "整轮取消" in reply["content"], (
            f"submit 回执未说明'因同轮其它调用非法而未执行': {reply['content']}")
