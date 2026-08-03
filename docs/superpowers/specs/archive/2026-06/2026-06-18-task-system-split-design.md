# TaskSystem 拆分设计

> 日期: 2026-06-18  
> 范围: `Module/task_system/task_system.py` (843行) 拆分为 3 个文件——提取查询和任务操作到独立模块

## 目标

将 `TaskSystem` 类中可独立提取的方法组拆为模块级函数，`TaskSystem` 方法变为薄封装委托调用。

## 当前问题

`task_system.py` 843 行，`TaskSystem` 类包含 4 组职责：生命周期、任务操作（pause/resume/cancel/approval）、查询 API（9个方法）、内部工具。

## 目标结构

```
Module/task_system/
├── task_system.py     # TaskSystem 类：生命周期 + 薄封装委托（~350行）
├── lifecycle.py       # 任务操作模块函数（~170行）
└── query.py           # 查询 API + 调试快照 + 工具收集（~290行）
```

### 各文件详情

#### `query.py` — 只读查询函数

从 `task_system.py` 提取 11 个只读方法。每个函数接受显式参数替代 `self._tasks` 等内部状态。

```python
def get_task_status(tasks: dict[TaskId, SubAgent], task_id: TaskId) -> TaskStatus | None
def get_task_mailbox(mailboxes: dict[TaskId, TaskMailbox], task_id: TaskId) -> TaskMailbox | None
async def get_task_summary(tasks: dict[TaskId, SubAgent], task_id: TaskId) -> TaskSummary | None
async def list_active_tasks(tasks: dict[TaskId, SubAgent]) -> list[TaskSummary]
def get_progress(tasks: dict[TaskId, SubAgent], task_id: TaskId) -> TaskProgress | None
async def get_task_dag(tasks: dict[TaskId, SubAgent], task_id: TaskId) -> TaskDAG | None
def get_token_usage(tasks: dict[TaskId, SubAgent], task_id: TaskId) -> TokenUsage | None
def list_agents(
    tasks: dict[TaskId, SubAgent],
    max_sub_agents: int,
    has_slots: bool,
) -> AgentList
def get_token_stats(tasks: dict[TaskId, SubAgent], period: TokenPeriod) -> TokenStats
def get_debug_snapshot(
    tasks: dict[TaskId, SubAgent],
    mailboxes: dict[TaskId, TaskMailbox],
    max_sub_agents: int,
) -> dict
async def collect_tools_used(sub_agent: SubAgent, exec_state) -> list[str]
```

`get_task_summary` 内部委托给 `list_active_tasks()` 然后按 `task_id` 过滤——两个函数共享相同的参数集。

依赖: `Module.task_system.schema`、`SubAgent`、`TaskMailbox`、`datetime`、`parse_iso`

#### `lifecycle.py` — 任务操作函数

从 `task_system.py` 提取 `pause()`、`cancel()`、`submit_approval()`。

`cancel()` 的 PAUSED 分支需要清理（`_cleanup_task` + `_cleanup_storage` + `semaphore.release`），通过回调参数传入：

```python
def pause(tasks: dict[TaskId, SubAgent], task_id: TaskId) -> None

async def cancel(
    tasks: dict[TaskId, SubAgent],
    mailboxes: dict[TaskId, TaskMailbox],
    semaphore: asyncio.BoundedSemaphore | None,
    cleanup_task: Callable[[TaskId], None],
    cleanup_storage: Callable[[TaskId, SubAgent], Awaitable[None]],
    task_id: TaskId,
) -> bool

def submit_approval(
    tool_registry,  # ToolManager 实例
    agent_id: str,
    approved: bool,
) -> bool
```

注意：`cancel()` 的 PAUSED 分支在内部完成 `cleanup_task()` + `cleanup_storage()` + `semaphore.release()`。调用方（`TaskSystem.cancel()` 薄封装）在委托后**不得**再次释放 semaphore 或清理——所有清理由 `lifecycle.cancel()` 内部完成。

依赖: `Module.task_system.schema`、`SubAgent`、`TaskMailbox`、`push_mailbox_event`

#### `task_system.py` — `TaskSystem` 类（精简版）

保留：
- `__init__()` — 状态初始化
- `start()`, `stop()` — 生命周期（逻辑紧密耦合内部状态，不拆）
- `submit()` — 核心入口（含 `_run_and_cleanup` 内部协程）
- `resume()` — 含 `_run_resume` 内部协程
- `_cleanup_task()`, `_cleanup_storage()` — 内部清理
- 所有对外方法签名不变——薄封装委托到 `query.py` / `lifecycle.py`

薄封装示例：
```python
def get_progress(self, task_id: TaskId) -> TaskProgress | None:
    return query.get_progress(self._tasks, task_id)

def pause(self, task_id: TaskId) -> None:
    lifecycle.pause(self._tasks, task_id)

async def cancel(self, task_id: TaskId) -> bool:
    # PAUSED 分支的 semaphore 释放 + 清理已由 lifecycle.cancel() 内部完成
    return await lifecycle.cancel(
        self._tasks, self._mailboxes, self._semaphore,
        self._cleanup_task, self._cleanup_storage, task_id,
    )
```

### 不受影响

- 所有 `TaskSystem` 的消费者——公开 API 签名不变
- `Agent`、`CognitiveLoop`、测试——零改动
- 运行时行为完全不变

## 验证

```bash
python -m pytest Module/task_system/tests/ -v -q
```
