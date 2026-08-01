# WorkflowEngine 分离设计（Module/task_system → Module/workflow_engine）

> 日期：2026-08-01
> 状态：设计评审中
> 目标：把 `Module/task_system` 与 core 应用层分离干净，各自独立迭代，通过独立契约层（`Module/workflow_contract`）通信。

## 1. 背景与动机

`Module/task_system` 是项目里最大的单个模块（顶层 3052 行 + core/ + schema/ + safety/ + learning/ + docs/ + tests/），但目前存在三个问题阻碍它独立迭代：

1. **它是「杂物抽屉」**：`docs/migration.md` 自己列出了 **7 项不属于它的组件**（UserProfile / Tool / SessionId / AgentInfra / DelegationPreparer / EventBusHITLNotifier / RBACLevel），且都是已识别的待迁移项。
2. **它是「伪中间层」持有者**：`Module/task_system/schema` 被 10 个外部文件直接 import（`from Module.task_system.schema import UserProfile/Tool/SubmitError/...`）。名义上是内部模块，实际承担跨边契约职责。
3. **测试被墙外**：`make test-quick` 用 `--ignore=Module/task_system/tests` 把它排除，日常循环碰不到它的测试。

### 与 core/task (TaskScheduler) 的关系

两者**互补，不重复**，是「预留 → 实现」的演进关系：

| | `core/task/` TaskScheduler | `Module/task_system/` TaskSystem |
|---|---|---|
| 海拔 | 单轮内并行执行 ReAct 工具调用 | 跨轮后台长任务 DAG 编排 |
| 时长 | 秒级（一轮对话内，同步收结果）| 分钟级（后台跑，mailbox 异步回传）|
| 消费者 | 仅 `core/agent/agent.py` | 10 个生产文件 |
| 现态 | 留在 core，不参与本次分离 | 本次分离目标 |

`core/task` 的 `TaskType.BACKGROUND = "脱离对话流的长时后台任务 (预留)"`——task_system 正是这个「预留」长出来的正式形态。**`core/task` 不迁移、不改名**（其 `TaskScheduler` 类名保留）。

## 2. 目标状态（三区架构）

```
core/ 世界                         契约层（独立包）                   Module/workflow_engine/ 世界
─────────────────              ┌─────────────────┐              ┌──────────────────────────┐
│ Agent / CognitiveLoop        │ workflow_contract│              │ WorkflowEngine            │
│ core/task (TaskScheduler)    │  跨边协议+类型    │              │  Orchestrator/Executor/   │
│ core/schema (消息/part)      │  · Delegation... │ ◀──契约──▶   │   Planner/SubAgent/       │
│ core.tools.* (插件)          │  · SubmitError   │              │   Replanner               │
│ core/cognitive/delegation.py │  · HITLNotifier  │              │  safety/checkpoint/       │
│ core/hitl.py (EventBus...)   │  · Constraint    │              │  WorkflowMailbox/         │
│                              │  · Workflow* 类型 │              │  WorkflowDebugAPI        │
│                              │  · AgentInfra    │              │  schema/（内部类型）       │
└─────────────────              └─────────────────┘              │  Module.tool              │
                                                                 └──────────────────────────┘
        只依赖契约层 + 少量单点注入                    两边只依赖契约层，互不 import core 内部
```

**核心不变式**：
- `Module/workflow_engine` **不 import 任何 `core.*`**（除 stdlib + Module.tool + workflow_contract）。
- `core/` 通过契约层类型 + 工厂/协议获取 workflow_engine 能力，不直接 import workflow_engine 内部实现（阶段 4 前允许保留 `from Module.workflow_engine import WorkflowEngine` 单点注入）。
- 契约层是唯一跨边类型来源，**零业务逻辑**（纯类型 + 协议 ABC + 无状态运行时容器）。

## 3. 已定决策

| # | 决策 |
|---|------|
| 1 | 契约层 = 独立包 `Module/workflow_contract/`（放 Module/ 下，与 workflow_engine 同级）|
| 2 | `Module.tool`（protocol + manager）算 workflow_engine 世界 |
| 3 | `Module/task_system/` → `Module/workflow_engine/` |
| 4 | 主类 `TaskSystem` → `WorkflowEngine` |
| 5 | 模块内 **全量** `Task*` 前缀标识符 → `Workflow*` |
| 6 | `core/task` 的 `TaskScheduler`、`core/schema/event` 的 `TaskResultData` **不参与改名**（不属于本模块）|

## 4. 改名盘点（Task* → Workflow* 全量）

### 4.1 模块内标识符（16 个）

| 现名 | 新名 |
|------|------|
| TaskSystem | WorkflowEngine |
| TaskMailbox | WorkflowMailbox |
| TaskId | WorkflowId |
| TaskStatus | WorkflowStatus |
| TaskEvent | WorkflowEvent |
| TaskEventType | WorkflowEventType |
| TaskState | WorkflowState |
| TaskErrorCode | WorkflowErrorCode |
| TaskCheckpoint | WorkflowCheckpoint |
| TaskConstraints | WorkflowConstraints |
| TaskSummary | WorkflowSummary |
| TaskDAG | WorkflowDAG |
| TaskDebugAPI | WorkflowDebugAPI |
| TaskProgress | WorkflowProgress |
| TaskResult | WorkflowResult |
| TaskSummaryStore | WorkflowSummaryStore |

非 `Task*` 前缀但属于模块的标识符（`DelegationContext` 等）**保留原名**，仅迁移/重新归属。

### 4.2 包与文件

| 现路径 | 新路径 |
|--------|--------|
| `Module/task_system/` | `Module/workflow_engine/` |
| `Module/task_system/task_system.py` | `Module/workflow_engine/workflow_engine.py` |
| `Module/task_system/tests/` | `Module/workflow_engine/tests/` |
| import `Module.task_system` | import `Module.workflow_engine` |

### 4.3 排除项（明确不 rename）

- `TaskScheduler` — `core/task/scheduler.py` 的类，core 侧单轮调度器。
- `TaskResultData` — `core/schema/event.py` 的事件类型，agent 事件总线用。
- `TaskType` / `TaskStatus` / `Task`（core/task/task.py）— core 侧，保留。

### 4.4 消费者 import 更新（10 个生产 + 2 个测试文件）

`core/cognitive/loop.py`、`core/cognitive/tools.py`、`core/agent/agent.py`、`core/agent/_types.py`、`core/agent/lifecycle.py`、`core/agent/server.py`、`core/agent/_lifecycle_task_system.py`、`core/input/server.py`、`core/input/debug_routes/__init__.py`、`core/tools/runtime_config/plugin.py`、`core/agent/tests/test_types.py`、`core/tests/test_phase3.py`。

## 5. 契约层内容（Module/workflow_contract/）

### 5.1 从 schema 迁入的跨边类型

| 类型 | 说明 |
|------|------|
| `DelegationContext` | 委派上下文（LLM 筛选+填充后的工具调用方案）|
| `DelegationResult` / `DelegationStatus` | 委派结果三态 |
| `SubmitError` | 提交错误 |
| `HITLData` | 人审数据 |
| `HITLNotifier` | 协议 ABC（跨边通信接口）|
| `ConstraintConfig` | 全局天花板约束 |
| `TaskId` → `WorkflowId` | 作业标识 |
| `TaskStatus` → `WorkflowStatus` | 作业状态枚举 |
| `TaskEvent` / `TaskEventType` → `WorkflowEvent` / `WorkflowEventType` | 作业事件 |
| `ApprovalId` | 审批标识 |
| `AgentInfra` | 跨边运行时注入容器（见 §6.3）|

> **跨边但留 workflow_engine 的运行时对象**：`WorkflowMailbox`（core/agent 构造）与 `WorkflowDebugAPI`（core/input 接线）属跨边运行时对象，但含实现逻辑，不入契约层。按 §10 的「单点注入」策略，core 侧在构造点直接 import 它们，阶段 4 前允许。

### 5.2 契约层约束

- **零业务逻辑**：只放类型定义、枚举、协议 ABC、无状态容器。
- 不 import `core.*`，不 import workflow_engine 内部（避免循环）。
- `Module.tool` 的轻量 `Tool` 投影：若契约层需要工具描述，用 `Module.tool` 的已有类型（§6.2 迁移后），不在契约层重复定义。

### 5.3 契约层消费者

- core 侧：cognitive loop/tools、agent lifecycle、input server/debug_routes。
- workflow_engine 侧：task_system 本体。

## 6. 剥皮清单（migration.md 7 项归位）

| 项 | 现位置 | 归位 | 说明 |
|----|--------|------|------|
| UserProfile | `schema/protocol.py` | **core 侧** | CognitiveLoop 拥有，契约层只引用 |
| Tool | `schema/protocol.py` | **Module/tool** | 工具描述由 ToolRegistry 定义 |
| SessionId | `schema/types.py` | **core 侧** | CognitiveLoop 生成 |
| RBACLevel | `schema/types.py` | **Module/tool** | 权限是工具属性 |
| DelegationPreparer | `delegation_preparer.py` | **core 侧**（`core/cognitive/delegation.py`）| 消费者全在 core（loop/tools/lifecycle）；迁出后自然消解 `core.schema.message/part` 反向依赖。与 workflow_engine 交互只走契约层类型（`DelegationContext` 等）|
| EventBusHITLNotifier | `protocol.py` | **core 侧**（core/hitl.py）| HITLNotifier 的 EventBus 实现；ABC 留契约层 |
| AgentInfra | `infra.py` | **契约层**（见下）| 跨边运行时注入容器 |

### 6.3 AgentInfra 归属论证

`AgentInfra` 是 core 向 workflow_engine 提供运行资源（AsyncOpenAI/Milvus/http/ToolRegistry/ConstraintConfig）的**注入容器**：
- core 侧 `_lifecycle_task_system.py` 构造并注入；
- workflow_engine 侧 Planner/Executor/Evaluator 只读消费；
- 含契约类型 `ConstraintConfig`。

→ 放**契约层**：它是「core 提供资源、engine 消费」的运行时契约。`tool_registry` 字段已用 `object` 类型规避循环导入，迁移后无硬依赖。`llm_model` 默认值 `TASK_SYSTEM_MODEL` env 保持。

## 7. 依赖反转

| 现依赖 | 反转方案 |
|--------|----------|
| `delegation_preparer.py: from core.schema.message import Message` | DelegationPreparer 迁 core 侧后自然消解 |
| `delegation_preparer.py: from core.schema.part import TextPart` | 同上 |
| `executor.py: from Module.tool.protocol import ToolContext, ToolResult` | **保留**（Module.tool 算 workflow_engine 世界）|

反转完成后，`Module/workflow_engine` 生产代码只依赖：stdlib + `Module.tool` + `Module.workflow_contract`。

## 8. 阶段划分（每阶段独立可交付、测试全绿）

| 阶段 | 内容 | 可交付物 | 验证 |
|------|------|----------|------|
| **1. 改名** | 目录/包/文件重命名 + 16 标识符 Task*→Workflow* + Makefile `--ignore` + 全量 import 更新 | 机械重构，行为零变化 | `make test-quick` 全绿 |
| **2. 契约层提取** | 新建 `Module/workflow_contract/`，跨边类型迁入，外部消费者改 import | 边界真正成形 | `make test-quick` + 新增契约层单测 |
| **3. 剥皮 + 依赖反转** | migration.md 7 项归位 + DelegationPreparer 迁 core + 消解 core.schema 反向依赖 | workflow_engine 成干净叶子 | `make test-quick` + `grep -r "core\." workflow_engine` 为空 |
| **4. 独立迭代基建** | `make test-workflow-engine`（去掉忽略墙）+ 独立 README + 独立 spec/plan 循环 | 独立迭代成立 | 独立测试命令全绿 |

## 9. 测试策略

- 每阶段后 `make test-quick` 全绿（阶段 1 后 test-quick 与现在一样不含 workflow_engine tests）。
- 阶段 2 新增 `Module/workflow_contract/tests/`（契约类型序列化/校验单测，marker=unit）。
- 阶段 3 后验证：`grep -rn "from core\." Module/workflow_engine --include="*.py"` 输出为空（排除 tests 的测试辅助）。
- 阶段 4 提供 `make test-workflow-engine`：`pytest Module/workflow_engine/tests -m "not slow" -q --tb=short`。

## 10. 风险与边界

- **全量改名 diff 巨大**：~16 标识符 × 模块内 1000+ 处引用 + 10 消费者 + 文档。阶段 1 严格机械（`sed`/重命名 + import 更新），不允许顺手改行为；行为回归由 test-quick + 阶段 1 后 test-normal 兜底。
- **`TaskId` 双归属**：契约层 `WorkflowId` 与 core 侧未来 `SessionId` 等并存，命名已错开，无撞名。
- **`__init__.py` 大导出面**：改名后 `__all__` 全部更新；外部只用到子集，但公开 API 完整性由模块内测试兜底。
- **DelegationPreparer 迁 core 的边界**：它调 LLM 做筛选/填充，迁 core 后仍通过契约层类型与 workflow_engine 交互（`DelegationContext` 等），不产生新跨边。
- **阶段 4 前允许单点注入**：`from Module.workflow_engine import WorkflowEngine` 在 core 保留一处（构造点），工厂抽象留待独立迭代时演进，不在本 spec 范围内强制。

## 11. 相关文档

| 文档 | 内容 |
|------|------|
| [本设计](../../../docs/superpowers/specs/2026-08-01-workflow-engine-separation-design.md) | 分离设计 |
| [TaskSystem migration 清单](../../../Module/task_system/docs/migration.md) | 7 项待迁移组件（本 spec §6 的源）|
| [TaskSystem README](../../../Module/task_system/README.md) | 现架构 |
| [CLAUDE.md](../../../CLAUDE.md) | 项目约定（README 中文、代码注释中文、模块注册）|
