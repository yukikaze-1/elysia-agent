# Batch 13 — Pending Input Ordering Correctness（KB-38）执行记录

- 日期：2026-09-19
- canonical scope：**KB-38**（pending / admission 顺序）
- worktree 验证：`tmp/b13-wt`（HEAD `873c2f77` ＋ 本批测试 → **9 failed / 16 passed**；＋ 本批生产改动 → **25 passed**）
- 攻击沙箱：`tmp/b13-chal-a`（Challenger A，两轮）、`tmp/b13-chal-b`（Challenger B）、变异 `tmp/b13-mut`、历史对照 `tmp/b13-pre12`（`2e70854d`）
- 相关：KB-24（Batch 12 已修，本批窗口是它的邻域）、KB-28 / KB-45（机制交界，**未**关闭）
- 结论：**KB-38 = CONFIRMED / GUARDED**。修复经两轮独立 Challenger 反证（A 第一轮 **FALSIFIED** 一次、第二轮又 **FALSIFIED** 一次停滞），两次反证都已修复并各配守卫。

---

## 1. Executive Summary

| 问题 | 结论 |
|---|---|
| KB-38 confirmed? | ✅ **CONFIRMED**（当前 HEAD 独立 E2：admission `[M1,M2,M3]` → 处理顺序 `[M1,M3,M2]`） |
| classification | **ORDERING DEFECT + LIVENESS DEFECT（同批修复）/ GUARDED** |
| evidence | E2（真 `Agent.start()` ＋ 真 `process_message` ＋ 真 EventBus ＋ 真 pending，无 sleep 造序）＋ 五层身份（admission / pending / claim / session / LLM）＋ 变异 M1–M10 ＋ 两个独立 Challenger（随机化 2400 轮） |
| ordering contract | **既有 FIFO 承诺的贯通**（`process_message` / `pending_messages` / `_dispatch_pending_message` docstring ＋ AD3；前端按发送顺序渲染且无 priority 指示）。本批**不新增**合同 |
| ordering domain | **单个 Agent 实例**的 accepted 输入流（一个 FSM / 一个 `_pending_messages` / 一个 `_current_task` / 一把 `_process_lock` ⇒ 回合全 Agent 串行，与 session、连接无关） |
| production fix? | ✅ 三处最小改动：① 入队收敛到**单一权威** `_enqueue_pending`（按 admission 顺序插入）；② 越队判据看**完整等待集**（pending ∪ 总线驻留），`process_message` 与 `_handle_user_input` 共用同一定义；③ 兜底出队挂到**每一个**回合 spawn 点（活性） |
| user reachability? | ✅ 连续发送（前端连发）；**STT/语音直投**（`ws_handler.py:733` / `ws_v2.py:787` / `voice_sessions.py:702`，不经 `process_message`）；proactive 回合窗口 |
| public API / 契约 | **零** schema / wire 变化（`process_message` 签名与返回值不变）；一处**语义时序**变化：队列满时由"先 accepted 再被丢弃"变成**立即 QUEUE_FULL 拒收**（见 §20） |
| persistence | **零**（无新字段、无库写入、无迁移） |
| real CI | 见 §31（owner push 后确认） |

---

## 2. Original Backlog Evidence（canonical 描述 vs 当前 HEAD 复核）

canonical（`todo.md:57`）：

> **KB-38**：**连发三条时 pending 内的既有乱序**——上一条消息还排在 **EventBus 队列**里
> （事件消费者尚未把它交给 `_handle_user_input`）时，`process_message` 因
> `bus.pending_user_inputs > 0` 把**新**消息直接 append 进 `_pending_messages`；随后消费者才把
> **旧**消息经 `_handle_user_input` 也 append 进去 ⇒ 两条在 pending 里**反序**（实测 `['M3','M2']`）。
> 与 KB-24 同族但**不同根**……修法前后行为一致（**非** Batch 12 引入）。

**当前 HEAD 复核（本批独立 E2，`tmp/batch13/probe_kb38.py`）**：

| 层 | 修前（873c2f77） | 修后 |
|---|---|---|
| admission order | `['M1','M2','M3']` | `['M1','M2','M3']` |
| pending（release 前） | `['M3','M2']` | `['M2','M3']` |
| turn_enter（coroutine 首步） | `['M1','M3','M2']` | `['M1','M2','M3']` |
| **claim（canonical 处理点）** | **`['M1','M3','M2']`** | `['M1','M2','M3']` |
| session 历史顺序 | `['M1','M3','M2']` | `['M1','M2','M3']` |
| LLM 实际回合顺序 | `['M1','M3','M2']` | `['M1','M2','M3']` |

⇒ backlog 摘要**成立**且比登记更重：不只是"pending 内次序错"，**实际处理顺序**（claim / session /
LLM 三条独立证据）都反了。

**边界对照（§10/§44）**：

| 场景 | 修前 | 修后 |
|---|---|---|
| O1 普通忙时排队（M1 已接管、在产出） | `M1 → M2 → M3` ✅ | 不变 ✅ |
| O2 admission 窗口 / 总线驻留（M1 未接管） | **`M1 → M3 → M2`** ❌ | `M1 → M2 → M3` ✅ |
| 两条消息边界 | `M1 → M2` ✅ | 不变 ✅ |

⇒ 乱序**只**出现在 O2；O1 与两条边界一直是对的（这两条因此是 ALREADY_GREEN 守卫）。

**"是否早已存在"——本批实测（`tmp/b13-pre12` = worktree `2e70854d`，Batch 12 修复之前）**：
同一用户形态（M1 停在 admission 窗口、M2/M3 经真 public `process_message` 连发）：

```text
[PRE12] M1 window: task alive = True | owns-input predicate exists = False
[PRE12] pending = []                          ← 没有反序：两条都没进 pending
[PRE12] claim order = ['M2', 'M3']
[PRE12] session users = ['M2', 'M3']          ← M1 从未进历史
[PRE12] M1 task cancelled = True | phase = idle
```

⇒ 2e70854d 上这条路径走的是"抢占"分支（KB-24 的消息被吞），**根本没有机会反序**。

**对 canonical 表述的更正**：

- **根判据确实早于 Batch 12**：`a4f590ab`（2026-08-10，"review 复核 #4 / P1-06"）引入
  `bus.pending_user_inputs > 0` → 直投 append，且在 `2e70854d` 与 HEAD 上逐字节相同
  （`git diff 2e70854d HEAD -- core/agent/agent.py | grep pending_user_inputs` 为空）。
  它从一开始就是"按写入时刻排序"，只是被抢占分支掩盖。
- **可观测的反序是 Batch 12 打开的窗口**：Batch 12 把排队判据从"还没 first-step"扩到"尚未
  接管输入"，更早那条不再被抢占、改为稍后 append ⇒ 反序第一次可观测。
- 所以准确表述是：**根判据 pre-existing（order-unsound by construction），可见性由 Batch 12
  的判据放宽打开**。canonical 里"修法前后行为一致（非本批引入）"是**推断而非测量**
  （Batch 12 记录 §31 只测到"9 failed / 3 passed"，守卫 #3 红在"消息被吞"）。

---

## 3. Ordering Contract（Phase 0：先确认，后修）

| 来源 | 原文 |
|---|---|
| `core/agent/agent.py`（`process_message` docstring） | `Agent 忙碌时消息入队（FIFO），IDLE 时出队处理。` |
| `core/agent/agent.py`（`pending_messages`） | `FIFO 队列中暂存的消息（只读快照视图，busy 时排队未出队）。` |
| `core/agent/agent.py`（`_dispatch_pending_message`） | `把队首的排队消息重投进 EventBus（FIFO 出队）。` |
| `core/agent/_types.py`（`_PendingMessage`） | `FIFO 队列中暂存的消息（FSM 忙碌时入队）。` |
| `docs/architecture/invariants.md` AD3 | `未接管输入的在途回合不得被抢占：新输入排队（FIFO 不抢）` |
| `docs/architecture.md` §4.1 | 总线 `seq: int = field(compare=True, default=0)  # FIFO within same priority` |
| `docs/completed/2026-05.md` | `消息排队 FIFO｜busy 时 _pending_messages 入队, STATE_CHANGE idle 时出队, INTERRUPT 清空` |
| 前端 `chatStore.addUserMessage` / `InputBar.sendText` | 用户消息按**发送顺序**立即渲染；无 priority / reorder / "已排队"指示 |
| `docs/electron-backend-protocol-v2.md` | **无**排序条款（`accepted` 只承诺 ack 与后续 `generation.*`） |

判定：**合同 = 队列 FIFO ＋ "新输入排队不抢"（AD3）**；产品的用户可见表示是"有序对话"。
**本批不引入 strict-FIFO 新承诺**，只把既有承诺贯通到 bus↔pending 的交接段。

**不保证**：网络到达顺序（并发/跨连接的到达顺序未定义——合同管的是**已 accepted 的先后**）；
`accepted` 一定被回复（Batch 12 的 `Transport Accepted` 口径）；停机后的复活；持久化。

---

## 4. Ordering Domain

**单个 Agent 实例**。依据：一个 `AgentFSM`（相位）、一个 `_pending_messages`、一个
`CognitiveLoop._current_task`、一把 `CognitiveLoop._process_lock`（覆盖整回合含收尾）⇒
回合在全 Agent 上**串行**，与 session 无关。Challenger B 实测：3 个会话轮流 v2 发 9 条
⇒ `claims == admission 顺序`、**全 Agent FIFO**（不是每会话并行）。跨进程**不**承诺。

---

## 5. Admission Order Definition

**`Agent.process_message()` 提交准入决策的那一刻**：

```text
v2: await self._session_manager.reserve_work(...)   ← 唯一的 await（可能挂起）
    rid = self.next_request_id()                    ← ★ admission 顺序见证（同步、单调）
    分支落地（入队 / 直投总线）                      ← 与上一行之间没有任何 await
```

见证用**既有** `request_id`（`next_request_id()` 的 `{n:03d}` 单调计数器），不新增第二套
message identity（§31）。直投路径同样在用 `next_request_id()`（三个生产调用点都是），
且 `_handle_user_input` 侧的 `data.request_id` 与 `GenerationContext.request_id` 是同值
（`_process_events` 派发前 `set_request_id(rid)`，`_prepare_turn` 用 `get_request_id()`）。

**不用**：websocket 接收微秒顺序、ACK 完成顺序、`clientRequestId`（客户端提供、跨客户端不可比）。

---

## 6. Processing Order Definition（canonical）

**active-turn ownership commit** = `CognitiveLoop._generation_ctx` 建立
（`current_turn_owns_input` 变真），与 Batch 12 的 admission commit / Batch 10 的 ownership
判据**同一点**：带输入身份、在 `enter_thinking()` 之前且与它之间无 await、比 session 历史
追加更早更权威。旁证三层：turn coroutine 首步、session 历史、LLM 实际回合。

---

## 7. Ordering Pipeline（标出 reorder 阶段）

```text
M1/M2/M3（用户连发）
   ↓
public admission  reserve_work → rid → 分支                       ★ 顺序在此定义（无 await）
   ↓
ACK / EventBus post（快速路径）｜ _pending_messages（忙时）
   ↓                                            ★★ A：两个容器、两个 append 权威
EventBus dispatch（同优先级按 seq 严格 FIFO）                        ← 不乱序
   ↓
_handle_user_input
   ├─ queue：按 admission 顺序插入                                  ★★ B：写入时刻 ≠ admission 时刻
   └─ spawn：成为 active turn                                        ★★ C：直投输入可越过等待者
   ↓
turn completion（_process_lock 整回合串行；锁等待 FIFO，但本批不依赖）
   ↓
出队口 ①  STATE_CHANGE → IDLE ｜ 出队口 ②  回合 done callback
   ↓                                            ★★ D：pending 项被 pop 后、被 claim 前
                                                     （"交接态"）对 A/C 的判据不可见
next process
```

**四个乱序/停滞点**：
- **A/B**：入队按写入时刻排序（→ `_enqueue_pending` 修）；
- **C**：直投输入不经 `process_message`，会在 spawn 分支越过等待者（→ 等待集判据修）；
- **D**：`pop(0)` → `bus.post` 之间 pending 已空、总线尚未派发，只看 pending 的判据**失明**
  （→ `EventBus.pending_user_input_request_ids()` ＋ 等待集并集修；这是 Challenger A 第一轮
  反证出的那一层）；
- **E（停滞）**：pending 非空没有自己的触发器，两个出队口都可能不触发（→ 兜底出队挂到每个
  回合 spawn 点；这是 Challenger A 第二轮反证出的那一层）。

---

## 8. Pending Authority Map（修前 / 修后）

| 动作 | 修前 | 修后 |
|---|---|---|
| append（admission 时刻） | `process_message` 直接 `list.append` | `_enqueue_pending()` ← **唯一入队权威**（按 admission 顺序插入） |
| append（派发时刻） | `_handle_user_input` 直接 `list.append` | 同上 |
| pop / claim | `_dispatch_pending_message`：`pop(0)` → `bus.post`（同步、无 await） | **不变**（列表有序 ⇒ `pop(0)` = 取最老） |
| 越队仲裁 | **无** | `_has_older_waiting_input()`：等待集（pending ∪ 总线驻留）里有更早者 ⇒ 排队。**两个入口共用**（`process_message` 的快速路径判据 + `_handle_user_input` 的 spawn 判据） |
| 出队触发 | ① `STATE_CHANGE→IDLE` ② **仅用户输入回合**的 done callback | ① 不变 ② **所有**回合 spawn 点（新增 `_on_auxiliary_turn_done` 挂到 `_handle_system_tick` / `_handle_task_result`） |
| 清空 | `_handle_interrupt` / `stop()` | **不变**（可归因、释放 lease） |
| 容量 | `len(_pending_messages) >= max_pending_messages` | **不变**（§42：不重做 queue policy；触发条件因此更常命中，见 §20） |
| 总线可见性 | 只有计数 `pending_user_inputs` | ＋ 只读快照 `pending_user_input_request_ids()`（**不改**优先级/派发/drain） |

---

## 9. Pre-Fix Reproduction / Falsification（正常 burst 与 recovery burst 分开）

| 场景 | 构造 | 修前 | 修后 |
|---|---|---|---|
| **O2 三条 burst** | M1 停在 admission 窗口（`_Gate` 包 `CharacterRuntimeRegistry.get_or_load`），M2/M3 经 public 入口连发 | pending `['M3','M2']`；claim `['M1','M3','M2']` ❌ | `['M2','M3']`；`['M1','M2','M3']` ✅ |
| **O2 burst 5** | 同上，M2–M5 | pending `['M3','M4','M5','M2']` ❌ | `['M2','M3','M4','M5']` ✅ |
| **O1 普通忙时排队** | M1 已接管并在产出（LLM 流屏障） | `['M1','M2','M3']` ✅ | 不变 ✅ |
| **recovery burst** | M1 在窗口内 fail-closed（`StorageUnavailableError`） | claim `['M3','M2']` ❌ | `['M2','M3']` ✅ |
| **直投越队（PO3）** | M1 已接管并在产出；M2 排队；V1 以生产形态直投 `bus.post(UserInputData)` | M1 被抢占（`cancelling()==1`），V1 越过 M2 ❌ | M1 不被抢占；claim `['M1','M2','V1']` ✅ |
| **交接态越队（Challenger A 第一轮）** | M1 产出中；M2 排队；V1 直投；**出队口把 M2 交给总线**（消费者被延迟） | claims `['M1','V1','M2']` ❌ | `['M1','M2','V1']` ✅ |
| **`process_message` 变体** | 同上但更晚者走公开入口，两个出队口都被延迟 | 更晚者抢到更小总线 `seq` ❌ | 入队，`['M2','M3']` ✅ |
| **并发入口** | 两个 v2 入口 `asyncio.gather` 同时投递 | claim 顺序 ≠ rid 单调 ❌ | == rid 单调 ✅ |
| **proactive 回合窗口内失败（Challenger A 第二轮）** | `SystemTickData` 起主动搭话回合 → 停在窗口 → T1 排队 → 回合 fail-closed（从未离开 IDLE） | T1 **永久静音**（两个出队口都不触发）❌ | T1 重新获得处理权 ✅ |
| **两条边界** | M1 窗口 + M2 | `['M1','M2']` ✅ | 不变 ✅ |
| **停机** | M1 产出中、M2/M3 排队 → `stop()` | 清空 pending、不起回合 ✅ | 不变 ✅ |

---

## 10. User Impact（真实乱序例子）

```text
用户: M1  → 助手: （正在处理 M1…）
用户: M2  → （accepted，但排在总线里尚未归属）
用户: M3  → （accepted，直接进排队）
实际: 助手按 M1 → M3 → M2 回复（session 历史也是 M1,M3,M2）
```

第二条真实路径更常见：**助手正在回答时用户先打字、再说话**（STT/语音直投 不经
`process_message`）：说话那条会抢占当前回合并**越过**已排队的打字消息（修前实测
`cancelling()==1`）。第三条：主动搭话回合在窗口内失败后，队列**失去触发器** ⇒ 已 accepted
的输入永久静音（修前实测 `pending=['T1'] fsm=idle claims=[]`）。

严重度维持 **LOW–MEDIUM**（顺序）／**HIGH**（静音，但需要"proactive 回合 + 窗口内存储失败"叠加）：
不丢数据、不需要崩溃，但都是可观测的正确性/可用性偏差。

---

## 11. Root Cause

**不是** EventBus（同优先级按 `seq` 严格 FIFO）、**不是** task 调度（coroutine 首步与锁等待都
FIFO）、**不是** `STATE_CHANGE→IDLE` 的 broadcast/lock 窗口（KB-28，本批未触及）。

**根因一：入队顺序依据错了**——两个 append 权威按**写入时刻**排序（admission 时刻 vs 派发时刻）。

**根因二：等待集只数了一半**——"在等"的形态有两个容器（`_pending_messages` ∪ 总线驻留），
而判据只看前者。更晚的输入在"pending 项已 pop、尚未被 claim"的交接窗口里能拿到更小的总线
`seq`（直投甚至不经 `process_message`），于是先被派发、先成为 active turn。

**根因三（活性）：pending 非空没有自己的触发器**——两个出队口（`STATE_CHANGE→IDLE`、
回合 done callback）在"回合停在窗口内失败且从未离开 IDLE ＋ 该回合来源不带 done callback"
时**都不触发**；修前靠"后续输入走快速路径起回合、顺带排空队列"自愈，而根因二的判据会让后续
输入也排队 ⇒ 从"自愈"变成"永久静音"。

三条的修复都是**收口**（单一入队权威 / 单一等待集定义 / 单一兜底出队挂点），不是新增调度器。

---

## 12. Fix

### before

```python
# ① 两个 append 权威，按写入时刻排序
self._pending_messages.append(_PendingMessage(...))          # process_message（admission 时刻）
self._pending_messages.append(_PendingMessage(...))          # _handle_user_input（派发时刻）

# ② 越队判据只看 pending（且根本没有这条判据）→ spawn 分支直接抢占
self._cognitive.cancel_current()
task = self._turns.spawn(...)                                 # 直投输入在这里越过等待者

# ③ 只有用户输入回合带 done callback
task.add_done_callback(_lease_release_done)                   # _handle_user_input
task = self._turns.spawn(..., name="cognitive-proactive")     # _handle_system_tick：无
task = self._turns.spawn(..., name=f"task-result-…")          # _handle_task_result：无
```

### after

```python
# ① 入队：唯一权威，按 admission 顺序插入
def _enqueue_pending(self, pending): ...                      # 键 = int(request_id)

# ② 等待集（pending ∪ 总线驻留）里有更早者 ⇒ 排队；两个入口共用
if not self.status.is_accepting_input or self._has_older_waiting_input(rid):   # process_message
    ..._enqueue_pending(...); return True
...
if not_owning or self._has_older_waiting_input(data.request_id):               # _handle_user_input
    ..._enqueue_pending(...); return

# ③ 兜底出队挂到每个回合 spawn 点（非用户输入来源）
task.add_done_callback(self._on_auxiliary_turn_done)          # proactive / task-result

# ④ 总线只读快照（不改优先级/派发/drain）
def pending_user_input_request_ids(self) -> tuple[str, ...]: ...
```

### why minimal

- 生产改动只在两个文件、四个点：`agent.py` 的入队/判据/兜底回调，`event_bus.py` 的一个只读快照；
- **不动** 优先级 / 派发顺序 / drain 语义 / pending 容器 / 容量策略 / FSM / `_process_lock` /
  wire 契约 / 持久化 / 输入层三条直投入口；
- fast path 额外开销 = 一次判据（pending 空且总线空时 ≈170 ns，见 §28）；
- 三条根因各由一个**单一权威**收口，而不是新增调度层。

---

## 13. Single-Claim Semantics

pending 的唯一入队权威 = `_enqueue_pending`；唯一 claim 点 = `_dispatch_pending_message`
（`pop(0)` → `bus.post`，同步无 await ⇒ 不可能同时属于 pending 与总线）。两个 drain 源只是
同一函数的两个触发点（本批把触发点补齐到所有回合 spawn）。变异 M4（搬移改复制）被杀。

---

## 14. Direct Fast-Path Semantics

- 等待集为空：**行为完全不变**（直投总线、不进 pending、零调度跳变）。
- 等待集非空（有更早 accepted 的输入在等）：本条入队等待（PO3）。
- **未改变**：直投输入在等待集为空时**仍会**抢占已接管输入的回合（既有语义，§34）。
  副作用是"已 claim 但未进历史就被抢占"的丢失形态（→ §35 N7，非本批引入）。

---

## 15. Pending FIFO Semantics

队列**始终按 admission 顺序有序**（不变量）；`pop(0)` 因此仍是"取最老"；等键/无键项稳定
插入。容器仍是 `list`（有界 32 条：最坏头插 8.6 µs/条）。无键口径保守（§34）。

---

## 16. Recovery Ordering

出队重投**保留原 `request_id`** ⇒ 被越队判据挡回的条目经 `_enqueue_pending` 放回**原位置**，
不会重新计龄。变异 M5（出队重盖 `request_id`）被杀（5 条守卫红）。

---

## 17. Cancellation Ordering

- **停止按钮 / VAD（INTERRUPT）**：既有合同 = 放弃尚未开始的输入（bus + pending 清空、可归因）。
  本批不变；守卫钉住"清除不变成重排、被清除者不复活"。
- **逐条取消 pending 中间项**：无此接口 ⇒ §38 **N/A**（记入 §34）。
- 取消当前回合不影响其余 pending 的相对顺序。

---

## 18. Error / Interrupt Ordering

- 回合在 admission 窗口内**失败**：失败者不获得 ownership（`failed_pre_ownership`），
  M2/M3 按序处理 ✅（修前 `['M3','M2']`）。
- **proactive / task-result 回合**在窗口内失败：兜底出队保证队列不失去触发器（§11 根因三）。
- **不依赖** `asyncio.Lock` 的 waiter 公平性（顺序由 admission 键与队列顺序保证）。

---

## 19. Shutdown Semantics

`Agent.stop()` 仍**同步**清空 pending 并释放 lease（不 drain、不起回合）。守卫从"只看 claim"
升级为"**连回合 spawn 都不许发生**"（变异 M6a 曾逃过一版判据，现已杀死）。

---

## 20. Queue-Full Boundary

策略未改（`len(_pending_messages) >= max_pending_messages`）。**但触发条件更常命中**：等待集
判据把"总线空但 pending 非空"的输入也导向排队分支，于是它会走**接受时**容量检查 ⇒
`process_message` 返回 `False`（WS 回 `QUEUE_FULL rejected`，客户端可重试），而不是
"先 accepted、随后被消费者丢弃"（KB-43 的 post-ACK 竞争）。Challenger A 实测：
`[CAPACITY] new=False round1=True`。这是**语义变好**（可归因、可重试、不静默丢），
但属于 ACK 时序变化，故在此显式记录。被拒者从不进入顺序；已 accepted 者的相对顺序不变。

---

## 21. Duplicate / Omission Proof

每条顺序守卫都做三层集合断言（`_assert_full_order`）：`claims == expected`（顺序相等）＋
`set(claims) == set(expected)`（无遗漏）＋ `len(claims) == len(set(claims))`（无重复）；
再叠加"每条 claim 自己的 `request_id` 严格单调且互不相同"（§5 见证，对并发投递同样成立）。
Challenger B 的 2400 轮随机化独立复核：无重复、无遗漏、无停滞。

---

## 22. Regression Tests

| # | 用例 | 分类 | 修前 | 修后 |
|---|---|---|---|---|
| 1 | `test_three_message_burst_keeps_admission_order` | **PRE_FIX_RED**（PO1/PO2/PO6） | ❌ `['M3','M2']` | ✅ |
| 2 | `test_five_message_burst_keeps_admission_order` | **PRE_FIX_RED** | ❌ | ✅ |
| 3 | `test_error_in_admission_window_then_drain_keeps_order` | **PRE_FIX_RED**（PO1/PO4） | ❌ | ✅ |
| 4 | `test_newer_direct_input_cannot_bypass_older_pending_input` | **PRE_FIX_RED**（PO3） | ❌ 抢占+越队 | ✅ |
| 5 | `test_newer_input_cannot_enter_bus_while_older_pending_waits` | **PRE_FIX_RED**（PO3，Ch A 一轮反例的公开入口变体） | ❌ | ✅ |
| 6 | `test_direct_input_cannot_overtake_when_older_pending_is_in_flight` | **PRE_FIX_RED**（PO3，交接态；含前提断言防假绿） | ❌ `['M1','V1','M2']` | ✅ |
| 7 | `test_concurrent_entries_processed_in_admission_order` | **PRE_FIX_RED**（PO1 跨入口，v2 并发） | ❌ | ✅ |
| 8 | `test_queue_never_loses_its_drain_trigger` | **PRE_FIX_RED**（活性，Ch A 二轮反例） | ❌ 永久静音 | ✅ |
| 9 | `test_single_message_fast_path_unchanged` | ALREADY_GREEN_GUARD（AD6/§43） | ✅ | ✅ |
| 10 | `test_two_message_boundary_unchanged` | ALREADY_GREEN_GUARD（§44/§53） | ✅ | ✅ |
| 11 | `test_stop_button_clears_pending_without_reordering_survivors` | PRODUCT_CONTRACT_GUARD（PO5） | ✅ | ✅ |
| 12 | `test_shutdown_does_not_drain_pending` | PRODUCT_CONTRACT_GUARD（PO6/§41） | ✅ | ✅ |
| 13 | `test_admission_order_key_is_monotone` | ORDERING_KEY 合同守卫 | ✅ | ✅ |
| 14 | `test_admission_input_delivery.py::test_third_message_arriving_while_second_is_still_on_the_bus` | **PRE_FIX_RED**（Batch 12 的 FALSIFICATION_GUARD 按其自身指示改为正确顺序断言） | ❌ | ✅ |

`_wait_settled` / `_pump_until` 的时间等待只作**活性探测**（收口链含 50ms UI 可观测延迟）；
所有 race 由 `asyncio.Event` 屏障决定。

---

## 23. Challenger Findings

### 23a Challenger A（第一轮：EventBus / 派发 / 任务创建顺序 + 攻击）

| 项 | 结果 |
|---|---|
| **FALSIFIED（已修）** | **等待集缺"已出队交给总线"那一半**：`chal_attack1b`，`claims=['Q1','M3','M2']` 四层证据全反序。根因：判据挂在"从总线 spawn"一侧、信息面只有 pending，而竞态发生在"pending 项交给总线"一侧。修法：等待集 = pending ∪ 总线驻留（`EventBus.pending_user_input_request_ids()`）＋ 两入口共用同一判据 |
| HELD | 6/10 条 burst、混合直投、流式窗口、变化释放时机、IDLE 出队挡回（claim == rid） |
| HELD | 重复/活锁：同一条被"出队→挡回→重入队"5 次后恰处理一次；乱序队列能自愈 |
| HELD | 两个出队口并发触发（KB-28 窗口）无重复派发/乱序 |
| HELD | INTERRUPT：清空、可归因、不复活、中断后仍按序 |
| HELD（顺序） | 容量 2/5 时 Y4/Y5 被丢弃但顺序未破坏（可归因） |
| 变异 | `append` 变异 → 3 红；`noguard` 变异 → PO3 红（无 fake-green） |
| 覆盖缺口（已采纳） | `test_admission_input_delivery.py` 对 `noguard` 全绿；PO5/PO6 对该变异不敏感 |
| 潜在地雷（已登记） | 排序键"捕获时刻 == 准入时刻"假设无机制（`ws_handler.py:726/733` 等三处同同步块）；非数字键会让判据静默失效（本批已改为保守口径） |

### 23b Challenger A（第二轮：攻击新机制）

| 项 | 结果 |
|---|---|
| **FALSIFIED（已修）** | **停滞**：proactive 回合窗口内失败 ⇒ 队列失去触发器 ⇒ 整条输入流永久静音（`r2_attack2_stall.py`；对照显示修前靠"新输入起回合"自愈、本轮判据把它也挡住）。修法：兜底出队挂到**所有**回合 spawn 点 ＋ 新守卫 #8 |
| **fake-green（已修）** | 守卫 #6 的放行条件让 `current_task is not None` 恒真 ⇒ 更早者仍在 pending，从未进入它自称的交接态；`pending_only` / `no_bus_ids` 变异下 12 条全绿。修法：放行条件改为"pending 已清空"＋**前提断言**；变异 M7/M9 现在会杀死它 |
| HELD | 等待集无漏项（通用泄漏探测器 `violations=[]`；交接态 `pending=[] bus_ids=('003',)` 可见；中断 drain 后计数与 id 同归零） |
| HELD | EventBus 计数与 id 快照在全组合下等长（post/next/drain/优先级/重复 rid/空串/重投） |
| 行为改变（已记录） | 有更早者时直投 barge-in 变成排队；容量满时从"先 accepted 后丢弃"变成**立即拒收**（§20） |
| 新 finding（已登记） | `stop()` **不 drain 总线**：残留 id 跨重启存活、重启后被消费（§35 N8）；`type=` 记账 vs `isinstance` 分派不对称（§35 N9，当前不可达） |
| 旧丢失类（非本批引入，已登记） | 已 claim、进历史前被抢占（K1/R2/V3）⇒ 会话里没有该输入（§35 N7） |

**最终一版上的复核（本批收尾）**：把最终生产代码复制进两个 Challenger 沙箱后重跑其全部用例
——A 的 12 个文件 **32 passed / 4 failed**，四个失败全部是"证明缺陷存在"的演示/对照用例，
其**缺陷前提已不成立**（`r2_attack2_stall` 的 `pending=[]` = 停滞已修；`r2_attack5` 的
`pending==["K1"]` 前置断言同理），加上一条已知的 **N7**（已 claim、进历史前被抢占的丢失，
修前既有）；B 的随机化 + 边界套件（新 seed 块 600，400 轮）**15 passed**，排队 admit
mean 6.9 µs。

### 23c Challenger B（随机化 / 边界 / 抖动 / 性能）

| 项 | 结果 |
|---|---|
| HELD | **2400 轮随机化（≈15.5k claim）**：claim 顺序 == admission 顺序 0 例外；含双屏障、随机 0–5ms 真实间隔、25% 直投、随机放闸与重新武装 |
| HELD | 边界扫描 40 组（max ∈ {0,1,2,3,32} × burst ∈ {2,3,4,5,10,32,33,100}）：被拒者从未被处理、存活者顺序逐项相等、无重复 |
| HELD | 混合会话：3 会话 9 条 ⇒ 全 Agent FIFO（rid 002–010） |
| HELD | 无停滞（四条件落定；`StallError` 0） |
| 无抖动 | 同 seed 重跑 claims 逐 seed 完全相同；仅"被合法抢占未走到 LLM"的计数 ±1 |
| 主动披露 | 首版 harness 对 LLM/历史层用严格相等 ⇒ seed 200–207 有 7 轮假红（抢占让它成为子序列）；claim 判据自始至终严格相等 |
| 性能 | 单条 E2E 40.7ms；快速路径 admit 37µs；排队 admit `_enqueue_pending` mean **7.3µs**；32 条 burst 1325ms（41.4ms/条） |
| INCONCLUSIVE（已加固） | 空/非数字 `request_id` 会让判据失效（当前 5 个生产者都带 `next_request_id()` ⇒ 不可达）；本批已改为**保守口径**（读不到就排队） |

---

## 24. Mutation Evidence

| 变异 | 内容 | 杀死的守卫 |
|---|---|---|
| **M1** | `_enqueue_pending` 回退为盲 append | #1 #2 #3 #14 |
| **M2** | 出队 `pop(0)` → `pop()`（LIFO） | #1 #2 #3 #4 #6 #14 |
| **M3** | 越队判据整体关闭 | #4 #5 #6（三条 PO3 守卫） |
| **M4** | 出队"复制而非搬移" | **17 条**（含重复/遗漏断言） |
| **M5** | 出队重盖 `request_id`（重新计龄） | **8 条** |
| **M6a** | `stop()` 先把 pending drain 起来 | #12（靠 spawn 判据） |
| **M6b** | `stop()` 不清 pending | #12 |
| **M7** | 等待集**只留 pending**（= 被 Ch A 一轮反证掉的那版） | **#6**（交接态守卫——证明它不是 fake-green） |
| **M8** | 等待集**只留总线**（去掉 pending 半） | #4 #5 |
| **M9** | 总线 id 快照不再维护（peek 恒空） | **#6** |
| **M10** | 辅助回合失去兜底出队回调 | **#8**（活性守卫） |

### 24a 变异矩阵实测（最终修复版）

```text
BASELINE（最终修复）                          → 25 passed
M1  盲 append（回退按写入时刻排序）           → 4 failed（#1 #2 #3 #14）
M2  出队 pop()（LIFO）                       → 7 failed（#1 #2 #3 #4 #7 #14 …）
M3  关闭等待集判据                            → 3 failed（#4 #5 #6）
M4  出队复制而非搬移                          → 17 failed
M5  出队重盖 request_id（重新计龄）           → 8 failed
M6a 停机先 drain                             → 1 failed（#12）
M6b 停机不清 pending                         → 1 failed（#12）
M7  等待集只留 pending（Ch A 一轮的那版）      → 1 failed（#6）
M8  等待集只留总线                            → 2 failed（#4 #5）
M9  总线 id 快照恒空                          → 1 failed（#6）
M10 辅助回合失去兜底出队                      → 1 failed（#8）
```

**每个机制都有 load-bearing 的守卫**：M7/M9 专门证明 #6（交接态守卫）不是 fake-green
——它正是 Challenger A 第二轮指出的覆盖缺口，修复后由这两个变异钉住。

---

## 25. Task / Queue Leak Evidence

- 停机后 pending 清空且 lease 释放（#12 断言 pending 为空 ＋ 无 spawn）。
- 每条顺序守卫结束都断言 `pending == []`、`bus.pending_user_inputs == 0`、
  `status.is_completely_idle`。
- 新增的是 done callback（不是新协程/轮询/计时器）⇒ 无新任务所有权登记需求
  （`make check-arch` R6 绿）。

---

## 26. Public API / Schema Impact

**零** schema / wire 变化：`process_message` 签名与返回值不变；`UserInputData` /
`_PendingMessage` 字段不变；`make check-types` 绿。唯一可观测变化是 §20 的
QUEUE_FULL 时序（更早拒收 = 更正确的合同）。

---

## 27. Persistence Impact

**零**：不写库、不新增集合/索引/字段、无迁移。顺序键与总线快照都是进程内状态。

---

## 28. Performance Impact

| 项 | 本批实测 |
|---|---|
| 32 条升序入队 | 72.0 µs/批（≈2.25 µs/条） |
| 32 条最坏（每次插队首） | 275 µs/批（≈8.6 µs/条） |
| 越队判据（等待集空） | **≈170 ns/次** |
| 越队判据（pending 32 条） | ≈670 ns/次 |
| 单条 E2E | 38.7ms（Ch A 对照：38.6ms） |
| 32 条 burst | 1302ms（对照 1283ms，噪声级） |
| 排队 admit（Ch B 实测） | mean 7.3 µs |

---

## 29. Commits

| # | commit | 内容 |
|---|---|---|
| 1 | `fix(agent): preserve pending input admission order` | `core/agent/agent.py` + `core/monitoring/event_bus.py` + 两个测试文件（production ＋ PRE_FIX_RED 同 commit） |
| 2 | `docs(architecture): record pending-input ordering invariants` | `invariants.md` §4.6（PO1–PO6 ＋ 合同/域/顺序定义 ＋ 边界）＋ `architecture.md` §4.1 一行 |
| 3 | `docs(review): record Batch 13 pending input ordering` | 本文件 |
| 4 | `docs(todo): close KB-38 as GUARDED` | `todo.md` KB-38 行 |

---

## 30. Worktree Verification

`tmp/b13-wt`（HEAD `873c2f77` detach，最终测试文件）：

```text
① pristine HEAD + 本批测试（未改生产）  → 9 failed / 16 passed   （全部红在行为断言，无符号类假红）
② + core/agent/agent.py + event_bus.py → 25 passed
```

变异在 `tmp/b13-mut`；历史对照在 `tmp/b13-pre12`（`2e70854d`）。

---

## 31. Test Results

| 项 | 结果 |
|---|---|
| targeted（`test_pending_input_ordering.py`） | 13 passed |
| 2 条 / 3 条 / 5 条 burst | ✅ |
| 并发投递（v2 `reserve_work` 交错） | ✅ |
| 交接态（bus∩pending）与活性（proactive 停滞） | ✅ |
| `test_admission_input_delivery.py` | 12 passed |
| `core/agent/tests` ＋ `core/cognitive/tests` ＋ `core/monitoring/tests` | **816 passed** |
| `core/input/tests` | **921 passed** |
| `make test-quick` / `test-normal` | **6230 passed**, 2 skipped, 2 xfailed（各 ≈1min20s） |
| `make test-full` | **6262 passed**, 36 skipped, 2 xfailed（≈7min38s） |
| `make check-test-sets` | `|Quick ∩ remainder| = 0`；`|Quick ∪ remainder| = 6234 == |Normal|`（全量收集 6300） |
| `make test-real-mongo`（本批无 storage 依赖，按项目惯例本地也跑） | **24 passed**（6276 deselected，26.9s；容器已起停） |
| frontend `npm run build` | ✅（built in 1.18s；`static/` 产物未入库） |
| `make lint` / `pyright` / `check-arch` / `check-timeouts` / `check-contract` / `check-types` / `check-debug-auth` / `check-secrets` / `check-compose` / `check-env-lock` / `check-ci-isolation` | 全绿 |
| real CI | owner push 后确认（Backend / Real MongoDB / Frontend） |

---

## 32. Invariant Mapping

| ID | 一句话 | 守卫 |
|---|---|---|
| PO1 | 同一 ordering domain 内，保持可处理的 accepted 输入按 admission 顺序获得 active-turn ownership | #1 #2 #3 #7 #14 ＋ rid 单调断言 |
| PO2 | pending 只有一个入队权威，且始终按 admission 顺序有序；`pop(0)` = 取最老 | #1 #2 #10 |
| PO3 | 等待集（pending ∪ 总线驻留）里有更早者时，新输入（含直投）不得越队 | #4 #5 #6 |
| PO4 | 恢复保留原 admission 顺序、不重新计龄 | #3 ＋ 变异 M5 |
| PO5 | 显式取消只移除被取消的输入；幸存者保序；不复活 | #11 ＋ Batch 12 归因用例 |
| PO6 | 不制造重复 owner；停机不复活 | #12 ＋ 变异 M4/M6a/M6b ＋ 无重复断言 |
| PO-L | pending 非空始终存在出队触发（活性） | #8 ＋ 变异 M10 |

---

## 33. Todo Update

KB-38：`OPEN` / `NEW_FINDING_OUT_OF_SCOPE` → **`GUARDED`**（附 commit 与守卫清单）；
canonical 描述里"非本批引入"改为准确表述（根判据 pre-existing，可见性由 Batch 12 判据放宽打开）。
**交叉引用**：KB-28 / KB-45 只做 cross-reference，**不**因本批关闭。

---

## 34. Keep As-Is

- **EventBus 架构与优先级**（INTERRUPT 插队、同优先级 FIFO、drain 语义）：未改；只加了一个只读快照访问器。
- **FSM / 相位语义**、**`_process_lock`**、**accepted wire 契约**、**pending 容器与容量策略**：未改。
- **两个出队口**（`STATE_CHANGE→IDLE` ＋ done callback）：保留，并把 done callback 补齐到所有回合 spawn 点。
- **直投输入的抢占语义**（等待集为空时）：保留（§14）。
- **不做**：durable queue / inbox / 通用 scheduler / priority queue / message broker / 全 EventBus 重构 / 前端改动 / 输入层改造。

---

## 35. New Findings（只登记，不修）

| # | 发现 | 处置 |
|---|---|---|
| **N1** | 直投输入（STT/voice）**不经过** `process_message` ⇒ 不过忙判定、不做接受时容量检查；等待集为空时抢占已接管回合（文本路径却排队） | 登记（与 KB-41/KB-42 同族） |
| **N2** | KB-38 canonical 的"非本批引入"缺测量；实测 2e70854d 上该形态走抢占分支 | 本批已在 todo / invariants 更正 |
| **N3** | 容量检查看不见总线驻留与在途回合 ⇒ `max_pending_messages` 不是硬上界（默认 32 下 burst ≥ 100 才显现） | 登记（§42 明确不重做 queue policy） |
| **N4** | `EventBus` 的计数/id 快照此前零测试覆盖 | **本批已补**（Ch B 的 EventBus 一致性用例 ＋ 新访问器有守卫） |
| **N5** | KB-45"fall-through 不可达"被部分推翻：直投输入可达（修前实测 `cancelling()==1`） | 建议在 KB-45 行补注（本批只 cross-reference） |
| **N6** | 排序键"捕获时刻 == 准入时刻"是**假设**：三个直投点目前都在同一同步块，拆开即失序 | 登记（不可达但有潜在地雷） |
| **N7** | "已 claim、进历史前被抢占"⇒ 会话里没有该输入（有终态帧、无 session 记录） | 登记（非本批引入；与 N1 同根） |
| **N8** | `Agent.stop()` **不清总线**：残留 USER_INPUT 的 id 跨重启存活，重启后被消费（复活） | 登记（`stop()` 只清 pending） |
| **N9** | EventBus 按 `event.type` 记账、`_dispatch_event` 按 `isinstance` 分派 ⇒ `post(UserInputData, type=…)` 会让等待集看不见它（当前仅 `_handle_interrupt` 用 `type=` 重投非用户事件） | 登记（当前不可达） |

---

## 36. Follow-up

保留：KB-8 / F13 / KB-17 / KB-26 / KB-27 / **KB-28** / KB-29 / KB-30 / KB-31 / KB-32 /
KB-33 / KB-34 / KB-35 / KB-36 / KB-37 / KB-39 / KB-40 / KB-41 / KB-42 / KB-43 / KB-44 /
**KB-45** / KB-18–KB-21 / KB-12 / KB-13 / AR-16（Ollama Provider Adapter，本批**未触碰**）/
KB-15 / KB-16 / KB-11 / KB-2 / KB-4 / KB-10 / GR-25 / N-15 / F3 / F17 / OBS-6/7/8 /
以及其余 canonical backlog。
