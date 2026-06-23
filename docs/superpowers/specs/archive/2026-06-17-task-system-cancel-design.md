# TaskSystem cancel() 设计

> 日期: 2026-06-17
> 状态: 设计完成，待实施

---

## 1. 语义

`cancel(task_id)` — 幂等的软取消。调了就保证任务进入停止流程：

- 任务 RUNNING → 设标志 → Orchestrator 安全点停止 → checkpoint → CANCELLED
- 任务 PAUSED → 直接 CANCELLED
- 任务已终态 / 已清理 → 幂等返回 True

所有路径返回 `True`——调用方不需要处理"取消失败"（v1 限制，见 6. 风险）。

二次确认由 CognitiveLoop 在上层完成（用户说"停" → LLM/关键词 识别 → 确认弹窗 → 调 `cancel()`）。

---

## 2. 状态转换

```
TaskStatus:
  RUNNING → (cancel_requested=True) → CANCELLED
  PAUSED  → CANCELLED（直接）

OrchestratorRunStatus:
  新增 CANCELED
```

- 从 PAUSED：`cancel()` 直接设 `task_state.status = CANCELLED`，push CANCELLED 事件
- 从 RUNNING：`cancel()` 设 `exec_state.cancel_requested = True`，Orchestrator 下一轮循环检测到 → save checkpoint → 设 CANCELLED → 返回 CANCELED
- 尚未进入 Orchestrator（`_exec_state=None`）：直接 CANCELLED
- 已是终态：幂等返回 True

---

## 3. 实现

### 3.1 schema.py

```python
# ExecutionState 加字段
cancel_requested: bool = False  # 外部请求取消（Orchestrator 检测后设 CANCELLED）

# OrchestratorRunStatus 加枚举值
CANCELED = "canceled"
```

### 3.2 task_system.py — cancel()

```python
async def cancel(self, task_id: TaskId) -> bool:
    """软取消任务。二次确认由 CognitiveLoop 在上层处理。

    幂等——任务不存在/已终态也返回 True。
    """
    sub_agent = self._tasks.get(task_id)
    if sub_agent is None:
        return True  # 已清理 = 已取消

    ts = sub_agent._task_state
    if ts is None or ts.is_terminal():
        return True  # 已终态 = 已取消

    # PAUSED → 直接 CANCELLED
    if ts.status == TaskStatus.PAUSED:
        ts.status = TaskStatus.CANCELLED
        ts.updated_at = now_iso()
        mailbox = self._mailboxes.get(task_id)
        if mailbox:
            push_mailbox_event(mailbox, TaskEventType.CANCELLED, {
                "task_id": str(task_id), "reason": "用户取消",
            })
        # cleanup 由 _run_and_cleanup finally 块负责
        return True

    # RUNNING / PLANNING → 设标志，Orchestrator 接管
    # TODO: v1 只设标志的方式缺少失败反馈——
    #   cancel_requested 可能因异常永远不被检测到。
    #   后续考虑带超时的 await + cancel 确认机制。
    exec_state = sub_agent._orchestrator._exec_state
    if exec_state is None:
        # 尚未进入 Orchestrator（规划/校验阶段）
        ts.status = TaskStatus.CANCELLED
        ts.updated_at = now_iso()
        return True

    exec_state.cancel_requested = True
    return True
```

### 3.3 orchestrator.py — 循环顶检测

在 `pause_requested` 检查之后加（类比模式）：

```python
# 0. 检查外部取消信号
if exec_state.cancel_requested:
    logger.info("Orchestrator: task_id=%s 收到取消请求", task_id)
    task_state.status = TaskStatus.CANCELLED
    task_state.updated_at = now_iso()
    await self._save_checkpoint(task_state, exec_state)
    return OrchestratorResult(
        status=OrchestratorRunStatus.CANCELED,
        checkpoint_state=self._build_checkpoint(task_state, exec_state),
    )
```

### 3.4 sub_agent.py — 分发 + handler

`_dispatch_result` 加分支：

```python
elif result.status == OrchestratorRunStatus.CANCELED:
    await self._handle_cancelled(result, task_state, mailbox)
    return False, None
```

`_handle_cancelled`：

```python
async def _handle_cancelled(
    self,
    result: OrchestratorResult,
    task_state: TaskState,
    mailbox: TaskMailbox,
) -> None:
    """取消——推送 CANCELLED 事件，执行循环终止后由 finally 清理。"""
    task_state.status = TaskStatus.CANCELLED
    task_state.updated_at = now_iso()
    await self._push_mailbox(mailbox, TaskEventType.CANCELLED, {
        "task_id": str(task_state.task_id),
        "reason": "用户取消",
    })
```

---

## 4. 改动文件

| 文件 | 改动 | 行数 |
|------|------|------|
| `schema.py` | `ExecutionState` + `cancel_requested`；`OrchestratorRunStatus` + `CANCELED` | +3 |
| `task_system.py` | 实现 `cancel()` 替代 `raise NotImplementedError` | +40 |
| `orchestrator.py` | 循环顶加 `cancel_requested` 检测 | +12 |
| `sub_agent.py` | `_dispatch_result` + `CANCELED` 分支；`_handle_cancelled` | +25 |
| `tests/unit/test_task_system.py` | 6 个单元测试 | ~100 |
| `tests/integration/test_task_system_lifecycle.py` | 1 个集成测试 | ~40 |
| `docs/` | PROGRESS.md / api.md 同步 | ~10 |

---

## 5. 与 pause 的对称性

| | pause | cancel |
|---|---|---|
| **设置标志** | `exec_state.pause_requested = True` | `exec_state.cancel_requested = True` |
| **检测点** | Orchestrator 循环顶 | Orchestrator 循环顶 |
| **配置** | 当前节点执行完成后 | 当前节点执行完成后 |
| **checkpoint** | 写 checkpoint | 写 checkpoint |
| **返回** | `PAUSED` — 可 resume | `CANCELED` — 终态，不可恢复 |
| **事件** | `PAUSED` | `CANCELLED` |
| **幂等** | ❌ 重复调抛 RuntimeError | ✅ 所有路径返回 True |

---

## 6. 风险和后续

1. **v1 只设标志**：如果 Orchestrator 因异常永远不到达下一个循环迭代（工具死循环、网络挂起），cancel 信号永远不被处理。当前硬超时 + `max_task_duration_s` 提供兜底。后续考虑：
   - `TaskSystem.cancel()` 带超时：设标志后 `await` wait_for，超时后 force cancel asyncio.Task
   - 或给 Orchestrator 加 watchdog 协程

2. **checkpoint 中 cancel_requested 不持久化**：如果进程在 cancel_requested=True 但 checkpoint 在下一个节点完成后才写时崩溃，标志丢失。但 `pause_requested` 有同样的问题——两者一致，v1 不修。

3. **PAUSED 分支的 mailbox 可能已被 pop**：`cancel()` 里 `self._mailboxes.get(task_id)` 可能与 `_cleanup_task` 竞态。加判空保护即可。
