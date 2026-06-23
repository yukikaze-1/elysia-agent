# 待做

## 已知 Bug

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 中 | **INTERRUPT + TTS 同时触发时 emoji 合并到上一气泡** | `finalizeDelta` 和下一句 delta 的时序竞争。Phase 3.5 延后，需单独 mini-spec |
| 低 | **语音识别容易被环境音触发** | VAD 对环境噪音敏感，需调整 aggressiveness 或加降噪 |
| 低 | **工具执行超时需手动测试** | `_execute_tool` 的 60s 超时，需构造慢工具场景验证 |
| 中 | **无去重机制** | 延后 — 现有测试确认重复文本+不同元数据是设计意图。需独立设计去重方案 |
| **高** | **`llm_adapter.py` `started` 标记未用于阻止中流产错重试** | `stream_think()` L98-100: `started` 设为 True 后从未在重试判断中使用。若 LLM 流式响应中途断开（已产出部分内容），重试会从头重新 yield，调用方收到重复 token。注释明确说 "Only retries if streaming hasn't started yet"，但代码未实现该逻辑。需在 L110 加 `if started: raise` |
| 中 | **`emotion_engine.py` `_push_expression()` 缺失 6 种 mood 映射** | `emotion_engine.py:197-206`: `_push_expression` 仅映射 happy/excited/sad/curious 4 种 mood，STT 路径检测到的 warm/concerned/calming/reassuring/understanding 全部 fallback 到 `("neutral", 0.5)`，声学情绪检测结果在 expression 层面无法体现。 |
| 中 | **`reminder/plugin.py` coroutine 被提前 await 后传入 payload** | `plugin.py:37`: `_remind()` 被调用生成 coroutine 对象后传入 `dispatch_background`。调度器 checkpoint 恢复时 payload 中的 coroutine 不可 JSON 序列化（不持久化），且 `from_dict` 重建后 `hasattr(coro, "__await__")` 为 False。边缘场景，当前提醒工具很少跨重启执行。 |
| 低 | **`long_term.py` Milvus 匹配用 text 做 key 丢失同文异构条目** | `long_term.py:320-323`: Milvus 结果回连 in-memory 条目时用 `e.text` 做 dict key，若多条记忆文本相同但元数据不同（时间戳/重要性），仅最后一条进入候选池。当前 `insert` 端无去重，容易因种子数据重复插入触发。 |
| 低 | **数据库连接失败静默降级无 warning** | `agent.py:start()` L153-158: MongoDB/Milvus 连接失败时 `mongo_ok`/`milvus_ok` 为 False，仅跳过 `load()`，无 `logger.warning`。底层 SessionMemory/LongTermMemory 有降级保护（`if self._db`/`if self._vector_db`），但管理员无法从日志发现问题。需加 warning + 前端状态提示。 |
| **高** | **Brain.j2 硬编码工具列表严重过时（7/12）** | `Brain.j2:107-141`: 系统提示列了 7 个工具，实际注册 12 个。缺 `write_file`、`set_reminder`、`get_runtime_config`、`get_self_state`、`describe_image`。LLM 通过 API `tools` 参数能拿到 schema，但提示里说"你有以下工具"只列 7 个会误导模型。需改为动态生成工具列表或从 ToolManager 读取。 |
| **高** | **Brain.j2 缺少 "你是谁/用户是谁" 的明确定义** | `Brain.j2:155+`: 整个提示没有一句 "你是 Elysia，你在和妖梦对话"。模型只能从 `<perception>妖梦说：xxx</perception>` 推断关系。`prompt_assembler.py` fallback 分支反而写了 `你是 {name}, 用户叫 {user_name}`。需在 BrainSystemPrompt 中补充 Interaction 块。 |
| **高** | **Dual-Track Protocol 要求模型提取不存在的情绪数据** | `Brain.j2:164-175`: 模板要求模型"采纳 `<perception>` 中的 Core Emotion 和 Behavioral Tendency"，但 perception 里只有 `妖梦说：<原始文本>`，没有结构化情绪字段。STT 关闭时尤其空洞。需要么在 perception 中注入情绪数据，要么修改 Protocol 措辞。 |
| 中 | **Brain.j2 记忆时间戳渲染为 Unix 整数** | `Brain.j2:68,80`: `{{ mem.timestamp }}` 直接渲染为原始 epoch（如 `1718150400`），`datetime` filter 已在 `PromptManager` 注册但从未使用。需改成 `{{ mem.timestamp | datetime }}`。 |
| 中 | **`"妖梦"` 默认值在 4 处硬编码** | `app_config.py:11`、`prompt_assembler.py:49,105`、`agent.py:384,555`。只有 `app_config.py` 是权威配置来源，其余应 fallback 到 config 而非硬编码。 |
| 低 | **Brain.j2 中英混杂标签** | `Brain.j2`: 结构标签用英文 (`# Role Definition`, `# Sensory Input`, `# Output Format`)，人格内容/用户名/输出语言用中文。不一致且浪费 token。 |

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
- **多角色 prompt 模板**: `PromptAssembler._prompt` 当前指向 `prompt/`，多角色时改为 `character/<name>/`
- **页面加载优化**: Live2D iframe 加载 481KB JS 竞争主线程 → WS onopen 延迟 ~200ms。已加开关可关闭释放资源，开启时仍存在。方案: `loading="lazy"` / 静态 emoji 首屏 / 修复 K shader
- **Docker 化部署**: 多阶段构建 + docker-compose 编排（agent + MongoDB + Milvus + SenseVoice + GPT-SoVits）。等架构稳定后实施。
- **Brain.j2 模板改进（低优先级）**:
  - `personality_block` traits/speaking_style 为空时渲染出空字段（如 `性格: `），需加守卫
  - Jinja2 `lstrip_blocks`/`trim_blocks` 在 memory 块产生不可预测空行
  - `macro` 记忆层始终为空，死代码路径
  - L0/L2/l3 命名不一致，无 L1 层
  - `user_name` 作为 `user_id` 传入 ToolContext，名称和标识符耦合

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
| 中 | `Module/bench/` 位置不当 | bench.py 自注释"将移至 tools/bench/"，依赖 core/，后续考虑迁移 |

## 多模态 Phase 2

| 优先级 | 项目 | 说明 |
|--------|------|------|
| 中 | **AI → 用户图片输出** | 工具返回图片（WebFetch 截图、图片生成）+ LLM 生成图片 → OutputManager 广播 → 前端渲染。`ToolResult.images: list[MediaItem]` 已设计，需打通 CognitiveLoop → session → 前端链路 |
| 低 | **文件支持** | `type: "file"` content part 实现（上传 + LLM context + 前端渲染） |
| 低 | **音频/视频 Part** | `VideoPart`/`AudioPart` 已定义 dataclass，需实现全链路 |
| 低 | **content_type 迁入 content** | assistant 消息的 markdown/code block 索引迁入 content array，减少独立字段 |

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


> 已完成的工作归档在 [docs/completed.md](docs/completed.md)。
