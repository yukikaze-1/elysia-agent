# SubAgent 拆分设计

> 日期: 2026-06-18  
> 范围: `Module/task_system/core/sub_agent.py` (825行) 拆分为 `sub_agent/` 目录下 5 个文件

## 目标

将 `SubAgent` 类的规划、执行循环、结果处理和 Replan 逻辑提取为模块级函数，`SubAgent` 方法变为薄封装委托。

## 目标结构

```
Module/task_system/core/sub_agent/
├── __init__.py          # 公开导出 SubAgent
├── sub_agent.py         # SubAgent 类：__init__ + run + resume + _dispatch_result + 薄封装（~180行）
├── planning.py          # _plan_and_validate（~112行）
├── execution_loop.py    # _execute（~56行）
├── result_handlers.py   # 5 个 handler（~110行）
└── replan.py            # _handle_replan + _check_meltdown + _extract_node_results（~243行）
```

原 `Module/task_system/core/sub_agent.py` 删除。

注：`_dispatch_result` 保留在 `SubAgent` 中——42 行 if/elif 链，每分支 1-2 行调用，提取为模块函数增加的样板代码（11 个参数签名）超过节省的代码量。

### 各文件详情

#### `planning.py` — 规划 + 校验

```python
async def plan_and_validate(
    planner,              # Planner 实例
    plan_validator,       # PlanValidator 实例
    context: DelegationContext,
    task_state: TaskState,
    mailbox: TaskMailbox,
    constraints: ConstraintConfig,  # global_constraints
    push_mailbox_fn,      # Callable — 推送 mailbox 事件
    abort_error_code_fn,  # Callable — 推导错误码
) -> PlanGraph | None
```

依赖: `Planner`, `PlanValidator`, `schema`

#### `execution_loop.py` — 执行循环

```python
async def execute_loop(
    orchestrator,         # Orchestrator 实例
    task_state: TaskState,
    graph: PlanGraph,
    mailbox: TaskMailbox,
    context: DelegationContext,
    resume_from: ExecutionState | None,
    dispatch_fn,          # Callable → (bool, ExecutionState | None)
    push_mailbox_fn,      # Callable — 中断信号检查中推送 PAUSED/CANCELLED
) -> None
```

`_dispatch_result` 不提取——保留在 `SubAgent` 中作为实方法，直接调用 `result_handlers.*` 和 `self._handle_replan(...)`。

依赖: `Orchestrator`, `schema`

#### `result_handlers.py` — 5 个结果处理器

```python
async def handle_success(result: OrchestratorResult, task_state: TaskState) -> None

async def handle_failed(
    result: OrchestratorResult, task_state: TaskState, mailbox: TaskMailbox,
    push_mailbox_fn,
) -> None

async def handle_cancelled(
    result: OrchestratorResult, task_state: TaskState, mailbox: TaskMailbox,
    push_mailbox_fn,
) -> None

async def handle_blocked(
    result: OrchestratorResult, task_state: TaskState, mailbox: TaskMailbox,
    push_mailbox_fn,
) -> None

async def handle_paused(
    result: OrchestratorResult, task_state: TaskState, mailbox: TaskMailbox,
    checkpoint_mgr: CheckpointManager, push_mailbox_fn,
) -> None
```

依赖: `CheckpointManager`, `schema`

#### `replan.py` — Replan 决策 + 熔断

```python
async def handle_replan(
    result: OrchestratorResult,
    task_state: TaskState,
    graph: PlanGraph,
    mailbox: TaskMailbox,
    context: DelegationContext,
    replanner: Replanner,
    plan_validator: PlanValidator,
    orchestrator,         # 仅读取 _exec_state 检查中断信号
    checkpoint_mgr: CheckpointManager,
    constraints: ConstraintConfig,
    push_mailbox_fn,
) -> tuple[bool, ExecutionState | None]

def check_meltdown(plan_version: int, max_failure_count: int) -> str | None

def extract_node_results(checkpoint: TaskCheckpoint) -> dict[str, NodeState]
```

依赖: `Replanner`, `PlanValidator`, `CheckpointManager`, `schema`

#### `sub_agent.py` — `SubAgent` 类（精简版）

保留：
- `__init__` — 依赖注入
- `run()` / `resume_from_pause()` — 入口
- `_dispatch_result()` — if/elif 分发链（42 行，保留在类中）
- 薄封装: `_plan_and_validate`, `_execute`, 5 个 handler, `_handle_replan`, `_check_meltdown`, `_extract_node_results`
- `_push_mailbox` / `_abort_error_code` — static helper，作为回调传入提取的函数

### 消费者

- `task_system.py`：`from Module.task_system.core.sub_agent import SubAgent` → 路径不变（`__init__.py` re-export）

### 不受影响

- 所有 `SubAgent` 的消费者——import 路径不变
- 运行时行为完全不变

## 验证

```bash
python -m pytest Module/task_system/tests/unit/test_sub_agent.py -v
python -m pytest Module/task_system/tests/ -v -q
```
