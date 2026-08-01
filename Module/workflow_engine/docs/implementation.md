# TaskSystem — 实施计划

> 截至 2026-06-18，v1 核心已完成，目录结构重构已完成。本文件描述当前实际文件布局。

**Goal:** 将当前紧耦合的 CognitiveLoop + TaskScheduler 重构为解耦的 Plan-Execute-Evaluate-Replan 架构，实现异步非阻塞任务执行。

**Architecture:** 3 层（core/safety/learning）+ 顶层扁平模块。编排分两层：Sub-Agent（决策层）+ Orchestrator（执行层）。通过 TaskMailbox + HITLNotifier 与 CognitiveLoop 通信。DAG 图存储使用 NetworkX（PlanGraph 封装）。`submit_task` 注册为 LLM 工具，LLM 自主判断是否委派后台任务。

**Spec:** `Module/workflow_engine/docs/architecture.md`<br>
**API:** `Module/workflow_engine/docs/api.md`

---

## 文件结构

### 当前实际布局（2026-06-18 重构后）

```
Module/workflow_engine/
├── __init__.py                # 公开 API facade（re-export 自 task_system.py）
├── task_system.py             # TaskSystem 主类：生命周期 (start/stop) + submit + 薄封装委托 (~430 行)
├── protocol.py                # HITLNotifier 等对外依赖协议（ABC）
├── delegation_preparer.py     # DelegationPreparer: LLM 准备委派上下文（v1 已实现）
├── infra.py                   # AgentInfra + 共享基础设施
├── mailbox.py                 # TaskMailbox（单播，Agent 唯一消费者）
├── checkpoint.py              # CheckpointManager: 节点级 TaskState 快照（内存版）
├── artifact_store.py          # ArtifactStore: 节点大产物存储（内存版）
├── query.py                   # 查询 API（11 个只读模块函数）
├── lifecycle.py               # 任务操作（pause / cancel / submit_approval）
├── token_ledger.py            # TokenLedger: write-through + per-call 审计日志
├── debug_api.py               # Debug WebUI 数据接口 + SSE 推送端点
├── utils.py                   # 工具函数
├── schema/                    # 核心数据结构（5 文件，拆分自 schema.py）
│   ├── __init__.py            # 全部 re-export，向后兼容
│   ├── types.py               # 工具函数 + NewType 别名 + 基础枚举（含 NodeErrorCode）
│   ├── protocol.py            # 跨层协议（ConstraintConfig, DelegationContext, HITLData 等）
│   ├── output.py              # 事件 + 查询视图（TaskEvent, TaskSummary, TokenStats 等）
│   └── internal.py            # 编排数据模型（DAGNode, Plan, TaskState, NodeResult 等）
├── core/                      # 编排循环
│   ├── __init__.py
│   ├── sub_agent/             # SubAgent: 决策层——规划/Replan/熔断（拆分自 sub_agent.py）
│   │   ├── __init__.py        # 公开导出
│   │   ├── sub_agent.py       # SubAgent 主类：run() 入口 + 决策循环驱动
│   │   ├── planning.py        # 规划阶段：plan → validate → FIX_PLAN 重试
│   │   ├── execution_loop.py  # 执行循环：orchestrator.run() + 结果处理
│   │   ├── result_handlers.py # 结果处理函数
│   │   └── replan.py          # Replan 决策：evaluator 判定 → replan 触发
│   ├── orchestrator.py        # Orchestrator: 执行层——按 Plan 推进 DAG (421 行)
│   ├── graph.py               # PlanGraph: NetworkX DiGraph 封装（图算法层）
│   ├── planner/               # Planner: LLM → DAG 骨架 / DAGPatch（拆分自 planner.py）
│   │   ├── __init__.py        # 公开导出
│   │   ├── planner.py         # Planner：plan() 首次规划
│   │   ├── replanner.py       # Replanner：replan() 失败修复（full_replan 双路径）
│   │   ├── prompts.py         # Prompt 模板（5 个常量）
│   │   └── parser.py          # 共享解析 + call_llm() + PlanParseError
│   ├── plan_validator.py      # PlanValidator: DAG 静态校验（纯逻辑，9 项）
│   ├── executor.py            # Executor: 单节点工具调用（LLM matching + ToolManager）
│   ├── evaluator.py           # Evaluator: 双重判定（ExecutionStatus + BusinessStatus）
│   ├── execution.py           # 节点执行管线（execute → evaluate → apply）
│   └── finalize.py            # 收尾 + checkpoint 工具（8 个函数）
├── safety/                    # 安全护栏（v2 TODO）
│   └── __init__.py            # 仅 stub
├── learning/                  # 经验记忆与学习（v2 TODO）
│   └── __init__.py            # 仅 stub
├── tests/
│   ├── __init__.py
│   ├── helpers/
│   │   └── __init__.py
│   ├── infra/
│   │   └── __init__.py
│   ├── unit/
│   │   ├── __init__.py
│   │   ├── test_task_system.py
│   │   ├── test_orchestrator.py
│   │   ├── test_sub_agent.py
│   │   ├── test_graph.py
│   │   ├── test_evaluator.py
│   │   ├── test_executor.py
│   │   ├── test_planner.py
│   │   ├── test_delegation_preparer.py
│   │   ├── test_checkpoint.py
│   │   ├── test_mailbox.py
│   │   └── test_token_ledger.py
│   ├── integration/
│   │   ├── __init__.py
│   │   ├── conftest.py
│   │   ├── test_task_system_lifecycle.py
│   │   ├── test_e2e_mvp.py
│   │   ├── test_delegation_preparer.py
│   │   ├── test_executor.py
│   │   └── test_planner.py
│   ├── safety/
│   │   └── __init__.py
│   └── learning/
│       └── __init__.py
└── docs/
    ├── api.md
    ├── architecture.md
    ├── implementation.md       # 本文件
    ├── migration.md
    └── PROGRESS.md
```

### 2026-06-18 重构要点

**Schema 拆分**：`schema.py` (1287行) → `schema/` 包（5 文件）。
- `types.py` — 工具函数 + NewType 别名 + 基础枚举
- `protocol.py` — 跨层协议（ConstraintConfig, DelegationContext, HITLData）
- `output.py` — 事件 + 查询视图（TaskEvent, TaskSummary, TokenStats）
- `internal.py` — 编排数据模型（DAGNode, Plan, TaskState, NodeResult）
- `__init__.py` — 全部 re-export，向后兼容
- 关键修复：`NodeErrorCode` 从 internal 移入 `types.py`，切断 output ↔ internal 循环依赖。

**Planner 拆分**：`planner.py` (739行) → `core/planner/` 包（5 文件）。
- `prompts.py` — 5 个 prompt 常量
- `parser.py` — 共享 `call_llm()` + `parse_plan_from_dict()` + `parse_patch_from_dict()` + `PlanParseError`
- `planner.py` — `Planner` 类：`plan()` 首次规划
- `replanner.py` — `Replanner` 类：`replan()` 失败修复
- `__init__.py` — 公开导出

**Sub-Agent 拆分**：`sub_agent.py` → `core/sub_agent/` 包（6 文件）。
- `sub_agent.py` — SubAgent 主类
- `planning.py` — 规划阶段
- `execution_loop.py` — 执行循环
- `result_handlers.py` — 结果处理函数
- `replan.py` — Replan 决策
- `__init__.py` — 公开导出

**TaskSystem 拆分**：`task_system.py` (843行) → 3 文件（`task_system.py` + `query.py` + `lifecycle.py`）。所有提取函数接受显式参数替代 `self._xxx`，TaskSystem 公开 API 签名不变。

**Orchestrator 拆分**：`orchestrator.py` (708行) → 3 文件（`orchestrator.py` + `execution.py` + `finalize.py`）。薄封装签名与当前方法完全一致，`run()` 调用点零改动。

### v2（已明确不做）

| 项目 | 说明 |
|------|------|
| Worker Agent 池 | 当前顺序执行独立节点 |
| DBT 行为树 | 降级路径：`NodeConfig(max_retries + fallback_tool + on_exhausted)` |
| File-persistent checkpoint | 当前内存版 |
| 安全护栏 (RBAC) | `safety/` 目录仅有 `__init__.py` |
| 经验记忆 (Reflector) | `learning/` 目录仅有 `__init__.py` |
| 审批风暴检测 | `_needs_approval()` 硬编码 False |

---

## 改造（外部集成点）

以下文件位于父项目（Elysia/Agent），不属于本仓库，仅供集成参考。

```
core/cognitive_loop.py          # 注册 submit_task 工具 → DelegationPreparer → TaskSystem.submit()
core/agent.py                   # _process_events() + _process_task_events() 双协程
core/agent_fsm.py               # 新增 AgentState.IDLE_BACKGROUND_TASK
core/hitl.py                    # EventBusHITLNotifier 使用 HITL_REQUEST/HITL_CANCEL
Module/output/manager.py        # 新增 task_progress、task_completed action
static/                         # 审批收件箱 UI + /debug/tasks 面板
```

## 保留不动

```
Module/tool/                   # ToolPlugin、ToolManager、SafetyPipeline
core/task.py                   # Task 数据模型（可选扩展）
core/task_scheduler.py         # 现有任务调度器（保留或逐步替换）
Module/output/protocol.py      # OutputChannel、ChannelCapability
```
