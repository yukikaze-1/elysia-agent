# TaskSystem 重构设计文档

> 日期: 2026-06-08  
> 状态: 设计完成，待实现

---

## 1. 背景与动机

### 1.1 现状问题

当前任务系统与认知循环紧耦合，存在以下结构性缺陷：

1. **阻塞式调用**：`CognitiveLoop.dispatch_tool_calls()` 内部 `asyncio.gather` 等待所有工具执行完毕才返回，期间 FSM 卡在 EXECUTING 状态，无法处理用户输入，体验上表现为"卡住"。
2. **无规划/重规划能力**：多步骤复杂任务全靠 LLM 的 ReAct 硬扛。没有 Planner、没有 Evaluator、没有失败后的 Replan，LLM 可能产生幻觉或不可行的调用链。
3. **任务系统与认知循环边界模糊**：`TaskScheduler` 被 `CognitiveLoop` 直接调用和阻塞等待，两个系统无法独立迭代。
4. **无结构化任务编排**：任务是扁平的列表，不支持依赖关系（DAG）、不支持运行时动态决策（DBT）。

### 1.2 设计目标

- 将 CognitiveLoop 和 TaskSystem 解耦为独立模块，各自迭代
- 引入 Plan-Execute-Evaluate-Replan 的完整任务生命周期
- 支持异步非阻塞的任务执行（Fire-and-Forget）
- 建立统一的安全护栏和权限模型
- 具备可观测性（Debug WebUI + 日志）

---

## 2. 总体架构

```
用户 ←→ 浏览器 ←→ 主 CognitiveLoop
                       ├─ 对话管理
                       ├─ LLM 工具选择 (submit_task → DelegationPreparer → TaskSystem.submit(context))
                       └─ 审批代理
                            │
                            │  submit_task 工具
                            ▼
                    ┌─────────────────────────────┐
                    │ TaskSystem                  │
                    │ Sub-Agent Pool (5-10)       │
                    │                             │
                    │  ┌───────────────────────┐  │
                    │  │ SubAgent (per task)   │  │
                    │  │  ┌─────────────────┐  │  │
                    │  │  │ 决策层           │  │  │
                    │  │  │ Planner          │  │  │
                    │  │  │ PlanValidator    │  │  │
                    │  │  └────────┬────────┘  │  │
                    │  │           │ Plan       │  │
                    │  │  ┌────────▼────────┐  │  │
                    │  │  │ 执行层           │  │  │
                    │  │  │ Orchestrator     │  │  │
                    │  │  │  ├ PlanGraph(NX) │  │  │
                    │  │  │  ├ Executor      │  │  │
                    │  │  │  │  └ WorkerPool │  │  │
                    │  │  │  └ Evaluator     │  │  │
                    │  │  └─────────────────┘  │  │
                    │  └───────────────────────┘  │
                    │                             │
                    │ ┌───────────────────────┐   │
                    │ │ Checkpoint + Artifact │   │
                    │ │ 经验记忆 + Reflector   │   │
                    │ │ 任务摘要存储           │   │
                    │ └───────────────────────┘   │
                    └──────────┬───────┬──────────┘
                               │       │
                        TaskMailbox   HITLNotifier
                        (进度+结果)   (审批快速通道)
                               │       │
                               ▼       ▼
                        ┌────────┐ ┌──────────┐
                        │ Agent  │ │ [实现者]  │
                        │ 单播消费│ │ 今天:EventBus
                        └───┬────┘ │ 将来:任意  │
                            │      └─────┬─────┘
                       OutputManager     │
                            │            │
                            ▼            ▼
                          WebSocket → 前端
```

**通信路径**：
- **进度/结果**：Orchestrator → TaskMailbox → Agent → OutputManager → WebSocket → 前端
- **审批收件箱**：Orchestrator → HITLNotifier（快通道，不绕 Agent）→ 前端收件箱列表
- **WebUI 查询**：前端 → `/debug/api/tasks` → TaskSystem 查询 API（拉模式，不消费 Mailbox）
- **编排循环**：SubAgent（决策层: Planner + PlanValidator）→ Orchestrator（执行层: PlanGraph + Executor + Evaluator）

---

## 3. 核心组件

### 3.1 主 CognitiveLoop（改造）

**职责**：对话管理 + 工具执行（含 `submit_task` 路由）+ 审批代理。

**任务路由机制——`submit_task` 工具**：

`submit_task` 注册为 LLM 可用工具，工具描述说明：
> 将复杂多步骤任务提交到后台异步执行。适用场景：涉及多步操作、状态变更、或长时间运行的任务。
> 简单单步操作（如单次搜索、单次文件读取）请直接用对应工具。

LLM 调用 `submit_task` → CognitiveLoop 工具执行器识别到此工具 → 路由到 `TaskSystem.submit()`。**零额外 LLM 调用、零额外字段、零分类逻辑**——LLM 选择工具本身就是复杂度判断。

```
用户: "帮我查最近三天天气，下雨的日子给老板发邮件"
LLM 思考 → 多步骤（搜索 + 条件判断 + 发邮件）→ 调用 submit_task(intent="...")
CognitiveLoop → 识别 submit_task → TaskSystem.submit(intent, ctx)
Agent → "好的，已经在后台执行了" → IDLE_BACKGROUND_TASK
```

**改造点**：
- 注册 `submit_task` 工具，工具执行器路由到 TaskSystem
- 实现 `DelegationPreparer`（一次 LLM 调用，准备干净的委派上下文）
- 新增 TaskMailbox 消费者（监听任务进度和结果）
- 实现 `HITLNotifier`（`EventBusHITLNotifier`），注入 TaskSystem

**状态机变化**：
- 发出任务后：SPEAKING（"好的，我这就帮你查询"）→ IDLE_BACKGROUND_TASK（后台有任务）
- 新增 `AgentState.IDLE_BACKGROUND_TASK`：Agent 可以对话，但后台有任务在执行
- 任务完成 → IDLE；新用户输入 → 正常处理（任务继续后台跑）
- 任务结果到达时从 IDLE 或 IDLE_BACKGROUND_TASK 触发新的认知回合

**异步任务结果处理**：当后台任务完成、触发认知回合时（`is_task_result=True`），不能用普通用户消息流水线——因为对话上下文可能已发生剧烈漂移（用户在等待期间聊了别的话题）。

必须注入专用的**任务唤醒模板**：

```
[系统通知]
你在 {N} 轮对话前委派了一个后台任务：
  任务ID: {task_id}
  原始意图: "{original_intent}"
  委派时间: {dispatched_at}
现在任务已执行完毕。以下是结果：

{task_result}

请忽略委派后这段时间内的闲聊内容，直接针对上述结果向用户汇报。
如果任务成功，总结关键发现。如果部分失败，说明哪些步骤未完成。
```

CognitiveLoop 对 `is_task_result=True` 的消息走此模板，不走普通对话流水线。

**渐进式唤醒（Soft Wake-up）**：直接甩出任务结果会显得生硬、丧失同理心。在注入模板前，先检查对话上下文：

```
1. 检查当前对话历史最后 1-2 轮：
   ├─ 用户在聊与任务无关的话题 → 不直接汇报，先礼貌插话
   └─ 对话已自然归位（用户没在说话）→ 直接汇报

2. 渐进式唤醒提示：
   "抱歉打断一下，您之前让我{original_intent}的任务已经有结果了，
    您看需要现在向您汇报，还是我们聊完眼前这个话题再说？"

3. 用户选择"现在汇报" → 注入完整任务唤醒模板
   用户选择"稍后再说" → 任务结果暂存，等待用户主动询问或对话空闲
```

**多任务并发唤醒锁 + 租约超时**：

```
active_wakeup_lock: Optional[str] = None  # 当前持有唤醒权的 task_id
pending_wakeups: list[WakeupEntry] = []  # 排队等待唤醒的任务

任务 X 完成 → 获取锁 → 渐进式唤醒用户
任务 Y 在 X 汇报期间完成 → 无锁 → 排队静默挂起
X 锁持有超过 3 轮对话或 2 分钟 → 租约超时 → 强制释放 → X 退回 pending_wakeups 队尾 → Y 出队
```

**紧急事件绕过锁**：`FAILURE_MELTDOWN`、高危变更被拦截等异常通知拥有 High Priority，直接绕过 `active_wakeup_lock` 强行打断提示，不排队。

这样既防止多任务撞车，又防止一个未处理的唤醒永久阻塞后续所有任务。

### 3.2 TaskSystem（新建）

主模块，对外提供统一 API。

```
Module/task_system/
├── __init__.py          # 公开 API facade
├── protocol.py          # HITLNotifier 等对外依赖协议（ABC）
├── delegation_preparer.py # DelegationPreparer: LLM 准备委派上下文（将来独立）
├── schema.py            # 基础 Pydantic 数据结构（所有子包共用）
├── infra.py             # AgentInfra + 共享基础设施（将来移出 TaskSystem）
├── mailbox.py           # TaskMailbox（单播，Agent 唯一消费者）
├── checkpoint.py        # 节点级 Checkpoint
├── pool.py              # Agent 并发控制（BoundedSemaphore）
├── summary_store.py     # 任务摘要存储（JSONL, TTL 7天）
├── core/                # Plan-Execute-Evaluate 编排循环
│   ├── sub_agent.py     # SubAgent: 决策层——规划/Replan/熔断
│   ├── orchestrator.py  # Orchestrator: 执行层——按 Plan 推进 DAG
│   ├── graph.py         # PlanGraph: NetworkX DiGraph 封装（图算法层）
│   ├── planner.py       # Planner: LLM → DAG 骨架 / DAGPatch
│   ├── plan_validator.py # PlanValidator: DAG 静态校验（纯逻辑）
│   ├── executor.py      # Executor: 拓扑排序 + DBT 生成
│   └── evaluator.py     # Evaluator: 双重判定
├── safety/              # 安全护栏（纯逻辑，无副作用）
│   ├── constraint.py    # 统一约束层
│   └── rbac.py          # RBAC 权限矩阵
├── learning/            # 经验记忆与学习（双向反馈闭环）
│   ├── reflector.py     # Reflector: 失败经验提炼
│   ├── experience_memory.py  # 任务经验记忆（独立于用户 LTM）
│   └── dedup.py         # 时间滑动窗口去重
├── tool_runner.py       # CPU 密集工具子进程入口
├── debug_api.py         # Debug WebUI 数据接口
└── __main__.py          # 独立运行测试
```

**子包职责**：

| 子包 | 职责 | 依赖 | 独立演化的方向 |
|------|------|------|---------------|
| `core/` | 任务编排循环 | `schema`, `mailbox` | 换编排模式（如 ReAct→Tree-of-Thought） |
| `safety/` | 约束检查 + 权限判定 | `schema`, `errors` | 换权限策略、加新约束规则 |
| `learning/` | Reflector + 经验记忆 + 去重 | `schema`, Milvus Client | 换学习范式（规则→LLM→RL） |
| `infra.py` / `mailbox.py` 等 | 基础设施、信箱、检查点、并发控制、摘要 | `schema` | 换存储后端（文件→Redis→数据库），将来移出 TaskSystem |

**schema.py** — 所有基础数据结构集中定义：

```python
# Plan / DAG / DBT 模型
class Plan(BaseModel): ...
class DAGNode(BaseModel): ...
class DBTNode(BaseModel): ...
class DBTNodeType(str, Enum): SEQUENCE, SELECTOR, PARALLEL, ACTION, CONDITION

# 上下文与结果
class DelegationResult(BaseModel): ...
class DelegationContext(BaseModel): ...
class TaskConstraints(BaseModel): ...
class TaskResult(BaseModel): ...
class NodeResult(BaseModel): ...
class TaskSummary(BaseModel): ...

# 通知
class TaskEvent(BaseModel): ...
```

> **注意**：`errors.py` 已合并到 `schema.py`。所有数据模型、状态码、错误码、枚举统一在 `schema.py` 中定义。不单独拆分 error 文件。

### 3.3 Sub-Agent

> **代码位置**：`Module/task_system/core/sub_agent.py`

**定义**：TaskSystem 池中的任务级执行单元。一个用户任务一个 Sub-Agent。

**决策层**——持有 Planner + PlanValidator，负责：
- 初始规划（`Planner.plan()`）
- Plan 校验（`PlanValidator.validate()`）
- Replan 决策 + 熔断检查
- 审批等待管理

**执行层**（通过 Orchestrator）——负责按 Plan 推进 DAG：
- 找 ready nodes → Executor 执行 → Evaluator 评估 → 更新状态
- 局部容错（节点级重试）
- 遇到结构性失败时返回 NEEDS_REPLAN，不自己调 Planner

**两层关系**：

```
SubAgent.run(context, mailbox)           ← 决策层
  ├─ Planner.plan(context) → Plan
  ├─ PlanValidator.validate(plan)
  └─ while True:                         ← 决策循环
       result = Orchestrator.run(plan)    ← 执行层（保持内部循环）
       if NEEDS_REPLAN:
           patch = Planner.replan(...)    ← 决策层调 Planner
           plan.apply_patch(patch)
           continue
```

大部分任务 Orchestrator 内部一次跑完，Sub-Agent 的 while 循环只跑一次。

**生命周期**：`AgentState` 原子替换。SubAgent wrapper 可复用（持有 `_infra`），但任务状态通过 `AgentState()` 新建 → `= None` 丢弃。绝不跨任务共享字段。见 Section 14。

### 3.4 Worker Agent

**定义**：DBT 执行引擎，**不是单工具调用器**。

- 接收一个 DAG Node（内含完整 DBT）
- 执行整棵行为树（可能多次工具调用、重试、切换策略）
- 不规划，不生成子 Agent
- 完成后返回节点结果，`WorkerState = None` 丢弃，wrapper 归还复用

| 属性 | Sub-Agent | Worker Agent |
|------|-----------|--------------|
| 规划能力 | ✅ Planner + DAG | ❌ |
| 评估能力 | ✅ Evaluator | ❌ |
| 调工具 | ✅ | ✅ |
| 生成 Agent | ✅ (生成 Worker) | ❌ |
| 池大小 | 5-10 (固定) | 10-20 (动态) |

**严格两层，禁止递归**。

---

## 4. 任务路由

### 4.1 `submit_task` 工具

LLM 通过**工具选择**来决定是否委派后台任务。不需要额外字段或分类逻辑。

`submit_task` 注册为 LLM 可用工具：

```json
{
    "name": "submit_task",
    "description": "将复杂多步骤任务提交到后台异步执行。"
                   "适用：涉及多步操作、状态变更、或长时间运行的任务。"
                   "简单单步操作请直接用对应工具（搜索、读文件等）。",
    "parameters": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "description": "完整任务意图描述，包含目标和约束"
            },
            "hints": {
                "type": "string",
                "description": "对任务执行有帮助的额外信息，"
                               "如用户情绪、紧急程度、偏好等"
            }
        },
        "required": ["intent"]
    }
}
```

```
用户: "现在几点了"
LLM → tool_call: get_time                              ← 不进 TaskSystem

用户: "帮我查最近三天天气，下雨的日子给老板发邮件"
LLM → tool_call: submit_task(intent="查最近三天天气...")   ← 路由到 TaskSystem
```

**为什么用工具而不是字段**：
- LLM 的工具选择本身就是准确的复杂度判断
- 不需要 prompt 标记、不需要解析、不需要 `complexity` 字段
- 工具描述清楚适用场景，LLM 自然知道什么时候该用
- 零额外 LLM 调用

### 4.2 闲聊/问候

所有消息统一走 CognitiveLoop LLM，无特殊处理。LLM 自行回复文本，不产生 tool_calls，不进 TaskSystem。

---

## 5. 任务编排

### 5.0 两层编排——Sub-Agent 与 Orchestrator

> **代码位置**：`Module/task_system/core/sub_agent.py`、`core/orchestrator.py`

编排分两层：Sub-Agent（决策层）和 Orchestrator（执行层）。

**为什么分两层**：

DAG 执行中的失败有两种——
- **局部可容错**（节点级）：网络抖动、重试成功。Orchestrator 内部消化。
- **结构性失败**（图级）：DAG 走不通了，必须修改 Plan 结构。这超出了"执行给定 Plan"的职责边界。

当 Plan 失效时，Orchestrator 不应自己调用 Planner.replan()——这违反单向依赖原则。改为返回中断信号，由 Sub-Agent 决策。

**职责分配**：

| | Sub-Agent（决策层） | Orchestrator（执行层） |
|---|---|---|
| 面向 | 任务 | Plan |
| 输入 | DelegationContext | Plan + Mailbox |
| 拥有 | Planner, PlanValidator | Executor, Evaluator |
| 职责 | 初始规划 → 校验 → 驱动执行 → Replan 决策 → 熔断检查 → 审批等待 | 找 ready → 执行 → 评估 → 更新状态 → checkpoint |
| 调 Planner | ✅ plan() + replan() | ❌ |
| 调 Executor/Evaluator | ❌ | ✅ |
| 返回 | 终态（SUCCESS/FAILED） | 中断信号（SUCCESS/FAILED/NEEDS_REPLAN/BLOCKED） |

**Sub-Agent 主循环**：

```
SubAgent.run(context, mailbox):
    plan = Planner.plan(context)
    PlanValidator.validate(plan)

    while True:
        result = Orchestrator.run(plan, mailbox)

        SUCCESS       → 终态，返回
        FAILED        → 终态，返回
        NEEDS_REPLAN  → 熔断检查
            ├─ 通过 → Planner.replan() → DAGPatch → 应用到 Plan → continue
            └─ 熔断 → FAILED
        BLOCKED       → 等待审批回传 → 恢复到 checkpoint → continue
```

大部分任务 Orchestrator 内部一次跑完，Sub-Agent 的 while 循环只跑一次。
只有在结构性失败或审批阻塞时循环多次。

**Orchestrator 内部循环**：

```
Orchestrator.run(plan, mailbox) → OrchestratorResult:
    while not terminal:
        1. get_ready_nodes(state)
        2. 审批检查
        3. execute_nodes(ready, state)   → Executor
        4. evaluate_node(result, state)  → Evaluator
        5. apply_evaluation(...)
           ├─ continue → mark success
           ├─ retry    → back to pending
           ├─ replan   → return NEEDS_REPLAN（不调 Planner！）
           ├─ ask_user → return BLOCKED
           └─ abort    → return FAILED
        6. save_checkpoint(state)
    return SUCCESS
```

**Orchestrator 依赖（6 个）**：infra, hitl_notifier, executor, evaluator, checkpoint_manager, artifact_store（注意：没有 Planner、PlanValidator——它们属于 Sub-Agent 层）。

**Sub-Agent 依赖（7 个）**：infra, hitl_notifier, planner, plan_validator, orchestrator, checkpoint_manager, artifact_store（Executor + Evaluator 在 Orchestrator 内部）。

**PlanGraph —— NetworkX 图存储层**

> **代码位置**：`Module/task_system/core/graph.py`

Plan 保持 Pydantic 模型（序列化 / checkpoint / API 不变），PlanGraph 包装 NetworkX DiGraph 提供图算法。两者互转：

```
Plan (Pydantic)              ← JSON 序列化、API、checkpoint 不变
  ↕ PlanGraph.from_plan() / to_plan()
PlanGraph (NetworkX DiGraph) ← 图算法：环检测、拓扑排序、ready nodes、patch
```

edges 方向：`dep → node`（前置节点必须先完成）。

| 方法 | 功能 | NetworkX 等价 |
|------|------|--------------|
| `get_ready_nodes(states)` | 前驱全 SUCCESS 的 PENDING 节点 | `G.predecessors(n)` + status check |
| `topological_order()` | Executor 的调度顺序 | `nx.topological_sort(G)` |
| `is_terminal(states)` | 所有 required 节点 SUCCESS？ | `G.nodes` 遍历 |
| `has_failed_required(states)` | 有 required 节点 FAILED？ | `G.nodes` 遍历 |
| `validate_structure()` | 环/依赖引用/孤立/不可达 | `nx.is_directed_acyclic_graph`, `nx.find_cycle`, `nx.descendants` |
| `apply_patch(patch)` | 增量修改 DAG | `G.add_node`, `G.remove_node`, `G.add_edge` |
| `to_dag_view(states)` | Debug WebUI 视图 | `list(G.edges)` |
| `compute_progress(states)` | 完成百分比 | `G.nodes` 遍历 |

### 5.1 DAG + DBT 双层架构

```
DAG (Plan)                     DBT (每个节点内部)
┌─────────────────┐            ┌──────────────────────┐
│ Step A (无依赖)   │            │ Sequence:             │
│ Step B (dep: A)  │            │   ├─ validate_input() │
│ Step C (dep: A)  │ ← 并行     │   ├─ transform()      │
│ Step D (dep: B,C)│            │   └─ output()         │
└─────────────────┘            │                       │
                               │ 失败 → Fallback →      │
                               │ 仍失败 → 上报 DAG      │
                               └──────────────────────┘
```

- **DAG**：LLM 产出目标拆解 + 步骤依赖。Executor 按拓扑排序调度。
- **DBT**：每个 DAG 节点的内部执行策略。Runtime 生成，动态决策。

> **⚠️ 实验性质**：DBT 是整套设计中最"重"的抽象层。实际 90% 的场景可以用简单的 `NodeConfig`（max_retries + fallback_tool + on_exhausted）覆盖。如果 DBT 在开发/使用中表现不佳（LLM 生成质量不稳定、调试困难、过度建模），降级方案为：去掉 DBT 概念，DAG Node 直接带 RetryPolicy。<br>
> 见 [22. 术语表] 中"降级路径"条目。

### 5.2 Planner

- 输入：用户意图 + 能力令牌 + 工具列表 + (可选) 经验记忆查询
- 输出：DAG 骨架（节点列表 + depends_on 依赖关系）
- 不填满细节——每个节点只声明目标和期望结果，DBT 由 Executor 运行时生成

### 5.3 Executor

- 接收 Plan → 拓扑排序
- 为每个节点生成 DBT（按需调用 LLM 或使用模板）
- **审批风暴检测**：执行前扫描所有节点 → 汇总 MUTATION 步骤 → 生成批量审批预览卡片
- 无依赖的节点分配 Worker Agent 并发执行
- **上下文传递**（push 模式）：调度节点 B 时，若 B 依赖 A，Executor 将 A 的 `NodeResult` 注入 B 的 context。Worker 不主动 pull，避免跨 Worker 状态共享
- 运行时窗口内多个 MUTATION 自动合并为批量审批

**DBT 模板**：常见节点类型有预定义模板，优先匹配模板（零 LLM 调用），匹配不上才调 LLM 生成：

| 模板 | 适用场景 | DBT 结构 |
|------|---------|---------|
| `retry-once` | 简单查询、可能偶发超时 | Sequence → Action → 失败 → Retry(1) |
| `search-then-fetch` | 先搜索再取详情 | Sequence → Action(search) → Action(fetch) |
| `transform-validate` | 数据处理 + 校验 | Sequence → Action(parse) → Condition(valid?) |
| `fallback-search` | 搜索可能无结果 | Selector → Action(primary) → Action(fallback_search) |

> **MVP 阶段**：可以先 stubb 模板匹配，全走 LLM 生成 DBT。模板是成本优化手段，非功能必需。

### 5.4 Evaluator

**双重判定** — 区分代码状态和业务状态：

```python
class NodeResult:
    execution_status: str   # "ok" | "timeout" | "error" | "permission_denied"
    business_status: str    # "goal_achieved" | "goal_partial" | "goal_failed"
    evidence: str           # 判定依据（工具返回了什么）
```

- **ExecutionStatus**：代码是否报错？工具是否正常返回？
- **BusinessStatus**：Plan 中声明的期望结果是否达成？

**例子**：搜索工具返回 `"抱歉，没有找到相关结果"`

| 层面 | 判定 | 原因 |
|------|------|------|
| ExecutionStatus | `"ok"` | HTTP 200，无异常 |
| BusinessStatus | `"goal_failed"` | 没拿到所需信息 |

Evaluator 的 LLM prompt 强制要求：**校验"是否达成了 Plan 中声明的期望结果"，而非仅检查有无异常**。

判定输出：
- `goal_achieved` → 节点成功
- `goal_partial` → 部分成功，标记 warning，后续节点可使用部分结果
- `goal_failed` → 上报 DAG 层 Replan
- 失败时携带诊断信息上报 DAG 层

### 5.5 双层错误恢复

```
DBT 层自愈
  ├─ 重试 (指数退避)
  ├─ 换参数
  ├─ 换策略 (Selector: 尝试下一个子节点)
  └─ 换工具 (Fallback)
      ↓ 仍失败
DAG 层 Replan
  ├─ 注入 Evaluator 诊断信息到 Planner prompt（防止用相同上下文产出相同坏计划）
  ├─ 局部修改子图 (替换/拆分/跳过该节点)
  ├─ 全局重规划
  ├─ 降级 (跳过可选步骤, 标记 partial)
  └─ 向用户求助 (TaskMailbox)
```

**Replan 时的 Planner 输入**：必须在原始规划 context 基础上追加 Evaluator 的失败诊断（哪个节点失败了、为什么、建议如何修复），否则 Planner 可能产出相同的坏计划。

---

## 6. 异步与通知

### 6.1 Fire-and-Forget 委托

CognitiveLoop 提交任务后立即返回 IDLE，不阻塞等待。FSM 自由接收用户输入。

### 6.2 TaskMailbox + HITLNotifier 双通道

TaskSystem 对外有两条独立通道，但**不是并发**——同一协程内顺序写入，Mailbox 先、HITLNotifier 后：

```python
# Worker 协程内，顺序执行（非并行）
Mailbox._push(approval_needed)                 # 1. 先写系统记录（本地队列，可靠）
HITLNotifier.request_approval(data)            # 2. 再推前端投影（外部通道，best-effort）
```

**为什么这个顺序：**
- Mailbox 是系统记录（本地 `asyncio.Queue`，可靠）
- HITLNotifier 是前端投影（可能失败——WebSocket 断开、EventBus 满等）
- Mailbox 先写成功 → 即使 HITLNotifier 失败，Agent 审计流仍检测到"有审批在等待"→ 可恢复："前端好像没收到审批卡片，重新推送"

| 通道 | 传输内容 | 消费模式 | 可靠性 |
|------|---------|---------|--------|
| TaskMailbox | 进度更新、审批审计、任务完成/失败 | 单播（唯一消费者: Agent）| 可靠，本地队列 |
| HITLNotifier | 审批请求推送到前端收件箱 | 协议（实现者注入）| best-effort，Agent 审计流兜底 |

**为什么审批需要两条路径？**
- **Mailbox（审计流 + 可靠性锚点）**：CognitiveLoop 感知任务状态。如果 HITLNotifier 投递失败，Agent 从 Mailbox 检测到 `approval_needed` 后可触发重投。
- **HITLNotifier（快通道）**：直推前端收件箱，不绕 Agent，审批延迟最低。

**HITLNotifier 失败处理：**
TaskSystem 侧保证 `request_approval` / `cancel_approval` 不抛异常（catch + log）。
- 可恢复失败（EventBus 满、WS 短暂断开）→ Agent 审计流检测到 `approval_needed` 后重投
- 不可恢复（EventBus 挂了、前端离线）→ Agent 启动定时器，30s 后仍未审批则通过 OutputManager 温和提醒用户
- 这是 CognitiveLoop/Agent 侧的事，TaskSystem 不参与

**cancel_approval 批量清理：**
`cancel_approval(approval_ids: list[ApprovalId])` 接受 ID 列表。
主要场景——`cancel(task_id)` 时该任务所有 pending approval 一次性清理，
避免用户审批一个已取消的任务。

**查询 API 与 Mailbox 的关系：** TaskMailbox 是 Push（实时事件流，跟踪单个任务），查询 API 是 Pull（快照，看全局）。Debug WebUI 用查询 API 拉 DAG 图/池使用率/Token 统计等结构化视图，不消费 Mailbox。两者互补，无重叠。

**审批模式：收件箱，非弹窗**

审批请求进入前端收件箱（持久列表），不是模态弹窗。Worker 提交审批后 `await` 等待，不归还池——等待中的协程不消耗 CPU/LLM/IO 资源。**无超时**，用户散步回来审批还在。

```
Worker 遇到 MUTATION
  ├→ HITLNotifier.request_approval(data) → 前端收件箱
  └→ await approval_future              ← 挂起等待。不归还池。
  
用户随时处理审批
  → approved=True  → Worker 继续执行 MUTATION
  → approved=False → Worker 收到 PERMISSION_DENIED → Evaluator → 三层降级
```

**为什么不用 EventBus 传任务数据？**
- EventBus 是 Agent 内部的控制信号通道（INTERRUPT、STATE_CHANGE）
- 任务数据量大（Plan + 多步工具输出 + 错误信息），不适合混入控制通道
- TaskMailbox 消费者只有一个（Agent），不存在多方分流

**HITLNotifier 协议**：TaskSystem 不依赖 EventBus。定义在 `Module/task_system/protocol.py`。CognitiveLoop 重构时只需换实现，TaskSystem 一行不改。

### 6.3 软 INTERRUPT（二次确认 + 优雅停止）

```
用户明确表达打断意图（"停"、"别干了"、"先帮我做别的"），非普通对话输入
  → task.status = PAUSING（立即！不等用户回复）
      └─ Executor 在调度下一个 DAG 节点或执行下一个工具前必须检查此标签
         若 PAUSING → 挂起等待，不产生新的输出/审批/进度事件
  → Agent 回复："你有一个后台任务正在执行，确定要取消吗？"
  → 用户确认"是，取消" → 启动软取消
  → 用户说"不用取消" → task.status 恢复，Agent 回到 IDLE_BACKGROUND_TASK
```

**普通输入不触发 PAUSING**：用户正常聊天、提问不应暂停后台任务。只有明确的打断意图（关键词匹配或 LLM 意图识别为 INTERRUPT）才触发。

**在途事件拦截**：PAUSING 瞬间可能已有事件在队列中。`_dispatch_task_event` 消费时必须二次校验：

```
_dispatch_task_event(msg):
    task = get_task(msg.task_id)
    if task.status == PAUSING and msg.type not in ("cancelled",):
        # 拦截所有在途事件，包括 progress、approval_needed、completed
        # approval_needed 被拦截避免了取消确认弹窗与审批卡片同时出现
        暂存到 per-task 拦截缓冲区
    else:
        正常处理

用户选"不取消" → 重放缓冲区
用户选"取消" → 遍历缓冲区中所有 `approval_needed` 事件
  → 向对应的 Worker Future 灌入 `CancelledError` 或 ABORT 信号
  → 强制唤醒并终止后台悬挂等待的 Worker
  → 清空缓冲区
```

**为什么需要 PAUSING**：确认弹窗期间，后台任务仍在运行。如果不暂停，极短时间窗内可能：
- 任务刚好完成 → `completed` 事件冲掉确认弹窗上下文
- 任务触发新的 MUTATION → HITL 审批卡片与取消确认弹窗同时出现 → 状态机卡死

```
软取消流程（用户确认后才触发）：
  → task.status = CANCELLING
  → 不再接受新的 user input 给此任务
  → 当前 DAG 节点执行到安全停止点（不是立即杀）
  → 启动硬超时看门狗：asyncio.wait_for(worker_cleanup(), timeout=5.0)
  → 5s 内收到 ACK → 正常保存 checkpoint → 清理 → CANCELLED
  → 5s 超时（工具卡死/死循环/无超时网络请求）：
      → 协程模式：强制 task.cancel() 引发 CancelledError
      → 进程模式：ProcessPoolExecutor 直接 SIGTERM
  → task.status = CANCELLED_FORCE（或仍为 CANCELLED）
```

**两层含义**：
1. **二次确认**：防止用户只是临时插话，任务误杀
2. **软中断**：确认取消后也不是硬杀，而是优雅停止 → checkpoint → 清理

即使确认取消，也不会强行 `task.cancel()`，保证数据完整性。

### 6.4 用户主动暂停/恢复

用户也可以在任务正常运行时主动暂停（非打断意图）：

```
用户说"先停一下"
  → task_system.pause(task_id)
  → RUNNING → PAUSE_REQUESTED → 当前节点执行完 → 写 checkpoint → PAUSED
  → task_system.resume(task_id)
  → PAUSED → RESUMING（CAS 防双 resume）→ RUNNING
```

> v1 限制：`resume()` 使用原始 DelegationContext，不支持注入新上下文。如需修改任务参数，必须 cancel → 重新 submit。

---

## 7. 安全模型

### 7.1 RBAC 工具权限矩阵

工具注册时声明权限级别，非运行时猜测：

| 级别 | 含义 | 示例 | HITL |
|------|------|------|------|
| `READ_ONLY` | 只读 | search, memory_search, read_file | ❌ 完全自主 |
| `SAFE_WRITE` | 安全写入 | 任务缓冲区、临时文件 | ❌ 条件自主 |
| `MUTATION` | 外部变更 | 发邮件、执行脚本、删配置 | ✅ 必须审批 |

Worker 执行到 MUTATION 工具时：
```
Worker 挂起
  ├─→ HITLNotifier.request_approval(data)   ← 快通道: 审批进前端收件箱
  └─→ await approval_future                 ← Worker 等待。不归还池，不超时

用户处理审批:
  ├─ 批准 → submit_approval(task_id, agent_id, approved=True)
  │         → Worker 继续执行 MUTATION
  └─ 拒绝 → submit_approval(task_id, agent_id, approved=False, feedback=...)
            → Worker 收到 PERMISSION_DENIED → Evaluator → 三层降级

Worker 等待期间: 不消耗 CPU/LLM/IO。前端收件箱持续展示待审批列表。
无超时——用户散步回来审批还在。
```

### 7.2 审批风暴防护

**第一层：Plan 预审批**
Executor 执行前扫描所有 DAG 节点 → MUTATION 步骤汇总为批量预览卡片 → 用户一次性批准。

**第二层：运行时窗口合并**
多个 Worker 在短时间窗口内同时触发 MUTATION → 自动合并为一张批量卡片。

### 7.3 审批拒绝处理（三层降级）

```
用户拒绝审批
  → L1: Evaluator → DAG Replan 换替代方案
      ├─ 找到替代 → 用新方案继续
      └─ 无替代 → L2
  → L2: 检查 required 属性
      ├─ required=false → 跳过，结果标记 partial
      └─ required=true → L3
  → L3: 通过 TaskMailbox 向主循环求助
      → 主循环转发给用户，等待决策
```

被拒操作写入经验记忆，同一任务内相同操作不再重复申请。

### 7.4 审批收件箱 UI

审批不弹窗打断用户，而是进入**收件箱列表**（类似通知中心）：

- **持久展示**：待审批项以列表形式常驻在聊天界面侧边栏或顶栏
- **无超时消失**：审批卡片不自动消失，用户随时处理
- **批量审批**：多条 MUTATION 可选中后一次性批准/拒绝
- **Plan 上下文**：每张卡片显示步骤在整体计划中的位置（Step 2 / 5）
- **风险摘要**：操作类型、影响范围、参数预览
- **空闲提醒**：用户下次发送消息时，如果有待审批项，Agent 温和提醒"你有 N 条审批等待处理"

### 7.5 沙箱限制

- 文件写入限定目录
- 网络访问白名单
- 进程执行白名单

---

## 8. 统一约束层

所有执行路径（简化池 / Sub-Agent / Worker Agent）强制遵守：

| 约束项 | 说明 | 硬/软 |
|--------|------|-------|
| Token 消耗阈值 | LLM 调用总 token 上限 | 硬 |
| 上下文窗口上限 | 单次 prompt 最大长度 | 硬 |
| 失败次数熔断 | 连续失败 N 次后终止 | 硬 |
| 任务链路最大长度 | DAG 节点数上限 | 硬 |
| 重试次数上限 | 单工具/单节点最大重试 | 硬 |
| 敏感操作次数阈值 | MUTATION 操作频率限制 | 硬 |
| 工具错误信息长度 | 错误消息截断上限 | 软 |
| 运行时间硬超时 | 整个任务最大执行时间 | 硬 |

### 8.1 约束违规处理

每个约束有对应的违规动作。

**关键区分：资源耗尽型 vs 逻辑可恢复型**。

资源耗尽型错误（Token 耗尽、上下文窗口超限、运行时间硬超时、上下文窗口上限）**不可触发 Replan**——因为 Replan 本身也要消耗 LLM token，会陷入"截断 → Replan → 再截断 → 再 Replan"的死循环。这类错误必须立即终止 DAG，跳过 Evaluator，直接返回 FAILURE_MELTDOWN。

| 约束项 | 类型 | 违规动作 |
|--------|------|---------|
| Token 消耗阈值 | **资源耗尽** | 立即停止 DAG → 保存 checkpoint → 跳过 Evaluator/Replan → FAILURE_MELTDOWN |
| 运行时间硬超时 | **资源耗尽** | 立即停止 DAG → 保存 checkpoint → 跳过 Evaluator/Replan → FAILURE_MELTDOWN |
| 上下文窗口上限 | **资源耗尽** | Planner 阶段拒绝 Plan → 返回错误"任务超出上下文窗口，请拆分" |
| 失败次数熔断 | **资源耗尽** | 立即终止任务 + 写 checkpoint + FAILURE_MELTDOWN |
| 任务链路最大长度 | 逻辑可恢复 | Planner 阶段拒绝 Plan → 返回错误"步骤过多，请简化任务" |
| 重试次数上限 | 逻辑可恢复 | 标记节点 FAILED + 上报 DAG 层 Replan |
| 敏感操作次数阈值 | 逻辑可恢复 | 拦截当前 MUTATION + 通知用户"已达操作频率上限，请稍后重试" |
| 工具错误信息长度 | 逻辑可恢复 | 截断错误消息到上限字符数，末尾追加 `[truncated]` |

**Token 预扣款机制**：

**两层配额**：Planner 生成 Plan 时，为整个 DAG 预估"大池配额"一次性从全局扣除。Worker 只争任务内部私有池，跨任务隔离，杜绝跨任务死锁。

```
全局 Budget
  └─ Task-A 大池 (Planner 预估并一次性扣除)
       ├─ W1 reserve(2000) → 批准
       ├─ W2 reserve(2000) → 排队等 W1 或 W3 归还
       └─ W1 追加申请(1500) → 同任务内追加优先，允许授信透支
```

```
Worker --> TaskBudget.reserve(estimated_tokens, priority, timeout=5.0)
  ├─ 配额充足 → 批准
  ├─ 同任务追加申请 → 优先于初次申请（让跑了一半的节点先完结）
  ├─ 同任务内 → 允许授信透支（任务结束时统一结算）
  └─ 配额不足 + timeout 到期 + 非追加 → FAILURE_MELTDOWN
```

**动态估算**：DBT 引擎根据任务类型估算：

| 任务类型 | 预扣估算 |
|---------|---------|
| 分类/判断 | 200 tokens |
| 简短生成 | 1000 tokens |
| 代码生成 | 2000 tokens |
| 不确定 | `max_output_tokens // 2` |

**破死锁规则**：同任务内的追加申请优先于初次申请，且可授信透支到任务结束结算。这防止了 W1（已跑一半等追加）和 W2（等初次）互锁。

**FAILURE_MELTDOWN 路径**：
```
资源耗尽触发 → DAG 停止 → checkpoint 封存 → TaskMailbox 通知主循环
  → 主循环通知用户："任务因资源不足而终止，已完成步骤 {X}/{Y} 已保存"
```

---

## 9. 上下文传递 —— DelegationPreparer

### 9.1 架构

**DelegationPreparer** 代码在 `Module/task_system/delegation_preparer.py`，与 TaskSystem 共享类型。
但调用者是 CognitiveLoop——它在提交任务前先调用 `prepare()`。

```
CognitiveLoop LLM 调用 submit_task(intent, hints)
  → DelegationPreparer.prepare(intent, hints, session_messages, user_profile, tools, constraints)
  → DelegationResult(status, context?, ...)
      context 已包含 intent/hints，不再分散传参
  → 如果 ok → TaskSystem.submit(context)
  → 如果 not ok → 直接返回错误给用户

将来 DelegationPreparer 可独立为单独的 delegation 服务。
```

### 9.2 DelegationPreparer

**输入（全部来自 CognitiveLoop 已有的数据，不访问数据库）：**

| 输入 | 来源 | 说明 |
|------|------|------|
| `intent` | LLM 输出 | 用户完整任务意图。**写入 DelegationContext，Planner 直接消费** |
| `hints` | LLM 输出 | 元信息（紧急程度、偏好等）。**写入 DelegationContext，Planner 直接消费** |
| `session_messages` | SessionMemory | **全部对话消息（不截断）** |
| `user_profile` | CognitiveLoop (`UserProfile`) | 用户画像：偏好、禁止事项、联系方式。CognitiveLoop 管理，DelegationPreparer 只读消费 |
| `tools` | ToolRegistry (`list[Tool]`) | 全量工具列表。CognitiveLoop 管理，占位类型 |
| `global_constraints` | ConstraintConfig | TaskSystem 全局约束（天花板）|

**输出 —— DelegationResult：**

| status | 含义 | context | CognitiveLoop 处理 |
|--------|------|---------|-------------------|
| `ok` | 有匹配工具，可以委派 | DelegationContext（含 intent/hints） | 调用 TaskSystem.submit(context) |
| `no_matching_tools` | 工具库缺少所需工具 | None | 告诉用户：缺少 X 能力 |
| `too_vague` | 意图过于模糊 | None | 让用户澄清 |

**四个职责（仅 status=ok 时产出）：**

1. **意图透传**：`intent` 和 `hints` 原样写入 DelegationContext。不修改——DelegationPreparer 不做意图改写，只做工具筛选 + 上下文整理。
2. **工具筛选**：从 tools 中选出相关工具名（字符串列表，如 `["web_search", "send_email"]`）。后处理校验剔除 LLM 幻觉出的不存在的工具。
3. **上下文摘要**：从完整对话历史中提取相关信息。解析指代（"那个方案"），提取相关偏好。
4. **任务级约束**：
   - `max_dag_nodes`：估计 DAG 步数。简单查询 3，复杂任务 10。
   - `max_mutations`：估计敏感操作次数。

**`session_id`** 字段从 CognitiveLoop 直接透传，不做处理。用于日志关联和 Debug 追踪。

**边界条件：**
- `no_matching_tools`：工具库缺少所需类别。返回 `missing_capabilities` 告知缺什么。
- `too_vague`：意图无法确定工具。建议用户澄清。
- 后处理校验：LLM 可能幻觉出不存在的工具名，`_validate_output()` 剔除它们。如果剔除后为空 → `no_matching_tools`。

**DelegationPreparer 不访问数据库。** 它只处理 CognitiveLoop 已经持有的数据。

### 9.3 与 Planner 的关系

| | DelegationPreparer | Planner |
|---|---|---|
| **职责** | 范围界定 + 可行性判断 | 结构规划 —— "具体怎么做？" |
| **输入** | intent + 全量对话 + 全量工具 | intent + 精选工具子集 + 上下文摘要 + 约束 |
| **产出** | DelegationResult（可能拒绝）| DAG（节点 + 依赖 + 期望结果） |
| **会说"不"** | ✅ no_matching_tools / too_vague | ✅ FAILURE_MELTDOWN（资源耗尽） |

**DelegationPreparer 不是迷你 Planner。** 它不规划步骤，不设计流程。它划定范围并判断可行性。

**约束冲突处理：** DelegationPreparer 的 `max_dag_nodes` 和 `max_mutations` 是 Planner 的硬边界。Planner 的 system prompt 中约束作为不可违反的限制声明。

### 9.4 两层约束

```
全局约束 (ConstraintConfig)      ← TaskSystem 构造时注入，硬天花板
  └─ 任务约束 (TaskConstraints)   ← DelegationPreparer 本次设置，更严格
       └─ 取 min(全局, 任务) 作为实际生效值

任务约束只设两个值（其余用全局就够了）:
  - max_dag_nodes: DAG 复杂度上限
  - max_mutations: 敏感操作上限
```

例：全局 `max_dag_nodes=20`，DelegationPreparer 判断这是简单查询 → 任务 `max_dag_nodes=3`。实际生效 `3`。

---

## 10. 恢复与持久化

### 10.1 节点级 Checkpoint

每个 DAG 节点完成后立即写检查点：

```json
{
  "task_id": "xxx",
  "plan": {...},
  "completed_nodes": ["A", "B"],
  "current_node": "C",
  "pending_nodes": ["D", "E"],
  "pending_approvals": [
    {
      "approval_id": "appr-001",
      "node_id": "D",
      "tool_name": "send_email",
      "params": {"to": "boss@example.com", "body": "..."},
      "requested_at": "2026-06-09T..."
    }
  ],
  "node_results": {
    "A": {"status": "completed", "output": {...}},
    "B": {"status": "completed", "output": {...}}
  },
  "timestamp": "2026-06-08T..."
}
```

**写入策略**：write-to-tmp + atomic rename（与现有 `TaskScheduler._save_checkpoint()` 同模式，`core/task_scheduler.py:394`），进程中途崩溃不留下损坏文件。

恢复时：
- `pending_approvals` 非空 → HITLNotifier.request_approval() 重新推送审批卡片到前端收件箱
- `completed_nodes` 结果直接复用
- 从 `current_node` 继续执行节点需幂等设计。

**MUTATION 节点的特殊处理**：MUTATION 操作（发邮件、调外部 API）天然非幂等——重复执行可能产生副作用（重发邮件、重复扣款）。策略：

```
MUTATION 节点执行流程：
  1. 执行前先写 checkpoint：node.status = "executing" + idempotency_key
  2. 执行 MUTATION（带上 idempotency_key 以便外部系统去重）
  3. 写 checkpoint：node.status = "completed"

崩溃恢复时：
  - node.status = "pending" → 正常执行
  - node.status = "completed" → 跳过
  - node.status = "executing" → 不确定性状态！
      → 跳到下一节点（绝不重试 MUTATION）
      → 标记 node.status = "unrecoverable"
      → 通知用户："步骤 X 可能已执行（idempotency_key: xxx），请手动确认"
```

**级联防护**：MUTATION 节点被标记为 `unrecoverable` 后，Scheduler 不能盲目跳到下一个依赖它的节点（如下游节点需要该 MUTATION 的输出，会直接 KeyError 崩溃）。必须阻断所有依赖子图：

```
MUTATION 节点 unrecoverable → 阻断所有 depends_on 包含该节点的下游子图
  → TaskMailbox 发送 HITL_RECOVERY_REQUEST：
      "步骤 C（创建虚拟机）可能已执行，请确认：
       [已成功] → 补录完整 NodeResult（结构化 JSON，含 server_id、IP、网络配置等该节点本应产出的所有派生字段）
               → 节点状态 unrecoverable → 补录版 completed
               → 解除阻断 → 下游看到完整上下文
       [未执行] → 重试
       [放弃整个任务]"

关键：不能只补录一个值。下游节点需要上游产出的完整 NodeResult，缺字段会二次崩溃。
```

核心原则：**从不重复执行**状态不确定的 MUTATION。宁可漏执行，不重复执行。且绝不能把不确定性传递给下游节点。

> **TODO**：此策略缩小了风险窗但未完全消除（checkpoint 写入与 MUTATION 执行之间仍有时序间隙）。后续需结合外部系统的幂等接口（如支付平台的去重 ID）实现端到端的 exactly-once 语义。

> **TODO (v2)**：DBT 内部 checkpoint。当 DBT 包含多步工具调用且单步耗时较长时，中断后从 DAG 节点头重新执行浪费大。v2 考虑 DBT 节点数 > N 时自动开启 DBT 内部 checkpoint。

### 10.2 残留结果 TTL

被取消任务的节点结果设置 TTL（默认 1h），过期自动清理并记录日志。

---

## 11. 记忆与学习

### 11.1 任务经验记忆

TaskSystem 专属，独立于用户 LongTermMemory（同一 Milvus 实例，不同 namespace）。

```json
{
  "tool_name": "web_search",
  "error_pattern": "timeout after 30s",
  "params_snapshot": {"query": "very long query with many filters"},
  "context_snapshot": {
    "task_intent": "查询最近三天天气",
    "dag_position": "Step 1 / 3",
    "preceding_results": ["..."],
    "timestamp": "2026-06-08T..."
  },
  "diagnosis": "查询太复杂，搜索引擎超时",
  "fix": "拆分为多个简短查询",
  "confidence": 0.87,            # 置信度，0~1。初始低，验证后升高
  "verified_count": 3,           # 此经验被成功应用的次数
  "success_after_apply": 0.75,   # 应用后成功的比例
  "status": "verified",          # "unverified" | "verified" | "deprecated"
  "occurrences": 5,
  "last_seen": "2026-06-08T..."
}
```

**Planner 查询过滤**：仅 `confidence > 0.8 AND status = "verified"` 的经验参与规划决策。低置信度和 unverified 经验仅展示在 Debug 面板，不直接影响决策，防止错误经验自我强化。

`context_snapshot` 保留当时的原始上下文，让 AI 可以复盘"当时在干什么、为什么失败"。

> **写入前必须 stringify**：不要直接把带复杂嵌套结构的 dict 写入向量库。写入前通过轻量模型将其总结为结构化纯文本描述（例："在执行 [查询天气] 的第 2 步时，因为 [参数缺少日期] 导致了 [API 400 错误]"）。纯文本在 Cosine Similarity 检索时比裸 dict 准确得多。

### 11.2 Reflector 反馈闭环

```
工具失败
  → Reflector 分析原因 + 提出修复
  → 内存去重层（先于 Milvus）：
      ├─ 时间滑动窗口（OrderedDict + 10s TTL）：缓存最近错误 hash
      ├─ 新错误 hash 命中窗口 → 计数累加（高频同类错误去重）
      ├─ 过期 key 自动弹出（10s 后 Milvus 索引已对检索可见）
      └─ 合并后：定时（任务结束/Checkpoint 时）批量 stringify 写入 Milvus
  → Milvus 持久化
  → Planner 规划时查询已知失败模式（查 Milvus）
  → Evaluator 评估时对照已知问题
  → 持续改进闭环
```

**为什么需要内存去重层**：向量数据库有写入可见性延迟。并发 Worker 在几毫秒内连续报出同类错误时，后写入的 Worker 检索"是否有相似错误"可能还查不到刚写入的记录，导致去重失败、大量重复记录塞满向量库。时间滑动窗口在内存层完成毫秒级去重，10 秒过期自动弹出（此时 Milvus 索引已可见）。

**不用布隆过滤器**：布隆过滤器只能添加不能删除，长时间运行后位阵列饱和，假阳性率接近 100%，会将所有新错误误判为"已见过"。

---

## 12. 工具改进

### 12.1 参数验证（两层）

```
Layer 1: Pydantic Schema 校验
  → 类型检查、必填字段、格式验证
  → 由 ToolPlugin.param_model 自动执行
  → JSON 解析失败 / 类型不匹配 → 返回结构化错误（含字段名和期望类型）

Layer 2: 工具内部 validate() 业务规则校验
  → 合法值范围、业务约束、参数组合逻辑
  → 例如：limit 必须在 1-100 之间；file_path 必须在沙箱内
  → 返回结构化错误（含解决建议，如"limit 有效范围为 1-100"）
```

两层校验在 TaskSystem 侧执行。参数/JSON 错误在工具层被拦截，不传入实际执行。

### 12.2 执行模式

```python
class ToolPlugin:
    execution_mode: str = "coroutine"  # "coroutine" | "process"
```

- `coroutine`（默认）：I/O 密集，asyncio 直接执行
- `process`：CPU 密集（正则、大 JSON 解析），按需 `create_subprocess_exec`，用完即毁

### 12.3 CPU 密集工具执行：按需子进程

不用 `ProcessPoolExecutor`（常驻进程吃内存 + `future.cancel()` 不能硬杀已在跑的进程 + Pickle 序列化炸弹）。改为**随用随建，用完即毁**：

```python
async def execute_process_tool(tool_name: str, params: dict, timeout: float):
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "Module.task_system.tool_runner",
        "--tool", tool_name,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=os.setsid,  # 独立进程组，确保 killpg 能杀整棵进程树
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=json.dumps(params).encode()),
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise ToolError(stderr.decode())
        return json.loads(stdout)
    except asyncio.TimeoutError:
        # killpg 一网打尽整棵进程树（工具内部可能 subprocess.run 产生了孙子进程）
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
        raise
```

**对比**：

| | ProcessPoolExecutor | 按需子进程 |
|---|---|---|
| 进程生命周期 | 常驻，吃内存 | 随用随建，用完即毁 |
| cancel 真杀 | ❌ 返回 False，杀不了 | ✅ `proc.kill()` SIGKILL |
| 序列化 | Pickle（Pydantic 炸） | JSON stdin/stdout |
| 资源泄露 | 高（进程卡死残留） | 零（用完销毁） |
| 启动开销 | 低（预热好） | 每次 fork+exec |

Demo 阶段 CPU 密集工具少、频率低，启动开销可忽略。真杀 > 预热。

**v1 限制**：子进程无法访问主进程的 asyncio.Queue。进程模式工具只返回最终结果，不支持中途 `emit.progress()`。

**大结果走文件**：`proc.communicate()` 在内存中完整缓存 stdout。若工具返回 100MB+ 的大 JSON，5 个并发 Worker 会触发 OOM Killer。工具输出超过阈值（1MB）时必须写入沙箱临时文件，stdout 仅返回 `{"result_file": "/sandbox/tmp/res_X.json"}`，主进程流式读取。

**安全边界**：
- 子进程以 Agent 进程用户身份运行（非 root）
- 文件访问限定沙箱目录（通过参数传入，工具内部校验）
- 网络访问白名单（通过环境变量 `ALLOWED_HOSTS` 传入子进程）
- 超时 `proc.kill()` SIGKILL 硬杀，零残留
- 参数通过 JSON stdin 传入（无 Pickle 序列化问题）

---

## 13. 池化策略

**池化的是基础设施，不是 Agent 实例。** Sub-Agent/Worker Agent 每次任务新建，任务结束销毁。`BoundedSemaphore` 控制并发数，但不复用 Agent 对象。

| 池 | 大小 | 策略 | 说明 |
|----|------|------|------|
| Sub-Agent 并发限制 | 5-10 | BoundedSemaphore | 限制同时运行的任务数，天然限流 LLM API |
| Worker Agent 并发限制 | 10-20 | 按 DAG 并行度动态 | 无依赖节点并发 |
| LLM Client (httpx) | 自动 | 连接池复用 | 跨所有 Agent 共享 |
| Milvus Client | 1 | 单例 | 跨所有 Agent 共享 |

---

## 14. Sub-Agent 生命周期与摘要

### 14.1 状态管理：AgentState 原子替换

```python
@dataclass
class AgentInfra:
    """跨任务共享的底层资源。创建成本高、不含任务状态、协程安全。"""
    llm: AsyncOpenAI
    milvus: MilvusClient
    http: httpx.AsyncClient
    tool_registry: ToolRegistry
    constraints: ConstraintConfig

@dataclass
class AgentState:
    """每个任务独有的状态。新建 → 丢弃，绝不跨任务复用。"""
    plan: Plan | None = None
    dag_nodes: dict[str, DAGNode] = field(default_factory=dict)
    dbts: dict[str, DBTNode] = field(default_factory=dict)
    current_node: str | None = None
    tool_cache: dict = field(default_factory=dict)
    context: DelegationContext | None = None
    node_results: dict[str, NodeResult] = field(default_factory=dict)

class SubAgent:
    def __init__(self, infra: AgentInfra):
        self._infra = infra            # 跨任务共享，永不重置
        self._state: AgentState | None = None

    def assign_task(self, ctx: DelegationContext) -> None:
        self._state = AgentState(context=ctx)  # 全新对象

    def complete_task(self) -> None:
        self._state = None  # GC 一把回收，零泄漏
```

**三个保证**：

| 保证 | 机制 |
|------|------|
| 不会漏字段 | `AgentState(...)` 是 dataclass，所有字段必须初始化，编译器级别保证 |
| 不会忘重置 | `self._state = None` 是一行，不存在"忘了清某个字段"的问题 |
| 不会跨任务污染 | 新任务 = 新 AgentState 实例，旧实例被 GC 完整回收 |

### 14.2 任务完成后

Sub-Agent 完成（或失败/取消）后：
1. **丢弃状态**：`self._state = None`，wrapper 归还，可承接下一任务
2. **生成任务摘要**：轻量日志保留在 TaskSystem 内部（非 Milvus），TTL 默认 7 天

```json
{
  "task_id": "xxx",
  "status": "completed",
  "intent": "查询三天天气并给老板发邮件",
  "started_at": "2026-06-08T10:00:00",
  "duration_ms": 15420,
  "nodes": {"total": 3, "completed": 2, "failed": 0, "skipped": 1},
  "tools_used": ["web_search", "send_email"],
  "token_usage": {"planner": 500, "executor": 800, "evaluator": 200},
  "approvals": {"requested": 1, "approved": 1},
  "errors": [],
  "replans": 0
}
```

### 14.3 持久化

摘要文件路径 `data/task_summaries/{YYYY-MM-DD}.jsonl`。

**写入策略**：不可让 Sub-Agent 自行写文件（阻塞 I/O 卡 event loop + 大摘要交织破损）。TaskSystem 内部维护单一消费者 `SummaryWriterWorker`：

```python
class SummaryWriterWorker:
    def __init__(self):
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._task = asyncio.create_task(self._loop())

    def enqueue(self, summary: dict):
        self._queue.put_nowait(summary)

    async def _loop(self):
        import aiofiles
        while True:
            summary = await self._queue.get()
            path = f"data/task_summaries/{today()}.jsonl"
            async with aiofiles.open(path, "a") as f:
                await f.write(json.dumps(summary) + "\n")
```

- Agent 只 `put_nowait()`，不碰磁盘
- 单消费者顺序写，零交织风险
- aiofiles 异步 I/O，不阻塞 event loop
- **TTL 清理**：按文件名（日期）判定，非 mtime。今天是 6 月 8 日 → 删除 `2026-05-31.jsonl` 及更早的文件

### 14.4 摘要的用途

- Debug / 排查问题
- 成本追踪（token 消耗汇总）
- 经验记忆构建（Reflector 可读取摘要决定是否需要深入分析）
- 主循环查询（用户问"刚才那个任务怎么样了？"）

> **注意**：这不是经验记忆的替代品。摘要轻量、有 TTL；经验记忆持久化、结构化。

---

## 15. Token 消耗统计

### 15.1 统计点

TaskSystem 内部的 LLM 调用点：

| 组件 | 调用场景 | 统计粒度 |
|------|---------|---------|
| Planner | 生成 DAG 骨架 | 每次 Plan 生成 |
| Executor | 为每个 DAG Node 生成 DBT | 每个 Node |
| Evaluator | 检查节点结果 | 每个 Node |
| Reflector | 分析失败、提炼经验 | 每次 Reflect |
| Sub-Agent 模式② | 自然语言摘要 | 每次摘要生成 |
| Worker Agent | 执行 DBT 时如果工具内部调了 LLM | 每个工具调用 |

每个统计点记录：`(component, node_id, prompt_tokens, completion_tokens, model)`

### 15.2 汇总维度

```
                    ┌─ Planner
                    ├─ Executor
  任务级(token/task)─├─ Evaluator
                    ├─ Reflector
                    └─ 工具调用

                    ┌─ 今日总计
  聚合级(token/period)├─ 本周总计
                    └─ 按模型分组
```

### 15.3 与现有 TokenTracker 的关系

现有 `core/token_tracker.py` 统计 CognitiveLoop 的 LLM 调用（主对话）。TaskSystem 有独立的 TokenTracker，两者汇总到同一个展示层（Debug WebUI）。

### 15.4 API

见 18.1 节 `get_token_usage(task_id)` 和 `get_token_stats()`。

---

## 16. 可观测性

### 16.1 Debug WebUI

独立的 `/debug/tasks` 页面，展示：

- 活跃 DAG 图（节点状态颜色标记）
- 节点执行状态 / 进度
- Sub-Agent / Worker Agent 并发使用率
- 约束指标仪表盘（Token 消耗、失败计数、剩余时间）
- 经验记忆命中情况

提供 JSON API + SSE 推送：

- `/debug/api/tasks` → TaskSystem 状态快照
- `/debug/api/tasks/{task_id}/dag` → DAG 图数据（节点 + 依赖边 + 状态），对应 `TaskSystem.get_task_dag()`
- `/debug/api/tasks/stream` (SSE) → 轻量状态变更事件流

**SSE 事件格式：**
```
event: node_status
data: {"task_id": "T1", "node_id": "C", "status": "completed"}

event: task_status  
data: {"task_id": "T1", "status": "completed"}

event: agent_change
data: {"available_slots": 3, "active_sub_agents": 2, "active_workers": 5}
```

前端模式：初始拉取全量 → 监听 SSE 增量 → 收到变更后按需 Fetch。替代高频轮询。
内部由 `debug_api.py` 维护 `_status_subscribers` 列表，TaskSystem 状态变更时扇出。

### 16.2 任务日志

结构化日志，包含：

- Plan 生成（Planner 输入/输出）
- 每步执行结果（工具名、参数、耗时、返回值摘要）
- Evaluator 判定（通过/失败原因）
- Replan 触发原因
- 审批链路（请求 → 用户决策 → 回传）

---

## 17. Sub-Agent 返回模式

### 模式①：纯工具调用

返回结构化 `TaskResult`，主循环自行决定如何呈现。

### 模式②：自然语言

Sub-Agent 内部调 LLM 做摘要/润色，返回可直接发送给用户的自然语言。内部保留各工具详情（存入 buffer），主循环可随时查询。

---

## 18. API 设计

### 18.1 TaskSystem 对外 API

```python
class TaskSystem:
    # ── 核心入口 ──

    async def submit(
        self,
        context: DelegationContext,
        return_mode: Literal["structured", "natural"] = "structured",
    ) -> TaskMailbox | SubmitError:
        """
        提交任务。内部 Planner → Executor → Mailbox 回传。
        唯一入口，简单任务不进这里（CognitiveLoop 自己处理了）。
        参数验证在 TaskSystem 侧执行，不合法直接拒绝，不创建 Sub-Agent。
        context: DelegationPreparer.prepare() 的产出，包含 intent/hints/
                 capabilities/task_constraints/summarized_context/session_id。
        """
        ...

    # ── 任务控制 ──

    async def pause(self, task_id: TaskId) -> bool:
        """暂停：当前节点执行完后写 checkpoint，不再推进。"""
        ...

    async def resume(self, task_id: TaskId) -> TaskMailbox | ResumeError:
        """从暂停/中断的 checkpoint 恢复执行。

        成功返回已有的 TaskMailbox（pause 时不销毁），
        失败返回 ResumeError（含 ResumeErrorCode 枚举，见 api.md）。

        v1 限制：resume() 使用原始 DelegationContext，不支持注入新上下文。
        如需修改任务参数，必须 cancel → 重新 submit。
        """
        ...

    async def cancel(self, task_id: TaskId) -> bool:
        """软取消。先写 checkpoint，再安全停止。二次确认由 CognitiveLoop 处理。"""
        ...

    async def submit_approval(
        self, task_id: TaskId, agent_id: AgentId, approved: bool,
        feedback: str | None = None,
    ) -> bool:
        """人工审批回传。approved=False 时 feedback 传入 Planner/Replan 作为修正建议。"""
        ...

    # ── 查询 ──

    def get_task_status(self, task_id: TaskId) -> TaskStatus: ...

    def get_task_summary(self, task_id: TaskId) -> TaskSummary | None: ...

    def list_active_tasks(self) -> list[TaskSummary]: ...

    def list_agents(self) -> AgentList:
        """当前活跃的 Sub-Agent 和 Worker Agent 列表"""

    def get_token_usage(self, task_id: TaskId) -> TokenUsage | None: ...

    def get_token_stats(self, period: str = "today") -> TokenStats: ...

class SubmitError:
    code: str           # INVALID_PARAM, PLAN_TOO_LARGE, ...
    message: str
    details: dict
    suggestions: list[str]  # 修复建议
```

### 18.2 TaskMailbox API

```python
class TaskMailbox:
    task_id: TaskId
    _maxsize: int = 100  # 有界队列，防止主循环慢消费时内存堆积

    async def get(self, timeout: float | None = None) -> TaskEvent:
        """阻塞等待下一个事件。timeout 秒后返回 None。"""
        ...

    async def get_nowait(self) -> TaskEvent | None:
        """非阻塞获取，无事件时返回 None"""
        ...

    def count(self) -> int:
        """返回 mailbox 中待消费的事件数量"""
        ...

class TaskEvent:
    type: str  # "progress" | "approval_needed" | "completed" | "failed" | "cancelled"
    data: dict

# progress 事件的 data schema:
{
    "task_id": "xxx",
    "node_id": "C",
    "node_index": "2/5",           # 当前第几步
    "node_description": "正在搜索天气数据...",
    "percent": 0.4,                # 0~1，如果可估算
    "message": "搜索完成，正在解析结果..."
}
```

> `count()` 用途：mailbox 可能堆积（主循环繁忙时），消费者可据此判断是否需加速消费或跳过旧进度事件。

**队列溢出策略**：`maxsize=100`。溢出时丢弃最旧的 `progress` 事件（进度丢失可接受），保留 `approval_needed`/`completed`/`failed`（这些必须送达）。

### 18.3 主循环集成

**任务提交——通过 `submit_task` 工具**：

`submit_task` 注册为 LLM 可用工具。LLM 选择此工具后，流程为：

```
submit_task 工具执行
  → DelegationPreparer.prepare(...)   ← 一次 LLM 调用，准备上下文
  → TaskSystem.submit(intent, context, hints)
```

```python
# 工具执行器中的路由逻辑
async def _execute_tool(self, name: str, **args) -> ToolResult:
    if name == "submit_task":
        # 防御性校验：intent 必须非空
        intent = (args.get("intent") or "").strip()
        if not intent:
            return ToolResult.fail(name="submit_task", error="intent 不能为空")
        if len(intent) > 10000:
            return ToolResult.fail(name="submit_task", error="intent 过长")

        hints = (args.get("hints") or "").strip() or None

        # DelegationPreparer: 一次 LLM 调用，可能拒绝委派
        prep_result = await self._delegation_preparer.prepare(
            intent=intent,
            hints=hints,
            session_messages=self._session.messages,  # 全量会话，不截断
            user_profile=self._user_profile,
            tools=self._tool_executor.definitions(),
            global_constraints=self._task_system.constraints,
        )

        if prep_result.status == "no_matching_tools":
            missing = ", ".join(prep_result.missing_capabilities)
            return ToolResult.fail(
                name="submit_task",
                error=f"缺少 {missing} 相关能力，这不是参数问题，"
                      f"无法通过重试解决。请直接告知用户当前不支持此功能。"
            )
        elif prep_result.status == "too_vague":
            msg = prep_result.suggestion or "能再具体一点吗？"
            return ToolResult.fail(
                name="submit_task",
                error=f"意图不够明确: {msg}。请向用户澄清，不要重复猜测。"
            )

        result = await self._task_system.submit(
            context=prep_result.context,  # 已含 intent/hints
        )
        if isinstance(result, SubmitError):
            await self._output.send(f"抱歉：{result.message}")
            return ToolResult.fail(name="submit_task", error=result.message)
        else:
            await self._output.send("好的，已经在后台执行了。")
            await self._fsm.transition(AgentState.IDLE_BACKGROUND_TASK)
            self._register_mailbox(result)
            return ToolResult.ok(name="submit_task", content="任务已提交")
    else:
        return await self._execute_single_tool(name, **args)
```

**`submit_task` 工具定义**（注册到 LLM tool list）：

```json
{
    "type": "function",
    "function": {
        "name": "submit_task",
        "description": "将复杂多步骤任务提交到后台异步执行。适用：涉及多步操作、"
                       "状态变更、或长时间运行的任务。简单单步操作请直接用对应工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "description": "完整任务意图描述，包含目标和约束"
                },
                "hints": {
                    "type": "string",
                    "description": "对任务执行有帮助的额外信息，"
                                   "如用户情绪、紧急程度、偏好等"
                }
            },
            "required": ["intent"]
        }
    }
}
```

**事件消费**（以下为演示代码，非真实 Agent 实现。真实调用方式由 CognitiveLoop 侧决定）：

> **审批不走这里**：`approval_needed` 已通过 `HITLNotifier` 快速通道直推前端收件箱。Mailbox 中的 `approval_needed` 仅用于审计——Agent 记录日志，不负责转发审批卡片。

```python
# ═══ 演示代码（非真实实现）═══
# 展示调用方如何使用 TaskMailbox API。
# 真实集成由 CognitiveLoop 侧按照自己的架构实现。

class Agent:
    def _on_task_submitted(self, mailbox: TaskMailbox):
        """收到新 Mailbox，启动独立消费协程。"""
        asyncio.create_task(self._consume_mailbox(mailbox))

    async def _consume_mailbox(self, mailbox: TaskMailbox):
        """每个 Mailbox 一个协程，直接 await mailbox.get()。"""
        while True:
            msg = await mailbox.get()
            if msg is None:  # Mailbox 已关闭（任务终态）
                return
            await self._dispatch_task_event(msg)

    async def _dispatch_task_event(self, msg: TaskEvent):
        match msg.type:
            case "progress":
                await self._output.action("task_progress", params=msg.data)
            case "approval_needed":
                logger.info(f"Task {msg.data['task_id']} 等待审批: {msg.data['tool_name']}")
            case "completed":
                rid = self.next_request_id()
                set_request_id(rid)
                await self._cognitive.process(
                    content=msg.data["result"],
                    is_task_result=True,
                )
            case "failed":
                await self._output.send(
                    f"任务执行失败：{msg.data.get('error', '未知错误')}"
                )
            case "cancelled":
                pass
```

**TaskMailbox 内部**：

```python
class TaskMailbox:
    def __init__(self, task_id: TaskId):
        self.task_id = task_id
        self._queue: asyncio.Queue[TaskEvent] = asyncio.Queue(maxsize=100)

    async def get(self, timeout: float | None = None) -> TaskEvent | None:
        ...

    def _push(self, event: TaskEvent):
        """Sub-Agent 内部调用，写入事件。

        无线程安全——TaskSystem 全部 asyncio 协程。
        CPU 密集工具走按需子进程（create_subprocess_exec），
        通过 JSON stdin/stdout 通信，不直接 push Mailbox。
        """
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # 溢出策略：丢最旧的 progress，保留关键事件
            ...
```

**关键优势**：
- submit()/resume() 返回的 Mailbox 到手即用，调用方直接 `await mailbox.get()`
- 零绑定、零配置——Agent 不需要知道内部队列机制
- 内部 asyncio.Queue，零轮询 CPU 开销

---

## 19. 与现有代码的关系

### 19.1 保留

- `Module/tool/` — 工具协议、ToolManager、SafetyPipeline、Capability
- `core/task.py` — Task 数据模型（可能需要扩展字段）
- `core/task_scheduler.py` — 保留，作为 Worker Pool 底层调度基础
- `core/event_bus.py` — EventBus 不变，新增 HITL 事件类型
- `Module/output/` — OutputManager 不变，新增 task_progress 等 action

### 19.2 改造

- `core/cognitive_loop.py` — 注册 submit_task 工具，调用 DelegationPreparer → TaskSystem.submit()
- `core/agent.py` — `_process_events()` 新增 TaskMailbox 消费
- `static/debug.html` — 新增 TaskSystem 面板（或新页面）
- `static/js/` — 审批收件箱 UI

### 19.3 新建

- `Module/task_system/` — 全新模块

---

## 20. 实施阶段

### Phase 1：数据模型 + 安全层
- 定义 `Plan`, `DAGNode`, `DBT`, `DelegationContext` 等数据模型
- 实现约束层 + RBAC
- **MVP 基础**：提供 TaskSystem 所需的类型系统和安全护栏

### Phase 2：Sub-Agent + Planner + Executor
- 实现 Planner（LLM → DAG）
- 实现 Executor（拓扑排序 + DBT 生成）— DBT 模板可 stubb，先全走 LLM
- 实现 Worker Agent + 池
- 实现 Evaluator + Replan
- **MVP 核心** ✅：Phase 2 完成即可跑复杂多步骤任务

### Phase 3：异步化 + 通知
- TaskMailbox 实现
- CognitiveLoop Fire-and-Forget 改造
- 软 INTERRUPT
- **MVP 核心** ✅：Phase 3 完成即实现非阻塞异步任务

### Phase 4：任务路由 + 安全
- `submit_task` 注册为 LLM 工具（LLM 自主决定是否委派后台任务）
- RBAC 权限矩阵 + 审批风暴防护
- 审批 UI 升级 — 批量审批可 stubb，先走单条审批
- **MVP 可精简**：审批先走单条

### Phase 5：恢复 + 记忆 + 可观测性
- 节点级 Checkpoint
- 经验记忆 + Reflector
- Debug WebUI
- 工具执行模式（coroutine/process）
- **MVP 可延后**：Phase 5 是增强特性，不影响核心功能闭环

---

## 21. 风险与注意事项

1. **LLM 调用增多**：Planner + DBT 生成 + Evaluator 都需要 LLM，token 消耗大幅增加。Demo 阶段可接受，后续通过模型蒸馏、缓存、规则降级优化。
2. **审批用户体验**：批量审批如果太多可能失去意义。MUTATION 工具应尽量少，多数操作用 SAFE_WRITE 覆盖。
3. **两层 Agent 递归风险**：需要在代码层面强制校验禁止递归，而非靠约定。
4. **按需子进程的 fork+exec 开销**：每次 CPU 密集工具调用都创建新进程，比常驻进程池的启动开销高。但 Demo 阶段 CPU 密集工具少、频率低，真杀（SIGKILL）+ 零泄露的价值远大于启动开销。高频场景后续可视情况引入进程池。
5. **数据库依赖与降级**：
   - Milvus 不可用 → Planner 跳过经验查询（非阻塞），Reflector 暂存内存去重层 + 文件 fallback
   - 文件系统不可用 → Checkpoint 写内存 + 定时重试；Summary 仅内存保存
   - 降级期间功能受限但不崩溃

---

## 22. 术语表 (Glossary)

| 术语 | 定义 |
|------|------|
| **DAG** (有向无环图) | Planner 产出的高层任务结构：节点列表 + depends_on 依赖关系 |
| **DBT** (动态行为树) | ⚠️ 实验性质。每个 DAG 节点内部的执行策略，运行时动态生成，含 Sequence/Selector/Fallback 等 |
| **Sub-Agent** | 拥有完整 Plan → Execute → Evaluate → Replan 能力的自主执行单元 |
| **Worker Agent** | DBT 执行引擎，接收一个 DAG Node 并执行整棵行为树，不规划不生成子 Agent |
| **Planner** | Sub-Agent 组件，用 LLM 将用户意图转化为 DAG 骨架 |
| **Executor** | Sub-Agent 组件，拓扑排序 DAG + 生成 DBT + 分配给 Worker Agent |
| **Evaluator** | Sub-Agent 组件，双重判定（ExecutionStatus + BusinessStatus）检查节点结果 |
| **Replan** | 节点失败后 DAG 层重新规划——换方案/跳过/求助 |
| **TaskMailbox** | 每个 Sub-Agent 独立的异步事件队列。单播——唯一消费者是 Agent。传输进度和结果给主循环 |
| **HITLNotifier** | 审批快速通道协议。TaskSystem 依赖此抽象，不依赖 EventBus。实现者注入 |
| **EventBus** | Agent 内部优先级控制信号通道（INTERRUPT、STATE_CHANGE）。HITLNotifier 的今天实现用到它 |
| **Reflector** | 分析工具失败原因 → 提炼经验 → 写入任务经验记忆 |
| **经验记忆** | TaskSystem 专属知识库，存失败模式 + 修复方案，与用户 LongTermMemory 独立 |
| **Checkpoint** | 节点级持久化快照，含当前进度、节点结果、挂起审批，支持任务恢复 |
| **RBAC 权限矩阵** | 工具三层权限：READ_ONLY（自主）、SAFE_WRITE（条件自主）、MUTATION（HITL） |
| **约束层** | 所有执行路径强制遵守的护栏：Token/时间/次数/链路长度/错误长度 等 |
| **任务路由** | `submit_task` 注册为 LLM 工具，LLM 自主决定是否委派后台任务 |
| **FAILURE_MELTDOWN** | 资源耗尽型错误立即终止 DAG，禁止 Replan |
| **Soft Wake-up** | 后台任务完成后先检查对话上下文，话题漂移时礼貌询问是否现在汇报 |
| **AgentState 原子替换** | `= AgentState()` 新建 → `= None` 丢弃，dataclass 编译期保证无字段泄漏 |
| **降级路径 (DBT)** | 如果 DBT 效果不佳，降级为 `NodeConfig`(max_retries + fallback_tool + on_exhausted)。Worker 用简单 while 循环替代行为树遍历 |
