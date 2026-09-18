# 系统不变量（Invariants）

> 文档导航：[项目文档](../../README.md) → [docs 索引](../README.md) → 本文

## 这是什么

本文记录**已经被测试钉住、且被认为必须长期成立**的系统不变量，并把每一条映射到
「测试 → 门禁 → CI」。

它回答一个具体问题：**某条不变量被破坏了，会有东西变红吗？**
——只写 prose 的不变量清单会退化成愿望清单；只有同时给出可执行判据，
它才是可维护的 contract。

## 怎么读

| 列 | 含义 |
|---|---|
| **Invariant** | 不变量本身（一句话，可证伪） |
| **Test** | 钉住它的测试（文件::用例）。**这是判据所在** |
| **Gate** | 该测试经由哪道门禁执行 |
| **CI** | 哪个 job 会跑到它 |

**CI 列的含义**：`push/backend` = 每次 push 到 main 的 `ci.yml:backend` job
（跑 `make test-normal`）会执行；`manual` = 只在手工触发的 `nightly.yml` 里跑
（**push 不会自动跑到**）；`—` = **当前没有任何自动执行点**。

> ⚠️ **`manual` 与 `—` 是本文最有价值的两列**。一条不变量"有测试、但 CI 不跑"
> 等于**假保障**——它在下一次回归时不会红。这类项集中列在 §7。

---

## 1. Prompt（P）

来源：[2026-09-16 Batch 1 执行记录](../review/2026-09-16-Batch1-correctness-durability-执行记录.md) §4。

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **P1** | prompt 预算降级是 **render-local** 的：一次 `render` 的降级只影响本次，不得永久改写下一次的上下文 | `Module/system_prompt/tests/test_render_local_budget.py`（12 例，含异常路径恢复） | `make test-quick` / `test-normal` | **push/backend** |
| **P2** | 临时上下文**不泄漏**到 system prompt：角色/关系/情绪等运行时片段不得固化进模板正文 | `core/prompt/tests/test_system_prompt_leak.py`（21 例） | `make test-quick` / `test-normal` | **push/backend** |

**P1 的机制**：opt-in 覆盖 + `finally` 逆序恢复；`render()` 不再使用破坏性的
`set_context` 广播。判据点：`test_render_local_budget.py:231`（溢出轮结束应恢复为
`None`）与 `:450`（**异常路径也必须恢复**）。

**P2 的补充网**：`core/prompt/tests/test_system_prompt_snapshot.py`（快照）与
`Module/system_prompt/sections/authored.py` 的**递归转义**（见 CLAUDE.md
「system prompt 变量转义」）。后者有 AST 守卫防二次转义。

### 1.1 Preview window contract（PW）

来源：[2026-09-18 Batch 7 执行记录](../review/2026-09-18-Batch7-provider-preview-window-执行记录.md)。
缺陷形状（canonical backlog **F8**）：prompt 预算（S2-H9，`076ebee8`）只接进了
**chat** 组装路径（`core/cognitive/loop.py::_build_turn_prompt`）；**预览**是第二条
组装路径（`core/character/preview.py`，比预算机制早 6 天落地），从未迁移 ⇒
`system + userMessage` 原样发出，小窗口 provider 上超窗 → provider 侧失败 →
路由把它折成 503 `CHARACTER_PREVIEW_FAILED`（**retryable，是谎报**：重试不会让它
消失）。

> **Provider preview requests must respect the resolved model's effective context window.**

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **PW1** | 预览请求**必须装得下 resolved runtime model 的声明窗口**（估算输入 < 有效窗口）；裁剪沿用 chat 的同一实现与同一常量，不新增预览专用默认值 | `core/input/tests/test_character_preview_window.py::test_preview_fits_small_window_when_trimming_suffices`、`::test_builtin_character_preview_records_window_margin` | `make test-quick` / `test-normal` | **push/backend** |
| **PW2** | 窗口/预算权威 = **resolved runtime model**（`LLMAdapter.model` → `LLMFactory.context_window`）；不得读 `config.yaml`/bootstrap 模型，也不得新建第二份 contextWindow 表 | `::test_preview_budget_follows_resolved_model_not_config`、`::test_preview_undeclared_window_keeps_compat_policy` | 同上 | **push/backend** |
| **PW3** | **能裁进窗口的预览必须能预览**：本地拒绝的阈值是**窗口**而非预算（预算含 25% 输出余量，保护节自身就可能超过它） | `::test_preview_boundary_sends_when_fits_and_validates_when_not`、`::test_small_window_model_can_preview_a_small_request` | 同上 | **push/backend** |
| **PW4** | 估算输入**占满或超过**窗口（`>=`，含"装得下但无输出余量"）→ **本地**拒绝（400 `CHARACTER_VALIDATION`，非重试）且**零 provider 请求**；不得发送已知必失败的请求、不得自动缩预算重试 | `::test_preview_impossible_input_fails_locally_without_provider_call`、`::test_preview_boundary_sends_when_fits_and_validates_when_not` | 同上 | **push/backend** |
| **PW5** | **大窗口行为不回归**；未声明窗口的第三方 provider 维持既有兼容 fail-open（与预算同源的保守默认，不是"无限大"、也不是 0） | `::test_preview_large_window_provider_unchanged`、`::test_preview_undeclared_window_keeps_compat_policy` | 同上 | **push/backend** |
| **PW6** | 预览预算**不改变正常生成策略**：回退规则只有一份（`Module.system_prompt.budget.effective_context_window`）；**现有配置下 chat 路径逐字不变** | `core/prompt/tests/test_prompt_budget.py::TestEstimation::test_effective_window_is_the_shared_fallback`、`::test_budget_uses_headroom_and_default` | 同上 | **push/backend** |

**为什么拒绝用 400 而不是 503**：产品契约（Stage H §4.5）把 503
`CHARACTER_PREVIEW_FAILED` 定义为"provider/运行时**暂时**不可用"。裁完仍占满窗口是
**确定性**的输入问题（改角色档案或换模型才可解），用可重试的 503 报它等于重复
F8 的原始错误——把真相换成"稍后重试"。400 `CHARACTER_VALIDATION` 是既有错误码，
**不新增契约字段**。

**PW6 的精确边界**：`effective_context_window(None/0/负数)` 一律回退保守默认
（32768）。对 `0` 与 `None` 这与旧行为一致；对**负数**，旧式
`context_window or DEFAULT` 会把它当"已声明"并算出退化预算（`max(1, -5*0.75)=1`），
现在改为回退默认值。仓内**没有任何配置声明 0/负数**（`providers.yaml` 六家均为正数，
`LLMFactory.context_window` 对 falsy 已归 None）⇒ 该分支不可达，属 hardening。

**为什么预览不设 output cap**：chat 与预览**都不**设 `max_tokens`（provider 默认），
输出余量由 `CONTEXT_HEADROOM_RATIO`（0.75）预留；仓库没有 model 级 output cap
metadata，本批不新造（"限制输入长度和输出 token" 那句设计稿从未落成契约字段，
属独立产品决策）。

## 已知边界（accepted risk，全部有测试钉住行为）

1. **保护节压过预算**：8K 级窗口上 *保护节*（F1/F2/F3/C1/C2/C6/C7）自身就可能超过
   25% 输出余量 ⇒ 预览只保证"输入 < 窗口"，不保证"输入 ≤ 预算"。不是静默降级：
   `SystemPromptManager.render` 会 warning 报出保护节规模，`build_messages` 会
   warning 报出"裁尽历史后仍超预算"。
2. **判据是启发式估算，不是 provider tokenizer**：`estimate_tokens` 按 CJK 1 字
   1 token、其余 4 字符 1 token。它对 CJK 偏高（实测 est 8329 vs qwen 真值 7921，
   ~5%），对 JSON/代码偏低（实测 est 7302 vs 真值 12212，约 0.6×）⇒ 本地判定有
   **双向误差带**：可能误拒一个勉强装得下的预览，也可能漏放一个在**强制窗口**的
   provider 上必 400 的请求。修它需要真 tokenizer（`§15/§16` 明确不在本批范围），
   已登记 `KB-12`。
3. **声明窗口 ≠ 服务窗口**：`providers.yaml` 的 `context_window` 是**手写估算值**
   （`ollama` 声明 8192，实测 ollama 0.24.0 默认 `num_ctx` = 4096，且**超窗时静默
   截断并返回 200**，不报 400）⇒ "装得下声明窗口"是必要条件而非充分条件。属既有
   条目 `OBS-2` 的家族，Batch 7 用真实 provider 补了证据，见 `KB-13`；改它会全局
   改变 chat 的截断时机，**不在本批范围**。
4. **窗口小于保护节下界**：若 `context_window` ≤ 保护节规模（实测 ~2.1K 起），
   任何预览都会被本地拒绝（PPW-3 在该区间退化为"明确拒绝"而非"永不 400"）。
   这是诚实的结果：不具备可预览性时应报"内容超窗"，而不是发一个必然被截断/拒绝的
   请求。

---

## 2. Background Task（T）

来源：[2026-09-17 Batch 1.7 执行记录](../review/2026-09-17-Batch1.7-background-task-terminal-state-执行记录.md) §3。

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **T1** | accepted task **必然到达终态**（`COMPLETED`/`FAILED`/`CANCELLED`），绝不永久停在 `queued`/`planning`/`running`，也不静默消失 | `core/agent/tests/test_task_terminal_closure.py`（4 例） | `make test-quick` / `test-normal` | **push/backend** |
| **T2** | 每个任务**恰好一个** logical terminal outcome（first-terminal-wins；安全网与所有 push 点共用同一守卫） | 同上 + `core/agent/tests/test_task_result_durability.py`（26 例） | 同上 | **push/backend** |
| **T3** | 终态**跨进程边界存活**：所有终态事件经 `push_mailbox_event` → 唯一性守卫 → **同步 WAL sink** → 邮箱 → 轮询落 repo，**无 fast-path** | `core/agent/tests/test_task_result_durability.py`、`core/agent/tests/test_task_result_interrupt_delivery.py` | 同上 | **push/backend** |
| **T4** | 失败**归因与实际阶段一致**：规划期任何失败 → `planning_failed`（结构不合法 → `plan_too_large`），**不得**是 `internal_error`/`timeout` | `core/agent/tests/test_task_terminal_closure.py` | 同上 | **push/backend** |
| **T5** | 重启补投**不覆写**更强的终态（补投逐字不改写；`delivered` 状态与库一致） | `core/agent/tests/test_task_terminal_closure.py`（T6 用例） | 同上 | **push/backend** |
| **T6** | durable ack 只在**消费方取得所有权之后**授予（禁止态 `WAL removed ∧ delivered=True ∧ ¬accepted`） | `core/agent/tests/test_task_result_durability.py` | 同上 | **push/backend** |
| **T7** | at-least-once 重投**不改变逻辑结果**（同一终态的 replay 允许，第二次不得产生新语义） | `core/agent/tests/test_task_result_interrupt_delivery.py`、`test_task_outcome_user_semantics.py`（19 例） | 同上 | **push/backend** |

**T1/T2 的最强证据**：Batch 1.7 用 `probe_t2_race.py` 做过并发攻击扫描
（planning 失败 × `cancel()`/`stop()`，10 个时延点 × 2 种攻击 = 20 例），
**全部恰好一个 logical terminal**。该探针在 `/tmp`，**未入库**（见 §7）。

---

## 3. Runtime（R）

来源：[2026-09-17 Batch 1.6 执行记录](../review/2026-09-17-Batch1.6-runtime-authority-lifecycle-执行记录.md)、
[2026-09-16 Batch 1 执行记录](../review/2026-09-16-Batch1-correctness-durability-执行记录.md) §4、
[2026-09-18 Batch 4 执行记录](../review/2026-09-18-Batch4-runtime-health-truthfulness-执行记录.md)、
[2026-09-18 Batch 5 执行记录](../review/2026-09-18-Batch5-provider-capability-truthfulness-执行记录.md)、
[2026-09-18 Batch 6 执行记录](../review/2026-09-18-Batch6-conflict-metadata-truthfulness-执行记录.md)（§3.3）。

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **R1** | **bootstrap ≠ runtime ≠ persisted**：三处配置来源各自独立，不得互相冒充 | `core/agent/tests/test_runtime_state_persistence.py`、`core/agent/tests/test_state_single_authority.py` | `make test-quick` / `test-normal` | **push/backend** |
| **R2** | runtime reporter **读 runtime 权威**（不是 bootstrap，也不是持久化快照） | `core/agent/tests/test_runtime_authority_end_to_end.py::test_switch_then_restart_keeps_every_reporter_consistent` | `make test-quick` / `test-normal` | **push/backend** |
| **R3** | **显式 CLI 覆盖跨 reload 存活**："没传" 与 "传了等于默认值" 必须可区分（argparse 的 default 不得冒充显式覆盖） | `core/agent/tests/test_cli_overrides.py`（22 例，含 `test_explicit_override_survives_every_reload`） | `make test-quick` / `test-normal` | **push/backend** |
| **R4** | 独立子系统**拥有自己的资源生命周期**（谁创建谁关闭；停机后必须 drain 再关依赖） | `core/agent/tests/test_task_llm_ownership.py`（10 例） | `make test-quick` / `test-normal` | **push/backend** |
| **R5** | **readiness 反映真实可用性**：`/ready` 的 identity 门禁读「身份已装载且权威库连着」的运行时状态，不读代理量（ping 通 / 对象存在 / repo 已构造） | `core/profile/tests/test_ready_identity_truthfulness.py::test_RHT2_ready_503_when_identity_load_failed`、`::test_RHT5c_failed_reload_does_not_serve_stale_identity`、`core/agent/tests/test_identity_readiness_lifecycle.py::test_healthy_start_is_ready_and_does_not_touch_health_check_probe` | `make test-quick` / `test-normal` | **push/backend** |
| **R6** | **关键装载失败留在运行时状态**：失败事实由拥有该 lifecycle 的对象（`UserIdentityStore`）持有并可查（`is_available` = 不可用 + `last_error` = 类型名），不得只以日志形式存在；成功恢复后必须消解，不得钉死判定 | `core/profile/tests/test_ready_identity_truthfulness.py::test_RHT1_load_failure_sets_unavailable_and_queryable_error`、`::test_RHT6_reconnect_clears_failure_and_restores_ready`、`core/agent/tests/test_identity_readiness_lifecycle.py::test_start_with_undecodable_identity_keeps_process_alive_but_not_ready` | `make test-quick` / `test-normal` | **push/backend** |

**R4 的静态面**：`scripts/check_architecture.py` 的 **R6**（裸 `create_task`/
`ensure_future` 必须逐点登记「任务存哪 / 谁取消 / 谁等待 / 异常谁看」，
19 个登记点 / 22 处 spawn）。门禁：`make check-arch`，**push/backend**。
运行期契约见 `Module/_common/task_scope.py`（`AsyncTaskScope` / `cancel_and_drain`）。

### 3.1 Readiness contract（PD-1 落纸）

> **Identity availability is required for chat readiness.**
> **Process liveness is independent from readiness.**

**三种语义，互不冒充**：

| 端点 | 回答的问题 | 判据 | 契约 |
|---|---|---|---|
| `/health` | 进程还活着吗 | 进程内存读取，**零网络探针** | **恒 200** |
| `/ready` | 对外承诺的核心 chat 能力现在可用吗 | core chat 门禁（见下） | `chatReady` → 200，否则 503 |
| `/ready` 的 `fullReady` | 长期记忆真的能持久化吗 | `chatReady` ∧ Milvus ∧ embedding | **不**门禁 HTTP，只可观测 |

**为什么「身份不可用 ⇒ 不就绪」是产品语义而不是新增严格性**：产品自己早已对同一
权威 fail-closed——`UserIdentityStore.is_available`（`_cached is not None` 且
`repo.is_connected`）是这三处的围栏判据：`/api/me`（`core/input/character_routes.py`
→ 503 `STORAGE_UNAVAILABLE`）、对话轮次的身份快照
（`CognitiveLoop._identity_snapshot` → 503）、角色服务（`service/queries.py` 预览、
`service/instances.py` 归档）。**产品不服务，readiness 就不许说就绪**；否则运维/部署
看到的绿与用户拿到的 503 直接矛盾（N-1 的实测形态：同一受控状态下 `/ready` 200
而 `/api/me` 503）。

**范围纪律（不泛化）**：本门禁只覆盖「聊天/用户请求的必要依赖」这一条身份状态，
不把 TTS/STT/Workflow/Memory/可选集成或任意 warning 塞进 `/ready`。允许
`Agent` 进程继续运行而 `readiness=false`——「不就绪」不等于「进程必须退出」。

**为什么不是 `health_check()`**：两个谓词回答两个问题，且**调用它还有副作用**。

| 谓词 | 问题 | 实现 | 副作用 |
|---|---|---|---|
| `is_available` | 身份现在能不能服务？ | `_cached is not None and repo.is_connected`（纯读） | 无 |
| `health_check()` | 权威库现在通不通？ | `repo.health_check()` → 真实 `ping()` | 失败时 `_mark_unavailable()`（`_db = None`）并**重连** |

「ping 通」不蕴含「身份已装载」：库里文档不合域模型时 repo 连得好好的，而 `_cached`
恒为 None。故 `/ready` 读 `health_check()` 会假绿；而读 `is_available` 同时让探针
（`/ready`）、修复器（`_dependency_needs_recovery("identity")`，本就是
`not store.is_available`）与产品围栏三者**同源**，并恢复「探针无副作用」。
`health_check()` 的职责**不变**——它仍是数据库恢复协调器的探针。

**未就绪的判据表**：

| 进程 | 身份状态 | chat 能力 | 期望 `/ready` |
|---|---|---|---|
| 存活 | 已装载可用 | 可用 | ready（200） |
| 存活 | 装载失败/重载失败 | 不可用 | **not ready（503）** |
| 存活 | 从未配置 repo（本地/单测模式） | 可用（本地 bootstrap） | ready（200） |
| 存活 | 失败后恢复（`connect()` 重载成功） | 可用 | ready（200） |
| 停机窗口（`_state ≠ RUNNING`） | 任意 | — | not ready（503），CR-08 |

**本契约**不**包含的判定（经复核后的接受项，防止把它读成更强的承诺）**：

1. `is_available` 只看「repo 句柄仍在 + 缓存非空」，**不**做新鲜度探测——权威库在
   缓存之后挂掉、或权威文档被**外部写入者**改坏/删除时，`is_available` 仍为真
   （缓存即服务），身份**写**路径会 fail-closed（`update_if_revision` 真查库 → 503/409），
   而恢复协调器**不会**因此重载（`_dependency_needs_recovery("identity")` =
   `not is_available` 为假）。⇒ **R5 的强度只到"装载期真值"**：它保证「装载/重载失败 ⇒
   不就绪」，不保证「缓存与权威文档逐轮比对」。要闭合这一段需要 freshness 机制
   （按 revision/updatedAt 轮询比对 + 防抖），属**独立设计**，不在本批（§77 禁止为
   两条 finding 新增 debounce/hysteresis）；已登记为 follow-up。
2. 本地模式（无 repo）`is_available` 恒 True：该模式下 `storage_path` 无生产调用方
   （`Agent` 与 `create_app` 两条装配路径都不传），且本地模式没有异步装载窗口，
   故「loading ⇒ not ready」这一行在本地模式不适用；
3. `/ready` 的 identity 门禁是**进程内状态读取**，不重新加载、不重试、不写状态；
4. `/ready` 的判据是 `is True`（显式布尔）：真值非布尔（duck-typed 替身的
   `is_connected = 1`）会被判 **not ready**。生产 `Repository.is_connected` 是
   `self._db is not None`（真布尔），故这是**有意的保守方向**而非缺陷——宁可拒绝
   一个无法确认的"可用"，也不让替身属性冒充健康（守卫：
   `test_ready_503_when_availability_is_truthy_but_not_bool`）。

### 3.2 Provider capability truth（PC）

来源：[2026-09-18 Batch 5 执行记录](../review/2026-09-18-Batch5-provider-capability-truthfulness-执行记录.md)。
canonical finding：**N-10**（预检说可用、运行期必 `NotImplementedError`）、
**F6**（provider test 测的不是跑的那个）、**F7**（`/debug` 的 `*_ok` 两个权威）。

**五层必须分开，不得互相冒充**（一个 provider 配置能走多远）：

| 层 | 问题 | 权威 |
|---|---|---|
| `CONFIGURED` | 配置/env 里有这么一条吗 | `config.<svc>.model` / `providers.yaml` |
| `RESOLVED` | 最终 provider/model 解析成什么 | 各 Module registry 的 `provider/model` 解析（含 custom 参数解析） |
| `INSTANTIATED` | 后端对象建出来了吗 | `agent.llm` / `agent.stt` / `agent.tts`（运行期句柄） |
| `SUPPORTED` | **本仓这份代码**实现了该 capability 吗 | 各 registry 的 `is_implemented()`（与 `create()` 守卫同常量） |
| `REACHABLE` | 远端此刻真的可用吗 | 只有 `--full` / `POST /api/providers/test` 能回答（需要网络） |

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **PC1** | **报告的能力必须对应运行期真正在跑的那个 provider/model**：provider test 的缺省目标解析到 `agent.<svc>.model`（后端未连接才回退 config 默认值），不得测 bootstrap 配置 | `core/input/tests/test_provider_test_target_authority.py`（8 例，含真实 `switch_stt` A→B 与端到端端点） | `make test-normal` | **push/backend** |
| **PC2** | **实例化受 capability 门控**：`providers.yaml` 里声明但本仓未实现的 provider，构造期即 `NotImplementedError`；`is_implemented()` 与工厂守卫读**同一常量**，且两条装配路径还各有一道 1s 连通性探测才挂载 ⇒ "句柄存在 ⇒ 实现支持"。（依据是**工厂守卫 + 挂载期探测**，**不是** ABC——ABC 只保证方法存在，不保证有实现；Challenger 2026-09-18 实测一个把抽象方法实现成 `raise NotImplementedError` 的 `STTBackend` 子类照样能实例化） | `Module/config/tests/test_preflight_provider_capability.py::TestRuntimeFactAnchor`、`core/agent/tests/test_service_availability_truthfulness.py::TestInstantiatedImpliesImplemented`（遍历 providers.yaml） | `make test-normal` | **push/backend** |
| **PC3** | **静态预检 network-free**：`--check` 只判「配置 + resolver + capability 静态有效性」；"实现支持但服务离线"仍 PASS，连通性归 `--full` / provider test | `Module/config/tests/test_preflight_provider_capability.py::TestStaticCheckStaysNetworkFree`（含 socket 哨兵） | `make test-normal` | **push/backend** |
| **PC4** | **`/debug` 与 capabilities 是纯投影**：只报运行期已知状态，**不得**为了"更真实"主动探活（昂贵/有副作用/可能计费/拖慢端点） | `core/agent/tests/test_service_availability_truthfulness.py::TestAvailabilityReportingNeverProbes` | `make test-normal` | **push/backend** |
| **PC5** | **一个上报事实一个权威**：`/debug` 的 `tts_ok`/`stt_ok`、capabilities 的 `available`、provider test 的目标全部由**运行期句柄**得出，不允许第二份缓存标志 | 同上 `TestAvailabilityHasOneAuthority`（失败热切换后三面同答案）、`TestNoHandleMeansNotAvailable` | `make test-normal` | **push/backend** |
| **PC6** | **unsupported 不得先报成功**：未实现的 provider 在预检/`--full`/下拉里都不得报 usable（LLM FAIL、STT WARN、`--full` 不再 `[PASS] 跳过`），且不得伪装成凭据或连通性错误 | `Module/config/tests/test_preflight_provider_capability.py`（N10-1/N10-5）、`core/input/tests/test_provider_registry.py::test_unimplemented_provider_excluded_even_with_models` | `make test-normal` | **push/backend** |

**`*_ok` 的语义（PC4/PC5 的落地口径）**：

```text
*_ok = 运行期句柄存在
     = runtime usable by construction
     ≠ 远端 provider 此刻可达
```

允许这样定义，是因为**构造期已经把 capability 门住了**（PC2：未实现的 provider 建不出来，
STT/TTS 后端又都是 `ABC`），也因为 `capabilities.services.*.available`（下发给前端的
产品契约）本来就用的这个判据。**没有**任何进程内状态能回答"远端此刻可达"——
`/ready` 的教训（§3.1）是"不要拿代理量冒充可用"，而这里的边界是
"不要拿一次网络探测冒充进程状态"。

**范围纪律（不泛化）**：本条只覆盖 LLM/STT/TTS 三个 provider 子系统的
「配置 → 解析 → 实例化 → capability → 上报」这一条链，不把 embedding / vectordb /
documentdb 拉进来（它们有同形的 `NotImplementedError` 守卫，但当前没有"上报假绿"的
报告面），也不建立通用的 ProviderCapability 框架。

### 3.3 Conflict metadata truth（CM）

来源：[2026-09-18 Batch 6 执行记录](../review/2026-09-18-Batch6-conflict-metadata-truthfulness-执行记录.md)。
canonical finding：**N-3**（`PUT /api/me` 的 409 `details.currentRevision` 读进程内
陈旧缓存 ⇒ 谎报；客户端按它重试必然再冲突）。

**权威归属（一句话）**：乐观并发冲突时，**当前 revision 的唯一权威是权威持久化
存储里的那条记录**（`user_identity_profiles.revision`，经 repository 读取），
不是请求负载、不是进程内缓存、不是 `expected + 1`、也不是路由自己重新算一次。

| 关注点 | 权威 | 说明 |
|---|---|---|
| 请求里的 `expectedRevision` | 请求负载 | 客户端**声称**的状态；冲突后不得回显成"现值" |
| `UserIdentityStore._cached` | 运行时读缓存 | 读路径服务用；多 writer 时按定义可能陈旧 |
| 持久化当前 revision | 权威库文档字段 | **唯一权威**；读出走 `UserIdentityRepository.get()` |
| CAS 冲突判定 | repository 的 `update_one(filter={user_id, revision: expected})` | `modified_count == 0` ⇒ 未命中 |
| 409 的 `currentRevision` | repository 在冲突点回读的权威现值 | 上层只投影；未知则**省略** |

**语义（必须写清，防止被读成更强的承诺）**：`currentRevision` = **构造冲突响应那一刻**
可观察到的权威现值（response-time），**不**保证等于"击败本次写入的那个 writer 的
revision"。Mongo 的 `update_one` 在 filter 未命中时只返回计数（`matched_count=0`），
**拿不到**冲突瞬间的 actual revision；用一次额外 read 去假装拥有这种更强语义正是
§12 禁止的。并发写者 C 在 CAS 与回读之间提交时，上报的是 C 之后的值——那也正是
客户端恢复所需要的值。

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **CM1** | 冲突元数据**源自权威持久化状态**：409 的 `details.currentRevision` 等于权威库里那条记录的 `revision` | `core/input/tests/test_me_conflict_metadata_authority.py::test_cmt1_conflict_reports_authoritative_revision`、`tests/real_mongo/test_real_identity_conflict_metadata.py::test_real_conflict_reports_authoritative_revision` | `make test-normal` / `make test-real-mongo` | **push/backend** + **push/real-mongo** |
| **CM2** | **陈旧请求/缓存 revision 永不冒充权威现值**：不得返回客户端提交的 expected、store 读缓存、冲突前旧值、`expected + 1`；多次 intervening write 后报的是权威现值 | `::test_cmt2_stale_cache_never_reported_as_current`、`::test_cmt3_multiple_intervening_writes_not_expected_plus_one`、`::test_cmt_repeated_conflict_tracks_authoritative_state`、`core/storage/tests/test_user_identity_repo.py::test_update_if_revision_conflict_reads_authority_not_caller_state`、`tests/real_mongo/…::test_real_multiple_writes_never_expected_plus_one` | `make test-normal` / `make test-real-mongo` | **push/backend** + **push/real-mongo** |
| **CM3** | **失败的乐观更新不得改动权威状态，也不得隐式重试**：冲突路径不重写、不 bump revision、不 save、不 repair、不 refresh 后重放；winner 的 payload 与 revision 原样保留 | `::test_cmt4_failed_writer_preserves_winner_state` | `make test-normal` | **push/backend** |
| **CM4** | **同一权威记录上的不同 writer 报同一真相**：两个独立 store/repo 实例、乃至跨进程 writer 写入后，落败方的 409 报的都是权威现值 | `::test_cmt_order_perturbation_both_orders_agree`、`tests/real_mongo/…::test_real_cross_process_writer_is_the_reported_authority` | `make test-normal` / `make test-real-mongo` | **push/backend** + **push/real-mongo** |
| **CM5** | **不知道就不说**（fail-closed）：权威现值读不到（记录已不存在）→ **省略** `details.currentRevision`；权威回读失败 → 503 `STORAGE_UNAVAILABLE`。**绝不**回退成缓存值假装权威 | `::test_cmt_missing_record_reports_no_fabricated_revision`、`::test_cmt_read_failure_after_conflict_fails_closed_503`、`core/storage/tests/test_user_identity_repo.py::test_update_if_revision_conflict_without_record_reports_unknown` | `make test-normal` | **push/backend** |

**范围纪律（不泛化）**：本条只覆盖「乐观并发冲突时的**上报值**」，不覆盖 cache
freshness（E1：外部写入者改库后热缓存不自动刷新 ⇒ `GET` 可陈旧、`PUT` 可能持续 409）。
冲突路径读一次权威库**不**构成全局 freshness 机制，也不做 conflict→cache refresh。

**CAS 本身未被削弱**：filter 仍含 `revision: expected`，`$set` 在同一原子操作内
bump 版本；没有去掉 revision filter、没有 last-write-wins、没有"取最新 revision
静默重试"。本批只改**报告真相**，不改并发保护强度。


---

## 4. User-visible semantics（U）

来源：[2026-09-17 Batch 1.8 执行记录](../review/2026-09-17-Batch1.8-user-visible-terminal-semantics-执行记录.md)。

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **U1** | 用户可见 outcome **等于** logical terminal outcome | `core/agent/tests/test_task_outcome_user_semantics.py`（19 例，端到端：真 mailbox → 真 `drain_mailboxes_once` → 真 EventBus → 真 `_handle_task_result` → 真 `_prepare_think_input`） | `make test-quick` / `test-normal` | **push/backend** |
| **U2** | 内部细节**可以压缩，但不得反转**：细节可压扁，**失败绝不翻成功** | 同上（每条"不得宣称成功"都配一条**正向分类断言**） | 同上 | **push/backend** |
| **U3** | **live 与 replay 语义相等** | 同上 | 同上 | **push/backend** |
| **U4** | `failed ≠ completed`（两者不得互相冒充） | 同上 | 同上 | **push/backend** |
| **U5** | `cancelled` **独立可辨**（不并入 failed/completed） | 同上 | 同上 | **push/backend** |

**U 的单一映射点**：内部终态 → 用户级语义只经**一个**函数（Batch 1.8 §1 第 5 条）。
这样"映射正确"只需在一个地方成立，而不是在每个消费方各自成立。

### 4.1 Pagination completeness（PG）

来源：[2026-09-18 Batch 8 执行记录](../review/2026-09-18-Batch8-pagination-completeness-执行记录.md)。
缺陷形状（canonical backlog **P2-01r**）：会话列表的数据源是
`SessionRepository.list_all()` 的 `find({}, sort=[("updated_at", -1)], limit=200)`
——**单页上限被当成了数据集上限**；`SessionManager.list()` 在这个 ≤200 的窗口里
做游标过滤与搜索，于是第 201+ 条记录既翻不到也搜不到，而最后一页**谎报**
`has_more=false`（客户端据此停止）。设计文档（批次 3 spec §4「列表游标（keyset）」）
从未允许任何数据集上限——这是"实现缺失"，不是产品决策。

> **A client following the published pagination contract can eventually reach every
> visible record in a stable result set.**

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **PG1** | **可达性完备**：稳定数据集里，客户端只依据公开合同（`has_more` / `cursor`）继续请求，最终能拿到**全部**可见记录；不得存在"前 N 条可见、其余永久不可达" | `core/input/tests/test_sessions_pagination.py`（21 例：201/225 条 + 边界矩阵 0/1/50/51/199/200/201/225 + search 分页）、`core/memory/tests/test_session_manager_pagination.py`（覆盖层/离线/竞态）、`core/storage/tests/test_session_repo_pagination.py`（存储层原语） | `make test-quick` / `test-normal` | **push/backend** |
| **PG2** | **单页上限 ≠ 数据集上限**：`limit` 只约束一页；取数必须**在存储层分页**（keyset 谓词 + 排序 + `limit` 下推），不得"预取全量再切片"，也不得"取前 N 条再分页" | `core/storage/tests/test_session_repo_pagination.py::TestQueryShape::test_page_read_never_fetches_more_than_the_page`、`::test_sort_and_cursor_predicate_are_pushed_down`、`core/memory/tests/test_session_manager_pagination.py::TestReachabilityBeyondTwoHundred::test_list_does_not_prefetch_the_whole_dataset`（结构性：用户列表路径碰 `list_all` 即红） | 同上 | **push/backend** |
| **PG3** | **不重不漏 + 稳定序**：完整分页的 ID 并集 **等于**权威可见集合、两两交集为空；排序 `updated_at DESC, session_id ASC` 的 tie-break **必须下推到查询**（相同时间戳跨页不重复不遗漏） | `test_sessions_pagination.py::TestBoundaryMatrix`、`::TestReachabilityBeyondTwoHundred::test_two_hundred_and_one_sessions_all_reachable`、`test_session_repo_pagination.py::TestStorageLevelPagination::test_identical_timestamps_tie_break_across_boundaries`、`tests/real_mongo/test_real_session_pagination.py::test_identical_timestamps_use_storage_level_tie_break` | `make test-quick` / `test-normal` / `make test-real-mongo` | **push/backend** + **push/real-mongo** |
| **PG4** | **元数据自洽**：`has_more=true ⇔ cursor 存在`；走到末尾是"空页 + has_more=false"（200），不是 404/400；`limit` 被夹取只影响**单页**大小，不改变可达性 | `test_sessions_pagination.py::TestMetadataCoherence`、`::TestPageSizeCapIsNotDatasetCap` | 同上 | **push/backend** |
| **PG5** | **单文档读与列表窗口无关**：`GET /api/sessions/{id}` 的摘要按 ID 直读（不再依赖"最近 N 条"列表），否则第 201+ 条的详情会兜底成 `updatedAt: ""`（前端契约要求非空） | `core/memory/tests/test_session_manager_pagination.py::test_get_summary_is_authoritative_beyond_the_old_cap`、`core/storage/tests/test_session_repo_pagination.py::TestSummaryReads`、`tests/real_mongo/test_real_session_pagination.py::test_single_document_summary_is_window_independent` | `make test-quick` / `make test-real-mongo` | **push/backend** + **push/real-mongo** |
| **PG6** | **失败的真相**：翻页中途的存储读失败**不得**折成"短页 + has_more=false"（那是把故障谎报成"列表到底了"）；离线降级只在首页（或以同一个降级页游标续翻）成立 | `test_session_manager_pagination.py::TestAdversarialRegressions::test_mid_walk_storage_failure_is_not_reported_as_end_of_list`、`::test_offline_repo_falls_back_to_local_summaries_with_pagination` | `make test-quick` / `test-normal` | **push/backend** |

**为什么"下推"而不是"提高 200 上限"**：把 `limit=200` 改成 2000 只是把边界往后挪
（数据集总会超过它），并且每次列表请求都要读全量文档（会话 doc 内嵌消息窗口）。
PG2 因此把"存储层分页"本身当成不变量：修复的验收判据之一是**单页读取量不得随
数据集增长**（`test_page_read_never_fetches_more_than_the_page` + 结构性守卫）。

**为什么 tie-break 必须在查询里**：内存 fake 的排序是 Python Timsort（同值稳定），
真 Mongo 对相等排序键**不作任何顺序保证**——只按 `updated_at` 排序时，相同时间戳
的记录在页边界可能被重复或漏掉。该不变量由真库用例证明（`push/real-mongo`）。

**已知边界（本批登记，未修）**：缺 `updated_at`（字段缺失/null）的历史文档在
keyset 谓词下不可达——真库实测（`$lt` 对缺失/null 走 **type bracketing**，与内存
fake 行为一致）5 正常 + 3 坏文档、每页 2 时只走出 5 条就"收敛"；若某页末尾正好是
这类文档，`_encode_cursor` 会写出 `updated_at=""` 的游标，下一页谓词匹配不到任何
字符串文档 ⇒ 该页之后的记录被掐断。本批只保证**它不连坐健康记录**（投影层把非
`str` 归一为 `""`，避免 `None > str` / `max(None, str)` 让整份列表静默退化）。
仓内没有任何写入者会产生这种文档。见 [todo.md](../../todo.md)（`KB-15`）与
`test_missing_updated_at_row_is_unreachable_registered_gap`（xfail 钉住）。

### 4.2 Speaking 生命周期（SP）

来源：[2026-09-18 Batch 9 执行记录](../review/2026-09-18-Batch9-speaking-state-recovery-执行记录.md)。
缺陷形状（canonical backlog **KB-6**）：`TTSStreamManager.wait_all()` 没有超时预算，
而"某一轮讲话真正结束"的唯一信号就是它返回（`FSMBackedPhaseController.finish_turn`）。
一旦有**任何**门控（`_release_events[seq]`）永远不会被 `set`，`wait_all()` 就永不返回，
FSM 永久停在 **SPEAKING**——回合仍持有 `_process_lock` ⇒ 新用户消息只进
`_pending_messages`（只在 `STATE_CHANGE→IDLE` 时出队），**纯文字聊天里 Agent 不再回复**，
且"再说一句"救不回来（新消息不触发 interrupt），只能等一次 VAD/停止按钮或重启。

三条挂起源：
① `_download_and_forward` 的两个**早退分支**在 `try/finally` 之外直接 `return`（S2-G3 已修）；
② `reset_for_turn` 直接 `clear()` 门控 dict，让已持有旧 `Event` 的等待者永久挂起（S2-G3 已修）；
③ **任务在"首个 event-loop step 之前"被取消** ⇒ 协程体一行都不执行、早退分支与 `finally`
全都没进 ⇒ 放行从未发生（Batch 9 修）。③ 的可达路径是用户在本轮播放中点掉 TTS 再打开
（`Agent.set_service("tts", False/True)` → `set_enabled(False)` 的同步段里 `cancel_all()`；
`BaseEventLoop._run_once` 先 poll I/O 再跑本批 ready，所以 WS handler 的 step 能与
"本轮入队句子"的 step 同批，而 TTS 任务的 step 要等下一批）。

**SPEAKING 的语义**（本批核对，未重新定义）："Agent 正在向用户输出本轮的 response"，
**不是**"扬声器正在出声"——首句到达即进入（与 TTS 是否可用无关），退出由**服务端输出工作**
的完成决定，客户端播放不被追踪。

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **SP1** | 每次进入 SPEAKING 都有**明确的退出 owner**（正常：`finish_turn`；异常/打断：`handle_interrupt`），不存在"进了没人负责出"的路径 | `core/cognitive/tests/test_speaking_lifecycle.py`（16 例，真实 `process()` → 真实 `AgentFSM.transition`；每条先断言**确实进过 SPEAKING**再断言终态） | `make test-quick` / `test-normal` | **push/backend** |
| **SP2** | success / failure / cancel / interrupt / empty / shutdown **都不能**留下 `state == SPEAKING` 且"当前输出工作 == 无" | 同上逐场景；`assert_no_speaking_work_left()` 同时断言无未落定 TTS 任务、无未释放门控 | 同上 | **push/backend** |
| **SP3** | 旧 speaking epoch 的**迟到完成**不得覆盖新一轮（不得把它切成 IDLE，也不得提前放行它的门控） | `test_speaking_lifecycle.py::test_stale_completion_from_interrupted_turn_cannot_overwrite_next_turn`；`core/adapter/tests/test_tts_adapter.py::test_stale_task_from_previous_turn_does_not_forward` | 同上 | **push/backend** |
| **SP4** | 完成由**生命周期完成**驱动，**不是**"队列空"/"任务已创建"：输出工作还活着时不得提前退出；门控链必须闭合（放行不得依赖"协程体是否运行过"） | `test_speaking_lifecycle.py::test_speaking_does_not_exit_while_output_work_is_alive`；`core/adapter/tests/test_tts_adapter.py::test_prestart_cancel_releases_successor_gate`（PRE_FIX_RED） | 同上 | **push/backend** |

**明确不做**（本批的取舍，避免"看起来更稳"的假修复）：不给 `wait_all()` 加超时、
不加 SPEAKING watchdog / 超时复位、不新增第二个 generation 计数器、不引入说话生命周期协调器。
超时只会把"永久卡死"换成"提前离开 SPEAKING 而音频仍在播"——那违反 SP4 与 SPEAKING 的语义。

### 4.3 Thinking 生命周期与取消边界（TH）

来源：[2026-09-18 Batch 10 执行记录](../review/2026-09-18-Batch10-thinking-state-cancellation-recovery-执行记录.md)。
缺陷形状（canonical backlog **KB-22**）：`CognitiveLoop.process()` 在 `_prepare_turn()`
**之前**就取得回合锁（`async with self._process_lock`），但取消边界（`except
asyncio.CancelledError → _handle_interrupt()`）原本只覆盖 `_think_loop` 那一段。而
`_prepare_turn` 在 `await self.phases.enter_thinking()`（FSM 已是 THINKING）**之后**
还有一个真挂起点——`await self._generation_registry.register(...)`（Mongo upsert），
它只被 `except Exception` 包着，而 Python 3.8+ 起 `asyncio.CancelledError` 是
**`BaseException`**（实测 `__mro__ = (CancelledError, BaseException, object)`），
外层 `try` 又只 `except (CharacterBusyError, StorageUnavailableError)`。

于是落在该窗口的取消**一路逃出 `process()`**：`async with` 退出时**释放锁**，
canonical 收口 `_handle_interrupt()`（唯一做 `THINKING → INTERRUPTED → IDLE` 的地方）
永不执行 ⇒ FSM 永久停在 **THINKING**。`status.is_accepting_input` 恒假 ⇒ 之后**每一条**
用户消息都走 `process_message` 的排队分支、被 ACK `accepted` 后进 `_pending_messages`，
而 pending 只在 `STATE_CHANGE → IDLE` 出队（那个事件再也不会来）⇒ 进程活着、EventBus
活着、消息能进队列，但**没有任何回合再启动**——用户感知为 Agent **永久失聪**。
仓内唯一恢复点是 `Agent.start()` 的 `fsm.reset()`（`core/agent/lifecycle.py:204`）。

**THINKING 的语义**（本批核对，未重新定义）：回合**已被接受、正在做本回合的准备工作与
认知工作**（选项 C）——`enter_thinking()` 在 generation 建档与 prompt 组装**之前**，
把会话解析/身份快照/角色 runtime 换绑都算在内。它是"本回合已开始"的对外声明，
所以**不**把它挪到 prepare 的尾部（见下方"明确不做"）。

**ownership 判据**（本批的核心决定）：收口只在"**本回合**确实已经开始"时执行，判据是
`_generation_ctx`——它在 `enter_thinking()` **之前**建立、在 `_prepare_turn` 入口清空，
且 `_prepare_turn` 全程在 `_process_lock` 之内（不可能混入第二个回合的 ctx）。
**明确不用** `fsm.state == THINKING`：那分不清"我的 THINKING"与"别人的 THINKING"，
也分不清"本回合还没开始、相位上留着上一回合的 `IDLE_BACKGROUND_TASK`"。
ctx 赋值与 `enter_thinking()` 的 FSM transition 之间**没有 await**，因此"ctx 非 None"
与"FSM 已经/即将是 THINKING"**严格等价**——即使某个 `on_change` 订阅者在
`enter_thinking()` 的 `_notify` 上挂起（`on_change` 是公开订阅点，
签名就是 `Callable[[str], Awaitable[None]]`），判据也不会误判。这条由
`test_cancel_inside_enter_thinking_notify_still_recovers` 钉住（Challenger A3b）。

| ID | Invariant | Test | Gate | CI |
|---|---|---|---|---|
| **TH1** | 每次进入 THINKING 都有**turn-scoped 的终态 owner**（含 prepare 窗口）：接受/失败/取消/打断/被取代都必须有明确责任方把它推离 THINKING | `core/agent/tests/test_thinking_cancel_recovery.py`（4 例，真 `Agent.start()`/`process_message`/EventBus/INTERRUPT）；`core/cognitive/tests/test_thinking_cancellation.py`（8 例，真 `process()` → 真 `AgentFSM.transition`） | `make test-quick` / `test-normal` | **push/backend** |
| **TH2** | 本回合开始之后的取消**或普通异常**都**不能**留下"state == THINKING 且本回合已无存活 owner"：收口必须走 canonical 路径（`THINKING → INTERRUPTED → IDLE`），**不得**用 `finally: set_idle()` 之类的补丁（`THINKING → IDLE` 在 `VALID_TRANSITIONS` 里是**非法边**） | `test_thinking_cancellation.py::test_cancel_after_thinking_entry_recovers_via_canonical_interrupt`（PRE_FIX_RED，断言状态序列 `["THINKING","INTERRUPTED","IDLE"]`）、`::test_cancel_during_error_recovery_is_also_covered`（PRE_FIX_RED，取消落在异常收口自身）、`::test_generic_exception_after_turn_started_still_recovers`（PRE_FIX_RED，普通异常同样有 owner）、`::test_cancel_inside_enter_thinking_notify_still_recovers`（PRE_FIX_RED，判据对挂起的 `on_change` 订阅者免疫）、`test_thinking_cancel_recovery.py::test_cancelled_thinking_turn_recovers_and_next_message_is_processed`（PRE_FIX_RED） | 同上 | **push/backend** |
| **TH3** | 旧回合的 cleanup **不能**覆盖新回合：A 的收口与 B 的 `enter_thinking()` 之间由 `_process_lock` 串行化——B 进不来，A 也不会迟到 | `test_thinking_cancellation.py::test_old_turn_cleanup_cannot_overwrite_newer_turn`、`::test_cleanup_scoped_by_ownership_not_by_fsm_value`（用 FSM 取值当 ownership proof 的实现会红） | 同上 | **push/backend** |
| **TH4** | FSM 真相 / 回合任务所有权 / `_process_lock` 所有权三者一致：取消后不得出现"锁已释放 + 无存活 owner + 仍是 THINKING"，也不得让停机语义被"复活成可接受输入的 IDLE"破坏 | `test_thinking_cancel_recovery.py::test_stop_during_thinking_window_does_not_strand_the_agent`（PRE_FIX_RED：断言相位不留在 THINKING、`run_state == STOPPED`、停机窗口拒收新消息）、`test_thinking_cancellation.py::test_cancel_before_thinking_entry_is_safe`、`::test_pre_thinking_cancel_preserves_other_turn_background_phase` | 同上 | **push/backend** |

> **TH4 的成立条件（必须一起读）**：TH4 在"收口能在停机 drain 预算（3s）内走完"时成立。
> 若收口自身被**第二发** `task.cancel()` 打断（`AsyncTaskScope.drain` 超时后的直接取消，
> 绕过 `cancel_current()`），相位会停在 THINKING/INTERRUPTED 而 `run_state == STOPPED`——
> Challenger 已用"`mark` 卡住 > 3s"确定性复现（`stop elapsed=3.00s fsm=thinking
> run_state=STOPPED`）。那是 **KB-23 同族**（第二发取消打断收口链），本批按 scope 不修。
| **TH5** | 相位忙时被 ACK 接受的消息（pending）在恢复后必须**经既有 drain 机制**重新获得处理权（`STATE_CHANGE → IDLE` → `_handle_state_change` 出队），不新增调度器/轮询 | `test_thinking_cancel_recovery.py::test_message_queued_during_recovery_window_is_processed`（PRE_FIX_RED，生产取消源 = 模型热切换） | 同上 | **push/backend** |

**明确不做**（本批的取舍，避免"看起来更稳"的假修复）：不把 `enter_thinking()` 挪到
prepare 尾部（会改变 UI/打断/观测时机，而根因是取消边界不是入口时机）；不写
`finally: set_idle()`（A 的迟到收口会打掉 B）；不新增 `_thinking_epoch` / 第二套
生命周期计数器（现有 `_generation_ctx` + `_process_lock` 已足够）；不加 watchdog /
超时复位 / `asyncio.shield` 包整个 `process()`（那会破坏用户打断）；不改 FSM 转换表、
不改 EventBus 优先级、不改 pending 出队机制。

**已知边界（本批登记，未修）**：

- **第二发取消打断收口链**（KB-25，KB-23 同族）：收口是一条长 await 链
  （`turn_cancelled → mark → cancel_desktop → typing → _notify → sleep(0.05) → …`），
  任何一发**直接** `task.cancel()` 都能把它打断，留下 THINKING **或** INTERRUPTED。
  生产实例是停机 `_turns.drain(timeout=3.0)` 超时后的取消（Challenger E2/F1/F2/F2b 实测）。
  公开 API `cancel_current()` 的第二发是 one-shot、无害（F3）。
- **入口之前的取消**（O1）：本回合已 ACK 的消息静默结束（没有任何 `generation.failed` 终态）。
  相位不受影响，故不属于 KB-22。
- **`_prepare_turn` 的回合级写入不回滚**（O3）：取消传播时（入口之前）`_session` /
  `_prompt` / tool_context / `_tts_mgr.reset_for_turn` / `_clock` 的写入保留；`_clock`
  会残留成非 None（消费者都有"本回合会重设"的前提，当前无害）。
- **`IDLE 已广播 / 锁未释放`窗口**（O2）：若收口/回合在 post IDLE 之后继续挂起（例如某个
  会挂起的 `on_change("idle")` 订阅者），pending 出队 spawn 的回合会卡在锁上，而
  "下一条消息"可能把它当作在途回合取消掉 ⇒ 已 ACK 的排队消息被丢弃且顺序被抢。
  需要"有订阅者挂起"这一前提（仓内今日没有），生产形态还要求收口后仍持锁挂起。

---

## 5. 架构与静态面

| Invariant | Test / Gate | Gate 命令 | CI |
|---|---|---|---|
| `Module/` 生产代码与 `frontend_contract/` 不得反向依赖 | `make check-arch`（R1） | `scripts/check_architecture.py` | **push/backend** |
| 领域/基础设施层不得依赖 `core.input` | `make check-arch`（R2） | 同上 | **push/backend** |
| 禁止跨包导入下划线私有符号 | `make check-arch`（R3，allowlist 空） | 同上 | **push/backend** |
| 不得新增 **eager** import 环 | `make check-arch`（R4） | 同上 | **push/backend** |
| 裸 `create_task` 必须逐点登记所有权 | `make check-arch`（R6，19 点 / 22 处） | 同上 | **push/backend** |
| **跨包私有访问（含私有方法）必须公开化或显式标注** | `core/tests/test_cross_package_privacy.py`（5 例） | `make test-normal` | **push/backend** |
| 输入层任何未标注私有穿透即红（比全仓更严） | `core/input/tests/test_layer_discipline.py` | `make test-normal` | **push/backend** |
| **索引选项声明表只许指向真实 schema 实体**：集合键/索引名写错或整条漏登记 ⇒ 该集合选项**一个都不比**却照报健康（B3-CB-2） | `core/storage/tests/test_schema_drift.py::TestExpectedIndexOptionsPinnedToCode`（9 例；判据 = 生产 `ensure_index(...)` 的 **AST**，非第二张手写表） | `make test-quick` / `test-normal` | **push/backend** |
| **上传目录在调用期解析**：import 期固化 ⇒ 测试的环境隔离静默失效、文件写进真实图片目录（B3-CB-7） | `core/input/tests/test_upload_dir_isolation.py`（4 例：真实上传 + 正对照 + 真实目录 delta=0 + fresh 子进程） | `make test-quick` / `test-normal` | **push/backend** |
| 具名超时预算必须表态（外层 ≥ 内层 / 叶子 + 理由） | `make check-timeouts` | `scripts/check_timeout_budget.py` | **push/backend** |
| 契约枚举成员必须有生产者、字段有读必须有写 | `make check-contract` | `scripts/check_contract_reachability.py` | **push/backend** |
| 契约产物无漂移（ts / schema / raw_ts / golden frames） | `make check-types` | `frontend_contract --check` 等 | **push/backend + frames** |
| PR 可达 job 不得 self-hosted、不得引宿主凭据 | `make check-ci-isolation` | `scripts/check_ci_isolation.py` | **push/backend** |
| 每个 workflow 必须声明 `permissions:` | 同上 | 同上 | **push/backend** |
| self-hosted 清理必须密闭 + 双守卫 + fail-closed | 同上（6 处清理点） | 同上 | **push/backend** |
| 测试集合代数：`Quick ∪ remainder == Normal` 且交集空 | `make check-test-sets` | `scripts/check_test_sets.py` | **push/backend** |
| 每个测试必须有分层 marker、快慢层不得交叠 | 根 `conftest.py::pytest_collection_modifyitems` | 任何 pytest 进程内断言 | **push/backend** |
| 依赖锁与声明一致 | `make check-env-lock` | `scripts/check_env_lock.py` | **push/backend** |
| 无凭据形态入库 | `make check-secrets` | `scripts/check_secrets.py` | **push/backend** |
| 依赖无已知漏洞（Medium+） | `make audit` | pip-audit + bandit | **push/backend** |

---

## 6. Self-hosted runner 信任边界

| Invariant | 实现 | 强制点 | CI |
|---|---|---|---|
| 不受信代码**永不**落在持久 self-hosted runner 上 | 所有 job `if: github.event_name == 'push'`（或 `workflow_dispatch`）；无 `pull_request` 触发器 | `make check-ci-isolation` | **push/backend** |
| 宿主凭据（`.env`）不跨 job 泄漏 | 每个 self-hosted job 开头密闭清理 workspace + `Load .env` 依赖该清理 | 同上（6 处，`TestWorkspaceWipeRecognition` + `test_every_self_hosted_cleanup_site_is_fail_closed`） | **push/backend** |
| 清理 fail-closed（显式检查退出码，`set -e` 不算） | 6/6 清理点 | 同上 | **push/backend** |
| `runs-on` 无法静态判定时**拒答** | 表达式 `runs-on` → 违规 | 同上 | **push/backend** |
| 无死 job（守卫引用的事件必须在 `on:` 声明过） | `_EVENT_GUARD_RE` 与 `_is_push_only` **共用同一正则** | 同上 | **push/backend** |

安全模型与「为什么本仓不开放 PR」见 [`docs/ci.md`](../ci.md)。

---

## 7. 已知假保障（有测试 / 有不变量，但自动化跑不到）

**这一节是本文存在的核心理由。** 以下项**不是**"已覆盖"，列出来是为了让它们
无法静默退化：

| 项 | 现状 | 风险 | 处置 |
|---|---|---|---|
| **T1/T2 并发攻击扫描**（`probe_t2_race.py`，20 例） | 探针在 `/tmp`，**未入库** | 并发竞态回归不会被自动发现 | 见 [todo.md](../../todo.md) 的 Batch 2.3 follow-up |
| **`make test-full`（含 slow）无自动执行点** | 只在 `nightly.yml` 手工 `workflow_dispatch` 触发 | slow 层回归只在人工注意时才发现 | 若需覆盖，须先有隔离 runner 或接受 push 成本；见 `docs/ci.md` |
| **`external` 层（真实 LLM/DB）无自动执行点** | 同上（`nightly.yml` 的 `external` 输入） | 外部集成回归不自动发现 | 同上；**刻意 opt-in**（真实费用） |
| ~~`external` opt-in 门禁只覆盖 `Module/`~~ | **已修**（2026-09-17）：门禁并入根 `conftest.py`，现覆盖全仓；`core/tests/test_external_optin_gate.py` 钉住范围 | 修前 `core/` 的 external 用例**未 opt-in 也真跑**（真实 DeepSeek 调用、计费） | — |
| **`real-mongo` 层** | push 侧有 job（`ci.yml:real-mongo`） | ✅ 有覆盖 | — |
| **私有守卫的"同名遮蔽"**（Challenger 2026-09-17 实测） | 在**访问方包内**定义一个同名 `def _x` 即可让该属性名全仓免检（9 处违规塌成 1 处）；扫描面也只含 `core`+`Module` | 守卫被当成"完备"时会漏掉真实穿透 | **接受**（属"按属性名归属近似"的固有代价，模块 docstring 已记载）；定位仍是**回归网**，见 [todo.md](../../todo.md) |
| **清空形态识别的长尾** | 规范 `find … -exec rm` 与 4 种 `$GITHUB_WORKSPACE` 写法 + 当前目录形态已覆盖；`xargs`/`for` 循环/`git clean` 未覆盖 | 间接清空写法不受三道清理子句约束 | **接受**（本仓 6 处清理点全用规范写法，无实例）；见 todo.md |
| **`if:` 守卫是 token 存在性** | `… \|\| true` 会被判成受信 push-only | 静态判据无法判定布尔语义 | **接受**（保守方向是"当 PR 可达"，此处相反但需构造）；见 todo.md |
| **删除 `__init__.py` 重导出的风险** | F401 已豁免 `__init__.py`；重导出由 `__all__` + 直接导入覆盖；**无自动"公开面冻结"测试**（`Module/workflow_contract` 有，其余没有） | 误删重导出曾真实发生（Batch 2：`SessionId`/`asdict` → 59 模块 ImportError） | 见 todo.md |
| **`core/tests/test_doc_counts.py` 的覆盖范围** | 该文档数字门禁**不含根 `README.md`**，且正则只认粗体 `**N 个同名 @property**` ⇒ 根 README 的「39 个 @property」（实际 46）永远抓不到；本文也未登记它 | 文档数字失真不会被自动发现 | 见 todo.md（Batch 3 登记） |

---

## 8. 维护纪律

1. **新增不变量必须同时给判据**。只写 prose 的条目一律不收——本文要么指向一个
   真实存在的测试/门禁，要么进 §7。
2. **改判据要同步改本文**。测试改名/删除后，本文的映射会变成谎言；CI 不检查本文
   （它与代码的对应关系靠人工维护），所以改动者负责同步。
3. **§7 只增不删**，除非该项真的获得了自动执行点——那时把它移到 ▲ 表里。
4. **不变量是"必须长期成立"的断言，不是"当前实现细节"**。实现换了、不变量还在，
   就保留条目并换掉指向；实现细节变了、不变量本身不再成立，那是**设计变更**，
   要单独评审。

## 相关文档

| 文档 | 内容 |
|---|---|
| [ci.md](../ci.md) | CI 权威说明：job 布局 / 信任边界 / 为什么全 self-hosted / PR 恢复清单 |
| [testing.md](../testing.md) | 测试分层体系与档位命令 |
| [architecture.md](../architecture.md) | 完整架构详解 |
| [CLAUDE.md](../../CLAUDE.md) | 项目地图 + 各门禁的约定 |
| [todo.md](../../todo.md) | 待办与 follow-up（含本文 §7 的承接项） |
| [2026-09-18 Batch 4 执行记录](../review/2026-09-18-Batch4-runtime-health-truthfulness-执行记录.md) | R5/R6 的来源 |
| [2026-09-18 Batch 5 执行记录](../review/2026-09-18-Batch5-provider-capability-truthfulness-执行记录.md) | PC1–PC6 的来源（N-10 / F6 / F7） |
| [2026-09-18 Batch 6 执行记录](../review/2026-09-18-Batch6-conflict-metadata-truthfulness-执行记录.md) | CM1–CM5 的来源（N-3：409 冲突元数据真值） |
| [2026-09-16 Batch 1 执行记录](../review/2026-09-16-Batch1-correctness-durability-执行记录.md) | P1/P2/R 的来源 |
| [2026-09-17 Batch 1.7 执行记录](../review/2026-09-17-Batch1.7-background-task-terminal-state-执行记录.md) | T1–T4 的来源 |
| [2026-09-17 Batch 1.8 执行记录](../review/2026-09-17-Batch1.8-user-visible-terminal-semantics-执行记录.md) | U1–U5 的来源 |
