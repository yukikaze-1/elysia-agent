# TaskSystem 类型迁移清单

> 记录当前临时放在 TaskSystem 模块内、但不属于此模块的类型和组件。
> 后续架构演进时逐项移走。

---

## 1. UserProfile

- **当前位置**: `Module/workflow_engine/schema/protocol.py`
- **应移至**: CognitiveLoop 侧（`core/` 或专属模块）
- **原因**: 用户画像由 CognitiveLoop 管理，DelegationPreparer 只读消费。TaskSystem 不应拥有此类型。
- **当前状态**: 占位（`model_config = {"extra": "allow"}`），字段由 CognitiveLoop 侧定义
- **迁移后**: TaskSystem 改为 `from core.user_profile import UserProfile`

## 2. Tool

- **当前位置**: `Module/workflow_engine/schema/protocol.py`
- **应移至**: `Module/tool/`（ToolRegistry / ToolPlugin 侧）
- **原因**: 工具描述由 ToolRegistry 定义和管理。DelegationPreparer 从全量工具列表中筛选，只读引用。
- **当前状态**: ✅ 已完成 — 已升级为具体模型：`name: str`, `description: str = ""`, `capabilities: list[str]`。TaskSystem 仅持有轻量投影（许可列表），不含执行逻辑或 SafetyProfile。
- **迁移后**: TaskSystem 改为 `from Module.tool import Tool`

## 3. SessionId

- **当前位置**: `Module/workflow_engine/schema/types.py`
- **应移至**: CognitiveLoop 侧或 Core 共享类型层
- **原因**: 会话 ID 由 CognitiveLoop 生成和管理。TaskSystem 仅用于日志关联和 Debug 追踪，不生成 SessionId。
- **当前状态**: NewType，已正确定义。TaskSystem 侧只读使用。
- **迁移后**: TaskSystem 改为 `from core.types import SessionId`

## 4. AgentInfra

- **当前位置**: `Module/workflow_engine/infra.py`
- **应移至**: Core 共享基础设施层
- **原因**: AgentInfra 是跨模块共享的底层资源容器（LLM Client、Milvus、HTTP、ToolRegistry、ConstraintConfig）。TaskSystem、CognitiveLoop 等都可能用到。不属于 TaskSystem 专属。
- **当前状态**: dataclass，文件顶部已标注"后续会转移到其它地方"
- **迁移后**: `infra.py` 整体移走或删除，TaskSystem 改为 `from core.infra import AgentInfra`

## 5. DelegationPreparer

- **当前位置**: `core/cognitive/delegation.py`（已迁入 core 侧）
- **应移至**: 独立 delegation 服务，或 CognitiveLoop 侧
- **原因**: 调用者是 CognitiveLoop（在 `submit_task` 工具执行器中调用 `prepare()`）。代码虽在 TaskSystem 模块内，但职责是"提交前的预处理"，将来可独立为 delegation 服务。
- **当前状态**: 已完整实现——`prepare()` 真实调 LLM（DeepSeek v4），含 JSON 解析、工具名防幻觉校验、约束估算、全局约束注入 system prompt
- **迁移后**: delegation_preparer.py 已 git mv 至 `core/cognitive/delegation.py`，消费者改 `from core.cognitive.delegation import DelegationPreparer`（见 workflow_engine 分离项目 Task 9）

## 6. EventBusHITLNotifier

- **当前位置**: `Module/workflow_engine/protocol.py`
- **应移至**: CognitiveLoop 侧
- **原因**: HITLNotifier 的演示实现，基于 EventBus（CognitiveLoop 的内部机制）。TaskSystem 只定义 HITLNotifier 抽象协议（ABC），不拥有具体实现。
- **当前状态**: 演示代码，已标注 ⚠️。CognitiveLoop 重构时替换为正式实现。
- **迁移后**: protocol.py 仅保留 HITLNotifier ABC。EventBusHITLNotifier 移入 CognitiveLoop 侧（如 `core/hitl.py`）

## 7. RBACLevel（待定）

- **当前位置**: `Module/workflow_engine/schema/types.py`
- **应移至**: 待定（建议 `Module/tool/` 为 canonical home）
- **原因**: Tools 在 `Module/tool/` 注册时声明权限级别，TaskSystem 执行时校验。两个模块都需要此枚举。以 Tool 侧为 canonical home 更合理——权限级别是工具的属性，应在工具定义处声明。
- **当前状态**: 已标记 TODO，暂留 TaskSystem
- **迁移后**: `Module/tool/` 定义 RBACLevel，TaskSystem 改为 re-export

---

## 时间线

无硬性 deadline。建议在以下时机逐项迁移：

| 时机 | 迁移项 |
|------|--------|
| CognitiveLoop 定义 UserProfile 实际字段时 | #1 UserProfile |
| ✅ 已完成（Tool 模型已具名化） | #2 Tool |
| Core 层抽取共享类型时 | #3 SessionId |
| TaskSystem 开发完成、上提基础设施时 | #4 AgentInfra |
| DelegationPreparer Prompt 调优完成、稳定后 | #5 DelegationPreparer |
| CognitiveLoop 重构 HITL 审批通道时 | #6 EventBusHITLNotifier |
| Tool 模块与 TaskSystem 解耦时 | #7 RBACLevel |
