# Batch 6 — Conflict Metadata Truthfulness 执行记录

> 文档导航：[项目文档](../../README.md) → [docs 索引](../README.md) → 本文
>
> 前置：[Batch 4 Backlog Triage](2026-09-18-Batch4-backlog-triage.md) §1.2/§3/§10-C
> （`N-3`，8 分，单条）与 [Batch 3.5 正确性分诊](2026-09-17-Batch3.5-correctness-triage-执行记录.md)。
>
> Scope **冻结**为 canonical backlog 的**一条**：**N-3**（`PUT /api/me` 的 409
> `details.currentRevision` 读进程内陈旧缓存 ⇒ 谎报）。本批**没有**扫描其它 409 API、
> **没有**重构 profile repository、**没有**碰 N-1 / N-10 / F6 / F7 / provider /
> readiness / pagination / reminder persistence / WAITING_APPROVAL / cache freshness E1，
> 也**没有**新建 ConflictManager / RevisionService / 事务框架。

---

## 1. Executive Summary

| 问题 | 结论 |
|---|---|
| **N-3 confirmed?** | ✅ **CONFIRMED_USER_REACHABLE**（E3）。修前实测（两个独立 store/repo 实例 + 真实 HTTP 路由）：A/B 都读 R1 → B 写成功 → 权威库 R2 → A 携带 `expectedRevision=1` → **409 `details.currentRevision = 1`**（应为 2）。真 Mongo 层同样复现（`assert 1 == 2`） |
| **fixed?** | ✅ **FIXED** —— `616e4993`（主修）+ `07110e8f`（Challenger 三处弱点加固）。409 的 `currentRevision` 现在由 repository 在**冲突产生点**回读权威库并携带上来，HTTP 层只做投影 |
| **authoritative revision owner?** | **权威持久化文档的 `revision` 字段**（`user_identity_profiles.revision`，经 `UserIdentityRepository.get()` 读取）。request / 进程内缓存 / `expected + 1` **都不是**权威 |
| **public API changed?** | **否**。409 信封字段集逐字不变（`code`/`message`/`retryable`/`requestId`/`error`/`details`/`retryAfterMs`）；`details.currentRevision` 类型不变（int），只在**权威现值未知**时按协议既有的"可带"语义**缺省**。`make check-types` / `make check-contract` / golden frames 无漂移 |
| **persistence changed?** | **否**。无集合/索引/字段变更、无 migration、无新索引 |
| **migration?** | **否**（沿用 ARCH-INC-P1-04：本批不需要任何 schema 变更） |
| **real CI?** | ⏳ **待 owner push 后核验**（本机无 GitHub 凭据）。三个 job 的**等价本地命令**已各自跑绿：backend 12 道门禁 + 6068 passed、real-mongo 15 passed、frontend `npm run build` 成功；见 §18.8 |
| 修复规模 | **5 个生产文件**（profile/user_identity、storage/repo、profile/store、input/route、character/instances），行为改动 = 冲突结果表示 + 冲突点回读 + HTTP 投影 + 2 处消费方适配 |
| 回归网 | 新增 **26** 条（路由层 19 + 真库层 3 + repository 3 + store 1）；其中 **14 条 PRE_FIX_RED**（原 HEAD 11 + 真库 3），另有 **3 条**是第一版修复的弱点守卫（对 `616e4993` 红），其余为守卫 |
| Mutation | 主 Agent M1–M4 **4/4 被杀**；Challenger 侧 9 个变异 **9/9 被杀**（存活 0），见 §13 |
| Challenger | 独立子代理 + 独立 worktree，A–K 十一个攻击 + 9 个变异：**3 处弱点命中并当场修**（J-1/J-2/J-4）、**4 处登记不修**（J-3/F-1/F-2/F-4）、1 处时序假警报（F-3）、3 处确认（无遗漏消费者 / 竞态语义诚实 / fake 不比 Mongo 强），见 §12 |

**一句话**：这个 bug 不是"取错了变量"，而是**冲突表示把真相丢了**——`update_if_revision`
在 CAS 未命中时返回裸 `None`，把「冲突了吗」和「现在到第几版」压成一个布尔，
于是唯一还能回答第二个问题的只剩进程内缓存。修法是让**知道真相的那一层**
（repository 的 CAS 未命中分支）把现值带上来。

---

## 2. Pre-Fix Reproduction

### 2.1 完整冲突链（§2 要求先画出来，改代码前）

```
Client expectedRevision = R1                       ← 请求负载（客户端声称，非权威）
        ↓
UserIdentityUpdate.model_validate（ge=1, int）     ← 校验期就挡掉缺失/0/负数/非整数
        ↓
_me_put → store.update_if_revision(R1, updates)    ← store 只做 is_available 门禁 + 归一化
        ↓
repo.update_if_revision(user_id, R1, patch)
   patch["revision"] = R1 + 1                      ← bump 与 payload **同一次原子 $set**
   update_one(filter={user_id, revision: R1})      ← CAS：Mongo 只返回计数
        ↓
authoritative Mongo revision = R2（winner 已提交） ← **唯一权威**
        ↓
modified_count == 0 → 「冲突」                    ← 冲突在这一层被发现
        ↓
修前：return None                                  ← ★ 真相在这里丢掉
        ↓
_me_put: current = store.get()                     ← 回头问**进程内缓存**（多 writer 时 = R1）
        ↓
409 details.currentRevision = R1                   ← ★ 错误值在这里被重新构造
```

| 问题 | 答案 |
|---|---|
| 请求里的 revision 从哪来 | 客户端负载 `UserIdentityUpdate.expectedRevision` |
| cached identity revision 从哪来 | `UserIdentityStore._cached`——`connect()` 装载或**本 store 自己**写入成功时被刷新 |
| Mongo 权威 revision 从哪来 | `user_identity_profiles` 文档的 `revision` 字段（CAS 的 `$set` 原子递增） |
| 冲突在哪一层被发现 | `UserIdentityRepository.update_if_revision`（`modified_count == 0`） |
| 哪一层知道真实 current revision | 只有 repository（且必须**回读**才知道值——`update_one` 未命中时只给计数） |
| 哪一层把它丢掉 | repository：`return None` |
| 409 从哪重新推导 | 修前是 route，从 `store.get()`（缓存）推导 |

### 2.2 修前实测数字（E2，假后端两条独立连接）

```
初始: A cache revision=1  B cache revision=1
      Mongo authoritative revision=1
B 写成功后: B cache=2  Mongo=2
A 写结果=None
A cache revision（陈旧）=1
Mongo authoritative revision=2
→ 409 details = {'currentRevision': 1}   （期望 currentRevision=2）
Mongo 文档内容 display_name='B写入的名字'
```

### 2.3 修前 PRE_FIX_RED（独立 worktree `/tmp/b6-prefix`，HEAD = `c9f926c0` + 新测试）

| 层 | 命令 | 结果 |
|---|---|---|
| E2 路由（假后端，两条独立连接） | `pytest core/input/tests/test_me_conflict_metadata_authority.py` | **8 failed, 8 passed**（红的全是元数据判据） |
| E3 真库 + 真路由 | `RUN_REAL_MONGO_TESTS=1 ELYSIA_TEST_MONGODB_URI=mongodb://127.0.0.1:27018/elysia_test pytest tests/real_mongo/test_real_identity_conflict_metadata.py` | **3 failed**（`assert 1 == 2` / `assert 1 == 3`） |

代表性原文（逐字）：

```
AssertionError: 409 必须报权威库现值（2），实际 1（request=1 / A 缓存=1 / Mongo=2）
AssertionError: request=1 A缓存=1 Mongo=2 → 409 currentRevision=1（必须是 2）
AssertionError: 必须报权威现值 3，实际 1（expected+1=2、缓存=1 都是假的）
AssertionError: 记录不存在 ⇒ 权威现值未知，不得回退成缓存里的 1
  assert 'currentRevision' not in {'currentRevision': 1}
AssertionError: 回读失败必须 fail-closed，实际 409 / {'code': 'USER_PROFILE_REVISION_CONFLICT', ...
                 'details': {'currentRevision': 1}, 'retryAfterMs': 100}
```

---

## 3. Revision Authority Model

| Concern | Authority |
|---|---|
| request expected revision | **请求负载**（`UserIdentityUpdate.expectedRevision`）——客户端声称的状态 |
| store cached revision | **运行时读缓存**（`UserIdentityStore._cached`）——读路径服务用，可陈旧 |
| persisted current revision | **权威库文档字段**（`user_identity_profiles.revision`）——唯一权威 |
| CAS conflict detection | **repository 的原子 update**（`update_one(filter={user_id, revision: expected})` → `modified_count`） |
| HTTP `currentRevision` | **repository 在冲突点回读的权威现值**，HTTP 只投影 |

**三个来源三个答案，互不冒充**；`expected + 1` 不是任何一个来源（可能跳过 N 次写入、
多 writer 下可能已 +N、revision 不一定连续），本批明文禁止。

落纸位置：[`docs/architecture/invariants.md`](../architecture/invariants.md) §3.3（CM1–CM5）。

---

## 4. Root Cause

**一句话**：冲突表示层（`UserIdentityRepository.update_if_revision`）用裸 `None`
同时表达「CAS 未命中」与「现值是多少」，第二个信息在**唯一知道它的那一层**被丢弃；
上层要填 409 元数据时只剩一个可问的对象——**进程内缓存**，而多 writer 场景下那按定义
就是陈旧的。

三个必要条件叠加才产生谎报：

1. `None` 把真相压成布尔（§18：root cause 形态）；
2. store 在冲突路径**不**刷新缓存（有意设计：冲突不是 refresh 的时机）；
3. 路由从 `store.get()` 取 `currentRevision`（§14 点名的「cached object revision」模式）。

修前 `grep` 复核（§14 高风险模式）：

| 模式 | 修前 | 修后 |
|---|---|---|
| `currentRevision = store.get().revision` | ✅ 命中（`character_routes.py::_me_put`） | 已消除 |
| `currentRevision = request.revision` | 未命中 | 未命中 |
| `expected_revision + 1` 用于**上报** | 未命中（仅用于 CAS 的 `$set`，合法） | 仍仅用于 CAS 写入 |
| 缓存对象 revision / route-local snapshot | ✅ 命中（同一处） | 已消除 |
| fallback 默认 revision | 未命中 | 未命中 |

---

## 5. Conflict Semantics

> `409.details.currentRevision` = **构造冲突响应那一刻**可观察到的**权威持久化现值**
> （response-time authoritative revision）。

**明确不承诺**：它**不**保证等于"击败本次写入的那个 writer 的 revision"。

理由（§12，不许假装拥有更强的语义）：Mongo 的 `update_one` 在 filter 未命中时只返回
`matched_count=0`，**拿不到**冲突瞬间的 actual revision；要拿到必须换原子原语（事务 /
change stream / `find_one_and_update` 也不给未命中时的现值）。用一次额外 read 去冒充
"冲突当时的精确值"是伪造精确性，本批拒绝。

**接受的竞态**（§11 / Challenger Attack D）：R1 → B 写 R2 → A 冲突 → C 写 R3 → A 回读 →
上报 **R3**。这是可接受且**有用**的行为：客户端拿 R3 重试即可收敛（拿 R2 会再冲突一次）。
测试**不**依赖"恰好 R2"——新增用例断言的是"等于权威库现值"，与实现同步读同一个字段。

---

## 6. Fix

### 6.1 before / after

| 层 | before | after |
|---|---|---|
| `UserIdentityRepository.update_if_revision` | `-> UserIdentity \| None`；未命中 `return None` | `-> UserIdentityUpdateOutcome`；未命中则**当场回读**权威现值并携带；回读失败抛 `StorageUnavailableError`；记录不存在 → `current_revision=None` |
| `UserIdentityStore.update_if_revision` | `-> UserIdentity \| None` | `-> UserIdentityUpdateOutcome`（1:1 投影；成功才刷新缓存；冲突**不**刷新） |
| `_me_put` 409 分支 | `current = store.get()` → `{"currentRevision": current.revision}` | `outcome.current_revision` → 有值才写该字段，未知则**省略** |
| `_retarget_default_character` | `updated is not None` | `outcome.identity is not None`（语义不变） |

新增类型（`core/profile/user_identity.py`）：

```python
@dataclass(frozen=True)
class UserIdentityUpdateOutcome:
    identity: UserIdentity | None = None          # 应用成功后的新身份
    current_revision: int | None = None           # 冲突时的权威现值；None = 未知
    @property
    def applied(self) -> bool: return self.identity is not None
```

### 6.2 加固（`07110e8f`，Challenger 命中后）

| 弱点 | 加固 |
|---|---|
| 冲突回读**解码失败**（外部写坏文档）⇒ `ValidationError` 逃出 `_call` ⇒ 500 | 新增 `_read_authoritative()`：解码失败 → `StorageUnavailableError`（503），与 `connect()` 对同一状态的判定同类 |
| CAS **已应用**但写后回读为空 ⇒ 旧代码返回 `identity=None` ⇒ 路由报 409（与"已落库"相反） | 该状态改为抛 `StorageUnavailableError`（503，可重试）；`applied` 的 docstring 与实现一致（该类型不再能表达"已应用但读不回"） |
| 回读解码值 **== expected**（库里的 revision 类型/形态与域模型不一致）⇒ 回显客户端期望值、无限重试 | 自相矛盾的回读值按「未知」上报（省略字段）+ warning 日志 |

三条各有 PRE_FIX_RED 守卫（对 `616e4993` 实测 3 failed）。

### 6.3 why minimal

- **一处新增判据**（repository 冲突分支回读）+ **一处投影替换**（route）+ **一处类型替换**；
- 不新建 service/framework（§9）：没有 `ConflictManager`、没有 `RevisionConflictService`、
  没有 version vector、没有事务框架；
- CAS 一字未动：filter 仍含 `revision: expected`、`$set` 仍原子 bump、无 last-write-wins、
  无"取最新 revision 静默重试"（§20）；
- 不碰 cache freshness（E1）：冲突路径读一次权威库是**局部、只读**行为，未被泛化成
  全时 freshness，也没有 conflict → cache refresh（§38 / §59）；
- 409 schema 与字段类型不变（§28/§29）。

**为什么让 repository 携带真相，而不是 route 回读**（§8/§35/§36）：CAS 未命中这个**事件**
只发生在 repository，它同时知道"该回读了"和"回读的语义是什么"。让 route 自己查 Mongo
会越过 store 层（§35 Option D，明确不取）；让 route 调一个 store 的"再读一次"方法
（Option C）也能工作，但会把"冲突表示"和"现值"拆成两次独立推导，正是 §34 警告的形态。

---

## 7. Race Analysis

| 场景 | 行为 | 判据 |
|---|---|---|
| A/B 两 writer | B 赢 → A 的 409 报 B 的 revision（权威现值） | `test_cmt1` / `test_real_conflict_reports_authoritative_revision` |
| A/B/C 三 writer | C 赢（R3）→ A 的 409 报 3（`expected+1` = 2 是假的） | `test_cmt3` / `test_real_multiple_writes_never_expected_plus_one` |
| 冲突后再写（C 在 A 的 CAS 与回读之间提交） | 上报**回读那一刻**的权威现值（可能是 C 之后的值） | §5 语义；测试断言"= 权威库现值"而非"= winner 的 revision" |
| 反复冲突（A 用同一个 R1 重试） | 每次 409 报**当时**的权威现值（不粘住第一次） | `test_cmt_repeated_conflict_tracks_authoritative_state` |
| 顺序扰动（A 赢/B 冲突 ⇄ B 赢/A 冲突） | 两种顺序报同一个权威现值 | `test_cmt_order_perturbation_both_orders_agree[A/B]` |
| 跨进程 writer | 父进程从未见过子进程的写入，仍报出它的 revision | `test_real_cross_process_writer_is_the_reported_authority` |

---

## 8. Winner Preservation

失败 writer 的响应构造**零副作用**：

- repository 冲突分支只做一次 `get()`（纯读），不写、不 bump、不 repair；
- store 冲突分支**不**刷新 `_cached`、不 `save()`、不重放写入；
- route 只格式化信封。

证据：`test_cmt4_failed_writer_preserves_winner_state` 对权威文档做 `deepcopy` 前后
逐字段比对（`after == before`，含 `display_name`/`pronouns`/`revision`）；M4 变异
（冲突后静默重试）被这条用例连同 6 条 409 用例一起杀掉（§13）。

---

## 9. Cache Behavior（本批**没有**解决什么）

| 问题 | 本批态度 |
|---|---|
| 409 之后 store 缓存是否仍陈旧？ | **仍是**（A 的缓存停在 R1）。这是**有意**的：409 是报告失败，不是隐式 refresh（§38）。测试显式断言该行为，并在注释里写明"将来做 E1 freshness 时这条判据要同步改" |
| 客户端如何恢复？ | 按 409 的 `currentRevision` 重新提交（协议文档 §9.1 的既定恢复路径）。修前这个字段是假的 ⇒ 该路径**永远不会收敛**（每次都被同一个陈旧值弹回） |
| GET 是否会自动变新鲜？ | **不会**。`_cached` 只在 `connect()` 与**本 store 自己**写入成功时更新 |
| E1（外部写入者改库 → 热缓存陈旧 → `GET` 可陈旧 / `PUT` 可能持续 409） | **仍 OPEN，不在本批**（§16/§58）。本批只保证"一旦 409 已发生，metadata 必须真实" |
| 冲突后的回读是否构成 freshness 机制？ | **不**。它只在冲突路径发生一次，且不写回缓存（§59） |

### 9.1 客户端消费面（§40 / §41）

| 问题 | 答案 |
|---|---|
| 仓内谁读 `details.currentRevision`（profile 409）？ | **0 个**。`grep -rn "USER_PROFILE_REVISION_CONFLICT\|currentRevision" frontend/src` 只命中 `contract.generated.ts` 里 **character** 域的 `CharacterSummary.currentRevision`；Vue 前端从不 `PUT /api/me` |
| 谁是该字段的真实消费者？ | **外部 Electron 客户端**（`docs/electron-backend-protocol-v2.md` §9.1 + `2026-08-30-character-prompt-customization-backend-architecture-design.md` 的「PUT 必传并与 currentRevision 一致，否则 409」恢复流程）。§9.1 本次同步改述为"权威库现值 / 未知时缺省" |
| 没人消费 ⇒ 影响等级下调？ | **不**（§41 明文）。谎报会让一个**按协议重试**的客户端永远用陈旧 revision 重放（每次都被同一个假值弹回）；这是 contract truthfulness 缺陷，不因仓内无消费者而消失 |
| 409 后客户端下一步怎么恢复？ | 用 `currentRevision` 重新构造 PUT（`retryAfterMs=100`）。修前这条路**不可能收敛**（服务端每次都报同一个陈旧值）；修后拿到的就是权威现值（除非该字段缺省 ⇒ 应改为 GET 重新取档案） |

---

## 10. Regression Tests

### 10.1 PRE_FIX_RED（14 条）

| # | 用例 | 层 | 修前判据 |
|---|---|---|---|
| 1 | `test_cmt1_conflict_reports_authoritative_revision` | E2 路由 | `409.currentRevision = 1`（应 2） |
| 2 | `test_cmt2_stale_cache_never_reported_as_current` | E2 路由 | 同上，且显式打印 request/缓存/Mongo/409 四个值 |
| 3 | `test_cmt3_multiple_intervening_writes_not_expected_plus_one` | E2 路由 | `= 1`（应 3） |
| 4–5 | `test_cmt_order_perturbation_both_orders_agree[A/B]` | E2 路由 | `= 1`（应 2） |
| 6 | `test_cmt_repeated_conflict_tracks_authoritative_state` | E2 路由 | `= 1`（应 3/4） |
| 7 | `test_cmt_missing_record_reports_no_fabricated_revision` | E2 路由 | 报了缓存值 1（应缺省） |
| 8 | `test_cmt_read_failure_after_conflict_fails_closed_503` | E2 路由 | 409 假真相（应 503） |
| 9 | `test_real_conflict_reports_authoritative_revision` | **E3 真库 + 真路由** | `assert 1 == 2` |
| 10 | `test_real_multiple_writes_never_expected_plus_one` | **E3** | `assert 1 == 3` |
| 11 | `test_real_cross_process_writer_is_the_reported_authority` | **E3 跨进程** | `assert 1 == 2` |

对原 HEAD（`c9f926c0`）实测：路由层 **11 failed / 8 passed**（上表 1–8 + 下表 12–14），
真库层 **3 failed**。

**第二轮（对第一版修复 `616e4993` 的 3 条弱点守卫，Challenger 命中）**：

| # | 用例 | 修前（@616e4993）判据 |
|---|---|---|
| 12 | `test_cmt_cas_applied_but_unreadable_is_not_a_conflict` | `assert 409 == 503`（写已落库却报冲突） |
| 13 | `test_cmt_undecodable_authority_fails_closed_503_not_500` | `assert 500 == 503`（fail-crash） |
| 14 | `test_cmt_decoded_revision_equal_to_expected_reports_unknown` | `'currentRevision' not in {'currentRevision': 1}`（回显 expected） |

### 10.2 ALREADY_GREEN_GUARD（修前即绿，**不算**修复证据）

| 用例 | 防的是什么 |
|---|---|
| `test_cmt4_failed_writer_preserves_winner_state` | 失败 writer 不得改动 winner（M4 的杀手） |
| `test_cmt5_healthy_update_still_succeeds` | 并发保护不得被削弱、正常更新不回归 |
| `test_cmt_invalid_expected_revision_never_reaches_conflict_path[0/-1/"abc"/None/1.5]`（5 条） | 坏 revision 在 validation 阶段 400，不进冲突路径（§30） |
| `test_cmt_missing_expected_revision_is_validation_error` | 同上（键缺失） |
| `test_update_if_revision_conflict_reads_authority_not_caller_state`、`..._without_record_reports_unknown`、`test_update_if_revision_stale_reports_authoritative_revision`（repo 层 3 条） | 新表示自身的语义（修前不存在，属 fix 的判据而非回归证据） |
| `test_update_if_revision_conflict_reports_local_authority`（store 层，本地模式） | 本地模式（无 repo）冲突仍报权威（该模式下缓存即权威） |

---

## 11. Real Mongo Evidence（E3）

`tests/real_mongo/test_real_identity_conflict_metadata.py`（`external` + `real_mongo`，
push-only 的 `real-mongo` job 执行）：

| 用例 | 断言的四件事 |
|---|---|
| `test_real_conflict_reports_authoritative_revision` | ①真库文档 revision（1→2）②A 的进程内缓存（仍 1）③409 `currentRevision`（2）④winner 的 `display_name` 原样保留（`B 赢了`） |
| `test_real_multiple_writes_never_expected_plus_one` | 真 CAS R1→R2→R3 后，A 用 R1 冲突 → 409 报 3 |
| `test_real_cross_process_writer_is_the_reported_authority` | winner 由**全新子进程**（新解释器 / 新 store / 新连接）写入；父进程 409 报 2 且库里是子进程的 payload —— 排除"同进程共享对象状态造成假象"（§54） |

运行：`make test-real-mongo` → **15 passed**（本批 3 条 + 既有 12 条）。

**fake 不比 Mongo 强**（§24）：权威回读发生在 **repository 内**（`self.get()`），
fake 与真驱动跑的是**同一段**冲突代码；fake 的 `update_one` 只实现"filter 未命中 → 0"
这一条真语义（`_fake_documentdb.py` docstring 已把 `modified_count` 语义与真后端对齐）。
本批新增用例**没有**任何断言依赖 fake 独有能力。

---

## 12. Challenger Findings

> 独立子代理 + 独立 worktree（`/tmp/b6-challenger`，detach 于 `616e4993`），
> 攻击清单 A–K（request-local 假真相 / cache-local 假真相 / `+1` 假设 / 静默重试 /
> 回读失败回退 / 冲突后竞态语义 / fake 强于 Mongo / 反复冲突粘性 / 漏掉的消费方 /
> 契约与 fail-closed 洞 / 副作用与并发保护），逐条要求原始输出。
> 报告：`/tmp/b6-challenger/CHALLENGER_REPORT.md`；执行探针 9 条全绿（每条弱点都被跑实）。

### 12.1 变异（Challenger 侧，9 个 **全部被杀**）

| 变异 | 结果 |
|---|---|
| A `expectedRevision`（request 假真相） | 8 failed |
| B `store.get().revision`（旧代码） | 7 failed |
| C `expected + 1` | 3 failed（CMT-3 杀的；CMT-1/2 杀不掉 `+1` 是设计事实） |
| D1 route 层静默重试重写 | 8 failed（含 **CMT-4**） |
| D2 store 层刷新缓存 + 重写 | 8 failed（含 **CMT-4**） |
| D3 删 CAS filter（last-write-wins） | 10 failed |
| E1 回读失败回退陈旧缓存 | 1 failed（503 那条守卫） |
| H 粘住首次冲突值 | 6 failed |
| K-refresh 冲突后只刷缓存 | 2 failed（order-perturbation） |

**"修复型变异存活 = 守卫是假的"这一最坏情形未发生。**

### 12.2 分类结论

| # | 发现 | 分类 | 处置 |
|---|---|---|---|
| J-4 | `applied` 契约自相矛盾：CAS 已应用（`modified_count>0`、库里确实写入）但写后回读被吞 ⇒ 旧代码返回 `identity=None` ⇒ 路由报 **409**，客户端以为写入没生效 | **fix weakness（真实）** | ✅ **当场修**（`07110e8f`）：该状态改为抛 `StorageUnavailableError`（503），`applied` 语义与实现一致；PRE_FIX_RED 守卫 `test_cmt_cas_applied_but_unreadable_is_not_a_conflict`（对 `616e4993` 红：`assert 409 == 503`） |
| J-2 | 权威文档被外部写坏（`revision: 0`）时，冲突回读的 `ValidationError` 逃出 `_call` → **500 INTERNAL**（修前同状态是 409）= fail-crash 而非 fail-closed | **fix weakness（真实，回归）** | ✅ **当场修**（`07110e8f`）：新增 `_read_authoritative()` 把解码失败转成 `StorageUnavailableError`（与 `connect()` 对同一状态的判定同类）；守卫 `test_cmt_undecodable_authority_fails_closed_503_not_500`（对 `616e4993` 红：`assert 500 == 503`） |
| J-1 | 库里 `revision` 类型与域模型不一致（字符串 `"1"`/缺字段）⇒ CAS 未命中而解码值 == 客户端 expected ⇒ 409 **回显 expected**，客户端无限重试（N-3 死循环换门回来），且违反 CMT-2 字面 | **fix weakness（真实）** | ✅ **当场修**（`07110e8f`）：自相矛盾的回读值按「未知」上报（省略字段）+ warning；守卫 `test_cmt_decoded_revision_equal_to_expected_reports_unknown`（对 `616e4993` 红：`assert 'currentRevision' not in {'currentRevision': 1}`） |
| J-3 | 外部删除权威档 + 缓存热：PUT 连续 409「expectedRevision 已过期」（记录根本不存在、`details` 空无恢复线索），同状态 GET 仍 200 + 陈旧值 | **new out-of-scope**（E1 同族） | 登记 `OBS-6`，不修（`GET`/`PUT` 的存在性判定与 404/409 契约是独立决策） |
| F-1 | `_retarget_default_character` 重试循环读**缓存**取 revision，冲突不刷缓存 ⇒ 权威模式并发改档案时必然耗尽重试、归档假失败 | **new out-of-scope** | 登记 `KB-11`，不修（角色归档写链，§0 禁止扩本批） |
| F-2 | `test_archive_consistency.py::_ConflictOnceStore` 是**本地模式**（无 repo）替身 ⇒ 它覆盖的"冲突后重试成功"在权威模式不可复现（fake-green） | **new out-of-scope（测试保真度）** | 登记 `GR-25` |
| F-3 | 协议文档引用的 `invariants.md §3.3` 在 `616e4993` 处不存在（悬空引用） | **refuted（时序假警报）** | 该引用与 §3.3 分属两个 commit；`22f7c130` 落地后引用即解析（Challenger 工作在 `616e4993`，看不到后一个 commit） |
| F-4 | `test_me_identity_failclosed.py::test_put_me_rejects_stale_revision`（单 store）在三种错误实现下全绿 = 惰性守卫 | **fake-green（既有测试债）** | 就地标注"这不是真值守卫"（`07110e8f`），并登记 `GR-25` |
| — | Attack I（遗漏消费者）：生产调用点恰好 **2** 个，均正确投影；全仓 AST 扫描 truthiness 命中 1 处（`return outcome`，无害），`outcome is not None` 型 0 处 | **confirmed（无遗漏）** | — |
| — | Attack F（竞态语义）：探针在 CAS 未命中与回读之间插入第三方提交 → 409 上报 response-time 值 3（winner 是 2），与书面语义一致；**无断言依赖"winner 的 revision"** | **confirmed** | 仅 2 处注释措辞偏强，已在 §5 再次明确 |
| — | Attack G（fake 强于 Mongo）：fake 与真驱动同为 `modified_count` 语义，回读在 repo 内两路共用同一段代码，新测试无 fake-only 断言 | **confirmed** | 真库一致性由本批 `make test-real-mongo`（15 passed）另行覆盖 |
| — | Attack K（副作用）：冲突路径实测 1 次 CAS（filter 完整保留）+ 1 次回读 + **0** 次额外写，权威文档逐字节不变、缓存不刷新 | **confirmed** | 与 `test_cmt4` / M4 一致 |

**Challenger 的基线噪声**：其全量 quick 跑出 1 条环境相关失败
（`Module/config/tests/test_preflight.py::test_minimax_key_present_passes`，既有的 GR-2 弱用例），
`git diff 616e4993^ 616e4993 -- Module/` 为空 ⇒ 与本 fix 无关；主 Agent 在本机
（同一 conda env、无 ambient `MINIMAX_API_KEY`）跑 `make test-quick` **6065 passed** 全绿。

**一句话**：主病真治好（我方 4/4 + Challenger 9/9 变异死亡）；Challenger 的价值在于
攻击**新代码自己的死角**——"回读值本身可信吗""结果类型是否又丢了一次真相"，
三条命中已当场修 + 补 PRE_FIX_RED 守卫，其余四条按 §0 登记不修。

---

## 13. Mutation Evidence

### 13.1 主 Agent（worktree `/tmp/b6-mut` @ `616e4993`）

每条变异都是**源码级**改动 + 跑新回归网 + `git checkout` 还原。

| 变异 | 内容 | 结果 | 杀它的用例 |
|---|---|---|---|
| **M1** | 409 报 `wire.expectedRevision`（request-local 假真相） | **killed=7** | cmt1/2/3/order[A]/order[B]/repeated/missing-record |
| **M2** | 409 报 `store.get().revision`（cache-local 假真相 = 旧代码形状） | **killed=7** | 同上 |
| **M3** | 409 报 `wire.expectedRevision + 1` | **killed=3** | cmt3（多次 intervening write）/repeated/missing-record |
| **M4** | 冲突后**静默重试**（回读现值 → 重放写入） | **killed=7** | cmt1/2/3/**cmt4（winner preservation）**/order[A]/order[B]/repeated |

M1/M2/M3 覆盖了 §51 要求的"旧错误判据"；M4 证明"失败 writer 不改变 winner"这条不是
空话（变异后 cmt4 立刻红）。

### 13.2 Challenger（worktree `/tmp/b6-challenger` @ `616e4993`）

9 个变异（含 D 族三种"静默重试/删 filter"、E1 回退、H 粘性、K-refresh）
**全部被杀，存活 0** —— 明细见 §12.1。

### 13.3 第二轮 N-1/N-2/N-3 守卫（对第一版修复的弱点）

Challenger 命中的三条弱点（§12.2 的 J-1/J-2/J-4）各自补了守卫，并**对 `616e4993`
实测为红**（3 failed）：写入已生效不得报冲突 / 解码失败必须 503 / 不得回显 expected。

---

## 14. Public API / Schema Impact

- 409 信封字段集**逐字不变**（`ProtocolError`）；`details.currentRevision` 仍是 int、仍"可带"；
- **唯一语义变化**：权威现值未知（记录已不存在）时该字段**缺省**——修前它会填一个
  **陈旧编造值**。协议文档 §9.1 本就标注该字段"可带"（与 `CHARACTER_REVISION_CONFLICT`
  同形），故这是**既有契约内的合法取值**，不是 schema 变更；
- `make check-types` / `make check-contract` / `check_error_codes` / golden frames 全部无漂移；
- 内部 API 变化（`UserIdentityRepository.update_if_revision` /
  `UserIdentityStore.update_if_revision` 的返回类型）**不是** wire 契约，但仓内 9 处调用点
  已同步（含 6 个测试文件，其中 2 处是"结果对象恒真"的静默风险点，已改成显式 `.identity`）；
- 文档同步：`docs/electron-backend-protocol-v2.md` §9.1 该行改述为"权威库现值 + 未知时缺省"
  并更新了行号锚点（`character_routes.py:270-286`）。

## 15. Persistence Impact

**无**：不改集合、不改字段、不新增/修改索引、无 migration；`ensure_index` 路径一字未动；
authoritative 文档结构（`user_identity_profiles`）逐字段不变。

## 16. Commits

| hash | message |
|---|---|
| `616e4993` | `fix(profile): report authoritative revision on update conflict`（生产修复 + PRE_FIX_RED 测试同 commit，§65） |
| `22f7c130` | `docs(architecture): record conflict metadata truth invariant`（不变量 CM1–CM5 + 执行记录 + todo/协议文档） |
| `07110e8f` | `fix(profile): harden conflict read-back against unreadable authority`（Challenger J-1/J-2/J-4 加固 + 3 条 PRE_FIX_RED 守卫） |

生产代码改动集中在 5 个文件；`616e4993` 与 `07110e8f` 是同一条 finding（N-3）的
"主修 + 攻击后加固"，`22f7c130` 是长期不变量文档。**没有**为了凑粒度拆得更碎（§64）。

## 17. Worktree Verification（§66）

| worktree | 起点 | 用途 | 结果 |
|---|---|---|---|
| `/tmp/b6-prefix` | `c9f926c0`（修前 HEAD，detach） | 新测试 → **红** | 路由层 8 红 / 真库层 3 红（§2.3） |
| `/tmp/b6-mut` | `616e4993`（修复 commit） | M1–M4 变异 | 4/4 被杀（§13） |
| `/tmp/b6-challenger` | `616e4993` | 独立攻击 | 见 §12 |

## 18. Tests

### 18.1 Targeted

```
core/input/tests/test_me_conflict_metadata_authority.py        19 passed
core/storage/tests/test_user_identity_repo.py                  （含 3 条新增）
core/profile/tests/test_profile_store.py                       （含 1 条新增）
core/profile core/storage core/input core/character core/agent
                                                               2040 passed
```

### 18.2 Order perturbation（§53）

`test_cmt_order_perturbation_both_orders_agree[A|B]` 参数化两种胜负顺序；每个用例自建
独立 fake 库与独立 store（无共享全局状态），并额外断言"失败方缓存仍停在 R1"
（防"共享缓存造成假绿"）。

### 18.3 Fresh process（§54）

`test_real_cross_process_writer_is_the_reported_authority`：winner 由
`subprocess.run([sys.executable, "-c", ...])` 的**全新解释器**写入，父进程 store 从未
见过它 ⇒ 409 的 2 只可能来自权威回读。

### 18.4 Quick / Normal（`07110e8f` 之后，最终态）

| 档位 | 结果 |
|---|---|
| `make test-quick` | **6068 passed, 2 skipped, 1 xfailed**（77.2s） |
| `make test-normal` | **6068 passed, 2 skipped, 1 xfailed**（77.4s） |

### 18.5 Full

`make test-full` → **6100 passed, 27 skipped, 1 xfailed**（441.8s）。

### 18.6 Real mongo

`make test-real-mongo` → **15 passed**（6113 deselected）。

### 18.7 Static gates

`lint` ✅ / `pyright` ✅（0 errors）/ `check-arch` ✅ / `check-contract` ✅ /
`check-types` ✅（含 golden frames）/ `check-timeouts` ✅ / `check-debug-auth` ✅ /
`check-secrets` ✅ / `check-test-sets` ✅ / `check-compose` ✅ / `check-env-lock` ✅ /
`check-ci-isolation` ✅

### 18.8 Real CI

**状态：待 owner push 后核验**（本机无 GitHub 凭据：私有仓、无 `gh`、无
`~/.git-credentials`、无 SSH key、env 无 token；`git push origin main` 报
`could not read Username for 'https://github.com'`。owner 选择自行 push）。

为降低"push 后才发现红"的风险，本机按 **CI 三个 job 的等价命令**各跑了一遍：

| CI job | 等价本地命令 | 结果 |
|---|---|---|
| `backend`（静态门禁 + 离线套件） | `make lint` / `pyright` / `check-types` / `check-arch` / `check-timeouts` / `check-contract` / `check-debug-auth` / `check-ci-isolation` / `check-secrets` / `check-test-sets` / `check-compose` / `check-env-lock` + `make test-normal` | 12 道门禁全绿 + **6068 passed** |
| `real-mongo` | `make test-real-mongo` | **15 passed** |
| `frontend`（`vue-tsc --noEmit` + `vite build`） | `cd frontend && npm run build` | ✅ 构建成功（本批未改前端文件；构建后 `git status` 仍干净） |

真实 Actions 结论以 owner push 后的 run 为准；**本批的 §76 完成标准中，唯独
"真实 CI 全绿"这一条尚未由我独立见证**（其余全部有本地可复现证据）。

---

## 19. Invariant Mapping

| Invariant | Test | Suite | CI |
|---|---|---|---|
| **CM1** 冲突元数据源自权威持久化状态 | `test_me_conflict_metadata_authority.py::test_cmt1_*`、`test_real_identity_conflict_metadata.py::test_real_conflict_*` | `test-normal` / `test-real-mongo` | push/backend + push/real-mongo |
| **CM2** 陈旧请求/缓存 revision 永不冒充权威现值 | `::test_cmt2_*`、`::test_cmt3_*`、`::test_cmt_repeated_*`、`test_user_identity_repo.py::test_update_if_revision_conflict_reads_authority_not_caller_state`、真库 `::test_real_multiple_writes_*` | 同上 | 同上 |
| **CM3** 失败的乐观更新不改动权威状态、不隐式重试 | `::test_cmt4_*` | `test-normal` | push/backend |
| **CM4** 同一记录上的不同 writer 报同一真相 | `::test_cmt_order_perturbation_*`、真库 `::test_real_cross_process_*` | `test-normal` / `test-real-mongo` | push/backend + push/real-mongo |
| **CM5** 不知道就不说（fail-closed） | `::test_cmt_missing_record_*`、`::test_cmt_read_failure_*`、`test_user_identity_repo.py::test_update_if_revision_conflict_without_record_reports_unknown` | `test-normal` | push/backend |

（同表已落纸到 [`docs/architecture/invariants.md`](../architecture/invariants.md) §3.3。）

## 20. Todo Update

`todo.md`：**N-3 → GUARDED `616e4993`**（唯一状态变更）；§1.2/§1.3 两张表的 N-3 行同步；
Truthfulness Backlog §3 的 N-3 行标记已修。其余 canonical 条目**一字未动**（§69）。

## 21. Keep As-Is

- **乐观 CAS**：`update_one(filter={user_id, revision: expected})` + 原子 `$set` bump，一字未动；
- **不做自动重试**：409 只报告失败，不 refresh / 不 repair / 不重放；
- **不建 cache freshness 框架**：E1 仍 OPEN（外部写入者的 freshness 是独立设计）；
- **409 schema 不变**：字段集/类型不变，只在未知时缺省；
- **不用 fake 换取测试便利**：权威回读在生产代码路径内，fake 与真驱动同码；
- **不把 HTTP 层变成 persistence authority**：route 不 import Mongo、不查集合。

## 22. New Findings（只记录，不修）

按 §0/§70 登记进 `todo.md`（新 ID，不改动任何其它条目的状态）：

| ID | 内容 | 来源 | 为什么不修 |
|---|---|---|---|
| **OBS-6** | 外部删除权威身份档 + 缓存热：`PUT /api/me` 连报 409「expectedRevision 已过期」（记录根本不存在、`details` 无恢复线索），同状态 `GET /api/me` 仍 200 + 陈旧值 | Challenger J-3 | E1 freshness 同族；"记录是否存在"的判定与 404/409 的映射是**独立契约决策**（本批只做到"不伪造 revision"） |
| **KB-11** | `_retarget_default_character` 的重试循环读**缓存**取 revision，而冲突路径刻意不刷缓存 ⇒ 权威模式下并发改档案时 3 轮重放同一 expected、必然 `RevisionConflictError`（归档默认角色假失败）。本批新带的 `outcome.current_revision` 在这条路径无消费者 | Challenger F-1 | 角色归档写链，§0 明确禁止扩本批；fail-closed 无数据损坏 |
| **GR-25** | ① `test_archive_consistency.py::_ConflictOnceStore` 是本地模式替身 ⇒ "冲突后重试成功"在权威模式不可复现（fake-green）；② `test_me_identity_failclosed.py` 的单 store 409 用例对冲突元数据真值零鉴别力（三种错误实现下全绿） | Challenger F-2/F-4 | 测试保真度债；②已就地标注并把真值守卫指向本批新文件，①待补权威模式用例 |

**已当场修、不进 backlog 的**（属本批新代码自身的弱点，§42 的 Challenger 轮次正是为此）：
J-4（写成功被报成冲突）、J-2（fail-crash 500）、J-1（回显 expected 的死循环）——见 §12.2 与 §6.2。

## 23. Follow-up

- **E1 cache freshness**（外部写入者改库后热缓存陈旧）——仍 OPEN，本批刻意不碰；
- canonical backlog 其它项（P2-01r / F8 / GR-* / reminder persistence / provider debt / F5d …）
  一律保留原状态，本批未开工。

---

## 相关文档

| 文档 | 内容 |
|---|---|
| [invariants.md](../architecture/invariants.md) §3.3 | CM1–CM5 不变量与判据映射 |
| [electron-backend-protocol-v2.md](../electron-backend-protocol-v2.md) §9.1 | `USER_PROFILE_REVISION_CONFLICT` 的 wire 契约（本批更新了取值语义说明） |
| [Batch 4 Backlog Triage](2026-09-18-Batch4-backlog-triage.md) | N-3 的分诊与候选批次 |
| [Batch 5 执行记录](2026-09-18-Batch5-provider-capability-truthfulness-执行记录.md) | 上一批（provider capability truth） |
| [todo.md](../../todo.md) | backlog 单一事实源（N-3 已 GUARDED） |
