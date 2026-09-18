# Batch 9 — Speaking State Recovery（KB-6）执行记录

> 范围：canonical backlog 仅 **KB-6**。本批**不**处理 KB-12 / KB-13 / Ollama Provider Adapter /
> AR-16 / WAITING_APPROVAL / reminder 持久化 / 通用分页 / 前端语音 UX。
> 结论：**CONFIRMED_USER_REACHABLE**（E2）→ 最小修复 + PRE_FIX_RED 守卫 + 不变量落纸。
> 生产 diff：`core/adapter/tts_adapter.py`（1 个新 helper + 取消点接线）。

---

## 1. Executive Summary

| 问题 | 回答 |
|---|---|
| KB-6 confirmed? | **是**（且是**第三个**独立挂起源；S2-G3 已修的两条不覆盖它） |
| classification | `CONFIRMED_USER_REACHABLE` → 修复并守卫（todo: **GUARDED**） |
| evidence level | **E2**（真实 `CognitiveLoop.process` + 真实 `AgentFSM` + 真实 `TTSStreamManager`，确定性调度、无 sleep） |
| production fix | 有：取消点也负责放行后继门控（`TTSStreamManager._cancel_task`） |
| public API / wire | **无变化**（无新字段、无新枚举、无 schema 变更） |
| persistence | **无变化**（无新集合/字段/迁移；FSM 运行期状态本就不持久化） |
| real CI | 见 §23 |

一句话根因：**`stream_sentence()` 创建的任务若在"首个 event-loop step 之前"被取消，
协程体一行都不会执行**——S2-G3 把放行写在"早退分支"和 `finally` 里，而这两处都在**协程体内部**，
于是后继门控永不 `set`，`wait_all()`（无超时）永不返回，`finish_turn` 卡住，**FSM 永久停在 SPEAKING**。

---

## 2. Original Backlog Evidence

`todo.md` §1.2 的 canonical 描述（Batch 4 triage 建立）：

> **KB-6** | TTS 密集短句（5+）时 `wait_all()` 无超时 ⇒ FSM 卡 `SPEAKING`
> （已用 read timeout 120s→30s 缓解，根因未复现） | USER | MEDIUM | **E1+E0** | INSUFFICIENT_EVIDENCE

在 HEAD 上与原文核对：

- 「`wait_all()` 无超时」—— **仍成立**（`core/adapter/tts_adapter.py::wait_all` 至今无 `wait_for`）。
- 「read timeout 120s→30s 缓解」—— 确有其事（`7346922d`，2026-07-13，`Module/tts/minimax_tts.py`
  `_TTS_TIMEOUT`）。它只把**单次合成的上界**从 120s 压到 30s，与"门控没人放行"无关 ⇒ 不是根因修复。
- 「根因未复现」—— 本批**复现成功**，但**不是**"密集短句"本身：句子数量只影响窗口出现频率，
  真正的条件是"某句的任务被取消在首个 step 之前"。S2-G3（`9f323f07`）当时修掉了两条**门控挂起源**，
  并把残余如实记为「`wait_all()` 仍无超时预算」——本批补上的正是它漏掉的第三档：**任务根本没启动**。

---

## 3. Speaking Contract（SPEAKING 到底表示什么）

**当前实现的语义 = "Agent 正在向用户输出本轮的 response"，不是"扬声器正在出声"。**判据全部来自代码：

| 证据 | 含义 |
|---|---|
| `core/cognitive/streaming.py::_handle_sentence_event`：**首句到达即** `await self.phases.enter_speaking()`，**且与 TTS 是否可用无关**（`tts_active` 只决定要不要 `stream_sentence`） | 入 SPEAKING 的触发是"有可输出的句子"，不是"有音频" |
| TTS 关闭（`set_enabled(False)`）或运行期后端为 `None` 时，实测仍进入 SPEAKING 并正常退出（本批 §14/§15/§16 用例） | 文本输出也算 speaking |
| `core/status/protocol.py::AgentStatus.is_speaking` docstring：「正在输出语音/文本给用户？」 | 契约层自陈 |
| `_finalize_turn` → `_transition_idle` → `finish_turn` 的退出点由 **`wait_all()`（服务端 TTS 任务）** 决定，而不是客户端播放回调 | 系统**不追踪客户端播放**，所以 SPEAKING 也不可能表示"扬声器还在出声" |

**因此本批不重新定义状态**，只保证这个语义下不 stranded。§57 的提醒（不要假装知道音频何时播完）
在这里落实为：退出权威是**服务端输出工作的完成**，客户端播放结束不参与判定，也不该参与。

---

## 4. State Lifecycle Map

```
USER_INPUT
   ↓   CognitiveLoop.process() 取 _process_lock（accepts_new_turns 门禁）
THINKING                      ← phases.enter_thinking()
   ↓   (tools) EXECUTING ⇄ THINKING（tool_phase）
response ready（流式；分句由**后续输入**驱动，最后一句靠 flush）
   ↓   _handle_sentence_event(首句) → phases.enter_speaking()          ← SPEAKING 入口（唯一）
SPEAKING                      ← AgentFSM.transition(SPEAKING)
   ↓   TTSStreamManager.stream_sentence() 每句一个 Task（async）
   ↓   Task: synthesize → gate(seq).wait() → 文本/音频转发 audio_start/chunk/end
   ↓   Task finally: _release_gate(seq+1)（仅同 epoch）
queue drained / utterance finished
   ↓   _finalize_turn → _transition_idle → phases.finish_turn(has_background_tasks)
   ↓       await tts_mgr.wait_all()      ← **真正的 completion signal 所在**
   ↓       await tts_mgr.flush_emojis()
   ↓
IDLE 或 IDLE_BACKGROUND_TASK（有活跃后台任务时）
```

异常/打断路径（同一 owner 的另一半）：

```
任意点异常 → process 的 except Exception → _archive_partial_reply → _handle_turn_exception
                                                                    → _handle_interrupt()
任意点 CancelledError → process 的 except CancelledError → _handle_interrupt()
_handle_interrupt: tts_mgr.cancel_all() → phases.handle_interrupt()
                   （SPEAKING → INTERRUPTED →(sleep 0.05)→ IDLE，走 FSM 合法边）
停机: Agent.stop() → _stop_cognitive_and_tts（cancel_current + cancel_all）
                → _drain_cognitive_turns（等被取消的回合把收尾写完）→ 各资源逐步关闭
```

**关键事实**：SPEAKING 的**唯一入口**是 `_handle_sentence_event` 的首句分支；
**主出口**只有 `finish_turn`（正常收尾）与 `handle_interrupt`（异常/打断，经 INTERRUPTED 中转）。
两条出口都经过 `FSMBackedPhaseController`，没有第三处写状态。

---

## 5. Authority Map（§6 / §51 的正式回答）

| Concern | Current authority |
|---|---|
| enter SPEAKING | `CognitiveStreaming._handle_sentence_event`（首句）→ `FSMBackedPhaseController.enter_speaking()` → `AgentFSM.transition` |
| current utterance / epoch | `TTSStreamManager._turn_epoch`（`reset_for_turn()` 抬一）+ `_seq`/`_total`（句序号与全局 playback id） |
| TTS 合成完成 | `TTSStreamManager` 的 per-sentence Task（`_download_and_forward`）自身；失败在任务内被捕获并记 warning |
| audio 转发完成 | 同上（`audio_end` 之后）；客户端**播放**完成**不**被追踪（有意，见 §3） |
| cancellation | `TTSStreamManager.cancel_all()` / `cancel_stream(sid)`；回合级由 `CognitiveLoop._handle_interrupt` 调用 |
| interrupt | `EventBus`(INTERRUPT) → `Agent._handle_interrupt` → `CognitiveLoop.cancel_current()` → 回合任务的 `CancelledError` 分支 |
| exit SPEAKING | **`FSMBackedPhaseController.finish_turn()`（正常）** 与 **`handle_interrupt()`（异常/打断）**；两者都只在 `CognitiveLoop._transition_idle` / `_handle_interrupt` 里被调用 |
| shutdown 清理 | `Agent.stop()` 的 `_SHUTDOWN_STEPS`：`_stop_cognitive_and_tts` → `_drain_cognitive_turns` → … → `_stop_output`（顺序保证收尾先于关资源） |

**§51 的一句话**：
> **`TTSStreamManager` 拥有"这一轮讲话的完成"这件事（`wait_all()` 是唯一 completion signal），
> `FSMBackedPhaseController.finish_turn` 是唯一的正常退出者，`handle_interrupt` 是唯一的异常/打断退出者；
> FSM 本身只执行转换，不判定完成。**

修复前唯一的结构缺陷**不在"谁负责"**，而在"**放行责任被挂在协程体内部**"——
任务没跑起来时，owner 的意图无法表达。修法因此是**补一条与协程体无关的放行路径**，而不是新增 owner。

---

## 6. Scenario Matrix（§9 的 A–M 逐项结果）

| # | 场景 | 进入 SPEAKING? | Terminal event | 期望状态 | HEAD 实测 |
|---|---|---|---|---|---|
| A | TTS success | 是 | `wait_all()` 返回 | IDLE | ✅ IDLE |
| B | TTS generation raises | 是 | 任务内 `except Exception` 捕获 → 任务结束 → `wait_all` 返回 | IDLE | ✅ IDLE |
| B' | LLM 流在**已进 SPEAKING 之后**抛异常 | 是 | `_handle_turn_exception` → `_handle_interrupt` | IDLE | ✅ IDLE |
| C | TTS 返回空 audio | 是 | 任务正常结束（`audio_end` 空流） | IDLE | ✅ IDLE |
| D | output/playback raises（`audio_start` / `audio_chunk`） | 是 | `except Exception` 兜底发文本 → 任务结束 | IDLE | ✅ IDLE |
| E | 用户 interrupt during SPEAKING | 是 | `cancel_current` → `CancelledError` → `_handle_interrupt` | IDLE（经 INTERRUPTED） | ✅ IDLE |
| F | cancel before TTS starts（回合级） | 是 | 同 E | IDLE | ✅ IDLE |
| G | cancel while TTS in progress（`cancel_stream`） | 是 | 任务被取消 → `finally` 放行 → 回合正常收尾 | IDLE | ✅ IDLE |
| H | cancel after produced / before playback complete | 是 | 同 G（`CancelledError` 分支补 `audio_cancelled`） | IDLE | ✅ IDLE |
| I | client disconnect | 是 | `ws_handler` finally → INTERRUPT(disconnect) → `cancel_current` | IDLE | ✅ IDLE（同一原语，见 §12） |
| J | shutdown during SPEAKING | 是 | `_stop_cognitive_and_tts` → `_drain_cognitive_turns` | IDLE | ✅ IDLE |
| K | empty / 无可朗读内容 | **视形状**：仅 `<mood>` → 兜底回复（守卫介入）仍会进；仅 mood+PAUSE → 会进但没有 TTS 合成 | `finish_turn` | IDLE | ✅ IDLE（两条都覆盖） |
| L | new user input arrives while SPEAKING | 是 | VAD/按钮 → INTERRUPT → 旧回合 `_handle_interrupt` | IDLE，随后新回合 THINKING | ✅ IDLE |
| **M** | **TTS 中途关→开（本轮还有句子）** | 是 | ❌ **无 terminal event**：`wait_all()` 永不返回 | **永久 SPEAKING** | ❌ **stranded（本批修复）** |
| N | TTS disabled / TTS unavailable（纯文字） | 是 | `finish_turn`（`wait_all` 无事可等） | IDLE | ✅ IDLE |

M 的完整机制与证据见 §7。

---

## 7. Pre-Fix Reproduction（确定性、无 sleep、E2）

### 7.1 最小形态（manager 级）

```python
mgr.reset_for_turn()
mgr.stream_sentence("句 1")        # Task 已创建，首个 step 尚未运行
await mgr.set_enabled(False)       # 同步段：_enabled=False → flush_on_cancel → cancel_all()
await mgr.set_enabled(True)        # 用户又把 TTS 打开
mgr.stream_sentence("句 2")        # seq=2 —— gate 2 只能由句 1 放行，而它一行都没跑
await mgr.wait_all()               # ← 永不返回
```

现场：`epoch=1 seq=2 gates={1: True, 2: False} _tasks=[tts-2(未完成)]`，句 2 的合成**已完成**，
永久停在 `await gate.wait()`，**没有任何其它在途输出工作**（§3 定义的"非法 SPEAKING"）。

### 7.2 端到端（真实入口路径，§32）

`core/cognitive/tests/test_speaking_lifecycle.py::test_tts_toggle_mid_turn_does_not_strand_speaking`
用**纯 asyncio FIFO**（无 sleep、无巧合）构造生产时序：

1. 回合任务的一个 step 内：mock LLM 生成器 `handler_go.set()`（把"WS handler"的 step 排到后面）
   → 连喂两块文本（分句器在**下一块**到达时才 dispatch 第一句的 SENTENCE）→ `stream_sentence("第一句。")`
   创建 `tts-1`（step 入队，排在 handler 之后）→ `await chunk_go.wait()` 挂起。
2. 下一批 ready = `[handler_step, tts1_step]`：handler 先跑 → `set_enabled(False)`
   → **`tts-1` 在首个 step 之前被取消**（协程体一行不执行）；随后 `set_enabled(True)`。
3. 回合恢复 → 后续句子入队（seq=2/3）→ 门控孤儿 → `finish_turn` → `wait_all()` 永久挂起。

现场（修复前，隔离 worktree 实测 dump）：

```
FSM=speaking  history=[(IDLE,THINKING,ok),(THINKING,SPEAKING,ok)]   ← 无出口
process_lock.locked=True   turn.done=False
gates={1: True, 2: False, 3: False}
alive: tts-2 @tts_adapter.py:491(await gate.wait())  tts-3 @同
turn stack: process@loop.py:730 → _think_loop → _translate_idle → finish_turn → wait_all
```

**生产触发面**：用户在聊天中途点掉 TTS（`Agent.set_service("tts", False)` →
`core/agent/server.py::_sync_tts_gate` → `set_enabled(False)`）再打开。
`BaseEventLoop._run_once` **先 poll I/O 再跑本批 ready**，所以 I/O 唤醒的 WS handler step
可以与"本轮入队句子"的 step 落在同一批，而 TTS 任务的 step 要等下一批 ⇒ 取消落在首个 step 之前。

### 7.3 严重性（同样实测）

卡住期间回合**仍持有 `_process_lock`**，`status.accepts_new_turns=False`：

- 新用户消息进 `Agent._pending_messages`，而它**只在 STATE_CHANGE→IDLE 时出队**
  ⇒ **纯文字聊天里 Agent 永久不再回复**（直到队列满拒收）。
- **新消息不会触发 interrupt**（走排队分支），所以"再说一句"救不回来。
- 出路：VAD/停止按钮（INTERRUPT 事件 → `cancel_current`）或 proactive SYSTEM_TICK（同原语），
  否则要到重启。救援路径本身实测有效（`SPEAKING→INTERRUPTED→IDLE`）。

---

## 8. Root Cause

**放行责任被写在"协程体内部"，而存在一条"协程体从不执行"的取消路径。**

- `_download_and_forward` 的两个早退分支（`not self._enabled` / `not self._tts`）与 `finally`
  都含 `_release_gate(seq+1)`——S2-G3 正是把放行补在这些位置。
- 但 `stream_sentence()` 用 `create_task()`，首 step 要等到**下一批**；同一批内落下的
  `cancel_all()` 会让任务在**起点**被抛出（Python 3.12 语义），于是：
  **早退分支没进、`try/finally` 没进、`_release_gate(seq+1)` 从未执行。**
- 后继门控由后来那句 `stream_sentence(seq+1)` 现建且**未 set**，它的任务永久停在
  `await gate.wait()`；`wait_all()` 无超时 ⇒ `finish_turn` 永不推进 ⇒ FSM 停在 SPEAKING。

**为什么不是"密集短句"**：句子越多，这类窗口出现次数越多（每句入队都是一次机会），
所以原始症状描述指向"5+ 短句"是**频率相关**而非机制必需——1 句也能复现（用同一条取消路径）。

**为什么加超时不是修法**（§23/§24）：`wait_all()` 加超时会把"永久卡死"变成"提前离开 SPEAKING"，
而音频任务仍然活着 ⇒ 状态说"说完了"而音频随后才播（§3 的合法 SPEAKING 被破坏），
正是本批 §99 要防的反面。修法必须是**让放行不再依赖协程体是否运行过**。

---

## 9. Fix

### before

```python
def cancel_all(self) -> None:
    pending = [t for t in self._tasks if not t.done()]
    for t in pending:
        t.cancel()                     # ← 未启动的任务：协程体永不执行
    self._tasks.clear()
    ...
```

### after

```python
def _release_successor_gate(self, seq: int, epoch: int) -> None:
    """放行 `seq` 的后继门控——**与任务是否运行过无关**的兜底释放（KB-6）。"""
    if epoch == self._turn_epoch:      # 与 finally 同一语义：旧轮次不得推进新一轮
        self._release_gate(seq + 1)

def _cancel_task(self, task: asyncio.Task) -> None:
    seq = getattr(task, "_tts_seq", None)
    epoch = getattr(task, "_tts_epoch", None)
    task.cancel()
    if isinstance(seq, int) and isinstance(epoch, int):
        self._release_successor_gate(seq, epoch)

def cancel_all(self) -> None:
    pending = [t for t in self._tasks if not t.done()]
    for t in pending:
        self._cancel_task(t)           # ← 取消点负责补放行
    ...
```

配套：`stream_sentence()` 在创建任务后把 `_tts_seq` / `_tts_epoch` 挂在**任务对象**上
（与既有 `flush_on_cancel` 同一手法）。`cancel_stream(sid)` 走同一个 `_cancel_task`。

### why minimal

1. **一个 owner 不变**：完成权威仍是 `wait_all` + `finish_turn`，没有新增协调器/服务/看门狗。
2. **不引入第二套代次**（§18/§28）：epoch 复用既有 `_turn_epoch`，只是**在入队时就记下**
   （而不是等协程体启动才捕获），因此跨轮隔离与 `finally` 完全同源。
3. **幂等**：`_release_gate` 对已 `set` 的 event 再 `set` 无副作用，所以"取消点 + finally"
   双路径并存安全（无需"只放行一次"的簿记——这正是 §50 double-finally 的形态，但幂等使其无害）。
4. **不吞异常、不加超时、不加 watchdog**（§22/§23/§24/§54）。
5. 不触碰 `wait_all()` 本身：它仍是"无超时"的，但**它的等待对象现在不会出现永久无人放行的门控**——
   残余的无界等待只剩"TTS 后端 `synthesize` 自己不返回"，那属于**输出工作仍在进行**（合法 SPEAKING），
   且各后端都有自己的 HTTP 预算（`minimax` read=30s / `gpt_sovits` read=120s / `openai` 60s），
   输出侧每条通道还被 `OutputManager._CHANNEL_SEND_TIMEOUT=5s` 兜住。

---

## 10. Epoch / Stale Completion Semantics

- `_turn_epoch` 由 `reset_for_turn()` 抬一（每轮 `process()` 开头调用），是**唯一**的轮次代号；
  本批**没有**新增第二个计数器（§19）。
- 任务在**入队时**冻结 `(seq, epoch)`；所有放行都必须通过 `epoch == self._turn_epoch` 判定。
- **旧 epoch 的迟到完成不能推进新一轮**：`finally` 里的放行本来就有这个守卫；本批新加的
  取消点放行**取任务入队时记下的 epoch**（不是"取消发生时的当前 epoch"），因此旧轮次被取消的
  任务在跨轮之后落定时，不会凭空 `set` 新一轮的门控。
- 已由既有测试守卫：`core/adapter/tests/test_tts_adapter.py::test_stale_task_from_previous_turn_does_not_forward`
  （本批复核：去掉 **两处** epoch 检查后该用例红 ⇒ 守卫有判别力；只去掉一处时另一处仍能拦住 ⇒ 冗余而非空洞）。
- 本批另加 `test_stale_completion_from_interrupted_turn_cannot_overwrite_next_turn`：
  在 B 的 SPEAKING **窗口内**手工推进 A 的迟到收尾（同一段完成代码），断言 B 的 SPEAKING 不被改写。

---

## 11. Cancellation Semantics

- 取消是**终态生命周期事件**（§SSR-5）：`cancel_all` / `cancel_stream` 现在同时负责
  "取消任务"与"放行它欠下的后继门控"。
- 双路径幂等：任务已运行过时 `finally` 也会放行一次（`_release_gate` 对已 set 的 event 无副作用）。
- 取消**不会**被吞：`_download_and_forward` 的 `except asyncio.CancelledError` 分支补
  `audio_cancelled` 后 `raise`（既有语义，未改）。
- 已在 HEAD 实测证伪的怀疑（由 Challenger 提出、主 Agent 复核）：
  - 转发窗口内 `cancel_stream` ⇒ 任务正常收尾、`finally` 放行；
  - 在 `CancelledError` 分支的 `await` 上再来一次 `cancel()` **不会丢失**（`finally` 仍执行）；
  - 分支内 `await` 阻塞只会**延迟**收尾——输出侧每条通道有 5s 预算 + 连续 3 次超预算摘除
    （`OutputManager._send_within_budget` / `_CHANNEL_FAILURE_LIMIT`），不存在永久挂起。

---

## 12. Interrupt Semantics

- 合法下一状态**不是硬编码 IDLE**：`handle_interrupt()` 走
  `INTERRUPTED →(0.05s)→ IDLE`，全部经 `VALID_TRANSITIONS` 校验（本批未改）。
- 新用户输入（VAD/按钮）→ `Agent._handle_interrupt` → `CognitiveLoop.cancel_current(source=…)`
  → 旧回合任务收 `CancelledError` → `_handle_interrupt()` → `cancel_all()` + `phases.handle_interrupt()`。
- **client disconnect（矩阵 I）**走同一原语（`ws_handler` finally 里 post
  `InterruptData(source="disconnect")`），本批用同一根因的验证路径覆盖，未单列 E2E。
- 已实测：SPEAKING 期间中断 / 停机 / 断连，FSM 均 `SPEAKING → INTERRUPTED → IDLE`，无残留任务。

---

## 13. Shutdown Semantics

- `Agent.stop()` 的顺序保证"先让回合收尾，再关它依赖的资源"：
  `_stop_cognitive_and_tts`（cancel 回合 + `cancel_all`）→ `_drain_cognitive_turns`（3s 预算，
  等被取消的回合把 generation 终态/租约写完）→ … → `_stop_output`。
- 收尾写的仍是**合法边**，不访问已关闭的 EventBus：`process` 的 `CancelledError` 分支
  在 `_handle_interrupt` 内部完成，`_drain_cognitive_turns` 之后才关 LLM/TTS/output。
- 本批实测（矩阵 J）：SPEAKING 期间 `cancel_current + cancel_all + drain` ⇒ IDLE，无孤儿任务。
- 本批**没有**为停机新增全局终态或特殊分支（§12 of the brief）。

---

## 14. Regression Tests

| 分类 | 用例 |
|---|---|
| **PRE_FIX_RED** | `core/adapter/tests/test_tts_adapter.py::test_prestart_cancel_releases_successor_gate`（manager 级：`wait_all()` 挂死）<br>`core/cognitive/tests/test_speaking_lifecycle.py::test_tts_toggle_mid_turn_does_not_strand_speaking`（端到端：FSM 停在 SPEAKING） |
| **ALREADY_GREEN_GUARD**（本批新增、修前即绿——它们守的是"这条链一旦再断"的形状） | `test_speaking_lifecycle.py` 的 normal / TTS raise / empty audio / output sink raise（×2：`audio_start`、`audio_chunk`）/ LLM 流中途失败 / interrupt / cancel_stream / shutdown / stale-completion / TTS-disabled & unavailable / bg-task→IDLE_BACKGROUND_TASK / 空响应守卫 / markup-only；`test_adapter::test_task_created_before_backend_removed_still_releases_gate`（S2-G3 窗口的另一半） |
| **FALSIFICATION_GUARD**（防假绿：断言"确实进过 SPEAKING"） | `SpeakingHarness.assert_entered_speaking()` —— 每条用例都先断言 `(THINKING→SPEAKING, ok)` 出现在 `RecordingFSM.history` 里，再断言终态；`test_markup_only_response_does_not_strand_speaking` 用反向断言 |
| 既有守卫（本批复核仍有效） | `test_tts_adapter.py::test_early_return_releases_successor_gate`、`::test_reset_for_turn_does_not_orphan_gate_waiter`、`::test_stale_task_from_previous_turn_does_not_forward` |

新增 2 条 PRE_FIX_RED + 16 条 lifecycle 守卫 + 2 条 adapter 守卫。

**观察窗口（§4/§35/§36）**：所有用例只在**结构化终态信号**上等待——
回合任务落定（`asyncio.gather`）、门控全部 set、`wait_all()` 返回、TTS 任务 `done()`、
`asyncio.Event` 屏障（"TTS 已进入 synthesize"/"继续放行"）。
`asyncio.wait_for` 的超时（5s / 2s）只作**死锁探测器**，断言失败时打印
FSM 状态 / history / epoch / 未释放门控 / 存活 TTS 任务 / `process_lock`（§36 要求的诊断）。

---

## 15. Challenger Findings

Challenger B（TTS lifetime / unbounded await，独立 subagent）产出并**由主 Agent 独立复现**：

| 分类 | 内容 |
|---|---|
| **confirmed（本批根因）** | pre-start cancel 门控孤儿 → `wait_all()` 永不返回 → FSM 卡 SPEAKING。Challenger 给出 manager 级最小复现 + 671 交错穷举（14 条违规同形）+ 端到端 wedge + 严重性（`_process_lock` 持有、新消息只进 `_pending_messages`）。主 Agent 用**自己的** harness 独立复现（`tmp/batch9_probe/test_probe7.py`、`tmp/b9_tts/test_p4_e2e_wedge.py` 修复前红） |
| **fix weakness（已采纳）** | Challenger 指出"S2-G3 的早退放行漏掉'任务根本没启动'这一档"——即本批修法的落点。 |
| **fix weakness（未采纳，理由）** | Challenger 建议的另外两条方向：①`wait_all()` 加超时（§23/§24 明令禁止，且会破坏"合法 SPEAKING"）；②在 `stream_sentence` 里"发现前驱已死就自放行"（需额外活性判定，且取消点已有确定信息，属更重的重复机制）。 |
| **reclassified → 潜伏（不在本批修）** | C2：epoch 在**任务首个 step** 才捕获（本批已在入队时冻结并挂到任务上，恰好把它顺手硬化了）。Challenger 自评 E1 可达性未证（所有轮次收尾路径都先取消/等待任务）。 |
| **falsified（不必修）** | q1 早退分支的 `await` 是洞 → 不成立；q2 取消路径全部安全（转发窗口 cancel / 双重 cancel / 分支内阻塞）；q3 `flush_emojis` 并发变异 → 无生产调用者可触发；q4 `reset_for_turn` 跨轮 → 旧门控被放行、epoch 检查成立；q5 `_seq`/gate 对齐（含 emoji/空句）→ 正确 |
| **accepted risk** | `flush_emojis` 的"迭代中被并发入队"形状脆弱但今日不可达（唯一调用者与 `stream_sentence` 同一协程）→ 只登记，不改（§46 的 side-channel 同样不参与完成判定） |
| **new out-of-scope** | 见 §27 |

**Challenger A（FSM exit authority，独立 subagent）**：主结论是「SPEAKING 卡死能构造出来但**只在
`Agent.stop()` 收尾窗口**，下一次 `Agent.start()` 的 `fsm.reset()` 会清掉 ⇒ 用户不可见」。
它与本批的关系与去向：

| 分类 | 内容 |
|---|---|
| **reclassified（不是 KB-6）** | **C1**：`_prepare_turn` 在 `phases.enter_thinking()`（`loop.py:591`）之后仍有一段**未受保护的真挂起 await**（`:609` 的 `generation_registry.register` → Mongo upsert；其 `except Exception` 接不住 `CancelledError`），而包住 `_prepare_turn` 的 try 在 `:714`、包住 `_think_loop` 的在 `:729` ⇒ 该窗口里落下的取消**逃出 `process()`**，`_handle_interrupt()` 永不执行 ⇒ **FSM 停在 THINKING**（不是 SPEAKING）。后果：进程活着但**永久失聪**（后续消息被静默丢弃、`cancel_current()` 返回 False），只有 `Agent.start()` 能救。**本批不修**（KB-6 的 scope 是 SPEAKING 生命周期；FSM 取消边界重排属独立批次），登记为 **KB-22** |
| **accepted risk / currently unreachable** | **C2**：停机 drain 的**第二发 cancel** 打在 `_handle_interrupt` 的无超时 `await bridge.pending_calls()` 上 ⇒ FSM 停在 SPEAKING。只在「已 STOPPED、未 start」窗口成立（`start()` 必 `reset()`、`run_state` 轴本就拒收）⇒ 用户不可见。登记为 **KB-23** |
| **confirmed（机制，可达性未证）** | **C3/C4/C6**：`FSMBackedPhaseController.handle_interrupt()` 缺 loop 侧的 `is_completely_idle` 守卫（IDLE→INTERRUPTED 直接抛）；`finish_turn` 12 组合里 5 组抛（本批复核：均**未经 `process()` 可达**，唯 `reset + 活跃回合 + bg=True` 例外，且异常被 `process` 收口、状态仍回 IDLE）。一并登记 KB-23 |
| **falsified** | **F1**：`handle_interrupt` 的 50ms 窗口**不能被顶掉**（INTERRUPTED 的唯一合法出口是 IDLE；`process()` 被 `accepts_new_turns` 拒收；`enter_thinking()` 被转换表拒）。**F2**：一轮只有一次终态化；`finish_turn` 在 IDLE 上幂等。**F3**：`reset()` 打在活跃 SPEAKING 回合上仍以 IDLE 收尾（不卡死）。本批 §10 的「旧 epoch 迟到完成」与它同族，另有本批独立用例守卫 |
| **主 Agent 复核** | C1 的 E2 用例由 Challenger 提供（`tmp/b9_fsm/test_audit*.py` 系列），主 Agent 复核了其**静态结构**（`:591` 早于 `:609` 的 try；主 try 在 `:729`）并独立搭了同形停车场探针；探针本次跑在"取消尚未送达"的一侧（任务在停车场内被释放后正常走完），未能独立复现 C1 的**送达侧** ⇒ 如实标注：**C1 = Challenger E2 + 主 Agent E1 静态复核**，未达"主 Agent 独立 E2"。它的分类与 KB-6 无关，故不影响本批结论 |

---

## 16. Mutation Evidence

独立 worktree `/tmp/b9-mut`（HEAD + 仅测试文件），逐条注入：

| # | 注入 | 结果 |
|---|---|---|
| **M8** | **本批修法的最小形态**：删掉 `cancel_all` 取消点的补放行（等价于修复前行为） | **2 failed**：`test_prestart_cancel_releases_successor_gate` + `test_tts_toggle_mid_turn_does_not_strand_speaking`（前者 manager 级挂死、后者 FSM 停在 SPEAKING） |
| M1 | 删掉 `_download_and_forward` 两个早退分支的 `_release_gate(seq+1)`（回到 S2-G3 前） | 2 failed：既有 `test_early_return_releases_successor_gate` + 本批 `test_task_created_before_backend_removed_still_releases_gate` |
| M2 | `FSMBackedPhaseController.finish_turn` 整段跳过（`wait_all` + 状态转换都不做） | **5 failed**（含新增的"输出工作还活着时不得提前退出"） |
| M4 | `finish_turn` 的"无后台任务"分支直接 `return`（不转 IDLE） | **11 failed / 4 passed** |
| M5 | `process` 的 `CancelledError` 分支不调 `_handle_interrupt()` | 3 failed：interrupt / shutdown / stale-completion |
| M6 | `_handle_turn_exception` 不调 `_handle_interrupt()` | 1 failed：LLM 流中途失败用例（**证明该用例确实从 SPEAKING 走失败分支**，而不是"没进 SPEAKING"的假绿） |
| M7 | 删掉 `_download_and_forward` **一处** epoch 检查 | 0 failed —— 另一处仍拦住（**冗余守卫**，如实记录） |
| M7b | 删掉**两处** epoch 检查 | 1 failed：既有 `test_stale_task_from_previous_turn_does_not_forward` |

---

## 17. Task/Queue Leak Evidence

- 每条 lifecycle 用例结束都断言：无 `not t.done()` 的 TTS 任务、无 `not ev.is_set()` 的门控；
  交错类用例显式 `aclose()`（取消残留任务并 `gather`）。
- 修复前 stranded 现场：`tts-2` / `tts-3` 永久停在 `await gate.wait()`；修复后同一用例
  `assert not alive` 通过。
- `_task_by_sid` 在既有实现里会保留**已完成**任务的引用直到下一次 `stream_sentence` 剪枝
  （本次观察到的 `task_by_sid: [1..8]` 即此）——`cancel_stream` 对 `done()` 任务返回 False，
  不影响语义；属既有形状，**不在本批改**（§87 登记）。

---

## 18. Public API / Schema Impact

**无**。未新增/修改任何契约模型、WS 帧、REST 字段、枚举；未跑 `make gen-types`（无产物漂移：
`make check-types` 绿）。`TTSStreamManager` 的新增成员均为私有（`_release_successor_gate` / `_cancel_task`）
与任务上的动态属性（测试替身可见）。

---

## 19. Persistence Impact

**无**。无新集合/字段/索引/迁移；FSM 运行期状态本就不落库。本批未触碰任何 repository。

---

## 20. Performance Impact

- **零新增轮询 / 心跳 / 定时器 / 看门狗**（§78）。改动只在"创建任务"时多两次属性赋值、
  在"取消任务"时多一次 dict 查找 + `Event.set()`（每条被取消的句子一次，常数级）。
- 正常路径（无取消）行为与修复前**逐字节一致**。

---

## 21. Commits

| commit | 内容 |
|---|---|
| `fix(tts): guarantee speaking lifecycle termination on pre-start cancel` | `core/adapter/tts_adapter.py` + 2 条 PRE_FIX_RED 测试 + 16 条 lifecycle 守卫（同一 commit，§84） |
| `docs(architecture): record speaking lifecycle invariants` | `docs/architecture/invariants.md` 新增 SP1–SP4 |
| `docs(todo): reclassify KB-6 as guarded` | `todo.md` KB-6 行 |
| `docs(review): record Batch 9 speaking state recovery` | 本文件 |

---

## 22. Worktree Verification

```bash
git worktree add /tmp/b9-prefix HEAD          # 未修代码（先验 S2-G3 机制）
git worktree add /tmp/b9-mut    HEAD          # 变异注入
```

| worktree | 用途 | 结果 |
|---|---|---|
| `/tmp/b9-prefix` @ `9f323f07~1`（S2-G3 之前） | 证明 KB-6 的**第一代**机制（早退分支不放行）真实存在 | manager 级交错 ⇒ `wait_all()` 永不返回（`test_origin2.py` 失败于断言，非挂起 pytest） |
| `/tmp/b9-mut` @ HEAD | 变异矩阵（§16） | M1/M2/M4/M5/M6/M8 各有用例红；M7 冗余（如实记录） |
| 工作树内联验证（`git stash` 仅回退 `tts_adapter.py`） | 本批修复的 PRE_FIX_RED 属性 | 2 failed（PRE_FIX_RED 用例） / 27 passed；恢复修复后全绿 |

---

## 23. Test Results

| 项 | 结果 |
|---|---|
| targeted（`core/status` + `core/adapter` + `core/cognitive`） | 见下 |
| 新增套件 | `core/cognitive/tests/test_speaking_lifecycle.py` 16 passed；`core/adapter/tests/test_tts_adapter.py` 13 passed |
| order perturbation（§91） | 交错用例按"success→failure / failure→success / interrupt→normal / normal→interrupt / 陈旧完成→新轮"覆盖；另有 400 轮随机交错探针（`tmp/batch9_probe/test_probe4.py`，4×100 seed）无 stranded、无孤儿任务 |
| fresh process（§92） | **NOT APPLICABLE**：`TTSStreamManager` / `CognitiveLoop` / `AgentFSM` 均为实例级对象，无模块级单例或 import 期状态；每个用例自建实例（`RecordingFSM` 记录 history 也只在测试内） |
| `make test-quick` | 见 §23 附表 |
| `make test-normal` | 同上 |
| `make test-full` | 同上 |
| frontend / build | 后端 schema 未变 ⇒ 只跑 required build（见附表） |
| static gates | `lint` / `pyright` / `check-arch` / `check-contract` / `check-types` / `check-timeouts` / `check-test-sets` 全绿（§89） |
| real CI | 见附表（push 后确认 Backend / Real MongoDB / Frontend 全绿） |

---

## 24. Invariant Mapping

见 `docs/architecture/invariants.md` §4.2 SP1–SP4（本批新增）。
四条全部有可执行守卫：

| 不变量 | 守卫 |
|---|---|
| SP1 每次进入 SPEAKING 都有退出 owner | `test_speaking_lifecycle.py`（全 16 条）——每条断言"进过 SPEAKING"且"最终离开" |
| SP2 success/failure/cancel/interrupt/empty/shutdown 都不能留下 SPEAKING 且无输出工作 | 同上逐场景；`assert_no_speaking_work_left()` |
| SP3 旧 speaking epoch 的迟到完成不得覆盖新一轮 | `test_stale_completion_from_interrupted_turn_cannot_overwrite_next_turn`；adapter 侧 `test_stale_task_from_previous_turn_does_not_forward` |
| SP4 完成由生命周期完成驱动，而不是队列空 | `test_speaking_does_not_exit_while_output_work_is_alive`（活着不许退）+ `test_prestart_cancel_releases_successor_gate`（门控链必须闭合） |

---

## 25. Todo Update

KB-6：`INSUFFICIENT_EVIDENCE` → **`GUARDED`**（证据 E2；REACH `USER`；SEV `MEDIUM`）。
其余 canonical item **未改**。

---

## 26. Keep As-Is

- **FSM 架构**：状态枚举、`VALID_TRANSITIONS` 矩阵、`AgentFSM`、`FSMBackedStatus/PhaseController`
  一行未改（§77）。本批只补"漏发的生命周期事件/放行"。
- **EventBus priority**：`INTERRUPT(0) / STATE_CHANGE(1) / NORMAL(2)` 未改；本批链路不依赖优先级抢占
  （SPEAKING 的完成不经 EventBus，走 `wait_all()` 的 await）。
- **TTSStreamManager ownership**：完成权威仍是 `wait_all()`；本批只补了"取消点也要放行"这一条
  与协程体无关的路径，未拆模块、未引入 `SpeakingLifecycleService` 之类的协调器（§52）。
- **no watchdog**：未新增任何轮询/看门狗。
- **no timeout reset**：未新增"SPEAKING 超 N 秒就复位"。
- **no frontend change**：未改前端；SPEAKING 的用户可见影响见 §27（UI 指示/打断策略均未改语义）。
- `wait_all()` 仍是"无超时"：残余的无界等待只剩"TTS 后端自己不返回"（属合法 SPEAKING），
  且各后端有 HTTP 预算、输出侧有 5s/通道预算。

---

## 27. New Findings（只登记）

| ID 建议 | 内容 | 证据 |
|---|---|---|
| **KB-18** | `TTSStreamManager._task_by_sid` 会保留**已完成**任务的引用，直到下一次 `stream_sentence` 才剪枝；长会话下该 dict 随轮次线性增长（每条仅一个已结束 Task 引用，非任务泄漏）。`cancel_stream` 对 `done()` 任务返回 False，语义未失真 | E2（本批观察：8 句后 `task_by_sid` 仍有 8 个 done 任务） |
| **KB-19** | `flush_emojis()` 在迭代 `_pending_emojis` 时若被并发 `pop`/入队会 KeyError / 提前广播 / 静默丢弃。**今日不可达**（唯一调用者与 `stream_sentence` 同一协程） | E2（Challenger 构造；可达性 E1 未证） |
| **KB-20** | `_download_and_forward` 的 `except Exception` 会吞掉**输出侧**任何异常（含 `AttributeError` 之类的编程错误），只记 warning；测试替身缺方法时会静默变成"没转发"（S2-G3 的教训已被本批复用，但生产侧仍无区分） | E2 |
| **KB-22** | **Challenger A（严重度高于 KB-6）**：`_prepare_turn` 里 `enter_thinking()` 之后仍有未受保护的挂起 await（`generation_registry.register`）⇒ 该窗口的取消逃出 `process()`，FSM 停在 **THINKING**，agent 活着但永久失聪（只有 `Agent.start()` 能救） | E2(Challenger)+E1(主 Agent 静态复核) |
| **KB-23** | **Challenger A**：停机 drain 的第二发 cancel 逃出 `_handle_interrupt`（停在无超时的 `bridge.pending_calls()`）⇒ 停在 SPEAKING，但只在「已 STOPPED、未 start」窗口 ⇒ 用户不可见；附带 `handle_interrupt` 的 IDLE 守卫缺失、`finish_turn` 5/12 组合抛（未经 `process()` 可达） | E2(Challenger) |
| **KB-21** | SPEAKING 的语义是"服务端正在输出"，与"用户实际听到"无关（客户端播放不被追踪）。当前已知且有意（§3/§57），但对 UI 指示与打断策略的使用者不显然——建议在文档/契约注释里显式写明 | E1 |
| **OBS-8** | `core/cognitive/finalize.py::_run_after_response` 在 engine 的 `reflect` 为 `MagicMock`/非协程时会记 warning（`object MagicMock can't be used in 'await' expression`）——测试替身形状问题，非生产缺陷，但会污染诊断输出 | E2 |

---

## 28. Follow-up（canonical backlog 保留）

`KB-12`（预览窗口估算误差带）、`KB-13` + **Ollama Provider Adapter / AR-16**（本批明令不碰）、
`KB-15/16/17`、`OBS-6/7`、`KB-11`、`GR-25`、`N-15`，以及本批新增 `KB-18..KB-23` / `OBS-8`
（均只登记，未修）。

**建议下一批优先 `KB-22`**：它是本批发现的唯一「进程活着但不可恢复、且用户面可达」的失效
（FSM 停在 THINKING、后续消息静默丢弃、无任何恢复路径），严重度高于 KB-6；修法方向是把
`enter_thinking()` 之后的 await 收进 `process()` 的取消边界（或把 `enter_thinking` 挪进
`_prepare_turn` 的受保护区末尾）。它与 KB-23 同属"取消边界"一族，适合合并成一批。
