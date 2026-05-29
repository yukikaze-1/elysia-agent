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
| Architecture.md 完整重写 | 从三层架构、Protocol-Registry-Backend 模式、事件总线/FSM、认知循环、记忆模型、输入输出系统、TTS 流式管线、文字/语音同步、配置系统、工程规范等 8 个章节完整描述项目设计。 |
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
