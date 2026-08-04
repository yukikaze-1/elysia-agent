# docs/ 文档索引

项目文档目录结构。根级文件为当前有效文档；`review/` 为审查报告；`legacy/` 为已废弃的历史文档；`superpowers/` 为设计规格与实施计划。

## 根级文档

| 文档 | 内容 |
|------|------|
| [architecture.md](architecture.md) | 完整架构详解（数据流、模块、目录结构） |
| [frontend-api.md](frontend-api.md) | 前端 API 文档（WebSocket 消息类型） |
| [testing.md](testing.md) | 测试命令速查 + 分层体系（unit/service/integration/slow） |
| [readme-conventions.md](readme-conventions.md) | README 编写规范（模板 + 面包屑规则 + 检查清单） |
| [database-storage.md](database-storage.md) | 数据库存储设计（MongoDB + Milvus 集合/索引） |
| [data-structure-survey.md](data-structure-survey.md) | 全项目数据结构普查 |
| [completed.md](completed.md) | 已完成工作记录（月度归档见 [completed/](completed/)） |
| [experience.md](experience.md) | 开发经验记录（踩坑笔记） |
| [sync-lessons.md](sync-lessons.md) | 文字/语音同步演进经验 |

### Electron 后端联调
| 文档 | 内容 |
|------|------|
| [electron-backend-baseline.md](electron-backend-baseline.md) | 后端协议基线（v1 现状，批次 0） |
| [electron-backend-protocol-v2.md](electron-backend-protocol-v2.md) | v2 结构化聊天 + 多会话/历史/搜索/资源 REST + 错误码目录（批次 2/3） |
| [electron-backend-security.md](electron-backend-security.md) | 远程认证/TLS/限制/受保护会话数据完整性（批次 1/3） |
| [electron-backend-progress.md](electron-backend-progress.md) | 后端改造进度（批次 0-3） |
| [14-Cyrene-后端配合改造任务清单-v0.1.md](14-Cyrene-后端配合改造任务清单-v0.1.md) | Electron 后端配合改造任务清单（BE-001~081，状态图例） |
| [15-Cyrene-后端改造执行上下文与Prompt-v0.1.md](15-Cyrene-后端改造执行上下文与Prompt-v0.1.md) | 后端改造执行上下文与 Prompt |

## 子目录

| 目录 | 内容 |
|------|------|
| [superpowers/specs/](superpowers/specs/) | 设计规格（`-design.md` 后缀） |
| [superpowers/plans/](superpowers/plans/) | 实施计划（`-plan.md` 后缀） |
| [superpowers/specs/archive/](superpowers/specs/archive/) | 已归档设计规格（按月分目录 `archive/<YYYY-MM>/`） |
| [superpowers/plans/archive/](superpowers/plans/archive/) | 已归档实施计划（按月分目录 `archive/<YYYY-MM>/`） |
| [review/](review/) | 代码/文档审查报告（code-review、doc-review、frontend_completed） |
| [legacy/](legacy/) | 已废弃文档（companion-*，保留作历史参考） |
| [completed/](completed/) | 已完成月度归档（2026-05 ~ 2026-08） |

## 约定

- spec 文件统一 `YYYY-MM-DD-<主题>-design.md` 命名；plan 文件推荐 `YYYY-MM-DD-<主题>-plan.md`（历史文件部分未带该后缀，见 `superpowers/plans/`）。
- 新 spec/plan 一律放在 `superpowers/specs/` 和 `superpowers/plans/`（`docs/specs/`、`docs/plans/` 已废弃并入）。
- 月度归档：已实施完成的 spec/plan 移入对应 `superpowers/<type>/archive/<YYYY-MM>/`，按月份分目录。
- 审查报告放 `review/`；已废弃文档移入 `legacy/`。
