# Electron v2 首次联调后端整改设计

> **For agentic workers:** 实施时用 writing-plans 出 plan，按 plan 执行。

**Goal:** 修复 `docs/review/electron-v2-first-integration-backend-review-2026-08-11.md` 全部 P0×2、P1×5、P2×3 问题，使后端可重新宣称「D6c 并发与恢复语义、负面 ACK/限流契约、干净启动会话发现已通过后端冻结门禁」。

**Architecture:** 一次 spec 覆盖 review 三批（A 幂等正确性 / B 联调契约 / C 运维交接，plan 内按批次序排任务，A 必须先做）。核心改动集中在 `core/input/idempotency.py`（内存权威 overlay）、`core/storage/idempotency_repo.py`（claim 所有权 + 三态状态机）、`core/storage/base.py`（DuplicateKey 不判死）、`core/input/ws_handler.py`（claim loser 分支 + 限流关联 + complete 前置）、`frontend_contract/ws_message.py`（ACK 判别联合）+ 生成器（嵌套判别联合支持）、`core/memory/session_manager.py`（clean-start default 发现）、`core/input/server.py`（探针分层 + 限流认证主体）。

**Tech Stack:** Python 3.12 + aiohttp + Motor + Pydantic 契约（`frontend_contract`）+ pytest（`@pytest.mark.asyncio` + `aiohttp_client` + fake repo 故障注入）。

---

## 背景

Review 基于 `main@b5b7e6e` 确认：单连接/单请求/稳定 Mongo 的文本 happy path 已具备，但 D6c 并发与恢复语义、负面 ACK/限流契约、干净启动会话发现未过后端冻结门禁。本 spec 按该 review 的「必须修改 + 验收测试」逐项整改。

## 现状缺陷摘要（映射 review 条目）

| review ID | 优先级 | 现状 | 根因位置 |
|---|---|---|---|
| BE-P0-01 | P0 | 并发同键 claim loser 仍进入业务处理；消息/正文串线；DuplicateKey 被误判连接失败 | `idempotency_repo.claim()` 返回无所有权信息；`base.py:148-156` 把所有 driver 异常判死 |
| BE-P0-02 | P0 | idempotency 未入生命周期：无健康探测/恢复/关闭，`/ready` 不含它，durable 初值误报 | `_lifecycle_persistence.py`；`server.py:273-280`；`idempotency.py:configure` |
| BE-P1-01 | P1 | 崩溃/拒绝残留一律重放 accepted（幽灵 accepted） | `ws_handler.py:743-755` 不查状态机不查提交证据 |
| BE-P1-02 | P1 | dev 内存降级在 Mongo 状态切换时丢去重 | `idempotency.py:89-137` lookup/store 不双读 |
| BE-P1-03 | P1 | WS 限流错误无 `clientRequestId` 关联 | `ws_handler.py:1013-1028` generic ErrorMessage |
| BE-P1-04 | P1 | ACK 机器契约单模型，允许非法组合；无负面 golden | `ws_message.py:310-320`；`scripts/gen_contract_frames.py` |
| BE-P1-05 | P1 | clean-start 时 active `default` 不在 REST 列表，detail 时间空 | `session_manager.py:399-423` `_merged_summaries` 不含 eager default |
| BE-P2-01 | P2 | `/health` 同步探测慢依赖；`/ready` 把 Milvus 当整体门禁 | `server.py:256-310` `_collect_health_payload` |
| BE-P2-02 | P2 | 限流先于认证按任意 Authorization 分桶 | `rate_limiter.py:101-110` |
| BE-P2-03 | P2 | 公开文档漂移（host/fail-open/query-token/E2-E3 旧状态） | `docs/electron-backend-security.md`、README、progress |

---

## 设计

### 一、幂等正确性（批次 A：BE-P0-01 / P0-02 / P1-01 / P1-02）

#### 1.1 状态机与 claim 所有权（BE-P0-01 + BE-P1-01）

幂等记录状态改为三态，文档 schema 扩展为**完整 ACK 重放负载**：

```
CLAIMED ──(process_message 返回 True 后立即 complete)──→ ACCEPTED
   └──────(rejected ACK 前 store.reject)────────────→ REJECTED（持久化 error 负载）
（TTL 到期删除；legacy "in_progress"/"completed" 在 from_doc 时防御映射为 claimed/accepted）
```

文档字段：`conversation_id`、`client_request_id`、`content_digest`、`message_id`、`status`、`claimed_at`（unix 秒）、`expires_at`（BSON Date TTL）、`received_at?`、`client_message_id?`、`conversation_id_wire?`、`error?{code,message,retryable,details?,retryAfterMs?}`。ACCEPTED/REJECTED 都持久化**完整 ACK 负载**（messageId、clientRequestId、clientMessageId、conversationId、receivedAt 原值 + rejected 的 error），使重放逐字一致。

**`IdempotencyRepository.claim()` 返回 `ClaimResult{entry, acquired}`**（dataclass，`idempotency_repo.py` 定义）：

- `acquired=True` = 新插入 **或过期 CLAIMED 接管**（见 §1.2）→ 调用方方可进入业务处理；
- `acquired=False` = 已有记录（并发 loser / 重放）→ **恒不进入业务处理**，调用方按状态机分支。

**原子性取舍（决策记录 D6-d）**：`DocumentDBBackend.update_one` 只返回 `modified_count`、不暴露 `upserted_id`（已核实 `mongodb.py:101-106`），单条原子 upsert 不可行。采用 report 明示的兜底路径 + **DuplicateKey 不判死**：

1. `Repository._call`（`base.py:123-156`）增加 `non_fatal: Callable[[BaseException], bool] | None` 参数：命中时**不 `_mark_unavailable()`、不包装**，裸抛原始异常；
2. `_is_duplicate_key` 扩展为**同时检查 `type(exc).__name__` 与 `type(exc.__cause__).__name__`**（raw driver 异常的 `__cause__` 为 None，只查 cause 会漏判——review 复核确认）；
3. `claim()` 主路径：`find`（已存在 → `(existing, acquired=False)`）→ `insert_one`（传 `non_fatal=_is_duplicate_key`）；插入抛原始异常且 `_is_duplicate_key` 为真 → 读回 winner `(winner, acquired=False)`；否则按连接错误处理（判死 + StorageUnavailableError）；
4. `claim()` 的 except 需同时覆盖 `StorageUnavailableError` 与原始 driver 异常（统一经 `_is_duplicate_key` 判定后分支）。

**`ws_handler` 三路径（text / 附件+sticker / voice.commit）**：`store()` 后按 `ClaimResult.acquired` 分支：

- `acquired=True` → 继续（附件 refs、`process_message`、录音 commit、EventBus post）；
- `acquired=False` → **立即 return，绝不进业务处理**，按 entry 状态分支：
  - 同内容 + `ACCEPTED` → 重放原 accepted ACK（持久化字段逐字一致，含原 receivedAt）；
  - 同内容 + `REJECTED` → 重放原 rejected ACK（持久化 error 逐字一致）；
  - 同内容 + `CLAIMED` → 按 §1.2 恢复规则（新鲜→retryable；过期+证据→accepted；过期+无证据→接管重执行）；
  - 异内容（任意状态）→ rejected `CONTRACT_VALIDATION`（同内容才有重放资格）。

**complete 前置（BE-P1-01 提交窗口）**：text/附件/voice 的 accepted 路径中 `store.complete()`（→ACCEPTED）移至 `process_message` 返回 True 后**立即 await、在 accepted ACK 之前**（现状 `ws_handler.py:828/866` 在 ACK 后）。使「process 后 ACK 前 kill」⇒ 记录为 ACCEPTED ⇒ 重放 accepted，不重复生成、终态确定。残余窗口「ACCEPTED 后、loop 执行 `register()` 前 kill」为接受风险（bus 已入队但进程死亡，generation 未建档；客户端可凭 accepted + 超时兜底）——记入决策记录 D7-b。

**voice.commit 顺序修正（BE-P0-01#4）**：resolve work → `reserve_work` → claim（`acquired=False`/异常 → **release_work + 发对应 ACK**，不消费录音）→ `commit_recording`（抛 `VoiceSessionError` 且 claim 已获取 → `store.reject()` + release_work）→ ACK → complete。claim 失败不得预占/消费录音状态，不得泄漏 work 引用（现状 `ws_handler.py:969-977` 在 claim 失败时已泄漏 reserve_work——本次一并修复）。

#### 1.2 CLAIMED 残留恢复（BE-P1-01）

提交证据 = **GenerationRegistry**（`register()` 已在 `loop.py:514` process 入口同步 await；文档含 `user_message_id`，与 claim 的 `message_id` 同键）。新增 `GenerationRegistry.find_by_user_message(user_message_id)`（repo 按 `user_message_id` 字段查询，小改动）。

同内容 CLAIMED 命中时（决策记录 D7，报告验收测试强制）：

1. **新鲜**（`now - claimed_at ≤ GRACE_SECONDS`，默认 30s）→ rejected `IN_PROGRESS`（`retryable=True` + `retryAfterMs`）——原请求大概率仍在途；
2. **过期 + 证据**（`find_by_user_message` 命中）→ 重放 accepted ACK + `complete()` 标记 ACCEPTED——证明已入队处理；
3. **过期 + 无证据** → **接管**：以同键重新 `claim()`（内部对过期 CLAIMED 做 `update_one` 延长 `claimed_at`/`expires_at` 后返回 `acquired=True`），按新请求继续——无证据表明消息已入队，重执行安全。接管通过「重新 claim 得 acquired」表达，**不与「acquired=False 恒不处理」矛盾**（acquired=False 只在并发 loser/重放时出现；接管是重新取得所有权）。**职责边界：证据检查（generation_registry）在 handler 完成；`claim()` 内部只按 `claimed_at` 判 stale（不访问 generation_registry），stale CLAIMED 命中即接管更新并返回 `acquired=True`——两者分离，防 plan 双实现漂移。**

> 注：`lookup()` 命中 CLAIMED 与 `store()` 返回 loser 两个入口共用同一套分支（§1.1/§1.2 规则）。plan 以单一 `_handle_idempotency_hit(entry)` 统一两处，防双实现漂移。

#### 1.3 dev 内存降级连续性（BE-P1-02）

**内存权威 overlay + Mongo 写穿 + 恢复 reconcile**（决策记录 D5）。`IdempotencyStore` 改为：

- **`lookup()` 内存优先**：恒查内存（TTL 有效即命中返回）；miss 才查 Mongo（跨重启镜像）；Mongo 报错：产品模式透传（fail-closed），dev 模式视为 miss；
- **`store()` 恒写内存**：Mongo 可用时写穿 claim（`acquired` 语义保留）；Mongo 报错：产品模式透传，dev 模式内存降级；`acquired=False` 时把 winner 也写入内存（保持内存镜像完整）；
- **`recover()`（Mongo 恢复 reconcile）**：健康循环探测到 idempotency False→True 时调用——对每条内存 entry：Mongo 无对应文档才插入；Mongo 已有则**内存刷新为 Mongo 状态**（ACCEPTED/REJECTED/error 对齐，确定性规则：**Mongo 优先，且读时一致**——lookup 内存命中返回的即是 Mongo 对齐后的状态），随后恢复正常 durable 写穿；
- **`durable`（决策记录 D5-b）**：`repo is not None and not _in_memory_fallback`；`configure()` 初值 `_in_memory_fallback = repo is None or not repo.is_connected`（`is_connected` = `_db is not None`）——修复「repo 对象非空即 durable」的初值误报；运行时 durability 由操作结果驱动（既有 `_in_memory_fallback` 机制，Mongo 静默断网经首次失败翻 false），**不**引入 `_db is not None` 作为 /ready 健康信号（`base.py:8-12` 已记录该假阳性，/ready 用真实 ping，见 §3.1）。

进程内任意 Mongo 状态切换（up→down→up→down）不丢去重；`idempotency_durable=false` 仅表示「跨进程/跨重启不保证」，同一进程基本重试不受影响。

#### 1.4 idempotency 生命周期（BE-P0-02）

`_lifecycle_persistence.py` 统一 persistence inventory 接入：

- **启动 connect**：现状 `:94` 调一次 `connect()`，保留；`configure_store()` 的 durable 初值改由 `repo.is_connected` 决定（§1.3 D5-b）；
- **60s 健康探测**：`_db_health_loop` probes 增加 `"idempotency"`（`idempotency_repo.health_check` 真实 ping）；
- **False→True 恢复**：`_recover_dependency("idempotency")` → `repo.connect()` + `store.recover()`（§1.3 reconcile）；
- **shutdown close**：`_stop_persistence`/`_stop_attachment_repo` 补 `idempotency_repo.close()`；stop/restart 两轮无悬挂 client、可重连；
- **`/ready` 门禁**：product 模式的核心 chat readiness 必须含 idempotency（真实 ping，见 §3.1 D9）；dev 模式（回环）允许降级不门禁；
  - **product_mode 定义**：`create_app(product_mode: bool = False)` 现有参数（`server.py:352`），生产入口传 `product_mode=not is_loopback_host(host)`（`agent/server.py:446`，非回环绑定 → 产品）；测试经显式 `product_mode=True`/`False` 进入对应模式（review 首轮要求明确运行时可判定概念）；
- **能力初值**：`server_info.features.idempotency_durable` 走 `get_store().durable`（§1.3），connect 成功前为 false；
- **能力变化策略（决策记录 D3）**：`server_info` = 握手快照，durable 翻转不在线重发事件；此后 `STORAGE_UNAVAILABLE` 的 rejected ACK/error 在 `details` 携带当前 `idempotency_durable`，客户端可重连取新快照。

### 二、联调契约（批次 B：BE-P1-03 / P1-04 / P1-05）

#### 2.1 ACK 判别联合（BE-P1-04）

`frontend_contract/ws_message.py`：`UserAckMessage` 拆为 status 判别联合：

- **`AcceptedUserAckMessage`**：`status: Literal["accepted"]`、`messageId: str (min_length=1)`、`clientRequestId: str (min_length=1)`、`clientMessageId?`、`conversationId?`、`receivedAt`；**无 error**；
- **`RejectedUserAckMessage`**：`status: Literal["rejected"]`、**`messageId: Literal[""]`（决策记录 D2：短期保留空白串兼容前端批次 A；omit 记未来协议版本备忘）**、`clientRequestId: str`（**无 `min_length`——记录偏差：handler 的「缺少 clientRequestId」拒绝路径回显空串，Rejected 允许空；Accepted 保持 `min_length=1`**，见决策记录 D2 注）、`clientMessageId?`、`conversationId?`、`receivedAt`、**`error: AckError`（必填）**；
- **`AckError`**（取代任意 dict）：`code`、`message`、`retryable`、`details?`、**`retryAfterMs?`（顶层，与 ErrorMessage 同位置）**；`details` 可携带 `idempotency_durable`（bool，STORAGE_UNAVAILABLE 时）。

模块级命名联合 `UserAckMessage = Annotated[Union[Accepted, Rejected], Discriminator("status")]`（Python 侧类型用）；`ServerMessage` 的 `user_message.ack` 分支引用它。

**生成器嵌套判别联合支持（关键实现项，review 复核确认现状会崩）**：

- `gen_schema.build_models`（`frontend_contract/gen_schema.py:36`）：现状 `all(issubclass(c, BaseModel) ...)` 遇嵌套 Annotated 成员抛 `TypeError`。改为成员判定允许「BaseModel 或 Annotated 联合」（递归判定），pydantic `TypeAdapter.json_schema()` 原生产出嵌套 oneOf（已确认 pydantic 2.13 正确处理嵌套判别联合）；
- `gen_ts`（`__main__._collect` :62-67）：现状把嵌套 Annotated 成员原样放入成员表，发射 `m.__name__=="Annotated"`。改为**递归展开**嵌套 Annotated 联合的成员（TS 判别联合天然支持重叠 type 分支，前端按 status 再分）；`contract.raw.ts` 同步正确。
- 验证：`make gen-types` 重新生成 `contract.generated.ts`/`contract.schema.json`/`contract.raw.ts`；`python -m frontend_contract --check` + `python scripts/gen_contract_frames.py --check` 通过。

**`_send_ack`（`ws_handler.py:524`）** 按 status 构造 `AcceptedUserAckMessage`/`RejectedUserAckMessage`；重放路径用持久化字段（含原 receivedAt）重建，逐字一致。

**负面 golden**：`scripts/gen_contract_frames.py` 新增 **6 帧**：`CONTRACT_VALIDATION`、`SESSION_NOT_FOUND`、`QUEUE_FULL`、`STORAGE_UNAVAILABLE`、`RATE_LIMITED`、**`IN_PROGRESS`**（§1.2 新增的 CLAIMED 新鲜窗口拒绝码——review 首轮补漏，保证「handler 产生的每一种 rejected ACK 都有 golden 基线」）。均 rejected + `messageId:""` + AckError。**跨仓承接（BE-P1-04 必须修改 #6 / 验收 #3/#4）**：后端交付负面 golden 帧 + `schemaHash` 到前端仓；Electron raw parser/mapper 对新增帧的 replay 为前端仓活动，**在冻结联调前须完成并回填 runbook，不得静默丢弃**（D1 推迟执行但不推迟门禁判定——后端侧以 golden `--check` 锁住自家 schema/handler 一致，跨仓 CI 漂移由前端仓承接并在联调门禁核对）。

#### 2.2 WS 限流关联（BE-P1-03）

`handle_json_message` 限流块（`ws_handler.py:1013-1028`）按消息类别分通道（决策记录 D8）：

- **`text` / `voice.commit`（且 clientRequestId 提取有效）**：超限发送 **rejected ACK**（`messageId:""`、`clientRequestId=提取值`、`error{AckError: code=RATE_LIMITED, retryable=true, retryAfterMs}`）——直接 settle 前端对应 promise；
- **`text`/`voice.commit` 但 clientRequestId 缺失/非法**：无合法关联键 → 退化为 generic `ErrorMessage` RATE_LIMITED（前端对这两类恒发 clientRequestId，缺失即畸形帧；文档化限制）；
- **其余命令**：`ErrorMessage` 增加可选 `clientRequestId`（提取值）+ 顶层 `retryAfterMs`；
- **提取**：限流在完整校验前，仅做有严格长度/类型限制的 `clientRequestId` 提取（`str` + 长度上界），不触发完整解析；
- **`retryAfterMs`**：`RateLimiter`/`TokenBucket` 增加 `retry_after_ms(category, key)`（`(capacity - tokens)/refill_per_sec * 1000`，下限 1000）；
- **WS 建连 429**：升级前限流响应补 `Retry-After` 头（与 REST 一致）；
- 客户端命令消息（`switch_model`/`tool_approval`/`set_service`/`set_tts_voice`/`activate_session`/`wake_toggle`）契约加可选 `clientRequestId`。

#### 2.3 clean-start 会话发现（BE-P1-05）

`SessionManager`（`core/memory/session_manager.py`）：

- 启动时（`__init__` eager default 处）为 `DEFAULT_SESSION` 建立合法本地 summary：`updated_at=now_iso()`、`title=""`、`message_count=0`，写入 `_local_summaries`；
- Mongo 可用时 best-effort 持久化：确保 `default` 会话文档存在（upsert，message_count=0；失败仅记日志，不阻塞启动）；
- **合并优先级**：`_merged_summaries` 既有 `max(updated_at)` 合并（`:420`）已保证新鲜本地 summary 胜出（避免陈旧 Mongo default 文档遮蔽 clean-start），spec 明确该不变量；
- 结果：clean-start 时 `GET /api/sessions` 返回 `[default(active=true)]`；`GET /api/sessions/default` detail `updatedAt` 非空可解析；WS 首帧 conversationId、REST active、`agent.active_session_id` 同源；**「会话非空时列表恒恰一个 active」不变量**显式成立（active 由 `_active_id` 单点派生）；
- 更新既有 `test_list_empty`（`TestSessionsCRUD`，`test_session_routes.py:158`，原名 `test_sessions_list_empty` 不存在——review 复核修正）——从断言 `sessions == []` 改为断言 `default` 存在且 active。

### 三、运维（批次 C：BE-P2-01 / P2-02 / P2-03）

#### 3.1 探针分层（BE-P2-01，决策记录 D9）

`core/input/server.py` 拆 `_collect_health_payload`：

- **`/health`（liveness）**：只做进程/FSM/事件循环存活检查 + 运行信息（uptime/build/fsm），**零网络探针**——去掉 session/message/attachment/Milvus 的 `wait_for` 探测（现状最多 8s），任何依赖挂起都在很小固定预算内返回；
- **`/ready`（readiness）**：HTTP 门禁 = **核心 chat**——`mongo_ok`（session+message+attachment 真实 ping，**并行探测** `asyncio.gather` + 每项 `wait_for`）+ **product 模式时 idempotency 真实 ping**（dev 允许降级不门禁）；**Milvus 不门禁 HTTP**（文本可降级使用）；
  - payload：`status`（全依赖 ok→"ok" 否则 "degraded"，可观测）、`chatReady`（=HTTP 门禁）、`fullReady`（=chatReady AND milvus）、`dependencies`（明细：mongo/milvus/idempotency）；
  - 语义：`/ready=200` ⇒ v2 send 可用；`/ready=503` + payload 解释文本可用/不可用原因。

#### 3.2 限流认证主体分类（BE-P2-02）

`rate_limiter.py:rate_key_for_request` + `server.py:_make_rate_limit_middleware`：

- 中间件持有合法 token 集（`auth_token`/`access_token`，`create_app` 作用域内可得，`server.py:347-348`）；解析 `Authorization: Bearer <token>`；
- **认证成功**（token 与合法集之一常量时间相等）→ `auth:<sha256(token)[:16]>` 分桶；
- **缺失或无效** → 统一 `ip:<remote>` 分桶（含无效 token 轮换攻击），**不得**再以任意 Authorization 字符串生成独立 key；
- WS 升级前限流用同一分类器（`handle_ws` 建连限流 key 不再按任意 header）；
- 保持「限流先于认证」以挡匿名洪水，但无效主体不再获得 per-token 配额、不能挤掉合法主体桶、不能填满 10,000 上限。

#### 3.3 文档漂移（BE-P2-03）

- `docs/electron-backend-security.md:43-45`：改述 fail-closed（非回环无 Token 拒绝）+ 默认 host=127.0.0.1；
- `README.md` 配置示例：`server.host` 改回安全默认（127.0.0.1），注明远程绑定需显式配置；
- `docs/progress/2026-08-10-backend-frontend-progress.md`：删浏览器 WS query-token 阻塞表述、E2/E3/F 旧状态；标注该文档为历史快照；
- `docs/contract-release-2026-08-11.md`：区分 `contractCommit`（契约生成时）与 `runtimeCommit`（本次联调 runtime SHA）；
- **`docs/review/electron-v2-backend-change-list-2026-08-11.md`**：同步（review BE-P2-03 必须修改清单含 change-list——首轮 spec review 补漏）；
- 新增一页最小联调 runbook（`docs/electron-v2-first-integration-runbook.md`）：唯一列出 backend runtime SHA / schemaHash / frontend SHA、`SERVER_HOST`/端口/Token、Mongo/Milvus 条件、`SERVE_CHAT=0` 与 `/` 404 含义、WS 三请求头、clean-start bootstrap、ACK/generation 成功标准与负面用例、requestId/clientRequestId 对照；**联调纪律（review §8.5）：每次全新 clientRequestId、不并发同 ID、ACK 未决不重传**；**联调通过标准（review §8.8 六事件序列）：session → server_info → expression、REST 创建/列表、accepted ACK、generation 六事件、completed、历史读取、断开**；
- `todo.md` 同步关闭项与决策记录。

## 契约变更总表

| 位置 | 变更 |
|---|---|
| `ws_message.py` | `UserAckMessage` → `AcceptedUserAckMessage`/`RejectedUserAckMessage`（status 判别联合）；新增 `AckError`；`ErrorMessage` 加可选 `clientRequestId` + 顶层 `retryAfterMs`；6 个命令消息加可选 `clientRequestId` |
| `gen_schema.py` / `__main__._collect` / `gen_ts.py` | 嵌套 Annotated 判别联合支持（schema 成员判定递归化 + gen_ts 递归展开） |
| `contract.generated.ts` / `contract.schema.json` / `contract.raw.ts` | 重新生成 |
| `scripts/gen_contract_frames.py` | 新增 6 负面帧（含 IN_PROGRESS）；`--check` 门禁 |

## 测试（全部 unit/service，fake repo 故障注入，不依赖真实 Mongo/Milvus）

marker 分配（CLAUDE.md 强制，review 复核要求显式）：契约/golden → `unit`；其余 → `service`（`@pytest.mark.asyncio`）。

- **幂等 claim**（`test_idempotency_durable.py` 扩展，service）：并发同键同内容 → process/EventBus/generation 恒 1；同键异内容 → 一 accepted 一 `CONTRACT_VALIDATION`、不串线；text/附件/sticker/voice.commit 三路径；DuplicateKey 后 `health_check` 仍 true、下一条可正常发送；内存降级路径同并发测试；
- **状态机**（service）：claim 后 process 前 kill（无证据）→ 重启接管重执行不重复；process 后 ACK 前 kill（complete 已前置 → ACCEPTED）→ 重放 accepted 不重复；QUEUE_FULL/INTERNAL 后 reject 持久化失败 → 恢复重试为原 rejected 或 retryable，不幽灵 accepted；accepted/rejected 重放字段逐字一致（receivedAt 用持久化原值）；
- **dev 降级**（`test_idempotency.py`，service）：up→down、down→up、down→up→down 三序列，同 clientRequestId 重试 process/generation 恒 1；同键异内容各序列稳定 rejected；恢复时内存/Mongo 冲突走确定性规则（Mongo 优先 + 读时一致）；
- **生命周期**（service，fake repo 翻转可用性）：Mongo 离线启动 → durable=false、product text rejected；不重启恢复 → health loop 重连 → `/ready` 200 → accepted → 新连接 durable=true；**运行中（先健康）断开再恢复同样通过（BE-P0-02#3，review 首轮补漏）**；idempotency 单独失效 `/ready` 不假绿；**dev 与 product 双模式分别覆盖（BE-P0-02#6，review 首轮补漏）**；stop/restart 两轮无悬挂；
- **契约**（`frontend_contract/tests`，unit）：schema 拒绝 status/字段非法组合；6 负面帧（含 IN_PROGRESS）经后端校验；
- **限流**（`test_rate_limit.py`/`test_v2_ws.py`，service）：text/voice.commit 超限立即 settle、只拒被限那条、rejected 带原 clientRequestId/RATE_LIMITED/retryable/数值退避、连接存活可重试；WS 429 带 Retry-After；同 IP 轮换 10001 假 Token 不能获 10001 份配额、不挤掉合法主体；**合法 Token 仍按主体隔离（两个不同合法主体并发互不挤占——BE-P2-02 正向用例，review 二轮补漏）**；clientRequestId 缺失时退化为 generic ErrorMessage；
- **会话**（`test_session_routes.py`，service）：clean-start list 含 default active、detail updatedAt 非空；**Mongo 离线/恢复/重启后三条接口仍一致（BE-P1-05#3，review 首轮补漏）**；**列表会话非空时恒恰一个 active 不变量**；`test_list_empty` 更新；
- **探针**（`test_server.py`，service）：依赖全挂起 `/health` 小预算返回；Milvus down → `/ready` 200（chat ready）+ payload 解释；product idempotency down → `/ready` 503；dev idempotency down → `/ready` 200；
- **门禁**：`make test-quick`/`test-normal`/`pyright`/`ruff`/`check-types`/golden `--check` 全绿。

## 验收标准（DoD）

- P0 全部修复；P1 有代码、测试、机器契约证据；
- 同键并发、Mongo 启停/抖动、claim 后 kill、reject 持久化失败均不重复生成、不幽灵 accepted；
- `/ready` 与 product v2 send 可用性一致；`server_info` 不误报 durable；
- clean-start 有确定、跨 REST/WS 一致的活动会话；列表恒恰一个 active；
- `RATE_LIMITED` 关联到具体客户端请求并立即 settle；
- rejected ACK 判别 schema 与 handler 完全一致，负面 golden（6 帧）进 `--check`；跨仓 replay 完成并回填 runbook 后，BE-P1-04 方算闭环；
- 后端完整门禁通过；README/security/progress/todo/change-list 不再互相冲突；
- 交付 runbook 页（runtime SHA/schemaHash 待联调时回填；含 §8.5 纪律与 §8.8 六事件通过标准）。

## 决策记录（2026-08-11）

1. **D1 批次范围**：一次 spec 覆盖 A+B+C（后端部分），plan 内按批次序；「真实 Electron Main 联调 + 跨仓 golden replay」属前端仓活动，后端交付 runbook/manifest 与门禁证据。
2. **D2 rejected messageId**：保留 `messageId:""`（判别联合 `Literal[""]` 表达不变量）；omit 记未来协议版本备忘。
   - **D2 注（记录偏差，2026-08-11 实现期）**：`RejectedUserAckMessage.clientRequestId` **不加 `min_length`**（空串合法）——handler 的「缺少 clientRequestId」拒绝路径会回显空串，若 schema 加了 `min_length=1` 会拒绝真实 wire。`AcceptedUserAckMessage.clientRequestId` **保持 `min_length=1`**（accepted 恒有幂等键）。future reviewer 请勿再把它标记为 schema 缺陷。
3. **D3 durable 能力变化**：server_info = 握手快照，不推送能力事件；STORAGE_UNAVAILABLE 的 rejected ACK/error 在 details 携带当前 `idempotency_durable`。
4. **D4 clean-start 会话**：eager default 建合法本地 summary + Mongo 可用 best-effort 持久化；合并 `max(updated_at)`；activeConversationId 前端契约留作未来。
5. **D5 dev 降级连续性**：内存权威 overlay + Mongo 写穿 + 恢复 reconcile（Mongo 优先且读时一致）；进程内任意 Mongo 切换不去重。
6. **D5-b durable 定义**：`repo is not None and not _in_memory_fallback`；configure 初值用 `repo.is_connected`；运行时 op 驱动；不引入 `_db is not None` 作健康信号（/ready 用真实 ping）。
7. **D6 claim ownership**：`ClaimResult{entry, acquired}`；`_call` 加 `non_fatal` 谓词；`_is_duplicate_key` 同时查 `exc` 与 `exc.__cause__`。
8. **D6-d 原子 upsert 取舍**：`DocumentDBBackend.update_one` 不暴露 `upserted_id`（已核实），采用唯一键兜底 + `non_fatal`；单条原子 upsert 记为未来增强。
9. **D7 CLAIMED 恢复**：新鲜→retryable IN_PROGRESS；过期+证据（GenerationRegistry user_message_id）→ accepted；过期+无证据→接管（重新 claim 得 acquired，不违反 acquired=False 不变式）。
10. **D7-b 提交窗口**：complete(ACCEPTED) 移至 process 返回后、ACK 前（「process 后 ACK 前 kill」⇒ ACCEPTED 重放，不重复）；残余「ACCEPTED 后 loop register 前 kill」为接受风险（bus 已入队、generation 未建档，客户端凭 accepted+超时兜底）。
11. **D8 限流关联范围**：text/voice.commit 走 rejected ACK（合法 clientRequestId 时）；命令类 ErrorMessage 加可选 clientRequestId + 顶层 retryAfterMs；6 个命令消息契约加可选 clientRequestId；clientRequestId 缺失退化 generic ErrorMessage。
12. **D9 探针分层**：/health 纯 liveness 零网络探针；/ready HTTP 门禁=核心 chat（mongo repos 并行 ping + product idempotency ping），Milvus 不门禁，payload 带 chatReady/fullReady。
13. **BE-P2-02 实现选型**：只有认证成功主体按 auth principal 分桶，缺失/无效统一 `ip:<remote>`；WS 建连同分类器。
14. **生成器嵌套联合**：gen_schema 成员判定递归化（pydantic 原生产出嵌套 oneOf）+ gen_ts 递归展开；`UserAckMessage` 保留为模块级命名联合。

## 非后端责任边界（review §10 摘录）

Endpoint 规范化、Token 与 Origin 绑定、Real/Mock 装配、Renderer/IPC 错误传递、UI 空列表/断线/重连恢复、rejected ACK `messageId:""` 的消费兼容——均属 Electron 前端；BE-P1-03/04/05 是双方交界契约，本 spec 从后端侧修齐，前端按冻结 runtime SHA + schemaHash 对接。
