# Batch 8 — Pagination Completeness（P2-01r）执行记录

> 范围：canonical backlog 仅 **P2-01r**。本批**不**处理 KB-6 / KB-12 / KB-13 / Ollama
> Provider Adapter（AR-16）/ OBS-6 / KB-11 / GR-25 / N-15 等其它条目，也**不**做
> 全项目分页框架、其它 list endpoint 审计或前端 UX 重设计。
>
> 结果：**P2-01r = GUARDED**（生产修复 + 三层 PRE_FIX_RED + 真库 E3 + 不变量 PG1–PG6）。
> 同时把该条目**另一半**（"显式 session 过期级联清理 worker"）按证据重分类为
> **SUPERSEDED**（§2.4），并登记 6 条 `NEW_FINDING_OUT_OF_SCOPE`（§24）。

---

## 0. 结论摘要

| 问题 | 回答 |
|---|---|
| P2-01r 确认？ | **确认**（canonical 描述与 HEAD 一致；`session_repo.list_all` 的 `limit=200` 是数据集上限） |
| 复现证据级？ | **E1（HEAD 现读）+ E2（真实 route：真 SessionManager + 真 SessionRepository + HTTP）**，并补 **E3（真实 MongoDB 驱动 + 真实存储）** |
| Root cause？ | **G+F+A+H 复合**：仓储预取 ≤200（G）→ service 在该窗口内分页/搜索（F）→ 窗口成为 API 级数据集上限（A）→ 最后一页 `has_more=false` 谎报"没有更多"（H）。**不是**前端问题（B 已证伪，§12） |
| 用户入口？ | `GET /api/sessions?cursor&limit&search`（前端消费方在**外部仓** `elysia-frontend`，其游标翻页实现正确——它只是被服务端的 `has_more=false` 叫停） |
| 修好了？ | **是**：分页下推到存储（keyset 谓词 + 双键排序 + `limit` + title `$regex`），service 只做"持久层分页流 ⊕ 进程内覆盖层"归并；第 201+ 条可达、可搜、详情可读 |
| 公开 API 变化？ | **无**（`SessionListResult{sessions,has_more,cursor}` 与 `SessionSummary` 逐字段不变；cursor 格式不变；错误码不变） |
| Persistence 变化？ | **仅新增一条索引** `sessions.updated_at_-1_session_id_1`（`ensure_index` 幂等覆盖，无迁移框架需求）+ `schema_drift` 登记；**无字段/集合变更** |
| 真实 CI？ | 见 §21（push 后核对 backend / real-mongo / frontend 全绿） |

---

## 1. Scope Lock

| 项 | 结论 |
|---|---|
| 唯一 canonical 条目 | **P2-01r**（§1.2 Correctness，USER / MEDIUM） |
| 允许检查 | 会话 list endpoint、`SessionManager.list/get_summary`、`SessionRepository` 查询、limit/cursor/sort/`has_more`、契约 `SessionListResult`、debug/前端消费方（只读） |
| 明确不碰 | 通用分页框架、其它 list endpoint、搜索系统重写、前端无限滚动、缓存 freshness、reminder 持久化、KB-6/12/13、Ollama Provider Adapter、已关闭的 N-*/F* |
| 越界发现 | 只登记（§24），不修 |

---

## 2. Backlog 复核（HEAD 现读）

### 2.1 canonical 登记（`todo.md` §1.2，Batch 4 triage 之后）

> `P2-01r`：会话列表 `list_all` 仍预取 200 ⇒ **第 201+ 条不可分页/搜索**；
> 显式 session 过期无级联清理 worker ｜ USER ｜ MEDIUM ｜ E1 ｜ OPEN

原始来源：`git show 69f2e5a9:todo.md` §D 部分完成项（2026-08-10 Electron v2 hardening）：

> `P2-01` ｜ 已做：TTL 真实生效（BSON `ttl_expires_at`）+ reflector 持久化接线 ｜
> 剩余：**session 列表 keyset 分页下推（repo.list_all 仍预取 200，第 201+ 条不可分页/搜索）**；
> 显式 session 过期级联清理 worker

### 2.2 HEAD 现读：描述成立，且**两处出口**同源

| 事实 | 位置 |
|---|---|
| 仓储用 `limit=200` 预取，且**没有任何游标参数** | `core/storage/session_repo.py::list_all`（`find({}, sort=[("updated_at",-1)], limit=200)`） |
| service 唯一数据源 = 该预取列表 | `core/memory/session_manager.py::_merged_summaries`（`await self._repo.list_all()`） |
| 游标过滤 / 搜索 / `has_more` 都在这个 ≤200 窗口内做 | `SessionManager.list()`（内存 `summaries[:limit]`、`has_more = len(summaries) > limit`） |
| **第二个出口**：详情/改名走 `get_summary` → 同一个 ≤200 列表 ⇒ 第 201+ 条拿到 `None`，路由兜底成 `updatedAt: ""` | `SessionManager.get_summary` / `core/input/session_routes.py::sessions_detail_handler` |
| 设计文档**从未允许**数据集上限，且明确要求 keyset 下推 | `docs/superpowers/specs/archive/2026-08/2026-08-04-electron-backend-batch3-multi-session.md` §4:65/164/182/183（"`has_more` 用 limit+1 探测"、"`search` 对 title `$regex`"） |

⇒ 这是**"实现缺失"**（文档承诺 vs 代码事实），不是产品决策。

### 2.3 契约（修改前已存在，本批逐字遵守）

| 维度 | 契约 |
|---|---|
| 分页类型 | **keyset cursor**（非 offset、非 page/pageSize） |
| 默认 page size | 50（`_parse_limit(default=50)`）；`limit<=0`/非法 → 回退默认 |
| 最大 page size | 100（`_MAX_LIMIT`；单页夹取，**与数据集无关**） |
| 排序 | `updated_at DESC, session_id ASC`（tie-break 必须存在） |
| 下一页谓词 | `(updated_at < U) OR (updated_at = U AND session_id > S)` |
| 返回字段 | `sessions[] / has_more / cursor?`（无 `total`） |
| `has_more` | limit+1 探测 |
| 搜索 | title 的大小写不敏感子串（设计文档明示 `$regex` 下推；不混消息文本） |
| 客户端续页 | 回传响应里的 `cursor`（不解析） |

### 2.4 另一半（"过期级联清理 worker"）→ **SUPERSEDED**

- 2026-08-10 整改起 `sessions` **不再建 Mongo TTL 索引**（`docs/database-storage.md` §110）；
  `SessionRepository` 中不存在任何 `ttl_/expire` 字段或索引（本批 grep 复核：0 命中）。
- 会话删除走**显式级联**：`SessionManager.delete()` + `cascade_repos` 清单
  （`token_usage`/`generations`/`character_versions`）→ 提交后清理 + pending journal 重试，
  并由 AST 守卫 `core/memory/tests/test_session_cascade.py` 断言"所有带 `delete_by_session`
  的仓储都在清单里"。
- ⇒ **没有"过期"机制可级联**；该半条已随 2026-08-10 决策被取代（不是"忘了做"）。

---

## 3. 修前复现（真实 route，E2）

脚本：真 `create_app` + 真 `SessionManager` + 真 `SessionRepository`（内存 DocumentDB 后端），
播种 N 条 sessions doc 后**只依据 `has_more`/`cursor`** 逐页请求 `GET /api/sessions?limit=50`。

| 播种 N | 期望可见（含 clean-start 本地 default） | 实际返回 | 缺失 | 最后一页 |
|---|---|---|---|---|
| 0 | 1 | 1 | 0 | `has_more=false` |
| 50 | 51 | 51 | 0 | `has_more=false` |
| 199 | 200 | 200 | 0 | `has_more=false` |
| 200 | 201 | 201 | 0 | `has_more=false` |
| **201** | 202 | 201 | **1**（最旧 `conv_seed_000`） | **`has_more=false`（谎报）** |
| **205** | 206 | 201 | **5**（`conv_seed_000..004`） | **`has_more=false`（谎报）** |

205 条的逐页轨迹（修前）：

```
page1: n=50 has_more=True  first=conv_seed_204 last=conv_seed_155
page2: n=50 has_more=True  first=conv_seed_154 last=conv_seed_105
page3: n=50 has_more=True  first=conv_seed_104 last=conv_seed_055
page4: n=50 has_more=True  first=conv_seed_054 last=conv_seed_005
page5: n=1  has_more=False first=default     last=default      ← 只剩本地 default
returned=201 unique=201 missing=5
```

**不可达的起点 = 第 201 条持久记录**（按 `updated_at DESC` 排序后最旧的那批）；
`default`（进程内本地摘要）之所以还在，只是因为覆盖层不走仓储——这正是"bug 被长期
掩盖"的原因：**在同一进程里创建/改名的会话不受 200 限制**。

修后同一脚本：`page5: n=6 has_more=False`、`returned=206 unique=206 missing=0`。

修前边界矩阵（种子数 → 缺失数）：`0..200 → 0`；`201 → 1`；`205 → 5`。

---

## 4. 请求链（修前 → 修后）

| 层 | 修前 | 修后 |
|---|---|---|
| UI/客户端（外部仓 `elysia-frontend`） | `listConversations({cursor,limit})` 沿 `nextCursor` 翻页（**实现正确**） | 不变 |
| HTTP route `sessions_list_handler` | 解析 cursor/search/limit → `manager.list()`；逐条 `count_by_session` | 不变 |
| limit 夹取 `_parse_limit` | `[1,100]`，默认 50 | 不变 |
| service `SessionManager.list` | `_merged_summaries()`（**≤200**）→ 内存 search/sort/cursor/slice | `_merged_page()`：**持久层 keyset 分页流 ⊕ 进程内覆盖层归并** |
| service `get_summary` | 遍历同一个 ≤200 合并列表 | 按 ID **直读单文档** + 覆盖层合并 |
| repository | `list_all()` = `find({}, sort=updated_at DESC, limit=200)` | `list_page(limit, after, search)`（keyset 谓词 + 双键排序 + `limit` + `$regex` 全部下推）+ `summaries_by_ids` + `get_summary` |
| storage | Mongo `find`（单键排序，无游标） | Mongo `find`（`$or` 谓词 + 复合排序 + `limit`），复合索引支撑 |

---

## 5. Root Cause

`limit=200` 出现在**仓储**，但变成"数据集上限"发生在**service 把预取结果当成取数原语**这一步：

1. `list_all()` 的 `limit=200` 本是"单页/窗口"参数，却没有任何办法翻到下一页（无 cursor 入参）；
2. `SessionManager.list()` 把这份 ≤200 当**完整数据集**，在其上做 search → cursor → slice；
3. `has_more = len(summaries) > limit` 也在这个被截断的集合上计算 ⇒ 最后一页**必然**报 `false`；
4. `get_summary`（详情/改名）复用同一列表 ⇒ 第 201+ 条的 `updatedAt` 兜底成 `""`。

⇒ 不是 A（路由硬 cap）、不是 B（前端不翻页）、不是 D（游标算错）、不是 I（排序不稳）、
不是 J（schema 没暴露分页）；是 **G（仓储 cap）经 F（service 一次性取 200）放大为 A + H**。

---

## 6. Authority Map（谁拥有什么）

| 维度 | 修前 owner | 修后 owner |
|---|---|---|
| 单页大小 | route `_parse_limit`（[1,100]） | 不变 |
| 数据集窗口 | **repository `list_all(limit=200)`**（错位：单页参数当数据集上限） | **无**（仓储只回答"游标之后的至多 `limit` 条"） |
| 游标语义/过滤 | service（内存） | service（定义）+ storage（下推执行） |
| 排序 + tie-break | service（内存稳定排序） | **storage**（双键 `sort`，tie-break 必须下推——fake 的 Timsort 会掩盖真库的不确定性） |
| 搜索 | service（`str.lower()` 子串） | **storage**（title `$regex i`，转义 + 长度上限单源 `search_regex_filter`）；覆盖层侧用同族正则语义 |
| `has_more` | service（基于预取长度） | service（**limit+1 探测**，覆盖层尾部计入） |
| `total` | 不存在（契约无此字段） | 不变（不新增） |
| 覆盖层合并 | service（`_merged_summaries`） | service（`_merge_summary` 单点，语义逐条保持不变） |

---

## 7. Fix（before → after → 为什么最小）

**`core/storage/session_repo.py`**

- 新增 `list_page(*, limit, after=None, search="")`：把 §2.3 的谓词/排序/`limit`/`$regex`
  一次性下推给存储；`limit<=0` 在进后端前兜住（真后端 `.limit(0)` 是"不限制"）。
- 新增 `get_summary(session_id)`（单文档读）与 `summaries_by_ids(ids)`（覆盖层批量读，
  显式 `limit=len(ids)`——后端 `find` 默认 `limit=100`，覆盖层多于 100 条会被截断）。
- `list_all()` **保留但收窄为"有界诊断读"**（debug 面板专用，docstring 明示用户列表不走这里）。
- 投影收成一个 `_summary_from_doc`（四条读路径同源；`updated_at` 强制归一为 `str`）。
- `connect()` 增补复合索引 `(updated_at -1, session_id 1)`（见 §17）。

**`core/memory/session_manager.py`**

- 删除 `_merged_summaries()`；`list()` 改为 `_merged_page()`：
  - 快照一次可见本地摘要 → 一次 `summaries_by_ids` 拿到它们在持久层的键/字段；
  - 覆盖层条目按**有效排序键** `max(持久键, 本地键)` 排序（改名会把记录"抬"到最前——
    这是既有语义，必须保住）；
  - 持久层流按游标逐窗读（`limit+1`），与覆盖层做 **k 路归并**；覆盖层代表过的 ID
    从流里剔除、同一 `session_id` 的重复文档去重；
  - `has_more` = 归并结果 > `limit`（覆盖层尾部计入）；
  - 首页仓储不可达 → 沿用既有"降级为进程内摘要"；**翻页中途失败如实抛出**（不伪装成
    "没有更多"），降级续翻只在游标来自上一次降级页时成立（离线多页遍历自洽）。
- `get_summary()` 改为按 ID 直读 + 覆盖层合并（详情/改名不再依赖任何列表窗口）。
- 删除对 `list_all` 的依赖（结构性守卫：用户列表路径碰 `list_all` 即红）。

**为什么最小**：不动契约、不动 cursor 格式、不动 page-size 策略、不动覆盖层合并规则、
不动鉴权/会话隔离；只把"取数原语"从"预取全量窗口"换成"存储层 keyset 一页"。

**为什么不是"把 200 改大/去掉"**：那只是把边界往后挪，且每请求读全量文档（会话 doc
内嵌消息窗口）；PG2 把"存储层分页"本身当成不变量，并配了守卫（§13/§14）。

---

## 8. 边界矩阵（route 级，修后实测）

| N（播种） | 可见总数 | 页数（limit=50） | 首/末页 | `has_more` | 缺失 | 重复 |
|---|---|---|---|---|---|---|
| 0 | 1 | 1 | 1 条 | false | 0 | 0 |
| 1 | 2 | 1 | 2 条 | false | 0 | 0 |
| 50 | 51 | 2 | 50 / 1 | true → false | 0 | 0 |
| 51 | 52 | 2 | 50 / 2 | true → false | 0 | 0 |
| 199 | 200 | 4 | … | true → false | 0 | 0 |
| 200 | 201 | 5 | 50/50/50/50/1 | true → false | 0 | 0 |
| 201 | 202 | 5 | 50/50/50/50/2 | true → false | 0 | 0 |
| 225 | 226 | 5 | 50/50/50/50/26 | true → false | 0 | 0 |

`limit` 夹取（`limit=500 → 100`）与非法值（`0`/`abc`）保持既有 contract；畸形 cursor
仍 400 `CONTRACT_VALIDATION`。

---

## 9. Completeness / Duplicate-Omission 证明

- **route 级**：`paginate_sessions()` 只依据 `has_more`/`cursor` 翻页 →
  `set(返回 IDs) == 播种 IDs ∪ {default}` 且 `len(ids) == len(set(ids))`；
  对 `N ∈ {0,1,50,51,199,200,201,225}` 全绿（`core/input/tests/test_sessions_pagination.py`）。
- **service 级**：同断言走 `SessionManager.list()`；额外覆盖覆盖层形态——改名最旧会话
  （有效键被抬高到 now）、离线创建后重连、重复文档、竞态 create
  （`core/memory/tests/test_session_manager_pagination.py`）。
- **storage 级**：`list_page` 逐页走遍 + `updated_at` 全同（打乱插入序）跨页 tie-break
  （`core/storage/tests/test_session_repo_pagination.py`）。
- **真库级（E3）**：201 / 225 条在**真实 MongoDB 驱动**上逐页走遍、同一游标两次结果一致、
  下一页与上一页零重叠、`updated_at` 全同且插入顺序打乱时期望**字典序**
  （`tests/real_mongo/test_real_session_pagination.py`，7 例）。

---

## 10. 元数据真值（`has_more` / `cursor`）

- 每页断言 `has_more ⇔ cursor 存在`（route 与 service 双层）。
- 走到末尾：空页/短页 + `has_more=false`，**不是** 404/400。
- 严格依据 metadata 停下的客户端（driver 的语义）看到的就是全集——避免"测试自己知道该继续"。
- **翻页中途存储失败**：如实抛错（不返回"短页 + has_more=false"）；离线降级只在首页或
  同一降级页游标上成立。

---

## 11. 前端/用户可达性

- 仓内 Vue 前端**不消费** `GET /api/sessions`（只用 `/debug/api/sessions`）；用户列表的真实
  消费方在**外部仓** `elysia-frontend`（`real-backend.ts` 发 `cursor`+`limit`、
  `conversation.store.ts` 沿 `nextCursor` 翻页，独立审计确认实现正确）。
- ⇒ P2-01r 的 root cause **不在前端**（B 证伪）；本批**未改任何前端代码**，只按 §64 跑了
  required build（全绿）。
- 会话/角色隔离：`/api/sessions` 不按用户或角色过滤（单用户应用），**无 scope 可回归**；
  但每页字段集与 `character` 绑定快照有逐页一致性守卫（§13）。
- 鉴权：路由仍走既有产品认证中间件，未改动。

---

## 12. 回归测试分类

| 类别 | 文件 | 数量 | 说明 |
|---|---|---|---|
| **PRE_FIX_RED** | `core/input/tests/test_sessions_pagination.py` | 21（修前 10 红） | 201/225 可达、边界矩阵、元数据自洽、search 跨页、limit 夹取、删除不复活、字段不漂移 |
| **PRE_FIX_RED** | `core/memory/tests/test_session_manager_pagination.py` | 30（修前 16 红 + 1 xpass） | 覆盖层有效键/竞态/重复文档/坏时间戳/中途失败/离线降级/search 语义一致 |
| **PRE_FIX_RED** | `core/storage/tests/test_session_repo_pagination.py` | 24（修前 23 红） | 存储层 keyset 原语、下推形状、单页读量、fail-closed |
| **PRE_FIX_RED(E3)** | `tests/real_mongo/test_real_session_pagination.py` | 7（修前 API 不存在） | 真驱动 201/225、tie-break、游标排他、单文档、搜索、批量读 |
| **ALREADY_GREEN_GUARD** | 同上三文件中的边界 `N ≤ 200` 用例、`has_more⇔cursor`、失败语义、离线降级 | 25 | 修前即绿——它们钉的是"不许在修的过程中改坏" |
| **PRODUCT_CONTRACT_GUARD** | `core/input/tests/test_session_routes.py`（既有 112 例）+ `core/storage/tests/test_fail_closed_convention.py` + `test_schema_drift.py` | — | 既有 route 契约、fail-closed 约定表、索引漂移表（本批按新增方法/索引同步登记） |
| **结构性守卫** | `test_list_does_not_prefetch_the_whole_dataset`、`test_list_reads_at_most_one_page_from_storage` | 2 | 前者毒化 `list_all`；后者记录下推 `limit` 并断言 ≤ limit+1（Challenger G1 采纳） |

修前/修后的隔离验证见 §19。

---

## 13. Challenger Findings

两个独立子代理（route/service 攻击面、fake-green/变异攻击面）+ 主 Agent 独立复现。
分类：**confirmed（已修）** / **fix weakness（已补守卫）** / **accepted risk** / **new-out-of-scope（只登记）**。

| # | 发现（子代理编号） | 分类 | 处置 |
|---|---|---|---|
| 1 | **F1/G3** 缺/`null` `updated_at` 的历史文档在 keyset 下不可达；页尾是这类文档时 `""` 游标会掐断其后记录 | **new-out-of-scope（部分已修）** | 本批只保证**不连坐健康记录**：投影层把非 `str` 归一为 `""`（消除 `None > str` / `max(None, str)` 的 `TypeError` → 整列表静默退化）。可达性本体登记 `KB-15` + xfail 钉住（真库探针实测） |
| 2 | **F2** `create()` 与 `list()` 竞态 ⇒ 同一 ID 在一页里出现两次（`_visible_local()` 被读两次） | **confirmed（已修）** | 快照一次并传入 `_local_entries(visible=...)`；另有去重兜底（见 #4） |
| 3 | **F4** 搜索两套语义（持久层 `$regex i` vs 覆盖层 `str.lower()`）⇒ 同一标题（U+017F）两种答案 | **confirmed（已修）** | 覆盖层改用 `re.IGNORECASE` 正则（与存储/fake 同族语义） |
| 4 | **F5** 重复 `session_id` 文档（该字段**无唯一索引**）⇒ 同 ID 两条 | **confirmed（已修）+ new-out-of-scope** | `_merged_page` 显式去重（保留先出现者 + warning）；schema 侧登记 `KB-16`（未动索引） |
| 5 | **F6** 一条坏文档 + 覆盖层 ⇒ 整份列表静默退化为 2 条本地记录 | **confirmed（已修）** | 同 #1 的归一化 + 新增守卫用例（含 `max()` 那条路径） |
| 6 | **F7** 翻页中途读失败被折成"短页 + has_more=false" | **confirmed（已修）** | 降级只在首页/降级页游标上成立；中途失败如实抛出（PG6 不变量 + 用例） |
| 7 | **F3** `list(limit<=0)` 曾给 `has_more=true` 且无游标（HTTP 不可达：路由夹到 [1,100]） | **confirmed（已修）** | `limit<1 → 1`，元数据恒自洽 + 用例 |
| 8 | **G1（最高价值的绿缺口）** manager 级"每请求读量"无人守：把 `list_page(limit=10**6)`（读全表、语义仍对）注入后 **303/303 全绿** | **fix weakness（已补守卫）** | 采纳其最小守卫：记录下推 `limit` 并断言 `≤ limit+1`（已在隔离 worktree 复验：baseline 绿 / 该变异红） |
| 9 | **G2** `has_more` 忽略"覆盖层剩余的尾部"这半（无有效检测） | **fix weakness（已补守卫）** | 新增"50 持久 + 更旧的本地-only 尾条"用例；变异（持久层翻完即丢覆盖层尾部）下该用例红 |
| 10 | **G4/G5** 未覆盖分支：manager 级超长搜索词、持久层候选 tombstone 跳过、`get_summary` tombstone 早退 | **fix weakness（已补用例）** | 三条分支各加一条用例 |
| 11 | **并发插入/改名期间的位移**（分页期间新记录/被抬高的记录当轮跳过；删除不复活） | **accepted risk** | 修前修后一致（keyset 固有语义）；产品接受 eventual pagination，不引入快照隔离；登记于 `todo.md` §4 |
| 12 | **M2b 变异只以挂死暴露**（流游标不推进时，测试循环无界） | **fix weakness（已修）** | 测试循环加上界（游标不推进 → 断言失败而非挂死） |
| 13 | **G6** debug 面板 `list_all()` 仍 ≤200（ADMIN 面、另一 endpoint） | **new-out-of-scope** | 登记 `OBS-7`（用户面已不依赖它） |
| 14 | **`SessionManager.list()` 只有 limit 下界、无上界**（非路由调用方可传巨值） | **new-out-of-scope** | 与 G1 同族；路由已夹取；登记于 `AR-17` 一带（本批不新增 API 面约束） |
| 15 | **fake-vs-real 分歧**：未知操作符被 fake 静默忽略（match-all）而真库抛 `OperationFailure`；`limit=0` 两侧一致；`$lt` 对缺失/null 两侧一致（type bracketing） | **记录** | 本批下推的 6 个算子（`$or`/`$lt`/`$gt`/`$in`/`$regex`/`$options`）当前 fake 全支持；**不新增**依赖（刻意避开 `$nin`/`$nor`）；分歧面登记在 §24 说明 |
| 16 | **`_local_summaries` 无淘汰无上限** ⇒ 每页 `$in` 覆盖层查询代价随进程内写量增长 | **new-out-of-scope** | 登记 `AR-17`（性能/内存，非正确性） |
| 17 | **rename 无 title 长度上限**（create 限 200、搜索词上限 200）⇒ 契约不对称 | **new-out-of-scope** | 登记 `KB-17` |

未能证伪的性质（两个 Challenger 都明确列出）：原始 200 上限缺陷类被修掉（M1/M2/M3/M4/
M5/M6/M7/M9/M10 全红）、真库 keyset/谓词/`limit`/tie-break 与设计一致、删除不复活、
搜索跨页不丢 filter、游标不重复不跳项、离线降级可多页遍历。

---

## 14. Mutation Evidence（隔离 worktree，修后代码）

命令：在 `/tmp/b8_prefix`（HEAD + 新测试 + 修复补丁）上逐条注入变异，跑三个新套件
（baseline **69 passed + 1 xfailed**）。

| 变异 | 语义 | 结果 | 判定 |
|---|---|---|---|
| **M1** | 仓储恢复"预取 200 再分页"（原缺陷形态） | **26 failed** | 杀掉 |
| **M2** | `list()` 忽略 cursor（永远第一页） | **32 failed** | 杀掉 |
| **M3** | `has_more=True` 却不给 `next_cursor` | **34 failed** | 杀掉 |
| **M4** | 去掉下推排序的 `session_id` tie-break | **3 failed** | 杀掉 |
| **M5** | 覆盖层不从持久层流里剔除（抬高记录重复） | **1 failed** | 杀掉 |
| **M6** | `get_summary` 退回 ≤200 列表视图 | **1 failed** | 杀掉 |
| **M7** | 覆盖层两次读取不同快照（F2 回归） | 69 passed | **未杀**（被 M8 的去重独立覆盖——同一不变量的两条防线，见下） |
| **M8** | 去掉重复文档去重 | **1 failed** | 杀掉（首版用例 limit=1 时未杀，已加固为 limit=10） |
| **M9** | 中途失败也降级（把故障伪装成"没有更多"） | **1 failed** | 杀掉 |
| **M10** | 投影不归一 `updated_at`（坏文档连坐整列表） | **1 failed** | 杀掉（首版未杀，已加固为"坏文档 + 其覆盖层"触发 `max()` 路径） |
| **M-G1** | `_merged_page` 改 `list_page(limit=10**6)`（读全表、语义仍对） | **1 failed** | 杀掉（Challenger G1 采纳的守卫；修复前该变异 303/303 全绿） |
| **M-G2** | 持久层翻完即丢覆盖层尾部 | **20 failed** | 杀掉（Challenger G2 采纳的守卫） |

`M7` 的说明：快照与去重**互为保险**——去掉任一条，另一条仍保证"同一 ID 只出现一次"，
所以单独注入 M7 不红是**设计上的重叠**，不是守卫缺口；用户可见不变量的守卫是 M8 那一侧
（已红）。

---

## 15. Public API / Schema Impact

| 面 | 变化 |
|---|---|
| `GET /api/sessions` 响应 | **无**（`sessions/has_more/cursor` 逐字段不变；`cursor` 值格式不变） |
| `SessionSummary` | **无**（`conversationId/title/messageCount/updatedAt/active/character`） |
| 请求参数 | **无**（`cursor/limit/search` 语义不变；`limit` 夹取不变） |
| 错误码 | **无**（畸形 cursor 仍 400 `CONTRACT_VALIDATION`；末尾空页仍 200） |
| `frontend_contract` | **零改动**（未跑 `make gen-types` 的需要；`make check-types` 绿） |
| `GET /api/sessions/{id}` | 语义修正（第 201+ 条不再 `updatedAt: ""`），**形状不变** |

---

## 16. Persistence Impact

| 面 | 变化 |
|---|---|
| 集合/字段 | **无** |
| 索引 | **新增一条**：`sessions.updated_at_-1_session_id_1`（复合，非唯一、无 TTL） |
| 迁移 | **不需要**（ARCH-INC-P1-04：新增索引由 `connect()` 的 `ensure_index` 幂等覆盖） |
| `schema_drift` | `EXPECTED_INDEXES["sessions"]` 同步登记（`test_schema_drift.py` 的两条数字/缺失断言同步更新） |
| 破坏性操作 | 无（未动 `drop_index`；旧单键 `updated_at_-1` 保留——它仍是"只看时间"查询的可用索引，删除属非增量变更、需人工决策） |

---

## 17. Performance Impact（含"为什么这条索引是必需的"）

真 Mongo 实测（测试容器，同一份数据，keyset 分页查询）：

| 形态 | 计划 | 20k 会话中位耗时 |
|---|---|---|
| 旧实现 `find({}).sort(updated_at -1).limit(200)` | `LIMIT > FETCH > IXSCAN(updated_at_-1)` | 1.9 ms |
| **本批查询，无复合索引** | `SUBPLAN > SORT > FETCH > OR > IXSCAN`（阻塞排序） | **23.3 ms** |
| **本批查询 + `(updated_at -1, session_id 1)`** | `SUBPLAN > LIMIT > FETCH > SORT_MERGE > IXSCAN ×2` | **1.2 ms** |
| 搜索（title `$regex`） | 扫描（非锚定正则无法走 b-tree） | 0.9 ms（20k） |

⇒ 不建这条索引等于**用 12× 的每页回归换分页正确性**，故它是修复的一部分（而非"顺手优化"），
并已在 `schema_drift` 登记（§16）。另外每请求的**读取条数**从"≤200 条完整文档"降为
"≤limit+1 条"（默认 51），内存/网络都更省。

---

## 18. Commits

| commit | 内容 |
|---|---|
| `36cb7cec` | `fix(session): make pagination complete beyond the first 200 records`（生产修复 + 新回归测试 + 既有守卫同步） |
| `9ee09e09` | `docs(architecture): record pagination completeness invariants (PG1-PG6)` |
| （本记录提交） | `docs(review): record Batch 8 execution + close P2-01r`（todo.md 状态更新同提交） |

---

## 19. Worktree Verification（独立 worktree）

```
git worktree add /tmp/b8_prefix HEAD          # 独立 worktree = 未修代码
cp <4 个新测试文件> /tmp/b8_prefix/...          # 只带测试，不带修复
pytest 三个新套件 → 49 failed, 25 passed, 1 xpassed     # PRE_FIX_RED（含 1 条"修前可达"的 xpass，见 KB-15）
git apply /tmp/b8_fix.patch                   # 同一 worktree 打上修复
pytest 三新套件 + 5 个既有相关套件 → 324 passed, 1 xfailed  # GREEN
```

`1 xpassed` = `test_missing_updated_at_row_is_unreachable_registered_gap`：修前坏文档
**可达**（旧实现内存分页、投影成 `""`），修后不可达 ⇒ 该用例用 `xfail(strict=False)`
把这条**已知缺口**如实钉住（修好时会 XPASS 而不是变红）。

---

## 20. Test Results

| 档位 | 结果 |
|---|---|
| targeted（3 新套件 + 5 既有相关）| **324 passed, 1 xfailed**（隔离 worktree，见 §19） |
| 边界矩阵 | route 8 组 N + service 7 组 N + storage 8 组 N（§8） |
| 顺序扰动 | 同 `updated_at` 全同 + 打乱插入序（storage 层 + 真库层各一条）；`limit=2` 时页边界正好落在同值记录之间 |
| 真实存储 | `make test-real-mongo` → **22 passed**（含本批 7 例） |
| `make test-quick` | **6140 passed, 2 skipped, 1 xfailed** |
| `make test-normal` | **6146 passed, 2 skipped, 2 xfailed** |
| `make test-full` | **6178 passed, 34 skipped, 2 xfailed**（453s） |
| `make check-test-sets` | Quick ∪ remainder == Normal（6150）；全量收集 6214 |
| frontend/build | `npm run build` 退出 0（本体未改；`static/` 为 gitignored 产物） |
| static gates | `lint` 干净、`pyright` **0 errors**、`check-arch` / `check-contract` / `check-types` / `check-timeouts` / `check-debug-auth` / `check-secrets` / `check-compose` / `check-env-lock` / `check-ci-isolation` 全绿 |
| real CI | 见 §26 |

---

## 21. Invariant Mapping

`docs/architecture/invariants.md` §4.1（新增 PG1–PG6）：

| ID | 不变量 | 主要守卫 |
|---|---|---|
| **PG1** | 按公开合同翻页能到达全部可见记录 | route 21 例 + service 30 例 + 真库 7 例 |
| **PG2** | 单页上限 ≠ 数据集上限；分页在存储层完成 | `test_page_read_never_fetches_more_than_the_page`、`test_list_reads_at_most_one_page_from_storage`、`test_list_does_not_prefetch_the_whole_dataset` |
| **PG3** | 不重不漏 + 稳定序（tie-break 下推） | 边界矩阵 + 同值跨页（fake + 真库） |
| **PG4** | 元数据自洽（`has_more ⇔ cursor`；末尾空页非错误） | route/service 元数据用例 |
| **PG5** | 单文档读与列表窗口无关 | service + storage + 真库各一条 |
| **PG6** | 中途失败不得伪装成"列表到底了" | service 两条 + 离线降级用例 |

---

## 22. Todo Update

- `P2-01r`：§1.2 行改为 **GUARDED**（`~~strikethrough~~` + 提交号），§3 TRUTHFULNESS 主表
  补一行（`has_more=false` 谎报 → 已修），§5 Closed 索引补一行；另一半"过期级联清理 worker"
  标注 **SUPERSEDED**（§2.4 证据）。
- 新登记（只登记不修）：`KB-15`（坏 `updated_at` 不可达/掐断）、`KB-16`（`session_id` 无唯一
  索引 ⇒ 重复文档）、`KB-17`（rename 无 title 上限 vs 搜索词上限 200）、`OBS-7`（debug 面板
  `list_all` ≤200）、`AR-17`（`_local_summaries` 无上限 ⇒ 每页 `$in` 代价）、§4 的
  keyset 并发写位移（accepted risk）。
- 其它 canonical 条目状态**未改动**。

---

## 23. Keep As-Is（本批刻意不动）

- keyset 游标技术路线、cursor 值格式、`limit` 默认 50 / 上限 100；
- 会话列表**不加** `total` 字段（契约没有；`has_more` 用 limit+1 探测已足够）；
- 覆盖层合并规则（`max(updated_at)`、`max(message_count)`、title/binding 覆盖）；
- `list_all()` 作为 debug 面的**有界诊断读**（≤200）保留；
- 鉴权/产品围栏/会话隔离（本批零改动）；
- 不引入分页框架、不引入快照隔离/事务；
- 前端（外部仓）不改 —— 服务端修好后其既有游标翻页实现即可走遍全集。

---

## 24. New Findings（只登记，不修）

见 §22 的五个新 ID。补充两条**不单列 ID** 的记录：

1. **fake-vs-real 操作符安全网缺口**：`FakeDocumentDBBackend._field_matches` 对未知操作符
   **静默忽略**（相当于"匹配全部"），而真库抛 `OperationFailure`。本批下推的 6 个算子当前都被
   fake 支持，且**刻意避开** `$nin`/`$nor`（避免 fake-green），但"新算子会静默退化"这件事本身
   仍是缺口（属 GR-* 家族，需独立登记语义）。
2. **`SessionManager.list()` 无 limit 上界**（路由已夹取 [1,100]；非路由调用方可传巨值 ⇒
   单页读量放大）。与 G1 同族，修法是"API 边界统一夹取"，属独立评估。

---

## 25. Follow-up（canonical backlog 其余项保持原状）

KB-6（TTS FSM SPEAKING）、KB-12（预览窗口估算误差带）、**KB-13 + Ollama Provider Adapter
（AR-16）**（明确延期，见 `docs/review/2026-09-18-ollama-runtime-context-authority-记录.md`）、
OBS-6（409/404 存在性语义）、KB-11（角色归档重试读缓存）、GR-25（测试鉴别力）、N-15/GR-3
（索引选项登记）、F2/FEAT-1（提醒持久化）、AR-4/AR-5（provider portability）、以及 §1.4/§1.5
的 DEP-*/F14 等。**本批未触碰其中任何一项。**

---

## 26. CI 验证状态

**本机无 GitHub 凭据（`git push` 报 `could not read Username for 'https://github.com'`，
与 Batch 4–7 同一环境限制）⇒ 本地三个提交已就绪，真实 CI 由 owner push 后触发。**

push 后需核对的 required job（`docs/ci.md`：本仓只有 push 触发器）：

| job | 期望 | 本批相关面 |
|---|---|---|
| `backend` | 绿 | `make lint` / `pyright` / `check-arch` / `check-contract` / `check-types` / `check-timeouts` / `check-debug-auth` / `check-secrets` / `check-test-sets` / `check-compose` / `check-env-lock` / `check-ci-isolation` + `test-quick`/`test-normal` |
| `real-mongo` | 绿 | `tests/real_mongo/test_real_session_pagination.py`（7 例）——本机已跑整层 `make test-real-mongo`（22 passed） |
| `frontend` | 绿 | 前端未改；本机 `npm run build` 退出 0 |
| `contract-frames` | 绿 | 契约层零改动；本机 `make check-types` 绿 |

本地等价证据（全部已执行，见 §20）：`test-quick` 6140 ✅ / `test-normal` 6146 ✅ /
`test-full` 6178 ✅ / `test-real-mongo` 22 ✅ / 全部静态门禁 ✅ / 前端 build ✅。
