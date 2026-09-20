# architecture/ — 架构类文档

> 文档导航：[项目文档](../../README.md) → [docs 索引](../README.md) → 本文

本目录是**架构侧的细粒度权威**集合；系统级总览在上一级的
[`../architecture.md`](../architecture.md)。

| 文件 | Authority 类型 | 管什么 |
|---|---|---|
| [`../architecture.md`](../architecture.md) | **CANONICAL** | 系统架构总览：数据流、层与包、组装、文件结构、并发全景、已知缺口。**系统级表示以它为准** |
| [`invariants.md`](invariants.md) | **CANONICAL** | 系统不变量：每条 → 测试 → 门禁 → CI 的完整链路；含「有测试但 CI 跑不到」的假保障清单（§7）。**新不变量一律登记在这里** |
| [`dead-code-review-checklist.md`](dead-code-review-checklist.md) | **CANONICAL** | 删代码前的假死审计清单：`zero static references != zero runtime consumers`、12 类高风险假死模式、Consumer-Proof Checklist、Challenger 要求 |

## 三者的分工（避免重叠）

- **架构形状 / 数据流 / 目录结构** → [`../architecture.md`](../architecture.md)。
  它写"系统长什么样"，可以被引用，但**不**承载逐条判据。
- **"某条断言会不会变红"** → [`invariants.md`](invariants.md)。
  它写"哪条不变量由哪个测试钉住、经哪道门禁、在哪个 CI job 跑"。
- **"这段代码能不能删"** → [`dead-code-review-checklist.md`](dead-code-review-checklist.md)。
  它写"零引用为什么不等于零消费者"以及怎么证明。

**冲突时的裁决顺序**（CLAUDE.md 的 Architecture Authority Conflict Rule）：
先验证 **current code**，再以 [`../architecture.md`](../architecture.md) 作为
**系统级表示**。不要用"文件日期更新"来裁决。

**子系统内部的细节**以各包自己的 README 为准（它们可以比系统架构文档更细，
但**不能重新定义全局层级**，见 `docs/readme-conventions.md`）。

---

## 相关文档

| 文档 | 内容 |
|---|---|
| [../architecture.md](../architecture.md) | 系统架构权威（本目录的上级总览） |
| [../README.md](../README.md) | docs 索引与完整 authority map |
| [../code-style-guide.md](../code-style-guide.md) | 类型与风格约束（写实现时配合 `invariants.md` 读） |
| [../../CLAUDE.md](../../CLAUDE.md) | 工程约定：门禁清单 + 私有穿透纪律 + 后台协程所有权 |
