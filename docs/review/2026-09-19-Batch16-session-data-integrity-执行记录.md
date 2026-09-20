# Batch 16 — Session Data Integrity（KB-15 / KB-16）执行记录

> ⚠️ **Historical execution record.** 本文描述**执行当时**的系统与结论；后续批次可能推翻其中部分判断。
> 当前事实见 [docs 索引](../README.md) 的 Canonical References（架构 / 不变量 / 测试 / CI / 运维 / 契约）。
> 正文**按当时原样保留**——不改结论、不删失败轨迹、不重写 Challenger 反证（`docs/readme-conventions.md` Rule 3）。

> 范围：canonical backlog 仅 **KB-15**（malformed/missing/null `updated_at` 导致 persisted
> session 不可达）与 **KB-16**（逻辑 `session_id` 缺少数据库唯一性约束）。**KB-17 默认
> OUT OF FIX SCOPE**（本批只做边界核对，未改 title 校验）。不做通用迁移框架、不做自动
> 删除/合并重复文档、不改文档 schema、不改公开分页 API、不恢复全量预取。
>
> 结果：**KB-15 = GUARDED**、**KB-16 = GUARDED**（各自独立 E3 真库证据 + PRE_FIX_RED +
> 不变量 SD1–SD6 + 变异被杀）。真实开发库只读审计：**零重复、零 malformed**（缺陷不是
> "库里已经坏了"，而是"契约缺定义 + 约束缺强制"）。登记 3 条
> `NEW_FINDING_OUT_OF_SCOPE`（KB-58/59/60）。

---

## 0. 结论摘要

| 问题 | KB-15 | KB-16 |
|---|---|---|
| 确认？ | **确认**（描述成立，但根因被原登记**低估**：不止"漏项"，还有"游标毒化 → 400 掐断"） | **确认**（逻辑主键无存储层唯一约束；并发 upsert 可造重复） |
| 证据级 | **E3**（真实 MongoDB 8 + 真仓储/管理器/路由三层复现） | **E3**（真实 MongoDB 8；含**确定性** TOCTOU 复现 2 篇同 ID） |
| 可达性 | `LEGACY_DATA_ONLY`（仓内任何历史版本的写者都恒写非空字符串，见 §7 写者审计） | `INTERNAL` 写路径 + `DEPLOYMENT`（索引引导是部署面） |
| 严重度 | LOW（原登记）→ **LOW**（但用户面后果比原描述重：整条列表可被 400 掐断） | LOW → **MEDIUM**（身份唯一性缺失会污染直读/改名/删除/归档关联的权威选择） |
| Root | **A + D**（legacy/外部数据 + keyset 查询未定义 malformed 排序域）；**B/C 证伪** | **D（约束缺失）**：应用层 find→upsert 结构性非原子 |
| 修法 | 排序域**显式两桶**（有序 ⊕ legacy Sentinel）+ 游标编解码对称 | 受守卫的**索引升级**（drop+重建 / 有脏数据则降级）+ 写路径冲突收敛 + schema_drift 登记 |
| 公开 API 变化？ | **无**（响应字段/错误码不变；cursor 格式不变，只多接受一个此前非法的 Sentinel 值） | **无** |
| 文档 schema 变化？ | **无**（零字段改动，零 migration） | **无** |
| 索引变化？ | **无** | **`sessions.session_id_1` 由非唯一升级为唯一**（升级路径见 §26/§28） |
| 需要 migration？ | **不需要**（legacy 文档原地可达，不写回、不修复） | **不需要**（唯一索引由 `connect()` 幂等引导；有脏数据时降级为显式告警，不自行删数据） |
| 真实 CI？ | 见 §45（push 后核对 backend / real-mongo / frontend / static jobs 全绿） | 同左 |

---

## 1. Scope Lock

| 项 | 结论 |
|---|---|
| canonical 条目 | **KB-15**、**KB-16**（§1.2 Correctness，两条都是 Batch 8 的 `NEW_FINDING_OUT_OF_SCOPE`） |
| 允许检查 | `SessionRepository`（读/写/索引引导）、`SessionManager`（游标/归并/覆盖层）、session schema 投影、`GET /api/sessions`、`GET /api/sessions/{id}`、搜索、`list_all`（debug）、`schema_drift` 与 Batch 3.6 AST 守卫、内存 fake 后端、真库测试层 |
| 明确不碰 | 通用迁移框架、Mongo ORM 重构、`SessionService` 大重构、pagination API redesign、total-count API、前端 history UX、**title 校验（KB-17）**、消息归档 schema、会话生命周期/FSM/EventBus、KB-30 事务、Ollama/KB-13/AR-16、KB-12 token 估算、通用数据清洗子系统 |
| 越界发现 | 只登记（§49）：KB-58（历史 `updated_at` 字符串格式不统一 ⇒ 字典序不保时序）、KB-59（rename 不写持久 `updated_at`，覆盖层抬升导致重启回落）、KB-60（fake 后端保真度缺口） |

---

## 2. Phase 0 — 只读 Data Contract Audit（做了什么）

**读**：`todo.md`（KB-15/16/17 原登记）、`docs/review/2026-09-18-Batch8-pagination-completeness-执行记录.md`、
`docs/architecture/invariants.md`（PG1–PG6）、`docs/database-storage.md` §五、
`core/storage/schema_drift.py`、`Module/database/documentdb/{protocol,mongodb}.py`。

**代码**：`SessionRepository` 全部读写、`SessionManager`（list/get_summary/覆盖层/游标）、
`SessionMemory.save` 的 JSONL fallback 与三处 restore-into-Mongo、`Agent._start_persistence`
与 `_STARTUP_STEPS`、`session_routes.py` 的 list/detail/PATCH/DELETE、`debug_routes/archive.py`
的旁路写、脚本与测试里的全部 session 写者、内存 fake 后端、真库测试层 conftest。

**三个并行只读审计子代理**（索引/守卫；写者权威图谱；测试与 fake 保真度）产出的关键输入：

- `ensure_index` 无类型化参数、同名不同选项抛 `OperationFailure` 并被 `connect()` 吞成
  "仓储不可用"（S2-B1 形态）；
- `sessions` 三条索引全非唯一、无 TTL、`EXPECTED_INDEX_OPTIONS` **不含 `sessions`**
  （⇒ 运行期一个选项都不比）；
- Batch 3.6 守卫只保证"代码声明的索引 ↔ 表"一致，**不保证"该声明的索引都声明了"**；
- 全仓唯一 session 写者 = `core/storage/session_repo.py`；`updated_at` 自 2026-05-25 起
  **始终是字符串**（格式换过三次：naive → `+08:00` → UTC），字段名从未变；
- `created_at` **任何版本都没写过**（真库审计 7/7 缺失）；
- fake 后端：未知算子静默 match-all、混类型比较抛 `TypeError`、`aggregate()` 恒 `[]`。

**本阶段禁止 production change**（遵守：只读探针 + 只读审计脚本，写操作只在独立测试库）。

---

## 3. Session Persistence Contract（权威文档形态）

| 字段 | 地位 | 说明 |
|---|---|---|
| `session_id` | **REQUIRED / 非空字符串 / 逻辑主键** | 三条写者恒写；全仓按它寻址（§4） |
| `updated_at` | **REQUIRED（写入侧）、NON-NULLABLE（写入侧）**；读入侧允许缺失 | 规范形态 = `now_iso()` 的 UTC ISO 字符串；**排序键就是它本身**，不做任何格式解析 |
| `messages` | OPTIONAL | `create()` 写 `[]`；内嵌窗口由 `session_max_messages` 界界（完整历史在 `message_archive`） |
| `title` | OPTIONAL | 缺省 `""`；`save()` 保留非空 title，否则由首条 user 文本派生 |
| `character_binding` | OPTIONAL | 缺省 `{}`；`character_profiles` 能力下有完整性自检 |
| `character_state` / `reflector_state` / `service_state` | OPTIONAL | `create()` 不写，首次 `save()` 才出现；`load()` 缺省构造默认值 |
| `created_at` | **不存在** | 真库 7/7 缺失；历史的任何写者都没写过 ⇒ **不可作为 fallback 候选** |
| 非空字符串以外的一切 `updated_at` 形态 | **LEGACY OPTIONAL（legacy 桶）** | 可达、排最后、桶内 `session_id ASC`；**不视为 corruption、不伪造时间戳** |
| `session_id` 缺失/null/非字符串/空串 | **CORRUPTION（invalid）** | 无身份、不可寻址；唯一索引把它们并入 null 桶（至多一篇）；本批不修、只让约束暴露它 |

**DERIVED**：无（投影层只做归一，不派生新字段）。
**NULLABLE**：`updated_at`（仅历史/外部数据）；**NON-NULLABLE**：`session_id`、写入侧的 `updated_at`。

---

## 4. Session Identity Contract（`session_id` 是不是逻辑主键）

**是**——逐调用方核对（全部假设 `session_id → exactly one logical session`）：

| 调用方 | 位置 |
|---|---|
| 直读（load / get_summary） | `session_repo.load` / `get_summary`（`find({session_id}, limit=1)`） |
| 更新（save / rename） | `update_one({session_id}, …)`（**只改一篇**，重复时另一篇永久分叉） |
| 删除 | `delete_many({session_id})`（重复时全删——这一条本来就把两份当同一身份） |
| 摘要 / 批量摘要 | `get_summary` / `summaries_by_ids`（后者按 session_id 建字典，天然去重 ⇒ 静默吞一份） |
| 分页 | `list_page` 的 tie-break 键、游标编码 |
| 前端路由 | `GET|PATCH|DELETE /api/sessions/{conversationId}`、`/api/sessions/{id}/messages|resources|generations|pending` |
| 归档关联 | `message_archive` / `token_usage` / `generations` / `character_versions` 全部按 session_id 分区；级联删除清单按它走 |
| 缓存键 | `SessionManager._sessions` / `_local_summaries` / `_work_counts` / tombstone 集合 |
| 幂等/引用 | 待清理 journal、附件释放、pending attachment release |

⇒ `_id` 不是身份（它只是存储实现细节，外部可见面从不暴露）；`session_id` 是**逻辑主键**，
数据库允许重复就是 **integrity gap**。**KB-16 不需要 reclassify。**

---

## 5. Session Ordering Contract（当前真实合同）

```text
primary sort      : updated_at DESC
tie breaker       : session_id ASC
cursor encoding   : base64url("<sort_key>|<session_id>")     # 格式不变
cursor comparison : (k' < k) OR (k = k AND s' > s)
sentinel          : k == "" 表示 legacy 桶（无可用排序键）
null/missing 行为 : 落入 legacy 桶 —— 排在**所有**有序记录之后，桶内 session_id ASC
malformed 行为    : 同上（非字符串/空串/缺失/null 一视同仁）
```

**修前**的真实合同（本批纠正）：排序/谓词压在**原始** `updated_at` 上，于是
① 缺失/null/数字型排在 BSON 末位且谓词永不匹配 ⇒ 静默漏项；② Date 型排在字符串**之前**
（BSON 序）而投影归一为 `""` ⇒ 落在页边界时产出解码不了的游标 ⇒ 下一页 400；
③ 全同 `updated_at` 时 tie-break 已在查询里（Batch 8）——这一点保持不变。

---

## 6. Reachability Contract（R1–R4）

| 级别 | 定义 | 本批结论 |
|---|---|---|
| **R1 直读** | 给定 `session_id`，`load`/`get_summary` 能找到 | 修前**已成立**（与 `updated_at` 无关）；新增真库回归（`test_direct_lookup_still_works_for_legacy_rows`） |
| **R2 枚举** | 正常历史分页有限翻页后能找到 | **修前不成立**（KB-15 本体）→ 修后成立 |
| **R3 搜索** | title 命中时搜索路径能找到 | 修前仅在"命中数少于一页"时成立 → 修后跨页成立（§19、§32） |
| **R4 摘要** | summary API 能返回 | 修前已成立（走单文档直读）→ 保持不变 |

---

## 7. Data Audit（真实开发库，只读；**只记录数量与结构，不输出任何会话内容**）

脚本：`tmp/b16/main/audit_real_db.py`、`tmp/b16/main/audit_ts_format.py`（只 count/aggregate/list_indexes）。

| 指标 | 值 |
|---|---|
| `sessions` 文档数 | 7 |
| `updated_at` = 合法非空字符串 | 7 |
| `updated_at` 空串 / 缺失 / 显式 null / 非字符串 | 0 / 0 / 0 / 0 |
| `updated_at` 格式分布 | UTC `+00:00` 7；非 UTC 偏移 0；naive 0；`Z` 结尾 0 |
| `session_id` 缺失 / null / 空串 / 非字符串 | 0 / 0 / 0 / 0 |
| **重复 `session_id`（group by having count>1）** | **0 组 / 0 篇** |
| `created_at` | 7 篇**全缺失** |
| 索引实况 | `_id_`、`session_id_1`(非唯一)、`updated_at_-1`、`updated_at_-1_session_id_1`（**无孤儿**，与声明一致） |
| 关联集合规模（只计数） | `message_archive` 19 / `token_usage` 69 / `generations` 188 / `character_versions` 1 |

**测试数据 ≠ 生产数据（§8）**：审计干净**不能**证伪两条 finding——它们问的是
"当前生产代码是否结构上允许未来制造这种状态"以及"过去版本是否可能已经制造"：

- KB-15：仓内**任何历史版本**的写者都恒写非空字符串 ⇒ 该形态只能来自外部工具/手工写入；
  但**契约**（查询域）从未定义这类文档的行为 ⇒ 一旦出现就静默漏项/掐断（ROOT A + D）。
- KB-16：**当前**写路径就能制造（并发 upsert），真实复现见 §20。

---

## 8. Writer Authority Map

| Writer | 操作 | session_id 保证？ | updated_at 保证？ | 类型保证？ | 可造重复？ |
|---|---|---|---|---|---|
| `session_repo.save()` | upsert `$set` | ✅ 参数 | ✅ `now_iso()` | ✅ str | **✅（并发 find→upsert 窗口）** |
| `session_repo.create()` | upsert `$set` | ✅ `conv_<uuid12>` | ✅ `now_iso()` | ✅ str | **✅（同上）** |
| `session_repo.ensure_exists()` | upsert `$setOnInsert` | ✅ `"default"`/脚本 | ✅（仅插入时） | ✅ str | **✅（find→upsert 窗口）** |
| `session_repo.rename()` | `$set {title}`（无 upsert） | ✅ | ❌ **不写 updated_at**（→ KB-59） | — | 否（只命中一篇） |
| `session_repo.delete()` / `clear_all()` | `delete_many` | ✅ | — | — | 否（全删） |
| `session_repo.connect()` | `ensure_index` ×3 | — | — | — | **修前：约束缺失的根因**；修后：唯一索引引导 |
| `SessionMemory.save()` JSONL fallback | 写 `data/session_*.json` | ✅（文件名） | ✅ `now_iso()` | ✅ str | 否（单文件） |
| JSONL → Mongo 三处 restore | 经 `repo.save()` | ✅ | ✅ | ✅ | 同 save |
| `debug_routes/archive.py` | 经 `session_repo.delete` / `clear_all` | ✅ | — | — | 否 |
| `scripts/backfill_*` | `$set messages.<i>.*`（按 `_id`） | 不改 | 不改 | — | 否 |
| `scripts/dev/seed_stage_h_pinned.py` | `ensure_exists` / `delete` | ✅ | ✅ | ✅ | 同 ensure_exists |
| `scripts/capture_ws_trace.py` | 注入真仓储 + `switch()` | ✅ | ✅ | ✅ | 同 save |
| 测试（real_mongo / fake） | 裸 `insert_one` | 测试自定 | 测试自定 | 测试自定 | 测试**有意**制造 |

**结论**：Python model 的类型（`SessionSummary.updated_at: IsoTimestamp`）**不是** DB 约束；
真实写入形态由 `$set` 字典决定。KB-15 的 malformed 形态**没有任何生产写者**（ROOT B/C 证伪），
KB-16 的重复**有**生产写者（ROOT D）。

---

## 9. KB-15 — Pre-Fix Reproduction（真库 E3）

脚本：`tmp/b16/main/probe_kb15_e3.py`（独立库 `b16_probe_*`，跑完即弃）。

**Mongo 语义（mongo:8 实测，`sort=updated_at DESC`）**：

```text
1. Date 型   2. Bool 型   3. Object 型   4. 字符串(降序)   5. 空串   6. 数字型   7. null   8. 缺失
{"updated_at": {"$lt": "<str>"}}       → 只匹配字符串（类型括号）；缺失/null/数字/Date 全不匹配
{"updated_at": {"$gt": ""}}            → 恰好 = 非空字符串
{"updated_at": {"$not": {"$gt": ""}}}  → 恰好 = 其补集（**含字段缺失**）
{"updated_at": null}                   → 缺失 + 显式 null
```

**当前 HEAD 的完整分页（真 SessionManager + 真仓储，limit=50）**：

| 数据集 | 枚举数 | 可达性 | 备注 |
|---|---|---|---|
| 205 有序列 + 3 缺失 | 206 | **205/208** | 3 条永久不可达 |
| 205 有序列 + 3 null | 206 | **205/208** | 同上 |
| 5 有序列 + 3 缺失（一页装得下） | 9 | 8/8 | **只有这种形态才露过面**（掩盖了缺陷） |
| 205 有序列 + 1 个 Date 型 | 207 | 206/206 | 可达**但**它排在第一位 ⇒ 见下 |

**游标毒化（真库）**：`limit=2`、`[Date 型, 5 条字符串]`：

```text
page1: ids=['default', 'z_date']  has_more=True  cursor='fHpfZGF0ZQ=='   # base64("|z_date")
       decode → ValueError: 游标格式非法
page2 → ValueError（路由折 400 CONTRACT_VALIDATION）   ← 该页之后全部不可达
```

---

## 10. Malformed Metadata Matrix（missing / null / wrong type / 空串）

| 形态 | 修前 | 修后 | 覆盖用例 |
|---|---|---|---|
| `updated_at` 合法非空字符串 | 有序桶，可达 | 不变（顺序逐项相同） | `test_ordered_bucket_order_is_unchanged_for_healthy_data` |
| 缺失（字段不存在） | **不可达**（谓词永不匹配） | legacy 桶，可达、排最后 | `test_legacy_rows_are_reachable_for_every_dataset_size[49/50/51/205]`、manager/route/真库对应用例 |
| 显式 `null` | **不可达** | 同上 | 同上 |
| 空串 `""` | 匹配谓词但排最末；**落在页边界即毒化游标** | legacy 桶（Sentinel） | `test_legacy_row_at_page_boundary_does_not_poison_the_cursor` |
| 错误 BSON 类型（Date / 数字 / 布尔） | Date 型**排最前**且归一成 `""` ⇒ 必然毒化；数字/布尔型不可达 | legacy 桶，可达、排最后 | `test_wrong_bson_type_rows_join_the_legacy_bucket`、真库 `test_wrong_bson_type_rows_are_reachable_and_ordered_last`（SD-T6） |
| **数组型**（含非空字符串元素） | 元素遍历 ⇒ 匹配 `$gt: ""`（按**最大元素**排最前）却被投影成 `""` ⇒ 游标 Sentinel 撞车：**整个有序桶被跳过**（挑战者 A 反证；见 §33.1） | 由 `$not: {$type: "array"}` 排除出有序桶 ⇒ legacy 桶，可达、排最后 | `TestArrayValuedOrderingMetadata`（fake）、`TestArrayValuedOrderingMetadataRealMongo`（真库） |

**wrong BSON type 是否真实可达**：仓内写者不可能（§8）；外部工具/手工/未来 schema 变更可能。
本批选择**兼容**（而不是划为 unsupported corruption）——因为让它们可达的成本是零（同一套桶判据），
而"显式 quarantine"反而要新增诊断面。**边界写在 §14。**

---

## 11. Keyset Query Analysis

**修前**：`find(filter ⊕ cursor_pred, sort=[(updated_at,-1),(session_id,1)], limit)` —— 单一查询，
排序键与投影键不一致（§5）。

**修后**（两桶，最多 2 次 `find`，仍 O(limit)）：

```text
有序桶  : {"updated_at": {"$type": "string", "$gt": "", "$not": {"$type": "array"}}}
          ⊕ search ⊕ cursor_pred
          sort=[("updated_at", -1), ("session_id", 1)]   limit = 本页剩余
legacy桶: {"$nor": [有序桶判据]} ⊕ search
          ⊕ (游标在桶内时) {"session_id": {"$gt": S}}
          sort=[("session_id", 1)]                        limit = 本页剩余
（有序桶已取满一页 ⇒ 不查 legacy；游标 `k==""` ⇒ 不查有序桶）
```

判据必须是**真补集**且有序判据必须是"非空字符串**标量**"——否则会出现"某形态既不在
有序桶、也不在 legacy 桶"（数组型，挑战者 A）或"有序桶文档投影成 Sentinel"（游标撞车）。

**真库 `explain`（`tmp/b16/main/probe_explain.py`）**：

| 查询 | 计划 | docsExamined | keysExamined |
|---|---|---|---|
| 修前 HEAD 谓词 | `IXSCAN(updated_at_-1_session_id_1)` + 索引序 | 51 | 51 |
| 修后有序桶（+`$gt:""`） | `IXSCAN(updated_at_-1_session_id_1)` + 索引序 | 51 | 51 |
| 修后 legacy 桶（第一版 `$not $gt ""`） | `IXSCAN(updated_at_-1_session_id_1)`，indexBounds `[MaxKey,"{}"] ∪ ["",MinKey]` | 2 | 4 |
| 修后 legacy 桶（**最终** `$nor` 真补集） | `IXSCAN(session_id_1)` + 索引序，整条索引扫描 | | == 集合大小 |

⇒ **正常路径（有序桶，含首页/深页）仍是索引序 + 单页读量**；legacy 桶的 `$nor` 拿不到
索引范围（O(N) 索引扫描、无阻塞排序）——但它只在**有序桶取尽之后**被调用（一次遍历的
最后一页，或小数据集上的每次请求），用这点代价换"补集正确性"是划算的（挑战者 A 的数组
反证证明"省这点代价"会直接漏数据）。
多键数据集（集合里存在数组型 `updated_at`）会让复合索引变 multikey、有序查询的
`docsExamined` 上升——这与谓词无关（旧谓词同样如此），是数组数据本身的代价。

---

## 12. Cursor Boundary Analysis（KB-15 的第二个出口）

- 修前：`_encode_cursor("", sid)` 产出 `base64("|sid")`，而 `_decode_cursor` 要求
  `updated_at` 非空 ⇒ **生产者能产出、消费者拒收** ⇒ 路由 400。
- 修后：解码端接受空时间戳（Sentinel），仅要求"存在 `|` 分隔符且 session_id 非空"。
  - 旧游标（非空时间戳）**逐字节兼容**；
  - 没有 legacy 文档时**永不**产出空时间戳游标 ⇒ 公开 API 零变化；
  - 路径上不存在"空页死循环"（游标恒推进：`session_id` 严格递增）。
- 攻击面：`base64("|sid")`（合法）、`base64("ts|")`（拒）、`garbage`（拒）、非 ASCII（拒，
  `UnicodeEncodeError` 是 `ValueError` 子类 ⇒ 400 而非 500）、sid 含 `|`（首个 `|` 分割，兼容）。

---

## 13. User Impact

- **静默漏项**：用户在历史列表里**看不到**这些会话；它们仍可被直读/删除/改名（REST 与 v2 路由
  都按 ID 走），于是表现为"会话存在但列表里找不到"——`has_more=false` 让客户端**停止翻页**，
  用户没有任何线索。
- **游标毒化（更重）**：只要有一篇 Date 型/空串文档落在页边界，客户端拿到的 `cursor` 会在下一次
  请求被判 400 `CONTRACT_VALIDATION` ⇒ **该页之后的全部会话不可达**（外部前端仓把游标当不透明串
  回传，不具备"跳过坏游标"的能力）。
- 本仓 Vue 前端不消费 `GET /api/sessions`（用户列表消费方在外部仓 `elysia-frontend`），
  故影响面 = Electron/外部客户端 + debug 面板（`list_all`）。

---

## 14. Supported / Corrupt Data Boundary（§81 的落纸）

> **属于 supported dataset**：集合里任何一篇带**非空字符串 `session_id`** 的文档。
> 它的 `updated_at` 无论是非空字符串（有序桶）还是缺失/null/非字符串/空串（legacy 桶），
> 都必须能被正常分页枚举到。
>
> **视为 corruption（不属于 supported dataset）**：`session_id` 缺失 / null / 非字符串 /
> 空串的文档——它没有可寻址身份，任何 API 都无法操作它。本批**不**把它藏起来
> （它在 legacy 桶里照样可达、`sessionId` 会是空串），也**不**伪造修复；唯一索引把它们
> 并入 null 桶（同类至多一篇），`schema_drift` / 启动日志负责让操作者看见。
>
> **不支持无限兼容**：不为任意 BSON 形态引入类型特判、不做 repair-on-read、不把
> `updated_at` 解析成时间（排序是字符串序，不做格式猜测）。

---

## 15. Ordering / Fallback Policy

- **禁止**：`now()` / 当前进程时间 / 首次读取时间 / 随机值 / `_id` ObjectId 时间戳 /
  `created_at`（该字段根本不存在）。
- **采用**：Sentinel `""`（排序最小）+ **桶内 `session_id ASC`**——全部来自**已持久化的事实**。
- **可恢复性**：如果将来某篇 legacy 文档被显式修好了 `updated_at`，它的排序位置会从
  "末尾的 legacy 桶"变成"有序桶中的正确位置"——这是**顺带发生**的（读时分类，不写回），
  不需要额外动作，也不会让其它记录漂移。
- **不允许顺序漂移**：同一稳定数据集两次遍历顺序逐项一致（单测 + 真库用例）。

---

## 16. Fix（before → after）

**`core/storage/session_repo.py`**

| 项 | before | after |
|---|---|---|
| 排序域 | 原始 `updated_at` 单域，缺失/null/非字符串无定义 | `ORDERED_TS_FILTER` ⊕ `LEGACY_TS_FILTER` 两桶，覆盖集合中每一篇文档 |
| legacy 桶顺序 | （无） | `session_id ASC`（确定性、不依赖观察时刻） |
| `list_page` | 1 次 `find` | ≤2 次 `find`（legacy 只在有序列取尽时查；至多 O(limit)） |
| `list_all`（debug） | `find({}, sort=updated_at DESC, limit=200)`（Date 型会占名额） | 同一套两桶 + `session_id` tie-break |
| 写路径 | 裸 `update_one(upsert=True)` | `_upsert_session`：`non_fatal` + 经确认的 `session_id` 冲突收敛一次 |

**`core/memory/session_manager.py`**：`_decode_cursor` 接受 Sentinel `""`（对称化）。

**为什么 deterministic**：legacy 桶的键是 **Sentinel 常量 + 持久化 `session_id`**，
与"何时读、读几次、进程时间"完全无关；`has_more`/`cursor` 的语义与有序桶同构。

---

## 17. Migration / Legacy Handling（KB-15）

**不做 migration**：legacy 文档**原地**变成可达（读时按判据分类，不改一行数据、不写回）。
理由：① 修复不需要新字段/新集合；② 写回 timestamp 就是伪造事实（§19/§20 明令禁止）；
③ 唯一"需要迁移"的形态（`updated_at` 为 ISO 的旧格式）属 §49 登记的 KB-58，与本 finding 无关。

---

## 18. Pagination Completeness Proof

**命题**：对任意稳定数据集 D（每篇文档都有 `session_id`），按上面的两桶查询 + 游标语义，
有限次翻页枚举出的 ID 序列 = D，且每个恰好一次。

**论证**：

1. 定义有效键 `k(d) = d.updated_at if isinstance(str) and d.updated_at != "" else ""`，
   与投影层 `_summary_from_doc` 的输出**逐字相同**。
2. `ORDERED_TS_FILTER` ⟺ `k(d) != ""`，`LEGACY_TS_FILTER` ⟺ `k(d) == ""`（真库实测二者互补且
   覆盖缺失字段）⇒ 两桶**划分** D。
3. 有序桶内按 `(updated_at DESC, session_id ASC)` 排序 ⟺ 服务层 `_precedes` 在 `k != ""` 上的序；
   legacy 桶按 `session_id ASC` ⟺ `_precedes` 在 `k == ""` 上的序（`_precedes` 先比 `updated_at`，
   相等再比 `session_id`）。
4. 所有有序键 > `""` ⇒ 有序桶整体先于 legacy 桶 ⇒ 拼接序 == `_precedes` 全序。
5. 游标谓词就是该全序上的"严格大于当前位置"：有序桶 `(k' < k) OR (k'=k AND s' > s)`，
   legacy 桶 `s' > s`（因为桶内所有 `k'` 都等于 Sentinel）。
6. ⇒ 每一页返回的是"全序中游标之后的前 `limit` 个"，序列严格推进、无重复；数据集有限 ⇒ 终止。

**经验证据**：单位/服务/真库三层对 `N ∈ {0,1,49,50,51,205,500}` × 混合元数据做完整遍历，
断言 `set(ids) == expected`、`len(ids) == len(set(ids))`、两次遍历顺序一致（§36）。

---

## 19. Search / Summary / Direct Lookup Impact

| 路径 | 修前 | 修后 |
|---|---|---|
| search（title 子串） | 命中数超过一页时 legacy 文档不可达 | 两桶都带 `search` 子句 ⇒ 跨页可达（`test_search_reaches_legacy_rows` 等 3 处） |
| `get_summary` | 单文档直读，不受排序域影响 | 不变（真库回归 `test_direct_lookup_still_works_for_legacy_rows`） |
| `load` | 同上 | 不变 |
| 空搜索 | == 普通列表 | 不变 |
| 超长搜索词 | 按"无匹配"处理 | 不变 |
| `list_all`（debug，≤200） | Date 型占名额、legacy 排最后但不保证 | 同一套桶 + tie-break，legacy 仍在窗口内（窗口满时按序截断） |

---

## 20. KB-16 — Pre-Fix Reproduction（真库 E3）

脚本：`tmp/b16/main/probe_kb16_e3.py`（独立库）。

| 实验 | 结果（修前 HEAD） |
|---|---|
| D1 顺序两次 `create(同 ID)` | 1 篇（upsert 覆盖：**顺序重复不产生重复文档**，但会重置该篇字段） |
| D2/D3 20 轮 × **2 个独立 client** 并发 `create(同 ID)` | **1–3 轮出现重复文档**（两次运行分别 3/20、1/20） |
| D3 同上，并发 `ensure_exists`（find→`$setOnInsert` upsert） | **1–2 轮出现重复** |
| 索引升级：非唯一 `session_id_1` + 请求 `unique=True`（同名） | `OperationFailure` **code 86** `IndexKeySpecsConflict` |
| 同键 + 不同 name + `unique=True` | 成功（Mongo 8 允许同键不同选项并存）——本批**不采用**（会留两个同键索引） |
| 已有重复 + drop 旧索引后 build unique | `DuplicateKeyError` **code 11000**，且集合退化成**只剩 `_id_`** |
| 唯一索引存在时的 20 并发（create/ensure_exists/save/load+create） | **恒 1 篇、0 异常**（服务端把 loser 的插入重试为 update） |

**确定性复现**（本批采用为 PRE_FIX_RED 主证据）：把两个 repo 的存在性 `find` 卡在同一个会合点，
让二者都通过"不存在"检查，随后各自 upsert —— 修前 `count == 2`（真库断言失败信息：
`find→upsert 窗口产出了 2 篇同 ID 文档`），修后 `count == 1`。
用例：`tests/real_mongo/test_real_session_identity_truth.py::test_find_then_upsert_window_cannot_produce_two_authorities`。

---

## 21. Existing Index State

| 索引 | 键 | 选项 | 引入 |
|---|---|---|---|
| `_id_` | `_id: 1` | unique | Mongo 内建 |
| `session_id_1` | `session_id: 1` | **修前：非唯一**；修后：**unique** | 2026-07-16 起声明 |
| `updated_at_-1` | `updated_at: -1` | — | 早期 |
| `updated_at_-1_session_id_1` | `updated_at: -1, session_id: 1` | — | 批次 8 / P2-01r（keyset 排序） |

真库实况与声明**逐项一致**（无孤儿、无缺失）；`EXPECTED_INDEX_OPTIONS` 修前**不含 `sessions`**
⇒ 运行期对 sessions 只比名字、一个选项都不比。

---

## 22. Duplicate Reachability（数据库缺口 vs 产品可达）

| 形态 | 是否可达 | 证据 |
|---|---|---|
| 进程内两条不同 `SessionMemory` 写同一 ID | 否（`SessionManager` 按 ID 缓存单实例） | `session_manager.get/ensure_loaded` 按 sid 取缓存 |
| **两个独立 client / 两个进程并发 upsert 同一 ID** | **是** | D2/D3 实测 + 确定性会合点复现 |
| clean-start 的 `ensure_exists` 并发 | **是** | D3 实测 |
| 手工/外部工具裸 `insert_one` | 是（但那是运维面，不是产品面） | 直读暴露 |

⇒ 既不是"只有裸 collection 能造"（**产品写路径可达**），也不是"必然发生"（窗口窄）；
定性为 **DEPLOYMENT/MEDIUM**。

---

## 23. Duplicate Consequence Matrix

| 操作 | 有重复文档时的行为 |
|---|---|
| `load` / `get_summary` | `find(…, limit=1)` ⇒ **任意**命中一篇（Mongo 不作顺序保证）⇒ 权威不确定 |
| `rename` | `update_one` ⇒ **只改一篇** ⇒ 两份永久分叉 |
| `save` | `update_one(upsert=True)` ⇒ 只改一篇 ⇒ 消息窗口分叉 |
| `delete` | `delete_many` ⇒ 两份**一起删**（把两份当同一身份） |
| `list_page`（仓储层） | 两份**都返回**（同 ID 两条） |
| `SessionManager.list` | `_merged_page` 去重 + warning（Batch 8 的兜底） |
| `summaries_by_ids` | 按 session_id 建字典 ⇒ **后者覆盖前者**（静默） |
| `summary` / 归档关联 | 归档按 session_id 关联 ⇒ 两条记录的归档**混在一起**（无法区分） |
| 级联删除 | 按 session_id ⇒ 一起删 |

**结论**：重复 = **non-deterministic authority**（读任选、写只命中一份、删全删）。

---

## 24. Current Data Duplicate Audit

真库：**duplicate key 0 组、涉及文档 0 篇**（§7）。独立测试库里的重复均为本批构造。

---

## 25. Deployment / Legacy Policy（选定 Policy B）

| Policy | 采用？ |
|---|---|
| A：无重复 ⇒ 直接加约束 | **是**（这是正常路径，自动完成） |
| **B：可能有重复，但应用必须启动** | **是（本批策略）**：preflight 重复探测 → 有重复则保持非唯一 + **显著告警** + `schema_drift` 报 `option_conflicts`（error 级），应用照常启动 |
| C：重复即 corruption ⇒ fail-closed 拒绝启动 | 否（证据不支持：单用户应用、重复不破坏安全边界、且拒绝启动会把数据问题升级成可用性问题） |

**启动可用性（§42）**：真正危险的形态是"`ensure_index(unique=True)` 直接写在 `connect()` 里" ——
在**已有非唯一索引**的库上它必然抛 code 86，被 `connect()` 吞成 `_db=None` ⇒ 整个 sessions 域
不可用。本批的升级路径**先探测、只删自己那一条非唯一索引、失败即退回**，并用单测把
"有重复 ⇒ 不 drop、不建唯一、`connect()` 仍 True"钉死。

---

## 26. Unique Constraint Design

- **形态**：单键 `{session_id: 1}`，`unique=True`，规范名 `session_id_1`（与既有声明同名，
  避免留下两个同键索引）。
- **不加 partial/sparse**：`session_id` 是 REQUIRED 字段（§3），为"建索引方便"而降低身份
  契约是反的（§43）；缺 `session_id` 的文档会被索引成 null 值 ⇒ **同类至多一篇**，
  这正是"无身份 = corruption"的期望形态。
- **同键不同名 + unique 并存**：Mongo 8 允许（实测），但会造成两个同键索引（写放大 +
  schema_drift 孤儿告警）⇒ **不采用**。
- **现状识别按"键模式"而不是名字**（挑战者 B D2）：操作者可能手工建过非规范名的同键索引
  （`session_id_unique`）——只认规范名会对它视而不见、转而去建第二个同键索引（真库 code 85）
  再退回非唯一，留下双索引 + 每次 `connect()` 注定重试失败。按 `key == {"session_id": 1}`
  识别后：任意名字的唯一同键索引都算"已强制"（并告警提示规范名与 schema_drift 的处置），
  非唯一的同键索引则按其真实名字 drop 后升级。
- **升级 = drop + 重建**：Mongo 不能原地改选项（实测 code 86），故 `drop_index` → `ensure_index`。
  这是 `drop_index` 在生产代码里的**第一个**调用点，且只在"数据干净 + 选项不同"时发生。

---

## 27. DuplicateKey Semantics（§58/§59/§60）

| 问题 | 答复（按当前 create/save/ensure_exists 语义） |
|---|---|
| 同 ID 重复 create 是幂等还是冲突？ | **upsert 语义**（"该身份存在"即满足后置条件）；顺序重复 create 会覆盖该篇字段——**既有语义，本批不改**（不可由产品路径触发：ID 由服务端 uuid 生成） |
| 唯一索引下的并发冲突怎么处理？ | 服务端把 loser 的插入**重试为 update**（真库实测：20 并发 × 4 路径 = 0 异常、恒 1 篇）；应用层再兜一次"确认是本集合 `session_id` 约束 ⇒ 重跑一次 upsert"（第二次不可能再冲突：过滤键就是唯一键且 `$set` 不改它） |
| DuplicateKeyError 会不会泄到用户层？ | 不会：`_upsert_session` 收敛；`non_fatal=is_duplicate_key` 保证不 `_mark_unavailable()`（否则一次正常并发会变成全站 503 直到下一轮健康重连）。**非** `session_id` 约束的 E11000 **原样冒泡**（§60：不盲目吞）——由 `_is_session_identity_conflict` 校验 `details.keyPattern`，拿不到上下文一律不认 |
| 非 upsert 的写路径呢？ | `rename` 也是写路径：挑战者 B（D1）实测"无 `non_fatal` ⇒ 外部唯一冲突把整个 sessions 域判死，而 `SessionManager.rename` 又把它吞成改名成功"。⚠ 已补 `non_fatal=is_duplicate_key`（冲突仍原样冒泡，不猜不吞） |
| 有测试吗？ | `test_session_id_conflict_converges_and_keeps_repository_alive`、`test_foreign_unique_conflict_is_not_swallowed`、`test_rename_survives_a_foreign_unique_conflict`、真库 `test_unique_collision_does_not_kill_the_repository`；变异 M6/M7/M10 |

---

## 28. Index Bootstrap / Idempotence

| 场景 | 行为 | 测试 |
|---|---|---|
| 全新库 | 直接建唯一索引 | `test_clean_database_creates_unique_index`、真库 `test_connect_creates_the_unique_index` |
| 非规范名同键索引（unique） | 认作"已强制"，零变更 + 告警 | `test_non_canonical_unique_index_is_recognized`（变异 M11） |
| 非规范名同键索引（非唯一） | 按其**真实名字** drop 后升级 | `test_non_canonical_non_unique_index_is_upgraded` |
| `list_indexes` **抛异常**（后端违约） | 按"状态未知"处理：零变更、`connect()` 仍 True | `test_list_indexes_raising_does_not_break_connect` |
| 已唯一（重复 `connect()`） | **不跑昂贵的重复探测**（真库实测该探测是全集合 `$group`：20k→71ms、100k→351ms，健康重连每轮都会 `connect()`） | `test_already_unique_skips_the_duplicate_probe`（变异 M12） |
| 旧部署（非唯一 `session_id_1`，数据干净） | drop → 建唯一 | `test_non_unique_index_is_upgraded_by_drop_then_create`、真库 `test_existing_non_unique_index_is_upgraded_in_place` |
| 已唯一 | **零变更**（重复 `connect()` 幂等） | `test_already_unique_is_idempotent`、真库同一测试内两次 `connect()` |
| 有重复 | 保持现状 + 告警；`connect()` 仍 True | `test_duplicates_keep_availability_and_skip_unique_build`、真库 + `schema_drift` |
| `list_indexes()` 失败 | 零变更（状态未知不执行破坏性操作） | `test_index_listing_failure_skips_all_changes` |
| 重复探测失败 | 按"可能有重复" ⇒ 零变更 | `test_duplicate_probe_failure_counts_as_possible_duplicates` |
| 唯一建失败 | 退回非唯一 + error 日志；`connect()` 仍 True | `test_unique_build_failure_falls_back_to_non_unique` |

**启动顺序**：`SessionRepository.connect()`（`_start_persistence` 内）→ 同一进程内的启动步骤
`db_health` / `character_integrity` / **`schema_drift`**（只读比对，不自动删/建）。本批**没有**
新增启动步骤、没有改 preflight（`--check` 仍 network-free）。

---

## 29. KB-16 Fix（before → after）

| 项 | before | after |
|---|---|---|
| 索引引导 | `ensure_index([("session_id",1)])`（非唯一，且改不了） | `_ensure_session_id_index()`：探测 → 比选项 → 升级/降级（不抛） |
| 写路径 | 裸 upsert | `_upsert_session`（`non_fatal` + 身份冲突收敛） |
| 结构性守卫 | `EXPECTED_INDEX_OPTIONS` 无 `sessions` | 登记 `unique: True` + 双向守卫（登记存在 & 代码仍声明 `unique=True`） |
| 运行时可见性 | 无 | 有重复 ⇒ 启动告警 + `schema_drift` `option_conflicts`（error） |

---

## 30. Migration Requirements

**无 migration**。唯一索引由 `connect()` 幂等引导；脏数据情形下**不删不改**任何文档，
只做显式降级与告警。运维动作（若库里真有重复）：人工确认并清理重复文档后**重启**，
下次 `connect()` 会自动升级为唯一索引。备份建议：清理前自行备份 `sessions` 集合
（本批不提供 CLI，不自动删除）。

---

## 31. Interaction Between KB-15 and KB-16（是不是同一 root）

**不是同一 root**（§50 的默认判断成立，且本批用证据确认）：

- KB-15 = **排序域/可达性**（查询语义缺口；修的是"读"），修前修后**索引没有任何变化**；
- KB-16 = **身份唯一性**（约束缺口；修的是"写"与"schema"），与排序键无关。

证据：把 KB-15 的修复单独应用在 HEAD 上，KB-16 的所有症状仍在（真库并发仍造重复）；
反之把 KB-16 的唯一索引单独加上，KB-15 的漏项/毒化也仍在（重复文档与 `updated_at` 无关）。
两条各自有独立的 PRE_FIX_RED、独立的不变量、独立的变异击杀 ⇒ 分两个 production commit（§43）。

**唯一的交叉点**：`SessionSummary.updated_at` 的投影与 `list_page` 的桶判据必须由**同一条规则**
导出（`k(d)`），这一点写在 invariants SD1 的"实现形状"里。

---

## 32. Regression Tests

| 类别 | 文件 | 数量 | 说明 |
|---|---|---|---|
| **PRE_FIX_RED（KB-15）** | `core/storage/tests/test_session_repo_pagination.py::TestLegacyOrderingBucket` + `::TestArrayValuedOrderingMetadata` | 18 | 修前 14 红（含 search 跨页、Date/数字型、**数组型**、legacy-only、页边界游标、`list_all`） |
| **PRE_FIX_RED（KB-15）** | `core/memory/tests/test_session_manager_pagination.py::TestAdversarialRegressions`（新增 4 例，其中 1 例由原 xfail 改写） | 4 | 修前 4 红（含游标毒化 400） |
| **PRE_FIX_RED（KB-15）** | `core/input/tests/test_sessions_pagination.py::TestLegacyOrderingRowsAreReachable` | 4 | 修前 3 红（route 层 = 用户入口） |
| **PRE_FIX_RED（KB-16）** | `tests/real_mongo/test_real_session_identity_truth.py` | 9 | 修前 4 红（无唯一索引 / 无升级 / 无降级告警 / **确定性 2 篇同 ID**） |
| **PRE_FIX_RED（KB-15，真库）** | `tests/real_mongo/test_real_session_ordering_truth.py` | 10 | 修前 **7 红**（不可达 / Date 型不排最后 / 页边界截断 / search 漏 / manager 端到端 / 数组型 2 例） |
| **ALREADY_GREEN_GUARD** | 上述文件中的"健康数据顺序不变""一页装得下""直读不受影响""非身份约束冲突不吞"等 | 18 | 修前即绿——钉住"修的过程中不许改坏" |
| **PRODUCT_CONTRACT_GUARD** | `core/input/tests/test_session_routes.py`（既有 112 例）、`core/storage/tests/test_fail_closed_convention.py`、`core/storage/tests/test_schema_drift.py`（39 例） | — | 路由契约、fail-closed 约定表、索引漂移表（本批同步登记 `sessions` 唯一选项） |
| **FALSIFICATION_GUARD** | `core/storage/tests/test_fake_documentdb_semantics.py::TestQueryOperatorFidelity`（3 例）+ `::TestUniqueIndexBuildFidelity`（3 例）+ fake docstring 的保真度缺口清单 | 6 | 证明新查询在 fake 上**真的被执行**（`$not` / `$type`（含数组遍历）/ `$nor` / 类型括号），且"有重复 ⇒ unique build 失败"这条分支在 fake 上真实可达——否则 fake 会把未知算子当 match-all、把降级分支伪装成成功（假绿） |
| **结构性守卫** | `core/storage/tests/test_schema_drift.py::TestSessionIdentityOptionIsRegistered`（2 例） | 2 | R3/R5 对"代码侧被悄悄弱化"是盲的，本守卫双向钉住 |

**PRE_FIX_RED 汇总（真实执行，非推断；`tmp/b16/main/` 下的日志）**：在 `HEAD + 新测试` 的
worktree（`/tmp/b16-prefix`）上 **24 failed / 74 passed**（unit+service 三层：
`pre_fix_red_unit.txt`），真库层 **11 failed / 8 passed**（`pre_fix_red_real_mongo.txt`）。
红的原因全部是**行为**（omission / 顺序错 / 游标拒绝 / 无唯一约束 / 造出重复），
**没有一条**是"新 helper/新常量不存在"（§101）。

---

## 33. Challenger Findings

两个独立挑战者（各写自己的探针、跑真库、不碰生产代码/既有测试）：**A = KB-15（BSON/keyset
语义、边界、假绿）**、**B = KB-16（索引/并发/部署/降级）**。完整报告：
`tmp/b16/challenger/ch_a_report.md`、`tmp/b16/challenger/ch_b_report.md`（`tmp/` 不入库）。

### 33.1 挑战者 A —— **confirmed defect（修复不完整）+ fake-green**（已修）

| # | 发现 | 分类 | 处置 |
|---|---|---|---|
| A1 | **数组型 `updated_at`**（如 `["1990-…","2040-…"]`）：Mongo 的比较/`$type` 算子**遍历数组元素** ⇒ 该文档既匹配 `{"$gt": ""}`（按**最大元素**排序到最前）、又被投影成 Sentinel `""` ⇒ 游标里的 `""` 被误读成"已在 legacy 桶" ⇒ **整个有序桶被跳过**：`limit=1` 只拿到该文档、`limit=2` 重复、route 端 200 + `has_more=false` 撒谎（修前是**响亮**的 400，本批中间态退化成**静默**漏项——比修前更隐蔽） | **confirmed（修复不完整）** | `ORDERED_TS_FILTER` 收紧为"非空字符串**标量**"（`+ $type: "string" + $not: {$type: "array"}`），`LEGACY_TS_CLAUSE` 改为**真补集**（`$nor`）。真库实测两桶并集 == 全集且不相交；`limit ∈ {1,2,3,50}` 遍历全部不重不漏 |
| A2 | **fake-green**：fake 的 `_comparable()` 只建模"跨类型不匹配"，**不建模数组遍历** ⇒ 它把数组判进 legacy 桶、分页完全正确 ⇒ 所有基于 fake 的 legacy 用例（以及真库层只种 Date/int 的用例）对该形态全盲 | **fake-green** | fake 补 `$type`（**含数组元素遍历**）与 `$nor`；真库层与存储层的 legacy 形态**都加入数组型**（`seed_legacy` / `_legacy_docs`） |
| A3 | 次要 fake 缺口：混类型 `session_id` 排序时 fake 抛 `TypeError`（真库返回 `[123,'abc']`）；`$exists` 对显式 null 与真库相反 | 已登记 | 并入 **KB-60**（fake 保真度缺口清单） |
| A4 | 未发现：missing/null/empty/int/bool/Date 的任何遗漏/重复/不终止；全部构造游标 → 200 或 400（**无 500**、无死循环）；`N ∈ {0,1,49,50,51,205,500}` × `limit ∈ {50,100}` + search + limit 边界 + 离线降级全部干净 | no finding | 这些正是本批的 ALREADY_GREEN 面，挑战者独立复验 |

**挑战者 A 的复现在修后逐条转绿**（其原始测试文件因符号改名不可直接收集，本批把
`LEGACY_TS_FILTER` → `LEGACY_TS_CLAUSE` 后原样跑：`3 passed`；其最小复现数据集已
固化进 `TestArrayValuedOrderingMetadata`（fake）与
`TestArrayValuedOrderingMetadataRealMongo`（真库））。

### 33.2 挑战者 B —— **3 个缺口（1 中 / 2 低）+ 1 项规模问题 + 1 条未设防假绿通道**（已修）

| # | 发现 | 分类 | 处置 |
|---|---|---|---|
| B1 | `rename` 没走 `non_fatal` ⇒ **任何**唯一约束冲突都 `_mark_unavailable()` 判死整个 sessions 域，而 `SessionManager.rename` 又把它吞成"改名成功"（用户 200、随后全站 503）。三条 upsert 路径装了护栏，这条漏了 | **confirmed（中；当前不可达——需 sessions 上再有第二个唯一索引）** | `rename` 的 `_call` 补 `non_fatal=is_duplicate_key`（冲突仍原样冒泡，不猜不吞）+ 守卫用例 + 变异 M10 |
| B2 | 同键唯一索引在**非规范名**（`session_id_unique`）下无法识别 ⇒ 建 `session_id_1` unique 抛 code 85 ⇒ 退回非唯一 ⇒ 同键双索引 + 每次 `connect()` 注定重试失败 + `schema_drift` 永久 option_conflict | **confirmed（低-中）** | 改为**按键模式**识别现状（`_same_key_session_id_index`），任意名字都能识别/升级；非规范名时显著告警 + 守卫用例 + 变异 M11 |
| B3 | `_ensure_session_id_index` 里 `list_indexes` 未包 try，与方法 docstring"本方法不抛"冲突（当前被出厂后端的吞异常行为掩盖；包装/新后端违约即全站 503） | **confirmed（低）** | 包 try（按"状态未知"处理）+ 守卫用例 |
| B4 | 重复探测在幂等 early-return **之前**：每次 `connect()`（含每轮健康重连）跑全集合 `$group` COLLSCAN（真库实测 20k→71ms、100k→351ms，已唯一仍付 346ms） | **fix weakness（规模）** | 幂等判定（`list_indexes` + 按键模式）**前移**到探测之前；已唯一时零探测 + 守卫用例（断言 `aggregate` 未被调用）+ 变异 M12 |
| B5 | 降级告警的三条"影响"与实测不符（删除其实是 `delete_many` 全删；`repo.list_page`/`list_all` **不去重**，只有 `SessionManager.list` 去重 ⇒ debug 面板会出现两条同 ID；未提"直读与列表可能指向不同文档"） | **confirmed（低，文案）** | 告警文案逐条按实测重写 |
| B6 | **未设防的假绿通道**：`FakeDocumentDBBackend` 结构上表达不了"库里有重复"（`aggregate` 恒 `[]`、`ensure_index` 不校验既有文档）⇒ 用 fake 验证"有重复 ⇒ 降级"会得到假绿 | **fix weakness（当前无假绿）** | fake 的 `ensure_index(unique=True)` 补**既有文档校验**（冲突抛 `DuplicateKeyError`，对齐真库 build 期行为；同名不同选项也抛）⇒ 该分支在 fake 上真实可达 + 两条守卫用例 |
| B7 | 挑战者 B 明确"证法弱于结论"的一处：本批真库并发用例用的是**注入 db** 的 repo（注入路径不调 `_mark_unavailable`）⇒ "冲突不判死仓储"那条断言的证明力弱 | **accepted（已用独立证法补）** | B 用非注入 repo + 打点重做，结论仍成立；本批的 `non_fatal` 语义另有单测（`test_session_id_conflict_converges_and_keeps_repository_alive` 断言 `repo.backend is not None`） |
| B8 | 未发现：A1–A5/A7 状态矩阵（零数据删除、无同键残留、二次 `connect()` 幂等）、B（不删不合并 + 自检报出 + 清干净后重启自动升级）、C（20 并发 × 5 模式 × 5 轮 + 不同键 + 并发升级竞争：恒 1 篇 / 0 异常 / 0 判死）、D（外部唯一约束原样裸抛）、E（顺序重复 `create` 清空 `messages` 是**修前逐字节相同**的既有行为，产品路径不可达）、G（无第二写者、级联无损）、H（干净库无漂移、重复库报 conflict、启动步骤严格只读） | no finding | 独立复验 |

**未验证项（挑战者自陈，如实登记）**：完整 `Agent.start()`（改为直调启动步骤本体 + 静态核对
step 顺序）；副本集/分片拓扑；生产库（27017）上 A1/B1–B3 的真实可达性；数组型 `updated_at`
在真实用户库中是否存在（只在测试库构造过）；`rename` 重复态的前端表现。

### 33.3 分类小结

| 分类 | 条目 |
|---|---|
| **confirmed（已修）** | A1（数组型假划分）、B1（`rename` 无护栏）、B2（非规范名）、B3（`list_indexes` 未包 try）、B5（文案） |
| **fix weakness（已补守卫）** | B4（探测代价前移）、B6（fake 唯一索引 build 校验）、A2（fake `$type`/`$nor`） |
| **fake-green（已消除）** | A2（数组形态）、B6（重复分支在 fake 上不可达） |
| **accepted** | B7（注入 db 的证明力；已用独立证法补强）；B8 未验证项清单 |
| **reclassified** | 无（两条 canonical finding 的定性未被推翻） |
| **new out-of-scope** | A3（混类型排序 / `$exists` 保真度）→ 并入 KB-60 |


---

## 34. Mutation Evidence

脚本 `tmp/b16/main/mutate.py`（独立 worktree `/tmp/b16-mut`，逐项打补丁 → 跑定向用例 → 还原）。

| 变异 | 内容 | 结果 |
|---|---|---|
| M1 | legacy 桶判据失效（两桶同判据） | ✅ 红（25 failed） |
| M1b | 有序桶退回旧判据（不排除数组）——挑战者 A 的回归点 | ✅ 红（数组用例） |
| M2 | Sentinel fallback 改成 `now()` | ✅ 红（顺序断言失败） |
| M3 | 去掉 legacy 桶的 `session_id` tie-break | ✅ 红（10 failed） |
| M4 | 游标解码退回拒绝 Sentinel | ✅ 红（KB-15 游标组 2 failed） |
| M5 | `connect()` 不再保障 `session_id` 唯一索引 | ✅ 红（8 failed，含真库确定性 2 篇） |
| M6 | 写路径不做冲突收敛（退回裸抛） | ✅ 红 |
| M7 | 盲目吞掉所有 `DuplicateKeyError` | ✅ 红（非身份约束那例） |
| M8 | 有重复时静默降级（去掉显著告警） | ✅ 红（caplog + 真库） |
| M9 | `schema_drift` 不再登记 `sessions` 唯一选项 | ✅ 红（假绿守卫） |
| M10 | `rename` 去掉 `non_fatal`（挑战者 B D1） | ✅ 红 |
| M11 | 索引识别退回按名字（挑战者 B D2） | ✅ 红（2 failed） |
| M12 | 重复探测挪回幂等判定之前（挑战者 B D4） | ✅ 红 |

**漏网变异：无**（12 个变异全部被杀；完整输出 `tmp/b16/main/mutation_transcript.txt`）。
其中 M1b/M10/M11/M12 是**挑战者反证驱动的回归变异**（数组判据、`rename` 护栏、按键模式识别、
探测代价前移）——没有它们，本轮挑战者发现的 4 个缺口都不会被守卫覆盖。

---

## 35. Real Mongo Evidence（本批的**核心**证据层）

- 版本：`docker compose -f docker-compose.test.yml`（`mongo:8`，`127.0.0.1:27018`），
  与 CI `real-mongo` job 同源；每个用例独立测试库、进入/退出各 drop 一次。
- 新增测试：`tests/real_mongo/test_real_session_ordering_truth.py`（KB-15，10 例）
  + `tests/real_mongo/test_real_session_identity_truth.py`（KB-16，9 例）
  + 共享 helper `tests/real_mongo/_session_data_support.py`。
- 真库证明的语义（fake 结构性给不出）：
  ① BSON 排序序与类型括号；② `$gt: ""` / `$not: {$gt: ""}` 的精确划分（含缺失字段）；
  ③ 两桶查询的 `explain`（IXSCAN + 索引序、legacy 桶精确 indexBounds）；
  ④ upsert 并发：修前可造重复（含确定性会合点）、修后恒 1 篇；
  ⑤ 索引升级路径（非唯一 → drop+重建 → 唯一）与降级路径（有重复 ⇒ 保持非唯一 + `schema_drift`
  报 `option_conflicts`）；⑥ 已有重复时 build unique 的真实异常码（11000）与
  "先 drop 再失败会只剩 `_id_`"这一危险形态。
- 本地运行：`make test-real-mongo`（**46 例全绿**，覆盖既有 `tests/real_mongo` 全部文件）。

---

## 36. Dataset Traversal Evidence

| N（有序列） | 混合 legacy | 页大小 | 断言 | 结果 |
|---|---|---|---|---|
| 0 / 1 / 49 / 50 / 51 / 205 | +3（missing/null/empty） | 50 | 全集可达、无重复、legacy 排最后且桶内 `session_id ASC` | ✅（存储层参数化 6 组） |
| 0 / 1 / 50 / 51 / 199 / 200 / 201 / 225 | — | 50 | 既有 Batch 8 完整性（健康数据） | ✅ 不变 |
| 205 | +3 | 50 | route 层（`GET /api/sessions`）完整翻页 | ✅ |
| 205 | +1 Date 型 | 50 | 真库完整翻页 | ✅ |
| 80（时间戳大量重复） | +3 | 13 | 真库两次遍历顺序逐项一致 | ✅ |
| 1000 | +3 | 50 | 真库遍历 1003 条 / 22 页，`unique == 1003` | ✅ |

---

## 37. Index Structural Guard

- `EXPECTED_INDEX_OPTIONS` 新增 `"sessions": {"session_id_1": {"unique": True}}`
  → Batch 3.6 的 R1/R2/R3/R5 全部通过（代码确实静态声明了该选项）。
- **补一条盲区守卫**（`TestSessionIdentityOptionIsRegistered`）：R3/R5 只查"代码声明了选项却
  没登记"这一个方向；把**代码侧**弱化（删掉 `unique=True`）会让 R1/R2/R3/R5 **全绿**而约束
  实际消失 ⇒ 新增用例同时钉住"表里登记了" 与 "代码仍静态声明 `unique=True`"。
- `test_ttl_present_matches` 的 mock 改为**从声明派生**（原先手写一张"哪些索引 unique"的
  第二张表——本批新增一条唯一索引时它会假红，正是该文件 docstring 反复警告的"两张手写表
  共同漂移"）。

---

## 38. Public API Impact

**零变化**：`SessionListResult{sessions[], has_more, cursor}` 与 `SessionSummary` 字段逐个不变；
`PATCH/GET/DELETE /api/sessions/{id}`、`/api/sessions/{id}/messages|resources|generations|pending`
不变；错误码集合不变（畸形游标仍 400 `CONTRACT_VALIDATION`）。
cursor 的**格式**不变（`base64url("<key>|<session_id>")`），只是多接受一个此前会被拒的键值
（Sentinel `""`），且**只有存在 legacy 文档时**才可能产出。

---

## 39. Document Schema Impact

**零变化**：没有新增/删除/重命名字段，没有回填，没有 migration。legacy 文档保持原样。

---

## 40. Index Impact

**一条索引的选项升级**：`sessions.session_id_1` 由非唯一 → 唯一（键与名字不变，故
`EXPECTED_INDEXES` 不变、`total == 29` 不变）。升级由 `connect()` 幂等完成，
`drop_index` 只在"数据干净 + 选项不同"时执行一次。

---

## 41. Migration Impact

**无**（§17/§30）。运维面唯一的动作是"清理重复文档后重启"（若库里真有重复）。

---

## 42. Performance / Query Plan Impact

真库实测（1000 有序列 + 3 legacy，每页 50；`tmp/b16/main/probe_perf.py`）：

| 指标 | 修复后 | 修前 HEAD 谓词 |
|---|---|---|
| 首页 | **2.07 ms**（IXSCAN + 索引序） | — |
| 深页（第 16 页） | **1.85 ms** | 2.08 ms |
| 搜索（title `$regex`） | 3.32 ms | — |
| 完整遍历 1003 条 / 22 页 | **48.8 ms（2.22 ms/页）** | — |
| legacy 桶一次查询（0 命中，最坏情形） | 2.16 ms | — |

- 单页读量：`docsExamined == nReturned == limit`（§11）——**没有**回到"预取全量再切片"
  （PG2 仍成立，Batch 8 的结构性守卫 `test_page_read_never_fetches_more_than_the_page`
  与 `test_list_does_not_prefetch_the_whole_dataset` 均保持绿色）。
- 每请求至多 2 次 `find`（第二次只在有序列取尽时发生），仍是 O(limit)。

---

## 43. Commits

```text
fix(session): preserve pagination reachability and enforce session identity uniqueness
docs(architecture): record session data integrity invariants       # SD1–SD6
docs(review): record Batch 16 execution and challenger findings
docs(todo): close KB-15/KB-16 and register Batch 16 findings
```

**为什么两个修复合成一个 production commit**（与 §105 的"建议分两个"偏离，理由如下）：
两条修复落在**同一个模块**（`core/storage/session_repo.py`）且共享同一段索引引导区域；
按文件/hunk 拆成两个 commit 需要构造一个**未被测试过的中间态**（KB-15 的桶谓词与 KB-16 的
索引引导在同一文件里交错），而"未测过的中间态"正是本仓最反感的假保障形态。

代价与补偿：bisect 粒度变粗（一个 commit 覆盖两条 finding），但
① **测试文件是分开的**（KB-15/KB-16 各自的单测与真库文件互不重叠）；
② **PRE_FIX_RED 是分开测的**（§32：unit+service 24 红 / 真库 11 红，逐条归属到 finding）；
③ **变异是分开杀的**（§34：M1–M4/M1b 属 KB-15，M5–M12 属 KB-16）；
④ §31 的"不是同一 root"由**独立复现**证明（把 KB-15 的修复单独应用，KB-16 的症状仍在；
反之亦然——两次都由真库探针确认），而不是靠 commit 边界。
若后续需要严格 bisect 粒度，可在本条记录的证据索引下安全拆 commit（不改变任何行为）。

---

## 44. Worktree Verification

| worktree | 路径 | 用途 |
|---|---|---|
| pre-fix | `/tmp/b16-prefix`（HEAD + 本批新测试） | PRE_FIX_RED 真红证据（18 + 9 例） |
| fix | 工作树（本仓） | 生产补丁 + 全量回归 |
| mutation | `/tmp/b16-mut` | 9 个变异逐项注入并还原 |
| 真实库实验 | 独立容器 `elysia-mongo-test`（`mongo:8`，127.0.0.1:27018） | 所有破坏性索引实验只在此进行；用户真实库（27017）**只读**审计 |

---

## 45. Test Results

| 层 | 命令 | 结果 |
|---|---|---|
| targeted（存储/管理器/路由/真库） | `pytest core/storage/tests/test_session_repo_pagination.py …` | 38 + 33 + 25 + 9 + 39 + 22 收集；全绿 |
| 相关套件 | `pytest core/storage core/memory core/input core/agent -n 8` | **2262 passed** |
| quick | `pytest -m "unit or service" -n 8 --dist loadfile` | **6302 passed / 2 skipped / 1 xfailed**（80s） |
| normal | `pytest -m "not slow and not external" -n 8 --dist loadfile` | **6302 passed / 2 skipped / 1 xfailed**（81s） |
| full | `pytest -q -n 8 --dist loadfile` | **6334 passed / 58 skipped / 1 xfailed**（81s；skip = external/slow 未 opt-in） |
| real Mongo | `make test-real-mongo` | **46 passed**（本层全部文件；本批新增 19 例） |
| related suites | `pytest core/storage core/memory core/input core/agent -n 8` | **2262 passed** |
| randomized/property | 参数化 N ∈ {0,1,49,50,51,205} × 4 种 malformed（missing/null/empty/array）+ Date/数字型 + 真库 1000 条遍历 + 两次遍历一致性 | ✅（§36） |
| static gates | `make lint`（ruff 全绿）/ `make pyright`（**0 errors**）/ `make check-arch` / `check-types`（含 17 golden frames）/ `check-compose` / `check-env-lock` / `check-timeouts` / `check-contract` / `check-debug-auth` / `check-secrets` / `check-test-sets` / `check-ci-isolation` | **全部 PASS**（`tmp/b16/main/gate_*.log`） |
| frontend | `cd frontend && npm run build` | ✅ built（本批未改任何前端代码） |
| real CI | push 后核对 backend / real-mongo / frontend / static jobs | ⏳ **待 owner push**：本机没有 GitHub 凭据（`git push` 报 `could not read Username`），三个 commit 已在本地 `main` 就绪（`1fa25939` / `91abe43e` / `19e01078`） |

---

## 46. Invariant Mapping（SD1–SD6）

| ID | Invariant | 落点 |
|---|---|---|
| **SD1** | 每一条结构上被接受的持久会话都必须能被支持的分页语义走到（或显式归类为 corruption） | `docs/architecture/invariants.md` §4.9；测试见该节表 |
| **SD2** | 排序 fallback 必须确定性，不得依赖观察时刻 | 同上（`session_id` 桶内序 + 两次遍历一致） |
| **SD3** | 游标遍历全域、不重不漏、终止 | 同上（含页边界 Sentinel 往返） |
| **SD4** | 一个逻辑 `session_id` 至多一篇权威文档（存储层强制） | 同上（索引引导 + 真库并发） |
| **SD5** | 遗留身份腐化必须显式（不静默挑一篇当权威） | 同上（告警 + `option_conflicts`） |
| **SD6** | 约束部署向后安全，索引引导不得毁掉可用性 | 同上（降级 / 退回 / 不抛） |

---

## 47. Todo Update

- **KB-15 → GUARDED**（`Batch 16`；Reach `LEGACY_DATA_ONLY`、Sev LOW、Ev **E3(16)**）。
- **KB-16 → GUARDED**（`Batch 16`；Reach `INTERNAL`→`DEPLOYMENT`、Sev LOW→**MEDIUM**、Ev **E3(16)**）。
- **KB-17 → 保持 OPEN**（本批只做边界核对：title 长度不参与 session identity / pagination
  integrity，未改任何校验）。
- 新增登记：**KB-58**（历史 `updated_at` 字符串格式不统一 ⇒ 字典序不保时序；当前库无存量）、
  **KB-59**（rename 不写持久 `updated_at`，覆盖层抬升 ⇒ 重启回落；reported ≠ actual）、
  **KB-60**（fake 后端保真度缺口清单：`aggregate` stub、`$nin`/`$all`/`$size` 静默 match-all、
  `$exists` 对 null 与真库相反、`limit=0` 语义相反、混类型 `session_id` 排序抛 `TypeError`）。
  本批已补 `$not` / `$type`（含数组遍历）/ `$nor` / 唯一索引 build 校验。

---

## 48. Keep As-Is

- Batch 8 的 keyset 架构（谓词/排序/`limit` 全部下推存储）；
- 公开 pagination API（`sessions[] / has_more / cursor`，无 total）；
- 不做全量物化 / 不做预取（PG2）；
- `SessionService`/`SessionManager` 架构与覆盖层语义（改名抬升是**既有**语义，本批保留；
  其与持久值的不一致登记为 KB-59）；
- 不做通用 migration framework；
- 不改前端；
- **不自动删除/合并重复文档**；
- **不做 read-time current-time 修复**（禁止 `now()` fallback）。

---

## 49. New Findings（只登记）

| ID | 一句话 | 处置 |
|---|---|---|
| **KB-58** | `sessions.updated_at` 的历史字符串格式换过三次（naive → `+08:00` → UTC），而排序是字典序 ⇒ 跨格式不保时序；当前真实库 7/7 UTC（无存量） | `NEW_FINDING_OUT_OF_SCOPE`（LEGACY_DATA_ONLY / LOW） |
| **KB-59** | `rename()` 不更新持久 `updated_at`，覆盖层把本地有效键抬到 `now` ⇒ `GET /api/sessions` 的 `updatedAt` 可比库里任何值都新，**重启后回落** | `NEW_FINDING_OUT_OF_SCOPE`（INTERNAL / LOW，`TRUTHFULNESS`） |
| **KB-60** | 内存 fake 后端保真度缺口：`aggregate()` 恒 `[]`、`$type`/`$nin`/`$all`/`$size`/`$nor` 静默 match-all、`$exists` 对显式 null 与真库相反、`limit=0` 语义相反 | `NEW_FINDING_OUT_OF_SCOPE`（测试基建 / LOW；本批已补 `$not` + BSON 类型括号） |

---

## 50. Follow-up（canonical backlog 保持）

KB-17（title 长度不对称）、KB-29、KB-30（回合级事务）、KB-32/KB-33、KB-39–45、KB-48–57、
KB-18–21、KB-12（token 估算）、KB-13、AR-16（Ollama Provider Adapter）、OBS-6/7/8、KB-11、
GR-25、N-15，以及本批新增的 KB-58/59/60。
