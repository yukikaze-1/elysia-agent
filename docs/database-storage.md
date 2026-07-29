# 数据库存储表格

项目共有 **2 个持久化数据库**（Milvus 向量库 + MongoDB 文档库），4 个集合，外加若干内存/文件存储结构。没有使用 SQL 数据库。

| 集合 | 位置 | 用途 | 保留策略 |
|------|------|------|----------|
| `long_term_memory` | Milvus | 记忆摘要（向量搜索）| 90天/1万条(内存)，Milvus 不自动删除 |
| `sessions` | MongoDB | LLM 上下文窗口 | max_messages 截断, 30天 TTL |
| `message_archive` | MongoDB | 对话消息永久归档 | 永久, 只增不删 |
| `token_usage` | MongoDB | LLM token 统计 | 永久, 只增不删 |

---

## 一、Milvus 向量数据库

**连接**: 优先级 `config.yaml` → 环境变量 → 默认值。YAML 路径 `database.milvus_uri` / `database.milvus_token`，环境变量 `MILVUS_URI`（默认 `http://localhost:19530`）/ `MILVUS_TOKEN`

### 集合: `long_term_memory`

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INT64（主键，自增） | 自动生成 |
| `embedding` | FLOAT_VECTOR[1024] | 文本嵌入向量（BGE-large-zh-v1.5） |
| `text` | VARCHAR（动态字段） | 记忆文本内容 |
| `memory_type` | VARCHAR（动态字段） | 类型：`fact` / `plan` / `preference` / `note` |
| `poignancy` | INT（动态字段） | 重要性 1-10 |
| `timestamp` | FLOAT（动态字段） | Unix 时间戳 |

- **索引**: IVF_FLAT，metric_type=L2，nlist=1024
- **检索方式**: ANN 向量搜索 + 重排序（0.5 × 相似度 + 0.3 × 重要性 + 0.2 × 时效性）
- **淘汰策略**: max_age_days=90，max_entries=10000
- **降级**: Milvus 不可用时回退到内存列表 + 文本重叠搜索

---

## 二、MongoDB 文档数据库

**连接**: 优先级 `config.yaml` → 环境变量 → 默认值。YAML 路径 `database.mongodb_uri`，环境变量 `MONGODB_URI`（默认 `mongodb://localhost:27017`）

### 集合: `sessions`（数据库: `default`，可配置）

| 字段 | 类型 | 说明 |
|------|------|------|
| `_id` | ObjectId | MongoDB 自动生成 |
| `session_id` | String | 会话唯一标识 |
| `messages` | Array\<Object\> | 消息数组 |
| `character_state` | Object | CharacterState 序列化（mood + personality L2/L3 + reflector buffer/context + awareness 冷却 + state_history） |
| `reflector_state` | Object | MemoryReflectorState 序列化（round_count / new_memory_count） |
| `params` | Object | 运行时参数（legacy） |
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
| `updated_at`（TTL） | — | 30 天过期（`expireAfterSeconds=2592000`） |

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
| `token_usage` | TokenUsageSummary? | assistant 的 token 统计（rounds: TokenUsageRecord[], turn_total: int） |
| `tool_call_id` / `name` / `success` / `error` | — | tool 角色专用字段 |

**索引**: `_id`（MongoDB 内置默认降序扫描），无 TTL，不截断。

**操作**: `insert_one`（fire-and-forget），`find`（分页游标），`delete_many({})`（debug 清空）。

### 集合: `token_usage`（数据库: `default`）

每次 LLM 调用的 token 统计日志，只增不删。

| 字段 | 类型 | 说明 |
|------|------|------|
| `session_id` | String | 会话标识 |
| `model` | String | LLM 模型名 |
| `prompt_tokens` | Int | 输入 token 数 |
| `completion_tokens` | Int | 输出 token 数 |
| `total_tokens` | Int | 总 token 数 |
| `round_num` | Int | 轮次编号 |
| `timestamp` | Float | Unix 时间戳 |

**操作**: `insert_one`（每次 LLM 调用后），`find`（按 session 聚合统计）。无 TTL，无上限。

---

## 三、内存/文件存储（非数据库表）

### SessionMemory（内存，`core/memory/session.py`）

| 字段 | 类型 | 说明 |
|------|------|------|
| `_messages` | deque[Message] | 当前会话消息缓冲 (maxlen config.yaml 可配, 默认 20). 存 Pydantic Message 对象 |
| `_character_state` | CharacterState \| None | 性格+情绪+reflector 状态（Pydantic 模型，替代原 _params） |
| `_reflector_state` | MemoryReflectorState \| None | MR 计数器（Pydantic 模型） |
| `_session_id` | str | 会话 ID |

### MemoryEntry（内存 dataclass，`core/memory/long_term.py`）

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | str（12 位 hex） | UUID4 前 12 位 |
| `text` | str | 记忆文本 |
| `embedding` | list[float] | 向量 |
| `memory_type` | str | `fact` / `plan` / `preference` / `note` |
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
         LongTermMemory（Milvus long_term_memory）←→ 向量检索
                ↓
         工具调用时可查 MemorySearch / MemorySave
```

## 六、数据库调试接口

开发阶段可通过 Web UI 直接操作数据库。访问 `http://localhost:8765/debug` → 「数据库」Tab。

### REST API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/debug/api/db-status` | 数据库状态汇总 |
| GET | `/debug/api/ltm?search=&type=&limit=&offset=` | 查询 LTM 条目 |
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

| 变量 | YAML 路径 | 用途 | 默认值 |
|------|-----------|------|--------|
| `MILVUS_URI` | `database.milvus_uri` | Milvus 连接地址 | `http://localhost:19530` |
| `MILVUS_TOKEN` | `database.milvus_token` | Milvus 认证令牌 | 空 |
| `MONGODB_URI` | `database.mongodb_uri` | MongoDB 连接地址 | `mongodb://localhost:27017` |
| `OPENAI_API_KEY` | — | 嵌入模型 API Key（可选） | 空 |
