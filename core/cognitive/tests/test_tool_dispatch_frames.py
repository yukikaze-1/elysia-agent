"""Task 3（B-6-03）：_dispatch_tool_calls 发射点 v2 tool.started/tool.completed 帧测试。

克隆 test_v2_generation.py / test_tool_gate.py 的真实工具轮构造作骨架（mock LLM
产出 tool_call → gate → dispatch → 结果注入），service 层直驱 _dispatch_tool_calls /
_execute_tool_round（mixin 无 __init__，逐属性注入替身）。

覆盖：
- backend 工具成功：tool.started → tool.completed 两帧，字段正确（runtime/inputSummary/
  outputSummary/conversationId/generationId 透传，v1_args/v1_content 与旧 action 逐字节一致）。
- 失败工具：仍产出良构 tool.completed（v2 error=dict{code,message}，v1 error 为 str）。
- gate 拒绝：不产生 tool.started/tool.completed。
- _truncate_output_summary 边界（≤2000 原样 / 超长截断 / None）。
- runtime_for：desktop.*→desktop；未知/submit_task→backend；越界 runtime 兜底 backend。
"""

import json
from types import SimpleNamespace

import pytest

from core.adapter.tool_adapter import ToolAdapter
from core.cognitive.timing import TimingClock
from core.cognitive.tool_phase import CognitiveToolPhase, _truncate_output_summary
from core.schema.message import AssistantMessage
from core.schema.tool import ToolCall, ToolCallFunction
from core.schema.types import ToolCallId
from Module.tool.protocol import ErrorType, ToolResult, to_llm_name
from Module.tool.safety import _safe_arg_summary


class _RecordingOutput:
    """Mock OutputManager：记录 tool_started/tool_completed 调用（Task 3 发射点验证）。"""

    def __init__(self) -> None:
        self.started: list[dict] = []
        self.completed: list[dict] = []

    async def tool_started(self, **kwargs: object) -> None:
        self.started.append(kwargs)

    async def tool_completed(self, **kwargs: object) -> None:
        self.completed.append(kwargs)


class _FakeSession:
    """Mock SessionMemory：session_id + add_message 记录。"""

    def __init__(self) -> None:
        self.session_id = "default"
        self.added: list[object] = []

    def add_message(self, msg: object) -> None:
        self.added.append(msg)


class _FakePhases:
    """Mock FSMBackedPhaseController：记录 enter_executing/return_to_thinking。"""

    def __init__(self) -> None:
        self.entered = 0
        self.returned = 0

    async def enter_executing(self) -> None:
        self.entered += 1

    async def return_to_thinking(self) -> None:
        self.returned += 1


class _FakeToolAdapter:
    """Mock ToolAdapter：runtime_for + resolve_llm_name + definitions（gate 校验用）。"""

    def __init__(self, runtimes: dict[str, str] | None = None) -> None:
        self._runtimes = runtimes or {}

    def runtime_for(self, tool_name: str) -> str:
        return self._runtimes.get(tool_name, "backend")

    def resolve_llm_name(self, llm_name: str) -> str | None:
        for name in self._runtimes:
            if to_llm_name(name) == llm_name:
                return name
        return None

    def definitions(self) -> list[dict]:
        return [{"type": "function", "function": {"name": to_llm_name(n)}}
                for n in self._runtimes]


def _make_tc(name: str, call_id: str = "call_1", args: dict | None = None) -> ToolCall:
    return ToolCall(
        id=ToolCallId(call_id),
        function=ToolCallFunction(name=name, arguments=json.dumps(args or {})),
    )


def _make_phase(runtimes: dict[str, str] | None = None,
                generation_id: str | None = None,
                character_snapshot: object = None) -> CognitiveToolPhase:
    """构造 CognitiveToolPhase 实例（mixin 无 __init__，逐属性注入测试替身）。"""
    phase = CognitiveToolPhase()
    phase._output = _RecordingOutput()
    phase._clock = TimingClock()
    phase._session = _FakeSession()
    phase._tool_adapter = _FakeToolAdapter(runtimes)
    phase._delegation_preparer = None
    phase._executing_tools = []
    phase._generation_ctx = SimpleNamespace(generation_id=generation_id) if generation_id else None
    phase.phases = _FakePhases()
    phase._on_background_task = None
    phase._task_system = None
    # 跨 mixin 协作方：运行时由 CognitiveLoop（CognitiveFinalize）提供——
    # 独立实例化本 mixin 的测试必须同样注入，否则工具轮归档路径不可用。
    phase._turn_character_snapshot = lambda: character_snapshot  # type: ignore[method-assign]
    return phase


@pytest.mark.service
class TestToolDispatchFrames:
    @pytest.mark.asyncio
    async def test_backend_tool_emits_started_completed(self):
        """backend 工具：tool.started → tool.completed 两帧，字段正确。"""
        phase = _make_phase(runtimes={"get_time": "backend"}, generation_id="gen_1")
        tc = _make_tc("get_time", "call_1", {"tz": "UTC"})

        async def _run(dispatch_tcs: list[ToolCall]):
            assert [t.id for t in dispatch_tcs] == ["call_1"]
            return [(dispatch_tcs[0], ToolResult.ok(
                name="get_time", content="2026-08-19 12:00:00", duration_ms=1.5))]

        messages: list[dict] = []
        await phase._dispatch_tool_calls([tc], _run, messages, conversation_id="sess_1")

        out = phase._output
        assert len(out.started) == 1 and len(out.completed) == 1
        st = out.started[0]
        assert st["call_id"] == "call_1"
        assert st["tool_name"] == "get_time"
        assert st["runtime"] == "backend"
        # v1_args 与旧 action{tool_executing}.args 逐字节一致（{k: str(v)}）
        assert st["v1_args"] == {"tz": "UTC"}
        assert st["input_summary"] == _safe_arg_summary({"tz": "UTC"})
        assert st["input_summary"] == "tz=<str:3>"
        assert st["generation_id"] == "gen_1"
        assert st["conversation_id"] == "sess_1"

        cp = out.completed[0]
        assert cp["call_id"] == "call_1"
        assert cp["tool_name"] == "get_time"
        assert cp["runtime"] == "backend"
        assert cp["success"] is True
        # v1_content 与旧 action{tool_done}.content 逐字节一致（content or ""）
        assert cp["v1_content"] == "2026-08-19 12:00:00"
        assert cp["output_summary"] == "2026-08-19 12:00:00"
        assert cp["v1_truncated"] is False and cp["output_truncated"] is False
        assert cp["error_v1"] is None and cp["error_v2"] is None and cp["error_type"] is None
        assert cp["duration_ms"] == 1.5
        assert cp["generation_id"] == "gen_1"
        assert cp["conversation_id"] == "sess_1"

        # 结果注入 + session 归档 + executing 追踪清理
        assert len(messages) == 1
        assert messages[0]["role"] == "tool" and messages[0]["tool_call_id"] == "call_1"
        assert len(phase._session.added) == 1
        assert phase._executing_tools == []

    @pytest.mark.asyncio
    async def test_failed_tool_emits_wellformed_completed(self):
        """失败工具：仍产出良构 tool.completed（v2 error=dict{code,message}、v1 error 为 str）。"""
        phase = _make_phase(runtimes={"get_time": "backend"})
        tc = _make_tc("get_time", "call_2", {})

        async def _run(dispatch_tcs: list[ToolCall]):
            return [(dispatch_tcs[0], ToolResult.fail(
                name="get_time", error="boom", error_type=ErrorType.TOOL_ERROR, duration_ms=0.5))]

        await phase._dispatch_tool_calls([tc], _run, [])

        cp = phase._output.completed[0]
        assert cp["success"] is False
        assert cp["error_v1"] == "boom"                              # v1 error 为 str
        assert cp["error_v2"] == {"code": "tool_error", "message": "boom"}  # v2 error 为 dict
        assert cp["error_type"] == "tool_error"
        assert cp["v1_content"] == ""
        assert cp["output_summary"] == ""

    @pytest.mark.asyncio
    async def test_gate_rejected_tool_no_frames(self):
        """gate 拒绝：不产生 tool.started/tool.completed（_dispatch_tool_calls 不被调用）。"""
        phase = _make_phase(runtimes={"get_time": "backend"})  # 只注册 get_time
        raw_calls = [{"id": "call_3", "type": "function",
                      "function": {"name": "delete_everything", "arguments": "{}"}}]
        await phase._execute_tool_round([], "", raw_calls, None)

        out = phase._output
        assert not out.started
        assert not out.completed
        assert phase._executing_tools == []
        assert phase.phases.entered == 1 and phase.phases.returned == 1

    def test_truncate_output_summary_boundaries(self):
        """outputSummary 截断：≤max_len 原样；超长截断 + truncated=True；None → (None, False)。"""
        short = "x" * 100
        assert _truncate_output_summary(short) == (short, False)
        assert _truncate_output_summary(None) == (None, False)
        assert _truncate_output_summary("z" * 2000) == ("z" * 2000, False)
        summary, truncated = _truncate_output_summary("y" * 3000)
        assert truncated is True
        assert len(summary) == 2000  # content[:1999] + "…"
        assert summary.endswith("…")

    def test_runtime_for_desktop_and_unknown(self):
        """runtime_for：desktop.*→desktop；未知/submit_task→backend；越界 runtime 兜底 backend。"""
        adapter = ToolAdapter([])
        adapter._manager = SimpleNamespace(
            runtime_of=lambda name: {
                "desktop.file.read": "desktop",
                "get_time": "backend",
                "weird": "neon",  # 越界 runtime → 兜底 backend（防 ToolRuntime 构造 ValidationError）
            }.get(name),
        )
        assert adapter.runtime_for("desktop.file.read") == "desktop"
        assert adapter.runtime_for("get_time") == "backend"
        assert adapter.runtime_for("submit_task") == "backend"    # 未注册（合成工具）
        assert adapter.runtime_for("not_registered") == "backend"  # 未知幻觉名
        assert adapter.runtime_for("weird") == "backend"           # 越界兜底


@pytest.mark.service
class TestToolRoundCharacterSnapshot:
    """工具轮归档的 assistant 消息必须携带角色身份快照。

    回归（P3 剩余第 1 项）：本 mixin 归档 AssistantMessage 时漏传 `character=`，
    而 finalize 的同一构造有 `character=self._turn_character_snapshot()`。工具轮在
    真实对话中极常见（LLM 只发 tool_calls 不附文字时 content 还是空列表），于是
    每条工具轮消息都以 `character=null` 落 message_archive —— 正是问题单
    「14 条 assistant 缺角色戳」的源头（character_profiles=true 时前端对消息历史
    整批 fail-closed）。
    """

    @staticmethod
    def _snapshot():
        from core.schema.message import CharacterMessageSnapshot

        return CharacterMessageSnapshot(
            character_id="chr_builtin_elysia",
            character_instance_id="chi_builtin_elysia",
            character_revision=1,
            display_name="Elysia",
        )

    @pytest.mark.asyncio
    async def test_tool_round_message_carries_character(self):
        """工具轮归档的 AssistantMessage 带 character 快照（非 None）。"""
        phase = _make_phase(
            runtimes={"get_time": "backend"}, generation_id="gen_1",
            character_snapshot=self._snapshot(),
        )
        tc = _make_tc("get_time", "call_1")

        async def _run(dispatch_tcs: list[ToolCall]):
            return [(dispatch_tcs[0], ToolResult.ok(name="get_time", content="ok", duration_ms=1.0))]

        await phase._execute_tool_round([], "调用工具", [tc.model_dump(mode="json")], _run)

        assistant_msgs = [m for m in phase._session.added
                          if isinstance(m, AssistantMessage)]
        assert len(assistant_msgs) == 1, "工具轮应归档恰好一条 assistant 消息"
        assert assistant_msgs[0].character is not None, "工具轮消息缺 character 快照"
        assert assistant_msgs[0].character.character_id == "chr_builtin_elysia"

    @pytest.mark.asyncio
    async def test_tool_round_empty_text_still_carries_character(self):
        """LLM 只发 tool_calls 不附文字（content 为空）时，仍必须带 character。

        这是真实对话里最常见的形态——空正文本身是合法的（工具调用记录靠 tool_calls
        承载），但缺角色戳会让整页历史被前端拒绝。
        """
        phase = _make_phase(
            runtimes={"get_time": "backend"}, generation_id="gen_1",
            character_snapshot=self._snapshot(),
        )
        tc = _make_tc("get_time", "call_1")

        async def _run(dispatch_tcs: list[ToolCall]):
            return [(dispatch_tcs[0], ToolResult.ok(name="get_time", content="ok", duration_ms=1.0))]

        await phase._execute_tool_round([], "", [tc.model_dump(mode="json")], _run)

        assistant_msgs = [m for m in phase._session.added
                          if isinstance(m, AssistantMessage)]
        assert assistant_msgs[0].content == [], "前提：空正文（LLM 未附文字）"
        assert assistant_msgs[0].character is not None, "空正文工具轮消息缺 character 快照"

    @pytest.mark.asyncio
    async def test_snapshot_absent_does_not_break_tool_round(self):
        """快照不可得（legacy/无 runtime）时返回 None，但不得阻断工具轮执行。"""
        phase = _make_phase(runtimes={"get_time": "backend"}, character_snapshot=None)
        tc = _make_tc("get_time", "call_1")

        async def _run(dispatch_tcs: list[ToolCall]):
            return [(dispatch_tcs[0], ToolResult.ok(name="get_time", content="ok", duration_ms=1.0))]

        await phase._execute_tool_round([], "", [tc.model_dump(mode="json")], _run)

        assistant_msgs = [m for m in phase._session.added
                          if isinstance(m, AssistantMessage)]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0].character is None  # 优柔降级，不得抛
