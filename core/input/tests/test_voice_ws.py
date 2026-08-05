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
async def test_recording_full_flow():
    """start→stop→transcript→commit→ack+generation（完整流，依赖 _handle_voice_commit）。"""
    agent, ws, mgr = make()
    await handle_json_message({"type": "voice.record.start", "recordingId": "rec_1"},
                              agent, None, ws, voice_mgr=mgr)
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
