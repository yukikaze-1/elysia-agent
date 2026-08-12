"""ToolResult.images → ImagePart 落盘 + desktop cancel 贯通（批次 4 Task 9，marker=service）。

覆盖 loop.py 两个改动点：
- `_execute_tool_round`：`ToolResult.images` 非空 → `ToolResultMessage.content` 组装为
  `[TextPart(llm_summary or content), *[ImagePart ...]]`（截图进聊天，回放/搜索自动吃到）。
- `ImagePart.mime` 兜底：`MediaItem.mime_type` 空串 / 非图片（如 `application/octet-stream`）
  → **过滤不落 ImagePart**，不抛 ValidationError 拖垮整轮工具结果。
- 中断/取消回合（`_handle_interrupt`）→ 对在途 desktop 调用 `bridge.cancel_call`（interrupt
  语义）；无桥 / 无在途调用 → no-op；offline 调用不取消（保重连重传结果闭环，D-o）。

使用最小 CognitiveLoop + mock ToolResult / fake bridge，不碰真实 Mongo/LLM/桌面客户端。
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.status.agent_fsm import AgentFSM
from core.monitoring.event_bus import EventBus
from Module.output import OutputManager
from core.memory.session import SessionMemory
from core.cognitive import CognitiveLoop
from core.cognitive.timing import TimingClock
from core.adapter.llm_adapter import LLMAdapter
from core.schema.part import ImageMime, ImagePart, TextPart
from core.schema.message import ToolResultMessage
from Module.tool.protocol import MediaItem, ToolResult, to_llm_name

pytestmark = pytest.mark.service


class _FakeAdapter:
    """最小 ToolAdapter 替身：gate 校验已知工具 + tool_context.services 供桥注入。

    _names 存运行时工具名；definitions() 暴露脱点名、resolve_llm_name() 反查
    （镜像真实 ToolManager 行为）。
    """

    def __init__(self, tool_names: list[str] | None = None,
                 bridge: object | None = None):
        self._names = tool_names or ["desktop.screen.capture"]
        self._ctx = MagicMock()
        self._ctx.services = {"desktop_bridge": bridge} if bridge is not None else {}

    def definitions(self) -> list[dict]:
        return [{"function": {"name": to_llm_name(n)}} for n in self._names]

    def resolve_llm_name(self, llm_name: str) -> str | None:
        for n in self._names:
            if to_llm_name(n) == llm_name:
                return n
        return None

    @property
    def tool_context(self):
        return self._ctx


def _make_loop(adapter: _FakeAdapter | None = None) -> CognitiveLoop:
    """构造最小 CognitiveLoop（供 _execute_tool_round / _handle_interrupt 直接调用）。"""
    llm = MagicMock(spec=LLMAdapter)
    llm.model = "mock"
    output = OutputManager()
    fsm = AgentFSM()
    bus = EventBus()
    prompt = MagicMock()
    prompt._character = MagicMock(name="TestBot")
    prompt._character.traits = []

    async def resolver(conversation_id, protocol_version):
        return SessionMemory(session_id="default")

    loop = CognitiveLoop(
        llm=llm, fsm=fsm, event_bus=bus, output=output,
        prompt=prompt, session_resolver=resolver,
        tool_adapter=adapter,
    )
    return loop


def _raw_tool_call(call_id: str = "call_1", name: str = "desktop.screen.capture",
                   args: str = "{}") -> list[dict]:
    return [{"id": call_id, "type": "function",
             "function": {"name": name, "arguments": args}}]


def _result_from_session(loop: CognitiveLoop, call_id: str) -> ToolResultMessage:
    """从 session 取指定 tool_call 的 ToolResultMessage。"""
    for msg in loop._session.messages:
        if isinstance(msg, ToolResultMessage) and str(msg.tool_call_id) == call_id:
            return msg
    raise AssertionError(f"session 中无 ToolResultMessage(tool_call_id={call_id})")


# ---- ImagePart 落盘 ----

class TestImagePartLanding:
    @pytest.mark.asyncio
    async def test_images_become_image_parts(self):
        """images 非空 → content = [TextPart(summary), ImagePart(src/mime)]。"""
        adapter = _FakeAdapter()
        loop = _make_loop(adapter)
        loop._session = SessionMemory(session_id="default")
        loop._clock = TimingClock()
        await loop.phases.enter_thinking()  # _execute_tool_round 要求 FSM 在 THINKING

        async def fake_execute(tcs):
            return [(tc, ToolResult.ok(
                name="desktop.screen.capture", content="截图", duration_ms=0,
                llm_summary="已截取用户屏幕",
                images=[MediaItem(url="/uploads/desktop/x.png", mime_type="image/png")],
            )) for tc in tcs]

        await loop._execute_tool_round(
            [], "", _raw_tool_call(), fake_execute)

        result = _result_from_session(loop, "call_1")
        assert [p.type for p in result.content] == ["text", "image"]
        assert isinstance(result.content[0], TextPart)
        assert result.content[0].content == "已截取用户屏幕"
        img = result.content[1]
        assert isinstance(img, ImagePart)
        assert img.type == "image"
        assert img.src == "/uploads/desktop/x.png"
        assert img.mime == ImageMime.PNG
        await loop._output.close()

    @pytest.mark.asyncio
    async def test_text_part_falls_back_to_content(self):
        """llm_summary 为空 → TextPart 用 content（text 结果 wire 不变）。"""
        adapter = _FakeAdapter()
        loop = _make_loop(adapter)
        loop._session = SessionMemory(session_id="default")
        loop._clock = TimingClock()
        await loop.phases.enter_thinking()  # _execute_tool_round 要求 FSM 在 THINKING

        async def fake_execute(tcs):
            return [(tc, ToolResult.ok(
                name="desktop.screen.capture", content="截图原始输出", duration_ms=0,
                images=[MediaItem(url="/uploads/desktop/x.png", mime_type="image/png")],
            )) for tc in tcs]

        await loop._execute_tool_round([], "", _raw_tool_call(), fake_execute)

        result = _result_from_session(loop, "call_1")
        assert isinstance(result.content[0], TextPart)
        assert result.content[0].content == "截图原始输出"
        await loop._output.close()

    @pytest.mark.asyncio
    async def test_text_result_wire_unchanged(self):
        """无 images → content 与既有行为一致：仅 [TextPart(content)]。"""
        adapter = _FakeAdapter(tool_names=["desktop.screen.capture", "get_time"])
        loop = _make_loop(adapter)
        loop._session = SessionMemory(session_id="default")
        loop._clock = TimingClock()
        await loop.phases.enter_thinking()  # _execute_tool_round 要求 FSM 在 THINKING

        async def fake_execute(tcs):
            return [(tc, ToolResult.ok(name="get_time", content="12:00", duration_ms=0)) for tc in tcs]

        await loop._execute_tool_round(
            [], "", _raw_tool_call(call_id="c_text", name="get_time"), fake_execute)

        result = _result_from_session(loop, "c_text")
        assert len(result.content) == 1
        assert isinstance(result.content[0], TextPart)
        assert result.content[0].content == "12:00"
        await loop._output.close()


# ---- LLM 脱点名反查（BE：dot 名 OpenAI 非法） ----

class TestLLMNameReverseMapping:
    @pytest.mark.asyncio
    async def test_sanitized_llm_name_reversed_before_dispatch(self):
        """LLM 发出脱点名 desktop_screen_capture → _execute_tool_round 反查回运行时名
        desktop.screen.capture 再 dispatch（execute/gate/session 全部对得上）。"""
        adapter = _FakeAdapter(["desktop.screen.capture"])
        loop = _make_loop(adapter)
        loop._session = SessionMemory(session_id="default")
        loop._clock = TimingClock()
        await loop.phases.enter_thinking()

        dispatched: list[str] = []

        async def fake_execute(tcs):
            dispatched.extend(tc.function.name for tc in tcs)
            return [(tc, ToolResult.ok(
                name="desktop.screen.capture", content="截图", duration_ms=0)) for tc in tcs]

        await loop._execute_tool_round(
            [], "", _raw_tool_call(call_id="c_llm", name="desktop_screen_capture"),
            fake_execute)

        assert dispatched == ["desktop.screen.capture"]
        await loop._output.close()

class TestMimeFallback:
    @pytest.mark.asyncio
    async def test_empty_mime_filtered_no_validation_error(self):
        """mime_type=""（MediaItem 默认）→ 过滤不抛 ValidationError，仅文本摘要。"""
        adapter = _FakeAdapter()
        loop = _make_loop(adapter)
        loop._session = SessionMemory(session_id="default")
        loop._clock = TimingClock()
        await loop.phases.enter_thinking()  # _execute_tool_round 要求 FSM 在 THINKING

        async def fake_execute(tcs):
            return [(tc, ToolResult.ok(
                name="desktop.file.read", content="文件内容", duration_ms=0,
                llm_summary="已读取文件",
                images=[MediaItem(url="/uploads/desktop/f", mime_type="")],
            )) for tc in tcs]

        await loop._execute_tool_round([], "", _raw_tool_call(), fake_execute)

        result = _result_from_session(loop, "call_1")
        assert [p.type for p in result.content] == ["text"]
        assert result.content[0].content == "已读取文件"
        await loop._output.close()

    @pytest.mark.asyncio
    async def test_octet_stream_mime_filtered(self):
        """application/octet-stream（Task 8 附件白名单非图片）→ 过滤不落 ImagePart。"""
        adapter = _FakeAdapter()
        loop = _make_loop(adapter)
        loop._session = SessionMemory(session_id="default")
        loop._clock = TimingClock()
        await loop.phases.enter_thinking()  # _execute_tool_round 要求 FSM 在 THINKING

        async def fake_execute(tcs):
            return [(tc, ToolResult.ok(
                name="desktop.file.read", content="二进制附件", duration_ms=0,
                llm_summary="已读取附件",
                images=[MediaItem(url="/uploads/desktop/f.bin",
                                  mime_type="application/octet-stream")],
            )) for tc in tcs]

        await loop._execute_tool_round([], "", _raw_tool_call(), fake_execute)

        result = _result_from_session(loop, "call_1")
        assert [p.type for p in result.content] == ["text"]
        assert result.content[0].content == "已读取附件"
        await loop._output.close()

    @pytest.mark.asyncio
    async def test_mixed_mime_keeps_only_images(self):
        """混合 mime（png + octet-stream）→ 只落 png ImagePart，非图片过滤。"""
        adapter = _FakeAdapter()
        loop = _make_loop(adapter)
        loop._session = SessionMemory(session_id="default")
        loop._clock = TimingClock()
        await loop.phases.enter_thinking()  # _execute_tool_round 要求 FSM 在 THINKING

        async def fake_execute(tcs):
            return [(tc, ToolResult.ok(
                name="desktop.screen.capture", content="截图", duration_ms=0,
                llm_summary="已截取",
                images=[
                    MediaItem(url="/uploads/desktop/a.png", mime_type="image/png"),
                    MediaItem(url="/uploads/desktop/b.bin",
                              mime_type="application/octet-stream"),
                ],
            )) for tc in tcs]

        await loop._execute_tool_round([], "", _raw_tool_call(), fake_execute)

        result = _result_from_session(loop, "call_1")
        assert [p.type for p in result.content] == ["text", "image"]
        img = result.content[1]
        assert isinstance(img, ImagePart)
        assert img.src == "/uploads/desktop/a.png"
        await loop._output.close()


# ---- desktop cancel 贯通 ----

class TestDesktopCancel:
    async def _interrupt_loop(self, bridge: object) -> CognitiveLoop:
        """构造已注入 fake bridge 的 loop，置非 idle 状态后触发 _handle_interrupt。"""
        adapter = _FakeAdapter(bridge=bridge)
        loop = _make_loop(adapter)
        loop._session = SessionMemory(session_id="default")
        # 非 idle 状态 + mock phases，绕开 FSM 真实迁移（本测试只验证 cancel 挂点）
        loop._status = MagicMock(is_completely_idle=False)
        loop._phases = AsyncMock()
        return loop

    @pytest.mark.asyncio
    async def test_interrupt_cancels_pending_desktop_calls(self):
        """取消回合 → 对 pending_calls() 枚举的在途调用逐一 cancel_call。"""
        bridge = AsyncMock()
        bridge.pending_calls = AsyncMock(return_value=[
            {"call_id": "call_desktop_1", "status": "pending"},
            {"call_id": "call_desktop_2", "status": "pending"},
        ])
        bridge.cancel_call = AsyncMock()
        loop = await self._interrupt_loop(bridge)

        await loop._handle_interrupt()

        assert bridge.cancel_call.await_count == 2
        called_ids = sorted(c.args[0] for c in bridge.cancel_call.await_args_list)
        assert called_ids == ["call_desktop_1", "call_desktop_2"]
        await loop._output.close()

    @pytest.mark.asyncio
    async def test_interrupt_skips_offline_calls(self):
        """offline 调用不取消（D-o：断线标 offline，保重连重传结果闭环）。"""
        bridge = AsyncMock()
        bridge.pending_calls = AsyncMock(return_value=[
            {"call_id": "call_pending", "status": "pending"},
            {"call_id": "call_offline", "status": "offline"},
        ])
        bridge.cancel_call = AsyncMock()
        loop = await self._interrupt_loop(bridge)

        await loop._handle_interrupt()

        assert bridge.cancel_call.await_count == 1
        assert bridge.cancel_call.await_args.args[0] == "call_pending"
        await loop._output.close()

    @pytest.mark.asyncio
    async def test_interrupt_no_bridge_noop(self):
        """无 desktop 桥（backend-only）→ no-op 不抛异常。"""
        loop = await self._interrupt_loop(None)
        await loop._handle_interrupt()  # 不抛即通过
        await loop._output.close()

    @pytest.mark.asyncio
    async def test_interrupt_no_pending_calls_noop(self):
        """无在途调用 → cancel_call 不被调用。"""
        bridge = AsyncMock()
        bridge.pending_calls = AsyncMock(return_value=[])
        bridge.cancel_call = AsyncMock()
        loop = await self._interrupt_loop(bridge)

        await loop._handle_interrupt()

        assert bridge.cancel_call.await_count == 0
        await loop._output.close()

    @pytest.mark.asyncio
    async def test_interrupt_bridge_error_swallowed(self):
        """桥抛异常（断连竞态）→ 中断清理不被打断。"""
        bridge = AsyncMock()
        bridge.pending_calls = AsyncMock(
            return_value=[{"call_id": "call_x", "status": "pending"}])
        bridge.cancel_call = AsyncMock(side_effect=RuntimeError("bridge down"))
        loop = await self._interrupt_loop(bridge)

        await loop._handle_interrupt()  # cancel 失败被吞，不抛
        await loop._output.close()
