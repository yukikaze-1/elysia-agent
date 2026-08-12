# Electron v2 后端加固后复核与继续整改清单

**日期：** 2026-08-12

**审查对象：** `/home/yomu/elysia-agent` `main@96623b599e63599b501f24dae385aebd361f550c`

**对照基线：** `main@b5b7e6e057878342066310123ace2d2fc3ecf97b`（上一份首次联调前后端复核报告的后端基线）

**范围：** 后端加固批次落地后的持久化幂等恢复、存储生命周期、accepted 提交边界和后端联调文档口径。

**不在本文件范围：** Electron 前端阶段 0/1、前端运行时 Schema、前后端字段消费差异和 UI 行为。上述内容待前端阶段 0/1 整改完成后另行复核，避免本轮后端任务混入跨仓工作。

**关联文档：**

- `docs/review/electron-v2-first-integration-backend-review-2026-08-11.md`
- `docs/superpowers/specs/2026-08-11-electron-v2-backend-hardening-design.md`
- `docs/electron-v2-first-integration-runbook.md`
- `todo.md`

## 1. 结论

本轮后端加固已经实质关闭上一份报告中最危险的并发 claim 所有权问题，ACK 三态、限流关联、默认会话发现、readiness/liveness 和契约负面帧也有明显改进。当前代码的稳定 Mongo、单实例文本 happy path 已具备联调主体条件。

但不能把后端标记为“全部封口”或“D6c 故障恢复完成”。本轮通过代码审查和故障注入又确认两个需要继续修复的 P1：

1. **BE-P1-06：** dev 内存降级记录补落 Mongo 时，只恢复 claim 的最小字段，accepted/rejected 终态 ACK 的完整重放负载会丢失；进程重启后 rejected 可能被改写成 `INTERNAL / 未知错误`。
2. **BE-P1-07：** Mongo 离线启动后，如果在健康循环第一次探测前已经恢复，首轮健康结果直接为 true，恢复 hook 不会执行；降级窗口的内存幂等记录可能永远不补落 Mongo。

另有一个已经由设计明确接受、但必须持续记录的提交窗口风险：accepted ACK 目前证明的是消息已进入进程内 EventBus，不等于业务工作已经持久化。它不阻塞第一次 happy-path 联调，但阻止“生产级 exactly-once / accepted 请求必不丢失”的结论。

后端联调 Runbook 还有一处确定的自身口径错误：文档要求 `server_info` 携带 `schemaHash`，实际 `ServerInfoMessage` 没有该字段。跨仓字段扩展暂不在本轮做，后端至少应先修正文档，避免用不存在的 wire 字段作为验收条件。

建议当前状态统一写为：

> 后端正常文本链路和并发 claim 主路径已加固；Mongo 降级恢复仍有终态负载丢失和首次健康探测漏补偿两个缺口。修复并完成跨重启故障注入前，只能认定为 happy-path 联调候选，不能认定 D6c 故障恢复或生产级 exactly-once 已封口。

## 2. 本轮验证

### 2.1 已通过门禁

在 `Elysia` conda 环境执行：

```bash
python -m pytest \
  core/input/tests/test_idempotency_durable.py \
  core/agent/tests/test_lifecycle_idempotency.py \
  core/input/tests/test_v2_ws.py \
  core/input/tests/test_rate_limit.py \
  core/input/tests/test_health.py \
  core/input/tests/test_session_routes.py \
  frontend_contract/tests/test_task6_ack_union.py -q
```

结果：**230 passed**。

以下生成/漂移检查也通过：

```bash
python -m frontend_contract --check
python scripts/gen_contract_frames.py --check
git diff --check
```

这些结果说明当前实现与自身测试、生成产物一致；它们没有覆盖下文两个“离线内存终态 → Mongo 恢复 → 进程重启”的组合边界。

### 2.2 已确认修复、后续不得回退

- `IdempotencyRepository.claim()` 已返回 `ClaimResult{entry, acquired}`；并发 loser 不再进入 `process_message()`。
- DuplicateKey 已按预期竞争处理，不再作为普通存储断线把 repo 判死。
- idempotency 已进入 startup、health、recover、shutdown 生命周期；产品模式 readiness 纳入持久化幂等依赖。
- claim 已采用 `CLAIMED / ACCEPTED / REJECTED` 三态；accepted/rejected ACK 使用判别联合并有负面 golden。
- text/voice 的 `RATE_LIMITED` rejected ACK 已回显 `clientRequestId`，命令类 error 也具有关联字段。
- clean-start 的 `default` active 会话已能通过 REST 发现。
- `/health` 与 `/ready` 已拆分，认证前限流不再按未经验证的任意 Token 值分桶。

本文件不是要求推翻这些设计；新修复必须保留以上行为和既有测试。

## 3. 优先级总表

| ID | 优先级 | 类型 | 问题 | 直接影响 |
|---|---:|---|---|---|
| BE-P1-06 | P1 | 后端缺陷 | `IdempotencyStore.recover()` 只补最小 claim，终态 ACK 负载未落库 | 重启后 ACK 不再逐字一致；rejected 可变成 `INTERNAL / 未知错误` |
| BE-P1-07 | P1 | 后端缺陷 | 离线启动后首轮健康探测直接成功时不触发 recover | 内存 claim/终态不 durable；后续崩溃可丢失去重与重放证据 |
| BE-RISK-01 | 已接受风险 | 架构边界 | accepted 持久化发生在进程内 EventBus 入队后、generation 建档前 | 极端 kill 下可能“已 accepted、无 generation、工作丢失” |
| BE-P2-04 | P2 | 后端文档 | Runbook 声称 `server_info` 带 `schemaHash`，实际模型不带 | 联调按不存在的字段验收，形成假失败或临时错误实现 |

## 4. BE-P1-06：恢复补落只保存最小 claim，终态 ACK 负载丢失

### 4.1 证据与根因

`core/input/idempotency.py:246-265` 的 `recover()` 遍历内存 overlay。Mongo 没有对应文档时，仅调用：

```python
await self._repo.claim(
    conversation_id,
    client_request_id,
    content_digest=entry.content_digest,
    message_id=entry.user_message_id,
    status=entry.status,
    claimed_at=...,
    expires_at=...,
)
```

而 `core/storage/idempotency_repo.py:257-265` 的 claim 插入文档只有：

- `conversation_id`
- `client_request_id`
- `content_digest`
- `message_id`
- `status`
- `claimed_at`
- `expires_at`

它不会保存 `IdempotencyEntry` 中用于终态 ACK 重放的字段：

- `received_at`
- `client_message_id`
- `conversation_id_wire`
- `error`

即使 `entry.status` 被直接写成 `accepted` 或 `rejected`，Mongo 中也只是一个“带终态标签的残缺 claim”，不是完整终态。

进程仍在时，`lookup()` 内存优先，所以这个缺陷暂时被内存 overlay 掩盖。进程重启后，`IdempotencyEntry.from_doc()` 只能得到缺字段的终态：

- accepted 重放会用本次重试时间代替原 `receivedAt`，并回退使用本次传入的 client/conversation 字段；
- rejected 重放得到 `entry.error is None`，`core/input/ws_handler.py:571-576` 会构造 `AckError(code="INTERNAL", message="未知错误", retryable=False)`。

这与当前注释和设计文档中的“持久化字段逐字一致重放”直接冲突。

### 4.2 故障注入结果

场景：Mongo 不可用 → dev 内存降级保存并 reject 一条 `RATE_LIMITED` 请求（含 `retryAfterMs`）→ Mongo 恢复 → `store.recover()`。

实际补落文档只包含：

```text
conversation_id, client_request_id, content_digest, message_id,
status=rejected, claimed_at, expires_at, _id
```

缺少：

```text
error, received_at, client_message_id, conversation_id_wire
```

因此“recover 当下 durable=true”并不等于“该内存终态已经可跨重启完整重放”。

### 4.3 必须修改

推荐采用一个明确的原子/幂等恢复入口，例如 `IdempotencyRepository.restore(entry)` 或 `upsert_full_entry(...)`，一次性表达完整记录；不要继续让调用方用“先 claim、再猜测是否补 complete/reject”拼装恢复语义。

无论最终 API 形态如何，必须满足：

1. Mongo 无记录且内存为 `CLAIMED`：补落完整 claim。
2. Mongo 无记录且内存为 `ACCEPTED`：补落 accepted 所需的完整 ACK 字段。
3. Mongo 无记录且内存为 `REJECTED`：补落 rejected 的完整 `error`，包括 `details`、`retryAfterMs` 等可选字段。
4. Mongo 已有记录：继续保持 Mongo 优先；不能用旧内存覆盖另一个进程已提交的 winner。
5. 并发恢复遇到 DuplicateKey：读回 Mongo winner 并刷新内存，不把 repo 判为 unavailable。
6. 任一步补偿失败：本轮 recover 必须失败并保持 `durable=false`，供健康循环下轮重试；不能在部分终态未落完整时提前翻 durable。
7. 过期内存记录继续遵守现有“不复活”规则。

如果选择保留现有两步 API，最低限度也要在 `claim(... acquired=True)` 后按内存状态调用 `complete()` 或 `reject()` 写入完整负载，并确保中间失败不会把 store 标为 durable；但单条完整 upsert 更容易证明崩溃边界。

### 4.4 验收测试

至少新增以下测试，且必须模拟“清空进程内 store 后从 Mongo 重新读取”，不能只在 recover 后继续命中原内存：

1. `test_recover_claimed_entry_survives_store_restart`；
2. `test_recover_accepted_entry_replays_identical_ack_after_restart`；
3. `test_recover_rejected_entry_replays_identical_ack_after_restart`；
4. rejected 分别覆盖 `RATE_LIMITED.retryAfterMs`、`STORAGE_UNAVAILABLE.details.idempotency_durable`；
5. recover 完成前在完整终态写入处注入失败，断言 `store.durable is False`，下一轮可继续补偿；
6. 两个恢复者竞争同键，Mongo winner 不被 loser 覆盖，repo 保持健康；
7. accepted/rejected 重放的完整 JSON 与首次 ACK 比较，不只断言 status/code。

## 5. BE-P1-07：首轮健康探测直接恢复时漏掉补偿

### 5.1 证据与根因

`core/agent/_lifecycle_persistence.py:224-266` 的恢复协调器从空字典开始：

```python
prev: dict[str, bool] = {}
```

健康探测成功后，恢复 hook 的条件是：

```python
if name not in prev or prev[name]:
    continue
```

即首轮结果只建立基线，不触发恢复。现有测试 `core/agent/tests/test_lifecycle_idempotency.py:197-228` 也明确建模为：

```text
第 1 轮 False → 写入 prev
第 2 轮 True  → 观察到 False→True，执行 recover
```

但真实 repository 的 `health_check()` 会在内部尝试重连（`core/storage/base.py:93-117`）。因此存在以下实际时序：

1. 后端启动时 Mongo 离线，IdempotencyStore 进入内存降级，`durable=false`；
2. 用户请求在内存中形成 claim/accepted/rejected；
3. Mongo 在第一次 60 秒健康探测前恢复；
4. 第一次 `health_check()` 自己重连成功并返回 true；
5. 因 `name not in prev`，协调器跳过 `_recover_dependency("idempotency")`；
6. `prev[idempotency]` 从此为 true，后续轮次也不会触发恢复。

故障注入确认首轮直接 healthy 后的结果为：

```text
store.durable = False
mongo_doc = None
recover/connect hook calls = 0
```

只要后续没有另一轮明确的 down→up，降级期内存记录就不会补落 Mongo。

### 5.2 必须修改

不要只用进程内 health-loop 的 `prev` 推断“是否需要补偿”。恢复条件还应包含依赖自身的补偿状态。

推荐方案：

1. 对 idempotency：只要 probe 为 true 且 `IdempotencyStore.durable is False`，就执行 `_recover_dependency("idempotency")`；recover 成功后 durable 才翻 true。
2. 或在启动连接结果中保存显式初始 health 状态，让 health loop 的 `prev` 从 startup 真实结果初始化，而不是从 `{}` 开始。
3. 更通用的做法是让每个依赖暴露 `needs_recovery`，健康与补偿完成分开建模；“可以 ping”不等于“降级期状态已对齐”。
4. 补偿失败必须保持待恢复状态，并在下一轮重试，即使 probe 一直为 true。
5. 恢复 hook 应保持幂等，允许在不确定边界重复调用。

其中方案 1 对本问题改动最小、语义最直接；如果统一协调器准备继续扩展，建议采用方案 3。

### 5.3 验收测试

1. Mongo 离线启动，内存保存一条记录；第一次 health round 直接返回 true，断言当轮执行 recover、记录已落库、durable=true。
2. 第一次 recover 失败、第二轮 probe 仍为 true，断言第二轮继续 recover，而不是因 `prev=true` 永久跳过。
3. 启动时即健康且没有降级记录，不产生破坏性重复补偿。
4. 运行中 true→false→true 的既有恢复测试继续通过。
5. 与 BE-P1-06 组合：首轮直接恢复 accepted/rejected，模拟进程重启后 ACK 仍逐字一致。
6. `/ready` 和新连接 `server_info.features.idempotency_durable` 只能在补偿完成后反映 durable=true。

## 6. BE-RISK-01：accepted 仍不是持久化业务提交证据

### 6.1 当前顺序

文本路径当前大致为：

```text
持久化 CLAIMED
→ Agent.process_message()
→ EventBus.post(UserInputData)（进程内队列）
→ IdempotencyStore.complete() 持久化 ACCEPTED
→ 发送 accepted ACK
→ CognitiveLoop 稍后消费事件
→ GenerationRegistry.register() 建档
```

代码位置：

- `core/agent/agent.py:399-409`：`process_message()` 只完成进程内 EventBus post 即返回 true；
- `core/input/ws_handler.py:1069-1076`：随后 complete-before-ACK；
- `core/cognitive/loop.py:493-523`：generation 证据要到 loop 真正开始处理时才建档。

因此在 `store.complete()` 成功后、loop 执行 `register()` 前 kill 进程，会留下 ACCEPTED 幂等记录，但 EventBus 工作已经随进程消失，Mongo 中又没有 generation 证据。重试同 clientRequestId 会重放 accepted，不会重新入队。

### 6.2 本轮要求

该窗口已在 `docs/superpowers/specs/2026-08-11-electron-v2-backend-hardening-design.md:71` 明确记录为接受风险，本轮不把它升级成首次 happy-path 联调阻断项，也不要求在两个 P1 修复中顺手做大规模 outbox 重构。

但必须遵守：

1. `todo.md`、加固报告和联调结论不得声称 production exactly-once 或“accepted 后任务绝不丢失”。
2. 首轮联调通过只能标记 happy path，不得关闭该风险。
3. 如果后续要生产封口，需要独立设计持久化 ingress/outbox，或在 accepted 前建立可恢复的 generation/work 证据，并提供启动恢复重新入队机制。
4. 未来实现时必须同时证明“不丢工作”和“不重复 generation”，不能只把 register 提前而产生新的幽灵 running generation。

## 7. BE-P2-04：Runbook 使用了不存在的 `server_info.schemaHash`

### 7.1 证据

`docs/electron-v2-first-integration-runbook.md:60` 和 `:125` 把握手帧写成包含 `schemaHash`：

```text
server_info（features/version/schemaHash）
```

但 `frontend_contract/ws_message.py:277-295` 的 `ServerInfoMessage` 实际字段只有：

- `type`
- `protocol_version`
- `server_version`
- `features`
- `services`
- `connectionId?`
- `build_sha?`

当前 `schemaHash=7a5348a4767a9fc6` 存在于 `frontend_contract/contract.schema.json` 和生成产物头部，不在运行时 `server_info` wire 中。

### 7.2 本轮建议

前端和跨仓契约调整按用户要求暂缓。因此本轮后端先修正 Runbook：

1. 删除 `server_info` 运行时携带 `schemaHash` 的说法；
2. 把 schemaHash 验证明确写成联调前离线门禁：`python -m frontend_contract --check`，并记录冻结产物的 hash；
3. 保留 runtime backend SHA、frontend SHA 的联调前回填项；
4. 如果未来确实需要握手时协商 schemaHash，另起跨仓契约变更，不要只改文档或只让后端私自加字段。

## 8. 后端继续整改的推荐顺序

### 批次 A：恢复正确性（必须先做）

1. 修复 BE-P1-06，建立完整记录恢复 API；
2. 增加 accepted/rejected 跨 store 重启逐字重放测试；
3. 注入恢复中途失败和并发恢复；
4. 保证未完整补偿时 durable 不翻 true。

### 批次 B：恢复触发条件（必须先做）

1. 修复 BE-P1-07 的首次 healthy 漏补偿；
2. 增加首轮 true、连续 true 重试和组合跨重启测试；
3. 复核 `/ready` 与 `server_info.features.idempotency_durable` 的状态时机。

### 批次 C：文档收口

1. 修复 BE-P2-04 Runbook；
2. 更新 `todo.md`：BE-P1-06/07 完成前不得继续写“全部 P0/P1/P2 已闭环”；
3. 保留 BE-RISK-01 的 accepted 残余窗口；
4. 记录最终后端 commit、测试命令和故障注入输出。

## 9. 完成门禁

后端只有同时满足以下条件，才能把本轮两个 P1 标记为完成：

1. BE-P1-06、BE-P1-07 的所有组合故障测试通过；
2. 必须包含清空/重建 IdempotencyStore 后从 Mongo 读取的断言；
3. 首轮 health probe 直接 true 和 recover 连续失败后重试均有测试；
4. accepted/rejected ACK 比较完整 wire JSON，包含时间、clientMessageId、conversationId、details、retryAfterMs；
5. 既有并发 claim、DuplicateKey、三态恢复、限流、default session、readiness 测试不回归；
6. `python -m frontend_contract --check` 与 `python scripts/gen_contract_frames.py --check` 通过；
7. Runbook 不再要求当前 wire 中不存在的字段；
8. `todo.md` 和最终报告诚实区分“已修缺陷”与“BE-RISK-01 接受风险”；
9. 至少复跑本文件 §2.1 的 230 项定向测试；提交前再按仓库门禁执行相应 quick/normal 测试。

在上述门禁完成之前，建议联调状态保持：

> **后端已开发、加固中；可供受控 happy-path 准备，不作为 D6c 故障恢复冻结基线。**
