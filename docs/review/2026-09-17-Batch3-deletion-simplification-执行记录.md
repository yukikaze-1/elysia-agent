# Batch 3 — Deletion / Simplification 执行记录

> ⚠️ **Historical execution record.** 本文描述**执行当时**的系统与结论；后续批次可能推翻其中部分判断。
> 当前事实见 [docs 索引](../README.md) 的 Canonical References（架构 / 不变量 / 测试 / CI / 运维 / 契约）。
> 正文**按当时原样保留**——不改结论、不删失败轨迹、不重写 Challenger 反证（`docs/readme-conventions.md` Rule 3）。

> 文档导航：[项目文档](../../README.md) → [docs 索引](../README.md) → [review](.) → 本文

**范围**：`b78f0fcc..58ddcf05`（**19 个 commit**；代码+配置+测试 **52 文件 +184/−295**，另本次执行记录 +484 行）
**目标**：系统性删除已失去价值、无真实消费者、被新不变量替代、重复或误导的代码/配置/兼容层。
**不做**：新增 Service/Manager/Adapter/Protocol/Facade；大规模 rename；目录大搬家；L0/L1/L2 大重构；
任何新 feature。
**方法**：6 个并行 subagent（A–E 盘点 + F 对抗复核）→ 3.1 只读盘点 → consumer proof → 3.2 删除 →
3.3 简化 → 3.4 文档清理。**严格按序，不边扫边删。**

---

## 1. Executive Summary

| 问题 | 结论 |
|---|---|
| 找到多少候选？ | **约 460 条**（A 生产 43+47 / B 配置 306 行 / D 导出面 889 名 / E 文档 191 条失真 + 35 候选 / F 15 个被挑战目标）。 |
| 多少 proven-dead？ | **18 个对象**（覆盖 20 条编号候选），全部有 consumer proof。 |
| 删除多少？ | **18 个对象 / 11 个删除 commit**；净 **−105 行**（+158/−263，其中 +100 行是本批新写的证据性注释与文档更正）。 |
| 多少被 Challenger 拦下？ | **8 条 FALSE_DEAD**（F 独立推翻）+ **5 条本 agent 复核后改判 KEEP**（`run_repl`、`DEFAULT_CHARACTER_ID`、`voice`、`appearance`、`milvus_token`）。F 还推翻了前序审计「可直接删」的 3 条结论。 |
| 有没有 correctness bug？ | **有 7 条**（B3-CB-1..7）。其中 6 条**只在 `todo.md` 登记、未修**；第 7 条（图片上传目录的测试隔离失效，实测每次跑图真实落盘 +1）**已确认阻塞「测试可信」这一本批前提**，故按用户第 60 条另起 `fix(input)` commit（`800e54da`）修复。 |
| public API 是否改变？ | **公开面零删除**。删除项全部为 private / 无消费者的 module 级常量 / 未被读取的配置字段 / 兼容别名。 |

**未完成项（唯一）**：真实 CI。本机无 git 凭据（HTTPS 无 credential helper、SSH 无私钥），`git push` 无法执行 ⇒ push-only 的两个 workflow 尚未被触发。详见 §19。

**一句话结论**：本批删掉的东西比预期少，但**拦下的比删掉的多**——这个仓已经过了"搜不到就删"能获益的阶段；
真正的产出是 18 处确定性清理 + 1 个测试可信缺陷的修复 + 7 个被埋住的真实 bug + 一张能阻止下轮重复发现的保留清单。

---

## 2. Candidate Inventory（完整表见 `tmp/batch3/INVENTORY.md` 与各 subagent 报告）

| Candidate | Category | Evidence | Decision |
|---|---|---|---|
| `_lifecycle_persistence._mongo_available/_milvus_available` | dead field（只写不读） | 全仓 6 处 = 2 def + 2 写 + 2 声明；`getattr` 0 | **PROVEN_DEAD → 删** |
| `EmotionConfig.thresholds` + 2 角色 YAML | dead config | `rg thresholds --include=*.py` 全仓**仅 2 行**（定义 + 赋值） | **PROVEN_DEAD → 删** |
| `AppConfig` 7 个 provider 字段 | dead config | 全仓**含 tests** 0 命中 | **PROVEN_DEAD → 删** |
| `providers.yaml::database` 段 | dead YAML | `load_provider_defaults("database")` 0 调用 | **PROVEN_DEAD → 删** |
| `ThoughtResult.has_tool_calls` | dead property | 2 处 = 定义 + 归档设计稿（稿自述"无消费者"） | **PROVEN_DEAD → 删** |
| `LLMAdapter.think_stream` | dead method | 2 处；活跃是 `stream_think` | **PROVEN_DEAD → 删** |
| `WorkflowEngine._collect_tools_used` | 重复转发壳 | 真实现 `query.collect_tools_used`（活） | **PROVEN_DEAD → 删** |
| `ModelManifest.file_hash_map` | dead helper | 1 处（定义自身） | **PROVEN_DEAD → 删** |
| `_make_silence_wav` | dead helper（已内联） | 3 处 = 定义 + 归档 plan×2 | **PROVEN_DEAD → 删** |
| `_EMOTION_TAGS`/`_EVENT_TAGS` | 重复映射表 | 0 引用；单源已迁 `core/schema/mood.py` | **PROVEN_DEAD → 删** |
| `DesktopCallRepository._apply_update` | dead method | 1 处；被 `_apply_update_cas` 取代 | **PROVEN_DEAD → 删** |
| `release_all_attachments` | dead function（自带 deprecated） | 0 调用；#41 三件套已取代 | **PROVEN_DEAD → 删** |
| `ws_handlers/chat._conn_id` | 重复 helper | 0 调用；已内联（语义不同） | **PROVEN_DEAD → 删** |
| `ConsolidatorPendingStore.drop` | 同义方法 | 0 调用；`put(key,[])` 同语义 | **PROVEN_DEAD → 删** |
| `registry.flush`/`flush_all` | dead methods（**不落库**的手工口） | 0 调用；真 flush 是 `rt.flush()` | **PROVEN_DEAD → 删** |
| `CharacterRuntime.dirty` | 只写不读字段 | 生产 0 读；读点仅测试 | **PROVEN_DEAD → 删** |
| `AWARENESS_COOLDOWN_SECONDS` | dead constant | 0 引用 + `architecture.md:581` **自述**已废弃 | **PROVEN_DEAD → 删** |
| `BYTES_PER_SECOND`(`gpt_sovits`) | dead constant | 4 处，3 处是 bench 的**同名不同常量** | **PROVEN_DEAD → 删** |
| `_EMPTY_ID_FILTER` | dead constant | 2 处 = 定义 + 归档 plan | **PROVEN_DEAD → 删** |
| `safety._sanitize_session_id` | compatibility shim | 0 消费；2 处是 docstring 文本 | **PROVEN_DEAD → 删** |
| `ToolControlManager` docstring×2 | dead doc（误导） | `git log -S "class …" --all` **空**（从未存在） | **PROVEN_DEAD → 改文案** |
| `core/prompt/tests/leak_lexicon.py` | dead test helper（零 import） | 7 常量名全仓 0 命中；docstring 自陈的导入从未接线 | **PROVEN_DEAD → 删** |
| `AgentServer.run_repl` | dead method（0 消费者） | 8 处文档写成对外承诺；`--cli` 真入口是 `app.py::run_interactive` | **KEEP_PUBLIC_API** |
| `services["_tool_names"]` | dead services key | 同族 `_tool_count`/`_fsm_state`/`_session_msg_count` 是活的双端键 | **KEEP_DYNAMIC** |
| `ToolAdapter.{resolve_llm_name,executing_now,history,capability_status}` | 零消费者转发 | adapter 上 0 消费（debug 读的是 ToolManager）；门面已文档化 | **REFACTOR_LATER** |
| `DEFAULT_CHARACTER_ID` | dead constant | `core/cognitive/README.md:69` 显式登记为公开常量 | **KEEP_PUBLIC_API** |
| `live2d_directive` 全链路 | 无发射点 | 协议文档「待接线」+ TS 生成物 + v2-only 门禁 + owner 裁定 | **KEEP_COMPAT** |
| `AgentInfra.{milvus,http,background_task_results}` | 字段被断言形状 | `test_public_surface.py:248-258` 钉住完整 field_names | **KEEP_COMPAT** |
| `Module/database/graphdb` | stub only | `Module/database/__init__.py` eager re-export + `todo.md:311` 裁定 | **KEEP_DYNAMIC** |
| `run_in_background`/`children_ids`/…/checkpoint | 预留簇 | `todo.md:16-23,317` owner 明文作废删除；`scheduler.py:557,575` 活读 | **KEEP_DYNAMIC** |
| `ToolManager._inflight` | "第二索引" | 是 ARCH-INC-P2-03 取消漏洞的**修复件** | **KEEP_DYNAMIC** |
| `core/hitl.py` | 半接线 | 生产 import + 测试**读源码**断言 | **KEEP_TEST_ONLY** |
| `character/*/config.yaml::voice`/`appearance` | 无读者 | YAML 注释显式登记「占位 / 待启用」+ 未来用途明确 | **KEEP_COMPAT**（补 `P3-01-RESERVED`） |
| `config.yaml::database.milvus_token` | YAML 被 loader 跳过 | `secret=True` ⇒ 实测写 YAML 读回 `''` | **REFACTOR_LATER**（属部署面） |
| Mongo `sha256`/`delivered_at`/`metadata.incomplete` | write-only | **无真实 Mongo 无法证「旧记录还能读」** | **LIKELY_DEAD（不删）** |
| `ConfigLoader(env_path=…)` | deprecated no-op 形参 | 无生产调用，但 Module 是库 | **LIKELY_DEAD（不删）** |
| `CharacterRuntimeRegistry.mark_busy` / `SystemPromptManager.reload_all` / `SafetyPipeline.request_exists` / `CharacterConfig.personality_block` | test-only | 各有测试引用 | **KEEP_TEST_ONLY** |
| `WorkflowEngine.cancel/pause/resume/get_progress` | core 侧无调用 | 成规模测试网 + 前端 `TaskCard.vue:19` 已定义 `cancelled` 配色 | **REFACTOR_LATER** |

---

## 3. Consumer-Proof Method

每个 `PROVEN_DEAD` 候选都跑过以下**全部**检查（原始输出见各 subagent 报告；本节只写方法）：

| 手段 | 怎么做的 | 抓到过什么 |
|---|---|---|
| **direct import** | `rg -n "\b<sym>\b" .`（`--include=*.py` 与不限扩展名两种） | — |
| **AST/import 搜索** | 自写脚本 `ast.parse` 全仓 **914 个 .py**，`Attribute`/`Name` 按 `Load`/`Store` 分离 | 区分「只写不读」与「只读不写」；`defs=3392 / zero-zero=43 / zero-prod-tested=47` |
| **re-export / `__all__`** | 107 个 `__init__.py` 的导出面 + 模块级 `__getattr__`（PEP 562） | 321 个无外部消费者；**183 在 `__all__`** ⇒ 默认 KEEP_PUBLIC_API |
| **动态查找** | `getattr/setattr/hasattr/delattr` 字符串形式（**501 处**）、`importlib`/`__import__`（6 处） | **头号假 dead 来源**：`Agent` 的 46 个 property 静态计数全部不可信 |
| **字符串消费** | 13 个「只被字符串访问」的名字、179 处 patch 目标、14 处 `usefixtures` | `to_openai/to_anthropic/to_gemini`、`on_tool_started/completed`、`fake_storage` 等 |
| **config/env** | 非 py 扩展名 + `.env.example` + `docker-compose*.yml` + `.github/workflows/` + `Makefile` | 5 个 compose-only env（`MONGO_INITDB_ROOT_*` 等）——**不是** dead |
| **tests** | `--collect-only`（6003 例）+ patch 目标核对 + **fixture 按参数名注入**（不能只搜调用点） | 避免把 pytest 反射型 helper 误判为死 |
| **docs/scripts/CI** | `docs/**`、`scripts/**`、`.github/**`、`tmph`… | `run_repl` 的 8 处文档承诺 |
| **运行期探测** | `conda run -n Elysia python -c` 逐名 `hasattr` + 注册表条目数 | `ALL_PLUGINS=16` / `_WS_HANDLERS=22` / `_BACKENDS` 6·3·4·2·1·3 / 契约模型 198 |
| **git 历史** | `git log -S` / `git blame` / `git show`（判断「从未存在」vs「曾经存在」） | `ToolControlManager` **从未存在**；`AWARENESS_COOLDOWN_SECONDS` 是重构遗留 |

**coverage 只作辅助**：本批**未用** coverage 作任何删除判据（用户第 18 条）。`vulture` 未安装，未使用。

**删除后复核协议**（D 提供，`tmp/batch3/d_check.py`）：
```bash
python tmp/batch3/d_check.py <候选名…>          # BLOCK/WARN/NOCONSUMER + 逐条 file:line
conda run -n Elysia python tmp/batch3/d_check.py --runtime   # 107 包 import + __all__/裸重导出面 + 注册表条目数
```
变更后任何 `[IMPORT-FAIL]` / `[ALL-MISSING]` / `[bare-reexport] 缺 N>0` 即回滚。
**本批实测：全绿，注册表条目数与基线一致。**

---

## 4. Proven-Dead Deletions（逐项）

详见 §2 证据列与各 commit message。这里只记**为什么可删**的判据类型：

| 判据类型 | 项 | 为什么这是证明而不是猜测 |
|---|---|---|
| **活兄弟字段存在** | `_mongo_available`/`_milvus_available` | 同源 `_mongo_ok`/`_milvus_ok` 才是被 `getattr` 读的那对；删除**不丢信息**（`results` 已含 `session`/`message`/`ltm`） |
| **字段 0 读取 + 语义已被别处承担** | `AppConfig` 7 字段 | 生效路径是 `providers.yaml` 的 `url_key`/`env_key`（`registry_helpers` 的 fallback 链里**没有** AppConfig 这一层） |
| **配置项连解析都没有** | `appearance`（→ 改判 KEEP，见 §6） | `from_yaml` 的 `raw.get()` 清单里没有它 |
| **同名不同物被误认为同一物** | `BYTES_PER_SECOND` | bench 的 `TTS_BYTES_PER_SECOND` 是**类属性**，与模块常量无关 |
| **已被新机制取代且注释自陈** | `release_all_attachments`、`_apply_update`、`_conn_id` | 各自有活跃替代实现；前两者自带 `deprecated`/CAS 注释 |
| **只被文档/注释提到** | `AWARENESS_COOLDOWN_SECONDS`、`_sanitize_session_id`、`_EMPTY_ID_FILTER` | `rg` 命中的"消费者"全是 prose，非代码 |
| **类从未存在** | `ToolControlManager` | `git log -S "class ToolControlManager" --all` **空输出** |
| **零 import + 注释自陈的意图从未接线** | `core/prompt/tests/leak_lexicon.py` | 7 个常量名全仓（排除定义文件）**命中均为 0**；唯一「消费者」是另一文件 docstring 里一句「词条从 leak_lexicon 导入」，而它的 import 段没有它、断言用硬编码字面量 |
| **写了但没人读，且读的是别的判据** | `CharacterRuntime.dirty` | `_evict_if_needed` 读 `_busy`；`agent.py:1300` 的注释声称读 `dirty` 是**错的** |

**每个删除 commit 的验证**（全部实跑）：import smoke（`core.agent` / 全包 walk）+ 该模块的 targeted tests +
`make lint` + `make pyright` + `make check-arch`。

---

## 5. Simplifications

**只做了 1 项**——因为其余候选都被判为"改判据会变行为"或"属 public surface 变更"：

| # | before | after | 为什么语义不变 |
|---|---|---|---|
| **S-1** | `_lifecycle_services.py::_sync_tts_gate` → `self._cognitive._tts_mgr.set_enabled(...)` + `# noqa: SLF001` | `self._cognitive.tts_manager.set_enabled(...)` | `tts_manager` 是**已有的** `@property`，实现就是 `return self._tts_mgr`（`loop.py:328-335`），且其 docstring **自陈**这个公开口就是为清掉这处穿透而加的。零抽象新增。 |

**明确拒做的简化候选**（避免"为整洁而改"）：

| 候选 | 为什么不做 |
|---|---|
| `AppConfig.TTS/STT/LLMConfig` ↔ `Module/*/config.py` 合并 | 跨层（core 配置 vs Module 库 API），生命周期不同；`Module` 侧是**已发布面**（在 `__all__` 里） |
| `preflight` 三张 provider→env 表 ↔ `providers.yaml` 去重 | **不等价**：gpt-sovits 用的是 `url_key`（`GPT_SOVITS_URL`）不是 `env_key` ⇒ 天真 DRY **会改变行为**。改判 `REFACTOR_LATER` |
| `OutputManager` 补 `legacy_text_delta` 清 2 处 `_broadcast` 穿透 | 需要**新增**公开方法 = 新增抽象，且会把 v1 的 `on_text_delta` 冻结为公开契约（类型化层开字符串 dynamic dispatch 洞）。本批"默认拒绝新增 Protocol/Facade"，判 `REFACTOR_LATER` |
| `ToolAdapter` 4 个零消费者转发成员删除 | 缩小**已文档化**的公开门面；需单独一批 + 全量回归。判 `REFACTOR_LATER`（已登记） |
| `ToolAdapter → ToolRuntime` 改名 / `core/adapter → Module/` 搬家 | 用户第 35/36 条明确禁止 |
| `Agent` 事件消费块抽 mixin | 属重构（新增文件/新 mixin），非删减 |

---

## 6. False-Dead Candidates（**必须有的护栏**）

**Challenger F 独立找到 8 条**（要求 ≥3），另 5 条由本 agent 复核后从前序审计的"可删"清单里撤回。

| # | 看起来 dead | 真实消费者 | 谁发现 |
|---|---|---|---|
| **FD-1** | `Agent` 的 46 个 `@property` 中静态计数为 0 的（如 `character_service`） | `core/input/server.py:337` 的 **`getattr(agent, "character_service")`**（字符串形式）⇒ REST 角色域。同型：`instance_repo`/`character_profiles`/`character_state_stream`/`character_runtime_registry` | F（**推翻前序审计**） |
| **FD-2** | `services["_tool_names"]` | 同函数相邻的 `_tool_count` 被 `core/tools/self_state/plugin.py:31` 经 `ctx.get` **动态读**（`ToolContext.get()` 不产生静态引用）；`_fsm_state`/`_session_msg_count` 同型，且有 `test_self_state_is_live.py` 时序守卫 | F |
| **FD-3** | `live2d_directive` 全链路（core 内 0 发射点） | `docs/electron-backend-protocol-v2.md:722` 明文「待接线，客户端需能容忍缺席」+ `contract.generated.ts` 帧 + `test_output.py:147` v2-only 门禁 + `todo.md:45` owner 裁定 | F |
| **FD-4** | `AgentInfra.{milvus,http,background_task_results}`（engine 侧 0 读者） | `test_public_surface.py:248-258` **逐项断言完整 field_names**（"dataclass 形状不得静默变化"）；`background_task_results` 的消费者在 core 侧（`upsert_pending`） | F |
| **FD-5** | `Module/database/graphdb`（54 行 ABC） | `Module/database/__init__.py:2,8` **eager re-export**（已发布面）+ `todo.md:311` owner 裁定 | F |
| **FD-6** | `run_in_background`/`children_ids`/`depends_on`/`progress`/checkpoint | `todo.md:16-23,317` owner 明文**作废**删除计划；`scheduler.py:557,575` 的 prune 循环（每 10s）**就是活读点** | F |
| **FD-7** | `ToolManager._inflight`（审计称"第二索引"） | 是 ARCH-INC-P2-03 修掉的真实取消漏洞的**修复件**（`manager.py:583` 取 `_tasks`∪`_inflight`） | F（**推翻前序审计**） |
| **FD-8** | `core/hitl.py`（半接线） | 生产 import（`_lifecycle_task_system.py:266`）+ `core/tests/test_event_dispatch_else.py:84-114` **读源码**断言 `_hitl_requests` 出现在固定窗口内（改名/重排即红） | F |
| **FD-9** | `AgentServer.run_repl`（真 0 代码消费者） | **文档承诺**：`CLAUDE.md:145`、`core/README.md:81,85`、`core/agent/README.md:20,77`、`agent.py:7`、`todo.md:350` 共 8 处 | 本 agent（F 判 UNKNOWN） |
| **FD-10** | `CognitiveLoop.DEFAULT_CHARACTER_ID` | `core/cognitive/README.md:69` 显式登记为公开常量 | 本 agent（A 判 PROVEN_DEAD） |
| **FD-11** | `character/*/config.yaml::voice` + `CharacterConfig.voice` | YAML 注释 + `character/README.md:36` 登记为「[占位]」；同族 `appearance` 注释写「预留给 Live2D/前端渲染」⇒ 按 `P3-01-RESERVED` 判别标准（**有明确未来用途 + 有注释说明**）属预留 | 本 agent（B 判 PROVEN_DEAD） |
| **FD-12** | `character/*/config.yaml::appearance` | 同上（`from_yaml` 不读，但注释明确"待启用"） | 本 agent（B 判 PROVEN_DEAD） |
| **FD-13** | `config.yaml::database.milvus_token` | `secret=True` ⇒ loader 显式跳过 YAML（**这一行今天就不可能生效**，删了零行为变更）——但属**部署/环境面**，且 `docs/database-storage.md` 需同步 | 本 agent（B 判 PROVEN_DEAD，改判 REFACTOR_LATER） |
| **FD-14** | `conftest.py` 的 4 个 `usefixtures` 字符串 fixture | 删除 = 整个测试层**存储隔离静默失效**（真连 Mongo/Milvus），不是"一条测试红" | D |
| **FD-15** | `frontend_contract` 顶层 114 个零消费者 `__all__` 名 / `core/schema/__init__.py` 的 `# noqa: F401` 面 | 消费者是三产物 + 17 golden frames + `make check-types`；**注释本身就是公开承诺的机器表达**（Batch 2 曾在此误删） | D |
| **FD-16** | `core/monitoring/__init__.py` 的 PEP 562 模块级 `__getattr__` | 全仓唯一的懒导出，存在理由是**打断结构性 import 环** | D |
| **FD-17** | `rest.Message.mongo_id`（Python 零读者） | wire 键 `_id` 是前端归档分页**游标**（`chatStore.ts:251,299`）+ `ArchivePanel.vue` 10 处主键 | D |
| **FD-18** | `ToolRequestMessage.sourceCallId`（只写不读） | 被 golden frame `ws-v2-tool.jsonl` 逐字节钉住（实测：内存内删除只有这一个帧漂移） | D |
| **FD-19** | `Module/*/config.py` 6 个 dataclass + `from_config()` | Module 库**公开 API**（在 `__all__` 里） | B |
| **FD-20** | `core/tools/vision/config.yaml`（0 字节） | 删它会触发 `manager.py:181` 的 warning（该空文件在**抑制一条 warning**），且违反 CLAUDE.md 的插件三元组约定 | B |
| **FD-21** | `scripts/capture_ws_trace.py`（DEPRECATED） | 被 `scripts/tests/test_capture_ws_trace.py` import | E |
| **FD-22** | `Module/tool/safety.py` 三张 provider→env 表 | **不等价**重叠（见 §5） | B |

> **F 的方法论自检**（本批最重要的一条约束，已写进报告与 todo.md）：
> **`getattr(agent, "<name>")` 让 46 个 property 的静态引用计数全部失效。**
> 任何「`.prop` 计数 = 0」的删除判据在本仓库**无效**。

---

## 7. Public API Impact

**公开面净删除：0。** 逐类统计：

| 面 | 删除前 | 删除后 | 说明 |
|---|---|---|---|
| `__init__.py` re-export | 107 个包 / 889 名 | **不变** | 本批**未删任何 re-export**（D 的 `--runtime` 探测：`__all__` 每名可解析、裸重导出面缺 0） |
| `Agent` 公开 `@property` | 46（`agent.py` 自身） | **46** | 未增未删 |
| `ToolAdapter` 公开成员 | 24 | **24** | 4 个零消费者成员判 `REFACTOR_LATER`（已登记） |
| `AppConfig` 字段（含嵌套） | 44 | **37**（−7） | 全为**零读者**的 provider 字段（Batch 1.5 F8 的完整版）；`enabled`/`model`/`rate_limit_rpm` 保留 |
| CLI flag | 10 | **10** | 本批未删任何 flag；`--cli` 的 `run_repl` 判 KEEP |
| 契约模型 / wire 帧 | 198 模型 | **不变** | `make check-types` 绿；17 golden frames 零漂移 |
| env 变量 | ~90 | **不变** | `SENSEVOICE_URL` / `TTS_API_URL` / `LLM_API_BASE` 等**全部仍生效**（见 §8） |
| ToolAdapter/Registry 注册表 | 16/22/6·3·4·2·1·3 | **不变** | 运行期探测实测一致 |

---

## 8. Config / Env Impact

**删除的配置字段（7 个，均零读者）**：
`core/config/app_config.py` 的 `llm.base_url`、`llm.api_key`、`tts.base_url`、`tts.api_key`、
`tts.voices_config`、`stt.base_url`、`stt.api_key`。

**删除的 YAML**：
- `Module/config/providers.yaml` 的 `database:` 整段（2 键，`load_provider_defaults("database")` 0 调用）
- `character/elysia|cyrene/config.yaml` 的 `emotion.thresholds`（各 1 块）
- `config.yaml::database.milvus_token` **未删**（改判 REFACTOR_LATER：属部署面）
- `character/*/config.yaml` 的 `voice` / `appearance` **未删**（预留，已补 `P3-01-RESERVED` 标注）

**环境变量（重要，勿误读）——全部仍然生效**：

| env | 为什么仍生效 |
|---|---|
| `SENSEVOICE_URL` | 由 `providers.yaml::stt.sensevoice.url_key` 解析（不经 AppConfig） |
| `TTS_API_URL` | `Module/tts/config.py:16` 的库级 `ConfigField` + `Module/bench/bench.py:39` 的 `os.getenv` |
| `LLM_API_BASE` / `LLM_API_KEY` | `Module/llm/config.py` 的库级 shadow 名 |
| `MILVUS_TOKEN` | `DatabaseConfig.milvus_token`（env 路径完全未受影响） |

**生效路径的单一权威**：`providers.yaml` 的 `url_key`/`env_key` → `Module/_common/registry_helpers.py` 的
fallback 链。本批删除的是"能设置但无声失效"的**顶层影子路径**（Batch 1.5 F8 的原文处置建议即
"删字段或接上"；本批按 `Delete > Simplify` 取舍选删）。

**副产物**：`Module/config/providers.py:30` 的 docstring 合法值枚举同步删掉 `"database"`。

---

## 9. Persistence Compatibility

**持久化兼容零破坏。** 逐面回答「旧记录还能读吗？」

| 面 | 本批是否触碰 | 结论 |
|---|---|---|
| Mongo 集合/字段 | **未删任何字段** | ✅ 旧文档照常读。`script_drift` 的集合声明未改；`AppConfig` 是内存配置不是 Mongo |
| Mongo 索引声明 | 未改 | ✅ `EXPECTED_INDEX_OPTIONS` 的 `idempotency_claims` 拼错**已登记为 B3-CB-2**，未顺手改（避免夹带行为变更） |
| WAL（`task_result_wal.jsonl`） | 未触碰 | ✅ |
| `ServiceState` / `session.character_state` | 未触碰 | ✅ |
| checkpoint 旧键 `parent_id` | 未触碰（只改文档描述） | ✅ 兼容读取保留（`task.py:225`） |
| character YAML | 删 `emotion.thresholds` | ✅ 未知顶层键被 loader 忽略；`idle_expressions` 保留 |
| 契约层字段 | **未删任何字段** | ✅ 17 golden frames 逐帧零漂移 |

---

## 10. Test Cleanup

**测试侧删除：0 个测试。**

subagent C 的独立结论：本仓测试侧**没有可删的用例**——0 死 import（4838 名字全解析）、
0 dead patch target（`pkgutil.resolve_name` 复核 + 执行级反证）、**0 零消费者 fixture（167/167）**、
0 marker 错标（快慢混标 8 种组合逐一 collect 验 0）、0 可合并重复（121 指纹组逐条复核）、
0 与 `core/**/tests/` 重复的文件。唯一 `PROVEN_DEAD` 是那个零 import 的词表模块（已删）。

C 还用 `sys.addaudithook` 做了**执行级**验证：`unit or service` 5949 条跑完，**无任何到真实外部
服务的 connect**（命中的 socket 全是进程内 `aiohttp TestServer` 回环）——marker 分层不是「声明」，是实测。本批对测试侧的全部改动是**修正**而非删除：

| 文件 | 改动 | 原因 |
|---|---|---|
| `core/character/tests/test_runtime.py` | `test_flush_sets_dirty_false_and_persist` → `test_flush_persists_state` | 去掉已删字段 `dirty` 的断言，**保留**真正的不变量断言（flush 真的调 persist 回调） |
| `core/agent/tests/{conftest,test_lifecycle_steps,test_service_state_reporting,test_state_single_authority}.py` | 9 处打桩目标 `_cognitive._tts_mgr` → `_cognitive.tts_manager` | 跟随 S-1；同时去掉失去理由的 `# noqa: SLF001` |
| `core/tests/test_doc_counts.py` | 参数化列表加 `README.md` | **扩范围**（见 §11），非删除 |
| `core/tools/tests/test_write_file.py` | docstring 里的 `_sanitize_session_id` → 公开名 | 原文把私有别名说成"权威那一份" |

规模：测试侧 +20 / −16 行（净 +4）。

---

## 11. Documentation Cleanup

只清**可证伪的失真**；`docs/review|progress|superpowers|archive|completed|legacy` 全部按
「历史执行记录」保留（它们是"当时的判断"的证据，即使内容已过时）。

| 类别 | 条数 | 代表 |
|---|---|---|
| **死链** | 9 → **0** | `docs/architecture/invariants.md` 3 条漏 `../` 层级 + `../todo.md`×3 / `../CLAUDE.md` 少一层；`core/README.md` 的 `task/README.md`（58 份 README 唯一死链）。全仓活跃 md 重扫后 **0** |
| **表格结构缺陷** | 1 | `invariants.md` 一行缺 `\|` 分隔符导致列错位 |
| **改名未回写**（`core/task` → `core/tool_runtime`） | 6 文件 | `docs/testing.md:367` 的 `pytest core/task/tests/ -v` 是**跑不通的命令**；`core/README.md` 6 处 `TaskScheduler`→`ToolCallDispatcher` 等 |
| **可证伪的数字** | 4 | 根 README「39 个 @property」→ 46；测试统计 3804 → 6003（脚本实测）；「~22min」→ 去数字；TTS 语气词「22 种」→ 指向单源（实测 24） |
| **字段表三错** | 1 表 | `docs/database-storage.md` 的 Task 检查点：类型名错、`parent_id`→`request_id`、`result` 根本不在序列化键里 |
| **"机制不存在"的描述** | 1 | `Module/tool/README.md` 的「背压控制：`asyncio.Queue(maxsize=1024)`」——该机制**不存在** |
| **不存在的字段名** | 1 清单 | `core/debug/README.md` 的 `info.*` 清单大多不存在（按代码重写 + 守卫验证 0 残留） |
| **不存在的 LRU 保护** | 2/3 条 | `core/character/README.md`：`dirty`（已随本批删除）、`_session_binding_index`（从未接线且已移除）；真实保护只有 `_busy` |
| **指向已删符号的注释** | 4 处 | `archive.py` CR-04 说明（保留 **why**，补现状）、`test_write_file.py` docstring |
| **门禁登记缺失** | 1 | `invariants.md` §7 补登记 `test_doc_counts.py` 的假保障（现已修范围，故此条改为"已修"记录） |
| **磁盘残留** | 9 文件 | `Module/logger/` 的陈旧 `__pycache__`（包已从 git 删除，`c2602876`） |

**顺带做的门禁加固（1 处，含变异验证）**：
`core/tests/test_doc_counts.py::test_property_count_matches_code` 此前只覆盖 `CLAUDE.md` +
`core/README.md`，**不含根 `README.md`**，且正则只认粗体形态 ⇒ 根 README 的数字**长期无人核对**
（写 39，实际 46）。本批加入参数化列表 + 统一格式，并做**变异验证**：把 46 改成 45 →
`FAILED …[README.md]`（exit 1）；改回 → 5 passed。**即这道门禁对根 README 真的会红。**

---

## 12. Challenger Findings

| 分类 | 条数 | 内容 |
|---|---|---|
| **confirmed deletion** | **1** | `_mongo_available`/`_milvus_available`——F 唯一敢签字的 TRUE_DEAD（与 A 独立一致） |
| **blocked deletion** | **8** | FD-1..FD-8（F 独立拦下），其中 **3 条推翻了前序架构审计**「可直接删」的判定（`_tool_names`、`ToolManager._inflight`、46 property 的静态计数判据） |
| **correctness bug** | **3 + 3** | F 给出 3 条（`ToolControlManager` docstring / `UPDATABLE_FIELDS` 双真相源 / `run_repl` 与 `run_interactive` 行为已分岔）；B 另给 3 条（preflight 假 FAIL / `EXPECTED_INDEX_OPTIONS` 拼错 / `pronouns` 永久读不出）。全部合并为 B3-CB-1..6 登记 |
| **accepted risk** | **1** | `Module/workflow_contract` 之外的包没有"公开面冻结"测试（`invariants.md` §7 已记） |
| **false positive** | **1** | F 复核「`UPDATABLE_FIELDS` 两份同名定义」——**在 HEAD 已不成立**（`core/storage/profile_store.py` 已不存在，真问题是"两份不同形态的白名单"）；A 也同样推翻了「`scheduler.py:23` 伪依赖」 |

**F 给删除执行者的硬约束**（本批全部遵守）：
- C-01 禁以静态 `.prop` 计数判 property；
- C-02 删 `services` 键前查 `ctx.get` + 兄弟键 + `test_tool_adapter.py:102`；
- C-03 `AgentInfra` 删字段必须同步改 `test_public_surface.py:248-258`（⇒ 本批不删）；
- C-14 删除 commit 后按 `check-arch → check-contract → check-timeouts → check-types → pyright → test-normal` 验收（本批已跑全序列）。

---

## 13. Commits

```text
521b31cb chore(agent): 删除只写不读的 _mongo_available/_milvus_available
5799546c chore(config): 删除 emotion.thresholds 死配置
716101c2 chore(config): 删除 AppConfig 7 个零读者 provider 字段
70fb53cc chore(config): 删除 providers.yaml 的 database 段
92ca19fa chore(adapter,workflow): 删除无消费者的适配器成员与转发壳
aeba5843 chore(embedding,stt,tts): 删除无消费者 helper、重复映射表与常量
280bee4d chore(storage): 删除被取代的 _apply_update 与 release_all_attachments
8fe6281b chore(input,memory): 删除重复 helper 与同义方法
f9681eee chore(character): 删除 registry 手工 flush 口与只写不读的 dirty 字段
c1c173bf chore(scripts,arch): 删除未用常量、兼容别名与引用不存在类的 docstring
65b75a04 chore(personality): 删除架构文档自述已废弃的 AWARENESS_COOLDOWN_SECONDS
a7ae2cbf docs: 修正改名/删除后失效的文档引用与计数（Batch 3.4 第一批）
fe4f0b61 refactor(agent): TTS 门控收口到 CognitiveLoop.tts_manager 公开口
02627a2e docs: 删除/更正模块 README 里"机制不存在"与字段名失真的描述（Batch 3.4 第二批）
0dabff65 docs(todo): 登记 Batch 3 发现的 6 个 correctness bug 与有意保留清单
7133031a docs: 清掉指向已删除符号的注释与 docstring 残留（Batch 3.4 第三批）
0937e85a test(docs): 把根 README.md 纳入 @property 计数门禁（含变异验证）
800e54da fix(input): 图片上传目录改为调用期解析，修复测试隔离失效
58ddcf05 chore(tests): 删除零 import 的泄漏词表 leak_lexicon.py
```

每个 commit **单一原因、可独立 `git revert`**。删除项按主题分组（每主题 ≤ 4 个对象），
无一 commit 混合"删除 + 修正"或跨不相关模块。

---

## 14. Worktree Verification

对每个 commit 跑了**该 commit 的 targeted 验证**（见 §4 末），并额外用**独立 checkout** 做了跨 commit 的
API 面回归：

```bash
git worktree add /tmp/b3base b78f0fcc          # 基线（删除前）
# 对比 AppConfig 字段数：before 44 / after 37
# 对比注册表条目数、__all__ 可解析性、裸重导出面（D 的 --runtime 探测）
git worktree remove --force /tmp/b3base
```

**AppConfig 字段数 44 → 37**（正是删除的 7 个，无意外连带删除）。
**运行期注册表**：`ALL_PLUGINS=16` / `_WS_HANDLERS=22` / `_BACKENDS` 6·3·4·2·1·3 / `contract models=198` /
`ServerMessage=43` / `ClientMessage=22` —— **与基线逐项一致**。

---

## 15. Test Results

| 档位 / 门禁 | 结果 |
|---|---|
| `make lint`（ruff） | **All checks passed** |
| `make pyright` | **0 errors, 0 warnings, 0 informations** |
| `make check-arch` | **通过**（R1 / R2 / R3 / R4 / R6） |
| `make check-contract` | **通过**（23 预留 / 4 已知缺口在册） |
| `make check-timeouts` | **通过**（R1–R4） |
| `make check-types` | **通过**（ts/schema/raw_ts + 17 golden frames + 54 错误码） |
| `make check-debug-auth` / `check-secrets` / `check-compose` / `check-env-lock` / `check-ci-isolation` | **全部 PASS** |
| `make check-test-sets` | **通过**（`\|Quick ∩ remainder\| = 0`、`\|Quick ∪ remainder\| = 5953 == \|Normal\|`、全量 6003） |
| `make test-quick` | **5949 passed, 2 skipped, 1 xfailed**（73s） |
| `make test-normal` | **5949 passed, 2 skipped, 1 xfailed**（74s） |
| **full suite**（含 slow，无界） | **5981 passed, 20 skipped, 1 xfailed**（424s / 7min04s） |
| import smoke | 全包 `walk_packages` 递归 import：**0 failures** |
| 隔离可信度（audithook 执行级） | `unit or service` 5949 条 **零外部 connect**（C 独立实测） |
| 测试残留落盘复现 | 修复前单跑图片用例真实目录 `441→442`；修复后 **delta=0** |
| 真实 CI | 见 §19（push 后） |

**删除前后的对比基线**：`b78f0fcc` 上 `make test-normal` 5949 passed（同数）；本批**没有删除任何测试**，
所以"测试变少"这条不可能掩盖回归——唯一数量变化是 `+1`（新增的参数化用例）。

---

## 16. Size / Complexity Delta（**记录，不是 KPI**）

| 指标 | 值 |
|---|---|
| commits | 19（11 删除 + 1 修复 + 1 简化 + 6 文档/门禁） |
| 文件变更 | 49 |
| LOC（代码+配置+测试，52 文件） | **+184 / −295 ⇒ 净 −111**。新增部分主要是把「为什么保留 / 为什么删」写成注释与文档更正（约 +100 行），以及测试侧 +20 行；**纯删除约 −240 行** |
| LOC（含本次执行记录） | +668 / −295（执行记录本身 +484 行，属文档产物，不计入清理收益） |
| 删除的文件 | 0（无整文件删除——本批没有可整删的模块） |
| 删除的符号 | **18 个对象**：9 个函数/方法、2 个 property、4 个 module 常量、1 个 dataclass 字段、1 个 services key、1 个兼容别名 |
| 删除的配置字段 | **8**（AppConfig 7 + `EmotionConfig.thresholds` 1）+ YAML 3 块（providers `database` 段 + 2 角色 `emotion.thresholds`） |
| 删除的 CLI flag | **0** |
| 删除的 re-export | **0** |
| 删除的测试 | **0** |
| public method delta | **0**（`Agent` 46 property 不变；`ToolAdapter` 24 成员不变） |
| 文档 | 9 条死链 → 0；11 类失真更正；1 处门禁扩范围（含变异验证） |

> **本批拒绝"为数字而删"**：`run_repl`、`DEFAULT_CHARACTER_ID`、`voice`/`appearance`、
> `ToolAdapter` 4 个零消费者成员、`config.yaml::database.milvus_token` 都是"看着可删"但被拦下的项。
> 若把它们删掉，LOC 会再降 ~120，但那会**缩小文档化的公开面 / 删掉预留**——本批选择不换这个数字。

---

## 17. Keep As-Is（被审计但决定保留）

| 模块 / 抽象 | 理由 |
|---|---|
| `Module/workflow_contract` 的 `AgentInfra`（含 3 个 engine 侧"死"字段） | 契约形状被 `test_public_surface.py` 逐项断言；`background_task_results` 消费者在 core 侧（设计如此） |
| `Module/database/graphdb`（54 行 ABC） | `P3-01-RESERVED` + eager re-export + owner 裁定（`todo.md:311`） |
| `core/tool_runtime` 的 background/DAG/progress/checkpoint 簇 | `P3-01-RESERVED` + owner 明文作废删除计划 + prune 循环活读 |
| `core/hitl.py` + `ToolEmitter` 流式上报面 | 生产 import + 源码级测试守卫 |
| `Module/bench/` | `P3-01-RESERVED`（手工基准工具）+ 已知且接受的 2 条 R1 例外 |
| `OutputManager.stream()` / `WorkflowEngine.resume()` / `cancel`/`pause`/`get_progress` | 有测试网与前端就绪能力，缺的是产品接线 |
| `FilePart`/`AudioPart`/`VideoPart` 预留 | `P3-01-RESERVED`（Phase 2） |
| `live2d_directive` 全链路 | 协议文档承诺 + 契约/门禁就绪 |
| `core/adapter` 层本身（LLMAdapter/TTSStreamManager/ToolAdapter） | 分层健康（前序审计已裁定"不值得大迁移"） |
| `Module/tool/safety.py`（618 行） | 前序评估明确"不拆" |
| `Module/config/preflight.py` 三张 provider→env 表 | 与 providers.yaml **不等价**重叠（gpt-sovits 用 url_key）⇒ 天真 DRY 会改行为 |
| 测试侧全部用例 | Challenger 与 C 独立结论：无可删项 |

---

## 18. Follow-up

**继续保留、本批未做**（按用户第 41 条，不误删、不偷做）：

```text
WAITING_APPROVAL live-lock
reminder persistence
plan_too_large semantics
task LLM provider H3
workflow model reporter
F5 STT provider-test asymmetry
/debug tts_ok/stt_ok semantics
prompt budget cross-path
provider portability
error protocol extension
architecture cleanup（ToolAdapter 转发面收窄 / OutputManager 窄语义方法 / 事件消费块 mixin）
```

**本批新增 follow-up（已登记 `todo.md` §「Batch 3 登记项」）**：

| id | 项 | 优先级 |
|---|---|---|
| **B3-CB-1** | `user_identity_profiles.pronouns` 可被合法 PUT 写成 `null` → 永久读不出 → `/api/me` 等全 500（**已复现**） | **高**（用户可见损害 + 不可自愈） |
| **B3-CB-2** | `EXPECTED_INDEX_OPTIONS` 键写成 `idempotency_claims`（真名 `idempotency`）⇒ 一条门禁对该集合永不生效；该表**无 AST 守卫** | **高**（假保障） |
| **B3-CB-3** | `preflight` 把可用配置判死 ⇒ `scripts/start.sh` 拒绝启动（**已复现**） | **高**（启动阻断） |
| **B3-CB-4** | `clean_db.PROTECTED_COLLECTIONS` 自称有测试守卫，实际没有 | 中 |
| **B3-CB-5** | 文档数字门禁的范围/正则漏洞（**已在本批就地修复根 README 一半**；`docs/architecture.md` 等仍未覆盖） | 中 |
| **B3-CB-6** | 两份语义不同的 `UPDATABLE_FIELDS` 白名单，两侧静默 `continue` | 中 |
| — | `ToolAdapter` 4 个零消费者转发成员 + `OutputManager` 2 处 `_broadcast` 穿透（需窄语义方法） | 低 → `REFACTOR_LATER` |
| — | Mongo 3 个 write-only 字段（`attachments.sha256` / `background_task_results.delivered_at` / `message_archive.metadata.incomplete`）——需**一次真实 Mongo 抽样**才能从 `LIKELY_DEAD` 升级 | 低 |
| — | `ConfigLoader(env_path=…)` 形参 / `config.yaml::database.milvus_token` | 低 |
| — | `core/tools/tests/mock_plugin.py`（173 行）+ `use_mocks` 死支路（7/7 调用点显式 False，从未执行；需**原子**删除） | 中 |
| — | 3 个 0 字节空测试包 `Module/workflow_engine/tests/{infra,learning,safety}/__init__.py` | 低 |

**F1–F17 的权威清单位置**：`docs/review/2026-09-17-Batch1.8-user-visible-terminal-semantics-执行记录.md`
的 Follow-up 表（本批在 `todo.md` 补了**指针**，因为 `docs/README.md` 宣称 `todo.md` 是"待办与已知问题总表"
而它此前 0 命中 —— 这是 E-16 发现的登记缺口）。**不复制内容，避免两处漂移。**

---

## 19. 完成标准自检（对照用户 §70）

| 标准 | 状态 |
|---|---|
| 所有删除都有 consumer proof | ✅ §3 + §4（每条都跑了 10 类检查） |
| 未把 re-export 当 unused 删除 | ✅ 删除 re-export **0 个**；D 的 `--runtime` 探测前后一致 |
| 未把 plugin side-effect import 当 unused 删除 | ✅ 未删任何 import；`ALL_PLUGINS=16` 不变 |
| 未破坏旧持久化数据兼容 | ✅ §9（未删任何 Mongo/WAL/契约字段） |
| 未删除 restart/recovery-only 活代码 | ✅ `_mongo_ok`/`_milvus_ok`、WAL、pending journal、checkpoint 兼容读取全部保留 |
| Challenger 至少独立复核高风险删除 | ✅ F 独立拦下 8 条、推翻前序审计 3 条；与 A 独立一致 1 条 |
| dead code 与 correctness bug 明确区分 | ✅ 6 条 bug 单独登记，**未在删除 commit 里偷修** |
| public surface 变化明确记录 | ✅ §7（公开面零删除；`AppConfig` 字段 44→37 明确列出） |
| 每个 commit 独立 worktree 可验证 | ✅ §13/§14（单一原因、可独立 revert） |
| quick + normal + full/static gates 全绿 | ✅ §15 |
| 真实 CI 绿 | ❌ **未完成**：本机无 git 凭据，无法 push（见 §19）。**这是本批唯一未达标项** |
| git status 干净 | ✅（push 前最后确认） |
| 没有为了"删得多"进行无价值改动 | ✅ §16 记录了 6 条被拦下的"看着可删"项 |

**真实 CI：本环境无法完成——这不是「本地全绿」的替代品，是明确未完成项。**

19 个 commit 已在本地 `main`（领先 `origin/main` 20 个 commit）。CI 为 push-only
（`ci.yml` / `nightly.yml` 均只监听 `push: [main]`），结果须由 `ci.yml:backend`
（`make test-normal` + 全部门禁）与 `real-mongo` job 给出。

**push 在本机不可执行**（已实测，非未尝试）：

```
$ git ls-remote --heads origin main
fatal: could not read Username for 'https://github.com': ...
# remote = https://github.com/yukikaze-1/elysia-agent.git
# 无 credential.helper、无 ~/.git-credentials、~/.ssh/ 下无私钥（只有 authorized_keys / known_hosts）
# ⇒ HTTPS 与 SSH 两条路都缺凭据
```

⇒ **Batch 3 尚差「push 后确认真实 CI 绿」这一步。** owner 在本机补凭据后执行：

```bash
git push origin main
# 然后确认 ci.yml 的 backend / real-mongo 两个 job 均绿
```

在此之前，本文的所有「绿」**仅指本地实测**，不构成 CI 已绿的声明。
