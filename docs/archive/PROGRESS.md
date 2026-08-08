> ⚠️ 已归档（2026-08-09）：历史记录，不再随代码库更新。当前有效文档见 [../README.md](../README.md)。

# TaskSystem 项目进度

> 日期: 2026-06-22  
> 状态: v1 核心完成，CognitiveLoop 异步任务集成 + e2e 验证通过，目录结构重构完成，Agent/CognitiveLoop 类内拆分完成

---

## 总览

| 指标 | 数值 |
|------|------|
| 生产代码 | 37 文件, 8,107 行 |
| 测试代码 | 24 文件 |
| 单元测试 | **305 tests**, ~37s |
| 集成测试 | 36 tests (含 7 e2e), ~15min |
| 最近提交 | `1c3f26f` — docs: README 全面更新 (2026-06-24) |

---

## 架构

```
TaskSystem
├── DelegationPreparer     # LLM 工具筛选 + 上下文准备
├── TaskSystem             # 主类：生命周期 + submit/resume + 薄封装委托
│   ├── query.py           #   查询 API（11 个模块函数）
│   ├── lifecycle.py       #   任务操作（pause / cancel / submit_approval）
│   └── task_system.py     #   生命周期 + 薄封装 (478 行)
├── SubAgent (per-task)    # 决策层：规划 → 校验 → 执行循环
│   ├── planner/           #   规划与修复
│   │   ├── planner.py     #     Planner：plan() 首次规划
│   │   ├── replanner.py   #     Replanner：replan() 失败修复
│   │   ├── prompts.py     #     Prompt 模板（5 个常量）
│   │   └── parser.py      #     共享解析 + call_llm + PlanParseError
│   ├── sub_agent/          #   决策循环
│   │   ├── planning.py     #     plan_and_validate() 规划+校验
│   │   ├── execution_loop.py #   execute_loop() 执行-决策循环
│   │   ├── result_handlers.py #  5 个结果处理器
│   │   ├── replan.py       #     handle_replan() + check_meltdown()
│   │   └── sub_agent.py    #     SubAgent 类 + _dispatch_result + 薄封装
│   ├── PlanValidator      #   9 项 DAG 静态校验
│   └── Orchestrator       #   执行层：DAG 拓扑驱动
│       ├── execution.py   #     节点执行管线（execute → evaluate → apply）
│       ├── finalize.py    #     收尾 + checkpoint 工具
│       ├── Executor       #     单节点 tool-calling / LLM 直出文本
│       ├── Evaluator      #     双重判定（execution + business）
│       └── PlanGraph      #     NetworkX DiGraph + DAGPatch
├── schema/                # 核心数据结构
│   ├── types.py           #   工具函数 + NewType 别名 + 基础枚举（含 NodeErrorCode）
│   ├── protocol.py        #   跨层协议（ConstraintConfig, DelegationContext, HITLData）
│   ├── output.py          #   事件 + 查询视图（TaskEvent, TaskSummary, TokenStats）
│   ├── internal.py        #   编排数据模型（DAGNode, Plan, TaskState, NodeResult）
│   └── __init__.py        #   全部 re-export，向后兼容
├── CheckpointManager      #   任务级快照（内存版）
├── ArtifactStore          #   节点产物存储（内存版）
├── TaskMailbox            #   单播事件队列（deque + Event）
├── TokenLedger            #   write-through + per-call 审计日志
└── TaskDebugAPI           #   SSE 实时推送（内部 500ms 轮询 + diff）
    # WebUI「任务」tab 已迁移至 frontend/src/debug/components/TasksTab.vue (Vue 3 + vis-network DAG)
```

---

## v1 完成项

### 核心编排

| 组件 | 功能 | 状态 |
|------|------|------|
| Planner | `plan()` 首次规划 | ✅ |
| Replanner | `replan()` 失败修复（full_replan 双路径） | ✅ |
| PlanValidator | 9 项静态校验（含 output_as 唯一性） | ✅ |
| Executor | LLM function-calling + `__llm_direct__` 直出文本 | ✅ |
| Evaluator | ExecutionStatus + BusinessStatus 双重判定，`goal_failed + required → REPLAN` | ✅ |
| PlanGraph | NetworkX 封装：拓扑排序 / ready nodes / DAGPatch（同 ID 保留边 + 异 ID 自动重连） | ✅ |
| Orchestrator | 执行循环：找 ready → 执行 → 评估 → 重试 → replan | ✅ |
| SubAgent | 决策循环：plan → validate → execute → replan → 熔断 | ✅ |
| FIX_PLAN | PlanValidator 校验失败 → 最多 2 次带错误反馈重试规划 | ✅ |

### 生命周期

| 方法 | 功能 | 状态 |
|------|------|------|
| `start()` | 初始化并发控制 | ✅ |
| `submit()` | Fire-and-forget 任务提交 | ✅ |
| `stop(timeout)` | Graceful shutdown：等自然完成 → 超时 cancel → 兜底清理 | ✅ |
| `pause()` | 设置 `exec_state.pause_requested` → Orchestrator 检测 → PAUSED（幂等） | ✅ |
| `resume()` | CAS PAUSED→RESUMING→RUNNING，幂等 | ✅ |
| `cancel()` | 软取消——PAUSED 直接标记 / RUNNING 设标志（幂等） | ✅ |
| `submit_approval()` | v1 桥接到 ToolManager SafetyPipeline | ✅ |

### 查询 API

| 方法 | 状态 |
|------|------|
| `get_task_status()`, `get_task_mailbox()`, `get_task_summary()` | ✅ |
| `list_active_tasks()`, `get_progress()`, `get_task_dag()` | ✅ |
| `get_token_usage()`, `list_agents()`, `get_token_stats()` | ✅ |

### Debug WebUI

| 组件 | 功能 | 状态 |
|------|------|------|
| `TaskDebugAPI` | 内部 500ms 轮询 → diff → SSE 推送（snapshot/task_new/task_update/node_update/task_done/heartbeat） | ✅ |
| Server 路由 | `/debug/api/tasks` (JSON) + `/debug/api/tasks/stream` (SSE) | ✅ |
| TasksTab.vue | 「任务」tab：Vue 3 + vis-network DAG（已迁至 frontend/src/debug/） | ✅ |
| 激活条件 | `app['task_system'] = task_system` 注册后路由生效 | ✅ |

### 测试

| 层级 | 文件 | 测试数 | 覆盖 |
|------|------|--------|------|
| unit/ | `test_workflow_engine.py` | 25→40 | stop / pause / token / 查询 API |
| unit/ | `test_orchestrator.py` | 26 | 执行循环 / 重试 / 审批 / 暂停 |
| unit/ | `test_sub_agent.py` | 22→24 | 决策循环 / 熔断 / replan |
| unit/ | `test_graph.py` | 46 | PlanGraph + DAGPatch |
| unit/ | `test_evaluator.py` | 29 | 双重判定 + `_derive_action` |
| unit/ | `test_executor.py` | 12 | tool-calling + llm_direct |
| unit/ | `test_planner.py` | 17→41 | plan + replan 解析（适配 Planner/Replanner 拆分） |
| unit/ | （`test_delegation_preparer.py` 已随 DelegationPreparer 迁 `core/cognitive/tests/`） | 53 | 校验 + 格式化 |
| unit/ | `test_checkpoint.py` | 9 | |
| unit/ | `test_mailbox.py` | 10 | |
| unit/ | `test_token_ledger.py` | 8 | write-through + 审计 + 损耗 |
| integration/ | `test_workflow_engine_lifecycle.py` | 10 | stop / pause / cancel / resume / 并发 / replan |
| integration/ | `test_e2e_mvp.py` | 7 | 完整 7 链路（含 llm_direct / cancel / resume） |
| integration/ | `test_delegation_preparer.py` | 11 | LLM 工具筛选 |
| integration/ | `test_executor.py` | 4 | LLM tool-calling + llm_direct + 哨兵 |
| integration/ | `test_planner.py` | 4 | LLM DAG 生成 + replan |

### 接入 CognitiveLoop

| 项目 | 状态 |
|------|------|
| `TaskSystem` 实例创建 / `start()` | ✅ Agent.start() 中创建 |
| `submit_task` 注册为 LLM 工具 | ✅ function definition 在 tool_defs 合并 |
| `DelegationPreparer` 调用 | ✅ CognitiveLoop._handle_submit_task() |
| Mailbox 事件消费 | ✅ Agent._poll_mailboxes() 500ms 轮询 |
| Debug WebUI 路由 | ✅ `app['task_system']` 通过 create_app(task_system=...) 参数注册 |

### 内部功能

| 项目 | 状态 |
|------|------|
| Token 追踪 | ✅ TokenLedger write-through + per-call 审计日志 |
| 时间戳类型化 | ✅ `IsoTimestamp`(Annotated str + BeforeValidator) + `parse_iso()` |
| DAGNode.execution_mode | ✅ Planner 标注 llm_direct/tool_call，Executor 按标注执行 |
| 模型名可配置 | ✅ Planner 改用 `infra.llm`，统一由 `AgentInfra.llm_model` 管理 |
| 大产物截断 | Evaluator 传入完整 artifact，但使用 1M 上下文模型不构成实际风险 |
| Plan 预审批 | 设计文档有，代码无 |

---

## v2（已明确不做）

| 项目 | 说明 |
|------|------|
| Worker Agent 池 | 当前顺序执行独立节点 |
| DBT 行为树 | 降级路径：`NodeConfig(max_retries + fallback_tool + on_exhausted)` |
| File-persistent checkpoint | 当前内存版 |
| 安全护栏 (RBAC) | `safety/` 目录仅有 `__init__.py` |
| 经验记忆 (Reflector) | `learning/` 目录仅有 `__init__.py` |
| 审批风暴检测 | `_needs_approval()` 硬编码 False |

---

## 2026-06-18 目录结构重构

### Planner 拆分（5 commits）
`planner.py` (739行) → `planner/` 包：
- `prompts.py` — 5 个 prompt 常量
- `parser.py` — 共享 `call_llm()` + `parse_plan_from_dict()` + `parse_patch_from_dict()` + `PlanParseError`
- `planner.py` — `Planner` 类：`plan()` 首次规划
- `replanner.py` — `Replanner` 类：`replan()` 失败修复
- `__init__.py` — 公开导出

### Schema 拆分（1 commit）
`schema.py` (1287行) → `schema/` 包：
- `types.py` — 工具函数 + NewType 别名 + 基础枚举
- `protocol.py` — 跨层协议（ConstraintConfig, DelegationContext, HITLData）
- `output.py` — 事件 + 查询视图（TaskEvent, TaskSummary, TokenStats）
- `internal.py` — 编排数据模型（DAGNode, Plan, TaskState, NodeResult）
- `__init__.py` — 全部 re-export，向后兼容

关键修复：`NodeErrorCode` 从 internal 移入 `types.py`，切断 output ↔ internal 循环依赖。

### TaskSystem 拆分（3 commits）
`task_system.py` (843行) → 3 文件：
- `query.py` — 11 个只读查询函数（`get_progress`, `list_active_tasks`, `get_token_stats` 等）
- `lifecycle.py` — 3 个任务操作函数（`pause`, `cancel`, `submit_approval`）
- `task_system.py` (478行) — 生命周期 + 薄封装委托

所有提取函数接受显式参数替代 `self._xxx`，TaskSystem 公开 API 签名不变。

### Orchestrator 拆分（3 commits + 类型修复）
`orchestrator.py` (708行) → 3 文件：
- `execution.py` — 节点执行管线：`execute_nodes()` + `evaluate_node()` + `apply_evaluation()`
- `finalize.py` — 收尾 + checkpoint 工具（8 个函数）：`init_exec_state`, `merge_exec_state`, `has_blocked_nodes`, `needs_approval`, `finalize`, `build_checkpoint`, `save_checkpoint`, `push_mailbox`
- `orchestrator.py` (421行) — `run()` 主循环 + 薄封装

`apply_evaluation()` 后续拆分为独立 handler（`40d64d5`），`run()` 方法独立拆分（`325d37c`）。薄封装签名与当前方法完全一致，`run()` 调用点零改动。后续补全了全部类型注解。

### apply_evaluation 拆分（1 commit）
`execution.py` 的 `apply_evaluation()` (150行) → 按 action 提取：
- `_handle_continue` / `_handle_skip` / `_handle_retry` / `_handle_replan_progress`
- `_push_progress` 共用 helper（4 分支复用 progress 事件构造）
- ASK_APPROVAL / ABORT 保留内联（各 3 行）

### SubAgent 拆分（1 commit）
`sub_agent.py` (825行) → `sub_agent/` 包：
- `planning.py` — `plan_and_validate()` 规划+校验
- `execution_loop.py` — `execute_loop()` 执行-决策循环
- `result_handlers.py` — 5 个结果处理器
- `replan.py` — `handle_replan()` + `check_meltdown()` + `extract_node_results()`
- `sub_agent.py` (403行) — `SubAgent` 类 + `_dispatch_result` + 薄封装

### 项目目录整理（2 commits）
- `core/test_*.py` ×3 → `core/tests/` 归位
- `code-review-report.md` → 仓库顶层 `docs/`、`app_log.txt` → `log/app.log`
- 清理 17 个零字节 `logger_*.log` 测试残留

---

## 2026-06-22 Agent/CognitiveLoop 重构

### Agent 类内拆分（4 commits）
`core/agent.py` (738行→520行) 按职责提取私有方法：
- `start()` (145行→35行)：拆分为 `_start_persistence`、`_start_token_and_prompt`、`_start_tool_context`、`_start_worker_pool`、`_start_idle_and_events`、`_start_tts`、`_start_task_system`、`_start_mailbox_polling`、`_print_startup_banner` 共 9 个方法
- `stop()` (105行→42行)：拆分为 `_flush_consolidator`、`_stop_event_task`、`_stop_cognitive_and_tts`、`_stop_embedding_if_running`、`_stop_mailbox`、`_stop_task_system`、`_stop_scheduler`、`_stop_tool_manager`、`_stop_idle`、`_stop_persistence`、`_stop_llm_and_tts`、`_stop_output` 共 12 个方法
- `_process_events()` (100行→28行)：拆分为 `_handle_interrupt`、`_handle_user_input`、`_handle_system_tick`、`_handle_task_result`、`_handle_state_change` 共 5 个 handler

### Agent bug 修复（1 commit）
- `_config_path` 为 None 时重启不再崩溃，降级复用当前配置
- `LLMAdapter.get_raw_client()` 替代 `self._llm._backend._client` 私有属性链
- `OutputManager.notify_state()` 替代 `self._output._channels` 直接访问
- `_poll_mailboxes` 内层 `while True` 改为 walrus `while (event := ...) is not None`
- `_EventBusHITLNotifier.cancel_approval` 补充注释

### CognitiveLoop prompt 修复（1 commit）
- `Brain.j2` ToolUsageGuidance 新增请求分类规则：Type A (复杂多步) → submit_task 立即委托，Type B (简单) → DIY
- `submit_task` function definition 强调后台有自己的 LLM，无需调用前做准备工作
- `_build_wake_up_message` 注入当前时间 + 周几，避免 LLM 时间幻觉 (凌晨说"今早")

---

## 历史修复记录

### 正确性 bug（2026-06-16）

| # | 文件 | 问题 |
|---|------|------|
| 1 | `sub_agent.py` | `_task_state` 在 `run()` 初始化前被访问 → AttributeError 竞态 |
| 2 | `orchestrator.py`, `task_system.py` | `CancelledError` 不被 `except Exception` 捕获 |
| 3 | `orchestrator.py` | 重试差一错误：`max_retries=3` 只重试 2 次 |
| 4 | `executor.py` | 空 tool_defs 阻断 `__llm_direct__` 直出文本路径 |
| 5 | `task_system.py` | `_cleanup_storage` 异常导致信号量泄漏 |
| 7 | `plan_validator.py` | `output_as` 唯一性未校验 |

### CognitiveLoop 异步任务——e2e 调试修复（2026-06-18）

| # | 文件 | 问题 | 修复 |
|---|------|------|------|
| 1 | `cognitive_loop.py` | `self._session.messages` 不存在 | → `self._session.recent_messages()` |
| 2 | `cognitive_loop.py` | `user_profile=None` → `.model_dump()` 崩溃 | → `UserProfile()` 默认空实例 |
| 3 | `cognitive_loop.py` | assistant_entry 缺少 submit_task 的 tool_calls | normalized 保留 submit_task，只 dispatch other_tcs |
| 4 | `cognitive_loop.py` | tool result 先于 assistant_entry | 调整顺序 |
| 5 | `Brain.j2` | ToolUsageGuidance 缺少 submit_task | 加入说明 |
| 6 | `planner.py` | `dict.get("output_as", "")` 对 `null` 返回 None | → `(d.get("output_as") or "").strip()` |
| 7 | `executor.py` | `max_tokens=2048` 不足 | → `4096` |
| 8 | `server.py` | Debug SSE 断连 | `create_app(task_system=...)` 参数注入 |
| 9 | `orchestrator.py` | COMPLETED 事件无产出摘要 | 加入 `node_summaries` |

### 时间戳类型化（2026-06-18）

- 新增 `IsoTimestamp`(Annotated str + BeforeValidator) + `parse_iso()` + `_normalize_utc()`
- 所有 timestamp 字段从 `str` 改为 `IsoTimestamp`
- 4 处 `fromisoformat+replace(tzinfo)` 样板代码 → `parse_iso()`
- `now_iso()` 格式统一为 `Z` 后缀
