# Batch 15 — Claimed Input Conversation Truth Closure（KB-46）

> 批次范围：**KB-46 单条**。canonical scope 之外的一切发现只登记（§96）。
> 基线 HEAD：`9e9e25b9`。产出 commit：`e5e6e4b5`（production + PRE_FIX_RED）、
> `dbed9856`（变异暴露的两条守卫缺口）、`2f51b9c7`（真库旁证）、
> `3416f7ea`（不变量 + todo）。
> 不变量落纸：`docs/architecture/invariants.md` §4.8（**CT1–CT6**）。

---

## 1. 执行摘要

| 问题 | 答案 |
|---|---|
| KB-46 confirmed? | **YES** —— 当前 HEAD 上独立重新达到 **E2**（真 Agent + 真 `start()` + 真 `process_message` + 真 EventBus + 真停止按钮 + 确定性屏障） |
| classification | **CONFIRMED / USER / HIGH / GUARDED**（原 `PRODUCT_DECISION_REQUIRED` 已由本批的合同审计裁定并关闭） |
| 会话历史合同 | **Contract A（用户意图史）**；实现上是**混合语义**（user 在模型调用前写、assistant 在生成后写）。B/C 均被反证 |
| claim boundary | `_prepare_turn` 里 `self._generation_ctx = GenerationContext(...)` 的**一次无 await 赋值**（`current_turn_owns_input`）——本批复核后**沿用**，未更正 |
| record boundary | `_build_turn_prompt` 里 `session.add_message(UserMessage(...))`（**同步**）；本批把它收口为 `_record_user_input()` 唯一权威 |
| model-consumed boundary | `_start_turn_tracking` 之后 `_stream_llm_response` → `LLMAdapter.stream_think`（record 早于它） |
| production fix | **已做**，最小、非事务：唯一权威 + 一次性载荷 + 三条门禁 + 一处消费点（`_handle_interrupt` 零 await prologue） |
| KB-30 dependency | **无**（Batch 14 撤回的 `DEPENDENCY_ON_KB-30` 经本批独立复核**确认应撤回**；KB-30 保持独立，未被改动） |
| public API / wire | **零变化**（未新增 `message.cancelled` / `turn_status` / `history_status`） |
| persistence schema | **零 schema、零 migration** |
| real CI | **待 owner push**（本机无 GitHub 凭据；与 Batch 14 同一处境）。三个 job 的**全部命令**已在本地逐一执行等价验证，见 §18 |

**一句话**：一条用户输入跨过 active-turn claim 之后，Elysia 对它的 conversation-truth
承诺是"**它必须留在会话真相里**"（即使 assistant 一个字都没生成）；本批把这个承诺的
commit 边界从"`_build_turn_prompt` 里那一次 `add_message`"收紧到"**claim 之后的任何终态
路径都必须保证它已经/即将进历史**"，且不引入事务、不阻塞终态收口。

---

## 2. Phase 0 — Conversation Truth Contract（先定合同，再动代码）

§1 要求：**在合同定论前禁止修改 production code**。本批先做合同审计（真子代理 + 主
Agent 独立复核），四个候选：

| 候选 | 内容 | 判定 | 反证 |
|---|---|---|---|
| **A** | 用户意图史：用户实际提交且被 runtime 接管过的每一条 | **成立（声明意图 + 本批修法目标）** | — |
| B | 只记模型真正消费过的 | **反证** | record 早于 LLM 首轮；provider 从未被调用时输入已在历史里（`history ⊋ model-consumed`） |
| C | 只记完成到某阶段的回合 | **反证** | `_archive_partial_reply` 把被打断的**半截** assistant 回复照样归档（带 `metadata={'incomplete': True}`）；且存在"有 user 无 assistant"的取消回合 |
| **D** | 混合既有语义 | **实测形状**：唯一 record commit point 在 `build_messages` 的 await **之后**、`stream_think` **之前**；user 侧只有 `is_task_result` 一个豁免；assistant 侧成功走 `_archive_final_turn`、取消/异常走 `_archive_partial_reply` | — |

**结论**：**声明意图 = A，实测 = D，二者差集恰好就是 KB-46 窗口**。因此本批修的是
"让实测形状在**取消/失败**这一支上符合已声明的 A"，**不是**发明新的产品语义。

### 2.1 用户视角（§5）

用户发「记住我喜欢咖啡」→ ACK accepted → active claim 成立 → 用户按停止：
**产品期望是 A**（LLM 下一轮应当知道"咖啡"）。三条证据：

1. `loop.py` 的 record 点紧邻注释「此后用户至少能在历史里看到"我说过这句"」；
2. 前端 `chatStore.ts::addUserMessage` **乐观渲染** user 气泡，
   `useWebSocket.ts` 收到 `session` 帧时 `chat.restoreFromServer(...)` **整体替换**
   `messages.value` ⇒ 服务端 history 才是权威转写稿；缺一条 = 用户刷新后自己的消息消失；
3. 停止按钮在前端是 `connectionStore.send({ type: 'interrupt' })`——**无 payload、不回收
   user 气泡** ⇒ 语义是"停止 assistant 当前生成"，**不是**"撤销我刚发出的消息"。

---

## 3. Truth Layers（§6/§7）

| 层 | 载体 | 写入时机 | 本批是否改变 |
|---|---|---|---|
| **① session/runtime** | `SessionMemory._messages`（有界 deque） | record commit point（同步） | **是** —— 取消/失败路径现在也会补写 |
| **② archive/persistence** | `message_archive`（append-only） | 与 ① 同一步，但由 **fire-and-forget 任务**真实落库 | 随 ① 一并覆盖 |
| **③ future LLM context** | `PromptAssembler.build_messages` → `_session.recent_messages(8)` → `MessageBuilder` | 每回合读 ① | 随 ① 一并覆盖（CT-T3 用真实 `stream_think` 入参钉住） |

**三层**是否**设计上**必须一致？—— ①③ 必须一致（③ 就是 ① 的投影）。② 与 ① 的**写入
时机本就不同构**：`add_message` 同一步写内存 deque + 派发归档任务，而 `sessions`
**文档**只在**成功回合的 after_response** / 关机 `save_all()` / 逐出时全量 `$set`
upsert。⇒ **中途取消的回合：内存与 archive 有内容，`sessions` 文档没有**；而
`SessionMemory.load()` 只读 `sessions` 文档（不读 archive）。

这条**既有**差异未被本批改变，也**不在**本批保证范围内（§11/§56：本批解决
**runtime conversation truth**，不承诺 crash durability）。**已登记 KB-51**。
真库旁证 `test_rm1` 因此显式 `save()` 之后才经真 `SessionRepository.load()` 断言。

---

## 4. Turn Truth Lifecycle（§8）

```text
ACCEPTED      Agent.process_message() → bus.post(UserInputData) / _pending_messages.append
              → return True → WS 层回 accepted=true（v2 且幂等表落 ACCEPTED）
   ↓          owner：EventBus 队列 或 _pending_messages（AD1）
CLAIMED       _prepare_turn()：self._generation_ctx = GenerationContext(...)
              ★ 一次无 await 赋值；清空点 = process() 取锁**之前**（第一条语句）
   ↓          owner：本回合（current_turn_owns_input == True）
              ⚠️ 与 enter_thinking() 之间无 await ⇒ "ctx 非 None" ⟺ "FSM 已经/即将 THINKING"
RECORDED      _build_turn_prompt()：_record_user_input() → session.add_message(UserMessage)
              + _turn_message_persisted = True（同一同步段，无 await 可插入）
   ↓          owner：会话历史（内存 deque ⊕ 归档任务）
MODEL_CONSUMED _start_turn_tracking() → _stream_llm_response() → LLMAdapter.stream_think
   ↓
TERMINAL      成功：_finalize_turn → mark(completed) → turn_completed → finish_turn
              取消/失败：_handle_interrupt() / _handle_turn_exception() → mark(terminal)
```

### 4.1 窗口内的 await（§8 要求逐点标注）

claim（`_generation_ctx` 赋值）→ record（`session.add_message`）之间**只有 3 个 await
字面点**，且**是否真挂起是条件性的**：

| # | 位置 | 真挂起条件 | 本批的证据 |
|---|---|---|---|
| **A** | `await self.phases.enter_thinking()` | `_notify` 只有在**有挂起型 `on_change` 订阅者**时才让出；仓内生产订阅者只有 `Module/bench/bench.py` | 静态 + 子代理探针 |
| **B** | `await self._generation_registry.register(...)` | Mongo upsert；**未 connect 时 `generation_repo` 直接 return（不让出）**，已 connect 时是真网络往返 | CT-T1(W1) 用真屏障强制钉住 |
| **C** | `await self._prompt.build_messages(...)` | 12 段渲染里唯一真 IO 是 `sections/memory.py` 的 `await long_term.search()` | CT-T1(W2)/T2/T3/T8/T9 用真屏障强制钉住 |

> **更正**（审计子代理实测）：`loop.py` 里"本回合没有产出过**任何**用户可见帧"的说法
> **不准确** —— `enter_thinking()` 的 `bus.post(StateChangeData)` 是**同步**的，事件消费者
> 随后经 `output.notify_state` 发出的 `state: THINKING` 帧**在 record 之前**（实测
> `取消前已发出帧=['notify_state']`）。typing / `generation.started` / TTS **确实**在
> record 之后（`_start_turn_tracking`）。本批已就地更正该注释。

### 4.2 三个落点（W1/W2/W3）

| 落点 | 取消/失败落在 | 走哪条终态路径 |
|---|---|---|
| **W1** | A 或 B（`_prepare_turn` 之内） | `process()` 的 `except asyncio.CancelledError` → `_handle_prepare_cancelled()`（ctx 非 None 分支）→ `_handle_interrupt()` |
| **W2** | C（`_think_loop` → `_build_turn_prompt`） | `process()` 的 `except asyncio.CancelledError`（`loop.py` 的 `_think_loop` 臂）→ `_archive_partial_reply` → `_handle_interrupt()` |
| **W3** | 同窗口内的**普通异常** | `_handle_turn_exception()` → `finally: _handle_interrupt()` |

**W1 与 W2 是同一个 ownership 状态**（`_generation_ctx` 非 None + `_turn_message_persisted`
False），修前却给出**同一种**（都丢）会话真相但**不同的**归因（W1 有
`cancelled_pre_ownership`，**W2/W3 零归因**——Batch 12 的 AD2 只覆盖了前半段）。

---

## 5. Claim / Record / Model-Consumed Commit Points

### 5.1 Claim commit point（§9）——**确认沿用**

`self._generation_ctx = GenerationContext(...)`。复核：

- 全仓**唯一**写入点；清空点**唯一**（`process()` 取锁**之前**，与 `_admission_*` /
  `_turn_message_persisted` / `_claimed_input_record` 同一同步段）；
- 与 `enter_thinking()` 之间**没有 await** ⇒ 与 Batch 10/12 的既有等价性成立；
- ⚠️ **边界**：回合结束时**不清空** ⇒ "非 None ⟺ 本回合已接管"只在**在途回合**内成立，
  消费方必须自带 `.done()` 守卫。**已登记 KB-54**：`core/input/ws_handler.py:522` 是
  **第 6 个**读者且**没有** `.done()` 守卫（只靠连接归属兜底，今日良性）——
  这更正了 KB-26「恰好 5 处、每处都在 `.done()` 之内」的清单，但**不改** KB-26 的结论。

### 5.2 Record commit point（§10/§11）

**`SessionMemory.add_message`** —— 但必须区分三件事，只有前两件在同一步：

| 层 | 同一步完成？ | 机制 |
|---|---|---|
| 内存 deque append | **是**（同步） | `deque.append` |
| archive 任务**派发** | **是**（同步 `ensure_future`） | `_archive_with_limit`（Semaphore(8) 在协程体内） |
| archive **落库** | 否 | fire-and-forget；Mongo 离线时 `append` **fail-open**（warning + return） |
| `sessions` **文档** | **否** | 只有 `save()` / `save_all()` / 逐出 |

⇒ 本批的保证用词严格限定为 **runtime conversation truth**（§57），不写
"claimed input is guaranteed durable"。

### 5.3 Model-consumed boundary（§12）

`PromptAssembler.build_messages` 里 `history = self._session.recent_messages(8)`，
`user_content` 作为**独立参数**传入 `MessageBuilder.build_messages`，后者把两者拼成
两条**独立通道**（`message_builder.py`：history 在 `[1:-1]`、当前消息在 `[-1]`）。
**当前输入只经 `user_content` 通道进入**——这正是"提前 record 会双计"的机制。

---

## 6. W1/W2 复核与 Batch 14 候选补丁的证伪（§17/§18/§9）

Batch 14 的挑战者留下 `tmp/b14/challenger/n7_minfix.patch`（14 行，在 `_think_loop`
的 `except CancelledError` 里补 append）。本批复核：

| 项 | 结论 |
|---|---|
| W1 具体是什么 | 取消落在 `_prepare_turn`（A/B）。归 `_handle_prepare_cancelled`，**该方法签名只有 `self`，拿不到消息内容**（只有 `_interrupt_source` / `_admission_message_id` / `_admission_session_id`） |
| W2 具体是什么 | 取消落在 `_build_turn_prompt` 的 `build_messages`。归 `process()` 的 `except CancelledError`，**四个参数全在范围**（`user_message` / `content` / `message_id` / `setup.target`） |
| 同一 root？ | **是**（同一 ownership 状态），因此必须**共用同一 helper**，否则会长出两种会话真相 |
| 候选补丁为何不足 | **只覆盖 W2**。本批独立复现（`tmp/b15/main/probe_kb46_e2.py` + 主 Agent 的 CT-T1(W1) 守卫）：W1 落点修前**仍丢失**。⇒ 该补丁**未被采用**，只作设计证据 |
| 14 行 vs 完整修法 | 完整修法需要"在 claim 之前暂存消息载荷"——本批用 `process()` 入口的同步段暂存（那里额外可见 `is_task_result`，**不必**给 `_prepare_turn` 加形参），再共用 helper。实际规模 ≈ +150/-9 行（含注释与文档） |

---

## 7. 取消源矩阵（§26）

| Source | 生产入口 | 窗口（CLAIMED→RECORDED）可达？ | User? | 期望 history truth | 本批守卫 |
|---|---|---|---|---|---|
| **A. 停止按钮** | `ws_handlers/chat.py` → `InterruptData(source="websocket")` | **可达**（E2 实测） | ✅ | **保留** user 输入 | CT-T1(W1)/T1(W2) 用的就是它 |
| **B. 语音取消** | `voice_sessions.py` → `InterruptData(source="voice_cancel")` | 可达（同一 INTERRUPT 通路；窗口内相位是 THINKING，`cancel_session` 的 IDLE 门不拦） | ✅ | **保留** | 与 A 同通路（`cancel_current(source=…)` 贯通） |
| **C. 断连** | `ws_handler.py` → `InterruptData(source="disconnect")` | 可达（claim 后 `user_message_id` 已非空，连接归属成立） | ✅ | **保留** | 同上；generation 落 `interrupted` |
| **D. switch_llm** | `server.py` → `cancel_current()`（无 source） | 可达 | ✅（用户主动切模型） | **保留** | 同上（Batch 12 已保证 source 归零为 unknown，不沿用旧值） |
| **E. Agent.stop()** | `_lifecycle_tools.py` → `cancel_current()` | **可达**（CT-T12 实测：停机有界、且 STOPPED 后不开新回合） | ✅ | **保留** | **CT-T12** |
| **F. 内部失败** | `_handle_turn_exception`（W3） | **可达** | 视具体错误 | **保留** | **CT-T2** |
| **G. 普通异常** | 同 F | **可达** | 视具体错误 | **保留** | **CT-T2** / CT-T6b（claim 前则**不**保留） |
| **H. test-only cancel** | `task.cancel()` 直接调用 | 可达（Batch 11 的第二发形态） | ❌ | 与真实取消同 | CT-T11 / CT-T13 |

**没有为不同 source 写不同分支**：合同是"claim 之后一律保留 user 输入"，因此
`_record_claimed_input` **不按 source 分派**（只把 source 记进 disposition 日志）。

---

## 8. Pre-Fix E2 复现（§19–§25）

**探针**：`tmp/b15/main/probe_kb46_e2.py`（主 Agent 独立编写，5 例全绿，原始输出
`tmp/b15/main/e2_evidence.txt`）。骨架 = 真 `Agent` + 真 `start()` + 真
`process_message` + 真 EventBus；屏障 = `asyncio.Event` 包**生产代码本来就会挂起**的调用；
取消源 = 真 `InterruptData(source="websocket")` 经真消费者。

**修前原始输出（`9e9e25b9`）**：

```text
=== W1 窗口快照（claim 之后、record 之前） ===
  accepted = True   claimed = True   recorded = False   phase = 'thinking'
  pending = []      bus_pending = 0  session_user = ['（暖场）']
=== W1 取消后三层真相 ===
  session_user = ['（暖场）']   archive_user = ['（暖场）']
  in_history = False   in_archive = False
  phase_after = 'idle'  pending_after = []  llm_calls = 1
=== W2 窗口快照 ===
  accepted = True   claimed = True   recorded = False   phase = 'thinking'
=== W2 取消后三层真相 ===
  session_user = ['（暖场）']   archive_user = ['（暖场）']
  in_history = False   in_archive = False   llm_calls = 1
=== W2-异常后三层真相 ===
  session_user = ['（暖场）']   archive_user = ['（暖场）']
  in_history = False   in_archive = False   phase_after = 'idle'
=== 未来回合的真实 LLM 输入 ===
  m1_in_m2_prompt = False   m2_in_m2_prompt = True   total_llm_calls = 2
=== 正常路径恰一次 ===
  session_user = ['（暖场）','KB46-正常路径唯一性']
  count_in_history = 1   count_in_prompt = 1
```

⇒ **W1 / W2 / W3 三层皆丢；正常路径对照恰一次。** KB-46 在 HEAD 上成立（E2）。

**防假绿**：每条用例都先断言 `accepted=True / claimed=True / recorded=False`（并检查
`_turn_message_persisted is False`）**然后**才取消——否则测的根本不是这个窗口（§88）。

**一个测试侧陷阱（值得记录）**：W1 落点的取消发生在 `_prepare_turn` 之内，而
`loop._current_task` 要到 `loop.py:940`（`_prepare_turn` **返回之后**）才赋值 ⇒
"等 `_current_task` 落定"的既有 drain 助手会**早退**，读到"取消还没收口完"的中间态
（实测相位仍读成 `interrupted`）。守卫套件改用 `asyncio.all_tasks()` 按
`_turns.spawn(name="cognitive-…")` 赋的**真实任务名**取结构化句柄，并 await 真实任务
（否则 `sleep(0)` 忙等不会让 `handle_interrupt` 的 0.05s **定时器**前进）。

---

## 9. Root Cause

**不是**"`add_message` 写晚了"这一件事，而是：

> **conversation truth 的 commit boundary 与 active-turn ownership boundary 不重合，
> 且终态路径没有把两者对齐的机制。**

具体三层：

1. **边界不重合**：ownership 在 `_prepare_turn` 建立（早），truth 在
   `_build_turn_prompt` 落库（晚），中间有 3 个 await；终态路径（取消/异常）此前只负责
   "相位收口 + 可归因日志"，**不负责 truth**。
2. **归因不完整**：AD2 的 `cancelled_pre_ownership` 只打在 W1；W2/W3 连一句 disposition
   都没有 ⇒ 即使不想保留输入，"它被放弃了"这件事在观测面上也无法区分。
3. **前端把服务端 history 当权威转写稿**（乐观渲染 + 整体替换）⇒ 上述内部缺口直接
   变成用户可见的"我发的消息消失了"。

---

## 10. KB-30 Boundary（§14/§15/§97）

**本批没有改变**：rollback ❌ / transaction ❌ / assistant side effects ❌ /
tool side effects ❌ / generation multi-write atomicity ❌。**全部没有。**

理由（可执行地成立，不是声明）：

- 修法只调用 `SessionMemory.add_message`，它是**同步 `def`、函数体零 await**
  （`deque.append` + `ensure_future`）。因此它**不可能**被第二发取消打断，
  **不引入**新的挂起点 ⇒ Batch 11 的 CF1/CF2 不回归；
- **零新增 DB 往返**（正常路径与修前完全一致；取消路径多一次**内存** append + 一次既有的
  归档任务派发）⇒ 不把持久化 I/O 塞进 MUST_COMPLETE 临界区（§61）；
- append 与 generation 行**本来就没有事务耦合**（Batch 14 已反证）。本批**没有**去建立
  这种耦合，也**没有**引入跨写入的原子性要求。
- **KB-30 讲的是**「`_prepare_turn` 状态不回滚 / `_clock` 残留」——本批一行都没碰。

§16 的 STOP 条件**未触发**：正确修 KB-46 **不需要** `user message append + generation
registry + memory + assistant output` 原子化。

---

## 11. Fix（before / after / why minimal）

### Before

```python
# _build_turn_prompt（唯一写入点）
if not is_task_result:
    self._session.add_message(UserMessage(
        content=parts_from_dicts(user_content),
        message_id=user_message_id or new_message_id(),
        timestamp=_now_iso(),
    ))
    self._turn_message_persisted = True
# 终态路径：只有日志，没有 truth
```

### After

```python
# ① 唯一权威（normal 与 recovery 共用）
def _record_user_input(self, session, content, message_id) -> None:
    session.add_message(UserMessage(
        content=parts_from_dicts(content),
        message_id=message_id or new_message_id(),
        timestamp=_now_iso(),
    ))

# ② process() 取锁**之前**的同步段构造**协程局部**载荷（那里才知道 is_task_result）
record = (
    None if is_task_result
    else (content if content else [{"type": "text", "content": user_message}], message_id)
)
#    ⇒ 沿终态路径**显式下传**：_handle_prepare_cancelled(record) /
#      _handle_turn_exception(e, record) → _handle_interrupt(record=…) → 下面这个 helper。
#    （**不**放实例属性——Challenger W1 反证 + 实测 N2：两个 process() 体重叠时
#     载荷会跨回合串味。见 §17 的 M11。）

# ③ 唯一消费点：_handle_interrupt() 的零 await prologue（在 is_completely_idle 早退之前）
def _record_claimed_input(self, source=None, record=None) -> bool:
    if self._generation_ctx is None:      return False   # ①未 claim
    if self._turn_message_persisted:      return False   # ②已记录（幂等）
    if record is None:                    return False   # ③非用户输入
    target = getattr(self, "_session", None)             # 已 claim ⇒ 必为本回合 target
    if target is None:                    return False
    content, message_id = record
    if message_id is not None and _session_has_message_id(target, message_id):
        self._turn_message_persisted = True;  return False   # 既有身份 ⇒ 只对账
    self._record_user_input(target, content, message_id)
    self._turn_message_persisted = True
    logger.info("... disposition=cancelled_after_claim_persisted ...")
    return True
```

### Why minimal

- **一处消费点覆盖三个落点**（`_handle_interrupt` 的 3 个生产调用者 = W1 `:779` /
  W3 `:853` / W2 `:965`），不新增 per-path 分支；
- **没有 schema / wire / FSM / EventBus / lock / admission / pending 的任何改动**；
- **没有新增调度器、轮询、watchdog、shield、detached 清理任务**；
- 载荷是**协程局部量**，不新增实例状态 ⇒ "载荷属于哪个回合"在结构上不可能搞错；
- 全部注释写清"为什么在这里"与"明确不做什么"。

---

## 12. Record Authority / Idempotency（§35–§38）

| 项 | 结论 |
|---|---|
| 唯一权威 | `_record_user_input()`。全仓 user message 的 `session.add_message` 调用点**只有**这一个（其余 5 处是 `ToolResultMessage` / `AssistantMessage`，见 §13 的调用点清单） |
| 幂等判据 | **既有身份** `(session, message_id)`：`_session_has_message_id()` 扫描 ≤ `max_messages`(20) 条内存窗口。**不**新增平行布尔 truth source |

> ⚠️ **关于 `_session_has_message_id` 的判据（§90）**：**不用文本相等** ——
> 两条内容相同的用户消息是合法的（用户真的可以连发同一句），按文本判重会把真实的
> 第二条吞掉。窗口是内存 deque（≤20 条），无 DB（§100）。

| 门禁 | 作用 | 变异验证 |
|---|---|---|
| ① `_generation_ctx is not None` | claim 之前不得补写（CT5） | **M3** → CT-T6b 红 |
| ② `not _turn_message_persisted` | 已记录 ⇒ no-op（CT3/AD4） | **M2** → CT-T11 红 |
| ③ 载荷非 None | 合成系统唤醒不是用户输入（§43） | **M8** → CT-T7 红 |
| 协程局部载荷（非实例属性） | 两个 `process()` 体重叠时载荷不跨回合串味 | **M11** → CT-T15 红 |
| 零 await prologue | 第二发取消/等待锁的新回合打不进"已 claim 未补写"的缝 | **M9 / M10** → CT-T14 红 |
| 身份核对 | 防御纵深（**非唯一防线**） | **M7** → 存活（见 §16） |
| 异常吞成 warning | 绝不外抛、绝不阻塞收口（CT6） | **M5** → CT-T10 红 |

**身份矩阵**（实测）：v2 传 `message_id` 时 `_admission_message_id == ctx.user_message_id`；
v1/REST/STT 直投为 `None`（`ctx.user_message_id` 是**空串**，不是 None）。
`clientRequestId` **从不进 loop**（只在 v2 幂等表里）；`request_id` 每回合重分配、不持久化
⇒ 唯一可靠去重键是 `(session_id, message_id)`，**仅 v2 可用**；v1 由门禁②+一次性消费保证。

---

## 13. Prompt Duplication Proof（§13/§44/§80）

**Option A（把 record 提前）被实测证伪**：`PromptAssembler.build_messages` 把
`session.recent_messages(8)` 作为 `history` 通道、把当前 `user_content` 作为**独立通道**
传给 `MessageBuilder`，两者在 `messages[]` 里各占一条 ⇒ 提前 record 会让当前输入
**出现两次**：

```text
演示 1A：现状顺序（先 build_messages，后 add_message）
  [0] system   [1] user(上一轮)  [2] assistant(上一轮)  [3] user(当前输入)
  → 当前输入出现次数 = 1
演示 1B：假设把 append 提前
  [0] system   [1] user  [2] assistant  [3] user(历史侧)  [4] user(当前侧)
  → 当前输入出现次数 = 2
```

E2E 反事实探针（真 Agent + 假 LLM，monkeypatch 把记录提前）实测
`roles=['system','user','assistant','user','user']`、当前输入出现 **2** 次；
并实测**预算截断不去重**（`while len(messages) > 2: del messages[1]` 由**最旧**端删，
两份副本都在尾部时双双进 prompt），且非文本输入的两份副本**文案不同**
（历史侧过去时占位 vs. 当前侧现在时块列表）⇒ **字符串去重不可能兜住**。
故本批选 **(b) 保留原位置 + 终态 recovery**，并由 **CT-T4** 长期钉住
（变异 **M4** = Option A ⇒ CT-T4 红）。

**其他消费者**（审计子代理列出 23 项，逐项判定）：`_execute_tool_round` **不重建**
messages（只在同一列表原地追加）⇒ 工具轮不放大；TTS / consolidator / reflector /
describe_image / submit_task **不受影响**；`svc["_session_msg_count"]` 与
会话语义**不受影响**（record 位置未变）。

---

## 14. Cancellation / Failure / Stop / Shutdown Semantics

- **取消语义**：停止按钮 = "停止 assistant 当前生成"（§21）。已 claim 的 user 输入保留，
  **assistant 侧不伪造**（CT4：`roles[-1] == "user"`，`generations.status ∈ {cancelled, interrupted}`）。
- **失败语义**：普通异常走**同一个** recording boundary（§33 明确要求不只修
  `CancelledError`）。claim **之前**的失败（`StorageUnavailableError` / `CharacterBusyError`）
  **不**补写（CT5 / CT-T6b）。
- **停机语义**（§67/§83）：`Agent.stop()` 落在窗口内 ⇒ 停机**有界**（CT-T12 用
  `wait_for` 做死锁探测）、`STOPPED` 之后**不得**再开回合/调用 LLM。真库旁证确认收口期
  落的补写**会**被后续 `save_all()` 落进 `sessions`（`save_all` 在 `drain_all_archives`
  **之前**）。
- **`is_task_result`**（§43）：合成后台任务唤醒**不**进 user history。CT-T7 按**条数**
  钉住（只查文案杀不死"误记一条空 user 消息"的变异——唤醒回合的 `user_message` 是空串）。
  而 `_handle_system_tick` 的主动搭话（`is_task_result=False`）**保持修前行为**（会被记录）。

---

## 15. History Ordering（§23/§49/§50/§51/§58/§71）

- 被取消回合的 shape：`user`（无 assistant）—— CT-T1 断言 `roles[-1] == "user"`；
- 失败（model 前）回合的 shape：同上 —— CT-T2；
- 顺序：M1（窗口内取消）→ M2 → assistant(M2)，**不得** M2 在 M1 前 —— CT-T8；
- 正常成功回合 shape 不变：`user, assistant` —— CT-T4；
- **不擅自加** `[cancelled]` 文本消息；**不**新增 wire 字段（§53）。

---

## 16. Regression Tests 分类（§26/§65）

`core/agent/tests/test_claimed_input_conversation_truth.py`（**17 例**）：

| 用例 | 类别 | 修前 |
|---|---|---|
| `test_ct_t1_w1_cancel_in_prepare_after_claim_keeps_input_in_history` | **PRE_FIX_RED** | 🔴 |
| `test_ct_t1_w2_cancel_in_prompt_build_after_claim_keeps_input_in_history` | **PRE_FIX_RED** | 🔴 |
| `test_ct_t2_ordinary_exception_after_claim_keeps_input_in_history` | **PRE_FIX_RED** | 🔴 |
| `test_ct_t3_next_turn_llm_context_contains_cancelled_claimed_input` | **PRE_FIX_RED** | 🔴 |
| `test_ct_t8_cancelled_then_normal_keeps_history_order` | **PRE_FIX_RED** | 🔴 |
| `test_ct_t9_multimodal_payload_structure_is_preserved` | **PRE_FIX_RED** | 🔴 |
| `test_ct_t4_normal_turn_records_input_exactly_once` | ALREADY_GREEN_GUARD（prompt 双计） | 🟢 |
| `test_ct_t5_cancel_after_record_does_not_duplicate` | ALREADY_GREEN_GUARD（AD4 边界另一侧） | 🟢 |
| `test_ct_t6_pre_claim_cancel_creates_no_phantom_history` | PRODUCT_CONTRACT_GUARD（AD 边界） | 🟢 |
| `test_ct_t6b_pre_claim_exception_creates_no_phantom_history` | PRODUCT_CONTRACT_GUARD（**变异 M3 只有它能杀**） | 🟢 |
| `test_ct_t7_synthetic_task_result_turn_is_not_recorded_as_user_history` | PRODUCT_CONTRACT_GUARD（按条数） | 🟢 |
| `test_ct_t10_session_write_failure_does_not_block_terminal_finalization` | FALSIFICATION_GUARD（CF） | 🟢 |
| `test_ct_t11_record_recovery_is_idempotent_across_repeated_finalization` | FALSIFICATION_GUARD（幂等） | 🟢 |
| `test_ct_t12_shutdown_in_window_stays_bounded_and_starts_no_new_turn` | FALSIFICATION_GUARD（停机 + 真相层恰一条） | 🟢 |
| `test_ct_t13_exception_finalization_cancelled_does_not_double_record` | FALSIFICATION_GUARD（W3→W1 叠加） | 🟢 |
| `test_ct_t14_direct_injection_preemption_still_records_claimed_input` | **PRE_FIX_RED**（直投输入抢占 = 最宽的生产触发面）+ 唯一能杀 **M9/M10** 的守卫 | 🔴 |
| `test_ct_t15_payload_never_leaks_across_overlapping_process_bodies` | FALSIFICATION_GUARD（两个 `process()` 体重叠 ⇒ 载荷不得跨回合串味）+ 唯一能杀 **M11** 的守卫 | 🟢 |

**PRE_FIX_RED 证据**：修前 `6 failed, 4 passed`（当时套件 10 例；CT-T6b / CT-T14 是随后补入的，
它们的鉴别力由**变异** M3 / M9 给出，见 §17）；修后 **17 passed**。

---

## 17. 变异证据（§87）

驱动：`tmp/b15/mutate.py`（隔离 worktree `/tmp/b15-mut`，**每段替换强制恰好命中一次**，
否则报"变异未装上"并退出 2——防"变异根本没生效"的假绿）。

| 变异 | 内容 | 期望 | 实测 |
|---|---|---|---|
| **M1** | 移除 recovery 调用点（恢复旧空洞） | CT-T1(w1/w2)/T2/T3/T8/T9 红 | **KILLED**（加固后 **10 条**红，含 CT-T12/T13/T14/T15） |
| **M2** | 取消侧去掉门禁② + 一次性消费 | CT-T11 红 | **KILLED** |
| **M3** | 去掉门禁①（允许 pre-claim 补写） | CT-T6b 红 | **KILLED** |
| **M4** | **Option A**：normal path 记录移到 `build_messages` 之前 | CT-T4 红（prompt 双计） | **KILLED** |
| **M5** | terminal 收口链上等无界持久化 | CT-T10 红 | **KILLED** |
| **M6** | 只写旁路会话，不进目标会话 | CT-T1(w1)/T3 红 | **KILLED** |
| **M7** | 去掉身份幂等（保留门禁②+一次性消费） | 预期**存活** | **SURVIVED（符合预期）** |
| **M8** | 去掉 `is_task_result` 排除 | CT-T7 红 | **KILLED** |
| **M9** | 把 recovery 从**零 await prologue** 挪到收口链末尾（`finally` 里 `phases.handle_interrupt()` 之后） | CT-T14 红 | **KILLED**（`1 failed, 15 passed`，**只有 CT-T14 抓到**） |
| **M10** | 在 recovery **之前**插一个 `await asyncio.sleep(0)`（保留位置，破坏「零 await」） | CT-T14 红 | **KILLED**（`1 failed, 16 passed`，**只有 CT-T14 抓到**）—— 关闭 Challenger 的 finding #3（它跑的是加 CT-T14 **之前**的套件，当时 15/15 全绿） |
| **M11** | 把载荷从**协程局部量**改回**实例属性**（重新引入跨回合泄漏） | CT-T15 红 | **KILLED**（`1 failed, 16 passed`，**只有 CT-T15 抓到**）—— 关闭 Challenger 的 finding #1 |

> **M7 的诚实解读**：身份核对 `_session_has_message_id()` **不是**唯一的幂等防线——
> 门禁②（`_turn_message_persisted`）+ 一次性消费已经足够。身份核对是**防御纵深**
> （v2 有稳定 ID 时做权威对账）。本批**不**把它说成"必需"，也不为它编造一个能杀它的
> 守卫；它的价值在于"判据落在既有身份而非平行布尔"（§38 的**首选**，不是**唯一**）。

> **M9 的意义**：它证明「补写放在**零 await prologue**」是**承重设计**而非风格偏好 ——
> 一旦挪到收口链末尾，被**直投输入抢占**的回合（抢占方的新回合会在取锁前清空
> `_generation_ctx`，Batch 12 的有意设计）就会让门禁①把这条**已 claim** 的输入判成
> 「未 claim」而静默丢弃。CT-T14 是唯一能抓到它的守卫（CT-T11 / CT-T13 用的是第二发
> 取消，没有新回合来清 ctx）。

**随机调度扰动（§104，辅助证据）**：`tmp/b15/main/stress_kb46.py`，**600 轮**在
同一个真 Agent 上按随机场景序列推进（`w2_cancel` 122 / `w2_exc` 132 /
`preclaim_cancel` 115 / **`w2_preempt` 115** / `normal` 116；`seed=20260919`）。
每轮只断言**该轮**的不变量（session 是有界 deque，会逐出旧消息，末尾统一断言无意义）：
claimed 场景该 token **恰一条**、pre-claim 场景**0 条**、每轮结束 `is_completely_idle`
且 pending 为空、正常回合 LLM **恰一次**调用，另做 3 次真实 prompt 计数（恰一次）。

```text
rounds=600 stats={'w2_cancel': 122, 'w2_exc': 132, 'preclaim_cancel': 115,
                  'w2_preempt': 115, 'normal': 116}
prompt_checks=3 llm_calls=232 phase=idle final_user_msgs=14
1 passed in 34.60s
```

其中 **`w2_preempt`（直投输入抢占）是 M9 的临界路径**（抢占方的新回合会在取锁**之前**
清空 `_generation_ctx`）—— 115 轮全部无丢失、无重复、无 phantom、无 active-state orphan。

**两个由变异暴露、当场补齐的守卫缺口**（`dbed9856`）：

1. **CT-T6b**：pre-claim **异常**路径与取消路径控制流不同 —— 取消走
   `_handle_prepare_cancelled()` 的 ctx-None 分支，**根本不进** `_handle_interrupt()`；
   异常走 `_handle_turn_exception()`，它的 `finally:` **一定**会进 ⇒ 在那里门禁①
   是**唯一**防线。生产形态 = P0-3 `StorageUnavailableError` fail-closed
   （与 AD8 同形，但 AD8 只断言 M2 被处理，**不看** M1 是否被误写）。
2. **CT-T7 按条数**：唤醒回合的 `user_message` 是**空串** ⇒ "误记一条空 user 消息"
   不会被文案断言杀死。

---

## 18. 测试结果（§102/§107）

| 档位 / 门禁 | 命令 | 结果 |
|---|---|---|
| lint | `make lint` | ✅ All checks passed |
| pyright | `make pyright` | ✅ 0 errors, 0 warnings |
| 架构边界 | `make check-arch` | ✅ R1/R2/R3/R4/R6 |
| 契约可达性 | `make check-contract` | ✅ |
| 超时预算 | `make check-timeouts` | ✅ R1–R4 |
| Debug 凭据守卫 | `make check-debug-auth` | ✅ |
| 密钥扫描 | `make check-secrets` | ✅ |
| 测试集合代数 | `make check-test-sets` | ✅ `\|Quick ∪ remainder\| = 6257 == \|Normal\|`，交集 0，全量 6326 |
| compose 安全 | `make check-compose` | ✅ |
| env 锁 | `make check-env-lock` | ✅ |
| CI 信任边界 | `make check-ci-isolation` | ✅ 3 个 workflow |
| 契约漂移 | `make check-types` | ✅（含 17 个 golden frames + 错误码单源） |
| targeted（新守卫） | `pytest core/agent/tests/test_claimed_input_conversation_truth.py` | ✅ **17 passed** |
| Batch 12/13/14 套件 | `test_admission_input_delivery` + `test_pending_input_ordering` + 认知套件 | ✅ |
| 取消安全收口（Batch 11） | `core/cognitive/tests/test_cancellation_safe_finalization.py` + `core/agent/tests/test_turn_finalization_cancellation_safety.py` | ✅ |
| quick | `make test-quick` | ✅ **6253 passed**, 2 skipped, 2 xfailed |
| normal | `make test-normal` | ✅ **6253 passed**, 2 skipped, 2 xfailed（remainder = 0，符合预期） |
| full | `make test-full` | ✅ **6285 passed**, 39 skipped, 2 xfailed |
| real mongo | `make test-real-mongo` | ✅ **27 passed**（24 既有 + 3 新增） |
| frontend | `cd frontend && npm run build` | ✅ built in 836ms（`static/` 为 gitignore） |
| **real CI** | owner push 后由 GitHub Actions 跑 | **待 owner push**（本机无 GitHub 凭据，与 Batch 14 同一处境）。CI 的三个 job（Backend / Real MongoDB / Frontend）的**全部命令**已在本地逐条等价执行 |

**真库旁证**（`tests/real_mongo/test_real_claimed_input_truth.py`，3 例）：

- **RM1**：真实 `loop.process()` + 真实 claim 路径（屏障钉在 `build_messages`），先断言
  `current_turn_owns_input is True` 且 `_turn_message_persisted is False` 再取消 ⇒
  内存窗口 / 真库 `message_archive` / `save()` 后经真 `SessionRepository.load()` 读回的
  `sessions` 文档 **三处各恰一条**；再跑一次 canonical 终态 owner，两处仍各恰一条。
- **RM2**：正常回合三处各恰一条（修法不得双写）。
- **RM3**：真库读回的消息重建会话 → 真实 `MessageBuilder.build_messages` ⇒ 被取消的
  输入在下一回合 prompt 里**恰一次**。

**诚实声明**：本层**不**启动真 Agent（根 conftest 的真实库防线按 marker 放行，
`real_mongo` 不在名单里；绕开只能 `no_storage_guard`，而那会让 Agent 其余全部仓储连上
开发者**生产库**）。因此沿用既有 `test_real_finalization_cancellation.py` 的形状：
真 `CognitiveLoop` + 经 `real_mongo_repo` 工厂核验过落点的真仓储。

---

## 19. Invariant Mapping（§94）

| ID | 不变量 | 守卫 |
|---|---|---|
| **CT1** | 被 active turn claim 的 user input，不得因受支持的取消/失败从 runtime conversation truth 静默消失 | CT-T1(W1/W2)、CT-T2、RM1 |
| **CT2** | claim / record / model-consume / terminal 各有独立 owner；进历史只有一个 canonical 权威；**载荷与 claim 同源**（协程局部量，不跨回合） | CT-T3（真实 `stream_think` 入参）、**CT-T15**、RM1/RM2 |
| **CT3** | 补写幂等：同一条逻辑输入在 history/archive/model context 里恰一次 | CT-T11、CT-T5、CT-T13、CT-T4 |
| **CT4** | 保留 user truth 不得伪造 assistant 产出或"回合已完成" | CT-T1（`roles[-1]`、`generations.status`）、CT-T8 |
| **CT5** | claim **之前**取消/失败的输入不得被提升进会话历史 | CT-T6、CT-T6b |
| **CT6** | truth 保护不得阻塞取消安全终态收口与停机（**补写必须在收口链的首个 await 之前**、同步零 await、绝不外抛） | CT-T10、**CT-T12**、CT-T11、**CT-T14**（M9/M10 的唯一杀手） |

落纸：`docs/architecture/invariants.md` §4.8。

---

## 20. Public API / Schema / Persistence / Performance（§53/§54/§98/§99/§100）

| 面 | 影响 |
|---|---|
| wire / public API | **零变化**。未新增 `message.cancelled` / `turn_status` / `history_status`；未新增 WS/REST/Debug 模型；`make check-types` 无漂移 |
| persistence schema | **零 schema、零 migration**。未新增集合/索引/字段 |
| 正常路径性能 | **零额外 DB 请求**。只多一次同步赋值 `self._claimed_input_record = None`；`_claimed_input_record` 的暂存是纯内存（同一份 `content` 列表，本来就会被 `add_message` 消费） |
| 取消路径性能 | 一次内存 append + 一次**既有**归档任务派发（`ensure_future` 到既有 Semaphore(8) 池）；**无** read-before-write、**无** 同步 DB |
| 内存驻留 | 载荷在"normal 记录"或"recovery 消费"时立即清空；未被消费时最多存活到下一回合入口（内容与已入历史的同一条重复，无净增） |

---

## 21. Keep As-Is（§38/§97）

- **EventBus**：未改（未新增事件类型、未改优先级、未改 drain 语义）。
- **FSM**：未改（未新增状态/转换边；`handle_interrupt` 幂等性与 CF1/CF2 临界区未动）。
- **`_process_lock`**：未改（未新增锁、未改获取顺序）。
- **admission contract（AD1–AD8）**：未改；本批的 recovery **反而**受 AD 边界约束（门禁①）。
- **pending ordering（PO1–PO6 / PO-L）**：未改；CT-T8 钉住 M1→M2 顺序。
- **no durable inbox / no transaction framework / no rollback framework**：**未做**。
- **no frontend schema change**：未改前端任何文件；`npm run build` 通过。
- **KB-30 仍独立**：未改其状态，只在 §4.8 与本文档交叉引用。

---

## 22. New Findings（只登记，不顺手修，§96）

| ID | 内容 | 归属 |
|---|---|---|
| **KB-51** | 三层 truth 写入时序**不同构**：取消回合的内存/archive 有内容而 `sessions` 文档没有（`load()` 只读后者）⇒ 硬杀后该回合从 LLM 上下文消失。本批保证范围明确不含 crash durability | 与 KB-46 不同 root；本批只登记 |
| **KB-52** | `is_task_result` 豁免**只在 user 侧** ⇒ 合成唤醒回合 `roles=[USER, ASSISTANT, ASSISTANT]`；与 F13（主动搭话**会**被当成用户消息）方向相反 | 与 F13 同族，小批 |
| **KB-53** | wire/UI 没有"已取消/不完整"表示（回放项丢 `metadata`；契约 `completeness` 恒 `"complete"`）⇒ 刷新后半截回复与完整回复无异 | 需**产品/契约决策** |
| **KB-54** | 更正 **KB-26** 的读者清单：`_current_task` 陈旧读者 5 处 → **6 处**（`core/input/ws_handler.py:522` 无 `.done()` 守卫，靠连接归属兜底，今日良性）。**不改** KB-26 的 KEEP_AS_IS 结论 | cross-ref KB-26 |
| **KB-55** | `GenerationContext.__post_init__` 构造期抛错 ⇒ ctx 仍为 None ⇒ 已 ACK 输入**零记录零归因**（可达性未验证） | 待验证 |
| **KB-56** | 前端 `loadMoreMessages` 打全库 `/api/messages`，游标取自回放项的 `_id` 而 `messages_for_client` 不产出 `_id` ⇒ 首次"加载更多"可能跨会话（**纯代码推断，未做运行期验证**） | 待验证（Batch 8 续） |

**文档漂移更正**（就地修，不登记）：`loop.py` 里"本回合没有产出过任何用户可见帧"
被实测证伪（`state: THINKING` 在 record 之前就发）—— 已在该注释处更正并标注实测证据。

---

---

## 24. Challenger Findings（§74–§86）

独立对抗审计（Challenger 子代理）：`tmp/b15/challenger/report.md`（含 68 例自建对抗用例
`final_run.log`、独立变异驱动器 `mutate.py`，跑在自建 worktree `/tmp/b15-challenger-wt`）。
**主 Agent 独立复现了它报告的全部 FIX finding**（不采信结论，只看字面输出）。

### 24.1 被打破的（已在本批修复）

| # | Challenger 主张 | 主 Agent 独立复现 | 处置 |
|---|---|---|---|
| **W1** | `_claimed_input_record` 是**实例属性**且 `process()` 同步前缀在**锁外** ⇒ 载荷与 claim 可来自不同回合 | ✅ `tmp/b15/main/probe_n2_leak.py`：`claimed_by_b=True payload=([{'type':'text','content':'N2-A-正文'}], None) A_count=2 B_count=0` | **已修**（`c961595d`）：载荷改为 `process()` 的**协程局部量**并沿终态路径显式下传 ⇒ 同探针 `A_count=1 B_count=1`；新增守卫 **CT-T15**（变异 **M11** 唯一杀手） |
| **#3** | "零 await prologue"这半边**无人守**（它跑的是加 CT-T14 **之前**的套件：插一个 `await` 后 15/15 全绿） | 复跑变异 **M10**（recovery 前插 `await asyncio.sleep(0)`）：当前套件 **1 failed（CT-T14）/ 16 passed** | **已闭合**：CT-T14 是 M9/M10 的唯一杀手。Challenger 的观察对**当时**的套件成立，对当前 HEAD 不成立 |
| **假绿 CT-T12** | 功能整个删掉仍绿；含恒真式断言 `pending_messages == [] or is not None`（该属性恒返回 list），且从不断言真相层 | ✅ 复跑变异 **M1**：加固前 CT-T12 绿 | **已修**：删恒真式，补"停机窗口内被取消的已 claim 输入**恰一条**"的真相层断言 ⇒ **M1 现在 10 条红**（含 CT-T12） |
| **假绿 CT-T11** | 单独删门禁②、一次性消费、recovery 后的 `persisted=True` 都不红（只有"门禁②+一次性消费"同删才红） | ✅ 载荷改为下传后，只调 `_handle_interrupt()` 是**无载荷 no-op** ⇒ 原写法确实退化成恒真 | **已修**：改为在窗口内以 `process()` 的同一构造形状给 helper 喂**两次**（True→False） ⇒ 变异 **M2** 现在被 CT-T11 + CT-T5 杀 |
| **假绿 CT-T8** | `roles[texts.index(m1)+1] != "assistant" or …` 用 user 文本下标索引 `roles`，`or` 右侧实测恒真 | ✅ 静态复核确认 | **已修**：按 `session.messages` 的**真实**下标断言"M1 之后不得出现 assistant" |
| **注释不成立** | "normal 与 recovery 共享同一份一次性载荷，谁先写谁消费，另一个必然在门禁②上短路"——normal path **不查**门禁② | ✅ 静态复核确认（normal path 无条件写） | **已修注释**：改为准确表述（门禁② 是 **recovery 侧**的短路判据；两条权威在**生产**上互斥，因为 recovery 只在终态路径运行） |

### 24.2 登记不修（超出本批 scope）

| # | 主张 | 判定 |
|---|---|---|
| **#2 / L** | 同一 `message_id` 跨回合重放会得到**两条**历史（上游 `ws_v2` 的 TAKEOVER 分支复用 `entry.user_message_id`；触发前提 = `_prepare_turn` 的 `register` 失败被 `except Exception: logger.debug` 吞掉 ⇒ 幂等表 stale） | **不是本批引入**：修前 normal path 同样无条件 append（同 id 两次准入 ⇒ 两条）。本批的 recovery 只在**同回合内**按身份去重。跨回合去重属准入/幂等语义，**登记 KB-57**（需产品决策：合法重发 vs 重放） |
| **E2/E2b** | 在**回合之外**手动先跑 recovery 再放行 normal path ⇒ 双写（v2 身份检查也拦不住） | 非生产可达（recovery 只在终态路径运行；normal 与 recovery 在生产上互斥）。**已由注释更正覆盖**，不新增守卫 |

### 24.3 未能打破的（逐条实测，均有字面输出）

W1/W2 取消（v1 与 v2 身份均保留预分配 id）、record 之后取消/异常、普通异常、
W3→W1 叠加恰一次、重复终态调用、phantom 防护（G1–G3）、prompt 唯一性（`count=1`）、
M1→M2→M3 顺序与既有 PO 套件 25 passed、停机**有界**且不复活且**确实落库**、
`StorageUnavailableError` / 归档失败均不阻终态、多模态 image/file 结构保留、
`add_message` 零 await（AST + 运行期零切换）、`self._session` 全类唯一赋值点
（`loop.py:593`，claim 在 `:680`）+ 归属实验（只写目标会话与 archive）、
`_handle_interrupt` 生产调用点恰 3、`is_task_result` 排除完整、
既有套件未被削弱（`git show --name-status`：删掉的**逻辑行**只有原 5 行 `add_message`）。

### 24.4 一处结论修正（Challenger 对，我们低估）

**"不承诺 durability"是低估**：停机路径下补写**确实**会被 `save_all()` 落库
（`[I3] sessions_hits=2 archive_hits=1`，与正常回合的对照同值）。本批"不承诺
durability"的准确含义是**进程硬杀**（`accepted ≠ durable`），不是"取消路径永不落
`sessions`"。已据此更正 `docs/architecture/invariants.md` §4.8 的边界措辞。

## 23. 后续（§40）

保留不动：**KB-30**、KB-29、KB-32/33、KB-39–45、KB-48–50、KB-51–56、KB-18–21、KB-12、
KB-13、AR-16（Ollama Provider Adapter）、KB-15/16/17、OBS-6/7/8、KB-11、GR-25、N-15，
以及其余 canonical backlog（见 `todo.md` §1）。
