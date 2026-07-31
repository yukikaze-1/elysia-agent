# TaskSystem pause 超时确认设计

> 日期: 2026-06-22
> 状态: 设计

## 问题

`pause()` 的 `async def` 内部没有 `await`——设完 `pause_requested` 标志立即返回。
调用方不知道暂停何时（或是否）生效。如果当前节点在执行长 LLM 调用，
pause 可能延迟数十秒。

## 方案

`pause()` 设标志后轮询 `task_state.status == TaskStatus.PAUSED`，带 30s 超时。
返回值从 `None` 改为 `bool`（True=成功，False=超时）。

PAUSED 状态的前端感知由现有 `TaskDebugAPI` 的 500ms SSE 轮询覆盖，
无需新增 EventBus 事件类型。

## 改动范围

只改 2 个文件：`lifecycle.py` + `task_system.py`。

## 不变原则

- Orchestrator `_check_interrupts()` 不变——已在正确位置检测 `pause_requested`
- `ExecutionState` 不加新字段——不复用 `asyncio.Event`，保持轮询
- `cancel()` 保持 fire-and-forget——取消不需要确认
- `agent.py` 不改——`_poll_mailboxes` 的 PAUSED 忽略行为不变，TaskDebugAPI 轮询自行感知

## 调用方影响分析

`TaskSystem.pause()` 所有 9 个调用方：
- `task_system.py:255` — 薄封装自身
- `test_task_system.py` — 5 处 `await ts.pause(tid)`
- `test_e2e_mvp.py` — 1 处 `await ts.pause(task_id)`
- `test_task_system_lifecycle.py` — 3 处 `await ts.pause(task_id)`

全部使用 `await`，不检查返回值。`None → bool` 向后兼容，无外部调用方。

---

## 1. `lifecycle.py` — `pause()` 改为 async 轮询

`import time` 移到模块顶部。

### 改动前

```python
def pause(
    tasks: dict[TaskId, SubAgent],
    task_id: TaskId,
) -> None:
```

### 改动后

```python
async def pause(
    tasks: dict[TaskId, SubAgent],
    task_id: TaskId,
    timeout: float = 30.0,
) -> bool:
    """暂停任务——在下一个安全点停止执行并保存 checkpoint。幂等。

    设 pause_requested 标志后，轮询等待 orchestrator 确认 PAUSED 状态。

    Returns:
        True:  暂停已确认（task_state.status == PAUSED）
        False: 超时——orchestrator 在 timeout 秒内未到达检查点
                （可能正在执行长 LLM 调用，稍后可重试）
    """
    sub_agent = tasks.get(task_id)
    if sub_agent is None:
        return False  # 已清理——无需暂停

    ts = sub_agent._task_state
    if ts is None or ts.is_terminal():
        return False  # 已终态——无需暂停
    if ts.status == TaskStatus.PAUSED:
        return True   # 已暂停——幂等

    if sub_agent._orchestrator is None:
        return False  # 尚未启动——无需暂停
    exec_state = sub_agent._orchestrator._exec_state
    if exec_state is None:
        return False  # 尚未启动——无需暂停

    exec_state.pause_requested = True
    logger.info("lifecycle.pause: task_id=%s 暂停请求已发送", task_id)

    # 等待 orchestrator 确认 PAUSED
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ts.status == TaskStatus.PAUSED:
            logger.info("lifecycle.pause: task_id=%s 暂停已确认", task_id)
            return True
        # 任务可能在等待期间进入终态（LLM 恰好在检查点前完成）
        if ts.is_terminal():
            logger.info("lifecycle.pause: task_id=%s 在等待期间已进入终态", task_id)
            return False
        await asyncio.sleep(0.1)

    logger.warning("lifecycle.pause: task_id=%s 暂停超时 (%.0fs)", task_id, timeout)
    return False
```

---

## 2. `task_system.py` — `pause()` 改为 await 委托

### 改动前

```python
async def pause(self, task_id: TaskId) -> None:
    """暂停任务——委托给 lifecycle.pause()。"""
    lifecycle.pause(self._tasks, task_id)
```

### 改动后

```python
async def pause(self, task_id: TaskId, timeout: float = 30.0) -> bool:
    """暂停任务——委托给 lifecycle.pause()。

    等待 orchestrator 确认 PAUSED 状态，带超时。

    Returns:
        True:  暂停已确认
        False: 超时——orchestrator 未在 timeout 秒内到达检查点
    """
    return await lifecycle.pause(self._tasks, task_id, timeout=timeout)
```

---

## 3. 测试

在 `test_task_system.py` 新增：

### `test_pause_confirmed()`
Mock orchestrator 在 `pause_requested` 后切换到 PAUSED，验证 `pause()` 返回 `True` 且耗时 < timeout。

### `test_pause_timeout()`
Mock orchestrator 不切换状态，验证 `pause()` 返回 `False` 且耗时 ≈ timeout。

### `test_pause_terminal_during_wait()`
Mock 任务在轮询期间进入 COMPLETED，验证 `pause()` 返回 `False`（不傻等到超时）。

### `test_pause_idempotent()`
对已 PAUSED 的任务调用 `pause()`，验证立即返回 `True`（不进入轮询）。

---

## 验证

```bash
conda activate Elysia
python -m pytest Module/task_system/tests/unit/test_task_system.py -v -k "pause"
python -m pytest Module/task_system/tests/unit/ -v
```
