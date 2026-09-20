# 待做（Backlog）

> **本文件是 backlog 的 current source of truth**（2026-09-20 由 **Batch 19 — Long-tail
> Backlog Closure** 重构；上一版结构见 `git show e11619f1:todo.md`）。
>
> 用法：
> 1. **§1 Active** = 真正还值得投入工程时间的项目；每条有稳定 ID、可达性、严重度、证据级别、
>    最终状态与 **Reopen trigger**；
> 2. **§2 Deferred / Unreachable / Accepted** = 有明确触发条件的暂缓项（**不是**"以后看看"）；
> 3. **§3 Product Decisions** = 需要 owner 拍板；**§4 Architecture Debt**；**§5 Future Features**；
>    三者**不与 correctness 混排**（否则"任何未实现的理想状态都会永远看起来像 defect"）；
> 4. **§6 Closed Archive** = 一行索引（FIXED/GUARDED/FALSIFIED/SUPERSEDED/DUPLICATE），
>    逐条证据在 `docs/review/` 与 `docs/completed/`；
> 5. 本文件**只承载当前状态**，不累积过程叙事；历史正文见 §6 索引与 `git show 69f2e5a9:todo.md`。
>
> **口径（§204 的验收标准）**：不追求 `todo.md = 0`，追求 **unknown correctness risk = minimal**。
> `todo.md` 里存在 LONG-TERM 项不等于系统没做完 hardening。

## 0. 词表（统一口径，Batch 19 收敛）

| 维度 | 取值 |
|---|---|
| **Status** | `OPEN` / `GUARDED` / `FIXED` / `FALSIFIED` / `SUPERSEDED_BY <ID>` / `DUPLICATE_OF <ID>` / `SAME_ROOT_ALIAS` / `CURRENTLY_UNREACHABLE` / `LEGAL_TRANSIENT` / `ACCEPTED_RISK` / `PRODUCT_DECISION_REQUIRED` / `ARCHITECTURE_DEBT` / `FUTURE_FEATURE` / `MIGRATION_REQUIRED` / `PARTIALLY_GUARDED` / `DEFERRED_LOW_PRIORITY` / `KEEP_AS_IS` / `OBSERVABILITY_DEBT` / `DOCUMENTED_LIMITATION` / `INSUFFICIENT_EVIDENCE` |
| **Reachability** | `USER` / `DEPLOYMENT` / `ADMIN` / `INTERNAL` / `LEGACY_DATA_ONLY` / `TEST_ONLY` / `CI_ONLY` / `CURRENTLY_UNREACHABLE` |
| **Evidence** | `E3` 真实 runtime/真库/真实 tokenizer；`E2` 定向测试或确定性子进程复现；`E1` 静态控制流证据；`E0` 仅假设 |
| **Severity** | `HIGH` / `MEDIUM` / `LOW` / `INFO`；架构/产品/未来项写 `N/A` |
| **标签** | `TRUTHFULNESS`＝reported truth ≠ actual truth |

> **Batch 19 的一条硬纪律**：`state mismatch` / 代码异味 / "这里没有 guard" **都不是**缺陷证据。
> 合法 transient 只有在造成 omission、duplication、wrong cancellation、liveness failure 或
> truth corruption 时才算 correctness defect（`invariants.md` §4.4/§4.7）。

---

## 1. Active Backlog（真正未解决、且值得投入）

### 1.1 Correctness（4 条）

| ID | 标题 | Reach | Sev | Ev | State | Reopen / 下一步 |
|---|---|---|---|---|---|---|
| **KB-78** | **密集变音符号拉丁文本击穿估算硬下限**（Batch 19 裁决 `IN_CONTRACT`，**不是** misclassified corpus）：`script_latin2_czech/polish` 是**普通拉丁文字**（`other` 占比 ≈0），走**拟合**的 `latin_ext` 类 ⇒ 可低估。**E3 实测（真 Wikipedia 散文 × 本地权威 tokenizer）**：qwen profile 捷克 **0.65–0.68** / 波兰 0.73–0.80；generic 捷克 0.67。根因：`latin_ext` 由 `latin1_accented`（5% 变音密度）标定，而捷克自然文本 12–14%。后果：6144 估算预算可放进 ~9450 真实 token > 8192 窗口 ⇒ Ollama 静默截断 | USER | MEDIUM | **E3**（真散文）+E2（签入语料）| **PARTIALLY_GUARDED**（合同/守卫已修：`UNCOVERED_CONTENT_CLASSES` 单一定义 + `TestKnownLimitations::test_dense_accented_latin_is_a_registered_uncovered_class` 钉住区间 + 验证脚本单独报告缺口；**估算器未改**）| **估算器标定批次**：按既有 LP 流程重标定 `latin_ext`（已验证可行：qwen 0.86→1.18 / generic 0.20→0.56 即可闭合，且合同内 band 不变），把 `latin_accent` 语料转正进合同并重跑 holdout；**必须**与 `_CONTRACT_MAX_RATIO` 的 T 复核同步 |
| **KB-56** | **「加载更多」跨会话串消息**（Batch 19 由 INSUFFICIENT_EVIDENCE **升级为 CONFIRMED-机制**）：回放项不含 `_id` （`session.messages_for_client` 只发 role/parts/content/message_id/timestamp）⇒ 游标恒为空，而 `chatStore.loadMoreMessages` 打的是**全库** `GET /api/messages`（无 `before` ⇒ 全局最新 50 条）。E2 进程内复现：`repo.query(before=None, limit=3)` 返回 `[('a1','A'),('b1','B'),('b2','B')]`（**含 B 会话**）。后果 = 会话真相被污染（别会话的消息出现在当前聊天）；修复点在**前端 store**（改打已存在的 `/api/sessions/{id}/messages`）或后端给全局端点加会话过滤。仍缺的证据：一次浏览器/DOM 级抓取 | USER | MEDIUM | **E2**(19 机制)+E1 | **PARTIALLY_GUARDED**（会话域端点已存在，前端未用）| 修复点很小但属前端改动（Batch 19 前端零变化）；前端批次或专门小修 |
| **F13** | 系统注入文本（唤醒词 / 任务结果）被当作"用户消息"参与 **mood 判定 / 记忆提取 / 反射器**（历史侧已豁免，这三条链没有）——E2：失败任务的唤醒文案让 `_detect_mood` 判出 `sad` | USER | LOW-MED | E1+**E2** | **PARTIALLY_GUARDED**（`CT-T7` 只钉历史侧） | 修复面很小：在 mood/consolidator/reflector 三个消费点用同一 `is_task_result` 判据；**或**统一裁决合成注入语义（见 PD-13） |
| **KB-41** | `_handle_task_result` / `_handle_system_tick` 的抢占**没有 ownership 判据**：后台任务终态唤醒 / 主动搭话 tick 落在 admission 窗口时会抢占**尚未接管输入**的用户回合 ⇒ 已 ACK 输入**静默丢**（E2：`cancelled_pre_ownership`、session 里不存在、唤醒回合多跑 2 次 LLM；用户输入路径 Batch 12 已有判据，这两条没有） | INTERNAL | LOW | **E2**(12)+**E2**(19) | **PARTIALLY_GUARDED** | 给两个 handler 补与 `_handle_user_input` 同一份 `current_turn_owns_input` 判据；窗口很窄（tick 门=相位 IDLE + 1s 轮询；task result 需恰好完成），故触发式 |
| **KB-48** | 输入层两条**直投**入口在停机窗口仍向 bus 投递：`ws_handler.do_transcription`（`:733`）与 `ws_v2._handle_voice_commit`（`:787`）**不查** `is_service_running`（同类入口已按 S2-R2-4 加了）⇒ RO1 清扫**之后**投进的事件会跨 epoch 复活（E2：真 `stop()` 返回后真 `do_transcription` 仍投进总线，`start()` 后被消费进历史） | DEPLOYMENT | LOW | **E2**(14)+**E2**(19) | **PARTIALLY_GUARDED** | 两条入口补同一 `is_service_running` 判据（`voice.commit` 用既有 `_not_accepted_error(agent)` → `AGENT_BUSY`，**零新增 wire 枚举**）|

### 1.2 Deployment / Migration（2 条）

| ID | 标题 | Reach | Sev | Ev | State | Reopen / 下一步 |
|---|---|---|---|---|---|---|
| **GR-24** | `_option_mismatches` **不比对 `partialFilterExpression`**（且 `EXPECTED_INDEX_OPTIONS` 根本不登记该索引）⇒ `character_instances.owner_user_id_1_character_id_1_canonical_1` 若在库里是**普通**唯一索引（同样 `unique:true`）时**不报冲突**，而 `create_index(partialFilterExpression=…)` 必抛 `IndexKeySpecsConflict`（**E3 实测 code=86**）⇒ 仓储 `_db` 永久 None、整个 character_instances 域 503，而启动自检报健康。历史窗口：`0962fb88`(09-04) 建普通唯一 → `c10d3a52`(09-07) 改 partial，无迁移（本地开发库当前正确） | DEPLOYMENT | MEDIUM | **E3**(19) | **ARCHITECTURE_DEBT**（无 required CI 门禁对它变红） | 给「部分索引」建独立登记语义（登记表 + 比对 + 静态守卫 `_OPTION_KWARGS` 补维度）；**不要**自动迁移（§154） |

### 1.3 Guardrail / CI（7 条；其中 GR-4/GR-16 是**已复现的假绿**）

| ID | 标题 | Reach | Sev | Ev | State | Reopen / 下一步 |
|---|---|---|---|---|---|---|
| **GR-4** | R4 eager 环判据**可复现漏报**：合成仓里两个真环（`core/a/__init__→core/b→core.a`、`core/c/__init__→.d→core.c`）**全绿通过**。两个成因：`check_architecture.py:292-293` **整类跳过相对导入**（本仓只允许出现在 `__init__.py`/`__main__.py`，故影响面有限但真实）、`:257-258/:392-395` 把 `X` 与 `X.__init__` 拆成两个节点 | CI_ONLY | MEDIUM | **E2**(19，合成仓复现) | **PARTIALLY_GUARDED**（`test_r4_flags_eager_cycle` 只覆盖绝对导入、非包节点） | 修判据前先补 fixture（相对导入 + 包节点两种形态）；本仓当前 0 环 ⇒ 修完应为绿 |
| **GR-16** | desktop golden **未从 runtime 单源**：`gen_contract_frames.py:242` 的 `tool.request` 携带 `offset/limit`，而 runtime 的 `FileReadParams` 只有 `path`（`plugin.py:64-68`，`:119` 发 `model_dump()`）⇒ 该 payload **不可产生**；`--check` 只做 generator↔archive 自洽 | CI_ONLY | MEDIUM | E1 | **PARTIALLY_GUARDED** | 要么 golden 按真实 `FileReadParams` 重新生成，要么 `FileReadParams` 真加这两字段（需前端契约决策）；`--check` 增一条"golden 参数 ⊆ runtime param model"的交叉断言 |
| **KB-27** | `_AFTER_RESPONSE_DRAIN_TIMEOUT`（`loop.py:54`）以 `drain(drain_timeout=…)` 传入，而 `check_timeout_budget.py` 只认 `wait_for/wait/timeout` + `timeout=` kwarg ⇒ 该预算**对门禁不可见**，门禁却打印"R3 具名预算全部表态" | INTERNAL | LOW | **E2**(19：`--list` 零命中) | **PARTIALLY_GUARDED** | 扩 `_wait_kind`/kwarg 匹配到 `*timeout*` 形参名（保守方向：宁可多要求表态）|
| **KB-32** | 同族：`_turns.drain(timeout=3.0)`（`lifecycle.py:387`，裸字面量）与 `_CHANNEL_SEND_TIMEOUT=5.0`（`output/manager.py:40`）的"外层 3.0 < 内层 5.0"不对称对门禁不可见（正是 S2-A3 那条规则要防的形状） | INTERNAL | LOW | E1 | **ARCHITECTURE_DEBT** | 与 KB-27 同批：门禁要能表达**跨模块**预算（或把这两个常量收口成同一处可登记的形状）|
| **GR-15** | `pytest.ini` 未启用 `--strict-markers`：漏 marker/拼错**分层** marker 已被根 `conftest.py` 钩子挡住，但"合法分层 marker + 拼错的附加 marker"仍静默影响选择 | CI_ONLY | LOW | E1 | **PARTIALLY_GUARDED** | 加 `--strict-markers`（先跑一遍确认无未注册 marker 在用）|
| **GR-1** | 文档数字门禁**硬编码三份**文档（`test_doc_counts.py:43`）⇒ `docs/architecture.md:381` 的「43 个 `@property`」（真值 46）永不被检查 | INTERNAL | LOW | E1 | **PARTIALLY_GUARDED** | 把 `docs/architecture.md` 加进 parametrize（立刻变红 ⇒ 顺手把 43 改成 46）|
| **GR-14** | `docs/testing.md` 档位计数陈旧（自称脚本生成）：实测 Quick=Normal=**6450**、全量 **6551**，文档写 6002/5952 | INTERNAL | LOW | **E2**(19) | **OPEN**（文档） | 下次改 `docs/testing.md` 时用 `make check-test-sets` 的实测数字 |

### 1.4 Observability / Tooling（9 条；不影响 runtime correctness）

| ID | 标题 | Reach | Sev | Ev | State | 说明 / 触发 |
|---|---|---|---|---|---|---|
| **KB-55** | pre-claim 的**通用**失败（`GenerationContext` 校验 / 角色绑定失效）零 disposition（CT-T6b 只钉"无幽灵历史"，不钉归因） | DEPLOYMENT | LOW | E2(19) | **OBSERVABILITY_DEBT** | 与 AD2/KB-24 同族；需要"pre-claim 通用失败"的归因口径 |
| **KB-44** | v1/REST/STT 输入的终态归因身份为空（`_admission_message_id` 来自 v2 预分配 id）⇒ 终态日志 `message_id=-` | INTERNAL | INFO | E1 | **OBSERVABILITY_DEBT** | 需要给 v1/REST 入口一个边界 id（或明确不回填）|
| **KB-60** | `FakeDocumentDBBackend` 保真度缺口：`aggregate()` 恒 `[]`、`$type`/`$nin`/`$all`/`$size`/`$nor` 静默忽略、`$exists` 对显式 null 与真库相反、`limit=0` 语义相反 | INTERNAL | LOW | E1(16) | **OBSERVABILITY_DEBT**（测试基建） | 靠真库（`tests/real_mongo/`）覆盖依赖聚合的路径；新用到的算子在 fake 上必须显式 mock |
| **KB-58** | `LEGACY_DATA_ONLY`：`sessions.updated_at` 历史字符串格式三变（naive 本地 → `+08:00` → UTC），而排序是字典序 ⇒ 跨格式不保时序 | LEGACY_DATA_ONLY | LOW | E1(16)+真库审计 | **CURRENTLY_UNREACHABLE**（真库 7/7 为 `+00:00`，无存量；无任何仓内写者能产出旧形态） | 触发：出现非 UTC/naive 的 `updated_at`（如恢复旧备份）⇒ 症状是**桶内相对顺序错**，不再有不可达 |
| **KB-59** | `rename()` 不更新持久 `updated_at`，而进程内覆盖层把本地键抬到 `now_iso()` ⇒ 本进程排最前、回一个比库里任何值都新的 `updatedAt`，重启后回落（reported ≠ actual） | INTERNAL | LOW | E2(16) | **DOCUMENTED_LIMITATION**（Batch 8 §7 明确要求保住"改名抬升"） | 触发：改名排序语义进产品合同 |
| **KB-63** | `OllamaBackend.probe_runtime_context()`（configured/requested/observed）**无报告面** ⇒ 用户看不到 runtime mismatch | DEPLOYMENT | LOW | E1(17) | **OBSERVABILITY_DEBT** | 触发：产品要展示 runtime 真相时，**附在既有** provider test（用户点击）里，不新增轮询探针 |
| **OBS-5** | 把 stt/tts 的 `model`/`enabled` 补进 `/debug/api`（需 `make gen-types`）——F7 只覆盖标志语义，字段补齐这半没有条目跟踪 | ADMIN | LOW | E1 | **OBSERVABILITY_DEBT** | 与 debug 面板批次一起做 |
| **OBS-6** | 外部删除权威身份档后（缓存仍热）`PUT /api/me` 连报 409「expectedRevision 已过期」而记录**根本不存在**（`details` 为空、无恢复线索），同状态 `GET /api/me` 仍 200 + 陈旧 revision ⇒ 错误码在撒谎 + 恢复路径未闭环（E2，Batch 6 只做到"不伪造 revision"）| USER | LOW | **E2**(6) | **PARTIALLY_GUARDED**（N-3 的元数据权威已修；存在性判定与 404/409 契约未闭环）| 与 E1 freshness 同族；修它需要"存在性"判定 + 404/409 契约决策 |
| **OBS-7** | debug 面板 `GET /debug/api/sessions` 仍读 `list_all()`（有界 ≤200）⇒ 超 200 会话时看不到更早的（USER 面 P2-01r 已修，ADMIN 面无 cursor 参数） | ADMIN | LOW | E2(8) | **OBSERVABILITY_DEBT** | 需要 debug 契约加分页（前端 `SessionPanel.vue` 同步改）|
| **OBS-8** | `finalize._run_after_response` 在 engine 的 `reflect` 是 `MagicMock`/非协程时记 warning ⇒ 污染每条 lifecycle 用例日志（**TEST_ONLY**，生产不会有非 awaitable）| TEST_ONLY | INFO | E1 | **KEEP_AS_IS** | 触发：生产路径把非 awaitable 交给 `reflect_task` |

---

## 2. Deferred / Unreachable / Accepted / Documented（有触发条件，**不排期**）

| ID | 为什么不排 | 重开条件 |
|---|---|---|
| **KB-29** | pre-claim **异常**臂对上一回合遗留的 `IDLE_BACKGROUND_TASK` 调 `_handle_interrupt()` ⇒ 假 `interrupted`+`idle` 帧（取消臂已由 ownership 判据挡住）。E2 已确证 INTERNAL/LOW | 该异常路径补上 ownership 判据，或有消费者不能接受假 IDLE |
| **KB-49** | 同族（desktop 面）：`_cancel_desktop_calls()` 无回合归属 ⇒ pre-ownership 收口会取消**上一回合**在途 desktop 调用 | 出现 desktop 桥 + 在途调用 + pre-commit 失败同时发生 |
| **KB-50** | 非 `USER_INPUT` 事件跨 epoch 存活：陈旧 `InterruptData` 会把新 epoch 刚投进的事件一起清掉（当前无生产实例）；残留 `StateChangeData`/`SystemTickData` 会被新 epoch 消费（E2）。属 AR-8 父项 | 引入非 USER_INPUT 的 epoch hygiene，或生产出现"就地 stop→start + 活跃客户端" |
| **KB-51** | 中途取消的回合：内存/archive 有内容而 `sessions` 文档没有 ⇒ 硬杀后该回合从 LLM 上下文消失。CT1 明确不含 crash durability | crash durability 成为产品要求，或出现从 `message_archive` 回填会话的消费者 |
| **KB-52** | 合成注入的会话语义不一致：任务唤醒不写 user 历史、主动搭话写（与 F13 方向相反，CT-T7 按条数钉住） | 与 F13/PD-13 一起裁决 |
| **KB-57** | 跨回合同 `message_id`/`clientRequestId` 重放会得到两条历史（幂等 stale ⇒ TAKEOVER）；"合法重发"与"重放"在 wire 上不可区分。**证据级别更正（Batch 19）**：旧记 `E1+E2(15 子代理复现)` **不成立**——Batch 15 记录只是推断，无探针产物 ⇒ 实际为 **E1（机制）/ 运行期 UNPROVEN** | 产品裁决重放语义（PD-14），或一次运行期双准入复现 |
| **KB-61** | `num_ctx` 的**上限夹取在 Ollama 侧**（0.24.0 实测 100000000→32768）；Elysia 不新增 provider 版本相关的硬编码上限 | 声明窗口高于该夹取上限的真实部署；或 KB-63 有诊断面 |
| **KB-62 / AR-18** | **双协议并存**：native（生成，带 `num_ctx`）与 compat 侧车（工作流，SDK 形状）语义/窗口不同；compat 默认 4096 会让 warm runner 降档重载（E3 实测 ~2.3s/步） | workflow/task LLM 改走 generic backend 接口（AR-4/AR-5）后**删除侧车** |
| **KB-64** | `Module/llm/__main__.py` 集成 smoke 只覆盖 deepseek/openai 兼容路径；ollama native 的真实 smoke 只有 external 档；daemon 默认窗口规则未测（provider-owned） | 升级 Ollama / 换机器时重跑 `test_ollama_real.py` 并记版本与默认值 |
| **KB-65 / KB-72** | **无预算消费者**（同根不同面）：core 侧直连 backend（vision/consolidator/reflector/awareness/compression）+ workflow executor 直连 LLM，都不读 `context_window`、不估算（KB-72 的 30000 字符上游截断是**数据**截断，不是 token 预算） | 某条路径真的超窗（多图 `image_indices`、大整合缓冲、workflow 节点 + 工具 schema）|
| **KB-68** | 协议切换后 SDK 隐式重试消失：流式仍有 `LLMAdapter` 的 3 次（仅 429/5xx 且流未开始），非流式现在 0 次；SDK 的隐式重试**从来不是**仓库文档化的合同 | 把重试策略收口到一处并覆盖非流式调用（retry 策略批次）|
| **KB-69** | `repeat` 类（同字符重复）token 率完全由词表决定，字符统计**无法**闭合（实测区间 [0.58, 5.34]，`TestKnownLimitations` 钉住） | 引入词表级 tokenizer（依赖门槛见 Batch 18 记录）或产品限制单字符重复输入 |
| **KB-70** | provider 侧固定框架（默认注入的 system prompt、tool 样板）观测不到：实测 ≈22–72 token 有界常数（守卫上界 96） | 某 provider 的框架变成非平凡函数（随消息数/工具数增长）|
| **KB-71** | `image_url` 固定计 1024 token（未验证）；同族不一致：`debug/prompt_recorder.py` 按 20 字符计同一张图。多模态链路**可达**（`providers.yaml::llm.mimo.supports_multimodal=true`）但本机无 VLM 真值 | 接入真正 vision provider 并用 provider usage 标定（按分辨率分档）|
| **KB-73 / KB-75** | `profile_for_model` 是**名字子串匹配**、不是权威 metadata：实活误判 `ollama/deepseek-r1:8b`（Llama 蒸馏拿 deepseek profile，实测 1/38 样本 <0.775，最低 0.67）；`switch_model` 接受任意 id、无 allowlist | 有进程内可读的权威家族信号（`ollama show` 的 `architecture` 缓存 / provider 声明 tokenizer family）|
| **KB-74** | 估算合同外的**两条轴**（家族 = generic 保守并集；内容 = `repeat`/`script`/`latin_accent`）无法用有限字符类闭合；`script` 由固定上界覆盖（只许高估，实测最坏高估 **13.9×**）| 语料/系数变更（见 KB-78 的标定批次）|
| **KB-76** | 工具 schema 结构化常量在"参数量多而描述极短"的形状上高估到 1.57×（方向安全；preview 不传 tools ⇒ 无 false reject 路径）| 出现真实的高参数低描述工具集（自动生成的 CRUD），按实测重标定 |
| **KB-2** | LLM 偶不发 `<mood>`：回退链是**设计好的**降级路径（确定性、有用例），omission 是模型合规问题，不产生假状态 | mood 明显错误/冻结 ⇒ 才考虑结构化输出能力 |
| **KB-4** | VAD 易被环境音触发：`aggressiveness=3` 已在两处硬编码，且已有 5 道缓解（最短段/最短语音块/冷却/短丢弃/空转写丢弃）；残余漏触发率是**产品选择**（PD-15）| 用户报告"嘈杂环境里角色自打断"|
| **KB-8** | tokenizer NORMAL 态 `_pending`/`_held` 无上界：增长**按未中断 run**（每个 `\n`/空格/句末/`<`/`（(` 都 flush）、每轮 reset，同文本调用方也已持有 ⇒ 无额外 MB 级驻留；代价是二次拼接（100k 字符 0.78s）| 真实回合出现 >64KB 未中断 run（内联 base64/data-URI），或非 chat 消费者喂无界流 |
| **KB-20** | `_download_and_forward` 的宽 `except` 把输出侧**契约/编程错误**也记成"TTS 失败"（有意为之：SP1/SP2 门控要释放），但遥测无法区分；中途失败会留 stale `_audio_forwarding` 且不发 `audio_end` | "TTS failed" 事件最终追到契约 bug，或客户端卡在无 `audio_end` 的播放上 |
| **KB-11** | `_retarget_default_character` 重试循环读**缓存** revision（冲突路径刻意不刷）⇒ 权威模式下并发改档案会 3 轮重放同一 expected、必然 `RevisionConflictError`（fail-closed、无损坏、假失败）| 两个 agent 进程/`mongosh` 与角色归档写并发（跨进程 409）|
| **KB-19** | `flush_emojis` 并发 pop ⇒ KeyError（形状已复现）：两个调用者都与 live popper 有序（`wait_all` 之后 / drain 完成后；超时分支**不做** flush）| 新的 `flush_emojis` 调用者可能与在途播放 pop 重叠 |
| **KB-10** | TTS 取消 + HTTP 流清理：**FALSIFIED**——per-request client 在 `finally` 关闭，探针实测 `aclose` 1 次、0 泄漏任务（2026-06-22 记录指向重构前的**共享** client）| 重新引入共享/pooled client，或流式消费者被放弃且不 `aclose()` |
| **KB-18** | `_task_by_sid` 保留已完成任务：**FALSIFIED**——每次 `stream_sentence` 都剪枝，48 句/8 回合峰值 5 条 | 移除逐句剪枝 |
| **KB-21** | SPEAKING 语义（"服务端正在输出本轮 response"，与"用户听到什么"无关）已写进 `invariants.md` §4.2 并有守卫用例；缺的只是 wire 契约注释 | 有消费方把 SPEAKING 当"音频播完"（UI 指示/打断策略）|
| **KB-3** | INTERRUPT + TTS emoji 合并残留竞态：后端那半已修（`4bb8aff7`，S3-G7），**剩余在外部前端仓** | 前端仓修（前端批次）|
| **KB-39** | `accepted` 的边界承诺（Transport Accepted + 幂等决策；**不**承诺持久化/一定被回复）写在 Batch 12 记录 §3.2，但**没有**写进消费方可见的 wire 文档 | v2 客户端据 `accepted`/`idempotency_durable` 推导超时/重试策略 |
| **KB-42** | 语音取消的门（`not is_completely_idle`）排除了 admission 窗口：窗口内相位恰是 IDLE ⇒ 不触发取消，但此时**没有 generation 存在**、输入照常前进 ⇒ 无害 | 产品要求"撤回已 accepted 未 claim 的语音输入"|
| **KB-17** | `PATCH /api/sessions/{id}` 的 title **无长度上限**，而 `POST` 限 200、搜索词上限也是 200 ⇒ >200 字标题只能用 ≤200 字搜索词命中（两条写路径契约不对称，**无错误真值**：搜索是转义子串匹配，≤200 字词仍能命中；`SessionSummary.title` 无上限 ⇒ 不会 500）| **PRODUCT_DECISION_REQUIRED**（PD-16）| 触发：出现"改了名却搜不到"的报告 ⇒ 对齐上限 |
| **KB-33** | `finish_turn` 12 组合里 5 组抛 `ValueError`：唯一调用链（`_finalize_turn`→`_transition_idle`）处 FSM ∈ {THINKING, SPEAKING} ⇒ 5 组不可达；矩阵覆盖只存在于 `tmp/`（被 `norecursedirs` 排除）| 出现 `finish_turn` 的新调用者/相位，或补 gated 组合矩阵 |
| **KB-35 / KB-36 / KB-37** | `handle_interrupt` 的 `try` 起点/`finally` 抛异常顶替 `CancelledError`（KB-35）、并发第二调用者重复广播（KB-36）、`_notify` 顺序 best-effort 让后续订阅者收不到（KB-37）——三者都在 `invariants.md` §4.4 落纸，且各自前提（能抛的 `finally`、第二个并发调用者、会挂起的 `on_change` 订阅者）仓内均**不存在** | `finally` 里新增可能抛异常的语句 / 引入第二个并发调用者 / 注册第二个 `on_change` 订阅者 |
| **KB-30** | ~~回合级多写入无 rollback~~：**FALSIFIED**（E2：回合级写入都在最后一个 pre-claim await **之后**，post-claim 取消会清 `_clock`，`_session` 未被污染 ⇒ 没有需要回滚的状态）| 在 `_prepare_turn` 最后一个挂起点**之前**新增回合级写入 |
| **KB-45** | ~~`_handle_user_input` 抢占分支不可达~~：**FALSIFIED**（E2：直投 barge-in 正常走到该分支，历史/归因完整）| 直投入口开始按相位门控（该分支可能重新变死代码）|
| **KB-26 / KB-54** | `_current_task` 陈旧句柄：5 个（KB-54 补第 6 个：`ws_handler.py:522` 读 generation ctx，守卫是**连接归属**而非 `.done()`）读者全部良性；ownership truth 已由 `_generation_ctx` + `_started_cognitive_tasks` 接管 | 出现**无** `.done()`/连接归属守卫的新读者 |
| **KB-28** | `IDLE 已广播 / 锁未释放`：主形态已被 Batch 12/13 判据覆盖；残留的"严格两发停止静默丢失"需要仓内**不存在**的挂起 `on_change` 订阅者 | 引入第二个 `on_change` 订阅者或让 `_notify` 可挂起 |
| **KB-34** | 第二发取消落在 `bridge.pending_calls()` 上时在途 desktop 调用不会被逐条 `cancel_call`（§4.4 的 best-effort 分层，明确不修）| 出现运行期 sweep，或残留 pending 调用对用户可见 |
| **F16** | `terminal_payload.get(k, default)` 在值为 `null` 时不生效 | 出现 `null` 值载荷 |
| **F12** | 失败细节长度上界（现文本是引擎编写的短诊断串）| 某 provider 回显长文本 |
| **RB-1** | **FALSIFIED as a risk**（Batch 19）：三个字段都是**只写不读**（`sha256` 不在 `AttachmentInfo`；`delivered_at` 只写一处；`metadata.incomplete` 由 finalize 写、读取按 dict 默认值）⇒ 无"旧记录不可读"风险 | 触发：出现需要这三个字段的读者（完整性校验 / UI 展示不完整）|
| **N-12** | 存量文档 `pronouns: ""` 会让 `GET /api/me` 500（**CURRENTLY_UNREACHABLE**：PUT 与响应契约都拒空串，仓内无写入者）| 出现写入者或存量数据出现空串 |
| **N-2** | `ProfileStore._load_from_file` 读回失败会**重新 bootstrap**（静默重置）：**仅本地/单测模式**可达（生产装配恒定权威 repo；前半 `_update_local` 绕过 pydantic 已由 `fb438167` 关闭）| 本地模式被产品化 / `_load_from_file` 路径进入生产 |
| **KB-14** | `core/tests/fake_llm.py` 的 `fault="http_429"/"http_503"` 路径 import 不存在的 `Module/llm/errors.py`（唯一 fault 调用者不用这两个值 ⇒ CURRENTLY_UNREACHABLE）| 第一个使用这两个 fault 值的测试 |
| **GR-2 / GR-5 / GR-6 / GR-7 / GR-8 / GR-9 / GR-10 / GR-11 / GR-12 / GR-13 / GR-17 / GR-18 / GR-21 / GR-22 / GR-23 / GR-25** | 见 [Batch 19 执行记录](docs/review/2026-09-20-Batch19-long-tail-backlog-closure-执行记录.md) 的逐条裁决。要点：GR-2 弱用例（`test_minimax_key_present_passes` 随 ambient `.env` 走）/GR-5 私有穿透同名遮蔽（KEEP_AS_IS，回归网非完备审计）/GR-6 扫描面不含 `scripts/`·`frontend_contract/`/GR-7 非字面量私有访问不可见/GR-8 R6 条数漂移分支零覆盖（fixture 里 `entry` 恒 None）/GR-9 RUF100 未启用（1069 处 `# noqa`；同族观察：bandit 的 B608 抑制当前**惰性**）/GR-10 CI 未扫 `env:`/`with:`/`working-directory:`（**CURRENTLY_UNREACHABLE**：当前无 PR 可达 job）/GR-11 清空形态长尾（0 实例，KEEP_AS_IS）/GR-12 `if:` 守卫是 token 存在性（保守方向，KEEP_AS_IS）/GR-13 `print_test_stats.py` 不查返回码（非门禁，KEEP_AS_IS）/GR-17 `use_mocks` 死支路（7/7 调用点显式 False，**默认值仍是 True**）/GR-18 3 个 0 字节测试包/GR-21 token 结构 4 项低残留/GR-22 前端遗留（a11y/CSP/Worklet，属前端仓）/GR-23 `# type: ignore` 60 处（旧记 23）/GR-25 测试鉴别力（① 无 repo 模式覆盖了权威模式走不到的路径） | 各自触发：弱用例改为 hermetic / 首次跨包私有访问 / 新私有访问形态 / 新增 fixture 语义测试 / `# noqa` 审计批次 / 重新开启 PR CI / 新调用方省略 kwarg / 动该测试树 / 观察到重复 token 行 / 前端批次 / ignore 掩盖真实类型错误 / 补权威模式冲突用例 |
 见 `docs/review/2026-09-20-Batch19-long-tail-backlog-closure-执行记录.md` 的逐条裁决：R6 条数漂移分支零覆盖（fixture 里 `entry` 恒 None）、`use_mocks` 死支路（7/7 调用点显式 False，默认值仍是 True）、3 个空测试包、token 结构 4 项低残留、`# type: ignore` 60 处（旧记 23）、RUF100（1069 处 `# noqa`）、前端遗留（a11y/CSP/Worklet，属前端仓）| 各自触发：新增 fixture 语义测试 / 新调用方省略 kwarg / 动该测试树 / 观察到重复 token 行 / ignore 掩盖真实类型错误 / 私有穿透纪律迁进 ruff / 前端批次 |
| **OBS-1** | 间歇性 xdist worker 死亡（8 轮 soak 中 1 次且被并发改动污染）| 复现一次干净环境下的 worker 死亡 |
| **OBS-2** | `context_window` 六家 provider 数值是**估算值**：其"只影响截断时机、不影响正确性"的从句经 Batch 19 复核 **FALSIFIED**（Ollama 侧该值已是真实 `num_ctx` 请求且是预算权威）| **SUPERSEDED_BY KB-13**；触发：声明窗口内的请求被 provider 400/静默截断 |
| **OBS-3 / OBS-4** | 均 **FALSIFIED**（Batch 19）：`openai_tts.test()` 已走真实 `GET /health`；`consolidator` 持 adapter 逐次 `think()`（无冻结 raw client），Batch 1.6 §12#6 的实例早在 `_lifecycle_task_system.py:225-249` 修掉，`get_raw_client()` 生产零调用 | 触发：出现无 `GET /health` 的 TTS provider / 新的长期持有 raw client 者 |
| **F18r** | Mongo 补投对坏文档：永久 `delivered=False` + 每次启动告警，无人工清理入口 | 出现坏文档（给一条 CLI 清理入口）|
| **DEP-5** | `config.yaml::database.milvus_token` 带 `secret=True` ⇒ loader 显式跳过 YAML，**该行永不生效**（真凭据只走 env `MILVUS_TOKEN`）| **DOCUMENTED_LIMITATION**（loader docstring + YAML 注释已写明）；触发：运维试图用该 YAML 行启用 Milvus 认证 ⇒ 顺手改成占位注释 |
| **DEP-2** | 无数据库迁移机制（新增集合/索引由 `ensure_index` 覆盖；删/改索引只能人工 `drop_index`）——发生频率低、影响单集合 | 需要"删/改已有索引"的部署 |
| **DEP-6** | **Partially FALSIFIED**：每个依赖都有上界 + `make check-env-lock` 门禁；只剩 uv 迁移未做（**DEFERRED_LOW_PRIORITY**）| 依赖治理批次 / 门禁出现漂移 |
| **SEC-1** | 真实 DeepSeek key 仍在公开 remote 历史里且**有效**（owner 裁定暂不处理；最低成本止血=只轮换 ≈10 分钟）| owner 决定 |
| **SEC-2** | bandit Low 19 项已知基线（B603/B607 17 + B112 2）；门禁判据是 Medium+ 为 0 | 出现新的 Low 或规则变更 |
| **DEP-7 / DEP-8 / DEP-9** | preflight truthfulness 残留（**都 fail-safe，无假绿**）：DEP-7 `sensevoice` 假 WARN（合法无地址，真默认值在代码里；**OBSERVABILITY_DEBT**）；DEP-8 model 级假 PASS（`tts.model` 音色不存在 ⇒ 预检 PASS 但启动**响亮**失败；**OBSERVABILITY_DEBT**）；DEP-9 缺 SDK 时 `run()` 裸 `ImportError`（`[FAIL]` 已先打印，只是丢了汇总；**DEFERRED_LOW_PRIORITY**）。根：DEP-7+DEP-8 同机制（preflight 自己重推可用性而不问运行期权威），DEP-9 独立 | DEP-8：model 级解析 API 出现时；DEP-7：运维按 WARN 行动时 |
| **F5 / N-6 / N-7 / F5d** | **F5 = OBSERVABILITY_DEBT**（debug 只报主模型，task/workflow 模型无面）；**N-6/N-7 = DEFERRED_LOW_PRIORITY**（`invariants.md:1210` 陈旧行；`clean_db.py` docstring 自称有守卫而守卫不读该常量）；**F5d = FALSIFIED**（`docs/database-storage.md` 已覆盖存储/GC/删除/TTL/restart，`README.md:309` 已链接）| F5：运维需要"哪个模型跑了我的后台任务"；N-6/N-7：顺手维护；F5d：— |
| **F2 / FEAT-1** | reminder **无持久化**（进程重启即丢，用户已收到"已设置提醒"）——修法是 feature 形状（durable store + 启动恢复）| 见 §5 FEAT-1 |
| **DEP-3** | `scripts/clean_db.py` 清 **v1** `long_term_memory`，而生产写 v2 ⇒ 从不触碰真实记忆（代码内标注"勿擅自改"）| PD-7 |
| **DEP-4** | S2-H9 的**写侧**长度上限未做（owner 选的是运行期截历史 + 定预算）| PD-11 |
| **P2-02r** | **PARTIALLY_GUARDED / 多半 FALSIFIED**：附件已有 ≤20 的契约上限 + 16MB 帧 / 64MB body 传输上限；残余仅 `content`/ID 无 `max_length` | 触发：新增无界契约字段，或传输上限被移除 |
| **KB-5** | `_execute_tool` 的 60s 超时手工场景 | ≡ **GR-19**（已被 `test_tool_retry_idempotency.py` 覆盖 ⇒ FALSIFIED）|

---

## 3. Product Decisions（需 owner 拍板）

| ID | 决策点 | 阻塞谁 | 现状 |
|---|---|---|---|
| **PD-4** | 审批流是否启用？（`WAITING_APPROVAL` live-lock 只在 Evaluator v2 产出 `ASK_APPROVAL` 后可达；`core/hitl.py` 自陈无触发路径，E1 已复核：evaluator 只返回 CONTINUE/SKIP/RETRY/REPLAN/ABORT）| F1（**不是当前 bug**）| 能力已备、未接线 |
| **PD-5** | `plan_too_large` 语义 + 新契约枚举值（debug/task 摘要面用；`SubmitRejectionCode.PLAN_TOO_LARGE` 是另一个无生产者成员、已在册预留）| F3 | 需契约可达性登记 |
| **PD-6** | 可选节点失败的任务终态语义（今天仍 `COMPLETED`，但**同一文案里已列出失败节点**；engine 语义与措辞均有用例钉住）| F17 | 需终态定义 |
| **PD-7** | `clean_db.py` 破坏范围：真清 v2（危险）vs 只清 v1（无用）| DEP-3 | 代码内已标注 |
| **PD-8** | `live2d.directive` 是否接线（全链路就绪、`core/` 零发射点，协议已标注客户端需容忍缺席）| FEAT-2 / KB-1 | `KEEP_COMPAT` |
| **PD-9** | 任务中心（TaskSystem）是否恢复？（2026-08-30 暂缓，恢复激励点已写明）| FEAT-16 | 暂缓 |
| **PD-10** | 两套 CLI driver 去留（`run_repl` 0 代码消费者但 5 处文档承诺 vs `app.py::run_interactive` 真实入口，行为已分岔）| AR-12 | 8→5 处文档承诺 |
| **PD-11** | 记忆/工具参数的**写侧** `max_length` 是否加 | DEP-4 | 只做了运行期截断 |
| **PD-13** | **合成注入的会话语义**：任务唤醒不写 user 历史、主动搭话写、mood/记忆侧都把唤醒文案当"用户说的"（F13 + KB-52 同根）| F13 / KB-52 | 需统一裁决 |
| **PD-14** | **跨回合重放语义**：同 `clientRequestId`/`message_id` 的"合法重发"与"重放"在 wire 上不可区分（KB-57 会产生两条历史）| KB-57 | 需 wire 决策 |
| **PD-15** | VAD 灵敏度策略（`aggressiveness` 已满 + 5 道缓解；残余漏触发率是产品选择：降噪 / 能量门 / 开关）| KB-4 | 需产品选择 |
| **PD-16** | session title 长度上限是否对齐（`PATCH` 无上限 vs `POST`/搜索 200）| KB-17 | 形式确认即可 |
| **PD-3** | 那 4 条 `unique` 是否纳入 schema drift enforcement（`check_schema_drift` 从不 raise ⇒ 无"启动变红"风险；真正需要独立设计的是 GR-24 的部分索引语义）| GR-3 / GR-24 | 降级为形式确认 |

**已裁决**：PD-1（`/ready` 探 `is_available`，fail-closed 降级运行，Batch 4 落纸 §3.1）、
PD-2（409 报**权威库现值**，未知则省略，Batch 6 `616e4993`）、PD-12（`context_window` =
**effective runtime window**，Batch 17，Ollama 落实为 `num_ctx`）。

### 客户端终态 / 容量（仍开放，需 wire 决策）

| ID | 决策点 | 现状 |
|---|---|---|
| **KB-40** | 客户端没有"accepted 但从未处理"的终态：Electron 在 ACK 上就 settle、WebUI 不消费 ack；服务端终态只进日志 ⇒ 用户无法区分"被取消"与"从未受理" | Batch 12 刻意不新增 wire 帧；需产品决策（MEDIUM/USER）|
| **KB-43** | post-ACK 容量竞争的处置无产品定义：`_handle_user_input` 排队分支满时释放 work 后**丢弃**（有 WARNING 归因，无 rejected 帧）| 需容量策略（优先插回 / rejected 帧）|
| **KB-53** | wire/UI 没有"已取消/不完整"表示：archive 有 `metadata.incomplete`、REST 也带，但 WS 回放不投影、契约 `completeness` 是 `Literal["complete"]` ⇒ 刷新后半截回复与完整回复无异 | 需契约决策（EMPTY/新增字段）|

---

## 4. Architecture Debt（无当前 bug，未来扩展风险显著）

| ID | 维护风险 | 重开条件 |
|---|---|---|
| **AR-4 / AR-5** | provider portability（F10）：切换 provider 的运行时能力未成体系；`AR-5` 是它的实例（`TASK_SYSTEM_MODEL` 是裸模型名，连接参数按主模型解析 ⇒ 工作流不能用另一个 provider）| 部署需要 task LLM 在另一个 provider 上（需契约决策）|
| **AR-8** | EventBus lifecycle epoch（父项）：首个具体实例（`USER_INPUT`）已修+守卫；剩余面见 KB-50 | 生产出现"就地 stop→start + 活跃客户端"，或引入非 USER_INPUT 的 epoch hygiene |
| **AR-9** | 模块级 import 期 `ProjectPath.resolve()` 常量 9 处；已证一个实例有害（`image_source` 读路径对测试重定向静默无效，N-8 守卫不含它）| 出现 N-8 守卫之外的运行期目录重定向需求 |
| **AR-15** | 两个一次性 `.env` 加载器（`Module/_common/env.py` 钉 `ProjectPath.DOTENV`/`override=False` vs `Module/config/loader.py` 裸 `load_dotenv()` cwd 搜索）| 第三个入口点需要 .env，或跨 cwd 运行脚本需加载仓库 .env |
| **AR-17** | `_local_summaries`（进程内会话摘要覆盖层）无淘汰无上限（代价 = 本进程建/改名过的会话数）| 单进程长期运行 + 大量建会话 |
| **AR-19** | `Module/config/preflight.py` 两处 `if provider == "ollama"`（凭据/连通性），而 `providers.yaml` 已有 `local: true` 元数据可替代 | 第二个本地 provider / 带认证的远程 Ollama 网关需要同一规则 |
| **KB-67** | `context_window` 是 **provider 级**却作用于该 provider 下任意模型 id（`switch_llm` 无 allowlist）；Batch 17 起它**真的**成为 runtime 分配（`num_ctx`）| `models:` 支持 `{name: window}` 映射并让**两侧**读同一条目 |
| **AR-3 / AR-13** | 事件消费块 mixin 抽取、`handle_json_message` 进一步拆分（都属可选打磨，非阻塞）| 下次因功能原因改动对应代码时 |
| **AR-6** | error protocol 大扩展（结构化下传 `error_type`，当前仅 tool 结果有）| 客户端需要按 `error_type` 分支处理非工具错误（协议 v3）|
| **AR-1 / AR-2 / AR-10 / AR-11 / AR-14** | `ToolAdapter` 4 个零消费者转发成员、`OutputManager._broadcast` 2 处穿透、`Module/tool` 反向依赖残留、F11 accepted→consumed 窗口（已接受的 at-least-once 边界）、`ConfigLoader(env_path=…)` 无生产调用方 | 公开面收窄专项 / 产品决策 |
| **DEP-6 部分** | 见 §2 |

---

## 5. Future Features（**不与 correctness 混排**）

| ID | 功能 | 现状 |
|---|---|---|
| **FEAT-1 / F2** | 提醒持久化（durable store + 启动恢复）| 未做 |
| **FEAT-2 / KB-1** | Live2D directive 接线（契约/发送链路/门禁/单测全就绪，`core/` 0 发射点）| 待 PD-8 |
| **FEAT-3** | 多模态：AI→用户图片输出（`ToolResult.images` 已设计）/ `type:"file"` / `VideoPart`·`AudioPart` 全链路 | Phase 2 |
| **FEAT-4** | 记忆导出/备份（JSON）+ 记忆统计（类型/年龄/poignancy → DebugInfo）| 未做 |
| **FEAT-5** | MacroMemory + MacroReflector（每日日记，需新 Milvus 集合）| 未做 |
| **FEAT-6** | CharacterEngine Phase 2（C3/C7 动态化、L4→L3 汇聚、话题检测触发 C9、模板精简）| 未做 |
| **FEAT-7** | 唤醒词：开关手工验证 + 自定义 .onnx 训练流程 | 实现已完成，验证待做 |
| **FEAT-8** | Docker 化部署（agent + SenseVoice + GPT-SoVits 多阶段构建）| 数据库 compose 已有 |
| **FEAT-9** | 意图路由 + 渐进式工具披露（触发条件：工具 >15 规则分类 / >20 必须分类 / ≥3 次误用）| 评估过，暂不做 |
| **FEAT-10** | F2 代码块格式重新启用（解析器已支持，仅需模板取消注释 + 前端渲染确认）| 一行开关 |
| **FEAT-11** | 性能：时钟补 TTS span + bench 断言进 CI | 未做 |
| **FEAT-12** | MiniMax SSE 真流式 / `t2a_async_v2` 长文本（上次尝试已回退）| 待上游 |
| **FEAT-13** | poignancy 时间衰减 | 未做 |
| **FEAT-14** | 角色切换（需独立 spec）+ 多角色 prompt 模板 | 评估过 |
| **FEAT-15** | 页面加载优化（Live2D iframe 481KB 竞争主线程 → WS onopen 延迟 ~200ms）| 有开关可缓解 |
| **FEAT-16 / PD-9** | 任务中心 TaskSystem（三实体 + A~E 分批）| 暂缓 |
| **FEAT-17** | 多前端支持（Qt 等）：YAGNI，触发条件=确定做第二前端 | 记录 |
| **FEAT-18** | README 截图（聊天页/Debug/启动流程）| 未做 |
| **FEAT-19** | 前端 Live2D 集成（Cubism SDK v5 需 TS+npm+bundler；当前仅 SDK 文件到位、默认关闭）| 未做（前端批）|
| **AR-4 / AR-6** | provider portability / error protocol 扩展（同时列在 §4 架构债）| 触发式 |

---

## 6. Closed / Superseded / Falsified Archive（一行索引）

> ✅ = 本批（Batch 19）关闭/重分类。详细证据在对应 `docs/review/` 执行记录。

| ID | 一句话 | 关闭于 |
|---|---|---|
| **KB-77** | ✅ ReAct 第 2..N 轮**完全不在预算内**：首轮 6123 ≤ 6144，追加一条接近单条上限（2000 token）的工具结果后第 2 轮 **8205 > 8192 窗口**（E2；真实 tokenizer 交叉验证 9056–10231），而 Ollama 超窗是 200+静默截断 ⇒ 人设区静默劣化 | **FIXED** `Batch 19`：`core/prompt/message_builder.py::enforce_round_budget`（三档：丢历史 → 整组丢较早往返 → 截最新一组 tool 内容）+ `_think_loop` 每轮调用；不变量 **TE9**；守卫 `core/cognitive/tests/test_react_round_budget.py`(7) + `core/prompt/tests/test_round_budget.py`(9)；修前 2 红、**variant M1–M5 全杀** |
| **KB-79** | ✅ 维护者工具 `validate_families.py` **全部家族 SKIP 时仍 exit 0**（无网络 = 看起来"通过"）| **FIXED** `Batch 19`：0 个家族产出结论 ⇒ **exit 2 拒答**；未覆盖 ⇒ exit 1；守卫 `scripts/tests/test_validate_families_exit.py`(6) |
| **KB-80** | ✅ `validate_families.py` 的过滤条件与 CI 合同不一致（`!= "repeat"` vs `{"repeat","script"}`）⇒ 对合同**故意排除**的样本报 `NOT COVERED`（误导性假红）；且 Latin-2 的真实击穿靠 `script` 大类躲过硬下限 | **FIXED** `Batch 19`：`budget.UNCOVERED_CONTENT_CLASSES` **单一定义**（合同测试与验证脚本共用）+ `latin_accent` 单独报告（对齐过滤 ≠ 藏缺口）；守卫同上 |
| **KB-81** | ✅ 旧 profile 名残留（`cjk_opt`）：可执行 0 处、**全在文档**（`invariants.md`、Batch 18 记录、验证脚本 docstring）| **FIXED** `Batch 19`（docs）：`invariants.md` 改为 6 profile/11 字符类/T 指向代码表 + 新增 TE9；数字更正（高估上界 13.9×、repeat 区间）|
| **KB-78** | ✅ **新登记（Batch 19）**：密集变音符号拉丁文本击穿硬下限 ⇒ 见 §1.1（Active）| 登记并加守卫；估算器标定批次修 |
| **KB-30** | ✅ 回合级多写入无 rollback | **FALSIFIED**（E2，Batch 19）：写入都在最后一个 pre-claim await 之后，post-claim 取消清 `_clock` |
| **KB-45** | ✅ `_handle_user_input` 抢占分支不可达 | **FALSIFIED**（E2，Batch 19）：直投 barge-in 正常走该分支 |
| **AR-7** | ✅ 包级伪环（`prompt_assembler` + `consolidator`）| **FALSIFIED**（E1/AST，Batch 19）：`core.memory → core.prompt` 边**根本不存在**（MemoryLLM Protocol 已消除）|
| **GR-19** | 测试覆盖缺口（audio_buffer/server/console/并发/DescribeImage/OutputManager 隔离/KB-5 工具超时）| **FALSIFIED**（Batch 19，E1/E2）：各缺口已被后续批次补上；**KB-5 → DUPLICATE_OF GR-19 → FALSIFIED** |
| **GR-20** | `test_phase2/phase3` 裸 `setdefault(DEEPSEEK_API_KEY)` 污染进程 env | **FALSIFIED**（Batch 19）：已改 autouse+monkeypatch fixture |
| **KB-10** | TTS 取消 + HTTP 流清理泄漏 | **FALSIFIED**（Batch 19，E2）：per-request client 在 finally 关闭，0 泄漏 |
| **KB-18** | `_task_by_sid` 保留已完成任务 | **FALSIFIED**（Batch 19，E2）：逐句剪枝，峰值 5 |
| **KB-2** | LLM 偶不发 `<mood>` | **KEEP_AS_IS**（设计好的降级链，有用例）|
| **KB-19 / KB-33 / KB-35 / KB-36 / KB-37 / F1 / KB-42 / KB-28** | 形状真实但前提仓内不存在（并发/挂起订阅者/相位）| **CURRENTLY_UNREACHABLE**（E1/E2，Batch 19）；各自 reopen trigger 见 §2 |
| **KB-34** | 第二发取消 + 在途 desktop 调用 | **ACCEPTED_RISK**（§4.4 best-effort 分层）|
| **KB-8** | tokenizer `_pending`/`_held` 无上界 | **KEEP_AS_IS**（增长按未中断 run、每轮 reset、调用方已持有）|
| **KB-51 / KB-59 / KB-21 / KB-39** | 崩溃持久化 / 改名抬升 / SPEAKING 语义 / accepted 边界 | **DOCUMENTED_LIMITATION**（合同已落纸，见 §2）|
| **KB-20 / KB-44 / KB-55 / KB-60 / KB-63 / OBS-5 / OBS-7 / OBS-8** | 诊断/可观测缺口 | **OBSERVABILITY_DEBT**（§1.4 / §2）|
| **KB-61 / KB-68 / KB-76 / KB-69 / KB-70 / KB-71 / KB-73 / KB-74 / KB-75** | 估算/provider 的**已知边界**（合同外或方向安全）| **ACCEPTED_RISK / DOCUMENTED_LIMITATION**（§2，各自触发条件）|
| **AR-16** | Ollama Provider Adapter（`KB-13` 的架构出口）| **CLOSED** `11755751`（Batch 17；不变量 OL1–OL6）|
| **KB-13** | Ollama runtime context authority mismatch（声明 8192 ≠ 服务窗口）| **GUARDED** `11755751`（Batch 17；configured=requested=observed=8192）|
| **KB-15** | 缺/null/非字符串 `updated_at` 在 keyset 下不可达 + 游标毒化 | **GUARDED** `Batch 16`（真库 E3）|
| **KB-16** | `sessions.session_id` 无唯一约束 ⇒ 并发 upsert 产出同 ID 两篇 | **GUARDED** `Batch 16`（受守卫的索引升级 + 冲突收敛）|
| **KB-12** | prompt 估算器双向误差带 + `tools` 完全不在估算内 | **GUARDED** `Batch 18`（6 profile × 11 字符类 + 语料合同；残余登记 KB-69..KB-77）|
| **KB-46 / KB-47 / KB-38 / KB-31 / KB-24 / KB-25 / KB-22 / KB-23** | claim→record 缺口 / 停机复活 / pending 乱序 / think 路径异常收口被取消 / admission 静默丢失 / 收口链被第二发取消 / THINKING 永停 | **GUARDED**（Batch 10–15；不变量 TH/CF/AD/PO/RO/CT + 各自守卫）|
| **KB-6 / KB-9 / KB-32(旧) / S2-G3** | TTS `wait_all()` 无超时 ⇒ FSM 卡 SPEAKING（三条挂起源）| **GUARDED** `Batch 9`（`fix(tts): guarantee speaking lifecycle termination on pre-start cancel`）|
| **F8** | 预览超声明窗口照发 ⇒ 400 折成可重试 503 / 静默截断 | **GUARDED** `558503be`（Batch 7；PW1–PW6）|
| **P2-01r** | 会话列表 `list_all(200)` ⇒ 第 201+ 条不可分页/搜索 | **GUARDED** `36cb7cec`（Batch 8；PG1–PG6；另一半 SUPERSEDED）|
| **N-1 / N-11** | `/ready` 探 ping 假绿 + 装载失败无可查状态 | **GUARDED** `82b80e2e` + `fc648e20`（Batch 4；PD-1 落纸 §3.1）|
| **N-3** | 409 `details.currentRevision` 读陈旧缓存 ⇒ 谎报 | **GUARDED** `616e4993`（Batch 6；CM1–CM5）|
| **N-10 / F6 / F7** | provider 能力假绿 / provider-test 缺省模型读 config / `/debug` 面板标志语义混用 | **GUARDED** `c70312de` + `7ef3202b` + `87cc7e38`（Batch 5）；F7 的"存在性"半 **FALSE_POSITIVE** |
| **N-4 ≡ N-13 / N-8(guard) / B3-CB-1…B3-CB-7** | 索引选项表守卫 / `_IMAGES_DIR` 隔离守卫 / 早期 correctness 批次（B3-CB-1 `pronouns:null` 写读不一致 · `B3-CB-2` `idempotency_claims` 键永不生效 · `B3-CB-3` preflight 把可用配置判死 · `B3-CB-4` `PROTECTED_COLLECTIONS` 自称有守卫 · `B3-CB-5` 文档门禁漏根 README · `B3-CB-6` 两份 `UPDATABLE_FIELDS` · `B3-CB-7` `_IMAGES_DIR` import 期固化）| **GUARDED / FIXED / ALREADY_FIXED / FALSE_POSITIVE / KEEP_AS_IS**（Batch 3.x；见旧 §5）|
| **M19 / M20** | resume 后 pause 被销毁 / cancel 规划期不推终态 | FIXED（E2/E3，`bb69485`…`5aa7368`）|
| **F9 / F1(旧) / 其它基础设施** | Batch 2 的 F401/CI/privacy guard；`LoggerFactory`；空 LLM 响应；`flush_after_response` 超时层不匹配；STT 运行时切换/env；WS Bearer；`.env` 模板；`memory_repo.clear_all` 守卫；后台协程所有权 43 站点；`Module/llm/registry` 反向依赖；input 层拆分；`llm_adapter.close` drain；token 统一结构；两个偶发 external 用例；`Module/tool/safety.py` 不拆；Claude 原生持久记忆 | FIXED / CLOSED（见旧 §5 与 `docs/review/archive/2026-08/*`）|

---

## 7. 判例：不要把下列问题登记成后端缺陷（前端/其他仓的适配面）

1. `generation.segment.completed.content` 是后端**权威完整文本**；inner voice / TTS 场景允许零 delta。
2. `generation.completed.conversationUsage` 的真实定义是 `{total, byModel}`；前端不应虚构会话累计 `input/output`。
3. raw wire 用 `type/delta/mood/generationId`，领域层用 `event/text/emotion/responseId`——属 Main Adapter 的正常改名职责。
4. Desktop bridge 的 `tool.request/tool.status` 安全元数据、状态机与重连交叠精确投递已实现并有专项测试；缺口主要在 Electron `ToolResult` 领域接口过窄。

## 8. 决策记录（仍然有效，勿删）

> **任务中心（TaskSystem + TaskService）暂缓不做（2026-08-30）**：依据见
> [`docs/2026-08-29-task-system-overall-architecture-design.md`](docs/2026-08-29-task-system-overall-architecture-design.md)
> 与 [backend 设计](docs/2026-08-29-task-system-backend-architecture-design.md)。暂缓期内不把普通工具调用并入
> WorkflowEngine、不创建产品 TaskRecord、不动现有 wire 契约。恢复激励点：需要可查询/可控/可恢复展示的任务历史时，
> 从批次 A（公共契约 + 持久读模型）启动。→ `FEAT-16` / `PD-9`。

> **`core/tool_runtime` 预留能力保留（2026-09-12）**：`depends_on` / `children_ids` / `run_in_background` /
> `progress` / checkpoint 持久化 / `dispatch_background()` / `new_background()` 全部保留（评估稿的删除计划作废）。

> **剩余桩代码一律保留（2026-09-12）**：「零引用 ≠ 该死」。保留项统一带可 grep 标记 **`P3-01-RESERVED`**。
> 清单见 [`docs/architecture/dead-code-review-checklist.md`](docs/architecture/dead-code-review-checklist.md) §9。

> **CI / 测试分层**：`external` marker 取代 `--ignore`；`test-quick`/`test-normal` 不再跳过任何目录；
> `real_mongo` 走容器（push-only + self-hosted）；`make test-full` 无自动执行点（见 `docs/ci.md`）。
> **本地最终 battery 必须含 `make audit`**（Batch 18 follow-up 的教训；Batch 19 已把它写进验收清单）。

> **Batch 19 的 Keep-As-Is 清单（owner 确认，勿因边角 case 重开）**：EventBus / FSM / `_process_lock` /
> CognitiveLoop / PromptAssembler / SessionRepository 架构 / LLMAdapter·Factory / OllamaBackend /
> `contextWindow` authority / TokenEstimator authority / 0.75 context headroom / push-only trusted
> self-hosted CI / no durable inbox / no generic transaction framework / no automatic tokenizer download /
> no runtime token-count provider call。

## 9. ID 与编号漂移（**读旧文档必看**）

| 你现在看到 | canonical | 说明 |
|---|---|---|
| 「N-8 = ProjectPath 常量」「N-9 = gemini」「N-10 = 吞异常」「N-11 = 空串 pronouns」「N-12 = 弱用例」（Batch 3.5 §12 的编号）| **N-9 / N-10 / N-11 / N-12 / N-14** | todo.md 在 Batch 3.6 把「B3-CB-7 缺回归守卫」插入为 **N-8**，其后整体 +1 |
| `N-8`（guard gap）| 与 **N-9**（ProjectPath 常量）**不是一回事** | 前者已 GUARDED，后者见 §4 AR-9 |
| `N-13` | ≡ **N-4** | 同一条（表级 AST 守卫缺失），已 GUARDED |
| `F5`（Batch 1.6 用）| ≡ **F6**（Batch 1.8 表里的 provider-test STT F5）| **不要**与 Batch 1.8 的 `F5 = workflow model reporter` 混 |
| `B3-CB-5 / B3-CB-4 / B3-CB-7` 的「残留」| **N-5 / N-7 / N-8(guard)** | 残留已各归其 canonical 条目 |
| `N-15` | ≡ **GR-3**（那 4 条 `unique` 未登记）| 见 §1.3/§3 PD-3；`N-4 ≡ N-13` 是另一条（已 GUARDED）|
| `KB-7` | ≡ **GR-20**（裸 `setdefault` 污染 env）| Batch 19 复核 FALSIFIED |
| `DEP-1` | `.env.example` 落后 9 键 | **ALREADY_FIXED** `67eeab83`（9 键都在模板里，注释形态）|
| `P2-03` / `P2-09` | ≡ **AR-8**（EventBus lifecycle epoch）| 首个实例（USER_INPUT）已修；剩余面见 KB-50 |
| `F-1 / F-2 / F-4`（Batch 6 Challenger 号）| ≡ **GR-25** | 测试鉴别力残留 |
| **AUDIT-A / AUDIT-B / AUDIT-C / AUDIT-D**（Batch 18 audit follow-up 的临时标识）| **KB-80 / KB-78 / KB-79 / KB-81** | Batch 19 按仓库编号规则正式登记（**不要**再使用 A/B/C/D）|
| `KB-*` / `OBS-*` / `DEP-*` / `PD-*` / `AR-*` / `GR-*` / `FEAT-*` | 首次分配于 Batch 4/9+ | 不是给已有 ID 的问题再造 ID |

> ⚠️ **`F` 命名空间比 `N` 更乱**：Batch 1.6/1.7/1.8 各有一套 F1..F18。本文一律以 Batch 1.8 的
> follow-up 表为准；引用旧号必须带来源批次（例如"1.6-F5"）。

---

> 历史正文（决策过程、逐批完成记录、详细证据）按 §6 索引到 `docs/review/`、`docs/completed/`
> 与 `git show 69f2e5a9:todo.md`。**本文件只承载当前状态**，不再累积过程叙事。
