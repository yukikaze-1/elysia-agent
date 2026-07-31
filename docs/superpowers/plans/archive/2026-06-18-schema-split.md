# Schema 拆分实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `Module/task_system/schema.py` (1287行) 按三层分界拆为 `schema/` 包下 5 个文件，`__init__.py` 全部 re-export 保持向后兼容。

**Architecture:** 原 `schema.py` 删除 → 新建 `schema/` 包（`types.py` + `protocol.py` + `output.py` + `internal.py` + `__init__.py`）。纯搬移——不改逻辑、不改 import 路径。唯一结构变化：`NodeErrorCode` 从 internal 区域移入 `types.py` 以切断 output ↔ internal 循环依赖。

**Tech Stack:** Python 3.13, Pydantic, 无新依赖

## Global Constraints

- 不改变任何运行时行为——纯代码搬移
- 所有消费者 import 路径不变（`from Module.task_system.schema import X`）
- `NodeErrorCode` 从 L1180 移入 `types.py`（切断循环依赖）
- `now_iso()`, `_normalize_utc()`, `parse_iso()`, `IsoTimestamp` 不对外导出
- `__init__.py` 保持与当前 `Module/task_system/__init__.py` 一致的公开接口

---

### Task 1: 删除旧 `schema.py` + 创建 `schema/` 包

**Files:**
- Delete: `Module/task_system/schema.py`
- Create: `Module/task_system/schema/__init__.py`
- Create: `Module/task_system/schema/types.py`
- Create: `Module/task_system/schema/protocol.py`
- Create: `Module/task_system/schema/output.py`
- Create: `Module/task_system/schema/internal.py`

**Interfaces:**
- Produces: 与旧 `schema.py` 完全相同的 import 接口——所有符号通过 `__init__.py` 从同一路径 export

#### 搬移规则

原 `schema.py` 按行号拆分为 4 个子模块：

| 子模块 | 源行号 | 行数 |
|--------|--------|------|
| `types.py` | L1-110 (工具+NewType) + L112-246 (枚举) + **L1180-1193** (`NodeErrorCode`) | ~270 |
| `protocol.py` | L248-527 | ~280 |
| `output.py` | L529-757 | ~229 |
| `internal.py` | L759-1288（不含 L1180-1193 的 `NodeErrorCode`） | ~515 |

**`NodeErrorCode` 特殊处理**：原本在 schema.py L1180-1193（internal 区域），移入 `types.py`。它在 `types.py` 中的位置——排在 `NodeStatus` 之后、`RBACLevel` 之前——与其它节点级枚举自然聚集。

- [ ] **Step 1: 删除旧文件 + 创建目录**

```bash
mkdir -p Module/task_system/schema
git rm Module/task_system/schema.py
```

- [ ] **Step 2: 创建 `types.py`**

内容 = 原 schema.py 的三段拼接：
1. L1-66: docstring + imports + `now_iso()` + `_normalize_utc()` + `parse_iso()` + `IsoTimestamp`
2. L69-110: NewType 别名（`TaskId`, `AgentId`, `ApprovalId`, `SessionId`, `NodeId`, `ArtifactId`）
3. L112-246: 基础枚举（`TaskStatus`, `SubmitRejectionCode`, `TaskErrorCode`, `ResumeErrorCode`, `NodeStatus`） + **`NodeErrorCode`**（L1180-1193）+ `RBACLevel`

文件结构：
```python
# Module/task_system/schema/types.py
"""TaskSystem 基础类型——工具函数、NewType 别名、枚举。

所有其他 schema 子模块的底层依赖。零 schema 内部依赖。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal, NewType

from pydantic import BaseModel, BeforeValidator, Field


# ── 时间戳工具 ────────────────────────────────────────────────────────

def now_iso() -> str: ...
# [L20-26 原样复制]

def _normalize_utc(v: object) -> str: ...
# [L29-47 原样复制]

def parse_iso(ts: str) -> datetime: ...
# [L50-61 原样复制]

IsoTimestamp = Annotated[str, BeforeValidator(_normalize_utc)]
# [L66 原样复制]


# ── NewType 别名 ──────────────────────────────────────────────────────

TaskId = NewType("TaskId", str)        # [L71 原样复制]
AgentId = NewType("AgentId", str)      # [L78 原样复制]
ApprovalId = NewType("ApprovalId", str)  # [L84 原样复制]
SessionId = NewType("SessionId", str)  # [L90 原样复制]
NodeId = NewType("NodeId", str)        # [L97 原样复制]
ArtifactId = NewType("ArtifactId", str)  # [L104 原样复制]


# ── 基础枚举 ──────────────────────────────────────────────────────────

class TaskStatus(StrEnum): ...          # [L116-148 原样复制]
class SubmitRejectionCode(StrEnum): ...  # [L151-162 原样复制]
class TaskErrorCode(StrEnum): ...       # [L165-188 原样复制]
class ResumeErrorCode(StrEnum): ...     # [L191-201 原样复制]
class NodeStatus(StrEnum): ...          # [L204-233 原样复制]
class NodeErrorCode(StrEnum): ...       # [L1180-1193 原样复制] ★ 从 internal 区域移入
class RBACLevel(StrEnum): ...           # [L236-245 原样复制]
```

**关键**：从原 `schema.py` 复制时，保持每个代码块完全不变（包括 docstring、注释、字段默认值）。只改文件顶部的 module docstring。

- [ ] **Step 3: 创建 `protocol.py`**

内容 = 原 schema.py L248-527 完整复制。修改顶部 imports：

```python
# Module/task_system/schema/protocol.py
"""第一层：跨层协议 —— CognitiveLoop ↔ TaskSystem 输入/输出契约。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .types import (
    AgentId,
    ApprovalId,
    IsoTimestamp,
    NodeId,
    RBACLevel,
    SessionId,
    SubmitRejectionCode,
    TaskErrorCode,
    ResumeErrorCode,
    NodeErrorCode,
    TaskStatus,
    now_iso,
)

# ── [L252-527 内容原样复制] ──
# ConstraintConfig, TaskConstraints, UserProfile, Tool,
# ReturnMode, DelegationContext, DelegationStatus, DelegationResult,
# SubmitError, ResumeError, HITLData
```

- [ ] **Step 4: 创建 `output.py`**

内容 = 原 schema.py L529-757 完整复制。修改顶部 imports：

```python
# Module/task_system/schema/output.py
"""第二层：核心输出 —— TaskMailbox 事件 + 查询视图。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .types import (
    AgentId,
    IsoTimestamp,
    NodeErrorCode,
    NodeId,
    NodeStatus,
    TaskId,
    TaskStatus,
    now_iso,
)

# ── [L529-757 内容原样复制] ──
# TaskEventType, TaskEvent, TaskSummary, TaskProgress,
# DAGNodeInfo, TaskDAG, AgentType, AgentStatus, AgentInfo,
# AgentList, TokenComponentType, TokenPeriod, TokenComponent,
# TokenUsage, TokenStats
```

- [ ] **Step 5: 创建 `internal.py`**

内容 = 原 schema.py L759-1288 完整复制，**但移除 L1180-1193 的 `NodeErrorCode`**（已移入 `types.py`）。修改顶部 imports：

```python
# Module/task_system/schema/internal.py
"""第三层：内部编排 —— Planner / Executor / Evaluator 数据模型。

注意：部分类型（TaskResult, NodeResultEvaluation, ExecutionStatus 等）
通过 TaskEvent.data 和查询 API 暴露给外部消费者，是稳定公共契约的一部分。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .types import (
    ArtifactId,
    IsoTimestamp,
    NodeErrorCode,   # ★ 从 types.py 导入（原在 internal 区域）
    NodeId,
    NodeStatus,
    RBACLevel,
    SessionId,
    TaskErrorCode,
    TaskId,
    TaskStatus,
    now_iso,
)
from .protocol import Tool
from .output import TaskEventType, TokenComponentType, TokenComponent

# ── [L759-1288 内容原样复制，跳过 L1180-1193 NodeErrorCode] ──
# OrchestratorRunStatus, OrchestratorResult, NodeState, TaskState,
# ExecutionPhase, ExecutionState, TaskCheckpoint, OutputContract,
# ExecutionMode, DAGNode, DAG, Plan, DAGPatchType, DAGPatch,
# ValidationIssueType, ValidationIssue, ValidationAction, ValidationResult,
# Artifact, DBTNodeType, DBTNode, ExecutionStatus, BusinessStatus,
# EvaluatorAction, NodeResult, NodeResultEvaluation, TaskResult,
# ExperienceRecord
```

**注意**：`internal.py` 中 `NodeState` 等引用 `NodeErrorCode` 的地方保持不变——`NodeErrorCode` 现在从 `.types` 导入。

- [ ] **Step 6: 创建 `__init__.py`**

```python
# Module/task_system/schema/__init__.py
"""TaskSystem 核心数据结构。

所有类型通过此模块统一导出，保持向后兼容。
现有 `from Module.task_system.schema import X` 无需修改。
"""

# ── types.py: 基础类型 ──
from .types import (
    # NewType 别名
    AgentId,
    ApprovalId,
    ArtifactId,
    NodeId,
    SessionId,
    TaskId,
    # 枚举
    NodeErrorCode,
    NodeStatus,
    RBACLevel,
    ResumeErrorCode,
    SubmitRejectionCode,
    TaskErrorCode,
    TaskStatus,
)

# ── protocol.py: 跨层协议 ──
from .protocol import (
    ConstraintConfig,
    DelegationContext,
    DelegationResult,
    DelegationStatus,
    HITLData,
    ReturnMode,
    ResumeError,
    SubmitError,
    TaskConstraints,
    Tool,
    UserProfile,
)

# ── output.py: 事件 + 查询视图 ──
from .output import (
    AgentInfo,
    AgentList,
    AgentStatus,
    AgentType,
    DAGNodeInfo,
    TaskDAG,
    TaskEvent,
    TaskEventType,
    TaskProgress,
    TaskSummary,
    TokenComponent,
    TokenComponentType,
    TokenPeriod,
    TokenStats,
    TokenUsage,
)

# ── internal.py: 编排数据模型 ──
from .internal import (
    Artifact,
    BusinessStatus,
    DAG,
    DAGNode,
    DAGPatch,
    DAGPatchType,
    DBTNode,
    DBTNodeType,
    EvaluatorAction,
    ExecutionMode,
    ExecutionPhase,
    ExecutionState,
    ExecutionStatus,
    ExperienceRecord,
    NodeResult,
    NodeResultEvaluation,
    NodeState,
    OrchestratorResult,
    OrchestratorRunStatus,
    OutputContract,
    Plan,
    TaskCheckpoint,
    TaskResult,
    TaskState,
    ValidationAction,
    ValidationIssue,
    ValidationIssueType,
    ValidationResult,
)

# 内部工具不导出: now_iso, _normalize_utc, parse_iso, IsoTimestamp
```

**关键**：export 列表必须与当前 `Module/task_system/__init__.py` 中从 `schema` 导入的符号完全对齐。

- [ ] **Step 7: 更新 `Module/task_system/__init__.py`**

当前 `__init__.py` 的 import 路径无需修改——`from Module.task_system.schema import (...)` 仍然有效，因为 `schema/__init__.py` re-export 了所有符号。

验证 `Module/task_system/__init__.py` 从 `schema` 导入的符号与 `schema/__init__.py` 导出的符号匹配：

```bash
# 在 schema/__init__.py 中 grep from . 导出，与 Module/task_system/__init__.py 的 import 比较
diff <(grep "^from \." Module/task_system/schema/__init__.py | sort) /dev/null
```

- [ ] **Step 8: 验证导入**

```bash
python -c "
from Module.task_system.schema import (
    Plan, DAGNode, TaskEvent, DelegationContext, TaskState,
    TaskResult, NodeResultEvaluation, NodeErrorCode,
    OrchestratorResult, ExecutionStatus, BusinessStatus,
    IsoTimestamp, now_iso
)
print('OK')
"
```

预期: `OK`

- [ ] **Step 9: 全量测试**

```bash
python -m pytest Module/task_system/tests/ -v -q
```

预期: 334 passed (与拆分前一致)

- [ ] **Step 10: 提交**

```bash
git add Module/task_system/schema/
git commit -m "refactor: 拆分 schema.py 为 schema/ 包 (types + protocol + output + internal)"
```

---

## 验证

```bash
# 1. 所有公开符号可从同一路径导入
python -c "from Module.task_system.schema import Plan, TaskEvent, DelegationContext; print('OK')"

# 2. 全量测试无回归
python -m pytest Module/task_system/tests/ -v

# 3. 核心测试无回归
python -m pytest core/tests/ -v
```
