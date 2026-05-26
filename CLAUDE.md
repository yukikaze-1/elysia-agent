# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

`Module/` is a Python 3.13 library providing backend-agnostic interfaces for AI companion infrastructure: LLM, STT, TTS, embedding, vector/document/graph databases, output channels, and configuration management. It serves as the shared "module" layer consumed by the larger Elysia/Agent application (which lives outside this repo).

This repo has no build system (`pyproject.toml`, `setup.py`, etc.) — it's a dependency library imported by the parent project.

## Running tests

```bash
# Unit tests (all modules — mock-based, no external services needed)
python -m pytest Module/ -v

# Single module tests
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
- **registry** — `_BACKENDS` dict stores provider metadata (base_url, env_key, supports_prefix). `Factory.create("provider/model")` reads env vars, creates the appropriate backend instance, and injects a caller-aware logger.
- **backend** — Concrete implementation. All use `httpx.AsyncClient` or `openai.AsyncOpenAI` for async I/O. Streaming is bridged via `asyncio.Queue` + `create_task`.

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
| `Module.schema` | Shared data models | `UserMessage`, `ChatMessage`, `Event`, `EnvironmentMessage`, media types | — |
| `Module.database.vectordb` | Vector DB (ANN search) | `milvus` | — |
| `Module.database.documentdb` | Document DB | `mongodb` | — |
| `Module.database.graphdb` | Graph DB | stub only | — |
| `Module.bench` | Latency benchmarking | Covers LLM/TTS/STT streaming and non-streaming | — |

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

## Adding a new backend

1. Create `<module>/your_backend.py` — implement the protocol ABC
2. Register in `registry.py` `_BACKENDS` dict with provider metadata
3. Export in `__init__.py` if the class should be public
4. Follow the existing pattern: `__init__` takes `model`, `api_key`, `base_url`, optional `logger`, creates async client

## Rules

1. readme.md用中文写。
2. 代码注释用中文
3. 使用conda环境.conda环境命令： conda activate Elysia
4. 所有敏感信息必须通过环境变量传递。