# TaskSystem — 实施计划（草案）

> 细节尚未敲定，本文件仅保留模块文件结构。实施任务清单将在设计收敛后补充。

**Goal:** 将当前紧耦合的 CognitiveLoop + TaskScheduler 重构为解耦的 Plan-Execute-Evaluate-Replan 架构，实现异步非阻塞任务执行。

**Architecture:** 3 个子包（core/safety/learning）+ 顶层扁平模块。编排分两层：Sub-Agent（决策层）+ Orchestrator（执行层）。通过 TaskMailbox + HITLNotifier 与 CognitiveLoop 通信。DAG 图存储使用 NetworkX（PlanGraph 封装）。`submit_task` 注册为 LLM 工具，LLM 自主判断是否委派后台任务。

**Spec:** `Module/task_system/docs/architecture.md`<br>**API:** `Module/task_system/docs/api.md`

---

## 文件结构

### 新建

```
Module/task_system/           # 全新模块
├── __init__.py                # 公开 API facade（re-export 自 task_system.py）
├── task_system.py             # TaskSystem 主类：生命周期 (start/stop) + 任务提交 (submit) + 查询/控制 stubs
├── protocol.py                # HITLNotifier 等对外依赖协议（ABC）
├── delegation_preparer.py     # DelegationPreparer: LLM 准备委派上下文（v1 已实现，将来可独立）
├── schema.py                  # 所有 Pydantic 数据模型（含状态码、错误码、枚举）
├── infra.py                   # AgentInfra + 共享基础设施（将来移出 TaskSystem）
├── mailbox.py                 # TaskMailbox（单播，Agent 唯一消费者）
├── checkpoint.py              # CheckpointManager: 节点级 TaskState 快照
├── artifact_store.py          # ArtifactStore: 节点大产物存储（v1 内存）
├── pool.py                    # v2 TODO: Agent 并发控制（BoundedSemaphore）
├── summary_store.py           # v2 TODO: 任务摘要存储（JSONL, TTL 7天）
├── tool_runner.py             # v2 TODO: CPU 密集工具子进程入口
├── debug_api.py               # v2 TODO: Debug WebUI 数据接口 + SSE 推送端点
├── __main__.py                # v2 TODO: 独立运行测试
├── core/                      # 编排循环
│   ├── __init__.py
│   ├── sub_agent.py           # SubAgent: 决策层——规划/Replan/熔断（7 deps）
│   ├── orchestrator.py        # Orchestrator: 执行层——按 Plan 推进 DAG（6 deps）
│   ├── graph.py               # PlanGraph: NetworkX DiGraph 封装（图算法层）
│   ├── planner.py             # Planner: LLM → DAG 骨架 / DAGPatch
│   ├── plan_validator.py      # PlanValidator: DAG 静态校验（纯逻辑，10 项）
│   ├── executor.py            # Executor: v1 单节点工具调用（LLM匹配+ToolManager），v2 DBT + Worker
│   └── evaluator.py           # Evaluator: 双重判定（ExecutionStatus + BusinessStatus）
├── safety/                    # 安全护栏（纯逻辑，无副作用）
│   ├── __init__.py
│   ├── constraint.py          # v2 TODO: 统一约束层
│   └── rbac.py                # v2 TODO: RBAC 权限矩阵
└── learning/                  # 经验记忆与学习
    ├── __init__.py
    ├── reflector.py           # v2 TODO: Reflector: 失败经验提炼
    ├── experience_memory.py   # v2 TODO: 任务经验记忆（独立于用户 LTM）
    └── dedup.py               # v2 TODO: 时间滑动窗口去重

Module/task_system/tests/      # 独立测试
├── test_schema.py
├── core/
├── safety/
└── learning/
```

### 改造

```
core/cognitive_loop.py          # 注册 submit_task 工具 → DelegationPreparer → TaskSystem.submit()
core/agent.py                   # _process_events() + _process_task_events() 双协程
core/agent_fsm.py               # 新增 AgentState.IDLE_BACKGROUND_TASK
core/event_bus.py               # EventBusHITLNotifier 使用 HITL_REQUEST/HITL_CANCEL
Module/output/manager.py        # 新增 task_progress、task_completed action
static/                         # 审批收件箱 UI + /debug/tasks 面板
```

### 保留不动

```
Module/tool/                   # ToolPlugin、ToolManager、SafetyPipeline
core/task.py                   # Task 数据模型（可选扩展）
core/task_scheduler.py         # 现有任务调度器（保留或逐步替换）
Module/output/protocol.py      # OutputChannel、ChannelCapability
```
