# core/ -- 认知中枢

`core/` 是 Elysia/Agent 的认知中枢层（L1），位于无状态原子能力层（L0 `Module/`）之上，向下依赖 `Module/` 提供的 LLM/STT/TTS/Embedding/DB 等基础服务，向上为应用入口提供完整的 Agent 运行时。

## 目录

- [架构概览](#架构概览)
- [核心组件](#核心组件)
- [数据流](#数据流)
- [扩展指南](#扩展指南)

---

## 架构概览

### 三层定位

```
L0  Module/    无状态原子能力（LLM/STT/TTS/Embedding/DB/Config/...）
     ↑ 依赖
L1  core/      认知中枢 — Agent → AgentStatus → CognitiveLoop → PromptAssembler
     ↑ 使用
L2  core/memory/      记忆子系统 — SessionMemory / LongTermMemory
L2  core/tools/       工具插件 — filesystem / web / memory / time / ...
L2  core/personality/ 性格引擎 — CharacterEngine / CharacterReflector

顶层 paths.py   项目路径统一管理 — Module/ 和 core/ 均 import
```

### 核心组件关系

```
EventBus ← 用户输入/系统事件
    ↓
AgentStatus（语义状态接口：is_accepting_input / is_speaking / ...）
    ↓
CognitiveLoop (ReAct 调度 + inner_voice 状态机)
    ├── CognitivePhaseController（阶段转换：enter_thinking / enter_speaking / ...）
    ├── PromptAssembler (12 PromptSection → SystemPromptManager)
    ├── CharacterEngine (合并 EmotionEngine，关键词 → mood → expression)
    ├── MemoryConsolidator (对话 → LLM 提取 → LTM)
    ├── MemoryReflector (定期 LTM 去重)
    ├── TaskScheduler (Worker Pool → 工具执行)
    └── TTSStreamManager (分句 → 合成 → 播放)
```

### 启动顺序

```
Agent.__init__()
  ├── CharacterConfig.load()
  ├── AppConfig (ConfigLoader)
  ├── SessionMemory / LongTermMemory
  ├── CharacterEngine
  ├── PromptAssembler (12 sections)
  ├── ToolAdapter (tool plugin loading)
  ├── TaskScheduler
  ├── IdleMonitor
  └── OutputManager
Agent.start()
  ├── DB connect (MongoDB + Milvus, parallel)
  ├── Embedding preload (background, 10s timeout)
  ├── ToolAdapter.load_all()
  ├── IdleMonitor.start()
  ├── TaskScheduler.start()
  ├── EventBus loop start
  ├── TTS backend test (conditional)
  └── WorkflowEngine + DelegationPreparer（mailbox 轮询）
```

---

## 核心组件

`core/` 根目录 + 子包共约 165 个生产 `.py` 文件（排除 tests/__pycache__，口径不含 `core/tools/tests` 等测试文件）。其中 10 个为核心组件（详细描述），13 个为支撑模块（简表），另有 21 个子包（`agent/`, `cognitive/`, `memory/`, `personality/`, `text/`, `debug/`, `input/`, `desktop/`, `prompts/` 等，含 `tools/`/`schema/`）在各自 README 中详述。

### 核心组件详解（10 个）

#### Agent (`agent/` package, 9 files)

组装入口，持有所有子系统的引用。使用双 mixin 模式：`Agent(AgentLifecycle, AgentServer)`。`__init__` 创建 L0/L1/L2 全部组件；`AgentLifecycle.start()` 按序连接数据库、启动后台任务、加载插件；`AgentLifecycle.stop()` 按序 flush → cancel → close → save。暴露 `process_message()` 接收用户输入（支持纯文本和多模态 content 数组），`AgentServer.run_server()` 启动 WebSocket 服务，`AgentServer.run_repl()` 启动交互式命令行。管理 `RunState` 生命周期（STOPPED / RUNNING / STOPPING / RESTARTING），支持 `/restart` 热重载配置。共享类型（`RunState`, `_PendingMessage`, `_BackgroundTaskEntry`）在 `_types.py` 中。

**公开接口**：作为组合根，通过 **39 个同名 `@property`** 公开访问路径——`agent.session` / `agent.session_manager` / `agent.active_session_id` / `agent.pending_messages` / `agent.long_term` / `agent.message_repo` / `agent.session_repo` / `agent.memory_repo` / `agent.token_repo` / `agent.attachment_repo` / `agent.idempotency_repo` / `agent.tool_adapter` / `agent.output` / `agent.llm` / `agent.stt` / `agent.tts` / `agent.tts_manager` / `agent.stt_enabled` / `agent.tts_enabled` / `agent.stt_emotion_state` / `agent.config` / `agent.bus` / `agent.cognitive` / `agent.generation_registry` / `agent.desktop_bridge` / `agent.idle` / `agent.scheduler` / `agent.consolidator` / `agent.memory_reflector` / `agent.character_engine` / `agent.character_reflector` / `agent.character` / `agent.token_tracker` / `agent.prompt_recorder` / `agent.response_recorder` / `agent.user_name` / `agent.state` / `agent.started_at` / `agent.stt_ok`。状态写方法：`agent.request_shutdown()` / `agent.request_restart()`（设 `_state` 信号引导主循环退出，不做清理）。公开方法 `agent.purge_message_from_session(msg_id)`。跨模块消费禁止 `agent._xxx`，一律走公开接口（debug 深读用 `# noqa: SLF001` 豁免）。设计文档：[spec](../docs/superpowers/specs/archive/2026-08/2026-08-01-agent-public-readonly-interface-design.md)。

关键方法：`start()`, `stop()`, `process_message()`, `run_server()`, `run_repl()`, `switch_llm()`, `switch_stt()`, `switch_tts()`, `set_service()`, `request_shutdown()`, `request_restart()`, `purge_message_from_session()`

#### AgentStatus + CognitivePhaseController (`status/` package)

语义状态接口层，将底层 FSM 封装为对外只读的 `AgentStatus`（`is_accepting_input`、`is_completely_idle`、`is_speaking`、`should_suppress_vad_interrupt` 等 property）和对内写入的 `CognitivePhaseController`（`enter_thinking()`、`enter_executing()`、`enter_speaking()`、`finish_turn()`、`handle_interrupt()`）。实现类 `FSMBackedStatus` 和 `FSMBackedPhaseController` 基于 `AgentFSM`，但消费者只依赖接口。

`on_change(callback)` 支持事件驱动订阅替代轮询；`wait_until_idle(timeout)` 替代 `while fsm.state != "idle"` 忙等。

#### AgentFSM (`status/agent_fsm.py`)

有限状态机，`AgentStatus` / `CognitivePhaseController` 的底层实现。管理状态转换：`IDLE → THINKING → (EXECUTING → THINKING)* → SPEAKING → IDLE`，另有 `IDLE_BACKGROUND_TASK` 和 `INTERRUPTED`。`reset()` 强制回 IDLE 用于服务重启。不对外暴露——外部消费者通过 `agent.status` 查询。

#### EventBus (`monitoring/event_bus.py`)

优先级事件总线，底层为 `asyncio.PriorityQueue`。三种优先级：`INTERRUPT(0)` > `STATE_CHANGE(1)` > `NORMAL(2)`。事件类型：`USER_INPUT`、`SYSTEM_TICK`、`TASK_RESULT`、`INTERRUPT`、`STATE_CHANGE`。每个事件携带 `data: EventData`（discriminated union: `InterruptData | StateChangeData | UserInputData | SystemTickData | TaskResultData`），type 从 `data.event_type` 自动推导。`request_id` 由事件数据模型携带，用于链路追踪。`post()` 非阻塞投递，`next()` 阻塞取事件，`drain()` 排空队列（INTERRUPT 时保留 STATE_CHANGE 给前端）。

关键方法：`post()`, `next()`, `drain()`

#### CognitiveLoop (`cognitive/` package, 11 files)

ReAct 调度器 + inner_voice 状态机（`loop.py`），是认知流水线的核心执行引擎。使用 mixin 模式：`CognitiveLoop(CognitiveStreaming, CognitiveToolPhase, CognitiveFinalize)`——流式消费/事件分发、工具轮、终态收尾分别落在 `streaming.py` / `tool_phase.py` / `finalize.py`（CORE-MOD-P1-04）；`process()` 的轮前准备与异常收尾为 `_prepare_turn()` / `_handle_turn_exception()`。流式标签检测（mood + inner_voice）由 `StreamProcessor` (`core/text/stream_processor.py`) 独立处理——纯状态机，零 I/O，将标签解析结果以 `StreamEvent` 事件流输出。`_dispatch_stream_event()` 负责事件→副作用翻译（mood 更新、inner_voice 广播、TTS 分发、FSM 切换）。工具调用检测后最多 5 轮工具循环。`_handle_submit_task` 拦截后台任务委托。`_after_response()` 触发记忆沉淀、性格反思、MemoryReflector 检查、session 保存。`tools.py` 包含 `_execute_tools_directly`、`_submit_task_definition`、`_default_user_profile`。`timing.py` 包含 `Timer` context manager + `TimingClock` 请求级聚合（span 嵌套）。`delegation.py` 包含 `DelegationPreparer`（submit_task 前的委派准备）。`user_profile.py` 定义 `UserProfile` 用户画像模型。

关键方法：`process()`, `_think_loop()`, `_dispatch_stream_event()`, `cancel_current()`, `_after_response()`

#### CharacterEngine (`personality/engine.py`)

统一个性管理与情绪驱动。Mood 由 LLM 在主回复中通过 `<mood>` 标签自由生成（中文描述 + 强度），`StreamProcessor` 流式提取后通过 `_dispatch_stream_event()` 更新 `CharacterState.mood: str`。关键词检测 `_detect_mood()` 保留为 fallback（LLM 未输出 `<mood>` 时降级）。`analyze_user_stt()` 仅处理音频事件→表情，`analyze_assistant()` 只衰减 intensity 不重置 mood 文本。详见 [personality/README.md](personality/README.md)。

关键方法：`analyze_user_stt()`, `analyze_user()`, `analyze_assistant()`

#### MemoryConsolidator (`memory/consolidator.py`)

对话记忆提取：缓冲 6 条消息对 → LLM 提取 Fact/Preference/Event/Plan/Note → `ExtractedMemory` Pydantic 模型 → `ConsolidationCompare.j2` → `CompareAction` 判别联合（insert/update/merge/skip）→ LongTermMemory（Milvus）。`consolidate()` 每轮对话后调用，`flush()` 在 shutdown 时强制落盘未持久化的缓冲。提取 prompt 由 `core/prompts/ExtractMemory.j2` 运行时加载（`Path(__file__)` 相对路径）。比对失败时回退全 insert。

关键方法：`consolidate()`, `flush()`

#### PromptAssembler (`prompt/prompt_assembler.py`)

胶水层：创建 12 个 PromptSection 实例，注入 `SystemPromptManager`，委托 `MessageBuilder` 组装最终 `messages[]`。Section 顺序固定：F1 ConflictRules → F2 OutputFormat → F3 TTSMarkup → C2 Relationship → C7 Proactive → C6 SpeakingStyle → C1 Personality → C9 SelfPerception → C8 OwnLife → C3 Emotion → C4 Memory → C5 Sensory。`_refresh_personality_data()` 从 CharacterEngine 拉取 C1/C9 数据，支持多模态 content 数组和 TTS provider 注入。

关键方法：`build_messages()`, `_refresh_personality_data()`

#### IdleMonitor (`monitoring/idle_monitor.py`)

空闲监控 + 主动触发（位于 monitoring/ 子包）。每秒 tick，检查 `silence_timeout` 和 `time_of_day` 触发条件。`touch()` 重置交互计时器。触发时通过 EventBus 投递 `SYSTEM_TICK` 事件，Agent 注入自然开场白提示给 CognitiveLoop。语音唤醒词检测（openWakeWord）已集成于 `core/input/wakeword.py`（WakeWordDetector）+ `core/input/audio_buffer.py`（VAD 并行流式检测）；IdleMonitor 本身只读 `character.wake_words` 文本唤醒词。

关键方法：`start()`, `stop()`, `touch()`, `_check_proactive()`

#### TaskScheduler (`task/scheduler.py`)

Worker Pool（N=5），执行 AI 规划的工具子任务和后台任务。`dispatch_tool_calls()` 将 tool_call 列表封装为 `Task` 提交到优先队列。支持 retry（2 次，指数退避），timeout（60s），INTERRUPT 时级联 cancel（Future 双向绑定）。`_prune_loop()` 定期清理终态 task。checkpoint 持久化到 `data/scheduler_checkpoint.json`。

关键方法：`start()`, `stop()`, `dispatch_tool_calls()`, `cancel_all()`

### 支撑模块（12 个）

| 文件 | 职责 |
|------|------|
| `monitoring/` | 监控子包：`EventBus` 优先级事件总线、`IdleMonitor` 空闲检测+主动触发、`TokenTracker` token 统计+广播 |
| `cognitive/delegation.py` | `DelegationPreparer`（workflow_engine 分离 Task 9 迁入）：submit_task 委派预处理——LLM 筛选工具 + 防幻觉校验 + 约束估算 → `DelegationContext` |
| `hitl.py` | `EventBusHITLNotifier`（workflow_engine 分离 Task 10 迁入）：基于 EventBus 的 HITL 审批通知演示实现（ABC 在契约层 `Module/workflow_contract`） |
| `adapter/` | 适配器层：`LLMAdapter` 封装 `Module.llm` 后端，提供 `stream_think()` 流式接口，自动重试（429/500/502/503/504，指数退避 3 次）。`TTSStreamManager` 管理 TTS 句队列 + emoji 同步 + 门控播放。`ToolAdapter` 封装工具子系统生命周期（创建+加载+关闭）。`tool_call_parser` 流式 tool_call 合并/归一化。详见 [`adapter/`](adapter/) |
| `config/` | 配置子包：`character_config.py`（角色配置：从 YAML 加载角色名、wake words、emotion 阈值、proactive 参数、L1 Core personality 文本路径。`from_yaml()` 工厂方法）+ `app_config.py`（应用配置：`ConfigLoader` 加载 `config.yaml` + env override。包含 `AgentConfig`、`LLMConfig`、`MemoryConfig`、`TTSConfig` 等 dataclass） |
| `text/` | 文本流处理包：`tags.py` 标签注册表（PAUSE/STAGE_CN/META/CODE/MARKDOWN 全部定义 + emoji 收口）。`tokenizer.py` 结构感知切分（`StreamTokenizer`，跨 chunk 不变式 + 持有上界）。`splitter.py` 结构感知分句（`SpeechSplitter`，边界扫描跳过受保护 span，输出 SENTENCE + TEXT_DELTA）。`parts.py` 从句子 + 块交错构建 `TextPart` + content_type（session 存储）。`stream_processor.py` 协调器（tokenizer + splitter + 事件发射 + get_blocks + public_text 重建；META 路由注册表化，`META_TAGS.emit` + 枚举自动派生，未知 emit 降级 warning）。详见 [`text/`](text/) |
| `utils/` | 通用工具：`new_message_id()` 消息 ID 生成 + `set_request_id()`/`get_request_id()` ContextVar + `log_conversation()` 对话日志 + `content_text_from_content()`/`MessageObj` 文本提取（自 core/text 迁入）。详见 [`utils/`](utils/) |
| `debug/` | Debug 面板数据聚合包：`DebugInfo` dataclass 涵盖 FSM（含 mood）、IdleMonitor、Session、LTM、Token、Tool、Prompt/Response recorder、Consolidator（含 buffer）、MemoryReflector、Character Reflector、CharacterEngine 等所有子系统状态。`collect()` 遍历 Agent 聚合数据，前端 16 面板。`PromptRecorder`/`ResponseRecorder` 环形缓冲区（容量 20），捕获 LLM 调用完整 messages/tools/回复/inner_voice，供 debug 面板查看。详见 [`debug/`](debug/) |
| `monitoring/token_tracker.py` | Token 消耗追踪：`record()` 记录每次 LLM 调用的 token usage，`get_session_stats()` 聚合统计。支持 MongoDB 持久化和 OutputManager 广播 |
| `task/` | 任务子包：`task.py`（Task 数据模型：`Task` dataclass + `TaskType`/`TaskStatus` 枚举 + `asyncio.Future` 双向绑定。支持 retry 和序列化）+ `scheduler.py`（TaskScheduler Worker Pool，N=5，dispatch_tool_calls/cancel_all/prune_loop） |
| `schema/` | 共享类型包：`AgentState`、`EventData` 判别联合（5 种事件类型）、多模态 Part、`ToolCall`、`ExtractedMemory`/`CompareAction` 判别联合、`MemoryReflectorState`/`ReflectorActions`、消息、token、character/mood 状态等，含 `adapters/` 适配层。16 个 `.py` 文件（含 `__init__.py`）+ `adapters/` 适配层 |
| `version.py` | 版本号单一事实来源：`__version__` + `get_build_sha()`/`build_server_version()`（server_info.server_version 用） |
| `__init__.py` | 包标记，当前为空 |

### 子目录

| 子目录 | 说明 | 详细文档 |
|--------|------|----------|
| `core/agent/` | 组装入口：`Agent(AgentLifecycle, AgentServer)`，管理所有子系统引用和 RunState 生命周期 | [agent/README.md](agent/README.md) |
| `core/cognitive/` | 认知循环：ReAct 调度器 + 工具执行 + 计时（流式解析协调器在 core/text）。批次 3 增 `generation.py`（GenerationContext）+ `generation_registry.py`（generation 生命周期持久化） | [cognitive/README.md](cognitive/README.md) |
| `core/config/` | 配置子包：`character_config.py`（CharacterConfig，从 YAML 加载角色配置）+ `app_config.py`（AgentConfig，应用配置 dataclass） | [config/README.md](config/README.md) |
| `core/adapter/` | 适配器层：LLMAdapter + TTSStreamManager + ToolAdapter + tool_call_parser，封装 Module/ 后端供 core/ 层使用 | [adapter/README.md](adapter/README.md) |
| `core/prompt/` | System prompt 组装层：MessageBuilder + PromptAssembler（CORE-MOD-P1-02 从 adapter 迁入） | [prompt/README.md](prompt/README.md) |
| `core/memory/` | 记忆子系统：`SessionManager`（会话缓存池 + 活动指针 + resolve/switch/CRUD，批次 3 多会话）、`SessionMemory`（对话窗口）、`LongTermMemory`（向量检索）、`MemoryConsolidator`（提取+比对+写入）、`MemoryReflector`（定期去重） | [memory/README.md](memory/README.md) |
| `core/storage/` | 存储仓库层：Session/Message/Token/Memory/Generation/CharacterVersion/Attachment/DesktopCall/Idempotency 各 Repository + SecretStore + AttachmentGC（MongoDB + Milvus） | [storage/README.md](storage/README.md) |
| `core/providers/` | Provider 元数据与自定义 provider 持久化（`custom_providers.py` + `provider_registry.py`，CORE-MOD-P1-01 从 input 迁入） | [providers/README.md](providers/README.md) |
| `core/services/` | 服务运行时能力声明与广播（`capabilities.py`，CORE-MOD-P1-01 从 input 迁入） | [services/README.md](services/README.md) |
| `core/network/` | 网络运行时基础设施（`rate_limiter.py`，CORE-MOD-P2-01 从 input 迁入） | [network/README.md](network/README.md) |
| `core/input/` | 输入层：WebSocket + HTTP server（`server.py`/`ws_handler.py`/`ws_v2.py`/`session_routes.py`/`attachment_routes.py`/`voice_sessions.py`/`audio_buffer.py`/`sticker_registry.py`/`wakeword.py`/`tts_profile.py`/`debug_routes/`） | [input/README.md](input/README.md) |
| `core/personality/` | 性格引擎：`CharacterEngine`（4 层模型：L1 Core/L2 Self/L3 State/L4 Mood）、`CharacterReflector`（缓冲 50 对消息 → LLM 反思 → 更新 L3 State） | [personality/README.md](personality/README.md) |
| `core/task/` | 任务子包：`task.py`（Task 数据模型 + TaskType/TaskStatus 枚举 + Future 绑定）+ `scheduler.py`（TaskScheduler Worker Pool） | [task/README.md](task/README.md) |
| `core/text/` | 文本流处理管线：`tags`（标签注册表）+ `tokenizer`（结构感知切分）+ `splitter`（分句保护）+ `parts`（TextPart 构建）+ `stream_processor`（协调器） | [text/README.md](text/README.md) |
| `core/utils/` | 通用工具：`new_message_id()` 消息 ID 生成 + `set_request_id()`/`get_request_id()` ContextVar 请求追踪 + `log_conversation()` 对话日志 + `content_text_from_content()`/`MessageObj` 文本提取 | [utils/README.md](utils/README.md) |
| `core/debug/` | Debug 仪表板数据层：`DebugInfo`（16 panel 聚合）+ `PromptRecorder`/`ResponseRecorder`（LLM 交互录制） | [debug/README.md](debug/README.md) |
| `core/tools/` | 工具插件：filesystem、web、memory、time、reminder、vision 等。每个插件 `plugin.py` + `config.yaml` + `README.md` | [tools/README.md](tools/README.md) |
| `core/desktop/` | Desktop 工具桥：`bridge.py`（DesktopToolBridge——请求 → 幂等去重 → 定向投递 → 结果回传，批次 4 BE-041/043）+ 配套 `DesktopCallRepository` | -- |
| `core/tests/` | 测试套件：覆盖 Agent、CognitiveLoop、CharacterEngine、MemoryConsolidator 等核心组件的单元/集成测试 | -- |

---

## 数据流

### 对话流

```
WebSocket text → EventBus.post(USER_MESSAGE) → Agent._process_events()
  → FSM.transition(THINKING) → CognitiveLoop.process()
    → CharacterEngine.analyze_user() → 更新 mood
    → PromptAssembler.build_messages() → LLM.stream_think()
      → inner_voice 分流 → 分句 (SpeechSplitter)
        → TTSStreamManager.stream_sentence() → Output.broadcast()
          → FSM.transition(IDLE)
            → _after_response() → MemoryConsolidator + CharacterReflector + Session.save()
```

### 记忆流

```
对话 → MemoryConsolidator.buffer(user_msg, assistant_reply)
  → buffer 满 6 条 → LLM 提取 (ExtractMemory.j2)
    → ConsolidationCompare 比对已有记忆 (ConsolidationCompare.j2)
      → insert/update/merge/skip → LongTermMemory (Milvus)
        → MemoryReflector.increment_round() / increment_new_memories()
          → check_and_reflect() → KMeans 聚类 → LLM 去重 → merge/delete
```

### 性格演化流

```
对话 → CharacterReflector.buffer(user, assistant)
  → buffer 满 50 对 → LLM 反思 (CharacterReflector.j2)
    → CharacterEngine.consume_trigger("reflector", ...)
      → L3 State 更新 → awareness_cooldown 写入 CharacterState
        → C1/C9 模板数据刷新 → PromptAssembler._refresh_personality_data()
          → CharacterEngine.flush_compression() → L3→L2 压缩 (定期)
```

---

## 扩展指南

### 添加新工具

在 `core/tools/` 下创建目录：

```
core/tools/<name>/
├── __init__.py
├── plugin.py      # ToolPlugin 子类，定义 execute() 和 definitions()
├── config.yaml    # SafetyProfile 配置（权限级别、参数约束）
└── README.md
```

在 `core/tools/__init__.py` 的 `ALL_PLUGINS` 列表中注册插件类。Agent 启动时 `ToolAdapter` 通过 `ALL_PLUGINS` 自动加载。如需为工具提供依赖服务，在 `Agent._start_tool_context()` 中向 `ToolContext.services` 注入。

### 添加新 Prompt Section

1. 在 `Module/system_prompt/templates/<section>.j2` 创建 Jinja2 模板
2. 在 `Module/system_prompt/sections/<section>.py` 创建 `PromptSection` 子类，实现 `render()` 方法
3. 在 `core/prompt/prompt_assembler.py` 的 `sections` 列表中按序插入新 section
4. 如需动态数据，在 `_refresh_personality_data()` 中注入

模板格式见 `Module/system_prompt/templates/README.md`。

### 添加新记忆类型

1. 在 `consolidator.py` 的 EXTRACTION_PROMPT（`core/prompts/ExtractMemory.j2`）中添加新类型的提取示例
2. 在 `ConsolidationCompare.j2` 中补充新类型的比对规则
3. 在 `memory/long_term.py` 的 `search()` 和 rerank 逻辑中按需调整权重
4. 如有新的向量字段需求，更新 Milvus collection schema

### 生命周期钩子

Agent 生命周期中的关键扩展点：

- **`Agent.__init__()`** — 创建组件，注入依赖。新组件在此处初始化并传入 CognitiveLoop
- **`Agent.start()`** — 连接 DB、启动后台任务。`_start_*()` 系列按序执行，新子系统在此处 start
- **`Agent._start_persistence()`** — 恢复持久化状态。`restore_state()` 在此处调用，新组件如需恢复 session 状态在此注入
- **`Agent.stop()`** — flush → cancel → close → save。新组件在此处按序清理
- **组件级钩子**：`restore_state()`（恢复 CharacterState + MemoryReflectorState）、`flush()`（落盘缓冲）、`check_and_reflect()`（定期触发检查）

### 文件索引

| 文件 | 行数 | 类型 |
|------|------|------|
| `agent/agent.py` | 857 | 核心组件 |
| `status/agent_fsm.py` | 83 | 核心组件 |
| `monitoring/event_bus.py` | 76 | 核心组件 |
| `cognitive/loop.py` | 1478 | 核心组件 |
| `personality/engine.py` | 469 | 核心组件 |
| `memory/consolidator.py` | 366 | 核心组件 |
| `prompt/prompt_assembler.py` | 186 | 核心组件 |
| `monitoring/idle_monitor.py` | 133 | 核心组件 |
| `task/scheduler.py` | 510 | 核心组件 |
| `config/character_config.py` | 157 | 支撑模块 |
| `config/app_config.py` | 122 | 支撑模块 |
| `text/` | 6 文件 (1519) | 支撑模块 |
| `utils/message_id.py` | 30 | 支撑模块 |
| `debug/` | 4 文件 (631) | 支撑模块 |
| `utils/log_setup.py` | 215 | 支撑模块 |
| `monitoring/token_tracker.py` | 218 | 支撑模块 |
| `task/task.py` | 222 | 支撑模块 |
| `input/debug_routes/` | 7 文件 (1075) | 支撑模块 |
| `schema/` | 目录（16 .py + adapters/） | 支撑模块 |
| `desktop/bridge.py` | 488 | 支撑模块 |
| `version.py` | 54 | 支撑模块 |
| `__init__.py` | 0 | 包标记 |
