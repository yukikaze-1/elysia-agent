# TaskSystem resume() 设计

> 日期: 2026-06-17
> 状态: 设计完成，待实施

---

## 1. 语义

`resume(task_id)` — 将 PAUSED 任务恢复到 RUNNING，从暂停时的 checkpoint 继续执行。

- 成功 / 幂等 → 返回已有的 `TaskMailbox`（pause 期间未销毁）。任务已在 RUNNING 也返回 mailbox（幂等——已经恢复过了）
- 失败 → 返回 `ResumeError`（含结构化错误码）——仅终态和已清理两种真正失败场景
- CAS（compare-and-swap）：只有 `status==PAUSED` 才走实际恢复流程；RUNNING 直接返回 mailbox

## 2. 状态转换

```
PAUSED ──resume()──► RESUMING ──Orchestrator 接管──► RUNNING
                         │                                │
                         │ CAS: 必须 status==PAUSED       │ 正常执行循环
                         │                                │ → COMPLETED / FAILED / PAUSED / ...
                         │                                │
                         └── checkpoint 缺失 / graph 缺失
                             → FAILED（内部异常，直接终态）
```

`RESUMING` 瞬时态——在 `resume_from_pause()` 设置，Orchestrator 启动后切到 `RUNNING`。

## 3. 核心前提：pause 后不清理

### 3.1 当前问题

`_run_and_cleanup` 的 `finally` 块无条件执行 `_cleanup_task()` + `_cleanup_storage()` + `semaphore.release()`。PAUSED 任务的 checkpoint 被删、SubAgent 被 GC。

### 3.2 修改

```python
# task_system.py _run_and_cleanup() finally 块

finally:
    # PAUSED 任务保持存活——checkpoint/mailbox/SubAgent/semaphore 全部保留
    if (sub_agent._task_state is not None and
        sub_agent._task_state.status == TaskStatus.PAUSED):
        return
    self._cleanup_task(task_id)
    try:
        await self._cleanup_storage(task_id, sub_agent)
    except Exception:
        logger.exception(...)
    self._semaphore.release()
```

## 4. 实现

### 4.1 SubAgent——保存 mailbox 和 context 引用

```python
# sub_agent.py run() 方法内

async def run(self, context: DelegationContext, mailbox: TaskMailbox) -> None:
    ...
    self._context = context   # ← 新增：resume 时需要 intent/tools/constraints
    self._mailbox = mailbox   # ← 新增：resume 时复用同一个 mailbox
    ...
    await self._execute(task_state, graph, mailbox, context)
```

### 4.2 SubAgent.resume_from_pause()

```python
async def resume_from_pause(self) -> None:
    """从 PAUSED 恢复——CAS 检查 → 加载 checkpoint → _execute。

    Raises:
        RuntimeError: 非 PAUSED 状态 / checkpoint 缺失 / graph 缺失
    """
    ts = self._task_state
    if ts is None or ts.status != TaskStatus.PAUSED:
        raise RuntimeError(
            f"task_id={self._mailbox.task_id} 非 PAUSED 状态（{ts.status if ts else 'None'}），"
            "无法 resume"
        )

    ts.status = TaskStatus.RESUMING
    ts.updated_at = now_iso()

    checkpoint = await self._checkpoint_mgr.load(self._mailbox.task_id)
    if checkpoint is None:
        ts.status = TaskStatus.FAILED
        ts.updated_at = now_iso()
        raise RuntimeError("checkpoint 缺失，无法恢复")

    graph = self._graph
    if graph is None:
        ts.status = TaskStatus.FAILED
        ts.updated_at = now_iso()
        raise RuntimeError("graph 缺失，无法恢复")

    # 从 checkpoint 的 ExecutionState 恢复——Orchestrator 会合并旧状态
    await self._execute(ts, graph, self._mailbox, self._context,
                        resume_from=checkpoint.exec)
```

### 4.3 _execute——接受 resume_from 参数

```python
# 将当前 _execute 内部的 resume_from 初始化从硬编码改为参数

async def _execute(
    self,
    task_state: TaskState,
    graph: PlanGraph,
    mailbox: TaskMailbox,
    context: DelegationContext,
    resume_from: ExecutionState | None = None,  # ← 新增参数
) -> None:
    ...
    while True:
        result = await self._orchestrator.run(
            task_state, graph, mailbox, resume_from=resume_from,
        )
        ...
```

### 4.4 task_system.resume()

```python
async def resume(self, task_id: TaskId) -> TaskMailbox | ResumeError:
    """从 PAUSED 恢复执行。

    成功返回 TaskMailbox（pause 时未销毁，同一个实例）。
    失败返回 ResumeError（含结构化错误码）。
    """
    sub_agent = self._tasks.get(task_id)
    if sub_agent is None:
        return ResumeError(
            code=ResumeErrorCode.CHECKPOINT_NOT_FOUND,
            message=f"task_id={task_id} 不存在或已清理",
        )

    ts = sub_agent._task_state
    if ts is None:
        return ResumeError(
            code=ResumeErrorCode.INVALID_STATE,
            message="任务尚未初始化",
        )
    if ts.is_terminal():
        return ResumeError(
            code=ResumeErrorCode.INVALID_STATE,
            message=f"任务已终态（{ts.status.value}），无法 resume",
        )
    if ts.status == TaskStatus.RUNNING or ts.status == TaskStatus.RESUMING:
        # 幂等——已经在跑了
        mailbox = self._mailboxes.get(task_id)
        if mailbox is not None:
            return mailbox
        return ResumeError(
            code=ResumeErrorCode.INVALID_STATE,
            message="任务正在运行但 mailbox 缺失，内部状态不一致",
        )
    if ts.status != TaskStatus.PAUSED:
        return ResumeError(
            code=ResumeErrorCode.INVALID_STATE,
            message=f"任务状态为 {ts.status.value}，仅 PAUSED 可 resume",
        )

    mailbox = self._mailboxes.get(task_id)
    if mailbox is None:
        return ResumeError(
            code=ResumeErrorCode.CHECKPOINT_NOT_FOUND,
            message="mailbox 缺失，内部状态不一致",
        )

    async def _run_resume() -> None:
        """包装 resume_from_pause，处理异常 + 终态清理。
        
        和 _run_and_cleanup 对称——pause 路径没有 _run_and_cleanup 的清理，
        resume 后的终态（COMPLETED/FAILED/CANCELLED）由此 finally 块负责。
        """
        try:
            await sub_agent.resume_from_pause()
        except asyncio.CancelledError:
            push_mailbox_event(mailbox, TaskEventType.FAILED, {
                "task_id": str(task_id),
                "error": "任务被取消",
                "error_type": TaskErrorCode.INTERNAL_ERROR.value,
            })
            raise
        except Exception:
            logger.exception("TaskSystem: task_id=%s resume 异常", task_id)
            push_mailbox_event(mailbox, TaskEventType.FAILED, {
                "task_id": str(task_id),
                "error": "resume 执行异常",
                "error_type": TaskErrorCode.INTERNAL_ERROR.value,
            })
        finally:
            self._cleanup_task(task_id)
            try:
                await self._cleanup_storage(task_id, sub_agent)
            except Exception:
                logger.exception(...)
            assert self._semaphore is not None
            self._semaphore.release()

    task = asyncio.create_task(_run_resume())
    self._task_futures[task_id] = task
    logger.info("TaskSystem.resume: task_id=%s 已恢复", task_id)
    return mailbox
```

### 4.5 Orchestrator——运行前设 RUNNING

`Orchestrator.run()` 在循环开始前检查 `task_state.status`，如果是 `RESUMING` → 切到 `RUNNING`。

```python
# orchestrator.py run() 开头，在 while 循环之前

if task_state.status == TaskStatus.RESUMING:
    task_state.status = TaskStatus.RUNNING
    task_state.updated_at = now_iso()
```

---

## 5. 改动文件

| 文件 | 改动 | 行数 |
|------|------|------|
| `task_system.py` | `_run_and_cleanup` 加 PAUSED 守卫；实现 `resume()` + `_run_resume` | +70 |
| `sub_agent.py` | `run()` 保存 `_context`/`_mailbox`；新增 `resume_from_pause()`；`_execute` 加 `resume_from` 参数 | +35 |
| `orchestrator.py` | 循环前 RESUMEING→RUNNING 转换 | +5 |
| `tests/unit/test_task_system.py` | 5 个 resume 单元测试 | ~90 |
| `tests/unit/test_sub_agent.py` | 2 个 resume_from_pause 测试 | ~40 |
| `tests/integration/test_task_system_lifecycle.py` | 1 个 pause→resume 集成测试 | ~50 |
| `docs/` | PROGRESS.md / api.md 同步 | ~10 |

---

## 6. 与 pause / cancel 的对称性

| | pause | resume | cancel |
|---|---|---|---|
| **触发** | `exec_state.pause_requested` | 外部调 `resume()` | `exec_state.cancel_requested` |
| **状态** | RUNNING → PAUSE_REQUESTED → PAUSED | PAUSED → RESUMING → RUNNING | RUNNING → CANCELLED |
| **checkpoint** | 写 | 读 | 写 |
| **清理** | 跳过 | 终态时清理 | 正常清理 |
| **semaphore** | 保持 | 终态时释放 | 释放 |
| **幂等** | ❌ 抛 RuntimeError（⚠️ 待修复） | ✅ PAUSED→恢复，RUNNING→返回 mailbox | ✅ 返回 True |

---

## 7. 风险和后续

1. **PAUSED 占用 semaphore 槽位**：pause 后不释放信号量，意味着 5 个槽位可以全部被 PAUSED 任务占满，阻塞新提交。这是设计意图——PAUSED 是"暂停中"，资源没释放。但需要文档明确。

2. **RESUMING → FAILED 的清理路径**：如果 `resume_from_pause()` 在 `_execute` 之前抛异常（checkpoint 缺失、graph 缺失），`_run_resume` 的 finally 会正确清理。但如果 `_execute` 内部异常，由 `_execute` 自己的异常处理 + `_dispatch_result` 覆盖。

3. **v1 不支持 resume 时修改参数**：resume 使用原始的 `DelegationContext`（intent/tools/constraints 不变）。如需修改，必须 cancel → 重新 submit。设计与架构文档 Section 6.4 一致。
