# 开发计划

## 已完成

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
| 6.1 配置系统 | `config.yaml` 统一配置 (Agent/LLM/Memory/Server)，`demo.py --config` |
| 6.2 代码清理 | 移除死代码，统一风格 |
| 6.3 prompt 模板导入 | 从 Elysia 上层项目导入多层认知模板 (Brain/Amygdala/ActiveSpeak/Reflector)，修复集成测试 |

### 测试修复

| 任务 | 说明 |
|------|------|
| prompt 测试 | `core.Paths` → 本地路径，17 个测试全部通过 |
| database 测试 | `serverSelectionTimeoutMS` 参数匹配，47 个测试全部通过 |
| session 隔离测试 | UUID-based session_id 确保 agent 间隔离 |

### 基础设施优化

| 任务 | 说明 |
|------|------|
| 启动速度 | 嵌入模型懒加载 + thread pool；MongoDB + Milvus 并行连接；serverSelectionTimeout 3s |
| 响应延迟 | 嵌入加载不阻塞事件循环；consolidation fire-and-forget |
| 日志系统 | 库日志 → `log/` 文件（http/mongo/milvus/embedding）；终端只显示 core INFO |
| 输出去重 | ConsoleChannel 跟踪 delta 状态，流式 + 消息不重复打印 |
| 采样率修复 | AudioContext({sampleRate:16000}) 匹配 server 端 16kHz |
| 转录显示 | server → client transcription 事件，浏览器显示 🎤 气泡 |

### 测试: 197+70 passed (core) + 243 passed (Module) = 510 total

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
| 配置驱动入口 | `config.yaml` 新增 TTS.enabled / STT.enabled。`demo.py --tts`/`--stt`/`--host`/`--port` 默认值从 config 读取。优先级 CLI > config > 默认值。移除 `DEMO_ENABLE_TTS` 环境变量。 |
| Architecture.md 完整重写 | 从三层架构、Protocol-Registry-Backend 模式、事件总线/FSM、认知循环、记忆模型、输入输出系统、TTS 流式管线、文字/语音同步、配置系统、工程规范等 8 个章节完整描述项目设计。 |
| SentenceBuffer 升级 | 分隔符扩展 (～…〜)、英文句号上下文检测 (数字/缩写/URL)、连续分隔符合并、min_chunk 5 + 短句不卡死、构造参数可配置。70 tests (12 组)。 |

### 测试: 197+70 (core) + 243 (Module) = 510 total

### DeepSeek v4 适配 (2026-05-27)

| 任务 | 说明 |
|------|------|
| model config 透传 | config.yaml → DemoAgent → LLMAdapter |
| thinking mode 禁用 | `extra_body={"thinking": {"type": "disabled"}}` |
| API 错误日志 | 完整 exception + model name 打印到 WARNING |

### WebRTC VAD (2026-05-27)

| 任务 | 说明 |
|------|------|
| webrtcvad 替代 RMS | GMM 声学模型区分语音/噪音，aggressiveness=3 |
| 延迟中断 | 3 个连续 speech chunk 后才 interrupt |
| nospeech 过滤 | `<|nospeech|>` 结果丢弃 |

### 简化双轨 (2026-05-27)

| 任务 | 说明 |
|------|------|
| 去除结构化输出 | Brain.j2 纯文本，删除 TagParser + inner_voice/mood |
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
| 优雅退出 | `POST /shutdown` Web 端点 + `/quit` 终端命令 + 浏览器关闭按钮。修复 WebSocketChannel.close() 空函数、state-monitor task 泄漏、embedding 线程泄漏、stdin to_thread 阻塞等 5 个 hang |
| 测试: 494 passed（清理冗余后，-13）|

## 待做（按优先级）

- **EmotionEngine 后续升级**: ~~关键词匹配 → STT 情绪标签~~ (done)。待做：生理状态模型（Drive/Social Battery/Energy）。情绪衰减改为时间曲线。支持混合情绪。Amygdala LLM 作为文字输入语义补充。
- **STT 情绪→AI mood 映射升级**: 当前简单固定映射，后续升级为更复杂的：生理状态调制 + 性格调制 + 时间衰减曲线 + 混合情绪向量
- **ToolRegistry 升级**: 执行超时（防止卡死）。工具结果截断（防止撑爆 context）。并行工具调用（多个 tool_calls 并发执行）。新增内置工具。执行前后 hook。
- ~~**STT 识别改进**: webrtcvad 替代 RMS VAD~~ (done)
- 唤醒词检测（openWakeWord 集成）
- Web 前端: 更好的角色立绘/动画
- 单元测试补充 server.py 部分（当前通过集成覆盖）
- 性能 profiling + 优化

### 日志系统修复

| 优先级 | 问题 | 说明 |
|--------|------|------|
| ~~**高**~~ | ~~模板渲染静默失败~~ (done) | `prompt_assembler.py:82` 加 `logger.warning("Brain.j2 渲染失败，使用 fallback", exc_info=True)` |
| ~~**高**~~ | ~~WARNING 刷屏~~ (done) | `cognitive_loop.py:179,194` `logger.warning` → `logger.info` |
| ~~**高**~~ | ~~FileHandler 无滚动~~ (done) | `core/log_setup.py:73` `FileHandler` → `RotatingFileHandler`（maxBytes=10MB, backupCount=3） |
| ~~**中**~~ | ~~ToolRegistry 无日志~~ (done) | `tool_registry.py` 加 `logger`，`execute()` 成功记 INFO，失败记 WARNING |
| ~~**中**~~ | ~~WebSocket 双重日志~~ (done) | `manager.py:57` ERROR → DEBUG，保留 `websocket.py:41` WARNING |
| ~~**中**~~ | ~~STATE_CHANGE 错误级别过低~~ (done) | `demo_agent.py:408` `logger.debug` → `logger.warning` |
| **低** | LoggerFactory 死代码 | `Module/logger/factory.py` 169 行未被任何生产代码使用，只有测试引用（暂不处理） |

### 核心事件循环审查 (2026-05-27)

| 优先级 | 问题 | 说明 |
|--------|------|------|
| ~~**高**~~ | ~~快速连续消息丢失~~ (done) | `cognitive_loop.py:75` 加 `asyncio.Lock` 序列化 `process()` 调用，并发消息排队等待而非丢弃 |
| ~~**高**~~ | ~~process() 并发 ValueError~~ (done) | 同上，Lock 确保只有一个 task 执行 transition |
| ~~**高**~~ | ~~INTERRUPT 后 0.1s 魔数不可靠~~ (done) | `demo_agent.py:245` `asyncio.sleep(0.1)` → 轮询 `fsm.state == IDLE`（5s 超时） |
| ~~**中**~~ | ~~EventBus 同优先级不保证 FIFO~~ (done) | `event_bus.py:32` `Event.seq` 的 `compare=False` → `compare=True` |
| **中** | EmotionEngine._schedule 无异常边界 | `emotion_engine.py:197-205`。`loop.create_task(coro)` 创建的 task 若抛异常，asyncio 只打印 "Task exception was never retrieved" 到 stderr |
| ~~**低**~~ | ~~Consolidator 可能用 mock 提取记忆~~ (done) | MockLLMBackend 已删除，此问题不再存在 |

### 架构审查 (2026-05-27)

| 优先级 | 问题 | 说明 |
|--------|------|------|
| **高** | `Module/schema/` 479 行死代码 | `message.py`(293行) + `event.py` + `io_models.py` + `media.py` 定义了完整的共享数据模型（`UserMessage`, `ChatMessage`, `Event`, `AudioData` 等），零个生产代码导入。core 层自己用 dict 和简单 dataclass 重新实现了一遍 |
| ~~**高**~~ | ~~`MockLLMBackend` 放在 core/ 违反 L0/L1 分层~~ (done) | 删除 MockLLMBackend + fallback/_using_mock 逻辑，LLMAdapter 简化为 90 行。测试用新 `FakeLLMBackend` 替代 |
| ~~**高**~~ | ~~`Module/config/` 配置系统未接入生产代码~~ (done) | 新建 `core/app_config.py`（6 个子 dataclass），`demo.py` 用 `ConfigLoader` 加载，CLI 覆盖后传入 `DemoAgent(config=config)`。删掉 `_load_config_defaults()` 和两处手动 `yaml.safe_load()` |
| **中** | `database/graphdb/` 不完整 | 只有 `protocol.py`（ABC），缺少 `registry.py` + backend + `config.py`，无法通过 Factory 创建 |
| ~~**中**~~ | ~~`character/system.j2` 废弃但文档称"当前使用"~~ (done) | 删除 system.j2，Architecture.md/README.md 更新为 Brain.j2 |
| **中** | `StreamingSentenceBuffer` 未使用 | `sentence_buffer.py:155` 定义 25 行异步包装器，`CognitiveLoop` 直接调用 `SentenceBuffer.feed()` 而非通过它 |
| **中** | `OutputManager.stream()` 未使用 | manager.py 37 行方法提供 typing→stream→finalize 管线，`CognitiveLoop` 绕过它直接调 `_broadcast()` + `send()` |
| **低** | 单 Session 多客户端共享 | `SessionMemory.session_id = "default"` 硬编码，多浏览器标签共享同一对话状态。桌面伴侣场景问题不大，但未在架构文档说明此限制 |
| **低** | Consolidator 每轮都调 LLM | `_after_response` 每次成功回复都调 `consolidator.consolidate()`，额外一次 LLM API 调用。无节流/批处理/最小轮次启发式 |

### 已有测试质量问题 — 已清理 (2026-05-27)

| 操作 | 数量 | 说明 |
|------|------|------|
| ~~删除冗余 SentenceBuffer~~ | 10 | test_phase1 TestSentenceBuffer(6) + test_phase3 TestSentenceBufferEdges(4)，被 test_sentence_buffer.py(70) 全覆盖 |
| ~~删除弱断言无效测试~~ | 3 | test_very_long_message / test_rapid_messages / test_unicode_emoji_handling |
| ~~转换 FakeLLMBackend~~ | 3 | consolidate_extracts_facts + session_isolation + idle_touch_called |
| ~~修复弱断言~~ | 2 | consolidate 从 `mem.count >= 0` 改为验证类型；interrupt_when_not_idle 改为有效场景 |

**仍保留的集成测试 (约 10 个)**: 需要真实 LLM 行为的测试（interrupt 流式响应、多轮对话上下文、工具调用）保留使用真实 API。它们有硬编码 sleep 等待 LLM 响应，但属于必要的集成覆盖。

---

### 测试补充计划 (2026-05-27)

**总计约 80 个待补测试用例，分 3 批执行。**

#### 第一批：高优先级 (约 18 个) — server.py / demo_agent / CognitiveLoop

**`Module/input/server.py`** (零测试 → 补 7 个):
- `test_create_app_registers_routes` — /ws / /debug /debug/api /shutdown
- `test_ws_handler_text_message` — {"type":"text"} → process_message
- `test_ws_handler_interrupt_message` — {"type":"interrupt"} → INTERRUPT event
- `test_ws_handler_creates_channel` — connect → WebSocketChannel added
- `test_ws_handler_cleanup` — disconnect → channel removed + monitor cancelled
- `test_shutdown_endpoint` — POST /shutdown → agent._running=False
- `test_do_transcription_with_emotion` — STT emotion → transcription msg + USER_INPUT

**`core/demo_agent.py`** (补 8 个):
- `test_start_connects_backends` — MongoDB + Milvus 并行连接
- `test_start_loads_session` — mongo_ok 时 load() 被调用
- `test_start_seeds_memories` — 5 条初始记忆写入
- `test_stop_sequence` — 按序: idle.stop → task.cancel → session.save → lt.close → llm.close → output.close
- `test_stop_cancels_tasks` — _current_task + _tts_tasks 被 cancel
- `test_process_events_interrupt` — INTERRUPT cancel + drain + 保留 STATE_CHANGE
- `test_process_events_system_tick` — silence_timeout → proactive task
- `test_process_message_poll_wait_idle` — INTERRUPT 后轮询等 FSM.IDLE

**`core/cognitive_loop.py`** (补 3 个关键 sync 测试):
- `test_sync_event_holds_text` — TTS 启用时 text delta 进 _sync_buffer
- `test_sync_event_releases_on_first_tts_chunk` — 首 TTS chunk → flush
- `test_sync_timeout_releases_text` — 5s timeout 兜底

#### 第二批：中优先级 (约 35 个) — 剩余组件覆盖

**`core/cognitive_loop.py`** (补 8 个): start_tts_stream, drain_audio_pending, process_lock, interrupt_during_streaming, multi_tool_round

**`core/idle_monitor.py`** (补 5 个): start/stop/touch/silence_timeout/proactive_disabled

**`core/debug_info.py`** (补 4 个): collect fsm/session/lt/connections

**`core/prompt_assembler.py`** (补 3 个): stt_emotion 注入, fallback, _format_events

**`core/emotion_engine.py`** (补 3 个): push_idle, _schedule with/without loop

**`core/character_config.py`** (补 2 个): from_yaml, personality_block

**`core/app_config.py`** (补 2 个): load_from_yaml, default_values

**`core/tool_registry.py`** (补 3 个): register+execute, unknown tool, type coercion

**`core/llm_adapter.py`** (补 1 个): no_api_key_raises

**`Module/input/audio_buffer.py`** (补 3 个): deafen, flush, stt_timeout

**`Module/input/server.py`** (补 4 个): binary audio, invalid JSON, static files, do_transcription deafen

#### 第三批：低优先级 (约 25 个) — 边界/文档/压力

- `core/log_setup.py` (3): setup, rotation, routing
- `core/prompt_assembler.py` (3): _time_of_day, _day_name, _season
- `core/emotion_engine.py` (2): emoji 全量映射
- `core/tool_registry.py` (2): tool_definitions format, coerce boolean
- `core/memory/long_term.py` (3): embedding 加载/超时/close
- `core/character_config.py` (1): file_not_found
- `core/cognitive_loop.py` (1): max_tool_rounds
- `core/demo_agent.py` (2): _register_tools, _seed_memories
- `Module/output/console.py` (1): inner_voice 打印
- `Module/input/audio_buffer.py` (1): interrupt cooldown
- `Module/input/server.py` (2): static files, invalid JSON
- 并发压力测试 (4): 多 WS / 快速连续消息 / STT+文本同时 / 长时间运行