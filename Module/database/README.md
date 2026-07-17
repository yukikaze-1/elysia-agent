# Database Module

> 文档导航：[项目文档](../../README.md) → [Module 文档](../README.md)

统一的数据库模块，按数据库类型拆分子模块：

```
database/
├── vectordb/       # 向量数据库 — Milvus（已实现）、Pinecone/Qdrant（预留）
├── graphdb/        # 图数据库 — Neo4j（预留）
├── documentdb/     # 文档数据库 — MongoDB（已实现）
└── README.md
```

各子模块独立维护自己的 ABC、后端实现、registry、测试。

## 当前状态

| 子模块 | 状态 | 后端 |
|--------|:--:|------|
| `vectordb` | 可用 | Milvus |
| `graphdb` | 预留 | — |
| `documentdb` | 可用 | MongoDB |

详见各子模块的 README。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [Module 文档](../README.md) | 所有 Module 服务总览 + 架构模式 |
| [VectorDB 文档](vectordb/README.md) | Milvus 向量数据库 |
| [DocumentDB 文档](documentdb/README.md) | MongoDB 文档数据库 |
| [docs/architecture.md](../../docs/architecture.md) | 记忆与存储架构 |
