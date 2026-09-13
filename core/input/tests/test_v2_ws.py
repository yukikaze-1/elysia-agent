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
import time

import pytest
from aiohttp import WSMsgType

from core.cognitive.tests.test_v2_generation import _UsageLLM, _make_agent_with
from core.storage.idempotency import reset_store


async def _noop_ref(self, aid, mid):
    """测试 fake attachment repo 的 add_ref/release_reservation no-op。"""
    return None


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


@pytest.mark.service
class TestV2MoodConnectInstanceState:
    """Task 12（§6.5 读端迁移）：WS 建连 expression(mood) 帧读 instance engine.state。

    M3 后 mood 权威在 engine.state（runtime.state）；建连帧须从当前实例
    engine.state 读，而非 session.character_state（非权威，stale）。覆盖
    ws_handler._open_ws_connection 的 mood connect 读端迁移（v1/v2 统一读
    agent.character_engine.state——Task 11 后经 registry 解析当前绑定 instance 的 engine）。
    """

    @pytest.mark.asyncio
    async def test_v2_mood_connect_reads_instance_engine_state(
            self, aiohttp_client, fresh_idempotency):
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        agent.session._messages.clear()  # noqa: SLF001 空会话
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            # 先触发一次生成：_UsageLLM 返回 <mood>开心|0.8</mood> → 写 engine.state
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({"type": "text", "content": "你好",
                                "clientRequestId": f"req_mood_{time.monotonic_ns()}"})
            # 等生成完成（收到 idle 帧）
            for _ in range(30):
                m = await asyncio.wait_for(ws.receive(), timeout=10)
                if m.type == WSMsgType.TEXT:
                    d = json.loads(m.data)
                    if d.get("type") == "state" and d.get("state") == "idle":
                        break
            # 读端迁移后当前实例 engine.state 已写入 mood（实例权威）。
            # 注意：强度经 analyze_assistant 每轮衰减（0.8 → 0.65），只需断言 mood
            # 已读自 engine.state（非默认"平静"）而非精确强度。
            assert agent.character_engine.state.mood == "开心"
            await ws.close()

            # 重连：读建连 expression 帧 → 应为 instance engine.state 的 mood
            ws2 = await client.session.ws_connect(
                client.make_url("/ws"), headers={"X-Protocol-Version": "2"})
            expr = None
            for _ in range(6):
                m = await asyncio.wait_for(ws2.receive(), timeout=5)
                if m.type == WSMsgType.TEXT:
                    d = json.loads(m.data)
                    if d.get("type") == "expression":
                        expr = d
                        break
            assert expr is not None, "重连未收到 expression 帧"
            assert expr["expression"] == "开心"
            await ws2.close()
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
        agent.session.service_state.tts_enabled = True  # 测试注入权威（会话三态字段）
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
        agent.session.service_state.tts_enabled = True  # 权威注入
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
        agent.session.service_state.tts_enabled = False  # 权威注入
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
        agent.session.service_state.tts_enabled = True  # 权威注入
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
        agent.session.service_state.stt_enabled = True  # 权威注入

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
    """§3.7：纯文本队列满 → rejected QUEUE_FULL + reject 持久化；抛异常 → rejected INTERNAL + reject 持久化。"""

    class _QueueFullAgent:
        """process_message 返回 False（队列满）。"""
        def __init__(self, ws):
            from Module.output import WebSocketChannel
            from core.agent import RunState
            # 真实 WebSocketChannel——_route_v2_text 经 _find_channel 按 isinstance 匹配
            self.output = type("O", (), {
                "channels": [WebSocketChannel(ws, protocol_version=2)]})()
            self.resolve_calls = 0
            # CR-08：False 的成因由 `agent.state` 区分（RUNNING → QUEUE_FULL，
            # 否则 AGENT_BUSY）。本替身模拟的是「运行中队列满」。
            self.state = RunState.RUNNING
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
        from core.storage.idempotency import get_store
        from core.storage.idempotency_repo import STATUS_REJECTED
        ws = FakeWs()
        agent = self._QueueFullAgent(ws)
        await handle_json_message(
            {"type": "text", "content": "你好", "clientRequestId": "q1"},
            agent, None, ws, voice_mgr=None)
        ack = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert ack and ack[0]["status"] == "rejected"
        assert ack[0]["error"]["code"] == "QUEUE_FULL"
        assert ack[0]["error"]["retryable"] is True
        # S2-C1：`retryable=True` 的拒绝**不得落成终态**——否则同 clientRequestId
        # 重试只会永远重放这条 QUEUE_FULL（客户端遵守 retryable 重发多少次都不成功，
        # 且没有任何线索指向"必须换 clientRequestId"）。保持 CLAIMED 让重试能被重新处理。
        entry = await get_store().lookup("default", "q1")
        assert entry is not None and entry.status != STATUS_REJECTED, (
            "retryable 的拒绝被落成终态 → 该幂等键永久失败")
        assert entry.status == "claimed"
        ws.sent.clear()
        await handle_json_message(
            {"type": "text", "content": "你好", "clientRequestId": "q1"},
            agent, None, ws, voice_mgr=None)
        ack2 = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert ack2 and ack2[0]["status"] == "rejected"
        # S2-C1：**瞬时**拒绝不落终态，故重试不再是"逐字重放"——它回到
        # 「原请求可能仍在途」的 IN_PROGRESS 语义（可退避重试，最终能成功）。
        # 终态拒绝（`retryable=False`）的重放语义不变，见
        # `test_plain_text_exception_rejected_and_entry_rejected`。
        assert ack2[0]["error"]["code"] == "IN_PROGRESS", ack2[0]["error"]
        assert ack2[0]["error"]["retryable"] is True

    @pytest.mark.asyncio
    async def test_plain_text_exception_rejected_and_entry_rejected(self, fresh_idempotency):
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs
        from core.storage.idempotency import get_store
        from core.storage.idempotency_repo import STATUS_REJECTED
        ws = FakeWs()
        agent = self._RaisingAgent(ws)
        await handle_json_message(
            {"type": "text", "content": "你好", "clientRequestId": "q2"},
            agent, None, ws, voice_mgr=None)
        ack = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert ack and ack[0]["status"] == "rejected"
        assert ack[0]["error"]["code"] == "INTERNAL"
        # Task 4：异常路径 persist rejected（不残留 CLAIMED——重放原 rejected）
        entry = await get_store().lookup("default", "q2")
        assert entry is not None and entry.status == STATUS_REJECTED
        assert entry.error["code"] == "INTERNAL"

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
    async def test_media_exception_rejected_and_entry_rejected(self, fresh_idempotency):
        """#12：附件消息 process_message 抛异常 → rejected INTERNAL + reject 持久化。"""
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs
        from core.storage.idempotency import get_store
        from core.storage.idempotency_repo import STATUS_REJECTED
        ws = FakeWs()
        agent = self._RaisingAgent(ws)
        async def _fake_get(self, aid):
            return {"attachment_id": aid, "kind": "image", "status": "active",
                    "mime": "image/png", "filename": "x.png",
                    "stored_name": "x.png", "size": 10}
        agent.attachment_repo = type("R", (), {"get": _fake_get,
                                               "add_ref": _noop_ref,
                                               "release_reservation": _noop_ref})()
        await handle_json_message(
            {"type": "text", "content": "", "clientRequestId": "q4",
             "attachments": [{"attachmentId": "att_1", "kind": "image",
                              "mime": "image/png"}]},
            agent, None, ws, voice_mgr=None)
        ack = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert ack and ack[0]["status"] == "rejected"
        assert ack[0]["error"]["code"] == "INTERNAL"
        # Task 4：异常路径 persist rejected（重试重放原 rejected，不重新进入）
        entry = await get_store().lookup("default", "q4")
        assert entry is not None and entry.status == STATUS_REJECTED
        assert entry.error["code"] == "INTERNAL"


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
        agent.attachment_repo = type("R", (), {"get": _fake_get,
                                               "add_ref": _noop_ref,
                                               "release_reservation": _noop_ref})()

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
        # 缺省 conversationId 在 ACK 前解析并固化为实际 active，避免消费前切 active 改投。
        assert agent.processed_kwargs["conversation_id"] == "default"


# ---------------------------------------------------------------------------
# Task 4：v2 幂等命中状态机 + 并发 loser 分支（spec §1.1/§1.2，fake agent + fake
# generation registry + fake session manager；marker=service）。
# ---------------------------------------------------------------------------


class _FakeGenRegistry:
    """find_by_user_message 证据 fake（stale CLAIMED 恢复判定；None = 无证据）。"""

    def __init__(self, doc=None):
        self.doc = doc
        self.calls: list[str] = []

    async def find_by_user_message(self, user_message_id):
        self.calls.append(str(user_message_id))
        return self.doc


class _FakeSessionMgr:
    """计数版 session_manager：text 走 has_session/resolve/ensure_loaded；
    voice 断言 reserve_work/release_work 顺序与泄漏。"""

    def __init__(self, known=None):
        self._known = set(known or {"default", "conv_a"})
        self.reserve_calls = 0
        self.release_calls = 0

    async def has_session(self, sid) -> bool:
        return str(sid) in self._known

    async def resolve(self, conversation_id, protocol_version):
        return type("S", (), {"session_id": str(conversation_id or "default")})()

    async def ensure_loaded(self, sid):
        return type("S", (), {"session_id": str(sid)})()

    async def reserve_work(self, conversation_id, protocol_version):
        from core.memory.session_manager import SessionNotFoundError
        self.reserve_calls += 1
        sid = str(conversation_id or "default")
        if sid not in self._known:
            raise SessionNotFoundError(sid)
        return type("S", (), {"session_id": sid})()

    def release_work(self, sid):
        self.release_calls += 1
        return None


class _FakeV2Agent:
    """_route_v2_text 的 fake agent（可注入 process 行为 / generation 证据 / 附件 repo）。"""

    def __init__(self, ws, *, gen_registry=None, session_manager=None):
        from Module.output import WebSocketChannel
        self.output = type("O", (), {
            "channels": [WebSocketChannel(ws, protocol_version=2)]})()
        self.user_name = "用户"
        self._rid = 0
        self.idle = type("Idle", (), {"touch": lambda self: None})()
        self.stt_enabled = True
        self.bus_posts = []
        bus_posts = self.bus_posts
        self.bus = type("Bus", (), {
            "post": lambda self, d: bus_posts.append(d)})()
        self.process_calls = 0
        self.process_result: bool = True
        self.process_exc: Exception | None = None
        self.gen_registry = gen_registry
        self.session_manager = session_manager or _FakeSessionMgr()
        # CR-08：`process_message` 返回 False 时，错误码由 `agent.state` 区分
        # （RUNNING → QUEUE_FULL，否则 AGENT_BUSY）。本替身默认模拟运行中的 agent。
        from core.agent import RunState
        self.state = RunState.RUNNING

    @property
    def generation_registry(self):
        return self.gen_registry

    def next_request_id(self):
        self._rid += 1
        return f"req_{self._rid}"

    async def process_message(self, **kwargs):
        self.process_calls += 1
        if self.process_exc is not None:
            raise self.process_exc
        return self.process_result


class _FakeAttachmentRepo:
    """附件 fake：get 恒返回 active；add_ref 可注入失败；记录 release_reservation。"""

    def __init__(self, *, fail_on=None):
        self.fail_on = set(fail_on or [])
        self.released: list[str] = []
        self.refs: list[tuple[str, str]] = []

    async def get(self, attachment_id):
        return {"attachment_id": str(attachment_id), "kind": "image", "status": "active",
                "mime": "image/png", "filename": "x.png", "size": 10}

    async def add_ref(self, aid, mid):
        if str(aid) in self.fail_on:
            from core.storage.attachment_repo import StorageUnavailableError
            raise StorageUnavailableError("offline")
        self.refs.append((str(aid), str(mid)))

    async def release_reservation(self, aid, mid):
        self.released.append(str(aid))


def _text_msg(client_request_id: str, content: str = "你好") -> dict:
    return {"type": "text", "content": content, "clientRequestId": client_request_id}


@pytest.mark.service
class TestV2IdempotencyStateMachine:
    """Task 4：v2 text/附件幂等命中状态机（§1.1/§1.2）+ reject 持久化 + 并发去重。"""

    @pytest.mark.asyncio
    async def test_concurrent_same_key_same_content_single_process(self, fresh_idempotency):
        """并发同键同内容：单进程内并发请求恒恰一次 process；accepted ACK 同 messageId。"""
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs
        ws = FakeWs()
        agent = _FakeV2Agent(ws)
        # 首请求（winner）先完成 → 后续并发请求均为 accepted 重放
        await handle_json_message(_text_msg("r1"), agent, None, ws, voice_mgr=None)
        await asyncio.gather(
            handle_json_message(_text_msg("r1"), agent, None, ws, voice_mgr=None),
            handle_json_message(_text_msg("r1"), agent, None, ws, voice_mgr=None),
        )
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        accepted = [a for a in acks if a["status"] == "accepted"]
        assert len(accepted) == 3, f"三请求都应 accepted: {acks}"
        assert len({a["messageId"] for a in accepted}) == 1, "同键重放须同一 messageId"
        assert agent.process_calls == 1, "并发同键不重复 process"
        assert len(agent.bus_posts) == 0  # text 路径不直接 bus.post（process_message 负责）

    @pytest.mark.asyncio
    async def test_concurrent_same_key_diff_content_no_crosswire(self, fresh_idempotency):
        """并发同键异内容：winner accepted；loser 异摘要 → CONTRACT_VALIDATION，不串线。"""
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs
        ws = FakeWs()
        agent = _FakeV2Agent(ws)
        await handle_json_message(_text_msg("r1", "内容A"), agent, None, ws, voice_mgr=None)
        await asyncio.gather(
            handle_json_message(_text_msg("r1", "内容A"), agent, None, ws, voice_mgr=None),
            handle_json_message(_text_msg("r1", "内容B"), agent, None, ws, voice_mgr=None),
        )
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        accepted = [a for a in acks if a["status"] == "accepted"]
        rejected = [a for a in acks if a["status"] == "rejected"]
        assert len(accepted) == 2 and all(a["messageId"] for a in accepted)
        assert len(rejected) == 1
        assert rejected[0]["error"]["code"] == "CONTRACT_VALIDATION"
        assert not rejected[0].get("messageId"), "异摘要拒绝不串 winner 的 messageId"
        assert agent.process_calls == 1, "异内容拒绝不触发 process"

    @pytest.mark.asyncio
    async def test_concurrent_same_key_text_loser_in_progress(self, fresh_idempotency):
        """真实并发（两 text 同时 claim）：winner accepted + loser rejected IN_PROGRESS，
        process 恒 1——覆盖 ``if not result.acquired:`` loser 分支（BE-P0-01 回归）。

        fake store 的 lookup 恒 miss、首个 store 为 winner（acquired=True），第二个
        store 返回 winner 的新鲜 CLAIMED（acquired=False）；winner 在 process 中被 gate
        阻塞，确保 loser claim 时 entry 仍为 CLAIMED（不是 ACCEPTED 重放）。
        """
        import core.input.ws_v2 as _m
        from core.storage.idempotency import IdempotencyEntry, IdempotencyStore, StoreResult
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs

        class _RacingTextStore(IdempotencyStore):
            """lookup 恒 miss；首个 store 为 winner（新鲜 CLAIMED，acquired=True），
            后续 store 为 loser（acquired=False，返回 winner entry）。"""

            def __init__(self):
                super().__init__()
                self._winner: IdempotencyEntry | None = None
                self._claimed = asyncio.Event()

            async def lookup(self, conversation_id, client_request_id):
                return None  # 两请求 lookup 时 winner 尚未落表（真实并发窗口）

            async def store(self, conversation_id, client_request_id, entry):
                if self._winner is None:
                    entry.claimed_at = time.time()  # 新鲜 CLAIMED（IN_PROGRESS 判定）
                    self._winner = entry
                    self._claimed.set()
                    return StoreResult(entry=entry, acquired=True)
                await self._claimed.wait()
                return StoreResult(entry=self._winner, acquired=False)

        ws = FakeWs()
        agent = _FakeV2Agent(ws)
        gate = asyncio.Event()
        claimed = asyncio.Event()

        async def _gated_process(**kwargs):
            agent.process_calls += 1
            claimed.set()
            await gate.wait()
            return True

        agent.process_message = _gated_process
        racing_store = _RacingTextStore()  # 单实例——get_store() 恒返回同一 fake store
        original = _m.get_store
        _m.get_store = lambda: racing_store
        try:
            async def send():
                await handle_json_message(_text_msg("r_con"), agent, None, ws, voice_mgr=None)

            a_task = asyncio.create_task(send())
            await claimed.wait()  # winner 已 claim 并在 process 中阻塞
            b_task = asyncio.create_task(send())
            await asyncio.wait_for(b_task, timeout=5)  # loser 完成：IN_PROGRESS 拒绝
            gate.set()  # 放行 winner
            await a_task
        finally:
            _m.get_store = original
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        statuses = sorted(a["status"] for a in acks)
        assert statuses == ["accepted", "rejected"], f"实际 {acks}"
        rejected = next(a for a in acks if a["status"] == "rejected")
        assert rejected["error"]["code"] == "IN_PROGRESS"
        assert rejected["error"]["retryable"] is True
        assert not rejected.get("messageId"), "loser 不串 winner 的 messageId"
        accepted = next(a for a in acks if a["status"] == "accepted")
        assert accepted["messageId"]
        assert agent.process_calls == 1, "并发同键恒恰一次 process"

    @pytest.mark.asyncio
    async def test_text_store_loser_takeover_reclaim_processes(self, fresh_idempotency):
        """text store loser = stale CLAIMED + 无证据 → TAKEOVER → 二次 claim 接管
        （acquired=True）→ 进入业务流（process 一次，accepted）。"""
        import core.input.ws_v2 as _m
        from core.storage.idempotency import IdempotencyEntry, IdempotencyStore, StoreResult
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs

        class _TextTakeoverStore(IdempotencyStore):
            """lookup 恒 miss；store 1 → stale CLAIMED loser；store 2 → 接管 acquired=True。"""

            def __init__(self, stale: IdempotencyEntry):
                super().__init__()
                self.stale = stale
                self.store_calls = 0

            async def lookup(self, conversation_id, client_request_id):
                return None

            async def store(self, conversation_id, client_request_id, entry):
                self.store_calls += 1
                if self.store_calls == 1:
                    return StoreResult(entry=self.stale, acquired=False)
                return StoreResult(entry=self.stale, acquired=True)

        import hashlib as _hl
        stale = IdempotencyEntry(
            user_message_id="msg_loser_tk", content_digest=_hl.sha256("你好".encode()).hexdigest(),
            created_at=time.monotonic(), conversation_id="default",
            claimed_at=time.time() - 60.0)  # 过期 CLAIMED
        ws = FakeWs()
        agent = _FakeV2Agent(ws, gen_registry=_FakeGenRegistry(doc=None))  # 无证据
        takeover_store = _TextTakeoverStore(stale)  # 单实例——get_store() 恒返回同一 fake
        original = _m.get_store
        _m.get_store = lambda: takeover_store
        try:
            await handle_json_message(_text_msg("r_tk"), agent, None, ws, voice_mgr=None)
        finally:
            _m.get_store = original
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks and acks[-1]["status"] == "accepted", f"实际 {ws.sent}"
        assert acks[-1]["messageId"] == "msg_loser_tk", "接管沿用原 messageId"
        assert agent.process_calls == 1, "接管后进入业务流（process 一次）"

    @pytest.mark.asyncio
    async def test_text_store_loser_reclaim_fails_in_progress_fallback(self, fresh_idempotency):
        """text store loser = stale CLAIMED + 无证据 → TAKEOVER → 二次 claim 仍
        acquired=False（纯内存不可接管/竞态输家）→ 兜底 IN_PROGRESS（不静默无 ACK）。"""
        import core.input.ws_v2 as _m
        from core.storage.idempotency import IdempotencyEntry, IdempotencyStore, StoreResult
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs

        class _AlwaysLoserStore(IdempotencyStore):
            """lookup 恒 miss；store 恒返回 stale CLAIMED loser（acquired=False）。"""

            def __init__(self, stale: IdempotencyEntry):
                super().__init__()
                self.stale = stale

            async def lookup(self, conversation_id, client_request_id):
                return None

            async def store(self, conversation_id, client_request_id, entry):
                return StoreResult(entry=self.stale, acquired=False)

        import hashlib as _hl
        stale = IdempotencyEntry(
            user_message_id="msg_loser_fb", content_digest=_hl.sha256("你好".encode()).hexdigest(),
            created_at=time.monotonic(), conversation_id="default",
            claimed_at=time.time() - 60.0)  # 过期 CLAIMED
        ws = FakeWs()
        agent = _FakeV2Agent(ws, gen_registry=_FakeGenRegistry(doc=None))  # 无证据
        always_loser = _AlwaysLoserStore(stale)  # 单实例——get_store() 恒返回同一 fake
        original = _m.get_store
        _m.get_store = lambda: always_loser
        try:
            await handle_json_message(_text_msg("r_fb"), agent, None, ws, voice_mgr=None)
        finally:
            _m.get_store = original
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks and acks[-1]["status"] == "rejected", f"实际 {ws.sent}"
        assert acks[-1]["error"]["code"] == "IN_PROGRESS", "接管失败兜底 IN_PROGRESS"
        assert acks[-1]["error"]["retryable"] is True
        assert agent.process_calls == 0, "接管失败不进入业务处理"

    @pytest.mark.asyncio
    async def test_text_takeover_reclaim_storage_unavailable(self, fresh_idempotency):
        """TAKEOVER 二次 claim 期间 Mongo 断连（StorageUnavailableError）→ 幂等感知
        rejected STORAGE_UNAVAILABLE（D3 durable）+ 不进入业务处理（接管未取得所有权）。"""
        import core.input.ws_v2 as _m
        from core.storage.idempotency import IdempotencyEntry, IdempotencyStore, StoreResult, get_store
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs
        from core.storage.attachment_repo import StorageUnavailableError

        class _ReclaimFailStore(IdempotencyStore):
            """lookup 恒 miss；store 1 → stale CLAIMED loser；store 2 → raise。"""

            def __init__(self, stale: IdempotencyEntry):
                super().__init__()
                self.stale = stale
                self.store_calls = 0

            async def lookup(self, conversation_id, client_request_id):
                return None

            async def store(self, conversation_id, client_request_id, entry):
                self.store_calls += 1
                if self.store_calls == 1:
                    return StoreResult(entry=self.stale, acquired=False)
                raise StorageUnavailableError("Mongo down")

        import hashlib as _hl
        stale = IdempotencyEntry(
            user_message_id="msg_loser_su", content_digest=_hl.sha256("你好".encode()).hexdigest(),
            created_at=time.monotonic(), conversation_id="default",
            claimed_at=time.time() - 60.0)  # 过期 CLAIMED
        ws = FakeWs()
        agent = _FakeV2Agent(ws, gen_registry=_FakeGenRegistry(doc=None))  # 无证据
        reclaim_fail = _ReclaimFailStore(stale)  # 单实例——get_store() 恒返回同一 fake
        original = _m.get_store
        _m.get_store = lambda: reclaim_fail
        try:
            await handle_json_message(_text_msg("r_su"), agent, None, ws, voice_mgr=None)
        finally:
            _m.get_store = original
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks and acks[-1]["status"] == "rejected", f"实际 {ws.sent}"
        assert acks[-1]["error"]["code"] == "STORAGE_UNAVAILABLE"
        assert acks[-1]["error"]["retryable"] is True
        assert acks[-1]["error"]["details"]["idempotency_durable"] is get_store().durable
        assert agent.process_calls == 0, "接管 claim 失败不进入业务处理"

    @pytest.mark.asyncio
    async def test_claimed_fresh_rejects_in_progress(self, fresh_idempotency):
        """同键 CLAIMED 新鲜 → rejected IN_PROGRESS（retryable + retryAfterMs=30000）。"""
        import hashlib as _hl
        from core.storage.idempotency import (
            IN_PROGRESS_RETRY_AFTER_MS, IdempotencyEntry, get_store,
        )
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs
        store = get_store()
        digest = _hl.sha256("你好".encode("utf-8")).hexdigest()
        await store.store("default", "r1", IdempotencyEntry(
            user_message_id="msg_inflight", content_digest=digest,
            created_at=time.monotonic(), conversation_id="default"))
        ws = FakeWs()
        agent = _FakeV2Agent(ws)
        await handle_json_message(_text_msg("r1"), agent, None, ws, voice_mgr=None)
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks and acks[-1]["status"] == "rejected", f"实际 {ws.sent}"
        err = acks[-1]["error"]
        assert err["code"] == "IN_PROGRESS"
        assert err["retryable"] is True
        assert err["retryAfterMs"] == IN_PROGRESS_RETRY_AFTER_MS == 30000
        assert agent.process_calls == 0, "新鲜 CLAIMED 命中不进入业务处理"
        # entry 保持 claimed（未接管、未 complete）
        entry = await store.lookup("default", "r1")
        assert entry is not None and entry.status == "claimed"

    @pytest.mark.asyncio
    async def test_claimed_stale_with_evidence_replays_accepted(self, fresh_idempotency):
        """同键 CLAIMED 过期 + 证据（generation 存在）→ 重放 accepted + complete。"""
        import hashlib as _hl
        from core.storage.idempotency import IdempotencyEntry, get_store
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs
        store = get_store()
        digest = _hl.sha256("你好".encode("utf-8")).hexdigest()
        await store.store("default", "r1", IdempotencyEntry(
            user_message_id="msg_stale_evidence", content_digest=digest,
            created_at=time.monotonic(), conversation_id="default"))
        entry = store._entries[("default", "r1")]  # noqa: SLF001 — 测试篡改 claimed_at
        entry.claimed_at = time.time() - 60.0  # 过期 CLAIMED（> GRACE_SECONDS）
        ws = FakeWs()
        gen = _FakeGenRegistry(doc={"generation_id": "gen_1"})
        agent = _FakeV2Agent(ws, gen_registry=gen)
        await handle_json_message(_text_msg("r1"), agent, None, ws, voice_mgr=None)
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks and acks[-1]["status"] == "accepted", f"实际 {ws.sent}"
        assert acks[-1]["messageId"] == "msg_stale_evidence"
        assert agent.process_calls == 0, "有证据不重执行"
        assert gen.calls == ["msg_stale_evidence"]
        # complete 已前置：entry 标记 accepted（重放后终态确定）
        assert (await store.lookup("default", "r1")).status == "accepted"

    @pytest.mark.asyncio
    async def test_claimed_stale_no_evidence_takeover_reprocesses(self, fresh_idempotency):
        """同键 CLAIMED 过期 + 无证据 → 接管重执行（process 一次，同 messageId）。

        接管经 repo.claim 对 stale CLAIMED 的 update_one 实现（acquired=True）——
        纯内存路径内存优先去重不可接管，故用 fake repo 预置 stale CLAIMED 文档。
        """
        import hashlib as _hl
        from core.storage.idempotency import configure_store, get_store
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs
        from core.storage.idempotency_repo import IdempotencyRepository, STATUS_CLAIMED
        from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
        repo = IdempotencyRepository(db=FakeDocumentDBBackend())
        await repo.claim(
            "default", "r1",
            content_digest=_hl.sha256("你好".encode("utf-8")).hexdigest(),
            message_id="msg_takeover", status=STATUS_CLAIMED,
            claimed_at=time.time() - 60.0,  # 过期 CLAIMED（> GRACE_SECONDS）
            expires_at=None)
        configure_store(repo, product_mode=False)
        ws = FakeWs()
        agent = _FakeV2Agent(ws, gen_registry=_FakeGenRegistry(doc=None))  # 无证据
        await handle_json_message(_text_msg("r1"), agent, None, ws, voice_mgr=None)
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks and acks[-1]["status"] == "accepted", f"实际 {ws.sent}"
        assert acks[-1]["messageId"] == "msg_takeover", "接管沿用原 messageId"
        assert agent.process_calls == 1, "无证据接管重执行一次"
        assert (await get_store().lookup("default", "r1")).status == "accepted"

    @pytest.mark.asyncio
    async def test_retryable_rejection_is_not_persisted_as_terminal(self, fresh_idempotency):
        """**S2-C1 修正**：QUEUE_FULL 是 `retryable=True`，**不得**落成终态。

        原用例名 `test_rejected_replay_byte_identical`，断言"同键重试逐字重放原 rejected"
        ——**把缺陷写成了预期**。那条语义对**瞬时**拒绝是致命的：客户端遵守 `retryable`
        重发多少次都只会拿到同一条拒绝，且没有任何线索指向"必须换 clientRequestId"。
        （终态拒绝的重放语义仍然成立，由 `test_process_exception_rejects_internal`
        那条 `retryable=False` 的路径覆盖。）
        """
        from core.storage.idempotency import get_store
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs
        ws = FakeWs()
        agent = _FakeV2Agent(ws)
        agent.process_result = False  # 队列满
        await handle_json_message(_text_msg("q1"), agent, None, ws, voice_mgr=None)
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks and acks[-1]["status"] == "rejected"
        assert acks[-1]["error"]["code"] == "QUEUE_FULL"
        # 幂等键**未**落终态 → 重试路径仍然活着
        entry = await get_store().lookup("default", "q1")
        assert entry is not None and entry.status == "claimed", (
            f"retryable 的拒绝被落成 {entry.status} → 该幂等键永久失败")
        # 立即重试：新鲜 CLAIMED → IN_PROGRESS（可退避重试），不是重放 QUEUE_FULL
        ws.sent.clear()
        await handle_json_message(_text_msg("q1"), agent, None, ws, voice_mgr=None)
        acks2 = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks2 and acks2[-1]["status"] == "rejected"
        assert acks2[-1]["error"]["code"] == "IN_PROGRESS", (
            f"应回到「原请求可能仍在途」的语义，实得 {acks2[-1]['error']}")
        assert acks2[-1]["clientRequestId"] == "q1"
        # 首次调用已进过一次业务流（返回 False），重试**不再**进入
        assert agent.process_calls == 1, "立即重试走 IN_PROGRESS，不重复进入业务流"

    @pytest.mark.asyncio
    async def test_process_exception_rejects_internal(self, fresh_idempotency):
        """process_message 抛异常 → store.reject INTERNAL；重试重放原 rejected。"""
        from core.storage.idempotency import get_store
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs
        ws = FakeWs()
        agent = _FakeV2Agent(ws)
        agent.process_exc = RuntimeError("boom")
        await handle_json_message(_text_msg("q2"), agent, None, ws, voice_mgr=None)
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks and acks[-1]["status"] == "rejected"
        assert acks[-1]["error"]["code"] == "INTERNAL"
        entry = await get_store().lookup("default", "q2")
        assert entry is not None and entry.status == "rejected"
        assert entry.error["code"] == "INTERNAL"

    @pytest.mark.asyncio
    async def test_attachment_refs_failure_reject_and_rollback(self, fresh_idempotency):
        """附件 refs 失败 → store.reject STORAGE_UNAVAILABLE + 附件预留回滚。"""
        from core.storage.idempotency import get_store
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs
        ws = FakeWs()
        agent = _FakeV2Agent(ws)
        repo = _FakeAttachmentRepo(fail_on={"att_y"})
        agent.attachment_repo = repo
        await handle_json_message(
            {"type": "text", "content": "hi", "clientRequestId": "ab",
             "attachments": [{"attachmentId": "att_x", "kind": "image", "mime": "image/png"},
                             {"attachmentId": "att_y", "kind": "image", "mime": "image/png"}]},
            agent, None, ws, voice_mgr=None)
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks and acks[-1]["status"] == "rejected"
        assert acks[-1]["error"]["code"] == "STORAGE_UNAVAILABLE"
        assert agent.process_calls == 0, "refs 未全持久化不调 process"
        assert repo.released == ["att_x"], "已预留 refs 需回滚"
        # S2-C1：STORAGE_UNAVAILABLE 是**瞬时**故障（retryable=True）→ 不落终态；
        # 保持 CLAIMED，存储恢复后同键重试能被重新处理。
        entry = await get_store().lookup("default", "ab")
        assert entry is not None and entry.status != "rejected", (
            "瞬时故障被落成终态 → 该幂等键永久失败")
        assert entry.status == "claimed"

    @pytest.mark.asyncio
    async def test_storage_unavailable_ack_carries_idempotency_durable(self, fresh_idempotency):
        """D3：STORAGE_UNAVAILABLE rejected ACK 的 error.details 含 idempotency_durable。"""
        from core.storage.idempotency import configure_store, get_store
        from core.input.ws_handler import handle_json_message
        from core.input.tests.test_voice_ws import FakeWs
        from core.storage.attachment_repo import StorageUnavailableError

        class _OfflineRepo:
            """is_connected=False + 全操作抛 StorageUnavailableError（离线 Mongo fake）。"""
            is_connected = False

            async def get(self, conversation_id, client_request_id):
                raise StorageUnavailableError("Mongo down")

            async def claim(self, *a, **kw):
                raise StorageUnavailableError("Mongo down")

            async def complete(self, *a, **kw):
                raise StorageUnavailableError("Mongo down")

            async def reject(self, *a, **kw):
                raise StorageUnavailableError("Mongo down")

            async def release(self, *a, **kw):
                raise StorageUnavailableError("Mongo down")

        # 产品模式 + Mongo 离线 → lookup fail-closed → STORAGE_UNAVAILABLE
        configure_store(_OfflineRepo(), product_mode=True)
        store = get_store()
        assert store.durable is False
        ws = FakeWs()
        agent = _FakeV2Agent(ws)
        await handle_json_message(_text_msg("durable"), agent, None, ws, voice_mgr=None)
        acks = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert acks and acks[-1]["status"] == "rejected"
        assert acks[-1]["error"]["code"] == "STORAGE_UNAVAILABLE"
        assert acks[-1]["error"]["details"]["idempotency_durable"] is store.durable
        assert store.durable is False


# ---------------------------------------------------------------------------
# Task 7（BE-P1-03）：WS 限流关联——text 超限 → rejected ACK RATE_LIMITED（原
# clientRequestId + retryAfterMs）经真实 WS 连接送达，promise 立即 settle；缺失
# clientRequestId → 退化 generic error（marker=service）。
# ---------------------------------------------------------------------------

@pytest.mark.service
class TestWsRateLimitCorrelation:
    """Task 7：WS 消息限流与发送请求关联（真实 WS + create_app(rate_limits=...)）。"""

    @pytest.mark.asyncio
    async def test_v2_text_over_limit_rejected_ack_over_ws(
            self, aiohttp_client, fresh_idempotency):
        """第一条 text 配额内 accepted；第二条超限 → rejected RATE_LIMITED（原 clientRequestId）。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False, rate_limits={"messages": 1})
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({"type": "text", "content": "第一条", "clientRequestId": "rl_1"})
            ack1 = None
            for _ in range(8):
                m = await asyncio.wait_for(ws.receive(), timeout=5)
                if m.type == WSMsgType.TEXT:
                    d = json.loads(m.data)
                    if d.get("type") == "user_message.ack":
                        ack1 = d
                        break
            assert ack1 is not None and ack1["status"] == "accepted"
            # 第二条 → 超限 → rejected ACK RATE_LIMITED（原 clientRequestId + retryAfterMs）
            await ws.send_json({"type": "text", "content": "第二条", "clientRequestId": "rl_2"})
            ack2 = None
            for _ in range(8):
                m = await asyncio.wait_for(ws.receive(), timeout=5)
                if m.type == WSMsgType.TEXT:
                    d = json.loads(m.data)
                    if d.get("type") == "user_message.ack":
                        ack2 = d
                        break
            assert ack2 is not None, "超限应收到 rejected ACK（立即 settle，不超时）"
            assert ack2["status"] == "rejected"
            assert ack2["clientRequestId"] == "rl_2"          # 原 clientRequestId 关联
            assert ack2["error"]["code"] == "RATE_LIMITED"
            assert ack2["error"]["retryable"] is True
            assert ack2["error"]["retryAfterMs"] >= 1000
            assert ack2.get("messageId") == ""
            await ws.close()
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_v2_text_over_limit_missing_request_id_error_over_ws(
            self, aiohttp_client, fresh_idempotency):
        """text 超限但 clientRequestId 缺失 → generic error RATE_LIMITED（无 ACK）。"""
        from core.input.server import create_app

        agent = _make_agent_with(_UsageLLM())
        await agent.start()
        app = create_app(agent, serve_static=False, rate_limits={"messages": 1})
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await ws.send_json({"type": "text", "content": "第一条", "clientRequestId": "rl_1"})
            # 消耗唯一 token（等 ACK 确认第一条已处理）
            for _ in range(8):
                m = await asyncio.wait_for(ws.receive(), timeout=5)
                if m.type == WSMsgType.TEXT and json.loads(m.data).get("type") == "user_message.ack":
                    break
            # 第二条（无 clientRequestId）→ 超限 → generic error
            await ws.send_json({"type": "text", "content": "第二条"})
            err = None
            for _ in range(8):
                m = await asyncio.wait_for(ws.receive(), timeout=5)
                if m.type == WSMsgType.TEXT:
                    d = json.loads(m.data)
                    if d.get("type") == "error" and d.get("code") == "RATE_LIMITED":
                        err = d
                        break
            assert err is not None, f"应收到 generic error RATE_LIMITED: {err}"
            assert err["retryable"] is True
            assert err["retryAfterMs"] >= 1000
            await ws.close()
        finally:
            await agent.stop()
