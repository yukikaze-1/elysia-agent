# Elysia Agent 架构与设计

> 文档导航：[项目文档](../README.md)

## 1. 架构总览

```
                        ┌──────────────────────────┐
                        │     Entry Points          │
                        │  app.py (argparse)       │
                        │  ├── --cli (interactive)  │
                        │  ├── --check / --full     │
                        │  └── server (default)     │
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
    │  │(name      │ │(主动搭话)  │ │(合并情绪) │ │Buffer   │ │
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
    │  ┌──────────┐ ┌────────┐                              │
    │  │ Schema   │ │ Prompt │                              │
    │  │(models   │ │Manager │                              │
    │  │ +msg)    │ │(Jinja2)│                              │
    │  └──────────┘ └────────┘                              │
    └────────────────────────────────────────────────────────┘
```

三层职责分明：
- **L0 Module/**: 无状态原子能力 — 每个服务遵循 Protocol-Registry-Backend 模式，可独立替换
- **L1 core/**: 认知中枢 — 事件总线、状态机、ReAct 调度器、情绪引擎、提示词组装
- **L2 core/memory/**: 记忆与工具 — 会话持久化、语义召回、事实提取
- **接口层 core/input/**: WebSocket + HTTP 输入输出（server/ws/audio/voice/REST），不属于 L0

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
  │      ├── content delta ──► StreamProcessor.feed(delta)            │
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
StreamProcessor 分句（splitter） → stream_sentence(sentence) [background task]
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
#   （阶段4 P0-01 D1=B：内置 local provider（sensevoice）base_url 解析为空时由 STTFactory
#    兜底回填 `DEFAULT_BASE_URL` 单源常量——一处修复覆盖启动/切换/听写快照/Profile 测试 4 调用点；
#    custom 仍必须显式 base_url）
#   inject_logger(__name__, explicit) → logger

# Registry 注册：provider 默认值已外置，不再内联字典
# Module/config/providers.yaml 是默认值单源，loader 在 Module/config/providers.py
_BACKENDS: dict[str, dict] = load_provider_defaults("llm")
# → 形如 {"deepseek": {"base_url": "", "url_key": "DEEPSEEK_URL",
#                      "env_key": "DEEPSEEK_API_KEY", "supports_prefix": True,
#                      "supports_multimodal": False, "models": [...]}, ...}
# 用户可经 .env / config.yaml 覆盖，无需改代码；"provider/model" 标识在 create() 时拆分
```

### 3.2 已实现 Backend

| 模块 | Backend | 协议 | 说明 |
|------|---------|------|------|
| `Module.llm` | `deepseek`, `openai`, `ollama`, `mimo`, `gemini*`, `custom` | OpenAI-compatible | `stream_think()` 结构化流式 (content + tool_calls 并行检测) |

> \* gemini 已注册但抛出 NotImplementedError
| `Module.stt` | `sensevoice`, `custom` | HTTP API | SenseVoice: localhost:20042，返回文本 + 情绪标签 + 音频事件；Custom: OpenAI-compatible。`openai`/`deepgram` 已注册但抛出 NotImplementedError |
| `Module.tts` | `gpt-sovits`, `minimax`, `custom` | HTTP API | `synthesize_stream()` 异步生成器，逐 chunk WAV；MiniMax: speech-2.8-hd，hex 编码音频 |
| `Module.embedding` | `huggingface`, `openai` | 本地模型 / API | HF 离线加载 + GPU 兼容 (sm_<70 → CPU fallback) |
| `Module.database.vectordb` | `milvus`, `pinecone*`, `qdrant*` | gRPC / REST | ANN 语义搜索。`pinecone`/`qdrant` 已注册但抛出 NotImplementedError |
| `Module.database.documentdb` | `mongodb` | motor async | `serverSelectionTimeoutMS=3000` |
| `Module.database.graphdb` | stub | — | 预留 |
| `Module.output` | `ConsoleChannel`, `WebSocketChannel` | — | 多通道广播 + capability 路由 |
| `Module.config` | `ConfigLoader` | YAML + env | 分层配置 + 类型校验 |
| `Module.system_prompt` | `PromptManager`, `SystemPromptManager`, `PromptSection` | Jinja2 | Section Provider 架构 + 通用模板渲染。注：`SystemPromptManager` 需通过 `Module.system_prompt.system_prompt_manager` 子模块导入（`__init__.py` 不导出） |

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
    database: DatabaseConfig = ConfigField(default_factory=DatabaseConfig)

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

### 3.5 项目路径管理 (paths.py)

顶层模块 `paths.py`（项目根目录），集中管理所有项目级文件系统路径。`Module/` 和 `core/` 均可 import。

```python
from paths import ProjectPath, get_project_root

log_dir = ProjectPath.LOG.resolve()                        # → <root>/log
tmpl = ProjectPath.SYSTEM_PROMPT_TEMPLATES.resolve()       # → <root>/Module/system_prompt/templates
sandbox = ProjectPath.DATA_SANDBOX.resolve(session_id="a") # → <root>/data/sandbox/a
```

- **项目根检测**: `ELYSIA_ROOT` 环境变量 > `paths.py` 所在目录
- **环境变量覆盖**: `ELYSIA_PATH_{NAME}` 可逐路径覆盖（如 `ELYSIA_PATH_LOG=/var/log/elysia`）
- **25 个枚举成员**: 15 目录 + 10 文件（含 `MEMORY_PENDING_SYNCS` / `SESSION_PENDING_CLEANUP` 两个 journal），枚举类 `ProjectPath(str, Enum)`。批次 4 新增 `DATA_UPLOADS_DESKTOP`、批次 5 新增 `DATA_SECRETS`，批次 7 起 `DATA_UPLOADS_IMAGES` 预置，2026-08-14 起 `LIVE2D_ASSETS` 指向 `data/live2d-assets`
- **语义**: `resolve()` 纯计算返回 `pathlib.Path`，不检查存在性

---

## 4. L1 认知控制层

### 4.1 事件总线 (EventBus)

基于 `asyncio.PriorityQueue`，三重优先级 + FIFO 保序。`EventPriority` / `EventType` 定义在 `core/schema/agent.py`（`core/status/fsm.py` 也从此处消费），`Event` / `EventBus` 在 `core/monitoring/event_bus.py`：

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

`AgentStatus` 是 FSM 之上的语义接口，将消费者与底层 `AgentFSM` 解耦。设计详见 [spec](superpowers/specs/archive/2026-07/2026-07-30-agent-status-interface-design.md)。

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

底层由 `AgentFSM`（`core/status/agent_fsm.py`）实现状态转换验证，通过 `FSMBackedStatus` + `FSMBackedPhaseController`（`core/status/fsm.py`）桥接。接口定义在 `core/status/protocol.py`。

有效转移（与旧版 FSM 一致）：
```
IDLE        → THINKING
IDLE_BACKGROUND_TASK → THINKING | INTERRUPTED
THINKING    → SPEAKING | EXECUTING | INTERRUPTED
EXECUTING   → THINKING | INTERRUPTED
SPEAKING    → IDLE | EXECUTING | THINKING | INTERRUPTED | IDLE_BACKGROUND_TASK
INTERRUPTED → IDLE
```

### 4.2.1 Agent 公开接口（组合根访问路径）

`Agent` 是组合根（composition root），持有 30+ 子系统/仓库。除 `agent.status` / `agent.phases` 外，通过 **43 个同名 `@property`** 公开访问路径——`@property` 只是薄 getter（`return self._xxx`，少数为懒初始化），可写性由子系统自身 API 自管。设计详见 [spec](superpowers/specs/archive/2026-08/2026-08-01-agent-public-readonly-interface-design.md)。

**会话/记忆/存储（12 个）**：`agent.session` / `agent.session_manager` / `agent.active_session_id` / `agent.pending_messages` / `agent.long_term` / `agent.message_repo` / `agent.session_repo` / `agent.memory_repo` / `agent.token_repo` / `agent.attachment_repo` / `agent.idempotency_repo` / `agent.user_identity_store`

**适配器/能力（13 个）**：`agent.tool_adapter` / `agent.output` / `agent.llm` / `agent.tts` / `agent.tts_manager` / `agent.stt` / `agent.stt_enabled` / `agent.tts_enabled` / `agent.stt_emotion_state` / `agent.config` / `agent.bus` / `agent.character_profiles` / `agent.character_state_stream`

**认知/后台（9 个）**：`agent.cognitive` / `agent.generation_registry` / `agent.character_runtime_registry` / `agent.desktop_bridge` / `agent.idle` / `agent.scheduler` / `agent.consolidator` / `agent.memory_reflector` / `agent.character_engine`

**角色/观测（5 个）**：`agent.character_reflector` / `agent.character` / `agent.token_tracker` / `agent.prompt_recorder` / `agent.response_recorder`

**标量（4 个）**：`agent.user_name` / `agent.state` / `agent.started_at` / `agent.stt_ok`

**状态写方法（2 个）**：`agent.request_shutdown()` / `agent.request_restart()` — 设 `_state` 信号（STOPPING / RESTARTING）引导主循环退出，不做清理（`agent.stop()` 才做）。

**公开方法**：`agent.purge_message_from_session(msg_id)` — 从当前 session 移除匹配消息，archive 删除时同步清理。

**约定**：跨模块消费禁止 `agent._xxx`，一律走公开接口；debug 层深度内省（`agent.<sub>._<attr>`）用 `# noqa: SLF001` 显式豁免。

### 4.3 认知循环 (CognitiveLoop)

ReAct 调度器，采用 mixin 模式 `CognitiveLoop(CognitiveStreaming, CognitiveToolPhase, CognitiveFinalize)`——流式分发、工具轮、终态收尾三块分别落在 `core/cognitive/{streaming,tool_phase,finalize}.py`（CORE-MOD-P1-04）。`process()` 的轮前准备/异常收尾也提取为 `_prepare_turn()` / `_handle_turn_exception()`。通过 `agent.phases`（`CognitivePhaseController`）驱动状态转换，每步设置 `self._phase`（TurnPhase）供 debug 面板观测内部进度：

```python
class CognitiveLoop(CognitiveStreaming, CognitiveToolPhase, CognitiveFinalize):
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
- **ToolContext 会话换绑**: `process()` 每轮目标会话解析后原子更新 `tool_context.services["session"]`/`["_session_msg_count"]`——`clone_for_call` 浅拷贝 services，本 turn 所有工具调用副本（含 describe_image/self_state）读到目标会话当前图片/计数；切换/失败/取消由下一轮换绑覆盖（BE-S3-RUN-P0-01）
- **VAD 状态保护**: THINKING/EXECUTING 时 VAD 不打断，STT 文本和显式按钮始终生效
- **inner_voice 流式分流**: StreamProcessor 纯状态机检测 `<inner_voice>` 标签，通过 `_dispatch_stream_event()` 事件→副作用翻译。详见 `docs/superpowers/specs/archive/2026-06/2026-06-24-inner-voice-design.md`

### 4.4 TTS 绑定发送 + 句队列

```
LLM token stream
  → StreamProcessor.feed(delta)  [core/text 协调器]
    → splitter 分句完成 → CognitiveLoop.stream_sentence(sentence)
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

**衰减**: `analyze_assistant()` 每次回复后 `new_intensity = max(0.1, intensity - 0.15)`（每轮 -0.15，下限 0.1；`emotion.py:110`），低于 0.2 推送闲置表情。mood 文本不再重置——LLM 下轮通过 `<mood>` 自行描述心情变化。

**Expression 映射**: `mood` 文本（str）直接作为 expression 名透传 → OutputManager.expression()

详见 [4.10 CharacterEngine](#410-characterengine--角色性格情绪引擎)。

### 4.6 文本流解析（结构感知分句）

统一文本流组件族位于 `core/text/`（Task 7 重构后替代旧的分块过滤 + 分句缓冲组件）：

- **`tags.py` 标签注册表** — PAUSE/STAGE_CN/META/CODE/MARKDOWN 全部标签定义 + emoji 收口（`is_emoji_only`）
- **`tokenizer.py`（StreamTokenizer）** — 增量输入 → 只吐完整 token。标签起始前缀 / PAUSE 尾串 / STAGE_CN K-M 计数窗口持有上界；跨 chunk 不变式（任意切分点解析结果与一次到达一致）
- **`splitter.py`（SpeechSplitter）** — 结构感知分句：边界扫描只在 SPEECH 文本上进行，PAUSE `<#x#>` / STAGE_CN `（中文指示）` 以受保护 span 整体嵌入句子、内部标点不参与边界判定。标点逻辑（英文缩写/小数/URL 保护、连续标点合并、min_chunk 暂留、max_buffer 强制切分）从旧组件吸收
- **`parts.py`** — 句子 + 块按 `after_sentence_idx` 交错 → TextPart + content_type（session 存储）
- **`stream_processor.py`（协调器）** — tokenizer + splitter + 事件发射 + `get_blocks()` + public_text 重建

数据流：`LLM delta → StreamProcessor.feed(delta)` → token 流 → splitter → SENTENCE（含内嵌标签）+ TEXT_DELTA（剥离 PAUSE）事件 → `_dispatch_stream_event()` 分发。

详细组件职责见 [core/text/README.md](../core/text/README.md)；设计基准（行为对照表 + 持有上界 + 围栏细则）见 `docs/superpowers/specs/archive/2026-08/2026-08-01-stream-parser-refactor-design.md`。

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

**Section 提供者架构** (`Module/system_prompt/` protocol + sections + templates): 2026-07-04 重构，将 `Brain.j2` 单体拆分为 12 个独立的 `PromptSection` 模块（ConflictRules, OutputFormat, TTSMarkup, Sensory, Emotion, Memory, Personality, Relationship, SpeakingStyle, Proactive, OwnLife, SelfPerception）。每个 section 自管数据源、自管 `.j2` 模板、可独立 reload/validate。`SystemPromptManager` 按固定顺序 async render 并拼接。详见 `docs/superpowers/specs/archive/2026-07/2026-07-04-prompt-section-provider-design.md`。

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

薄适配层 (~213 行)：接收 `model` 标识符 (如 `"deepseek/deepseek-chat"`)，通过 `LLMFactory.create()` 创建 Module 层 backend，提供 `think()` 和 `stream_think()` 方法。

测试用 `FakeLLMBackend` ([core/tests/fake_llm.py](../core/tests/fake_llm.py)) — 可配置 test double，支持正则分词逐 token 流式输出 + 故障注入（HTTP 429/503、超时、断连）。

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

**AwarenessTrigger**（C9 激活判定）：L3 非空 + 距上次触发超过 5 分钟（300s） → C9 渲染。冷却时间戳持久化到 CharacterState，重启不丢失。（注：`engine.py` 中 `AWARENESS_COOLDOWN_SECONDS=3600` 为已废弃的死代码，实际冷却由 `awareness.py` 的 `AWARENESS_JUDGMENT_COOLDOWN=300` 控制。）

**阻尼机制**：L3 偏移 L1 特质时，L1 决定"怎么说"（表达方式），L3 影响"说什么"（内容倾向）。Phase 2 引入 L4→L3、L3→L2 向上回流压缩。

**版本历史**：L2/C6 演化产物持久化到 `character_versions` MongoDB 集合（原 `character_l2_versions`），通过 `content_type` 字段区分 `"l2"` / `"c6"`，per type 独立计数。支持在线浏览 + 回滚；L2 回滚自动联动刷新 C6。

完整四层模型、模板渲染、数据流、调试入口 → [core/personality/README.md](../core/personality/README.md)

---

### 4.11 角色域（多角色运行时，Stage A–H）

在既有单角色 `character/{name}/config.yaml` + `personality.md` 之上渐进演进的**多角色 / 角色自定义**子系统（阶段 A–H 后端已落地）。核心是「角色定义（Definition）→ 不可变设定快照（Revision）→ 用户相处的演化体（Instance）」三级模型 + per-instance 运行时。

**领域模型与运行时（`core/character/`）：**

```
core/character/
├── definition.py       # CharacterDefinition — 稳定角色身份（characterId/slug/source/status）
├── revision.py         # CharacterRevision — 不可变设定快照（promptProfile/runtimeDefaults/checksum）
├── instance.py         # CharacterInstance — 相处并演化的实例（instanceId/revision/stateVersion/state）
├── draft.py            # CharacterDraft + ConversationExample — custom 唯一入口（Pydantic + 语义双层校验）
├── service.py          # CharacterService — create/get/list/update/clone/archive/rollback/validate + CAS
├── builtin_source.py   # BuiltinCharacterSource — 从 character/{name}/config.yaml 构建内置角色
├── catalog.py          # CharacterCatalog — 只读内置角色目录（无 Mongo 也能列/校验）+ get_revision
├── binding.py          # 会话↔实例绑定规范化边界（内部 snake_case；camelCase 仅迁移兼容输入）
├── runtime.py          # CharacterRuntime — 单实例容器（快照 + 懒 engine + memory_view/consolidator/reflector + lock/dirty）
├── registry.py         # CharacterRuntimeRegistry — LRU(32) + busy/session_binding 保护 + flush_all/close_all
├── runtime_loader.py   # 持久化实例 → 精确 runtime 源解析
├── runtime_profile.py  # Revision → 安全运行时输入的单一类型化边界（engine/prompt 共用）
├── session_binding.py  # 新会话角色解析 + canonical instance + 摘要快照编排
├── public_state.py     # make_public_state — 内部扁平态 → 产品裁剪视图（内部态不外泄）
└── preview.py          # CharacterPreviewPort — 无状态预览推理（零副作用，BE-STAGEH-03）
```

**关键设计：**
- **engine per-instance**（阶段 E，M3）：`CharacterRuntime` 懒建 per-instance engine，构造时与 `runtime.state`（`CharacterRuntimeState`）共享同一引用——`_apply_mood`/`_finalize_turn` 读写即 runtime.state。**`CharacterState` 退役、`session.character_state` 不再权威**；`engine.attach_session` 等换绑动作全部删除，engine 永不换绑，从根上消除跨会话污染。
- **状态权威 + CAS**：`CharacterInstanceRepository`（`character_instances` 集合）持久化 `state` + `state_version` 乐观锁；`CharacterRuntimeRegistry` 按 instance_id 解析/懒建 runtime（LRU 驱逐前检查 dirty / busy / session_binding 三类受保护状态）。
- **操作 lease**（BE-STAGEH-01 + P0-1）：`try_begin_generation` / `end_generation` 等由一把同步 `_op_guard`（`threading.Lock`）保护「判定 + 登记」同一原子步；generation 在途（含 tail 收尾）时 reset 立即 `CharacterBusyError`（409）。tail lease 覆盖 `_after_response`（记忆沉淀 / 性格反思 / 会话保存）。
- **custom CRUD**：`CharacterService` 采用 **Revision-first 写序**（先 append revision 再 CAS 指针，失败回滚），update/rollback 枚举相关 instance 按 `follow_latest`/`pinned` × `preserve`/`reset_evolution` 传播。归档前先改写 `UserIdentity.defaultCharacterId` 到回退角色（反序会留悬空引用）。
- **能力门禁**：真实 `Agent` 在四道隔离门禁通过后发布 `character_profiles=true`；`character_state_stream=true` 时 v2 发 `CharacterStateChangedMessage`（只含产品态 `CharacterPublicStateView`，客户端按 `stateVersion` 严格递增应用）。

**配套：**
- 路由：`core/input/character_routes.py`（14 路由，含 `/api/me`、`/api/characters*`、`/api/character-instances/{id}/*`）——清单见 §6.3。
- 存储：`core/storage/character_repo.py`（`characters`）/ `character_revision_repo.py`（`character_revisions`）/ `character_instance_repo.py`（`character_instances`）+ `character_version_repo.py`（L2/C6 版本历史）。
- 契约：`frontend_contract/rest.py` 的 `Character*` 模型（`CharacterDraft`/`CharacterSummary`/`CharacterDetail`/`CharacterPublicStateView`/`CharacterPreview*` 等）+ `ws_message.py::CharacterStateChangedMessage` + `Live2DDirectiveMessage`。
- Agent 访问路径（公开只读 `@property`）：`agent.character_runtime_registry` / `agent.character_profiles` / `agent.character_state_stream`。

完整设计见 [core/character/README.md](../core/character/README.md) 与 [角色域架构稿](2026-08-30-character-prompt-customization-backend-architecture-design.md)。

### 4.12 用户最小身份（UserIdentity）

用户「最小身份」模型（架构稿决策 #7）——Prompt 所需的代词 / 显示名从角色 / 全局配置移出。

```
core/profile/
├── user_identity.py      # UserIdentity — userId/displayName/preferredAddress/pronouns/locale/timezone/defaultCharacterId/revision
└── profile_store.py      # UserIdentityStore — Mongo 权威 + 进程内读缓存（无 repo 时内存 + JSON 降级）
```

- **正位（B1）**：`CharacterConfig.user_pronoun` 已**彻底移除**（字段 + config.yaml + fixtures 删除），代词唯一权威是 `UserIdentity.pronouns`（全局唯一，恒「她」，非 per-role）。
- **Mongo 权威**（BE-STAGEH-02）：`UserIdentityRepository` 的 `user_identity_profiles` 集合是唯一持久化事实源；`UserIdentityStore` 只持进程内读缓存。`Agent.start()` 经 `_start_persistence` 装载 / bootstrap（`$setOnInsert`，并发启动只有一个 winner），重启不回到 bootstrap；repo 不可用 → fail-closed（不伪造本地档案）。
- **CAS 更新**：`update_if_revision(expected, updates)` 走 Mongo 原子 CAS，白名单字段（displayName/preferredAddress/pronouns/locale/timezone/defaultCharacterId/initialized）；权威不可用 → `StorageUnavailableError` → 503（不悄悄写本地伪造「已更新」）。
- **消费**：`CognitiveLoop._prepare_turn` 每轮 `get()` 冻结进 `TurnCharacterContext`（displayName/preferredAddress/pronouns/locale/timezone/revision），Prompt 称呼 / 代词 / 时区一律取自该快照，不再读 `config.agent.user_name`。
- 路由 `/api/me`（GET/PUT，见 §6.3）；Agent 访问路径 `agent.user_identity_store`。

完整设计见 [core/profile/README.md](../core/profile/README.md)。

---

### 5.1 记忆三层模型

| 层 | 存储 | 生命周期 | 容量 | 用途 |
|------|------|------|------|------|
| Working | 内存 (单次推理) | 单次 LLM 调用 | 动态 | PromptAssembler 组装 |
| Session | MongoDB / deque | 跨进程重启 | 代码默认 20，config.yaml 覆盖为 50 | 对话上下文 + CharacterState / ServiceState（专用类型字段经 save/load 持久化） |
| Long-term | Milvus + 内存 | 跨会话 | 无上限 | 事实/偏好/计划 语义搜索 |

### 5.2 SessionMemory

> **批次 3（多会话）**：单实例之外新增 `SessionManager`（`core/memory/session_manager.py`）——
> 会话缓存池 + 活动指针 + `resolve(conversation_id, protocol_version)` + `switch`/`create`/`delete`。
> `agent.session` 仍指活动会话；v1 恒 default、v2 显式 conversationId → 该会话、v2 缺省 → active。

```python
class SessionMemory:
    _messages: deque[Message]     # 最多 max_messages 条 (config.yaml session_max_messages). Pydantic Message 对象
    character_state: CharacterState  # 性格+情绪+reflector buffer 等（专用类型字段）
    service_state: ServiceState      # 服务运行时状态（当前模型 + STT/TTS 启用模式，enabled 三态 bool|None）
    _db: DocumentDBBackend | None # MongoDB 或降级

    # 运行时状态经专用类型字段持久化：save() 写 character_state + service_state
    # （reflector 计数器经 set_memory_reflector() 在 save 时并入 character_state，session.py:210/246），
    # load()/load_from_jsonl() 恢复（MongoDB 持久化 + JSONL fallback；2026-08-10 后
    # sessions 不再建 Mongo TTL 索引，删除走显式级联删除）。已无 _reflector_state 字段。
    async connect_db() → bool
    async save() → bool           # Upsert by session_id（MongoDB 优先，不可用时降级 JSONL）
    async load(sid?) → bool       # 从 MongoDB 恢复（JSONL fallback）
    async load_from_jsonl() → bool  # 从本地 JSONL 文件恢复
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

`memory_type` 输入经 `MemoryType.coerce` 归一：lowercase/大小写混 → TitleCase（`"fact"`→`"Fact"`），未知 → `"Note"`；`search()`/`/debug/api/ltm` 过滤经 `try_coerce` 大小写不敏感，无效值返回空。

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
tool_adapter = ToolAdapter(ALL_PLUGINS, ...); await tool_adapter.start()

# CognitiveLoop 自动:
# tool_definitions() → LLM 请求（通过 API tools 参数传递，不再注入 system prompt）
# LLM 返回 tool_calls → Action Gate (工具存在性校验) → SafetyPipeline (validate→safety→hitl→execute→truncate) → 结果回注 messages
# GATE_REJECTED: 调度前被 gate 拦截，与 UNKNOWN_TOOL（执行层发现）分工明确
```

**热重载语义（E1，P2-06）**：`reload_plugin` 返回 `(bool, str)`——成功/失败 + 原因。
流程：锁外 `importlib.reload` 新模块 + 重新 `_discover_class`（不再读旧 `_plugin_classes`）+
共用 `_load_safety_config`（config.yaml → SafetyProfile）+ 构造/setup 新实例 → 持
`_reload_lock` 身份复核 + 原子替换四注册表 dict（微秒窗口）→ 锁外排空旧实例 in-flight
（`_inflight` 按实例快照，超时 10s 转后台延迟 teardown，`_teardown_tasks` 独立登记、不被
`cancel_all` 取消、shutdown 显式 gather 收尾）。新 setup 失败/身份被并发覆盖 → 回滚保留
旧实例，注册表零改动。锁仅覆盖 swap——排空期其他工具与新调用不受阻塞，调用只见完整旧版
或完整新版（无混合状态）。

详见 `Module/tool/README.md`。

已注册工具（16 个）：
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
| `desktop.file.select` | — | 桌面端文件选择器（批次 4，经 DesktopToolBridge 委托执行，remote 侧结果回调） |
| `desktop.file.read` | path, range | 桌面端文件读取（批次 4） |
| `desktop.file.write` | path, content | 桌面端文件写入（批次 4） |
| `desktop.screen.capture` | region, scale | 桌面端屏幕截图（批次 4） |

CognitiveLoop 使用 `asyncio.gather` 并行执行无依赖工具。

### 5.6 记忆作用域（Stage F，记忆 v2 隔离）

阶段 F 引入显式**记忆作用域**——任意记忆写 / 读必须带 scope，不存在「漏传即全局」默认值。

```python
# core/memory/scope.py
class MemoryVisibility(StrEnum):
    CHARACTER_PRIVATE = "character_private"  # 角色私有（产品记忆一律此值）
    USER_SHARED = "user_shared"              # 预留枚举，不提供自动升级路径

@dataclass(frozen=True)
class MemoryScope:
    user_id: str
    character_instance_id: str
    visibility: MemoryVisibility = MemoryVisibility.CHARACTER_PRIVATE
```

- **`ScopedLongTermMemory`**（`core/memory/scoped_long_term.py`）——绑定 `MemoryScope` 的记忆视图：`insert`/`search`/`update_text`/`delete`/`get_all` 全部作用域过滤，底层共享同一 `LongTermMemory`，但 scope 在视图创建时**固定**，调用点无法在方法调用时绕过。
- **`MemoryEntry` 新增 scope 字段**（阶段 F §2.3）：`user_id` / `character_instance_id` / `visibility`；`search(scope=...)` 双路径过滤——ANN 阶段用 Milvus filter（避免其他 scope 条目挤占 top-k 导致本 scope 召回丢失），Python fallback 再过滤。
- **存储**：`core/storage/memory_repo_v2.py::MemoryRepositoryV2`（Milvus 集合 `long_term_memory_v2`）——产品记忆全部写 v2。
- **装配**：`agent._make_runtime` 构造 `CharacterRuntime` 时经 `for_scope()` 挂入 `memory_view` / `consolidator` / `reflector`（三者各持 scoped view，scope 在视图创建时固定），consolidator / reflector 因此 **per-runtime 化、无全局单例**。

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

协议已**契约层化**：前后端消息类型的唯一类型来源是 `frontend_contract/ws_message.py`（Pydantic 判别联合 + `gen_ts` 生成 `contract.generated.ts` 只读快照）。本表只列消息类别概要，**字段定义与序列化以契约模型为准**。

**Server → Browser**（`ServerMessage` 判别联合，现 42 具名成员）：

| 类别 | 成员 | 说明 |
|------|------|------|
| 会话/能力 | `SessionMessage`、`CapabilitiesMessage`、`ServerInfoMessage`、`WakeMessage` | 建连会话历史、能力声明（v2 改由 `server_info` 取代）、唤醒通知 |
| 流式文字 | `DeltaMessage`、`MessageMessage`、`TypingMessage` | 增量 / 完整消息 / 输入中指示器 |
| TTS 音频 | `AudioMessage`、`AudioEndMessage`、`AudioStartMessage`、`AudioCancelledMessage` | 音频 chunk / 结束 / 开始 / 取消（BE-063 播放控制） |
| 状态/情绪 | `StateMessage`、`ExpressionMessage`、`Live2DDirectiveMessage`、`ActionMessage`、`CommandMessage` | FSM 状态、表情、Live2D 表现建议（客户端按现实态优先级仲裁）、动作、inner_voice/tool_approval/block 指令 |
| STT | `TranscriptionMessage`、`VoiceTranscriptMessage` | 声学结果、实时语音转写 |
| 语音会话（批次 6） | `VoiceSessionStartedMessage` / `VoiceSessionPartialMessage` / `VoiceSessionTurnMessage` / `VoiceSessionAckMessage` / `VoiceSessionEndedMessage`、`DictationPartialMessage` / `DictationFinalMessage` / `DictationCancelledMessage` | 三模式语音状态机（普通录音/实时语音/输入法转写，BE-060~062） |
| generation 生命周期（批次 2） | `GenerationStartedMessage` / `GenerationSegmentStartedMessage` / `GenerationDeltaMessage` / `GenerationSegmentCompletedMessage` / `GenerationCompletedMessage` / `GenerationFailedMessage` | 统一回合事件双 wire 呈现 |
| 工具 | `ToolRequestMessage`、`UserAckMessage`、`ToolStartedMessage`、`ToolCompletedMessage` | HITL 审批请求、用户确认（批次 4）、Backend 工具展示生命周期（v2 展示帧，sourceCallId 关联） |
| 角色（阶段 G） | `CharacterStateChangedMessage` | 角色产品态变化（`character.state.changed`，只含 CharacterPublicStateView，按 stateVersion 严格递增应用） |
| 服务管理 | `SwitchModelResultMessage`、`ServiceModeResultMessage`、`SetTtsVoiceResultMessage` | 模型切换 / STT·TTS 模式开关 / TTS 声音选择（批次 5） |
| 统计/错误 | `TokenUsageMessage`、`ErrorMessage` | Token 统计、统一错误信封（含 retryable/details） |

**Browser → Server**：`text`（文本/附件，含 requestId 幂等）/ `interrupt` / `wake_toggle` / `switch_model` / `set_service` / `set_tts_voice`（批次 5）/ `tool_approval` / `activate_session`（批次 3 多会话切换）/ `tool.status`（批次 4 桌面调用状态）/ 语音控制（`voice.start` / `voice.stop` / `audio.cancel` 等，批次 6）/ binary（Int16 PCM 麦克风音频）。客户端消息同样在 `frontend_contract/ws_message.py` 中定义。

> **注意**：早期版本在本文档手工枚举 payload 字段（delta/message/typing/…）的做法已废弃——前后端契约单源机制落地后，任何字段变更只改 `frontend_contract/ws_message.py`，用 `make gen-types` 再生成 TS 快照、`make check-types` 做漂移门禁。

### 6.3 WebSocket 服务器 (core/input/)

```
aiohttp Application（78 个显式 handler 注册，不含 aiohttp 自动 HEAD；另有 4 个静态挂载）。2026-08-14 起路由按域拆到独立模块，定义分布见 `core/input/server.py` + 下列 `*_routes.py`：

**server.py（10）— 纯装配 + 页面/管理/健康端点：**

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/` | 聊天页面（`static/index.html`，`serve_chat` 开启时注册） |
| GET | `/debug` | Debug 页面（`static/debug.html`） |
| GET | `/ws` | WebSocket handler（协议版本协商 + 并发上限 + 建连限流） |
| GET | `/health`, `/ready` | 健康检查（批次 7 拆分：liveness/readiness + 真依赖检查） |
| GET\|POST | `/shutdown` | 关闭服务 |
| POST | `/restart` | 重启服务 |
| GET | `/tools` | 工具插件列表（管理端点） |
| POST | `/tools/reload` | 热重载工具插件（返回 `{ok, name, error?}`——成功/失败 + 原因，E1） |

**provider_routes.py（4）：**

| 方法 | 路径 | 功能 |
|------|------|------|
| GET\|POST | `/api/providers` | 模型 provider 列表 / 添加自定义 provider（批次 5：密钥入 Secret Store + SSRF/runtime 守卫） |
| DELETE | `/api/providers/{service}/{name}` | 删除自定义 provider（激活中禁止） |
| POST | `/api/providers/test` | 连通性测试（批次 5：SSRF 守卫 + runtime 门控） |

**tts_routes.py（2）：**

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/api/tts/voices` | TTS 声音列表（批次 5 BE-051） |
| POST | `/api/tts/preview` | TTS 固定短文本试听（批次 5，不读聊天） |

**upload_routes.py（3）：**

| 方法 | 路径 | 功能 |
|------|------|------|
| POST | `/api/images` | 图片上传（遗留端点） |
| POST | `/api/uploads` | 通用附件上传（批次 7，filetype 魔数校验 + 签名 URL + 64MB；阶段 3 逐文件 kind——每文件一组 `kind+files` 邻接配对；MIME 规范化单源 `normalize_attachment_mime`） |
| GET | `/api/stickers` | 内置表情包列表（批次 7，sticker_registry） |

**tools_routes.py（3）：**

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/api/tools` | 工具元数据导出（批次 4，runtime 过滤） |
| GET | `/api/tools/calls/{callId}` | 桌面调用状态查询（批次 4） |
| POST | `/api/uploads/desktop/{callId}` | 桌面附件上传（批次 4，持有者校验） |

**chat_routes.py（2）：**

| 方法 | 路径 | 功能 |
|------|------|------|
| POST | `/api/chat` | JSON chat API |
| GET | `/api/messages` | 消息归档分页查询 |

**session_routes.py（13，批次 3 多会话）：**

| 方法 | 路径 | 功能 |
|------|------|------|
| GET\|POST | `/api/sessions` | 会话列表 / 创建 |
| GET\|PATCH\|DELETE | `/api/sessions/{conversationId}` | 会话详情 / 重命名 / 删除 |
| POST | `/api/sessions/{conversationId}/activate` | 切换活动会话 |
| GET | `/api/sessions/{conversationId}/messages` | 会话消息历史（before/after cursor 分页） |
| GET | `/api/sessions/{conversationId}/resources` | 会话资源（sticker 分类 + 附件软删过滤） |
| GET | `/api/sessions/{conversationId}/generations` | 会话 generation 列表（BE-024） |
| GET | `/api/sessions/{conversationId}/pending` | 会话 pending 消息 |
| GET | `/api/messages/{messageId}/context` | 消息上下文窗口 |
| GET | `/api/search/messages` | 全库消息搜索（category 白名单 8 值；date_from/date_to 严格 YYYY-MM-DD fail-fast） |
| GET | `/api/generations/{generationId}` | generation 详情（BE-024） |

**attachment_routes.py（2，批次 7 BE-070）：**

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/api/attachments/{attachment_id}` | 附件签名下载（HMAC 短签，Range/206；Content-Type 过 `normalize_attachment_mime`） |
| GET | `/api/attachments/{attachment_id}/sign` | 签发下载签名（Bearer） |

**character_routes.py（14，阶段 A–H 角色域 + 用户身份）：**

| 方法 | 路径 | 功能 |
|------|------|------|
| GET\|PUT | `/api/me` | 用户最小身份读取 / 更新（CAS `revision`，白名单字段；权威不可用 503） |
| GET\|POST | `/api/characters` | 角色列表（内置 catalog + custom repo）/ 创建 custom 角色 |
| POST | `/api/characters/validate` | 草稿双层校验（Pydantic 边界 + `validate_semantic`）（固定路径先于 `{id}` 注册） |
| POST | `/api/characters/preview` | 无状态角色预览（组装一次性 Prompt + 单次推理，零副作用，BE-STAGEH-03） |
| GET\|PUT\|DELETE | `/api/characters/{id}` | 角色详情 / 更新 / 归档 |
| POST | `/api/characters/{id}/clone` | 克隆角色 |
| GET | `/api/characters/{id}/revisions` | 角色 Revision 历史（append-only） |
| POST | `/api/characters/{id}/rollback` | 回滚到指定 Revision（产生新 Revision，不删历史） |
| GET | `/api/character-instances/{id}/state` | 角色实例产品态快照（经 `agent.character_runtime_registry` 解析） |
| POST | `/api/character-instances/{id}/reset-evolution` | 重置实例演化态（`stateVersion` CAS） |

**debug_routes/（25 handler 注册 / 18 路径）：**

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/debug/api` | DebugInfo 完整 JSON（16 面板） |
| GET | `/debug/api/db-status` | 数据库连接状态 |
| GET | `/debug/api/prompts` + `{n}` | Prompt 记录列表/详情 |
| GET | `/debug/api/responses` + `{n}` | Response 记录列表/详情 |
| GET\|POST\|DELETE | `/debug/api/ltm` | LTM 列表 / 新增 / 清空 |
| PUT\|DELETE | `/debug/api/ltm/{entry_id}` | 更新 / 删除单条记忆 |
| GET\|DELETE | `/debug/api/sessions` | Session 列表 / 清空全部 |
| GET\|DELETE | `/debug/api/sessions/{session_id}` | Session 详情 / 删除 |
| GET\|DELETE | `/debug/api/archive` | Archive 分页 / 清空全部 |
| PUT\|DELETE | `/debug/api/archive/{msg_id}` | 更新 / 删除单条归档 |
| GET | `/debug/api/tasks` | WorkflowEngine 快照 |
| GET | `/debug/api/tasks/stream` | WorkflowEngine SSE 事件流 |
| GET | `/debug/api/l2-versions` | L2 self-image 版本历史 |
| POST | `/debug/api/l2-rollback` | L2 self-image 回滚（指定版本） |
| GET | `/debug/api/character-versions` | Character 版本历史（L2/C6，通过 type 参数选择） |
| POST | `/debug/api/character-rollback` | Character 内容回滚（L2/C6，通过 type/version 参数） |

**静态挂载（4）**：`/static/`（构建产物）、`/live2d/`（Live2D 资源）、`/uploads/images/` + `/uploads/desktop/`（附件静态目录——已收窄：只挂 images/desktop 两个子目录，新 kind 目录走 `/api/attachments/{id}` 签名下载，见 `core/input/server.py` 静态挂载注册）。

每个 WS 连接:
  └── 创建 WebSocketChannel → OutputManager
  └── 创建 AudioBuffer (if STT enabled)
  └── 启动 state-monitor task:
        FSM → "speaking" → audio_buf.deafen(2.0)  # 防回声
  └── 消息循环: TEXT → process_message, BINARY → audio_buf.feed()
  └── disconnect: cleanup channel + audio_buf + monitor
```

### 6.4 浏览器客户端 (frontend/)

Vue 3 + Vite + Pinia + TypeScript。开发时通过 Vite 代理（:5173 → :8765），生产构建经 `vite.config.ts` `outDir` 直接输出到 `static/` 由 aiohttp serve（无 `frontend/dist/` 复制步骤）。

三栏布局：
- **左栏**: 角色面板 — 状态灯 (CSS transition)、心情标签、LLM 模型名
- **中栏**: 对话区 — 消息气泡（TransitionGroup 动画）、时间戳分隔线 (5min)、typing dots 动画
- **浮层**: 设置面板 + 工具管理面板 + HITL 审批弹窗

关键实现：
- 21 个 Vue 3 组件，全部使用 `<script setup lang="ts">` + `<style scoped>`
- 6 个 Pinia store（connectionStore, chatStore, audioStore, settingsStore, toolsStore, debugStore）
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
  host: 127.0.0.1                    # 默认回环；远程暴露用 SERVER_HOST=0.0.0.0 覆盖
  port: 8765

tts:
  enabled: true                      # CLI --tts 可覆盖

stt:
  enabled: true                      # CLI --stt 可覆盖

wake_word:
  enabled: false                     # 语音唤醒开关

live2d:
  static_image: "/static/Elysia.png" # Live2D 未启用时的静态立绘

database:
  mongodb_uri: mongodb://localhost:27017  # MongoDB 连接串
  milvus_uri: http://localhost:19530     # Milvus 服务端点
  db_name: default                       # MongoDB 数据库名

vision:
  model: mimo/mimo-v2.5              # describe_image 视觉模型
```

优先级: `CLI flag > 环境变量 (.env) > config.yaml > AppConfig 默认值`

### 7.2 角色配置 (character/elysia/config.yaml)

每个角色一个子目录：`character/<name>/config.yaml` + `character/<name>/personality.md`。

```yaml
name: Elysia
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
| 概览 | `/debug/api` 轮询 | 16 面板（2 列 grid）：System、Memory（含 Session 展开）、Latency、Process Memory、Scheduler、Token、LLM Prompt 查看器、Raw Response、LTM Recent、Tools、Tool History、Consolidation、Memory Reflector、Character Reflector、CharacterEngine |
| 数据库 | `/debug/api/db-status` + `/debug/api/ltm` + `/debug/api/sessions` + `/debug/api/archive` | LTM CRUD + 分页 + 搜索；Session 管理（从 archive 权威源读取，限 20 条）；Archive CRUD |
| 任务 | `/debug/api/tasks/stream` SSE | WorkflowEngine 实时监控 + DAG 可视化 (vis-network) |

---

## 9. 组件组装 (Agent)

```python
class Agent:
    def __init__(self, *, config: AppConfig):
        # L0: OutputManager + ConsoleChannel
        # Character: CharacterConfig.from_yaml(config.agent.character)
        # L2: SessionMemory + LongTermMemory + ToolAdapter
        #     + register 16 tools (ALL_PLUGINS)
        # L1: EventBus + AgentFSM + LLMAdapter + PromptAssembler
        #     + CharacterEngine + MemoryConsolidator
        #     + CognitiveLoop + IdleMonitor
        # TTS: 根据 config.tts.enabled 决定是否初始化

    async def start():
        # AgentFSM.reset() + LLMAdapter.reconnect()
        # _start_persistence() — MessageRepository/SessionRepository/TokenRepository/
        #   MemoryRepository + MongoDB/Milvus 并行连接 + SessionManager 恢复
        # 随后按 _STARTUP_STEPS 数据驱动执行:
        #   token+prompt → tool_context → worker_pool → idle+events → service_state
        #   → tts → task_system → mailbox_polling → db_health → attachment_repo → attachment_gc
        # 任一启动步骤失败 → 回滚 stop()（不留下 RUNNING 僵尸）
        # 事件处理循环 _process_events 由 _start_idle_and_events 启动

    async def stop():
        # 按 _SHUTDOWN_STEPS（18 步）顺序执行：
        # 1. _stop_event_task() — cancel event processing
        # 2. _stop_cognitive_and_tts() — cancel cognitive + TTS tasks
        # 3. _flush_after_response() — 等待 fire-and-forget after-response task 完成
        # 4. _flush_consolidator() — Memory consolidation buffer 落盘
        # 5. _flush_reflector() — CharacterReflector buffer 落盘
        # 6. _flush_compression() — L3→L2 压缩触发
        # 7. _stop_db_health() — cancel db_health_task
        # 8. _stop_mailbox() — cancel mailbox polling
        # 9. _stop_task_system() — stop WorkflowEngine
        # 10. _stop_scheduler() — scheduler.shutdown()
        # 11. _stop_tool_adapter() — tool_adapter.shutdown()
        # 12. _stop_idle() — idle_monitor.stop()
        # 13. _stop_embedding_if_running() — cancel embedding preload
        # 14. _stop_persistence() — session.save() → EmbeddingFactory.shutdown() → long_term.close()
        # 15. _stop_attachment_gc() — 停止附件 GC 兜底扫描
        # 16. _stop_attachment_repo() — 关闭附件仓储连接
        # 17. _stop_llm_and_tts() — llm.close() + tts.close()
        # 18. _stop_output() — output.close()
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

3,804 个测试按 5 层 markers 组织（unit / service / integration / slow / external opt-in；全量收集，合并门禁 `-m "not external"` 3,768，2026-08-16 实测 `make test-quick` 3618 passed / `make test-normal` 3726 passed；具体数字以 `scripts/print_test_stats.py` 为准，不手写易漂移）。

完整分层体系、测试文件索引、按模块运行命令 → [docs/testing.md](testing.md)

## 12. 文件结构

```
Agent/
├── Makefile                # 测试/构建命令
├── requirements.txt        # Python 依赖
├── docker-compose.yml      # 数据库容器（dev/production profile，仅 127.0.0.1 端口 + 启用认证）
├── pytest.ini              # pytest 配置（markers）
├── data/                   # 应用数据（上传、缓存、Live2D）
├── app.py                  # 入口 (argparse + ConfigLoader + CLI overrides)
├── config.yaml              # 总配置
├── CLAUDE.md                # AI 助手指令
├── README.md                # 项目说明
│
├── Module/                  # L0 基础设施层
│   ├── _common/              #   共享工具 (env + registry_helpers + ssrf + error_type + test_helpers + main_runner)
│   ├── llm/                 # LLM: DeepSeek / OpenAI / Ollama / Mimo / Custom
│   ├── stt/                 # STT: SenseVoice / Custom (OpenAI-compatible)
│   ├── tts/                 # TTS: GPT-SoVits / MiniMax / Custom (OpenAI-compatible)
│   ├── embedding/           # Embedding: HuggingFace / OpenAI
│   ├── output/              # Output: Console + WebSocket 多通道
│   ├── config/              # Config: YAML + env 加载器 + preflight
│   ├── system_prompt/       # System Prompt 引擎 + Section Provider + 12 templates
│   ├── database/            # Database: Milvus / MongoDB / GraphDB (stub)
│   ├── tool/                # Tool: ToolPlugin框架 + SafetyPipeline + ToolManager
│   ├── workflow_engine/     # WorkflowEngine: 异步任务编排 (Planner/Orchestrator/Executor)
│   ├── workflow_contract/   # 跨边契约层 (core ↔ workflow_engine 唯一类型来源)
│   └── bench/               # Bench: 延迟基准
│
├── core/                    # L1 认知控制 + L2 记忆
│   ├── agent/                #   Agent 组装包 (agent + lifecycle + server + _types + 4 lifecycle mixins)
│   │   ├── agent.py          #     Agent.__init__ + 事件处理 + process_message + 43 公开 @property（含 stt/stt_enabled/tts_enabled/stt_emotion_state/character_profiles/character_state_stream/character_runtime_registry/user_identity_store）
│   │   ├── lifecycle.py      #     AgentLifecycle: start/stop 编排 + _STARTUP_STEPS / _SHUTDOWN_STEPS
│   │   ├── server.py         #     AgentServer: run_server/repl + switch_*/set_service + run_server STT 槽位 + banner
│   │   ├── _types.py         #     RunState + 辅助类
│   │   ├── _lifecycle_persistence.py  #  持久化层启停
│   │   ├── _lifecycle_services.py     #  LLM/TTS/STT/Output/Token 启停 + _apply_service_state 启动步骤
│   │   ├── _lifecycle_tools.py        #  工具/调度/空闲监控 启停
│   │   └── _lifecycle_task_system.py  #  WorkflowEngine/DelegationPreparer/mailbox 启停
│   ├── adapter/
│   │   ├── llm_adapter.py
│   │   ├── tts_adapter.py
│   │   ├── tool_adapter.py
│   │   └── tool_call_parser.py
│   ├── prompt/               #   System prompt 组装层（CORE-MOD-P1-02 从 adapter 迁入）
│   │   ├── message_builder.py #  MessageBuilder: system prompt + history → messages[]
│   │   └── prompt_assembler.py # PromptAssembler: 12 sections → SystemPromptManager → MessageBuilder
│   ├── providers/            #   Provider 元数据与自定义 provider（CORE-MOD-P1-01 从 input 迁入）
│   │   ├── custom_providers.py
│   │   └── provider_registry.py
│   ├── services/             #   服务能力声明与广播（CORE-MOD-P1-01 从 input 迁入）
│   │   └── capabilities.py
│   ├── network/              #   网络运行时基础设施（CORE-MOD-P2-01 从 input 迁入）
│   │   └── rate_limiter.py
│   ├── cognitive/            #   CognitiveLoop 包 (loop + streaming + tool_phase + finalize + tools + timing + generation)
│   │   ├── loop.py           #     CognitiveLoop(CognitiveStreaming, CognitiveToolPhase, CognitiveFinalize): process + _prepare_turn + _think_loop + _handle_turn_exception
│   │   ├── streaming.py      #     CognitiveStreaming: _stream_llm_response + _dispatch_stream_event + _apply_mood（CORE-MOD-P1-04）
│   │   ├── tool_phase.py     #     CognitiveToolPhase: _execute_tool_round + _build_tool_result_parts（CORE-MOD-P1-04）
│   │   ├── finalize.py       #     CognitiveFinalize: _finalize_turn（CORE-MOD-P1-04）
│   │   ├── tools.py          #     CognitiveTools: 工具执行 + submit_task
│   │   ├── timing.py         #     Timer + TimingClock 延迟统计
│   │   ├── generation.py     #     GenerationContext: generation_id/assistant_message_id/conversation_id 预分配（ID 字段对齐 NewType：GenerationId/MessageId/SessionId/RequestId/SegmentId；channel/kind 对齐契约层 MessageChannel/SegmentKind StrEnum）
│   │   ├── generation_registry.py # GenerationRegistry: generation 生命周期持久化（register/mark 终态 + stale 清理）
│   │   ├── delegation.py     #     DelegationPreparer: 任务委托预处理
│   │   └── user_profile.py   #     用户画像
│   ├── monitoring/            #   监控子包
│   │   ├── event_bus.py       #     优先级事件总线
│   │   ├── idle_monitor.py    #     空闲监控 + 主动触发
│   │   └── token_tracker.py   #     Token 消耗统计
│   ├── text/                #   文本流处理包（标签注册表 + tokenizer + splitter + parts + 协调器）
│   │   ├── tags.py          #     标签注册表：PAUSE/STAGE_CN/META/CODE/MARKDOWN 全部定义 + emoji 收口
│   │   ├── tokenizer.py     #     StreamTokenizer: 结构感知切分（跨 chunk 不变式 + 持有上界）
│   │   ├── splitter.py      #     SpeechSplitter: 结构感知分句（span 保护 + SENTENCE/TEXT_DELTA）
│   │   ├── parts.py         #     TextPart + content_type 构建 / public_text 重建
│   │   └── stream_processor.py #  StreamProcessor 协调器（tokenizer + splitter + 事件发射 + get_blocks）
│   ├── task/                 #   任务子系统包
│   │   ├── __init__.py       #     重导出 Task + TaskScheduler
│   │   ├── task.py           #     任务数据模型
│   │   └── scheduler.py      #     Worker Pool 调度器
│   ├── utils/                #   通用工具包（message_id 重导出 shim + log_setup + text_utils + server_guard）
│   ├── schema/               #   共享类型包 (agent + types + part + tool + message + service_state + character + event + memory + memory_reflector + mood + token + compression + error_type + adapters + utils)
│   ├── status/                #   AgentStatus 接口层 (protocol + FSMBacked 实现)
│   │   ├── protocol.py       #     AgentStatus（对外只读）+ CognitivePhaseController（对内写）
│   │   └── fsm.py            #     FSMBackedStatus + FSMBackedPhaseController
│   ├── storage/               #   存储层 — Repository 模式 (MongoDB/Milvus)（20 文件）
│   │   ├── base.py           #     Repository 基类 + 共享连接/索引辅助（bson_expires 等）
│   │   ├── session_repo.py   #     SessionRepository: session CRUD（无 Mongo TTL，显式级联删除）
│   │   ├── message_repo.py   #     MessageRepository: 消息归档 append-only
│   │   ├── token_repo.py     #     TokenRepository: token 统计持久化
│   │   ├── memory_repo.py    #     MemoryRepository: LTM Milvus 操作
│   │   ├── memory_repo_v2.py #     MemoryRepositoryV2: 面向 long_term_memory_v2 集合（阶段 F 记忆作用域 v2）
│   │   ├── generation_repo.py #    GenerationRepository: generation 生命周期 upsert/query（BE-024，ttl_expires_at TTL 30d）
│   │   ├── character_version_repo.py # CharacterVersionRepository: L2/C6 版本历史
│   │   ├── character_repo.py #     CharacterRepository: characters 集合 + CharacterDefinition CRUD（阶段 D）
│   │   ├── character_revision_repo.py # CharacterRevisionRepository: character_revisions 集合（append-only Revision + CAS）
│   │   ├── character_instance_repo.py # CharacterInstanceRepository: character_instances 集合（instance CRUD + state_version CAS，阶段 E）
│   │   ├── user_identity_repo.py #  UserIdentityRepository: user_identity_profiles 集合（唯一 user_id 索引，BE-STAGEH-02）
│   │   ├── attachment_repo.py #    AttachmentRepository: 附件元数据（BE-070，fail-closed，ref_count/refs 软删，TTL 不设由 GC 管）
│   │   ├── attachment_release.py #  AttachmentRelease: 会话/消息删除的附件引用释放共享 helper
│   │   ├── attachment_gc.py  #     AttachmentGC: 启动兜底扫描四规则（软删/孤儿/未关联/磁盘）
│   │   ├── desktop_call_repo.py #  DesktopCallRepository: 桌面调用状态（Mongo + 进程内缓存 + ttl_expires_at TTL 30d，批次 4）
│   │   ├── idempotency.py    #     v2 消息幂等存储（内存权威 overlay + Mongo 写穿 + recover 四分支，CORE-MOD-P1-01 从 input 迁入）
│   │   ├── idempotency_repo.py #    IdempotencyRepository: 幂等 claim 三态持久化（唯一约束 + TTL，claim 所有权 + restore 条件 upsert，D6c + 2026-08-12）
│   │   └── secret_store.py   #     SecretStore: 密钥存储（data/secrets/profiles.json，0600 + 原子写，批次 5）
│   ├── debug/                #   Debug 仪表板包
│   │   ├── info.py           #     /debug 数据聚合
│   │   ├── prompt_recorder.py #   Prompt 记录器
│   │   └── response_recorder.py # Response 记录器
│   ├── input/                #   Input: WebSocket + HTTP 服务器
│   │   ├── server.py         #     aiohttp 纯装配入口（create_app + 管理端点；bind 守卫已迁 core/utils/server_guard.py）
│   │   ├── middleware.py     #     认证 / payload / 限流中间件工厂 + _nosniff_uploads
│   │   ├── health.py         #     /health /ready 探针（liveness/readiness 分层）
│   │   ├── errors.py         #     统一错误信封（_protocol_error_response）
│   │   ├── _common.py        #     跨业务共享 helper（_send_ack/_find_channel/_tool_error/_archive_*）
│   │   ├── ws_handler.py     #     WebSocket 连接/消息路由（_WS_HANDLERS 注册表分发）
│   │   ├── ws_v2.py          #     v2 深业务（_route_v2_text/_handle_voice_commit + 幂等/ACK helper）
│   │   ├── provider_routes.py #    Provider 管理 REST
│   │   ├── tts_routes.py     #     TTS 声音 REST
│   │   ├── upload_routes.py  #     上传/表情 REST
│   │   ├── tools_routes.py   #     Desktop 工具 REST
│   │   ├── chat_routes.py    #     Chat API + 消息归档 REST
│   │   ├── session_routes.py #     多会话 REST（CRUD/历史/搜索/资源/generation 状态/pending，批次 3）
│   │   ├── attachment_routes.py #  附件签名下载 + 签发（批次 7 BE-070）
│   │   ├── character_routes.py #  角色域 REST（/api/me + /api/characters* + /api/character-instances/*，阶段 A–H）
│   │   ├── voice_sessions.py #     三模式语音状态机（普通录音/实时语音/输入法转写，批次 6 BE-060~062）
│   │   ├── sticker_registry.py #  内置表情注册表（批次 7 BE-071）
│   │   ├── tts_profile.py    #     TTS 声音 Profile（批次 5 BE-051）
│   │   ├── debug_routes/     #     Debug API 路由 (package: 25 注册 / 7 模块 + _shared)
│   │   ├── audio_buffer.py   #     音频缓冲 + VAD
│   │   ├── wakeword.py       #     唤醒词检测
│   │   └── __main__.py       #     python -m core.input 入口
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
│   ├── character/           #   角色域：领域模型 + 多角色运行时（阶段 A–H）
│   │   ├── definition.py / revision.py / instance.py   # Definition / Revision / Instance 三级模型
│   │   ├── draft.py / service.py / catalog.py / builtin_source.py  # Custom CRUD + 内置目录 + 草稿校验
│   │   ├── runtime.py / registry.py / binding.py / session_binding.py  # per-instance runtime + LRU registry + 绑定
│   │   ├── runtime_loader.py / runtime_profile.py      # 持久化 → runtime 源 / Revision → 安全运行时输入
│   │   ├── public_state.py  #     内部扁平态 → CharacterPublicStateView（产品裁剪）
│   │   └── preview.py       #     CharacterPreviewPort 无状态预览（零副作用）
│   ├── profile/             #   用户最小身份（UserIdentity）
│   │   ├── user_identity.py #     UserIdentity 模型（pronouns 唯一权威）
│   │   └── profile_store.py #     UserIdentityStore（Mongo 权威 + 进程内缓存 + CAS）
│   ├── memory/              #   记忆子系统
│   │   ├── session_manager.py #   SessionManager（缓存池 + 活动指针 + resolve/switch/CRUD，批次 3 多会话）
│   │   ├── session.py       #     SessionMemory (MongoDB + deque; character_state/service_state 专用字段)
│   │   ├── long_term.py     #     LongTermMemory (Milvus + Embedding)
│   │   ├── scope.py         #     MemoryScope + MemoryVisibility（记忆作用域，阶段 F；写/读必须显式带 scope）
│   │   ├── scoped_long_term.py #  ScopedLongTermMemory（绑定 scope 的记忆视图，调用点无法绕过 scope）
│   │   ├── embedding_provider.py #  EmbeddingProvider: 嵌入懒加载 + fallback
│   │   ├── llm_protocol.py        #  MemoryLLM Protocol（MemoryReflector/Consolidator 的 LLM 能力面）
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
│   ├── tools/               #   Agent 工具集 (16 个插件)
│   ├── desktop/             #   桌面桥（DesktopToolBridge: desktop_calls 集合 + 持有者校验）
│   ├── hitl.py              #   HITL 审批通知器（pending 审批 /tools 审批流）
│   ├── version.py           #   后端版本（__version__ + build SHA，server_info.server_version）
│   └── tests/               #   测试
│
├── frontend_contract/          # 前后端契约单源（零依赖，仅 pydantic + stdlib）
│   ├── base.py                 #   ContractModel 基类 + TsNull marker + wire_dict 序列化
│   ├── version.py              #   TRANSPORT_VERSION="2" / PROTOCOL_VERSION="2.0" 协议版本单源（批次 9B）
│   ├── ws_message.py           #   WS 消息契约（ServerMessage 判别联合 42 具名成员 + 支持类型 + Client 消息）
│   ├── rest.py                 #   REST 业务契约（providers/images/messages/多会话/Desktop/附件/角色域 等 84 模型）
│   ├── debug.py                #   Debug API 契约（DebugData 16 面板等 38 模型）
│   ├── errors.py               #   错误码单源（54 码，F5；check_error_codes.py 比对协议文档 §9.1）
│   ├── gen_ts.py               #   TS 发射器：模型 → contract.generated.ts + generate_raw_ts（raw wire 快照，批次 9B）
│   ├── gen_schema.py           #   JSON Schema 发射器：模型 → contract.schema.json（含 schemaHash，批次 9B）
│   ├── contract.schema.json    #   生成产物（机器契约 JSON Schema，Electron replay 主源，批次 9B）
│   ├── contract.raw.ts         #   生成产物（Electron raw wire 类型快照，批次 9B）
│   ├── __main__.py             #   python -m frontend_contract（生成 ts/schema/raw_ts 三产物 / --check 漂移门禁）
│   ├── README.md               #   契约层说明
│   └── tests/                  #   契约建模测试（marker=unit）
│
├── character/
│   ├── elysia/              # Elysia 角色
│   │   ├── config.yaml       #   角色配置
│   │   ├── personality.md    #   L1 Core 人格文本
│   │   └── reference/        #   GPT 生成参考
│   └── cyrene/              # Cyrene 角色
│       ├── config.yaml       #   角色配置
│       ├── personality.md    #   L1 Core 人格文本
│       └── reference/        #   GPT 生成参考
│
├── frontend/                  # Vue 3 前端源码 (Vite + Pinia + TypeScript)
│   ├── index.html              # 聊天页面入口
│   ├── debug.html              # Debug 仪表板入口
│   ├── vite.config.ts          # 代理 :8765 + 多入口构建
│   ├── src/
│   │   ├── main.ts             # App 入口
│   │   ├── App.vue             # 根组件
│   │   ├── styles/variables.css
│   │   ├── types/protocol.ts   # 契约类型 shim（re-export contract.generated.ts + 少量纯前端辅助类型）
│   │   ├── stores/             # 6 个 Pinia store
│   │   ├── composables/        # 4 个 composables
│   │   ├── components/         # 21 个 Vue 组件
│   │   └── debug/              # 独立 Debug 应用
│
├── static/                     # 静态资源（仅图片/文档）
│   ├── Elysia.png              # 角色头像

├── docs/                   # 文档
│   ├── README.md           #   文档导航索引
│   ├── architecture.md     #   架构详解（本文档）
│   ├── testing.md          #   测试指南
│   ├── readme-conventions.md #  README 编写规范
│   ├── database-storage.md #   数据库存储设计
│   ├── code-style-guide.md #   代码风格与类型约束指南
│   ├── completed.md        #   已完成记录
│   ├── completed/          #   已完成月度归档
│   ├── progress/           #   进度与执行记录（stage-* execution-record + archive/）
│   ├── electron-backend-protocol-v2.md #  Electron 后端协议 v2
│   ├── electron-backend-security.md    #  Electron 后端安全
│   ├── archive/            #   历史归档（data-structure-survey / sync-lessons / experience 等）
│   ├── review/             #   代码/文档审查报告
│   ├── legacy/             #   已废弃文档（历史参考）
│   └── superpowers/
│       ├── specs/          #   设计规格（-design.md）
│       ├── plans/          #   实施计划（-plan.md）
│       └── archive/        #   历史归档
├── log/                     # 日志文件 (gitignore)
└── scripts/                 # 启动/预检/工具/清理脚本
    ├── setup.sh            #   一键安装
    ├── start.sh            #   一键启动
    ├── check.sh            #   环境预检
    ├── download_model.py   #   嵌入模型下载
    ├── clean_db.py         #   数据库清理（MongoDB + Milvus + 本地文件；--conversation-only 只清对话数据）
    ├── backfill_message_ids.py  # 历史消息 ID 回填
    ├── capture_ws_trace.py #   WS trace 捕获（golden frames 用）
    ├── gen_contract_frames.py   # 契约 golden frames 生成（--check 漂移门禁）
    ├── stage4_smoke.py     #   阶段 4 确定性 smoke（Profile/三语音模式/TTS 播放）
    ├── print_test_stats.py #   测试统计打印（marker 分层计数）
    ├── check_error_codes.py #  错误码目录比对（errors.py ↔ 协议文档 §9.1）
    ├── check_ci_isolation.py    # CI 隔离检查
    ├── check_compose_security.py # docker-compose 安全检查
    ├── check_env_lock.py   #   环境锁检查
    ├── dev/                #   开发用脚本（stage_h_pinned_smoke / seed_stage_h_pinned）
    ├── tests/              #   脚本测试（slow/独立）
    └── README.md           #   脚本说明
```

> **后台任务子系统三区（workflow_engine 分离后）**：`core/`（应用层）──import──▶ `Module/workflow_contract`（跨边契约层）◀──import── `Module/workflow_engine`（引擎，干净叶子）。跨边类型（`DelegationContext` / `AgentInfra` / `HITLNotifier` 等）唯一来源在契约层，两边各自单向 import、互不感知对方内部实现。`DelegationPreparer` / `EventBusHITLNotifier` / `UserProfile` 已归位 core 侧（`cognitive/delegation.py`、`hitl.py`、`cognitive/user_profile.py`）。

> **WorkflowEngine 取消/暂停终态单调性（E3，2026-08-11）**：**取消信号 = 任务状态**（`WorkflowState.status == CANCELLED`，不引入独立 cancelled 字段，避免 checkpoint 序列化问题）。`cancel()` 同步置 `status=CANCELLED`（权威终态，任何阶段恒可达）→ `owned.cancel()` 打断 in-flight LLM/planner future；executor 在四个 await 边界做 `_check_cancelled` 检查点，LLM 强中断、**工具调用 asyncio.shield 保护**（已开始工具完成、副作用不打断、结果弃用，孤儿工具登记到 `AgentInfra.orphan_tools` 共享容器、stop() 等 3s 后超时 cancel）；终态不可逆（planning/_init_exec 写 RUNNING 前检查终态）；CancelledError 传播链统一收尾（except 分支不 release、finally 唯一释放点，slot 释放前置防二次取消泄漏）；terminal event 只推一次（`WorkflowMailbox._terminal_pushed` 守卫）。

---

## 13. 并发任务全景

并发任务以 `asyncio.create_task` 为主（个别 fire-and-forget 归档用 `asyncio.ensure_future` 并登记 task 集合），分布在以下调用点：

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
| 1 | agent/_lifecycle_tools.py | embedding 预加载 | fire-and-forget + tracked |
| 2 | agent/agent.py | `_process_events` 主循环 | 常驻，stop 时 cancel |
| 3 | agent/agent.py | cognitive process (USER_INPUT) | `_current_task`，INTERRUPT cancel |
| 4 | agent/agent.py | cognitive process (SYSTEM_TICK) | 同上 |
| 5 | cognitive/finalize.py | `_after_response` | fire-and-forget，由 `_pending_after_response` 追踪 |
| 6 | adapter/tts_adapter.py | TTS 音频下载 + 转发（每句） | fire-and-forget，_tts_tasks[] 追踪 |
| 7 | idle_monitor.py | idle 监控 tick | 常驻，stop 时 cancel |
| 8 | memory/embedding_provider.py | embedding 懒加载 | insert 时 await (wait=True), search 时 fire-and-forget |
| 9 | personality/engine.py | expression 输出 | 纯 fire-and-forget，无追踪 |
| 10 | Module/tool/manager.py | SafetyPipeline 执行 | Pipeline 内部追踪 |
| 11 | agent/_lifecycle_task_system.py | `_poll_mailboxes()` | WorkflowEngine mailbox 500ms 轮询，常驻，stop 时 cancel |
| 12 | agent/agent.py | cognitive process (TASK_RESULT) | `_current_task` |
| 13 | agent/_lifecycle_persistence.py | `_db_health_loop()` | 数据库健康检查 120s 间隔，常驻，stop 时 cancel |
| 14 | memory/session.py | archive_append | `ensure_future` + `_archive_tasks` 登记 + 信号量上限 |
| 15 | input/ws_handler.py | per-connection state monitor | 常驻，disconnect 时 cancel |
| 16 | Module/workflow_engine/workflow_engine.py | sub-agent task submit | WorkflowEngine 内部追踪 |
| 17 | Module/workflow_engine/workflow_engine.py | sub-agent task resume | WorkflowEngine 内部追踪 |
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
  2. ✅ 后台任务 — WorkflowEngine v1: Planner/Executor/Evaluator DAG 编排 + Mailbox 轮询
  3. ✅ 流式中断 — CancelledError 防御 + FSM 幂等
  4. ✅ 多会话 — 批次 3 SessionManager 缓存池 + 活动指针 + resolve/switch/CRUD（v1 恒 default、v2 显式 conversationId）

待解决:
  4. 主动对话 — C7 Proactive section 已有规则 + 冷却机制。IdleMonitor 驱动待完善
  5. 记忆人格化 — C4 Memory section 已有可靠性规则（当前说法优先、不推测当事实），CharacterEngine Phase 2 深化
  6. 角色切换 — CharacterConfig 重载 + TTS voice 热切换
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../README.md) | 快速开始 + 完整文档导航 |
| [docs/testing.md](testing.md) | 测试指南（分层体系 + 命令） |
| [docs/readme-conventions.md](readme-conventions.md) | README 编写规范 |
| [CLAUDE.md](../CLAUDE.md) | AI 助手指令 + 项目地图 |
