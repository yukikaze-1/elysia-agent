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
[2026-09-18 Batch 5 执行记录](../review/2026-09-18-Batch5-provider-capability-truthfulness-执行记录.md)。

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
| [2026-09-16 Batch 1 执行记录](../review/2026-09-16-Batch1-correctness-durability-执行记录.md) | P1/P2/R 的来源 |
| [2026-09-17 Batch 1.7 执行记录](../review/2026-09-17-Batch1.7-background-task-terminal-state-执行记录.md) | T1–T4 的来源 |
| [2026-09-17 Batch 1.8 执行记录](../review/2026-09-17-Batch1.8-user-visible-terminal-semantics-执行记录.md) | U1–U5 的来源 |
