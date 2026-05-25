# 开发计划

## 已完成

### Phase 1: 认知核心 - completion (2026-05-25)

| 任务 | 说明 |
|------|------|
| 1.1 流式响应 | `stream_think()` 结构化流式输出，TTFT 毫秒级，同时检测 tool_calls |
| 1.2 中断机制 | cognitive task 独立运行，INTERRUPT 即时 cancel，回到 IDLE 后正常处理新消息 |
| 1.3 情绪系统 | EmotionEngine 关键词检测 + 强度衰减 + expression 推送，mood 注入系统提示词 |
| 1.4 SentenceBuffer | 内嵌到流式循环，逐 token feed，完整句触发 TTS 合成（有 TTS 时） |

### Phase 2: 记忆持久化 - completion (2026-05-25)

| 任务 | 说明 |
|------|------|
| 2.1 MongoDB Session | `SessionMemory` 增加 `connect_db()`/`save()`/`load()`，不可用时自动降级 |
| 2.2 Milvus + Embedding | `LongTermMemory` 支持向量语义搜索，HuggingFace 本地模型离线加载，GPU 兼容检测 |
| 2.3 记忆自动沉淀 | `MemoryConsolidator` 每轮后用 LLM 提取事实/偏好/计划，存入长期记忆 |

### 基础设施优化

| 任务 | 说明 |
|------|------|
| 启动速度 | 嵌入模型延迟加载 + init 级预加载；MongoDB + Milvus 并行连接；serverSelectionTimeout 3s |
| 响应延迟 | 嵌入加载不阻塞事件循环（thread pool）；consolidation fire-and-forget |
| 日志系统 | 库日志 → `log/` 文件（http/mongo/milvus/embedding）；终端只显示 core INFO |
| 输出去重 | ConsoleChannel 跟踪 delta 状态，流式 + 消息不重复打印 |

### 测试: 86 passed

- Phase 1: 38 tests（流式、中断、情绪、分句、多轮、边界情况）
- Phase 2: 48 tests（Session 持久化、长期记忆、记忆沉淀、集成、压力）

---

## 已完成

### Phase 6: 工程完善 — completion (2026-05-26)

| 任务 | 说明 |
|------|------|
| 6.1 配置系统 | `config.yaml` 统一配置 (Agent/LLM/Memory/Server)，`DemoAgent` 接入，`demo.py --config` |
| 6.2 代码清理 | 移除未使用的 `_shutdown`，更新签名 |
| 6.3 文档 | README/CLAUDE 更新 |

---

## 待做

### Phase 3: 输入端补齐

**3.1 WebSocket 通道** — 优先级最高
- 当前只有 stdin 输入，需要 WebSocket 接入前端
- 创建 `WebSocketInputChannel` → EventBus USER_INPUT
- 创建 `AgentServer` 组件：启动 HTTP/WS 服务器
- 输入输出都走 WebSocket（Module/output/websocket.py 已就绪）

**3.2 音频输入管道**
- 架构设计: `麦克风 → VAD → STT → EventBus`
- 接入 `Module.stt.SenseVoiceBackend`
- 实现 `AudioInputManager`：
  - VAD 检测语音起始 → Priority 0 INTERRUPT
  - STT 识别完成 → Priority 2 USER_INPUT

**3.3 唤醒词检测**
- `IdleMonitor` 目前只做定时触发
- 集成轻量唤醒词模型（openWakeWord 或类似）
- 唤醒词命中 → EventBus USER_INPUT
- 非唤醒词语音 → 忽略

### Phase 4: TTS 输出

**4.1 TTS 流式接入**
- Module/tts 已实现 `synthesize_stream`
- `CognitiveLoop` 当前用 SentenceBuffer 分句后逐句调 `synthesize`
- 改为真正的流式 `synthesize_stream`

**4.2 音频队列缓冲**
- TTS 合成（快）与前端播放（慢）之间加 `asyncio.Queue` 解耦

### Phase 5: 前端

**5.1 Web 前端原型**
- 聊天界面 + WebSocket 连接
- 接收 text_delta / expression / audio
- 发送用户文字输入

**5.2 前端个性化**
- 角色配置文件驱动前端（颜色/头像/表情集）

### 已知问题

- **Module/prompt/test_prompt.py**: 4 个集成测试 fail，引用 `from core.Paths import PROMPT_DIR`。`core/Paths` 在上层 Elysia/Agent 应用中，本 repo 不存在。需要 mock 或移除这些测试。
- **分句策略**: 短句（如"好嘞！"3字符）虽满足 min_chunk 但仍太短，TTS 启动/停止开销占比高。`～` 不是分隔符，导致 `\n` 前超长句。需要：`～` 加入分隔符，或提高 hold 阈值为 5-6 字符。
- **文字和语音不同步**: LLM token 到达即显示，但 TTS 合成有延迟，首句音频晚于首字显示。预期：首字显示时第一句音频同时播放。需要：在首句 TTS chunk 到达前 hold 文字显示，或预合成。
- **STT 识别效果**: SenseVoice 非流式（整句识别），RMS VAD 在嘈杂环境误触发。需要：更好的 VAD（webrtcvad），或持续监听 + 唤醒词模型。

### Phase 6: 工程完善

**6.1 配置系统接入**
- `DemoAgent` 部分参数硬编码
- 接入 `Module.config.ConfigLoader` → `config.yaml`

**6.2 Module/llm 完善**
- 流式模式下 tool_calls 的 chunk 格式（当前 `stream_think` 已支持结构化输出）
- ollama 后端的 tool calling 支持

---

## 优先级

| # | 任务 | 理由 |
|---|------|------|
| P0 马上 | 3.1 WebSocket 通道 | 脱离 stdin，能接前端 |
| P0 马上 | 5.1 Web 前端原型 | 从终端变成真正的桌面应用 |
| P1 本周 | 3.2 音频输入 | 真正的语音对话 |
| P1 本周 | 4.1 TTS 流式 | 语音输出 |
| P2 后续 | 6.x 工程完善 | 配置、测试、代码质量 |
