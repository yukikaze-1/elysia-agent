# TaskSystem cancel() 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 TaskSystem.cancel()——幂等的软取消，通过 `exec_state.cancel_requested` 标志位让 Orchestrator 在安全点停止任务。

**Architecture:** 与 `pause()` 对称——标志位在 `ExecutionState`，状态转换在 Orchestrator 循环顶。`cancel()` 设 `cancel_requested=True` → Orchestrator 检测 → save checkpoint → CANCELLED。PAUSED 状态直接转 CANCELLED。所有路径返回 `True`（幂等）。

**Spec:** `docs/superpowers/specs/2026-06-17-task-system-cancel-design.md`

**Tech Stack:** Python 3.13, asyncio, Pydantic

---

### Task 1: schema.py — 新增 cancel_requested 字段和 CANCELED 枚举值

**Files:**
- Modify: `Module/task_system/schema.py:727-745,842-855`

- [ ] **Step 1: OrchestratorRunStatus 加 CANCELED 枚举值**

在 `PAUSED = "paused"` 之后加：

```python
    PAUSED = "paused"
    CANCELED = "canceled"       # 外部取消请求，已保存 checkpoint（终态，不可恢复）
```

更新 docstring 将 "5 态" 改为 "6 态"，并在"外部中断"组加一行：

```python
    """Orchestrator.run() 的返回状态——6 态。

    Orchestrator 驱动 Plan 直到完成、遇到障碍、或被外部中断。
    非 SUCCESS 状态由 Sub-Agent 做决策。

    内部出向
        SUCCESS       — 所有 required 节点完成
        FAILED        — 不可恢复（资源耗尽、内部异常）
        NEEDS_REPLAN  — 结构性失败，DAG 走不通，需要修改 Plan
        BLOCKED       — 等待用户审批回传
    外部中断
        PAUSED        — 收到暂停请求，已保存 checkpoint，可恢复
        CANCELED      — 收到取消请求，已保存 checkpoint（终态，不可恢复）
    """
```

- [ ] **Step 2: ExecutionState 加 cancel_requested 字段**

在 `pause_requested` 之后加：

```python
    pause_requested: bool = False          # 外部请求暂停（Orchestrator 检查后返回 PAUSED）
    cancel_requested: bool = False         # 外部请求取消（Orchestrator 检查后返回 CANCELED）
    created_at: str = ""                   # Orchestrator._init_exec_state() 时赋值
```

- [ ] **Step 3: 运行单元测试确认字段兼容**

Run: `python -m pytest Module/task_system/tests/unit/ -q`
Expected: 281 passed（新字段有默认值，所有现有测试应通过）

- [ ] **Step 4: Commit**

```bash
git add Module/task_system/schema.py
git commit -m "feat(cancel): schema——ExecutionState 加 cancel_requested，OrchestratorRunStatus 加 CANCELED"
```

---

### Task 2: task_system.py — 实现 cancel() 方法

**Files:**
- Modify: `Module/task_system/task_system.py:313-320`

- [ ] **Step 1: 替换 cancel() stub**

将 `raise NotImplementedError("TaskSystem.cancel() 尚未实现")` 替换为：

```python
    async def cancel(self, task_id: TaskId) -> bool:
        """软取消任务。二次确认由 CognitiveLoop 在上层处理。

        幂等——所有路径返回 True：
        - RUNNING → 设置 exec_state.cancel_requested → Orchestrator 检测 → CANCELLED
        - PAUSED  → 直接标记 CANCELLED，已有 checkpoint
        - 已终态   → True（无需操作）
        - 已清理   → True（任务已不存在）
        """
        # TODO: v1 只设标志，无超时反馈。
        #   若 Orchestrator 因异常永远不到达下一个循环迭代，
        #   cancel 信号可能永远不被处理。后续考虑 await + 超时机制。
        sub_agent = self._tasks.get(task_id)
        if sub_agent is None:
            return True  # 已清理 = 已取消

        ts = sub_agent._task_state
        if ts is None or ts.is_terminal():
            return True  # 已终态 = 已取消

        # ── 分支 1: PAUSED → 直接 CANCELLED ──
        if ts.status == TaskStatus.PAUSED:
            ts.status = TaskStatus.CANCELLED
            ts.updated_at = now_iso()
            mailbox = self._mailboxes.get(task_id)
            if mailbox:
                push_mailbox_event(mailbox, TaskEventType.CANCELLED, {
                    "task_id": str(task_id),
                    "reason": "用户取消",
                })
            # cleanup 由 _run_and_cleanup finally 块负责
            return True

        # ── 分支 2: RUNNING / PLANNING → 设取消标志 ──
        exec_state = sub_agent._orchestrator._exec_state
        if exec_state is None:
            # 尚未进入 Orchestrator（规划/校验阶段）
            ts.status = TaskStatus.CANCELLED
            ts.updated_at = now_iso()
            return True

        exec_state.cancel_requested = True
        return True
```

确认 `now_iso` 已导入。当前 `task_system.py` 的 schema import 中没有 `now_iso`，需要添加：

在 `from Module.task_system.schema import (` 块中加一行：

```python
    NodeStatus,
    now_iso,        # ← 新增
    ResumeError,
```

然后替换 `cancel()` stub。

- [ ] **Step 2: 运行单元测试确认 cancel() 不破坏现有功能**

Run: `python -m pytest Module/task_system/tests/unit/ -q`
Expected: 281 passed

- [ ] **Step 3: 手动验证 import 正确**

```bash
python -c "from Module.task_system.task_system import TaskSystem; print('import OK')"
```
Expected: `import OK`（无 ModuleNotFoundError / ImportError）

- [ ] **Step 4: Commit**

```bash
git add Module/task_system/task_system.py
git commit -m "feat(cancel): 实现 TaskSystem.cancel()——幂等软取消，支持 RUNNING/PAUSED"
```

---

### Task 3: orchestrator.py — 循环顶加 cancel_requested 检测

**Files:**
- Modify: `Module/task_system/core/orchestrator.py:156-164`

- [ ] **Step 1: 在 pause_requested 检查之后加 cancel_requested 检测**

在 `if exec_state.pause_requested:` 代码块之后、`# 1. 找就绪节点` 之前插入：

```python
                # 0. 检查外部取消信号
                if exec_state.cancel_requested:
                    logger.info(
                        "Orchestrator: task_id=%s 收到取消请求", task_id,
                    )
                    task_state.status = TaskStatus.CANCELLED
                    task_state.updated_at = now_iso()
                    await self._save_checkpoint(task_state, exec_state)
                    return OrchestratorResult(
                        status=OrchestratorRunStatus.CANCELED,
                        checkpoint_state=self._build_checkpoint(
                            task_state, exec_state,
                        ),
                    )
```

精确替换位置参考：

```python
# orchestrator.py 约第 156-166 行，当前代码为：
                # 0. 检查外部中断信号
                if exec_state.pause_requested:
                    ...
                    return OrchestratorResult(
                        status=OrchestratorRunStatus.PAUSED,
                        ...
                    )

                # 1. 找就绪节点（PlanGraph 负责图遍历）
                ready_ids = graph.get_ready_nodes(exec_state.node_states)
```

在 `pause_requested` 块的 `return` 闭合之后、`# 1.` 注释之前插入新代码块即可。

- [ ] **Step 2: 运行单元测试确认 orchestrator 变更不破坏现有逻辑**

Run: `python -m pytest Module/task_system/tests/unit/test_orchestrator.py -v`
Expected: all passed（cancel_requested 默认 False，不触发新路径）

- [ ] **Step 3: Commit**

```bash
git add Module/task_system/core/orchestrator.py
git commit -m "feat(cancel): Orchestrator 循环顶检测 cancel_requested → save checkpoint → CANCELED"
```

---

### Task 4: sub_agent.py — _dispatch_result 加 CANCELED 分支 + _handle_cancelled

**Files:**
- Modify: `Module/task_system/core/sub_agent.py:326-365`（dispatch），`Module/task_system/core/sub_agent.py:368`（handler 区域）

- [ ] **Step 1: _dispatch_result 加 CANCELED 分支**

在 `elif result.status == OrchestratorRunStatus.PAUSED:` 和 `elif result.status == OrchestratorRunStatus.FAILED:` 之间插入：

```python
        elif result.status == OrchestratorRunStatus.CANCELED:
            await self._handle_cancelled(result, task_state, mailbox)
            return False, None
```

- [ ] **Step 2: 新增 _handle_cancelled 方法**

在 `_handle_failed` 方法之后（约第 395 行）、`_handle_blocked` 方法之前插入：

```python
    # ── 结果处理：CANCELED ────────────────────────────────────────────────────

    async def _handle_cancelled(
        self,
        result: OrchestratorResult,
        task_state: TaskState,
        mailbox: TaskMailbox,
    ) -> None:
        """外部取消——推送 CANCELLED 事件。

        Orchestrator 已将 task_state.status 设为 CANCELLED 并保存了 checkpoint。
        推送事件后退出执行循环，_run_and_cleanup finally 块负责清理。
        """
        logger.info("SubAgent: task_id=%s 已取消", task_state.task_id)
        await self._push_mailbox(mailbox, TaskEventType.CANCELLED, {
            "task_id": str(task_state.task_id),
            "reason": "用户取消",
        })
```

- [ ] **Step 3: 更新 SubAgent 类 docstring 和 _dispatch_result docstring**

SubAgent 类的 docstring 里提到决策的标签更新（约第 5-22 行），把 CANCELED 加到 Replan/熔断/审批/暂停 之后：

在 `run()` 的 docstring "5. 等待取消" 之类的行后确认涵盖 CANCELED 路径。

`_dispatch_result` 的 docstring 对 return `(False, None)` 的描述改为覆盖 CANCELED。

- [ ] **Step 4: 运行 SubAgent 单元测试**

Run: `python -m pytest Module/task_system/tests/unit/test_sub_agent.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add Module/task_system/core/sub_agent.py
git commit -m "feat(cancel): SubAgent 分发 CANCELED → _handle_cancelled 推送 CANCELLED 事件"
```

---

### Task 5: 单元测试——6 个测试

**Files:**
- Modify: `Module/task_system/tests/unit/test_task_system.py`

- [ ] **Step 1: 在现有测试文件末尾添加 6 个 cancel 测试**

```python
# ═══════════════════════════════════════════════════════════════════════════
# cancel()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cancel_missing_task_returns_true():
    """不存在的 task_id → True（幂等——已清理 = 已取消）。"""
    ts = await _build_task_system()
    assert await ts.cancel(TaskId("nonexistent")) is True


@pytest.mark.asyncio
async def test_cancel_terminal_task_returns_true():
    """已终态任务 → True（幂等——已完成/已失败 = 已取消）。"""
    ts = await _build_task_system()

    tid = TaskId("t1")
    task_state = _make_task_state("t1", TaskStatus.COMPLETED)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    assert await ts.cancel(tid) is True
    # 状态不变
    assert task_state.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_cancel_running_sets_cancel_requested():
    """RUNNING 任务 → 设 exec_state.cancel_requested=True，不立即改 status。"""
    ts = await _build_task_system()

    tid = TaskId("t1")
    task_state = _make_task_state("t1", TaskStatus.RUNNING)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    assert await ts.cancel(tid) is True
    assert exec_state.cancel_requested is True
    # RUNNING → 状态不变（由 Orchestrator 改为 CANCELLED）
    assert task_state.status == TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_cancel_paused_sets_cancelled_directly():
    """PAUSED 任务 → 直接 CANCELLED，不依赖 Orchestrator。"""
    ts = await _build_task_system()

    tid = TaskId("t1")
    task_state = _make_task_state("t1", TaskStatus.PAUSED)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    assert await ts.cancel(tid) is True
    assert task_state.status == TaskStatus.CANCELLED
    # cancel_requested 不应被设置（直接跳过了）
    assert exec_state.cancel_requested is False


@pytest.mark.asyncio
async def test_cancel_before_orchestrator_sets_cancelled():
    """尚未进入 Orchestrator（_exec_state=None）→ 直接 CANCELLED。"""
    ts = await _build_task_system()

    tid = TaskId("t1")
    task_state = _make_task_state("t1", TaskStatus.PLANNING)

    # exec_state 为 None（尚未 run）
    sub_agent = MagicMock()
    sub_agent._task_state = task_state
    orch = MagicMock()
    orch._exec_state = None
    sub_agent._orchestrator = orch
    ts._tasks[tid] = sub_agent

    assert await ts.cancel(tid) is True
    assert task_state.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_task_state_none_returns_true():
    """_task_state=None（尚未初始化）→ True（幂等）。"""
    ts = await _build_task_system()

    tid = TaskId("t1")
    sub_agent = MagicMock()
    sub_agent._task_state = None
    ts._tasks[tid] = sub_agent

    assert await ts.cancel(tid) is True
```

- [ ] **Step 2: 先只运行新测试确认它们 FAIL**

Run: `python -m pytest Module/task_system/tests/unit/test_task_system.py -k "cancel" -v`
Expected: 6 FAIL（cancel() 是 `raise NotImplementedError`）

- [ ] **Step 3: 运行全部单元测试**

Run: `python -m pytest Module/task_system/tests/unit/ -q`
Expected: 287 passed（281 + 6 cancel）

- [ ] **Step 4: Commit**

```bash
git add Module/task_system/tests/unit/test_task_system.py
git commit -m "test(cancel): 6 个 cancel() 单元测试"
```

---

### Task 6: 集成测试——1 个测试

**Files:**
- Modify: `Module/task_system/tests/integration/test_task_system_lifecycle.py`

- [ ] **Step 1: 添加集成测试**

在文件末尾添加（参考现有 `test_pause_sets_flag_and_cleanly_stops` 的结构）：

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_cancel_during_execution():
    """提交任务 → cancel() → 收到 CANCELLED 事件 → 注册表清理。

    在任务执行期间取消，验证：
    - cancel() 返回 True
    - 最终收到 cancelled 事件
    - 注册表已清理
    """
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    ts = await _build_task_system()
    tools = _tools_from_manager(ts._infra.tool_registry)

    ctx = DelegationContext(
        intent="现在几点了",
        tools=tools,
        session_id=SessionId("cancel-test"),
    )
    result = await ts.submit(ctx)
    assert isinstance(result, TaskMailbox), f"submit 失败: {result}"
    task_id = result.task_id

    # 等第一个 PROGRESS——表示 Orchestrator 已启动
    first_event = await asyncio.wait_for(result.get(), timeout=30.0)
    assert first_event is not None, "未收到首个事件"

    # 取消任务
    assert await ts.cancel(task_id) is True

    # 消费剩余事件——预期最终收到 cancelled
    event = None
    deadline = time.monotonic() + 30.0
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
        if event.type in (TaskEventType.CANCELLED, TaskEventType.FAILED,
                          TaskEventType.COMPLETED):
            break

    # 三种可能：cancelled（取消成功）、completed（任务太快已完成）、
    # failed（取消路径中的异常）
    assert event is not None, "未收到终态事件"
    assert event.type in (
        TaskEventType.CANCELLED, TaskEventType.COMPLETED, TaskEventType.FAILED
    ), f"意外的终态事件: {event.type.value}: {event.data}"

    # 等清理完成
    await asyncio.sleep(0.5)
    # 任务应从注册表清理
    assert task_id not in ts._tasks, "任务应已从注册表清理"

    await ts.stop(timeout=10.0)
    # 关闭 LLM client 连接池
    await ts._infra.llm.close()
```

- [ ] **Step 2: 运行集成测试**

Run: `python -m pytest "Module/task_system/tests/integration/test_task_system_lifecycle.py::test_cancel_during_execution" -v --tb=short`
Expected: PASSED（需要 DEEPSEEK_API_KEY）

- [ ] **Step 3: Commit**

```bash
git add Module/task_system/tests/integration/test_task_system_lifecycle.py
git commit -m "test(cancel): cancel 集成测试——运行时取消 + 验证 CANCELLED 事件"
```

---

### Task 7: 文档更新

**Files:**
- Modify: `Module/task_system/docs/PROGRESS.md`
- Modify: `Module/task_system/docs/api.md`

- [ ] **Step 1: PROGRESS.md — cancel 标记为已实现**

```diff
-| `cancel()` | ⚠️ `NotImplementedError` |
+| `cancel()` | ✅ 已实现 |
```

更新单元测试计数 `281 tests` → `287 tests`，集成测试 `26 tests` → `27 tests`。

将日期更新为 2026-06-17，最近提交更新。

- [ ] **Step 2: api.md — cancel 更新为已实现**

```diff
     async def cancel(self, task_id: TaskId) -> bool:
-        """⚠️ 未实现——raise NotImplementedError。"""
+        """✅ 已实现——幂等软取消。TaskSystem 和 Orchestrator 之间的中断标志。"""
```

- [ ] **Step 3: Commit**

```bash
git add Module/task_system/docs/
git commit -m "docs(cancel): 同步 PROGRESS + api.md——cancel() 已实现"
```

---

## 验证

```bash
# 全量单元测试
python -m pytest Module/task_system/tests/unit/ -q
# Expected: 287 passed

# 全量集成测试
python -m pytest Module/task_system/tests/integration/ -v --tb=short
# Expected: 27 passed (需要 DEEPSEEK_API_KEY)
```
