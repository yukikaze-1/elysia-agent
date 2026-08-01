# WorkflowEngine 分离设计（Module/task_system → Module/workflow_engine）

> 日期：2026-08-01
> 状态：设计评审中
> 目标：把 `Module/task_system` 与 core 应用层分离干净，各自独立迭代，通过独立契约层（`Module/workflow_contract`）通信。

## 1. 背景与动机

`Module/task_system` 是项目里最大的单个模块（顶层 3052 行 + core/ + schema/ + safety/ + learning/ + docs/ + tests/），但目前存在三个问题阻碍它独立迭代：

1. **它是「杂物抽屉」**：`docs/migration.md` 自己列出了 **7 项不属于它的组件**（UserProfile / Tool / SessionId / AgentInfra / DelegationPreparer / EventBusHITLNotifier / RBACLevel），且都是已识别的待迁移项。
2. **它是「伪中间层」持有者**：整个 task_system 被 10 个生产文件消费，其中 4 个直接 import `Module.task_system.schema`（另有 2 个测试文件）——`from Module.task_system.schema import UserProfile/Tool/SubmitError/...`。schema 名义上是内部模块，实际承担跨边契约职责。
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
- `Module/workflow_engine` **不 import 任何 `core.*`**（除 stdlib + Module.tool + workflow_contract）。`Module.tool` 自身的 `core.schema.error_type` 引用为**已知传递例外**（§7/§10，阶段 3 消解或显式 park）。
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

### 4.1 模块内标识符（15 个）

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

> `TaskSummaryStore` 仅在注释中出现（`task_system.py:452`「在 v2 引入 TaskSummaryStore 持久化」），非真实标识符，**不参与改名**。

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
| `TaskConstraints` → `WorkflowConstraints` | 任务级约束——`DelegationContext.task_constraints` 的字段类型，须与 DelegationContext 同层（否则契约层反向引用引擎内部类型）|
| `SessionId` | 关联会话标识——`DelegationContext.session_id` 的字段类型。core/schema/types.py **已有**同名 NewType；本模块副本删除，统一到此，core 与 workflow_engine 都从契约层引用 |
| `ApprovalId` | 审批标识 |
| `AgentInfra` | 跨边运行时注入容器（见 §6.3）|

> **DelegationContext 字段类型的同层解析**：`DelegationContext` 的三个字段类型——`tools: list[Tool]`（→ Module.tool，§6）、`task_constraints: TaskConstraints`（→ 契约层）、`session_id: SessionId`（→ 契约层）——全部在契约层或 Module.tool 可解析，不产生对 core/引擎内部的引用。

> **跨边但留 workflow_engine 的运行时对象**：`WorkflowMailbox`（core/agent 构造）与 `WorkflowDebugAPI`（core/input 接线）属跨边运行时对象，但含实现逻辑，不入契约层。按 §10 的「单点注入」策略，core 侧在构造点直接 import 它们，阶段 4 前允许。

### 5.2 契约层约束

- **零业务逻辑**：只放类型定义、枚举、协议 ABC、无状态容器、**无状态纯函数**（无 I/O、无内部状态——容纳 `format_tools`/`strip_markdown_fence` 等共享格式化助手，见 §7）。
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
| SessionId | `schema/types.py` | **契约层**（§5.1）| core/schema/types.py **已有**同名 NewType（`SessionId = NewType("SessionId", str)`），运行时兼容；本模块副本删除，统一到契约层定义。core/schema/types.py 重导出或直接改用契约层引用（plan 定）|
| RBACLevel | `schema/types.py` | **Module/tool** | 权限是工具属性 |
| DelegationPreparer | `delegation_preparer.py` | **core 侧**（`core/cognitive/delegation.py`）| 消费者全在 core（loop/tools/lifecycle）；迁出后自然消解 `core.schema.message/part` 反向依赖。其共享的 `format_tools`/`strip_markdown_fence`（utils.py）随迁契约层（§7），与引擎侧 planner/replanner/parser 共用。与 workflow_engine 交互只走契约层类型（`DelegationContext` 等）|
| EventBusHITLNotifier | `protocol.py` | **core 侧**（core/hitl.py）| HITLNotifier 的 EventBus 实现；ABC 留契约层 |
| AgentInfra | `infra.py` | **契约层**（见下）| 跨边运行时注入容器 |

### 6.3 AgentInfra 归属论证

`AgentInfra` 是 core 向 workflow_engine 提供运行资源（AsyncOpenAI/Milvus/http/ToolRegistry/ConstraintConfig）的**注入容器**：
- core 侧 `_lifecycle_task_system.py` 构造并注入；
- workflow_engine 侧 Planner/Executor/Evaluator 只读消费；
- 含契约类型 `ConstraintConfig`。

→ 放**契约层**：它是「core 提供资源、engine 消费」的运行时契约。⚠️ **此决定有意覆盖 migration.md 第 4 项建议（AgentInfra → Core 共享基础设施层）**：若放 core，则 workflow_engine 反向依赖 core，违反 §2 核心不变式；契约层是唯一能同时被两边无环引用的位置。`tool_registry` 字段已用 `object` 规避循环导入，不构成对 Module.tool 的硬引用；AgentInfra 依赖的 openai/httpx/pymilvus 为通用第三方库，不构成对 core 的引用。`llm_model` 默认值 `TASK_SYSTEM_MODEL` env 保持。

## 7. 依赖反转

| 现依赖 | 反转方案 |
|--------|----------|
| `delegation_preparer.py: from core.schema.message import Message` | DelegationPreparer 迁 core 侧后自然消解 |
| `delegation_preparer.py: from core.schema.part import TextPart` | 同上 |
| `delegation_preparer.py` + `core/planner/{planner,replanner,parser}.py: from Module.task_system.utils import format_tools, strip_markdown_fence` | **utils 归位契约层**：两函数为无状态纯函数（LLM prompt 工具格式化 / markdown 围栏剥离），被迁往 core 的 DelegationPreparer 与留在引擎的 planner/replanner/parser 共用 → 迁至 `Module/workflow_contract/utils.py`，双方从契约层引用 |
| `executor.py: from Module.tool.protocol import ToolContext, ToolResult` | **保留**（Module.tool 算 workflow_engine 世界）|
| **传递依赖**：`Module/tool/protocol.py: from core.schema.error_type import ErrorType` | Module.tool 算 workflow_engine 世界，其 `core.*` 依赖使「干净叶子」不变式被**传递性**破坏（core 与 Module.tool 共用该枚举：adapter/task/schema.message/cognitive.tools 等）。消解：`ErrorType` 迁中性位置（建议 `Module/workflow_contract` 或 `Module/_common`），core 与 Module.tool 都从之引用。若评审判为跨切改动过大，须在 §10 显式记录为已知例外并 park，§2 不变式措辞相应改为「workflow_engine 不**直接** import core.*」|

反转完成后，`Module/workflow_engine` 生产代码只依赖：stdlib + `Module.tool`（ErrorType 消解后不含 core.*）+ `Module.workflow_contract`。

## 8. 阶段划分（每阶段独立可交付、测试全绿）

| 阶段 | 内容 | 可交付物 | 验证 |
|------|------|----------|------|
| **1. 改名** | 目录/包/文件重命名 + 15 标识符 Task*→Workflow* + Makefile `--ignore` + 全量 import 更新 | 机械重构，行为零变化 | `make test-quick` 全绿 |
| **2. 契约层提取** | 新建 `Module/workflow_contract/`（类型+协议+utils 纯函数 + AgentInfra），跨边类型迁入，外部消费者改 import | 边界真正成形 | `make test-quick` + 新增契约层单测 |
| **3. 剥皮 + 依赖反转** | migration.md 7 项归位 + DelegationPreparer 迁 core + utils 迁契约层 + 消解 core.schema 反向依赖 + ErrorType 传递依赖消解（或显式 park）| workflow_engine 成干净叶子 | `make test-quick` + `grep -r "from core\." Module/workflow_engine Module/tool` 为空 |
| **4. 独立迭代基建** | `make test-workflow-engine`（unit-or-service 门禁）+ 独立 README + 独立 spec/plan 循环 + 全量文档更新 | 独立迭代成立 | 独立测试命令全绿 |

## 9. 测试策略

- 每阶段后 `make test-quick` 全绿（阶段 1 后 test-quick 与现在一样不含 workflow_engine tests）。
- 阶段 2 新增 `Module/workflow_contract/tests/`（契约类型序列化/校验单测，marker=unit）。
- 阶段 3 后验证：`grep -rn "from core\." Module/workflow_engine Module/tool --include="*.py"` 输出为空（排除 tests 的测试辅助；Module/tool 一并检查以覆盖传递依赖）。
- 阶段 4 提供 `make test-workflow-engine`：`pytest Module/workflow_engine/tests Module/workflow_contract/tests -m "unit or service" -q --tb=short`。
  - ⚠️ **不用 `-m "not slow"`**：`Module/task_system/tests/integration/` 下 `test_planner.py:46` / `test_executor.py:29` / `test_delegation_preparer.py:22` 均为 `pytestmark = pytest.mark.integration`，依赖 `.env` 加载的 `DEEPSEEK_API_KEY` 真实调用 LLM——环境条件性且花钱，不能作为独立迭代门禁。`-m "unit or service"` 与 docs/testing.md 分层约定一致（unit=纯函数无 I/O，service=单模块+mock），排除 integration/slow。

## 10. 风险与边界

- **全量改名 diff 巨大**：~15 标识符 × 模块内 1000+ 处引用 + 10 消费者 + 文档。阶段 1 严格机械（`sed`/重命名 + import 更新），不允许顺手改行为；行为回归由 test-quick + 阶段 1 后 test-normal 兜底。
- **契约层 `WorkflowId` 与 core 侧 `SessionId` 并存**：core/schema/types.py 已有 SessionId 定义，契约层再统一一个 SessionId——两处都从契约层解析，无撞名。core/schema/types.py 与契约层的引用方向由 plan 定（重导出或直改）。
- **`__init__.py` 大导出面**：改名后 `__all__` 全部更新；外部只用到子集，但公开 API 完整性由模块内测试兜底。
- **DelegationPreparer 迁 core 的边界**：它调 LLM 做筛选/填充，迁 core 后仍通过契约层类型与 workflow_engine 交互（`DelegationContext` 等），不产生新跨边；其共享 utils 已随迁契约层（§7）。
- **传递依赖（Module.tool → core.schema.error_type）**：§7 已列消解方案。若判为跨切改动过大（涉及 core/adapter、core/task、core/schema/message、core/cognitive/tools 等 ~5 处 core 引用方），在阶段 3 显式 park 为已知例外，§2 不变式措辞相应收窄为「不直接 import」。
- **文档更新缺口**：改名影响 CLAUDE.md 模块索引、core/README、docs/architecture.md、`Module/task_system/README.md`→workflow_engine README、migration.md。每个阶段随改，阶段 4 全量核对（遵守 readme-conventions.md）。
- **阶段 4 前允许单点注入**：`from Module.workflow_engine import WorkflowEngine` 在 core 保留一处（构造点），工厂抽象留待独立迭代时演进，不在本 spec 范围内强制。

## 11. 相关文档

| 文档 | 内容 |
|------|------|
| [本设计](../../../docs/superpowers/specs/2026-08-01-workflow-engine-separation-design.md) | 分离设计 |
| [TaskSystem migration 清单](../../../Module/task_system/docs/migration.md) | 7 项待迁移组件（本 spec §6 的源）|
| [TaskSystem README](../../../Module/task_system/README.md) | 现架构 |
| [CLAUDE.md](../../../CLAUDE.md) | 项目约定（README 中文、代码注释中文、模块注册）|
