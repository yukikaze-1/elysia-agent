# Phase 3: 测试缺口修复 — Output / Input 模块

> **Status: COMPLETE** — 2026-06-02, 3 tasks done, 101 tests passing.
> Commits: `1b84d4f` `1c10769` `4c3fd34`

**Goal:** 补充 Output 模块的音频能力/命令/生命周期测试 + Input 模块的 VAD 边缘/WebSocket 路由/端点测试

**Architecture:** Output 追加到已有 `test_output.py`；Input 追加到 `test_audio_buffer.py` 和 `test_server.py`

**Tech Stack:** Python 3.13, pytest, asyncio, unittest.mock

**Spec:** [2026-06-02-test-audit-design.md](../specs/2026-06-02-test-audit-design.md) 第八至九节

---

## File Structure

```
Module/output/test_output.py     # 追加: AUDIO, command, stream lifecycle, error isolation
Module/input/test_audio_buffer.py  # 追加: VAD edges, STT timeout, on_transcription=None
Module/input/test_server.py      # 追加: WS message types, /health, FSM monitor, assertions
```

---

### Task 1: Module/output/test_output.py 追加 — 音频 / command / 生命周期 / 错误隔离

**Files:**
- Modify: `Module/output/test_output.py` — 追加测试类
- Read first: `Module/output/protocol.py`, `Module/output/manager.py`, `Module/output/websocket.py`, `Module/output/console.py`

**Test scenarios:**

#### A. AUDIO 能力——零覆盖
- `ConsoleChannel.capabilities` 不包含 `AUDIO` — 验证
- `WebSocketChannel.capabilities` 包含 `AUDIO` — 验证
- `OutputManager.audio_chunk(b"data")` — 通过 capability 路由到 WebSocketChannel，验证 `ws.send_json` 被调用并携带 `{"type": "audio_chunk", "data": ...}`
- `OutputManager.audio_end()` — 发送 `{"type": "audio_end"}` 到 WebSocket

#### B. command() 便利方法——零覆盖
- `OutputManager.command("reload", {"plugin": "x"})` — 验证通过 `_broadcast` 调用 `ChannelCapability.COMMAND` channel 的 `on_command` 方法

#### C. stream() 完整生命周期
- `typing(True)` → `stream(deltas, finalize=cb)` → `typing(False)` 协作流程
- 验证 `typing(True)` 调用 channel 的 `on_typing(True)`
- 验证 stream 结束时调用 finalize 回调
- 验证 `typing(False)` 在 stream 后调用

#### D. 错误隔离补全
- `_FailingChannel` 添加 `on_text_delta` 抛异常 → 验证不阻塞其他 channel
- `on_expression` 抛异常 → 不阻塞其他 channel
- `on_action` 抛异常 → 不阻塞其他 channel
- `on_command` 抛异常 → 不阻塞其他 channel
- `OutputManager.close()` 中某个 `channel.close()` 抛异常 → 验证 warning log 被发出 + 其他 channel 仍被关闭

#### E. WebSocketChannel 方法全覆盖
- `on_typing` (带 stream_id / 不带)
- `on_expression` (带 stream_id / 不带)
- `on_action` (带 params / params=None — `if params:` 分支)
- `on_command` (带 params / params=None)
- `on_message` (str 输入 / dict 输入)
- `on_audio_chunk` / `on_audio_end` / `on_state`
- `close()` 时 ws 已断开 → 验证 `except Exception: pass` 吞下异常

#### F. ConsoleChannel 非流式路径
- `on_message("hello")` 字符串路径 — 验证 `_delta_printed=False` 分支 (line 27-31)
- `on_message("hello", inner_voice=True)` — 验证 `_delta_printed=False` + inner_voice 分支 (line 44-45)
- `on_message` after `on_text_delta` — 验证 `_delta_printed=True` 分支 (line 36-41)

**运行:** `conda run -n Elysia python -m pytest Module/output/test_output.py -v`

---

### Task 2: Module/input/test_audio_buffer.py 追加 — VAD 边缘 / STT 超时

**Files:**
- Modify: `Module/input/test_audio_buffer.py` — 追加测试类
- Read first: `Module/input/audio_buffer.py`

**Test scenarios:**

#### A. VAD 帧处理
- 多帧 chunk (1280 bytes = 2 frames) — `range(0, 1280-640+1, 640)` 处理两帧，验证两帧都被送入 VAD
- chunk 非整除帧 (700 bytes) — 验证 trailing 60 bytes 被丢弃
- 50% speech 边界 — 2 帧中 1 帧 speech = 50%，验证 `>` 严格比较：不算 speech
- 51% speech — 102 帧中 52 帧 speech = ~51%，验证算 speech
- `vad.is_speech()` 抛异常 → `except Exception: pass` → 函数不崩溃

#### B. Interrupt cooldown
- 第一段语音触发 interrupt → reset → 立即第二段语音（`now - _last_interrupt < 0.5s`）→ 验证 interrupt **不**被触发
- 第一段语音触发 interrupt → `asyncio.sleep(0.6)` → 第二段语音 → 验证 interrupt 被触发

#### C. STT timeout / 异常
- `_process_segment` 中 `stt_transcribe` 超过 30s → `asyncio.TimeoutError` → 验证 warning log + 不崩溃
- `stt_transcribe` 抛通用异常 → 验证 warning log + 不崩溃

#### D. on_transcription=None
- 构造 `AudioBuffer(on_transcription=None)` → 语音触发 STT → 结果静默丢弃，不崩溃

#### E. 多段语音序列
- speech → silence (STT触发) → speech → silence (STT再次触发) — 验证两段都被处理

#### F. reset() 不重置 _deafen_until
- `deafen(10)` → `reset()` → 验证 `_deafen_until` 保留（或记录这是已知行为）

**运行:** `conda run -n Elysia python -m pytest Module/input/test_audio_buffer.py -v`

---

### Task 3: Module/input/test_server.py 追加 — WS 消息 / 端点 / FSM monitor

**Files:**
- Modify: `Module/input/test_server.py` — 追加/修复测试
- Read first: `Module/input/server.py`

**Test scenarios:**

#### A. WebSocket 消息类型
- `WSMsgType.BINARY` 消息 → 路由到 `audio_buf.feed(msg.data)` — 验证 buffer 接收到数据
- `WSMsgType.ERROR` → 日志输出 — 验证不崩溃
- 有效 JSON 但未知 `"type"` 字段 → `else` 分支 (line 191) → DEBUG 日志 → 不崩溃

#### B. 文本消息边缘
- `{"type": "text", "content": ""}` → 空字符串 → strip 后为空 → `process_message` 不被调用
- `{"type": "text", "content": "   "}` → 纯空白 → strip 后为空 → `process_message` 不被调用

#### C. 端点测试
- `GET /health` → 返回 200 + 状态信息
- `GET /ready` → 返回 200 + ready 状态
- `GET /debug/api` → 返回 JSON debug 数据
- `GET /` → 返回 index.html (静态文件)
- `GET /debug` → 返回 debug.html
- `serve_static=True` (默认) → 静态文件可用

#### D. FSM state monitor
- 启动 WS 连接 → agent FSM 变为 `"speaking"` → 验证 `_monitor_state` 调用 `audio_buf.deafen()`
- agent FSM 从 `"speaking"` 变为其他状态 → 验证 deafen 被解除或状态被处理

#### E. 修复无断言测试
- `test_interrupt_message` — 发送 `{"type": "interrupt"}` 后验证 `event_bus.post()` 被调用并携带 interrupt 事件
- `test_do_transcription_with_emotion` — 验证 WebSocket 发送了 output 消息
- `test_invalid_json_returns_error` — 验证 WebSocket 响应包含 `{"type": "error", "message": "Invalid JSON"}`

#### F. 关闭生命周期
- WS 连接活跃时 shutdown → 验证 channel 被移除、monitor task 被取消、audio 被 flush
- 两次 shutdown → 验证幂等（`_running = False` 已设置，第二次不崩溃）

**运行:** `conda run -n Elysia python -m pytest Module/input/test_server.py -v`

---

## 验证

```bash
conda run -n Elysia python -m pytest Module/output/test_output.py Module/input/test_audio_buffer.py Module/input/test_server.py -v
```
