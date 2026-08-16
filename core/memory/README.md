# Memory — 记忆子系统

> 文档导航：[项目文档](../../README.md)

三层记忆管线：对话窗口（SessionMemory）→ 向量检索（LongTermMemory）→ 沉淀+去重（MemoryConsolidator + MemoryReflector）。

## 架构

```
┌─────────────────────────────────────────────────────────┐
│                   记忆管线                                │
│                                                         │
│  对话轮次 → SessionMemory (deque, maxlen, MongoDB 持久化，无自动 TTL)│
│     ↓                                                   │
│  MemoryConsolidator (缓冲 6 条 → LLM 提取 → 比对 → 写入)  │
│     ↓                                                   │
│  LongTermMemory (Milvus 向量搜索 + rerank)              │
│     ↓                                                   │
│  MemoryReflector (定期 KMeans 聚类去重)                  │
└─────────────────────────────────────────────────────────┘
```

## 数据流

```
每轮对话后:
  _after_response()
    → MemoryConsolidator.consolidate(user_msg, assistant_msg)
      → buffer 6 条消息对
      → LLM 提取: Fact / Preference / Event / Plan / Note
      → 向量搜索现有记忆
      → LLM 比对: insert / update / merge / skip
      → LongTermMemory 写入

定期（round/new-memory 触发）:
  MemoryReflector._maybe_reflect()
    → KMeans 聚类（按 content）
    → LLM 审查同簇记忆：合并 / 去噪 / 删除
    → _absorbed_ids 追踪合并记录
```

### 双重存储与同步

消息同时存在于两处：

| 存储 | 集合 | 用途 |
|------|------|------|
| SessionMemory | MongoDB `sessions` | LLM prompt 上下文窗口（权威消息源） |
| MessageRepository | MongoDB `message_archive` | 永久归档（fire-and-forget） |

`add_message()` 同时写入两处。debug 页面删除 Archive 消息时调用 `Agent.purge_message_from_session()` 同步清理 session，确保 LLM prompt 不再包含已删消息。

## 关键机制

### SessionMemory

- 后端：`deque`（maxlen 可配，默认 20）+ MongoDB 持久化（无 Mongo TTL；删除走 REST/debug 显式级联删除）
- `recent_messages()` 取最近 N 条给 CognitiveLoop 拼接上下文
- `append()` 追加消息时 fire-and-forget 写入 MongoDB archive
- `save()` / `restore()` 服务端会话恢复

### SessionManager（批次 3 多会话）

- 会话缓存池 + 活动指针（`active_id` / `active`）；懒加载（`ensure_loaded`，Mongo → JSONL fallback）
- `resolve(conversation_id, protocol_version)`：v1 恒 `"default"`、v2 显式 conversationId → 该会话、v2 缺省 → 活动会话
- `switch(sid)` 切活动（同步 reflector buffer + 换绑 CharacterEngine）；`create`/`rename`/`delete`（联动归档）
- 启动 `load_active()` 恢复 default；`save_all()` 关机持久化所有已加载会话

### LongTermMemory

- 后端：Milvus 向量搜索 + rerank（时效衰减 + poignancy 加权）
- `search(query_text="", query_embedding=None, top_k=5, memory_type=None)` 返回最相关记忆
- **memory_type 归一**：`core/schema/memory.py` 的 `MemoryType` StrEnum（Fact/Preference/Event/Plan/Note）是 LTM 记忆类型权威来源；构造/update/search 边界经 `try_coerce`/`coerce` 归一——过滤大小写不敏感，未知值 → `None`（过滤不命中）或 `Note`（存储兜底，不崩）
- `_absorbed_ids` 追踪合并记录，避免重复返回
- 无 Milvus 时降级为纯本地匹配（不 crash）

### MemoryConsolidator

- 缓冲阈值：`BUFFER_THRESHOLD = 6` 条消息对
- 提取 prompt：`core/prompts/ExtractMemory.j2`（`Path(__file__)` 相对路径）
- 比对 prompt：`core/prompts/ConsolidationCompare.j2`
- 提取类型为 `MemoryType` StrEnum（Fact/Preference/Event/Plan/Note，`core/schema/memory.py`），与 ExtractMemory.j2 / ConsolidationCompare.j2 的大写词表一致
- `flushing` 标志防止并发 flush

### MemoryReflector

- 触发条件：距上次 round 数 >= 50 **或** 新增记忆 >= 30 条（OR 逻辑）
- 聚类算法：KMeans（n_clusters = ceil(记忆数 / CLUSTER_SIZE)，CLUSTER_SIZE = 20，单组最多约 20 条）
- LLM 审查后决定合并 / 去噪 / 删除
- `_reflect_lock` 防并发

## 文件布局

```
core/memory/
├── __init__.py            — re-export SessionMemory / LongTermMemory / SessionManager
├── session_manager.py     — SessionManager: 会话缓存池 + 活动指针 + resolve/switch/CRUD（批次 3 多会话）
├── session.py             — SessionMemory: deque + MongoDB 持久化
├── long_term.py           — LongTermMemory: Milvus 向量搜索 + rerank
├── embedding_provider.py  — EmbeddingProvider: 嵌入后端懒加载 + 超时 + OpenAI/HuggingFace fallback
├── consolidator.py        — MemoryConsolidator: LLM 提取 + 比对 + 写入
├── memory_reflector.py    — MemoryReflector: KMeans 聚类定期去重
└── tests/
    ├── test_session.py
    ├── test_session_manager.py
    ├── test_long_term.py
    ├── test_consolidator.py
    └── test_memory_reflector.py

关联文件:
core/
├── adapter/llm_adapter.py  — LLM 适配器（consolidator + reflector 共用）
└── schema/memory.py   — MemoryType StrEnum（try_coerce/coerce 归一，未知→None/NOTE）+ ExtractedMemory / CompareAction 判别联合

core/prompts/
├── ExtractMemory.j2         — 记忆提取模板
├── ConsolidationCompare.j2  — 记忆比对模板
└── MemoryReflector.j2       — 记忆去重反思模板

Module/database/
├── vectordb/   — Milvus (ANN 搜索)
└── documentdb/ — MongoDB (session 持久化 + 消息归档)
```

## 调试

访问 `http://localhost:8765/debug` → **Memory** 卡片，可查看：
- Session 消息数
- LTM 条目数 + DB 连接状态
- Consolidator 缓冲状态（buffer_size / threshold）

**Database** 标签页提供 LTM CRUD + Session 管理 + 消息归档查询。

后端 API: `GET /debug/api` → `info.session_message_count` / `info.ltm_entry_count` / `info.consolidator_buffer_size`

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../README.md) | 快速开始 + 完整文档导航 |
| [core/README.md](../README.md) | Core 层总览 |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 |
| [core/storage/README.md](../storage/README.md) | 统一存储层（MongoDB + Milvus 的 Repository） |
| [core/cognitive/README.md](../cognitive/README.md) | 认知循环 — 记忆沉淀的触发者 |
