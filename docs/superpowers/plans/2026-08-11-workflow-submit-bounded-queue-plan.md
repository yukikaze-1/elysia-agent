# WorkflowEngine submit 非阻塞有界排队实施计划（E2）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `WorkflowEngine.submit()` 立即返回 mailbox，执行槽 acquire 移到后台有界队列 runner；PAUSED 释放 slot；队列满快速失败——聊天轮永不被槽位阻塞。

**Architecture:** `WorkflowEngine` 增加有界 `asyncio.Queue[QueuedItem(task_id, kind)]` + 单后台 `_drain_queue` worker。`submit()` 预创建 `WorkflowState(status=QUEUED)` + 注册 `_tasks`/`_mailboxes`/`_contexts` 后入队即返；worker acquire 后按 kind 派发 `_run_and_cleanup`/`_run_resume`（先提取为方法）。PAUSED 任务在 finally 释放 slot。`stop()` 扫描 `_tasks` 终态化 QUEUED/PAUSED。

**Tech Stack:** Python 3.12 + asyncio（BoundedSemaphore/Queue/Lock）+ `Module/workflow_contract`（WorkflowStatus/WorkflowEventType/SubmitRejectionCode）+ `Module/workflow_engine/schema`（ResumeErrorCode/WorkflowState）。

## Global Constraints

- 生产代码函数参数必须类型注解；禁相对导入；注释中文。
- 每 Task TDD；验收门禁全绿（`make test-quick`/`test-normal`/pyright/ruff/contract drift）。
- `pytest.ini` 无 `asyncio_mode`（strict）：所有 async 测试须 `@pytest.mark.asyncio`。
- **测试文件模块级 import 补全**：`test_workflow_engine.py` 现仅局部 import `DelegationContext`/`SessionId`/`SubmitRejectionCode`（`WorkflowEngine` 也在 `_build_task_system` 内 lazy import）——各 Task 测试片段用到的 `WorkflowEngine` 来自 `Module.workflow_engine.workflow_engine`，`DelegationContext`/`SessionId`/`SubmitRejectionCode`/`WorkflowStatus`/`WorkflowEventType` 来自 `Module.workflow_contract`，`QueuedItem`/`QueuedItemKind` 来自 `Module.workflow_engine.workflow_engine`，`WorkflowMailbox` 来自 `Module.workflow_engine.mailbox`——**Task 2 起在文件顶部补全这些 import**（否则收集即 NameError）。
- **已有大量测试用 `_build_task_system()` 只 start 不 stop**——Task 3 起 `start()` 创建常驻 worker，存量测试会泄漏 pending task（"Task was destroyed but it is pending!" 告警，**非门禁错误**）。Task 10 Step 2 按修正方案处理（补 `await ts.stop()` 或接受 warning）。

## 文件结构

| 文件 | 职责 |
|---|---|
| `Module/workflow_contract/schema.py` | `WorkflowStatus`+QUEUED、`WorkflowEventType`+QUEUED、`SubmitRejectionCode`+QUEUE_FULL（均在契约层，已核实 :64/:360/:99） |
| `Module/workflow_engine/schema/types.py` | `ResumeErrorCode`+QUEUE_FULL（引擎 schema，:106——**不在契约层**） |
| `Module/workflow_engine/schema/internal.py` | `WorkflowState`+`resume_pending: bool = False`（:110——**不在契约层**） |
| `Module/workflow_engine/workflow_engine.py` | 提取 `_run_and_cleanup`/`_run_resume` 方法 + `submit`/`_drain_queue`/`resume`/`stop` + `QueuedItem`/`QueuedItemKind` + `_active`/`_queue`/`_queue_worker`/`_contexts` |
| `Module/workflow_engine/lifecycle.py` | `cancel()` +QUEUED 分支、PAUSED 分支去 release |
| `Module/workflow_engine/core/sub_agent/sub_agent.py` | `run()` 复用预创建 WorkflowState |
| `Module/workflow_engine/query.py` | `get_debug_snapshot` slots_used/queued 修正 |
| `Module/workflow_engine/tests/unit/test_workflow_engine.py` | 主要测试落点 |
| `Module/workflow_contract/tests/` | 契约测试 |

---

### Task 1: 契约与 schema 扩展（正确文件位置）

**Files:**
- Modify: `Module/workflow_contract/schema.py`（WorkflowStatus/WorkflowEventType/SubmitRejectionCode）
- Modify: `Module/workflow_engine/schema/types.py`（ResumeErrorCode）
- Modify: `Module/workflow_engine/schema/internal.py`（WorkflowState）
- Test: `Module/workflow_contract/tests/` + `Module/workflow_engine/tests/unit/test_schema.py`

**Interfaces:**
- Produces: `WorkflowStatus.QUEUED`、`WorkflowEventType.QUEUED`、`SubmitRejectionCode.QUEUE_FULL`（契约层）；`ResumeErrorCode.QUEUE_FULL`（`Module.workflow_engine.schema.types`）；`WorkflowState.resume_pending: bool = False`（`Module.workflow_engine.schema.internal`）。

- [ ] **Step 1: 写失败测试**

`Module/workflow_contract/tests/`（契约层）：
```python
def test_workflow_status_has_queued():
    assert WorkflowStatus.QUEUED.value == "queued"

def test_workflow_event_type_has_queued():
    assert WorkflowEventType.QUEUED.value == "queued"

def test_submit_rejection_has_queue_full():
    assert SubmitRejectionCode.QUEUE_FULL.value == "QUEUE_FULL"
```

`Module/workflow_engine/tests/unit/test_schema.py`（引擎 schema）：
```python
from Module.workflow_contract import WorkflowStatus, WorkflowId, SessionId   # WorkflowId/SessionId 在 contract（已迁移）
from Module.workflow_engine.schema.types import ResumeErrorCode
from Module.workflow_engine.schema.internal import WorkflowState

def test_resume_error_has_queue_full():
    assert ResumeErrorCode.QUEUE_FULL.value == "QUEUE_FULL"

def test_workflow_state_resume_pending_default_false():
    st = WorkflowState(task_id=WorkflowId("t1"), status=WorkflowStatus.QUEUED, tools=[], session_id=SessionId("s1"))
    assert st.resume_pending is False
```

- [ ] **Step 2: 运行验证失败**

Run: `conda run -n Elysia python -m pytest Module/workflow_contract/tests Module/workflow_engine/tests/unit/test_schema.py -q -k "queued or queue_full or resume_pending" -x --tb=short`
Expected: FAIL（枚举/字段不存在）

- [ ] **Step 3: 最小实现**

- `Module/workflow_contract/schema.py`：`WorkflowStatus` 加 `QUEUED = "queued"`（PENDING 后，注释 12 态）；`WorkflowEventType` 加 `QUEUED = "queued"`（7 种）；`SubmitRejectionCode` 加 `QUEUE_FULL = "QUEUE_FULL"`。
- `Module/workflow_engine/schema/types.py`：`ResumeErrorCode` 加 `QUEUE_FULL = "QUEUE_FULL"`。
- `Module/workflow_engine/schema/internal.py`：`WorkflowState` 加 `resume_pending: bool = False`。

- [ ] **Step 4: 运行验证通过**

Run: 同上命令
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add Module/workflow_contract/schema.py Module/workflow_engine/schema/types.py Module/workflow_engine/schema/internal.py Module/workflow_contract/tests/ Module/workflow_engine/tests/unit/test_schema.py
git commit -m "feat(workflow): QUEUED status/event and QUEUE_FULL error codes (E2)"
```

---

### Task 2: 提取 `_run_and_cleanup`/`_run_resume` 为方法 + init 字段 + `_contexts`

**Files:**
- Modify: `Module/workflow_engine/workflow_engine.py`
- Test: `Module/workflow_engine/tests/unit/test_workflow_engine.py`

**Interfaces:**
- Consumes: 无（纯重构）。
- Produces: 类方法 `_run_and_cleanup(self, sub_agent, task_id)`、`_run_resume(self, sub_agent, task_id)`（从 submit()/resume() 闭包提取，行为不变）；`self._contexts: dict[WorkflowId, DelegationContext]`；`__init__` 加 `max_queue_size: int = 5` + `_queue`/`_queue_worker`/`_active` 字段（None/空，Task 3 才激活）。

- [ ] **Step 1: 写失败测试**（**提取后必须由门禁测试覆盖 submit→_run_and_cleanup→完成**——否则错误的提取（参数序/注册表取参）只被 external e2e 抓到；resume 路径已有 `test_resume_paused_again_keeps_task_alive` 覆盖）

```python
class TestRunCleanupExtracted:
    @pytest.mark.asyncio
    async def test_submit_run_cleanup_completes(self):
        """提取的 _run_and_cleanup 方法：调 sub_agent.run → 完成清理。"""
        engine = WorkflowEngine(infra=MagicMock(), hitl_notifier=MagicMock(), max_sub_agents=1)
        await engine.start()
        sub_agent = MagicMock()
        ts = MagicMock()
        ts.status = WorkflowStatus.PENDING
        ts.is_terminal.return_value = False
        sub_agent._task_state = ts
        sub_agent.run = AsyncMock()
        engine._tasks[WorkflowId("t1")] = sub_agent
        engine._mailboxes[WorkflowId("t1")] = WorkflowMailbox(WorkflowId("t1"))
        engine._contexts[WorkflowId("t1")] = DelegationContext(intent="t", session_id=SessionId("s1"))
        await engine._semaphore.acquire()                     # 必须 acquire——finally 终态分支无条件 release（防越界 ValueError）
        engine._active.add(WorkflowId("t1"))
        await engine._run_and_cleanup(sub_agent, WorkflowId("t1"))
        sub_agent.run.assert_awaited_once()      # 用 _contexts/_mailboxes 正确取参调 run
        assert WorkflowId("t1") not in engine._tasks
        await engine.stop()
```

- [ ] **Step 2: 运行验证失败**

Run: `conda run -n Elysia python -m pytest Module/workflow_engine/tests/unit/test_workflow_engine.py -q --tb=short`
Expected: 现有 PASS（基线绿；本测试在提取后 PASS——提取前 `_run_and_cleanup` 非方法则 AttributeError）

- [ ] **Step 3: 最小实现**

把 `submit()` 内 `_run_and_cleanup` 闭包和 `resume()` 内 `_run_resume` 闭包**原样提取**为类方法（保留 try/except/finally 逻辑与异常处理），签名 `(self, sub_agent, task_id)`，从 `self._contexts[task_id]` / `self._mailboxes[task_id]` 取 context/mailbox。`submit()`/`resume()` 改为 `asyncio.create_task(self._run_and_cleanup(sub_agent, task_id))`。`__init__` 加字段：
```python
self._max_queue_size = max_queue_size
self._queue: asyncio.Queue["QueuedItem"] | None = None
self._queue_worker: asyncio.Task[None] | None = None
self._active: set[WorkflowId] = set()
self._contexts: dict[WorkflowId, DelegationContext] = {}
```
`submit()` 注册 `self._contexts[task_id] = context`；`_cleanup_task` 顺带 `self._contexts.pop(task_id, None)`。

- [ ] **Step 4: 运行验证通过**

Run: `conda run -n Elysia python -m pytest Module/workflow_engine/tests/unit/test_workflow_engine.py -q --tb=short`
Expected: PASS（与 Step 2 基线一致，无回归）

- [ ] **Step 5: 提交**

```bash
git add Module/workflow_engine/workflow_engine.py Module/workflow_engine/tests/unit/test_workflow_engine.py
git commit -m "refactor(workflow): extract run wrappers as methods with contexts registry (E2)"
```

---

### Task 3: `QueuedItem` + `_drain_queue` + `start()` 创建 worker

**Files:**
- Modify: `Module/workflow_engine/workflow_engine.py`
- Test: `Module/workflow_engine/tests/unit/test_workflow_engine.py`

**Interfaces:**
- Consumes: Task 2 方法 + `_contexts`。
- Produces: `QueuedItemKind(StrEnum)`（SUBMIT/RESUME）、`QueuedItem`（frozen dataclass）；`_drain_queue()`；`start()` 创建 `_queue` + `_queue_worker`。

- [ ] **Step 1: 写失败测试**

```python
class TestDrainQueue:
    @pytest.mark.asyncio
    async def test_start_creates_queue_and_worker(self):
        engine = WorkflowEngine(infra=MagicMock(), hitl_notifier=MagicMock(), max_queue_size=2)
        await engine.start()
        assert engine._queue is not None and engine._queue.maxsize == 2
        assert engine._queue_worker is not None and not engine._queue_worker.done()
        await engine.stop()

    @pytest.mark.asyncio
    async def test_submit_item_dispatches_run_and_cleanup(self, monkeypatch):
        engine = WorkflowEngine(infra=MagicMock(), hitl_notifier=MagicMock(), max_sub_agents=1)
        await engine.start()
        sub_agent = MagicMock()
        ts = MagicMock()
        ts.status = WorkflowStatus.QUEUED
        ts.is_terminal.return_value = False
        sub_agent._task_state = ts
        tid = WorkflowId("t1")
        engine._tasks[tid] = sub_agent
        engine._mailboxes[tid] = WorkflowMailbox(tid)
        engine._contexts[tid] = DelegationContext(intent="t", session_id=SessionId("s1"))
        ran = []
        async def fake(sub, task_id):
            ran.append(task_id)
        monkeypatch.setattr(engine, "_run_and_cleanup", fake)
        engine._queue.put_nowait(QueuedItem(tid, QueuedItemKind.SUBMIT))
        await asyncio.sleep(0.05)
        assert ran == [tid]
        await engine.stop()

    @pytest.mark.asyncio
    async def test_missing_task_skipped(self):
        engine = WorkflowEngine(infra=MagicMock(), hitl_notifier=MagicMock())
        await engine.start()
        engine._queue.put_nowait(QueuedItem(WorkflowId("nope"), QueuedItemKind.SUBMIT))
        await asyncio.sleep(0.05)
        assert engine._queue.qsize() == 0
        await engine.stop()
```

> 注：`DelegationContext(intent=..., session_id=...)` 是正确构造——`tools`/`task_constraints`/`summarized_context`/`return_mode` 均有默认；**无 `constraints` 字段**（正确为 `task_constraints`）。

- [ ] **Step 2: 运行验证失败**

Run: `conda run -n Elysia python -m pytest Module/workflow_engine/tests/unit/test_workflow_engine.py -q -k "DrainQueue" -x --tb=short`
Expected: FAIL（`QueuedItem`/`_drain_queue`/`start` worker 不存在）

- [ ] **Step 3: 最小实现**

`workflow_engine.py` 顶部：
```python
class QueuedItemKind(StrEnum):
    SUBMIT = "submit"
    RESUME = "resume"

@dataclass(frozen=True)
class QueuedItem:
    task_id: WorkflowId
    kind: QueuedItemKind
```

`start()` 内创建 semaphore 后：
```python
self._queue = asyncio.Queue(maxsize=self._max_queue_size)
self._queue_worker = asyncio.create_task(self._drain_queue())
```

`_drain_queue`（spec §4.3）：
```python
async def _drain_queue(self) -> None:
    while True:
        item: QueuedItem = await self._queue.get()
        task_id = item.task_id
        dispatched = False
        held_permit = False
        try:
            sub_agent = self._tasks.get(task_id)
            if sub_agent is None:
                continue
            ts = sub_agent._task_state
            if ts is None or ts.is_terminal():
                self._cleanup_task(task_id)
                continue
            await self._semaphore.acquire()
            held_permit = True
            self._active.add(task_id)
            if task_id not in self._tasks:
                self._active.discard(task_id)
                self._semaphore.release()
                held_permit = False
                continue
            try:
                if item.kind is QueuedItemKind.SUBMIT:
                    task = asyncio.create_task(self._run_and_cleanup(sub_agent, task_id))
                else:
                    task = asyncio.create_task(self._run_resume(sub_agent, task_id))
            except Exception:
                self._active.discard(task_id)
                self._semaphore.release()
                held_permit = False
                self._cleanup_task(task_id)
                continue
            self._task_futures[task_id] = task
            dispatched = True
        except asyncio.CancelledError:
            if held_permit and not dispatched:
                self._active.discard(task_id)
                self._semaphore.release()
            raise
        finally:
            self._queue.task_done()
```

- [ ] **Step 4: 运行验证通过**

Run: `conda run -n Elysia python -m pytest Module/workflow_engine/tests/unit/test_workflow_engine.py -q --tb=short`
Expected: PASS。若存量 `_build_task_system()` 测试因 worker 泄漏出现 "Task was destroyed" 告警——记录待 Task 10 清理（先不阻塞本步）。

- [ ] **Step 5: 提交**

```bash
git add Module/workflow_engine/workflow_engine.py Module/workflow_engine/tests/unit/test_workflow_engine.py
git commit -m "feat(workflow): bounded queue drain loop and background worker (E2)"
```

---

### Task 4: `submit()`——预创建 QUEUED 状态 + 入队即返

**Files:**
- Modify: `Module/workflow_engine/workflow_engine.py`
- Modify: `Module/workflow_engine/core/sub_agent/sub_agent.py`
- Test: `Module/workflow_engine/tests/unit/test_workflow_engine.py`

**Interfaces:**
- Consumes: Task 1 `WorkflowStatus.QUEUED`、Task 3 `QueuedItem(SUBMIT)`。
- Produces: 新 `submit()`——立即返回 mailbox / `SubmitError(QUEUE_FULL)`；`SubAgent.run()` 复用预创建 `_task_state`。

- [ ] **Step 1: 写失败测试**（红阶段用 `asyncio.wait_for` 防旧实现无限阻塞挂起）

```python
class TestSubmitNonBlocking:
    @pytest.mark.asyncio
    async def test_submit_returns_mailbox_immediately(self):
        engine = WorkflowEngine(infra=MagicMock(), hitl_notifier=MagicMock(), max_sub_agents=1)
        await engine.start()
        engine._semaphore.acquire_nowait()          # 占满唯一槽
        engine._active.add(WorkflowId("busy"))
        ctx = DelegationContext(intent="test", session_id=SessionId("s1"))
        started = time.monotonic()
        result = await asyncio.wait_for(engine.submit(ctx), timeout=1.0)   # 旧实现会挂起 → wait_for 超时失败
        assert isinstance(result, WorkflowMailbox)
        assert time.monotonic() - started < 1.0
        assert engine.get_task_status(result.task_id) == WorkflowStatus.QUEUED
        await engine.stop()

    @pytest.mark.asyncio
    async def test_submit_queue_full_returns_error(self):
        engine = WorkflowEngine(infra=MagicMock(), hitl_notifier=MagicMock(), max_sub_agents=1, max_queue_size=1)
        await engine.start()
        engine._semaphore.acquire_nowait()
        ctx = DelegationContext(intent="t", session_id=SessionId("s1"))
        await engine.submit(ctx)                    # 第 1 个入队
        result = await engine.submit(ctx)           # 第 2 个 → 队满
        assert isinstance(result, SubmitError)
        assert result.code == SubmitRejectionCode.QUEUE_FULL
        await engine.stop()
```

- [ ] **Step 2: 运行验证失败**

Run: `conda run -n Elysia python -m pytest Module/workflow_engine/tests/unit/test_workflow_engine.py -q -k "SubmitNonBlocking" -x --tb=short`
Expected: FAIL（wait_for 超时——旧 submit 阻塞 acquire；或 QUEUED 断言失败）

- [ ] **Step 3: 最小实现**

`submit()` 重构（spec §4.4）：前置校验（`_started`/intent 非空）→ 建 task_id/mailbox/sub_agent → 预创建 `WorkflowState(status=QUEUED, tools=context.tools, session_id=context.session_id, ...)` 注入 `sub_agent._task_state` → 注册 `_tasks`/`_mailboxes`/`_contexts` → `push_mailbox_event(mailbox, QUEUED, ...)` → `put_nowait(QueuedItem(task_id, SUBMIT))`（`except asyncio.QueueFull` → `_cleanup_task` + `SubmitError(QUEUE_FULL)`）→ 返回 mailbox。**删除方法内 `await self._semaphore.acquire()`。**

`sub_agent.py` `run()` 开头：
```python
if self._task_state is None:
    # 原创建逻辑（保留）——直接使用 SubAgent 的测试路径
    task_state = WorkflowState(task_id=task_id, status=WorkflowStatus.PENDING, ...)
    self._task_state = task_state
else:
    # E2：submit() 已预创建 QUEUED 状态，复用并转入 PENDING
    task_state = self._task_state                              # 关键：本地 task_state 必须绑定（下游 :166/:193/:199 使用）
    if task_state.is_terminal():                               # 终态守卫：drain 复核后、run() 执行前被 cancel → 不再复活
        raise asyncio.CancelledError()
    task_state.status = WorkflowStatus.PENDING
    task_state.tools = context.tools
    task_state.session_id = context.session_id
    task_state.updated_at = now_iso()
```

- [ ] **Step 4: 运行验证通过**

Run: `conda run -n Elysia python -m pytest Module/workflow_engine/tests/unit/test_workflow_engine.py -q --tb=short`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add Module/workflow_engine/workflow_engine.py Module/workflow_engine/core/sub_agent/sub_agent.py Module/workflow_engine/tests/unit/test_workflow_engine.py
git commit -m "feat(workflow): nonblocking submit with QUEUED state and bounded queue (E2)"
```

---

### Task 5: PAUSED 释放 slot（改 Task 2 提取方法的 finally）

**Files:**
- Modify: `Module/workflow_engine/workflow_engine.py`
- Test: `Module/workflow_engine/tests/unit/test_workflow_engine.py`

**Interfaces:**
- Consumes: Task 2 方法、`_active`。
- Produces: `_run_and_cleanup` finally 的 PAUSED 分支 release slot + `_active` 移除（保留注册表）；**`_run_resume` 的 PAUSED release 在 Task 6**（当前 resume 未 acquire，改它会导致 `test_resume_paused_again_keeps_task_alive` 越界释放）。

- [ ] **Step 1: 写失败测试**

```python
class TestPausedReleasesSlot:
    @pytest.mark.asyncio
    async def test_paused_branch_releases_slot_keeps_registry(self):
        engine = WorkflowEngine(infra=MagicMock(), hitl_notifier=MagicMock(), max_sub_agents=1)
        await engine.start()
        sub_agent = MagicMock()
        sub_agent._task_state = MagicMock()
        sub_agent._task_state.status = WorkflowStatus.PAUSED
        tid = WorkflowId("t1")
        engine._tasks[tid] = sub_agent
        engine._mailboxes[tid] = WorkflowMailbox(tid)
        engine._contexts[tid] = DelegationContext(intent="t", session_id=SessionId("s1"))
        await engine._semaphore.acquire()
        engine._active.add(tid)
        async def fake_run(context, mailbox):
            sub_agent._task_state.status = WorkflowStatus.PAUSED   # run 中途暂停
        sub_agent.run = fake_run
        await engine._run_and_cleanup(sub_agent, tid)
        assert engine._semaphore.locked() is False   # slot 已释放
        assert tid in engine._tasks                   # registry 保留
        assert tid not in engine._active
        await engine.stop()
```

- [ ] **Step 2: 运行验证失败**

Expected: FAIL（PAUSED 分支不 release）

- [ ] **Step 3: 最小实现**

**仅改 `_run_and_cleanup` 的 finally**（`_run_resume` 的 PAUSED 释放**推迟到 Task 6**——当前 `resume()` 尚未 acquire semaphore，若本 Task 就在 `_run_resume` 的 PAUSED 分支 release，会越界抛 `ValueError`（`test_resume_paused_again_keeps_task_alive` 必挂））：

```python
# _run_and_cleanup finally（spec E2 §5）：
finally:
    if (sub_agent._task_state is not None and
            sub_agent._task_state.status == WorkflowStatus.PAUSED):
        if self._semaphore:
            self._semaphore.release()          # 释放执行槽
        self._active.discard(task_id)
        return                                 # checkpoint/mailbox/注册表保留
    self._cleanup_task(task_id)
    self._semaphore.release()                  # 终态分支 release 先于 cleanup_storage（E3 §3.4）
    self._active.discard(task_id)
    try:
        await self._cleanup_storage(task_id, sub_agent)
    except Exception:
        logger.exception("WorkflowEngine: task_id=%s 存储清理失败", task_id)
```
（注：`_cleanup_storage` 前已 release——E2/E3 一致；`_run_resume` 的 PAUSED 分支同步 release 在 Task 6 resume acquire 落地后补。）

- [ ] **Step 4: 运行验证通过**

Run: `conda run -n Elysia python -m pytest Module/workflow_engine/tests/unit/test_workflow_engine.py -q --tb=short`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add Module/workflow_engine/workflow_engine.py Module/workflow_engine/tests/unit/test_workflow_engine.py
git commit -m "feat(workflow): paused tasks release execution slot (E2)"
```

---

### Task 6: `resume()`——槽空直接 / 槽满入队 + `resume_pending` 幂等

**Files:**
- Modify: `Module/workflow_engine/workflow_engine.py`
- Test: `Module/workflow_engine/tests/unit/test_workflow_engine.py`

**Interfaces:**
- Consumes: Task 1 `resume_pending`、Task 3 `QueuedItem(RESUME)`。
- Produces: `resume()` 新契约——槽空直接恢复；槽满入队（保持 PAUSED）；队列满 `ResumeError(QUEUE_FULL)`；重复 resume 幂等。

- [ ] **Step 1: 写失败测试**（**MagicMock 的 `resume_pending` 必须显式设 False**——否则 Mock 属性真值短路）

```python
class TestResumeBounded:
    def _paused_task(self, engine, tid="t1"):
        sa = MagicMock()
        ts = MagicMock()
        ts.status = WorkflowStatus.PAUSED
        ts.is_terminal.return_value = False
        ts.resume_pending = False                      # 关键：显式设 False，防 Mock 真值短路
        sa._task_state = ts
        engine._tasks[WorkflowId(tid)] = sa
        engine._mailboxes[WorkflowId(tid)] = WorkflowMailbox(WorkflowId(tid))
        return sa, ts

    @pytest.mark.asyncio
    async def test_resume_slot_busy_enqueues_stays_paused(self):
        engine = WorkflowEngine(infra=MagicMock(), hitl_notifier=MagicMock(), max_sub_agents=1)
        await engine.start()
        sa, ts = self._paused_task(engine)
        engine._semaphore.acquire_nowait()            # 槽占满
        result = await engine.resume(WorkflowId("t1"))
        assert isinstance(result, WorkflowMailbox)
        assert ts.status == WorkflowStatus.PAUSED      # 保持 PAUSED
        assert ts.resume_pending is True               # 入队标记
        assert engine._queue.qsize() == 1
        await engine.stop()

    @pytest.mark.asyncio
    async def test_resume_double_idempotent(self):
        engine = WorkflowEngine(infra=MagicMock(), hitl_notifier=MagicMock(), max_sub_agents=1)
        await engine.start()
        sa, ts = self._paused_task(engine)
        engine._semaphore.acquire_nowait()
        await engine.resume(WorkflowId("t1"))
        await engine.resume(WorkflowId("t1"))         # 二次 → 幂等短路
        assert engine._queue.qsize() == 1
        await engine.stop()

    @pytest.mark.asyncio
    async def test_resume_queue_full_returns_error(self):
        engine = WorkflowEngine(infra=MagicMock(), hitl_notifier=MagicMock(), max_sub_agents=1, max_queue_size=1)
        await engine.start()
        sa, ts = self._paused_task(engine)
        engine._semaphore.acquire_nowait()
        await engine.resume(WorkflowId("t1"))          # 入队占满
        sa2, ts2 = self._paused_task(engine, tid="t2")
        result = await engine.resume(WorkflowId("t2")) # 队满 → 失败
        assert isinstance(result, ResumeError)
        assert result.code == ResumeErrorCode.QUEUE_FULL
        assert ts2.resume_pending is False             # 入队失败清标记
        await engine.stop()
```

- [ ] **Step 2: 运行验证失败**

Expected: FAIL（新行为未实现）

- [ ] **Step 3: 最小实现**

`resume()` PAUSED 分支（spec §4.5）：
```python
mailbox = self._mailboxes.get(task_id)
if mailbox is None:
    return ResumeError(code=ResumeErrorCode.CHECKPOINT_NOT_FOUND, ...)
if ts.resume_pending:
    return mailbox                                  # 幂等短路
if self._semaphore.locked() is False:               # 槽空闲 → 直接恢复
    await self._semaphore.acquire()
    self._active.add(task_id)
    ts.resume_pending = True
    try:
        task = asyncio.create_task(self._run_resume(sub_agent, task_id))
    except Exception:
        self._active.discard(task_id)
        self._semaphore.release()
        ts.resume_pending = False
        raise
    self._task_futures[task_id] = task
else:                                               # 槽满 → 入队（保持 PAUSED）
    ts.resume_pending = True
    try:
        self._queue.put_nowait(QueuedItem(task_id, QueuedItemKind.RESUME))
    except asyncio.QueueFull:
        ts.resume_pending = False
        return ResumeError(code=ResumeErrorCode.QUEUE_FULL, message="执行队列已满，请稍后再试")
    push_mailbox_event(mailbox, WorkflowEventType.QUEUED, {"task_id": str(task_id)})  # 排队可见（§7）
return mailbox
```
`_run_resume` 首步置 RESUMING 时 `ts.resume_pending = False`（统一清理点）。
**`_run_resume` finally 的 PAUSED 分支同步补 release**（Task 5 推迟到此——resume 现在已 acquire）：
```python
# _run_resume finally：
finally:
    if (sub_agent._task_state is not None and
            sub_agent._task_state.status == WorkflowStatus.PAUSED):
        if self._semaphore:
            self._semaphore.release()          # 释放执行槽（resume 已 acquire，对称）
        self._active.discard(task_id)
        return                                 # checkpoint/mailbox/注册表保留
    self._cleanup_task(task_id)
    self._semaphore.release()
    self._active.discard(task_id)
    try:
        await self._cleanup_storage(task_id, sub_agent)
    except Exception:
        logger.exception("WorkflowEngine: task_id=%s resume 后存储清理失败", task_id)
```

- [ ] **Step 4: 运行验证通过**

Run: `conda run -n Elysia python -m pytest Module/workflow_engine/tests/unit/test_workflow_engine.py -q --tb=short`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add Module/workflow_engine/workflow_engine.py Module/workflow_engine/tests/unit/test_workflow_engine.py
git commit -m "feat(workflow): bounded resume via queue with resume_pending idempotency (E2)"
```

---

### Task 7: `stop()`——worker 停 + `_tasks` 扫描终态化

**Files:**
- Modify: `Module/workflow_engine/workflow_engine.py`
- Test: `Module/workflow_engine/tests/unit/test_workflow_engine.py`

**Interfaces:**
- Consumes: Task 3 worker、Task 5 PAUSED 语义。
- Produces: `stop()` 新流程——停 worker → 扫描 `_tasks` 终态化 → 等活跃 → 兜底。

- [ ] **Step 1: 写失败测试**（**先捕获 mailbox 引用再 stop**——`_cleanup_task` 会 pop `_mailboxes`，stop 后读会 KeyError）

```python
class TestStopTerminalizes:
    @pytest.mark.asyncio
    async def test_stop_terminalizes_queued_and_paused(self):
        engine = WorkflowEngine(infra=MagicMock(), hitl_notifier=MagicMock(), max_sub_agents=1)
        await engine.start()
        captured = {}
        for status, tid in [(WorkflowStatus.QUEUED, "q1"), (WorkflowStatus.PAUSED, "p1")]:
            sa = MagicMock()
            ts = MagicMock()
            ts.status = status
            sa._task_state = ts
            wid = WorkflowId(tid)
            engine._tasks[wid] = sa
            engine._mailboxes[wid] = WorkflowMailbox(wid)
            captured[wid] = engine._mailboxes[wid]     # 先捕获引用
        await engine.stop()
        assert WorkflowId("q1") not in engine._tasks
        assert WorkflowId("p1") not in engine._tasks
        assert captured[WorkflowId("q1")].count() >= 1  # 用捕获的引用断言 CANCELLED 事件
        assert captured[WorkflowId("p1")].count() >= 1
```

- [ ] **Step 2: 运行验证失败**

Expected: FAIL（stop 不终态化 QUEUED/PAUSED）

- [ ] **Step 3: 最小实现**

`stop()`（spec §4.6）：`_started=False` → 若 `_queue_worker` 未 done 则 cancel + `await asyncio.gather(return_exceptions=True)` → 扫描 `_tasks`：
```python
for task_id in list(self._tasks.keys()):
    sub_agent = self._tasks.get(task_id)
    if sub_agent is None:
        continue
    ts = sub_agent._task_state
    if (ts is None or ts.status in (WorkflowStatus.QUEUED, WorkflowStatus.PAUSED)
            or task_id not in self._task_futures):
        if ts is not None:
            ts.status = WorkflowStatus.CANCELLED
            ts.updated_at = now_iso()
        mailbox = self._mailboxes.get(task_id)
        if mailbox:
            push_mailbox_event(mailbox, WorkflowEventType.CANCELLED,
                               {"task_id": str(task_id), "reason": "服务停止"})
        self._cleanup_task(task_id)
        try:
            await self._cleanup_storage(task_id, sub_agent)
        except Exception:
            logger.exception("WorkflowEngine.stop: task_id=%s 清理失败", task_id)
```
→ 现有「等待活跃任务（`_task_futures` + timeout）」→ 现有兜底清理。

- [ ] **Step 4: 运行验证通过**

Run: `conda run -n Elysia python -m pytest Module/workflow_engine/tests/unit/test_workflow_engine.py -q --tb=short`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add Module/workflow_engine/workflow_engine.py Module/workflow_engine/tests/unit/test_workflow_engine.py
git commit -m "feat(workflow): stop terminalizes queued and paused tasks (E2)"
```

---

### Task 8: `cancel()`——QUEUED 分支 + PAUSED 去 release

**Files:**
- Modify: `Module/workflow_engine/lifecycle.py`
- Test: `Module/workflow_engine/tests/unit/test_workflow_engine.py`（**不是 test_lifecycle.py——该文件不存在；测试构造 WorkflowEngine 调 engine.cancel**）

**Interfaces:**
- Consumes: Task 5（PAUSED 暂停时已释放 slot）。
- Produces: `cancel()` QUEUED 分支（置 CANCELLED + 事件 + 清理 + 不 release）；PAUSED 分支去掉 `semaphore.release()`。

- [ ] **Step 1: 写失败测试**

```python
class TestCancelBounded:
    @pytest.mark.asyncio
    async def test_cancel_queued_no_release(self):
        engine = WorkflowEngine(infra=MagicMock(), hitl_notifier=MagicMock(), max_sub_agents=1)
        await engine.start()
        sa = MagicMock(); ts = MagicMock(); ts.status = WorkflowStatus.QUEUED
        sa._task_state = ts
        tid = WorkflowId("q1")
        engine._tasks[tid] = sa
        engine._mailboxes[tid] = WorkflowMailbox(tid)
        engine._semaphore.acquire_nowait()
        ok = await engine.cancel(tid)
        assert ok is True
        assert tid not in engine._tasks
        assert engine._semaphore.locked() is True   # 未被 release（我们占的 1 个仍占用）

    @pytest.mark.asyncio
    async def test_cancel_after_pause_no_double_release(self):
        engine = WorkflowEngine(infra=MagicMock(), hitl_notifier=MagicMock(), max_sub_agents=1)
        await engine.start()
        sa = MagicMock(); ts = MagicMock(); ts.status = WorkflowStatus.PAUSED
        sa._task_state = ts
        tid = WorkflowId("p1")
        engine._tasks[tid] = sa
        engine._mailboxes[tid] = WorkflowMailbox(tid)
        await engine.cancel(tid)                    # 不应抛 ValueError
        await engine.stop()
```

- [ ] **Step 2: 运行验证失败**

Expected: FAIL（现 cancel 对 QUEUED 走 orchestrator-None 分支不清理/不推事件；PAUSED 分支二次 release 抛 ValueError）

- [ ] **Step 3: 最小实现**

`lifecycle.cancel()`（spec §4.7）：
- QUEUED 分支（`sub_agent is None` 检查后、现有 PAUSED 分支前）：
```python
if ts.status == WorkflowStatus.QUEUED:
    ts.status = WorkflowStatus.CANCELLED
    ts.updated_at = now_iso()
    mailbox = mailboxes.get(task_id)
    if mailbox:
        push_mailbox_event(mailbox, WorkflowEventType.CANCELLED, {"task_id": str(task_id), "reason": "用户取消"})
    cleanup_task(task_id)
    try:
        await cleanup_storage(task_id, sub_agent)
    except Exception:
        logger.exception("lifecycle: task_id=%s cancel(QUEUED) 存储清理失败", task_id)
    return True
```
- PAUSED 分支**删除** `semaphore.release()`（:153-154）——slot 已在暂停时释放。

- [ ] **Step 4: 运行验证通过**

Run: `conda run -n Elysia python -m pytest Module/workflow_engine/tests/unit/ -q --tb=short`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add Module/workflow_engine/lifecycle.py Module/workflow_engine/tests/unit/
git commit -m "fix(workflow): cancel QUEUED task and drop double release on PAUSED (E2)"
```

---

### Task 9: debug snapshot——slots_used + queued 修正

**Files:**
- Modify: `Module/workflow_engine/query.py`
- Modify: `Module/workflow_engine/workflow_engine.py`（`_get_debug_snapshot`——**真实调用点是这里，debug_api 只调 `self._ts._get_debug_snapshot()`**）
- Test: `Module/workflow_engine/tests/unit/test_workflow_engine.py`

**Interfaces:**
- Consumes: Task 3 `_active`/`_queue`。
- Produces: `get_debug_snapshot(..., active, queue)`；`slots_used` 从 `active`、`queued` 从 `queue.qsize()`（**修 `len(active or tasks)` 空集 falsy 回退 bug**）。

- [ ] **Step 1: 写失败测试**

```python
class TestDebugSnapshotBounded:
    @pytest.mark.asyncio
    async def test_snapshot_slots_from_active_and_queued(self):
        engine = WorkflowEngine(infra=MagicMock(), hitl_notifier=MagicMock(), max_sub_agents=2)
        await engine.start()
        # 槽全空（_active 空）、队列有 1 个 QUEUED 任务
        engine._queue.put_nowait(QueuedItem(WorkflowId("q1"), QueuedItemKind.SUBMIT))
        snap = engine._get_debug_snapshot()
        assert snap["slots_used"] == 0     # 槽空 = 0（不能用 len(active or tasks) 回退到任务数）
        assert snap["queued"] == 1
        await engine.stop()
```

- [ ] **Step 2: 运行验证失败**

Expected: FAIL（现 `slots_used=len(tasks)` 把 QUEUED 计入）

- [ ] **Step 3: 最小实现**

`query.py:get_debug_snapshot` 签名加 `active: set[WorkflowId] | None = None, queue: asyncio.Queue | None = None`：
```python
return {
    "tasks": task_list,
    "slots_total": max_sub_agents,
    "slots_used": len(active) if active is not None else len(tasks),   # 显式判 None，不用 `active or tasks`（空集 falsy 回退 bug）
    "queued": queue.qsize() if queue is not None else 0,
}
```
`workflow_engine.py:_get_debug_snapshot` 调用处传 `self._active, self._queue`。

- [ ] **Step 4: 运行验证通过**

Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add Module/workflow_engine/query.py Module/workflow_engine/workflow_engine.py Module/workflow_engine/tests/unit/
git commit -m "fix(workflow): debug snapshot slots from active set with queued count (E2)"
```

---

### Task 10: service 级验证 + worker 泄漏清理 + 门禁 + 文档

**Files:**
- Modify: `Module/workflow_engine/tests/unit/test_workflow_engine.py`（存量 `_build_task_system` 测试补 stop teardown）
- Modify: `todo.md`、`docs/`

**Interfaces:**
- Consumes: Task 1-9。

- [ ] **Step 1: 写 service 测试（真实代码，非骨架）**

```python
@pytest.mark.service
@pytest.mark.asyncio
async def test_submit_never_blocks_turn_when_slot_busy():
    """完整 WorkflowEngine：首任务 PAUSED 占槽 → 第二次 submit 立即返回 mailbox（不阻塞聊天轮）。"""
    engine = WorkflowEngine(infra=MagicMock(), hitl_notifier=MagicMock(), max_sub_agents=1)
    await engine.start()
    # 占满唯一槽 + 一个 PAUSED 任务在 _tasks
    engine._semaphore.acquire_nowait()
    engine._active.add(WorkflowId("busy"))
    sa = MagicMock(); ts = MagicMock(); ts.status = WorkflowStatus.PAUSED
    sa._task_state = ts
    engine._tasks[WorkflowId("paused")] = sa
    engine._mailboxes[WorkflowId("paused")] = WorkflowMailbox(WorkflowId("paused"))
    ctx = DelegationContext(intent="second", session_id=SessionId("s1"))
    started = time.monotonic()
    result = await asyncio.wait_for(engine.submit(ctx), timeout=1.0)
    assert isinstance(result, WorkflowMailbox)
    assert time.monotonic() - started < 1.0
    await engine.stop()
```

- [ ] **Step 2: 存量测试 worker 泄漏处理**

Task 3 起 `start()` 创建常驻 worker；`_build_task_system()` 测试只 start 不 stop。**实测该泄漏只产生 "Task was destroyed but it is pending!" WARNING，不是门禁错误**（pytest-asyncio strict 下 `@pytest.fixture(autouse=True)` 的 async fixture 会报错且不持 engine 引用、无法 stop——**勿用该方案**）。

正确处理：
1. 跑全量 unit 确认泄漏仅为 warning（非 error）——记录接受或清理。
2. 若需消除告警：为使用 `_build_task_system()` 的测试补 `await ts.stop()`（真实修法，~30 处；或改 `_build_task_system` 注册 teardown）。此为可选清理，不阻塞门禁（warning 非 error）。

- [ ] **Step 3: 文档更新**

- `docs/architecture.md` / workflow_engine 文档：状态机补 QUEUED、submit 非阻塞语义、PAUSED 释放 slot。
- `todo.md`：E2 标记完成 + 记录「方案 A 拍板」（2026-08-11）。

- [ ] **Step 4: 全量门禁**

```bash
make lint && make pyright && make check-types
conda run -n Elysia python -m pytest -m "unit or service" -q -x --tb=short
conda run -n Elysia python -m pytest -m "not slow and not external" -q --tb=short
```
Expected: 全绿。若 `coroutine .*was never awaited` 报新警告 → 按 E4 §6 修。

- [ ] **Step 5: 提交**

```bash
git add Module/workflow_engine/tests/ todo.md docs/
git commit -m "test(workflow): service-level nonblocking submit + E2 docs (E2)"
```

---

## 自审记录

- **Spec 覆盖**：Task 1→§3/§7、Task 2→§4.1、Task 3→§4.2/§4.3、Task 4→§4.4、Task 5→§5、Task 6→§4.5、Task 7→§4.6、Task 8→§4.7、Task 9→§7、Task 10→§8/§9。
- **类型一致**：`QueuedItem`/`QueuedItemKind`/`resume_pending`/`_contexts`/`QUEUE_FULL` 全 plan 一致；`DelegationContext` 用 `intent`/`session_id`（无 `constraints` 字段）。
- **顺序依赖**：Task 2 先提取方法（Task 3 drain 依赖）→ Task 3 建 worker（Task 4 submit 依赖）→ 后续。Task 2 的 `start()` 尚不建 worker（Task 3 才建），Task 2 不触发 `_drain_queue` 缺失。
- **无占位符**：每 Task 含可运行测试代码 + 实现代码；service 测试非骨架。
