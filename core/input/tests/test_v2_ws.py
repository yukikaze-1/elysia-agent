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
