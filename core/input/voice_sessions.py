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

from Module.stt import STTFactory
from Module._common.task_scope import AsyncTaskScope
from core.input.audio_buffer import AudioBuffer, Callback, pcm_to_wav, _FRAME_BYTES
from core.providers.custom_providers import resolve_model_params
from core.memory.session_manager import SessionNotFoundError
from core.schema.types import MessageId, SessionId
from core.utils import get_request_id, new_message_id
from frontend_contract.ws_message import AudioInputDescriptor

if TYPE_CHECKING:
    from core.agent import Agent
    from Module.stt.protocol import STTBackend

logger = logging.getLogger(__name__)

# spec §4.2/§5.3 常量
MAX_RECORDING_SECONDS = 600
MAX_RECORDING_BYTES = 16000 * 2 * MAX_RECORDING_SECONDS   # ≈18.3MiB
VOICE_SESSION_IDLE_TIMEOUT = 300.0
STT_TIMEOUT = 30.0
ACK_INTERVAL = 1.0
IDLE_CHECK_INTERVAL = 5.0
# 9C-1（R15）：单条 BINARY chunk 上限——超过 → CONTRACT_VALIDATION + 会话拆除
MAX_CHUNK_BYTES = 1 * 1024 * 1024

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
                 on_vad_interrupt: Callback | None = None,
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
        # REALTIME 生命周期本身持有一份会话引用；每个已提交 turn 另持一份，直到
        # Agent/CognitiveLoop 消费终态。这样 stop 后 EventBus 中的残留 turn 仍受保护。
        self._reserved_conv_id: SessionId | None = None
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
        # 9C-2：dictation 会话内 STT profile 快照后端（None → 用全局 agent.stt，不改全局活动 STT）
        self._dictation_stt: "STTBackend | None" = None
        # ARCH-INC-P2-03：脱离会话生命周期的收尾任务（STT 快照 backend 的 close）。
        # 这类 backend 持有独立 httpx client，裸 create_task 一旦丢失引用既无异常
        # 观察也无 drain——close() 时统一收尾。
        self._detached_tasks = AsyncTaskScope("voice-detached")
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

    def depends_on_global_stt(self) -> bool:
        """本会话是否依赖全局 STT 开关（9abc P1-2：profile dictation 独立）。

        判定结果被 ws_handler 的 ``_voice_stt_gate`` 与 ``set_service(stt=false)``
        通知共用——stt 门禁只对依赖全局开关的会话生效：
        - REALTIME / RECORDING：恒依赖全局 STT；
        - DICTATION：仅当无 profile 快照（``_dictation_stt is None``，缺省落全局
          agent.stt）时依赖；有快照后端（``sttProfileId``）则独立于全局开关；
        - IDLE：无会话，恒 False。
        """
        if self._mode == VoiceMode.IDLE:
            return False
        if self._mode == VoiceMode.DICTATION and self._dictation_stt is not None:
            return False  # profile 快照独立于全局活动 STT
        return True

    # ---- 通用 ----

    def _stt_available(self) -> bool:
        return (self._agent.stt is not None
                and getattr(self._agent, "stt_enabled", False))

    async def _transcribe_no_emotion(self, wav: bytes) -> str:
        """转写但不写共享 stt_emotion_state（D6：dictation 情绪隔离）。

        dictation 只回文字，本就不该触碰 agent 级情绪——隔离后无需快照/恢复，
        也不抹掉其他连接在 dictation 窗口内写入的 emotion/events。
        9C-2：优先用会话内 STT profile 快照 backend；无快照（缺省 profile）才落全局 agent.stt。
        """
        backend = self._dictation_stt or getattr(self._agent, "stt", None)
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

    def _validate_audio_descriptor(self, desc: AudioInputDescriptor | None) -> str | None:
        """校验输入音频描述（9C-1 R15）：返回错误 code 或 None。

        唯一支持 pcm_s16le / 16000Hz / 单声道；desc 为 None（缺省）→ 向后兼容默认
        16k/mono。不符 → CONTRACT_VALIDATION（ws_handler 捕获 → _send_voice_error）。
        """
        if desc is None:
            return None
        if desc.codec != "pcm_s16le" or desc.sampleRate != 16000 or desc.channels != 1:
            return "CONTRACT_VALIDATION"
        return None

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
        if self._reserved_conv_id is not None:
            self._agent.session_manager.release_work(self._reserved_conv_id)
            self._reserved_conv_id = None
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
        # 9C-2：释放 dictation 会话内 STT profile 快照 backend（后台关闭，不阻塞
        # 会话结束，close 失败也不外抛）。
        # ARCH-INC-P2-03：经 scope 调度——此前裸 create_task 的结果被丢弃，该 backend
        # 的 httpx client 若在事件循环关闭前没跑完就永不关闭，异常也无人观察。
        if self._dictation_stt is not None:
            stt = self._dictation_stt
            self._dictation_stt = None
            self._detached_tasks.try_spawn(
                stt.close(), name="dictation-stt-close")
        self._mode = VoiceMode.IDLE

    # ---- RECORDING（BE-060） ----

    async def start_recording(self, recording_id: str,
                              audio_descriptor: AudioInputDescriptor | None = None) -> None:
        if self._mode != VoiceMode.IDLE:
            raise VoiceSessionError(ERR_CONFLICT, "已有活动语音会话")
        bad = self._validate_audio_descriptor(audio_descriptor)
        if bad is not None:
            raise VoiceSessionError(bad, "输入音频格式不兼容（仅支持 pcm_s16le/16000Hz/单声道）")
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
        rec_pcm = b"".join(self._rec_pcm)
        # F3（review）：送 STT 前加最小长度守卫——< 一帧（640B）录音视为空转写直接
        # 丢弃，不送 STT、不触发转录（VAD 路径 _process_segment 有 min_bytes 0.3s 守卫，
        # RECORDING 路径没有 → 空/超短 WAV 送 STT 的洞）。丢弃后重置回 IDLE（同空转写）。
        # S3（review 复验）：长度 ≥ 一帧的录音整段送 STT，不再截断到 640 整帧倍数
        # （STT 接受任意长度 PCM，尾部残帧是真语音，截断丢字）。
        if len(rec_pcm) < _FRAME_BYTES:
            logger.info("录音过短（%d 字节 < 一帧 %d 字节）丢弃，不触发转录",
                        len(rec_pcm), _FRAME_BYTES)
            self._reset_recording()
            return
        wav = pcm_to_wav(rec_pcm)
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
        """取消当前录音的任意未提交阶段（BE-S4-RUN-P0-01，阶段4真实联调修订）。

        - RECORDING/TRANSCRIBING/PENDING（recordingId 匹配）：取消在途 STT、清 PCM/字幕并回 IDLE；
        - IDLE：幂等成功（迟到/重复 discard），不发错误、无 ACK（send-only wire 形状）；
        - recordingId 不匹配（另一条录音活跃）或 realtime/dictation 活跃：VOICE_SESSION_CONFLICT，
          绝不误清其它会话。
        """
        if self._mode == VoiceMode.IDLE:
            return                     # 幂等成功：无活动录音
        if self._mode != VoiceMode.RECORDING:
            raise VoiceSessionError(ERR_CONFLICT, "已有活动语音会话，无法 discard")
        if self._recording_id != recording_id:
            raise VoiceSessionError(ERR_CONFLICT, "recordingId 不匹配，存在另一条活动录音")
        if self._in_flight_stt is not None and not self._in_flight_stt.done():
            self._in_flight_stt.cancel()   # TRANSCRIBING 态取消在途 STT
        self._reset_recording()

    # ---- REALTIME（BE-061） ----

    async def start_realtime(self, session_id: str, conversation_id: str | None,
                             audio_descriptor: AudioInputDescriptor | None = None) -> None:
        if self._mode != VoiceMode.IDLE:
            raise VoiceSessionError(ERR_CONFLICT, "已有活动语音会话")
        bad = self._validate_audio_descriptor(audio_descriptor)
        if bad is not None:
            raise VoiceSessionError(bad, "输入音频格式不兼容（仅支持 pcm_s16le/16000Hz/单声道）")
        if not self._stt_available():
            raise VoiceSessionError(ERR_STT_UNAVAILABLE, "STT 不可用", True)
        target = await self._reserve_conversation(conversation_id)
        self._reserved_conv_id = SessionId(target)
        try:
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
        except Exception:
            self._teardown_session()
            raise

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
        # 取消在途轮次生成（spec §5.2）：generation 未完结才发中断。
        # S2-R2-4：停机窗口内不投——消费者已退出，残留中断会被同实例重启后的新循环
        # 消费（"残留事件跨运行被消费"与 CR-08 同源，判据统一 `status.is_service_running`）。
        if (not getattr(self._agent.status, "is_completely_idle", True)
                and self._agent.status.is_service_running):
            from core.monitoring.event_bus import EventPriority
            from core.schema.event import InterruptData
            self._agent.bus.post(InterruptData(source="voice_cancel"),
                                 priority=EventPriority.INTERRUPT)
        self._teardown_session()
        from frontend_contract.ws_message import VoiceEndReason, VoiceSessionEndedMessage
        await self._emit(VoiceSessionEndedMessage(
            sessionId=session_id, reason=VoiceEndReason("cancelled")).wire_dict())

    # ---- DICTATION（BE-062） ----

    async def start_dictation(self, client_request_id: str, stt_profile_id: str | None = None,
                              audio_descriptor: AudioInputDescriptor | None = None) -> None:
        """启动 dictation 会话（9C-2：可选会话内 STT profile，不改全局活动 STT）。

        Args:
            client_request_id: 客户端请求 ID（stop/cancel 对齐）。
            stt_profile_id: STT profile（<provider>/<model>）——非空则解析创建独立快照
                backend（_dictation_stt），会话期间转写走快照，global agent.stt/switch_stt
                不受影响；缺省 None → 用全局活动 STT。
            audio_descriptor: 输入音频描述（Task 7 三种 start 帧共用）。
        """
        if self._mode != VoiceMode.IDLE:
            raise VoiceSessionError(ERR_CONFLICT, "已有活动语音会话")
        bad = self._validate_audio_descriptor(audio_descriptor)
        if bad is not None:
            raise VoiceSessionError(bad, "输入音频格式不兼容（仅支持 pcm_s16le/16000Hz/单声道）")
        backend = None
        if stt_profile_id:
            # resolve_model_params 是同步函数（custom_providers.py:134 def），勿 await（P1）
            params = resolve_model_params("stt", stt_profile_id)
            runtime = (params or {}).pop("runtime", "backend")
            if runtime != "backend":
                await self._emit_error("STT_PROFILE_NOT_FOUND",
                                       f"STT profile 不可由后端执行: {stt_profile_id}")
                return
            try:
                if params and params.get("base_url"):
                    # SSRF 出站守卫（Q5）：validate_endpoint/build_guarded_http_client 在
                    # Module._common.ssrf。custom+backend 须注入 http_client_factory **工厂**再
                    # create——对齐 switch_stt（core/agent/server.py:58/65-66 lambda 工厂），
                    # 仅静态 validate_endpoint 不防 OpenAI 系 backend 以默认 client 出站建连。
                    from Module._common.ssrf import build_guarded_http_client, validate_endpoint
                    validate_endpoint(params["base_url"])
                    params["http_client_factory"] = (
                        lambda b=params["base_url"]: build_guarded_http_client(b))
                backend = STTFactory.create(stt_profile_id, **params)   # params 已 pop runtime，勿透传
            except Exception:
                await self._emit_error("STT_PROFILE_NOT_FOUND", f"STT profile 不存在: {stt_profile_id}")
                return
        elif not self._stt_available():
            # Q10：保留现状显式 raise（含 retryable=True）——_voice_stt_gate 只对已有活动
            # 会话生效（has_active_session 首行），新 dictation 不兜底。缺省 profile 且全局
            # STT 不可用 → 显式拒绝，否则静默进无后端 DICTATION 态。
            raise VoiceSessionError(ERR_STT_UNAVAILABLE, "STT 不可用", True)
        self._dictation_stt = backend   # None → 用全局 agent.stt
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
            if epoch != self._stt_epoch or self._mode == VoiceMode.IDLE:
                return                   # 跨会话/teardown 后迟到结果——丢弃（B1 防线）
            self._stt_pending[seq] = (text, emotion, events)
            try:
                await self._commit_ready_stt()
            except Exception:
                logger.debug("语音 STT 段提交失败（连接可能已关）", exc_info=True)
        finally:
            # S3-R2-2：摘除必须发生在**提交也做完之后**。原先它在转写一结束就执行，
            # 于是 `_drain_dispatch_stt()`（只快照 `_stt_tasks`）看不见"已转写完、仍在提交"
            # 的段：`stop_session` 会抢先 `_teardown_session()`（抬 epoch），该段随后被
            # `_on_segment` 的 epoch 守卫丢掉——客户端收到 `ended{stopped}`，
            # 却**既没有 turn 也没有回复**（DICTATION 侧同理会截断末尾文本）。
            self._stt_tasks.discard(asyncio.current_task())

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
        # 9C-1（R15）：单条 BINARY chunk 上限——超限 → CONTRACT_VALIDATION + 按模式分流拆除。
        # _teardown_session 是同步方法（勿 await）；REALTIME/DICTATION 完整拆除
        # _buffer/_ack_task/_idle_task/_session_id（不能用只清 RECORDING 字段的
        # _reset_recording()——那会留下 ack/idle 后台任务继续发帧，会话静默无声）；
        # RECORDING 反其道——错误帧是 error 不是 ended，须用 _reset_recording() 清
        # _rec_pcm/_rec_state/_recording_id（F4，_teardown_session 不碰这些字段）。
        if len(chunk) > MAX_CHUNK_BYTES:
            # S6（review 复验）：CONTRACT_VALIDATION 只在真正终止会话的分支内发。
            # TRANSCRIBING/PENDING（已 stop、在途 STT/待 commit）窗口只丢 chunk 不
            # emit error——否则客户端收到「chunk 超上限」后又收正常 transcript，矛盾信号。
            if self._mode == VoiceMode.RECORDING:
                # 9abc R2：仅录音中（RECORDING 态）才 _reset_recording()——已 stop 进入
                # TRANSCRIBING/PENDING（在途 STT / 待 commit）时，迟到的 >1MiB 帧只丢弃、
                # 不碰转录状态（否则毁掉在途/待提交转录，后续 voice.commit 报 NOT_FOUND）。
                if self._rec_state == _RecordingState.RECORDING:
                    await self._emit_error(ERR_CONTRACT, "音频 chunk 超过上限（1 MiB）")
                    self._reset_recording()
                else:
                    logger.debug("超限帧在转录/待提交窗口，忽略")
                return
            if self._mode == VoiceMode.DICTATION:
                await self._emit_error(ERR_CONTRACT, "音频 chunk 超过上限（1 MiB）")
                rid = self._dict_request_id            # teardown 前缓存（review #2 惯例）
                self._teardown_session()
                from frontend_contract import DictationCancelledMessage
                await self._emit(DictationCancelledMessage(
                    clientRequestId=rid).wire_dict())
                return
            await self._emit_error(ERR_CONTRACT, "音频 chunk 超过上限（1 MiB）")
            sid = self._session_id or ""
            self._teardown_session()
            from frontend_contract.ws_message import VoiceEndReason, VoiceSessionEndedMessage
            await self._emit(VoiceSessionEndedMessage(
                sessionId=sid, reason=VoiceEndReason("cancelled")).wire_dict())
            return
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
            # F1（review）：dictation 会话内 STT profile 快照 backend 独立于全局
            # stt_enabled——快照存在时首 chunk 不得因全局关闭被误拆会话（start_dictation
            # 带 profile 时本就跳过全局 _stt_available）；无快照（缺省 profile 落全局）
            # 才查全局 stt_enabled。REALTIME 无 profile 概念（_dictation_stt 恒 None），
            # 判定与旧行为一致。
            if self._dictation_stt is None \
                    and not getattr(self._agent, "stt_enabled", False):
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
            # CR-08 / S2-R2-4：**准入判定必须覆盖语音路径**。REALTIME 提交是直接
            # `bus.post(UserInputData)`，不经过 `process_message`——于是停机窗口
            # （`stop()` 首行即置 STOPPED、事件消费者已在第 1 步退出）里语音段仍能进
            # bus 队列；同实例重启（`rebuild_on_restart=False`）会把它当**新输入**消费。
            # 判据与 `process_message` 同源（`status.is_service_running`，生命周期轴），
            # 不另设 flag——否则又是"N 处都要记得"。
            # 这里**拒收而不排队**：与 `process_message` 一致，停机窗口没有"稍后再处理"。
            if not self._agent.status.is_service_running:
                logger.warning(
                    "语音段提交被拒：服务未在运行（停机窗口）——该段丢弃，会话随连接关闭")
                return
            # 每个 turn 独立预占 accepted work；REALTIME 生命周期引用只保证会话在
            # 录音期间存在，stop 后 EventBus/生成中的 turn 仍需自己的引用。
            try:
                target = await self._agent.session_manager.reserve_work(self._conv_id, 2)
            except SessionNotFoundError:
                await self._emit_error("SESSION_NOT_FOUND", "目标会话已不存在")
                return
            target_id = SessionId(target.session_id)
            if epoch != self._stt_epoch or self._mode != VoiceMode.REALTIME:
                self._agent.session_manager.release_work(target_id)
                return
            self._agent.idle.touch()
            try:
                self._agent.bus.post(
                    self._build_user_input(text, es, message_id, target_id))
            except Exception:
                self._agent.session_manager.release_work(target_id)
                raise
            self._emitted_message_ids.add(message_id)   # D1 归属（Task 9）
            await self._emit(VoiceSessionTurnMessage(
                sessionId=str(self._session_id) if self._session_id else "",
                turnId=self._turn_id, messageId=message_id,
                conversationId=str(target_id), text=text).wire_dict())
        elif self._mode == VoiceMode.DICTATION:
            from frontend_contract import DictationPartialMessage
            self._dict_text.append(text)
            await self._emit(DictationPartialMessage(text=text).wire_dict())

    @property
    def emitted_message_ids(self) -> set[str]:
        """本连接语音段提交的用户消息 ID 集合（D1 断连归属判定，Task 9 消费）。"""
        return set(self._emitted_message_ids)

    def _build_user_input(self, text: str, es: dict, message_id: MessageId,
                          conversation_id: SessionId):
        from core.schema.event import UserInputData
        return UserInputData(
            text=text, user_name=self._agent.user_name,
            emotion=es.get("emotion", "") or "",
            events=es.get("events", []) or [],
            request_id=self._agent.next_request_id(),
            message_id=message_id, protocol_version=2,
            conversation_id=conversation_id,
            session_work_reserved=True)

    async def _reserve_conversation(self, conversation_id: str | None) -> str:
        """原子解析并预占 REALTIME 目标会话，失败时不进入活动语音态。"""
        try:
            target = await self._agent.session_manager.reserve_work(conversation_id, 2)
            return str(target.session_id)
        except SessionNotFoundError as exc:
            raise VoiceSessionError(
                "SESSION_NOT_FOUND", f"会话不存在: {conversation_id or '(active)'}") from exc

    def _require_session(self, session_id: str) -> None:
        # 仅 REALTIME 接受 voice_session.* 消息（review #18：dictation 无这些消息，
        # 防客户端用 voice_session.stop{sessionId:"dictation"} 误结束转写）
        if self._mode != VoiceMode.REALTIME or self._session_id != session_id:
            raise VoiceSessionError(ERR_SESSION_NOT_FOUND, "会话不存在")

    async def notify_stt_disabled(self) -> bool:
        """set_service('stt', False) 成功后由 ws_handler 调用（spec §3.2 NEW-X/XA）。

        Returns:
            True 表示**本次调用终结了一个会话**（已发出终态帧）——调用方据此决定还要不要
            执行原命令（S3-08：会话已 IDLE，再执行必然抛 `VOICE_SESSION_NOT_FOUND`，
            客户端会收到两帧语义矛盾的消息）。RECORDING 不处理 ⇒ 返回 False
            （spec §3.2 NEW-XA：录音模式由 `voice.record.*` 自己收尾）。
        """
        if self._mode == VoiceMode.REALTIME:
            sid = self._session_id  # teardown 前缓存（review #2）
            self._teardown_session()
            from frontend_contract.ws_message import VoiceEndReason, VoiceSessionEndedMessage
            await self._emit(VoiceSessionEndedMessage(
                sessionId=sid or "", reason=VoiceEndReason("stt_disabled")).wire_dict())
            return True
        if self._mode == VoiceMode.DICTATION:
            rid = self._dict_request_id  # teardown 前缓存
            self._teardown_session()
            from frontend_contract import DictationCancelledMessage
            await self._emit(DictationCancelledMessage(
                clientRequestId=rid).wire_dict())
            return True
        # RECORDING 不处理（spec §3.2 NEW-XA）
        return False

    async def _end_session_stt_disabled(self) -> None:
        if self._mode == VoiceMode.REALTIME:
            await self.notify_stt_disabled()
        elif self._mode == VoiceMode.DICTATION:
            await self.notify_stt_disabled()

    async def close(self) -> None:
        """连接断开清理（spec §3.2 NEW-AA/XB/XC）——不发 InterruptData（复用 finally）。

        ARCH-INC-P2-03：末尾收尾 ``_detached_tasks``（如 dictation STT 快照 backend
        的 close）——这些 backend 持有独立 httpx client，连接关掉后就再没有第二次
        机会收它们了。
        """
        if self._in_flight_stt is not None and not self._in_flight_stt.done():
            self._in_flight_stt.cancel()
        self._teardown_session()
        self._reset_recording()
        leftover = await self._detached_tasks.drain(timeout=3.0)
        if leftover:
            logger.warning("语音会话收尾任务未在 3s 内落定（%d 个仍在途）", leftover)

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
