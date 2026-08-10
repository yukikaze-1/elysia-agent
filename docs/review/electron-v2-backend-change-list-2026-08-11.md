# Electron 后端契约变更清单（2026-08-11）

> F5 交接：本次发版（D6b/D6c 产品化门禁）对 Electron 前端的 wire 契约影响，逐条可回放。
> 前置批次 9A/9B/9C 的变更见 `docs/electron-backend-protocol-v2.md` + golden frames
> （`docs/archive/frontend-handoff-v2/traces/`）；本清单只列 **2026-08-11 新增**。

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

## 回放路径

- 协议文档：`docs/electron-backend-protocol-v2.md`
- golden frames：`docs/archive/frontend-handoff-v2/traces/ws-v2-{handshake,session,chat,tool,voice,cancel,fail}.jsonl`
- 机器契约：`frontend_contract/contract.schema.json` + `contract.raw.ts`
- 发版记录：`docs/contract-release-2026-08-11.md`
