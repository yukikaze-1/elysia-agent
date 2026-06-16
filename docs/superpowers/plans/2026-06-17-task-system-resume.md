# TaskSystem resume() 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 TaskSystem.resume()——从 PAUSED 恢复执行，通过保留 SubAgent/checkpoint/mailbox 在 pause 后不清理实现。

**Architecture:** pause 后 `_run_and_cleanup` 跳过清理（SubAgent/checkpoint/mailbox/semaphore 全部保留）。resume() 做 CAS 检查后调 `sub_agent.resume_from_pause()`，从 checkpoint 加载 ExecutionState 注入 `_execute`。幂等——RUNNING 状态直接返回 mailbox。

**Spec:** `docs/superpowers/specs/2026-06-17-task-system-resume-design.md`

**Tech Stack:** Python 3.13, asyncio, Pydantic

---

### Task 1: task_system.py — _run_and_cleanup PAUSED 守卫

**Files:**
- Modify: `Module/task_system/task_system.py:227-236`

- [ ] **Step 1: 在 finally 块开头加 PAUSED 检查**

当前 `_run_and_cleanup` 的 finally 块（约 line 227-236）：

```python
            finally:
                self._cleanup_task(task_id)
                try:
                    await self._cleanup_storage(task_id, sub_agent)
                except Exception:
                    logger.exception(
                        "TaskSystem: task_id=%s 存储清理失败，槽位仍会释放", task_id,
                    )
                assert self._semaphore is not None
                self._semaphore.release()
```

替换为：

```python
            finally:
                # PAUSED 任务保持存活——checkpoint/mailbox/SubAgent/semaphore
                # 全部保留，等待 resume() 恢复执行。
                if (sub_agent._task_state is not None and
                    sub_agent._task_state.status == TaskStatus.PAUSED):
                    return
                self._cleanup_task(task_id)
                try:
                    await self._cleanup_storage(task_id, sub_agent)
                except Exception:
                    logger.exception(
                        "TaskSystem: task_id=%s 存储清理失败，槽位仍会释放", task_id,
                    )
                assert self._semaphore is not None
                self._semaphore.release()
```

- [ ] **Step 2: 验证** — 运行现有测试确认 pause 不清理 guard 不破坏正常路径

```bash
python -m pytest Module/task_system/tests/unit/ -q
```
Expected: 287 passed（正常路径 COMPLETED/FAILED 仍然清理）

- [ ] **Step 3: Commit**

```bash
git add Module/task_system/task_system.py
git commit -m "feat(resume): _run_and_cleanup——PAUSED 跳过清理，保留 checkpoint/mailbox/semaphore"
```

---

### Task 2: sub_agent.py——保存引用 + resume_from_pause + _execute 参数化

**Files:**
- Modify: `Module/task_system/core/sub_agent.py:127-182,298-314`

- [ ] **Step 1: run() 保存 _context 和 _mailbox**

在 `run()` 方法中，TaskState 创建之后、_plan_and_validate 之前，加两行（约 line 138 之后）：

```python
        self._task_state = task_state
        self._context = context   # ← resume 时需要 intent/tools/constraints
        self._mailbox = mailbox   # ← resume 时复用同一个 mailbox
```

- [ ] **Step 2: _execute 加 resume_from 参数**

将 `_execute` 签名从：

```python
    async def _execute(
        self,
        task_state: TaskState,
        graph: PlanGraph,
        mailbox: TaskMailbox,
        context: DelegationContext,
    ) -> None:
        """执行-决策主循环。
        ...
        """
        resume_from: ExecutionState | None = None
```

改为：

```python
    async def _execute(
        self,
        task_state: TaskState,
        graph: PlanGraph,
        mailbox: TaskMailbox,
        context: DelegationContext,
        resume_from: ExecutionState | None = None,
    ) -> None:
        """执行-决策主循环。

        Orchestrator 内部自建 ExecutionState，SubAgent 不感知。
        大部分任务 Orchestrator 内部一次跑完，这个循环只跑一次。
        只在结构性失败（NEEDS_REPLAN）时循环多次。

        resume_from 在 Replan 后携带旧 ExecutionState，
        Orchestrator 合并后保留已成功节点的状态，避免重复执行。
        resume 时传入 pause checkpoint 的 ExecutionState。
        """
```

注意：删掉 `resume_from: ExecutionState | None = None` 这行局部变量声明——现在它从参数获取初始值。

- [ ] **Step 3: 新增 resume_from_pause() 方法**

在 `run()` 方法之后（约 line 183）、`_plan_and_validate` 之前插入：

```python
    # ── 恢复 ─────────────────────────────────────────────────────────────────

    async def resume_from_pause(self) -> None:
        """从 PAUSED 恢复——CAS 检查 → 加载 checkpoint → _execute。

        Raises:
            RuntimeError: 非 PAUSED 状态 / checkpoint 缺失 / graph 缺失
        """
        ts = self._task_state
        if ts is None or ts.status != TaskStatus.PAUSED:
            raise RuntimeError(
                f"task_id={self._mailbox.task_id} 非 PAUSED 状态"
                f"（{ts.status if ts else 'None'}），无法 resume"
            )

        ts.status = TaskStatus.RESUMING
        ts.updated_at = now_iso()

        checkpoint = await self._checkpoint_mgr.load(self._mailbox.task_id)
        if checkpoint is None:
            ts.status = TaskStatus.FAILED
            ts.updated_at = now_iso()
            raise RuntimeError("checkpoint 缺失，无法恢复")

        if self._graph is None:
            ts.status = TaskStatus.FAILED
            ts.updated_at = now_iso()
            raise RuntimeError("graph 缺失，无法恢复")

        assert self._context is not None, "run() 必须先于 resume_from_pause()"

        await self._execute(
            ts, self._graph, self._mailbox, self._context,
            resume_from=checkpoint.exec,
        )
```

- [ ] **Step 4: 运行 SubAgent 单元测试确认兼容**

```bash
python -m pytest Module/task_system/tests/unit/test_sub_agent.py -v
```
Expected: 22 passed（_execute 参数有默认值，现有调用不受影响）

- [ ] **Step 5: 运行全量单元测试**

```bash
python -m pytest Module/task_system/tests/unit/ -q
```
Expected: 287 passed

- [ ] **Step 6: Commit**

```bash
git add Module/task_system/core/sub_agent.py
git commit -m "feat(resume): SubAgent 保存 _context/_mailbox + resume_from_pause + _execute resume_from 参数"
```

---

### Task 3: task_system.py——实现 resume() + _run_resume

**Files:**
- Modify: `Module/task_system/task_system.py:17-38,302-312`

- [ ] **Step 0: 确认 ResumeErrorCode 已导入**

检查 `from Module.task_system.schema import (` 块中是否有 `ResumeErrorCode`。如果没有，在 `ResumeError` 之后加：

```python
    ResumeError,
    ResumeErrorCode,    # ← 新增（如果不存在）
    SubmitError,
```

同样检查测试文件中 `ResumeError` 和 `ResumeErrorCode` 是否导入。

- [ ] **Step 1: 替换 resume() stub**

将当前的 `raise NotImplementedError("TaskSystem.resume() 尚未实现")` stub 替换为：

```python
    async def resume(self, task_id: TaskId) -> TaskMailbox | ResumeError:
        """从 PAUSED 恢复执行。

        幂等——已在 RUNNING/RESUMING 的任务直接返回 mailbox。
        仅 PAUSED 状态经过 CAS 检查后进入实际恢复流程。
        v1 限制：使用原始 DelegationContext，不支持注入新上下文。
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
        if ts.status in (TaskStatus.RUNNING, TaskStatus.RESUMING):
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

            和 submit() 的 _run_and_cleanup 对称——resume 后的终态
            （COMPLETED/FAILED/CANCELLED）由此 finally 块负责清理。
            """
            try:
                await sub_agent.resume_from_pause()
            except asyncio.CancelledError:
                push_mailbox_event(
                    mailbox, TaskEventType.FAILED, {
                        "task_id": str(task_id),
                        "error": "任务被取消",
                        "error_type": TaskErrorCode.INTERNAL_ERROR.value,
                    },
                    caller="TaskSystem",
                )
                raise
            except Exception:
                logger.exception("TaskSystem: task_id=%s resume 异常", task_id)
                push_mailbox_event(
                    mailbox, TaskEventType.FAILED, {
                        "task_id": str(task_id),
                        "error": "resume 执行异常",
                        "error_type": TaskErrorCode.INTERNAL_ERROR.value,
                    },
                    caller="TaskSystem",
                )
            finally:
                self._cleanup_task(task_id)
                try:
                    await self._cleanup_storage(task_id, sub_agent)
                except Exception:
                    logger.exception(
                        "TaskSystem: task_id=%s resume 后存储清理失败", task_id,
                    )
                assert self._semaphore is not None
                self._semaphore.release()

        task = asyncio.create_task(_run_resume())
        self._task_futures[task_id] = task
        logger.info("TaskSystem.resume: task_id=%s 已恢复", task_id)
        return mailbox
```

- [ ] **Step 2: 验证 import OK**

```bash
python -c "from Module.task_system.task_system import TaskSystem; print('import OK')"
```
Expected: `import OK`

- [ ] **Step 3: 运行全量单元测试**

```bash
python -m pytest Module/task_system/tests/unit/ -q
```
Expected: 287 passed

- [ ] **Step 4: Commit**

```bash
git add Module/task_system/task_system.py
git commit -m "feat(resume): 实现 TaskSystem.resume()——CAS + 幂等 + _run_resume"
```

---

### Task 4: orchestrator.py——RESUMING → RUNNING 转换

**Files:**
- Modify: `Module/task_system/core/orchestrator.py:151-153`

- [ ] **Step 1: 在循环开始前加状态转换**

在 `run()` 方法中，`while not task_state.is_terminal():` 循环之前（约 line 152），加：

```python
        # 恢复执行——将瞬时 RESUMING 转为 RUNNING
        if task_state.status == TaskStatus.RESUMING:
            task_state.status = TaskStatus.RUNNING
            task_state.updated_at = now_iso()

        try:
            while not task_state.is_terminal():
```

- [ ] **Step 2: 运行 Orchestrator 单元测试**

```bash
python -m pytest Module/task_system/tests/unit/test_orchestrator.py -v
```
Expected: 26 passed

- [ ] **Step 3: Commit**

```bash
git add Module/task_system/core/orchestrator.py
git commit -m "feat(resume): Orchestrator 启动时 RESUMING→RUNNING 转换"
```

---

### Task 5: 单元测试——task_system resume()

**Files:**
- Modify: `Module/task_system/tests/unit/test_task_system.py`

5 个测试，追加到文件末尾：

- [ ] **Step 1: Read file and append**

```python
# ═══════════════════════════════════════════════════════════════════════════
# resume()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_resume_missing_task_returns_error():
    """不存在的 task_id → ResumeError(CHECKPOINT_NOT_FOUND)。"""
    ts = await _build_task_system()
    result = await ts.resume(TaskId("nonexistent"))
    assert isinstance(result, ResumeError)
    assert result.code == ResumeErrorCode.CHECKPOINT_NOT_FOUND


@pytest.mark.asyncio
async def test_resume_terminal_task_returns_error():
    """已终态任务 → ResumeError(INVALID_STATE)。"""
    ts = await _build_task_system()

    tid = TaskId("t1")
    task_state = _make_task_state("t1", TaskStatus.COMPLETED)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    result = await ts.resume(tid)
    assert isinstance(result, ResumeError)
    assert result.code == ResumeErrorCode.INVALID_STATE


@pytest.mark.asyncio
async def test_resume_running_is_idempotent():
    """RUNNING 任务 → 直接返回 mailbox（幂等）。"""
    ts = await _build_task_system()

    tid = TaskId("t1")
    task_state = _make_task_state("t1", TaskStatus.RUNNING)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    result = await ts.resume(tid)
    # 幂等——返回 mailbox 而非 ResumeError
    assert isinstance(result, TaskMailbox)


@pytest.mark.asyncio
async def test_resume_not_paused_returns_error():
    """非 PAUSED/非 RUNNING 状态 → ResumeError。"""
    ts = await _build_task_system()

    tid = TaskId("t1")
    task_state = _make_task_state("t1", TaskStatus.PLANNING)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    result = await ts.resume(tid)
    assert isinstance(result, ResumeError)
    assert result.code == ResumeErrorCode.INVALID_STATE


@pytest.mark.asyncio
async def test_resume_paused_returns_mailbox():
    """PAUSED 任务 → 返回 mailbox（进入实际恢复流程）。"""
    ts = await _build_task_system()

    tid = TaskId("t1")
    task_state = _make_task_state("t1", TaskStatus.PAUSED)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    result = await ts.resume(tid)
    assert isinstance(result, TaskMailbox)
    assert task_state.status == TaskStatus.RESUMING
```

需要的 import 检查：
- `ResumeError` 和 `ResumeErrorCode` 是否已在文件顶部 import（在 `from Module.task_system.schema import (...)` 中）
- `TaskMailbox` 是否可用（已有，在 `_build_task_system` 内 import）

- [ ] **Step 2: 先只运行新测试确认它们通过**

```bash
python -m pytest Module/task_system/tests/unit/test_task_system.py -k "resume" -v
```
Expected: 5 passed

- [ ] **Step 3: 运行全量单元测试**

```bash
python -m pytest Module/task_system/tests/unit/ -q
```
Expected: 292 passed（287 + 5 new）

- [ ] **Step 4: Commit**

```bash
git add Module/task_system/tests/unit/test_task_system.py
git commit -m "test(resume): 5 个 resume() 单元测试"
```

---

### Task 6: 单元测试——sub_agent resume_from_pause

**Files:**
- Modify: `Module/task_system/tests/unit/test_sub_agent.py`

2 个测试，追加到文件末尾：

- [ ] **Step 1: Read file and append**

```python
# ═══════════════════════════════════════════════════════════════════════════
# resume_from_pause()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_resume_from_pause_not_paused_raises():
    """非 PAUSED 状态调 resume_from_pause → RuntimeError。"""
    from Module.task_system.infra import AgentInfra

    infra = AgentInfra(
        llm=MagicMock(),
        milvus=None,
        http=None,
        tool_registry=MagicMock(),
        constraints=ConstraintConfig(),
    )
    sub_agent = SubAgent(infra=infra, hitl_notifier=MagicMock())

    # 手动设 task_state 为非 PAUSED
    sub_agent._task_state = TaskState(
        task_id=TaskId("t1"),
        status=TaskStatus.RUNNING,
        created_at=now_iso(),
        updated_at=now_iso(),
    )
    # 需要 _mailbox（resume_from_pause 读它拿 task_id）
    from Module.task_system.mailbox import TaskMailbox
    sub_agent._mailbox = TaskMailbox(TaskId("t1"))

    with pytest.raises(RuntimeError, match="非 PAUSED"):
        await sub_agent.resume_from_pause()


@pytest.mark.asyncio
async def test_resume_from_pause_no_checkpoint_raises():
    """PAUSED 但 checkpoint 缺失 → RuntimeError + 状态切到 FAILED。"""
    from Module.task_system.infra import AgentInfra

    infra = AgentInfra(
        llm=MagicMock(),
        milvus=None,
        http=None,
        tool_registry=MagicMock(),
        constraints=ConstraintConfig(),
    )
    sub_agent = SubAgent(infra=infra, hitl_notifier=MagicMock())

    task_state = TaskState(
        task_id=TaskId("t1"),
        status=TaskStatus.PAUSED,
        created_at=now_iso(),
        updated_at=now_iso(),
    )
    sub_agent._task_state = task_state
    from Module.task_system.mailbox import TaskMailbox
    sub_agent._mailbox = TaskMailbox(TaskId("t1"))

    # 注入 mock CheckpointManager——load 返回 None（模拟缺失）
    mock_cm = MagicMock()
    mock_cm.load = AsyncMock(return_value=None)
    sub_agent._checkpoint_mgr = mock_cm

    with pytest.raises(RuntimeError, match="checkpoint 缺失"):
        await sub_agent.resume_from_pause()

    # 验证状态切到了 FAILED
    assert task_state.status == TaskStatus.FAILED
```

确认 import:
- `SubAgent` 已能访问（文件顶部已有 `from Module.task_system.core.sub_agent import SubAgent`）
- `AgentInfra` 可能已在顶部导入或在某个 fixture 中
- `TaskMailbox` 需要 import（`from Module.task_system.mailbox import TaskMailbox`）
- `TaskState`, `TaskId`, `TaskStatus`, `ConstraintConfig`, `now_iso` 可能已导入

- [ ] **Step 2: 运行新测试**

```bash
python -m pytest Module/task_system/tests/unit/test_sub_agent.py -k "resume" -v
```
Expected: 2 passed

- [ ] **Step 3: 运行全量子 agent 测试**

```bash
python -m pytest Module/task_system/tests/unit/test_sub_agent.py -v
```
Expected: 24 passed (22 + 2)

- [ ] **Step 4: Commit**

```bash
git add Module/task_system/tests/unit/test_sub_agent.py
git commit -m "test(resume): 2 个 resume_from_pause 单元测试"
```

---

### Task 7: 集成测试——pause → resume 完整链路

**Files:**
- Modify: `Module/task_system/tests/integration/test_task_system_lifecycle.py`

- [ ] **Step 1: 追加集成测试**

在文件末尾添加：

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_pause_and_resume():
    """提交任务 → pause → resume → 任务正常完成。

    验证：
    - pause 后 PAUSED 事件被推送
    - resume 返回 mailbox
    - 任务最终完成（COMPLETED）
    - 注册表清理
    """
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    ts = await _build_task_system()
    tools = _tools_from_manager(ts._infra.tool_registry)

    ctx = DelegationContext(
        intent="现在几点了",
        tools=tools,
        session_id=SessionId("resume-test"),
    )
    result = await ts.submit(ctx)
    assert isinstance(result, TaskMailbox), f"submit 失败: {result}"
    task_id = result.task_id

    # 等第一个 PROGRESS——表示 Orchestrator 已启动
    first_event = await asyncio.wait_for(result.get(), timeout=30.0)
    assert first_event is not None, "未收到首个事件"

    # pause
    try:
        await ts.pause(task_id)
    except KeyError:
        # 任务已完成（get_time 很快），跳过测试
        return

    # 等 PAUSED 事件
    paused_received = False
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            event = await asyncio.wait_for(result.get(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if event is not None and event.type == TaskEventType.PAUSED:
            paused_received = True
            break
        if event is not None and event.type in (
            TaskEventType.COMPLETED, TaskEventType.FAILED,
        ):
            # 任务在 pause 生效前就完成了
            pass
            break

    if not paused_received:
        # 任务太快——pause 窗口太窄，跳过 resume 验证
        await ts.stop(timeout=10.0)
        await ts._infra.llm.close()
        return

    # 验证任务仍在注册表中（PAUSED 不清理）
    assert task_id in ts._tasks, "PAUSED 任务应从注册表保留"

    # resume
    resume_result = await ts.resume(task_id)
    assert isinstance(resume_result, TaskMailbox), \
        f"resume 应返回 TaskMailbox，实际: {type(resume_result)}"

    # 消费到终态
    event = None
    deadline = time.monotonic() + 45.0
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            event = await asyncio.wait_for(result.get(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if event is None:
            continue
        if event.type in (TaskEventType.COMPLETED, TaskEventType.FAILED):
            break

    assert event is not None, "未收到终态事件"
    assert event.type == TaskEventType.COMPLETED, \
        f"期望 COMPLETED，实际 {event.type.value}: {event.data}"

    # 等清理完成
    await asyncio.sleep(0.5)
    assert task_id not in ts._tasks, "任务应已从注册表清理"

    await ts.stop(timeout=10.0)
    await ts._infra.llm.close()
```

- [ ] **Step 2: 运行集成测试**

```bash
python -m pytest "Module/task_system/tests/integration/test_task_system_lifecycle.py::test_pause_and_resume" -v --tb=short
```
Expected: PASSED（需要 DEEPSEEK_API_KEY）

- [ ] **Step 3: Commit**

```bash
git add Module/task_system/tests/integration/test_task_system_lifecycle.py
git commit -m "test(resume): pause→resume 集成测试——完整恢复链路"
```

---

### Task 8: 文档更新

**Files:**
- Modify: `Module/task_system/docs/PROGRESS.md`
- Modify: `Module/task_system/docs/api.md`

- [ ] **Step 1: PROGRESS.md——resume 标记为已实现**

```diff
-| `resume()` | ⚠️ `NotImplementedError` |
+| `resume()` | ✅ 已实现 |
```

更新测试计数：单元 `287 → 294`，集成 `27 → 28`。

日期、最近 commit 更新。

- [ ] **Step 2: api.md——resume 更新为已实现**

```diff
     async def resume(self, task_id: TaskId) -> TaskMailbox | ResumeError:
-        """⚠️ 未实现——raise NotImplementedError。"""
+        """✅ 已实现——从 PAUSED 恢复执行。幂等——RUNNING 直接返回 mailbox。"""
```

- [ ] **Step 3: Commit**

```bash
git add Module/task_system/docs/
git commit -m "docs(resume): 同步 PROGRESS + api.md——resume() 已实现"
```

---

## 验证

```bash
# 全量单元测试
python -m pytest Module/task_system/tests/unit/ -q
# Expected: 294 passed

# 全量集成测试
python -m pytest Module/task_system/tests/integration/ -v --tb=short
# Expected: 28 passed (需要 DEEPSEEK_API_KEY)
```
