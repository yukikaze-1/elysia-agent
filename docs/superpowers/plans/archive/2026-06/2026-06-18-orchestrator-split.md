# Orchestrator 拆分实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `Module/task_system/core/orchestrator.py` (708行) 的节点执行管线和收尾/checkpoint 方法提取为 `execution.py` / `finalize.py` 模块级函数，Orchestrator 方法变为薄封装委托。

**Architecture:** 创建 `execution.py` 和 `finalize.py`，提取 11 个方法为接受显式参数的模块级函数。薄封装签名与当前方法签名完全一致——`run()` 内部调用点零改动。

**Tech Stack:** Python 3.13, asyncio, Pydantic

## Global Constraints

- 不改变任何运行时行为——纯代码搬移 + `self._xxx` → 参数
- `Orchestrator` 公开 API 不变——`run()` 签名和返回值不变
- 薄封装方法签名与当前方法签名完全一致——`run()` 调用点零改动
- `apply_evaluation` 保持 `async def` + 返回 `EvaluatorAction`
- `evaluate_node` 需要 `plan` 参数（`plan=None` → 短路返回 ABORT）
- `execute_nodes` 接收 `nodes: list[DAGNode]`，返回 `list[NodeResult]`
- `finalize` 返回 `None`（纯提取——`run()` 继续自行构造 SUCCESS result）
- 审批检查/工具匹配循环（`run()` 内）不拆

---

### Task 1: 创建 `execution.py` + `finalize.py` + 更新 `orchestrator.py`

**Files:**
- Create: `Module/task_system/core/execution.py`
- Create: `Module/task_system/core/finalize.py`
- Modify: `Module/task_system/core/orchestrator.py`

- [ ] **Step 1: 创建 `execution.py`**

提取 3 个方法。模块函数参数按"需要什么收什么"原则设计（不传 `self._infra` 等在 v1 未使用的属性）。

```python
# Module/task_system/core/execution.py
"""节点执行管线——execute → evaluate → apply。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from Module.task_system.schema import (
    DAGNode,
    EvaluatorAction,
    ExecutionState,
    NodeId,
    NodeResult,
    NodeResultEvaluation,
    NodeState,
    NodeStatus,
    TaskEventType,
    TaskState,
    TaskId,
    Tool,
    now_iso,
)

if TYPE_CHECKING:
    from Module.task_system.core.evaluator import Evaluator
    from Module.task_system.core.executor import Executor
    from Module.task_system.core.graph import PlanGraph
    from Module.task_system.mailbox import TaskMailbox

logger = logging.getLogger(__name__)


async def execute_nodes(
    executor: Executor,
    exec_state: ExecutionState,
    nodes: list[DAGNode],
    task_id: TaskId,
    tools: list[Tool],
    session_id: str,
) -> list[NodeResult]:
    """执行就绪节点。v1 顺序执行，单节点。"""
    # [原 orchestrator.py L383-410:_execute_nodes 方法体]
    # self._executor → executor
    ...


async def evaluate_node(
    evaluator: Evaluator,
    node: DAGNode,
    result: NodeResult,
    plan,  # Plan | None
) -> NodeResultEvaluation:
    """评估单个节点。plan=None 时短路返回 ABORT。"""
    # [原 orchestrator.py L412-439:_evaluate_node 方法体]
    # self._evaluator → evaluator
    # task_state.plan → plan 参数
    ...


def apply_evaluation(
    node_id: NodeId,
    evaluation: NodeResultEvaluation,
    exec_state: ExecutionState,
    graph: PlanGraph,
    constraints,         # ConstraintConfig — max_retries_per_node
    task_state: TaskState,
    mailbox: TaskMailbox,
) -> EvaluatorAction:
    """应用评估结果——更新 node_states + 推送 PROGRESS 事件。

    返回 EvaluatorAction。NEEDS_REPLAN/BLOCKED 由 run() 中调用方按 action 判定。
    """
    # [原 orchestrator.py L441-592:_apply_evaluation 方法体]
    # self._infra.constraints → constraints
    # self._push_mailbox(...) → finalize.push_mailbox(...)
    ...
```

- [ ] **Step 2: 创建 `finalize.py`**

提取 8 个方法。`finalize` 返回 `None`（纯提取）。

```python
# Module/task_system/core/finalize.py
"""收尾 + checkpoint 工具函数。"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from Module.task_system.mailbox import push_mailbox_event

from Module.task_system.schema import (
    DAGNode,
    ExecutionState,
    NodeId,
    NodeResult,
    NodeResultEvaluation,
    NodeStatus,
    TaskCheckpoint,
    TaskEventType,
    TaskState,
    now_iso,
    parse_iso,
)

if TYPE_CHECKING:
    from Module.task_system.checkpoint import CheckpointManager
    from Module.task_system.core.graph import PlanGraph
    from Module.task_system.mailbox import TaskMailbox

logger = logging.getLogger(__name__)


def init_exec_state(graph: PlanGraph) -> ExecutionState:
    """从 PlanGraph 初始化 ExecutionState。"""
    # [原 orchestrator.py L319-335:_init_exec_state 方法体]
    ...


def merge_exec_state(resume_from: ExecutionState, graph: PlanGraph) -> ExecutionState:
    """合并 resume 的 ExecutionState 到当前 graph。"""
    # [原 orchestrator.py L337-371:_merge_exec_state 方法体]
    ...


def has_blocked_nodes(exec_state: ExecutionState) -> bool:
    """检查是否有等待审批的节点。"""
    # [原 orchestrator.py L373-381:_has_blocked_nodes 方法体]
    ...


def needs_approval(node: DAGNode) -> bool:
    """v1 硬编码返回 False。"""
    # [原 orchestrator.py L666-687:_needs_approval 方法体]
    ...


async def finalize(
    task_state: TaskState,
    exec_state: ExecutionState,
    mailbox: TaskMailbox,
    checkpoint_mgr: CheckpointManager,
) -> None:
    """成功收尾——收集摘要 + 推送 COMPLETED 事件。
    
    run() 在调用后自行构造 OrchestratorResult(status=SUCCESS)。
    """
    # [原 orchestrator.py L593-662:_finalize 方法体]
    # self._checkpoint_mgr → checkpoint_mgr
    # self._push_mailbox(...) → push_mailbox(...)
    ...


def build_checkpoint(
    task_state: TaskState, exec_state: ExecutionState,
) -> TaskCheckpoint:
    """构建 TaskCheckpoint 快照。"""
    # [原 orchestrator.py L689-693:_build_checkpoint 方法体]
    ...


async def save_checkpoint(
    checkpoint_mgr: CheckpointManager,
    task_state: TaskState,
    exec_state: ExecutionState,
) -> None:
    """保存 checkpoint。"""
    # [原 orchestrator.py L695-701:_save_checkpoint 方法体]
    # self._checkpoint_mgr → checkpoint_mgr
    ...


def push_mailbox(
    mailbox: TaskMailbox,
    event_type: TaskEventType,
    data: dict,
) -> None:
    """推送任务事件到 mailbox。"""
    # [原 orchestrator.py L703-708:_push_mailbox 方法体]
    # 注意：原 lazy import "from Module.task_system.mailbox import push_mailbox_event"
    # 已移入模块顶层（Step 2 顶部）
    push_mailbox_event(mailbox, event_type, data, caller="Orchestrator")
```

**关键**：`push_mailbox_event` 从 lazy import 变为模块顶层 import。

- [ ] **Step 3: 更新 `orchestrator.py`**

3a. 添加 import：

```python
from Module.task_system.core import execution, finalize
```

3b. 替换方法为薄封装——**签名与当前完全一致**，内部委托到 `execution.*` / `finalize.*`：

```python
# 当前签名: _execute_nodes(self, nodes, exec_state, task_id, tools, session_id="")
async def _execute_nodes(self, nodes, exec_state, task_id, tools, session_id=""):
    return await execution.execute_nodes(
        self._executor, exec_state, nodes, task_id, tools, session_id,
    )

# 当前签名: _evaluate_node(self, result, node, task_state)
async def _evaluate_node(self, result, node, task_state):
    return await execution.evaluate_node(
        self._evaluator, node, result, task_state.plan,
    )

# 当前签名: _apply_evaluation(self, task_state, exec_state, evaluation, mailbox, graph)
async def _apply_evaluation(self, task_state, exec_state, evaluation, mailbox, graph):
    return execution.apply_evaluation(
        str(evaluation.node_id), evaluation, exec_state, graph,
        self._infra.constraints, task_state, mailbox,
    )

# _finalize → 返回 None（纯提取）
async def _finalize(self, task_state, exec_state, mailbox):
    return await finalize.finalize(
        task_state, exec_state, mailbox, self._checkpoint_mgr,
    )
```

3c. 其余方法也改为薄封装，签名完全一致：

```python
def _init_exec_state(self, graph):
    return finalize.init_exec_state(graph)

def _merge_exec_state(self, resume_from, graph):
    return finalize.merge_exec_state(resume_from, graph)

def _has_blocked_nodes(self, exec_state):
    return finalize.has_blocked_nodes(exec_state)

def _needs_approval(self, node):
    return finalize.needs_approval(node)

def _build_checkpoint(self, task_state, exec_state):
    return finalize.build_checkpoint(task_state, exec_state)

async def _save_checkpoint(self, task_state, exec_state):
    return await finalize.save_checkpoint(self._checkpoint_mgr, task_state, exec_state)

def _push_mailbox(self, mailbox, event_type, data):
    finalize.push_mailbox(mailbox, event_type, data)
```

3d. 移除不再需要的 imports（已移入 `execution.py` / `finalize.py` 的类型）。

3e. **`run()` 调用点零改动**——所有 `self._xxx(...)` 调用签名不变。

- [ ] **Step 4: 运行测试**

```bash
python -m pytest Module/task_system/tests/unit/test_orchestrator.py -v
```

预期: 26 passed

- [ ] **Step 5: 全量 TaskSystem 测试**

```bash
python -m pytest Module/task_system/tests/ -v -q
```

预期: 334 passed

- [ ] **Step 6: 提交**

```bash
git add Module/task_system/core/execution.py Module/task_system/core/finalize.py Module/task_system/core/orchestrator.py
git commit -m "refactor: 拆分 orchestrator.py — 提取 execution.py + finalize.py"
```
