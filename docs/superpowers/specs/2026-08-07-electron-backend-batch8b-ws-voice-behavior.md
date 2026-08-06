# Electron Backend Batch 8b Spec — WS/语音行为修复

**版本：** v0.3（v0.2 经最终 review 修订：§3.12 补提交路径残段防线——teardown 取消在途任务 + `_on_segment` epoch 复检；§7 记录三已知限制）
**日期：** 2026-08-07
**上游：** [audit-batches0-7](docs/electron-backend-audit-batches0-7.md) D 簇（WS/语音行为）13 条未修复项
**前置批次：** [8a](docs/superpowers/specs/2026-08-06-electron-backend-batch8a-security-attachment-search.md)（安全+附件+搜索，D1-D5）
**相关文档：** [protocol-v2 §12 语音与 TTS 播放契约](docs/electron-backend-protocol-v2.md)、[8a audit D 簇标注](docs/electron-backend-audit-batches0-7.md)、[batch6 voice-modes spec](docs/superpowers/specs/2026-08-05-electron-backend-batch6-voice-modes-tts-control.md)

## 1. 背景与范围

批次 8 全量审查（audit-batches0-7）产出 56 条发现，8a 修复 20 条 + 3 合规缺口；本批次修复 **D 簇 WS/语音行为 13 条**（5 important + 8 minor），8c 承接剩余 E/F 簇（契约/信封/ID/杂项）19 条。

### 1.1 范围清单（13 条）

| # | 文件:行 | 严重度 | 问题 | 修复章节 |
|---|---------|--------|------|----------|
| 1 | `core/input/ws_handler.py:337`（断连归属门；audit 锚 333） | important | 断连误杀其他连接 generation | §3.1 (D1) |
| 2 | `core/input/ws_handler.py:607`（纯文本 ACK+process；audit 锚 603） | important | 队列满仍发 accepted ACK | §3.7 |
| 3 | `core/cognitive/loop.py:326` | important | `_interrupt_source` 跨回合泄漏 | §3.4 (D4) |
| 4 | `core/input/voice_sessions.py:375` | important | RECORDING 残留帧自动提交 | §3.2 (D2) |
| 5 | `core/input/voice_sessions.py:371` | important | cancel 无法抢占在途 VAD 段 | §3.3 (D3) |
| 6 | `core/input/ws_handler.py:695`（空文本分支；audit 锚 693） | minor | v2 空文本静默丢弃 | §3.8 |
| 7 | `core/cognitive/loop.py:922` | minor | 空响应泄漏 v1 message 帧 | §3.5 (D5) |
| 8 | `core/adapter/tts_adapter.py:227` | minor | cancel_stream audio_end 后返回 True | §3.9 |
| 9 | `core/input/voice_sessions.py:348` | minor | dictation ID 校验不一致 | §3.10 |
| 10 | `core/input/voice_sessions.py:165` | minor | `_restore_emotion` 抹掉他连接 emotion | §3.6 (D6) |
| 11 | `core/adapter/tts_adapter.py:57` | minor | `_wav_info` 固定偏移 | §3.11 |
| 12 | `core/input/ws_handler.py:574` | minor | 附件幂等 entry 异常残留 | §3.7 |
| 13 | `core/input/ws_handler.py:603` | minor | process_message 抛异常变体（同根因 #2） | §3.7 |

### 1.2 已拍板决策（D1-D6）

| 决策 | 内容 | 拍板 |
|------|------|------|
| D1 | 断连按归属取消——仅当在途 generation 由断开连接发起才取消 | 用户确认「按归属取消」 |
| D2 | v2 全程禁 legacy——v2 连接 BINARY 帧只在 voice_mgr 会话内有效，无会话丢弃 | 用户确认「v2 全程禁 legacy」 |
| D3 | STT 后台解耦——语音段转写移后台任务，cancel 可丢弃残段 | 用户确认「STT 后台解耦」 |
| D4 | `_interrupt_source` 仅在实际取消到任务时记录 | 按推荐 |
| D5 | v2 空响应发 v2 兼容空 segment，不发 v1 message 帧 | 按推荐 |
| D6 | dictation 用独立 STT wrapper，不写共享 `stt_emotion_state`，移除快照/恢复 | 按推荐 |

---

## 2. 全局约束（继承项目规范）

- **禁相对导入**（ruff TID252 强制）；直接引子模块（`from core.input.voice_sessions import ...`），不绕包重导出。
- **代码注释用中文**；README 用中文。
- **`Agent` 公开接口纪律**：跨模块消费禁止 `agent._xxx`，一律走公开 property。新增只读接口**沿用 `agent.cognitive` 既有转发链**（`agent.py:735` 已返回 loop），在 `CognitiveLoop` 上暴露即可，**不新增 Agent 顶层同名 property**。
- **conda 环境 Elysia** 测试；新测试带 marker（unit/service/integration/slow）。
- **契约一致性**：WS ACK/error 帧用错误码目录标准码（`INTERNAL`/`QUEUE_FULL`/`CONTRACT_VALIDATION`/`DICTATION_NOT_FOUND` 等，protocol-v2 §9.1），不发明新码。
- **敏感纪律（BE-081）**：日志/ACK 只记文本长度与 message_id，不记正文。
- 行为变更必须保持 **v1 兼容**（v1 客户端 legacy 语音/MessageObj 帧路径不动）。

---

## 3. 修复设计

### 3.1 D1 断连按归属取消（ws_handler.py:333）

**问题：** v2 连接断开时 `finally` 无条件 post `InterruptData(source='disconnect')`（只要 `not is_completely_idle`），取消的是 agent 全局「当前」认知任务——多标签/多设备下 Tab B 关闭误杀 Tab A 的生成（audit §3 第 1 条验证链）。

**设计：** 断连只取消「该连接发起」的在途 generation。

1. **`core/cognitive/loop.py`** —— 新增公开只读 property：
   ```python
   @property
   def current_generation_user_message_id(self) -> str | None:
       """当前在途 generation 的用户消息 ID（D1 断连归属判定）。无在途/未建档 → None。"""
       if self._generation_ctx is None:
           return None
       return self._generation_ctx.user_message_id or None
   ```
   （`_generation_ctx.user_message_id` 为 `""` 时返回 None——v1 消息无预分配 ID，不参与归属判定。）

2. **`core/input/ws_handler.py`** —— `handle_ws` 维护连接级 `sent_message_ids: set[str] = set()`：
   - 贯通 `handle_json_message(..., sent_message_ids=None)` → `_route_v2_text(..., sent_message_ids=None)` 与 `_handle_voice_commit(..., sent_message_ids=None)`。
   - `_route_v2_text`：纯文本路径 accepted 时、media 路径 accepted 时，`if sent_message_ids is not None: sent_message_ids.add(message_id)`。
   - `_handle_voice_commit`：ACK + post UserInputData 前 `sent_message_ids.add(message_id)`。
   - `finally` 块：在 `voice_mgr.close()` **之前**并入语音消息 ID：
     ```python
     if voice_mgr is not None:
         sent_message_ids.update(voice_mgr.emitted_message_ids)
         await voice_mgr.close()
     ```
   - 断连中断判定改为归属检查（替换原 `not is_completely_idle` 门）：
     ```python
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
   - `getattr(agent, "cognitive", None)` 兜底 `_FakeAgent` 测试桩。

3. **`core/input/voice_sessions.py`** —— `VoiceSessionManager` 记录本连接语音段提交的消息 ID：
   - `__init__` 加 `self._emitted_message_ids: set[str] = set()`（连接级累积；`set` 防长连接无限增长，M）。
   - `_on_segment` REALTIME 分支创建 `message_id` 后 `self._emitted_message_ids.add(message_id)`（DICTATION 无消息副作用，不记录）。
   - 只读 property：`def emitted_message_ids(self) -> set[str]: return set(self._emitted_message_ids)`。
   - `_teardown_session` **不**清空该集合（连接级累积，供断连判定；`close()` 后仍可读）。

**语义：** 发起者断开 → 保留批次 2「断开取消在途生成」重连语义；非发起者断开 → 不误杀。`getattr` 链保证任何异常/测试桩降级为不取消（安全侧）。与 D4 组合：归属命中但任务已 done（IDLE_BG 窗口）→ cancel_current 不再记 sticky source（D4），无残留。

### 3.2 D2 v2 全程禁 legacy（voice_sessions.py:375）

**问题：** RECORDING 超限/失败/空转写触发 `_reset_recording()` → `_mode=IDLE`，残留/持续 BINARY 帧在 `ws_handler` BINARY 分支回落到 legacy `audio_buf.feed()` → VAD 自动提交 → 不经 `voice.commit` 直接生成用户消息，破坏「确认后才形成语音输入」契约（audit 验证链完整）。

**设计：** v2 连接 BINARY 帧只在 voice_mgr 会话内有效；无活动会话 → 丢弃，不落 legacy。

`core/input/ws_handler.py` BINARY 分支（~303-310）：
```python
elif msg.type == WSMsgType.BINARY:
    if voice_mgr is not None and voice_mgr.has_active_session():
        await voice_mgr.feed_pcm(msg.data)
    elif protocol_version < 2 and audio_buf and agent.stt_enabled:
        await audio_buf.feed(msg.data)
    # v2 无活动语音会话 → 丢弃（legacy 自动提交仅 v1；RECORDING 复位残留帧不绕过 commit）
```

- `protocol_version` 在 `handle_ws` 作用域已定义（line 337 使用）。
- **v1 路径不变**（v1 连接 `protocol_version < 2`，legacy 照常）。
- 前端批次 6 起 v2 语音全部走 voice_mgr 三模式，无 v2 legacy 依赖——行为收敛而非破坏。

### 3.3 D3 STT 后台解耦（voice_sessions.py:371）★ 最大任务

**问题：** WS 消息串行循环中 `feed_pcm → buffer.feed → _process_segment → stt_transcribe`（`asyncio.wait_for(..., 30.0)`）阻塞循环，`voice_session.cancel` 只能等段 STT 完成、`_on_segment` 已提交 partial+turn 并 post UserInputData 后才处理——残段仍成轮次，违背 spec §5.2「cancel = 丢弃残段」。

**设计：** REALTIME/DICTATION 的 AudioBuffer 注入「派发型 STT」wrapper——立即派发后台转写任务并返回空串（feed 不阻塞消息循环）；转写完成按派发序号**顺序提交**；cancel/stop 语义保持（cancel 丢弃残段，stop/dictation-stop 等待在途段完成）。

1. **`VoiceSessionManager` 新增内部方法：**
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
           # 不加锁时早段提交可能读到晚段 emotion。锁只串行 STT 本身（与改前一致），
           # 消息循环仍不阻塞。
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
       """按派发序号顺序提交（提交锁串行化——防多任务并发进入 while 导致
       partial/turn 帧交错，M）；防并发乱序破坏 turn_id。"""
       async with self._stt_commit_lock:
           while self._stt_commit_seq in self._stt_pending:
               text, emotion, events = self._stt_pending.pop(self._stt_commit_seq)
               self._stt_commit_seq += 1
               if not text:
                   continue         # 超时/失败段：不补 partial/turn（符合现状空段不提交）
               if self._mode in (VoiceMode.REALTIME, VoiceMode.DICTATION) \
                       and not self._paused:
                   await self._on_segment(text, emotion=emotion, events=events)
   ```
   - **超时/失败段写空 entry（防死锁）：** `_run_dispatch_stt` 异常路径把 `(text="", ...)` 写入 `_stt_pending` 而非提前 return——否则 `_commit_ready_stt` 永远等不到该 seq，后续段永久卡死。空 text 在提交循环 `continue` 推进 `_stt_commit_seq`。
   - `__init__` 加：`self._stt_seq = 0`、`self._stt_epoch = 0`（**单调递增，teardown 时 `+=1`，绝不归零**）、`self._stt_pending: dict[int, tuple[str, str, list[str]]] = {}`、`self._stt_commit_seq = 0`、`self._stt_tasks: set[asyncio.Task] = set()`、`self._stt_stt_lock = asyncio.Lock()`、`self._stt_commit_lock = asyncio.Lock()`。
   - `_teardown_session`：重置 `_stt_seq=0`/`_stt_pending={}`/`_stt_commit_seq=0`/`_stt_tasks=set()`，并 **`_stt_epoch += 1`**（不归零）——与现有 `_turn_id/_segment_index` 重置并列。`_stt_epoch` 是「会话代际」：cancel/stop 后旧会话在途任务持旧 epoch，新会话启动后 epoch 已递增，旧任务提交前 epoch 校验失败被丢弃（B1）。

2. **`_on_segment` 段级情绪参数**（F3）：REALTIME 分支不再提交时读共享 state，改用提交传入的段级 emotion/events；缺省参数保留 legacy AudioBuffer 回调兼容：
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
           self._emitted_message_ids.add(message_id)   # D1 归属（§3.1）
           await self._emit(VoiceSessionTurnMessage(
               sessionId=self._session_id, turnId=self._turn_id,
               messageId=message_id, conversationId=self._conv_id, text=text).wire_dict())
       elif self._mode == VoiceMode.DICTATION:
           from frontend_contract import DictationPartialMessage
           self._dict_text.append(text)
           await self._emit(DictationPartialMessage(text=text).wire_dict())
   ```

3. **构造 AudioBuffer 时注入 wrapper：**
   - `start_realtime`：`stt_transcribe=self._make_dispatch_stt(self._agent.stt_transcribe)`。
   - `start_dictation`：`stt_transcribe=self._make_dispatch_stt(self._transcribe_no_emotion)`（见 §3.6）。

4. **`stop_session` / `stop_dictation` 保持「在途段等待完成」语义**（D3 关键回归防线，F1）：
   ```python
   async def stop_session(self, session_id: str) -> None:
       self._require_session(session_id)
       if self._buffer:
           await self._buffer.flush()          # 派发最后一段（feed 立即返回）
       await self._drain_dispatch_stt()        # 等待在途段完成再 teardown（残段仍提交）
       self._teardown_session()
       await self._emit(VoiceSessionEndedMessage(sessionId=session_id, reason="stopped").wire_dict())

   async def stop_dictation(self, client_request_id: str) -> None:
       if (self._mode != VoiceMode.DICTATION
               or self._dict_request_id != client_request_id):
           raise VoiceSessionError(ERR_DICTATION_NOT_FOUND, "无活动转写")
       if self._buffer:
           await self._buffer.flush()
       await self._drain_dispatch_stt()        # F1：等最后一段提交进 _dict_text 再 join
       text = "".join(self._dict_text)
       self._teardown_session()
       await self._emit(DictationFinalMessage(
           clientRequestId=client_request_id, text=text).wire_dict())
   ```
   - `_drain_dispatch_stt`（任务集合方案）：等待 `_stt_tasks` 中未完成任务（在途段仍提交，保持 stop 语义）：
     ```python
     async def _drain_dispatch_stt(self) -> None:
         """等待在途派发 STT 段完成（stop/dictation-stop 路径：残段仍提交，spec §5.2）。"""
         pending = [t for t in self._stt_tasks if not t.done()]
         if pending:
             await asyncio.gather(*pending, return_exceptions=True)
     ```
     `_run_dispatch_stt` 的 `finally` 已 self-discard（见上），drain 后 `_stt_tasks` 自然清空。**cancel 路径不 drain**——`cancel_session`/`cancel_dictation` 保持立即 `_teardown_session()`，在途段随 epoch 失配被丢弃（这正是「丢弃残段」）。
   - `_idle_check_loop` 超时路径不变（直接 `_teardown_session`，在途段丢弃——与现状超时不 flush 一致）。

5. **并发安全：** 派发是**并发**的（feed 立即返回，用户下一段话音可在上一段 STT 在途时触发派发）——解耦的必然，正确性由三道防线保证：
   - `_stt_stt_lock`：串行化「转写 + emotion 捕获」，保证每段 emotion 原子取自本段转写（F3）；
   - `_stt_commit_lock`：串行化提交 while 循环，保证 partial/turn 帧不交错（M）；
   - `_stt_epoch` 代际校验：cancel/stop 后旧会话在途任务提交前 `epoch != self._stt_epoch` 即丢弃，即使新会话已启动（B1）。
   - `turn_id` 单调性由 `_commit_ready_stt` 按 `_stt_commit_seq` 顺序提交保证（提交在锁内，`_on_segment` 的 `_turn_id += 1` 无并发交错）。

### 3.4 D4 `_interrupt_source` 只在实际取消时记录（loop.py:326）

**问题：** `cancel_current` 先存 sticky source 再检查 task 是否 done（loop.py:326-334）。回合完成后的 IDLE_BACKGROUND_TASK 窗口断线：task 已 done → 不取消但 source 被记录；下一回合用户取消被「disconnect 不被非 disconnect 覆盖」规则锁死 → registry 标 `interrupted` 而非 `cancelled`，与 wire `CANCELLED` 矛盾（audit 验证链 7 步）。

**设计：** source 记录移入「task 实际被取消」分支：
```python
def cancel_current(self, source: str | None = None) -> bool:
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
- 语义：没有任务被取消 = 没有发生中断 = 不留 sticky source。
- 注释（321-324「只增不减」）同步更新，说明记录条件收窄到实际取消。
- **组合**：D1 归属命中但 task 已 done（IDLE_BG）→ 不断言 source → 下一回合用户取消正常记 `cancelled`。

### 3.5 D5 v2 空响应发空 segment（loop.py:922）

**问题：** `_finalize_turn` 中「有 public_text 但从未流式输出（`not speaking_started`）」兜底向 `self._output.send(MessageObj(...))` 发 v1 message 帧——v2 通道收到无 segment 关联的 `message` 帧，破坏 v2 段语义契约（audit minor #7）。

**设计：**
1. **`core/cognitive/generation.py`** —— `GenerationContext` 加字段：
   ```python
   protocol_version: int = 1
   ```
2. **`core/cognitive/loop.py`** —— process() 构造 ctx（line 461）加 `protocol_version=protocol_version`。
3. **`_finalize_turn`** 兜底（~921-924）分支化：
   ```python
   if not speaking_started:
       await self._output.typing(False)
       ctx = self._generation_ctx
       if ctx is not None and ctx.protocol_version >= 2:
           # v2：发 v2 兼容 segment（channel=public_reply）——替代 v1 message 帧，
           # 客户端以 segment.completed.content 为渲染真值（protocol-v2 §2 注）。
           # F4：TEXT_DELTA 已流式开段但未出 SENTENCE 边界时（speaking_started 仍 False），
           # current_segment_id 已非 None——直接完成现有段，不重复 open_segment
           # （重复 open 会覆盖 current_segment_id，旧段悬空 + 文本重复渲染）
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
- **v1 路径**：MessageObj 仅保留给 v1（`else` 分支）；v2 走 `segment_started + segment_completed(content)`，无 delta（completed.content 为渲染源）。v1 通道在 v2 回合的此兜底收到 `segment_completed` 的 `on_message ""` 空边界——delta 已流式场景下 v1 已收到全文，空边界正确闭合气泡；纯兜底无流式场景理论不可达（public_text 由已流式 token 重建）。
- `_generation_ctx` 在 `_finalize_turn` 作用域可读（line 928-929 已用）。

### 3.6 D6 dictation emotion 隔离（voice_sessions.py:165）

**问题：** `agent.stt_transcribe`（agent.py:836-844）原地写 agent 级共享 `stt_emotion_state`。dictation 窗口内其转写会覆盖该状态，结束后 `_restore_emotion` clear+update 回写快照——抹掉 dictation 期间**其他连接** STT 写入的 emotion/events（audit minor #10）。

**设计：** dictation 用独立转写 wrapper，不写共享状态，彻底移除快照/恢复。

1. **`VoiceSessionManager` 新增：**
   ```python
   async def _transcribe_no_emotion(self, wav: bytes) -> str:
       """转写但不写共享 stt_emotion_state（D6：dictation 情绪隔离）。
       dictation 只回文字，本就不该触碰 agent 级情绪——隔离后无需快照/恢复。"""
       backend = getattr(self._agent, "stt", None)
       if backend is None:
           return ""
       return await backend.transcribe(wav)
   ```
2. **`start_dictation`**：AudioBuffer `stt_transcribe=self._make_dispatch_stt(self._transcribe_no_emotion)`；删除 `_emotion_snapshot` 快照（~330-334）。
3. **删除恢复机制：** `_restore_emotion` 方法、`__init__` 的 `_emotion_snapshot`、以及四个调用点（`stop_dictation` / `cancel_dictation` / `notify_stt_disabled` / `close`）。
- REALTIME 保持用 `agent.stt_transcribe` 转写（情绪随段更新）——但经 D3 §3.3 步骤 2 的**段级情绪捕获**贯通：`_run_dispatch_stt` 在 STT 锁内转写完成后原子捕获 emotion/events，`_on_segment` 提交时用段级值而非共享 state（F3 修正，D3 与 D6 正交）。
- RECORDING 的 emotion 快照读（line 219-222，读当前值供 commit）不受影响。

### 3.7 队列满 ACK 对齐 + 异常变体 + 附件 entry 残留（ws_handler.py:603/574）

**问题：** 纯文本 v2 路径（603-610）忽略 `process_message` 返回值仍发 accepted；media 路径（582-605）`process_message` 抛异常时幂等 entry 残留且异常冒泡 crash 连接；两处同根因（audit #2/#12/#13）。

**设计：** 两条路径统一「先 process 后判定 ACK」，异常 → 删 entry + rejected，不 crash 连接。

`_route_v2_text` 纯文本路径（替换 607-610）：
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
                    error={"code": "INTERNAL", "message": "消息处理失败", "retryable": False})
    return True
if accepted:
    await _send_ack(ws, message_id, client_request_id, client_message_id,
                    ack_conversation_id, "accepted")
    if sent_message_ids is not None:
        sent_message_ids.add(message_id)   # D1
else:
    # 队列满 → 删幂等 entry（重试允许重新进入）+ rejected QUEUE_FULL
    store.delete(client_request_id)
    await _send_ack(ws, "", client_request_id, client_message_id,
                    ack_conversation_id, "rejected",
                    error={"code": "QUEUE_FULL",
                           "message": "Agent 忙，消息队列已满", "retryable": True})
return True
```

media 路径（582-605）process_message 段包 try/except：
```python
try:
    accepted = await agent.process_message(...)
except Exception:
    logger.warning("附件消息 process_message 抛异常——删幂等 entry", exc_info=True)
    store.delete(client_request_id)
    await _send_ack(ws, "", client_request_id, client_message_id,
                    ack_conversation_id, "rejected",
                    error={"code": "INTERNAL", "message": "消息处理失败", "retryable": False})
    return True
if accepted:
    ... 现有 accepted 分支（refs 递增 + ACK + sent_message_ids.add） ...
else:
    ... 现有 QUEUE_FULL 分支（不变） ...
```

- 错误码 `INTERNAL`/`QUEUE_FULL` 均已登记 protocol-v2 §9.1 目录（`INTERNAL` 行注明「未归类内部错误」，`QUEUE_FULL` 行注明 v2 ACK 场景）。
- `_send_ack` 签名不变（397-408）。

### 3.8 v2 空文本 ACK（ws_handler.py:693）

**问题：** 空文本消息（无 sticker/attachments）被 `handle_json_message` 的 `elif isinstance(text_content, str) and (...)` 条件整体跳过，v2 静默无 ACK（audit minor #6）。

**设计：** `handle_json_message` text 分支改嵌套，v2 空文本发 rejected：
```python
elif isinstance(text_content, str):
    if text_content.strip() or data.get("stickerId") or data.get("attachments"):
        ... 现有逻辑（_route_v2_text / v1 process_message） ...
    elif _is_v2(agent, ws):
        # v2 空文本 → rejected CONTRACT_VALIDATION（不再静默丢弃）
        await _send_ack(ws, "", data.get("clientRequestId", ""),
                        data.get("clientMessageId"), data.get("conversationId"),
                        "rejected",
                        error={"code": "CONTRACT_VALIDATION",
                               "message": "消息内容为空", "retryable": False})
```
- `_is_v2(agent, ws)` 已存在（line 365）。
- v1 空文本行为不变（静默忽略）。

### 3.9 cancel_stream audio_end 后返回 False（tts_adapter.py:227）

**问题：** `cancel_stream` 以「task 存活」判定（227-231）——任务在 audio_end（`_audio_forwarding` 已 discard）后的 emoji/finally 窗口仍存活 → 返回 True；取消后 CancelledError 分支 `sid not in _audio_forwarding` 不发 audio_cancelled → 客户端得「成功」却无事件，违背 spec §8.2「已 audio_end → PLAYBACK_NOT_FOUND」（audit minor #8）。

**设计：** 判定改为与「CancelledError 分支是否补 audio_cancelled」一致——`sid in _audio_forwarding`：
```python
def cancel_stream(self, stream_id: int) -> bool:
    """取消指定播放实例（BE-063）；正在转发（audio_start 后 audio_end 前）返回
    True（任务补 audio_cancelled），已 audio_end / 不存在返回 False（PLAYBACK_NOT_FOUND）。"""
    task = self._task_by_sid.get(stream_id)
    if task is None or task.done():
        return False
    if stream_id not in self._audio_forwarding:
        return False           # 已 audio_end（末尾 emoji/finally 窗口）——不再补事件
    task.cancel()
    return True
```
- 调用方 `ws_handler.py:976-978` 已把 False → `PLAYBACK_NOT_FOUND`，无需改动。
- **已知残留窗口（记录）：** `_download_and_forward` 中 `await audio_end(...)` 与 `self._audio_forwarding.discard(sid)` 之间有一次 await（tts_adapter.py:407-408）——该窗口内 `cancel_stream` 仍判定 `sid in _audio_forwarding` → 返回 True，任务取消后 CancelledError 分支补 `audio_cancelled`。客户端在极窄窗口内可能先收 `audio_end` 再收 `audio_cancelled`；前端 audio_cancelled 为幂等清理（播放已毕则 no-op），影响可忽略，不做任务内 ended 状态标记（复杂度不抵收益）。

### 3.10 dictation ID 校验（voice_sessions.py:348）

**问题：** `stop_dictation`/`cancel_dictation` 只查 `_mode == DICTATION`，不校验传入 clientRequestId 与 `_dict_request_id` 一致——与 `record.stop`/`voice_session.stop` 的 ID 校验不一致（audit minor #9）。

**设计：** 与 RECORDING 一致——ID 不一致同样拒绝：
```python
async def stop_dictation(self, client_request_id: str) -> None:
    if (self._mode != VoiceMode.DICTATION
            or self._dict_request_id != client_request_id):
        raise VoiceSessionError(ERR_DICTATION_NOT_FOUND, "无活动转写")
    ...
```
`cancel_dictation` 同理（校验并入现有 if 条件）。错误码 `ERR_DICTATION_NOT_FOUND`（已定义 `voice_sessions.py:36`）。D3 的 `stop_dictation` 完整形态见 §3.3 步骤 4（含 `_drain_dispatch_stt`）；本节的 ID 校验是其前置门。

### 3.11 `_wav_info` chunk 解析（tts_adapter.py:57）

**问题：** `_wav_info` 固定偏移 40 读 data chunk 大小、固定 24/28 读 sample_rate/byte_rate——含额外 chunk（LIST/fact/cue）的 WAV 头非 44 字节，得到错误 durationMs（audit minor #11，PLAUSIBLE）。

**设计：** 遍历 chunk 头定位 `fmt `/`data`：
```python
def _wav_info(audio: bytes) -> tuple[int | None, int | None]:
    """解析 WAV header → (sample_rate, duration_ms)；非 WAV/异常返回 (None, None)。

    遍历 RIFF chunk 表定位 fmt/data（不假设标准 44 字节头）——含额外 chunk
    （LIST/fact/cue）的 WAV 同样正确解析。
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
- 标准 44 字节头仍正确（fmt chunk offset=12，sample_rate 绝对 24、byte_rate 绝对 28——与现行为一致）。

### 3.12 防御清单（D3 边界）

- **提交异常降级：** `_run_dispatch_stt` 的 `_commit_ready_stt` 调用已包 try/except（§3.3 代码块内）——连接已关 `_on_segment` emit 失败时降级 debug，防 task-exception 噪音（仿 `_ack_loop` 476-487 模式）。
- **迟到帧双闸：** `_dispatch` 在 `self._mode == IDLE` 时返回空串**不派发**（防 teardown 后 `_buffer.feed` 残调——实际 teardown 后 `_buffer=None`，feed 早退）；`_run_dispatch_stt` 在「epoch 失配 或 mode==IDLE」检查点 return 不写 pending（防 cancel/stop 后迟到结果串流到新会话或 IDLE 态）。
- **跨会话隔离：** `_stt_epoch` 在 `_teardown_session` 单调 `+=1`（绝不归零），是旧会话在途任务被丢弃的最终防线——即使 cancel 后立即重启新会话（mode≠IDLE）也不吞入旧段。
- **提交路径残段防线（最终 review 补充）：** ① `_teardown_session` **取消在途派发任务**（`_stt_tasks` 中非当前任务 `cancel()`，跳过自取消）——cancel 后立即重启不再卡在旧任务占用的 STT 锁上，且迟到结果不浪费后端调用；② `_on_segment` REALTIME 分支在 **partial emit await 后、post UserInputData 前**做 epoch/mode 复检（`epoch != self._stt_epoch or mode != REALTIME → return`）——关闭「提交途中 cancel 仍 post UserInputData」的窄窗口（spec §5.2「丢弃残段」在提交路径的兜底）。

---

## 4. 测试计划

新测试加 marker，全部走 conda Elysia。按修复章节组织：

| 章节 | 测试要点 | marker |
|------|---------|--------|
| §3.1 D1 | 非发起连接断开 → 不 post disconnect interrupt（generation 不被取消）；发起连接断开 → 照常取消；voice 段归属并入 | service |
| §3.2 D2 | v2 连接无语音会话 BINARY → 不 feed legacy audio_buf（断言 audio_buf.feed 未调用）；v1 照常 | service |
| §3.3 D3 | 在途 STT 段 + cancel → 段不提交为轮次（无 partial/turn/UserInputData）；**stop 等待在途段完成（残段提交）**；**stop_dictation 等待最后一段进 _dict_text（dictation 最终文本不丢段）**；**cancel→立即 restart 新会话，旧会话在途段不落入新会话（epoch 隔离）**；**乱序完成的两段 → 各自 emotion 取本段（不张冠李戴）**；顺序提交不破坏 turn_id | service |
| §3.4 D4 | IDLE_BG 窗口 disconnect → 下一回合用户取消 mark=cancelled（非 interrupted） | service |
| §3.5 D5 | v2 空响应兜底 → 发 segment.completed 非 message 帧；v1 仍发 MessageObj | service |
| §3.6 D6 | dictation 转写不写共享 stt_emotion_state；dictation 前后共享状态不变；其他连接 STT 结果不被抹掉 | service |
| §3.7 | 纯文本队列满 → rejected QUEUE_FULL + entry 删；process_message 抛异常（mock）→ rejected INTERNAL + entry 删 + 连接不 crash；media 抛异常同样 | service |
| §3.8 | v2 空文本 → rejected CONTRACT_VALIDATION；v1 空文本静默 | service |
| §3.9 | cancel_stream 在 audio_end 后 → False（PLAYBACK_NOT_FOUND）；audio_start 后 audio_end 前 → True 且补 audio_cancelled | service |
| §3.10 | stop_dictation/cancel_dictation ID 不一致 → DICTATION_NOT_FOUND | service |
| §3.11 | 含 LIST 额外 chunk 的 WAV → 正确 sample_rate/duration_ms；标准 44 头不变 | unit |

---

## 5. 文档更新

- **`docs/electron-backend-audit-batches0-7.md`**：13 条 D 簇标注「⏳ 待 8b」→「✅ 批次 8b 已修复」+ 对应 commit；§1 合规表核对。
- **`docs/electron-backend-progress.md`**：批次 8b 节（D1-D6 决策 + 完成明细）。
- **`docs/electron-backend-protocol-v2.md`**：若契约面变化（D5 segment 兜底、§3.7 ACK 错误码）同步。
- **`CLAUDE.md`**：core 层结构注释更新（voice_sessions/tts_adapter 相关，若有接口增删）。
- 涉及模块 README（`core/input/` 等）若新增公开接口同步。

## 6. 合规对照（audit 13 条）

修复完成后 audit 对应条目全部转为「✅ 已修复」，验证依据处补 commit。8c 承接 E/F 簇 19 条（不属本批次）。

## 7. 已知限制（记录）

- D3 修复后，REALTIME 段 partial 字幕在**段 STT 完成后**才到达（后台派发）——即 partial 的实时性取决于 STT 延迟，与「VAD 检测到静音后即刻显示」的即时性略有差异。行为契约不变（partial 仍逐段按序），前端依赖 partial 做即时字幕的体验可能滞后一个 STT 周期。若不可接受，可在后续批次引入「分段转写流式化」（非本批次范围）。
- §3.9 `cancel_stream` 极窄窗口（`audio_end` await 与 `_audio_forwarding.discard` 之间）可能先发 `audio_end` 再发 `audio_cancelled`——前端幂等清理，影响可忽略（见 §3.9）。
- §3.5 D5 兜底路径下，v1 通道在 v2 回合收到空句子边界而非全文 MessageObj——delta 已流式场景下 v1 已收全文，正确闭合；纯无流式场景理论不可达（public_text 由已流式 token 重建）。
- **跨连接 emotion 共享（最终 review 记录，Minor）：** `_stt_stt_lock` 是每连接锁，而 `stt_emotion_state` 是 agent 级共享 dict——多连接同时 REALTIME 转写时锁只串行本连接内的「转写+捕获」，跨连接仍可互相覆盖 emotion（既有共享状态设计，低概率，非本批次引入）。若需根治，须把 emotion 状态改为 per-connection（非本批次范围）。
- **D1 归属未覆盖 v2 content-as-parts（最终 review 记录，Minor）：** v2 客户端发 `content` 为 list 形式（多模态 parts）时不经 `_route_v2_text`，无预分配 message_id → 断开不取消（benign 方向：消息完成而非取消）。v2 客户端按契约应走 ACK 路径。
- **pause 丢弃在途段（最终 review 记录，Minor）：** `_commit_ready_stt` 对 `_paused` 跳过的段静默丢弃（已在 STT 在途、pause 前派发的段）——与「pause = 清分段状态」语义一致（spec §5.2 NEW-C），属有意行为。
