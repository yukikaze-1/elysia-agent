# 批次 6 三种语音模式 + TTS 播放控制 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把唯一的「WS BINARY PCM → VAD 自动提交」语音链路拆成普通录音/实时语音/输入法转写三种隔离模式（BE-060~062），并给 TTS 播放加上 playback 语义（BE-063）。

**Architecture:** 新增 per-connection `VoiceSessionManager`（`core/input/voice_sessions.py`）作为三模式状态机，复用现有 `AudioBuffer` 的 VAD 分段（realtime/dictation）与裸 PCM 缓冲（recording）；ws_handler 按「有无活动会话」分流 BINARY 帧；v2 语音提交复用 `_route_v2_text` 的幂等+ACK 模式；TTS 播放控制经 OutputManager→WebSocketChannel 广播 `audio_start`/`audio_cancelled`，`stream_id` 正式化为 playback ID。

**Tech Stack:** Python 3.12（conda Elysia）、aiohttp、pydantic、frontend_contract（契约单源）、pytest。

## Global Constraints

- **禁相对导入**（ruff TID252）：`from core.xxx import y` / `from Module.xxx import y`。
- **代码注释中文**；新消息契约 docstring 中文。
- **契约联合归属**：新增 Server→Client 消息**必须**加入 `ServerMessage` 判别联合（否则 gen_ts 不生成 TS 判别）；Client→Server 消息不入联合（仿 `TextMessage` 惯例）。
- **audio 相关 wire 键统一 snake_case `stream_id`**（spec P6-6/M1：不重命名现有键，playback ID 语义 = `stream_id`）。
- **legacy 无会话 BINARY 行为逐字节不变**（v1/v2 均走现有 `AudioBuffer.feed()` 自动提交）。
- **三种语音模式 v2-only**：仿 `_find_channel` 的 protocol_version 门禁，v1 连接收到新消息**忽略**（debug 日志），不报错。
- **feature 严禁超前**：`dictation/voice_recording/voice_realtime` 仅在本批次实现后置 true。
- 测试 marker：unit/service（`make test-quick`）；新测试必须加 marker。
- **lint 门禁在 Task 10 统一执行**（`make lint`）：Task 3 路由调用的 `_handle_voice_commit` 于 Task 4 才定义，Task 3 单独跑 ruff 会报 F821——pytest 层无此问题，各 Task 仅保证 `pytest` 绿，`make lint` 在 Task 10 收口（review A）。
- `UserInputData` 零新增字段（已含 `message_id/protocol_version/conversation_id/emotion/events`，`core/schema/event.py:24-36`）。
- 每任务结束独立测试 + commit。

---

### Task 1: frontend_contract 契约新增（消息类型 + features + union）

**Files:**
- Modify: `frontend_contract/ws_message.py`
- Modify: `frontend_contract/__init__.py`
- Modify: `core/input/capabilities.py`（features 置 true，见 Task 9 —— 本任务只加字段）
- Test: `frontend_contract/tests/test_batch6_contract.py`（新）

**Interfaces:**
- 本任务产出全部新契约类，供 Task 2~8 引用（manager/ws_handler/output 层 import）。

- [ ] **Step 1: 写失败测试**（契约序列化 + union 成员）

`frontend_contract/tests/test_batch6_contract.py`：

```python
"""批次 6 契约测试——三种语音 + TTS 播放控制消息。"""
from typing import get_args

import pytest
from frontend_contract import (
    AudioCancelMessage, AudioCancelledMessage, AudioMessage, AudioStartMessage,
    DictationCancelledMessage, DictationFinalMessage, DictationPartialMessage,
    DictationStartMessage, DictationStopMessage, DictationCancelMessage,
    ServerInfoFeatures, ServerMessage, VoiceCommitMessage, VoiceDiscardMessage,
    VoiceRecordStartMessage, VoiceRecordStopMessage, VoiceSessionAckMessage,
    VoiceSessionEndedMessage, VoiceSessionPartialMessage, VoiceSessionStartedMessage,
    VoiceSessionStartMessage, VoiceSessionStopMessage, VoiceSessionTurnMessage,
    VoiceTranscriptMessage,
)

pytestmark = pytest.mark.unit


def _union_members():
    return {c.__name__ for c in get_args(get_args(ServerMessage)[0])}  # Union[Annotated[Union[...]]]


def test_server_message_union_members():
    members = _union_members()
    for cls in (
        VoiceTranscriptMessage, VoiceSessionStartedMessage, VoiceSessionPartialMessage,
        VoiceSessionTurnMessage, VoiceSessionAckMessage, VoiceSessionEndedMessage,
        DictationPartialMessage, DictationFinalMessage, DictationCancelledMessage,
        AudioStartMessage, AudioCancelledMessage, AudioMessage,
    ):
        assert cls.__name__ in members, f"{cls.__name__} 未入 ServerMessage 联合"


def test_voice_commit_wire():
    m = VoiceCommitMessage(recordingId="rec_1", text="你好", clientRequestId="req_1")
    d = m.wire_dict()
    assert d["type"] == "voice.commit"
    assert d["text"] == "你好"


def test_audio_cancel_stream_id_key():
    m = AudioCancelMessage(stream_id=42)
    d = m.wire_dict()
    assert d == {"type": "audio.cancel", "stream_id": 42}


def test_audio_message_is_final_modeled():
    m = AudioMessage(stream_id=1, seq=0, data="eA==")
    assert m.is_final is False


def test_server_info_features_new_flags():
    f = ServerInfoFeatures()
    assert f.voice_recording is False and f.voice_realtime is False and f.dictation is False
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest frontend_contract/tests/test_batch6_contract.py -v`
Expected: FAIL — ImportError（新消息类不存在）。

- [ ] **Step 3: 实现契约类**

在 `frontend_contract/ws_message.py` 追加（批次 6 注释段，Client→Server 放 Client→Server 区，Server→Client 放 Server→Client 区）：

```python
# ─────────────────── 批次 6 三种语音（BE-060/061/062） ───────────────────

# Server→Client
class VoiceTranscriptMessage(ContractModel):
    """普通录音预转写字幕（stop 后、commit 前）。"""
    type: Literal["voice.transcript"] = "voice.transcript"
    recordingId: str = Field(description="录音 ID")
    text: str = Field(description="字幕文本")
    emotion: Optional[str] = Field(default=None, description="情绪标签，可缺省")


class VoiceSessionStartedMessage(ContractModel):
    """实时语音会话已开始（conversationId = 解析后实际目标会话）。"""
    type: Literal["voice_session.started"] = "voice_session.started"
    sessionId: str = Field(description="会话 ID")
    conversationId: Optional[str] = Field(default=None, description="实际目标会话，可缺省")


class VoiceSessionPartialMessage(ContractModel):
    """实时语音轮次字幕（每段一条 = 该轮 final 文本，仅显示）。"""
    type: Literal["voice_session.partial"] = "voice_session.partial"
    sessionId: str = Field(description="会话 ID")
    turnId: int = Field(description="轮次序号（每轮单调递增）")
    segmentIndex: int = Field(description="全局分段序号（跨轮累计）")
    text: str = Field(description="该轮文本")


class VoiceSessionTurnMessage(ContractModel):
    """实时语音轮次已提交（自动触发 generation）。"""
    type: Literal["voice_session.turn"] = "voice_session.turn"
    sessionId: str = Field(description="会话 ID")
    turnId: int = Field(description="轮次序号")
    messageId: str = Field(description="用户消息 ID（预分配，落库一致）")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，可缺省")
    text: str = Field(description="该轮最终文本")


class VoiceSessionAckMessage(ContractModel):
    """实时语音背压进度通知（seq = 服务端已接收 BINARY 帧序数）。"""
    type: Literal["voice_session.ack"] = "voice_session.ack"
    sessionId: str = Field(description="会话 ID")
    seq: int = Field(description="已接收帧序数")


class VoiceSessionEndedMessage(ContractModel):
    """实时语音会话结束。"""
    type: Literal["voice_session.ended"] = "voice_session.ended"
    sessionId: str = Field(description="会话 ID")
    reason: Literal["stopped", "cancelled", "timeout", "stt_disabled"] = Field(description="结束原因")


class DictationPartialMessage(ContractModel):
    """输入法转写中间字幕（仅显示）。"""
    type: Literal["dictation.partial"] = "dictation.partial"
    text: str = Field(description="累积文本")


class DictationFinalMessage(ContractModel):
    """输入法转写最终结果（只回文字，零消息副作用）。"""
    type: Literal["dictation.final"] = "dictation.final"
    clientRequestId: str = Field(description="原请求 ID")
    text: str = Field(description="最终文字")


class DictationCancelledMessage(ContractModel):
    """输入法转写已取消。"""
    type: Literal["dictation.cancelled"] = "dictation.cancelled"
    clientRequestId: str = Field(description="原请求 ID")


# Client→Server
class VoiceRecordStartMessage(ContractModel):
    """开始普通录音（全段缓冲，不触发）。"""
    type: Literal["voice.record.start"] = "voice.record.start"
    recordingId: str = Field(description="录音 ID（客户端生成 rec_ 前缀）")


class VoiceRecordStopMessage(ContractModel):
    """停止普通录音并预转写。"""
    type: Literal["voice.record.stop"] = "voice.record.stop"
    recordingId: str = Field(description="录音 ID")


class VoiceCommitMessage(ContractModel):
    """确认普通录音提交（v2：幂等 + 预分配 messageId）。text = 用户确认的字幕文本（幂等摘要依据）。"""
    type: Literal["voice.commit"] = "voice.commit"
    recordingId: str = Field(description="录音 ID")
    text: str = Field(description="确认的字幕文本")
    clientRequestId: str = Field(description="幂等键（必填）")
    clientMessageId: Optional[str] = Field(default=None, description="客户端本地消息 ID，可缺省")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，可缺省")


class VoiceDiscardMessage(ContractModel):
    """丢弃普通录音。"""
    type: Literal["voice.discard"] = "voice.discard"
    recordingId: str = Field(description="录音 ID")


class VoiceSessionStartMessage(ContractModel):
    """开始实时语音会话。"""
    type: Literal["voice_session.start"] = "voice_session.start"
    sessionId: str = Field(description="会话 ID（客户端生成 vs_ 前缀）")
    conversationId: Optional[str] = Field(default=None, description="会话 ID，可缺省")


class VoiceSessionPauseMessage(ContractModel):
    type: Literal["voice_session.pause"] = "voice_session.pause"
    sessionId: str = Field(description="会话 ID")


class VoiceSessionResumeMessage(ContractModel):
    type: Literal["voice_session.resume"] = "voice_session.resume"
    sessionId: str = Field(description="会话 ID")


class VoiceSessionStopMessage(ContractModel):
    type: Literal["voice_session.stop"] = "voice_session.stop"
    sessionId: str = Field(description="会话 ID")


class VoiceSessionCancelMessage(ContractModel):
    type: Literal["voice_session.cancel"] = "voice_session.cancel"
    sessionId: str = Field(description="会话 ID")


class DictationStartMessage(ContractModel):
    type: Literal["dictation.start"] = "dictation.start"
    clientRequestId: str = Field(description="请求 ID")


class DictationStopMessage(ContractModel):
    type: Literal["dictation.stop"] = "dictation.stop"
    clientRequestId: str = Field(description="请求 ID")


class DictationCancelMessage(ContractModel):
    type: Literal["dictation.cancel"] = "dictation.cancel"
    clientRequestId: str = Field(description="请求 ID")


# ─────────────────── 批次 6 TTS 播放控制（BE-063） ───────────────────

class AudioStartMessage(ContractModel):
    """音频流开始（stream_id = playback ID）。"""
    type: Literal["audio_start"] = "audio_start"
    stream_id: int = Field(description="playback ID")
    format: Literal["wav", "mp3"] = Field(default="wav", description="音频格式")
    sampleRate: Optional[int] = Field(default=None, description="采样率（WAV header），可缺省")
    durationMs: Optional[int] = Field(default=None, description="时长毫秒（WAV 计算），可缺省")


class AudioCancelledMessage(ContractModel):
    """音频流被取消（前端清理孤儿 chunk）。"""
    type: Literal["audio_cancelled"] = "audio_cancelled"
    stream_id: int = Field(description="playback ID")


class AudioCancelMessage(ContractModel):
    """取消在途播放实例（全局操作，v2-only）。"""
    type: Literal["audio.cancel"] = "audio.cancel"
    stream_id: int = Field(description="playback ID")
```

同时：`AudioMessage` 增 `is_final: bool = Field(default=False, description="是否最终块")`；`ServerInfoFeatures` 增 `voice_recording: bool = Field(default=False, description="普通录音（批次 6）")`、`voice_realtime: bool = Field(default=False, description="实时语音会话（批次 6）")`。

将新增 Server→Client 类加入 `ServerMessage` 联合（`AudioStartMessage`/`AudioCancelledMessage`/`VoiceTranscriptMessage`/`VoiceSessionStartedMessage`/`VoiceSessionPartialMessage`/`VoiceSessionTurnMessage`/`VoiceSessionAckMessage`/`VoiceSessionEndedMessage`/`DictationPartialMessage`/`DictationFinalMessage`/`DictationCancelledMessage`）。

`frontend_contract/__init__.py` re-export 全部新增类（含 `ServerMessage` 已有）。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest frontend_contract/tests/test_batch6_contract.py -v` + `make check-types`
Expected: PASS（union 成员、字段 wire、features 默认 false）。

- [ ] **Step 5: Commit**

```bash
git add frontend_contract/ && git commit -m "contract: 批次 6 语音/播放契约新增（三种语音 + TTS 播放控制，BE-060~063）"
```

---

### Task 2: VoiceSessionManager 状态机（核心）

**Files:**
- Create: `core/input/voice_sessions.py`
- Test: `core/input/tests/test_voice_sessions.py`（新）

**Interfaces:**
- 本任务产出 `VoiceSessionManager` 完整 API，供 Task 3~6（ws_handler）引用。签名见下方实现。

- [ ] **Step 1: 写失败测试**

`core/input/tests/test_voice_sessions.py`（marker=unit，用 fake_agent + 记录 emit 列表 + fake stt）：

```python
"""VoiceSessionManager 状态机测试——三模式隔离 + 边界（spec §3.3/§4/§5/§7）。"""
import asyncio
import struct

import pytest

from core.input.voice_sessions import (
    VoiceSessionError, VoiceSessionManager,
    MAX_RECORDING_BYTES,
)

pytestmark = pytest.mark.unit


def _loud(samples: int) -> bytes:
    return struct.pack(f"<{samples}h", *([20000] * samples))


def _sil(samples: int) -> bytes:
    return struct.pack(f"<{samples}h", *([5] * samples))


class FakeAgent:
    def __init__(self):
        self.stt = object()
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
async def test_recording_too_long():
    mgr, emitted, _ = make_manager()
    await mgr.start_recording("rec_1")
    big = b"\x00" * (MAX_RECORDING_BYTES // 2 + 1)
    await mgr.feed_pcm(big)
    await mgr.feed_pcm(big)
    assert any(e.get("type") == "error" and e.get("code") == "VOICE_RECORDING_TOO_LONG"
               for e in emitted)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest core/input/tests/test_voice_sessions.py -v`
Expected: FAIL — ModuleNotFoundError（voice_sessions 不存在）。

- [ ] **Step 3: 实现 VoiceSessionManager**

`core/input/voice_sessions.py` 完整实现（这是全批次核心，逐字照抄）：

```python
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
        from frontend_contract import DictationFinalMessage
        await self._emit(DictationFinalMessage(
            clientRequestId=client_request_id, text=text).wire_dict())

    async def cancel_dictation(self, client_request_id: str) -> None:
        if self._mode != VoiceMode.DICTATION:
            raise VoiceSessionError(ERR_DICTATION_NOT_FOUND, "无活动转写")
        self._teardown_session()
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
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest core/input/tests/test_voice_sessions.py -v`
Expected: PASS。若 `test_realtime_turn_submits_bus` 的 bus_posts 判别失败，检查 `_build_user_input` 返回的 `UserInputData.event_type`（应为 `"user_input"`）。

- [ ] **Step 5: Commit**

```bash
git add core/input/voice_sessions.py core/input/tests/test_voice_sessions.py
git commit -m "feat(voice): VoiceSessionManager 三模式状态机（普通录音/实时语音/输入法转写，BE-060~062）"
```

---

### Task 3: ws_handler 语音路由 + BINARY 分流 + 集成

**Files:**
- Modify: `core/input/ws_handler.py`
- Test: `core/input/tests/test_voice_ws.py`（新，marker=service）

**Interfaces:**
- 消费 Task 2 的 `VoiceSessionManager`（构造参数 `agent/emit/on_vad_interrupt`）。
- 产出 `_handle_voice_commit` 等，Task 4 完善。

- [ ] **Step 1: 写失败测试**

`core/input/tests/test_voice_ws.py`：

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest core/input/tests/test_voice_ws.py -v`
Expected: FAIL — 新消息类型未被路由（未知消息 debug 日志，无 transcript/ack）。

- [ ] **Step 3: 实现 ws_handler 路由**

在 `handle_ws` 中**无条件创建** manager（放在 `if agent.stt is not None:` 块之外——否则 STT 为 None 时 `voice_mgr` 未定义，BINARY 分支 NameError，review #10）：

```python
    from core.input.voice_sessions import VoiceSessionManager
    voice_mgr = VoiceSessionManager(
        agent=agent, emit=lambda data: ws.send_json(data),
        on_vad_interrupt=lambda: do_interrupt(agent, skip_vad=True),
    )
```

将 `voice_mgr` 传入 `handle_json_message` 调用（`await handle_json_message(data, agent, audio_buf, ws, voice_mgr=voice_mgr)`）与 BINARY 分支（`voice_mgr` 恒非 None，但仍加判空兜底）：

```python
            elif msg.type == WSMsgType.BINARY:
                if voice_mgr is not None and voice_mgr.has_active_session():
                    await voice_mgr.feed_pcm(msg.data)
                elif audio_buf and agent.stt_enabled:
                    await audio_buf.feed(msg.data)
```

`handle_json_message` 签名加 `voice_mgr=None` 参数。新增 v2-only 门禁助手 + 路由分支：

```python
def _is_v2(agent: "Agent", ws: "web.WebSocketResponse | None") -> bool:
    ch = _find_channel(agent, ws)
    return ch is not None and ch.protocol_version >= 2


async def _send_voice_error(ws, error) -> None:
    """VoiceSessionError → error 信封。"""
    from frontend_contract import ErrorMessage
    try:
        await ws.send_json(ErrorMessage(
            message=error.message, code=error.code).wire_dict())
    except Exception:
        logger.debug("发送 voice error 失败", exc_info=True)
```

在 `handle_json_message` 的 `else` 之前加分支（每支先 `if voice_mgr is None or not _is_v2(agent, ws): logger.debug(...); return`）：

```python
    elif msg_type == "voice.record.start":
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("voice.record.start: 非 v2 或无 mgr，忽略")
        else:
            try:
                await voice_mgr.start_recording(data.get("recordingId", ""))
            except VoiceSessionError as e:
                await _send_voice_error(ws, e)
    elif msg_type == "voice.record.stop":
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("voice.record.stop: 非 v2 或无 mgr，忽略")
        else:
            try:
                await voice_mgr.stop_recording(data.get("recordingId", ""))
            except VoiceSessionError as e:
                await _send_voice_error(ws, e)
    elif msg_type == "voice.commit":
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("voice.commit: 非 v2 或无 mgr，忽略")
        else:
            await _handle_voice_commit(agent, ws, voice_mgr, data)
    elif msg_type == "voice.discard":
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("voice.discard: 非 v2 或无 mgr，忽略")
        else:
            try:
                await voice_mgr.discard_recording(data.get("recordingId", ""))
            except VoiceSessionError as e:
                await _send_voice_error(ws, e)
    elif msg_type == "voice_session.start":
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("voice_session.start: 非 v2，忽略")
        else:
            try:
                await voice_mgr.start_realtime(
                    data.get("sessionId", ""), data.get("conversationId"))
            except VoiceSessionError as e:
                await _send_voice_error(ws, e)
    elif msg_type == "voice_session.pause":
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("voice_session.pause: 非 v2，忽略")
        else:
            try:
                await voice_mgr.pause_session(data.get("sessionId", ""))
            except VoiceSessionError as e:
                await _send_voice_error(ws, e)
    elif msg_type == "voice_session.resume":
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("voice_session.resume: 非 v2，忽略")
        else:
            try:
                await voice_mgr.resume_session(data.get("sessionId", ""))
            except VoiceSessionError as e:
                await _send_voice_error(ws, e)
    elif msg_type == "voice_session.stop":
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("voice_session.stop: 非 v2，忽略")
        else:
            try:
                await voice_mgr.stop_session(data.get("sessionId", ""))
            except VoiceSessionError as e:
                await _send_voice_error(ws, e)
    elif msg_type == "voice_session.cancel":
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("voice_session.cancel: 非 v2，忽略")
        else:
            try:
                await voice_mgr.cancel_session(data.get("sessionId", ""))
            except VoiceSessionError as e:
                await _send_voice_error(ws, e)
    elif msg_type == "dictation.start":
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("dictation.start: 非 v2，忽略")
        else:
            try:
                await voice_mgr.start_dictation(data.get("clientRequestId", ""))
            except VoiceSessionError as e:
                await _send_voice_error(ws, e)
    elif msg_type == "dictation.stop":
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("dictation.stop: 非 v2，忽略")
        else:
            try:
                await voice_mgr.stop_dictation(data.get("clientRequestId", ""))
            except VoiceSessionError as e:
                await _send_voice_error(ws, e)
    elif msg_type == "dictation.cancel":
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("dictation.cancel: 非 v2，忽略")
        else:
            try:
                await voice_mgr.cancel_dictation(data.get("clientRequestId", ""))
            except VoiceSessionError as e:
                await _send_voice_error(ws, e)
```

导入 `VoiceSessionManager`、`VoiceSessionError`。`set_service` 分支成功后追加（在广播之后）：

```python
        # 批次 6（spec §3.2 NEW-X/XA）：stt 关闭成功 → 通知本连接语音会话（RECORDING 除外）
        if (service == "stt" and not enabled and result.get("ok")
                and voice_mgr is not None):
            try:
                await voice_mgr.notify_stt_disabled()
            except Exception:
                logger.debug("notify_stt_disabled 失败", exc_info=True)
```

`handle_ws` finally 中（`audio_buf.flush()` 之后）追加：

```python
        if voice_mgr is not None:
            await voice_mgr.close()
```

（Task 4 补 `_handle_voice_commit`。）

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest core/input/tests/test_voice_ws.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add core/input/ws_handler.py core/input/tests/test_voice_ws.py
git commit -m "feat(voice): ws_handler 语音消息路由 + BINARY 分流 + set_service/断连接线（BE-060~062）"
```

---

### Task 4: v2 语音提交 `_handle_voice_commit`

**Files:**
- Modify: `core/input/ws_handler.py`
- Test: `core/input/tests/test_voice_ws.py`（补充）

**Interfaces:**
- 消费 Task 2 `VoiceSessionManager.commit_recording`（返回 `(text, emotion, events)`）。
- 复用现有 `_route_v2_text` 的幂等 store / `_send_ack`。

- [ ] **Step 1: 写失败测试**（并入 `test_voice_ws.py`）

```python
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
```

- [ ] **Step 2: 运行确认失败**

Expected: FAIL — `_handle_voice_commit` 未定义。

- [ ] **Step 3: 实现 `_handle_voice_commit`**

在 `handle_json_message` 之前加（复用 `_route_v2_text` 的 store/ack 模式，spec §6.1）。**注意 `new_message_id` 在 ws_handler 仅局部 import——函数内需 `from core.utils import new_message_id`（仿 `_route_v2_text` 局部导入，review NEW-4）**：

```python
async def _handle_voice_commit(agent: "Agent", ws: "web.WebSocketResponse | None",
                               voice_mgr, data: dict) -> None:
    """voice.commit 处理顺序（spec §3.3 NEW-A/NEW-M/NEW-T）：
    ① 解析目标会话 ack_conv（幂等重放分支也用，对齐 _route_v2_text，review #15）
    ② 缺 clientRequestId → 直接 ack rejected（置于幂等与门禁之前）
    ③ 幂等表查重（digest = commit 携带 text）→ 命中重发原 ACK / 异摘要 rejected
    ④ 未命中 → 状态机 PENDING 门禁（commit_recording）→ ACK + post UserInputData。
    """
    recording_id = data.get("recordingId", "")
    text = data.get("text", "")
    client_request_id = data.get("clientRequestId", "")
    client_message_id = data.get("clientMessageId")
    conversation_id = data.get("conversationId")

    try:
        target = await agent.resolve_session(conversation_id, 2)
        ack_conv = str(target.session_id)
    except Exception:
        ack_conv = conversation_id

    if not client_request_id:
        await _send_ack(ws, "", "", client_message_id, ack_conv, "rejected",
                        error={"code": "CONTRACT_VALIDATION",
                               "message": "缺少 clientRequestId", "retryable": False})
        return

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    store = get_store()
    entry = store.lookup(client_request_id)
    if entry is not None:
        if entry.content_digest == digest:
            await _send_ack(ws, entry.user_message_id, client_request_id,
                            client_message_id, ack_conv, "accepted")
        else:
            await _send_ack(ws, "", client_request_id, client_message_id,
                            ack_conv, "rejected",
                            error={"code": "CONTRACT_VALIDATION",
                                   "message": "clientRequestId 冲突（内容摘要不同）",
                                   "retryable": False})
        return

    try:
        pending_text, emotion, events = voice_mgr.commit_recording(recording_id, text)
    except VoiceSessionError as e:
        await _send_ack(ws, "", client_request_id, client_message_id, ack_conv,
                        "rejected", error={"code": e.code, "message": e.message,
                                           "retryable": e.retryable})
        return

    message_id = new_message_id()
    store.store(client_request_id, IdempotencyEntry(
        user_message_id=message_id, content_digest=digest, created_at=time.monotonic()))
    await _send_ack(ws, message_id, client_request_id, client_message_id, ack_conv, "accepted")
    agent.idle.touch()
    # conversation_id 传原始值（None=活动会话），与 _route_v2_text 传原始给 process_message 一致；
    # ACK 侧已用解析后 ack_conv（review NEW-5）
    agent.bus.post(UserInputData(
        text=pending_text, user_name=agent.user_name, emotion=emotion, events=events,
        request_id=agent.next_request_id(), message_id=message_id,
        protocol_version=2, conversation_id=conversation_id))
    logger.debug("voice.commit 提交成功: %s (%s)", pending_text[:30], recording_id)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest core/input/tests/test_voice_ws.py -v`
Expected: PASS（含 Task 3 测试回归）。

- [ ] **Step 5: Commit**

```bash
git add core/input/ws_handler.py core/input/tests/test_voice_ws.py
git commit -m "feat(voice): 普通录音 commit 的 v2 提交（幂等 + ACK + UserInputData，BE-060/P6-4）"
```

---

### Task 5: 实时语音轮次提交（partial + turn）

**Files:**
- 主要逻辑已在 Task 2 `_on_segment`（realtime 分支）。
- Test: `core/input/tests/test_voice_ws.py`（补充 service 层）

**Interfaces:**
- 消费 Task 2 `start_realtime/pause/resume/stop/cancel`。

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.asyncio
async def test_realtime_session_lifecycle():
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
```

- [ ] **Step 2: 运行确认失败**

Expected: 未知会话 cancel 返回 `error{VOICE_SESSION_NOT_FOUND}`（`_require_session`）。

- [ ] **Step 3: 实现**

Task 2 已实现 `_require_session`/`pause`/`resume`/`stop`/`cancel`；Task 3 已接 ws_handler 分支。本任务仅验证 service 层行为（测试驱动确认）。

若 `test_realtime_cancel_unknown_session` 失败，检查 `_require_session` 逻辑与 ws_handler 分支是否捕获 `VoiceSessionError` 并 `_send_voice_error`。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest core/input/tests/test_voice_ws.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add core/input/tests/test_voice_ws.py
git commit -m "test(voice): 实时语音会话生命周期 + 未知会话拒绝（BE-061）"
```

---

### Task 6: 输入法转写隔离 + stt_emotion 快照恢复

**Files:**
- 修改 `core/input/voice_sessions.py`（如需 stt_emotion 快照恢复）
- Test: `core/input/tests/test_voice_sessions.py`（补充）

**Interfaces:**
- 消费 Task 2 `start_dictation/stop_dictation/cancel_dictation`。

- [ ] **Step 1: 写失败测试**（dictation 不污染共享 stt_emotion_state）

```python
@pytest.mark.asyncio
async def test_dictation_restores_stt_emotion_state():
    from core.input.voice_sessions import VoiceSessionManager
    agent = FakeAgent()
    agent.stt_emotion_state = {"emotion": "SAD", "events": []}  # dictation 前状态
    emitted, bus_posts = [], []
    agent.bus = type("Bus", (), {"post": lambda self, d: bus_posts.append(d)})()
    agent.idle = type("Idle", (), {"touch": lambda self: None})()

    # 让 stt_transcribe 原地改共享 dict（与真实 agent.stt_transcribe 语义一致，review #8）
    real = agent.stt_transcribe
    async def polluted_stt(wav):
        agent.stt_emotion_state["emotion"] = "POLLUTED"
        agent.stt_emotion_state["events"] = ["p"]
        return await real(wav)
    agent.stt_transcribe = polluted_stt

    mgr = VoiceSessionManager(agent=agent, emit=async_emit(emitted), silence_duration=0.1)
    await mgr.start_dictation("req_d2")
    agent._last_text = "转写"
    await _drive_segment(mgr)
    await asyncio.sleep(0.05)
    await mgr.stop_dictation("req_d2")
    # dictation 结束后共享状态应被恢复为 dictation 前值（S5）
    assert agent.stt_emotion_state.get("emotion") == "SAD"


@pytest.mark.asyncio
async def test_dictation_stt_disabled_restores_emotion():
    """STT 中途关闭终止 dictation → 同样恢复 emotion（review NEW-3/C）。"""
    from core.input.voice_sessions import VoiceSessionManager
    agent = FakeAgent()
    agent.stt_emotion_state = {"emotion": "SAD", "events": []}
    emitted, bus_posts = [], []
    agent.bus = type("Bus", (), {"post": lambda self, d: bus_posts.append(d)})()
    agent.idle = type("Idle", (), {"touch": lambda self: None})()
    real = agent.stt_transcribe
    async def polluted_stt(wav):
        agent.stt_emotion_state["emotion"] = "POLLUTED"
        return await real(wav)
    agent.stt_transcribe = polluted_stt

    mgr = VoiceSessionManager(agent=agent, emit=async_emit(emitted), silence_duration=0.1)
    await mgr.start_dictation("req_d3")
    agent._last_text = "转写"
    await _drive_segment(mgr)
    await asyncio.sleep(0.05)
    mgr._agent.stt_enabled = False
    await mgr.notify_stt_disabled()
    assert agent.stt_emotion_state.get("emotion") == "SAD"
    cancelled = [e for e in emitted if e.get("type") == "dictation.cancelled"]
    assert cancelled and cancelled[-1]["clientRequestId"] == "req_d3"
```

- [ ] **Step 2: 运行确认失败**

Expected: FAIL — `stt_emotion_state` 仍为 POLLUTED（dictation 污染未恢复）。

- [ ] **Step 3: 实现 stt_emotion 快照恢复**

在 `VoiceSessionManager`：

```python
    def __init__(...):
        ...
        self._emotion_snapshot = None   # dictation 前 stt_emotion_state 快照
```

`start_dictation` 开头（stt_available 校验后）保存快照（**dict 复制**）：

```python
        # 快照共享 stt_emotion_state（dict 复制）——dictation 转写会原地改它，
        # 结束后恢复（spec §7.2 S5）；Agent.stt_emotion_state 是只读 property，
        # 不能整对象赋值，须复制 + 原地 clear/update（review #8）
        es = getattr(self._agent, "stt_emotion_state", None)
        self._emotion_snapshot = dict(es) if isinstance(es, dict) else None
```

`stop_dictation` / `cancel_dictation` / `close` 恢复（在 `_teardown_session()` 之后调）：

```python
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
```

`stop_dictation`/`cancel_dictation` 在 `_teardown_session()` 后、`close()` 在 `_reset_recording()` 前各调一次 `self._restore_emotion()`。**`notify_stt_disabled` 的 DICTATION 分支（STT 中途关闭结束转写）同样在 teardown 后调 `_restore_emotion()`**（review NEW-3——该路径 dictation 的 STT 已污染共享状态，不恢复破坏 S5 隔离）。`_teardown_session` 不通用恢复（RECORDING 无需）；`_restore_emotion` 自身在末尾清 `_emotion_snapshot`（无需在 `start_dictation` 重复重置）。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest core/input/tests/test_voice_sessions.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add core/input/voice_sessions.py core/input/tests/test_voice_sessions.py
git commit -m "fix(voice): dictation 转写后恢复共享 stt_emotion_state（S5 隔离）"
```

---

### Task 7: TTS 播放控制 — 输出层（manager + websocket）

**Files:**
- Modify: `Module/output/manager.py`
- Modify: `Module/output/websocket.py`
- Modify: `Module/output/protocol.py`（OutputChannel ABC 增 on_audio_start/on_audio_cancelled 默认实现）
- Test: `Module/output/tests/`（补充）

**Interfaces:**
- 产出 `OutputManager.audio_start(stream_id, *, format, sample_rate, duration_ms, ...)` / `audio_cancelled(stream_id, ...)`。
- 产出 `WebSocketChannel.on_audio_start/on_audio_cancelled`。

- [ ] **Step 1: 写失败测试**

`Module/output/tests/test_audio_control.py`：

```python
"""TTS 播放控制输出层测试（BE-063）。"""
import base64
import pytest

from Module.output import OutputManager, WebSocketChannel

pytestmark = pytest.mark.unit


class FakeWs:
    def __init__(self):
        self.sent = []
    async def send_json(self, data):
        self.sent.append(data)


def test_audio_start_wire():
    ws = FakeWs()
    ch = WebSocketChannel(ws)
    import asyncio
    asyncio.run(ch.on_audio_start(5, format="wav", sample_rate=32000, duration_ms=1000))
    frame = ws.sent[-1]
    assert frame["type"] == "audio_start"
    assert frame["stream_id"] == 5 and frame["sampleRate"] == 32000


def test_audio_cancelled_pops_seq():
    ws = FakeWs()
    ch = WebSocketChannel(ws)
    import asyncio
    asyncio.run(ch.on_audio_chunk(b"\x00" * 100, stream_id=7))
    assert ch._audio_seq.get(7) == 1
    asyncio.run(ch.on_audio_cancelled(7))
    assert 7 not in ch._audio_seq
    assert ws.sent[-1]["type"] == "audio_cancelled"


def test_audio_chunk_uses_audio_message():
    ws = FakeWs()
    ch = WebSocketChannel(ws)
    import asyncio
    asyncio.run(ch.on_audio_chunk(b"\x01\x02", stream_id=3))
    frame = ws.sent[-1]
    assert frame["type"] == "audio"
    assert frame["is_final"] is False
    assert frame["data"] == base64.b64encode(b"\x01\x02").decode("ascii")
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest Module/output/tests/test_audio_control.py -v`
Expected: FAIL — `on_audio_start`/`on_audio_cancelled` 不存在。

- [ ] **Step 3: 实现输出层**

`Module/output/protocol.py` `OutputChannel` ABC 加默认方法（空实现，其他 channel 不强制）：

```python
    async def on_audio_start(self, stream_id: int, *, format: str = "wav",
                             sample_rate: int | None = None,
                             duration_ms: int | None = None) -> None:
        """音频流开始（BE-063）。默认空实现。"""

    async def on_audio_cancelled(self, stream_id: int) -> None:
        """音频流被取消（BE-063）。默认空实现。"""
```

`Module/output/websocket.py`：

```python
    async def on_audio_start(self, stream_id: int, *, format: str = "wav",
                             sample_rate: int | None = None,
                             duration_ms: int | None = None) -> None:
        """音频流开始（stream_id = playback ID）。"""
        await self._send_json(AudioStartMessage(
            stream_id=stream_id, format=format, sampleRate=sample_rate,
            durationMs=duration_ms).wire_dict())

    async def on_audio_cancelled(self, stream_id: int) -> None:
        """音频流被取消——清理 _audio_seq 残留 + 通知前端清理孤儿 chunk。"""
        self._audio_seq.pop(stream_id, None)
        await self._send_json(AudioCancelledMessage(stream_id=stream_id).wire_dict())
```

`on_audio_chunk` 改用手动 dict 为 `AudioMessage`（修 `is_final` 漂移）：

```python
    async def on_audio_chunk(self, chunk: bytes, *, stream_id: int = 0) -> None:
        import base64
        seq = self._audio_seq.get(stream_id, 0)
        self._audio_seq[stream_id] = seq + 1
        await self._send_json(AudioMessage(
            stream_id=stream_id, seq=seq,
            data=base64.b64encode(chunk).decode("ascii"), is_final=False).wire_dict())
```

import 补 `AudioStartMessage`、`AudioCancelledMessage`、`AudioMessage`。

`Module/output/manager.py`：

```python
    async def audio_start(self, stream_id: int, *, format: str = "wav",
                          sample_rate: int | None = None, duration_ms: int | None = None,
                          channels: list[OutputChannel] | None = None,
                          capability: ChannelCapability | None = None) -> None:
        """广播音频流开始（stream_id = playback ID，BE-063）。"""
        await self._broadcast(
            "on_audio_start", stream_id, format=format, sample_rate=sample_rate,
            duration_ms=duration_ms, channels=channels,
            capability=capability or ChannelCapability.AUDIO)

    async def audio_cancelled(self, stream_id: int, *,
                              channels: list[OutputChannel] | None = None,
                              capability: ChannelCapability | None = None) -> None:
        """广播音频流被取消（BE-063）。"""
        await self._broadcast(
            "on_audio_cancelled", stream_id, channels=channels,
            capability=capability or ChannelCapability.AUDIO)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest Module/output/tests/ -v`
Expected: PASS（新增 + 现有输出测试）。

- [ ] **Step 5: Commit**

```bash
git add Module/output/ && git commit -m "feat(tts): 输出层 audio_start/audio_cancelled 广播 + AudioMessage is_final 契约建模（BE-063）"
```

---

### Task 8: TTS 播放控制 — adapter 层（audio_start / cancel_stream / cancel_all）

**Files:**
- Modify: `core/adapter/tts_adapter.py`
- Modify: `core/agent/agent.py`（增 `tts_manager` 公开访问器）
- Test: `core/adapter/tests/test_tts_adapter_control.py`（新）或并入现有

**Interfaces:**
- 产出 `TTSStreamManager.cancel_stream(stream_id) -> bool`、`Agent.tts_manager`。
- 消费 Task 7 `OutputManager.audio_start/audio_cancelled`。

- [ ] **Step 1: 写失败测试**

`core/adapter/tests/test_tts_adapter_control.py`（marker=unit）：

```python
"""TTSStreamManager 播放控制（BE-063）。"""
import asyncio
import struct
import time

import pytest

from core.adapter.tts_adapter import TTSStreamManager, _wav_info

pytestmark = pytest.mark.unit


def test_wav_info_parses():
    sample_rate = 8000
    data_size = 1600
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE", b"fmt ", 16, 1, 1,
        sample_rate, sample_rate * 2, 2, 16, b"data", data_size,
    )
    sr, dur = _wav_info(header + b"\x00" * data_size)
    assert sr == 8000
    assert dur == int(data_size * 1000 / (sample_rate * 2))  # 100ms


class FakeOut:
    def __init__(self):
        self.calls = []
        self.chunk_gate = None  # asyncio.Event 置位后 audio_chunk 阻塞——测试可停在转发段（NEW-1）
    async def _broadcast(self, method, *a, **kw):
        self.calls.append((method, a, kw))
    async def send(self, *a, **kw):
        pass
    async def audio_chunk(self, chunk, *, stream_id=0, **kw):
        self.calls.append(("audio_chunk", stream_id, len(chunk)))
        if self.chunk_gate is not None:
            await self.chunk_gate.wait()
    async def audio_end(self, stream_id=0, *, format="wav", **kw):
        self.calls.append(("audio_end", stream_id))
    async def audio_start(self, stream_id, *, format="wav", sample_rate=None, duration_ms=None, **kw):
        self.calls.append(("audio_start", stream_id, sample_rate, duration_ms))
    async def audio_cancelled(self, stream_id, **kw):
        self.calls.append(("audio_cancelled", stream_id))


class FakeTTS:
    stream_format = "wav"
    def get_preprocess(self):
        return None
    async def synthesize(self, text, *, character=None, emotion=None, voice_id=None):
        header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + 3200, b"WAVE",
                             b"fmt ", 16, 1, 1, 32000, 64000, 2, 16, b"data", 3200)
        return header + b"\x00" * 3200


async def _wait_start(out):
    deadline = time.monotonic() + 3
    while not any(c[0] == "audio_start" for c in out.calls) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_stream_sentence_emits_audio_start():
    out = FakeOut()
    mgr = TTSStreamManager(tts=FakeTTS(), output=out, character_name="elysia")
    mgr.reset_for_turn()  # 预置 seq=1 门控（否则 _download_and_forward 卡 wait）
    mgr.stream_sentence("你好")
    await mgr.wait_all()
    assert any(c[0] == "audio_start" for c in out.calls)
    start = [c for c in out.calls if c[0] == "audio_start"][0]
    assert start[2] == 32000  # sample_rate


@pytest.mark.asyncio
async def test_cancel_stream_emits_audio_cancelled():
    out = FakeOut()
    out.chunk_gate = asyncio.Event()  # 阻塞转发段，确定性停在「已 audio_start、仍在转发」（NEW-1）
    mgr = TTSStreamManager(tts=FakeTTS(), output=out, character_name="elysia")
    mgr.reset_for_turn()
    mgr.stream_sentence("一句话")
    await _wait_start(out)  # 等 audio_start（任务已堵在 chunk_gate）
    ok = mgr.cancel_stream(1)  # sid=1
    out.chunk_gate.set()  # 释放被堵任务（cancel 本身不依赖 gate，仅清理）
    await asyncio.sleep(0.05)
    assert ok is True
    assert any(c[0] == "audio_cancelled" for c in out.calls)


@pytest.mark.asyncio
async def test_cancel_all_emits_audio_cancelled():
    """cancel_all 补 audio_cancelled（修孤儿 chunk，spec §12/§8.2）。"""
    out = FakeOut()
    out.chunk_gate = asyncio.Event()
    mgr = TTSStreamManager(tts=FakeTTS(), output=out, character_name="elysia")
    mgr.reset_for_turn()
    mgr.stream_sentence("一句话")
    await _wait_start(out)
    mgr.cancel_all()
    out.chunk_gate.set()
    await asyncio.sleep(0.05)
    assert any(c[0] == "audio_cancelled" for c in out.calls)


def test_cancel_unknown_returns_false():
    mgr = TTSStreamManager(tts=None, output=FakeOut(), character_name="elysia")
    assert mgr.cancel_stream(999) is False
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest core/adapter/tests/test_tts_adapter_control.py -v`
Expected: FAIL — `_wav_info`/`cancel_stream` 不存在。

- [ ] **Step 3: 实现 adapter**

`core/adapter/tts_adapter.py`：

```python
import struct

def _wav_info(audio: bytes) -> tuple[int | None, int | None]:
    """解析 WAV header → (sample_rate, duration_ms)；非 WAV/异常返回 (None, None)。"""
    try:
        if len(audio) < 44 or audio[:4] != b"RIFF":
            return None, None
        sample_rate = struct.unpack("<I", audio[24:28])[0]
        byte_rate = struct.unpack("<I", audio[28:32])[0]
        data_size = struct.unpack("<I", audio[40:44])[0]
        duration_ms = int(data_size * 1000 / byte_rate) if byte_rate else None
        return sample_rate, duration_ms
    except Exception:
        return None, None
```

`TTSStreamManager.__init__` 加状态：

```python
        self._task_by_sid: dict[int, asyncio.Task] = {}  # sid → 任务（BE-063 cancel）
        self._audio_forwarding: set[int] = set()         # 已开始转发的 sid（cancel 补 audio_cancelled）
```

`stream_sentence` 任务创建处注册：

```python
        task = asyncio.create_task(
            self._download_and_forward(sid, seq, sentence, display_text, action_text),
            name=f"tts-{sid}",
        )
        self._tasks.append(task)
        self._task_by_sid[sid] = task
        # 完成清理
        self._tasks = [t for t in self._tasks if not t.done()]
        self._task_by_sid = {s: t for s, t in self._task_by_sid.items() if not t.done()}
```

`_download_and_forward` 分块转发前（Phase 2 放行后）发 audio_start：

```python
            # 发本句文字
            text_broadcast = True
            await self._output._broadcast(
                "on_text_delta", display_text,
                capability=ChannelCapability.TEXT_DELTA, stream_id=sid)
            await self._output.send("")
            # BE-063：句开始播放（stream_id = playback ID）+ WAV 元数据
            fmt = self._tts.stream_format if self._tts else "wav"
            sr, dur = _wav_info(audio)
            self._audio_forwarding.add(sid)
            await self._output.audio_start(sid, format=fmt, sample_rate=sr, duration_ms=dur)
            chunk_size = 4096
            for offset in range(0, len(audio), chunk_size):
                await self._output.audio_chunk(audio[offset:offset + chunk_size], stream_id=sid)
            await self._output.audio_end(sid, format=fmt)
            self._audio_forwarding.discard(sid)
```

`CancelledError` 分支补 audio_cancelled（若已开始转发）：

```python
        except asyncio.CancelledError:
            if getattr(asyncio.current_task(), "flush_on_cancel", False) and not text_broadcast:
                await self._output._broadcast(
                    "on_text_delta", display_text,
                    capability=ChannelCapability.TEXT_DELTA, stream_id=sid)
                await self._output.send("")
            if sid in self._audio_forwarding:
                await self._output.audio_cancelled(sid)
                self._audio_forwarding.discard(sid)
            raise
```

`cancel_stream` 方法：

```python
    def cancel_stream(self, stream_id: int) -> bool:
        """取消指定播放实例（BE-063）；在途返回 True（任务补 audio_cancelled），否则 False。"""
        task = self._task_by_sid.get(stream_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True
```

`Agent`（`core/agent/agent.py`）加公开访问器：

```python
    @property
    def tts_manager(self):
        """TTS 流管理器（播放控制，BE-063 audio.cancel 入口）。"""
        return self._cognitive._tts_mgr
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest core/adapter/tests/ -v`
Expected: PASS（新增 + 现有 adapter 测试）。

- [ ] **Step 5: Commit**

```bash
git add core/adapter/tts_adapter.py core/agent/agent.py core/adapter/tests/
git commit -m "feat(tts): TTSStreamManager audio_start + cancel_stream/cancel_all 补 audio_cancelled（BE-063）"
```

---

### Task 9: capabilities features + 错误码 + 文档

**Files:**
- Modify: `core/input/capabilities.py`
- Modify: `core/input/ws_handler.py`（audio.cancel 路由分支）
- Test: `core/input/tests/test_voice_ws.py`（audio.cancel）
- Docs: `docs/electron-backend-protocol-v2.md`、`docs/electron-backend-progress.md`、`docs/electron-backend-security.md`

**Interfaces:**
- 消费 Task 8 `Agent.tts_manager.cancel_stream`。

- [ ] **Step 1: 写失败测试**（features + audio.cancel 路由）

```python
def test_server_info_features_true_after_batch6():
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
    agent, ws, mgr = make()
    cancelled = []
    agent.tts_manager = type("TtsMgr", (), {
        "cancel_stream": lambda self, sid: cancelled.append(sid) or True})()
    await handle_json_message({"type": "audio.cancel", "stream_id": 7},
                              agent, None, ws, voice_mgr=mgr)
    assert cancelled == [7]
```

- [ ] **Step 2: 运行确认失败**

Expected: FAIL — features 仍 false；audio.cancel 未路由。

- [ ] **Step 3: 实现**

`core/input/capabilities.py` `build_server_info` 的 features：

```python
        features=ServerInfoFeatures(
            structured_generation=True,
            multi_conversation=True,
            desktop_runtime=True,
            dictation=True,        # 批次 6 已实现（BE-062）
            voice_recording=True,  # 批次 6（BE-060）
            voice_realtime=True,   # 批次 6（BE-061）
        ),
```

`core/input/ws_handler.py` 加 audio.cancel 分支：

```python
    elif msg_type == "audio.cancel":
        # 批次 6（BE-063）：取消在途 TTS 播放实例（全局操作）。
        if voice_mgr is None or not _is_v2(agent, ws):
            logger.debug("audio.cancel: 非 v2，忽略")
        else:
            sid = data.get("stream_id")
            if sid is None:
                await _send_voice_error(ws, VoiceSessionError(
                    "CONTRACT_VALIDATION", "缺少 stream_id"))
                return
            try:
                sid_int = int(sid)  # 非数字 → CONTRACT_VALIDATION（review #17）
            except (TypeError, ValueError):
                await _send_voice_error(ws, VoiceSessionError(
                    "CONTRACT_VALIDATION", "stream_id 非法"))
                return
            tts_mgr = getattr(agent, "tts_manager", None)
            if tts_mgr is None or not tts_mgr.cancel_stream(sid_int):
                await _send_voice_error(ws, VoiceSessionError(
                    "PLAYBACK_NOT_FOUND", "播放实例不存在或已结束"))
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest core/input/tests/ -v`
Expected: PASS。

- [ ] **Step 5: 文档更新**

- `docs/electron-backend-protocol-v2.md`：新增 §12 三种语音与 TTS 播放契约；§9.1 错误码目录 +7（VOICE_SESSION_CONFLICT/VOICE_SESSION_NOT_FOUND/VOICE_RECORDING_NOT_FOUND/VOICE_RECORDING_TOO_LONG/DICTATION_NOT_FOUND/STT_UNAVAILABLE/PLAYBACK_NOT_FOUND）；§1 features 行（dictation/voice_recording/voice_realtime）。
- `docs/electron-backend-progress.md`：批次 6 节 + 验收结论。
- `docs/electron-backend-security.md`：dictation/录音临时音频不落盘说明。

- [ ] **Step 6: Commit**

```bash
git add core/ docs/ && git commit -m "feat(voice): features 声明 + audio.cancel 路由 + 批次 6 文档（BE-060~063）"
```

---

### Task 10: 全量回归 + 最终 commit

**Files:**
- 全仓回归：`make test-quick` + `make test-normal` + `make lint` + `make check-types`

- [ ] **Step 1: 回归**

Run: `make test-quick`（conda Elysia）
Expected: 全部 PASS。修复任何回归（重点：legacy 无会话 BINARY 自动提交、`_route_v2_text`、ws_handler 现有测试）。

Run: `make lint` + `make check-types`
Expected: 无 lint 错误、无契约漂移（`contract.generated.ts` 需重新生成并提交——`make gen-types` 后 diff 入 commit）。

- [ ] **Step 2: 运行全量**

Run: `make test-normal`
Expected: 全部 PASS（约 5min）。

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "test: 批次 6 全量回归收口（legacy 语音不变 + 三模式 + TTS 播放控制）"
```
