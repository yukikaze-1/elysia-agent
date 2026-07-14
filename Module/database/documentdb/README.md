# DocumentDB Module

> 文档导航：[项目文档](../../../README.md) → [Module 文档](../../README.md) → [数据库文档](../README.md)

统一的文档数据库模块。提供 `DocumentDBBackend` 接口，通过 `DocumentDBFactory` 按 `provider/database` 标识创建后端实例。

## 文件结构

```
documentdb/
├── __init__.py          # 导出 DocumentDBBackend, DocumentDBFactory
├── __main__.py          # 真实 API 集成测试 (python -m Module.database.documentdb)
├── protocol.py          # DocumentDBBackend ABC
├── registry.py          # 后端注册表 + DocumentDBFactory.create() / from_config()
├── mongodb.py           # MongoDBBackend — motor (async-native)
├── config.py            # DocumentDBConfig — dataclass 配置
├── test_documentdb.py   # 单元测试
├── test_config.py       # Config 测试
└── README.md
```

## 快速开始

```python
from Module.database.documentdb import DocumentDBFactory

backend = DocumentDBFactory.create("mongodb/elysia")

# 插入
doc_id = await backend.insert_one("chat_history", {
    "message_id": "...",
    "session_id": "妖梦_Elysia",
    "role": "user",
    "content": "你好",
    "timestamp": 1715779200.0,
    "type": "text",
})

# 批量插入
ids = await backend.insert_many("chat_history", [doc1, doc2, doc3])

# 分页查询
docs = await backend.find(
    "chat_history",
    {"session_id": "妖梦_Elysia"},
    sort=[("timestamp", -1)],
    limit=50,
)

# 计数
n = await backend.count("chat_history", {"session_id": "妖梦_Elysia"})

await backend.close()
```

## 接口

```python
class DocumentDBBackend(ABC):
    async def insert_one(self, collection: str, document: dict) -> str: ...
    async def insert_many(self, collection: str, documents: list[dict]) -> list[str]: ...
    async def find(self, collection: str, filter: dict, *,
                   sort=None, limit=100, skip=0) -> list[dict]: ...
    async def update_one(self, collection: str, filter: dict, update: dict) -> int: ...
    async def delete_many(self, collection: str, filter: dict) -> int: ...
    async def count(self, collection: str, filter: dict) -> int: ...
    async def ensure_index(self, collection: str, keys: list[tuple], **kwargs) -> str: ...
    async def close(self) -> None: ...
```

## 支持的后端

| Provider | Backend Class | 默认 URI |
|----------|--------------|---------|
| `mongodb` | `MongoDBBackend` | `mongodb://localhost:27017` |

可通过 `uri` 参数或 `MONGODB_URI` 环境变量覆盖。

## Logger

每个 backend `__init__` 接受可选的 `logger` 参数：

```python
backend = DocumentDBFactory.create("mongodb/elysia", logger=my_logger)
```

不传则默认使用无 handler 的静默 logger，调用方可通过 `Module/logger/LoggerFactory` 注入。

## Config

`DocumentDBConfig` dataclass 支持通过 YAML + 环境变量加载配置：

```python
from Module.config import ConfigLoader
from Module.database.documentdb import DocumentDBFactory
from Module.database.documentdb.config import DocumentDBConfig

cfg = ConfigLoader(DocumentDBConfig, yaml_path="config.yaml", yaml_key="DocumentDB").load()
backend = DocumentDBFactory.from_config("mongodb/elysia", cfg)
```

| 字段 | 默认值 | 环境变量 | 说明 |
|------|--------|----------|------|
| `base_url` | `mongodb://localhost:27017` | `MONGODB_URI` | MongoDB 连接串 |

## 测试

```bash
# 单元测试（mock，不需要 MongoDB）
python -m pytest Module/database/documentdb/test_documentdb.py -v

# 真实 API 集成测试（需要 MongoDB 运行中）
python -m Module.database.documentdb
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [数据库文档](../README.md) | 数据库模块总览 |
| [Module 文档](../../README.md) | 所有 Module 服务总览 |
| [docs/architecture.md](../../../docs/architecture.md) | SessionMemory + 消息归档架构 |
