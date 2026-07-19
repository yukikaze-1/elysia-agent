# VectorDB Module

> 文档导航：[项目文档](../../../README.md) → [Module 文档](../../README.md) → [数据库文档](../README.md)

统一的向量数据库模块。提供 `VectorDBBackend` 接口，通过 `VectorDBFactory` 按 `provider/name` 标识创建后端实例。

## 文件结构

```
vectordb/
├── __init__.py          # 导出 VectorDBBackend, VectorDBFactory
├── __main__.py          # 真实 API 集成测试 (python -m Module.database.vectordb)
├── protocol.py          # VectorDBBackend ABC
├── registry.py          # 后端注册表 + VectorDBFactory.create() / from_config()
├── milvus.py            # MilvusBackend
├── config.py            # VectorDBConfig — dataclass 配置
├── tests/                 # 单元测试

└── README.md
```

## 快速开始

```python
from Module.database.vectordb import VectorDBFactory

backend = VectorDBFactory.create("milvus/micro_memory")

# 创建 collection
await backend.create_collection("micro_memory", dimension=1024)

# 插入
ids = await backend.insert(
    "micro_memory",
    vectors=[[0.1, 0.2, ...], [0.3, 0.4, ...]],
    metadata=[{"text": "doc1", "type": "micro"}, {"text": "doc2", "type": "micro"}],
)

# ANN 搜索
results = await backend.search(
    "micro_memory", query_vector, top_k=5,
    output_fields=["text", "type"],
)

# 标量查询
results = await backend.query(
    "micro_memory", filter_expr='type == "micro"', limit=10,
)

await backend.close()
```

## 接口

```python
class VectorDBBackend(ABC):
    async def create_collection(self, name: str, dimension: int, **schema_kwargs) -> None: ...
    async def drop_collection(self, name: str) -> None: ...
    async def collection_exists(self, name: str) -> bool: ...

    async def insert(self, collection: str, vectors: list[list[float]],
                     metadata: list[dict]) -> list[int]: ...

    async def search(self, collection: str, vector: list[float], *,
                     top_k: int = 10, filter_expr: str = "",
                     output_fields: list[str] | None = None) -> list[dict]: ...

    async def query(self, collection: str, *,
                    filter_expr: str, output_fields: list[str] | None = None,
                    limit: int = 10000) -> list[dict]: ...

    async def close(self) -> None: ...
```

## 支持的后端

| Provider | Backend Class | 默认 URI |
|----------|--------------|---------|
| `milvus` | `MilvusBackend` | `http://localhost:19530` |
| `pinecone` | 占位，未实现 | — |
| `qdrant` | 占位，未实现 | — |

## 与 Embedding 的关系

vectordb 只做向量 CRUD + ANN 搜索，不内嵌 Embedding。调用方需自行通过 `Module/embedding` 获取向量后传入。`create_collection` 的 `dimension` 应与 Embedding 模型的 `dimension` 一致。

## Logger

每个 backend `__init__` 接受可选的 `logger` 参数：

```python
backend = VectorDBFactory.create("milvus/micro_memory", logger=my_logger)
```

不传则默认使用无 handler 的静默 logger，调用方可通过 `Module/logger/LoggerFactory` 注入。

## Config

`VectorDBConfig` dataclass 支持通过 YAML + 环境变量加载配置：

```python
from Module.config import ConfigLoader
from Module.database.vectordb import VectorDBFactory
from Module.database.vectordb.config import VectorDBConfig

cfg = ConfigLoader(VectorDBConfig, yaml_path="config.yaml", yaml_key="VectorDB").load()
backend = VectorDBFactory.from_config("milvus/micro_memory", cfg)
```

| 字段 | 默认值 | 环境变量 | 说明 |
|------|--------|----------|------|
| `base_url` | `http://localhost:19530` | `MILVUS_URI` | Milvus 服务端点 |
| `token` | `""` | `MILVUS_TOKEN` | 认证 token（secret，仅从 env 读取） |

## 测试

```bash
# 单元测试（mock，不需要 Milvus）
python -m pytest Module/database/vectordb/tests/ -v

# 真实 API 集成测试（需要 Milvus 运行中）
python -m Module.database.vectordb
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [数据库文档](../README.md) | 数据库模块总览 |
| [Module 文档](../../README.md) | 所有 Module 服务总览 |
| [Module/embedding/README.md](../../embedding/README.md) | Embedding 模块（向量生产者） |
