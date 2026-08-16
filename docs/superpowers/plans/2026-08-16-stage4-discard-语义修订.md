# `voice.discard` 取消原语语义修订 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `voice.discard` 从「RECORDING 态拒绝 + IDLE 报 NOT_FOUND」修订为「可取消任意未提交阶段 + IDLE/重复幂等成功」的取消原语（BE-S4-RUN-P0-01）。

**Architecture:** 唯一生产改动是重构 `core/input/voice_sessions.py` 的 `discard_recording()` 一个方法（约 15 行）：IDLE 幂等成功、RECORDING 态任一子状态直接取消清态、错误 recordingId/其它语音模式用既有 `VOICE_SESSION_CONFLICT` 拒绝。WS 路由层与契约模型零改动；golden 门禁零 churn（`ws-v2-recording.jsonl` 只含 `voice.transcript` 输出帧）。配套改动为单元测试（Task 1）、WS 路由测试（Task 2）、smoke 两真实序列（Task 3）、活跃文档同步（Task 4）。

**Tech Stack:** Python 3.11+ asyncio / aiohttp / pytest（marker: unit/service/slow）。

**Spec:** `docs/superpowers/specs/2026-08-16-stage4-discard-语义修订-design.md`

## Global Constraints

- 禁相对导入（ruff TID252）；生产代码参数必须类型注解（测试文件豁免）。
- 注释/README 中文；marker 必须打（unit/service/slow）。
- 改动范围纪律：`golden`、`Makefile`、`providers.yaml`、`frontend_contract/` 零改动。
- 历史文档（`docs/superpowers/plans/2026-08-15-...`、execution-record）**不动**。
- 不新增 wire 消息/字段；错误码复用 `VOICE_SESSION_CONFLICT`，不新增错误目录成员。
- 门禁：`make test-quick` / `make test-normal` / `make lint` / `make pyright` / `make check-types` 全绿；smoke 全 PASS 退出 0。

---

### Task 1: `discard_recording()` 语义重构 + 单元测试

**Files:**
- Modify: `core/input/voice_sessions.py:336-343`（`discard_recording()`）
- Modify: `core/input/tests/test_voice_sessions.py:177-181`（`test_recording_state_boundaries`）
- Test: `core/input/tests/test_voice_sessions.py`（新增 10 个测试）

**Interfaces:**
- Consumes: 现有 `VoiceSessionManager._mode/_recording_id/_rec_state/_in_flight_stt/_reset_recording()`、`VoiceMode`、`_RecordingState`、`ERR_CONFLICT`/`ERR_RECORDING_NOT_FOUND`、`VoiceSessionError`。
- Produces: `discard_recording(recording_id: str) -> None` 新语义——IDLE 幂等成功；RECORDING 匹配任意子状态取消清态；不匹配/其它模式 raise `VoiceSessionError(ERR_CONFLICT, ...)`。Task 2/3 依赖此行为。

- [ ] **Step 1: 修改既有断言（红测前置）**

`core/input/tests/test_voice_sessions.py:177-181` 现断言「TRANSCRIBING discard 允许 → 复位后二次 discard 报 NOT_FOUND」。改为二次幂等成功：

```python
    # TRANSCRIBING 态 discard 允许 → 复位后二次 discard 幂等成功（阶段4真实联调修订）
    await mgr.discard_recording("rec_1")
    await mgr.discard_recording("rec_1")       # 幂等成功，不再 NOT_FOUND
```

- [ ] **Step 2: 新增 10 个单元测试（红测）**

在 `test_voice_sessions.py` 末尾（`test_recording_state_boundaries` 之后任意位置）追加：

```python
@pytest.mark.asyncio
async def test_discard_recording_state_succeeds():
    """start → discard（未 stop）成功回 IDLE，随后可再次 start（现象 A，review §3.6 #1）。"""
    mgr, emitted, _ = make_manager()
    await mgr.start_recording("rec_a")
    await mgr.discard_recording("rec_a")          # 不再 raise CONTRACT_VALIDATION
    assert mgr.has_active_session() is False
    assert not emitted                            # 成功 send-only，无任何帧
    await mgr.start_recording("rec_b")            # 不再 VOICE_SESSION_CONFLICT
    assert mgr.has_active_session() is True


@pytest.mark.asyncio
async def test_discard_no_stt_no_transcript_no_message():
    """start → PCM → discard：不调 STT、不发 transcript、不生成消息（补充约束 1）。"""
    mgr, emitted, bus_posts = make_manager()
    await mgr.start_recording("rec_1")
    await mgr.feed_pcm(b"\x00" * 6400)
    mgr._agent._last_text = "不应被转写"
    await mgr.discard_recording("rec_1")
    await asyncio.sleep(0.05)
    assert not any(e.get("type") == "voice.transcript" for e in emitted)
    assert not any(e.get("type") == "user_message.ack" for e in emitted)
    assert not bus_posts
    assert mgr._in_flight_stt is None


@pytest.mark.asyncio
async def test_discard_transcribing_cancels_stt():
    """start → stop → TRANSCRIBING → discard：取消在途 STT，迟到结果不复活，可重新 start。"""
    mgr, emitted, _ = make_manager()
    await mgr.start_recording("rec_1")
    await mgr.feed_pcm(b"\x00" * 6400)
    mgr._agent._last_text = "迟到结果"
    await mgr.stop_recording("rec_1")
    await mgr.discard_recording("rec_1")
    await asyncio.sleep(0.05)                     # 让被取消任务收尾
    assert not any(e.get("type") == "voice.transcript" for e in emitted)
    assert mgr._in_flight_stt is None
    assert mgr.has_active_session() is False
    await mgr.start_recording("rec_2")


@pytest.mark.asyncio
async def test_discard_pending_then_restart():
    """start → stop → PENDING → discard：清字幕后可立即开始新录音（review §3.6 #4）。"""
    mgr, emitted, _ = make_manager()
    await mgr.start_recording("rec_1")
    await mgr.feed_pcm(b"\x00" * 6400)
    mgr._agent._last_text = "待确认"
    await mgr.stop_recording("rec_1")
    await asyncio.sleep(0.05)
    assert any(e.get("type") == "voice.transcript" for e in emitted)  # 确认进入 PENDING
    await mgr.discard_recording("rec_1")
    with pytest.raises(VoiceSessionError) as ei:
        mgr.commit_recording("rec_1", "待确认")
    assert ei.value.code == "VOICE_RECORDING_NOT_FOUND"
    assert mgr.has_active_session() is False
    await mgr.start_recording("rec_2")


@pytest.mark.asyncio
async def test_discard_after_short_pcm_idempotent():
    """<640B → stop（F3 空转写 reset 回 IDLE）→ discard：幂等成功无 raise（review §3.6 #5）。"""
    mgr, _, _ = make_manager()
    await mgr.start_recording("rec_1")
    await mgr.feed_pcm(b"\x00" * 100)             # < 一帧 640B
    await mgr.stop_recording("rec_1")             # 空转写直接 reset 回 IDLE
    await mgr.discard_recording("rec_1")          # IDLE 态幂等成功
    await mgr.discard_recording("rec_1")          # 重复幂等成功


@pytest.mark.asyncio
async def test_discard_after_empty_transcript_idempotent():
    """STT 空 → reset 回 IDLE → discard：幂等成功无 raise（现象 B，review §3.6 #6）。"""
    mgr, emitted, _ = make_manager()
    await mgr.start_recording("rec_1")
    await mgr.feed_pcm(b"\x00" * 6400)
    mgr._agent._last_text = "   "                 # 纯空白 → 空转写 reset
    await mgr.stop_recording("rec_1")
    await asyncio.sleep(0.05)
    assert not any(e.get("type") == "voice.transcript" for e in emitted)
    await mgr.discard_recording("rec_1")          # IDLE 幂等成功


@pytest.mark.asyncio
async def test_discard_repeat_idempotent():
    """discard → discard 重复调用均成功（IDLE 幂等，review §3.6 #7）。"""
    mgr, _, _ = make_manager()
    await mgr.start_recording("rec_1")
    await mgr.discard_recording("rec_1")
    await mgr.discard_recording("rec_1")          # 二次幂等成功


@pytest.mark.asyncio
async def test_discard_mismatched_id_does_not_cancel_current():
    """录音 A 活跃时 discard(B)：CONFLICT，A 不受影响（review §3.6 #8，绝不取消当前录音）。"""
    mgr, emitted, _ = make_manager()
    await mgr.start_recording("rec_a")
    with pytest.raises(VoiceSessionError) as ei:
        await mgr.discard_recording("rec_b")
    assert ei.value.code == "VOICE_SESSION_CONFLICT"
    assert mgr._mode == VoiceMode.RECORDING       # A 仍在
    assert mgr._recording_id == "rec_a"
    await mgr.feed_pcm(b"\x00" * 6400)
    mgr._agent._last_text = "A 正常"
    await mgr.stop_recording("rec_a")             # A 仍可走完正常生命周期
    await asyncio.sleep(0.05)
    assert any(e.get("type") == "voice.transcript" for e in emitted)


@pytest.mark.asyncio
async def test_discard_realtime_isolated():
    """realtime 活跃时 discard：CONFLICT，realtime 会话不受影响（review §3.6 #9）。"""
    mgr, _, _ = make_manager()
    await mgr.start_realtime("vs_1", None)
    with pytest.raises(VoiceSessionError) as ei:
        await mgr.discard_recording("rec_x")
    assert ei.value.code == "VOICE_SESSION_CONFLICT"
    assert mgr._mode == VoiceMode.REALTIME
    assert mgr._session_id == "vs_1"
    await mgr.feed_pcm(_loud(1600))               # realtime 仍可 feed
    await mgr.stop_session("vs_1")
    await mgr.close()                             # 清 ack/idle 后台任务


@pytest.mark.asyncio
async def test_discard_dictation_isolated():
    """dictation 活跃时 discard：CONFLICT，dictation 会话不受影响（review §3.6 #9）。"""
    mgr, _, _ = make_manager()
    await mgr.start_dictation("req_d1")
    with pytest.raises(VoiceSessionError) as ei:
        await mgr.discard_recording("rec_x")
    assert ei.value.code == "VOICE_SESSION_CONFLICT"
    assert mgr._mode == VoiceMode.DICTATION
    await mgr.stop_dictation("req_d1")
```

- [ ] **Step 3: 运行定向测试确认红**

Run: `python -m pytest core/input/tests/test_voice_sessions.py -q -x`
Expected: 失败——`test_recording_state_boundaries` 二次 discard raise（新断言要求不 raise）；`test_discard_recording_state_succeeds` raise `CONTRACT_VALIDATION`；`test_discard_mismatched_id_does_not_cancel_current` / `test_discard_realtime_isolated` / `test_discard_dictation_isolated` raise `VOICE_RECORDING_NOT_FOUND`（新断言要求 `VOICE_SESSION_CONFLICT`）；`test_discard_after_*_idempotent` / `test_discard_repeat_idempotent` 首次 discard 即 raise（IDLE 态 `NOT_FOUND` 或 RECORDING 态 `CONTRACT_VALIDATION`），测试失败。

- [ ] **Step 4: 实现 `discard_recording()` 重构**

`core/input/voice_sessions.py:336-343` 整个方法替换为：

```python
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
```

- [ ] **Step 5: 运行定向测试确认绿**

Run: `python -m pytest core/input/tests/test_voice_sessions.py -q`
Expected: 全部通过（含修改后的 `test_recording_state_boundaries` 与新增 10 个）。

- [ ] **Step 6: 提交**

```bash
git add core/input/voice_sessions.py core/input/tests/test_voice_sessions.py
git commit -m "fix(voice): voice.discard 取消任意未提交阶段 + IDLE/重复幂等成功（BE-S4-RUN-P0-01）"
```

---

### Task 2: WS 路由级 discard 测试

**Files:**
- Test: `core/input/tests/test_voice_ws.py`（新增 3 个测试）

**Interfaces:**
- Consumes: Task 1 的 `discard_recording()` 新语义；`test_voice_ws.py` 既有 `make()` → `(agent, ws, mgr)` + `handle_json_message(data, agent, None, ws, voice_mgr=mgr)`；`ws.sent` 记录 `send_json`。
- Produces: 无新接口——验证 review §3.6 #9/#10 与 IDLE 幂等在 WS 路由层的行为。

- [ ] **Step 1: 新增 3 个 WS 路由测试（红测）**

在 `test_voice_ws.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_voice_discard_ws_sequence():
    """WS：start(A) → discard(A) → start(B) 无 CONTRACT/NOT_FOUND/CONFLICT（review §3.6 #10）。"""
    agent, ws, mgr = make()
    await handle_json_message({"type": "voice.record.start", "recordingId": "rec_a"},
                              agent, None, ws, voice_mgr=mgr)
    await handle_json_message({"type": "voice.discard", "recordingId": "rec_a"},
                              agent, None, ws, voice_mgr=mgr)
    errs = [s.get("code") for s in ws.sent if s.get("type") == "error"]
    assert not any(c in ("CONTRACT_VALIDATION", "VOICE_RECORDING_NOT_FOUND") for c in errs)
    assert mgr.mode().value == "idle"
    await handle_json_message({"type": "voice.record.start", "recordingId": "rec_b"},
                              agent, None, ws, voice_mgr=mgr)
    assert mgr.mode().value == "recording"       # 无 VOICE_SESSION_CONFLICT
    errs = [s.get("code") for s in ws.sent if s.get("type") == "error"]  # 重算，含 start(B)
    assert not any(c in ("CONTRACT_VALIDATION", "VOICE_RECORDING_NOT_FOUND", "VOICE_SESSION_CONFLICT")
                   for c in errs)


@pytest.mark.asyncio
async def test_voice_discard_realtime_isolated_ws():
    """WS：realtime 活跃收 voice.discard → error{VOICE_SESSION_CONFLICT}，会话不受影响。"""
    agent, ws, mgr = make()
    await handle_json_message({"type": "voice_session.start", "sessionId": "vs_1",
                               "conversationId": None},
                              agent, None, ws, voice_mgr=mgr)
    await handle_json_message({"type": "voice.discard", "recordingId": "rec_x"},
                              agent, None, ws, voice_mgr=mgr)
    errs = [s for s in ws.sent if s.get("type") == "error"]
    assert errs and errs[-1].get("code") == "VOICE_SESSION_CONFLICT"
    assert mgr.mode().value == "realtime"        # 会话未拆
    assert mgr._session_id == "vs_1"
    await mgr.close()


@pytest.mark.asyncio
async def test_voice_discard_idle_idempotent_ws():
    """WS：无录音时 voice.discard（IDLE）→ 不发任何 error 帧（现象 B，review §3.6 #6）。"""
    agent, ws, mgr = make()
    await handle_json_message({"type": "voice.discard", "recordingId": "rec_x"},
                              agent, None, ws, voice_mgr=mgr)
    assert not any(s.get("type") == "error" for s in ws.sent)
```

- [ ] **Step 2: 运行定向测试确认红**

Run: `python -m pytest core/input/tests/test_voice_ws.py::test_voice_discard_ws_sequence -q -x`
Expected: 失败——`test_voice_discard_ws_sequence` 首段 discard raise（旧语义 CONTRACT），且 second start 报 `VOICE_SESSION_CONFLICT`；`test_voice_discard_realtime_isolated_ws` 期望 `VOICE_SESSION_CONFLICT` 而旧代码发 `VOICE_RECORDING_NOT_FOUND`。

- [ ] **Step 3: 运行全量定向测试确认绿（依赖 Task 1）**

Run: `python -m pytest core/input/tests/test_voice_ws.py -q`
Expected: 全部通过（Task 1 已落地新语义）。

- [ ] **Step 4: 提交**

```bash
git add core/input/tests/test_voice_ws.py
git commit -m "test(voice): WS 路由级 voice.discard 幂等/隔离测试（BE-S4-RUN-P0-01）"
```

---

### Task 3: smoke 改写——采集失败清理 + 空录音迟到删除

**Files:**
- Modify: `scripts/stage4_smoke.py`（`_voice_ws_scenarios` 内 discard 段，现 `:264-279`；新增两个模块级 helper）

**Interfaces:**
- Consumes: Task 1 的 `discard_recording()` 新语义；smoke 既有 `_Smoke.check(scenario, cond, detail)`、`ws`（`aiohttp` WS）、`PCM_FIXTURE`、`WSMsgType`。
- Produces: 两个新 helper `_recv_until_type(ws, target, timeout)` / `_drain_errors(ws, rounds)`（模块级，Task 3 内部使用）；检查数 24 → 27。

- [ ] **Step 1: 新增两个模块级接收 helper（在 `_backend_sha()` 之后）**

```python
async def _recv_until_type(ws: Any, target: str, timeout: float = 10) -> dict | None:
    """收帧直到匹配 target 类型或超时；跳过其它帧，超时返回 None（不抛）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            m = await asyncio.wait_for(ws.receive(), timeout=deadline - time.monotonic())
        except asyncio.TimeoutError:
            return None
        if m.type != WSMsgType.TEXT:
            continue
        d = json.loads(m.data)
        if d.get("type") == target:
            return d
    return None


async def _drain_errors(ws: Any, rounds: int = 3) -> dict | None:
    """最多 rounds 轮短超时排空帧，返回第一条 error 帧（无则 None）。"""
    err = None
    for _ in range(rounds):
        try:
            m = await asyncio.wait_for(ws.receive(), timeout=0.5)
        except asyncio.TimeoutError:
            break
        if m.type != WSMsgType.TEXT:
            continue
        d = json.loads(m.data)
        if d.get("type") == "error" and err is None:
            err = d
    return err
```

- [ ] **Step 2: 改写 discard 段（`_voice_ws_scenarios` 内，现 `:264-279`）**

把整段（从 `# discard：start → stop → discard（voice_sessions.py:339-340 ...` 注释到 `smoke.check("discard 不创建消息", no_ack, "")`）替换为：

```python
    # ── discard 取消原语（BE-S4-RUN-P0-01，review §5 两真实序列）──
    # 序列 1（采集启动失败清理，现象 A）：start 被后端接受后采集初始化失败 → 前端 fire-and-forget
    # discard → RECORDING 态直接取消（不再「先 stop 再 discard」），无需 stop/重连即可再 start。
    await ws.send_json({"type": "voice.record.start", "recordingId": "rec_smoke_cap"})
    await ws.send_json({"type": "voice.discard", "recordingId": "rec_smoke_cap"})
    e1 = await _drain_errors(ws)
    smoke.check("采集失败 discard 无错误帧", e1 is None, str(e1)[:120])
    # discard 后可立即重录（rec_smoke_2 完整走 stop→transcript，证明会话已释放）
    await ws.send_json({"type": "voice.record.start", "recordingId": "rec_smoke_2"})
    await ws.send_bytes(pcm[:4096])
    await ws.send_json({"type": "voice.record.stop", "recordingId": "rec_smoke_2"})
    t2 = await _recv_until_type(ws, "voice.transcript", timeout=10)
    smoke.check("discard 后可立即重录", bool(t2), str(t2)[:120])
    # PENDING 态 discard：丢弃已收字幕，不创建消息（无 user_message.ack）
    await ws.send_json({"type": "voice.discard", "recordingId": "rec_smoke_2"})
    e2 = await _drain_errors(ws)
    ack2 = False
    while True:
        try:
            m = await asyncio.wait_for(ws.receive(), timeout=1)
        except asyncio.TimeoutError:
            break
        if m.type == WSMsgType.TEXT and json.loads(m.data).get("type") == "user_message.ack":
            ack2 = True
    smoke.check("已收字幕 discard 无错误不建消息", e2 is None and not ack2,
                f"err={e2} ack={ack2}")
    # 序列 2（空录音迟到删除，现象 B）：空录音 stop 自动 reset 回 IDLE，迟到 discard 幂等成功
    await ws.send_json({"type": "voice.record.start", "recordingId": "rec_smoke_empty"})
    await ws.send_json({"type": "voice.record.stop", "recordingId": "rec_smoke_empty"})  # 零 PCM → 空转写 reset
    await ws.send_json({"type": "voice.discard", "recordingId": "rec_smoke_empty"})
    e3 = await _drain_errors(ws)
    smoke.check("空录音迟到 discard 幂等无错误", e3 is None, str(e3)[:120])
```

- [ ] **Step 3: 运行 smoke 确认绿（依赖 Task 1）**

Run: `conda run -n Elysia python scripts/stage4_smoke.py`
Expected: 全部 PASS（24 → 27 检查），末尾 `结果: 27 通过 / 0 失败`，退出码 0。`scripts/tests/test_stage4_smoke.py`（slow）只断言非空，不受影响。

- [ ] **Step 4: 提交**

```bash
git add scripts/stage4_smoke.py
git commit -m "test(smoke): discard 采集失败清理 + 空录音迟到删除真实序列（BE-S4-RUN-P0-01）"
```

---

### Task 4: 活跃文档同步

**Files:**
- Modify: `docs/electron-backend-protocol-v2.md`（`:211`、`:390`、`:402`、§12.2）
- Modify: `docs/superpowers/specs/2026-08-05-electron-backend-batch6-voice-modes-tts-control.md`（`:74-81` 状态机图、`:84`、`:87`、`:121`）

**Interfaces:**
- Consumes: Task 1 定案的错误码（`VOICE_SESSION_CONFLICT`）与幂等语义（D1/D3）。
- Produces: 无——文档措辞与生产行为一致。

- [ ] **Step 1: 修订 `docs/electron-backend-protocol-v2.md`**

`:211` 行 `VOICE_RECORDING_NOT_FOUND` 描述改为：

```markdown
| `VOICE_RECORDING_NOT_FOUND` | 404 | false | IDLE 态收 stop/commit（未知 recordingId，批次 6）；**`voice.discard` 已幂等成功不再产生**（阶段4真实联调修订） |
```

`:390` 行失败信封区分句末追加：

```markdown
`voice.discard` 成功（含 IDLE/重复幂等）为 send-only 无 ACK；仅 recordingId 不匹配或 realtime/dictation 活跃时报 `error{VOICE_SESSION_CONFLICT}`（阶段4真实联调修订）。
```

`:402` 行 discard 注释改为：

```markdown
client → {"type":"voice.discard","recordingId":"…"}（取消任意未提交阶段：RECORDING/TRANSCRIBING/PENDING；IDLE/重复幂等成功，send-only 无 ACK，阶段4真实联调修订）
```

§12.2 末尾（`- 录音缓冲超 600s...` 之后）追加一条 bullet：

```markdown
- **`voice.discard` 取消原语（阶段4真实联调修订）**：可取消当前录音的任意未提交阶段——RECORDING（未 stop，采集启动失败场景）、TRANSCRIBING（取消在途 STT，迟到结果不复活）、PENDING（丢弃字幕/情绪/events）；录音已结束（IDLE）后迟到/重复 discard 幂等成功，不发错误。错误 recordingId 或 realtime/dictation 活跃 → `error{VOICE_SESSION_CONFLICT}`，绝不误清其它会话。成功后不调 STT、不发 `voice.transcript`、不触发 generation。
```

- [ ] **Step 2: 修订 batch6 spec**

状态机图（`:74-81`）在 `IDLE ──voice.record.start──▶ RECORDING` 后补一条、`PENDING ──voice.discard──▶ IDLE` 后补一条：

```text
RECORDING ──voice.discard──▶ IDLE           （取消任意未提交阶段，不要求先 stop；阶段4真实联调修订）
IDLE ──voice.discard──▶ IDLE               （幂等成功；阶段4真实联调修订）
```

`:84` 边界规则改为：

```markdown
- RECORDING 态收 `commit` → `CONTRACT_VALIDATION`（未 stop 不得提交）；收 `discard` → **允许**（取消任意未提交阶段，阶段4真实联调修订）。
```

`:87` 边界规则改为：

```markdown
- IDLE 态收 `stop`/`commit` → `VOICE_RECORDING_NOT_FOUND`；收 `discard` → **幂等成功**（阶段4真实联调修订）。
```

`:121` 契约行改为：

```markdown
| `voice.discard` | `recordingId` | 丢弃任意未提交阶段（RECORDING/TRANSCRIBING/PENDING）；IDLE/重复幂等成功（阶段4真实联调修订） |
```

`:290` 错误码目录行（与 protocol-v2 `:211` 同措辞）改为：

```markdown
| `VOICE_RECORDING_NOT_FOUND` | 404 | false | IDLE 态收 stop/commit（未知 recordingId）；**`voice.discard` 已幂等成功不再产生**（阶段4真实联调修订） |
```

- [ ] **Step 3: 验证无残留旧措辞**

Run: `grep -rn "先 stop 再 discard\|IDLE 态收 stop/commit/discard\|丢弃 pending/在途录音" docs/ scripts/ core/`
Expected: 历史 plan 的史实记录命中且**不动**——`docs/superpowers/plans/2026-08-05-...batch6...plan.md:761`、`docs/superpowers/plans/2026-08-15-stage4-联调前整改.md:895-897`；本批工作产物（本 plan、spec、review 清单）含旧语义叙述命中属**预期**；**活跃文档零命中**——`docs/electron-backend-protocol-v2.md`、`docs/superpowers/specs/2026-08-05-...batch6...spec.md`、`scripts/stage4_smoke.py`、`core/input/voice_sessions.py`。

- [ ] **Step 4: 提交**

```bash
git add docs/electron-backend-protocol-v2.md docs/superpowers/specs/2026-08-05-electron-backend-batch6-voice-modes-tts-control.md
git commit -m "docs(voice): voice.discard 幂等/取消语义同步（BE-S4-RUN-P0-01）"
```

---

## 门禁（全部任务完成后主审查跑）

```bash
make test-quick        # 预期全绿（3617+ 基础 + 本批新增/修改）
make test-normal       # 预期全绿
make lint              # ruff All passed
make pyright           # 0 errors（Elysia env）
make check-types       # frontend_contract --check + gen_contract_frames --check 全绿（golden 零 churn）
conda run -n Elysia python scripts/stage4_smoke.py   # 27 检查全 PASS 退出 0
```

## Self-Review

- **Spec 覆盖**：§3.4 目标行为表 6 行 → Task 1 转场分支逐行对应；§3.4 补充约束 6 条 → Task 1 代码 + 测试（约束 1 由 `test_discard_no_stt_no_transcript_no_message`、约束 2 由 `test_discard_transcribing_cancels_stt`、约束 3/5 由幂等测试与 `close()` 保留、约束 4 由 mismatch 测试、约束 6 由不改 `stop` 语义）；§3.5 文档清单 → Task 4（历史文档按 D3 不动）；§3.6 11 条测试 → Task 1（#1-#9 单元）+ Task 2（#9/#10 + IDLE）+ Task 3（现象 A/B 序列）+ 既有回归保留（#11）。
- **占位符扫描**：无 TBD/TODO；每个代码步骤含完整代码。
- **类型一致性**：`discard_recording(recording_id: str) -> None` 签名不变；`VoiceSessionError(ERR_CONFLICT, ...)` 复用既有 `ERR_CONFLICT` 常量（`voice_sessions.py:40`）；helper 参数全部类型注解。
