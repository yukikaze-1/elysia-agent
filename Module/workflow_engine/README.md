# WorkflowEngine — 异步 Plan-Execute-Evaluate-Replan 任务编排

> 文档导航：[项目文档](../../README.md) → [Module 文档](../README.md)

WorkflowEngine 是独立于 CognitiveLoop 的后台任务编排引擎：LLM 通过 `submit_task` 工具调用委派意图，引擎以 Sub-Agent 池执行「规划 → 校验 → DAG 执行 → 评估 → 重规划」闭环，结果经 WorkflowMailbox 单播回传 agent。

## 三区架构

分离重构后 workflow_engine 是**零 core 依赖的干净叶子**，与 core 应用层之间通过契约层 `workflow_contract` 解耦——跨边类型只允许从契约层解析，两边各自单向 import：

```
┌─────────────────────────────┐
│ core/（应用层）              │  CognitiveLoop / agent lifecycle / input server
│   DelegationPreparer         │  ← 消费方：submit_task 委派 + 轮询 mailbox
└──────────────┬──────────────┘
               │ import（引擎 API + 契约类型）
┌──────────────▼──────────────┐
│ workflow_contract（契约层）   │  跨边唯一类型来源：类型/枚举/协议 ABC/
│  （零业务逻辑）               │  无状态纯函数/AgentInfra
│                              │  不 import core.*，不 import workflow_engine
└──────────────┬──────────────┘
               │ import（单向）
┌──────────────▼──────────────┐
│ workflow_engine（干净叶子）   │  WorkflowEngine / SubAgent / Orchestrator /
│                              │  Planner / Executor / Evaluator / ...
│                              │  不 import core.*
└─────────────────────────────┘
```

契约层文档：[Module/workflow_contract/README.md](../workflow_contract/README.md)

## 与 `core/tool_runtime/ :: ToolCallDispatcher` 的关系（互补非重复）

| 维度 | `core/tool_runtime/ :: ToolCallDispatcher` | `workflow_engine` |
|------|---------------------------|-------------------|
| 职责 | ReAct 工具子任务 worker pool（5 workers） | 后台长任务 Plan-Execute-Evaluate-Replan 编排 |
| 粒度 | 单次工具调用 / 短任务 | 多节点 DAG + Sub-Agent 决策闭环 |
| 生命周期 | 单 turn 内消费，无持久化 | checkpoint 持久化 + mailbox 跨 turn 回传 |
| 调用方 | CognitiveLoop 直接使用 | 通过 `submit_task` 工具委派 |
| 取消传播 | Future 双向绑定 → INTERRUPT 精准取消 | 任务级 pause/cancel/submit_approval |

两者不重复：ToolCallDispatcher 管「一个工具调用如何并发跑」，WorkflowEngine 管「一个意图如何拆成计划、校验、按 DAG 执行并闭环」。新后台编排一律走 workflow_engine。

## 关键机制

- **委派入口**：`submit_task` 工具调用 → core 侧 `DelegationPreparer`（LLM 工具筛选 + 参数填充）→ 产出 `DelegationContext` → 契约层 `WorkflowEngine.submit()`
- **非阻塞有界 submit（E2）**：`submit()` 预创建 `QUEUED` 状态 + 注册后立即返回 mailbox（不等待执行槽）；`acquire` 移到后台 `_drain_queue` worker（有界队列 `max_queue_size=5`）；PAUSED 任务释放执行槽；resume 槽满入队（`resume_pending` 幂等），队列满快速失败 `QUEUE_FULL`
- **Sub-Agent 决策循环**：`SubAgent` 对每个任务执行 plan → validate → execute → evaluate →（失败时）replan，超过 `check_meltdown` 阈值熔断
- **Orchestrator 执行层**：按 DAG 拓扑序驱动节点，节点经 `execution.py` 管线（execute → evaluate → apply）
- **校验**：`PlanValidator` 对计划做 9 项静态校验（工具存在性、参数类型、约束合规）
- **安全**：`ConstraintConfig`（全局天花板，随 `AgentInfra` 注入）+ 任务级 `WorkflowConstraints` 取 min 生效
- **持久化**：`CheckpointManager` 任务级快照、`TokenLedger` token 记账、`ArtifactStore` 节点产物存储
- **回传**：`WorkflowMailbox` 单播异步队列，agent `_poll_mailboxes()` 轮询消费

## 执行槽与排队（E2 非阻塞 submit）

`WorkflowEngine.submit()` 立即返回 mailbox，不阻塞聊天轮（首任务无限暂停时第二次 submit 仍即时返回）：

- **状态机入口新增 `QUEUED`**（12 态）：已注册（`_tasks`/`_mailboxes`/`_contexts` 就位）、等待首个执行槽、**不消耗 semaphore**；可被 `cancel`/`stop` 直接终态化为 CANCELLED
- `submit()` 预创建 `WorkflowState(status=QUEUED)` 注入 `sub_agent._task_state` → 注册 → 入队（有界 `max_queue_size=5`）即返；队列满快速失败 `SubmitError(QUEUE_FULL)`，不无限 await
- 后台单任务 **`_drain_queue`** worker 负责「终态复核 → `acquire` 槽位 → 按 `QueuedItem.kind` 派发 `_run_and_cleanup`/`_run_resume`」——`acquire` 不再阻塞聊天轮
- **PAUSED 释放执行槽**：`_run_and_cleanup`/`_run_resume` 的 finally 遇 PAUSED 释放 semaphore + 移出 `_active`，checkpoint/mailbox/注册表保留；resume 重新 acquire（槽满入队、`resume_pending` 幂等短路、队列满 `ResumeError(QUEUE_FULL)`）
- `stop()` 停 worker + 扫描 `_tasks` 终态化 QUEUED/PAUSED（推唯一 CANCELLED 事件）；`cancel()` 新增 QUEUED 分支直接 CANCELLED（不 release——QUEUED 从未 acquire）
- debug snapshot：`slots_used` 改由 `_active` 集合计算（不把 QUEUED/PAUSED 计入占槽）、新增 `queued`（队列深度）

## 文件布局

```
Module/workflow_engine/
├── __init__.py           # 重导出（公共 API + 引擎内部类型分组）
├── workflow_engine.py    # WorkflowEngine 主类（对外 API 入口）
├── mailbox.py            # WorkflowMailbox 单播事件队列 + push_mailbox_event
├── lifecycle.py          # 任务操作函数（pause/cancel/submit_approval）
├── query.py              # 任务查询 API（WorkflowSummary/DAG/TokenStats）
├── checkpoint.py         # 任务级检查点（CheckpointManager）
├── token_ledger.py       # 任务级 token 记账（TokenLedger）
├── artifact_store.py     # 节点产物临时存储（ArtifactStore）
├── debug_api.py          # SSE 端点（/debug/api/tasks/stream）+ DAG vis-network
├── core/
│   ├── orchestrator.py   # 执行编排（Orchestrator：DAG 拓扑驱动）
│   ├── sub_agent/        # 决策层：plan → validate → execute → replan
│   │   ├── sub_agent.py  #   SubAgent 类 + _dispatch_result
│   │   ├── planning.py   #   plan_and_validate()
│   │   ├── execution_loop.py  #   execute_loop() 执行-决策循环
│   │   ├── result_handlers.py #   5 个结果处理器
│   │   └── replan.py     #   handle_replan() + check_meltdown()
│   ├── planner/          # 规划与修复
│   │   ├── planner.py    #   Planner：plan() 首次规划
│   │   ├── replanner.py  #   Replanner：replan() 失败修复
│   │   ├── prompts.py    #   Prompt 模板常量
│   │   └── parser.py     #   共享解析 + call_llm + PlanParseError
│   ├── plan_validator.py # 计划静态校验（PlanValidator）
│   ├── executor.py       # 单节点执行（Executor：tool-calling / LLM 直出）
│   ├── evaluator.py      # 双重判定（Evaluator：execution + business）
│   ├── execution.py      # 节点执行管线（execute → evaluate → apply）
│   ├── graph.py          # DAG 依赖拓扑（PlanGraph + DAGPatch）
│   └── finalize.py       # 收尾 + checkpoint 工具
├── safety/               # 占位（空 __init__，安全约束在契约层 ConstraintConfig + core/safety）
├── schema/               # 引擎内部类型（types/protocol/output/internal，经 __init__ 统一导出）
├── learning/             # 学习模块（预留）
└── tests/
    ├── unit/             # 单元测试（pytestmark = mark.unit）
    └── integration/      # 集成测试（需真实 LLM/DB，不进门禁）
```

跨边类型（`DelegationContext`/`SubmitError`/`HITLData`/`WorkflowId`/`WorkflowStatus`/`AgentInfra` 等）已随契约层提取迁至 `Module/workflow_contract`，消费方一律从契约层解析，不再从本模块导出。

## 独立迭代命令

```bash
# 门禁（unit + service，不含 integration）
make test-workflow-engine

# 等价命令：
conda run -n Elysia python -m pytest Module/workflow_engine/tests Module/workflow_contract/tests -m "unit or service" -q --tb=short
```

> integration 测试需要真实 LLM/DB（`DEEPSEEK_API_KEY` / Milvus），不在门禁内。

## 调试

访问 `http://localhost:8765/debug` → **Tasks** Tab，可查看：
- 活跃任务 + 已完成任务（状态徽章 + 进度条）
- `vis-network` DAG 可视化（hierarchical TB 布局，status 着色节点）
- SSE 实时流（`/debug/api/tasks/stream`），自动重连 + exponential backoff

后端 API：`GET /debug/api/tasks/stream`（SSE），`GET /debug/api` → `info.scheduler_pending` / `info.scheduler_running` / `info.scheduler_total`（调度器工作线程池快照，见 `core/debug/info.py`）

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module/workflow_contract/README.md](../workflow_contract/README.md) | 跨边契约层——本模块的唯一跨边类型来源 |
| [Module 文档](../README.md) | 所有 Module 服务总览 |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 |
| [core/agent/README.md](../../core/agent/README.md) | Agent 组装入口——WorkflowEngine 的消费者 |
| [core/cognitive/README.md](../../core/cognitive/README.md) | 认知循环——submit_task 的触发者 |
