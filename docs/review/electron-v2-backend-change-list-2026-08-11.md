# Electron 后端契约变更清单（2026-08-11）

> F5 交接：本次发版（D6b/D6c 产品化门禁 + **2026-08-11 加固批次**：P0×2 + P1×5 + P2×3）对 Electron
> 前端的 wire 契约影响，逐条可回放。前置批次 9A/9B/9C 的变更见 `docs/electron-backend-protocol-v2.md`
> + golden frames（`docs/archive/frontend-handoff-v2/traces/`）；加固批次设计与决策见
> `docs/superpowers/specs/2026-08-11-electron-v2-backend-hardening-design.md`，原始问题清单见
> `docs/review/electron-v2-first-integration-backend-review-2026-08-11.md`。本清单只列 **2026-08-11 新增**。

## 1. 新增：`server_info.features.idempotency_durable`

- **字段**：`features.idempotency_durable: boolean`
- **语义**：当前幂等 claim 是否跨重启持久化。
  - `true` = Mongo 可用，`clientRequestId` 去重在进程重启后仍有效。
  - `false` = Mongo 离线 + 回环（本地 dev）内存降级——**非 durable 窗口**：重启后
    同 `clientRequestId` 重试可能重复生成（客户端应在 `idempotency_durable=false`
    期间对重试更保守，或提示用户）。
- **旧客户端**：忽略新增字段即可（wire 兼容）。

## 2. 变更：幂等 fail-closed（产品模式 + Mongo 离线）

- **场景**：后端非回环绑定（产品模式）且 Mongo 离线。
- **行为**：v2 文本 / `voice.commit` 幂等 lookup/claim 失败 → 
  `user_message.ack{status:rejected, error:{code:STORAGE_UNAVAILABLE, retryable:true}}`
  （不生成 messageId、不重复 generation）。
- **对比**：附件存储 Mongo 离线的 `STORAGE_UNAVAILABLE`（503）语义扩展到了幂等 claim。
- **客户端应对**：收到 `STORAGE_UNAVAILABLE` retryable 时，可稍后重试（同 clientRequestId）。

## 3. 新增：公开入口限流 `RATE_LIMITED`（此前目录登记无生产者）

- **REST**：高成本入口（`/api/providers/test`、`/api/uploads`、`/api/images`、
  `/api/search/*`、`/api/chat`）超限 → HTTP **429** + `Retry-After: 60`，
  `ProtocolError{code:RATE_LIMITED, retryable:true}`。
- **WS**：
  - 建连超限（`ws` 类别）→ 升级前 HTTP 429（握手失败）。
  - 消息超限（`text`/`voice.*`→messages、`switch_model`→provider_test、
    `tool_approval`→tools）→ `error{code:RATE_LIMITED, retryable:true,
    details:{retryHint:"请稍后重试"}}`，**连接存活**（不关闭）。
- **key**：认证主体（token）优先、匿名回退 IP；默认不信任 `X-Forwarded-For`
  （伪造代理头不改变限流 key）。
- **语义**：单进程 per-process 限流——多实例部署需自建共享后端，`RATE_LIMITED`
  不代表全局配额。
- **配置**：`config.yaml` `server.rate_limit_*_per_min`（0 关闭），env 覆盖
  `RATE_LIMIT_*_PER_MIN`。
- **默认**（config.yaml）：ws 120 / messages 120 / provider_test 30 / upload 60 /
  search 60 / tools 120（每分钟）。

## 4. 幂等表现维度变化（对客户端透明，供排障参考）

- 幂等键现按 `(conversation_id, client_request_id)` 维度（Mongo 唯一约束）；
  不同会话的同 `clientRequestId` 独立去重。
- claim TTL 1h（Mongo `expires_at` BSON Date 真 TTL）。

## 5. 无 wire 变更但需知晓

- 错误码目录 41 码单源化（`frontend_contract/errors.py`）——增删错误码走契约 + 文档同步 + CI。
- `test-all` 重定义为全仓非 external；Electron 仓库 CI 若跑后端契约 replay，应排除
  `external` 标记的测试（真实 LLM E2E，需受保护凭据）。

## 6. 加固批次：ACK 判别联合（BE-P1-04）

- **`user_message.ack` 改为 status 判别联合**：`AcceptedUserAckMessage` / `RejectedUserAckMessage`，schema 不再允许非法组合（accepted+空 messageId、accepted+error、rejected+非空 messageId、rejected+无 error 一律拒绝）。
- **`AcceptedUserAckMessage`**：`messageId: str (min_length=1)`、`clientRequestId: str (min_length=1)`，**无 error**。
- **`RejectedUserAckMessage`**：`messageId: Literal[""]`（D2：兼容前端批次 A）、`clientRequestId: str`（**无 min_length**——「缺少 clientRequestId」拒绝路径回显空串）、**`error: AckError`（必填）**。
- **`AckError`**（取代任意 dict）：`code/message/retryable/details?/retryAfterMs?`（顶层，与 ErrorMessage 同位置）；`details` 可携带 `idempotency_durable`（bool，STORAGE_UNAVAILABLE 时）。
- **负面 golden 增 6 帧**：`CONTRACT_VALIDATION` / `SESSION_NOT_FOUND` / `QUEUE_FULL` / `STORAGE_UNAVAILABLE` / `RATE_LIMITED` / **`IN_PROGRESS`**（§1.2 新增的 CLAIMED 新鲜窗口拒绝码）——均 rejected + `messageId:""` + AckError，进 `scripts/gen_contract_frames.py --check` 门禁。**跨仓承接**：Electron raw parser/mapper 须能 replay 这些负面帧（replay 完成并回填 runbook 后 BE-P1-04 方闭环）。
- **`ErrorMessage` 加可选 `clientRequestId` + 顶层 `retryAfterMs`**；6 个命令消息（switch_model / tool_approval / set_service / set_tts_voice / activate_session / wake_toggle）契约加可选 `clientRequestId`。

## 7. 加固批次：限流关联（BE-P1-03）与认证主体分桶（BE-P2-02）

- **text / `voice.commit` 超限 → 走 rejected ACK**（`messageId:""`、`clientRequestId=提取值`、`error{code:RATE_LIMITED, retryable:true, retryAfterMs}`）——**直接 settle 前端对应 promise，不再只发无关联 generic error**。
- **clientRequestId 缺失/非法**（text/voice.commit）→ 退化为 generic `ErrorMessage` RATE_LIMITED（前端对这两类恒发 clientRequestId，缺失即畸形帧）。
- **命令类超限** → `ErrorMessage` 带可选 `clientRequestId` + 顶层 `retryAfterMs`。
- **`retryAfterMs`**：`(capacity - tokens)/refill_per_sec * 1000`，下限 1000。
- **WS 建连 429 补 `Retry-After` 头**（与 REST 一致）。
- **限流 key 按认证主体分桶**：只有认证成功（与合法 token 集常量时间相等）才按 `auth:<sha256(token)[:16]>` 分桶；缺失/无效 Authorization 统一 `ip:<remote>`——同 IP 轮换任意假 Token 不再获得 per-token 配额、不能挤掉合法主体桶。WS 建连限流用同一分类器。

## 8. 加固批次：幂等表现维度更新（D6c 状态机，BE-P0-01/P1-01/P1-02）

- **claim 三态 `CLAIMED / ACCEPTED / REJECTED`**：完整 ACK 重放负载持久化（messageId / clientRequestId / clientMessageId / conversationId / receivedAt 原值 + rejected 的 error）。`complete()` 在 `process_message` 返回后、accepted ACK 之前——「process 后 ACK 前 kill」⇒ 记录 ACCEPTED ⇒ 重放 accepted，不重复生成。
- **claim 所有权（`ClaimResult.acquired`）**：并发同键 loser **恒不进入业务处理**（同内容重放原 ACK；异内容 rejected `CONTRACT_VALIDATION`；不串线、不重复 generation）。DuplicateKey 不再被误判为连接失败（repo 不判死）。
- **CLAIMED 残留恢复**：新鲜（≤30s）→ rejected `IN_PROGRESS`（retryable + retryAfterMs）；过期 + generation 证据（`find_by_user_message`）→ 重放 accepted；过期 + 无证据 → 接管重执行。**不再有幽灵 accepted**（残留一律不重放成 accepted）。
- **dev 内存权威 overlay + Mongo 写穿 + 恢复 reconcile**：进程内任意 Mongo 状态切换不去重（up→down→up→down 稳定）；`idempotency_durable=false` 仅表示「跨进程不保证」。
- **idempotency 入生命周期**：60s 健康探测、False→True 恢复、shutdown close；durable 初值由 `repo.is_connected` 决定（修复「repo 对象非空即 durable」误报）。

## 9. 加固批次：clean-start 会话发现（BE-P1-05）

- 全新数据目录下 `GET /api/sessions` 返回 `[default(active=true)]`；`GET /api/sessions/default` detail `updatedAt` 非空可解析；列表会话非空时**恒恰一个 active**；WS 首帧 conversationId / REST active / `agent.active_session_id` 同源。Electron 冷启动无需手工「新建会话」即可发送。

## 10. 加固批次：探针分层（BE-P2-01）

- **`/health` 纯 liveness**：零网络探针，恒 200，任何依赖挂起都在固定小预算内返回。
- **`/ready` = 核心 chat 门禁**：`chatReady`（= mongo 三 repo 并行 ping 且 product 模式时 idempotency ping）→ 200，否则 503；**Milvus 不门禁 HTTP**（读 `fullReady` 可观测）；payload 带 `status/chatReady/fullReady/dependencies`。Electron 探活：`/health` 只看存活，`/ready` 才是文本可用门禁。

## 回放路径

- 协议文档：`docs/electron-backend-protocol-v2.md`
- golden frames：`docs/archive/frontend-handoff-v2/traces/ws-v2-{handshake,session,chat,tool,voice,cancel,fail}.jsonl`
- 机器契约：`frontend_contract/contract.schema.json` + `contract.raw.ts`
- 发版记录：`docs/contract-release-2026-08-11.md`
