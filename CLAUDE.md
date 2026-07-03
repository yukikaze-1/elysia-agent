# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

`Module/` is a Python 3.13 library providing backend-agnostic interfaces for AI companion infrastructure: LLM, STT, TTS, embedding, vector/document/graph databases, output channels, and configuration management. It serves as the shared "module" layer consumed by the larger Elysia/Agent application (which lives outside this repo).

This repo has no build system (`pyproject.toml`, `setup.py`, etc.) — it's a dependency library imported by the parent project.

## Running tests

```bash
# TaskSystem unit tests (mock-based, no external services, ~6s)
python -m pytest Module/task_system/tests/unit/ -v

# TaskSystem integration tests (require LLM / external services)
python -m pytest Module/task_system/tests/integration/ -v

# All TaskSystem tests
python -m pytest Module/task_system/tests/ -v

# Other module tests
python -m pytest Module/llm/test_llm.py -v
python -m pytest Module/tts/test_tts.py -v
python -m pytest Module/stt/test_stt.py -v
python -m pytest Module/embedding/test_embedding.py -v
python -m pytest Module/output/test_output.py -v
python -m pytest Module/config/test_config.py -v
python -m pytest Module/prompt/test_prompt.py -v
python -m pytest Module/logger/test_logger.py -v
python -m pytest Module/database/vectordb/test_vectordb.py -v
python -m pytest Module/database/documentdb/test_documentdb.py -v
python -m pytest Module/tool/test_framework.py -v
python -m pytest Module/tool/test_tool_safety.py -v

# Core tests
python -m pytest core/tests/ -v
python -m pytest core/tools/ -v
python -m pytest core/test_timing.py -v

# Integration smoke tests (require live services)
python -m Module.llm          # Needs DEEPSEEK_API_KEY in .env
python -m Module.stt          # Needs SenseVoice at localhost:20042
python -m Module.tts          # Needs GPT-SoVits at localhost:9880
python -m Module.embedding    # Needs OPENAI_API_KEY in .env
python -m Module.output       # Self-contained smoke test
python -m Module.bench        # Unified latency benchmark (LLM + TTS + STT)
```

## Architecture: Protocol-Registry-Backend pattern

Every service module follows the same three-layer pattern:

```
Module/<service>/
├── protocol.py    # ABC defining the interface (LLMBackend, STTBackend, TTSBackend, etc.)
├── registry.py    # Factory + _BACKENDS dict mapping "provider/model" → backend class
├── <backend>.py   # Concrete implementation(s)
├── config.py      # Dataclass config using Module.config.ConfigField
├── __init__.py    # Re-exports Backend ABC, Factory, Config
└── __main__.py    # Integration smoke test
```

- **protocol** — async ABC with `chat`/`chat_stream`, `transcribe`/`transcribe_stream`, `synthesize`/`synthesize_stream`, etc. All backends expose both streaming and non-streaming paths.
- **registry** — `_BACKENDS` dict stores provider metadata (base_url, env_key, supports_prefix). `Factory.create("provider/model")` uses `Module/_common/registry_helpers` for api_key/base_url resolution and logger injection.
- **backend** — Concrete implementation. All use `httpx.AsyncClient` or `openai.AsyncOpenAI` for async I/O. Streaming is bridged via `asyncio.Queue` + `create_task`. `test()` methods use `Module/_common/test_helpers.make_test_method()` factory.
- **`Module/_common/`** — Shared utility layer (not a service module). Provides `env.py` (dotenv), `registry_helpers.py` (api_key/base_url/logger), `test_helpers.py` (make_test_method), `config_test_helpers.py` (make_config_test_classes), `main_runner.py` (run_module_main). Does NOT expose public API via `Module/__init__.py`.

## Module index

| Module | Purpose | Backends implemented | Backends stubbed |
|--------|---------|---------------------|-----------------|
| `Module.llm` | LLM chat/streaming | `deepseek`, `openai`, `ollama` | `gemini` |
| `Module.stt` | Speech-to-text | `sensevoice` | `openai`, `deepgram` |
| `Module.tts` | Text-to-speech | `gpt-sovits` | — |
| `Module.embedding` | Text vectorization | `huggingface`, `openai` | — |
| `Module.output` | Multi-channel output | `ConsoleChannel`, `WebSocketChannel` | — |
| `Module.config` | Config infrastructure | `ConfigLoader` (YAML + env + validation) | — |
| `Module.logger` | Logging factory | `LoggerFactory` (plain + JSON, rotating files) | — |
| `Module.prompt` | Jinja2 template rendering | `PromptManager` | — |
| `Module.brain_prompt` | Brain Prompt 模块化引擎 | `BrainPromptRegistry`, `SectionParser`, `ProfileConfig` | — |
| `Module.schema` | Shared data models | `UserMessage`, `ChatMessage`, `Event`, `EnvironmentMessage`, media types | — |
| `Module.database.vectordb` | Vector DB (ANN search) | `milvus` | — |
| `Module.database.documentdb` | Document DB | `mongodb` | — |
| `Module.database.graphdb` | Graph DB | stub only | — |
| `Module.bench` | Latency benchmarking | Covers LLM/TTS/STT streaming and non-streaming | — |
| `Module._common` | Shared utility layer (internal) | `env.py`, `registry_helpers.py`, `test_helpers.py`, `config_test_helpers.py`, `main_runner.py` | — |

## Config system (`Module.config`)

Hierarchical config: `env var > YAML file > dataclass default`. Uses `ConfigField` (a `dataclasses.field()` wrapper) to declare fields with optional env-var binding, `secret=True` (env-only), constraints (`Range`, `Choices`, `Regex`), and required checks. Validation collects all errors before raising `ConfigValidationError`.

```python
from dataclasses import dataclass
from Module.config import ConfigField, ConfigLoader, Range, Choices

@dataclass
class MyConfig:
    host: str = ConfigField(default="0.0.0.0")
    port: int = ConfigField(default=8000, constraints=[Range(1, 65535)])
    api_key: str = ConfigField(default="", secret=True, env="MY_API_KEY")

cfg = ConfigLoader(MyConfig, yaml_path="config.yaml", yaml_key="Server").load()
```

## Output system (`Module.output`)

`OutputManager` broadcasts to multiple `OutputChannel` instances, filtered by `ChannelCapability` (TEXT_DELTA, TYPING, EXPRESSION, ACTION, COMMAND, AUDIO). `stream()` coordinates: typing indicator on → stream deltas → finalize message → typing off. Each channel declares which capabilities it supports; the manager routes accordingly. Channel failures don't block other channels.

## Key patterns

- **Async-first**: All I/O is async. Backends manage their own `httpx.AsyncClient` or `openai.AsyncOpenAI` lifecycle.
- **Logger injection**: `Factory.create()` auto-detects the caller's module via `inspect` and injects a logger. Callers can override by passing `logger=...` to `create()`.
- **env var precedence**: API keys and base URLs are resolved from `os.getenv()` in registry factories, not from YAML. `secret=True` in config fields enforces env-only.
- **DeepSeek prefix continuation**: Messages with `{"role": "assistant", "content": "...", "prefix": True}` trigger beta endpoint routing and `extra_body={"prefix": True}`. Non-DeepSeek providers raise `ValueError` on prefix.
- **Naming convention**: Backend classes end in `Backend` (not `Provider` or `Client`): `OpenAICompatBackend`, `SenseVoiceBackend`, `GPTSoVitsBackend`, etc.

## Frontend static files (`static/`)

The frontend is vanilla HTML/CSS/JS served directly by aiohttp. No build step, no framework.

### Chat interface (`static/index.html`)

Minimal 47-line HTML shell. All logic lives in `static/js/*.js` (ES modules, loaded via `<script type="module">`).

```
static/
├── index.html          # HTML shell (~55 lines): input bar + preview bar + lightbox
├── debug.html           # Internal debug/monitoring page
├── css/
│   └── chat.css         # All styles (~440 lines): bubbles, preview, lightbox, tool cards
└── js/
    ├── config.js         # Named constants (timeouts, buffer sizes, storage keys)
    ├── state.js          # AppState singleton — all mutable state in one place
    ├── ws.js             # WebSocket: connect, reconnect (exponential backoff + jitter), send
    ├── chat.js           # Chat display: bubbles, delta streaming, typing, tool cards, mood,
    │                       image preview, lightbox, multimodal user messages
    ├── audio.js          # TTS buffer/playback: base64 decode, queue, bubble highlight
    ├── mic.js            # Microphone: getUserMedia, ScriptProcessor, PCM→Int16
    ├── settings.js       # Settings panel: TTS toggle, volume, clear, shutdown
    ├── persistence.js    # localStorage save/restore (incremental, with image support)
    └── main.js           # Entry point: WS message routing, input, keyboard shortcuts,
                            image upload (paste/drag/file), sendText with multimodal content
```

### Image support (multimodal Phase 1+2)

- **Upload**: paste (Ctrl+V), drag-and-drop, or 📷 button → `POST /api/images` → saves to `data/uploads/images/`
- **Preview**: thumbnails in `#preview-bar` above input, × to remove
- **Send**: images + text combined as multimodal `content` array via WS `{type:"text", content:[...]}`
- **Render**: user bubbles show `<img>` thumbnails (max 200px), click to open lightbox
- **Persistence**: session history and localStorage both preserve image metadata

### Debug page (`static/debug.html`)

Single-file (no split). Polls `/debug/api` every 2s with backoff on failure. Displays: FSM state, session/long-term memory, latency stats, process memory, scheduler, tools, consolidation history. Pause/resume button, JSON export, expandable truncated messages, responsive layout.

### Server routing

Defined in `Module/input/server.py`:
- `/` → `static/index.html`
- `/debug` → `static/debug.html`
- `/debug/api` → JSON debug info
- `/static/*` → static files
- `/uploads/*` → user uploaded files (`data/uploads/`)
- `/live2d/*` → Live2D Cubism SDK assets
- `/ws` → WebSocket endpoint
- `/api/images` → image upload (multipart)
- `/api/chat` → multimodal test endpoint
- `/health`, `/ready` → health checks
- `/shutdown` → graceful shutdown

## Tool plugin layout (`core/tools/`)

Each plugin group lives in its own directory with code, config, and docs:

```
core/tools/
├── __init__.py          # ALL_PLUGINS list (imports from subdirectories)
├── filesystem/          # ReadFile, ListDir, WriteFile
│   ├── plugin.py        # Plugin classes
│   ├── config.yaml      # SafetyProfile overrides (read_dirs, write_dir, etc.)
│   └── README.md
├── web/                 # WebSearch, WebFetch
├── memory/              # MemorySearch, MemorySave
├── time/                # TimePlugin
├── reminder/            # SetReminderPlugin
├── runtime_config/      # RuntimeConfigPlugin
└── self_state/          # SelfStatePlugin
```

Each plugin's `config.yaml` overrides `SafetyProfile` fields at load time via `ToolManager.load_all()`. Only the plugin directory's config is read — there is no global config for tool safety.

## HITL approval flow

Tools with `requires_human_approval=True` (e.g., `write_file`) trigger a round-trip:
1. `SafetyPipeline._run_hitl()` broadcasts `command: tool_approval` to frontend
2. Frontend shows approval dialog (modal overlay with approve/deny)
3. User choice sent back as `{type: "tool_approval", request_id, approved}`
4. Server `_handle_json()` routes to `ToolManager.submit_approval()` → `SafetyPipeline.submit_approval()`
5. Tool proceeds or returns PERMISSION_DENIED

## Adding a new backend

1. Create `<module>/your_backend.py` — implement the protocol ABC
2. Register in `registry.py` `_BACKENDS` dict with provider metadata
3. Export in `__init__.py` if the class should be public
4. Follow the existing pattern: `__init__` takes `model`, `api_key`, `base_url`, optional `logger`, creates async client

## Adding a new tool plugin

1. Create `core/tools/<name>/` directory with `plugin.py`, `config.yaml`, `__init__.py`, `README.md`
2. Implement `ToolPlugin` subclass in `plugin.py` — set `name`, `description`, `param_model`, `capabilities`, `safety`
3. Write `config.yaml` with SafetyProfile field overrides (optional — empty file if no config needed)
4. Re-export the plugin class from `__init__.py`
5. Add to `ALL_PLUGINS` list in `core/tools/__init__.py`

## Rules

1. readme.md用中文写。
2. 代码注释用中文
3. 使用conda环境.conda环境命令： conda activate Elysia
4. 所有敏感信息必须通过环境变量传递。# CLAUDE.md
