# Embedding Module

> 文档导航：[项目文档](../../README.md) → [Module 文档](../README.md)

统一的文本向量化模块。提供 `EmbeddingBackend` 接口，通过 `EmbeddingFactory` 按 `provider/model` 标识创建后端实例，支持本地 HuggingFace 模型和云端 OpenAI 兼容 API。

## 文件结构

```
embedding/
├── __init__.py            # 导出 EmbeddingBackend, EmbeddingFactory
├── __main__.py            # 真实 API 集成测试 (python -m Module.embedding)
├── protocol.py            # EmbeddingBackend ABC
├── registry.py            # 后端注册表 + EmbeddingFactory（内置缓存 + shutdown()）
├── huggingface.py         # HuggingFaceBackend — 本地 sentence-transformers
├── openai_compat.py       # OpenAICompatEmbeddingBackend — OpenAI/Cohere 兼容协议
├── config.py              # EmbeddingConfig — dataclass 配置
├── tests/                 # 单元测试

└── README.md
```

## 快速开始

```python
from Module.embedding import EmbeddingFactory

# 本地模型（首次调用创建并缓存，后续命中缓存）
backend = EmbeddingFactory.create("huggingface/BAAI/bge-large-zh-v1.5")

# 云端模型
backend = EmbeddingFactory.create("openai/text-embedding-3-large")

# 单条文本 → 单条向量（用于搜索）
vec = await backend.embed_query("今天天气怎么样？")

# 批量文本 → 向量列表（用于插入）
vecs = await backend.embed_documents(["文本1", "文本2", "文本3"])

# 不再使用时：只解引用，不释放模型
backend = None

# 程序退出时：统一释放所有缓存的模型
await EmbeddingFactory.shutdown()
```

## 接口

```python
class EmbeddingBackend(ABC):
    @property
    def dimension(self) -> int: ...

    async def embed_documents(self, texts: list[str], **kwargs) -> list[list[float]]: ...

    async def embed_query(self, text: str, **kwargs) -> list[float]: ...
```

- `dimension` — 模型输出的向量维度（如 1024、3072），供向量数据库创建 schema
- `embed_documents(texts)` — 批量文本 → 向量列表
- `embed_query(text)` — 单条文本 → 单个向量（等价于 `embed_documents([text])[0]`）
- `kwargs` — 透传参数（如 OpenAI 的 `dimensions` 参数可缩减维度）

## Factory 缓存与生命周期

`EmbeddingFactory.create()` 内部维护类级缓存 `_instances`，同一 `provider/model` 只创建一次。后续调用命中缓存直接返回，避免重复加载模型（本地模型 ~3GB）。

```
首次: Factory.create("huggingface/BAAI/bge-large-zh-v1.5") → 创建 → 缓存 → 返回
后续: Factory.create("huggingface/BAAI/bge-large-zh-v1.5") → 命中缓存 → 返回同一实例
```

**调用方不应手动 `close()` 缓存的实例**（会破坏共享）。程序退出前调用 `Factory.shutdown()` 统一释放所有缓存模型：

```python
await EmbeddingFactory.shutdown()  # 释放所有缓存的模型 + CUDA cache 清理
```

`shutdown()` 幂等，多次调用安全。

## 支持的后端

| Provider | 模型示例 | dimension | API Key |
|----------|---------|:---------:|---------|
| `huggingface` | `BAAI/bge-large-zh-v1.5` | 1024 | 不需要 |
| `openai` | `text-embedding-3-large` | 3072 | `OPENAI_API_KEY` |
| `openai` | `text-embedding-3-small` | 1536 | `OPENAI_API_KEY` |

未知的 HuggingFace 模型可通过 `dimension=` 参数显式指定维度。

## HuggingFace 后端细节

- 本地模型推理（CPU/CUDA），自动检测设备
- 模型缓存于 `~/.cache/huggingface/`
- 同步推理通过 `asyncio.to_thread()` 包装为异步
- 默认启用 L2 归一化（`normalize_embeddings=True`）
- 无网络开销，适合本地部署

## OpenAI 后端细节

- 使用 `openai.AsyncOpenAI`，与 LLM 模块共用
- `POST /v1/embeddings`
- `dimension` 从首次 API 响应自动获取
- 支持 `dimensions` 参数缩减输出维度（API native 功能）

## Logger

每个 backend `__init__` 接受可选的 `logger` 参数：

```python
backend = EmbeddingFactory.create("openai/text-embedding-3-large", logger=my_logger)
```

不传则默认使用无 handler 的静默 logger，调用方可通过 `Module/logger/LoggerFactory` 注入。

## Config

`EmbeddingConfig` dataclass 支持通过 YAML + 环境变量加载配置：

```python
from Module.config import ConfigLoader
from Module.embedding import EmbeddingFactory
from Module.embedding.config import EmbeddingConfig

cfg = ConfigLoader(EmbeddingConfig, yaml_path="config.yaml", yaml_key="Embedding").load()
backend = EmbeddingFactory.from_config("openai/text-embedding-3-large", cfg)
```

| 字段 | 默认值 | 环境变量 | 说明 |
|------|--------|----------|------|
| `base_url` | `https://api.openai.com/v1` | `OPENAI_API_BASE` | OpenAI 兼容 API 端点 |
| `api_key` | `""` | `OPENAI_API_KEY` | API 密钥（secret，仅从 env 读取） |

## 添加新后端

1. 在 `embedding/` 下新建 `your_backend.py`
2. 继承 `EmbeddingBackend`，实现 `dimension`、`embed_documents`、`embed_query`
3. 在 `registry.py` 的 `_BACKENDS` 中注册

```python
from .protocol import EmbeddingBackend

class YourBackend(EmbeddingBackend):
    def __init__(self, *, model, api_key, base_url):
        ...

    @property
    def dimension(self) -> int:
        ...

    async def embed_documents(self, texts, **kwargs) -> list[list[float]]:
        ...

    async def embed_query(self, text, **kwargs) -> list[float]:
        ...
```

## 测试

```bash
# 单元测试（mock，不需要 API key）
python -m pytest Module/embedding/tests/ -v

# 真实 API 集成测试
python -m Module.embedding
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [Module/database/vectordb/README.md](../database/vectordb/README.md) | Milvus 向量数据库（Embedding 消费者） |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 + LongTermMemory |
