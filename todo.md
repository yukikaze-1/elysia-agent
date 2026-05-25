# 下一步计划

当前状态：L0（Module/）完成，L1+L2（core/）demo 原型完成，真实 DeepSeek API 已跑通。

## Phase 1: L1 补齐 — 认知核心完善

### 1.1 流式响应支持
- 当前 `CognitiveLoop` 用非流式 `chat()` 拿完整响应再 delivery
- 需要改成：先用非流式检测 tool_call，无 tool 则走流式，token 级 delivery 到 OutputManager
- 依赖 Module/llm 的 `chat_stream` 已就绪，只需要改 `CognitiveLoop._deliver_response`

### 1.2 中断机制完善
- 当前 `INTERRUPT` 事件通过 `task.cancel()` 取消当前推理
- 需要端到端测试：LLM 推理中按 Ctrl+C → cancel → 资源清理 → 回到 IDLE → 新消息正常处理
- 加一个 `_cleanup` 时清空 OutputManager 的 pending 消息

### 1.3 心情/情绪系统
- `CharacterConfig.emotion` 已定义阈值，但未使用
- 在 `PromptAssembler` 中注入当前 mood（已有占位 `mood: neutral`）
- 根据对话情感分析动态更新 `session.set_param("mood", ...)`
- 在 `CognitiveLoop` 中调用 `output.expression()` 推送表情变更

### 1.4 SentenceBuffer 接入
- `SentenceBuffer` 已实现，但 `CognitiveLoop` 未使用
- 流式 LLM token → SentenceBuffer.feed() → 完整句 → TTS/Output
- 配合流式响应支持一起改造

## Phase 2: L2 补齐 — 记忆与工具层实战

### 2.1 MongoDB Session 持久化
- 当前 `SessionMemory` 是内存 deque，进程重启即丢失
- 接入 `Module.database.documentdb.MongoDBBackend`
- 实现 `save_session(session_id)` / `load_session(session_id)`
- Session 内消息自动持久化，启动时恢复

### 2.2 Milvus 长期记忆
- 当前 `LongTermMemory` 是内存 list + 字符串匹配
- 接入 `Module.database.vectordb.VectorDBFactory`
- 对话摘要自动向量化 → 存入 Milvus
- `search_memory` 工具用 Embedding 做语义检索

### 2.3 记忆自动沉淀
- 对话结束后，自动调用轻量 LLM 提取摘要
- 摘要向量化存入 Milvus
- 淘汰旧记忆的 TTL 策略

## Phase 3: 输入端补齐

### 3.1 音频输入管道
- 架构设计了 `麦克风 → VAD → STT → EventBus`
- 接入 `Module.stt.SenseVoiceBackend`
- 实现 `AudioInputManager` 组件
- 对接 EventBus：
  - VAD 语音起始 → Priority 0 INTERRUPT
  - STT 识别完成 → Priority 2 USER_INPUT

### 3.2 唤醒词检测
- 当前 `IdleMonitor` 仅做定时触发，无真实唤醒词
- 集成 openWakeWord 或类似轻量模型
- 唤醒词命中 → EventBus USER_INPUT
- 非唤醒词语音 → 忽略（或可选地作为 INTERRUPT）

### 3.3 WebSocket 输入通道
- 当前用户输入来自 stdin（demo）
- 创建 `WebSocketInputChannel` → 接 EventBus USER_INPUT
- 同时输出走 `WebSocketChannel`（Module 已有）

## Phase 4: TTS 输出流水线

### 4.1 TTS 流式接入
- Module/tts 已实现 `synthesize_stream`
- `CognitiveLoop._deliver_response` 目前用非流式 `synthesize`
- 改造为：SentenceBuffer 分句 → 逐句 `synthesize_stream` → 前端播放

### 4.2 音频队列缓冲
- TTS 合成（快）与前端播放（慢）之间加 `asyncio.Queue` 解耦
- 确保长句连续说话不卡顿

## Phase 5: 前端

### 5.1 Web 前端原型
- 角色 Live2D 或静态头像 + 聊天界面
- WebSocket 连接后端
- 接收：text_delta（流式文字）、expression（表情）、audio（音频）
- 发送：用户文字输入、语音激活信号

### 5.2 前端注入 Agent 配置
- 角色配置文件 → 前端个性化（颜色/头像/表情集）

## Phase 6: 工程完善

### 6.1 测试
- core/ 各模块的单元测试（mock LLM + 真实 FSM + EventBus）
- 端到端集成测试（完整 pipeline）
- 中断场景测试（并发 + cancel）

### 6.2 配置系统接入
- 当前 `DemoAgent` 硬编码部分参数
- 创建 `config.yaml` 总配置文件
- 接入 `Module.config.ConfigLoader`

### 6.3 Module/llm 工具调用支持
- 当前 `OpenAICompatBackend.chat_stream` 只 yield text delta，不处理 tool_calls
- 新增 `chat_with_tools` 方法或改造 chunk format
- 让流式模式下也能检测和返回 tool_calls

---

## 优先级建议

| 优先级 | Phase | 原因 |
|--------|-------|------|
| P0 马上 | 1.1 流式响应 | 影响体验最大的改動，TTFT 从秒级降到毫秒级 |
| P0 马上 | 1.2 中断机制 | 桌面板核心功能，用户随时打断 |
| P1 本周 | 3.3 WebSocket | 有了 WS 就能接前端，脱离 stdin |
| P1 本周 | 5.1 Web 前端 | 从终端 demo 变成真正桌面应用 |
| P2 下周 | 1.3 心情系统 | 让角色有情绪变化，体验提升 |
| P2 下周 | 1.4 SentenceBuffer | 配合流式 + TTS 一起改 |
| P3 一月内 | 2.1/2.2 数据库 | 持久化记忆，跨会话保持上下文 |
| P3 一月内 | 3.1 音频输入 | 真正的语音对话 |
| P4 后续 | 6.x 工程完善 | 测试、配置、代码质量 |
