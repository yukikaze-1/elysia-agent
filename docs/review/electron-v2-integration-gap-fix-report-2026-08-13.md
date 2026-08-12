# Electron v2 首次联调封口问题修复报告

**日期：** 2026-08-13

**后端仓库：** `/home/yomu/elysia-agent`

**实现基线：** `main@da8a53c` 上的当前工作区改动

**前端配套报告：** `/home/yomu/elysia-frontend/docs/review/2026-08-13-首次联调封口问题修复报告.md`

## 1. 结论

本轮后端复核发现的两个持久化幂等恢复竞态已修复：

1. `restore()` 输掉并发 upsert 后，Mongo winner 不再只被读出后丢弃；winner 会返回给 `recover()` 并刷新内存镜像。
2. `recover()` 不再用“恢复期间是否新增 key”判断能否清除补偿状态；改为单调 recovery epoch，因此同一 key 的 `complete/reject` 写穿失败也不会被本轮恢复误清。

新增回归测试已先确认旧实现失败，再随修复转绿。相关专项、全仓 unit/service 快速套件、Ruff、Pyright 和契约漂移门禁全部通过。结合前端同批修复，当前前后端具备开始 **Linux 本机文本 B-01 首次真实联调** 的代码条件。

这不关闭 `BE-RISK-01`。accepted ACK 与业务工作持久化之间仍有进程内 EventBus 窗口，本批没有实现 outbox 或启动重入队，因此不能宣称生产级 exactly-once。

## 2. 修复总表

| 项目 | 原问题 | 修复 | 状态 |
|---|---|---|---|
| BE-FIX-01 | `restore()` 遇 E11000 后虽然读回 Mongo winner，但返回 `None`；`recover()` 继续保留本地 loser 并可能翻 durable | `restore()` 返回实际生效记录；冲突分支返回 winner；`recover()` 用返回值刷新 overlay | 已关闭 |
| BE-FIX-02 | `recover()` 仅比较 key 集；同 key 的并发 `complete/reject` 写穿失败不新增 key，补偿位可被误清 | 每次新补偿事实递增 `_recovery_epoch`；仅 epoch 未变化时清 `needs_recovery` 与 fallback | 已关闭 |
| BE-RISK-01 | accepted 已落幂等表、但 generation 尚未建档时 kill，EventBus 中工作可丢失 | 本批未改架构；继续显式记录 | 开放的接受风险 |

## 3. BE-FIX-01：恢复采用实际 Mongo winner

### 3.1 原竞态

两个恢复者可能执行以下顺序：

1. 恢复者 A `get()` 看到记录缺失；
2. 恢复者 B 先写入同一 `(conversation_id, client_request_id)` 的终态 winner；
3. A 的条件 upsert 因唯一索引触发 E11000；
4. repository 防御层读回 winner，但旧接口不返回它；
5. store 仍保留 A 的内存 loser，却可能把 durable 翻回 true。

### 3.2 当前行为

`IdempotencyRepository.restore(doc)` 现在返回 `dict`：

- insert 或 claimed→终态升级成功：返回输入完整记录；
- E11000：按身份键读回并返回 Mongo winner；
- 冲突后查不到 winner：重新抛出原冲突，无法证明完成时 fail-closed。

`IdempotencyStore.recover()` 总是用 `restore()` 的返回记录重建本地 `IdempotencyEntry`。因此内存、Mongo 与 durable 声明保持同一 winner。

## 4. BE-FIX-02：用恢复版本代替 key 集清位

旧守卫只记录恢复开始时的 key 集。恢复 await 间隙若已有 key 的 `complete()` 或 `reject()` 写 Mongo 失败，key 集不变，但内存已经新增一条“超前 Mongo 的终态事实”。旧逻辑会在循环结尾错误清除 `needs_recovery`。

当前 store 增加单调 `_recovery_epoch`：

- dev 降级新增内存 claim 时递增；
- 现有内存 entry 的 `complete/reject` 写穿失败时递增；
- `recover()` 开始时快照 epoch；
- 只有循环结束时 epoch 完全未变化，才同时清除 `_in_memory_fallback` 与 `_needs_recovery`；
- epoch 变化时保留非 durable 状态，由健康循环下一轮再次 reconcile。

`reset()` 会清零 epoch，保持测试与重新配置隔离。

## 5. 回归测试

新增或强化三条关键测试：

1. `test_restore_terminal_winner_not_overwritten`：强制走真实 E11000 防御分支，断言 repository 返回的记录就是 Mongo winner。
2. `test_recover_refreshes_memory_from_restore_winner`：模拟 get-first 后另一恢复者提交 rejected winner，断言本地 message/status/error 全部切换到 winner。
3. `test_recover_keeps_flag_when_same_key_terminal_write_fails`：在 `recover()` await 间隙让同 key `complete()` 写穿失败，断言本轮后 `needs_recovery=true/durable=false`，下一轮恢复后才清位。

所有 repository fake 的 `restore()` 签名同步为返回实际记录，避免测试替身掩盖生产接口变化。

## 6. 验证证据

| 门禁 | 结果 |
|---|---|
| 三条新增/强化回归 | **3 passed** |
| 幂等、生命周期、WS v2、限流、健康、会话、ACK 专项 | **271 passed** |
| `make test-quick` | **3510 passed, 2 skipped, 183 deselected** |
| Ruff（`core/ Module/ frontend_contract/ scripts/`） | 通过 |
| Pyright | **0 errors, 0 warnings** |
| `python -m frontend_contract --check` | 通过 |
| `scripts/gen_contract_frames.py --check` | 通过 |
| `scripts/check_error_codes.py` | 41 个公开错误码一致 |
| `git diff --check` | 通过 |

测试仅出现既有 `webrtcvad` 对 `pkg_resources` 的弃用 warning；没有新增 warning 或失败。

## 7. 联调与发布口径

### 可以开始

- Linux 本机文本首次联调；
- 会话列表/激活、发送、accepted/rejected ACK、流式 generation、completed/failed、取消、断线重连与结构化错误链；
- Mongo 正常、dev 降级后恢复以及相关 idempotency durable 观测。

### 尚不能宣称

- 已完成 B-01：仍需启动真实前后端、冻结双方 SHA 并保存真实运行证据；
- 所有前端 manifest 已验证：`verified/backendCommit` 必须由真实联调更新；
- 生产级 exactly-once 或 accepted 请求绝不丢失：`BE-RISK-01` 需要 outbox/可恢复 generation 证据和启动重入队机制。

当前准确状态为：**后端本轮代码缺陷已关闭、门禁全绿、可以开始首次真实联调；架构接受风险继续开放。**
