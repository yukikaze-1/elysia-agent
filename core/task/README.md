# core/task/ — 任务子系统

> 文档导航：[项目文档](../../README.md) · [core/ 总览](../README.md)

任务数据模型和 Worker Pool 调度器。

## 文件布局

```
core/task/
├── __init__.py     # 重导出
├── task.py         # 任务数据模型
└── scheduler.py    # Worker Pool 调度器
```

## 组件

### task.py（222 行）

`Task` dataclass + `TaskType`/`TaskStatus` 枚举：

- `TaskType`: TOOL_CALL / BACKGROUND
- `TaskStatus`: PENDING / DISPATCHED / COMPLETED / FAILED / CANCELLED
- `asyncio.Future` 双向绑定 — 支持 cancel 传播
- 支持 retry（2 次，指数退避）和超时（60s）
- 可序列化 checkpoint（`data/scheduler_checkpoint.json`）

### scheduler.py（510 行）

`TaskScheduler` — Worker Pool（N=5）：

- `dispatch_tool_calls()` — 将 tool_call 列表封装为 Task，提交优先队列
- Worker 循环 — 从队列消费，执行工具，收集结果
- `cancel_all()` — INTERRUPT 时级联 cancel（Future 双向绑定）
- `_prune_loop()` — 定期清理终态 Task
- Checkpoint 持久化

工具执行通过 `ToolExecutor` Protocol 委托，不直接依赖 `ToolManager`。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [core/README.md](../README.md) | Core 层总览 |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 + 并发模型 |
