# 开发计划

## 已完成

完成条目按月归档至 `completed/YYYY-MM.md`（见下表）。

## 历史归档

| 月份 | 文件 | 覆盖 |
|------|------|------|
| 2026-05 | [2026-05.md](completed/2026-05.md) | 早期搭建 |
| 2026-06 | [2026-06.md](completed/2026-06.md) | TaskSystem / 存储层 |
| 2026-07 | [2026-07.md](completed/2026-07.md) | prompt 引擎 / 性格引擎 / 文本管线 |
| 2026-08 | [2026-08.md](completed/2026-08.md) | Electron v2 契约 / 附件 / 多会话 |
| 2026-09 | [2026-09.md](completed/2026-09.md) | 多角色 Stage A–H / code review 修复轮 / correctness hardening |

> **为什么不写条目数与提交数**：那类数字没有稳定 authority（"条目"按什么粒度归并、
> 提交数截至哪一刻，都会随口径变化），历史上已出现过 252 / 412 两个互相矛盾的版本。
> 需要计数请用 `git log --since=<月>-01 --until=<下月>-01 --oneline | wc -l`，
> 并明确截断时点。

---
