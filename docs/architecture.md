# Elysia Agent 架构与设计

## 1. 架构总览

```
                        ┌──────────────────────────┐
                        │     Entry Points          │
                        │  app.py (argparse)       │
                        │  ├── repl (stdin)         │
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
  │    ├── SystemPromptManager.render()              # section 提供者           │  │
  │    │     ├── 11 个 PromptSection async render                          │  │
  │    │     ├── set_context() → per-request 参数透传                      │  │
  │    │     └── _RENDER_ORDER → 固定顺序拼接                              │  │
  │    ├── SessionMemory.recent_messages(8)      # 最近对话           │  │
  │    ├── stt_emotion / stt_events              # 声学情绪           │  │
  │    ├── stt_emotion / stt_events              # 声学情绪           │  │
  └────────────────────────────┬─────────────────────────────────────┘  │
  │    └── prompt assembler → MessageBuilder.build_messages()               │  │
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

Module/_common/      # 共享工具层（不属于任何服务模块）
├── env.py           #   dotenv 统一入口
├── registry_helpers.py  # api_key / base_url 解析 + logger 注入
├── test_helpers.py  #   make_test_method() 工厂
├── config_test_helpers.py  # make_config_test_classes() 工厂
├── main_runner.py   #   run_module_main() 统一 __main__ 入口
└── tests/           #   自身单元测试
```

Factory 模式：
```python
# 使用示例
llm = LLMFactory.create("deepseek/deepseek-chat")
# Factory 自动: 读取 DEEPSEEK_API_KEY env var → 解析 base_url → 注入 logger → 返回实例

# Registry 使用 Module/_common/registry_helpers 共享函数:
#   resolve_api_key(env_key, cfg, explicit) → api_key
#   resolve_base_url(env_key, cfg, explicit) → base_url
#   inject_logger(__name__, explicit) → logger

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
| `Module.system_prompt` | `PromptManager`, `SystemPromptManager`, `PromptSection` | Jinja2 | Section Provider 架构 + 通用模板渲染 |

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
- **inner_voice 流式分流**: PRE→THINK→REPLY 三状态机检测 `<inner_voice>` 标签。THINK 内容不进 TTS/前端 delta，闭标签后一次性广播 `command: inner_voice` 到前端渲染淡色思考气泡。详见 `docs/superpowers/specs/2026-06-24-inner-voice-design.md`

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

### 4.7 提示词组装 (PromptAssembler + SystemPromptManager)

```
build_messages(user_message, stt_emotion, stt_events)
  │
  ├── build_system_prompt()
  │     ├── PromptAssembler 创建 11 个 PromptSection 实例（注入数据源）
  │     ├── SystemPromptManager.set_context(user_message, user_name, ...)
  │     ├── SystemPromptManager.render()
  │     │     ├── 按 _RENDER_ORDER 序列 async render 每个 section
  │     │     │     F1 ConflictRules → F2 OutputFormat
  │     │     │     → C5 Sensory → C3 Emotion → C4 Memory
  │     │     │     → C1 Personality → C2 Relationship → C6 SpeakingStyle
  │     │     │     → C7 Proactive → C8 OwnLife → C9 SelfPerception
  │     │     ├── 每个 section 自管数据源 + Jinja2 渲染
  │     │     └── "\n\n" 拼接 → 返回 system prompt
  │     └── MessageBuilder.build_messages(system, history, user_content)
  │
  ├── SessionMemory.recent_messages(8)  # 最近对话
  └── {"role": "user", "content": user_message}
```

**Section 提供者架构** (`Module/system_prompt/` protocol + sections + templates): 2026-07-04 重构，将 `Brain.j2` 单体拆分为 12 个独立的 `PromptSection` 模块（ConflictRules, OutputFormat, TTSMarkup, Sensory, Emotion, Memory, Personality, Relationship, SpeakingStyle, Proactive, OwnLife, SelfPerception）。每个 section 自管数据源、自管 `.j2` 模板、可独立 reload/validate。`SystemPromptManager` 按固定顺序 async render 并拼接。详见 `docs/superpowers/specs/2026-07-04-prompt-section-provider-design.md`。

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
| Session | MongoDB / deque | 跨进程重启 | config.yaml 可配 (默认 200) | 对话上下文 + 运行时参数 |
| Long-term | Milvus + 内存 | 跨会话 | 无上限 | 事实/偏好/计划 语义搜索 |

### 5.2 SessionMemory

```python
class SessionMemory:
    _messages: deque[dict]        # 最多 max_messages 条 (config.yaml session_max_messages)
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

### 5.5 ToolManager

Pydantic ToolPlugin 架构：

```python
# 每个工具是 ToolPlugin 子类，Pydantic BaseModel 参数校验
class WebSearchPlugin(ToolPlugin):
    name = "web_search"
    description = "搜索互联网信息"
    param_model = WebSearchParams  # Pydantic BaseModel
    capabilities = {Capability.NETWORK}
    safety = SafetyProfile(network_allowed=True, block_private_ips=True, ...)

# ToolManager 统一管理: 加载、执行、HITL 审批、热重载
tool_manager.load_all(ALL_PLUGINS, plugin_ctx)

# CognitiveLoop 自动:
# tool_definitions() → LLM 请求（通过 API tools 参数传递，不再注入 system prompt）
# LLM 返回 tool_calls → SafetyPipeline (validate→safety→hitl→execute→truncate) → 结果回注 messages
```

详见 `Module/tool/README.md`。

已注册工具（12 个）：
| 工具 | 参数 | 功能 |
|------|------|------|
| `search_memory` | query, limit | 语义搜索长期记忆 |
| `save_memory` | content, type, poignancy | 保存新记忆 |
| `get_time` | — | 获取当前日期时间 |
| `web_search` | query, engine | DuckDuckGo / Brave 搜索 |
| `web_fetch` | url | 抓取网页内容 |
| `read_file` | path, start_line, max_lines | 读取本地文件（沙箱内） |
| `list_directory` | path | 列出目录内容 |
| `write_file` | path, content | 写入文件（需审批） |
| `set_reminder` | text, delay_seconds | 设置定时提醒 |
| `get_runtime_config` | query | 查询运行时配置和状态 |
| `get_self_state` | — | 查询 Agent 自身状态 |
| `describe_image` | image_indices | 视觉模型描述图片 |

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
| `command` | `{"command":"inner_voice","params":{"content":"..."}}` | AI 内心独白（淡色气泡） |
| `command` | `{"command":"tool_approval","params":{...}}` | 工具审批请求 |
| `command` | `{"command":"block","params":{...}}` | 结构化内容块 |

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
  character: character/elysia/config.yaml  # 角色文件
  user_name: 妖梦                    # 用户名

llm:
  model: deepseek/deepseek-v4-flash  # LLM 模型标识

memory:
  session_max_messages: 200          # 会话窗口大小

server:
  host: 0.0.0.0
  port: 8765

tts:
  enabled: true                      # CLI --tts 可覆盖

stt:
  enabled: true                      # CLI --stt 可覆盖
```

优先级: `CLI flag > config.yaml > AppConfig 默认值`

### 7.2 角色配置 (character/elysia/config.yaml)

每个角色一个子目录：`character/<name>/config.yaml` + `character/<name>/personality.md`。

```yaml
name: Elysia
user_pronoun: 她
base_personality_file: personality.md   # L1 Core 文本
voice: elysia_v2                        # [占位] TTS voice ID

emotion:
  thresholds: {excitement: 0.6, sadness: 0.3}
  idle_expressions: [neutral, blink]

wake_words: [Elysia, 爱莉, 爱莉希雅, 妖精小姐]

proactive:
  enabled: false
  interval: 300
  triggers:
    - type: silence_timeout
      duration: 30
    - type: time_of_day
      times: ["08:00", "12:00", "22:00"]
    - type: time_of_day
      times: ["08:00", "12:00", "22:00"]
```

---

## 8. Debug Dashboard

`GET /debug` → `static/debug.html`（多标签页仪表板）

**标签页：**
| 标签 | 数据来源 | 功能 |
|------|---------|------|
| System | `/debug/api` 轮询 | FSM 状态、Session/LTM、工具执行、Token 消耗、延迟统计、内存占用、Scheduler |
| 数据库 | `/debug/api/ltm` + `/debug/api/sessions` | 长期记忆 CRUD、Session 管理 |
| 任务 | `/debug/api/tasks/stream` SSE | TaskSystem DAG 可视化 (vis-network) |
| Config | Provider REST API | LLM/STT/TTS 端点管理、连通性测试 |

---

## 9. 组件组装 (Agent)

```python
class Agent:
    def __init__(self, *, config: AppConfig):
        # L0: OutputManager + ConsoleChannel
        # Character: CharacterConfig.from_yaml(config.agent.character)
        # L2: SessionMemory + LongTermMemory + ToolManager
        #     + register 3 tools
        # L1: EventBus + AgentFSM + LLMAdapter + PromptAssembler
        #     + EmotionEngine + MemoryConsolidator
        #     + CognitiveLoop + IdleMonitor
        # TTS: 根据 config.tts.enabled 决定是否初始化

    async def start():
        # MongoDB + Milvus 并行连接
        # SessionMemory.load() — 恢复历史会话
        # TokenTracker + PromptRecorder 初始化
        # ToolManager.load_all(ALL_PLUGINS) — 加载工具插件
        # TaskScheduler Worker Pool 启动
        # IdleMonitor.start()
        # TTS/STT 条件启动
        # TaskSystem 启动 + Mailbox 轮询
        # 事件处理循环 (_process_events)

    async def stop():
        # cancel cognitive + TTS tasks
        # cancel embedding preload + mailbox polling
        # idle_monitor.stop()
        # scheduler.stop()
        # tool_manager.shutdown()
        # task_system.stop()
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
~1,366 tests total (305 TaskSystem unit + 36 TaskSystem integration + ~367 core + ~598 Module + ~82 tools)

core/tests/
├── test_phase1.py             # L1 认知核心 (32 tests)
├── test_phase2.py             # L2 记忆持久化 (72 tests)
├── test_phase3.py             # 集成 + 组件测试 (114 tests)
├── test_sentence_buffer.py    # SentenceBuffer 专项 (65 tests)
└── fake_llm.py                # 测试用 LLM backend

Module/task_system/tests/
├── unit/                       # 单元测试 (305 tests, ~37s)
└── integration/                # 集成测试 (36 tests, 含 7 e2e)

Module/
├── llm/test_llm.py
├── stt/test_stt.py
├── tts/test_tts.py
├── embedding/test_embedding.py
├── output/test_output.py
├── config/test_config.py
├── logger/test_logger.py
├── prompt/test_prompt.py
├── tool/test_framework.py      # ToolManager + ToolPlugin + SafetyPipeline (96 tests)
├── tool/test_tool_safety.py    # 路径验证 + URL检查 + 参数校验 (33 tests)
├── _common/tests/              # 共享工具函数单元测试
├── database/vectordb/
├── database/documentdb/
├── input/test_audio_buffer.py
└── input/test_server.py
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
├── docs/architecture.md     # 本文档
├── docs/frontend-api.md     # 前端接入 API 文档
├── CLAUDE.md                # AI 助手指令
├── README.md                # 项目说明
│
├── Module/                  # L0 基础设施层
│   ├── _common/              #   共享工具 (registry_helpers, test_helpers, main_runner, ...)
│   ├── llm/                 # LLM: DeepSeek / OpenAI / Ollama
│   ├── stt/                 # STT: SenseVoice / OpenAI-compatible
│   ├── tts/                 # TTS: GPT-SoVits / OpenAI-compatible
│   ├── embedding/           # Embedding: HuggingFace / OpenAI
│   ├── output/              # Output: Console + WebSocket 多通道
│   ├── config/              # Config: YAML + env 加载器 + 验证
│   ├── logger/              # Logger: caller-aware 工厂
│   ├── prompt/              # Prompt: System Prompt 引擎 + Section Provider
│   ├── schema/              # Schema: 共享数据模型
│   ├── database/            # Database: Milvus / MongoDB / GraphDB
│   ├── tool/                # Tool: ToolPlugin框架 + SafetyPipeline + ToolManager
│   ├── input/               # Input: AudioBuffer (VAD) + WebSocket server
│   ├── task_system/         # TaskSystem: 异步任务编排 (Planner/Orchestrator/Executor)
│   └── bench/               # Bench: 延迟基准
│
├── core/                    # L1 认知控制
│   ├── schema.py            #   共享类型 (AgentState, EventType, EventPriority)
│   ├── event_bus.py         #   优先级事件总线
│   ├── agent_fsm.py         #   五状态机
│   ├── cognitive_loop.py    #   ReAct 调度器 + submit_task 拦截
│   ├── block_filter.py      #   流式块过滤 + 结构化块解析
│   ├── tts_stream.py        #   TTS 流式播放管理 (TTSStreamManager)
│   ├── tool_call_parser.py  #   流式 tool call 合并/归一化
│   ├── text_utils.py        #   文本提取 + MessageObj
│   ├── llm_adapter.py       #   LLM 适配器
│   ├── prompt_assembler.py  #   提示词组装 + 性格引擎数据注入
│   ├── emotion_engine.py    #   情绪引擎
│   ├── sentence_buffer.py   #   分句缓冲
│   ├── consolidator.py      #   记忆沉淀 (缓冲6条+rerank)
│   ├── idle_monitor.py      #   空闲监控
│   ├── character_config.py  #   角色配置
│   ├── app_config.py        #   App 配置
│   ├── log_setup.py         #   日志系统
│   ├── token_tracker.py     #   Token 消耗统计 + 持久化
│   ├── prompt_recorder.py   #   LLM Prompt 记录器 (环形缓冲)
│   ├── response_recorder.py #   LLM Response 记录器 (环形缓冲)
│   ├── task.py              #   调度任务数据模型
│   ├── task_scheduler.py    #   Worker Pool 调度器
│   ├── message_id.py        #   消息 ID 生成
│   ├── debug_info.py        #   /debug 数据聚合
│   ├── agent.py             #   组件组装 + 生命周期 + TaskSystem 集成
│   ├── tools/               #   Agent 工具集 (12 个插件)
│   ├── memory/              #   记忆子系统
│   │   ├── session.py       #     会话上下文 (MongoDB + deque)
│   │   └── long_term.py     #     长期记忆 (Milvus + Embedding)
│   └── tests/               #   测试
│
├── character/
│   └── elysia.yaml          # Elysia 角色定义
│
├── prompt/                  # 反射模板 (CharacterReflector 活跃，其余为参考)
│   └── CharacterReflector.j2  #   角色进化反射
│
├── static/                  # Web 前端
│   ├── index.html           # 聊天页面
│   ├── debug.html           # Debug 仪表板
│   ├── css/chat.css         # 聊天样式
│   └── js/                  # ES modules (chat/audio/mic/ws/state/persistence/settings/main)
│
└── log/                     # 日志文件 (gitignore)
```

---

## 13. 并发任务全景

所有并发使用裸 `asyncio.create_task`，分布在以下调用点：

```
Agent 启动
├── [后台] embedding 预加载      fire-and-forget, stop 时 cancel
├── [常驻] _process_events()    阻塞在 EventBus, 单消费者
├── [常驻] IdleMonitor._loop()  每秒 tick
│
每个 USER_INPUT:
├── [前台] CognitiveLoop.process()   asyncio.Lock 串行, _current_task 追踪
│   ├── 每句 → [后台] TTS _stream()  _tts_tasks[] 追踪
│   └── 完成后 → [后台] _after_response  fire-and-forget, 无追踪
│       ├── MemoryConsolidator.consolidate()
│       └── session.save()
│
INTERRUPT:
├── cancel _current_task → cancel _tts_tasks[] → drain queue → → IDLE
```

**13 个 `asyncio.create_task` 调用点一览：**

| # | 位置 | 用途 | 管理模式 |
|---|------|------|----------|
| 1 | agent.py | embedding 预加载 | fire-and-forget + tracked |
| 2 | agent.py | `_process_events` 主循环 | 常驻，stop 时 cancel |
| 3 | agent.py | cognitive process (USER_INPUT) | `_current_task`，INTERRUPT cancel |
| 4 | agent.py | cognitive process (SYSTEM_TICK) | 同上 |
| 5 | cognitive_loop.py | `_after_response` | fire-and-forget，无追踪 |
| 6 | tts_stream.py | TTS 流式合成 | `TTSStreamManager._tasks[]`，gather/cancel |
| 7 | idle_monitor.py | idle 监控 tick | 常驻，stop 时 cancel |
| 8 | long_term.py | embedding 加载 | insert 时 await (wait=True), search 时 fire-and-forget |
| 9 | emotion_engine.py | expression 输出 | 纯 fire-and-forget，无追踪 |
| 10 | tool/manager.py | SafetyPipeline 执行 | Pipeline 内部追踪 |
| 11 | agent.py | `_poll_mailboxes()` | TaskSystem mailbox 500ms 轮询，常驻，stop 时 cancel |
| 12 | agent.py | cognitive process (TASK_RESULT) | `_current_task` |
| 13 | agent.py | `_db_health_loop()` | 数据库健康检查 120s 间隔，常驻，stop 时 cancel |

---

## 14. 关键设计决策

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
| section 提供者架构 | 模板损坏不影响基本功能 |

---

## 15. 已知架构缺口

```
当前模型: 用户说一句 → AI 回一句 (串行)

已解决:
  1. ✅ 消息排队 — pending_messages + 容量上限，busy 时暂存
  2. ✅ 后台任务 — TaskSystem v1: Planner/Executor/Evaluator DAG 编排 + Mailbox 轮询
  3. ✅ 流式中断 — CancelledError 防御 + FSM 幂等

待解决:
  4. 主动对话 — Elysia 只能在用户说话后回复（伴侣化改造 Step 4）
  5. 记忆人格化 — 记忆融入对话，不是机械引用（伴侣化改造 Step 3）
  6. 多 Session — 当前 session_id 硬编码为 "default"，无多会话管理
  7. 角色切换 — CharacterConfig 重载 + TTS voice 热切换
```
