# WorkflowEngine cancel/pause 终态单调性实施计划（E3）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现强取消——`cancel()` 同步置 `status=CANCELLED`（权威终态）+ `owned.cancel()` 立即中断 in-flight LLM/planner；工具调用 shield 保护（副作用不打断）；终态不可逆、terminal event 唯一；修 M19（resume→pause 不销毁）。

**Architecture:** 取消信号 = 任务状态。`cancel()` 单同步块复查终态后置 CANCELLED → owned.cancel() 打断 in-flight → 各层 CancelledError 传播链统一收尾（finally 唯一 release 点）。executor 加 `_check_cancelled` 检查点 + `_call_tool` shield + `AgentInfra.orphan_tools` 共享容器。确定性测试用 E4 的 fake-LLM。

**Tech Stack:** Python 3.12 + asyncio（shield/task.cancel/BoundedSemaphore）+ `Module/workflow_engine`（engine/lifecycle/executor/orchestrator/sub_agent/planning/mailbox）。

## Global Constraints

- 生产代码函数参数必须类型注解；禁相对导入；注释中文。
- 每 Task TDD；验收门禁全绿。
- **依赖 E4**：确定性测试用 `Module/workflow_engine/tests/helpers/fake_llm.py`（E4 spec §3）——执行顺序 E2 → E4（基建）→ E3。
- **与 E2 共享文件**：`workflow_engine.py`/`lifecycle.py` 是 E2 已改动的——E3 在 E2 之后执行。

## 文件结构

| 文件 | 职责 |
|---|---|
| `Module/workflow_engine/lifecycle.py` | `cancel()` 同步置终态 + owned.cancel + exec_state 快速路径（None 守卫） |
| `Module/workflow_engine/workflow_engine.py` | `_run_and_cleanup`/`_run_resume` CancelledError 分支 + finally 唯一 release + stop() 孤儿工具收尾 |
| `Module/workflow_engine/core/executor.py` | `_check_cancelled` 检查点 + `_call_tool` shield + `AgentInfra.orphan_tools` 登记 + 签名穿透 |
| `Module/workflow_contract/infra.py` | 加运行时字段 `orphan_tools` + docstring 更新 |
| `Module/workflow_engine/core/execution.py` | `execute_nodes` 透传 ts |
| `Module/workflow_engine/core/orchestrator.py` | `_execute_nodes` 透传 ts；CancelledError 兜底；`_init_exec` 终态检查 |
| `Module/workflow_engine/core/sub_agent/planning.py` | 写 RUNNING 前终态检查 |
| `Module/workflow_engine/core/finalize.py` | 终态一致性不变量（COMPLETED 写在首个 await 前） |
| `Module/workflow_engine/mailbox.py` | `_terminal_pushed`/`_terminal_status` 守卫 |
| 各对应测试文件 | 见 Task |

---

### Task 1: `_check_cancelled` helper + mailbox 终态守卫

**Files:**
- Modify: `Module/workflow_engine/core/executor.py` 定义 `_check_cancelled`（定位确定，无「或共享位置」）
- Modify: `Module/workflow_engine/mailbox.py`
- Test: `Module/workflow_engine/tests/unit/test_executor.py`、`test_mailbox.py`

**Interfaces:**
- Produces: `_check_cancelled(ts) -> None`（`ts.status == CANCELLED` → raise CancelledError）；`WorkflowMailbox` 加 `_terminal_pushed`/`_terminal_status`；`push_mailbox_event` 对 `_TERMINAL_EVENTS` 幂等守卫。

- [ ] **Step 1: 写失败测试**

```python
class TestCheckCancelled:
    def test_raises_when_cancelled(self):
        ts = WorkflowState(task_id=WorkflowId("t1"), status=WorkflowStatus.CANCELLED, tools=[], session_id=SessionId("s1"))
        with pytest.raises(asyncio.CancelledError):
            _check_cancelled(ts)

    def test_passes_when_not_cancelled(self):
        ts = WorkflowState(task_id=WorkflowId("t1"), status=WorkflowStatus.RUNNING, tools=[], session_id=SessionId("s1"))
        _check_cancelled(ts)   # 不抛

class TestMailboxTerminalGuard:
    def test_terminal_event_once(self):
        mb = WorkflowMailbox(WorkflowId("t1"))
        push_mailbox_event(mb, WorkflowEventType.COMPLETED, {})
        push_mailbox_event(mb, WorkflowEventType.CANCELLED, {})
        assert mb.count() == 1          # 第二个终态被守卫丢弃
```

- [ ] **Step 2: 运行验证失败**

Run: `conda run -n Elysia python -m pytest Module/workflow_engine/tests/unit/test_mailbox.py Module/workflow_engine/tests/unit/test_executor.py -q -k "CheckCancelled or TerminalGuard" -x --tb=short`
Expected: FAIL

- [ ] **Step 3: 最小实现**

**`_check_cancelled` 定位于 `core/executor.py`**（executor 是叶子模块、不引 sub_agent——planning.py 从它 import 无环）。**executor.py 现无 `asyncio`/`WorkflowStatus` 运行时 import**（WorkflowState 仅 TYPE_CHECKING），须补：
```python
import asyncio
from Module.workflow_contract import WorkflowStatus
from Module.workflow_engine.schema.internal import WorkflowState   # 或保留 TYPE_CHECKING，按现有风格

def _check_cancelled(ts: "WorkflowState") -> None:
    """取消检查——cancel() 已同步置 CANCELLED，await 边界一查即中。"""
    if ts.status == WorkflowStatus.CANCELLED:
        raise asyncio.CancelledError()
```

`mailbox.py`：
```python
_TERMINAL_EVENTS = {WorkflowEventType.COMPLETED, WorkflowEventType.FAILED, WorkflowEventType.CANCELLED}

# WorkflowMailbox.__init__ 增加：
self._terminal_pushed: bool = False
self._terminal_status: WorkflowStatus | None = None

# push_mailbox_event 内（type 判别后）：
if event.type in _TERMINAL_EVENTS:
    if mailbox._terminal_pushed:
        return
    mailbox._terminal_pushed = True
    mailbox._terminal_status = <对应 WorkflowStatus>   # 记录首个终态（spec §3.5；供一致性断言/调试）
```

- [ ] **Step 4: 运行验证通过**

Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add Module/workflow_engine/core/executor.py Module/workflow_engine/mailbox.py Module/workflow_engine/tests/unit/
git commit -m "feat(workflow): cancellation check and terminal-event guard (E3)"
```

---

### Task 2: `lifecycle.cancel()`——同步置终态 + owned.cancel

**Files:**
- Modify: `Module/workflow_engine/lifecycle.py`
- Modify: `Module/workflow_engine/workflow_engine.py`（`cancel()` 传 `task_futures`）
- Test: `Module/workflow_engine/tests/unit/test_workflow_engine.py`

**Interfaces:**
- Consumes: E2 §4.7（QUEUED/PAUSED 分支）、`task_futures`。
- Produces: `lifecycle.cancel(..., task_futures, ...)` RUNNING/PLANNING 分支——同步置 CANCELLED + `owned.cancel()` + exec_state 快速路径。

- [ ] **Step 1: 写失败测试**

```python
class TestCancelStrong:
    @pytest.mark.asyncio
    async def test_cancel_sets_terminal_and_cancels_owned(self):
        engine = WorkflowEngine(infra=MagicMock(), hitl_notifier=MagicMock(), max_sub_agents=1)
        await engine.start()
        sa = MagicMock()
        ts = MagicMock()
        ts.status = WorkflowStatus.PLANNING          # 未终态
        ts.is_terminal.return_value = False
        sa._task_state = ts
        sa._orchestrator = None                       # PLANNING，orchestrator 未入 run
        tid = WorkflowId("t1")
        engine._tasks[tid] = sa
        engine._mailboxes[tid] = WorkflowMailbox(tid)
        owned = asyncio.create_task(asyncio.sleep(10))   # in-flight
        engine._task_futures[tid] = owned
        ok = await engine.cancel(tid)
        assert ok is True
        assert ts.status == WorkflowStatus.CANCELLED      # 同步置终态（不等 orchestrator 回写）
        await asyncio.sleep(0)                            # 让出事件循环——CancelledError 下一迭代才投递
        assert owned.cancelled() is True                  # owned.cancel() 已打断 in-flight
        await engine.stop()
```

> **现有冲突测试**：`test_cancel_running_sets_cancel_requested`（test_workflow_engine.py:744-757）断言 `status == RUNNING`（旧「只设标志」语义）——Task 2 实现后必红，**改断言为 `status == CANCELLED`**（或删除，语义被本 Task 取代）。plan Step 4 全文件跑通前须处理该测试。

- [ ] **Step 2: 运行验证失败**

Expected: FAIL（现 cancel 只设 cancel_requested，status 不变、owned 不取消）

- [ ] **Step 3: 最小实现**

`lifecycle.cancel` 签名加 `task_futures: dict[WorkflowId, asyncio.Task]`；RUNNING/PLANNING 分支（spec §3.2）：
```python
if ts.is_terminal():
    return True
if ts.status == WorkflowStatus.COMPLETED:   # 终态一致性（§3.5）
    return True
ts.status = WorkflowStatus.CANCELLED
ts.updated_at = now_iso()
if sub_agent._orchestrator is not None:
    exec_state = sub_agent._orchestrator._exec_state
    if exec_state is not None:
        exec_state.cancel_requested = True
owned = task_futures.get(task_id)
if owned is not None and not owned.done():
    owned.cancel()
return True
```
`workflow_engine.cancel()` 调用处传 `self._task_futures`。

- [ ] **Step 4: 运行验证通过**

Expected: PASS（现有 cancel 测试更新）

- [ ] **Step 5: 提交**

```bash
git add Module/workflow_engine/lifecycle.py Module/workflow_engine/workflow_engine.py Module/workflow_engine/tests/unit/
git commit -m "feat(workflow): strong cancel — terminal status + owned task cancel (E3)"
```

---

### Task 3: executor 签名穿透 + `_check_cancelled` 检查点 + shield

**Files:**
- Modify: `Module/workflow_engine/core/executor.py`
- Modify: `Module/workflow_engine/core/execution.py`
- Modify: `Module/workflow_engine/core/orchestrator.py`
- Test: `Module/workflow_engine/tests/unit/test_executor.py`、`test_orchestrator.py`

**Interfaces:**
- Consumes: Task 1 `_check_cancelled`。
- Produces: `executor.execute(node, task_id, tools, session_id, ts)`；`execution.execute_nodes(..., ts)`；`orchestrator._execute_nodes(..., ts)`；`_call_tool` shield + **`AgentInfra.orphan_tools`（本 Task 加字段，Task 6 的 stop() 采集依赖它）**。

- [ ] **Step 1: 写失败测试**

**mock 方案（复用 E4 FakeLLM + 真实工具 registry mock）**：
```python
class TestExecutorCancellation:
    @pytest.mark.asyncio
    async def test_tool_not_called_when_cancelled_before(self):
        # fake LLM 返回 tool 匹配；工具调用前把 ts.status 置 CANCELLED
        fake = FakeLLM()                              # E4 helpers（执行序 E2→E4→E3，Task 3 前已存在）
        infra = AgentInfra(llm=fake, milvus=None, http=None,
                           tool_registry=MagicMock(), constraints=ConstraintConfig())
        executor = Executor(infra=infra, artifact_store=MagicMock(), hitl_notifier=MagicMock())
        ts = WorkflowState(task_id=WorkflowId("t1"), status=WorkflowStatus.RUNNING,
                           tools=[], session_id=SessionId("s1"))
        node = DAGNode(node_id=NodeId("n1"), description="do something",   # 字段名：node_id/execution_mode（非 id/mode）；input_bindings 是 list
                       input_bindings=[], output_as=ArtifactId("a1"), execution_mode=ExecutionMode.TOOL_CALL)
        def handler(msgs):
            ts.status = WorkflowStatus.CANCELLED       # 工具调用前取消（在 LLM 匹配返回后、②检查点前）
            return {"type": "tool", "name": "get_time", "arguments": {}}
        fake.handler = handler
        with pytest.raises(asyncio.CancelledError):
            await executor.execute(node, WorkflowId("t1"), [tool], SessionId("s1"), ts)
        infra.tool_registry.execute.assert_not_called()   # 工具未调（②检查点拦截）

    @pytest.mark.asyncio
    async def test_tool_shielded_completes_on_cancel(self):
        # 工具调用中取消外层任务 → shield 让工具完成、CancelledError 上抛
        fake = FakeLLM()
        fake.handler = lambda msgs: {"type": "tool", "name": "get_time", "arguments": {}}
        infra = AgentInfra(llm=fake, milvus=None, http=None,
                           tool_registry=MagicMock(), constraints=ConstraintConfig())
        gate = asyncio.Event()
        async def slow_tool(tool_name, **kw):        # _call_tool 以位置参数 tool_name + kwargs 调 execute（executor.py:437）
            await gate.wait(); return "done"
        infra.tool_registry.execute.side_effect = slow_tool
        executor = Executor(infra=infra, artifact_store=MagicMock(), hitl_notifier=MagicMock())
        ts = WorkflowState(task_id=WorkflowId("t1"), status=WorkflowStatus.RUNNING, tools=[], session_id=SessionId("s1"))
        node = DAGNode(node_id=NodeId("n1"), description="do", input_bindings=[], output_as=ArtifactId("a1"), execution_mode=ExecutionMode.TOOL_CALL)
        outer = asyncio.create_task(executor.execute(node, WorkflowId("t1"), [tool], SessionId("s1"), ts))
        await asyncio.sleep(0.05)                       # 工具已开始（shield 内）
        ts.status = WorkflowStatus.CANCELLED
        outer.cancel()                                  # 取消外层任务
        with pytest.raises(asyncio.CancelledError):
            await outer
        gate.set()                                      # 放行——工具完成（shield），结果弃用
        await asyncio.sleep(0.05)
        assert infra.tool_registry.execute.await_count == 1
```

- [ ] **Step 2: 运行验证失败**

Expected: FAIL（executor.execute 无 ts 参数、无检查点）

- [ ] **Step 3: 最小实现**

**本 Task 同时给 `AgentInfra`（`Module/workflow_contract/infra.py`）加字段 + 更新 docstring**（spec §3.3「孤儿挂共享 infra」）：
```python
# infra.py 顶部须补 import asyncio（或 TYPE_CHECKING 内 import）——字符串注解 `set["asyncio.Task"]` 被 pyright 解析名字
import asyncio

orphan_tools: set["asyncio.Task"] = field(default_factory=set)   # engine 内部运行时（executor 登记、stop 采集）
# 同步更新 infra.py:28-30 docstring：去除「不含任务状态/只读引用」，注明 engine 运行时用途
```

签名穿透：`execution.execute_nodes(ready_nodes, exec_state, task_id, tools, session_id, ts)`；`orchestrator._execute_nodes(...)` 透传 `task_state`。`executor.execute` 加 `ts` 参数 + 四个 `_check_cancelled(ts)` 检查点（spec §3.3）。工具调用改 `_call_tool` shield + **`self._infra.orphan_tools.add(tool_task)` + done 回调 discard**（spec §3.3，统一 infra 容器——**不是 per-Executor `_orphan_tools`**）。直出文本分支走 `_call_llm_direct`（同样不 shield）。

- [ ] **Step 4: 运行验证通过**

Expected: PASS（同步更新 executor/orchestrator 全部测试调用点）

- [ ] **Step 5: 提交**

```bash
git add Module/workflow_engine/core/ Module/workflow_engine/tests/unit/test_executor.py Module/workflow_engine/tests/unit/test_orchestrator.py
git commit -m "feat(workflow): executor cancellation checkpoints and shielded tool calls (E3)"
```

---

### Task 4: 终态不可逆——planning/_init_exec 写点检查 + finalize 顺序不变量

**Files:**
- Modify: `Module/workflow_engine/core/sub_agent/planning.py`
- Modify: `Module/workflow_engine/core/orchestrator.py`（`_init_exec`）
- Modify: `Module/workflow_engine/core/finalize.py`（确认顺序不变量）
- Test: 对应 unit 测试

**Interfaces:**
- Consumes: `_check_cancelled`。
- Produces: `planning.py` 写 RUNNING 前 `_check_cancelled`/is_terminal 检查；`_init_exec` RESUMING→RUNNING 前检查。

- [ ] **Step 1: 写失败测试**

```python
class TestTerminalMonotonic:
    @pytest.mark.asyncio
    async def test_planning_guard_blocks_when_cancelled(self):
        """真正驱动 planning 的 RUNNING 写点：planner.plan 执行期间 cancel 落地（ts.status=CANCELLED），
        plan_and_validate 到写 RUNNING 处 _check_cancelled 抛 CancelledError、status 不被覆写。"""
        from Module.workflow_engine.core.sub_agent.planning import plan_and_validate
        ts = WorkflowState(task_id=WorkflowId("t1"), status=WorkflowStatus.PENDING,
                           tools=[], session_id=SessionId("s1"))
        planner = MagicMock()
        async def fake_plan(*a, **kw):
            ts.status = WorkflowStatus.CANCELLED      # 规划期间 cancel 落地
            return MagicMock()                         # 返回 mock plan（validator 放行）
        planner.plan = fake_plan
        validator = MagicMock()
        validator.validate = MagicMock(return_value=(True, MagicMock()))   # 同步方法（plan_validator.py:55 def validate）——不能用 AsyncMock，否则解包 coroutine TypeError
        with pytest.raises(asyncio.CancelledError):
            await plan_and_validate(
                planner, validator, MagicMock(), ts, WorkflowMailbox(WorkflowId("t1")),
                ConstraintConfig(),
                push_mailbox_fn=AsyncMock(), abort_error_code_fn=lambda: "ABORT",
            )
        assert ts.status == WorkflowStatus.CANCELLED   # 未被覆写为 RUNNING
```
（mock 返回形状按 `plan_and_validate` 实际解包调整——validator 返回 `(ok, graph)`，planner 返回 plan 对象；以真实代码为准。）

> **`_init_exec` 守卫不做单测**：RESUMING + is_terminal（CANCELLED）经公开 API 不可达（cancel() 会把 RESUMING 覆写为 CANCELLED，`_init_exec` 的 RUNNING 写有条件仅 RESUMING 命中）——该守卫为防御性代码，由 Task 7 的 `_init_exec` 逆转用例（确定性）覆盖，不单独单测。

- [ ] **Step 2: 运行验证失败**

Expected: `test_planning_guard_blocks_when_cancelled` FAIL（现 planning 无条件写 RUNNING，不抛 CancelledError）。`_init_exec` 守卫不做单测（防御性，Task 7 覆盖）——见 Step 1 注。

- [ ] **Step 3: 最小实现**

`planning.py` 写 RUNNING 处：前插 `_check_cancelled(task_state)`（CANCELLED → 抛，终止）。`orchestrator._init_exec` RESUMING→RUNNING 写点：前插 `if task_state.is_terminal(): raise asyncio.CancelledError()`。`finalize.py` 确认 COMPLETED 写（:159）在首个 await（:161 save_checkpoint）之前——若顺序不符调整，并加注释注明不变量。

- [ ] **Step 4: 运行验证通过**

Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add Module/workflow_engine/core/sub_agent/planning.py Module/workflow_engine/core/orchestrator.py Module/workflow_engine/core/finalize.py Module/workflow_engine/tests/unit/
git commit -m "fix(workflow): terminal states irreversible at all write points (E3)"
```

---

### Task 5: `_run_and_cleanup`/`_run_resume`——CancelledError 分支 + finally 唯一 release

**Files:**
- Modify: `Module/workflow_engine/workflow_engine.py`
- Test: `Module/workflow_engine/tests/unit/test_workflow_engine.py`

**Interfaces:**
- Consumes: Task 2（status 已 CANCELLED）。
- Produces: CancelledError → 置状态（幂等）+ 推 CANCELLED 事件 + raise；finally 唯一 release（PAUSED 分支 / 终态分支）；删现 FAILED「任务被取消」分支。

- [ ] **Step 1: 写失败测试**

```python
class TestRunCleanupCancelled:
    @pytest.mark.asyncio
    async def test_cancelled_pushes_cancelled_event_and_releases_once(self):
        engine = WorkflowEngine(infra=MagicMock(), hitl_notifier=MagicMock(), max_sub_agents=1)
        await engine.start()
        sa = MagicMock()
        sa._task_state = MagicMock(); sa._task_state.status = WorkflowStatus.CANCELLED
        sa.run = AsyncMock(side_effect=asyncio.CancelledError)
        tid = WorkflowId("t1")
        engine._tasks[tid] = sa
        engine._contexts[tid] = DelegationContext(intent="t", session_id=SessionId("s1"))  # E2 引入，须设置
        mailbox = engine._mailboxes[tid] = WorkflowMailbox(tid)   # 先捕获引用——finally 的 _cleanup_task 会 pop
        await engine._semaphore.acquire()
        engine._active.add(tid)
        with pytest.raises(asyncio.CancelledError):
            await engine._run_and_cleanup(sa, tid)
        assert engine._semaphore.locked() is False      # 恰好释放一次（无 ValueError）
        assert mailbox.count() == 1                      # 唯一 CANCELLED 事件（用捕获的引用）
        assert tid not in engine._tasks
        await engine.stop()
```

- [ ] **Step 2: 运行验证失败**

Expected: FAIL（现 push FAILED；release 结构不符）

- [ ] **Step 3: 最小实现**

`_run_and_cleanup`/`_run_resume` 重构（spec §3.4）——**`mailbox` 在 try 之前绑定**（否则 `except Exception` 分支 UnboundLocalError）：
```python
async def _run_and_cleanup(self, sub_agent, task_id):
    mailbox = self._mailboxes.get(task_id)          # 提前绑定（两个 except 分支都要用）
    try:
        await sub_agent.run(...)   # 注意：改为接收已取出的 sub_agent，run 调用点调整
    except asyncio.CancelledError:
        ts = sub_agent._task_state
        if ts is not None:
            ts.status = WorkflowStatus.CANCELLED
            ts.updated_at = now_iso()
        if mailbox:
            push_mailbox_event(mailbox, WorkflowEventType.CANCELLED,
                               {"task_id": str(task_id), "reason": "任务被取消"})
        raise
    except Exception:
        logger.exception(...)
        if mailbox:
            push_mailbox_event(mailbox, WorkflowEventType.FAILED, ...)
    finally:
        ts = sub_agent._task_state
        if ts is not None and ts.status == WorkflowStatus.PAUSED:
            if self._semaphore: self._semaphore.release()
            self._active.discard(task_id)
            return
        self._cleanup_task(task_id)
        if self._semaphore: self._semaphore.release()
        self._active.discard(task_id)
        try:
            await self._cleanup_storage(task_id, sub_agent)
        except Exception:
            logger.exception("WorkflowEngine: task_id=%s 存储清理失败", task_id)
```
`_run_resume` 对称。

- [ ] **Step 4: 运行验证通过**

Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add Module/workflow_engine/workflow_engine.py Module/workflow_engine/tests/unit/test_workflow_engine.py
git commit -m "fix(workflow): cancel path pushes CANCELLED with single slot release (E3)"
```

---

### Task 6: M19 验证 + stop() 孤儿工具收尾

**Files:**
- Modify: `Module/workflow_engine/workflow_engine.py`
- Modify: `Module/workflow_contract/infra.py`（若 Task 3 未做 docstring 更新则在此补——字段本身 Task 3 已加）
- Test: `Module/workflow_engine/tests/unit/test_workflow_engine.py`

**Interfaces:**
- Consumes: **`AgentInfra.orphan_tools`（Task 3 已加的共享容器，非 per-Executor `_orphan_tools`）**。
- Produces: `stop()` 对 `self._infra.orphan_tools` 未完成任务等待/取消；M19 循环测试。

- [ ] **Step 1: 写失败测试**

```python
class TestM19AndOrphans:
    @pytest.mark.asyncio
    async def test_resume_pause_resume_cycle(self):
        # resume → pause → resume 多次循环，task/mailbox 不销毁、可消费事件（E2 已实现，验证）

    @pytest.mark.asyncio
    async def test_stop_awaits_orphan_tools(self):
        # engine._infra.orphan_tools 含一个挂起工具任务 → stop() 等待/取消它（不挂起、不泄漏）
        infra = AgentInfra(llm=MagicMock(), milvus=None, http=None,
                           tool_registry=MagicMock(), constraints=ConstraintConfig())
        infra.orphan_tools = set()                    # 赋真 set（Task 3 字段；MagicMock 迭代会 TypeError）
        engine = WorkflowEngine(infra=infra, hitl_notifier=MagicMock(), max_sub_agents=1)
        await engine.start()
        held = asyncio.create_task(asyncio.Event().wait())   # 挂起工具任务
        infra.orphan_tools.add(held)
        await engine.stop()                           # stop 采集 → 等 3s → 超时 cancel
        assert held.cancelled() is True               # cancel 投递需下一事件循环迭代——若断言失败，在 stop() 实现里 cancel 后再 await 一轮（镜像现有 stop 对 _task_futures 的二次等待）
```
（注：该测试固定等待 stop() 的 3s 孤儿超时——预期耗时，非缺陷。）

- [ ] **Step 2: 运行验证失败**

Expected: FAIL/不覆盖（stop 不采集孤儿）

- [ ] **Step 3: 最小实现**

executor `_call_tool` 创建工具任务时 `self._infra.orphan_tools.add(tool_task)` + done 回调 `discard`（**Task 3 已接线**——本 Task 只补 `stop()` 采集）。`stop()` 在任务等待/取消**之后**从 `self._infra.orphan_tools` 采集未完成任务：`await asyncio.wait(orphans, timeout=3)`，超时 `cancel()`。M19 的 `_run_resume` finally PAUSED early-return 已具备（E2），补测试。

- [ ] **Step 4: 运行验证通过**

Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add Module/workflow_engine/workflow_engine.py Module/workflow_contract/infra.py Module/workflow_engine/tests/unit/test_workflow_engine.py
git commit -m "fix(workflow): M19 resume-pause cycle and stop drains orphan tools (E3)"
```

---

### Task 7: 确定性测试套件（fake-LLM，E4 基建）

**Files:**
- Modify: `Module/workflow_engine/tests/`（fake-LLM 驱动）
- 依赖：E4 的 `Module/workflow_engine/tests/helpers/fake_llm.py`

**Interfaces:**
- Consumes: E4 fake-LLM + `with_deadline`。
- Produces: spec §7 测试表全用例。

- [ ] **Step 1: E4 基建检查点**（**Task 7 前必须先确认 E4 产物就位**——否则 fake_llm/with_deadline 不存在，测试收集即挂）

```bash
ls Module/workflow_engine/tests/helpers/fake_llm.py   # E4 plan Task 1 产物
conda run -n Elysia python -c "from Module.workflow_engine.tests.helpers.fake_llm import FakeLLM, with_deadline"
```
Expected: 文件存在 + import 无错（E4 已执行）。若缺失 → 阻塞等待 E4 落地。

- [ ] **Step 2: 写确定性验证套件**（**本 Task 定位为「验证套件」而非严格 TDD 红-绿**——Task 1-6 已实现行为，多数用例写时即 PASS；仅 stop-orphan、`_init_exec` 逆转等新守卫相关用例可能红）

按 spec §7 表实现：慢 planner cancel（gate 挂起 → cancel → calls 保持 0、CANCELLED、不回 RUNNING）、tool 前 cancel（工具未调）、tool 中 cancel（shield 完成、结果弃用）、cancel∥pause 乱序、cancel∥complete、重复 cancel 幂等 + slot 无泄漏、stop、`_init_exec` 逆转。全部 `with_deadline` 包裹。
> **AgentInfra 构造**：`AgentInfra(llm=fake, milvus=None, http=None, tool_registry=MagicMock(), constraints=ConstraintConfig())`（必填字段模式，见 E4 plan Task 5）。

- [ ] **Step 3: 运行验证失败**

Expected: 多数 PASS（已实现）；若某用例红 → 按 Task 1-6 实现补齐缺口。

- [ ] **Step 4: 运行验证通过**

Run: `conda run -n Elysia python -m pytest Module/workflow_engine/tests/unit/ -q --tb=short`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add Module/workflow_engine/tests/
git commit -m "test(workflow): deterministic cancellation suite with fake LLM (E3)"
```

---

### Task 8: 门禁 + 文档

**Files:**
- Modify: `todo.md`（E3 勾选 + 决策记录）
- Modify: `docs/`（workflow_engine 状态机/架构文档）

- [ ] **Step 1: 全量门禁**

```bash
make lint && make pyright && make check-types
conda run -n Elysia python -m pytest -m "unit or service" -q -x --tb=short
conda run -n Elysia python -m pytest -m "not slow and not external" -q --tb=short
```
Expected: 全绿。

- [ ] **Step 2: 文档更新**

`todo.md` E3 完成 + 决策（强取消、status 为 token、finally 唯一 release、orphan 容器）；`docs/architecture.md`/相关文档状态机与取消语义。

- [ ] **Step 3: 提交**

```bash
git add todo.md docs/
git commit -m "docs(workflow): E3 completion — strong cancellation with terminal monotonicity"
```

---

## 自审记录

- **Spec 覆盖**：Task 1→§3.1/§3.5、Task 2→§3.2、Task 3→§3.3、Task 4→§4、Task 5→§3.4、Task 6→§5/stop 孤儿、Task 7→§7、Task 8→§8。
- **类型一致**：`_check_cancelled`/`AgentInfra.orphan_tools`/`task_futures` 命名一致；executor 签名穿透三文件同步。
- **无占位符**：每 Task 含测试 + 实现代码；fake-LLM 依赖明确指向 E4。
