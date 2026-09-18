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

### 1.2 Correctness（14 行：10 条 USER/DEPLOYMENT 可达 + 4 条 INTERNAL 侧）

| ID | 标题 | Reach | Sev | Ev | Status | 候选批次 |
|---|---|---|---|---|---|---|
| **N-1** | ~~`/ready` 的 identity 门禁探「repo 可达」而非「身份已装载且可用」⇒ 装载失败时 `/ready` 200 而 `/api/me` 503~~ | DEPLOYMENT | HIGH | E1(HEAD)+E3(3.5)+**E3(4)** | **GUARDED** `82b80e2e` | ✅ Batch 4 |
| **N-11** | ~~`ProfileStore.connect` / `_load_from_file` 吞掉一切异常（N-1 的上游：失败不可观测）~~ | DEPLOYMENT | MEDIUM | E1+E3(3.5)+**E2(4)** | **GUARDED** `fc648e20` | ✅ Batch 4 |
| **N-3** | ~~409 `details.currentRevision` 读进程内陈旧缓存 ⇒ 谎报，文档承诺的 CAS 恢复路径可死循环~~ —— 冲突现值改由 repository 在冲突点回读权威库并随结果携带（`UserIdentityUpdateOutcome.current_revision`），route 只投影；未知则省略该字段 | USER | MEDIUM | E1+**E2+E3(6)** | **GUARDED** `616e4993` `TRUTHFULNESS` | ✅ Batch 6 |
| **KB-6** | TTS 密集短句（5+）时 `wait_all()` 无超时 ⇒ FSM 卡 `SPEAKING`（已用 read timeout 120s→30s 缓解，根因未复现） | USER | MEDIUM | E1+E0 | **INSUFFICIENT_EVIDENCE** | 需先复现 |
| **F8** | ~~prompt 预算 cross-path：`core/character/preview.py` 调 `build_messages` **不传** `prompt_budget_tokens`（chat 路径传） ⇒ 预览路径可超 context → provider 400~~ —— 预览改用 canonical 预算（**resolved runtime model** 的声明窗口 × 同一常量）；裁完仍装不下 → **本地** 400 `CHARACTER_VALIDATION`（非重试、**零 provider 请求**），不再把确定性的输入问题谎报成可重试的 503。**频率主张修正（Batch 7 现测）**：原记「≈9283 est tokens」实为**字符数**（9235 chars → 7597 est tokens < 8192）⇒「内置角色 100% 复现」不成立；真实边界是**预览消息 > ~575 CJK 字**（内置 elysia 余量）或档案更大时确定性失败 | USER | MEDIUM | E1+**E2(7)** | **GUARDED** `558503be` `TRUTHFULNESS` | ✅ Batch 7 |
| **KB-8** | tokenizer NORMAL 态 `_pending` / `_held` 缓冲**无上界**（长无换行段落可撑到 MB 级） | INTERNAL | LOW | E1 | **OPEN** | 小修 |
| **F13** | 系统注入文本（唤醒词）被当作「用户消息」参与记忆提取 / mood 判定 | USER | LOW | E1 | **OPEN** | 小修 |
| **P2-01r** | 会话列表 `list_all` 仍预取 200 ⇒ **第 201+ 条不可分页/搜索**；显式 session 过期无级联清理 worker | USER | MEDIUM | E1 | **OPEN** | Batch 4 备选 |
| **KB-2** | LLM 有时不输出 `<mood>`（回退关键词），deepseek-v4-flash 结构化输出不稳定 | USER | LOW | E3 | **OPEN** | prompt 小修 |
| **KB-4** | VAD 易被环境音触发（aggressiveness / 降噪） | USER | LOW | E3 | **OPEN** | 调参 |
| **KB-10** | TTS 取消 + HTTP 流清理交互可能泄漏（`Module/tts/gpt_sovits.py`，2026-06-22 记录） | INTERNAL | LOW | E1 | **OPEN** | 零散 |
| **KB-11** | **Batch 6 Challenger F-1**：`CharacterService._retarget_default_character` 的重试循环用 `user_identity_store.get()`（**缓存**）取 revision，而冲突路径刻意不刷缓存 ⇒ 权威模式下并发改档案时 3 轮重放同一个 expected、必然 `RevisionConflictError`（实测 observed = `[(1, False, 2)]*3`，fail-closed 无数据损坏，但归档默认角色会假失败）。本批新带的 `outcome.current_revision` 在这条路径**无消费者** | INTERNAL | LOW | E2(6) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` | 零散（改角色归档写链） |
| **F17** | 可选节点失败时任务仍报 `COMPLETED`（文案同时含「已完成」与失败列表）——引擎终态定义问题 | INTERNAL | MEDIUM | E1 | **OPEN** → `PD-6` | 需语义决策 |
| **F3** | `plan_too_large` 语义重构（需新枚举值 ⇒ 触契约可达性门禁） | INTERNAL | LOW | E1 | **OPEN** → `PD-5` | 需契约决策 |
| **KB-12** | **Batch 7 Challenger W1/W2**（预览本地窗口判定的误差带）：`estimate_tokens` 是字符启发式，**对 CJK 偏高、对 JSON/代码偏低** —— 实测 est 8329 vs qwen 真值 7921（偏高 ⇒ 可能**误拒**一个勉强装得下的预览）、est 7302 vs 真值 12212（偏低 ⇒ 在强制窗口 provider 上仍可能 400 → 503）。修它需要真 tokenizer（§15/§16 明示不在 Batch 7 范围），且该 estimator 是 chat 与预览的共享底座 | USER | LOW | E2(7) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` | 需 tokenizer 决策 |

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
| **KB-13** | **Batch 7 Challenger W3 + 主 Agent E3**：`providers.yaml::llm.ollama.context_window = 8192` 与实测 ollama 0.24.0 默认 `num_ctx = 4096` **不一致**，且 ollama **超窗时静默截断并返回 200**（不报 400）⇒「装得下声明窗口」是**必要非充分**条件；影响 chat 与预览**两者**（人设被悄悄削掉）。同时解释了 F8 原始登记里「provider 400」为何在本地 ollama 上复现不出来。**注**：本条反驳 `OBS-2` 括号里的「不影响正确性」 | DEPLOYMENT | MEDIUM | E3(7) | **OPEN** `NEW_FINDING_OUT_OF_SCOPE` `TRUTHFULNESS` | owner 决策（改它会全局改 chat 截断时机） |
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
| **AR-8** | P2-09 残留：EventBus lifecycle epoch（旧 epoch 事件潜在重放，风险低） | 低 | E1 | **OPEN** |
| **AR-9** | **N-9**：模块级 import 期 `ProjectPath...resolve()` 常量——**census 9 个常量 / 8 个文件**：`custom_providers.py:16`、`log_setup.py:16`、`image_source.py:51-52`（2 个）、`consolidator.py:34`、`vision/plugin.py:23`、`server.py:51`、`secret_store.py:46`、`memory_reflector.py:32`；另有同形的**类属性** `TokenTracker._pending_file`（`conftest.py` 已把它记为"已知缺口"）。**已证一个实例有害**（Challenger，Batch 4）：`image_source._IMAGES_DIR` 在**读路径**使用，而 ≥4 个测试文件晚设 `ELYSIA_PATH_DATA_UPLOADS_IMAGES` ⇒ 重定向对读路径静默无效（`/Elysia.png` 仍从真实仓库文件生成 data URI）。**无测试断言错误结果**，故严重性仍 LOW；且 N-8 的守卫**只覆盖上传路由**，不含 `image_source`（守卫范围窄于它声称的类） | 低 | E1（image_source 实例 E2） | **OPEN**（其余 8 个仍无已证损害） |
| **AR-14** | `ConfigLoader(env_path=…)` 形参：无生产调用方（Module 是库 ⇒ 无法证明仓外无调用方）。Batch 1.6 §12 R5 另记「加 DeprecationWarning 或删除参数」 | 低 | E1 | **KEEP_AS_IS**（`LIKELY_DEAD` 不删） |
| **AR-15** | Batch 1.5 R4 的两个**一次性 `.env` 加载器**仍在：`Module/_common/env.py::_LOAD_ATTEMPTED` + `Module/config/loader.py` 的 `_loaded`/`ensure_dotenv`（docstring 自陈"入口点 API，库代码不要调用"；现存调用点 `scripts/dev/stage_h_pinned_smoke.py:301`） | 低 | E1 | **OPEN** |
| **AR-10** | 反向依赖残留：`Module/tool/safety.py` lazy `core.utils` + `Module/bench` lazy core（已评估接受 / CLR 豁免） | 低 | E1 | **KEEP_AS_IS** |
| **AR-11** | F11 accepted→consumed 窗口（ACK 证明入 EventBus ≠ 业务已持久化；BE-RISK-01） | 中 | E3 | **KEEP_AS_IS**（已接受的 at-least-once 边界） |
| **AR-12** | 两套 CLI driver：`AgentServer.run_repl`（0 代码消费者，但 8 处文档承诺）vs `app.py::run_interactive`（真实入口，**行为已分岔**：后者有 `wait_until_idle()`） | 低 | E1 | **PRODUCT_DECISION_REQUIRED**（`PD-10`） |
| **AR-13** | F4 残余：`handle_json_message`（≈16 圈 / 80 行）可选进一步拆分（已改为 `_WS_HANDLERS` 注册表薄壳，非阻塞） | 低 | E1 | **OPEN**（可选） |

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
| 门禁 | **N-15** | “13 个集合无漂移” | 4 条 `unique` 从未参与比对 | DEPLOYMENT | MEDIUM |
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
| **KB-3** INTERRUPT+TTS emoji 合并 | 后端那半已修（`4bb8aff7`，S3-G7）；**剩余竞态在外部前端仓** | 前端仓修 |
| **KB-9** `ConfirmModal` @keydown | 前端遗留（现行为已可靠） | 前端加固批次 |
| **KB-1** `live2d.directive` 无发射点 | `KEEP_COMPAT`（协议已标注客户端容忍缺席） | `PD-8` |

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
