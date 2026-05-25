# Module

AI 伴侣基础设施的 Python 3.13 共享库。为上层 Elysia/Agent 应用提供 LLM、STT、TTS、Embedding、数据库、输出通道和配置管理等模块，每个模块通过统一的 Protocol-Registry-Backend 模式隔离后端差异。

## 模块结构

```
Module/
├── llm/                  # 大模型调用
├── stt/                  # 语音识别
├── tts/                  # 语音合成
├── embedding/            # 文本向量化
├── output/               # 多通道输出
├── config/               # 配置基础设施
├── logger/               # 日志工厂
├── prompt/               # Jinja2 模板引擎
├── schema/               # 共享数据模型
├── database/             # 数据库后端
│   ├── vectordb/         # 向量数据库（Milvus）
│   ├── documentdb/       # 文档数据库（MongoDB）
│   └── graphdb/          # 图数据库（预留）
└── bench/                # 统一延迟基准测试
```

## 架构：Protocol - Registry - Backend

每个服务模块遵循相同的三层模式：

```
Module/<service>/
├── protocol.py    # ABC 抽象接口
├── registry.py    # 工厂 + _BACKENDS 注册表
├── <backend>.py   # 具体后端实现
├── config.py      # dataclass 配置
├── __init__.py    # 导出 Backend / Factory / Config
└── __main__.py    # 集成冒烟测试
```

- **protocol** — 异步 ABC，所有后端都提供流式和非流式两条路径（如 `chat` / `chat_stream`、`synthesize` / `synthesize_stream`）
- **registry** — `_BACKENDS` 字典存储 provider 元数据（base_url、env_key 等），`Factory.create("provider/model")` 读取环境变量、创建后端实例、注入调用方 logger
- **backend** — 统一使用 `httpx.AsyncClient` 或 `openai.AsyncOpenAI`，流式通过 `asyncio.Queue` + `create_task` 桥接

## 各模块概览

### LLM — 大模型调用

支持 DeepSeek、OpenAI、Ollama，通过 OpenAI 兼容协议统一接入。内置 DeepSeek prefix continuation（自动路由至 beta 端点，强制 JSON 输出）。

```python
from Module.llm import LLMFactory

backend = LLMFactory.create("deepseek/deepseek-v4-pro")
reply = await backend.chat([{"role": "user", "content": "你好"}])
async for chunk in backend.chat_stream(messages):
    print(chunk, end="")
await backend.close()
```

### STT — 语音识别

当前仅实现 SenseVoice（本地部署），OpenAI / DeepGram 为占位。

```python
from Module.stt import STTFactory

backend = STTFactory.create("sensevoice/sense-voice")
text = await backend.transcribe(audio_bytes)
await backend.close()
```

### TTS — 语音合成

对接 GPT-SoVITS 服务端，输出 32000Hz / mono / int16 WAV。音色通过 `gpt_sovits_voices.yaml` 配置。

```python
from Module.tts import TTSFactory

tts = TTSFactory.create("gpt-sovits/default", base_url="http://192.168.1.18:9880")
wav = await tts.synthesize("你好呀。", character="Elysia")
async for chunk in tts.synthesize_stream("你好呀。", character="Elysia"):
    websocket.send_bytes(chunk)
await tts.close()
```

### Embedding — 文本向量化

支持本地 HuggingFace 模型和云端 OpenAI 兼容 API。

```python
from Module.embedding import EmbeddingFactory

backend = EmbeddingFactory.create("huggingface/BAAI/bge-large-zh-v1.5")
vec = await backend.embed_query("今天天气怎么样？")
vecs = await backend.embed_documents(["文本1", "文本2"])
await backend.close()
```

### Output — 多通道输出

`OutputManager` 向多个 `OutputChannel` 广播，按 `ChannelCapability` 过滤。内置 `ConsoleChannel`（终端）和 `WebSocketChannel`（JSON 消息）。

```python
from Module.output import OutputManager, ConsoleChannel, WebSocketChannel

mgr = OutputManager()
mgr.add_channel(ConsoleChannel())
mgr.add_channel(WebSocketChannel(ws))

await mgr.typing(True)
await mgr.stream(llm.chat_stream(messages), finalize=lambda text: ChatMessage(...))
await mgr.typing(False)
```

### Config — 配置基础设施

基于 dataclass 的声明式配置，支持 `YAML + 环境变量 + 校验` 三级加载。

```python
from dataclasses import dataclass
from Module.config import ConfigField, ConfigLoader, Range

@dataclass
class MyConfig:
    host: str = ConfigField(default="0.0.0.0")
    port: int = ConfigField(default=8000, constraints=[Range(1, 65535)])
    api_key: str = ConfigField(default="", secret=True, env="MY_API_KEY")

cfg = ConfigLoader(MyConfig, yaml_path="config.yaml").load()
```

优先级：`环境变量 > YAML > dataclass 默认值`

### Logger — 日志工厂

支持 plain / JSON 双格式，按模块差异化日志级别，自动日志轮转。

```python
from Module.logger import LoggerFactory

factory = LoggerFactory(level="DEBUG", json_format=True)
logger = factory.get("MyModule")
```

### Prompt — Jinja2 模板引擎

管理 prompt 模板目录，支持整文件渲染和命名 macro 渲染，内置 `datetime` filter。

```python
from Module.prompt import PromptManager

pm = PromptManager("prompt/")
system = pm.render_macro("Brain.j2", "BrainSystemPrompt", character_name="Elysia")
```

### Schema — 共享数据模型

定义全链路通用的数据结构：

| 类型 | 用途 |
|------|------|
| `UserMessage` | 各输入端（WebSocket / CLI / Telegram）用户消息的统一抽象 |
| `EnvironmentMessage` | 传感器 / 系统事件输入 |
| `ChatMessage` | 系统内通用消息，所有层之间流通 |
| `Event` | 事件总线消息 |
| `AudioData / ImageData / VideoData / FileData` | 媒体数据模型 |

### Database — 数据库后端

- **VectorDB** — 向量 CRUD + ANN 搜索，已实现 Milvus
- **DocumentDB** — 文档 CRUD，已实现 MongoDB
- **GraphDB** — 图数据库接口预留（Neo4j）

```python
from Module.database.vectordb import VectorDBFactory

backend = VectorDBFactory.create("milvus/micro_memory")
await backend.create_collection("micro_memory", dimension=1024)
await backend.insert("micro_memory", vectors, metadata)
results = await backend.search("micro_memory", query_vector, top_k=10)
```

## 测试

```bash
# 所有模块单元测试（mock，无需外部服务）
python -m pytest Module/ -v

# 单模块测试
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

# 集成冒烟测试（需要对应服务运行）
python -m Module.llm          # 需要 DEEPSEEK_API_KEY
python -m Module.stt          # 需要 SenseVoice 在 localhost:20042
python -m Module.tts          # 需要 GPT-SoVits 在 localhost:9880
python -m Module.embedding    # 需要 OPENAI_API_KEY
python -m Module.output       # 自包含，无需外部服务
python -m Module.bench        # 统一延迟基准测试（LLM + TTS + STT）
```

## 添加新后端

1. 创建 `<module>/your_backend.py`，继承 protocol ABC 并实现所有抽象方法
2. 在 `registry.py` 的 `_BACKENDS` 字典中注册 provider 元数据
3. 如需公开导出，在 `__init__.py` 中添加

后端类统一以 `Backend` 结尾，构造器接受 `model`、`api_key`、`base_url`、可选的 `logger`，自行创建并管理 async client 生命周期。
