# 待做

## 已知 Bug

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 中 | **INTERRUPT + TTS 同时触发时 emoji 合并到上一气泡** | `finalizeDelta` 和下一句 delta 的时序竞争。Phase 3.5 延后，需单独 mini-spec |
| 低 | **语音识别容易被环境音触发** | VAD 对环境噪音敏感，需调整 aggressiveness 或加降噪 |
| 低 | **工具执行超时需手动测试** | `_execute_tool` 的 60s 超时，需构造慢工具场景验证 |
| 中 | **无去重机制** | 延后 — 现有测试确认重复文本+不同元数据是设计意图。需独立设计去重方案 |
| **高** | **`llm_adapter.py` `started` 标记未用于阻止中流产错重试** | `stream_think()` L98-100: `started` 设为 True 后从未在重试判断中使用。若 LLM 流式响应中途断开（已产出部分内容），重试会从头重新 yield，调用方收到重复 token。注释明确说 "Only retries if streaming hasn't started yet"，但代码未实现该逻辑。需在 L110 加 `if started: raise` |
| ~~**高**~~ | ~~**`_seed_memories()` 每次启动重复插入种子数据**~~ | **已修复 (2026-06-03)**: 移除 `_seed_memories()` 方法及其在 `start()` 中的调用。demo 遗留代码，不属于生产路径。 |
| 中 | **`emotion_engine.py` `_push_expression()` 缺失 6 种 mood 映射** | `emotion_engine.py:197-206`: `_push_expression` 仅映射 happy/excited/sad/curious 4 种 mood，STT 路径检测到的 warm/concerned/calming/reassuring/understanding 全部 fallback 到 `("neutral", 0.5)`，声学情绪检测结果在 expression 层面无法体现。 |
| 中 | **`reminder/plugin.py` coroutine 被提前 await 后传入 payload** | `plugin.py:37`: `_remind()` 被调用生成 coroutine 对象后传入 `dispatch_background`。调度器 checkpoint 恢复时 payload 中的 coroutine 不可 JSON 序列化（不持久化），且 `from_dict` 重建后 `hasattr(coro, "__await__")` 为 False。边缘场景，当前提醒工具很少跨重启执行。 |
| 低 | **`long_term.py` Milvus 匹配用 text 做 key 丢失同文异构条目** | `long_term.py:320-323`: Milvus 结果回连 in-memory 条目时用 `e.text` 做 dict key，若多条记忆文本相同但元数据不同（时间戳/重要性），仅最后一条进入候选池。当前 `insert` 端无去重，容易因种子数据重复插入触发。 |
| 低 | **数据库连接失败静默降级无 warning** | `agent.py:start()` L153-158: MongoDB/Milvus 连接失败时 `mongo_ok`/`milvus_ok` 为 False，仅跳过 `load()`，无 `logger.warning`。底层 SessionMemory/LongTermMemory 有降级保护（`if self._db`/`if self._vector_db`），但管理员无法从日志发现问题。需加 warning + 前端状态提示。 |

## 功能改进

- **poignancy 时间衰减**: rerank 含时效衰减但 poignancy 本身不变，长期不访问的记忆应逐渐降级
- **记忆去重**: insert 时检查相似内容，避免重复条目（延后）
- **按 ID 更新/删除记忆**: search() 返回记忆 ID，LLM 可引用修改；新增 update/delete API
- **记忆导出/备份**: 支持 JSON 导出和恢复
- **记忆统计**: 按类型/年龄/poignancy 分布统计，接入 DebugInfo 仪表板
- **冲突检测**: 检测矛盾记忆 (如 "最喜欢蓝色" vs "最喜欢绿色")，提示 LLM 确认
- **Claude 原生持久记忆**: `.claude/projects/-home-yomu-Agent/memory/` 目录为空，从未启用
- **MacroMemory + MacroReflector**: 每日合成日记条目。需新增 Milvus 集合 + MacroReflector 后台任务
- **EmotionEngine 升级**: 生理状态模型、时间衰减曲线、混合情绪、Amygdala LLM 补充
- **STT 情绪→mood 映射升级**: 生理状态调制 + 性格调制 + 时间衰减
- 唤醒词检测（openWakeWord）— 已完成实现，待办：
  - 测试唤醒词开关：启动服务端，打开前端设置面板，点击开关确认 ON/OFF 切换正常
  - 训练自定义唤醒词模型 (.onnx)：录制每个唤醒词 100-500 样本 → 用 openWakeWord Colab 训练 → 将 .onnx 文件导入 `model_paths`
- **性能 profiling + 优化**:
  - 时钟补上 TTS span（`_transition_idle` 里的 TTS 等待）
  - 性能回归测试（bench 模块加断言，CI 可跑）
- **角色切换**: 加载不同角色配置（名字、性格、发音人、prompt 模板），需独立 spec。涉及 CharacterConfig 重新加载、TTS voice lookup 逻辑修复（`voice` 字段当前是死代码）、CognitiveLoop 角色引用更新。`gpt_sovits_voices.yaml` 目前只有 `Elysia` 一个发音人
- **模型切换与自定义接入**: Spec 已定（`docs/superpowers/specs/2026-06-04-model-switching-design.md`），待实现。三阶段：配置驱动 → custom provider → 运行时热切换 + 前端面板
- **多角色 prompt 模板**: `PromptAssembler._prompt` 当前指向 `prompt/`，多角色时改为 `character/<name>/`
- **页面加载优化**: Live2D iframe 加载 481KB JS 竞争主线程 → WS onopen 延迟 ~200ms。方案: `loading="lazy"` / 静态 emoji 首屏 / 修复 K shader
- **Docker 化部署**: 多阶段构建 + docker-compose 编排（agent + MongoDB + Milvus + SenseVoice + GPT-SoVits）。等架构稳定后实施。

### 后台任务系统 — 后续迭代

后台任务分级 + DAG 依赖解析（`core/task.py` 中预留了 `run_in_background` / `depends_on` 字段，当前未使用）。

- `depends_on: list[str]` — TaskScheduler 需在出队前检查依赖是否完成
- `run_in_background: bool` — Worker Loop 需对 BACKGROUND 类型任务走独立执行路径
- `progress: float` — Worker 执行期间更新进度，前端可轮询

## 代码清理

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 高 | `Module/schema/` 479行预留设计 | 已添加 deprecation 注释，保留参考 |
| 中 | `database/graphdb/` 不完整 | 只有 protocol.py，保留参考 |
| 中 | `OutputManager.stream()` 未使用 | `manager.py:63-88`，保留参考 |
| 低 | LoggerFactory 死代码 | `Module/logger/factory.py` 169 行，保留参考 |

## 健壮性 & 架构

| 优先级 | 问题 | 说明 |
|--------|------|------|
| **高** | `.env` API key 需要轮换 | 已添加 `.env.example`；实际轮换需在 DeepSeek/Milvus 后台手动操作 |
| 低 | 单 Session 多客户端共享 | `session_id = "default"` 硬编码 |
| 低 | 会话管理 | 无法新建/切换/列出对话。SessionMemory 的 MongoDB 持久化已为此做好准备，需前后端联动 |

## 测试补充

- `Module/input/audio_buffer.py` (3)
- `Module/input/server.py` (4)
- `Module/output/console.py` (1)
- 并发压力测试 (4): 多 WS / 快速连续消息 / STT+文本同时

## Module 层代码审查发现 (2026-06-04)

审查范围：`Module/llm`、`Module/tts`、`Module/stt` — 结构一致性、逻辑错误、接口层、功能完备性。118/118 测试通过。

### 已知 Bug（审查新增）

| 优先级 | 问题 | 说明 |
|--------|------|------|
| **高** | **TTS `gpt_sovits.py:127-132` — `_pull()` 异常被静默吞噬** | `asyncio.Task` 中 HTTP 读取抛出 `RuntimeError` 后，`finally` 块 `except Exception: pass` 吞掉异常。非流式 `synthesize()` 返回 `b""` 作为有效音频，调用方无任何错误指示 |
| **高** | **TTS `gpt_sovits.py:141-145` + STT `sensevoice.py:134-137` — `test()` 对 4xx 返回 `ok: True`** | 两个 `test()` 用 `status_code < 500` 判定健康。401/403/404 时监控系统认为服务正常，无法检测配置错误 |
| **高** | **STT `openai_stt.py:47-48` — `last_emotion`/`last_events` 从不更新** | 初始化为 `"NEUTRAL"`/`[]`，`transcribe()` 未更新。Agent 核心 `agent.py:413-414` 每次转录后读取这两个值送入情绪管线，自定义 STT 情绪检测静默失效 |
| **高** | **TTS `openai_tts.py:46-57` — 缺少文本预处理** | `GPTSoVitsBackend` 通过 `strip_all_markup()` 移除 emoji/`[动作]`/`<<心情>>`/`<表情>`/`（语气）`，`OpenAICompatTTSBackend` 直接传原始文本。OpenAI TTS 会朗读标记如 `[点头思索]`，输出不连贯语音。GPT-SoVits 已在 commit `31bd27d` 修复，未同步到新后端 |
| **高** | **TTS `registry.py:90-97` — `from_config()` 自定义 TTS 触发 `TypeError`** | `from_config()` 将 `voices_config` 通过 `**kwargs` 传给 `OpenAICompatTTSBackend.__init__()`，该构造函数不接受此参数。`gpt-sovits` 路径正常。目前 `from_config` 未被生产代码调用，潜伏 bug |
| 中 | **LLM `openai_compat.py:195-219` — `reconnect()` 与 `__init__()` 重复客户端创建** | 两处独立构造 `AsyncOpenAI` + `httpx.Timeout(60.0, connect=10.0)`，调整超时时容易遗漏同步 |
| 中 | **STT `sensevoice.py:85` — `**kwargs` 接受但未转发到 HTTP** | `_call_api(audio, **kwargs)` 不将 kwargs 传给 `self._client.post()`，违反协议"透传"约定 |
| 中 | **TTS `gpt_sovits.py:72` — 未知角色抛出裸 `KeyError`** | `self._voices[character]` 无 try/except 包裹，调用方只看到 `KeyError: '角色名'`，不知道哪个后端/配置文件 |
| 中 | **TTS `registry.py:76-77` — 不可达的 `base_url` 检查** | `custom` 块内检查与 line 59-60 重复，提前 `raise ValueError` 使此处永不可达 |
| 低 | **TTS `protocol.py:17` — 承诺 32000Hz 但 OpenAI TTS 输出 24000Hz** | 协议声明 32000Hz/mono/int16，OpenAI `tts-1`/`tts-1-hd` 实际 24000Hz。`OpenAICompatTTSBackend` 未重采样也未注释差异 |
| 低 | **LLM `openai_compat.py:196` — `reconnect()` 文档引用不存在的 `stop()` 方法** | 实际方法是 `close()` |
| 低 | **TTS `protocol.py:24` — `synthesize_stream` 文档不精确** | 称"首 chunk 为 WAV header"，实际首 chunk = 44字节 header + ~4052字节音频 |
| 低 | **TTS `openai_tts.py:70` — `test()` 硬编码 `voice="alloy"`** | OpenAI 专用 voice 名，自定义端点可能不支持 |

### 架构改进

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 中 | **`__init__.py` 导出不一致** | LLM/STT 导出 3 符号，TTS 导出 6 符号（含 `GPTSoVitsBackend` + `strip_all_markup`/`strip_action_only`）。需统一策略：后端走工厂则全部不导出类；需公开则全部导出 |
| 中 | **环境变量命名不统一** | LLM 用 `LLM_API_BASE`，TTS/STT 用 `TTS_API_URL`/`STT_API_URL`。建议统一为 `{MODULE}_API_URL` 或 `{MODULE}_API_BASE` |
| 低 | **`create()` 参数名不统一** | TTS 用 `provider_id`，LLM/STT 用 `model_id` |
| 低 | **新建后端模式遵循不完整** | `OpenAICompatTTSBackend`(commit `2ad625e`) 和 `OpenAICompatSTTBackend`(commit `b105c77`) 未完全复制 LLM `OpenAICompatBackend` 的最佳实践：缺文本预处理、`reconnect()`、`test()` 逻辑不严谨 |
| 低 | **STT 有 `last_emotion`/`last_events` 状态，LLM/TTS 无** | 合理的领域差异，但应文档化作为接口约定 |

### 代码风格

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 低 | **LLM `openai_compat.py:196-207` — 英文注释违反 CLAUDE.md** | `reconnect()` 的 docstring 和内联注释用了英文，违反"代码注释用中文"规则 |
| 低 | **TTS `text_preprocess.py:5-12` — 英文注释** | `_EMOJI_RE` 的 Unicode block 注释用了英文 |

## 已完成 (2026-06-04)

| 日期 | 内容 |
|------|------|
| 06-04 | **模型切换与自定义接入** — 3 阶段实现：配置驱动（LLM/TTS/STT model 字段 + app_config）、custom provider（registries + custom_providers.yaml + OpenAICompatSTTBackend + OpenAICompatTTSBackend）、运行时热切换（protocol test() + Agent switch_*() + REST + WS + 前端设置面板下拉选择器 + 自定义端点模态框 + 测试连接）。915 tests。测试结果：一/二/六(Section) 全部通过；三/四/五 待测 |
| 06-04 | **6.4 错误提示修复** — LLM 调用失败时 broadcast 错误消息到前端（如 `❌ LLM 调用失败: Connection error`），不再静默失败 |
| 06-04 | **MIMO 接入** — 新增 `mimo` provider (mimo-v2.5 / mimo-v2.5-pro)，OpenAI 兼容协议。顺便修复空 `choices` 流式响应兼容 |
| 06-04 | **前端模型标签** — 角色名下方显示当前 LLM 模型名（如 `deepseek/deepseek-v4-flash`），切换时实时更新 |
| 06-04 | **模型切换测试修复（3 项）** — 修正 provider model 列表（deepseek 去 chat、ollama 改 qwen2.5:3b）；`switch_llm()` 切换前 `cancel_current()` 打断当前 thinking 防止 FSM 卡住；`cognitive_loop.process()` 加 `except Exception` 兜底，LLM 连接失败时回 IDLE |
| 06-04 | **ConfigLoader None 修复** — YAML 空值(`model:`)不再覆盖 dataclass 默认值。`_merge_yaml` 跳过 `None` |
| 06-04 | **前端 STT/TTS 状态指示** — WS capabilities 消息同步服务能力，麦克风按钮/tts toggle 在服务不可用时禁用+灰显 |
| 06-04 | **TTS 重启重建** — TTS 创建从 `__init__` 移到 `start()`，`/restart` 使用重载配置重建。TTS toggle 跟随服务端实际状态 |
| 06-04 | **TTSFactory model 验证** — model 参数传入 GPTSoVitsBackend，`gpt-sovits` provider 验证 model 为 "default" 或已知 voice 名（大小写不敏感），custom 走 OpenAICompatTTSBackend |
| 06-04 | **启动横幅显示 model** — Agent/Server 启动横幅显示 TTS/STT 实际使用的 model 名 |
| 06-04 | **数据库调试接口** — 新增 8 个 REST 端点 + Web UI 面板（/debug 数据库 Tab），支持 LTM 和 Session 的查看/新增/删除/清空。扩展 VectorDBBackend 协议新增 `delete` 方法，LongTermMemory 新增 `delete()` / `clear_all()` |
| 06-04 | **移除 `_seed_memories()`** — demo 遗留代码，每次启动重复插入 5 条种子数据 |
| 06-03 | **测试卫生** — `test_session_isolation` 测试后清理 MongoDB 中遗留的 session 文档 |

## 已完成 (2026-06-02)

| 日期 | 内容 |
|------|------|
| 06-02 | **测试缺口修复（5 阶段）** — 511 个新测试覆盖 core/tools、LLM/STT/TTS/Embedding、Output/Input、Database/Config/Logger/Prompt、Module/tool 剩余。修复 3 个源码 bug |
| 06-02 | **文档整理归档** — 16 篇已完成计划 → `superpowers/plans/archive/`，删除冗余 `task_scheduler_test.md`，更新 README 结构 |
| 06-02 | **EmbeddingFactory 全局单例缓存** — 同一模型只加载一次，多个 LongTermMemory 实例共享。`test_phase2` 从 8-9 分钟 OOM → 46 秒全部通过 |
| 06-02 | **工具系统重构（方案 C）** — Plugin 架构，SafetyPipeline 6 阶段中间件，ToolExecutor Protocol，ToolManager。删除旧 ToolRegistry/@tool 装饰器 |
| 06-02 | **core/tools/ bugfix (15/16)** — ReadFile 单文件打开 + 正确行数、网络能力检查、NETWORK capability 强制、HITL 广播 wiring
