# 待做（Backlog）

> **本文件是 backlog 的 current source of truth**（2026-09-18 由 [Batch 4 backlog triage](docs/review/2026-09-18-Batch4-backlog-triage.md) 重构）。
>
> 用法：
> 1. 找活干先看 **§1 Canonical Backlog**——每个活动项有稳定 ID、可达性、证据级别与候选批次；
> 2. 词表见 §1.1；**FIXED ≠ GUARDED**（行为已修但无回归保护 = `FIXED`；行为已修且守卫可靠 = `GUARDED`）；
> 3. 已关闭项只留一行索引（§5），逐条证据在 `docs/review/` 与 `docs/completed/`；
> 4. **本次重构没有删除任何证据**：重构前全文见 `git show 69f2e5a9:todo.md`，长叙事各条都指向其执行记录。
>
> 覆盖审计（旧文件每一节 → 现在在哪）见 triage 文档 §7。

## 1. Canonical Backlog（Active）

### 1.1 词表（统一口径）

| 维度 | 取值 |
|---|---|
| **Status** | `OPEN` / `GUARDED` / `FIXED` / `ALREADY_FIXED` / `FALSE_POSITIVE` / `KEEP_AS_IS` / `DEFERRED` / `CURRENTLY_UNREACHABLE` / `PRODUCT_DECISION_REQUIRED` / `INSUFFICIENT_EVIDENCE` / `SUPERSEDED` |
| **Reachability** | `USER` / `DEPLOYMENT` / `ADMIN` / `INTERNAL` / `LEGACY_DATA_ONLY` / `CURRENTLY_UNREACHABLE` / `UNKNOWN` |
| **Evidence** | `E3` 真实 runtime/integration 复现；`E2` 定向测试/子进程复现；`E1` 静态控制流证据；`E0` 仅假设 |
| **Severity** | `CRITICAL` / `HIGH` / `MEDIUM` / `LOW`；架构/功能项写 `N/A` 并单独评估维护风险 |
| **标签** | `TRUTHFULNESS`＝reported truth ≠ actual truth（本项目最大主题，见 §3） |

> 新 ID 约定：`KB-*` 已知 Bug 表、`OBS-*` 运行时健康/观测、`DEP-*` 部署与配置、`PD-*` 产品决策、
> `AR-*` 架构债、`GR-*` 门禁/测试债、`FEAT-*` 未来功能。这些 ID 是**本批为原先无 ID 的条目首次分配**；
> 已有 ID（`N-*`/`F*`/`B3-*`/`CR-*`/`P2-*`/`S2-*`/`M19/M20`）一律保留，编号漂移见 §2。

### 1.2 Correctness（41 行：17 条 USER/DEPLOYMENT 可达 + 24 条 INTERNAL 侧）

| ID | 标题 | Reach | Sev | Ev | Status | 候选批次 |
|---|---|---|---|---|---|---|
| **N-1** | ~~`/ready` 的 identity 门禁探「repo 可达」而非「身份已装载且可用」⇒ 装载失败时 `/ready` 200 而 `/api/me` 503~~ | DEPLOYMENT | HIGH | E1(HEAD)+E3(3.5)+**E3(4)** | **GUARDED** `82b80e2e` | ✅ Batch 4 |
| **N-11** | ~~`ProfileStore.connect` / `_load_from_file` 吞掉一切异常（N-1 的上游：失败不可观测）~~ | DEPLOYMENT | MEDIUM | E1+E3(3.5)+**E2(4)** | **GUARDED** `fc648e20` | ✅ Batch 4 |
| **N-3** | ~~409 `details.currentRevision` 读进程内陈旧缓存 ⇒ 谎报，文档承诺的 CAS 恢复路径可死循环~~ —— 冲突现值改由 repository 在冲突点回读权威库并随结果携带（`UserIdentityUpdateOutcome.current_revision`），route 只投影；未知则省略该字段 | USER | MEDIUM | E1+**E2+E3(6)** | **GUARDED** `616e4993` `TRUTHFULNESS` | ✅ Batch 6 |
| **KB-6** | ~~TTS `wait_all()` 无超时 ⇒ FSM 卡 `SPEAKING`~~ —— **已确证用户可达并修复**（Batch 9）。三条挂起源，各是"某个门控 `_release_events[seq]` 永不 `set`"：① 早退分支在 `try/finally` 之外 `return`；② `reset_for_turn` 直接 `clear()` 门控 dict（①② 由 S2-G3 `9f323f07` 修）；③ **任务在"首个 event-loop step 之前"被取消 ⇒ 协程体一行都不执行、早退分支与 `finally` 全都没进**（Batch 9 修：取消点 `_cancel_task` 也负责放行后继门控，epoch 取入队时冻结值）。③ 的用户可达路径 = 本轮播放中点掉 TTS 再打开（`set_service("tts", False/True)` → `set_enabled(False)` 的同步段 `cancel_all()`；`BaseEventLoop._run_once` 先 poll I/O 再跑本批 ready ⇒ WS handler 的 step 与"入队句子"的 step 同批，而 TTS 任务的 step 要等下一批）。卡住期间回合仍持 `_process_lock` ⇒ 新消息只进 `_pending_messages`（只在 `STATE_CHANGE→IDLE` 出队）⇒ 纯文字聊天不再回复，且新消息**不**触发 interrupt（只能等 VAD/停止按钮或重启）。**明确不修**：不加 `wait_all` 超时、不加 watchdog/超时复位（会违反 SP4：状态说"说完了"而音频随后才播） | USER | MEDIUM | E1+S2-G3(E2)+**E2(9)** | **GUARDED** `fix(tts): guarantee speaking lifecycle termination on pre-start cancel` `TRUTHFULNESS` | ✅ Batch 9 |
| **F8** | ~~prompt 预算 cross-path：`core/character/preview.py` 调 `build_messages` **不传** `prompt_budget_tokens`（chat 路径传） ⇒ 预览路径可超 context → provider 400~~ —— 预览改用 canonical 预算（**resolved runtime model** 的声明窗口 × 同一常量）；裁完仍装不下 → **本地** 400 `CHARACTER_VALIDATION`（非重试、**零 provider 请求**），不再把确定性的输入问题谎报成可重试的 503。**频率主张修正（Batch 7 现测）**：原记「≈9283 est tokens」实为**字符数**（9235 chars → 7597 est tokens < 8192）⇒「内置角色 100% 复现」不成立；真实边界是**预览消息 > ~575 CJK 字**（内置 elysia 余量）或档案更大时确定性失败 | USER | MEDIUM | E1+**E2(7)** | **GUARDED** `558503be` `TRUTHFULNESS` | ✅ Batch 7 |
| **KB-8** | tokenizer NORMAL 态 `_pending` / `_held` 缓冲**无上界**（长无换行段落可撑到 MB 级） | INTERNAL | LOW | E1 | **OPEN** | 小修 |
| **F13** | 系统注入文本（唤醒词）被当作「用户消息」参与记忆提取 / mood 判定 | USER | LOW | E1 | **OPEN** | 小修 |
| **P2-01r** | ~~会话列表 `list_all` 仍预取 200 ⇒ **第 201+ 条不可分页/搜索**~~ —— 分页下推到存储（`SessionRepository.list_page` keyset：谓词 + `updated_at DESC, session_id ASC` + `limit` + title `$regex` 全部进查询），`SessionManager.list()` 改为"持久层分页流 ⊕ 进程内覆盖层"归并，`get_summary` 按 ID 直读单文档。**另一半**（显式 session 过期级联清理 worker）经复核 **SUPERSEDED**：2026-08-10 起 `sessions` 不再有 TTL/过期机制，删除走显式级联（`cascade_repos` + AST 覆盖守卫 `test_session_cascade.py`），没有"过期"可级联 | USER | MEDIUM | ~~E1~~ **E2+E3(8)** | **GUARDED** `36cb7cec` | ✅ Batch 8 |
| **KB-17** | **Batch 8 副产物**：`PATCH /api/sessions/{id}` 的 title **无长度上限**，而 `POST /api/sessions`（`SessionCreateRequest`）限 200、搜索词上限也是 200（超长按"无匹配"）——于是 >200 字的标题可被创建，但**只能用 ≤200 字的搜索词命中**；两条写路径的契约不对称 | USER | LOW | E1 | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` | 零散 |
| **KB-22** | ~~`_prepare_turn` 在 `enter_thinking()` 之后有未受保护的挂起 await ⇒ 取消逃出 `process()`、FSM 永停 THINKING~~ —— **已确证用户可达并修复**（Batch 10）。取锁（`loop.py:699`）到受保护 try（`:729`）之间**整段没有 CancelledError-aware handler**，其中可挂起点 4 个（争用锁 / `session_resolver` / `registry.get_or_load` / `generation_registry.register`），**只有 `register`（Mongo upsert）在 `enter_thinking()` 之后**——它就是致命的那一个：取消从这里逃出 ⇒ `_process_lock` 被 `async with` 释放、canonical 收口 `_handle_interrupt()` 永不执行 ⇒ **锁/任务/FSM 三者真相不一致**（TH4）。用户面后果 = **永久失聪**：相位 THINKING ⇒ `is_accepting_input` 恒假 ⇒ 之后每条消息都被 ACK `accepted` 后进 `_pending_messages`（只在 `STATE_CHANGE→IDLE` 出队，那个事件再也不会来），队列满后转 `QUEUE_FULL` rejected；进程/EventBus 都活着，只有 `Agent.start()`（`fsm.reset()`）或重启能救。**可达取消源（用户可见 ×4）**：停止按钮（WS `interrupt`，Escape/Ctrl+Escape/`beforeunload`）、语音会话取消、客户端断线（`source="disconnect"`，其归属守卫读的 `_generation_ctx.user_message_id` 正在窗口内建立）、`Agent.stop()`；另有模型热切换与后台任务终态唤醒两条直调路径。**修法**：取消边界与异常边界一起覆盖**整个回合**（外层 `except asyncio.CancelledError` → `_handle_prepare_cancelled()`；内层 `except Exception` → `_handle_turn_exception`），收口复用 canonical `_handle_interrupt()`（`THINKING→INTERRUPTED→IDLE`，`THINKING→IDLE` 是非法边）；ownership 判据 = 本回合的 `_generation_ctx`（`enter_thinking()` **之前**建立、回合入口清空、全程持锁）而**非** `state == THINKING`；入口之前的取消/异常保持既有语义（传播）。**明确不做**：不移动 `enter_thinking()`、不写 `finally: set_idle()`、不新增 epoch/计数器、不加 watchdog/超时复位/shield 整个 `process()`。**残留（同族，登记为 KB-25）**：收口链被**第二发** `task.cancel()`（停机 `_turns.drain(3s)` 超时）打断仍可留下无主 THINKING/INTERRUPTED | USER | HIGH | E1+**E2(10)** | **GUARDED** `f550666c` | ✅ Batch 10 |
| **KB-23** | ~~**Batch 9 Challenger A**：`Agent.stop()` 的 `_drain_cognitive_turns`（3s 预算）会对已被取消的回合发**第二发** `task.cancel()`；若该回合正停在 `_handle_interrupt` 的**无超时** `await bridge.pending_calls()`（`loop.py:1240`），第二发取消直接逃出 `_handle_interrupt` ⇒ FSM 停在 SPEAKING~~ —— **Batch 11 判定为 KB-25 的同 root 别名（`SAME_ROOT_ALIAS`）**：同一回合任务、同一 `CognitiveLoop._handle_interrupt`、同一第二发来源（`AsyncTaskScope.drain` ← `Agent.stop()`，全仓唯一）、同一窗口（已 STOPPED / 未 start）、同一唯一修复点（`transition(IDLE)` 之前的任何一个 await），差别只是"第二发落在同一条链的哪个 await 上"（决定停在 SPEAKING / THINKING / INTERRUPTED）。⇒ **SUPERSEDED_BY KB-25**，由 `b56a8442` 同一 fix 覆盖。**附带发现处置**：① `handle_interrupt` 缺 `is_completely_idle` 守卫 —— 本批**已补**（IDLE 早退，幂等 + 避开 `IDLE→INTERRUPTED` 非法边）；② `_CHANNEL_SEND_TIMEOUT=5.0` > drain 预算 3.0 的**门禁盲区** —— 转 **KB-32**；③ `finish_turn` 12 组合 5 组抛且零 gated 覆盖 —— 转 **KB-33**。**题面更正**：`bridge.pending_calls()` 实际在 `core/cognitive/loop.py:1324`（修复前），且只在 helper `_cancel_desktop_calls` 里被**间接** await；其暴露也被原登记**高估**——它需要"desktop 桥 + 在途调用"，而 KB-25 的 `mark` 什么都不需要 | INTERNAL | LOW | E2(9)+**E2(11)** | **SUPERSEDED_BY KB-25** `b56a8442` | ✅ Batch 11 |
| **KB-24** | ~~**Batch 10 副产物（O1）**：取消落在**回合开始之前**（`session_resolver` / `get_or_load`）时，本回合已 ACK 的消息**静默结束**~~ —— **Batch 12 已确证并修复**（严重度**上调** LOW→**HIGH**）。**机制**：`Agent._handle_user_input` 判断「能否抢占在途回合」用的是 `prev not in self._started_cognitive_tasks`——它回答「coroutine 有没有 first-step」，而抢占需要的是「这条**输入**有没有被接管」。两者在 `_prepare_turn` 期间不一致（已 first-step，但 `_generation_ctx` 已清空、尚未赋值）⇒ 旧判据放行抢占 ⇒ 取消落到会话解析/角色装载的挂起点 ⇒ CancelledError 逃出 `process()`（`_handle_prepare_cancelled` 返回 False 后 `raise`）⇒ 回合任务 cancelled。此时这条消息**不在** session/archive（append 在 `_build_turn_prompt`，即 `_think_loop` 之内）、**不在** pending、EventBus 对 USER_INPUT **不重投**、一个 `generation.*` 帧都没发；而 v2 幂等表已在 ACK 前落成 ACCEPTED ⇒ 客户端同 `clientRequestId` 重试只会被**逐字重放那个 accepted**。**触发不需要任何故障**：用户在窗口内**再发一条消息**即可（前端连发两条 = 常态操作，窗口 = 真实的 Mongo 会话/角色装载耗时）。**`accepted` 的真实合同（本批先确认后修）**：Transport Accepted ＋决策 幂等；**不**承诺持久化、**不**承诺一定被回复、**不**承诺失败一定告知 ⇒ 本批**不**升级成 durable guarantee，只建立「accepted 之后始终有 owner 或显式终态」。**修法（最小：两处生产改动 ＋ 归因）**：① 新增 `CognitiveLoop.current_turn_owns_input`（`_generation_ctx is not None`），并在 **`process()` 取锁之前**清空（Challenger C：`await _process_lock` 是第一挂起点，陈旧的 ctx 会让停锁的回合被误判「已接管」）；② `_handle_user_input` 的抢占判据补上「未接管 ⇒ 不抢占（新输入排队）」；③ 四处**可归因终态**（`cancelled_pre_ownership` / `failed_pre_ownership` / `cancelled_by_interrupt`，含取消来源与 message/session 身份，**不**新增 wire 帧、**不**写库）；④ 无 source 的 `cancel_current()` 把来源重置为 `unknown`（修前沿用上一发 ⇒ 错归因）；⑤ 回合结束后的 pending 兜底出队（Challenger A：窗口内取消/异常时相位**从未离开 IDLE** ⇒ 没有 `STATE_CHANGE→IDLE` ⇒ 被新守卫排队的已 ACK 消息会永久滞留，而 Agent 显示完全空闲）。**明确不做**：不延后 ACK、不建 durable queue/inbox、不新增 FSM 状态或 wire 枚举、不改 pending 出队机制、不加 watchdog/轮询/shield、不把 session history 当队列、不对所有 `CancelledError` 一律 requeue（那会复活用户 stop 掉的输入）。**守卫**：12 条 gated 用例（9 条 PRE_FIX_RED / 前提级红）＋ 变异 M0–M7 各自被不同守卫杀死；Challenger 独立反证 3 个真洞（全部修复并各配守卫） | **USER** | **HIGH**（原 LOW） | E2(10)+**E2(12)** | **GUARDED** `Batch 12` | ✅ Batch 12 |
| **KB-25** | ~~**Batch 10 Challenger（E2/F1/F2/F2b）**：收口链（`turn_cancelled → mark → cancel_desktop → typing → _notify → sleep(0.05) → …`）被**第二发直接 `task.cancel()`** 打断 ⇒ 相位停在 THINKING 或 INTERRUPTED（同样失聪）~~ —— **Batch 11 已确证并修复**。**机制**：`asyncio.CancelledError` 是 `BaseException`，既不被沿途的 `except Exception` 接住、也**不被兄弟 `except` 子句接住**，而"相位离开活跃区 + 广播 IDLE"（逻辑终态）此前挂在一条长 await 链**链尾** ⇒ 任何一发直接 `task.cancel()` 落在链上就把它整条带走：锁被 `async with` 释放、任务 cancelled，而相位永停 THINKING/INTERRUPTED ⇒ `is_accepting_input` 恒假 ⇒ 后续每条消息都被 ACK `accepted` 后进 `_pending_messages`（只在 `STATE_CHANGE→IDLE` 出队）⇒ **永久失聪**，只有 `Agent.start()` 能救。**逐点实测（真 Agent + 屏障，6 个挂起点）**：`turn_cancelled` / `mark` / `typing(False)` ⇒ 停 THINKING；`_notify("interrupted")` / `sleep(0.05)` ⇒ 停 INTERRUPTED；`_notify("idle")`（逻辑终态之后）⇒ 稳定。**第二发生产来源唯一**：`Agent.stop()` 第 3 步 `_turns.drain(timeout=3.0)` 预算耗尽后的 `task.cancel()`（`cancel_current()` 本身 one-shot，不可能发第二发）⇒ 严格形态的窗口是 `run_state == STOPPED`（**后果用户不可见**，与 KB-23 同）。**同 boundary 的"第一发"变体用户可见（见 KB-31）**：provider 失败收尾时按停止按钮 ⇒ RUNNING 下永久失聪。**修法（owner-side，最小）**：逻辑终态（同步：`transition(IDLE)` + `bus.post(IDLE)`）进 `finally` —— `FSMBackedPhaseController.handle_interrupt` 的 IDLE 落位/广播/通知进 `finally` 并对 IDLE 早退（幂等 + 非法边）、`_handle_interrupt` 的 best-effort 段进 `try` 且 `phases.handle_interrupt()` 进 `finally`、`_handle_turn_exception` 同理；资源清理（帧 / mark / desktop / TTS / typing）**保持可取消**。**明确不做**：不 shield 整块（会让清理逃出 `_process_lock`，破坏 TH3）、不加 `uncancel()`、不把 `Task.cancelling()` 当 authority、不 retry-until-clean、不重排 wire 帧顺序。**守卫**：21 条 gated 用例（15 条 PRE_FIX_RED）+ 2 条 real-mongo 旁证；变异 M0–M7 各被不同守卫杀死 | INTERNAL（严格形态）/ **USER**（KB-31 变体） | MEDIUM（严格形态）/ **HIGH**（KB-31 变体） | E2(10)+**E2(11)** | **GUARDED** `b56a8442` | ✅ Batch 11 |
| **KB-26** | **Batch 10 副产物**：`CognitiveLoop._current_task` 在**正常完成的回合**后仍指向已结束的任务（只在 `cancel_current()` / `reset_lifecycle_state()` 清）。**Batch 14 复核（原 N-系列）**：状态**成立**、缺陷**证伪**——生产读者**恰好 5 处**（`loop.py:418/431`、`agent.py:1092/1340`、`debug/info.py:164`），每一处都在 `.done()` 守卫之内；ownership truth 自 Batch 12 起是 `_generation_ctx`（`current_turn_owns_input`）＋ `_started_cognitive_tasks`，`_current_task` 只剩「最近 spawn 的回合 / 取消目标」这一含义。实测（真 Agent，逐个触发全部读者 + 2 公开 + 1 直投 burst）：错误取消=无、错误排队=无、误报忙=无（debug `processing=False`）、跳过登记=无、顺序 `B→C→D` 无丢失/重复 ⇒ **无需为 hygiene 改生命周期**。证据 `tmp/b14/subA/probe_kb26.py`（5 例）；边界落纸见 `invariants.md` §4.7 RO3 | INTERNAL | LOW | E2(14) | **KEEP_AS_IS**（FALSIFIED_BY_GUARD） | — |
| **KB-27** | **Batch 10 审计（OBS）**：`_AFTER_RESPONSE_DRAIN_TIMEOUT = 10.0`（`loop.py:54`）以 `drain_timeout=` 传入，**未登记**在 `scripts/check_timeout_budget.py`，而门禁只匹配关键字 `timeout` ⇒ 该预算对门禁**不可见**（R3 的具名预算盘点是盲的） | INTERNAL | LOW | E1(10) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` | 零散（门禁） |
| **KB-28** | **Batch 10 Challenger（O2/J3）**：`IDLE 已广播 / 锁未释放`窗口——收口或回合在 post `StateChangeData(IDLE)` 之后仍持锁挂起时，pending 出队 spawn 的回合会卡在 `_process_lock` 上。**Batch 14 复核（原 O2/J3）**：主形态**已证伪**——卡在锁上的回合尚未接管输入（`_generation_ctx` 已在 `process()` 入口清空，`loop.py:874`），于是 Batch 12 的 `not_owning`（`agent.py:1093-1095`）＋ Batch 13 的 `_has_older_waiting_input`（`agent.py:990-1032`）让新输入（公开入口与直投）**排队**而不是取消它。实测（确定性子进程探针，挂起 `on_change` 订阅者造窗口）：`[T1] final: claims=['KA','KB','KC','KV'] session=[同] llm=[同] turn_b_cancelled=False pending=[] fsm=idle lock=False` ⇒ 无 bypass / 无丢失 / 无重复 / 队列正常排空。**残留一半**：严格**两发停止**形态（第一发收口在 `finally` 里 post IDLE 仍持锁 → 新回合在锁上被第二发取消）确实**静默**丢失（`dispositions=[]`：`_handle_prepare_cancelled` 在锁之后，够不着）——但前提仍是「存在会挂起的 `on_change` 订阅者」，而仓内生产**不注册**这类订阅者（唯一注册者 `wait_until_idle` 的回调无 await，生产调用点只有 `bench`）⇒ 今天不可达。**Batch 11 补证**（第二发取消路径）同此结论。证据 `tmp/b14/subA/probe_kb28.py`（4 例） | USER（前提不足）→ **CURRENTLY_UNREACHABLE** | LOW | E2(10)+E2(11)+**E2(14)** | **CURRENTLY_UNREACHABLE**（主形态已由 Batch 12/13 判据覆盖；残留待有订阅者时重估）| 触发式 |
| **KB-29** | **Batch 10 Challenger（O4）**：`_handle_turn_exception` 在**回合开始之前**触发时（如 `StorageUnavailableError`）会对上一回合遗留的 `IDLE_BACKGROUND_TASK` 调 `_handle_interrupt()` ⇒ `IDLE_BACKGROUND_TASK→INTERRUPTED→IDLE`，即本批刻意避免的"误伤别人相位"由**异常**路径触发（取消路径已由 ownership 判据挡住）。**Batch 14 已确证（E2，真 Agent + 生产入口）**：可达性 = **INTERNAL/LOW**——需要合取「仍有活跃后台任务」（经生产入口 `WorkflowEngine.submit` 委派产生）＋「下一回合 pre-commit 撞上 `StorageUnavailableError`」（`loop.py:578 _identity_snapshot`，Mongo 离线 fail-closed 的生产形态；`CharacterBusyError` 在 commit point **之后**（`:692-694`）⇒ 不在该窗口）。**可观测后果**：`interrupted`+`idle` 两帧上线、UI 状态点、`is_completely_idle` 提前变真（IdleMonitor 主动搭话闸门 / 语音取消闸门 / `wait_until_idle` / `_drain_pending_after_turn` / `/health` 的 fsm 字段）——**后台任务不被取消、用户输入不丢、不失聪**。取消臂在同一窗口**保持** `IDLE_BACKGROUND_TASK`（`loop.py:717-720` 的设计意图），异常臂与之**直接矛盾**（同一条 ownership 判据只装在取消臂上）。**产品合同 = (B) 保留后台、失败后回到它**（`protocol.py:51`＋`schema/agent.py:21`＋`loop.py:1432-1437` 每回合重算）。**修法方向**（未做）：`finally` 里的 canonical 收口改用与本回合相同的 ownership 判据（ctx 非 None 才收口），并给 `_cancel_desktop_calls` 补回合归属。证据 `tmp/b14/subB/probe_kb29.py`（2 例）| INTERNAL | LOW | E2(10)+**E2(14)** | **OPEN**（本批只 triage；不与 KB-47 同 root）| 零散 |
| **KB-30** | **Batch 10 Challenger（O3）**：取消在回合开始之前传播时，`_prepare_turn` 已写入的回合级状态（`_session`/`_prompt`/tool_context/`_tts_mgr.reset_for_turn`/`_clock`）**不回滚**；`_clock` 会残留成非 None（消费方都有"本回合会重设"的前提 ⇒ 当前无害）。"取消即无痕"不成立 | INTERNAL | LOW | E2(10) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` | 零散 |
| **KB-31** | **Batch 11 Phase 1 新发现（K1），已随同一 fix 关闭**：取消（**第一发**）落在 **think 路径** `_handle_turn_exception` 的 await（`turn_failed` 广播 / `generation.mark`）上时，取消从 `except Exception` 块里逃出 `process()`——在 `except` 块里抛出的异常**不被兄弟 `except` 子句接住**（本机实测 Python 3.12.13：`(a) 同一 try 的兄弟 except → escaped` / `(b) 外层 try 的 except → caught-by-outer`）⇒ 相位永停 THINKING，而 `run_state` 仍是 **RUNNING** ⇒ **用户可见的永久失聪**（实测：第二条 `accepted=True` 但 `被处理=False`、`pending=['第二条']`）。生产触发 = provider 失败/内部错误收尾时用户按停止按钮（或 VAD/断线/语音取消/切模型）。**为什么 prepare 路径没有而 think 路径有**：前者的 handler 调用点位于 Batch 10 的外层取消边界**之内**（外层 try 接得住），后者的 `except Exception` 分支**没有任何外层 try**。**修法**：分类+广播+mark 进 `try`、`await self._handle_interrupt()` 进 `finally`（`b56a8442`）。**守卫**：`core/cognitive/tests/test_cancellation_safe_finalization.py::test_cancel_during_error_finalization_settles` + `core/agent/tests/test_turn_finalization_cancellation_safety.py::test_stop_button_during_error_finalization_does_not_deafen_agent`，独立 worktree `@f62e2468` 实测 **2 failed → 2 passed**；变异 **M4**（只拿掉这个 finally）恰好杀死这两条，而 M2/M3 杀不死 ⇒ 两半修复各自必要。**canonical 分类 = `SAME_ROOT_DISTINCT_REACHABILITY`**：同 root / 同 owner / 同 invariant / 同 fix，但**不并入 KB-25**——§36 要求"同一 regression guard 基本可覆盖"，而两者守卫**互斥**，可达性也相反（KB-25 严格形态 STOPPED/不可见；KB-31 是 RUNNING/用户可见） | **USER** | **HIGH** | **E2(11)** | **GUARDED** `b56a8442`（同一 fix） | ✅ Batch 11 |
| **KB-32** | **Batch 11 审计**：`scripts/check_timeout_budget.py` 只盘**具名常量**、且明确拒绝跨模块调用图推断 ⇒ `_turns.drain(timeout=3.0)`（`core/agent/lifecycle.py:320`，裸字面量）与 `_CHANNEL_SEND_TIMEOUT=5.0`（`Module/output/manager.py:40`）的"外层 3.0 < 内层 5.0"不对称对门禁**不可见**——正是 S2-A3 那条规则要防的形状。后果：一个不读数据的 WS 客户端可让单帧占满 5.0s > 3.0s drain 预算（停机时稳定命中第二发取消）。**本批不改任何预算常量**（§86/§2 scope），只登记 | INTERNAL | LOW | E1(11) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` | 门禁（与 KB-27 同族） |
| **KB-33** | **Batch 11 审计**：`FSMBackedPhaseController.finish_turn` 的 **12 组合里 5 组抛** `ValueError`（`bg=True` × IDLE/EXECUTING/INTERRUPTED、`bg=False` × EXECUTING/IDLE_BACKGROUND_TASK）——只有**第一次** transition 有 `can_transition` 守卫（`fsm.py:212-213`），第二次（`:214`/`:225`）无条件；且**零 gated 覆盖**：12 组合矩阵只存在于 `tmp/b9_fsm/`，而 `pytest.ini::norecursedirs` 排除 `tmp` ⇒ `make test-quick/normal/full` 一条都跑不到。当前 5 组均**未经 `process()` 可达**（除 `reset + 活跃回合 + bg=True`） | INTERNAL | LOW | E1(11) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` | 零散（覆盖债） |
| **KB-34** | **Batch 11 副产物**：第二发取消若恰好落在 `_cancel_desktop_calls()` 的 `bridge.pending_calls()` 上，在途 desktop 调用**不会**被逐条 `cancel_call`（属 §5 B「best-effort 资源清理」，放弃是允许的）；兜底只有**启动期** `DesktopCallRepository.mark_stale_expired` ⇒ 运行期长窗口内这些调用会一直挂在 `pending`。**明确不修**：为它加 shield/timeout 会把资源清理抬成逻辑义务，违反本批 CF4 的分层 | INTERNAL | LOW | E2(11) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` | 零散 |
| **KB-35** | **Batch 11 Challenger（H）**：`handle_interrupt` 的 `try` 起点在 `transition(INTERRUPTED)` + `bus.post(INTERRUPTED)` **之后**，且 `finally` 体内任一步抛异常会**顶替**在飞的 `CancelledError`（任务从 cancelled 变 failed；抛点在 `transition(IDLE)` 之前时相位会留在 INTERRUPTED）。实测 `[H-notify] cancelled=False exc=ValueError`、`[H-transition] fsm=interrupted`。**当前不可达**：`_notify` 逐订阅者吞 `Exception`、`bus.post` 是无界队列 `put_nowait`、护栏保证 `transition(IDLE)` 合法。已写进 `invariants.md` §4.4 已知边界——**将来在 `finally` 里新增任何可能抛异常的语句时必须重新评估** | INTERNAL | LOW | E2(11) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE`（CURRENTLY_UNREACHABLE） | 零散 |
| **KB-36** | **Batch 11 Challenger（C/J-interrupted）**：**并发**第二个 `handle_interrupt()` 调用者会重复广播——`transition(INTERRUPTED)` 在"已是 INTERRUPTED"时是 no-op，但 `bus.post` 照发 ⇒ `interrupted` ×2、`idle` 通知 ×2。即幂等口径**只**对"IDLE 入口（收口完成后再调）"成立。**当前不可达**（唯一调用者是持 `_process_lock` 的回合任务）；若将来引入第二个调用者（IdleMonitor / Agent 层打断），必须先把判据从"IDLE 早退"升级为"完整 terminal 状态判据" | INTERNAL | LOW | E2(11) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE`（CURRENTLY_UNREACHABLE） | 零散 |
| **KB-38** | ~~**Batch 12 审计（FALSIFICATION_GUARD 钉住）**：**连发三条时 pending 内的既有乱序**——上一条消息还排在 **EventBus 队列**里（事件消费者尚未把它交给 `_handle_user_input`）时，`process_message` 因 `bus.pending_user_inputs > 0` 把**新**消息直接 append 进 `_pending_messages`；随后消费者才把**旧**消息经 `_handle_user_input` 也 append 进去 ⇒ 两条在 pending 里**反序**（实测 `['M3','M2']`）。与 KB-24 同族（「事件在总线里排队时尚无 pending 归属」）但**不同根**：这里两条都**有** owner（pending），只是次序错；修法前后行为一致（**非** Batch 12 引入）。修它要动 `process_message` 的入队判据与 pending 语义~~ —— **Batch 13 已确证并修复**（严重度补记：顺序 LOW／失活 HIGH）。**机制（三层）**：① **两个 append 权威按写入时刻排序**（`process_message` 在 admission 时刻写、`_handle_user_input` 在**派发**时刻写）；② **等待集只数了一半**——"在等"的形态有 `_pending_messages` ∪ **总线驻留**两个容器，只看前者会在"队首已 `pop(0)` 交给总线、尚未被 claim"的交接窗口里**失明**（更晚的输入在那里能拿到更小的总线 `seq`），直投输入（STT/voice，`ws_handler.py` / `ws_v2.py` / `voice_sessions.py`，**不经** `process_message`）尤其容易触发；③ **活性缺口**：pending 非空没有自己的触发器——proactive（`_handle_system_tick`）与 task-result 回合此前**不带 done callback**，回合在窗口内失败且从未离开 IDLE 时两个出队口都不触发 ⇒ 已 accepted 的输入**永久静音**（修前靠"新输入起回合顺带排空队列"自愈，②的判据会把它也挡住）。**实测（E2，五层证据：admission/pending/claim/session/LLM）**：修前 admission `[M1,M2,M3]` → claim/session/LLM `[M1,M3,M2]`；交接态反例 claims `[M1,V1,M2]`；proactive 停滞 `pending=['T1'] claims=[] fsm=idle`。**修法（最小，两个文件）**：① 入队收敛到唯一权威 `_enqueue_pending`（按既有 `int(request_id)` admission 键顺序插入）；② 越队判据 `_has_older_waiting_input` 看**完整等待集**（pending ∪ 总线驻留），`process_message` 与 `_handle_user_input` **共用同一定义**；③ 兜底出队挂到**所有**回合 spawn 点（`_on_auxiliary_turn_done`，复用既有 `_drain_pending_after_turn`）；④ `EventBus.pending_user_input_request_ids()` **只读**快照（**不改**优先级/派发/drain）。**明确不改**：EventBus 优先级与 drain 语义、pending 容器、容量策略、FSM、`_process_lock`、wire 契约、输入层三条直投入口。**canonical 更正**：根判据 pre-existing（`a4f590ab` 2026-08-10 "review 复核 #4"），但**可见性由 Batch 12 的判据放宽打开**——原文"修法前后行为一致（非本批引入）"是**推断而非测量**（本批 worktree `2e70854d` 实测：该形态走抢占分支、M1 被吞，`pending=[]` 无反转）。**守卫**：`core/agent/tests/test_pending_input_ordering.py` 13 条（8 条 PRE_FIX_RED）＋ 改写 Batch 12 的 FALSIFICATION_GUARD 为正确顺序断言；变异 M1–M10 各被不同守卫杀死（M7 只留 pending / M9 快照恒空 → 交接态守卫红）；两轮独立 Challenger（A 一轮反证交接态、二轮反证停滞，**均已修复并配守卫**；B 2400 轮随机化 + 边界 40 组 HELD）。不变量 **PO1–PO6 ＋ PO-L** 见 `docs/architecture/invariants.md` §4.6 | **USER** | LOW（顺序）／HIGH（静音） | E2(13) | **GUARDED** `fix(agent): preserve pending input admission order` | ✅ Batch 13 |
| **KB-39** | **Batch 12 审计**：**`accepted` 语义没有书面合同**——契约 schema 与 `electron-backend-protocol-v2.md` 只描述 happy path（「accepted → 后续 generation.* 事件」），没有任何地方说明它承诺到哪里；`server_info.features.idempotency_durable` 读起来像「消息已持久化」，实际只覆盖幂等 claim（TTL 1h）。客户端因此无从判断「多久没收到 `generation.*` 就该认为这条丢了」 | INTERNAL | LOW | E1(12) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` | 文档 |
| **KB-40** | **Batch 12 审计**：**客户端没有「accepted 但从未处理」的终态**——Electron 在 ACK 上就 settle（`elysia-frontend` 的 `handleAck`），WebUI 根本不消费 ack。Batch 12 的终态只存在于服务端日志 ⇒ 用户面仍无法区分「被取消」与「从未受理」（这是显式 terminal disposition 的**已知弱化**，本批刻意不新增 wire 帧） | USER | MEDIUM | E1(12) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` | 前端/契约 |
| **KB-41** | **Batch 12 审计**：**`_handle_task_result` / `_handle_system_tick` 的抢占没有 ownership 判据**——与 `_handle_user_input` 不同，这两处直接 `cancel_current()`，可抢占一个**尚未接管输入**的用户回合（后台任务终态唤醒 / 主动搭话 tick 落在 admission 窗口上）。当前窗口很窄（tick 门是相位 IDLE ＋ 1s 轮询；task result 需后台任务恰好完成） | INTERNAL | LOW | E2(12) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` | 零散 |
| **KB-42** | **Batch 12 审计**：**语音取消的门排除了 admission 窗口**——`voice_sessions.py::cancel_session` 要求 `not is_completely_idle`，而窗口内相位恰是 IDLE ⇒ 语音取消在窗口内**不触发**。当前无害，但该门是「相位代理」而不是「是否有在途回合」，语义与真实意图有偏差 | INTERNAL | LOW | E2(12) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE`（CURRENTLY_UNREACHABLE） | 零散 |
| **KB-43** | **Batch 12 审计**：**post-ACK 容量竞争的处置缺产品定义**——`_handle_user_input` 排队分支满时释放 work 后丢弃（Batch 12 补了 WARNING 归因，修前连日志都没有）；但「已 accepted 的项是否该优先插回 / 是否该形成一条 rejected 帧」没有产品级决策 | USER | LOW | E2(12) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` | 产品决策 |
| **KB-44** | **Batch 12 审计**：**v1/REST 输入的终态归因身份为空**——`_admission_message_id` 取自 v2 预分配的 `messageId`；v1/REST/STT 路径没有这个 ID ⇒ 终态日志只有 `conversation_id`。身份最弱的地方恰是最需要归因的地方 | INTERNAL | LOW | E1(12) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` | 可观测性 |
| **KB-45** | **Batch 12 审计**：**`_handle_user_input` 的抢占分支（`cancel_current()`）当前不可达**——`process_message` 只在相位 IDLE 时才 `bus.post`，而相位离开 IDLE 之后回 IDLE 的同一同步段里就会发出下一个回合 ⇒「相位 IDLE ＋ 有存活且在跑的回合」这一前提只在 KB-28 那条需要挂起订阅者的窗口里成立。Batch 12 **保留**它作为租约/孤儿任务的失效保护，并在注释里写明它**不是**用户级中断机制（中断走 INTERRUPT 事件） | INTERNAL | LOW | E2(12) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE`（CURRENTLY_UNREACHABLE） | 零散 |
| **KB-46** | **Batch 13 新发现（原 N7）→ Batch 14 已确证**：**"已 claim、进历史前被抢占/取消"的用户输入在会话历史里彻底不存在**。窗口 = active ownership commit（`_generation_ctx` 建立，`loop.py:655/671`）→ `session.add_message`（`loop.py:1021`）；窗口内**真挂起点 3 个**：`enter_thinking` 的 `_notify`、`generation_registry.register`（Mongo upsert）、`_build_turn_prompt → build_messages`（Milvus ANN + 12 段 prompt 渲染，假存储下限实测 **32.7 ms**）。落点决定处置：A/B 段（`_prepare_turn` 之内）有 `disposition=cancelled_pre_ownership`；**C 段（`_think_loop`→`_build_turn_prompt` 之内）零归因**，日志只剩 `<<< 用户打断`（Batch 12 的 AD2 只修了前半段）。触发入口：三条**直投**输入（`ws_handler.py:733` STT / `ws_v2.py:787` voice.commit / `voice_sessions.py:702` REALTIME——它们不过忙判定，等待集为空时直接抢占）；**停止按钮/任何 INTERRUPT 落在同一窗口同样丢且无归因**（更宽的触发面）。**实测（进程内确定性 E2，屏障装在 `PromptAssembler.build_messages`）**：session `['（暖场）','第二条']`（第一条不存在）、`message_archive` 同样没有、后续两条 LLM prompt 均不含它、该 generationId 上只有 1 条 `generation.failed`（**无** `generation.started`）、`cancelled_pre_ownership` 日志为空；v2 形态下 generation 记录 `status=cancelled` 而 session 的 messageId 集合里没有它。**用户可见后果 = 静默丢失 + 持久化的会话真相错误**：前端乐观渲染过这条消息、刷新/重连时 `session` 回放整体替换 store ⇒ **消息消失**，并且**后续所有回合的 LLM 上下文都没有它**；v2 同 `clientRequestId` 重试只被逐字重放 `accepted`，协议给的"换新 clientRequestId 重发"恢复路径也失效（回放里根本没有这条）。**合同判定**：`session.add_message` 是"用户真的发过的每一条"（唯一豁免是 `is_task_result` 合成唤醒；紧邻注释"此后用户至少能在历史里看到'我说过这句'"），(C)"只记完整回合"被 `_archive_partial_reply` 与"append 早于 LLM 首轮"反证 ⇒ 现状是**合同 (A) 的破口**，不是设计内边界。**为何本批不修**（**已按 Challenger 反证更正**）：① §50 把本批 production 范围硬性限制为**一个** root-cause cluster，而 KB-46 与 KB-47 **不同 root**（§49 禁止因为都叫 ownership 就合并）；② **"claimed ⇒ recorded" 必须贯通两个落点**——Challenger 的 14 行补丁（在 `_think_loop` 的 `except CancelledError` 里 append）**只覆盖 W2**，我独立验证（`tmp/b14/main/probe_w1_w2.py`）W1（取消落在 `_prepare_turn` 的 `register`/Mongo 上，走 `_handle_prepare_cancelled`，那里拿不到消息内容）**仍丢失** ⇒ 同一个 ownership 状态两种会话真相；完整修法 ≈25–30 行（两处共用 helper + `_prepare_turn` 暂存 `(content, message_id)`）。**撤回**原先的 `DEPENDENCY_ON_KB-30`：`session.add_message`（`loop.py:1021`）与 generation 行（`loop.py:675`）本就无事务耦合，KB-30 讲的是 `_prepare_turn` 状态不回滚 / `_clock` 残留，与 append 无关（Challenger 反证 + 14 行补丁实测恢复真相、798 用例全绿，见 `tmp/b14/challenger/n7_minfix.patch`）。**按 §51 优先级 KB-46 高于 KB-47**（USER/HIGH 静默丢失 > DEPLOYMENT/MEDIUM 复活）——本批选择后者的唯一依据是 §50 预算；若 owner 认领不同，回退 KB-47 补丁、以该补丁为起点即可。施工图见 Batch 14 记录 §9 的 Decision。证据 `tmp/b14/subC/probe_n7.py`（5 例）+ `n7_evidence.txt` | **USER** | **HIGH** | E2(14)+**E2(15)** | **GUARDED** `e5e6e4b5`+`dbed9856`+`2f51b9c7` `TRUTHFULNESS` | ✅ Batch 15 |—— **Batch 15 已确证并修复**。**会话历史合同已裁定 = Contract A（用户意图史）**：history 记录「用户真的提交过、且被 runtime 接管过的每一条输入」（实现上是混合语义——user 消息在模型调用**之前**写、assistant 在生成**之后**写；唯一豁免 `is_task_result` 合成唤醒）。反证 B（「只记模型消费过的」）：record 早于 LLM 首轮；反证 C（「只记完成的回合」）：`_archive_partial_reply` 会把被打断的半截 assistant 回复照样归档。**停止按钮语义**：前端只发 `{type:'interrupt'}` 且**不回收** user 气泡 ⇒ 是「停止 assistant 当前生成」，不是「撤销我的消息」（`chatStore.ts::addUserMessage` 乐观渲染 + `restoreFromServer` **整体替换** `messages.value` ⇒ 服务端历史缺一条，用户刷新后自己的消息就消失）。**修法（最小、非事务）**：① `_record_user_input()` 收口为 user 消息进历史的**唯一权威**（normal 与 recovery 共用，消除 W1/W2 两种会话真相）；② `process()` 取锁**之前**的同步段暂存 `_claimed_input_record`（`is_task_result` 直接存 None）；③ `_record_claimed_input()` 在 `_handle_interrupt()` 的**零 await prologue** 一次性消费——**一处覆盖 W1/W2/W3**，且在 `is_completely_idle` 早退之前；三条门禁 = 已 claim / 未 recorded / 是用户输入，幂等判据落在**既有身份** `(session, message_id)`（非新增平行布尔），异常一律吞成 warning。**E2(15) 独立复现**（真 Agent + 真 `start()` + 真 EventBus + 真停止按钮 + 确定性屏障）：修前 `accepted=True / claimed=True / recorded=False` → 取消后**三层皆无**（`history=['（暖场）']`、archive 同样没有、下一回合真实 LLM messages 不含它、相位正常回 idle）。修后 15 例守卫全绿 + 真库 3 例旁证（RM1 内存/archive/`sessions` 三处各恰一条且幂等）。**KB-30 保持独立**：`SessionMemory.add_message` 本就是同步 deque append + fire-and-forget 归档 ⇒ 补写不引入挂起点、零新增 DB 往返、不需要事务/回滚（CF1/CF2 与 shutdown 有界性不回归，`test_ct_t10/t12` 钉住）。**变异矩阵 8 个变异**：M1（移除 recovery）→ 6 条守卫红；M2（取消侧无门禁②/无一次性消费）→ CT-T11 红；M3（去掉「已 claim」门禁）→ CT-T6b 红；M4（**Option A**：把 record 提前到 `build_messages` 之前）→ CT-T4 红（**prompt 双计**，实测 `roles=['system','user','assistant','user','user']` ⇒ Option A 被证伪）；M5（终态链等无界持久化）→ CT-T10 红；M6（只写旁路会话）→ CT-T1/T3 红；M7（去掉身份幂等）→ **存活**（门禁②+一次性消费已足够 ⇒ 记录为冗余防御，非唯一防线）；M8（去掉 `is_task_result` 排除）→ CT-T7 红。Batch 14 的 14 行候选补丁（`tmp/b14/challenger/n7_minfix.patch`）**只覆盖 W2** —— 已由 `probe_w1_w2.py` 与本批 W1 守卫双重证伪，未被采用。执行记录见 `docs/review/2026-09-19-Batch15-claimed-input-conversation-truth-执行记录.md`，不变量 CT1–CT6 见 `docs/architecture/invariants.md` §4.8 |
| **KB-47** | **Batch 13 新发现（原 N8）→ Batch 14 已确证并修复**：`Agent.stop()` 清 `_pending_messages` 却**不清 EventBus**，于是停机那一刻仍驻留总线的已接受输入会在同一实例 `start()` 之后被**当成新输入消费（复活）**：进 session 历史、喂 LLM、可能二次触发工具副作用（用户"没回复就重发"⇒ 同一指令执行两次）。**机制**：`EventBus` 生命周期 = Agent 实例生命周期（`EventBus()` 在 `Agent.__init__`），而 `stop()→start()` 是**新 runtime epoch**（`fsm.reset()` + `reset_lifecycle_state()` + `_started_cognitive_tasks.clear()` + 新消费者；`clear_work()` docstring 自陈"避免同一 Agent restart 残留"）；取消消费者 ≠ 清队列（`asyncio.Queue.get()` 被取消不取走元素）。窗口宽度 = 消费者两帧之间的**真挂起点**：`_handle_state_change → output.notify_state`（逐通道 await，每回合 3~4 次）与 `_handle_task_result → `_mark_task_result_delivered``（≤2s Mongo 写）。**既有合同早已写明**（`invariants.md` §4.6 PO5 + 已知边界"停止按钮与 `Agent.stop()` 清空 bus + pending 是既有终态合同"、§4.5 AD5"停机不得复活"、输入层 S2-R2-4"残留事件跨运行被消费本身就是缺陷族"）——**代码只做了 pending 那一半**（静态：`stop()` 函数体 AST 零引用 `_bus`）。**实测（真 `Agent.start/process_message/stop/start` + 真 EventBus，消费者钉在 `notify_state`）**：修前 `stop()` 后 `pending_user_inputs=1`、`start()` 后该输入进历史并被喂 LLM（`llm_calls 1→2`）、**零 disposition**；独立复现（另一探针，6 例）同样 6 passed→修后 2 failed 并配 no-op 对照锁死 before/after。**修法（最小，2 文件）**：① `EventBus.discard_user_inputs()`——`drain()` 后**按 event type 分类**，只丢 `USER_INPUT`、其余按原 `priority/type` 重投（同步、无 await ⇒ 不交错、不改幸存者顺序）；② `Agent.stop()` **最后一步**调用 `_discard_abandoned_user_inputs()`（放在最后是为了同时覆盖停机窗口内输入层残留投递；**不能**放 `start()` 侧——`start()` 在 spawn 消费者之前就已 `_state=RUNNING`，那期间的输入是合法的新 epoch 工作，会被误伤），逐条记 `disposition=cancelled_by_shutdown` 并释放 v2 session-work 引用；③（Challenger 反证补全，同一 cluster）`stop()` 清空 `_pending_messages` 此前**零归因**（循环里没有日志，而停止按钮路径对同一类处置记 `cancelled_by_interrupt`）——这是本族**唯一生产可达**的静默丢弃形态（回合进行中连发两条 + `/restart`/SIGTERM），现同样记 `cancelled_by_shutdown` + `message_ids`。**硬边界**：durable `TaskResultData` 必须原位保留（负对照实测：一把 `bus.clear()` 会丢掉用户正等着的后台任务终态）——变异 M2"粗暴全清"被该守卫 + 4 条 EventBus 单测杀死。**明确不做**：不引入 `RuntimeEpoch` 框架/字段、不做 restart 侧过滤、不改 FSM/pending/lock、不加 watchdog/polling、零 wire/schema 变化。**残留（已登记 KB-48）**：清扫之后才投进总线的事件仍会跨 epoch 存活。**AR-8 关系**：AR-8（"EventBus lifecycle epoch 旧 epoch 事件潜在重放，风险低"）是被以"stop 已 cancel event task"为论据结案的——该论据被本批复现**证伪**；KB-47 是 AR-8 抽象下的**首个具体可复现实例**，AR-8 保留为父项并改写缓解说明。不变量 **RO1/RO2** 见 `docs/architecture/invariants.md` §4.7。**可达性研判**：默认生产入口 `/restart` **重建 Agent 实例**（旧 bus 随实例丢弃）、`/shutdown`/SIGTERM 结束进程 ⇒ 本缺陷的复活形态**不是默认用户路径**，而是**受支持的原地重启模式**（`run_server(rebuild_on_restart=False)`，仓内无生产调用方；但 `chat.py`/`voice_sessions.py` 的 S2-R2-4 防守与ARCH-INC-P2-03 的"同一实例 stop→start"都以它为前提）⇒ 定级 **DEPLOYMENT/MEDIUM**，按 **Gate C**（与已确认的 CR-08/S2-R2-4 同一缺陷族、修复不可合理拆分：只修任一半都留下复活路径）落地 | **DEPLOYMENT** | **MEDIUM** | **E2(14)×2 独立探针** | **GUARDED** `fix(agent): discard abandoned user input across runtime restart` | ✅ Batch 14 |
| **KB-48** | **Batch 14 新发现**：**输入层两条直投入口在停机窗口仍向 bus 投递**——`core/input/ws_handler.py::do_transcription`（`:733`）与 `core/input/ws_v2.py::_handle_voice_commit`（`:787`）**不查** `status.is_service_running`，而另两条同类入口已按 S2-R2-4（`c029110e`）加了该判据（`ws_handlers/chat.py::do_interrupt`、`voice_sessions.py` REALTIME）。后果：RO1 的停机清扫发生在 `stop()` 最后一步，**清扫之后**（停机尾部 / `runner.cleanup()` 期间）这两条入口仍能把 `UserInputData` 投进总线 ⇒ 仍可跨 epoch 复活，与 S2-R2-4 自陈的"停机窗口内输入层不再向 bus 投**任何**事件"不一致。与 KB-47 **同族但不同权威**（输入层准入 vs. Agent 停机边界）⇒ 按"一个 root-cause cluster"约束本批**不合并修复**。修法方向：两条入口补同一`is_service_running` 判据（`voice.commit` 用既有 `_not_accepted_error(agent)` → `AGENT_BUSY`，**零新增 wire 枚举**）| INTERNAL | LOW | **E2(14)**（Challenger 实测：`stop()` 返回后真 `do_transcription` 仍能投进总线并被 `start()` 消费进 session/LLM） | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` | 零散 |
| **KB-49** | **Batch 14 新发现（随 KB-29）**：`CognitiveLoop._cancel_desktop_calls()`（`loop.py:1451-1464`）遍历 `bridge.pending_calls()` 时**没有回合归属判据** ⇒ pre-ownership 的 canonical 收口（`_handle_turn_exception` 的 `finally: _handle_interrupt()`）会取消**上一回合**仍在途的 desktop 调用（KB-29 的放大面：不只 FSM 相位，还有真实的在途调用被误撤）。当前需要 desktop 桥 + 在途调用才可见（探针无桥 ⇒ 实测 no-op）| INTERNAL | LOW | E1(14) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` | 随 KB-29 |
| **KB-50** | **Batch 14 新发现（AR-8 残余面）**：除 `USER_INPUT`（KB-47 已修）之外，`StateChangeData` / `SystemTickData` / 陈旧 `InterruptData` 同样会跨 `stop()→start()` 存活并被新 epoch 消费（实测：残留 `StateChangeData`/`SystemTickData` 在 `start()` 后被读出）。其中**陈旧 `InterruptData` 最值得关注**：新 epoch 消费它 → `_handle_interrupt` → `bus.drain()`，会把新 epoch **刚投进总线**的事件一起清掉（当前无生产实例，属潜在地雷）。另：同容器里的 durable `TaskResultData` 跨重启会被**重复交付**（同 task_id 的宣告回合跑 2×），而 `_handle_task_result` 的 `cancel_current()`（KB-41）会因此取消兄弟回合——at-least-once 本身是 T5/T7 已接受的语义，但"重复副本杀兄弟回合"是新的交互面 | INTERNAL | LOW | **E2(14)** | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` | 触发式 |
| **KB-51** | **Batch 15 新发现（三层 truth 不同构，既有语义）**：`SessionMemory.add_message` 同一步写**内存 deque**（经 fire-and-forget 任务写 `message_archive`），但 `sessions` 文档只在**成功回合的 after_response**（`loop.py` 的 `save()`）/ 关机 `save_all()` / 逐出时全量 `$set` upsert。⇒ **中途取消的回合，内存与 archive 有内容而 `sessions` 文档没有**；而 `load()` 只读 `sessions` 文档（不读 archive）⇒ 若进程在此之后被**硬杀**（非 graceful stop），重启后该回合从 LLM 上下文里消失（archive 仍在，但无消费者回填）。**Batch 15 的 CT1 保证范围明确不含 crash durability**（与 §4.5 的 `accepted ≠ durable` 同一条合同），本批的 recovery 也不改变这条时序（真库旁证 `test_rm1` 显式 `save()` 后才断言）。**归属**：与 KB-46 **不同 root**（持久化时序 vs. claim→record 缺口），本批只登记 | INTERNAL | MEDIUM | E2(15) | **OPEN** `TRUTHFULNESS` | 小批 |
| **KB-52** | **Batch 15 新发现（`is_task_result` 豁免只在 user 侧）**：合成后台任务唤醒回合实测 `roles=[USER, ASSISTANT, ASSISTANT]`——即**有 assistant 消息而无对应 user 消息**（唤醒文案由 `_prepare_think_input` 生成，不经 `add_message`）。它与 **F13**（「系统注入文本被当作『用户消息』参与记忆提取」）**方向相反**：主动搭话（`_handle_system_tick`，`is_task_result=False`）**会**被写进 user 历史，而任务唤醒**不会** ⇒ 两条合成注入路径的会话语义不一致。Batch 15 的 recovery **刻意保持**这一现状（`is_task_result` 在 `process()` 入口就存 None，CT-T7 按**条数**钉住） | INTERNAL | LOW | E2(15) | **OPEN** `TRUTHFULNESS` | 小批（与 F13 同族） |
| **KB-53** | **Batch 15 新发现（wire/UI 没有「已取消/不完整」表示）**：被打断的半截 assistant 回复在 `archive` 里带 `metadata={'incomplete': True, 'reason': …}`（`_archive_partial_reply`），但 **WS 回放项不带 metadata**，且契约层的 `completeness` 是 `Literal["complete"]`（`frontend_contract/ws_message` generation 域）⇒ **刷新后半截回复与完整回复在 UI 上完全无异**；前端也没有任何「本条被取消」的表示（`restoreFromServer` 整体替换）。**归属**：与 KB-46 同族（conversation truth 的用户可见投影），但**需要产品/契约决策**（是否新增 wire 字段），本批只登记、并**明示不新增** `message.cancelled` / `turn_status` / `history_status` | USER | LOW | E1(15) | **OPEN** → `PRODUCT_DECISION_REQUIRED` | 需契约决策 |
| **KB-54** | **Batch 15 新发现（KB-26 的读者清单少一处）**：KB-26 记「`_current_task` 陈旧的生产读者**恰好 5 处**，每处都在 `.done()` 守卫之内」。实测**还有第 6 处**：`core/input/ws_handler.py:522` 的 v2 断线路径读 `agent.cognitive.current_generation_user_message_id`，它**没有** `.done()` 守卫，只靠 `gen_uid in conn.sent_message_ids`（连接归属）兜底。**今日良性**：陈旧 id 只可能属于**上一个已结束**的回合，此时 `cancel_current()` 对已 done 的任务是 no-op（且新回合的 id 不在旧连接的 `sent_message_ids` 里）。⇒ **只更正 KB-26 的清单与论据**（「恰好 5 处」→ 6 处，第 6 处的守卫是连接归属而非 `.done()`），**不改 KB-26 的 KEEP_AS_IS 结论** | INTERNAL | LOW | E1(15) | **OPEN**（cross-ref KB-26） | 小批 |
| **KB-55** | **Batch 15 新发现（未验证）**：`GenerationContext.__post_init__` 可在**赋值之前**抛 `ValueError`（审计字段校验）⇒ `_generation_ctx` 仍为 None，此时 `process()` 的异常分支走「入口之前抛出的异常保持既有语义（原样向外抛）」⇒ **已 ACK 的输入零记录、零归因**。**可达性未验证**（需要构造出能通过 `_prepare_turn` 前面全部步骤、却在 `GenerationContext` 构造期失败的输入形状）。Batch 15 的门禁①以 `_generation_ctx is not None` 为判据，**天然不覆盖**这一形态 | INTERNAL | LOW | E1(15，未验证) | **OPEN** `INSUFFICIENT_EVIDENCE` | 待验证 |
| **KB-56** | **Batch 15 新发现（前端分页游标，未做运行期验证）**：`chatStore.ts` 的 `loadMoreMessages` 打的是**全库** `/api/messages`（`chat_routes.py`），而游标取自回放项的 `_id`（`chatStore.ts`），但 `messages_for_client` **不产出 `_id`** ⇒ 首次「加载更多」可能取到全库最新 50 条（跨会话）。**纯代码推断，未做运行期验证**（审计子代理明确标注）；与 Batch 8（分页完整性）同族但**不同面**（前端游标来源 vs. 后端分页下推） | USER | MEDIUM | E1(15，未验证) | **OPEN** `INSUFFICIENT_EVIDENCE` | 待验证（Batch 8 续） |
| **KB-37** | **Batch 11 Challenger（C）**：`AgentStatus._notify` 是**顺序 best-effort**——逐个 `await` 订阅者，`CancelledError` 不是 `Exception` 故不会被逐订阅者的 `except Exception` 吞掉 ⇒ 某个订阅者在通知上挂起/被取消时，**其后**的订阅者收不到该通知（`wait_until_idle` 若排在后面，会在 agent 已 IDLE 的情况下等到 timeout）。**当前不可达**（仓内唯一 `on_change` 注册者是 `wait_until_idle`，回调无 await，生产调用点只有 `bench` 且自带超时）。属 `_notify` 的**既有**语义，非本批引入 | INTERNAL | LOW | E2(11) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE`（CURRENTLY_UNREACHABLE） | 零散 |
| **KB-18** | **Batch 9 副产物**：`TTSStreamManager._task_by_sid` 会保留**已完成**任务的引用，直到下一次 `stream_sentence` 才剪枝 ⇒ 长会话下该 dict 随句数线性增长（每条仅一个已结束 Task 引用，非任务泄漏、非无界 await；`cancel_stream` 对 `done()` 任务返回 False，语义未失真） | INTERNAL | LOW | E2(9) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` | 零散 |
| **KB-19** | **Batch 9 Challenger**：`flush_emojis()` 在迭代 `_pending_emojis` 时若被并发 `pop`/入队会 KeyError / 提前广播 / 静默丢弃。**今日不可达**（唯一调用者与 `stream_sentence` 同一协程；`set_enabled(False)` 的 flush 窗口里 `stream_sentence` 被 `_enabled` 挡掉） | INTERNAL | LOW | E2 构造 / E1 可达性 | **CURRENTLY_UNREACHABLE** `NEW_FINDING_OUT_OF_SCOPE` | 不排 |
| **KB-20** | **Batch 9 副产物**：`_download_and_forward` 的 `except Exception` 吞掉**输出侧**任何异常（含 `AttributeError` 之类编程错误）只记 warning ⇒ 测试替身缺方法时会静默变成"没转发"（S2-G3 曾因此写出假绿用例）。生产侧同样无法把"输出后端坏了"与"这句本来就该跳过"区分开 | INTERNAL | LOW | E2(9) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` | 零散 |
| **KB-21** | **Batch 9 语句澄清**：SPEAKING 的语义是「**服务端正在输出本轮 response**」，与"用户实际听到什么"无关（客户端播放结束不被追踪）。当前有意为之（Batch 9 §3/§57 已落纸到 `docs/architecture/invariants.md` §4.2），但对 UI 指示/打断策略的消费方不显然——建议在契约注释里显式写明 | INTERNAL | LOW | E1 | **OPEN**（文档） `NEW_FINDING_OUT_OF_SCOPE` | 顺手 |
| **KB-2** | LLM 有时不输出 `<mood>`（回退关键词），deepseek-v4-flash 结构化输出不稳定 | USER | LOW | E3 | **OPEN** | prompt 小修 |
| **KB-4** | VAD 易被环境音触发（aggressiveness / 降噪） | USER | LOW | E3 | **OPEN** | 调参 |
| **KB-10** | TTS 取消 + HTTP 流清理交互可能泄漏（`Module/tts/gpt_sovits.py`，2026-06-22 记录） | INTERNAL | LOW | E1 | **OPEN** | 零散 |
| **KB-11** | **Batch 6 Challenger F-1**：`CharacterService._retarget_default_character` 的重试循环用 `user_identity_store.get()`（**缓存**）取 revision，而冲突路径刻意不刷缓存 ⇒ 权威模式下并发改档案时 3 轮重放同一个 expected、必然 `RevisionConflictError`（实测 observed = `[(1, False, 2)]*3`，fail-closed 无数据损坏，但归档默认角色会假失败）。本批新带的 `outcome.current_revision` 在这条路径**无消费者** | INTERNAL | LOW | E2(6) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` | 零散（改角色归档写链） |
| **F17** | 可选节点失败时任务仍报 `COMPLETED`（文案同时含「已完成」与失败列表）——引擎终态定义问题 | INTERNAL | MEDIUM | E1 | **OPEN** → `PD-6` | 需语义决策 |
| **F3** | `plan_too_large` 语义重构（需新枚举值 ⇒ 触契约可达性门禁） | INTERNAL | LOW | E1 | **OPEN** → `PD-5` | 需契约决策 |
| **KB-12** | **Batch 7 Challenger W1/W2**（预览本地窗口判定的误差带）：`estimate_tokens` 是字符启发式，**对 CJK 偏高、对 JSON/代码偏低** —— 实测 est 8329 vs qwen 真值 7921（偏高 ⇒ 可能**误拒**一个勉强装得下的预览）、est 7302 vs 真值 12212（偏低 ⇒ 在强制窗口 provider 上仍可能 400 → 503）。修它需要真 tokenizer（§15/§16 明示不在 Batch 7 范围），且该 estimator 是 chat 与预览的共享底座；**另一条实测约束**：本机 ollama 0.24.0 在某次 9000 字请求上返回了 **usage 全 0**（回复却正常）⇒ provider 侧计数并非总可得，不能当唯一权威 | USER | LOW | E2(7) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` | 需 tokenizer 决策 |

### 1.3 Runtime / Observability（TRUTHFULNESS 主表，见 §3）

| ID | 标题 | Reach | Sev | Ev | Status | 候选批次 |
|---|---|---|---|---|---|---|
| **N-1** | 同上（readiness 假绿） | DEPLOYMENT | HIGH | E1+E3+**E2/E3(4)** | **GUARDED** `82b80e2e` | ✅ Batch 4 |
| **N-11** | 同上（吞异常 ⇒ 不可观测） | DEPLOYMENT | MEDIUM | E1+**E2(4)** | **GUARDED** `fc648e20` | ✅ Batch 4 |
| **N-3** | 同上（409 谎报 revision） | USER | MEDIUM | E1+**E2+E3(6)** | **GUARDED** `616e4993` | ✅ Batch 6 |
| **N-10** | ~~`gemini/<model>` 预检全绿，运行期 `LLMFactory.create` 无条件 `NotImplementedError`（LLM 侧无 stub 冻结表，STT 侧有 `_STT_STUB_PROVIDERS`）~~ | DEPLOYMENT | MEDIUM | E3(3.5)+E1(HEAD)+**E2(5)** | **GUARDED** `c70312de` | ✅ Batch 5 |
| **F6** | ~~`/api/providers/test` 的 **STT 缺省模型读 config**，而 llm/tts 读 runtime ⇒ 测的不是跑的那个~~ | ADMIN | MEDIUM | E1+**E2(5)** | **GUARDED** `7ef3202b` | ✅ Batch 5 |
| **F7** | ~~`/debug` 的 `tts_ok = agent.tts is not None`（存在性），而 `stt_ok` 是真实连通标志 ⇒ 面板混用两种语义~~ —— **两种语义已收敛到运行期句柄**；"存在性冒充可用"那半经复核**不成立**（构造期 capability 门控 + capabilities `available` 本就用同一判据）→ `FALSE_POSITIVE` | ADMIN | LOW | E1+**E2(5)** | **GUARDED** `87cc7e38`（混用语义）+ **`FALSE_POSITIVE`**（存在性半） | ✅ Batch 5 |
| **F5** | workflow 模型 reporter 缺（debug 只报主模型 `llm_model`，不报 task/workflow 用的模型） | ADMIN | LOW | E1 | **OPEN** | Batch 4-B 可选 |
| **OBS-1** | 间歇性 xdist worker 死亡（8 轮 soak 中 1 次，且那轮被并发改动污染） | INTERNAL | LOW | E0 | **INSUFFICIENT_EVIDENCE** | 定向 soak |
| **OBS-2** | `context_window` 六家 provider 数值为**估算值**（只影响 prompt 截断时机，不影响正确性） | DEPLOYMENT | LOW | E1 | **OPEN**（校准） | 零散 |
| **KB-13** | **Ollama runtime context authority mismatch**（旧描述「声明 8192，但实际只有 4096」**不准确，已改写**）：Elysia 用静态 `contextWindow`（`providers.yaml::llm.ollama = 8192`）做 prompt budgeting，而 Ollama 的实际 serving context 是 **runtime allocation**（`num_ctx` / daemon 分配），**不会因为声明 8192 就变成 8192**——声明值 ↔ 运行时分配之间**没有绑定（authority 缺失）**。本机 E3 冷启动 A/B（ollama 0.24.0 / `qwen2.5:3b`；systemd **未配** `OLLAMA_CONTEXT_LENGTH`、service definition 无 `num_ctx` override）：①`ollama stop` 确认卸载后，native `/api/chat` **不传** `options.num_ctx` → `ollama ps` `CONTEXT=4096`、`GET /api/ps` `context_length=4096`；②再次彻底 stop 后显式 `num_ctx=8192` → 两者皆 **8192** ⇒ **静态 model metadata ≠ 当前 Ollama serving window**。后果：可能出现「Elysia budgeting window = 8192 而 Ollama actual serving context = 4096」，且 ollama 超窗是 **HTTP 200 + 静默截断**（不报 400）⇒ **不能依赖 provider error 发现 mismatch**；**preview 与 normal chat 两者都受影响**（人设被悄悄削掉）。同批旧测（6000 字不传 `num_ctx` → `prompt_eval_count=4096`；显式 8192 的 9000 字 → 8192，三截面全 200）也解释了 F8 原登记里的「provider 400」为何在本地 ollama 复现不出来。**注**：本条反驳 `OBS-2` 括号与 `providers.yaml` 注释里的「只影响截断时机，不影响正确性」。**Reach 备注**：词表无 `USER-adjacent` ⇒ 保持 `DEPLOYMENT`，但**用户可见**（人设静默缺失）。证据与设计方向见 [记录](docs/review/2026-09-18-ollama-runtime-context-authority-记录.md) | DEPLOYMENT | MEDIUM | E3(7)+**E3(A/B 冷启动)** | **OPEN**（**已确证**；`CONFIRMED` 非 §1.1 词表值）`NEW_FINDING_OUT_OF_SCOPE` `TRUTHFULNESS` | owner 决策（`PD-12` 语义 + 出口 `AR-16`） |
| **F18r** | Mongo 补投对坏文档：永久 `delivered=False` + 每次启动告警，**无人工清理入口** | ADMIN | LOW | E1 | **OPEN** | 零散 |
| **N-6** | `docs/architecture/invariants.md` §7 关于 `test_doc_counts` 的那行已过期 | INTERNAL | LOW | E1 | **OPEN**（文档） | 顺手 |
| **N-7** | `scripts/clean_db.py` docstring 说常量「给测试当守卫用」，实际守卫不读它 | INTERNAL | LOW | E1 | **OPEN**（文档） | 顺手 |
| **F5d** | 运维文档剩余：存储/GC/删除/TTL/restart 说明未更新 | INTERNAL | LOW | E1 | **OPEN**（文档） | 顺手 |
| **F16** | `terminal_payload` 的 `.get(k, default)` 在值为 `null` 时不生效（今天不可达） | CURRENTLY_UNREACHABLE | LOW | E1 | **CURRENTLY_UNREACHABLE** | 不排 |
| **F12** | 失败细节长度上界（仅当未来某 provider 回显长文本才需要） | INTERNAL | LOW | E0 | **DEFERRED** | 触发式 |
| **KB-5** | `_execute_tool` 的 60s 超时需要构造慢工具手工验证 | INTERNAL | LOW | E0 | **OPEN**（测试缺口） | ≡ 归入 `GR-19`（指针，不单独计数） |
| **OBS-3** | `Module/tts/openai_tts.py::test()` 曾记「假阴性」；当前实现已走 `make_test_method(_health_check)`（GET /health <400）⇒ **疑已消解，需一次定点复核**才能关闭 | INTERNAL | LOW | E0 | **INSUFFICIENT_EVIDENCE** | 定点复核 |
| **OBS-4** | Batch 1.6 §12#6 的遗留（`consolidator` + `Cannot send a request, as the client has been closed`）在 HEAD 上**定位不到**：`consolidator.py` 无 `close`、`_apply_service_state` 已在 `core/agent/_lifecycle_services.py:158` ⇒ 记录本身疑似错位，需一次定性复核 | INTERNAL | LOW | E0 | **INSUFFICIENT_EVIDENCE** | 定点复核 |
| **OBS-5** | Batch 1.6 §12#2 后半：把 stt/tts 的 `model`/`enabled` 补进 `/debug/api`（需跑 `make gen-types`）——F7 只覆盖了**标志语义**，字段补齐这半没被任何条目跟踪 | ADMIN | LOW | E1 | **OPEN** | 零散 |
| **OBS-6** | **Batch 6 Challenger J-3**：外部删除权威身份档后（缓存仍热）`PUT /api/me` 连报 409「expectedRevision 已过期」而记录**根本不存在**（`details` 为空、无恢复线索），同状态 `GET /api/me` 仍 200 + 陈旧 revision —— 错误码在撒谎 + 恢复路径未闭环。属 E1 freshness 同族（需要"存在性"判定与 404/409 的契约决策），Batch 6 只做到"不伪造 revision" | USER | LOW | E2(6) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` | 与 E1 同批 |
| **OBS-8** | **Batch 9 副产物**：`core/cognitive/finalize.py::_run_after_response` 在 engine 的 `reflect` 是 `MagicMock`/非协程时记 warning（`object MagicMock can't be used in 'await' expression`）。**测试替身形状问题，非生产缺陷**，但会污染诊断输出（本批每条 lifecycle 用例的日志里都有） | INTERNAL | LOW | E2(9) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` | 零散（测试替身） |
| **OBS-7** | **Batch 8 副产物**：debug 面板 `GET /debug/api/sessions` 仍读 `SessionRepository.list_all()`（**有界诊断读 ≤200**）——它是**另一个 endpoint**（ADMIN 面、无 cursor 参数），不在 P2-01r（USER 面）范围内；但同一"200 变成数据集上限"的形状仍在，超过 200 个会话时面板看不到更早的会话。修它需要 debug 契约加分页（前端 `SessionPanel.vue` 同步改） | ADMIN | LOW | E2(8) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` | 零散 |

### 1.4 Deployment / Config truthfulness

| ID | 标题 | Reach | Sev | Ev | Status | 候选批次 |
|---|---|---|---|---|---|---|
| **N-10** | 见 §1.3（provider 能力假绿） | DEPLOYMENT | MEDIUM | E3 | **GUARDED** `c70312de` | ✅ Batch 5 |
| **DEP-2** | **无数据库迁移机制**：新增集合/索引由 `ensure_index` 自动覆盖，但**删/改已有索引**只能人工 `drop_index`（`ARCH-INC-P1-04` 明确不做框架） | DEPLOYMENT | MEDIUM | E1 | **KEEP_AS_IS**（发生频率低、影响单集合） | 触发式 |
| **DEP-3** | `scripts/clean_db.py` 清的是 **v1** `long_term_memory`，而生产写 **v2** ⇒ 该脚本从不触碰真实记忆；修它 = 破坏范围变更 | ADMIN | MEDIUM | E1 | **PRODUCT_DECISION_REQUIRED**（`PD-7`） | 需决策 |
| **DEP-4** | S2-H9 的**写侧**长度上限未做（owner 选的是运行期截历史 + 定预算） | DEPLOYMENT | LOW | E1 | **PRODUCT_DECISION_REQUIRED**（`PD-11`） | 需决策 |
| **DEP-5** | `config.yaml::database.milvus_token` 带 `secret=True` ⇒ loader 显式跳过 YAML，**该行永不生效** | DEPLOYMENT | LOW | E3 | **OPEN** | 顺手 |
| **DEP-6** | `requirements*.txt` 宽范围声明 + `environment.lock.yml` 精确锁并存；uv 迁移未做 | DEPLOYMENT | LOW | E1 | **DEFERRED** | 触发式 |
| **SEC-1** | **CR-01**：真实 DeepSeek key 仍在公开 remote 历史里且**有效**（owner 裁定暂不处理；最低成本止血=只轮换 ≈10 分钟） | DEPLOYMENT | HIGH | E3 | **KEEP_AS_IS**（owner-deferred） | owner 决定 |
| **SEC-2** | bandit **Low 19 项**已知基线（B603/B607 17 + B112 2）；门禁判据是 Medium+ 为 0 | INTERNAL | LOW | E3 | **KEEP_AS_IS** | — |
| **P2-02r** | 契约层业务长度上限（content/ID/attachments ≤10）+ 附件 bounded batch 查询未做 | INTERNAL | LOW | E1 | **OPEN** | 零散 |
| **DEP-7** | **`sensevoice` 假 WARN**：合法无-key 本地 provider，预检报 `[WARN] sensevoice 没有可用地址 — STT ('sensevoice') 将不可用`，而 `STTFactory.create` **成功**——真默认值在代码里（`Module/stt/sensevoice.py::DEFAULT_BASE_URL`，由 registry 兜底分支应用），`_provider_url_note` 只查 env + providers.yaml（`base_url` 是空串）。WARN 不抬红 ⇒ 假文案而非假拒。修法同 S2-H7 对 gpt-sovits 做过的（把内置默认写进 providers.yaml） | DEPLOYMENT | LOW | E2(5 Challenger) | **OPEN** `TRUTHFULNESS` | 零散 |
| **DEP-8** | **model 级能力假绿**：`tts.model = "gpt-sovits/<不存在的音色>"` 时 `--check` 报 `[PASS] TTS 可用`，而 `Agent._start_tts()` 对该 `ValueError` **fail-fast** ⇒ 预检绿 + 启动崩（与 N-10 同形，在 **model 轴**）。修它需要 model 级解析 API（`is_implemented` 刻意是 provider 级） | DEPLOYMENT | LOW | E2(5 Challenger) | **OPEN** `TRUTHFULNESS` | 零散 |
| **DEP-9** | **缺 SDK 时 preflight 裸崩**：`run()` 在缺 `openai` 的机器上以裸 `ImportError` 结束、无 `结果:` 汇总（`_check_embedding_model` → `Module.embedding` → `openai_compat`）。`_check_pip_deps` 会打印 `[FAIL] 缺失 pip 包: openai` 但 `run()` 不提前结束。本批已保证**自己新增的** import 不增加崩溃点（fail-open + 用例），残留点未修 | DEPLOYMENT | LOW | E2(5 Challenger) | **OPEN** | 零散 |

### 1.5 Persistence / Recovery

| ID | 标题 | Reach | Sev | Ev | Status | 候选批次 |
|---|---|---|---|---|---|---|
| **F2** | reminder **无持久化**：进程重启提醒即丢（用户已收到「已设置提醒」）——修法是 feature 形状（durable store + 启动恢复） | USER | MEDIUM | E1+E2 | **OPEN** | 独立小批 |
| **F14** | durable delivery（WAL + 仓储 + 启动补投）**已由 Batch 1.7 完成**；exactly-once **刻意不承诺**（at-least-once 语义） | INTERNAL | N/A | E3 | **KEEP_AS_IS** | — |
| **RB-1** | Mongo 3 个 write-only 字段（`attachments.sha256` / `background_task_results.delivered_at` / `message_archive.metadata.incomplete`）：未连真实 Mongo 无法证明旧记录可读 | LEGACY_DATA_ONLY | LOW | E1 | **DEFERRED**（`LIKELY_DEAD` 不删） | 真实 Mongo 抽样 |
| **KB-15** | **Batch 8 Challenger G3**：**缺/为 null 的 `updated_at` 的历史会话文档在 keyset 下不可达**（真库实测 `$lt` 对缺失/null 走 type bracketing，与内存 fake 一致：5 正常 + 3 坏文档、每页 2 只走出 5 条就"收敛"）；若某页末尾正好是这类文档，游标会写成 `updated_at=""`，下一页谓词匹配不到任何字符串 ⇒ **该页之后的记录被掐断**。本批只做到"不连坐健康记录"（投影层把非 `str` 归一为 `""`，避免 `None > str` / `max(None, str)` 让整份列表静默退化）+ xfail 钉住。仓内无写入者会产生这种文档（`create/save/ensure_exists` 恒写 `now_iso()`）；修它需数据归一化或查询形态变更（聚合 `$ifNull`/哨兵键） | LEGACY_DATA_ONLY | LOW | E2(8，真库探针) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` |
| **KB-16** | **Batch 8 Challenger F5**：`sessions.session_id` **没有唯一索引**（只有普通 `session_id_1`）⇒ 并发 upsert 可产生同一 `session_id` 的两份文档。修前按 session_id 建字典天然去重（静默吞掉一份）；下推分页后列表会显示两条同 ID 记录——本批在 `SessionManager._merged_page` 里显式去重（保留先出现者 + warning），**未动 schema**（加唯一索引会改变写路径的失败语义，属独立决策） | INTERNAL | LOW | E2(8) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` |
| **N-12** | 存量文档若 `pronouns: ""`，`GET /api/me` 仍 500（契约 `_text()` 拒空串 ⇒ 仓内无写入者） | LEGACY_DATA_ONLY | LOW | E3 | **CURRENTLY_UNREACHABLE** | 不排 |

### 1.6 Architecture Debt

| ID | 标题 | 维护风险 | Ev | Status |
|---|---|---|---|---|
| **AR-1** | `ToolAdapter` 4 个零消费者转发成员（`resolve_llm_name`/`executing_now`/`history`/`capability_status`）收窄公开面 | 中（改公开面需全量回归） | E1 | **KEEP_AS_IS**（`REFACTOR_LATER`，dead-code 清单登记） |
| **AR-2** | `OutputManager._broadcast` 2 处穿透（需窄语义方法） | 中 | E1 | **KEEP_AS_IS**（同上） |
| **AR-3** | 事件消费块 mixin 抽取（Batch 3 登记） | 低 | E1 | **DEFERRED** |
| **AR-4** | provider portability（F10）：切换 provider 的运行时能力未成体系。**`AR-5` 是它的一个实例**（不单独计数） | 中 | E1 | **OPEN**（架构/未来能力） |
| **AR-5** | ≡ `AR-4` 的实例：task LLM provider H3——`TASK_SYSTEM_MODEL` 是**裸模型名**，连接参数按主模型解析 ⇒ 工作流**不能用另一个 provider**（已在 docstring 成文解释） | 低 | E1 | **KEEP_AS_IS**（要跨 provider 需契约决策，属 `AR-4`） |
| **AR-6** | error protocol 大扩展（结构化下传 `error_type`，F15） | 低 | E0 | **DEFERRED** |
| **AR-7** | 包级伪环：`core/prompt/prompt_assembler.py` + `core/memory/consolidator.py`（文件级 DAG，可选惰性化） | 低 | E1 | **OPEN**（可选打磨） |
| **AR-8** | P2-09 残留：EventBus lifecycle epoch（旧 epoch 事件潜在重放）。**Batch 14 更正**：本条原以"stop 已 cancel event task，风险低"结案——该论据被复现**证伪**（取消消费者 ≠ 清队列：`asyncio.Queue.get()` 被取消不取走元素，实测停机后 `pending_user_inputs=1` 并跨 `start()` 复活）。**首个具体实例已确证并修复**（KB-47 的 `USER_INPUT` 半边）；本条保留为**父项**，剩余面（`StateChangeData` / `SystemTickData` / 陈旧 `InterruptData` 同样跨 epoch 存活）**未修** | 低 | E2(KB-47 实例) | **OPEN**（父项；缓解说明已改写） |
| **AR-9** | **N-9**：模块级 import 期 `ProjectPath...resolve()` 常量——**census 9 个常量 / 8 个文件**：`custom_providers.py:16`、`log_setup.py:16`、`image_source.py:51-52`（2 个）、`consolidator.py:34`、`vision/plugin.py:23`、`server.py:51`、`secret_store.py:46`、`memory_reflector.py:32`；另有同形的**类属性** `TokenTracker._pending_file`（`conftest.py` 已把它记为"已知缺口"）。**已证一个实例有害**（Challenger，Batch 4）：`image_source._IMAGES_DIR` 在**读路径**使用，而 ≥4 个测试文件晚设 `ELYSIA_PATH_DATA_UPLOADS_IMAGES` ⇒ 重定向对读路径静默无效（`/Elysia.png` 仍从真实仓库文件生成 data URI）。**无测试断言错误结果**，故严重性仍 LOW；且 N-8 的守卫**只覆盖上传路由**，不含 `image_source`（守卫范围窄于它声称的类） | 低 | E1（image_source 实例 E2） | **OPEN**（其余 8 个仍无已证损害） |
| **AR-14** | `ConfigLoader(env_path=…)` 形参：无生产调用方（Module 是库 ⇒ 无法证明仓外无调用方）。Batch 1.6 §12 R5 另记「加 DeprecationWarning 或删除参数」 | 低 | E1 | **KEEP_AS_IS**（`LIKELY_DEAD` 不删） |
| **AR-15** | Batch 1.5 R4 的两个**一次性 `.env` 加载器**仍在：`Module/_common/env.py::_LOAD_ATTEMPTED` + `Module/config/loader.py` 的 `_loaded`/`ensure_dotenv`（docstring 自陈"入口点 API，库代码不要调用"；现存调用点 `scripts/dev/stage_h_pinned_smoke.py:301`） | 低 | E1 | **OPEN** |
| **AR-10** | 反向依赖残留：`Module/tool/safety.py` lazy `core.utils` + `Module/bench` lazy core（已评估接受 / CLR 豁免） | 低 | E1 | **KEEP_AS_IS** |
| **AR-11** | F11 accepted→consumed 窗口（ACK 证明入 EventBus ≠ 业务已持久化；BE-RISK-01） | 中 | E3 | **KEEP_AS_IS**（已接受的 at-least-once 边界） |
| **AR-12** | 两套 CLI driver：`AgentServer.run_repl`（0 代码消费者，但 8 处文档承诺）vs `app.py::run_interactive`（真实入口，**行为已分岔**：后者有 `wait_until_idle()`） | 低 | E1 | **PRODUCT_DECISION_REQUIRED**（`PD-10`） |
| **AR-13** | F4 残余：`handle_json_message`（≈16 圈 / 80 行）可选进一步拆分（已改为 `_WS_HANDLERS` 注册表薄壳，非阻塞） | 低 | E1 | **OPEN**（可选） |
| **AR-17** | **Batch 8 副产物**：`_local_summaries`（进程内会话摘要覆盖层）**无淘汰无上限**——每页一次 `summaries_by_ids($in)` 的代价 = 本进程创建/改名/恢复过的会话数，且覆盖层条目永不因持久化确认而清除。当前规模（个人应用进程生命周期内的新建/改名）下可忽略；若单进程长期运行并大量建会话，需"持久化确认即淘汰"或按窗口裁剪 | 低 | E1 | **OPEN**（性能/内存，非正确性） |
| **AR-16** | **Ollama Provider Adapter**（`KB-13` 的架构出口）：当 provider 的 runtime semantics 明显偏离通用 OpenAI-compatible 行为时，应由 provider-specific adapter 收口，而不是把 `if provider == "ollama"` 分散到 preview / CognitiveLoop / preflight / debug 等上层；目标形状 `LLMAdapter → LLMFactory → {OpenAICompatibleBackend, GeminiBackend, OllamaBackend}`（**不是**"每个 provider 都要独立 adapter"，只有 semantics 差异足够明显才拆）。预期职责：request/wire translation、`num_ctx`、`keep_alive`、Ollama-specific runtime options、**canonical Elysia context budget → Ollama runtime context 落实**、`prompt_eval_count`/`eval_count` → 通用 usage 归一、silent truncation / runtime diagnostic 语义、可选 `/api/ps` diagnostics（**不得**成为每次 generation 前的强制 probe；正常路径保持 1 user generation → 1 provider request）。**与 `AR-4`/`AR-5` 不同轴**（那两条是"切换 provider 的运行时能力"，本条是 provider-specific semantics 的收口点） | 中 | E1（动机为 `KB-13` 的 E3） | **DEFERRED**（不插进当前 correctness batch；理由与触发条件见 [记录](docs/review/2026-09-18-ollama-runtime-context-authority-记录.md) §3.5/§3.6；`PD-12` 未裁决） |

### 1.7 Guardrail / Test Debt

| ID | 标题 | Sev | Ev | Status |
|---|---|---|---|---|
| **GR-1** | **N-5**：文档数字门禁范围**硬编码三份文档** ⇒ `docs/architecture.md:372` 的「43 个 @property」（真值 46）永不被检查 | LOW | E1 | **OPEN** |
| **GR-2** | **N-14**：`Module/config/tests/test_preflight.py` 两条弱用例（`test_minimax_key_present_passes` 随 ambient `.env` 走两条路径；`test_c1_*` 只测 helper） | LOW | E1 | **OPEN** |
| **KB-14** | **Batch 7 副产物**：`core/tests/fake_llm.py` 的故障注入路径 `from Module.llm.errors import RateLimitError/ServiceUnavailableError` —— **该模块全仓不存在**（无 `Module/llm/errors.py`）⇒ `fault="http_429"` / `fault="http_503"` 会以 `ModuleNotFoundError` 结束而不是注入 429/503（当前无调用点，故未暴露）。与 F8 无关，本批不动测试替身 | LOW | E1 | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` |
| **GR-3** | **N-15**：`EXPECTED_INDEX_OPTIONS` 有 **4 条 `unique` 未登记**（`generations.generation_id_1`/`desktop_calls.call_id_1`/`idempotency.conversation_id_1_client_request_id_1`/`character_instances.owner_user_id_1_character_id_1_canonical_1`）⇒ 这 4 条索引的选项**不被比对**。**Challenger 复核后降级**：`check_schema_drift` 从不 raise、冲突只 `logger.error`（"只报告不处置"就是实现本身）⇒ 补登记是**低风险机械收尾**，不是产品决策（`PD-3` 降为形式确认） | LOW | E2+E1 | **OPEN**（机械收尾：补 4 条 + 删 4 条记账） |
| **GR-24** | **同表的新盲点（Challenger 发现）**：`_option_mismatches` **不比对 `partialFilterExpression`** ⇒ `character_instances.owner_user_id_1_character_id_1_canonical_1` 这类**部分唯一索引**只能"半登记"：库里是**普通唯一**索引（同样 `unique:true`）时**不报冲突**，而 `create_index(partialFilterExpression=…)` 必抛 ⇒ 仓储 `_db` 永久 None。GR-3 的解除条件（"确认线上该索引确为 unique"）**不足以**关闭它 | MEDIUM | E2 | **OPEN**（需独立的"部分索引"登记语义） |
| **GR-25** | **Batch 6 Challenger F-2/F-4**（测试鉴别力）：① `core/character/tests/test_archive_consistency.py::_ConflictOnceStore` 是**无 repo 的本地模式**（缓存即权威），它覆盖的"冲突后重试成功"在**权威模式**不可复现 ⇒ 该路径 fake-green；② `core/input/tests/test_me_identity_failclosed.py::test_put_me_rejects_stale_revision` 用**单个 store**（winner 即自己 ⇒ 缓存已刷新），在"回显 expected / 读缓存 / `expected+1`"三种错误实现下**全绿**，对冲突元数据真值零鉴别力（已在文件内就地标注，真值守卫移到 `test_me_conflict_metadata_authority.py`） | LOW | E2 | **OPEN**（测试保真度；②已标注，①待补权威模式用例） |
| **GR-4** | R4 eager 环判据**可复现漏报**（包节点 `X` 与 `X.__init__` 分裂；相对导入整类跳过）——真实仓库当前 0 环 | MEDIUM | E2 | **OPEN**（独立专项） |
| **GR-5** | 私有穿透守卫的**同名遮蔽**（访问方包内 `def _x` 即让该名字全仓免检）——按属性名近似的固有代价 | MEDIUM | E2 | **KEEP_AS_IS**（定位是回归网，非完备审计） |
| **GR-6** | 守卫扫描面不含 `scripts/`、`frontend_contract/` | LOW | E1 | **OPEN** |
| **GR-7** | 非字面量私有访问不可见（`getattr(obj, var)` / `attrgetter` / `vars(...)["_x"]`） | LOW | E1 | **OPEN** |
| **GR-8** | R6「条数漂移」分支零覆盖（变异实测仍 20 passed） | LOW | E2 | **OPEN** |
| **GR-9** | RUF100 未启用 ⇒ 863 处 `# noqa` 无陈旧检查（其中 38 处 F401） | LOW | E2 | **DEFERRED**（独立专项） |
| **GR-10** | CI 未扫描 `env:` / `with:` / `working-directory:` / `container:` 里的宿主路径 | LOW | E1 | **OPEN** |
| **GR-11** | 清空形态识别长尾（`xargs` / `for` 循环 / `git clean`）——本仓 6 处清理点全用规范写法，无实例 | LOW | E1 | **KEEP_AS_IS** |
| **GR-12** | `if:` 守卫是 token 存在性而非布尔求值（保守方向 = 当 PR 可达，更严） | LOW | E1 | **KEEP_AS_IS** |
| **GR-13** | `scripts/print_test_stats.py` 不检查子进程返回码（统计打印器、非门禁） | LOW | E1 | **KEEP_AS_IS** |
| **GR-14** | `docs/testing.md` 档位计数陈旧（全量 5033 vs 5932 等，自称脚本生成却已漂移） | LOW | E3 | **OPEN** |
| **GR-15** | `pytest.ini` 未启用 `--strict-markers` | LOW | E1 | **OPEN** |
| **GR-16** | E6：desktop golden 未从 runtime 单源（generator 带 `offset/limit`，runtime 只支持 `path`；`--check` 只查自洽） | MEDIUM | E1 | **OPEN** |
| **GR-17** | `Module/workflow_engine/tests/helpers` 的 `use_mocks` 支路 + `mock_plugin.py`（173 行）：7/7 调用点显式 `use_mocks=False` ⇒ **从未执行**（`LIKELY_DEAD`）。注意 helper 的**默认值是 True**（新调用方省略 kwarg 即可达）⇒ 删除须原子，**或**先把默认改 False | MEDIUM | E2 | **OPEN** |
| **GR-18** | 3 个 0 字节空测试包 `Module/workflow_engine/tests/{infra,learning,safety}/__init__.py` | LOW | E1 | **OPEN**（可删） |
| **GR-19** | 测试覆盖缺口：`audio_buffer`(3) / `server`(4) / `console`(1) / 并发压力(4) / DescribeImagePlugin(0) / CognitiveLoop 边界 / OutputManager 错误隔离；**含 `KB-5`（工具 60s 超时手工场景）** | LOW | E1 | **OPEN** |
| **GR-20** | `KB-7`：`core/tests/test_phase2.py` / `test_phase3.py` 裸 `setdefault("DEEPSEEK_API_KEY","test")`（进程 env 污染隐患） | LOW | E1 | **OPEN** |
| **GR-21** | token 结构 4 项低残留（`TokenLabel` 显示 `cached:0` / `flush_pending` 部分失败可重复插入 / role 判别变严格 / `TypeAdapter` 注解不精确 / debug `current_turn` 无消费者） | LOW | E1 | **OPEN** |
| **GR-22** | 前端遗留（a11y 12 项 / CSP 2 项 / AudioWorklet / messageRouter / useLocalStorage / Vue errorHandler / 命名 / 硬编码颜色） | LOW | E1 | **DEFERRED**（FE 独立 spec） |
| **GR-23** | `# type: ignore` 剩 23 处（二/三梯队）+ `Any` 剩 ~22 处（JSON 容器 / 流式协议 / 通用协程，多属不可消除） | LOW | E1 | **OPEN**（长期） |

### 1.8 Product Decisions（需 owner 拍板）

| ID | 决策点 | 阻塞谁 | 现状 |
|---|---|---|---|
| **PD-1** | 身份装载失败时：**fail-closed 启动** vs **降级运行**？`/ready` 该探什么？ | ~~**N-1 + N-11（Batch 4-A）**~~ | ✅ **已裁决并落纸（Batch 4）**：产品语义**早已**是 fail-closed / 降级运行（三处产品围栏按 `is_available` 503，但**进程不回滚**）；`/ready` 探 `is_available`（非 `health_check`）。契约见 [`docs/architecture/invariants.md`](docs/architecture/invariants.md) §3.1 + 不变量 R5/R6 |
| **PD-2** | 409 的 `currentRevision` **该报谁**（权威库现值 vs 客户端期望值）？ | N-3 | ✅ **已裁决并落地（Batch 6 `616e4993`）**：报**权威库现值**；权威现值未知时**省略**该字段（`invariants.md` §3.3 CM5）。此前：**降级为形式确认**（Challenger：只有"权威值"有用——回显客户端期望值无法收敛；且 `UserIdentityRepository.get()` 已存在，只需一个小公开访问器）。真实前提是**多进程/外部写**（同进程更新会刷新缓存） |
| **PD-3** | 那 4 条 `unique` 是否纳入 schema drift enforcement？ | GR-3（N-15） | **降级为形式确认**（`check_schema_drift` 从不 raise、只 `logger.error` ⇒ 不存在"让启动变红"的风险）。真正需要独立设计的是 `GR-24` 的**部分索引**语义 |
| **PD-4** | 审批流是否启用？（`WAITING_APPROVAL` live-lock 只在 Evaluator v2 产出 `ASK_APPROVAL` 后可达；`core/hitl.py` 自陈「没有任何触发路径」） | F1（**不是当前 bug**） | 能力已备、未接线 |
| **PD-5** | `plan_too_large` 语义 + 新契约枚举值 | F3 | 需契约可达性登记 |
| **PD-6** | 可选节点失败的任务终态语义（今天仍 `COMPLETED`） | F17 | 引擎终态定义 |
| **PD-7** | `clean_db.py` 破坏范围：让它真的清 v2（危险）vs 保持只清 v1（无用） | DEP-3 | 代码内已标注「勿擅自改」 |
| **PD-8** | `live2d.directive` 是否接线（当前全链路就绪但 `core/` 零发射点，协议已标注客户端需容忍缺席） | FEAT-2 | `KEEP_COMPAT` |
| **PD-9** | 任务中心（TaskSystem）是否恢复？（2026-08-30 决策暂缓，恢复激励点已写明） | FEAT-16 | 暂缓 |
| **PD-10** | 两套 CLI driver（`run_repl` vs `app.py::run_interactive`）去留——**行为已分岔** | AR-12 | 8 处文档承诺 |
| **PD-11** | 记忆/工具参数的**写侧** `max_length` 是否加（S2-H9 的另一半） | DEP-4 | 只做了运行期截断 |
| **PD-12** | `providers.yaml::context_window` 的语义是 **A：Elysia 实际允许/要求 provider 使用的 effective runtime context**（⇒ 可由 Ollama adapter 落实为 `num_ctx = contextWindow`），还是 **B：模型理论 capability / maximum context**（⇒ **不能**直接等价于 `num_ctx`：模型理论支持 32768，但本地 6GB GPU 可能只希望 runtime allocation 为 8192 ⇒ 未来或需区分 model context capability 与 effective/runtime context allocation）？ | `KB-13` 的修法 + `AR-16`（Phase 1/3） | **PRODUCT/ARCHITECTURE DECISION REQUIRED**：字段名与 schema **暂不决定**（不预设"一定直传 `num_ctx`"、也不预设"一定新增 `runtimeContextWindow`"）；留待 adapter batch 的全仓语义审计后由 owner 拍板 |

### 1.9 Future Features（**不与 correctness 混排**）

| ID | 功能 | 现状 |
|---|---|---|
| **FEAT-1** | 提醒持久化 | ≡ `F2`（指针，不单独计数） |
| **FEAT-2** | Live2D directive 接线（契约/发送链路/门禁/单测全就绪，`core/` 0 发射点） | ≡ `KB-1`（指针，不单独计数）；待 `PD-8` |
| **FEAT-3** | 多模态：AI→用户图片输出（`ToolResult.images` 已设计）/ `type:"file"` / `VideoPart`·`AudioPart` 全链路 | Phase 2 |
| **FEAT-4** | 记忆导出/备份（JSON）+ 记忆统计（类型/年龄/poignancy → DebugInfo） | 未做 |
| **FEAT-5** | MacroMemory + MacroReflector（每日日记，需新 Milvus 集合） | 未做 |
| **FEAT-6** | CharacterEngine Phase 2（C3/C7 动态化、L4→L3 汇聚、话题检测触发 C9、模板精简） | 未做 |
| **FEAT-7** | 唤醒词：开关手工验证 + 自定义 .onnx 训练流程 | 实现已完成，验证待做 |
| **FEAT-8** | Docker 化部署（agent + SenseVoice + GPT-SoVits 多阶段构建） | 数据库 compose 已有 |
| **FEAT-9** | 意图路由 + 渐进式工具披露（触发条件：工具 >15 规则分类 / >20 必须分类 / ≥3 次误用） | 评估过，暂不做 |
| **FEAT-10** | F2 代码块格式重新启用（解析器已支持，仅需模板取消注释 + 前端渲染确认） | 一行开关 |
| **FEAT-11** | 性能：时钟补 TTS span + bench 断言进 CI | 未做 |
| **FEAT-12** | MiniMax SSE 真流式 / `t2a_async_v2` 长文本（上次尝试已回退） | 待上游 |
| **FEAT-13** | poignancy 时间衰减 | 未做 |
| **FEAT-14** | 角色切换（需独立 spec：CharacterConfig 重载、voice lookup、`gpt_sovits_voices.yaml` 单发音人）+ 多角色 prompt 模板 | 评估过 |
| **FEAT-15** | 页面加载优化（Live2D iframe 481KB 竞争主线程 → WS onopen 延迟 ~200ms） | 有开关可缓解 |
| **FEAT-16** | 任务中心 TaskSystem（三实体 + A~E 分批；恢复激励点：需要可查询/可控/可恢复的任务历史） | **暂缓**（`PD-9`） |
| **FEAT-17** | 多前端支持（Qt 等）：评估结论 YAGNI，触发条件=确定做第二前端 | 记录 |
| **FEAT-18** | README 截图（聊天页/Debug/启动流程） | 未做 |
| **FEAT-19** | 前端 Live2D 集成（Cubism SDK v5 需 TS+npm+bundler 构建；当前仅 SDK 文件到位、默认关闭） | 未做（前端批） |

## 2. ID 与编号漂移（**读旧文档必看**）

| 你现在看到 | canonical | 说明 |
|---|---|---|
| 「N-8 = ProjectPath 常量」「N-9 = gemini」「N-10 = 吞异常」「N-11 = 空串 pronouns」「N-12 = 弱用例」（**Batch 3.5 报告 §12 的编号**） | **N-9 / N-10 / N-11 / N-12 / N-14** | todo.md 在 Batch 3.6 把「B3-CB-7 缺回归守卫」插入为 **N-8**，其后整体 +1 |
| `N-8`（guard gap） | 与 **N-9**（ProjectPath 常量）**不是一回事** | 前者已 GUARDED，后者 OPEN |
| `N-13` | ≡ **N-4** | 同一条（表级 AST 守卫缺失），已 GUARDED |
| `F5`（Batch 1.6 用） | ≡ **F6**（Batch 1.8 表里的「provider-test STT F5」） | STT provider-test 不对称；**不要**与 Batch 1.8 的 `F5 = workflow model reporter` 混 |
| `B3-CB-5 / B3-CB-4 / B3-CB-7` 的「残留」 | **N-5 / N-7 / N-8(guard)** | 残留已各归其 canonical 条目 |
| `KB-*` / `OBS-*` / `DEP-*` / `PD-*` / `AR-*` / `GR-*` / `FEAT-*` | 本批**首次分配** | 原先无 ID 的条目，便于引用；不是给已有 ID 的问题再造 ID |

> ⚠️ **`F` 命名空间比 `N` 更乱（Challenger 复核）**：Batch 1.6 的 F5/F7/F8/F9、Batch 1.7 的 F5/F6/F7/F8、
> Batch 1.8 的**两份表**（攻击清单 F1..F9 与 follow-up F1..F18）各有一套编号——同一个 `F7`/`F8`
> 在 Batch 1.8 文件内就指两件事。**本文与 `todo.md` 一律以 Batch 1.8 的 follow-up 表（F1..F18）为准**；
> 引用旧号时必须带上来源批次（例如"1.6-F5"）。

> ⚠️ **Batch 3.5 报告内自身有两套 N 编号**：§12（N-8=ProjectPath / N-9=gemini / N-10=吞异常 / N-11=`""` / N-12=弱用例）
> 与 §17（N-8=guard gap / N-13=表守卫）。§3.2 的映射以 §12 为准；§9 里"见 §12 的 N-10..N-13"是**断链**
> （§12 没有 N-13，且 §12 的 N-10..N-12 只有 3 条），读旧报告时不要被它带走。

## 3. Truthfulness Backlog（reported ≠ actual，单列）

> 本项目最大的主题簇。四层同型：**探测的是 A，报告成 B**。

| 层 | ID | 报什么 | 实际是什么 | Reach | Sev |
|---|---|---|---|---|---|
| 进程健康 | **N-1** | ~~`/ready` 200 + `chatReady`~~ | identity 未装载/不可用（`/api/me` 503） | DEPLOYMENT | HIGH |
| ~~进程健康~~ | ~~**N-1**~~ | ✅ **已修（Batch 4 `82b80e2e`）**：`/ready` 改读 `is_available`，与产品围栏同源 | 守卫 `core/profile/tests/test_ready_identity_truthfulness.py` + `core/agent/tests/test_identity_readiness_lifecycle.py`（修前 12 红） | — | — |
| 进程健康 | **N-11** | ~~静默成功（只 warning）~~ | 装载/读取失败被吞，无处可查 | DEPLOYMENT | MEDIUM |
| ~~进程健康~~ | ~~**N-11**~~ | ✅ **已修（Batch 4 `fc648e20`）**：失败由 `UserIdentityStore` 持有（`is_available` + `last_error`），成功恢复即消解 | 同上（+ `tests/real_mongo/test_real_identity_readiness.py` 真库旁证） | — | — |
| API 元数据 | **N-3** | ~~`details.currentRevision = 1`~~ | 权威库已是 2（进程内缓存） | USER | MEDIUM |
| ~~API 元数据~~ | ~~**N-3**~~ | ✅ **已修（Batch 6 `616e4993`）**：冲突现值由 repository 在冲突点回读权威库（唯一权威 = Mongo 文档 `revision`）并随 `UserIdentityUpdateOutcome` 携带，`_me_put` 只投影；未知则**省略**该字段（fail-closed）。语义 = 响应构造期的权威现值 | 守卫 `core/input/tests/test_me_conflict_metadata_authority.py`（16，修前 8 红）+ `tests/real_mongo/test_real_identity_conflict_metadata.py`（3，修前 3 红，含跨进程 writer）+ 不变量 CM1–CM5（`invariants.md` §3.3） | — | — |
| ~~provider 能力~~ | ~~**N-10**~~ | ✅ **已修（Batch 5 `c70312de`）**：capability 真相收敛到各 registry 的 `_UNIMPLEMENTED_PROVIDERS`（`is_implemented()` 与 `create()` 守卫同常量），preflight/`--full`/`/api/providers` 一律问它 | 守卫 `Module/config/tests/test_preflight_provider_capability.py`（15）+ `core/input/tests/test_provider_registry.py`（修前 10 红） | — | — |
| ~~provider 探测~~ | ~~**F6**~~ | ✅ **已修（Batch 5 `7ef3202b`）**：缺省目标读运行期句柄 `agent.stt.model`（未连接才回退 config） | 守卫 `core/input/tests/test_provider_test_target_authority.py`（8，含真实 `switch_stt` A→B；修前 5 红） | — | — |
| ~~预览窗口~~ | ~~**F8**~~ | ✅ **已修（Batch 7 `558503be`）**：预览组装此前**不传** `prompt_budget_tokens` ⇒ 超**声明**窗口的请求照发，两类 provider 都受害——强制窗口类：provider 400 → 折成 **retryable 的 503**（"稍后重试"是谎报）；截断类（本机 ollama 0.24 实测）：200 + **静默截断**，人设被悄悄削掉。现按 **resolved runtime model** 的窗口取 canonical 预算；裁完仍装不下则**本地** 400 `CHARACTER_VALIDATION`，**零 provider 请求** | 守卫 `core/input/tests/test_character_preview_window.py`（8，修前 5 红；路由级 = `POST /api/characters/preview` + 窗口约束 stub）+ 不变量 PW1–PW6（`invariants.md` §1.1） | — | — |
| ~~面板标志~~ | ~~**F7**~~ | ✅ **已修（Batch 5 `87cc7e38`）**：`Agent.stt_ok` 改为派生属性（`_stt is not None`），与 capabilities/`/debug` 同源；粘性槽位 `_stt_ok` 已删。**"对象非空不代表可用"经复核不成立**（实例化受 capability 门控；`available` 的既有判据就是这个） | 守卫 `core/agent/tests/test_service_availability_truthfulness.py`（8；修前 2 红） | — | — |
| API 元数据 | **P2-01r** | ~~`has_more=false` + 无 cursor（"没有更多"）~~ | 持久层还有第 201+ 条（单页上限被当成数据集上限） | USER | MEDIUM |
| ~~API 元数据~~ | ~~**P2-01r**~~ | ✅ **已修（Batch 8 `36cb7cec`）**：分页下推到存储 （keyset 谓词 + 双键排序 + `limit` + title `$regex`），service 只做"持久层分页流 ⊕ 进程内覆盖层"归并；`has_more` 用 limit+1 探测 | 守卫 `core/input/tests/test_sessions_pagination.py`（21，修前 10 红）+ `tests/real_mongo/test_real_session_pagination.py`（7）+ 不变量 PG1–PG6 | — | — |
| 门禁 | **N-15** | “13 个集合无漂移” | 4 条 `unique` 从未参与比对 | DEPLOYMENT | MEDIUM |
| provider 窗口 | **KB-13** | 「声明 8192 就能用 8192」（budgeting 按静态声明值算） | Ollama serving context 是 **runtime allocation**：冷启动不传 `num_ctx` 实测 **4096**、显式 `num_ctx=8192` 才 8192；超窗 **200 + 静默截断** ⇒ 不能靠 provider error 发现 mismatch | DEPLOYMENT | MEDIUM |
| 门禁（已修） | N-4/N-8 | — | Batch 3.6 已 GUARDED，且守卫本身又被 Challenger 补强 | — | — |

## 4. Deferred / Currently Unreachable / Keep-As-Is

| ID | 为什么不排 | 重开条件 |
|---|---|---|
| **N-12** 空串 pronouns | 仓内无写入者（契约拒空串），存量数据形态 | 出现写入者或存量数据出现空串 |
| **N-2** `_update_local` 绕过 pydantic | **前半已由 `fb438167` 关闭**（`pronouns: str \| None` ⇒ `None` 合法）；剩后半：`_load_from_file` 读回失败会**重新 bootstrap**（静默重置）——**仅本地/单测模式**可达，生产装配恒定权威 repo | 本地模式被产品化 / `_load_from_file` 路径进入生产 |
| **AR-14** `ConfigLoader(env_path=…)` | 无生产调用方；Module 是库，无法证明仓外无调用方 | 需要收窄 Module 公开面时 |
| **AR-15** 两个一次性 `.env` 加载器 | 现存调用点只剩 `scripts/dev/stage_h_pinned_smoke.py`；库代码已不调用 | 收敛入口点 API 时 |
| **OBS-4** Batch 1.6 §12#6（consolidator + client closed） | HEAD 上定位不到（记录疑似错位） | 复现出该报错时 |
| **F1** `WAITING_APPROVAL` live-lock | Evaluator v1 **不产出** `ASK_APPROVAL`；`core/hitl.py` 自陈三处无触发路径 | 启用审批流 / Evaluator v2（`PD-4`） |
| **F16** `terminal_payload.get(null)` | 今天不可达 | 出现 `null` 值载荷 |
| **F12** 失败细节长度上界 | 现文本是引擎编写的短诊断串 | 某 provider 回显长文本 |
| **RB-1** Mongo write-only 字段 | 无真实 Mongo 抽样 → 不能升级为 `PROVEN_DEAD` | 一次真实 Mongo 抽样 |
| **GR-9** RUF100 | 863 处命中，独立专项 | 私有穿透纪律迁移进 ruff 时 |
| **GR-22** 前端遗留 | 属前端仓/独立 spec | 前端批次 |
| **AR-1/AR-2/AR-5/AR-10/AR-11** | 见 §1.6（`KEEP_AS_IS`/`REFACTOR_LATER`，理由已入库） | 公开面收窄专项 / 产品决策 |
| **DEP-2 / DEP-4 / DEP-6 / SEC-1 / SEC-2** | 见 §1.4/§1.5（已接受或已裁定） | owner 决定 / 触发条件 |
| **Batch 8 并发写位移**（accepted risk） | keyset 分页的**固有语义**：翻页期间新插入的记录（key 在游标之前）会被本轮遍历跳过、被改名的记录会被"抬高"而跳过——两者都只影响**当轮**遍历，下一次请求即一致；删除的记录不会复活。产品接受 eventual pagination（不引入 snapshot isolation） | 产品要求"分页期间强一致快照"时（需版本化读/事务） |
| **KB-3** INTERRUPT+TTS emoji 合并 | 后端那半已修（`4bb8aff7`，S3-G7）；**剩余竞态在外部前端仓** | 前端仓修 |
| **KB-9** `ConfirmModal` @keydown | 前端遗留（现行为已可靠） | 前端加固批次 |
| **KB-26** `_current_task` 陈旧句柄 | Batch 14 复核：状态成立、缺陷证伪（5 个生产读者全在 `.done()` 守卫内；ownership truth 已由 `_generation_ctx` + `_started_cognitive_tasks` 接管）| 出现**无 `.done()` 守卫**的新读者时 |
| **KB-28** `IDLE 已广播 / 锁未释放` | Batch 14 复核：主形态（下一条消息取消卡在锁上的回合）已被 Batch 12/13 判据覆盖；残留的"严格两发停止静默丢失"需要仓内不存在的挂起 `on_change` 订阅者 | 引入第二个 `on_change` 订阅者（或让 `_notify` 可挂起）时 |
| **KB-1** `live2d.directive` 无发射点 | `KEEP_COMPAT`（协议已标注客户端容忍缺席） | `PD-8` |
| **AR-16** Ollama Provider Adapter | **时序**：不插进当前 correctness batch 中途（blast radius：provider factory/backend boundary、context authority、request options、usage 归一、diagnostics、现有 OpenAI-compatible 路径） | 高优先级 correctness backlog **再清一轮后**自行开批；**提前触发**：某项 correctness 需动 Ollama request parameters / context window runtime semantics / `num_ctx` / provider-specific truncation / Ollama runtime model state 时，先做 Adapter batch |

## 5. Closed / Historical（一行索引；详细证据在 `docs/review/`）

| ID | 一句话 | 关闭于 |
|---|---|---|
| **B3-CB-1** | `pronouns: null` write-valid/read-invalid（PUT 500 → 重启 503） | FIXED `fb438167`；守卫 `core/input/tests/test_me_pronouns_roundtrip.py` |
| **B3-CB-2** | `EXPECTED_INDEX_OPTIONS` 键 `idempotency_claims` 永不生效 | FIXED `0302566a` + **GUARDED** `1382ae43`/`34b0faf6` |
| **B3-CB-3** | preflight 把可用配置判死（`start.sh` 拒绝启动） | FIXED `edd67cef`（+ `466e3242` TTS 凭据回归） |
| **B3-CB-4** | `PROTECTED_COLLECTIONS` 自称有守卫 | **FALSE_POSITIVE**（守卫存在，只是不读该常量）→ 措辞见 `N-7` |
| **B3-CB-5** | 文档数字门禁漏根 README | **ALREADY_FIXED** `0937e85a`；残留窄洞见 `N-5`/`GR-1` |
| **B3-CB-6** | 两份语义不同的 `UPDATABLE_FIELDS` 白名单 | **KEEP_AS_IS**（当前不可达：契约 `extra="forbid"` 挡住分岔） |
| **B3-CB-7** | `_IMAGES_DIR` import 期固化 ⇒ 测试隔离从未生效 | **ALREADY_FIXED** `800e54da` + **GUARDED** `6c559816`/`210e9c37` |
| **N-1 / N-11** | `/ready` 探 ping 假绿 + 装载失败无可查状态（同一因果链） | **GUARDED** `82b80e2e` + `fc648e20`；PD-1 契约落纸 `invariants.md` §3.1（R5/R6） |
| **N-4 ≡ N-13** | `EXPECTED_INDEX_OPTIONS` 无表级 AST 守卫 | **GUARDED** `1382ae43` + `34b0faf6`（选项键集，Challenger F-A1） |
| **N-8**(guard) | B3-CB-7 缺回归守卫 | **GUARDED** `6c559816`+`210e9c37`+`36bb5bcf`+`cc413eb4` |
| **F8** | 预览超声明窗口照发 → 强制窗口类 provider 400 折成 **retryable 的 503**（谎报可恢复）；截断类 provider 静默截断 | **GUARDED** `558503be`；不变量 PW1–PW6（`invariants.md` §1.1）；守卫 `core/input/tests/test_character_preview_window.py`（8，修前 5 红） |
| **P2-01r** | 会话列表数据源 `list_all(limit=200)` ⇒ 第 201+ 条不可分页/搜索，最后一页 `has_more=false` 谎报"没有更多"（详情路径同源 ⇒ 第 201+ 条 `updatedAt:""`）；另一半"过期级联清理 worker"**SUPERSEDED**（2026-08-10 起 sessions 无 TTL，删除走显式级联） | **GUARDED** `36cb7cec`；不变量 PG1–PG6（`invariants.md` §4.1）；守卫 `core/input/tests/test_sessions_pagination.py`（21）+ `core/memory/tests/test_session_manager_pagination.py`（30）+ `core/storage/tests/test_session_repo_pagination.py`（24）+ `tests/real_mongo/test_real_session_pagination.py`（7）；修前 49 红（三套件）+ 1 xpass |
| **KB-31** | **think 路径异常收口被取消** ⇒ RUNNING 下相位永停 THINKING、**用户可见的永久失聪**（Batch 11 Phase 1 新发现，与 KB-25 同 root、**不**并入） | **GUARDED** `b56a8442`（同一 fix）；守卫 `test_cancel_during_error_finalization_settles` + `test_stop_button_during_error_finalization_does_not_deafen_agent`（修前 2 红）；证据见 `docs/review/2026-09-18-Batch11-*` §9.1.1/§11.2/§34 |
| **F9** | Batch 2 的 F401 / CI / privacy guard | 已由 Batch 2 完成（见其执行记录） |
| **M19 / M20** | resume 后 pause 被销毁 / cancel 规划期不推终态 | FIXED（E2/E3，`bb69485`…`5aa7368`） |
| **LoggerFactory** | 被取代的旧设计（stdout 污染对话流） | 已删（2026-09-12），`get_caller_logger` 迁 `Module/_common` |
| **空 LLM 响应被静默吞掉** | 空响应守卫 + 兜底回复 | FIXED 2026-08-02（`_EMPTY_RESPONSE_NUDGE`） |
| **`flush_after_response` 超时层不匹配** | 两层各自计时 ⇒ 任务被外层砍掉 | FIXED `b3e513df`（预算单源到 `drain` 总预算） |
| **STT 运行时切换不生效** | `switch_stt` 真建后端 + 运行时槽位 | FIXED 2026-08-03 |
| **STT env 变量陈旧** | 统一到 `SENSEVOICE_URL`（运行期唯一生效路径） | FIXED 2026-08-07 |
| **WS 无法带 Bearer（浏览器前端）** | 决策：v1 浏览器聊天已停用，无消费方 | CLOSED（2026-08-11 决策，不做） |
| **`.env` 含真实 key** | 被 gitignore、从未进历史；`.env.example` 的 9 键**已于 `67eeab83`（2026-09-13）补入模板**（注释形态，凭据类刻意不预填） | 复核降级 2026-09-11 + **ALREADY_FIXED `67eeab83`** |
| **DEP-1** | `.env.example` 落后 9 键 —— **ALREADY_FIXED**（同上一行；旧记录的"缺 9 键 / 9/9 MISSING"已过期，9 键都在模板里，只是注释形态） | ALREADY_FIXED `67eeab83` |
| **Batch 1.5 R4/R5 的一部分** | `EnvManager` 类名已不存在（loader 侧只剩 `_loaded`/`ensure_dotenv`，见 `AR-15`）；`config_test_helpers` 陈旧注释与 `test_phase3` 死代码已随批次清理 | SUPERSEDED（形状已变，残留归 `AR-15`） |
| **`memory_repo.clear_all()` 无守卫** | 类型化 `?confirm=` 守卫 + 修掉「存储不可用却报成功」 | FIXED 2026-09-11（残余 `DEP-3`） |
| **后台协程所有权分散** | 43 站点普查 + 7 处定点修复 + R6 门禁 | FIXED 2026-09-12（`ARCH-INC-P2-03`） |
| **`Module/llm/registry` 反向依赖 core.input.ssrf** | ssrf 下沉 `Module/_common`，Module→core 生产反向依赖清零 | FIXED 2026-08-14 |
| **idempotency_repo 反向依赖 input** | `GRACE_SECONDS` 单源下沉 | FIXED 2026-08-14 |
| **input 层内聚（ws_handler 1825 行等）** | 批次 A/B/C/D 拆分完成（注册表分发 + 5 域 REST + 纯装配 server） | FIXED 2026-08-14 |
| **`llm_adapter.close` fire-and-forget** | `close()` 先 `drain` 热切换遗留关闭任务（3s 预算） | FIXED（`ARCH-INC-P2-03`，见 `core/adapter/llm_adapter.py:159`） |
| **token 统一结构 / 模块化评估 / Electron v2 联调 / 阶段 3·4 联调** | 各批 23/41+ 项全部 ✅ | 见 `docs/review/`（archive/2026-08）与 `docs/completed/` |
| **两个偶发失败的 external 集成测试** | `test_integration_global_constraints_*` / `test_multi_step_task` —— 随文件删除消解（那些用例 `external` 门控、100% skip，确定性单测早已迁移） | FIXED 2026-09-10（删文件） |
| **`Module/tool/safety.py`（618 行）拆分** | 评估结论：**不拆** | CLOSED（`docs/review/2026-09-17-Batch3-*`） |
| **Claude 原生持久记忆** | 已启用（2026-08-01 起持续写入关键决策/教训）；后续写入属实践而非 TODO | CLOSED |
| **`/api/providers/test` 等历史联调项** | 后端侧全部闭环 | 见 `docs/review/archive/2026-08/*` |

## 6. 判例：不要把下列问题登记成后端缺陷（前端/其他仓的适配面）

1. `generation.segment.completed.content` 是后端**权威完整文本**；inner voice / TTS 场景允许零 delta。由前端 Adapter/Accumulator 消费，后端不为兼容错误前端伪造 delta。
2. `generation.completed.conversationUsage` 的真实定义是 `{total, byModel}`；前端不应虚构会话累计 `input/output`。若产品需要累计拆分，另立需求扩展 tracker 与契约。
3. raw wire 用 `type/delta/mood/generationId`，领域层用 `event/text/emotion/responseId`——属 Main Adapter 的正常改名职责。
4. Desktop bridge 的 `tool.request/tool.status` 安全元数据、状态机与重连交叠精确投递已实现并通过专项测试；缺口主要是 Electron `ToolResult` 领域接口过窄。

## 7. 决策记录（仍然有效，勿删）

> **任务中心（TaskSystem + TaskService）暂缓不做（2026-08-30）**：正式依据见
> [`docs/2026-08-29-task-system-overall-architecture-design.md`](docs/2026-08-29-task-system-overall-architecture-design.md)
> 与 [backend 架构设计](docs/2026-08-29-task-system-backend-architecture-design.md)（三实体目标架构 + 分批 A~E + 待决策 TSA-P01~P10/BE-D01~D13）。
> 暂缓期内不把普通工具调用并入 WorkflowEngine、不创建产品 TaskRecord、不动现有 wire 契约、不实现任务中心 REST/WS API。
> 恢复激励点：需要可查询/可控/可恢复展示的任务历史时，从批次 A（公共契约 + 持久读模型）启动。→ 见 `FEAT-16` / `PD-9`。

> **`core/tool_runtime` 预留能力保留（2026-09-12）**：`depends_on` / `children_ids` /
> `run_in_background` / `progress` / checkpoint 持久化 / `dispatch_background()` /
> `new_background()` 全部保留（评估稿 §ARCH-INC-P1-01 批次 B 的删除计划**作废**）。
> P1-01 剩余范围收窄为命名与概念，且改名已于 2026-09-13 完成（checkpoint 反序列化兼容旧键
> `parent_id`）。见 `core/tool_runtime/README.md`。

> **剩余桩代码一律保留（2026-09-12）**：「零引用 ≠ 该死」。保留项统一带可 grep 标记
> **`P3-01-RESERVED`**，后续引用审计遇到即跳过。判别标准：**预留** = 有明确未来用途 + 注释；
> **残留** = 已被取代或注释与代码不符。清单见 [`docs/architecture/dead-code-review-checklist.md`](docs/architecture/dead-code-review-checklist.md) §9。

> **CI / 测试分层**：`external` marker 取代 `--ignore`（F1）；`test-quick`/`test-normal` 不再跳过任何目录；
> `real_mongo` 走容器（push-only + self-hosted）；`make test-full` 无自动执行点（见 `docs/ci.md`）。

---

> 历史正文（决策过程、逐批完成记录、详细证据）按 §5 索引到 `docs/review/`、`docs/completed/`
> 与 `git show 69f2e5a9:todo.md`。**本文件只承载当前状态**，不再累积过程叙事。
