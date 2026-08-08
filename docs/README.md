# docs/ 文档索引

项目文档目录结构。根级文件为当前有效文档；`review/` 为审查报告；`legacy/` 为已废弃的历史文档；`archive/` 为归档的历史记录；`superpowers/` 为设计规格与实施计划。

## 根级文档

| 文档 | 内容 |
|------|------|
| [architecture.md](architecture.md) | 完整架构详解（数据流、模块、目录结构） |
| [testing.md](testing.md) | 测试命令速查 + 分层体系（unit/service/integration/slow） |
| [readme-conventions.md](readme-conventions.md) | README 编写规范（模板 + 面包屑规则 + 检查清单） |
| [database-storage.md](database-storage.md) | 数据库存储设计（MongoDB + Milvus 集合/索引） |
| [code-style-guide.md](code-style-guide.md) | 代码风格与类型约束（ID NewType 体系 + 权威归属 + StrEnum/coerce + getattr 纪律） |
| [completed.md](completed.md) | 已完成工作记录（月度归档见 [completed/](completed/)） |

### Electron 后端联调（需求 / 协议 / 安全）
| 文档 | 内容 |
|------|------|
| [14-Cyrene-后端配合改造任务清单-v0.1.md](14-Cyrene-后端配合改造任务清单-v0.1.md) | 前端联调需求清单（BE-001~BE-062：目的 + 后端现状 + 验收标准）——联调验收依据（2026-08-09 从 git 历史恢复） |
| [15-Cyrene-后端改造执行上下文与Prompt-v0.1.md](15-Cyrene-后端改造执行上下文与Prompt-v0.1.md) | 前端改造执行上下文：必须纠正的错误口径（架构纪律）+ 默认产品决策 + 批次实施顺序（2026-08-09 从 git 历史恢复） |
| [electron-backend-protocol-v2.md](electron-backend-protocol-v2.md) | v2 结构化聊天 + 多会话/历史/搜索/资源 REST + Desktop 工具契约 + 模型 Profile/TTS 声音契约 + 三种语音/TTS 播放契约 + 通用附件与媒体契约（§13）+ 错误码目录 40 码（批次 2-8c） |
| [electron-backend-security.md](electron-backend-security.md) | 远程认证/TLS/限制/受保护会话数据完整性 + 密钥 Secret Store/SSRF + 附件签名下载与签发安全（§3.5）（批次 1-8c） |

## 子目录

| 目录 | 内容 |
|------|------|
| [superpowers/specs/](superpowers/specs/) | 设计规格（`-design.md` 后缀） |
| [superpowers/plans/](superpowers/plans/) | 实施计划（`-plan.md` 后缀） |
| [superpowers/specs/archive/](superpowers/specs/archive/) | 已归档设计规格（按月分目录 `archive/<YYYY-MM>/`） |
| [superpowers/plans/archive/](superpowers/plans/archive/) | 已归档实施计划（按月分目录 `archive/<YYYY-MM>/`） |
| [review/](review/) | 代码/文档审查报告（code-review、doc-review、frontend_completed） |
| [legacy/](legacy/) | 已废弃文档（companion-*，保留作历史参考） |
| [archive/](archive/) | 归档历史记录：electron 基线/迁移/进度/审计、经验与同步笔记、数据结构普查、前端交接包（frontend-handoff-v2）、WorkflowEngine 历史设计（architecture/PROGRESS/migration）、frontend_todo |
| [completed/](completed/) | 已完成月度归档（2026-05 ~ 2026-08） |

## 约定

- spec 文件统一 `YYYY-MM-DD-<主题>-design.md` 命名；plan 文件推荐 `YYYY-MM-DD-<主题>-plan.md`（历史文件部分未带该后缀，见 `superpowers/plans/`）。
- 新 spec/plan 一律放在 `superpowers/specs/` 和 `superpowers/plans/`（`docs/specs/`、`docs/plans/` 已废弃并入）。
- 月度归档：已实施完成的 spec/plan 移入对应 `superpowers/<type>/archive/<YYYY-MM>/`，按月份分目录。
- 审查报告放 `review/`；已废弃文档移入 `legacy/`；历史过程/一次性文档移入 `archive/`。
