# Test Coverage Gaps — Design

## Context

todo.md 记录了 7 项测试覆盖率缺口，经分析后：

- **3 项死代码/trivial**：不补测试，在 todo.md 标注说明
- **4 项实施修复**：`_flush_after_response`、FSM transitions、`_prune_tasks()`、conftest.py MagicMock

## 不补测试的项

| 项 | 原因 | todo.md 处理 |
|----|------|-------------|
| `dispatch_background()` | 死代码，唯一引用是注释说"不要用这个" | 标注死代码 |
| `execute_stream()` | 一行 pass-through 委托，无独立逻辑 | 标注 trivial |
| EventBus `drain()` 竞态 | `drain()` 从未被调用，死代码 | 标注死代码 |

## 修复项

### 1. `_flush_after_response` — 新增 `core/cognitive/tests/test_flush.py`

5 个测试场景：

1. **空列表直接返回** — `_pending_after_response = []`
2. **全部已完成直接返回** — 放入已 done 的 task
3. **正常完成** — `asyncio.sleep(0)` task，等待完成
4. **超时取消** — `asyncio.sleep(999)` + `timeout=0.01`，验证 task.cancel()
5. **异常捕获** — raise RuntimeError 的 task，验证不崩溃 + 日志警告

使用 `MagicMock` 配置 `CognitiveLoop._pending_after_response`。

### 2. FSM 未测转换 — 追加到 `core/agent/tests/test_agent_fsm.py`

新增 10 条 transition 测试 + 3 条 `interrupt()` 路径：

```python
@pytest.mark.parametrize("from_state, to_state", [...])
def test_transition(fsm, from_state, to_state):
    fsm._state = from_state
    fsm.transition(to_state)
    assert fsm.state == to_state
```

以及 `interrupt()` 从 EXECUTING/SPEAKING/IDLE_BACKGROUND_TASK 的覆盖。

### 3. `_prune_tasks()` — 追加到 `core/task/tests/test_task_scheduler.py`

纯同步方法，4 个场景：

- 终端无活跃子 → 删除
- 活跃任务 → 保留
- 终端有活跃子 → 保留
- 全部终端 → 全部删除

直接构造 task dict 操作 `scheduler._tasks`。

### 4. `conftest.py` MagicMock — 改用 `PropertyMock`

```python
from unittest.mock import PropertyMock
type(a._fsm).state = PropertyMock(return_value=AgentState.IDLE)
```

改后跑 `core/agent/tests/` + `core/task/tests/`，修冒出来的假通过测试。
