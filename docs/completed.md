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
| Bug 修复 | `save_memory` type 校验 | 枚举约束 (fact/plan/preference/note) + 运行时参数过滤 |
| Bug 修复 | `get_time` 时区 | 增加 `timezone` + `format` 参数，默认 ISO 8601 |
| Bug 修复 | TaskScheduler 重试 | `ErrorType.is_retryable` 检查，cancel 循环检测 |
| 文件变更 | 新增 | `capability.py`、`manager.py`、`test_framework.py`、4 个插件文件、README.md |
| 文件变更 | 修改 | `protocol.py`、`safety.py`、`__init__.py`×3、`agent.py`、`cognitive_loop.py`、`task_scheduler.py`、`debug_info.py`、`server.py`、4 个迁移插件 |
| 文件变更 | 删除 | `registry.py`、`decorator.py`、`schema.py` |
| 测试 | 310/311 passed | 1 pre-existing failure (TTS config) |
| 文档 | spec + plan + README | `docs/superpowers/specs/2026-06-02-tool-system-refactor-design.md` + `docs/superpowers/plans/2026-06-02-tool-system-refactor.md` + `Module/tool/README.md` |

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

**文档:** `docs/superpowers/specs/2026-06-02-test-audit-design.md` + 5 phase plans in `docs/superpowers/plans/`

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

**Spec:** `docs/superpowers/specs/2026-06-04-model-switching-design.md`
**计划:** `docs/superpowers/plans/2026-06-04-model-switching.md`

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

**Spec:** `docs/superpowers/specs/2026-06-04-live2d-toggle-design.md`
**计划:** `docs/superpowers/plans/2026-06-04-live2d-toggle.md`

---

### 项目清理 (2026-06-05)

| 任务 | 说明 |
|------|------|
| 磁盘清理 | 删除 ~280MB 垃圾文件（`__pycache__`、`.pyc`、node_modules、临时文件） |
| UUID 统一 | 8 文件改用 `new_message_id()`，消除 `uuid4().hex[:12]` 散落 |
| 补缺失文件 | `Module/config/__main__.py`、`Module/input/__main__.py`、`Module/prompt/__main__.py`、`Module/logger/__main__.py`、`Module/input/README.md` |
| 文档整理 | `test_tool_parallel.md` 移至 `docs/`，清理 `settings.local.json` 过期引用 |

**Spec:** `docs/superpowers/specs/2026-06-05-cleanup-design.md`

### 多模态图片支持 Phase 1 — 用户→AI (2026-06-05)

| 任务 | 说明 |
|------|------|
| 消息格式 | `Module/schema/message_format.py` — TextPart/ImagePart/FilePart/AudioPart/VideoPart + ChatMessageV2 |
| 统一 content | SessionMemory/PromptAssembler content 统一为 `list[dict]` |
| 上传端点 | Server 新增 `/api/images`（multipart）+ `/api/chat`（测试）|
| base64 转换 | 本地图片自动转 data URI 发送 MIMO API |
| content_to_openai | 内部格式转 OpenAI API format |

**Spec:** `docs/superpowers/specs/2026-06-05-multimodal-phase1-design.md`

### 多模态 Phase 2 — 前端图片交互 (2026-06-05)

| 任务 | 说明 |
|------|------|
| 上传交互 | 粘贴(Ctrl+V) / 拖拽 / 📷 按钮 — 3 种方式上传 |
| 预览栏 | Slack 风格缩略图预览，× 移除 |
| 气泡渲染 | 用户气泡显示 200px 缩略图，点击打开 lightbox |
| 持久化 | session 恢复 + localStorage 支持图片 |
| 上传目录 | `data/uploads/images/`，独立 `/uploads/` 路由 |

**Spec:** `docs/superpowers/specs/2026-06-05-multimodal-phase2-frontend-design.md`

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

**Spec:** `docs/superpowers/specs/2026-06-05-vision-tool-design.md`
**计划:** `docs/superpowers/plans/2026-06-05-vision-tool.md`

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

**Spec:** `docs/superpowers/specs/2026-06-05-tool-visibility-design.md`
**计划:** `docs/superpowers/plans/2026-06-05-tool-visibility.md`

---

### 近期完成 (2026-06-22 ~ 2026-06-23)

| 日期 | 内容 |
|------|------|
| 06-23 | **前端接入 API 文档** — `docs/frontend-api.md`，完整 WebSocket 协议（16 种 Server→Client + 5 种 Client→Server）+ REST API + .NET/WPF 接入指南。含分句策略、流式 TTS 同步、工具执行生命周期、结构化内容块。经 3 轮 review 修正 |
| 06-23 | **TaskSystem debug DAG 图可视化** — 引入 vis-network CDN，替换 ASCII DAG 渲染为交互式有向图。仅改 `static/debug.html`，后端不动。spec: `docs/superpowers/specs/2026-06-23-task-dag-graph-visualization-design.md`，plan: `docs/superpowers/plans/2026-06-23-task-dag-graph-visualization.md` |
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
| 06-24 | **伴侣化改造 Step 2: 内心世界 (inner_voice)** — LLM 输出 `<inner_voice>...</inner_voice>` 标签，cognitive_loop 流式状态机（PRE→THINK→REPLY）分流，前端淡色斜体 think 气泡。持久化到 session（刷新恢复），debug 页面新增 📤 LLM Raw Response 查看器。ResponseRecorder 环形缓冲区。prompt 加入 few-shot 示例确保标签输出稳定。LLM 对话历史注入 inner_voice 格式延续。spec: `docs/superpowers/specs/2026-06-24-inner-voice-design.md` |
| 06-24 | **WS 重连恢复修复** — FSM 同状态幂等 (idle→idle no-op)、CancelledError 泄漏防御、HITL 审批重发（新客户端重连后恢复审批弹窗）、shutdown 期间工具不重试 |
| 06-24 | **Git 历史清理** — 全量 rebase: 724→164 提交（77% 缩减），按功能/主题合并。spec: `docs/superpowers/specs/2026-06-23-git-history-cleanup-design.md` |
| 06-24 | **文档全面更新** — README、companion-roadmap、companion-prompt-guide、completed、todo 同步到最新状态 |
| 06-24 | **Step 2 收尾: inner_voice toggle** — 设置面板"内心独白"开关（localStorage 持久化，默认开启）。spec: `docs/superpowers/specs/2026-06-24-inner-voice-toggle.md` |
| 06-24 | **感官/记忆模块标签汉化** — Brain.j2 `<facts>` 标签 + prompt_assembler `_time_of_day()`/`_season()` 返回值全部中文化。spec: `docs/superpowers/specs/2026-06-24-brain-sensory-zh.md` |
| 06-23 | **Brain.j2 prompt 修复** — Identity/情绪Protocol/时间戳/Perception/Interaction 修复，拒绝第三人称叙述用户 |
| 06-24 | **llm_adapter started 标记修复** — 流已开始产出 token 后遇到 429/503 不再重试，避免用户收到重复内容。spec: `docs/superpowers/specs/2026-06-24-llm-adapter-started-fix.md` |
| 06-24 | **硬编码 "妖梦" 清理** — agent.py/server.py/prompt_assembler.py 5 处硬编码全部改为从 `agent._user_name` 来源。`app_config.py` 保留为唯一权威源。personality_block 空字段守卫 + macro 记忆层标 TODO。spec: `docs/superpowers/specs/2026-06-24-hardcoded-username-cleanup.md` |
| 06-24 | **Brain.j2 渲染优化** — `Mood:`→`心情:`、工具规则+怎么回复间加空行+可见标题、latency 字段移除 |
| 06-25 | **code-review 中低危项修复** — 9 项（Python 6 + 前端 3）：_audio_seq cap、token limit、VAD log、broadcast log level、ensure_future→create_task、localhost harden、live2dStaticImage reset、ws.send return false、legacy comment。报告删除，遗留问题迁入 todo。spec: `docs/superpowers/specs/2026-06-25-code-review-fixes.md` |
