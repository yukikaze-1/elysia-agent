# TaskSystem 项目进度

> 日期: 2026-06-17  
> 状态: v1 核心完成，查询 API 全部就绪，待接入 CognitiveLoop

---

## 总览

| 指标 | 数值 |
|------|------|
| 生产代码 | 22 文件, ~5,000 行 |
| 测试代码 | 21 文件 |
| 单元测试 | **298 tests**, ~4.3s |
| 集成测试 | 36 tests (含 7 e2e), ~15min |
| 最近提交 | `78cfc59` — cancel e2e 测试改进 |

---

## 架构

```
TaskSystem
├── DelegationPreparer    # LLM 工具筛选 + 上下文准备
├── TaskSystem.submit()   # fire-and-forget 入口
├── SubAgent (per-task)   # 决策层：规划 → 校验 → 执行循环
│   ├── Planner           #   plan() 首次规划 / replan() 失败修复
│   ├── PlanValidator     #   9 项 DAG 静态校验
│   └── Orchestrator      #   执行层：DAG 拓扑驱动
│       ├── Executor      #     单节点 tool-calling / LLM 直出文本
│       ├── Evaluator     #     双重判定（execution + business）
│       └── PlanGraph     #     NetworkX DiGraph + DAGPatch
├── CheckpointManager     #   任务级快照（内存版）
├── ArtifactStore         #   节点产物存储（内存版）
├── TaskMailbox           #   单播事件队列（deque + Event）
├── TokenLedger           #   write-through + per-call 审计日志
├── TaskDebugAPI          #   SSE 实时推送（内部 500ms 轮询 + diff）
└── debug.html            #   WebUI「任务」tab（EventSource 消费）
```

---

## v1 完成项

### 核心编排

| 组件 | 功能 | 状态 |
|------|------|------|
| Planner | `plan()` 首次规划 + `replan()` 失败修复（full_replan 双路径） | ✅ |
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
| `start()` | 初始化所有组件 + 并发控制 | ✅ |
| `submit()` | Fire-and-forget 任务提交 | ✅ |
| `stop(timeout)` | Graceful shutdown：等自然完成 → 超时 cancel → 兜底清理 | ✅ |
| `pause()` | 设置 `exec_state.pause_requested` → Orchestrator 检测 → PAUSED（幂等） | ✅ |
| `_cleanup_task()` | 注册表 + asyncio.Task 引用清理 | ✅ |

### Debug WebUI

| 组件 | 功能 | 状态 |
|------|------|------|
| `TaskDebugAPI` | 内部 500ms 轮询 → diff → SSE 推送（snapshot/task_new/task_update/node_update/task_done/heartbeat） | ✅ |
| Server 路由 | `/debug/api/tasks` (JSON) + `/debug/api/tasks/stream` (SSE) | ✅ |
| debug.html | 「任务」tab：EventSource 消费 SSE，ASCII DAG 渲染，状态 emoji + 进度条 | ✅ |
| 激活条件 | `app['task_system'] = task_system` 注册后路由生效 | ⏳ |

### 测试

| 层级 | 文件 | 测试数 | 覆盖 |
|------|------|--------|------|
| unit/ | `test_task_system.py` | 25 | stop / pause / token / 查询 API |
| unit/ | `test_orchestrator.py` | 26 | 执行循环 / 重试 / 审批 / 暂停 |
| unit/ | `test_sub_agent.py` | 22 | 决策循环 / 熔断 / replan |
| unit/ | `test_graph.py` | 21 | PlanGraph + DAGPatch |
| unit/ | `test_evaluator.py` | 28 | 双重判定 + `_derive_action` |
| unit/ | `test_executor.py` | 8 | tool-calling + llm_direct |
| unit/ | `test_planner.py` | 17 | plan + replan 解析 |
| unit/ | `test_delegation_preparer.py` | 16 | 校验 + 格式化 |
| unit/ | `test_checkpoint.py` | 5 | |
| unit/ | `test_mailbox.py` | 4 | |
| unit/ | `test_token_ledger.py` | 8 | write-through + 审计 + 损耗 |
| integration/ | `test_task_system_lifecycle.py` | 10 | stop / pause / cancel / resume / 并发 / replan |
| integration/ | `test_e2e_mvp.py` | 7 | 完整 7 链路（含 llm_direct / cancel / resume） |
| integration/ | `test_delegation_preparer.py` | 11 | LLM 工具筛选 |
| integration/ | `test_executor.py` | 4 | LLM tool-calling + llm_direct + 哨兵 |
| integration/ | `test_planner.py` | 5 | LLM DAG 生成 + replan |

---

## v1 未完成项

### 生命周期

| 方法 | 当前状态 |
|------|----------|
| `resume()` | ✅ 已实现 |
| `cancel()` | ✅ 已实现 |
| `submit_approval()` | ✅ 已实现（v1 桥接到 ToolManager SafetyPipeline） |

### 查询 API

| 方法 | 当前状态 |
|------|----------|
| `list_active_tasks()` | ✅ 已实现 |
| `get_task_summary()` | ✅ 已实现 |
| `get_task_dag()` | ✅ 已实现 |
| `get_task_status()` | ✅ 已实现 |
| `get_progress()` | ✅ 已实现 |
| `get_token_usage()` | ✅ 已实现 |
| `list_agents()` | ✅ 已实现 |
| `get_token_stats()` | ✅ 已实现 |

### 接入 CognitiveLoop

| 项目 | 状态 |
|------|------|
| `TaskSystem` 实例创建 / `start()` | ✅ Agent.start() 中创建 |
| `submit_task` 注册为 LLM 工具 | ✅ function definition 在 tool_defs 合并 |
| `DelegationPreparer` 调用 | ✅ CognitiveLoop._handle_submit_task() |
| Mailbox 事件消费 | ✅ Agent._poll_mailboxes() 500ms 轮询 |
| Debug WebUI 路由 | ⏳ `app['task_system']` 注册（后续） |

### 内部功能缺口

| 项目 | 状态 |
|------|------|
| Token 追踪 | ✅ 已实现（TokenLedger write-through + per-call 审计日志） |
| 大产物截断 | Evaluator 传入完整 artifact，可能超出 LLM 上下文 |
| Plan 预审批 | 设计文档有，代码无 |
| 模型名可配置 | ✅ 已修复——Planner 改用 `infra.llm`，模型名统一由 `AgentInfra.llm_model` 管理 |
| DAGNode.execution_mode | ✅ 已实现——Planner 标注 llm_direct/tool_call，Executor 按标注执行，根治误选工具 |
| 时间戳类型化 | `created_at`/`updated_at` 全为裸 `str`，try/except+replace(tzinfo) 模式重复 10+ 处。后续用 Pydantic `IsoTimestamp`(Annotated str + BeforeValidator 归一化 UTC) + `parse_iso()` 消除 |

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
| Debug WebUI 激活 | 等待 `app['task_system']` 注册 |

---

## 本周修复

### 正确性 bug（2026-06-16）

| # | 文件 | 问题 |
|---|------|------|
| 1 | `sub_agent.py` | `_task_state` 在 `run()` 初始化前被访问 → AttributeError 竞态 |
| 2 | `orchestrator.py`, `task_system.py` | `CancelledError` 不被 `except Exception` 捕获 → 关闭时 mailbox 永久挂起 |
| 3 | `orchestrator.py` | 重试差一错误：`max_retries=3` 只重试 2 次 |
| 4 | `executor.py` | 空 tool_defs 阻断 `__llm_direct__` 直出文本路径 |
| 5 | `task_system.py` | `_cleanup_storage` 异常导致信号量泄漏 |
| 7 | `plan_validator.py` | `output_as` 唯一性未校验 → ArtifactStore 静默覆盖 |

### 代码质量

- `_exec_state` 初始化为 None（`__init__`），消除 `try/except AttributeError`
- `_task_state` / `_graph` 初始化为 None（`__init__`），消除竞态条件
- `push_mailbox_event` / `TaskErrorCode` / `TaskEventType` 提升为顶层 import
- pytest 目录重构：`tests/core/` → `tests/unit/` + `tests/integration/`
- TokenLedger 设计为 per-task 组件隔离：每个 SubAgent 持有独立 `TokenLedger` 实例，记录本级 LLM 调用 token 消耗（含 `purpose` / `node_id` 标签），子任务 token 不计入父级

### 文档

- architecture.md：21 处与代码不一致修复
- api.md：SSE 事件名、方法签名、实现状态同步
- evaluator.py：`_derive_action` docstring 更新（CONTINUE → REPLAN）

### 查询 API + 集成测试修复（2026-06-17）

**查询 API 补全**：

| # | 方法 | 说明 |
|---|------|------|
| 1 | `get_task_status()` | 查询任务当前状态，不存在时 raise KeyError |
| 2 | `get_progress()` | 进度快照（current_node_id + 完成数/总数 + 耗时 + 线性预估剩余） |
| 3 | `list_agents()` | 活跃 Sub-Agent 列表，TaskStatus→AgentStatus 自动映射 |

**集成测试修复**：

| # | 文件 | 问题 |
|---|------|------|
| 1 | `test_planner.py` | `Planner()` 无参调用——适配构造函数 `Planner(infra=make_infra())` |
| 2 | `test_task_system_lifecycle.py` | token 竞态——`get_token_usage()` 清理后 None → 从 COMPLETED 事件 data 读取 |
| 3 | `test_task_system_lifecycle.py` | `test_goal_failed_triggers_replan` mock 签名多了 `self`（patch 不自动传 self） |
| 4 | 同上 | 完整套件中 LLM 限速 → `max_failure_count=2` + deadline 300s |

### cancel() 实现（2026-06-17）

| # | 文件 | 说明 |
|---|------|------|
| 1 | `task_system.py` | `cancel()` 实现——幂等软取消，设置 `exec_state.cancel_requested` |
| 2 | `orchestrator.py` | Orchestrator 安全点检测 `cancel_requested` → 复写节点为 CANCELLED |
| 3 | `sub_agent.py` | SubAgent 检测取消 → 推送 CANCELLED 事件 → FAILED_CANCELLED 终态 |
| 4 | `test_cancel.py` | 6 个单元测试——幂等/已终态/未启动/运行时取消/暂停后取消/all_finished |
| 5 | `test_cancel_integration.py` | 1 个集成测试——运行时取消 + 验证 CANCELLED 事件 |

### resume() 实现 + cancel/pause 中断信号修复（2026-06-17）

| # | 文件 | 说明 |
|---|------|------|
| 1 | `task_system.py` | `resume()` 实现——CAS PAUSED→RESUMING→RUNNING，幂等，PAUSED 不清理 |
| 2 | `sub_agent.py` | `resume_from_pause()` + 保存 `_context`/`_mailbox` + `_execute` 参数化 |
| 3 | `sub_agent.py` | cancel/pause 信号 SubAgent 层拦截——`_handle_replan` 前 + `_execute` 循环顶 |
| 4 | `sub_agent.py` | resume 时清零 checkpoint 的 pause/cancel_requested 标志（防立即重触发） |
| 5 | `plan_validator.py` | required 节点 output_as=null 自动填充 `output:<node_id>` |
| 6 | `orchestrator.py` | RESUMING→RUNNING 转换 |
| 7 | `planner.py` | Replan prompt 补充 execution_mode 规则 |

### submit_approval() 实现 + pause 幂等 + code review 修复（2026-06-17）

| # | 文件 | 说明 |
|---|------|------|
| 1 | `task_system.py` | `submit_approval()` v1 桥接到 ToolManager.submit_approval() |
| 2 | `task_system.py` | `pause()` 幂等——移除 RuntimeError/KeyError，所有路径静默成功 |
| 3 | 多文件 | code review #1-#10 修复（tools_used 语义/资源泄漏/重复事件等）|
| 4 | `utils.py` | 新建——`strip_markdown_fence()` + `format_tools()` 公共函数 |

### CognitiveLoop 拆分 + IDE 类型错误修复（2026-06-17）

**CognitiveLoop 拆分**：

| # | 文件 | 说明 |
|---|------|------|
| 1 | `cognitive_loop.py` | 815→612 行，移除 block filter/TTS/tool_call parser/text utils |
| 2 | `block_filter.py` | 新建——`BlockFilter` 流式块过滤 + `build_parts_with_blocks` 块解析 |
| 3 | `tts_stream.py` | 新建——`TTSStreamManager` 类，管理 TTS 流式播放 + emoji 排队 |
| 4 | `tool_call_parser.py` | 新建——`merge_stream_tool_calls` + `normalize_tool_calls` 纯函数 |
| 5 | `text_utils.py` | 新建——`content_text_from_content` + `MessageObj` |
| 6 | `agent.py` | `_tts`/`_token_tracker`/`_prompt_recorder` 私有属性访问 → 公开 setter |

**IDE 类型错误修复**：

| # | 文件 | 问题 | 修复 |
|---|------|------|------|
| 1 | `sub_agent.py` | `_checkpoint_mgr` 可空直接调 `.load()` | 加 `assert ... is not None` |
| 2-6 | `task_system.py` | `_orchestrator._exec_state` 5 处可空访问 | 加 orchestrator None 守卫或三元表达式 |
| 7 | `task_system.py` | `tz.utc` 未定义 | → `timezone.utc` |
| 8 | `tts/protocol.py` | `async def synthesize_stream` 协程类型问题 | 改为 `def`（非 async），子类仍可 `async def` |

### CognitiveLoop 异步任务支持（2026-06-17）

| # | 文件 | 说明 |
|---|------|------|
| 1 | `schema.py` | FSM 加 `IDLE_BACKGROUND_TASK` + EventBus 加 `TASK_RESULT` |
| 2 | `cognitive_loop.py` | `_handle_submit_task()` 拦截 + `_submit_task_definition()` + 4 setter |
| 3 | `agent.py` | TaskSystem 生命周期 + `_poll_mailboxes()` + TASK_RESULT 处理 |
| 4 | `protocol.py` | `ToolResult.async_task_id` 字段 |
| 5 | `task_system.py` | `get_task_mailbox()` 查询方法 |
