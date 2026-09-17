# docs/ 文档索引

项目文档目录结构。根级文件为当前有效文档；`review/` 为审查报告；`legacy/` 为已废弃的历史文档；`archive/` 为归档的历史记录；`superpowers/` 为设计规格与实施计划。

## 根级文档

| 文档 | 内容 |
|------|------|
| [architecture.md](architecture.md) | 完整架构详解（数据流、模块、目录结构） |
| [architecture/invariants.md](architecture/invariants.md) | **系统不变量清单**：每条不变量 → 测试 → 门禁 → CI 的完整链路；含「有测试但 CI 跑不到」的假保障清单 |
| [architecture/dead-code-review-checklist.md](architecture/dead-code-review-checklist.md) | **False-Dead 审计清单**（删代码前先读）：`zero static references != zero runtime consumers`；12 类高风险假死模式 + 可复制的 Consumer-Proof Checklist + 分类词汇 + 风险等级 + Challenger 要求 |
| [testing.md](testing.md) | 测试命令速查 + 分层体系（unit/service/integration/slow）+ 真实库防线 + 残留清理脚本 |
| [ci.md](ci.md) | **CI 架构与信任边界**（唯一权威）：job 布局 / 为什么全部 self-hosted（账单教训）/ `check_ci_isolation` 四条规则 / **开放 PR 的恢复清单（含分支保护陷阱）** / job 内几条非洁癖约定 |
| [readme-conventions.md](readme-conventions.md) | README 编写规范（模板 + 面包屑规则 + 检查清单） |
| [database-storage.md](database-storage.md) | 数据库存储设计（MongoDB + Milvus 集合/索引） |
| [code-style-guide.md](code-style-guide.md) | 代码风格与类型约束（ID NewType 体系 + 权威归属 + StrEnum/coerce + getattr 纪律） |
| [completed.md](completed.md) | 已完成工作记录（月度归档见 [completed/](completed/)） |

> **待办与已知问题总表**见仓库根 [`todo.md`](../todo.md)（Bug 表 / 决策记录 / 功能 backlog）；
> **项目约定总入口**见根 [`CLAUDE.md`](../CLAUDE.md)。
> （原 `handoff.md` 交接文档已于 2026-09-11 删除——其有效内容分别归入 `CLAUDE.md`、本索引、
> `todo.md` 与各问题单，避免多份「当前事实」互相漂移。）

### Electron 后端联调（协议 / 安全）
| 文档 | 内容 |
|------|------|
| [electron-backend-protocol-v2.md](electron-backend-protocol-v2.md) | v2 结构化聊天 + 多会话/历史/搜索/资源 REST + Desktop 工具契约 + 模型 Profile/TTS 声音契约 + 三种语音/TTS 播放契约 + 通用附件与媒体契约（§13）+ 多角色/身份契约（§14，Stage A–H）+ 错误码目录 54 码 |
| [credential-handling.md](credential-handling.md) | **凭据处理规范与泄露响应**（CR-01）：凭据放哪 / 文档引用只写变量名+掩码 / 泄露响应顺序「先轮换→再清历史→最后补门禁」/ 清历史 runbook / `make check-secrets` 门禁能力与**已知边界** |
| [electron-backend-security.md](electron-backend-security.md) | 远程认证/TLS/限制/受保护会话数据完整性 + 密钥 Secret Store/SSRF + 附件签名下载与签发安全（§3.5）+ 公开入口限流/幂等持久化 + 探针分层 /health /ready（§3.7，批次 1-9D + 2026-08-11 加固 + 2026-08-12 补偿门禁） |
| [archive/electron-v2-first-integration-runbook.md](archive/electron-v2-first-integration-runbook.md) | **Electron v2 首次联调最小 runbook**（runtime SHA / schemaHash 离线门禁 / Token / 探针口径 / WS 三头 / clean-start / ACK 成功标准与负面用例）⚠️ 已归档 |
| [archive/contract-release-2026-08-11.md](archive/contract-release-2026-08-11.md) | Contract 发版记录：transportVersion/protocolVersion/schemaHash/backend commit + 产物清单 ⚠️ 已归档 |
| [review/electron-v2-backend-change-list-2026-08-11.md](review/electron-v2-backend-change-list-2026-08-11.md) | Electron 后端契约变更清单（D6b/D6c：idempotency_durable / 幂等 fail-closed / RATE_LIMITED） |
| [review/electron-v2-backend-post-hardening-review-2026-08-12.md](review/electron-v2-backend-post-hardening-review-2026-08-12.md) | 加固后复核：BE-P1-06/07（恢复正确性 + 触发条件，2026-08-12 修复）+ BE-P2-04（runbook schemaHash）+ BE-RISK-01（接受风险） |
| [review/2026-08-13-Windows-Electron-首次真实联调后端问题清单.md](review/2026-08-13-Windows-Electron-首次真实联调后端问题清单.md) | Windows Electron 首次真实联调后端问题：工具消息空 ID、新写入约束、历史稳定回填与回归门禁 |
| [review/electron-v2-message-id-fix-report-2026-08-13.md](review/electron-v2-message-id-fix-report-2026-08-13.md) | 消息 ID 修复报告：BE-INT-P0-01/02 + P1-01 关闭（D1-D6 + 真实 Mongo 回填 69→0 + 门禁证据） |
| [review/2026-08-13-Windows-Electron-二次真实联调后端整改清单.md](review/2026-08-13-Windows-Electron-二次真实联调后端整改清单.md) | 二次联调后端整改：WS replay tool 项缺身份 → 前端重复消息（BE-INT2-P0-01/P1-01） |
| [review/electron-v2-int2-replay-identity-fix-report-2026-08-13.md](review/electron-v2-int2-replay-identity-fix-report-2026-08-13.md) | 二次联调 WS replay 身份修复报告：BE-INT2-P0-01/P1-01 关闭（D1-D4 + 真实 sanity + 门禁） |
| [review/2026-08-19-阶段6-工具双轨统筹决策记录.md](review/2026-08-19-阶段6-工具双轨统筹决策记录.md) | 阶段 6 工具双轨统筹：方案 B 定案 + `tool.started/tool.completed`/`sourceCallId` 契约冻结 + B-6/F-6 更新点清单 |

### Provider、模型协议与运行状态设计（2026-08-30）

| 文档 | 内容 |
|------|------|
| [Provider、模型发现、协议适配与运行状态后端架构设计](2026-08-30-provider-model-runtime-backend-architecture-design.md) | ⚠️ **未实施规划稿**（`ProviderConnection`/`ModelProfile` 拆分、协议 Adapter Registry、`/api/runtime/status`、在线模型发现——**代码中均不存在**）：ProviderConnection/ModelProfile 拆分、在线模型发现、Chat Completions/Responses Adapter、Secret 与 SSRF 边界、v1 迁移、运行状态接口及完整实施清单 |

### 多角色、角色 Prompt 与自定义角色设计（2026-08-30）

| 文档 | 内容 |
|------|------|
| [多角色、角色 Prompt 与自定义角色后端架构设计](2026-08-30-character-prompt-customization-backend-architecture-design.md) | 现有十二段 Prompt/四层人格事实基线，Definition/Revision/Instance/UserIdentity 分层，角色状态与记忆隔离，`character-contract-v1` 前后端冻结契约，REST/WS、存储、迁移、测试及分阶段实施门禁 |
| [Stage H 最终真实联调验收](review/2026-09-10-多角色Stage-H最终真实联调验收.md) | 后端 `7d2e2b31` + 前端 `d02ce15` 的最终 Windows Electron 验收：preview、pinned/follow_latest、tail 强串行、A/B Prompt 隔离、完整 cleanup 与冷启动无残留；Stage H 闭账事实入口 |
| [Stage H 真实联调后端整改与复验清单](review/2026-09-09-多角色Stage-H真实联调后端整改与复验清单.md) | Windows Electron 真实联调后剩余的 reset 并发、UserIdentity 持久化、preview、校验错误、pinned 夹具及单用户范围决策；第二轮 P0-1 tail lease / P0-2 身份进入 Prompt / P0-3 身份 fail-closed / P1 归档一致性 |
| [Stage H 第二轮联调后续整改清单（后端）](review/2026-09-10-多角色Stage-H第二轮联调后续整改清单-后端.md) | 第二轮真实联调结论；修复 pinned fixture 非法 createdBy、cleanup 遗留 revisions，交付真实库 smoke（`scripts/dev/stage_h_pinned_smoke.py`，BE-R1~R5），冻结 A/B tail 强串行验收口径；§9 记录实施/验证与两个验收顺序问题 |
| [多角色数据完整性缺口导致前端整页拒绝（后端）](review/2026-09-10-多角色数据完整性缺口导致前端整页拒绝-后端.md) | 真实联调发现：`character_profiles=true` 下前端对会话列表/消息历史整页 fail-closed，而 50 条会话 `character_binding={}` + 14 条 assistant 消息 `character=null` 的历史/测试残留各自触发了该拒绝，致功能整体不可用；含根因（缺多角色数据完整性自检）、测试写生产库证据、现场处置与回滚路径 |

### 架构与模块化评估

| 文档 | 内容 |
|------|------|
| [review/2026-08-16-core模块化健康评估.md](review/2026-08-16-core模块化健康评估.md) | 第一轮 `core/` 模块化健康评估及 9 项已关闭问题：运行时依赖环、输入层职责、复杂度枢纽、测试与文档布局 |
| [review/2026-08-30-后端架构分层与模块化增量评估.md](review/2026-08-30-后端架构分层与模块化增量评估.md) | 阶段 6 后的增量静态评估：即时工具调度语义收窄、应用用例边界、私有接口、WS/契约拆分、后台协程治理与数据库迁移机制 |

### 任务系统与任务中心设计（2026-08-29）

| 文档 | 内容 |
|------|------|
| [任务系统与任务中心总体架构设计](2026-08-29-task-system-overall-architecture-design.md) | ⚠️ **未实施规划稿**（`TaskService`/`ToolExecutionCoordinator`/`ExecutionGateway`/`TaskCenter` 全仓命中 0，无 `/api/tasks`）：前后端共同领域模型、Runtime 边界、公共契约、状态机、持久化、分批实施与待决策项；与 `elysia-frontend` 中同名文档保持镜像同步 |
| [任务系统后端架构设计](2026-08-29-task-system-backend-architecture-design.md) | ⚠️ **未实施规划稿**（同上，目标组件均不存在）：TaskService、WorkflowEngine、ToolExecutionCoordinator、正式 REST/WS API、Mongo 持久化和 Desktop Runtime 后端接入设计 |

## 子目录

| 目录 | 内容 |
|------|------|
| [superpowers/specs/](superpowers/specs/) | 设计规格（`-design.md` 后缀）——当前为空，全部已归档 |
| [superpowers/plans/](superpowers/plans/) | 实施计划（`-plan.md` 后缀）——当前为空，全部已归档 |
| [superpowers/specs/archive/](superpowers/specs/archive/) | 已归档设计规格（按月分目录 `archive/<YYYY-MM>/`，2026-06 ~ 2026-09） |
| [superpowers/plans/archive/](superpowers/plans/archive/) | 已归档实施计划（按月分目录 `archive/<YYYY-MM>/`，2026-06 ~ 2026-09） |
| [review/](review/) | 代码/文档审查报告；当前有效集为 Stage H 事实源 + 两轮架构评估 + 未闭合问题单 |
| [review/archive/<YYYY-MM>/](review/archive/) | 已归档审查报告（按月分目录，2026-07 ~ 2026-08；`misc/` 为无日期前缀者） |
| [progress/](progress/) | 执行记录/进度快照；当前保留 Stage H 两份事实源 |
| [progress/archive/<YYYY-MM>/](progress/archive/) | 已归档执行记录（按月分目录，2026-08） |
| [legacy/](legacy/) | 已废弃文档（companion-*，保留作历史参考） |
| [archive/](archive/) | 归档历史记录：electron 基线/迁移/进度/审计、经验与同步笔记、数据结构普查、前端交接包（frontend-handoff-v2）、WorkflowEngine 历史设计（architecture/PROGRESS/migration）、frontend_todo、Cyrene 后端改造任务清单/执行上下文（v0.1 历史）、2026-08-10 后端+前端适配进度快照 |
| [completed/](completed/) | 已完成月度归档（2026-05 ~ 2026-08） |

## 约定

- spec 文件统一 `YYYY-MM-DD-<主题>-design.md` 命名；plan 文件推荐 `YYYY-MM-DD-<主题>-plan.md`（历史文件部分未带该后缀，见 `superpowers/plans/`）。
- 新 spec/plan 一律放在 `superpowers/specs/` 和 `superpowers/plans/`（`docs/specs/`、`docs/plans/` 已废弃并入）。
- 月度归档：已实施完成的 spec/plan 移入对应 `superpowers/<type>/archive/<YYYY-MM>/`，按月份分目录。
  - 月份取**文件名日期前缀**；同一特性串跨月的（如 character stage a–g）整串归入收口月，不拆散。
- 审查报告放 `review/`，完成后移入 `review/archive/<YYYY-MM>/`；执行记录放 `progress/`，完成后移入 `progress/archive/<YYYY-MM>/`。
- 已废弃文档移入 `legacy/`；无月份归类的历史过程/一次性文档移入 `archive/`。
- **归档时必须同批修正全仓引用**（源码 README/docstring、`todo.md`、根 `README.md`、其他文档）——`ci.yml` 的 `paths-ignore` 含 `docs/**` 与 `**.md`，**没有任何门禁会检查文档引用**。
- **禁止移动 `archive/frontend-handoff-v2/traces/`**：`elysia-frontend/apps/desktop/scripts/check-backend-contract.mjs` 硬编码了该绝对路径，移动即令前端契约校验静默失效。
- **跨仓镜像文档**（如 task-system 系列，与 `elysia-frontend` 双份同步）单仓不得移动——须两侧同批处理，或留原地加状态标注。
