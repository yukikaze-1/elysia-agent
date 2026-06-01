# TaskScheduler 测试文档

## 概述

TaskScheduler 是 AI 任务的 Worker Pool，被 CognitiveLoop 调用执行工具子任务和后台任务。
测试覆盖三个层次：Task 模型单元测试、TaskScheduler 单元测试、Agent 集成测试。

## 测试架构

```
测试层次          被测组件               mock 范围
─────────────────────────────────────────────────────
模型层            Task dataclass         无 (纯数据)
调度层            TaskScheduler          ToolRegistry (mock tools)
集成层            Agent → Scheduler      LLM (FakeLLMBackend)
```

## 运行测试

```bash
# 模型 + 调度层
python -m pytest core/tests/test_task_model.py core/tests/test_task_scheduler.py -v

# 全部测试 (含集成)
python -m pytest core/tests/ -v

# 单文件
python -m pytest core/tests/test_task_scheduler.py -v --tb=short
```

## 测试清单

### 一、Task 模型 (test_task_model.py, 14 tests)

| # | 测试 | 覆盖 |
|---|------|------|
| 1 | `test_new_tool_call_has_valid_id` | 工厂方法，ID 生成 |
| 2 | `test_new_tool_call_status_is_pending` | 初始状态 |
| 3 | `test_new_tool_call_default_priority` | 默认优先级=5 |
| 4 | `test_can_retry_success` | 可重试条件: retry_count < max_retries |
| 5 | `test_can_retry_exceeded` | 重试次数超限 |
| 6 | `test_can_retry_wrong_status` | 已完成任务也可重试 |
| 7 | `test_retry_backoff_first` | 退避: 2^0 = 1s |
| 8 | `test_retry_backoff_second` | 退避: 2^1 = 2s |
| 9 | `test_retry_backoff_third` | 退避: 2^2 = 4s |
| 10 | `test_retry_backoff_capped` | 退避上限 30s |
| 11 | `test_roundtrip_dict` | to_dict/from_dict 序列化 |
| 12 | `test_touch_updates_timestamp` | touch() 更新时间戳 |
| 13 | `test_chaining_children` | parent/children 双向链接 |
| 14 | `test_depends_on_reserved` | DAG 依赖预留字段 |

### 二、TaskScheduler 调度层 (test_task_scheduler.py, 26 tests)

#### 2.1 提交与分发 (7)

| # | 测试 | 覆盖 |
|---|------|------|
| 1 | `test_submit_and_dispatch` | 单任务提交→执行→Future 返回 |
| 2 | `test_dispatch_tool_calls_parallel` | 3 工具并行 dispatch + 内容验证 |
| 3 | `test_priority_ordering` | 高优先级先于低优先级执行 |
| 4 | `test_worker_pool_saturation` | 10 任务 > 5 worker，全部完成 |
| 5 | `test_dispatch_tool_calls_mixed_results` | 混合：1 成功 + 1 失败 + 1 异常 |
| 6 | `test_dispatch_tool_calls_unknown_tool` | 工具未注册 → ToolResult.fail |
| 7 | `test_dispatch_tool_calls_empty` | 空 calls 列表 → 空结果 |

#### 2.2 取消 (5)

| # | 测试 | 覆盖 |
|---|------|------|
| 8 | `test_cancel_removes_future` | cancel → Future 传播 CancelledError |
| 9 | `test_cancel_cascades_to_children` | 三级级联取消 |
| 10 | `test_cancel_during_execution` | 执行中 cancel → 等 DISPATCHED 后取消 |
| 11 | `test_dispatch_tool_calls_cancelled` | dispatch CancelledError → 子任务清理 |
| ~~12~~ | (merged into cancel test) | — |

#### 2.3 重试 (2)

| # | 测试 | 覆盖 |
|---|------|------|
| 12 | `test_tool_retries_on_failure` | 首次异常→重试→成功 |
| 13 | `test_tool_gives_up_after_max_retries` | 超过 max_retries → 返回 failed |

#### 2.4 查询 (3)

| # | 测试 | 覆盖 |
|---|------|------|
| 14 | `test_get_tree` | get_tree() 递归构建任务树 |
| 15 | `test_get_tree_nested` | 三层嵌套 |
| 16 | `test_get_tree_nonexistent` | 查不存在 ID → {} |

#### 2.5 Checkpoint (4)

| # | 测试 | 覆盖 |
|---|------|------|
| 17 | `test_checkpoint_save_and_restore` | 单 BACKGROUND 持久化+恢复+try/finally |
| 18 | `test_checkpoint_multiple_tasks` | 多 BACKGROUND + try/finally |
| 19 | `test_checkpoint_ignores_non_background` | 工具不持久化 + try/finally |
| 20 | `test_checkpoint_no_path` | 无路径静默跳过 |

### 三、Agent 集成层 (test_task_scheduler.py, 6 tests)

| # | 测试 | 覆盖 |
|---|------|------|
| 21 | `test_cognitive_loop_callback_wired` | process() + callback 传递验证 |
| 22 | `test_cognitive_loop_no_callback_backward_compat` | 无 callback 向后兼容 |
| 23 | `test_agent_scheduler_lifecycle` | Agent start/stop → Scheduler 启停 |
| 24 | `test_agent_fifo_queue` | 忙时消息排队→空闲时出队 |
| 25 | `test_agent_interrupt_clears_queue` | INTERRUPT → 清空排队消息 |
| 26 | `test_scheduler_state_in_debug_info` | DebugInfo 包含 scheduler 状态 |

## Mock 工具

```python
# 成功工具
async def echo(msg: str = "", ctx=None):
    return ToolResult.ok(name="echo", content=msg, duration_ms=0.0)

# 失败工具  
async def failing_tool(ctx=None):
    return ToolResult.fail(name="failing", error="always fails")

# 异常工具
async def crashing_tool(ctx=None):
    raise RuntimeError("crash")

# 慢工具 (测试执行中取消)
async def slow_tool(ctx=None):
    await asyncio.sleep(10)
    return ToolResult.ok(name="slow", content="done", duration_ms=0.0)

# 重试工具
class FlakyTool:
    def __init__(self, fail_count=1):
        self.call_count = 0
        self.fail_count = fail_count
    
    async def __call__(self, ctx=None):
        self.call_count += 1
        if self.call_count <= self.fail_count:
            raise RuntimeError("临时错误")
        return ToolResult.ok(name="flaky", content="ok", duration_ms=0.0)
```

## FakeLLM 集成

```python
from core.tests.fake_llm import FakeLLMBackend

# 模拟 LLM 返回 tool_call JSON
tool_json = json.dumps({
    "tool_calls": [{
        "id": "t1",
        "type": "function",
        "function": {"name": "echo", "arguments": '{"msg":"hello"}'}
    }]
})
llm = LLMAdapter(_test_backend=FakeLLMBackend(response=tool_json))
```
