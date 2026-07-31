# 数据库连接告警 & Milvus PK 匹配 — 设计文档

日期: 2026-06-25
状态: 已确认
关联 TODO: todo.md #15 (Milvus text key 覆盖), #16 (数据库静默降级)

---

## 概述

修复两个低优先级已知 bug：

1. **数据库连接失败静默降级无 warning** — MongoDB/Milvus 连接失败时使用 `logger.info()`，且启动后无健康检查/自动重连
2. **Milvus 匹配用 text 做 key 丢失同文异构条目** — `long_term.py:search()` 用 text 去重 Milvus 结果，相同文本不同向量的条目被覆盖

---

## Issue 1: 数据库连接失败告警 + 健康检查

### 1.1 日志级别修正

| 文件 | 位置 | 当前 | 改为 |
|------|------|------|------|
| `session.py:connect_db()` | ~L180 | `logger.info("MongoDB unavailable...")` | `logger.warning(...)` |
| `long_term.py:connect()` | ~L81 | `logger.info("Milvus unavailable...")` | `logger.warning(...)` |
| `session.py` | ~L175 | `except Exception: pass` (TTL 索引) | `logger.warning("TTL index creation failed: ...")` |

### 1.2 启动时告警

`agent.py:_start_persistence()` — `asyncio.gather` 后增加：

```python
if not mongo_ok:
    logger.warning("MongoDB 不可用，会话记忆仅使用内存+JSONL 持久化")
if not milvus_ok:
    logger.warning("Milvus 不可用，长期记忆检索降级为纯本地匹配")
```

### 1.3 健康检查 + 自动重连

#### LongTermMemory.health_check()

```python
async def health_check(self) -> bool:
    """尝试重连 Milvus。已连接则直接返回 True。"""
    if self._connected:
        return True
    try:
        from Module.database.vectordb import VectorDBFactory
        self._vector_db = VectorDBFactory.create("milvus/memory")
        self._connected = True
        logger.info("LongTermMemory: Milvus 已恢复连接")
        return True
    except Exception:
        return False
```

#### SessionMemory.health_check()

```python
async def health_check(self) -> bool:
    """尝试重连 MongoDB。已连接则直接返回 True。"""
    if self._db is not None:
        return True
    try:
        from Module.database.documentdb import DocumentDBFactory
        self._db = DocumentDBFactory.create("mongodb/default")
        logger.info("SessionMemory: MongoDB 已恢复连接")
        return True
    except Exception:
        return False
```

#### agent.py 健康检查循环

- 间隔: **120 秒**
- `start()` 中 `asyncio.create_task(self._db_health_loop())`
- `stop()` 中 cancel
- 首次恢复成功打 `logger.info`，连续失败不打日志（避免洪泛，首次失败在启动时已 warning）

```python
async def _db_health_loop(self):
    """每 120s 检查数据库连接，失败时尝试自动重连。"""
    while True:
        await asyncio.sleep(120)
        mongo_ok = await self._session.health_check()
        milvus_ok = await self._long_term.health_check()
        self._mongo_available = mongo_ok
        self._milvus_available = milvus_ok
```

### 1.4 设计决定

- 健康检查周期 120s：MongoDB `serverSelectionTimeoutMS=3000`，Milvus 首次操作才失败；120s 足够长避免日志洪泛，足够短让管理员不用等太久
- 不引入共享基础设施：仅两个消费者（MongoDB + Milvus），各自 `health_check()` ~10 行，遵守 YAGNI
- 连续失败不打日志：避免每 120s 刷一条 warning；恢复时才打 info

---

## Issue 2: Milvus PK 匹配替代 text key

### 2.1 问题根因

`long_term.py:search()` 方法跨越 L310-L396，包含三个区域，均以 `entry.text` 为 key，导致同文异构条目被覆盖：

**区域 A — Milvus 结果索引 (L343-L354)：**

```python
milvus_texts: dict[str, float] = {}
for r in raw:
    text = r.get("text", "")
    milvus_texts[text] = float(r.get("distance", ...))  # 同 text 覆盖: 后值覆盖前值

for e in self._entries:
    if e.text in milvus_texts:          # 所有同文条目拿同一个 distance
        candidates.append(e)
        distances[e.text] = milvus_texts[e.text]  # distances keyed by text
```

**区域 B — 回退路径 (L358-L367)：**

```python
fb_scores: dict[str, float] = {}
if not candidates:
    ...
    fb_scores = {e.text: s for s, e in fallback_results}  # keyed by text
    for fb_score, entry in fallback_results:
        candidates.append(entry)
        distances[entry.text] = None  # None 哨兵: 告诉 rerank 用 fb_score
```

**区域 C — 重排序循环 (L372-L389)：**

```python
for entry in candidates:
    dist = distances.get(entry.text, 0.0)       # 按 text 查找 distance
    if dist is not None:
        sim = 1.0 / (1.0 + dist)                 # Milvus 距离
    else:
        sim = fb_scores.get(entry.text, 0.3)     # 回退分数 (None 哨兵触发)
    ...
```

`MemoryEntry._milvus_pk` 在 insert 时写入，但 search 时 `output_fields` 未请求 `id`，也未用 PK 匹配。

### 2.2 修复方案：统一用 `id(entry)` 做 key

核心思路：将 `distances` 和 `fb_scores` 的 key 从 `entry.text`（字符串，会冲突）改为 `id(entry)`（Python 对象 id，进程内唯一）。Milvus 匹配侧用 PK 做精确关联，回退侧直接用 `id(entry)`。

#### Step 1 — output_fields 加入 "id" (L341)

```python
output_fields=["id", "text", "memory_type", "poignancy", "timestamp"]
```

#### Step 2 — 建双索引 (替代 L343-L347)

```python
pk_to_distance: dict[int, float] = {}
text_fallback: dict[str, float] = {}
for r in raw:
    pk = r.get("id")
    text = r.get("text", "")
    distance = float(r.get("distance", r.get("score", 1.0)))
    if pk is not None:
        pk_to_distance[pk] = distance
    else:
        text_fallback[text] = distance
```

#### Step 3 — 匹配 self._entries, distances keyed by id(entry) (替代 L349-L354)

```python
for e in self._entries:
    if e._milvus_pk is not None and e._milvus_pk in pk_to_distance:
        candidates.append(e)
        distances[id(e)] = pk_to_distance[e._milvus_pk]
    elif e._milvus_pk is None and e.text in text_fallback:
        candidates.append(e)
        distances[id(e)] = text_fallback[e.text]
        logger.debug("LongTermMemory: entry matched via text fallback (no PK)")
```

#### Step 4 — 回退路径: fb_scores 同步改为 id(entry) key (替代 L364, L367)

```python
fb_scores = {id(e): s for s, e in fallback_results}       # 原: {e.text: s}
for fb_score, entry in fallback_results:
    candidates.append(entry)
    distances[id(entry)] = None                             # 原: distances[entry.text] = None
```

#### Step 5 — 重排序循环: lookup 改为 id(entry) (替代 L377, L381)

```python
for entry in candidates:
    dist = distances.get(id(entry), 0.0)                   # 原: distances.get(entry.text, 0.0)
    if dist is not None:
        sim = 1.0 / (1.0 + dist)
    else:
        sim = fb_scores.get(id(entry), 0.3)                # 原: fb_scores.get(entry.text, 0.3)
    ...
```

#### Step 6 — distances 类型声明更新 (L324)

```python
distances: dict[int, float] = {}  # 原: dict[str, float] — id(entry) → L2 distance
```

### 2.3 兼容性

- `_milvus_pk` 为 `None` 的历史条目走 text fallback，行为等价于修复前
- `_milvus_pk` 非 `None` 的新条目走 PK 精确匹配，同一 text 不同向量的条目各自获得正确 distance
- text fallback 路径打 `logger.debug`，方便排查历史数据迁移进度
- `fb_scores` 和 `distances` key 类型同步变更，None 哨兵机制保持不变

### 2.4 影响范围

仅 `long_term.py:search()` 方法，L310-L396。不改 `MemoryEntry` 数据结构，不改 `insert()` 路径。

---

## 测试要点

### Issue 1
- 单元测试：MongoDB/Milvus 不可用时 `connect_db()`/`connect()` 返回 `False` + 打 warning
- 单元测试：`health_check()` 在连接恢复后返回 `True` + 打 info
- 集成测试：启动 agent 时数据库不可用，日志中有 warning；数据库恢复后，120s 内日志出现恢复 info

### Issue 2
- 单元测试：两条 `_milvus_pk` 不同但 `text` 相同的 `MemoryEntry`，search 后各自获得正确的 distance（不等同）
- 单元测试：`_milvus_pk=None` 的历史条目仍能通过 text fallback 匹配
- 单元测试：Milvus 返回结果中无 `id` 字段时（模拟旧数据），走 text fallback 不崩溃

---

## 不变量

- 现有降级行为不变（数据库不可用时内存+JSONL 继续工作）
- `MemoryEntry` 公开 API 不变
- 对外接口（`insert()`, `search()`, `connect()`）签名不变
