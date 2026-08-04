# Electron 后端协议 v2 — 结构化聊天事件契约参考

**文档版本：** v0.2（批次 2 产出 v0.1，批次 3 补充多会话 §2.3/§9）
**关联：** `docs/electron-backend-baseline.md`（v1 现状）、`docs/electron-backend-security.md`（认证/版本协商）
**Spec：** `docs/superpowers/specs/2026-08-04-electron-backend-batch2-chat-v2.md`（v0.2）+ `docs/superpowers/specs/2026-08-04-electron-backend-batch3-multi-session.md`（v0.2）

本文档是 Electron 前端联调 v2 协议的**事件契约参考**。v2 由 WS 握手头 `X-Protocol-Version: 2` 判定；未带版本头走 v1 路径（现状不变）。

---

## 1. 握手与版本

- Electron Main 连 `/ws` 时带：`Authorization: Bearer <access_token>`（远程）+ `X-Protocol-Version: 2`。
- v2 连接建连帧：`session`（历史回放）→ `server_info`（协议版本/能力/服务形状）→ `expression`（当前心情）。
- `server_info.features` 如实反映能力：`structured_generation`（本批次 true 前为 false）、`multi_conversation`（批次 3）、`desktop_runtime`（批次 4）、`dictation`（批次 6）。

## 2. 一次完整 v2 回复事件序列

```
client → {"type":"text","content":"…","clientRequestId":"req_…","clientMessageId":"msg_…"}
server → {"type":"user_message.ack","messageId":"msg_…","clientRequestId":"req_…","receivedAt":"…","status":"accepted"}
server → {"type":"state","state":"thinking"}
server → {"type":"generation.started","generationId":"gen_…","messageId":"msg_a…","model":"…"}
server → {"type":"generation.segment.started","segmentId":"seg_…","segmentIndex":0,"channel":"inner_voice","kind":"text"}
server → {"type":"generation.segment.completed","…","content":"…"}
server → {"type":"generation.segment.started","…","segmentIndex":1,"channel":"public_reply","kind":"text"}
server → {"type":"generation.delta","…","deltaSequence":0,"delta":"你"}
server → …（delta ×N）
server → {"type":"generation.segment.completed","…","content":"完整段文本"}
server → …（public_reply 段 ×N）
server → {"type":"generation.completed","generationId":"gen_…","messageId":"msg_a…","finishReason":"stop","usage":{…},"model":"…","mood":"…","completeness":"complete"}
server → {"type":"state","state":"idle"}
```

### 2.1 事件类型

| type | 方向 | 关键字段 |
|---|---|---|
| `user_message.ack` | Server→Client | `messageId`（正式用户消息 ID，预分配落库一致）、`clientRequestId`、`clientMessageId`、`conversationId`、`receivedAt`（UTC）、`status`（accepted/rejected）、`error`（rejected 时 {code,message,retryable}） |
| `generation.started` | Server→Client | `generationId`（gen_）、`messageId`（assistant 父消息 ID）、`conversationId`、`requestId`、`model` |
| `generation.segment.started` | Server→Client | `generationId`、`segmentId`（seg_）、`segmentIndex`（全局单调）、`channel`（inner_voice/public_reply/status）、`kind`（text/markdown，其余预留） |
| `generation.delta` | Server→Client | `generationId`、`segmentId`、`segmentIndex`、`deltaSequence`（段内 0 起）、`delta` |
| `generation.segment.completed` | Server→Client | 上述 + `content`（完整段文本，public_reply 与 inner_voice 段都携带） |
| `generation.completed` | Server→Client | `generationId`、`messageId`、`channel`（public_reply）、`finishReason`（stop/tool_calls）、`usage`{turn,session}、`model`、`mood`、`completeness`（complete） |
| `generation.failed` | Server→Client | `generationId`、`messageId`、`code`、`message`、`retryable`、`details` |

### 2.2 Client→Server 文本（v2）

```json
{ "type": "text", "content": "…", "clientRequestId": "req_…", "clientMessageId": "msg_…", "conversationId": null }
```

- `clientRequestId` **必填**（幂等键）；缺省 → `user_message.ack{status:rejected, error:{code:CONTRACT_VALIDATION}}`。
- `conversationId` **批次 3 起生效**（多会话）：缺省 → 当前活动会话；显式 → 该会话（显式覆盖 active，见 §2.3）。非文本消息（content 数组）沿用 v1 多模态路径。

### 2.3 多会话：activate_session 与 conversationId 路由（批次 3）

**会话模型**：`SessionManager`（`core/memory/session_manager.py`）管理会话缓存池 + 活动指针。
默认会话 `"default"`（无缝衔接既有 Mongo `sessions` doc）；新建 `conv_<uuid4 hex[:12]>`。

**resolve 规则**（`agent.resolve_session(conversation_id, protocol_version)`，spec §3.1）：

| protocol_version | conversationId | 目标会话 |
|---|---|---|
| < 2（v1） | 任意 | **恒 `"default"`**（active 切换不影响 v1，回放/写入/统计与批次 0-2 一致） |
| ≥ 2（v2） | 缺省 | 当前活动会话（`agent.session_manager.active_id`） |
| ≥ 2（v2） | 显式 | 该会话（显式覆盖 active） |

**`activate_session`（Client→Server）**：

```json
{ "type": "activate_session", "conversationId": "conv_x" }
```

- 服务端切换活动会话 + 重发该会话 `session` 回放（复用建连回放路径）。
- 未知 conversationId → `error{code: SESSION_NOT_FOUND}`（批次 1 错误信封）。
- 缺 conversationId → `error{code: CONTRACT_VALIDATION}`。

**`text` 携带 conversationId**：文本路由到指定会话，`user_message.ack.conversationId` 与
generation.* 事件的 `conversationId` 均填**实际目标会话 ID**（非仅回显原始字段）。
v2 缺省 conversationId 时填活动会话 ID（如 `"default"`）。

## 3. ID 语义

| ID | 前缀 | 含义 |
|---|---|---|
| `messageId` | `msg_` | 落库消息 ID（稳定）。`user_message.ack.messageId` = 用户消息 ID；`generation.*.messageId` = assistant 父消息 ID（generation 开始预分配，落库一致） |
| `generationId` | `gen_` | 一次生成实例（重新生成时递增；`responseId` 弃用，别名 generationId） |
| `segmentId` | `seg_` | 一个分段（句子/多媒体块） |
| `segmentIndex` | — | 全局分段序号（跨 inner_voice/public_reply/工具轮单调递增） |
| `deltaSequence` | — | 段内增量序号（0 起） |

**段粒度**：一个 public_reply 段 = 一个句子（PRD BE-022）。段按 SENTENCE 边界开合；
每句有独立 `segmentId` 与全局 `segmentIndex`，`generation.segment.completed.content` 携带该句完整文本
（客户端无需拼接 delta 即可渲染整句）。注：中文无空格文本的 delta 可能在首段聚合
（tokenizer 按空格/换行 flush），但各句 content 以 `segment.completed` 为准。

## 4. 幂等

- 相同 `clientRequestId` + 相同内容摘要 → **重发原 ACK（同 messageId），不重复生成**。
- 相同 `clientRequestId` + 不同内容摘要 → `user_message.ack{status:rejected, error:{code:CONTRACT_VALIDATION}}`，不生成。
- 幂等表进程内 + TTL（1h）；重启丢失可接受（Demo）。

## 5. 完成 / 失败 / 取消

- **完成**：`generation.completed` 是唯一终态（携带 usage/model/mood）。前端**不得**从 `state idle` 推断回复结束。
- **LLM 层失败**：`generation.failed{code:GENERATION_PROVIDER_UNAVAILABLE|GENERATION_TIMEOUT, retryable:true}`。**不向 v2 客户端发 `❌` 前缀 delta**（v1 兼容保留）。
- **内部错误**：`generation.failed{code:INTERNAL, retryable:false}`。
- **取消**：客户端发 `{"type":"interrupt"}` → `generation.failed{code:GENERATION_CANCELLED, retryable:false}` + TTS 终止 + `state interrupted` → `state idle`。迟到增量被服务端停止。
- **终态唯一**：一次生成只有 `generation.completed` 或 `generation.failed` 之一；错误路径已发 failed 则打断不重发 CANCELLED。

## 6. 重连（最小语义）

- 重连后 v2 客户端经 `session` 回放获取历史（消息含稳定 `messageId`，与 ACK/completed 对齐）。
- v2 源连接在生成中断开 → 服务端取消在途 generation（标记中断）；部分消息保留。
- 客户端判断：回放中消息未见对应 `generation.completed` → 视为中断，可用**新** `clientRequestId` 重新生成。
- **批次 2 边界**：不做续传（断点续流）。事件游标/generation 状态查询/pending 查询由**批次 3**落地（见 §9 多会话 REST，spec BE-024）。

## 7. v2 通道对既有 v1 帧的收/抑制

| v1 帧 | v2 通道 |
|---|---|
| `state` / `typing` | 照发（FSM 状态、打字指示） |
| `audio` / `audio_end` | 照发（TTS 播放） |
| `action`（tool_executing/tool_done） | 照发（工具卡片） |
| `command` block | 照发（代码块标记） |
| `command` inner_voice | **抑制**（由 `generation.segment.started(channel=inner_voice)` 表达） |
| `token_usage` | **抑制**（由 `generation.completed.usage` 承载） |
| `expression` | **抑制**（心情由 `generation.completed.mood` 落位 + 建连 mood 推送） |

## 8. 真实轨迹

`frontend-handoff-v2/traces/`：

- `ws-v2-chat.jsonl` — 完整生成（ack → started → inner_voice 段 → public_reply 段 → completed）。
- `ws-v2-cancel.jsonl` — 取消（ack → started → … → generation.failed{CANCELLED} → interrupted）。
- `ws-v2-fail.jsonl` — 失败（ack → started → generation.failed{GENERATION_PROVIDER_UNAVAILABLE}）。
- `ws-v2-session.jsonl` — 多会话（批次 3）：`activate_session` 切隔离会话 → text 带 conversationId → generation.* conversationId=conv_trace_x。

生成方式：`scripts/capture_ws_trace.py v2chat|v2cancel|v2fail|v2session`。

## 9. 多会话 REST 查询（批次 3，spec BE-030/031 + BE-024）

产品级会话历史/搜索/资源/状态走 REST（`core/input/session_routes.py`），`/api/messages`
仍为全库 `_id` 游标兼容接口（零改动保留，spec §10.3）。

| 端点 | 说明 |
|---|---|
| `GET/POST /api/sessions` | 会话列表（cursor/limit/search，items 带 `active`）/ 创建（`conv_<uuid4>`） |
| `GET/PATCH/DELETE /api/sessions/{id}` | 详情（含首页消息）/ 重命名 / 删除（联动归档 + active 回落 default） |
| `POST /api/sessions/{id}/activate` | 激活会话（等价 WS `activate_session`） |
| `GET /api/sessions/{id}/messages` | 历史分页：`before`（`_id` 倒序 + has_more）+ `total` |
| `GET /api/sessions/{id}/messages?after={messageId}` | **增量拉取**（正序，仅锚点后新增） |
| `GET /api/messages/{mid}/context` | 上下文窗口：`conversationId&before=5&after=5` |
| `GET /api/search/messages` | 搜索：`query/conversationId/date_from/date_to/timezone/category/cursor/limit`（上限 100） |
| `GET /api/sessions/{id}/resources` | 资源索引：`category=image|link|video|audio|file`（sticker 恒空，text 400） |
| `GET /api/sessions/{id}/generations` | **generation 状态查询**：status/cursor/limit keyset 分页 |
| `GET /api/generations/{gid}` | generation 详情（404 `GENERATION_NOT_FOUND`） |
| `GET /api/sessions/{id}/pending` | **pending 查询**：pendingMessages（按会话）+ pendingApprovals（全局） |

错误信封统一（code/message/retryable）；未知会话 404 `SESSION_NOT_FOUND`；缺 conversationId 的
context 查询 400 `CONTRACT_VALIDATION`；非法时区 400 `INVALID_TIMEZONE`；全部匿名 401
（`AGENT_ACCESS_TOKEN` 配置时）。
