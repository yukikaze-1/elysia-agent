# TaskSystem API 接口定义

> 最终版。

---

## 总体原则

- **`submit_task` 注册为 LLM 工具**：LLM 自主决定是否委派后台任务。零额外字段。
- **简单任务不进 TaskSystem**：CognitiveLoop 直接调工具（现有 ReAct 流程）。
- **一个入口**：`submit(context)`。只处理复杂任务。intent/hints 已合并进 DelegationContext。

---

## 通信架构

```
用户 → LLM 调用 submit_task 工具(intent="查三天天气，下雨给老板发邮件")
  ↓
CognitiveLoop 工具执行器识别 submit_task
  │
  ├─ ① DelegationPreparer.prepare(intent, hints, 全量tools, 对话历史, ...)
  │      │  一次 LLM 调用，调用者是 CognitiveLoop（代码在 TaskSystem 模块内）
  │      ▼
  │   DelegationResult
  │      ├── status=NO_MATCHING_TOOLS → CognitiveLoop 回复用户"缺工具"（不进 TaskSystem）
  │      ├── status=TOO_VAGUE         → CognitiveLoop 请用户澄清（不进 TaskSystem）
  │      └── status=OK → DelegationContext（含筛选后的 tools + 摘要上下文 + 任务约束）
  │                           │
  └─ ② TaskSystem.submit(context) ────────────────────────────┐
         │  调用者仍是 CognitiveLoop                           │
         │  返回 (TaskId, TaskMailbox)，fire-and-forget       │
         └────────────────────────────────────────────────────┘
                                                               │
    ┌──────────────────────────────────────────────────────────┘
    ▼
  TaskSystem 内部
    ├── TaskId + TaskMailbox 创建（Mailbox 创建在 Planner 之前）
    ├── SubAgent.run(context, mailbox)
    │     ├── Planner.plan(context)        ← 第二次 LLM: DelegationContext → Plan(DAG)
    │     ├── PlanValidator.validate()
    │     ├── PlanGraph.from_plan()
    │     └── Orchestrator.run()
    │           ├── Executor.execute()      ← 工具匹配 + Worker 派发
    │           └── Evaluator.evaluate()    ← 可能调 LLM 做语义判定
    │
    ├── TaskMailbox ──→ Agent ──→ OutputManager → WebSocket → 前端 (进度/结果)
    └── HITLNotifier ─→ [EventBus] → 前端审批收件箱 (审批快速通道)
```

**关键顺序**：`DelegationPreparer.prepare()` 和 `TaskSystem.submit()` 都是 CognitiveLoop 调用的，不是 TaskSystem 内部链式调用。DelegationPreparer 需要全量工具列表（CognitiveLoop 持有），TaskSystem 只需要筛选后的 DelegationContext。

- **TaskMailbox**：单播。消费者只有 Agent。WebUI 通过查询 API + SSE 拉数据，不从 Mailbox 消费。
- **HITLNotifier**：审批快速通道。TaskSystem 依赖抽象协议，不依赖 EventBus。

---

# 第一部分：核心契约

> **受众**：CognitiveLoop 开发者。必须理解才能集成 TaskSystem。

---

## 1. 生命周期

```python
class TaskSystem:
    def __init__(self, infra: AgentInfra, hitl_notifier: HITLNotifier):
        """
        infra: LLM Client, Milvus, HTTP, ToolRegistry, ConstraintConfig
        hitl_notifier: 审批快速通道实现。TaskSystem 不关心另一端是什么。
        """

    async def start(self) -> None:
        """启动 Sub-Agent 并发控制、SummaryWriterWorker、恢复 pending checkpoints"""

    async def stop(self) -> None:
        """软关停：等当前节点完成 → checkpoint → 清理"""
```

---

## 2. 核心入口 —— submit

```python
    async def submit(
        self,
        context: DelegationContext,
    ) -> TaskMailbox | SubmitError:
        """
        提交任务。内部先创建 Mailbox → 再启动 Planner/Executor。
        context: DelegationPreparer.prepare() 的产出，包含 intent/hints/
                 tools/task_constraints/summarized_context/session_id/return_mode。
        """
```

没有 `classify()`，没有 `execute_simple()`。一个 `submit()`。

> **内部保证**：Mailbox 创建在 Planner 启动之前。内部 asyncio.Queue，返回即用，零绑定。

---

## 3. 任务控制

```python
    async def pause(self, task_id: TaskId) -> bool:
        """暂停任务。RUNNING → PAUSE_REQUESTED → PAUSED。v1 不支持注入新参数。"""

    async def resume(self, task_id: TaskId) -> TaskMailbox | ResumeError:
        """从 checkpoint 恢复。仅 PAUSED 可 resume（CAS 防双 resume）。

        成功返回已有的 TaskMailbox（pause 时不销毁，保持同一实例）。
        失败返回 ResumeError，调用方据此生成用户可见的错误信息。

        v1 限制：resume() 使用原始 DelegationContext，不支持注入新上下文。
        如需修改任务参数，必须 cancel → 重新 submit。
        """

    async def cancel(self, task_id: TaskId) -> bool:
        """软取消。二次确认由 CognitiveLoop 在上层处理。"""

    async def submit_approval(
        self, task_id: TaskId, agent_id: AgentId, approved: bool,
        feedback: str | None = None,
    ) -> bool:
        """
        审批结果回传。路由到等待审批的 Worker，解除 await。
        task_id: 任务 ID。
        agent_id: 审批发起者 ID，TaskSystem 内部用 (task_id, agent_id)
                  定位正在 await approval_future 的 Worker。
        approved=True  → Worker 继续执行 MUTATION。
        approved=False → PERMISSION_DENIED → Evaluator → 三层降级。
        feedback: 拒绝审批时的理由/修正建议。注入 Planner prompt 用于 Replan。
                  通过审批后无需填写。
        无超时——Worker await 等待，不归还池，不消耗 CPU/LLM/IO。
        """
```

---

## 4. HITLNotifier 协议（CognitiveLoop 实现）

TaskSystem 不依赖 CognitiveLoop 的实现细节（EventBus、WebSocket 等），只依赖此协议。
实现者放在 CognitiveLoop 侧。

```python
class HITLNotifier(ABC):
    """审批快速通道。非阻塞，不抛异常。"""

    async def request_approval(self, event_data: HITLData) -> None:
        """
        推送审批请求到前端收件箱。Worker 触发 MUTATION 时调用。
        event_data 为 HITLData（Pydantic model），与 TaskEvent.data.approval_needed 同源。

        HITLData 字段:
          - task_id, approval_id, agent_id
          - node_id, node_task（当前节点在做什么）
          - tool_name, params, rbac_level, risk_summary
        """
        ...

    async def cancel_approval(self, approval_ids: list[ApprovalId]) -> None:
        """批量取消已发出的审批请求。

        场景：任务取消（该任务所有 pending）、Worker 终止、Replan 换方案。
        清理前端收件箱中已不需要的审批卡片。空列表无操作。
        """
        ...
```

EventBusHITLNotifier 演示实现见 protocol.py。

---

## 5. DelegationPreparer（CognitiveLoop 调用）

> **代码位置**：`Module/task_system/delegation_preparer.py`
> **调用者**：CognitiveLoop
> **将来**：可独立为 delegation 服务

`submit_task` 被调用后、`TaskSystem.submit()` 之前，用一次 LLM 调用准备干净的委派上下文。

```python
class DelegationPreparer:
    async def prepare(
        self,
        intent: str,                       # 用户原始意图（透传）
        hints: str | None,                  # LLM 观察到的元信息
        session_messages: list[dict],       # 全部对话消息（不截断）
        user_profile: UserProfile,         # 用户画像（CognitiveLoop 管理）
        tools: list[Tool],                  # 全量工具注册表（CognitiveLoop 管理）
        global_constraints: ConstraintConfig,  # 全局约束（天花板）
    ) -> DelegationResult: ...
```

**输出 `DelegationResult`：**

| status | 含义 | context | CognitiveLoop 处理 |
|--------|------|---------|-------------------|
| `ok` | 有匹配工具 | DelegationContext（含 intent/hints/tools/return_mode） | 调用 TaskSystem.submit(context) |
| `no_matching_tools` | 工具库缺少所需工具 | None | fail——不是参数问题，直接告知用户 |
| `too_vague` | 意图过于模糊 | None | fail——请用户澄清，不反复猜测 |

DelegationPreparer Prompt 全文见 architecture.md Section 9。

---

## 6. TaskMailbox —— submit/resume 的返回值

**单播。唯一消费者是 Agent。** WebUI 通过查询 API + SSE 拉数据。

```python
class TaskMailbox:
    task_id: TaskId

    async def get(self, timeout: float | None = None) -> TaskEvent | None: ...
    def get_nowait(self) -> TaskEvent | None: ...
    def count(self) -> int: ...

class TaskEvent:
    type: TaskEventType  # progress | approval_needed | completed | failed | cancelled | paused
    data: dict
    # progress:          {"task_id", "node_id", "node_description",
    #                      "percent", "message"}
    # approval_needed:   {"task_id", "approval_id", "agent_id", "node_id",
    #                      "node_task", "tool_name", "params", "rbac_level", "risk_summary"}
    # completed:         {"task_id", "result", "summary", "total_nodes",
    #                      "completed_nodes", "failed_nodes"}
    # failed:            {"task_id", "error", "error_type"}
    # cancelled:         {"task_id", "reason"}
    # paused:            {"task_id", "message"}
```

> `approval_needed` 两条路径，顺序执行（非并发）：
> 1. Mailbox._push(approval_needed) — 系统记录（可靠）
> 2. HITLNotifier.request_approval(data) — 前端收件箱（best-effort，审计流兜底）

**生命周期**：任务终态后 Mailbox 销毁（无 close，GC 回收即可），调用方无需手动管理。
**队列溢出策略**：`maxsize=100`，溢出时丢最旧的 progress，保留 approval_needed/completed/failed。

---

## 7. 状态机

```python
class TaskStatus(StrEnum):
    """任务状态机——11 态。"""
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    WAITING_APPROVAL = "waiting_approval"
    RESUMING = "resuming"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    FAILED = "failed"
    COMPLETED = "completed"
```

---

# 第二部分：查询接口

> **受众**：CognitiveLoop LLM（用户追问任务状态时查询）+ Debug WebUI（渲染仪表盘）。
> 这部分是 Pull 模式——按需拉取快照。Push 走 SSE（Debug 专用）。

---

## 8. Pull —— 查询方法

```python
    def get_task_status(self, task_id: TaskId) -> TaskStatus: ...
    """查询任务当前状态。最轻量——用户问"做完了吗？"时 LLM 调用。"""

    def get_task_summary(self, task_id: TaskId) -> TaskSummary | None: ...
    """查询任务摘要。用户问"刚才那个任务做了什么？"时 LLM 调用。"""

    def list_active_tasks(self) -> list[TaskSummary]: ...
    """列出所有活跃任务。用户问"我有几个后台任务？"时 LLM 调用。"""

    def get_progress(self, task_id: TaskId) -> TaskProgress | None: ...
    """查询任务进度。用户问"到哪一步了？"时 LLM 调用。"""

    def get_task_dag(self, task_id: TaskId) -> TaskDAG | None: ...
    """查询 DAG 图。用户问"分了几个步骤？"时 LLM 调用。"""

    def get_token_usage(self, task_id: TaskId) -> TokenUsage | None: ...
    """查询单任务的 token 消耗。LLM 报成本 / WebUI 展示。"""

    def list_agents(self) -> AgentList: ...
    """当前活跃的 Sub-Agent 和 Worker Agent 列表。Debug 用。"""

    def get_token_stats(self, period: TokenPeriod = TokenPeriod.TODAY) -> TokenStats: ...
    """聚合 token 统计。Debug 仪表盘用。"""
```

---

## 9. Push —— SSE 端点（Debug 专用）

```
GET /debug/api/tasks/stream

event: node_status    data: {"task_id": "t1", "node_id": "A", "status": "success"}
                      # status 使用 NodeStatus 枚举
event: task_status    data: {"task_id": "t1", "status": "completed"}
                      # status 使用 TaskStatus 枚举
event: agent_change   data: {"available_slots": 3, "active_sub_agents": 1, "active_workers": 2}
```

使用模式：初始拉全量 DAG → 监听 SSE 增量 → 收到变更后按需 Fetch。

---

# 第三部分：辅助类型

> 上面所有方法签名中出现的类型汇总。按归属分组。与 schema.py 同步。

---

## 10. 基础 NewType

```python
TaskId = NewType("TaskId", str)
"""任务唯一标识符。TaskSystem 内部生成，对外透明。NewType 防止调用方随意传 str。"""

AgentId = NewType("AgentId", str)
"""Agent 唯一标识符。TaskSystem 内部生成，用于 submit_approval 路由。"""

ApprovalId = NewType("ApprovalId", str)
"""审批请求唯一标识符。TaskSystem 内部生成，用于 cancel_approval 清理。"""

SessionId = NewType("SessionId", str)
"""会话唯一标识符。CognitiveLoop 管理，TaskSystem 仅用于日志关联和 Debug 追踪。"""

NodeId = NewType("NodeId", str)
"""DAG 节点唯一标识符。出现在 DAGNodeInfo、TaskProgress、TokenComponent 中。
调用方不需要自己构造——查询结果中包含，日志追踪用。"""

ArtifactId = NewType("ArtifactId", str)
"""产物唯一标识符。ArtifactStore 存储时的 key，与 DAGNode.output_as 对应。"""
```

---

## 11. 错误码

三层错误码模型：API 层 → 任务层 → 节点层。

```python
class SubmitRejectionCode(StrEnum):
    """submit() 同步拒绝码——4 项。参数校验不通过时返回。"""
    INVALID_PARAM = "invalid_param"
    PLAN_TOO_LARGE = "plan_too_large"
    CONTEXT_WINDOW_EXCEEDED = "context_window_exceeded"
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"

class TaskErrorCode(StrEnum):
    """任务运行时错误码——10 项。TaskState.error_code + TaskEvent error_type。"""
    # 规划阶段
    PLANNING_FAILED = "planning_failed"
    PLAN_TOO_LARGE = "plan_too_large"
    # 执行阶段
    TOOL_ERROR = "tool_error"
    PERMISSION_DENIED = "permission_denied"
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"
    # 资源耗尽
    FAILURE_MELTDOWN = "failure_meltdown"
    TIMEOUT = "timeout"
    MUTATION_LIMIT_REACHED = "mutation_limit_reached"
    # 外部 / 兜底
    CANCELLED_BY_USER = "cancelled_by_user"
    INTERNAL_ERROR = "internal_error"

class NodeErrorCode(StrEnum):
    """节点执行错误码——8 项。NodeResult.error 使用。"""
    TOOL_ERROR = "tool_error"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_NOT_FOUND = "tool_not_found"
    PERMISSION_DENIED = "permission_denied"
    WORKER_CRASH = "worker_crash"
    CONTEXT_OVERFLOW = "context_overflow"
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"
    NETWORK_ERROR = "network_error"

class ResumeErrorCode(StrEnum):
    """resume() 失败时的错误码——6 项。"""
    CHECKPOINT_NOT_FOUND = "checkpoint_not_found"
    CHECKPOINT_CORRUPTED = "checkpoint_corrupted"
    CHECKPOINT_VERSION_MISMATCH = "checkpoint_version_mismatch"
    INVALID_STATE = "invalid_state"
    PENDING_APPROVALS_FAILED = "pending_approvals_failed"
    NODE_RECOVERY_FAILED = "node_recovery_failed"
```

---

## 12. 委派相关

```python
class ConstraintConfig:
    """全局约束配置（TaskSystem 构造时注入）。所有执行路径的硬天花板。"""
    max_total_tokens: int = 100_000       # 整个任务 LLM 调用总 token 上限
    max_context_tokens: int = 32_000      # 单次 prompt 最大 token 数
    max_failure_count: int = 5            # 连续失败 N 次后熔断
    max_dag_nodes: int = 20               # DAG 节点数上限
    max_retries_per_node: int = 3         # 单节点最大重试次数
    max_mutations: int = 10               # 敏感操作次数上限
    max_task_duration_s: float = 600.0    # 整个任务最大执行时间（秒）
    max_tool_error_chars: int = 2000      # 工具错误消息截断长度

class TaskConstraints:
    """任务级约束。与全局 ConstraintConfig 取 min。None 表示使用全局默认。"""
    max_dag_nodes: int                    # DAG 节点数上限
    max_mutations: int                    # 敏感操作次数上限
    max_total_tokens: int | None          # None=全局默认
    max_task_duration_s: float | None     # None=全局默认
    max_failure_count: int | None         # None=全局默认

class DelegationContext:
    """status=ok 时传入。Planner 的工作说明书——所有信息在此，不分散传参。"""
    intent: str                          # LLM 输出的完整任务意图（非用户原始输入）
    hints: str | None                     # LLM 观察到的元信息（紧急程度、偏好等）
    tools: list[Tool]                     # 本次任务可用的工具列表（从全量工具中筛选）
    task_constraints: TaskConstraints     # 任务级限制（与 ConstraintConfig 取 min）
    summarized_context: str               # LLM 摘要的相关上下文（≤ 500 字）
    session_id: SessionId                 # 日志关联
    return_mode: ReturnMode               # 产出报告模式——自然语言 or 结构化

class ReturnMode(StrEnum):
    """产出报告模式。"""
    NATURAL_LANGUAGE = "natural_language"  # 自然语言总结（默认）
    STRUCTURED = "structured"              # 结构化数据（node_results）

class DelegationStatus(StrEnum):
    """DelegationPreparer 的三态判定结果。"""
    OK = "ok"
    NO_MATCHING_TOOLS = "no_matching_tools"
    TOO_VAGUE = "too_vague"

class DelegationResult:
    """DelegationPreparer 的产出。status 决定 context 是否有值。"""
    status: DelegationStatus             # ok / no_matching_tools / too_vague
    context: DelegationContext | None    # ok 时有值
    reason: str                          # LLM 判定理由（始终有）
    missing_tools: list[Tool]            # no_matching_tools 时，缺少的工具
    suggestion: str | None               # too_vague 时，给用户的澄清建议
```

---

## 13. 错误类型

```python
class SubmitError:
    """submit() 拒绝时的结构化错误。"""
    code: SubmitRejectionCode
    message: str
    details: dict
    suggestions: list[str]               # 修复建议

class ResumeError:
    """resume() 失败时的结构化错误。"""
    code: ResumeErrorCode
    message: str
    details: dict
    suggestions: list[str]               # 修复建议
```

---

## 14. 审批

```python
class RBACLevel(StrEnum):
    """工具权限级别。"""
    READ_ONLY = "read_only"       # 只读，完全自主
    SAFE_WRITE = "safe_write"     # 安全写入（沙箱内），条件自主
    MUTATION = "mutation"         # 外部变更，必须审批

class HITLData:
    """HITL 审批请求数据。同一份数据走 HITLNotifier（强类型）和 Mailbox（审计流）。"""
    task_id: TaskId
    approval_id: ApprovalId
    agent_id: AgentId
    node_id: NodeId
    node_task: str                       # 当前节点在做什么（来自 DAGNode.description）
    tool_name: str
    params: dict
    rbac_level: RBACLevel                # 工具权限级别，前端据此渲染危险程度
    risk_summary: str
```

---

## 15. 查询结果类型

```python
class TaskSummary:
    """任务完成/查询时的摘要视图。"""
    task_id: TaskId
    status: TaskStatus
    intent: str
    created_at: str                      # ISO 8601，对齐 TaskState.created_at
    duration_ms: int                     # 总耗时（预计算）
    nodes: dict                          # {"total": N, "completed": N, "failed": N, "skipped": N}
    tools_used: list[Tool]               # 审计：本任务调用了哪些工具
    token_usage: dict                    # {"planner": N, "executor": N, "evaluator": N, ...}
    approvals: dict                      # {"requested": N, "approved": N, "denied": N}
    error: str | None                    # 任务级错误消息（来自 TaskState.error）
    error_code: TaskErrorCode | None     # 任务级错误码（来自 TaskState.error_code）
    replans: int

class TaskProgress:
    """单任务执行进度快照。"""
    task_id: TaskId
    status: TaskStatus
    current_node_id: NodeId | None
    completed_nodes: int                 # 已完成节点数
    total_nodes: int                     # 总节点数
    elapsed_ms: int
    estimated_remaining_ms: int | None   # None=无法估算

class TaskDAG:
    """任务 DAG 的可视化数据。"""
    nodes: list[DAGNodeInfo]
    edges: list[tuple[NodeId, NodeId]]

class DAGNodeInfo:
    """DAG 图中单个节点的展示信息（查询用，非内部编排数据）。

    注意：当前不包含 tool_name——v1 工具由 Executor 运行时匹配，
    整个管线尚未携带工具名。后续在 NodeResult 上补 tool_name 时同步加回。
    """
    node_id: NodeId
    description: str
    status: NodeStatus                   # pending / running / success / failed / skipped / unrecoverable / waiting_approval / cancelled
    progress: float
    error: str | None                    # 人类可读，来自 NodeState.error
    error_code: NodeErrorCode | None     # 机器可读，来自 NodeState.error_code

class NodeStatus(StrEnum):
    """DAG 节点的运行时状态——8 态。出现在 DAGNodeInfo 中。"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNRECOVERABLE = "unrecoverable"
    WAITING_APPROVAL = "waiting_approval"
    CANCELLED = "cancelled"

class AgentType(StrEnum):
    """Agent 类型。"""
    SUB = "sub"        # Sub-Agent：任务级执行单元
    WORKER = "worker"  # Worker Agent：节点级执行单元

class AgentStatus(StrEnum):
    """Agent 运行时状态。"""
    RUNNING = "running"
    PAUSED = "paused"
    AWAITING_APPROVAL = "awaiting_approval"

class AgentList:
    """Agent 池当前状态。"""
    sub_agents: list[AgentInfo]
    worker_agents: list[AgentInfo]
    total_capacity: int
    available_slots: int

class AgentInfo:
    """单个 Agent 的运行信息。"""
    agent_id: AgentId
    agent_type: AgentType
    task_id: TaskId
    node_id: NodeId | None               # Worker 专用
    status: AgentStatus
    started_at: str                      # ISO 8601
    last_seen_at: str                    # ISO 8601
    duration_ms: int

class TokenPeriod(StrEnum):
    """Token 统计时间段。"""
    TODAY = "today"
    WEEK = "week"
    MONTH = "month"
    ALL = "all"

class TokenComponentType(StrEnum):
    """Token 消耗来源组件。"""
    PLANNER = "planner"
    EXECUTOR = "executor"
    EVALUATOR = "evaluator"
    REFLECTOR = "reflector"    # TODO: 尚未实现，Token 统计预留

class TokenUsage:
    """单个任务的 token 消耗明细。"""
    task_id: TaskId
    components: list[TokenComponent]

class TokenComponent:
    """单个组件的 token 消耗记录。"""
    component: TokenComponentType
    node_id: NodeId | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int                    # prompt + completion（预计算）
    model: str

class TokenStats:
    """聚合 token 统计（按时间段）。Debug 仪表盘用。"""
    period: TokenPeriod
    total_tokens: int
    by_component: dict[str, int]         # TokenComponentType → 总量
    by_model: dict[str, int]
```

---

## 16. CognitiveLoop 侧类型（占位）

```python
class UserProfile(BaseModel):
    """用户画像。CognitiveLoop 管理，DelegationPreparer 只读消费。"""
    # TODO: 字段由 CognitiveLoop 侧定义，当前占位

class Tool(BaseModel):
    """工具描述。ToolRegistry 管理，DelegationPreparer 从全量中筛选。"""
    name: str                            # 工具唯一标识符
    # TODO: 其余字段由 ToolRegistry / ToolPlugin 侧定义，当前占位
```
