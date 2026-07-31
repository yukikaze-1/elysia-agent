# TaskSystem pause 超时确认实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `pause()` 设标志后轮询等待 orchestrator 确认 PAUSED 状态（30s 超时），返回值 None→bool

**Architecture:** 2 文件改动——lifecycle.py 加 timeout 轮询，task_system.py 改为 await 委托。TDD：先写 4 个测试，再实现。

**Tech Stack:** Python 3.13, asyncio

**Design spec:** `docs/superpowers/specs/2026-06-22-pause-timeout-design.md`

## Global Constraints

- agent.py 不改，event_bus.py 不改
- `cancel()` 保持 fire-and-forget
- Orchestrator `_check_interrupts()` 不变
- `import time` 移到 lifecycle.py 模块顶部
- 所有现有 `await ts.pause(tid)` 调用方向后兼容（返回 bool 替代 None）

## File Map

| 操作 | 文件 |
|------|------|
| Modify | `Module/task_system/lifecycle.py` |
| Modify | `Module/task_system/task_system.py` |
| Modify | `Module/task_system/tests/unit/test_task_system.py` |

---

### Task 1: 新增 4 个 pause 测试（TDD——先写失败的测试）

**Files:**
- Modify: `Module/task_system/tests/unit/test_task_system.py`

**Interfaces:**
- Consumes: `TaskSystem.pause(task_id) -> None`（当前签名，测试会失败因为还没改实现）
- Produces: 无——测试文件内部

- [ ] **Step 1: 读取现有测试文件，找到 pause 相关的现有测试**

```bash
grep -n "pause\|PAUSED" Module/task_system/tests/unit/test_task_system.py
```

现有测试只测幂等（不抛异常），不测返回值。新增 4 个测试插入到现有 pause 测试附近。

- [ ] **Step 2: 新增 4 个测试函数（使用现有 helper：_build_task_system, _make_task_state, _make_exec_state, _make_mock_sub_agent）**

```python
@pytest.mark.asyncio
async def test_pause_confirmed():
    """pause() 在 orchestrator 确认 PAUSED 后返回 True（≤1s）。"""
    ts = await _build_task_system()
    tid = TaskId("t1")
    task_state = _make_task_state("t1", TaskStatus.RUNNING)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    t0 = time.monotonic()
    result = await ts.pause(tid, timeout=5.0)
    elapsed = time.monotonic() - t0

    # pause_requested 设置后 mock sub_agent 不会真的跑 orchestrator，
    # status 保持 RUNNING → 应超时返回 False（需待 Task 2 实现后再验证 True 路径）
    # 此测试在 Task 1 阶段预期 FAIL——pause() 返回 None 不可 await
    assert result is True


@pytest.mark.asyncio
async def test_pause_timeout():
    """pause() 在 orchestrator 不响应时超时返回 False。"""
    ts = await _build_task_system()
    tid = TaskId("t2")
    # 使用终态任务——is_terminal() 检查使 pause() 提前返回 False
    task_state = _make_task_state("t2", TaskStatus.COMPLETED)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    t0 = time.monotonic()
    result = await ts.pause(tid, timeout=0.5)
    elapsed = time.monotonic() - t0

    # 终态任务立即返回 False，不进入轮询（需待 Task 2 实现后验证）
    assert result is False
    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_pause_terminal_during_wait():
    """pause() 等待期间任务进入终态——提前返回 False。"""
    ts = await _build_task_system()
    tid = TaskId("t3")
    task_state = _make_task_state("t3", TaskStatus.RUNNING)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    # 模拟：orchestrator 在 pause 轮询期间完成执行
    async def delayed_complete():
        await asyncio.sleep(0.15)
        task_state.status = TaskStatus.COMPLETED
    asyncio.create_task(delayed_complete())

    t0 = time.monotonic()
    result = await ts.pause(tid, timeout=2.0)
    elapsed = time.monotonic() - t0

    assert result is False
    assert elapsed < 0.5   # 应在 detection 后立即返回，不等超时


@pytest.mark.asyncio
async def test_pause_idempotent():
    """对已 PAUSED 的任务——立即返回 True，不进入轮询。"""
    ts = await _build_task_system()
    tid = TaskId("t4")
    task_state = _make_task_state("t4", TaskStatus.PAUSED)
    exec_state = _make_exec_state()
    sub_agent = _make_mock_sub_agent(task_state, exec_state)
    ts._tasks[tid] = sub_agent

    t0 = time.monotonic()
    result = await ts.pause(tid, timeout=5.0)
    elapsed = time.monotonic() - t0

    assert result is True
    assert elapsed < 0.1   # 幂等——立即返回
```

- [ ] **Step 3: 运行测试——预期全部 FAIL**

```bash
source /home/yomu/data/anaconda3/etc/profile.d/conda.sh && conda activate Elysia && \
python -m pytest Module/task_system/tests/unit/test_task_system.py -v \
  -k "pause_confirmed or pause_timeout or pause_terminal_during_wait or pause_idempotent"
```

预期：`TypeError: object NoneType can't be used in 'await' expression`（pause() 返回 None）

- [ ] **Step 4: Commit**

```bash
git add Module/task_system/tests/unit/test_task_system.py
git commit -m "test: 新增 pause 超时确认 4 个测试用例（当前 FAIL——等待实现）"
```

---

### Task 2: `lifecycle.py` — `pause()` 改为 async 轮询

**Files:**
- Modify: `Module/task_system/lifecycle.py` — `pause()` 函数签名改为 async，加 timeout 轮询

**Interfaces:**
- Consumes: `SubAgent._task_state`, `SubAgent._orchestrator._exec_state`
- Produces: `async def pause(tasks, task_id, timeout=30.0) -> bool`

- [ ] **Step 1: 读取当前 `lifecycle.py`**

```bash
cat -n Module/task_system/lifecycle.py | sed -n '1,60p'
```

- [ ] **Step 2: `import time` 移到模块顶部**

在 `import asyncio` 之后添加 `import time`。

- [ ] **Step 3: 将 `pause()` 从 `def` 改为 `async def`，替换函数体**

用 spec 中的完整代码替换。关键变更：
- `def` → `async def`
- 返回类型 `None` → `bool`
- 新增 `timeout: float = 30.0` 参数
- 设 `pause_requested = True` 后进入轮询循环：
  ```python
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
      if ts.status == TaskStatus.PAUSED:
          return True
      if ts.is_terminal():
          return False
      await asyncio.sleep(0.1)
  return False
  ```

- [ ] **Step 4: 运行测试——预期 4 个 pause 测试 PASS**

```bash
source /home/yomu/data/anaconda3/etc/profile.d/conda.sh && conda activate Elysia && \
python -m pytest Module/task_system/tests/unit/test_task_system.py -v \
  -k "pause"
```

- [ ] **Step 5: Commit**

```bash
git add Module/task_system/lifecycle.py
git commit -m "feat: lifecycle.pause() 改为 async 轮询等待 orchestrator 确认 PAUSED"
```

---

### Task 3: `task_system.py` — `pause()` 改为 await 委托

**Files:**
- Modify: `Module/task_system/task_system.py:253-255` — `pause()` 方法签名

**Interfaces:**
- Consumes: `lifecycle.pause(tasks, task_id, timeout) -> bool`
- Produces: `async def pause(self, task_id, timeout=30.0) -> bool`

- [ ] **Step 1: 读取当前 `task_system.py` pause()**

```bash
cat -n Module/task_system/task_system.py | sed -n '253,256p'
```

- [ ] **Step 2: 替换 `pause()` 方法**

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

- [ ] **Step 3: 运行完整测试套件验证向后兼容**

```bash
source /home/yomu/data/anaconda3/etc/profile.d/conda.sh && conda activate Elysia && \
python -m pytest Module/task_system/tests/unit/ -v
```

预期：298 tests passed（含原有的 294 + 新增 4 个 pause 测试）

- [ ] **Step 4: Commit**

```bash
git add Module/task_system/task_system.py
git commit -m "feat: TaskSystem.pause() 改为 await 委托，返回 bool + 支持 timeout"
```
