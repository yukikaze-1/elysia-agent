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
- **统一错误（D7）**：`ProtocolError` 信封（code/message/retryable/requestId/error/details），`error`=message 兼容别名（v1 前端 `err.error` 不破坏），requestId 独立 UUID。错误码目录初始 13 码（`RATE_LIMITED` 仅入目录，限流器批次 2+；完整累计目录见 protocol-v2 §9.1）。
- **限制（D8）**：`max_request_bytes`（app 级 client_max_size，超限 413 `REQUEST_TOO_LARGE` 信封，Content-Length 前置防 handler 吞异常）+ `max_ws_connections`（并发超限 503 `CONNECTION_LIMITED`，retryable=true）。
- **启动守卫**：非回环绑定 + 无产品认证 → ERROR 显著阻塞告警（`warn_if_unsecured`，`run_server` 每轮调用）。
- **安全文档**：`docs/electron-backend-security.md`（双 token 权限域、Nginx TLS/WSS 反代示例、限制、已知缺口、敏感纪律）。
- **轨迹**：`frontend-handoff-v2/traces/ws-server-info.jsonl`（v2 握手：session → server_info → expression）。
- **测试**：test-quick **1911 passed**（净增 28；新增 auth 17 + version 2 + limits 3 + config 4 + 契约测试）；`make check-types` 通过；`contract.generated.ts` 纯增量（v1 类型字节不变）。

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
- **错误码目录（批次 3）**：`SESSION_NOT_FOUND`(404)/`GENERATION_NOT_FOUND`(404)/`RESOURCE_NOT_FOUND`(404 消息缺失)/`INVALID_TIMEZONE`(400)/`CONTRACT_VALIDATION`(400)/`SESSION_PROTECTED`(409 default 禁删守卫)/`AGENT_BUSY`(409 忙时切换·删活动会话门禁，retryable)。完整表见 `docs/electron-backend-protocol-v2.md §9.1`。
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

---

## 批次 4：统一 Tool Runtime 与 Desktop 工具桥（BE-040~043） ✅ 完成

**提交：** `8cf9da11`（契约 tool.request/tool.status + ToolMetadata/ToolRegistryResponse/DesktopCallStatus）`8d996951`（ToolPlugin runtime/version/risk_level/output_schema + desktop 四插件）`bebd3817`（DesktopCallRepository）`9731a794`（DesktopToolBridge）`07d365dc`（ToolMetadata 导出 + definitions 运行时过滤 + tool_approval 版本门控）`56296cf1`（agent 集成：desktop_bridge 构造/注入/生命周期 + ToolAdapter 过滤）`1df8db34`（X-Client-Platform 握手 + tool.status 路由 + 负面隔离 + desktop_runtime=true）`1eacb550`（GET /api/tools + calls/{callId} + 附件上传持有者校验）`7f3d0347`（server_info 下发 connectionId）+ **Task 10**（`docs+batch4: 回归收敛 + 测试矩阵 + 协议/进度/安全文档`，本批次收尾）
**Spec（v0.2，吸收四角度 review 全部 findings）：** `docs/superpowers/specs/2026-08-05-electron-backend-batch4-desktop-tools.md`
**Plan：** `.superpowers/plans/2026-08-05-electron-backend-batch4-desktop-tools/`（Task 1-10 严格顺序）

### 结果

- **Runtime 感知 ToolRegistry（BE-040）**：`ToolPlugin` 增 `runtime/version/risk_level/output_schema`；12 个 backend 工具补 risk_level 标注（runtime=backend 继承默认零改动）；`ToolManager.registry_metadata()`（16 工具完整元数据）+ `definitions(filter_runtime)` LLM schema 运行时过滤 + `plugin_list` 元数据扩展（runtime/version/risk/permission/approvalAuthority/available）。
- **Desktop 工具桥（BE-041/043）**：`DesktopCallRepository`（`desktop_calls` 集合，Mongo + 进程内缓存，离线读走缓存幂等不失效，状态迁移守卫终态不可覆盖/offline→终态合法）+ `DesktopToolBridge`（幂等去重锁同参并发单投、先登记后投递防极速竞态、future 终态唯一、篡改拒绝立即 resolve、断连 pending 立即 UNAVAILABLE/accepted 挂起可重传、超时 expired、重连不自动重投）。
- **四 desktop 插件**（`core/tools/desktop/plugin.py`）：`desktop.file.select/read/write` + `desktop.screen.capture`（risk safe/medium/high/high，safety.request_timeout=120s 防 SafetyPipeline 30s 截断）；execute 委托桥；`desktop.command.run` 不注册。沙盒 `read_file/write_file` 仍 runtime=backend。
- **WS 协议（spec §5）**：`X-Client-Platform: electron` 握手（仅 v2，v1 伪冒隔离）→ 桥 connected + `server_info.connectionId` 下发 + `desktop_runtime=true`；`tool.request`（Server→Client 定向投递单 electron 连接）+ `tool.status`（Client→Server 六态判别）经现有 v2 WS。
- **注册表 REST（BE-042）**：`GET /api/tools`（16 工具元数据 + desktopAvailable 翻转）+ `GET /api/tools/calls/{callId}`（repo 枚举状态，expired 如实返回，未知 404 `CALL_NOT_FOUND`）；认证 401。
- **附件上传（spec §5.4）**：`POST /api/uploads/desktop/{callId}` 持有着校验（callId 存在/连接=target_connection/可收结果状态）→ 落盘 `/uploads/desktop/<callId>.<ext>` → `ToolResult.images` → `ToolResultMessage.content = [TextPart, ImagePart]`（截图进聊天，回放/搜索/资源自动吃到；mime 非图片/空兜底过滤）。
- **tool_approval 版本门控（BE-042）**：enrich（summary/runtime/version/riskLevel/permissionRequired/approvalAuthority）仅 v2 生效、v1 收原样 `{tool, request_id, params}` 逐字节不变；desktop 工具不走 tool_approval（approvalAuthority=client）。
- **错误码目录（批次 4）**：`DESKTOP_UNAVAILABLE`(503, 目录登记无信封生产者)/`CALL_NOT_FOUND`(404, 落地于 calls/{callId}+附件上传)/`CALL_EXPIRED`(408, 目录成员语义在桥内)/`CALL_IDEMPOTENCY_CONFLICT`(409, 目录成员语义在桥内篡改拒绝)。完整表见 `docs/electron-backend-protocol-v2.md §9.1` + §10 Desktop 工具契约节。
- **Task 10 测试收敛**：新增多轮 tool-calling fake LLM（`_ToolCallingLLM`，首轮 tool_calls + 结果后文本回复，pytest 可导入）+ `_inject_fake_storage` 注入 DesktopCallRepository（`agent._desktop_call_repo` + `agent.desktop_bridge._repo` 双引用同步）+ service 往返/断连重连/幂等/篡改全矩阵（`test_desktop_roundtrip.py` 6 测）+ LLM 暴露过滤双断言（definitions + plugin_list，`test_tools_api.py`）。spec §11 矩阵逐项收敛（握手/负面隔离/版本门控/注册表/附件/ImagePart/feature 由 Task 7/8/9 已覆盖）。
- **回归**：`make test-quick` **2358 passed / 2 skipped / 87 deselected**（新增 182）；`make lint` 通过；`make check-types` 无漂移（`contract.generated.ts` v1 类型字节不变）。

### 验收

docs/14 场景 4（BE-040~043 门槛：四个 Desktop P0 工具完成安全往返）逐项满足：① electron 连接 → `GET /api/tools` 拉到 4 desktop 工具元数据；② LLM 选 `desktop.file.read` → 建 callId + 定向投递 `tool.request`（paramHash/expiresAt 完整）；③ `accepted→running→result` → Agent 拿 ToolResult + 截图附件上传 → 落盘消息含 ImagePart；④ backend write_file 审批 `tool_approval` v2 enrich/v1 原样、approvalAuthority=backend、desktop 不回退；⑤ 同参并发单投复用 callId、篡改拒绝及时失败、断连 pending 立即 UNAVAILABLE/accepted 标 offline 可查、重连重传 result → succeeded、`GET /api/tools/calls/{callId}` 可查；⑥ `desktop.command.run` 不在注册表、沙盒 read/write 仍 runtime=backend。Task 10 自检：测试矩阵与 spec §11 逐项对齐、错误码文档与实现一致、progress 批次 4 完整。

**验收批次 4 独立复批（2026-08-05）**：ACCEPTED。验收员按 docs/14 BE-040/041/042/043 逐项核对 spec/代码/测试——全部满足：BE-040（`ToolPlugin.runtime/version/risk_level/output_schema` + 派生 `approval_authority`/`permission_required` + `ToolManager.registry_metadata`/`definitions(filter_runtime)`/`plugin_list` 元数据扩展；12 backend 工具 runtime=backend 零改动、沙盒 read_file/write_file 不伪装 Desktop）；BE-041（`DesktopCallRepository` Mongo+进程内缓存+迁移守卫、`DesktopToolBridge` 幂等去重锁+先登记后投递+future 终态唯一、四 desktop 插件 execute 委托桥、`tool.request`/`tool.status` 六态判别、`X-Client-Platform: electron` 握手 + `server_info.connectionId` + `desktop_runtime=true`、附件 `POST /api/uploads/desktop/{callId}` 持有者三重校验 → `ToolResult.images` → `ImagePart` 落盘）；BE-042（tool_approval enrich 版本门控 v2 收 enrich/v1 收原样逐字节不变、approvalAuthority=backend/client 派生规则、`str(params)[:200]` 保留为兼容摘要、`GET /api/tools` 认证注册表导出、无代码串执行字段、`desktop.command.run` 不注册）；BE-043（同参并发去重单投复用 callId、篡改拒绝 mark failed+立即 resolve、断连 pending 立即 UNAVAILABLE/accepted→offline 可查、重连重传 result → succeeded、`GET /api/tools/calls/{callId}` 状态查询含 expired 如实返回、终态不可覆盖、重连不自动重投）。回归 `make test-quick` **2362 passed / 2 skipped / 87 deselected**、`make test-normal` **2410 passed / 2 skipped / 39 deselected**；`make lint` 通过；`make check-types` 无漂移；`contract.generated.ts` v1 类型字节不变（纯增量 154 行）、`python -m frontend_contract --check` 通过。验收报告见 `.superpowers/sdd/2026-08-05-electron-backend-batch4-desktop-tools/acceptance-report.md`。

---

---

## 批次 5：模型 Profile 与密钥所有权 / TTS 声音（BE-050/051） ✅ 完成

**提交：** `b0c22e6`（Secret Store）`50ea95a`（SSRFGuard）`7f77d4b`（契约层）`9b4c174`（custom_providers 解析统一）`8980d07`（后端 http_client 注入）`c8eb1f8`（generation.model 捕获）`4b6749e`（providers REST 三 handler）`a2adc09`（switch 路径守卫）`3af4201`（TTS 协议 list_voices + synthesize voice_id）`6099c2a`（POST /api/tts/preview）`f647357`（声音覆盖 service_state.tts_voice + WS set_tts_voice）`a0f8dfe`（fix：每 turn 声音注入防御性 getattr）
**Spec（v0.3）：** `docs/superpowers/specs/2026-08-05-electron-backend-batch5-model-profile-keys-voices.md`
**Plan：** `docs/superpowers/plans/2026-08-05-electron-backend-batch5-model-profile-keys-voices.md`（12 Task）

### 决策（10 项拍板，2026-08-05）

Secret Store 密钥（spec §10 推荐路径）+ 0600 明文不落 DB + api_key_env 并存不迁移 + Profile `runtime` 执行位置（Backend 管密钥/Desktop 归 Electron）+ SSRF 创建校验+出站再校验 + TTS 声音静态声明+MiniMax 动态拉取 + 新 `POST /api/tts/preview` 试听 + Generation `model` 字段（本条回复实际模型）+ 激活拒删 + `service_state.tts_voice` 运行时覆盖。完整决策表见 spec §2。

### 结果

- **Secret Store（BE-050）**：`core/storage/secret_store.py`（`data/secrets/profiles.json`，目录 0700 + 文件 0600 + gitignore + 原子写 + 损坏容错）；`ProjectPath.DATA_SECRETS`。密钥**只写不可读**——响应/日志只出 `credentialConfigured` 布尔。
- **SSRFGuard（BE-050）**：`core/input/ssrf.py`——`validate_endpoint`（scheme 缺省 https + 字面量黑名单 + DNS 解析双保险，拒私网/元数据/回环/链路本地/保留/多播/CGNAT，空串放行）+ `build_guarded_http_client`（校验传输层每跳校验 + 跨源重定向剥敏感头 + Header 允许名单）。作用于保存/切换/测试三入口 + `/api/providers/test` body base_url 无论 provider 一律校验（堵死任意拨号内网通道）；custom backend 出站注入（openai `http_client=` + httpx transport）。内置 provider 豁免。
- **Profile 契约**：`ProviderEntry`/`CustomProvider` 增 `credentialConfigured`/`runtime`/`capabilities`；`custom_providers.yaml` 条目支持 runtime/能力字段，api_key 入 Secret Store 不入 YAML（同名 upsert 保密钥，runtime 变更清理 D18）。REST 三 handler：保存（runtime 门控 base_url + SSRF + api_key 入 Store）/ 删除（激活 409 `PROFILE_IN_USE` + 清密钥）/ 测试（SSRF + 非 backend 400 `PROFILE_RUNTIME_NOT_BACKEND`）。WS switch 路径守卫（runtime 拦截 + SSRF + 出站注入 + 剥离 runtime 透传）。错误信封双轨：新码 ProtocolError / 老参数错误 v1 ApiFail。
- **本条回复实际模型（D8）**：`GenerationContext.model_id` turn 启动快照 → `generation.started/completed/failed` 携带实际模型（`completed.model` 保持必填；registry 已存 model 零改动）。
- **TTS 声音（BE-051）**：`TTSBackend.list_voices()`（MiniMax `/v1/get_voice` 动态 + 进程内缓存 / gpt-sovits 静态 = 已加载 YAML keys / custom OpenAI 静态集）+ `synthesize(..., voice_id=None)`（跳过 character 映射）；`GET /api/tts/voices` + `POST /api/tts/preview`（固定短文本，不读聊天）+ WS `set_tts_voice`（活动会话 + `list_voices` 校验 + 失败保留原选择）+ 每 turn 由发起会话 `service_state.tts_voice` 注入 TTSStreamManager。
- **错误码目录（批次 5）**：+`SSRF_BLOCKED`(400)/`PROFILE_IN_USE`(409)/`PROFILE_INVALID_RUNTIME`(400)/`PROFILE_RUNTIME_NOT_BACKEND`(400)——目录累计 25 码。完整表见 `docs/electron-backend-protocol-v2.md §9.1` + §11。
- **回归**：`make test-quick` **2484 passed / 2 skipped / 87 deselected**（批次 5 净增 ~121）；`make lint` 通过；`make check-types` 通过；`frontend_contract --check` 无漂移（`contract.generated.ts` 纯增量，v1 未改类型字节不变）。

### 验收

**批次 5 验收（2026-08-05，双路并行）**：

- **docs/15 交叉验收（批次 5 门槛 + Review 要点）：ACCEPTED**。三 Profile 安全新增/测试/切换（runtime 门控 + SSRF + 守卫工厂 + 启动路径守卫）、普通查询永不读回密钥（遍历响应/错误/日志全路径）、敏感纪律、v1 读取兼容、诚实性（desktop/local 不造假 + TOCTOU 取舍声明）、文档闭环、测试覆盖全部满足。
- **docs/14 BE-050/051 逐项验收：ACCEPTED_WITH_ISSUES**。10 项清单全部满足；F1（save/delete 响应 legacy api_key 回显加固）→ 已提交 `_strip_secret_keys`；F2（save 响应 credentialConfigured 文档漂移）→ docstring + protocol-v2 §11 措辞对齐；F3/F4 记录在案不修（见下）。
- 回归门禁：`make test-quick` **2495 passed / 2 skipped / 87 deselected**；`make lint` 通过；`make check-types` 通过；`frontend_contract --check` 无漂移（`contract.generated.ts` 纯增量，v1 未改类型字节不变）。

**记录在案不修（设计固有/显式取舍）**：
1. **`_active_tts_profile` 返回全局后端，不随激活会话派生**（F3）：`activate_session` 切会话不重派后端——voices/preview/set_tts_voice 作用于全局单 TTS 后端（批次 3 单后端架构固有；单会话/活动会话路径一致）。
2. **跨会话 voice 注入潜在不匹配**（F4）：set_tts_voice 校验于活动会话后端、每 turn 注入按发起会话——显式 conversationId 指向非活动会话且后端已切换时 voice 可能不在目标列表（TTS 合成失败但文本不受影响；spec §4.3 已明示 per-session voice 设计）。
3. **LLM 初始配置 custom 无出站守卫**（review 记录）：`LLMAdapter.__init__` 不解析 custom 参数（config.llm.model=custom/x 属既有限制，批次 5 前已存在）；运行时切换路径（switch_llm）有守卫。
4. **save/update 无 in-use 约束**（D9 只守卫 delete）：backend→desktop 更新活动 profile 会清密钥而内存后端继续运行至重启/切换（与 spec 一致）。

**验收批次 5 独立复批后续**：docs/14 BE-050/051 已标 ✅（2026-08-05）。

---

## 全批次独立复批（2026-08-05，批次 0-4 覆盖全部 15 项已完成 BE 任务）

**验收方式**：6 路并行 subagent（批次 0/1/2/3/4 各 1 + 1 路跨批次 docs/15 Review 要点审查），全部静态核对（未跑 pytest/未写 Mongo/未启服务/未改文件），回归门禁由协调者另跑。报告见 `/tmp/electron-batch-acceptance/batchN-acceptance.md`（0/1/2/3/4）+ `batchX-crosscutting.md`。

### verdict

| 验收 | verdict | 结论 |
|---|---|---|
| 回归门禁（test-quick/lint/check-types/契约漂移） | ✅ | **2362 passed / 2 skipped / 87 deselected**，lint/check-types 通过、契约无漂移 |
| 批次 0 BE-001 | ACCEPTED_WITH_ISSUES | 55/55 帧契约校验通过、UTC 时间戳、12 工具全 backend、批 0 未扰动测试基线 |
| 批次 1 BE-010/020/080 | ACCEPTED_WITH_ISSUES | 匿名 401、权限域分离、错误信封、v1 契约 553 行纯增量 |
| 批次 2 BE-021/022/023 | ACCEPTED_WITH_ISSUES | 幂等/每句独立 segmentId/终态唯一/v1 wire 逐字节 |
| 批次 3 BE-030~032/024 | ACCEPTED | 两会话隔离、分页不重不漏、重连恢复闭环 |
| 批次 4 BE-040~043 | ACCEPTED_WITH_ISSUES | 状态迁移表/幂等去重锁/审批门控/场景 D/E |
| 跨批次 Review 要点（docs/15） | ACCEPTED_WITH_ISSUES | 无 v1 破坏、无泄漏、无造假、8 轨迹全契约 |

**全部 15 项已完成 BE 任务满足 docs/14 + docs/15 验收**，无 critical/important 缺口。

### 本轮顺手修复（低风险，随本记录提交）

1. **畸形 before 游标 → 400**（批次 3 MINOR #1）：`session_routes.sessions_messages_handler` 加 `ObjectId` 合法性校验，`InvalidId` → 400 `CONTRACT_VALIDATION`（原 500）。TDD：新增 `TestMessagesHistory::test_malformed_before_cursor_400`，先 FAIL 后 PASS；`test_session_routes.py` 67 测全绿。
2. **protocol-v2 §9.1 错误码目录补全**（跨批次 LOW #1）：目录由批次 3 的 12 码扩为批次 1-4 累计 17 码（补 AUTH_REQUIRED/AUTH_INVALID/RATE_LIMITED/CONNECTION_LIMITED/REQUEST_TOO_LARGE/INVALID_REQUEST/INTERNAL/GENERATION_PROVIDER_UNAVAILABLE/GENERATION_TIMEOUT），标题改「批次 1-4 累计」。
3. **README/docs-README 批次号**（批次 4 INFO #5 = 跨批次 INFO #5）：「批次 0-3」→「批次 0-4」，protocol-v2/security 行补批次 4。
4. **progress 批次 1 计数**（批次 1 LOW #1/#3 = 跨批次 LOW #3）：错误码 12→13 码；测试数「auth 14 + …」→「净增 28；auth 17 + …」。
5. **baseline §6/§10 划销注记**（批次 0 MINOR #2）：标注批次 0 认证/上传现状已被批次 1 `AGENT_ACCESS_TOKEN` 取代。
6. **traces README 历史快照注记**（批次 2 LOW #3 = 跨批次 LOW #2）：`frontend-handoff-v2/traces/README.md` 注明各轨迹捕获时点 + features 快照性质 + 帧数表。

### 记录在案不修（spec 显式范围外 / 设计固有 / 加固建议 / 覆盖增强）

- 批次 0 MINOR #1：baseline 行号随批次漂移（快照文档正常现象）。
- 批次 1 LOW #2：spec §3.3「415 图片类型映射 INVALID_REQUEST 本批实现」过报——`/api/images` 415 走 v1 `ImageUploadError`（兼容硬约束使然，归档 spec）。
- 批次 2 LOW #1：EmotionEngine 独立 `expression` 仍广播 v2（plan Task 5 明示照旧直发，记录在案）。LOW #2：CJK 持有上界致首段 delta 携带后续句（`segment.completed.content` 为准，仅 live-delta 显示保真）。INFO #4：ACK `clientMessageId` 回显无直接断言。INFO #5：v1 回归为帧类型断言非字节级（翻译层已静态比对）。
- 批次 3 MINOR #2：搜索畸形日期吞空返回空结果（符合 spec 字面，只强制 INVALID_TIMEZONE 400）。MINOR #3：v2 text 未知 conversationId 懒加载静默建会话（设计取舍，404 仅 activate/CRUD/history 强制）。
- 批次 4 LOW #1：重连后携带附件的 result 重传被持有者校验 403 拦截（spec §5.4 未定义 target_connection 重连更新机制，范围外）。LOW #2：tool.status 未校验发送连接==target_connection（桥四字段篡改校验兜底，加固建议）。LOW #3：`_locks` 无界增长（设计固有）。LOW #4：断连快照竞态（极窄窗口，无状态机破坏）。
- 跨批次 INFO #4：`transcription` 仍自动提交触发 Agent（批次 6 场景 F 待隔离的既有行为，批次 0-4 诚实未做）。

---

## 批次 6：三种语音模式 + TTS 播放控制（BE-060~063） ✅ 完成

**提交：** `09bca10`（契约，Task 1）`9b10adb`（VoiceSessionManager，Task 2）`32db89c`（ws_handler 路由，Task 3）`2955010`（voice.commit v2 提交，Task 4）`c53e42d`（实时会话 service 层，Task 5）`854aa9c`（dictation stt_emotion 隔离，Task 6）`46d6201`（输出层 audio 控制，Task 7）`0e4212e`（adapter 层 audio 控制，Task 8）`00f71d2`（features + audio.cancel 路由 + 批次 6 文档，Task 9）`de36c37`（全量回归收口：test_paths 枚举 23 成员——批次 5 DATA_SECRETS 遗留断言漂移）+ 最终 review 修复 `27c4546`（会话 JSON 入口 stt_enabled 门控 + 后台循环异常捕获 + cancel_all 剪除）+ `61e2727`（spec 微修：RECORDING deafen 消歧 + 断连边界记录）+ `1836d6b`（idle 超时 ended 事件丢失——teardown 不自取消当前任务）
**Spec：** `docs/superpowers/specs/2026-08-05-electron-backend-batch6-voice-modes-tts-control.md`（v0.7）
**Plan：** `docs/superpowers/plans/2026-08-05-electron-backend-batch6-voice-modes-tts-control.md`（10 Task）

### 结果

- **契约（Task 1）**：`frontend_contract/ws_message.py` 新增 24 个消息契约类——11 Server→Client 入 `ServerMessage` 判别联合（`VoiceTranscript`/`VoiceSessionStarted`/`Partial`/`Turn`/`Ack`/`Ended` + `DictationPartial`/`Final`/`Cancelled` + `AudioStart`/`AudioCancelled`，另 `AudioMessage` 增 `is_final`），13 Client→Server（`voice.record.start/stop/commit/discard` + `voice_session.start/pause/resume/stop/cancel` + `dictation.start/stop/cancel` + `audio.cancel`）；`ServerInfoFeatures` 增 `voice_recording`/`voice_realtime`（Task 1 仅加字段默认 false，Task 9 置 true）。`ServerMessage` 联合 28→39 成员。
- **VoiceSessionManager（Task 2）**：`core/input/voice_sessions.py` 三模式状态机（IDLE/RECORDING/REALTIME/DICTATION）——RECORDING 四态（RECORDING→TRANSCRIBING→PENDING→commit/discard）、realtime/dictation 每会话一个 VAD `AudioBuffer`（dictation `on_interrupt=None` 隔离，review NEW-K/R）+ partial/turn/ack/idle 后台循环 + 错误码目录（`VOICE_SESSION_CONFLICT`/`VOICE_SESSION_NOT_FOUND`/`VOICE_RECORDING_NOT_FOUND`/`VOICE_RECORDING_TOO_LONG`/`DICTATION_NOT_FOUND`/`STT_UNAVAILABLE`/`CONTRACT_VALIDATION`）+ 空转写不进 PENDING + 录音 600s 上限。
- **ws_handler 路由（Task 3）**：BINARY 帧按「活动语音会话 → `voice_mgr.feed_pcm()`，否则 legacy `AudioBuffer.feed()`（逐字节不变）」分流；12 条新消息路由分支（`voice.record.*`/`voice_session.*`/`dictation.*`）+ v2-only 门禁（`_is_v2`，v1 忽略不发错）+ `_send_voice_error` 信封 + `set_service('stt', False)` 成功 → `notify_stt_disabled`（结束活动 REALTIME/DICTATION）+ 断连 finally `voice_mgr.close()`。
- **voice.commit v2 提交（Task 4）**：复用 `_route_v2_text` 幂等 + ACK 模式——缺 clientRequestId → rejected；幂等表命中 → 重发原 ACK（无视状态机门禁）；未命中 → PENDING 门禁 → 预分配 messageId + post `UserInputData`（emotion/events 快照随行，不重转写）。
- **dictation 隔离（Task 6）**：转写前快照共享 `stt_emotion_state`（dict 复制）、结束后原地 clear/update 恢复（S5）——stop/cancel/close/STT 中途关闭（`notify_stt_disabled`）全路径恢复。
- **TTS 播放控制（Task 7/8）**：`OutputManager.audio_start/audio_cancelled` 广播（capability=AUDIO）+ `WebSocketChannel.on_audio_start/on_audio_cancelled`（`_audio_seq.pop` 防残留）+ `on_audio_chunk` 改 `AudioMessage` 契约建模（修 `is_final` 漂移）；`TTSStreamManager._wav_info` 解析 WAV sampleRate/durationMs + `stream_sentence` Phase 2 放行后发 `audio_start` + `cancel_stream(stream_id) -> bool` + `cancel_all` 补 `audio_cancelled`（修中断孤儿 chunk）+ `Agent.tts_manager` 公开访问器（`agent.tts_manager.cancel_stream` = `audio.cancel` 入口）。
- **Task 9（本 commit）**：`build_server_info` features 置 true（`dictation`/`voice_recording`/`voice_realtime`，spec §9.3 如实声明）；ws_handler `audio.cancel` 路由——缺/非数字 stream_id → `CONTRACT_VALIDATION`、`tts_manager` 缺失或 `cancel_stream` False → `PLAYBACK_NOT_FOUND`；文档闭环（protocol-v2 §12 + §9.1 错误码 25→32 + §1 features；security §3.4 语音临时音频不落盘）。
- **错误码目录（批次 6）**：+`VOICE_SESSION_CONFLICT`(409)/`VOICE_SESSION_NOT_FOUND`(404)/`VOICE_RECORDING_NOT_FOUND`(404)/`VOICE_RECORDING_TOO_LONG`(413)/`DICTATION_NOT_FOUND`(404)/`STT_UNAVAILABLE`(503, retryable)/`PLAYBACK_NOT_FOUND`(404)——目录累计 32 码。完整表见 `docs/electron-backend-protocol-v2.md §9.1` + §12。
- **测试（Task 9 自检）**：`core/input/tests/` **328 passed**（含 `test_voice_ws.py` 12 测、`test_voice_sessions.py` 13 测、`test_version.py` 2 测——features 精确断言已随 Task 9 同步为全 true）；`make test-quick` **2533 passed / 2 skipped / 87 deselected**（Task 10 全量回归另行收口）。

### 验收

**✅ 2026-08-06 双 agent 验收通过：**

- **docs/14（BE-060~063 + 场景 F）**：ACCEPTED。普通录音 stop→预转写→PENDING 只发 `voice.transcript` 不触发 Agent，commit 经幂等+ACK+UserInputData 后才生成；实时语音 VAD 段 partial+turn 自动提交 + 背压 ack + 暂停/取消/300s 超时 + stt 中途关闭；输入法转写零消息/零 UserInputData/零 Agent，emotion 快照全路径恢复；TTS stream_id=playback ID + audio_start/audio_cancelled/audio_end + audio.cancel；场景 F 三模式取消均不误发消息。批次 6 测试 35 passed + core/input+Module/output 421 passed + 契约门禁零漂移。
- **docs/15（批次 6 门槛）**：ACCEPTED。三套独立 Session/命令（互斥 + `_require_session` 隔离）；普通录音 commit 语义、实时语音序号/背压/partial/final/VAD/轮次/暂停/取消、输入法零副作用；TTS playback ID/音频块 seq/格式 wav/取消/完成；**Inner Voice 不进入 TTS 抽查确认**（`loop.py:1296` `stream_sentence` 仅 SENTENCE 分支触发，INNER_VOICE 走 `output.inner_voice`）；三模式分别自动化测试 35 全绿；取消不误发消息。

**记录在案不修（批次 6）：**
- 断连在途段半途消息（spec §3.2 记录）：`feed_pcm` 在 ws 串行循环内 await 段 STT，aiohttp 不取消在途 handler——断连发生在 STT 窗口时，STT 完成后 `_on_segment` 先 post UserInputData（落半途消息），随后 finally `InterruptData(source="disconnect")` 取消该 generation。架构固有限制，窗口概率低，Demo 可接受。
- realtime「partial/final」为**分段级**伪流式（VAD 分段字幕 + 轮次提交），非词级增量——非流式 STT 架构下的合理解释，spec P6-3 明示。
- 背压「最大未确认块」为客户端窗口（`MAX_UNACKED=200`），服务端仅发 ack 不下发硬性限流（spec §5.3 明示设计）。
- RECORDING 超长丢弃后残余 BINARY 回落 legacy 路径（超限边界）。

---

## 批次 7：通用附件与媒体 / 版本与可观测性（BE-070/071/081） ✅ 完成

**提交：** `6e0cadf`（契约层——媒体 Part/附件模型/签名响应/stickerId/build_sha/media_upload/requestId）`96dcb4e`（存储层——AttachmentRepository fail-closed + AttachmentFileResolver + agent.attachment_repo 归属）`b688412`（通用上传 /api/uploads + filetype 真值校验 + 签名 URL + 64MB/静态收敛）`35b182d`（附件签名下载 + Range + 签发端点 /sign + fail-closed/fail-open）`366cadb`（内置表情包——sticker_registry + /api/stickers）`19391f6`（附件/sticker 消息关联——v2 wire + process bool + refs + 删除联动含归档 + 视觉适配）`8fc909b`（启动 GC 兜底扫描——deleted/孤儿/未引用清理，限四 kind 目录）`5497e49`（版本——git build_sha + media_upload 如实声明）`158c633`（/health//ready 拆分 + 真依赖检查 + 版本/uptime/request_id）`38dde3a`（日志正文打印收敛 + 敏感扫描测试）`59d0927`（关联 ID——WS 错误带 requestId + GenerationRegistry 记 request_id）
**Spec：** `docs/superpowers/specs/2026-08-06-electron-backend-batch7-media-and-observability.md`（9 轮 review 收口）
**Plan：** `docs/superpowers/plans/2026-08-06-electron-backend-batch7-media-and-observability.md`（4 轮 review 收口）

### 结果

- **契约层（Task 1）**：`ContentPart` 判别联合 6 类（text/image/file/audio/video/sticker），Image/File/Audio/Video Part 加 `attachment_id`；`rest.py` 新增 `AttachmentInfo`/`AttachmentUploadResponse`/`AttachmentSignResponse`/`StickerEntry`/`StickerListResponse`；`V2TextMessage` 加 `attachments`（`AttachmentRef`）与 `stickerId`；`ServerInfoFeatures.media_upload` + `ServerInfoMessage.build_sha`；`ErrorMessage.requestId`（WS 侧请求 ID，P7-12）。
- **附件存储（Task 2）**：`core/storage/attachment_repo.py`（`attachments` 集合，**fail-closed**——Mongo 离线所有读写 raise `StorageUnavailableError` → 503 `STORAGE_UNAVAILABLE`；schema：attachment_id/kind/mime/size/filename/stored_name/sha256/ref_count/refs/status/deleted_at；索引 attachment_id unique + status + ref_count + created_at，TTL 不设由 GC 管）+ `AttachmentFileResolver`（进程内 ID→路径映射，图片喂 LLM 转 data URI）+ `agent.attachment_repo` 归属。
- **通用上传（Task 3）**：`POST /api/uploads`（multipart `files[]` + 可选 `kind` 客户端意图不信任）——filetype 魔数白名单（png/jpeg/gif/webp/mp4/webm/wav/mp3/ogg/pdf/zip）+ 扩展名一致性 + kind 归位（webm tiebreaker）+ 大小复核（image 10MB / video 50MB / audio 20MB / file 20MB）+ 落盘 `data/uploads/{kind}/{uuid}.{ext}` + sha256 + `attachments` 记录（离线删文件不留孤儿）+ `AttachmentFileResolver.register`；整体聚合 64MB（`client_max_size` + payload 中间件 Content-Length 预检）；filename 脱敏（去分隔符/截 255/剥控制字符）；响应 `signedUrl` fail-closed 省略。`/uploads/` 静态挂载**收敛**为仅 legacy images/desktop 子目录（新 kind 目录不静态暴露）。
- **签名下载 + 签发（Task 4）**：`GET /api/attachments/{id}`（免 Bearer，HMAC 短签授权）校验序 sig 合法 hex → 记录 active → exp 窗口 → `compare_digest` 验签，成功走 aiohttp `FileResponse`（原生 Range/206）；fail-open（token 空）跳过签名仅校验 active；`GET /api/attachments/{id}/sign`（Bearer）返回 `AttachmentSignResponse`；`ATTACHMENT_SIG_SECRET` fail-closed（token 配 + sig 未配 → 一律 403 `ATTACHMENT_SIGNATURE_DISABLED`）。
- **内置表情（Task 6，BE-071）**：`core/input/sticker_registry.py`（启动扫描 `static/stickers/*.png`）+ `GET /api/stickers`（`StickerListResponse`）；sticker 为内置公共资源（公开 `/static/stickers/{id}.png`），非附件（不落集合/不 ref_count/不 GC/不上传白名单，kind=sticker → 400 `CONTRACT_VALIDATION`）。
- **消息关联（Task 5）**：v2 `attachments`/`stickerId` wire + `process_message` 返回 bool（附件/sticker-only 消息触发生成）+ ACK 延后（accepted 才 `add_ref`；队列满 → rejected `QUEUE_FULL` 重试允许）+ 删除联动（消息删除 `remove_ref` refs 归零软删，含归档）+ 视觉适配（图片经 resolver 内存映射喂 LLM；File/Audio/Video/Sticker 不喂 LLM）。
- **GC（Task 7）**：`core/storage/attachment_gc.py` 启动兜底扫描四规则——① 软删超 24h 删文件+记录；② active 记录文件缺失软删；③ ref_count=0 超 7 天软删；④ 磁盘孤儿删文件；Mongo 离线 skipped。
- **版本（Task 8，BE-081）**：`core/version.py` `get_build_sha()`（git rev-parse HEAD，模块级缓存）+ `build_server_version()`（`0.1.0+sha7`）；`server_info.build_sha` + `server_version` 后缀 + `features.media_upload=true`。
- **/health//ready 拆分 + 可观测（Task 9）**：`/health`（liveness 恒 200，status ok/degraded）+ `/ready`（readiness，依赖不 ok → 503）；探针负载含真依赖检查（mongo/milvus，2s 超时）+ fsm/llm/memory/tools/version/build_sha/uptime/request_id。
- **日志收敛 + 关联 ID（Task 10/11）**：`ws_handler`/`loop` 正文打印收敛（只记长度/ID）；`conversation.log` **明文保留**（调试审计通道，文档标注风险）；新增「日志敏感扫描」测试；`ErrorMessage.requestId` + `GenerationRegistry` 记 `request_id`。
- **错误码目录（批次 7）**：+`ATTACHMENT_NOT_FOUND`(404)/`SIGNATURE_EXPIRED`(403)/`INVALID_SIGNATURE`(403)/`UNSUPPORTED_MEDIA_TYPE`(415)/`TOO_MANY_FILES`(400)/`ATTACHMENT_SIGNATURE_DISABLED`(403)/`STORAGE_UNAVAILABLE`(503, retryable)/`QUEUE_FULL`(429, retryable，WS ACK rejected)——目录累计 **40 码**。完整表见 `docs/electron-backend-protocol-v2.md §9.1` + §13。
- **测试**：批次 7 新增针对性测试 **106 passed**（`test_upload.py`/`test_attachment_download.py`/`test_attachment_message.py`/`test_stickers.py`/`test_request_id.py`/`test_log_sensitivity.py`/`test_version.py`/`test_health.py`/`test_attachment_repo.py`/`test_attachment_gc.py`/`test_attachment_resolver.py`/`test_build_info.py`/`test_batch7_contract.py` 等）；`make test-quick` **2640 passed / 2 skipped / 87 deselected**（批次 6 基线 2533 → 净增 ~107）；`make lint` 通过；`make check-types` 无漂移（`contract.generated.ts` 纯增量，v1 类型字节不变）。

### 验收

**docs/14 BE-070/071/081 逐项验收（2026-08-06）**：BE-070（通用上传校验矩阵：数量/大小/MIME/编码 + 稳定附件 ID + 签名下载/签发 + Range/206 + 资源索引 sticker 填充与附件软删过滤 + 引用计数/删除联动 + 结构化错误）；BE-071（Range 播放、内置表情包、GC 四规则 24h/7d）；BE-081（build_sha/版本串、/health//ready 拆分、统一错误 40 码、requestId 关联、日志收敛）。回归 `make test-quick` 2640 passed + `make check-types` 无漂移。

**记录在案的取舍（spec §10）：**
1. **旧 `/api/images` 不纳入附件体系**：两链路并存，图片迁移由前端切换驱动；前端未切换期间旧图链路安全由全局 Bearer 维持（不在签名/GC 保护内）。
2. **视频/音频/文件/表情不喂 LLM**：`content_to_openai` 仅 text/image 进上下文（图片经 AttachmentFileResolver 内存映射转 data URI）；sticker-only/附件-only 消息触发生成、空文本喂 LLM 为已知取舍。
3. **软删保留期 24h + 未关联 7 天**：规则 1 软删超 24h 物理删、规则 3 未引用超 7 天软删；refs 写盘失败场景由规则 3 兜底（7 天后历史消息变「附件已删除」）。
4. **`conversation.log` 明文保留**：调试审计通道有意设计，正文打印收敛后仍全文落盘（敏感扫描测试排除该通道）。
5. **sticker 非附件**：内置公共资源，不落 attachments/不 ref_count/不参与 GC（P7-6 四轮 review 定案）。
6. **下载存在性 oracle**（spec 七轮 Minor 7 记录在案不修）：校验序 ②（404）在 ④（403）前，匿名可区分「active」与「不存在」；attachment_id 104-bit 不可枚举 + 单用户 demo，风险低。
7. **payload 中间件未豁免 `/api/uploads`**（spec I3/Minor 5 设计为前缀豁免）：实际实现保留中间件，聚合 64MB 由 Content-Length 预检承担、单文件 50MB 由 handler `read_chunk` 自限承担，两条 413 路径均为 `REQUEST_TOO_LARGE`——文档按实际实现记录。

---

## 批次 8a：审计修复——安全 / 附件 / 搜索（2026-08-06） ✅ 完成

**提交范围：** `66be44f..565fc10`（16 commits，2026-08-06）
**Spec/Plan：** `docs/superpowers/specs/2026-08-06-electron-backend-batch8a-security-attachment-search.md` / `docs/superpowers/plans/2026-08-06-electron-backend-batch8a-security-attachment-search.md`
**依据：** `docs/electron-backend-audit-batches0-7.md`（A 安全 / B 附件 / C 搜索 三簇 + 探活 20 条发现 + BE-030/BE-031/BE-070 三项合规缺口）

### 结果

- **范围**：闭合审计报告 20 条发现 + 3 项合规缺口（BE-030/BE-031/BE-070——审计判 PARTIAL，批次 8a 全部收口；合规判定更新为 **24 SATISFIED / 0 PARTIAL**）。
- **D1 管理面认证 fallback（`2c5a293`）**：`_check_auth` 闭包捕获 `auth_token` + `access_token`——auth_token 空 + access_token 非空 → 管理端点（`/shutdown` `/restart` `/tools` `/tools/reload` `/debug/api/*`）回退用 access_token 校验（Demo 单 token 语义）；两者都空 → 匿名本地开发；两者都配 → 严格分离。启动告警 `warn_if_unsecured` 接收 auth_token 参数——非回环仅配一个 token → warning 建议补齐。
- **D2 tool.status 发送连接校验（`8b01a91`）**：每 v2 连接服务端分配稳定 `conn_<hex>` 身份存 `WebSocketChannel.connection_id`；`handle_status(status, *, sender_connection_id)` 校验 `doc.target_connection != sender` → 拒绝；offline 重连重传例外放行并迁移归属。残留「抢跑投递目标」移交后续批次。
- **D3 下载签名校验前置（`7b0e0ba`）**：签名模式 sig hex → exp → HMAC 全部通过后才 repo.get——无效 sig 恒 403，防附件存在性枚举。
- **D4 搜索分类组合过滤（`f69298a` / BE-031）**：query 与 category 拆两条 `$elemMatch` 用 `$and` 组合；category=link 特判 URL 内容正则；未知 category → 400 `CONTRACT_VALIDATION`。
- **D5 会话删除附件引用释放（`82eaf04` / BE-030 + BE-070）**：新模块 `core/storage/attachment_release.py`（提取自 debug_routes/archive.py 共享 helper）；REST `DELETE /api/sessions/{id}` 先 `release_session_attachments(agent, sid)` 再删。
- **Minor 修复**：SSRF 补 site-local IPv6 fec0::/10（`6958722`）；认证失败日志记 code/path 不记凭据（`ae590e1`）；GC 日志收敛（只记 aid/kind）+ 规则1 unlink 失败保留记录（`66be44f`）；错误信封 `str(e)` → 通用消息（`2b886b0`）；Inner Voice 应用日志降级 debug（`84b3775`）；Secret Store `threading.Lock` 防并发丢更新（`d0e556d`）；LLM 初始 custom 配置复用 SSRF 守卫（`963d1a8`）；add_ref/remove_ref 原子化——幂等 add + 归零条件软删（`9a16709`）；上传 kind 限长 + 多文件先校验再写盘（`716f2ad`）；探活改用独立 `req_<uuid>`（`f14d9c9`）；共享 fake 时序修复（`565fc10`）。
- **测试**：批次 8a 全量回归 **2731 passed**（审计报告 §1 合规判定同步更新为 24 SATISFIED / 0 PARTIAL）。

### 后续批次（8b / 8c）——审计剩余项

批次 8a 只覆盖 A 安全 / B 附件 / C 搜索 三簇 + 探活。审计报告 `docs/electron-backend-audit-batches0-7.md` 中**尚未修复**的条目按用户拍板的批次组织（8a spec §批次组织）归入：

- **8b（D 簇 WS/语音行为）**：断连误杀其他连接 generation、队列满仍发 accepted ACK、`_interrupt_source` 跨回合泄漏、RECORDING 残留帧自动提交、cancel 无法抢占在途 VAD 段（5 important）；v2 空文本静默丢弃、空响应泄漏 v1 message 帧、dictation ID 校验、`_restore_emotion` 抹除他连接、cancel_stream audio_end 后返回 True、`_wav_info` 固定偏移、附件幂等 entry 异常残留（7 minor）。
- **8c（E/F 簇契约/信封/ID/杂项）**：WS error 信封漏 retryable/details、admin/debug 裸 error 无统一信封、HeartbeatAckMessage 死契约面（3 important）；generation.started.requestId 不传值、畸形 cursor 500、generation 持久化 error 丢 code/retryable、pending 跨会话可见、llm_summary 未用、tools/calls snake_case、voices current 引用不存在、trace 脚本 2 条、chunked 413 兜底、并发 WS TOCTOU、v2 收 capabilities 帧、max_request_bytes=0 回落、build_sha 缓存、decode error 无 code、契约 docstring/wire key（16 minor）。
