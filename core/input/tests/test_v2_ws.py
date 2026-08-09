"""批次 2 Task 7：ws_handler v2 文本路由 + ACK + 幂等（marker=service）。

覆盖 spec §4/§10 测试 3：
- v2 文本 → user_message.ack（messageId/clientRequestId）
- 重复 clientRequestId 同内容 → 重发原 ACK 不重复生成
- 不同内容 → rejected + error
- 缺 clientRequestId → CONTRACT_VALIDATION
"""

import asyncio
import json
import struct

import pytest
from aiohttp import WSMsgType

from core.cognitive.tests.test_v2_generation import _UsageLLM, _make_agent_with
from core.input.idempotency import reset_store


@pytest.fixture
def fresh_idempotency():
    """每个测试独立幂等表。"""
    reset_store()
    yield


async def _drain(client, headers):
    """建连并排空建连帧，返回 ws。"""
    ws = await client.session.ws_connect(client.make_url("/ws"), headers=headers)
    for _ in range(5):
        try:
            m = await asyncio.wait_for(ws.receive(), timeout=5)
            if m.type == WSMsgType.TEXT:
                t = json.loads(m.data).get("type")
                if t in ("capabilities", "server_info"):
                    break
        except asyncio.TimeoutError:
            break
    return ws


async def _send_and_read_ack(ws, content: str, client_request_id: str) -> dict:
    await ws.send_json({
        "type": "text", "content": content,
        "clientRequestId": client_request_id,
    })
    for _ in range(8):
        m = await asyncio.wait_for(ws.receive(), timeout=5)
        if m.type == WSMsgType.TEXT:
            d = json.loads(m.data)
            if d.get("type") == "user_message.ack":
                return d
    raise AssertionError("未收到 user_message.ack")


@pytest.mark.service
class TestV2AckIdempotency:
    @pytest.mark.asyncio
    async def test_v2_text_gets_ack(self, aiohttp_client, fresh_idempotency):
        """v2 文本 → ACK（messageId/clientRequestId/accepted）。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            ack = await _send_and_read_ack(ws, "你好", "req_1")
            assert ack["status"] == "accepted"
            assert ack["messageId"].startswith("msg_")
            assert ack["clientRequestId"] == "req_1"
            assert ack["receivedAt"]
            # 批次 3：ACK conversationId = 实际目标会话（v2 缺省 → 当前活动会话，default）
            assert ack.get("conversationId") == "default"
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_duplicate_same_content_replays_ack_no_regeneration(self, aiohttp_client, fresh_idempotency):
        """重复 clientRequestId 同内容 → 重发原 ACK（同 messageId），不重复生成。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({"type": "text", "content": "同一内容", "clientRequestId": "req_same"})
            ack1 = None
            gen_started = 0
            for _ in range(30):
                m = await asyncio.wait_for(ws.receive(), timeout=10)
                if m.type != WSMsgType.TEXT:
                    continue
                d = json.loads(m.data)
                if d.get("type") == "user_message.ack":
                    if ack1 is None:
                        ack1 = d
                        # 第一次已接受 → 第二次同 ID 同内容应重发原 ACK
                        await ws.send_json({"type": "text", "content": "同一内容", "clientRequestId": "req_same"})
                elif d.get("type") == "generation.started":
                    gen_started += 1
                if d.get("type") == "state" and d.get("state") == "idle":
                    break
            assert ack1 is not None and ack1["status"] == "accepted"
            # 只生成一次（重复请求不触发第二次 generation）
            assert gen_started == 1, f"重复请求不应重复生成，实际 generation.started={gen_started}"
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_duplicate_different_content_rejected(self, aiohttp_client, fresh_idempotency):
        """重复 clientRequestId 不同内容 → rejected + CONTRACT_VALIDATION。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await _send_and_read_ack(ws, "内容A", "req_conflict")
            ack2 = await _send_and_read_ack(ws, "内容B", "req_conflict")
            assert ack2["status"] == "rejected"
            assert ack2["error"]["code"] == "CONTRACT_VALIDATION"
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_missing_client_request_id_rejected(self, aiohttp_client, fresh_idempotency):
        """缺 clientRequestId → rejected + CONTRACT_VALIDATION。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({"type": "text", "content": "没有 requestId"})
            for _ in range(8):
                m = await asyncio.wait_for(ws.receive(), timeout=5)
                if m.type == WSMsgType.TEXT:
                    d = json.loads(m.data)
                    if d.get("type") == "user_message.ack":
                        assert d["status"] == "rejected"
                        assert d["error"]["code"] == "CONTRACT_VALIDATION"
                        break
            else:
                raise AssertionError("未收到 rejected ACK")
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_v1_text_no_ack(self, aiohttp_client, fresh_idempotency):
        """v1 文本 → 无 ACK（v1 路径不变）。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, None)
            await ws.send_json({"type": "text", "content": "v1 消息"})
            got_ack = False
            for _ in range(10):
                m = await asyncio.wait_for(ws.receive(), timeout=5)
                if m.type == WSMsgType.TEXT:
                    if json.loads(m.data).get("type") == "user_message.ack":
                        got_ack = True
                        break
            assert not got_ack, "v1 客户端不应收到 ACK"
            await ws.close()
        finally:
            await agent.stop()


class _FakeTtsBackend:
    """假 TTS 后端：list_voices 返回固定列表（set_tts_voice 校验用）。"""

    def __init__(self, voices, provider_name="minimax"):
        self._voice_list = voices
        self.provider_name = provider_name

    async def list_voices(self):
        return self._voice_list


async def _send_set_tts_voice_and_read(ws, voice: str) -> dict:
    """发 set_tts_voice 并读回 set_tts_voice_result 帧。"""
    await ws.send_json({"type": "set_tts_voice", "voice": voice})
    for _ in range(8):
        m = await asyncio.wait_for(ws.receive(), timeout=5)
        if m.type == WSMsgType.TEXT:
            d = json.loads(m.data)
            if d.get("type") == "set_tts_voice_result":
                return d
    raise AssertionError("未收到 set_tts_voice_result")


@pytest.mark.service
class TestSetTtsVoice:
    @pytest.mark.asyncio
    async def test_set_tts_voice_success(self, aiohttp_client, fresh_idempotency):
        """set_tts_voice 已知音色 → result ok + 活动会话 tts_voice 写入。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        agent._tts = _FakeTtsBackend([{"id": "v1", "name": "音色一"}])  # noqa: SLF001 — 测试注入后端
        agent._tts_enabled = True  # noqa: SLF001 — 测试注入有效开关
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            result = await _send_set_tts_voice_and_read(ws, "v1")
            assert result["ok"] is True
            assert result["voice"] == "v1"
            # 活动会话 service_state.tts_voice 已写入
            assert agent.session.service_state.tts_voice == "v1"
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_set_tts_voice_unknown_voice_keeps_state(self, aiohttp_client, fresh_idempotency):
        """未知音色 → ok=False + error，活动会话 tts_voice 不变。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        agent._tts = _FakeTtsBackend([{"id": "v1"}])  # noqa: SLF001
        agent._tts_enabled = True  # noqa: SLF001
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            result = await _send_set_tts_voice_and_read(ws, "nope")
            assert result["ok"] is False
            assert result["error"]
            # 失败不改 state（保持 None）
            assert agent.session.service_state.tts_voice is None
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_set_tts_voice_disabled(self, aiohttp_client, fresh_idempotency):
        """TTS 未启用 → ok=False + error，不改 state。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        agent._tts = None  # noqa: SLF001
        agent._tts_enabled = False  # noqa: SLF001
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            result = await _send_set_tts_voice_and_read(ws, "v1")
            assert result["ok"] is False
            assert result["error"]
            assert agent.session.service_state.tts_voice is None
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_set_tts_voice_empty_clears(self, aiohttp_client, fresh_idempotency):
        """review 修正：空串 voice = 清除覆盖（回退 character 默认音色）——tts_voice 只进不出的死路径修复。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        agent._tts = _FakeTtsBackend([{"id": "v1"}])  # noqa: SLF001
        agent._tts_enabled = True  # noqa: SLF001
        agent.session.service_state.tts_voice = "v1"  # 先有覆盖
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            result = await _send_set_tts_voice_and_read(ws, "")
            assert result["ok"] is True
            # wire 省略 None（exclude_none）→ voice 键缺省
            assert result.get("voice") is None
            # 覆盖已清除 → 后端默认
            assert agent.session.service_state.tts_voice is None
            await ws.close()
        finally:
            await agent.stop()


@pytest.mark.service
class TestV2Reconnect:
    @pytest.mark.asyncio
    async def test_reconnect_replay_stable_message_ids(self, aiohttp_client, fresh_idempotency):
        """重连后 session 回放含稳定 messageId（预分配用户 ID + assistant 父 ID）。"""
        import time as _time
        from core.input.server import create_app
        from core.cognitive.tests.test_v2_generation import _UsageLLM, _make_agent_with

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            # 第一次连接：生成完成，记录 ACK messageId + completed messageId
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({
                "type": "text", "content": "你好",
                "clientRequestId": f"req_r_{_time.monotonic_ns()}",
            })
            ack_msg_id = completed_msg_id = None
            for _ in range(30):
                m = await asyncio.wait_for(ws.receive(), timeout=10)
                if m.type != WSMsgType.TEXT:
                    continue
                d = json.loads(m.data)
                if d.get("type") == "user_message.ack":
                    ack_msg_id = d.get("messageId")
                elif d.get("type") == "generation.completed":
                    completed_msg_id = d.get("messageId")
                    break
            assert ack_msg_id and completed_msg_id
            await ws.close()

            # 重连：直接读首帧 session 回放（_drain 会消费它）
            ws2 = await client.session.ws_connect(
                client.make_url("/ws"), headers={"X-Protocol-Version": "2"})
            session = None
            for _ in range(6):
                m = await asyncio.wait_for(ws2.receive(), timeout=5)
                if m.type == WSMsgType.TEXT:
                    d = json.loads(m.data)
                    if d.get("type") == "session":
                        session = d
                        break
            assert session is not None, "重连未收到 session 回放"
            ids = [item.get("message_id") for item in session.get("messages", [])]
            assert ack_msg_id in ids, "回放应含预分配用户消息 ID"
            assert completed_msg_id in ids, "回放应含 assistant 父消息 ID"
            await ws2.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_v2_disconnect_cancels_inflight_generation(self, aiohttp_client, fresh_idempotency):
        """v2 源连接生成中断开 → 取消在途 generation（agent 回 idle）。"""
        import time as _time
        from core.input.server import create_app
        from core.cognitive.tests.test_v2_generation import _SlowLLM, _make_agent_with

        agent = _make_agent_with(_SlowLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({
                "type": "text", "content": "慢速生成",
                "clientRequestId": f"req_d_{_time.monotonic_ns()}",
            })
            await asyncio.sleep(0.4)  # 生成开始
            await ws.close()          # 生成中断开
            # 等待取消 → agent 回 idle
            for _ in range(30):
                if agent.status.is_completely_idle:
                    break
                await asyncio.sleep(0.1)
            assert agent.status.is_completely_idle, "v2 断线应取消在途 generation"
            # 批次 3（Task 13）：断线 → registry 落终态 interrupted（disconnect 来源）
            reg = agent.cognitive._generation_registry  # noqa: SLF001
            docs, _, _ = await reg.query_by_session("default")
            assert docs and docs[-1]["status"] == "interrupted"
            assert docs[-1]["finished_at"]
        finally:
            await agent.stop()


@pytest.mark.service
class TestV2BinaryNoLegacy:
    """D2：v2 连接 BINARY 只在 voice_mgr 会话内有效——无会话 → 丢弃，不落 legacy。

    对照实验：v1 对同样响度音频走 legacy 路径触发 STT（证明 harness 能驱动 legacy）；
    v2 实验组同音频零 STT 调用（D2 门生效）。全零 PCM 不触发 WebRTC VAD（静音），
    必须用响度波形才能让旧代码真正红。
    """

    class _CountingSTT:
        provider_name = "fake"
        model = "fake/model"
        def __init__(self, calls):
            self._calls = calls
        async def transcribe(self, audio: bytes, **kwargs):
            self._calls.append(len(audio))
            return "你好"
        async def close(self):
            pass

    @staticmethod
    def _loud(samples=1600):   # 0.1s @16kHz
        return struct.pack(f"<{samples}h", *([20000] * samples))

    @staticmethod
    def _sil(samples=1600):
        return struct.pack(f"<{samples}h", *([5] * samples))

    async def _send_speech_and_wait(self, ws):
        """喂 ~3s 响度语音 + 持续静音，等待 legacy VAD 分段触发（silence_duration=1.5s）。"""
        for _ in range(30):
            await ws.send_bytes(self._loud())
            await asyncio.sleep(0.02)      # 30 × 0.1s ≈ 3s 语音
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 3.0
        while loop.time() < deadline:
            await ws.send_bytes(self._sil())
            await asyncio.sleep(0.2)       # 静音累积 → 段完成 → STT

    def _make_agent(self):
        from core.cognitive.tests.test_v2_generation import _make_agent_with, _UsageLLM
        calls: list[int] = []
        agent = _make_agent_with(_UsageLLM())
        agent._stt = self._CountingSTT(calls)   # noqa: SLF001 — 测试注入只读槽位
        return agent, calls

    @staticmethod
    def _enable_stt(agent) -> None:
        # 必须在 start() 之后注入：_apply_service_state 启动时按 service_state/config
        # 重写 _stt_enabled（config.stt.enabled=False），start 前设 True 会被覆盖掉。
        agent._stt_enabled = True  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_v1_binary_drives_legacy_stt(self, aiohttp_client, fresh_idempotency):
        """v1 对照：同音频 legacy 路径触发 STT——harness 有效（未改路径行为保持）。"""
        from core.input.server import create_app
        agent, calls = self._make_agent()
        await agent.start()
        self._enable_stt(agent)
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "1"})
            await self._send_speech_and_wait(ws)
            assert calls, "v1 legacy 路径应触发 STT（对照）"
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_v2_binary_without_voice_session_never_transcribes(
            self, aiohttp_client, fresh_idempotency):
        """v2 实验组：同音频零 STT 调用——D2 门生效。"""
        from core.input.server import create_app
        agent, calls = self._make_agent()
        await agent.start()
        self._enable_stt(agent)
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await self._send_speech_and_wait(ws)
            assert calls == []  # v2 无语音会话 → 不落 legacy，STT 零调用
            await ws.close()
        finally:
            await agent.stop()


@pytest.mark.service
class TestV2EmptyTextAck:
    """§3.8：v2 空文本 → rejected CONTRACT_VALIDATION（不再静默丢弃）。"""
    @pytest.mark.asyncio
    async def test_v2_empty_text_gets_rejected(self, aiohttp_client, fresh_idempotency):
        from core.input.server import create_app
        from core.cognitive.tests.test_v2_generation import _make_agent_with, _UsageLLM

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({"type": "text", "content": "   ",
                                "clientRequestId": "req_empty"})
            got = None
            for _ in range(8):
                m = await asyncio.wait_for(ws.receive(), timeout=5)
                if m.type == WSMsgType.TEXT:
                    d = json.loads(m.data)
                    if d.get("type") == "user_message.ack":
                        got = d
                        break
            assert got is not None
            assert got["status"] == "rejected"
            assert got["error"]["code"] == "CONTRACT_VALIDATION"
            assert got["error"]["retryable"] is False
            assert got["clientRequestId"] == "req_empty"
            await ws.close()
        finally:
            await agent.stop()


@pytest.mark.service
class TestQueueFullAck:
    """§3.7：纯文本队列满 → rejected QUEUE_FULL + entry 删；抛异常 → rejected INTERNAL + entry 删。"""

    class _QueueFullAgent:
        """process_message 返回 False（队列满）。"""
        def __init__(self, ws):
            from Module.output import WebSocketChannel
            # 真实 WebSocketChannel——_route_v2_text 经 _find_channel 按 isinstance 匹配
            self.output = type("O", (), {
                "_channels": [WebSocketChannel(ws, protocol_version=2)]})()
            self.resolve_calls = 0
        async def resolve_session(self, conversation_id, protocol_version):
            self.resolve_calls += 1
            return type("S", (), {"session_id": "default"})()
        async def process_message(self, **kwargs):
            return False

    class _RaisingAgent(_QueueFullAgent):
        async def process_message(self, **kwargs):
            raise RuntimeError("boom")

    @pytest.mark.asyncio
    async def test_plain_text_queue_full_rejected(self, fresh_idempotency):
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs
        from core.input.idempotency import get_store
        ws = FakeWs()
        agent = self._QueueFullAgent(ws)
        await handle_json_message(
            {"type": "text", "content": "你好", "clientRequestId": "q1"},
            agent, None, ws, voice_mgr=None)
        ack = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert ack and ack[0]["status"] == "rejected"
        assert ack[0]["error"]["code"] == "QUEUE_FULL"
        assert ack[0]["error"]["retryable"] is True
        # 幂等 entry 已删——同 clientRequestId 重试可重新进入（lookup 未命中）
        assert get_store().lookup("q1") is None

    @pytest.mark.asyncio
    async def test_plain_text_exception_rejected_and_entry_deleted(self, fresh_idempotency):
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs
        from core.input.idempotency import get_store
        ws = FakeWs()
        agent = self._RaisingAgent(ws)
        await handle_json_message(
            {"type": "text", "content": "你好", "clientRequestId": "q2"},
            agent, None, ws, voice_mgr=None)
        ack = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert ack and ack[0]["status"] == "rejected"
        assert ack[0]["error"]["code"] == "INTERNAL"
        assert get_store().lookup("q2") is None  # entry 已删——异常不残留

    @pytest.mark.asyncio
    async def test_plain_text_accepted_adds_sent_id(self, fresh_idempotency):
        """accepted → sent_message_ids.add(message_id)（D1 归属接口，Task 9 消费）。"""
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs
        ws = FakeWs()
        class _OkAgent(self._QueueFullAgent):
            async def process_message(self, **kwargs):
                return True
        agent = _OkAgent(ws)
        sent: set[str] = set()
        await handle_json_message(
            {"type": "text", "content": "你好", "clientRequestId": "q3"},
            agent, None, ws, voice_mgr=None, sent_message_ids=sent)
        assert len(sent) == 1

    @pytest.mark.asyncio
    async def test_media_exception_rejected_and_entry_deleted(self, fresh_idempotency):
        """#12：附件消息 process_message 抛异常 → rejected INTERNAL + entry 删（异常不残留）。"""
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs
        from core.input.idempotency import get_store
        ws = FakeWs()
        agent = self._RaisingAgent(ws)
        async def _fake_get(self, aid):
            return {"attachment_id": aid, "kind": "image", "status": "active",
                    "mime": "image/png", "filename": "x.png",
                    "stored_name": "x.png", "size": 10}
        agent.attachment_repo = type("R", (), {"get": _fake_get})()
        await handle_json_message(
            {"type": "text", "content": "", "clientRequestId": "q4",
             "attachments": [{"attachmentId": "att_1", "kind": "image",
                              "mime": "image/png"}]},
            agent, None, ws, voice_mgr=None)
        ack = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert ack and ack[0]["status"] == "rejected"
        assert ack[0]["error"]["code"] == "INTERNAL"
        assert get_store().lookup("q4") is None  # entry 已删——重试可重新进入


import re as _re


class _HeldLLM:
    """可手动放行的慢 LLM——生成在途可控（镜像 _UsageLLM 接口，stream_think 阻塞）。

    _make_agent_with 把它作为 agent.llm._backend 注入；loop 经 LLMAdapter 调
    stream_think，故须提供 _model/chat/test/close/stream_think 全接口。
    """

    def __init__(self, response: str = "你好，这是慢回复"):
        self.response = response
        self.released = asyncio.Event()
        self._model = "fake/test"

    async def chat(self, messages, **kwargs):
        await self.released.wait()
        return self.response

    async def test(self) -> dict:
        return {"ok": True, "latency_ms": 0}

    async def close(self):
        pass

    async def stream_think(self, messages, **kwargs):
        await self.released.wait()           # 生成在途——阻塞直至放行
        for tok in _re.compile(r"(.)").findall(self.response):
            yield {"content": tok, "tool_calls": None}
        yield {"content": None, "tool_calls": None}


@pytest.mark.service
class TestDisconnectOwnership:
    """D1：断连只取消本连接发起的 generation——Tab B 关闭不杀 Tab A 生成。"""

    @pytest.mark.asyncio
    async def test_other_connection_close_does_not_cancel_generation(
            self, aiohttp_client, fresh_idempotency):
        from core.input.server import create_app
        from core.cognitive.tests.test_v2_generation import _make_agent_with

        llm = _HeldLLM()
        agent = _make_agent_with(llm)
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws_a = await _drain(client, {"X-Protocol-Version": "2"})
            ws_b = await _drain(client, {"X-Protocol-Version": "2"})
            # A 发消息 → 生成在途（阻塞在 released）
            await ws_a.send_json({"type": "text", "content": "慢请求",
                                  "clientRequestId": "slow_1"})
            await asyncio.sleep(0.3)            # 等 generation 进入 THINKING/在途
            # B 断开 → 不应取消 A 的生成
            await ws_b.close()
            await asyncio.sleep(0.3)
            llm.released.set()                  # 放行 → A 应收到完整回复
            got_completed = False
            # 帧数上限取 30（对齐本文件既有 generation 流测试——completed 是
            # 建连帧 + ack/state/typing/segment 之后的第 11+ 帧，range(10) 读不够）
            for _ in range(30):
                m = await asyncio.wait_for(ws_a.receive(), timeout=5)
                if m.type == WSMsgType.TEXT:
                    d = json.loads(m.data)
                    if d.get("type") == "generation.completed":
                        got_completed = True
                        break
                    if d.get("type") == "generation.failed":
                        break
            assert got_completed               # B 断开未误杀 → completed
            await ws_a.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_own_connection_close_cancels_generation(
            self, aiohttp_client, fresh_idempotency):
        from core.input.server import create_app
        from core.cognitive.tests.test_v2_generation import (
            _make_agent_with, _registry_docs,
        )

        llm = _HeldLLM()
        agent = _make_agent_with(llm)
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({"type": "text", "content": "慢请求",
                                "clientRequestId": "slow_2"})
            await asyncio.sleep(0.3)
            await ws.close()                    # 发起者断开 → 在途生成取消
            # 轮询 registry 终态（宽限时序，仿既有 test_v2_disconnect 3s poll 模式）
            loop = asyncio.get_event_loop()
            deadline = loop.time() + 3.0
            status = None
            while loop.time() < deadline:
                docs = await _registry_docs(agent)
                if docs and docs[0].get("status"):
                    status = docs[0]["status"]
                    break
                await asyncio.sleep(0.1)
            assert status == "interrupted"      # 发起者断开 → 标 interrupted
        finally:
            await agent.stop()


@pytest.mark.service
class TestWsErrorEnvelope:
    """批次 8c F18：WS decode/validation 错误信封带 CONTRACT_VALIDATION code。"""

    @pytest.mark.asyncio
    async def test_invalid_json_error_has_code(self, aiohttp_client):
        """非法 JSON → error 帧 code=CONTRACT_VALIDATION。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_str("{not json")
            # 建连后可能有 expression 心情帧残留——循环读到 error 帧为止
            d = None
            for _ in range(10):
                m = await asyncio.wait_for(ws.receive(), timeout=5)
                if m.type != WSMsgType.TEXT:
                    continue
                d = json.loads(m.data)
                if d.get("type") == "error":
                    break
            assert d is not None and d["type"] == "error"
            assert d["code"] == "CONTRACT_VALIDATION"
            await ws.close()
        finally:
            await agent.stop()


@pytest.mark.service
class TestClientMessageValidation:
    """9B-2（Task 5）：v2 入站 ClientMessage 判别联合统一校验——负面表驱动 + 连接存活。

    覆盖：unknown type / 错误枚举 / 缺必填 / 错误字段类型 → ErrorMessage
    CONTRACT_VALIDATION；text 特判（缺 clientRequestId / list content）→ ACK
    rejected CONTRACT_VALIDATION；非 dict 帧 → error 而非断连（修 :304 bug）。
    """

    async def _make(self):
        """v2 FakeAgent + FakeWs（仿 test_voice_ws.make）+ 独立幂等表。"""
        from core.input.tests.test_voice_ws import FakeAgent, FakeWs
        from core.input.voice_sessions import VoiceSessionManager

        ws = FakeWs()
        agent = FakeAgent(ws)
        agent.bus = type("Bus", (), {
            "post": lambda self, d, **kw: agent.bus_posts.append(d)})()

        async def set_service(service, enabled):
            return {"ok": True, "enabled": enabled}
        agent.set_service = set_service
        mgr = VoiceSessionManager(agent=agent, emit=ws.send_json,
                                  on_vad_interrupt=lambda: None)
        reset_store()
        return agent, ws, mgr

    @staticmethod
    def _last_error(ws) -> dict:
        errs = [s for s in ws.sent if s.get("type") == "error"]
        assert errs, f"无 error 帧: {ws.sent}"
        return errs[-1]

    @staticmethod
    def _has_interrupt(agent) -> bool:
        return any(getattr(p, "event_type", "") == "interrupt" for p in agent.bus_posts)

    @pytest.mark.asyncio
    async def test_unknown_type_rejected_connection_alive(self):
        """unknown type → ErrorMessage CONTRACT_VALIDATION，连接存活（后续 interrupt 仍处理）。"""
        from core.input.ws_handler import handle_json_message

        agent, ws, mgr = await self._make()
        await handle_json_message({"type": "nope"}, agent, None, ws, voice_mgr=mgr)
        assert self._last_error(ws)["code"] == "CONTRACT_VALIDATION"
        # 连接存活：后续合法 interrupt 仍被处理（bus.post 收到 InterruptData）
        await handle_json_message({"type": "interrupt"}, agent, None, ws, voice_mgr=mgr)
        assert self._has_interrupt(agent), f"interrupt 未被处理: {ws.sent}"

    @pytest.mark.asyncio
    async def test_set_service_illegal_service_rejected(self):
        """错误枚举：set_service service=llm（非法，仅 stt/tts）→ CONTRACT_VALIDATION。"""
        from core.input.ws_handler import handle_json_message

        agent, ws, mgr = await self._make()
        await handle_json_message(
            {"type": "set_service", "service": "llm", "enabled": True},
            agent, None, ws, voice_mgr=mgr)
        assert self._last_error(ws)["code"] == "CONTRACT_VALIDATION"
        assert not any(s.get("type") == "service_mode_result" for s in ws.sent)

    @pytest.mark.asyncio
    async def test_text_missing_client_request_id_ack_rejected(self):
        """v2 text 缺 clientRequestId → ACK rejected CONTRACT_VALIDATION（text 不走判别联合）。"""
        from core.input.ws_handler import handle_json_message

        agent, ws, mgr = await self._make()
        await handle_json_message({"type": "text", "content": "hi"},
                                  agent, None, ws, voice_mgr=mgr)
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks and acks[-1]["status"] == "rejected"
        assert acks[-1]["error"]["code"] == "CONTRACT_VALIDATION"
        assert not any(s.get("type") == "error" for s in ws.sent)  # 走 ACK 通道非 ErrorMessage

    @pytest.mark.asyncio
    async def test_v2_list_content_text_ack_rejected(self):
        """v2 list-content text → ACK rejected CONTRACT_VALIDATION，连接存活。

        9abc R1：content-as-list 仅 v1 多模态（v2 媒体走 attachments/stickerId，
        content 恒为 str）；v2 收到 `content=[{"type":"text","text":"hi"}]` →
        ACK rejected CONTRACT_VALIDATION（非 ErrorMessage、非断连），随后合法
        interrupt 仍被处理。
        """
        from core.input.ws_handler import handle_json_message

        agent, ws, mgr = await self._make()
        await handle_json_message(
            {"type": "text",
             "content": [{"type": "text", "text": "hi"}],
             "clientRequestId": "r1"},
            agent, None, ws, voice_mgr=mgr)
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks and acks[-1]["status"] == "rejected"
        assert acks[-1]["error"]["code"] == "CONTRACT_VALIDATION"
        assert not any(s.get("type") == "error" for s in ws.sent)  # 走 ACK 通道非 ErrorMessage
        # 连接存活：后续合法 interrupt 仍被处理（bus.post 收到 InterruptData）
        await handle_json_message({"type": "interrupt"}, agent, None, ws, voice_mgr=mgr)
        assert self._has_interrupt(agent), f"interrupt 未被处理: {ws.sent}"

    @pytest.mark.asyncio
    async def test_validation_ack_non_str_ids_sanitized(self):
        """9abc R4：v2 text 校验失败 ACK 把非 str clientMessageId/conversationId 收口为 None。

        旧实现把 data.get("clientMessageId")（如 123）原样传给 _send_ack →
        UserAckMessage.clientMessageId Optional[str] 校验抛 → bare except 吞 ACK，
        客户端无响应。修复后 123 → None，ACK rejected CONTRACT_VALIDATION 正常送达。
        """
        from core.input.ws_handler import handle_json_message

        agent, ws, mgr = await self._make()
        await handle_json_message(
            {"type": "text", "content": "hi",
             "clientRequestId": "r1", "clientMessageId": 123},
            agent, None, ws, voice_mgr=mgr)
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks and acks[-1]["status"] == "rejected"
        assert acks[-1]["error"]["code"] == "CONTRACT_VALIDATION"
        assert acks[-1]["error"]["retryable"] is False

    @pytest.mark.asyncio
    async def test_non_text_missing_required_rejected(self):
        """非 text 缺必填：dictation.start 缺 clientRequestId → ErrorMessage CONTRACT_VALIDATION。"""
        from core.input.ws_handler import handle_json_message

        agent, ws, mgr = await self._make()
        await handle_json_message({"type": "dictation.start"},
                                  agent, None, ws, voice_mgr=mgr)
        assert self._last_error(ws)["code"] == "CONTRACT_VALIDATION"

    @pytest.mark.asyncio
    async def test_wrong_field_type_rejected(self):
        """错误字段类型：voice.record.start 缺 recordingId → ErrorMessage CONTRACT_VALIDATION。"""
        from core.input.ws_handler import handle_json_message

        agent, ws, mgr = await self._make()
        await handle_json_message({"type": "voice.record.start"},
                                  agent, None, ws, voice_mgr=mgr)
        assert self._last_error(ws)["code"] == "CONTRACT_VALIDATION"
        assert not any(s.get("type") == "voice.transcript" for s in ws.sent)

    @pytest.mark.asyncio
    async def test_interrupt_with_extra_field_still_processed(self):
        """无必填字段消息带 extra 字段不影响：interrupt + extra → 正常处理（bus.post）。"""
        from core.input.ws_handler import handle_json_message

        agent, ws, mgr = await self._make()
        await handle_json_message({"type": "interrupt", "extra": 123},
                                  agent, None, ws, voice_mgr=mgr)
        assert self._has_interrupt(agent)

    @pytest.mark.asyncio
    async def test_non_dict_frame_error_not_disconnect(self, aiohttp_client):
        """非 dict 帧（顶层数组/字符串）→ error CONTRACT_VALIDATION 而非断连（修 :304 bug）。"""
        import json as _json
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_str(_json.dumps(["top", "level"]))
            await ws.send_str(_json.dumps("bare string"))
            got = 0
            for _ in range(8):
                m = await asyncio.wait_for(ws.receive(), timeout=5)
                if m.type == WSMsgType.TEXT:
                    d = json.loads(m.data)
                    if d.get("type") == "error":
                        assert d["code"] == "CONTRACT_VALIDATION"
                        got += 1
                    if got >= 2:
                        break
            assert got == 2, f"应收到 2 个 error 帧，实际 {got}"
            # 连接存活：后续正常 v2 文本仍被处理（非断连）
            ack = await _send_and_read_ack(ws, "还在线", "req_alive")
            assert ack["status"] == "accepted"
            await ws.close()
        finally:
            await agent.stop()


@pytest.mark.service
class TestOfflineSessionVisible:
    """P1-1：MongoDB 离线 create 的会话立即可见——strict has_session 不再误拒。"""

    @pytest.mark.asyncio
    async def test_offline_created_session_text_ack_accepted(self, aiohttp_client,
                                                             fresh_idempotency):
        """离线 create → has_session True + 用该 conversationId 走 v2 text ACK accepted。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        # 模拟 MongoDB 离线（start 后摘除 repo._db——create 静默不落库）
        agent.session_manager._repo._db = None  # noqa: SLF001 — 测试注入离线状态
        summary = await agent.session_manager.create(title="offline")
        assert await agent.session_manager.has_session(summary.session_id) is True
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({
                "type": "text", "content": "你好",
                "clientRequestId": "req_offline",
                "conversationId": str(summary.session_id),
            })
            ack = None
            for _ in range(20):
                m = await asyncio.wait_for(ws.receive(), timeout=10)
                if m.type == WSMsgType.TEXT:
                    d = json.loads(m.data)
                    if d.get("type") == "user_message.ack":
                        ack = d
                        break
            assert ack is not None, f"未收到 ACK: 应 accepted（离线会话可见）"
            assert ack["status"] == "accepted", f"实际 {ack}"
            assert ack.get("conversationId") == str(summary.session_id)
            await ws.close()
        finally:
            await agent.stop()


@pytest.mark.service
class TestV2TextValidation:
    """P1-3：v2 text 完整帧 V2TextMessage 校验——畸形字段 → ACK rejected
    CONTRACT_VALIDATION（非 ErrorMessage）+ 连接存活（不再 TypeError 断连）。"""

    async def _make(self):
        from core.input.tests.test_voice_ws import FakeAgent, FakeWs
        from core.input.voice_sessions import VoiceSessionManager

        ws = FakeWs()
        agent = FakeAgent(ws)
        agent.bus = type("Bus", (), {
            "post": lambda self, d, **kw: agent.bus_posts.append(d)})()
        mgr = VoiceSessionManager(agent=agent, emit=ws.send_json,
                                  on_vad_interrupt=lambda: None)
        reset_store()
        return agent, ws, mgr

    @staticmethod
    def _has_interrupt(agent) -> bool:
        return any(getattr(p, "event_type", "") == "interrupt" for p in agent.bus_posts)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [
        # 畸形 attachments（int）→ 旧实现抛 TypeError: 'int' object is not iterable
        {"type": "text", "content": "hi", "clientRequestId": "r1", "attachments": 1},
        # 畸形 clientRequestId（list）→ 旧实现抛 TypeError: unhashable type: 'list'
        {"type": "text", "content": "hi", "clientRequestId": ["r2"]},
        # 缺 clientRequestId → ValidationError → ACK rejected（回归既有语义）
        {"type": "text", "content": "hi"},
        # 非 str stickerId
        {"type": "text", "content": "hi", "clientRequestId": "r4", "stickerId": 7},
    ])
    async def test_malformed_v2_text_ack_rejected_connection_alive(self, payload):
        from core.input.ws_handler import handle_json_message

        agent, ws, mgr = await self._make()
        await handle_json_message(payload, agent, None, ws, voice_mgr=mgr)
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks and acks[-1]["status"] == "rejected", f"实际 {ws.sent}"
        assert acks[-1]["error"]["code"] == "CONTRACT_VALIDATION"
        assert not any(s.get("type") == "error" for s in ws.sent), \
            "v2 text 校验错误走 ACK 通道，非 ErrorMessage"
        # 连接存活：后续合法 interrupt 被处理（原缺陷：TypeError 会断连）
        await handle_json_message({"type": "interrupt"}, agent, None, ws, voice_mgr=mgr)
        assert self._has_interrupt(agent), f"interrupt 未被处理: {ws.sent}"

    @pytest.mark.asyncio
    async def test_valid_v2_text_uses_validated_model(self):
        """合法 text（含完整 AttachmentRef）→ 走 _route_v2_text，attachments 为已校验模型。"""
        from core.input.ws_handler import handle_json_message

        agent, ws, mgr = await self._make()

        async def _fake_get(self, aid):
            return {"attachment_id": aid, "kind": "image", "status": "active",
                    "mime": "image/png", "filename": "x.png",
                    "stored_name": "x.png", "size": 10}
        agent.attachment_repo = type("R", (), {"get": _fake_get})()

        async def _ok_process(**kwargs):
            agent.processed_kwargs = kwargs
            return True
        agent.process_message = _ok_process
        await handle_json_message(
            {"type": "text", "content": "", "clientRequestId": "r5",
             "attachments": [{"attachmentId": "att_1", "kind": "image",
                              "mime": "image/png"}]},
            agent, None, ws, voice_mgr=mgr)
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks and acks[-1]["status"] == "accepted", f"实际 {ws.sent}"
        # process_message 收到校验后安全字段（conversation_id=None 缺省 → 未校验 data 无加法/索引）
        assert agent.processed_kwargs["conversation_id"] is None
