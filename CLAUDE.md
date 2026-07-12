# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Elysia/Agent is a Python 3.13 AI companion application. The codebase has two main layers:

- **`Module/`** — backend-agnostic library for AI companion infrastructure: LLM, STT, TTS, embedding, vector/document databases, output channels, config, logging, system prompt engine, tool framework.
- **`core/`** — the Elysia Agent application: `Agent`, `CognitiveLoop`, `PersonalityEngine`, `EmotionEngine`, `CharacterReflector`, `MemoryConsolidator`, `SessionMemory`, `LongTermMemory`, `IdleMonitor`, task scheduler, debug info, character config.

No build system (`pyproject.toml`, etc.) — plain Python. Frontend is vanilla HTML/CSS/JS (no framework, no build step).

## Running tests

```bash
# Personality engine tests
python -m pytest core/personality/tests/ -v

# System prompt tests
python -m pytest Module/system_prompt/tests/ -v

# Core tests (excludes personality sub-package)
python -m pytest core/tests/ -v

# TaskSystem tests
python -m pytest Module/task_system/tests/ -v

# Other module tests
python -m pytest Module/llm/test_llm.py -v
python -m pytest Module/tts/test_tts.py -v
python -m pytest Module/stt/test_stt.py -v
python -m pytest Module/embedding/test_embedding.py -v
python -m pytest Module/output/test_output.py -v
python -m pytest Module/config/test_config.py -v
python -m pytest Module/system_prompt/test_prompt.py -v
python -m pytest Module/logger/test_logger.py -v
python -m pytest Module/database/vectordb/test_vectordb.py -v
python -m pytest Module/database/documentdb/test_documentdb.py -v
python -m pytest Module/tool/test_framework.py -v
python -m pytest Module/tool/test_tool_safety.py -v
```

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
├── agent.py              # Agent: orchestrates all subsystems, FSM lifecycle
├── cognitive_loop.py     # CognitiveLoop: THINKING → (EXECUTING → THINKING)* → SPEAKING → IDLE
├── personality/          # PersonalityEngine: 4-layer personality model
│   ├── engine.py         #   L1 Core (file, immutable) → L2 Self (session, slow evolution) → L3 State (session, Reflector-driven) → L4 Mood (EmotionEngine)
│   ├── reflector.py      #   CharacterReflector: buffers 50 pairs → LLM reflection → submit to engine
│   └── tests/
├── emotion_engine.py     # EmotionEngine: keyword + STT-driven mood tracking (L4 Mood)
├── consolidator.py       # MemoryConsolidator: buffers 6 msgs → LLM extracts Fact/Preference/Event/Plan → Milvus
├── memory/
│   ├── session.py         # SessionMemory: deque (maxlen), MongoDB persistence (30d TTL), fire-and-forget archive
│   ├── long_term.py       # LongTermMemory: Milvus-backed vector search + reranking
│   └── message_archive.py # MessageArchive: append-only MongoDB collection, paginated queries
├── character_config.py   # CharacterConfig: loads YAML + L1 Core personality text
├── prompt_assembler.py   # PromptAssembler: creates 12 sections → SystemPromptManager → MessageBuilder
├── debug_info.py         # DebugInfo: aggregates agent state for /debug dashboard
├── idle_monitor.py       # IdleMonitor: wake-word detection + proactive triggers
├── task_scheduler.py     # TaskScheduler: worker pool + task dispatch
├── tools/                # Tool plugins (filesystem, web, memory, time, reminder, etc.)
└── app_config.py         # AgentConfig: YAML-loaded app configuration
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
C5 Sensory          → Current environment context
C3 Emotion          → Mood + 8 emotional response rules
C4 Memory           → Long-term memory recall + reliability rules
C1 Personality      → L1 Core + L2 Self (data-driven)
C2 Relationship     → Relationship view (4-stage + boundaries)
C6 SpeakingStyle    → Speaking style (5-mode switching + praise/flirt/address)
C7 Proactive        → When to speak first + cooldown mechanism
C8 OwnLife          → Own activities (pass-through, may be empty)
C9 SelfPerception   → Self-awareness (AwarenessTrigger conditional)
```

Templates at `Module/system_prompt/templates/` — format convention: XML wrapper + Markdown body. See `Module/system_prompt/templates/README.md` for full format docs.

## Module index

| Module | Purpose | Backends |
|--------|---------|----------|
| `Module.llm` | LLM chat/streaming | `deepseek`, `openai`, `ollama` |
| `Module.stt` | Speech-to-text | `sensevoice` |
| `Module.tts` | Text-to-speech | `gpt-sovits`, `minimax`, `custom` |
| `Module.embedding` | Text vectorization | `huggingface`, `openai` |
| `Module.output` | Multi-channel output | `ConsoleChannel`, `WebSocketChannel` |
| `Module.config` | Config infrastructure | `ConfigLoader` (YAML + env + validation) |
| `Module.logger` | Logging factory | `LoggerFactory` (plain + JSON, rotating) |
| `Module.system_prompt` | System prompt engine | 12-section orchestration + templates |
| `Module.schema` | Shared data models | `UserMessage`, `ChatMessage`, `Event`, media types |
| `Module.database.vectordb` | Vector DB (ANN search) | `milvus` |
| `Module.database.documentdb` | Document DB | `mongodb` |
| `Module.database.graphdb` | Graph DB | stub only |
| `Module.bench` | Latency benchmarking | Covers LLM/TTS/STT |
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
- **FSM**: `AgentFSM` manages state transitions: IDLE → THINKING → (EXECUTING → THINKING)* → SPEAKING → IDLE.

## Frontend (`static/`)

Vanilla HTML/CSS/JS, ES modules (`<script type="module">`), no build step.

```
static/
├── index.html       # Chat shell (~55 lines)
├── debug.html        # Debug dashboard (12+ panels, polls /debug/api every 2s)
├── css/chat.css      # All styles
└── js/
    ├── config.js      # Constants (timeouts, buffer sizes, storage keys)
    ├── state.js       # AppState singleton
    ├── ws.js          # WebSocket: connect, reconnect (exponential backoff + jitter)
    ├── chat.js        # Chat display: bubbles, delta streaming, typing, tool cards, mood, lightbox
    ├── audio.js       # TTS buffer/playback
    ├── mic.js         # Microphone: getUserMedia, ScriptProcessor, PCM→Int16
    ├── settings.js    # Settings panel: TTS toggle, volume, clear, shutdown
    ├── persistence.js # localStorage save/restore (incremental, with images)
    └── main.js        # Entry point: WS routing, input, keyboard, image upload
```

### Debug page panels

FSM state, session/Long-Term memory, message archive, token stats, PersonalityEngine (L2 Self, L3 State, awareness cooldown, state history), Character Reflector (buffer, last reflection, prompt), latency stats, task scheduler, tools, consolidation history, MongoDB/Milvus status.

### Server routing (`Module/input/server.py`)

`/` → `static/index.html`, `/debug` → `static/debug.html`, `/debug/api` → JSON debug info, `/ws` → WebSocket, `/api/images` → image upload, `/health` `/ready` `/shutdown`

## PersonalityEngine

4-layer model managing character personality state. See `core/personality/README.md` for full docs.

- L1 Core: file-loaded, immutable base personality (`character/<name>/personality.md`)
- L2 Self: session-persisted long-term self-image, evolves slowly via compression (Phase 2)
- L3 State: session-persisted short-term state, driven by CharacterReflector + events
- L4 Mood: emotion per-turn, driven by EmotionEngine (existing, unchanged)

CharacterReflector is a subcomponent — after LLM reflection, it submits events to `PersonalityEngine.consume_trigger("reflector", ...)` instead of writing `self_perception_history` directly.

C1 renders L1 + L2. C9 conditionally renders L3 via `AwarenessTrigger` (cooldown: 1 hour, persisted to session).

## Tool plugins (`core/tools/`)

Each plugin in its own directory: `plugin.py` + `config.yaml` + `README.md`. Plugins are imported in `__init__.py` and added to `ALL_PLUGINS`.

## Memory & storage

| Store | Backend | Purpose |
|-------|---------|---------|
| `sessions` | MongoDB `default.sessions` | LLM context window, 30d TTL |
| `message_archive` | MongoDB `default.message_archive` | Permanent append-only message log |
| `token_usage` | MongoDB `default.token_usage` | LLM token statistics |
| `long_term_memory` | Milvus `long_term_memory` | Vector memory with reranking |

## Rules

1. README 用中文写。
2. 代码注释用中文。
3. 使用 conda 环境: `conda activate Elysia`。
4. 所有敏感信息必须通过环境变量传递。
5. 模板文件遵循 `Module/system_prompt/templates/README.md` 的格式约定。
