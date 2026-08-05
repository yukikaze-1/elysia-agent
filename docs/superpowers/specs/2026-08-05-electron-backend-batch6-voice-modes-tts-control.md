# Electron 后端批次 6 — 三种语音模式 + TTS 播放控制

**Spec 版本：** v0.7（v0.6 基础上修复六轮 review NEW-XA + NEW-XB/XC/YD）  
**文档状态：** 待七轮 review  
**关联任务：** BE-060 普通录音输入 / BE-061 实时语音会话 / BE-062 输入法转写 / BE-063 TTS 播放控制增强  
**任务清单：** `docs/14-Cyrene-后端配合改造任务清单-v0.1.md` §3.3 + §5 任务表（BE-060~063）  
**协议参考：** `docs/electron-backend-protocol-v2.md`（本 spec 落地后补 §12）  
**基线 Commit：** `5b7a4ce`（批次 5 收口）

---

## 1. 目标与范围

把当前唯一的「WS BINARY PCM → 后端 VAD 自动分段 → batch STT → 自动提交 `UserInputData` 触发 Agent」链路，拆成三种互相隔离的语音模式，并给 TTS 播放加上明确的 playback 语义。

**不做（批次边界）：**
- 音频附件落盘 / 语音消息携带音频文件——留给批次 7 BE-070（通用附件与媒体链路）。批次 6 语音消息 = 纯文本消息。
- 真流式 STT 后端（sensevoice/custom 均为 batch 一次性转写）——partial 用「VAD 段级累积」伪实现（P6-3）。
- 前端 UI / Electron Main 进程代码——本 spec 只定后端协议与行为。

**验收目标（docs/14 场景 F）：** 普通录音等待用户确认；实时语音显示 partial/final 字幕并提交语音轮次；输入法转写只返回草稿文字。三者取消后均不会误发消息。三种模式的数据去向互不混淆。

## 2. 已确认决策（拍板结论）

| ID | 决策 | 结论 |
|---|---|---|
| P6-1 | 语音上行通道 | **统一 WS BINARY 实时 PCM 流**（16kHz mono Int16，与现状一致）。三模式共用现有采集管线，后端按「活动语音会话」分流。前端采集零改动，只改交互。 |
| P6-2 | 普通录音提交语义 | **预转写 + 确认复用**：停止后后端立即 STT 返回字幕 → 前端显示待发送 → 用户 `commit`（复用字幕建消息，**不重转写**）或 `discard`。字幕 ≠ 文字消息。音频不落库。 |
| P6-3 | 实时语音轮次与 partial | 显式 `voice_session.start/stop` 会话 + **VAD 段完成自动提交为语音轮次**；partial 字幕 = 已识别段的累积文本（**伪流式，不扩展 STT 后端**）。字幕不写文字草稿。 |
| P6-4 | 语音提交 v2 语义 | 普通录音 commit + 实时轮次提交**纳入 v2**：预分配 messageId + 幂等（commit 侧）+ 显式会话归属 + ACK（commit 侧）。 |
| P6-5 | 输入法转写 | 复用**当前活动后端 STT Profile**；转写 = 无状态 STT 调用（不写历史 / 不建 UserInputData / 不触发 Agent / 不自动发送）；partial（可选）+ final 只回文字；取消/失败清理临时缓冲。活动 STT 为 desktop/local runtime 时后端无 STT backend → `STT_UNAVAILABLE`（本机 Profile 归 Electron 管理，非后端职责）。 |
| P6-6 | TTS 播放控制 | `stream_id` 正式化为 **playback ID**（**不重命名字段**，全链路统一 `stream_id` wire 键）+ 补 `audio_start`；`audio_end` 明确为「该句字节已全部下发」≠「播完」；前端本地 `onended` 定播完，**不引入上行回执**；新增 WS `audio.cancel{stream_id}` + 前端打断本地停播兜底；格式契约明确 wav、前端从 WAV header 读采样率自算振幅（后端不下发振幅元数据）；修 `is_final` 契约漂移。 |
| P6-7 | feature 标志 | `ServerInfoFeatures`：`dictation=true` + 新增 `voice_recording`/`voice_realtime`（如实声明三模式能力）。 |

## 3. 语音模式总览

### 3.1 四种 BINARY 处理模式（per-connection）

| 模式 | 触发 | VAD | 提交 | 消息创建 | 字幕事件 |
|---|---|---|---|---|---|
| `LEGACY`（v1/v2 无活动会话） | 无 | 是（现状） | 自动 | 是 | `transcription`（现状，**保持不变**） |
| `RECORDING`（普通录音） | `voice.record.start` | 否（全段缓冲） | 手动 `voice.commit` | commit 后 | `voice.transcript`（待确认预览） |
| `REALTIME`（实时语音） | `voice_session.start` | 是 | 每 VAD 段自动 | 每轮是 | `voice_session.partial` / `voice_session.turn` |
| `DICTATION`（输入法） | `dictation.start` | 是 | 无 | **永不** | `dictation.partial` / `dictation.final` |

**规则：**
- BINARY 帧到达时，若有活动语音会话（RECORDING/REALTIME/DICTATION 之一）→ 喂给会话；否则 → legacy `AudioBuffer.feed()`（现状 VAD 自动提交，**v1/v2 均不变**，保护现有 web 前端）。
- 三种模式均为 **v2-only** 显式会话（仿 `activate_session`/`tool.status` 的 `_find_channel` protocol_version 门禁）；v1 连接收到这些消息**忽略**（debug 日志，统一「忽略」而非「拒绝」，与激活/重放门禁惯例一致）。
- 会话/录音 ID 由 **客户端生成**（recordingId `rec_<uuid>` / sessionId `vs_<uuid>`），服务端校验存在与唯一——**唯一性仅限本连接内**（per-connection 管理器，跨连接唯一校验需全局注册表，超规格，review NEW-ZZ）。`vs_` 前缀避免与多会话 ID `conv_` 前缀混淆。
- 字幕（partial/transcript）**服务端不写文字草稿、不创建消息**（docs/14 §3.3 三语义硬约束——「不写文字草稿」指服务端不动草稿；输入法 final 返回给前端由前端写草稿，见 §7）。

### 3.2 VoiceSessionManager（新增）

新增 `core/input/voice_sessions.py`：**每连接一个** `VoiceSessionManager`，持有：
- 当前模式 + 活动会话状态（recordingId / sessionId、模式、暂停标志、turn/segment 计数）。
- RECORDING 模式：裸 PCM 累积缓冲（`list[bytes]`），不 VAD；`stop()` → `pcm_to_wav()` → STT → 存待确认字幕。
- REALTIME / DICTATION 模式：首会话开始前实例化（非建连时）一个 **无唤醒词的 `AudioBuffer`**（复用现有 VAD 分段 + `on_transcription` 回调 → 管理器按模式路由：realtime=partial+turn，dictation=partial+accumulate），**不复制 VAD 逻辑**。**内部 buffer 随会话创建/销毁（每会话一个，review NEW-R）**——`on_interrupt` 是构造参数（`audio_buffer.py:98`），惰性创建 + 跨会话复用会让 DICTATION 继承 REALTIME 的接线，静默破坏 NEW-K 隔离；故每个 REALTIME/DICTATION 会话各建一个 buffer，DICTATION 会话创建时 `on_interrupt=None`。**`on_interrupt` 仅 REALTIME 接线**（接同一 `do_interrupt(skip_vad=True)`，DICTATION **不接**——否则 dictation 语音会触发 InterruptData 清空 pending 队列，违反 §7.2 零副作用硬约束，review NEW-K）。VAD 打断继承 legacy 的 `should_suppress_vad_interrupt` 抑制语义（THINKING/EXECUTING 窗口不触发），作对齐 legacy 的防御层；**会话内实际轮次打断由新轮次提交的 `cancel_current()` 承担**（§5.3），非 on_interrupt（review NEW-L）。
- legacy 路径的 `AudioBuffer` 仍由 ws_handler 持有，仅无活动会话时使用——同一连接同时只存在一个处理入口。

ws_handler 集成：
- JSON 消息路由：新增 `voice.record.*` / `voice_session.*` / `dictation.*` / `audio.cancel` 分支 → `manager` 方法。
- **`set_service('stt', False)` 成功后 → 通知 manager（结束活动 REALTIME/DICTATION 会话，review NEW-X/NEW-XA）**——暂停期无帧、无 JSON，需显式接线才不漏 stt_disabled 检测。**RECORDING 会话不因 set_service 结束**：录音继续，STT 关闭在 `voice.record.stop` 时以 §4.2 `STT_UNAVAILABLE` 呈现。
- BINARY 帧：`manager.has_active_session()` → `manager.feed_pcm()`，否则 `audio_buf.feed()`。
- **连接断开（handle_ws finally，review NEW-AA/NEW-XB/NEW-XC）**：结束活动语音会话（丢弃残段、取消在途 STT 段；取消在途轮次**复用**现有 finally 的 `InterruptData(source="disconnect")`，manager 不重复发、不发 `ended`——连接已关，发送无意义）。取消在途 STT 段的目的：realtime 防落半途消息（STT 完成后 on_transcription 会 post `UserInputData`）、dictation 防迟到 `dictation.partial` 发往已关连接。销毁会话 buffer。
- **回声抑制（deafen）覆盖 REALTIME/DICTATION（review N1 消歧）**：会话模式不依赖建连时的 `_monitor_state`（STT 热切换后才可用时无该任务）——`manager.feed_pcm()` 入口对 REALTIME/DICTATION 检查 `agent.status.is_speaking`，为真则丢帧（deafen 等价）；**RECORDING 不检查**（全量累积，助手播放期录音会录进回声，属下节「已知取舍」的有意选择）。

**已知取舍（记录在案，review NEW-B）：** 会话模式下 TTS 播放期间用户语音被**静默丢弃而非 barge-in 打断**（`feed_pcm` 的 `is_speaking` 丢帧即回声抑制，与 legacy 播放期 deafen 一致）；打断只发生在对话间隙（非播放）的在途生成。若需真 barge-in 需在丢帧前先送 VAD 判定，复杂度高，不在此批次。RECORDING 模式无 VAD 去回声——用户录音时若助手正在播放，麦克风会录进助手语音，属有意选择（前端可提示「播放中录音」）。

**断连在途段边界（记录在案，review S1）：** `feed_pcm` 内 `await buffer.feed()` 在段 STT 期间阻塞 ws 串行消息循环——`voice_session.stop`/`dictation.stop` 要等该段完成才被处理，故 **stop 天然等待在途段**（§5.2「等待完成并补发」语义满足，非丢失）。但**连接断开**时：循环在 feed_pcm await STT 中，aiohttp 不取消在途 handler，STT 完成后 `_on_segment` 先 post `UserInputData`（落一条半途用户消息），随后 loop 下一次 receive 才检测断连 → finally `InterruptData(source="disconnect")` 取消该 generation。此半途消息无法在 manager 层取消（feed_pcm 阻塞循环，close() 运行时该 STT 已完成），属架构固有限制——Demo 可接受（断连发生在 ~1-30s STT 窗口的概率低），记录在案不修。

### 3.3 RECORDING 状态机（显式）

```
IDLE ──voice.record.start──▶ RECORDING
RECORDING ──voice.record.stop──▶ TRANSCRIBING   （STT 在途）
TRANSCRIBING ──STT 完成──▶ PENDING             （voice.transcript 已发，存字幕+emotion 快照）
TRANSCRIBING ──voice.discard──▶ IDLE           （取消在途 STT + 清缓冲）
PENDING ──voice.commit──▶ IDLE                  （v2 提交）
PENDING ──voice.discard──▶ IDLE
任一转场失败（校验不过）→ 发失败响应，状态不变（commit 走 ack{rejected}，其余走 ErrorMessage{code}，见下，review NEW-S）
```

边界规则（review S7）：
- RECORDING 态收 `commit`/`discard` → `CONTRACT_VALIDATION`（未 stop 不得提交）。
- TRANSCRIBING 态收 `stop`/`commit` → `CONTRACT_VALIDATION`（STT 在途）；`discard` **允许**（取消在途 STT + 清态）。
- PENDING 态收 `stop` → `CONTRACT_VALIDATION`（已转写完，须 commit/discard）。
- IDLE 态收 `stop`/`commit`/`discard` → `VOICE_RECORDING_NOT_FOUND`。
- **commit 处理顺序（review NEW-A/NEW-M/NEW-T，优先于状态机门禁）**：① 缺 `clientRequestId` → 直接 `user_message.ack{rejected, CONTRACT_VALIDATION}`（置于幂等查重与状态机门禁**之前**，畸形请求不依赖当前状态归因）；② 按 `clientRequestId` 查幂等表，三态分支——命中且同摘要 → **重发原 ACK，无视当前状态**（网络重试时状态可能已转 IDLE，幂等重放不应被门禁吞掉）；命中但异摘要 → `user_message.ack{rejected, CONTRACT_VALIDATION}`，不查状态；未命中（新请求）→ ③ 走状态机 PENDING 门禁。与 §6.1 step 2/3 一致。
- **失败信封区分（review NEW-Q/NEW-Z）**：`voice.commit` 违规 → **`user_message.ack{rejected}`**（v2 rejected 语义，可带 code，如 IDLE-commit → `ack{rejected, code: VOICE_RECORDING_NOT_FOUND}`）；`voice.record.start/stop/discard` 违规 → **`ErrorMessage{code}`**。

## 4. BE-060 普通录音输入协议

**语义：** 录音停止后不触发 Agent；客户端 `commit` 后才进入语音消息 + Agent 流程。可返回字幕，字幕不等同于文字消息。

### 4.1 消息流

```
client → {"type":"voice.record.start","recordingId":"rec_…"}
          （校验：无活动会话 → 否则 VOICE_SESSION_CONFLICT；agent.stt is not None
           且 stt_enabled → 否则 STT_UNAVAILABLE，与 legacy 门控一致）
client → BINARY PCM × N（RECORDING 模式全段累积，不 VAD、不触发）
client → {"type":"voice.record.stop","recordingId":"rec_…"}
server → {"type":"voice.transcript","recordingId":"rec_…","text":"…","emotion":"…"?"}
          （预转写字幕，进入 PENDING；STT 失败 → STT_UNAVAILABLE error + 清缓冲回 IDLE）
client → {"type":"voice.commit","recordingId":"rec_…","text":"…","clientRequestId":"req_…",
          "clientMessageId":"msg_…","conversationId":null}
server → {"type":"user_message.ack",…}（v2：幂等 + 预分配 messageId + 会话归属）
server → generation.*（Agent 流程，复用字幕文本，不重转写）
client → {"type":"voice.discard","recordingId":"rec_…"}  （丢弃，清缓冲）
```

**`voice.commit` 携带 `text`（plan 期补充，已确认）：** 幂等摘要的权威依据。首次 commit 后状态转 IDLE、pending 字幕已清，重试时服务端无法重算摘要——commit 带 text（用户确认的字幕文本）才能做「同 ID 异摘要 → CONTRACT_VALIDATION」拒绝（§3.3 NEW-M 三态分支依赖）。服务端新请求路径校验 `text == pending 字幕`（不一致 → 拒绝）；消息正文用服务端 pending 文本（不重转写）。

### 4.2 契约（Client→Server）

| 消息 | 字段 | 说明 |
|---|---|---|
| `voice.record.start` | `recordingId: str` | 客户端生成；已有活动录音/会话 → `VOICE_SESSION_CONFLICT` |
| `voice.record.stop` | `recordingId: str` | 触发预转写；无此录音 → `VOICE_RECORDING_NOT_FOUND` |
| `voice.commit` | `recordingId, text, clientRequestId, clientMessageId?, conversationId?` | v2 提交（§6）；`text` = 用户确认的字幕文本（幂等摘要依据，服务端校验与 PENDING 一致）；复用 PENDING 字幕文本不重转写 |
| `voice.discard` | `recordingId` | 丢弃 pending/在途录音 |

Server→Client：`voice.transcript {recordingId, text, emotion?}`。

**约束：**
- **空转写（review NEW-J）**：STT 返回空/纯空白 → 不进 PENDING，清缓冲回 IDLE，**不发 `voice.transcript`**。**前端判据显式化**：`voice.record.stop` 后收到 `voice.transcript` → 进入待确认；收到 error 信封 → 失败；两者均未收到（即空结果）→ 视为未识别到语音、结束该 recording（不挂起，无需 commit）。杜绝「ACK accepted 但无消息无生成」。
- **emotion/events 快照**：PENDING 存 **stop 时刻**的 emotion/events 快照（`agent.stt_transcribe` 写共享 `stt_emotion_state`，后续任意 STT 调用会覆盖——pending 期间不受污染）。
- **STT 中途关闭**：stop 时 `agent.stt_enabled=False` → `STT_UNAVAILABLE` + 清缓冲回 IDLE。
- 录音缓冲上限 `MAX_RECORDING_SECONDS = 600`（16kHz mono Int16 = 32000 B/s × 600 ≈ 18.3 MiB），超限 → `VOICE_RECORDING_TOO_LONG` + 清缓冲。
- 字幕文本即最终消息文本（不重转写，P6-2）；emotion/events 随 `UserInputData` 传给 mood 分析。
- 音频不落盘、不入消息 content（批次 7 才做音频附件）。

## 5. BE-061 实时语音会话协议

**语义：** 用户显式开启持续会话；partial/final 字幕 + VAD/断句 + 语音轮次；字幕只用于会话显示，不修改文字草稿。

### 5.1 消息流

```
client → {"type":"voice_session.start","sessionId":"vs_<uuid>","conversationId":null}
server → {"type":"voice_session.started","sessionId":"…","conversationId":…}
          （校验：无活动会话 → 否则 VOICE_SESSION_CONFLICT；STT 可用 → 否则 STT_UNAVAILABLE）
client → BINARY PCM × N（VAD 分段）
server → {"type":"voice_session.ack","sessionId":"…","seq":N}   （周期性，背压进度通知）
每段完成 → server → {"type":"voice_session.partial","sessionId":"…","turnId":1,"segmentIndex":0,"text":"…"}
          + 自动提交轮次 → server → {"type":"voice_session.turn","sessionId":"…","turnId":1,
              "messageId":"msg_…","conversationId":…,"text":"…"}（v2：预分配 messageId + 会话归属 + 触发 generation）
client → {"type":"voice_session.pause"/"resume","sessionId":"…"}
client → {"type":"voice_session.stop","sessionId":"…"} → server → {"type":"voice_session.ended","sessionId":"…","reason":"stopped"}
client → {"type":"voice_session.cancel","sessionId":"…"} → server → {"type":"voice_session.ended","sessionId":"…","reason":"cancelled"}
```

### 5.2 契约（Client→Server）

| 消息 | 字段 | 说明 |
|---|---|---|
| `voice_session.start` | `sessionId: str`, `conversationId?` | 客户端生成；重复/有活动会话 → `VOICE_SESSION_CONFLICT` |
| `voice_session.pause` / `resume` | `sessionId` | 暂停瞬间 `buffer.reset()`（清分段状态，防陈旧段 + 超长静音触发误分段）；暂停期间 BINARY 帧丢弃；pause 时在途 STT 段**等待完成并补发 partial+turn**（不丢该轮，与 stop 一致） |
| `voice_session.stop` | `sessionId` | 处理残段后关闭；若当前段 STT 在途 → **等待完成并补发 partial+turn 再 `ended`**（不丢该轮；STT 30s 超时则丢弃残段） |
| `voice_session.cancel` | `sessionId` | 丢弃残段 + 取消在途轮次生成 → `ended{cancelled}` |

**「在途轮次」定义（review S9/NEW-E）：** 轮次一旦 post `UserInputData` 即已提交（已开始生成）。`voice_session.cancel` 的「取消在途」= 该轮 **generation 尚未发 `generation.completed`/`generation.failed`**（`_generation_completed` 未置）时发 `InterruptData`（仿 `do_interrupt`/断线取消，`core/input/ws_handler.py:297-304`）；已完结轮次（终态已发）不回滚、不补 `generation.failed`。**TTS 播放期不在「在途」判据内**——cancel 可能连带打断刚完结轮的播放（`agent.status` 非 idle），此为本批次的**有意取舍**（语音语境下「用户再说话打断播放」符合直觉，review NEW-E 记录在案）。

### 5.3 轮次与 partial（P6-3 落地）

- **轮次 = 一个 VAD 段**。段完成 → STT → `voice_session.partial`（字幕）→ 立即自动提交：预分配 `messageId`、post `UserInputData(text, user_name, emotion, events, request_id, message_id, protocol_version=2, conversation_id)` → `voice_session.turn` 确认 → cognitive 生成。
- **partial/final 语义（伪流式，review S6）**：STT 后端为 batch，段内无法产出中间词。**每段一条 partial 即该轮的 final**——partial 是字幕显示；turn 边界的确定 = 该段进入提交。`voice_session.turn` **携带 `text`**（该轮最终文本），前端不依赖「partial 后紧跟 turn」的隐式顺序即可渲染。`ended` 不带累计文本。
- `turnId` 每轮单调递增；`segmentIndex` 会话内全局单调（跨轮累计）。
- 轮次提交**不经 idempotency store**（服务端 VAD 事件，非客户端重试语义）——`voice_session.turn` 即确认；messageId 服务端生成。commit 侧（§6）才用 clientRequestId 幂等。
- **轮次打断语义（review S2/S4，有意为之）**：语音提交走直接 post `UserInputData`（与 legacy `do_transcription` 一致，携带 emotion/events），**不做 `process_message` 忙队列**——新轮次 `_handle_user_input` 会 `cancel_current()` 取消在途生成（语音打断语义，与 VAD 打断一致）。提交前调 `agent.idle.touch()`（防「用户沉默」误判主动搭话，同 legacy）。文档记录：实时语音多轮快速连发时只响应最后一轮为预期行为。
- **背压（轻量，review N3/NEW-F）**：服务端每 ~1s 发 `voice_session.ack {sessionId, seq}`，seq = **会话内**已接收 BINARY 帧序数（服务端计数）；前端维护已发送序数，未确认窗口 = `sent - acked`，超 `MAX_UNACKED = 200`（≈50s）时**前端自行节流暂停发送**；节流持续 `THROTTLE_RESUME_SECONDS = 2s` 后**重置窗口恢复发送**（服务端空闲期仍周期性补 ack，窗口可自愈，避免病理场景窗口永闭）。服务端不主动丢弃（可靠 WS + 前端窗口下不会发生），仅超时/超窗打 warning 日志（安全阀）。帧无显式 seq（BINARY 裸 PCM，无 header），seq 由服务端序数定义。
- **会话超时（review NEW-P/NEW-V/NEW-W）**：`VOICE_SESSION_IDLE_TIMEOUT = 300s` 无音频 → 自动 `ended{timeout}`。**暂停期间挂起 idle 计时**（暂停不触发 idle 超时，避免长暂停被误 ended）；`pause` 已暂停时再 pause、`resume` 未暂停时再 resume → **幂等忽略**（不报错）。**暂停中会话遇 stt 关闭 → 同样 `ended{stt_disabled}`**（由会话 JSON 消息入口或 set_service 通知触发——暂停期无帧流入、idle 计时挂起，需显式检测点避免静默悬挂至连接关闭）。
- **STT 中途关闭（review S3/NEW-D）**：`agent.stt_enabled=False` → 活动 realtime 会话自动结束 → `voice_session.ended{reason:"stt_disabled"}`，清缓冲（与「关闭即停」一致）。**检测点**：每个 BINARY `feed_pcm()` 帧入口 + 每条会话 JSON 消息入口检查 `agent.stt_enabled`（不依赖空闲超时——否则静默会话会延迟到 300s 才结束）。
- **deafen**：`feed_pcm()` 入口检查 `agent.status.is_speaking` → 丢帧（§3.2）。

## 6. 语音提交的 v2 语义（P6-4）

### 6.1 普通录音 commit（客户端发起）

**处理顺序（review NEW-A）：** `voice.commit` 分支先做幂等查重（按 clientRequestId），**命中则重发原 ACK、无视 RECORDING 状态机**（首次 commit 成功但 ACK 丢失后重试时状态已 IDLE，门禁不应吞掉幂等重放）；未命中才走状态机 PENDING 门禁 → `_submit_v2_voice` 新请求路径。

复用 `_route_v2_text` 的幂等 + ACK 模式，新增语音提交助手（ws_handler）：

```
_submit_v2_voice(agent, ws, text, client_request_id, client_message_id, conversation_id, emotion, events):
  1. channel.protocol_version < 2 → 忽略（v2-only，与 §3.1 门禁一致）
  2. 缺 client_request_id → user_message.ack{rejected, CONTRACT_VALIDATION}
  3. idempotency store 查重：
     - 同 ID + 同内容摘要 → 重发原 ACK（同 messageId），不重复生成
     - 同 ID + 异摘要 → user_message.ack{rejected, CONTRACT_VALIDATION}
  4. 新请求：预分配 messageId → store → ACK{accepted}
  5. agent.idle.touch()（防「用户沉默」误判）
  6. post UserInputData(text, user_name, emotion, events, request_id,
       message_id, protocol_version=2, conversation_id)
     → _handle_user_input → cognitive.process(message_id=…) → 消息落库用预分配 ID（ACK 一致）
```

**关键：** 与 `do_transcription` 一致直接 post `UserInputData`（携带 emotion/events），但补 v2 字段（message_id / protocol_version=2 / conversation_id）。`UserInputData` 已含这些字段（`core/schema/event.py:24-36`）。内容摘要 = 字幕文本的 SHA-256（与 `_route_v2_text` 一致）。

**提交语义声明（review S2）：** 语音提交**不做 `process_message` 忙队列**——忙时 `_handle_user_input` 取消在途生成（语音打断语义，与 legacy 语音一致）；此为有意选择，非文本排队语义。

### 6.2 实时轮次（服务端发起）

无 clientRequestId；服务端生成 messageId → `idle.touch()` → post `UserInputData(...)` → `voice_session.turn` 确认。不入 idempotency store（VAD 事件不可重放）。

### 6.3 会话归属

commit 的 `conversationId` 显式 → 目标会话；缺省 → 活动会话（复用 `agent.resolve_session`）。`user_message.ack.conversationId` 填**实际目标会话**（非仅回显），与文本 v2 一致（协议 v2 §2.3）。realtime 的 `conversationId` 在 `voice_session.start` 指定、每轮沿用；**`voice_session.started.conversationId` = 解析后的实际目标会话**（缺省填活动会话，非回显原始可能省略值，review NEW-G）。

## 7. BE-062 输入法转写协议

**语义：** 只做 STT 的通道；按下/松开/取消或一次性音频；partial（可选）+ final；final 只返回文字；不写服务器聊天历史、不创建 UserInputData、不触发 Agent、不自动发送；取消或失败后清理临时音频（内存缓冲）。

### 7.1 消息流

```
client → {"type":"dictation.start","clientRequestId":"req_…"}
          （校验：无活动会话 → 否则 VOICE_SESSION_CONFLICT；`agent.stt is not None` 且
           `stt_enabled` → 否则 STT_UNAVAILABLE，与 §4.1 record.start 双条件一致，review NEW-I）
client → BINARY PCM × N（VAD 分段）
每段完成 → server → {"type":"dictation.partial","text":"…"}     （累积字幕，仅显示）
client → {"type":"dictation.stop","clientRequestId":"req_…"}
server → {"type":"dictation.final","clientRequestId":"req_…","text":"…"}   （只回文字）
client → {"type":"dictation.cancel","clientRequestId":"req_…"}
server → {"type":"dictation.cancelled","clientRequestId":"req_…"}
```

- 无活动 dictation 时收到 `dictation.stop`/`dictation.cancel` → `DICTATION_NOT_FOUND`。
- 空结果（静音）→ `dictation.final{text:""}` 仍返回（只回空文字，前端可忽略）。
- **partial 拼接（review N11）**：多段 partial 累积为 final 时**段间直接拼接**（SenseVoice 中文输出自带标点，无需额外分隔符）。

### 7.2 隔离硬约束（docs/14 §10 BE-062）

- final 文本**只经 `dictation.final` 返回**：不写 `session.add_message`、不 post `UserInputData`、不触发 cognitive、不自动发送。
- 临时音频 = 内存缓冲（不落盘）；cancel / stop / STT 失败 / 会话断开 / **STT 中途关闭（检测点同 §5.3：feed_pcm 帧入口 + 会话 JSON 消息入口检查 `agent.stt_enabled`，发 `dictation.cancelled`，review NEW-O）** → 清缓冲。
- **共享 `stt_emotion_state` 隔离（review S5）**：`agent.stt_transcribe` 每次写共享 `stt_emotion_state`。dictation 的 STT 调用不得污染该状态——实现时转写前快照、转写后恢复（或独立读取），否则 dictation 会污染后续 legacy/realtime 转录的情绪。
- 复用活动后端 STT（P6-5）：`agent.stt_transcribe`。若活动 STT 为 desktop/local runtime → 后端无 STT backend → `dictation.start` 拒绝 `STT_UNAVAILABLE`。
- `dictation.final` 不携带 emotion/events（转写只回文字；情绪分析不属于转写职责）。

## 8. BE-063 TTS 播放控制增强

### 8.1 现状缺口（探查确认）

- 无播放开始事件、无取消句柄；`stream_id` 全局计数器可作 playback ID 但无独立语义。
- 中断（`cancel_all`）不补 `audio_end` → 前端孤儿 chunk（永不 finalize）。
- 前端打断时只清排队气泡、不停当前音频。
- `is_final` 键已随 audio 帧下发但契约 `AudioMessage` 未建模（`websocket.py:141-152` 手动 dict）。

### 8.2 设计

| 项 | 设计 |
|---|---|
| playback ID | 现有 `stream_id`（= `TTSStreamManager._total`）**正式化为 playback ID**。**不重命名字段**：`audio`/`audio_end`/`audio_start`/`audio_cancelled`/`audio.cancel` 全链路统一用 `stream_id` wire 键（避免多个字段名），语义 = playback ID。 |
| `audio_start` | 新增 Server→Client 事件：句开始播放（Phase 2 放行后、首 chunk 前）携带 `stream_id, format, sampleRate?, durationMs?`——sampleRate/durationMs 从已合成的 WAV bytes 解析（非流式合成完成后才有）。前端据此识别播放实例边界。 |
| `audio_end` | 语义明确为「该句音频字节已全部下发」**≠ 播完**。字段不变（`stream_id + format`）。 |
| 完成语义 | 播放完成由前端本地 `onended` 判定（P6-6：**不引入上行回执**，YAGNI）；整轮播完前端靠队列清空 + `state=idle`。 |
| `audio.cancel` | 新增 Client→Server `{stream_id}`：取消**在途播放实例**（`stream_id` = playback ID）。仍在转发 → 停发 + `audio_cancelled`；已 `audio_end`/未知 stream_id → `PLAYBACK_NOT_FOUND` error 信封。**全局播放操作，非 per-connection**（TTS 是全局广播，任一连接可取消在途播放，作用对象=播放实例）；v2-only。前端打断时**既发 interrupt 也发 audio.cancel** + 本地停当前音频（兜底）。 |
| `audio_cancelled` | 新增 Server→Client `{stream_id}`：句被取消（audio.cancel / TTS cancel_all 中断）。前端据此清理孤儿 chunk。**修「中断不补 audio_end」缺口**——取消路径统一发 `audio_cancelled`（不再静默丢）；`websocket.on_audio_cancelled` 同步 `_audio_seq.pop(stream_id)`（防取消 stream 项永久残留，review N6）。 |
| 格式/口型 | 契约 `format` 明确 `wav`（三后端均为 WAV）；前端从 WAV header 读采样率（32000/24000 不一）+ 解析 PCM 算 RMS 振幅估口型。**后端不下发振幅元数据**（YAGNI，docs/14「口型可由前端振幅估算」即此意）。 |
| `is_final` 漂移 | `AudioMessage` 增 `is_final: bool = False`；`websocket.on_audio_chunk` 改用 `AudioMessage` 模型（替代手动 dict，`exclude_none` 下 `False` 仍输出，行为不变）。 |

**打断双发竞态（review S10）：** interrupt 先到取消在途实例 → 随后 audio.cancel 命中已取消实例 → `PLAYBACK_NOT_FOUND`。该错误经 `ErrorMessage{code}` 送达，**前端视为良性可忽略**（本地已兜底停播）。

### 8.3 输出层接线

- `Module/output/manager.py`：新增 `audio_start(stream_id, format, sample_rate?, duration_ms?, ...)` 与 `audio_cancelled(stream_id, ...)` 广播（capability=AUDIO）。
- `Module/output/websocket.py`：新增 `on_audio_start` / `on_audio_cancelled`；`on_audio_chunk` 改 `AudioMessage`。
- `core/adapter/tts_adapter.py`：`_download_and_forward` Phase 2 放行后、分块前发 `audio_start`（解析 WAV sampleRate/durationMs）；新增 `cancel_stream(stream_id)`：命中在途句 → 停发 + `audio_cancelled`；`cancel_all` 对每个在途句发 `audio_cancelled`。跟踪「句已转发完成」状态区分 cancel 目标。

## 9. 契约新增（frontend_contract/ws_message.py）

### 9.1 Client→Server（不入 ServerMessage 联合，仿 TextMessage 惯例）

`VoiceRecordStartMessage{recordingId}`、`VoiceRecordStopMessage{recordingId}`、`VoiceCommitMessage{recordingId, text, clientRequestId, clientMessageId?, conversationId?}`、`VoiceDiscardMessage{recordingId}`、`VoiceSessionStartMessage{sessionId, conversationId?}`、`VoiceSessionPauseMessage{sessionId}`、`VoiceSessionResumeMessage{sessionId}`、`VoiceSessionStopMessage{sessionId}`、`VoiceSessionCancelMessage{sessionId}`、`DictationStartMessage{clientRequestId}`、`DictationStopMessage{clientRequestId}`、`DictationCancelMessage{clientRequestId}`、`AudioCancelMessage{stream_id: int}`。

### 9.2 Server→Client（**入 ServerMessage 判别联合**，否则 gen_ts 不生成 TS 判别）

`VoiceTranscriptMessage{recordingId, text, emotion?}`、`VoiceSessionStartedMessage{sessionId, conversationId?}`、`VoiceSessionPartialMessage{sessionId, turnId, segmentIndex, text}`、`VoiceSessionTurnMessage{sessionId, turnId, messageId, conversationId?, text}`、`VoiceSessionAckMessage{sessionId, seq}`、`VoiceSessionEndedMessage{sessionId, reason: Literal["stopped","cancelled","timeout","stt_disabled"]}`、`DictationPartialMessage{text}`、`DictationFinalMessage{clientRequestId, text}`、`DictationCancelledMessage{clientRequestId}`、`AudioStartMessage{stream_id, format, sampleRate?, durationMs?}`、`AudioCancelledMessage{stream_id}`；`AudioMessage` 增 `is_final`。

### 9.3 features

`ServerInfoFeatures` 增 `voice_recording: bool = False`、`voice_realtime: bool = False`；批次 6 落地后 `dictation=True`、`voice_recording=True`、`voice_realtime=True`（`core/input/capabilities.py` 如实声明，严禁超前）。

## 10. 错误码目录扩充（protocol-v2 §9.1）

| code | HTTP | retryable | 场景 |
|---|---|---|---|
| `VOICE_SESSION_CONFLICT` | 409 | false | 已有活动录音/会话时再 start（recording/realtime/dictation 通用） |
| `VOICE_SESSION_NOT_FOUND` | 404 | false | pause/resume/stop/cancel 未知 sessionId |
| `VOICE_RECORDING_NOT_FOUND` | 404 | false | IDLE 态收 stop/commit/discard（未知 recordingId） |
| `VOICE_RECORDING_TOO_LONG` | 413 | false | 录音缓冲超 `MAX_RECORDING_SECONDS` |
| `DICTATION_NOT_FOUND` | 404 | false | 无活动 dictation 时收 `dictation.stop`/`dictation.cancel` |
| `STT_UNAVAILABLE` | 503 | true | STT 未启用/后端不可用/活动 STT 为 desktop-local runtime（start 拒绝 / 转写失败 / 录音 stop 时 STT 已关） |
| `PLAYBACK_NOT_FOUND` | 404 | false | `audio.cancel` 未知**或已结束**的 stream_id（前端可忽略良性） |

## 11. 兼容性

| 面 | 处理 |
|---|---|
| v1 连接 | BINARY → legacy VAD 自动提交（**逐字节不变**）；新增语音消息 v2-only，v1 忽略（debug 日志，仿 activate_session/tool.status 门禁）。 |
| v2 连接无活动会话 | BINARY → legacy VAD 自动提交（**现状不变**，保护现有 web 前端）；三模式显式 opt-in。 |
| `audio_start`/`audio_cancelled` | 新事件；v1/现有前端忽略未知 type（无破坏）。 |
| `AudioMessage.is_final` | 纯增量字段（wire 已发，仅补契约建模，`exclude_none` 下行为不变）。 |
| `stream_id` | 保持 wire 键名（不重命名），语义正式化为 playback ID。 |
| `UserInputData` 字段 | 已含 message_id/protocol_version/conversation_id/emotion/events（批次 2/3），零新增。 |

## 12. 测试计划

| 层 | 覆盖 |
|---|---|
| unit | `VoiceSessionManager` 状态机：RECORDING 四态转场（含 TRANSCRIBING 中间态边界、double-stop、commit-before-stop）、pause 重置分段 + 在途段等待、**暂停挂起 idle 计时 + 重复 pause/resume 幂等忽略**、模式冲突、STT-disabled 拒绝（start 双条件）、recording 超长、dictation 隔离（无消息/无 UserInputData/无 Agent）、**dictation 会话 buffer `on_interrupt=None`（每会话重建，NEW-K/R 隔离）**、`stt_emotion_state` 快照/恢复、**幂等优先于状态机（commit 重试在 IDLE 态命中幂等表 → 重发原 ACK；缺 clientRequestId → 直接 CONTRACT_VALIDATION，NEW-T）**。 |
| unit | `tts_adapter`：audio_start 触发（sampleRate/durationMs 解析）、`cancel_stream` → audio_cancelled、`cancel_all` 补 audio_cancelled（修孤儿 chunk）、已完结句 cancel → PLAYBACK_NOT_FOUND。 |
| service | ws_handler 路由：record→transcript→commit→ack+generation；dictation→final 只回文字；realtime 会话→partial+turn；audio.cancel；v1 忽略新消息；空转写不发 transcript；**STT 中途关闭 → realtime `ended{stt_disabled}` / dictation `cancelled` / 暂停中会话同样 `ended{stt_disabled}`**；**失败信封区分（commit 违规 → ack{rejected} / start/stop/discard 违规 → ErrorMessage{code}，NEW-Q）**。 |
| service | 幂等：同 clientRequestId 重试 commit → 单消息单生成；异摘要 → CONTRACT_VALIDATION。 |
| contract | 新消息序列化 + ServerMessage 联合成员 + `make check-types` 漂移门禁。 |
| 回归 | legacy 无会话 BINARY 自动提交不变（现有 test_server/test_v2_ws）；**断连清理：活动会话结束 + 在途 STT/轮次取消，不落半途消息（NEW-AA）**。 |

## 13. 文档更新

- `docs/electron-backend-protocol-v2.md`：新增 §12 三种语音与 TTS 播放契约；§9.1 错误码目录 +7；§1 features 行（voice_recording/voice_realtime/dictation）。
- `docs/electron-backend-progress.md`：批次 6 节 + 验收结论。
- `docs/14-Cyrene-后端配合改造任务清单-v0.1.md`：BE-060~063 标记 ✅（验收后），完成度更新。
- `docs/electron-backend-security.md`（如涉及）：dictation/录音临时音频不落盘说明。

## 14. 验收场景（docs/14 场景 F 后端侧）

1. 普通录音：start → 录音 → stop → 收 `voice.transcript`（未建消息）→ commit → `user_message.ack` + generation；discard → 无消息。
2. 实时语音：start → 说话 → 收 `partial` + `turn`（自动提交 + generation）→ stop → `ended`；cancel → 当前轮取消、已提交轮次保留。
3. 输入法转写：start → 说话 → 收 `partial` → stop → `final{text}`；历史/消息/Agent 零副作用；cancel → `cancelled`。
4. 三者各自 cancel/失败后均不产生用户消息。
5. TTS：回复多句 → 每句 `audio_start` → chunk → `audio_end`；`audio.cancel{stream_id}` → `audio_cancelled`；打断 → 残余 chunk 可清理。
