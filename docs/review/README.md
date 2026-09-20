# Review & Execution History

> 文档导航：[项目文档](../../README.md) → [docs 索引](../README.md) → 本文

> ⚠️ **Historical evidence.** 本目录下的文档记录**当时**的系统与**当时**的结论。
> 其中一些结论后来被证伪或被后续批次修正（例如 Batch 1 的"缺陷 C"断言被 Batch 1 自己
> 的复现否定、`N-8` 的编号漂移）。**当前事实一律看 canonical docs**：
> [architecture.md](../architecture.md) · [architecture/invariants.md](../architecture/invariants.md) ·
> [testing.md](../testing.md) · [ci.md](../ci.md) · [operations.md](../operations.md) ·
> [decisions.md](../decisions.md) · [todo.md](../../todo.md)。
>
> **本目录的正文不得改写**：不改结论、不删失败的实验轨迹、不重写 Challenger 的反证。
> 允许的动作只有两种：修导航（坏链接）、加 banner/index 元数据。

## Batch 编号碰撞（读旧文档必看）

**"Batch N" 在本仓至少有两个互不相关的命名空间**，同为 `Batch 1..9` 且时间重叠：

| 命名空间 | 出处 | 范围 | 内容 |
|---|---|---|---|
| **报告 Roadmap** | [`CODE_REVIEW_REPORT.md`](../../CODE_REVIEW_REPORT.md) 的 `# Remediation Roadmap`（2026-09-12） | Batch **1–9** | 按严重度排的修复建议：1 Production Risks（P0）、2 Shutdown & Recovery、3 Data Integrity、4 Concurrency & Failure Paths、5 Workflow Engine、6 Input Protocol & Text Pipeline、7 Storage Consistency、8 Ops & Supply Chain、9 Architecture & Tests |
| **Correctness Hardening** | 本目录的 `2026-09-1x-BatchN-*-执行记录.md`（2026-09-16 ~ 09-20） | Batch **1–20**（含 1.5/1.6/1.7/1.8/3.5/3.6） | 逐批的缺陷复现 → 修复 → 守卫 → 不变量落纸；见下方表 |

**本文一律用 `Hardening Batch N` 指称后者**，以免与报告 Roadmap 混。
报告自身还有第三个空间：正文的 `CR-01..CR-13` 与
[`2026-09-12-code-review-ledger.md`](2026-09-12-code-review-ledger.md) 的 `CR-01..CR-49`
**编号相同、含义不同**。

其它不是"hardening batch"的批号：Electron 产品批次 `0–8c`、契约批次 `9A/9B/9C`、
输入层拆分批次 `A/B/C/D`、以及**没有独立文件**的 hardening 子批 `2.1 / 2.2 / 2.3`
（它们只存在于 `2026-09-17-Batch2-guardrails-hardening-执行记录.md` 内部章节与
[ci.md](../ci.md) / [code-style-guide.md](../code-style-guide.md) 的引用里）。

## Correctness Hardening Program（Batch 1 → 20）

每个批次只一行。**当前最终状态见 [hardening-program.md](../hardening-program.md)**——
那是唯一 roll-up；本表只做导航。

| Batch | 记录 | 主题 |
|---|---|---|
| Hardening Batch 1 | [2026-09-16](2026-09-16-Batch1-correctness-durability-执行记录.md) | correctness / durability：prompt 截断状态污染、中断丢 `TaskResultData` |
| Hardening Batch 1.5 | [2026-09-16](2026-09-16-Batch1.5-config-truthfulness-执行记录.md) | 测试/配置真值性 |
| Hardening Batch 1.6 | [2026-09-17](2026-09-17-Batch1.6-runtime-authority-lifecycle-执行记录.md) | runtime 资源生命周期所有权 / runtime-config 真值 / CLI 覆盖丢失 |
| Hardening Batch 1.7 | [2026-09-17](2026-09-17-Batch1.7-background-task-terminal-state-执行记录.md) | 后台任务必须落到**一个**持久终态 |
| Hardening Batch 1.8 | [2026-09-17](2026-09-17-Batch1.8-user-visible-terminal-semantics-执行记录.md) | 内部终态真值 → 用户可见语义 |
| Hardening Batch 2 | [2026-09-17](2026-09-17-Batch2-guardrails-hardening-执行记录.md) | 架构与 CI 门禁加固（含子批 2.1/2.2/2.3） |
| Hardening Batch 3 | [2026-09-17](2026-09-17-Batch3-deletion-simplification-执行记录.md) | 删除/简化 + false-dead 审计 |
| Hardening Batch 3.5 | [2026-09-17](2026-09-17-Batch3.5-correctness-triage-执行记录.md) | B3-CB 7 条分诊 |
| Hardening Batch 3.6 | [2026-09-18](2026-09-18-Batch3.6-regression-guard-completion-执行记录.md) | 回归守卫补全（N-4 / N-8） |
| Hardening Batch 4（分诊稿） | [2026-09-18](2026-09-18-Batch4-backlog-triage.md) | **不是执行记录**：backlog 分诊，重建 todo.md |
| Hardening Batch 4 | [2026-09-18](2026-09-18-Batch4-runtime-health-truthfulness-执行记录.md) | runtime health 真值（`/ready` 假绿） |
| Hardening Batch 5 | [2026-09-18](2026-09-18-Batch5-provider-capability-truthfulness-执行记录.md) | provider 能力真值（preflight / provider test / 标志语义） |
| Hardening Batch 6 | [2026-09-18](2026-09-18-Batch6-conflict-metadata-truthfulness-执行记录.md) | 409 冲突元数据真值 |
| Hardening Batch 7 | [2026-09-18](2026-09-18-Batch7-provider-preview-window-执行记录.md) | provider 预览窗口（不变量 PW1–PW6） |
| Hardening Batch 8 | [2026-09-18](2026-09-18-Batch8-pagination-completeness-执行记录.md) | 分页完备性（PG1–PG6） |
| Hardening Batch 9 | [2026-09-18](2026-09-18-Batch9-speaking-state-recovery-执行记录.md) | SPEAKING 状态恢复（SP1–SP4） |
| Hardening Batch 10 | [2026-09-18](2026-09-18-Batch10-thinking-state-cancellation-recovery-执行记录.md) | THINKING 取消恢复（TH1–TH5） |
| Hardening Batch 11 | [2026-09-18](2026-09-18-Batch11-cancellation-safe-turn-finalization-执行记录.md) | 取消安全的回合收口（CF1–CF6） |
| Hardening Batch 12 | [2026-09-18](2026-09-18-Batch12-accepted-input-delivery-closure-执行记录.md) | 已 ACK 输入的投递闭环（AD1–AD8） |
| Hardening Batch 13 | [2026-09-19](2026-09-19-Batch13-pending-input-ordering-执行记录.md) | pending 输入定序（PO1–PO6） |
| Hardening Batch 14 | [2026-09-19](2026-09-19-Batch14-residual-input-ownership-triage-执行记录.md) | 残余输入所有权分诊（RO1–RO4） |
| Hardening Batch 15 | [2026-09-19](2026-09-19-Batch15-claimed-input-conversation-truth-执行记录.md) | 已接管输入的会话真值（CT1–CT6） |
| Hardening Batch 16 | [2026-09-19](2026-09-19-Batch16-session-data-integrity-执行记录.md) | 会话数据完整性（SD1–SD6） |
| Hardening Batch 17 | [2026-09-19](2026-09-19-Batch17-ollama-provider-adapter-执行记录.md) | Ollama provider adapter（OL1–OL6；PD-12 裁决） |
| Hardening Batch 18 | [2026-09-19](2026-09-19-Batch18-context-token-estimation-执行记录.md) | 上下文/token 估算合同（TE1–TE8） |
| Hardening Batch 19 | [2026-09-20](2026-09-20-Batch19-long-tail-backlog-closure-执行记录.md) | 长尾 backlog 收口 + 回合内预算（TE9） |
| Hardening Batch 20 / Residual Correctness Closure | [2026-09-20](2026-09-20-Batch20-residual-correctness-closure-执行记录.md) | 残余 correctness 收口（KB-82 / KB-78 / KB-41 / KB-48 / KB-56；SY1–SY3 + PD-13） |

> 文件名排序会骗人：同一天里 `Batch10/11/12` 的字典序在 `Batch4–9` **之前**；
> `Batch 4` 有**两个**文件（分诊稿 + 执行记录）。按上表的 Batch 号读，不要按 `ls` 顺序读。

## 架构审计（Architecture Audits）

不是 hardening batch，但结论仍在被引用：

| 记录 | 内容 |
|---|---|
| [2026-08-16-core模块化健康评估.md](2026-08-16-core模块化健康评估.md) | 第一轮 `core/` 模块化健康评估，9 项问题的关闭过程 |
| [2026-08-30-后端架构分层与模块化增量评估.md](2026-08-30-后端架构分层与模块化增量评估.md) | 增量评估：即时工具调度语义收窄、应用用例边界、私有接口、WS/契约拆分、后台协程治理、DB 迁移机制 ⇒ `ARCH-INC-P1-01..03` / `P2-01..03` 的来源 |
| [2026-09-16-架构职责分离与分层重构审计.md](2026-09-16-架构职责分离与分层重构审计.md) | 七域职责分离审计；**Hardening Batch 1 的上游**（其中"缺陷 C"的断言被 Batch 1 自己的复现否定——保留原样，不改结论） |

## Electron 集成（Electron Integration）

| 记录 | 内容 |
|---|---|
| [electron-v2-backend-change-list-2026-08-11.md](electron-v2-backend-change-list-2026-08-11.md) | 2026-08-11 加固批次的 wire 契约变更清单（D6b/D6c） |
| [electron-v2-backend-post-hardening-review-2026-08-12.md](electron-v2-backend-post-hardening-review-2026-08-12.md) | 加固后复核：BE-P1-06/07 修复 + BE-P2-04 + **BE-RISK-01 保持开放** |
| [2026-08-13-Windows-Electron-首次真实联调后端问题清单.md](2026-08-13-Windows-Electron-首次真实联调后端问题清单.md) | 首次真实 Windows 联调：工具消息空 ID、新写入约束、历史稳定回填 |
| [electron-v2-message-id-fix-report-2026-08-13.md](electron-v2-message-id-fix-report-2026-08-13.md) | 上述问题的修复报告（含真实 Mongo 回填证据） |
| [2026-08-13-Windows-Electron-二次真实联调后端整改清单.md](2026-08-13-Windows-Electron-二次真实联调后端整改清单.md) | 二次联调：WS replay 工具项缺身份 → 前端重复消息 |
| [electron-v2-int2-replay-identity-fix-report-2026-08-13.md](electron-v2-int2-replay-identity-fix-report-2026-08-13.md) | 上述问题的修复报告 |
| [2026-08-19-阶段6-工具双轨统筹决策记录.md](2026-08-19-阶段6-工具双轨统筹决策记录.md) | 阶段 6 工具双轨：方案 B 定案 + `tool.started`/`tool.completed`/`sourceCallId` 契约冻结 |

## Character / Stage H

| 记录 | 内容 |
|---|---|
| [2026-09-09-多角色Stage-H真实联调后端整改与复验清单.md](2026-09-09-多角色Stage-H真实联调后端整改与复验清单.md) | reset 并发、UserIdentity 持久化、preview、校验错误、pinned 夹具 |
| [2026-09-10-多角色Stage-H第二轮联调后续整改清单-后端.md](2026-09-10-多角色Stage-H第二轮联调后续整改清单-后端.md) | 第二轮：tail lease / 身份进入 Prompt / 身份 fail-closed / 归档一致性 |
| [2026-09-10-多角色Stage-H最终真实联调验收.md](2026-09-10-多角色Stage-H最终真实联调验收.md) | 最终验收（PASS，Stage H 闭账事实入口） |
| [2026-09-10-多角色数据完整性缺口导致前端整页拒绝-后端.md](2026-09-10-多角色数据完整性缺口导致前端整页拒绝-后端.md) | 数据完整性缺口 → 前端整页 fail-closed 的根因与现场处置 |

## 其它记录

| 记录 | 内容 |
|---|---|
| [2026-09-12-code-review-ledger.md](2026-09-12-code-review-ledger.md) | [`CODE_REVIEW_REPORT.md`](../../CODE_REVIEW_REPORT.md) 的子代理证据台账（自带 `CR-01..CR-49` 命名空间） |
| [2026-09-18-ollama-runtime-context-authority-记录.md](2026-09-18-ollama-runtime-context-authority-记录.md) | **不是**执行记录：KB-13 / AR-16 / PD-12 的 backlog 登记与 E3 证据 |

## Archived Reviews

[`archive/`](archive/) 按月份存放更早的审查报告：

| 目录 | 覆盖 |
|---|---|
| [`archive/2026-07/`](archive/2026-07/) | 最早的单应用审查：句子切分、code review、文档审查 |
| [`archive/2026-08/`](archive/2026-08/) | Electron v2 时代：契约 9ABC、首次联调、全项目审查与整改、阶段 3/4/5 真实机整改与验收、第三次联调、Live2D |
| [`archive/misc/`](archive/misc/) | 无日期前缀者（frontend 审查收口、开发机迁移 handoff） |

## 相邻目录

- [`../progress/`](../progress/) — 执行记录/进度快照（根目录保留 Stage H 两份事实源，其余在 `archive/2026-08/`）。
- [`../completed/`](../completed/) — 月度完成归档（索引见 [`../completed.md`](../completed.md)）。
- [`../archive/`](../archive/) — Electron 契约时代 / TaskSystem 时代 / 迁移 / 前端交接（见 [`../archive/README.md`](../archive/README.md)）。
- [`../superpowers/`](../superpowers/) — 设计规格与实施计划（见 [`../superpowers/README.md`](../superpowers/README.md)）。

---

## 相关文档

| 文档 | 内容 |
|---|---|
| [../hardening-program.md](../hardening-program.md) | Batch 1→20 的**唯一 roll-up**（当前最终状态以 HEAD + todo + invariants 为准） |
| [../architecture/invariants.md](../architecture/invariants.md) | 不变量权威；每个 family 的"来源"列直接链回本目录的执行记录 |
| [../todo.md](../../todo.md) | 当前 backlog（§6 是 closed archive 的一行索引，证据在本目录） |
| [../decisions.md](../decisions.md) | 决策索引与 ID 命名空间对照 |
| [../README.md](../README.md) | docs 索引入口 |
