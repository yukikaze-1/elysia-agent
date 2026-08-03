# Schema 拆分设计

> 日期: 2026-06-18  
> 范围: `Module/task_system/schema.py` (1287行) 拆分为 `schema/` 目录下的 5 个文件

## 目标

将 `schema.py` 中按文档标注的三层类型，分别拆分到独立文件，`__init__.py` 全部 re-export 保持向后兼容。

## 当前问题

`schema.py` 1287 行，包含 50+ Pydantic 模型、12 个枚举、6 个 NewType 别名、4 个工具函数。文件本身已用注释标注了三层分界，但所有类型仍在一个文件中。

## 目标结构

```
Module/task_system/schema/
├── __init__.py      # 全部 re-export，保持向后兼容
├── types.py         # 工具函数 + NewType 别名 + 基础枚举（~246行）
├── protocol.py      # 第一层: 跨层协议 CognitiveLoop ↔ TaskSystem（~279行）
├── output.py        # 第二层: 事件 + 查询视图（~228行）
└── internal.py      # 第三层: 编排数据模型（~529行）
```

原 `Module/task_system/schema.py` 删除。

### 各文件详情

#### `types.py` — 基础类型，零 schema 内部依赖

从 `schema.py` L1-246 搬入：

- **工具函数**: `now_iso()`, `_normalize_utc()`, `parse_iso()`
- **Pydantic 注解**: `IsoTimestamp`
- **NewType 别名**: `TaskId`, `AgentId`, `ApprovalId`, `SessionId`, `NodeId`, `ArtifactId`
- **基础枚举**: `TaskStatus`, `SubmitRejectionCode`, `TaskErrorCode`, `ResumeErrorCode`, `NodeStatus`, `NodeErrorCode`, `RBACLevel`

注意: `NodeErrorCode` (schema.py L1180) 从原本的 internal 区域移入 types.py——它是 StrEnum，无内部依赖，与 `TaskErrorCode`/`ResumeErrorCode` 同类。此举同时切断了 output.py ↔ internal.py 之间的循环依赖（`DAGNodeInfo` 在 output.py 引用 `NodeErrorCode`，`TaskState` 在 internal.py 引用 `TokenComponent`）。

**内部符号**（不 re-export）: `now_iso()`, `_normalize_utc()`, `parse_iso()`, `IsoTimestamp`——这些是 Pydantic 模型内部用的工具函数/注解类型，`__init__.py` 不导出。

依赖: `datetime`, `enum.StrEnum`, `typing`, `pydantic` — 无 schema 内部依赖。

#### `protocol.py` — 第一层: 跨层协议

从 `schema.py` L248-527 搬入：

- **约束**: `ConstraintConfig`, `TaskConstraints`
- **CognitiveLoop 侧类型**: `UserProfile`, `Tool`
- **委派上下文**: `ReturnMode`, `DelegationContext`, `DelegationStatus`, `DelegationResult`
- **提交结果**: `SubmitError`, `ResumeError`
- **HITL**: `HITLData`

依赖: `types.py`（`TaskId`, `AgentId`, `ApprovalId`, `NodeId`, `SessionId`, `TaskStatus`, `SubmitRejectionCode`, `TaskErrorCode`, `ResumeErrorCode`, `NodeStatus`, `RBACLevel`, `IsoTimestamp`, `now_iso`）

#### `output.py` — 第二层: 核心输出

从 `schema.py` L529-757 搬入：

- **事件**: `TaskEventType`, `TaskEvent`
- **查询视图**: `TaskSummary`, `TaskProgress`, `DAGNodeInfo`, `TaskDAG`
- **Agent 池**: `AgentType`, `AgentStatus`, `AgentInfo`, `AgentList`
- **Token**: `TokenComponentType`, `TokenPeriod`, `TokenComponent`, `TokenUsage`, `TokenStats`

依赖: `types.py`（`TaskId`, `AgentId`, `NodeId`, `TaskStatus`, `IsoTimestamp`, `now_iso`, `NodeErrorCode`, `NodeStatus`）

#### `internal.py` — 第三层: 编排数据模型

从 `schema.py` L759-1288 搬入：

- **Orchestrator 状态**: `OrchestratorRunStatus`, `OrchestratorResult`, `NodeState`, `TaskState`, `ExecutionPhase`, `ExecutionState`
- **Checkpoint**: `TaskCheckpoint`
- **产出契约**: `OutputContract`
- **DAG**: `ExecutionMode`, `DAGNode`, `DAG`, `Plan`, `DAGPatchType`, `DAGPatch`
- **校验**: `ValidationIssueType`, `ValidationIssue`, `ValidationAction`, `ValidationResult`
- **产物**: `Artifact`, `DBTNodeType`, `DBTNode`
- **评估**: `ExecutionStatus`, `BusinessStatus`, `EvaluatorAction`, `NodeResult`, `NodeResultEvaluation`, `TaskResult`
- **经验**: `ExperienceRecord`

依赖: `types.py`（全部基础类型 + 枚举），`protocol.py`（`Tool`, `SessionId`），`output.py`（`TaskEventType`, `TokenComponentType`, `TokenComponent`）

**公有/内部边界说明**: `internal.py` 中的类型并非都是内部实现细节。以下类型通过 `TaskEvent.data` 和查询 API 暴露给外部消费者，是稳定公共契约的一部分，不应在无弃用期的情况下移除：
- `TaskResult`, `NodeResultEvaluation`, `NodeResult`
- `ExecutionStatus`, `BusinessStatus`, `EvaluatorAction`
- `DAGNode`, `Plan`, `DAGPatch`
- `OrchestratorResult`

真正的内部类型（仅 Orchestrator/SubAgent 内部使用，不暴露给 Agent 层）:
- `NodeState`, `ExecutionState`, `ExecutionPhase`
- `TaskCheckpoint`
- `DBTNode`, `DBTNodeType`（实验性）

#### `__init__.py` — 向后兼容 re-export

所有 `from Module.task_system.schema import X` 调用者无需修改 import 路径。从 4 个子模块导入并 re-export 所有公开符号。

保持与当前 `Module/task_system/__init__.py` 一致的公开接口。

### 不受影响

- 所有 `schema.py` 的消费者——import 路径不变（`from Module.task_system.schema import ...`）
- 运行时行为完全不变——纯文件搬移
- `TaskSystem`、`SubAgent`、`Planner` 等所有调用方——零改动

## 验证

```bash
# 导入验证
python -c "from Module.task_system.schema import Plan, DAGNode, TaskEvent, DelegationContext, TaskState; print('OK')"

# 全量 TaskSystem 测试
python -m pytest Module/task_system/tests/ -v
```
