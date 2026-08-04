# Electron 后端改造进度

**文档日期：** 2026-08-04
**依据：** `docs/14-Cyrene-后端配合改造任务清单-v0.1.md`、`docs/15-Cyrene-后端改造执行上下文与Prompt-v0.1.md`
**基线：** `1e62b58`（含 PRD 文档）

本文件记录各批次实施状态与验收结果。每批次完成 = 契约/实现/测试/文档闭环 + 本地提交。

---

## 批次 0：基线确认 ✅ 完成

**提交：** `fea1b8b`
**Spec/Plan：** `docs/superpowers/specs/2026-08-04-electron-backend-batch0-baseline.md` / `...plans/...batch0-baseline.md`

### 结果

- **基线**：HEAD `1e62b58`、branch `main`、工作树干净；`make test-quick` **1883 passed / 2 skipped / 58 deselected**；`python -m frontend_contract --check` 无漂移。
- **时间戳**：核心数据（消息/Token/会话）统一 UTC（`now_iso()` = `datetime.now(timezone.utc).isoformat()`）；naive `datetime.now()` 4 处均在展示/通知路径（`idle_monitor:120`、`loop:1044`、`sensory:51`、`tool/__main__:32`），不落库。交接资料"UTC/本地矛盾"消解。
- **事件面**：ServerMessage 18 成员 + Client→Server 6 类；无版本/ID/ACK 字段。完成靠 `state idle`、句子靠空 `message` 帧、错误靠 `❌` 文本（现状确认）。
- **消息/会话**：单活动会话（SessionMemory 单实例，session_id="default"）；MessageRepository 已按 session_id 索引 + `query_by_session` 存在（批次 3 利好）。
- **工具**：12 插件类，全部 Backend Runtime；沙盒 `read_file/write_file` 正确标注，无"误标 Desktop"。
- **认证/上传**：`SERVER_AUTH_TOKEN` 仅护 admin/debug；`/uploads/*` 匿名静态。
- **真实 WS 轨迹**：`scripts/capture_ws_trace.py` + `frontend-handoff-v2/traces/ws-chat.jsonl`（16 帧，session→capabilities→expression→thinking→typing→inner_voice→token_usage→expression→delta→speaking→message×4→idle）、`ws-cancel.jsonl`（15 帧，第一句 delta → interrupted → idle）、`ws-tool-approval.jsonl`（24 帧，executing→tool_approval→tool_done→idle）。空会话可重放、无敏感数据。
- **基线文档**：`docs/electron-backend-baseline.md`。

### 验收

subagent 验收 ACCEPTED（初 REJECTED：cancel 未真正打断 + 轨迹混入真实 DB 数据 → 修复后复批 ACCEPTED）。

---

## 批次 1：认证 / 版本协商 / 统一错误 ✅ 完成

**提交：** `3a7211a`（契约）`1fcafd9`（配置）`9852d65`（认证）`b4afe17`（版本）`e386626`（启动守卫）`3ff1a88`（限制）`812fa88`（错误信封）`1acd431`（安全文档）`ff18233`（验收修复）
**Spec/Plan：** `docs/superpowers/specs/2026-08-04-electron-backend-batch1-auth-version-error.md`（v0.2） / `...plans/...batch1-auth-version-error.md`（v0.2）

### 结果

- **产品认证（D2）**：`AGENT_ACCESS_TOKEN`（`ServerConfig.access_token`，`secret=True`，显式参数默认 `""`）护 `/ws` + 业务 `/api/*` + `/uploads/*`；匿名 401 `AUTH_REQUIRED`、错 token 401 `AUTH_INVALID`；无 token 环境全匿名（v1 兼容）。与 `SERVER_AUTH_TOKEN`（admin/debug）权限域分离。
- **版本协商（D3）**：WS 握手头 `X-Protocol-Version: 2` → v2 发 `server_info`（`core/version.py` `__version__="0.1.0"`，features 全 false 严禁超前声明，取代 capabilities）；缺省 → v1 发 capabilities 零改动。
- **统一错误（D7）**：`ProtocolError` 信封（code/message/retryable/requestId/error/details），`error`=message 兼容别名（v1 前端 `err.error` 不破坏），requestId 独立 UUID。错误码目录初始 12 码（`RATE_LIMITED` 仅入目录，限流器批次 2+）。
- **限制（D8）**：`max_request_bytes`（app 级 client_max_size，超限 413 `REQUEST_TOO_LARGE` 信封，Content-Length 前置防 handler 吞异常）+ `max_ws_connections`（并发超限 503 `CONNECTION_LIMITED`，retryable=true）。
- **启动守卫**：非回环绑定 + 无产品认证 → ERROR 显著阻塞告警（`warn_if_unsecured`，`run_server` 每轮调用）。
- **安全文档**：`docs/electron-backend-security.md`（双 token 权限域、Nginx TLS/WSS 反代示例、限制、已知缺口、敏感纪律）。
- **轨迹**：`frontend-handoff-v2/traces/ws-server-info.jsonl`（v2 握手：session → server_info → expression）。
- **测试**：test-quick **1911 passed**（新增 auth 14 + version 2 + limits 3 + config 4 + contract 3）；`make check-types` 通过；`contract.generated.ts` 纯增量（v1 类型字节不变）。

### 验收

subagent 验收 ACCEPTED（1 个中等问题 F1 CONNECTION_LIMITED retryable + 2 低优先 → F1/F2/F3 已修，F4 为批次 2 规划文档无害）。

### 批次 2 前置已就绪

`WebSocketChannel.protocol_version` 注入、`server_info`、统一错误信封（code 目录）均已交付，批次 2 可启动。

---

## 批次 2：结构化聊天 v2 ⏳ 实施完成，待最终验收

**提交：** `6fab1de`（契约）`531c95b`（ID）`901f792`（messageId 贯通）`5b47afe`（channel v2）`445f100`（版本扇出）`e3c7a49`（loop 重构）`d3fcb75`（ACK/幂等）`361b4ae`（取消）`待提交`（重连/轨迹/文档）
**Spec（v0.2，APPROVED）：** `docs/superpowers/specs/2026-08-04-electron-backend-batch2-chat-v2.md`
**Plan（v0.2，reviewed+fixed）：** `docs/superpowers/plans/2026-08-04-electron-backend-batch2-chat-v2.md`

### 结果

- **契约**：`user_message.ack` + 6 个 `generation.*` 事件 + `V2TextMessage`；`ServerMessage` 联合 26 成员；`contract.generated.ts` 纯增量（v1 类型字节不变）。
- **ID 三概念**：`msg_`/`gen_`/`seg_` 独立前缀 + `GenerationContext`（全局 segmentIndex 单调、段内 deltaSequence）。
- **messageId 贯通**：`UserInputData`/`_PendingMessage` 扩展；`process_message → process() → _think_loop → _build_turn_prompt` 参数贯穿（落库优先用预分配 ID）；assistant 父消息 ID 在 generation 开始预分配。
- **版本感知扇出**：`OutputChannel.protocol_version`；`OutputManager` 统一回合事件（turn_started/segment_started/text_delta/segment_completed/inner_voice/mood/turn_completed/turn_failed/turn_cancelled）双 wire 呈现——v1 复刻现状、v2 发 generation.*；v2 抑制 inner_voice command/token_usage/expression。
- **loop 重构**：`_dispatch_stream_event`/`_finalize_turn`/错误路径改统一发射 + `GenerationContext`；`token_usage` 对 v2 抑制。
- **ACK/幂等**：`IdempotencyStore`（进程内 + TTL）；缺 clientRequestId → rejected；重复同内容 → 重发原 ACK；不同内容 → CONTRACT_VALIDATION。
- **取消/终态唯一**：interrupt → `generation.failed{CANCELLED}`（v2），v1 无 ❌；错误路径已发 failed 不重发。
- **重连最小**：回放含稳定 messageId（ACK/completed 对齐）；v2 断线取消在途 generation。
- **轨迹**：`ws-v2-chat.jsonl` / `ws-v2-cancel.jsonl` / `ws-v2-fail.jsonl`（ack→started→…→completed/CANCELLED/PROVIDER_UNAVAILABLE）。
- **文档**：`docs/electron-backend-protocol-v2.md`（v2 事件契约参考）。
- **测试**：test-quick 通过（1949+）。

### 验收

待最终 subagent 验收。
