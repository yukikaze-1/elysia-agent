# core/task/ — 任务子系统

> 文档导航：[项目文档](../../README.md) · [core/ 总览](../README.md)

即时工具调用的数据模型和 Worker Pool 调度器。**当前生产职责只有一个**：
把认知循环当前轮次的 tool_call 列表并行执行完并回收结果。

> ⚠️ **命名与边界在收窄中**：本包名 `core/task` 与 `Module/workflow_engine`（长流程编排）、
> 未来 TaskService（产品任务中心）共用「任务」一词，是已知概念债。收窄方案与决策见
> [架构评估 §ARCH-INC-P1-01](../../docs/review/2026-08-30-后端架构分层与模块化增量评估.md)：
> **仅命名与概念收窄**（改名未执行），预留能力保留。

## 文件布局

```
core/task/
├── __init__.py     # 重导出
├── task.py         # 任务数据模型
└── scheduler.py    # Worker Pool 调度器
```

## 组件

### task.py（236 行）

`Task` dataclass + `TaskType`/`TaskStatus` 枚举：

- `TaskType`: TOOL_CALL / BACKGROUND
- `TaskStatus`: PENDING / DISPATCHED / COMPLETED / FAILED / CANCELLED
- `asyncio.Future` 双向绑定 — 支持 cancel 传播
- 支持 retry（`max_retries` 默认 2，指数退避 cap 30s）；执行超时在 scheduler 侧（60s）

### scheduler.py（549 行）

`TaskScheduler` — Worker Pool（N=5）：

- `dispatch_tool_calls()` — 生产主路径：将 tool_call 列表封装为 Task，提交优先队列，
  `asyncio.gather` 等待结果；**捕获 `CancelledError` 时逐个 `cancel(st.id)` 级联取消**
  （这是 INTERRUPT 的真实取消路径）
- Worker 循环 — 从队列消费，执行工具，收集结果
- `cancel(task_id)` — 级联取消子任务 + 取消 Worker 执行 + 取消 Future
- `_prune_loop()` — 定期清理终态 Task
- Checkpoint 持久化 — `_save_checkpoint()` / `_restore_checkpoint()`（`data/scheduler_checkpoint.json`；**P3-01-RESERVED**，见下）

工具执行经 `ToolExecutor` Protocol（`Module/tool/protocol.py`）委托，不直接依赖 `ToolManager`。

## 扩展储备（`P3-01-RESERVED`）

以下能力**当前无生产消费者，但 2026-09-12 决策保留作扩展储备**（非死代码）——
勿按引用审计删除：

| 项 | 说明 |
|---|---|
| `TaskType.BACKGROUND` / `new_background()` / `dispatch_background()` | 脱离对话流的长时后台任务入口 |
| `depends_on` / `children_ids` / `add_child()` / `get_tree()` | DAG 依赖与任务树 |
| `run_in_background` / `progress` | 后台标记与进度（生产恒 `0.0`） |
| `submit(Task)` / `get_task()` | 通用提交/查询入口 |
| checkpoint 子系统 | 只保存 PENDING + BACKGROUND 且 payload 可 JSON 序列化者；唯一生产者传入 `{"coro": coro}` 会被跳过，故 `data/scheduler_checkpoint.json` 当前不存在 |

判别标准（残留 = 删 / 预留 = 留）见 [todo.md §代码清理](../../todo.md)。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [core/README.md](../README.md) | Core 层总览 |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 + 并发模型 |
| [架构评估 §ARCH-INC-P1-01](../../docs/review/2026-08-30-后端架构分层与模块化增量评估.md) | 命名收窄方案与决策 |
