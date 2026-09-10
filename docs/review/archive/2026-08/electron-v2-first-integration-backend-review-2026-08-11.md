# Electron v2 首次真实联调前：后端复核与整改清单

**日期：** 2026-08-11

**后端审查对象：** `/home/yomu/elysia-agent` `main@b5b7e6e057878342066310123ace2d2fc3ecf97b`

**前端对照对象：** `/home/yomu/elysia-frontend` `main@3372ac9`（Claude Code 正在继续开发，未提交设计稿不作为冻结实现）

**范围：** 第一次 Electron 真实联调所依赖的 WS v2、会话 REST、认证、限流、持久化幂等、健康检查、机器契约与交接资料

**关联文档：** `electron-v2-backend-integration-readiness-review-2026-08-10.md`、`electron-v2-backend-change-list-2026-08-11.md`
**性质：** 本文件是本轮后端 review 结果和整改要求；本轮只新增报告，没有修改后端业务源码或 `todo.md`。

## 1. 结论

当前后端的**单连接、单请求、Mongo 从启动起稳定可用**的文本 happy path 已具备，第一次受控联调可以开始；但后端还不适合冻结为“Electron 接入充分、D6c 已封口”的最终基线。

本轮新确认：

- **P0 × 2**：持久化幂等的并发 claim 并没有阻止重复生成；IdempotencyRepository 没有接入恢复、readiness 和关闭生命周期，产品模式可能在 Mongo 已恢复后仍永久拒绝所有 v2 消息，直到重启后端。
- **P1 × 5**：持久 claim 的崩溃/拒绝状态会被错误重放成 accepted；dev 内存降级在 Mongo 状态切换时丢失去重；WS 消息限流无法与 `clientRequestId` 关联；ACK 机器契约和 golden 没有表达真实 rejected wire；全新数据目录下活动 `default` 会话在 REST 列表不可发现。
- **P2 × 3**：liveness 会同步探测慢依赖；限流在认证前按任意 Authorization 值分桶，可被无效 Token 绕过并填满桶表；公开文档仍混有旧的 host/fail-open/浏览器 WS query-token 结论。

因此建议采用下面的状态口径：

> 后端核心接口主体已开发，正常文本链路可做第一次受控联调；D6c 并发与恢复语义、负面 ACK/限流契约、干净启动会话发现尚未通过后端冻结门禁。联调成功只能证明 happy path，不能据此证明 exactly-once、故障恢复或生产就绪。

在 P0 修复前，联调应限定为：单 Electron 实例、每次发送使用全新 `clientRequestId`、不并发重传、Mongo 在后端启动前已可用且联调期间不抖动。

## 2. 本轮验证与故障注入

### 2.1 已通过的现有门禁

在 Elysia conda 环境中执行：

```bash
python -m pytest \
  core/input/tests/test_idempotency_durable.py \
  core/input/tests/test_rate_limit.py \
  core/input/tests/test_v2_ws.py \
  core/input/tests/test_session_routes.py \
  core/input/tests/test_auth.py \
  core/input/tests/test_version.py -q
```

结果：**179 passed**。

以下检查也通过：

```bash
python -m frontend_contract --check
python scripts/gen_contract_frames.py --check
git diff --check
```

这说明当前测试和生成产物彼此自洽，但不能覆盖下述并发、恢复和跨仓消费问题。

### 2.2 故障注入结果

1. **并发同键 claim 仍有两个业务处理者。** 用带 barrier 的原子 fake repo，让两个请求同时完成 lookup miss，再竞争同一个 `(conversation_id, client_request_id)`：

   ```text
   processed_count = 2
   A 使用 digest-A 继续处理，但拿到了 B 的 msg-B / digest-B
   Mongo 中只有一条 claim
   ```

   唯一索引只保证“表里一条记录”，没有保证“只有 claim owner 进入 `process_message()`”。同内容会重复生成；不同内容还会出现“B 的 messageId 承载 A 的正文”的归属错误。

2. **Mongo 离线但 repo 对象存在时，首次握手能力误报：**

   ```text
   repo._db = None
   durable_before_first_operation = True
   ```

   `configure()` 把“repo 对象非空”当成 durable；只有第一条存储操作失败后才翻为 false。

3. **dev 内存降级在 Mongo 恢复时丢失去重：**

   ```text
   during_outage = msg-memory
   after_repo_recovers = None
   memory_entry_still_exists = True
   ```

   内存中明明仍有 claim，但一次成功的 Mongo miss 会直接返回 `None`，不会再查内存，于是同一请求可再次生成。

4. **全新 SessionManager 的跨接口状态不一致：**

   ```text
   active_id = default
   GET /api/sessions 对应的 list_ids = []
   ```

   WS 建连会回放 `conversationId=default`，REST 列表却没有任何 active 会话；详情兜底还会返回空字符串 `updatedAt`。

5. 七类当前 golden 中没有一条 `user_message.ack.status=rejected`，也没有 `RATE_LIMITED`、`QUEUE_FULL` 或 `STORAGE_UNAVAILABLE` 负面样例。

## 3. 优先级总表

| ID | 优先级 | 问题 | 对联调/运行的直接影响 |
|---|---:|---|---|
| BE-P0-01 | P0 | D6c claim 没有“是否取得所有权”结果，并发同键请求都继续生成 | 重复 generation；异内容时 messageId/正文串线 |
| BE-P0-02 | P0 | IdempotencyRepository 未接入健康恢复、readiness、能力初值和关闭生命周期 | Mongo 恢复后文本仍 503；`/ready` 可假绿；`idempotency_durable` 可误报 |
| BE-P1-01 | P1 | 持久 claim 的状态机不足，崩溃/拒绝残留一律重放 accepted | 前端收到 accepted，但后端没有对应用户消息或 generation |
| BE-P1-02 | P1 | dev 内存降级与 Mongo 路径不做双读/迁移 | Mongo 抖动或恢复时，同 clientRequestId 可在同一进程重复生成 |
| BE-P1-03 | P1 | WS `RATE_LIMITED` 没有 `clientRequestId` 关联 | 当前 Electron `sendMessage()` 只能等 ACK 超时；错误还可能带上一请求的 requestId |
| BE-P1-04 | P1 | ACK 机器契约未按 status 判别，负面 golden 缺失 | 后端生成的 schema 允许无效组合，也没有准确描述真实 rejected 空 messageId |
| BE-P1-05 | P1 | 干净启动时 active `default` 不出现在会话列表，详情时间为空 | 前端 bootstrap 可能无 currentId，无法发送；REST/WS 对活动会话说法不一致 |
| BE-P2-01 | P2 | `/health` liveness 同步探测多个外部依赖，`/ready` 没有按 Electron 文本能力分层 | 探活可等待数秒；Milvus 降级时文本能用但 ready=503 |
| BE-P2-02 | P2 | 限流先于认证，任意无效 Authorization 都形成独立 auth key | 可绕过凭据爆破的 IP 配额并填满 10,000 个桶，误拒合法客户端 |
| BE-P2-03 | P2 | README/security/progress/交接基线互相漂移 | 前后端会按错误 host、安全守卫或 query-token 方案联调 |

---

## 4. P0：后端冻结前必须修复

### BE-P0-01：并发 claim loser 仍进入业务处理，唯一索引没有实现单次生成

#### 证据

- `core/storage/idempotency_repo.py:128-185` 的 `claim()` 用 `None` 表示新 claim、已有文档表示 loser；
- `core/input/idempotency.py:106-137` 把两种结果都收敛成一个 `IdempotencyEntry`，丢失“本调用是否取得 claim”的信息；
- `core/input/ws_handler.py:731-775` 先 lookup、再 store，store 返回后只替换 `message_id`；无论返回的是新 entry 还是既有 entry，都会继续调用 `process_message()`；
- `voice.commit` 在 `core/input/ws_handler.py:965-991` 有同样问题，而且它在 claim 前已经消费录音状态并预占 session work。

当前注释写“并发同键时用实际生效的 message_id，不重复 generation”，代码实际只做到了“复用 messageId”，没有阻止第二个生成者。

并发时有两种错误：

1. **同 ID、同内容**：两个 handler 都触发 generation，恰好共用同一用户 messageId；
2. **同 ID、不同内容**：loser 没有在 claim 后重新比较 digest，可能用 winner 的 messageId 处理 loser 的文本。

另外，正常 Mongo 唯一键冲突会先经过 `Repository._call()`。`core/storage/base.py:148-156` 会把所有 driver 异常都视为连接错误并把 `_db` 置空；`IdempotencyRepository.claim()` 随后虽然能识别 `DuplicateKeyError` 并读回 winner，repo 自身却可能已经被标成 unavailable。由于 BE-P0-02 的恢复接线缺失，这次合法并发还可能让后续所有产品消息永久 503。

#### 必须修改

1. claim API 必须保留所有权结果，例如：

   ```text
   ClaimResult { entry, acquired: bool }
   ```

   或使用明确枚举 `ACQUIRED / REPLAY / CONFLICT`，不能再只返回 entry。
2. text 和 `voice.commit` 的 claim loser：
   - digest 相同：只重放原 ACK，立即 return；
   - digest 不同：ACK rejected `CONTRACT_VALIDATION`，立即 return；
   - 只有 `acquired=true` 才能进入附件 refs、`process_message()`、录音 commit 或 EventBus post。
3. 最好把 repo claim 改成单条原子 upsert（如 `$setOnInsert` + 可判断 `upserted_id` 的结果）；若仍使用唯一键冲突兜底，预期的 DuplicateKey 不能触发 `_mark_unavailable()`。
4. claim ownership 必须在 `voice_mgr.commit_recording()` 和 session work 预占之前确定，或保证 loser 完整回滚并不消费录音。

#### 验收测试

- 两个真实 v2 WS 同时发送同会话、同 clientRequestId、同内容：两边收到同 ACK，但 `process_message`/EventBus/generation 只能有一次；
- 同时发送同键、不同内容：一个 accepted，一个 `CONTRACT_VALIDATION` rejected；正文和 messageId 不串线；
- text、附件、sticker、`voice.commit` 四条路径都覆盖；
- 强制走 DuplicateKey 分支后，repo `health_check` 仍为 true，下一条不同请求可正常发送；
- 在内存降级路径也做同样并发测试，不能只测 Mongo fake 的顺序二次调用。

### BE-P0-02：IdempotencyRepository 没有进入完整存储生命周期

#### 证据

- 启动仅在 `core/agent/_lifecycle_persistence.py:91-97` 调一次 `connect()`，注释称“Mongo 恢复由健康循环重连”；
- 真实健康循环 `core/agent/_lifecycle_persistence.py:228-244` 的 probes 没有 idempotency；
- `_recover_dependency()` 没有 idempotency 分支；
- `_stop_persistence()` 和 `_stop_attachment_repo()` 都没有关闭 IdempotencyRepository；
- `/ready` 的 Mongo 判定 `core/input/server.py:273-280` 只含 session/message/attachment；
- `IdempotencyStore.configure()` 在 `core/input/idempotency.py:75-85` 只按 `repo is not None` 设置 durable，不读取 repo 是否已连接。

由此会形成以下可复现状态：

1. 后端在 Mongo 离线时启动；idempotency connect 失败；
2. 后续 Mongo 恢复，session/message/attachment 经 health loop 自动重连；
3. `/ready` 可返回 200；
4. idempotency repo 仍保持 `_db=None`；非回环产品模式的所有 v2 text/voice.commit 继续 rejected `STORAGE_UNAVAILABLE`，直至重启后端。

首次握手还有反方向的假阳性：repo 对象已经构造但 `_db=None` 时，`server_info.features.idempotency_durable` 先发 true；第一条消息失败后才变 false。已连接客户端也没有收到能力变化通知。

#### 必须修改

1. 把 idempotency 加入统一 persistence inventory：startup connect、60 秒健康探测、False→True 恢复、shutdown close 全部接线；
2. product mode 的 chat readiness 必须包含 idempotency；不能出现 ready=200 但所有 v2 用户消息都 503；
3. durable 初值由真实连接/索引状态决定，而不是 repo 对象是否存在；`connect()` 结果应显式传给 store；
4. 明确运行时能力变化策略：
   - 要么在 durable 状态变化时向 v2 客户端重发 `server_info`/专用 capability event；
   - 要么明确 `server_info` 是握手快照并要求客户端重连，同时在错误中给出最新状态；
5. DuplicateKey 等预期业务冲突不能使 repo 被健康系统判死；
6. restart/stop 后验证 Mongo client 已关闭且再次 start 可以重连。

#### 验收测试

- Mongo 离线启动 → server_info durable=false；product text rejected；
- 不重启进程，Mongo 恢复 → health loop 重连 → `/ready` 200 → text accepted → 新连接 server_info durable=true；
- 运行中断开再恢复同样通过；
- idempotency 单独失效而其他 repo 健康时 `/ready` 不能假绿；
- stop/restart 两轮无悬挂 client、无资源泄漏；
- 回环 dev 与非回环 product 两种模式分别覆盖。

---

## 5. P1：第一次正式联调验收前应修复

### BE-P1-01：持久 claim 没有可恢复状态机，残留记录会被重放成“幽灵 accepted”

#### 证据

`core/input/ws_handler.py:743-755` 对任何已存在、digest 相同的 entry 都直接发送 accepted ACK 并 return，不检查 entry.status，也不确认：

- 用户消息是否已经入队/落历史；
- generation 是否已经注册；
- 原请求是否其实被 `QUEUE_FULL`/`INTERNAL`/附件 refs 失败拒绝；
- 上次进程是否在 claim 后、业务提交前崩溃。

当前新请求顺序是“Mongo claim → process_message → ACK”；所以至少存在以下窗口：

1. claim 成功后进程崩溃，尚未调用 `process_message()`；
2. process 已入队但 ACK/`complete()` 前崩溃；
3. process 返回 false 或抛异常，`store.delete()` 又因 Mongo 抖动失败；
4. 附件 refs 预留失败，claim release 失败。

这些情况下 Mongo 会留下 `in_progress`。重启或恢复后重试同一个 clientRequestId，后端会回 accepted，但可能根本没有 user message/generation。`IdempotencyStore.delete()` 的注释称“条目保留仍可重放，安全方向”，但当前记录没有保存原 rejected ACK，实际会把 rejected 改写成 accepted。

#### 必须修改

1. 先定义并持久化真实状态机，例如 `CLAIMED / ACCEPTED / REJECTED`，不要把“ACK 已发送”和“generation 已完成”都叫 `completed`；
2. 持久化可重放的 ACK 结果（status、messageId、error code/message/retryable）以及必要的 generation/user-message 关联；
3. lookup 命中后按状态处理：
   - ACCEPTED：重放同一 accepted ACK，并可查询/回放 generation 状态；
   - REJECTED：重放原 rejected ACK，不能变 accepted；
   - CLAIMED 但业务未提交：执行恢复/接管，或返回明确 retryable 恢复错误，不能假 accepted；
4. 评估用事务/outbox 或 generation registry 做提交证据；仅依赖 1h TTL 会把幽灵请求隐藏一小时，不是恢复方案。

#### 验收测试

- 在 claim 后、process 前 kill；重启后同键重试不会“accepted 但无 generation”；
- 在 process 后、ACK 前 kill；重启后不重复生成且客户端能查到确定终态；
- QUEUE_FULL/INTERNAL 后强制 release 失败；恢复后重试仍是原 rejected 或安全重新执行，不能幽灵 accepted；
- accepted/rejected 两类 ACK 的重放字段逐字一致（时间字段策略需明确）。

### BE-P1-02：dev 内存降级不是连续幂等窗口

#### 证据

`core/input/idempotency.py:89-104` 的 lookup 逻辑是：

- Mongo 抛错时才查内存；
- Mongo 请求成功但 miss 时直接 return None，不再查内存。

`store()` 在 Mongo 成功时不写内存；在内存降级时写的 claim 也没有恢复同步。结果是：

- Mongo up → down：先前只在 Mongo 的 claim 不在内存，断线后的重试可再次生成；
- Mongo down → up：降级期 claim 仍在内存，但恢复后的 Mongo miss 直接覆盖内存事实，可再次生成。

这比文档写的“重启后不 durable”范围更大：**不重启，只要 Mongo 状态切换也会丢去重。**

#### 必须修改

从以下方案中明确选择一种并写入协议：

1. dev 一旦进入内存降级，本进程余下生命周期固定使用内存，重启才重新尝试 durable；或
2. durable 成功时同时写 bounded memory shadow，恢复时先 reconcile 内存 claim 到 Mongo，再切回 durable；或
3. lookup 做双读并定义冲突优先级，但仍需解决内存 claim 如何最终落库。

无论选择哪种，`idempotency_durable=false` 只能表示“跨进程不保证”，不应连同一进程的基本重试也失效；如果产品确实接受更弱语义，字段/文档必须换成能准确表达的状态，而不是 `durable` 一个布尔值。

#### 验收测试

- up→down、down→up、down→up→down 三种序列；
- 每段都用同一 clientRequestId 重试，process/generation 总数恒 1；
- 同键异内容在每种切换序列都稳定 rejected；
- 恢复时内存/Mongo 冲突有确定、可审计的处理规则。

### BE-P1-03：WS 消息限流错误无法关联到发送请求

#### 证据

`core/input/ws_handler.py:1013-1028` 在完整消息校验和 text ACK 路由之前做限流。超限时统一发送：

```json
{
  "type": "error",
  "code": "RATE_LIMITED",
  "requestId": "...",
  "retryable": true,
  "details": { "retryHint": "请稍后重试" }
}
```

该帧没有 `clientRequestId`。这里的 `requestId` 来自 ContextVar，当前限流请求尚未分配后端 request ID，因此它可能为空，也可能是同任务中上一条消息留下的 ID，不能用来关联本次发送。

Electron `RealBackend.sendMessage()` 以 `clientRequestId` 等待 `user_message.ack`。收到无关联的 generic error 后，pending send 仍然存在，最终只能走 ACK timeout。多个并发发送时，前端也无法可靠猜出哪一个被限流。

#### 必须修改

1. 对 `text` 和 `voice.commit`，限流应沿用其正式响应通道：

   ```json
   {
     "type": "user_message.ack",
     "status": "rejected",
     "messageId": "",
     "clientRequestId": "原值",
     "error": { "code": "RATE_LIMITED", "retryable": true, "message": "..." }
   }
   ```

2. 对 `switch_model`、`tool_approval` 等没有 ACK 的命令，给 ErrorMessage 增加明确的 `clientRequestId`/`correlationId`（对应客户端消息也要有 ID），不能复用日志 requestId 冒充业务关联；
3. 提供数字化 `retryAfterMs` 或等价字段；WS 建连 429 也应带 `Retry-After`，与 REST 一致；
4. 限流可以在重解析前，但应先做一个有严格长度/类型限制的 correlation ID 提取，不能因此执行完整高成本业务解析。

#### 验收测试

- text/voice.commit 超限立即 settle 对应 promise，不等待 15 秒；
- 两条并发发送只拒绝被限流的那条；
- rejected 带原 clientRequestId、RATE_LIMITED、retryable 和数值退避；
- 连接仍存活，退避后用同 clientRequestId 可安全重试。

### BE-P1-04：ACK 机器契约没有表达真实的 accepted/rejected 不变量

#### 证据

`frontend_contract/ws_message.py:310-320` 只有一个 `UserAckMessage`：

- `messageId: str` 必填但无最小长度；
- `clientRequestId: str` 必填但无最小长度；
- `status` 只有枚举；
- `error` 是可选的任意 dict。

因此生成 schema 同时允许：

- accepted + `messageId:""`；
- accepted + error；
- rejected + 非空 messageId；
- rejected + 无 error。

真实 handler 却在所有 rejected 路径发送 `messageId:""`。协议文字又写“rejected 不生成 messageId”，没有明确到底是“字段缺省”“null”还是“必发空串”。这正是 Electron 前端原先 `z.string().min(1)` 丢掉 rejected ACK、最终超时的根因之一。

同时，当前七类 golden 全是 accepted/happy path；后端的 `--check` 只能证明生成器与自己一致，不能给 Electron 提供负面 wire 基线。

#### 必须修改

1. 把 ACK 建模为 status 判别联合：`AcceptedUserAck` 与 `RejectedUserAck`；
2. accepted 强制非空 messageId、非空 clientRequestId；rejected 强制 typed error；
3. 拍板 rejected messageId 的 wire：
   - 为兼容当前前端批次 A，短期可明确规定 `messageId:""`；
   - 长期更干净的方案是 rejected 分支缺省 messageId，但这属于 wire 变更，需版本/迁移说明；
4. 用 `AckError{code,message,retryable,details?}` 取代任意 dict；
5. 新增至少以下 golden：CONTRACT_VALIDATION、SESSION_NOT_FOUND、QUEUE_FULL、STORAGE_UNAVAILABLE、RATE_LIMITED；
6. 让这些 frames 同时经过后端 schema 校验和 Electron raw parser/mapper，而不是两仓各写常量。

#### 验收测试

- schema 层拒绝所有 status/字段非法组合；
- handler 产生的每一种 rejected ACK 都能被生成契约直接校验；
- 前端直接 replay 后即时得到结构化失败，不走 ACK timeout；
- 修改 ACK wire 而不更新跨仓 fixture 时 CI 失败。

### BE-P1-05：干净启动时 active `default` 会话在 REST 不可发现

#### 证据

- `SessionManager.__init__` eager 创建 `default` 并把 `_active_id` 指向它；
- `load_active()` 只 ensure/load，不创建 `_local_summaries`；
- `_merged_summaries()` 在 `core/memory/session_manager.py:399-423` 只合并 Mongo 文档和 `_local_summaries`；
- 因而全新 Mongo/空 pending 目录时，`active_id=default` 但 `list()` 返回空；现有 `test_sessions_list_empty` 还把这一行为锁成了预期；
- `GET /api/sessions/default` 虽能通过 `has_session()`，但 `core/input/session_routes.py:460-464` 会兜底成 `updatedAt:""`；Electron REST schema要求非空字符串。

与此同时，WS 建连会发送 `session{conversationId:"default"}`。同一个后端因此同时声称：

- WS：活动会话是 default；
- REST list：没有任何会话，也没有 active 项；
- REST detail：default 存在，但 updatedAt 为空。

这会放大前端当前 FE-P1-03 bootstrap 问题：连接后 list 为空、`currentId` 为空，输入框不能发送。前端当然也要正确消费 session replay/处理空列表，但后端不应让活动会话在自己的会话 API 中不可发现。

#### 必须修改

推荐在 SessionManager 启动时为 eager default 建立合法本地 summary，并在 Mongo 可用时持久化；至少保证：

- 无 search 时，会话列表包含 `default` 且 `active=true`；
- `updatedAt` 是合法 UTC ISO 时间；
- 任意时刻 list 在会话非空时恰好一个 active；
- WS 首帧 conversationId、REST active 和 `agent.active_session_id` 同源。

如果产品决定 Electron 不展示 legacy default，则必须另行提供明确的 active/bootstrap 契约（例如 server_info 中的 `activeConversationId`）并规定前端在空列表时自动创建/激活；不能维持现在三套互相矛盾的结果。

#### 验收测试

- 空 Mongo + 空 pending 的真实 app：首次 GET sessions 可得到可发送的活动会话，或得到文档化的自动创建流程；
- default detail 的 `updatedAt` 非空且可解析；
- Mongo 离线、恢复、重启后三条接口仍一致；
- Electron 冷启动无需用户手工“新建会话”即可完成第一次发送。

---

## 6. P2：联调期间同步收敛

### BE-P2-01：liveness、readiness 与 Electron 可用能力没有分层

`/health` 标为 liveness 且恒 200，但 `core/input/server.py:256-280` 仍会依次等待 session、message、attachment、Milvus 的真实网络探测；每项上限 2 秒，慢而未超时的组合可让一次 liveness 请求等待数秒。外部探针如果自身 timeout 更短，可能把存活进程误杀。

`/ready` 又把 Milvus 作为整体门禁。当前 Agent 明确支持 Milvus 降级后继续文本聊天，因此“Electron 文本可用”和“完整长期记忆栈 ready”不是同一件事；反过来，BE-P0-02 的 idempotency 又没有纳入 ready。

建议：

1. `/health` 只做进程/事件循环/FSM 的快速 liveness，不访问外部网络；
2. `/ready` 的依赖并行探测，并定义它代表“完整服务”还是“核心 chat”；
3. 若两者都需要，增加 feature-scoped readiness（如 dependencies 细项或 chatReady/fullReady），让 Electron 不用猜；
4. 首次联调文档明确：`/` 404 只表示 v1 UI 关闭；Milvus down 不等于 WS 文本链路 down；product idempotency down 则确实阻断 v2 send。

验收：liveness 在依赖全部挂起时仍在很小固定预算内返回；readiness 负载能准确解释为什么文本可用/不可用。

### BE-P2-02：无效 Authorization 可绕过 IP 限流并制造高基数桶

限流中间件刻意挂在认证中间件之前；`rate_key_for_request()` 只要看到任意 Authorization 字符串，就使用其 SHA-256 摘要作为 auth key，不验证 Token 是否有效。

攻击者可每次发送一个不同的假 Bearer：

- 每次获得一个全新 token bucket，绕过按 IP 的认证失败/高成本入口限流；
- 最多填满全局 10,000 个 key；达到上限后新 key 被拒，可能影响合法新客户端；
- 这与代码注释中的“防凭据爆破/高基数 DoS”目标相反。

修复方向：只有认证成功的主体才按 auth principal 分桶；缺失或无效凭据统一按 remote IP（或单独 `invalid_auth:<ip>`）分桶。若仍需 rate middleware 先执行，可由一个不产生响应的轻量认证分类器提供 validated principal，或拆成连接/IP 前置限流 + 认证后主体限流两层。

验收：同 IP 轮换 10,001 个假 Token 不能获得 10,001 份配额，也不能挤掉合法主体桶；合法 Token 仍按主体隔离。

### BE-P2-03：后端公开资料仍有会误导联调的旧结论

至少存在以下漂移：

1. `docs/electron-backend-security.md:43-45` 仍称非回环无 Token 只告警、不拒绝，且默认 host=0.0.0.0；当前代码已经 `assert_secure_bind()` fail-closed，默认 host=127.0.0.1；
2. README 的配置示例仍写 `server.host: 0.0.0.0`，与实际默认和安全建议相反；用户照抄会突然触发远程绑定门禁；
3. `docs/archive/2026-08-10-backend-frontend-progress.md:203-214` 仍把浏览器 WS Bearer/query token 写成当前联调阻塞，而 `todo.md` 和最新提交已经明确：v1 chat 停用，Electron Node `ws` 使用 Authorization header，不做 query token；
4. 同一 progress 文档仍写 E2/E3、F 等旧状态，不能继续作为当前联调事实源；
5. `docs/archive/contract-release-2026-08-11.md` 的 commit 是契约生成时快照，不等于本次实际联调 runtime SHA；前端 manifest 也仍固定更旧后端 SHA。两者需要区分 `contractCommit` 与 `runtimeCommit`。

必须在冻结联调基线时同步：README、安全文档、progress、contract release/manifest、Electron change list，并新增一页最小联调 runbook，唯一列出：

- backend runtime SHA、schemaHash、frontend SHA；
- `SERVER_HOST`、端口、Token、Mongo/Milvus 条件；
- `SERVE_CHAT=0` 与 `/` 404 的正确含义；
- WS 三个请求头；
- clean-start 会话 bootstrap；
- ACK/generation 成功标准与负面用例；
- 日志 requestId/clientRequestId 对照方法。

---

## 7. 修复顺序建议

### 批次 A：D6c 正确性（先做）

1. BE-P0-01 claim ownership + DuplicateKey 不误判连接失败；
2. BE-P0-02 lifecycle/readiness/durable 初值；
3. BE-P1-01 持久 ACK/claim 状态与崩溃恢复；
4. BE-P1-02 dev failover 连续性。

这一批完成前，不应继续在文档中使用“跨重启 exactly-once”或“D6c 已封口”。准确说法应是“有持久唯一 claim，但并发 owner 与恢复状态机待修”。

### 批次 B：第一次联调契约

1. BE-P1-03 text/voice 的可关联 RATE_LIMITED；
2. BE-P1-04 ACK 判别契约 + 负面 golden；
3. BE-P1-05 clean-start 会话发现；
4. 用当前 Electron raw parser 对新增 golden 做跨仓 replay。

### 批次 C：运维与交接

1. BE-P2-01 探针分层；
2. BE-P2-02 限流认证主体分类；
3. BE-P2-03 文档和冻结 manifest；
4. 跑一次真实 Electron Main ↔ 后端 runtime 联调并保存脱敏证据。

## 8. 在修复完成前可进行的受控联调方式

为了不让后端整改阻塞前端继续推进，可以先做受控 happy-path 联调，但必须显式承认限制：

1. 后端绑定 `127.0.0.1:8765`；`SERVE_CHAT=0` 保持默认，`/` 返回 404 是正常现象；
2. Mongo 必须在后端启动前可用，联调期间保持稳定；若只测文本而 Milvus 离线，不要把整体 `/ready=503` 直接等同为 WS 不可用；
3. 建议仍配置 `AGENT_ACCESS_TOKEN`，Electron Main 使用：
   - `Authorization: Bearer <token>`
   - `X-Protocol-Version: 2`
   - `X-Client-Platform: electron`
4. 冷启动先显式 `POST /api/sessions` 创建会话并激活，或让前端从 WS `session.conversationId=default` 建立 currentId；不要假定空 REST list 会自动提供 active；
5. 每次发送使用全新 UUID clientRequestId，不并发发送同一个 ID，不在 ACK 未决时重传；
6. 保持在默认限流阈值以内；一旦收到 generic `RATE_LIMITED`，当前发送可能只能超时，联调记录需把它标为 BE-P1-03，而不是普通网络超时；
7. `server_info.features.idempotency_durable` 在第一条存储操作前可能误报，暂时不要把该字段作为 UI 强保证；
8. 联调通过标准至少记录 session → server_info → expression、REST 创建/列表、accepted ACK、generation 六事件、completed、历史读取和断开；但结果只标“happy path 已联调”。

## 9. 后端最终 DoD

后端可以重新宣称“足够对接 Electron”前，至少应满足：

- P0 全部修复；P1 有代码、测试、机器契约和真实 wire 证据；
- 同键并发、Mongo 启停/抖动、claim 后 kill、release 失败均不会重复生成或幽灵 accepted；
- `/ready` 与 product v2 send 可用性一致；server_info 不误报 durable；
- clean-start 有确定、跨 REST/WS 一致的活动会话；
- RATE_LIMITED 能关联到具体客户端请求并立即 settle；
- rejected ACK 判别 schema 与 handler 完全一致，负面 golden 进入 `--check`；
- 后端完整门禁通过：`make test-quick`、Ruff、Pyright、contract check、golden check；
- 前端用冻结的 backend runtime SHA + schemaHash replay，并完成至少一次真实 Main 进程联调；
- README/security/progress/todo/change-list 不再互相冲突。

## 10. 不应误记为后端缺陷的前端责任

为避免两边重复改错层次，本轮仍明确以下属于 Electron 前端：

- Endpoint 规范化、Token 与 Origin 绑定、Renderer 不能指挥 Main 向任意地址携带 Token；
- Real/Mock 装配策略和生产默认 Real；
- Renderer/IPC 的结构化错误传递、conversation/connection epoch、防迟到事件；
- UI 在空列表、断线、重连、取消、generation 失败时的状态恢复；
- 在后端 wire 变更前，前端应兼容当前 rejected ACK 的 `messageId:""`。

但 BE-P1-03、BE-P1-04、BE-P1-05 是双方交界处的后端契约问题，不能只让前端用 timeout、宽松 schema 或特殊 bootstrap 永久兜底。
