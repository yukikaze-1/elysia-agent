# Orchestrator 拆分设计

> 日期: 2026-06-18  
> 范围: `Module/task_system/core/orchestrator.py` (708行) 拆分为 3 个文件

## 目标

将 `Orchestrator` 类的节点执行管线和收尾/checkpoint 方法提取为模块级函数，纯提取——不改返回类型、不重构逻辑。

## 目标结构

```
Module/task_system/core/
├── orchestrator.py     # Orchestrator 类：run() 主循环 + 薄封装（~300行）
├── execution.py        # 节点执行管线（~270行）
└── finalize.py         # 收尾 + checkpoint 工具（~130行）
```

### 各文件详情

#### `execution.py` — 节点执行管线

纯提取，返回类型不变：

```python
async def execute_nodes(
    executor,              # Executor 实例
    exec_state: ExecutionState,
    nodes: list[DAGNode],  # 已解析的节点列表（审批检查 + 工具匹配由 run() 在调用前完成）
    task_id: TaskId,
    tools: list[Tool],
    session_id: SessionId,
) -> dict[NodeId, NodeResult]


async def evaluate_node(
    evaluator,             # Evaluator 实例
    node: DAGNode,
    result: NodeResult,
    plan,                  # Plan | None — plan=None 时短路返回 ABORT
) -> NodeResultEvaluation


def apply_evaluation(
    node_id: NodeId,
    evaluation: NodeResultEvaluation,
    exec_state: ExecutionState,
    graph,                 # PlanGraph
    constraints,           # ConstraintConfig — 获取 max_retries_per_node
    task_state: TaskState,
    mailbox,               # TaskMailbox — 推送 PROGRESS 事件
) -> EvaluatorAction       # 保持现有返回类型不变
```

`execute_nodes` 当前 v1 顺序执行单节点（非并发），未使用的参数不传入。未使用的预留参数（`infra`, `artifact_store`, `token_ledger`）不传入——v2 需要时再加。

依赖: `Executor`, `Evaluator`, `PlanGraph`, `schema`

#### `finalize.py` — 收尾 + checkpoint 工具

```python
def init_exec_state(graph: PlanGraph) -> ExecutionState

def merge_exec_state(resume_from: ExecutionState, graph: PlanGraph) -> ExecutionState

def has_blocked_nodes(exec_state: ExecutionState) -> bool

def needs_approval(node: DAGNode) -> bool    # v1 硬编码返回 False

async def finalize(
    task_state: TaskState,
    exec_state: ExecutionState,
    mailbox: TaskMailbox,
    checkpoint_mgr: CheckpointManager,
) -> OrchestratorResult
    # SUCCESS 路径：收集结果 → 推送 COMPLETED → 返回 OrchestratorResult(status=SUCCESS)
    # executor/evaluator/infra 等在 v1 中未使用——v2 需要时再加

def build_checkpoint(
    task_state: TaskState,
    exec_state: ExecutionState,
) -> TaskCheckpoint

async def save_checkpoint(
    checkpoint_mgr: CheckpointManager,
    task_state: TaskState,
    exec_state: ExecutionState,
) -> None

def push_mailbox(
    mailbox: TaskMailbox,
    event_type: TaskEventType,
    data: dict,
) -> None
```

依赖: `PlanGraph`, `CheckpointManager`, `TaskMailbox`, `schema`

#### `orchestrator.py` — `Orchestrator` 类（精简版）

保留：
- `__init__()` — 依赖注入
- `run()` — 主循环（调用 `execution.*` / `finalize.*` 替代 `self._xxx` 方法调用）
- 审批检查/工具匹配循环——是 `run()` 主循环的一部分，不拆
- 薄封装方法——一行委托到 `execution.*` / `finalize.*`

薄封装示例：
```python
async def _execute_nodes(self, exec_state, nodes, task_id, tools, session_id):
    return await execution.execute_nodes(
        self._executor, exec_state, nodes, task_id, tools, session_id,
    )
```

### 不受影响

- `SubAgent`——唯一消费者，`Orchestrator` 公开 API 不变
- 所有测试——`Orchestrator` 实例化方式不变
- 运行时行为完全不变——纯提取

## 验证

```bash
python -m pytest Module/task_system/tests/unit/test_orchestrator.py -v
python -m pytest Module/task_system/tests/ -v -q
```
