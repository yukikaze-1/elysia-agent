# 2026-08-13 二次联调 WS replay 身份修复批次状态

**批次：** Electron v2 二次联调 WS replay 身份修复（BE-INT2-P0-01 + P1-01）
**实现基线：** `main@b1f372d` → `fb9f8f1`
**状态：** ✅ 已闭环

## 完成内容

- **P0-01 运行时输出**：`messages_for_client()` tool 分支输出 `message_id`/`timestamp`（复用已持久/派生的 ID）；`ClientToolMessage` 加必填身份字段。
- **P1-01 契约门禁**：`SessionMessageItem.message_id`/`timestamp` 必填非空；缺身份项 fail-closed 拒绝；三产物重生成；测试 fixture 与构造点修补。

## 门禁证据

| 门禁 | 结果 |
|---|---|
| `make test-quick` | 3544 passed, 2 skipped |
| Ruff / Pyright | All checks passed / 0 errors |
| `frontend_contract --check` | 通过（schemaHash 变化预期） |
| 真实数据 sanity | `conv_036c872c7c55` tool 消息 REST/WS 同一身份（`msg_7c0c72b0…`，inmem_same=True） |

## 决策记录

- D1 复用持久 ID 不在 replay 随机生成；D3 user/assistant 构造校验器复核后记后续项（REST 侧 min_length 已使空 ID 双侧一致 fail-closed）。
- v1 replay 影响：tool 项新增身份字段（附加，安全）；v1 同样要求身份（均有，无回归）。

详细见 [修复报告](../review/electron-v2-int2-replay-identity-fix-report-2026-08-13.md) 与 [设计 spec](../superpowers/specs/2026-08-13-electron-v2-int2-replay-identity-fix-design.md)。
