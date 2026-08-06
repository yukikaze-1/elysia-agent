# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Elysia/Agent is a Python 3.11+ AI companion application. The codebase has three top-level packages:

- **`Module/`** — backend-agnostic library for AI companion infrastructure: LLM, STT, TTS, embedding, vector/document databases, output channels, config, logging, system prompt engine, tool framework.
- **`core/`** — the Elysia Agent application: `Agent`, `CognitiveLoop`, `CharacterEngine`, `CharacterReflector`, `MemoryConsolidator`, `MemoryReflector`, `SessionMemory`, `LongTermMemory`, `IdleMonitor`, task scheduler, debug info, character config.
- **`frontend_contract/`** — 前后端契约单源（唯一类型来源 + 生成器 + 门禁）：可执行 Pydantic 契约模型（WS/REST/Debug）→ `gen_ts` 发射器 → `frontend/src/types/contract.generated.ts`（TS 只读快照）。后端发送点构造契约模型，`make check-types` 做漂移门禁。零依赖（仅 pydantic + stdlib）。

Plain Python（无 `pyproject.toml` 等构建系统）。前端 Vue 3 + Vite + TypeScript（无框架 → Vite 构建步骤）。`Makefile` 提供常用任务入口。

## Running tests

### 分层运行

```bash
# 日常开发 — 秒级（unit + service）
make test-quick
# 或: pytest -m "unit or service" -q -x --tb=short --ignore=Module/workflow_engine/tests

# 提交前 — 排除 slow（~5min）
make test-normal
# 或: pytest -m "not slow" -q --tb=short --ignore=Module/workflow_engine/tests

# 全量 — 推之前（~22min）
make test-full
# 或: pytest -q

# 导入风格门禁（禁相对导入，秒级）
make lint
# 或: ruff check core/ Module/ frontend_contract/ --exclude __pycache__
```

### 按模块运行

```bash
# Personality engine tests
python -m pytest core/personality/tests/ -v

# System prompt tests
python -m pytest Module/system_prompt/tests/ -v

# Core tests
python -m pytest core/tests/ -v

# WorkflowEngine tests
python -m pytest Module/workflow_engine/tests/ -v

# Agent lifecycle tests
python -m pytest core/agent/tests/ -v

# Config tests
python -m pytest core/config/tests/ -v

# Monitoring tests
python -m pytest core/monitoring/tests/ -v

# Task tests
python -m pytest core/task/tests/ -v

# 其他模块
python -m pytest Module/llm/tests/ -v
python -m pytest Module/tts/tests/ -v
python -m pytest Module/stt/tests/ -v
python -m pytest Module/embedding/tests/ -v
python -m pytest Module/output/tests/ -v
python -m pytest Module/config/tests/ -v
python -m pytest Module/system_prompt/tests/ -v
python -m pytest Module/logger/tests/ -v
python -m pytest Module/database/vectordb/tests/ -v
python -m pytest Module/database/documentdb/tests/ -v
python -m pytest Module/tool/tests/ -v
python -m pytest frontend_contract/tests/ -v
```

### pytest markers

| marker | 标准 | 日常使用 |
|--------|------|----------|
| `unit` | 纯函数/类，无 I/O，无 Agent | `-m "unit or service"` |
| `service` | 单模块 + mock，不启动 Agent | `-m "unit or service"` |
| `integration` | 需真实 DB 或外部 API | `-m "not slow"` |
| `slow` | 完整管线或 >1s | 全量时才跑 |

## Architecture

### Module/ layer (Protocol-Registry-Backend pattern)

```
Module/<service>/
├── protocol.py    # ABC (LLMBackend, STTBackend, TTSBackend, etc.)
├── registry.py    # Factory + _BACKENDS dict ("provider/model" → backend class)
├── <backend>.py   # Concrete implementation(s)
├── config.py      # Dataclass config using Module.config.ConfigField
├── __init__.py    # Re-exports Backend ABC, Factory, Config
└── __main__.py    # Integration smoke test
```

### core/ layer (Application)

```
core/
├── agent/                 # Agent (package): AgentLifecycle mixin + AgentServer mixin + _types + AgentStatus + FSM + 4 lifecycle mixins
│   ├── agent.py           #   Agent.__init__ + event handling + process_message + 27 公开 @property + background tasks
│   ├── lifecycle.py       #   AgentLifecycle: start()/stop() 编排 + _STARTUP_STEPS + _SHUTDOWN_STEPS + 3 flush
│   ├── server.py          #   AgentServer: run_server/run_repl + switch_*/get_wake_words + banner
│   ├── _types.py          #   RunState, _PendingMessage, _BackgroundTaskEntry
│   ├── _lifecycle_persistence.py  #   持久化层启停 (MongoDB/Milvus/session/repos)
│   ├── _lifecycle_services.py     #   LLM/TTS/Output/TokenTracker 启停
│   ├── _lifecycle_tools.py        #   ToolAdapter/Scheduler/IdleMonitor/event loop 启停
│   ├── _lifecycle_task_system.py  #   WorkflowEngine/DelegationPreparer/mailbox 启停
│   └── tests/
├── status/                # AgentStatus 接口层：protocol.py（ABC）+ fsm.py（FSM-backed 实现）+ agent_fsm.py（底层状态机）
├── cognitive/             # CognitiveLoop (package): CognitiveTools mixin + timing + TurnPhase
│   ├── loop.py            #   CognitiveLoop(CognitiveTools): process + ReAct 主循环 + TurnPhase 追踪（_build_turn_prompt / _stream_llm_response / _execute_tool_round / _finalize_turn / _dispatch_stream_event）
│   ├── tools.py           #   CognitiveTools mixin: _execute_tools_directly + _handle_submit_task + _gate_tool_calls
│   ├── timing.py          #   Timer + TimingClock latency tracking
│   ├── generation.py      #   GenerationContext: generation_id/assistant_message_id/conversation_id 预分配贯通
│   └── generation_registry.py # GenerationRegistry: generation 生命周期持久化（register/mark 终态 + stale 清理，Mongo 离线 no-op）
├── adapter/               # Adapter layer: wraps Module/ backends for core/ consumption
│   ├── llm_adapter.py     #   LLMAdapter: tool-calling + retry + streaming + hot-switch
│   ├── tts_adapter.py     #   TTSStreamManager: sentence queuing + emoji sync + gated playback + async set_enabled 禁用兜底（per-task flush_on_cancel）
│   ├── tool_call_parser.py #  merge/normalize streaming tool_call deltas
│   ├── tool_adapter.py    #   ToolAdapter: tool subsystem lifecycle + ToolExecutor delegation
│   ├── message_builder.py #   MessageBuilder: system prompt + history → LLM API messages[]
│   └── prompt_assembler.py #  PromptAssembler: creates 12 sections → SystemPromptManager → MessageBuilder
├── monitoring/             # Monitoring (package): event bus + idle monitor + token tracking
│   ├── event_bus.py       #   EventBus: pub/sub for agent internal events
│   ├── idle_monitor.py    #   IdleMonitor: wake-word detection + proactive triggers
│   ├── token_tracker.py   #   TokenTracker: usage counting + cost estimation
│   └── tests/
├── text/                  # Text stream processing (package): 标签注册表 + tokenizer + splitter + parts + 协调器
│   ├── tags.py            #   标签注册表：PAUSE/STAGE_CN/META/CODE/MARKDOWN 全部定义 + emoji 收口（未来加标签 = 加一行注册）
│   ├── tokenizer.py       #   StreamTokenizer: 结构感知切分，只吐完整 token（跨 chunk 不变式 + 持有上界）
│   ├── splitter.py        #   SpeechSplitter: 结构感知分句，边界扫描跳过受保护 span，输出 SENTENCE + TEXT_DELTA
│   ├── parts.py           #   build_parts/rebuild_public_text: 句子 + 块交错 → TextPart + content_type
│   ├── stream_processor.py #   StreamProcessor: 协调器（tokenizer + splitter + 事件发射 + get_blocks + public_text 重建；META 路由注册表化，未知 emit 降级 warning）
│   └── __init__.py         #   re-export（content_text_from_content/MessageObj 已迁 core/utils/text_utils）
├── debug/                 # Debug dashboard (package): DebugInfo aggregation + prompt/response recorders
│   ├── info.py            #   DebugInfo: aggregates agent state (16 panels) for /debug dashboard
│   ├── prompt_recorder.py #   PromptRecorder: ring buffer (20) for LLM prompts
│   └── response_recorder.py # ResponseRecorder: ring buffer (20) for LLM responses
├── personality/           # CharacterEngine: 4-layer personality model (L1→L2→L3→L4)
│   ├── engine.py          #   CharacterEngine: L1 Core (file, immutable) → L2 Self (session, slow evolution) → L3 State (session, Reflector-driven) → L4 Mood
│   ├── reflector.py       #   CharacterReflector: buffers 50 pairs → LLM reflection → submit to engine
│   ├── awareness.py       #   AwarenessTrigger: C9 觉察判定 + cooldown 管理
│   ├── compression.py     #   CompressionManager: L3→L2 自我压缩 + C6 联动编辑
│   ├── emotion.py         #   EmotionEngine: LLM mood tag 解析 + keyword 回退
│   ├── _utils.py          #   内部工具函数
│   └── tests/
├── memory/
│   ├── session_manager.py  # SessionManager: 会话缓存池 + 活动指针 + resolve/switch/CRUD（批次 3 多会话）
│   ├── session.py          # SessionMemory: deque (maxlen), MongoDB persistence (30d TTL), fire-and-forget archive; 专用类型字段 character_state/service_state 经 save/load 同步
│   ├── long_term.py        # LongTermMemory: Milvus-backed vector search + reranking; count-based _evict_overflow; async mark_absorbed with Milvus cleanup
│   ├── embedding_provider.py # EmbeddingProvider: 嵌入后端懒加载 + 超时 + OpenAI/HuggingFace fallback
│   ├── memory_reflector.py # MemoryReflector: periodic LTM dedup (round/new-memory triggers, KMeans clustering + LLM review)
│   └── consolidator.py     # MemoryConsolidator: buffers 6 msgs → LLM extracts + compares → insert/update/merge/skip
├── storage/                # Storage layer: MongoDB/Milvus repositories
│   ├── session_repo.py     #   SessionRepository: session CRUD + MongoDB TTL
│   ├── message_repo.py     #   MessageRepository: message archive append-only log
│   ├── token_repo.py       #   TokenRepository: token usage statistics
│   ├── memory_repo.py      #   MemoryRepository: LTM Milvus operations
│   ├── generation_repo.py  #   GenerationRepository: generation 生命周期 upsert/query（BE-024）
│   ├── character_version_repo.py # CharacterVersionRepository: L2/C6 version history
│   ├── attachment_repo.py  #   AttachmentRepository: attachments 集合（fail-closed，BE-070——Mongo 离线 raise StorageUnavailableError → 503；refs/ref_count + 软删 status/deleted_at）
│   └── attachment_gc.py    #   AttachmentGC: 启动兜底扫描四规则（软删超 24h 物理删 / active 文件缺失软删 / 未关联超 7 天软删 / 磁盘孤儿删文件，限四 kind 目录，BE-071）
├── config/                 # Config sub-package: character_config + app_config
│   ├── character_config.py    # CharacterConfig: loads YAML + L1 Core personality text
│   ├── app_config.py          # AgentConfig: YAML-loaded app configuration
│   └── tests/
├── utils/                  # Utility functions: message_id + log_setup + text_utils
├── task/                   # Task sub-package: task model + scheduler
│   ├── task.py             # Task dataclass + TaskType/TaskStatus enums + Future binding
│   ├── scheduler.py        # TaskScheduler: worker pool for ReAct tool calls (5 workers)
│   └── tests/
└── tools/                 # Tool plugins (filesystem, web, memory, time, reminder, etc.)
```

### frontend_contract/ layer (契约层)

```
frontend_contract/
├── base.py                 # ContractModel 基类 + TsNull marker + wire_key + wire_dict 序列化
├── ws_message.py           # WS 消息契约：ServerMessage 判别联合（39 具名成员，批次 6 语音/TTS 播放契约）+ 多条 Client→Server + 支持类型（ContentPart 6 类判别联合 + AttachmentRef + V2TextMessage.attachments/stickerId + ErrorMessage.requestId + ServerInfoMessage.build_sha/media_upload，批次 7）
├── rest.py                 # REST 业务契约：providers / images / messages / 多会话 / Desktop / 附件与表情（AttachmentInfo/AttachmentUploadResponse/AttachmentSignResponse/StickerEntry/StickerListResponse，批次 7）等 40 模型
├── debug.py                # Debug API 契约：/debug/api 响应（DebugData 16 面板等 38 模型）
├── gen_ts.py               # TS 发射器：BaseModel → contract.generated.ts（拓扑排序 + 同名安全网）
├── __main__.py             # python -m frontend_contract：动态收集子模块 → 生成 / --check 漂移检测
└── tests/                  # 契约建模测试（marker=unit，无 I/O）
```

前后端契约唯一类型来源：可执行 Pydantic 模型 → `gen_ts` 生成 `frontend/src/types/contract.generated.ts`
（TS 只读快照）。后端发送点构造契约模型 + `wire_dict()` 序列化；`make gen-types` 重新生成、
`make check-types` 漂移门禁（CI 强制）。零依赖（仅 pydantic + stdlib），不 import core/ Module/。

### Character config (`character/`)

```
character/
└── <name>/                  # One directory per character
    ├── config.yaml           # YAML config (name, wake_words, emotion thresholds, proactive, etc.)
    ├── personality.md        # L1 Core personality text (natural language, loaded at startup)
    └── reference/            # GPT-generated reference materials (optional)
```

### System prompt engine (`Module/system_prompt/`)

12 sections rendered in fixed order by `SystemPromptManager`:

```
F1 ConflictRules    → Priority rules + safety borders
F2 OutputFormat     → inner_voice protocol + task priority
F3 TTSMarkup        → TTS synthesis markers + usage limits
C2 Relationship     → Relationship view (4-stage + boundaries)
C7 Proactive        → When to speak first + cooldown mechanism
C6 SpeakingStyle    → Speaking style (5-mode switching + praise/flirt/address)
C1 Personality      → L1 Core + L2 Self (data-driven)
C9 SelfPerception   → Self-awareness (AwarenessTrigger conditional)
C8 OwnLife          → Own activities (pass-through, may be empty)
C3 Emotion          → Mood + 8 emotional response rules
C4 Memory           → Long-term memory recall + reliability rules
C5 Sensory          → Current environment context
```

Templates at `Module/system_prompt/templates/` — format convention: XML wrapper + Markdown body. See `Module/system_prompt/templates/README.md` for full format docs.

## Module index

| Module | Purpose | Backends |
|--------|---------|----------|
| `Module.llm` | LLM chat/streaming | `deepseek`, `openai`, `ollama`, `mimo`, `gemini`, `custom` |
| `Module.stt` | Speech-to-text | `sensevoice`, `custom` |
| `Module.tts` | Text-to-speech | `gpt-sovits`, `minimax`, `custom` |
| `Module.embedding` | Text vectorization | `huggingface`, `openai` |
| `Module.output` | Multi-channel output | `ConsoleChannel`, `WebSocketChannel` |
| `Module.config` | Config infrastructure | `ConfigLoader` (YAML + env + validation) |
| `Module.logger` | Logging factory | `LoggerFactory` (plain + JSON, rotating) |
| `Module.system_prompt` | System prompt engine | 12-section orchestration + templates |
| `Module.workflow_engine` | 后台任务引擎（原 `task_system`）：ReAct 编排/规划/执行/子智能体。主类 `WorkflowEngine`；跨边类型经契约层 `Module/workflow_contract` | `orchestrator` + `planner` + `executor` + `sub_agent` |
| `Module.workflow_contract` | 跨边契约层（core ↔ workflow_engine 唯一类型来源）：类型/枚举/协议 ABC/无状态纯函数/AgentInfra 运行时注入容器，零业务逻辑 | `schema` + `infra` + `utils` |
| `frontend_contract` | 前后端契约单源（唯一类型来源 + 生成器 + 门禁）：可执行 Pydantic 契约模型（WS/REST/Debug）+ `gen_ts` 发射器 → `contract.generated.ts`，`make check-types` 漂移检测，零依赖（仅 pydantic + stdlib） | `base` + `ws_message` + `rest` + `debug` + `gen_ts` |
| `core.schema` | Shared Pydantic types: EventData (discriminated union, 5 event types), ToolCall, ExtractedMemory/CompareAction (discriminated union), ReflectorActions, MemoryType, message types, mood, etc. `part.py`：Part 判别联合 6 类（含 StickerPart，批次 7）、Image/File/Audio/Video Part 带 `attachment_id`、FileMime 补值（pdf/zip/markdown） | |
| `Module.database.vectordb` | Vector DB (ANN search) | `milvus` |
| `Module.database.documentdb` | Document DB | `mongodb` |
| `Module.database.graphdb` | Graph DB | stub only |
| `Module.bench` | Latency benchmarking | Covers LLM/TTS/STT |
| `core.input` | Input layer: WebSocket + HTTP server | `server.py` (glue), `ws_handler.py`, `capabilities.py` (嵌套 per-service 能力声明), `api_routes.py`, `attachment_routes.py` (附件签名下载 + 签发，BE-070), `session_routes.py` (多会话/历史/搜索/资源/状态/pending REST), `voice_sessions.py` (三模式语音状态机：普通录音/实时语音/输入法转写，BE-060~062), `sticker_registry.py` (内置表情注册表，BE-071), `audio_buffer.py` (VAD 分段 + STT 提交), `debug_routes/` (package, 7 modules) |
| `Module._common` | Shared utility layer | env, registry helpers, test helpers |

## Config system

Hierarchical: `env var > YAML file > dataclass default`. Main config at `config.yaml`.

- `agent.character` — path to character config (e.g. `character/elysia/config.yaml`)
- `agent.user_name` — user name injected into system prompt
- `llm.model` — format `<provider>/<model>` (e.g. `deepseek/deepseek-v4-flash`)
- `tts.model` — TTS provider (e.g. `minimax/speech-2.8-hd`). Voice mapping in `Module/tts/minimax_voices.yaml`
- `stt.model` — STT provider (e.g. `sensevoice/sense-voice`)

## Key patterns

- **Async-first**: All I/O is async. Backends manage their own `httpx.AsyncClient` or `openai.AsyncOpenAI`.
- **Logger injection**: `Factory.create()` auto-detects caller's module via `inspect`.
- **env var precedence**: API keys and base URLs from `os.getenv()`, not YAML.
- **MongoDB**: motor async driver. `insert_one/insert_many` pass `dict()` copy to avoid motor mutating callers' dicts.
- **Session state**: 批次 3 起由 `SessionManager`（`core/memory/session_manager.py`）管理会话缓存池 + 活动指针（`agent.session_manager`；`agent.session` 仍指活动会话）。多会话路由：`agent.resolve_session(conversation_id, protocol_version)`——v1 恒 `"default"`、v2 显式 conversationId → 该会话、v2 缺省 → 活动会话；`activate_session` WS 消息切 active + 重发回放。运行时状态用 SessionMemory 的**专用类型字段**（`character_state` / `service_state`；反射器状态非字段，由 `session.save()` 经 `save_state()` 派生），经 `session.save()` 同步、`load()` 恢复（MongoDB 30d TTL + JSONL fallback）。无 `set_param/get_param` 通用接口——新增状态仿 `character_state` 加字段 + save/load 两侧同步。`service_state`（服务运行时状态：当前模型 + STT/TTS 启用模式，enabled 三态 `bool | None`，None 回退 config）由 `agent.set_service` / `switch_*` 写、启动时经 `_apply_service_state` 应用。
- **inner_voice protocol**: Every reply has one `<inner_voice>` block (1-2 sentences: current feeling + response intent, no reasoning chains).
- **Project paths**: `from paths import ProjectPath, get_project_root` — 所有项目文件系统路径的唯一定义点。`ProjectPath.X.resolve()` 返回基于 `ELYSIA_ROOT` 环境变量（fallback 为项目根目录）的绝对 `Path`。
- **AgentStatus**: `agent.status`（`AgentStatus`）提供语义状态查询（`is_accepting_input` / `is_speaking` / `should_suppress_vad_interrupt` 等）；`agent.phases`（`CognitivePhaseController`）驱动阶段转换。底层 `AgentFSM` 不对外暴露。设计文档：[spec](docs/superpowers/specs/archive/2026-07/2026-07-30-agent-status-interface-design.md)。
- **Agent 公开接口**: `Agent` 作为组合根，通过 31 个同名 `@property` 公开子系统访问路径（`agent.session` / `agent.long_term` / `agent.tool_adapter` / `agent.output` / `agent.llm` / `agent.stt` / `agent.tts` / `agent.config` / `agent.bus` 等，可写性由子系统 API 自管）。状态写方法：`agent.request_shutdown()` / `agent.request_restart()`（设 `_state` 信号引导主循环退出，不做清理）；`agent.set_service(service, enabled)`（STT/TTS 后端模式开关）；`agent.purge_message_from_session(msg_id)`。跨模块消费禁止 `agent._xxx`，一律走公开接口；debug 层深度内省用 `# noqa: SLF001` 显式豁免。设计文档：[spec](docs/superpowers/specs/2026-08-01-agent-public-readonly-interface-design.md)。

## Prompt files (`core/prompts/`)

模板（`.j2`），PascalCase 命名。多数活跃模板使用 `str.replace()` 注入变量（因 JSON 示例中 `{}` 与 `.format()` 冲突）；`AwarenessJudge.j2` 和 `SpeakingStyleEdit.j2` 使用 Jinja2 宏，通过 `PromptManager.render_macro()` 渲染。通过 `Path(__file__)` 相对路径加载，不受 cwd 影响。详见 `core/prompts/README.md`。

```
core/prompts/
├── ExtractMemory.j2          # 对话提取记忆 (consolidator.py 加载)
├── ConsolidationCompare.j2   # 新记忆 vs 已有记忆比对 (consolidator.py 加载)
├── MemoryReflector.j2        # 定期深度去重 (memory_reflector.py 加载)
├── CharacterReflector.j2     # 性格反思 (reflector.py 加载，10 上下文变量)
├── PersonalityCompression.j2 # L3→L2 自我压缩 — LLM 编辑模式 (engine.py 加载)
├── ActiveSpeak.j2            # 主动搭话 prompt（参考模板，不活跃）
├── Amygdala.j2               # 情感分析 prompt（参考模板，不活跃）
├── AwarenessJudge.j2         # C9 觉察判定 prompt（活跃，Jinja2 宏渲染）
├── SpeakingStyleEdit.j2      # C6 说话风格编辑 prompt（活跃，Jinja2 宏渲染）
├── Utils.j2                  # 共享 Jinja2 工具宏
├── MacroReflector.j2         # 旧项目参考（不活跃）
├── MicroReflector.j2         # 旧项目参考（不活跃）
├── README.md                 # prompt 命名规范 + 渲染方式
└── ref/                      # GPT 生成的原始版本 + 需求文档（snake_case.md）
```

Prompt 需求文档：`core/prompts/ref/` 目录下的 `*-requirements.md` 和 `优化后的prompt-requirements.md`。

## Frontend (`frontend/`)

Vue 3 + Vite + Pinia + TypeScript。开发时通过 Vite 代理（:5173 → :8765），生产构建直接输出到 `static/` 由 aiohttp serve。

```
frontend/
├── index.html              # Chat 页面 HTML 入口
├── debug.html              # Debug 页面 HTML 入口（Vite 多页构建）
├── vite.config.ts          # Vite 配置（代理 :8765 + 多入口构建）
├── package.json
├── tsconfig.json
└── src/
    ├── main.ts             # Chat 应用入口（Pinia + marked + highlight.js）
    ├── App.vue             # 根组件（三栏布局 + 全局快捷键）
    ├── styles/variables.css     # 全局 CSS 变量（从旧版 chat.css 提取）
    ├── styles/message-layout.css # 共享消息布局样式（ChatMessage + TypingIndicator）
    ├── types/protocol.ts   # 20+ WebSocket 消息类型定义
    ├── utils/escape.ts     # HTML 转义工具
    ├── utils/format.ts     # 数字格式化（fmt: 1.0k）
    ├── stores/
    │   ├── connectionStore.ts  # WebSocket 连接生命周期
    │   ├── chatStore.ts        # 消息列表 + delta 流式追加 + TTS 排队
    │   ├── audioStore.ts       # TTS 缓冲/播放 + 麦克风
    │   ├── settingsStore.ts    # 设置项 + localStorage 同步 + 后端模式 toggle（sttEnabled/ttsEnabled → set_service）
    │   ├── toolsStore.ts       # 工具插件列表
    │   └── debugStore.ts       # Debug 仪表盘：轮询生命周期 + Tab 切换 + 分页状态
    ├── composables/
    │   ├── useWebSocket.ts  # WS 消息路由（替代旧版巨型 switch）
    │   ├── useAbortController.ts  # AbortController 封装
    │   ├── useImageUpload.ts      # 图片上传管理
    │   └── useLightbox.ts   # 全局图片灯箱（任意组件 open(url) 即可全屏查看）
    ├── components/
    │   ├── layout/AppLayout.vue           # 三栏布局
    │   ├── character/                     # 左侧角色面板
    │   │   ├── CharacterPanel.vue
    │   │   ├── AvatarDisplay.vue         # Live2D / 静态图
    │   │   └── CharStatus.vue            # 状态点 + 心情 + 模型名
    │   ├── chat/                          # 消息渲染
    │   │   ├── MessageList.vue           # 消息列表（TransitionGroup 动画）
    │   │   ├── ChatMessage.vue           # 单条消息
    │   │   ├── ChatBubble.vue            # 气泡内容（文本/markdown/code）
    │   │   ├── ToolCard.vue              # 工具调用卡片
    │   │   ├── InnerVoice.vue            # 内心独白气泡
    │   │   ├── TokenLabel.vue            # Token 统计
    │   │   ├── TimeDivider.vue           # 时间分隔符
    │   │   └── TypingIndicator.vue       # 打字指示器
    │   ├── input/
    │   │   ├── InputBar.vue              # 输入框 + 附件 + 麦克风 + 发送
    │   │   └── PreviewBar.vue            # 图片预览条
    │   ├── panels/
    │   │   ├── SettingsPanel.vue         # 设置面板
    │   │   └── ToolsPanel.vue            # 工具管理面板
    │   ├── ui/
    │   │   ├── ToggleSwitch.vue          # 开关组件
    │   │   ├── ApprovalDialog.vue        # HITL 审批弹窗
    │   │   └── ModelSelector.vue         # 模型选择下拉框
    │   └── shared/
    │       ├── Lightbox.vue              # 图片灯箱
    │       └── CodeBlock.vue             # 代码块（highlight.js + 复制）
    └── debug/                            # 独立 Debug 应用（Vite 多入口）
        ├── main.ts                       # 入口 + Pinia
        ├── App.vue                       # Tab shell（概览 | 数据库 | 任务）+ pause/copy
        ├── types.ts                      # DebugData 等 TS 接口定义
        └── components/
            ├── OverviewTab.vue           # 16 面板 2 列 grid（poll /debug/api）
            ├── DatabaseTab.vue           # 容器，切换时加载
            ├── LtmPanel.vue              # LTM CRUD（搜索/筛选/翻页/新增/编辑/删除/批量/清空）
            ├── SessionPanel.vue          # 会话管理（列表/详情/删除/清空）
            ├── ArchivePanel.vue          # 消息归档（搜索/筛选/翻页/编辑/删除/清空）
            ├── TasksTab.vue              # SSE EventSource + 自动重连 + 活跃/完成区
            ├── TaskCard.vue              # 任务卡片：状态徽章 + 进度条 + vis-network DAG
            ├── shared/
            │   ├── PaginationBar.vue     # 分页导航（offset 语义，跳转，perPage 选择）
            │   ├── ConfirmModal.vue      # 危险操作确认弹窗
            │   └── ExpandableText.vue    # 文本截断 + 点击展开
            └── modals/
                ├── PromptViewer.vue      # LLM Prompt 详情：可折叠消息 + 模板 + 工具列表
                ├── ResponseViewer.vue    # LLM Response 详情：inner_voice + raw text
                ├── VersionHistory.vue    # L2/C6 版本历史 + 回滚
                └── ImageLightbox.vue     # 图片全屏灯箱
```

**开发：**
```bash
cd frontend && npm install && npm run dev
# → Vite dev server :5173（自动代理 WS + API 到 :8765）
```

**构建：**
```bash
cd frontend && npm run build
# → 构建产物直接输出到 static/（index.html + debug.html + assets/）
```

### Debug page panels

**Overview tab**: 16 panels (2-column grid) — System, Memory, Latency, Process Memory, Scheduler, Token, LLM Prompt (→ PromptViewer), LLM Response (→ ResponseViewer), Recent Session (→ ImageLightbox), LTM Recent, Tools, Tool History, Consolidation, Memory Reflector, Character Reflector, CharacterEngine (→ VersionHistory).

**Database tab**: LtmPanel (Milvus CRUD: search/filter/paginate/create/edit/delete/batch/clear), SessionPanel (MongoDB sessions: list/detail/delete/clear), ArchivePanel (message archive: search/role-filter/paginate/edit/delete/clear). All destructive ops gated by ConfirmModal.

**Tasks tab**: SSE stream from `/debug/api/tasks/stream`, vis-network DAG (hierarchical TB, status-colored nodes), auto-reconnect with exponential backoff, active/completed task sections.

**Shared UI**: PaginationBar (offset semantics, perPage 50/100/200, page jump), ConfirmModal (Teleport, confirm/cancel emits), ExpandableText (truncate/expand toggle).

**Stores**: `debugStore.ts` — Pinia (Composition API): poll lifecycle, activeTab switching, pause/resume, LTM/Archive pagination state.

### Server routing (`core/input/` — `server.py` + `ws_handler.py` + `capabilities.py` + `api_routes.py` + `attachment_routes.py` + `session_routes.py` + `debug_routes/` package)

`/` → `static/index.html`, `/debug` → `static/debug.html`, `/ws` → WebSocket, `/health`（liveness 恒 200）`/ready`（readiness，依赖不 ok → 503）`/shutdown` `/restart` `/api/chat` `/api/messages` `/api/images` `/api/providers` `/tools` `/tools/reload`。批次 3 多会话 REST（`session_routes.py`）: `/api/sessions`（CRUD/activate）、`/api/sessions/{id}/messages`（before/after 分页）、`/api/messages/{mid}/context`、`/api/search/messages`、`/api/sessions/{id}/resources`（sticker 分类经 StickerPart 填充 + 附件软删过滤，批次 7）、`/api/sessions/{id}/generations`、`/api/generations/{gid}`、`/api/sessions/{id}/pending`。批次 7 附件/媒体（`api_routes.py` + `attachment_routes.py`）: `POST /api/uploads`（通用附件上传，filetype 魔数校验 + 签名 URL）、`GET /api/attachments/{id}`（HMAC 短签下载，免 Bearer，Range/206）、`GET /api/attachments/{id}/sign`（签发，Bearer）、`GET /api/stickers`（内置表情包）、`POST /api/uploads/desktop/{callId}`（批次 4 桌面附件）。`/uploads/` 静态挂载收敛：仅 images/desktop 子目录（新 kind 目录不静态暴露）。Debug API: `/debug/api` `/debug/api/prompts/{n}` `/debug/api/responses/{n}` `/debug/api/db-status` `/debug/api/ltm` `/debug/api/sessions` `/debug/api/archive` `/debug/api/tasks/stream`

## CharacterEngine

4-layer model managing character personality state. See `core/personality/README.md` for full docs.

- L1 Core: file-loaded, immutable base personality (`character/<name>/personality.md`)
- L2 Self: session-persisted long-term self-image, evolves slowly via compression (Phase 2)
- L3 State: session-persisted short-term state, driven by CharacterReflector + events
- L4 Mood: emotion per-turn, driven by LLM `<mood>` tag in main response stream (free-text Chinese), keyword detection as fallback

CharacterReflector is a subcomponent — after LLM reflection, it submits events to `CharacterEngine.consume_trigger("reflector", ...)` instead of writing `self_perception_history` directly.

C1 renders L1 + L2. C9 conditionally renders L3 via `AwarenessTrigger` (cooldown: 1 hour, persisted to session).

- **C6 SpeakingStyle 动态联动**: L2 演化后，LLM 编辑说话风格描述使与 L2 一致。版本历史与 L2 共享 `character_versions` 集合，per content_type 独立计数。

## Tool plugins (`core/tools/`)

Each plugin in its own directory: `plugin.py` + `config.yaml` + `README.md`. Plugins are imported in `__init__.py` and added to `ALL_PLUGINS`.

## Memory & storage

| Store | Backend | Purpose |
|-------|---------|---------|
| `sessions` | MongoDB `default.sessions` | LLM context window, 30d TTL |
| `message_archive` | MongoDB `default.message_archive` | Permanent append-only message log |
| `token_usage` | MongoDB `default.token_usage` | LLM token statistics |
| `long_term_memory` | Milvus `long_term_memory` | ExtractedMemory (Pydantic) → CompareAction (insert/update/merge/skip) → vector with reranking |
| `attachments` | MongoDB `default.attachments` | 附件元数据（BE-070，fail-closed）：ref_count/refs/status（软删 deleted_at）；TTL 不设，生命周期由 GC 管（24h/7d 规则） |

## Conventions

项目有以下约束文档，所有代码和文档变更必须遵守：

- **`docs/readme-conventions.md`** — README 编写规范。新增/修改模块时必须按模板写 README（面包屑 + 相关文档节），并在根 README 文档导航中注册。
- **`docs/testing.md`** — 测试分层体系。新测试必须加 marker（unit/service/integration/slow），日常用 `make test-quick` / `make test-normal` / `make test-full`。
- **`core/prompts/README.md`** — Prompt 模板命名规范（PascalCase `.j2`）+ 渲染方式（`str.replace()` / Jinja2 `render_macro()`，通过 `PromptManager`）。
- **`Module/system_prompt/templates/README.md`** — System prompt 模板格式约定（XML wrapper + Markdown body，标签 snake_case）。

## Import 风格

- **禁相对导入**（`from .xxx` / `from ..xxx`），一律绝对导入：`from core.xxx import y` / `from Module.xxx import y`。用 `make lint`（ruff TID252）强制；`__init__.py`（重导出）与 `__main__.py`（`python -m` 入口）豁免，见 `ruff.toml`。
- 循环导入规避：直接引子模块（`from core.monitoring.idle_monitor import IdleMonitor`），不要改经包重导出（`core.monitoring` 包用 lazy `__getattr__` 绕环）。

## 基础规则：

1. README 用中文写。
2. 代码注释用中文。
3. 使用 conda 环境: `conda activate Elysia`。
4. 所有敏感信息必须通过环境变量传递。
5. 新增 core/tools 插件必须包含 `plugin.py` + `config.yaml` + `README.md` 三元组。
