# Module

> 文档导航：[项目文档](../README.md)

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
├── system_prompt/        # System Prompt 引擎 + Section Provider 架构
├── tool/                 # 工具执行框架
├── database/             # 数据库后端
│   ├── vectordb/         # 向量数据库（Milvus）
│   ├── documentdb/       # 文档数据库（MongoDB）
│   └── graphdb/          # 图数据库（预留）
├── input/                # 音频输入 + WebSocket 服务器
├── task_system/          # 异步任务编排（Planner/Orchestrator/Executor）
└── bench/                # 延迟基准测试
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

> 📖 完整 API 文档 → [Module/llm/README.md](llm/README.md)

### STT — 语音识别

当前仅实现 SenseVoice（本地部署），OpenAI / DeepGram 为占位。

```python
from Module.stt import STTFactory

backend = STTFactory.create("sensevoice/sense-voice")
text = await backend.transcribe(audio_bytes)
await backend.close()
```

> 📖 完整 API 文档 → [Module/stt/README.md](stt/README.md)

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

> 📖 完整 API 文档 → [Module/tts/README.md](tts/README.md)

### Embedding — 文本向量化

支持本地 HuggingFace 模型和云端 OpenAI 兼容 API。

```python
from Module.embedding import EmbeddingFactory

backend = EmbeddingFactory.create("huggingface/BAAI/bge-large-zh-v1.5")
vec = await backend.embed_query("今天天气怎么样？")
vecs = await backend.embed_documents(["文本1", "文本2"])
await backend.close()
```

> 📖 完整 API 文档 → [Module/embedding/README.md](embedding/README.md)

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

> 📖 完整 API 文档 → [Module/output/README.md](output/README.md)

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

> 📖 完整 API 文档 → [Module/config/README.md](config/README.md)

### Logger — 日志工厂

支持 plain / JSON 双格式，按模块差异化日志级别，自动日志轮转。

```python
from Module.logger import LoggerFactory

factory = LoggerFactory(level="DEBUG", json_format=True)
logger = factory.get("MyModule")
```

> 📖 完整 API 文档 → [Module/logger/README.md](logger/README.md)

### Prompt — System Prompt 引擎 + 通用渲染器

提供两套 API：

- **SystemPromptManager** — Section Provider 架构：12 个独立 `PromptSection` 自管数据源/模板/生命周期，中央编排器按 `_RENDER_ORDER` 组装 system prompt
- **PromptManager** — 通用 Jinja2 渲染器，用于非 system prompt 的模板（如 `CharacterReflector.j2`）

```python
from Module.system_prompt import PromptManager

# 通用渲染（反射模板等）
pm = PromptManager("prompt/")
system = pm.render_macro("CharacterReflector.j2", "CharacterReflectorSystemPrompt",
                         character_name="Elysia", user_name="妖梦")
```
> System prompt 通过 `Module.system_prompt.system_prompt_manager.SystemPromptManager` + `sections/` 下的 12 个 PromptSection 渲染。

> 📖 完整 API 文档 → [Module/system_prompt/README.md](system_prompt/README.md)

### Tool — 工具执行框架

为 AI Agent 提供声明式、安全、可观测的工具执行系统。基于 ToolPlugin-ToolManager-SafetyPipeline 架构，支持 Pydantic 参数校验、安全检查、HITL 审批、并发控制、流式进度、热重载。

```python
from pydantic import BaseModel, Field
from Module.tool import ToolPlugin, ToolManager, SafetyProfile, Capability

class SearchParams(BaseModel):
    query: str = Field(..., description="搜索关键词")

class SearchPlugin(ToolPlugin):
    name = "web_search"
    description = "搜索互联网"
    param_model = SearchParams
    capabilities = {Capability.NETWORK}
    safety = SafetyProfile(network_allowed=True, max_concurrency=3)

    async def execute(self, *, ctx, emit=None, params: SearchParams) -> str:
        return await do_search(params.query)

tm = ToolManager()
await tm.load_all([SearchPlugin], PluginContext(session_id="demo"))
result = await tm.execute("web_search", ctx=ToolContext(), query="hello")
print(tm.definitions())  # OpenAI function calling schema
await tm.shutdown()
```

> 📖 完整 API 文档 → [Module/tool/README.md](tool/README.md)

### Schema — 共享数据模型

Schema 类型已迁移到 `core/schema/`，详见 [core/schema/README.md](../core/schema/README.md)。

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

> 📖 VectorDB 文档 → [Module/database/vectordb/README.md](database/vectordb/README.md)
> 📖 DocumentDB 文档 → [Module/database/documentdb/README.md](database/documentdb/README.md)

### Input — 音频输入 + WebSocket 服务器

`AudioBuffer` 管理麦克风 PCM 缓冲 + WebRTC VAD 语音活动检测。`server.py`（胶水组装）+ `ws_handler.py` + `api_routes.py` + `debug_routes.py` 提供 41 路由的 aiohttp 应用（WebSocket、REST API、静态文件、debug API）。

```python
from Module.input import AudioBuffer

buf = AudioBuffer()
buf.feed(pcm_bytes)          # 喂入 PCM 音频
if buf.is_speech_complete():  # VAD 检测到完整语音段
    wav = buf.to_wav()
```

> 📖 完整 API 文档 → [Module/input/README.md](input/README.md)

### TaskSystem — 异步任务编排

Planner/Orchestrator/Executor 三层架构，支持子 Agent 委托、DAG 依赖图、checkpoint 持久化、mailbox 轮询。

```python
from Module.task_system import TaskSystem

ts = TaskSystem(llm_adapter=llm, tools=tool_manager)
task_id = await ts.submit("帮我查一下今天的天气")
result = await ts.mailbox.wait(task_id, timeout=60)
```

> 📖 完整文档 → [Module/task_system/docs/](task_system/docs/)

## 测试

```bash
# 所有模块单元测试（mock，无需外部服务）
python -m pytest Module/ -v

# 单模块测试
python -m pytest Module/llm/tests/ -v
python -m pytest Module/tts/tests/ -v
python -m pytest Module/stt/tests/ -v
python -m pytest Module/embedding/tests/ -v
python -m pytest Module/output/tests/ -v
python -m pytest Module/config/tests/ -v
python -m pytest Module/system_prompt/tests/ -v
python -m pytest Module/logger/tests/ -v
python -m pytest Module/tool/tests/ -v
python -m pytest Module/database/vectordb/tests/ -v
python -m pytest Module/database/documentdb/tests/ -v
python -m pytest Module/input/ -v
python -m pytest Module/task_system/tests/ -v

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

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../README.md) | 快速开始 + 完整文档导航 |
| [docs/architecture.md](../docs/architecture.md) | 完整架构详解 + 数据流 |
| [docs/testing.md](../docs/testing.md) | 测试指南 |
