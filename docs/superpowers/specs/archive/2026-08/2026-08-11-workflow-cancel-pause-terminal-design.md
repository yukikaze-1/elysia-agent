# WorkflowEngine cancel/pause 终态单调性设计（E3）

> **状态**：设计定稿（2026-08-11）
> **决策**：**强取消**（2026-08-11 拍板）——cancel 立即中断 in-flight LLM/planner future；已开始的工具调用用 shield 保护（完成不打断副作用）、不再开始下一个调用
> **对应**：P2-08 + 已知 M19/M20（`todo.md` E3）、`docs/superpowers/plans/archive/2026-08/2026-08-10-full-project-remediation-plan.md` Task E3
> **相关 spec**：E2 submit 非阻塞有界排队（`2026-08-11-workflow-submit-bounded-queue-design.md`）——本 spec 与 E2 共享 slot 释放/排队语义，§7 对齐

---

## 1. 问题（P2-08 + M19/M20）

1. **cancel 只设标志、不真取消**：`lifecycle.cancel()` RUNNING 分支仅写 `exec_state.cancel_requested`（`Module/workflow_engine/lifecycle.py:169`），有显式 TODO「若 Orchestrator 因异常永远不到达下一个循环迭代，cancel 信号可能永远不被处理」（:123-125）。in-flight 的 LLM 调用会跑完，取消延迟可达数十秒。
2. **终态逆转**：`planning.py` 返回后**无条件写 RUNNING**（:121-123、:146-147）——cancel 期间（PLANNING 被置 CANCELLED）可能被改回 RUNNING；`orchestrator._init_exec`（:236-238）resume 路径 `RESUMING → RUNNING` 也是逆转点。
3. **cancel 期间重复/缺失 terminal 事件**：`_run_and_cleanup` 的 CancelledError 分支推 FAILED「任务被取消」（`workflow_engine.py:228-238`），与用户 cancel 的 CANCELLED 语义不符；多个路径可能重复推或漏推终态。
4. **M19**：resume 后再次 pause 可能销毁 task/mailbox，消费者永久阻塞。

## 2. 决策

**强取消 + 终态单调**（2026-08-11 拍板）：

- `cancel()` **同步置 `status = CANCELLED`（权威终态）**，随后 `owned.cancel()` 中断 in-flight future + 置 `exec_state.cancel_requested`（循环顶部快速路径）。取消信号即终态——**不依赖运行协程回写 status**，PLANNING 等任何阶段取消后 status 恒为 CANCELLED。
- **LLM/planner 调用立即中断**（不 shield）；**工具调用 `asyncio.shield` 保护**——已开始工具完成、副作用不打断、结果弃用、不再开始下一个调用。
- 每个外部 await（planner、LLM、tool、retry）后检查 `status == CANCELLED` → 抛 CancelledError；`CANCELLED/FAILED/COMPLETED` 终态**不可逆**。（semaphore acquire 在 `_drain_queue` worker 内、无 per-task ts——取消覆盖靠 E2 §4.3 的 `_tasks` 成员复查，见 §2 注。）
- terminal event **只推一次**（`_terminal_pushed` 守卫记录首个终态），registry/mailbox 清理幂等。
- 修 M19：resume 后再次 pause 不销毁 task/mailbox。
- 与 E2 对齐：PAUSED 释放 slot、resume 重新 acquire；cancel 排队/暂停任务均立即完成（E2 §4.5/§4.7）。

## 3. 取消机制

### 3.1 统一 token = 任务状态（不做独立字段）

- **`WorkflowState.cancelled` 字段不引入**：取消信号直接用 `ts.status == CANCELLED` 表达——`cancel()` 同步写 status，任何 await 边界一查即得；避免独立 token 的 checkpoint 序列化问题（`WorkflowState` 经 `WorkflowCheckpoint` 整体持久化，`asyncio.Event` 不可序列化、`bool` 成第二真相源）与「PAUSED 却带 cancelled 标记」的击穿态。
- 边界检查统一为：

```python
def _check_cancelled(ts: WorkflowState) -> None:
    """取消检查——cancel() 已同步置 CANCELLED，await 边界一查即中。"""
    if ts.status == WorkflowStatus.CANCELLED:
        raise asyncio.CancelledError()
```

### 3.2 `lifecycle.cancel()` 改造

```python
# 分支 0：QUEUED（E2 §4.7）——置 CANCELLED + 事件 + 清理，不 release
# 分支 1：PAUSED（E2 §4.7 改）——置 CANCELLED + 事件 + 清理，不 release（暂停时已释放 slot）
# 分支 2：RUNNING / PLANNING：
#   if ts.is_terminal():
#       return True                            # 已终态 = 已取消（幂等）
#   # —— 以下为单同步块（无 await）——
#   if ts.status == WorkflowStatus.COMPLETED:  # 与 finalize 竞争：终态写入已原子发生则不再覆盖
#       return True
#   ts.status = WorkflowStatus.CANCELLED       # ① 同步置权威终态（PLANNING/RUNNING 恒可达）
#   ts.updated_at = now_iso()
#   if sub_agent._orchestrator is not None:
#       exec_state = sub_agent._orchestrator._exec_state
#       if exec_state is not None:             # ② 保留 None 守卫（planning 阶段 _exec_state 未建）
#           exec_state.cancel_requested = True #    循环顶部快速路径
#   owned = task_futures.get(task_id)          # ③ owned.cancel() 打断 in-flight await（强取消）
#   if owned is not None and not owned.done():
#       owned.cancel()
#   return True
```

- `lifecycle.cancel` 签名需接收 `task_futures`（E2 已扩展 QUEUED/PAUSED 分支）。
- **①同步置终态**：PLANNING（orchestrator 未入 `run`）、RUNNING 任何阶段 cancel 后 status 恒 CANCELLED——不再依赖 orchestrator `:193-198` 或 `_run_and_cleanup` 回写（这两个回写作为兜底保留但非必需）。
- **②`exec_state is None` 守卫保留**：planning 阶段 orchestrator 已建（`sub_agent.run:183-190`）但 `_exec_state` 仍 None——无条件解引用会 AttributeError。
- **`exec_state.cancel_requested` 快速路径仅对首次 RUNNING 有效**：resume 路径（RESUMING）时 `_exec_state` 是旧 pause checkpoint，写 `cancel_requested` 会被 `_init_exec` 的 `_merge_exec_state` 重建丢弃（`finalize.py:92-97`）——resume 中取消**依赖 `owned.cancel()` 的 CancelledError 投递**（强取消本就不依赖该标志）。实现不得把该标志当作 resume 路径的取消保证。
- **终态一致性**：`cancel()` 写 CANCELLED 前先查 `is_terminal()`（已终态即 bail）；finalize 的 COMPLETED 写在协程首个 await 之前。顺序不变量与证明见 §3.5（不依赖「同步块先到者胜」）。终态守卫见 §3.5。

### 3.3 executor 层：LLM 强中断 + 工具 shield + 边界 token 检查

**签名穿透**：`executor.execute` 需接收任务状态以做边界检查。改造调用链：

```
orchestrator._execute_nodes(:444)      → 增加 ts 参数
  → execution.execute_nodes(:76)       → 增加 ts 参数
    → executor.execute(node, task_id, tools, session_id, ts)   # 新增 ts
```

（同步更新 executor 全部测试调用点。）

```python
# AgentInfra 加运行时字段 orphan_tools: set[asyncio.Task]（共享对象，跨任务存活；见下注）
# Executor._call_tool 创建工具任务时登记 + done 回调移除；stop() 从 self._infra.orphan_tools 采集

async def execute(self, node, task_id, tools, session_id, ts):
    # …解析上游输入…
    _check_cancelled(ts)                                # ① 执行前
    # LLM 工具匹配（强中断——不 shield）
    tool_name, params = await self._match_tool(...)     # CancelledError 在此穿透
    _check_cancelled(ts)                                # ② LLM 返回后、工具调用前
    if tool_name == _DIRECT_TEXT_SENTINEL:
        # 直出文本节点：_call_llm_direct 同样走强中断路径（②④ 检查点同样适用）
        content = await self._call_llm_direct(...)      # CancelledError 在此穿透（不 shield）
        _check_cancelled(ts)
        # …存储 LLM 文本…
        return
    # 工具调用（shield——在途副作用不打断）
    tool_task = asyncio.create_task(self._call_tool(tool_name, params, task_id, ...))
    self._infra.orphan_tools.add(tool_task)
    tool_task.add_done_callback(self._infra.orphan_tools.discard)   # 收引用 + 防泄漏
    tool_task.add_done_callback(
        lambda t: t.exception() if not t.cancelled() else None  # 收「Task exception was never retrieved」
    )
    try:
        result = await asyncio.shield(tool_task)
    except asyncio.CancelledError:
        _check_cancelled(ts)                            # ③ 工具返回后被取消 → 走 CANCELLED
        raise                                           # 否则正常抛
    _check_cancelled(ts)                                # ④ 存储前
    # …存储 artifact…
```

- **`_call_tool` 而非内联 `tool_registry.execute`**：真实工具调用经 `_call_tool(...)` 包装（`ToolContext` 构造 + 透传，`executor.py:437`）；shield 必须包 `_call_tool`，参数才正确。
- **shield 孤儿工具容器挂共享 `AgentInfra`（引擎级，跨任务存活）**：`_orphan_tools` 是 per-Executor 实例的容器，任务清理（`_cleanup_task` 弹出 `_tasks`）后 stop() 无法再从 sub_agent 枚举——**容器必须挂在跨任务共享对象上**。实现：`Module/workflow_contract/infra.py` 的 `AgentInfra` 加运行时字段 `orphan_tools: set[asyncio.Task]`（`field(default_factory=set)`）；executor `_call_tool` 创建工具任务时 `self._infra.orphan_tools.add(tool_task)` + done 回调 `discard`（收引用 + 防泄漏）——`infra` 是 `WorkflowEngine` 与 executor 共享的同一对象，工具完成后或 stop() 采集后自然清空。**stop() 在任务等待/取消**之后**从 `self._infra.orphan_tools` 采集**（容器不随 `_tasks` 弹出，stop() 自触发取消所产生的新孤儿同样可达），`await asyncio.wait(orphans, timeout=3)`、超时 `cancel()`——防止事件循环关闭强杀正在执行副作用的工具（「副作用不打断」在 loop-close 场景同样成立）。孤儿工具若抛异常由 done 回调收取。
  - **契约 docstring 同步**：`infra.py:28-30` 现声明「不含任务状态」「各个 Sub-Agent/Worker 只读引用、不做修改」——加 `orphan_tools` 后须**同步更新 docstring**，注明该字段为 engine 内部运行时用途（executor 多协程 add/discard 改写、core 构造但不使用），防契约文档误导维护者。此为 `AgentInfra` 上唯一 engine 侧可变运行时字段。
- 副作用语义：取消后工具继续在后台完成（设计意图），其结果不落 artifact（存储前被 ④ 拦下）；期间新任务可取得同一 slot 并发执行工具——副作用重叠为**已接受取舍**（用户决策：不打断已开始副作用），记录 §9。
- `_match_tool`/`_call_llm_direct` 内部不做 shield——`await self._infra.llm.chat.completions.create(...)`（`executor.py:298/382`）被 `owned.cancel()` 直接中断（AsyncOpenAI 支持任务取消，流式/请求即停，不再消耗 token）。

### 3.4 CancelledError 传播链（统一终态）

```
cancel() → 同步置 status=CANCELLED → owned.cancel()
  → CancelledError 在下一个 await 抛（LLM / shield 的 caller / planner）
    → executor/planner 不吞，向上传播
      → orchestrator.run except asyncio.CancelledError（:193-198 兜底）:
            ts.status = CANCELLED（幂等）; save checkpoint（best-effort）; raise
        → SubAgent.run() / planning 不吞，向上传播
          → _run_and_cleanup / _run_resume:
                except asyncio.CancelledError:
                    ts.status = CANCELLED（幂等，权威已由 cancel() 置）   # ①
                    push_mailbox_event(mailbox, CANCELLED, {...})        # 唯一终态事件
                    raise                                                 # 传播给 finally 统一收尾
                finally（唯一 release 点——except 分支绝不 release，防双重释放）:
                    if ts.status == PAUSED:                              # ⑤ 仅真暂停命中；取消已覆写 CANCELLED，不命中
                        release_slot(); active.discard; return           #    checkpoint/mailbox/注册表保留（E2 §5）
                    self._cleanup_task(task_id)                          # ② 同步清理注册表
                    release_slot(); active.discard                       # ③ 先释放 slot（同步）——见下
                    await best-effort cleanup_storage                    # ④ 其后已无 slot 可泄漏
```

- **release 唯一释放点 = finally**：`except asyncio.CancelledError` 分支**不 release**（只置状态 + 推事件 + `raise`）——否则 finally 对 CANCELLED（非 PAUSED）再 release 构成双重释放（`BoundedSemaphore` 每次取消抛 `ValueError`，且该异常替换正在重抛的 CancelledError）。
- **`_run_and_cleanup`/`_run_resume` 收到 CancelledError 时不再 push FAILED**（删除现 :228-238 与 resume 侧对应分支）。
- **slot 释放前置**：release 是同步操作，放在任何可能再抛 CancelledError 的 await（`_cleanup_storage`）**之前**——即使清理中被二次取消，slot 已释放、无泄漏（修复「第二次 CancelledError 落在 finally 的 cleanup_storage await 跳过 release」）。
- **重复 cancel 幂等**：`cancel()` 已同步置 CANCELLED（终态），后续 `cancel()` 见 `is_terminal()` 直接返回 True，不会对 unwinding 中任务二次 `owned.cancel()`——消除了二次 CancelledError 击穿清理路径的可能。

### 3.5 terminal event 唯一性

`WorkflowMailbox`（`Module/workflow_engine/mailbox.py`）加**终态事件守卫**：

```python
# WorkflowMailbox 增加：
self._terminal_pushed: bool = False
self._terminal_status: WorkflowStatus | None = None

# _TERMINAL_EVENTS = {COMPLETED, FAILED, CANCELLED}（QUEUED/PROGRESS/… 非终态，不入）
# push_mailbox_event 对 terminal 事件：
if event.type in _TERMINAL_EVENTS:
    if mailbox._terminal_pushed:
        return            # 已推过终态，静默丢弃（防多路径重复）
    mailbox._terminal_pushed = True
    mailbox._terminal_status = <对应 status>
```

- **`_terminal_pushed` 记录首个终态**：防「orchestrator FAILED + engine 兜底 FAILED + CancelledError 分支」多路径重复推。
- **终态一致性不变量**（防 cancel∥complete 分叉）：① `finalize` 的 **COMPLETED status 写在协程首个 await 之前**（`finalize.py:159` 先于 `:161` 的 `await save_checkpoint`、`:208` 的推事件）——一旦写过，该协程后续不可能再被 cancel 改写成 CANCELLED；② `cancel()` 在写 CANCELLED 前**先查 `is_terminal()`**，读到已终态即 bail。二者共同保证「status 与首个终态事件一致」——**不依赖「两个协程同步块先到者胜」**（finalize 的置状态与推事件之间隔着 await，不是同步块）。实现时须保持该顺序不变量（status 写先于本协程首个 await；cancel 写前必查终态），加断言守护。

## 4. 终态不可逆

- **所有状态写点前置 `is_terminal()` 检查**：写 RUNNING/PLANNING/PAUSED/RESUMING 前若 `ts.is_terminal()` → 抛 CancelledError / 直接终止，禁止逆转。
- **修正的逆转写点清单**（基于代码核实的完整枚举）：
  - `planning.py:121-123`、`:146-147`：规划/重规划返回后写 RUNNING 前检查终态（已 CANCELLED → 直接终止）。
  - `orchestrator._init_exec:236-238`：resume 路径 `RESUMING → RUNNING` 写点前检查终态（resume 加载 checkpoint 时若已 CANCELLED → 不写 RUNNING）。
  - `resume()` 入口已有终态检查（E2 保留）。
- **`_check_interrupts` 的 pause/cancel 分支**（`orchestrator.py:257/266`）：cancel 分支（:266）写 CANCELLED 后返回——若 `cancel()` 已同步置 CANCELLED，此分支成为幂等兜底。

## 5. M19 修复（resume→pause 不销毁）

- `_run_resume` finally 对 PAUSED early-return **保留 task/mailbox/注册表**（`workflow_engine.py:392-394` 已具备）——补验证测试覆盖「resume → 再次 pause → 再 resume」循环。
- 消费者不永久阻塞：任何路径（含取消）必推唯一 terminal 事件（§3.5）。
- 复核 resume 重复 pause 时 `_task_futures[task_id]` 不泄漏（旧 task 已终态自然回收，新 spawn 覆盖登记）。

## 6. 与 E2 对齐

| 场景 | 行为 |
|---|---|
| cancel QUEUED 任务 | E2 §4.7 分支 0：置 CANCELLED + 事件 + 清理，不 release（从未 acquire） |
| cancel PAUSED 任务 | E2 §4.7 分支 1（改）：置 CANCELLED + 事件 + 清理，**不 release**（暂停时已释放 slot，E2 §5） |
| PAUSED 释放 slot | E2 §5：`_run_*` finally PAUSED 分支 release + `_active` 移除 |
| resume 重新 acquire | E2 §4.5：槽空直接 acquire；槽满入队（`resume_pending` 幂等标记，定义/去重/清理见 E2 §4.5——两路径入口置标记、`_run_resume` 置 RESUMING 时清、QueueFull 失败清）；队列满 `ResumeError(QUEUE_FULL)` |
| stop() | E2 §4.6 + 本 spec：扫描 `_tasks` 终态化 QUEUED/PAUSED → 等活跃任务（`_task_futures`）→ 超时 owned.cancel 强取消 |

## 7. 测试计划（确定性，需 fake-LLM——E4 基建）

| 用例 | 断言 |
|---|---|
| 慢 planner cancel | PLANNING 中 cancel → LLM future 中断、status=CANCELLED、`fake.calls` 不增长、不回 RUNNING |
| planning retry 间 cancel | retry 之间 `_check_cancelled` 生效，cancel 后不再重试 |
| tool 调用前 cancel | LLM 匹配完成、工具调用前 cancel → **工具未调用**（`_check_cancelled` ② 拦截） |
| tool 调用中 cancel | 工具调用完成（shield 副作用不打断）、结果不落 artifact、任务 CANCELLED |
| resume→pause→resume 循环 | 不销毁 task/mailbox，循环多次正常 |
| 重复 cancel | 首次 CANCELLED 后二次 cancel 幂等返回 True、不二次 owned.cancel、slot 无泄漏 |
| cancel∥pause 乱序 | 无论先后，终态 CANCELLED 唯一且不逆转（PAUSED 不复活） |
| cancel∥complete 乱序 | 先到终态者胜：status 与首个终态事件一致，无「COMPLETED 事件 + CANCELLED status」分叉 |
| stop | 活跃任务 CANCELLED 终态 + 唯一事件 + slot 无泄漏 |
| cancel 后无 token 消耗 | fake LLM 记录完成调用数，cancel 后完成数不再增长 |
| `_init_exec` RESUMING→RUNNING 逆转 | resume 加载 checkpoint 已 CANCELLED → 不写 RUNNING |

> fake-LLM：`Module/workflow_engine/tests/helpers/fake_llm.py`（E4 spec §3 定义）。

## 8. 验收标准

1. 状态一旦 `CANCELLED` 永远不会回 `RUNNING`。
2. 取消后不再调用 LLM/工具或消耗 token（强取消：in-flight LLM 立即停；已开始工具完成但结果弃用）。
3. terminal event 只推一次；`resume→pause→resume` 不销毁 task/mailbox；重复 cancel 幂等、slot 无泄漏。
4. 全部门禁绿：`make test-quick` / pyright / ruff / contract drift。

## 9. 待记录决策

- **强取消**：LLM/planner future 立即中断（不 shield）；工具调用 shield 保护副作用、边界 `_check_cancelled`（2026-08-11 拍板）。
- **取消信号 = 终态**：`cancel()` 同步置 `status=CANCELLED`（权威终态），不引入独立 `cancelled` 字段（避免 checkpoint 序列化问题 + 「PAUSED 带 cancelled」击穿态）；owned.cancel() 只负责打断 in-flight await。
- **slot 释放前置**：release 在清理 await 前同步执行，防二次取消泄漏；重复 cancel 幂等。
- **shield 孤儿工具**：保留引用 + done 回调收异常；与后续任务副作用重叠为已接受取舍。
- **终态事件守卫记录首个终态**：status 与首个终态事件原子一致（先到者胜），防分叉。
