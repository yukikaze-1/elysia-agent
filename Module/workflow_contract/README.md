# WorkflowContract — 跨边契约层

> 文档导航：[项目文档](../../README.md) → [Module 文档](../README.md)

core 应用层与 `Module/workflow_engine` 之间**唯一跨边类型来源**。只承载类型定义、枚举、协议 ABC、无状态纯函数与运行时注入容器，**零业务逻辑**（零 I/O、零引擎逻辑）。

## 职责

三区架构中，workflow_contract 位于 core 与 workflow_engine 之间，让两边各自单向 import、互不感知对方内部实现：

```
core/（应用层） ──import──▶ workflow_contract ◀──import── workflow_engine（干净叶子）
```

契约层只包含：

- **类型定义**：`NewType` 标识符、Pydantic 模型（`BaseModel`）
- **枚举**：`StrEnum` 状态/原因/返回模式
- **协议 ABC**：`HITLNotifier`（抽象接口）
- **无状态纯函数**：`format_tools` / `parse_iso` / `strip_markdown_fence`
- **运行时注入容器**：`AgentInfra`——core 向 engine 提供运行资源（LLM/Milvus/http/ToolRegistry/ConstraintConfig）的载体，是整个生命周期共享、不含任务状态、协程安全的 dataclass

## 允许的依赖方向

- ✅ 不 import `core.*`（Task 12 门禁验证：含延迟 import 为零）
- ✅ 不 import `Module.workflow_engine`（反向依赖会成环）
- ✅ 允许 import `Module.tool`（`Tool` / `RBACLevel` 仅用于注解——通过延迟 import + `model_rebuild()` 打破 Module.tool → core.schema → 本模块的循环）
- ✅ 允许第三方库（pydantic / httpx / openai / enum / typing 等）

## 类型清单

### 标识符（NewType）

`WorkflowId` · `AgentId` · `ApprovalId` · `SessionId` · `NodeId`

### 枚举（StrEnum）

| 枚举 | 含义 |
|------|------|
| `WorkflowStatus` | 工作流生命周期状态（pending/planning/running/.../completed） |
| `SubmitRejectionCode` | submit 拒绝原因码 |
| `ReturnMode` | 回传模式（STRUCTURED / NATURAL_LANGUAGE） |
| `DelegationStatus` | 委派结果三态（ok / no_matching_tools / too_vague） |
| `WorkflowEventType` | 对外事件类型（progress/completed/failed/cancelled/paused） |

### 约束（Pydantic）

| 模型 | 含义 |
|------|------|
| `ConstraintConfig` | 全局天花板约束（max_tokens / max_dag_nodes / 超时等，随 AgentInfra 注入） |
| `WorkflowConstraints` | 单任务约束（与 ConstraintConfig 取 min 生效） |

### 委派协议（Pydantic）

| 模型 | 含义 |
|------|------|
| `DelegationContext` | 委派上下文（intent + tools + session_id + 约束） |
| `DelegationResult` | DelegationPreparer 产出（status + context / reason） |
| `SubmitError` | submit 失败载荷（code + message + suggestions） |

### HITL（Pydantic + ABC）

| 模型 | 含义 |
|------|------|
| `HITLData` | 审批请求数据（task_id / node / tool_name / params） |
| `HITLNotifier` | 审批通知抽象接口（ABC） |

### 事件（Pydantic）

| 模型 | 含义 |
|------|------|
| `WorkflowEvent` | 引擎对外事件载荷（type + data） |

### 运行时注入容器

| 类型 | 含义 |
|------|------|
| `AgentInfra` | 跨任务共享资源容器（llm / milvus / http / tool_registry / constraints） |

### 纯函数

| 函数 | 含义 |
|------|------|
| `format_tools` | Tool 列表 → LLM prompt 可读文本 |
| `parse_iso` | ISO 时间戳 → offset-aware UTC datetime |
| `strip_markdown_fence` | 去除 LLM 响应中 ``` 代码块包裹 |

## 测试命令

```bash
# 单包：
conda run -n Elysia python -m pytest Module/workflow_contract/tests -v

# 随 workflow_engine 门禁一起：
make test-workflow-engine
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module/workflow_engine/README.md](../workflow_engine/README.md) | 引擎本体——本契约层的唯一 engine 侧消费者 |
| [Module 文档](../README.md) | 所有 Module 服务总览 |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 |
| [core/cognitive/README.md](../../core/cognitive/README.md) | 认知循环——core 侧主要消费者（submit_task 触发） |
