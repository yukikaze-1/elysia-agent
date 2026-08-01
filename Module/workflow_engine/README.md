# TaskSystem — 后台任务委派与编排

> 文档导航：[项目文档](../../README.md)

LLM 的 `submit_task` 工具调用 → DelegationPreparer（LLM 筛选+填充参数）→ TaskSystem（DAG 编排+执行）→ TaskMailbox（结果回传 agent）。

## 架构

```
submit_task tool call
  → CognitiveLoop._handle_submit_task()
    → DelegationPreparer.prepare(intent, hints, session_msgs, tools, constraints)
      → LLM: 工具筛选 + 参数填充 + 约束注入
      → DelegationResult (status=ok → context; otherwise → no_matching_tools/too_vague)
    → TaskSystem.submit(context)
      → core/orchestrator: plan → validate → DAG → dispatch
      → core/executor: 逐节点执行，支持依赖拓扑排序
      → core/finalize: 结果聚合 + token 记账
    → TaskMailbox (asyncio.Queue 单播)
      → agent poll loop → 注入 CognitiveLoop wakup message
```

## 关键机制

### 委派管线（DelegationPreparer）

- **输入**：LLM 输出的 intent（自由文本）+ hints + 当前 session 消息 + 全量工具列表 + 全局约束（通过 `TaskSystem.constraints` 属性获取）
- **LLM 调用**：筛选相关工具 + 填充参数 + 注入约束（单轮，无循环）
- **产出**：`DelegationResult`——三态：`ok`（含 `DelegationContext`）、`no_matching_tools`、`too_vague`

### 编排引擎（core/orchestrator）

- **plan**：解析 `DelegationContext` → 构建执行计划
- **validate**：`PlanValidator` 校验计划合法性（工具存在性、参数类型、约束合规）
- **graph**：拓扑排序、依赖解析、并行度计算

### 执行器（core/executor）

- 按 DAG 拓扑序逐节点执行工具调用
- 支持 `HITLNotifier` 审批回调（v1 走 ToolManager SafetyPipeline，不经过此通道）
- 结果写入 `TaskMailbox`，由 agent `_poll_mailboxes()` 轮询消费

### 安全（safety/）

- `ConstraintConfig`：全局天花板约束（max_tokens、max_tools、超时等）
- `PlanValidator`：执行前校验，拦截不安全计划

### 持久化

- `checkpoint.py`：任务状态检查点（`data/scheduler_checkpoint.json`）
- `token_ledger.py`：任务级 token 记账
- `artifact_store.py`：任务产出临时存储

## 文件布局

```
Module/workflow_engine/
├── __init__.py              — 重导出（按 7 个子领域分组）
├── task_system.py           — TaskSystem 主类（对外 API 入口）
├── delegation_preparer.py   — LLM 驱动的委派上下文准备
├── protocol.py              — 对外依赖协议（HITLNotifier, ToolExecutor 等）
├── lifecycle.py             — 任务操作函数（pause/cancel/submit_approval）
├── infra.py                 — AgentInfra 共享基础设施
├── mailbox.py               — TaskMailbox 单播异步事件队列
├── checkpoint.py            — 文件检查点（data/scheduler_checkpoint.json）
├── token_ledger.py          — 任务级 token 记账
├── artifact_store.py        — 任务产出临时存储
├── query.py                 — 任务查询接口
├── utils.py                 — 内部工具函数
├── debug_api.py             — SSE 端点（/debug/api/tasks/stream）+ Task DAG vis-network
├── core/
│   ├── orchestrator.py      — 编排引擎：plan → validate → DAG → dispatch
│   ├── executor.py          — 执行器：逐节点工具调用 + 结果收集
│   ├── execution.py         — 单节点执行上下文
│   ├── graph.py             — DAG 依赖拓扑
│   ├── plan_validator.py    — 执行前计划校验
│   ├── evaluator.py         — 约束/条件评估
│   └── finalize.py          — 结果聚合 + token 记账
├── safety/
│   └── __init__.py          — ConstraintConfig + PlanValidator 重导出
├── schema/
│   ├── types.py             — NewType（TaskId, SessionId 等）+ 枚举
│   ├── protocol.py          — DelegationResult, DelegationContext 等 Pydantic 模型
│   ├── internal.py          — 内部中间表示
│   └── output.py            — 对外输出类型
├── learning/
│   └── __init__.py          — 学习模块（预留）
└── tests/
    ├── helpers/              — 测试辅助（FakeLLM 等）
    ├── unit/                 — 单元测试
    └── integration/          — 集成测试
```

关联文件：
```
core/
├── agent/agent.py           — _register_background_task + 邮件箱轮询
├── cognitive/tools.py       — _handle_submit_task（拦截 submit_task tool call）
├── cognitive/loop.py        — submit_task_definition + get_task_mailbox
└── monitoring/event_bus.py  — TaskResultData 事件发布
```

## 调试

访问 `http://localhost:8765/debug` → **Tasks** Tab，可查看：
- 活跃任务 + 已完成任务（状态徽章 + 进度条）
- `vis-network` DAG 可视化（hierarchical TB 布局，status 着色节点）
- SSE 实时流（`/debug/api/tasks/stream`），自动重连 + exponential backoff

后端 API：`GET /debug/api/tasks/stream`（SSE），`GET /debug/api` → `info.scheduler_queue_size`

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../README.md) | 快速开始 + 完整文档导航 |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 |
| [docs/testing.md](../../docs/testing.md) | 测试分层 + 运行命令 |
| [core/agent/README.md](../../core/agent/README.md) | Agent 组装入口——TaskSystem 的消费者 |
| [core/cognitive/README.md](../../core/cognitive/README.md) | 认知循环——submit_task 的触发者 |
