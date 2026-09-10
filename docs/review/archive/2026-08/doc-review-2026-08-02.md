# 文档审查报告

> 审查日期: 2026-08-02 | 范围: 全项目文档（排除 `docs/review`、`docs/superpowers`、`docs/completed`、`docs/legacy`、`.superpowers/`、`static/live2d-demo/`、`character/*/reference/`、`core/prompts/ref/`） | 方法: 6 路并行交叉验证 + 关键项人工复核 + pytest 实测计数

---

## 测试计数实测（本报告的基准数据）

`pytest --collect-only -q`（Elysia env，2026-08-02）实测：

| 项 | 实测 | README/testing.md 声称 | 偏差 |
|----|------|------------------------|------|
| 总数 | **2163** | 1927 | +236 |
| `unit` | **1310** | 1112 | +198 |
| `service` | **782** | 728 | +54 |
| `integration` | **35** | 35 | 0 |
| `slow` | **56** | 52 | +4 |

README.md:420 与 docs/testing.md:5、9-12 的测试数字均已过时。

---

## 严重问题（会直接误导读者/对接方）

### 1. [docs/frontend-api.md:680, 757, 119] 图片内容块字段名错误：`source` → `src`

文档示例写 `{ "type": "image", "source": "/uploads/..." }`。实际前后端统一为 `src`：

- 前端契约 `frontend/src/types/protocol.ts:41` → `{ type: 'image'; src: string; mime?: string }`
- 后端模型 `core/schema/part.py:80` → `ImagePart.src`（`core/schema/adapters/message_adapter.py:223` 读 `p.src`）

第三方客户端照文档用 `source` 发送会被 Pydantic 校验丢弃，图片丢失。

### 2. [docs/frontend-api.md:780-793] `GET /api/providers` 响应结构完全不符

文档声称返回数组 `{ "llm": [ { "name", "base_url", "env_key" } ] }`。实际 `core/input/provider_registry.py:42-60` `build_providers_response` 返回：

```python
{"llm": {"<provider>": {"models": [...]}}, "stt": {...}, "tts": {...},
 "custom": {...}, "unconfigured": {service: [{"name", "env_vars"}]}}
```

provider 是 dict 键，无 `base_url`/`env_key` 字段，另有 `custom`/`unconfigured` 键。前端 `protocol.ts:90-104` 与此一致。

### 3. [Module/workflow_engine/docs/api.md] 全篇使用已废弃的 `Task*` 命名（文档自标"最终版"）

task_system→workflow_engine 重命名后，实际类名全部为 `WorkflowMailbox`（mailbox.py:26）、`WorkflowSummary`/`WorkflowProgress`/`WorkflowDAG`（schema/output.py）、`WorkflowStatus`/`WorkflowId`/`WorkflowEvent`（契约层）、`WorkflowErrorCode`（types.py:80）。api.md 仍通篇用 `TaskMailbox`/`TaskSummary`/`TaskDAG`/`TaskStatus`/`TaskId`/`TaskEvent` 等，与 `workflow_engine.py:173/306/458` 实际签名全部对不上。

### 4. [docs/data-structure-survey.md] 两大节整体过时（2026-07-15 生成，早于契约层提取与 session params 迁移）

- **第 4 节（L125-192）WorkflowEngine 类型**：声称 types.py 12 枚举 + 6 NewType、protocol.py 12 BaseModel、output.py 14 BaseModel、"约 40 个独占类型"。实际契约层提取后：types.py 仅 4 枚举（WorkflowErrorCode/ResumeErrorCode/NodeStatus/NodeErrorCode）+ 1 NewType（ArtifactId）；WorkflowStatus/WorkflowEvent/DelegationContext/DelegationResult/SubmitError/HITLData 等全部迁至 `Module/workflow_contract/schema.py`；RBACLevel 迁 `Module/tool/capability.py:21`；UserProfile 迁 `core/cognitive/user_profile.py`。
- **第 6 节（L216-238）Session Params**：声称 15 个裸 param key、建议建立 SessionParams dataclass。实际 `set_param`/`get_param` 已删除，字段全部迁入类型化 Pydantic 模型 `CharacterState`（core/schema/character.py）与 `MemoryReflectorState`（core/schema/memory_reflector.py），该建议已被实现。

### 5. [docs/testing.md:138-143] `core/status/tests/` 测试文件路径错误

文档列出 6 个测试文件，但 `core/status/tests/` 实际只有 `test_agent_fsm.py`；其余 5 个（test_types / test_lifecycle_steps / test_lifecycle_flush / test_server_banner / test_agent_methods）实际在 `core/agent/tests/`，status→agent 拆分时已迁移。且整个 `core/agent/tests/` 目录（7 文件）完全未列入 core 层测试索引（L130-190）。

---

## 中等问题（过时信息 / 内部矛盾 / 实际会踩坑）

### 文档间/文档内矛盾

| # | 位置 | 问题 |
|---|------|------|
| 6 | [docs/testing.md:19-29] + [CLAUDE.md:21,25] | `make test-quick`/`test-normal` 都带 `$(TASK_SYSTEM_IGNORE)` = `--ignore=Module/workflow_engine/tests`（Makefile:22,25,28），两文档给出的"或:"等价 pytest 命令缺该参数，实际运行范围更大 |
| 7 | [docs/database-storage.md:17 vs 225] | 配置优先级自相矛盾：第一节"config.yaml → 环境变量"、第七节"环境变量 > config.yaml"。实际是"环境变量 > YAML > 默认值"（loader.py + .env.example） |
| 8 | [docs/database-storage.md:26,141] | `memory_type` 枚举错误：文档写 `fact/plan/preference/note`，实际 `core/schema/memory.py:9-15` 为 **Fact/Preference/Event/Plan/Note**（含 Event，TitleCase） |
| 9 | [docs/database-storage.md:7,32] | 淘汰策略错误：`max_age_days=90` 标注"当前未使用"（long_term.py:60）；且 `_evict_overflow` 驱逐时同步从 Milvus 删除 evicted_pks，"Milvus 不自动删除"不成立 |
| 10 | [docs/database-storage.md:138] | MemoryEntry.id 格式错误：文档写"UUID4 前 12 位"，实际是 `new_message_id()` 的 `msg_` + 26 位 hex |
| 11 | [core/monitoring/README.md:15,33] | EventBus"5 级优先级（CRITICAL→BACKGROUND）"错误：实际 `EventPriority`（core/schema/agent.py:38-42）仅 3 级 INTERRUPT/STATE_CHANGE/NORMAL，与 core/README.md:99"三种优先级"矛盾 |
| 12 | [core/task/README.md:18,28] | task.py ~80 行（实际 222）、scheduler.py ~430 行（实际 510），与 core/README 文件索引表（222/510）矛盾 |
| 13 | [Module/README.md:235] | `debug_routes.py` 是包不是单文件；"41 路由"实际 42 路由注册 + 3 add_static = 45 |
| 14 | [character/README.md:19-21] | 声称每个角色"config.yaml 必需、personality.md 必需"，但 `character/cyrene/` 只有 reference/，与"必需"直接矛盾（根 README 结构树也仍把 cyrene 列为角色） |

### 类名/文件名过时

| # | 位置 | 问题 |
|---|------|------|
| 15 | [character/README.md:45,55-56] | `PersonalityEngine` 已合并为 `CharacterEngine`（core/personality/engine.py:62） |
| 16 | [core/personality/README.md:112,137] | `AwarenessTrigger` 实际类名是 `AwarenessJudge`（awareness.py:24） |
| 17 | [core/README.md:93] + [core/agent/README.md:75] | `agent_fsm.py` 不在 `core/agent/` 而在 `core/status/agent_fsm.py` |
| 18 | [core/agent/README.md:59-61] | `_start_background_tasks()`/`_stop_background_tasks()` 不存在，后台任务由 `_register_background_task` + lifecycle mixin 管理 |
| 19 | [core/agent/README.md:14-16] | start() 顺序与实际 `_STARTUP_STEPS`（lifecycle.py:71）不符 |
| 20 | [core/schema/README.md:13,51] | `_utils.py` 实际文件名是 `utils.py`；且 README 遗漏 compression.py / error_type.py / utils.py 三个文件 |
| 21 | [core/memory/README.md:65] | `LongTermMemory.search(query, limit=20)` 实际签名 `search(query_text="", query_embedding=None, top_k=5, ...)`（long_term.py:444） |
| 22 | [core/memory/README.md:79] | 聚类公式过时：实际 `n_clusters = ceil(n / CLUSTER_SIZE)`，`CLUSTER_SIZE=20`（memory_reflector.py:240-242） |
| 23 | [core/prompts/README.md:17] | `SpeakingStyleEdit.j2` 加载方是 `core/personality/compression.py:282`（CompressionManager._refresh_c6），非 engine.py |
| 24 | [Module/_common/README.md:22,26] | 示例函数名 `run_main` 实际是 `run_module_main`（main_runner.py:8） |

### 配置/环境变量错误

| # | 位置 | 问题 |
|---|------|------|
| 25 | [Module/llm/README.md:173-176] | env 写 `DEEPSEEK_API_BASE`/`DEEPSEEK_API_KEY`，实际 `LLMConfig` 读 `LLM_API_BASE`/`LLM_API_KEY`（config.py:16,21） |
| 26 | [Module/stt/README.md:96] | env 写 `SENSEVOICE_API_URL`，实际 config.py 读 `STT_API_URL` |
| 27 | [README.md:146] | STT 环境变量三处不一致：README 写 `STT_API_URL`（代码 config 也读它），但 `.env.example:49` 与 `Module/config/preflight.py:47` 用 `SENSEVOICE_URL` —— 照 .env.example 配置会设一个代码不读的变量 |
| 28 | [Module/database/vectordb/README.md:110-113] + [Module/database/documentdb/README.md:102-104] | Config 表默认值写 `http://localhost:19530` / `mongodb://localhost:27017`，实际默认值为 `""`（必填，config.py:11） |
| 29 | [core/tools/memory/README.md:15-19] | config.yaml 声称含 `database_readonly`，实际只有 `database_allowed`/`max_result_tokens`（注释明确该值由代码自管） |

### 其他 MED

| # | 位置 | 问题 |
|---|------|------|
| 30 | [README.md:386] | `src/composables/` 声称 3 个，实际 4 个（useAbortController/useImageUpload/useLightbox/useWebSocket），与 CLAUDE.md 的 4 个矛盾 |
| 31 | [README.md:345-377] | core/ 树遗漏 `status/`（AgentStatus 接口层）与 `hitl.py`（HITL 通知器） |
| 32 | [README.md:420] + [docs/testing.md:5,9-12] | 测试计数过时（见顶部基准表） |
| 33 | [docs/frontend-api.md:20,29-31] | 连接/重连声称收到 `state`，实际 ws_handler.py 建连只发 `session` + `capabilities` + `expression`（心情），从不发 `state`（notify_state 仅 FSM 转换时触发） |
| 34 | [docs/frontend-api.md:145-162] | `capabilities` 字段表漏 `stt_model`/`tts_model`（ws_handler.py:74-75 实际下发） |
| 35 | [docs/frontend-api.md:752-757] | `POST /api/images` 响应结构不符：实际返回 `{"url": "/uploads/images/{name}", "mime", "size"}`（api_routes.py:304-305），URL 前缀含 `images/` 段；超限返回 413 `{"error": "图片过大，上限 NMB"}`，无 `ok`/`path` 字段 |
| 36 | [docs/frontend-api.md:301-307] | Live2D `action: "wave"` 等子类型已死：后端只发 `tool_executing`/`tool_done`（loop.py:558,609,633），前端只处理这两种 |
| 37 | [docs/frontend-api.md:82,208-215] | session/message 完整消息示例里的 `mood` 字段不存在：会话序列化不输出 mood，`AssistantMessage` 无该字段，mood 只经独立 `expression` 消息传递 |
| 38 | [docs/architecture.md:298] | ProjectPath"20 枚举：13 目录 + 7 文件"，实际 21 个（13 目录 + 8 文件，含 SESSION_PENDING_CHECK） |
| 39 | [docs/architecture.md:10-13] | 入口模式含 `once (single-shot)`，app.py 实际只有默认 server + `--cli` + `--check`/`--full`，无 once 模式 |
| 40 | [docs/architecture.md:1026] | 文件树列 `core/text/text_utils.py` 不存在，MessageObj 实际在 `core/utils/text_utils.py:30` |
| 41 | [Module/system_prompt/README.md:66-69] | 声称 ConflictRules 用 `StaticSection`，实际 12 个 section 全部继承 `Jinja2Section`（conflict_rules.py:5），StaticSection 未使用 |
| 42 | [Module/system_prompt/README.md:15] | 声称 `__init__.py` 保留 MessageBuilder DeprecationWarning re-export，实际已完全移除 |
| 43 | [scripts/README.md:23-27] | clean_db.py 清理范围漏 `character_versions`/`character_l2_versions`（clean_db.py:140,224-225 实际清理） |
| 44 | [core/cognitive/README.md:102-109] | 文件布局漏 `delegation.py`/`user_profile.py` 及 6 个测试文件 |
| 45 | [core/debug/README.md:108] | `core/input/debug_routes.py` 是包不是单文件 |
| 46 | [core/input/README.md:9-21] | 文件结构漏 `custom_providers.py`、`provider_registry.py` |
| 47 | [core/debug/README.md:80-91] | API 端点表只列 GET 型 10 个，漏 ltm POST/PUT/DELETE、archive/sessions 的 PUT/DELETE、l2-versions/character-versions/rollback、/debug/api/tasks 等 CRUD 端点 |
| 48 | [Module/workflow_engine/docs/implementation.md:16-102] | 文件布局仍列 `task_system.py`/`protocol.py`/`infra.py`/`utils.py`，实际主文件是 `workflow_engine.py`，protocol.py 已删、infra.py 迁契约层 |

---

## 轻微问题（计数漂移 / 遗漏 / 过时细节）

### 文件行数/文件数漂移（多为口头"~N 行"）

| # | 位置 | 声称 → 实际 |
|---|------|-------------|
| 49 | core/README.md:260-279 | cognitive/loop.py 999→1031；personality/engine.py 421→428；token_tracker.py 193→176；text/ "7 文件 1352"→6 文件 1519 行；debug/ "3 文件 623"→4 文件 631 行；debug_routes/ "7 文件 893"→7 文件 922 行 |
| 50 | core/config/README.md:18,31 | app_config.py ~70→112；character_config.py ~80→149 |
| 51 | core/utils/README.md:19,27 | message_id.py 11→15；log_setup.py ~150→215 |
| 52 | core/adapter/README.md:25,35,60 | llm_adapter 212→213；tts_adapter 370→344；message_builder 139→141 |
| 53 | core/README.md:75,103,153,278 | "9 核心组件"→实际 10；cognitive "5 files"→6（+delegation/user_profile）；schema "13 .py"→14 |
| 54 | docs/architecture.md:521,961 | LLMAdapter ~170→213 行；"~1,800 测试"→2163 |
| 55 | todo.md:105,107,90,31,16,104 | LoggerFactory 169→246 行；part.py ~70→130 行；"13 工具"→12；protocol.py:15 行号/内容过时；loop.py:875→897、lifecycle.py:170→191；manager.py:63-88→97 |

### 遗漏/死链

| # | 位置 | 问题 |
|---|------|------|
| 56 | [README.md:392] | DebugData "40 字段"→实际约 65（frontend/src/debug/types.ts:84-115） |
| 57 | [README.md:389-402] | frontend/src/debug/ 树漏 `styles/panel.css` |
| 58 | [README.md:185] | Module 层 LLM 说明漏 MiMo（L281/L330 均含） |
| 59 | [README.md:283 vs 449-465] | 功能表 Live2D 标 "done"，与"默认静态图、需自建 index.html"自相矛盾 |
| 60 | [README.md:11] | "最近 (2026-07-03)… 971 测试" 距今日一个月未更新，且与 L420 的 1927 差距巨大 |
| 61 | [Module/stt/README.md:138] + [Module/output/README.md:119] | `[core/input/README.md](../input/README.md)` 相对路径错（解析为 Module/input/README.md），应为 `../../core/input/README.md` |
| 62 | [core/prompts/README.md:39-40] | ref/ 清单含不存在的 `优化后的prompt-requirements.md`、`character-reflector-prompt-requirements.md`；漏实际存在的 `awareness-judge-requirements.md` |
| 63 | [core/prompts/README.md:5,50 vs 16-17] | "所有活跃模板使用 str.replace()"与"所有…"措辞与 AwarenessJudge/SpeakingStyleEdit 用 Jinja2 宏渲染矛盾 |
| 64 | [core/schema/README.md:53] | 测试清单只列 4 个，实际 12 个测试文件 |
| 65 | [core/text/README.md:132-137] | 测试清单漏 `test_fuzz_sequences.py`（实际 6 个） |
| 66 | [core/utils/README.md:30-32] | 库日志分流漏 `http.log`/`embedding.log`（log_setup.py:80-92） |
| 67 | [Module/workflow_engine/README.md:86] | `safety/` 声称"占位重导出"，实际 `safety/__init__.py` 为空 |
| 68 | [Module/workflow_engine/docs/PROGRESS.md:120] | 声称 unit/test_delegation_preparer.py 属 workflow_engine，实际已迁 core/cognitive/tests |
| 69 | [Module/tts/README.md:23-26] | 文件树重复列两个 `tests/` 条目 |
| 70 | [Module/system_prompt/README.md:46-52] | 测试树漏 `unit/test_sections/` 子目录与 `test_prompt.py` |
| 71 | [Module/bench/README.md:15,33-47] | 示例路径 `ref/Elysia/Elysia.wav` 实际为 `data/ref/Elysia.wav`；测试内容表漏 `bench_e2e()` |
| 72 | [docs/experience.md:44-47] | prompt 路径经验仍写 `prompt/`，实际已迁 `core/prompts/`（2026-07-23 rehome） |
| 73 | [docs/readme-conventions.md:115-131,136] | log/README.md 缺"文档导航"面包屑；"`Module/schema/`"示例目录已不存在（schema 已迁 core） |
| 74 | [docs/README.md:33-34] | 命名约定"统一 YYYY-MM-DD-<主题>-plan.md"与实际不符（plans/ 大量文件无该后缀） |
| 75 | [log/README.md:12] | mongo.log 声称含"pymongo/motor 库日志 DEBUG"，实际库日志为 WARNING（log_setup.py:84），应用层 CRUD 为 DEBUG |
| 76 | [CLAUDE.md:209] | Module.llm Backends 漏 `gemini`（占位，registry 已注册） |
| 77 | [frontend/README.md:18] | "ServerMessage 20+ 类型"高估（union 实际 18 个成员） |
| 78 | [frontend_todo.md:21] | "8 个文件集成 useAbortController"→实际 6 个；debugStore 用原生 AbortController 不属此列 |
| 79 | [docs/frontend-api.md:44] | Server→Client "16 种" vs protocol.ts union 17 种（多 `heartbeat_ack`，死类型） |
| 80 | [docs/frontend-api.md:341,343] | inner_voice"结束后一次性发送"不准确（每遇标签发一条）；`#inner-voice-toggle` id 不存在 |
| 81 | [Module/config/README.md:24] | preflight"14 项"→实际 11 项（非 full）/ 16 项（full） |
| 82 | [core/memory/README.md:87] | `__init__.py` 声称"空"，实际 re-export SessionMemory/LongTermMemory |

---

## 无问题文档（逐项核对未发现错误）

- `docs/sync-lessons.md`
- `Module/logger/README.md`、`Module/tool/README.md`、`Module/embedding/README.md`、`Module/database/README.md`、`Module/workflow_contract/README.md`、`Module/system_prompt/templates/README.md`
- `core/storage/README.md`、`core/tools/README.md` 及全部插件 README（filesystem/web/time/reminder/runtime_config/self_state/vision）
- `static/debug.md`、`Module/tts/minimax_tts_api.md`
- `frontend/README.md`（仅 L18"20+ 类型"一处轻微，见 #77）

---

## 修复优先级

| 优先级 | 涉及文档 | 主要问题 |
|--------|----------|----------|
| P0（事实性错误，会误导对接/执行） | docs/frontend-api.md（image `src`、/api/providers、/api/images、连接流程）、Module/workflow_engine/docs/api.md（Task* 命名）、docs/data-structure-survey.md 第 4/6 节、docs/testing.md 测试路径 | #1-5 |
| P1（配置/类名错误 + 计数过时） | README.md、docs/testing.md、CLAUDE.md（测试数与 make 等价命令）、database-storage.md、character/README.md、core/* README 的类名/方法名错误 | #6-48 |
| P2（行数/计数/树图漂移） | core/README 及子 README 行数表、Module 各 README 树图、docs/architecture.md 细节 | #49-55, 56-82 |
| P3（后续清理） | todo.md/frontend_todo.md 行号、readme-conventions 面包屑、log/README 级别措辞 | 其余 LOW |

---

## 相对 2026-07-18 审查的进展

上次审查报告的 32 项中，以下已确认修复：

- `personality.txt` → `personality.md`（README/character 配置注释均已更正）
- 废弃文档已移入 `docs/legacy/` 并标注（README 导航同步）
- system_prompt section 数量 11→12 已更正（CLAUDE.md/README 均正确）
- `core/schema/README.md` 的 emotion.py/reflector.py 旧引用已清理
- `character_state_repo.py` 死引用已删除（core/storage/README 现已干净）
- `static/live2d-demo/index.html` 标注已改为"待创建"
- docs/README.md 已登记 data-structure-survey.md（不再孤立）

但以下旧问题**仍未修复**或复现：

- 测试计数类问题（README/testing.md，旧报告 #12/13）→ 仍过时，且数值已进一步漂移（见顶部基准表）
- `character/cyrene/` 仍列为角色但无 config.yaml/personality.md（旧报告 #14）→ README 树与 character/README 仍矛盾
- core/README 文件行数表过时（旧报告 #11）→ 仍然过时
- Module/system_prompt/README section 与模板描述（旧报告 #16）→ 部分修复但 StaticSection/MessageBuilder 描述仍错
