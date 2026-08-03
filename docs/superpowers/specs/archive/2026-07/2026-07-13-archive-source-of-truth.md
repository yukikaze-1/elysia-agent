# Archive as Source of Truth — Session 视图数据一致性

**日期:** 2026-07-13
**状态:** 待实现

## 背景

当前存在两个独立的消息存储：

| 存储 | 集合 | 写入方式 | 用途 |
|------|------|----------|------|
| Session 快照 | `sessions` | `save()` 整体覆盖 | LLM 上下文恢复 |
| Message Archive | `message_archive` | `append()` 逐条追加 | 永久归档 |

问题：Debug 页面 Session 视图读的是 `sessions` 快照，Archive 视图读的是 `message_archive`。删除 Archive 中的消息不会影响 Session 快照，导致两边数据不一致。

## 目标

**Archive (`message_archive`) 是权威数据源。** Debug 页面的 Session 视图从 Archive 读取数据，不再依赖 `sessions` 快照的消息列表。

Session 快照保留其核心职责：LLM 上下文窗口的快速恢复（消息 + params）。

## 设计

### 1. Archive 消息加 `session_id`

`SessionMemory._archive_append()` 在 entry 中写入 `session_id` 字段后调用 `archive.append()`。

```python
# session.py — _archive_append()
entry["session_id"] = self._session_id
await self._archive.append(entry)
```

### 2. MessageArchive 新增查询方法

```python
# message_archive.py

async def query_by_session(
    self, session_id: str, *, skip: int = 0, limit: int = 200
) -> tuple[list[dict], int]:
    """按 session_id 查询消息，_id 降序（最新在前）。
    返回 (messages, total)。"""

async def count_by_session(self, session_id: str) -> int:
    """按 session_id 统计消息数。"""
```

### 3. Session 列表 API 改读 Archive

`GET /debug/api/sessions` — `message_count` 改为从 `archive.count_by_session(sid)` 获取。

N+1 查询注意：当前 sessions 数量极少（<=10），独立 count 查询可接受。若未来 session 数量增长，改用 MongoDB aggregation `$group` by session_id 一次查出。

回退：MongoDB/archive 不可用时，从内存 `len(session._messages)` 获取。

### 4. Session 详情 API 改读 Archive

`GET /debug/api/sessions/{id}` — 消息列表从 `archive.query_by_session(sid)` 获取，不再读 `sessions` 快照。

消息格式保持独立的格式化逻辑（与 `_archive_to_client()` 分开），确保前端 Session 详情面板不受 Archive 列表格式变更影响。新增一个 `_sessions_to_client()` 辅助函数用于此端点。

MongoDB/archive 不可用时，降级读内存 `session._messages`（`session.messages_for_client()`），不返回 503。

### 5. 索引

`MessageArchive.connect()` 中加 `session_id` 复合索引，创建失败不影响启动：

```python
try:
    await self._db.ensure_index(
        self.COLLECTION, [("session_id", 1), ("_id", -1)]
    )
except Exception:
    logger.warning("MessageArchive: session_id index creation failed, continuing")
```

### 6. 数据迁移

启动时检测 `message_archive` 中缺少 `session_id` 的旧文档，补上当前 session_id。

**时机：必须在 `session.load()` 之后执行**（session_id 在 load() 中从 MongoDB 恢复，之前为默认值 "default"）。

`Agent._start_persistence()` 中的执行顺序：

```
connect_db() → archive.connect() → session.load() → migrate_archive_session_id()
```

策略：`update_many({"session_id": {"$exists": False}}, {"$set": {"session_id": current_sid}})`

单 session 场景下所有旧消息都归属当前 session，此策略正确。

## 不改的

- **Session 快照的写入逻辑**：`save()` 仍然整体写入 `sessions` 集合，用作 LLM 上下文恢复的缓存
- **Session 快照的加载逻辑**：`load()` 仍然从 `sessions` 快照恢复 `_messages` deque（快速恢复，不需要查 archive）
- **Archive 写入时机**：仍然在 `SessionMemory.add_message()` 中 fire-and-forget
- **Archive 删除逻辑**：`delete_one()` 不变

## 涉及文件

| 文件 | 改动 |
|------|------|
| `core/memory/message_archive.py` | 加 `session_id` 索引（try/except）、`query_by_session()`、`count_by_session()` |
| `core/memory/session.py` | `_archive_append()` 写入 `session_id`；新增 `migrate_archive_session_id()` |
| `core/input/server.py` | `sessions_list_handler` count 改读 archive；`session_detail_handler` 读 archive + 降级逻辑；新增 `_sessions_to_client()` |
| `core/agent.py` | `_start_persistence()` session.load() 之后调用 migration |
