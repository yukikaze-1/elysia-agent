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
| `generation_repo.py` | GenerationRepository — MongoDB generations 集合（BE-024 生命周期） |
| `character_version_repo.py` | CharacterVersionRepository — MongoDB character_versions 集合（L2/C6 版本历史） |
| `attachment_repo.py` | AttachmentRepository — MongoDB attachments 集合（BE-070 fail-closed：Mongo 离线 raise StorageUnavailableError → 503；refs/ref_count + 软删 status/deleted_at） |
| `desktop_call_repo.py` | DesktopCallRepository — MongoDB desktop_calls 集合（批次 4 BE-041/043：桌面工具调用注册表，进程内缓存读权威） |
| `secret_store.py` | SecretStore — 用户密钥本地磁盘存储（JSON 文件，0700/0600 权限 + 原子写，批次 5；非 Repository 子类） |
| `attachment_gc.py` | AttachmentGC — 附件 GC 兜底扫描（BE-071 四规则：软删超 24h 物理删 / active 缺文件软删 / 未关联超 7 天软删 / 磁盘孤儿删文件） |
| `attachment_release.py` | 附件引用释放共享 helpers（会话/消息删除时释放附件 refs，批次 8a D5；release_message/messages/all/session_attachments） |

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
| [存储层设计 spec](../../docs/superpowers/specs/archive/2026-07/2026-07-16-storage-layer-design.md) | 本模块的 spec |
