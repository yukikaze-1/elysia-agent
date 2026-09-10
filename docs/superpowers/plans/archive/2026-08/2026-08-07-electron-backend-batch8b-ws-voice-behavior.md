# Electron Backend Batch 8b Implementation Plan — WS/语音行为修复

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 audit D 簇 WS/语音行为 13 条发现（5 important + 8 minor），落地 spec `docs/superpowers/specs/archive/2026-08/2026-08-07-electron-backend-batch8b-ws-voice-behavior.md` 的 D1-D6 决策。

**Architecture:** 六个相互独立的修复面：① loop 中断状态正确性（D4）、② ws_handler 连接行为（D1/D2/§3.7/§3.8）、③ voice_sessions 语音状态机（D3/D6/§3.10）、④ tts_adapter 播放控制（§3.9/§3.11）、⑤ loop 空响应段契约（D5）、⑥ generation 上下文版本字段（D5）。核心难点 D3（STT 后台解耦）以「派发 wrapper + 双锁串行化 + epoch 代际隔离」实现，不改 legacy v1 路径。

**Tech Stack:** Python 3.11+ / asyncio / aiohttp / pydantic（frontend_contract）/ pytest（unit + service marker）。

## Global Constraints

- **禁相对导入**（ruff TID252）；直接引子模块（`from core.input.voice_sessions import ...`），不绕包重导出。
- **代码注释用中文**；README 用中文。
- **Agent 公开接口纪律**：跨模块消费禁止 `agent._xxx`，一律走公开 property；新增只读接口经 `agent.cognitive` 既有转发链暴露，**不新增 Agent 顶层 property**（spec §2）。
- **conda 环境 Elysia** 测试；新测试带 marker（unit/service/integration/slow）。
- **契约一致性**：WS ACK/error 帧用错误码目录标准码（protocol-v2 §9.1：`INTERNAL`/`QUEUE_FULL`/`CONTRACT_VALIDATION`/`DICTATION_NOT_FOUND`/`PLAYBACK_NOT_FOUND`），不发明新码。
- **敏感纪律（BE-081）**：日志/ACK 只记文本长度与 message_id，不记正文。
- **v1 兼容硬约束**：v1 客户端 legacy 语音 / MessageObj 帧路径逐字节不动；所有行为变更只在 v2 侧生效。
- **子代理纪律**：implementer **串行**执行（8a 教训：并行 implementer 共享工作树污染 git），每 task 独立 commit。

---

## File Structure

- `core/cognitive/loop.py` — D4 `cancel_current`（Task 1）、D1 `current_generation_user_message_id` property（Task 9）、D5 `_finalize_turn` 兜底 + GenerationContext 构造传 protocol_version（Task 10）
- `core/cognitive/generation.py` — D5 `protocol_version` 字段（Task 10）
- `core/input/ws_handler.py` — D2 BINARY 门（Task 2）、§3.8 空文本 ACK（Task 7）、§3.7 队列满 ACK + 异常变体 + `sent_message_ids` 接线（Task 8）、D1 断连归属 + `handle_json_message`/`_handle_voice_commit` threading（Task 9）
- `core/input/voice_sessions.py` — D6 emotion 隔离 + §3.10 ID 校验（Task 5）、D3 派发机制 + `_on_segment` 段级 emotion + `_emitted_message_ids` + drain（Task 6）
- `core/adapter/tts_adapter.py` — §3.11 `_wav_info`（Task 3）、§3.9 `cancel_stream`（Task 4）
- 测试：`core/cognitive/tests/test_interrupt_source.py`（新，Task 1）、`core/cognitive/tests/test_v2_empty_response.py`（新，Task 10）、`core/input/tests/test_v2_ws.py`（Task 2/7/8/9 增补）、`core/input/tests/test_voice_sessions.py`（Task 5/6 增补）、`core/adapter/tests/test_tts_adapter.py`/`test_tts_adapter_control.py`（Task 3/4 增补）

---

### Task 1: D4 — `cancel_current` 只在实际取消任务时记录 source

**Files:**
- Modify: `core/cognitive/loop.py:313-334`
- Test: `core/cognitive/tests/test_interrupt_source.py`（新文件）

**Interfaces:**
- Consumes: `CognitiveLoop._interrupt_source` / `_current_task`（既有内部字段）。
- Produces: `cancel_current(source=None) -> bool` 语义变化——source 仅在任务被实际取消时写入 `_interrupt_source`；Task 9 的断连归属检查与 Task 10 依赖此语义（IDLE_BG 窗口断线不再留 sticky source）。

- [ ] **Step 1: 写失败测试**

```python
# core/cognitive/tests/test_interrupt_source.py
"""D4：_interrupt_source 只在实际取消任务时记录（spec §3.4）。"""
import asyncio
from unittest.mock import MagicMock

import pytest

from core.cognitive.loop import CognitiveLoop

pytestmark = pytest.mark.unit


def _make_loop() -> CognitiveLoop:
    return CognitiveLoop(
        llm=MagicMock(), fsm=MagicMock(), event_bus=MagicMock(),
        output=MagicMock(), prompt=MagicMock(), session_resolver=MagicMock(),
    )


@pytest.mark.asyncio
async def test_source_recorded_when_task_actually_cancelled():
    loop = _make_loop()

    async def _pending():
        await asyncio.sleep(10)

    task = asyncio.create_task(_pending())
    loop._current_task = task
    assert loop.cancel_current(source="websocket") is True
    assert loop._interrupt_source == "websocket"
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_no_source_recorded_when_nothing_cancelled():
    """IDLE_BG 窗口：task 已 done → cancel_current 不取消也不留 sticky source。"""
    loop = _make_loop()
    loop._interrupt_source = None

    async def _noop():
        pass

    done = asyncio.create_task(_noop())
    await done
    loop._current_task = done
    assert loop.cancel_current(source="disconnect") is False
    assert loop._interrupt_source is None  # 旧代码：先记 source 再查 done → 泄漏


@pytest.mark.asyncio
async def test_no_source_when_no_task():
    loop = _make_loop()
    loop._current_task = None
    assert loop.cancel_current(source="disconnect") is False
    assert loop._interrupt_source is None
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest core/cognitive/tests/test_interrupt_source.py -v --asyncio-mode=auto`
Expected: `test_no_source_recorded_when_nothing_cancelled` FAIL——旧代码 `_interrupt_source == "disconnect"`（task 已 done 仍先记录 source）。

- [ ] **Step 3: 最小实现**

`core/cognitive/loop.py` 的 `cancel_current` 改为：
```python
    def cancel_current(self, source: str | None = None) -> bool:
        """取消当前认知任务（如有）。返回 True 表示确实取消了任务。

        Args:
            source: 中断来源（vad / disconnect / button 等），批次 3 起贯通到
                取消路径——存到 _interrupt_source 供 _handle_interrupt 消费，
                Task 9 mark 拆分据此区分「语音打断」与「断线」。

        批次 3 review 修复（F6）的「只增不减」语义保留，但记录条件收窄到
        「实际取消了任务」（D4）：task 已 done（IDLE_BG 窗口断线）不取消任何
        东西，也就没有中断来源可消费——不留 sticky source，否则下一回合用户
        取消被 disconnect 规则锁死成 interrupted。
        """
        if self._current_task is not None and not self._current_task.done():
            if source is not None:
                if self._interrupt_source != "disconnect" or source == "disconnect":
                    self._interrupt_source = source
            self._current_task.cancel()
            self._current_task = None
            return True
        self._current_task = None
        return False
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest core/cognitive/tests/test_interrupt_source.py -v --asyncio-mode=auto`
Expected: PASS（3 passed）。

- [ ] **Step 5: 回归 + commit**

⚠️ **既有测试更新**：`core/cognitive/tests/test_session_resolver.py::test_cancel_current_preserves_disconnect_source`（~174）断言「无在途 task 时 `cancel_current("disconnect")` 仍记录 source」——这是 D4 要消除的旧行为。把该测试改为断言「无 task → 不记录 source」（对齐新语义），并补一个「有在途 task → disconnect 不被后续非 disconnect 覆盖」的保留测试（沿用其既有「只增不减」场景，`_current_task` 用真实 pending task）。

Run: `python -m pytest core/cognitive/tests/test_interrupt_source.py core/cognitive/tests/test_session_resolver.py -q`
Run: `python -m pytest core/cognitive/tests/ -q`（确认 loop 相关既有测试未回归）
Run: `make lint`
Commit:
```bash
git add core/cognitive/loop.py core/cognitive/tests/test_interrupt_source.py core/cognitive/tests/test_session_resolver.py
git commit -m "fix(batch8b): D4 cancel_current 只在实际取消任务时记录 _interrupt_source（IDLE_BG 断线不留 sticky source）

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: D2 — v2 BINARY 全程禁 legacy（无语音会话丢弃）

**Files:**
- Modify: `core/input/ws_handler.py:303-310`（BINARY 分支）
- Test: `core/input/tests/test_v2_ws.py`（新增 `TestV2BinaryNoLegacy` 类）

**Interfaces:**
- Consumes: `handle_ws` 作用域 `protocol_version`（line 120）、`voice_mgr`（恒非 None）、`audio_buf`（STT 启用时创建）。
- Produces: v2 连接 BINARY 帧仅在 `voice_mgr.has_active_session()` 时喂给 `feed_pcm`；无会话且 `protocol_version >= 2` → 丢弃。Task 9 不依赖本任务，但同文件顺序执行。

- [ ] **Step 1: 写失败测试**

在 `core/input/tests/test_v2_ws.py` 末尾追加：
```python
import struct


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
        agent._stt_enabled = True               # noqa: SLF001
        return agent, calls

    @pytest.mark.asyncio
    async def test_v1_binary_drives_legacy_stt(self, aiohttp_client, fresh_idempotency):
        """v1 对照：同音频 legacy 路径触发 STT——harness 有效（未改路径行为保持）。"""
        from core.input.server import create_app
        agent, calls = self._make_agent()
        await agent.start()
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
        app = create_app(agent, serve_static=False)
        client = await aiohttp_client(app)
        try:
            ws = await _drain(client, {"X-Protocol-Version": "2"})
            await self._send_speech_and_wait(ws)
            assert calls == []  # v2 无语音会话 → 不落 legacy，STT 零调用
            await ws.close()
        finally:
            await agent.stop()
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest core/input/tests/test_v2_ws.py::TestV2BinaryNoLegacy -v --asyncio-mode=auto`
Expected: `test_v2_binary_without_voice_session_never_transcribes` FAIL——旧代码 v2 无会话时 BINARY 落 `audio_buf.feed` → 响度语音 + 静音触发段 → STT 被调用（`calls` 非空）。`test_v1_binary_drives_legacy_stt` 通过（对照，未改路径）。

- [ ] **Step 3: 最小实现**

`core/input/ws_handler.py` BINARY 分支（~303-310）改为：
```python
            elif msg.type == WSMsgType.BINARY:
                # 批次 6：有活动语音会话 → 喂给会话管理器（RECORDING/REALTIME/DICTATION 分流）；
                # 批次 8b（D2）：v2 连接 BINARY 只在 voice_mgr 会话内有效——无会话 → 丢弃，
                # 不落 legacy audio_buf（否则 RECORDING 复位残留帧绕过 commit 自动成轮次）；
                # legacy VAD 自动提交仅 v1（现状逐字节不变）。voice_mgr 恒非 None，判空仅兜底。
                if voice_mgr is not None and voice_mgr.has_active_session():
                    await voice_mgr.feed_pcm(msg.data)
                elif protocol_version < 2 and audio_buf and agent.stt_enabled:
                    await audio_buf.feed(msg.data)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest core/input/tests/test_v2_ws.py::TestV2BinaryNoLegacy -v --asyncio-mode=auto`
Expected: PASS。

- [ ] **Step 5: 回归 + commit**

Run: `python -m pytest core/input/tests/test_voice_ws.py core/input/tests/test_audio_buffer.py -q`
Run: `make lint`
Commit:
```bash
git add core/input/ws_handler.py core/input/tests/test_v2_ws.py
git commit -m "fix(batch8b): D2 v2 BINARY 全程禁 legacy——无语音会话丢弃，RECORDING 复位残留帧不再绕过 commit

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: §3.11 — `_wav_info` chunk 遍历解析

**Files:**
- Modify: `core/adapter/tts_adapter.py:57-68`
- Test: `core/adapter/tests/test_tts_adapter.py`（追加）

**Interfaces:**
- Consumes: `_wav_info(audio: bytes)`（模块级函数，返回 `(sample_rate, duration_ms)`）。
- Produces: 同签名；含额外 chunk（LIST/fact/cue）的 WAV 正确解析；标准 44 字节头结果不变。

- [ ] **Step 1: 写失败测试**

在 `core/adapter/tests/test_tts_adapter.py` 追加：
```python
# §3.11：_wav_info 遍历 chunk 表——含额外 chunk 的 WAV 正确解析（spec §3.11）
import struct

from core.adapter.tts_adapter import _wav_info


def _wav_standard(sample_rate=16000, byte_rate=32000, data_size=3200):
    """标准 44 字节 WAV 头 + data。"""
    header = b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVE"
    fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, byte_rate, 2, 16)
    data = b"data" + struct.pack("<I", data_size)
    return header + fmt + data + b"\x00" * data_size


def _wav_with_extra_chunks(sample_rate=16000, byte_rate=32000, data_size=3200):
    """RIFF 头后先插 LIST/fact 额外 chunk，再 data——非 44 字节头。"""
    body = b"RIFF" + b"WAVE"
    # 直接构造：RIFF 头 12 字节 + LIST chunk + fmt chunk + data chunk
    def chunk(cid, payload):
        padded = payload if len(payload) % 2 == 0 else payload + b"\x00"
        return cid + struct.pack("<I", len(payload)) + padded
    list_payload = b"INFO" + b"\x00" * 20
    fact_payload = struct.pack("<I", 400)
    fmt_payload = struct.pack("<HHIIHH", 1, 1, sample_rate, byte_rate, 2, 16)
    total = 4 + len(chunk(b"LIST", list_payload)) + len(chunk(b"fact", fact_payload)) \
        + len(chunk(b"fmt ", fmt_payload)) + len(chunk(b"data", b""))
    prefix = b"RIFF" + struct.pack("<I", total) + b"WAVE"
    return prefix + chunk(b"LIST", list_payload) + chunk(b"fact", fact_payload) \
        + chunk(b"fmt ", fmt_payload) + b"data" + struct.pack("<I", data_size) \
        + b"\x00" * data_size


def test_wav_info_standard_header():
    sr, dur = _wav_info(_wav_standard())
    assert sr == 16000
    assert dur == int(3200 * 1000 / 32000)  # 100ms


def test_wav_info_extra_chunks_correct_duration():
    """含 LIST/fact 额外 chunk → durationMs 不再错（旧固定偏移 40 会读错 data size）。"""
    sr, dur = _wav_info(_wav_with_extra_chunks())
    assert sr == 16000
    assert dur == int(3200 * 1000 / 32000)


def test_wav_info_non_wav_returns_none():
    assert _wav_info(b"\x00" * 64) == (None, None)
    assert _wav_info(b"") == (None, None)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest core/adapter/tests/test_tts_adapter.py -q`
Expected: `test_wav_info_extra_chunks_correct_duration` FAIL（旧固定偏移 40 读到错误 data size → durationMs 错）。

- [ ] **Step 3: 最小实现**

`core/adapter/tts_adapter.py` 的 `_wav_info` 替换为：
```python
def _wav_info(audio: bytes) -> tuple[int | None, int | None]:
    """解析 WAV header → (sample_rate, duration_ms)；非 WAV/异常返回 (None, None)。

    遍历 RIFF chunk 表定位 fmt/data（不假设标准 44 字节头）——含额外 chunk
    （LIST/fact/cue）的 WAV 同样正确解析（spec §3.11）。
    """
    try:
        if len(audio) < 12 or audio[:4] != b"RIFF":
            return None, None
        sample_rate: int | None = None
        byte_rate: int | None = None
        offset = 12                      # 跳过 RIFF 头（12 字节），进入 chunk 表
        while offset + 8 <= len(audio):
            chunk_id = audio[offset:offset + 4]
            size = struct.unpack("<I", audio[offset + 4:offset + 8])[0]
            if chunk_id == b"fmt " and offset + 20 <= len(audio):
                sample_rate = struct.unpack("<I", audio[offset + 12:offset + 16])[0]
                byte_rate = struct.unpack("<I", audio[offset + 16:offset + 20])[0]
            elif chunk_id == b"data":
                duration_ms = int(size * 1000 / byte_rate) if byte_rate else None
                return sample_rate, duration_ms
            offset += 8 + size + (size & 1)   # chunk 数据 + 奇偶对齐
        return sample_rate, None
    except Exception:
        return None, None
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest core/adapter/tests/test_tts_adapter.py -q`
Expected: PASS（3 个新测试 + 既有全绿）。

- [ ] **Step 5: 回归 + commit**

Run: `python -m pytest core/adapter/tests/test_tts_adapter.py -q`（新测试 + 既有全绿）
Run: `make lint`
Commit:
```bash
git add core/adapter/tts_adapter.py core/adapter/tests/test_tts_adapter.py
git commit -m "fix(batch8b): §3.11 _wav_info 遍历 chunk 表定位 fmt/data，弃固定偏移（额外 chunk WAV 得正确 durationMs）

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: §3.9 — `cancel_stream` 以「正在转发」判定

**Files:**
- Modify: `core/adapter/tts_adapter.py:225-231`
- Test: `core/adapter/tests/test_tts_adapter_control.py`（追加）

**Interfaces:**
- Consumes: `TTSStreamManager._task_by_sid` / `_audio_forwarding`（既有字段）。
- Produces: `cancel_stream(stream_id) -> bool`——仅 `sid in _audio_forwarding` 返回 True（任务补 audio_cancelled）；已 audio_end → False（客户端 PLAYBACK_NOT_FOUND）。调用方 `ws_handler.py:976-978` 不变。

- [ ] **Step 1: 写失败测试**

在 `core/adapter/tests/test_tts_adapter_control.py` 追加：
```python
# §3.9：cancel_stream 以「正在转发」判定——已 audio_end 的任务返回 False（spec §8.2）
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.adapter.tts_adapter import TTSStreamManager


def _make_mgr():
    tts = MagicMock()
    tts.stream_format = "wav"
    tts.get_preprocess.return_value = None
    tts.synthesize = AsyncMock(return_value=b"RIFFWAVE" + b"\x00" * 100)
    output = MagicMock()
    output._broadcast = AsyncMock()
    output.send = AsyncMock()
    for name in ("audio_start", "audio_chunk", "audio_end", "audio_cancelled",
                 "typing", "turn_completed", "turn_cancelled"):
        setattr(output, name, AsyncMock())
    mgr = TTSStreamManager(tts=tts, output=output, character_name="测试")
    return mgr, output


@pytest.mark.asyncio
async def test_cancel_after_audio_end_returns_false():
    """audio_end 已发（_audio_forwarding 已 discard）的任务 → False（PLAYBACK_NOT_FOUND）。"""
    mgr, _ = make_mgr = _make_mgr()
    # 构造一个「存活但已 audio_end」的任务：sid=1 已从 _audio_forwarding 移除
    async def _hanging():
        await asyncio.sleep(10)
    task = asyncio.create_task(_hanging())
    mgr._task_by_sid = {1: task}
    mgr._audio_forwarding = set()          # 已 audio_end
    assert mgr.cancel_stream(1) is False
    assert task.done() is False            # 未取消——客户端应收到 PLAYBACK_NOT_FOUND
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_cancel_while_forwarding_returns_true():
    """正在转发（_audio_forwarding 含 sid）→ True，任务被取消。"""
    mgr, _ = make_mgr = _make_mgr()
    async def _hanging():
        await asyncio.sleep(10)
    task = asyncio.create_task(_hanging())
    mgr._task_by_sid = {1: task}
    mgr._audio_forwarding = {1}
    assert mgr.cancel_stream(1) is True
    await asyncio.sleep(0)        # task.cancel() 后须让出事件循环，done() 才置位
    assert task.done()
    with pytest.raises(asyncio.CancelledError):
        await task
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest core/adapter/tests/test_tts_adapter_control.py -q`
Expected: `test_cancel_after_audio_end_returns_false` FAIL——旧代码以「task 存活」判定返回 True（且任务被取消后无 audio_cancelled）。

- [ ] **Step 3: 最小实现**

`core/adapter/tts_adapter.py` 的 `cancel_stream` 改为：
```python
    def cancel_stream(self, stream_id: int) -> bool:
        """取消指定播放实例（BE-063）；正在转发（audio_start 后 audio_end 前）返回
        True（任务补 audio_cancelled），已 audio_end / 不存在返回 False（PLAYBACK_NOT_FOUND，
        spec §8.2）。判定与 CancelledError 分支是否补 audio_cancelled 对齐——用
        _audio_forwarding 而非 task 存活，否则 audio_end 后的 emoji/finally 窗口
        返回 True 却无事件（spec §3.9）。"""
        task = self._task_by_sid.get(stream_id)
        if task is None or task.done():
            return False
        if stream_id not in self._audio_forwarding:
            return False           # 已 audio_end（末尾 emoji/finally 窗口）——不再补事件
        task.cancel()
        return True
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest core/adapter/tests/test_tts_adapter_control.py -q`
Expected: PASS（新 2 测试 + 既有全绿）。

- [ ] **Step 5: 回归 + commit**

Run: `python -m pytest core/adapter/tests/test_tts_adapter_control.py core/adapter/tests/test_tts_adapter.py -q`（新 2 测试 + 既有 cancel 语义测试全绿——尤其 `test_cancel_stream_emits_audio_cancelled` 仍通过）
Run: `make lint`
Commit:
```bash
git add core/adapter/tts_adapter.py core/adapter/tests/test_tts_adapter_control.py
git commit -m "fix(batch8b): §3.9 cancel_stream 以 _audio_forwarding 判定——audio_end 后返回 False（PLAYBACK_NOT_FOUND），不再无声成功

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: D6 + §3.10 — dictation emotion 隔离 + ID 校验

**Files:**
- Modify: `core/input/voice_sessions.py`
- Test: `core/input/tests/test_voice_sessions.py`（追加）

**Interfaces:**
- Consumes: `agent.stt` 后端槽位、`Agent.stt_transcribe`（写共享 emotion）——本任务新增 `_transcribe_no_emotion` 绕开写点。
- Produces: `VoiceSessionManager._transcribe_no_emotion(wav)`（D3 Task 6 消费）；`stop_dictation`/`cancel_dictation` 的 clientRequestId 校验（§3.10）；`_emotion_snapshot`/`_restore_emotion` 删除（Task 6 依赖：不再需要恢复）。

- [ ] **Step 1: 写失败测试**

在 `core/input/tests/test_voice_sessions.py` 追加：
```python
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
    await mgr.feed_pcm(_loud(1600))
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
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest core/input/tests/test_voice_sessions.py -q`
Expected: `test_dictation_does_not_touch_shared_emotion` FAIL（旧代码 dictation 经 `agent.stt_transcribe` 写共享 state → 进行中已是 HAPPY，断言红）；`test_stop_dictation_wrong_id_rejected`/`test_cancel_dictation_wrong_id_rejected` FAIL（旧代码只查 mode）。

- [ ] **Step 3: 最小实现**

`core/input/voice_sessions.py` + **测试桩更新（`test_voice_sessions.py`）**：
0. **测试桩更新（先行）**：`_transcribe_no_emotion` 接线后 dictation 走 `agent.stt.transcribe`，而现有 `FakeAgent.__init__` 的 `self.stt = object()` 无 `.transcribe` → AttributeError。给 `test_voice_sessions.py` 加一个假 STT 后端并替换 `FakeAgent.stt`：
   ```python
   class _FakeSTTBackend:
       """返回 _last_text 的假 STT——供 _transcribe_no_emotion 调 agent.stt.transcribe。"""
       def __init__(self, agent):
           self._agent = agent
       async def transcribe(self, wav):
           return self._agent._last_text
   ```
   并把 `FakeAgent.__init__` 的 `self.stt = object()` 改为 `self.stt = _FakeSTTBackend(self)`（既有测试仍兼容——`agent.stt is not None`、`agent.stt_transcribe` 不变）。
1. 新增方法（放在 `_stt_available` 附近）：
```python
    async def _transcribe_no_emotion(self, wav: bytes) -> str:
        """转写但不写共享 stt_emotion_state（D6：dictation 情绪隔离）。

        dictation 只回文字，本就不该触碰 agent 级情绪——隔离后无需快照/恢复，
        也不抹掉其他连接在 dictation 窗口内写入的 emotion/events。
        """
        backend = getattr(self._agent, "stt", None)
        if backend is None:
            return ""
        return await backend.transcribe(wav)
```
2. `start_dictation` 删除 `_emotion_snapshot` 快照段（~330-334），并将 AudioBuffer 的 `stt_transcribe=` 改为 **`self._transcribe_no_emotion`**（直接接线——dictation 转写不再写共享 emotion；Task 6 再套 dispatch wrapper）：
   ```python
        self._buffer = AudioBuffer(
            stt_transcribe=self._transcribe_no_emotion,   # D6：不写共享 emotion
            aggressiveness=3,
            silence_duration=self._silence_duration,
            on_interrupt=None,  # DICTATION 不接（spec §3.2 NEW-K）
            on_transcription=self._on_segment,
        )
   ```
3. 删除 `_restore_emotion` 方法（165-175）与 `__init__` 的 `_emotion_snapshot`（line 79），并移除 `stop_dictation`/`cancel_dictation`/`notify_stt_disabled`/`close` 四处的 `self._restore_emotion()` 调用。
4. `stop_dictation`/`cancel_dictation` 校验并入（§3.10）：
```python
    async def stop_dictation(self, client_request_id: str) -> None:
        if (self._mode != VoiceMode.DICTATION
                or self._dict_request_id != client_request_id):
            raise VoiceSessionError(ERR_DICTATION_NOT_FOUND, "无活动转写")
        if self._buffer:
            await self._buffer.flush()
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
```
（`stop_dictation` 的 `_restore_emotion` 已删。`_transcribe_no_emotion` 在此**直接接线**（同步 STT 已隔离 emotion）——Step 1 的 D6 测试断言放 dictation 进行中，旧代码写 HAPPY 时红、新代码不写时绿；Task 6 再改 `_make_dispatch_stt(self._transcribe_no_emotion)` 上后台派发，语义不变。）

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest core/input/tests/test_voice_sessions.py -q`
Expected: PASS（新 3 测试 + 既有全绿——注意既有 dictation 测试若断言恢复行为需同步调整，见 Step 5）。

- [ ] **Step 5: 回归检查 + commit**

Run: `python -m pytest core/input/tests/test_voice_sessions.py core/input/tests/test_voice_ws.py -q`
⚠️ **既有测试语义调整（`test_voice_sessions.py`）**：D6 后 `_restore_emotion` 删除、dictation 不再写共享 emotion——`test_dictation_restores_stt_emotion_state`（~239）与 `test_dictation_stt_disabled_restores_emotion`（~266）断言「dictation 结束恢复快照」不再有意义（D6 语义是「dictation 从不动共享 state」）。把这两个测试改为断言「dictation 全程共享 state 不变」（用 `_EmotionWritingAgent` 验证不写 + 不恢复）。
Commit:
```bash
git add core/input/voice_sessions.py core/input/tests/test_voice_sessions.py
git commit -m "fix(batch8b): D6 dictation emotion 隔离（不写共享 state，删快照/恢复）+ §3.10 dictation ID 校验

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: D3 — STT 后台解耦（派发机制 + 接线 + drain）★ 最大任务

**Files:**
- Modify: `core/input/voice_sessions.py`
- Test: `core/input/tests/test_voice_sessions.py`（新增 `TestDispatchStt` 类）

**Interfaces:**
- Consumes: Task 5 的 `_transcribe_no_emotion`；`AudioBuffer(stt_transcribe=..., on_transcription=...)` 构造（`audio_buffer.py:92-121`，`stt_transcribe`/`on_transcription` 参数在 99-100）；`agent.stt_transcribe` / `agent.stt_emotion_state`。
- Produces: `VoiceSessionManager._make_dispatch_stt(transcriber)` / `_run_dispatch_stt(wav, seq, epoch, transcriber)` / `_commit_ready_stt()` / `_drain_dispatch_stt()`；`_on_segment(text, emotion=None, events=None)` 新签名（段级 emotion + `_emitted_message_ids` 归属）；`start_realtime`/`start_dictation` 注入 dispatch wrapper；`stop_session`/`stop_dictation` 加 drain；`__init__`/`_teardown_session` 增 `_stt_seq/_stt_epoch/_stt_pending/_stt_commit_seq/_stt_tasks/_stt_stt_lock/_stt_commit_lock`；`_emitted_message_ids` set + `emitted_message_ids` property（Task 9 消费）。

- [ ] **Step 1: 写失败测试**

在 `core/input/tests/test_voice_sessions.py` 追加：
```python
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
                return "第一段"
            await self.release_b.wait()
            self._agent.stt_emotion_state["emotion"] = "SAD"
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
    # 反序放行：先 B（SAD）后 A（HAPPY）——锁保证各自捕获本段 emotion
    agent._two.release_b.set()
    await asyncio.sleep(0.1)
    agent._two.release_a.set()
    await asyncio.sleep(0.1)
    turns = [s for s in emitted if s.get("type") == "voice_session.turn"]
    assert [t["turnId"] for t in turns] == [1, 2]       # 提交锁 + seq 顺序
    assert [p.emotion for p in bus_posts] == ["HAPPY", "SAD"]  # 各自本段 emotion


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
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest core/input/tests/test_voice_sessions.py::TestDispatchStt -q --asyncio-mode=auto`
Expected: 全部 FAIL——`_make_dispatch_stt`/`_stt_tasks`/`_drain_dispatch_stt` 不存在（AttributeError）。

- [ ] **Step 3: 最小实现**

`core/input/voice_sessions.py`：
1. `__init__` 增：
```python
        # D3（spec §3.3）：STT 后台派发状态——seq 单调、epoch 代际、pending 缓冲、任务集合、双锁
        self._stt_seq = 0
        self._stt_epoch = 0                 # 会话代际——teardown 时 +=1，绝不归零（B1）
        self._stt_pending: dict[int, tuple[str, str, list[str]]] = {}
        self._stt_commit_seq = 0
        self._stt_tasks: set[asyncio.Task] = set()
        self._stt_stt_lock = asyncio.Lock()
        self._stt_commit_lock = asyncio.Lock()
        self._emitted_message_ids: set[str] = set()   # D1 归属（Task 9 消费）
```
2. `_teardown_session` 增（与既有 `_turn_id/_segment_index` 重置并列）：
```python
        self._stt_seq = 0
        self._stt_epoch += 1                 # 代际递增——旧会话在途任务提交前 epoch 失配被丢弃
        self._stt_pending = {}
        self._stt_commit_seq = 0
        self._stt_tasks = set()
```
3. 新增方法（放在 `feed_pcm` 前）：
```python
    def _make_dispatch_stt(self, transcriber):
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
                                transcriber) -> None:
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
```
4. `_on_segment` 改签名 + REALTIME 分支段级 emotion + 归属：
```python
    async def _on_segment(self, text: str, emotion: str | None = None,
                          events: list[str] | None = None) -> None:
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
            if emotion is None:      # 缺省回退读共享 state（legacy AudioBuffer 回调路径）
                es = getattr(self._agent, "stt_emotion_state", None) or {}
            else:
                es = {"emotion": emotion, "events": events or []}
            message_id = new_message_id()
            self._agent.idle.touch()
            self._agent.bus.post(self._build_user_input(text, es, message_id))
            self._emitted_message_ids.add(message_id)   # D1 归属（Task 9）
            await self._emit(VoiceSessionTurnMessage(
                sessionId=self._session_id, turnId=self._turn_id,
                messageId=message_id, conversationId=self._conv_id, text=text).wire_dict())
        elif self._mode == VoiceMode.DICTATION:
            from frontend_contract import DictationPartialMessage
            self._dict_text.append(text)
            await self._emit(DictationPartialMessage(text=text).wire_dict())

    @property
    def emitted_message_ids(self) -> set[str]:
        """本连接语音段提交的用户消息 ID 集合（D1 断连归属判定，Task 9 消费）。"""
        return set(self._emitted_message_ids)
```
5. `start_realtime` 的 AudioBuffer 构造 `stt_transcribe=` 改为 `self._make_dispatch_stt(self._agent.stt_transcribe)`；`start_dictation` 改为 `self._make_dispatch_stt(self._transcribe_no_emotion)`。
6. `stop_session` 加 `await self._drain_dispatch_stt()`（flush 后、teardown 前）：
```python
    async def stop_session(self, session_id: str) -> None:
        self._require_session(session_id)
        if self._buffer:
            await self._buffer.flush()          # 派发最后一段（feed 立即返回）
        await self._drain_dispatch_stt()        # 等待在途段完成再 teardown（残段仍提交）
        self._teardown_session()
        await self._emit(VoiceSessionEndedMessage(sessionId=session_id, reason="stopped").wire_dict())
```
7. `stop_dictation` 加 `await self._drain_dispatch_stt()`（Task 5 已加 ID 校验）：
```python
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
```
8. `notify_stt_disabled` 的 dictation 分支：teardown 前不再 `_restore_emotion()`（D6 已删）——保持其余。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest core/input/tests/test_voice_sessions.py -q --asyncio-mode=auto`
Expected: PASS（新 4 测试 + 既有全绿）。

- [ ] **Step 5: 回归 + commit**

⚠️ 既有 `test_voice_sessions.py`/`test_voice_ws.py` 中同步驱动 REALTIME/DICTATION 分段后立即断言 partial/turn 的测试，需在断言前补 `await asyncio.sleep(0)` 让后台任务提交（派发后事件循环切换）。
Run: `python -m pytest core/input/tests/test_voice_sessions.py core/input/tests/test_voice_ws.py -q`
Run: `make lint`
Commit:
```bash
git add core/input/voice_sessions.py core/input/tests/test_voice_sessions.py
git commit -m "fix(batch8b): D3 STT 后台解耦——派发 wrapper + 双锁 + epoch 代际隔离 + drain（cancel 丢弃残段/stop 等待在途段/dictation 不丢段）

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: §3.8 — v2 空文本 ACK

**Files:**
- Modify: `core/input/ws_handler.py:695-712`（`handle_json_message` text 分支）
- Test: `core/input/tests/test_v2_ws.py`（追加）

**Interfaces:**
- Consumes: `_is_v2(agent, ws)`（line 365）、`_send_ack`（line 397）。
- Produces: v2 空文本 → `rejected CONTRACT_VALIDATION`；v1 空文本行为不变（静默）。Task 8 同文件顺序执行。

- [ ] **Step 1: 写失败测试**

在 `core/input/tests/test_v2_ws.py` 追加：
```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest core/input/tests/test_v2_ws.py::TestV2EmptyTextAck -v --asyncio-mode=auto`
Expected: FAIL——旧代码空文本被条件跳过，收不到 ack（`got is None`）。

- [ ] **Step 3: 最小实现**

`core/input/ws_handler.py` `handle_json_message` text 分支（~695-712）改嵌套（**`list` 多模态分支原样保留**）：
```python
    elif msg_type == "text":
        text_content = data.get("content", "")
        if isinstance(text_content, str):
            if text_content.strip() or data.get("stickerId") or data.get("attachments"):
                # 批次 2 v2 路由（握手 protocol_version=2）：ACK + 幂等 + 预分配 messageId。
                # 批次 7：入口门放宽——sticker/附件-only 消息（content 可空）也进 v2 路由。
                if await _route_v2_text(agent, ws, text_content.strip(), data):
                    return
                await agent.process_message(text=text_content.strip())
                # BE-081 敏感纪律：只记文本长度 + 客户端消息 ID，不记正文
                logger.debug("WS 收到文本 (len=%d, clientMessageId=%s)",
                             len(text_content.strip()),
                             data.get("clientMessageId", ""))
            elif _is_v2(agent, ws):
                # 批次 8b（§3.8）：v2 空文本 → rejected CONTRACT_VALIDATION（不再静默丢弃）
                await _send_ack(ws, "", data.get("clientRequestId", ""),
                                data.get("clientMessageId"), data.get("conversationId"),
                                "rejected",
                                error={"code": "CONTRACT_VALIDATION",
                                       "message": "消息内容为空", "retryable": False})
        elif isinstance(text_content, list):
            # 多模态 content-as-parts 分支原样保留（不动）
            from core.schema.adapters.message_adapter import content_text, parts_from_dicts
            await agent.process_message(text=content_text(parts_from_dicts(text_content)), user_name=agent.user_name, content=text_content)
            logger.debug("WS 收到多模态消息")
```
- `_is_v2(agent, ws)` 已存在（line 365）。
- **回归面**：`elif isinstance(text_content, list)` 多模态路径必须保留（Step 5 跑既有多模态测试确认）。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest core/input/tests/test_v2_ws.py::TestV2EmptyTextAck -v --asyncio-mode=auto`
Expected: PASS。

- [ ] **Step 5: 回归 + commit**

Run: `python -m pytest core/input/tests/test_v2_ws.py -q`（空文本 + 既有 v2 测试全绿）
Run: `python -m pytest core/input/tests/test_session_ws.py -q`（多模态 content-as-parts 既有测试——确认 list 分支保留）
Run: `make lint`
Commit:
```bash
git add core/input/ws_handler.py core/input/tests/test_v2_ws.py
git commit -m "fix(batch8b): §3.8 v2 空文本发 rejected CONTRACT_VALIDATION（不再静默丢弃）

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: §3.7 — 队列满 ACK 对齐 + process_message 异常变体

**Files:**
- Modify: `core/input/ws_handler.py:468-611`（`_route_v2_text`）
- Test: `core/input/tests/test_v2_ws.py`（追加 `TestQueueFullAck`）

**Interfaces:**
- Consumes: `store`（idempotency）、`_send_ack`、`agent.process_message`（返回 bool）、`agent.resolve_session`。
- Produces: `_route_v2_text(agent, ws, text, data, sent_message_ids=None)` 新参数——accepted 时 `sent_message_ids.add(message_id)`；纯文本路径检查返回值（False → 删 entry + rejected QUEUE_FULL）；media/纯文本路径 process_message 抛异常 → 删 entry + rejected INTERNAL（不 crash 连接）。Task 9 消费 `sent_message_ids` 参数与 add 语义。

- [ ] **Step 1: 写失败测试**

在 `core/input/tests/test_v2_ws.py` 追加：
```python
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
        agent = _QueueFullAgent(ws)
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
        agent = _RaisingAgent(ws)
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
        class _OkAgent(_QueueFullAgent):
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
        agent = _RaisingAgent(ws)
        agent.attachment_repo = type("R", (), {
            "get": lambda aid: {"attachment_id": aid, "kind": "image", "status": "active",
                                "mime": "image/png", "filename": "x.png",
                                "stored_name": "x.png", "size": 10},
        })()
        await handle_json_message(
            {"type": "text", "content": "", "clientRequestId": "q4",
             "attachments": [{"attachmentId": "att_1"}]},
            agent, None, ws, voice_mgr=None)
        ack = [s for s in ws.sent if s.get("type") == "user_message.ack"]
        assert ack and ack[0]["status"] == "rejected"
        assert ack[0]["error"]["code"] == "INTERNAL"
        assert get_store().lookup("q4") is None  # entry 已删——重试可重新进入
```
（`FakeWs` 复用 `test_voice_ws.FakeWs`；`sent_message_ids` 参数此时尚不存在——Task 8 一并加入 `handle_json_message`/`_route_v2_text` 签名。）

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest core/input/tests/test_v2_ws.py::TestQueueFullAck -q --asyncio-mode=auto`
Expected: FAIL——旧纯文本路径忽略返回值仍发 accepted；`sent_message_ids` 参数不存在。

- [ ] **Step 3: 最小实现**

`core/input/ws_handler.py`：
1. `handle_json_message` 签名加 `sent_message_ids: set | None = None`，text 分支调 `_route_v2_text(agent, ws, text_content.strip(), data, sent_message_ids=sent_message_ids)`。
2. `_route_v2_text` 签名加 `sent_message_ids: set | None = None`；纯文本路径（~607-610）替换为：
```python
    try:
        accepted = await agent.process_message(
            text=text, message_id=message_id, protocol_version=2,
            conversation_id=conversation_id)
    except Exception:
        logger.warning("process_message 抛异常——删幂等 entry，rejected", exc_info=True)
        store.delete(client_request_id)
        await _send_ack(ws, "", client_request_id, client_message_id,
                        ack_conversation_id, "rejected",
                        error={"code": "INTERNAL", "message": "消息处理失败",
                               "retryable": False})
        return True
    if accepted:
        await _send_ack(ws, message_id, client_request_id, client_message_id,
                        ack_conversation_id, "accepted")
        if sent_message_ids is not None:
            sent_message_ids.add(message_id)   # D1 归属
    else:
        # 队列满 → 删幂等 entry（重试允许重新进入）+ rejected QUEUE_FULL
        store.delete(client_request_id)
        await _send_ack(ws, "", client_request_id, client_message_id,
                        ack_conversation_id, "rejected",
                        error={"code": "QUEUE_FULL",
                               "message": "Agent 忙，消息队列已满", "retryable": True})
    return True
```
3. media 路径（~586-589）process_message 包 try/except（异常 → 删 entry + rejected INTERNAL）：
```python
        try:
            accepted = await agent.process_message(
                text=text, content=content, message_id=message_id,
                protocol_version=2, conversation_id=conversation_id)
        except Exception:
            logger.warning("附件消息 process_message 抛异常——删幂等 entry", exc_info=True)
            store.delete(client_request_id)
            await _send_ack(ws, "", client_request_id, client_message_id,
                            ack_conversation_id, "rejected",
                            error={"code": "INTERNAL", "message": "消息处理失败",
                                   "retryable": False})
            return True
```
media 的 accepted 分支补 `if sent_message_ids is not None: sent_message_ids.add(message_id)`。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest core/input/tests/test_v2_ws.py -q --asyncio-mode=auto`
Expected: PASS（新 3 测试 + 既有 v2 测试全绿）。

- [ ] **Step 5: 回归 + commit**

Run: `python -m pytest core/input/tests/ -q`（确认 ws 相关无回归）
Run: `make lint`
Commit:
```bash
git add core/input/ws_handler.py core/input/tests/test_v2_ws.py
git commit -m "fix(batch8b): §3.7 队列满 ACK 对齐 + process_message 异常变体——纯文本/附件统一先 process 后判定，异常删 entry 不 crash 连接

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: D1 — 断连按归属取消

**Files:**
- Modify: `core/cognitive/loop.py`（新增 property）、`core/input/ws_handler.py`（handle_ws 归属跟踪 + 断连门 + `_handle_voice_commit` threading）
- Test: `core/input/tests/test_v2_ws.py`（追加 `TestDisconnectOwnership`）

**Interfaces:**
- Consumes: Task 1 的 `cancel_current` 语义、Task 6 的 `emitted_message_ids`、Task 8 的 `sent_message_ids` 参数/add 语义、`agent.cognitive.current_generation_user_message_id`（本任务新增）。
- Produces: `CognitiveLoop.current_generation_user_message_id` property；`handle_ws` 断开归属判定——仅当前 generation 用户消息 ID ∈ 本连接已发集合才 post `InterruptData(source='disconnect')`。

- [ ] **Step 1: 写失败测试**

在 `core/input/tests/test_v2_ws.py` 追加：
```python
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
            for _ in range(10):
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
```
（**voice 侧归属**：`sent_message_ids.update(voice_mgr.emitted_message_ids)` 并入与 `_on_segment` 写入 `_emitted_message_ids` 由 Task 6 `test_realtime_segment_records_emitted_message_ids` 覆盖——本任务文本路径测试已验证归属门逻辑本身。）

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest core/input/tests/test_v2_ws.py::TestDisconnectOwnership -q --asyncio-mode=auto`
Expected: `test_other_connection_close_does_not_cancel_generation` FAIL——旧代码 B 断开无条件取消 A 的生成 → 收不到 completed（收到 generation.failed 或超时）。

- [ ] **Step 3: 最小实现**

1. `core/cognitive/loop.py` 新增公开只读 property（放在 `status`/`phases` property 附近）：
```python
    @property
    def current_generation_user_message_id(self) -> str | None:
        """当前在途 generation 的用户消息 ID（D1 断连归属判定）。无在途/未建档 → None。"""
        if self._generation_ctx is None:
            return None
        return self._generation_ctx.user_message_id or None
```
2. `core/input/ws_handler.py`：
   - `handle_ws` 在 `voice_mgr` 创建后维护 `sent_message_ids: set[str] = set()`，贯通 `handle_json_message(..., sent_message_ids=sent_message_ids)`。
   - `handle_json_message` 已加 `sent_message_ids` 参数（Task 8），补 `_handle_voice_commit(..., sent_message_ids=sent_message_ids)` 调用。
   - `_handle_voice_commit` 签名加 `sent_message_ids: set | None = None`，在 post UserInputData 前 `if sent_message_ids is not None: sent_message_ids.add(message_id)`。
   - `finally` 块：`voice_mgr.close()` 之前并入语音归属：
```python
        if voice_mgr is not None:
            sent_message_ids.update(voice_mgr.emitted_message_ids)
            await voice_mgr.close()
```
   - 断连中断判定（替换现 337-344）：
```python
        # 批次 2 重连语义（最小）+ 批次 8b（D1）：仅当在途 generation 由本连接发起才取消——
        # 多标签/多设备下 Tab B 断开不再误杀 Tab A 的生成。归属 = 当前 generation 的
        # user_message_id ∈ 本连接已发消息集合（文本/附件/语音 commit/REALTIME 段）。
        if protocol_version >= 2:
            try:
                gen_uid = getattr(getattr(agent, "cognitive", None),
                                  "current_generation_user_message_id", None)
                if gen_uid and gen_uid in sent_message_ids:
                    from core.monitoring.event_bus import EventPriority
                    agent.bus.post(InterruptData(source="disconnect"),
                                   priority=EventPriority.INTERRUPT)
            except Exception:
                logger.debug("v2 断线取消 generation 失败", exc_info=True)
```
   - `getattr(agent, "cognitive", None)` 兜底 `_FakeAgent`。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest core/input/tests/test_v2_ws.py -q --asyncio-mode=auto`
Expected: PASS（新 2 测试 + 既有全绿）。

- [ ] **Step 5: 回归 + commit**

Run: `python -m pytest core/input/tests/ core/cognitive/tests/ -q`
Run: `make lint`
Commit:
```bash
git add core/cognitive/loop.py core/input/ws_handler.py core/input/tests/test_v2_ws.py
git commit -m "fix(batch8b): D1 断连按归属取消——仅本连接发起的在途 generation 被断开取消，多标签零误杀

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 10: D5 — v2 空响应发 segment（不泄漏 v1 message 帧）

**Files:**
- Modify: `core/cognitive/generation.py`（`protocol_version` 字段）、`core/cognitive/loop.py`（process 构造 ctx + `_finalize_turn` 兜底）
- Test: `core/cognitive/tests/test_v2_empty_response.py`（新文件）

**Interfaces:**
- Consumes: Task 1 的 loop.py 现状；`OutputManager.segment_started/segment_completed`（manager.py:363/397，需 `ctx.current_segment_id` 非 None）、`GenerationContext.open_segment/close_segment`。
- Produces: `GenerationContext.protocol_version: int = 1`；`_finalize_turn` 兜底在 v2 且已有活动段时直接完成现有段，否则 open+started+completed；v1 保持 MessageObj。

- [ ] **Step 1: 写失败测试**

```python
# core/cognitive/tests/test_v2_empty_response.py
"""D5：空响应兜底——v2 发 segment.completed 非 message 帧（spec §3.5）。"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.cognitive.generation import GenerationContext
from core.cognitive.loop import CognitiveLoop

pytestmark = pytest.mark.unit


def _make_loop():
    loop = CognitiveLoop(
        llm=MagicMock(), fsm=MagicMock(), event_bus=MagicMock(),
        output=MagicMock(), prompt=MagicMock(), session_resolver=MagicMock(),
    )
    loop._generation_ctx = GenerationContext(
        generation_id="gen_1", user_message_id="msg_1",
        assistant_message_id="msg_a", conversation_id="default",
        protocol_version=2,
    )
    loop._output.segment_started = AsyncMock()
    loop._output.segment_completed = AsyncMock()
    loop._output.typing = AsyncMock()
    return loop


@pytest.mark.asyncio
async def test_v2_fallback_emits_segment_not_message():
    """v2 空响应兜底 → segment_started + segment_completed，不发 MessageObj。"""
    loop = _make_loop()
    sent = []
    loop._output.send = AsyncMock(side_effect=lambda m: sent.append(m))
    loop._finalize_turn(
        "你好", [], "", False, "用户", 1, [],
        "你好",
        conversation_id="default",
        session=MagicMock(),
    )
    loop._output.segment_completed.assert_awaited()
    assert sent == []  # 未发 v1 message 帧


@pytest.mark.asyncio
async def test_v2_fallback_completes_existing_segment():
    """F4：已有活动段（delta 已开段无 SENTENCE）→ 直接完成现有段，不重复 open_segment。"""
    loop = _make_loop()
    loop._generation_ctx.open_segment("public_reply", "text")   # 模拟 delta 已开段
    opened = loop._generation_ctx.current_segment_id
    loop._output.segment_started = AsyncMock()
    loop._output.segment_completed = AsyncMock()
    loop._output.send = AsyncMock()
    loop._finalize_turn(
        "你好", [], "", False, "用户", 1, [],
        "你好",
        conversation_id="default",
        session=MagicMock(),
    )
    loop._output.segment_started.assert_not_awaited()          # 不重复 open
    assert loop._generation_ctx.current_segment_id is None     # 段已关闭


@pytest.mark.asyncio
async def test_v1_fallback_still_emits_message():
    """v1 保持 MessageObj 行为。"""
    loop = _make_loop()
    loop._generation_ctx = GenerationContext(
        generation_id="gen_1", user_message_id="msg_1",
        assistant_message_id="msg_a", conversation_id="default",
        protocol_version=1,
    )
    sent = []
    loop._output.send = AsyncMock(side_effect=lambda m: sent.append(m))
    loop._finalize_turn(
        "你好", [], "", False, "用户", 1, [],
        "你好",
        conversation_id="default",
        session=MagicMock(),
    )
    assert len(sent) == 1  # v1 仍发 MessageObj
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest core/cognitive/tests/test_v2_empty_response.py -q --asyncio-mode=auto`
Expected: FAIL——`_generation_ctx` 无 `protocol_version` 属性（AttributeError）；v2 兜底仍发 MessageObj。

- [ ] **Step 3: 最小实现**

1. `core/cognitive/generation.py` `GenerationContext` 加字段：
```python
    protocol_version: int = 1
```
2. `core/cognitive/loop.py` process() 构造 ctx（~461）加 `protocol_version=protocol_version`。
3. `_finalize_turn` 兜底（~921-924）分支化：
```python
        if not speaking_started:
            await self._output.typing(False)
            ctx = self._generation_ctx
            if ctx is not None and ctx.protocol_version >= 2:
                # v2：发 v2 兼容 segment（channel=public_reply）——替代 v1 message 帧，
                # 客户端以 segment.completed.content 为渲染真值（protocol-v2 §2 注）。
                # F4：TEXT_DELTA 已流式开段但未出 SENTENCE 边界时 current_segment_id
                # 已非 None——直接完成现有段，不重复 open（防悬空段+文本重复）。
                if ctx.current_segment_id is not None:
                    await self._output.segment_completed(public_text, ctx)
                    ctx.close_segment()
                else:
                    ctx.open_segment("public_reply", "text")
                    await self._output.segment_started(ctx)
                    await self._output.segment_completed(public_text, ctx)
                    ctx.close_segment()
            else:
                msg = MessageObj(content=public_text)
                await self._output.send(msg)
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest core/cognitive/tests/test_v2_empty_response.py -q --asyncio-mode=auto`
Expected: PASS（3 passed）。

- [ ] **Step 5: 回归 + commit**

Run: `python -m pytest core/cognitive/tests/ -q`
Run: `make lint`
Commit:
```bash
git add core/cognitive/generation.py core/cognitive/loop.py core/cognitive/tests/test_v2_empty_response.py
git commit -m "fix(batch8b): D5 v2 空响应兜底发 segment.completed（含活动段直接完成 F4），不再向 v2 泄漏 v1 message 帧

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review 对照

**1. Spec 覆盖**（13 条 → task）：
- #1 ws_handler 断连误杀 → Task 9 ✓
- #2/#12/#13 队列满 ACK/附件 entry/异常变体 → Task 8 ✓
- #3 loop:326 interrupt_source → Task 1 ✓
- #4 voice_sessions:375 RECORDING 残留 → Task 2 ✓
- #5 voice_sessions:371 cancel 抢占 → Task 6 ✓
- #6 ws_handler:693 空文本 → Task 7 ✓
- #7 loop:922 空响应 → Task 10 ✓
- #8 tts_adapter:227 cancel_stream → Task 4 ✓
- #9 voice_sessions:348 dictation ID → Task 5 ✓
- #10 voice_sessions:165 restore_emotion → Task 5 ✓
- #11 tts_adapter:57 _wav_info → Task 3 ✓

**2. 依赖序**：Task 5 → 6（`_transcribe_no_emotion`）；Task 6 → 9（`emitted_message_ids`）；Task 1 → 9/10（loop 文件顺序）；Task 7 → 8（ws_handler 同文件顺序）；Task 8 → 9（`sent_message_ids`）。

**3. 类型一致性**：`_on_segment(text, emotion=None, events=None)` 全 plan 一致；`emitted_message_ids -> set[str]`；`current_generation_user_message_id -> str | None`；`_stt_pending: dict[int, tuple[str, str, list[str]]]` 在 Task 6 各步一致。

**4. 已知实现注意**：
- Task 1：`cancel_current` 语义变更须同步更新 `test_session_resolver.py::test_cancel_current_preserves_disconnect_source`（旧断言「无 task 仍记 source」→ 新语义「无 task 不记」）。
- Task 2：D2 测试用响度 PCM + v1 对照（全零 PCM 不触发 VAD，测试会假绿）。
- Task 5/6：`test_voice_sessions.py` 的 `FakeAgent.stt` 须从 `object()` 换为 `_FakeSTTBackend`（提供 `.transcribe`）——`_transcribe_no_emotion` 接线后 dictation 走 `agent.stt.transcribe`。
- Task 6：D3 既有同步驱动测试无需改（已含 `await asyncio.sleep(0.05)`，后台派发在其窗口内完成）；新增 F3/提交序/归属三测试。
- Task 9：`_HeldLLM` 镜像 `_UsageLLM` 接口（`stream_think` 异步生成器 + `chat`/`test`/`close`/`_model`），`released` 手动放行；`_registry_docs` 显式导入；registry 终态轮询（3s）。
