> 文档导航：[项目文档](../../README.md)

# core/storage — 统一存储层

系统持久化中间层。Repository 模式封装 MongoDB/Milvus 操作，
消除业务代码对数据库的直接访问。

## 模块速查

| 文件 | 内容 |
|------|------|
| `base.py` | Repository ABC（connect / health_check / close） |
| `session_repo.py` | SessionRepository — MongoDB sessions 集合 |
| `message_repo.py` | MessageRepository — MongoDB message_archive 集合 |
| `token_repo.py` | TokenRepository — MongoDB token_usage 集合 |
| `memory_repo.py` | MemoryRepository — Milvus long_term_memory 集合 |
| `character_state_repo.py` | CharacterStateRepository — MongoDB session_state 集合 |

## 设计原则

- **Repository 模式**：每个集合一个 Repository，统一 connect/health_check/close 生命周期
- **类型化接口**：接受和返回 Pydantic/schema 类型，不暴露裸 dict
- **无 fallback 逻辑**：JSONL 备胎路径保留在调用方（SessionMemory / TokenTracker），Repository 只管数据库

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../README.md) | 快速开始 + 文档导航 |
| [core/schema/README.md](../schema/README.md) | 基础类型定义层 |
| [存储层设计 spec](../../docs/specs/2026-07-16-storage-layer-design.md) | 本模块的 spec |
