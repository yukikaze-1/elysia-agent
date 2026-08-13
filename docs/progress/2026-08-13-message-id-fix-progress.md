# 2026-08-13 消息 ID 修复批次状态

**批次：** Electron v2 首次联调消息 ID 修复（BE-INT-P0-01/02 + P1-01）
**实现基线：** `main@ac3299f` → `740a998`
**状态：** ✅ 已闭环

## 完成内容

- **P0-01 新写入**：`ToolResultMessage.message_id` 必填 + 拒空（模型）；两条生产写入路径（loop.py submit_task/普通工具）补 `new_message_id()`；`MessageRepository.append` 空 ID 守卫。
- **P0-02 历史兼容**：读路径适配器（archive/快照/JSONL）对存量空 ID 工具消息派生稳定 ID；幂等回填脚本 `scripts/backfill_message_ids.py --dry-run` 实跑 archive 69 条、剩余 0、重跑零变化。
- **P1-01 门禁**：契约 `Message.message_id` `min_length=1` + 三产物重生成；构造拒绝/归档守卫/REST/搜索/JSONL/回填幂等全链测试。

## 门禁证据

| 门禁 | 结果 |
|---|---|
| `make test-quick` | 3542 passed, 2 skipped |
| Ruff / Pyright | All checks passed / 0 errors |
| `frontend_contract --check` | 通过 |
| `check_error_codes.py` | 41 码一致 |
| 真实 Mongo 回填 | archive 69→0 空 ID，幂等零变化 |

## 决策记录

- D1 派生键偏离问题清单「基于 `_id`」：`_id` 仅 archive 有，快照无；跨库一致性用 `(tool_call_id, timestamp)`。
- 搜索/pending 契约 `min_length` 残差（非阻断）：runtime 经读侧派生恒非空。

详细见 [修复报告](../review/electron-v2-message-id-fix-report-2026-08-13.md) 与 [设计 spec](../superpowers/specs/2026-08-13-electron-v2-first-integration-message-id-fix-design.md)。
