# Electron v2 首次联调 runbook

> ⚠️ **Historical execution record.** 本文描述**执行当时**的系统与结论；后续批次可能推翻其中部分判断。
> 当前事实见 [docs 索引](../README.md) 的 Canonical References（架构 / 不变量 / 测试 / CI / 运维 / 契约）。
> 正文**按当时原样保留**——不改结论、不删失败轨迹、不重写 Challenger 反证（`docs/readme-conventions.md` Rule 3）。

> 文档导航：[项目文档](../README.md)

> **唯一出处**：backend runtime SHA / schemaHash / frontend SHA、`SERVER_HOST`/端口/Token、探针口径、WS 三请求头、clean-start bootstrap、ACK/generation 成功标准与负面用例、requestId/clientRequestId 对照。
> 后端侧整改已完成（2026-08-11 加固批次，P0×2 + P1×5 + P2×3 闭环），见 `docs/review/archive/2026-08/electron-v2-first-integration-backend-review-2026-08-11.md` 与 `docs/review/electron-v2-backend-change-list-2026-08-11.md`。

---

## 1. 冻结基线（2026-08-14 B-01 已回填）

| 项 | 值 | 说明 |
|---|---|---|
| **backend runtime SHA** | **`73ac7a32415aa96bbec7687ae0c9e674ab675438`** | 第三次真实联调实际运行 commit，`server_info=0.1.0+73ac7a3` |
| **schemaHash** | `e5d4dd2b9296b727` | 机器契约身份标识（`frontend_contract/contract.schema.json`，**非 server_info wire 字段**）；跨仓检查 WS v2 `13/13`、字段级帧 `63` 条通过 |
| contractCommit | `fb9f8f1dc34e06595e6b148c54b1cf844f300966` | 当前契约产物最后重生成 commit（非运行时） |
| **frontend SHA** | **`6b27e2e1ebf4e6ba3e335352379892e7c2679da2`** | 含 `perMessageDeflate:false` 空闲断连修复与握手回归的第三次联调 commit |
| transportVersion / protocolVersion | `"2"` / `"2.0"` | 握手头 `X-Protocol-Version: 2`（缺省/`"1"`→v1；`"2.0"`/`"3"`→升级前 400） |

### 1.1 B-01 实际结果

2026-08-14 已在 Windows Electron `43.3.0` / Chrome `150.0.7871.212` + Ubuntu 回环后端上完成 B-01 十项真实复跑。纯文本、两次真实工具、Renderer reload、断开重连/replay、Store/DOM 去重、取消、存量 `default` 与全轮诊断均通过。修复后两轮 `76s` 空闲复验未出现断线或 RSV 错误。

证据见 [2026-08-14 Windows Electron 第三次真实联调结果](../review/archive/2026-08/2026-08-14-Windows-Electron-第三次真实联调结果.md)。该结论表示阶段 2 单机主链已真实联调，不等于多客户端、故障恢复或正式发布门禁全部完成。

## 2. 启动条件

### 2.1 绑定与端口

- 默认 `SERVER_HOST=127.0.0.1`、`--port`/`SERVER_PORT` 默认 `8765`；
- **非回环绑定必须配 `AGENT_ACCESS_TOKEN`**（`assert_secure_bind` fail-closed，否则拒绝启动）；远程联调 = 显式 `SERVER_HOST=0.0.0.0` + token（或 `ALLOW_INSECURE_REMOTE=1` 仅本地开发开关）。本地回环联调无需 token。

### 2.2 数据库条件

| 依赖 | 条件 | 影响 |
|---|---|---|
| **Mongo** | **后端启动前已可用，联调期间保持稳定** | `/ready` chatReady 门禁；幂等 claim 持久化（产品模式离线 → fail-closed `STORAGE_UNAVAILABLE`） |
| **Milvus** | 可降级 | **不门禁 HTTP**（文本可降级使用）；经 `/ready` payload 的 `fullReady` 可观测 |

### 2.3 Token

| Token | 保护范围 |
|---|---|
| `AGENT_ACCESS_TOKEN` | 产品 `/ws`、业务 `/api/*`、`/uploads/*` |
| `SERVER_AUTH_TOKEN` | 管理 `/shutdown` `/restart` `/tools` `/debug/*` |

## 3. 探针口径（不要误解）

- **`/health`（liveness）**：纯进程存活检查，**恒 200**，零网络探针——任何依赖挂起都在固定小预算内返回，不携带依赖状态；
- **`/ready`（readiness）** = **核心 chat 门禁**：`chatReady`（mongo 三 repo 并行 ping + product 模式时 `idempotency ping AND not needs_recovery`——降级期幂等记录未 reconcile 时 503，2026-08-12 BE-P1-07）→ `200`，否则 `503`（读 payload `chatReady`/`fullReady`/`dependencies` 判断文本可用/不可用原因）；**Milvus 不门禁 HTTP**；**product 模式（非回环绑定）idempotency 失效或未 reconcile → `/ready` 503**（v2 send 不可用）。
- **`SERVE_CHAT=0`（默认）→ `/` 返回 404 是正常现象**（v1 网页聊天停用），**不是联调失败**；Debug `/debug` HTML 页匿名可加载（`/debug/api/*` 走管理 token）。

## 4. WS 握手三请求头（Electron Main 必带）

| Header | 值 |
|---|---|
| `Authorization` | `Bearer <AGENT_ACCESS_TOKEN>` |
| `X-Protocol-Version` | `2` |
| `X-Client-Platform` | `electron` |

升级前失败：认证失败 401（非 101）；建连限流 429（带 `Retry-After`）；版本不支持 400。凭据只在 Header，**绝不放 URL 查询参数**（`/ws?token=...` 不做）。

## 5. clean-start bootstrap（全新数据目录）

1. `GET /api/sessions` → `[default(active=true)]`（恒恰一个 active；detail `updatedAt` 非空可解析）；
2. WS 首帧 `session{conversationId:"default", ...}`（回放）→ `server_info`（features/version——**无 schemaHash**；schemaHash 仅离线门禁，见 :15）；
3. Electron **无需手工「新建会话」即可发送**——缺省 `conversationId` 解析为当前 active；显式 `conversationId` 必须是已登记会话（未知 → rejected `SESSION_NOT_FOUND`，禁幽灵会话）。

## 6. ACK 成功标准与负面用例

### 6.1 成功标准（accepted）

`V2TextMessage{type:"text", conversationId?, clientRequestId, content, ...}` → 回：

```json
{ "type": "user_message.ack", "status": "accepted",
  "messageId": "msg_...", "clientRequestId": "<原值>", "receivedAt": "..." }
```

- `messageId` 非空（`min_length=1`，预分配落库一致）；`clientRequestId` 回显**原值**；无 error；
- 随后按 §7 生成事件流。

### 6.2 负面用例（rejected）

```json
{ "type": "user_message.ack", "status": "rejected", "messageId": "",
  "clientRequestId": "<原值或空串>",
  "error": { "code": "...", "message": "...", "retryable": true, "retryAfterMs": 1000 } }
```

| code | 触发 | retryable |
|---|---|---|
| `CONTRACT_VALIDATION` | 帧校验失败 / 同键异内容重放 | false |
| `SESSION_NOT_FOUND` | 显式 conversationId 不存在 | false |
| `QUEUE_FULL` / `AGENT_BUSY` | 忙时门禁 | 按 error 字段 |
| `STORAGE_UNAVAILABLE` | 产品模式 + Mongo 离线（幂等 lookup/claim 失败；`details` 带 `idempotency_durable`） | true |
| `RATE_LIMITED` | text/voice.commit 超限（关联原 `clientRequestId`） | true（带 `retryAfterMs`） |
| `IN_PROGRESS` | 同键 CLAIMED 新鲜窗口（≤30s，原请求在途） | true（带 `retryAfterMs`） |

> **重要**：rejected 的 `clientRequestId` 在「缺少 clientRequestId」路径回显**空串**（schema 无 `min_length`）——前端 raw parser **不得**用 `min(1)` 校验丢弃 rejected ACK（这正是 BE-P1-04 修复的根因）。`messageId` 恒空串（`Literal[""]`，D2）。

## 7. generation 事件序列（六事件）

六种 `generation.*` 帧：`generation.started` / `generation.segment.started` / `generation.delta` / `generation.segment.completed` / `generation.completed` / `generation.failed`。

happy path：

```
generation.started                    # messageId = assistant 父消息（预分配）
  → generation.segment.started        # 段落开始
  → generation.delta × n              # 流式正文
  → generation.segment.completed      # 段落完成（content 为权威完整文本）
  → (更多 segment 循环)
  → generation.completed              # 唯一终态（finishReason: stop | tool_calls）
```

失败路径：`generation.failed`（取消/错误终态）。`generation.completed.content` 是后端权威完整文本——Electron Adapter/Accumulator 据此消费，inner voice 与 TTS 场景允许零 delta。

## 8. 联调纪律（review §8.5，必须遵守）

- **每次发送使用全新 `clientRequestId`**（UUID 级唯一）；
- **不并发发送同一个 ID**（同键并发会命中幂等 claim，loser 只重放/拒绝，不产生第二条 generation）；
- **ACK 未决时不重传同一 `clientRequestId`**——等 accepted/rejected ACK，或明确超时后凭后端证据（generation/消息落库）决定重试；
- 保持在默认限流阈值内（ws 120 / messages 120 / provider_test 30 / upload 60 / search 60 / tools 120 每分钟，config 可调、0 关闭）；收到 `RATE_LIMITED` 按 `retryAfterMs` 退避后可用同 ID 安全重试（连接存活）。

## 9. 通过标准（review §8.8 六事件序列）

首轮受控联调记录以下序列全部通过（结果只标「happy path 已联调」，不据此宣称故障恢复/生产就绪）：

1. **session**：WS 首帧 `session` 回放，`conversationId=default`；
2. **server_info**：握手快照（版本 + `features`；**不携带 schemaHash**——schemaHash 是 `contract.schema.json` 的离线身份，联调前经 `python -m frontend_contract --check` 验证，不上握手帧）；
3. **expression**：情绪/表情帧出现；
4. **REST 创建/列表**：`POST/GET /api/sessions`——创建新会话 + 列表含 default active；
5. **accepted ACK**：messageId 非空、clientRequestId 回显原值；
6. **generation 六事件**：§7 happy path 序列完整（started → segment 循环 → completed）；
7. **completed**：`generation.completed` 唯一终态（finishReason stop/tool_calls）；
8. **历史读取**：`GET /api/sessions/{id}/messages`（cursor 分页）能读到该轮消息；
9. **断开**：clean close（无异常帧/未决 ACK 悬空）。

外加：`/health` 恒 200；`/ready=200`（Mongo 正常时）；`/` 404（`SERVE_CHAT=0` 正常）。

## 10. requestId / clientRequestId 对照

| 字段 | 生成方 | 语义 | 用途 |
|---|---|---|---|
| `requestId` | **后端** | 后端每任务日志关联 ID（ContextVar） | 排障：错误帧/日志关联，**不用于业务关联** |
| `clientRequestId` | **客户端** | 幂等键（`(conversation_id, client_request_id)` 唯一，TTL 1h） | ACK 关联 / 去重 / 重试 |

- **text / `voice.commit`** 的限流错误走 **rejected ACK 带原 `clientRequestId`**（立即 settle 对应 promise）；
- **命令类**（switch_model / tool_approval / set_service / set_tts_voice / activate_session / wake_toggle）超限 → `ErrorMessage` 带可选 `clientRequestId` + 顶层 `retryAfterMs`（这些命令消息契约已加可选 `clientRequestId`）；
- 排障对照方法：日志按 `requestId` 查后端处理轨迹；客户端按 `clientRequestId` 查 ACK/幂等去重结果——两者在 wire 上是两个 ID，别混用。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../README.md) | 快速开始 + 文档导航 |
| [docs/electron-backend-protocol-v2.md](../electron-backend-protocol-v2.md) | v2 协议全量契约（消息帧、REST、错误码 41 码） |
| [docs/electron-backend-security.md](../electron-backend-security.md) | 远程认证/TLS/限制 + 探针分层（§3.7）+ 启动守卫 fail-closed（§1.4） |
| [docs/archive/contract-release-2026-08-11.md](contract-release-2026-08-11.md) | contractCommit/runtimeCommit 区分 + schemaHash 溯源 |
| [docs/review/electron-v2-backend-change-list-2026-08-11.md](../review/electron-v2-backend-change-list-2026-08-11.md) | 2026-08-11 新增 wire 契约变更逐条回放 |
| [docs/review/archive/2026-08/electron-v2-first-integration-backend-review-2026-08-11.md](../review/archive/2026-08/electron-v2-first-integration-backend-review-2026-08-11.md) | 首次联调后端复核整改清单（已闭环） |
