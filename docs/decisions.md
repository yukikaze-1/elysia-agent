# 决策索引（Decisions）

> 文档导航：[项目文档](../README.md) → [docs 索引](README.md) → 本文

本文是**决策索引**：记录**已经定下**的决定，以及本仓历史上用过的**决策 ID 命名空间**。

**它不是**：
- ❌ 第二个 backlog（"还没定的事"在 [`todo.md`](../todo.md)）；
- ❌ 历史 ADR 重写（历史记录留在 `docs/review/` 与 `docs/completed/`，本文只登记指针）。

**职责边界（严格）**：

| 文件 | 承载 |
|---|---|
| [`../todo.md`](../todo.md) §3 | **待裁决**的 `PD-*`（pending product decisions） |
| 本文 | **已裁定**的决定：ID、决定内容、日期、**当前权威**、原始证据 |
| [`review/`](review/) · [`completed/`](completed/) | 当时的决策过程与逐条证据（历史正文） |

> **不 renumber 历史 ID。** 本仓的决策 ID 有多个命名空间（见下），它们各自在自己的
> 语境里被代码注释、执行记录、`git log` 引用。本文只建立**对照**，不统一重编号——
> 重编号会让所有历史引用同时失效。

---

## Pending Product Decisions

**唯一权威是 [`todo.md`](../todo.md) §3**（含 blocker 与现状）。本文不复制它们的状态，
只给一个入口索引：

**只有 ID 与一句话决策点**——状态 / blocker / 下一步**一律不在这里复制**
（那正是"同一事实两处权威"的成因；本轮 Challenger 复核发现 `PD-9` 曾在本文件里
同时以"待裁决"和"已裁定为暂缓"两种身份出现，已按下述方式收口）。

| ID | 决策点（一行） |
|---|---|
| `PD-3` | 那 4 条 `unique` 是否纳入 schema drift enforcement |
| `PD-4` | 审批流是否启用（`WAITING_APPROVAL` live-lock 的前提） |
| `PD-5` | `plan_too_large` 语义 + 新契约枚举值 |
| `PD-6` | 可选节点失败的任务终态语义（**未关闭的 wrong user-visible truth**） |
| `PD-7` | `clean_db.py` 破坏范围（清 v2 危险 / 只清 v1 无用） |
| `PD-8` | `live2d.directive` 是否接线 |
| `PD-10` | 两套 CLI driver 去留 |
| `PD-11` | 记忆/工具参数的**写侧** `max_length` 是否加 |
| `PD-13` | **合成注入的会话语义**（控制刺激是否进 mood/记忆/反思三条链） |
| `PD-14` | 跨回合重放语义（合法重发 vs 重放 wire 上不可区分） |
| `PD-15` | VAD 灵敏度策略（残余漏触发率是产品选择） |
| `PD-16` | session title 长度上限是否对齐（`PATCH` 无上限 vs `POST`/搜索 200） |

> `PD-1` / `PD-2` / `PD-12` 已裁定（见下方「已裁定的当前决策」）。
> `PD-9`（任务中心是否恢复）**也已裁定为"暂缓"**（见下方「范围/暂缓类」）——
> 它仍留在 `todo.md` §3 是因为那里的 `PD-9` 与 `FEAT-16` 同条登记、需要时重新评估，
> **不代表它待裁决**。

---

## Ratified Current Decisions

"当前权威"= **今天要去哪里读这条决定的生效形态**；"原始证据"= 它当初是怎么定下来的。

### 合同 / 真值类

| ID | 决定 | 日期 | 当前权威 | 原始证据 |
|---|---|---|---|---|
| `PD-1` | `/ready` 探 `is_available`（不是 ping），依赖不 ok 就 fail-closed 降级运行 | 2026-09-18 | [`architecture/invariants.md`](architecture/invariants.md) §3.1（Readiness contract） | [Hardening Batch 4 执行记录](review/2026-09-18-Batch4-runtime-health-truthfulness-执行记录.md) |
| `PD-2` | 409 冲突元数据报**权威库现值**（`details.currentRevision` / `currentStateVersion`），未知则**省略**该字段、绝不谎报 | 2026-09-18 | [`architecture/invariants.md`](architecture/invariants.md) §3.3（CM1–CM5） | [Hardening Batch 6 执行记录](review/2026-09-18-Batch6-conflict-metadata-truthfulness-执行记录.md)（`616e4993`） |
| `PD-12` | `context_window` 的语义 = **effective runtime window**（不是"声明值"也不是"估算值"）；对 Ollama 落实为 native `options.num_ctx` | 2026-09-19 | [`architecture/invariants.md`](architecture/invariants.md) §3.4（OL1–OL6）；[`operations.md`](operations.md) §6.3 | [Hardening Batch 17 执行记录](review/2026-09-19-Batch17-ollama-provider-adapter-执行记录.md)、[ollama-runtime-context-authority 记录](review/2026-09-18-ollama-runtime-context-authority-记录.md) |
| `D6b` | 公开入口限流 = **单进程** token-bucket（`RATE_LIMITED` 不代表全局配额）；key 认证主体优先、匿名回退 IP、默认不信 `X-Forwarded-For` | 2026-08-11 | [`electron-backend-security.md`](electron-backend-security.md) §3.6 | 同上（批次 9D） |
| `D6c` | `clientRequestId` 幂等 claim **持久化到 Mongo**；产品模式 + Mongo 离线 ⇒ **fail-closed**；回环 dev 可内存降级并让 `server_info.features.idempotency_durable=false` 如实暴露 | 2026-08-11 | 同上 §3.6 + [`electron-backend-protocol-v2.md`](electron-backend-protocol-v2.md) §1 | 同上 |

### 数据 / 持久化类

| ID | 决定 | 日期 | 当前权威 | 原始证据 |
|---|---|---|---|---|
| `决策 #6` | 后台任务终态必须**跨重启不丢**：先落库（`background_task_results`，`task_id` 唯一 + 7d TTL）再投递；硬杀窗口用本地 WAL（`task_result_wal.jsonl`，**库为权威**）补 | 2026-09-17 | [`architecture/invariants.md`](architecture/invariants.md) §2（T1–T4）；[`database-storage.md`](database-storage.md) | [Hardening Batch 1.7 执行记录](review/2026-09-17-Batch1.7-background-task-terminal-state-执行记录.md)；代码：`Module/workflow_contract/infra.py`、`core/storage/task_result_wal.py` |
| `决策 #7` | 被中断/失败的回合，**已经发出**的半截回复照样入库，但标 `metadata["incomplete"]=True`（下游据此区分完整回复），且**不发** `generation.completed` | 2026-08（S2-H5） | `core/cognitive/finalize.py :: CognitiveFinalize._archive_partial_reply`；[`electron-backend-protocol-v2.md`](electron-backend-protocol-v2.md) §5 | `core/cognitive/tests/test_v2_generation.py::test_partial_reply_is_archived_and_marked_incomplete` |
| `决策 #8` | 版本历史三条读路径改 **fail-closed**（503）：仓储不可用时**整仓**读不返回部分数据，不留混向豁免 | 2026-09（`114f6668` / `45d3ba15`） | `core/storage/character_version_repo.py`；[`database-storage.md`](database-storage.md) | [`completed/2026-09.md`](completed/2026-09.md) |
| `决策 #10` | 关机 flush 超时即丢的未整合对话缓冲，改为**每轮/失败/成功三处落盘**（`consolidator_pending` journal），构造时读回、下次启动补提 | 2026-09（`#10`） | `core/memory/consolidator_pending.py`；[`architecture.md`](architecture.md) §5.4 区 | `core/agent/lifecycle.py` 的启动步骤 `consolidator_catchup`；CLAUDE.md 记忆表 |
| `决策 #3` | 阶段 F 记忆 v2：consolidator / reflector **per-runtime**（无全局单例），由 `agent._make_runtime` 经 `for_scope()` 构造 | 2026-09-05 | [`core/memory/README.md`](../core/memory/README.md)；[`electron-backend-protocol-v2.md`](electron-backend-protocol-v2.md) §14 | [Stage F 设计稿](superpowers/specs/archive/2026-09/2026-09-05-character-stage-f-memory-v2-isolation-design.md) |

### 架构 / 工程纪律类

| ID | 决定 | 日期 | 当前权威 | 原始证据 |
|---|---|---|---|---|
| `ARCH-INC-P1-01` | 即时工具调度改名收窄：`core/task/` → `core/tool_runtime/`，类名 `Task`/`TaskScheduler` → `QueuedToolCall`/`ToolCallDispatcher` | 2026-08-30（实施 2026-09） | [`core/tool_runtime/README.md`](../core/tool_runtime/README.md)、[`architecture.md`](architecture.md) §12 | [后端架构分层与模块化增量评估](review/2026-08-30-后端架构分层与模块化增量评估.md) |
| `ARCH-INC-P2-02` | WS 契约单文件 → `frontend_contract/ws_message/` 包；`ServerMessage`/`ClientMessage` 联合**只在 `__init__.py`** 装配 | 2026-09-11 | [`frontend_contract/README.md`](../frontend_contract/README.md) | 同上评估稿 §5 批次 D |
| `ARCH-INC-P2-03` | 后台协程所有权收口到 `Module/_common/task_scope.py`（`AsyncTaskScope` / `cancel_and_drain`）；确需裸 spawn 必须在 `make check-arch` R6 逐点登记 | 2026-09 | [`architecture/invariants.md`](architecture/invariants.md) §5；CLAUDE.md「后台协程所有权」 | 同上评估稿 |
| 私有穿透纪律 | 每处跨包私有穿透**要么改走公开 API、要么同行 `# noqa: SLF001` + 理由**；由 `core/tests/test_cross_package_privacy.py`（全仓）+ `core/input/tests/test_layer_discipline.py`（输入层更严）执行 | 2026-09-11/17 | `core/tests/test_cross_package_privacy.py`；[`code-style-guide.md`](code-style-guide.md) | [Hardening Batch 2 执行记录](review/2026-09-17-Batch2-guardrails-hardening-执行记录.md) |
| 契约枚举预留登记 | 预留枚举值必须登记 **"值域预留 + 为什么暂不产" + 「解除条件」**两条，缺一即红（防止登记表退化成永久豁免单） | 2026-09-16（owner 裁定） | `scripts/check_contract_reachability.py::_RESERVED_ENUM_MEMBERS`；CLAUDE.md「契约可达性」 | [Hardening Batch 2 执行记录](review/2026-09-17-Batch2-guardrails-hardening-执行记录.md) |
| CI 保持 **push-only** | 本仓不开放 PR；不把 `pull_request` 加回 self-hosted job，不用 `pull_request_target` 绕过；要恢复 PR 门禁必须先有**隔离 runner** | 2026-09-17（owner 选择） | [`ci.md`](ci.md)（唯一权威，含恢复清单） | [Hardening Batch 2.2 执行记录](review/2026-09-17-Batch2-guardrails-hardening-执行记录.md) |
| 测试分层与 `external` opt-in | `external` marker 取代 `--ignore`；`test-quick`/`test-normal` 不跳目录；`real_mongo` 走容器（push-only + self-hosted）；**`make test-full` 无自动执行点** | 2026-09-17 | [`testing.md`](testing.md)、[`ci.md`](ci.md) | [Hardening Batch 1.5/2 执行记录](review/) |
| 本地最终 battery 必须含 `make audit` | 验收清单强制包含依赖/静态安全扫描（Batch 18 follow-up 的教训） | 2026-09-20 | [`testing.md`](testing.md) §Required pre-push verification | [Hardening Batch 19 执行记录](review/2026-09-20-Batch19-long-tail-backlog-closure-执行记录.md) |
| Hardening Batch 19 **Keep-As-Is** 清单 | EventBus / FSM / `_process_lock` / CognitiveLoop / PromptAssembler / SessionRepository 架构 / LLMAdapter·Factory / OllamaBackend / `contextWindow` authority / TokenEstimator authority / 0.75 headroom / push-only self-hosted CI / no durable inbox / no generic transaction framework / no automatic tokenizer download / no runtime token-count provider call。**owner 确认，勿因边角 case 重开**；**清单正文不在此复制**——权威是 [`todo.md`](../todo.md) §8（新增第 17 项时只需改那一处） | 2026-09-20 | [`todo.md`](../todo.md) §8 | [Hardening Batch 19 执行记录](review/2026-09-20-Batch19-long-tail-backlog-closure-执行记录.md) |

### 范围/暂缓类（"不做"也是决定）

| ID | 决定 | 日期 | 当前权威 | 原始证据 |
|---|---|---|---|---|
| `PD-9` / `FEAT-16` | **任务中心（TaskSystem + TaskService）暂缓**；暂缓期内不把普通工具调用并入 WorkflowEngine、不创建产品 TaskRecord、不动现有 wire 契约；恢复激励点 = 需要可查询/可控/可恢复展示的任务历史时从批次 A 启动 | 2026-08-30 | [`todo.md`](../todo.md) §5/§8 | [task-system 总体设计稿](2026-08-29-task-system-overall-architecture-design.md)（⚠️ UNIMPLEMENTED DESIGN DRAFT） |
| — | `core/tool_runtime` 的**预留能力保留**（`depends_on` / `children_ids` / `run_in_background` / `progress` / checkpoint 持久化 / `dispatch_background()` / `new_background()`）——评估稿的删除计划作废 | 2026-09-12 | [`todo.md`](../todo.md) §8 | [`core/tool_runtime/README.md`](../core/tool_runtime/README.md) |
| — | **剩余桩代码一律保留**（"零引用 ≠ 该死"）；保留项统一带可 grep 标记 **`P3-01-RESERVED`** | 2026-09-12 | [`architecture/dead-code-review-checklist.md`](architecture/dead-code-review-checklist.md) §9 | [`todo.md`](../todo.md) §8 |
| 阶段 6 工具双轨 = **方案 B** | Desktop 工具走 `tool.request`/`tool.status`；Backend 工具 v2 发 `tool.started`/`tool.completed` 展示帧、停发 v2 的 `action{tool_executing/tool_done}`（**v1 逐字节不变**）；`tool.request` 必填 `sourceCallId` | 2026-08-19 | [`electron-backend-protocol-v2.md`](electron-backend-protocol-v2.md) §10.7；CLAUDE.md「工具双轨」 | [阶段 6 工具双轨统筹决策记录](review/2026-08-19-阶段6-工具双轨统筹决策记录.md) |
| Schema 变更**不做迁移框架** | 新增集合/索引/字段由幂等 `ensure_index`（每次 `connect()`）自动覆盖；只有"删/改已有索引"需人工 `drop_index()`；`schema_drift` 只读比对、孤儿索引**绝不自动删** | 2026-09 | [`database-storage.md`](database-storage.md) §五；[`operations.md`](operations.md) §4.4 | [`architecture/invariants.md`](architecture/invariants.md) §5（索引选项声明表守卫） |

---

## Historical Decision ID Spaces

本仓历史上并行使用过**至少四套**决策 ID。它们**不重编号**——只在此建立对照，
并说明每套的权威现状。

| 命名空间 | 形态 | 用在哪 | 现状 |
|---|---|---|---|
| **`PD-*`** | `PD-1` … `PD-16` | [`todo.md`](../todo.md) §3（产品决策） | **当前空间**。新增产品决策用这个 |
| **`决策 #N`** | `决策 #1` … `决策 #10+` | 2026-08/09 的早期间隔编号：`docs/completed/2026-09.md`、代码注释（如 `Module/workflow_contract/infra.py`、`core/storage/session_repo.py`）、CLAUDE.md 的记忆/持久化表 | **历史空间，不要新增**。已裁定的几条见上表；未在本文登记的，去 [`completed/2026-09.md`](completed/2026-09.md) 与代码注释里查。⚠️ 它与 `PD-*` **没有任何编号对应关系** |
| **`D*`** | `D1`…`D6`, `D6a/D6b/D6c`, `D-s`, `D-GL-01` | Electron v2 契约/加固批次的**批内**决策（设计稿与执行记录内部编号） | **批内空间**：只在对应批次记录里有意义。被正文引用的当前权威是 [security §3.6](electron-backend-security.md)（`D6b`/`D6c`）与 [protocol-v2 §10](electron-backend-protocol-v2.md)（`D-GL-01`）。**离开该批次语境后不要裸用** |
| **`TSA-P*`** | `TSA-P01` … `TSA-P10` | [task-system 总体设计稿](2026-08-29-task-system-overall-architecture-design.md) 的待决项 | ⚠️ 属 **UNIMPLEMENTED DESIGN DRAFT** 的内部编号，**未进入本仓决策体系**；TaskSystem 整体暂缓（`FEAT-16`/`PD-9`），故这套 ID 目前不代表任何待办 |

> ⚠️ **`F*` 也不是决策空间**，而且比上面几套更乱：[`todo.md`](../todo.md) §9 明确
> "Batch 1.6/1.7/1.8 各有一套 F1..F18"，引用旧号**必须带来源批次**（例如"1.6-F5"）。
> 找不到的 ID 先去 todo.md §9 的对照表查，不要新建。

---

## 相关文档

| 文档 | 内容 |
|---|---|
| [`todo.md`](../todo.md) | **backlog 唯一权威**（pending 的 `PD-*`、Active/Deferred/Architecture Debt/Future Features/Closed archive + ID 漂移对照表） |
| [`hardening-program.md`](hardening-program.md) | Batch 1→20 的历史 roll-up（**不是**当前权威） |
| [`review/README.md`](review/README.md) | 决策过程与逐条证据的历史正文；含两套 Batch 编号的消歧 |
| [`completed.md`](completed.md) | 月度完成记录（`决策 #N` 的落地记录在 2026-09 那期） |
| [`architecture/invariants.md`](architecture/invariants.md) | 已裁定决定的**生效形态**（不变量 + 测试 + 门禁） |
