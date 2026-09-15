"""批次 7 Task 11：关联 ID——WS 错误带 requestId + GenerationRegistry 记 request_id（marker=service）。

覆盖：
- ws_handler._send_voice_error：有 request_id（当前 ContextVar）→ error 信封携带 requestId；
  无（"-"）→ requestId 缺省省略（None）。
- ws_handler._handle_activate_session：缺 conversationId 的错误信封同样携带 requestId。
- voice_sessions._emit_error（经 feed_pcm 录音过长公开路径）：有 request_id → 携带 requestId。
- GenerationRegistry.register：doc 含 request_id 字段（有则落、无则省略）。
"""

import asyncio
from typing import get_type_hints

import pytest

from core.cognitive.generation_registry import GenerationRegistry
from core.input.voice_sessions import (
    MAX_RECORDING_BYTES,
    MAX_CHUNK_BYTES,
    VoiceSessionError,
    VoiceSessionManager,
)
from core.schema import GenerationId, MessageId, RequestId, SessionId
from core.storage.generation_repo import GenerationRepository
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
from core.utils import set_request_id

pytestmark = pytest.mark.service


class _FakeWS:
    """捕获 send_json 载荷的假 WS（模拟 aiohttp WebSocketResponse.send_json）。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


class _FakeVoiceAgent:
    """voice_sessions._emit_error 测试的最小 Agent 桩（stt 可用）。"""

    def __init__(self) -> None:
        self.stt = object()
        self.stt_enabled = True
        self.status = type("Status", (), {
            "is_speaking": False, "is_completely_idle": True,
            "should_suppress_vad_interrupt": False})()
        self._rid = 0

    def next_request_id(self):
        self._rid += 1
        return f"req_{self._rid}"


def _make_voice_manager():
    """构造 VoiceSessionManager（emit 捕获到列表），返回 (mgr, emitted)。"""
    emitted: list[dict] = []

    async def emit(payload: dict) -> None:
        emitted.append(payload)

    mgr = VoiceSessionManager(agent=_FakeVoiceAgent(), emit=emit)
    return mgr, emitted


def _registry() -> GenerationRegistry:
    return GenerationRegistry(repo=GenerationRepository(db=FakeDocumentDBBackend()))


class TestWsErrorMessageRequestId:
    @pytest.mark.asyncio
    async def test_send_voice_error_carries_request_id(self):
        """有 request_id（当前 ContextVar）→ _send_voice_error 的 error 信封带 requestId。"""
        from core.input.ws_handler import _send_voice_error

        ws = _FakeWS()
        set_request_id("req_voice_1")
        await _send_voice_error(
            ws, VoiceSessionError("VOICE_SESSION_CONFLICT", "已有活动语音会话"))
        assert ws.sent and ws.sent[0]["type"] == "error"
        assert ws.sent[0]["requestId"] == "req_voice_1"

    @pytest.mark.asyncio
    async def test_send_voice_error_without_request_id_omits(self):
        """无 request_id（ContextVar 为 "-"）→ requestId 缺省省略（None）。"""
        from core.input.ws_handler import _send_voice_error

        ws = _FakeWS()
        set_request_id("-")
        await _send_voice_error(
            ws, VoiceSessionError("VOICE_SESSION_CONFLICT", "已有活动语音会话"))
        assert ws.sent and ws.sent[0]["type"] == "error"
        assert ws.sent[0].get("requestId") is None

    @pytest.mark.asyncio
    async def test_activate_session_missing_conv_id_carries_request_id(self):
        """activate_session 缺 conversationId 的错误信封携带 requestId。"""
        from core.input.ws_handler import _handle_activate_session

        ws = _FakeWS()
        set_request_id("req_act_1")
        await _handle_activate_session(None, ws, {})
        assert ws.sent and ws.sent[0]["type"] == "error"
        assert ws.sent[0]["code"] == "CONTRACT_VALIDATION"
        assert ws.sent[0]["requestId"] == "req_act_1"


class TestVoiceSessionsEmitErrorRequestId:
    @pytest.mark.asyncio
    async def test_emit_error_carries_request_id(self):
        """voice_sessions._emit_error（录音过长公开路径）带 requestId。"""
        mgr, emitted = _make_voice_manager()
        set_request_id("req_voice_2")
        await mgr.start_recording("rec_rid")
        chunk = b"\x00" * MAX_CHUNK_BYTES       # ≤1 MiB，不触发 chunk 上限（9C-1）
        for _ in range((MAX_RECORDING_BYTES // MAX_CHUNK_BYTES) + 2):
            await mgr.feed_pcm(chunk)
        err = next(e for e in emitted if e.get("type") == "error")
        assert err["code"] == "VOICE_RECORDING_TOO_LONG"
        assert err["requestId"] == "req_voice_2"

    @pytest.mark.asyncio
    async def test_emit_error_without_request_id_omits(self):
        """无 request_id → voice_sessions error 信封缺省省略 requestId。"""
        mgr, emitted = _make_voice_manager()
        set_request_id("-")
        await mgr.start_recording("rec_rid2")
        chunk = b"\x00" * MAX_CHUNK_BYTES       # ≤1 MiB，不触发 chunk 上限（9C-1）
        for _ in range((MAX_RECORDING_BYTES // MAX_CHUNK_BYTES) + 2):
            await mgr.feed_pcm(chunk)
        err = next(e for e in emitted if e.get("type") == "error")
        assert err.get("requestId") is None


class TestGenerationRegistryRequestId:
    def test_register_records_request_id(self):
        """register 传入 request_id → doc 含 request_id 字段。"""
        reg = _registry()
        asyncio.run(reg.register(
            GenerationId("gen_1"), session_id=SessionId("s1"),
            user_message_id=MessageId("u1"), message_id=MessageId("a1"),
            model="mock", request_id=RequestId("req_1")))
        doc = asyncio.run(reg.get(GenerationId("gen_1")))
        assert doc is not None
        assert doc["request_id"] == "req_1"

    def test_register_without_request_id_omits(self):
        """register 不传 request_id → doc 无 request_id 字段。"""
        reg = _registry()
        asyncio.run(reg.register(
            GenerationId("gen_1"), session_id=SessionId("s1"),
            user_message_id=MessageId("u1"), message_id=MessageId("a1"), model="mock"))
        doc = asyncio.run(reg.get(GenerationId("gen_1")))
        assert doc is not None
        assert doc.get("request_id") is None


@pytest.mark.unit
def test_next_request_id_returns_request_id():
    """next_request_id 返回 RequestId（NewType）。"""
    from core.agent.server import AgentServer
    hints = get_type_hints(AgentServer.next_request_id)
    assert hints["return"] is RequestId


class _LoopWS:
    """驱动 `_run_ws_message_loop` 的最小假 WS（异步可迭代 + 捕获 send_json）。

    每次 yield 之前都**故意**把 ContextVar 设成一个陈旧 id——模拟"上一条命令留下了
    关联 ID"，这正是 S3-06 的触发条件。
    """

    def __init__(self, frames: list[str]) -> None:
        self._frames = frames
        self.sent: list[dict] = []

    def __aiter__(self):
        async def _gen():
            from aiohttp import WSMsgType

            class _Msg:
                def __init__(self, data: str) -> None:
                    self.type = WSMsgType.TEXT
                    self.data = data
            for raw in self._frames:
                set_request_id("stale_from_previous_command")
                yield _Msg(raw)
        return _gen()

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


class TestRequestIdIsPerFrame:
    """S3-06：关联 ID 必须**按帧**重置，不能沿用到下一条无关请求的错误帧上。"""

    @pytest.mark.asyncio
    async def test_stale_request_id_does_not_leak_into_next_frame_error(self):
        from types import SimpleNamespace

        from core.input.ws_handler import _run_ws_message_loop

        ws = _LoopWS(["{ not json", "{ also bad"])
        conn = SimpleNamespace(ws=ws, audio_buf=None, voice_mgr=None,
                               sent_message_ids=set())

        await _run_ws_message_loop(conn, None, None)

        errs = [p for p in ws.sent if p.get("type") == "error"]
        assert len(errs) == 2, ws.sent
        assert all(p.get("requestId") is None for p in errs), (
            f"上一帧的关联 ID 泄漏进了本帧错误：{[p.get('requestId') for p in errs]}")
