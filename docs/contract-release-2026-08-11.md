# Contract 发版记录（2026-08-11）

> F5：记录最终契约产物（schema/raw TS/golden frames）的版本与溯源，供 Electron 团队
> replay 与跨仓核对。产物由 `frontend_contract/` 单源生成（`make gen-types` /
> `make gen-frames`），`make check-types` 漂移门禁（含 golden frames + 错误码单源）。

## 版本与哈希

| 项 | 值 |
|---|---|
| transportVersion（握手可用值） | `"2"` |
| protocolVersion（展示） | `"2.0"` |
| schemaHash | `30ece81962a4a669` |
| 后端 commit | `c15ea9e`（`git rev-parse HEAD` 快照） |
| 生成日期 | 2026-08-11 |

## 产物清单

| 产物 | 路径 | 说明 |
|---|---|---|
| TS 快照 | `frontend/src/types/contract.generated.ts` | v1 Vue 只读快照（Vite 构建消费） |
| JSON Schema | `frontend_contract/contract.schema.json` | 机器契约（Electron replay 主源） |
| raw TS | `frontend_contract/contract.raw.ts` | Electron raw wire 类型快照 |
| golden frames | `docs/archive/frontend-handoff-v2/traces/ws-v2-*.jsonl` | 七类契约源 golden（handshake/session/chat/tool/voice/cancel/fail） |

## 本发版含（相对上一批次 9A/9B/9C）

- `ServerInfoFeatures.idempotency_durable`（D6c）：true=幂等 claim 跨重启持久化 / false=内存降级窗口。
- 错误码目录 41 码单源化（`frontend_contract/errors.py` + `scripts/check_error_codes.py`）。

## Electron 交接入口

协议契约变更清单见 `docs/review/electron-v2-backend-change-list-2026-08-11.md`；
协议完整文档见 `docs/electron-backend-protocol-v2.md`。
