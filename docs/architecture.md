# Elysia Agent 架构与设计

> 文档导航：[项目文档](../README.md)

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
    │   config.yaml      │  │  Agent  │  │  character/*/ │
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
    │  │(Priority │  │  (6 states) │  │  (ReAct + stream  │    │
    │  │ Queue)   │  │             │  │   + tools + sync) │    │
    │  └──────────┘  └─────────────┘  └──────────────────┘    │
    │                                                          │
    │  ┌────────────┐ ┌───────────┐ ┌───────────┐ ┌─────────┐ │
    │  │CharacterCfg│ │IdleMonitor│ │Character  │ │Sentence │ │
    │  │(name+pron.│ │(主动搭话)  │ │(合并情绪) │ │Buffer   │ │
    │  │ +L1 Core) │ └───────────┘ │          │ │(分句)   │ │
    │  └────────────┘               └───────────┘ └─────────┘ │
    │  ┌──────────────┐ ┌──────────┐ ┌────────────┐ ┌───────┐ │
    │  │PromptAssembler│ │LLMAdapter│ │Char.Engine│ │Debug  │ │
    │  │(12-section+  │ │(protocol │ │(L1-L4模型) │ │Info   │ │
    │  │ Person.Engine)│ │适配)     │ └────────────┘ │(聚合) │ │
    │  └──────────────┘ └──────────┘                 └───────┘ │
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
  │    │     ├── 12 个 PromptSection async render                          │  │
  │    │     ├── set_context() → per-request 参数透传                      │  │
  │    │     └── _RENDER_ORDER → 固定顺序拼接                              │  │
  │    ├── SessionMemory.recent_messages(8)      # 最近对话           │  │
  │    ├── stt_emotion / stt_events              # 声学情绪           │  │
  └────────────────────────────┬─────────────────────────────────────┘  │
                               │                                        │
                               ▼                                        │
  ┌──────────────────────────────────────────────────────────────────┐  │
  │  CognitiveLoop._think_loop()                                       │  │
  │                                                                   │  │
  │  for round in 0..MAX_TOOL_ROUNDS(5):                              │
  │    LLM.stream_think(messages, tools=tool_defs)                    │
  │      │                                                            │
  │      ├── content delta ──► SentenceBuffer.feed(token)             │
  │      │    │    └── sentence complete                               │
  │      │    │          ├── [TTS on]  stream_sentence(sentence)    │
  │      │    │          │     └── bind-send delta + audio            │
  │      │    │          └── [TTS off] direct delta + send("")        │
  │      │    │                                                       │
  │      │    └── OutputManager ──┬── ConsoleChannel (stderr)         │
  │      │                        └── WebSocketChannel → Browser      │
  │      │                              ├── text delta (sentence)      │
  │      │                              └── audio chunk (stream)       │
  │      │                                                       │    │
  │      └── tool_calls ──► asyncio.gather ──► inject ──► loop───────┘
  │  CharacterEngine.analyze_assistant() ──► expression 推送           │  │
  │  MemoryConsolidator.consolidate() ──► 提取 → 比对 → LongTermMemory（fire-forget，Pydantic typed）│  │
  │  CharacterReflector.buffer() ──► CharacterEngine (fire-forget)    │  │
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
    └── 消息由 WebSocket session 推送 (无 localStorage 缓存)                      │
```

### 2.1 文字/语音同步（绑定发送）

服务端 TTS 后台 task 等首块音频到达后，绑在一起发送文字+音频：

**Server 端** (CognitiveLoop):
```
SentenceBuffer 分句 → stream_sentence(sentence) [background task]
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
└── tests/           # 单元测试 (mock)

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
| `Module.llm` | `deepseek`, `openai`, `ollama`, `mimo`, `gemini*`, `custom` | OpenAI-compatible | `stream_think()` 结构化流式 (content + tool_calls 并行检测) |

> \* gemini 已注册但抛出 NotImplementedError
| `Module.stt` | `sensevoice`, `custom` | HTTP API | SenseVoice: localhost:20042，返回文本 + 情绪标签 + 音频事件；Custom: OpenAI-compatible。`openai`/`deepgram` 已注册但抛出 NotImplementedError |
| `Module.tts` | `gpt-sovits`, `minimax`, `custom` | HTTP API | `synthesize_stream()` 异步生成器，逐 chunk WAV；MiniMax: speech-2.8-hd，hex 编码音频 |
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
# core/config/app_config.py
@dataclass
class AppConfig:
    agent: AgentConfig = ConfigField(default_factory=AgentConfig)
    llm: LLMConfig = ConfigField(default_factory=LLMConfig)
    memory: MemoryConfig = ConfigField(default_factory=MemoryConfig)
    server: ServerConfig = ConfigField(default_factory=ServerConfig)
    tts: TTSConfig = ConfigField(default_factory=TTSConfig)
    stt: STTConfig = ConfigField(default_factory=STTConfig)
    wake_word: WakeWordConfig = ConfigField(default_factory=WakeWordConfig)
    live2d: Live2DConfig = ConfigField(default_factory=Live2DConfig)
    vision: VisionConfig = ConfigField(default_factory=VisionConfig)

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
    data: EventData | None = field(compare=False, default=None)
    seq: int = field(compare=True, default=0)  # FIFO within same priority
```

`EventData` 是 5 种模型的判别联合（`InterruptData` / `StateChangeData` / `UserInputData` / `SystemTickData` / `TaskResultData`），通过 `discriminator="event_type"` 区分。`post()` 自动从 `data.event_type` 推导 `type` 字段。

INTERRUPT 处理流程：
```
1. cancel self._cognitive._current_task (CancelledError → handle_interrupt)
2. drain() 清空队列
3. 保留 STATE_CHANGE 事件 (用于 UI 更新)
4. FSM: 当前态 → INTERRUPTED → IDLE
```

### 4.2 AgentStatus 接口层

`AgentStatus` 是 FSM 之上的语义接口，将消费者与底层 `AgentFSM` 解耦。设计详见 [spec](../docs/specs/2026-07-30-agent-status-interface-design.md)。

**对外只读：`AgentStatus`（`agent.status`）**
- `is_accepting_input` — IDLE 或 IDLE_BACKGROUND_TASK
- `is_completely_idle` — 仅 IDLE
- `is_speaking` — SPEAKING
- `should_suppress_vad_interrupt` — THINKING 或 EXECUTING
- `current_name()` / `previous_name()` — 用于日志和调试
- `on_change(callback)` — 事件驱动订阅，替代轮询
- `wait_until_idle(timeout)` — 异步等待空闲

**对内写入：`CognitivePhaseController`（`agent.phases`）**
- `enter_thinking()` / `enter_executing()` / `return_to_thinking()` / `enter_speaking()` — 阶段入口
- `finish_turn(has_background_tasks)` — 结束 turn，转换到 IDLE 或 IDLE_BACKGROUND_TASK
- `handle_interrupt()` — 中断状态转换（INTERRUPTED → IDLE）

底层由 `AgentFSM`（`agent/agent_fsm.py`）实现状态转换验证，通过 `FSMBackedStatus` + `FSMBackedPhaseController`（`agent/status.py`）桥接。

有效转移（与旧版 FSM 一致）：
```
IDLE        → THINKING
IDLE_BACKGROUND_TASK → THINKING | INTERRUPTED
THINKING    → SPEAKING | EXECUTING | INTERRUPTED
EXECUTING   → THINKING | INTERRUPTED
SPEAKING    → IDLE | EXECUTING | THINKING | INTERRUPTED | IDLE_BACKGROUND_TASK
INTERRUPTED → IDLE
```

### 4.3 认知循环 (CognitiveLoop)

ReAct 调度器，采用 mixin 模式 `CognitiveLoop(CognitiveTools)`。通过 `agent.phases`（`CognitivePhaseController`）驱动状态转换，`_think_loop()` 从 378 行提取为 4 个独立方法 + ~30 行主循环，每步设置 `self._phase`（TurnPhase）供 debug 面板观测内部进度：

```python
class CognitiveLoop:
    MAX_TOOL_ROUNDS = 5  # 防止无限工具调用

    async def process(self, user_message, ...):
        async with self._process_lock:          # 串行化并发调用
            if not self.status.is_accepting_input:  # 已在处理中 → skip
                return
            await self.phases.enter_thinking()
            try:
                await self._think_loop(user_message, ...)
            except CancelledError:
                await self._handle_interrupt()    # 清理 → INTERRUPTED → IDLE

    async def _think_loop(self, user_message, ...):
        self._phase = TurnPhase.BUILD_PROMPT
        messages, tool_defs = await self._build_turn_prompt(...)

        for round_num in range(MAX_TOOL_ROUNDS + 1):
            self._phase = TurnPhase.STREAMING
            acc_text, tool_calls, ... = await self._stream_llm_response(...)

            if tool_calls and round_num < MAX:
                self._phase = TurnPhase.EXECUTING_TOOLS
                await self._execute_tool_round(...)  # submit_task 拦截 + 调度
                continue

            self._phase = TurnPhase.FINALIZING
            await self._finalize_turn(...)  # 标签剥离 + 归档 + 结构化块推送
            return
```

**TurnPhase 内部追踪：** 4 个阶段标记（`BUILD_PROMPT` / `STREAMING` / `EXECUTING_TOOLS` / `FINALIZING`）不同于外部状态，TurnPhase 是纯追踪标记，不设状态转换验证，通过 `/debug/api` 的 `turn_phase` 字段暴露。

关键设计决策：
- **asyncio.Lock**: 确保 `process()` 不可重入，防止并发消息导致状态混乱
- **Fire-and-forget 后处理**: consolidation + session save 异步执行，用户可立即发下一条消息
- **TTS 绑定发送**: 文字和音频在服务端绑在一起发送，前端无需同步逻辑
- **Action Gate**: `_gate_tool_calls()` 在 LLM 产出 tool_calls 后、dispatch 前校验工具名是否在注册表。任一未知工具 → 整轮 abort 注入 `GATE_REJECTED` failure messages，LLM 自行修正
- **并行工具调用**: `asyncio.gather` + `return_exceptions=True`，单工具失败不影响其他
- **VAD 状态保护**: THINKING/EXECUTING 时 VAD 不打断，STT 文本和显式按钮始终生效
- **inner_voice 流式分流**: StreamProcessor 纯状态机检测 `<inner_voice>` 标签，通过 `_dispatch_stream_event()` 事件→副作用翻译。详见 `docs/superpowers/specs/archive/2026-06-24-inner-voice-design.md`

### 4.4 TTS 绑定发送 + 句队列

```
LLM token stream
  → SentenceBuffer.feed(token)
    → sentence complete → CognitiveLoop.stream_sentence(sentence)
      → TTS.synthesize_stream(sentence) [background task]
        → 首 chunk 到达 → 绑定发送: delta(完整句) + send("") + audio_chunks
          → WebSocket → browser appendDelta + audioQueue → playNext()

句队列: 多句并行合成
  stream_id = ++tts_stream_id
  create_task(_stream(stream_id))   # 不 cancel 前句，自然排队
  workers=1 FIFO 保证句间顺序，前端 nextBubbleQueue 等音频播完再弹出
```

### 4.5 情绪引擎 (已合并至 CharacterEngine)

原 `EmotionEngine` 已合并至 `CharacterEngine`（`core/personality/engine.py`），作为 L4 Mood 层。

**主路径** (语音): STT 声学情绪标签通过 C5 Sensory 直接传给 LLM
```
STT 返回: emotion="HAPPY", events=["Laughter"]
  → C5 Sensory: "语调: HAPPY 😊 | 事件: Laughter 😂" → LLM 自主判断如何回应
  → 音频事件 → expression 推送: Laughter→"laugh", Cry→"cry", Applause→"surprised"
```
不再通过硬编码表预设 AI mood，mood 决策交给 LLM 结合角色性格动态判断。

**文本路径**: 关键词匹配
```
MOOD_KEYWORDS = {
    "happy":    ["哈哈", "开心", "太好了", "喜欢", ...],
    "excited":  ["!", "！", "天哪", "wow", ...],
    ...
}
→ 计分 → 最高分 mood → update CharacterState.mood + push expression
```
文本关键词路径作为 fallback（LLM 未输出 `<mood>` 时降级使用）。

**衰减**: `analyze_assistant()` 每次回复后 intensity -= 0.15，低于 0.2 推送闲置表情。mood 文本不再重置——LLM 下轮通过 `<mood>` 自行描述心情变化。

**Expression 映射**: `mood` 文本（str）直接作为 expression 名透传 → OutputManager.expression()

详见 [4.10 CharacterEngine](#410-characterengine--角色性格情绪引擎)。

### 4.6 分句缓冲 (SentenceBuffer)

```
默认分隔符: 。！？!?\n～…〜.

逐 token feed → 检测分隔符 → 提取完整句
  英文句号检测: 过滤数字(3.14)、缩写(Dr./Mr./U.S.)、URL
  连续分隔符合并: ？, ！, ～ 等连续标点合并为一句 ("啊？！" 不拆)
  短句阈值: min_chunk_length=5, 短于阈值的句子暂存, 后续有文字时释放
  flush() → 返回剩余未完成文本

配置项: delimiters, min_chunk_length, max_buffer_length (默认 2000),
        abbreviations (可扩展英文缩写集)
72 个单元测试覆盖边界
```

### 4.7 提示词组装 (PromptAssembler + SystemPromptManager)

```
build_messages(user_message, stt_emotion, stt_events)
  │
  ├── build_system_prompt()
  │     ├── PromptAssembler 创建 12 个 PromptSection 实例（注入数据源）
  │     ├── SystemPromptManager.set_context(user_message, user_name, ...)
  │     ├── SystemPromptManager.render()
  │     │     ├── 按 _RENDER_ORDER 序列 async render 每个 section
	  │     │     │     F1 ConflictRules → F2 OutputFormat → F3 TTSMarkup
	  │     │     │     → C2 Relationship → C7 Proactive → C6 SpeakingStyle
	  │     │     │     → C1 Personality → C9 SelfPerception → C8 OwnLife
	  │     │     │     → C3 Emotion → C4 Memory → C5 Sensory
  │     │     ├── 每个 section 自管数据源 + Jinja2 渲染
  │     │     └── "\n\n" 拼接 → 返回 system prompt
  │     └── MessageBuilder.build_messages(system, history, user_content)
  │
  ├── SessionMemory.recent_messages(8)  # 最近对话
  └── {"role": "user", "content": user_message}
```

**Section 提供者架构** (`Module/system_prompt/` protocol + sections + templates): 2026-07-04 重构，将 `Brain.j2` 单体拆分为 12 个独立的 `PromptSection` 模块（ConflictRules, OutputFormat, TTSMarkup, Sensory, Emotion, Memory, Personality, Relationship, SpeakingStyle, Proactive, OwnLife, SelfPerception）。每个 section 自管数据源、自管 `.j2` 模板、可独立 reload/validate。`SystemPromptManager` 按固定顺序 async render 并拼接。详见 `docs/superpowers/specs/archive/2026-07-04-prompt-section-provider-design.md`。

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

薄适配层 (~170 行)：接收 `model` 标识符 (如 `"deepseek/deepseek-chat"`)，通过 `LLMFactory.create()` 创建 Module 层 backend，提供 `think()` 和 `stream_think()` 方法。

测试用 `FakeLLMBackend` (core/tests/fake_llm.py) — 最小化 test double，直接 yield 预设字符串。

---

### 4.10 CharacterEngine — 角色性格 + 情绪引擎

合并原 PersonalityEngine + EmotionEngine，四层性格模型，让角色从对话经历中演化性格：

```
L1 Core（核心底色）    文件加载，启动后不变                  → C1 模板锚点
L2 Self（长期自我）    从 L3 压缩沉淀，月级演化              → C1 主体——"我是谁"
L3 State（短期状态）   CharacterReflector + 事件触发，数十轮  → C9 条件素材——"我最近怎么了"
L4 Mood（当前情绪）    LLM `<mood>` 标签每轮更新（自由文本），keyword fallback → C3 内容
```

**CharacterState**（Pydantic 模型）：集中管理 mood、personality (L2 Self + L3 State)、reflector buffer/context、awareness 冷却、state_history 等所有性格+情绪状态。替代原 `session._params` 散落存储。

**CharacterReflector**（子组件）：缓冲 50 对 user/assistant 消息 → LLM 反思 → 提交事件到 `CharacterEngine.consume_trigger("reflector", ...)`，更新 L3 State。

**AwarenessTrigger**（C9 激活判定）：L3 非空 + 距上次触发超过 1h → C9 渲染。冷却时间戳持久化到 CharacterState，重启不丢失。

**阻尼机制**：L3 偏移 L1 特质时，L1 决定"怎么说"（表达方式），L3 影响"说什么"（内容倾向）。Phase 2 引入 L4→L3、L3→L2 向上回流压缩。

**版本历史**：L2/C6 演化产物持久化到 `character_versions` MongoDB 集合（原 `character_l2_versions`），通过 `content_type` 字段区分 `"l2"` / `"c6"`，per type 独立计数。支持在线浏览 + 回滚；L2 回滚自动联动刷新 C6。

完整四层模型、模板渲染、数据流、调试入口 → [core/personality/README.md](core/personality/README.md)

---

### 5.1 记忆三层模型

| 层 | 存储 | 生命周期 | 容量 | 用途 |
|------|------|------|------|------|
| Working | 内存 (单次推理) | 单次 LLM 调用 | 动态 | PromptAssembler 组装 |
| Session | MongoDB / deque | 跨进程重启 | 代码默认 20，config.yaml 覆盖为 50 | 对话上下文 + CharacterState/reflector_state |
| Long-term | Milvus + 内存 | 跨会话 | 无上限 | 事实/偏好/计划 语义搜索 |

### 5.2 SessionMemory

```python
class SessionMemory:
    _messages: deque[Message]     # 最多 max_messages 条 (config.yaml session_max_messages). Pydantic Message 对象
    _character_state: CharacterState | None  # 性格+情绪+reflector 状态（替代原 _params）
    _reflector_state: MemoryReflectorState | None  # MR 计数器
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
    _repo: MemoryRepository | None       # Milvus (optional)
    _embedding_provider: EmbeddingProvider  # 嵌入懒加载 (lazy, thread-pool)

    async connect()          # Milvus 连接 (fast)
    async insert(text, ...)  # 写 Milvus + 内存 → 触发 _evict_overflow() 数量驱逐
    async search(query, k)   # Milvus ANN → fallback: 字符重叠 → rerank (0.5×sim + 0.3×poi + 0.2×rec)

    # 驱逐: 超过 max_entries 时保留最新 N 条，同步清理 Milvus
    # mark_absorbed(): async，含 Milvus 清理，加 _write_lock
    # EmbeddingProvider: 优先 OpenAI API (fast)，fallback HuggingFace，超时 10s → 降级 text-only
```

### 5.4 MemoryConsolidator

攒 6 条消息后调 LLM 提取，fire-and-forget 执行。提取 Prompt 要求事件合并、重要性评分 1-10、第一人称叙事。

```
缓冲 6 条消息 → EXTRACTION_PROMPT (事件合并 + poignancy)
  → LLM 提取 → ExtractedMemory Pydantic 模型 (MemoryType: Fact/Preference/Event/Plan/Note, content, poignancy) → ConsolidationCompare → CompareAction 判别联合 (insert/update/merge/skip)
  → LongTermMemory.insert(poignancy=7, memory_type="Fact")

_history: 最近 20 条 (供 /debug 面板)
_buffer: 未达阈值的待处理对话
```

### 5.5 ToolAdapter

Pydantic ToolPlugin 架构：

```python
# 每个工具是 ToolPlugin 子类，Pydantic BaseModel 参数校验
class WebSearchPlugin(ToolPlugin):
    name = "web_search"
    description = "搜索互联网信息"
    param_model = WebSearchParams  # Pydantic BaseModel
    capabilities = {Capability.NETWORK}
    safety = SafetyProfile(network_allowed=True, block_private_ips=True, ...)

# ToolAdapter 统一管理: 加载、执行、HITL 审批、热重载
tool_adapter.load_all(ALL_PLUGINS, plugin_ctx)

# CognitiveLoop 自动:
# tool_definitions() → LLM 请求（通过 API tools 参数传递，不再注入 system prompt）
# LLM 返回 tool_calls → Action Gate (工具存在性校验) → SafetyPipeline (validate→safety→hitl→execute→truncate) → 结果回注 messages
# GATE_REJECTED: 调度前被 gate 拦截，与 UNKNOWN_TOOL（执行层发现）分工明确
```

详见 `Module/tool/README.md`。

已注册工具（12 个）：
| 工具 | 参数 | 功能 |
|------|------|------|
| `search_memory` | query, limit | 语义搜索长期记忆 |
| `save_memory` | content, type | 将重要信息保存到长期记忆库（type: fact/preference/event/plan/note，输入大小写不敏感，归一化为 TitleCase） |
| `get_time` | timezone, format | 获取当前日期时间，支持时区和格式 |
| `web_search` | query, limit | Tavily / Brave 搜索（默认 Tavily） |
| `web_fetch` | url | 抓取网页内容 |
| `read_file` | path, start_line, max_lines | 读取本地文件（沙箱内） |
| `list_directory` | path, depth | 列出目录内容（depth 默认 1，最大 3） |
| `write_file` | path, content | 写入文件（需审批） |
| `set_reminder` | message, delay_seconds | 设置定时提醒（1-86400 秒） |
| `get_runtime_config` | query | 查询运行时配置和状态 |
| `get_self_state` | — | 查询 Agent 自身状态 |
| `describe_image` | prompt, image_index, image_indices | 视觉模型描述图片（单图/批量互斥） |

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
| `session` | `{"type":"session","messages":[...],"token_stats":{...}}` | 连接时推送会话历史 |
| `capabilities` | `{"type":"capabilities","stt":true,...}` | 服务能力声明 |
| `wake` | `{"type":"wake"}` | 唤醒词检测通知 |
| `switch_model_result` | `{"type":"switch_model_result","service":"...","ok":true}` | 模型切换结果 |
| `error` | `{"type":"error","message":"..."}` | 错误通知 |

**Browser → Server:**

| type | payload | 说明 |
|------|---------|------|
| `text` | `{"type":"text","content":"你好"}` | 文本输入（content 可为 string 或 content array） |
| `interrupt` | `{"type":"interrupt"}` | 打断按钮 |
| `wake_toggle` | `{"type":"wake_toggle","enabled":true}` | 唤醒词开关 |
| `switch_model` | `{"type":"switch_model","service":"llm","model":"..."}` | 切换模型 |
| `tool_approval` | `{"type":"tool_approval","request_id":"...","approved":true}` | 工具审批回复 |
| binary | Int16 PCM bytes | 麦克风音频 |

### 6.3 WebSocket 服务器 (core/input/)

```
aiohttp Application (41 条路由):

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/` | static/index.html |
| GET | `/debug` | static/debug.html |
| GET | `/ws` | WebSocket handler |
| GET | `/health`, `/ready` | 健康检查 |
| GET\|POST | `/shutdown` | 关闭服务 |
| POST | `/restart` | 重启服务 |
| GET | `/static/` | 静态文件（JS/CSS/HTML） |
| GET | `/live2d/` | Live2D 角色模型资源 |
| GET | `/uploads/` | 用户上传文件 |
| POST | `/api/chat` | JSON chat API |
| GET | `/api/messages` | 消息归档分页查询 |
| POST | `/api/images` | 图片上传 |
| GET | `/api/providers` | 模型 provider 列表 |
| POST | `/api/providers` | 添加自定义 provider |
| DELETE | `/api/providers/{service}/{name}` | 删除自定义 provider |
| POST | `/api/providers/test` | 连通性测试 |
| GET | `/tools` | 工具插件列表 |
| POST | `/tools/reload` | 热重载工具插件 |
| GET | `/debug/api` | DebugInfo 完整 JSON |
| GET | `/debug/api/prompts` | Prompt 记录列表 |
| GET | `/debug/api/prompts/{n}` | Prompt 详情 |
| GET | `/debug/api/responses` | Response 记录列表 |
| GET | `/debug/api/responses/{n}` | Response 详情 |
| GET | `/debug/api/db-status` | 数据库连接状态 |
| GET\|POST | `/debug/api/ltm` | LTM 列表 / 新增记忆 |
| PUT\|DELETE | `/debug/api/ltm/{entry_id}` | 更新 / 删除单条记忆 |
| DELETE | `/debug/api/ltm` | 清空全部 LTM |
| GET\|DELETE | `/debug/api/sessions` | Session 列表 / 清空全部 |
| GET\|DELETE | `/debug/api/sessions/{session_id}` | Session 详情 / 删除 |
| GET\|DELETE | `/debug/api/archive` | Archive 分页 / 清空全部 |
| PUT\|DELETE | `/debug/api/archive/{msg_id}` | 更新 / 删除单条归档 |
| GET | `/debug/api/tasks` | TaskSystem 快照 |
| GET | `/debug/api/tasks/stream` | TaskSystem SSE 事件流 |

每个 WS 连接:
  └── 创建 WebSocketChannel → OutputManager
  └── 创建 AudioBuffer (if STT enabled)
  └── 启动 state-monitor task:
        FSM → "speaking" → audio_buf.deafen(2.0)  # 防回声
  └── 消息循环: TEXT → process_message, BINARY → audio_buf.feed()
  └── disconnect: cleanup channel + audio_buf + monitor
```

### 6.4 浏览器客户端 (frontend/)

Vue 3 + Vite + Pinia + TypeScript。开发时通过 Vite 代理（:5173 → :8765），生产构建产物 `frontend/dist/` 复制到 `static/` 由 aiohttp serve。

三栏布局：
- **左栏**: 角色面板 — 状态灯 (CSS transition)、心情标签、LLM 模型名
- **中栏**: 对话区 — 消息气泡（TransitionGroup 动画）、时间戳分隔线 (5min)、typing dots 动画
- **浮层**: 设置面板 + 工具管理面板 + HITL 审批弹窗

关键实现：
- 21 个 Vue 3 组件，全部使用 `<script setup lang="ts">` + `<style scoped>`
- 5 个 Pinia store（connectionStore, chatStore, audioStore, settingsStore, toolsStore）
- WebSocket 消息路由通过 `useWebSocket` composable 分发到 store action（替代旧版 switch）
- AudioContext 16kHz → Int16 PCM → WS binary
- 流式 delta 追加（Vue 响应式 + `chatStore.appendDelta()`）
- TTS 气泡排队（`nextBubbleQueue` + `visibleMessages` computed）
- highlight.js 代码高亮 + marked Markdown 渲染
- 多入口构建（chat 页面 + debug 仪表盘）

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
  session_max_messages: 50           # 会话窗口大小

server:
  host: 0.0.0.0
  port: 8765

tts:
  enabled: true                      # CLI --tts 可覆盖

stt:
  enabled: true                      # CLI --stt 可覆盖
```

优先级: `CLI flag > 环境变量 (.env) > config.yaml > AppConfig 默认值`

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

appearance:
  height: "162cm"
  hair: "粉色长发"
  eyes: "粉红色"

wake_words:
  - Elysia
  - 爱莉
  - 爱莉希雅
  - 妖精小姐

proactive:
  enabled: false
  interval: 300
  triggers:
    - type: silence_timeout
      duration: 30
    - type: time_of_day
      times: ["08:00", "12:00", "22:00"]

---

## 8. Debug Dashboard

`GET /debug` → `static/debug.html`（多标签页仪表板）

**标签页：**
| 标签 | 数据来源 | 功能 |
|------|---------|------|
| 概览 | `/debug/api` 轮询 | 12+ 卡片面板：System、Memory（含 Session Params 可展开）、Latency、Process Memory、Scheduler、Token、Prompt 查看器、Raw Response、LTM、Tools、Tool History、Consolidation、Character Reflector、CharacterEngine |
| 数据库 | `/debug/api/db-status` + `/debug/api/ltm` + `/debug/api/sessions` + `/debug/api/archive` | LTM CRUD + 分页 + 搜索；Session 管理（从 archive 权威源读取，限 20 条）；Archive CRUD |
| 任务 | `/debug/api/tasks/stream` SSE | TaskSystem 实时监控 + DAG 可视化 (vis-network) |

---

## 9. 组件组装 (Agent)

```python
class Agent:
    def __init__(self, *, config: AppConfig):
        # L0: OutputManager + ConsoleChannel
        # Character: CharacterConfig.from_yaml(config.agent.character)
        # L2: SessionMemory + LongTermMemory + ToolAdapter
        #     + register 12 tools (ALL_PLUGINS)
        # L1: EventBus + AgentFSM + LLMAdapter + PromptAssembler
        #     + CharacterEngine + MemoryConsolidator
        #     + CognitiveLoop + IdleMonitor
        # TTS: 根据 config.tts.enabled 决定是否初始化

    async def start():
        # AgentFSM.reset() + LLMAdapter.reconnect()
        # Archive.connect()
        # 状态恢复: CharacterReflector.restore_state → CharacterEngine.restore_state → MemoryReflector.restore_state
        # JSONL snapshot 恢复
        # ToolContext 构造 + 注入
        # _db_health_loop 创建
        # MongoDB + Milvus 并行连接
        # SessionMemory.load() — 恢复历史会话
        # TokenTracker + PromptRecorder 初始化
        # ToolAdapter.load_all(ALL_PLUGINS) — 加载工具插件
        # TaskScheduler Worker Pool 启动
        # IdleMonitor.start()
        # TTS 条件启动（STT 在 run_server() 中初始化）
        # TaskSystem 启动 + Mailbox 轮询
        # 事件处理循环 (_process_events)

    async def stop():
        # 1. Buffer flush: consolidator → CharacterReflector → L3→L2 compression → MemoryReflector
        # 2. _stop_event_task() — cancel event processing
        # 3. cancel cognitive + TTS tasks
        # 4. _stop_mailbox() — cancel mailbox polling
        # 5. _stop_task_system() — stop TaskSystem
        # 6. scheduler.stop()
        # 7. tool_manager.shutdown()
        # 8. idle_monitor.stop()
        # 9. _stop_embedding_if_running() — cancel embedding preload
        # 10. _stop_persistence() — session.save() → EmbeddingFactory.shutdown() → long_term.close()
        # 11. _stop_db_health() — cancel db_health_task
        # 12. llm.close() + tts.close() + output.close()
        # (每步 3s timeout，少数关键步骤 5s，单步失败不阻塞后续)

    async def process_message(text):
        # idle_monitor.touch()
        # if FSM != IDLE: 暂存至 pending_messages（容量上限保护）
        # post USER_INPUT (or dequeue from pending_messages after IDLE)

    async def run_server():
        # start() → create_app() → aiohttp server
        # stdin watcher: /quit → _running = False
        # cleanup: stdin.close() → stop() → runner.cleanup()
```

---

## 10. 日志系统

```
core/utils/log_setup.py:

  库日志 (pymongo/pymilvus/httpx/openai/torch)
    → 分流到 log/mongo.log, log/milvus.log, log/http.log, log/torch.log
       (RotatingFileHandler, WARNING+, propagate=False — 不污染主日志)

  核心日志 (core/Module/asyncio, logger.setLevel(DEBUG))
    → stderr          (INFO / --verbose → DEBUG)
    → app.log         (INFO+, 10MB×3)     — 干净操作日志
    → app-debug.log   (DEBUG+, 20MB×5)    — 全量调试日志

  DB 日志 (core.storage.*, core.memory.session, core.memory.long_term)
    → 额外分流到 mongo.log / milvus.log  (DEBUG+, propagate=True — 主日志保留副本)

  stdout → 仅对话输出 (不污染)
```

详见 `log/README.md`。

---

## 11. 测试架构

~1,800 个测试按 4 层 markers 组织（unit / service / integration / slow）。

完整分层体系、测试文件索引、按模块运行命令 → [docs/testing.md](testing.md)

## 12. 文件结构

```
Agent/
├── Makefile                # 测试/构建命令
├── requirements.txt        # Python 依赖
├── docker-compose.yml      # 数据库容器
├── pytest.ini              # pytest 配置（markers）
├── data/                   # 应用数据（上传、缓存、Live2D）
├── app.py                  # 入口 (argparse + ConfigLoader + CLI overrides)
├── config.yaml              # 总配置
├── CLAUDE.md                # AI 助手指令
├── README.md                # 项目说明
│
├── Module/                  # L0 基础设施层
│   ├── _common/              #   共享工具 (registry_helpers, test_helpers, main_runner, ...)
│   ├── llm/                 # LLM: DeepSeek / OpenAI / Ollama / Mimo / Custom
│   ├── stt/                 # STT: SenseVoice / Custom (OpenAI-compatible)
│   ├── tts/                 # TTS: GPT-SoVits / MiniMax / Custom (OpenAI-compatible)
│   ├── embedding/           # Embedding: HuggingFace / OpenAI
│   ├── output/              # Output: Console + WebSocket 多通道
│   ├── config/              # Config: YAML + env 加载器 + preflight
│   ├── logger/              # Logger: caller-aware 工厂
│   ├── system_prompt/       # System Prompt 引擎 + Section Provider + 12 templates
│   ├── database/            # Database: Milvus / MongoDB / GraphDB (stub)
│   ├── tool/                # Tool: ToolPlugin框架 + SafetyPipeline + ToolManager
│   ├── task_system/         # TaskSystem: 异步任务编排 (Planner/Orchestrator/Executor)
│   └── bench/               # Bench: 延迟基准
│
├── core/                    # L1 认知控制 + L2 记忆
│   ├── agent/                #   Agent 组装包 (agent + lifecycle + server + _types + 4 lifecycle mixins)
│   │   ├── agent.py          #     Agent.__init__ + 事件处理 + process_message
│   │   ├── lifecycle.py      #     AgentLifecycle: start/stop 编排 + _STARTUP_STEPS / _SHUTDOWN_STEPS
│   │   ├── server.py         #     AgentServer: run_server/repl + provider 切换
│   │   ├── _types.py         #     RunState + 辅助类
│   │   ├── _lifecycle_persistence.py  #  持久化层启停
│   │   ├── _lifecycle_services.py     #  LLM/TTS/Output/Token 启停
│   │   ├── _lifecycle_tools.py        #  工具/调度/空闲监控 启停
│   │   └── _lifecycle_task_system.py  #  TaskSystem/DelegationPreparer/mailbox 启停
│   ├── adapter/
│   │   ├── llm_adapter.py
│   │   ├── tts_adapter.py
│   │   ├── tool_adapter.py
│   │   ├── tool_call_parser.py
│   │   ├── message_builder.py
│   │   └── prompt_assembler.py
├── cognitive/            #   CognitiveLoop 包 (loop + stream_processor + tools + timing)
│   │   ├── loop.py           #     CognitiveLoop(CognitiveTools): process + _think_loop + _dispatch_stream_event
│   │   ├── stream_processor.py  #  StreamProcessor: mood/inner_voice 标签检测 + BlockFilter + SentenceBuffer
│   │   ├── tools.py          #     CognitiveTools: 工具执行 + submit_task
│   │   └── timing.py         #     Timer + TimingClock 延迟统计
│   ├── monitoring/            #   监控子包
│   │   ├── event_bus.py       #     优先级事件总线
│   │   ├── idle_monitor.py    #     空闲监控 + 主动触发
│   │   └── token_tracker.py   #     Token 消耗统计
│   ├── text/                #   文本流处理包
│   │   ├── block_filter.py  #     流式块过滤 + 结构化块解析
│   │   ├── sentence_buffer.py #   分句缓冲
│   │   └── text_utils.py    #     文本提取 + MessageObj
│   ├── task/                 #   任务子系统包
│   │   ├── __init__.py       #     重导出 Task + TaskScheduler
│   │   ├── task.py           #     任务数据模型
│   │   └── scheduler.py      #     Worker Pool 调度器
│   ├── utils/                #   通用工具包
│   ├── schema/               #   共享类型包 (agent + types + part + tool + message + adapters)
│   ├── debug/                #   Debug 仪表板包
│   │   ├── info.py           #     /debug 数据聚合
│   │   ├── prompt_recorder.py #   Prompt 记录器
│   │   └── response_recorder.py # Response 记录器
│   ├── input/                #   Input: WebSocket + HTTP 服务器
│   │   ├── server.py         #     aiohttp 应用入口
│   │   ├── ws_handler.py     #     WebSocket 消息路由
│   │   ├── api_routes.py     #     REST API 路由
│   │   ├── debug_routes/     #     Debug API 路由 (package: 7 modules)
│   │   ├── audio_buffer.py   #     音频缓冲 + VAD
│   │   ├── wakeword.py       #     唤醒词检测
│   │   └── custom_providers.py #   自定义 provider 路由
│   ├── config/               #   配置子包
│   │   ├── __init__.py       #     重导出 AppConfig + CharacterConfig
│   │   ├── app_config.py     #     App 配置
│   │   └── character_config.py #   角色配置
│   ├── personality/         #   CharacterEngine 子系统
│   │   ├── engine.py        #     CharacterEngine (合并 PersonalityEngine + EmotionEngine)
│   │   ├── reflector.py     #     CharacterReflector
│   │   ├── awareness.py     #     AwarenessTrigger (C9 觉察判定)
│   │   ├── compression.py   #     CompressionManager (L3→L2 + C6 联动)
│   │   ├── emotion.py       #     情绪分析 (LLM mood tag + keyword fallback)
│   │   └── _utils.py        #     内部工具
│   ├── memory/              #   记忆子系统
│   │   ├── session.py       #     SessionMemory (MongoDB + deque)
│   │   ├── long_term.py     #     LongTermMemory (Milvus + Embedding)
│   │   ├── embedding_provider.py #  EmbeddingProvider: 嵌入懒加载 + fallback
│   │   ├── consolidator.py  #     MemoryConsolidator (提取 + 比对 + 写入)
│   │   └── memory_reflector.py #  MemoryReflector (定期去重 + KMeans)
│   ├── prompts/              # Jinja2 模板 (.j2, PascalCase)
│   │   ├── ExtractMemory.j2          # 对话提取记忆
│   │   ├── ConsolidationCompare.j2   # 新记忆 vs 已有记忆比对
│   │   ├── MemoryReflector.j2        # 定期深度去重
│   │   ├── CharacterReflector.j2     # 性格反思
│   │   ├── ActiveSpeak.j2            # 主动搭话
│   │   ├── Amygdala.j2               # 情感分析
│   │   ├── AwarenessJudge.j2         # C9 觉察判定
│   │   ├── SpeakingStyleEdit.j2      # C6 风格编辑
│   │   ├── MacroReflector.j2         # 日记合成 (参考)
│   │   ├── MicroReflector.j2         # 逐对话提取 (参考)
│   │   ├── Utils.j2                  # 工具宏
│   │   ├── PersonalityCompression.j2     # L3→L2 性格压缩
│   │   └── ref/                      # GPT 生成原始版本
│   ├── tools/               #   Agent 工具集 (12 个插件)
│   └── tests/               #   测试
│
├── character/
│   ├── elysia/              # Elysia 角色
│   │   ├── config.yaml       #   角色配置
│   │   ├── personality.md    #   L1 Core 人格文本
│   │   └── reference/        #   GPT 生成参考
│   └── cyrene/              # Cyrene 角色 (备用，仅含 GPT 参考材料)
│       └── reference/
│
├── frontend/                  # Vue 3 前端源码 (Vite + Pinia + TypeScript)
│   ├── index.html              # 聊天页面入口
│   ├── debug.html              # Debug 仪表板入口
│   ├── vite.config.ts          # 代理 :8765 + 多入口构建
│   ├── src/
│   │   ├── main.ts             # App 入口
│   │   ├── App.vue             # 根组件
│   │   ├── styles/variables.css
│   │   ├── types/protocol.ts   # 20+ WS 消息类型定义
│   │   ├── stores/             # 6 个 Pinia store
│   │   ├── composables/        # 4 个 composables
│   │   ├── components/         # 21 个 Vue 组件
│   │   └── debug/              # 独立 Debug 应用
│
├── static/                     # 静态资源（仅图片/文档）
│   ├── Elysia.png              # 角色头像
│   └── live2d-demo/            # Live2D Cubism SDK + demo

├── docs/                   # 文档
│   ├── architecture.md     #   架构详解（本文档）
│   ├── frontend-api.md     #   前端 API
│   ├── testing.md          #   测试指南
│   ├── readme-conventions.md #  README 编写规范
│   ├── companion-roadmap.md  #   伴侣化路线
│   ├── companion-prompt-guide.md  # Prompt 指南
│   ├── database-storage.md #   数据库存储设计
│   ├── completed.md        #   已完成记录
│   ├── experience.md       #   开发经验
│   ├── sync-lessons.md     #   同步经验
│   ├── plans/              #   实施计划
│   ├── specs/              #   设计规格
│   └── superpowers/        #   Superpowers 计划/规格
├── log/                     # 日志文件 (gitignore)
└── scripts/                 # 启动/预检/工具/清理脚本
    ├── setup.sh            #   一键安装
    ├── start.sh            #   一键启动
    ├── check.sh            #   环境预检
    ├── download_model.py   #   嵌入模型下载
    ├── clean_db.py         #   数据库清理（MongoDB + Milvus + 本地文件）
    └── README.md           #   脚本说明
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

**19 个 `asyncio.create_task` 调用点一览（非穷举，仅列出核心调用点）：**

| # | 位置 | 用途 | 管理模式 |
|---|------|------|----------|
| 1 | agent/lifecycle.py | embedding 预加载 | fire-and-forget + tracked |
| 2 | agent/agent.py | `_process_events` 主循环 | 常驻，stop 时 cancel |
| 3 | agent/agent.py | cognitive process (USER_INPUT) | `_current_task`，INTERRUPT cancel |
| 4 | agent/agent.py | cognitive process (SYSTEM_TICK) | 同上 |
| 5 | cognitive/loop.py | `_after_response` | fire-and-forget，无追踪 |
| 7 | idle_monitor.py | idle 监控 tick | 常驻，stop 时 cancel |
| 8 | long_term.py | embedding 加载 | insert 时 await (wait=True), search 时 fire-and-forget |
| 9 | personality/engine.py | expression 输出 | 纯 fire-and-forget，无追踪 |
| 10 | tool/manager.py | SafetyPipeline 执行 | Pipeline 内部追踪 |
| 11 | agent/agent.py | `_poll_mailboxes()` | TaskSystem mailbox 500ms 轮询，常驻，stop 时 cancel |
| 12 | agent/agent.py | cognitive process (TASK_RESULT) | `_current_task` |
| 13 | agent/lifecycle.py | `_db_health_loop()` | 数据库健康检查 120s 间隔，常驻，stop 时 cancel |
| 14 | memory/session.py | archive_append | fire-and-forget，无追踪 |
| 15 | input/server.py | per-connection state monitor | 常驻，disconnect 时 cancel |
| 16 | task_system/task_system.py | sub-agent task submit | TaskSystem 内部追踪 |
| 17 | task_system/task_system.py | sub-agent task resume | TaskSystem 内部追踪 |
| 18 | personality/reflector.py | CharacterReflector fire-and-forget | fire-and-forget，无追踪 |
| 19 | task/scheduler.py | Worker Pool + prune loop | 常驻，stop 时 cancel |

---

## 14. 关键设计决策

| 决策 | 理由 |
|------|------|
| asyncio.Lock 串行化 process() | 防止并发消息导致 FSM 状态混乱 |
| 消息队列模式（busy 时暂存至 pending_messages） | 避免忙等消耗 CPU，容量上限防止内存无限增长，新消息不会丢失 |
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
  4. 主动对话 — C7 Proactive section 已有规则 + 冷却机制。IdleMonitor 驱动待完善
  5. 记忆人格化 — C4 Memory section 已有可靠性规则（当前说法优先、不推测当事实），CharacterEngine Phase 2 深化
  6. 多 Session — 当前 session_id 硬编码为 "default"，无多会话管理
  7. 角色切换 — CharacterConfig 重载 + TTS voice 热切换
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../README.md) | 快速开始 + 完整文档导航 |
| [docs/testing.md](testing.md) | 测试指南（分层体系 + 命令） |
| [docs/readme-conventions.md](readme-conventions.md) | README 编写规范 |
| [CLAUDE.md](../CLAUDE.md) | AI 助手指令 + 项目地图 |
