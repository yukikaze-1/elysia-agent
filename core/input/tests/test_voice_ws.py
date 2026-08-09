"""ws_handler 语音消息路由（marker=service，fake agent + 记录 send_json）。"""
import asyncio

import pytest

from core.input.idempotency import reset_store
from core.input.ws_handler import handle_json_message

pytestmark = pytest.mark.service


class FakeWs:
    def __init__(self):
        self.sent = []
        self.closed = False
    async def send_json(self, data):
        self.sent.append(data)


class FakeOutput:
    def __init__(self, ws):
        # 必须用真实 WebSocketChannel——_find_channel 按 isinstance(ch, WebSocketChannel) 匹配
        from Module.output import WebSocketChannel
        self._channels = [WebSocketChannel(ws, protocol_version=2)]


class _FakeSessionManager:
    """session_manager stub：known 会话 set + 简易 resolve/ensure_loaded（9a3 has_session 校验）。"""

    def __init__(self, known: set | None = None):
        self._known = set(known or {"conv_a", "default"})

    async def has_session(self, sid) -> bool:
        return str(sid) in self._known

    async def resolve(self, conversation_id, protocol_version):
        return type("S", (), {"session_id": conversation_id or "default"})()

    async def ensure_loaded(self, sid):
        return type("S", (), {"session_id": str(sid)})()


class FakeAgent:
    def __init__(self, ws):
        self.output = FakeOutput(ws)
        self.bus_posts = []
        self.status = type("Status", (), {
            "is_speaking": False, "is_completely_idle": True,
            "should_suppress_vad_interrupt": False})()
        self.stt = object()
        self.stt_enabled = True
        self.user_name = "用户"
        self._rid = 0
        self.idle = type("Idle", (), {"touch": lambda self: None})()
        self._last_text = ""
        self.session_manager = _FakeSessionManager()

    def next_request_id(self):
        self._rid += 1
        return f"req_{self._rid}"

    async def stt_transcribe(self, wav):
        return self._last_text

    async def resolve_session(self, conversation_id, protocol_version):
        return type("S", (), {"session_id": conversation_id or "default"})()


def make():
    ws = FakeWs()
    agent = FakeAgent(ws)
    agent.bus = type("Bus", (), {"post": lambda self, d: agent.bus_posts.append(d)})()
    from core.input.voice_sessions import VoiceSessionManager
    mgr = VoiceSessionManager(agent=agent, emit=ws.send_json,
                              on_vad_interrupt=lambda: None)
    reset_store()  # 幂等表单例隔离
    return agent, ws, mgr


@pytest.mark.asyncio
async def test_v1_ignores_voice_messages():
    agent, ws, mgr = make()
    agent.output._channels[0].protocol_version = 1
    await handle_json_message({"type": "voice.record.start", "recordingId": "rec_1"},
                              agent, None, ws, voice_mgr=mgr)
    assert ws.sent == []  # v1 忽略，不发错误


@pytest.mark.asyncio
async def test_recording_stop_produces_transcript():
    """start→stop→transcript（commit 依赖测试在 Task 4，避免 Task 3 依赖未实现函数，review NEW-2）。"""
    agent, ws, mgr = make()
    await handle_json_message({"type": "voice.record.start", "recordingId": "rec_1"},
                              agent, None, ws, voice_mgr=mgr)
    await mgr.feed_pcm(b"\x00" * 6400)  # F3：≥ 一帧 PCM，否则 stop 丢弃不转写
    agent._last_text = "你好"
    await handle_json_message({"type": "voice.record.stop", "recordingId": "rec_1"},
                              agent, None, ws, voice_mgr=mgr)
    await asyncio.sleep(0.05)  # 后台 STT 同一 loop
    assert any(s.get("type") == "voice.transcript" for s in ws.sent)
    assert not any(getattr(p, "event_type", "") == "user_input" for p in agent.bus_posts)  # 未提交


@pytest.mark.asyncio
async def test_empty_transcript_ws_no_transcript():
    """空转写 → 不发 transcript（spec §4.2，前端收不到即视为未识别）。"""
    agent, ws, mgr = make()
    await handle_json_message({"type": "voice.record.start", "recordingId": "rec_1"},
                              agent, None, ws, voice_mgr=mgr)
    await mgr.feed_pcm(b"\x00" * 6400)  # F3：有 PCM 才走 STT 空转写路径（否则被最小长度守卫丢弃）
    agent._last_text = "  "
    await handle_json_message({"type": "voice.record.stop", "recordingId": "rec_1"},
                              agent, None, ws, voice_mgr=mgr)
    await asyncio.sleep(0.05)
    assert not any(s.get("type") == "voice.transcript" for s in ws.sent)


@pytest.mark.asyncio
async def test_stt_disabled_via_set_service_ws():
    """set_service('stt', False) 成功后 → realtime ended{stt_disabled}（spec §5.3 NEW-D）。"""
    agent, ws, mgr = make()
    await handle_json_message(
        {"type": "voice_session.start", "sessionId": "vs_1", "conversationId": None},
        agent, None, ws, voice_mgr=mgr)

    async def set_service(service, enabled):
        agent.stt_enabled = enabled
        return {"ok": True, "enabled": enabled}
    agent.set_service = set_service

    await handle_json_message({"type": "set_service", "service": "stt", "enabled": False},
                              agent, None, ws, voice_mgr=mgr)
    ends = [s for s in ws.sent if s.get("type") == "voice_session.ended"]
    assert ends and ends[-1]["reason"] == "stt_disabled"
    assert ends[-1]["sessionId"] == "vs_1"


@pytest.mark.asyncio
async def test_paused_session_stt_disabled_via_json_entry():
    """暂停中会话：跨连接关 STT（agent.stt_enabled=False 但本连接无 set_service 通知）→
    任意会话 JSON 消息入口检测并 ended{stt_disabled}（spec §5.3 NEW-W/S2）。"""
    agent, ws, mgr = make()
    await handle_json_message(
        {"type": "voice_session.start", "sessionId": "vs_1", "conversationId": None},
        agent, None, ws, voice_mgr=mgr)
    await handle_json_message(
        {"type": "voice_session.pause", "sessionId": "vs_1"}, agent, None, ws, voice_mgr=mgr)
    agent.stt_enabled = False  # 模拟另一连接关 STT（本连接无 set_service 通知）
    await handle_json_message(
        {"type": "voice_session.resume", "sessionId": "vs_1"}, agent, None, ws, voice_mgr=mgr)
    ends = [s for s in ws.sent if s.get("type") == "voice_session.ended"]
    assert ends and ends[-1]["reason"] == "stt_disabled"
    # 会话已结束 → 后续 stop 报 VOICE_SESSION_NOT_FOUND
    await handle_json_message(
        {"type": "voice_session.stop", "sessionId": "vs_1"}, agent, None, ws, voice_mgr=mgr)
    errs = [s for s in ws.sent if s.get("type") == "error"]
    assert errs and errs[-1].get("code") == "VOICE_SESSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_recording_full_flow():
    """start→stop→transcript→commit→ack+generation（完整流，依赖 _handle_voice_commit）。"""
    agent, ws, mgr = make()
    await handle_json_message({"type": "voice.record.start", "recordingId": "rec_1"},
                              agent, None, ws, voice_mgr=mgr)
    await mgr.feed_pcm(b"\x00" * 6400)  # F3：≥ 一帧 PCM
    agent._last_text = "你好"
    await handle_json_message({"type": "voice.record.stop", "recordingId": "rec_1"},
                              agent, None, ws, voice_mgr=mgr)
    await asyncio.sleep(0.05)
    assert any(s.get("type") == "voice.transcript" for s in ws.sent)
    await handle_json_message(
        {"type": "voice.commit", "recordingId": "rec_1", "text": "你好",
         "clientRequestId": "req_c1", "clientMessageId": "msg_c1", "conversationId": None},
        agent, None, ws, voice_mgr=mgr)
    acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
    assert acks and acks[-1]["status"] == "accepted" and acks[-1]["messageId"]
    assert any(getattr(p, "event_type", "") == "user_input" for p in agent.bus_posts)


@pytest.mark.asyncio
async def test_voice_commit_idempotent_replay():
    agent, ws, mgr = make()
    await handle_json_message({"type": "voice.record.start", "recordingId": "rec_1"},
                              agent, None, ws, voice_mgr=mgr)
    await mgr.feed_pcm(b"\x00" * 6400)  # F3：≥ 一帧 PCM
    agent._last_text = "你好"
    await handle_json_message({"type": "voice.record.stop", "recordingId": "rec_1"},
                              agent, None, ws, voice_mgr=mgr)
    await asyncio.sleep(0.05)
    commit = {"type": "voice.commit", "recordingId": "rec_1", "text": "你好",
              "clientRequestId": "req_same", "conversationId": None}
    await handle_json_message(dict(commit), agent, None, ws, voice_mgr=mgr)
    n_acks_1 = len([s for s in ws.sent if s.get("type") == "user_message.ack"])
    n_input_1 = len(agent.bus_posts)
    # 重试同 ID（状态已 IDLE，幂等表命中 → 重发 ACK，不重复生成）
    await handle_json_message(dict(commit), agent, None, ws, voice_mgr=mgr)
    n_acks_2 = len([s for s in ws.sent if s.get("type") == "user_message.ack"])
    assert n_acks_2 == n_acks_1 + 1
    assert len(agent.bus_posts) == n_input_1


@pytest.mark.asyncio
async def test_voice_commit_missing_client_request_id():
    agent, ws, mgr = make()
    await handle_json_message({"type": "voice.record.start", "recordingId": "rec_1"},
                              agent, None, ws, voice_mgr=mgr)
    await mgr.feed_pcm(b"\x00" * 6400)  # F3：≥ 一帧 PCM
    agent._last_text = "你好"
    await handle_json_message({"type": "voice.record.stop", "recordingId": "rec_1"},
                              agent, None, ws, voice_mgr=mgr)
    await asyncio.sleep(0.05)
    await handle_json_message(
        {"type": "voice.commit", "recordingId": "rec_1", "text": "你好",
         "clientRequestId": "", "conversationId": None},
        agent, None, ws, voice_mgr=mgr)
    acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
    assert acks and acks[-1]["status"] == "rejected" \
        and acks[-1]["error"]["code"] == "CONTRACT_VALIDATION"


@pytest.mark.asyncio
async def test_voice_commit_state_gate_before_submit():
    # IDLE 态（无录音）commit → ack{rejected, VOICE_RECORDING_NOT_FOUND}
    agent, ws, mgr = make()
    await handle_json_message(
        {"type": "voice.commit", "recordingId": "rec_x", "text": "x",
         "clientRequestId": "req_c2", "conversationId": None},
        agent, None, ws, voice_mgr=mgr)
    acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
    assert acks and acks[-1]["status"] == "rejected" \
        and acks[-1]["error"]["code"] == "VOICE_RECORDING_NOT_FOUND"


@pytest.mark.asyncio
async def test_voice_commit_unknown_conversation_rejected():
    """9a3（Q9）：voice.commit + 未知会话 → user_message.ack rejected + SESSION_NOT_FOUND。

    ACK 通道（非 ErrorMessage）：不提交、不产生 messageId、零 bus post。
    """
    agent, ws, mgr = make()
    await handle_json_message(
        {"type": "voice.commit", "recordingId": "rec_1", "text": "hi",
         "clientRequestId": "req_ghost", "clientMessageId": "msg_ghost",
         "conversationId": "conv_ghost"},
        agent, None, ws, voice_mgr=mgr)
    acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
    assert acks and acks[-1]["status"] == "rejected", f"实际 {ws.sent}"
    assert acks[-1]["error"]["code"] == "SESSION_NOT_FOUND", f"实际 {acks[-1].get('error')}"
    assert not acks[-1].get("messageId"), "未知会话拒绝不应生成 messageId"
    assert not any(s.get("type") == "error" for s in ws.sent), "voice.commit 走 ACK 通道，非 ErrorMessage"
    assert not agent.bus_posts, "未知会话不应提交 UserInputData"


@pytest.mark.asyncio
async def test_realtime_start_unknown_conversation_rejected():
    """9a3（Q4）：voice_session.start + 未知会话 → ErrorMessage SESSION_NOT_FOUND。

    不建 audio buffer、不切 REALTIME 态（禁止懒创建幽灵会话）。
    """
    agent, ws, mgr = make()
    await handle_json_message(
        {"type": "voice_session.start", "sessionId": "vs_ghost",
         "conversationId": "conv_ghost"},
        agent, None, ws, voice_mgr=mgr)
    errs = [s for s in ws.sent if s.get("type") == "error"]
    assert errs and errs[-1].get("code") == "SESSION_NOT_FOUND", f"实际 {ws.sent}"
    assert not any(s.get("type") == "voice_session.started" for s in ws.sent)
    assert mgr.has_active_session() is False, "未知会话不应进入 REALTIME 态"


@pytest.mark.asyncio
async def test_realtime_session_lifecycle():
    """实时语音会话生命周期：start→started、pause/resume 幂等、stop→ended{stopped}（BE-061）。"""
    agent, ws, mgr = make()
    await handle_json_message(
        {"type": "voice_session.start", "sessionId": "vs_1", "conversationId": "conv_a"},
        agent, None, ws, voice_mgr=mgr)
    assert any(s.get("type") == "voice_session.started" for s in ws.sent)
    await handle_json_message(
        {"type": "voice_session.pause", "sessionId": "vs_1"}, agent, None, ws, voice_mgr=mgr)
    await handle_json_message(
        {"type": "voice_session.resume", "sessionId": "vs_1"}, agent, None, ws, voice_mgr=mgr)
    await handle_json_message(
        {"type": "voice_session.stop", "sessionId": "vs_1"}, agent, None, ws, voice_mgr=mgr)
    ends = [s for s in ws.sent if s.get("type") == "voice_session.ended"]
    assert ends and ends[-1]["reason"] == "stopped"


@pytest.mark.asyncio
async def test_realtime_cancel_unknown_session():
    agent, ws, mgr = make()
    await handle_json_message(
        {"type": "voice_session.cancel", "sessionId": "vs_999"}, agent, None, ws, voice_mgr=mgr)
    errs = [s for s in ws.sent if s.get("type") == "error"]
    assert errs and errs[-1].get("code") == "VOICE_SESSION_NOT_FOUND"


def test_server_info_features_true_after_batch6():
    """批次 6 落地后 features 如实声明三种语音能力（spec §9.3，严禁超前）。"""
    from core.input.capabilities import build_server_info

    class Cfg:
        live2d = type("L", (), {"static_image": "/img.png"})()
        llm = type("L", (), {"model": "lm"})()
        stt = type("S", (), {"model": "sm"})()
        tts = type("T", (), {"model": "tm"})()

    class A:
        config = Cfg()
        stt = type("S", (), {"model": "sm"})()
        stt_enabled = True
        tts = type("T", (), {"model": "tm"})()
        tts_enabled = True
        llm = type("L", (), {"model": "lm"})()

    info = build_server_info(A())
    assert info.features.dictation is True
    assert info.features.voice_recording is True
    assert info.features.voice_realtime is True


@pytest.mark.asyncio
async def test_audio_cancel_routes_to_tts_manager():
    """audio.cancel{stream_id} v2 路由 → tts_manager.cancel_stream（BE-063）。"""
    agent, ws, mgr = make()
    cancelled = []
    agent.tts_manager = type("TtsMgr", (), {
        "cancel_stream": lambda self, sid: cancelled.append(sid) or True})()
    await handle_json_message({"type": "audio.cancel", "stream_id": 7},
                              agent, None, ws, voice_mgr=mgr)
    assert cancelled == [7]


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, d):
        self.sent.append(d)


@pytest.mark.asyncio
async def test_send_voice_error_carries_retryable():
    """VoiceSessionError.retryable → _send_voice_error → wire 帧带 retryable。"""
    from core.input.ws_handler import _send_voice_error
    from core.input.voice_sessions import VoiceSessionError
    ws = _FakeWS()
    err = VoiceSessionError("STT_UNAVAILABLE", "转写失败", retryable=True)
    await _send_voice_error(ws, err)
    assert ws.sent[0]["code"] == "STT_UNAVAILABLE"
    assert ws.sent[0]["retryable"] is True


# ── 9C-1 输入音频契约（Task 7）：start 帧 descriptor 校验 ─────────────────


@pytest.mark.asyncio
async def test_recording_start_bad_descriptor_rejected():
    """voice.record.start 带 audio={codec:opus} → error CONTRACT_VALIDATION，不建会话。"""
    agent, ws, mgr = make()
    await handle_json_message(
        {"type": "voice.record.start", "recordingId": "rec_1",
         "audio": {"codec": "opus", "sampleRate": 16000, "channels": 1}},
        agent, None, ws, voice_mgr=mgr)
    errs = [s for s in ws.sent if s.get("type") == "error"]
    assert errs and errs[-1].get("code") == "CONTRACT_VALIDATION"
    assert mgr.has_active_session() is False


@pytest.mark.asyncio
async def test_realtime_bad_descriptor_rejected():
    """voice_session.start 48k/stereo → error CONTRACT_VALIDATION，不建会话。"""
    agent, ws, mgr = make()
    await handle_json_message(
        {"type": "voice_session.start", "sessionId": "vs_1", "conversationId": None,
         "audio": {"codec": "pcm_s16le", "sampleRate": 48000, "channels": 2}},
        agent, None, ws, voice_mgr=mgr)
    errs = [s for s in ws.sent if s.get("type") == "error"]
    assert errs and errs[-1].get("code") == "CONTRACT_VALIDATION"
    assert mgr.has_active_session() is False


@pytest.mark.asyncio
async def test_dictation_start_bad_descriptor_rejected():
    """dictation.start 带非法 descriptor → error CONTRACT_VALIDATION，不建会话。"""
    agent, ws, mgr = make()
    await handle_json_message(
        {"type": "dictation.start", "clientRequestId": "req_d",
         "audio": {"codec": "pcm_s16le", "sampleRate": 8000, "channels": 1}},
        agent, None, ws, voice_mgr=mgr)
    errs = [s for s in ws.sent if s.get("type") == "error"]
    assert errs and errs[-1].get("code") == "CONTRACT_VALIDATION"
    assert mgr.has_active_session() is False


@pytest.mark.asyncio
async def test_dictation_start_passes_stt_profile():
    """9C-2：dictation.start 帧 sttProfileId 透传到 start_dictation（快照 profile）。"""
    agent, ws, mgr = make()
    seen = {}

    async def fake_start(client_request_id, stt_profile_id=None, audio_descriptor=None):
        seen["profile"] = stt_profile_id
    mgr.start_dictation = fake_start
    await handle_json_message(
        {"type": "dictation.start", "clientRequestId": "req_d",
         "sttProfileId": "sensevoice/sense-voice"},
        agent, None, ws, voice_mgr=mgr)
    assert seen.get("profile") == "sensevoice/sense-voice"


@pytest.mark.asyncio
async def test_valid_descriptor_accepts_start():
    """合法 descriptor（pcm_s16le/16000/1）→ start 正常，无错误。"""
    agent, ws, mgr = make()
    await handle_json_message(
        {"type": "voice_session.start", "sessionId": "vs_1", "conversationId": "conv_a",
         "audio": {"codec": "pcm_s16le", "sampleRate": 16000, "channels": 1}},
        agent, None, ws, voice_mgr=mgr)
    assert any(s.get("type") == "voice_session.started" for s in ws.sent)
    assert not any(s.get("type") == "error" for s in ws.sent)
    assert mgr.has_active_session() is True
    await handle_json_message(
        {"type": "voice_session.stop", "sessionId": "vs_1"}, agent, None, ws, voice_mgr=mgr)


@pytest.mark.asyncio
async def test_chunk_too_large_rejected():
    """BINARY > MAX_CHUNK_BYTES 经 feed_pcm → CONTRACT_VALIDATION + 会话终止。"""
    from core.input.voice_sessions import MAX_CHUNK_BYTES
    agent, ws, mgr = make()
    await handle_json_message(
        {"type": "voice_session.start", "sessionId": "vs_1", "conversationId": "conv_a"},
        agent, None, ws, voice_mgr=mgr)
    await mgr.feed_pcm(b"\x00" * (MAX_CHUNK_BYTES + 1))
    errs = [s for s in ws.sent if s.get("type") == "error"]
    assert errs and errs[-1].get("code") == "CONTRACT_VALIDATION"
    assert mgr.has_active_session() is False
    assert any(s.get("type") == "voice_session.ended" for s in ws.sent)


@pytest.mark.asyncio
async def test_descriptor_omitted_default_format():
    """缺省 audio → 默认 16k/mono 正常（向后兼容）。"""
    agent, ws, mgr = make()
    await handle_json_message(
        {"type": "voice_session.start", "sessionId": "vs_1", "conversationId": "conv_a"},
        agent, None, ws, voice_mgr=mgr)
    assert any(s.get("type") == "voice_session.started" for s in ws.sent)
    assert mgr.has_active_session() is True
    await handle_json_message(
        {"type": "voice_session.stop", "sessionId": "vs_1"}, agent, None, ws, voice_mgr=mgr)
