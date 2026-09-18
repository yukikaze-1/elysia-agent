# Batch 12 — Accepted Input Delivery Closure（KB-24）执行记录

- 日期：2026-09-18
- canonical scope：**KB-24**（admission 窗口内已 ACK 输入的所有权）
- worktree：`tmp/b12-wt`（HEAD `2e70854d` + 本批改动）
- 结论：**KB-24 = CONFIRMED / GUARDED**（production fix + 8 条 gated 守卫；见 §34）

---

## 1. Executive Summary

| 问题 | 结论 |
|---|---|
| **KB-24 confirmed?** | ✅ **CONFIRMED**（当前 HEAD 独立达到 **E2**：真 `Agent.start()` + 真 `process_message` + 真 EventBus 消费者 + 真 `CharacterRuntimeRegistry.get_or_load` 屏障 + 真 INTERRUPT 事件） |
| **classification** | **USER** / **HIGH**（原登记为 `LOW`，本批独立重判后**上调**——触发它不需要任何异常条件，见 §10） |
| **evidence** | **E2**（独立复现，非继承 Batch 10） |
| **accepted contract** | **Transport Accepted**（§3）：`accepted` = 调用已进入 runtime 生命周期 ＋ 决策幂等；**不**承诺持久化、**不**承诺一定被回复、**不**承诺失败一定告知 |
| **production fix?** | ✅ 最小改动集（§12）：① `CognitiveLoop.current_turn_owns_input` 新判据（在 `process()` **取锁之前**清空）+ `Agent._handle_user_input` 抢占判据补全；② 回合结束后 pending 兜底出队（Challenger A）；③ 4 处**可归因终态**日志；④ 无 source 取消的来源重置（Challenger D）。**零** wire / schema / 持久化 / FSM 改动 |
| **user reachability** | ✅ **是**：用户在 admission 窗口内**再发一条消息**（前端连发两条）即触发，无需任何故障条件 |
| **public API** | 零变化（无 schema / 无 wire 枚举 / 无新字段） |
| **persistence** | 零变化（无 schema / 无 migration / 不建 durable inbox） |
| **real CI** | 本机无凭据 ⇒ 提交就绪，**owner push 后触发**（§32.4）；本机等价证据：quick **6213** / normal **6209** / full **6245** 全绿，real-mongo **24 passed**，12 条静态门禁全绿 |

**一句话**：`accepted=true` 之后、回合真正接管输入之前，存在一个**没有任何 owner** 的窗口；
窗口内的取消（最常见的触发是"用户又发了一条"）会把这条已 ACK 的消息连同回合一起静默抹掉，
而 v2 幂等表已落 ACCEPTED ⇒ 客户端重试只会被逐字重放那个 `accepted`，消息永久消失。
本批把判据补全为"**未接管输入的回合不得被抢占**"，并让所有窗口内的终态**可归因**。

**Challenger 独立反证的三个真洞**（全部已修，各配守卫，§24）：判据在取锁段**陈旧**（第三条
消息仍能复现静默丢失）、新排队的消息**没有出队触发**（回合从未离开 IDLE ⇒ 永久滞留而 Agent
显示空闲）、归因**可以是错的**（无 source 取消沿用上一发来源）。这三个洞都是**修法自身**引入
或暴露的，不是原缺陷——记下来是因为"修完就宣布成功"会让它们在下一次变更里复活。

---

## 2. Original Backlog Evidence（canonical 描述与当前 HEAD 核对）

`todo.md` 的 KB-24 原始登记（Batch 10 副产物 O1）：

> 取消落在**回合开始之前**（`session_resolver` / `get_or_load`）时，本回合已 ACK 的消息
> **静默结束**——没有任何 `generation.started`/`generation.failed` 终态帧（v2 客户端一直
> 显示这条用户消息 + 已 accepted）。相位不受影响（FSM 保持 IDLE），故不属于 KB-22；
> 与 CR-08 的"ACK 说谎"同族。Reachability USER / Severity LOW / Evidence E2(10)。

逐项核对：

| 原始描述 | 当前 HEAD 核对 |
|---|---|
| "取消落在 `session_resolver` / `get_or_load` 时消息静默结束" | ✅ **成立**，但**不完整**：窗口其实是"`_generation_ctx` 清空 → 赋值"之间的**整段 prepare**（两处挂起点都算），而不仅限于那两个函数（§4） |
| "没有任何 `generation.started`/`generation.failed` 终态帧" | ✅ 成立。session append 在 `_build_turn_prompt`（`_think_loop` **之内**），窗口内连历史都没进 |
| "相位不受影响（FSM 保持 IDLE），故不属于 KB-22" | ✅ 成立（本批复现中相位全程 IDLE），与 KB-22 的"永久失聪"确为不同族 |
| "Severity LOW" | ❌ **上调为 HIGH**：原登记的触发形态是"外部取消恰好落在窗口上"；本批发现**用户自己连发两条消息**即触发，且第二条会**抢到第一条前面**被处理（§10） |
| "与 CR-08 的 ACK 说谎同族" | ✅ 成立，且**更严重**：CR-08 的窗口内客户端至少拿到 `False`/rejected；这里客户端拿到的是 `accepted` **且幂等表已终态化**，重试路径被彻底封死 |

---

## 3. `accepted` Contract（Phase 0 结论）

**这是本批最重要的一节**：修法必须由合同推出，而不是反过来。

### 3.1 证据

| 证据源 | 内容 |
|---|---|
| `frontend_contract/ws_message/ack.py:16-19` | `UserAckMessage = Union[AcceptedUserAckMessage, RejectedUserAckMessage]`（判别键 `status`）——**只有两个值**，`"replayed"` 不是 wire 值（它是 `ws_v2.py:157` 的内部派发字符串） |
| `frontend_contract/ws_message/_types.py:312-326` | accepted 变体字段：`type="user_message.ack"` / `messageId` / `clientRequestId` / `clientMessageId?` / `conversationId?` / `receivedAt` / `status="accepted"`；`extra="forbid"` ⇒ **accepted 不能带 error**；**没有** `terminal` / `code` / `retryable` |
| `contract.schema.json:5` | accepted 描述："accepted → 后续 generation.\* 事件；无 error"——**happy path 描述，不是有界保证** |
| `core/agent/agent.py:636-638` | `process_message` docstring："True = 消息已接受（已入队或直发）；False = …队列满被丢弃" |
| `core/input/ws_v2.py:563-569 / 600-606 / 801-807` | 三个 accepted 发送点都**先** `store.complete` 再发 ACK（complete-before-ack），且都只在 `process_message` 返回 True 时发 |
| `core/input/ws_v2.py:135-228` | 同 `clientRequestId` + 同内容摘要 + ACCEPTED ⇒ **逐字重放**原 ACK（含原 `receivedAt`、同 `messageId`）；**不会**复查这条消息有没有真的被处理过 |
| `docs/electron-backend-protocol-v2.md:111` | 只写了"重发原 ACK（同 messageId），不重复生成"，未提示重放的 ACK 可能断言一条从未被处理的消息 |
| `/home/yomu/elysia-frontend`（Electron） | `apps/desktop/src/main/transport/real-backend.ts:664-687` 消费 ack：accepted → resolve `{clientRequestId, serverMessageId}`；**没有任何跟踪 `generation.*` 是否到来的逻辑** ⇒ 客户端**不存在**"accepted 但从未处理"的终态 |
| `frontend/src/`（Vue WebUI） | **根本不消费 ack**（`useWebSocket.ts:32-115` 无 `user_message.ack` 分支），且 `InputBar.vue:71-72` 发送时不带 `clientRequestId` ⇒ 它是 v1 客户端 |

### 3.2 判定

**A. Transport Accepted（＋决策幂等）**——不是 Runtime Admission Accepted，也不是 Durable Accepted。

- **承诺**：① 请求已被 runtime 接手（`bus.post` 成功，或进了有界 `_pending_messages`）；
  ② 决策是幂等的（同 `clientRequestId` 永远得到同一个 ACK，重启后仍成立——幂等声明本身落库，TTL 1h）。
- **不承诺**：消息已持久化（`accepted` 时**没有**任何消息落库；只有幂等 claim 落库）；
  一定被回复；失败一定告知。
- `server_info.features.idempotency_durable` 只覆盖**claim**，不覆盖消息本身。

### 3.3 由此得到的本批目标（§117 的原文）

> 一旦系统对输入作出 runtime-level `accepted` 承诺，该输入就必须始终拥有一个明确的
> lifecycle owner 或 terminal disposition；任何受支持的 cancellation path 都不能让它在
> ACK 与 active-turn ownership 之间静默消失。

**明确不做的**：不把 `accepted` 升级成 durable guarantee；不保证"每条 accepted 都得到回复"
（用户可以取消、系统可以停机）；不为这个目标引入持久化队列、重试框架或 wire 新枚举。
**并且**：`cancelled` 之所以与"消失"不同，只在于它**可归因**——这正是本批给终态加日志的原因（§12）。

---

## 4. Admission Lifecycle Map

```text
client / user
   ↓ WS v2 text | voice.commit | REST /api/chat | v1 STT
[1] core/input/ws_v2.py::_finish_v2_text
      store.claim(clientRequestId)            ← 幂等 claim（Mongo，TTL 1h）
      attachment refs 预留
   ↓
[2] Agent.process_message()                   agent.py:610
      拒收窗口检查  status.is_service_running  ← ★ 这里 return False（无 ACK 或 rejected）
      v2 会话 work 预留  session_manager.reserve_work
      ├─ 忙（相位忙 或 bus 里已有未派发 USER_INPUT）→ _pending_messages.append → return True
      └─ 否则 → bus.post(UserInputData) → return True
   ↓
[3] ★ ACK 点 ★  core/input/ws_v2.py:600-606
      store.complete(...)                     ← 决策落成 ACCEPTED（终态）
      _send_ack(status="accepted")            ← 客户端此刻认定"系统已接手"
   ↓
[4] EventBus 队列（无界 PriorityQueue；无重投、无 TTL、无持久化）
   ↓  Agent._process_events()（唯一消费者，per-event 异常边界）
[5] Agent._handle_user_input()（**同步**，无挂起点）  agent.py:892
      prev 存活 && 未接管输入 → 排队（本批补全的判据）
      否则 → cancel_current() → _turns.spawn(回合任务) → current_task = task
   ↓
[6] 回合任务 first-step → CognitiveLoop.process()    loop.py:748
      async with self._process_lock               ← 可能是第一个挂起点
   ↓
[7] _prepare_turn()                                loop.py:512
      _generation_ctx = None                      ← 清空（**窗口开始**）
      await session_resolver(...)                 ← 真挂起点（SessionMemory.load：Mongo/JSONL）
      _admission_message_id / _admission_session_id 记录（本批新增，仅供归因）
      await registry.get_or_load(...)             ← 真挂起点（角色实例/修订的 Mongo 读）
      ...（下面全同步）...
   ↓
[8] ★ ADMISSION COMMIT POINT ★  _generation_ctx = GenerationContext(...)   loop.py:598
      （与 [9] 之间**没有 await** ⇒ "ctx 非 None" ⟺ "已/即将 THINKING"）
   ↓
[9] await phases.enter_thinking()                  ← FSM IDLE → THINKING
   ↓
[10] generation_registry.register(...)（Mongo upsert，可挂起）
   ↓
[11] _think_loop → _build_turn_prompt
      session.add_message(UserMessage(...))        ← ★ 到这一步输入才进 session/archive
   ↓
[12] generation.started 帧 / typing(true) → LLM 流式 → … → IDLE
```

**ACK 在哪一步？** **第 [3] 步**——在 `process_message` 返回 True 之后、事件被消费之前。
**commit point 在哪一步？** **第 [8] 步**（`_generation_ctx` 建立）。
⇒ **ACK（[3]）远早于 commit point（[8]）**，两者之间隔着 [4][5][6][7] —— 这就是 KB-24 的窗口。

---

## 5. Ownership Map

| 阶段 | M 的 owner | 证据 |
|---|---|---|
| validation 后（空文本/无内容） | 无（`return False`，调用方回 rejected） | `agent.py:643-644` |
| 拒收窗口（`_state != RUNNING`） | 无（`return False`，rejected） | `agent.py:656-660` |
| 忙分支 append 之前 / ACK 之前 | 调用方帧（栈上） | `agent.py:688-697` |
| **ACK 之前**（`bus.post` 尚未返回） | 调用方帧；异常则回滚 work 并向外抛 | `agent.py:699-710` |
| **ACK 之后 / EventBus 队列中** | **EventBus 队列**（唯一强引用；无界） | `event_bus.py:31,54` |
| `bus.next()` 返回之后 | **事件消费者任务**（已出队，**不会重投**） | `event_bus.py:72-73` |
| handler 开始（`_handle_user_input`，同步） | 消费者任务；无挂起点 ⇒ 此段不可被取消 | `agent.py:892` |
| task created（`spawn` 返回） | **回合任务**（`_turns` 强引用 + `current_task`） | `agent.py:963-973` |
| task 未 first-step（pre-start） | 回合任务（但 coroutine 体从未执行） | 既有 C1/P1-06 判据 |
| task first-step、停在 prepare 挂起点 | 回合任务 —— **但输入本身没有 owner** ← **KB-24 窗口** | `loop.py:524`（`_generation_ctx = None`） |
| **commit point 之后**（ctx 非 None） | **回合任务（回合已接管输入）** | `loop.py:598` |
| active turn（THINKING/EXECUTING/SPEAKING） | 回合任务 | Batch 10/11 保证 |
| pending（busy 时 append 的） | `_pending_messages` → `STATE_CHANGE→IDLE` 出队 | `agent.py:1054-1074` |
| cancelled（窗口内） | **显式终态**（本批：`disposition=cancelled_pre_ownership` / `cancelled_by_interrupt`，带 source + 身份） | 本批 §12 |
| 队列满（pre-ACK） | 无（`return False` → rejected） | `agent.py:681-685` |
| 队列满（post-ACK，`_handle_user_input` 分支） | 显式终态（本批加 WARNING 归因）+ 释放 work | 本批 §12 |

---

## 6. Admission Commit Point

> **从 `_generation_ctx = GenerationContext(...)` 这一行开始，本回合已接管这条输入。**

理由（三条都必须成立）：

1. **时序**：它与 `enter_thinking()` 之间**没有 await** ⇒ "ctx 非 None" ⟺ "FSM 已/即将 THINKING"
   （Batch 10 Challenger A3b 已证，即使某个 `on_change` 订阅者在 `_notify` 上挂起也成立）。
2. **互斥**：整段 `_prepare_turn` 在 `_process_lock` 之内 ⇒ 不存在第二个回合的 ctx 混入
   （Batch 10 的 TH3）。
3. **既有先例**：Batch 10 已经把"ctx 非 None"用作"本回合是否开始过"的**唯一判据**
   （`_handle_prepare_cancelled`）——本批复用它，**不**新增第二套生命周期计数器。

**⚠️ 关键澄清（本批新增的区分）**：`_generation_ctx` 非 None 在 Batch 10 的语境里是
"**回合**是否开始"，但它**不等于**"**输入**已被交付给用户可见的生命周期"——
输入真正进 session 是在 [11] 的 `session.add_message`。因此本批把三者分开：

| 概念 | 判据 | 谁负责 |
|---|---|---|
| 回合已开始（相位可收口） | `_generation_ctx is not None` | Batch 10/11 |
| **输入已被回合接管** | 同左（本批 `current_turn_owns_input`） | **Batch 12**：接管前不得被抢占 |
| 输入已进入用户可见历史 | `session.add_message` 已执行 | 无需新不变量（[8]→[11] 之间无新挂起点之外的窗口） |

---

## 7. ACK Timing

- ACK 在 **`process_message` 返回 True 之后**、**事件被消费之前**（[3] 步）。
- commit point 在 **[8] 步**。⇒ **ACK 早于 commit point**，中间 4 个阶段。
- **本批不移动 ACK**：把 ACK 推到 [8] 之后会让"排队时客户端长时间没有 ACK"进入 wire 契约
  （§11 明令）；且 v2 幂等表必须在**事故前**终态化，否则重试会重复入队（**既有设计**）。
- 本批选择的方向是 **"把 commit point 的语义前移"**：让 [3]→[8] 之间这条输入**也有 owner**
  （留在原回合里 = 排队保护；若被取消则落显式终态），而不是推迟 ACK。

---

## 8. Cancellation Source Matrix

| Source | 代码路径 | 能落进 admission 窗口？ | USER? | 本批结论 | 理由 |
|---|---|---|---|---|---|
| **A. 第二条前台消息（抢占）** | `agent.py:929-930` `_handle_user_input` → `cancel_current()` | ✅ **能，且最常见** | ✅ | **修（本批核心）** | 未接管输入的回合**不构成**"正在生成"，抢占它等于静默丢弃一条已 ACK 输入 |
| **B. 停止按钮** | `core/input/ws_handlers/chat.py:136` → `bus.post(InterruptData("websocket"), INTERRUPT)` → `_handle_interrupt` | ✅ 能 | ✅ | **保持取消 + 显式终态** | 既有产品语义 = "放弃尚未开始的输入"（`_handle_interrupt` 明确丢弃 `UserInputData` 与 pending）；本批**不**复活，只让它可归因 |
| **C. 语音取消** | `voice_sessions.py:425-431`（`source="voice_cancel"`） | ⚠️ 有门 | ✅ | 同 B | 门是 `not is_completely_idle` —— 窗口内相位 **IDLE** ⇒ 当前**不触发**（登记为新发现） |
| **D. 客户端断线** | `ws_handler.py:524-530`（`source="disconnect"`） | ❌ 否 | ✅ | 无需修 | 门是 `current_generation_user_message_id`（窗口内为 None，正是**未** commit）⇒ 结构上不可能落进窗口 |
| **E. 模型热切换** | `server.py:392` `_cognitive.cancel_current()`（无 source） | ✅ 能 | ✅ | **显式终态**（归因为 `unknown`） | 换模型只该打断"正在进行的 LLM 调用"，窗口内并没有调用可打断；本批不扩展其语义，只保证可归因 |
| **F. `Agent.stop()`** | `lifecycle.py:_SHUTDOWN_STEPS` 第一步 `_stop_event_task` 先停消费者；后续 `_turns.cancel_all()`+`drain(3s)` | ⚠️ 消费者已停 ⇒ INTERRUPT 无人消费 | ✅ | 无需修 | 停机中不承诺恢复（AD5）；本批不新增持久化 |
| **G. 后台任务终态唤醒** | `agent.py:1029` `_handle_task_result` → `cancel_current()` | ⚠️ 能 | 否（系统） | 仅登记 | `_handle_task_result` **没有** `prev` 判据（与 `_handle_user_input` 不同）⇒ 可抢占一个未接管的用户回合 |
| **H. 主动搭话 tick** | `agent.py:991` `_handle_system_tick` → `cancel_current()` | ⚠️ 能 | 否（系统） | 仅登记 | 门是 `is_completely_idle`（窗口内成立）+ 1s 轮询；触发时 `touch()` 已重置计时器，故不会立刻重发 |
| **I. 重复取消** | 第二次 INTERRUPT | 否 | ✅ | 不适用 | 窗口内回合任务已 done ⇒ `cancel_current()` 返回 False |
| **J. 测试直调 `task.cancel()`** | 仅测试 | — | — | **不作为可达性证据** | 本批所有 E2 都用生产取消源（真实入口消息 / 真 INTERRUPT 事件） |

**门禁盲区**：`Agent.stop()` 对 in-flight 回合的直接 `task.cancel()`（`AsyncTaskScope.drain` 超时后）
是 Batch 11 的第二发来源；它在窗口内也可能落下，但那时 `run_state == STOPPING/STOPPED` ⇒ 用户不可见（§73 允许放弃）。

---

## 9. Pre-Fix Reproduction / Falsification（deterministic E2）

### 9.1 主 E2（本批独立复现，当前 HEAD）

**入口**：真 `Agent(config=AppConfig())` + 真 `Agent.start()`（含真 `_process_events` 消费者）
+ 真 `process_message` + 真 EventBus + 真 FSM；唯一替身是**存储**（`fake_storage`）与 **LLM**（确定性流式替身）。
**屏障**：`CharacterRuntimeRegistry.get_or_load`（`_prepare_turn` 里**真实**的第二个挂起点，
生产上要读角色实例/修订的 Mongo 文档）——`asyncio.Event` 精确钉死，不用 `sleep`。

```text
1. process_message("第一条")            →  True   （= WS 层已回 accepted=true）
2. 等待屏障 entered                      →  回合任务已 first-step、停在 get_or_load
   · 相位 == IDLE                        （enter_thinking() 还没走到）
   · _generation_ctx is None             （admission **未**提交）
3. process_message("第二条")            →  True   （第二条真实入口消息，生产形态）
```

| 断言 | 修前实测 | 修后实测 |
|---|---|---|
| 第一条回合任务 | `cancelled()=True`, `done()=True` | 存活并正常完成 |
| `_pending_messages` | `[]` | `["第二条"]` |
| session 里的 user 消息 | `[]`（**第一条从未进历史**） | `["第一条","第二条"]` |
| 相位 | `thinking` → `idle`（K22 收口后） | `idle`（正常完成） |
| 第二条 | 被处理（**抢到第一条前面**） | 第一条之后按序处理 |

⇒ **第一条已 ACK 的消息：不在 active turn、不在 pending、不在 session/archive、
没有 `generation.*` 终态帧、EventBus 不会重投、之后也不会自然恢复**——完整的
"silent disappearance"（§7 的判据逐条成立）。

修前的探针输出（`tmp/b12_probe/test_b12_probe.py`，修复前 HEAD）：

```text
=== 状态 ===
first_task.cancelled() = True
phase                  = thinking
pending                = []
session user msgs      = []
bus pending_user_inputs= 0
=== gate 放行后 ===
phase                  = idle
pending                = []
session user msgs      = [TextPart(... '第二条')]        ← 只有第二条
```

### 9.2 第二处窗口（同族，Batch 10 已修相位但**未修输入**）

同屏障换成 `_generation_repo.upsert`（`generation_registry.register`，**commit point 之后**的挂起点）：

```text
phase at register = thinking | generation_ctx = True
=== 后段 gap（register）取消 ===
first_task.cancelled() = False        ← 任务正常结束（Batch 10 的收口生效）
phase                  = idle
pending                = []
session user msgs      = []           ← 但这条输入同样没进历史、也没有 generation 帧
```

⇒ Batch 10 修的是**相位**（不再永久失聪），**输入仍然静默消失**。这条被本批归类为
KB-24 的**同 root 第二形态**：同样是"已 ACK、未进 session"的输入以无 export 的方式结束
（它的终态在 `_handle_prepare_cancelled` 的 **True** 分支——走 canonical 中断路径，
但**没有任何东西告诉用户这条输入被放弃了**）。

### 9.3 反证（不是所有取消都该被"保护"）

| 场景 | 期望 | 实测 |
|---|---|---|
| 窗口内**停止按钮** | 明确取消、**不**复活、可归因 | 取消 + `disposition=cancelled_pre_ownership source=websocket`；之后不被处理 ✅ |
| 窗口内**两条**消息 | 都不丢、原序 | `M1` 先完成、`M2` 随后 ✅ |
| commit point **之后**取消 | 交给 Batch 10/11，**不**重复处理 | 该输入在 session 恰一条、LLM 只被喂一次 ✅ |
| 无取消的快速路径 | 零变化 | 直发、即时处理、pending 空 ✅ |
| Agent 真忙 | 既有 pending 路径不变 | 排队 → IDLE 出队 ✅ |

---

## 10. User Impact

**用户看到什么**：

1. 用户发出消息 M1（前端乐观显示，v2 收到 `accepted` 幂等确认）；
2. 用户很快又发一条 M2（**这是触发条件，不需要任何故障**）；
3. M1 的回合在"解析会话 / 装载角色"期间被 M2 抢占并**整个取消**；
4. 客户端**永远**不会收到 M1 的 `generation.started` / `generation.failed`，
   服务端历史里也**没有** M1；刷新页面后 M1 消失（历史以服务端为准）；
5. 若客户端因超时重试 M1（同 `clientRequestId`），服务端从幂等表读出 ACCEPTED，
   **逐字重放**那条 `accepted` ⇒ 客户端被明确告知"已受理"，而这条消息在服务端**不存在**。

**为什么严重度从 LOW 上调到 HIGH**：

- 原登记设想的触发形态是"某个外部取消恰好落在窗口上"（窗口只有毫秒~秒级）；
- 实际触发形态是**用户自己的下一次输入**——前端连发两条是常态操作，
  而窗口在生产上是"会话加载 + 角色 runtime 装载"的真实耗时（Mongo 读，实测毫秒到秒级）；
- 后果**不可观测且不可恢复**：无日志（修前）、无 wire 帧、无历史、幂等表封死重试。

---

## 11. Root Cause

**不是** "ACK 太早"（把 ACK 推到 commit point 之后会把排队延迟写进 wire 契约，
而且幂等表必须在事故前终态化——既有设计，§7）。

**是** **ownership handoff 有洞**：`_handle_user_input` 判断"能否抢占在途回合"用的判据
是 `prev not in self._started_cognitive_tasks`——它回答的是"**coroutine 有没有 first-step**"，
而抢占真正需要的答案是"**这条输入有没有被接管**"。两者在 `_prepare_turn` 期间**不一致**：

```text
task in _started_cognitive_tasks == True      （coroutine 已 first-step）
_generation_ctx is None                       （但还没接管输入）
```

旧判据看到第一个为真就放行抢占 ⇒ 取消落到 `_prepare_turn` 的挂起点 ⇒
CancelledError 逃出 `process()`（`_handle_prepare_cancelled` 返回 False 后 `raise`）
⇒ 回合任务 cancelled ⇒ **输入没有任何 owner**。

**四个促成因素**（都在修法中被逐条针对）：

1. session append 在 `_build_turn_prompt`（`_think_loop` 内），**不在** `process()` 入口 ⇒ 窗口内输入连历史都没有；
2. EventBus 对 USER_INPUT **只搬移不复制**（`_handle_interrupt` 的 drain 表），无重投；
3. `process_message` 在"相位忙"时排队、在"相位 IDLE 但事件刚 post"时不排队——
   窗口内相位恰好是 **IDLE** ⇒ 第二条走 fast path 直发总线；
4. 幂等表在 ACK 前已落 ACCEPTED ⇒ 客户端即使重试也拿不到"未处理"的真相。

---

## 12. Fix

### before / after（生产 diff，两处 + 三处归因日志）

**(1) `core/cognitive/loop.py`：新增 ownership 判据**

```python
@property
def current_turn_owns_input(self) -> bool:
    """在途回合是否已经接管它那条用户输入（KB-24 的 admission commit point）。"""
    return self._generation_ctx is not None
```

**(2) `core/agent/agent.py`：补全抢占判据**

```python
# before
if prev is not None and not prev.done() and prev not in self._started_cognitive_tasks:
    ...排队...

# after
if (prev is not None and not prev.done()
        and (prev not in self._started_cognitive_tasks
             or not self._cognitive.current_turn_owns_input)):
    ...排队...
```

**(3) `process()` 取锁**之前**清空判据（Challenger C）**

```python
# before：只在 _prepare_turn 里清 ⇒ `await self._process_lock` 那一段沿用上一回合的 ctx
async with self._process_lock:
    ...
# after
self._generation_ctx = None            # ← 判据必须先描述"本回合"
self._admission_message_id = None
self._admission_session_id = ""
self._turn_message_persisted = False
async with self._process_lock:
```

`await self._process_lock` 是 pending 出队 spawn 的回合**最常见的第一个挂起点**（上一回合收口
尚未放锁）。修前那一段里 `_generation_ctx` 仍是上一回合的值 ⇒ 判据误报"已接管" ⇒ 第三条
消息抢占一个什么都没接管的回合，且取消落在取锁的 `__aenter__`（**在取消边界之外**）
⇒ 已 ACK 输入静默丢失且**连 disposition 都没有**（Challenger 用"公开订阅者挂起"确定性复现）。

**(4) 回合结束后的 pending 兜底出队（Challenger A）**

```python
def _drain_pending_after_turn(self) -> None:
    """回合任务落定后的兜底出队。"""
    if not self._pending_messages: return
    if not self.status.is_completely_idle: return
    current = self._cognitive.current_task
    if current is not None and not current.done(): return
    self._dispatch_pending_message()
```

pending 的唯一既有出队时机是 `STATE_CHANGE → IDLE`；而窗口内的取消/异常恰恰发生在相位
**仍是 IDLE** 时（`handle_interrupt` 因"已 IDLE"而 no-op）⇒ 没有 STATE_CHANGE ⇒ 被本批
守卫**新排队**的那条已 ACK 消息会一直躺着，而 Agent 显示完全空闲（实测 `switch_llm` 与
P0-3 `StorageUnavailableError` 两条生产形态）。兜底点放在回合任务的 done callback 里
（同步、不 spawn、不新增轮询），复用同一个 `_dispatch_pending_message`。

**(5) 四处"显式终态"（可归因，不新增 wire 帧 / 不写库）**

| 位置 | 场景 | 记录 |
|---|---|---|
| `loop.py::_handle_prepare_cancelled`（ctx None） | commit point **之前**被取消 | `disposition=cancelled_pre_ownership source=… message_id=… conversation_id=…` |
| `loop.py::_handle_prepare_cancelled`（ctx 非 None **且输入未进历史**） | commit point 之后、`session.add_message` 之前被取消（§9.2 第二形态） | 同上（判据 = 新增的 `_turn_message_persisted`） |
| `loop.py::_handle_turn_exception`（ctx None） | 窗口内**异常**（同 root，§21） | `disposition=failed_pre_ownership error=… message_id=… conversation_id=…` |
| `agent.py::_handle_interrupt`（drain `UserInputData` / 清空 pending）+ `_handle_user_input`（队列满） | 停止按钮丢弃已接受输入 / post-ACK 队列满 | `disposition=cancelled_by_interrupt` / WARNING 归因 |

**(6) 归因不能是错的（Challenger D）**：`cancel_current()` **无 source** 时把
`_interrupt_source` 重置为 `"unknown"`（修前沿用上一发的来源——实测先 `voice_cancel`
再来一次无 source 取消，admission 终态被误归因成 `voice_cancel`）；`_handle_prepare_cancelled`
的 False 分支消费后立即清空来源。**错归因比没有归因更坏**：它把排查引向不存在的原因。

### 为什么这是最小方案（§10 的四个候选逐条排除）

| 候选 | 为什么**不**选 |
|---|---|
| **A. ACK 延后到 commit point 之后** | 会把"排队时客户端长时间没 ACK"引入 wire 契约；且 `store.complete` 必须早于 ACK（否则重试重复入队）——**blast radius 覆盖整条 v2 幂等链**，远超本批 |
| **B. 先把消息放进受控 admission/pending 再 ACK** | 会让**每条**消息都经过 pending，改变 fast-path 时序、`QUEUE_FULL` 语义、metrics 与 `_current_task` 行为（§12 明令除非架构证据支持）——本批没有这种证据 |
| **C. cancel-before-ownership 时 requeue** | ① 会把"停止按钮取消的输入"复活（违反 §32/AD5）；② 要重新争 pending 容量（§23）；③ 要重算 FIFO 位置（§24）；④ 需要区分"从未转移"与"已转移"的原子 claim（§52）——四类新风险换一个**已经**由"不抢占"更简单解决的问题 |
| **D. 显式 terminal cancellation**（选中） | 与既有产品语义一致（停止按钮本来就要放弃未开始的输入），零 wire 变化、零持久化、零新调度器，且**正好**补上"静默"与"被取消"之间缺失的那一步：可归因 |

**本批选 D ＋ 修 A 的触发源**：把"用户自己连发两条"这条**最常见的触发路径**从
"静默取消"改成"排队保护"（AD3），其余取消源保持既有语义但落显式终态（AD2/AD5）。

### 明确不做

不延后 ACK、不新增 durable queue / inbox、不新增 FSM 状态或转换边、不新增 wire 枚举或字段、
不改 pending 出队机制、不加 watchdog / 轮询 / `asyncio.shield` 包 `process()`、
不把 session history 当工作队列、不对所有 `CancelledError` 一律 requeue、不建立 retry 框架。

---

## 13. Ownership Transfer Semantics（本报告重点）

```text
Transport（WS v2 / REST / STT）
   │  ownership handoff：process_message 返回 True ⇒ WS 回 accepted=true
   ▼
EventBus 队列             ← owner：总线（无界、无重投、无持久化）
   │  ownership handoff：consumer 的 bus.next() 返回（**已出队，不可回退**）
   ▼
Agent handler（同步）      ← owner：消费者任务（此段无挂起点 ⇒ 不可被取消）
   │  ownership handoff：_turns.spawn 返回 + current_task 登记
   ▼
回合任务
   ├─ [未接管] first-step → prepare 挂起点   ← owner：**回合任务**，但**输入无 owner** ★KB-24★
   │     · 被取消 → 显式终态 cancelled_pre_ownership（本批）
   │     · 新输入到 → **不抢占**，新输入排队（本批）
   │  ownership handoff：commit point（`_generation_ctx` 建立，无 await 相邻）
   ├─ [已接管] THINKING / EXECUTING / SPEAKING ← owner：回合任务
   │     · 被取消 → Batch 10/11 的取消安全收口（本批不插手）
   ▼
Pending 队列（忙时 / 未接管时的输入）        ← owner：_pending_messages
   │  ownership handoff：STATE_CHANGE → IDLE 出队 → bus.post(UserInputData)
   ▼
Terminal disposition（completed / failed / cancelled / cancelled_pre_ownership）
```

**每个 handoff 都有明确 owner**，唯一的例外（[未接管] 那一段）由本批的两条规则覆盖：

1. **不抢占** ⇒ 输入留在它自己的回合里（该回合会正常跑完，或按取消源落终态）；
2. **若被取消** ⇒ 落显式终态（可归因），而不是无记录地结束。

**Batch 12 只负责 commit point 之前**；commit point 之后由 Batch 10（相位）与
Batch 11（取消安全的终态收口）负责——本批**不**重修那一段（§54）。

---

## 14. Requeue / Cancel Decision（逐 source）

| Source | 决策 | 依据 |
|---|---|---|
| 第二条前台消息（A） | **不取消**（排队保护） | 用户没有取消意图；这条输入只是"还没轮到" |
| 停止按钮 / 语音取消（B/C） | **取消 + 显式终态**（不 requeue） | 既有产品语义：`_handle_interrupt` 明确丢弃 `UserInputData` 与 pending = "放弃尚未开始的输入"（§32：若当前行为明确是 A，Batch 12 不能为 AI1 把它重排回来） |
| 断线（D） | 不适用（结构上进不了窗口） | 门读 `current_generation_user_message_id`（窗口内 None） |
| 模型热切换（E） | 取消 + 显式终态 | "打断正在进行的 LLM 调用"在窗口内无对象可打断；保留既有语义、不扩展 |
| 停机（F） | 取消、不 requeue | §73：停机中允许放弃，但不得留下"重启后重新启动处理"或 task leak ⇒ 本批**不**引入任何持久化/复活路径 |
| 后台任务唤醒（G）/ 主动 tick（H） | 仅登记，不改 | 非用户可达（G 是系统事件，H 门是相位 IDLE + 1s 轮询）；修它们要动各自的 cancel 语义，超出 scope |
| 队列满（pre-ACK） | `return False` → rejected（不变） | §41：recovery 不得把 rejected 变成 accepted |
| 队列满（post-ACK） | 显式终态 + 释放 work | §40：已 accepted 的项不得因事后容量竞争静默丢失 |

**核心区分**：本批**没有**把"所有 `CancelledError` 都 requeue"（§29）——
唯一的"保护"是**不制造**取消（A），其余路径一律"取消 + 归因"。

---

## 15. Duplicate Prevention（AI4 / §52）

**为什么不可能双处理**：

1. **原子交接由 `_process_lock` 保证**：一个回合要么在 commit point 之前（此时它**没有**接管输入），
   要么在之后（接管成功，且持锁直到收口完成）。不存在"两个回合同时接管同一条输入"。
2. **本批不引入任何 requeue 路径** ⇒ 没有"cancel hook 把已接管的消息塞回 pending"的可能
   （这正是 §52 担心的形状）。
3. **pending 与 active 互斥**：`_handle_state_change` 用 `pop(0)` **搬移**（不是复制）pending 项后
   `bus.post`；被保护而排队的输入只有**一个** owner。
4. **`_handle_user_input` 的排队分支只在"未接管"时进入**：一旦 `current_turn_owns_input` 为真，
   新输入走 `cancel_current()` 路径——它不会把被取消回合的输入再放进 pending。

**回归守卫**：`test_cancel_after_history_append_does_not_duplicate_processing` 从**边界另一侧**
断言：commit point 之后取消 ⇒ 该输入在 session 中**恰一条**、LLM 只被喂**一次**、pending 为空。
（按 message identity/内容判据，不只比较 response 数量——§90。）

---

## 16. Ordering / FIFO（AI5）

- **被保护的消息不插队**：窗口内到达的新输入 append 到 `_pending_messages` **尾部**，
  在它前面的输入（正被保护的 M1）先完成 ⇒ 顺序 M1 → M2（`test_admission_gap_preserves_fifo_order`）。
- **修前是反序**：M2 抢占 M1 并**先**被处理，M1 消失。
- **已知边界（新发现，未修）**：连发**三条**时，若第二条还排在 EventBus 队列里
  （消费者尚未把它交给 `_handle_user_input`），`process_message` 会因
  `bus.pending_user_inputs > 0` 把第三条**直接** append，随后消费者才 append 第二条
  ⇒ pending 里反序（实测 `['M3','M2']`）。**两条都有 owner**，只是次序错；修法前后一致。
  由 `test_third_message_arriving_while_second_is_still_on_the_bus` 钉住现状并登记
  （§36 新发现；不顺手修——修它要动 `process_message` 的入队判据与 pending 语义）。

---

## 17. Queue-Full Semantics（§23/§40/§70）

| 场景 | 行为 | 是否静默 |
|---|---|---|
| pre-ACK（`process_message` 忙分支满） | `return False` → WS 回 `rejected`（`QUEUE_FULL`，`retryable` 语义保持） | 否（既有） |
| post-ACK（`_handle_user_input` 排队分支满） | 释放 session work + **WARNING 归因**（本批新增）+ 终态丢弃 | **修前静默**，修后可归因 |
| post-ACK（`_handle_interrupt` 清空 pending） | 释放 work + **INFO 归因**（本批新增） | **修前静默**，修后可归因 |

**本批不新增容量**、不做"优先插回"（那需要区分"从未转移"的原子 claim，见 §12 候选 C 的排除理由）；
post-ACK 的容量竞争仍以"显式终态"收场，符合 §40 的"不得静默丢失"而不是 §70 的"不得越界"。

---

## 18. User Stop Semantics（§32）

**产品语义（本批确认，不改）**：停止按钮 = 取消当前工作 **并放弃尚未开始的输入**。
证据：`_handle_interrupt` 的 drain 表明确"`UserInputData` → 释放会话 work 后丢弃（用户已取消这条输入）"，
并 `_pending_messages.clear()`。

⇒ `send M → accepted → 立即 stop` 的结论是 **A. cancelled，不回复**。
本批**没有**为了让 AI1 好看而把它重排回来；补的只是"这条被放弃了"这一事实**可归因**：
`disposition=cancelled_pre_ownership source=websocket message_id=… conversation_id=…`。

**守卫**：`test_admission_gap_stop_button_cancels_with_explicit_disposition` 同时断言
① 有显式终态；② **不** requeue；③ 之后**不**被处理（防"错误复活"）。

---

## 19. Disconnect Semantics（§72）

当前实现：断线**只在**"本连接发起的 generation 仍在途"时取消
（`ws_handler.py:524-530`：`current_generation_user_message_id` 属于 `conn.sent_message_ids`）。
窗口内该属性为 **None**（commit point 未到）⇒ 断线**不会**落进窗口，结构上安全。
本批不改断线语义，也**不**把 `current_turn_owns_input` 接到断线路径上（无必要）。
语音取消（`voice_sessions.py:425-431`）另有一个 `not is_completely_idle` 门 ⇒ 窗口内（IDLE）**不触发**；
登记为新发现（§36）。

---

## 20. Shutdown Semantics（§31/§73）

- 停机**第一个**步骤 `_stop_event_task` 就停掉事件消费者 ⇒ 其后的 INTERRUPT 不再被消费，
  不会出现"停机中处理新输入"；
- 后续 `_turns.cancel_all()` + `drain(3.0)` 取消在途回合（含窗口内的），超时后直接 `task.cancel()`
  （Batch 11 已保证收口对重复取消免疫）；
- 本批**不新增**任何持久化或"重启后恢复"路径 ⇒ `run_state == STOPPED` 后**不会**有新回合启动；
- 停机中窗口内被取消的输入**允许**被放弃（§73），本批对它的一致性要求只有"不得留下 task leak /
  不得在 STOPPED 后处理"。实测：`test_busy_agent_still_queues_after_fix` 等用例结束时无孤儿任务（§26）。

---

## 21. Ordinary Exception Observation（§35/§76）

**结论：与 KB-24 同 root，已在同一改动里被覆盖，但形态不同。**

- 窗口内普通异常（例如 `generation` 审计字段校验失败、`StorageUnavailableError`）在
  `_generation_ctx is None` 时**原样向外抛**（不产生任何相位收口——既有合同），
  回合任务以异常结束 ⇒ **消息同样没有任何 owner**（同 root：admission 窗口）。
- 与取消的**差别**：异常路径上任务以 `exception()` 结束（不是 `cancelled()`），
  且 `store` 里那条是 ACCEPTED。
- **本批未新增针对异常的分支**：`current_turn_owns_input` 的判据对"取消"与"异常"是同一个
  （都只看 `_generation_ctx`），而 §12 的 (2)（不抢占）本身就同时消掉了两者在窗口内的
  **最常见触发源**（新输入抢占）。异常**自发**发生的窗口内失败仍以"任务异常结束"收场——
  它**有** owner（回合任务以异常终态结束，异常本身会进日志与 `_handle_turn_exception` 的分类），
  不属于"无 owner"。
- **Challenger 补充（已修）**：窗口内的失败在修前**连归因都没有**——`generation.failed`
  只在 ctx 非 None 时发，所以 `StorageUnavailableError`（P0-3 fail-closed）落到窗口里时，
  用户看到的只有服务端一句"存储依赖不可用"，而那条已 ACK 的输入在观测面上与"从未被处理"
  完全一样。本批在 `_handle_turn_exception` 的 `ctx is None` 分支补了
  `disposition=failed_pre_ownership`。因此本批**不**单独立 AI8：异常与取消同 root、同判据、
  同一个终态点，由 **AD1/AD2** 一起覆盖。

---

## 22. Persistence Boundary

| 层 | 保证到哪里 |
|---|---|
| **live runtime**（本批范围） | `accepted` 之后，输入始终有 owner 或显式终态（AD1–AD5） |
| **进程崩溃 / 硬杀** | **不保证**（`accepted ≠ durable`）：`accepted` 时消息没有落库，EventBus 是内存队列，无 WAL、无 inbox。§34 明确排除 |
| **重启** | 不恢复（不引入 durable queue / inbox / migration） |
| **客户端重试** | 幂等表（TTL 1h）保证"同一个 `clientRequestId` 不会被处理两次"；但它**不**保证"被处理过"——这正是 §10 的危害面之一，本批修的是**制造这个不一致的那个窗口**，而不是改变幂等语义 |

---

## 23. Regression Tests

文件：`core/agent/tests/test_admission_input_delivery.py`（8 例，`marker=service`，
真 Agent + 真 `start()` + 真 `process_message` + 真 EventBus + 真 INTERRUPT；屏障全部用 `asyncio.Event`）

| # | 用例 | 分类 | 修前 | 修后 |
|---|---|---|---|---|
| 1 | `test_admission_gap_incoming_input_queues_and_nothing_is_lost` | **PRE_FIX_RED**（AD1/AD3） | ❌ `pending 未在预期时间内达到 1 条：[] (bus=0)`（消息被抢占吞掉） | ✅ |
| 2 | `test_admission_gap_preserves_fifo_order` | **PRE_FIX_RED**（AD3/AI5） | ❌ 同上 | ✅ |
| 3 | `test_third_message_arriving_while_second_is_still_on_the_bus` | **FALSIFICATION_GUARD**（钉住既有乱序 + 三条都不丢） | ❌（消息被吞） | ✅ |
| 4 | `test_admission_gap_stop_button_cancels_with_explicit_disposition` | **PRE_FIX_RED**（AD2/AD5） | ❌ `窗口内取消没有落显式终态（disposition 缺失）——已 ACK 的输入是静默消失的。logs=['<<< 准备阶段被取消（THINKING 入口之前）— 无状态需要收口']` | ✅ |
| 5 | `test_stop_button_clearing_pending_is_attributable` | **PRE_FIX_RED**（AD2/AD5） | ❌ `pending 未在预期时间内达到 1 条` | ✅ |
| 6 | `test_cancel_after_commit_but_before_history_gets_terminal_disposition` | **PRE_FIX_RED**（AD2 第二形态，§9.2） | ❌ 只有"准备阶段被取消（THINKING 入口之后）"，无任何关于这条输入的终态 | ✅ |
| 7 | `test_cancel_after_history_append_does_not_duplicate_processing` | **PRODUCT_CONTRACT_GUARD**（AD4 边界另一侧） | ❌ **前提级红**（修前没有"输入已进历史"这一可判定的状态——`note`） | ✅ |
| 8 | `test_ownership_judgement_is_never_stale_for_a_new_turn` | **PRODUCT_CONTRACT_GUARD / FALSIFICATION_GUARD**（AD7，Challenger C 的回归守卫） | ❌ **前提级红**（修前判据一直沿用上一回合的 ctx） | ✅ |
| 9 | `test_turn_ending_without_state_change_still_drains_pending` | **PRE_FIX_RED**（AD8，Challenger A） | ❌ 消息滞留 pending 而 Agent 显示空闲 | ✅ |
| 10 | `test_new_input_does_not_preempt_a_turn_that_owns_its_input` | **PRODUCT_CONTRACT_GUARD**（AD6；杀掉"一律抢占/一律排队"两种错误修法） | ✅（前提不成立：已接管状态不可达） | ✅ |
| 11 | `test_normal_fast_path_unchanged` | **ALREADY_GREEN_GUARD**（AD6） | ✅ | ✅ |
| 12 | `test_busy_agent_still_queues_after_fix` | **ALREADY_GREEN_GUARD**（AD6） | ✅ | ✅ |

> **`note`（诚实标注）**：#7 与 #8 在修复前的**红是前提级的**——它们要断言的"边界正确
> 状态"（输入已进历史 / 新回合已开始执行）在修前的代码里根本建立不起来，因为消息会被
> 抢占吞掉。它们的价值在修复后：**防止修法退化**（#8 恰好杀死"判据陈旧"变异 M6，
> #7 钉住"恰一次"而不是 `<= 1` 的假绿）。**真正的行为级 PRE_FIX_RED 是 #1–#6 与 #9。**

既有套件回归：`core/agent/tests/` + `core/cognitive/tests/` = **556 passed**（§32）。

---

## 24. Challenger Findings

独立 Challenger subagent（对抗式，只读 + `/tmp` 副本）对**已应用修法的版本**做了反证。
它找到 3 个真问题（全部被主 Agent 独立复现并修复），另有多条假绿指认（部分成立）。

| 分类 | 项 | 处置 |
|---|---|---|
| **confirmed（已修）** | **C：判据陈旧**——`_generation_ctx` 只在 `_prepare_turn` 清空，而 `await _process_lock` 是本回合第一个挂起点 ⇒ 停在锁上的新回合沿用上一回合的 ctx ⇒ 判据误报"已接管" ⇒ 第三条消息抢占它，且取消落在取锁的 `__aenter__`（在取消边界之外）⇒ 已 ACK 输入静默丢失 + **零 disposition**。Challenger 用"公开订阅者挂起"确定性复现（`PD turn_b.cancelled=True ... users=[暖场, M3] PD disposition_logs=[]`） | **修**：判据清空移到 `process()` 取锁**之前**（§12(3)）；新增 AD7 守卫 #8 |
| **confirmed（已修）** | **A：新队列无出队触发**——pending 只在 `STATE_CHANGE→IDLE` 出队，而窗口内取消/异常时相位**仍是 IDLE**（`handle_interrupt` no-op）⇒ 被本批守卫排队的已 ACK 消息永久滞留，Agent 却显示完全空闲。两条生产形态：`switch_llm` 的 `cancel_current()` 与 P0-3 `StorageUnavailableError`（实测 `pending=['M2'] fsm=idle current_task=None bus=0`） | **修**：回合任务 done callback 里加 `_drain_pending_after_turn()`（同步、复用既有出队路径、不新增调度器/轮询）；新增 AD8 守卫 #9 |
| **confirmed（已修）** | **D：归因可以是错的/空的**——`cancel_current()` 无 source 时沿用上一发的 `_interrupt_source`（实测 `voice_cancel` 被误报成后续无 source 取消的原因）；窗口内**异常**路径完全没有终态（`StorageUnavailableError` 下 M1 无声消失，只有"存储依赖不可用"） | **修**：无 source 时重置为 `"unknown"`；异常路径补 `failed_pre_ownership`（§12(5)(6)） |
| **fake-green（成立，已改）** | FIFO 断言**顺序盲**：`[t for t in ("M1","M2") if any(t in u for u in users)]` 按期望顺序重建 ⇒ `['M2','M1']` 也会通过 | **改**：按**实际处理顺序**取（`for u in users for t in (...)`） |
| **fake-green（成立，已改）** | AD3 的重复断言是单边 `<=`；实测未变异代码里计数是 **0/0**（"一次都没处理"同样通过） | **改**：双向断言"**恰一次**"（session 恰 1 条、LLM 恰 1 次），并拆成"边界之后"的专门用例 #7 |
| **fake-green（成立，已改）** | `test_normal_fast_path_unchanged` 在 `process_message` 返回后立刻断言 pending 为空——v1 路径上无 await ⇒ 消费者还没跑，该断言**恒真**、无判别力 | **改**：改为断言它**进了总线**（`bus.pending_user_inputs == 1`），这才是 fast path 与排队分支的真实差异 |
| **FAKE-GREEN 指认不成立（reclassified）** | "AD2/AD4 是 log-only，在 M1（回退守卫）下仍绿"——AD2/AD4 的职责本来就是**终态可归因**（那条日志），不是守判据；守判据的是 AD1/AD3。把两者混为一谈会得出"日志没用"的错误结论 | 分类澄清（§23 的表按"这条用例钉的是什么"重列） |
| **COULD NOT FALSIFY** | **B：重复处理**——`_handle_interrupt` 的 drain 只搬移不复制；`_handle_state_change` 的 `pop(0)` 与 `bus.post` 之间无 await ⇒ 不存在"pending + 总线各一份"或"投递两次"；Challenger 反而发现**反向**误用（零处理，即上面的 A/C） | 接受（无洞） |
| **accepted risk** | `message_id=-`：v1/REST 输入没有预分配 ID ⇒ 终态日志的身份最弱。修它要动 v1 的 ID 分配链，超出 KB-24 scope | **登记**（§36） |
| **new out-of-scope** | 连发三条时 pending 内的既有乱序（`['M3','M2']`）——两条都有 owner、只是次序错，修法前后一致 | **登记 KB-38**，用例 #3 钉住现状 |

**Challenger 的结论原文**（保留以便追溯）：

> the ordering fix is real for the exact interleaving it tests, but it is incomplete in two
> independent ways: (i) the ownership predicate is stale in the lock-wait segment …
> (ii) the queue it now fills has no drain trigger when the in-flight turn dies in the window …

两条都已被 §12(3)/(4) 修掉，并各配一条守卫（#8/#9），两条守卫在对应变异（M6/M7）下**变红**（§25）。

## 25. Mutation Evidence

变异在**独立副本** `tmp/b12-mut`（从修好的 worktree 复制）上执行，每个变异前从 `tmp/b12-wt`
恢复原始文件；主 worktree 从未被变异。命令：
`conda run -n Elysia python -m pytest core/agent/tests/test_admission_input_delivery.py -q -m service`

| 变异 | 内容 | 结果 | 杀它的用例 |
|---|---|---|---|
| **M0** | 无变异 | **12 passed** | —（基线） |
| **M1** | 回退抢占判据为 `prev not in self._started_cognitive_tasks` | **5 failed** | #1 #2 #3 #5 #9 |
| **M2** | 判据改成"一律排队"（`or True`） | **1 failed** | #10（已接管回合不被抢占）；配合 #8 |
| **M3** | `current_turn_owns_input` 恒 True | **6 failed** | #1 #2 #3 #4 #5 #9 |
| **M4** | 去掉 `cancelled_pre_ownership` 日志 | **1 failed** | #4（单条覆盖，但确定被杀） |
| **M6** | 去掉 `process()` 取锁前的清空（Challenger C 的修复点） | **6 failed** | #1 #2 #3 #4 #5 #9 |
| **M7** | 去掉回合结束后的 pending 兜底出队（Challenger A 的修复点） | **1 failed** | #9 |

**M5（"错误 requeue 一切"）**：本批**没有** requeue 路径，故该形态由 #4 的
"不 requeue / 之后不得被处理"双向断言覆盖（早期版本实测 1 failed ✅）。

> **M2 曾经存活**（第一轮变异：`or True` 无任何用例变红）。根因是当时**没有任何**用例钉住
> "已接管输入的回合不被新输入抢占"这一既有语义。现在 #8 与 #10 覆盖它。#8 本身在 M2 下也
> 变红，因为"一律排队"会让新回合先接管再被判定，从而触发判据新鲜度断言。**这条记录保留**：
> 它是一次真实的"变异体存活 → 补守卫"闭环，而不是事后叙述。

---

## 26. Task / Queue Leak Evidence

每条新用例结束时的检查（`_drain_turns` 收尾）：

- 无孤儿回合任务：`agent._cognitive.current_task` 为空或已 done；
- 无 detached recovery 任务（本批**没有**任何 `create_task` / done-callback 补偿逻辑 ⇒ 结构上不存在）；
- `_pending_messages` 为空（除被显式断言"被停止按钮清空"的用例）；
- `bus.pending_user_inputs == 0`；
- `_process_lock` 未被持有；
- `_current_task` 指向 dead task 的既有现象（KB-26）**未**在本批引入/扩大：本批只读该句柄，
  不改变它的清空时机（`cancel_current` / `reset_lifecycle_state`）。

---

## 27. Public API / Schema Impact

**零**：无 `frontend_contract` 改动、无 wire 新字段/新枚举、无 REST 变化、无 `make gen-types` 重生成需求。
新增的 `CognitiveLoop.current_turn_owns_input` 是 **core 内部**公开属性（与既有
`current_task` / `process_lock` / `current_generation_user_message_id` 同层），不进契约层。

---

## 28. Persistence Impact

**零**：无 schema 变化、无 migration、无新集合/索引、不建 durable inbox、不写任何新文档。
`sessions` / `message_archive` 的写入时机**未变**（仍在 `_build_turn_prompt`）。

---

## 29. Performance Impact

- 正常快速路径：**零新增网络请求、零新增 DB 写、零轮询**；
- 新增开销是常数级的属性读取（`_generation_ctx is not None`）＋一次 `getattr` 分支；
- 新增日志只在**异常路径**（取消终态 / 中断丢弃 / 队列满）产生，正常消息不新增 INFO 级日志（§98）。

---

## 30. Commits

| 顺序 | commit | 内容 |
|---|---|---|
| 1 | `16547090` `fix(agent): preserve accepted input ownership across admission cancellation` | 生产修复 + **12 条**守卫（同一 commit，§108）：`core/cognitive/loop.py`、`core/agent/agent.py`、`core/agent/tests/test_admission_input_delivery.py` |
| 2 | `631d974b` `docs(architecture): record accepted-input delivery invariants` | `docs/architecture/invariants.md` §4.5 AD1–AD8 / O1 关闭 |
| 3 | `22fbc027` `docs(review): record Batch 12 accepted input delivery closure` | 本执行记录 |
| 4 | `ea3d87d2` `docs(todo): close KB-24 as GUARDED and register KB-38..KB-45` | todo 更新（KB-24 → GUARDED/HIGH；新增 KB-38–KB-45） |

全部落在 `main`（本仓既有做法：worktree 只做隔离验证）。`git status` 干净。

---

## 31. Worktree Verification

- 独立 worktree：`git worktree add tmp/b12-wt HEAD --detach`（`tmp/` 已 gitignored，且
  `pytest.ini::norecursedirs` 排除它 ⇒ 不会污染收集）；
- 流程：**HEAD + new tests → PRE_FIX_RED**（pristine HEAD 上 **9 failed / 3 passed**，
  红全部落在断言或前提上，**无** `AttributeError` 类的"符号不存在"假红——
  用例用 `getattr(loop, "current_turn_owns_input", …)` 兜底）
  → **apply production patch → 12 passed**
  → mutations M0–M7（`tmp/b12-mut` 副本，主 worktree 未变异）
  → Challenger 反证（`tmp/b12-chal` 副本）→ 三个洞修复 → 反证前提失效；
- 主工作树 `/home/yomu/elysia-agent` 在探针期被临时回退过一次（用于在 pristine HEAD 上
  跑探针），已立即从 worktree 恢复；收尾核对 `git -C /home/yomu/elysia-agent status --short`
  **干净**（改动只存在于 worktree，待提交）；
- 提交按 §30 落到 `main`（本仓既有做法：worktree 只做隔离验证）。

## 32. Test Results

| 层 | 命令 | 结果 |
|---|---|---|
| **targeted（本批新增）** | `pytest core/agent/tests/test_admission_input_delivery.py -m service` | **12 passed**（修前：9 failed / 3 passed，红全部落在断言或前提上） |
| **既有 agent + cognitive** | `pytest core/agent/tests/ core/cognitive/tests/ -m service` | **560 passed**（82s；Batch 11 时该口径为 556 ⇒ +4 净增） |
| **quick（全仓 unit+service）** | `pytest -m "unit or service" -q -n 8 --dist loadfile` | **6217 passed / 2 skipped / 2 xfailed** |
| **normal（not slow and not external）** | `pytest -m "not slow and not external" -q -n 8 --dist loadfile` | **6217 passed / 2 skipped / 2 xfailed** |
| **full（含 slow）** | `pytest -q -n 8 --dist loadfile` | **6249 passed / 36 skipped / 2 xfailed**（79s） |
| **real-mongo 层** | `make test-real-mongo` | **24 passed**（`docker-compose.test.yml`；本批零 schema 改动，属旁证） |
| **static gates** | 12 条（§32.1） | 全绿 |
| **frontend build** | `cd frontend && npm run build` | ✅ `✓ built in 874ms`（前端本批零改动） |
| **real CI** | owner push 后确认 Backend / Real MongoDB / Frontend / contract-frames | 见 §32.4 |
| **Challenger 反证套件** | `pytest core/agent/tests/test_chal_admission.py -m service`（`tmp/b12-chal`，**不进仓**） | 修前 **1 failed / 6 passed** → 修后 **4 failed / 3 passed**：4 条反证里有 3 条的前提在修后**不再成立**（洞已闭合），1 条（`AD3_COUNTS 0/0`）被本批的"恰一次"断言取代 |

### 32.1 静态门禁（逐条实测）

| 门禁 | 结果 |
|---|---|
| `make lint` | ✅ `All checks passed!` |
| `make pyright` | ✅ `0 errors, 0 warnings, 0 informations` |
| `make check-arch` | ✅ R1/R2/R3/R4/R6 全过 |
| `make check-contract` | ✅ 枚举成员全部可达或在册预留（23 个）；无新增"有读无写" |
| `make check-types` | ✅ 契约三产物 + 17 golden frames + 错误码单源（54 码）无漂移 |
| `make check-timeouts` | ✅ R1/R2(4)/R3(叶子 9)/R4 全过（本批**未新增**任何具名超时） |
| `make check-debug-auth` | ✅ 21 个文件 |
| `make check-secrets` | ✅ 无凭据形态命中 |
| `make check-test-sets` | ✅ `|Quick ∪ remainder| = 6221 == |Normal|`；全量收集 6287 |
| `make check-compose` | ✅ 两个 compose 文件均过 |
| `make check-env-lock` | ✅ requirements 20 个顶层包均有精确 pin |
| `make check-ci-isolation` | ✅ ci.yml / contract-frames.yml / nightly.yml 三个 workflow 均过 |

### 32.2 运行记录（逐条实测）

| 项 | 命令 | 结果 |
|---|---|---|
| **targeted（本批新套件）** | `pytest core/agent/tests/test_admission_input_delivery.py -m service` | ✅ **12 passed**；修前（pristine HEAD）**9 failed / 3 passed** |
| **targeted（agent + cognitive 全量）** | `pytest core/agent/tests/ core/cognitive/tests/ -m service` | ✅ **560 passed**（82s） |
| **async ordering（§111 的时序矩阵）** | 见下 | ✅ 7 组全部屏障驱动 |
| **random stress（§92，辅助）** | 未做 | **如实记录**：本批判据是"屏障钉死的确定性时序"（ACK→cancel / cancel→ACK / task create→cancel / first-step→cancel / M1 recover→M2 / M3 / stop→cancel / cancel→stop），随机化不会增加信息量；且随机 cancel 无法给出"窗口内"这一前提 |
| **fresh process（§112）** | **NOT APPLICABLE** | `Agent` / `CognitiveLoop` / `EventBus` / `_pending_messages` / admission 身份全是**实例级**对象，无模块级队列或单例；用例逐条自建 Agent |
| **real storage（§106）** | `make test-real-mongo` | ✅ **24 passed**（`docker-compose.test.yml` 起 `elysia-mongo-test`；本批无新 schema，属旁证） |
| **`make test-quick`** | `pytest -m "unit or service" -q -n 8 --dist loadfile` | ✅ **6217 passed / 2 skipped / 2 xfailed**（79s，提交后的 main） |
| **`make test-normal`** | `pytest -m "not slow and not external" -q -n 8 --dist loadfile` | ✅ **6217 passed / 2 skipped / 2 xfailed**（80s；本仓 Normal ≡ Quick，`test-remainder` 为 0 条） |
| **`make test-full`** | `pytest -q -n 8 --dist loadfile` | ✅ **6249 passed / 40 skipped / 2 xfailed**（提交后的 main） |
| **frontend build** | `cd frontend && npm run build` | ✅ `vue-tsc --noEmit && vite build` 退出 0（`✓ built in 861ms`；前端本批**零改动**） |
| **static gates** | §32.1 的 12 条 | ✅ 全绿 |
| **Challenger 反证套件** | `pytest core/agent/tests/test_chal_admission.py -m service`（`tmp/b12-chal`，**不进仓**） | 修前 **1 failed / 6 passed** → 修后 **4 failed / 3 passed**：4 条反证里 3 条的前提在修后**不再成立**（洞已闭合，前提断言变红），1 条（`AD3_COUNTS 0/0` 的 `<=` 假绿）被本批"恰一次"断言取代 |

### 32.3 §111 时序矩阵（全部屏障驱动，零 sleep 造 race）

| 时序 | 用例 | 屏障/判据 |
|---|---|---|
| ACK → cancel（stop） | #4 | `CharacterRuntimeRegistry.get_or_load` + 真 INTERRUPT 事件 |
| ACK → cancel（第二条抢占） | #1 #2 #3 | 同上 + 真实入口第二条消息 |
| cancel → ACK 边界（窗口之前） | AD5/§44 由既有 `test_process_start_clears_stale_generation_ctx`（F5）覆盖：入口之前的取消保持传播、不产生 adopted 状态 | — |
| ACK → task create → cancel（pre-start） | 既有 C1/P1-06 用例（`test_multi_session.py::test_prestart_cancel_releases_lease_via_done_callback`）+ 本批 #1（判据同时看 `_started_cognitive_tasks`） | — |
| ACK → first-step → cancel（lock / prepare） | #1 #2 #3 #9（`get_or_load` 屏障）/ #6（`generation_repo.upsert` 屏障） | — |
| M1 recover → M2 pending | #1 #2 | — |
| M1 recover → M2/M3 pending | #3（钉住既有乱序 + 三条都不丢） | — |
| shutdown → cancel / cancel → shutdown | §20 + 既有 `test_shutdown_*`（`_stop_event_task` 先停消费者 ⇒ 后续 INTERRUPT 无人消费） | — |
| 已接管 → 新消息（不抢占） | #10（`llm.stream_entered` 屏障） | — |

### 32.4 Real CI 状态

本机无 GitHub 凭据（与 Batch 4–11 同一环境限制）⇒ 本批提交已就绪，**真实 CI 由 owner push 后触发**。
push 后需核对的 required job（`docs/ci.md`：本仓只有 push 触发器）：

| job | 期望 | 本批相关面 |
|---|---|---|
| `backend` | 绿 | 全部静态门禁 + `test-quick` / `test-normal`（本机等价证据见 §32.1/§32.2；12 条新用例都带 `service` marker ⇒ 落在 Quick ⊆ Normal） |
| `real-mongo` | 绿 | 本批**零 schema 改动**；本机 `make test-real-mongo` **24 passed** |
| `frontend` | 绿 | 前端零改动；本机 `npm run build` 退出 0 |
| `contract-frames` | 绿 | 契约层零改动；本机 `make check-types` 绿 |

## 33. Invariant Mapping

| 本批新编号 | 内容 | 守卫 |
|---|---|---|
| **AD1** | accepted 输入始终有 owner 或显式终态 | 用例 #1 #3 #9 |
| **AD2** | commit point 之前（以及之后、进历史之前）的取消必须落可归因的显式终态 | 用例 #4 #5 #6 |
| **AD3** | 未接管输入的在途回合不得被抢占（新输入排队） | 用例 #1 #2 |
| **AD4** | admission 交接原子：同一条输入不得被处理两次 | 用例 #7 |
| **AD5** | 显式取消/停机不得复活本应取消的输入 | 用例 #4 #5 |
| **AD6** | 正常快速路径、忙时排队、以及"已接管回合不被新消息抢占"三条既有语义不变 | 用例 #10 #11 #12 |
| **AD7** | ownership 判据不得陈旧（新回合一开始执行就不再冒领上一回合的 ctx） | 用例 #8 |
| **AD8** | 排队消息不得因"在途回合从未离开 IDLE"而永久滞留 | 用例 #9 |

对应 §9 的 AI1–AI6（按仓库编号规范落为 **AD** 前缀：`docs/architecture/invariants.md` §4.5）。
AI7（shutdown 不得复活）并入 **AD5**；AI8（普通异常）**没有**单独编号——窗口内异常与取消
同 root，由 **AD1/AD2** 一起覆盖（§21 说明了为什么不单独立项：异常路径本来就有可观测的
任务终态，缺的只是那条输入的归因，已由 `failed_pre_ownership` 补上）。
**AD7/AD8 是 Challenger 反证的产物**（不是最初设计的编号）——它们各自对应一个"修法自身
引入的新洞"，都有变异体证明守卫必要（M6 → AD7，M7 → AD8）。

---

## 34. Todo Update

- **KB-24** → **GUARDED**（Batch 12），严重度 LOW → **HIGH**，Evidence E2(10) → **E2(12)**。
- 新增登记见 §36。

---

## 35. Keep As-Is

- **EventBus 架构**：无界优先队列 / 单消费者 / INTERRUPT 插队 / drain 表——**未改**；
- **FSM 架构**：无新状态、无新转换边、`finish_turn` / `handle_interrupt` 未改；
- **`_process_lock`**：仍是回合串行化 authority，本批**只读**它表达的不变量，未改语义；
- **既有 pending 机制**：`_pending_messages` + `STATE_CHANGE→IDLE` 出队——**未改**（只补归因日志）；
- **ACK wire schema**：零变化（含 `rejected`/`error` 信封与幂等表语义）；
- **无 durable queue / 无 retry framework / 无 watchdog / 无新轮询**；
- **前端**：零改动（本批不引入任何 UI 状态）；
- **`_current_task` 的 stale 语义（KB-26）**：未顺手修。

---

## 36. New Findings（只登记，不修）

| ID | 描述 | 类别 |
|---|---|---|
| **KB-38** | **连发三条时 pending 内的既有乱序**：上一条还在 EventBus 队列里时，`process_message` 因 `bus.pending_user_inputs > 0` 把新消息直接 append 到 `_pending_messages`，随后消费者才 append 旧消息 ⇒ `['M3','M2']`。两条都有 owner，只是次序错；修法前后一致（**非**本批引入）。守卫 `test_third_message_arriving_while_second_is_still_on_the_bus` 钉住现状 | 零散（ordering） |
| **KB-39** | **`accepted` 语义无书面合同**：契约 schema/文档只描述 happy path（"accepted → 后续 generation.\* 事件"），没有任何地方说明"是否承诺处理 / 承诺到哪一步"；`idempotency_durable` 读起来像"消息已持久化"，实际只覆盖 claim（TTL 1h） | 文档 |
| **KB-40** | **客户端无"accepted 但从未处理"的终态**：Electron 在 ACK 上就 settle（`real-backend.ts:664-687`），WebUI 根本不消费 ack。本批的终态只在服务端日志里 ⇒ 用户面仍无法区分"被取消"与"从未受理" | 前端/契约 |
| **KB-41** | **`_handle_task_result` / `_handle_system_tick` 的抢占无 ownership 判据**：与 `_handle_user_input` 不同，这两处直接 `cancel_current()`，可抢占一个**未接管输入**的用户回合（后台任务终态唤醒 / 主动搭话 tick 落在窗口上） | 零散 |
| **KB-42** | **语音取消的门排除了窗口**：`voice_sessions.py:425` 要求 `not is_completely_idle`，而 admission 窗口内相位恰是 IDLE ⇒ 语音取消在窗口内**不触发**（当前无害，但语义是"相位代理"而不是"是否有在途回合"） | 零散 |
| **KB-43** | **`_handle_user_input` 队列满分支的静默丢弃**（修前）：post-ACK 的容量竞争丢弃既无日志也无计数。本批补了 WARNING 归因，但**未**定义产品级策略（是否该优先插回 / 是否该形成 rejected 帧） | 产品决策 |
| **KB-44** | **v1/REST 输入的终态归因身份为空**：`_admission_message_id` 取自 v2 预分配的 `messageId`；v1/REST/STT 路径没有这个 ID ⇒ 终态日志只有 `conversation_id`。身份最弱的地方恰是最需要归因的地方 | 零散（可观测性） |
| **KB-45** | **`_handle_user_input` 的抢占分支（`cancel_current()`）当前不可达**：`process_message` 只在相位 IDLE 时才 `bus.post`，而相位离开 IDLE 之后回 IDLE 的同一同步段里就会发出下一个回合——所以"相位 IDLE + 有存活且在跑的回合"这一前提只在 KB-28 那条需要挂起订阅者的窗口里成立。本批因此**保留**它作为租约/孤儿任务的失效保护，并在注释里写明它**不是**用户级中断机制（中断走 INTERRUPT 事件） | 零散（待复核） |

---

## 37. Follow-up（保留，不动）

KB-26 / KB-28 / KB-29 / KB-30 / KB-32 / KB-33 / KB-18–21 / KB-12 / KB-13 /
AR-16（Ollama Provider Adapter）/ KB-15 / KB-16 / KB-17 / OBS-6 / OBS-7 / OBS-8 /
KB-11 / GR-25 / N-15，以及 canonical backlog 的其余条目**均未在本批触碰**。
