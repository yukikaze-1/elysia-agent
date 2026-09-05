# Memory — 记忆子系统

> 文档导航：[项目文档](../../README.md)

三层记忆管线：对话窗口（SessionMemory）→ 向量检索（LongTermMemory）→ 沉淀+去重（MemoryConsolidator + MemoryReflector）。

**阶段 F（记忆 v2 隔离）起**：产品记忆全部按 `MemoryScope`（用户 × 角色实例）隔离，读写一律经 `ScopedLongTermMemory` 绑定视图 + 底层 `long_term_memory_v2` 存储（`MemoryRepositoryV2`）；consolidator/reflector 改为 per-runtime 构造（各持 scoped view），不再有全局单例。

## 架构

```
┌─────────────────────────────────────────────────────────┐
│                   记忆管线（阶段 F scoped）                 │
│                                                         │
│  对话轮次 → SessionMemory (deque, maxlen, MongoDB 持久化，无自动 TTL)│
│     ↓                                                   │
│  MemoryConsolidator (per-runtime，缓冲 6 条 → LLM 提取 → 比对 → 写入)│
│     ↓                                                   │
│  ScopedLongTermMemory (绑定 MemoryScope 的视图)          │
│     └── LongTermMemory (Milvus long_term_memory_v2 向量搜索 + rerank)│
│     ↓                                                   │
│  MemoryReflector (per-runtime，定期 KMeans 聚类去重)      │
└─────────────────────────────────────────────────────────┘
```

> 阶段 F 前为单例管线；阶段 F 后 consolidator/reflector、记忆写入/召回全部 scope 化（`MemoryScope` = `user_id` + `character_instance_id` + `visibility`）。任何记忆写/读都经过 `ScopedLongTermMemory` 视图，调用方无法在调用点绕过 scope。

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

### MemoryScope + ScopedLongTermMemory（阶段 F，架构稿 §8.1/§8.2）

- **MemoryScope**（[scope.py](scope.py)）：`@dataclass(frozen=True)`，字段 `user_id` / `character_instance_id` / `visibility`（`MemoryVisibility` StrEnum：`character_private`｜`user_shared`）。产品记忆一律 `character_private`；`user_shared` 只预留枚举，不提供自动升级路径。
- **ScopedLongTermMemory**（[scoped_long_term.py](scoped_long_term.py)）：绑定 `MemoryScope` 的只读/读接口视图。`insert/search/update_text/delete/get_all` 全部作用域过滤；底层与 `LongTermMemory` 共享同一 `_entries/_repo`，但 scope 在视图创建时固定，调用点无法在方法调用时绕过。
- **`update_text` 签名与 `LongTermMemory` 严格对齐**（含 `poignancy`）——consolidator/reflector 调用须传 `poignancy=...`，否则丢 poignancy。`mark_absorbed` 必须暴露（reflector merge 分支调用）。
- **`LongTermMemory.for_scope(scope)`**：返回绑定该 scope 的视图。底层 `LongTermMemory` 的 `search/insert/update_text/delete` 均收 `scope` 参数——scope 非空时先按 scope 过滤（`_in_scope`：`character_instance_id` + `user_id` 匹配，ANN/内存 fallback 双路径一致）；`scope=None` 走宽容（仅 admin 用途）。

### 存储层（阶段 F，架构稿 §8.3）

- **`MemoryRepositoryV2`**（[core/storage/memory_repo_v2.py](../storage/memory_repo_v2.py)）：`MemoryRepository` 的 v2 变体，`COLLECTION = "long_term_memory_v2"`——产品记忆全部写 v2（scope 字段 `user_id`/`character_instance_id`/`visibility`）。复用父类 Milvus 操作，仅 collection 不同。

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

- 后端：Milvus 向量搜索 + rerank（时效衰减 + poignancy 加权），底层指向 **`long_term_memory_v2`**（`MemoryRepositoryV2`，阶段 F）
- `search(query_text="", query_embedding=None, top_k=5, memory_type=None, scope=None)` 返回最相关记忆；scope 非空时先按 scope 过滤（跨角色/跨用户记忆不参与召回）
- **`for_scope(scope)`**：返回绑定该 scope 的 `ScopedLongTermMemory` 视图（产品路径唯一入口）
- **memory_type 归一**：`core/schema/memory.py` 的 `MemoryType` StrEnum（Fact/Preference/Event/Plan/Note）是 LTM 记忆类型权威来源；构造/update/search 边界经 `try_coerce`/`coerce` 归一——过滤大小写不敏感，未知值 → `None`（过滤不命中）或 `Note`（存储兜底，不崩）
- `MemoryEntry`（阶段 F）scope 字段：`user_id` / `character_instance_id` / `visibility`（顶部字段，供 `_in_scope`/`_search_fallback` 过滤）
- `_absorbed_ids` 追踪合并记录，避免重复返回
- 无 Milvus 时降级为纯本地匹配（不 crash）

### MemoryConsolidator（阶段 F per-runtime）

- **阶段 F：per-runtime 构造**——agent 在 `_make_runtime` 时构造（engine 同模式），buffer 属 instance；`memory` 参数为 `ScopedLongTermMemory | LongTermMemory`（duck-typed），ExtractMemory 输入 scope、compare 只查同 scope。**无全局单例**。
- 缓冲阈值：`BUFFER_THRESHOLD = 6` 条消息对
- 提取 prompt：`core/prompts/ExtractMemory.j2`（`Path(__file__)` 相对路径）
- 比对 prompt：`core/prompts/ConsolidationCompare.j2`
- 提取类型为 `MemoryType` StrEnum（Fact/Preference/Event/Plan/Note，`core/schema/memory.py`），与 ExtractMemory.j2 / ConsolidationCompare.j2 的大写词表一致
- `flushing` 标志防止并发 flush
- 构造参数（阶段 B-2 新增）：`MemoryConsolidator(llm, memory, character_name, user_name, buffer_threshold=None)`

### MemoryReflector（阶段 F per-runtime）

- **阶段 F：per-runtime 构造**——每 instance 独立计数；`memory` 参数为 `ScopedLongTermMemory | LongTermMemory`（duck-typed）；`get_all()` 只返回当前 scope；merge 验证 scope。**无全局单例**。
- 触发条件：距上次 round 数 >= 50 **或** 新增记忆 >= 30 条（OR 逻辑）
- 聚类算法：KMeans（n_clusters = ceil(记忆数 / CLUSTER_SIZE)，CLUSTER_SIZE = 20，单组最多约 20 条）
- LLM 审查后决定合并 / 去噪 / 删除
- `_reflect_lock` 防并发
- **构造参数**（阶段 B-2/Task 7 调整）：`MemoryReflector(llm, memory, session=None, character_name, user_name)`——`session` 仅存不用（per-runtime 后 counter 持久化改经 `runtime.state`，架构稿 §8.5）；`character_name`/`user_name` 替换 prompt 中的 `{character_name}`/`{user_name}` 占位符（去 Elysia 化；agent.py 构造时传 `self._character.name`/`self._user_name`）。

## 文件布局

```
core/memory/
├── __init__.py            — re-export SessionMemory / LongTermMemory / SessionManager
├── session_manager.py     — SessionManager: 会话缓存池 + 活动指针 + resolve/switch/CRUD（批次 3 多会话）
├── session.py             — SessionMemory: deque + MongoDB 持久化
├── scope.py               — MemoryScope + MemoryVisibility（阶段 F，架构稿 §8.1）
├── long_term.py           — LongTermMemory: Milvus 向量搜索 + rerank + for_scope()/scope 参数（阶段 F）
├── scoped_long_term.py    — ScopedLongTermMemory: 绑定 MemoryScope 的视图（阶段 F，架构稿 §8.2）
├── embedding_provider.py  — EmbeddingProvider: 嵌入后端懒加载 + 超时 + OpenAI/HuggingFace fallback
├── consolidator.py        — MemoryConsolidator: LLM 提取 + 比对 + 写入（per-runtime，阶段 F）
├── memory_reflector.py    — MemoryReflector: KMeans 聚类定期去重（per-runtime，阶段 F）
└── tests/
    ├── test_session.py
    ├── test_session_manager.py
    ├── test_long_term.py
    ├── test_consolidator.py
    ├── test_memory_reflector.py
    ├── test_scope.py
    └── test_scoped_long_term.py

关联文件:
core/
├── adapter/llm_adapter.py  — LLM 适配器（consolidator + reflector 共用）
└── schema/memory.py   — MemoryType StrEnum（try_coerce/coerce 归一，未知→None/NOTE）+ ExtractedMemory / CompareAction 判别联合

core/storage/
└── memory_repo_v2.py — MemoryRepositoryV2: 指向 long_term_memory_v2 + scope 字段（阶段 F，架构稿 §8.3）

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
| [阶段 F Spec](../../docs/superpowers/specs/2026-09-05-character-stage-f-memory-v2-isolation-design.md) | 记忆 v2 隔离设计（MemoryScope + ScopedLongTermMemory + per-runtime） |
| [阶段 F Plan](../../docs/superpowers/plans/2026-09-05-character-stage-f-memory-v2-isolation-plan.md) | 阶段 F 实施计划 |
