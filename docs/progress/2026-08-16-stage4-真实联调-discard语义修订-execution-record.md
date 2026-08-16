# 阶段 4 真实联调 discard 语义修订执行记录（2026-08-16）

## 批次状态

| 项 | 值 |
|---|---|
| 上游需求 | `docs/review/2026-08-16-阶段4真实联调后端整改清单.md`（BE-S4-RUN-P0-01：`voice.discard` 取消原语语义修订） |
| 前端对照 | `/home/yomu/elysia-frontend` `main@37a78e31`（配套清单 FE-S4-RUN-P0-01/P1-01） |
| spec | `docs/superpowers/specs/2026-08-16-stage4-discard-语义修订-design.md`（commit `80199a10`，**单一 spec commit**） |
| plan | `docs/superpowers/plans/2026-08-16-stage4-discard-语义修订.md`（commit `5c8b3a84`，**单一 plan commit**） |
| 执行 | 4 个 subagent 顺序执行 Task 1/2/3/4（SDD：单实现 subagent 不并行），每 Task 独立任务评审；主审查跑全量门禁 + 最终 whole-branch 评审（opus） |
| feature commits | `ff1fccd4`（Task1 状态机重构+10 单元测试）、`a96901bc`（Task2 3 个 WS 路由测试）、`4442d9be`（Task3 smoke 两真实序列 24→27）、`46492d26`（Task4 活跃文档同步） |
| 门禁 | test-quick **3631 passed** / test-normal **3739 passed**（基线 3617/3725）/ ruff All passed / pyright **0 errors** / check-types 全绿（golden `--check` **零 churn** + 错误码单源 41 码）/ smoke **27 检查全 PASS 退出 0** |

## 决策点定案（D0-D5）

| 决策 | 定案 | 理由 |
|---|---|---|
| D0 批组织/跨仓边界 | **A 后端单批** | 前端配套（FE-S4-RUN-P0/P1-01）+ Windows Electron 复验属 elysia-frontend；§5 完成定义明确"复验通过后才可标完整联调"，本批只可标「已开发/自动化已验证」 |
| D1 拒绝路径错误码 | **B 复用 `VOICE_SESSION_CONFLICT`** | IDLE-discard 改幂等后 `VOICE_RECORDING_NOT_FOUND` 语义自相矛盾；CONFLICT 精确描述"已有活动语音会话"，前端现象 A 已见并处理，错误目录零新增 |
| D2 golden 是否加样例 | **A 不动 golden** | `ws-v2-recording.jsonl` 只录后端→前端输出帧且无 discard 错误帧；成功 send-only 无帧可录；`--check` 零 churn（已实测） |
| D3 历史文档修订 | **A 只改活跃文档** | 就地修订 protocol-v2.md + batch6 spec + smoke 注释；历史 plan/execution-record 作史实不动 |
| D4 smoke 变更 | **A 改写 discard 段 + 空录音迟到删除** | 旧段是旧语义工作区（stop 后 discard 已无意义）；改写为 `start→discard` 才覆盖现象 A |
| D5 WS 路由级测试 | **A 加 discard 路由测试** | smoke 是 slow 不进 test-quick；把 review §3.6 场景 9/10 固化进日常门禁 |

批组织为单 P0 项单批；spec/plan 各单 commit，**无需 Phase 9 rebase**（前批 rebase 是为收敛多 commit；本批评审修复在提交前完成）。

## Review 记录

- **spec**：code-review 1 轮（general-purpose agent）结论 **no（无 HIGH，修后提交）**——3 MED + 1 MED/LOW 边界 + 3 LOW 全消化：**MED-1** §3.3 测试数「其余 25 个」与实际 45 个不符（实为仅 1 个测试触碰）；**MED-2** smoke 检查数「24→~26」与 plan「24→27」不一致；**MED-3** Task 4 漏 batch6 spec 错误码目录行 `:290`（含「IDLE 态收 discard」旧措辞）；**MED-4** batch6 plan `:761` 也含旧语义残留须列入史实不动。LOW×3（stop_recording 行归属、§7 item 11 显式化、Task 2 表编号）。
- **plan**：code-review 1 轮结论 **no**（2 MED + 2 LOW，无 HIGH）——**MED-1** smoke helper `ws` 参数未类型注解（ruff ANN001，`scripts/` 在 lint 路径 → `make lint` 必挂）；**MED-2** Task 4 grep 命中集预期不准确（误报 execution-record:37、漏报本批 plan/spec/review 自引用）。LOW×2（smoke 注释 TRANSCRIBING→PENDING、红测措辞）。另有主审查自查 Task 2 WS 测试 `errs` 列表在 `start(B)` 后未重算（stale 断言）→ 提交评审前修复。
- **最终 whole-branch 评审**（Phase 8，opus）：结论 **Ready to merge: Yes**——无 Critical/Important，独立复核（ruff 全过 + 84 targeted tests + 活跃文档 grep 干净）+ 状态机 6 行转场表逐行核实（IDLE 先返、CONFLICT 分支触碰状态前 raise、零 await 无 TOCTOU、`_rec_state != TRANSCRIBING` 防线保留）。5 Minor 全 triage safe（显式 cancel 与 `_reset_recording` 重复 / WS 测试缺 `mgr.close()` 不泄漏 / `_drain_errors` 与 `ack2` 理论弱化 / `stage4_smoke.py:298` 否定式命中 / `:390` 「。」紧接 backtick）；1 可选 recommendation（TRANSCRIBING-mismatch 测试）parked——与既有 mismatch 测试同代码路径，不扩 scope。

## 执行偏差（已解决/Ruling）

- **Task 2 Red 阶段不可行（impl 发现 + reviewer 认可）**：plan Step 2「确认红」——Task 1 已先落地新语义，WS 测试首跑即绿；红测仅在 Task 1 内成立。该偏差已记录，无语义遗漏。
- **Task 4 grep 命中集偏差（Ruling）**：plan 预期活跃文档零命中，实际 `stage4_smoke.py:298`「不再『先 stop 再 discard』」**否定式注释**命中（Task 3 引入）。Ruling：否定式引用陈述新语义不再要求旧规则，语义正确保留；验证意图（活跃文档无**过期**语义）满足。
- **spec/plan 评审修复前置**：两轮评审的 MED/LOW 全部在提交前于工作树消化（不同于前批提交后修复→Phase 9 收敛），故 commit 链天然为单一 spec/plan commit。
- **subagent 可靠**：4 个实现 subagent + 6 个评审 subagent 全部按时返回，无卡住。
- **门禁范围**：实现 agent 各自跑定向测试（55/29/smoke 27）；主审查跑全量 test-quick/test-normal + ruff + pyright + check-types + smoke。

## 验收结论（Phase 10，subagent 独立验收）

- **结论：本批可标记「阶段 4 已开发/自动化已验证」（整改清单 §5）；不得标记「完整真实联调通过」**——Windows Electron 真实复验（前端配套 FE-S4-RUN-P0-01/P1-01 落地 + 双方 SHA 固定后）未执行，属 elysia-frontend + 联调作业另行推进。
- 8 个维度全 PASS，全部关键门禁独立重跑核实（非采信）：① 生产状态机语义（`discard_recording()` 六行转场 + IDLE 先返 + CONFLICT 不拆会话 + 零 await 无 TOCTOU + `_run_recording_stt` 防线保留）；② 测试覆盖（55 unit + 29 service，+10/+3，零 mock 断言真实行为，marker 正确，§3.6 11 场景全映射）；③ 门禁复核（84 targeted + smoke 27/0 退出 0 + **test-quick 3631 精确复现** + ruff + pyright 0 + check-types 三子项）；④ golden 零 churn（`ws-v2-recording.jsonl` 未变 + `--check` 退出 0 + 41 码单源 + ERR 常量与基线 IDENTICAL）；⑤ 文档同步（活跃文档新语义 + 历史文档零触碰）；⑥ 范围纪律（8 文件精确 + golden/Makefile/providers.yaml/frontend_contract 零改动）；⑦ commit 完整性（spec/plan 单 commit + feature 链线性逐 commit 文件核对）；⑧ 执行记录核对（数字精确复现 + 修复项已落）。
- **备注**：test-quick +14 vs 本批新增 13——基线 fc5b45ec 相对上一批验收计数 3617 已有前序 commit 微移（模块化/readme 刷新批次），非本批多出测试；当前值 3631 被精确复现，不影响结论。test-normal 3739 按指示采信主审查未独立重跑。

## 后续衔接

- 后端修复 SHA `46492d26` 交前端（elysia-frontend FE-S4-RUN-P0-01/P1-01：清理 helper 收敛 + 有状态协议桩测试）；
- 双方 SHA 固定后 → Windows Electron + Ubuntu 后端**真实复验**：采集启动失败后无需重连即可再次录音、空录音删除不再产生 `VOICE_RECORDING_NOT_FOUND`/`CONTRACT_VALIDATION`；
- 复验通过前，阶段 4 只可标记「已开发/自动化已验证」，**不得**标记「完整真实联调通过」。