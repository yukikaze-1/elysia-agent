# TaskSystem 类型迁移清单

> 记录 workflow_engine 分离项目中从 TaskSystem（现 WorkflowEngine）模块移出的跨边类型与组件。
> **7 项全部归位完成**（2026-08-02，见 workflow_engine 分离项目 Task 14）。

---

## 1. UserProfile

- **当前位置**: `core/cognitive/user_profile.py`（✅ 已迁入 core 侧）
- **应移至**: CognitiveLoop 侧（`core/` 或专属模块）
- **原因**: 用户画像由 CognitiveLoop 管理，DelegationPreparer 只读消费。TaskSystem 不应拥有此类型。
- **当前状态**: ✅ 已完成 — 已归位 `core/cognitive/user_profile.py`（workflow_engine 分离 Task 8），CognitiveLoop 拥有
- **迁移后**: 消费方改 `from core.cognitive.user_profile import UserProfile`

## 2. Tool

- **当前位置**: `Module/tool/tool_descriptor.py`（✅ 已归位）
- **应移至**: `Module/tool/`（ToolRegistry / ToolPlugin 侧）
- **原因**: 工具描述由 ToolRegistry 定义和管理。DelegationPreparer 从全量工具列表中筛选，只读引用。
- **当前状态**: ✅ 已完成 — 已升级为具体模型：`name: str`, `description: str = ""`, `capabilities: list[str]`。WorkflowEngine 仅持有轻量投影（许可列表），不含执行逻辑或 SafetyProfile
- **迁移后**: 消费方改 `from Module.tool import Tool`

## 3. SessionId

- **当前位置**: `Module/workflow_contract/schema.py`（✅ 已迁契约层）
- **应移至**: 契约层
- **原因**: 会话 ID 由 CognitiveLoop 生成和管理。TaskSystem 仅用于日志关联和 Debug 追踪，不生成 SessionId。
- **当前状态**: ✅ 已完成 — 已迁契约层 `Module/workflow_contract/schema.py`（Task 5）；core 侧 `core/schema/types.py` 重导出统一入口
- **迁移后**: 消费方改 `from Module.workflow_contract import SessionId`

## 4. AgentInfra

- **当前位置**: `Module/workflow_contract/infra.py`（✅ 已迁契约层）
- **应移至**: **契约层**（覆盖原「Core 共享基础设施层」建议——理由见 spec §6.3）
- **原因**: AgentInfra 是跨模块共享的底层资源容器（LLM Client、Milvus、HTTP、ToolRegistry、ConstraintConfig）。TaskSystem、CognitiveLoop 等都可能用到。不属于 TaskSystem 专属。
- **当前状态**: ✅ 已完成 — 已归契约层 `Module/workflow_contract/infra.py`（Task 5）。放契约层而非 core 的原因：它是「core 提供资源、engine 消费」的运行时契约，若放 core 则 workflow_engine 反向依赖 core，违反 §2 分离不变式；契约层是唯一能同时被两边无环引用的位置（详见 spec §6.3）
- **迁移后**: 消费方改 `from Module.workflow_contract import AgentInfra`

## 5. DelegationPreparer

- **当前位置**: `core/cognitive/delegation.py`（✅ 已迁入 core 侧）
- **应移至**: 独立 delegation 服务，或 CognitiveLoop 侧
- **原因**: 调用者是 CognitiveLoop（在 `submit_task` 工具执行器中调用 `prepare()`）。代码虽在 TaskSystem 模块内，但职责是"提交前的预处理"，将来可独立为 delegation 服务。
- **当前状态**: ✅ 已完成 — 已 git mv 至 `core/cognitive/delegation.py`（Task 9），`prepare()` 真实调 LLM（DeepSeek v4），含 JSON 解析、工具名防幻觉校验、约束估算、全局约束注入 system prompt
- **迁移后**: 消费方改 `from core.cognitive.delegation import DelegationPreparer`

## 6. EventBusHITLNotifier

- **当前位置**: `core/hitl.py`（✅ 已迁入 core 侧）
- **应移至**: CognitiveLoop 侧
- **原因**: HITLNotifier 的演示实现，基于 EventBus（CognitiveLoop 的内部机制）。TaskSystem 只定义 HITLNotifier 抽象协议（ABC），不拥有具体实现。
- **当前状态**: ✅ 已完成 — `EventBusHITLNotifier` 已迁入 `core/hitl.py`（Task 10），演示代码保留 ⚠️ 标注，CognitiveLoop 重构时替换为正式实现。HITLNotifier ABC 归契约层 `Module/workflow_contract/schema.py`
- **迁移后**: 消费方改 `from core.hitl import EventBusHITLNotifier`；`Module/workflow_engine/protocol.py` 已删除

## 7. RBACLevel

- **当前位置**: `Module/tool/capability.py`（✅ 已归位）
- **应移至**: `Module/tool/` 为 canonical home
- **原因**: Tools 在 `Module/tool/` 注册时声明权限级别，TaskSystem 执行时校验。两个模块都需要此枚举。以 Tool 侧为 canonical home 更合理——权限级别是工具的属性，应在工具定义处声明。
- **当前状态**: ✅ 已完成 — 已归位 `Module/tool/capability.py`（Task 8），契约层 `HITLData.rbac_level` 顶部直接 import
- **迁移后**: 消费方改 `from Module.tool.capability import RBACLevel`

---

## 时间线

**7 项全部归位**（2026-08-02，workflow_engine 分离项目 Task 5/8/9/10/14）：

| 迁移项 | 状态 |
|--------|------|
| #1 UserProfile → core/cognitive/user_profile.py | ✅ 已完成（Task 8） |
| #2 Tool → Module/tool/tool_descriptor.py | ✅ 已完成 |
| #3 SessionId → Module/workflow_contract | ✅ 已完成（Task 5） |
| #4 AgentInfra → Module/workflow_contract/infra.py | ✅ 已完成（Task 5，覆盖「Core 共享基础设施层」原建议） |
| #5 DelegationPreparer → core/cognitive/delegation.py | ✅ 已完成（Task 9） |
| #6 EventBusHITLNotifier → core/hitl.py | ✅ 已完成（Task 10） |
| #7 RBACLevel → Module/tool/capability.py | ✅ 已完成（Task 8） |
