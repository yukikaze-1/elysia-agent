# Elysia Agent 架构与设计

## 1. 架构总览

三层 Protocol-Registry-Backend 架构，底层能力与上层认知逻辑解耦。

```
┌─────────────────────────────────────────────────────────────┐
│ L2  Memory & Skills                                          │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────┐           │
│  │ MongoDB  │  │   Milvus     │  │ Tool Registry │           │
│  │ Session  │  │ Long-term    │  │ (OpenAI       │           │
│  │ Context  │  │ Memory       │  │  Tool Schema) │           │
│  └──────────┘  └──────────────┘  └──────────────┘           │
│  ┌──────────────────────┐                                    │
│  │ MemoryConsolidator   │                                    │
│  │ (LLM 提取事实/偏好)   │                                    │
│  └──────────────────────┘                                    │
├─────────────────────────────────────────────────────────────┤
│ L1  Cognitive Core                                           │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐           │
│  │ EventBus │  │ AgentFSM │  │ CognitiveLoop     │           │
│  │(Priority │  │(5 states)│  │ (ReAct scheduler) │           │
│  │ Queue)   │  │          │  │ + stream & tools  │           │
│  └──────────┘  └──────────┘  └──────────────────┘           │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │ CharacterCfg │  │ IdleMonitor  │  │EmotionEngine │       │
│  │ (YAML 角色)  │  │ (主动搭话)    │  │(关键词+衰减) │       │
│  └──────────────┘  └──────────────┘  └──────────────┘       │
│  ┌──────────────┐  ┌──────────────┐                         │
│  │PromptAssembler│  │SentenceBuffer│                         │
│  │(Jinja2 + 记忆)│  │(分句流式)    │                         │
│  └──────────────┘  └──────────────┘                         │
├─────────────────────────────────────────────────────────────┤
│ L0  Infrastructure (Module/)                                 │
│  ┌──────┐ ┌──────┐ ┌──────┐ ┌───────────┐ ┌────────┐       │
│  │ LLM  │ │ STT  │ │ TTS  │ │ Embedding │ │ Config │       │
│  │DeepSeek│SenseV.│ │GPT-So│ │HuggingFace│ │ Loader │       │
│  └──────┘ └──────┘ └──────┘ └───────────┘ └────────┘       │
│  ┌──────────┐ ┌────────┐ ┌────────────────────────┐         │
│  │ Output   │ │ Logger │ │ Database               │         │
│  │ Manager  │ │Factory │ │ VectorDB / DocumentDB  │         │
│  └──────────┘ └────────┘ └────────────────────────┘         │
│  ┌──────────┐ ┌────────┐                                   │
│  │  Input   │ │ Prompt │                                   │
│  │ AudioBuf │ │Manager │                                   │
│  └──────────┘ └────────┘                                   │
└─────────────────────────────────────────────────────────────┘
```

### 完整数据流

```
用户输入 (Browser/REPL/STT)
  → EventBus → FSM(THINKING)
    → PromptAssembler(CharacterConfig + SessionMemory + LongTermMemory)
      → CognitiveLoop.stream_think()
        ├── text delta → OutputManager
        │     ├── ConsoleChannel (stderr)
        │     └── WebSocketChannel → Browser
        │           ├── textQueue → appendDelta (hold until audio play)
        │           └── audioQueue → playNext() (sequential)
        ├── SentenceBuffer → TTS synthesize_stream()
        │     └── audio_chunk → _audio_pending → browser audio buffer
        └── tool_calls → EXECUTING → 回注 messages → 继续 THINKING
  → EmotionEngine.analyze_assistant() → expression 推送
  → MemoryConsolidator.consolidate() → LongTermMemory (fire-and-forget)
  → SessionMemory.save() → MongoDB
  → FSM → IDLE
```

---

## 2. L0 基础设施层：Protocol-Registry-Backend 模式

每个服务模块遵循统一的三层结构：

```
Module/<service>/
├── protocol.py    # ABC 定义接口 (LLMBackend, STTBackend, TTSBackend, ...)
├── registry.py    # Factory + _BACKENDS 字典 ("provider/model" → Backend 类)
├── <backend>.py   # 具体实现 (OpenAICompatBackend, SenseVoiceBackend, ...)
├── config.py      # Dataclass 配置 (ConfigField)
├── __init__.py    # 重导出 ABC, Factory, Config
└── __main__.py    # 集成烟雾测试
```

### 已实现的 Backend

| 模块 | Backend | 说明 |
|------|---------|------|
| `Module.llm` | `deepseek`, `openai`, `ollama` | OpenAI-compatible 协议。`stream_think()` 结构化流式 (content + tool_calls 同时检测) |
| `Module.stt` | `sensevoice` | HTTP API 调用 `localhost:20042` |
| `Module.tts` | `gpt-sovits` | `synthesize_stream()` 异步生成器，逐 chunk 产出 WAV |
| `Module.embedding` | `huggingface`, `openai` | HuggingFace 本地模型离线加载 + GPU 兼容检查 (sm_<70 → CPU fallback) |
| `Module.database.vectordb` | `milvus` | ANN 语义搜索 |
| `Module.database.documentdb` | `mongodb` | motor 异步驱动，`serverSelectionTimeoutMS=3000` |
| `Module.database.graphdb` | stub | 预留 |
| `Module.output` | `ConsoleChannel`, `WebSocketChannel` | 多通道广播 + capability 过滤 |
| `Module.config` | `ConfigLoader` | YAML + env + 验证 |
| `Module.logger` | `LoggerFactory` | 带 caller 检测的日志工厂 |
| `Module.prompt` | `PromptManager` | Jinja2 模板管理 + macro 渲染 + datetime filter |

### Registry 模式

```python
# Module/llm/registry.py
_BACKENDS = {
    "deepseek/deepseek-chat": {
        "class": OpenAICompatBackend,
        "base_url": "https://api.deepseek.com/v1",
        "env_key": "DEEPSEEK_API_KEY",
        "supports_prefix": True,
    },
    ...
}

# 使用
llm = LLMFactory.create("deepseek/deepseek-chat")
# Factory 自动读取 DEEPSEEK_API_KEY env var，注入 caller-aware logger
```

---

## 3. L1 认知控制层

### 3.1 事件总线 (EventBus)

基于 `asyncio.PriorityQueue`，三重优先级：

| 优先级 | 类型 | 触发源 | 行为 |
|--------|------|--------|------|
| 0 (INTERRUPT) | `INTERRUPT` | 用户新消息、VAD 检测 | 无条件 cancel 当前 cognitive task + TTS |
| 1 (STATE_CHANGE) | `STATE_CHANGE` | FSM 状态流转 | 推送前端更新状态指示器 |
| 2 (NORMAL) | `USER_INPUT` | STT、WebSocket 文本 | 进入认知循环 |
| 2 (NORMAL) | `SYSTEM_TICK` | IdleMonitor 定时器 | 主动搭话决策 |

INTERRUPT 处理流程：cancel cognitive task → drain pending USER_INPUT → 重 post STATE_CHANGE (idle) → 回到 IDLE。

### 3.2 有限状态机 (AgentFSM)

```
                    ┌──────────────────────────────┐
                    │         INTERRUPTED           │
                    │  (cancel tasks, clear buffer) │
                    └──────────────────────────────┘
                      ▲                         │
              Priority 0 (any state)            │ (cleanup)
                      │                         ▼
  ┌──────┐   INPUT   ┌──────────┐   LLM    ┌──────────┐  done   ┌──────────┐
  │ IDLE │ ────────► │ THINKING │ ◄──────► │EXECUTING │ ──────► │ SPEAKING │
  │      │ ◄──────── │          │(toolcall)│          │         │          │
  └──────┘  cancel   └──────────┘          └──────────┘         └─────┬────┘
      ▲                                                               │
      └───────────────────────── done ────────────────────────────────┘
```

有效转移：
- IDLE → THINKING
- THINKING → SPEAKING | EXECUTING | INTERRUPTED
- EXECUTING → THINKING | INTERRUPTED
- SPEAKING → IDLE | EXECUTING | THINKING | INTERRUPTED
- INTERRUPTED → IDLE

### 3.3 认知循环 (CognitiveLoop)

ReAct 调度器的核心实现：

```python
async def _think_loop(user_message, user_name):
    messages = await prompt.build_messages(...)
    for round in range(MAX_TOOL_ROUNDS + 1):
        async for chunk in llm.stream_think(messages, tools=...):
            if content:
                # 文字/语音同步：hold delta 到首句 TTS chunk 到达
                if not sync_event.is_set():
                    sync_buffer.append(content)
                else:
                    flush sync_buffer → text deltas
                    drain audio_pending → audio chunks
                # 分句 → TTS
                sentence = sentence_buf.feed(content)
                if sentence and tts:
                    start_tts_stream(sentence)
            if tool_calls:
                merge tool_calls → execute → inject results → continue
        # No tool calls → finalize
        send(message) → consolidate → save session → IDLE
```

关键设计：

- **流式 + 工具检测并行**：`stream_think()` yield `{"content": str|None, "tool_calls": list|None}`，不阻塞文字流来检测工具调用
- **MAX_TOOL_ROUNDS = 3**：防止无限工具调用循环
- **文字/语音同步**（双层缓冲）：
  - Server: `_sync_event` + `_sync_buffer` + `_audio_pending`，首句 TTS chunk 触发释放，5s timeout
  - Browser: `textQueue` hold delta 直到 `Audio.play()` resolve，8s timeout
- **fire-and-forget 后处理**：consolidation + session save 异步执行，不阻塞响应

### 3.4 情绪引擎 (EmotionEngine)

关键词匹配 + 强度衰减：

```python
KEYWORDS = {
    "happy": ["哈哈", "开心", "太好", "喜欢", ...],
    "sad": ["难过", "伤心", "哭", "失败", ...],
    "excited": ["哇", "太棒", "厉害", "惊喜", ...],
    "curious": ["好奇", "为什么", "怎么", "有意思", ...],
}
```

- `analyze_user(text)`: 匹配用户消息关键词 → 加权
- `analyze_assistant(text)`: 匹配助手回复关键词 → 加权
- `decay()`: 每秒衰减 0.05，低于 0.15 重置为 neutral
- mood 注入 `SessionMemory._params["mood"]`，PromptAssembler 读入系统提示词
- expression 变化通过 OutputManager 推送到前端 (avatar ring + mood tag)

### 3.5 分句缓冲 (SentenceBuffer)

正则分句：`SPLIT_PATTERN = re.compile(r"([。！？.!?\n])")`

- 逐 token feed，检测到分隔符提取完整句
- 连续分隔符合并：如果剩余文本以分隔符开头 → hold 到下次
- 短句阈值：剩余 < 3 字符 → hold（避免 "好嘞！" 被拆成 "好嘞" + "！"）
- `flush()` 返回剩余未完成文本

### 3.6 提示词组装 (PromptAssembler)

```
PromptAssembler.build_messages(user_message)
  ├── CharacterConfig.personality_block()           # 角色性格文本
  ├── SessionMemory.recent_messages(10)             # 最近对话
  ├── LongTermMemory.search(user_message, top_k=5)  # 语义召回记忆
  ├── SessionMemory.get_param("mood")               # 当前心情
  └── prompt/Brain.j2 (Jinja2)                       # 系统提示词模板
        │
        ▼
  [{"role": "system", "content": "..."},
   ...recent messages...,
   {"role": "user", "content": user_message}]
```

`prompt/Brain.j2` 是当前使用的系统模板（双轨思维协议）。`prompt/` 目录还有 5 个多层认知模板（Amygdala/ActiveSpeak/Reflectors + Utils）待接入。

### 3.7 DemoAgent — 组件组装

```python
class DemoAgent:
    def __init__(self, character_path, config_path, tts_enabled=None):
        # L0: OutputManager + ConsoleChannel
        # Character: CharacterConfig.from_yaml()
        # L2: SessionMemory + LongTermMemory + ToolRegistry
        # L1: EventBus + AgentFSM + LLMAdapter + PromptAssembler
        #     + TTS (optional, from config or CLI) + EmotionEngine
        #     + MemoryConsolidator + CognitiveLoop + IdleMonitor

    async def start(self):
        # MongoDB + Milvus 并行连接
        # SessionMemory.load() 恢复历史会话
        # embedding 预加载 (background task, thread pool)
        # IdleMonitor.start()
        # 事件处理循环

    async def process_message(self, text):
        # INTERRUPT 当前处理 → post USER_INPUT

    async def run_server(host, port, stt_enabled):
        # aiohttp server + WebSocket + STT
```

---

## 4. L2 记忆与工具层

### 4.1 记忆三层模型

| 层 | 存储 | 生命周期 | 用途 |
|------|------|------|------|
| Working Memory | 内存 (单次调用) | 单次推理 | PromptAssembler 动态组装 |
| Session Memory | MongoDB (持久化) | 跨进程重启 | 最近 N 轮对话 + 运行时参数 |
| Long-term Memory | Milvus (向量) + 内存 | 跨会话共享 | 事实/偏好/计划 语义搜索 |

### 4.2 SessionMemory

```python
class SessionMemory:
    _messages: deque[dict]      # 最多 max_messages 条
    _params: dict               # mood, mood_intensity 等
    _session_id: str            # 默认 "default"
    _db: DocumentDBBackend | None  # MongoDB 或 None (降级)

    async connect_db() -> bool  # 自动检测 MongoDB，不可用时降级
    async save() -> bool        # Upsert by session_id
    async load(sid?) -> bool    # 恢复最近 max_messages 条
    snapshot() / restore()      # 快照备份
```

### 4.3 LongTermMemory

```python
class LongTermMemory:
    _entries: list[MemoryEntry]         # 内存缓存
    _vector_db: VectorDBBackend | None  # Milvus
    _embedding: EmbeddingBackend | None # HuggingFace (懒加载)

    async connect() -> bool     # Milvus 连接
    async insert(text, **meta)  # 写入 Milvus + 内存
    async search(query, top_k)  # 语义搜索 → fallback: 字符重叠

    # 懒加载: _ensure_embedding() 在 thread pool 运行，不阻塞事件循环
    # GPU 兼容: sm_<70 → CPU fallback
    # 离线: MODEL_CACHE_DIR 环境变量或 .model_cache/ 目录
```

### 4.4 MemoryConsolidator

每轮对话后 LLM 提取关键信息，存入 LongTermMemory：

```
对话: "妖梦: 我最近在学 Rust"
  → LLM 提取: [{"type": "fact", "content": "妖梦最近在学习 Rust"}]
  → LongTermMemory.insert("妖梦最近在学习 Rust", type="fact", source="consolidation")
```

`_history` 记录最近 20 条沉淀结果（供 /debug 面板展示）。

### 4.5 ToolRegistry

OpenAI Function Calling Schema 标准化工具注册：

```python
# 注册工具
tools.register(
    name="search_memory",
    description="在长期记忆中搜索",
    parameters={"type": "object", "properties": {...}, "required": [...]},
    func=async_search_memory,
)

# CognitiveLoop 自动注入 tool_definitions 到 LLM 请求
# LLM 返回 tool_calls → CognitiveLoop.execute() → 回注结果
```

已注册工具：`search_memory`, `get_time`, `save_memory`。`_history` 记录最近 20 次调用（函数名 + 参数 + 结果/错误）。

---

## 5. 输入输出系统

### 5.1 OutputManager + Channel 模型

```python
class OutputManager:
    _channels: list[OutputChannel]  # ConsoleChannel, WebSocketChannel, ...

    # 按 ChannelCapability 过滤路由:
    async typing(bool)          # → TYPING capability
    async expression(expr, val) # → EXPRESSION capability
    async audio_chunk(data, id) # → AUDIO capability

    # 直接广播（内部使用）:
    async _broadcast(method, *args, capability=FILTER)
```

ChannelCapability 枚举：`TEXT_DELTA`, `TEXT_MESSAGE`, `TYPING`, `EXPRESSION`, `ACTION`, `COMMAND`, `AUDIO`

单通道失败不影响其他通道（`return_exceptions=True`）。

### 5.2 WebSocket 通道 (`Module/output/websocket.py`)

JSON 消息协议：

| type | payload | 方向 |
|------|---------|------|
| `delta` | `{"content": "..."}` | server → browser |
| `message` | `{"role":"assistant","content":"..."}` | server → browser |
| `typing` | `{"typing": true/false}` | server → browser |
| `state` | `{"state": "idle/thinking/speaking/..."}` | server → browser |
| `expression` | `{"expression":"happy","intensity":0.8}` | server → browser |
| `audio` | `{"data":"base64...","stream_id":...,"seq":...}` | server → browser |
| `audio_end` | `{"stream_id": ...}` | server → browser |
| `transcription` | `{"text": "..."}` | server → browser |
| `text` | `{"type":"text","content":"..."}` | browser → server |

### 5.3 音频输入管道

```
Browser Mic → AudioContext(16kHz) → ScriptProcessorNode → Int16 PCM
  → WebSocket binary → server AudioBuffer.feed()
    → RMS VAD (threshold=0.03, silence=1.5s)
      → voice segment → PCM→WAV → STT.transcribe()
        → EventBus USER_INPUT
      → voice onset → INTERRUPT (echo 消除: deafen 2s)
```

`AudioBuffer.deafen(duration)`: TTS 播放期间忽略麦克风输入，防止回声循环。
INTERRUPT cooldown 0.5s 防抖。

### 5.4 浏览器客户端 (`static/index.html`)

三栏布局：
- **左栏** (220px)：角色面板 — 表情 ring (CSS border-color)、状态灯 (pulse 动画)、心情标签
- **中栏**：对话区 — 消息分组 (同角色连续消息合并)、时间戳分隔线 (5 分钟间隔)、 typing dots 动画
- **右栏**：设置面板 — TTS 开关/音量滑块/清空对话/连接状态，右键齿轮菜单

关键实现：
- **AudioContext 16kHz** → Int16 PCM → WebSocket binary
- **逐 delta 流式**：`appendDelta()` 合并到同一 bubble，`finalizeDelta()` 存档
- **音频顺序播放**：`audioQueue` + `playNext()` 串行，`Audio.play().then()` 触发 `flushTextQueue()`
- **localStorage 持久化**：debounce 500ms 保存 (流式中持续存盘)，刷新不丢失
- **响应式**：窄屏 (< 600px) 折叠侧栏

---

## 6. TTS 流式合成

### 6.1 流式管线

```
LLM token stream
  → SentenceBuffer.feed(token)
    → sentence complete → CognitiveLoop._start_tts_stream(sentence)
      → TTS.synthesize_stream(sentence) [background task]
        → audio_chunk → _audio_pending (sync buffer)
          → drained after text flush → WebSocket audio events
            → browser audioQueue → playNext() → Audio.play()
```

### 6.2 句队列

多句并行合成：`_start_tts_stream` 创建独立 background task，不 cancel 前句（句队列自然排队）。

```python
def _start_tts_stream(self, sentence):
    stream_id = ++self._tts_stream_id
    async def _stream(sid):
        async for chunk in tts.synthesize_stream(sentence, ...):
            if not _audio_flow:   # 首句首 chunk 前 → 缓冲
                _audio_pending.append(('chunk', chunk, sid))
            else:                   # 首句已释放 → 直通
                await output.audio_chunk(chunk, stream_id=sid)
        # audio_end 同样处理
    task = asyncio.create_task(_stream(stream_id))
```

### 6.3 文字/语音同步

双层缓冲设计：

**Server 端** (`cognitive_loop.py`):
- `_sync_event` (asyncio.Event) — 控制 text delta 是否放行
- `_sync_buffer: list[str]` — 暂存 text delta
- `_audio_pending: list[tuple]` — 暂存 audio chunk + end
- `_audio_flow: bool` — 首句释放后直通
- 触发源：TTS 首 chunk → `_sync_event.set()` + `_drain_audio_pending()` (先 flush text, 再 drain audio)
- 超时兜底：5s timeout → 强制释放

**Browser 端** (`index.html`):
- `textQueue: list[str]` — 暂存 delta
- `audioStarted: bool` — 首次 `Audio.play()` resolve 后置 true
- `flushTextQueue()` — `Audio.play().then()` 时调用，批量 appendDelta
- typing 动画保持直到 flush
- 超时兜底：8s timeout

---

## 7. 配置系统

### 7.1 ConfigLoader (`Module/config`)

分层优先级：`环境变量 > YAML > dataclass 默认值`

```python
@dataclass
class MyConfig:
    host: str = ConfigField(default="0.0.0.0")
    port: int = ConfigField(default=8000, constraints=[Range(1, 65535)])
    api_key: str = ConfigField(default="", secret=True, env="MY_API_KEY")

cfg = ConfigLoader(MyConfig, yaml_path="config.yaml", yaml_key="Server").load()
```

`ConfigField` 支持：`secret=True` (env-only), `constraints` (Range/Choices/Regex), `env` (env var 绑定)。

### 7.2 config.yaml

```yaml
agent:
  character: character/elysia.yaml
  user_name: 妖梦

llm:
  model: deepseek/deepseek-chat

memory:
  session_max_messages: 20

server:
  host: 0.0.0.0
  port: 8765

tts:
  enabled: false            # CLI --tts 可覆盖

stt:
  enabled: false            # CLI --stt 可覆盖
```

优先级：`CLI flag > config.yaml > 硬编码默认值`。`demo.py` 通过 `_load_config_defaults()` 预解析 `--config` 加载 YAML 默认值，再传给 argparse。

---

## 8. 工程规范

### 8.1 日志系统 (`core/log_setup.py`)

- 库日志 (pymongo/pymilvus/httpx/openai/torch) → `log/*.log` 文件 (DEBUG)
- 核心日志 (core/Module/asyncio) → stderr (INFO, --verbose 时 DEBUG)
- stdout 仅用于对话输出
- Child loggers (如 `core.cognitive_loop`) 通过 propagate=True 继承父 logger 的 stderr handler

### 8.2 测试架构

```
core/
├── test_phase1.py   # L1 认知核心 (38 tests): EventBus, AgentFSM, CognitiveLoop, EmotionEngine, SentenceBuffer
├── test_phase2.py   # L2 记忆持久化 (48 tests): SessionMemory, LongTermMemory, MemoryConsolidator, ToolRegistry
└── test_phase3.py   # 集成测试 (70 tests): CognitiveLoop + TTS + AudioBuffer + WebSocket + DemoAgent

Module/
├── llm/test_llm.py           # LLM backend (mock + live)
├── stt/test_stt.py           # STT backend
├── tts/test_tts.py           # TTS backend
├── embedding/test_embedding.py
├── output/test_output.py     # OutputManager + ConsoleChannel
├── config/test_config.py
├── logger/test_logger.py
├── prompt/test_prompt.py     # PromptManager + integration with real templates
├── database/vectordb/        # Milvus tests
├── database/documentdb/      # MongoDB tests
├── input/                    # AudioBuffer + server tests
└── bench/                    # Latency benchmarks
```

总计：197 (core) + 243 (Module) = 440 tests。运行：`conda run -n Elysia python -m pytest core/ Module/ -v --asyncio-mode=auto`。

### 8.3 /debug 面板

`GET /debug` → `static/debug.html`，每 2s 轮询 `GET /debug/api` (JSON)。

```
┌──────────────────┬──────────────────────┐
│ System           │ Memory               │
│ FSM / LLM / TTS  │ Session / LT / DB    │
├──────────────────┼──────────────────────┤
│ Recent Session   │ Long-term Memory     │
├──────────────────┼──────────────────────┤
│ Tool History     │ Consolidation        │
└──────────────────┴──────────────────────┘
```

`ToolRegistry._history` + `MemoryConsolidator._history` 各记录最近 20 条。

### 8.4 文件结构

```
Agent/
├── demo.py                  # 入口 (argparse, config-driven)
├── config.yaml              # 总配置
├── Architecture.md          # 本文档
├── CLAUDE.md                # AI 助手指令
├── todo.md                  # 开发计划
├── test_plan.md             # 测试计划
│
├── Module/                  # L0 基础设施层
│   ├── llm/                 # LLM: DeepSeek / OpenAI / Ollama
│   ├── stt/                 # STT: SenseVoice
│   ├── tts/                 # TTS: GPT-SoVits
│   ├── embedding/           # Embedding: HuggingFace / OpenAI
│   ├── output/              # Output: Console + WebSocket 多通道
│   ├── config/              # Config: YAML + env 加载器
│   ├── logger/              # Logger: caller-aware 工厂
│   ├── prompt/              # Prompt: Jinja2 模板管理
│   ├── schema/              # Schema: 共享数据模型
│   ├── database/            # Database: Milvus / MongoDB / GraphDB
│   ├── input/               # Input: AudioBuffer + WebSocket server
│   └── bench/               # Bench: 延迟基准
│
├── core/                    # L1 认知控制 + L2 记忆工具
│   ├── event_bus.py         # 优先级事件总线
│   ├── agent_fsm.py         # 五状态机
│   ├── cognitive_loop.py    # ReAct 调度器 (流式 + 工具 + sync)
│   ├── llm_adapter.py       # LLM 适配器 (真实 + mock)
│   ├── prompt_assembler.py  # 提示词组装 (角色 + 记忆 + 模板)
│   ├── emotion_engine.py    # 情绪引擎 (关键词 + 衰减)
│   ├── sentence_buffer.py   # 分句缓冲 (正则分隔 + hold)
│   ├── tool_registry.py     # 工具注册 (OpenAI Schema)
│   ├── consolidator.py      # 记忆沉淀 (LLM 提取事实)
│   ├── idle_monitor.py      # 空闲监控 (主动搭话)
│   ├── character_config.py  # 角色配置 (YAML)
│   ├── log_setup.py         # 日志系统
│   ├── debug_info.py        # Debug Dashboard 状态聚合
│   ├── demo_agent.py        # 组件组装
│   ├── test_phase1.py       # Phase 1 测试
│   ├── test_phase2.py       # Phase 2 测试
│   ├── test_phase3.py       # Phase 3 测试
│   └── memory/
│       ├── session.py       # 会话上下文 (MongoDB)
│       └── long_term.py     # 长期记忆 (Milvus + Embedding)
│
├── character/
│   └── elysia.yaml          # 角色定义
│
├── prompt/                  # 多层认知模板 (待接入)
│   ├── Brain.j2             #   L1 主脑 — 双轨思维协议
│   ├── Amygdala.j2          #   L0 潜意识 — 情绪感知
│   ├── ActiveSpeak.j2       #   L1 主动说话决策
│   ├── MicroReflector.j2    #   L2 即时反思 — 记忆提取
│   ├── MacroReflector.j2    #   L2 日终反思 — 日记/情感趋势
│   └── Utils.j2             #   工具宏 (FormatHistory, datetime filter)
│
├── static/
│   ├── index.html           # 浏览器客户端
│   └── debug.html           # Debug 面板
│
└── log/                     # 日志 (gitignore)
```
