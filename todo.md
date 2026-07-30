# 待做

## 2026-07-31 架构文档审查 — docs/architecture.md 与代码不一致

以下问题来自 `docs/architecture.md` 及其关联文档与代码的对比审查。按严重性降序排列。

### 高

| # | 文件 | 行号 | 摘要 | 失败场景 |
|---|------|------|------|----------|
| 1 | `docs/architecture.md` | 560 | **`SessionMemory.snapshot()/restore()` 不存在** — 文档声称存在这两个方法，实际代码中 `session.py` 没有 `def snapshot` 或 `def restore` | 测试用 `session.snapshot()` 会触发 `AttributeError` |
| 2 | `docs/architecture.md` | 242-252 | **AppConfig 缺少 `database: DatabaseConfig` 字段** — 文档列出 9 个字段，实际代码 `app_config.py` 有 10 个（`database` 缺失） | 用户依赖文档了解配置结构会完全错过 `database` 配置节 |
| 3 | `docs/architecture.md` | 694-730 | **4 个角色版本路由未文档化** — `debug_routes/character.py` 注册了 4 条路由（l2-versions, l2-rollback, character-versions, character-rollback），不在文档路由表中。文档说"41 条路由"，实际 45 条 | API 使用者不知道这些端点存在 |
| 4 | `docs/architecture.md` | 530 | **AwarenessTrigger 冷却时间: 文档说 1h，实际 5 分钟** — 文档："距上次触发超过 1h"。实际: `AWARENESS_JUDGMENT_COOLDOWN = 300`（`awareness.py:20`）。`AWARENESS_COOLDOWN_SECONDS = 3600` 定义在 `engine.py:40` 但从未被使用 | 依赖文档理解 C9 触发频率的开发者和文档不符 |
| 5 | `docs/architecture.md` | 457 | **SentenceBuffer 默认分隔符包括 `～…〜` 但代码没有** — 文档："默认分隔符: 。！？!?\n～…〜." 实际默认值: `_HARD_DELIMITERS = frozenset("。！？!?\n")` + `_DEFAULT_DELIMITERS = _HARD_DELIMITERS \| {"."}` = `。！？!?\n.`（7 字符，非 10 字符） | `～`, `…`, `〜` 不会按文档所述触发分句 |
| 6 | `docs/architecture.md` | 231 | **vectordb 文档说"milvus"唯一，`pinecone`/`qdrant` 已注册** — 文档表格只列出 milvus。实际: 3 个 backend 注册（milvus + pinecone + qdrant），后两者抛 `NotImplementedError`。与其他模块（stt 的 openai/deepgram）不一致 | 文档模式不统一，遗漏注册的 stub |

### 中

| # | 文件 | 行号 | 摘要 | 失败场景 |
|---|------|------|------|----------|
| 7 | `docs/architecture.md` | 663-678 | **`action` 和 `token_usage` WS 消息类型未文档化** — `WebSocketChannel.on_action()` (websocket.py:96) 和 `on_token_usage()` (websocket.py:159) 实际发送，但不在 6.2 协议表中 | 前端开发者会漏接这两个消息类型 |
| 8 | `docs/architecture.md` | 668-669 | **`audio`/`audio_end` 报文格式字段缺失** — `audio` 报文含 `is_final: False`，`audio_end` 含 `format: "wav"`，但文档未列出 | 前端解析可能忽略这些字段 |
| 9 | `docs/architecture.md` | 957-1037 | **`core/status/` 和 `core/storage/` 目录从文件树中缺失** — 两个目录（AgentStatus 接口层 + Storage repository 层）完全不在第 12 节文件结构中 | 开发者理解项目结构时遗漏重要组件 |
| 10 | `docs/architecture.md` | 607 | **ToolAdapter 公共 API 文档用 `load_all()` 实际用 `start()`** — 文档: `tool_adapter.load_all(ALL_PLUGINS, plugin_ctx)`。实际: `ToolAdapter.start()` 是公共入口 | 开发者调用 `load_all()` 会报错或方法不存在 |
| 11 | `docs/architecture.md` | 755, 1056 | **第 6.4 节说 5 个 Pinia store，第 12 节说 6 个** — 文档内部矛盾: 6.4 列出 5 个（缺 `debugStore`），12 节说 6 个。实际代码 6 个 | 读者困惑实际 store 数量 |
| 12 | `docs/architecture.md` | 1112 | **并发任务表称 19 个调用点但编号只有 18（缺 #6）** — 第 13 节表格编号 1,2,3,4,5,7,...19，缺 #6，标题却说"19 个调用点"。实际 18 个条目 | 编号混乱，#6 缺失原因不明 |
| 13 | `docs/architecture.md` | 767-786 | **config.yaml 文档块缺少 database/vision/wake_word/live2d 节** — 第 7.1 节 YAML 示例省略了 4 个完整配置节 + 若干字段 | 文档作为配置参考不完整 |
| 14 | `docs/architecture.md` | 236 | **SystemPromptManager 未从 `Module/system_prompt/__init__.py` 重导出** — 文档表格将其列为可导出类型，实际 `__init__.py` 没有 `SystemPromptManager`，必须通过完整子模块路径导入 | `from Module.system_prompt import SystemPromptManager` 会失败 |
| 15 | `docs/architecture.md` | 871-883 | **Agent.stop() 步骤顺序不一致** — 文档列出: persistence(10) → db_health(11)。实际 `_SHUTDOWN_STEPS`: `stop_db_health`(#7) 在 `stop_persistence`(#14) 之前 | 文档与实际 cleanup 顺序不符 |

> **CI 配置**：`Module/task_system/` 尚未完善、非当前开发重点，`test-quick` 和 `test-normal` 已通过 Makefile 跳过该目录（`--ignore=Module/task_system/tests`）。完整跑 `test-full` 或手动指定路径时仍会执行。

## 2026-07-31 其他文档审查 — docs/*.md、CLAUDE.md、README.md、Module/README 等与代码不一致

审查范围：`docs/` 下除 `specs/`、`plans/`、`superpowers/` 之外的全部文档、根级 `README.md`/`CLAUDE.md`、`Module/` 和 `core/` 各 README、`frontend/README.md`。方法：逐文件与代码交叉验证。

### 高

| # | 文件 | 行号 | 摘要 | 失败场景 |
|---|------|------|------|----------|
| 1 | `static/debug.md` | 全文 | **文档完全过时** — 描述的是旧版独立 HTML debug 页（~2100 行、单文件、无构建步骤、内联 JS），实际 `static/debug.html` 仅为 15 行 Vite 构建产物入口（加载 module JS + CSS bundle）。文档的"单文件 HTML + CSS + JS，无依赖"、"ES5 兼容 JavaScript，无框架依赖"等描述全部错误 | 用户按文档理解 debug 页面架构和实现会得到完全错误的信息 |
| 2 | `CLAUDE.md` | 292-293 | **列出不存在的前端 composable 文件** — 树结构中列出 `useAudio.ts`（TTS 播放控制）和 `useMic.ts`（麦克风采集封装），同时缺少实际存在的 `useAbortController.ts` 和 `useImageUpload.ts`。实际 composables 目录含 4 个文件：`useAbortController.ts`、`useImageUpload.ts`、`useLightbox.ts`、`useWebSocket.ts`。音频/麦克风逻辑已移至 `audioStore.ts`（Pinia store） | 开发者按 CLAUDE.md 查找 `useAudio.ts`/`useMic.ts` 找不到文件；AI agent 按 CLAUDE.md 生成代码会引用不存在的模块 |
| 3 | `CLAUDE.md` | 104 | **列出已删除的 `core/agent/status.py`** — 树结构中仍列出 `status.py`（虽有注释"已提取为 core/status/ 包"），但该文件已完全不存在，非薄包装或重导出 | 开发者和 IDE 导航会尝试访问不存在的文件路径 |
| 4 | `README.md` + `Module/llm/README.md` | README:327, llm:85 | **MiMo 列为 LLM 后端但未注册** — 两处文档均列出 `mimo` 为可用后端，但 `Module/llm/registry.py` 中无 `mimo` 条目。`Module/config/preflight.py` 的 `_LLM_PROVIDER_ENV` 虽定义了 `MIMO_API_KEY`，但 factory 无法创建 | 用户尝试 `LLMFactory.create("mimo/...")` 会因 registry key error 而失败 |
| 5 | `Module/llm/README.md` | 50 | **`test()` 返回类型错误** — 文档写 `async def test(self) -> bool: ...`，实际 `protocol.py:69` 签名为 `async def test(self) -> dict:` | 开发者写 `if await backend.test():` 会得到 `dict` truthy（恒真），错过 `{"ok": False}` 的失败检测 |

### 中

| # | 文件 | 行号 | 摘要 | 失败场景 |
|---|------|------|------|----------|
| 6 | `database-storage.md` | 5-10, 23 | **遗漏 `character_versions` 集合 + 嵌入模型名不准确** — 主表只列 4 个集合（缺第 5 个 `character_versions`，定义在 `character_version_repo.py`，用于 L2/C6 版本历史）。嵌入模型写 `BGE-large-zh-v1.5`（应为 `BAAI/bge-large-zh-v1.5`），且未提及 `openai/text-embedding-3-small` 是主后端（`embedding_provider.py:105-112`），BGE 为 fallback | DBA 无法从文档了解完整集合清单；配置嵌入管线的用户会使用错误的模型名且不了解分层 fallback 策略 |
| 7 | `core/prompts/README.md` | 15 | **`PersonalityCompression.j2` 加载方写错** — 文档声称由 `core/personality/engine.py` 加载，实际加载代码在 `core/personality/compression.py:191-196`（`PromptManager.render_macro("PersonalityCompression.j2", ...)`） | 开发者追踪 PersonalityCompression 模板的消费者时会在 engine.py 中找不到任何引用 |
| 8 | `README.md` | 289, 416, 428 | **3 处计数/时间数据过时** — (a) 预检项数"14 项"，实际 `--check --full` 执行 16 项（11 基础 + 5 连通性）；(b) 测试计数"1869 个测试"，实际 `pytest --collect-only` 返回 1927（差 58）；(c) 全量测试耗时 `README` 写"~20min"，`CLAUDE.md:28` 写"~22min"，不一致 | 引用这些数字的报告/changelog/外部文档会传播错误数据 |
| 9 | `companion-roadmap.md` | 64 | **关键文件列表含已删除文件 + 错误路径** — 列出 `static/js/main.js`、`static/js/persistence.js`、`static/css/chat.css`（均已随 Vue 2→Vue 3 迁移删除），且 `core/prompt_assembler.py` 应为 `core/adapter/prompt_assembler.py`，`Module/input/server.py` 应为 `core/input/server.py` | 虽文档已有废弃声明，但关键文件列表仍然误导想了解当时实现的新读者 |
| 10 | `database-storage.md` | 91 | **`message_archive` 索引表不完整** — 文档只写 `_id`（MongoDB 内置默认降序扫描），实际 `message_repo.py:70-71` 额外创建了 `session_id` 索引和 `(session_id, _id)` 复合索引 | DBA 规划查询时不知道已存在的索引，可能创建重复索引或写出次优查询 |

> **补充**：以上为 top-10 精选。完整审查中还发现以下较次要问题（不纳入编号）：
> - `docs/testing.md`：**测试数量全面过时** — 总测试声称"约 1800"，实际 1927（+7%）；unit ~1030→1112；service ~710→728；integration ~37→35；slow ~51→52。**测试文件索引严重不全** — Module/ 层声称 39 文件实际 44；core/ 层声称 31 文件实际 52（偏差 +68%）；列出 2 个不存在的测试文件（`test_message_archive.py`、`test_session_archive_integration.py`）；遗漏 `core/schema/tests/`、`core/storage/tests/`、`core/cognitive/tests/` 中多个文件
> - `data-structure-survey.md`（2026-07-15）：多处行号偏移（代码变更所致）、`AppConfig` 缺 `database` 字段（同 architecture.md 发现 #2）、常量定位错误（6 个常量中 4 个不在 engine.py）、`AgentState`/`EventType` 定义位置不精确（列 `__init__.py` 而非实际定义文件 `agent.py`）
> - `companion-prompt-guide.md:11`：11 vs 12 个 PromptSection（同 architecture.md 发现）
> - `companion-prompt-guide.md:150`：交叉引用缺失 `archive/` 路径段
> - `companion-prompt-guide.md:187,222,245,256`：4 处 `memory.j2` 应为 `c4_memory.j2`
> - `Module/README.md:5` + `Module/tool/README.md:195`：Python 版本多处不一致（3.13  vs CLAUDE.md 的 3.11+）
> - `core/README.md` + `core/adapter/README.md`：多处文件行数过时（数十行偏差）
> - `CLAUDE.md`：styles 缺 `message-layout.css`，utils 缺 `format.ts`
> - `docs/completed.md:1346`：引用不存在的 `core/memory/message_archive.py`
> - `frontend/README.md:382`：composables 数量"3 个"，实际 4 个（缺 `useAbortController.ts`）
> - `docs/code-review-2026-07-22.md`：旧路径残留（`Module/input/` 应为 `core/input/`）、项目规模夸大（"~1.4M 行" 实际 Python ~69K）、#3/#6 等 issue 已修复但仍列为严重 bug

## 已知 Bug

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 中 | **LLM 有时不输出 <mood> 标签** | deepseek-v4-flash 遵循结构化输出不稳定，部分轮次 `<mood>` 缺失，回退到关键词 fallback。需强化 prompt 或在 F2 加更强制性的约束 |
| 中 | **INTERRUPT + TTS 同时触发时 emoji 合并到上一气泡** | `finalizeDelta` 和下一句 delta 的时序竞争。Phase 3.5 延后，需单独 mini-spec |
| 低 | **语音识别容易被环境音触发** | VAD 对环境噪音敏感，需调整 aggressiveness 或加降噪 |
| 低 | **工具执行超时需手动测试** | `_execute_tool` 的 60s 超时，需构造慢工具场景验证 |
| 低 | **`test_integration_global_constraints_respected` 偶发失败** | `Module/task_system/tests/integration/test_delegation_preparer.py`: 全量跑偶发 FAIL，单独跑 PASS，疑似 TaskSystem 集成测试时序/资源竞争 |
| 低 | **`test_multi_step_task` 偶发失败** | `Module/task_system/tests/integration/test_planner.py`: DeepSeek API 偶发返回截断 JSON (`Unterminated string`)，LLM 输出不稳定导致 `PlanParseError` |
| 中 | **TTS 密集短句导致 wait_all 阻塞** | TTS 连续 5+ 短句密集入队时 MiniMax 部分请求无响应，`_transition_idle()` → `wait_all()` 永久等待，FSM 卡在 SPEAKING。已缩短 read timeout 120s→30s 缓解，根因（wait_all 无超时）待重现后修复。 |
| 低 | **`flush_after_response` 超时层不匹配** | 内层 `loop.py:875` 每 task 5s，外层 `lifecycle.py:170` 整体 10s。3+ task 同时超时可能被外层截断。实际 pending 典型 0-2 个、亚秒级完成，impact 极低。修法：外层 timeout 改为 `max(10.0, len(pending) * 5.0 + 1.0)` |
| 低 | **`ConfirmModal` @keydown 在不可聚焦 `<div>`** | `frontend/src/debug/components/shared/ConfirmModal.vue:35` — overlay div 无 `tabindex`，依赖按钮聚焦后事件冒泡。弹窗内只有文字+两个按钮，点击文字不会失焦，现有行为已可靠。加固：加 `tabindex="0"` |

## 功能改进

- ✅ **项目路径统一管理** — 2026-07-31 已实现。新建顶层 `paths.py`（`ProjectPath(str, Enum)`，20 个枚举成员），项目根由 `ELYSIA_ROOT` 环境变量锚定。迁移 ~27 处硬编码路径（`__file__`-relative、cwd-relative、裸字符串），验证 3 项 grep check 全 clean。详见 `docs/superpowers/specs/2026-07-31-project-paths-unification-design.md`。

- ✅ **CognitiveLoop: 工具执行后注入结构化决策提示** — 2026-07-31 已实现。`loop.py:801`: `messages.append({"role": "system", "content": "[TOOL_EXECUTED]"})` 在 `_execute_tool_round()` 返回后、`continue` 前。`[TOOL_EXECUTED]` 作为显式模式切换信号，帮助 LLM 从"工具执行模式"切回"对话模式"。改动量 1 行。
- **README 截图**: 添加聊天界面 / debug 面板 / 启动流程截图，让新用户第一眼就知道项目长什么样
- **Live2D 集成**: Cubism SDK v5 需 TypeScript + npm + bundler 构建，当前仅 SDK 文件到位、默认关闭
- **poignancy 时间衰减**: rerank 含时效衰减但 poignancy 本身不变，长期不访问的记忆应逐渐降级
- **MiniMax SSE 真流式**: 已尝试并回退 (2026-07-10)。MiniMax SSE 每个 event 是独立可播 mp3（非增量片段），拼接导致重复，只取最后 event 又等于同步下载。待 MiniMax API 支持增量 SSE 或换方案（WebSocket）
- **MiniMax 异步长文本合成**: `t2a_async_v2`，适合 >3000 chars。需创建任务 → 轮询状态 → 下载文件
- **记忆导出/备份**: 支持 JSON 导出和恢复
- **记忆统计**: 按类型/年龄/poignancy 分布统计，接入 DebugInfo 仪表板
- **Claude 原生持久记忆**: `.claude/projects/-home-yomu-Agent/memory/` 目录为空，从未启用
- **MacroMemory + MacroReflector**: 每日合成日记条目。需新增 Milvus 集合 + MacroReflector 后台任务
- **CharacterEngine mood 升级**（原 EmotionEngine 升级）: LLM `<mood>` 标签驱动已实现。生理状态模型、时间衰减曲线、混合情绪、Amygdala LLM 补充等后续 Phase 待做。
- 唤醒词检测（openWakeWord）— 已完成实现，待办：
  - 测试唤醒词开关：启动服务端，打开前端设置面板，点击开关确认 ON/OFF 切换正常
  - 训练自定义唤醒词模型 (.onnx)：录制每个唤醒词 100-500 样本 → 用 openWakeWord Colab 训练 → 将 .onnx 文件导入 `model_paths`
- **性能 profiling + 优化**:
  - 时钟补上 TTS span（`_transition_idle` 里的 TTS 等待）
  - 性能回归测试（bench 模块加断言，CI 可跑）
- **角色切换**: 加载不同角色配置（名字、性格、发音人、prompt 模板），需独立 spec。涉及 CharacterConfig 重新加载、TTS voice lookup 逻辑修复（`voice` 字段当前是死代码）、CognitiveLoop 角色引用更新。`gpt_sovits_voices.yaml` 目前只有 `Elysia` 一个发音人
- **多角色 prompt 模板**: `PromptAssembler` 通过 `SystemPromptManager` + 12 `PromptSection` 渲染，多角色时加载不同 `CharacterConfig` 即可切换 section 数据源
- **页面加载优化**: Live2D iframe 加载 481KB JS 竞争主线程 → WS onopen 延迟 ~200ms。已加开关可关闭释放资源，开启时仍存在。方案: `loading="lazy"` / 静态 emoji 首屏 / 修复 K shader
- **Docker 化部署**: 多阶段构建 + docker-compose 编排（agent + SenseVoice + GPT-SoVits）。数据库已完成 docker-compose.yml（MongoDB + Milvus）
- **F2 代码块格式重新启用**: `f2_output_format.j2` 中 `<|code|>` 和 `<|markdown|>` 格式已注释。等需要代码/Markdown 渲染功能时取消注释即可启用
- **意图路由 + 渐进式工具披露**: 当前 13 个工具全量暴露给 LLM，每人次 ~1,643 schema tokens。大部分对话只用 2–4 个工具，~70% schema token 是浪费的。评估结论：13 个工具未到临界点，暂不做。触发条件：工具数 > 15 考虑规则分类，> 20 必须引入分类层，或累计 ≥ 3 次工具误用事件。推荐方案：关键词匹配分组（filesystem/web/memory/utility），零延迟零 token 成本，无命中则全量兜底。
- **CharacterEngine Phase 2**（原 PersonalityEngine Phase 2）:
  - CharacterReflector.j2 模板 ~140 行偏长，LLM 注意力可能被稀释，后续观察产出质量决定是否精简
  - C3 情绪传染规则动态化
  - C7 主动性动态化
  - L4→L3 情绪汇聚统计（需 CharacterEngine 改接口）
  - 用户话题检测触发 C9（目前只看状态存在+冷却）


## 代码清理

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 中 | `database/graphdb/` 不完整 | 只有 protocol.py，保留参考 |
| 中 | `OutputManager.stream()` 未使用 | `manager.py:63-88`，保留参考 |
| 低 | LoggerFactory 死代码 | `Module/logger/factory.py` 169 行，保留参考 |
| 中 | `Module/bench/` 位置不当 | bench.py 自注释"将移至 tools/bench/"，依赖 core/，后续考虑迁移 |
| 低 | **FilePart/AudioPart/VideoPart 预留代码** | `part.py` ~70 行 + `message_adapter.py` 3 个 helper + `Part` union。`message_adapter.py:188` 注释明确 Phase 2。暂保留 |

## 模块化改进

| 优先级 | 目标 | 说明 |
|--------|------|------|
| ~~低~~ ❌ | **`Module/tool/safety.py`（618 行）** | 评估结论：不拆 |

## 多模态 Phase 2

| 优先级 | 项目 | 说明 |
|--------|------|------|
| 中 | **AI → 用户图片输出** | 工具返回图片（WebFetch 截图、图片生成）+ LLM 生成图片 → OutputManager 广播 → 前端渲染。`ToolResult.images: list[MediaItem]` 已设计，需打通 CognitiveLoop → session → 前端链路 |
| 低 | **文件支持** | `type: "file"` content part 实现（上传 + LLM context + 前端渲染） |
| 低 | **音频/视频 Part** | `VideoPart`/`AudioPart` 已定义 dataclass，需实现全链路 |

## 健壮性 & 架构

| 优先级 | 问题 | 说明 |
|--------|------|------|
| **高** | `.env` 仍含真实 API Key | DeepSeek、MiMo、Tavily、MiniMax 的真实 key 仍在 `.env` 文件中。已有 `.env.example` + `setup.sh` + `app.py --check`，但 `.env` 不应提交真实密钥。建议：将 `.env` 中 key 替换为占位符，真实 key 通过 shell 环境变量或密钥管理器注入 |
| 低 | 单 Session 多客户端共享 | `session_id = "default"` 硬编码 |
| 低 | 会话管理 | 无法新建/切换/列出对话。SessionMemory 的 MongoDB 持久化已为此做好准备，需前后端联动 |
| **高** | **无数据库迁移机制** | MongoDB 索引在 `connect()` 时 ad-hoc 创建，Milvus collection 硬编码创建。若字段/索引/维度需变更，无 upgrade path。`memory_repo.clear_all()` 是破坏性操作无安全守卫 |
| 低 | **`character/cyrene/` 不可用** | 缺少 `config.yaml` 和 `personality.md`，仅含 GPT 参考材料，无法作为角色加载 |
| 低 | **依赖无版本上限** | `requirements.txt` 中 pydantic、openai、torch 等均无上限约束，上游 breaking change 可导致不可预期问题。`torch` 是 2GB+ 重依赖 |
| 低 | **`# type: ignore` + `Any` 类型清理** | **# type: ignore**：生产代码 32 处→已消除 9 处→剩余 23 处（二/三梯队）。测试文件 IDE 类型错误已批量修复 ~155 处（2026-07-29/30：NewType 包装、ToolAdapter 替换、type: ignore 补充）。**`Any` 类型替换**：core/ 下 63 处→已消除 ~44 处（属性声明/task/emit 签名/mailbox/_process_events）。**剩余不可消除**：`dict[str, Any]` JSON 容器（~12 处）、流式协议 `**raw_params: Any`+`AsyncIterator[Any]`（5 处）、`Coroutine[Any, Any, Any]` 通用协程（3 处）、`_embed_task: Task[Any]`（2 处）。详见 `docs/completed.md` 2026-07-29 第一、三条 + 2026-07-30 第一条。 |


## 来自 code-review-2026-07-22 的遗留问题

已修复 19/30，详见 [code-review 报告](docs/code-review-2026-07-22.md)。

| 优先级 | 问题 | 说明 |
|--------|------|------|
| HIGH | **#9 管理端点无认证** | `/shutdown`, `/restart`, `/debug/api/*` 绑定 0.0.0.0 无认证 |
| HIGH | **#10 SSE CORS 通配符** | `/debug/api/tasks/stream` 允许任意来源 |
| MEDIUM | **#11 /static/ 目录列表** | `show_index=True` 暴露文件列表 |
| MEDIUM | **#12 用户消息明文日志** | `ws_handler.py` INFO 级别记录用户文本前 100 字符 |
| MEDIUM | **#13 API Key 明文泄露** | `/api/providers` GET 返回完整 api_key |
| LOW | **#25 私有 _infra 访问** | `cognitive/tools.py` 直接访问 `TaskSystem._infra` |
| LOW | **#26 取消的 Task 永不等待** | `idle_monitor.stop()` cancel 后不 await |

## 来自 code-review-report-2026-06-22 的遗留问题

### 需独立 spec

| 类别 | 数量 | 说明 |
|------|------|------|
| 无障碍 (a11y) | 12 | `aria-*`、颜色对比度、`:focus-visible`、键盘导航、toggle 非 button |
| CSP/安全头 | 2 | CDN `integrity`、Content-Security-Policy |
| ScriptProcessorNode→AudioWorklet | 1 | 麦克风管线换新 API |

### 中低危（待排期）

| 问题 | 文件 |
|------|------|
| llm_adapter close fire-and-forget | `core/adapter/llm_adapter.py:140-143` |
| openai_tts test() 假阴性 | `tts/openai_tts.py:73` |
| TTS 取消+HTTP 流清理交互可能泄漏 | `tts/gpt_sovits.py:134-150` |
| 缺失测试覆盖 | DescribeImagePlugin (0)、CognitiveLoop 边界、OutputManager 错误隔离 |

## ~~测试质量问题 (2026-07-30 review)~~ ✅ 已修复

基于 `cfc48f6` 的全面审查，以下问题已确认并修复 (2026-07-30)。

### 假测试（已全部修复）

| 优先级 | 文件:行 | 问题 | 修复 |
|--------|---------|------|------|
| ~~高~~ ✅ | `core/tests/test_request_id.py`（原 `test_log_setup.py`） | **文件名完全误导** | 重命名为 `test_request_id.py`，docstring 更正为 "Tests for core/utils" |
| ~~高~~ ✅ | `core/tests/test_phase2.py:63` | **`assert result in (True, False)` 恒真** | 拆为两个带 mock 的 unit 测试，验证 true/false 两条路径 |
| ~~高~~ ✅ | `core/tests/test_phase2.py:361` | **`len(messages) > 1` 后门使断言无意义** | 删除 `or len(messages) > 1` 后门 |
| ~~高~~ ✅ | `core/tests/test_phase3.py:210` | **零断言** | 补上 `assert loop._fsm.state == AgentState.INTERRUPTED` |
| ~~高~~ ✅ | `core/tests/test_phase3.py:239-285` | **错误处理逻辑被本地重新实现** | 改为调用真实的 `_execute_tools_directly()`，验证 `ToolResult.fail` |
| ~~高~~ ✅ | `core/tests/test_phase3.py:651-662` | **try/except RuntimeError 吞掉唯一可能的失败** | 改为 `@pytest.mark.asyncio` + `await engine.push_idle_expression()` |
| ~~高~~ ✅ | `core/tests/test_phase3.py:972` | **只验证"没崩溃"** | 使用 `capsys` 捕获 stdout，验证 `captured.out.count("hello") == 1` |
| ~~中~~ ✅ | `core/memory/tests/test_consolidator.py:60` | **逗号尾缀测试没有逗号** | 输入改为 `'[{...},]'`（带尾缀逗号），验证返回 `[]` |
| ~~中~~ ✅ | `core/memory/tests/test_long_term.py:44` | **假重连测试** | 合并为 `test_delegates_to_repo_when_connected`，正确描述行为 |
| ~~高~~ ✅ | `core/adapter/tests/test_message_builder.py:147` | **用错 dict key** | `"text"`→`"content"`, `"image_url"`→`"src"` |
| ~~高~~ ✅ | `core/task/tests/test_task_scheduler.py:656` | **回调从未被验证** | 修正测试注释，诚实说明 mock LLM 不产生 tool_calls 时回调不被触发 |

### 弱断言和逻辑问题（已全部修复）

| 优先级 | 文件:行 | 问题 | 修复 |
|--------|---------|------|------|
| ~~中~~ ✅ | `core/tests/test_phase3.py:378` | **`in ("idle", "thinking")` 应改为 `== "idle"`** | 改为 `== AgentState.IDLE` |
| ~~低~~ ✅ | `core/memory/tests/test_consolidator.py:55` | **`isinstance(items, list)` 恒真** | 改为 `assert items == []` |
| ~~低~~ ✅ | `core/tests/test_clean_db.py:168` | **`>= 4` 应改为 `== 5`** | 改为 `== 5` |
| ~~低~~ ✅ | `core/tests/test_clean_db.py:19` | **Mock 返回 int 而非 DeleteResult** | 驳回 — `DocumentDB.delete_many` 返回类型就是 `int` |
| ~~中~~ ✅ | `core/monitoring/tests/test_token_tracker.py:55` | **多个测试污染项目数据目录** | fixture 中 `_pending_file` 指向 tempfile |
| ~~低~~ ✅ | `core/task/tests/test_task_scheduler.py:551,611` | **`_completed_count` 死代码** | 删除两处 `sched._completed_count = 5` |

### 重复/低价值测试（已清理）

| 优先级 | 文件 | 问题 | 修复 |
|--------|------|------|------|
| ~~低~~ ✅ | `core/task/tests/test_task_model.py:45` | **`test_can_retry_wrong_status` 被 `:117` 完全覆盖** | 删除冗余测试 |
| ~~低~~ ✅ | `core/memory/tests/test_long_term.py:146,171` | **text_fallback 两个测试几乎相同** | 合并为 `test_text_fallback_when_no_milvus_id` |
| ~~低~~ ✅ | `core/input/tests/test_server.py:65-92` | **shutdown/restart 只设状态变量，无 HTTP 测试** | 改为验证 route 已注册（集成测试已有 HTTP 覆盖） |

> 保留不动的低价值测试：`test_task_model.py` dataclass 默认值/字段赋值测试、`test_session.py` 字符串常量测试（无害 smoke test）。

### 覆盖率缺口

| 严重性 | 路径 | 影响 | 状态 |
|--------|------|------|------|
| ~~关键~~ ✅ | `_flush_after_response` 完全未测 | 关闭时内存持久化的关键路径 | 2026-07-30 修复：5 个测试覆盖空/已完成/正常/超时/异常 |
| ~~关键~~ ✅ | FSM 10 条有效转换未测 | EXECUTING/IDLE_BACKGROUND_TASK/INTERRUPTED 相关路径 | 2026-07-30 修复：新增 10 条 transition + 3 条 interrupt 测试 |
| ~~高~~ ✅ | `_prune_tasks()` 完全未测 | 任务周期性清理 | 2026-07-30 修复：5 个测试覆盖删除/保留/父子关系 |
| 高 | `dispatch_background()` 完全未测 | TaskScheduler 后台任务派发。**死代码**（唯一引用是注释说"不要用"），不补测试 |
| 高 | `execute_stream()` 完全未测 | ToolAdapter 流式执行。**一行 pass-through 委托**，无独立逻辑，不补测试 |
| ~~中~~ ✅ | `fake_llm.py` 逐字符流式不符合真实 LLM | 第二轮已修复：正则分词逐 token yield + 故障注入 |
| 中 | EventBus drain() 与认知循环中断竞态 | 中断时 TASK_RESULT 事件丢失。注：`drain()` 是**死代码**，从未被调用，不补测试 |
| ~~中~~ ✅ | `agent/tests/conftest.py` MagicMock 过于宽松 | `_fsm.state = MagicMock()` 是地雷 | 2026-07-30 修复：改为 `AgentState.IDLE` 真实枚举值，16 个受影响测试全部通过 |

## ~~测试质量问题 — 第二轮 (2026-07-30, f413a9f)~~ ✅ 已修复

基于 `f413a9f` 提交的集成测试 & E2E 审查，以下问题已确认并修复 (2026-07-30)。

### 假测试（遗留 — 已修复）

| 优先级 | 文件:行 | 问题 | 修复 |
|--------|---------|------|------|
| ~~高~~ ✅ | `core/tests/test_phase2.py:209` | **`assert result in (True, False)` 恒真（遗留）** | mock `MemoryRepository`，验证 true/false 两条路径 |
| ~~中~~ ✅ | `core/tests/test_phase2.py:194` | **`test_insert_stores_milvus_pk` 空操作** | 重命名为 `test_insert_assigns_id`，加 `assert id.startswith("msg_")` |
| ~~中~~ ✅ | `core/memory/tests/test_consolidator.py:223` | **仅防崩溃测试** | `isinstance`→`assert items == []`（FakeLLMBackend 无提取） |
| ~~低~~ ✅ | `core/adapter/tests/test_message_builder.py:126` | **仅防崩溃测试** | 加 `assert "[已调用: test]" in assistant[0]["content"]` |
| ~~低~~ ✅ | `core/memory/tests/test_consolidator.py:156,233` | **`isinstance(items, list)` 恒真（遗留）** | :156→`assert len(items) >= 2`，:233→`assert items == []` |

### 标记/风格不一致（已修复）

| 优先级 | 文件:行 | 问题 | 修复 |
|--------|---------|------|------|
| ~~中~~ ✅ | `core/input/tests/test_server.py:150,493` | **`@pytest.mark.service` 却调用 `agent.start()`** | → `@pytest.mark.slow` |
| ~~中~~ ✅ | `core/tests/test_phase3.py:1030` | **`@pytest.mark.slow` 但不调用 `agent.start()`** | → `@pytest.mark.service` |
| ~~低~~ ✅ | `core/tests/test_phase3.py:389,433` | **字符串 `"idle"` vs `AgentState.IDLE`** | 两处改为 `AgentState.IDLE` |

### E2E/集成测试覆盖缺口（fake_llm 已改进）

| 优先级 | 问题 | 状态 |
|--------|------|------|
| ~~高~~ ✅ | **fake_llm.py 逐字符流式不符合真实 LLM** | `chat_stream` 改为正则分词逐 token yield |
| ~~中~~ ✅ | **fake_llm.py 无故障注入** | 新增 `fault` 参数：http_429/http_503/timeout/disconnect |
| 高 | **E2E 测试在无 API_KEY 时静默跳过** | 17 个测试依赖真实 LLM，需 CI 环境配置 KEY。暂无修复（需基础设施配合） |

### 重复/冗余（已修复）

| 优先级 | 文件 | 问题 | 修复 |
|--------|------|------|------|
| ~~低~~ ✅ | `core/tests/test_phase2.py:1012,1039,1090` | 三个驱逐测试 | 合并为 `@pytest.mark.parametrize` 参数化测试 |
| ~~低~~ ✅ | `core/input/tests/test_server.py:44-56` | 路由验证 3 处复制 | 提取 `_make_app()` + `_find_routes_by_name()` 共享 helper |

### tempfile 泄漏（已修复）

| 文件:行 | 修复 |
|---------|------|
| `core/monitoring/tests/test_token_tracker.py:52` | `tempfile.mkdtemp()` → pytest `tmp_path`（自动清理） |
| `core/task/tests/test_task_scheduler.py:542,572,602` | `Path(cp).unlink()` → `shutil.rmtree(tmpdir, ignore_errors=True)` |

## 测试补充

- `core/input/audio_buffer.py` (3)
- `core/input/server.py` (4)
- `Module/output/console.py` (1)
- 并发压力测试 (4): 多 WS / 快速连续消息 / STT+文本同时
- 上述覆盖率缺口中标记为"关键"和"高"的路径

> 已完成的工作归档在 [docs/completed.md](docs/completed.md)。
