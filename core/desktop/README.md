# core/desktop — Desktop 工具桥

> 文档导航：[项目文档](../../README.md)

承载 `desktop.*` 工具的「请求 → 幂等去重 → 定向投递 → 结果回传」并发状态机（批次 4，BE-041/043）：后端不操作用户电脑，只做结构化传输，本地执行全部由 Electron Main 完成。

## 数据流

```
desktop 插件 execute() → ctx.services["desktop_bridge"].request_and_wait(call)
  → 锁内：_dedup 命中 pending → 复用 callId + await 原 future（不二次投递）
  → 锁内：建 call doc → repo.upsert(pending) → 登记 _pending[call_id] → deliver(tool.request)
    → Electron 执行 → tool.status（accepted/running 仅更新 repo；result/error/rejected/cancelled 为终态）
      → handle_status：发送连接校验 → 篡改校验 → repo.mark → future resolve
        → request_and_wait 返回 ToolResult
```

## 关键机制

- **幂等去重（锁内完成）**：`_dedup` 索引 `(tool_name, arguments_hash) → call_id`；同参并发在 pending 命中时复用 callId 并 await 原 future，只投递一次（防重复弹窗）。offline / expired 不参与去重——断连后原弹窗不可恢复，重试即新调用。复用方**不持有**首方的登记：它被取消 / 超时不得释放首方的 `_pending` / `_dedup`，`finally` 按 owner 语义兜底释放。
- **固定条纹锁**：`_lock_for(key)` 把 key 哈希到 `_LOCKS_STRIPES` 个桶——同 key 恒同桶（互斥语义与逐 key 锁等价）、空间有界；锁懒建，不在构造期绑定事件循环。key 有两种来源：`(tool_name, arguments_hash)`（`request_and_wait`）与 `call_id`（`cancel_call` / `_reject_tampering`）。
- **先登记后投递（顺序不可颠倒）**：`_pending[call_id] = future` 必须先于 `deliver(...)`，否则极速回传的 status 在登记前到达 → 结果丢弃、future 挂到超时；投递失败则回滚登记并立即收敛为 `UNAVAILABLE`。
- **future 终态唯一**：所有 resolve 走 `_resolve_future` 的 `if not future.done()` 防护（超时 / 取消 / 晚到结果竞态不抛 `InvalidStateError`）；`_await_with_timeout` 用 `asyncio.shield` 包住共享 future，超时只结束本次等待、不取消 future。
- **超时必须标注来源**：桥合成的超时经 `_timeout_result()` 标 `synthesised_timeout=True`——否则它会以"插件返回的 ToolResult"形态绕过 `idempotent` 门控，让 write / select / capture 被重复执行。
- **篡改拒绝**：wire 的 `toolName` / `toolVersion` / `runtime` / `paramHash` 与登记不一致 → 锁内 mark failed（`CALL_IDEMPOTENCY_CONFLICT`）+ 立即 resolve `TOOL_ERROR` + 返回 False（不挂超时，不掩盖 Electron 已产生的副作用）。
- **发送连接校验**：`handle_status(sender_connection_id=…)` 必须等于该 call 的 `target_connection`（防伪造结果）；唯一例外是 offline 调用由当前已连接的 Electron 重传结果——放行并把归属迁移到新连接。
- **断连规则**：`on_client_disconnect` → repo 标 offline + `_connected=False`；未接手的 pending 调用立即 resolve `UNAVAILABLE`（回合及时收尾），accepted / running 调用**不** resolve（可能已有副作用，留到超时或晚到结果，重连重传即 offline → 终态闭环）；重连不自动重投执行请求。
- **敏感纪律**：日志只记 callId / 状态 / 字段名，不打印 arguments 值。

## 文件布局

```
core/desktop/
├── __init__.py   — 重导出 DesktopToolBridge
├── bridge.py     — DesktopToolBridge：生命周期 + 状态机（关键入口见下）
└── tests/        — test_bridge.py（幂等 / 竞态 / 断连 / 超时 / 终态迁移）+ test_bridge_sender.py（发送连接校验）

关联文件:
core/storage/desktop_call_repo.py  — DesktopCallRepository：desktop_calls 集合 + 进程内缓存读权威 + 状态迁移守卫
frontend_contract/ws_message/      — ToolRequestMessage / ToolStatusMessage / ToolStatusEnum（wire 契约单源）
Module/tool/protocol.py            — ToolResult / ErrorType / MediaItem
```

关键入口（`DesktopToolBridge`）：`request_and_wait(call)`（插件委托）/ `handle_status(status, sender_connection_id=…)` / `cancel_call(call_id)` / `get_call(call_id)` / `pending_calls()` / `is_available()` / `connect_db()` / `health_check()` / `storage_health_check()` / `save_all()` / `bind_deliver(...)` / `on_client_connect(connection_id)` / `on_client_disconnect(connection_id)`。

## 依赖与所有权

- **方向**：本包 → `Module.tool.protocol` + `frontend_contract.ws_message` + `core.schema.utils`（`now_iso`）+ `core.storage`。不 import `core.input` / `core.agent` / `core.cognitive`——投递回调由 Agent 反向注入，桥本身不认识连接对象。
- **谁构造**：`Agent.__init__` 建 `DesktopToolBridge(repo=self._desktop_call_repo)` 并公开为 `agent.desktop_bridge`（构造顺序先于 `ToolAdapter`）；`_lifecycle_tools._start_tool_context` 调 `bind_deliver(...)` 绑定定向投递，并经 `ToolContext.services["desktop_bridge"]` 交给 desktop 插件。
- **谁启动 / 探活**：`_lifecycle_persistence` 调 `connect_db()`（建连 + 把启动时刻超时前仍 pending / accepted / running 的调用批量标 expired；清理失败不阻断启动），并把 `storage_health_check` 登记为恢复探针；`save_all()` 是 no-op 预留（repo 即时 upsert，无内存脏集）。
- **谁消费**：`core/input`（`ws_handler` 握手 / 断开挂点 + `agent.sync_desktop_available()`；`ws_handlers/tool.py` 回传 status；`tools_routes` 的调用状态查询与桌面附件持有者校验）、`core/tools/desktop/plugin.py`（`execute()` 委托）、`core/cognitive/loop.py`（中断时 `pending_calls()` → `cancel_call()`）。

## 不属于本包

- 不执行任何本地操作，也不做路径黑名单 / 沙盒 / 用户审批：Electron Main 本地兜底，后端只做结构化传输 + paramHash 绑定。
- 不定义调用状态枚举与迁移守卫（归 `core/storage/desktop_call_repo.py`），不直接访问数据库（只经 `DesktopCallRepository`）。
- 不处理 WS 路由与握手（归 `core/input`），不持有连接对象——只记 `_connection_id`，投递定位交回 Agent 的 deliver 回调。
- 不定义 wire 类型（归契约层 `frontend_contract`），只构造 / 解析契约模型。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../README.md) | 快速开始 + 文档导航 |
| [core/README.md](../README.md) | Core 层总览 |
| [core/storage/README.md](../storage/README.md) | DesktopCallRepository —— 本桥的持久化与状态迁移守卫 |
| [core/tools/desktop/README.md](../tools/desktop/README.md) | `desktop.*` 插件——本桥的请求方与安全预算声明 |
| [批次 4 spec](../../docs/superpowers/specs/archive/2026-08/2026-08-05-electron-backend-batch4-desktop-tools.md) | 桥 / 注册表 / WS 协议 / 审批模型 |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 |
