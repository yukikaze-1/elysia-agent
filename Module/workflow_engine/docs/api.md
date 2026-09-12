# WorkflowEngine API 接口定义

> 最终版。

---

## 总体原则

- **`submit_task` 注册为 LLM 工具**：LLM 自主决定是否委派后台任务。零额外字段。
- **简单任务不进 WorkflowEngine**：CognitiveLoop 直接调工具（现有 ReAct 流程）。
- **一个入口**：`submit(context)`。只处理复杂任务。intent/hints 已合并进 DelegationContext。

---

## 通信架构

```
用户 → LLM 调用 submit_task 工具(intent="查三天天气，下雨给老板发邮件")
  ↓
CognitiveLoop 工具执行器识别 submit_task
  │
  ├─ ① DelegationPreparer.prepare(intent, hints, 全量tools, 对话历史, ...)
  │      │  一次 LLM 调用，调用者是 CognitiveLoop（代码在 WorkflowEngine 模块内）
  │      ▼
  │   DelegationResult
  │      ├── status=NO_MATCHING_TOOLS → CognitiveLoop 回复用户"缺工具"（不进 WorkflowEngine）
  │      ├── status=TOO_VAGUE         → CognitiveLoop 请用户澄清（不进 WorkflowEngine）
  │      └── status=OK → DelegationContext（含筛选后的 tools + 摘要上下文 + 任务约束）
  │                           │
  └─ ② WorkflowEngine.submit(context) ────────────────────────────┐
         │  调用者仍是 CognitiveLoop                           │
         │  返回 (WorkflowId, WorkflowMailbox)，fire-and-forget       │
         └────────────────────────────────────────────────────┘
                                                               │
    ┌──────────────────────────────────────────────────────────┘
    ▼
  WorkflowEngine 内部
    ├── WorkflowId + WorkflowMailbox 创建（Mailbox 创建在 Planner 之前）
    ├── SubAgent.run(context, mailbox)
    │     ├── Planner.plan(context)        ← 第二次 LLM: DelegationContext → Plan(DAG)
    │     ├── plan, plan_graph = PlanValidator.validate(plan)
    │     └── Orchestrator.run()
    │           ├── Executor.execute()      ← v1 LLM 工具匹配 + ToolManager 调用，v2 Worker 派发
    │           └── Evaluator.evaluate()    ← 可能调 LLM 做语义判定
    │
    ├── WorkflowMailbox ──→ Agent ──→ OutputManager → WebSocket → 前端 (进度/结果)
    └── HITLNotifier ─→ [EventBus] → 前端审批收件箱 (审批快速通道)
```

**关键顺序**：`DelegationPreparer.prepare()` 和 `WorkflowEngine.submit()` 都是 CognitiveLoop 调用的，不是 WorkflowEngine 内部链式调用。DelegationPreparer 需要全量工具列表（CognitiveLoop 持有），WorkflowEngine 只需要筛选后的 DelegationContext。

- **WorkflowMailbox**：单播。消费者只有 Agent。WebUI 通过查询 API + SSE 拉数据，不从 Mailbox 消费。
- **HITLNotifier**：审批快速通道。WorkflowEngine 依赖抽象协议，不依赖 EventBus。

---

# 第一部分：核心契约

> **受众**：CognitiveLoop 开发者。必须理解才能集成 WorkflowEngine。

---

## 1. 生命周期

```python
class WorkflowEngine:
    def __init__(self, infra: AgentInfra, hitl_notifier: HITLNotifier, *, max_sub_agents: int = 5):
        """
        infra: LLM Client, Milvus, HTTP, ToolRegistry, ConstraintConfig
        hitl_notifier: 审批快速通道实现。WorkflowEngine 不关心另一端是什么。
        max_sub_agents: Sub-Agent 并发上限（keyword-only），默认 5。
        """

    async def start(self) -> None:
        """初始化 BoundedSemaphore 并发控制。所有业务组件（Planner/Executor/Evaluator 等）现在是 per-task 的，由 SubAgent.run() 内部创建。"""

    async def stop(self, timeout: float = 10.0) -> None:
        """Graceful shutdown：拒绝新提交 → 等待活跃任务完成（timeout 秒）→ 超时 cancel → 兜底清理"""
```

---

## 2. 核心入口 —— submit

```python
    async def submit(
        self,
        context: DelegationContext,
    ) -> WorkflowMailbox | SubmitError:
        """
        提交任务。内部先创建 Mailbox → 再启动 Planner/Executor。
        context: DelegationPreparer.prepare() 的产出，包含 intent/hints/
                 tools/task_constraints/summarized_context/session_id/return_mode。
        """
```

没有 `classify()`，没有 `execute_simple()`。一个 `submit()`。

> **内部保证**：Mailbox 创建在 Planner 启动之前。内部 collections.deque + asyncio.Event，返回即用，零绑定。

---

## 3. 任务控制

```python
    async def pause(self, task_id: WorkflowId, timeout: float = 30.0) -> bool:
        """✅ 已实现——暂停任务。设置 exec_state.pause_requested → Orchestrator 检测 → PAUSED。
        轮询等待确认（默认 30s），返回 True=暂停确认，False=超时/已终态/不存在。幂等。"""

    async def resume(self, task_id: WorkflowId) -> WorkflowMailbox | ResumeError:
        """✅ 已实现——从 PAUSED 恢复执行。幂等——RUNNING 直接返回 mailbox。"""

    async def cancel(self, task_id: WorkflowId) -> bool:
        """✅ 已实现——幂等软取消。设置 exec_state.cancel_requested 标志，Orchestrator 安全点停止。
        所有路径返回 True（幂等）。二次确认由 CognitiveLoop 在上层处理。"""

    def submit_approval(
        self, task_id: WorkflowId, agent_id: AgentId, approved: bool,
        feedback: str | None = None,
    ) -> bool:
        """✅ 已实现（v1）：桥接到 ToolManager.submit_approval()。
        agent_id 作为 request_id 委托给 SafetyPipeline 处理。
        task_id / feedback 为 v2 预留。
        task_id: 任务 ID。
        agent_id: 审批发起者 ID，WorkflowEngine 内部用 (task_id, agent_id)
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

WorkflowEngine 不依赖 CognitiveLoop 的实现细节（EventBus、WebSocket 等），只依赖此协议。
实现者放在 CognitiveLoop 侧。

```python
class HITLNotifier(ABC):
    """审批快速通道。非阻塞，不抛异常。"""

    async def request_approval(self, event_data: HITLData) -> None:
        """
        推送审批请求到前端收件箱。Worker 触发 MUTATION 时调用。
        event_data 为 HITLData（Pydantic model），与 WorkflowEvent.data.approval_needed 同源。

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

EventBusHITLNotifier 演示实现见 core/hitl.py（ABC 在契约层 Module/workflow_contract/schema.py）。

---

## 5. DelegationPreparer（CognitiveLoop 调用）

> **代码位置**：`core/cognitive/delegation.py`（2026-08 从 workflow_engine 迁出）
> **调用者**：CognitiveLoop
> **状态**：v1 已实现（2026-06-14）
> **将来**：可独立为 delegation 服务

`submit_task` 被调用后、`WorkflowEngine.submit()` 之前，用一次 LLM 调用准备干净的委派上下文。

```python
class DelegationPreparer:
    # v1: 内部创建 AsyncOpenAI（读 DEEPSEEK_API_KEY）。Planner 已改用 infra.llm。
    #     后续独立为 delegation 服务时改为 LLM client 注入。
    def __init__(self) -> None: ...

    async def prepare(
        self,
        intent: str,                       # LLM 输出的完整任务意图（透传，不修改）
        hints: str | None,                  # LLM 观察到的元信息
        session_messages: list[Message],     # 全部对话消息（不截断，LLM 自己判断相关性）
                                            # Message 类型来自 core.schema.message
        user_profile: UserProfile,         # 用户画像（CognitiveLoop 管理，v1 占位符）
        tools: list[Tool],                  # 全量工具注册表（CognitiveLoop 管理）
        global_constraints: ConstraintConfig,  # 全局约束（天花板）
        session_id: SessionId,              # 会话 ID（CognitiveLoop 透传）
    ) -> DelegationResult: ...
```

**四个职责（仅 status=ok 时产出 DelegationContext）**：

1. **意图透传**：intent/hints 原样写入 DelegationContext，不做修改。
2. **工具筛选**：从全量 Tool 列表中选出与任务直接相关的工具，传给 Planner。
3. **上下文摘要**：从对话历史中提取与任务相关的信息（≤500 字）。
4. **任务级约束**：LLM 根据意图复杂度估算 `max_dag_nodes`/`max_mutations` 等字段。

**后处理校验 `_validate_output()`**：

LLM 返回的 tools 与输入 tools 做交集（按 name 精确匹配）：
- 幻觉工具名（LLM 编造的不存在的工具）→ 剔除 + warning
- status=ok 但筛选后 tools 为空 → 降级为 `no_matching_tools`
- 剔除的幻觉工具名写入 `missing_tools`

**输出 `DelegationResult`：**

| status | 含义 | context | CognitiveLoop 处理 |
|--------|------|---------|-------------------|
| `ok` | 有匹配工具 | DelegationContext（含 intent/hints/tools/return_mode） | 调用 WorkflowEngine.submit(context) |
| `no_matching_tools` | 工具库缺少所需工具 | None | fail——不是参数问题，直接告知用户 |
| `too_vague` | 意图过于模糊 | None | fail——请用户澄清，不反复猜测 |

**LLM 输出 JSON Schema**：

```json
{
  "status": "ok | no_matching_tools | too_vague",
  "reason": "判定理由",
  "tools": ["tool_name_1"],
  "summarized_context": "≤500字",
  "max_dag_nodes": 3,
  "max_mutations": 1,
  "max_total_tokens": null,
  "max_task_duration_s": null,
  "max_failure_count": null,
  "missing_tools": [],
  "suggestion": null
}
```

DelegationPreparer Prompt 全文见 architecture.md Section 9。

---

## 6. WorkflowMailbox —— submit/resume 的返回值

**单播。唯一消费者是 Agent。** WebUI 通过查询 API + SSE 拉数据。

```python
class WorkflowMailbox:
    task_id: WorkflowId

    async def get(self, timeout: float | None = None) -> WorkflowEvent | None: ...
    def get_nowait(self) -> WorkflowEvent | None: ...
    def count(self) -> int: ...

class WorkflowEvent:
    type: WorkflowEventType  # progress | approval_needed | completed | failed | cancelled | paused
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
class WorkflowStatus(StrEnum):
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
    # ── 已实现 ──

    async def get_task_summary(self, task_id: WorkflowId) -> WorkflowSummary | None: ...
    """查询任务摘要（✅ 已实现）。"""

    async def list_active_tasks(self) -> list[WorkflowSummary]: ...
    """列出所有活跃任务（✅ 已实现）。"""

    async def get_task_dag(self, task_id: WorkflowId) -> WorkflowDAG | None: ...
    """查询 DAG 图（✅ 已实现）。"""

    def get_task_status(self, task_id: WorkflowId) -> WorkflowStatus | None: ...
    """✅ 已实现——查询任务当前状态。任务不存在时返回 None。"""

    def get_progress(self, task_id: WorkflowId) -> WorkflowProgress | None: ...
    """✅ 已实现——查询执行进度。含 current_node_id/完成数/耗时/预估剩余时间。"""

    def get_token_usage(self, task_id: WorkflowId) -> TokenUsage | None: ...
    """✅ 已实现——返回完整审计日志（TokenComponent 列表）。"""

    def list_agents(self) -> AgentList: ...
    """✅ 已实现——当前活跃的 Sub-Agent 列表。v1 无 Worker Agent。"""

    def get_token_stats(self, period: TokenPeriod = TokenPeriod.TODAY) -> TokenStats: ...
    """✅ 已实现——按时间段/组件/模型聚合。"""
```

---

## 9. Push —— SSE 端点（Debug 专用）

由 `WorkflowDebugAPI`（`debug_api.py`）管理：500ms 轮询 WorkflowEngine → diff → SSE 推送。

**轮询懒启动（2026-09-12 起）**：`start()`（aiohttp on_startup 驱动）只标记就绪，**不**启动轮询；
首个 SSE 订阅者到达才拉起 `_poll_loop`，最后一个订阅者离开即停止并复位 diff 基线。
此前 `start()` 无条件拉起轮询，后端每次开机就开始 2Hz 空转——每 500ms 遍历全部
task × node_states、抽 graph edges、做整表 diff——**哪怕没有任何订阅者**（纯开销、零收益）。
`GET /debug/api/tasks`（一次性快照）不启动轮询。

```
GET /debug/api/tasks          → JSON 全量快照（WorkflowDebugAPI.snapshot_handler）
GET /debug/api/tasks/stream   → SSE 事件流（WorkflowDebugAPI.sse_handler）

SSE 事件类型：
  event: snapshot     data: [全量 tasks 数组]              ← 新客户端连接
  event: task_new     data: {task_id, status, nodes, ...}  ← 新任务
  event: task_update  data: {task_id, status, nodes}       ← 状态/进度变化
  event: task_done    data: {task_id, status, error}       ← 终态
  event: node_update  data: {task_id, node_id, status, attempt} ← 节点状态变化
  event: heartbeat    data: {}                             ← 30s 保活
```

使用模式：初始拉全量 DAG → 监听 SSE 增量 → 收到变更后按需 Fetch。

---

# 第三部分：辅助类型

> 上面所有方法签名中出现的类型汇总。按归属分组。与 schema/ 包同步。

---

## 10. 基础 NewType

```python
WorkflowId = NewType("WorkflowId", str)
"""任务唯一标识符。WorkflowEngine 内部生成，对外透明。NewType 防止调用方随意传 str。"""

AgentId = NewType("AgentId", str)
"""Agent 唯一标识符。WorkflowEngine 内部生成，用于 submit_approval 路由。"""

ApprovalId = NewType("ApprovalId", str)
"""审批请求唯一标识符。WorkflowEngine 内部生成，用于 cancel_approval 清理。"""

SessionId = NewType("SessionId", str)
"""会话唯一标识符。CognitiveLoop 管理，WorkflowEngine 仅用于日志关联和 Debug 追踪。"""

NodeId = NewType("NodeId", str)
"""DAG 节点唯一标识符。出现在 DAGNodeInfo、WorkflowProgress、TokenComponent 中。
调用方不需要自己构造——查询结果中包含，日志追踪用。"""

ArtifactId = NewType("ArtifactId", str)
"""产物唯一标识符。ArtifactStore 存储时的 key，与 DAGNode.output_as 对应。"""
```

---

## 11. 错误码

三层错误码模型：API 层 → 任务层 → 节点层。

```python
class SubmitRejectionCode(StrEnum):
    """submit() 同步拒绝码——5 项。参数校验不通过时返回。"""
    INVALID_PARAM = "invalid_param"
    PLAN_TOO_LARGE = "plan_too_large"
    CONTEXT_WINDOW_EXCEEDED = "context_window_exceeded"
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"
    NOT_READY = "not_ready"

class WorkflowErrorCode(StrEnum):
    """任务运行时错误码——10 项。WorkflowState.error_code + WorkflowEvent error_type。"""
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
    """全局约束配置（WorkflowEngine 构造时注入）。所有执行路径的硬天花板。"""
    max_total_tokens: int = 100_000       # 整个任务 LLM 调用总 token 上限
    max_context_tokens: int = 32_000      # 单次 prompt 最大 token 数
    max_failure_count: int = 5            # 连续失败 N 次后熔断
    max_dag_nodes: int = 20               # DAG 节点数上限
    max_retries_per_node: int = 3         # 单节点最大重试次数
    max_mutations: int = 10               # 敏感操作次数上限
    max_task_duration_s: float = 600.0    # 整个任务最大执行时间（秒）
    max_tool_error_chars: int = 2000      # 工具错误消息截断长度

class WorkflowConstraints:
    """任务级约束。与全局 ConstraintConfig 取 min。None 表示使用全局默认。"""
    max_dag_nodes: int | None = None      # DAG 节点数上限
    max_mutations: int | None = None      # 敏感操作次数上限
    max_total_tokens: int | None          # None=全局默认
    max_task_duration_s: float | None     # None=全局默认
    max_failure_count: int | None         # None=全局默认

    def resolve(self, global_: ConstraintConfig) -> WorkflowConstraints:
        """用全局 ConstraintConfig 填充所有 None 字段，返回一个新的 WorkflowConstraints。
        每个字段取 min(self.field, global_.field)，确保任务级约束不会超过全局天花板。"""

class DelegationContext:
    """status=ok 时传入。Planner 的工作说明书——所有信息在此，不分散传参。"""
    intent: str                          # LLM 输出的完整任务意图（非用户原始输入）
    hints: str | None                     # LLM 观察到的元信息（紧急程度、偏好等）
    tools: list[Tool]                     # 本次任务可用的工具列表（从全量工具中筛选）
    task_constraints: WorkflowConstraints     # 任务级限制（与 ConstraintConfig 取 min）
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
    missing_tools: list[str]            # no_matching_tools 时，缺少的工具名称
    suggestion: str | None               # too_vague 时，给用户的澄清建议
    token_usage: dict | None = None        # LLM 调用消耗: {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}
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
    task_id: WorkflowId
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
class WorkflowSummary:
    """任务完成/查询时的摘要视图。"""
    task_id: WorkflowId
    status: WorkflowStatus
    intent: str
    created_at: str                      # ISO 8601，对齐 WorkflowState.created_at
    duration_ms: int                     # 总耗时（预计算）
    nodes: dict                          # {"total": N, "completed": N, "failed": N, "skipped": N}
    tools_used: list[str]               # 审计：本任务调用了哪些工具名称
    token_usage: dict                    # {"planner": N, "executor": N, "evaluator": N, ...}
    token_total: int = 0                  # 任务总 token 消耗
    approvals: dict                      # {"requested": N, "approved": N, "denied": N}
    error: str | None                    # 任务级错误消息（来自 WorkflowState.error）
    error_code: WorkflowErrorCode | None     # 任务级错误码（来自 WorkflowState.error_code）
    replans: int

class WorkflowProgress:
    """单任务执行进度快照。"""
    task_id: WorkflowId
    status: WorkflowStatus
    current_node_id: NodeId | None
    completed_nodes: int                 # 已完成节点数
    total_nodes: int                     # 总节点数
    elapsed_ms: int
    estimated_remaining_ms: int | None   # None=无法估算

class WorkflowDAG:
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
    task_id: WorkflowId
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
    REFLECTOR = "reflector"        # TODO: v2 经验反思器
    SCHEDULER = "scheduler"        # TODO: v2 调度器
    SYNTHESIZER = "synthesizer"    # TODO: v2 结果合成器
    REPLANNER = "replanner"        # TODO: v2 独立 Replanner
    REDUCER = "reducer"            # TODO: v2 结果归约器

class TokenUsage:
    """单个任务的 token 消耗明细。"""
    task_id: WorkflowId
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

## 16. CognitiveLoop 侧类型（已迁移归位）

> 本节类型原为占位，已随契约层提取（Task 9 / migration.md）迁至真实归属，WorkflowEngine 只读消费，不再于本模块定义。

**UserProfile** — canonical home：`core/cognitive/user_profile.py`（`UserProfile`，Pydantic BaseModel，`model_config={"extra": "allow"}`）。DelegationPreparer 只读消费。已含真实字段：

```python
class UserProfile(BaseModel):
    preferences: str | dict[str, str] = ""   # 用户偏好（供集成测试，后续由 CognitiveLoop 填充）
    language: str = "zh"                     # 语言
    display_name: str = ""                   # 用户显示名（来自 config.agent.user_name）
```

**Tool** — canonical home：`Module/tool/tool_descriptor.py`（`Tool` 描述符）。WorkflowEngine 对 Module/tool 工具的轻量投影，仅持执行所需字段，参数 schema 由 Executor 运行时从 `ToolManager.definitions()` 获取：

```python
class Tool(BaseModel):
    name: str                                # 工具唯一标识符（与 ToolPlugin.name 对应）
    description: str = ""                    # 工具功能描述（Planner 注入 prompt）
    capabilities: list[str] = Field(default_factory=list)  # 能力标签（对齐 Module/tool.Capability）
```
