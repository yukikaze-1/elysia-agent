# 开发计划

## 已完成

### 2026-07-29: 前端功能回归 + 分句/消息同步修复

**前端 11 项功能回归**（Vue 3 重构后相对于旧 vanilla JS 的缺失）：
- 复制按钮：assistant 气泡 hover 显示"复制"按钮，点击复制全文
- ↓ 新消息浮动指示器：用户上滚后新消息到来时底部出现，点击回到底部
- 内心独白开关：toggle 联动 body class `inner-voice-hidden` 控制 CSS 显隐
- TTS 开关：`finalizeStream()` 入队前检查 `ttsEnabled`
- 转录 emoji：恢复情绪/事件 emoji 映射（HAPPY→😊, BGM→🎼 等）
- Session Token 统计条：`#session-token-bar` 显示累计 token（按模型分组）
- 历史图片渲染：`restoreFromServer()` 支持 `content[]` 中的 image parts
- 舞台指示拆分：中文括号动作描述（`(轻声地)`）独立气泡
- 图片灯箱：`useLightbox` composable，任意组件调用 `open(url)` 全屏查看
- 拖拽上传：`MessageList` `onDrop` 补上文件上传逻辑
- Stream bubble TTL：30 秒 TTL 清理 orphan 映射

**分句系统修复**（`core/cognitive/stream_processor.py`）：
- `_strip_pause_tags()`：在 `SentenceBuffer` 前剥离 `<#0.5#>` 标签（含跨 delta 尾部残缺标签），防止标签内 `.` 被误判为句子边界导致标签截断
- `flush()` 尾部 emoji 合并：纯 emoji 残留文本合并到前一 SENTENCE，避免 `♪` 等独占气泡

**Archive-Session 同步**（`core/agent/agent.py` + `core/input/debug_routes.py`）：
- `_purge_message_from_session(msg_id)`：从当前 session deque 移除匹配消息
- `archive_delete_handler` / `archive_clear_handler`：删除/清空 Archive 后同步清理 session 并 `save()` 持久化

**Reflector ISO 时间戳兼容**（`core/personality/reflector.py`）：
- `_reflect()` 中 `datetime.fromtimestamp()` 兼容 ISO 8601 字符串和旧 Unix float

**TTS 情绪映射**（`core/adapter/tts_adapter.py`）：
- `_mood_text_to_tts_emotion()`：LLM 中文心情自由文本 → MiniMax TTS emotion 参数 (happy/sad/neutral)

### 2026-07-28: AgentLifecycle 拆分 + EmbeddingProvider + Action Gate + LTM 驱逐修复

**AgentLifecycle 拆分为 mixin（522 → ~120 行）：** `lifecycle.py` 从手写 10 步 startup 序列改为数据驱动 `_STARTUP_STEPS` 循环（镜像已有的 `_SHUTDOWN_STEPS`）。4 个 mixin 文件：`_lifecycle_persistence.py`（持久化层启停）、`_lifecycle_services.py`（LLM/TTS/Output/Token 启停）、`_lifecycle_tools.py`（ToolAdapter/Scheduler/IdleMonitor 启停）、`_lifecycle_task_system.py`（TaskSystem/mailbox 启停）。模式与 CharacterEngine 拆分一致。

**EmbeddingProvider 提取（`core/memory/embedding_provider.py`）：** 将 `long_term.py` 中 72 行嵌入加载逻辑（懒加载、超时、OpenAI/HuggingFace fallback、线程池）提取为独立类。对外暴露 `ensure(wait)`、`embed(text)`、`cancel()` 三个方法。`LongTermMemory` 从 589 → ~520 行。

**Action Gate（工具调用决策校验）：** `CognitiveTools._gate_tool_calls()` 在 LLM 产出 tool_calls 后、dispatch 前校验工具名是否在注册表。任一未知工具 → 整轮 abort 注入 `GATE_REJECTED` failure messages。新增 `ErrorType.GATE_REJECTED`。

**LTM 驱逐修复 + mark_absorbed Milvus 清理：** `_evict_old_entries()` 重命名为 `_evict_overflow()`，去掉年龄驱逐只保留数量上限，加 Milvus 同步删除。`insert()` 中恢复驱逐调用（`enable_eviction` 开关门控）。`mark_absorbed()` 改为 async 加 `_write_lock` + Milvus 删除，消除僵尸向量累积。

### 2026-07-22: C6 说话风格动态联动 + CharacterState 共享修复

**C6 动态联动**：L3→L2 压缩完成后自动触发 C6 说话风格刷新。LLM 编辑模式，版本历史与 L2 共享 `character_versions` 集合（按 `content_type` 区分）。C6 模板支持条件渲染：非空时渲染动态文本，为空时回退 L1 默认。

**CharacterVersionRepository 通用化**：集合 `character_l2_versions` → `character_versions`，所有方法加 `content_type` 参数（默认 `"l2"`），版本号 per content_type 独立计数。

**CharacterState 共享修复**：`engine.state` 与 `session.character_state` 共用同一对象。engine 为 mood/性格状态的权威来源，session 负责持久化 + 对外读取。修复了 debug 面板 mood 与聊天页面不一致的 bug。

**其他**：mood 从 debug System 面板移入 CharacterEngine 面板；`clean_db.py` 新增 `character_versions` 和 `character_l2_versions` 清理；C5 Sensory 新增输入方式标注（文字/语音）；AwarenessJudge: C9 LLM 语义判定。

**code-review 修复（19/30）**：严重 Bug #1-#4 #8 (5)，静默数据丢失 #14-#18 (5)，前端 Bug #19-#21 (3)，资源泄漏 #22 #24 (2)，设计问题 #27-#30 (4)。跳过 #5(影响小) #6(TODO) #7(需设计) #23(并发度低)。剩余安全 #9-#13 + 设计 #25-#26 待后续。

**CharacterEngine Phase 2 收尾（2026-07-22）**：

| 条目 | 实现 |
|------|------|
| C6 说话风格动态化 | L2→C6 LLM 编辑模式（`_refresh_c6()` + `SpeakingStyleEdit.j2`），`_compress()` 后自动触发，版本历史 content_type="c6" |
| AwarenessTrigger | L2/L3 语义偏差检测 — LLM-judge 驱动（`_judge_awareness` + `AwarenessJudge.j2`），异步 fire-and-forget，缓存 + 冷却 + 展示次数三重门控 |
| L3→L2 梯度压缩自动化 | LLM 编辑模式 + 版本历史（`character_versions` 集合），`state_history` 计数/时间双触发 |
| ExpressionAdapter | Mood 升级为 LLM 自由文本（`<mood>` 标签），expression 直接透传 mood 文本，移除阻尼映射 |
| L2 永久存储 | `character_versions` 集合 append-only，不随 session TTL 过期 |
| C3/C6/C7 动态联动（C6 部分） | L2→C6 编辑模式 + 版本历史。C7 待后续 |

### 2026-07-14: 分句系统优化

全面重写 `SentenceBuffer` 和 `BlockFilter`，修复 2 个正确性 bug + 1 个安全 bug + 工程改进。

**SentenceBuffer (`core/sentence_buffer.py`)：**
- 删除死代码 `_split_pat`（编译从未使用的 regex）
- 修复 `_is_dot_boundary`：`3.14.` 末尾句号从误判为小数点改为正确切句（"前后都是数字"才认定为小数点）
- 缩写检测重写：固定 3 字符窗口 + 大小写敏感 → 向后扫描完整单词 + 大小写不敏感（23 个缩写）
- 新增 `max_buffer_length=2000`：无标点长文本超阈值时在空格处强制切分，防止 buffer 无限增长
- `_extract` 拆分出 `_consume_sentence`：连续分隔符合并逻辑独立成方法
- `_find_split` 改为单次 `enumerate` 遍历（替代逐分隔符 `str.find()`）
- `_HARD_DELIMITERS` → `frozenset`，风格统一
- 新增可选参数：`max_buffer_length`、`abbreviations`

**BlockFilter (`core/block_filter.py`)：**
- `_depth` → `_mode: Literal["markdown","code"] | None`：修复不同类型结束标签互相关闭的 bug（`</|code|>` 不再能关闭 `<|markdown|>`）
- `flush()` 安全修复：未闭合块内容直接丢弃，不再泄漏到 TTS
- 新增 `reset()`：配合 SentenceBuffer 生命周期
- `flush()` 尾部半截标签处理：`rfind("<|")` 替代 `_BLOCK_START_RE.sub()`
- `build_parts_with_blocks()` 修复：块按原文位置交错插入，不再全部追加到末尾
- `_CODE_START_RE` 放宽为 `[A-Za-z0-9_+.-]*`

**集成 (`core/cognitive_loop.py`)：**
- `SentenceBuffer` 和 `BlockFilter` 改为每轮 `process()` 新建（赋给 `self._sentence_buf` / `self._block_filter`），替代 `__init__` 中一次性创建
- `_handle_interrupt()` 新增 `self._block_filter.reset()` + None-guard

**测试：**
- `test_sentence_buffer.py`：新增 7 个 test（3.14. 末尾句号、缩写大小写、max_buffer 强制切分），72 test 总计
- `test_block_filter.py`：新建 18 个 test（过滤、flush 安全、跨 chunk、标签匹配、reset、parts 排序）
- `test_phase3.py`：3 个 test 适配新生命周期（`_sentence_buf` → `None` at init）

**文档更新：** `docs/architecture.md` §4.3/§4.6, `core/README.md`, `docs/frontend-api.md`

### 2026-07-14: 不稳定测试排查与修复

从 10 failed → 1 failed（1543→1552 passed）。修复 7 个测试，分 4 类根因：

**确定性问题（5 个）：**
- `test_ok_returns_latency`: `ok_check(self)` 需要 self 但 `make_test_method` 内部 `health_fn()` 不传参 → TypeError 被静默吞掉 → 移除多余 self
- `test_no_memories_when_empty`: Prompt 中文化后断言仍检查英文 "No specific memories" → 改为 `"<memory_bank>" not in system`
- `test_stop_cancels_cognitive_task`: `cancel_current()` 把 `_current_task` 置 None 后才检查 → 保存 task 引用再 stop
- `test_invalid_json_returns_error_message`: `session` 消息后还有 `capabilities` 推送竞态 → 循环排空启动消息
- `test_character_personality_injected`: `personality.traits` 不再直接入 prompt（走 PersonalityEngine + base_personality）→ 改用 base_personality

**状态污染（2 类根因）：**
- **`load_dotenv()` 全局污染**: test_phase1.py（9 处）和 test_phase3.py（2 处）直接调 `load_dotenv()` 注入 `MILVUS_URI`，后续测试连上真实 Milvus（35 条旧数据）。修复：`os.environ.setdefault("DEEPSEEK_API_KEY")` 替代
- **`SafetyProfile` 类属性变异**: `ToolManager.load_all()` 从 config.yaml 覆盖类级 `read_dirs`，后续 `ReadFilePlugin()` 实例受影响。修复：断言改为非空检查而非精确值
- **`EnvManager.ensure_dotenv()` 副作用**: `ConfigLoader.load()` → `ensure_dotenv()` 加载 .env 全部变量。`TestAppConfig` 修复：伪装 `_loaded = True` 跳过

**防御性修复（3 个）：**
- `test_collect_memory_counts`、`test_ltm_list_and_insert`、`test_ltm_clear_all`：绝对计数断言 → 相对增量，即使 LTM 有预存数据也能正确验证

**残留（1 个）：**
- `test_touch_resets_timer`: `IdleMonitor.__init__` 中 `asyncio.get_event_loop().time()` 在全量 suite 中偶现失败。已加 `time.sleep(0.001)` 缓解。建议后续将 `IdleMonitor` 改用 `time.monotonic()` 替代 `get_event_loop().time()`

**Commits:** `963ee6b` `7a07f98` `2a4bbc5`

### 2026-07-14: 提醒工具 coroutine 序列化 bug（已修复，归档）

- **`reminder/plugin.py` coroutine 被提前 await 后传入 payload** — `_remind()` 生成 coroutine → `scheduler.dispatch_background()` → checkpoint JSON 序列化时 coroutine 不可序列化，跨重启丢失。
- **已在 `39fa778` 修复**: 改用 `asyncio.ensure_future(_remind())` 绕过 scheduler 持久化，注释说明取舍。

### 2026-07-14: 安全修复 — code-review-report 遗留严重问题清零

- **Milvus `_metric_type` 跨集合污染** (`milvus.py:39`): `self._metric_type` 单值 → `self._metric_types: dict[str, str]` per-collection 字典。`create_collection` 按名存储，`search` 按名读取，fallback L2。
- **MongoDB NoSQL 注入** (`mongodb.py:31`): collection 名加正则白名单 `^[a-zA-Z0-9_-]+$`，覆盖所有 7 个方法 + database 名。
- **Embedding 工厂缓存忽略 kwargs** (`registry.py:30-35`): cache key 从 `model_id` 扩展为 `model_id + json.dumps(kwargs)`。

### 2026-07-13/14: LTM 自动更新与去重 + 日志精简 + 数据一致性 + 文档大修

**LTM 记忆系统全面升级：**
- **MemoryReflector** (`core/memory/memory_reflector.py`): 定期深度去重。双条件触发（50 轮 + 30 条新记忆），KMeans 聚类分组，LLM 批量审查（merge/update/delete），上限控制（5+5+3）。对标 CharacterReflector 生命周期（restore_state/flush）。
- **ConsolidationCompare** (`core/consolidator.py`): 提取时轻量比对。新记忆 vs 已有记忆 top 10 → LLM 判断 insert/update/merge/skip → 预校验 → 执行。失败回退全 insert。
- **LTM 数据模型扩展**: `_absorbed_ids` + `_write_lock` + `get_all()` + `mark_absorbed()` + `update_text()` 扩展（支持 poignancy/memory_type）+ `search()` 过滤已吸收条目
- **驱逐**: 开发阶段 `_evict_old_entries()` 已注释（2026-07-28 已修复：重命名为 `_evict_overflow()`，count-only，加 Milvus 清理，re-enabled）
- **Prompt 文件** (GPT 5.6 sol_pro 生成):
  - `prompt/ExtractMemory.j2` — 反幻觉提取（13 节，含正反示例 + 自检清单）
  - `prompt/ConsolidationCompare.j2` — 比对去重（186 行）
  - `prompt/MemoryReflector.j2` — 定期去重（239 行）
  - `prompt/ref/` — GPT 原始版本归档（4 个 snake_case.md）
  - `prompt/README.md` — 命名规范 + 渲染方式文档
- **Prompt 基础设施**: `.j2` 统一 PascalCase 命名；`str.replace()` 替代 `.format()`（避免 JSON 大括号冲突）；文件加载替代内联字符串；`prompt-requirements.md` 需求文档

**日志精简：**
- TTS 分句/播放日志从单句 INFO 降为 DEBUG，`wait_all()` 后一行汇总（"TTS: N 句 X 字符 → YKB 已播放"）
- 第三方库日志级别 DEBUG→WARNING（pymongo/motor/httpx/openai/urllib3/sentence_transformers）
- 启动日志：工具列表从逐条 INFO → 一行汇总；横幅工具数精简

**数据一致性：**
- **Archive 成为权威数据源**: Session 视图从 `message_archive` 读取，非 `sessions` 快照。`session_id` 注入 + 迁移 + 降级路径
- **MongoDB 三层缓存清理**: localStorage（前端）→ session._messages（内存）→ sessions 集合（MongoDB）连环问题排查。移除了前端 localStorage 消息缓存
- **数据库清理**: MongoDB sessions/archive/token_usage + Milvus LTM 全部清空，从零开始

**启动与工具链：**
- `./start.sh` 根目录符号链接 + `Makefile start` 目标
- `conda run --no-capture-output` 修复 stdout 缓冲
- `ALL_PROXY=socks://` 修复（httpx 不兼容）
- `readlink -f` 修复符号链接路径解析

**Debug 仪表板增强：**
- Session Params 可展开 key-value（L1 Core 下方），大数据 key 只显示计数
- 搜索输入值 + 折叠状态在 DOM 重建时保留
- Archive 刷新时同样保留搜索值

**Bug 修复：**
- `removeElement`→`indicator.remove()`（chat.js 滚动加载）
- TTS read timeout 120s→30s
- mood/mood_intensity 初始化 + `restore_state()` 确保 session params 默认值
- MemoryReflector 计数器 `items = []` 初始化防 NameError
- KMeans 索引越界（100% embedding 阈值）

**文档全面更新：**
- `CLAUDE.md`: prompt 目录 + persistence.js + server routing + MemoryReflector 架构
- `architecture.md`: 21 处过时内容修复（backend 列表、测试数量、文件结构、路由、配置、Debug 标签页）
- `static/debug.md`: Session 数据源 + LTM 生命周期 + 前端缓存移除
- `docs/experience.md`: 8 条踩坑经验（数据清理、prompt 命名、conda/stdout、proxy、symlink、motor ObjectId、DOM 重建）
- `todo.md`: 标记已完成项 + 新增 LTM 完成清单

### 2026-07-12: PersonalityEngine + Elysia 性格升级 + Review 反馈

- **PersonalityEngine** (`core/personality/`): 四层性格模型。L1 Core（文件，不可变）→ L2 Self（session，缓慢演化）→ L3 State（Reflector 驱动）→ L4 Mood（已有 EmotionEngine）。CharacterReflector 变更为子组件。C1/C9 模板数据驱动。Character YAML 瘦身 + 路径解析。
- **Elysia 性格融合** (`personality.md` + 模板): 从 GPT 生成的 5 份参考中提取，以 sol_pro 为基准融合。新增心理结构、六种情绪表现、五模式切换、日常爱好、道德边界、场景示例。
- **System Prompt Section 全量升级**: F1 安全边界 + inner_voice 约束、F2 任务优先、F3 TTS 限制、C2 四阶段关系、C3 8 种情绪响应、C4 记忆可靠性、C6 重写、C7 冷却机制
- **MongoDB ObjectId 修复**: motor insert_one 副作用 → dict copy
- **Character 目录重组**: `character/elysia/{config.yaml, personality.md, reference/}`
- 80 tests pass

### 2026-07-11: System Prompt Section 升级 (F1/F2/F3/C5)

对 Module/system_prompt/ 进行模块化升级：统一格式约定、拆分职责、修复安全问题。

- **F1 ConflictRules**: `StaticSection` → `Jinja2Section`，新增 `f1_conflict_rules.j2` 模板。内容从 1 句话扩展为 4 条优先级规则（硬约束/软约束分类、inner_voice 真实性、感官事实不可覆盖、角色定义互补）
- **F2 OutputFormat**: 加 `<output_format>` XML 包裹；解释 `「」` 示例约定；代码块格式 `<!-- -->` 注释掉（待后续启用）；TTS 标记内容拆出到 F3
- **F3 TTSMarkup** (新建): `f3_tts_markup.j2` + `TTSMarkupSection`，独立管理 TTS 合成标记（MiniMax/gpt-sovits）
- **C5 Sensory**: `<perception>` 拆为 `<message>` + `<voice>` 子标签；`html.escape()` 防 XML prompt injection；`<voice>` 始终渲染（无数据时占位文本）
- **C6 SpeakingStyle**: `<perception>` 引用同步更新为 `<voice>` / `<message>`
- **MongoDB ObjectId 修复**: `insert_one/insert_many` 加 `dict()` copy 防止 motor 注入 `_id` 污染调用方；`session.load()` 剥离已有损坏数据
- **templates/README.md** 新建：格式约定、命名规则、渲染顺序、添加新模板步骤
- 55 tests pass

### 2026-07-03: Brain Prompt 模块化重构

Brain.j2 单体模板 (~244行) 拆分为模块化 section 管理引擎。

- `Module/system_prompt/` — Brain Prompt 引擎 (`PromptSection`, `StaticSection`, `Jinja2Section`, `SystemPromptManager`, `MessageBuilder`, 11 section 实现)
- `Module/system_prompt/templates/` — 11 个独立 section 模板 (f2_output_format + c1~c9)
- `Module/system_prompt/sections/` — 11 个 PromptSection 实现 (ConflictRules/OutputFormat/Sensory/Emotion/Memory/Personality/Relationship/SpeakingStyle/Proactive/OwnLife/SelfPerception)
- `PromptAssembler` 简化为胶水层：创建 11 个 section → `SystemPromptManager.set_context()` + `render()` → `MessageBuilder.build_messages()`
- 黄金对比测试：新旧 system prompt 逐字节一致
- 379 测试通过，零回归

详见 已归档

### Phase 1: L1 认知核心

| 任务 | 说明 |
|------|------|
| 1.1 流式响应 | `stream_think()` 结构化流式，TTFT 毫秒级，同时检测 tool_calls |
| 1.2 中断机制 | cognitive task 独立运行，INTERRUPT 即时 cancel，回到 IDLE 正常处理新消息 |
| 1.3 情绪系统 | EmotionEngine 关键词检测 + 强度衰减 + expression 推送，mood 注入系统提示词 |
| 1.4 SentenceBuffer | 内嵌到流式循环，逐 token feed，完整句触发 TTS 合成 |

### Phase 2: L2 记忆持久化

| 任务 | 说明 |
|------|------|
| 2.1 MongoDB Session | `SessionMemory` 增加 `connect_db()`/`save()`/`load()`，不可用时自动降级 |
| 2.2 Milvus + Embedding | `LongTermMemory` 向量语义搜索，HuggingFace 本地模型离线加载，GPU 兼容检查 |
| 2.3 记忆沉淀 | `MemoryConsolidator` 每轮后用 LLM 提取事实/偏好/计划，存入长期记忆 |

### Phase 3: 输入层

| 任务 | 说明 |
|------|------|
| 3.1 WebSocket 通道 | aiohttp server + per-client channel + AudioBuffer，脱离 stdin |
| 3.2 音频输入 | RMS VAD + PCM→WAV + STT transcription → EventBus，回声消除（deafen） |
| 3.3 HTML 客户端 | 麦克风采集 (AudioContext 16kHz → Int16 PCM)，聊天显示 |

### Phase 4: TTS 流式

| 任务 | 说明 |
|------|------|
| 4.1 流式合成 | `synthesize_stream()` 替代 `synthesize()`，background task 不阻塞文字流 |
| 4.2 句队列 | 多句并行合成，浏览器按 stream_id 缓冲 + 顺序播放 |

### Phase 5: 前端美化

| 任务 | 说明 |
|------|------|
| 视觉重设计 | 三栏布局：角色面板（表情 ring/状态灯/心情标签）+ 对话区 + 设置 |
| 对话增强 | 时间戳分隔线、消息分组、localStorage 持久化（刷新不丢失）、debounce 存盘 |
| 设置面板 | TTS 开关/音量滑块/清空对话/连接状态，右键齿轮菜单 |
| 响应式 | 窄屏折叠侧栏 |

### Phase 6: 工程完善

| 任务 | 说明 |
|------|------|
| 6.1 配置系统 | `config.yaml` 统一配置 (Agent/LLM/Memory/Server)，`app.py --config` |
| 6.2 代码清理 | 移除死代码，统一风格 |
| 6.3 prompt 模板导入 | 从 Elysia 上层项目导入多层认知模板 (Brain/Amygdala/ActiveSpeak/Reflector)，修复集成测试 |

### 测试修复

| 任务 | 说明 |
|------|------|
| prompt 测试 | `core.Paths` → 本地路径，17 个测试全部通过 |
| database 测试 | `serverSelectionTimeoutMS` 参数匹配，47 个测试全部通过 |
| session 隔离测试 | UUID-based session_id 确保 agent 间隔离 |

### 记忆系统升级 (2026-05-29)

| 任务 | 说明 |
|------|------|
| Consolidator prompt 改进 | 事件合并、重要性 1-10、memory_type 枚举、第一人称叙事 |
| 结构化 Schema | `MemoryEntry` 增加 `poignancy`/`memory_type`/`timestamp` 字段 |
| Rerank | `0.5×sim + 0.3×poi + 0.2×rec`，recency 指数衰减 (半衰期 ~6.9 天) |
| 缓冲阈值提取 | 攒 6 条消息再调 LLM，减少 API 调用，更多上下文提升提取质量 |

### 代码整理 (2026-05-29)

| 任务 | 说明 |
|------|------|
| 测试文件迁移 | `core/test_*.py` + `_fake_llm.py` → `core/tests/` |
| 死代码清理 | 删除 `StreamingSentenceBuffer`、TTS 临时文件 |
| 文档整理 | `ref_memory-architecture.md` → `docs/` |

### 基础设施优化

| 任务 | 说明 |
|------|------|
| 启动速度 | 嵌入模型懒加载 + thread pool；MongoDB + Milvus 并行连接；serverSelectionTimeout 3s |
| 响应延迟 | 嵌入加载不阻塞事件循环；consolidation fire-and-forget |
| 日志系统 | 库日志 → `log/` 文件（http/mongo/milvus/embedding）；终端只显示 core INFO |
| 输出去重 | ConsoleChannel 跟踪 delta 状态，流式 + 消息不重复打印 |
| 采样率修复 | AudioContext({sampleRate:16000}) 匹配 server 端 16kHz |
| 转录显示 | server → client transcription 事件，浏览器显示 🎤 气泡 |

### 代码审查修复 (2026-05-26)

| 类别 | 修复项 | 说明 |
|------|--------|------|
| Bug | TTS 死锁 | `_pull()` 内 `try/finally` 确保哨兵始终投递，HTTP 异常不再导致永久挂起 |
| Bug | `run_once` 竞态 | 先等待离开 idle 确认处理已开始，再等待回到 idle |
| Bug | 事件排空丢 STATE_CHANGE | INTERRUPT drain 后把 STATE_CHANGE 重新 post 回 bus |
| Bug | Mock backend 丢 tool calls | stream_think 检测 tool-call JSON 后直接 yield，不再逐字符分解 |
| Bug | SenseVoice 静默失败 | 响应格式不匹配时记录 warning 日志 |
| Bug | OutputManager.close 快速失败 | `return_exceptions=True` + 逐个记录失败 channel |
| 健壮性 | 裸 except 无日志 | demo_agent on_state 失败增加 debug + exc_info |
| 健壮性 | fire-and-forget 无异常处理 | idle_monitor / server 状态监控循环增加 try/except |
| 架构 | stream_think 缺 ABC | LLMBackend 协议增加默认实现（chat + 逐字符 yield） |
| 工程 | 嵌入模型缓存 | `MODEL_CACHE_DIR` 环境变量 + `.model_cache/` 默认路径，移除硬编码 |

---

### 文字/语音同步 (2026-05-26)

| 任务 | 说明 |
|------|------|
| 同步文字与语音 | Server: `_sync_event` + `_audio_pending` 双向缓冲，首句 TTS chunk 触发释放，5s timeout。Browser: `textQueue` hold delta 直到 `Audio.play()` resolve，8s timeout。Sync 阶段 INFO 日志 + log propagation 修复。 |

### 内部状态可视化 /debug (2026-05-26)

| 任务 | 说明 |
|------|------|
| 独立 /debug 页面 | `static/debug.html` 每 2s 轮询 `/debug/api` 获取 JSON 快照。`core/debug_info.py` 聚合 FSM/会话/长期记忆/工具调用/沉淀/连接等状态。`ToolRegistry` + `MemoryConsolidator` 各加 20 条 `_history`。 |

### 配置驱动入口重构 (2026-05-26)

| 任务 | 说明 |
|------|------|
| 配置驱动入口 | `config.yaml` 新增 TTS.enabled / STT.enabled。`app.py --tts`/`--stt`/`--host`/`--port` 默认值从 config 读取。优先级 CLI > config > 默认值。移除 `DEMO_ENABLE_TTS` 环境变量。 |
| docs/architecture.md 完整重写 | 从三层架构、Protocol-Registry-Backend 模式、事件总线/FSM、认知循环、记忆模型、输入输出系统、TTS 流式管线、文字/语音同步、配置系统、工程规范等 8 个章节完整描述项目设计。 |
| SentenceBuffer 升级 | 分隔符扩展 (～…〜)、英文句号上下文检测 (数字/缩写/URL)、连续分隔符合并、min_chunk 5 + 短句不卡死、构造参数可配置。70 tests (12 组)。 |

### DeepSeek v4 适配 (2026-05-27)

| 任务 | 说明 |
|------|------|
| model config 透传 | config.yaml → Agent → LLMAdapter |
| thinking mode 禁用 | `extra_body={"thinking": {"type": "disabled"}}` |
| API 错误日志 | 完整 exception + model name 打印到 WARNING |

### 消息气泡分离修复 (2026-05-28)

| 任务 | 说明 |
|------|------|
| 根因定位 | `send("")` 发送空字符串到 `WebSocketChannel.on_message()`，`vars("")` 抛出 `TypeError`，被 `_broadcast(return_exceptions=True)` 静默吞掉。所有 `send("")` 调用都未到达前端，`finalizeDelta()` 从未被触发，`currentDelta` 保持非 null，导致 `appendDelta` 将所有句子追加到同一气泡 |
| 修复 | `WebSocketChannel.on_message()` 增加 `isinstance(msg, str)` 分支，将纯字符串包裹为 `{"content": msg}`。`ConsoleChannel.on_message()` 同步防御 |

### 气泡逐条弹出 + 语音对齐 (2026-05-29)

| 任务 | 说明 |
|------|------|
| 逐条弹出 | 新增气泡初始隐藏 (`opacity:0, translateY(8px)`)，通过 JS 定时器逐条触发 transition 弹出。首条 100ms 快速出现，后续 500ms 间隔 |
| 语音对齐 | `onAudioStart()` 挂钩到 `Audio.play().then()`，音频开始播放时立即弹出下一个隐藏气泡，文字出现与语音播放同步。TTS 关闭时纯定时器驱动 |
| 防重复 | `deltasRendered` 标记阻止 `send(msg)` 全量文本在已逐句渲染后创建重复气泡 |

### 文字/语音绑定发送 (2026-05-29)

| 任务 | 说明 |
|------|------|
| 架构简化 | 删除 `_sync_event`/`_sync_buffer`/`_audio_pending`(~60行)，TTS task 等首块音频后绑在一起发送 delta+sentence+audio。服务端从 4 个 flush 出口简化为单一路径。workers=1 FIFO 保证句间顺序 |
| 前端简化 | 删除 `textQueue`/`pendingBubbles`/`scheduleReveal`/`onAudioStart`(~40行)，delta 直接 appendDelta 渲染 |
| 排队弹出 | 新增 `nextBubbleQueue`：当前句音频播放中到达的后句气泡暂不挂 DOM，等 `playNext`/`onended` 触发弹出。防止文字在语音播完前出现 |
| CSS 踩坑 | `animation: fadeIn` 执行期间覆盖 inline `opacity:0`，需同时设 `animation:none`；`opacity:0` 仍占 flex 布局空间，排队气泡直接不 `appendChild` |
| 测试: 714 passed（+3 pre-existing failures）|

### 调试工具 + Emoji 排序 + 阻塞修复 (2026-05-29)

| 任务 | 说明 |
|------|------|
| /debug 工具状态 | `_executing_tools` 追踪当前执行工具，/debug 始终显示 "Idle" 或 "Executing: xxx" |
| Emoji 按序排队 | `_pending_emojis[stream_id]` 挂靠 TTS 句，等 TTS 发完后按序发送，不再乱序 |
| DDGS 阻塞修复 | `ddgs.text()` 同步阻塞事件循环 → `asyncio.to_thread()` 扔线程池，TTS 不再卡 22s |

### WebRTC VAD (2026-05-27)

| 任务 | 说明 |
|------|------|
| webrtcvad 替代 RMS | GMM 声学模型区分语音/噪音，aggressiveness=3 |
| 延迟中断 | 3 个连续 speech chunk 后才 interrupt |
| nospeech 过滤 | `<|nospeech|>` 结果丢弃 |

### 简化双轨 (2026-05-27)

| 任务 | 说明 |
|------|------|
| 去除结构化输出 | Brain.j2 纯文本，移除旧版 TagParser。2026-06-24 重新引入 inner_voice 输出标签（伴侣化改造 Step 2） |
| 保留思维协议 | Step 1 内心独白 → Step 2 对外回复在 prompt 中 |
| prompt_assembler | 始终用 Brain.j2，无分支

### EmotionEngine STT 情绪集成 (2026-05-27)

| 任务 | 说明 |
|------|------|
| SenseVoice 标签解析 | raw_text 正则提取情绪(HAPPY/SAD/...)和事件(Laughter/Cry/...)，存 last_emotion/last_events |
| EmotionEngine.analyze_user_stt | STT 情绪 → AI mood 映射(HAPPY→warm, SAD→concerned, ...)，非 1:1 跟随 |
| perception 富集 | Brain.j2 `<perception>` 注入 [语调: HAPPY 😊 | 事件: Laughter 😀] |
| 浏览器显示 | transcription 气泡带情绪 emoji + 事件 emoji |
| nospeech 过滤修复 | `_call_api` 用 raw_text 检测 `<|nospeech|>` 丢弃，替代 audio_buffer 无效的 text-contains 检查 |
| 优雅退出 | `POST /shutdown` Web 端点 + 浏览器关闭按钮 + `Ctrl+C`。修复 WebSocketChannel.close() 空函数、state-monitor task 泄漏、embedding 线程泄漏、stdin to_thread 阻塞等 5 个 hang。2026-05-30 进一步简化：移除 `_stdin_watcher` + `_run_and_diagnose`，统一 `asyncio.run()` |
| 测试: 714 passed（+3 pre-existing failures）|

### 日志链路系统 (2026-05-29)

完整重做日志系统，覆盖信息缺失、格式噪音、结构性缺陷三方面。

| 类别 | 任务 | 说明 |
|------|------|------|
| 基础设施 | 毫秒级时间戳 | `19:19:23.456 [I]`，替代秒级 `19:19:23` |
| 基础设施 | 请求 ID | ContextVar + logging Filter，每轮对话分配 `[001]`，自动注入所有日志行 |
| 信息可见 | 用户输入日志 | `>>> 用户输入 (妖梦): ...`，含 STT 情绪标签，到达即记 |
| 信息可见 | FSM 状态 → INFO | `FSM → thinking/speaking/executing/idle/interrupted` |
| 信息可见 | 心情变化 → INFO | `心情 (STT ANGRY): neutral → calming` |
| 管线计时 | 提示词构建/首token/工具/TTS | 全链路耗时可见，毫秒精度 |
| 请求边界 | 轮次标记 | `>>>` 开始 + `<<< 回复生成完成 (XXX ms)` 结束 |
| 格式清理 | 去空行 + emoji区分 | `print()` 去 `\n`；`flush emoji: 😊` 独立标记 |
| WS/STT | 消息日志 | `WS 收到文本` / `WS 收到打断指令` / `STT 识别: ... [ANGRY]` |
| 对话分离 | conversation.log | 独立 FileHandler，不与 stderr 混杂 |
| 跨 task 传播 | request_id 传递 | process_message 生成 → event data 携带 → _process_events 提取 → IDLE/INTERRUPT 重置 |
| TTS 编号 | 本轮/全局双计数 | `TTS 句 3/11 开始` = 本轮第3句 / 全局第11句 |

---

### Bug 修复 + 性能优化 (2026-05-30)

| 类别 | 修复项 | 说明 |
|------|--------|------|
| Bug | `PromptAssembler._prompt` 死代码 | 指向错误的 `character/` 目录且从未使用 → 改为正确复用 `PromptManager(prompt/)` |
| Bug | `SentenceBuffer._skip_dot_and_retry()` 死代码 | `_find_split()` 已过滤假阳性句号，此路径永远不可达 → 删除 |
| Bug | `ToolContext.user_id` 硬编码 | `"妖梦"` → `config.agent.user_name` |
| Bug | filesystem `.env` 拒绝过于宽泛 | 子串匹配 → 逐级路径组件匹配（`part == pattern or part.startswith(pattern+".")`） |
| Bug | `_transition_idle()` 无条件发 SPEAKING | 仅 tool_calls 无文本时不再发多余的 SPEAKING 事件 |
| 性能 | `core/timing.py` 计时工具 | `Timer` context manager + `TimingClock` 请求级聚合，替换 ad-hoc `time.perf_counter()` |
| 性能 | 认知循环计时汇总 | `<<< 回复生成完成 — 2340ms \| tools(2):1520ms \| ttft:380ms \| prompt:45ms` 单行输出 |
| 性能 | `Module/bench/` 端到端 benchmark | 新增 `bench_e2e()`，测试 Agent 完整管线（启动/问候/记忆搜索/Web搜索） |
| 修复 | TTS 句编号格式 | `TTS 句 1/1`(误导) → `TTS 句 1 (总#1)`(本轮序号+全局计数) |
| 修复 | FSM → IDLE 时机 | `_transition_idle()` 等待所有 TTS 任务完成后才进入 IDLE，避免模型生成完但 TTS 还在播时提前空闲 |
| 通用 | 移除 `_os._exit(0)` + stdin watcher | 删除 `_run_and_diagnose()` + `_stdin_watcher`，server 模式统一 `asyncio.run()` |

### 前端体验优化 (2026-05-30)

| 任务 | 说明 |
|------|------|
| 当前播放气泡高亮 | TTS 播放时对应气泡蓝色光晕 + `bubble-pulse` 呼吸动画，播放完自动取消 |
| 新消息提示箭头 | 用户上翻历史时底部 fixed 显示 "↓ 新消息" 红色按钮，点击滚回底部自动隐藏 |

### /debug 延迟 + 内存面板 (2026-05-30)

| 任务 | 说明 |
|------|------|
| 延迟历史 ring buffer | `CognitiveLoop._timing_history` 存最近 100 轮请求的计时数据 |
| /debug/api 暴露延迟 | `DebugInfo.timing_history` + `timing_stats`（p50/p95/p99/avg/last） |
| /debug 前端延迟卡片 | debug.html 新增 Latency 卡片，表格展示百分位数 |
| 内存统计 | `psutil.Process()` 获取 RSS/VMS/占比/线程数，/debug/api + 前端展示 |

---

### TaskScheduler 统一任务调度中心 (2026-05-31)

| 任务 | 说明 |
|------|------|
| Task 模型 | `core/task.py` — Task dataclass, Future 双向绑定, retry policy, 工厂方法, 序列化 |
| Worker Pool | `core/task_scheduler.py` — N=5 Worker, callback 模式被 CognitiveLoop 调用 |
| Future 取消传播 | INTERRUPT → `dispatch_tool_calls` CancelledError → 显式 cancel 每个子任务 → Worker 取消执行 |
| 工具执行通过 Scheduler | `_on_tool_calls` callback → `dispatch_tool_calls()` → Worker Pool 并行执行 → 结果回传 |
| 消息排队 FIFO | busy 时 `_pending_messages` 入队, STATE_CHANGE idle 时出队, INTERRUPT 清空 |
| 重试 | 工具 2 次 retry (指数退避 1→2→4s), 后台任务 3 次 (1→2→4→8s) |
| 超时保护 | `asyncio.wait_for(_execute_tool, timeout=60s)` |
| 内存管理 | `_prune_tasks()` 定期清理终态, `cancel()` 即删, `stop()` drain + clear |
| Checkpoint | BACKGROUND 任务 JSON 持久化, 原子写入, crash-safe, `.tmp` 残留清理 |
| /debug 面板 | Scheduler 卡片 (pending/running/total + task_tree) |
| 测试 | 40 tests — 14 model + 26 scheduler (含 7 集成测试), 无回归 |
| 代码审查修复 | 18 个问题 (4 高 + 8 中 + 6 低), 全部修复 |

**架构**:

```
EventBus → Agent 排队 → CognitiveLoop.process() (专属路径)
                              │
                      检测 tool_calls
                              │
                              ▼
                    Scheduler.dispatch_tool_calls()
                              │
                              ▼
                    Worker Pool (N=5)
                    ├── 工具子任务 (超时/重试/cancel)
                    └── 后台任务 (checkpoint/resume)
```

**关键设计**: 认知循环不经 Scheduler, Scheduler 是纯 Worker Pool。Future 双向绑定。`cancel()` 级联取消子树 + 清理 _tasks。shutdown 先停 event_task 再停 scheduler。

### Ctrl+C 修复 + data/ 目录 (2026-06-01)

| 任务 | 说明 |
|------|------|
| Ctrl+C traceback 修复 | `run_server()` 同时捕获 `(KeyboardInterrupt, asyncio.CancelledError)`，避免 `asyncio.sleep(1)` 取消导致的双重 traceback |
| `data/` 目录自动创建 | Scheduler `start()` 时 `mkdir(parents=True)`，无 BACKGROUND 任务时也确保目录存在 |
| VAD 环境音敏感 | 加入 todo — 需调整 aggressiveness 或加降噪 |
| 工具超时手动测试 | 加入 todo — 需构造慢工具场景验证 60s timeout |

---

### Review 修复 Phase 1 — 基础清理 (2026-06-01)

| 任务 | 说明 |
|------|------|
| `requirements.txt` | Python 依赖声明文件，含 core/optional 分组 |
| `_seed_memories()` demo 标注 | 添加 docstring 注明为演示脚手架数据 |
| `can_retry()` 修复 | COMPLETED/CANCELLED 终态任务不可重试 |
| `_pending_messages` 容量上限 | 默认 32 条，防止消息风暴导致内存无限增长 |
| 文件系统安全强化 | denylist 从 4 条扩展到 17 条路径 + 16 个敏感文件名 |
| `.env.example` 模板 | 环境变量模板 + API key 轮换指引 |

### Review 修复 Phase 2 — 代码质量 & 解耦 (2026-06-01)

| 任务 | 说明 |
|------|------|
| CognitiveLoop/TaskScheduler 公开 API | `cancel_current()` + `set_tool_context()`，消除私有属性访问 |
| EmotionEngine 异常边界 | `_safe_wrapper()` 捕获 fire-and-forget 异常并记日志 |
| TaskScheduler prune 并发安全 | 从 worker loop 移出为独立 `_prune_loop()` 周期任务 |
| SessionMemory MongoDB 原子 upsert | `update_one(upsert=True)` 替代 find-then-update 竞态 |
| DocumentDB protocol upsert 支持 | `update_one()` 增加可选 `upsert` 参数，MongoDB backend 透传 |
| 测试补充: `test_log_setup.py` | 3 tests — request_id 读写 + ContextVar 隔离 |
| 测试补充: long_term_memory | 2 tests — fallback search + empty search |
| `Module/schema/` deprecation 标注 | 添加模块级注释说明当前未被 core/ 使用 |

### Review 修复 Phase 3 — 韧性 & 可观测性 (2026-06-01)

| 任务 | 说明 |
|------|------|
| LLM 请求自动重试 | 429/500/502/503/504 指数退避重试 (最多 3 次)，非可重试错误立即抛出 |
| LongTermMemory TTL 驱逐 | 90 天过期 + 1 万条上限，`insert()` 后自动触发 |
| `/health` `/ready` 端点 | JSON 响应含 FSM 状态、LLM model、memory 计数、tools 列表 |
| LongTermMemory 脏数据回写 | `_dirty_ids` 追踪 Milvus 不可用时的离线条目，`_sync_dirty_entries()` 重连后同步 |
| MemoryConsolidator 节流 | `asyncio.Lock` 防止并发 LLM 调用，已有运行中的 consolidation 时跳过 |
| 前端: 消息悬停复制 | hover 气泡出现复制按钮，clipboard API 写入，2s "已复制" 反馈 |
| 前端: 发送按钮动画 | 发送后 0.8s 勾动画 + scale flash，按钮禁用防重复发送 |
| MemoryEntry.id 字段 | 添加唯一标识符，`insert()` 时 `uuid4().hex[:12]` 自动生成 |

---

### Bug 修复: 消息气泡尾部空行 (2026-06-01)

| 任务 | 说明 |
|------|------|
| 根因定位 | `SentenceBuffer._extract()` 返回的句子包含尾部 `\n`（来自分隔符 run）。CSS `white-space: pre-wrap` 将尾部 `\n` 渲染为可见空行 |
| 修复 | `complete.rstrip(" ")` → `complete.rstrip()`，strip 所有尾部空白包括 `\n` |
| 测试 | 65 个 SentenceBuffer 测试全部通过，2 个旧测试更新为正确的预期值 |

### Bug 修复: 复制按钮文字泄漏到气泡内容 (2026-06-01)

| 任务 | 说明 |
|------|------|
| 根因定位 | Phase 3.8 复制按钮 `<span>复制</span>` 放在 `.msg-bubble` 内部，`appendDelta()` 的 `textContent +=` 吞入按钮文字并销毁按钮元素 |
| 修复 | 文字隔离到 `<span class="msg-text">`，`currentDelta` 指向 text span，复制按钮读取 `textSpan.textContent`。`saveChat()` 优先读 `.msg-text` |

---

### Bug 修复: TTS 播放高亮不同步 (2026-06-01)

| 任务 | 说明 |
|------|------|
| 根因定位 | Delta 消息不含 `stream_id`，前端只能通过 `nextBubbleQueue` 推断高亮时机。首句气泡在音频开始前已挂 DOM，`revealNextQueuedBubble()` 无法处理，导致首句永不高亮；后续句依赖队列时序偶有错位 |
| 修复 | 后端 `on_text_delta` 增加 `stream_id` 参数（WebSocket/Console/协议），CognitiveLoop TTS delta 广播携带 `stream_id=_sid`。前端 `_streamBubbles[streamId]` 映射气泡，`playNext()` 直接查表高亮 |
| 测试 | 727 通过 (5 pre-existing failures) |

### 前端模块化重构 (2026-06-01)

将 `static/index.html` 从 741 行单文件拆分为 12 个模块化文件，同时修复 12 个 bug + 4 个 UX 增强。

| 改进 | 说明 |
|------|------|
| 模块拆分 | HTML 47 行 + CSS 206 行 + JS 863 行（10 个 ES module） |
| 状态封装 | 15+ 散落全局变量 → `AppState` 单例 (`state.js`)，`resetConnection()` 统一状态重置 |
| 退避重连 | 固定 3s → 指数退避 1s→2s→4s→8s→max 30s ± 20% jitter |
| 协议检测 | 硬编码 `ws://` → `location.protocol` 自动选 `wss://` |
| JSON 解析防护 | 无防护 → try-catch + console.warn |
| Tool card ID | 工具名 key（可冲突）→ `name-counter` 唯一 ID |
| 设置面板 | 图标切换 → 外部点击关闭 + 清空 confirm 对话框 |
| 增量保存 | `querySelectorAll` 全量扫描 DOM → 内存 `messages[]` 数组直接序列化 |
| 恢复历史 | 走 `addUserMsg()`（副作用）→ 直接构建 DOM（独立路径） |
| `window._ac` 泄漏 | 全局命名空间 → `state.audioChunks`，重连时清空 |
| `_streamBubbles` 泄漏 | 永不清除 → 30s TTL 超时自动清理 |
| `playNext()` 错误 | 静默吞掉 → `console.warn` 日志 |
| Escape 快捷键 | 无 → Escape 发送 `{type: "interrupt"}` WS 消息 |
| `prefers-reduced-motion` | 无 → 全部动画禁用 |
| 基本 ARIA | 无 → `role="article"`, `aria-label`, `aria-hidden` |
| Live2D sandbox 修复 | 去掉 `allow-same-origin` 导致资源加载失败 → 恢复（Live2D 需同源 fetch 模型文件）|

### Debug 页面优化 (2026-06-01)

| 改进 | 说明 |
|------|------|
| 数据补全 | 新增 `session_params` 展示；`llm_mock` 死代码 → `/mock/i.test(model)`；工具参数加 `title` tooltip |
| XSS 防护 | `fsm_state`、`previous`、role、type 等遗漏的动态值加 `esc()` |
| 轮询退避 | 固定 2s → 指数退避 2s→4s→8s→max 30s，成功后重置 |
| 暂停/恢复 | 点击暂停停止轮询，恢复后立即刷新 |
| 跳过无变化渲染 | 浅比较 7 个关键字段，相同跳过 DOM 重建（消除闪烁）|
| 复制 JSON | 一键复制完整 debug 数据到剪贴板 |
| 可展开消息 | 截断文本点击展开/收起全文 |
| 响应式 | 700px 断点切换单列布局 |

---

### 工具系统深度重构 (2026-06-02)

全面 review 发现 14 个问题（安全/资源/执行管道/功能缺口），深度重构为 Pydantic + ToolPlugin 架构。

| 类别 | 任务 | 说明 |
|------|------|------|
| 架构 | `@tool` 装饰器 → `ToolPlugin` 基类 | Pydantic `BaseModel` 参数校验，`model.model_json_schema()` 零成本生成 Schema |
| 架构 | `ToolRegistry` → `ToolManager` | `load_all()`、`execute()`、`execute_stream()`、`reload_plugin()`、`shutdown()`、Debug 接口 |
| 架构 | 接口依赖反转 | CognitiveLoop 依赖 `ToolExecutor` Protocol，不直接 import `ToolManager` |
| 安全 | `SafetyProfile` + `SafetyPipeline` | 6 阶段管道：Pydantic 校验 → 安全检查 → HITL 审批 → 信号量 → 超时执行 → Token 截断 |
| 安全 | SSRF 防护 | `web_fetch` 内网 IP 拒绝 + 重定向目标校验 + 响应体大小限制 |
| 安全 | 路径沙箱 | `Path.resolve()` + 白名单 + 符号链接检测 + 文件大小预检 + session 隔离 |
| 安全 | 并发控制 | `asyncio.Semaphore` + `RATE_LIMITED` 错误类型 |
| 安全 | Token 截断 | 中英文混合估算，`head_tail` / `head_only` 策略，流式强制降级 |
| 功能 | 新增 4 个工具 | `write_file`、`set_reminder`、`get_runtime_config`、`get_self_state` |
| 功能 | HITL 审批 | `requires_human_approval=True` → 管道暂停 → 前端确认 → 继续/拒绝/超时/过期 |
| 功能 | 流式执行 | `execute_stream()` yield `ToolProgress`，背压控制，进度超时检测 |
| 功能 | 热重载 | `reload_plugin(name)` 不中断正在执行的任务 |
| 功能 | 链路追踪 | `ToolContext.trace_id` + `agent_id`，CognitiveLoop 每轮生成 UUID4 |
| 功能 | 类型化 DI | `PluginContext` 替代弱类型 `services: dict` |
| 工程 | `ErrorType` 扩展 | 新增 `PERMISSION_DENIED`、`RATE_LIMITED`、`NETWORK_ERROR`，`is_retryable` 收紧 |
| 工程 | `Capability` 枚举 | 8 种能力值替代字符串 `requires`，支持可用性检测 |
| 工程 | Debug 接口 | `plugin_list`、`executing_now`、`history`、`capability_status`、`pipeline_stats` |
| Bug 修复 | `list_directory` 符号链接循环 | inode visited set 防无限递归 |
| Bug 修复 | `read_file` OOM | `f.readlines()` → 逐行读取 + early stop |
| Bug 修复 | `search_memory` limit 上限 | 上限 20，加 try/except |
| Bug 修复 | `save_memory` type 校验 | MemoryType 枚举 (Fact/Preference/Event/Plan/Note) + `.title()` 标准化 |
| Bug 修复 | `get_time` 时区 | 增加 `timezone` + `format` 参数，默认 ISO 8601 |
| Bug 修复 | TaskScheduler 重试 | `ErrorType.is_retryable` 检查，cancel 循环检测 |
| 文件变更 | 新增 | `capability.py`、`manager.py`、`test_framework.py`、4 个插件文件、README.md |
| 文件变更 | 修改 | `protocol.py`、`safety.py`、`__init__.py`×3、`agent.py`、`cognitive_loop.py`、`task_scheduler.py`、`debug_info.py`、`server.py`、4 个迁移插件 |
| 文件变更 | 删除 | `registry.py`、`decorator.py`、`schema.py` |
| 测试 | 310/311 passed | 1 pre-existing failure (TTS config) |
| 文档 | spec + plan + README | 已归档 + `Module/tool/README.md` |

---

### 测试审查 & 补充 (2026-06-02)

**背景:** 对项目 19 个测试文件、~302 个测试用例进行全面审查，发现 100+ 覆盖缺口，分 5 个 Phase 补充。

#### Phase 1: core/tools 插件 + Module/tool 框架 + timing (212 tests, 12 commits)

| 任务 | 说明 |
|------|------|
| test_tool_safety.py | `_resolve_sandbox_path`、`_validate_path`、`_coerce_value`、`validate_args` 33 测试 |
| test_framework.py 追加 | ToolParameter(8)、MediaItem(2)、ToolEmitter(5)、ToolResult.llm_content(7) |
| test_filesystem.py | ReadFilePlugin(8) + ListDirPlugin(4) + `_format_size`(4) |
| test_web.py | WebSearchPlugin(5) + WebFetchPlugin(7) + `_TextExtractor`(8) |
| test_time.py | TimePlugin(8) |
| test_memory.py | MemorySearchPlugin(4) + MemorySavePlugin(8) |
| test_reminder.py | SetReminderPlugin(3) |
| test_runtime_config.py | RuntimeConfigPlugin(5) |
| test_self_state.py | SelfStatePlugin(3) |
| test_write_file.py | WriteFilePlugin(11) |
| test_timing.py | Timer(6) + TimingClock(10) |

**Bug 修复:** `filesystem.py` 空文件 `UnboundLocalError` (i 未初始化) + 空文件截断提示错误 + `write_file.py` 嵌套路径 mkdir 缺失

#### Phase 2: LLM/STT/TTS/Embedding 服务模块 (135 tests, 4 commits)

| 任务 | 说明 |
|------|------|
| LLM (35 tests) | `stream_think`/`_stream_structured`(4)、API错误(4)、prefix边缘(3)、factory(2)、beta close(1) |
| STT (37 tests) | `_parse_emotion`(6)、`_parse_events`(5)、状态跟踪(3)、nospeech(1)、HTTP错误(3)、边缘(3) |
| TTS (32 tests) | emoji脱敏(5)、HTTP错误(3)、KeyError(1)、queue取消(1) |
| Embedding (31 tests) | shutdown(4)、单例缓存(3)、from_config(1)、API错误(3)、空输入(2) |

#### Phase 3: Output/Input 模块 (101 tests, 3 commits)

| 任务 | 说明 |
|------|------|
| Output (54 tests) | AUDIO路由(4)、command(1)、stream生命周期(1)、错误隔离(4)、WS全覆盖(10)、ConsoleChannel(3) |
| AudioBuffer (25 tests) | VAD帧边缘(4)、interrupt cooldown(2)、STT超时/异常(2)、on_transcription=None(1) |
| Server (22 tests) | `_handle_json`边缘(5)、/health/ready/shutdown端点(3)、修复无断言测试(1) |

#### Phase 4: Database/Config/Logger/Prompt 模块 (48 tests, 5 commits)

| 任务 | 说明 |
|------|------|
| VectorDB (12 tests) | 协议抽象强制(5)、index_preset(5)、连接失败 |
| DocumentDB (13 tests) | 协议抽象强制(6)、零结果(4)、upsert、ensure_index kwargs、连接失败 |
| Config (13 tests) | 约束describe(8)、Optional[X](2)、yaml_key多级、root-list、畸形YAML |
| Logger (4 tests) | CRITICAL、get_caller_logger、Unicode JSON、并发 |
| Prompt (6 tests) | datetime自定义格式、None值、缺失变量、空模板 |

#### Phase 5: Module/tool 剩余 + Core + emotion_engine 修复 (15 tests, 3 commits)

| 任务 | 说明 |
|------|------|
| ToolManager (11 tests) | definitions(anthropic/gemini/invalid)、cancel_all、reload_plugin、HITL broadcast、history maxlen、并发 |
| Core proactive (2 tests) | `_check_proactive` silence_timeout + empty triggers |
| Core consolidator (2 tests) | buffer_threshold=6 缓冲5条不触发 + 第6条触发清空 |
| emotion_engine 修复 | warm/concerned/calming/reassuring/understanding 关键词 fallback |

### 插件目录化重构 (2026-06-02)

| 任务 | 说明 |
|------|------|
| 目录化布局 | 8 个平铺 .py 文件 → 7 个插件目录，每个含 plugin.py / config.yaml / README.md / __init__.py |
| filesystem 合并 | filesystem.py + write_file.py → filesystem/plugin.py（ReadFile + ListDir + WriteFile） |
| config.yaml 加载 | ToolManager.load_all() 从各插件目录读取 config.yaml，覆盖 SafetyProfile 字段 |
| 沙箱配置可改 | 修改 core/tools/filesystem/config.yaml 的 read_dirs 即可调整允许访问的目录，无需改代码 |

### HITL 审批回路修复 (2026-06-02)

| 任务 | 说明 |
|------|------|
| 入站通路补全 | server.py `_handle_json()` 新增 `tool_approval` 路由，调用 `submit_approval()` |
| 前端审批对话框 | main.js `case 'command'` → `showApprovalDialog()` 弹模态确认框，允许/拒绝 + Escape |
| ToolManager 桥接 | 新增 `submit_approval()` 公共方法，不暴露 `_pipeline` 内部 |

### 工具卡片状态修复 (2026-06-02)

| 任务 | 说明 |
|------|------|
| call_id 匹配 | 后端 `tool_executing` / `tool_done` 都带上 LLM 的 `call_id`，前端用 `call_id` 做卡片 key 精确匹配，修复工具执行完后卡片永远显示"执行中"的 bug |

**总计: 29 commits, 511 新增测试, 5 个源码 bug 修复, 959 passed + 2 known failures = 961 total**

**文档:** 已归档

---

### 测试卫生 (2026-06-03)

| 任务 | 说明 |
|------|------|
| `test_session_isolation` 清理 | 测试后清理 MongoDB 中遗留的 session 文档 |

### 移除种子数据 (2026-06-03)

| 任务 | 说明 |
|------|------|
| 删除 `_seed_memories()` | demo 遗留代码，每次启动重复插入 5 条种子数据。从 `start()` 中移除调用 |

---

### 模型切换与自定义接入 (2026-06-04)

完整架构：配置驱动 provider 选择 → custom provider 端点 → 运行时热切换 + 前端 UI。915 tests。

| 阶段 | 任务 | 说明 |
|------|------|------|
| 配置驱动 | TTS/STT/LLM model 字段 | `app_config.py` 新增 model/base_url/api_key 字段，`config.yaml` 可配置默认模型 |
| 配置驱动 | ConfigLoader None 修复 | YAML 空值不再覆盖 dataclass 默认值 |
| custom provider | registries 扩展 | TTS/STT 新增 custom 条目，`custom_providers.yaml` 持久化自定义端点 |
| custom provider | OpenAICompatSTT/TTSBackend | 通用 OpenAI Whisper 协议 STT + TTS 后端，支持任意兼容端点 |
| custom provider | MiMo provider | 新增 `mimo` provider (mimo-v2.5 / mimo-v2.5-pro)，OpenAI 兼容协议 |
| 运行时热切换 | protocol `test()` 方法 | 所有后端新增连通性检测，返回 latency_ms |
| 运行时热切换 | Agent `switch_llm/stt/tts()` | 运行时替换后端实例，`switch_llm()` 先 cancel_current() 防 FSM 卡住 |
| 运行时热切换 | REST + WS | 4 个 `/api/providers*` REST 端点 + `switch_model` WS 消息 |
| 前端 | 设置面板下拉选择器 | 模型选择 + 自定义端点模态框 + 测试连接按钮 |
| 前端 | LLM 模型标签 | 角色名下方显示当前模型名，切换时实时更新 |
| 前端 | STT/TTS 能力指示 | `capabilities` WS 消息同步，不可用时禁用按钮 |
| 修复 | CognitiveLoop 异常兜底 | `except Exception` 广播错误到前端 + FSM 回 IDLE |
| 修复 | 空 `choices` 流式兼容 | MiMo 等返回 `choices: []` 时不再 IndexError |
| 修复 | TTS 重启重建 | TTS 创建从 `__init__` 移到 `start()` |
| 修复 | TTSFactory model 验证 | 大小写不敏感 voice 名验证 + custom 后端分发 |

**Spec:** 已归档
**计划:** 已归档

---

### 数据库调试接口 (2026-06-04)

开发阶段的调试工具，在 `/debug` 页面增加数据库管理面板。

| 任务 | 说明 |
|------|------|
| VectorDB 协议扩展 | 新增 `delete()` 抽象方法 + Milvus 实现 |
| LongTermMemory CRUD | `delete_entry()` + `clear_all()` |
| 8 个 REST 端点 | `/debug/api/ltm`（CRUD）、`/debug/api/sessions`（列表/详情/删除/清空）、`/debug/api/db-status` |
| Web UI 面板 | /debug 数据库 Tab，LTM 面板（搜索/过滤/新增/删除/清空）+ Session 面板 |

---

### Module 层代码审查修复 (2026-06-04)

代码审查发现 20 项问题，涉及 `Module/llm`、`Module/tts`、`Module/stt`。全部修复，594/595 tests 通过。

| 优先级 | 数量 | 内容 |
|--------|------|------|
| HIGH | 5 | 静默吞异常 → warning 日志、`test()` `<500` → `<400`、`last_emotion`/`last_events` 更新、文本预处理缺失、`from_config()` TypeError |
| MEDIUM | 4 | `_create_clients()` DRY 提取、`**kwargs` 转发 HTTP、KeyError 描述性包装、删除不可达代码 |
| LOW | 4 | 协议文档修正（32000Hz、首 chunk）、`stop()`→`close()` docstring、`test()` voice 硬编码→GET / |
| 架构 | 5 | `__init__.py` 统一 3 符号导出、`provider_id`→`model_id` 重命名、`last_emotion`/`last_events` 协议文档化、env var 命名注释 |
| 风格 | 2 | openai_compat.py + text_preprocess.py 英文注释→中文 |

**Commits:** `458fedf` `3e85ed3` `18720ab` `5a0728a` `8f6af2a`

---

### Live2D 开关 (2026-06-04)

设置面板新增 Live2D 开关，关闭时移除 iframe 释放 ~500KB Cubism 运行时资源。

| 任务 | 说明 |
|------|------|
| 配置层 | `Live2DConfig.static_image` 字段，env `LIVE2D_STATIC_IMAGE`，`config.yaml` |
| 服务端 | WS `capabilities` 消息下发 `live2d_static_image` |
| 前端状态 | `state.live2dEnabled`（localStorage 持久化）+ `state.live2dStaticImage`（capabilities 同步） |
| 开关 UI | 设置面板 Live2D 开关行 + CSS toggle + 点击状态提示 |
| iframe/image 切换 | `toggleLive2D()` — ON 时创建 iframe，OFF 时移除并显示 `<img>` / 🌸 emoji |
| 竞态修复 | capabilities 到达后更新已存在的 `#live2d-static-img` src |

**Spec:** 已归档
**计划:** 已归档

---

### 项目清理 (2026-06-05)

| 任务 | 说明 |
|------|------|
| 磁盘清理 | 删除 ~280MB 垃圾文件（`__pycache__`、`.pyc`、node_modules、临时文件） |
| UUID 统一 | 8 文件改用 `new_message_id()`，消除 `uuid4().hex[:12]` 散落 |
| 补缺失文件 | `Module/config/__main__.py`、`Module/input/__main__.py`、`Module/system_prompt/__main__.py`、`Module/logger/__main__.py`、`Module/input/README.md` |
| 文档整理 | `test_tool_parallel.md` 移至 `docs/`，清理 `settings.local.json` 过期引用 |

**Spec:** 已归档

### 多模态图片支持 Phase 1 — 用户→AI (2026-06-05)

| 任务 | 说明 |
|------|------|
| 消息格式 | `Module/schema/message_format.py` — TextPart/ImagePart/FilePart/AudioPart/VideoPart + ChatMessageV2 |
| 统一 content | SessionMemory/PromptAssembler content 统一为 `list[dict]` |
| 上传端点 | Server 新增 `/api/images`（multipart）+ `/api/chat`（测试）|
| base64 转换 | 本地图片自动转 data URI 发送 MIMO API |
| content_to_openai | 内部格式转 OpenAI API format |

**Spec:** 已归档

### 多模态 Phase 2 — 前端图片交互 (2026-06-05)

| 任务 | 说明 |
|------|------|
| 上传交互 | 粘贴(Ctrl+V) / 拖拽 / 📷 按钮 — 3 种方式上传 |
| 预览栏 | Slack 风格缩略图预览，× 移除 |
| 气泡渲染 | 用户气泡显示 200px 缩略图，点击打开 lightbox |
| 持久化 | session 恢复 + localStorage 支持图片 |
| 上传目录 | `data/uploads/images/`，独立 `/uploads/` 路由 |

**Spec:** 已归档

### Bug 修复集合 (2026-06-05)

| 优先级 | 问题 | 根因 | 修复 |
|--------|------|------|------|
| 高 | DeepSeek 收到 image_url 400 | `build_messages()` 历史消息含 MIMO 的 image_url blocks | 历史消息只提取 text parts |
| 高 | TTS 400 — markup-only 句子 | `_EMOJI_RE` 守卫漏过 `**动作**` 等 markup | `strip_all_markup()` 替代 `_EMOJI_RE` |
| 中 | Debug 页面显示 [object Object] | `collect()` 返回原始 content list，JS toString 变 [object Object] | 改用 `messages_for_client()` 格式化为 parts |
| 中 | MIMO 相对路径 URL 无效 | 前端发 `/uploads/...`，`_resolve_image_source()` 不处理 `/` 开头 | 处理好 `/` 前缀相对路径转 base64 |
| 中 | MIMO 工具耗尽无回复 | MAX_TOOL_ROUNDS=3，MIMO 用完后无最终回答 | → 5 |
| 中 | Debug 页面看不到图片 | `collect()` 和 `session_detail_handler` 丢弃 content 数组；前端只渲染文本 | 透传 content + 新增 renderImages() + 缩略图 + lightbox |

### describe_image 视觉工具 (2026-06-05)

| 任务 | 说明 |
|------|------|
| 工具插件 | `core/tools/vision/plugin.py` — DescribeImagePlugin，委托 MIMO 看图返回描述 |
| 自动注入 | `prompt_assembler.py` — 文本 LLM + 有图片时注入工具提示 |
| 缓存 | SHA256(image_bytes + model_id) → `data/cache/vision/{hash}.json` |
| 配置 | `config.yaml` vision.model 可配视觉模型 |
| Bug: setup async | `def setup()` → `async def setup()`，ToolProtocol 要求 async |
| Bug: chat() 返回 str | `response.get("content")` → `response.strip()`，chat() 返回 str 非 dict |
| Bug: timeout | `SafetyProfile.request_timeout` 30s → 120s |

**Spec:** 已归档
**计划:** 已归档

---

### describe_image 多图批量 (2026-06-06)

| 任务 | 说明 |
|------|------|
| `image_indices` 参数 | `DescribeImageParams` 新增 `image_indices: list[int] \| None`，一次 MIMO 调用处理多张图片 |
| 批量执行逻辑 | `_describe_batch()`：校验索引 → 解析所有图片 → 构建多图消息（N 个 image_url + 1 个 text）→ 一次 MIMO 调用 |
| 提示文本更新 | `prompt_assembler.py` 多图提示改为 `describe_image(image_indices=[0,1,...]) 一次性查看` |
| 向后兼容 | 不传 `image_indices` 走原 `_describe_single()` 路径，行为不变 |

### Session 完整化 — 工具消息持久化 + 历史降级渲染 (2026-06-06)

| 任务 | 说明 |
|------|------|
| 工具消息写入 Session | `cognitive_loop.py` 在工具执行后将 assistant(tool_calls) + tool(result) 写入 Session |
| 历史图片降级 | `build_messages()` 剥离图片时注入 `[用户曾发送N张图片]` 提示，多模态 LLM 原生传图 |
| 工具调用摘要 | 历史 assistant 消息的 tool_calls 转为 `[已调用: describe_image(...)]` 摘要注入 LLM 上下文 |
| 工具结果跳过 | role="tool" 消息不传给 LLM（摘要已在 assistant 中体现）|
| 前端透传 | `messages_for_client()` 透传 tool_calls/tool 字段（含 tool_call_id, name, content, success, error, duration_ms, truncated）|
| 前端恢复 | `restoreFromServer()` 遍历消息重建工具卡片（assistant+tool_calls → addToolCard，tool → updateToolCard）|
| localStorage | `saveChat()` 采集 `.tool-card` DOM，`restoreChat()` 支持还原 |
| Bug 修复 | `add_message()` 将 str content 包装为 TextPart 数组 → `messages_for_client()` tool 分支提取回纯文本，避免前端 `[object Object]` |
| Bug 修复 | `content_to_openai()` 返回 list 不能和 str `+` → 手动提取纯文本拼接 |
| Bug 修复 | 循环内局部变量 `content` 覆盖函数参数 → 改名 `msg_content` |

### 工具卡片内容不再截断 (2026-06-06)

| 任务 | 说明 |
|------|------|
| 移除 500 字符限制 | `cognitive_loop.py` `tool_done` 的 `content[:500]` → `content or ""` |
| CSS 调大可视区 | `.tool-card-section .value` max-height 200→400px |

### 工具调用详情可见性 (2026-06-05)

工具调用在 web 端和日志都看不到详情的问题修复。

| 任务 | 说明 |
|------|------|
| SafetyPipeline 统一日志 | `_finish` 闭包覆盖全部 8 个返回路径，所有工具自动获得开始/完成/失败日志 |
| `tool_done` 消息扩展 | 从 `{tool, call_id, success}` 扩展到 7 字段含 `content`/`error`/`error_type`/`duration_ms`/`truncated` |
| `tool_executing` args | 移除 `str(v)[:80]` 截断，传完整参数值 |
| 前端可展开卡片 | 收起态：工具名 + ✓/✗ + 耗时；展开态：参数 JSON + 返回结果 + 错误详情；失败默认展开红色边框 |

**Spec:** 已归档
**计划:** 已归档

---

### 近期完成 (2026-06-22 ~ 2026-06-23)

| 日期 | 内容 |
|------|------|
| 06-23 | **前端接入 API 文档** — `docs/frontend-api.md`，完整 WebSocket 协议（16 种 Server→Client + 5 种 Client→Server）+ REST API + .NET/WPF 接入指南。含分句策略、流式 TTS 同步、工具执行生命周期、结构化内容块。经 3 轮 review 修正 |
| 06-23 | **TaskSystem debug DAG 图可视化** — 引入 vis-network CDN，替换 ASCII DAG 渲染为交互式有向图。仅改 `static/debug.html`，后端不动。文档已归档 |
| 06-23 | **read_dirs 相对路径 session_id 修复** — `_resolve_sandbox_path()` 没对 `read_dirs` 做 `{session_id}` 替换，导致相对路径读文件找不到（缺 `default/` 子目录） |
| 06-23 | **STT/TTS test() 改用 GET /health** — 3 个 backend 的 `test()` 从 `GET /` 改为 `GET /health`，避免服务无根路由返回 404 误判不可用 |
| 06-23 | **启动横幅去重** — 去掉 `print()`，统一用 `logger.info()`；合并两个横幅为一个 |
| 06-22 | **runtime_config 工具增强** — `get_runtime_config` 从 ~75 行扩展到 ~344 行，7 个 section builder，支持 query 参数按需展开 |
| 06-22 | **代码审查修复** — 修复 ~35 backend + ~14 frontend bugs，含 SafetyProfile session_id sanitization、空 choices 兼容、embedding close() 竞态保护、Optional[X] 类型检查等 |
| 06-22 | **文档清理** — 删除过期文档 `ref_memory-architecture.md` + `test_tool_parallel.md`，更新 `architecture.md`（工具数/接口名/文件结构/session limit），修正 `database-storage.md` maxlen |
| 06-22 | **TTS/STT 连通性显示修复** — agent 启动时 `test()` 验证 TTS/STT 可用性；debug 页面补 STT 行 |
| 06-22 | **Token 追踪** — TokenTracker + PromptRecorder 注入 CognitiveLoop；debug 页面 Token 卡片；session-token-bar 顶栏 |

### 近期完成 (2026-06-23 ~ 2026-06-24)

| 日期 | 内容 |
|------|------|
| 06-24 | **伴侣化改造 Step 1: 对话风格** — Brain.j2 全面中文重写：身份/关系/内心世界/说话方式分离。回复从长文变 1-3 句自然对话。"嗯"/"哦"/"好嘛"作为合法回复；克制约束——不是客服不需要每句话都有用。详见 `docs/companion-roadmap.md`、`docs/companion-prompt-guide.md` |
| 06-24 | **伴侣化改造 Step 2: 内心世界 (inner_voice)** — LLM 输出 `<inner_voice>...</inner_voice>` 标签，cognitive_loop 流式状态机（PRE→THINK→REPLY）分流，前端淡色斜体 think 气泡。持久化到 session（刷新恢复），debug 页面新增 📤 LLM Raw Response 查看器。ResponseRecorder 环形缓冲区。prompt 加入 few-shot 示例确保标签输出稳定。LLM 对话历史注入 inner_voice 格式延续。spec: 已归档 |
| 06-24 | **WS 重连恢复修复** — FSM 同状态幂等 (idle→idle no-op)、CancelledError 泄漏防御、HITL 审批重发（新客户端重连后恢复审批弹窗）、shutdown 期间工具不重试 |
| 06-24 | **Git 历史清理** — 全量 rebase: 724→164 提交（77% 缩减），按功能/主题合并。spec: 已归档 |
| 06-24 | **文档全面更新** — README、companion-roadmap、companion-prompt-guide、completed、todo 同步到最新状态 |
| 06-24 | **Step 2 收尾: inner_voice toggle** — 设置面板"内心独白"开关（localStorage 持久化，默认开启）。spec: 已归档 |
| 06-24 | **感官/记忆模块标签汉化** — Brain.j2 `<facts>` 标签 + prompt_assembler `_time_of_day()`/`_season()` 返回值全部中文化。spec: 已归档 |
| 06-23 | **Brain.j2 prompt 修复** — 工具列表补全（7→12）、Identity/Interaction 块新增（定义"你是谁/用户是谁"）、Dual-Track Protocol 措辞软化（不再要求提取不存在的情绪数据）、记忆时间戳 Unix→datetime filter 渲染 |
| 06-24 | **llm_adapter started 标记修复** — 流已开始产出 token 后遇到 429/503 不再重试，避免用户收到重复内容。spec: 已归档 |
| 06-24 | **硬编码 "妖梦" 清理** — agent.py/server.py/prompt_assembler.py 5 处硬编码全部改为从 `agent._user_name` 来源。`app_config.py` 保留为唯一权威源。personality_block 空字段守卫 + macro 记忆层标 TODO。spec: 已归档 |
| 06-24 | **Brain.j2 渲染优化** — `Mood:`→`心情:`、工具规则+怎么回复间加空行+可见标题、latency 字段移除 |
| 06-25 | **code-review 中低危项修复** — 9 项（Python 6 + 前端 3）：_audio_seq cap、token limit、VAD log、broadcast log level、ensure_future→create_task、localhost harden、live2dStaticImage reset、ws.send return false、legacy comment。报告删除，遗留问题迁入 todo。spec: 已归档 |
| 06-25 | **数据库连接告警 + Milvus PK 匹配** — 连接失败 `logger.info`→`logger.warning`（3 处）+ 启动告警 + 120s health_check 自动重连（MongoDB/Milvus）。Milvus search() 用 `id(entry)` + PK 精确匹配替代 text key，解决同文异构条目覆盖。12 个单元测试。spec: 已归档 |
| 06-26 | **Module/_common/ 轻量共享层重构** — 新建 `Module/_common/`（6 文件 + 5 测试文件），提供 `env.py`（dotenv 统一入口）、`registry_helpers.py`（api_key/base_url/logger 解析）、`test_helpers.py`（make_test_method 工厂）、`config_test_helpers.py`（make_config_test_classes 工厂）、`main_runner.py`（run_module_main 统一入口）。消除 6 个 registry + 5 个 backend test() + 9 个 __main__.py + 6 个 test_config.py + config/ 内部的重复代码。22 个 _common/ 单元测试。971 全量测试零回归。spec: 已归档 |
| 07-02 | **性格演化系统** — Brain.j2 叙事式性格重写（~15 行人格叙事替代 traits 列表）、`<self_perception>` 注入段（LLM 自我认知反映在 prompt 中）、mood→trait 表达调制（5 种心情）、CharacterReflector 组件（周期性 LLM 自我反思，50 对 OR 24h 触发）、PromptAssembler 适配（移除 l3_personality_block + fallback 内联）。8 个单元测试。spec: 已归档 |

### 2026-07-10: 部署工具链

一键安装 + 预检 + docker-compose 数据库。

- `scripts/setup.sh` — 7步自动安装（conda → pip → .env → 嵌入模型 → 目录 → 预检）
- `scripts/start.sh` — 一键启动（conda → docker → 预检 → 服务）
- `scripts/download_model.py` — 嵌入模型下载（默认 hf-mirror.com 镜像）
- `Module/config/preflight.py` — 环境预检（14 项检查，`app.py --check`）
- `docker-compose.yml` — MongoDB 7 + Milvus v2.4 standalone
- `app.py` 默认改为 WebSocket 服务器模式，移除 `--once`/`--server`
- `requirements.txt` 更新：补 pydantic/networkx/psutil 等，移 ddgs
- README 完整重写：小白友好 + 配置文件参考 + 高级用法

### 2026-07-10: providers.yaml 重构

所有 provider 默认值从 registry.py 硬编码迁移到 `Module/config/providers.yaml`。

- 新建 `Module/config/providers.py` — 统一加载器
- `llm/stt/tts/embedding` 4 个 registry + `vectordb/documentdb` 2 个 config 改为从 YAML 加载
- `.env.example` 完善：87 行，按 LLM/TTS/STT/Embedding/数据库/工具 分类

### 2026-07-10: IDE 类型错误修复

修复 15 个文件的类型检查器报错。

- `_health_check` 闭包移除冗余 `self` 参数（3 个 backend）
- `stt/protocol.py` `transcribe_stream` 加 `yield ""` 成为生成器
- `logger/__main__.py`：`factory.create()` → `factory.get()`
- `llm/openai_compat.py`：裸 `logger` → `self._logger`
- 多处 `# type: ignore[attr-defined]` / null 守卫

### 2026-07-09: MiniMax TTS + 预处理解耦

- `MiniMaxTTSBackend` — HTTP 非流式 + 分块流式（hex→bytes）
- `TTSBackend.get_preprocess()` — 每个 backend 声明自己的预处理函数
- `TTSStreamManager` 从 backend 获取预处理，不再硬编码 `strip_all_markup()`
- `f2_output_format.j2` 按 TTS provider 输出不同标记指令
- MiniMax emotion 参数：`EmotionEngine` mood → `TTSStreamManager.set_emotion()`
- TTS 句子乱序修复：`asyncio.Event` 顺序释放门

### 2026-07-11: 消息永久归档 (MessageArchive)

所有对话消息永久保留到 MongoDB `message_archive` 集合，与 session 职责分离。

- `core/memory/message_archive.py` — MessageArchive 类（只增不删、分页查询、fire-and-forget 写入）
- `SessionMemory.add_message()` 中自动写入 archive
- `GET /api/messages` — 前端滚动到顶懒加载历史（游标分页）
- `GET /debug/api/archive` — debug 浏览/搜索/清空
- `static/js/chat.js` — 滚动监听 + prependMessages + 位置修正
- `static/debug.html` — 数据库 Tab 新增 Message Archive 面板（三栏布局）
- 16 个测试（13 单元 + 3 集成）

### 2026-07-11: LTM 从 Milvus 恢复 + 显示修复

- `LongTermMemory._load_from_milvus()` — 启动时从 Milvus query 已有条目恢复到 `_entries`，按时间排序并驱逐超限
- `insert()` 存储 `entry_id` 到 Milvus metadata，确保加载时恢复原始 ID
- Debug 概览页 `lt_recent` 补 `memory_type` 和 `poignancy` 字段，类型标签从 `?` 修正
- Session 20 条上限未改（上下文窗口），归档解决的是历史保留

### 2026-07-11: Consolidation user_name 参数化

`EXTRACTION_PROMPT` 中"用户"→`{user_name}`，对话前缀同步使用。

### 2026-07-11: CharacterReflector buffer 持久化

- `buffer()` 每次追加后自动 `_save_state()` 到 session.params（随 MongoDB 持久化）
- `restore_state()` 在 `session.load()` 后调用，重启恢复累积进度
- `flush()` 增加最低阈值（10 对），少量对话不强制反思，避免清空 buffer

### 2026-07-11: IDE 类型错误修复 (第二批)

生产代码：`_EventBusHITLNotifier` 继承协议、`get_raw_client()` cast、`stt_backend.last_emotion`→`getattr`、`distances` 类型放宽、`_archive` assert、流式变量 `content`→`delta` 消 shadowing。
测试代码：9 个文件的类型注解修复 (`# type: ignore`、`assert is not None`、`e.type.value`→`e.type`)。

### 2026-07-11: 死代码清理

删除 F3 工具声明相关死代码：`_reference_behavior.j2`、`ToolManager.to_prompt_text()`、`PromptAssembler.set_tool_manager()`。

### 2026-07-06: 人称代词参数化

`CharacterConfig.user_pronoun` + c1/c2/c3 模板 `{{ user_pronoun }}`。

### 2026-07-04: Brain Prompt Section Provider

Brain.j2 单体拆分 → 11 个独立 PromptSection + SystemPromptManager。

### 2026-06-26: Module/_common/ 轻量共享层

消除 registry/test/__main__/test_config 重复代码。

### 2026-07-21: todo 清理 — 情绪管线 + 存储优化

**STT→mood 硬编码删除：** `_STT_EMOTION_TO_MOOD` 映射表删除（7 条规则）。STT 情绪通过 C5 Sensory 直传 LLM，不再替 LLM 做 mood 决策。关键词路径 `analyze_user()` 始终执行（不跟 STT 互斥）。`analyze_user_stt()` 简化为仅处理音频事件→表情。

**`_push_expression()` 映射补全：** 删除 4 条硬编码映射表，改用 `mood.value` 直接透传。全部 10 种 Mood 的 expression 名与枚举值一致。同时修正 EXCITED→"surprised" 前端名字不匹配。

**MongoDB database 名可配置：** SessionRepository / MessageRepository / TokenRepository 加 `database_name` 参数（默认 `"default"`），`connect()` / `health_check()` 不再硬编码。

**Token 聚合 O(n)→O(1)：** `DocumentDBBackend` 协议加 `aggregate()`，MongoDB 实现。TokenRepository 加 `total_tokens_by_session()` / `total_tokens_by_model()`（MongoDB `$group`）。debug_routes N+1 查询 + token_tracker O(n) 累加全部替换为单次聚合查询。

**`_audio_seq` 绑定流生命周期：** `__init__` 显式初始化 + `on_audio_end` 清理对应 stream_id 条目。移除 32→16 cap 止血带，dict 大小绑定并发 TTS 流数（通常 ≤3）。

**移除无效 todo：** Optional 嵌套 dataclass（零触发）、content_type 迁入 content（投入产出比低）。

**关联 commit：** `e81d418` `b453735` `b7c9231` `dd50f78` `3b9e3ed` `4b96477` `b49f20b` `51873c2` `b81ba33` `a10a4a4` `0542f12` `fd851e0`

### 2026-07-21b: L2 Self 版本化 — LLM 编辑模式

**L3→L2 压缩升级：** LLM 从"全量重写"改为"编辑一份持续迭代的文档"。每次压缩返回结构化输出 `<new_self>` + `<changes>`（新增/强化/移除/修正）。新增 `character_l2_versions` MongoDB 集合，append-only 永久保留所有版本。Debug 面板可浏览全版本历史并回滚。

- `CompressionTrigger` StrEnum（compression/flush/manual）+ `VersionEntry` TypedDict
- `CharacterVersionRepository` — append（自动递增版本号）/ list（分页）/ get / count
- `PersonalityCompression.j2` 重写 — LLM 角色从"感受和写下"改为"编辑文档"
- `_compress(trigger=...)` 解析 `<new_self>` + `<changes>` → 写版本历史
- `_extract_tag()` 辅助函数
- `flush_compression()` 使用 FLUSH trigger
- Debug 面板：`GET /debug/api/l2-versions` + `POST /debug/api/l2-rollback`
- 前端：版本历史弹窗 + 回滚按钮
- 触发机制暂保留批触发（5 条 or 7 天），todo 标注后续改为连续增量

**关联 commit：** `b3caf14` `4af3c01` `4253bb8` `51ea0c8` `c8baa15`
