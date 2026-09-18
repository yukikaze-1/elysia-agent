# Batch 10 — Thinking State Cancellation Recovery（KB-22）执行记录

> 本文件是 Batch 10 的执行记录。canonical scope = **KB-22 单条**。
> 结构按本批任务书 §102 的 32 节；每一节只写**实测**到的东西，未测的显式写"未做/不适用"。

---

## 1. Executive Summary

| 问题 | 回答 |
|---|---|
| **KB-22 confirmed?** | ✅ **CONFIRMED**（当前 HEAD 独立达到 **E2**：真 Agent + 真 `process_message` + 真 EventBus + 真 INTERRUPT 事件 + 真 FSM；屏障钉死挂起点） |
| **classification** | `USER` / `HIGH` / `E2` → **GUARDED**（`fix(cognitive): recover cancelled thinking lifecycle safely`） |
| **production fix?** | ✅ 有：`process()` 的取消边界从 `_think_loop` 扩到**整个回合**（含 prepare 与异常收口），收口经 canonical `_handle_interrupt()`，判据是 turn-scoped 的 `_generation_ctx` 而**不是** `fsm.state == THINKING` |
| **user reachability?** | ✅ **CONFIRMED_USER_REACHABLE**：取消源是用户可见的停止按钮（WS `interrupt`）/ 断线 / 语音取消 / 停机 / 模型热切换——全部落在同一挂起点 `loop.py:610` |
| **public API?** | 零变化（无契约/字段/枚举改动；`make check-types` 绿） |
| **persistence?** | 零 schema 变化（无新集合/字段/索引/迁移）。唯一差异：取消后恢复会给该 generation 补一条 `cancelled` 终态行（upsert，`GenerationRegistry.mark` 的既有语义） |
| **real CI?** | ⏳ **待 owner push**（本机无 GitHub 凭据，与 Batch 4–9 同一环境限制；§27.1） |

**一句话**：`_prepare_turn` 在 `enter_thinking()` 之后还有一个真挂起 await（generation 建档的
Mongo upsert），它不在任何取消边界内 ⇒ 该窗口里落下的取消逃出 `process()`，FSM 永久停在
**THINKING**、`_process_lock` 却已释放、回合任务已 cancelled —— 这是"锁真相 / 任务所有权 /
FSM 真相三者不一致"的教科书形态（TSR-4）。后果是用户面**永久失聪**：后续每条消息都被
ACK `accepted` 后进 `_pending_messages`，而 pending 只在 `STATE_CHANGE → IDLE` 出队。
仓内唯一恢复点是 `Agent.start()` 的 `fsm.reset()`。

---

## 2. Original Backlog Evidence

canonical 登记（`todo.md` KB-22，Batch 9 Challenger A 提出、Batch 9 明确不修）：

> `CognitiveLoop._prepare_turn` 在 `phases.enter_thinking()`（`loop.py:591`）之后仍有
> **未受保护的真挂起 await**（`:609` 的 `generation_registry.register(...)` → Mongo upsert；
> `except Exception` 接不住 `CancelledError`），而包住 `_prepare_turn` 的 try 在 `:714`、
> 包住 `_think_loop` 的 try 在 `:729` ⇒ 这个窗口里落下的取消**逃出 `process()`**，唯一会做
> `INTERRUPTED→IDLE` 的 `_handle_interrupt()` 永不执行 ⇒ **FSM 停在 THINKING**。
> 后果：agent 进程活着但**永久失聪**……**只有 `Agent.start()` 能救**。
> E1 同源第二处：`:719` 的 `except (CharacterBusyError, StorageUnavailableError) →
> _handle_turn_exception` 也在 try 之外。

**HEAD 复核（逐条，不继承）**：

| 原登记断言 | HEAD 复核结果 |
|---|---|
| 行号 `:591` / `:609` / `:714` / `:729` | ✅ 行号基本吻合（`register` 在 `:610`，`:609` 是其 `try:`） |
| `enter_thinking()` 后有"未受保护的真挂起 await" | ✅ **成立**，且 AST 扫描 + 运行时测量证明：入口之后**恰好一个**挂起点（`:610`） |
| `except Exception` 接不住 `CancelledError` | ✅ 成立（本机 `python -c` 实测 `CancelledError.__mro__ == (CancelledError, BaseException, object)`） |
| 窗口取消逃出 `process()` | ✅ 复现（§8） |
| FSM 停在 THINKING、永久失聪、只有 `start()` 能救 | ✅ 全部复现（§8/§9） |
| **"E1 同源第二处：`:719` 的 except 也在 try 之外"** | ⚠️ **部分不准**：`:719` 就是 `:714` 那个 try 的 except 子句，`CharacterBusyError` 是**被接住**的。真正的第二处是**该 handler 自身的 await**（`_handle_turn_exception` 里的 output/registry 调用）——在 handler 内部落下的取消同样会逃出（异常在 except 块里抛出不会再被兄弟 except 捕获）。本批的修法把这一处一并覆盖，有专门的 PRE_FIX_RED 用例（§18） |
| 隐含前提："只有取消会走这条逃逸路径" | ⚠️ **不成立**（Challenger 实证）：窗口里抛出的**普通异常**（`GenerationContext` 校验 ValueError / 领域 op-guard RuntimeError）同样逃出 `process()` ⇒ 同样的永久 THINKING。本批把异常边界与取消边界一并补齐（§11 的 W2），守卫用例 `test_generic_exception_after_turn_started_still_recovers` |
| Severity "高于 KB-6" | ⚠️ **不继承，独立判定**：两者都导致"用户面永久失聪"，但**修复代价与可达宽度不同**。KB-6（Batch 9）的触发需要"取消落在任务首个 event-loop step 之前"这一亚稳态窗口；KB-22 的窗口是**任何一次 Mongo 写挂起期间**（毫秒~秒级、每次回合都经过），且取消源有 4 条用户可见路径（§7）。因此维持 **HIGH**（与 KB-6 同级），但**可达宽度明显更大** |

---

## 3. Thinking Contract

**结论：选项 C —— "回合已被接受，正在做本回合的准备与认知工作"。**

| 证据 | 位置 |
|---|---|
| `enter_thinking()` 在 generation 建档、prompt 组装、身份快照**之前** | `loop.py:591` vs `:593-606` / `_think_loop:935` |
| 进入 THINKING 时**还没有**任何对外帧（`typing(True)` / `generation.started` 都在 `_start_turn_tracking` 里） | `loop.py:884-893`（`_think_loop` 内） |
| 但 `StateChangeData(THINKING)` 已经发给前端（UI 指示器先亮） | `core/status/fsm.py:154-159` |
| `should_suppress_vad_interrupt` 把 THINKING 视为"抑制 VAD 打断"的阶段 | `core/status/fsm.py:83-84` |

因此 `enter_thinking()` 放在 prepare 中段是**有意的语义**（"我接下了这条消息，开始为你工作"），
不是放错位置；本批**不移动**它（§52），只修取消边界。

---

## 4. Process Lifecycle Map

```
USER_INPUT（EventBus，v1 直发 / v2 ACK accepted 后投递）
  ↓ Agent._handle_user_input（同步；spawn cognitive task；登记 _current_task）
  ↓ CognitiveLoop.process                                  ← ★ async with self._process_lock（:699）★
       ├─ status.accepts_new_turns 门禁（:702）
       ├─ _prepare_turn（:715）
       │     ├─ await session_resolver            （:494，可挂起）
       │     ├─ await registry.get_or_load        （:507，可挂起）
       │     ├─ 身份快照 / runtime 换绑 / prompt 换绑 / TTS reset（同步）
       │     └─ await phases.enter_thinking()     （:591 → FSM IDLE→THINKING，同步段 + `_notify`）
       │     ├─ GenerationContext 建档            （:593，`_generation_ctx` 在此建立）
       │     └─ await generation_registry.register（:610，Mongo upsert —— ★KB-22 窗口★）
       │     └─ try_begin_generation()            （:627，同步；失败抛 CharacterBusyError）
       ├─ self._current_task = current_task()     （:725）
       ├─ await _think_loop(...)                  ← ★try :729 / except CancelledError :747★
       │     ├─ _build_turn_prompt → typing(True) + generation.started
       │     ├─ _stream_llm_response → first SENTENCE ⇒ enter_speaking（FSM→SPEAKING）
       │     ├─ _execute_tool_round → enter_executing/return_to_thinking（FSM→EXECUTING→THINKING）
       │     └─ _finalize_turn → _transition_idle → phases.finish_turn（FSM→IDLE + bus.post(IDLE)）
       └─ finally：`_clock = None` + `runtime.end_generation()`（:754-758）
  ↓ Agent._handle_state_change（AgentState.IDLE ⇒ _pending_messages.pop(0) → bus.post(USER_INPUT)）  ← ★唯一 drain★
```

关键点：

- `_process_lock` **acquire**：`process()` 第一行（`:699`）；**release**：`async with`
  `__aexit__`，即 `process()` 返回/抛出时——**包括取消**。
- FSM 进入 THINKING：`_prepare_turn` 的 `:591`（`enter_thinking()` 内部第一步，同步）。
- `enter_thinking` **之前**的 await：`:494`、`:507`；**之后**的 await：**仅 `:610`**（§5）。
- 捕获 `CancelledError` 的分支：`_think_loop` 的 `except (Exception, asyncio.CancelledError)`
  （`:1023`，清理 tools/TTS/typing 后 **re-raise**）→ `process()` 的
  `except asyncio.CancelledError`（`:747`）→ `_archive_partial_reply` + `_handle_interrupt()`。
- 会**直接逃出**的分支（修复前）：`:610` 落下的取消（本批修复）；`_handle_turn_exception`
  内部落下的取消（本批一并修复）。
- pending 何时 drain：**唯一**在 `_handle_state_change`，条件是收到 `StateChangeData(IDLE)`
  或 `IDLE_BACKGROUND_TASK`（`agent.py:1054-1057`）。
- `STATE_CHANGE → IDLE` 由谁触发：`FSMBackedPhaseController.finish_turn`（正常收尾，
  `fsm.py:226-229`）与 `handle_interrupt` 的第二段（打断收口，`fsm.py:244-247`）。

---

## 5. Suspension Map

`enter_thinking()`（`:591`）之后到"被取消安全 owner 接管"（`:729` 的 protected try）之间，
**每一个**可挂起 await（AST 扫描 + 运行时测量双重确认；`conda run -n Elysia` = Python 3.12.13）：

| Await | Purpose | Cancel possible? | Current handler（修复前） | FSM after cancel | Lock after cancel | Turn owner after cancel |
|---|---|---|---|---|---|---|
| `:591 await self.phases.enter_thinking()` | FSM→THINKING + `bus.post` + `_notify` | ❌ **不可取消**（生产接线无挂起点：unique `on_change` 注册者是 `wait_until_idle`，其回调无 await） | n/a | 已是 THINKING（transition 在 await 之前完成） | held | 本回合 |
| `:610 await self._generation_registry.register(...)` | generation 建档（Mongo upsert） | ✅ **可取消**（Mongo 在线时；`_db is None` 时同步返回） | `try :609 / except Exception :618` — **接不住 CancelledError** | **THINKING（stranded）** | **已释放**（`async with` 退出） | **无**（task cancelled/done，`_current_task` 已被 `cancel_current` 置 None） |
| `:715 await self._prepare_turn(...)`（外的 handler） | 异常收口 `_handle_turn_exception` 的 await（output 广播 / registry mark） | ✅ 可取消 | 修复前无（异常从 except 块抛出，不再被兄弟 except 捕获） | THINKING（stranded） | 已释放 | 无 |

**窗口边界**（回答任务书 §3 的核心问题）：**这不是"单点漏保护"，而是 `:699`（取锁）到
`:729`（进受保护 try）之间一整段没有 CancelledError-aware handler 的区域**；其中有 4 个可挂起
点（`:699` 争用锁、`:494`、`:507`、`:610`），只有 `:610` 在回合开始之后（= 会造成 stranded）。
窗口在 `_think_loop` 内第一个让出型 await 处闭合。修复因此**覆盖整段**，而不是补 `:610` 一行。

> 上表是**修复前**（HEAD）的形态。修复后 `:591` 的 `enter_thinking()` 与"本回合开始"的
> ownership 标记（`_generation_ctx`）之间不再有 await；`:610` 与异常收口都在取消边界之内。

---

## 6. Authority Map

| 事项 | 唯一 owner | 位置 / 机制 |
|---|---|---|
| **turn** | `CognitiveLoop.process()`（持 `_process_lock` 的那次调用） | `loop.py:699-758` |
| **process task** | `Agent._handle_user_input` spawn + `_lease_release_done` done-callback | `agent.py:963-973`（lease 恰释放一次） |
| **lock** | `process()` 的 `async with`（取消也会经 `__aexit__` 释放） | `loop.py:699`；`process_lock` 只读暴露给角色路由做 409 快路径 |
| **FSM phase** | `FSMBackedPhaseController`（唯一写 FSM 的层）；THINKING 的合法出口只有 `EXECUTING`/`SPEAKING`/`INTERRUPTED`（`THINKING→IDLE` **非法**） | `core/status/fsm.py`；`core/schema/agent.py:24-31` |
| **cancel** | `CognitiveLoop.cancel_current(source)`（唯一取消入口，6 个生产调用点）；它**只** `task.cancel()` + 清句柄 + 存 source，**不**碰 FSM | `loop.py:364-385`；`agent.py:857/930/991/1029`、`_lifecycle_tools.py:162`、`server.py:392` |
| **中断收口** | `CognitiveLoop._handle_interrupt()`（→ `phases.handle_interrupt()` → INTERRUPTED→IDLE） | `loop.py:1249-1294` |
| **pending drain** | `Agent._handle_state_change`（`StateChangeData(IDLE/IDLE_BACKGROUND_TASK)`） | `agent.py:1043-1079` |

> 注意 `Agent._handle_interrupt`（`agent.py:830-874`）**不是** FSM 的 owner：它只
> `cancel_current()` + 排空总线 + 清 pending。把 FSM 推离 THINKING 的永远是**被取消的那个回合**
> ——这正是修复前"取消逃出 ⇒ 没有任何人收口"的机制解释。

---

## 7. Production Cancellation Sources

逐项分类（A–I 按任务书 §8；`REACHABLE-IN-WINDOW` = 能落在 `:610` 那个挂起点上）：

| # | 来源 | 入口（用户动作） | 在本窗口可达？ | 语义 | 是否附带 FSM/总线副作用 |
|---|---|---|---|---|---|
| A | **USER interrupt（停止按钮）** | WS `{"type":"interrupt"}`（前端 Escape / Ctrl+Escape / `beforeunload`）→ `bus.post(InterruptData("websocket"), INTERRUPT)` | ✅ **可达** | 用户打断 | `Agent._handle_interrupt`：排空总线、清 pending |
| A′ | USER interrupt（VAD） | `audio_buffer` → `do_interrupt(skip_vad=True)` | ❌ **不可达**（`should_suppress_vad_interrupt` 在 THINKING 时直接 return——设计如此） | 语音打断 | — |
| A″ | 语音会话取消 | `voice_sessions.cancel` → `InterruptData("voice_cancel")` | ✅ 可达（Electron/desktop 客户端） | 用户打断 | 同上 |
| B | 新用户输入 / 替换回合 | `agent.py:930 cancel_current()` | ❌ **不可达**（该路径要求 `is_accepting_input`，THINKING 时消息走排队分支，不派发 USER_INPUT） | 被新回合取代 | — |
| C | **Agent.stop()** | SIGTERM/SIGINT `/shutdown` `/restart` REPL `/quit` | ✅ **可达**（`_stop_cognitive_and_tts` → `cancel_current()`） | shutdown | 置 `run_state=STOPPED`、清 pending、`mark_shutting_down` |
| D | **client disconnect** | `ws_handler._close_ws_connection` → `InterruptData("disconnect")` | ✅ **可达**（其归属守卫读 `_generation_ctx.user_message_id`，而 ctx 正是在窗口内建立的） | 断线（终态 `interrupted`） | 同 A |
| E | timeout | `_turns.drain(timeout=3.0)` 超时后的取消 | ✅ 可达（仅停机路径） | shutdown/预算耗尽 | — |
| F | explicit `cancel_current()` | 6 个生产调用点（见 §6） | 4 个可达（A/A″/C/D 经 F1 调用点；`_handle_task_result` 与 `switch_llm` 直调） | 混合 | 直调点**无**总线/FSM 副作用 |
| F′ | **模型热切换** | 前端模型下拉框 → `switch_model` → `Agent.switch_llm` → `cancel_current()` | ✅ **可达**（本批 TSR-T2 的生产取消源；它**不**清 pending，因此排队消息能在取消后存活） | 被取代 | 无 |
| F″ | 后台任务终态唤醒 | `_poll_mailboxes`(500ms) → `_handle_task_result` → `cancel_current()` | ✅ 可达（`TaskResultData` 投递无忙判定） | 被取代 | 无 |
| G | FSM/状态转换副作用 | — | ❌ `cancel_current()` 不产生任何转换；THINKING→INTERRUPTED 只发生在回合内部 | — | — |
| H | outer task cancellation / shield | — | ❌ 全仓对回合任务**无 shield**；`_turns` 只 spawn+drain | — | — |
| I | test-only | 直接调 `loop.cancel_current()` 的单测 | 已单列（不用于可达性论证） | — | — |

**结论**：至少 4 条**用户可见**的取消源能落在该窗口 ⇒ `CONFIRMED_USER_REACHABLE`。

---

## 8. Pre-Fix Reproduction / Falsification

### 8.1 确定性 E2 时间线（真 Agent，无 sleep 造 race）

原始探针输出（修复前，`core/agent/tests/test_zz_batch10_probe.py`，已删除）：

```
[probe] 挂起中: fsm=thinking lock=True task_a=<Task pending name='cognitive-第一条' …>
[probe] 取消后: fsm=thinking lock=False task_a(cancelled=True done=True) current_task=None
[probe] transitions=['thinking'] pending=[]
[probe] generation row status=None finish_reason=None
[probe] 第二条: accepted=True fsm=thinking pending=['第二条'] current_task=None attempts=1
```

复现步骤（每一步都是结构化信号，`asyncio.sleep` 只用于"让出一次事件循环步"，不作为判据）：

1. `Agent(AppConfig())` + `agent.start()`（真 20 步启动、真 `_process_events` 消费者；存储走 `fake_storage`，出站走假 LLM 后端）。
2. 用屏障替换 `Agent._generation_repo.upsert`（**只**挡第一次调用）：`started` 置位后阻塞在 `release` 上 ⇒ 精确钉死 `:610` 这个挂起点。
3. `await agent.process_message("第一条")` → 真 USER_INPUT → 真 `process()` → **FSM 真的进入过 THINKING**（`on_change` 记录到 `thinking`）。
4. `bus.post(InterruptData(source="websocket"), priority=INTERRUPT)`（**生产停止按钮路径**）→ 真消费者 → `Agent._handle_interrupt` → `loop.cancel_current()`。
5. 等回合任务落定（`gather(..., return_exceptions=True)` + 死锁探测超时）。
6. 观察：`fsm=thinking`、`lock=False`、`task.cancelled()=True`、`loop._current_task=None`、
   `pending=[]`、generation 行**根本不存在**（upsert 被取消在半路）。
7. 再发第二条（真实入口）：`process_message` 返回 **True（已 ACK accepted）**，消息进 `pending`，
   **没有任何回合被启动**（`current_task` 仍是 None）——永久失聪。

### 8.2 "THINKING stranded" 的定义（本批判据）

> 当前回合已无任何合法仍在运行的工作（task 已 cancelled/done、锁已释放、无 current owner），
> FSM 仍保持 THINKING，且现有事件/状态转换无法自行推进；后续用户输入因此无法正常开始新回合。

上表同时观测到五元组：FSM state ✅ / process task ✅ / process lock ✅ / current turn owner ✅ /
pending queue ✅（EventBus 计数亦为 0）。这正是 TSR-4 的**非法组合**。

### 8.3 Falsification attempts（主动证伪，全部失败 ⇒ 确认可达）

| 假设 | 尝试 | 结果 |
|---|---|---|
| "取消被某处 shield 住" | 全仓 shield 清单（desktop bridge / embedding / workflow executor）均不覆盖回合任务 | ❌ 证伪 |
| "外层的 `except` 能接住" | `CancelledError.__mro__` 实测 = `(CancelledError, BaseException, object)`；`:618` 是 `except Exception`、`:719` 是 `except (CharacterBusyError, StorageUnavailableError)` | ❌ 证伪 |
| "生产取消永远不会落在这个窗口" | 4 条用户可见取消源可达（§7），且窗口宽度 = 任意一次 Mongo upsert 的挂起时长 | ❌ 证伪 |
| "`enter_thinking` 自身就有挂起点，窗口更宽" | 实测 `_notify` 只有一个注册者且其回调无 await ⇒ `enter_thinking` 不让出事件循环（本批用例 `test_enter_thinking_itself_has_no_suspension_point` 钉住） | ✅ 成立（窗口**窄于**最坏猜测，但不为空） |
| "process task 不是真正的 current task" | `Agent._handle_user_input` spawn 后即 `self._cognitive.current_task = task`；`cancel_current()` 命中的正是它（探针里 `task.cancelled()=True`） | ❌ 证伪 |
| "EventBus 取消语义不同" | INTERRUPT 只负责 `cancel_current()` + 清 pending；FSM 转换只能由回合内部触发 | ❌ 证伪 |

---

## 9. User Impact

**机制链**（每一环都有实测）：

```
:610 落下的取消逃出 process()
  → FSM 永停 THINKING
  → status.is_accepting_input 恒假（fsm.py:70-72）
  → 之后每条用户消息都走 process_message 的排队分支（agent.py:675-697）
       （v2：先 reserve_work，再 return True ⇒ WS 回 ACK accepted ⇒ 同 clientRequestId 重试永远重放该 ACK）
  → pending 只在 StateChangeData(IDLE) 出队（agent.py:1054-1057），而那个事件再也不会来
  → 没有任何回合再启动：用户看到"消息已发送但 Agent 永远不回"
  → 队列满（默认 32）后 process_message 返回 False ⇒ WS 回 QUEUE_FULL rejected
  → 只有 Agent.start()（fsm.reset）或重启进程能救
```

**实测证据**：探针第 7 步 —— 第二条消息 `accepted=True`、`pending=['第二条']`、
`current_task=None`（无回合启动）、`session_msgs=0`（无任何回复）。

**注意**：显式 INTERRUPT 按既有合同会**清空** pending（`agent.py:872-874`），因此"被取消的
那一条已 ACK 消息"确实被丢弃——这是**既有设计**（用户已取消该输入），不是本批缺陷；
本批缺陷是**之后**的每一条消息都无法推进。本批不修改该合同。

---

## 10. Root Cause

**任务书 §50 的五种可能中，实际是 A + B 的组合**：

- **A（CancelledError 没被 process 捕获）**：取消边界只覆盖 `_think_loop`（`:729`），
  而 `_prepare_turn`（`:715`）在 THINKING 之后还有一个可挂起点 ⇒ 逃逸。
- **B（被捕获但没有 canonical interrupt/finalize）**：不适用（本窗口**根本没有**人到场捕获）。

更深一层：**回合的取消边界与回合的资源边界不一致**。`_process_lock` 从 `:699` 起覆盖整个回合
（含 prepare），但"谁负责把状态推离 THINKING"只挂在 `:729` 之后的 try 上。两者错位的那一段
（`:699`→`:729`）就是缺口。因此修法不是"给 `:610` 包个 try"，而是**让取消边界与锁边界对齐**。

（§50 的 C：cleanup 执行但没有 ownership guard —— 修复时必须同时防住，见 §12/§15；
D：`_prepare_turn` 自身改状态后责任边界模糊 —— 是背景，不是根因。）

---

## 11. Fix

### before

```python
async with self._process_lock:
    if not self.status.accepts_new_turns: return
    try:
        setup = await self._prepare_turn(...)      # ← 里面有 enter_thinking() 与 register()
    except (CharacterBusyError, StorageUnavailableError) as exc:
        await self._handle_turn_exception(exc)     # ← 这个 handler 自身也不在取消边界内
        return
    if setup is None: return
    self._current_task = asyncio.current_task()
    try:
        await self._think_loop(...)                # ← 取消边界只覆盖这里
    except asyncio.CancelledError:
        await self._archive_partial_reply(...); await self._handle_interrupt()
    ...
```

### after

```python
    setup: _TurnSetup | None = None
    try:                                       # ← 取消边界（最外层）
        try:                                   # ← 异常边界
            setup = await self._prepare_turn(...)
        except (CharacterBusyError, StorageUnavailableError) as exc:
            await self._handle_turn_exception(exc)   # 语义不变
            return
        except Exception as exc:               # KB-22 同根的另一半（普通异常同样无主）
            if self._generation_ctx is None:
                raise                          # 回合开始之前：保持既有语义（向外抛）
            await self._handle_turn_exception(exc)
            return
    except asyncio.CancelledError:
        # 入口之前 → 保持取消语义（传播）；入口之后 → canonical 收口
        if not await self._handle_prepare_cancelled():
            raise
        return
    if setup is None: return
    ...（主体不变）
```

```python
async def _handle_prepare_cancelled(self) -> bool:
    if self._generation_ctx is None:      # 本回合还没开始 ⇒ 无状态可收口
        return False
    self._clock = None                    # 回合级计时器引用（与主体 finally 同义）
    await self._handle_interrupt()         # canonical：THINKING → INTERRUPTED → IDLE
    return True
```

**另一处一行级重排（Challenger W1，本批采纳）**：`_prepare_turn` 里 `_generation_ctx` 的
建立从 `enter_thinking()` **之后**挪到**之前**（两者之间没有 await）：

```python
        self._generation_ctx = GenerationContext(...)   # 判据在此建立
        self._turn_failed_emitted = False
        await self.phases.enter_thinking()              # 紧接着同步 transition
```

理由：`enter_thinking()` 内部的 `_notify("thinking")` 是一个 await，而 `on_change` 是**公开**
订阅点（签名 `Callable[[str], Awaitable[None]]`）。只要有订阅者在该 await 上挂起，取消就会落在
"FSM 已 THINKING、ctx 还没建"的缝隙里，旧判据会误判成"本回合没开始"并放任取消逃逸。
重排之后"ctx 非 None"与"FSM 已经/即将是 THINKING"**严格等价**，判据对订阅者行为免疫
（守卫用例 `test_cancel_inside_enter_thinking_notify_still_recovers`，见 §19/§20）。

### why minimal

1. **不移动 `enter_thinking()`**（§52）：THINKING 的语义是"回合已被接受"（§3），挪位置会改动
   UI/打断/观测时机，而根因是取消边界不是入口时机。
2. **不写 `finally: set_idle()`**（§12）：那正是会打掉新回合的形状；收口走 canonical
   `_handle_interrupt()`，并且**只有本回合进过 THINKING 才执行**。
3. **不新增 token/epoch**（§13/§15）：复用既有的 `_generation_ctx`（turn-scoped、入口清空、
   建立于 `enter_thinking()` 下一句）+ `_process_lock` 所有权。仓库里最可靠的现成判据。
4. **不新增 watchdog / 超时复位 / shield**（§30/§84）：稳态零开销，只有取消路径多一个布尔判断。
5. **保留既有语义边界**（§9/§26）：入口之前传播取消、入口之后由回合消费——与 `_think_loop`
   窗口（F9）和 resolve 窗口（F5）的既有合同**逐条一致**，不是新发明的语义。

---

## 12. Ownership / Turn Identity Semantics（本报告重点）

系统里**没有**显式 turn id；审计（§13/§14）得到的候选与取舍：

| 候选 | 是否可用作 cleanup 判据 | 结论 |
|---|---|---|
| `_current_task`（`loop.py`） | 正常完成的回合不清它（只在 `cancel_current`/`reset_lifecycle_state` 清）⇒ 会指向已结束任务 | ❌ 不可单独用（见 §31 新发现） |
| `asyncio.current_task()` 身份 | 只在 `:725` 才写入；prepare 窗口内为 None | ❌ 窗口内不可用 |
| `GenerationContext.generation_id` | 每回合唯一、冻结点在 `:593` | ⚠️ 可用，但需要额外存一份"我的 id" |
| **`_generation_ctx is not None`** | 入口清空（`:489`）、`enter_thinking()` **之前**建立（本批把它从 `:593` 上移，两者之间无 await）；`_prepare_turn` 全程持锁 ⇒ 不可能混入别的回合 | ✅ **采用**（零新增字段、turn-scoped、与 `_handle_interrupt` 消费的终态上下文同源；对"订阅者在 `_notify` 上挂起"免疫） |
| `fsm.state == THINKING` | 分不清"我的 THINKING / 别人的 THINKING"，也分不清"本回合未进入 + 相位残留 IDLE_BACKGROUND_TASK" | ❌ **明令禁止**（§59），并有专门守卫用例 |
| `_process_lock.locked()` | 锁在**本回合自己手里**才是前提（不是判据） | ✅ 作为"不可能有并发回合"的结构保证 |
| `TTSStreamManager._turn_epoch` | TTS 域专用 | ❌ 越域 |

**最终不变量（三条真相必须同时成立）**：

1. `_process_lock` 由 `process()` 全程持有（含 prepare 与收口）⇒ **任何时刻至多一个回合在改 FSM**；
2. THINKING 期间的 owner 是"持锁的那次 `process()` 调用"，其 turn-scoped 证据是
   **本回合建立的 `_generation_ctx`**；
3. 收口只结束**自己**的回合（不按 FSM 取值猜所有权）。

---

## 13. Lock / FSM Consistency

| 组合 | 合法性 | 说明 |
|---|---|---|
| 锁 held + FSM THINKING + owner 活 | ✅ 合法 | 正常 THINKING 窗口 |
| 锁 free + FSM IDLE + owner 无 | ✅ 合法 | 正常空闲 |
| **锁 free + FSM THINKING + owner 无** | ❌ **非法**（修复前的实测形态） | 本批修复目标；`test_cancelled_thinking_turn_recovers_and_next_message_is_processed` 断言其不再出现 |
| 锁 held 永久 + FSM IDLE | ❌ 非法 | 本批未发现（`async with` 保证释放；KB-6 的 SPEAKING 卡死是它的近亲，Batch 9 已修） |

**取消后锁一定释放**（`async with` 的 `__aexit__`），所以"锁释放"**不等于**生命周期完成——
这正是 KB-22 的核心教训（§17）。

---

## 14. Pending Message Recovery

- `_pending_messages.append`：`process_message` 忙时（`agent.py:688-696`）与 pre-start 重排队（`:919-927`）。
- drain：**唯一**在 `_handle_state_change`（`:1054-1057`），条件 = `StateChangeData(IDLE|IDLE_BACKGROUND_TASK)`。
- 修复后，恢复路径把相位推回 IDLE ⇒ `finish_turn`/`handle_interrupt` 的第二段**自然发出**该事件
  ⇒ 既有 drain 机制接管。**未新增任何调度器/轮询**（§36）。
- 生产可达的完整闭环用例（TSR-T2）：
  回合 A 卡在窗口 → 用户**切换模型**（`Agent.switch_llm` → `cancel_current()`，且该路径**不**清
  pending）→ 恢复尚未走完时用户再发一条（已 ACK 进 pending）→ 放行恢复 → IDLE → drain → 新回合处理它。
  修复前：第二步的恢复根本不存在，第三条消息永远留在 pending。

---

## 15. Stale Cleanup / New Turn Race

| 攻击 | 结论 | 证据 |
|---|---|---|
| A cancelled → **B THINKING** → A late cleanup | ✅ **结构上不可达**：A 的收口整体在 `_process_lock` 内完成，B 只能在 A 的 `process()` 返回后才进入 `enter_thinking()` | `test_old_turn_cleanup_cannot_overwrite_newer_turn`（用屏障把 A 卡在收口中间，断言 B 未进入 THINKING、且状态序列无交错） |
| A cancelled → B EXECUTING | ✅ 同上（B 连 THINKING 都进不去，更不可能已 EXECUTING） | 同一条用例 + §6 authority map（所有 FSM 写都在锁内） |
| A cancelled → B SPEAKING | ✅ 同上 | 同上 |
| "用 FSM 取值当 ownership proof" 的实现 | ✅ 会被守卫用例杀死 | `test_cleanup_scoped_by_ownership_not_by_fsm_value`（造出"别人的 THINKING"，cleanup 必须无操作） |
| "入口之前的取消把别人的 IDLE_BACKGROUND_TASK 推成 IDLE" | ✅ 会被守卫用例杀死 | `test_pre_thinking_cancel_preserves_other_turn_background_phase` |

> 本批**没有**多阶段 cleanup（§24：不人为制造）：收口是单阶段且被锁串行化。

---

## 16. Interrupt Semantics

- 既有语义：THINKING → INTERRUPTED → IDLE（`fsm.py:232-248`），并伴随
  `generation.failed{CANCELLED}`（v2）、`generation.mark(cancelled|interrupted)`（source=="disconnect"
  ⇒ interrupted）、desktop 在途调用取消、TTS 清理、`typing(False)`。
- 本批**完全复用**该路径（§26/§55），因此 prepare 窗口的取消与 `_think_loop` 窗口的取消在
  **终态帧、registry 终态、清理动作**上逐项一致（新增用例断言 mark 落位与状态序列）。
- `Task.cancel()` 与 `FSM INTERRUPT` 的关系：**cancel 只负责取消任务**，状态收口由**回合自己**执行
  ——KB-22 的本质就是"状态收口挂在被取消的那段代码后面"。

---

## 17. Shutdown Semantics

- 停机路径：`Agent.stop()` 步骤 2 `_stop_cognitive_and_tts`（`cancel_current()` + `mark_shutting_down`）
  → 步骤 3 `_turns.drain(timeout=3.0)`。
- 修复后：取消落在窗口时，收口仍走 canonical 中断路径 ⇒ 相位回到 **IDLE**；生命周期轴 `run_state`
  已是 **STOPPED**，`process_message` 因此**拒收**（实测 `is False`），不会"复活成可接受输入"。
- 停机**不** reset FSM（只有 `Agent.start()` 会，`lifecycle.py:204`）——本批未改动该语义。
- 新增用例：`test_stop_during_thinking_window_does_not_strand_the_agent`（修复前红）。

---

## 18. Regression Tests

| 分类 | 用例 |
|---|---|
| **PRE_FIX_RED**（10 条，修复前红、修复后绿） | `core/agent/tests/test_thinking_cancel_recovery.py`：`test_cancelled_thinking_turn_recovers_and_next_message_is_processed`（TSR-T1+T2）、`test_message_queued_during_recovery_window_is_processed`（TSR-T2/T5）、`test_stop_during_thinking_window_does_not_strand_the_agent`（TSR-T5）；`core/cognitive/tests/test_thinking_cancellation.py`：`test_cancel_after_thinking_entry_recovers_via_canonical_interrupt`、`test_cancel_during_error_recovery_is_also_covered`、`test_cancel_inside_enter_thinking_notify_still_recovers`（Challenger A3b）、`test_generic_exception_after_turn_started_still_recovers`（Challenger I3b）、`test_cleanup_scoped_by_ownership_not_by_fsm_value`、`test_old_turn_cleanup_cannot_overwrite_newer_turn`、`test_double_cancel_is_idempotent` |
| **ALREADY_GREEN_GUARD**（4 条，修复前后都绿——守"这条链一旦再断"的形状） | `test_normal_turn_is_untouched`（TSR-T4：正常回合状态序列 `thinking→speaking→idle`，无 `interrupted`）；`test_cancel_before_thinking_entry_is_safe`（入口前的取消保持**取消语义**，不改 FSM）；`test_pre_thinking_cancel_preserves_other_turn_background_phase`；`test_enter_thinking_has_no_suspension_point_in_production_wiring`（**只**记录接线事实，不作为判据证明——判据由 A3b 用例独立证明） |
| **FALSIFICATION_GUARD**（防假绿） | 每条 E2 用例先断言 `"thinking"` **确实出现**在真实 `on_change` 记录里（或 `RecordingFSM.entered(THINKING)`），再断言终态；`test_double_cancel_is_idempotent` 断言确切状态序列而不是"最终 IDLE"；`test_old_turn_cleanup_cannot_overwrite_newer_turn` 断言 A 未结束前 B 未进入 THINKING |

---

## 19. Challenger Findings

Challenger（独立子智能体，报告 `/tmp/batch10/D-challenger.md`，24 条临时对抗用例全部通过后自删）
对**当前修订**逐 await 注入、逐场景攻击，结论按类归档：

| 分类 | 内容 |
|---|---|
| **confirmed（本批根因）** | 窗口取消 ⇒ 永久 THINKING。Challenger 独立复现（A 表 / H / B），并实测 `CancelledError.__mro__`（`BaseException`）与 `enter_thinking` 无挂起点两个前提。主 Agent 独立 E2 复现见 §8。 |
| **fix weakness（已采纳）** | **W1（A3b）**：ownership 判据不精确——`on_change` 是公开订阅点，订阅者在 `enter_thinking` 的 `_notify` 上挂起时，"FSM 已 THINKING / ctx 未建"的缝隙让判据误判并放任取消逃逸。**已修**：ctx 建立挪到 `enter_thinking()` 之前（§11），并加守卫用例 `test_cancel_inside_enter_thinking_notify_still_recovers`。<br>**W2（I3/I3b）**：窗口里的**普通异常**（`GenerationContext` 校验 ValueError / op-guard RuntimeError）同样无 owner ⇒ 同样的永久 THINKING。**已修**：异常边界（`except Exception` + 同一 ownership 判据）→ `_handle_turn_exception`，加守卫用例 `test_generic_exception_after_turn_started_still_recovers`。 |
| **fix weakness（未采纳，理由）** | **W3（F1/F2/F2b）+ E2**：收口链被**第二发** `task.cancel()`（停机 `_turns.drain` 3s 超时）打断 ⇒ 相位停在 THINKING/INTERRUPTED。**按 scope 不修**（§90 明令 KB-23 不在本批），已登记为 **KB-25** 并把 TH4 的成立条件写进 `invariants.md`。Challenger 建议的 `asyncio.shield` 收口亦被 §30 限制（且会让 cleanup 逃出锁边界）。 |
| **fake-green（已处理）** | ① `test_enter_thinking_itself_has_no_suspension_point` 把一个**接线事实**当成判据证明（用同步回调证明一般命题）⇒ 已改写为 `..._in_production_wiring` 并显式声明它**不是**判据证明，判据由 A3b 用例独立证明。<br>② `test_stop_during_thinking_window_does_not_strand_the_agent` 只覆盖"收口在 3s 预算内走完"的快路径 ⇒ 已在用例 docstring 与 `invariants.md` 写明 TH4 的成立条件（慢路径 = KB-25）。<br>③ `test_double_cancel_is_idempotent` 只测公开 API 的第二发（F3，本来就无害）⇒ 用例名/docstring 已限定为"公开 API 幂等"，真正的第二发（F1/F2）归 KB-25。 |
| **reclassified（本批之外，但同根已覆盖）** | **I3**（`GenerationContext.__post_init__` 的 ValueError，触发物是数据质量：空 `characterId`）在 W1 重排之后**不再 stranded**（异常发生在 THINKING 之前 ⇒ 相位保持 IDLE），已被 `test_generic_exception_after_turn_started_still_recovers` 同族覆盖。 |
| **accepted risk / currently unreachable** | **C/D**：若相位是**外来的** EXECUTING/SPEAKING 且 ctx 非空，收口会把那个相位推成 INTERRUPTED→IDLE——即 ctx 判据**不保护别人的相位**，保护它的是 `_process_lock`（B 只能经 `enter_thinking` 进入，因而被同一把锁串行化）。Challenger 判定 NOT-REACHABLE（今日接线）；本批记录为设计边界：**ownership 判据 + 锁**共同成立，缺一不可。 |
| **new out-of-scope** | O1（入口前取消丢已 ACK 消息）、O2（`IDLE 已广播 / 锁未释放`窗口里 pending 被抢/丢，需会挂起的订阅者）、O3（`_prepare_turn` 的回合级写入不回滚，`_clock` 残留）、O4（`_handle_turn_exception` 在**入口之前**触发时仍会误伤上一回合的 `IDLE_BACKGROUND_TASK`）→ 全部登记进 §31。 |
| **Challenger 的变异测试自身假绿（如实记录）** | Challenger 首次 M2a 用 `.replace(..., 1)` 命中了文件里**另一处**同形判据（`current_generation_user_message_id` 属性），变异静默落错位置，结果假绿为 "12 passed"；改用完整锚点后得到真实结果（3 red）。该教训写进 §20。 |

---

## 20. Mutation Evidence

独立 worktree（`/tmp/b10-prefix` @ HEAD `d0d54b89` + 本批两个新测试文件 = 14 条用例）内
逐个注入变异（驱动脚本 `/tmp/batch10/mut_driver.py`），每次只跑两个新套件（+ 必要的既有套件）：

| # | 变异 | 结果 | 被杀死的用例 |
|---|---|---|---|
| **M1** | **完全回退修复**（= HEAD 的 `loop.py`） | **10 failed / 4 passed**（14 条新用例） | 全部 10 条 PRE_FIX_RED（3 条 E2 Agent 级 + 7 条循环级）；4 条显式标注的护栏保持绿（SAFE / ALREADY_GREEN 语义，无"无名假绿"） |
| **M2a** | `_handle_prepare_cancelled` **无视 ownership 判据**（ctx 为 None 也调 `_handle_interrupt()` 并返回 True） | **3 failed** | `test_cancel_before_thinking_entry_is_safe`（取消语义被吞）、`test_cleanup_scoped_by_ownership_not_by_fsm_value`（误伤"别人的 THINKING"）、`test_pre_thinking_cancel_preserves_other_turn_background_phase`（`IDLE_BACKGROUND_TASK` 被推成 IDLE） |
| **M3** | 收口改成**非 canonical 的粗暴路径**（`self._fsm.reset()` 直接回 IDLE，绕过 `THINKING→INTERRUPTED→IDLE`） | **8 failed** | 状态序列断言（`states() == ["THINKING","INTERRUPTED","IDLE"]`）×3、Agent 级 E2 ×2、竞态用例（屏障不再被命中）、重复取消用例 |
| **M4** | 删掉 `self._clock = None` | **1 failed**（**加断言前是 12 passed**——如实记录：该行最初无守卫，本批补了 `_clock is None` 断言后才有判别力） | `test_cancel_after_thinking_entry_recovers_via_canonical_interrupt` |
| **M5** | **入口之前的取消也吞掉**（删掉 `raise`，永远 `return`） | **1 failed**（本批套件） **+ 1 failed（既有合同）** | `test_cancel_before_thinking_entry_is_safe` + **既有** `core/cognitive/tests/test_session_resolver.py::test_process_start_clears_stale_generation_ctx`（F5 合同） |
| **M6** | **只回退 ctx 重排**（W1 的修复单独拿掉，其余保留） | **1 failed** | `test_cancel_inside_enter_thinking_notify_still_recovers`（Challenger A3b） |
| **M7** | **只拿掉普通异常边界**（W2 的修复单独拿掉） | **1 failed** | `test_generic_exception_after_turn_started_still_recovers`（Challenger I3b） |
| **M8** | 收口后 `status.set_run_state(RUNNING)`（复活认可态） | **1 failed** | `test_stop_during_thinking_window_does_not_strand_the_agent`（断言 `run_state is STOPPED` + 停机窗口拒收） |
| **M9** | 收口后仍 `raise`（消费取消但重新抛出） | **1 failed** | `test_cancel_after_thinking_entry_recovers_via_canonical_interrupt`（断言 `task.cancelled() is False`） |

M2/M3 正是任务书 §76 要防的两类错修（无 ownership 判据 / 非 canonical 收口），都有一击致命的守卫；
M5 证明"保持既有取消语义"这条边界不是随手写的，而是被**既有**合同（F5）钉住的；
M6/M7 是 Challenger 找到的两个洞的专属守卫（缺哪一半修复就红哪一条）。
**变异测试自身的假绿（Challenger 实测教训）**：用 `.replace(old, new, 1)` 做变异时，若锚点与
文件里另一处同形代码重合，变异会**静默落错位置**（Challenger 首次 M2a 因此假绿为 12 passed）。
本批的变异脚本一律用**完整块锚点 + `assert s.count(old) == 1`**。

---

## 21. Task/Lock Leak Evidence

- 修复前（探针）：回合任务 `cancelled=True done=True`（**非孤儿**）；`_process_lock` **已释放**；
  `loop._current_task = None`；`Agent._started_cognitive_tasks` 由 done-callback 清空；session work
  lease 由 `_lease_release_done` 释放 ⇒ **无任务/锁/租约泄漏**。失聪的成因**不是**泄漏，而是
  **真相不一致**（锁/任务都结束了，FSM 还挂着）。
- 修复后：收口在锁内完成，锁随 `process()` 返回释放；本批所有用例结束时不留下未落定的回合任务
  （`settle()` = `gather` + 死锁探测；`harness.aclose()` 关闭 output）。

---

## 22. Public API / Schema Impact

**零变化**：无契约模型/字段/枚举改动，无新 REST/WS 消息，无 debug 面板字段变化。
`make check-types`（含 17 golden frames）与 `make check-contract` 绿。

---

## 23. Persistence Impact

**无 schema 变化**（无新集合/字段/索引/迁移）。唯一行为差异：取消发生在窗口内时，
恢复会为**那一条** generation 调一次 `GenerationRegistry.mark(cancelled)`（upsert 语义，
`generation_registry.py:104-135` 明确支持 mark-before-register）⇒ 库里**新增一条终态行**，
而不是像修复前那样**什么都不留**（探针实测 `generation row status=None`）。这是**增强可追溯性**，
不是新持久化契约。

---

## 24. Performance Impact

- 稳态：**零开销**（只是把原有的 try 结构多套一层，正常路径不增加任何 await/分配）。
- 取消路径：一次布尔判断 + 一次既有的 `_handle_interrupt()`（本批不新增 I/O）。
- **无** polling / watchdog / heartbeat / 超时复位（§84）。

---

## 25. Commits

| commit | 内容 |
|---|---|
| `f550666c` | `fix(cognitive): recover cancelled thinking lifecycle safely` —— 生产修复（`core/cognitive/loop.py`）+ 14 条守卫用例（10 条 PRE_FIX_RED + 4 条 ALREADY_GREEN_GUARD）同 commit |
| `3a0fed8b` | `docs(architecture): record thinking lifecycle invariants` —— `docs/architecture/invariants.md` 新增 §4.3 TH1–TH5 |
| `ada5cce9` | `docs(review): record Batch 10 thinking state cancellation recovery` —— 本文件 |
| `f94a67eb` | `docs(todo): reclassify KB-22 as GUARDED (Batch 10)` + 登记 KB-24–KB-30 |

基线：`d0d54b89`（Batch 9 结束点）。

---

## 26. Worktree Verification

```bash
git worktree add --detach /tmp/b10-prefix HEAD     # d0d54b89（= Batch 9 结束点，未含本批修复）
cp <本批两个新测试文件> /tmp/b10-prefix/…           # 只带测试，不带修复
```

| 阶段 | 命令 | 结果 |
|---|---|---|
| **pre-fix** | `pytest core/agent/tests/test_thinking_cancel_recovery.py core/cognitive/tests/test_thinking_cancellation.py` | ❌ **8 failed / 4 passed**（失败信息逐条指向 KB-22：`FSM 仍停在 THINKING` / `取消没有进入 canonical 恢复路径` / `屏障未被命中`） |
| **post-fix** | `git apply kb22-fix.patch` 后同一命令（+ `test_session_resolver.py`） | ✅ **23 passed** |
| **变异** | M1–M5（见 §20） | 各有用例红 |

主工作树另做了一次等价内联验证（`git checkout core/cognitive/loop.py` → 跑测试 → `git apply` 恢复），
结果与 worktree 一致（8 failed / 4 passed → 12 passed）。

---

## 27. Test Results

| 项 | 结果 |
|---|---|
| targeted（`core/cognitive/tests` + `core/agent/tests` + `core/status` + `core/input/tests`） | ✅ **1805 passed**（51.9s，`-n 8 --dist loadfile`） |
| 本批新套件 | ✅ `test_thinking_cancellation.py` 10 passed；`test_thinking_cancel_recovery.py` 4 passed；同跑既有 `test_session_resolver.py` 共 **25 passed** |
| **PRE_FIX_RED 属性** | ✅ 独立 worktree（`/tmp/b10-prefix` @ `d0d54b89`）内：回退修复 ⇒ **10 failed / 4 passed**；`git apply` 修复 ⇒ **25 passed**（详见 §20/§26） |
| 变异矩阵 | ✅ M1–M7 各自杀死对应用例（§20），M0 基线 14 passed |
| async order perturbation（§94） | ✅ 确定性交错用例：A 取消→B 启动（锁串行化，状态序列零交错）、恢复窗口内 pending 到达→恢复→drain、停机→取消、取消→重复取消、入口前取消→正常回合。**无 sleep 造 race**（屏障 = Event；唯一 `sleep(0)` 只用于"让出一次事件循环步"） |
| random stress（辅助） | 未做（本批无共享设备/时序敏感资源；确定性交错为主证据，符合 §45） |
| fresh process | **NOT APPLICABLE** —— `CognitiveLoop`/`AgentFSM`/`EventBus`/`GenerationRegistry` 全是实例级对象，无模块级单例、无 import 期状态（用例逐条自建实例；唯一"全局"是测试内的 `fake_storage` 导流） |
| `make test-quick` | ✅ **6184 passed, 2 skipped, 2 xfailed**（78.4s；Batch 9 为 6170 ⇒ +14 = 本批新用例） |
| `make test-normal` | ✅ **6184 passed, 2 skipped, 2 xfailed**（78.5s） |
| `make test-full` | ✅ **6216 passed, 34 skipped, 2 xfailed**（445.8s） |
| frontend / build | ✅ 后端 schema/契约零改动；`cd frontend && npm run build` 退出 0（产物 `static/`，gitignored） |
| static gates（全项） | ✅ `make lint` / `make pyright`（0 errors, 0 warnings）/ `check-arch` / `check-contract` / `check-types`（含错误码单源 + golden frames）/ `check-timeouts` / `check-debug-auth` / `check-secrets` / `check-test-sets`（`|Quick|=|Normal|=6188`，`|remainder|=0`，互斥且并集相等）/ `check-compose` / `check-env-lock` / `check-ci-isolation` 全绿 |
| `make test-real-mongo` | 未本地跑（本批**零 persistence 改动**：无新集合/字段/索引/迁移；CI 自然运行） |
| **real CI** | ⏳ **待 owner push**（§27.1） |

### 27.1 Real CI 状态

本机无 GitHub 凭据（`git push --dry-run` ⇒ `could not read Username for 'https://github.com'`，
与 Batch 4–9 同一环境限制）⇒ 4 个本地提交已就绪（`f550666c` / `3a0fed8b` / `ada5cce9` / `f94a67eb`），
**真实 CI 由 owner push 后触发**。

push 后需核对的 required job（`docs/ci.md`：本仓只有 push 触发器）：

| job | 期望 | 本批相关面 |
|---|---|---|
| `backend` | 绿 | 全部静态门禁 + `test-quick` / `test-normal`（本机等价证据见上表；14 条新用例都带 `service` marker ⇒ 落在 Quick ⊆ Normal） |
| `real-mongo` | 绿 | 零 persistence 改动，未跑该层；CI 自然运行 |
| `frontend` | 绿 | 前端零改动；本机 `npm run build` 退出 0 |
| `contract-frames` | 绿 | 契约层零改动；本机 `make check-types` 绿 |

---

## 28. Invariant Mapping

见 `docs/architecture/invariants.md` §4.3 **TH1–TH5**（本批新增）。

| 不变量 | 守卫 | 套件 |
|---|---|---|
| TH1 每次进入 THINKING 都有 turn-scoped owner | `test_thinking_cancel_recovery.py`（4 例）+ `test_thinking_cancellation.py`（8 例） | `make test-quick` / `test-normal`（CI `push/backend`） |
| TH2 取消不能留下无 owner 的 THINKING（必须走 canonical 边） | 3 条 PRE_FIX_RED（E2）+ 状态序列断言 | 同上 |
| TH3 旧回合 cleanup 不能覆盖新回合 | `test_old_turn_cleanup_cannot_overwrite_newer_turn`、`test_cleanup_scoped_by_ownership_not_by_fsm_value` | 同上 |
| TH4 FSM / 任务所有权 / 锁三者一致 | `test_stop_during_thinking_window_does_not_strand_the_agent`、`test_cancel_before_thinking_entry_is_safe` | 同上 |
| TH5 pending 恢复后经既有 drain 获得处理权 | `test_message_queued_during_recovery_window_is_processed` | 同上 |

---

## 29. Todo Update

`KB-22` → **GUARDED**（commit 见 §25）；新增登记 §31 的 KB-24–KB-30（`NEW_FINDING_OUT_OF_SCOPE`）。
既有条目（KB-23 / KB-18..21 / KB-6 等）**状态一律未动**。

---

## 30. Keep As-Is

- **FSM 架构**：不加状态、不改转换表（本批一次都没碰）。
- **EventBus 优先级**：不动（INTERRUPT=0 插队语义保留）。
- **`_process_lock` 设计**：仍是快速路径而非唯一防线（角色路由 409 语义不变）。
- **pending 消息机制**：不加 scheduler/polling；显式 INTERRUPT 仍按既有合同清空队列。
- **`enter_thinking()` 的位置**：不动（§52）。
- **无 watchdog / 无超时复位 / 无 `finally: set_idle()` / 无新 epoch**。
- **前端零改动**（无 schema 变化）。
- **停机不 reset FSM**：只有 `Agent.start()` reset（既有语义）。

---

## 31. New Findings

只登记，不修（除与 KB-22 同 root 且不可分的两处已在 §11 覆盖）。

| ID（建议） | 发现 | 证据 | 影响 |
|---|---|---|---|
| **KB-24** | **取消落在 `enter_thinking()` 之前**（`:494`/`:507`）时，本回合已 ACK 的消息**静默结束**：没有任何 `generation.started`/`generation.failed` 终态帧，用户看不到任何回应 | 本批用例 `test_cancel_before_thinking_entry_is_safe`（相位不受影响，SAFE）＋§7 分类 | 用户面（消息丢失），但与 KB-22 的"永久失聪"不同族 |
| **KB-25** | **第二发取消**打在收口自身的 await 上（如 `FSMBackedPhaseController.handle_interrupt` 的 `await asyncio.sleep(0.05)`）仍可把相位留在 INTERRUPTED/THINKING ⇒ 同样失聪 | 任务书 §71 场景 + KB-23 同族（停机 drain 的第二发 cancel） | 窄窗口，与 KB-23 同批处理更合适 |
| **KB-26** | `CognitiveLoop._current_task` 在**正常完成的回合**后仍指向已结束任务（只在 `cancel_current()` / `reset_lifecycle_state()` 清） | `loop.py:381-385`、`:407` | 无用户影响（消费方都先问 `.done()`）；但它是"陈旧句柄"型真相债 |
| **KB-27**（OBS） | `_AFTER_RESPONSE_DRAIN_TIMEOUT = 10.0`（`loop.py:54`）作为 `drain_timeout=` 传入，**未登记**在 `scripts/check_timeout_budget.py`，且门禁只匹配关键字 `timeout` ⇒ 该预算对门禁不可见 | Challenger B 的审计 | 门禁盲区（非本批 scope） |
| **KB-28**（O2/J3） | `IDLE 已广播 / 锁未释放`窗口：收口或回合在 post IDLE 之后仍持锁挂起时，pending 出队 spawn 的回合会卡在锁上，而"下一条消息"会把它当在途回合取消 ⇒ 已 ACK 的排队消息被丢弃、FIFO 被抢 | Challenger J3（需会挂起的 `on_change("idle")` 订阅者；仓内今日没有这类订阅者） | 用户面（丢已 ACK 消息），窄前提 |
| **KB-29**（O4） | `_handle_turn_exception` 在**入口之前**触发时（如 `StorageUnavailableError`）会对上一回合遗留的 `IDLE_BACKGROUND_TASK` 调 `_handle_interrupt()` ⇒ `IDLE_BACKGROUND_TASK→INTERRUPTED→IDLE`，即本批刻意避免的"误伤别人相位"由**异常**路径触发 | Challenger O4 | 观测面（相位真相），无失聪 |
| **KB-30**（O3） | 取消在入口之前传播时，`_prepare_turn` 已写入的回合级状态（`_session`/`_prompt`/tool_context/`_tts_mgr.reset_for_turn`/`_clock`）**不回滚**；`_clock` 残留非 None | Challenger O3 | 无用户影响（下一回合重设），真相债 |

---

## 32. Follow-up

保留（本批未动）：KB-23、KB-18/19/20/21、KB-12、KB-13、AR-16（Ollama Provider Adapter）、
KB-15/16/17、OBS-6/7/8、KB-11、GR-25、N-15，以及其余 canonical backlog 条目。
本批新增登记见 §31。
