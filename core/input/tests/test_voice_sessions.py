"""VoiceSessionManager 状态机测试——三模式隔离 + 边界（spec §3.3/§4/§5/§7）。"""
import asyncio
import struct

import pytest

from core.input.voice_sessions import (
    VoiceSessionError, VoiceSessionManager,
    MAX_RECORDING_BYTES, MAX_CHUNK_BYTES,
)

pytestmark = pytest.mark.unit


def _loud(samples: int) -> bytes:
    return struct.pack(f"<{samples}h", *([20000] * samples))


def _sil(samples: int) -> bytes:
    return struct.pack(f"<{samples}h", *([5] * samples))


class _FakeSTTBackend:
    """返回 _last_text 的假 STT——供 _transcribe_no_emotion 调 agent.stt.transcribe。"""

    def __init__(self, agent):
        self._agent = agent

    async def transcribe(self, wav):
        return self._agent._last_text


class FakeAgent:
    def __init__(self):
        self.stt = _FakeSTTBackend(self)
        self.stt_enabled = True
        self.stt_emotion_state = {"emotion": "HAPPY", "events": ["x"]}
        self.user_name = "用户"
        self.status = type("Status", (), {
            "is_speaking": False, "is_completely_idle": True,
            "should_suppress_vad_interrupt": False})()
        self._rid = 0
        self._last_text = ""

    def next_request_id(self):
        self._rid += 1
        return f"req_{self._rid}"

    async def stt_transcribe(self, wav):
        return self._last_text

    async def resolve_session(self, conversation_id, protocol_version):
        return type("S", (), {"session_id": conversation_id or "default"})()


def async_emit(emitted):
    async def emit(d):
        await asyncio.sleep(0)
        emitted.append(d)
    return emit


def make_manager(agent=None):
    agent = agent or FakeAgent()
    emitted = []
    bus_posts = []
    agent.bus = type("Bus", (), {"post": lambda self, data: bus_posts.append(data)})()
    agent.idle = type("Idle", (), {"touch": lambda self: None})()
    # silence_duration=0.1 加速 VAD 分段（生产默认 1.5s）
    mgr = VoiceSessionManager(agent=agent, emit=async_emit(emitted),
                              silence_duration=0.1)
    return mgr, emitted, bus_posts


async def _drive_segment(mgr):
    """喂 3s loud PCM + 触发 silence_duration 分段（须 silence_duration=0.1）。"""
    for _ in range(30):
        await mgr.feed_pcm(_loud(1600))  # 30 × 100ms = 3s 语音
    await mgr.feed_pcm(_sil(1600))       # 开始静音
    await asyncio.sleep(0.15)            # 超过 silence_duration
    await mgr.feed_pcm(_sil(1600))       # 触发分段


@pytest.mark.asyncio
async def test_recording_lifecycle():
    mgr, emitted, _ = make_manager()
    await mgr.start_recording("rec_1")
    await mgr.feed_pcm(_loud(6400))
    mgr._agent._last_text = "你好世界"
    await mgr.stop_recording("rec_1")
    await asyncio.sleep(0.05)  # 后台 STT 任务在同一 loop 内完成
    assert any(e.get("type") == "voice.transcript" for e in emitted)
    payload = mgr.commit_recording("rec_1", "你好世界")
    assert payload[0] == "你好世界" and payload[1] == "HAPPY"


@pytest.mark.asyncio
async def test_commit_then_restart_recording():
    """commit 后 _mode 复位 IDLE，可再次 start（_reset_recording 须重置 mode）。"""
    mgr, _, _ = make_manager()
    await mgr.start_recording("rec_1")
    mgr._agent._last_text = "第一条"
    await mgr.stop_recording("rec_1")
    await asyncio.sleep(0.05)
    mgr.commit_recording("rec_1", "第一条")
    assert mgr.has_active_session() is False
    await mgr.start_recording("rec_2")  # 不抛 VOICE_SESSION_CONFLICT
    assert mgr.has_active_session() is True


@pytest.mark.asyncio
async def test_recording_empty_transcript_no_pending():
    mgr, emitted, _ = make_manager()
    await mgr.start_recording("rec_1")
    mgr._agent._last_text = "   "
    await mgr.stop_recording("rec_1")
    await asyncio.sleep(0.05)
    assert not any(e.get("type") == "voice.transcript" for e in emitted)
    with pytest.raises(VoiceSessionError) as ei:
        mgr.commit_recording("rec_1", "")
    assert ei.value.code == "VOICE_RECORDING_NOT_FOUND"


@pytest.mark.asyncio
async def test_recording_state_boundaries():
    mgr, _, _ = make_manager()
    await mgr.start_recording("rec_1")
    # RECORDING 态 commit → CONTRACT_VALIDATION
    with pytest.raises(VoiceSessionError) as ei:
        mgr.commit_recording("rec_1", "x")
    assert ei.value.code == "CONTRACT_VALIDATION"
    # TRANSCRIBING 态 stop → CONTRACT_VALIDATION
    await mgr.stop_recording("rec_1")
    with pytest.raises(VoiceSessionError) as ei:
        await mgr.stop_recording("rec_1")
    assert ei.value.code == "CONTRACT_VALIDATION"
    # TRANSCRIBING 态 discard 允许 → 复位后二次 discard 报 NOT_FOUND
    await mgr.discard_recording("rec_1")
    with pytest.raises(VoiceSessionError) as ei:
        await mgr.discard_recording("rec_1")
    assert ei.value.code == "VOICE_RECORDING_NOT_FOUND"


@pytest.mark.asyncio
async def test_mode_conflict():
    mgr, _, _ = make_manager()
    await mgr.start_recording("rec_1")
    with pytest.raises(VoiceSessionError) as ei:
        await mgr.start_realtime("vs_1", None)
    assert ei.value.code == "VOICE_SESSION_CONFLICT"


@pytest.mark.asyncio
async def test_dictation_isolation_no_bus_posts():
    mgr, emitted, bus_posts = make_manager()
    await mgr.start_dictation("req_d1")
    mgr._agent._last_text = "转写文字"
    await _drive_segment(mgr)
    await asyncio.sleep(0.05)
    await mgr.stop_dictation("req_d1")
    assert any(e.get("type") == "dictation.partial" for e in emitted)
    finals = [e for e in emitted if e.get("type") == "dictation.final"]
    assert finals and finals[0]["text"] == "转写文字"
    assert not bus_posts  # 零 UserInputData


@pytest.mark.asyncio
async def test_realtime_turn_submits_bus():
    mgr, emitted, bus_posts = make_manager()
    await mgr.start_realtime("vs_1", None)
    mgr._agent._last_text = "实时轮次"
    await _drive_segment(mgr)
    await asyncio.sleep(0.05)
    assert any(e.get("type") == "voice_session.partial" for e in emitted)
    turns = [e for e in emitted if e.get("type") == "voice_session.turn"]
    assert turns and turns[0]["text"] == "实时轮次"
    assert any(getattr(p, "event_type", "") == "user_input" for p in bus_posts)
    await mgr.close()  # 清理 ack/idle 后台任务（review B）


@pytest.mark.asyncio
async def test_stt_disabled_ends_session():
    mgr, emitted, _ = make_manager()
    await mgr.start_realtime("vs_1", None)
    mgr._agent.stt_enabled = False
    await mgr.notify_stt_disabled()
    ends = [e for e in emitted if e.get("type") == "voice_session.ended"]
    assert ends and ends[0]["reason"] == "stt_disabled"
    assert ends[0]["sessionId"] == "vs_1"  # teardown 前缓存 ID


@pytest.mark.asyncio
async def test_pause_resume_idempotent_and_resets():
    mgr, _, _ = make_manager()
    await mgr.start_realtime("vs_1", None)
    await mgr.pause_session("vs_1")
    await mgr.pause_session("vs_1")  # 幂等忽略
    await mgr.resume_session("vs_1")
    await mgr.resume_session("vs_1")  # 幂等忽略
    assert mgr._paused is False
    await mgr.close()  # 清理 ack/idle 后台任务（review B）


@pytest.mark.asyncio
async def test_close_cleans_active_session():
    """连接断开清理（spec §12 NEW-AA）：活动会话结束、不残留。"""
    mgr, _, _ = make_manager()
    await mgr.start_realtime("vs_1", None)
    await mgr.feed_pcm(b"\x00" * 6400)  # 进入 realtime，有 activity
    await mgr.close()
    assert mgr.has_active_session() is False
    assert mgr._buffer is None  # 会话 buffer 已销毁
    # 关闭后可重新开始（mode 复位）
    await mgr.start_realtime("vs_2", None)
    assert mgr.has_active_session() is True


@pytest.mark.asyncio
async def test_idle_timeout_emits_ended():
    """300s idle 超时 → ended{timeout} 必须发出（teardown 不自取消当前任务，review NEW-1）。"""
    import core.input.voice_sessions as vs
    original = vs.IDLE_CHECK_INTERVAL
    vs.IDLE_CHECK_INTERVAL = 0.01  # 加速 idle 循环
    try:
        mgr, emitted, _ = make_manager()
        await mgr.start_realtime("vs_1", None)
        mgr._last_audio_at = 0  # 远古 → 首轮检查即超时（time.monotonic() - 0 >> 300）
        await asyncio.sleep(0.1)
        ends = [e for e in emitted if e.get("type") == "voice_session.ended"]
        assert ends and ends[-1]["reason"] == "timeout"
        assert mgr.has_active_session() is False
        await mgr.close()
    finally:
        vs.IDLE_CHECK_INTERVAL = original


@pytest.mark.asyncio
async def test_recording_too_long():
    """9C-1 后 chunk 上限收紧为 1 MiB/条——用 ≤1 MiB chunk 累积仍触发 RECORDING_TOO_LONG。"""
    mgr, emitted, _ = make_manager()
    await mgr.start_recording("rec_1")
    chunk = b"\x00" * MAX_CHUNK_BYTES       # 恰好 1 MiB，不触发 chunk 上限（R15）
    for _ in range((MAX_RECORDING_BYTES // MAX_CHUNK_BYTES) + 2):
        await mgr.feed_pcm(chunk)
    assert any(e.get("type") == "error" and e.get("code") == "VOICE_RECORDING_TOO_LONG"
               for e in emitted)


@pytest.mark.asyncio
async def test_dictation_never_touches_shared_emotion():
    """D6：dictation 全程不碰共享 stt_emotion_state——不写也不恢复。

    原「结束恢复快照」语义在 D6 后不再有意义（dictation 经 _transcribe_no_emotion
    从不动共享 state）：用 _EmotionWritingAgent 验证 dictation 未走
    agent.stt_transcribe 写点，进行中与结束后共享 state 保持一致。
    """
    agent = _EmotionWritingAgent()
    agent.stt_emotion_state = {"emotion": "SAD", "events": []}  # dictation 前状态（其他连接写入）
    mgr, emitted, _ = make_manager(agent=agent)
    await mgr.start_dictation("req_d2")
    agent._last_text = "转写"
    await _drive_segment(mgr)
    await asyncio.sleep(0.05)
    assert agent.stt_emotion_state == {"emotion": "SAD", "events": []}  # 进行中不变
    await mgr.stop_dictation("req_d2")
    assert agent.stt_emotion_state == {"emotion": "SAD", "events": []}  # 结束后不变（无恢复覆盖）


@pytest.mark.asyncio
async def test_dictation_stt_disabled_keeps_shared_emotion():
    """D6：STT 中途关闭终止 dictation → 共享 state 全程不变（不写不恢复，review NEW-3/C）。"""
    agent = _EmotionWritingAgent()
    agent.stt_emotion_state = {"emotion": "SAD", "events": []}
    mgr, emitted, _ = make_manager(agent=agent)
    await mgr.start_dictation("req_d3")
    agent._last_text = "转写"
    await _drive_segment(mgr)
    await asyncio.sleep(0.05)
    assert agent.stt_emotion_state == {"emotion": "SAD", "events": []}  # 进行中不变
    mgr._agent.stt_enabled = False
    await mgr.notify_stt_disabled()
    assert agent.stt_emotion_state == {"emotion": "SAD", "events": []}  # 关闭终止后不变
    cancelled = [e for e in emitted if e.get("type") == "dictation.cancelled"]
    assert cancelled and cancelled[-1]["clientRequestId"] == "req_d3"


class _EmotionWritingAgent(FakeAgent):
    """stt_transcribe 写共享 emotion——镜像真实 agent.stt_transcribe（agent.py:842）。"""

    async def stt_transcribe(self, wav):
        text = await super().stt_transcribe(wav)
        backend = self.stt
        self.stt_emotion_state["emotion"] = getattr(backend, "last_emotion", "NEUTRAL")
        self.stt_emotion_state["events"] = list(getattr(backend, "last_events", None) or [])
        return text


# D6：dictation 不写共享 stt_emotion_state；§3.10：dictation ID 校验
@pytest.mark.asyncio
async def test_dictation_does_not_touch_shared_emotion():
    """D6：dictation 转写不写 agent 级 stt_emotion_state——**dictation 进行中**即验证。

    断言放在 stop 之前：旧代码 dictation 经 agent.stt_transcribe（_EmotionWritingAgent）
    会把共享 state 写成 HAPPY/laughter → 进行中断言红；新代码 _transcribe_no_emotion
    不写 → 共享 state 保持其他连接的 SAD/sigh。
    """
    agent = _EmotionWritingAgent()
    # 共享状态预置"其他连接"写入的值
    agent.stt_emotion_state = {"emotion": "SAD", "events": ["sigh"]}

    class _NoEmotionSTT:
        last_emotion = "HAPPY"
        last_events = ["laughter"]
        async def transcribe(self, wav):
            return "听写内容"
    agent.stt = _NoEmotionSTT()

    mgr, emitted, _ = make_manager(agent=agent)
    await mgr.start_dictation("req_d1")
    agent._last_text = "听写内容"
    for _ in range(4):
        await mgr.feed_pcm(_loud(1600))  # 0.4s 语音 > MIN_SEGMENT_SECONDS(0.3s)，保证段触发
    await mgr.feed_pcm(_sil(1600))
    await asyncio.sleep(0.15)
    await mgr.feed_pcm(_sil(1600))
    await asyncio.sleep(0.1)   # 段 STT（同步 fake 立即完成）
    # dictation 进行中：共享 state 未被写成 HAPPY（旧代码会写）
    assert agent.stt_emotion_state == {"emotion": "SAD", "events": ["sigh"]}
    await mgr.stop_dictation("req_d1")
    finals = [s for s in emitted if s.get("type") == "dictation.final"]
    assert finals and finals[0]["text"] == "听写内容"


@pytest.mark.asyncio
async def test_stop_dictation_wrong_id_rejected():
    """§3.10：clientRequestId 与 _dict_request_id 不一致 → DICTATION_NOT_FOUND。"""
    mgr, _, _ = make_manager()
    await mgr.start_dictation("req_d1")
    with pytest.raises(VoiceSessionError) as exc:
        await mgr.stop_dictation("req_wrong")
    assert exc.value.code == "DICTATION_NOT_FOUND"
    with pytest.raises(VoiceSessionError) as exc:
        await mgr.cancel_dictation("req_wrong")
    assert exc.value.code == "DICTATION_NOT_FOUND"


@pytest.mark.asyncio
async def test_cancel_dictation_wrong_id_rejected():
    mgr, _, _ = make_manager()
    await mgr.start_dictation("req_d1")
    with pytest.raises(VoiceSessionError) as exc:
        await mgr.cancel_dictation("req_d1_other")
    assert exc.value.code == "DICTATION_NOT_FOUND"


# D3：STT 后台解耦——cancel 丢弃残段、stop/dictation-stop 等待在途段、epoch 隔离、emotion 归属


class _BlockableAgent(FakeAgent):
    """stt_transcribe 与 stt.transcribe 都可手动放行——REALTIME/DICTATION 在途段可控。

    REALTIME 走 agent.stt_transcribe（阻塞）；DICTATION 走 _transcribe_no_emotion →
    agent.stt.transcribe（阻塞）。二者共享同一 released。
    """

    class _BlockSTT:
        def __init__(self, agent):
            self._agent = agent
        async def transcribe(self, wav):
            await self._agent.released.wait()
            return self._agent._last_text

    def __init__(self):
        super().__init__()
        self.released = asyncio.Event()
        self._last_text = "在途段文本"
        self.stt = self._BlockSTT(self)      # 覆盖 FakeAgent.stt = _FakeSTTBackend(self)

    async def stt_transcribe(self, wav):
        await self.released.wait()          # 阻塞直至放行——转写在途
        return self._last_text


@pytest.mark.asyncio
async def test_cancel_discards_in_flight_segment():
    """在途 STT 段 + cancel → 段不提交（无 partial/turn/UserInputData）。"""
    agent = _BlockableAgent()
    mgr, emitted, bus_posts = make_manager(agent=agent)
    await mgr.start_realtime("rt_1", None)
    # 喂一段触发 VAD 分段 → 派发后台 STT（阻塞在 released）
    for _ in range(30):
        await mgr.feed_pcm(_loud(1600))
    await mgr.feed_pcm(_sil(1600))
    await asyncio.sleep(0.15)
    await mgr.feed_pcm(_sil(1600))
    await asyncio.sleep(0.05)               # 让后台任务进入 transcribe 阻塞
    assert len(mgr._stt_tasks) >= 1
    await mgr.cancel_session("rt_1")
    agent.released.set()                    # 放行转写 → 迟到结果应被丢弃
    await asyncio.sleep(0.1)
    assert not any(s.get("type") == "voice_session.turn" for s in emitted)
    assert not any(p for p in bus_posts)


@pytest.mark.asyncio
async def test_stop_waits_for_in_flight_segment():
    """stop 等待在途段完成（残段仍提交为 turn）。"""
    agent = _BlockableAgent()
    mgr, emitted, bus_posts = make_manager(agent=agent)
    await mgr.start_realtime("rt_1", None)
    for _ in range(30):
        await mgr.feed_pcm(_loud(1600))
    await mgr.feed_pcm(_sil(1600))
    await asyncio.sleep(0.15)
    await mgr.feed_pcm(_sil(1600))
    await asyncio.sleep(0.05)
    stop_task = asyncio.create_task(mgr.stop_session("rt_1"))
    await asyncio.sleep(0.05)               # stop 卡在 drain
    agent.released.set()
    await asyncio.wait_for(stop_task, timeout=2)
    assert any(s.get("type") == "voice_session.turn" for s in emitted)
    assert any(s.get("type") == "voice_session.ended" for s in emitted)


@pytest.mark.asyncio
async def test_cancel_then_restart_epoch_isolation():
    """cancel → 立即重启新会话，旧会话在途段不落入新会话首轮（B1 epoch 隔离）。"""
    agent = _BlockableAgent()
    mgr, emitted, _ = make_manager(agent=agent)
    await mgr.start_realtime("rt_1", None)
    for _ in range(30):
        await mgr.feed_pcm(_loud(1600))
    await mgr.feed_pcm(_sil(1600))
    await asyncio.sleep(0.15)
    await mgr.feed_pcm(_sil(1600))
    await asyncio.sleep(0.05)
    await mgr.cancel_session("rt_1")        # teardown → epoch += 1
    await mgr.start_realtime("rt_2", None)  # 新会话 epoch 已变
    agent.released.set()                    # 旧会话在途段完成
    await asyncio.sleep(0.1)
    # 新会话无任何 turn（旧段被 epoch 隔离丢弃）
    assert not any(s.get("type") == "voice_session.turn" for s in emitted)


@pytest.mark.asyncio
async def test_stop_dictation_keeps_final_segment():
    """stop_dictation 等待最后一段提交进 _dict_text（F1）。"""
    agent = _BlockableAgent()
    mgr, emitted, _ = make_manager(agent=agent)
    await mgr.start_dictation("req_d")
    for _ in range(30):
        await mgr.feed_pcm(_loud(1600))
    await mgr.feed_pcm(_sil(1600))
    await asyncio.sleep(0.15)
    await mgr.feed_pcm(_sil(1600))
    await asyncio.sleep(0.05)
    stop_task = asyncio.create_task(mgr.stop_dictation("req_d"))
    await asyncio.sleep(0.05)
    agent.released.set()
    await asyncio.wait_for(stop_task, timeout=2)
    finals = [s for s in emitted if s.get("type") == "dictation.final"]
    assert finals and finals[0]["text"] == "在途段文本"


class _TwoReleaseSTTAgent(FakeAgent):
    """两段 STT 分别放行、各自写不同 emotion——验证 F3 锁下情绪归属 + 提交序。"""

    class _Backend:
        def __init__(self, agent):
            self._agent = agent
            self.calls = 0
            self.release_a = asyncio.Event()
            self.release_b = asyncio.Event()
        async def transcribe(self, wav):
            self.calls += 1
            if self.calls == 1:
                await self.release_a.wait()
                self._agent.stt_emotion_state["emotion"] = "HAPPY"
                await asyncio.sleep(0.05)   # 模拟真实 stt_transcribe 写 emotion 后的事件循环切换
                return "第一段"
            await self.release_b.wait()
            self._agent.stt_emotion_state["emotion"] = "SAD"
            await asyncio.sleep(0.05)
            return "第二段"

    def __init__(self):
        super().__init__()
        self.stt_emotion_state = {"emotion": "NEUTRAL", "events": []}
        self._two = self._Backend(self)
        self.stt = self._two

    async def stt_transcribe(self, wav):
        return await self._two.transcribe(wav)


@pytest.mark.asyncio
async def test_concurrent_segments_order_and_emotion():
    """F3 + 提交序：并发两段反序放行 → 按 seq 顺序提交（turn_id 单调）+ 各自 emotion 取自本段。

    锁缺失时：B 先完成写 SAD，A 的捕获会读到 SAD（张冠李戴）→ 测试红。
    """
    agent = _TwoReleaseSTTAgent()
    mgr, emitted, bus_posts = make_manager(agent=agent)
    await mgr.start_realtime("rt_1", None)
    # 段1 派发 seq0（阻塞 release_a）
    for _ in range(30):
        await mgr.feed_pcm(_loud(1600))
    await mgr.feed_pcm(_sil(1600))
    await asyncio.sleep(0.15)
    await mgr.feed_pcm(_sil(1600))
    await asyncio.sleep(0.05)
    # 段2 派发 seq1（阻塞 release_b）——两段并发在途
    for _ in range(30):
        await mgr.feed_pcm(_loud(1600))
    await mgr.feed_pcm(_sil(1600))
    await asyncio.sleep(0.15)
    await mgr.feed_pcm(_sil(1600))
    await asyncio.sleep(0.05)
    assert len(mgr._stt_tasks) >= 2
    # 制造竞态窗口：先 release_a（A 写 HAPPY 并 sleep），B 在 A sleep 期间写 SAD——
    # 锁缺失时 A 的捕获读到 B 的 SAD（张冠李戴）→ 断言 [HAPPY, SAD] 红。
    agent._two.release_a.set()
    await asyncio.sleep(0.02)   # A 进入「写 HAPPY + sleep」窗口
    agent._two.release_b.set()
    await asyncio.sleep(0.2)    # 两段完成
    turns = [s for s in emitted if s.get("type") == "voice_session.turn"]
    assert [t["turnId"] for t in turns] == [1, 2]       # 提交锁 + seq 顺序
    assert [p.emotion for p in bus_posts] == ["HAPPY", "SAD"]  # 各自本段 emotion（F3 锁）


@pytest.mark.asyncio
async def test_realtime_segment_records_emitted_message_ids():
    """D1 voice 侧：REALTIME 段提交后 emitted_message_ids 含该消息 ID（断连归属数据源）。"""
    agent = FakeAgent()
    agent._last_text = "实时段"
    mgr, emitted, _ = make_manager(agent=agent)
    await mgr.start_realtime("rt_1", None)
    for _ in range(30):
        await mgr.feed_pcm(_loud(1600))
    await mgr.feed_pcm(_sil(1600))
    await asyncio.sleep(0.15)
    await mgr.feed_pcm(_sil(1600))
    await asyncio.sleep(0.1)
    turns = [s for s in emitted if s.get("type") == "voice_session.turn"]
    assert turns, "REALTIME 段应提交 turn"
    assert mgr.emitted_message_ids == {turns[0]["messageId"]}


@pytest.mark.asyncio
async def test_cancel_during_commit_discards_segment():
    """残段提交途中 cancel（partial emit 期间 teardown）→ 不发 turn / 不 post UserInputData。

    review Important 窗口：_on_segment 的 partial emit await 期间 cancel → teardown
    取消在途任务 + epoch 复检丢弃残段（spec §5.2「丢弃残段」）。
    """
    agent = _BlockableAgent()
    emitted: list = []
    partial_gate = asyncio.Event()
    partial_block = asyncio.Event()

    async def slow_emit(d):
        if d.get("type") == "voice_session.partial":
            partial_gate.set()
            await partial_block.wait()     # 卡住提交——留出 cancel 窗口
        emitted.append(d)

    bus_posts: list = []
    agent.bus = type("Bus", (), {"post": lambda self, d: bus_posts.append(d)})()
    agent.idle = type("Idle", (), {"touch": lambda self: None})()
    mgr = VoiceSessionManager(agent=agent, emit=slow_emit, silence_duration=0.1)
    await mgr.start_realtime("rt_1", None)
    for _ in range(30):
        await mgr.feed_pcm(_loud(1600))
    await mgr.feed_pcm(_sil(1600))
    await asyncio.sleep(0.15)
    await mgr.feed_pcm(_sil(1600))
    await asyncio.sleep(0.05)
    agent.released.set()                     # STT 完成 → 提交 → partial emit 卡在 slow_emit
    await asyncio.wait_for(partial_gate.wait(), timeout=2)
    await mgr.cancel_session("rt_1")         # partial emit 期间 cancel → teardown 取消在途任务
    partial_block.set()                      # 放行（任务已被取消，不影响断言）
    await asyncio.sleep(0.1)
    assert not any(p for p in bus_posts)     # 残段不 post UserInputData
    assert not any(s.get("type") == "voice_session.turn" for s in emitted)


@pytest.mark.asyncio
async def test_error_frame_carries_retryable():
    """_emit_error(..., True) → error 帧带 retryable=true。"""
    mgr, emitted, _ = make_manager()
    await mgr._emit_error("STT_UNAVAILABLE", "STT 已关闭，录音丢弃", True)
    assert any(e.get("type") == "error" and e.get("code") == "STT_UNAVAILABLE"
               and e.get("retryable") is True for e in emitted)


# ── 9C-1 输入音频契约（Task 7）：descriptor 校验 + chunk 上限 + 段整帧对齐 ──


def _desc(codec: str = "pcm_s16le", rate: int = 16000, channels: int = 1):
    from frontend_contract.ws_message import AudioInputDescriptor
    return AudioInputDescriptor(codec=codec, sampleRate=rate, channels=channels)


@pytest.mark.asyncio
async def test_recording_start_bad_descriptor_rejected():
    """voice.record.start 带 audio={codec:opus} → CONTRACT_VALIDATION，不建会话。"""
    mgr, _, _ = make_manager()
    with pytest.raises(VoiceSessionError) as exc:
        await mgr.start_recording("rec_1", audio_descriptor=_desc(codec="opus"))
    assert exc.value.code == "CONTRACT_VALIDATION"
    assert mgr.has_active_session() is False


@pytest.mark.asyncio
async def test_realtime_bad_descriptor_rejected():
    """voice_session.start 48k/stereo → CONTRACT_VALIDATION，不建会话。"""
    mgr, _, _ = make_manager()
    with pytest.raises(VoiceSessionError) as exc:
        await mgr.start_realtime("vs_1", None,
                                 audio_descriptor=_desc(rate=48000, channels=2))
    assert exc.value.code == "CONTRACT_VALIDATION"
    assert mgr.has_active_session() is False


@pytest.mark.asyncio
async def test_dictation_start_bad_descriptor_rejected():
    """dictation.start 带非法 descriptor → CONTRACT_VALIDATION。"""
    mgr, _, _ = make_manager()
    with pytest.raises(VoiceSessionError) as exc:
        await mgr.start_dictation("req_d", audio_descriptor=_desc(rate=8000))
    assert exc.value.code == "CONTRACT_VALIDATION"
    assert mgr.has_active_session() is False


@pytest.mark.asyncio
async def test_descriptor_omitted_default_format():
    """缺省 audio → 默认 16k/mono 正常（向后兼容），三模式均可启动。"""
    # RECORDING：缺省 descriptor → 正常启动
    mgr, _, _ = make_manager()
    await mgr.start_recording("rec_1")
    assert mgr.has_active_session() is True
    # REALTIME：缺省 → 正常启动
    mgr2, _, _ = make_manager()
    await mgr2.start_realtime("vs_1", None)
    assert mgr2.has_active_session() is True
    await mgr2.close()
    # DICTATION：缺省 → 正常启动
    mgr3, _, _ = make_manager()
    await mgr3.start_dictation("req_d")
    assert mgr3.has_active_session() is True
    await mgr3.cancel_dictation("req_d")


@pytest.mark.asyncio
async def test_chunk_too_large_rejected():
    """feed_pcm 超 MAX_CHUNK_BYTES → CONTRACT_VALIDATION + 完整会话拆除 + ended 收尾。"""
    mgr, emitted, _ = make_manager()
    await mgr.start_realtime("vs_1", None)
    await mgr.feed_pcm(b"\x00" * (MAX_CHUNK_BYTES + 1))
    errs = [e for e in emitted if e.get("type") == "error"]
    assert errs and errs[-1].get("code") == "CONTRACT_VALIDATION"
    assert mgr.has_active_session() is False                    # 会话已拆除
    assert mgr._buffer is None and mgr._session_id is None      # 完整拆除（非 _reset_recording）
    ends = [e for e in emitted if e.get("type") == "voice_session.ended"]
    assert ends, "超限应发 voice_session.ended 收尾"
    await mgr.close()


@pytest.mark.asyncio
async def test_chunk_too_large_recording_terminates():
    """RECORDING 态超限 chunk → 同样 CONTRACT_VALIDATION + 终止，拆除后可重启。"""
    mgr, emitted, _ = make_manager()
    await mgr.start_recording("rec_1")
    await mgr.feed_pcm(b"\x00" * (MAX_CHUNK_BYTES + 1))
    errs = [e for e in emitted if e.get("type") == "error"]
    assert errs and errs[-1].get("code") == "CONTRACT_VALIDATION"
    assert mgr.has_active_session() is False
    await mgr.start_recording("rec_2")                          # mode 已复位，可重启
    assert mgr.has_active_session() is True


@pytest.mark.asyncio
async def test_arbitrary_length_chunk_accepted():
    """非 640 倍数 chunk 喂入不报错（feed 不强制整帧，VAD 只扫整帧）。"""
    mgr, emitted, _ = make_manager()
    await mgr.start_realtime("vs_1", None)
    await mgr.feed_pcm(b"\x00" * 100)     # 非 640 倍数
    await mgr.feed_pcm(_loud(1234))       # 1234 样本 = 2468B，非对齐
    await mgr.feed_pcm(_sil(700))         # 700B = 640 + 60 尾残
    assert mgr.has_active_session() is True   # 无报错、无终止
    assert not any(e.get("type") == "error" for e in emitted)
    await mgr.close()


@pytest.mark.asyncio
async def test_record_stop_drops_tail():
    """RECORDING stop：_rec_pcm 尾部 < 640B 丢弃，WAV 长度整帧倍数。"""
    captured: list[bytes] = []
    agent = FakeAgent()
    original = agent.stt_transcribe

    async def capturing(wav):
        captured.append(wav)
        return await original(wav)

    agent.stt_transcribe = capturing
    mgr, emitted, _ = make_manager(agent=agent)
    await mgr.start_recording("rec_1")
    for _ in range(3):
        await mgr.feed_pcm(b"\x00" * 2000)      # 3×2000B = 6000B = 9 帧 + 240B 尾残
    agent._last_text = "对齐"
    await mgr.stop_recording("rec_1")
    await asyncio.sleep(0.05)
    assert captured, "stop 应调用 stt_transcribe"
    import wave
    from io import BytesIO
    with wave.open(BytesIO(captured[0]), "rb") as wf:
        pcm_len = wf.getnframes() * wf.getsampwidth() * wf.getnchannels()
    assert pcm_len % 640 == 0
    assert pcm_len == 6000 // 640 * 640           # 尾部 240B 被截断


@pytest.mark.asyncio
async def test_fixture_feeds_all_three_modes():
    """真实 PCM fixture（1s 16k/mono/s16le）全链路喂入 recording/realtime/dictation。"""
    import pathlib
    pcm_path = pathlib.Path(__file__).parent / "fixtures" / "sample_16k_mono.pcm"
    data = pcm_path.read_bytes()
    assert len(data) == 32000
    # RECORDING：全段缓冲 → stop → 转写
    agent = FakeAgent()
    agent._last_text = "录音识别"
    mgr, emitted, _ = make_manager(agent=agent)
    await mgr.start_recording("rec_1")
    await mgr.feed_pcm(data)
    await mgr.stop_recording("rec_1")
    await asyncio.sleep(0.05)
    assert any(e.get("type") == "voice.transcript" for e in emitted)
    # REALTIME：VAD 分段 → partial
    agent2 = FakeAgent()
    agent2._last_text = "实时识别"
    mgr2, emitted2, _ = make_manager(agent=agent2)
    await mgr2.start_realtime("vs_1", None)
    await mgr2.feed_pcm(data)
    await mgr2.feed_pcm(_sil(1600))
    await asyncio.sleep(0.15)
    await mgr2.feed_pcm(_sil(1600))
    await asyncio.sleep(0.1)
    assert any(e.get("type") == "voice_session.partial" for e in emitted2)
    await mgr2.close()
    # DICTATION：VAD 分段 → partial + final
    agent3 = FakeAgent()
    agent3._last_text = "听写识别"
    mgr3, emitted3, _ = make_manager(agent=agent3)
    await mgr3.start_dictation("req_d")
    await mgr3.feed_pcm(data)
    await mgr3.feed_pcm(_sil(1600))
    await asyncio.sleep(0.15)
    await mgr3.feed_pcm(_sil(1600))
    await asyncio.sleep(0.1)
    assert any(e.get("type") == "dictation.partial" for e in emitted3)
    await mgr3.stop_dictation("req_d")
    finals = [e for e in emitted3 if e.get("type") == "dictation.final"]
    assert finals and finals[0]["text"] == "听写识别"
