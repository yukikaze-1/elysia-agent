# TaskSystem Debug WebUI 设计文档

> 日期: 2026-06-16
> 状态: 后端+前端已实现，待 TaskSystem 接入应用层（app['task_system'] 注册）即可激活

---

## 1. 背景

TaskSystem 已实现完整的 Plan-Execute-Evaluate-Replan 闭环（259 tests），但运行时完全不可见——没有 UI 能查看当前有哪些后台任务在执行、DAG 进度如何、哪个节点失败了。架构文档中规划的 `debug_api.py` 和 `/debug/tasks` 页面尚未创建。

## 2. 设计目标

**MVP**：任务列表 + 基本状态 + 实时进度 + 文本 DAG（非图形化）
**后续迭代**：DAG 图形可视化

## 3. 总体架构

```
TaskSystem (singleton)
  ├── SubAgent._task_state    ──┐
  ├── SubAgent._graph         ──┤  TaskDebugAPI 内部 500ms 轮询
  └── Orchestrator._exec_state ─┘       │
                                         │ diff → SSE events
                                         ▼
                              GET /debug/api/tasks        (JSON 快照)
                              GET /debug/api/tasks/stream (SSE 实时推送)
                                         │
                                         ▼
                              debug.html 新增「任务」tab
                                └── EventSource 消费 SSE
```

**关键设计决策**：
- SSE 实时推送给前端，但后端用内部轮询（500ms）+ diff 生成事件。TaskSystem 不改 hook 机制。
- 前端直接消费 SSE EventSource，与现有 Overview tab 的 HTTP poll 并行运行
- 页面位置：在现有 `debug.html` 新增第三个 tab「任务」

## 4. 数据归属

让 SubAgent 和 Orchestrator 将运行时状态存为实例属性——debug API 直接读，不经 stub 方法：

```
SubAgent (per-task，生命周期 = 任务创建→终结)
  ├── self._task_state: TaskState      ← run() 入口创建，replan 后 plan 更新
  ├── self._graph: PlanGraph           ← _plan_and_validate() 创建，
  │                                      _handle_replan() 原地 patch/replace
  └── self._orchestrator: Orchestrator
        └── self._exec_state: ExecutionState  ← run() 入口创建，循环中原地更新
```

TaskDebugAPI 访问路径：
- `sub_agent._task_state` → status, plan_version, error, created_at
- `sub_agent._graph` → DAG 结构 + `to_dag_view(exec_state)`
- `sub_agent._orchestrator._exec_state` → node_states（实时）, current_phase
- `TaskSystem._mailboxes[task_id].count()` → queue depth
  - 注：mailbox 由 TaskSystem 持有，SubAgent 不持有引用

## 5. 后端组件

### 5.1 `Module/task_system/debug_api.py` — 新建

`TaskDebugAPI` 类：
- `start()` — 启动 500ms 轮询循环
- `_poll_loop()` — 收集快照 → diff → broadcast SSE events
- `_collect_snapshot()` — 调 `TaskSystem._get_debug_snapshot()` 获取原始数据，组装 view model
- `_diff(old, new)` — 比较两次快照，产出 SSE 事件列表
- `subscribe() / unsubscribe()` — SSE 客户端管理
- `sse_handler(request)` — aiohttp SSE endpoint handler
- `snapshot_handler(request)` — `GET /debug/api/tasks` JSON endpoint

**SSE 事件类型**：

| event | 触发条件 | data |
|-------|---------|------|
| `snapshot` | 新客户端订阅 | 全量 tasks 数组（node_states 裁剪：只含 `node_id, status, attempt, summary, error`——不序列化 `evaluation`，那是 Planner replan 内部用的，Debug UI 不需要） |
| `task_new` | `_tasks` 新增 task_id | TaskSummary |
| `task_update` | TaskState.status / 进度变化 | `{task_id, status, completed, total}` |
| `task_done` | 终态（completed/failed/cancelled） | `{task_id, status, error}` |
| `node_update` | NodeState.status 变化 | `{task_id, node_id, status, attempt}` |
| `heartbeat` | 30s 无其他事件 | `{}` |

### 5.2 `Module/task_system/task_system.py` — 新增方法

```python
def _get_debug_snapshot(self) -> dict:
    """供 TaskDebugAPI 内部轮询使用。

    线程安全说明：
        本方法在 TaskDebugAPI._poll_loop() 中调用，与 Orchestrator 主循环
        运行在同一个 asyncio event loop 中。CPython + asyncio 的协作式调度
        保证本方法在遍历 _tasks / _exec_state 期间不会在非 await 点被挂起——
        因此读取 exec_state.node_states 等可变 dict 时不需显式加锁。

        约束：本方法内禁止加 await（如从磁盘加载 TaskState）。
        若将来需要异步 I/O，必须在 await 之前复制快照，await 之后重新获取
        exec_state 引用——Orchestrator 可能在 await 期间已替换整个对象。
    """
    tasks = []
    for task_id, sub_agent in self._tasks.items():
        exec_state = sub_agent._orchestrator._exec_state
        tasks.append({
            "task_state": sub_agent._task_state,
            "graph": sub_agent._graph,
            "exec_state": exec_state,
            "mailbox_depth": self._mailboxes[task_id].count(),
        })
    return {
        "tasks": tasks,
        "slots_total": self._max_sub_agents,
        "slots_used": len(self._tasks),
    }
```

三个公共查询方法一并实现：

| 方法 | 返回 | 数据源 |
|------|------|--------|
| `list_active_tasks()` | `list[TaskSummary]` | 遍历 `_tasks`，从 `sub_agent._task_state` + `checkpoint.exec` 聚合 |
| `get_task_dag(task_id)` | `TaskDAG \| None` | `sub_agent._graph.to_dag_view(exec_state)` — 此方法已实现 |
| `get_task_summary(task_id)` | `TaskSummary \| None` | 聚合：`_task_state`（status/duration）+ `exec_state.node_states`（completed/failed/skipped 计数）+ `plan.dag`（tools_used）+ `plan.plan_version`（replan 次数） |

`get_task_summary` 构建细节：
- `nodes`: 遍历 `exec_state.node_states` 统计各 NodeStatus 计数
- `tools_used`: 从 `plan.dag.nodes` 提取 `description` 中提到的工具名（v1 不做精确追踪）
- `approvals`: v1 始终返回 `{"requested": 0, "approved": 0, "denied": 0}`（审批功能未实现）
- `token_usage`: v1 返回空（token 追踪未接入）
- `replans`: `plan.plan_version - 1`

### 5.3 `Module/task_system/core/sub_agent.py` — 新增实例属性

```python
# run() 入口：
self._task_state = task_state    # TaskState

# _plan_and_validate() 返回后 / _handle_replan() 中：
self._graph = graph              # PlanGraph
```

### 5.4 `Module/task_system/core/orchestrator.py` — exec_state 提升为实例属性

```python
# run() 方法内：
self._exec_state = exec_state   # ExecutionState（循环中原地更新）
```

### 5.5 路由注册 — `core/input/server.py`

```python
task_debug_api = TaskDebugAPI(task_system)
await task_debug_api.start()   # 在 on_startup 中

app.router.add_get("/debug/api/tasks", task_debug_api.snapshot_handler)
app.router.add_get("/debug/api/tasks/stream", task_debug_api.sse_handler)
```

## 6. 前端 — debug.html 新增「任务」Tab

### 布局

```
┌─────────────────────────────────────────────────────┐
│ [概览]  [数据库]  [任务]                             │
├─────────────────────────────────────────────────────┤
│  ┌─ TaskSystem 概览 ─────────────────────────────┐  │
│  │ 运行中: 3  可用槽位: 2/5  SSE: ●已连接         │  │
│  └───────────────────────────────────────────────┘  │
│                                                     │
│  ┌─ task_1 (search_and_report) ── RUNNING ───────┐  │
│  │ intent: 搜索 AI 安全最新进展                    │  │
│  │ 耗时: 2m34s  Replan: 2/5  进度: ████░░ 67%    │  │
│  │ DAG:                                            │  │
│  │  ✅ search_ai_safety ─────────────┐              │  │
│  │  ⏳ search_value_align ──────────┤              │  │
│  │  ⊗ search_jailbreak ────────────┤              │  │
│  │                                   ├→ ⬜ compile  │  │
│  │  edges: search_ai→compile, search_val→compile   │  │
│  └───────────────────────────────────────────────┘  │
│                                                     │
│  ┌─ task_2 (get_time) ── COMPLETED ───────────────┐ │
│  │ 耗时: 10.5s  DAG: ✅ get_current_time (1/1)    │  │
│  └────────────────────────────────────────────────┘  │
│                                                     │
│  ┌─ task_3 ── FAILED ── 已 Replan 5 次，触发熔断 ─┐  │
│  └────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

**DAG 渲染策略**：
- **链状 DAG**（A→B→C）：缩进文本 + 状态 emoji（✅⏳⬜⊗），每节点独占一行
- **并行分支**（A→C, B→C）：用 ASCII 边 `─┐├→` 连接，显示汇合点
- 纯缩进无法表达并行关系——JS 渲染逻辑需根据 edges 计算汇合节点，生成 ASCII 连接线
- 图形化 DAG 可视化留到后续迭代

### 实现

- JS 追加在现有 `<script>` 中（~250 行）
- `EventSource('/debug/api/tasks/stream')` 监听 SSE
- 事件路由：`snapshot` → 全量渲染 / `task_new` → 插入新卡片 / `task_update` → 更新状态 / `task_done` → 折叠到底部 / `node_update` → 更新 DAG 节点状态
- 断连时指数退避重连（同现有 Overview tab 的 fetch 重试模式）
- DAG 用缩进文本 + 状态 emoji（✅⏳⬜⊗）
- 已完成/失败任务折叠为摘要行
- 复用现有 CSS 变量（`--bg`, `--surface`, `--accent`）

## 7. 改动汇总

| 文件 | 操作 | 说明 |
|------|------|------|
| `Module/task_system/debug_api.py` | **新建** | TaskDebugAPI 类（~200 行） |
| `Module/task_system/task_system.py` | 修改 | `_get_debug_snapshot()` + 3 个公共查询方法 |
| `Module/task_system/core/sub_agent.py` | 修改 | `self._task_state` + `self._graph` |
| `Module/task_system/core/orchestrator.py` | 修改 | `exec_state` → `self._exec_state` |
| `core/input/server.py` | 修改 | 2 条新路由 + on_startup 启动 TaskDebugAPI |
| `static/debug.html` | 修改 | HTML: 新增 tab 按钮 + `<section id="tab-tasks">` 容器 / JS: 新增 `TaskTab` 对象（~250 行），放在现有 global namespace / CSS: 复用现有变量，新增任务卡片样式（~30 行） |

## 8. 生命周期协调

`TaskSystem._cleanup_task()` 在任务终态后移除 `_tasks` 中的 SubAgent 引用。这会导致下一轮 `_poll_loop()` 的 snapshot 不再包含该任务。

为保证前端在 `task_done` 事件后一定时间内仍能看到终态任务的最终状态：

1. `TaskDebugAPI._poll_loop()` 在 diff 时检测到任务消失（上次 snapshot 有、本次无）→ 发出 `task_done` 事件（携带最终状态），然后从 snapshot 中移除
2. 或者：`TaskSystem._cleanup_task()` 在移除前等待一小段延迟（如 2s），给 `_poll_loop()` 足够的窗口发出 `task_done`

**推荐方案 1**——由 `_diff()` 负责检测任务消失并发出最终 `task_done` 事件，不需要 TaskSystem 配合。实现：`_poll_loop()` 持有 `_last_task_ids: set`，diff 时计算 `removed = last_ids - current_ids`，为每个 removed task 补发 `task_done`。

## 9. 兼容性验证

| 设计项 | 现有代码支持度 | 风险 |
|--------|-------------|------|
| `PlanGraph.to_dag_view()` | 已实现，可直接用 | 无 |
| `_get_debug_snapshot()` 读 `_exec_state` | Orchestrator 当前无 `_exec_state` 属性 | 需在 `run()` 中 `self._exec_state = exec_state` |
| `SubAgent._task_state` | `task_state` 是 `run()` 局部变量 | 需改为 `self._task_state = task_state` |
| `SubAgent._graph` | `graph` 在 `_execute()` 中局部持有 | 需改为 `self._graph = graph` |
| `TaskMailbox.count()` | 已实现 | 无 |
| SSE 端点路由 | aiohttp 原生支持 `StreamResponse` | 无 |

## 10. 验证

```bash
# 1. 启动应用，打开 /debug → 点击「任务」tab → 应看到空状态（无任务）
# 2. 触发一个后台任务（如通过聊天界面提搜索需求）→ 任务卡片实时出现
# 3. 观察 DAG 节点状态随执行进展实时更新（pending→running→success/failed）
# 4. 触发 5 次 replan 熔断 → 看到 FAILED 状态 + 错误信息
# 5. 刷新页面 → SSE 重连，收到 snapshot 全量恢复
# 6. 关闭 tab → SSE 断开，服务端清理 subscriber
```
