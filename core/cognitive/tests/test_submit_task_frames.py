"""Task 4（B-6-04）：submit_task 专用路径 tool.started/tool.completed 两帧测试。

克隆 test_tool_dispatch_frames.py 的 mixin 直驱骨架（mock LLM 产出 submit_task tool_call
→ _execute_tool_round 拦截 → _run_submit_tasks），service 层直驱（mixin 无 __init__，
逐属性注入替身）。

覆盖：
- submit_task 成功：tool.started（runtime=backend、v1_suppress=True、conversationId 透传）
  + tool.completed（success、conversationId 透传）两帧；归档（assistant tool_calls + tool
  结果消息 + session ToolResultMessage/AssistantMessage）不变。
- submit_task 失败：仍产出良构 tool.completed（v2 error=dict{code,message}，v1 error 为 str）。
- v1 帧集不变：真实 OutputManager 分流下，v1 通道只收 action{tool_done}、不收 tool_executing；
  v2 通道收 tool.started/tool.completed。
"""

import json
from types import SimpleNamespace

import pytest

from core.cognitive.timing import TimingClock
from core.cognitive.tool_phase import CognitiveToolPhase
from core.schema.message import AssistantMessage, ToolResultMessage
from core.schema.tool import ToolCall, ToolCallFunction
from core.schema.types import ToolCallId
from Module.output import OutputManager
from Module.output.protocol import ChannelCapability, OutputChannel
from Module.tool.protocol import ToolResult
from Module.tool.safety import _safe_arg_summary


class _RecordingOutput:
    """Mock OutputManager：记录 tool_started/tool_completed 调用（submit_task 发射点验证）。"""

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
    """Mock ToolAdapter：definitions 供 gate 校验（submit_task 本身被 gate 跳过）。"""

    def definitions(self) -> list[dict]:
        return []

    def resolve_llm_name(self, llm_name: str) -> str | None:
        return None


class _FrameChannel(OutputChannel):
    """真实 OutputManager 分流验证通道：protocol_version 区分 v1/v2，记录 action/帧。"""

    def __init__(self, protocol_version: int) -> None:
        self.protocol_version = protocol_version
        self.events: list[tuple[str, dict]] = []

    @property
    def capabilities(self) -> set[ChannelCapability]:
        return {ChannelCapability.ACTION}

    async def on_message(self, msg: object) -> None:
        pass

    async def close(self) -> None:
        pass

    async def on_action(self, action: str, *, params: dict | None = None) -> None:
        self.events.append(("action", {"action": action, "params": params}))

    async def on_tool_started(self, *, call_id: str, tool_name: str, runtime: str,
                              input_summary: str | None = None,
                              generation_id: str | None = None,
                              conversation_id: str | None = None) -> None:
        self.events.append(("tool_started", {"call_id": call_id, "tool_name": tool_name,
                                             "runtime": runtime, "input_summary": input_summary,
                                             "generation_id": generation_id,
                                             "conversation_id": conversation_id}))

    async def on_tool_completed(self, *, call_id: str, tool_name: str, runtime: str,
                                success: bool, output_summary: str | None = None,
                                error: dict | None = None, error_type: str | None = None,
                                duration_ms: float | None = None,
                                truncated: bool | None = None,
                                generation_id: str | None = None,
                                conversation_id: str | None = None) -> None:
        self.events.append(("tool_completed", {"call_id": call_id, "tool_name": tool_name,
                                               "runtime": runtime, "success": success,
                                               "output_summary": output_summary,
                                               "conversation_id": conversation_id}))


def _make_tc(name: str, call_id: str = "call_st_1", args: dict | None = None) -> ToolCall:
    return ToolCall(
        id=ToolCallId(call_id),
        function=ToolCallFunction(name=name, arguments=json.dumps(args or {})),
    )


def _make_phase(output: object | None = None,
                generation_id: str | None = None) -> CognitiveToolPhase:
    """构造 CognitiveToolPhase（mixin 无 __init__，逐属性注入替身）。"""
    phase = CognitiveToolPhase()
    phase._output = output if output is not None else _RecordingOutput()
    phase._clock = TimingClock()
    phase._session = _FakeSession()
    phase._tool_adapter = _FakeToolAdapter()
    phase._delegation_preparer = SimpleNamespace()  # 非 None → submit_task 分流启用
    phase._executing_tools = []
    phase._generation_ctx = SimpleNamespace(generation_id=generation_id) if generation_id else None
    phase.phases = _FakePhases()
    phase._on_background_task = None
    phase._task_system = None
    return phase


@pytest.mark.service
class TestSubmitTaskFrames:
    @pytest.mark.asyncio
    async def test_submit_task_emits_started_completed_with_conversation(self):
        """submit_task：tool.started（v1_suppress=True）+ tool.completed 两帧，conversationId 透传。"""
        phase = _make_phase(generation_id="gen_1")

        async def _fake_submit(tc: ToolCall) -> ToolResult:
            assert tc.function.name == "submit_task"
            return ToolResult.ok(name="submit_task", content="好的，已经在后台执行了。",
                                 duration_ms=0.0, async_task_id="task_1")

        phase._handle_submit_task = _fake_submit

        raw_calls = [{"id": "call_st_1", "type": "function",
                      "function": {"name": "submit_task",
                                   "arguments": json.dumps({"intent": "帮我搜索天气"})}}]
        messages: list[dict] = []
        await phase._execute_tool_round(messages, "", raw_calls, None, conversation_id="sess_1")

        out = phase._output
        assert len(out.started) == 1 and len(out.completed) == 1

        st = out.started[0]
        assert st["call_id"] == "call_st_1"
        assert st["tool_name"] == "submit_task"
        assert st["runtime"] == "backend"
        assert st["v1_suppress"] is True           # v1 帧集恒 {tool_done} 的关键
        assert st["v1_args"] == {}                  # submit_task 无旧 action{tool_executing}
        assert st["input_summary"] == _safe_arg_summary({"intent": "帮我搜索天气"})
        assert st["generation_id"] == "gen_1"
        assert st["conversation_id"] == "sess_1"    # 调用点透传（B-6-04 关键）

        cp = out.completed[0]
        assert cp["call_id"] == "call_st_1"
        assert cp["tool_name"] == "submit_task"
        assert cp["runtime"] == "backend"
        assert cp["success"] is True
        assert cp["v1_content"] == "好的，已经在后台执行了。"
        assert cp["output_summary"] == "好的，已经在后台执行了。"
        assert cp["error_v1"] is None and cp["error_v2"] is None and cp["error_type"] is None
        assert cp["duration_ms"] == 0.0
        assert cp["generation_id"] == "gen_1"
        assert cp["conversation_id"] == "sess_1"

        # 归档不变：assistant tool_calls + tool 结果消息
        assert [m["role"] for m in messages] == ["assistant", "tool"]
        assert messages[0]["tool_calls"][0]["id"] == "call_st_1"
        assert messages[1]["tool_call_id"] == "call_st_1"
        # session 落 ToolResultMessage + AssistantMessage（顺序不变）
        assert len(phase._session.added) == 2
        assert isinstance(phase._session.added[0], ToolResultMessage)
        assert isinstance(phase._session.added[1], AssistantMessage)
        tr_msg = phase._session.added[0]
        assert tr_msg.tool_name == "submit_task" and tr_msg.success is True
        assert phase.phases.entered == 1 and phase.phases.returned == 1

    @pytest.mark.asyncio
    async def test_submit_task_failed_emits_wellformed_completed(self):
        """submit_task 失败：仍产出良构 tool.completed（v2 error=dict{code,message}、v1 error 为 str）。"""
        phase = _make_phase()

        async def _fake_submit(tc: ToolCall) -> ToolResult:
            return ToolResult.fail(name="submit_task", error="intent 不能为空，请修正参数后重试。")

        phase._handle_submit_task = _fake_submit

        tc = _make_tc("submit_task", "call_st_2", {})
        await phase._run_submit_tasks([tc], [])

        cp = phase._output.completed[0]
        assert cp["success"] is False
        assert cp["error_v1"] == "intent 不能为空，请修正参数后重试。"          # v1 error 为 str
        assert cp["error_v2"] == {"code": "TOOL_ERROR",
                                  "message": "intent 不能为空，请修正参数后重试。"}  # v2 error 为 dict
        assert cp["error_type"] is None
        assert cp["v1_content"] == ""
        assert cp["output_summary"] == ""
        # 失败路径仍发 tool.started（v1_suppress）
        assert len(phase._output.started) == 1
        assert phase._output.started[0]["v1_suppress"] is True

    @pytest.mark.asyncio
    async def test_submit_task_v1_channel_only_tool_done(self):
        """v1 帧集不变：真实 OutputManager 分流下 v1 只收 action{tool_done}，v2 收两帧。"""
        mgr = OutputManager()
        v1_ch = _FrameChannel(1)
        v2_ch = _FrameChannel(2)
        mgr.add_channel(v1_ch)
        mgr.add_channel(v2_ch)
        phase = _make_phase(output=mgr, generation_id="gen_1")

        async def _fake_submit(tc: ToolCall) -> ToolResult:
            return ToolResult.ok(name="submit_task", content="好的，已经在后台执行了。",
                                 duration_ms=0.0, async_task_id="task_1")

        phase._handle_submit_task = _fake_submit

        tc = _make_tc("submit_task", "call_st_3", {"intent": "帮我搜索天气"})
        await phase._run_submit_tasks([tc], [], conversation_id="sess_1")

        # v1 通道：只收 tool_done，不收 tool_executing（v1 帧集恒 {tool_done}）
        assert [ev[0] for ev in v1_ch.events] == ["action"]
        assert [ev[1]["action"] for ev in v1_ch.events] == ["tool_done"]
        # v2 通道：tool.started + tool.completed（runtime=backend、conversationId 透传）
        assert [ev[0] for ev in v2_ch.events] == ["tool_started", "tool_completed"]
        st_ev, cp_ev = v2_ch.events
        assert st_ev[1]["runtime"] == "backend"
        assert st_ev[1]["conversation_id"] == "sess_1"
        assert cp_ev[1]["runtime"] == "backend"
        assert cp_ev[1]["conversation_id"] == "sess_1"
        assert cp_ev[1]["success"] is True
