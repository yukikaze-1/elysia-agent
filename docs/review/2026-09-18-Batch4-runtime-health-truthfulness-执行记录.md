# Batch 4 — Runtime Health Truthfulness 执行记录

> ⚠️ **Historical execution record.** 本文描述**执行当时**的系统与结论；后续批次可能推翻其中部分判断。
> 当前事实见 [docs 索引](../README.md) 的 Canonical References（架构 / 不变量 / 测试 / CI / 运维 / 契约）。
> 正文**按当时原样保留**——不改结论、不删失败轨迹、不重写 Challenger 反证（`docs/readme-conventions.md` Rule 3）。

> 文档导航：[项目文档](../../README.md) → [docs 索引](../README.md) → 本文
>
> 前置：[Batch 4 Backlog Triage & Planning](2026-09-18-Batch4-backlog-triage.md)（N-1 / N-11 的**登记来源**与 §10 推荐）
>
> Scope **冻结**为 canonical backlog 的两条：**N-11**（`connect`/`_load_from_file` 吞异常 ⇒
> 失败不可观测）→ **N-1**（`/ready` 探 ping 而非装载结果 ⇒ 假绿）。本批**没有**重新扫描
> 全仓，也**没有**碰 N-3 / N-10 / F6 / F7 / GR-3 / GR-24 / N-15 等其它 backlog 项。

---

## 1. Executive Summary

| 问题 | 结论 |
|---|---|
| **N-11 fixed?** | ✅ `fix(profile): preserve identity load failure state`（`fc648e20`）——失败事实由 `UserIdentityStore` 持有：`is_available` = 不可用 + `last_error` = 异常类型名；`connect()` **不** raise（不打穿 `Agent.start()`） |
| **N-1 fixed?** | ✅ `fix(input): make readiness reflect identity availability`（`82b80e2e`）——`/ready` 的 identity 门禁改读 `store.is_available is True` |
| **Readiness contract（PD-1）** | ✅ 落纸于 [`docs/architecture/invariants.md`](../architecture/invariants.md) §3.1 + 新增不变量 **R5 / R6**（本轮**确认**而非新决策：产品侧三处围栏本就按 `is_available` fail-closed） |
| **Public API changed?** | **否**。`/ready` 的 wire schema 逐字不变（`status`/`chatReady`/`fullReady`/`dependencies` 等字段与 `chatReady=false → 503` 契约原样），只改**取值来源**；无新字段、无 `last_error` 外泄 |
| **Persistence changed?** | **否**。无新集合/索引/字段，无迁移 |
| **Migration?** | **否**（也是本批刻意拒绝的方向：runtime failure state **不**持久化，见 §12） |
| **Production diff 规模** | 2 个生产文件、**+106/−13 行**（其中约 3/4 是 docstring 与注释；实际行为改动 = 一行判据替换 + 失败状态记录/消解）：`core/profile/profile_store.py`、`core/input/health.py` |
| **回归网** | 新增 **23** 条（`core/profile/tests/test_ready_identity_truthfulness.py` 20 + `core/agent/tests/test_identity_readiness_lifecycle.py` 3）+ **4** 条真实 Mongo（`tests/real_mongo/test_real_identity_readiness.py`）；修前 **14 红 / 9 绿**，修后 **23 绿**（其中 3 条是本批 Challenger 命中后补的守卫） |
| **Mutation** | M1（退回旧 authority）/ M2（失败不更新状态）/ M2b（保留陈旧缓存）/ M3（恢复不清空）——**全部被杀**（§9） |
| **Challenger** | 独立子代理，六个必做攻击 + 五个加试；结论见 §10 |
| **真实 CI** | ⚠️ **未完成**：本机无 GitHub 凭据（`git fetch origin main` → `could not read Username for 'https://github.com'`，与 Batch 3.5/3.6 同因）⇒ 改为**本地跑 CI 等价集合**（§13.1）。待有凭据的机器 `git push` 后按 §13.2 核验 |

**一句话**：本批把一条断掉的因果链接上——**真实装载失败**以前只以一行 warning 存在，
于是 runtime 没有任何权威能回答"身份可用吗"，`/ready` 只好去问一个**可得但不等价**的
代理量（repo ping 通），最终部署围栏（200）与产品围栏（503）在同一时刻互相矛盾。
现在失败归 `UserIdentityStore` 所有，`/ready`、产品入口与恢复协调器读**同一个**权威。

---

## 2. Pre-Fix Failure Chain（实测，非静态推理）

```
库里的身份文档不合当前域模型（如 revision < 1）
        ↓
repo.connect() 成功（Mongo 可达）、repo.get() → UserIdentity.model_validate 抛 ValidationError
        ↓
ProfileStore.connect()  except Exception:  logger.warning(...)          ← ① 失败只到这里
        ↓
_cached = None（业务侧 fail-closed）                                     ← ② 状态有了，但原因没有
        ↓
        ├─ 产品入口：/api/me、CognitiveLoop._identity_snapshot、
        │            service/queries.py、service/instances.py
        │            读 store.is_available → False → 503 STORAGE_UNAVAILABLE
        │
        └─ /ready：identity 门禁读 store.health_check() → repo ping → **True**
                    ⇒ chatReady=true、HTTP 200（dependencies.identity=true）
        ↓
部署 / 自动化收到假绿，而用户对话轮次 503（窗口 ≥60s 或永久）
```

**同一时刻、同一受控状态下的两个 surface（修前实测）**：

```text
  store.connect() 返回      : None
  store.get()               : None
  store.is_available        : False
  await store.health_check(): True          ← 库是通的（这正是假绿的来源）
  /ready   → HTTP 200  chatReady=True  dependencies.identity=True
  /api/me  → HTTP 503  code=STORAGE_UNAVAILABLE
```

复现探针（未入库）：`/tmp/batch4_probe/probe_n1_repro.py`——真实 store + 真实
`UserIdentityRepository` + 真实 `create_app` 路由，一次进程内同时取两个 surface。
修后同一探针输出 `/ready → HTTP 503 chatReady=False`。

### 2.1 两个独立 root cause（不硬合并为一个）

| | N-11 | N-1 |
|---|---|---|
| 根因 | **失败状态归属缺失**：装载失败没有 owner 持有，只有日志 | **readiness 投影读错权威**：`/ready` 问的是"库通不通" |
| 为什么长期存在 | 异常被 catch 是**合法的**（调用方按 None fail-closed），于是"吞掉"看起来无害——缺的不是 raise，而是**状态** | `health_check()` 的 docstring 自陈"恢复协调器探针"，而 identity 又确实在 `/ready` 的 `dependencies` 里；"有 identity 门禁"被误读成"identity 门禁正确" |
| 独立可验证 | 是（store 层测试） | 是（route 层测试） |
| 相连之处 | 不先让失败可见，`/ready` 就无法正确回答；改一个不改另一个只会得到"更响的日志 + 仍然假绿的就绪" | 同上 |

---

## 3. PD-1 Readiness Contract（落纸）

> **Identity availability is required for chat readiness.**
> **Process liveness is independent from readiness.**

**一句话**：身份子系统**不可用**（= 身份未装载成功 / 重载失败）时，`/ready` **必须**
not ready；进程存活与 readiness 是两件事，不互相冒充。

### 3.1 三种语义

| 端点 | 回答的问题 | 判据 | 契约 |
|---|---|---|---|
| `/health` | 进程还活着吗 | 进程内存读取，**零网络探针** | **恒 200** |
| `/ready` | 对外承诺的核心 chat 能力现在可用吗 | mongo ping ∧ **identity 可用** ∧ lifecycle ∧ (product: idempotency) | `chatReady` → 200，否则 503 |
| `/ready.fullReady` | 长期记忆真的能持久化吗 | `chatReady` ∧ Milvus ∧ embedding | **不**门禁 HTTP，只可观测 |

### 3.2 为什么这是"确认"而不是"新增严格性"

产品**早已**对同一权威 fail-closed——`UserIdentityStore.is_available`
（`_cached is not None` 且 `repo.is_connected`）是这三处的围栏判据：

| 产品入口 | 位置 | 不可用时 |
|---|---|---|
| `GET/PUT /api/me` | `core/input/character_routes.py` | 503 `STORAGE_UNAVAILABLE`（不报"未初始化"，避免诱导客户端覆盖用户认知） |
| 对话轮次身份快照 | `CognitiveLoop._identity_snapshot`（`core/cognitive/loop.py`） | `StorageUnavailableError` → 503 |
| 角色预览 / 实例归档 | `core/character/service/queries.py`、`instances.py` | 稳定公开错误 |

**产品不服务，readiness 就不许说就绪。** 本批把 `/ready` 对齐到产品已经在用的那个权威，
同时它与恢复协调器的补偿判据 `_dependency_needs_recovery("identity")`
（本就是 `not store.is_available`，`core/agent/_lifecycle_persistence.py:625-631`）
**首次同源**——探针、修复器、产品围栏三者一致。

### 3.3 范围纪律（不泛化）

只覆盖「聊天/用户请求的必要依赖」这一条身份状态。TTS / STT / Workflow / Memory /
可选集成、以及任意 warning **都不**进 `/ready`。允许 `Agent` 进程继续运行而
`readiness=false`——**「不就绪」不等于「进程必须退出」**（这正是 readiness 的意义：
`process alive → critical dependency unavailable → /ready false → 编排器不导流 →
可观察、可恢复`）。

---

## 4. Authority Map

| Concern | Before | After |
|---|---|---|
| Process alive | `/health`（零探针，恒 200） | **不变** |
| Store reachable | `UserIdentityStore.health_check()` → `repo.health_check()`（真 ping，会重连） | **不变**（仍是恢复协调器探针） |
| Identity loaded | `UserIdentityStore.get()`（`_cached`） | **不变** |
| Identity available | `UserIdentityStore.is_available`（产品入口四处用它） | **不变**（本批把它扶正为 readiness 的输入） |
| Identity failure reason | **无**（只有 `logger.warning`） | `UserIdentityStore.last_error`（异常类型名，只读、进程内、不出 HTTP） |
| Chat ready | `chatReady` = mongo ∧ **ping** ∧ lifecycle ∧ idem | `chatReady` = mongo ∧ **is_available** ∧ lifecycle ∧ idem |
| `/ready` 的 identity 门禁 | `_probe(store.health_check)` | `store.is_available is True`（纯读，无副作用） |
| Recovery trigger | `_dependency_needs_recovery("identity")` = `not is_available` | **不变**（因此与 `/ready` 同源） |

---

## 5. N-11 Root Cause 与修法

### 5.1 哪些异常可以吞，哪些 failure truth 不能丢

按 §61 分类本批触碰的每个 catch：

| catch | 位置 | 分类 | 处置 |
|---|---|---|---|
| `connect()` 的 `except Exception` | `profile_store.py` | **DEGRADATION_CATCH**（调用方按 `None` fail-closed，不 raise 是为了不整体回滚 Agent） | **保留** catch，但补 `_cached = None` 与失败记录（同一次加锁内成立） |
| `_load_from_file()` 的 `except Exception` | `profile_store.py` | **DEGRADATION_CATCH（load-bearing）** | **保留**（见 §11），只让它 `_record_error` |
| `health_check()` 的 `except Exception` | `profile_store.py` | **CONTROL_FLOW_CATCH**（探针语义：异常即不可达） | **不动** |
| `_start_persistence` 的 `except Exception` | `_lifecycle_persistence.py` | **DEGRADATION_CATCH**（启动不因身份失败回滚） | **不动** |
| `_recover_dependency` 的 `except Exception` | `_lifecycle_persistence.py` | **CONTROL_FLOW_CATCH**（返回 False 下轮重试） | **不动** |
| `_probe()` 的 `except Exception` | `core/input/health.py` | **CONTROL_FLOW_CATCH**（超时/异常/不可 await → down） | **不动** |

**没有**任何一处属于 BUG_MASKING_CATCH 却未处理；也**没有**清扫任何 broad exception。

### 5.2 为什么不是"不再吞异常"

Backlog §6.1 已证伪"把两处 except 都删掉"的修法：`_load_from_file` 的吞是
load-bearing（本地模式 `get()`/`bootstrap()` 直接依赖它降级），而 `connect()` 若 raise
会打穿到 `Agent.start()` 造成整体回滚——当前产品允许降级进程运行，不该为表达失败而
把进程搞崩。

N-11 的准确名字是 **preserve identity load failure state**：

- 失败事实由**拥有 identity lifecycle 的对象**（`UserIdentityStore`）持有——不是
  `/ready` handler、不是 Agent，也**没有**新建 `_ready_identity_ok` 之类的平行标志；
- `connect()` 的既有契约（返回 `None`）不变，调用链不变；
- 新增**只读**诊断 `last_error`：只存**异常类型名**（`type(exc).__name__`）——完整消息
  可能含连接串/文件路径，而它虽是进程内诊断仍不该成为 secret 的第二个落点；
- 三处调用点（`_start_persistence` 的 warning、`_recover_dependency` 的 `return False`、
  健康循环探针）**逐字未改**，因此不存在"为了显式失败而破坏 lifecycle"。

### 5.3 `last_error` 的三条语义（写清楚是为了防止它被当成第二套判据）

1. **不蕴含不可用**：本地模式 fallback 文件损坏后 bootstrap 成功时，可用为真而记录
   可能仍留有旧值——故禁止写 `last_error is not None ⇔ not is_available`；
2. **会被消解**：成功装载（`connect()`）、成功 bootstrap（本地模式落盘）、成功读回
   缓存文件（`get()`）都清空它——失败历史不得把当前判定钉死（§21）；
3. **不出 HTTP**：`/ready` 只报布尔可用性（`"last_error" not in payload` 有断言钉住）。

---

## 6. N-1 Root Cause 与修法

**一句话**：`/ready` 的 identity 门禁与产品入口**不是同一个谓词**——前者问"权威库
ping 通吗"（`health_check()`），后者问"身份已装载且可用吗"（`is_available`）。
"N-1 很可能就在这里或它的调用方"（§13）**得到证实**：根源在**它的调用方**
（`core/input/health.py:_readiness_payload`），不在 `is_available` 本身——
`is_available` 的取值一直是对的，只是 `/ready` 没读它。

**为什么错误 authority 能长期存在**（§72）：

1. `health_check()` 的 docstring 自陈"供数据库恢复协调器探测"，且它确实是**正确的**
   recovery 探针——于是"改它"看起来像在动别人的地；
2. `/ready` 的 `dependencies` 里确实有一个 `identity` 键，"有 identity 门禁"很容易被
   读成"identity 门禁正确"，而**没有人问它探的是哪个谓词**；
3. 两者的取值在绝大多数运行状态下**相同**（库通、身份也就装上了），只在"库通但文档
   坏"这类形态下分岔——而这类形态需要一条不合域模型的文档（B3-CB-1 曾真实产生过：
   `pronouns: ""`），不是随手能撞上的；
4. 唯一的信号是日志 warning，没有测试断言过"装载失败 ⇒ `/ready` 不说就绪"。

**修法**：

```python
# before
identity_ok = await _probe(agent.user_identity_store.health_check)
# after
identity_ok = agent.user_identity_store.is_available is True
```

四个理由（都在代码注释里）：① 与产品入口、与恢复协调器补偿判据**同源**；
② **纯读**——`repo.health_check()` 在 ping 失败时会 `_mark_unavailable()` 并**重连**，
而探针不该触发副作用（§80）；③ 非权威（本地）模式 `is_available` 恒 True，语义不变；
④ 判据是真布尔 `is True`——MagicMock 的属性默认真值恒真，会让"门禁接到了替身属性"
被悄悄放过（实施中确实因此暴露了 2 处 fixture 未声明该属性，已如实补上）。

**没有**改 `health_check()` 本身；**没有**把 DB health 与 identity availability 揉成
一个函数（`test_health_check_keeps_recovery_coordinator_semantics` 反向钉住）。

---

## 7. Runtime State Model

不引入 enum（代码本来就没有），实际状态由既有字段表达：

| 状态 | `_cached` | `repo` | `is_available` | `last_error` | `/ready` |
|---|---|---|---|---|---|
| **loading**（权威模式，`connect()` 尚未跑完） | `None` | 是 | **False** | `None` | not ready |
| **available** | 非空 | 已连接 | True | `None` | ready |
| **unavailable** | `None` | 是 | **False** | 最近失败类型名 | not ready |
| **recovered** | 非空 | 已连接 | True | `None`（被消解） | ready |
| 本地模式（无 repo） | 非空（bootstrap） | 无 | True（恒） | 失败会被消解 | ready |

`loading ⇒ not ready` 在权威模式下**自然成立**（`_cached` 仍为 `None`），不需要额外状态；
本地模式没有异步装载窗口（同步 bootstrap），故该行不适用。

---

## 8. Fixes（逐 commit）

### `fc648e20` fix(profile): preserve identity load failure state

| | |
|---|---|
| **before** | `connect()` 失败 → 只 `logger.warning`；`_cached = None` |
| **after** | 失败记录 `last_error`（类型名）+ `_cached = None` 同一次加锁内成立；`logger.error`；成功路径消解记录 |
| **为什么最小** | 不改 `connect()` 的返回契约与任何调用方；不新增类/框架；`last_error` 是唯一新公开成员且为只读属性 |

### `82b80e2e` fix(input): make readiness reflect identity availability

| | |
|---|---|
| **before** | `identity_ok = await _probe(store.health_check)` |
| **after** | `identity_ok = store.is_available is True` |
| **为什么最小** | 一行判据替换 + 注释；wire 契约、字段集、HTTP 状态映射都不变；不动 `health_check()` |

---

## 9. Regression Tests 与 Mutation

### 9.1 测试清单（区分 PRE_FIX_RED / ALREADY_GREEN_GUARD）

`core/profile/tests/test_ready_identity_truthfulness.py`（真实 store + 真实 repo + 真实路由）：

| 用例 | 口径 | 修前 |
|---|---|---|
| `test_RHT1_load_failure_sets_unavailable_and_queryable_error` | PRE_FIX_RED（N-11 本体） | 红 |
| `test_RHT1b_repo_ping_ok_does_not_imply_identity_available` | ALREADY_GREEN_GUARD（判据分离的**表征**用例，修前已绿——Challenger F 更正） | 绿 |
| `test_RHT2_ready_503_when_identity_load_failed` | PRE_FIX_RED（N-1 本体，真实路由） | 红 |
| `test_RHT3_ready_200_when_identity_loaded` | ALREADY_GREEN_GUARD | 绿 |
| `test_RHT3b_ready_and_product_endpoint_agree[True/False]` | 半红（§36 cross-surface） | `False` 支红 |
| `test_RHT4_local_bootstrap_path_is_not_misjudged_unavailable` | ALREADY_GREEN_GUARD | 绿 |
| `test_RHT5_local_corrupt_cache_is_recorded_then_cleared_by_recovery` | PRE_FIX_RED | 红 |
| `test_RHT5a_local_corrupt_cache_records_error_before_recovery` | PRE_FIX_RED | 红 |
| `test_RHT5b_authoritative_failure_has_no_fallback_so_unavailable` | PRE_FIX_RED | 红 |
| `test_RHT5c_failed_reload_does_not_serve_stale_identity` | PRE_FIX_RED（M2b 暴露的缺口） | 红 |
| `test_RHT6_reconnect_clears_failure_and_restores_ready` | PRE_FIX_RED（恢复语义） | 红 |
| `test_RHT7_fresh_process_derives_availability_from_its_own_load` | PRE_FIX_RED（**fresh 子进程**） | 红 |
| `test_health_check_keeps_recovery_coordinator_semantics` | ALREADY_GREEN_GUARD（KEEP_AS_IS） | 绿 |
| `test_recovery_predicate_matches_readiness_authority` | ALREADY_GREEN_GUARD（同源性的表征用例，修前已绿——Challenger F 更正） | 绿 |
| `test_unavailable_is_exact_bool` | ALREADY_GREEN_GUARD | 绿 |
| `test_unavailable_store_still_fails_closed_on_write` | ALREADY_GREEN_GUARD | 绿 |

`core/agent/tests/test_identity_readiness_lifecycle.py`（真实 `Agent.start()`）：

| 用例 | 口径 | 修前 |
|---|---|---|
| `test_start_with_undecodable_identity_keeps_process_alive_but_not_ready` | PRE_FIX_RED（lifecycle 层 N-1/N-11） | 红 |
| `test_identity_recovery_via_coordinator_restores_readiness` | PRE_FIX_RED | 红 |
| `test_healthy_start_is_ready_and_does_not_touch_health_check_probe` | PRE_FIX_RED（禁副作用探针） | 红 |

`tests/real_mongo/test_real_identity_readiness.py`（真实 Mongo，`-m real_mongo`）：
真库上「ping 通、身份装不上」的可达性、真库恢复、`health_check()` 与 `is_available`
的分离、正对照。**不主张 pre-fix red**——它们的等价断言由上面 fake 层的用例承担，
真库层证明的是这些形态在真驱动上同样成立（不是 fake 的产物）。

### 9.2 Mutation（每个变异都必须让新测试变红）

| 变异 | 内容 | 结果 |
|---|---|---|
| **M1** | `/ready` 退回 `_probe(store.health_check)` | **杀**：新测试 7 条红 + 既有 `test_health.py` 4 条红 |
| **M2** | 装载失败既不记录失败也不更新不可用状态 | **杀**：RHT1 / RHT5b / RHT6 / lifecycle-1 红 |
| **M2b** | 重载失败**保留**旧 `_cached`（供应陈旧身份） | **杀**（补测 RHT5c 之后）：该变异初始存活 ⇒ 已补 `test_RHT5c` |
| **M3** | 恢复后不清空失败记录 | **杀**：RHT6 红 |
| **M8** | `/ready` 的 `is True` → 真值判断 | **先存活（Challenger F 命中）→ 补测后杀**：补 `test_ready_503_when_availability_is_truthy_but_not_bool` |
| **M9** | `is_available` 去掉 `repo.is_connected` 合取项 | **先存活（Challenger F 命中）→ 补测后杀**：补 `test_close_marks_identity_unavailable` |
| **M10** | `_load_from_file` 读成功不再清空失败记录 | **先存活（Challenger F 命中）→ 补测后杀**：补 `test_successful_file_read_clears_previous_failure` |

> **M2b 是本批自查抓到的真缺口**：最初的用例集**没有**覆盖"重载失败后是否继续供应
> 上一次的缓存身份"。变异存活 ⇒ 补 `test_RHT5c_failed_reload_does_not_serve_stale_identity`。
> 这属于 §48「测试 fake-green」自查，不是外部 Challenger 的产物。

---

## 10. Challenger Findings

独立子代理（独立上下文，只读，未改仓库任何文件；全部运行在 `/tmp/challenger_b4/` 的冻结
副本或按 sha256 钉住的 HEAD 上）。六个必做攻击 + 五个加试全部执行，附带 12 个变异。
基线：本批 4 个测试文件 45 passed、广扫 1789 passed、真实 Mongo 层 4 passed。

### 10.1 逐条判定

| 攻击 | 判定 | 关键证据 |
|---|---|---|
| **A. Fake-ready**（对象在、身份缺） | **attack failed**（真实接线）+ 2 个**潜在**形态 | 未启动的真 `Agent()`：`is_authoritative=True / is_connected=False / is_available=False` → `/ready` 503。潜在形态见 §10.3 NEW-4/NEW-5 |
| **B. Ping ≠ availability** | **partially confirmed**：ping 已无法覆盖不可用（M1 变异 13 红、M12 变异 7 红），但替代判据自身有盲点 | 真实 motor 客户端 + 可 kill 的 TCP 中继：分区后 `/ready=200 deps.identity=true`，而独立真驱动探针 `connect=False/health_check=False`；首次身份**写** 503 才把门禁翻假（`_mark_unavailable` 触发） |
| **C. Warning-only state** | **attack failed** | M2（`_record_error` 置空）6 红、M5（文件失败不记录）红、M6（bootstrap 不清空）2 红、M7（`is_available` 恒真）15 红 |
| **D. Stale failure** | **attack failed（重载路径）** | 走**真实协调器**：坏文档 → `is_available=False last_error='ValidationError' /ready=503`；修好 → `agent._db_health_round({})` → `identity: True` → `/ready=200`、`last_error=None`。`last_error` 全仓无生产消费者（只有定义与测试）⇒ 不可能把 readiness 钉死 |
| **E. False recovery** | **confirmed（1 条生产可达 + 2 条边界）** | 见 §10.2 E1（本轮最重要的新发现） |
| **F. Test fake-green** | **整体不是 fake-green；3 条语义原先无守卫 + 2 处标签误标** | 见 §10.4 |

### 10.2 E1（confirmed，已定性为**范围外 follow-up** 而非本批回归）

**形态**：**热缓存掩盖被外部写入者改坏/删除的权威文档**。真实 `Agent.start()` + 真实
路由 + 外部写坏文档 + 3 轮真实 `_db_health_round()`：

```text
[bad-doc] /ready=200 chatReady=True deps.identity=True | GET /api/me=200（陈旧） | PUT /api/me=409 forever
[bad-doc] 同一 repo 上的全新 store：loaded=None is_available=False last_error='ValidationError'
```

**机制**：`_dependency_needs_recovery("identity")` == `not is_available`，而热缓存下
`is_available` 为真 ⇒ 不触发重载；探针是 ping（True）⇒ **没有任何路径重读文档**。
`is_available` 的写入者只有 `connect()`（启动 / 恢复 / 测试）。

**定性**：这**不是**本批引入的回归（修前 `/ready` 读 ping，同样常绿），且**不在**
两条 canonical finding 的字面范围（N-1 = 读错权威；N-11 = 失败无可查状态）。
它证伪的是 **R5 的强度**：R5 保证「装载/重载失败 ⇒ 不就绪」，**不**保证「缓存与权威
文档逐轮比对」。已按 §77（禁止为两条 finding 新增 debounce/hysteresis）**不在本批修**，
改为：① 在 `invariants.md` §3.1 的"不包含的判定"里写明强度边界；② 登记为 follow-up
（§18 NEW-3）。Challenger 的完整机制与证据留在 `/tmp/challenger_b4/attack_stale_doc.py`。

**E2**（边界，当前不可达）：契约违反的 repo 让 `bootstrap_if_absent` 返回 `None` →
`connect()` 返回 None 但**没有** `last_error`（"没抛异常但没装到"这一形态的 RHT-1 缺口）。
`UserIdentityRepository` 在 `user_identity_repo.py:139-141` 会抛 `StorageUnavailableError`，
故生产不可达 ⇒ 记 NEW-6。
**E3**（边界，当前不可达）：本地模式（无 repo）+ 损坏文件 ⇒ `get()=None` 而
`is_available=True`。无生产调用方传 `storage_path` ⇒ 与 NEW-1/NEW-2 同源。

### 10.3 B(3) / 加试结论

- **B(3)（confirmed，按已接受的语义归档）**：`is_available` 依赖
  `repo.is_connected`，而 `core/storage/base.py:196-203` 自己写着它"**不代表真实可用**"
  （断网后 `_db` 仍非 None）。分区窗口内 `/ready` 绿而身份写 503。
  **影响有界**：全局 Mongo 故障仍被 `mongo` 门禁（session/message/attachment 真 ping）
  拦住 ⇒ 残余是"单连接失效 / 未触发的句柄失效"窗口。同样写入 `invariants.md` 的
  "不包含的判定"第 1 条。
- **加试 3（confirmed，低影响）**：`connect()` 的失败分支里 `_record_error`（持锁）→
  `logger.error` → 第二次持锁置 `_cached = None`，**不是**同一次加锁内完成——
  代码注释与实现不符。跨线程可观察窗口 = "失败已记录但陈旧身份仍在服务"；
  事件循环内无 `await` 插入，故不会出现 `is_available=True ∧ get()=None`。
  ⇒ **本批内修**：注释改成如实描述（见 commit `fc648e20` 的最终版）。
- **加试 5（attack failed）**：全仓已无其它把 `health_check()` 当可用性用的消费方
  （identity 的 `health_check` 只剩恢复触发点 `_lifecycle_persistence.py:502`，那是它的
  正确职责）；`character_routes.py:235`、`queries.py:258`、`instances.py:366`、
  `loop.py:432-443` 与 `/ready` **一致**。debug 面板无 identity 字段。

### 10.4 F：原先无守卫的三条语义（**本批内已补守卫**）

Challenger 用变异证明：M8（`is True` → 真值）、M9（`is_available` 去掉
`repo.is_connected`）、M10（`_load_from_file` 读成功不清空）**在 45 条测试下全部存活**
（其中 M9 只在 opt-in 的 real-mongo 层被杀，而该层不在 quick/normal 档）。

⇒ **本批内补三条 service 层守卫**（不是记账了事，因为这三条都是本批**新增/新建模**的语义）：

| 变异 | 补的守卫 |
|---|---|
| M8 | `test_ready_503_when_availability_is_truthy_but_not_bool` |
| M9 | `test_close_marks_identity_unavailable` |
| M10 | `test_successful_file_read_clears_previous_failure` |

补测后三条变异**全部被杀**（复跑见 §9.2 表）。

**补测过程自查出的一个真问题（§41 全局状态）**：M8 守卫最初用
`type(store).is_available = property(...)` 实现，并在 `finally` 里"复原"——那会
**永久污染同进程内的类属性**，于是它反而把 M9 变异**掩盖**成"存活"（单跑 M9 红、
整文件跑绿）。改为用**假 repo 的真实 `is_connected = 1`** 构造真值非布尔后，两条
变异都稳定被杀。这正是 §41 要防的形态：测试改全局，失败原因会漂到别的用例上。

**标签更正**：`test_RHT1b_repo_ping_ok_does_not_imply_identity_available` 与
`test_recovery_predicate_matches_readiness_authority` 原标 PRE_FIX_RED，实测**修前即绿**
（它们是表征/一致性用例）⇒ 已在上表改为 ALREADY_GREEN_GUARD。

### 10.5 Challenger 没能证伪的断言（保留原判）

- **RHT-1（权威抛异常路径）**：真实装载失败**必然**留下 `is_available=False` +
  `last_error`，且 `connect()` 从不 raise 到 `Agent.start()`（store / 路由 / 真实
  lifecycle 三层验证过；进程保持 RUNNING）；
- **RHT-2 / RHT-3**：`/ready` 503 ⇔ `/api/me` 503（同一时刻、同一 store）；任何 ping
  都无法覆盖不可用；
- **RHT-5**：本地 fallback 与"读成功消解失败记录"的语义；恢复后 readiness 回来（D）；
- **pre-fix → post-fix 因果链**：本批新测试在 `f8b2f863` 上 14 红，现在 23 绿。

### 10.6 Challenger 未能测试的项（不作过度声明）

- 真实 Mongo **容器重启**（会干扰并发的真库运行）⇒ 用可 kill 的 TCP 中继 + 真 motor
  驱动替代（同驱动路径、真 `ServerSelectionTimeoutError`）；
- CI-only 条件（`push/backend`、real-mongo job）⇒ 本地跑真库层代替（§13 另有真实 CI 结果）；
- `_injected_db=True` 的 fake 跳过 `_mark_unavailable`，故任何"进程内 fake"演示都会
  低估自愈能力——§10.3 的结论一律基于真驱动或显式标注。

---

## 11. Fallback Behavior（`_load_from_file` 的语义，明确落纸）

**当前事实（HEAD 现读，非假定）**：`storage_path` 的**唯一**装配方式是构造参数，
而两条生产装配路径都不传它：

- `core/agent/agent.py:155` → `UserIdentityStore(repo=self._user_identity_repo)`
- `core/input/server.py:324` → `getattr(agent, "user_identity_store", None) or UserIdentityStore()`

⇒ JSON 文件 fallback 只存在于**本地/单测模式**，生产路径走 Mongo 权威，**且写路径
不读文件**。因此：

| 场景 | 语义 | 可用性 |
|---|---|---|
| 无 repo + 文件不存在 | 本地 bootstrap 新建身份并落盘 | **True**（本地模式恒 True） |
| 无 repo + 文件合法 | 直接读回 | True |
| 无 repo + 文件损坏 | catch 降级：新建身份 + `_persist` 覆盖损坏文件 | True（**降级成功**）；失败被记录后又被成功状态消解 |
| repo + 文件损坏 | **不可达**（权威模式绝不读文件） | 由 Mongo 装载结果决定 |
| repo + Mongo 装载失败 | 无任何 fallback 可用 | **False** |

**结论**：`_load_from_file` 的 catch **保留**（load-bearing），只让它留下可查状态；
`DB fail + valid file fallback` 这一组合在当前产品里**不存在**，故 §75/§74 的那一行
"depends on product semantics" 的答案是：**不是正式能力，是死代码路径**（无生产调用
方）——按 §66/§76 的纪律不扩 scope 去清理它，只登记事实（见 §16）。

---

## 12. Recovery Behavior

**IN CURRENT LIFECYCLE**（不新增任何 recovery 框架）：

- 恢复入口是既有的 `Agent._recover_dependency("identity")`
  （`_db_health_round` 每 60s 一轮，`_dependency_needs_recovery("identity")` =
  `not store.is_available` 每轮补触发）；
- 它调用 `identity_store.connect()`（Challenger 用**真实** `agent._db_health_round({})` 验证过整条链），成功即原子替换缓存、清空失败记录、`is_available`
  回到 True ⇒ `/ready` 自动恢复（RHT6 / lifecycle-2 / real-mongo-2 三条覆盖）；
- 本批**没有**新增 debounce / hysteresis / retry controller（§77）；
- **重启语义**：failure state 是进程内的，不持久化 ⇒ 新进程重新 `connect()`/装载，
  重新决定可用性，**不会**继承"上次坏了"（RHT7 用两个 fresh 子进程钉住，§67）。

---

## 13. Cross-Surface Consistency（§36，本批最重要的不变量）


`test_RHT3b_ready_and_product_endpoint_agree[True/False]` 在**同一时刻、同一 store**
下取两个 surface：

```text
identity_loaded=False：/api/me → 503 STORAGE_UNAVAILABLE  ∧  /ready.chatReady is False
identity_loaded=True ：/api/me → 200                       ∧  /ready.chatReady is True
```

断言写成 `ready_data["chatReady"] is (me_status == 200)`——两个方向都被钉住，不允许
"ready=true 而产品 503"或"ready=false 而产品可用"。修前 `False` 支红。

---

## 13.1 本地 CI 等价集合（真实 CI 无法执行时的替代证据）

CI 的 `backend` job 逐步内容（`.github/workflows/ci.yml:139-186`）已在本地逐条执行：
12 个静态门禁 + `make audit` + `make test-normal`（CI 的"离线套件只跑一遍"就是它）。
CI 的 `real-mongo` job 等价于本机 `docker compose -f docker-compose.test.yml up -d`
+ `make test-real-mongo`，已跑通（12 passed）。

**这不等价于"真实 CI 全绿"**：本机 `.env` 与 self-hosted runner 的 workspace 清洁步骤、
conda 环境装配、`GITHUB_PATH` 注入等仍有差异；本节只声称"CI 会跑的检查项在本地都绿"。

## 13.2 push 后的核验步骤（交给有凭据的机器）

```bash
git push origin main          # 三个 commit：fc648e20（N-11）/ 82b80e2e（N-1）/ docs(architecture) 那条
gh run watch                  # 或到 Actions 页看最新 run
# 需要的 required jobs 全绿：
#   - Backend (static gates + offline suite)   ← make test-normal + 12 gates + audit
#   - Real MongoDB (storage layer)             ← make test-real-mongo（含本批 4 条）
#   - Frontend (vue-tsc + build)               ← 本批无前端改动，应照常绿
```

若某个 job 红了，**先看 §20 的本地等价结果**：本地已绿的项若在 CI 红，优先怀疑
self-hosted 环境差异（`.env`、conda env、workspace 残留），而不是本批代码。

---

## 14. Public API / Schema Impact

- **wire schema unchanged**：`/ready` 的响应字段集、`dependencies` 的 6 个键、
  `chatReady=false → HTTP 503` 全部原样（`test_ready_response_fields` /
  `test_health.py` 既有断言未改语义）；
- 不新增 `identityError` / `failureReason` 等字段（§24）——raw 异常/类型名**不进** HTTP
  （有断言 `"last_error" not in payload`）；
- 契约产物（ts / schema.json / raw.ts / golden frames）无漂移（`make check-types` 绿）。

---

## 15. Persistence Impact

**无**：无新集合、无新索引、无字段变更、无迁移（`make check-contract` 绿）。
刻意**不**持久化 failure state（§66/§67）：持久化会让"上次运行坏了"变成新进程的先天
状态，与 RHT7 钉住的行为相反。

---

## 16. New Findings（只记录，不修）

| 编号 | 发现 | 证据 | 处置 |
|---|---|---|---|
| **NEW-1** | `storage_path`（JSON 文件 fallback）**无生产调用方** ⇒ `_load_from_file` 是可达性仅存于本地/单测模式的路径；`bootstrap()` 里"文件损坏→新建并覆盖"这条修复路只在单测里被走到 | `grep -rn "UserIdentityStore("` 两条生产装配路径均不传 `storage_path` | 记入本记录 §11；**不修**（清理属 dead-code 清单范畴，需独立判定） |
| **NEW-2** | `get()` 在本地模式下遇到损坏文件时返回 `None`（纯读，不修复）；只有 `get()` 会走到 `_load_from_file`，`connect()` 无 repo 时直接 `bootstrap()`。即"读失败"与"修复"分属两条路径 | `test_RHT5a` 实施中发现 | 记入 RHT5a docstring；**不修**（无生产调用方） |
| **NEW-3** | `store.close()` 之后真驱动下 `health_check()` 会**自行重连**并把 `repo.is_connected` 拉回真，而 `_cached` 仍在 ⇒ `is_available` 回到 True（`close()` 不清缓存） | `tests/real_mongo/test_real_identity_readiness.py` 实测 | 记入该用例 docstring；**不修**（正常停机链路由 `lifecycle_ok` 先摘除 `/ready`；改动会让 `close()` 语义变重） |
| **NEW-4** | `UserIdentityStore.save()` 在权威模式下也**只写缓存不写 Mongo**（`profile_store.py`）——一次 `save()` 就能让**装载失败后**的 store 变成"可用"，`/ready` 与 `/api/me` 同时转绿，服务的身份从未从 Mongo 读过 | Challenger A：真实失败后 `await store.save(...)` ⇒ `is_available=True`、`/ready=200`、`/api/me=200` | **不修**（scope）；全仓唯一调用方是单测（`core/profile/tests/test_profile_store.py`）。**重开条件**：`save()` 出现生产调用方，或身份写路径改经它 |
| **NEW-5** | `/ready` 读 `agent.user_identity_store`，而 REST 身份路由读 `app["user_identity_store"]`；后者在 agent 侧无 store 时会 `or UserIdentityStore()` **伪造一个恒可用的本地 store**（`core/input/server.py:324`） | Challenger A：agent 侧换 store 后 `/ready=200` 与 `/api/me=503` 同时成立；agent 侧置 None 时 `/ready` 500 而 `/api/me` 200 | **不修**（scope：装配面；两条路径在真实装配下同源）。**重开条件**：出现 agent/REST store 分叉的真实装配路径 |
| **NEW-6** | 契约违反的 repo 让 `bootstrap_if_absent` 返回 `None` 时，`connect()` 返回 None 但**没有** `last_error`（"没抛异常、也没装到"） | Challenger E2 | **不修**（`UserIdentityRepository` 会抛 `StorageUnavailableError` ⇒ 生产不可达）。**重开条件**：出现返回 None 的 bootstrap 实现 |
| **NEW-7** | 热缓存掩盖被外部写入者改坏/删除的权威文档（R5 的强度边界；Challenger E1） | `/tmp/challenger_b4/attack_stale_doc.py`；机制见 §10.2 | **不修**（§77：不为两条 finding 新增 debounce/hysteresis）；已写入 `invariants.md` §3.1 强度边界。**重开条件**：需要"缓存与权威逐轮比对"的产品需求出现（届时需 freshness 机制设计） |

（Challenger 的 out-of-scope 新发现见 §10。）

---

## 17. Worktree Verification（§71：每个 fix 都验 pre-fix 红 / post-fix 绿）

方法：`git archive f8b2f863 | tar -x` 到 `/tmp`（**不含**本批新测试文件），再把本批
两个新测试文件复制进去，用仓库自己的 `pytest.ini` marker 集跑；post-fix 侧用 `git archive HEAD`。

| 侧 | 命令 | 结果 |
|---|---|---|
| **pre-fix**（`f8b2f863` 生产代码 + 本批新测试） | `pytest core/profile/tests/test_ready_identity_truthfulness.py core/agent/tests/test_identity_readiness_lifecycle.py -q` | **14 failed, 9 passed** |
| **post-fix**（`82b80e2e`，docs commit 不含生产代码） | 同上 | **23 passed** |

pre-fix 红的 14 条包含 N-1 本体（`/ready` 假绿）、N-11 本体（失败无可查状态）、
lifecycle 三条、recovery 与 fresh-process 各一条；绿的 9 条即 §9.1 标
ALREADY_GREEN_GUARD 的用例加上"用新增 API 断言"的几条（那些在修前因属性不存在而红）。
`tests/real_mongo/test_real_identity_readiness.py` 未做 pre-fix 对照（其等价断言由
fake 层承担，见 §9.1 说明）。

## 18. Keep-As-Is（本批**没有**改动的语义）

| 项 | 为什么不改 |
|---|---|
| `UserIdentityStore.health_check()` | 它仍是数据库恢复协调器的探针（权威库可达性）。改成"装载结果"会让恢复协调器永远认为库不可达；反向钉住见 `test_health_check_keeps_recovery_coordinator_semantics` |
| `_load_from_file()` 的 `except Exception` | load-bearing 降级（见 §11）；只补状态记录 |
| `connect()` 返回 `None` 的契约与三个调用方 | 启动失败不得整体回滚（§31）；`_start_persistence` 的 warning、`_recover_dependency` 的 `return False` 逐字未动 |
| 产品入口的四处 `is_available` 围栏 | 本批把它们**扶正**为 readiness 的输入，而不是改它们 |
| `_probe()` / mongo 三 repo 并行探测 / 超时预算 | 与身份无关，未触碰 |
| `/health` 恒 200、零网络探针 | liveness 语义独立（§6） |
| Milvus / embedding 不门禁 HTTP | `fullReady` 语义未变 |

---

## 19. Follow-up（保留，不在本批）

`N-3`（409 revision 谎报）、`N-10`（gemini 预检假绿）、`F6`（STT provider 探测分岔）、
`F7`（`/debug tts_ok` 语义）、`GR-3`/`GR-24`（索引选项登记与部分索引语义）、`N-15`、
`WAITING_APPROVAL`、reminder persistence、task LLM provider H3、workflow reporter、
provider portability、prompt budget cross-path、ToolAdapter cleanup——以及 canonical
`todo.md` 的其余条目，全部**未触碰**。§16 的三条新发现也留在 backlog 之外（只记录）。

**本批不开始 N-3**（§96）：即使执行顺利也不顺手做 409 revision。


---

## 20. Test Results（最终工作树：两个 fix commit + 文档 commit 之后）

| 集合 | 命令 | 结果 |
|---|---|---|
| **targeted** | 本批 4 个测试文件 + 邻近套件（profile / agent identity / input health / me / ready / shutdown / session routes / storage） | **51 passed** |
| 邻近子系统广扫 | `pytest core/agent/tests core/input/tests core/cognitive/tests core/character/tests -n 8` | **1722 passed** |
| `core/` 全树 | `pytest core/ -n 8 --dist loadfile` | **4040 passed, 3 skipped, 1 xfailed** |
| **顺序扰动**（§40/§90） | 四种次序（failed-first / healthy-first / combined / `-n 4 --dist loadfile`）各 **45 passed** | 无状态串台 |
| **fresh process**（§39/§91） | `test_RHT7`（两个独立子进程：坏/好各一） | ✅ |
| **quick** | `make test-quick` | **6006 passed, 2 skipped, 1 xfailed** |
| **normal** | `make test-normal` | **6006 passed, 2 skipped, 1 xfailed** |
| **remainder** | `make test-remainder` | 0 条（Quick == Normal） |
| **full** | `make test-full` | **6038 passed, 24 skipped, 1 xfailed** |
| **real-mongo** | `make test-real-mongo`（容器 `elysia-mongo-test` on :27018） | **12 passed**（含本批新增 4 条） |
| **static gates**（§92） | `make lint` / `pyright` / `check-arch` / `check-contract` / `check-timeouts` / `check-types` / `check-debug-auth` / `check-secrets` / `check-test-sets` / `check-compose` / `check-env-lock` / `check-ci-isolation` | **12/12 通过**（`pyright`: 0 errors；`check-test-sets`: Quick 6009 == Normal，全量收集 6063） |
| **audit**（CI backend job 的另一条） | `make audit`（pip-audit + bandit -ll） | **通过**（无已知漏洞；bandit 仅输出既有配置告警） |
| **real CI** | — | ⚠️ 未执行（§13.2） |

**独立 Challenger 的旁证基线**（它自己的冻结副本）：本批文件 45 passed、广扫
1789 passed、真库层 4 passed；本批新测试在 `f8b2f863` 上 14 红。

## 21. Invariant Mapping（§58）

| Invariant | 判据（测试） | Gate | CI |
|---|---|---|---|
| **R5** readiness 反映真实可用性 | `test_RHT2_ready_503_when_identity_load_failed`、`test_ready_503_when_availability_is_truthy_but_not_bool`、`test_close_marks_identity_unavailable`、`test_healthy_start_is_ready_and_does_not_touch_health_check_probe` | `make test-quick` / `test-normal` | push/backend |
| **R6** 装载失败留在运行时状态 | `test_RHT1_...`、`test_RHT5a/b/c_...`、`test_RHT6_reconnect_clears_failure_and_restores_ready`、`test_successful_file_read_clears_previous_failure`、`test_start_with_undecodable_identity_keeps_process_alive_but_not_ready` | 同上 | push/backend |
| **cross-surface**（R5 的一致性面） | `test_RHT3b_ready_and_product_endpoint_agree[True/False]` | 同上 | push/backend |
| 真库旁证（非门禁，opt-in） | `tests/real_mongo/test_real_identity_readiness.py`（4 条） | `make test-real-mongo` | ci.yml:real-mongo |

无新增静态门禁（§58：本批不变量以测试为判据，不需要新的静态 gate）。

## 22. 完成标准逐条核对（§101）

| 条件 | 状态 |
|---|---|
| Scope 仅 N-1 + N-11 | ✅（生产 diff 只 2 个文件、2 处行为改动） |
| PD-1 readiness contract 明确 | ✅ `invariants.md` §3.1 |
| 真实 identity load failure 可被 runtime state 表达 | ✅ `is_available` + `last_error` |
| 关键 load failure 不再仅靠 warning | ✅ `logger.error` + 状态（测试不依赖日志文本） |
| `/ready` 使用真实 identity availability authority | ✅ `is_available is True` |
| identity unavailable 时 `/ready` 不再假绿 | ✅ 修前实测矛盾 → 修后 503 |
| 健康路径仍然 ready | ✅ RHT-3 / lifecycle-3 / real-mongo-4 |
| 合法 fallback 行为不被误杀 | ✅ RHT-4 / RHT-5（catch 保留） |
| `_load_from_file` 的 load-bearing catch 未被机械删除 | ✅ 逐字保留，只加状态记录 |
| connect failure propagation 经调用链证明安全 | ✅ 不 raise；三个调用方逐字未改 |
| product endpoint 与 `/ready` 不再互相矛盾 | ✅ RHT-3b（双向断言） |
| 至少一条真实 route test | ✅ RHT-2 / RHT-3b / lifecycle 三条（真 `create_app`） |
| 至少一条真实 load/connect failure test | ✅ RHT-1（真 store+repo）、lifecycle-1（真 `Agent.start()`）、真库 2 条 |
| pre-fix 红测试存在 | ✅ 14 红（§17） |
| Challenger 独立执行 falsification | ✅ §10（六个必做 + 五个加试 + 12 变异） |
| 不新增 health/readiness framework | ✅ 无新类/注册表/管理器 |
| 不修改 N-3 等其它 backlog | ✅ |
| quick + normal + full + real-mongo + static gates 全绿 | ✅ §20 |
| 真实 CI 全绿 | ⚠️ **未执行**（无凭据，§13.2 给核验步骤） |
| `todo.md` 更新 | ✅ N-1/N-11 = GUARDED、PD-1 = 已裁决 |
| git status clean | ✅（推送后应仍 clean） |
