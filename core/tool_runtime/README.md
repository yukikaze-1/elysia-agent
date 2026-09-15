# core/tool_runtime/ — 即时工具调用运行时

> 文档导航：[项目文档](../../README.md) · [core/ 总览](../README.md)

即时工具调用的数据模型和 Worker Pool 调度器。**当前生产职责只有一个**：
把认知循环当前轮次的 tool_call 列表并行执行完并回收结果。

> ✅ **改名已完成（2026-09-13，ARCH-INC-P1-01）**：本包原名 `core/task`，与
> `Module/workflow_engine`（长流程编排）、未来 TaskService（产品任务中心）共用「任务」
> 一词，是已知概念债。按
> [架构评估 §ARCH-INC-P1-01](../../docs/review/2026-08-30-后端架构分层与模块化增量评估.md)
> 的决策完成**命名与概念收窄**（桩代码按同批决策保留）：
>
> | 旧 | 新 |
> |---|---|
> | `core/task/` | `core/tool_runtime/` |
> | `TaskScheduler` | `ToolCallDispatcher` |
> | `Task` | `QueuedToolCall` |
> | `TaskType` / `TaskStatus` | `QueuedToolCallKind` / `QueuedToolCallState` |
> | `Task.parent_id` | `QueuedToolCall.request_id` |
>
> `TaskType`/`TaskStatus` 一并改名是因为它们在本包外**零引用**，留着会与
> `QueuedToolCall` 自相矛盾。checkpoint 反序列化兼容旧键（`parent_id` 仍可读）。

## 文件布局

```
core/tool_runtime/
├── __init__.py     # 重导出
├── task.py         # QueuedToolCall 数据模型
└── scheduler.py    # ToolCallDispatcher（Worker Pool）
```

## 组件

### task.py（237 行）

`QueuedToolCall` dataclass + `QueuedToolCallKind`/`QueuedToolCallState` 枚举：

- `QueuedToolCallKind`: TOOL_CALL / BACKGROUND
- `QueuedToolCallState`: PENDING / DISPATCHED / COMPLETED / FAILED / CANCELLED
- `asyncio.Future` 双向绑定 — 支持 cancel 传播
- 支持 retry（`max_retries` 默认 2，指数退避 cap 30s）；超时预算与重试门控见
  [重试与超时契约](#重试与超时契约2026-09-16)

### scheduler.py（771 行）

`ToolCallDispatcher` — Worker Pool（N=5）：

- `dispatch_tool_calls()` — 生产主路径：将 tool_call 列表封装为 QueuedToolCall，提交优先队列，
  `asyncio.gather` 等待结果；**捕获 `CancelledError` 时逐个 `cancel(st.id)` 级联取消**
  （这是 INTERRUPT 的真实取消路径）
- Worker 循环 — 从队列消费，执行工具，收集结果
- `cancel(task_id)` — 级联取消子任务 + 取消 Worker 执行 + 取消 Future
- `_prune_loop()` — 定期清理终态 QueuedToolCall
- Checkpoint 持久化 — `_save_checkpoint()` / `_restore_checkpoint()`（`data/scheduler_checkpoint.json`；**P3-01-RESERVED**，见下）

工具执行经 `ToolExecutor` Protocol（`Module/tool/protocol.py`）委托，不直接依赖 `ToolManager`。

## 重试与超时契约（2026-09-16）

**重试安全不是插件单方面的责任，也不是调度器的猜测——判据跟着数据走。**
`ToolResult.synthesised_timeout`（`Module/tool/protocol.py`）标记"这个 TIMEOUT 是
**运行时合成的**，不是插件自己返回的"；调度器的重试门控**只读这一个字段**
（`not result.synthesised_timeout or declares_idempotent(tool_name)`），不靠"这段
代码在哪"推断——否则每多一个合成点就要在门控上再补一条。

| 失败来源 | 谁说的 | 是否重试 |
|---|---|---|
| 插件返回 `RATE_LIMITED` / `NETWORK_ERROR` / `TIMEOUT` | 插件（`ToolResult.error_type`） | **是**——按 `ErrorType.is_retryable`，与 `SafetyProfile.idempotent` **无关** |
| 插件返回其他分类 / 不带 `error_type` | 插件 | 否（`fail()` 缺省 `error_type=None`） |
| **运行时合成**的 TIMEOUT（`synthesised_timeout=True`） | 管道 / 调度器的 `wait_for` | **仅当**插件声明 `SafetyProfile.idempotent=True` |

`SafetyProfile.idempotent` 的语义是"重复调用不产生额外副作用"（只读 / 可安全重入），
默认 `False` 是 fail-safe：**忘记声明 → 少重试一次，而不是把副作用做两遍**。调度器经
`executor.declares_idempotent(tool_name)` 询问（`ToolAdapter` → `ToolManager` → 插件
声明；未知工具 → `False`）——与 `declared_request_timeout` 同一纪律，**不使用
`getattr` 兜底**：缺实现必须响亮报错，静默兜底会把"重复副作用"藏起来。

**两个超时合成点**（都在结果上置 `synthesised_timeout=True`）：

1. **管道层**：`Module/tool/safety.py::SafetyPipeline.execute` 围绕
   `plugin.execute(...)` 的 `wait_for(plugin.safety.request_timeout)`，也是**先触发**的
   那一个；
2. **调度器层**：`scheduler._execute_tool` 的 `asyncio.TimeoutError` 分支，单次尝试
   预算由 `_timeout_for(tool_name)` 给出。

**两条预算：内层先到期。**

| 预算 | 值 | 由谁定 |
|---|---|---|
| 内层 `SafetyProfile.request_timeout` | 插件声明（缺省 30s；desktop/vision 120s） | 插件 |
| 外层 `_TOOL_TIMEOUT` / `_MAX_TOOL_TIMEOUT` | 60s / 125s（`_timeout_for` = `min(_MAX_TOOL_TIMEOUT, max(_TOOL_TIMEOUT, declared + _TIMEOUT_SLACK))`） | 调度器 |

外层**恒 ≥ 内层**（未声明时 60 > 30；声明 120 时 125 > 120），所以超时几乎总是先在
**管道层**触发。R4 门禁（`make check-timeouts`）另行保证插件声明的 `request_timeout`
≤ `_MAX_PLUGIN_REQUEST_TIMEOUT`（120s），并校验 `_CALL_BUDGET` / `_DISPATCH_TIMEOUT`
能容纳"首发 + `max_retries` 次重试"的最坏路径。

重试仍用**同一个 `call_id`** 再 invoke 一次，且调度层**没有**按 `call_id` 的去重/缓存
（`core/storage/idempotency.py` 只给 v2 用户消息去重）——因此"重试 = 副作用可能再落
一次"这条现实，以及上面每一条边界的正反两个方向，都由
`core/tool_runtime/tests/test_tool_retry_idempotency.py` 逐条钉住；
"哪些工具会被超时重试"的声明面由 `core/tools/tests/test_idempotency_convention.py`
逐插件表态（新增插件不表态 → 红）。

## 扩展储备（`P3-01-RESERVED`）

以下能力**当前无生产消费者，但 2026-09-12 决策保留作扩展储备**（非死代码）——
勿按引用审计删除：

| 项 | 说明 |
|---|---|
| `QueuedToolCallKind.BACKGROUND` / `new_background()` / `dispatch_background()` | 脱离对话流的长时后台任务入口 |
| `depends_on` / `children_ids` / `add_child()` / `get_tree()` | DAG 依赖与任务树 |
| `run_in_background` / `progress` | 后台标记与进度（生产恒 `0.0`） |
| `submit(QueuedToolCall)` / `get_task()` | 通用提交/查询入口 |
| checkpoint 子系统 | 只保存 PENDING + BACKGROUND 且 payload 可 JSON 序列化者；唯一生产者传入 `{"coro": coro}` 会被跳过，故 `data/scheduler_checkpoint.json` 当前不存在 |

判别标准（残留 = 删 / 预留 = 留）见 [todo.md §代码清理](../../todo.md)。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [core/README.md](../README.md) | Core 层总览 |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 + 并发模型 |
| [架构评估 §ARCH-INC-P1-01](../../docs/review/2026-08-30-后端架构分层与模块化增量评估.md) | 命名收窄方案与决策 |
