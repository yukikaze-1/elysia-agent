# Batch 14 — Residual Input Ownership Triage（执行记录）

> ⚠️ **Historical execution record.** 本文描述**执行当时**的系统与结论；后续批次可能推翻其中部分判断。
> 当前事实见 [docs 索引](../README.md) 的 Canonical References（架构 / 不变量 / 测试 / CI / 运维 / 契约）。
> 正文**按当时原样保留**——不改结论、不删失败轨迹、不重写 Challenger 反证（`docs/readme-conventions.md` Rule 3）。

> 范围：**先 triage、后修复**。审计 KB-26 / KB-28 / KB-29 / N7 / N8 五项残余 ownership
> finding，在 Batch 9–13 已建立的大量 lifecycle / admission / ordering 不变量之上，判断
> 哪些仍是**真实、用户可达、未被已有 invariant 覆盖**的缺陷。
>
> 基线 HEAD：`6a53a727`（`docs(todo): close KB-38 as GUARDED`）。
> 结论：**1 个 root-cause cluster 进入生产修复**（KB-47，原 N8）；其余四项分别
> 证伪 / 降级 / 确证但不修（各有明确分类与重开条件）。零 wire / 零 schema / 零持久化变更。

---

## 1. Executive Summary

| ID | confirmed? | classification | evidence | decision |
|---|---|---|---|---|
| **KB-26** `_current_task` 陈旧句柄 | 状态**成立**，缺陷**证伪** | INTERNAL / LOW / `FALSIFIED_BY_GUARD` | E2(14)：真 Agent 逐个触发**全部 5 个生产读者** | **KEEP_AS_IS**（不为 hygiene 改生命周期） |
| **KB-28** `IDLE 已广播 / 锁未释放` | 主形态**证伪**；残留一半不可达 | USER（前提不足）→ **CURRENTLY_UNREACHABLE** / LOW | E2(10)+E2(11)+E2(14) | **RECLASSIFY**（主形态已被 Batch 12/13 判据覆盖） |
| **KB-29** 入口异常误伤 `IDLE_BACKGROUND_TASK` | **CONFIRMED** | INTERNAL / LOW / phase truthfulness | E2(14)：真委派 + 真 `StorageUnavailableError` | **KEEP_OPEN**（本批只 triage；不与 KB-47 同 root） |
| **KB-46**（原 N7）已 claim 但未进历史 | **CONFIRMED** | **USER / HIGH**（静默丢失 + 会话真相错误） | E2(14)：直投输入/停止按钮落窗口，session+archive+LLM 三层皆无；**可最小修复已被 Challenger 实证** | **OPEN** `PRODUCT_DECISION_REQUIRED` + `DEFERRED_BY_BUDGET`（Batch 15 **第一项**；原 `DEPENDENCY_ON_KB-30` 已撤回） |
| **KB-47**（原 N8）停机残留输入跨 epoch 复活 | **CONFIRMED** | **DEPLOYMENT / MEDIUM**（受支持的原地重启模式） | E2(14)：**两个独立探针**各自复现，修前红 / 修后绿 | **GUARDED** `fix(agent): discard abandoned user input across runtime restart` |
| **KB-48**（新）输入层两条入口停机窗口仍投递 | 已确证 | INTERNAL / LOW | **E2(14)**（Challenger 实测：停机后仍可投递并进历史/LLM） | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` |

- **production fix?** 有，**一个** root-cause cluster：KB-47（runtime epoch 边界结束
  "非 durable 未接管输入"的 ownership，并让该处置**可归因**——pending 与总线两半）。
  2 个生产文件 / 约 +100 行 / 零删除。
- **哪个 root?** 「**非 durable 的 accepted 用户输入不得跨越 runtime epoch 边界**」——
  与已确认并已部分修复的 CR-08 / S2-R2-4 缺陷族同一 root（`c029110e` 只修了入口侧
  的两条路径、且 `Agent.stop()` 只清了 pending），修复不可合理拆分 ⇒ 走 **Gate C**。
- **public API?** **零**变化（无 wire 帧、无契约模型、无 REST、无前端）。
- **persistence?** **零**变化（不加 collection / 字段 / 索引 / migration）。
- **real CI?** 需 owner push 后确认（见 §28）。本地 static gates 与各测试档位见 §28。

**关键诚实声明（二）**：按 §51 的优先级，本批**最高风险**的 finding 其实是 **KB-46
（USER/HIGH 静默丢失）**；本批**没有**修它，理由是 §50 把 production 范围硬性限制为**一个**
root-cause cluster，而它与 KB-47 **不同 root**（§49）。Challenger 已证明它**可以**最小修复
（我另行验证了该补丁只覆盖两个落点中的一个，完整修法见 §20.2）——若 owner 更希望本批修它，
回退 KB-47 补丁、以 `tmp/b14/challenger/n7_minfix.patch` 为起点即可，建议排为 Batch 15 第一项。

**关键诚实声明（一）**：KB-47 的复活形态**不是默认用户路径**（默认 `/restart` 会**重建 Agent
实例**，旧 bus 随实例丢弃）。它落在**受支持的原地重启模式**
（`Agent.run_server(rebuild_on_restart=False)`，仓内无生产调用方）与"库式嵌入
`stop()→start()`"上。定级 DEPLOYMENT/MEDIUM 而非 USER/HIGH，落地依据是
**已写下的合同 + 已确认缺陷族的未完成半边**（详见 §13 / §16）。

---

## 2. Canonical Backlog Reconciliation

| 原描述（Batch 10–13 登记） | 当前 HEAD 实测 | 差异 |
|---|---|---|
| **KB-26**："`_current_task` 在正常完成的回合后仍指向已结束的任务……消费方都先问 `.done()` ⇒ 无用户影响" | **完全成立**，且"无用户影响"这一半被**正向证明**：读者集合 = 恰好 5 处、每处都在 `.done()` 守卫内（`tmp/b14/subA/probe_kb26.py` 用可执行 census 断言集合） | 无实质差异；从"推断"升级为"测量 + 边界落纸" |
| **KB-28**："下一条消息会把它当作在途回合取消掉 ⇒ 已 ACK 的排队消息被丢弃且 FIFO 被抢" | **不成立**（HEAD）：卡在 `_process_lock` 的回合尚未接管输入 ⇒ Batch 12 的 `not_owning` 让新输入**排队**；实测 claims/session/LLM `KA→KB→KC→KV` 无丢失无重复 | **证伪**主形态；残留"严格两发停止"仍静默丢，但前提（挂起订阅者）仓内不存在 |
| **KB-29**："`IDLE_BACKGROUND_TASK→INTERRUPTED→IDLE`，异常路径触发误伤别人相位" | **成立且可达**（INTERNAL）：真委派后台任务 + 真 `StorageUnavailableError`；product contract = **(B) 保留后台**；取消臂保持相位、异常臂不保持 | 无实质差异；补齐可达性、后果清单与合同证据 |
| **N7**（无 canonical ID）："已 claim、进历史前被抢占 ⇒ 会话里没有该输入（有终态帧、无 session 记录）" | **成立且 USER 可达**；并且**多出两条**：C 段窗口**零归因**（Batch 12 的 AD2 只覆盖前半段）、**停止按钮**是更宽的触发面 | 严重度 LOW→**HIGH**；补 canonical **KB-46** |
| **N8**（无 canonical ID）："`Agent.stop()` 不清总线：残留 USER_INPUT 的 id 跨重启存活，重启后被消费（复活）" | **成立**（两探针）；并确证 `invariants.md` §4.6 早已把"`Agent.stop()` 清空 bus + pending"写成既有合同（代码只做 pending 那一半） | 补 canonical **KB-47**；**已修** |
| 新 **KB-48** | `do_transcription` / voice.commit 两条直投入口不查 `is_service_running` | 新登记（与 KB-47 同族、不同权威，不合并） |
| 新（Batch 13 遗留处置） | `AR-8`（"EventBus lifecycle epoch … 风险低"）的**缓解论据被证伪**（取消消费者 ≠ 清队列） | AR-8 保留为父项、改写缓解说明 |

---

## 3. Residual Ownership Model

一条输入在其生命周期上会依次经过下列**容器/权威**；`→` 表示 ownership 的**交接**
（同一条输入在任一时刻**只能**被一个容器拥有）：

```text
WS/REST/voice 入口
  │
  ▼
accepted            Agent.process_message() 返回 True（bus.post 成功 或 有界 pending append 成功）
  │                 ＋ 决策幂等（同 clientRequestId 重放同一 ACK）；**不**承诺持久化
  ▼
EventBus residency  bus.post(UserInputData) 之后、消费者 _handle_user_input 之前
  │                 ‖ 与 _pending_messages 互斥（单 claim：谁拥有是二选一）
  ├──── pending ──── _enqueue_pending（按 admission 顺序插入；唯一入队权威）
  │                 ↑ 出队：STATE_CHANGE→IDLE（_handle_state_change）
  │                       或回合 done callback（_drain_pending_after_turn）
  ▼
active claim        _generation_ctx 建立（loop.py:655，与 enter_thinking 间无 await）
  │                 = `CognitiveLoop.current_turn_owns_input`；_process_lock 全程持有
  ▼
FSM active phase    THINKING（enter_thinking 同步 transition）→ EXECUTING → SPEAKING
  │
  ▼
history append      session.add_message（loop.py:1021，在 _build_turn_prompt 内）
  │                 ← 本批 N7/KB-46 的窗口就在 claim 与这一点之间
  ▼
generation exec     LLM 流式 / 工具轮（session 历史此后包含这条输入）
  │
  ▼
terminal finalize   generation.completed / failed / cancelled ＋ FSM → IDLE（＋bus.post）
                    ＋ 释放 session work lease（done callback）
  │
  ▼
epoch boundary      Agent.stop()（本批 KB-47 的修复点）
                    → 已接管回合被取消并收口；pending 清空；**总线驻留的未接管输入丢弃**
```

### 3.1 Truth source authority map（每个源代表什么）

| 真值源 | 代表什么 | **不**代表什么 | 谁负责交接 |
|---|---|---|---|
| **accepted 决策** | "调用已进入 runtime 生命周期"＋决策幂等（v2 幂等表 TTL 1h） | 不承诺持久化 / 一定被回复 / 失败一定告知（KB-39） | `Agent.process_message` / 三条直投入口 |
| **EventBus residency** | "已 accepted、**尚未**被任何回合接管"（等待派发） | 不是持久化队列；不是 admission 真值（优先级队列只是传输） | 消费者 `_next()` → `_dispatch_event`；停机边界 → KB-47 清扫 |
| **pending queue** | "已 accepted、因忙或越队被判等待"（有界 32、按 admission 顺序） | 不是 FIFO 全序的唯一容器（等待集 = pending ∪ 总线驻留） | `_enqueue_pending` / `_dispatch_pending_message` |
| **`_current_task`** | "**最近 spawn** 的回合 / 取消目标"（KB-26） | **不是** ownership 真值（陈旧也不影响行为：读者全在 `.done()` 守卫内） | `Agent._handle_user_input` / `_handle_system_tick` / `_handle_task_result` 赋值 |
| **`_process_lock`** | 回合执行的**互斥**（全 Agent 串行） | 不是"是否已接管输入"的判据（KB-28 的窗口 = IDLE 广播 + 锁仍持有） | `async with` 进出 `CognitiveLoop.process` |
| **`_generation_ctx`** | **active ownership truth**：非 None ⟺ 本回合已接管其输入、已/即将 THINKING（Batch 10/12） | 不代表输入已进历史（KB-46 的窗口） | `_prepare_turn` 赋值；`process()` 取锁**之前**清空 |
| **FSM phase** | 对外可观测的**阶段**（UI/门禁/`is_accepting_input`） | 不是"是否有在途回合"的判据（KB-42/45 都在此偏差上） | `FSMBackedPhaseController` |
| **session history** | "用户真的发过的每一条"（唯一豁免 `is_task_result` 合成唤醒） | 不代表"一定被模型处理过"（append 早于 LLM 首轮；半截回复也会归档） | `_build_turn_prompt`（KB-46 的缺口） |
| **generation registry** | generation 行的**持久**生命周期（register/mark；best-effort） | 不是 runtime truth（CF 已定级：runtime truth 优先） | `_prepare_turn` register / 终态 mark |
| **run_state** | 生命周期轴（STOPPED/RUNNING/STOPPING/RESTARTING），与相位轴同对象持有（#43） | 不等于"消费者已就绪"（`start()` 先置 RUNNING，第 4 步才 spawn 消费者） | `AgentStatus.set_run_state` |

---

## 4. Truth Matrix

| # | State | EventBus | Pending | `_current_task` | Lock | `_generation_ctx` | FSM | History | 合法? |
|---|---|---|---|---|---|---|---|---|---|
| 1 | accepted waiting（快速路径） | **yes** | no | 上一回合 done 句柄 或 none | free | none | IDLE / IDLE_BG | no | **合法**（Batch 13 PO：等待集包含总线驻留） |
| 2 | queued（忙/越队） | no | **yes**（有序） | 在途回合句柄 | held（在途） | 在途回合的 ctx | THINKING… | no | **合法** |
| 3 | 交接态（已 `pop(0)`、未 claim） | **yes**（seq 更早） | no | none/done | free | none | IDLE | no | **合法**（Batch 13 明确覆盖） |
| 4 | active pre-history（**KB-46 窗口**） | no | no | **live** | **held** | **yes** | THINKING | **no** | **合法 transient，但取消落到这里 ⇒ 输入丢失且（C 段）无归因 = 缺陷** |
| 5 | active | no | no | live | held | yes | active | yes | **合法** |
| 6 | finalized（正常） | no | no | **done 句柄（陈旧）** | free | none | IDLE | yes | **合法**（KB-26：无读者受影响） |
| 7 | finalized（取消/异常） | no | 可能（幸存者） | done | free | none | IDLE | 见 §5 | **合法**（Batch 11 CF） |
| 8 | `IDLE 已广播 / 锁未释放`（KB-28） | no | 可能 | live（卡在锁上） | **held** | **none**（入口已清） | **IDLE** | no | **合法 transient**（Batch 12/13 判据：新输入排队而非抢占；实测无 bypass/dup/omission） |
| 9 | 停机中（steps 1..N） | 可能仍有事件 | 已清 | 可能 live→cancelled | held→free | 可能非 none | INTERRUPTED→IDLE | yes | **合法**（CF6：停机有界、不产生新回合生命周期） |
| 10 | **停机后 / 重启前（残留）** | **yes（修前）** | no | none | free | none | IDLE | no | **非法**（KB-47：ownership 未结束 ⇒ 跨界复活）→ 修后 `USER_INPUT` 必为 0 |
| 11 | stopped | 只允许 durable（`TaskResultData`） | no | none | free | none | IDLE（`fsm.reset()`） | — | **合法**（RO2：分类保留） |

**判定口径**：合法 transient 的判据不是"两个源是否同时更新"，而是"它是否让用户可见
的 ownership 语义发生改变（omission / duplication / wrong cancellation / wrong
conversation truth / liveness failure / stale resurrection）"。第 6、8 行在 Batch 14
被**逐条证伪**（无用户可见后果），第 10 行被**逐条证实**。

---

## 5. Existing Invariant Coverage（SP / TH / CF / AD / PO）

| 本批 finding | 相关既有不变量 | 覆盖程度 | 说明 |
|---|---|---|---|
| KB-26 | AD7（ownership 判据不得陈旧） | **fully covered（等价替换）** | `current_turn_owns_input` + `_started_cognitive_tasks` 已是判据；`_current_task` 只剩句柄语义 |
| KB-28 | AD3 / AD7 / PO3 / CF5 / CF6 | **主形态 fully covered**；残留半形态 **not covered 但不可达** | 排队而非抢占由 `not_owning` + `_has_older_waiting_input` 提供；最接近的窗口技术守卫是 `test_ownership_judgement_is_never_stale_for_a_new_turn` |
| KB-29 | TH（取消边界的 ownership 判据）/ CF1/CF2 | **partially covered** | 取消臂有 turn-scoped 判据（`loop.py:717-720` 的设计意图）；**异常臂**没有 ⇒ 本 finding 正是这个缺口。AD8 用例把 `is_completely_idle` 写成**前提**，因此 IDLE_BG 变体零覆盖 |
| KB-46（N7） | AD2（commit point 之后的取消必须落可归因终态）/ AD5 / PO3 | **partially covered** | AD2 只覆盖 `_prepare_turn` 内的落点；`_think_loop`→`_build_turn_prompt` 段落点**零归因、零守卫、零断言** |
| KB-47（N8） | AD5（停机不得复活）/ PO5 / PO6 / §4.6 已知边界 | **partially covered（文本已声明、代码只做一半）** | 既有守卫只覆盖 pending（`test_shutdown_does_not_drain_pending`）与停止按钮（`test_stop_button_clearing_pending_is_attributable`）；总线驻留这一半**无守卫** ⇒ 本批补齐 |
| KB-48 | S2-R2-4（输入层停机窗口不投递） | **partially covered** | 4 条入口里 2 条有判据、2 条没有 |

**§40 反向验证**（回退候选修复前，现有 AD/PO/CF/TH 是否已经红）：**没有**。回退 KB-47 的
补丁后，`test_admission_input_delivery` / `test_pending_input_ordering` /
`test_turn_finalization_cancellation_safety` / `test_thinking_cancel_recovery` /
`test_shutdown_*` / `test_task_result_*` / `test_restart_rebuild` / `core/cognitive/tests`
全绿（详见 §19/§28）⇒ KB-47 **不是** ALREADY_GUARDED，而是真的新增覆盖。

---

## 6. KB-26 — `_current_task` 陈旧句柄

### Original claim
正常完成的回合后 `_current_task` 仍指向已结束的任务（只在 `cancel_current()` /
`reset_lifecycle_state()` 清）；消费方都先问 `.done()` ⇒ 无用户影响。未知项：会不会错误
cancel 新 turn / 让 admission 判据失真 / 已被 done callback 间接防护。

### Static analysis
- **权威含义** = "最近 spawn 的回合 / 取消目标"。写点 5 处：`loop.py:940`（回合自己在取锁后
  写 `asyncio.current_task()`）、`agent.py:1168/1218/1258`（三个 spawn 点）；
  清点 2 处：`loop.py:431-434`（`cancel_current`）、`loop.py:457`（`reset_lifecycle_state`）。
- **读者恰好 5 处**（可执行 census）：`loop.py:418`（`.done()` 守卫）、`loop.py:431`
  （取消调用，紧跟守卫）、`agent.py:1092`（`prev.done()` + `_started_cognitive_tasks` +
  `current_turn_owns_input`）、`agent.py:1340`（`.done()` 守卫）、`debug/info.py:164`
  （`.done()` → `processing`）。**不存在**字符串形式 `getattr(obj, "_current_task")`。
- 关键推理：`_current_task` 在**每个** spawn 点都在 spawn 之后**同步**赋值 ⇒ "陈旧且
  done" ⟺ "此刻没有更新的回合存在" ⇒ `not_owning=False` 是**正确**的。
- Batch 12/13 已**不再依赖**它做 ownership truth（判据是 `_generation_ctx` +
  `_started_cognitive_tasks`）。

### E2 reproduction / falsification
`tmp/b14/subA/probe_kb26.py`（5 例，真 Agent + 真 start + 假存储/假 LLM）逐点触发全部读者：

```text
[T2] stale handle=cognitive-KB26 done=True fsm=idle debug_processing=False owns_input=True
[T3a] cancel_current(stale) -> False; handle.cancelled=False handle.cancelling=0 interrupt_source=None
[T3c] before drain: pending=['KB26-C'] stale=cognitive-KB26-B debug_processing=False  → 出队照常发生
[T4] session user order=['KB26-B','KB26-C','KB26-D'] pending=[] assistant_msgs=4 stale_handle_still_same=True
```

⇒ 错误取消=**无**、错误排队/拒收=**无**、误报忙=**无**、跳过登记=**无**、顺序与完整性=**保持**。

### User impact
**无**（observability 面 `debug.processing` 也正确：`.done()` 守卫）。

### Existing guards
`loop.py:418` / `agent.py:1092` / `agent.py:1340` / `debug/info.py:164` 的 `.done()` 守卫；
Batch 12 的 `current_turn_owns_input`（AD7）。

### Classification
INTERNAL / LOW / E2 / `FALSIFIED_BY_GUARD` / `same-root: INDEPENDENT`。

### Decision
**KEEP_AS_IS**。不为 hygiene 改生命周期（§11 明示）。边界落到 `invariants.md` §4.7 RO3；
todo 行改写为"状态成立、缺陷证伪"，并给出重开条件（出现无 `.done()` 守卫的新读者）。

---

## 7. KB-28 — `IDLE 已广播 / 锁未释放`

### Original claim
收口/回合在 `bus.post(StateChangeData(IDLE))` 之后仍持锁挂起时，pending 出队 spawn 的回合
会卡在 `_process_lock` 上，而"下一条消息"会把它当作在途回合取消掉 ⇒ 已 ACK 的排队消息被
丢弃且 FIFO 被抢。前提：存在会挂起的 `on_change` 订阅者。

### Static analysis
- 窗口真实：`handle_interrupt` 的 `finally` 里 **同步**落位/广播 IDLE（`fsm.py:261-268`），
  之后还要 `await self._status._notify("idle")`（`:269`）——此刻 FSM=IDLE、`bus` 已收到 IDLE、
  但 `process()` 仍持有 `_process_lock`（直到 `async with` 退出）。
- 关键变化（Batch 12/13 之后）：窗口里被 spawn 的回合**已经 first-step**（进
  `_started_cognitive_tasks`）**但 `_generation_ctx` 已在 `process()` 入口清空** ⇒
  `not_owning=True` ⇒ 新输入走**排队**分支，而不是 `cancel_current()`。

### E2 reproduction / falsification
`tmp/b14/subA/probe_kb28.py`（4 例；用**挂起的 `on_change` 订阅者** + `_dispatch_event`
屏障造窗口，无 sleep）：

```text
[T0] 无订阅者时出队口看到的状态：{'lock': False, 'turn_done': True, 'pending': ['KB']}
[KB28-WINDOW] fsm=idle lock_held=True turn_a_done=False pending=['KB'] bus_user_inputs=0
[T1] final: claims=['KA','KB','KC','KV'] session=[同] llm=[同] turn_b_cancelled=False pending=[] fsm=idle lock=False
[T2] 第二发停止：b_turn_cancelled=True b_processed=False pending=[] dispositions=[cancelled_pre_ownership …]
[T3] 严格两发：b_turn_cancelled=True b_processed=False pending=[] dispositions=[]   ← 静默
```

⇒ 原始claim（新消息取消卡在锁上的回合）**证伪**：`KC`（公开入口）与 `KV`（直投）都**排队**，
最终顺序完整、无重复、队列正常排空。残留的"严格两发停止"形态确实**静默**丢失，但
`[T0]` 同时测出：没有挂起订阅者时，出队口看到的锁**已经释放**，窗口不存在。

### User impact
**当前不可达**：仓内生产**不注册**会挂起的 `on_change` 订阅者（唯一注册者
`wait_until_idle` 的回调无 await，生产调用点只有 `bench`）。

### Existing guards
`not_owning`（AD3/AD7，`agent.py:1093-1095`）＋ `_has_older_waiting_input`（PO3，
`agent.py:990-1032`）；窗口技术守卫 `test_ownership_judgement_is_never_stale_for_a_new_turn`。

### Classification
USER（前提不足）→ **CURRENTLY_UNREACHABLE** / LOW / E2(10)+E2(11)+E2(14) /
`SAME_ROOT_DISTINCT_REACHABILITY`（与 KB-45/KB-41 同族）。

### Decision
**RECLASSIFY**（主形态已被 Batch 12/13 判据覆盖）。残留半形态保持 OPEN/LOW，并在 todo 行
写明**重开条件**：引入第二个 `on_change` 订阅者（或让 `_notify` 可挂起）时必须重估，
修法方向是给 INTERRUPT / `cancel_current` 路径补 ownership 判据（与 KB-41 同类）。

---

## 8. KB-29 — 入口异常误伤 `IDLE_BACKGROUND_TASK`

### Original claim
`_handle_turn_exception` 在**回合开始之前**触发时会（对上一回合遗留的
`IDLE_BACKGROUND_TASK`）调 `_handle_interrupt()` ⇒ `IDLE_BACKGROUND_TASK→INTERRUPTED→IDLE`。

### Static analysis
- 唯一入口：`process()` 的 `except (CharacterBusyError, StorageUnavailableError)`
  （`loop.py:915-917`）——泛型异常分支在 ctx 为 None 时 `raise`（`:924-925`），
  think 路径的 ctx 必非 None。
- `_prepare_turn` 在 commit point（`:655`）**之前**只有两个 await（`:545` 会话解析、
  `:564` runtime `get_or_load`；其余 `:568-654` 同步）。可抛 `StorageUnavailableError`
  的真实点位：`:564`（custom 角色实例加载）与 **`:578` `_identity_snapshot`（Mongo 离线
  fail-closed 的生产形态）**。题面列的 `CharacterBusyError` 实际在 `:692-694`（commit
  point **之后**）⇒ 被排除。
- `_handle_interrupt` 的早退判据是 `is_completely_idle`（= FSM **恰为** IDLE），
  `IDLE_BACKGROUND_TASK` 不在其中 ⇒ 收口执行；两条边都是**合法边** ⇒ FSM 静默接受外来相位变更。

### E2 reproduction
`tmp/b14/subB/probe_kb29.py`（2 例，真 Agent + **生产委派入口** `WorkflowEngine.submit`
造活跃后台任务 + 真 fail-closed 触发）：

```text
回合 A：notify_state = ['thinking','speaking','idle_background_task']
异常回合 B： = ['interrupted','idle']；FSM before=idle_background_task → after=idle
bus StateChangeData = ['interrupted','idle']；is_completely_idle=True
后台任务：同 task_id、仍 PLANNING、仍在 _tasks；日志 failed_pre_ownership + 存储依赖不可用
对照臂（同一 pre-commit 窗口的取消）：保持 idle_background_task、bus 零帧
```

### User impact
**有，但限于"相位/门禁上报"**：客户端收到 `interrupted` + `idle` 两帧（而本回合从未产出
任何用户可见内容）、UI 状态点变化、`is_completely_idle` 提前变真 ⇒ IdleMonitor 主动搭话
闸门打开、语音取消闸门不再触发、`wait_until_idle()` 立即返回、`_drain_pending_after_turn`
变可用、`/health` 的 fsm 字段与 debug 面板报 idle。**不**取消后台任务、**不**丢用户输入、
**不**失聪（相位到 IDLE，仍 accepting）。相位不自愈，直到下一个回合边界才重算。

### Existing guards
取消臂的 turn-scoped 判据（`loop.py:717-720` 的设计意图 + 守卫
`test_pre_thinking_cancel_preserves_other_turn_background_phase`）；**异常臂无对应守卫**。
AD8 用例把 `is_completely_idle` 写成前提 ⇒ IDLE_BG 变体零覆盖（且 clobber 恰好让该前提为真）。

### Classification
**CONFIRMED** / INTERNAL / LOW / E2(10)+E2(14) / `INDEPENDENT`（与 KB-47 不同 root）。

### Decision
**KEEP_OPEN**（本批只 triage）。修法方向（未做）：`finally` 里的 canonical 收口改用与本回合
相同的 ownership 判据（`_generation_ctx is not None` 才收口），并顺带修 `_cancel_desktop_calls`
缺失的回合归属（Batch 14 新登记）。不改的理由：Gate A/B 均不满足（INTERNAL/LOW），且与
KB-47 不同 root（§48/§50：一个 cluster 上限）。

---

## 9. KB-46（原 N7）— Claimed But Not Recorded Input

### Original claim
输入已被 active turn claim、`_generation_ctx` 已建立，但 `session.add_message` 尚未完成时
发生抢占/取消 ⇒ 该输入完全不出现在 session history 中。

### Static analysis（窗口与入口）
- 窗口 = `loop.py:655`（commit point）→ `loop.py:1021`（`session.add_message`）。中间
  **真挂起点 3 个**：`enter_thinking()` 的 `_notify`（`fsm.py:159`）、
  `generation_registry.register`（Mongo upsert，`loop.py:675`）、
  `_build_turn_prompt → build_messages`（`loop.py:1011`；Milvus ANN + 12 段 prompt 渲染，
  假存储下限实测 **32.7 ms**）。
- 落点决定处置：A/B 段（`_prepare_turn` 之内）→ `_handle_prepare_cancelled` → **有**
  `disposition=cancelled_pre_ownership`；**C 段**（`_think_loop` → `_build_turn_prompt`）
  → `loop.py:962-965` → 只有 `<<< 用户打断`，**零归因**。
- 触发入口只有**直投**三条（不经 `process_message` ⇒ 不过忙判定）；文本路径在 claim 之后
  必然排队（`is_accepting_input` 为假）⇒ 到不了 `cancel_current`。

### E2 reproduction
`tmp/b14/subC/probe_n7.py`（5 例；屏障装在 `PromptAssembler.build_messages`，第二条经**真实
生产函数** `core.input.ws_handler.do_transcription(...)` 投递）：

```text
① 窗口快照 = {phase:'thinking', owns_input:True, 第一条已在 session:False, pending:[], bus:0}
② 抢占时刻 = {turn.cancelling():1, phase:'thinking', 第一条已在 session:False}
③ session user = ['（暖场）','N7-第二段（STT 直投）']      ← 第一条**不存在**
④ message_archive 也没有；⑤ 两条 LLM prompt 均不含它
⑦ 第一条 generationId 上的帧只有 1 条 generation.failed{code:GENERATION_CANCELLED}，无 started
⑧ cancelled_pre_ownership 日志 = []      ← ★ 完全无归因
⑩ STT 转写回显 = [{'type':'transcription','text':'N7-第二段…'}]  ← 前端就是这样乐观渲染的
用例 5：把抢占换成**生产停止按钮**（INTERRUPT）落在同一窗口 ⇒ 同样丢、同样零归因
```

### Session History Contract
判定 = **(A) "用户真的发过的每一条"**。证据：`loop.py:1019-1028` 的唯一豁免是
`is_task_result`（合成系统唤醒），紧邻注释"此后用户至少能在历史里看到'我说过这句'"；
反证 (C)：`session.add_message` 在 LLM 首轮**之前**，且 `_archive_partial_reply` 会把
"已发出但被打断"的半截回复照样归档；反证 (B)：会直接废掉 AD1/AD2 的 `accepted` 合同。
客户端把服务端历史当权威转写稿（`chatStore.ts:67` 乐观渲染 → `useWebSocket.ts:50-53`
`restoreFromServer` **整体替换**）⇒ 刷新/重连即"消息消失"。

### Claim vs Record semantics
`CLAIMED`（`_generation_ctx` 非 None）≠ `RECORDED`（`session.add_message` 已执行）≠
`MODEL_CONSUMED`（LLM 收到）≠ `TERMINAL`（generation 终态）。现状保证的是
"CLAIMED ⇒ 有 owner 或显式终态（**日志级**）"，**不**保证 CLAIMED ⇒ RECORDED；
KB-46 正是这四条之间的一个缺口，且 C 段连"显式终态"都没有。

### User impact
**静默丢失 + 持久化的会话真相错误**：UI 上刷新即消失；**后续所有回合的 LLM 上下文都没有
它**；v2 同 `clientRequestId` 重试只被逐字重放 `accepted`，协议给的"换新 clientRequestId
重发"恢复路径也失效（回放里根本没有这条）。STT 路径更彻底（无 clientRequestId，
音频段已消费，无从重试）。

### Existing guards
AD2 的 `test_cancel_after_commit_but_before_history_gets_terminal_disposition`（只覆盖
register 子窗口）、AD4 的 `test_cancel_after_history_append_does_not_duplicate_processing`
（边界另一侧）、PO3 的直投越队守卫（前提是等待集非空）。**C 段零覆盖**。

### Classification
**CONFIRMED** / **USER / HIGH** / E2(14) / `SAME_ROOT_DISTINCT_INSTANCE`（与 KB-24 同族：
"已 accepted 的输入在某个窗口里没有 owner / 没有真相"，但边界点与修法都不同）。

### Decision
**OPEN** — `PRODUCT_DECISION_REQUIRED`（会话历史合同 A vs B）＋ `DEFERRED_BY_BUDGET`
（§50 单 cluster 上限）。**本批不修的两条真实理由**（原第三条 `DEPENDENCY_ON_KB-30`
**已被 Challenger 证伪并撤回**）：

1. **§50 的单 cluster 上限**：本批 production 范围最多一个 root-cause cluster，而 KB-46 与
   KB-47 **不同 root**（§49 明确禁止"因为都叫 ownership 就合并"）。
2. **"claimed ⇒ recorded" 必须贯通两个落点**：我独立验证（`tmp/b14/main/probe_w1_w2.py`）
   证明 Challenger 那 14 行补丁**只覆盖 W2**——
   `[W1] session=['（暖场）'] in_history=False`（取消落在 `_prepare_turn` 的
   `register`/Mongo 上，走 `_handle_prepare_cancelled`，那里**拿不到消息内容**）与
   `[W2] first_recorded=True`（取消落在 `_build_turn_prompt`）**是同一个 ownership 状态**
   却给出两种会话真相。完整修法 = 两处共用同一 helper + 在 `_prepare_turn` 暂存
   `(content, message_id)`（≈25–30 行），并配 W1/W2 各自的守卫。

**已撤回的错误主张**：原写"正确修法需要 message append 与 generation 行的原子事务 ⇒
`DEPENDENCY_ON_KB-30`"。Challenger 反证：`session.add_message`（`loop.py:1021`）与
generation 行（`loop.py:675`）本来就无事务耦合，canonical KB-30 讲的是 `_prepare_turn`
状态不回滚 / `_clock` 残留；14 行 append 补丁实测即恢复真相且 798 用例全绿
（`tmp/b14/challenger/n7_minfix.patch` + `n7_minfix_result.txt`）。

**按 §51 的优先级，KB-46 高于 KB-47**（USER/HIGH 静默丢失 > DEPLOYMENT/MEDIUM 复活）。
本批选择后者的**唯一**依据是 §50 的预算约束与"两处落点须一致"的技术门槛；
若 owner 认领不同，回退 KB-47、改修 KB-46 的路径已经就绪（补丁 + 探针 + 我追加的 W1/W2 证据）。

**给 Batch 15 的施工图**（本批不实施）：
1. 在 `_prepare_turn` 里（与 `_admission_message_id` 同一处、同一同步段）暂存
   `_pending_user_record = (content or [{"type": "text", "content": user_message}], message_id or None)`，
   并在 `process()` 入口连同其它回合级状态一起清空。
2. 新增 `CognitiveLoop._record_claimed_input_if_pending(session)`：`not self._turn_message_persisted
   and not is_task_result and self._pending_user_record is not None` 时按 `loop.py:1022-1025`
   的同一构造 append（**复用**预分配 `message_id`，保证与 ACK/幂等表一致），置
   `_turn_message_persisted = True`，并记 `disposition=cancelled_after_claim_persisted`。
3. **两个落点都调用**：`_handle_prepare_cancelled` 的 ctx 非 None 分支（W1）与
   `_think_loop` 的 `except asyncio.CancelledError`（W2）；异常臂（`_handle_turn_exception`
   的 ctx 非 None 分支）是否同样落历史需 owner 一并裁定。
4. 守卫：W1/W2 各一条（断言"恰一次、带预分配 messageId、进 archive、进**下一回合** LLM
   prompt"，并断言 `is_task_result` 回合并**不**落历史），＋ 反例（cancel 在 append 之后
   ⇒ 不得重复，复用既有 AD4 用例）。

## 10. KB-47（原 N8）— Stop/Restart Input Resurrection

### Original claim
`Agent.stop()` 清理 pending，但不清 EventBus 中已驻留的 user input；残留 input 跨
stop/start 边界，在 Agent restart 后被重新消费。

### Static analysis
- `stop()`（`lifecycle.py:255-308`）函数体 **AST 零引用 `_bus`**；`_stop_event_task` 只
  `cancel_and_drain(self._event_task)`；`asyncio.Queue.get()` 被取消**不取走元素** ⇒ 队列保留。
- `start()`（`:191-253`）**第一行**就 `_state = RUNNING`，而消费者在第 4 个启动步骤
  （`_start_idle_and_events`）才 spawn ⇒ 新 epoch 的第一个 `bus.next()` 就会取到残留。
- 窗口宽度 = 消费者两帧之间的真挂起点：`_handle_state_change → output.notify_state`
  （逐通道 await，每回合 3~4 次）与 `_handle_task_result → _mark_task_result_delivered`
  （`_TASK_RESULT_STORE_TIMEOUT=2.0`，最长 2s 的 Mongo 写）。
- **既有合同早已写明**（见 §12 与 §16），代码只做了 pending 那一半。

### E2 reproduction（两个独立探针）
**探针 1（主 Agent 独立复现，`tmp/b14/main/probe_n8.py`，后转正为 tracked 测试）**：

```text
[probe] stop 后总线驻留 user_input=1 ids=('003',)
[probe] restart 后是否被处理=True session_user=['（暖场）','W','M1-停机前已接受'] llm_calls=3
[probe] disposition 日志命中=[]
```

**探针 2（subD 独立复现，6 例 + no-op 对照）**：

```text
[step1] bus 驻留：pending_user_inputs=1 ids=('003',)；pending_messages=[]
[step2] stop() 返回：run_state=STOPPED event_task=None
[step2] stop 之后 bus 驻留：pending_user_inputs=1（stop 清了 pending，没清 bus）
[step4] 历史新增 ['M1']；含 'M1' 的 prompt=1；帧 ['turn_started()','turn_completed(stop,…)']
pristine HEAD 6 passed（断言"缺陷存在"）→ 修后 2 failed / 4 passed
```

### Runtime Epoch Contract
`stop()→start()` = **新 runtime epoch**。证据：`start()` 做 `fsm.reset()` +
`reset_lifecycle_state()` + `_started_cognitive_tasks.clear()` + 新消费者；
`clear_work()` docstring "生命周期停止后清空引用计数，**避免同一 Agent restart 残留**"；
ARCH-INC-P2-03 明文以"Agent 同一实例 `stop()`→`start()`"为前提。

### Stop/Restart matrix（只填当前项目**已有**合同）

| Work type | stop | restart | 依据 |
|---|---|---|---|
| active user turn | 取消 ＋ 显式终态（CF1–CF6） | 不复活 | `_stop_cognitive_and_tts` + `_drain_cognitive_turns` + Batch 11 |
| pending user input | 清空 ＋ 可归因 ＋ 释放 lease | 不复活（连回合都不许起） | PO6 + `test_shutdown_does_not_drain_pending` |
| **EventBus user input** | **结束 ownership ＋ 可归因丢弃**（Batch 14 新增） | **不复活** | **RO1**（本批）＋ AD5 + §4.6 已知边界 |
| task result（durable） | **保留**（不丢） | 允许重投（at-least-once） | T3/T5/T6/T7 + `_redeliver_pending_task_results` |
| system tick | 不承诺（epoch 内的触发） | 不复活（RO1 不覆盖；→ AR-8 残余面） | IdleMonitor 随停机停转 |
| background workflow | 引擎停 ＋ mailbox 收尾排空 | 终态经 WAL/仓储补投 | S2-A4 + §2 T 系列 |

### User impact
被放弃的消息在重启后被当新输入：进 session 历史、喂 LLM、产生正常回复与状态帧、**可能
再次触发工具副作用**（提醒/文件/desktop，因为整条 ReAct 回合会重跑），且用户无法与正常
回合区分。**不敢夸大的部分**（Challenger 反证）：默认重启**重建 Agent 实例**，旧实例的
"第一份"根本活不到第二次执行 ⇒ "用户重发导致同一指令执行两次"**在生产默认路径上不成立**，
只在原地重启模式下才可能（而且那样一来用户看到的是"重启后它自己开始做那件事"）。二阶效应：若**同一会话**在重启后有新的 v2 输入在途，残留回合
done callback 的迟到 `release_work` 会把计数 1 打到 0（**over-release**，削弱
`SessionBusyError` 删除门禁）——无异常、无负数、LOW（已登记为 KB-47 的附注）。

### 可达性研判（诚实定级）
默认生产入口 `/restart` **重建 Agent 实例**（`run_agent_service`；`run_server` 默认
`rebuild_on_restart=True`）⇒ 旧 bus 随实例丢弃；`/shutdown`/SIGTERM 结束进程。因此复活
形态落在**受支持的原地重启模式**（`rebuild_on_restart=False`，仓内无生产调用方）与库式
嵌入的 `stop()→start()`。⇒ 定级 **DEPLOYMENT / MEDIUM**，**不**宣称 USER/HIGH。

### Existing guards
pending 侧：`test_shutdown_does_not_drain_pending`（PO6）；停止按钮侧：
`test_stop_button_clearing_pending_is_attributable`（PO5/AD5）；durable 侧：
`test_terminal_event_arriving_during_shutdown_window_is_delivered`、
`test_unconsumed_result_is_pending_and_redelivered_next_start`。**总线驻留的 user input 无守卫**。

### Classification
**CONFIRMED** / DEPLOYMENT / MEDIUM / E2(14)×2 / `SAME_ROOT`（与已确认并部分修复的
CR-08 / S2-R2-4 缺陷族同 root）。

### Decision
**FIX_NOW**（Gate C：与已确认 root 同一缺陷族，修复不可合理拆分——只修任一半都留下复活
路径）。详见 §14–§19。**残留**（清扫之后才投递的事件）登记 **KB-48**，不在本批修。

---

## 11. KB-48（新）— 停机窗口内输入层仍有两条入口投递

### Original claim（本批新发现）
`core/input/ws_handler.py::do_transcription`（`:733`）与 `core/input/ws_v2.py::_handle_voice_commit`
（`:787`）直接 `bus.post(UserInputData(...))`，**不查** `status.is_service_running`；而同类
入口 `ws_handlers/chat.py::do_interrupt`（`:136-141`）与 `voice_sessions.py` REALTIME
（`:687-691`）已按 S2-R2-4（`c029110e`）加了该判据，其注释自陈"停机窗口内输入层**不再向
bus 投任何事件**……残留事件跨运行被消费本身就是缺陷族"。

### Static analysis
四条直投入口的判据分布：`do_interrupt` ✅ / REALTIME ✅ / `do_transcription` ❌ /
voice.commit ❌。RO1 的清扫在 `stop()` **最后一步**，因此清扫之后（停机尾部 /
`runner.cleanup()` 期间）这两条入口仍可把事件投进总线 ⇒ 跨 epoch 存活。

### E2（Challenger 实测）
`stop()` **返回之后**调用真实 `do_transcription(...)`：转写照常回显给客户端，事件落在总线上
（`bus.pending_user_inputs == 1`）；随后 `start()` 把它消费进 session 与 LLM prompt（+1 次
调用）。⇒ 后果不是"理论上的跨界"，而是**一条已 ACK/已回显的输入在 `runner.cleanup()` 窗口里
静默丢失或跨界复活**——而清扫点是 `stop()` 的最后一步，窗口在其之后仍然敞开。

### Classification
INTERNAL / LOW / **E2(14)** / `SHARED_MECHANISM`（与 KB-47 同族、**不同权威**：输入层准入 vs.
Agent 停机边界）。

### Decision
**OPEN** `NEW_FINDING_OUT_OF_SCOPE`。修法方向：两条入口补同一 `is_service_running` 判据
（`voice.commit` 复用既有 `_not_accepted_error(agent)` → `AGENT_BUSY`，**零新增 wire 枚举**）。
不并入 KB-47 的理由：§48 的合并标准要求同权威/同 fix，而这两条属输入层准入面。

---

## 12. Triage Decision Table

| ID | Confirmed? | Reachability | Severity | Root | Existing guard | Decision |
|---|---|---|---|---|---|---|
| **KB-26** | 状态成立 / **缺陷证伪** | INTERNAL | LOW | INDEPENDENT | fully（`.done()` ×4 + AD7） | **KEEP_AS_IS** |
| **KB-28** | 主形态**证伪**；残留不可达 | CURRENTLY_UNREACHABLE | LOW | SAME_ROOT_DISTINCT_REACHABILITY（KB-41/45） | 主形态 fully（AD3/AD7 + PO3） | **RECLASSIFY** |
| **KB-29** | **YES**（E2） | INTERNAL | LOW | INDEPENDENT | partially（仅取消臂） | **KEEP_OPEN** |
| **KB-46**（N7） | **YES**（E2） | **USER** | **HIGH** | SAME_ROOT_DISTINCT_INSTANCE（KB-24） | partially（AD2 只覆盖前半段） | **OPEN**（产品决策 + KB-30 依赖） |
| **KB-47**（N8） | **YES**（E2×2） | DEPLOYMENT | MEDIUM | SAME_ROOT（CR-08/S2-R2-4） | partially（pending 有、bus 无） | **FIX_NOW** |
| **KB-48**（新） | 静态确证 | INTERNAL | LOW | SHARED_MECHANISM（KB-47） | partially（2/4 入口） | **OPEN**（新登记） |

---

## 13. Root-Cause Consolidation

- **KB-47 ≡ CR-08 / S2-R2-4 缺陷族**（`SAME_ROOT`）：root = "**非 durable 的 accepted 用户
  输入不得跨越 runtime epoch 边界**"。`c029110e` 修的是**入口侧**（停机窗口不投递）、
  且只覆盖 4 条入口里的 2 条；`Agent.stop()` 只清了 pending。**两侧属同一 invariant**：
  只修边界 → 窗口内投递仍跨界；只修入口 → 停机前已驻留的仍跨界。⇒ **不可合理拆分**，
  按 Gate C 合并为一个 cluster（本批只做边界侧；入口侧残留登记 KB-48，因**权威不同**）。
- **KB-46 独立**：root = "已 claim 的输入在进历史前被取消 ⇒ 会话真相缺失"。失败模式相反
  （**丢失** vs **复活**）、权威不同（turn 取消 vs. epoch 边界）、修法不同（时序/事务 vs.
  边界清扫）⇒ §49 明确不合并。
- **KB-29 独立**：root = "pre-ownership 的 canonical 收口没有 turn-scoped 判据"（异常臂
  缺取消臂已有的判据）。
- **KB-26 / KB-28**：状态成立/窗口真实，但用户可见后果在 HEAD 上被**证伪**（一个由
  `.done()` 守卫、一个由 Batch 12/13 的排队判据）⇒ 不构成 root。
- **AR-8**（父项）：KB-47 是其首个具体实例；AR-8 的缓解论据（"stop 已 cancel event task"）
  被证伪，故 AR-8 保留、改写缓解说明，残余面（StateChange / SystemTick / 陈旧 Interrupt
  同样跨界存活）**未修**。

---

## 14. Production Fix Selection

选择 **KB-47** 一个 cluster。理由与"为什么不修其它项"：

| 候选 | 是否 FIX_NOW | 理由 |
|---|---|---|
| KB-46（USER/HIGH 静默丢失） | ❌（本批） | **可最小修复**（Challenger 反证 + 我的追加验证，见 §20.1/§20.2）：撤回原"KB-30 事务依赖"。**本批不做的真实理由只有两条**：① §50 把本批 production 范围硬性限制为**一个** root-cause cluster，而 KB-46 与 KB-47 不同 root（§49 禁止因为都叫 ownership 就合并）；② "claimed ⇒ recorded" 必须**贯通两个落点**（W1 `_prepare_turn` 之内 / W2 `_build_turn_prompt` 之内），只修 W2 会把同一个 ownership 状态变成两种会话真相——完整修法需要共用 helper + 在 `_prepare_turn` 暂存消息内容（≈25–30 行）。**按 §51 的优先级，KB-46 高于 KB-47**：若 owner 认为本批应当修的是它，可回退 KB-47 补丁并以 `tmp/b14/challenger/n7_minfix.patch` 为起点（已实测恢复真相 + 798 绿），**建议直接排为 Batch 15 的第一项**。 |
| KB-29（INTERNAL/LOW） | ❌ | Gate A/B 均不满足（非 USER、非 HIGH）。修法方向已写明，留给后续小批。 |
| KB-26 / KB-28 | ❌ | 已证伪（无用户可见后果）⇒ KEEP_AS_IS / RECLASSIFY。 |
| KB-48 | ❌ | 与 KB-47 同族但**不同权威**，§48 不允许合并；独立小批更清楚。 |
| **KB-47** | ✅ | ① 有 **E2**（两个独立探针）；② 破坏的是一条**已写下**的 lifecycle invariant（AD5 / PO5 / §4.6 已知边界）；③ 与已确认缺陷族同 root、修复不可拆分（Gate C）；④ 修法**最小且可验证**（2 文件 +85 行、零 wire/schema/持久化/前端变化）；⑤ 用户可见后果明确（复活 + 重复执行风险）。**诚实边界**：可达性是 DEPLOYMENT 而非默认 USER 路径，定级 MEDIUM——**不**为此抬高定级。 |

---

## 15. Pre-Fix Reproduction

**PRE_FIX_RED 证据（同一 tracked 测试文件，生产代码未改）**：

```text
$ git checkout -- core/monitoring/event_bus.py core/agent/lifecycle.py   # 只留测试文件
$ conda run -n Elysia python -m pytest core/agent/tests/test_runtime_epoch_input_ownership.py \
    -q --no-header -p no:randomly --tb=line
E AssertionError: 停机边界未结束未接管输入的 ownership：总线仍驻留 1 条 UserInputData (ids=('003',))
    assert 1 == 0
E AssertionError: 停机清扫必须移除未接管的 user input
    assert 1 == 0
2 failed, 1 passed in 0.55s
```

修后（同一命令）：`3 passed in 0.48s`；`core/monitoring/tests/test_event_bus_ownership.py`
`4 passed`。

**"真实错误"而非"变量不干净"**：断言的是**产品合同行为**——① 停机返回后总线不得再驻留
未接管输入（RO1）；② 该输入不得在新 epoch 进入 session 历史、不得被喂给 LLM、不得出现在
LLM prompt 里（三层身份证据）；③ 丢弃必须可归因（`cancelled_by_shutdown`）；④ durable
终态必须原位保留。**没有**断言 `_current_task is None` 之类的内部变量形状。

---

## 16. Root Cause

`Agent.stop()` 只在**一个**容器上结束了 "非 durable accepted 输入" 的 ownership
（`_pending_messages`），而这类输入有**两个**容器（pending ∪ **EventBus 驻留**）。由于
`EventBus` 的生命周期 = Agent 实例生命周期，同一实例 `start()` 之后新 epoch 的消费者会把
残留 USER_INPUT 当**新输入**处理。既有合同（AD5 / PO5 / §4.6 已知边界 / S2-R2-4）早已把
"停机即结束 ownership、不得复活"写成不变量，**代码只实现了 pending 那一半**。

---

## 17. Fix

### before
```text
Agent.stop()
  ├─ _state = STOPPED
  ├─ _pending_messages 清空 + 释放 lease          ← 只做了这一半
  ├─ _SHUTDOWN_STEPS（20 步，含 stop_event_task → 取消消费者）
  ├─ _lifecycle_tasks.drain(2.0)
  └─ _session_manager.clear_work()
     ⇒ 总线里仍可能留着 UserInputData（取消消费者 ≠ 清队列）
Agent.start() → 新消费者 → bus.next() 取到残留 → 当新输入处理（复活）
```

### after
```text
core/monitoring/event_bus.py
+ EventBus.discard_user_inputs() -> list[Event]
    drain() 取出全部 → 按 event.type 分拣：USER_INPUT 移除、其余按原 priority/type **重投**
    （同步、无 await ⇒ 不交错；重投按原出队顺序 ⇒ 幸存者顺序与计数簿记不变）

core/agent/lifecycle.py
Agent.stop()
  ├─ _state = STOPPED
+ ├─ pending 清空前记 disposition=cancelled_by_shutdown + message_ids   ← 补全可归因半边
  ├─ …（不变）
  ├─ _lifecycle_tasks.drain(2.0)
+ ├─ _discard_abandoned_user_inputs()      ← 边界清扫（最后一步）
  │    逐条记 disposition=cancelled_by_shutdown + message_id/conversation_id
  │    逐条释放 v2 session-work 引用（_release_session_work）
  └─ _session_manager.clear_work()
```

> 两处归因（pending 与总线）是**同一个不变量 RO1 的两半**：停机边界结束 ownership 时
> 必须同时**可归因**。修前 pending 那半是清空的、却没有日志，而停止按钮路径对同一类处置
> 记 `cancelled_by_interrupt` ⇒ 同一处置在观测面上一半可归因一半不可归因（该缺口由本批
> Challenger 反证发现，随即在**同一 cluster** 内补全）。

### why minimal
1. **一个权威点**：所有权只在 `stop()` 结束，**不做** restart 侧过滤（§32）。理由不只是
   偏好：`start()` 在 spawn 消费者**之前**就已 `_state = RUNNING`，那期间被接受的输入是
   **合法的新 epoch 工作**，任何 start 侧清扫都会误伤它们（本批用三条用例钉住这条活性）。
2. **按类型分类、不是 `clear()`**：durable `TaskResultData` 原位保留（§34 硬边界）。
3. **放在 `_SHUTDOWN_STEPS` 之后**：同时覆盖停机窗口内输入层仍可能投递的残留事件
   （KB-48 的两条入口），否则清扫会漏掉它们。
4. **零 wire / 零 schema / 零持久化 / 零前端 / 零新框架**：不引入 `RuntimeEpoch`
   manager/字段、不改 FSM/pending/lock、不加 watchdog/polling/超时。
5. **幂等且 fail-safe**：清扫自身异常只记 warning，不打断停机（CR-02"进入 stop() 就走完"）。

---

## 18. Negative Boundaries

| 不得回归的边界 | 本批如何保证 |
|---|---|
| **durable `TaskResultData` 不能被误清** | `discard_user_inputs` **按 event type 分类**；`test_stop_boundary_preserves_durable_events` 断言"幸存者里仍有 TaskResultData、已无 UserInputData"；4 条 EventBus 单测钉分类/顺序/空集/已派发者；变异 **M2（粗暴全清）** 被杀（4 条守卫红） |
| **pending ordering（PO1–PO6 / PO-L）不能回归** | 未触碰 pending 容器与两个出队口；PO 套件全绿（§28）。清扫只作用于**总线**，且与 pending 互斥（单 claim） |
| **cancellation lifecycle（SP/TH/CF）不能回归** | 未触碰取消/收口路径；测试放在 `_SHUTDOWN_STEPS` 之后，不改变回合收口顺序；CF/TH/SP 套件全绿 |
| **shutdown 不能复活工作 / 不能产生新回合生命周期** | 清扫是**纯同步**、只丢弃、不 spawn；`test_shutdown_does_not_drain_pending`（PO6）与 CF6 用例保持绿 |
| **停机有界** | 清扫无 await、无网络、无锁等待（`stop()` 的预算链不变：`_turns.drain(3.0)` / `_lifecycle_tasks.drain(2.0)`） |
| **新 epoch 活性** | `test_new_epoch_still_accepts_and_processes_fresh_input` 断言 `start()` 之后新输入照常被处理、相位回到 IDLE |
| **旧 epoch 的残留不得反过来影响新 epoch 的正常输入** | 清扫后 `pending_user_inputs == 0`（RO1 第一层断言） |

---

## 19. Regression Tests

| 分类 | 用例 | 说明 |
|---|---|---|
| **PRE_FIX_RED** | `core/agent/tests/test_runtime_epoch_input_ownership.py::test_stop_boundary_ends_input_ownership_and_prevents_resurrection` | 修前红在"停机后总线仍驻留 1 条 UserInputData"；修后同时满足四条断言（驻留=0 / 可归因 / 不进历史 / 不喂 LLM） |
| **PRE_FIX_RED** | `::test_stop_boundary_preserves_durable_events` | 修前红在同一处；修后断言分类正确（durable 保留、user input 移除） |
| **ALREADY_GREEN_GUARD** | `::test_new_epoch_still_accepts_and_processes_fresh_input` | 修前修后都绿（活性对照，防"清扫把新 epoch 也扫了"） |
| **FALSIFICATION_GUARD** | `core/monitoring/tests/test_event_bus_ownership.py`（4 例：分类 / 幸存者顺序 / 空集 no-op / 已派发者不受影响） | 杀 M2（粗暴全清）与"改顺序/漏簿记"的修法 |
| **PRE_FIX_RED** | `::test_stop_boundary_attributes_queued_pending_input` | RO1 的"可归因"半边：修前 `dispositions=[]`（1 failed / 3 passed）；修后绿。Challenger 反证发现——停机清 pending 一直零日志，而停止按钮路径记 `cancelled_by_interrupt` |
| **PRODUCT_CONTRACT_GUARD** | `core/agent/tests/test_pending_input_ordering.py::test_shutdown_does_not_drain_pending`（PO6）、`test_admission_input_delivery.py::test_stop_button_clearing_pending_is_attributable`（PO5/AD5）、`test_shutdown_mailbox_delivery.py::test_terminal_event_arriving_during_shutdown_window_is_delivered`（T 系列） | 既有合同守卫，本批**未改**且保持绿 |
| **PROBE（未转正）** | `tmp/b14/subA/probe_kb26.py`(5) / `probe_kb28.py`(4) / `tmp/b14/subB/probe_kb29.py`(2) / `tmp/b14/subC/probe_n7.py`(5) / `tmp/b14/subD/probe_n8.py`(6) | 证伪/确证证据；`tmp/` 在 `norecursedirs` 内 ⇒ **不进常驻套件**。KB-46 与 KB-29 若要长期钉住，需在后续批次把探针转正 |

---

## 20. Challenger Findings

独立 Challenger（自己的仓副本内做回退/变异，**未**改动共享工作树；产物
`tmp/b14/challenger/**`）逐项攻击后，**打破了我两条判断、确认了机制层结论**。

### 20.1 被打破的主张（**必须写进结论**）

| # | 我原先的主张 | Challenger 的反证 | 处置 |
|---|---|---|---|
| **C1** | "N7 的正确修法要 message append 与 generation 行**原子事务** ⇒ `DEPENDENCY_ON_KB-30`" | **误归因**：`session.add_message`（`loop.py:1021`）与 generation 行（`loop.py:675`）本来就**没有事务耦合**，canonical KB-30 讲的是 `_prepare_turn` 状态不回滚 / `_clock` 残留，与 append 无关。而且 14 行补丁（在 `_think_loop` 的 `except CancelledError` 里补 append）实测**恢复了会话真相**：victim 带预分配 `messageId` 进 session、恰一次、进 archive、进**下一回合**的 LLM prompt，**零额外 LLM 调用**，`core/agent/tests core/cognitive/tests` **798 passed** | **撤回** `DEPENDENCY_ON_KB-30`；KB-46 的 decision 改为 `PRODUCT_DECISION_REQUIRED`（合同 A/B 未裁）＋ `DEFERRED_BY_BUDGET`（§50 单 cluster 上限），并写明"可最小修复" |
| **C2** | "只修 C 段（`_think_loop`）只是可观测性补丁、不恢复真相" | **部分证伪**：append 版确实恢复真相（见 C1）；只有 disposition 版才是纯可观测性 | 同上 |
| **C3** | "KB-48 是 E1（静态）" | Challenger 给出 **E2**：`stop()` 返回后真 `do_transcription` 仍能把事件投到总线（`pending_user_inputs==1`、转写已回显给客户端），`start()` 后进 session + LLM prompt（+1 次调用） | KB-48 证据级别 E1 → **E2** |
| **C4** | （未主张，但它是同族缺口） | `Agent.stop()` 清 `_pending_messages` **零归因**（`lifecycle.py:271-273`），而停止按钮路径对同一类处置记 `cancelled_by_interrupt` ⇒ 本族里**唯一生产可达的静默丢弃**（回合进行中连发两条 + `/restart`/SIGTERM） | **已在同一 cluster 内修复**（RO1 的"可归因"半边），并配 `::test_stop_boundary_attributes_queued_pending_input`（PRE_FIX_RED 实测 1 failed → 修后绿） |

### 20.2 我在 Challenger 结果之上**追加的独立验证**（W2-only 不足）

我独立复现了 C1 的补丁（临时应用 → 跑 subC 探针 → 恢复），确认它**确实**让被抢占的输入进
session（探针三条"缺陷存在"断言全部变红，方向正确）。但我**进一步**用自制探针
`tmp/b14/main/probe_w1_w2.py` 证明该补丁**只覆盖两个落点中的一个**：

```text
[W1] session=['（暖场）'] in_history=False      ← 取消落在 _prepare_turn（register/Mongo）之内
[W2] session=['（暖场）','W2-已claim但被抢占','W2-直投的第二条'] first_recorded=True
```

两者**是同一个 ownership 状态**（`_generation_ctx` 非 None、`_turn_message_persisted` False、
同样被直投输入抢占），只是一个落在 `_prepare_turn`（走 `_handle_prepare_cancelled`，
那里**拿不到消息内容**——方法签名只有 `self`），一个落在 `_build_turn_prompt`。
⇒ "claimed ⇒ recorded" 要真正贯通，必须**两处共用同一个 helper**，并在 `_prepare_turn`
里把消息内容/ID 像 `_admission_message_id` 那样先暂存（≈25–30 行，而不是 14 行）。
这条追加证据正是本批**不**仓促落 N7 修复的技术理由（只修 W2 会把同一个 ownership 状态
的两种落点变成两种会话真相）。

### 20.3 被确认（我无法打破）的结论

- **pre-fix RED 成立**：回退补丁后既有 10 个套件 + `core/cognitive/tests` = **462 passed**
  ⇒ KB-47 **不是** ALREADY_GUARDED，新守卫非冗余；修后 `core/agent/tests + core/cognitive/tests`
  = **798 passed**。
- **守卫有鉴别力**：Challenger 自建变异矩阵 M1（`return self.drain()` 粗暴全清）→ 4 red；
  M2（重投丢 `priority`）→ red；M3（无归因日志）→ red；M5（"丢事件+记账归零但**留在队列里**"）
  → 由**复活层**杀死（`停机丢弃的输入在新 epoch 复活并写进了会话历史`）；M6（no-op）→ red。
  ⇒ 我的守卫**不是**只查计数。
- **ownership 真值**：被 active turn 接管的输入**已离开队列** ⇒ `discard_user_inputs()` 返回空、
  不可能"偷走"已归属输入（Challenger probe C：`current_turn_owns_input=True`、
  `bus.pending_user_inputs=0`、`dropped=0`）；清扫点前一步已 `cancel_and_drain` 消费者 ⇒
  无活消费者。**§42 的攻击失败。**
- **USER 可达性**：默认重启**重建 Agent 实例**（`test_restart_rebuild.py` 钉住）⇒
  KB-47 的复活形态**不是**用户路径。这与我在 §10 的定级一致（DEPLOYMENT/MEDIUM）；
  Challenger 也指出"用户重发导致二次执行"的放大在生产上**不成立**（第一份根本活不到 restart）。
  我接受这一更正并在 §10/§1 保持 DEPLOYMENT 定级、删去该放大措辞。
- **无生产回归**：停机清扫不影响旧实例之外的任何路径。

### 20.4 接受的风险 / 已知弱点（不改）

| 项 | 说明 |
|---|---|
| `release_work` 在停机边界是"记账不可见" | 紧随其后的 `clear_work()` 同步清空同一张表；`release_work` 幂等 ⇒ 无重复释放 / 无计数损坏 |
| `seq` 在重投后变化 | 无生产读者（只用于优先级队列内部排序），且重投同步进行 ⇒ 相对顺序不变 |
| `data is None` 事件在重投时被丢弃 | `post()` 无法构造该形态 ⇒ 不可达；已在方法 docstring 写明为"已知边界"，不写成绝对承诺 |
| 引用"既有终态合同"的**出处** | 该句是 Batch 13（`2d0a5c5c`）写入 `invariants.md` 的，不是远古合同；但 AD5（Batch 12）本身已写"显式取消与**停机**不得复活"，输入层 S2-R2-4 亦以"残留事件跨运行被消费本身就是缺陷族"为前提 ⇒ 合同存在且被两处独立表达，只是**代码一直没做**。已在 §16 措辞上区分"合同入档时间"与"合同内容" |
| §4.7 曾短暂指向尚未创建的记录文件 | 本次提交内已补齐（同批落盘） |

### 20.5 新发现（Challenger，已登记）

- `Agent.stop()` 清 pending 零归因 → **本批已修**（同 cluster）。
- KB-48 有 **E2** 级证据、且后果是"停机窗口尾部一条已 ACK/已回显的输入静默丢失"
  （`runner.cleanup()` 仍在清扫点之后）→ KB-48 行已升级证据级别与后果描述。

---

## 21. Mutation Evidence

| 变异 | 内容 | 结果 |
|---|---|---|
| **M1** | 不做停机清扫（= 修前状态） | `test_runtime_epoch_input_ownership.py` **2 failed / 1 passed**（PRE_FIX_RED 成立；`llm_calls 1→2`、无损断言已在修前形态下被证明可观测） |
| **M2** | `discard_user_inputs` 退化为 `return self.drain()`（**粗暴全清**） | **被杀 4 处**：`test_stop_boundary_preserves_durable_events`（`survivors=[]` 无 TaskResultData）＋ 3 条 EventBus 单测（分类/顺序/空集语义） |
| **M3** | 只清 pending、不清总线（= M1 的等价形态） | 与 M1 同（红灯位置相同）⇒ 无法用"pending 里再 clear 一次"蒙过守卫 |
| **M4** | 把清扫从 `stop()` 挪到 `start()`（"restart 侧过滤"） | **被杀**：`test_stop_boundary_*` 两条在**停机后**那条断言上红（`pending_user_inputs=1`）⇒ ownership 守卫确实要求"在 stop 结束 ownership"，而不是事后过滤 |
| **M5** | 去掉 pending 清空前的归因日志（保留清空） | **被杀**：`::test_stop_boundary_attributes_queued_pending_input` 红（`dispositions=[]`） |
| **M6–M9（Challenger 独立变异矩阵）** | 重投丢 `priority` / 去掉归因 / no-op / **"丢事件+记账归零但把事件留在队列里"** | 四种全部被杀；其中最后一个由**复活层**杀死（`停机丢弃的输入在新 epoch 复活并写进了会话历史`）⇒ 守卫不是"只查计数" |

---

## 22. Task / Queue / State Leak Evidence

- **停机泄漏面收窄**：`USER_INPUT` 残留（本批消除）、pending（既有）、active turn（既有）、
  session work lease（既有 + 本批逐条释放）。
- **仍存的跨 epoch 存量**（AR-8 父项的残余面，本批**未**处理，只登记）：
  `StateChangeData` / `SystemTickData` / 陈旧 `InterruptData` 同样会跨 epoch 存活。
  实测（subD case5）：`StateChangeData` 与 `SystemTickData` 在 `start()` 后被新消费者读到。
  其中陈旧 `InterruptData` 最值得后续关注：新 epoch 消费它 → `_handle_interrupt` →
  `bus.drain()`（会把**新 epoch** 刚投进总线的事件一起清掉）——与 KB-47 同族但**尚无实例**，
  本批只登记。
- **二阶效应（已登记）**：残留 v2 输入在新 epoch 复活时，`clear_work()` 已抹掉计数，
  残留回合 done callback 的迟到 `release_work` 可能把**新**在途工作的计数打到 0
  （over-release，削弱 `SessionBusyError` 门禁）。修后该形态消失（输入不再复活）。
- **`TaskResultData` 重复交付**：既有 at-least-once 语义（T5/T7），subD 实测跨重启
  `_handle_task_result` 同 task_id 跑 2×；**不是**本批引入，也不在范围内（T 系列已接受）。

---

## 23. Public API / Schema Impact

**零**。无 wire 帧、无 `frontend_contract` 改动、无 REST、无 debug API 字段、无前端改动、
无新公开 `@property`。`EventBus.discard_user_inputs()` 与
`AgentLifecycle._discard_abandoned_user_inputs()` 均为内部方法（下划线 / 基础设施层）。
`make check-types` / `make check-contract` 应保持绿（§28）。

## 24. Persistence Impact

**零**。不加 collection / 字段 / 索引 / migration；不动 session / archive / generation
registry / WAL。停机清扫是**纯内存**操作；被丢弃的输入本就没有持久化形态
（`accepted ≠ durable`）。

## 25. Performance Impact

正常路径**零开销**（清扫只在 `stop()` 里执行一次）。停机路径的代价 = 一次
`drain()` + 一次重投，O(队列长度)（停机时通常 ≤ 个位数；即便满载也只有 pending 上界个
事件），全部同步、无 I/O、无锁等待。

---

## 26. Commits

| Commit | 内容 |
|---|---|
| `e63ff66b` | `fix(agent): discard abandoned user input across runtime restart` —— 生产改动（`core/monitoring/event_bus.py` 新增按类型分类的 `discard_user_inputs()`；`core/agent/lifecycle.py` 停机边界清扫 ＋ pending 归因 ＋ 组合面类型声明）＋ PRE_FIX_RED 守卫（`core/agent/tests/test_runtime_epoch_input_ownership.py` 4 例、`core/monitoring/tests/test_event_bus_ownership.py` 4 例） |
| `9f5daf73` | `docs(architecture): record runtime epoch ownership invariants` —— `docs/architecture/invariants.md` §4.7 RO1–RO3 ＋ §4.5/§4.6 已知边界更正 |
| `4b9fb70b` | `docs(review): record Batch 14 residual input ownership triage` —— 本文件（含 Challenger Findings 定稿） |
| `3f753ad7` | `docs(todo): register KB-46..KB-50 and reclassify KB-26/28/29` —— `todo.md`（含 AR-8 缓解说明更正） |
| *（本条）* | `docs(review): pin Batch 14 commit hashes` |

（生产 + PRE_FIX_RED 同 commit；triage/reclassify 文档独立 commit。）

## 27. Worktree Verification

- 预修复验证在**同一 worktree** 内以"只留测试文件、`git checkout` 两个生产文件"的方式完成
  （等价于 HEAD + PRE_FIX_RED → red），随后 `git apply` 回补丁 → green（§15）。
- 变异 M1–M4 各自独立施加并回滚，回滚后 `git diff --stat` 与施加前一致（§21）。
- 独立探针全部落在 `tmp/b14/**`（`norecursedirs` 内，不进测试收集），`git status` 只有
  预期的 2 个生产文件 + 2 个新测试文件 + 4 个文档改动。

## 28. Test Results

| 目标 | 结果 |
|---|---|
| targeted（新增守卫） | `test_runtime_epoch_input_ownership.py` 修前 **3 failed / 1 passed**（总线清扫 2 条 + pending 归因 1 条）→ 修后 **4 passed**；`test_event_bus_ownership.py` **4 passed** |
| Batch 12 regressions（AD） | `test_admission_input_delivery.py` + `test_pending_input_ordering.py` + `test_turn_finalization_cancellation_safety.py`：**30 passed**（subA）；主 Agent 合并跑 508 passed（见下） |
| Batch 13 regressions（PO） | 同上（PO1–PO6 / PO-L 全绿） |
| SP（SPEAKING lifecycle） | `core/cognitive/tests/test_speaking_lifecycle.py` + `core/adapter/tests/test_tts_adapter.py`：**29 passed in 1.05s**（显式回归；§61 要求的相邻边界） |
| ownership perturbation | 独立探针：`probe_kb26`(5) / `probe_kb28`(4) / `probe_kb29`(2) / `probe_n7`(5) / `probe_n8`(6) 全部在 HEAD 上复现或证伪（`tmp/b14/**`） |
| shutdown / restart | `test_shutdown_resilience` / `test_shutdown_steps_matrix` / `test_shutdown_mailbox_delivery` / `test_restart_rebuild` / `test_lifecycle_idempotency` / `test_lifecycle_flush` / `test_task_result_interrupt_delivery` / `test_task_result_durability`：**105 passed**（修前/修后各一次，subD） |
| 合并相邻套件（主 Agent 实跑） | AD+PO+CF+TH+shutdown+restart+lifecycle+task-result+`core/cognitive/tests`：**508 passed / 1 warning in 68.6s** |
| real-mongo | `make test-real-mongo`：**24 passed, 6283 deselected in 27.4s**（`docker compose -f docker-compose.test.yml up -d`，容器 `elysia-mongo-test`） |
| quick | `make test-quick`：**6238 passed, 2 skipped, 2 xfailed in 78.7s** |
| normal | `make test-normal`：**6238 passed, 2 skipped, 2 xfailed in 79.1s** |
| full | `make test-full`：**6270 passed, 36 skipped, 2 xfailed in 459.9s** |
| frontend | `cd frontend && npm run build`：**exit 0**（`✓ built in 865ms`；产物落 gitignored `static/`，`git status` 无新增） |
| static gates | `make lint` ✅ / `make pyright` **0 errors** / `check-arch` ✅ / `check-contract` ✅ / `check-types` ✅ / `check-timeouts` ✅ / `check-debug-auth` ✅ / `check-secrets` ✅ / `check-test-sets` ✅（`|Quick ∪ remainder| = 6242 == |Normal|`；全量收集 6308）/ `check-compose` ✅ / `check-env-lock` ✅ / `check-ci-isolation` ✅ |
| real CI | **待 owner push 后确认**（Backend / Real MongoDB / Frontend / 其它 required jobs）。本地无法代替。 |

> 说明：`quick`/`normal`/`full` 三轮实跑期间对两个生产文件做了 md5 稳定性校验
> （`md5sum -c`），确认结果对应的就是本批最终代码状态。
> Challenger 的独立套件结果（同一代码状态）：`core/agent/tests + core/cognitive/tests`
> = **798 passed**；回退补丁后同一集合 = **462 passed**（证明不是 ALREADY_GUARDED）。

---

## 29. Invariant Mapping

新增 **RO1 / RO2 / RO3**，落纸于 `docs/architecture/invariants.md` §4.7：

| ID | Invariant | 守卫 | 状态 |
|---|---|---|---|
| **RO1** | `Agent.stop()` 结束所有"非 durable、未被任何回合接管"的 accepted 输入的 ownership——**两个容器都算**（总线驻留 ＋ `_pending_messages`）；停机返回后总线不得再驻留 `USER_INPUT`；**两处丢弃都必须可归因**（`cancelled_by_shutdown`）并释放 v2 work 引用 | `test_runtime_epoch_input_ownership.py::test_stop_boundary_ends_input_ownership_and_prevents_resurrection` + `::test_stop_boundary_attributes_queued_pending_input` | **已建立**（GUARDED） |
| **RO2** | 停机清扫**按 event type 分类**、不得退化为全清；durable 终态原位保留；幸存者顺序与簿记不变 | `::test_stop_boundary_preserves_durable_events` + `core/monitoring/tests/test_event_bus_ownership.py`(4) | **已建立**（GUARDED） |
| **RO3** | 合法 transient（`_current_task` 陈旧 / `IDLE + lock held`）不得改变用户可见 ownership 语义 | KB-26 / KB-28 的证伪探针（`tmp/b14/subA/**`，**尚未转正为常驻守卫**） | **已证伪、未入库**（待后续批次决定是否转正） |
| **RO4**（原 §36 提议的"claimed input truth 与 conversation truth 一致"） | — | — | **未建立**：依赖 KB-46 的产品决策（会话历史合同 A vs B）。按 §36"不要在结果出来前强写 invariant"**刻意不写**。 |

---

## 30. Todo Update

| ID | 变更 |
|---|---|
| **KB-26** | 行改写：状态成立 + 缺陷**证伪**（可执行 reader census、全部 `.done()` 守卫、burst 无丢失/重复/乱序）；status `KEEP_AS_IS`；E2(14)；§4 也加一行（重开条件：出现无守卫的新读者） |
| **KB-28** | 行改写：主形态**证伪**（Batch 12 `not_owning` + Batch 13 `_has_older_waiting_input` 覆盖），残留"严格两发停止"静默但**不可达**（无挂起订阅者）；reachability → `CURRENTLY_UNREACHABLE`；§4 加一行（重开条件：引入第二个 `on_change` 订阅者） |
| **KB-29** | 行改写为 **CONFIRMED**（可达性、后果清单、产品合同 B、修法方向、`_cancel_desktop_calls` 归属缺口）；status `OPEN`（本批只 triage） |
| **N7 → KB-46** | **首次登记 canonical ID**；USER/HIGH/E2；decision `OPEN` + `PRODUCT_DECISION_REQUIRED` + `DEPENDENCY_ON_KB-30`；候选批次 Batch 15 |
| **N8 → KB-47** | **首次登记 canonical ID**；DEPLOYMENT/MEDIUM/E2×2；`GUARDED`；与 AR-8 的父子关系写明 |
| **KB-48**（新） | 登记：输入层两条直投入口停机窗口仍投递；`OPEN` `NEW_FINDING_OUT_OF_SCOPE` |
| **AR-8** | 缓解说明更正（"stop 已 cancel event task，风险低"被证伪；保留为父项，残余面未修） |
| §1.2 计数 | 36 → 39 行 |
| 其它 backlog | **未改**（严格遵守 §80"不要改其它backlog"） |

---

## 31. Keep As-Is

- **EventBus 架构**（优先级队列、INTERRUPT 插队、同优先级 FIFO、`drain()` 语义、两个计数/快照）：
  未改；只**新增**一个按类型分类的只读式移除方法。
- **FSM 架构与转换表**：未改（不新增状态/边）。
- **`_process_lock`**：未改（不移动取锁点、不加超时/shield）。
- **pending 架构**（单一入队权威、两个出队口、容量策略）：未改。
- **accepted 合同**：未改（仍是 Transport Accepted + 决策幂等；**不**升级成 durable 保证）。
- **no durable inbox**：未做。
- **no transaction framework**：未做（KB-46 明确挂到 KB-30，不偷上事务）。
- **no new scheduler / watchdog / polling / sweeper**：未做。
- **no frontend change**：未做。
- **KB-26 / KB-28 的内部状态**：不为 hygiene 修改（§11 的"不要为了 hygiene 改生命周期"）。

## 32. New Findings

| # | 发现 | 处置 |
|---|---|---|
| **KB-48** | `do_transcription` / `voice.commit` 停机窗口仍投递（4 条入口里 2 条无 `is_service_running` 判据） | 已登记（§11） |
| **KB-49** | `_cancel_desktop_calls()` 无回合归属判据：pre-ownership 的 canonical 收口会取消**上一回合**在途 desktop 调用（KB-29 的放大面；需 desktop 桥才可见） | 登记（随 KB-29） |
| **KB-50** | 陈旧 `InterruptData` 跨 epoch 存活：新 epoch 消费它 → `_handle_interrupt` → `bus.drain()`，可能清掉**新 epoch** 刚投进总线的事件（AR-8 残余面，当前无实例） | 登记（AR-8 父项下） |
| **T7-i（并入 KB-50）** | `TaskResultData` 跨重启**重复交付**（同 task_id 的宣告回合跑 2×）并借 `cancel_current()` 取消兄弟回合 | 登记（T5/T7 已接受 at-least-once；"重复副本杀兄弟回合"作为 KB-50 的交互面写明） |
| **KB-47-p** | `Agent.stop()` 清 `_pending_messages` 零归因（Challenger 反证；本族唯一**生产可达**的静默丢弃形态） | **本批已修**（同 cluster，RO1 的"可归因"半边）+ 守卫 `::test_stop_boundary_attributes_queued_pending_input` |
| **KB-46-e** | VAD/AudioBuffer 路径下 `should_suppress_vad_interrupt` 只抑制 INTERRUPT、不抑制 `on_transcription` 投递 —— 即"边想边说"会同时产生 INTERRUPT 与直投输入（KB-46 的常见触发形态） | 归入 KB-46 的触发面描述 |
| **N7-b** | 为**从未发过** `generation.started` 的 generationId 发 `generation.failed{GENERATION_CANCELLED, retryable:false}`，而 v2 文档把该 code 定义为"客户端 interrupt 的结果" | 归入 KB-46 |
| **KB-26-b** | `_current_task` 是"最近 spawn 的回合"而非"锁持有者"：`cancel_current()` 把它置 None 后，仍在跑的锁持有者变得不可见（测试 helper `_drain_turns`/`_wait_settled` 因此可能提前返回） | 登记为测试保真度项（KB-26 附注） |

## 33. Follow-up

保留（本批**未触碰**）：KB-30 / KB-32 / KB-33 / KB-39–KB-45 / KB-18–KB-21 / KB-12 / KB-13 /
AR-16（Ollama Provider Adapter）/ KB-15 / KB-16 / KB-17 / OBS-6/7/8 / KB-11 / GR-25 / N-15 /
KB-2 / KB-3 / KB-4 / KB-5 / KB-8 / KB-10 / KB-14 / F3 / F13 / F17 / DEP-* / SEC-* / PD-* /
AR-* / GR-* 以及其余 canonical backlog。

**本批新增排队**：KB-46（Batch 15 前，需 owner 裁会话历史合同）、KB-48（小批）、KB-29 的修法
（INTERNAL/LOW）、AR-8 残余面（触发式）。
