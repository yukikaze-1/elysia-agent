# 生产代码 assert 替换

> 2026-07-30 · 设计文档

## 目标

移除生产代码中所有 `assert` 语句（`__main__.py` 冒烟测试除外），替换为适当的运行时错误处理，确保 `python -O` 下不丢失守卫逻辑。

## 原则

- **B 方案：分情况处理。** 不可恢复的编程错误 → `RuntimeError` 冒泡；IO 边界/异常路径 → 防御式忽略。
- **不改变控制流语义。** assert 之前的行为和之后的行为保持一致，只改变失败时的错误类型（`AssertionError` → `RuntimeError`）或静默跳过（防御式）。
- **保持现有日志模式。** 每个文件使用已有的 logger 或项目约定的日志方式。

## 范围

| 类别 | 数量 | 策略 |
|------|------|------|
| 防御式忽略 | 5 | `if x: do()` / 返回 500 |
| RuntimeError | 24 | `if x is None: raise RuntimeError("msg")` |
| 冒烟测试（不碰） | 20 | `Module/tool/__main__.py` |

---

## 具体变更

### A. 防御式忽略（5 处）

#### `Module/task_system/task_system.py`（3 处）

行 251、382、390：`assert self._semaphore is not None` → `if self._semaphore: self._semaphore.release()`

这些在 finally / except 清理路径中。如果 semaphore 为 None（说明 start 阶段就失败了），release 没意义，跳过即可。

#### `Module/task_system/lifecycle.py`（1 处）

行 153：同样模式，`assert semaphore is not None` → `if semaphore: semaphore.release()`

#### `core/input/debug_routes/character.py`（2 处）

行 136、187：`assert result is not None` → `if result is None: return web.json_response({"error": "内部错误"}, status=500)`

这是 debug API handler，返回 500 比 crash 整个进程合理。

---

### B. RuntimeError（24 处）

#### `core/cognitive/tools.py`（3 处）

行 69-70：`assert self._tool_executor/adapter is not None` → `if ... is None: raise RuntimeError("...未初始化")`

行 275：`assert prep_result.context is not None` → `if prep_result.context is None: raise RuntimeError("submit_task: DelegationPreparer 返回 ok 但 context 为空")`

#### `core/cognitive/loop.py`（2 处）

行 334、565：`assert self._clock is not None` → `if self._clock is None: raise RuntimeError("TimingClock 未初始化")`

#### `core/task/scheduler.py`（1 处）

行 401：`assert self._tool_executor is not None` → `if self._tool_executor is None: raise RuntimeError("ToolExecutor 未注入，请先调用 Scheduler.start()")`

#### `Module/task_system/task_system.py`（1 处）

行 184：`assert self._semaphore is not None` → `if self._semaphore is None: raise RuntimeError("TaskSystem 信号量未初始化，请先调用 start()")`

#### `Module/task_system/core/plan_validator.py`（1 处）

行 99：`assert max_nodes is not None` → `if max_nodes is None: raise RuntimeError("max_dag_nodes 在 resolve() 后不应为 None")`

#### `Module/task_system/core/sub_agent/replan.py`（1 处）

行 165：`assert task_state.plan is not None` → `if task_state.plan is None: raise RuntimeError("NEEDS_REPLAN 时 plan 不应为 None")`

#### `Module/task_system/core/sub_agent/sub_agent.py`（13 处）

这是最密集的文件。抽取一个私有 helper 减少重复：

```python
def _require(self, *attrs: str) -> None:
    """确保 run() 已调用——所有入口方法的前置条件。"""
    for attr in attrs:
        if getattr(self, attr, None) is None:
            raise RuntimeError(
                f"SubAgent.{attr} 未初始化，请先调用 run()"
            )
```

调用替换：

| 行 | 当前 | 改为 |
|----|------|------|
| 209 | `assert self._checkpoint_mgr is not None, "run() 必须先于..."` | `self._require("_checkpoint_mgr")` |
| 210 | `assert self._mailbox is not None, "run() 必须先于..."` | `self._require("_mailbox")` |
| 222 | `assert self._context is not None, "run() 必须先于..."` | `self._require("_context")` |
| 229 | `assert mailbox is not None, "run() 必须先于..."` | `if mailbox is None: raise RuntimeError(...)` |
| 306 | `assert self._planner is not None` | `self._require("_planner")` |
| 307 | `assert self._plan_validator is not None` | `self._require("_plan_validator")` |
| 332 | `assert self._orchestrator is not None` | `self._require("_orchestrator")` |
| 403 | `assert self._checkpoint_mgr is not None` | `self._require("_checkpoint_mgr")` |
| 431 | `assert self._replanner is not None` | `self._require("_replanner")` |
| 432 | `assert self._plan_validator is not None` | `self._require("_plan_validator")` |
| 433 | `assert self._orchestrator is not None` | `self._require("_orchestrator")` |
| 434 | `assert self._checkpoint_mgr is not None` | `self._require("_checkpoint_mgr")` |
| 451 | `assert task_state.plan is not None, "熔断检查前..."` | `if task_state.plan is None: raise RuntimeError("熔断检查前 plan 必须已赋值")` |

> 行 229 的 `mailbox` 是局部变量而非 `self.*`，不适用 `_require()`，单独 if/raise。

---

## 验证

```bash
# 确认不含残留 assert（排除测试和 __main__.py）
grep -rn "^\s*assert " --include="*.py" \
  | grep -v "/tests/" | grep -v "test_" | grep -v "__pycache__" \
  | grep -v "conftest.py" | grep -v "/__main__.py" | grep -v "site-packages"
# 预期：空输出

# 全量测试
make test-quick
```

## 不做

- `Module/tool/__main__.py` 的 20 处 assert 保持不动（冒烟测试文件）
- 不改变任何函数的签名或返回值类型
- 不引入新的依赖
