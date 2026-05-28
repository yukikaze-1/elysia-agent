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

### DeepSeek v4 适配 (2026-05-27)

| 任务 | 说明 |
|------|------|
| model config 透传 | config.yaml → DemoAgent → LLMAdapter |
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
| 测试: 716 passed（+3 pre-existing failures）|

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
| 测试: 716 passed（+3 pre-existing failures）|

---

## 待做

### 已知 Bug

当前无已知高优 bug。

### 功能改进

- **EmotionEngine 后续升级**: 生理状态模型（Drive/Social Battery/Energy）。情绪衰减改为时间曲线。支持混合情绪。Amygdala LLM 作为文字输入语义补充。
- **STT 情绪→AI mood 映射升级**: 生理状态调制 + 性格调制 + 时间衰减曲线 + 混合情绪向量
- **ToolRegistry 升级**: ~~执行超时、工具结果截断、新增内置工具、执行前后 hook~~ — done，见 `Module/tool/`
- 唤醒词检测（openWakeWord 集成）
- Web 前端: 更好的角色立绘/动画
- 性能 profiling + 优化

### 后台任务系统

当前模型是严格串行的对话式处理（用户说一句 → AI 回一句），存在三个缺失的能力：

| # | 能力 | 说明 | 复杂度 |
|---|------|------|--------|
| 1 | **任务队列** | 多个请求排队，AI 逐个消化，不丢 | 低 |
| 2 | **并行执行** | 独立任务同时跑（asyncio.gather），不互相阻塞 | 中 |
| 3 | **后台任务** | 脱离对话流，AI 边聊天边干长活 | 高 |

核心洞察：对话线（快速响应，秒级）和任务线（长时执行）应共享 EventBus 但独立运行，不能混在同一个 CognitiveLoop 里。

#### 阶段 1：并行工具调用（小改，~20 行）

CognitiveLoop 内 `asyncio.gather()` 执行无依赖工具，替代当前逐个串行 `for tc in normalized: await execute()`。

- 改动范围: `core/cognitive_loop.py` 工具执行循环
- 风险: 零架构改动，LLM 原生支持并行 tool_calls
- 收益: 独立工具并行执行，减少总等待时间

#### 阶段 2：TaskQueue + TaskWorker（中改）

新增 `core/task_queue.py`，长任务 spawn 为独立 asyncio.Task，不阻塞对话。

- EventBus 新增事件: `TASK_PROGRESS` / `TASK_COMPLETE`
- FSM 叠加态: THINKING 时可以同时有后台 task 运行
- 1 个 worker 足够（瓶颈是 LLM API 延迟，不是 CPU），多 worker 带来状态一致性风险
- 关键设计: 后台任务完成 → EventBus TASK_COMPLETE → AI 主动推送结果。用户下长任务后可以继续正常聊天

#### 阶段 3：并行 Worker Pool + 任务依赖 DAG（大改，可能不需要）

多 TaskWorker 实例 + 任务依赖图（DAG）+ 共享状态锁 + 结果合并。

- 需要解决: 并发写 Session/LongTerm memory、LLM 调用合并、用户理解多任务并行输出
- **先看阶段 2 够不够用再做**

### 代码清理

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 高 | ~~`core/tool_registry.py` → `Module/tool/`~~ (done) | Module/tool/ 完成。 |
| 高 | ~~Stage B: demo_agent 迁移到 `Module/tool/`~~ (done) | `demo_agent` 使用 `@tool` 装饰器 + `Module.tool.ToolRegistry`，旧代码已删除。 |
| 高 | ~~工具结构化~~ (done) | 工具从闭包迁移到 `core/tools/`（memory.py / time.py / \_\_init\_\_.py），ToolContext 依赖注入，CognitiveLoop 支持 ctx 传递 |
| 高 | `Module/schema/` 479行预留设计 | 为 L0/L1/L2 多层架构设计，当前简化架构未使用。已保留。core 实际类型已抽到 `core/schema.py`（AgentState/EventType/EventPriority/VALID_TRANSITIONS） |
| 中 | `database/graphdb/` 不完整 | 只有 protocol.py，缺 registry/backend/config |
| 中 | `StreamingSentenceBuffer` 未使用 | `sentence_buffer.py:155` |
| 中 | `OutputManager.stream()` 未使用 | `manager.py:63-88` |
| 低 | LoggerFactory 死代码 | `Module/logger/factory.py` 169 行 |

### 健壮性

| 优先级 | 问题 | 说明 |
|--------|------|------|
| 高 | **EXECUTING 阶段不应接受 VAD 打断** | 当前 VAD 检测到语音后无条件 post INTERRUPT，无论 FSM 是什么状态。THINKING/SPEAKING 时合理（用户想插话），但 EXECUTING 时用户随便发出声音就可能取消正在执行的长任务。应改为：EXECUTING 时 VAD 忽略，只接受显式打断指令（按钮/`{"type":"interrupt"}`）。文本输入同理，EXECUTING 时应跳过打断+user input 投递 |
| 中 | `EmotionEngine._schedule` 无异常边界 | fire-and-forget task 异常时 asyncio 只打 "Task exception was never retrieved" |
| 低 | 单 Session 多客户端共享 | `session_id = "default"` 硬编码 |
| 低 | Consolidator 每轮调 LLM | 额外 API 调用，无节流/批处理 |

### 测试补充 (剩余约 12 个低优测试)

- `core/log_setup.py` (3): setup, rotation, routing
- `core/memory/long_term.py` (3): embedding 加载/超时/close
- `core/cognitive_loop.py` (1): max_tool_rounds
- `Module/output/console.py` (1): inner_voice
- `Module/input/audio_buffer.py` (1): interrupt cooldown
- 并发压力测试 (4): 多 WS / 快速连续消息 / STT+文本同时

### 日志系统修复 (历史记录)

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
| ~~**高**~~ | ~~`Module/schema/` 闲置~~ (保留) | 为 L0/L1/L2 多层架构设计，当前简化架构未使用。保留作为未来参考。core 实际使用的类型已抽到 `core/schema.py`（62 lines: AgentState/EventType/EventPriority/VALID_TRANSITIONS） |
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

#### 第一批：高优先级 (约 18 个，已完成 17 个) — server.py / demo_agent / CognitiveLoop

~~**`Module/input/server.py`**~~ (done — 8 tests, 新增 `Module/input/test_server.py`):
- ~~test_registers_routes~~ — /ws /shutdown
- ~~test_shutdown_endpoint~~ (GET+POST) — agent._running=False
- ~~test_text_message~~ — {"type":"text"} → process_message
- ~~test_interrupt_message~~ — {"type":"interrupt"} → 不崩溃
- ~~test_ws_creates_output_channel~~ — connect → channel added, disconnect → removed
- ~~test_invalid_json~~ — 非法 JSON → 不崩溃

~~**`core/demo_agent.py`**~~ (done — 6 tests):
- ~~test_seed_memories~~ — 5 条初始记忆
- ~~test_register_tools~~ — 3 个工具名
- ~~test_tool_execute_search_memory~~ — 工具实际执行
- ~~test_stop_cancels_cognitive_task~~ — stop() cancel 未完成的 task
- ~~test_stop_closes_output_channels~~ — stop() 关闭所有输出 channel
- ~~test_process_message_poll_waits_for_idle~~ — process → 等 IDLE

~~**`core/cognitive_loop.py`**~~ (done — 3 tests):
- ~~test_sync_event_preset_when_no_tts~~ — 无 TTS 时 sync_event 预置
- ~~test_sync_buffer_cleared_on_process_start~~ — process() 重置 sync 状态
- ~~test_process_lock_serializes~~ — asyncio.Lock 序列化并发调用

**修复 bug**: `server.py:97` — `monitor_task` 在 STT 未启用时未初始化，finally 块 `UnboundLocalError`

#### 第二批：中优先级 (约 35 个，已完成 24 个)

~~**`core/cognitive_loop.py`**~~ (3 done): sync_event/sync_buffer/process_lock
~~**`core/idle_monitor.py`**~~ (3 done): start/stop/touch
~~**`core/debug_info.py`**~~ (4 done): collect returns/fsm/tools/memory_counts
~~**`core/prompt_assembler.py`**~~ (3 done): format_events/format_empty/fallback
~~**`core/emotion_engine.py`**~~ (2 done): _schedule_no_loop/push_idle
~~**`core/character_config.py`**~~ (3 done): from_yaml/personality_block/file_not_found
~~**`core/app_config.py`**~~ (2 done): default_values/load_from_yaml
~~**`core/tool_registry.py`**~~ (6 done): execute_success/unknown_tool/history/definitions_format/coerce_integer/boolean
~~**`core/llm_adapter.py`**~~ (1 done): no_api_key_raises
- `Module/input/audio_buffer.py` (3 pending): deafen, flush, stt_timeout
- `Module/input/server.py` (4 pending): binary audio, invalid JSON, static files, do_transcription deafen
- `core/cognitive_loop.py` (5 pending): TTS stream task, handle_interrupt_during_streaming, multi_tool_round, max_tool_rounds, drain_audio_pending

#### 第三批：低优先级 (约 25 个，已完成 5 个)

~~- `core/emotion_engine.py` (2 done): emoji 全量映射~~
~~- `core/prompt_assembler.py` (3 done): _time_of_day, _day_name, _season~~
- `core/log_setup.py` (3): setup, rotation, routing
- `core/memory/long_term.py` (3): embedding 加载/超时/close
- `core/cognitive_loop.py` (1): max_tool_rounds
- `Module/output/console.py` (1): inner_voice 打印
- `Module/input/audio_buffer.py` (1): interrupt cooldown
- 并发压力测试 (4): 多 WS / 快速连续消息 / STT+文本同时 / 长时间运行

> 三批测试补充总计约 80 个计划，已完成约 50 个。剩余项为低优先级的边界覆盖和压力测试。