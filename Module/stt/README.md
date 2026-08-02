# STT Module

> 文档导航：[项目文档](../../README.md) → [Module 文档](../README.md)

统一的语音识别（Speech-to-Text）模块。提供 `STTBackend` 接口，通过 `STTFactory` 按 `provider/model` 标识创建后端实例，调用方不需要关心 API key、base URL 等细节。

## 文件结构

```
stt/
├── __init__.py          # 导出 STTBackend, STTFactory
├── __main__.py          # 真实 API 集成测试 (python -m Module.stt)
├── protocol.py          # STTBackend ABC
├── registry.py          # 后端注册表 + STTFactory.create() / from_config()
├── sensevoice.py        # SenseVoiceBackend — 本地 SenseVoice API
├── openai_stt.py        # OpenAICompatibleSTTBackend — OpenAI-compatible STT
├── config.py            # STTConfig — dataclass 配置
├── tests/              # 单元测试

└── README.md
```

## 快速开始

```python
from Module.stt import STTFactory

# 创建后端 — provider/model 格式
backend = STTFactory.create("sensevoice/sense-voice")

# 非流式
text = await backend.transcribe(audio_bytes)

# 流式（逐句产出识别结果）
async for chunk in backend.transcribe_stream(audio_bytes):
    print(chunk, end="")

await backend.close()
```

## 接口

```python
class STTBackend(ABC):
    async def transcribe(self, audio: bytes, **kwargs) -> str: ...
    async def transcribe_stream(self, audio: bytes, **kwargs) -> AsyncGenerator[str, None]: ...

    @property
    @abstractmethod
    def provider_name(self) -> str: ...   # "sensevoice" / "custom"

    @property
    def model(self) -> str: ...           # 完整标识 provider/model（拼接 _model），供 capabilities 下发
```

- `audio` — 原始音频 bytes（WAV 等格式，由后端决定支持哪些）
- `kwargs` — 透传 language / task 等参数，直接传给 API
- `provider_name` + `model` — 对齐 `TTSBackend`（`model` 返回 `provider_name/_model` 完整 id，capabilities 下发当前模型用；空 `_model` 返回 `""`）

## 支持的后端

| Provider | 模型示例 | API Key 来源 |
|----------|---------|-------------|
| `sensevoice` | `sensevoice/sense-voice` | 不需要 |
| `openai` | 占位，未实现（raises `NotImplementedError`） | `OPENAI_API_KEY` |
| `custom` | OpenAI-compatible Whisper API 格式，用户自定义端点 | 按端点配置 |
| `deepgram` | 占位，未实现 | `DEEPGRAM_API_KEY` |

可通过 `base_url` 参数覆盖默认 URL：

```python
backend = STTFactory.create("sensevoice/sense-voice", base_url="http://192.168.1.18:20042")
```

## 流式识别

`transcribe_stream()` 用于需要实时产出的场景。当前 SenseVoice 是整句识别（非真正流式），`transcribe_stream` 会 yield 完整结果作为单个 chunk。后续后端（如 DeepGram、OpenAI realtime）可实现真正的增量产出。调用方始终可以统一使用 `transcribe_stream` 获取结果流。

## Logger

每个 backend `__init__` 接受可选的 `logger` 参数：

```python
backend = STTFactory.create("sensevoice/sense-voice", logger=my_logger)
```

不传则默认使用无 handler 的静默 logger，调用方可通过 `Module/logger/LoggerFactory` 注入。

## Config

`STTConfig` dataclass 支持通过 YAML + 环境变量加载配置：

```python
from Module.config import ConfigLoader
from Module.stt import STTFactory
from Module.stt.config import STTConfig

cfg = ConfigLoader(STTConfig, yaml_path="config.yaml", yaml_key="STT").load()
backend = STTFactory.from_config("sensevoice/sense-voice", cfg)
```

| 字段 | 默认值 | 环境变量 | 说明 |
|------|--------|----------|------|
| `base_url` | `http://localhost:20042` | `STT_API_URL` | STT 服务端点 |

## 添加新后端

1. 在 `stt/` 下新建 `your_backend.py`
2. 继承 `STTBackend`，实现 `transcribe` 和 `transcribe_stream`
3. 在 `registry.py` 的 `_BACKENDS` 中注册

```python
from .protocol import STTBackend

class YourBackend(STTBackend):
    def __init__(self, model, *, api_key, base_url):
        ...

    async def transcribe(self, audio, **kwargs) -> str:
        ...

    async def transcribe_stream(self, audio, **kwargs):
        ...
```

## 测试

```bash
# 单元测试（mock，不需要 API）
python -m pytest Module/stt/tests/ -v

# 真实 API 集成测试（需要 SenseVoice 服务运行）
python -m Module.stt

# 统一延迟基准测试（含 LLM/TTS/STT 流式和非流式）
python -m Module.bench
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [core/input/README.md](../../core/input/README.md) | AudioBuffer + WebSocket（STT 输入源） |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 + 音频输入管道 |
