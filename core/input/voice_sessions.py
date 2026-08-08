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
from core.schema.types import MessageId, SessionId
from core.utils import get_request_id, new_message_id

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
        # RECORDING 状态
        self._recording_id: str | None = None
        self._rec_state = _RecordingState.IDLE
        self._rec_pcm: list[bytes] = []
        self._rec_bytes = 0
        self._pending_text = ""
        self._pending_emotion = ""
        self._pending_events: list[str] = []
        self._in_flight_stt: asyncio.Task | None = None
        # REALTIME / DICTATION 状态
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
        # D3（spec §3.3）：STT 后台派发状态——seq 单调、epoch 代际、pending 缓冲、任务集合、双锁
        self._stt_seq = 0
        self._stt_epoch = 0                 # 会话代际——teardown 时 +=1，绝不归零（B1）
        self._stt_pending: dict[int, tuple[str, str, list[str]]] = {}
        self._stt_commit_seq = 0
        self._stt_tasks: set[asyncio.Task] = set()
        self._stt_stt_lock = asyncio.Lock()
        self._stt_commit_lock = asyncio.Lock()
        self._emitted_message_ids: set[str] = set()   # D1 归属（Task 9 消费）

    # ---- 查询 ----

    def mode(self) -> VoiceMode:
        return self._mode

    def has_active_session(self) -> bool:
        return self._mode != VoiceMode.IDLE

    # ---- 通用 ----

    def _stt_available(self) -> bool:
        return (self._agent.stt is not None
                and getattr(self._agent, "stt_enabled", False))

    async def _transcribe_no_emotion(self, wav: bytes) -> str:
        """转写但不写共享 stt_emotion_state（D6：dictation 情绪隔离）。

        dictation 只回文字，本就不该触碰 agent 级情绪——隔离后无需快照/恢复，
        也不抹掉其他连接在 dictation 窗口内写入的 emotion/events。
        """
        backend = getattr(self._agent, "stt", None)
        if backend is None:
            return ""
        return await backend.transcribe(wav)

    async def _emit_error(self, code: str, message: str, retryable: bool = False) -> None:
        from frontend_contract import ErrorMessage
        # 批次 7 关联 ID：当前 ContextVar request_id（来源 UserInputData.request_id），
        # 有则带、无（"-"）则 None 缺省省略
        rid = get_request_id()
        await self._emit(ErrorMessage(
            message=message, code=code, retryable=retryable,
            requestId=rid if rid and rid != "-" else None,
        ).wire_dict())

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
        # 跳过当前任务自取消（review NEW-1）——_idle_check_loop 超时分支会调用本方法，
        # 自我 cancel 置 _must_cancel，在下一次挂起点投递 CancelledError，中断随后的
        # ended{timeout} 发送；stop/cancel/notify_stt_disabled/close 均从 ws_handler
        # 协程调用，不受影响
        current = asyncio.current_task()
        if self._ack_task is not None and self._ack_task is not current \
                and not self._ack_task.done():
            self._ack_task.cancel()
        if self._idle_task is not None and self._idle_task is not current \
                and not self._idle_task.done():
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
        self._stt_seq = 0
        self._stt_epoch += 1                 # 代际递增——旧会话在途任务提交前 epoch 失配被丢弃
        self._stt_pending = {}
        self._stt_commit_seq = 0
        # D3（review 修复）：取消在途派发任务——否则 cancel 后立即重启新会话会
        # 卡在旧任务占用的 STT 锁上（STT 串行化），且迟到结果浪费后端调用。
        # 跳过当前任务自取消（若 teardown 由派发任务内部触发，防自杀）。
        for t in list(self._stt_tasks):
            if t is not asyncio.current_task() and not t.done():
                t.cancel()
        self._stt_tasks = set()
        self._mode = VoiceMode.IDLE

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
            stt_transcribe=self._make_dispatch_stt(self._agent.stt_transcribe),
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
            await self._buffer.flush()          # 派发最后一段（feed 立即返回）
        await self._drain_dispatch_stt()        # 等待在途段完成再 teardown（残段仍提交）
        self._teardown_session()
        from frontend_contract.ws_message import VoiceEndReason, VoiceSessionEndedMessage
        await self._emit(VoiceSessionEndedMessage(
            sessionId=session_id, reason=VoiceEndReason("stopped")).wire_dict())

    async def cancel_session(self, session_id: str) -> None:
        self._require_session(session_id)
        # 取消在途轮次生成（spec §5.2）：generation 未完结才发中断
        if not getattr(self._agent.status, "is_completely_idle", True):
            from core.monitoring.event_bus import EventPriority
            from core.schema.event import InterruptData
            self._agent.bus.post(InterruptData(source="voice_cancel"),
                                 priority=EventPriority.INTERRUPT)
        self._teardown_session()
        from frontend_contract.ws_message import VoiceEndReason, VoiceSessionEndedMessage
        await self._emit(VoiceSessionEndedMessage(
            sessionId=session_id, reason=VoiceEndReason("cancelled")).wire_dict())

    # ---- DICTATION（BE-062） ----

    async def start_dictation(self, client_request_id: str) -> None:
        if self._mode != VoiceMode.IDLE:
            raise VoiceSessionError(ERR_CONFLICT, "已有活动语音会话")
        if not self._stt_available():
            raise VoiceSessionError(ERR_STT_UNAVAILABLE, "STT 不可用", True)
        self._mode = VoiceMode.DICTATION
        self._session_id = "dictation"
        self._dict_request_id = client_request_id
        self._dict_text = []
        self._last_audio_at = time.monotonic()
        self._buffer = AudioBuffer(
            stt_transcribe=self._make_dispatch_stt(self._transcribe_no_emotion),   # D6：不写共享 emotion
            aggressiveness=3,
            silence_duration=self._silence_duration,
            on_interrupt=None,  # DICTATION 不接（spec §3.2 NEW-K）
            on_transcription=self._on_segment,
        )

    async def stop_dictation(self, client_request_id: str) -> None:
        if (self._mode != VoiceMode.DICTATION
                or self._dict_request_id != client_request_id):
            raise VoiceSessionError(ERR_DICTATION_NOT_FOUND, "无活动转写")
        if self._buffer:
            await self._buffer.flush()
        await self._drain_dispatch_stt()        # F1：等最后一段提交进 _dict_text 再 join
        text = "".join(self._dict_text)
        self._teardown_session()
        from frontend_contract import DictationFinalMessage
        await self._emit(DictationFinalMessage(
            clientRequestId=client_request_id, text=text).wire_dict())

    async def cancel_dictation(self, client_request_id: str) -> None:
        if (self._mode != VoiceMode.DICTATION
                or self._dict_request_id != client_request_id):
            raise VoiceSessionError(ERR_DICTATION_NOT_FOUND, "无活动转写")
        self._teardown_session()
        from frontend_contract import DictationCancelledMessage
        await self._emit(DictationCancelledMessage(
            clientRequestId=client_request_id).wire_dict())

    # ---- feed / 回调 / 后台 ----

    def _make_dispatch_stt(self,
                           transcriber: Callable[[bytes], Awaitable[str]]):
        """派发型 STT wrapper（D3）：立即后台转写 + 返回空串，feed 不阻塞。
        transcriber: 真正转写可调用（REALTIME=agent.stt_transcribe；DICTATION=_transcribe_no_emotion）。"""
        async def _dispatch(wav: bytes) -> str:
            if self._mode == VoiceMode.IDLE:
                return ""            # 会话已结束——丢弃（含 cancel 后迟到帧）
            seq = self._stt_seq
            self._stt_seq += 1
            epoch = self._stt_epoch    # 捕获会话代际——防跨会话残段串流（B1）
            task = asyncio.create_task(
                self._run_dispatch_stt(wav, seq, epoch, transcriber),
                name=f"voice-stt-{seq}")
            self._stt_tasks.add(task)
            return ""                # feed 立即返回，不阻塞消息循环
        return _dispatch

    async def _run_dispatch_stt(self, wav: bytes, seq: int, epoch: int,
                                transcriber: Callable[[bytes], Awaitable[str]]) -> None:
        text = ""
        emotion = ""
        events: list[str] = []
        try:
            # 串行化「转写 + 情绪捕获」临界区（F3）：并发段转写共享 agent.stt_emotion_state，
            # 不加锁时早段提交可能读到晚段 emotion。锁只串行 STT 本身，消息循环仍不阻塞。
            async with self._stt_stt_lock:
                text = await asyncio.wait_for(transcriber(wav), timeout=STT_TIMEOUT)
                text = text.strip() if text else ""
                es = getattr(self._agent, "stt_emotion_state", None) or {}
                emotion = es.get("emotion", "") or ""
                events = list(es.get("events", []) or [])
        except asyncio.TimeoutError:
            logger.warning("语音 STT 段转写超时（丢弃段 seq=%d）", seq)
        except Exception:
            logger.warning("语音 STT 段转写失败", exc_info=True)
        finally:
            self._stt_tasks.discard(asyncio.current_task())
        if epoch != self._stt_epoch or self._mode == VoiceMode.IDLE:
            return                   # 跨会话/teardown 后迟到结果——丢弃（B1 防线）
        self._stt_pending[seq] = (text, emotion, events)
        try:
            await self._commit_ready_stt()
        except Exception:
            logger.debug("语音 STT 段提交失败（连接可能已关）", exc_info=True)

    async def _commit_ready_stt(self) -> None:
        """按派发序号顺序提交（提交锁串行化——防 partial/turn 帧交错）；防乱序破坏 turn_id。"""
        async with self._stt_commit_lock:
            while self._stt_commit_seq in self._stt_pending:
                text, emotion, events = self._stt_pending.pop(self._stt_commit_seq)
                self._stt_commit_seq += 1
                if not text:
                    continue         # 超时/失败段：不补 partial/turn
                if self._mode in (VoiceMode.REALTIME, VoiceMode.DICTATION) \
                        and not self._paused:
                    await self._on_segment(text, emotion=emotion, events=events)

    async def _drain_dispatch_stt(self) -> None:
        """等待在途派发 STT 段完成（stop/dictation-stop 路径：残段仍提交，spec §5.2）。"""
        pending = [t for t in self._stt_tasks if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

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

    async def _on_segment(self, text: str, emotion: str | None = None,
                          events: list[str] | None = None) -> None:
        text = text.strip() if text else ""
        if not text:
            return
        if self._mode == VoiceMode.REALTIME:
            epoch = self._stt_epoch    # 提交时快照会话代际（B1 提交路径防线）
            self._turn_id += 1
            seg = self._segment_index
            self._segment_index += 1
            from frontend_contract import VoiceSessionPartialMessage, VoiceSessionTurnMessage
            await self._emit(VoiceSessionPartialMessage(
                sessionId=str(self._session_id) if self._session_id else "",
                turnId=self._turn_id,
                segmentIndex=seg, text=text).wire_dict())
            # 提交途中被 cancel/teardown（partial emit 期间事件循环切换）→ 丢弃残段：
            # 不发 turn、不 post UserInputData——epoch 失配（含 cancel 后重启新会话）
            # 或 mode 已非 REALTIME 均作废（spec §5.2，review Important 窗口）。
            if epoch != self._stt_epoch or self._mode != VoiceMode.REALTIME:
                return
            if emotion is None:      # 缺省回退读共享 state（legacy AudioBuffer 回调路径）
                es = getattr(self._agent, "stt_emotion_state", None) or {}
            else:
                es = {"emotion": emotion, "events": events or []}
            message_id = new_message_id()
            self._agent.idle.touch()
            self._agent.bus.post(self._build_user_input(text, es, message_id))
            self._emitted_message_ids.add(message_id)   # D1 归属（Task 9）
            await self._emit(VoiceSessionTurnMessage(
                sessionId=str(self._session_id) if self._session_id else "",
                turnId=self._turn_id,
                messageId=message_id, conversationId=self._conv_id, text=text).wire_dict())
        elif self._mode == VoiceMode.DICTATION:
            from frontend_contract import DictationPartialMessage
            self._dict_text.append(text)
            await self._emit(DictationPartialMessage(text=text).wire_dict())

    @property
    def emitted_message_ids(self) -> set[str]:
        """本连接语音段提交的用户消息 ID 集合（D1 断连归属判定，Task 9 消费）。"""
        return set(self._emitted_message_ids)

    def _build_user_input(self, text: str, es: dict, message_id: MessageId):
        from core.schema.event import UserInputData
        return UserInputData(
            text=text, user_name=self._agent.user_name,
            emotion=es.get("emotion", "") or "",
            events=es.get("events", []) or [],
            request_id=self._agent.next_request_id(),
            message_id=message_id, protocol_version=2,
            conversation_id=SessionId(self._conv_id) if self._conv_id else None)

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
            from frontend_contract.ws_message import VoiceEndReason, VoiceSessionEndedMessage
            await self._emit(VoiceSessionEndedMessage(
                sessionId=sid or "", reason=VoiceEndReason("stt_disabled")).wire_dict())
        elif self._mode == VoiceMode.DICTATION:
            rid = self._dict_request_id  # teardown 前缓存
            self._teardown_session()
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
        except Exception:
            # 连接已关等 emit 失败——后台循环降级 debug，防 task-exception 噪音（review N2）
            logger.debug("voice_session ack 循环异常退出", exc_info=True)

    async def _idle_check_loop(self, session_id: str) -> None:
        try:
            while True:
                await asyncio.sleep(IDLE_CHECK_INTERVAL)
                if not self._paused and time.monotonic() - self._last_audio_at \
                        > VOICE_SESSION_IDLE_TIMEOUT:
                    self._teardown_session()
                    from frontend_contract.ws_message import VoiceEndReason, VoiceSessionEndedMessage
                    await self._emit(VoiceSessionEndedMessage(
                        sessionId=session_id, reason=VoiceEndReason("timeout")).wire_dict())
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("voice_session idle 检查循环异常退出", exc_info=True)
