# SubAgent 拆分实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `Module/task_system/core/sub_agent.py` (825行) 拆分为 `sub_agent/` 目录下 6 个文件，`SubAgent` 方法变为薄封装委托。

**Architecture:** 删除 `sub_agent.py` → 创建 `sub_agent/` 包（`planning.py` + `execution_loop.py` + `result_handlers.py` + `replan.py` + `sub_agent.py` + `__init__.py`）。`_dispatch_result` 保留在 `SubAgent` 中（42 行 if/elif 不提取）。

**Tech Stack:** Python 3.13, asyncio

## Global Constraints

- `SubAgent` 公开 API 不变——`run()` / `resume_from_pause()` 签名不变
- `_dispatch_result` 保留在 `SubAgent` 中——不提取
- `_push_mailbox` 保留为 `SubAgent` static 方法，作为回调传入提取函数
- `_abort_error_code` 保留为 `SubAgent` static 方法，作为回调传入 `planning.py`
- 消费者 import 路径不变（`from Module.task_system.core.sub_agent import SubAgent`）

---

### Task 1: 删除旧文件 + 创建 `sub_agent/` 包

**Files:**
- Delete: `Module/task_system/core/sub_agent.py`
- Create: `Module/task_system/core/sub_agent/__init__.py`
- Create: `Module/task_system/core/sub_agent/planning.py`
- Create: `Module/task_system/core/sub_agent/execution_loop.py`
- Create: `Module/task_system/core/sub_agent/result_handlers.py`
- Create: `Module/task_system/core/sub_agent/replan.py`
- Create: `Module/task_system/core/sub_agent/sub_agent.py`

- [ ] **Step 1: 删除旧文件 + 创建目录**

```bash
mkdir -p Module/task_system/core/sub_agent
git rm Module/task_system/core/sub_agent.py
```

- [ ] **Step 2: 创建 `planning.py`**

从 `sub_agent.py:238-346` 提取 `_plan_and_validate`。变换规则：
- `self._planner` → `planner` 参数
- `self._plan_validator` → `plan_validator` 参数
- `self._infra.constraints` → `constraints` 参数
- `self._push_mailbox(...)` → `push_mailbox_fn(...)`
- `self._abort_error_code(...)` → `abort_error_code_fn(...)`

```python
# Module/task_system/core/sub_agent/planning.py
"""规划 + 校验——Planner.plan() → PlanValidator.validate()。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from Module.task_system.schema import (
    DelegationContext,
    PlanGraph,     # TYPE_CHECKING — 实际在 validate() 返回
    TaskState,
    TaskStatus,
    ValidationAction,
    now_iso,
)

if TYPE_CHECKING:
    from Module.task_system.core.planner import Planner
    from Module.task_system.core.plan_validator import PlanValidator
    from Module.task_system.mailbox import TaskMailbox

logger = logging.getLogger(__name__)


async def plan_and_validate(
    planner: Planner,
    plan_validator: PlanValidator,
    context: DelegationContext,
    task_state: TaskState,
    mailbox: TaskMailbox,
    constraints,          # ConstraintConfig — 来自 self._infra.constraints
    push_mailbox_fn,      # Callable — 推送 mailbox 事件
    abort_error_code_fn,  # Callable — 从 ValidationIssue 推导 TaskErrorCode
) -> PlanGraph | None:
    """规划 + 校验。返回 PlanGraph 或 None（ABORT）。"""
    # [原 sub_agent.py L255-346:_plan_and_validate 方法体]
    # self._planner → planner
    # self._plan_validator → plan_validator
    # self._infra.constraints → constraints
    # self._push_mailbox(...) → push_mailbox_fn(...)
    # self._abort_error_code(...) → abort_error_code_fn(...)
    ...
```

- [ ] **Step 3: 创建 `execution_loop.py`**

从 `sub_agent.py:350-404` 提取 `_execute`（重命名为 `execute_loop`）。

```python
# Module/task_system/core/sub_agent/execution_loop.py
"""执行-决策主循环。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from Module.task_system.schema import (
    DelegationContext,
    ExecutionState,
    OrchestratorResult,
    TaskState,
    TaskStatus,
    TaskEventType,
    now_iso,
)

if TYPE_CHECKING:
    from Module.task_system.core.graph import PlanGraph
    from Module.task_system.core.orchestrator import Orchestrator
    from Module.task_system.mailbox import TaskMailbox

logger = logging.getLogger(__name__)


async def execute_loop(
    orchestrator: Orchestrator,
    task_state: TaskState,
    graph: PlanGraph,
    mailbox: TaskMailbox,
    context: DelegationContext,
    resume_from: ExecutionState | None,
    dispatch_fn,         # Callable → (bool, ExecutionState | None)
    push_mailbox_fn,     # Callable — 中断信号检查中推送 PAUSED/CANCELLED
) -> None:
    """执行-决策主循环。中断信号在循环顶检查，防止被 _merge_exec_state 吞掉。"""
    # [原 sub_agent.py L368-404:_execute 方法体]
    # self._orchestrator → orchestrator
    # self._push_mailbox(...) → push_mailbox_fn(...)
    # self._dispatch_result(...) → dispatch_fn(...)
    # self._orchestrator._exec_state → orchestrator._exec_state
    ...
```

- [ ] **Step 4: 创建 `result_handlers.py`**

从 `sub_agent.py:452-546` 提取 5 个 handler。

```python
# Module/task_system/core/sub_agent/result_handlers.py
"""Orchestrator 结果处理器——SUCCESS / FAILED / CANCELED / BLOCKED / PAUSED。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from Module.task_system.schema import (
    OrchestratorResult,
    TaskErrorCode,
    TaskEventType,
    TaskState,
    now_iso,
)

if TYPE_CHECKING:
    from Module.task_system.checkpoint import CheckpointManager
    from Module.task_system.mailbox import TaskMailbox

logger = logging.getLogger(__name__)


async def handle_success(
    result: OrchestratorResult, task_state: TaskState,
) -> None:
    """所有 required 节点完成——任务成功。"""
    # [原 sub_agent.py L452-458:_handle_success 方法体]
    ...


async def handle_failed(
    result: OrchestratorResult,
    task_state: TaskState,
    mailbox: TaskMailbox,
    push_mailbox_fn,
) -> None:
    """不可恢复失败——推送 FAILED 事件。"""
    # [原 sub_agent.py L462-478:_handle_failed 方法体]
    # self._push_mailbox(...) → push_mailbox_fn(...)
    ...


async def handle_cancelled(
    result: OrchestratorResult,
    task_state: TaskState,
    mailbox: TaskMailbox,
    push_mailbox_fn,
) -> None:
    """外部取消——推送 CANCELLED 事件。"""
    # [原 sub_agent.py L482-497:_handle_cancelled 方法体]
    ...


async def handle_blocked(
    result: OrchestratorResult,
    task_state: TaskState,
    mailbox: TaskMailbox,
    push_mailbox_fn,
) -> None:
    """等待审批——推送 APPROVAL_NEEDED 事件。"""
    # [原 sub_agent.py L501-518:_handle_blocked 方法体]
    ...


async def handle_paused(
    result: OrchestratorResult,
    task_state: TaskState,
    mailbox: TaskMailbox,
    checkpoint_mgr: CheckpointManager,
    push_mailbox_fn,
) -> None:
    """暂停——保存 checkpoint + 推送 PAUSED 事件。"""
    # [原 sub_agent.py L522-546:_handle_paused 方法体]
    # self._checkpoint_mgr → checkpoint_mgr
    # self._push_mailbox(...) → push_mailbox_fn(...)
    ...
```

- [ ] **Step 5: 创建 `replan.py`**

从 `sub_agent.py:550-751` + `sub_agent.py:755-794` 提取。

```python
# Module/task_system/core/sub_agent/replan.py
"""Replan 决策——熔断检查 → Replanner.replan() → DAGPatch 应用。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from Module.task_system.schema import (
    DAGPatch,
    DelegationContext,
    ExecutionState,
    NodeId,
    NodeState,
    OrchestratorResult,
    Plan,
    TaskCheckpoint,
    TaskErrorCode,
    TaskEventType,
    TaskState,
    TaskStatus,
    now_iso,
)

if TYPE_CHECKING:
    from Module.task_system.checkpoint import CheckpointManager
    from Module.task_system.core.graph import PlanGraph
    from Module.task_system.core.planner import Replanner
    from Module.task_system.core.plan_validator import PlanValidator
    from Module.task_system.mailbox import TaskMailbox

logger = logging.getLogger(__name__)


def check_meltdown(plan_version: int, max_failure_count: int) -> str | None:
    """检查是否触发熔断。返回原因或 None。"""
    # [原 sub_agent.py L755-777:_check_meltdown 方法体]
    # self._infra.constraints.max_failure_count → max_failure_count
    # task_state.plan.plan_version → plan_version
    ...


def extract_node_results(checkpoint: TaskCheckpoint) -> dict[str, NodeState]:
    """从 checkpoint 提取 node_states，供 Replanner 使用。"""
    # [原 sub_agent.py L781-794:_extract_node_results 方法体]
    ...


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
    constraints,          # ConstraintConfig
    push_mailbox_fn,
) -> tuple[bool, ExecutionState | None]:
    """结构性失败——熔断 → Replan → DAGPatch 应用 → 构建 resume_state。"""
    # [原 sub_agent.py L557-751:_handle_replan 方法体]
    # self._check_meltdown(...) → check_meltdown(...)
    # self._replanner → replanner
    # self._plan_validator → plan_validator
    # self._orchestrator → orchestrator
    # self._checkpoint_mgr → checkpoint_mgr
    # self._infra.constraints → constraints
    # self._push_mailbox(...) → push_mailbox_fn(...)
    # self._extract_node_results(...) → extract_node_results(...)
    ...
```

- [ ] **Step 6: 创建 `sub_agent.py`** — `SubAgent` 类（精简版）

保留 `__init__` + `run()` + `resume_from_pause()` + `_dispatch_result()` + 薄封装 + `_push_mailbox` / `_abort_error_code`。

```python
# Module/task_system/core/sub_agent/sub_agent.py
"""Sub-Agent —— TaskSystem 池中的任务级执行单元。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from Module.task_system.schema import (
    DelegationContext,
    ExecutionState,
    OrchestratorResult,
    OrchestratorRunStatus,
    TaskCheckpoint,
    TaskErrorCode,
    TaskEventType,
    TaskState,
    TaskStatus,
    ValidationIssue,
    ValidationIssueType,
    now_iso,
)

from Module.task_system.core.sub_agent import (
    planning,
    execution_loop,
    result_handlers,
    replan,
)

if TYPE_CHECKING:
    from Module.task_system.artifact_store import ArtifactStore
    from Module.task_system.checkpoint import CheckpointManager
    from Module.task_system.core.evaluator import Evaluator
    from Module.task_system.core.executor import Executor
    from Module.task_system.core.graph import PlanGraph
    from Module.task_system.core.orchestrator import Orchestrator
    from Module.task_system.core.planner import Planner, Replanner
    from Module.task_system.core.plan_validator import PlanValidator
    from Module.task_system.infra import AgentInfra
    from Module.task_system.mailbox import TaskMailbox
    from Module.task_system.protocol import HITLNotifier
    from Module.task_system.token_ledger import TokenLedger

logger = logging.getLogger(__name__)


class SubAgent:
    """..."""

    def __init__(self, infra, hitl_notifier): ...

    async def run(self, context, mailbox):
        # ── 创建 per-task 组件（同原代码）──
        ...
        graph = await self._plan_and_validate(context, task_state, mailbox)
        if graph is None:
            return
        self._graph = graph
        await self._execute(task_state, graph, mailbox, context)

    async def resume_from_pause(self):
        # ── 同原逻辑，_execute 改为薄封装 ──
        ...
        await self._execute(ts, self._graph, mailbox, self._context, resume_from=checkpoint.exec)

    # ── _dispatch_result 保留在类中 ──────────────────────────────────
    async def _dispatch_result(self, result, task_state, graph, mailbox, context):
        if result.status == OrchestratorRunStatus.SUCCESS:
            await self._handle_success(result, task_state)
            return False, None
        elif result.status == OrchestratorRunStatus.NEEDS_REPLAN:
            return await self._handle_replan(result, task_state, graph, mailbox, context)
        elif result.status == OrchestratorRunStatus.BLOCKED:
            await self._handle_blocked(result, task_state, mailbox)
            return False, None
        # ... 其余分支同原代码

    # ── 薄封装 ────────────────────────────────────────────────────────
    async def _plan_and_validate(self, context, task_state, mailbox):
        return await planning.plan_and_validate(
            self._planner, self._plan_validator, context, task_state, mailbox,
            self._infra.constraints, self._push_mailbox, self._abort_error_code,
        )

    async def _execute(self, task_state, graph, mailbox, context, resume_from=None):
        return await execution_loop.execute_loop(
            self._orchestrator, task_state, graph, mailbox, context, resume_from,
            self._dispatch_result, self._push_mailbox,
        )

    async def _handle_success(self, result, task_state):
        return await result_handlers.handle_success(result, task_state)

    async def _handle_failed(self, result, task_state, mailbox):
        return await result_handlers.handle_failed(result, task_state, mailbox, self._push_mailbox)

    # ... 其余 handler 薄封装同理

    async def _handle_replan(self, result, task_state, graph, mailbox, context):
        return await replan.handle_replan(
            result, task_state, graph, mailbox, context,
            self._replanner, self._plan_validator, self._orchestrator,
            self._checkpoint_mgr, self._infra.constraints, self._push_mailbox,
        )

    def _check_meltdown(self, task_state):
        assert task_state.plan is not None
        return replan.check_meltdown(
            task_state.plan.plan_version, self._infra.constraints.max_failure_count,
        )

    def _extract_node_results(self, checkpoint):
        return replan.extract_node_results(checkpoint)

    # ── static helpers ────────────────────────────────────────────────
    @staticmethod
    async def _push_mailbox(mailbox, event_type, data):
        from Module.task_system.mailbox import push_mailbox_event
        push_mailbox_event(mailbox, event_type, data, caller="SubAgent")

    @staticmethod
    def _abort_error_code(issues):
        ...
```

- [ ] **Step 7: 创建 `__init__.py`**

```python
# Module/task_system/core/sub_agent/__init__.py
"""SubAgent 包——任务级执行单元。"""
from .sub_agent import SubAgent

__all__ = ["SubAgent"]
```

- [ ] **Step 8: 验证导入 + 运行测试**

```bash
python -c "from Module.task_system.core.sub_agent import SubAgent; print('import OK')"
python -m pytest Module/task_system/tests/unit/test_sub_agent.py -v
```

预期: 全部 PASS

- [ ] **Step 9: 全量 TaskSystem 测试**

```bash
python -m pytest Module/task_system/tests/ -v -q
```

预期: 334 passed

- [ ] **Step 10: 提交**

```bash
git add Module/task_system/core/sub_agent/
git commit -m "refactor: 拆分 sub_agent.py 为 sub_agent/ 包 (planning + execution_loop + result_handlers + replan)"
```
