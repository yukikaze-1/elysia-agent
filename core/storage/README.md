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
| `user_identity_repo.py` | UserIdentityRepository — MongoDB user_identity_profiles 集合（BE-STAGEH-02 用户身份权威：唯一 user_id 索引 + `$setOnInsert` bootstrap + revision CAS，fail-closed） |
| `memory_repo.py` | MemoryRepository — Milvus long_term_memory 集合 |
| `generation_repo.py` | GenerationRepository — MongoDB generations 集合（BE-024 生命周期） |
| `character_version_repo.py` | CharacterVersionRepository — MongoDB character_versions 集合（L2/C6 版本历史） |
| `attachment_repo.py` | AttachmentRepository — MongoDB attachments 集合（BE-070 fail-closed：Mongo 离线 raise StorageUnavailableError → 503；refs/ref_count + 软删 status/deleted_at） |
| `desktop_call_repo.py` | DesktopCallRepository — MongoDB desktop_calls 集合（批次 4 BE-041/043：桌面工具调用注册表，进程内缓存读权威） |
| `idempotency.py` | v2 消息幂等运行时存储（内存权威 overlay + Mongo 写穿 + recover 四分支，CORE-MOD-P1-01 从 input 迁入） |
| `idempotency_repo.py` | IdempotencyRepository — MongoDB idempotency 集合（三态 claim + 唯一约束 + TTL，D6c + 2026-08-12） |
| `secret_store.py` | SecretStore — 用户密钥本地磁盘存储（JSON 文件，0700/0600 权限 + 原子写，批次 5；非 Repository 子类） |
| `attachment_gc.py` | AttachmentGC — 附件 GC 兜底扫描（BE-071 四规则：软删超 24h 物理删 / active 缺文件软删 / 未关联超 7 天软删 / 磁盘孤儿删文件） |
| `pending_attachment_release.py` | PendingAttachmentRelease — 提交后附件引用释放失败 journal（#41：主操作已提交但 refs 未释放 → 落盘，启动 + Mongo 恢复边界重试；原子写、空则删文件） |
| `background_task_repo.py` | BackgroundTaskResultRepository — MongoDB background_task_results 集合（#6 决策 #6：`task_id` 唯一 + 7d TTL，fail-closed；停机落库 + 启动补投） |
| `task_result_wal.py` | TaskResultWAL — 后台任务终态结果本地 WAL（#6 硬杀窗口：追加式 JSONL + 同步 fsync，派发/终态各一行；启动 replay 进仓储，库为权威。非 Repository 子类——无连接概念，写失败只降级不抛） |
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
