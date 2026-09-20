# Documentation

> 文档导航：[项目文档](../README.md) → 本文

本文件是**所有长期文档的导航与 authority map**。它**不**列出全部 500+ 个文档——
只列**入口**与**权威**；历史语料按目录整块导航（见 [Historical Records](#historical-records)）。

## Start Here

按"你是谁、要做什么"选一条读：

| 我想… | 先读 | 然后 |
|---|---|---|
| 把项目跑起来 | [`../README.md`](../README.md)（快速开始） | [`operations.md`](operations.md) |
| 改代码并提交 | [`testing.md`](testing.md) §Required pre-push verification | [`code-style-guide.md`](code-style-guide.md) |
| 理解系统怎么组织 | [`architecture.md`](architecture.md) | [`architecture/invariants.md`](architecture/invariants.md) |
| 接前端 / Electron | [`electron-backend-protocol-v2.md`](electron-backend-protocol-v2.md) | [`../frontend_contract/README.md`](../frontend_contract/README.md) |
| 知道"还剩什么没做" | [`../todo.md`](../todo.md) | [`decisions.md`](decisions.md) |
| 知道"以前发生过什么" | [`hardening-program.md`](hardening-program.md) | [`review/README.md`](review/README.md) |
| 让 Coding Agent 干活 | [`../CLAUDE.md`](../CLAUDE.md) | — |

## Canonical References

**一个事实只有一个主要权威。** 下表是每个 topic 的权威文档；其它文档遇到同一事实
**应当链接而不是复制**（[`readme-conventions.md`](readme-conventions.md) §易腐事实）。

| Topic | Authority | 说明 |
|---|---|---|
| 项目是什么 / 快速开始 | [`../README.md`](../README.md) | 安装、启动、最小开发入口 |
| 文档导航 | 本文 | 入口 + authority map |
| 架构 | [`architecture.md`](architecture.md) | 单文件系统架构权威 |
| 不变量 | [`architecture/invariants.md`](architecture/invariants.md) | 每条不变量 → 测试 → 门禁 → CI |
| 删代码前的假死审计 | [`architecture/dead-code-review-checklist.md`](architecture/dead-code-review-checklist.md) | `zero static references != zero runtime consumers` |
| 测试体系 | [`testing.md`](testing.md) | 分层 / marker / **提交前必跑清单** |
| CI | [`ci.md`](ci.md) | job 布局 / 信任边界 / PR 恢复清单 |
| 运维 | [`operations.md`](operations.md) | 启动 / 配置入口 / DB / provider / 健康 / 日志 / 恢复边界 |
| 存储 | [`database-storage.md`](database-storage.md) | 集合 / 索引 / TTL / GC / 漂移自检 |
| 人类 wire 契约 | [`electron-backend-protocol-v2.md`](electron-backend-protocol-v2.md) | 事件序列 / 字段语义 / 错误码目录 |
| 机器契约（可执行） | [`../frontend_contract/`](../frontend_contract/README.md) | Pydantic 模型 + 生成器 + 漂移门禁 |
| 安全（后端面） | [`electron-backend-security.md`](electron-backend-security.md) | 认证 / TLS / 限流 / 幂等 / 附件签名 |
| 凭据处理 | [`credential-handling.md`](credential-handling.md) | 放哪 / 怎么引用 / 泄露响应顺序 |
| 决策（已裁定） | [`decisions.md`](decisions.md) | 决策索引 + ID 命名空间 |
| Backlog（待办） | [`../todo.md`](../todo.md) | 唯一 backlog authority |
| 加固历史 | [`hardening-program.md`](hardening-program.md) | Batch 1→20 的唯一 roll-up（**历史**） |
| 已完成工作 | [`completed.md`](completed.md) | 月度归档索引 |
| 代码风格 | [`code-style-guide.md`](code-style-guide.md) | ID NewType / 权威归属 / StrEnum / getattr 纪律 |
| README 规范 | [`readme-conventions.md`](readme-conventions.md) | 模板 + 面包屑 + **易腐事实纪律** |
| 工程约定 / Coding Agent | [`../CLAUDE.md`](../CLAUDE.md) | 指令集；事实一律指向本表 |

## Developer Guides

- [`testing.md`](testing.md) — 测试分层体系、marker 纪律、并行执行、**提交前必跑清单**。
- [`ci.md`](ci.md) — CI 布局与信任边界；改 workflow 前必读。
- [`code-style-guide.md`](code-style-guide.md) — 写代码前必读。
- [`readme-conventions.md`](readme-conventions.md) — 写文档前必读。
- [`operations.md`](operations.md) — 本地把系统跑起来、排障。
- 子系统 README（唯一权威在包内）：
  [`core/README.md`](../core/README.md) ·
  [`Module/README.md`](../Module/README.md) ·
  [`frontend_contract/README.md`](../frontend_contract/README.md) ·
  [`scripts/README.md`](../scripts/README.md) ·
  [`character/README.md`](../character/README.md) ·
  [`frontend/README.md`](../frontend/README.md)

## Operations

- [`operations.md`](operations.md) — 启动、配置入口、环境变量索引、数据库、provider、
  Ollama、健康/就绪、日志、备份与恢复边界、故障排查、版本权威。
- [`database-storage.md`](database-storage.md) — 存储设计（集合/索引/TTL/GC）。
- [`credential-handling.md`](credential-handling.md) — 凭据与泄露响应。
- [`ci.md`](ci.md) — 流水线行为（含"声明 ≠ 执行"的门禁接线纪律）。

## Contracts

- [`electron-backend-protocol-v2.md`](electron-backend-protocol-v2.md) — **人类可读** wire 契约。
- [`electron-backend-security.md`](electron-backend-security.md) — 契约相关的安全边界。
- [`../frontend_contract/README.md`](../frontend_contract/README.md) — **机器契约**（可执行模型 + 生成器 + 门禁）。
- [`architecture/invariants.md`](architecture/invariants.md) §5/§6 — 静态面与 CI 信任边界的不变量。

## Decisions / Backlog

严格分工（不要在两处都维护）：

| 文件 | 承载 |
|---|---|
| [`../todo.md`](../todo.md) | **还没做 / 还没定**的事（唯一 backlog authority，含 `PD-*` 待裁决） |
| [`decisions.md`](decisions.md) | **已经定下**的决定 + 决策索引（含历史 ID 命名空间对照） |
| [`completed.md`](completed.md) | 月度完成记录（不承载 backlog 状态） |

## Historical Records

> ⚠️ **这一节里的文档是历史证据，不是当前 HEAD 的描述。**
> 它们记录**当时**的系统、**当时**的结论与**当时**的证据；其中一些结论后来被推翻
> （阅读时不要把它们当权威）。当前事实一律看上面的 Canonical References。

统一状态词汇（[`readme-conventions.md`](readme-conventions.md)）：`CANONICAL` /
`SUPPORTING` / `HISTORICAL` / `UNIMPLEMENTED` / `GENERATED` / `LEGACY`。
**不要**用"当前/旧/临时/差不多过时"这类自造词。

| 目录 | 内容 | 入口 |
|---|---|---|
| [`review/`](review/) | 审查报告 + **correctness hardening Batch 1→20 执行记录** + 联调清单 | [`review/README.md`](review/README.md) |
| [`review/archive/`](review/archive/) | 更早的审查报告（按月） | 同上 |
| [`archive/`](archive/) | Electron 契约时代 / TaskSystem 时代 / 迁移 / 前端交接 | [`archive/README.md`](archive/README.md) |
| [`superpowers/`](superpowers/) | 设计规格（spec）与实施计划（plan），**全部已归档** | [`superpowers/README.md`](superpowers/README.md) |
| [`progress/`](progress/) | 执行记录与进度快照 | [`review/README.md`](review/README.md) 的相邻说明 |
| [`completed/`](completed/) | 月度完成归档 | [`completed.md`](completed.md) |
| [`legacy/`](legacy/) | 已废弃文档（`companion-*`，旧人格设计） | [`legacy/companion-roadmap.md`](legacy/companion-roadmap.md) |

**历史文档允许保留当时的路径**（例如 `core/cognitive_loop.py` 这种现在不存在的路径）。
**不要**为了对齐 HEAD 去改历史正文——那会篡改历史上下文（见
[`architecture/invariants.md`](architecture/invariants.md) 的维护纪律）。

### 未实施的设计稿（历史，但用途特殊）

| 文档 | 状态 |
|---|---|
| [`2026-08-29-task-system-overall-architecture-design.md`](2026-08-29-task-system-overall-architecture-design.md) | ⚠️ **UNIMPLEMENTED DESIGN DRAFT**——`TaskService` / `ExecutionGateway` / `TaskCenter` / `/api/tasks` 全仓命中 0。当前对应能力在 [`Module/workflow_engine/`](../Module/workflow_engine/README.md) + [`Module/workflow_contract/`](../Module/workflow_contract/README.md) + [`core/tool_runtime/`](../core/tool_runtime/README.md)。**与 `elysia-frontend` 仓同名文档保持镜像，单仓不得移动/改名** |
| [`2026-08-29-task-system-backend-architecture-design.md`](2026-08-29-task-system-backend-architecture-design.md) | ⚠️ **UNIMPLEMENTED DESIGN DRAFT**（同上） |
| [`2026-08-30-provider-model-runtime-backend-architecture-design.md`](2026-08-30-provider-model-runtime-backend-architecture-design.md) | ⚠️ **UNIMPLEMENTED DESIGN DRAFT**——`ProviderConnection` / `ModelProfile` 拆分与 `/api/runtime/status` 不存在。当前 provider 模型权威是 [`Module/config/providers.yaml`](../Module/config/providers.yaml) + 各 `registry.py`，见 [`operations.md`](operations.md) §5 |
| [`2026-08-30-character-prompt-customization-backend-architecture-design.md`](2026-08-30-character-prompt-customization-backend-architecture-design.md) | 多角色契约族**已实施**（Stage A–H）；当前权威是 [`electron-backend-protocol-v2.md`](electron-backend-protocol-v2.md) §14 + [`core/character/README.md`](../core/character/README.md) |

### 审计台账（跨主题、仍被引用）

| 文档 | 状态 |
|---|---|
| [`../CODE_REVIEW_REPORT.md`](../CODE_REVIEW_REPORT.md) | **SUPPORTING / HISTORICAL AUDIT LEDGER**——全项目代码审查台账 + **owner 决策记录**，仍被 security / credential / CI 文档引用（例如 bandit Low 基线、`_GATES_NOT_IN_CI` 的解除条件）。**不是**当前架构权威；其 road-map 的 "Batch 1–9" 与 [`review/`](review/) 的 correctness hardening "Batch 1–20" 是**两套不同编号**（见 [`review/README.md`](review/README.md) §Batch 编号碰撞） |
| [`review/2026-09-12-code-review-ledger.md`](review/2026-09-12-code-review-ledger.md) | 上述报告的**子代理证据台账**（自带 `CR-01..CR-49` 命名空间，与报告正文的 `CR-01..CR-13` 不同） |

### 活产物写在历史路径下

> ⚠️ **LIVE CONTRACT ARTIFACTS IN LEGACY PATH**：
> [`archive/frontend-handoff-v2/traces/`](archive/frontend-handoff-v2/traces/) 里的
> golden frames 是**当前生效的契约产物**——由 `scripts/gen_contract_frames.py` 生成、
> 被 `make check-types` 门禁、被**跨仓**消费者逐行读取。路径叫 `archive` 只是历史原因。
> **Relocation requires a coordinated change with the external frontend consumer**
> （`elysia-frontend/apps/desktop/scripts/check-backend-contract.mjs` 硬编码该绝对路径）。
> 清单与说明见 [traces 的说明](archive/frontend-handoff-v2/traces/README.md)。

## Generated Artifacts

由代码生成的文档类产物——**不要手改**，改生成器：

| 产物 | 生成器 | 校验 |
|---|---|---|
| [`../frontend/src/types/contract.generated.ts`](../frontend/src/types/contract.generated.ts) | `python -m frontend_contract` | `make check-types` |
| [`contract.schema.json`](../frontend_contract/contract.schema.json) | `gen_schema.py` | 同上 |
| [`contract.raw.ts`](../frontend_contract/contract.raw.ts) | `gen_ts.generate_raw_ts` | 同上 |
| [`archive/frontend-handoff-v2/traces/*.jsonl`](archive/frontend-handoff-v2/traces/) | `scripts/gen_contract_frames.py` | 同上（golden frames） |
| 测试统计数字 | `scripts/print_test_stats.py` | 无门禁 ⇒ 文档**不抄写**该数字 |

## 约定

- **命名**：spec 统一 `YYYY-MM-DD-<主题>-design.md`；plan 推荐 `YYYY-MM-DD-<主题>-plan.md`（历史文件有例外）。
- **新 spec/plan** 放 `superpowers/specs/` 与 `superpowers/plans/`（`docs/specs/`、`docs/plans/` 已废弃并入）。
- **月度归档**：完成的 spec/plan 移入 `superpowers/<type>/archive/<YYYY-MM>/`；月份取**文件名日期前缀**，跨月特性整串归入收口月。
- **审查报告**放 `review/`，完成后移入 `review/archive/<YYYY-MM>/`。
- **已废弃文档**移入 `legacy/`。
- **归档时必须同批修正全仓引用**——`ci.yml` 的 `paths-ignore` 含 `docs/**`，**没有任何门禁检查文档引用**
  （`scripts/check_docs.py` 只覆盖 CANONICAL/SUPPORTING 一层，见 [`readme-conventions.md`](readme-conventions.md)）。
- **禁止移动** `archive/frontend-handoff-v2/traces/`（跨仓硬编码路径，见上）。
- **跨仓镜像文档**（task-system 系列）单仓不得移动——须两侧同批处理。

---

## 相关文档

| 文档 | 内容 |
|---|---|
| [`../README.md`](../README.md) | 项目入口：安装 / 启动 / 最小开发入口 |
| [`../CLAUDE.md`](../CLAUDE.md) | 工程约定与 Coding Agent 指令（事实指向本表） |
| [`architecture/invariants.md`](architecture/invariants.md) | 不变量权威；含"有测试但 CI 跑不到"的假保障清单 |
| [`review/README.md`](review/README.md) | 历史记录索引 + 两套 Batch 编号的消歧 |
