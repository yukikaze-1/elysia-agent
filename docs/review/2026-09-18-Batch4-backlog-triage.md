# Batch 4 — Backlog Triage & Planning（2026-09-18）

> ⚠️ **Historical execution record.** 本文描述**执行当时**的系统与结论；后续批次可能推翻其中部分判断。
> 当前事实见 [docs 索引](../README.md) 的 Canonical References（架构 / 不变量 / 测试 / CI / 运维 / 契约）。
> 正文**按当时原样保留**——不改结论、不删失败轨迹、不重写 Challenger 反证（`docs/readme-conventions.md` Rule 3）。

> 文档导航：[项目文档](../../README.md) → [docs 索引](../README.md) → 本文
>
> **本任务只做审计 / 规划 / 整理文档**：不改 production code、不改 tests、不改 CI、
> 不修 bug、不新增 guard、不重构、不开始 Batch 4 实现。发现的明显问题只记录证据。
>
> 产物：重构后的 [`todo.md`](../../todo.md)（**backlog 的 current source of truth**）+ 本文（分析快照）。
> 重构前的 `todo.md` 全文见 `git show 69f2e5a9:todo.md`；逐条证据仍在 `docs/review/`。

---

## 1. Executive Summary

| 指标 | 数量 |
|---|---|
| **Active（canonical 表：114 行 / 去重并扣除指针后 106 条）** | **106**（4 行跨组重复引用 `N-1`/`N-3`/`N-10`/`N-11`；4 行是显式指针 `KB-5≡GR-19`、`AR-5⊂AR-4`、`FEAT-1≡F2`、`FEAT-2≡KB-1`） |
| ├ Correctness | **13**（9 条 USER/DEPLOYMENT 可达 + 4 条 INTERNAL 侧） |
| ├ Runtime / Observability | 19（含 4 条与 correctness 交叉引用；TRUTHFULNESS 现役 7 条 + 2 条待复核 `OBS-3`/`OBS-4`） |
| ├ Deployment / Config | 9 |
| ├ Persistence / Recovery | 4 |
| ├ Architecture Debt | 15 |
| ├ Guardrail / Test Debt | 24 |
| ├ **Product Decisions**（需 owner 拍板；其中 2 条已降级为形式确认） | **11** |
| └ **Future Features**（不与 bug 混排） | **19** |
| **Deferred / Currently unreachable / Keep-as-is** | **16** |
| **Closed / Superseded**（一行索引） | **31** |
| 重复登记合并 | **5 组**（+ Challenger 追补 4 组指针） |
| 状态修正（本批现读 HEAD / git + Challenger 复核后） | **3 条"已修未关闭" + 5 条"需重分类/改判"**（§6.1） |

**一句话**：剩余债务里**真正的用户可见 correctness 是 13 条**（其余是部署/观测/门禁/架构/功能），
最大的一簇是 **TRUTHFULNESS（reported ≠ actual，7 条现役 + 1 条待复核）**；
其中 `N-1`+`N-11`（身份/就绪真值）共享同一子系统与同一条因果链，是 **Batch 4 的推荐主题**（§9/§10）。

---

## 2. Method（Normalization）

### 2.1 统一结构

每个条目归一为：`ID | Title | Origin | Type | Status | Reachability | Severity | Evidence | Dependencies | Candidate Batch`
（`todo.md` §1 的表即该结构；本文只保留分析）。

### 2.2 去重规则（严格执行）

两个 TODO 只有在 **same root cause + same failure mode + same fix direction** 三者同时成立时才合并。
仅"都和 config 有关 / 都和 profile 有关"**不算**理由。据此：

| 判定 | 例子 |
|---|---|
| **合并** | `N-13 ≡ N-4`（同表、同守卫、同修法，只是两次登记） |
| **合并** | `B3-CB-5 残留 ≡ N-5`（同一条门禁范围问题；B3-CB-5 主体已 ALREADY_FIXED） |
| **合并** | `B3-CB-4 ≡ N-7`（保护属性已被守卫；只剩 docstring 措辞） |
| **合并** | `F5(1.6) ≡ F6(1.8 表)`（同一条 STT provider-test 不对称） |
| **不合并** | `B3-CB-6`（两份白名单语义分岔）与 `N-1`/`N-2`/`N-11` 虽同在 `core/profile/`：前者是**白名单集合语义**分歧，后者是**健康判定/异常吞没**，根因、失败模式、修法都不同 |
| **不合并** | `F2`（提醒持久化）与 `RB-1`（write-only 字段）：都涉及持久化，但一个是**缺功能**，一个是**缺证据** |
| **不合并** | `F6`（provider test 读 config）与 `N-10`（gemini 未实现）：同一"能力真值"族，但一个是**探测对象错**、一个是**能力表缺失**，修法不同（故同批不同 commit） |

### 2.3 状态口径（FIXED ≠ GUARDED，沿用 Batch 3.5/3.6 纪律）

- `FIXED` = 行为已修，**无**可靠回归保护；
- `GUARDED` = 行为已修 **且**有可变异验证的守卫（本项目要求守卫本身被 Challenger 攻击过）；
- 已 `GUARDED` 的项不再留在 Active（N-4/N-8 在 Batch 3.6 已闭环）。

### 2.4 Evidence 级别

`E3` 真实 runtime/integration 复现 ＞ `E2` 定向测试/子进程复现 ＞ `E1` 静态控制流证据 ＞ `E0` 仅假设。
本批**没有**重跑 integration（规划任务，不做 code guard / mutation）；新标注的 E1 均为**本批在 HEAD 上现读代码**得到。

### 2.5 输入

`todo.md`（重构前全文）、Batch 1.6/1.7/1.8/2/2.3/3/3.5/3.6 执行记录、
`docs/architecture/invariants.md`、`docs/architecture/dead-code-review-checklist.md`、
`docs/review/2026-09-12-code-review-ledger.md`、`CODE_REVIEW_REPORT.md` 收口节。
**没有重新做全仓 bug audit**：一切条目都来自上述文档；本批新增的只有"状态核实"与"分类/排序"。

---

## 3. ID canonicalization 与编号漂移

### 3.1 采用的规则

**canonical = `todo.md`（Batch 3.6 之后）的编号**；Batch 3.5 报告 §12 的编号作为 **alias**。
理由：`todo.md` 是唯一被后续批次（Batch 3.6 的 commit message、报告、AGUARDED 标记）引用的活编号；
而 Batch 3.5 报告的 `N-8` 与 todo 的 `N-8` **指向不同问题**（前者=ProjectPath 常量，后者=B3-CB-7 缺守卫），
若改以报告编号为准，会把"已 GUARDED"读成"仍 OPEN"——风险更大。
代价：用户/旧文档里的 `N-9`/`N-10`/`N-11`/`N-12` 现在都要 +1 读（见 `todo.md` §2 对照表）。

### 3.2 Alias 表

| 旧编号（来源） | canonical | 主题 | 现状 |
|---|---|---|---|
| `N-8`（Batch 3.5 §12） | **N-9** → 归入 **AR-9** | 多处 import 期 `ProjectPath...resolve()` 常量 | OPEN（**已证一个实例有害**，见 §6.1） |
| `N-9`（Batch 3.5 §12 / 用户口述） | **N-10** | gemini 预检假绿 | **OPEN**（Batch 4-B） |
| `N-10`（Batch 3.5 §12 / 用户口述） | **N-11** | `connect`/`_load_from_file` 吞异常 | **OPEN**（Batch 4-A） |
| `N-11`（Batch 3.5 §12） | **N-12** | 空串 pronouns GET 500 | CURRENTLY_UNREACHABLE |
| `N-12`（Batch 3.5 §12） | **N-14** | preflight 两条弱用例 | OPEN（GR-2） |
| `N-8`（**Batch 3.5 §17**，后并入 `todo.md`） | **N-8**(guard) | B3-CB-7 缺回归守卫 | **GUARDED**（已闭环） |
| `N-13`（**Batch 3.5 §17**） | ≡ **N-4** | 表级 AST 守卫缺失 | **GUARDED** |
| `F5`（Batch 1.6 记录） | ≡ **F6**（Batch 1.8 follow-up 表） | STT provider-test 不对称 | OPEN（Batch 4-B） |
| `F5`（Batch 1.8 follow-up 表） | **F5** | workflow model reporter | OPEN（Batch 4-B 可选） |

> ⚠️ **三处历史陷阱（Challenger 复核）**：
> ① Batch 3.5 报告**自身有两套 N 编号**——§12（N-8=ProjectPath…N-12=弱用例）与 §17（N-8=guard gap、N-13=表守卫）；
> 而且 §9 的交叉引用「见 §12 的 N-10..N-13」是**断链**（§12 无 N-13，且 §12 的 N-10..N-12 只有 3 条）。本文 §3.2 只映射 §12。
> ② guard-gap `N-8` 的**出生地是 Batch 3.5 §17**（Batch 3.6 报告只到 §12）——早期草稿曾把它记成 "Batch 3.6 §17"，本版已改正。
> ③ **`F` 命名空间比 `N` 更乱**：Batch 1.6 有 F5/F7/F8/F9，Batch 1.7 有 F5/F6/F7/F8，Batch 1.8 **一个文件里两份表**
> （攻击清单 F1..F9 与 follow-up F1..F18）——同一个 `F7`/`F8` 在 Batch 1.8 内就指两件事（`F7`：顺序/状态残留 vs `/debug tts_ok`；`F8`：新字段破坏消费方 vs prompt budget）。
> 本文与 `todo.md` 一律以 **Batch 1.8 follow-up 表**为准；引用旧号必须带来源批次。

---

## 4. Canonical Backlog 摘要（完整表见 `todo.md` §1）

### A. Correctness（13 条：9 条 USER/DEPLOYMENT 可达 + 4 条 INTERNAL 侧）

| ID | 标题 | Reach | Sev | Ev |
|---|---|---|---|---|
| **N-1** | `/ready` 假绿（探 ping 而非装载结果） | DEPLOYMENT | HIGH | E1（HEAD 现读）+ E3（3.5 复现的是"身份已死"**状态**，非 `/ready` 响应本身） |
| **N-11** | `connect`/`_load_from_file` 吞异常（N-1 上游；**两者可改的范围不同**，见 §6.1） | DEPLOYMENT | MEDIUM | E1 |
| **N-3** | 409 `currentRevision` 谎报（需**多进程/外部写**才触发） | USER | MEDIUM | E1 |
| **KB-6** | TTS 密集短句 `wait_all` 无超时 ⇒ FSM 卡 SPEAKING | USER | MEDIUM | E1+E0 |
| **F8** | prompt 预算 cross-path（preview 无预算；**小窗口 provider 上 100% 复现**，见 §5） | USER | MEDIUM | E1+E2 |
| **P2-01r** | 会话列表第 201+ 条不可分页/搜索 + 过期级联缺失 | USER | MEDIUM | E1 |
| **KB-2** | LLM 偶不发 `<mood>` | USER | LOW | E3 |
| **KB-4** | VAD 易被环境音触发 | USER | LOW | E3 |
| **F13** | 唤醒文本混入记忆/性格通路 | USER | LOW | E1 |
| **KB-8** | tokenizer NORMAL 态缓冲无上界 | INTERNAL | LOW | E1 |
| **KB-10** | TTS 取消 + HTTP 流清理可能泄漏 | INTERNAL | LOW | E1 |
| **F17** | 可选节点失败仍报 `COMPLETED` | INTERNAL | MEDIUM | E1 |
| **F3** | `plan_too_large` 语义 | INTERNAL | LOW | E1 |

### B. Runtime / Observability（TRUTHFULNESS 主表）

`N-1`、`N-11`、`N-3`、`N-10`、`F6`、`F7`、`N-15`（门禁层）、`OBS-3`（待复核）。
这一族的共同形态：**探测的是 A，报告成 B**。详见 §7。

### C. Deployment / Config

`DEP-1`（`.env.example` 9 键——**ALREADY_FIXED `67eeab83`**，旧记录"9/9 MISSING"已过期，Challenger 复核 9/9 存在）、
`DEP-2`（无迁移机制，KEEP_AS_IS/MEDIUM）、`DEP-3`（`clean_db` 清 v1 而生产写 v2 → 破坏范围决策）、
`DEP-4`（写侧长度上限）、`DEP-5`（`milvus_token` 永不生效，HEAD 复核 `secret=True` 仍在）、
`DEP-6`（锁与 uv）、`SEC-1`（CR-01 历史 key，owner-deferred）、`SEC-2`（bandit Low 基线）。

### D. Persistence / Recovery

`F2`（提醒重启即丢，feature 形状）、`F14`（durable delivery 已修 / exactly-once 不承诺）、
`RB-1`（3 个 write-only 字段，需真实 Mongo 抽样）、`N-12`（空串 pronouns，不可达）。

### E. Architecture Debt

`AR-1..AR-13`（ToolAdapter/OutputManager 收窄、provider portability、task LLM 只用主 provider、
包级伪环、EventBus epoch、import 期常量族、反向依赖豁免、accepted 窗口、双 CLI driver、`handle_json_message` 可选拆分）。

### F. Guardrail / Test Debt

`GR-1..GR-23`（文档数字门禁范围、弱用例、**N-15**、R4 漏报、同名遮蔽、R6 漂移分支、
RUF100、CI env 扫描、清空形态长尾、`if:` 守卫、print_test_stats、testing.md 计数、
`--strict-markers`、desktop golden 单源、`use_mocks` 死支路、空测试包、覆盖缺口、env 污染、token 残留、前端遗留、type:ignore/Any、openai_tts、gpt_sovits 泄漏）。

### G. Product Decisions / H. Future Features

见 `todo.md` §1.8（`PD-1..PD-11`）与 §1.9（`FEAT-1..FEAT-19`）。

---

## 5. Impact 维度（OPEN correctness 项）

| ID | user impact | data integrity | startup availability | silent failure | recoverability | blast radius | frequency |
|---|---|---|---|---|---|---|---|
| **N-1** | **高（Challenger 加重）**：不止 `/api/me` 503——**产品侧两处围栏也按 `is_available` fail-closed**（`CognitiveLoop._identity_snapshot` → 503 `STORAGE_UNAVAILABLE`；`queries.py` 预览、`instances.py` 归档），于是**部署围栏（200）与产品围栏（503）互相矛盾**：探针说健康，而对话轮次在它后面 503 | — | **高**（部署门禁失效） | **是**（唯一信号是日志 warning） | 中（重启/重连可自愈，但没人知道） | 单机部署面 + 身份子系统全部消费方 | 低（需装载失败）；**窗口 ≥60s**（健康循环首轮前 `await asyncio.sleep(60)`），装载失败持续则**永久** |
| **N-11** | 高（与 N-1 同链） | 中（N-2 的"静默重置为 bootstrap"曾由此放大） | 高 | **是** | — | 身份子系统 | 低 |
| **N-3** | 中（前端 CAS 重试可能死循环 / 显示错 revision） | 无（不写坏数据） | — | 否（有 409 返回） | 中（拿到真 revision 即可恢复） | 单条 PUT | 低——**且需多进程/外部写**（同进程更新会刷新 `_cached`） |
| **KB-6** | 高（角色卡在 SPEAKING，不再应答） | 无 | — | 部分（FSM 卡死无自动恢复） | 低（需重启） | 单会话 | 低（密集短句 5+） |
| **F8** | 中（预览 400） | 无 | — | 否（400 可见） | 高 | 单功能（`POST /api/characters/preview`） | **小窗口 provider（ollama 8K）上 100%**：内置角色预览 prompt ≈9283 est tokens vs 窗口 8192 / 预算 6144（Challenger 用仓内 estimator 实测） |
| **P2-01r** | 中（老会话翻不到/搜不到） | 无 | — | **是**（表现为"没有更多"） | 高（改查询即可读） | 单用户历史 | 随档案增长↑ |
| **KB-8** | 低 | 无 | — | 否 | — | 单进程内存 | 低 |
| **F13** | 低（记忆被污染为"用户说过唤醒词"） | 中（写入 LTM 后难回收） | — | **是** | 低 | 长期记忆 | 低 |

---

## 6. Status 核实结果（本批在 HEAD 上现读代码）

| # | 条目 | 本批核实 | 结论 |
|---|---|---|---|
| 1 | **N-1** | `ProfileStore.health_check()` = repo 可达性（docstring 自陈"恢复协调器探针"）；`is_available = _cached is not None and repo.is_connected`；`health.py` 的 identity 门禁用前者 | **OPEN 确认**（两个谓词回答不同问题） |
| 2 | **N-11** | `connect()`/`_load_from_file()` 仍 `except Exception` 只 warning | **OPEN 确认**（且是 N-1 无法回答"装没装上"的直接原因） |
| 3 | **N-3** | `update_if_revision` 冲突时**不**刷新 `_cached`；`get()` 在权威模式只返回 `_cached` | **OPEN 确认**（谎报机制仍在） |
| 4 | **N-10** | `Module/llm/registry.py:50` 仍无条件 `NotImplementedError("Gemini backend is not yet implemented")`；`preflight.py:54` 只有 `_STT_STUB_PROVIDERS` | **OPEN 确认**（LLM 侧仍无 stub 表） |
| 5 | **N-9** | import 期常量仍在（`custom_providers.py:16`、`log_setup.py:16`、`image_source.py:51-52`、`consolidator.py:34`、`vision/plugin.py:23`、`server.py:51`） | **OPEN 确认**（仍无已证损害） |
| 6 | **N-15** | `_KNOWN_UNREGISTERED_OPTION_INDEXES` 4 条仍在，且守卫会因"补表后不删条目"报陈旧 | **OPEN 确认**（须先决策，见 §9） |
| 7 | **F8** | `core/character/preview.py` 调 `build_messages(...)` **无** `prompt_budget_tokens`；`core/cognitive/loop.py:796` 有 | **OPEN 确认** |
| 8 | **F7** | `core/debug/info.py:291-292`：`tts_ok = agent.tts is not None`，`stt_ok = agent.stt_ok` | **OPEN 确认**（两种语义混用） |
| 9 | **F6** | Batch 1.6 表格明确 `✗B`（STT 读 config，llm/tts 读 runtime），本批未在 HEAD 复核到相反证据 | **OPEN（沿用 1.6 证据，E1）** |
| 10 | **F1 `WAITING_APPROVAL`** | `core/workflow_engine/core/finalize.py:103-104`：v1 恒 False，`waiting_for_approval` 仅由 `ASK_APPROVAL` 设置，而 **Evaluator v1 不产出**；`core/hitl.py:40` 自陈三处无触发路径 | **CURRENTLY_UNREACHABLE（= 未来功能阻塞项，非当前用户 bug）** |
| 11 | **KB-3 emoji 合并** | 后端分支已删（`4bb8aff7`，S3-G7），剩外部前端仓 | **后端 FIXED / 前端另仓** |
| 12 | **`llm_adapter.close` fire-and-forget**（2026-06-22 记录） | `core/adapter/llm_adapter.py:159-170` 先 `drain(_closing_backends, timeout=3.0)` 再关当前后端（ARCH-INC-P2-03） | **已被后续批次修掉**（todo 未关闭） |
| 13 | **`use_mocks` 死支路** | 默认 True，但 `Module/workflow_engine/tests/integration/test_e2e_mvp.py` 7 处调用**全部**显式 `use_mocks=False` | **OPEN 确认**（LIKELY_DEAD，测试侧） |
| 14 | **P2-01r 会话分页** | `session_repo.list_all` docstring 仍写"最多 200 条" | **OPEN 确认** |
| 15 | **KB-7 env 污染** | `test_phase2/3` 仍裸 `setdefault` | **OPEN 确认**（沿用 todo 记录，未逐一读文件） |

### 6.1 状态修正清单（本批相对旧 todo 的改动）

| 类型 | 条目 | 旧记录 | 修正为 | 依据 |
|---|---|---|---|---|
| **已修未关闭** | `llm_adapter.close` fire-and-forget | 2026-06-22「中低危待排期」 | **FIXED**（移入 Closed） | `core/adapter/llm_adapter.py:159-170` 先 `drain(_closing_backends, timeout=3.0)` 再关后端（ARCH-INC-P2-03） |
| **已修未关闭** | `KB-3` INTERRUPT+TTS emoji 合并 | 「需单独 mini-spec」（读起来像整条未修） | **后端 FIXED**（`4bb8aff7`），剩余竞态在**外部前端仓** | `StreamProcessor` 的 emoji 尾并入分支已删（S3-G7） |
| **需重分类** | `F1` `WAITING_APPROVAL` live-lock | 与其它 F 项并列（看起来是现役 bug） | **CURRENTLY_UNREACHABLE**（未来功能阻塞项） | `finalize.py:103-104`（v1 恒 False）+ `core/hitl.py:40`（三处无触发路径） |
| **需重分类** | `OBS-3` `openai_tts.test()` 假阴性 | 「中低危待排期」 | **INSUFFICIENT_EVIDENCE**（疑已随 `make_test_method` 统一基建消解） | 现实现走 `_health_check`（GET /health <400）；需一次定点复核才能关闭 |
| **需重分类** | `F14` durable delivery / exactly-once | 与 F 项并列（像未做） | **delivery 已 FIXED（Batch 1.7）；exactly-once = KEEP_AS_IS（刻意不承诺）** | Batch 1.7/1.8 记录 + `task_result_wal.py` |
| **需重分类** | `KB-5` 工具超时手工测试 | 独立「已知 Bug」行 | 归入 **`GR-19` 测试覆盖缺口**（不是产品 bug） | 该行自陈"需构造慢工具场景验证" |
| **已修未关闭（本批自查新增）** | `DEP-1` `.env.example` 落后 9 键 | 「换机器照 `setup.sh` 会踩空」（读起来是现役缺陷） | **ALREADY_FIXED** `67eeab83`（2026-09-13） | 9 键均已进模板（注释形态：`# MONGODB_URI=…`、`# AGENT_ACCESS_TOKEN=<openssl rand -hex 32>`）；凭据类刻意不预填。**本批首个自查纠正**：旧记录的"缺 9 键"已过期 |
| **状态写错（Challenger 命中，已改）** | **AR-9 / N-9** import 期常量族 | 「同形但**未被证明有实际损害**」 | **OPEN，已证一个实例有害**（严重性仍 LOW） | Challenger 实测：重定向 `ELYSIA_PATH_STATIC`/`ELYSIA_PATH_DATA_UPLOADS_IMAGES` 后，`image_source` 仍从**真实仓库文件**生成 data URI（读路径）；而 ≥4 个测试文件正是**晚设**该 env。**附带结论**：N-8 的守卫只覆盖上传路由，不含 `image_source`（守卫范围窄于它声称的类）。census 亦补齐为 **9 常量 / 8 文件**（+`secret_store.py:46`、`memory_reflector.py:32`；另 `TokenTracker._pending_file` 是类属性同形项，conftest 已记为已知缺口） |
| **状态过重（Challenger 命中，已降级）** | **GR-3 / N-15** | `PRODUCT_DECISION_REQUIRED`（"可能让线上多出 ERROR 告警"） | **OPEN（低风险机械收尾）**；`PD-3` 降为形式确认 | `check_schema_drift` **从不 raise**（任何异常降级为 `scanned=False`），冲突只 `logger.error`——"只报告不处置"就是实现本身；且代码声明 `unique=True` 意味着库里同名非唯一索引**本就已经让 `create_index` 失败**（`connect()` 吞成 `_db=None`）⇒ 报出的是真阳性。**新增独立盲点 `GR-24`**：`_option_mismatches` 不比对 `partialFilterExpression` ⇒ 部分唯一索引只能"半登记"（库里是普通唯一索引时不报冲突，而 `create_index(partialFilterExpression=…)` 必抛） |
| **前半已关闭（Challenger 命中，已补行）** | `N-2` | 旧记录「本地模式静默接受 `pronouns=None`；`_load_from_file` 读回失败重新 bootstrap」 | **前半 CLOSED by `fb438167`**（`pronouns: str \| None` ⇒ `None` 合法）；剩后半 = 本地模式静默重置 ⇒ **CURRENTLY_UNREACHABLE** | `core/profile/user_identity.py:34`；N-2 此前在 `todo.md` **整条漏行**，现已补 §4 |
| **范围不可执行（Challenger 命中，已改 §10）** | `N-11` 修法「不再吞异常」 | 对**两个函数**一视同仁 | **`connect()`** 可安全 raise（`_start_persistence` / `_recover_dependency` 都已有 try/except）；**`_load_from_file` 的吞是 load-bearing**（本地模式 `get()`/`bootstrap()` 直接调它 ⇒ 移除会把本地 `GET /api/me` 变成 500） | 因此 N-11 的正确修法是「**留下可查询的失败状态**」，而不是"把两处 except 都删掉" |

---

## 7. Truthfulness Backlog（单列）

```text
                        ┌── 报告层 ────────────────────────── 实际层 ──────────┐
进程健康   N-1   /ready 200 + chatReady            identity 未装载（/api/me 503）
           N-11  只 warning、其余静默              装载/读取失败被吞，无处可查
API 元数据 N-3   409 currentRevision = 1           权威库 revision = 2（进程内缓存）
provider   N-10  preflight [PASS] (gemini)         运行期 NotImplementedError
           F6    /api/providers/test 测 config 模型 runtime 用的是另一个（STT）
面板标志   F7    tts_ok = true                     仅表示对象非空
门禁       N-15  "13 个集合无漂移"                 4 条 unique 从未参与比对
（已修）   N-4/N-8                                  Batch 3.6 已 GUARDED，且守卫被 Challenger 补强
```

**共同模式**：判据选了一个**可得但不等价**的代理量（ping / 存在性 / 缓存 / 静态表 / config），
并通过"看起来绿"把差异藏起来。批次设计上它们可分属不同子系统（见 §9 备选），
但**不应在同批里混做**——除 §10 推荐的那一对（共享同一个 store 与同一条因果链）。

---

## 8. Dependency Graph

```text
PD-1（装载失败语义：fail-closed 启动 vs 降级运行）
   │  ⚠️ 只闸 N-11 的**失败语义**；N-1 是纯不变量修复，不需要决策（Challenger）
   ├─▶ N-11（吞异常 ⇒ 失败不可观测）──┐
   │                                  ├─▶ N-1（/ready 假绿）★ Batch 4-A
   └──────────────────────────────────┘
       同源证据：`_dependency_needs_recovery("identity")` 判据**已经**是
       `is_authoritative and not store.is_available`（_lifecycle_persistence.py:625-631），
       而探针注册处用的却是 `health_check`（:502）——**把 /ready 对齐 is_available
       会让"探针"与"修复器"首次互相一致**，这正是修法的正当性来源，而不是风险。

PD-2（409 该报谁）── **形式确认**（只有权威值可用；`UserIdentityRepository.get()` 已存在）
   └─▶ N-3（谎报 revision；需多进程/外部写）  ← 可选并入 A（= A+）

PD-4（审批流是否启用）──▶ F1 WAITING_APPROVAL live-lock ──▶ core/hitl.py 接线 + Evaluator v2
   （今天**不可达**：两道 v1 闸门 + `NodeStatus.BLOCKED` 全仓零生产者；live-lock 仅由
    Challenger 强制打开 needs_approval 时观察到 ⇒ **mutation-only observation**）

PD-3（形式确认）──▶ GR-3/N-15（4 条 unique，机械收尾）──▶ GR-24（部分索引语义，需独立设计）

N-10（gemini 假绿）──▶ 需补 LLM 侧 stub 冻结表 ──┐
F6（test 读 config）──────────────────────────┼─▶ 同族：报告的能力 ≠ 实际能力（Batch 4-B）
F7（tts_ok 存在性）────────────────────────────┘
F5（workflow reporter 缺）── 依赖 AR-5 ⊂ AR-4（task LLM 归属）

AR-9（import 期常量族）── image_source 读路径**已证有害**；N-8 守卫只覆盖上传路由 ⇒ 类守卫仍缺
GR-4（R4 漏报）── 独立专项（改图粒度）
GR-5（同名遮蔽）── 需类型/所有权推断 ── 接受（回归网定位）
```

---

## 9. Batch 4 Options

> 评分仅作排序参考（Impact 0–3 / Reachability 0–3 / Confidence 0–2 / Fix locality 0–2），**不代替判断**。

| 候选 | 内容 | Σ | 共同 root cause | 可立 invariant | 一次闭环 |
|---|---|---|---|---|---|
| **A** | **N-1 + N-11**（Runtime Health Truthfulness） | 10+9=**19** | ✅ 同一 store、同一条因果链 | ✅ `reported readiness == identity 实际装载且可用` | ✅ 决策→实现→守卫 |
| **A+** | **N-1 + N-11 + N-3**（= A 加同一 store 的 REST 报告真值） | **27** | ✅ 同一 store（`profile_store.py` + 其两个消费面） | ✅ `报告的状态 == 权威 store 的状态` | ✅ 一个 harness 覆盖三处 |
| **B** | **N-10 + F6 + F7**（Provider Capability Truthfulness） | 9+7+7=**23** | ⚠️ 同一族但三个子系统 | ✅ `reported capability == runtime capability` | ✅ 三处小修 + 一处冻结表 |
| **C** | **N-3**（API Contract Truthfulness） | 8 | ✅ 单一 | ✅ `409 元数据 == 权威库状态` | ⚠️ 单条；**建议并入 A 成 A+** |

> **Σ 不构成推荐依据（必须显式说明）**：Σ_B(23) > Σ_A(19)，若只看分数应选 B。
> 推荐 A 的**真实 tie-break** 是三条加权：① **可达性质量**——A 是 DEPLOYMENT+USER 面（`/ready` 假绿会让部署与产品围栏互相矛盾），
> B 全是 ADMIN 面（用户可见面为零）；② **假绿代价**——A 让自动化/运维被误导，B 只让调试面板/预检被误导；
> ③ **闭环性**——A 的两个条目共用同一函数族与同一 harness，B 的三个条目跨三个子系统、无法共享回归网。
> 分数只用于排序参考（§17 纪律），不作为判据。

### Option A — Runtime Health Truthfulness（**推荐**）

- **包含**：`N-11`（吞异常 → 失败可观测）→ `N-1`（`/ready` 问对问题）。
- **为什么一起做**：两者是**同一条因果链**——不先让装载失败可见，`/ready` 就无法正确回答；
  改一个不改另一个会得到"更响的日志 + 仍然假绿的就绪"。
- **预期风险（Challenger 修正）**：比初稿低。**不需要**改 `health_check()` 的语义——它是
  "恢复协调器探针"，而补偿谓词 `_dependency_needs_recovery("identity")` **本来就是** `not store.is_available`；
  把 `/ready` 对齐 `is_available` 只会让**探针与修复器首次一致**。真正的风险点是 `N-11` 的**可改范围**：
  `connect()` 可直接 raise（调用方已有 try/except），`_load_from_file()` 的吞**是 load-bearing**（本地模式），
  只能"记录状态 + 提高日志级别"，不能删 except。⇒ `PD-1` 只闸后者。
- **预期测试方式**：unit/service 层构造「repo ping OK + 装载失败」的 store → 断言 `/ready` 503、
  `/api/me` 与之一致；反向用例（装载成功）保持 200；守卫用变异验证（把判定退回 `health_check()` 必须变红）。
  不需要真实 Mongo（fake repo 即可），可选一条真实 Mongo 旁证。
- **为什么现在**：这是唯一一条**会误导部署/自动化**的假绿，且 Challenger 查出它的代价比初稿更重——
  产品侧两处围栏（`CognitiveLoop._identity_snapshot`、预览/归档查询）已按 `is_available` fail-closed，
  于是"探针 200"与"对话 503"直接矛盾，窗口 ≥60s（健康循环首轮前 sleep）或永久。
  修复面 2 个文件、无 wire/契约变更。
- **可选扩展（A+）**：加 `N-3`（同一 store 的 409 报告真值）。Challenger 判 `PD-2` 只是形式确认
  （只有权威 revision 可用，且 `UserIdentityRepository.get()` 已存在）。若 owner 接受"一批三改"，A+ 是更完整的闭环；
  若坚持最小面，A 仍成立。

### Option B — Provider Capability Truthfulness

- **包含**：`N-10`（LLM 侧 stub 冻结表：gemini）＋ `F6`（`/api/providers/test` STT 读 runtime）＋ `F7`（`tts_ok` 语义）。
- **为什么一起做**：三者共同 invariant = "报告的能力 == 运行期的能力"，且都在 provider/调试面，测试策略相同（表驱动负面用例）。
- **预期风险**：低。`N-10` 改的是**预检判据**（可能让原本 PASS 的配置变 WARN/FAIL —— 正是目的）；
  `F7` 若改语义需同步 debug 契约字段（`tts_ok` 是否重命名）。
- **预期测试方式**：preflight 表驱动（每个未实现 provider 必须 FAIL/WARN）；`/api/providers/test` 用 runtime 模型断言；
  debug 面板字段语义测试。
- **为什么现在 / 为什么不**：证据最硬（`N-10` 是 E3 实测）、成本最低；但它**只影响配置/运维侧**，
  用户可见面为零，因此排在 A 之后。

### Option C — API Contract Truthfulness（单条，或并入 A）

- **包含**：`N-3`（409 `currentRevision`）。
- **为什么不现在**：单条 + 需要 `PD-2`（"409 该报谁"）↔ 收益/协调成本比低。
  更自然的做法是**并入 A**：同属 `core/profile/profile_store.py` 的"报告真值"，
  若 owner 愿意在批内多做一条，A 可扩为 A+。

### 明确不推荐的组合

- ❌ **"8 个不同领域的 bug 一起做"**（例如 N-1 + N-3 + N-10 + KB-6 + F8）：违反 §18 的同族原则，
  验证成本翻倍且回归面互不相关。
- ❌ **把 `F8`/`P2-01r` 塞进 A**：它们是不同子系统（prompt 组装 / 会话仓储），只是"看起来都在 correctness 池里"。
- ❌ **把 `F1`（WAITING_APPROVAL）排进 Batch 4**：它今天**不可达**（§6 #10），排它是把未来功能当现役 bug。

---

## 10. Recommended Batch 4

### 主题

**Runtime Health Truthfulness（运行时健康真值）**

### Scope（建议 3 个 commit 组）

1. **决策落纸**（`PD-1`）：身份装载失败时 agent 的行为（fail-closed 启动 vs 降级运行）+ `/ready` 该探什么。
   产出一段 spec 级说明，**不写代码**即为一个 commit。（Challenger 注：这条只闸 `_load_from_file` 的失败语义；
   `N-1` 本身是纯不变量修复，不需要决策。）
2. **N-11**：让装载失败**留下可查询的状态**（而非只有一行 warning）——
   `connect()` 可以安全 raise（两个调用方 `_start_persistence` / `_recover_dependency` 已有 try/except）；
   `_load_from_file()` 的吞**保持**（本地模式 load-bearing），只把它变成"可观测的降级"。
   ⚠️ 初稿写的"不再吞异常"对第二个函数会**破坏语义**，Challenger 已证伪，按本条执行。
3. **N-1**：`/ready` 的 identity 门禁改问"身份是否装载且可用"（`is_available` 语义），
   与 `/api/me` 的可用性判定**同源**、并与 `_dependency_needs_recovery("identity")` 的判据**一致**；
   `chatReady=false → 503`。**不动** `health_check()` 本身（它仍是恢复协调器的探针）。
4. **守卫**：一条可变异验证的测试（把判定退回 `health_check()` ⇒ 必红），
   外加**一致性断言**：`/ready` 绿 ⇒ `/api/me` 不 503（同一 fake 状态下两者必须同真同假）。

### Why now

- 这是唯一一条**会误导部署/自动化**的 `TRUTHFULNESS` 假绿；Challenger 复核后其代价比初稿更明确：
  **部署围栏（200）与产品围栏（503）互相矛盾**（身份子系统的三处消费方都按 `is_available` fail-closed），
  窗口 ≥60s 或永久；
- 修它=把 Batch 3.5 那个 CRITICAL 缺陷（B3-CB-1）的**放大因素**一并关掉，形成闭环；
- 面窄、无契约/wire/schema 变更，且**不需要改** `health_check()` 的既定语义。

### Why bounded

- 只碰 `core/profile/profile_store.py` + `core/input/health.py`（+ 测试）；
- 新谓词并列、不改旧谓词（`health_check` 仍服务恢复协调器，而补偿判据本就已是 `is_available`）；
- 不引入新框架：守卫就是"同一状态下两个报告的**一致性**"+"变异必红"。

### Dependencies

- **前置**：`PD-1`（owner 一句话，且只关 `_load_from_file` 的失败语义）。`N-11` 先行、`N-1` 后行（同批两个 commit）。
- **弱依赖**：`N-2`（`_update_local` 绕过校验）——本地模式、生产不可达，**不**纳入。
- **可选扩展（A+）**：`N-3`（同一 store 的报告真值；`PD-2` 已降为形式确认）。

### Out-of-scope（本批明确不做）

- `N-10`/`F6`/`F7`（→ 备选 B）、`F8`、`P2-01r`、`KB-6`、`AR-*`、`GR-*`、`FEAT-*`；
- **不**顺手改 `health_check()` 的既有调用方（recovery coordinator / product mode 的探针注册）；
- **不**新增 architecture abstraction、**不**改 schema/API/wire。

---

## 11. 覆盖审计（旧 `todo.md` 每一节 → 现在在哪）

| 旧章节 | 去向 |
|---|---|
| 头部 CI 配置说明 | `todo.md` §7 决策记录 |
| 决策记录（任务中心暂缓 / `core/task` 预留保留） | `todo.md` §7（逐字保留） |
| Code Review 2026-09-12 剩余项（CR-01 / bandit / xdist / context_window / S2-H9） | §1.4 `SEC-1`/`SEC-2`、§1.3 `OBS-1`/`OBS-2`、§1.4 `DEP-4` |
| 已知 Bug 表（18 行） | §1.2/§1.3 `KB-1..KB-10` + §4 + §5 |
| 健壮性 & 架构 的「`.env` 含真实 key」行 | §5（复核降级 + **ALREADY_FIXED** `67eeab83`） |
| 阶段 3/4 联调 blockquote（已闭环） | §5 一行索引（证据在 `docs/review/archive/2026-08/*`） |
| Electron v2 联调（长 blockquote + 12 行 ✅ 表 + 建议批次顺序） | §5 一行索引；其中"不应登记成后端缺陷的前端适配项"4 条 → **§6 判例**（保留，防误登记） |
| 全项目修复计划剩余项 A/B/C/D | A/B 全部 ✅→§5；C 的 F4 残余 → `AR-13`、F5 → §1.3 `F5d`、E6 → `GR-16`；D 的 P2-01/02/09 → `P2-01r`/`P2-02r`/`AR-8` |
| 模块化评估（反向依赖 / 软环 / 08-14 复审） | §1.6 `AR-9`/`AR-10`/`AR-7` + §5 已修行 |
| token 统一结构遗留（5 行） | `GR-21` |
| code-review-2026-07-31 遗留（M19/M20） | §5 一行索引 |
| 功能改进（全部 feature） | §1.9 `FEAT-1..FEAT-19` |
| 前端（frontend_todo 归档） | `GR-22` |
| 代码清理：Batch 2 Challenger 项 | `GR-4`..`GR-12` |
| 代码清理：Batch 2.3 F401 follow-up | `GR-8`/`GR-9`/`GR-10`/`GR-13`/`GR-14`/`GR-15` |
| 代码清理：桩代码决策 + 保留表 | §7 决策记录 + §4 + `dead-code-review-checklist.md` §9 |
| Batch 3 登记项：正确性 bug 表 | §5（B3-CB-1..7 一行索引） |
| Batch 3.5 out-of-scope findings | §1/§2/§4（N-1..N-15 全部有 canonical 状态） |
| Batch 3.6 登记 | §5（N-4/N-8 GUARDED）+ §1.7 `GR-3`（N-15） |
| 测试侧清理候选 | `GR-17`/`GR-18` |
| 有意保留（Batch 3 审计后 KEEP） | §4 + §1.6（未重开，遵守 dead-code 清单） |
| 模块化改进（safety.py 不拆） | §5 一行索引 |
| 多模态 Phase 2 | `FEAT-3` |
| 健壮性 & 架构（9 行） | §1.4/§1.6/§5 + `GR-23` |
| 多前端支持（✅ 表 + 触发条件） | §5 + `FEAT-17` |
| code-review-2026-06-22 遗留 | `GR-22`（a11y/CSP/AudioWorklet）、§5（llm_adapter close）、`OBS-3`（openai_tts）、`KB-10`（gpt_sovits）、`GR-19`（覆盖缺口） |
| 测试补充 | `GR-19` |

**初稿的覆盖声明是错的（Challenger §6.3）**：初稿写"没有任何条目被静默丢弃"，实际漏了 5 类，现已全部补上：

| 漏项 | 来源 | 现在的去处 |
|---|---|---|
| **N-2**（`_update_local` 绕过校验） | Batch 3.5 §12 | `todo.md` §4（前半 CLOSED by `fb438167`，后半 CURRENTLY_UNREACHABLE） |
| **`DEP-1` 的关闭行** | 本批自查（`67eeab83`） | `todo.md` §5（原先 ID 悬空） |
| **`ConfigLoader(env_path=…)`** | 旧 `todo.md` 保留表 + Batch 1.6 §12 R5 | `AR-14`（KEEP_AS_IS 不删） |
| **Batch 1.5 R4**——两个一次性 `.env` 加载器 | Batch 1.5 记录 | `AR-15`（OPEN；`EnvManager` 类名已不存在，但 `_LOAD_ATTEMPTED` + loader 的 `_loaded`/`ensure_dotenv` 仍在） |
| **Batch 1.6 §12#6 / #2b** | Batch 1.6 记录 | `OBS-4`（定位不到，需定性复核）/ `OBS-5`（`/debug/api` 补 stt/tts `model`+`enabled` 字段，F7 只覆盖了标志语义） |
| （附加）`TokenTracker._pending_file` | `conftest.py` 自陈的"已知缺口" | 并入 `AR-9` 的 census |

**结论（修正版）**：旧 `todo.md` 的每一节都有去向；**Batch 3.5 / 1.6 / 1.5 的几类残留是初稿漏登记的**，现已补齐并标注来源。
`KEEP_*` 项一律未重开（含 `dead-code-review-checklist.md` §9 的全部判定）。

---

## 12. Challenger

独立子代理（独立上下文）对本批的**分类 / 排序 / 去重**做了对抗式复核，**只读**（未改任何文件）。
它自带五类猎捕目标：future feature 当现役 bug / currently-unreachable 排太高 / 重复登记 / 已被后续批次修掉 / 名字吓人就给 HIGH。

### 12.1 Verdicts

| Claim | Verdict | 说明 |
|---|---|---|
| **C1** ID canonicalization | **overstated** | §12→todo 的 5 行映射**全对**；但同一份 Batch 3.5 报告在 §17 另有一套编号、且 §9 的交叉引用是断链；初稿把 guard-gap `N-8` 的出生地写成 "Batch 3.6 §17"（该报告只到 §12）——**已改正**（§3.2 + `todo.md` §2 警告） |
| **C2** duplicates merged | **holds，但漏 4 组** | `KB-1≡FEAT-2`、`F2≡FEAT-1`、`KB-5⊂GR-19`、`AR-5⊂AR-4` 按本批自己的三重判据应合并——**已改为指针**（不单独计数） |
| **C3** status claims | **11/12 holds；2 条写错** | `AR-9`"无已证损害"**被证伪**（`image_source` 读路径实例）；`GR-3/N-15` 的 `PRODUCT_DECISION_REQUIRED` **过重**（应降级为机械收尾）——**均已改正**，并新增 `GR-24`（部分索引半登记盲点） |
| **C4** Option A 推荐 | **holds，4 处需修正** | Σ_B(23) > Σ_A(19) 与推荐矛盾（已补 tie-break）；N-1 的 E3 是**继承**而非为 `/ready` 实测（已改标注）；`PD-1` 只闸 N-11 而非 N-1（已写明）；建议 A+ = N-1+N-11+N-3（已列为可选扩展） |

### 12.2 本批据其修正的内容（逐条）

1. **`DEP-1` 自相矛盾**（§4C 说"9/9 MISSING"、§6.1 说 ALREADY_FIXED）→ 已统一为 ALREADY_FIXED `67eeab83`，并给 `DEP-1` 补了 `todo.md` §5 的关闭行（原先 **ID 悬空**）。
2. **`N-2` 整条漏行** → 已补 `todo.md` §4（前半 CLOSED by `fb438167`）。
3. **`AR-9` 状态写错** → 改为"已证一个实例有害"（`image_source` 读路径；重定向 env 后仍读真实仓库文件），census 补齐为 9 常量 / 8 文件，并记下"**N-8 守卫只覆盖上传路由**，类守卫仍缺"。
4. **`GR-3/N-15` 过重** → 降级为 OPEN（机械收尾）；`PD-3` 降为形式确认；新增 `GR-24`（`partialFilterExpression` 不参与比对 ⇒ 半登记会给假保证）。
5. **`F8` 频率写错** → 改为"小窗口 provider 上 100%"（内置角色预览 ≈9283 tokens vs ollama 8192），并修正 `todo.md` §1.2 的候选批次指针（原写 "Batch 4-B/C 备选"，而 B/C 都不含它）。
6. **`N-1` 证据标注 + 影响被低估** → 改为 `E1(HEAD) + E3(3.5 复现的是状态而非 /ready 响应)`；影响补上"部署围栏与**产品围栏**（`CognitiveLoop._identity_snapshot`、预览/归档）互相矛盾、窗口 ≥60s 或永久"。
7. **`N-11` 修法不可执行** → §10 明确：`connect()` 可安全 raise，`_load_from_file()` 的吞是 load-bearing，只能记录状态。
8. **"协调器是约束"是误读** → 补偿谓词本就是 `not store.is_available`（`_lifecycle_persistence.py:625-631`），对齐 `/ready` 让探针与修复器**首次一致**；§8/§9/§10 已改写为"证据而非风险"。
9. **补充缺失条目** → `AR-14`（`ConfigLoader(env_path=…)`）、`AR-15`（两个一次性 `.env` 加载器）、`OBS-4`（Batch 1.6 §12#6 定位不到）、`OBS-5`（`/debug/api` 补 stt/tts 字段）、`TokenTracker._pending_file` 并入 `AR-9`。
10. **计数与标签自洽** → §1 计数改为"109 行 / 去重 105 条"并注明交叉引用；`§1.2` 标题改为"13 行：9 条 USER/DEPLOYMENT + 4 条 INTERNAL 侧"；指针项标注"不单独计数"。

### 12.3 攻击失败 / 接受的风险（保留原判）

| 项 | 结论 |
|---|---|
| `WAITING_APPROVAL` 不可达 | **成立且更强**：两道 v1 闸门（Evaluator v1 不产 `ASK_APPROVAL`；`orchestrator.py:436` 的 `needs_approval(node)` 恒 False）+ `NodeStatus.BLOCKED` **全仓零生产者**；live-lock 仅由 Challenger **强制打开** `needs_approval` 时观察到 ⇒ 准确定性是 **mutation-only observation**，分类为未来功能阻塞项正确 |
| `N-12` 空串 pronouns 是否被 B3-CB-1 顺带修掉 | **否**（攻击失败）：500 来自**响应契约**的 `_text(maximum=200)` 对 `""` 抛错，与 `pronouns: str \| None` 无关 ⇒ OPEN/CURRENTLY_UNREACHABLE 正确 |
| `N-15` 的 4 条 census | **独立复现一致**（15 条带选项调用 − 11 条已登记 = 同样的 4 条），无第五条 |
| `GR-17` `use_mocks` | 7/7 调用点 `False` 成立；**补充**：helper 默认是 `True`，新调用方省略 kwarg 即可达 ⇒ 删除须原子**或**先改默认 |
| `N-5`/`GR-1` | 成立且有界：真值 46（AST），三份文档写 46，`docs/architecture.md:372` 写 43 ⇒ 纳入门禁会立刻红（一行文档 + 一个参数） |
| `DEP-2`（无迁移机制） | **不存在"名字吓人就给 HIGH"**：已按实测频率（4 个月 24 次索引变更、仅 ~2 次需人工）评为 MEDIUM + KEEP_AS_IS |
| `F6` / `F7` / `KB-3` / `llm_adapter.close` / `M19/M20` / `LoggerFactory` / `OBS-3` | 全部复核成立（F6 另有**同文件内不一致**：`_is_custom_provider_active` 对 stt 含 `runtime_model`，而 `_resolve_test_model` 读 config；F7 的 `_stt_ok` 确为真实连通标志，面板渲染 ✓/✗） |
| 遗留杂物 | `tmp/batch1_5_probe/` 下有 Batch 1.5 的临时探针脚本（未跟踪/被忽略）；不属本次输入，未处理 |

### 12.4 Challenger 自己的排序（与本批推荐一致）

1. **A+（N-1+N-11+N-3）** —— 最优（同一文件、同一 invariant、同一 harness）；2. **A（N-1+N-11）** —— 若坚持范围纪律仍是正确选择；
3. **B（N-10+F6+F7）** —— 单条证据最硬（N-10 是 E3 实测），但三个子系统、纯 ADMIN 面，且 N-10 会让某些原本能启动的配置在预检变红（有 `--skip-preflight`）；
4. 隐藏候选：**约 1 小时的机械门禁清扫**（`GR-1` 加文档 + 43→46；`GR-3` 补 4 条 + 删记账；`GR-2` 两条弱用例），**零生产行为变化**，适合当热身而非主题。

**推荐结论存活**，但是靠**影响面 + 局部性**，不是靠证据等级——本批已在 §9 显式写明这条 tie-break。

---

## 13. Out-of-Scope 声明（本任务没做什么）

- **没改 production code**（`git diff --stat` 只有 `todo.md` 与本文）；
- **没改 tests / CI / 配置**；
- **没修 bug、没加 guard、没重构、没删任何 TODO 对应代码**；
- 没有重新做全仓 bug audit（只对 §6 的 15 条做了 HEAD 现读核实）；
- 没有开始 Batch 4 实现（§10 只是建议）。
