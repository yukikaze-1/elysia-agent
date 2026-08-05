"""VoiceSessionManager — 三种语音模式状态机（BE-060/061/062）。

- RECORDING：全段 PCM 缓冲，stop → 预转写 → PENDING → commit/discard。
- REALTIME：内部 AudioBuffer VAD 分段 → partial 字幕 + 每段自动提交轮次。
- DICTATION：内部 AudioBuffer VAD 分段 → partial 累积 → final 只回文字（零消息副作用）。
内部 buffer 随会话创建/销毁（每会话一个，on_interrupt 仅 REALTIME 接线）。
"""

import asyncio
import enum
import logging
import time
from typing import TYPE_CHECKING, Awaitable, Callable

from core.input.audio_buffer import AudioBuffer, pcm_to_wav
from core.utils import new_message_id

if TYPE_CHECKING:
    from core.agent import Agent

logger = logging.getLogger(__name__)

# spec §4.2/§5.3 常量
MAX_RECORDING_SECONDS = 600
MAX_RECORDING_BYTES = 16000 * 2 * MAX_RECORDING_SECONDS   # ≈18.3MiB
VOICE_SESSION_IDLE_TIMEOUT = 300.0
STT_TIMEOUT = 30.0
ACK_INTERVAL = 1.0
IDLE_CHECK_INTERVAL = 5.0

# 错误码目录（spec §10）
ERR_CONFLICT = "VOICE_SESSION_CONFLICT"
ERR_SESSION_NOT_FOUND = "VOICE_SESSION_NOT_FOUND"
ERR_RECORDING_NOT_FOUND = "VOICE_RECORDING_NOT_FOUND"
ERR_RECORDING_TOO_LONG = "VOICE_RECORDING_TOO_LONG"
ERR_DICTATION_NOT_FOUND = "DICTATION_NOT_FOUND"
ERR_STT_UNAVAILABLE = "STT_UNAVAILABLE"
ERR_CONTRACT = "CONTRACT_VALIDATION"


class VoiceMode(enum.Enum):
    IDLE = "idle"
    RECORDING = "recording"
    REALTIME = "realtime"
    DICTATION = "dictation"


class _RecordingState(enum.Enum):
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    PENDING = "pending"


class VoiceSessionError(Exception):
    """语音会话错误（code 用统一错误目录）。"""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


Emit = Callable[[dict], Awaitable[None]]


class VoiceSessionManager:
    """每连接一个：三模式语音状态机 + realtime/dictation VAD buffer 生命周期。"""

    def __init__(self, *, agent: "Agent", emit: Emit,
                 on_vad_interrupt: Callable[[], Awaitable[None]] | None = None,
                 silence_duration: float = 1.5):
        self._agent = agent
        self._emit = emit
        self._on_vad_interrupt = on_vad_interrupt
        self._silence_duration = silence_duration
        self._mode = VoiceMode.IDLE
        self._emotion_snapshot = None   # dictation 前 stt_emotion_state 快照（Task 6）
        # RECORDING
        self._recording_id: str | None = None
        self._rec_state = _RecordingState.IDLE
        self._rec_pcm: list[bytes] = []
        self._rec_bytes = 0
        self._pending_text = ""
        self._pending_emotion = ""
        self._pending_events: list[str] = []
        self._in_flight_stt: asyncio.Task | None = None
        # REALTIME / DICTATION
        self._session_id: str | None = None
        self._conv_id: str | None = None
        self._buffer: AudioBuffer | None = None
        self._paused = False
        self._turn_id = 0
        self._segment_index = 0
        self._received_frames = 0
        self._last_audio_at = 0.0
        self._ack_task: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None
        self._dict_request_id = ""
        self._dict_text: list[str] = []

    # ---- 查询 ----

    def mode(self) -> VoiceMode:
        return self._mode

    def has_active_session(self) -> bool:
        return self._mode != VoiceMode.IDLE

    # ---- 通用 ----

    def _stt_available(self) -> bool:
        return (self._agent.stt is not None
                and getattr(self._agent, "stt_enabled", False))

    async def _emit_error(self, code: str, message: str, retryable: bool = False) -> None:
        from frontend_contract import ErrorMessage
        await self._emit(ErrorMessage(message=message, code=code).wire_dict())

    def _reset_recording(self) -> None:
        self._recording_id = None
        self._rec_state = _RecordingState.IDLE
        self._rec_pcm = []
        self._rec_bytes = 0
        self._pending_text = ""
        self._pending_emotion = ""
        self._pending_events = []
        if self._in_flight_stt is not None and not self._in_flight_stt.done():
            self._in_flight_stt.cancel()
        self._in_flight_stt = None
        self._mode = VoiceMode.IDLE  # 必须复位——否则 RECORDING 一次后永久占用（review #1）

    def _teardown_session(self) -> None:
        if self._ack_task is not None and not self._ack_task.done():
            self._ack_task.cancel()
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        self._ack_task = None
        self._idle_task = None
        self._buffer = None
        self._session_id = None
        self._conv_id = None
        self._paused = False
        self._turn_id = 0
        self._segment_index = 0
        self._received_frames = 0
        self._dict_request_id = ""
        self._dict_text = []
        self._mode = VoiceMode.IDLE

    def _restore_emotion(self) -> None:
        """恢复共享 stt_emotion_state——原地 clear/update（不触发只读 property setter）。"""
        if self._emotion_snapshot is not None:
            try:
                state = getattr(self._agent, "stt_emotion_state", None)
                if isinstance(state, dict):
                    state.clear()
                    state.update(self._emotion_snapshot)
            except Exception:
                logger.debug("恢复 stt_emotion_state 失败", exc_info=True)
        self._emotion_snapshot = None

    # ---- RECORDING（BE-060） ----

    async def start_recording(self, recording_id: str) -> None:
        if self._mode != VoiceMode.IDLE:
            raise VoiceSessionError(ERR_CONFLICT, "已有活动语音会话")
        if not self._stt_available():
            raise VoiceSessionError(ERR_STT_UNAVAILABLE, "STT 不可用", True)
        self._mode = VoiceMode.RECORDING
        self._recording_id = recording_id
        self._rec_state = _RecordingState.RECORDING
        self._rec_pcm = []
        self._rec_bytes = 0

    async def stop_recording(self, recording_id: str) -> None:
        if self._mode != VoiceMode.RECORDING or self._recording_id != recording_id:
            raise VoiceSessionError(ERR_RECORDING_NOT_FOUND, "无活动录音")
        if self._rec_state == _RecordingState.TRANSCRIBING:
            raise VoiceSessionError(ERR_CONTRACT, "STT 转写进行中")
        if self._rec_state == _RecordingState.PENDING:
            raise VoiceSessionError(ERR_CONTRACT, "已转写完成，请 commit 或 discard")
        self._rec_state = _RecordingState.TRANSCRIBING
        wav = pcm_to_wav(b"".join(self._rec_pcm))
        self._rec_pcm = []
        self._rec_bytes = 0
        self._in_flight_stt = asyncio.create_task(
            self._run_recording_stt(recording_id, wav), name="rec-stt")

    async def _run_recording_stt(self, recording_id: str, wav: bytes) -> None:
        try:
            if not getattr(self._agent, "stt_enabled", False):
                await self._emit_error(ERR_STT_UNAVAILABLE, "STT 已关闭，录音丢弃", True)
                self._reset_recording()
                return
            text = await asyncio.wait_for(
                self._agent.stt_transcribe(wav), timeout=STT_TIMEOUT)
            text = text.strip() if text else ""
            if self._rec_state != _RecordingState.TRANSCRIBING:
                return  # discard 已取消
            if not text:
                self._reset_recording()  # 空转写：不进 PENDING（spec §4.2）
                return
            # 快照 emotion/events（立即读，防共享状态被覆盖，spec §4.2/S5）
            es = getattr(self._agent, "stt_emotion_state", None) or {}
            self._pending_text = text
            self._pending_emotion = es.get("emotion", "") or ""
            self._pending_events = es.get("events", []) or []
            self._rec_state = _RecordingState.PENDING
            from frontend_contract import VoiceTranscriptMessage
            await self._emit(VoiceTranscriptMessage(
                recordingId=recording_id, text=text,
                emotion=self._pending_emotion or None).wire_dict())
        except asyncio.TimeoutError:
            await self._emit_error(ERR_STT_UNAVAILABLE, "STT 转写超时", True)
            self._reset_recording()
        except Exception:
            logger.warning("录音转写失败", exc_info=True)
            await self._emit_error(ERR_STT_UNAVAILABLE, "STT 转写失败", True)
            self._reset_recording()

    def commit_recording(self, recording_id: str, text: str) -> tuple[str, str, list[str]]:
        """校验 PENDING + 文本一致，返回 (pending_text, emotion, events)；清态。
        ws_handler 在幂等查重之后调用（spec §3.3 NEW-A 顺序）。"""
        if self._mode != VoiceMode.RECORDING or self._recording_id != recording_id:
            raise VoiceSessionError(ERR_RECORDING_NOT_FOUND, "无待提交录音")
        if self._rec_state != _RecordingState.PENDING:
            raise VoiceSessionError(ERR_CONTRACT, "录音未就绪（须先 stop 并收到字幕）")
        if text != self._pending_text:
            raise VoiceSessionError(ERR_CONTRACT, "提交文本与字幕不一致")
        payload = (self._pending_text, self._pending_emotion, self._pending_events)
        self._reset_recording()
        return payload

    async def discard_recording(self, recording_id: str) -> None:
        if self._mode != VoiceMode.RECORDING or self._recording_id != recording_id:
            raise VoiceSessionError(ERR_RECORDING_NOT_FOUND, "无活动录音")
        if self._rec_state == _RecordingState.RECORDING:
            raise VoiceSessionError(ERR_CONTRACT, "录音中，先 stop 再 discard")
        if self._in_flight_stt is not None and not self._in_flight_stt.done():
            self._in_flight_stt.cancel()
        self._reset_recording()

    # ---- REALTIME（BE-061） ----

    async def start_realtime(self, session_id: str, conversation_id: str | None) -> None:
        if self._mode != VoiceMode.IDLE:
            raise VoiceSessionError(ERR_CONFLICT, "已有活动语音会话")
        if not self._stt_available():
            raise VoiceSessionError(ERR_STT_UNAVAILABLE, "STT 不可用", True)
        target = await self._resolve_conversation(conversation_id)
        self._mode = VoiceMode.REALTIME
        self._session_id = session_id
        self._conv_id = target
        self._turn_id = 0
        self._segment_index = 0
        self._received_frames = 0
        self._last_audio_at = time.monotonic()
        self._buffer = AudioBuffer(
            stt_transcribe=self._agent.stt_transcribe,
            aggressiveness=3,
            silence_duration=self._silence_duration,
            on_interrupt=self._on_vad_interrupt,
            on_transcription=self._on_segment,
        )
        self._ack_task = asyncio.create_task(self._ack_loop(session_id))
        self._idle_task = asyncio.create_task(self._idle_check_loop(session_id))
        from frontend_contract import VoiceSessionStartedMessage
        await self._emit(VoiceSessionStartedMessage(
            sessionId=session_id, conversationId=target).wire_dict())

    async def pause_session(self, session_id: str) -> None:
        self._require_session(session_id)
        if self._paused:
            return  # 幂等忽略（spec §5.3）
        self._paused = True
        if self._buffer:
            self._buffer.reset()  # 清分段状态（spec §5.2 NEW-C）

    async def resume_session(self, session_id: str) -> None:
        self._require_session(session_id)
        if not self._paused:
            return
        self._paused = False
        self._last_audio_at = time.monotonic()

    async def stop_session(self, session_id: str) -> None:
        self._require_session(session_id)
        if self._buffer:
            await self._buffer.flush()  # 残段处理（在途段等待完成，spec §5.2）
        self._teardown_session()
        from frontend_contract import VoiceSessionEndedMessage
        await self._emit(VoiceSessionEndedMessage(
            sessionId=session_id, reason="stopped").wire_dict())

    async def cancel_session(self, session_id: str) -> None:
        self._require_session(session_id)
        # 取消在途轮次生成（spec §5.2）：generation 未完结才发中断
        if not getattr(self._agent.status, "is_completely_idle", True):
            from core.monitoring.event_bus import EventPriority
            from core.schema.event import InterruptData
            self._agent.bus.post(InterruptData(source="voice_cancel"),
                                 priority=EventPriority.INTERRUPT)
        self._teardown_session()
        from frontend_contract import VoiceSessionEndedMessage
        await self._emit(VoiceSessionEndedMessage(
            sessionId=session_id, reason="cancelled").wire_dict())

    # ---- DICTATION（BE-062） ----

    async def start_dictation(self, client_request_id: str) -> None:
        if self._mode != VoiceMode.IDLE:
            raise VoiceSessionError(ERR_CONFLICT, "已有活动语音会话")
        if not self._stt_available():
            raise VoiceSessionError(ERR_STT_UNAVAILABLE, "STT 不可用", True)
        # 快照共享 stt_emotion_state（dict 复制）——dictation 转写会原地改它，
        # 结束后恢复（spec §7.2 S5）；Agent.stt_emotion_state 是只读 property，
        # 不能整对象赋值，须复制 + 原地 clear/update（review #8）
        es = getattr(self._agent, "stt_emotion_state", None)
        self._emotion_snapshot = dict(es) if isinstance(es, dict) else None
        self._mode = VoiceMode.DICTATION
        self._session_id = "dictation"
        self._dict_request_id = client_request_id
        self._dict_text = []
        self._last_audio_at = time.monotonic()
        self._buffer = AudioBuffer(
            stt_transcribe=self._agent.stt_transcribe,
            aggressiveness=3,
            silence_duration=self._silence_duration,
            on_interrupt=None,  # DICTATION 不接（spec §3.2 NEW-K）
            on_transcription=self._on_segment,
        )

    async def stop_dictation(self, client_request_id: str) -> None:
        if self._mode != VoiceMode.DICTATION:
            raise VoiceSessionError(ERR_DICTATION_NOT_FOUND, "无活动转写")
        if self._buffer:
            await self._buffer.flush()
        text = "".join(self._dict_text)
        self._teardown_session()
        self._restore_emotion()
        from frontend_contract import DictationFinalMessage
        await self._emit(DictationFinalMessage(
            clientRequestId=client_request_id, text=text).wire_dict())

    async def cancel_dictation(self, client_request_id: str) -> None:
        if self._mode != VoiceMode.DICTATION:
            raise VoiceSessionError(ERR_DICTATION_NOT_FOUND, "无活动转写")
        self._teardown_session()
        self._restore_emotion()
        from frontend_contract import DictationCancelledMessage
        await self._emit(DictationCancelledMessage(
            clientRequestId=client_request_id).wire_dict())

    # ---- feed / 回调 / 后台 ----

    async def feed_pcm(self, chunk: bytes) -> None:
        if self._mode == VoiceMode.RECORDING and self._rec_state == _RecordingState.RECORDING:
            self._rec_pcm.append(chunk)
            self._rec_bytes += len(chunk)
            if self._rec_bytes > MAX_RECORDING_BYTES:
                await self._emit_error(ERR_RECORDING_TOO_LONG, "录音过长，已丢弃")
                self._reset_recording()
            return
        if self._mode in (VoiceMode.REALTIME, VoiceMode.DICTATION):
            if self._paused or self._buffer is None:
                return
            if getattr(self._agent.status, "is_speaking", False):
                self._last_audio_at = time.monotonic()  # 丢帧也刷新 idle 计时（review #19）
                return  # deafen 等价（spec §3.2 NEW-B）
            if not getattr(self._agent, "stt_enabled", False):
                await self._end_session_stt_disabled()  # 检测点（spec §5.3 NEW-D）
                return
            self._received_frames += 1
            self._last_audio_at = time.monotonic()
            await self._buffer.feed(chunk)
        # IDLE / recording 非 RECORDING 态 → 忽略

    async def _on_segment(self, text: str) -> None:
        text = text.strip() if text else ""
        if not text:
            return
        if self._mode == VoiceMode.REALTIME:
            self._turn_id += 1
            seg = self._segment_index
            self._segment_index += 1
            from frontend_contract import VoiceSessionPartialMessage, VoiceSessionTurnMessage
            await self._emit(VoiceSessionPartialMessage(
                sessionId=self._session_id, turnId=self._turn_id,
                segmentIndex=seg, text=text).wire_dict())
            # 自动提交轮次（spec §5.3）：预分配 messageId + post UserInputData
            es = getattr(self._agent, "stt_emotion_state", None) or {}
            message_id = new_message_id()
            self._agent.idle.touch()
            self._agent.bus.post(
                self._build_user_input(text, es, message_id))
            await self._emit(VoiceSessionTurnMessage(
                sessionId=self._session_id, turnId=self._turn_id,
                messageId=message_id, conversationId=self._conv_id, text=text).wire_dict())
        elif self._mode == VoiceMode.DICTATION:
            from frontend_contract import DictationPartialMessage
            self._dict_text.append(text)
            await self._emit(DictationPartialMessage(text=text).wire_dict())

    def _build_user_input(self, text: str, es: dict, message_id: str):
        from core.schema.event import UserInputData
        return UserInputData(
            text=text, user_name=self._agent.user_name,
            emotion=es.get("emotion", "") or "",
            events=es.get("events", []) or [],
            request_id=self._agent.next_request_id(),
            message_id=message_id, protocol_version=2,
            conversation_id=self._conv_id)

    async def _resolve_conversation(self, conversation_id: str | None) -> str:
        try:
            target = await self._agent.resolve_session(conversation_id, 2)
            return str(target.session_id)
        except Exception:
            logger.warning("voice resolve_session 失败，回退", exc_info=True)
            return conversation_id or "default"

    def _require_session(self, session_id: str) -> None:
        # 仅 REALTIME 接受 voice_session.* 消息（review #18：dictation 无这些消息，
        # 防客户端用 voice_session.stop{sessionId:"dictation"} 误结束转写）
        if self._mode != VoiceMode.REALTIME or self._session_id != session_id:
            raise VoiceSessionError(ERR_SESSION_NOT_FOUND, "会话不存在")

    async def notify_stt_disabled(self) -> None:
        """set_service('stt', False) 成功后由 ws_handler 调用（spec §3.2 NEW-X/XA）。"""
        if self._mode == VoiceMode.REALTIME:
            sid = self._session_id  # teardown 前缓存（review #2）
            self._teardown_session()
            from frontend_contract import VoiceSessionEndedMessage
            await self._emit(VoiceSessionEndedMessage(
                sessionId=sid or "", reason="stt_disabled").wire_dict())
        elif self._mode == VoiceMode.DICTATION:
            rid = self._dict_request_id  # teardown 前缓存
            self._teardown_session()
            self._restore_emotion()  # STT 中途关闭结束转写，同样恢复（review NEW-3）
            from frontend_contract import DictationCancelledMessage
            await self._emit(DictationCancelledMessage(
                clientRequestId=rid).wire_dict())
        # RECORDING 不处理（spec §3.2 NEW-XA）

    async def _end_session_stt_disabled(self) -> None:
        if self._mode == VoiceMode.REALTIME:
            await self.notify_stt_disabled()
        elif self._mode == VoiceMode.DICTATION:
            await self.notify_stt_disabled()

    async def close(self) -> None:
        """连接断开清理（spec §3.2 NEW-AA/XB/XC）——不发 InterruptData（复用 finally）。"""
        if self._in_flight_stt is not None and not self._in_flight_stt.done():
            self._in_flight_stt.cancel()
        self._teardown_session()
        self._restore_emotion()
        self._reset_recording()

    # ---- 后台循环 ----

    async def _ack_loop(self, session_id: str) -> None:
        try:
            while True:
                await asyncio.sleep(ACK_INTERVAL)
                from frontend_contract import VoiceSessionAckMessage
                await self._emit(VoiceSessionAckMessage(
                    sessionId=session_id, seq=self._received_frames).wire_dict())
        except asyncio.CancelledError:
            raise

    async def _idle_check_loop(self, session_id: str) -> None:
        try:
            while True:
                await asyncio.sleep(IDLE_CHECK_INTERVAL)
                if not self._paused and time.monotonic() - self._last_audio_at \
                        > VOICE_SESSION_IDLE_TIMEOUT:
                    self._teardown_session()
                    from frontend_contract import VoiceSessionEndedMessage
                    await self._emit(VoiceSessionEndedMessage(
                        sessionId=session_id, reason="timeout").wire_dict())
                    return
        except asyncio.CancelledError:
            raise
