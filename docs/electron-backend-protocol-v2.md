# Electron 后端协议 v2 — 结构化聊天事件契约参考

**文档版本：** v0.4（批次 2 产出 v0.1，批次 3 补充多会话 §2.3/§9，批次 4 补充 Desktop 工具契约 §10/错误码目录，批次 5 补充模型 Profile 与 TTS 声音契约 §11 + 错误码 21→25）
**关联：** `docs/electron-backend-baseline.md`（v1 现状）、`docs/electron-backend-security.md`（认证/版本协商）
**Spec：** `docs/superpowers/specs/2026-08-04-electron-backend-batch2-chat-v2.md`（v0.2）+ `docs/superpowers/specs/2026-08-04-electron-backend-batch3-multi-session.md`（v0.2）+ `docs/superpowers/specs/2026-08-05-electron-backend-batch4-desktop-tools.md`（v0.2）

本文档是 Electron 前端联调 v2 协议的**事件契约参考**。v2 由 WS 握手头 `X-Protocol-Version: 2` 判定；未带版本头走 v1 路径（现状不变）。

---

## 1. 握手与版本

- Electron Main 连 `/ws` 时带：`Authorization: Bearer <access_token>`（远程）+ `X-Protocol-Version: 2` + **`X-Client-Platform: electron`**（批次 4，Desktop 工具桥定向投递目标标识，spec §5.1 D-l）。
- v2 连接建连帧：`session`（历史回放）→ `server_info`（协议版本/能力/服务形状 + **`connectionId`**，批次 4）→ `expression`（当前心情）。
- **`server_info.connectionId`（批次 4）**：仅 v2 + electron 连接下发（`conn_<uuid4 hex[:12]>`，= WS 握手生成的连接 ID）；普通 v2 / v1 恒省略。Electron 据此构造附件上传 `X-Client-Connection` 头（spec §5.4 持有者校验②依赖）。
- `server_info.features` 如实反映能力：`structured_generation`（本批次 true 前为 false）、`multi_conversation`（批次 3）、`desktop_runtime`（**批次 4 = true**）、`dictation`（批次 6）。
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

错误信封统一（code/message/retryable/requestId）；全部匿名 401（`AGENT_ACCESS_TOKEN` 配置时）。

### 9.1 错误码目录（批次 1-5 累计）

| code | HTTP | retryable | 场景 |
|---|---|---|---|
| `AUTH_REQUIRED` | 401 | false | 未带凭据访问受保护端点（`/ws`、业务 `/api/*`、`/uploads/*`，批次 1） |
| `AUTH_INVALID` | 401 | false | 凭据错误（批次 1） |
| `RATE_LIMITED` | 429 | false | 速率限制——**目录登记，限流器未实现**（无生产者） |
| `CONNECTION_LIMITED` | 503 | true | 并发 WS 连接超上限（批次 1） |
| `REQUEST_TOO_LARGE` | 413 | false | 请求体超限（`client_max_size`，Content-Length 前置，批次 1） |
| `INVALID_REQUEST` | 403 | false | 参数/请求非法；desktop 附件上传持有者校验失败（连接 ≠ 目标连接、终态拒收，批次 1/4） |
| `INTERNAL` | — | false | 未归类内部错误（`generation.failed` 携带，WS 事件无 HTTP 信封） |
| `GENERATION_PROVIDER_UNAVAILABLE` | — | true | 模型服务不可用（`generation.failed` 携带，批次 2） |
| `GENERATION_TIMEOUT` | — | true | 生成超时（`generation.failed` 携带，批次 2） |
| `SESSION_NOT_FOUND` | 404 | false | 未知会话（activate/CRUD/history/resources/pending/generations） |
| `GENERATION_NOT_FOUND` | 404 | false | 未知 generation 详情 |
| `RESOURCE_NOT_FOUND` | 404 | false | 消息不存在（context 窗口锚点缺失） |
| `INVALID_TIMEZONE` | 400 | false | 搜索时区非法（zoneinfo 无法解析） |
| `CONTRACT_VALIDATION` | 400 | false | 缺 conversationId / clientRequestId 冲突 / 畸形 before 游标 / 其他契约错误 |
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
