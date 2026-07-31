# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Elysia/Agent is a Python 3.11+ AI companion application. The codebase has two main layers:

- **`Module/`** — backend-agnostic library for AI companion infrastructure: LLM, STT, TTS, embedding, vector/document databases, output channels, config, logging, system prompt engine, tool framework.
- **`core/`** — the Elysia Agent application: `Agent`, `CognitiveLoop`, `CharacterEngine`, `CharacterReflector`, `MemoryConsolidator`, `MemoryReflector`, `SessionMemory`, `LongTermMemory`, `IdleMonitor`, task scheduler, debug info, character config.

Plain Python（无 `pyproject.toml` 等构建系统）。前端 Vue 3 + Vite + TypeScript（无框架 → Vite 构建步骤）。`Makefile` 提供常用任务入口。

## Running tests

### 分层运行

```bash
# 日常开发 — 秒级（unit + service）
make test-quick
# 或: pytest -m "unit or service" -q -x --tb=short

# 提交前 — 排除 slow（~5min）
make test-normal
# 或: pytest -m "not slow" -q --tb=short

# 全量 — 推之前（~22min）
make test-full
# 或: pytest -q
```

### 按模块运行

```bash
# Personality engine tests
python -m pytest core/personality/tests/ -v

# System prompt tests
python -m pytest Module/system_prompt/tests/ -v

# Core tests
python -m pytest core/tests/ -v

# TaskSystem tests
python -m pytest Module/task_system/tests/ -v

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
│   ├── agent.py           #   Agent.__init__ + event handling + process_message + background tasks
│   ├── lifecycle.py       #   AgentLifecycle: start()/stop() 编排 + _STARTUP_STEPS + _SHUTDOWN_STEPS + 3 flush
│   ├── server.py          #   AgentServer: run_server/run_repl + switch_*/get_wake_words + banner
│   ├── _types.py          #   RunState, _PendingMessage, _BackgroundTaskEntry
│   ├── _lifecycle_persistence.py  #   持久化层启停 (MongoDB/Milvus/session/repos)
│   ├── _lifecycle_services.py     #   LLM/TTS/Output/TokenTracker 启停
│   ├── _lifecycle_tools.py        #   ToolAdapter/Scheduler/IdleMonitor/event loop 启停
│   ├── _lifecycle_task_system.py  #   TaskSystem/DelegationPreparer/mailbox 启停
│   └── tests/
├── status/                # AgentStatus 接口层：protocol.py（ABC）+ fsm.py（FSM-backed 实现）+ agent_fsm.py（底层状态机）
├── cognitive/             # CognitiveLoop (package): CognitiveTools mixin + timing + TurnPhase
│   ├── loop.py            #   CognitiveLoop(CognitiveTools): process + ReAct 主循环 + TurnPhase 追踪（_build_turn_prompt / _stream_llm_response / _execute_tool_round / _finalize_turn / _dispatch_stream_event）
│   ├── stream_processor.py #   StreamProcessor: 流式标签检测 + 文本管道
│   ├── tools.py           #   CognitiveTools mixin: _execute_tools_directly + _handle_submit_task + _gate_tool_calls
│   └── timing.py          #   Timer + TimingClock latency tracking
├── adapter/               # Adapter layer: wraps Module/ backends for core/ consumption
│   ├── llm_adapter.py     #   LLMAdapter: tool-calling + retry + streaming + hot-switch
│   ├── tts_adapter.py     #   TTSStreamManager: sentence queuing + emoji sync + gated playback
│   ├── tool_call_parser.py #  merge/normalize streaming tool_call deltas
│   ├── tool_adapter.py    #   ToolAdapter: tool subsystem lifecycle + ToolExecutor delegation
│   ├── message_builder.py #   MessageBuilder: system prompt + history → LLM API messages[]
│   └── prompt_assembler.py #  PromptAssembler: creates 12 sections → SystemPromptManager → MessageBuilder
├── monitoring/             # Monitoring (package): event bus + idle monitor + token tracking
│   ├── event_bus.py       #   EventBus: pub/sub for agent internal events
│   ├── idle_monitor.py    #   IdleMonitor: wake-word detection + proactive triggers
│   ├── token_tracker.py   #   TokenTracker: usage counting + cost estimation
│   └── tests/
├── text/                  # Text stream processing (package): BlockFilter + SentenceBuffer + text utils
│   ├── block_filter.py    #   BlockFilter: streaming block tag filter + post-hoc block parsing
│   ├── sentence_buffer.py #   SentenceBuffer: split on sentence delimiters for TTS streaming
│   └── text_utils.py      #   content_text_from_content + MessageObj
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
│   ├── session.py          # SessionMemory: deque (maxlen), MongoDB persistence (30d TTL), fire-and-forget archive
│   ├── long_term.py        # LongTermMemory: Milvus-backed vector search + reranking; count-based _evict_overflow; async mark_absorbed with Milvus cleanup
│   ├── embedding_provider.py # EmbeddingProvider: 嵌入后端懒加载 + 超时 + OpenAI/HuggingFace fallback
│   ├── memory_reflector.py # MemoryReflector: periodic LTM dedup (round/new-memory triggers, KMeans clustering + LLM review)
│   └── consolidator.py     # MemoryConsolidator: buffers 6 msgs → LLM extracts + compares → insert/update/merge/skip
├── storage/                # Storage layer: MongoDB/Milvus repositories
│   ├── session_repo.py     #   SessionRepository: session CRUD + MongoDB TTL
│   ├── message_repo.py     #   MessageRepository: message archive append-only log
│   ├── token_repo.py       #   TokenRepository: token usage statistics
│   ├── memory_repo.py      #   MemoryRepository: LTM Milvus operations
│   └── character_version_repo.py # CharacterVersionRepository: L2/C6 version history
├── config/                 # Config sub-package: character_config + app_config
│   ├── character_config.py    # CharacterConfig: loads YAML + L1 Core personality text
│   ├── app_config.py          # AgentConfig: YAML-loaded app configuration
│   └── tests/
├── utils/                  # Utility functions: message_id + log_setup
├── task/                   # Task sub-package: task model + scheduler
│   ├── task.py             # Task dataclass + TaskType/TaskStatus enums + Future binding
│   ├── scheduler.py        # TaskScheduler: worker pool for ReAct tool calls (5 workers)
│   └── tests/
└── tools/                 # Tool plugins (filesystem, web, memory, time, reminder, etc.)
```

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
| `Module.llm` | LLM chat/streaming | `deepseek`, `openai`, `ollama`, `mimo`, `custom` |
| `Module.stt` | Speech-to-text | `sensevoice`, `custom` |
| `Module.tts` | Text-to-speech | `gpt-sovits`, `minimax`, `custom` |
| `Module.embedding` | Text vectorization | `huggingface`, `openai` |
| `Module.output` | Multi-channel output | `ConsoleChannel`, `WebSocketChannel` |
| `Module.config` | Config infrastructure | `ConfigLoader` (YAML + env + validation) |
| `Module.logger` | Logging factory | `LoggerFactory` (plain + JSON, rotating) |
| `Module.system_prompt` | System prompt engine | 12-section orchestration + templates |
| `core.schema` | Shared Pydantic types: EventData (discriminated union, 5 event types), ToolCall, ExtractedMemory/CompareAction (discriminated union), ReflectorActions, MemoryType, message types, mood, etc. | |
| `Module.database.vectordb` | Vector DB (ANN search) | `milvus` |
| `Module.database.documentdb` | Document DB | `mongodb` |
| `Module.database.graphdb` | Graph DB | stub only |
| `Module.bench` | Latency benchmarking | Covers LLM/TTS/STT |
| `core.input` | Input layer: WebSocket + HTTP server | `server.py` (glue), `ws_handler.py`, `api_routes.py`, `debug_routes/` (package, 7 modules) |
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
- **Session params**: `session.set_param("key", value)` / `session.get_param("key", default)` for runtime state. Persisted to MongoDB with session.
- **inner_voice protocol**: Every reply has one `<inner_voice>` block (1-2 sentences: current feeling + response intent, no reasoning chains).
- **Project paths**: `from paths import ProjectPath, get_project_root` — 所有项目文件系统路径的唯一定义点。`ProjectPath.X.resolve()` 返回基于 `ELYSIA_ROOT` 环境变量（fallback 为项目根目录）的绝对 `Path`。
- **AgentStatus**: `agent.status`（`AgentStatus`）提供语义状态查询（`is_accepting_input` / `is_speaking` / `should_suppress_vad_interrupt` 等）；`agent.phases`（`CognitivePhaseController`）驱动阶段转换。底层 `AgentFSM` 不对外暴露。设计文档：[spec](docs/superpowers/specs/2026-07-30-agent-status-interface-design.md)。

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
    │   ├── settingsStore.ts    # 设置项 + localStorage 同步
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

### Server routing (`core/input/` — `server.py` + `ws_handler.py` + `api_routes.py` + `debug_routes/` package)

`/` → `static/index.html`, `/debug` → `static/debug.html`, `/ws` → WebSocket, `/health` `/ready` `/shutdown` `/restart` `/api/chat` `/api/messages` `/api/images` `/api/providers` `/tools` `/tools/reload`。Debug API: `/debug/api` `/debug/api/prompts/{n}` `/debug/api/responses/{n}` `/debug/api/db-status` `/debug/api/ltm` `/debug/api/sessions` `/debug/api/archive` `/debug/api/tasks/stream`

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

## Conventions

项目有以下约束文档，所有代码和文档变更必须遵守：

- **`docs/readme-conventions.md`** — README 编写规范。新增/修改模块时必须按模板写 README（面包屑 + 相关文档节），并在根 README 文档导航中注册。
- **`docs/testing.md`** — 测试分层体系。新测试必须加 marker（unit/service/integration/slow），日常用 `make test-quick` / `make test-normal` / `make test-full`。
- **`core/prompts/README.md`** — Prompt 模板命名规范（PascalCase `.j2`）+ 渲染方式（`str.replace()` / Jinja2 `render_macro()`，通过 `PromptManager`）。
- **`Module/system_prompt/templates/README.md`** — System prompt 模板格式约定（XML wrapper + Markdown body，标签 snake_case）。

## 基础规则：

1. README 用中文写。
2. 代码注释用中文。
3. 使用 conda 环境: `conda activate Elysia`。
4. 所有敏感信息必须通过环境变量传递。
5. 新增 core/tools 插件必须包含 `plugin.py` + `config.yaml` + `README.md` 三元组。
