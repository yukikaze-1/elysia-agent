# 数据库存储表格

项目共有 **2 个持久化数据库**（Milvus 向量库 + MongoDB 文档库），共 **14 个集合**（Milvus 2 + MongoDB 12），外加若干内存/文件存储结构。没有使用 SQL 数据库。

| 集合 | 位置 | 用途 | 保留策略 |
|------|------|------|----------|
| `long_term_memory` | Milvus | 记忆摘要 v1（向量搜索；现仅作未注入 repo 时的回退路径） | 1万条数量上限（超限驱逐，同步删除 Milvus）；max_age_days=90 预留未使用 |
| `long_term_memory_v2` | Milvus | 记忆摘要 v2（**产品记忆实际落库集合**，scope 隔离） | 1万条数量上限（超限驱逐，同步删除 Milvus）；max_age_days=90 预留未使用 |
| `sessions` | MongoDB | LLM 上下文窗口 | 无自动 TTL——`max_messages` 截断，删除走 REST/debug 显式级联删除 |
| `message_archive` | MongoDB | 对话消息永久归档 | 永久, 只增不删 |
| `token_usage` | MongoDB | LLM token 统计 | 永久, 只增不删 |
| `character_versions` | MongoDB | L2/C6 性格版本历史 | 永久，per content_type 独立计数 |
| `characters` | MongoDB | 角色 Definition（`character-contract-v1`） | 永久；`character_id` 唯一索引，`status=archived` 软删 |
| `character_revisions` | MongoDB | 角色 Revision 快照（append-only） | 永久；`(character_id, revision)` 唯一索引 |
| `character_instances` | MongoDB | 角色实例 + `CharacterRuntimeState` | 永久；`instance_id` 唯一 + canonical 部分唯一索引 |
| `user_identity_profiles` | MongoDB | 用户最小身份（BE-STAGEH-02，Mongo 权威） | 永久；`user_id` 唯一索引，revision CAS |
| `generations` | MongoDB | generation 生命周期状态（BE-024） | 30天 TTL（按真实 BSON Date `ttl_expires_at`） |
| `attachments` | MongoDB | 附件元数据（BE-070，fail-closed） | TTL 不设，生命周期由 GC 管（软删 24h / 未关联 7d） |
| `desktop_calls` | MongoDB | 桌面工具调用状态（批次 4） | 30天 TTL（按真实 BSON Date `ttl_expires_at`） |
| `idempotency` | MongoDB | `clientRequestId` 幂等 claim（D6c，2026-08-11） | 1h TTL（`expires_at` BSON Date，TTL 索引）；`(conversation_id, client_request_id)` 唯一约束防并发双写 |

---

## 一、Milvus 向量数据库

**连接**: 优先级 环境变量 → `config.yaml` → 默认值。YAML 路径 `database.milvus_uri` / `database.milvus_token`，环境变量 `MILVUS_URI`（默认 `http://localhost:19530`）/ `MILVUS_TOKEN`。
**认证（P1-19）**: docker-compose 的 Milvus 默认开启授权（`common.security.authorizationEnabled=true`），root 密码由 `.env` 的 `MILVUS_ROOT_PASSWORD` 提供；应用侧 `MILVUS_TOKEN=root:<同一密码>` 必须设置，否则连接被拒。`setup.sh` 首次安装会自动生成并写入 `.env`。

### 集合: `long_term_memory`（v1，回退路径）

> **v2 起产品记忆实际写 `long_term_memory_v2`**（见下节）。本集合仅在未注入 repo 时使用——`LongTermMemory.connect()` 在 `repo=None` 时自建 `MemoryRepository`（`core/memory/long_term.py:239-241`），以及 `clear_all()` 的临时 repo 兜底（`core/memory/long_term.py:791-793`）。生产路径由 `MemoryRepositoryV2` 覆盖。

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INT64（主键，自增） | 自动生成 |
| `embedding` | FLOAT_VECTOR[1024]（openai/text-embedding-3-small）/ 1024（BAAI/bge-large-zh-v1.5 fallback） | 文本嵌入向量，主后端 openai，BGE 为离线 fallback |
| `text` | VARCHAR（动态字段） | 记忆文本内容 |
| `memory_type` | VARCHAR（动态字段） | 类型：`Fact` / `Preference` / `Event` / `Plan` / `Note`。写入经 `MemoryType.coerce` 归一（lowercase/大小写混 → TitleCase，未知 → `Note`），存储恒为规范值 |
| `poignancy` | INT（动态字段） | 重要性 1-10 |
| `timestamp` | FLOAT（动态字段） | Unix 时间戳 |

- **索引**: IVF_FLAT，metric_type=L2，nlist=1024
- **metric 持久化（P2-10）**: metric_type（L2/COSINE/IP）持久化于 Milvus index params——create_collection 时写入，`load_collection`/`search` 时从 Milvus index 描述读取恢复（跨实例/重启保持排序语义）。读取失败、index 缺失或 metric 不受支持时抛 ValueError（fail closed），禁止静默默认 L2 后返回看似正常的排序。白名单见 `Module/database/vectordb/milvus.py` 的 `SUPPORTED_METRIC_TYPES`。
- **bulk insert 校验（P2-10）**: 调用 Milvus 前校验 vectors/metadata 非空、数量相等、各向量维度一致；不合法输入抛 ValueError 且零写入（不做 `zip` 静默截断）。
- **检索方式**: ANN 向量搜索 + 重排序（0.5 × 相似度 + 0.3 × 重要性 + 0.2 × 时效性）
- **淘汰策略**: max_entries=10000 数量上限驱逐（保留最新 N 条，驱逐时同步删除 Milvus）；max_age_days=90 当前未使用（预留供后续实现）
- **降级**: Milvus 不可用时回退到内存列表 + 文本重叠搜索

### 集合: `long_term_memory_v2`（v2，产品记忆实际落库）

`MemoryRepositoryV2`（`core/storage/memory_repo_v2.py:13`，`COLLECTION = "long_term_memory_v2"`）——`MemoryRepository` 的子类，仅遮蔽 `COLLECTION` 名，复用父类全部 Milvus 操作。由 `Agent.__init__` 实例化（`core/agent/agent.py:167-170`）并以 `repo=` 注入 `LongTermMemory`（`core/agent/agent.py:198-204`），故运行时产品记忆（含 `ScopedLongTermMemory` 视图）全部读写本集合。引入于 2026-09-05（阶段 F）。

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INT64（主键，自增） | 自动生成（Milvus PK） |
| `embedding` | FLOAT_VECTOR[1024] | 文本嵌入向量（openai 主后端，BGE 离线 fallback） |
| `entry_id` | VARCHAR（动态字段） | 记忆 ID（`mem_` 前缀），`_load_from_milvus` 据此幂等去重（连续 connect 不倍增） |
| `text` | VARCHAR（动态字段） | 记忆文本内容 |
| `memory_type` | VARCHAR（动态字段） | 同 v1：`Fact` / `Preference` / `Event` / `Plan` / `Note`，写经 `MemoryType.coerce` 归一 |
| `poignancy` | INT（动态字段） | 重要性 1-10 |
| `timestamp` | FLOAT（动态字段） | Unix 时间戳 |
| `user_id` | VARCHAR（动态字段，scope） | 记忆归属用户（`core/memory/scope.py` `MemoryScope.user_id`） |
| `character_instance_id` | VARCHAR（动态字段，scope） | 记忆归属角色实例（`MemoryScope.character_instance_id`） |
| `visibility` | VARCHAR（动态字段，scope） | `character_private`（产品记忆一律此值）/ `user_shared`（预留，无自动升级路径）。写入点为 `LongTermMemory.insert()`（`core/memory/long_term.py:491-501`）与 `sync_dirty_entries()`（`core/memory/long_term.py:640-650`） |

- **索引**: 与 v1 同——IVF_FLAT，metric_type=L2，nlist=1024（`create_collection` 经 `MemoryRepository.connect()` 继承复用，index_preset=`default`，维度 1024）。scope 字段（`user_id` / `character_instance_id` / `visibility`）为 Milvus 动态字段，检索时经 `filter_expr` 做 ANN 阶段过滤（`core/memory/long_term.py:90-96`），非 ANN 召回后再 Python 过滤——避免其他 scope 条目挤占 top-k 导致本 scope 召回丢失（架构稿 §8.3）。
- **scope 隔离**: 写/读均经 `MemoryScope` 派生（`ScopedLongTermMemory`，`core/memory/scoped_long_term.py`），`scope=None` 为宽容路径（测试/内部），生产路径恒带 scope；不存在「漏传即全局」默认值。
- **降级/持久化补齐**: 与 v1 共用 `_dirty_ids` 内存同步 + 离线 journal（`pending_syncs`）与删除 tombstone（`pending_deletes`）——Milvus 离线时记忆落 `data/` journal，重连后补写/清理。

---

## 二、MongoDB 文档数据库

**连接**: 优先级 环境变量 → `config.yaml` → 默认值。YAML 路径 `database.mongodb_uri`，环境变量 `MONGODB_URI`（默认 `mongodb://localhost:27017`）。
**认证（P1-19）**: docker-compose 的 Mongo 以 `--auth` 启动（`MONGO_INITDB_ROOT_USERNAME`/`MONGO_INITDB_ROOT_PASSWORD`，见 `.env`），应用侧 `MONGODB_URI` 必须携带用户名密码，如 `mongodb://elysia:<密码>@127.0.0.1:27017/default?authSource=admin`。`setup.sh` 首次安装会自动改写 `.env` 中的默认 `MONGODB_URI`。

### 集合: `sessions`（数据库: `default`，可配置）

| 字段 | 类型 | 说明 |
|------|------|------|
| `_id` | ObjectId | MongoDB 自动生成 |
| `session_id` | String | 会话唯一标识 |
| `messages` | Array\<Object\> | 消息数组 |
| `character_state` | Object | CharacterState 序列化（mood + personality L2/L3 + reflector buffer/context + awareness 冷却 + state_history） |
| `reflector_state` | Object | MemoryReflectorState 序列化（round_count / new_memory_count）。save() 时经 `set_memory_reflector()` 同步派生（session.py:261） |
| `service_state` | Object | 服务运行时状态（批次 3）：当前模型 + STT/TTS 启用模式（enabled 三态 `bool \| None`，None 回退 config；`agent.set_service` / `switch_*` 写、启动经 `_apply_service_state` 应用） |
| `params` | Object | 运行时参数（legacy，向后兼容读取 mood/mood_intensity） |
| `updated_at` | String（ISO 格式） | 最后更新时间 |

**messages 子文档结构**（新格式）:

```json
{
  "id": "...",
  "role": "user | assistant",
  "parts": [...],
  "content_type": "..."
}
```

**旧格式**: `{"role": "...", "content": "...", "meta": {...}}`

**索引**:

| 索引 | 方向 | 说明 |
|------|------|------|
| `session_id` | ASC | 会话查询 |
| `updated_at` | DESC | 按时间排序 |

> 2026-08-10 整改后 `sessions` **不再建 Mongo TTL 索引**（ISO 字符串字段不会触发 TTL，且会话删除需走级联删除协议）；旧文档中的「30 天 TTL」已失效。清理通过 `DELETE /api/sessions/{id}` 与 debug 会话删除显式执行。

**操作**: 原子 upsert（`update_one(upsert=True)`）
**降级**: MongoDB 不可用时纯内存运行（deque，maxlen 默认 20，config.yaml session_max_messages 可配）

### 集合: `message_archive`（数据库: `default`）

所有对话消息的永久归档。与 `sessions` 职责分离：session 管 LLM 上下文窗口（截断保留），archive 管持久化（只增不删）。

| 字段 | 类型 | 说明 |
|------|------|------|
| `_id` | ObjectId | MongoDB 自动生成 |
| `role` | String | `user` / `assistant` / `tool` |
| `content` | Array\<Object\> | TextPart 数组 `[{"type": "text", "content": "..."}]` |
| `parts` | Array\<Object\> | 客户端友好格式（写入时不含，API 查询时补） |
| `timestamp` | String（ISO 格式） | 消息时间 |
| `inner_voice` | String? | assistant 的内心独白 |
| `token_usage` | TokenUsageSummary? | assistant 的 token 统计（rounds: TokenUsage[], turn_total: int） |
| `tool_call_id` / `name` / `success` / `error` | — | tool 角色专用字段 |

**索引**:
| 索引 | 说明 |
|------|------|
| `_id` | MongoDB 内置（默认降序扫描） |
| `session_id` ASC | 按会话查询 |
| `(session_id, _id)` 复合 | 会话内消息排序 |

无 TTL，不截断。

**操作**: `insert_one`（fire-and-forget），`find`（分页游标），`delete_many({})`（debug 清空）。

### 集合: `token_usage`（数据库: `default`）

每次 LLM 调用的 token 统计日志，只增不删。

| 字段 | 类型 | 说明 |
|------|------|------|
| `session_id` | String | 会话标识 |
| `model` | String | LLM 模型名 |
| `input_tokens` | Int | 输入 token 数 |
| `output_tokens` | Int | 输出 token 数 |
| `total_tokens` | Int | 总 token 数 |
| `cached_tokens` | Int \| null | 缓存命中 token（可选） |
| `reasoning_tokens` | Int \| null | 思考 token（可选） |
| `round_num` | Int | 轮次编号 |
| `timestamp` | String | ISO 8601（IsoTimestamp；读取时自动兼容旧 float Unix 输入） |

**索引**:

| 索引 | 方向 | 说明 |
|------|------|------|
| `session_id` | ASC | 按会话查询 / 聚合统计（`TokenRepository.connect()` 建，`core/storage/token_repo.py:53`） |

**操作**: `insert_one`（每次 LLM 调用后），`find`（按 session 聚合统计）。无 TTL，无上限。

### 集合: `generations`（数据库: `default`，BE-024）

每次 LLM 回合（generation）的生命周期状态，由 `GenerationRegistry` 写入（`register` / `mark` 终态 + stale 清理）。

| 字段 | 类型 | 说明 |
|------|------|------|
| `generation_id` | String | generation 唯一标识（NewType） |
| `session_id` | String | 所属会话 |
| `message_id` | String | 关联消息 ID |
| `status` | String | `started` / `segment_started` / `segment_completed` / `completed` / `failed` |
| `updated_at` | String（ISO） | 最后更新时间 |
| `ttl_expires_at` | BSON Date | 30 天 TTL 基准（B6/P2-01；启动对缺字段旧记录幂等回填） |

**索引**: `generation_id` unique + `(session_id, updated_at)` + `message_id` + `ttl_expires_at` TTL。
**TTL**: 30 天（`expireAfterSeconds` 按真实 BSON Date `ttl_expires_at` 过期；旧 ISO `updated_at` 不会触发，`GenerationRepository.connect()` 幂等回填）。Mongo 离线时 `GenerationRegistry` no-op。

### 集合: `attachments`（数据库: `default`，BE-070）

附件元数据（fail-closed：Mongo 离线 `StorageUnavailableError` → 503）。文件本体存 `data/uploads/`，本集合管元数据 + 引用计数。

| 字段 | 类型 | 说明 |
|------|------|------|
| `attachment_id` | String | 附件唯一标识 |
| `session_id` | String | 所属会话 |
| `owner_connection` | String? | 上传者 WS 连接（持有者校验闭环） |
| `filename` / `filetype` / `size` | — | 文件元信息（filetype 魔数校验） |
| `status` | String | `active` / `deleted`（软删 `deleted_at`） |
| `ref_count` | Int | 被消息引用计数；`refs` 数组记录引用消息 |
| `created_at` | String（ISO） | 创建时间 |

**索引**: `attachment_id` unique + `status` + `ref_count` + `created_at`。
**TTL**: 不设——附件生命周期由 `AttachmentGC` 兜底扫描管（软删超 24h 隔离+删记录 / active 文件缺失软删 / 未关联超 7 天软删 / 磁盘孤儿复核后隔离 / quarantine 超 7 天物理删，限四 kind 目录）。会话/消息删除时经 `AttachmentRelease` 释放引用。

> **P0-01 加固（2026-08-10）**：
> - `find_by_status` / `find_unreferenced_older_than` 为完整 keyset 分页，不再依赖 Mongo 默认 `limit=100`——第 101+ 条仍有效、仍有引用的附件不会被误判为磁盘孤儿。
> - 磁盘孤儿候选隔离前逐文件复核数据库（`find_by_stored_name`），闭合扫描期间新建引用/记录窗口；任一页查询失败或复核时离线 → GC `skipped=True` 返回，**零删除**（fail-closed）。
> - 物理删除改两阶段：先 `os.replace` 移入 `data/uploads/.quarantine/{kind}/`（同文件系统、可恢复），超 `quarantine_retention_hours`（默认 7 天）后才真正 `unlink`。
> - `run_attachment_gc(..., dry_run=True)` 只计数候选不落盘不改库，供运维预演。
> - 数据恢复：误隔离文件可从 `.quarantine/{kind}/` 按原文件名还原回 `data/uploads/{kind}/` 并重建记录。

### 集合: `desktop_calls`（数据库: `default`，批次 4）

桌面工具调用状态，`DesktopCallRepository`（Mongo + 进程内缓存 + 迁移守卫 + 离线降级）。

| 字段 | 类型 | 说明 |
|------|------|------|
| `call_id` | String | 调用唯一标识 |
| `status` | String | `pending` / `accepted` / `running` / `succeeded` / `failed` / `rejected` / `cancelled` / `expired` / `offline` |
| `params` / `result` / `error` | — | 调用参数 / 结果 / 错误 |
| `expires_at` | String（ISO） | 超时截止 |
| `updated_at` | String（ISO） | 最后更新 |
| `ttl_expires_at` | BSON Date | 30 天 TTL 基准（B6/P2-01；业务 `expires_at` 是调用超时，非回收 TTL） |

**索引**: `call_id` unique + `(status, updated_at)` + `ttl_expires_at` TTL。
**TTL**: 30 天（`expireAfterSeconds` 按真实 BSON Date `ttl_expires_at` 过期，与 generations 对齐）。

### 集合: `user_identity_profiles`（数据库: `default`，BE-STAGEH-02）

用户最小身份（`UserIdentity`），`UserIdentityRepository`。**Mongo 是权威事实源**——修复前只有进程内缓存，重启后端后 `/api/me` 回到 bootstrap。

| 字段 | 类型 | 说明 |
|------|------|------|
| `user_id` | String | 本地用户稳定 ID（首版 `usr_local_default`） |
| `display_name` / `preferred_address` | String / String? | 显示名 / 希望被如何称呼 |
| `pronouns` / `locale` / `timezone` | String | 代词（全局唯一）/ 语言 / 时区 |
| `initialized` | Bool | 仅 UX 标记（是否已确认），只可由 false → true |
| `default_character_id` | String? | 新会话默认角色 |
| `revision` | Int | 乐观并发版本，从 1 起 |
| `created_at` / `updated_at` | String（ISO） | 创建 / 最后更新 |

**索引**: `user_id` unique。
**写入语义**: 首次空库 `$setOnInsert` 原子 bootstrap（并发启动只有一个 winner）；更新走 `revision` CAS，`$set` 同一原子操作内递增 revision + 刷新 updated_at。repo 未连接时写路径 fail-closed（503 `STORAGE_UNAVAILABLE`）。

---

## 三、内存/文件存储（非数据库表）

### SessionMemory（内存，`core/memory/session.py`）

| 字段 | 类型 | 说明 |
|------|------|------|
| `_messages` | deque[Message] | 当前会话消息缓冲 (maxlen config.yaml 可配, 默认 20). 存 Pydantic Message 对象 |
| `character_state` | CharacterState | 性格+情绪+reflector buffer 等状态（Pydantic 模型，替代原 _params） |
| `service_state` | ServiceState | 服务运行时状态（当前模型 + STT/TTS 启用模式，enabled 三态 `bool \| None`） |
| `_session_id` | str | 会话 ID |

运行时状态经专用类型字段持久化：`save()` 写 `character_state` + `service_state`（reflector 计数器经 `set_memory_reflector()` 并入，无独立 `_reflector_state` 属性），`load()` / `load_from_jsonl()` 恢复。多会话由 `SessionManager`（`core/memory/session_manager.py`）缓存池管理。

### Secret Store（文件存储，`core/storage/secret_store.py`，批次 5）

自定义 provider 的 API 密钥存储，位置 `data/secrets/profiles.json`（`ProjectPath.DATA_SECRETS`）。

- 格式：按 provider service 分组的密钥 Profile（`{"<service>": {"<name>": "<secret-value>", ...}}`，LLM/STT/TTS 共用，value 为 api_key 等敏感值）
- 安全：文件 `0600` 权限 + 原子写（临时文件 + `os.replace`）+ 损坏容错（解析失败不崩溃）+ 并发锁（threading.Lock 防丢更新）；`api_key` 存读写都过 Secret Store，**永不读回**（列表/详情响应剔除密钥）
- 关联：`custom_providers` 解析统一 secret store 优先；switch 路径守卫 + SSRF 校验（批次 5/8a）
- 与 `sessions.service_state` 协同：运行中的 provider 配置经 service_state 跨重启存活

### MemoryEntry（内存 dataclass，`core/memory/long_term.py`）

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | MemoryId | `mem_` 前缀 + 26 位十六进制（`new_memory_id()` 生成；Milvus 旧 `msg_` 数据读回兼容） |
| `text` | str | 记忆文本 |
| `embedding` | list[float] | 向量 |
| `memory_type` | `MemoryType` | `Fact` / `Preference` / `Event` / `Plan` / `Note`。构造 `__post_init__` 经 `MemoryType.coerce` 归一（未知 → `Note`），`MemoryEntry` 字段类型即 `MemoryType` StrEnum |
| `poignancy` | int | 重要性 1-10 |
| `timestamp` | float | Unix 时间 |
| `metadata` | dict | 扩展元数据 |
| `_milvus_pk` | int \| None | Milvus 主键（insert 时记录，用于 delete） |

### Task 检查点（`data/scheduler_checkpoint.json`，文件存储）

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | str | 任务 ID |
| `type` | TaskType | `TOOL_CALL` / `BACKGROUND` |
| `status` | TaskStatus | `PENDING` / `DISPATCHED` / `COMPLETED` / `FAILED` / `CANCELLED` |
| `priority` | int | 0=最高，5=默认，9=后台 |
| `parent_id` | str \| None | 父任务 ID |
| `depends_on` | list[str] | 依赖任务列表 |
| `children_ids` | list[str] | 子任务列表 |
| `payload` | dict | 任务负载 |
| `result` | Any | 执行结果 |
| `error` | str \| None | 错误信息 |
| `retry_count` | int | 已重试次数 |
| `max_retries` | int | 最大重试次数（默认 2） |
| `progress` | float | 进度 0.0-1.0 |
| `created_at` | float | 创建时间（Unix） |
| `updated_at` | float | 更新时间（Unix） |

---

## 四、未实现的预留接口

| 数据库 | 模块路径 | 状态 |
|--------|----------|------|
| Pinecone | `Module/database/vectordb/` | 已注册工厂，抛 `NotImplementedError` |
| Qdrant | `Module/database/vectordb/` | 已注册工厂，抛 `NotImplementedError` |
| Neo4j（GraphDB） | `Module/database/graphdb/` | 仅有 ABC 协议，无注册器、无配置、无实现 |

---

## 五、数据流

```
用户消息 → SessionMemory（MongoDB sessions）←→ 会话持久化
                ↓
         记忆提取（MemoryConsolidator，LLM 驱动，6 条消息触发）
                ↓
         LongTermMemory / ScopedLongTermMemory（Milvus long_term_memory_v2，scope 隔离）←→ 向量检索
                ↓
         工具调用时可查 MemorySearch / MemorySave
```

## 六、数据库调试接口

开发阶段可通过 Web UI 直接操作数据库。访问 `http://localhost:8765/debug` → 「数据库」Tab。

### REST API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/debug/api/db-status` | 数据库状态汇总 |
| GET | `/debug/api/ltm?search=&type=&limit=&offset=` | 查询 LTM 条目。`type` 过滤大小写不敏感（`try_coerce` 归一），无效值返回空 |
| POST | `/debug/api/ltm` | 新增条目 `{text, memory_type, poignancy}` |
| DELETE | `/debug/api/ltm/{entry_id}` | 删除单条 |
| DELETE | `/debug/api/ltm` | 清空全部（drop + recreate collection） |
| GET | `/debug/api/sessions` | 列出 MongoDB 所有会话 |
| GET | `/debug/api/sessions/{session_id}` | 查看会话消息详情 |
| DELETE | `/debug/api/sessions/{session_id}` | 删除会话 |

### LongTermMemory 新增方法

| 方法 | 说明 |
|------|------|
| `delete(entry_id) -> bool` | 删除单条记忆（内存 + Milvus） |
| `clear_all() -> int` | 清空所有记忆（drop + recreate collection），返回删除数 |

### VectorDBBackend 新增方法

| 方法 | 说明 |
|------|------|
| `delete(collection, ids) -> int` | 按主键列表删除向量，返回删除数量 |

---

## 七、配置方式

数据库连接可通过 `config.yaml` 或环境变量配置。优先级：环境变量 > config.yaml > 默认值。

```yaml
# config.yaml
database:
  mongodb_uri: mongodb://localhost:27017  # MongoDB 连接串（env: MONGODB_URI）
  milvus_uri: http://localhost:19530     # Milvus 服务端点（env: MILVUS_URI）
  milvus_token: ""                        # Milvus 认证令牌（env: MILVUS_TOKEN）
  db_name: default                       # MongoDB 数据库名
```

> **认证（P1-19）**：docker-compose 的 Mongo/Milvus 默认启用认证，`MONGODB_URI` 需带用户名密码（如 `mongodb://elysia:<密码>@127.0.0.1:27017/default?authSource=admin`）、`MILVUS_TOKEN` 需为 `root:<密码>`，否则连接被拒。`setup.sh` 首次安装会自动生成密码并写入 `.env`。

| 变量 | YAML 路径 | 用途 | 默认值 |
|------|-----------|------|--------|
| `MILVUS_URI` | `database.milvus_uri` | Milvus 连接地址 | `http://localhost:19530` |
| `MILVUS_TOKEN` | `database.milvus_token` | Milvus 认证令牌（`root:<密码>`） | 空 |
| `MONGODB_URI` | `database.mongodb_uri` | MongoDB 连接地址（认证后须带用户名密码） | `mongodb://localhost:27017` |
| `OPENAI_API_KEY` | — | 嵌入模型 API Key（可选） | 空 |
