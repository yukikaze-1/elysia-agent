# WorkflowEngine submit 非阻塞有界排队设计（E2）

> **状态**：已拍板（2026-08-11）
> **决策**：方案 A —— 立即返回 mailbox + 后台有界 QUEUED
> **对应**：P2-07（`todo.md` E2）、`docs/superpowers/plans/2026-08-10-full-project-remediation-plan.md` Task E2
> **相关 spec**：E3 cancel/pause 终态单调性（`2026-08-11-workflow-cancel-pause-terminal-design.md`，另文）

---

## 1. 问题

`WorkflowEngine.submit()` 在方法体内**同步**执行 `await self._semaphore.acquire()`（`Module/workflow_engine/workflow_engine.py:220`），然后才注册任务并 spawn `_run_and_cleanup`。

后果（P2-07）：当全部执行槽被占——尤其是 **PAUSED 任务占住 slot 不放**（当前 `_run_and_cleanup` 的 finally 对 PAUSED early-return，**不释放 semaphore**）——`max_sub_agents=1` 时首任务暂停会让第二次 `submit()` 无限等待。

调用链：`core/cognitive/tools.py:_handle_submit_task` 直接 `await task_system.submit(...)` → 阻塞当前聊天轮（用户消息处理卡死）。

## 2. 决策

**方案 A：立即返回 + 后台有界 QUEUED**（2026-08-11 拍板，计划推荐方案）。

- `submit()` 只做参数校验 + 任务注册，**立即返回 mailbox**；semaphore acquire 移到后台 runner。
- 新增显式 **QUEUED** 状态/事件 + 有界队列；队列满快速返回稳定错误（`QUEUE_FULL`），**不无限 await**。
- **PAUSED 任务释放执行 slot**；resume 时重新 acquire（槽满则排队），保持公平并防饥饿。
- `stop`/`cancel` 能移除排队任务并推唯一终态；shutdown 不留下等待 semaphore 的 task。

否决方案 B（快速 `BUSY` 拒绝）：把重试负担推给上层，多后台任务连发体验差；A 的队列上限已兜住无限堆积。

## 3. 状态机扩展

`WorkflowStatus`（`Module/workflow_contract/schema.py:64`）11 → **12 态**，入口加 QUEUED：

```
QUEUED → PENDING → PLANNING → RUNNING
   │
   └──────→ CANCELLED（排队中被取消）
```

- **QUEUED**：已注册（`_tasks`/`_mailboxes` 就位）、等待首个执行槽；**不消耗 semaphore**。
- **QUEUED 状态存放**：`WorkflowState` 在 `submit()` 时**预先创建**并注入 `sub_agent._task_state`（`status=QUEUED`）；`SubAgent.run()` 检测到 `_task_state` 已存在则**复用**（仅把 `QUEUED` 转 `PENDING` 后走原流程），不再新建。
  - 依据：当前 `_task_state` 在 `run()` 内创建（`sub_agent.py:144-152`），`run()` 前为 `None`——若不预创建，`get_task_status` 对排队任务返回 PENDING（`query.py:64`）、`list_active_tasks` 直接跳过（`query.py:120-121`）、`debug_api` 对 `ts.task_id` 解引用崩溃（`debug_api.py:125`），QUEUED 对外不可见。
  - `WorkflowState` 的 `tools`/`session_id` 在 submit 时从 `context` 可得（`context.tools`/`context.session_id`），预创建所需字段齐全。
- 从 QUEUED 可直接 CANCELLED（排队中被取消，走统一 cleanup）。

## 4. 有界队列 + 后台 runner

### 4.1 数据结构（`WorkflowEngine.__init__`）

```python
self._queue: asyncio.Queue[QueuedItem] | None      # 有界，maxsize = max_queue_size
self._queue_worker: asyncio.Task[None] | None      # 后台派发任务
self._active: set[WorkflowId]                      # 已持有执行槽的任务（slots 精确计数）
```

- **`QueuedItem`**：判别条目（queue 元素，`workflow_engine.py` 内定义）：

```python
class QueuedItemKind(StrEnum):
    SUBMIT = "submit"    # 新任务：派发 _run_and_cleanup
    RESUME = "resume"    # 恢复暂停：派发 _run_resume

@dataclass(frozen=True)
class QueuedItem:
    task_id: WorkflowId
    kind: QueuedItemKind
```

  - 队列必须携带 kind——`_drain_queue` 无法从 `WorkflowId` 判别「新提交」还是「恢复暂停」，二者派发不同的执行包装。
- 新配置参数 `max_queue_size: int = 5`，与 `max_sub_agents` 并列（`workflow_engine.py:74` 签名扩展）。真实构造点仅 `core/agent/_lifecycle_task_system.py:53`（`core/hitl.py:33` 是类 docstring 用法示例，非调用点）；不传则用默认。

### 4.2 `start()`

创建 semaphore 后，额外创建并启动 `_queue_worker`：

```python
self._queue = asyncio.Queue(maxsize=self._max_queue_size)
self._queue_worker = asyncio.create_task(self._drain_queue())
```

### 4.3 `_drain_queue()`（后台 runner，单任务）

```python
async def _drain_queue(self) -> None:
    """后台派发循环——单任务，非阻塞聊天轮。

    从队列取出条目 → 终态复核 → acquire 槽位 → 派发执行。
    槽位所有权：已派发 → 归执行包装（_run_and_cleanup/_run_resume 的 finally）所有；
    未派发被 stop() 取消 → 由本循环释放，防泄漏。
    """
    while True:
        item: QueuedItem = await self._queue.get()   # 阻塞等排队条目
        task_id = item.task_id
        dispatched = False
        held_permit = False
        try:
            sub_agent = self._tasks.get(task_id)
            if sub_agent is None:
                continue                          # 已 cancel/stop 清理，跳过（不 acquire）
            ts = sub_agent._task_state
            if ts is None or ts.is_terminal():
                self._cleanup_task(task_id)        # 兜底清理（cancel 侧通常已清）
                continue
            await self._semaphore.acquire()        # acquire 移到此处——不阻塞聊天轮
            held_permit = True
            self._active.add(task_id)
            if task_id not in self._tasks:         # acquire 等待期间被 cancel/stop 清理
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
                # create_task 罕见失败（如事件循环关闭）：释放槽位 + 清理注册表，防槽位泄漏
                # （现 submit() 的 create_task 守卫 workflow_engine.py:268-274 迁移至此）
                self._active.discard(task_id)
                self._semaphore.release()
                held_permit = False
                self._cleanup_task(task_id)
                continue
            self._task_futures[task_id] = task     # 必须登记——stop() 靠它等待/取消
            dispatched = True                      # 已派发：槽位归执行包装所有
        except asyncio.CancelledError:
            if held_permit and not dispatched:
                # stop() 取消：已 acquire 未派发 → 释放槽位 + 移出 _active
                # （执行包装不存在，槽位不能泄漏；acquire 等待中被取消则 held_permit=False，无槽位可释放）
                self._active.discard(task_id)
                self._semaphore.release()
            raise
        finally:
            self._queue.task_done()
```

- **`_task_futures` 登记迁移**：现 `submit()` 在 `workflow_engine.py:275` 登记 `_task_futures`。新流程下 spawn 全部发生在 `_drain_queue`，**登记必须随之迁移**——否则 `stop()`（`workflow_engine.py:144`）看不到已派发任务，排队中被派发的任务无法被等待/取消。
- `_run_and_cleanup`/`_run_resume` 改为接收已取出的 `sub_agent`（不再从 `_tasks` 内部反查）。
- **槽位所有权单一**：worker 在「已 acquire 未派发」窗口负责释放；一旦派发（`dispatched=True`），槽位由执行包装 finally 负责（终态清理或 PAUSED 释放，§5）。
- **PAUSED 任务不占执行槽**：`_run_and_cleanup`/`_run_resume` 的 finally 改为 PAUSED 时**释放 semaphore + 从 `_active` 移除，但不清理注册表**（见 §5）。

### 4.4 `submit()` 改造

```python
# 1. 前置校验（_started / intent 非空）不变
# 2. 建 task_id / mailbox / sub_agent
# 3. 预创建 WorkflowState(status=QUEUED, tools=context.tools, session_id=context.session_id)
#    → sub_agent._task_state = task_state
# 4. 注册 _tasks[task_id] / _mailboxes[task_id]
# 5. 推 QUEUED 事件到 mailbox
# 6. try: self._queue.put_nowait(QueuedItem(task_id, SUBMIT))
#    except asyncio.QueueFull:
#        _cleanup_task(task_id)  # 反注册 _tasks/_mailboxes
#        return SubmitError(code=QUEUE_FULL, message="任务队列已满，请稍后再试")
# 7. return mailbox   # 立即，不等待槽位
```

- 不再 `await self._semaphore.acquire()`；semaphore 只由 `_drain_queue` 消费。
- 新增 `SubmitRejectionCode.QUEUE_FULL`。

### 4.5 `resume()` 改造

```python
# 现有校验不变（task 存在 / 非终态 / 仅 PAUSED 可恢复 / 幂等 RUNNING-RESUMING 返回 mailbox）
# PAUSED 分支：
#   mailbox = self._mailboxes[task_id]
#   if ts.resume_pending:                       # 已在排队恢复——幂等短路（无论槽是否已空）
#       return mailbox                          #   （重复 resume 不得二次入队）
#   if 槽空闲（not self._semaphore.locked()）:
#       await self._semaphore.acquire()
#       self._active.add(task_id)
#       ts.resume_pending = True                 # 直接路径也置标记——防「spawn 后、置 RESUMING 前」重复 resume 二次入队
#       task = asyncio.create_task(self._run_resume(sub_agent, task_id))
#       self._task_futures[task_id] = task
#   else:
#       ts.resume_pending = True                 # 排队中标记——重复 resume() 短路
#       try:
#           self._queue.put_nowait(QueuedItem(task_id, RESUME))   # 排队等待槽位（公平 FIFO）
#       except asyncio.QueueFull:
#           ts.resume_pending = False            # 入队失败清除标记
#           return ResumeError(code=QUEUE_FULL, message="执行队列已满，请稍后再试")
#       push_mailbox_event(mailbox, WorkflowEventType.QUEUED, {"task_id": str(task_id)})  # 排队可见（§7）
#       # 任务状态保持 PAUSED，不回退为 QUEUED（resume_from_pause 要求 status==PAUSED，
#       # 见 sub_agent.py:210——置 QUEUED 会让恢复路径抛 RuntimeError）
#   return mailbox
```

- **resume 槽满默认入队**（`QueuedItem(kind=RESUME)`，与 submit 同队列 FIFO）——符合计划「resume 时重新排队/acquire，保持公平性并防饥饿」。任务状态**保持 PAUSED**（`resume_from_pause` 的 `status == PAUSED` 前置不变）；入队后由 `_drain_queue` 按 kind 派发 `_run_resume`。
- **`resume_pending` 幂等标记**（`WorkflowState.resume_pending: bool`，本字段是 E2/E3 在 WorkflowState 上唯一新增——E3 §3.1 明确**不引入** `cancelled` 字段，取消用 `status == CANCELLED` 表达）：**两条恢复路径（直接 acquire / 排队）在入口即置标记**，覆盖「spawn 后、`_run_resume` 置 RESUMING 前」的窗口——该窗口内重复 `resume()` 短路返回同一 mailbox，不二次入队。标记由 **`_run_resume` 首步置 RESUMING 时统一清除**（单一清理点，直接/排队两路径共用；排队入队失败时在 `QueueFull` 分支立即清）。
- **队列满 → `ResumeError(QUEUE_FULL)` 快速失败**：恢复请求不长期悬空，用户重试即可；不无限 await（满足计划验收）。入队失败须清 `resume_pending`，否则下次 resume 被错误短路。

### 4.6 `stop()` 改造

```python
# 1. _started = False
# 2. 停 _queue_worker：worker.cancel() + await asyncio.gather(return_exceptions=True)。
#    worker 的 except CancelledError 已处理「已 acquire 未派发」的槽位释放（§4.3）；
#    未 acquire 的条目自然留在队列，无需归还（避免把已派发任务重复入队）。
# 3. 终态化未派发/暂停任务：扫描 _tasks——task_state 为空、status in (QUEUED, PAUSED)、
#    或 task_id 不在 _task_futures → 置 CANCELLED + 推 CANCELLED 事件
#    + _cleanup_task（+ best-effort _cleanup_storage）。已在 _tasks 缺失的跳过。
#    注：PAUSED 任务的 task_id 仍在 _task_futures（§5 PAUSED 分支不调 _cleanup_task，
#    而弹出 _task_futures 的正是 _cleanup_task，workflow_engine.py:558）——
#    仅凭「不在 _task_futures」选不中 PAUSED，须显式 `status in (QUEUED, PAUSED)` 判。
# 4. 等待活跃任务自然完成（现有逻辑，_task_futures + timeout）
# 5. 兜底清理（现有逻辑）
```

- **不依赖「归还条目」**：stop 以 `_tasks` 为唯一权威扫描队列/在途条目——无论条目在队列里、还是已被 worker `get()` 出但未 spawn（status 仍 QUEUED）、还是已 acquire 未派发，扫描都按 status 覆盖。worker 取消时只需释放自己已持有的槽位（§4.3 `held_permit` 分支），不做 re-enqueue——避免把**已派发**的运行中任务重复入队（否则会被 step 3 误标 CANCELLED 且脱离 step 4 的等待，产生 detached 运行 + 终态事件重复）。
- 排队任务在 stop 时**不等待 semaphore**——直接终态化 + 清理。

### 4.7 `cancel()` 改造（对齐 E3）

`lifecycle.cancel()` 新增显式 **QUEUED 分支**（当前早退分支 `query` 不到：QUEUED 任务 `_orchestrator is None` 时落入 `lifecycle.py:158-161`，只置 CANCELLED 不推事件不清理）：

```python
# 分支 0：QUEUED（status == QUEUED）
#   ts.status = CANCELLED; ts.updated_at = now_iso()
#   push CANCELLED 事件（唯一终态）
#   _cleanup_task(task_id)         # 移除 _tasks/_mailboxes
#   best-effort _cleanup_storage
#   不 release semaphore——QUEUED 任务从未 acquire
#   返回 True
```

- 队列中残留的 task_id 由 `_drain_queue` 取到时因 `_tasks` 缺失而跳过（§4.3），无需从 `asyncio.Queue` 移除。
- **PAUSED 分支去掉 `semaphore.release()`**（`lifecycle.py:153-154` 现行为）——E2 §5 起 PAUSED 在暂停时已释放 slot，此处再 release 构成双重释放（`BoundedSemaphore` 越界抛 `ValueError`）。`cleanup_task`/`cleanup_storage`/推事件保留。

## 5. PAUSED 释放 slot（现有语义变更）

当前 `_run_and_cleanup` finally（`workflow_engine.py:252-266`）：

```python
if sub_agent._task_state.status == PAUSED:
    return          # 不 release，保留 slot
self._cleanup_task(task_id)
await self._cleanup_storage(task_id, sub_agent)
self._semaphore.release()
```

改为：

```python
if sub_agent._task_state.status == PAUSED:
    self._semaphore.release()       # 释放执行槽，供 runner 派发下一个排队任务
    self._active.discard(task_id)   # slot 持有集合同步
    return                           # checkpoint/mailbox/注册表保留
self._cleanup_task(task_id)
self._semaphore.release()           # 终态分支 release 先于 cleanup_storage（E3 §3.4 调整——
self._active.discard(task_id)       #   防「清理 await 中被二次取消跳过 release」的 slot 泄漏）
await self._cleanup_storage(task_id, sub_agent)
```

- **slot 释放不变式（防双重释放）**：semaphore 每取得一次恰释放一次——
  - RUNNING→终态（COMPLETED/FAILED/CANCELLED）：`_run_*` finally 释放；
  - RUNNING→PAUSED：`_run_*` finally 的 PAUSED 分支释放；
  - PAUSED→CANCELLED（cancel）：**不释放**（暂停时已释放）；
  - QUEUED：从未取得，cancel/stop 均不释放。
- `_run_resume` 的 finally 对称处理：再次 PAUSED → 释放槽保留；终态 → 清理 + 释放槽。

## 6. cognitive 层

`core/cognitive/tools.py:_handle_submit_task`（:214）**无需修改**：`await submit()` 现在立即返回 mailbox / SubmitError，聊天轮不再被槽位阻塞。语义不变：
- `SubmitError` → `ToolResult.fail`（含 QUEUE_FULL 场景，消息提示用户稍后再试）；
- 成功 → `ToolResult.ok`，`async_task_id` 为 mailbox.task_id。

## 7. 对外契约影响

| 面 | 变更 |
|---|---|
| `WorkflowStatus`（`Module/workflow_contract/schema.py:64`） | +QUEUED（12 态） |
| `WorkflowEventType`（`schema.py:360`） | +QUEUED（6→7 种）——submit/resume 排队时推 `QUEUED` 事件，mailbox 消费者可见排队 |
| `SubmitRejectionCode` | +QUEUE_FULL |
| `ResumeErrorCode` | +QUEUE_FULL（resume 队列满快速失败） |
| `WorkflowState` | +`resume_pending: bool`（排队恢复幂等标记；E3 §3.1 **不引入** `cancelled`——取消用 status 表达） |
| debug snapshot | `slots_used` 改由 `_active` 集合计算（`get_debug_snapshot` 需接收 `_active`/queue，签名扩展）；新增 `queued` 计数（`_queue.qsize()`）。`query.py:470-471` 现 `slots_used=len(tasks)` 会把 QUEUED/PAUSED 计入占槽——必须修 |
| 前端 TasksTab | status 显示任意字符串（非 completed/failed 灰），QUEUED 自动可见；不强制改前端 |

> 注：`WorkflowStatus`/`WorkflowEventType`/`SubmitRejectionCode` 位于 `Module/workflow_contract`（core ↔ workflow_engine 跨边契约），非 `frontend_contract`。debug api 透传 status 字符串给前端，无需契约层改动。

## 8. 测试计划（TDD）

| 用例 | 断言 |
|---|---|
| max=1、首任务 RUNNING/PAUSED 占槽，第二次 submit | 短时限内返回 mailbox（非阻塞），task 状态 QUEUED |
| 队列满（max_queue_size=2 连发 3 个、槽全占） | 第 3 个返回 `SubmitError(QUEUE_FULL)` |
| FIFO：多任务排队，槽释放后按序派发 | 执行顺序 == 提交顺序 |
| PAUSED 释放槽 → 排队任务被派发 | 首任务 PAUSED 后 `_active` 不含它、semaphore 可用、排队任务启动 |
| resume 槽满队列有空 | 入队 `QueuedItem(RESUME)`、任务保持 PAUSED；槽释放后自动恢复执行 |
| resume 队列满 | 返回 `ResumeError(QUEUE_FULL)`（不阻塞、不置 QUEUED） |
| resume 槽空 | 直接恢复执行（无需排队） |
| resume 排队中重复 resume | 幂等短路返回同一 mailbox，不二次入队 |
| stop 终态化 PAUSED 任务 | PAUSED 任务在 stop 时置 CANCELLED + 唯一事件（`status in (QUEUED, PAUSED)` 扫描选中） |
| cancel QUEUED 任务 | 状态 CANCELLED、推**一次** CANCELLED 事件、`_tasks`/`_mailboxes` 清理、semaphore 不被错误释放 |
| cancel-after-pause（防双重释放） | PAUSED 任务 pause 后 cancel：不抛 `ValueError`，slot 只释放一次 |
| stop 清理排队任务 + 在途条目 | 排队任务全部 CANCELLED + 事件；worker 在 acquire 阻塞时被 stop，其持有的 item 被 drain；无等待 semaphore 的 task 泄漏 |
| 幂等/二次 submit 契约 | 现有「第二次 submit 应阻塞」测试改为断言立即返回（契约变更） |
| `_handle_submit_task` 不阻塞 | fake 引擎：submit 返回后聊天轮立即继续（service 层） |

## 9. 验收标准

1. 首任务无限暂停时，第二次 `submit()` 在短确定时限内返回 mailbox（或明确 `QUEUE_FULL`），**不阻塞聊天 turn**。
2. 队列满返回稳定错误，不无限 await。
3. PAUSED 释放 slot，resume 重新 acquire；多任务排队 FIFO、无饥饿。
4. stop/cancel 移除排队任务并推唯一终态；shutdown 不留下等待 semaphore 的 task。
5. 全部门禁绿：`make test-quick` / `make test-normal` / pyright / ruff / contract drift。

## 10. 待记录决策

- **max_queue_size 默认值 = 5**（与 max_sub_agents 默认一致；config 化由调用方按需覆盖）。
- **resume 槽满默认入队**（公平 FIFO，与 submit 同队列；任务保持 PAUSED）；仅**队列满**才快速失败 `ResumeError(QUEUE_FULL)`——与计划「resume 重新排队/acquire 保持公平」一致。
- **QUEUED 事件为强制契约变更**（`WorkflowEventType` +QUEUED），排队对 mailbox 消费者可见。
- **slot 释放不变式**：每取得一次 semaphore 恰释放一次（§5），cancel(PAUSED) 不再重复释放。
