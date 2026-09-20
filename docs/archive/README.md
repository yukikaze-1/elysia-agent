# 历史归档（archive/）

> 文档导航：[项目文档](../../README.md) → [docs 索引](../README.md) → 本文

> ⚠️ **HISTORICAL / LEGACY.** 本目录记录**过去**的设计、进度与交接材料，
> 不代表当前 HEAD。当前事实看 [architecture.md](../architecture.md) /
> [operations.md](../operations.md) / [database-storage.md](../database-storage.md) /
> [electron-backend-protocol-v2.md](../electron-backend-protocol-v2.md)。
>
> **例外见最后一节**：`frontend-handoff-v2/traces/` 下有**当前生效**的契约产物。

## Electron 契约时代（2026-08）

| 文件 | 内容 |
|---|---|
| [electron-backend-baseline.md](electron-backend-baseline.md) | 后端协议基线（v1 现状，批次 0） |
| [electron-backend-migration.md](electron-backend-migration.md) | v1 → v2 迁移/升级说明 |
| [electron-backend-progress.md](electron-backend-progress.md) | 改造进度（批次 0–8c，全部完成） |
| [electron-backend-audit-batches0-7.md](electron-backend-audit-batches0-7.md) | 批次 0–7 全量审查报告（56 条发现） |
| [contract-release-2026-08-11.md](contract-release-2026-08-11.md) | 契约发版记录（transportVersion / protocolVersion / schemaHash / commit） |
| [electron-v2-first-integration-runbook.md](electron-v2-first-integration-runbook.md) | 首次联调 runbook（当时口径；**当前口径见 protocol-v2 + security**） |
| [2026-08-10-backend-frontend-progress.md](2026-08-10-backend-frontend-progress.md) | 前后端适配进度快照 |
| [frontend_todo.md](frontend_todo.md) | 遗留的前端 code-review TODO |

## TaskSystem 时代（2026-06/07）

| 文件 | 内容 |
|---|---|
| [architecture.md](architecture.md) | 当时的工作流引擎/任务系统重构设计（**注意：与本目录外的 `docs/architecture.md` 同名但不同物**） |
| [PROGRESS.md](PROGRESS.md) | 当时的进度记录 |
| [migration.md](migration.md) | 类型迁移清单 |

> 当前"后台任务"能力的权威是 [`Module/workflow_engine/`](../../Module/workflow_engine/README.md)
> + [`Module/workflow_contract/`](../../Module/workflow_contract/README.md)；
> **未实施的 TaskSystem/TaskService 设计稿**不在本目录，见
> [`../README.md`](../README.md) 的「未实施的设计稿」。

## 经验与普查

| 文件 | 内容 |
|---|---|
| [data-structure-survey.md](data-structure-survey.md) | 数据结构普查 |
| [experience.md](experience.md) | 开发经验记录（含代理/环境的坑） |
| [sync-lessons.md](sync-lessons.md) | 文字/语音同步的踩坑记录 |
| [elysia-install-issues.md](elysia-install-issues.md) | 安装问题实录（N5095 / 代理 / 模型下载）——**当前仍被 [operations.md](../operations.md) §10 引用** |

## 前端交接（Cyrene / handoff）

| 文件 | 内容 |
|---|---|
| [14-Cyrene-后端配合改造任务清单-v0.1.md](14-Cyrene-后端配合改造任务清单-v0.1.md) | Cyrene 前端所需的后端改造清单（v0.1，历史） |
| [15-Cyrene-后端改造执行上下文与Prompt-v0.1.md](15-Cyrene-后端改造执行上下文与Prompt-v0.1.md) | 上述改造的执行上下文与 prompt（v0.1，历史） |
| [frontend-handoff-v2/README.md](frontend-handoff-v2/README.md) | 批次 7 的前端交接包说明 |

## ⚠️ 活产物写在历史路径下

> **LIVE CONTRACT ARTIFACTS IN LEGACY PATH**

[`frontend-handoff-v2/traces/`](frontend-handoff-v2/traces/) **不是**纯历史目录：
其中的 golden frames 是**当前生效的契约产物**——由 `scripts/gen_contract_frames.py`
确定性生成、被 `make check-types` 门禁、被**跨仓**消费者逐行读取
（`elysia-frontend/apps/desktop/scripts/check-backend-contract.mjs` 硬编码了该绝对路径）。

> **Relocation requires a coordinated change with the external frontend consumer.**
> 单仓移动即令对端契约校验静默失效。

清单（含帧数）与再生成命令见 [traces 清单与再生成命令](frontend-handoff-v2/traces/README.md)。

---

## 相关文档

| 文档 | 内容 |
|---|---|
| [../README.md](../README.md) | docs 索引与 authority map |
| [../review/README.md](../review/README.md) | 审查报告与 correctness hardening 执行记录 |
| [../superpowers/README.md](../superpowers/README.md) | 设计规格与实施计划（全部已归档） |
| [../architecture.md](../architecture.md) | **当前**系统架构权威（注意与本目录同名文件区分） |
