# TaskSystem 项目进度

> 日期: 2026-06-16  
> 状态: v1 核心完成，待接入 CognitiveLoop

---

## 总览

| 指标 | 数值 |
|------|------|
| 生产代码 | 20 文件, ~4,500 行 |
| 测试代码 | 21 文件 |
| 单元测试 | **252 tests**, ~6s |
| 集成测试 | 26 tests (含 4 e2e), ~4min |
| 最近提交 | `8e6ab1d` — 6 个正确性 bug 修复 |

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
| `pause()` | 设置 `exec_state.pause_requested` → Orchestrator 检测 → PAUSED | ✅ |
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
| unit/ | `test_task_system.py` | 10 | stop + pause |
| unit/ | `test_orchestrator.py` | 26 | 执行循环 / 重试 / 审批 / 暂停 |
| unit/ | `test_sub_agent.py` | 22 | 决策循环 / 熔断 / replan |
| unit/ | `test_graph.py` | 21 | PlanGraph + DAGPatch |
| unit/ | `test_evaluator.py` | 28 | 双重判定 + `_derive_action` |
| unit/ | `test_executor.py` | 8 | tool-calling + llm_direct |
| unit/ | `test_planner.py` | 17 | plan + replan 解析 |
| unit/ | `test_delegation_preparer.py` | 16 | 校验 + 格式化 |
| unit/ | `test_checkpoint.py` | 5 | |
| unit/ | `test_mailbox.py` | 4 | |
| integration/ | `test_task_system_lifecycle.py` | 5 | stop / pause / 并发 / goal_failed→REPLAN |
| integration/ | `test_e2e_mvp.py` | 4 | 完整 4 链路 |
| integration/ | `test_delegation_preparer.py` | 10 | LLM 工具筛选 |
| integration/ | `test_executor.py` | 2 | LLM tool-calling |
| integration/ | `test_planner.py` | 5 | LLM DAG 生成 |

---

## v1 未完成项

### 生命周期

| 方法 | 当前状态 |
|------|----------|
| `resume()` | ⚠️ `NotImplementedError` |
| `cancel()` | ⚠️ `NotImplementedError` |
| `submit_approval()` | ⚠️ `NotImplementedError` |

### 查询 API

| 方法 | 当前状态 |
|------|----------|
| `list_active_tasks()` | ✅ 已实现 |
| `get_task_summary()` | ✅ 已实现 |
| `get_task_dag()` | ✅ 已实现 |
| `get_task_status()` | ⚠️ `NotImplementedError` |
| `get_progress()` | ⚠️ `NotImplementedError` |
| `get_token_usage()` | ⚠️ `NotImplementedError` |
| `list_agents()` | ⚠️ `NotImplementedError` |
| `get_token_stats()` | ⚠️ `NotImplementedError` |

### 接入 CognitiveLoop（最大缺口）

TaskSystem 完全未接入 CognitiveLoop——`core/` 目录没有任何 TaskSystem 引用：

1. ❌ `TaskSystem` 实例未创建 / `start()` 未调用
2. ❌ `submit_task` 未注册为 LLM 工具——LLM 无法委派后台任务
3. ❌ `DelegationPreparer` 未被 CognitiveLoop 调用
4. ❌ Mailbox 事件无人消费——任务完成后不通知用户
5. ❌ Debug WebUI 路由未激活（`app['task_system']` 未赋值）

### 内部功能缺口

| 项目 | 状态 |
|------|------|
| Token 追踪 | 类型定义已完（`TokenComponent`），采集未实现 |
| 大产物截断 | Evaluator 传入完整 artifact，可能超出 LLM 上下文 |
| Plan 预审批 | 设计文档有，代码无 |
| 模型名可配置 | `TASK_SYSTEM_MODEL` env var 已支持，但 Planner 仍硬编码 DeepSeek 客户端 |

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

### 文档

- architecture.md：21 处与代码不一致修复
- api.md：SSE 事件名、方法签名、实现状态同步
- evaluator.py：`_derive_action` docstring 更新（CONTINUE → REPLAN）
