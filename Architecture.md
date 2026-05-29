# Elysia Agent 架构与设计

## 1. 架构总览

```
                        ┌──────────────────────────┐
                        │     Entry Points          │
                        │  app.py (argparse)       │
                        │  ├── interactive (stdin)  │
                        │  ├── once (single-shot)   │
                        │  └── server (WebSocket)   │
                        └──────────┬───────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                    │
    ┌─────────▼─────────┐  ┌──────▼──────┐  ┌─────────▼─────────┐
    │   config.yaml      │  │  Agent  │  │  character/*.yaml │
    │   (AppConfig)       │  │  (组装)     │  │  (CharacterConfig) │
    └───────────────────┘  └──────┬──────┘  └───────────────────┘
                                  │
         ┌────────────────────────┼────────────────────────┐
         │                        │                        │
    ┌────▼──── L2 ────────────────────────────────────────────┐
    │  Memory & Tools               │                         │
    │  ┌──────────┐ ┌────────────┐ ┌┴───────────┐ ┌────────┐ │
    │  │ Session  │ │ LongTerm   │ │Consolidator│ │  Tool  │ │
    │  │ Memory   │ │ Memory     │ │(事实提取)   │ │Registry│ │
    │  │(MongoDB) │ │(Milvus+Emb)│ └────────────┘ └────────┘ │
    │  └──────────┘ └────────────┘                            │
    └──────────────────────┬──────────────────────────────────┘
                           │
    ┌──────────────────────┼──── L1 ──────────────────────────┐
    │  Cognitive Core       │                                  │
    │  ┌──────────┐  ┌──────▼──────┐  ┌──────────────────┐    │
    │  │ EventBus │  │  AgentFSM   │  │  CognitiveLoop    │    │
    │  │(Priority │  │  (5 states) │  │  (ReAct + stream  │    │
    │  │ Queue)   │  │             │  │   + tools + sync) │    │
    │  └──────────┘  └─────────────┘  └──────────────────┘    │
    │                                                          │
    │  ┌────────────┐ ┌───────────┐ ┌───────────┐ ┌─────────┐ │
    │  │CharacterCfg│ │IdleMonitor│ │EmotionEng │ │Sentence │ │
    │  │(role+traits│ │(主动搭话)  │ │(STT+关键词 │ │Buffer   │ │
    │  │ +wake_words)│ └───────────┘ │ +衰减)    │ │(分句)   │ │
    │  └────────────┘               └───────────┘ └─────────┘ │
    │  ┌──────────────┐ ┌──────────────┐ ┌───────────────┐    │
    │  │PromptAssembler│ │  LLMAdapter  │ │  DebugInfo    │    │
    │  │(Jinja2+记忆) │ │(protocol适配)│ │(状态聚合)     │    │
    │  └──────────────┘ └──────────────┘ └───────────────┘    │
    └──────────────────────┬──────────────────────────────────┘
                           │
    ┌──────────────────────┼──── L0 ──────────────────────────┐
    │     Module/ (Protocol ─ Registry ─ Backend)              │
    │  ┌──────┐ ┌──────┐ ┌──────┐ ┌─────────┐ ┌────────┐     │
    │  │ LLM  │ │ STT  │ │ TTS  │ │Embedding│ │ Config │     │
    │  │DeepSe.│ │SenseV│ │GPT-So│ │HF/OpenAI│ │ Loader │     │
    │  └──────┘ └──────┘ └──────┘ └─────────┘ └────────┘     │
    │  ┌──────────┐ ┌───────┐ ┌──────────────────────────┐    │
    │  │ Output   │ │Logger │ │ Database                 │    │
    │  │ Manager  │ │Factor │ │ Vectordb / Docdb / Graph │    │
    │  └──────────┘ └───────┘ └──────────────────────────┘    │
    │  ┌──────────┐ ┌───────┐ ┌────────┐                     │
    │  │  Input   │ │ Schema│ │ Prompt │                     │
    │  │AudioBuf  │ │(models│ │Manager │                     │
    │  │+ Server  │ │ +msg) │ │(Jinja2)│                     │
    │  └──────────┘ └───────┘ └────────┘                     │
    └────────────────────────────────────────────────────────┘
```

三层职责分明：
- **L0 Module/**: 无状态原子能力 — 每个服务遵循 Protocol-Registry-Backend 模式，可独立替换
- **L1 core/**: 认知中枢 — 事件总线、状态机、ReAct 调度器、情绪引擎、提示词组装
- **L2 core/memory/**: 记忆与工具 — 会话持久化、语义召回、事实提取

---

## 2. 完整数据流

```
                     ┌── 语音路径 ──────────────────────────────────────┐
                     │                                                  │
  Browser Mic ──► AudioBuffer ──► VAD ──► STT ──► emotion tag          │
   (Int16 PCM)     (WebRTC)     (WebRTC)  (SenseVoice)  (HAPPY/SAD...)  │
                     │                                    │             │
                     │ INTERRUPT (skip THINKING/EXECUTING)   │             │
                     │                                    ▼             │
                     │                         stt_emotion_state{}       │
                     │                                    │             │
                     ▼                                    ▼             │
  ┌──────────────────────────────────────────────────────────────────┐  │
  │                        EventBus                                   │  │
  │    Priority 0: INTERRUPT                                         │  │
  │    Priority 1: STATE_CHANGE                                      │  │
  │    Priority 2: USER_INPUT (text + emotion) / SYSTEM_TICK         │  │
  └────────────────────────────┬─────────────────────────────────────┘  │
                               │                                        │
                               ▼                                        │
  ┌──────────────────────────────────────────────────────────────────┐  │
  │  AgentFSM:   IDLE ──► THINKING ──► (EXECUTING ◄─► THINKING)*     │  │
  │                               └──► SPEAKING ──► IDLE              │  │
  └────────────────────────────┬─────────────────────────────────────┘  │
                               │                                        │
                               ▼                                        │
  ┌──────────────────────────────────────────────────────────────────┐  │
  │  PromptAssembler.build_messages(user_message)                     │  │
  │    ├── CharacterConfig.personality_block()   # 角色性格           │  │
  │    ├── SessionMemory.recent_messages(8)      # 最近对话           │  │
  │    ├── LongTermMemory.search(query, top_k=5) # 语义召回           │  │
  │    ├── SessionMemory.get_param("mood")       # 当前心情           │  │
  │    ├── stt_emotion / stt_events              # 声学情绪           │  │
  │    └── prompt/Brain.j2 ──► system prompt                          │  │
  └────────────────────────────┬─────────────────────────────────────┘  │
                               │                                        │
                               ▼                                        │
  ┌──────────────────────────────────────────────────────────────────┐  │
  │  CognitiveLoop._think_loop()                                       │  │
  │                                                                   │  │
  │  for round in 0..MAX_TOOL_ROUNDS(3):                              │
  │    LLM.stream_think(messages, tools=tool_defs)                    │
  │      │                                                            │
  │      ├── content delta ──► SentenceBuffer.feed(token)             │
  │      │    │    └── sentence complete                               │
  │      │    │          ├── [TTS on]  _start_tts_stream(sentence)    │
  │      │    │          │     └── bind-send delta + audio            │
  │      │    │          └── [TTS off] direct delta + send("")        │
  │      │    │                                                       │
  │      │    └── OutputManager ──┬── ConsoleChannel (stderr)         │
  │      │                        └── WebSocketChannel → Browser      │
  │      │                              ├── text delta (sentence)      │
  │      │                              └── audio chunk (stream)       │
  │      │                                                       │    │
  │      └── tool_calls ──► asyncio.gather ──► inject ──► loop───────┘
  │  EmotionEngine.analyze_assistant() ──► expression 推送            │  │
  │  MemoryConsolidator.consolidate() ──► LongTermMemory (fire-forget)│  │
  │  SessionMemory.save() ──► MongoDB                                 │  │
  │  FSM → IDLE                                                       │  │
  └──────────────────────────────────────────────────────────────────┘  │
                                                                        │
  Browser Client (static/index.html)                                    │
    ├── text delta → appendDelta() → merged bubble                     │
    ├── audio chunk → audioQueue → playNext() → Audio.play()            │
    │    └── .then() → flushTextQueue()  (文字/音频同步)                │
    ├── expression → avatar ring CSS animation                         │
    ├── state → status indicator (pulse)                               │
    └── localStorage persistence (debounce 500ms)                      │
```

### 2.1 文字/语音同步（绑定发送）

服务端 TTS 后台 task 等首块音频到达后，绑在一起发送文字+音频：

**Server 端** (CognitiveLoop):
```
SentenceBuffer 分句 → _start_tts_stream(sentence) [background task]
  → TTS 首 chunk 到达
    → 绑定发送: delta(完整句) + send("") + audio_chunks + audio_end
    → workers=1 FIFO 保证句间顺序
  → TTS 关闭时: 直接发 delta + send("")
```

**Browser 端** (index.html):
```
delta 到达 → appendDelta() 直接渲染气泡
audio chunk 到达 → audioQueue → playNext() 顺序播放
audioPlaying=true 时新气泡暂存 nextBubbleQueue → 等音频结束再弹出
```

---

## 3. L0 基础设施层 (Module/)

### 3.1 Protocol-Registry-Backend 模式

每个服务模块统一结构：

```
Module/<service>/
├── protocol.py      # ABC 抽象接口
├── registry.py      # Factory + _BACKENDS 字典
├── <backend>.py     # 具体实现
├── config.py        # Dataclass 配置 (ConfigField)
├── __init__.py      # 重导出 ABC + Factory + Config
├── __main__.py      # 集成烟雾测试
└── test_*.py        # 单元测试 (mock)
```

Factory 模式：
```python
# 使用示例
llm = LLMFactory.create("deepseek/deepseek-chat")
# Factory 自动: 读取 DEEPSEEK_API_KEY env var → 注入 caller-aware logger → 返回实例

# Registry 注册
_BACKENDS = {
    "deepseek/deepseek-chat": {
        "class": OpenAICompatBackend,
        "base_url": "https://api.deepseek.com/v1",
        "env_key": "DEEPSEEK_API_KEY",
        "supports_prefix": True,
    },
}
```

### 3.2 已实现 Backend

| 模块 | Backend | 协议 | 说明 |
|------|---------|------|------|
| `Module.llm` | `deepseek`, `openai`, `ollama` | OpenAI-compatible | `stream_think()` 结构化流式 (content + tool_calls 并行检测) |
| `Module.stt` | `sensevoice` | HTTP API | `localhost:20042`，返回文本 + 情绪标签 + 音频事件 |
| `Module.tts` | `gpt-sovits` | HTTP streaming | `synthesize_stream()` 异步生成器，逐 chunk WAV |
| `Module.embedding` | `huggingface`, `openai` | 本地模型 / API | HF 离线加载 + GPU 兼容 (sm_<70 → CPU fallback) |
| `Module.database.vectordb` | `milvus` | gRPC | ANN 语义搜索 |
| `Module.database.documentdb` | `mongodb` | motor async | `serverSelectionTimeoutMS=3000` |
| `Module.database.graphdb` | stub | — | 预留 |
| `Module.output` | `ConsoleChannel`, `WebSocketChannel` | — | 多通道广播 + capability 路由 |
| `Module.config` | `ConfigLoader` | YAML + env | 分层配置 + 类型校验 |
| `Module.logger` | `LoggerFactory` | — | caller-aware 日志工厂 |
| `Module.prompt` | `PromptManager` | Jinja2 | 模板管理 + macro 渲染 |

### 3.3 Config 系统

```python
# core/app_config.py
@dataclass
class AppConfig:
    agent: AgentConfig = ConfigField(default_factory=AgentConfig)
    llm: LLMConfig = ConfigField(default_factory=LLMConfig)
    memory: MemoryConfig = ConfigField(default_factory=MemoryConfig)
    server: ServerConfig = ConfigField(default_factory=ServerConfig)
    tts: TTSConfig = ConfigField(default_factory=TTSConfig)
    stt: STTConfig = ConfigField(default_factory=STTConfig)

# 使用
config = ConfigLoader(AppConfig, yaml_path="config.yaml").load()
# 优先级: 环境变量 > YAML > dataclass 默认值

# CLI overrides
config.tts.enabled = args.tts    # --tts flag overrides config.yaml
config.stt.enabled = args.stt
```

`ConfigField` 特性: `secret=True` (env-only), `constraints` (Range/Choices/Regex), `env` (env var 绑定)

### 3.4 Output System

```python
class ChannelCapability(StrEnum):
    TEXT_DELTA = "text_delta"   # 流式文字增量
    TEXT_MESSAGE = "text_message"  # 完整消息
    TYPING = "typing"           # 输入中指示器
    EXPRESSION = "expression"   # 表情/情绪
    ACTION = "action"           # 动作指令
    AUDIO = "audio"             # 音频流
    COMMAND = "command"         # 前端指令

class OutputManager:
    def _broadcast(method, *args, capability):
        # 按 capability 过滤通道，并行广播
        # 单通道失败不影响其他通道 (return_exceptions=True)
```

---

## 4. L1 认知控制层

### 4.1 事件总线 (EventBus)

基于 `asyncio.PriorityQueue`，三重优先级 + FIFO 保序：

```python
class EventPriority(IntEnum):
    INTERRUPT = 0      # 最高：取消当前处理，返回 IDLE
    STATE_CHANGE = 1   # 中：FSM 状态变更，推送 UI
    NORMAL = 2         # 低：USER_INPUT / SYSTEM_TICK

@dataclass(order=True)
class Event:
    priority: int = field(compare=True)
    type: str = field(compare=False)
    data: Any = field(compare=False, default=None)
    seq: int = field(compare=True, default=0)  # FIFO within same priority
```

INTERRUPT 处理流程：
```
1. cancel self._cognitive._current_task (CancelledError → handle_interrupt)
2. drain() 清空队列
3. 保留 STATE_CHANGE 事件 (用于 UI 更新)
4. FSM: 当前态 → INTERRUPTED → IDLE
```

### 4.2 有限状态机 (AgentFSM)

```
                         ┌──────────────────────────────┐
                         │         INTERRUPTED           │
                         │  cancel tasks, clear buffers  │
                         │   → 50ms pause (UI register)  │
                         └──────────────┬───────────────┘
                                ▲       │
                     any non-IDLE      │ cleanup
                                │       ▼
  ┌──────┐   USER_INPUT   ┌──────────┐      tool_calls   ┌──────────┐
  │ IDLE │ ─────────────► │ THINKING │ ◄───────────────► │EXECUTING │
  │      │ ◄───────────── │          │                    │          │
  └──▲───┘    INTERRUPT   └────┬─────┘                    └──────────┘
     │                         │
     │                    LLM done
     │                         │
     │                    ┌────▼──────┐
     └────────────────────│ SPEAKING  │
              done        │           │
                          └───────────┘
```

有效转移：
```
IDLE        → THINKING
THINKING    → SPEAKING | EXECUTING | INTERRUPTED
EXECUTING   → THINKING | INTERRUPTED
SPEAKING    → IDLE | EXECUTING | THINKING | INTERRUPTED
INTERRUPTED → IDLE
```

### 4.3 认知循环 (CognitiveLoop)

ReAct 调度器的完整实现 (~400 行)：

```python
class CognitiveLoop:
    MAX_TOOL_ROUNDS = 3  # 防止无限工具调用

    async def process(self, user_message, user_name, stt_emotion, stt_events):
        async with self._process_lock:          # 串行化并发调用
            if not can_transition(THINKING):     # 已在处理中 → skip
                return
            fsm.transition(THINKING)
            sentence_buf.reset()
            try:
                await self._think_loop(user_message, ...)
            except CancelledError:
                await self._handle_interrupt()    # 清理 → INTERRUPTED → IDLE

    async def _think_loop(self, ...):
        # 1. 情绪分析 (STT 优先，关键词 fallback)
        # 2. 组装消息: prompt.build_messages()
        # 3. TTS 关闭: delta 直通 / TTS 开启: _start_tts_stream 绑定发送

        for round in range(MAX_TOOL_ROUNDS + 1):
            async for chunk in llm.stream_think(messages, tools=tool_defs):
                if content:
                    sentence = sentence_buf.feed(content)
                    if sentence:
                        if tts and not emoji_only:
                            start_tts_stream(sentence)  # 绑定发送 text+audio
                        else:
                            output delta + send("")     # 直接发送

                if tool_calls:
                    merge into accumulated list

            # 工具调用: asyncio.gather 并行执行，单失败不影响其他
            if raw_tool_calls and round < MAX_TOOL_ROUNDS:
                cancel TTS tasks → fsm → EXECUTING
                results = await asyncio.gather(*tools, return_exceptions=True)
                inject results → continue loop

            # 最终回复: emotion → session.save → _after_response (fire-forget)
            fsm → IDLE
            return
```

关键设计决策：
- **asyncio.Lock**: 确保 `process()` 不可重入，防止并发消息导致状态混乱
- **Fire-and-forget 后处理**: consolidation + session save 异步执行，用户可立即发下一条消息
- **TTS 绑定发送**: 文字和音频在服务端绑在一起发送，前端无需同步逻辑
- **并行工具调用**: `asyncio.gather` + `return_exceptions=True`，单工具失败不影响其他
- **VAD 状态保护**: THINKING/EXECUTING 时 VAD 不打断，STT 文本和显式按钮始终生效

### 4.4 TTS 绑定发送 + 句队列

```
LLM token stream
  → SentenceBuffer.feed(token)
    → sentence complete → CognitiveLoop._start_tts_stream(sentence)
      → TTS.synthesize_stream(sentence) [background task]
        → 首 chunk 到达 → 绑定发送: delta(完整句) + send("") + audio_chunks
          → WebSocket → browser appendDelta + audioQueue → playNext()

句队列: 多句并行合成
  stream_id = ++tts_stream_id
  create_task(_stream(stream_id))   # 不 cancel 前句，自然排队
  workers=1 FIFO 保证句间顺序，前端 nextBubbleQueue 等音频播完再弹出
```

### 4.5 情绪引擎 (EmotionEngine)

**主路径** (语音): STT 声学情绪标签 (SenseVoice 自带)
```
STT 返回: emotion="HAPPY", events=["Laughter"]
  → _STT_EMOTION_TO_MOOD 映射:
      HAPPY → "warm", SAD → "concerned", ANGRY → "calming"
      FEARFUL → "reassuring", SURPRISED → "curious"
  → 更新 session.params["mood"] + push expression

音频事件 → expression 推送:
  Laughter → "laugh", Cry → "cry", Applause → "surprised"
```

**Fallback** (文本): 关键词匹配
```
MOOD_KEYWORDS = {
    "happy":    ["哈哈", "开心", "太好了", "喜欢", ...],
    "excited":  ["!", "！", "天哪", "wow", ...],
    "sad":      ["难过", "伤心", "哭", "失败", ...],
    "curious":  ["为什么", "怎么", "好奇", ...],
}
→ 计分 → 最高分 mood → update session.params + push expression
```

**衰减**: `analyze_assistant()` 每次回复后 intensity -= 0.15，低于 0.2 重置为 neutral

**Expression 映射**: `{mood: (expression, intensity)}` → OutputManager.expression()

### 4.6 分句缓冲 (SentenceBuffer)

```
SPLIT_PATTERN = re.compile(r"([。！？.!?\n])")

逐 token feed → 检测分隔符 → 提取完整句
  连续分隔符合并: 剩余文本以分隔符开头 → hold 到下次
  短句阈值: 剩余 < 2 字符 → hold (避免 "好嘞！" → "好嘞" + "！")
  flush() → 返回剩余未完成文本

配置项: split_pattern, min_sentence_len, hold_short, merge_consecutive
70 个单元测试覆盖边界
```

### 4.7 提示词组装 (PromptAssembler)

```
build_messages(user_message, stt_emotion, stt_events)
  │
  ├── build_system_prompt()
  │     ├── CharacterConfig.personality_block()     # 性格描述
  │     ├── sensory (时间/季节/情绪/事件)            # 感知上下文
  │     ├── LongTermMemory.search(query, top_k=5)   # 语义召回
  │     ├── SessionMemory.get_param("mood")          # 当前心情
  │     └── prompt/Brain.j2 (Jinja2)                # 双轨思维协议
  │
  ├── SessionMemory.recent_messages(8)  # 最近对话
  └── {"role": "user", "content": user_message}

Fallback: Brain.j2 渲染失败 → _render_fallback() 硬编码模板
```

`prompt/Brain.j2` 当前使用的系统模板（双轨思维协议：内部独白 → 外部回复）。其余模板 (Amygdala/ActiveSpeak/MicroReflector/MacroReflector) 待接入。

### 4.8 空闲监控 (IdleMonitor)

```
每 1s tick (仅在 IDLE 状态):
  ├── silence_timeout: elapsed >= duration → SYSTEM_TICK("silence_timeout")
  │     → CognitiveLoop.process("系统提示：用户已经 N 秒没有说话...")
  └── time_of_day: 当前时间 in trigger times → SYSTEM_TICK("time_of_day")

touch(): 用户交互时重置 _last_interaction
proactive.enabled: false → 整个机制关闭
```

### 4.9 LLMAdapter

薄适配层 (~90 行)：接收 `model` 标识符 (如 `"deepseek/deepseek-chat"`)，通过 `LLMFactory.create()` 创建 Module 层 backend，提供 `think()` 和 `stream_think()` 方法。

测试用 `FakeLLMBackend` (core/_fake_llm.py) — 最小化 test double，直接 yield 预设字符串。

---

## 5. L2 记忆与工具层

### 5.1 记忆三层模型

| 层 | 存储 | 生命周期 | 容量 | 用途 |
|------|------|------|------|------|
| Working | 内存 (单次推理) | 单次 LLM 调用 | 动态 | PromptAssembler 组装 |
| Session | MongoDB / deque | 跨进程重启 | 最近 20 轮 | 对话上下文 + 运行时参数 |
| Long-term | Milvus + 内存 | 跨会话 | 无上限 | 事实/偏好/计划 语义搜索 |

### 5.2 SessionMemory

```python
class SessionMemory:
    _messages: deque[dict]        # 最多 max_messages 条
    _params: dict                 # mood, mood_intensity 等
    _db: DocumentDBBackend | None # MongoDB 或降级

    # MongoDB 自动检测 — 不可用时静默降级为 in-memory
    async connect_db() → bool
    async save() → bool           # Upsert by session_id
    async load(sid?) → bool       # 恢复最近 max_messages 条
    snapshot() / restore()        # 快照 (测试用)
```

### 5.3 LongTermMemory

```python
class LongTermMemory:
    _entries: list[MemoryEntry]          # 内存缓存 (always available)
    _vector_db: VectorDBBackend | None   # Milvus (optional)
    _embedding: EmbeddingBackend | None  # HuggingFace/OpenAI (lazy)

    async connect()          # Milvus 连接 (fast)
    async insert(text, ...)  # 写 Milvus + 内存
    async search(query, k)   # Milvus ANN → fallback: 字符重叠

    # 懒加载: _ensure_embedding() 在 thread pool 运行
    # 优先 OpenAI API (fast)，fallback HuggingFace 本地模型
    # 超时 10s → 降级为 text-only
    # GPU 兼容: sm_<70 → CPU fallback
```

### 5.4 MemoryConsolidator

攒 6 条消息后调 LLM 提取，fire-and-forget 执行。提取 Prompt 要求事件合并、重要性评分 1-10、第一人称叙事。

```
缓冲 6 条消息 → EXTRACTION_PROMPT (事件合并 + poignancy)
  → LLM 提取: [{"type":"Fact","content":"...","poignancy":7}]
  → LongTermMemory.insert(poignancy=7, memory_type="Fact")

_history: 最近 20 条 (供 /debug 面板)
_buffer: 未达阈值的待处理对话
```

### 5.5 ToolRegistry

OpenAI Function Calling Schema 标准化：

```python
tools.register(
    name="search_memory",
    description="在长期记忆中搜索相关信息",
    parameters={...},       # JSON Schema
    func=async_search_memory,
)

# CognitiveLoop 自动:
# tool_definitions() → LLM 请求
# LLM 返回 tool_calls → execute() → 结果回注 messages
# type coercion: "5" → 5 (integer), "true" → True (boolean)
```

已注册工具（7 个）：
| 工具 | 参数 | 功能 |
|------|------|------|
| `search_memory` | query, limit | 语义搜索长期记忆 |
| `save_memory` | content, type, poignancy | 保存新记忆 |
| `get_time` | — | 获取当前日期时间 |
| `web_search` | query, engine | DuckDuckGo / Brave 搜索 |
| `web_fetch` | url | 抓取网页内容 |
| `read_file` | path | 读取本地文件 |
| `list_directory` | path | 列出目录内容 |

CognitiveLoop 使用 `asyncio.gather` 并行执行无依赖工具。

---

## 6. 输入输出系统

### 6.1 音频输入管道

```
Browser Microphone
  → AudioContext(16000 Hz, 1ch, Int16)
  → ScriptProcessorNode(4096 buffer)
  → Int16 PCM → WebSocket binary message
  → Server AudioBuffer.feed(pcm)
    → WebRTC VAD (GMM, aggressiveness=3)
      → speech detected → INTERRUPT + accumulate
      → silence 1.5s → segment complete
        → PCM → WAV conversion
        → STT.transcribe(wav) → text + emotion + events
          → EventBus.USER_INPUT
```

`AudioBuffer.deafen(duration)`: TTS 播放期间忽略麦克风输入 (2s)，防止回声循环。

### 6.2 WebSocket 消息协议

**Server → Browser:**

| type | payload | 说明 |
|------|---------|------|
| `delta` | `{"content": "..."}` | 流式文字增量 |
| `message` | `{"role":"assistant","content":"..."}` | 完整消息（流式结束） |
| `typing` | `{"typing": true/false}` | 输入中指示器 |
| `state` | `{"state": "idle/thinking/speaking/..."}` | FSM 状态 |
| `expression` | `{"expression":"happy","intensity":0.8}` | 情绪/表情 |
| `audio` | `{"data":"base64...","stream_id":...,"seq":...}` | 音频 chunk |
| `audio_end` | `{"stream_id": ...}` | 音频流结束 |
| `transcription` | `{"text":"...","emotion":"HAPPY","events":["Laughter"]}` | STT 结果 |

**Browser → Server:**

| type | payload | 说明 |
|------|---------|------|
| `text` | `{"type":"text","content":"你好"}` | 文本输入 |
| `interrupt` | `{"type":"interrupt"}` | 打断按钮 |
| binary | Int16 PCM bytes | 麦克风音频 |

### 6.3 WebSocket 服务器 (Module/input/server.py)

```
aiohttp Application:
  GET  /            → static/index.html
  GET  /debug       → static/debug.html
  GET  /debug/api   → debug_info.collect() JSON
  GET  /ws          → WebSocket handler
  POST /shutdown    → agent._running = False
  GET  /shutdown    → 同上

每个 WS 连接:
  └── 创建 WebSocketChannel → OutputManager
  └── 创建 AudioBuffer (if STT enabled)
  └── 启动 state-monitor task:
        FSM → "speaking" → audio_buf.deafen(2.0)  # 防回声
  └── 消息循环: TEXT → process_message, BINARY → audio_buf.feed()
  └── disconnect: cleanup channel + audio_buf + monitor
```

### 6.4 浏览器客户端 (static/index.html)

三栏布局：
- **左栏** (220px): 角色面板 — 表情 ring (CSS border-color)、状态灯 (pulse 动画)、心情标签
- **中栏**: 对话区 — 消息气泡、时间戳分隔线 (5min)、typing dots 动画
- **右栏**: 设置面板 — TTS 开关/音量/清空对话/连接状态/关闭服务

关键实现：
- AudioContext 16kHz → Int16 PCM → WS binary
- `appendDelta()` 合并到同一 bubble，`finalizeDelta()` 存档
- `audioQueue` + `playNext()` 串行播放
- localStorage 持久化 (debounce 500ms)
- 响应式 (窄屏 <600px 折叠侧栏)

---

## 7. 配置系统

### 7.1 config.yaml

```yaml
agent:
  character: character/elysia.yaml   # 角色文件
  user_name: 妖梦                    # 用户名

llm:
  model: deepseek/deepseek-v4-flash  # LLM 模型标识

memory:
  session_max_messages: 20           # 会话窗口大小

server:
  host: 0.0.0.0
  port: 8765

tts:
  enabled: true                      # CLI --tts 可覆盖

stt:
  enabled: true                      # CLI --stt 可覆盖
```

优先级: `CLI flag > config.yaml > AppConfig 默认值`

### 7.2 角色配置 (character/elysia.yaml)

```yaml
name: Elysia
voice: elysia_v2
personality:
  traits: [温柔, 好奇, 偶尔毒舌]
  speaking_style: "口语化，短句为主..."
emotion:
  thresholds: {excitement: 0.6, sadness: 0.3}
  idle_expressions: [neutral, blink, slight_smile]
wake_words: [Elysia, 爱莉, 爱莉希雅, 妖精小姐]
proactive:
  enabled: false                     # 主动搭话开关
  interval: 300                      # 检查间隔 (秒)
  triggers:
    - type: silence_timeout
      duration: 30                   # 30秒不说话 → 搭话
    - type: time_of_day
      times: ["08:00", "12:00", "22:00"]
```

---

## 8. Debug Dashboard

`GET /debug` → `static/debug.html` (183 行)，每 2s 轮询 `GET /debug/api`:

```
┌──────────────────────┬──────────────────────┐
│ System               │ Memory               │
│ FSM state/previous   │ Session messages     │
│ Processing flag      │ Session params       │
│ LLM model            │ LT count + entries   │
│ TTS connected        │ Milvus / Embedding   │
│ WS clients           │ MongoDB connected    │
├──────────────────────┼──────────────────────┤
│ Tools                │ Consolidation        │
│ tool_names list      │ Recent history       │
│ Recent history (5)   │                      │
├──────────────────────┼──────────────────────┤
│ Recent Session (5)   │ Long-term Memory (5) │
│ role + content       │ text + metadata      │
└──────────────────────┴──────────────────────┘
```

---

## 9. 组件组装 (Agent)

```python
class Agent:
    def __init__(self, *, config: AppConfig):
        # L0: OutputManager + ConsoleChannel
        # Character: CharacterConfig.from_yaml(config.agent.character)
        # L2: SessionMemory + LongTermMemory + ToolRegistry
        #     + register 3 tools
        # L1: EventBus + AgentFSM + LLMAdapter + PromptAssembler
        #     + EmotionEngine + MemoryConsolidator
        #     + CognitiveLoop + IdleMonitor
        # TTS: 根据 config.tts.enabled 决定是否初始化

    async def start():
        # MongoDB + Milvus 并行连接
        # _seed_memories() — 5 条初始记忆 (in-memory only, no embedding)
        # SessionMemory.load() — 恢复历史会话
        # embedding 预加载 (background task, thread pool)
        # IdleMonitor.start()
        # 事件处理循环 (_process_events)

    async def stop():
        # cancel cognitive + TTS tasks
        # cancel embedding preload
        # idle_monitor.stop()
        # event_task.cancel()
        # session.save() (3s timeout)
        # long_term.close()
        # llm.close()
        # tts.close()
        # output.close()
        # (每步 3s timeout，单步失败不阻塞后续)

    async def process_message(text):
        # idle_monitor.touch()
        # if FSM != IDLE: post INTERRUPT + poll-wait (5s timeout)
        # post USER_INPUT

    async def run_server():
        # start() → create_app() → aiohttp server
        # stdin watcher: /quit → _running = False
        # cleanup: stdin.close() → stop() → runner.cleanup()
```

---

## 10. 日志系统

```
core/log_setup.py:
  库日志 (pymongo/pymilvus/httpx/openai/torch)
    → log/*.log (RotatingFileHandler, 10MB × 3)
  核心日志 (core/Module/asyncio)
    → stderr (INFO, --verbose → DEBUG)
  stdout → 仅对话输出 (不污染)

Child loggers (core.cognitive_loop 等)
  → propagate=True → 继承父 logger 的 stderr handler
```

---

## 11. 测试架构

```
708 passed (243 core + 465 Module)

core/tests/
├── test_phase1.py             # L1 认知核心 (38 tests)
│   EventBus, AgentFSM, CognitiveLoop, EmotionEngine, SentenceBuffer
├── test_phase2.py             # L2 记忆持久化 (48 tests)
│   SessionMemory, LongTermMemory, MemoryConsolidator
├── test_phase3.py             # 集成 + 组件测试 (148 tests)
│   CognitiveLoop TTS/tools, Agent lifecycle, IdleMonitor,
│   DebugInfo, ToolRegistry, VAD interrupt, PromptAssembler,
│   CharacterConfig, AppConfig, LLMAdapter errors
├── test_sentence_buffer.py    # SentenceBuffer 专项 (70 tests)
└── fake_llm.py                # 测试用 LLM backend

Module/
├── llm/test_llm.py
├── stt/test_stt.py
├── tts/test_tts.py
├── embedding/test_embedding.py
├── output/test_output.py      # OutputManager + WebSocketChannel
├── config/test_config.py
├── logger/test_logger.py
├── prompt/test_prompt.py
├── tool/test_tool.py           # ToolRegistry + decorator + safety (172 tests)
├── database/vectordb/
├── database/documentdb/
├── input/test_audio_buffer.py # AudioBuffer + WebRTC VAD (15 tests)
└── input/test_server.py       # WebSocket server + _do_interrupt (13 tests)
```

运行：
```bash
python -m pytest core/tests/ Module/ -q --asyncio-mode=auto
```

---

## 12. 文件结构

```
Agent/
├── app.py                  # 入口 (argparse + ConfigLoader + CLI overrides)
├── config.yaml              # 总配置
├── Architecture.md          # 本文档
├── CLAUDE.md                # AI 助手指令
├── README.md                # 项目说明
├── todo.md                  # 开发计划 + 已知问题
├── test_plan.md             # 测试计划
│
├── Module/                  # L0 基础设施层
│   ├── llm/                 # LLM: DeepSeek / OpenAI / Ollama
│   ├── stt/                 # STT: SenseVoice
│   ├── tts/                 # TTS: GPT-SoVits
│   ├── embedding/           # Embedding: HuggingFace / OpenAI
│   ├── output/              # Output: Console + WebSocket 多通道
│   ├── config/              # Config: YAML + env 加载器 + 验证
│   ├── logger/              # Logger: caller-aware 工厂
│   ├── prompt/              # Prompt: Jinja2 模板管理
│   ├── schema/              # Schema: 共享数据模型 (Event/Message/Media)
│   ├── database/            # Database: Milvus / MongoDB / GraphDB
│   ├── input/               # Input: AudioBuffer (VAD) + WebSocket server
│   └── bench/               # Bench: 延迟基准
│
├── core/                    # L1 认知控制
│   ├── schema.py            #   共享类型 (AgentState, EventType, EventPriority)
│   ├── event_bus.py         #   优先级事件总线
│   ├── agent_fsm.py         #   五状态机
│   ├── cognitive_loop.py    #   ReAct 调度器 (~400 行)
│   ├── llm_adapter.py       #   LLM 适配器
│   ├── prompt_assembler.py  #   提示词组装 + 记忆检索
│   ├── emotion_engine.py    #   情绪引擎
│   ├── sentence_buffer.py   #   分句缓冲
│   ├── consolidator.py      #   记忆沉淀 (缓冲6条+rerank)
│   ├── idle_monitor.py      #   空闲监控
│   ├── character_config.py  #   角色配置
│   ├── app_config.py        #   App 配置
│   ├── log_setup.py         #   日志系统
│   ├── debug_info.py        #   /debug 面板
│   ├── agent.py        #   组件组装 + 生命周期
│   ├── tools/               #   Agent 工具集
│   ├── memory/              #   记忆子系统
│   │   ├── session.py       #     会话上下文
│   │   └── long_term.py     #     长期记忆
│   └── tests/               #   测试
│       ├── fake_llm.py              #  测试用 LLM backend
│       ├── test_phase1.py           #  Phase 1
│       ├── test_phase2.py           #  Phase 2
│       ├── test_phase3.py           #  Phase 3
│       └── test_sentence_buffer.py  #  SentenceBuffer (70)
│
├── character/
│   └── elysia.yaml          # Elysia 角色定义
│
├── prompt/                  # 多层认知模板
│   ├── Brain.j2             #   L1 主脑 — 双轨思维协议 (已接入)
│   ├── Amygdala.j2          #   L0 潜意识 — 情绪感知 (待接入)
│   ├── ActiveSpeak.j2       #   L1 主动说话决策 (待接入)
│   ├── MicroReflector.j2    #   L2 即时反思 — 记忆提取 (待接入)
│   ├── MacroReflector.j2    #   L2 日终反思 — 日记/情感趋势 (待接入)
│   └── Utils.j2             #   工具宏 (FormatHistory, datetime filter)
│
├── static/
│   ├── index.html           # 浏览器客户端 (541 行)
│   └── debug.html           # Debug 面板 (183 行)
│
├── ref/                     # 参考资源
│   └── Elysia/Elysia.wav    # 唤醒音效
│
└── log/                     # 日志文件 (gitignore)
```

---

## 13. 关键设计决策

| 决策 | 理由 |
|------|------|
| asyncio.Lock 串行化 process() | 防止并发消息导致 FSM 状态混乱 |
| INTERRUPT 的 poll-wait (500×10ms) | 比 magic sleep(0.1) 更可靠，有明确超时 |
| Event.seq = compare=True | 同优先级 FIFO 保序 |
| TTS 句队列 (不 cancel 前句) | 前句音频已开始播放，cancel 会导致断音 |
| 文字/音频双层缓冲 (server + browser) | 网络延迟下保证同步 |
| Fire-and-forget 后处理 | consolidation + save 不阻塞 IDLE |
| MongoDB/Milvus 静默降级 | 无外部服务时仍可运行 (in-memory) |
| Embedding 懒加载 (thread pool) | 不阻塞启动 (模型加载可能耗时) |
| FakeLLMBackend 替代 MockLLM | 最小化 test double，减少维护成本 |
| ConfigLoader 替代手动 yaml.safe_load | 类型安全 + env 覆盖 + 校验 |
| RotatingFileHandler (10MB × 3) | 防止日志撑满磁盘 |
| Brain.j2 fallback | 模板损坏不影响基本功能 |
