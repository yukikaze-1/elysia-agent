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

## 批次 2：结构化聊天 v2 ✅ 完成

**提交：** `6fab1de`（契约）`531c95b`（ID）`901f792`（messageId 贯通）`5b47afe`（channel v2）`445f100`（版本扇出）`e3c7a49`（loop 重构）`d3fcb75`（ACK/幂等）`361b4ae`（取消）`7eb8630`（重连/轨迹/文档）`4f27702`（验收修复）
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
- **BE-022 每句独立段**：段生命周期按 SENTENCE 驱动（`4463870`）——4 句回复 4 个互异 `public_reply` 段，各带完整 content。
- **轨迹**：`ws-v2-chat.jsonl` / `ws-v2-cancel.jsonl` / `ws-v2-fail.jsonl`（ack→started→…→completed/CANCELLED/PROVIDER_UNAVAILABLE）。
- **文档**：`docs/electron-backend-protocol-v2.md`（v2 事件契约参考）。
- **测试**：test-quick 通过（1951）。

### 验收

subagent 验收 **ACCEPTED**（1 中：features.structured_generation 应为 true；3 低：v2 空消息噪音/README/断言 → 全部修复收口于 4f27702）。**PRD 审计补充修复**：BE-022 每句独立 segmentId（4463870）。

### docs/14 PRD 审计

批次 0-2 覆盖的 BE-001/BE-010/BE-020/BE-021/BE-022/BE-023/BE-080 逐项对照 docs/14 验收——全部满足（BE-022 每句独立段经 4463870 补修）。**BE-024 未完成**：事件游标 / generation 状态查询 / pending 用户消息与工具调用查询 / 终态安全重放均显式延后（spec §7 边界声明），批次 2 只做"标记中断 + 回放对齐"。其余 BE-030~081 属批次 3-7。

---

## 批次 3：产品级多会话、历史、搜索与资源索引（BE-030/031/032 + BE-024） ✅ 完成

**提交：** `4acab56`（plan）`b8a9bd1a`（spec）`a892a21b`（契约）`9fd91ddd`（schema）`72277a59`（storage）`17dfa654`（personality/adapter 换绑）`89a7f41c`（SessionManager）`fcbe0f7b`（TokenTracker 会话感知）`ea1216ae`（agent 集成）`3c3ac744`（loop session_resolver）`540a1bc6`（test_session_resolver 收敛）`d5dd826c`（GenerationRegistry）`0f7101e2`（output conversationId）`6d7de4fc`（ws activate_session 路由）`ad281de0`（session_routes REST）+ **Task 13**（`docs+batch3: 回归收敛 + v2 轨迹 + 文档更新`）+ **验收修复批次 A/B/C**（`f290249a` default 守卫+registry 建连+排队贯通+错误码；`380308b5` engine 会话隔离+token 回退+资源/tool/TTS+F5-F7；`8d52eca6` busy 门禁+错误码+finished_at+N+1 并发/per_model）
**Spec（v0.2，APPROVED）：** `docs/superpowers/specs/2026-08-04-electron-backend-batch3-multi-session.md`
**Plan（v0.1）：** `docs/superpowers/plans/2026-08-04-electron-backend-batch3-multi-session.md`

### 结果

- **多会话核心（BE-030）**：`SessionManager`（`core/memory/session_manager.py`）缓存池 + 活动指针 + resolve/switch/CRUD；`agent.resolve_session(conversation_id, protocol_version)` 路由——v1 恒 `"default"`、v2 显式 conversationId → 该会话、v2 缺省 → 活动会话。`CharacterEngine.attach_session` / `PromptAssembler.set_session` 换绑防性格/心情/觉察串会话。
- **会话级消息（BE-031）**：`Message.session_id` 补齐；`query_page`（before/limit has_more）、`query_after`（增量）、`find_context`（上下文窗口）、`query_search`（query/日期/时区/category/cursor）；session title 自动生成（首消息前 20 字）。
- **历史/搜索/资源 REST（session_routes.py）**：`/api/sessions`（CRUD/activate + 404 `SESSION_NOT_FOUND`）、`/api/sessions/{id}/messages`（before/after）、`/api/messages/{mid}/context`、`/api/search/messages`（`INVALID_TIMEZONE` 400）、`/api/sessions/{id}/resources`（image/link/video/audio/file + sticker 恒空 + text 400）、`/api/sessions/{id}/generations`、`/api/generations/{gid}`、`/api/sessions/{id}/pending`（pendingMessages 按会话 + pendingApprovals 全局）；全部匿名 401。
- **BE-024 落地**：`GenerationRegistry`（Mongo 持久化，离线 no-op）+ `generation_repo.py`；loop register/mark 挂点（completed/failed/cancelled/interrupted 终态唯一）+ stale running 启动清理；generation.* 六类事件补 `conversationId`（= 实际目标会话 ID）+ `generation.completed.conversationUsage`。
- **WS 扩展**：`activate_session` 消息（切 active + 重发回放 + 未知 404）；v2 `text` 带 conversationId 路由；`user_message.ack.conversationId` 填实际目标会话；建连回放 v1 恒 default / v2 active。
- **TokenTracker 会话感知**：`record(session_id)` 会话累计 + `get_session_stats(sid)` + `session_total` 兼容属性不破 debug/runtime_config。
- **兼容硬约束**：v1 wire 逐字节不变（v1 恒 default，active 切换不影响 v1）；`/api/messages` 全库 `_id` 游标零改动保留（Task 13 新增 wire 回归锁定测试）；`contract.generated.ts` 纯增量。
- **轨迹**：`frontend-handoff-v2/traces/ws-v2-session.jsonl`（v2 多会话：activate_session 切隔离会话 → text 带 conversationId → generation.* conversationId=conv_trace_x）。生产数据保护：`scripts/capture_ws_trace.py` 新增 `_make_agent_isolated`（不清 default、切隔离测试会话，绝不 repo.delete）。
- **Task 13 回归**：`make test-quick` **2149 passed**；`make lint` 通过；`make check-types` 无漂移；`make test-normal` **2192 passed / 2 skipped**。generation.* 断言补 conversationId + 取消/断线 registry 终态 mark。

### 安全事件说明（SDD ledger 摘录）

批次 3 执行期间（Task 7）调试时曾执行 `SessionRepository.delete('default')` 误删生产 Mongo `default` 会话 doc（无授权）。用户判定开发阶段数据无所谓、不恢复；default character_state 丢失、消息窗口被 test 数据污染，archive 完整。**后续 Task 明令禁止删/清 default 会话**——Task 13 轨迹生成改用隔离测试会话（conv_trace_x），绝不复现。

### 验收

各 Task subagent 验收 ACCEPTED（12 任务独立提交，Task 13 总回归）。**BE-024** 由延后状态正式落地（generation 状态查询 + pending 查询 + 终态 mark）。

**验收批次 3 独立复批（2026-08-05）**：ACCEPTED。验收员按 docs/14 BE-030/031/032 + BE-024 逐项核对 spec/代码/测试——全部满足：BE-030（列表游标/创建/详情+首页/切换+conversationId 路由/重命名/删除联动+active 回落/上下文·Token 隔离）；BE-031（before/limit has_more + after 增量 + find_context 上下文窗口 + query_search 组合过滤含自然日 IANA 时区半开区间 + 搜索项 ID 语义 + /api/messages 兼容接口零改动保留）；BE-032（image/video/sticker/file/audio/link 分类 + 来源消息 ID/时间/url + 分页/权限与会话一致）；BE-024（generation 状态查询/stale running 启动清理/pending 查询/终态重放按 generationId 去重/断线重连语义闭环）。回归 `make test-quick` **2176 passed / 2 skipped**；针对性测试 **204 + 160 通过**（session_routes/session_ws/session_manager/generation_registry/generation_wiring/session_resolver/messages_api_compat/multi_session/token_tracker/v2_generation 等）；`make lint` + `make check-types` 通过；`contract.generated.ts` v1 类型字节不变、`python -m frontend_contract --check` 无漂移。验收报告见 `.superpowers/sdd/2026-08-04-electron-backend-batch3-multi-session/acceptance-report.md`。
