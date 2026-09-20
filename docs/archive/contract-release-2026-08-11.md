# Contract 发版记录（2026-08-11）

> ⚠️ **Historical execution record.** 本文描述**执行当时**的系统与结论；后续批次可能推翻其中部分判断。
> 当前事实见 [docs 索引](../README.md) 的 Canonical References（架构 / 不变量 / 测试 / CI / 运维 / 契约）。
> 正文**按当时原样保留**——不改结论、不删失败轨迹、不重写 Challenger 反证（`docs/readme-conventions.md` Rule 3）。

> F5：记录最终契约产物（schema/raw TS/golden frames）的版本与溯源，供 Electron 团队
> replay 与跨仓核对。产物由 `frontend_contract/` 单源生成（`make gen-types` /
> `make gen-frames`），`make check-types` 漂移门禁（含 golden frames + 错误码单源）。

## 版本与哈希

| 项 | 值 |
|---|---|
| transportVersion（握手可用值） | `"2"` |
| protocolVersion（展示） | `"2.0"` |
| schemaHash | `7a5348a4767a9fc6`（**机器契约身份标识**——契约漂移门禁与跨仓 replay 据此对账；`python -m frontend_contract --check` 通过） |
| **contractCommit**（契约生成时后端 SHA） | `5690cfc`（**加固批次 ACK 判别联合 commit**，`git rev-parse HEAD` 快照——契约生成快照，非联调运行时） |
| **runtimeCommit**（联调后端运行时 SHA） | **`<待联调时回填>`**（联调实际启动/冻结的后端 commit；与 `schemaHash` 一起构成冻结基线，Electron 据此固定 runtime） |
| 生成日期 | 2026-08-11（**加固批次重新生成**：ACK 判别联合 + AckError + 嵌套判别联合生成器 + 6 负面帧，见 `docs/review/electron-v2-backend-change-list-2026-08-11.md` §6） |

> **contractCommit ≠ runtimeCommit**：`contractCommit` 只证明「这份契约产物在哪个后端 commit 上生成」；实际联调以后端**运行时** commit 为准——两者可能不同（契约生成后后端又演进，只要 `schemaHash` 未变即 wire 兼容，但 Electron 应对账后端运行时与冻结 manifest）。`runtimeCommit` 留占位，**联调启动时回填**并同步前端 manifest（spec DoD 声明：runbook 唯一列出）。

## 产物清单

| 产物 | 路径 | 说明 |
|---|---|---|
| TS 快照 | `frontend/src/types/contract.generated.ts` | v1 Vue 只读快照（Vite 构建消费） |
| JSON Schema | `frontend_contract/contract.schema.json` | 机器契约（Electron replay 主源） |
| raw TS | `frontend_contract/contract.raw.ts` | Electron raw wire 类型快照 |
| golden frames | `docs/archive/frontend-handoff-v2/traces/ws-v2-*.jsonl` | 八类契约源 golden（handshake/session/chat/tool/voice/cancel/fail/**rejected**——rejected 为加固批次 2026-08-11 新增负面帧） |

## 本发版含（相对上一批次 9A/9B/9C）

- `ServerInfoFeatures.idempotency_durable`（D6c + 2026-08-12 BE-P1-07 强化）：true=幂等 claim 完整跨重启持久化 / false=内存降级窗口 或 存在未 reconcile 到 Mongo 的记录（Mongo 已恢复但 recover() 未跑/未清位时也为 false）。
- 错误码目录 41 码单源化（`frontend_contract/errors.py` + `scripts/check_error_codes.py`）。
- **加固批次（ACK 判别联合，`5690cfc`）**：`user_message.ack` 拆 `AcceptedUserAckMessage`/`RejectedUserAckMessage`（status 判别联合）+ `AckError`；`ErrorMessage` 加可选 `clientRequestId` + 顶层 `retryAfterMs`；6 个命令消息加可选 `clientRequestId`；嵌套判别联合生成器支持；负面 golden 增 6 帧（`CONTRACT_VALIDATION` / `SESSION_NOT_FOUND` / `QUEUE_FULL` / `STORAGE_UNAVAILABLE` / `RATE_LIMITED` / `IN_PROGRESS`）。详见 `docs/review/electron-v2-backend-change-list-2026-08-11.md` §6-10。

## Electron 交接入口

协议契约变更清单见 `docs/review/electron-v2-backend-change-list-2026-08-11.md`；
协议完整文档见 `docs/electron-backend-protocol-v2.md`。
