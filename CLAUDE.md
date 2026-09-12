# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Elysia/Agent is a Python 3.11+ AI companion application. The codebase has three top-level packages:

- **`Module/`** — backend-agnostic library for AI companion infrastructure: LLM, STT, TTS, embedding, vector/document databases, output channels, config, logging, system prompt engine, tool framework.
- **`core/`** — the Elysia Agent application: `Agent`, `CognitiveLoop`, `CharacterEngine`, `CharacterReflector`, `MemoryConsolidator`, `MemoryReflector`, `SessionMemory`, `LongTermMemory`, `IdleMonitor`, task scheduler, debug info, character config.
- **`frontend_contract/`** — 前后端契约单源（唯一类型来源 + 生成器 + 门禁）：可执行 Pydantic 契约模型（WS/REST/Debug）→ `gen_ts`/`gen_schema` 发射器 → `frontend/src/types/contract.generated.ts`（TS 只读快照）+ `contract.schema.json` + `contract.raw.ts`（Electron 机器契约，批次 9B）。后端发送点构造契约模型，`make check-types` 做漂移门禁。零依赖（仅 pydantic + stdlib）。

Plain Python（无 `pyproject.toml` 等构建系统）。前端 Vue 3 + Vite + TypeScript（无框架 → Vite 构建步骤）。`Makefile` 提供常用任务入口。

## Running tests

### 分层运行

```bash
# 日常开发 — 秒级（unit + service）
make test-quick
# 或: pytest -m "unit or service" -q -x --tb=short

# 提交前 — 排除 slow（~5min）
make test-normal
# 或: pytest -m "not slow and not external" -q --tb=short

# 全量 — 推之前（~22min）
make test-full
# 或: pytest -q

# 导入风格门禁（禁相对导入，秒级）
make lint
# 或: ruff check core/ Module/ frontend_contract/ --exclude __pycache__

# pyright 类型检查门禁（core + Module + frontend_contract 生产代码，basic + 排除测试；须 Elysia env——base env 无 aiohttp 会漏报 ~25 处）
make pyright
# 或: conda run -n Elysia pyright

# 架构边界门禁（ARCH-INC-P1-03/P2-03，纯 AST 无外部依赖，秒级）：依赖方向 + 跨包私有符号 + eager 环 + 裸后台任务登记
make check-arch
# 或: python scripts/check_architecture.py [--list]
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
│   ├── agent.py           #   Agent.__init__ + event handling + process_message + 43 公开 @property + background tasks
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
│   ├── loop.py            #   CognitiveLoop(CognitiveStreaming, CognitiveToolPhase, CognitiveFinalize): process + ReAct 主循环 + TurnPhase 追踪（_build_turn_prompt；每轮目标会话解析后原子换绑 ToolContext.services["session"]/["_session_msg_count"]，BE-S3-RUN-P0-01）
│   ├── streaming.py       #   CognitiveStreaming mixin：_stream_llm_response / _dispatch_stream_event / _apply_mood（CORE-MOD-P1-04）
│   ├── tool_phase.py      #   CognitiveToolPhase(CognitiveTools) mixin：_execute_tool_round / _build_tool_result_parts（CORE-MOD-P1-04）
│   ├── finalize.py        #   CognitiveFinalize mixin：_finalize_turn（CORE-MOD-P1-04）
│   ├── tools.py           #   CognitiveTools mixin: _execute_tools_directly + _handle_submit_task + _gate_tool_calls
│   ├── timing.py          #   Timer + TimingClock latency tracking
│   ├── generation.py      #   GenerationContext: generation_id/assistant_message_id/conversation_id 预分配贯通（ID 字段 NewType；channel/kind 对齐契约层 MessageChannel/SegmentKind StrEnum）
│   └── generation_registry.py # GenerationRegistry: generation 生命周期持久化（register/mark 终态 + stale 清理，Mongo 离线 no-op）
├── adapter/               # Adapter layer: wraps Module/ backends for core/ consumption
│   ├── llm_adapter.py     #   LLMAdapter: tool-calling + retry + streaming + hot-switch
│   ├── tts_adapter.py     #   TTSStreamManager: sentence queuing + emoji sync + gated playback + async set_enabled 禁用兜底（per-task flush_on_cancel）
│   ├── tool_call_parser.py #  merge/normalize streaming tool_call deltas
│   └── tool_adapter.py    #   ToolAdapter: tool subsystem lifecycle + ToolExecutor delegation
├── prompt/                # System prompt 组装层（CORE-MOD-P1-02 从 adapter 迁入）
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
│   ├── session.py          # SessionMemory: deque (maxlen), MongoDB persistence (无 TTL——删除走显式级联删除), fire-and-forget archive; 专用类型字段 character_state/service_state 经 save/load 同步
│   ├── long_term.py        # LongTermMemory: Milvus-backed vector search + reranking; count-based _evict_overflow; async mark_absorbed with Milvus cleanup。MemoryEntry.memory_type 用 MemoryType StrEnum，构造/update/search 边界经 try_coerce/coerce 归一（未知→Note，过滤大小写不敏感）
│   ├── embedding_provider.py # EmbeddingProvider: 嵌入后端懒加载 + 超时 + OpenAI/HuggingFace fallback
│   ├── memory_reflector.py # MemoryReflector: periodic LTM dedup (round/new-memory triggers, KMeans clustering + LLM review)
│   └── consolidator.py     # MemoryConsolidator: buffers 6 msgs → LLM extracts + compares → insert/update/merge/skip
├── storage/                # Storage layer: MongoDB/Milvus repositories
│   ├── session_repo.py     #   SessionRepository: session CRUD（sessions 集合无 Mongo TTL）
│   ├── message_repo.py     #   MessageRepository: message archive append-only log
│   ├── token_repo.py       #   TokenRepository: token usage statistics
│   ├── memory_repo.py      #   MemoryRepository: LTM Milvus operations
│   ├── generation_repo.py  #   GenerationRepository: generation 生命周期 upsert/query（BE-024）
│   ├── character_version_repo.py # CharacterVersionRepository: L2/C6 version history
│   ├── attachment_repo.py  #   AttachmentRepository: attachments 集合（fail-closed，BE-070——Mongo 离线 raise StorageUnavailableError → 503；refs/ref_count + 软删 status/deleted_at）
│   ├── attachment_release.py # AttachmentRelease: 会话/消息删除的附件引用释放共享 helper（提取自 debug_routes/archive.py；REST DELETE /api/sessions/{id} 删除前先 release_session_attachments，批次 8a D5）
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
├── version.py              # TRANSPORT_VERSION="2"（握手头可用值）/ PROTOCOL_VERSION="2.0"（仅展示）——协议版本单源（批次 9B）
├── ws_message/             # WS 消息契约**包**（ARCH-INC-P2-02 物理拆分，原 1206 行单文件 → 9 模块）
│   ├── __init__.py         #   **单一装配点**：唯一 ServerMessage/ClientMessage 联合定义位置 + 显式 re-export（97 名）
│   ├── _types.py           #   共享词汇：ContentPart 联合 + 全部非联合成员类型（零跨模块依赖，426 行）
│   ├── chat.py             #   chat 域 17 定义（Delta/Message/Token/Audio/Expression/Live2D/State/Typing/Transcription…）
│   ├── session.py          #   session 域 12 定义（Session/Capabilities/ServerInfo/Command/Wake/SwitchModel/ServiceMode…）
│   ├── tool.py             #   tool 域 5 定义（ToolStarted/ToolCompleted/ToolRequest/ToolStatus/ToolCall*）
│   ├── generation.py       #   generation 域 6 定义（Started/SegmentStarted/Delta/SegmentCompleted/Completed/Failed）
│   ├── voice.py            #   voice 域 21 定义（Voice*/Dictation* + 批次 6 TTS 播放控制 Audio*）
│   ├── ack.py              #   ack 域 2 定义 + UserAckMessage 嵌套联合
│   └── character.py        #   character 域 1 定义（CharacterStateChanged）
# 成员数不变：ServerMessage 42 / ClientMessage 22（不含 TextMessage——type="text" 撞 discriminator）；
├── rest.py                 # REST 业务契约：providers / images / messages / 多会话 / Desktop / 附件与表情（AttachmentInfo/AttachmentUploadResponse/AttachmentSignResponse/StickerEntry/StickerListResponse，批次 7）等 73 模型（MessagesPageResponse.cursor，批次 9A）
├── debug.py                # Debug API 契约：/debug/api 响应（DebugData 16 面板等 38 模型）
├── gen_ts.py               # TS 发射器：BaseModel → contract.generated.ts（拓扑排序 + 同名安全网）+ generate_raw_ts（raw wire 快照，批次 9B）
├── gen_schema.py           # JSON Schema 发射器：Annotated 判别联合（get_origin 收集）+ BaseModel → contract.schema.json（transportVersion/protocolVersion/schemaHash，批次 9B）
├── contract.schema.json    # 生成产物（机器契约 JSON Schema，批次 9B）
├── contract.raw.ts         # 生成产物（Electron 可消费 raw wire 类型快照，批次 9B）
├── __main__.py             # python -m frontend_contract：动态收集子模块 → 生成 ts/schema/raw_ts 三产物 / --check 漂移检测
└── tests/                  # 契约建模测试（marker=unit，无 I/O）
```

前后端契约唯一类型来源：可执行 Pydantic 模型 → `gen_ts` 生成 `frontend/src/types/contract.generated.ts`
（TS 只读快照）+ `gen_schema` 生成 `contract.schema.json` + `gen_ts.generate_raw_ts` 生成 `contract.raw.ts`。
后端发送点构造契约模型 + `wire_dict()` 序列化；`make gen-types` 重新生成、`make check-types` 漂移门禁
（CI 强制，覆盖 ts/schema/raw_ts + `scripts/gen_contract_frames.py --check` **17 个 golden frames**——含阶段 4
普通录音/听写/音频播放/服务切换 WS 流程与 Profile/TTS REST 样例）。零依赖（仅 pydantic + stdlib），不 import core/ Module/。
**依赖方向（有意，勿"修复"）**：本层是共享底座，**`Module/output` 依赖本层**（`manager.py` / `websocket.py` 直接
构造 `ActionMessage`/`AudioMessage` 等契约模型，并复用 `MessageChannel`/`SegmentKind`/`AudioFormat`/`FinishReason`
枚举）。单向无环（本层零依赖、绝不反向 import Module/core）；若改成 Module 自建枚举再在 core 映射，会重复定义这些
枚举并重新引入漂移风险——正是契约单源要消除的问题。代价仅是 Module 不再是可独立分发的自底向上包。
**阶段 4 确定性 smoke**：`conda run -n Elysia python scripts/stage4_smoke.py`——进程内 `create_app` + 真 agent(fake LLM) +
STT/TTS 桩，覆盖 Profile/三语音模式/TTS 播放 5 场景（默认 stub 确定性、失败非零退出；`STAGE4_SMOKE_LIVE=1` 才接真实
provider，不进 CI）；`scripts/tests/test_stage4_smoke.py` 为 slow pytest 包装。

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
| `Module.system_prompt` | System prompt engine | 12-section orchestration + templates |
| `Module.workflow_engine` | 后台任务引擎（原 `task_system`）：ReAct 编排/规划/执行/子智能体。主类 `WorkflowEngine`；跨边类型经契约层 `Module/workflow_contract` | `orchestrator` + `planner` + `executor` + `sub_agent` |
| `Module.workflow_contract` | 跨边契约层（core ↔ workflow_engine 唯一类型来源）：类型/枚举/协议 ABC/无状态纯函数/AgentInfra 运行时注入容器，零业务逻辑 | `schema` + `infra` + `utils` |
| `frontend_contract` | 前后端契约单源（唯一类型来源 + 生成器 + 门禁）：可执行 Pydantic 契约模型（WS/REST/Debug）+ `gen_ts`/`gen_schema` 发射器 → `contract.generated.ts` + `contract.schema.json` + `contract.raw.ts`（机器契约，批次 9B），`make check-types` 漂移检测（含 golden frames --check），零依赖（仅 pydantic + stdlib） | `base` + `version` + `ws_message` + `rest` + `debug` + `gen_ts` + `gen_schema` |
| `core.schema` | Shared Pydantic types: EventData (discriminated union, 5 event types), ToolCall, ExtractedMemory/CompareAction (discriminated union), ReflectorActions, MemoryType, message types, mood, etc. `types.py`：ID NewType（MessageId/SessionId/ToolCallId/GenerationId/SegmentId/RequestId/AttachmentId/MemoryId/TaskId）——core 内部 ID 流转对齐 NewType，input 边界（wire 裸 str）收口点包一层，frontend_contract 契约层保持裸 str（零依赖，不引 core）。`part.py`：Part 判别联合 6 类（含 StickerPart，批次 7）、Image/File/Audio/Video Part 带 `attachment_id`、FileMime 补值（pdf/zip/markdown） | |
| `Module.database.vectordb` | Vector DB (ANN search) | `milvus` |
| `Module.database.documentdb` | Document DB | `mongodb` |
| `Module.database.graphdb` | Graph DB | stub only |
| `Module.bench` | Latency benchmarking | Covers LLM/TTS/STT |
| `core.providers` | Provider 元数据与自定义 provider 持久化（CORE-MOD-P1-01 从 input 迁入） | `custom_providers.py` + `provider_registry.py` |
| `core.services` | 服务运行时能力声明与广播（CORE-MOD-P1-01 从 input 迁入） | `capabilities.py` |
| `core.network` | 网络运行时基础设施（CORE-MOD-P2-01 从 input 迁入） | `rate_limiter.py` |
| `core.prompt` | System prompt 组装层（CORE-MOD-P1-02 从 adapter 迁入） | `message_builder.py` + `prompt_assembler.py` |
| `core.input` | Input layer: WebSocket + HTTP server | `server.py` (纯装配：create_app + 管理端点；bind 守卫在 core/utils/server_guard.py，批次 D), `middleware.py` (认证/payload/限流中间件工厂 + `_nosniff_uploads`；限流器在 core/network，批次 D), `health.py` (health/ready 探针，批次 D), `ws_handler.py` (协议 framing + 连接生命周期：握手/回放/消息循环；**1210→691 行**，22 个 dispatcher 已按领域迁出，2026-08-14 批次 B + 2026-09-11 ARCH-INC-P2-01), `ws_handlers/` (package：`context.py` 收口连接级状态 `WsRequestContext`——handler 签名由 10 参数收敛为 `(ctx, data, msg, channel)`；`chat/session/tool/voice.py` 四领域 handler；`__init__.py` 单一装配点 `_WS_HANDLERS`——wire 不变), `ws_v2.py` (v2 深业务：`_route_v2_text`/`_handle_voice_commit` + 幂等/ACK helper，批次 B 下移), `_common.py` (跨业务共享：`_send_ack`/`_find_channel`/`_tool_error`/`_archive_*`), `errors.py` (统一错误信封，批次 A 下沉), `provider_routes.py`/`tts_routes.py`/`upload_routes.py`/`tools_routes.py`/`chat_routes.py` (批次 C 拆 5 域，api_routes 退役), `attachment_routes.py` (附件签名下载 + 签发，BE-070), `session_routes.py` (多会话/历史/搜索/资源/状态/pending REST), `voice_sessions.py` (三模式语音状态机：普通录音/实时语音/输入法转写，BE-060~062), `sticker_registry.py` (内置表情注册表，BE-071), `audio_buffer.py` (VAD 分段 + STT 提交), `debug_routes/` (package, 7 modules) |
| `Module._common` | Shared utility layer | env, registry helpers（含 `inject_logger` / `get_caller_logger` 两种 logger 注入风格）, test helpers, **`task_scope.py`（`AsyncTaskScope`/`cancel_and_drain`——后台协程所有权收口点，ARCH-INC-P2-03）** |

## Config system

Hierarchical: `env var > YAML file > dataclass default`. Main config at `config.yaml`.

- `agent.character` — path to character config (e.g. `character/elysia/config.yaml`)
- `agent.user_name` — user name injected into system prompt
- `llm.model` — format `<provider>/<model>` (e.g. `deepseek/deepseek-v4-flash`)
- `tts.model` — TTS provider (e.g. `minimax/speech-2.8-hd`). Voice mapping in `Module/tts/minimax_voices.yaml`
- `stt.model` — STT provider (e.g. `sensevoice/sense-voice`)

## Key patterns

- **Async-first**: All I/O is async. Backends manage their own `httpx.AsyncClient` or `openai.AsyncOpenAI`.
- **Logger injection**: 两种风格并存，都在 `Module/_common/registry_helpers.py`——① `inject_logger(__name__)`：**显式**传模块名，各 Module registry 在 `Factory.create()` 内用；② `get_caller_logger()`：经**单跳 `f_back`** 自动探测调用方模块，用于 `__init__` 这种拿不到显式名称的注入点（`Module/output/manager.py` / `websocket.py`）。**理由不是洁癖**：`core/utils/log_setup.py` 按 logger 名把日志分流到不同文件，名字错一层日志就进错文件。
- **env var precedence**: API keys and base URLs from `os.getenv()`, not YAML.
- **MongoDB**: motor async driver. `insert_one/insert_many` pass `dict()` copy to avoid motor mutating callers' dicts.
- **Session state**: 批次 3 起由 `SessionManager`（`core/memory/session_manager.py`）管理会话缓存池 + 活动指针（`agent.session_manager`；`agent.session` 仍指活动会话）。多会话路由：`agent.resolve_session(conversation_id, protocol_version)`——v1 恒 `"default"`、v2 显式 conversationId → 该会话、v2 缺省 → 活动会话；`activate_session` WS 消息切 active + 重发回放。运行时状态用 SessionMemory 的**专用类型字段**（`character_state` / `service_state`；反射器状态非字段，由 `session.save()` 经 `save_state()` 派生），经 `session.save()` 同步、`load()` 恢复（MongoDB 持久化 + JSONL fallback；2026-08-10 后 `sessions` 不再建 Mongo TTL 索引，删除走显式级联删除）。无 `set_param/get_param` 通用接口——新增状态仿 `character_state` 加字段 + save/load 两侧同步。`service_state`（服务运行时状态：当前模型 + STT/TTS 启用模式，enabled 三态 `bool | None`，None 回退 config）由 `agent.set_service` / `switch_*` 写、启动时经 `_apply_service_state` 应用。
- **inner_voice protocol**: Every reply has one `<inner_voice>` block (1-2 sentences: current feeling + response intent, no reasoning chains).
- **Project paths**: `from paths import ProjectPath, get_project_root` — 所有项目文件系统路径的唯一定义点。`ProjectPath.X.resolve()` 返回基于 `ELYSIA_ROOT` 环境变量（fallback 为项目根目录）的绝对 `Path`。
- **AgentStatus**: `agent.status`（`AgentStatus`）提供语义状态查询（`is_accepting_input` / `is_speaking` / `should_suppress_vad_interrupt` 等）；`agent.phases`（`CognitivePhaseController`）驱动阶段转换。底层 `AgentFSM` 不对外暴露。设计文档：[spec](docs/superpowers/specs/archive/2026-07/2026-07-30-agent-status-interface-design.md)。
- **Agent 公开接口**: `Agent` 作为组合根，通过 43 个同名 `@property` 公开子系统访问路径（`agent.session` / `agent.long_term` / `agent.tool_adapter` / `agent.output` / `agent.llm` / `agent.stt` / `agent.tts` / `agent.config` / `agent.bus` 等，可写性由子系统 API 自管）。状态写方法：`agent.request_shutdown()` / `agent.request_restart()`（设 `_state` 信号引导主循环退出，不做清理）；`agent.set_service(service, enabled)`（STT/TTS 后端模式开关）；`agent.purge_message_from_session(msg_id)`。跨模块消费禁止 `agent._xxx`，一律走公开接口；debug 层深度内省用 `# noqa: SLF001` 显式豁免。设计文档：[spec](docs/superpowers/specs/archive/2026-08/2026-08-01-agent-public-readonly-interface-design.md)。

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

### Server routing (`core/input/` — `server.py` + `middleware.py` + `health.py` + `ws_handler.py` + `ws_v2.py` + `errors.py` + `provider_routes.py` + `tts_routes.py` + `upload_routes.py` + `tools_routes.py` + `chat_routes.py` + `attachment_routes.py` + `session_routes.py` + `debug_routes/` package)

`/` → `static/index.html`, `/debug` → `static/debug.html`, `/ws` → WebSocket, `/health`（liveness 恒 200）`/ready`（readiness，依赖不 ok → 503）`/shutdown` `/restart` `/api/chat` `/api/messages` `/api/images` `/api/providers` `/tools` `/tools/reload`。批次 3 多会话 REST（`session_routes.py`）: `/api/sessions`（CRUD/activate）、`/api/sessions/{id}/messages`（before/after 分页）、`/api/messages/{mid}/context`、`/api/search/messages`（category 白名单 8 值含 sticker 贴图历史，阶段 3；`date_from/date_to` 严格 `YYYY-MM-DD` fail-fast，非格式 400 `CONTRACT_VALIDATION`）、`/api/sessions/{id}/resources`（sticker 分类经 StickerPart 填充 + 附件软删过滤，批次 7）、`/api/sessions/{id}/generations`、`/api/generations/{gid}`、`/api/sessions/{id}/pending`。批次 7 附件/媒体（`upload_routes.py` + `attachment_routes.py`）: `POST /api/uploads`（通用附件上传，filetype 魔数校验 + 签名 URL；阶段 3 起逐文件 `kind`——每文件一组 `kind+files` 邻接配对，兼容旧单 kind 整批提示，悬空/歧义/未知 → 400；`kind` 确定后 MIME 规范化单源——audio+video/webm→audio/webm、WAV 探测别名→audio/wav，`core/schema/utils.py::normalize_attachment_mime`，写/读/下载三路径同值）、`GET /api/attachments/{id}`（HMAC 短签下载，免 Bearer，Range/206，Content-Type 同样过规范化）、`GET /api/attachments/{id}/sign`（签发，Bearer）、`GET /api/stickers`（内置表情包）、`POST /api/uploads/desktop/{callId}`（批次 4 桌面附件）。`/uploads/` 静态挂载收敛：仅 images/desktop 子目录（新 kind 目录不静态暴露）。Debug API: `/debug/api` `/debug/api/prompts/{n}` `/debug/api/responses/{n}` `/debug/api/db-status` `/debug/api/ltm` `/debug/api/sessions` `/debug/api/archive` `/debug/api/tasks/stream`

## CharacterEngine

4-layer model managing character personality state. See `core/personality/README.md` for full docs.

- L1 Core: file-loaded, immutable base personality (`character/<name>/personality.md`)
- L2 Self: session-persisted long-term self-image, evolves slowly via compression (Phase 2)
- L3 State: session-persisted short-term state, driven by CharacterReflector + events
- L4 Mood: emotion per-turn, driven by LLM `<mood>` tag in main response stream (free-text Chinese), keyword detection as fallback

CharacterReflector is a subcomponent — after LLM reflection, it submits events to `CharacterEngine.consume_trigger("reflector", ...)` instead of writing `self_perception_history` directly.

C1 renders L1 + L2. C9 conditionally renders L3 via `AwarenessTrigger` (cooldown: 5 minutes, persisted to session).

- **C6 SpeakingStyle 动态联动**: L2 演化后，LLM 编辑说话风格描述使与 L2 一致。版本历史与 L2 共享 `character_versions` 集合，per content_type 独立计数。

## Tool plugins (`core/tools/`)

Each plugin in its own directory: `plugin.py` + `config.yaml` + `README.md`. Plugins are imported in `__init__.py` and added to `ALL_PLUGINS`.

**阶段 6 工具双轨（2026-08-19，方案 B 已定案，后端代码批已完成）：** Desktop 工具走 `tool.request/tool.status`；Backend 工具 v2 发 `tool.started/tool.completed` 展示帧，v2 停发 `action{tool_executing/tool_done}`（v1 逐字节不变）；`tool.request` 必填 `sourceCallId`（LLM `tool_call_id`，经 scheduler 线程化到 `ctx.call_id`）并由桥填充 `requestId`。后端 B-6-01~09 落地（commit `c16e27d9..6a54fc25`），执行记录见 `docs/progress/archive/2026-08/2026-08-19-stage6-工具双轨-execution-record.md`；golden `ws-v2-tool.jsonl` 预期增 `sourceCallId`（D-GL-01）。前端 F-6 批已在 elysia-frontend 落地并完成真实 Windows Electron 联调（2026-08-21，阶段 6 已真实联调；D-§7-4/5 前端定，可选项 DEFERRED）。

## Vision（看图）备忘 — WebUI `describe_image` 优先

- **看用户/任务里提到的图片，优先用 WebUI（Agent 平台）自带的 `describe_image` 工具**：传图片本地绝对路径（如 `/home/yomu/elysia-agent/static/Elysia.png`）或 URL + `prompt`，即可拿到图片描述，无需经过项目后端。
- 同一平台的 `read_image`（图片直接喂给底层模型）**不可用**：当前底层模型 `deepseek-v4-pro` 未声明 image input，会报 `model ... does not declare image input`；看图一律走 `describe_image`。
- 实测记录（2026-08-19，`static/Elysia.png`）：WebUI `describe_image` 可正常查看并正确描述（粉色短发、紫瞳、精灵耳、粉白礼服少女）。偶发 timeout / 安全策略拒绝时，换更简短、中性的 `prompt` 重试即可。
- 项目后端也有同名工具 `core/tools/vision/DescribeImagePlugin`（agent 运行时描述用户消息中的图片，与 WebUI 工具无关）：模型来自 `config.vision.model`（默认 `mimo/mimo-v2.5`），支持 `image_index` / `image_indices`，结果缓存到 `data/cache/vision/*.json`，本地路径 / `/static/*` 会转 base64 data URI。链路实测成功，但 MiMo 可能认错角色（把 Elysia 认成《原神》诺艾尔），只保证返回描述。
- 项目插件的运行/测试坑：环境带 `ALL_PROXY=socks://127.0.0.1:7897/` 时，httpx 报 `Unknown scheme for proxy URL`；先 `env -u ALL_PROXY -u all_proxy` 再跑（HTTP 代理可保留）。

## Memory & storage

| Store | Backend | Purpose |
|-------|---------|---------|
| `sessions` | MongoDB `default.sessions` | LLM context window（**无 Mongo TTL**，删除走显式级联删除） |
| `message_archive` | MongoDB `default.message_archive` | Permanent append-only message log |
| `token_usage` | MongoDB `default.token_usage` | LLM token statistics |
| `long_term_memory` | Milvus `long_term_memory` | ExtractedMemory (Pydantic) → CompareAction (insert/update/merge/skip) → vector with reranking |
| `attachments` | MongoDB `default.attachments` | 附件元数据（BE-070，fail-closed）：ref_count/refs/status（软删 deleted_at）；TTL 不设，生命周期由 GC 管（24h/7d 规则） |
| `user_identity_profiles` | MongoDB `default.user_identity_profiles` | 用户最小身份（BE-STAGEH-02，Mongo 权威）：`user_id` 唯一索引 + revision CAS；Agent.start() 装载，重启不回到 bootstrap |

**Schema 变更（ARCH-INC-P1-04）**：**不做迁移框架**——新增集合/索引/字段由 `ensure_index`
（每次 `connect()` 幂等执行）自动覆盖，实测 4 个月 24 次索引变更中约 20 次属此类。
只有「删/改已有索引」需人工，用 `DocumentDBBackend.drop_index()`（Mongo 对不存在索引不报错，
故内部先 `list_indexes()` 查存在性）。启动步骤 `schema_drift` 只读比对代码声明与库中实况，
**孤儿索引**报 warning 并给命令，**绝不自动删**。详见 `docs/database-storage.md` §五。

## Conventions

项目有以下约束文档，所有代码和文档变更必须遵守：

- **`docs/code-style-guide.md`** — 代码风格与类型约束。写代码前先读：ID NewType 体系 + 类型权威归属（core 定义 vs 契约层定义）+ StrEnum/coerce 用法 + getattr 使用纪律。
- **pyright 门禁** — `make pyright`（`pyrightconfig.json`：include `core/` + `Module/` + `frontend_contract/`、排除 `**/tests/**`、`typeCheckingMode: basic`、`pythonVersion: 3.12`）。须在 Elysia env 运行——base env 无 aiohttp 会漏报 ~25 处。
- **架构边界门禁** — `make check-arch`（`scripts/check_architecture.py`，ARCH-INC-P1-03/P2-03）。六条规则：R1 `Module/` 生产代码与 `frontend_contract/` 不得反向依赖；R2 领域/基础设施层不得依赖 `core.input`；R3 禁止**跨包**导入下划线私有符号；R4 不得新增 **eager** import 环；R5 `TYPE_CHECKING` 类型边仅报告；R6 裸 `create_task`/`ensure_future` 必须逐点登记所有权。存量泄漏（现仅 3×R1：bench/documentdb CLI 两处刻意豁免）在脚本 allowlist 逐项登记理由——**新增即红**；收口某项后须同步删除对应 allowlist 条目（R3 已于 2026-09-11 批次 A 清空）。`TYPE_CHECKING` 类型边允许（R5 仅报告），函数内**懒导入**是打断 import 环的正当手段（R4 只算 eager，但 R1/R2/R3 仍算运行时依赖）。R6 记的是**期望条数**而非布尔——同一函数内追加裸 spawn、或清理后忘删条目，都会报；其 allowlist 只对真实仓库生效，测试用的合成 fixture 仓库不套用。
- **后台协程所有权** — 新增后台协程一律走 `Module/_common/task_scope.py` 的 `AsyncTaskScope`（按 owner 实例化：`spawn` 强引用 + done callback 统一记未处理异常 + `cancel_all`/`drain`/`aclose`）或 `cancel_and_drain(task, ...)`（单任务有序收尾）。`drain(timeout)` 是**总预算**而非逐任务超时。终态 owner（VoiceSession/ToolPlugin）用 `aclose()`；可重启 owner（Agent 同一实例 `stop()`→`start()`）用 `cancel_all()`+`drain()`——封闭 scope 会让重启后一律 `TaskScopeClosedError`。确需裸 `create_task` 的，须在 `_TASK_SPAWN_ALLOWLIST` 登记「任务存哪 / 谁取消 / 谁等待 / 异常谁看」。新 owner 停止后必须 drain 再关它依赖的资源（repo/客户端）。详见 `docs/review/2026-08-30-后端架构分层与模块化增量评估.md` §ARCH-INC-P2-03。
- **`docs/readme-conventions.md`** — README 编写规范。新增/修改模块时必须按模板写 README（面包屑 + 相关文档节），并在根 README 文档导航中注册。
- **`docs/testing.md`** — 测试分层体系。新测试必须加 marker（unit/service/integration/slow），日常用 `make test-quick` / `make test-normal` / `make test-full`。
- **`core/prompts/README.md`** — Prompt 模板命名规范（PascalCase `.j2`）+ 渲染方式（`str.replace()` / Jinja2 `render_macro()`，通过 `PromptManager`）。
- **`Module/system_prompt/templates/README.md`** — System prompt 模板格式约定（XML wrapper + Markdown body，标签 snake_case）。

## Import 风格

- **禁相对导入**（`from .xxx` / `from ..xxx`），一律绝对导入：`from core.xxx import y` / `from Module.xxx import y`。用 `make lint`（ruff TID252）强制；`__init__.py`（重导出）与 `__main__.py`（`python -m` 入口）豁免，见 `ruff.toml`。
- 循环导入规避：直接引子模块（`from core.monitoring.idle_monitor import IdleMonitor`），不要改经包重导出（`core.monitoring` 包用 lazy `__getattr__` 绕环）。

## 基础规则：

1. README 用中文写。
2. 代码注释用中文（docstring 同理；中英混合注释——中文嵌英文技术术语——可接受）。
3. 使用 conda 环境: `conda activate Elysia`。
4. 所有敏感信息必须通过环境变量传递。
5. 新增 core/tools 插件必须包含 `plugin.py` + `config.yaml` + `README.md` 三元组。
6. 生产代码函数参数必须类型注解（`make lint` 强制 ANN001/002/003；测试文件豁免——pytest fixture 动态类型惯例，`**kwargs: Any` / `*args: Any` 兜底）。
