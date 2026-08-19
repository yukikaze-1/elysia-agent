# Electron 后端协议 v2 — 结构化聊天事件契约参考

**文档版本：** v0.7（批次 2 产出 v0.1，批次 3 补充多会话 §2.3/§9，批次 4 补充 Desktop 工具契约 §10/错误码目录，批次 5 补充模型 Profile 与 TTS 声音契约 §11 + 错误码 21→25，批次 6 补充三种语音与 TTS 播放契约 §12 + 错误码 25→32，批次 7 补充通用附件与媒体契约 §13 + 错误码 32→40，2026-08-10 接入复审补当前 cancel/fail golden、Desktop 精确连接投递、错误码 40→41 与媒体/签名文档同步）
**关联：** `docs/archive/electron-backend-baseline.md`（v1 现状）、`docs/electron-backend-security.md`（认证/版本协商）
**Spec：** `docs/superpowers/specs/2026-08-04-electron-backend-batch2-chat-v2.md`（v0.2）+ `docs/superpowers/specs/2026-08-04-electron-backend-batch3-multi-session.md`（v0.2）+ `docs/superpowers/specs/2026-08-05-electron-backend-batch4-desktop-tools.md`（v0.2）+ `docs/superpowers/specs/2026-08-05-electron-backend-batch6-voice-modes-tts-control.md` + `docs/superpowers/specs/2026-08-06-electron-backend-batch7-media-and-observability.md`

本文档是 Electron 前端联调 v2 协议的**事件契约参考**。v2 由 WS 握手头 `X-Protocol-Version: 2` 判定；未带版本头走 v1 路径（现状不变）。

---

## 1. 握手与版本

- Electron Main 连 `/ws` 时带：`Authorization: Bearer <access_token>`（远程）+ `X-Protocol-Version: 2` + **`X-Client-Platform: electron`**（批次 4，Desktop 工具桥定向投递目标标识，spec §5.1 D-l）。
- **版本协商（批次 9B）**：`X-Protocol-Version` 支持集合 **{缺省/空 → v1, `"1"` → v1, `"2"` → v2}**；其余显式值（`"2.0"`/`"3"`/乱写）在**升级前**返回 HTTP 400 `ProtocolError`（`code=CONTRACT_VALIDATION`，message 明示支持集合）。传输 token 与展示版本分离：握手头只能用 `"2"`；`server_info.protocol_version` 为展示值 `"2.0"`（**不可**回填握手头，否则 400）。机器契约产物 `contract.raw.ts`/`contract.schema.json` 文件头带 `transportVersion: "2"`（握手可用值）。
- v2 连接建连帧：`session`（历史回放，**含 `conversationId` = 本回放消息所属会话，批次 9A**）→ `server_info`（协议版本/能力/服务形状 + **`connectionId`**，批次 4）→ `expression`（当前心情，**自由文本非枚举**，客户端本地归一映射，未知值忽略）。
- **批次 8c（F15）**：switch_model / set_service 后的 `capabilities` 重播仅达 v1 通道——v2 用 `server_info` 取代 capabilities，`broadcast_capabilities` 跳过 protocol_version ≥ 2 的通道；v2 对切换的感知经自身 `switch_model_result`/`service_mode_result` 回显（多客户端 v2 切换同步为已知限制）。
- **`server_info.connectionId`（批次 4）**：仅 v2 + electron 连接下发（`conn_<uuid4 hex[:12]>`，= WS 握手生成的连接 ID）；普通 v2 / v1 恒省略。Electron 据此构造附件上传 `X-Client-Connection` 头（spec §5.4 持有者校验②依赖）。Desktop 工具 call 同样登记该 ID 为 `target_connection`，后端只向 `client_platform=electron && connection_id=target_connection` 的精确通道投递；重连期间旧连接即使尚未移除也不会收到新请求。
- **`server_info.build_sha`（批次 7，BE-081）**：构建 git HEAD 完整 SHA（`core/version.py` `get_build_sha()`，`git rev-parse HEAD`，非 git 仓库/失败 → None 缺省省略）；`server_version` 为 `__version__ + "+" + build_sha[:7]` 后缀（`build_server_version()`，无 git 时仅 `0.1.0`）。供前端展示/反馈定位。
- `server_info.features` 如实反映能力：`structured_generation`（批次 2 = true）、`multi_conversation`（批次 3 = true）、`desktop_runtime`（**批次 4 = true**）、`dictation`/`voice_recording`/`voice_realtime`（**批次 6 = true**，spec BE-060~062，见 §12）、**`media_upload`（批次 7 = true，POST /api/uploads，见 §13）**、**`idempotency_durable`（批次 D6c + 2026-08-12 BE-P1-07 强化：true=幂等 claim 完整跨重启持久化；false=内存降级窗口 或 存在未 reconcile 到 Mongo 的记录——Mongo 已恢复但 recover() 未跑/未清位时也为 false，不止内存降级）**。
- **v1 忽略 `X-Client-Platform`**：v1 无 desktop 概念，伪冒 platform 头不生效（负面隔离，spec §5.1）。

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

> `generation.completed.mood` 为**自由文本/中文标签，非枚举**（LLM `<mood>` 标签原样落位，如 `开心`/`平静`）；客户端负责本地归一映射，未知值安全忽略（阶段5联调口径，2026-08-18）。
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
| `attachmentId` | `att_` | 上传附件 ID（批次 7，POST /api/uploads 返回；消息 part 以 `attachment_id` 关联，见 §13） |

**段粒度**：一个 public_reply 段 = 一个句子（PRD BE-022）。段按 SENTENCE 边界开合；
每句有独立 `segmentId` 与全局 `segmentIndex`，`generation.segment.completed.content` 携带该句完整文本
（客户端无需拼接 delta 即可渲染整句）。注：中文无空格文本的 delta 可能在首段聚合
（tokenizer 按空格/换行 flush），但各句 content 以 `segment.completed` 为准。

## 4. 幂等

- 相同 `clientRequestId` + 相同内容摘要 → **重发原 ACK（同 messageId），不重复生成**。
- 相同 `clientRequestId` + 不同内容摘要 → `user_message.ack{status:rejected, error:{code:CONTRACT_VALIDATION}}`，不生成。
- **D6c（2026-08-11）**：幂等 claim 持久化到 MongoDB（`(conversation_id, client_request_id)` 唯一约束 + `expires_at` BSON Date TTL，1h）——ACK 丢失 + 后端重启后重试**不重复生成**。
  - **产品模式**（非回环绑定）：Mongo 离线 → v2 文本/voice.commit 幂等 fail-closed，`user_message.ack{rejected, STORAGE_UNAVAILABLE, retryable:true}`（不生成 messageId / 不重复 generation）。
  - **本地 dev**（回环绑定）：Mongo 离线 → 内存降级（非 durable 窗口），`server_info.features.idempotency_durable=false` 暴露；Mongo 恢复自动回 durable。
  - `idempotency_durable=true` = 当前 claim 跨重启持久化（`server_info.features` 新增字段，批次 D6c）。

## 5. 完成 / 失败 / 取消

- **完成**：`generation.completed` 是唯一终态（携带 usage/model/mood；**`mood` 为自由文本非枚举**——LLM `<mood>` 标签原样落位，客户端本地归一，未知值忽略，阶段5联调口径）。前端**不得**从 `state idle` 推断回复结束。
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
| `expression` | **抑制**（心情由 `generation.completed.mood` 落位 + 建连 mood 推送；**`mood` 为自由文本非枚举**，客户端本地归一，未知值忽略） |

## 8. 契约 replay 样例与历史轨迹

`docs/archive/frontend-handoff-v2/traces/`：

- `ws-v2-handshake.jsonl` — 当前 v2 握手（session → server_info → expression）。
- `ws-v2-chat.jsonl` — 当前完整生成（ack → started → inner_voice/public_reply → completed）。
- `ws-v2-cancel.jsonl` — 当前取消（partial segment → `GENERATION_CANCELLED` → interrupted → idle）。
- `ws-v2-fail.jsonl` — 当前 Provider 失败（started → `GENERATION_PROVIDER_UNAVAILABLE` → idle）。
- `ws-v2-session.jsonl` — 当前多会话 conversationId 贯通。
- `ws-v2-tool.jsonl` / `ws-v2-voice.jsonl` — 当前 Desktop 工具与实时语音样例。
- `ws-v2-rejected.jsonl` — 当前负面 ACK（rejected：`CONTRACT_VALIDATION` / `SESSION_NOT_FOUND` / `QUEUE_FULL` / `STORAGE_UNAVAILABLE` / `RATE_LIMITED` / `IN_PROGRESS`，加固批次 2026-08-11 新增）。

上述八个无 `legacy` 后缀的文件由 `scripts/gen_contract_frames.py` 从
`frontend_contract/` 确定性生成，并由 `--check` 做漂移门禁。批次 2 真实抓包改名为
`ws-v2-cancel-legacy-batch2.jsonl` / `ws-v2-fail-legacy-batch2.jsonl`，只作历史参考；
已停用的 `scripts/capture_ws_trace.py` 不是当前契约生成入口。

## 9. 多会话 REST 查询（批次 3，spec BE-030/031 + BE-024）

产品级会话历史/搜索/资源/状态走 REST（`core/input/session_routes.py`），`/api/messages`
仍为全库 `_id` 游标兼容接口（零改动保留，spec §10.3）。

| 端点 | 说明 |
|---|---|
| `GET/POST /api/sessions` | 会话列表（cursor/limit/search，items 带 `active`）/ 创建（`conv_<uuid4>`） |
| `GET/PATCH/DELETE /api/sessions/{id}` | 详情（含首页消息）/ 重命名 / 删除（联动归档 + active 回落 default） |
| `POST /api/sessions/{id}/activate` | 激活会话（等价 WS `activate_session`） |
| `GET /api/sessions/{id}/messages` | 历史分页：`before`（cursor 倒序，取更早）+ 响应 `messages/has_more/total` + **`cursor`** |
| `GET /api/sessions/{id}/messages?after={cursor}` | **增量拉取**（正序，仅锚点后新增） |
| `GET /api/messages/{mid}/context` | 上下文窗口：`conversationId&before=5&after=5` |
| `GET /api/search/messages` | 搜索：`query/conversationId/date_from/date_to/timezone/category/cursor/limit`（上限 100） |
| `GET /api/sessions/{id}/resources` | 资源索引：`category=image|link|video|audio|file|sticker`（sticker 经 StickerPart 填充为 `/static/stickers/{id}.png`，批次 7；text 400） |
| `GET /api/sessions/{id}/generations` | **generation 状态查询**：status/cursor/limit keyset 分页 |
| `GET /api/generations/{gid}` | generation 详情（404 `GENERATION_NOT_FOUND`） |
| `GET /api/sessions/{id}/pending` | **pending 查询**：pendingMessages（按会话）+ pendingApprovals（全局） |

- **分页 cursor（批次 9A，严格区分）**：`GET /api/sessions/{id}/messages` 的 `before`/`after` 参数**只接受上一响应返回的 `cursor`**（opaque——值为 Mongo `_id` 形式 24-hex，客户端**不解析只回传**）。响应在 `messages/has_more/total` 外新增 `cursor`（有更多页时返回：`before` 分页指向返回页最旧消息 `_id`、`after` 分页指向最新消息 `_id`；无更多/空页省略）。**稳定 messageId（`msg_`）不再作为分页参数**——`before={messageId}`/`after={messageId}` → 400 CONTRACT_VALIDATION。旧客户端手传 `_id` 值仍可用（值形式兼容）。

错误信封统一（code/message/retryable/requestId）；全部匿名 401（`AGENT_ACCESS_TOKEN` 配置时）。**批次 8c（F1/F2）：** 泛型 `error` 帧补 `retryable`/`details` 字段（语音错误站点透传）；admin/debug 端点（/shutdown、/restart、/tools、/tools/reload、/debug/api/*）错误响应统一走 ProtocolError 信封（`error` = message 兼容别名，v1 读 `.error` 不破坏），不再裸 `{"error":...}`。

### 9.1 错误码目录（批次 1-9C 累计，41 码）

| code | HTTP | retryable | 场景 |
|---|---|---|---|
| `AUTH_REQUIRED` | 401 | false | 未带凭据访问受保护端点（`/ws`、业务 `/api/*`、`/uploads/*`，批次 1；**批次 8c：admin/debug 端点同样**） |
| `AUTH_INVALID` | 401 | false | 凭据错误（批次 1；**批次 8c：admin/debug 端点同样**） |
| `RATE_LIMITED` | 429 | true | **落地（D6b，2026-08-11）**：公开入口限流——REST 高成本入口（provider test/upload/search/chat）超限 → HTTP 429 + `Retry-After`；WS 建连超限 → 升级前 429；WS 消息超限 → `error{RATE_LIMITED}` + retryHint，连接存活。key 认证主体优先、匿名回退 IP，默认不信任 `X-Forwarded-For`；单进程语义（多实例需自建共享后端） |
| `CONNECTION_LIMITED` | 503 | true | 并发 WS 连接超上限（批次 1） |
| `REQUEST_TOO_LARGE` | 413 | false | 请求体超限（`client_max_size`，Content-Length 前置，批次 1）；通用上传单文件/聚合超限（批次 7，§13.1） |
| `INVALID_REQUEST` | 403 | false | 参数/请求非法；desktop 附件上传持有者校验失败（连接 ≠ 目标连接、终态拒收，批次 1/4）。**既有漂移注明**：desktop 上传超限 / 类型非法用 `INVALID_REQUEST` + 413/415（目录行 HTTP=403 仅为通用映射，desktop 附件既有契约保持不动，批次 7 文档注明） |
| `INTERNAL` | — | false | 未归类内部错误（`generation.failed` 携带，WS 事件无 HTTP 信封）；附件记录写入失败 500（批次 7） |
| `GENERATION_PROVIDER_UNAVAILABLE` | — | true | 模型服务不可用（`generation.failed` 携带，批次 2） |
| `GENERATION_TIMEOUT` | — | true | 生成超时（`generation.failed` 携带，批次 2） |
| `SESSION_NOT_FOUND` | 404 | false | 未知会话（activate/CRUD/history/resources/pending/generations） |
| `GENERATION_NOT_FOUND` | 404 | false | 未知 generation 详情 |
| `RESOURCE_NOT_FOUND` | 404 | false | 消息不存在（context 窗口锚点缺失） |
| `INVALID_TIMEZONE` | 400 | false | 搜索时区非法（zoneinfo 无法解析） |
| `CONTRACT_VALIDATION` | 400 | false | 缺 conversationId / clientRequestId 冲突 / 畸形 before 游标 / 语音契约错误（缺/非法 stream_id、commit 文本不一致等，批次 6）；**未知 stickerId / 上传 kind=sticker / 未知上传 kind**（批次 7，sticker 非附件，拒绝走上传，见 §13.1）；**WS JSON decode / 内容校验失败 + GET /api/sessions 畸形 cursor**（批次 8c F5/F18） |
| `SESSION_PROTECTED` | 409 | false | 删除受保护会话 `default`（禁删守卫） |
| `AGENT_BUSY` | 409 | true | 生成/TTS 进行中切换或删除活动会话（FSM 忙时门禁） |
| `GENERATION_CANCELLED` | — | false | WS interrupt → `generation.failed{CANCELLED}` |
| `DESKTOP_UNAVAILABLE` | 503 | false | **目录登记，批次 4 内无错误信封生产者**（同批次 1 `RATE_LIMITED` 先例）——无 desktop 客户端时执行 desktop 工具以 `ToolResult.fail(error_type=UNAVAILABLE)` 呈现给 Agent（LLM 可见）；错误信封落地随后续 REST 暴露 |
| `CALL_NOT_FOUND` | 404 | false | **落地**于 `GET /api/tools/calls/{callId}`（未知 callId）与附件上传（未知 callId → 404） |
| `CALL_EXPIRED` | 408 | false | 目录成员；语义在桥内（超时 → `error_type=TIMEOUT` + repo status=expired），`GET /api/tools/calls/{callId}` 查询如实返回 `expired` 不抛错 |
| `CALL_IDEMPOTENCY_CONFLICT` | 409 | false | 目录成员；语义在桥内（`handle_status` 参数篡改拒绝：mark failed + 立即 resolve 失败态 + return False） |
| `SSRF_BLOCKED` | 400 | false | **落地**：自定义 endpoint 命中私网/云元数据/链路本地/回环等 SSRF 规则，或无法解析目标主机（`/api/providers` 保存 / `/api/providers/test` body base_url / switch 路径，批次 5）；消息统一「目标地址被拒绝或无法解析」，不泄露 host/IP |
| `PROFILE_IN_USE` | 409 | false | **落地**：删除当前激活 profile（活动会话 service_state / config / vision 指向 `custom/{name}`，批次 5） |
| `PROFILE_INVALID_RUNTIME` | 400 | false | **落地**：runtime 枚举非法，或非 backend runtime 携带 api_key（批次 5） |
| `PROFILE_RUNTIME_NOT_BACKEND` | 400 | false | **落地**：对 desktop/local 条目执行后端 test/switch（后端不代理本机 Profile，批次 5） |
| `VOICE_SESSION_CONFLICT` | 409 | false | 已有活动录音/会话时再 start（recording/realtime/dictation 通用，批次 6） |
| `VOICE_SESSION_NOT_FOUND` | 404 | false | pause/resume/stop/cancel 未知 sessionId（批次 6） |
| `VOICE_RECORDING_NOT_FOUND` | 404 | false | IDLE 态收 stop/commit（未知 recordingId，批次 6）；**`voice.discard` 已幂等成功不再产生**（阶段4真实联调修订） |
| `VOICE_RECORDING_TOO_LONG` | 413 | false | 录音缓冲超 `MAX_RECORDING_SECONDS`（600s ≈ 18.3MiB，批次 6） |
| `DICTATION_NOT_FOUND` | 404 | false | 无活动 dictation 时收 `dictation.stop`/`dictation.cancel`（批次 6） |
| `STT_UNAVAILABLE` | 503 | true | STT 未启用/后端不可用/活动 STT 为 desktop-local runtime（start 拒绝 / 转写失败 / 录音 stop 时 STT 已关，批次 6） |
| `STT_PROFILE_NOT_FOUND` | — | false | `dictation.start.sttProfileId` 不存在，或 profile runtime 不是 backend、后端不能执行（批次 9C；WS `error` 帧） |
| `PLAYBACK_NOT_FOUND` | 404 | false | `audio.cancel` 未知**或已结束**的 stream_id（前端可忽略良性，批次 6） |
| `ATTACHMENT_NOT_FOUND` | 404 | false | **落地**：附件不存在或已软删（下载/签发，批次 7，见 §13.2/§13.3） |
| `SIGNATURE_EXPIRED` | 403 | false | **落地**：签名 exp 非整数或不在 [now, now+24h] 窗口（下载校验序②，批次 7/8a） |
| `INVALID_SIGNATURE` | 403 | false | **落地**：sig 缺失/非法 hex / HMAC 不匹配（下载校验序①③，批次 7/8a） |
| `UNSUPPORTED_MEDIA_TYPE` | 415 | false | **落地**：filetype 魔数无法判定 / 不在白名单 / 扩展名与内容不一致（上传校验序③④⑤，批次 7，见 §13.1） |
| `TOO_MANY_FILES` | 400 | false | **落地**：单次上传文件数 > 10（上传校验序②，批次 7） |
| `ATTACHMENT_SIGNATURE_DISABLED` | 403 | false | **落地**：fail-closed——AGENT_ACCESS_TOKEN 配置而 ATTACHMENT_SIG_SECRET 未配置（下载/签发一律拒绝，批次 7，见 security §3.5） |
| `STORAGE_UNAVAILABLE` | 503 | true | **落地**：附件存储 Mongo 离线（上传写记录/下载/签发一律 503，fail-closed；上传已落盘文件删除不留孤儿，批次 7）；D6c 幂等 claim（产品模式 + Mongo 离线 → v2 文本/voice.commit 拒绝，批次 D6c） |
| `QUEUE_FULL` | 429 | true | **落地**：Agent 忙队列满——v2 附件/sticker 消息 `process_message` 返回 False → `user_message.ack{status:rejected, error:{code:QUEUE_FULL, retryable:true}}`（WS ACK 错误，无 HTTP 信封，HTTP 429 为目录登记映射，批次 7，见 §13.7） |

**数据保护语义**：`DELETE /api/sessions/default` → 409 `SESSION_PROTECTED`（`default` 恒存在且受保护，禁删——含其 append-only 归档）；生成/TTS 进行中 `activate`/`delete` 活动会话 → 409 `AGENT_BUSY`。

---

## 10. Desktop 工具契约（批次 4，spec §4/§5/§6/§7）

统一 Tool Runtime + Desktop 工具桥（BE-040~043）：四个 P0 Desktop 工具
（`desktop.file.select/read/write` + `desktop.screen.capture`，`runtime="desktop"`、
`approvalAuthority="client"`）经现有 v2 WS 连接完成「请求 → 客户端校验 → 用户审批 →
执行 → 结果回传」安全往返。**`desktop.command.run` 不注册**；服务器沙盒 `read_file`/
`write_file` 继续标 `runtime="backend"`（不伪装 Desktop）。

### 10.1 客户端标识与连接

- Electron Main 连 `/ws` 带 `X-Protocol-Version: 2` + `X-Client-Platform: electron`。
  握手成功后桥 `is_available()=true`，`server_info.features.desktop_runtime=true`，
  `server_info.connectionId = conn_<uuid4 hex[:12]>`（附件上传持有者校验头来源）。
- **仅 v2 连接**处理 desktop 语义；v1 忽略 platform 头（伪冒隔离）。
- **LLM 暴露过滤（D-j）**：desktop 工具仅客户端连接时出现在 LLM function schema
  （`definitions()`）与工具面板（`plugin_list` 的 `available`）；未连接时 definitions
  移除、plugin_list 保留 `available=false`。`GET /api/tools` 的 `desktopAvailable`
  随连接状态翻转。

### 10.2 Server→Client：`tool.request`

```json
{
  "type": "tool.request",
  "callId": "call_<uuid4 hex[:12]>",
  "toolName": "desktop.file.read",
  "toolVersion": "1.0.0",
  "runtime": "desktop",
  "arguments": {"path": "/home/user/doc.txt"},
  "reason": "用户请求分析所选文件",
  "riskLevel": "medium",
  "permissionRequired": true,
  "approvalAuthority": "client",
  "paramHash": "sha256…",
  "expiresAt": "2026-08-05T00:01:00+00:00",
  "requestId": "req_…"
}
```

- **定向单连接**投递到 `client_platform == "electron"` 的 v2 channel（非广播）；
  无目标连接时插件层已提前拦截（`is_available` 守卫 → UNAVAILABLE）。
- `paramHash` = `arguments` 的规范化 JSON（键排序 + 紧凑分隔 + 保留非 ASCII）SHA-256
  ——BE-043 篡改拒绝权威依据（仅覆盖 `arguments`，不覆盖 result 的 output/attachments）。
- `expiresAt` = 桥超时（120s）绝对过期时间。

### 10.3 Client→Server：`tool.status`（判别六态）

```json
{
  "type": "tool.status",
  "callId": "call_…",
  "status": "result",
  "toolName": "desktop.file.read",
  "toolVersion": "1.0.0",
  "runtime": "desktop",
  "paramHash": "sha256…",
  "output": "…",
  "error": {"code": "…", "message": "…"},
  "attachments": [{"url": "/uploads/…", "mimeType": "image/png", "label": "截图"}],
  "executionSummary": "已读取文件（4.2 KB）"
}
```

| status | 语义 | 桥行为 |
|---|---|---|
| `accepted` / `running` | Electron 已接手 / 执行中 | 仅更新 repo（不 resolve，阶段推进 accepted↔running 合法） |
| `result` | 成功（output + attachments + executionSummary） | repo `succeeded` + resolve；TextPart 用 `llm_summary`（executionSummary）优先 |
| `error` | 失败 `{code,message}` | repo `failed` + resolve（error.message → Agent） |
| `rejected` | 用户拒绝 | repo `rejected` + resolve（CANCELLED，"用户拒绝了该工具调用"） |
| `cancelled` | 客户端取消 | repo `cancelled` + resolve（CANCELLED） |

- **篡改拒绝（BE-043）**：回传 `toolName/toolVersion/runtime/paramHash` 与登记不一致 →
  立即 `mark failed`（`CALL_IDEMPOTENCY_CONFLICT`）+ resolve 失败态
  （TOOL_ERROR，"参数与请求不一致，已拒绝"）+ 丢弃该 status——不挂超时。
- **断连（D-o）**：目标连接断开 → repo 标 `offline`。未接手（pending）call 立即 resolve
  UNAVAILABLE（"桌面客户端已断开"）；已接手（accepted/running）call **不自动取消**
  （Electron 可能已副作用），future 挂起到超时或**晚到结果**。**重连不自动重投执行请求**
  （防重复副作用，仅可查/可重传 result）。
- **重传结果闭环**：offline 半终态可被晚到 `result/error/rejected/cancelled` 覆盖为终态
  （迁移表 `offline → succeeded/failed/…` 合法）；已终态 call 晚到 status 仅更新
  `result/error` 字段（终态枚举不可覆盖）。
- **幂等去重（BE-043）**：同 `(toolName, paramHash)` 且 call 仍 pending 的并发/重试 →
  复用原 callId、仅一次投递（防重复弹窗）；offline/expired 不参与去重（重试即新调用）。

### 10.4 附件上传

- **传输路径**：附件字节经 REST `POST /api/uploads/desktop/{callId}`（multipart `file` 字段）
  → 落盘 `/uploads/desktop/<callId>.<ext>` → 返回 `{url}` → Electron 在
  `tool.status{status=result}` 的 `attachments` 回填该 url。
- **持有者校验**：① callId 存在（否则 404 `CALL_NOT_FOUND`）；② 上传连接带
  `X-Client-Connection` 头 = 握手 `connectionId` = `target_connection`（否则 403
  `INVALID_REQUEST`）；③ call 处于可收结果状态（pending/accepted/running/offline，
  终态拒绝 403——防伪附件）。校验失败不落盘。
- **落盘为消息图片**：`tool_result.images`（image/* 附件）→ `ToolResultMessage.content`
  = `[TextPart(llm_summary/execution_summary), ImagePart(src=url, mime)]`——截图经落盘
  自动出现在历史回放/搜索/资源索引。

### 10.5 注册表 REST（BE-042）

| 端点 | 说明 |
|---|---|
| `GET /api/tools` | `ToolRegistryResponse{tools: ToolMetadata[16], desktopAvailable}`——完整元数据（runtime/riskLevel/version/permissionRequired/approvalAuthority/inputSchema/outputSchema/available）供 Electron 校验「只接受已注册工具和版本」 |
| `GET /api/tools/calls/{callId}` | `DesktopCallStatus`（callId/toolName/toolVersion/runtime/paramHash/status/createdAt/expiresAt/result/error）；status 用 repo 枚举（`succeeded` 等），`expired` 如实返回不抛 408；未知 callId → 404 `CALL_NOT_FOUND` |

认证走批次 1 产品认证（`AGENT_ACCESS_TOKEN` 配置时匿名 401）。

### 10.6 后端审批 enrich 版本门控（BE-042）

`tool_approval` 消息**类型不变**（v1 前端零破坏）：enrich 字段（`summary` 结构化参数摘要、
`runtime`、`version`、`riskLevel`、`permissionRequired`、`approvalAuthority`）**仅 v2
连接收到**；v1 连接收到与现状逐字节一致的原样 `{tool, request_id, params}`。
Desktop 工具**不走 `tool_approval`**（approvalAuthority=client，Electron 自管本地授权），
走 §10.2/§10.3 的 `tool.request`/`tool.status`。

### 10.7 阶段 6：工具展示帧与 sourceCallId（B-6，2026-08-19）

- **`tool.request` 新增必填 `sourceCallId`**（LLM `tool_call_id`，前端统一领域键）；`requestId`
  由 `ctx.trace_id` 填充；`runtime` 收敛为 `ToolRuntime`（backend|desktop|local_worker，wire 值不变）。
- **新增 Server→Client v2 帧**：
  - `tool.started{callId(LLM tool_call_id), toolName, runtime, inputSummary?, generationId?, conversationId?}`；
  - `tool.completed{callId, toolName, runtime, success, outputSummary?(≤2000 字符截断), error?({code,message}),
    errorType?, durationMs?, truncated?(outputSummary 超长截断), generationId?, conversationId?}`。
- **v2 停发 `action{tool_executing/tool_done}`**（v1 保持 action 逐字节不变）；
  Backend 工具 `tool.started/tool.completed` 为展示生命周期，后端只发不回环。
- `generation.completed` 不扩展 `tool_calls`；`finishReason` 成功路径仍恒 `stop`。

---

## 11. 模型 Profile 与 TTS 声音契约（批次 5，spec BE-050/051）

### 11.1 模型 Profile（`/api/providers` 扩展）

- **三类 Profile（llm/stt/tts）**：列表/新增/同名更新/测试/激活/删除 + 能力字段。`ProvidersResponse` 成员扩展（**既有键读取兼容，新增字段为纯增量**）：
  - `ProviderEntry` 增 `credentialConfigured`（env_key/url_key 已配置 或 local 恒 true）/ `runtime`（内置恒 `backend`）/ `capabilities`（从 providers.yaml 带出 `supports_prefix`/`supports_multimodal` 等）。
  - `CustomProvider` 增 `runtime`（`backend`/`desktop`/`local_worker`，缺省 `backend`）/ `credentialConfigured`（backend：Secret Store 有密钥 或 api_key_env env 已设；desktop/local：恒 false，密钥归 Electron 管理）/ `capabilities`。
- **密钥只写不可读**：`POST /api/providers` body `entry.api_key` 一次性提交 → 后端写入 Secret Store（`data/secrets/`，0600 + gitignore），**永不读回**，YAML 条目**不含密钥**；`credentialConfigured` 经 `GET /api/providers` 获取（save 响应返回全量 custom 列表，含密钥键剔除）。非 backend runtime 携带 api_key → 400 `PROFILE_INVALID_RUNTIME`。
- **保存**：backend 条目 `base_url` 必填 + SSRF 校验（`SSRF_BLOCKED`）；desktop/local 条目 `base_url` 可空、跳过 SSRF。同名更新不带 api_key → 保留既有密钥；runtime 从 backend 改 desktop/local → 清除密钥。
- **删除**：当前激活 profile（活动会话 `service_state.{svc}_model` / `config.{svc}.model` / `vision.model` 指向 `custom/{name}`）→ 409 `PROFILE_IN_USE`；非激活 → 删条目 + 清密钥。
- **切换**：WS `switch_model`（复用批次 2 语义）——runtime ≠ backend → 400 `PROFILE_RUNTIME_NOT_BACKEND`（后端不代理本机 Profile）；custom+backend → SSRF 校验 + 出站守卫注入；失败保留原选择。
- **测试**：`POST /api/providers/test` body `base_url` **非空时无论 provider 一律 SSRF 校验**（堵死任意拨号内网通道）；desktop/local 条目 → `PROFILE_RUNTIME_NOT_BACKEND`。

### 11.2 本条回复实际模型（generation.model）

`generation.started` / `generation.completed` / `generation.failed` 携带 `model`——generation 启动时的实际 LLM 模型快照（含热切换后），区别于 capabilities/switch 回显的"当前选择"。`completed.model` 保持必填。

### 11.3 TTS 声音（BE-051）

| 端点/消息 | 说明 |
|---|---|
| `GET /api/tts/voices` | `TtsVoicesResponse{voices: TtsVoiceEntry[], current?, source: "static"\|"api", error?}`——活动会话激活 TTS profile 的声音列表；TTS 未启用 → 200 `{voices:[], source:"static", error:"TTS 未启用"}`。数据源：MiniMax 动态拉取（`/v1/get_voice` + 进程内缓存）/ gpt-sovits 静态（已加载 YAML 的 voice keys）/ custom OpenAI 静态集（alloy/echo/fable/onyx/nova/shimmer） |
| `POST /api/tts/preview` | `TtsPreviewResult{ok, mime?, duration_ms?, audio_base64?, error?}`——固定短文本（「你好，我是声音测试。」）试听，**不读真实聊天、不写历史**；body `{voice_id?}` 缺省用当前 voice |
| WS `set_tts_voice` | Client→Server `{voice}` → 经活动会话激活 TTS 后端 `list_voices()` 校验存在 → 写活动会话 `service_state.tts_voice` + 回 `set_tts_voice_result {ok, voice?, error?}`；未知 voice/未启用 → `{ok: False, error}` **不改 state**（保留原选择） |
| 合成生效 | `TTSBackend.synthesize(..., voice_id=None)`——voice_id 非空跳过 character→voice 映射；每 turn 由发起会话的 `service_state.tts_voice` 注入（v2 显式 conversationId 指向非活动会话时声音随发起会话） |

---

## 12. 三种语音与 TTS 播放契约（批次 6，spec BE-060~063）

把唯一「WS BINARY PCM → VAD 自动提交」链路拆成三种隔离语音模式（普通录音 / 实时语音 / 输入法转写），并给 TTS 播放补 playback 语义。**三种语音模式均 v2-only**（v1 连接收到新消息**忽略**，debug 日志，不报错）；BINARY 帧按「有无活动语音会话」分流——有 → 喂给会话管理器，无 → legacy `AudioBuffer` VAD 自动提交（**v1/v2 逐字节不变**，保护现有 web 前端）。

### 12.1 模式总览

| 模式 | 触发 | VAD | 提交 | 字幕事件 |
|---|---|---|---|---|
| `RECORDING`（普通录音，BE-060） | `voice.record.start` | 否（全段缓冲） | 手动 `voice.commit` | `voice.transcript` |
| `REALTIME`（实时语音，BE-061） | `voice_session.start` | 是 | 每 VAD 段自动 | `voice_session.partial` / `voice_session.turn` |
| `DICTATION`（输入法转写，BE-062） | `dictation.start` | 是 | 无（只回文字） | `dictation.partial` / `dictation.final` |

- 会话/录音 ID 客户端生成（`recordingId=rec_<uuid>` / `sessionId=vs_<uuid>`），唯一性仅限本连接内。
- 已有活动会话再 start → `VOICE_SESSION_CONFLICT`；`agent.stt` 为 None 或未启用 → `STT_UNAVAILABLE`。
- **音频输入契约（批次 9C，固定格式）**：三种 start 帧（`voice.record.start`/`voice_session.start`/`dictation.start`）可带可选 `audio` descriptor `{codec, sampleRate, channels}`；唯一支持 `codec="pcm_s16le"` + `sampleRate=16000` + `channels=1`，**不符 → 升级前 `error{CONTRACT_VALIDATION}`，不建会话**。`audio` 缺省 → 按默认 16k/mono/pcm_s16le 处理（向后兼容）。BINARY PCM 帧 feed 时校验 `MAX_CHUNK_BYTES`（1 MiB），超限 → `error{CONTRACT_VALIDATION}` + 会话终止（`voice_session.ended`/`dictation.cancelled`；已 stop 的 TRANSCRIBING/PENDING 窗口只丢 chunk 不发 error）。**网络 chunk 无需 640B 对齐**：后端会跨 BINARY chunk 重组连续的 640 字节/20ms VAD 帧；流末不足一帧的部分只暂存、不单独判静音，后续字节到达后继续检测。**段/WAV 边界**：原始段音频（含最终非整帧尾部）原样送 STT，不截断。
- **音频不落盘**（内存缓冲）、不入消息 content——语音消息 = 纯文本消息（批次 7 才做音频附件，BE-070）。
- 失败信封区分：`voice.commit` 违规 → `user_message.ack{rejected}`；`voice.record.start/stop/discard` 与 `voice_session.*`/`dictation.*`/`audio.cancel` 违规 → `error{code}`。`voice.discard` 成功（含 IDLE/重复幂等）为 send-only 无 ACK；仅 recordingId 不匹配或 realtime/dictation 活跃时报 `error{VOICE_SESSION_CONFLICT}`（阶段4真实联调修订）。

### 12.2 普通录音（BE-060）

```
client → {"type":"voice.record.start","recordingId":"rec_…","audio":{"codec":"pcm_s16le","sampleRate":16000,"channels":1}}?（audio 可缺省，缺省按 16k/mono/pcm_s16le）
client → BINARY PCM × N（RECORDING 模式全段累积，不 VAD、不触发）
client → {"type":"voice.record.stop","recordingId":"rec_…"}
server → {"type":"voice.transcript","recordingId":"…","text":"…","emotion":"…"?"}（PENDING，待确认）
client → {"type":"voice.commit","recordingId":"…","text":"…","clientRequestId":"req_…","clientMessageId":"msg_…"?,"conversationId":null}?
server → {"type":"user_message.ack",…}（v2：幂等 + 预分配 messageId + 会话归属）
server → generation.*（Agent 流程，复用字幕文本，不重转写）
client → {"type":"voice.discard","recordingId":"…"}（取消任意未提交阶段：RECORDING/TRANSCRIBING/PENDING；IDLE/重复幂等成功，send-only 无 ACK，阶段4真实联调修订）
```

- 空转写不发 `voice.transcript`（前端未收字幕即视为未识别，无需 commit）；`< 640B` 录音视为空转写，不发 `voice.transcript`。
- `voice.commit` 幂等（clientRequestId，digest = 携带 text）：重试命中 → 重发原 ACK 不重复生成；异摘要 / 缺 clientRequestId / 文本与 PENDING 字幕不一致 → `user_message.ack{rejected, CONTRACT_VALIDATION}`。
- 录音缓冲超 600s（≈18.3MiB）→ `error{VOICE_RECORDING_TOO_LONG}` + 清缓冲；stop 时 STT 已关 → `error{STT_UNAVAILABLE}` + 清缓冲回 IDLE。
- **`voice.discard` 取消原语（阶段4真实联调修订）**：可取消当前录音的任意未提交阶段——RECORDING（未 stop，采集启动失败场景）、TRANSCRIBING（取消在途 STT，迟到结果不复活）、PENDING（丢弃字幕/情绪/events）；录音已结束（IDLE）后迟到/重复 discard 幂等成功，不发错误。错误 recordingId 或 realtime/dictation 活跃 → `error{VOICE_SESSION_CONFLICT}`，绝不误清其它会话。成功后不调 STT、不发 `voice.transcript`、不触发 generation。

### 12.3 实时语音会话（BE-061）

```
client → {"type":"voice_session.start","sessionId":"vs_…","conversationId":null,"audio":{…}}?（audio 可缺省）
server → {"type":"voice_session.started","sessionId":"…","conversationId":…}（解析后实际目标会话）
client → BINARY PCM × N（VAD 分段）
server → {"type":"voice_session.ack","sessionId":"…","seq":N}（~1s 背压进度，seq=服务端已收帧序数）
server → {"type":"voice_session.partial","sessionId":"…","turnId":1,"segmentIndex":0,"text":"…"}（字幕，该轮 final）
server → {"type":"voice_session.turn","sessionId":"…","turnId":1,"messageId":"msg_…","conversationId":…,"text":"…"}（自动提交，触发 generation）
client → {"type":"voice_session.pause"/"resume"/"stop"/"cancel","sessionId":"…"}
server → {"type":"voice_session.ended","sessionId":"…","reason":"stopped|cancelled|timeout|stt_disabled"}
```

- 每 VAD 段 = 一轮：partial（字幕）后自动提交——预分配 messageId + post UserInputData + turn 确认。多轮快速连发只响应最后一轮为预期（语音打断语义）。`turnId` 每轮单调递增、`segmentIndex` 会话内全局单调。
- 未知 sessionId 的 pause/resume/stop/cancel → `error{VOICE_SESSION_NOT_FOUND}`；暂停中再 pause / 未暂停再 resume → 幂等忽略。
- 300s 无音频 → `ended{timeout}`（暂停期间挂起 idle 计时）；`set_service('stt', False)` 成功 → 活动会话 `ended{stt_disabled}`。

### 12.4 输入法转写（BE-062）

```
client → {"type":"dictation.start","clientRequestId":"req_…","audio":{…}?,"sttProfileId":"sensevoice/sense-voice"}?（audio/sttProfileId 均可缺省）
client → BINARY PCM × N（VAD 分段）
server → {"type":"dictation.partial","text":"…"}（累积字幕，仅显示）
client → {"type":"dictation.stop","clientRequestId":"req_…"}
server → {"type":"dictation.final","clientRequestId":"req_…","text":"…"}（只回文字）
client → {"type":"dictation.cancel","clientRequestId":"req_…"}
server → {"type":"dictation.cancelled","clientRequestId":"req_…"}
```

- **零消息副作用**：不写历史、不 post UserInputData、不触发 Agent、不自动发送；final 文本只经 `dictation.final` 返回（前端据此写草稿）。
- **`sttProfileId`（批次 9C）**：可选 `<provider>/<model>` 模型标识。指定 → start 时解析并**快照独立 backend 实例到本 dictation 会话**（**不修改**全局 `agent.stt`/`service_state`，与其它连接/模式隔离；custom provider 建后端走 SSRF 出站守卫）；缺省 → 用全局活动 STT。profile 不存在 / runtime=desktop/local（Electron 执行）→ `error{STT_PROFILE_NOT_FOUND}`；本会话新建实例在 stop/cancel 时关闭（fire-and-forget）。
- 无活动 dictation 收 stop/cancel → `error{DICTATION_NOT_FOUND}`；空结果（静音）→ `final{text:""}` 仍返回；STT 中途关闭 → `dictation.cancelled`。

### 12.5 TTS 播放控制（BE-063）

`stream_id` 正式化为 **playback ID**（wire 键名不变，全链路统一 `stream_id`）。

```
server → {"type":"audio_start","stream_id":N,"format":"wav","sampleRate":…?,"durationMs":…?}（句开始播放，Phase 2 放行后、首 chunk 前）
server → {"type":"audio","stream_id":N,"seq":…,"data":"…","is_final":false}（×N，chunk 转发）
server → {"type":"audio_end","stream_id":N,"format":"wav"}（该句音频字节已全部下发 ≠ 播完）
client → {"type":"audio.cancel","stream_id":N}（取消在途播放实例，v2-only 全局操作）
server → {"type":"audio_cancelled","stream_id":N}（句被取消——audio.cancel / TTS 中断，前端清理孤儿 chunk）
```

- `sampleRate`/`durationMs` 从已合成 WAV bytes 解析（非流式合成完成后才有）；播放完成由前端本地 `onended` 判定（**不引入上行回执**）。
- `audio.cancel` 为**全局播放操作**（TTS 全局广播，任一连接可取消在途播放实例）：命中在途句 → 停发 + `audio_cancelled`；未知/已结束 stream_id → `error{PLAYBACK_NOT_FOUND}`（打断双发竞态下前端可忽略良性）；缺 / 非数字 stream_id → `error{CONTRACT_VALIDATION}`。
- 中断（`cancel_all`）不再静默丢——统一发 `audio_cancelled`（修「中断不补 audio_end」孤儿 chunk 缺口）。

---

## 13. 通用附件与媒体（批次 7，spec BE-070/071/081）

通用附件/媒体链路四个端点：`POST /api/uploads`（通用上传）、`GET /api/attachments/{id}`（签名下载）、`GET /api/attachments/{id}/sign`（签发）、`GET /api/stickers`（内置表情）。附件是**服务端托管资源**：字节落盘 `data/uploads/{kind}/{uuid4hex[:16]}.{ext}` + 元数据落 MongoDB `attachments` 集合（`core/storage/attachment_repo.py`，fail-closed）。旧 `/api/images`（legacy `/uploads/images/`）与批次 4 `/uploads/desktop/` 为遗留链路，**不纳入**附件体系（不签名、不进 GC）；sticker 是内置公共静态资源（`/static/stickers/{id}.png`），**非附件**（不落集合、不 ref_count、不参与 GC）。视频/音频/文件/表情**不喂 LLM**（`content_to_openai` 仅 text/image 进上下文）。

### 13.1 附件上传（POST /api/uploads）

multipart 字段：`files[]`（一个或多个文件字段）+ `kind`（可选，客户端意图**不信任**，仅 webm 歧义作 tiebreaker）。服务端校验序为权威：

| 步骤 | 校验 | 失败错误 |
|---|---|---|
| ① kind 枚举预检 | kind 缺省通过；不在 `image/video/audio/file` → 400 `CONTRACT_VALIDATION`（**含 kind=sticker**——sticker 非附件，拒绝走上传） | 400 `CONTRACT_VALIDATION` |
| ② 数量 | 文件数 ≤ 10，超限 | 400 `TOO_MANY_FILES` |
| ③ 单文件大小 | 读入逐块自限（各 kind 上限最大值 50MB）；kind 指定时按 kind 上限预检 | 413 `REQUEST_TOO_LARGE` |
| ④ 内容类型 | 二进制用 `filetype.guess` 魔数判定（不信任扩展名/Content-Type）；txt/md/json 走扩展名白名单 + UTF-8 内容 sanity；无法判定或不在白名单 | 415 `UNSUPPORTED_MEDIA_TYPE` |
| ⑤ 扩展名一致性 | 判定 MIME 与原始文件扩展名一致（jpg/jpeg 归一）；改名绕过 | 415 `UNSUPPORTED_MEDIA_TYPE` |
| ⑥ kind 归位 + 复核 | webm 以客户端 kind tiebreaker（audio/video），其余按 MIME→kind；归位后超 kind 上限 | 413 `REQUEST_TOO_LARGE` |
| ⑦ 写盘 + 落记录 | `data/uploads/{kind}/{uuid}.{ext}` + sha256 + `attachments` 记录；Mongo 离线（fail-closed）→ 已落盘文件删除不留孤儿 | 503 `STORAGE_UNAVAILABLE`（retryable）/ 500 `INTERNAL` |

- **整体聚合上限 64MB**：app `client_max_size` + payload 中间件 Content-Length 预检 → 413 `REQUEST_TOO_LARGE`（多文件总和超限同样被拦，2×50MB 视频属有意取舍）。
- **filename 脱敏（Minor 9）**：去路径分隔符、截断 255、剥控制字符；仅展示用，不参与路径构造。
- **响应** `AttachmentUploadResponse{attachments: AttachmentInfo[]}`；`AttachmentInfo{attachmentId(att_), kind, mime, size, url=/api/attachments/{id}（稳定路径，不随签名过期）, signedUrl?, filename}`。`signedUrl`（24h 短签）**fail-closed 省略**：AGENT_ACCESS_TOKEN 配置而 ATTACHMENT_SIG_SECRET 未配置时省略，其余模式恒带（见 security §3.5）。
- 任一文件校验失败 → 整个请求返回对应 4xx 错误（不回部分成功；已写盘的前序文件由 GC 规则 3 兜底回收）。

### 13.2 签名下载（GET /api/attachments/{attachment_id}）

**免 Bearer**（签名即授权，认证中间件豁免非 /sign 的 `/api/attachments/` 路径）；`?sig=...&exp=...` 查询参数授权。校验序（fail-closed：AGENT_ACCESS_TOKEN 配 + SIG 未配 → 一律 403 `ATTACHMENT_SIGNATURE_DISABLED`，不进入校验序）：

| 步骤 | 校验 | 失败错误 |
|---|---|---|
| ① sig 存在且合法 hex | 64 位小写 hex（上界 128 防超长串拖慢 `compare_digest`，DoS 防护） | 403 `INVALID_SIGNATURE` |
| ② exp 窗口 | 整数且在 [now, now+86400] | 403 `SIGNATURE_EXPIRED` |
| ③ HMAC 验签 | HMAC-SHA256(`attachment_id:exp`)，`hmac.compare_digest` 防时序侧信道 | 403 `INVALID_SIGNATURE` |
| ④ 记录存在且 active | 前三步通过后查 `attachments` 集合 status=active；软删/不存在 | 404 `ATTACHMENT_NOT_FOUND`；Mongo 离线 → 503 `STORAGE_UNAVAILABLE`（retryable） |

- **fail-open**（access_token 空 = 本地开发）→ 跳过 ①②③，仅执行 ④ status=active 校验。
- 成功 → aiohttp `FileResponse`（**原生 Range/206**，视频/音频拖动播放）；`Content-Type` = 规范化后 mime（经 `normalize_attachment_mime`，存量 audio 别名兼容，L6）；落盘文件缺失 → 404 `ATTACHMENT_NOT_FOUND`。
- **批次 8a 已闭合存在性 oracle**：有鉴权模式先验签、后查记录；无效签名恒 403，不能再借 404/403 差异判断 attachment_id 是否存在。

### 13.3 签发端点（GET /api/attachments/{attachment_id}/sign）

- **走 Bearer**（认证中间件不豁免本端点）。
- fail-closed 早退同上 → 403 `ATTACHMENT_SIGNATURE_DISABLED`；记录不存在/非 active → 404 `ATTACHMENT_NOT_FOUND`；Mongo 离线 → 503。
- 响应 `AttachmentSignResponse{attachmentId, url(?sig=&exp=), expiresAt(ISO UTC)}`。
- **渲染层签名解析（C-1）**：消息 part 持久化用**稳定路径** `/api/attachments/{id}`；渲染层对稳定路径调本端点拿签名 URL（缓存 + 24h 复用 + 过期重取），`<img>/<video>` 用签名 URL。**资源索引附件项同样经签发端点解析**（不单独带 signedUrl）。前端**不接触** `ATTACHMENT_SIG_SECRET`、不自行计算签名。

### 13.4 内置表情（GET /api/stickers）

- 服务端预置 `static/stickers/*.png`（sticker_id = 文件名去扩展名，如 `st_happy` → `st_happy`），`create_app` 启动时 `core/input/sticker_registry.py` 扫描；响应 `StickerListResponse{stickers: StickerEntry[]}`（`StickerEntry{stickerId, src=/static/stickers/{id}.png}`），按 id 排序稳定。
- 表情是**公共静态资源**（公开 `/static/stickers/{id}.png`，无鉴权）：不落 `attachments` 集合、不 ref_count、不参与 GC、不上传白名单。
- 发送载体：v2 文本消息 `stickerId` 字段（见 §13.7）。

### 13.5 MIME/编码矩阵

| kind | MIME（filetype 魔数真值判定） | 允许扩展名 | 大小上限 |
|---|---|---|---|
| `image` | image/png / image/jpeg / image/gif / image/webp | png / jpg·jpeg / gif / webp | 10MB |
| `video` | video/mp4 / video/webm | mp4 / webm | 50MB |
| `audio` | audio/wav / audio/mpeg / audio/ogg / audio/webm | wav / mp3 / ogg / webm | 20MB |
| `file` | application/pdf / application/zip / text/plain / text/markdown / application/json | pdf / zip / txt / md / json | 20MB |

- **webm 歧义**：mime=video/webm 时以客户端 kind 提示作 tiebreaker（kind=audio → kind=audio + **mime 规范化为 audio/webm**，其余 → video/webm），归位后按最终 kind 复核上限。
- **MIME 规范化**：`kind` 确定后，探测/历史别名收敛到契约权威值——`kind=audio + video/webm → audio/webm`、WAV 探测别名 → `audio/wav`（`core/schema/utils.py::normalize_attachment_mime` 单源，写/读/下载三路径同值）；探测别名仅作校验内部值，不对外输出。
- **编码收敛**：仅限上表扩展名（不接受其他编码）；二进制以**魔数判定为权威**并校验扩展名一致性；txt/md/json 因无稳定魔数，按扩展名白名单并要求内容可解码为 UTF-8，客户端 Content-Type 均不作为信任依据。

### 13.6 引用计数 / 软删 / GC 语义

- **消息关联**：v2 附件/sticker 消息 `process_message` 接受后 `add_ref(attachment_id, message_id)`（refs 加消息 ID + ref_count 同步，幂等去重）；消息删除/移除联动 `remove_ref`（refs 归零 → 软删 `status=deleted` + `deleted_at`）。
- **软删**：签名 URL 对软删附件直接 404（**删除语义 = 不可再取**）；文件保留，由 GC 延后物理删。
- **GC 四规则**（启动兜底扫描，`core/storage/attachment_gc.py`，限四 kind 目录 `data/uploads/{image,video,audio,file}/`）：
  - 规则 1：`status=deleted` 且 `deleted_at` 超 24h → 删文件 + 删记录；
  - 规则 2：active 记录磁盘文件缺失 → 软删 + warning；
  - 规则 3：active 且 `ref_count=0` 且 `created_at` 超 **7 天**（未使用上传）→ 软删（物理删由规则 1 延后）；
  - 规则 4：磁盘四 kind 目录有文件但无记录（磁盘孤儿）→ 删文件。
- Mongo 离线 → GC 跳过（`skipped`，附件操作 fail-closed）。legacy `data/uploads/images/`、`data/uploads/desktop/` 不在扫描范围。

### 13.7 消息关联 wire（v2）

- `V2TextMessage` 增可选 **`attachments: AttachmentRef[]`** 与 **`stickerId: string`**：`AttachmentRef{attachmentId(att_), kind, mime}`——kind/mime 为**客户端提示值**，服务端以 `attachments` 集合记录为准（不校验提示与记录一致，展示用记录值）；stickerId（st_ 前缀）与 attachments 可共存或独立。附件/sticker-only 消息触发生成（`content` 可空）。
- `ContentPart` 判别联合含 6 类：text/image/file/audio/video/sticker；Image/File/Audio/Video Part 均带 **`attachment_id`**（att_ 前缀，消息关联）；`StickerPart{sticker_id, src=/static/stickers/{id}.png}`（无 attachment_id）。
- **幂等摘要输入含附件与 sticker**：`not attachments and stickerId is None → digest=sha256(text)`；否则 `digest=sha256(text + "\x00" + ",".join(sorted(attachment_ids)) + "\x00" + (stickerId or ""))`——同文本换附件/表情不误判重放（`attachments: []` 空数组等同缺省）。
- 队列满（`process_message` 返回 False）→ ACK `rejected` + `error{code: QUEUE_FULL, retryable: true}`，幂等 entry 删除（重试允许重新进入），refs 不递增。**批次 8b：纯文本 v2 路径与附件/表情路径统一**——均「先 process 后判定」，队列满/process_message 抛异常（`INTERNAL`，删 entry）都发 rejected，不再漏发 accepted。
- **资源索引**：sticker 分类经 StickerPart 填充为 `/static/stickers/{id}.png`（§9 行同步）；附件项（`/api/attachments/{id}`）经 `_attachment_available` **软删过滤**——记录 deleted/不存在/Mongo 离线保守不展示。
- **关联 ID（P7-12）**：`ErrorMessage.requestId` 数据源为 WS 侧请求 ID（UserInputData.request_id，`get_request_id()` ContextVar）；有则带、无则 None 缺省省略——客户端可凭此关联日志/幂等排查。`GenerationRegistry` 亦记 `request_id`。
