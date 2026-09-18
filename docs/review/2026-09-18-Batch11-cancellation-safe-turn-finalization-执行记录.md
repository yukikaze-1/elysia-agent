# Batch 11 — Cancellation-Safe Turn Finalization（KB-25 / KB-23）执行记录

> 本文件是 Batch 11 的执行记录。canonical scope = **KB-25 单条** + **KB-23 只读审计与 canonical 归类**。
> 结构按任务书 §94 的 35 节；每一节只写**实测**到的东西，未测的显式写"未做/不适用"。

---

## 1. Executive Summary

| 问题 | 回答 |
|---|---|
| **KB-25 confirmed?** | ✅ **CONFIRMED**（当前 HEAD 独立 E2：真 Agent + 真 `process_message` + 真 EventBus + 真 STOP 按钮 + 真 `_turns.drain`；屏障钉死 6 个挂起点，其中 **5 个** strand） |
| **classification** | 机制 `INTERNAL`；**同一边界上的第一发变体是 `USER`/`HIGH`**（探针 #2：provider 失败收尾时按停止按钮 ⇒ `run_state` 仍 RUNNING 下永久失聪） |
| **evidence** | HEAD 上 E2（探针 ×6 挂起点 + Agent 级 5 例全红）→ 修复后全绿；真库旁证 2 例（其中 1 例修前红） |
| **production fix?** | ✅ 有：收口"逻辑终态"（同步、无 await）放进 `finally`；best-effort 资源清理保持可取消。**3 处最小改动**，无新 task / 无 shield / 无 epoch |
| **user reachability?** | ⚠️ **分两问**：严格意义的"第二发取消"只有停机 drain 一条生产来源 ⇒ 窗口是 `run_state == STOPPED`（**用户不可见**，与 KB-23 同一窗口）；但**同一 logical boundary** 上的"第一发取消落在异常收口"**用户可达且用户可见**（RUNNING 下永久失聪），本批一并修复并给出 E2 |
| **KB-23 relationship?** | ✅ **SAME_ROOT_ALIAS**（同 task / 同函数 / 同第二发来源 / 同窗口 / 同唯一修复点）⇒ canonical 合并，`SUPERSEDED_BY KB-25` |
| **public API?** | 零变化（无契约/字段/枚举改动；`make check-types` / `check-contract` 绿） |
| **persistence?** | 零 schema 变化（无新集合/字段/索引/迁移）。行为差异：第二发取消落在 `mark` 之前时该 generation 行停在 `running`，由**既有**启动期 `mark_stale_interrupted` 兜底 |
| **real CI?** | ⏳ **待 owner push**（本机无 GitHub 凭据，与 Batch 4–10 同一环境限制；§30.1） |

**一句话**：回合的终态收口是一条长 await 链，而 `asyncio.CancelledError` 是 `BaseException`
——沿途的 `except Exception` 与**兄弟 `except` 子句**都接不住它。于是"相位离开活跃区 +
广播 IDLE"这一小段**逻辑终态**此前挂在链尾，任何一发直接 `task.cancel()` 都能把它连同
整个收口一起带走：锁释放了、任务结束了，FSM 却永远宣称"我正在思考"。修复把这**一小段同步
代码**移进 `finally`，其余（出站帧、Mongo 落库、desktop/TTS 清理）**保持可取消**。

---

## 2. Original Backlog Evidence

### 2.1 KB-25（canonical scope）

`todo.md` 原文：

> 收口链（`turn_cancelled → mark → cancel_desktop → typing → _notify → sleep(0.05) → …`）
> 被**第二发直接 `task.cancel()`** 打断 ⇒ 相位停在 THINKING 或 INTERRUPTED（同样失聪）。
> **生产实例**：`Agent.stop()` 的 `_turns.drain(timeout=3.0)` 超时后直接取消任务
> （绕过 `cancel_current()`，one-shot 所以公开 API 的第二发无害，F3）；Challenger 实测
> 「`mark` 卡住 >3s」⇒ `stop elapsed=3.00s fsm=thinking run_state=STOPPED`，只有
> `start()` 能救。与 **KB-23 同族**。

**HEAD 复核（逐条，不继承）**：

| 原登记断言 | HEAD 复核结果 |
|---|---|
| 收口链是一条长 await 链，被第二发取消打断 | ✅ 成立；**AST/运行时双重确认 8 个 await 点**（§5） |
| 生产第二发 = `_turns.drain(timeout=3.0)` 超时后的 `task.cancel()` | ✅ 成立；`AsyncTaskScope.drain`（`Module/_common/task_scope.py:277`），唯一调用点 `core/agent/lifecycle.py:320` |
| "绕过 `cancel_current()` ⇒ 公开 API 的第二发无害" | ✅ 成立：`cancel_current()` 在 `task.cancel()` 后**同步**把 `_current_task` 置 None，第二次调用必然返回 `False` |
| "相位停在 THINKING 或 INTERRUPTED" | ✅ 成立，且**按挂起点二分**：链上前半段（`turn_cancelled`/`mark`/`desktop`/`typing`）⇒ THINKING；`handle_interrupt` 内部（`_notify`/`sleep`）⇒ INTERRUPTED |
| "`mark` 卡住 >3s ⇒ fsm=thinking run_state=STOPPED" | ✅ 复现（本批 §11 的时间线独立重放） |
| "只有 `start()` 能救" | ✅ 成立：仓内唯一 `fsm.reset()` 生产调用点是 `Agent.start()`（`core/agent/lifecycle.py:204`）；`stop()` 不 reset；`AgentFSM.interrupt()`（IDLE-safe）**零生产调用者**；无任何 debug 路由碰 FSM |
| 隐含前提："只有第二发取消会造成这个" | ⚠️ **不成立**：**第一发**取消落在 `_handle_turn_exception` 的 await 上同样逃出（think 路径的 `except Exception` 分支不在任何取消边界内），且那条路径 `run_state == RUNNING` ⇒ **用户可见**（§11.2） |
| 隐含前提："`run_state == STOPPED` ⇒ 用户不可见" | ✅ 成立（`accepts_new_turns = is_service_running ∧ is_accepting_input`，`core/status/protocol.py:106/121`）——**所以 KB-25 的严格形态确实只在停机窗口** |
| Sev "MEDIUM" | ⚠️ **重估**：严格第二发形态 = `INTERNAL`/`LOW`（与 KB-23 同窗口、同不可见性）；同 boundary 的第一发形态 = `USER`/`HIGH`（永久失聪） |

### 2.2 KB-23（只读审计）

`todo.md` 原文（摘要）：

> `Agent.stop()` 的 `_drain_cognitive_turns`（3s 预算）会对已被取消的回合发**第二发**
> `task.cancel()`；若该回合正停在 `_handle_interrupt` 的**无超时**
> `await bridge.pending_calls()`（`loop.py:1240`），第二发取消直接逃出 `_handle_interrupt`
> ⇒ FSM 停在 SPEAKING。**可达性有限**：只存在于「已 STOPPED、未 start」窗口 ⇒ 用户不可见。
> 附带发现：`_CHANNEL_SEND_TIMEOUT=5.0` > drain 预算 3.0；`FSMBackedPhaseController.handle_interrupt()`
> 缺 `is_completely_idle` 守卫；`finish_turn` 12 组合里 5 组抛。

**HEAD 复核**：

| 原登记断言 | HEAD 复核结果 |
|---|---|
| 行号 `loop.py:1240`（`bridge.pending_calls()`） | ❌ **陈旧**：实际在 `core/cognitive/loop.py:1324`（修复前），且它在 helper `_cancel_desktop_calls`（`:1306-1331`）里，`_handle_interrupt` 只是**间接** await 它 |
| `pending_calls()` 是"无上界挂起点" | ⚠️ **过度陈述**：`bridge.pending_calls()` 只在**存在未在途调用**时才有内容（`core/desktop/bridge.py:441` 遍历 `self._pending`）；KB-25 的 `mark`（Mongo upsert）**不需要任何 desktop/Electron**，前提严格更弱 |
| `_CHANNEL_SEND_TIMEOUT = 5.0` 存在且在收口路径上 | ✅ 成立：`Module/output/manager.py:40`，经 `manager.py:159-162` 的 `wait_for` 作用在 `_broadcast` 上；`loop.py:1376 typing(False)` 与 `:1359 turn_cancelled` 都走它 ⇒「一个不读数据的客户端」可让单帧占满 5.0s > 3.0s drain 预算 |
| 缺 `is_completely_idle` 守卫 | ✅ 成立且**本批已补**（`fsm.py` 的 IDLE 早退）：修复前 `IDLE → INTERRUPTED` 抛 `ValueError: Invalid transition`；生产调用点都在 `loop._handle_interrupt` 的守卫之后，故**当前不可达**，属 defense-in-depth |
| `finish_turn` 12 组合 5 组抛 | ✅ 算术成立（本批独立按 `VALID_TRANSITIONS` 重算：bg=True 抛 IDLE/EXECUTING/INTERRUPTED；bg=False 抛 EXECUTING/IDLE_BACKGROUND_TASK）；且**任何 gated 套件都没钉它**（12 组合矩阵只存在于 `tmp/b9_fsm/`，而 `pytest.ini` 的 `norecursedirs` 排除 `tmp`）⇒ 属**独立**发现，已登记新 finding |

---

## 3. Cancellation Contract

| | **第一次取消** | **第二次取消** |
|---|---|---|
| 语义 | "结束当前 active turn" | "调用者仍然希望这个 task 尽快结束" |
| 谁发 | `CognitiveLoop.cancel_current(source)`（6 个生产调用点）/ 直接 `task.cancel()` | **只有** `AsyncTaskScope.drain` 预算耗尽后的 `task.cancel()` |
| owner | 被取消的那次 `process()` 调用（持 `_process_lock` 者） | 无新 owner——它是**同一个** task 上的又一发 |
| 期望结果 | 逻辑终态必须成立（FSM 离开活跃区 + 广播 IDLE）；资源清理尽量完成 | 逻辑终态**仍须**成立；资源清理**允许**被放弃；取消意图**仍须**传播 |
| 实现口径 | `except asyncio.CancelledError` → `_archive_partial_reply` → `_handle_interrupt()` | 同一段代码：`finally` 保证逻辑终态，CancelledError 继续向外传播 |

**要求（本批的判据，不是"最终还能回 IDLE"）**：

> 一旦某个 turn 已经开始 terminal finalization，后续**任意多发** cancellation 都不能阻止
> 它达到一个稳定、逻辑一致的 terminal lifecycle state；同时 cancellation 不能被永久吞掉，
> 停机不能被变成不可终止。

**必须区分的一点**：`cancelling()` 计的是**请求数**、不是投递数——实测三连发在同一 await 上
只投递**一次** `CancelledError`（§15）；而"落在不同 await 上"的发数会各自投递一次。
判据因此建立在**屏障（哪个 await）**上，不建立在 `cancelling()` 上（§32：`cancelling()`
只作诊断，不作业务 authority——全仓唯一消费者是 `Module/_common/task_scope.py:68` 的
`cancellation_is_external`）。

---

## 4. Turn Finalization Lifecycle Map

```text
第一发取消（生产源：WS 停止按钮 / 断线 / 语音取消 / 模型切换 / 停机 / 后台任务唤醒）
   ↓  task.cancel()
某处 await 抛 CancelledError
   ↓
process() 的取消/异常 handler
   ├─（prepare 窗口）_handle_prepare_cancelled() → _handle_interrupt()          [Batch 10]
   ├─（think 窗口）  _archive_partial_reply()（**同步函数体**）→ _handle_interrupt()
   └─（异常路径）    _handle_turn_exception() ──────────────────────────────┐
                                                                            │
_handle_interrupt()                                                          │
   ├─ (try)  output.turn_cancelled(ctx)           ← await ①（出站帧 ~5s 上界）│
   │        generation_registry.mark(status)      ← await ②（Mongo，**无上界**）│
   │        _cancel_desktop_calls() → pending_calls()/cancel_call()          │
   │                                              ← await ③（桥 RPC，无上界） │
   │        _tts_mgr.cancel_all()（同步）                                    │
   │        output.typing(False)                  ← await ④                  │
   └─ (finally) phases.handle_interrupt()                                    │
                  ├─ fsm.transition(INTERRUPTED) + bus.post   ← 同步（第一段终态）│
                  ├─ (try) _notify("interrupted")  ← await ⑤                 │
                  │        asyncio.sleep(0.05)     ← await ⑥（UI 可观测延迟）  │
                  └─ (finally) fsm.transition(IDLE) + bus.post(IDLE)  ★★★    │
                              ← **同步：逻辑终态在此成立 + pending drain 触发点**│
                              _notify("idle")      ← await ⑦                 │
   ↓                                                                         │
process() 的 finally：_clock = None + runtime.end_generation()（同步）        │
   ↓                                                                         │
async with self._process_lock 的 __aexit__ ⇒ **锁释放**                       │
   ↓                                                                         │
task 以 cancelled 结束（CancelledError 继续传播）                              │
   ↓                                                                         │
Agent._handle_state_change 收到 STATE_CHANGE(IDLE) ⇒ pending 出队 → 新回合 ←──┘
```

**修复前后的差别**：`★★★` 之前的所有 await（①–⑦）此前都**没有任何人**保证 `★★★` 会执行；
现在 `_handle_interrupt` 的 `finally`（覆盖 ①–④）与 `handle_interrupt` 的 `finally`（覆盖 ⑤⑥）
把 `★★★` 变成**不可跳过**的一步。

---

## 5. Finalization Suspension Map

修复前的真实 await 清单（`conda run -n Elysia` = Python 3.12.13；行号为修复前 HEAD）：

| Finalization step | Await? | Logical terminality critical? | Resource cleanup only? | Second cancel possible? | Current handler（修复前） |
|---|---:|---:|---:|---:|---|
| `output.turn_cancelled(ctx)`（`:1359`） | ✅ | ❌ | ✅ 出站终态帧（best-effort，`generation.failed{CANCELLED}`） | ✅ | `try/except Exception` ⇒ 接不住 CancelledError |
| `generation_registry.mark(status)`（`:1368`） | ✅ | ❌（**runtime truth 不依赖它**；§17/§18） | ✅ persisted 终态（best-effort） | ✅ | 同上 |
| `_cancel_desktop_calls()`（`:1373`）→ `bridge.pending_calls()`（`:1324`）/ `cancel_call()`（`:1329`） | ✅ | ❌ | ✅ best-effort | ✅ | helper 内 `try/except Exception` ⇒ 接不住 |
| `output.typing(False)`（`:1376`） | ✅ | ❌ | ✅ UI 指示器（best-effort） | ✅ | **无 try** ⇒ 普通异常也会逃出（CF4 的另一半） |
| `_tts_mgr.cancel_all()`（`:1375`） | ❌（同步） | ❌ | ✅ best-effort | n/a | — |
| `fsm.transition(INTERRUPTED)` + `bus.post`（`fsm.py:236-240`） | ❌（同步） | ✅ **是**（第一段） | — | ❌（同步不可打断） | — |
| `_status._notify("interrupted")`（`fsm.py:241`） | ✅ | ❌ | observability | ✅ | **无 try** ⇒ 逃出 |
| `asyncio.sleep(0.05)`（`fsm.py:242`） | ✅ | ❌ | UI 可观测延迟 | ✅ | **无 try** ⇒ 逃出 |
| `fsm.transition(IDLE)` + `bus.post(IDLE)`（`fsm.py:243-247`） | ❌（同步） | ✅ **★ 逻辑终态** | — | ❌ | 挂在链尾，**前面任一 await 被打断就到不了这里** |
| `_status._notify("idle")`（`fsm.py:248`） | ✅ | ❌ | observability | ✅ | 无 try |
| `process()` 的 `finally`（`_clock=None` / `runtime.end_generation()`） | ❌（同步） | ✅（租约） | — | ❌ | `finally` 已保证 |
| `_think_loop` 的异常清理 `typing(False)`（`:1110`） | ✅ | ❌ | ✅ | ✅ | 在 `process()` 的 try **之内** ⇒ 取消会被 process 捕获（既有正确形态） |

**窗口边界（回答"第二次 cancel 从哪里来/取消谁"）**：

- **从哪里来**：生产只有一条 —— `Agent.stop()` 第 3 步 `_drain_cognitive_turns` →
  `AsyncTaskScope.drain(timeout=3.0)` 预算耗尽 → `task.cancel()`。其余 6 个取消入口全部
  经 `cancel_current()`，而它是 one-shot（`_current_task` 同步置 None）⇒ 结构上不可能发第二发。
- **取消谁**：**同一个** `cognitive-*` 任务（`Agent._turns` 持有的那个，也就是
  `Agent._handle_user_input` spawn 的 `_run_with_session_lease()` 协程）。
- **哪些 side effect 在第二发之前已完成**：取决于落点（`turn_cancelled` 帧在 ① 之后才发出；
  `mark` 在 ② 之前未写；TTS/desktop 未清理）——本批的逐点矩阵见 §21。
- **哪些没完成**：`★★★` 之前的所有剩余步骤（这正是缺陷）。

---

## 6. Logical Terminality Boundary

**判据（一句话）**：`FSMBackedPhaseController.handle_interrupt()` 的 `finally` 里那两行
**同步**代码执行完毕之后，runtime truth 已经稳定：

```python
self._fsm.transition(AgentState.IDLE)          # 相位离开活跃区
self._bus.post(StateChangeData(IDLE), STATE_CHANGE)   # pending drain 的唯一触发点
```

- **成立条件**：这两行之间与之前**没有 await** ⇒ 从进入 `finally` 到 `★★★` 完成之间，
  事件循环不会切换任务 ⇒ **任何**后续 `task.cancel()` 都无法插进来。
- **可观测判据**：`status.is_completely_idle is True`、`fsm.states()` 末尾是 `IDLE`、
  `bus` 上恰好一条 `IDLE`。
- **为什么不是"整个 `handle_interrupt` 完成"**：`_notify("idle")`（⑦）在其后，是
  observability；它被取消不影响任何消费方（唯一订阅者 `wait_until_idle` 的生产调用点
  只有 `Module/bench/bench.py`，且它带超时）。
- **为什么不是"整个 `_handle_interrupt` 完成"**：①–④ 全是无上界的外部 I/O
  （Mongo/桥 RPC）——把它们放进不可取消区就等于让"用户按停止"失去意义，
  也违反 CF6（停机上界）。

---

## 7. Resource Cleanup Boundary

| 步骤 | 分类 | 被第二发取消后的结果 | 兜底 |
|---|---|---|---|
| `output.turn_cancelled` | BEST_EFFORT | 客户端可能收不到 `generation.failed{CANCELLED}`（只会看到 state=idle） | 下一次握手/回放给的是 state 真相 |
| `generation_registry.mark` | BEST_EFFORT（persisted truth） | 该行停在 `running` | 启动期 `mark_stale_interrupted(cutoff)`（既有） |
| `_cancel_desktop_calls` | BEST_EFFORT | 在途 desktop 调用不被逐条 cancel | `DesktopCallRepository.mark_stale_expired`（启动期，既有） |
| `_tts_mgr.cancel_all()` | BEST_EFFORT（同步，不会被跳过） | — | — |
| `output.typing(False)` | BEST_EFFORT | 指示器可能留在"正在输入" | 下一次 `typing()`/disconnect 重置 |
| `_notify("*")` | observability | 少一次通知 | 消费方都有超时 |

**允许放弃它们**的依据（§43）：逻辑终态已稳定 ⇒ 允许资源 cleanup 被放弃；
这也符合 SPEAKING 域既有取舍（Batch 9 的 SP4：不加超时把"卡死"换成"说谎"）。

---

## 8. Authority Map

| 事项 | 唯一 owner | 位置 / 机制 | 本批是否改动 |
|---|---|---|---|
| **turn** | 持 `_process_lock` 的那次 `CognitiveLoop.process()` | `loop.py:747` `async with` | ❌ |
| **FSM phase** | `FSMBackedPhaseController`（唯一写 FSM 的层） | `core/status/fsm.py` | ✅ 只改 `handle_interrupt` 的内部结构 |
| **收口流程** | `CognitiveLoop._handle_interrupt()` / `_handle_turn_exception()` | `loop.py:1346` / `:688` | ✅ 逻辑终态进 `finally` |
| **generation（runtime）** | `_generation_ctx`（turn-scoped；入口清空、`enter_thinking()` 前建立） | `loop.py:489` / `:598` | ❌ |
| **generation（persisted）** | `GenerationRegistry.mark`（upsert，Mongo 离线 no-op） | `core/cognitive/generation_registry.py` | ❌ |
| **lock** | `process()` 的 `async with`（取消也经 `__aexit__` 释放） | `loop.py:747` | ❌ |
| **process task** | `Agent._handle_user_input` spawn + `_lease_release_done` | `core/agent/agent.py:964/992/1030` | ❌ |
| **cancel（第一发）** | `CognitiveLoop.cancel_current(source)`（one-shot） | `loop.py:364-385` | ❌ |
| **cancel（第二发）** | `AsyncTaskScope.drain` 超时后的 `task.cancel()` | `Module/_common/task_scope.py:277` | ❌（明确**不**在这里加 `cancelling()` 判据，见 §14） |
| **pending drain** | `Agent._handle_state_change`（`STATE_CHANGE → IDLE/IDLE_BACKGROUND_TASK`） | `core/agent/agent.py:1054-1057` | ❌ |
| **shutdown 上界** | `Agent.stop()` 的 `_SHUTDOWN_STEPS` + `AsyncTaskScope` 预算 | `core/agent/lifecycle.py` | ❌ |

> **换句话说的 owner 归属**（§14 要求的那一句）：
> **`CognitiveLoop.process` 的这一次调用（持 `_process_lock` 者）拥有"释放锁之前的逻辑终态收口"**；
> `_handle_interrupt` 是它调用的流程，`FSMBackedPhaseController` 是它唯一的相位写入口。

---

## 9. Production Cancellation Sources

| # | 来源 | 入口（用户/运维动作） | 第一发可达 | 第二发可达 | 用户可达 | 仅停机 |
|---|---|---|---|---|---|---|
| A | WS 停止按钮 | `{"type":"interrupt"}` → `InterruptData("websocket")` → `Agent._handle_interrupt` → `cancel_current` | ✅ | ❌（one-shot） | ✅ | ❌ |
| A′ | VAD 打断 | `audio_buffer.do_interrupt` | ✅（THINKING/EXECUTING 被抑制） | ❌ | ✅ | ❌ |
| A″ | 语音会话取消 | `voice_sessions.cancel` → `InterruptData("voice_cancel")` | ✅ | ❌ | ✅ | ❌ |
| B | 新用户输入打断 | `agent.py:930` `cancel_current()`（真在跑时） | ✅ | ❌ | ✅ | ❌ |
| C | 客户端断线 | `ws_handler._close_ws_connection` → `InterruptData("disconnect")` | ✅ | ❌ | ✅ | ❌ |
| D | 主动搭话 | `IdleMonitor` → `SystemTickData` → `cancel_current()` | ✅ | ❌ | ✅（静默触发） | ❌ |
| E | 后台任务终态唤醒 | mailbox 轮询 → `_handle_task_result` → `cancel_current()` | ✅ | ❌ | ✅（间接） | ❌ |
| F | 模型热切换 | `switch_model` → `AgentServer.switch_llm` → `cancel_current()` | ✅ | ❌ | ✅ | ❌ |
| G | `Agent.stop()` 第 2 步 | `_stop_cognitive_and_tts` → `cancel_current()`（SIGTERM/SIGINT/`/shutdown`/`/restart`） | ✅ | ❌ | ✅ | ✅（`run_state=STOPPED`） |
| H | **`Agent.stop()` 第 3 步** | `_drain_cognitive_turns` → `_turns.drain(timeout=3.0)` 超时 → **`task.cancel()`** | — | ✅ **唯一** | ⚠️ 触发动作用户可见，**后果不可见**（STOPPED 窗口） | ✅ |
| I | 重复 `Agent.stop()`（同实例） | `start()` 回滚路径（非默认 `rebuild_on_restart=False`）/ bench / API | — | ✅（第 3/4 发，同一 `drain`） | ❌（非生产默认路径） | ✅ |
| J | test-only 显式第二发 | 本批测试 | — | ✅ | — | — |

**结论**：`SECOND_CANCEL_PRODUCTION_REACHABLE = YES`（H，且 I 可叠加多发），但**用户可见性**
只在 H 的触发动作层面成立、**后果不可见**（`accepts_new_turns` 在 STOPPED 时为假）。
**没有**发现任何 `run_state == RUNNING` 下的第二发来源（独立子智能体 A 逐条复核后同样的结论）。

### 9.1 同一边界上"用户可见"的形态（本批 Phase 1 新发现）

| 形态 | 第一发来源 | 落点 | `run_state` | 用户可见？ |
|---|---|---|---|---|
| **KB-25 严格形态** | H（第二发） | 收口链任意 await | STOPPED | ❌（后果不可见） |
| **K1（本批新发现）** | A（停止按钮，**第一发**） | `_handle_turn_exception` 的 `turn_failed` / `mark` await | **RUNNING** | ✅ **永久失聪** |

K1 的完整 E2：provider 失败 → 回合进入异常收口 → 用户按停止按钮 → 取消从 `except Exception`
块里逃出（**兄弟 `except asyncio.CancelledError` 不接**）→ 相位永停 THINKING →
第二条 `accepted=True` 但**永不处理**（`pending=['第二条']`）。探针原始输出见 §11.2。

---

## 10. KB-23 Canonical Analysis

独立子智能体 C（只读审计，逐条读代码 + 必要时实测）的结论是 **SAME_ROOT_ALIAS**。

| Assertion | Result |
|---|---|
| same process task? | ✅ 同一个 `CognitiveLoop.process` 回合任务（`_turns.spawn` → `loop.py:809` 登记 → 第一发 `cancel_current()`） |
| same `_handle_interrupt`? | ✅ 同一个函数；KB-23 的挂起点在它**间接** await 的 helper 里，KB-25 的在它的循环体里 |
| same suspension point? | ❌ **不同 await，同一条链**：KB-23 = `_cancel_desktop_calls()` → `bridge.pending_calls()`；KB-25 = `turn_cancelled` / `mark` / `typing` / `_notify` / `sleep(0.05)` |
| same second cancel source? | ✅ `AsyncTaskScope.drain`（`task_scope.py:277`）← `Agent.stop()`（`lifecycle.py:320`），全仓唯一 |
| same invariant? | ✅ 实质同一条：**"回合终态收口必须对重复取消免疫"**；登记面不同（KB-23 记在 SP1/SP2，KB-25 记在 TH4） |
| same fix? | ✅ 一个 fix：保证 `handle_interrupt` 到达 `transition(IDLE)`。任何"只给 `pending_calls()` 加超时"的局部修**都只覆盖 KB-23**（KB-25 的 `mark` 不需要 desktop 桥） |
| **canonical classification** | ✅ **SAME_ROOT_ALIAS ⇒ `SUPERSEDED_BY KB-25`**（KB-23 是 KB-25 在停机/desktop 桥上的窄化实例） |

**附带发现的三项处置**：

1. `_CHANNEL_SEND_TIMEOUT=5.0` > drain 预算 3.0：**不动生产常量**；登记 follow-up（超时预算
   门禁对**裸字面量**看不见，正是 `check_timeout_budget.py` 的自陈盲区）。
2. `handle_interrupt` 缺 `is_completely_idle` 守卫：**本批已补**（幂等 + 避开非法边；§14）。
3. `finish_turn` 12 组合 5 组抛且零门禁覆盖：**独立发现**，登记新 finding，不在本批修。

---

## 11. Pre-Fix Reproduction（deterministic E2）

### 11.1 KB-25：第二发取消 × 6 个挂起点（真 Agent，HEAD）

探针 `/tmp/b11/probe_kb25.py`（真 `Agent.start()`、真 `process_message`、真 INTERRUPT
事件、真 `_turns.drain(timeout=0)`；屏障 = `asyncio.Event`，无 sleep 造 race）：

| C2 落点 | C2 前 FSM | 终局 FSM | 状态序列 | 任务 | 稳定终态 |
|---|---|---|---|---|---|
| `output.turn_cancelled` | thinking | **thinking** | `['thinking']` | cancelled | ❌ **STRANDED** |
| `generation_registry.mark` | thinking | **thinking** | `['thinking']` | cancelled | ❌ **STRANDED**（且库里那行停在 `running`） |
| `output.typing(False)` | thinking | **thinking** | `['thinking']` | cancelled | ❌ **STRANDED** |
| `_notify("interrupted")` | interrupted | **interrupted** | `['thinking','interrupted']` | cancelled | ❌ **STRANDED** |
| `sleep(0.05)` | interrupted | **interrupted** | `['thinking','interrupted']` | cancelled | ❌ **STRANDED** |
| `_notify("idle")`（**逻辑终态之后**） | idle | idle | `['thinking','interrupted','idle']` | cancelled | ✅ 稳定 |

原始输出（节选）：

```
[probe] C1 后、收口链挂起中 point=turn_cancelled: fsm=thinking lock=True turn_done=False cancelling=1
[probe] C2（_turns.drain(timeout=0)）leftover=1 cancelling=2
[probe] 终局: fsm=thinking states=['thinking'] lock=False turn_cancelled=True current_task=None generation_status=running
[probe] 稳定逻辑终态: NO — STRANDED
```

**这张表同时划出了临界区的两条边界**：左边界 = `_handle_interrupt` 的第一个 await，
右边界 = `handle_interrupt` 里的 `transition(IDLE)`。修复只保护两者之间那**一小段同步代码**。

### 11.2 K1：第一发取消 × 异常收口（真 Agent，`run_state == RUNNING`）

探针 `/tmp/b11/probe_exc_window.py`（LLM 流抛普通异常；屏障钉在 `output.turn_failed`）：

```
[probe] 异常收口挂起中 point=turn_failed: fsm=thinking lock=True turn_done=False run_state=RunState.RUNNING
[probe] 取消投递后: fsm=thinking cancelling=1 turn_done=True
[probe] 终局: fsm=thinking states=['thinking'] lock=False turn_cancelled=True run_state=RunState.RUNNING
[probe] 第二条 accepted=True 被处理=False pending=['第二条']
[probe] 稳定逻辑终态: NO — STRANDED
```

`point=registry_mark` 同样 STRANDED。**这就是"永久失聪"的用户面形态**。

---

## 12. User Impact

**受影响的用户可见行为**（K1，`run_state == RUNNING`）：

```text
回合因 provider 失败 / 内部错误进入异常收口
  → 用户按停止按钮（或 VAD/断线/语音取消/切模型）
  → 取消从 except Exception 块里逃出 process()
  → FSM 永停 THINKING
  → status.is_accepting_input 恒假
  → 之后每条消息都走 process_message 的排队分支：v2 先 reserve_work 再 return True
    ⇒ WS 回 ACK accepted（客户端以为在跑）
  → pending 只在 STATE_CHANGE → IDLE 出队，而那个事件再也不会来
  → 队列满（默认 32）后转 QUEUE_FULL rejected
  → 只有 Agent.start()（fsm.reset()）或重启能救
```

**KB-25 严格形态的用户影响 = 无**（STOPPED 窗口里 `accepts_new_turns` 本就为假；
下次 `start()` 的 `fsm.reset()` 会清掉）。**但残留的可观测面**（与 KB-23 对称、非本批引入）：

- `/debug` 的相位面板在停机后仍显示 `thinking/interrupted`（真相不一致）；
- 停机尾部 aiohttp 仍在监听（`Agent.stop()` → `runner.cleanup()` 之间），此时若仍有连接，
  `should_suppress_vad_interrupt`（THINKING/EXECUTING）与 `is_speaking`（SPEAKING）
  会基于陈旧相位给出错误的打断判定。

---

## 13. Root Cause

任务书 §50 的五种可能中，实际是 **A'（取消落在"终态收口的中间")** 与 **C（cleanup 已执行但没有
ownership 收口）** 的组合，且**根因是结构性的**：

> **"逻辑终态"与"资源清理"被写在同一条 await 链上，而链上每一个 await 都可被取消。**

- **A**（`CancelledError` 没被捕获）：不适用——第一发取消**被**捕获了，收口**开始了**。
- **B**（被捕获但没有 canonical interrupt）：不适用——canonical 路径**被调用**了。
- **C**：✅ 收口执行到一半被打断，`★★★` 没发生。
- **D**（ownership 模糊）：✅ 背景成因——"谁保证 `★★★`"此前只由**调用顺序**保证
  （谁写在最后谁负责），没有任何机制。`_handle_turn_exception` 的异常路径连"写在最后"
  都没有（它在 `except` 块里抛出的取消不会被兄弟 `except` 接住）。
- **E**（根本没有 handler）：K1 成立（think 路径的 `except Exception` 分支不在任何取消边界内）。

**一句话根因**：`asyncio.CancelledError` 是 `BaseException`，因此"**把同步的终态落位挂在
一串可取消的 await 之后**"这种写法没有任何机制保证；修复必须把"必须发生的那一步"
换成 `finally`（结构性保证），而不是靠调用顺序。

---

## 14. Fix

### before

```python
# core/status/fsm.py
async def handle_interrupt(self) -> None:
    self._fsm.transition(AgentState.INTERRUPTED); self._bus.post(...)   # 同步
    await self._status._notify("interrupted")     # ← 可取消
    await asyncio.sleep(0.05)                     # ← 可取消
    self._fsm.transition(AgentState.IDLE); self._bus.post(...)   # ★ 挂在链尾
    await self._status._notify("idle")            # ← 可取消

# core/cognitive/loop.py
async def _handle_interrupt(self, source=None):
    ...
    if self._generation_completed:
        ...; await self._output.typing(False)
        await self.phases.handle_interrupt()      # ← 前面任一 await 被打断就到不了
        return
    if self._generation_ctx is not None and not self._turn_failed_emitted:
        try: await self._output.turn_cancelled(...)      # ← 可取消
        except Exception: ...                            #   接不住 CancelledError
        if self._generation_registry is not None:
            try: await self._generation_registry.mark(...)  # ← 可取消
            except Exception: ...
    await self._cancel_desktop_calls()            # ← 可取消
    ...
    await self._output.typing(False)              # ← 可取消（且无 try）
    await self.phases.handle_interrupt()
```

### after

```python
# core/status/fsm.py
async def handle_interrupt(self) -> None:
    if self._fsm.state == AgentState.IDLE:
        return                                    # 幂等 + 避开 IDLE→INTERRUPTED 非法边
    self._fsm.transition(AgentState.INTERRUPTED); self._bus.post(...)
    try:
        await self._status._notify("interrupted")
        await asyncio.sleep(0.05)
    finally:
        if self._fsm.state != AgentState.IDLE:    # ★ 同步、无 await、不可被打断
            self._fsm.transition(AgentState.IDLE); self._bus.post(...)
        await self._status._notify("idle")

# core/cognitive/loop.py
async def _handle_interrupt(self, source=None):
    ...
    try:
        ...（best-effort：帧 / mark / desktop / TTS / typing）
    finally:
        await self.phases.handle_interrupt()      # 幂等 ⇒ 正常走完与被打断后补做同一段代码

async def _handle_turn_exception(self, e):
    try:
        ...（分类 + turn_failed + mark）
    finally:
        await self._handle_interrupt()
```

### why minimal

1. **零新增 task / 零 shield**：临界区是**同步代码**，不需要 shield，也就不存在
   detached inner task、孤儿、异常无人收集的问题（§29/§30 的七个问题一个都不适用）。
2. **零新增状态/epoch/字段**：复用既有 `FSMBackedPhaseController` 与
   `_handle_interrupt` 的 ownership，不加 `_finalization_started` 之类的旗标。
3. **零重排**：best-effort 段的**顺序逐字不变**（帧 → mark → desktop → TTS → typing），
   所以 wire 帧顺序、50ms INTERRUPTED 可观测窗口、`generation.failed` 与 state 帧的相对
   次序**全部保持**（§34：不为"漂亮"重排）。
4. **零稳态开销**：正常路径只多一次 `if self._fsm.state != IDLE`（真）与一次
   `if state == IDLE`（假）；无额外 await、无分配。
5. **幂等性是必要的而非装饰**：`_handle_interrupt` 的 `finally` 与
   `handle_interrupt` 自己的 `finally` **会叠加**（取消落在 `_notify("interrupted")` 时，
   内层先落 IDLE，外层再调一次 `handle_interrupt`）——IDLE 早退让第二次调用变成 no-op，
   否则会广播出**第二组** terminal 事件（§50）。

### 为什么拒绝另外两种候选

| 候选 | 拒绝理由 |
|---|---|
| `asyncio.shield(整个 _handle_interrupt())` | ① 会让清理**逃出** `process()` 的 `async with _process_lock`（旧回合 cleanup 与新回合交错 ⇒ 破坏 CF5/TH3）；② shield 的 inner task 无人 await 时是 detached 清理任务（§30）；③ 包含无上界外部 I/O（§28 明令禁止） |
| 在 `AsyncTaskScope.drain` 里加 `if task.cancelling() == 0: task.cancel()` | ① §32：`Task.cancelling()` 不得成为业务 authority；② §9：那等于把**第二发取消当成无效**，违反 CF3；③ 它只"不发第二发"，不修 K1（第一发落在异常收口）；④ 会让停机预算变成软预算 |
| retry-until-clean（吞掉 CancelledError 重试 cleanup） | §7 明令禁止：任务会变成不可取消 |
| `uncancel()` | 全仓零使用（独立审计确认）；引入它必须解释 cancellation counter 语义并证明不吞 caller 取消——本修法不需要 |

---

## 15. Repeated Cancellation Semantics

在**本机 Python 3.12.13** 上实测（`conda run -n Elysia`，独立子智能体 A 的原始输出）：

| 问题 | 实测结论 |
|---|---|
| 第一发取消后 task 进入 cleanup await，第二发 `task.cancel()` 会不会在 cleanup 的 await 再抛一次？ | ✅ **会**。`inner await RAISED CancelledError (2nd delivery); cancelling()=2` —— cleanup 余下部分被中止 |
| `finally` 里的 await 呢？ | ✅ **照样会跑起来**（不是"进入即再抛"）：`ENTERED finally; cancelling()=1` → `finally await RAISED CancelledError; cancelling()=2` |
| 同一 await 上连发两发？ | ✅ 只投递**一次**：`TOTAL CancelledError deliveries observed in coroutine: 1`，且之后的 await **正常返回**（`post-catch await returned normally`） |
| `cancelling()` 语义 | 计**请求数**（0→1→2→3），不是投递数 |
| `CancelledError.__mro__` | `(CancelledError, BaseException, object)`；`issubclass(CancelledError, Exception) == False` |
| `uncancel()` 使用 | ❌ 全仓零调用（唯一出现是 `task_scope.py:61` 的 docstring 说明） |
| `cancelling()` 作为 authority | ❌ 全仓唯一消费者是 `Module/_common/task_scope.py:68`（`cancellation_is_external`），回合/收口路径**从不**读它 |

**推论（修复设计直接依赖）**：①`finally` 是有效的保护手段（其 await 会真的执行）；
②同一 await 上的连发不会"追加"第二次投递 ⇒ 不会出现"保护段自身被无限打断"；
③第三发若落在 `finally` 里的 await 上，仍会打断那个 await——所以**真正的临界区必须是同步的**
（本批就是这么做的：`transition(IDLE)` + `bus.post` 之间零 await）。

---

## 16. Cancellation Propagation

| 场景 | 修复后 task 终局 | 判据 |
|---|---|---|
| 取消落在 `turn_cancelled`/`mark`/`typing`/`desktop`（C2） | `cancelled() is True` | `test_second_cancel_during_*` 的 `expect_cancelled=True` |
| 取消落在 `_notify("interrupted")` / `sleep(0.05)`（C2） | `cancelled() is True` | 同上 |
| 三连发（C1+C2+C3） | `cancelled() is True` | `test_triple_cancel_is_still_bounded` |
| 取消落在**逻辑终态之后**（`_notify("idle")`） | `cancelled() is True` | `test_second_cancel_after_terminal_transition_keeps_terminal_state` |
| 单发取消（Batch 10 语义） | `cancelled() is False`（由 canonical owner 消费） | `test_single_cancel_is_unchanged`（ALREADY_GREEN_GUARD） |
| 第一发取消落在异常收口 | `cancelled() is True` | `test_cancel_during_error_finalization_settles`（不断言 cancelled，断言相位） |

**没有吞掉**：修复只在 `finally` 里**增加**工作，从不捕获 `CancelledError`；
变体 M5（在 `finally` 前 `except CancelledError: pass`）**杀死 6 条用例**。

---

## 17. Runtime vs Persisted Terminal Truth

**优先级（本批维持既有合同）**：runtime terminal truth（FSM/锁/任务）**不依赖**持久化成功。

| 层 | 权威 | 第二发取消后 |
|---|---|---|
| runtime 相位 | `AgentFSM`（经 `FSMBackedPhaseController`） | ✅ **必然**落到 IDLE（同步临界区） |
| runtime ownership | `_process_lock` + task 终局 | ✅ 锁释放、任务落定 |
| persisted generation | `generations` 集合的 `status` | ⚠️ **best-effort**：落在 `mark` 之前则停在 `running` |
| 兜底 | 启动期 `GenerationRegistry.mark_stale_interrupted(cutoff)`（`_lifecycle_persistence.py:173/575`） | ✅ 把残留 `running` 扫成 `interrupted` |

**实测**：真库旁证 `tests/real_mongo/test_real_finalization_cancellation.py` 显式断言这条分支
（`status == "running"` ⇒ 调兜底清扫并断言变 `interrupted`）；修前该用例红（相位停 THINKING）。

**为什么 `mark` 不进临界区**：它是无上界的 Mongo upsert（`generation_repo.py` 零
`wait_for`/`timeout=`；唯一"界"是 server-selection 3s，`socketTimeoutMS` 默认 `None`）。
把它放进不可取消区就等于让用户在 MongoDB 卡顿时**无法停止** Agent——违反 §18 与 CF6。

---

## 18. Pending Recovery

- pending 入队：`process_message` 忙时（`agent.py:688-696`）与 pre-start 重排队。
- drain：**唯一**在 `Agent._handle_state_change`（`agent.py:1054-1057`），条件
  `StateChangeData(IDLE|IDLE_BACKGROUND_TASK)`。
- 修复后：`★★★`（同步）**保证**该事件被广播 ⇒ 既有 drain 机制接管。**未新增任何
  调度器/轮询/心跳**。
- 用例：
  - `test_pending_message_is_processed_after_second_cancel`（Agent 级，PRE_FIX_RED）：
    收口挂着时第二条已 ACK 进 pending → 第二发取消 → 放行 → 第二条**真的被处理**
    （状态序列 `['thinking','interrupted','idle','thinking',…]`，pending 清空，
    会话里出现"第二条"）。
  - `test_old_turn_finalization_does_not_interleave_with_new_turn`（CF5）：
    A 的 `interrupted/idle` 严格早于 B 的 `thinking`（B 在锁上等，不交错）。

---

## 19. Stale Cleanup / New Turn

**成立依据是 `_process_lock`，且本批把它变成"必须用测试证明"的断言**（§66）：

- 收口**整体**在 `process()` 的 `async with self._process_lock` 之内完成（含 `finally`）
  ⇒ B 不可能在 A 释放锁之前进入 `enter_thinking()`；
- pending 出队 spawn 的 B 会**卡在锁上**，不是"跑起来再被覆盖"；
- `test_old_turn_finalization_does_not_interleave_with_new_turn` 断言完整序列
  `["thinking","interrupted","idle","thinking","speaking","idle"]`——A 的两段收口与
  B 的启动之间零交错；**修前该用例红**（B 永远不出现）。

**明确否掉的其它形态**：旧 cleanup 不可能 IDLE 新回合（A 的 `finally` 只在 A 的
`_process_lock` 内运行）；不可能 mark 新 generation（`_generation_ctx` turn-scoped，
A 的收口只读 A 的 ctx）。

---

## 20. Shutdown Semantics

| 断言 | 修复后 | 证据 |
|---|---|---|
| 停机仍在既有预算内结束 | ✅ | `test_shutdown_second_cancel_keeps_shutdown_bounded`：真实 `Agent.stop()` + 真实 3s drain 预算 + `mark` 卡住 ⇒ `elapsed < 2×(3.0+1.0)` |
| `run_state == STOPPED` | ✅ | 同上 |
| 相位离开活跃区（不再无主） | ✅ | 同上（修前 `transitions=['thinking']`，FSM 停在 thinking） |
| pending 不因收口广播的 IDLE 启动新回合 | ✅ | 同上：`not a.pending_messages` + `process_message(...) is False` |
| 停机不 reset FSM（既有语义） | ✅ 未改动 | 仓内唯一 `fsm.reset()` 调用点仍是 `Agent.start()` |
| 收口保护的临界区不含无界 I/O | ✅ | 临界区是 4 行同步代码（`transition` + `bus.post`） |

**CF6 的机制**：修复**没有**让任何等待变长——第二发取消仍立刻打断 best-effort 段；
新增的只是一段同步代码。因此"停机上界"这一性质**逐字保持**。

---

## 21. Regression Tests

新增 23 条（21 条 gated + 2 条 real-mongo），分布：

### PRE_FIX_RED（15 条；修复前红、修复后绿）

`core/cognitive/tests/test_cancellation_safe_finalization.py`（10 条）：

| 用例 | 挂起点 |
|---|---|
| `test_second_cancel_during_turn_cancelled_frame_settles` | `output.turn_cancelled` |
| `test_second_cancel_during_registry_mark_settles` | `generation_registry.mark` |
| `test_second_cancel_during_typing_cleanup_settles` | `output.typing(False)`（第 2 次命中） |
| `test_second_cancel_during_desktop_cleanup_settles` | `bridge.pending_calls()`（KB-23 的点） |
| `test_second_cancel_during_interrupted_notify_settles` | `_notify("interrupted")` |
| `test_second_cancel_during_interrupt_delay_settles` | `sleep(0.05)` |
| `test_triple_cancel_is_still_bounded` | 三连发 |
| `test_cancel_during_error_finalization_settles` | `_handle_turn_exception`（think 路径） |
| `test_ordinary_exception_in_cleanup_still_settles_state` | CF4：清理抛**普通异常** |
| `test_handle_interrupt_is_idempotent` | §24/§25：重复调用不得抛/不得重复广播 |

`core/agent/tests/test_turn_finalization_cancellation_safety.py`（5 条，全部真实入口）：

| 用例 | 判据 |
|---|---|
| `test_second_cancel_during_finalization_recovers` | CF-T1：相位离场 + 锁释放 + 状态序列 + `cancelled()` |
| `test_pending_message_is_processed_after_second_cancel` | CF-T2：pending 恢复处理权 |
| `test_stop_button_during_error_finalization_does_not_deafen_agent` | CF-T1 的**用户可见**形态（K1） |
| `test_shutdown_second_cancel_keeps_shutdown_bounded` | CF-T5/CF6：停机有界 + 回合派发轴无第二个回合 |
| `test_old_turn_finalization_does_not_interleave_with_new_turn` | CF-T4/CF5：零交错 |

### ALREADY_GREEN_GUARD（5 条；修复前后都绿——守"这条链一旦再断"的形状）

| 用例 | 钉住什么 |
|---|---|
| `test_second_cancel_after_terminal_transition_keeps_terminal_state` | 临界区**右边界**：越过 `transition(IDLE)` 后取消只影响资源 |
| `test_single_cancel_is_unchanged` | Batch 10 的单发语义逐项不变（含 `cancelled() is False`） |
| `test_normal_turn_is_untouched` | 正常回合序列 `["THINKING","SPEAKING","IDLE"]`，无 `interrupted` |
| `test_normal_error_finalization_is_untouched` | 无取消的异常收口仍 mark `failed` |
| `test_desktop_cleanup_completes_without_second_cancel` | 单发取消下 desktop 清理**真的完成**（offline 跳过） |

### CHALLENGER_ADOPTED（1 条；修复后新增，覆盖 `finally` 里的 `if state != IDLE` 守卫）

| 用例 | 钉住什么 |
|---|---|
| `test_terminal_settle_does_not_duplicate_idle_when_phase_already_forced` | §50：相位已被别的写入者强制成 IDLE 时，收口**不得**再广播一次 terminal event（Challenger J/M5 指出的零覆盖行，见 §22.2 W1） |

### FALSIFICATION_GUARD（防假绿，§46/§38）

- **每条 second-cancel 用例先证明前提**：屏障被命中（`Gate.wait_started` 超时即断言失败）、
  第一发取消**确已送达**（`task.cancelling() == 1`）、第二发**确已送达**
  （`cancelling() == 2`）、回合**确定处于 THINKING**（钉在 LLM 流上）；
- **断言完整状态序列**（`["THINKING","INTERRUPTED","IDLE"]`）而不是 `final != THINKING`；
  并断言 `fsm.rejected() == []`（不允许出现被拒的转换）、`bus` 上 interrupted/idle **各恰一条**
  （§50 duplicate guard）、`notified == ["thinking","interrupted","idle"]`（不重不漏）；
- **零 sleep 造 race**：全部用 `asyncio.Event` 屏障；`wait_for` 只作死锁探测（§78/§79）。

### real-mongo 旁证（2 条）

`tests/real_mongo/test_real_finalization_cancellation.py`：
`test_second_cancel_keeps_runtime_terminal_truth_with_real_mongo`（**修前红**）、
`test_normal_turn_persists_completed_with_real_mongo`（绿）。

---

## 22. Challenger Findings

Challenger = 独立子智能体（对抗性复核；在自己 scratch 的 `/tmp/b11-challenger/` 下建 21 条
攻击用例 + 修复前 worktree `pre-fix @ f62e2468` + 逐场景 trace 对比；**零**改动仓内文件）。
它的总判：**修复的核心主张顶住了它构造的全部生产形态攻击**（CF1–CF4 的"逻辑终态"部分、
无 task 泄漏、取消未被吞、停机有界、turn-scoping、可观测行为逐字节不变），
但找出 **4 条窄口径/当前不可达的弱点** 与 **6 条测试/文档口径问题**。

### 22.1 confirmed（本批根因；Challenger 独立复现）

| 攻击 | 判定 | 原始证据 |
|---|---|---|
| **A** shield / task 泄漏 | ✅ HELD | `[A] leaked=set() never_retrieved=[] pending_count=1 fsm=idle states=['THINKING','INTERRUPTED','IDLE']`；`-W error::RuntimeWarning` 下 21 例全绿 |
| **B** 第 3/4 发取消（含 finally 内部） | ✅ HELD | `[B] bus=['thinking','interrupted','idle'] cancelled=True lock=False`（C2+C3 落在 finally 的 `_notify("idle")`）；`[B3] cancelling=3`；`[B4] cancelling=5` ⇒ 均 FSM=IDLE、事件各 1 次 |
| **D** 可观测行为改变 | ✅ HELD（**已证明**） | 5 场景 trace（正常/单发取消/`_generation_completed`/异常/取消打在 typing）修复前后 `diff: IDENTICAL`；`[D] marks=[('thinking',0.0005),('interrupted',0.0266),('idle',0.0768)]` ⇒ 50.2ms INTERRUPTED 窗口与帧顺序逐字保持 |
| **E** 停机有界 | ✅ HELD（更强） | `[E] elapsed=3.06s state=STOPPED turn_done=True cancelled=True fsm=idle`，且 upsert 闸门**永不打开**（比仓内用例更严：仓内用例在 `stop()` 返回后才 open） |
| **F** 陈旧收口 / 新回合 | ✅ HELD（TH3/CF5） | `[F-1] lock_held_by_A=True b_spawned=True b_blocked_on_lock=True b_done=False` + `names.count("thinking")==1` ⇒ B 无法在 A 释放锁前进入 THINKING（**不是假设，是实测**） |
| **G** 取消被吞 | ✅ HELD | `[G-sync] fired=1 cancelling=2 cancelled=True`：取消注入在 finally 的**同步段**（`transition(IDLE)`）协程仍跑完、任务仍被判 cancelled |
| **I** `handle_interrupt` 幂等 | ✅ HELD（口径限 IDLE 入口） | 6 个入口相位逐个连调两次：首个调用后即 IDLE、第二次全部 no-op；修复前同一处 `ValueError: Invalid transition: idle → interrupted` |
| **J** 新测试判别力 | ✅ 基本有 | 修复前 `15 failed, 5 passed`（过的正是 5 条 ALREADY_GREEN 守卫）；独立变异 M1（loop 修/fsm 退）`4 failed`、M2（fsm 修/loop 退）`11 failed`、M3（`finally: fsm.reset()` 绕过 canonical 边）`16 failed` |

### 22.2 fix weakness（**已采纳**，见 §23 的 M5′）

| # | 弱点 | 处置 |
|---|---|---|
| **W1（J/M5）** | **新 `finally` 里的 `if self._fsm.state != AgentState.IDLE:` 零覆盖**：删掉它 20 例**全绿**（legal path 上 `INTERRUPTED → IDLE` 永远是"需要"的那次转换） | ✅ **已补守卫**：`test_terminal_settle_does_not_duplicate_idle_when_phase_already_forced`（病态订阅者在 `interrupted` 通知里 `fsm.reset()` 强推 IDLE ⇒ 收口**不得**再广播 IDLE）。实测：该变异现在 **1 failed**（§23 M5′） |
| **W2（J3）** | `test_shutdown_second_cancel_keeps_shutdown_bounded` 的 `assert not pending_messages` 对 CF6 后半句**零判别力**（把 `phases.handle_interrupt` 换成 no-op 也成立，因为 `Agent.stop()` 入口同步清空 pending、且 `_SHUTDOWN_STEPS[0]` 就是 `stop_event_task`） | ✅ **已改判据**：改为**回合派发轴**——`rec.names == ["thinking","interrupted","idle"]`（无第二个回合）+ `_current_task is None`；并把 `invariants.md` CF6 的措辞从"不得启动新回合"改成"不得产生新的回合生命周期"（说它真能证明的） |
| **W3（docstring 大于断言）** | `test_handle_interrupt_is_idempotent` 声称"任意重复调用"幂等（只测了 IDLE 入口）；`test_second_cancel_during_registry_mark_settles` 声称"库里停在 running"（取消后无 persisted 断言） | ✅ **docstring 已按可证范围重写**（前者显式限定为"收口完成后再调一次"，并把并发口径指向 `_process_lock`；后者改为"mark 被调用过但已被取消，persisted 是 best-effort"，指向真库用例） |
| **W4（文档口径）** | `invariants.md` CF1 写"15 例：逐 await 注入第二发"，实际只有 **7 例**是第二发注入 | ✅ **已改**为"16 例：**7 例**逐 await 注入第二发 …… 另 9 例是异常收口、普通异常清理、幂等与 5 条 ALREADY_GREEN 守卫" |

### 22.3 fix weakness（**不采纳**，理由 + 已登记）

| # | 弱点（Challenger 原文口径） | 可达性（Challenger 自评） | 本批立场 |
|---|---|---|---|
| **H** | "必然落到 IDLE"以 `transition`/`bus.post`/`_notify` 不抛为条件；finally 内异常会**顶替**在飞的 `CancelledError`（任务从 cancelled 变 failed，`[H-notify] cancelled=False exc=ValueError`）；且 `handle_interrupt` 的 `try` 起点在 `bus.post(INTERRUPTED)` **之后** | **不可达**（`_notify` 逐订阅者吞 `Exception`、`bus.post` 是无界队列 `put_nowait`、护栏保证 `transition(IDLE)` 合法） | **不写防御代码**（§84：不为不可达路径加复杂度）；写进 `invariants.md` §4.4 已知边界，并留一句"将来在 finally 里新增可能抛异常的语句时必须重新评估" |
| **C（notify 语义）** | `finally` 里 `await _notify("idle")` 可被取消 ⇒ **其后**的订阅者收不到通知（`_notify` 逐个 await、`CancelledError` 不是 `Exception`）；`wait_until_idle` 若排在会挂起的订阅者之后会在已 IDLE 时等到 timeout | **不可达**（仓内唯一 `on_change` 注册者回调无 await，且 `bench` 侧带超时） | 登记为已知边界（`_notify` 的**顺序 best-effort** 语义是既有性质，非本批引入） |
| **C/J-interrupted** | 两个**并发** owner 同时进入 ⇒ `interrupted` 广播 ×2、`idle` 通知 ×2（幂等只在"IDLE 入口"成立） | **不可达**（唯一调用者是持 `_process_lock` 的回合任务） | 登记 + 在 `invariants.md` 写明"若将来引入第二个调用者，必须先把判据从 'IDLE 早退' 升级为'完整 terminal 状态判据'" |
| **F-2（O2 落地）** | 带挂起订阅者时，"finally 里已广播 IDLE 但仍持锁"的窗口 + 再按停止 ⇒ 卡在锁上、已 ACK 已出队的 B 被取消并静默丢失 | **需挂起订阅者**（仓内无） | **既有边界（KB-28/O2，非本批引入）**；本批把"在第二发取消路径上也成立"这一句补进 `invariants.md` 的 O2 条目与 KB-28 行 |

### 22.4 fake-green（已处理 / 已如实记录）

| # | 内容 | 处置 |
|---|---|---|
| ① | W2 的停机断言（见 22.2） | ✅ 已修 |
| ② | `assert_stable_terminal` 的 `notified == [...]` 结构上测不到"**后来**注册的订阅者"漏收 idle（harness 记录器最先注册） | 如实记录：该断言证明的是"**收口路径本身**发出了 idle 通知"（这正是修复新增的行为），不是"通知对所有订阅者可靠"；后者是 22.3 C 的已知边界 |
| ③ | W3 的两条 docstring | ✅ 已按可证范围重写 |
| ④ | 真库用例 Challenger 侧 **INCONCLUSIVE**（未起容器） | 主 Agent 已实测：`make test-real-mongo` **24 passed**，且该文件在修复前 worktree 里**1 failed**（§30.2） |

### 22.5 reclassified

- Challenger 的 **M3 变体**（`finally: fsm.reset()` 绕过 canonical 边）杀死 **16 例** ⇒ 说明
  本批套件对"用非 canonical 路径强行回 IDLE"这一类错修**有强判别力**（与 Batch 10 的 M3 同型）。

### 22.6 accepted risk / currently unreachable

- 22.3 的四项（H / C-notify / 并发双 owner / O2）**全部**需要"仓内不存在"的前提
  （不可达异常、会挂起的订阅者、第二个收口调用者）。本批**不为它们增加代码**，
  但**全部写进** `invariants.md` §4.4 的已知边界，并在 §34 登记。

### 22.7 new out-of-scope

见 §34 的 **KB-32 / KB-33 / KB-34**（均由本批 Phase 1 的三条独立审计产出，
Challenger 侧的新发现已并入 22.3）。

---

## 23. Mutation Evidence

独立 worktree `/tmp/b11-mut`（detached @ `f62e2468` + 只提交本批**测试**、不含修复），
驱动脚本 `/tmp/b11/mut_driver.py` + `/tmp/b11/mut_m34.py`（每处变异都用**完整块锚点 +
`assert count == 1`**，正是 Batch 10 §20 记下的那条教训）：

| # | 变异 | 结果 | 被杀死的关键用例 |
|---|---|---|---|
| **M0** | 基线（修复后） | ✅ 20 passed | — |
| **M1** | **完全回退修复**（= HEAD 生产代码） | ❌ **15 failed** | 15 条 PRE_FIX_RED 全部（5 条 ALREADY_GREEN 保持绿） |
| **M2** | 去掉 **fsm 层** `finally`（逻辑终态退回 try 之后） | ❌ **3 failed** | `interrupt_delay`、`interrupted_notify`、`triple_cancel` |
| **M3** | 去掉 **loop 层** `finally` | ❌ **9 failed** | `turn_cancelled`、`registry_mark`、`typing`、`desktop`、`ordinary_exception` + 4 条 Agent 级 |
| **M4** | 去掉 `_handle_turn_exception` 的 `finally` | ❌ **2 failed** | `cancel_during_error_finalization`、`stop_button_during_error_finalization` |
| **M5** | **吞掉取消**（`except CancelledError: pass`） | ❌ **6 failed** | 5 条 `expect_cancelled=True` + 停机用例 |
| **M6** | 逻辑终态**不发 IDLE 广播** | ❌ **12 failed** | pending 恢复、交错、desktop 合同、幂等 + 全部序列断言 |
| **M7** | 去掉 `handle_interrupt` 的**幂等守卫** | ❌ **1 failed** | `test_handle_interrupt_is_idempotent`（`IDLE→INTERRUPTED` 非法边） |
| **M5′** | 去掉 **`finally` 里的 `if state != IDLE` 守卫**（Challenger J/M5 指出的零覆盖行） | ❌ **1 failed**（补测后） | `test_terminal_settle_does_not_duplicate_idle_when_phase_already_forced` —— **补测前该变异 20 passed（零覆盖），补测后被杀死** |

**读法**：M2 与 M3 分别对应"两层保护各自不可替代"——少了任何一层，**不同**的挂起点就会漏；
M4 证明"异常收口"不是同一条链的自然延伸（必须单独给 `finally`）；
M5/M6/M7/M5′ 分别钉住"不吞取消 / 广播是逻辑终态的一部分 / 幂等是必需的 /
逻辑终态不得重复广播"。
**没有**为了凑数做无关变异（§71）；M5′ 是 Challenger 复核后**新增**的（§22.2 W1）。

---

## 24. Task Leak / Detached Cleanup Evidence

- **本批零新增 task**：修复只用 `try/finally`，没有 `create_task`、没有 `shield`、
  没有 `gather`。`make check-arch` 的 R6（裸 `create_task` 逐点登记）**计数未变**（19 点 / 22 处）。
- 用例结束时无未落定的回合任务：每条都 `await gather(task, return_exceptions=True)`
  并带死锁探测超时；`AsyncTaskScope._observe` 会把未处理异常记 ERROR（本批套件运行期间
  **零** "Task exception was never retrieved" / "coroutine was never awaited" 告警）。
- M5 变异反向证明：如果实现改成吞取消，任务会以**正常完成**收场（6 条用例红）——
  说明"保护"没有被实现成 detached 清理。

---

## 25. Public API / Schema Impact

**零变化**：无契约模型/字段/枚举改动，无新 WS/REST 消息，无 debug 面板字段变化，
无公开属性/方法签名变化（`_handle_interrupt` / `handle_interrupt` 的**签名与语义**不变，
只是内部结构变了）。
`make check-types`（含 17 golden frames）与 `make check-contract` 绿。

唯一"接口面"变化是 `FSMBackedPhaseController.handle_interrupt()` 新增了 IDLE 早退——
这是**收紧**（原来抛 `ValueError`），不改变任何生产调用方的行为（生产调用点都在
`loop._handle_interrupt` 的守卫之后）。

---

## 26. Persistence Impact

**无 schema 变化**（无新集合/字段/索引/迁移）。
行为差异只有一处：第二发取消落在 `mark` **之前**时，那条 generation 行停在 `running`
（修复前同样如此；修复**没有**改变这一点，也**故意不**把它变成同步义务）。
既有兜底：启动期 `mark_stale_interrupted(cutoff)`。
真库旁证见 §17/§30。

---

## 27. Performance / Timing Impact

| 面 | 影响 |
|---|---|
| 正常路径 | **零额外 await / 零分配**；只多两次布尔判断（`state == IDLE` / `state != IDLE`） |
| 取消路径 | 多一次 `finally` 进入（同步）⇒ 纳秒级 |
| 停机 | **无变化**（临界区不含 I/O，不延长任何等待） |
| 新增轮询/看门狗/心跳 | **无**（§75 明令禁止） |
| 新增超时预算 | **无**（因此 `scripts/check_timeout_budget.py` 无需登记新条目；`make check-timeouts` 绿） |

---

## 28. Commits

| commit | 内容 |
|---|---|
| `b56a8442` | `fix(cognitive): make turn finalization cancellation-safe` —— 生产修复（`core/cognitive/loop.py` + `core/status/fsm.py`）+ 22 条守卫用例同 commit |
| （Challenger 采纳） | `test(cognitive): harden finalization guards per challenger review` —— W1（`finally` 里 `if state != IDLE` 守卫的判别用例）、W2（停机判据改为回合派发轴）、W3（两条 docstring 按可证范围重写） |
| （本文件） | `docs(review): record Batch 11 cancellation-safe turn finalization` |
| —— | `docs(architecture): record cancellation-safe finalization invariants`（§4.4 CF1–CF6） |
| —— | `docs(todo): reclassify KB-25 as GUARDED, KB-23 as SUPERSEDED_BY KB-25`（+ 登记 KB-31–KB-37、KB-28 补证） |

基线：`f62e2468`（Batch 10 结束点）。

---

## 29. Worktree Verification

```bash
git worktree add --detach /tmp/b11-prefix HEAD          # f62e2468（不含本批修复）
cp <本批两个套件> /tmp/b11-prefix/…                      # 只带测试，不带修复
```

| 阶段 | 命令 | 结果 |
|---|---|---|
| **pre-fix** | `pytest core/agent/tests/test_turn_finalization_cancellation_safety.py core/cognitive/tests/test_cancellation_safe_finalization.py` | ❌ **15 failed / 5 passed**（失败信息逐条指向同一句：`收口被打断后相位留在活跃区` / `相位停在 THINKING` / `pending 消息未被推进` / `IDLE→INTERRUPTED 非法边`） |
| **post-fix** | `git apply /tmp/b11/kb25-fix.patch` 后同一命令 | ✅ **20 passed**（Challenger 采纳后的补充守卫使主树为 21） |
| **real-mongo pre-fix** | 同一 worktree + `make test-real-mongo` 的 2 条 | ❌ **1 failed / 1 passed**（`真库写入路径下相位留在活跃区：thinking`） |
| **变异** | M0–M7（§23） | 各被不同守卫杀死 |

主工作树另做了一次等价验证（修复前后各跑一遍整套新用例），结果与 worktree 一致。

---

## 30. Test Results

| 项 | 结果 |
|---|---|
| targeted（`core/cognitive/tests` + `core/agent/tests` + `core/status` + `core/monitoring`，`-n 8 --dist loadfile`） | ✅ **929 passed**（46.9s；修复当时的口径，含本批 20 条） |
| 本批新套件 | ✅ `test_cancellation_safe_finalization.py` **16 passed**；`test_turn_finalization_cancellation_safety.py` 5 passed（合计 21） |
| **PRE_FIX_RED 属性** | ✅ 独立 worktree 内：回退修复 ⇒ **15 failed / 5 passed**；`git apply` 修复 ⇒ **20 passed**（§29） |
| 变异矩阵 | ✅ M0–M7 + M5′（§23） |
| async ordering（§44 timing matrix） | ✅ 6 个挂起点 × 第二发（含三连发、逻辑终态之后）；全部屏障驱动、零 sleep 造 race |
| random stress（§45，辅助） | **未做**——本批无共享设备/时序敏感资源；判据是"屏障钉死的确定性时序"，随机化不会增加信息量（如实记录，不冒充证据） |
| fresh process（§80） | **NOT APPLICABLE** —— `AgentFSM` / `EventBus` / `CognitiveLoop` / `GenerationRegistry` 全是实例级对象，无模块级单例、无 import 期状态；用例逐条自建实例 |
| `make test-quick` | ✅ **6205 passed, 2 skipped, 2 xfailed**（79.6s；Batch 10 为 6184 ⇒ +21 = 本批新用例） |
| `make test-normal` | ✅ **6205 passed, 2 skipped, 2 xfailed**（79.6s，与 Quick 同集） |
| `make test-full` | ✅ **6237 passed, 36 skipped, 2 xfailed**（454.7s；Batch 10 为 6216 ⇒ +21） |
| `make test-remainder` | ✅ 0 条（`6274 deselected`，退出码 5 = pytest "no tests ran"，是本仓**既有**且文档化的状态：`docs/testing.md` 第 29 行） |
| `make test-real-mongo`（真容器 `mongo:8` @ 127.0.0.1:27018） | ✅ **24 passed**（22 条既有 + 本批 2 条）；见 §30.2 |
| frontend / build | ✅ 后端零契约改动；`cd frontend && npm run build` 退出 0（产物 `static/`，gitignored） |
| static gates（全项） | ✅ `make lint`（ruff 全绿）/ `make pyright`（**0 errors, 0 warnings**）/ `check-arch` / `check-contract` / `check-types`（含 golden frames）/ `check-timeouts` / `check-debug-auth` / `check-secrets` / `check-test-sets` / `check-compose` / `check-env-lock` / `check-ci-isolation` 全绿 |
| **real CI** | ⏳ **待 owner push**（§30.1） |

### 30.1 Real CI 状态

本机无 GitHub 凭据（与 Batch 4–10 同一环境限制）⇒ 本批提交已就绪，**真实 CI 由 owner push 后触发**。
push 后需核对的 required job（`docs/ci.md`：本仓只有 push 触发器）：

| job | 期望 | 本批相关面 |
|---|---|---|
| `backend` | 绿 | 全部静态门禁 + `test-quick` / `test-normal`（本机等价证据见上表；20 条新用例都带 `service` marker ⇒ 落在 Quick ⊆ Normal） |
| `real-mongo` | 绿 | 新增 2 条真库用例（`real_mongo` + `external` marker，只在该 job 跑） |
| `frontend` | 绿 | 前端零改动；本机 `npm run build` 退出 0 |
| `contract-frames` | 绿 | 契约层零改动；本机 `make check-types` 绿 |

### 30.2 real-mongo 明细

```
$ docker compose -f docker-compose.test.yml up -d      # mongo:8 @ 127.0.0.1:27018
$ make test-real-mongo
```

| 用例 | 结果 |
|---|---|
| `test_second_cancel_keeps_runtime_terminal_truth_with_real_mongo` | ✅（**修前红**，见 §29） |
| `test_normal_turn_persists_completed_with_real_mongo` | ✅ |
| 该层其余既有用例 | ✅ 全绿 |

---

## 31. Invariant Mapping

见 `docs/architecture/invariants.md` **§4.4 CF1–CF6**。

| 不变量 | 守卫 | 套件 |
|---|---|---|
| CF1 收口已开始 ⇒ 重复取消不阻止稳定终态 | 15 条 PRE_FIX_RED（10 loop + 5 Agent） | `make test-quick` / `test-normal`（CI `push/backend`） |
| CF2 runtime terminal truth 先于 ownership 释放 | 每条用例的序列 + 锁断言 | 同上 |
| CF3 保护不得吞掉取消意图 | `expect_cancelled=True` 断言 + M5 变异 | 同上 |
| CF4 资源清理不得阻塞逻辑终态 | `test_ordinary_exception_in_cleanup_still_settles_state`、desktop 清理配对用例 | 同上 |
| CF5 收口仍 turn-scoped | 交错用例 + pending 恢复用例 | 同上 |
| CF6 停机仍有界、不启动新回合 | 停机用例（真实 3s 预算） | 同上 |

同时**解除**了 TH4 的附带限定（"收口能在 3s 内走完时成立"）——已在该节就地写明。

---

## 32. Todo Update

- **KB-25** → **GUARDED**（`b56a8442`）；Reachability 重估为 `INTERNAL`（严格第二发形态）
  并**在同一行写明**同 boundary 的 `USER`/`HIGH` 变体（K1）也已修复；Severity 重估；
  Evidence 升为 `E2(11)`。
- **KB-23** → **SUPERSEDED_BY KB-25**（`SAME_ROOT_ALIAS`，证据见 §10）；其附带发现
  `handle_interrupt` 缺 IDLE 守卫**已由同一 fix 覆盖**；另两项附带发现转为 follow-up 行
  （超时预算门禁盲区、`finish_turn` 5/12 零覆盖）。
- 其它条目状态**一律未动**（含 KB-24 / KB-26–30 / KB-18–21）。

---

## 33. Keep As-Is

至少以下各项**本批一次都没碰**：

- **FSM 架构**：不加状态、不改 `VALID_TRANSITIONS`、不改 `finish_turn`；
- **EventBus 优先级**：不动（INTERRUPT=0 插队语义保留）；
- **`_process_lock` 设计**：仍是回合串行化的权威（也不把它升级成"唯一防线"）；
- **pending 机制**：不加 scheduler/polling；显式 INTERRUPT 仍按既有合同清空队列；
- **无 watchdog / 无超时复位 / 无 `finally: set_idle()` / 无新 epoch / 无 `uncancel()`**；
- **无整块 shield**（临界区是同步代码，不是 shielded coroutine）；
- **前端零改动**（无 schema 变化）；
- **停机不 reset FSM**（只有 `Agent.start()` reset，既有语义）；
- **不改任何超时预算常量**（`_CHANNEL_SEND_TIMEOUT` / drain 预算都原样）；
- **`enter_thinking()` 位置**不动（Batch 10 的结论继续有效）。

---

## 34. New Findings

只登记，不修（除与 KB-25 **同一 logical boundary** 且不可分的 K1 已在 §14 一并修复）。

| ID（建议） | 发现 | 证据 | 影响 |
|---|---|---|---|
| **KB-31** | **K1**：取消（第一发）落在 `_handle_turn_exception` 的 await 上时，取消从 `except Exception` 块里逃出 `process()`（**兄弟 `except` 子句不接**）⇒ `run_state == RUNNING` 下相位永停 THINKING、Agent 永久失聪 | 本批探针 `/tmp/b11/probe_exc_window.py`（§11.2）+ `test_stop_button_during_error_finalization_does_not_deafen_agent`（PRE_FIX_RED） | **USER / HIGH** —— 与 KB-25 同一 logical boundary，本批**已一并修复**；单独登记是为了让"可达性证据"与 KB-25 的"严格第二发"区分开 |
| **KB-32** | `scripts/check_timeout_budget.py` 看不见**裸字面量**预算：`_turns.drain(timeout=3.0)`（`lifecycle.py:320`）与 `_CHANNEL_SEND_TIMEOUT=5.0`（`manager.py:40`）构成"外层 3.0 < 内层 5.0"的 S2-A3 形状，但门禁只盘**具名常量**、且明确拒绝跨模块调用图推断 ⇒ 该不对称对门禁不可见 | 独立子智能体 C 的审计；`manager.py:159-162` → `loop.py:1376` | 门禁盲区（非本批 scope）。KB-27 是它的姊妹条目 |
| **KB-33** | `FSMBackedPhaseController.finish_turn` 的 12 组合里 5 组抛 `ValueError`（只有第一次 transition 有 `can_transition` 守卫），且**零 gated 覆盖**（12 组合矩阵只存在于被 `pytest.ini::norecursedirs` 排除的 `tmp/b9_fsm/`） | 本批按 `VALID_TRANSITIONS` 独立重算 + 全仓 grep | 真实度/覆盖债；当前 5 组均未经 `process()` 可达（除 `reset + 活跃回合 + bg=True`） |
| **KB-34** | 第二发取消若落在 `_cancel_desktop_calls()` 上，在途 desktop 调用不会被逐条 `cancel_call`（best-effort 放弃，允许）；兜底只有启动期 `mark_stale_expired` ⇒ 运行期长窗口内那些调用会一直挂着 | `test_second_cancel_during_desktop_cleanup_settles` 的断言（`cancelled_calls == []`） | INTERNAL / LOW；属 §5 B 资源清理语义，不是 CF1/CF2 违反 |
| **KB-35** | **`finally` 内的普通异常会顶替在飞的 `CancelledError`**：任务从 cancelled 变 failed，且若抛点在 `transition(IDLE)` 之前，相位可能留在 INTERRUPTED（活跃区）。另外 `handle_interrupt` 的 `try` 起点在 `bus.post(INTERRUPTED)` **之后** ⇒ 那次 post 抛出时连 `finally` 都进不去 | Challenger B11 的 H 项：`[H-notify] cancelled=False exc=ValueError('idle notify exploded') fsm=idle`、`[H-transition] fsm=interrupted` | **当前不可达**（`_notify` 逐订阅者吞 `Exception`、`bus.post` 是无界队列 `put_nowait`、护栏保证 `transition(IDLE)` 合法）。已写进 `invariants.md` §4.4 已知边界；**将来在 `finally` 里新增任何可能抛异常的语句时必须重新评估** |
| **KB-36** | **并发第二个 `handle_interrupt()` 调用者会重复广播**：`transition(INTERRUPTED)` 在"已是 INTERRUPTED"时是 no-op，但 `bus.post` 照发 ⇒ `interrupted` ×2、`idle` 通知 ×2。即 docstring 的幂等只在"IDLE 入口"成立 | Challenger B11 的 C/J-interrupted：`states=['interrupted','interrupted','idle'] notified=[…,'idle','idle']` | **当前不可达**（唯一调用者是持 `_process_lock` 的回合任务）。若将来引入第二个调用者（IdleMonitor / Agent 层打断），必须先把判据从"IDLE 早退"升级为"完整 terminal 状态判据" |
| **KB-37** | **`AgentStatus._notify` 是顺序 best-effort**：逐个 `await` 订阅者，`CancelledError` 不是 `Exception` 因而不会被逐订阅者的 `except Exception` 吞掉 ⇒ 某个订阅者在通知上挂起/被取消时，**其后**的订阅者收不到该通知（`wait_until_idle` 若排在后面，会在 agent 已 IDLE 的情况下等到 timeout） | Challenger B11 的 C 项：`[C] late_listener=['thinking','interrupted']` 而 `harness.notified=[…,'idle']` | **当前不可达**（仓内唯一 `on_change` 注册者是 `wait_until_idle`，其回调无 await，且生产调用点只有 `bench`、自带超时）。属 `_notify` 的**既有**语义，非本批引入 |
| **KB-28（补证）** | O2 窗口（`IDLE 已广播 / 锁未释放`）在**第二发取消路径上同样成立**：带挂起订阅者时，收口在 `finally` 里已广播 IDLE 但仍持锁 ⇒ pending 里的 B 被派发并卡在锁上；此时再按一次停止，`cancel_current()` 取消的是**卡在锁上的 B**，而 B 的消息已 ACK、已出队 ⇒ 静默丢失 | Challenger B11 的 F-2：`[F-2] b_cancelled=True b_processed=False pending=[]` | 需"存在会挂起的 `on_change` 订阅者"这一前提（仓内无）；KB-28 行已补入该证据，本批**不修** |

---

## 35. Follow-up

保留（**本批未动**）：

- **KB-24 / KB-26 / KB-27 / KB-28 / KB-29 / KB-30**（Batch 10 副产物，仍 OPEN）；
- **KB-18 / KB-19 / KB-20 / KB-21**（Batch 9 副产物）；
- **KB-12**（tokenizer 估算启发式，Batch 7 已登记）；
- **KB-13** / **AR-16** / Ollama Provider Adapter（**本批不触碰**，见任务书 §86）；
- **KB-15 / KB-16 / KB-17**；**OBS-6 / OBS-7 / OBS-8**；**KB-11**；**GR-25**；**N-15**；
- 以及 §1 Canonical Backlog 其余全部条目。

本批新增的 follow-up：**KB-31**（已修但独立登记）、**KB-32**、**KB-33**、**KB-34**、
**KB-35**（`finally` 内异常掩盖取消，不可达）、**KB-36**（并发第二调用者重复广播，不可达）、
**KB-37**（`_notify` 顺序 best-effort，不可达），以及 **KB-28 的补证**（O2 在第二发取消路径上成立）。
