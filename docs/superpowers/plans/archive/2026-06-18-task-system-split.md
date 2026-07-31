# TaskSystem 拆分实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `Module/task_system/task_system.py` (843行) 的查询和任务操作方法提取为 `query.py` / `lifecycle.py` 模块级函数，TaskSystem 方法变为薄封装委托。

**Architecture:** 创建 `query.py` 和 `lifecycle.py`，将 TaskSystem 中的 11 个查询方法和 3 个任务操作方法提取为接受显式参数的模块级函数。`TaskSystem` 方法变成一行委托。公开 API 签名不变，消费者零改动。

**Tech Stack:** Python 3.13, asyncio

## Global Constraints

- 所有消费者 import 路径不变——`TaskSystem` 公开方法签名一个不改
- 运行时行为完全不变——纯代码搬移 + `self._xxx` → 参数
- `cancel()` PAUSED 分支的 semaphore 释放 + cleanup 由 `lifecycle.cancel()` 内部完成，调用方不得重复
- `submit_approval` 接受 `tool_registry` 参数（不访问 `tasks`）

---

### Task 1: 创建 `query.py` + 更新 `task_system.py` 查询方法为薄封装

**Files:**
- Create: `Module/task_system/query.py`
- Modify: `Module/task_system/task_system.py`

**Interfaces:**
- Produces: 11 个模块级查询函数，每个接受显式参数

#### 提取规则

从 `task_system.py` 提取以下方法到 `query.py`，修改规则：
- `self._tasks` → `tasks` 参数
- `self._mailboxes` → `mailboxes` 参数
- `self._max_sub_agents` → `max_sub_agents` 参数
- 保留 `self._infra.xxx` 引用 → 改为 `infra` 参数（仅 `list_active_tasks` 用 `self._infra.constraints`）
- `self._cleanup_task` / `self._cleanup_storage` → 不传入（查询方法不调用清理）
- 移除 `self` 参数，改为 `@staticmethod` 模式（模块级普通函数）
- 所有类型 import 从 `task_system.py` 顶部复制到 `query.py`

- [ ] **Step 1: 创建 `query.py`** — 提取 11 个只读函数

文件结构和精确签名：

```python
# Module/task_system/query.py
"""TaskSystem 查询 API —— 只读访问函数。

所有函数接受显式参数替代 TaskSystem 内部状态，
供 TaskSystem 方法薄封装委托，也可被 Debug API 直接调用。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from Module.task_system.schema import (
    AgentInfo, AgentList, AgentStatus, AgentType,
    DAGNodeInfo, NodeId, NodeStatus,
    TaskDAG, TaskId, TaskProgress, TaskStatus, TaskSummary,
    TokenPeriod, TokenStats, TokenUsage,
    now_iso, parse_iso,
)

if TYPE_CHECKING:
    from Module.task_system.core.sub_agent import SubAgent
    from Module.task_system.mailbox import TaskMailbox

logger = logging.getLogger(__name__)


def get_task_status(
    tasks: dict[TaskId, SubAgent],
    task_id: TaskId,
) -> TaskStatus | None:
    """查询任务当前状态。"""
    # [原 task_system.py:492-504 方法体，self._tasks → tasks]
    ...


def get_task_mailbox(
    mailboxes: dict[TaskId, TaskMailbox],
    task_id: TaskId,
) -> TaskMailbox | None:
    """获取任务的 mailbox。"""
    # [原 task_system.py:506-511 方法体，self._mailboxes → mailboxes]
    ...


async def get_task_summary(
    tasks: dict[TaskId, SubAgent],
    task_id: TaskId,
) -> TaskSummary | None:
    """获取单个任务摘要。委托给 list_active_tasks 然后过滤。"""
    # [原 task_system.py:513-519 方法体，self.list_active_tasks() → list_active_tasks(tasks)]
    ...


async def list_active_tasks(
    tasks: dict[TaskId, SubAgent],
) -> list[TaskSummary]:
    """列出所有活跃任务。"""
    # [原 task_system.py:521-578 方法体，self._tasks → tasks]
    # 注意：原实现访问 self._infra.constraints —— 需改为参数或省略
    # 实际代码中未使用 _infra.constraints，直接复制即可
    ...


def get_progress(
    tasks: dict[TaskId, SubAgent],
    task_id: TaskId,
) -> TaskProgress | None:
    """查询任务执行进度。"""
    # [原 task_system.py:580-646 方法体，self._tasks → tasks]
    ...


async def get_task_dag(
    tasks: dict[TaskId, SubAgent],
    task_id: TaskId,
) -> TaskDAG | None:
    """获取任务的 DAG 可视化数据。"""
    # [原 task_system.py:648-663 方法体，self._tasks → tasks]
    ...


def get_token_usage(
    tasks: dict[TaskId, SubAgent],
    task_id: TaskId,
) -> TokenUsage | None:
    """获取任务 token 消耗。"""
    # [原 task_system.py:665-678 方法体，self._tasks → tasks]
    ...


def list_agents(
    tasks: dict[TaskId, SubAgent],
    max_sub_agents: int,
    has_slots: bool,
) -> AgentList:
    """列出 Agent 池状态。"""
    # [原 task_system.py:680-737 方法体]
    # self._tasks → tasks
    # self._max_sub_agents → max_sub_agents
    # self._semaphore is not None → has_slots
    ...


def get_token_stats(
    tasks: dict[TaskId, SubAgent],
    period: TokenPeriod = TokenPeriod.TODAY,
) -> TokenStats:
    """聚合 token 统计。"""
    # [原 task_system.py:739-786 方法体，self._tasks → tasks]
    ...


def get_debug_snapshot(
    tasks: dict[TaskId, SubAgent],
    mailboxes: dict[TaskId, TaskMailbox],
    max_sub_agents: int,
) -> dict:
    """供 TaskDebugAPI 内部轮询使用。"""
    # [原 task_system.py:251-277 方法体]
    # self._tasks → tasks
    # self._mailboxes[task_id].count() → mailboxes[task_id].count()
    # self._max_sub_agents → max_sub_agents
    ...


async def collect_tools_used(
    sub_agent: SubAgent,
    exec_state,  # ExecutionState | None
) -> list[str]:
    """从执行状态 + ArtifactStore 收集实际被调用的工具名。"""
    # [原 task_system.py:787-819 方法体，保持不变——已经不依赖 self]
    ...
```

**关键**：从 `task_system.py` 复制每个方法体时，只替换 `self._xxx` 为对应参数名，不改任何逻辑。所有 `from Module.task_system.core.sub_agent import SubAgent` 等 import 也复制到 `query.py`（用 `TYPE_CHECKING`）。

- [ ] **Step 2: 更新 `task_system.py`** — 查询方法改为薄封装

在 `task_system.py` 顶部添加 `from Module.task_system import query`，然后将每个查询方法替换为一行委托：

```python
# task_system.py 内:
from Module.task_system import query

class TaskSystem:
    # ... __init__, start, stop, submit, resume 保持不变 ...

    def _get_debug_snapshot(self) -> dict:
        return query.get_debug_snapshot(
            self._tasks, self._mailboxes, self._max_sub_agents,
        )

    def get_task_status(self, task_id: TaskId) -> TaskStatus | None:
        return query.get_task_status(self._tasks, task_id)

    def get_task_mailbox(self, task_id: TaskId) -> TaskMailbox | None:
        return query.get_task_mailbox(self._mailboxes, task_id)

    async def get_task_summary(self, task_id: TaskId) -> TaskSummary | None:
        return await query.get_task_summary(self._tasks, task_id)

    async def list_active_tasks(self) -> list[TaskSummary]:
        return await query.list_active_tasks(self._tasks)

    def get_progress(self, task_id: TaskId) -> TaskProgress | None:
        return query.get_progress(self._tasks, task_id)

    async def get_task_dag(self, task_id: TaskId) -> TaskDAG | None:
        return await query.get_task_dag(self._tasks, task_id)

    def get_token_usage(self, task_id: TaskId) -> TokenUsage | None:
        return query.get_token_usage(self._tasks, task_id)

    def list_agents(self) -> AgentList:
        return query.list_agents(
            self._tasks, self._max_sub_agents,
            self._semaphore is not None,
        )

    def get_token_stats(self, period: TokenPeriod = TokenPeriod.TODAY) -> TokenStats:
        return query.get_token_stats(self._tasks, period)

    # _collect_tools_used 改为委托:
    async def _collect_tools_used(self, sub_agent, exec_state) -> list[str]:
        return await query.collect_tools_used(sub_agent, exec_state)
```

- [ ] **Step 3: 运行测试验证**

```bash
python -m pytest Module/task_system/tests/unit/test_task_system.py -v
```

预期: 全部 PASS（25 tests）

- [ ] **Step 4: 提交**

```bash
git add Module/task_system/query.py Module/task_system/task_system.py
git commit -m "refactor: 提取 query.py — TaskSystem 查询方法变为模块函数 + 薄封装委托"
```

---

### Task 2: 创建 `lifecycle.py` + 更新 `task_system.py` 任务操作方法

**Files:**
- Create: `Module/task_system/lifecycle.py`
- Modify: `Module/task_system/task_system.py`

**Interfaces:**
- Produces: `pause()`, `cancel()`, `submit_approval()` 模块级函数

- [ ] **Step 1: 创建 `lifecycle.py`**

```python
# Module/task_system/lifecycle.py
"""TaskSystem 任务操作函数 —— pause / cancel / submit_approval。"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Awaitable, Callable

from Module.task_system.mailbox import push_mailbox_event
from Module.task_system.schema import (
    TaskEventType, TaskErrorCode, TaskId, TaskStatus, now_iso,
)

if TYPE_CHECKING:
    from Module.task_system.core.sub_agent import SubAgent
    from Module.task_system.mailbox import TaskMailbox

logger = logging.getLogger(__name__)


def pause(
    tasks: dict[TaskId, SubAgent],
    task_id: TaskId,
) -> None:
    """暂停任务——在下一个安全点停止执行并保存 checkpoint。幂等。"""
    # [原 task_system.py:279-306 方法体，self._tasks → tasks]
    ...


async def cancel(
    tasks: dict[TaskId, SubAgent],
    mailboxes: dict[TaskId, TaskMailbox],
    semaphore: asyncio.BoundedSemaphore | None,
    cleanup_task: Callable[[TaskId], None],
    cleanup_storage: Callable[[TaskId, SubAgent], Awaitable[None]],
    task_id: TaskId,
) -> bool:
    """软取消任务。幂等。

    PAUSED 分支在内部完成 cleanup_task() + cleanup_storage()
    + semaphore.release()。调用方不得再次释放。
    """
    # [原 task_system.py:399-455 方法体]
    # self._tasks → tasks
    # self._mailboxes → mailboxes
    # self._cleanup_task(...) → cleanup_task(...)
    # self._cleanup_storage(...) → await cleanup_storage(...)
    # self._semaphore.release() → semaphore.release()
    # assert self._semaphore is not None → assert semaphore is not None
    ...


def submit_approval(
    tool_registry,  # ToolManager 实例
    agent_id: str,
    approved: bool,
) -> bool:
    """审批结果回传。v1 桥接到 ToolManager 的 SafetyPipeline。"""
    # [原 task_system.py:457-484 方法体]
    # self._infra.tool_registry → tool_registry
    ...
```

**关键**：`cancel()` 的 PAUSED 分支（原 L420-440）调用 `cleanup_task(task_id)` → `await cleanup_storage(task_id, sub_agent)` → `semaphore.release()`。这些回调解耦了 TaskSystem 的内部清理逻辑。

- [ ] **Step 2: 更新 `task_system.py`** — 任务操作方法改为薄封装

在 `task_system.py` 顶部添加 `from Module.task_system import lifecycle`，替换方法：

```python
# task_system.py 内:
from Module.task_system import lifecycle

class TaskSystem:
    # ...

    async def pause(self, task_id: TaskId) -> None:
        lifecycle.pause(self._tasks, task_id)

    async def cancel(self, task_id: TaskId) -> bool:
        # PAUSED 分支的 semaphore 释放 + 清理已由 lifecycle.cancel() 内部完成
        return await lifecycle.cancel(
            self._tasks, self._mailboxes, self._semaphore,
            self._cleanup_task, self._cleanup_storage, task_id,
        )

    def submit_approval(
        self, task_id: TaskId, agent_id: AgentId,
        approved: bool, feedback: str | None = None,
    ) -> bool:
        # task_id/feedback 为 v2 预留，v1 不使用
        return lifecycle.submit_approval(
            self._infra.tool_registry, str(agent_id), approved,
        )
```

- [ ] **Step 3: 清理 `task_system.py` imports**

移除不再直接使用的 import（`AgentInfo`, `AgentList`, `AgentStatus`, `AgentType`, `DAGNodeInfo`, `NodeStatus`, `TaskDAG`, `TaskProgress`, `TokenPeriod`, `TokenStats`, `now_iso`, `parse_iso`, `push_mailbox_event`, `TaskEventType`, `TaskErrorCode` 等——这些现在由 `query.py` 和 `lifecycle.py` 各自导入）。

保留 `TaskSystem` 自身仍需要的 import：`DelegationContext`, `ResumeError`, `ResumeErrorCode`, `SubmitError`, `SubmitRejectionCode`, `TaskId`, `TaskStatus`, `TokenUsage`（`get_token_usage` 返回类型在薄封装中引用）, `TaskSummary`（`list_active_tasks` 返回类型）, `TaskMailbox`, `SubAgent`, `AgentId`。

- [ ] **Step 4: 运行全量测试**

```bash
python -m pytest Module/task_system/tests/ -v -q
```

预期: 334 passed

- [ ] **Step 5: 提交**

```bash
git add Module/task_system/lifecycle.py Module/task_system/task_system.py
git commit -m "refactor: 提取 lifecycle.py — pause/cancel/submit_approval 变为模块函数 + 薄封装委托"
```

---

## 验证

```bash
# 全量测试
python -m pytest Module/task_system/tests/ -v -q

# TaskSystem 单元测试
python -m pytest Module/task_system/tests/unit/test_task_system.py -v

# 导入验证
python -c "from Module.task_system.task_system import TaskSystem; print('OK')"
```
