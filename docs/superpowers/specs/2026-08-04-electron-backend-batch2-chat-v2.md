# Electron 后端改造 — 批次 2：结构化聊天 v2 事件模型（Spec）

**Spec 版本：** v0.2（修订：吸收批次 2 三角度 review + 批次 0/1 review 交叉结论）
**文档日期：** 2026-08-04
**依据：** `docs/14-Cyrene-后端配合改造任务清单-v0.1.md`（BE-021/022/023/024）与 `docs/15-...`（批次 2）
**前置：** 批次 0 基线 + 批次 1（认证/版本协商/错误信封）

---

## 1. 目的

在保留 v1 协议字节不变的前提下，为 v2 客户端建立结构化聊天语义：**消息 ACK 与幂等、父回复/分段/增量稳定 ID、明确完成与失败、取消、最小重连语义**。前端不再需要从 `message content:""` 和 `state:"idle"` 推断结构。

**兼容硬约束**：v1 客户端（未带 `X-Protocol-Version: 2` 握手头）收到的 wire 与现状**逐字节一致**；现有 1883 测试全绿。

## 2. 已定决策

| 决策 | 结论 |
|---|---|
| D3 | v2 由握手 `X-Protocol-Version: 2` 判定（批次 1 已实现） |
| D4 | v2 事件用新 type 字符串、新契约模型；**不修改任何 v1 契约模型**；v2 模型加入同一 `ServerMessage` 判别联合（gen_ts 全量重生成，v1 类型字节不变；旧前端运行时对未知 type 走 `default` 分支优雅忽略，已核 `useWebSocket.ts`） |
| D5 | 时间戳 UTC ISO 8601，**统一用 `now_iso()`**（`+00:00` 偏移，与现状一致；不用 `Z` 后缀——示例统一为 `now_iso()` 输出格式） |
| D6 | ID 收口：`messageId`（落库消息，稳定）、`generationId`（一次生成实例）、`segmentId`（一个分段）；**`responseId` 弃用（别名 generationId，wire 不出现）**；ID 生成器独立前缀：`msg_`（消息）、`gen_`（生成）、`seg_`（分段），wire 上可区分；`clientRequestId` 幂等用进程内 store + TTL |
| D6a | **`messageId` 双重身份钉死**：`user_message.ack.messageId` = 用户消息 ID（入口预分配，落库一致）；`generation.*.messageId` = **assistant 父消息 ID**（generation 开始时预分配，贯通到 `_finalize_turn` 落库）——客户端按 messageId 对齐回放 |

## 3. 事件语义（Server→Client，v2）

### 3.1 事件序列（一次完整 v2 回复）

```
user_message.ack              ← 接受消息（含 messageId + 原 clientRequestId）
generation.started            ← 生成开始（generationId + assistant messageId）
generation.segment.started    ← 每段开始（segmentId + segmentIndex + channel + kind）
generation.delta ×N           ← 段内增量（deltaSequence 递增）
generation.segment.completed  ← 每段结束（含完整段文本）
  ── (inner_voice 与 public_reply 段按全局 segmentIndex 交错；工具轮分段计入同一 generation) ──
generation.completed          ← 父完成（finishReason + usage + model + mood）
  或
generation.failed             ← 父失败（code + message + retryable）
```

**约束：**
- 一次 AI 回复的所有分段（含工具轮 `_stream_llm_response` 各轮）共享同一 `generationId` 与 `messageId`；
- 每个分段有独立 `segmentId` + 全局递增 `segmentIndex`（跨 inner_voice/public_reply/工具轮单调递增）；
- `generation.segment.completed` 不结束父回复；
- 一条父消息只在 `generation.completed` 一次性出现（连同 usage/model/mood）；
- P0 分段 `kind` 只实现 `text`/`markdown`；`image/video/audio/file/sticker/link` 是预留枚举值（批次 7 用），不实现渲染；
- **终态唯一**：一次生成只有 `generation.completed` 或 `generation.failed` 之一，二者不可双发（见 §3.3/B9）。

### 3.2 字段定义

**`user_message.ack`**（type=`user_message.ack`）：

```json
{
  "type": "user_message.ack",
  "messageId": "msg_...",
  "clientRequestId": "req_...",
  "clientMessageId": "msg_client...",
  "conversationId": null,
  "receivedAt": "2026-08-04T00:00:00+00:00",
  "status": "accepted",
  "error": null
}
```

- `status`：`accepted`（进入生成）| `rejected`（幂等冲突/契约错误）；
- `status=rejected` 时 `error` 携带 `{code, message, retryable}`，**不再跟随 `generation.failed`**（rejected 从未开始生成，没有 generationId；B3 修正）；
- `conversationId`：批次 2 恒为 `null`（单会话）；批次 3 多会话后填值。

**`generation.started`**：`generationId`, `messageId`（assistant 父消息 ID）, `conversationId|null`, `requestId|null`（回显现有 `UserInputData.request_id`，日志关联用；无则 null）, `model`.

**`generation.segment.started`**：`generationId`, `segmentId`, `segmentIndex`, `channel`(inner_voice|public_reply|status), `kind`(text|markdown|image|video|audio|file|sticker|link).

**`generation.delta`**：`generationId`, `segmentId`, `segmentIndex`, `deltaSequence`(段内从 0 递增), `delta`.

**`generation.segment.completed`**：`generationId`, `segmentId`, `segmentIndex`, `channel`, `kind`, `content`(**完整段文本**——public_reply 与 inner_voice 段都携带；客户端无需拼接 delta 即可渲染整段).

**`generation.completed`**：`generationId`, `messageId`, `channel`(恒 `public_reply`，父回复通道), `finishReason`(stop|tool_calls), `usage`{turn{rounds,turn_total}, session{total,by_model}}, `model`, `mood|null`, `completeness`(complete).

- `finishReason` **只含 `stop`/`tool_calls`**：取消与错误一律由 `generation.failed` 承担，`completed` 枚举不含 cancelled/error（B1 修正）；
- `usage`：`turn` 为空（无 LLM 调用产出 usage）时 `turn_total=0`、`rounds=[]`（注 2）。

**`generation.failed`**：`generationId`, `messageId`（assistant 父消息 ID，恒非空——仅在已接受后发送）, `code`, `message`, `retryable`, `details|null`（公开安全）。`code` 用批次 1 错误目录。

### 3.3 完成 vs 失败语义

- **正常完成**：`generation.completed` 是唯一终态。v2 前端不得再从 `state:"idle"` 推断"回复结束"（v2 里 idle 仅表示 FSM 空闲，仍照发兼容）。
- **LLM 层错误**（当前 `❌ 文本` 路径，loop.py `except Exception`）：改为 `generation.failed{code: GENERATION_PROVIDER_UNAVAILABLE|GENERATION_TIMEOUT, retryable}`。**不再向 v2 客户端发 `❌` 前缀 delta**；v1 客户端照旧（兼容）。
- **内部错误**：`generation.failed{code: INTERNAL}`。
- **终态唯一（B9）**：错误路径发 `generation.failed` 后，`_handle_interrupt` 不得重复发 `GENERATION_CANCELLED`——发射点归属：错误路径已发 failed 则中断路径跳过；只有未被错误路径覆盖的取消才发 `GENERATION_CANCELLED`。
- 晚到增量：取消后服务端停止流式；客户端对已取消 generation 的迟到帧按 `generationId` 拒绝。

## 4. 幂等与 ACK（BE-021）

### 4.1 client→server 文本消息（v2）

v2 客户端发送 `type:"text"` 并额外携带（wire 同 v1 的 `text`，追加字段；handler 按连接版本路由）：

```json
{
  "type": "text",
  "content": "...",
  "clientRequestId": "req_...",
  "clientMessageId": "msg_client...",
  "conversationId": null
}
```

- `clientRequestId` 必填（v2）；缺省 → `CONTRACT_VALIDATION` 拒绝（`user_message.ack{status: rejected}`）；
- `clientMessageId` 可选（客户端本地稳定 ID，回显给 ACK 用）；
- `conversationId` 批次 2 忽略/恒 null；
- **非文本消息**（`content` 数组，含图片等）批次 2 沿用 v1 多模态路径（A4 声明），不纳入 v2 分段语义；`V2TextMessage` 契约只建模纯文本。

### 4.2 幂等存储

- 进程内 `dict[clientRequestId → {user_message_id, content_digest, status, created_at}]` + TTL（默认 1 小时）；
- 相同 `clientRequestId` + 相同内容摘要 → 不重复处理，重发原 ACK（同 messageId），不再次生成；
- 相同 `clientRequestId` + 不同内容摘要 → `user_message.ack{status: rejected, error: {code: CONTRACT_VALIDATION, ...}}`，不生成；
- **幂等表仅在 v2 路径维护**；v1 客户端无 `clientRequestId`，不受影响。

### 4.3 消息 ID 贯通

- handler 在接受 v2 消息时用 `new_message_id()` 预分配 **用户消息 ID**，随 `UserInputData` 传入循环；`_build_turn_prompt` 落库用户消息时**优先用预分配 ID**（无则维持现状）；
- **忙时排队路径**（`_PendingMessage`，`core/agent/_types.py`）必须携带预分配用户消息 ID，出队重建 `UserInputData` 时保留——否则忙时 ACK 的 messageId 与落库不一致（注 3）；
- **assistant 父消息 ID** 在 `generation.started` 时预分配，贯通到 `_finalize_turn` 落库（替换其 `new_message_id()`，注 5）；
- `UserInputData` 扩展字段（预分配 message_id / 版本标记）加在 **`core/schema/event.py`**（模型定义层，M1），消费者 `core/agent` 只读。

## 5. 架构：版本感知的发射扇出

### 5.1 原则

**单一语义源，双 wire 呈现。** CognitiveLoop 发射"统一回合事件"（含 ID）；`OutputManager` 按 channel 的 `protocol_version` 扇出。

**统一回合事件"取代"现有直接调用（H1 明确）**：loop 不再直接调 `on_text_delta` / `send("")` / `command("inner_voice")` / `expression`，改调统一发射 API；v1 呈现由翻译层**复刻现状 wire**。这是行为等价的强制保证，不是可选的叠加。

**v2 channel 对既有 v1 帧的收/抑制规则（H2 明确）**：

| v1 帧 | v2 channel |
|---|---|
| `state` / `typing` | **照发**（FSM 状态、打字指示对 v2 UI 有用，独立于 generation） |
| `audio` / `audio_end` | **照发**（TTS 播放需要；v2 TTS 形态批次 6 细化） |
| `action`（tool_executing/tool_done） | **照发**（工具卡片需要；v2 工具事件批次 4 细化） |
| `command` block | **照发**（代码块标记） |
| `command` inner_voice | **抑制**（已由 `generation.segment.started(channel=inner_voice)` 表达） |
| `token_usage` | **抑制**（已由 `generation.completed.usage` 表达） |
| `expression` | **抑制**（心情由 `generation.completed.mood` 落位 + 建连时的 mood 推送保留） |

任何来源的回合（v1 或 v2 触发）都产生统一回合事件；v1 触发时没有 v2 channel，v2 wire 无人接收，无副作用。

### 5.2 循环侧改动点（事件流测绘定位）

| 现状发射点 | 统一回合事件 | v1 呈现 | v2 呈现 |
|---|---|---|---|
| 生成开始（`_stream_llm_response` 前） | `turn_started` | （无新增） | `generation.started` |
| 新句子出现（SENTENCE 触发，段开始） | `segment_started` | （无新增） | `generation.segment.started` |
| TEXT_DELTA（loop.py:1021-1026） | `text_delta(text, seg, seq)` | `on_text_delta(text)` | `generation.delta` |
| SENTENCE 完成（loop.py:1006-1019） | `segment_completed(text, seg, tts_active)` | TTS 关→`send("")`；TTS 开→不变（由 `tts_active` 判定） | `generation.segment.completed` |
| INNER_VOICE（loop.py:989-998） | `segment_started/completed(channel=inner_voice)` | `command("inner_voice")` | `generation.segment.*` |
| MOOD（loop.py:984-987） | `mood(expr, intensity)` | `expression` | `generation.completed.mood`（完成时落位） |
| `_finalize_turn`（loop.py:736-746） | `turn_completed(finish_reason, usage, model, mood)` | `token_usage`（TokenTracker 现有）+ `state idle`（FSM 现有） | `generation.completed` |
| 异常路径（loop.py:366-373） | `turn_failed(code, message, retryable)` | `delta("❌ …")` + `send("")`（逐字节复刻现状） | `generation.failed` |

**TTS 状态传递（M4）**：`segment_completed` 携带 `tts_active`；v1 翻译层据此决定 `send("")` 或不变；v2 恒发 `segment.completed`（TTS 开关下都发，满足"TTS 开启时 v2 仍发段完成"）。

### 5.3 GenerationContext

- 每回合创建一个 `GenerationContext`：
  - `user_message_id`（来自 ACK 预分配）；
  - `assistant_message_id`（generation 开始时预分配，`msg_` 前缀）；
  - `generation_id`（`gen_` 前缀）；
  - `segment_index` 计数器（全局单调）；
  - 当前 `segment_id`（`seg_` 前缀）、`delta_sequence` 计数器、`channel` 追踪；
- 分段打开分配 `segmentId` 并递增 `segmentIndex`；段内 delta 递增 `deltaSequence`；工具轮不重置计数器。

### 5.4 完成时数据来源

- `usage`：`TokenTracker.current_turn_usages`（回合累计）+ `session_total`/`per_model`（会话累计）——数据已在 `token_tracker.py:62-64`，仅需在 `turn_completed` 读取；无调用记录时 turn 为空（§3.2）；
- `model`：`agent.llm.model`（实际模型）；
- `mood`：`character_engine.state.mood`（`_apply_mood` 已写）。

## 6. 取消（BE-023）

- v2 客户端发 `{"type":"interrupt"}`（沿用现有 wire）；
- 服务端：现有 interrupt 路径（`InterruptData` → 循环 `_handle_interrupt`）之上，若存在活动 generation 且错误路径未发 failed，发 `generation.failed{code: GENERATION_CANCELLED, retryable: false}` 并终止该 generation 的 TTS playback；
- 取消后服务端停止流式，不再发该 generation 的迟到 delta/segment 事件；
- **partial 落库**（B7 修正）：取消时若尚未到 `_finalize_turn`，assistant 消息不落库（现状如此）；仅工具轮前的 assistant 文本在 `_execute_tool_round` 落库。客户端以 session 回放为准，不假设"已落库部分必然存在"。

## 7. 重连（BE-024，最小语义）

- 重连后 v2 客户端照常收 `session` 回放（现有 `SessionMessage`，消息含稳定 `messageId`）；
- 若重连时存在**未完成**的 generation（源连接断开后仍在生成）：服务端把该 generation 标记 `interrupted`，向仍在线的 channel 发 `generation.failed{code: GENERATION_CANCELLED}`；
- 客户端判断：`session` 回放中收到消息但未见对应 `generation.completed` → 视为中断，按需重新生成；
- **批次 2 显式声明边界（A1）**：最新事件游标查询、generation 状态查询、pending 用户消息/工具调用查询、终态安全重放均记为 BE-024 后续细化，不在批次 2 实现；
- **客户端约束**：不确定服务端是否已接收时，不得自动重发副作用操作（副作用操作重发保护属批次 4 工具幂等范畴，本批次对文本消息由 `clientRequestId` 幂等覆盖）。

## 8. 契约模型清单（frontend_contract/ws_message.py 新增）

**Server→Client（加入 `ServerMessage` 联合）**：`UserMessageAckMessage`、`GenerationStartedMessage`、`GenerationSegmentStartedMessage`、`GenerationDeltaMessage`、`GenerationSegmentCompletedMessage`、`GenerationCompletedMessage`、`GenerationFailedMessage`。

**Client→Server（独立文档类，不进联合）**：`V2TextMessage`（type=`text` + 追加 `clientRequestId`/`clientMessageId`/`conversationId`）。注：gen_ts 的 `_collect` 会全量收集 BaseModel，`V2TextMessage` 仍会生成 TS interface（注 4，属预期）。

**UserInputData 扩展**：加在 `core/schema/event.py`（M1）。

**gen_ts**：重新生成 `contract.generated.ts`；`make check-types` 验证 v1 类型字节不变。

## 9. 兼容性分析

| 资产 | 影响 |
|---|---|
| v1 WS wire | **逐字节不变**（翻译层复刻）；`message content:""`、`state idle`、`❌` delta 照旧 |
| v1 前端运行时 | 未知 v2 type 走 `default` 分支忽略（已核 `useWebSocket.ts`） |
| 现有测试 | 全部保留；统一回合事件必须精确复现 v1 wire（回归测试守护） |
| `TokenTracker`/FSM/message 落库 | 不动数据逻辑；仅新增 `turn_completed` 读取已有数据 |
| 契约 | 新增模型进联合，v1 模型不碰 |

## 10. 测试要求（TDD；marker 按 docs/testing.md 标注）

1. **契约 round-trip**（unit）：每个新 v2 模型 `model_dump → model_validate` 往返一致；
2. **gen_ts 漂移**（unit）：`make check-types` 通过，v1 类型字节不变；
3. **ACK/幂等**（service）：重复 `clientRequestId` 同内容 → 一次生成、重发 ACK；不同内容 → rejected + error；缺 `clientRequestId` → `CONTRACT_VALIDATION`；忙时排队保留 messageId；
4. **生成序列**（service，v2 连接，FakeLLM 带标签响应）：`ack → started → segment.started(inner_voice) → … → segment.completed ×N → completed`，断言顺序、ID 一致、`segmentIndex` 全局递增、`deltaSequence` 段内递增、completed 携带 usage/model/mood；
5. **v1 回归**（service，同 agent，v1 连接）：wire 与现状逐字节一致；
6. **取消**（service）：interrupt → `generation.failed{GENERATION_CANCELLED}`，无迟到帧，终态唯一；
7. **失败**（service）：FakeLLM fault → `generation.failed{code}`，**无 `❌` delta**（v2）；v1 仍收 `❌` delta；
8. **TTS 边界**（service）：TTS 开启时 v2 `segment.completed` 仍发，v1 行为不变；
9. **回归**：`make test-quick` 全绿（Elysia conda）。

## 11. 产出交付物

1. 契约：`frontend_contract/ws_message.py` 新增 7+1 模型 + `gen_ts` 重新生成 TS 快照；
2. 代码：`Module/output/`（版本感知扇出 + v2 wire 方法）、`core/cognitive/loop.py`（统一回合事件 + GenerationContext + 预分配 messageId 贯通）、`core/input/ws_handler.py`（v2 文本路由 + ACK + 幂等 store）、`core/schema/event.py`（UserInputData 扩展）、`core/agent/_types.py`（_PendingMessage 携带 messageId）；
3. 测试：§10 全部用例（含 marker）；
4. 轨迹：v2 完整生成 + 取消 + 失败三份脱敏 JSONL（`frontend-handoff-v2/traces/`，若 gitignore 未覆盖需补充）；
5. 文档：更新 `docs/electron-backend-baseline.md` 事件面章节 + 新增 `docs/electron-backend-protocol-v2.md`（v2 事件契约参考）+ 模块 README 更新（`Module/output/README.md`、`core/cognitive/README.md`，按 readme-conventions.md）。

## 12. 退出标准

- [ ] v2 客户端一次回复收到完整结构化事件序列（ACK→started→segments→completed），不依赖空消息/`❌`/idle 推断；
- [ ] 幂等：重试不重复创建消息与生成；不同负载同 ID 报协议错误；忙时排队 messageId 一致；
- [ ] v1 客户端 wire 逐字节不变（回归测试通过）；
- [ ] 取消/失败结构化（`generation.failed`），v2 无 `❌` delta，终态唯一；
- [ ] `make test-quick` 全绿 + `make check-types` 通过；
- [ ] 三份 v2 脱敏 JSONL 可解析、可重放、无敏感数据；
- [ ] 未修改任何 v1 契约模型与 v1 事件发射语义。

## 13. 非目标（批次 2 不做的）

- 多会话（批次 3）；会话内 `conversationId` 恒 null；
- 重连续传 / 重新生成 regenerate / 事件游标与状态查询（BE-024 后续细化）；
- Desktop 工具桥（批次 4）；
- 非 text/markdown 分段渲染（批次 7 预留枚举）；v2 图片/多模态（沿用 v1 路径，见 §4.1）；
- 实时语音 / 输入法转写（批次 6）。
