# Database Module

统一的数据库模块，按数据库类型拆分子模块：

```
database/
├── vectordb/       # 向量数据库 — Milvus（已实现）、Pinecone/Qdrant（预留）
├── graphdb/        # 图数据库 — Neo4j（预留）
├── documentdb/     # 文档数据库 — MongoDB（预留）
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
