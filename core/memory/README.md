# Memory — 记忆子系统

> 文档导航：[项目文档](../../README.md)

三层记忆管线：对话窗口（SessionMemory）→ 向量检索（LongTermMemory）→ 沉淀+去重（MemoryConsolidator + MemoryReflector）。

## 架构

```
┌─────────────────────────────────────────────────────────┐
│                   记忆管线                                │
│                                                         │
│  对话轮次 → SessionMemory (deque, maxlen, MongoDB 30d TTL)│
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

## 关键机制

### SessionMemory

- 后端：`deque`（maxlen 可配，默认 200）+ MongoDB 持久化（30 天 TTL）
- `recent_messages()` 取最近 N 条给 CognitiveLoop 拼接上下文
- `append()` 追加消息时 fire-and-forget 写入 MongoDB archive
- `save()` / `restore()` 服务端会话恢复

### LongTermMemory

- 后端：Milvus 向量搜索 + rerank（时效衰减 + poignancy 加权）
- `search(query, limit=20)` 返回最相关记忆
- `_absorbed_ids` 追踪合并记录，避免重复返回
- 无 Milvus 时降级为纯本地匹配（不 crash）

### MemoryConsolidator

- 缓冲阈值：`BUFFER_THRESHOLD = 6` 条消息对
- 提取 prompt：`core/prompts/ExtractMemory.j2`（`Path(__file__)` 相对路径）
- 比对 prompt：`core/prompts/ConsolidationCompare.j2`
- `flushing` 标志防止并发 flush

### MemoryReflector

- 触发条件：距上次 >= 1h **且** 新增记忆 >= 10 条
- 聚类算法：KMeans（k=min(5, total_memories/2)）
- LLM 审查后决定合并 / 去噪 / 删除
- `_reflect_lock` 防并发

## 文件布局

```
core/memory/
├── __init__.py         — 空（各模块独立导入）
├── session.py          — SessionMemory: deque + MongoDB 持久化
├── long_term.py        — LongTermMemory: Milvus 向量搜索 + rerank
├── consolidator.py     — MemoryConsolidator: LLM 提取 + 比对 + 写入
├── memory_reflector.py — MemoryReflector: KMeans 聚类定期去重
└── tests/
    ├── test_session.py
    ├── test_long_term.py
    ├── test_consolidator.py
    └── test_memory_reflector.py

关联文件:
core/
├── llm_adapter.py     — LLM 适配器（consolidator + reflector 共用）
└── schema/memory.py   — ExtractedMemory / CompareAction 判别联合

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
