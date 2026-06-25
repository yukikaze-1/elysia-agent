# 数据库连接告警 & Milvus PK 匹配 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复数据库连接失败静默降级（log level + 健康检查自动重连）+ Milvus search() 用 PK 替代 text key 避免同文异构条目覆盖

**Architecture:** 两处独立修复，不改数据结构或外部接口。Issue 1 改 3 个文件的日志级别 + 各加 `health_check()` 方法 + agent.py 新增 `_db_health_loop()`；Issue 2 仅改 `long_term.py:search()` 内部 key 方案

**Tech Stack:** Python 3.13, asyncio, pytest + unittest.mock

## Global Constraints

- 现有降级行为不变（数据库不可用时内存+JSONL 继续工作）
- `MemoryEntry` 公开 API 不变
- 对外接口（`insert()`, `search()`, `connect()`）签名不变
- 健康检查周期 120s
- 连续失败不打日志，仅恢复时打 info

---

### Task 1: 日志级别修正（3 文件，3 处改动）

**Files:**
- Modify: `core/memory/session.py:175,180`
- Modify: `core/memory/long_term.py:81`
- Modify: `core/agent.py:185-201`

**Interfaces:**
- Consumes: nothing (独立修复)
- Produces: nothing new (仅改日志级别)

- [ ] **Step 1: session.py — TTL 索引失败加 warning (L175)**

将 `core/memory/session.py` L175 的：
```python
            except Exception:
                pass  # TTL index not supported by all MongoDB-compatible backends
```
改为：
```python
            except Exception as e:
                logger.warning("SessionMemory: TTL index creation failed (%s), continuing", e)
```

- [ ] **Step 2: session.py — connect_db() info→warning (L180)**

将 `core/memory/session.py` L180 的：
```python
            logger.info("SessionMemory: MongoDB unavailable (%s), using in-memory", e)
```
改为：
```python
            logger.warning("SessionMemory: MongoDB unavailable (%s), using in-memory", e)
```

- [ ] **Step 3: long_term.py — connect() info→warning (L81)**

将 `core/memory/long_term.py` L81 的：
```python
            logger.info("LongTermMemory: Milvus unavailable (%s), using in-memory", e)
```
改为：
```python
            logger.warning("LongTermMemory: Milvus unavailable (%s), using in-memory", e)
```

- [ ] **Step 4: agent.py — 启动时告警 (L200-201 之后)**

在 `core/agent.py` `_start_persistence()` 方法中，`return mongo_ok, milvus_ok` 之前插入：
```python
        if not mongo_ok:
            logger.warning("MongoDB 不可用，会话记忆仅使用内存+JSONL 持久化")
        if not milvus_ok:
            logger.warning("Milvus 不可用，长期记忆检索降级为纯本地匹配")
```

- [ ] **Step 5: 运行现有测试确认无回归**

```bash
python -m pytest core/tests/ -v -x
```
Expected: all existing tests PASS

- [ ] **Step 6: Commit**

```bash
git add core/memory/session.py core/memory/long_term.py core/agent.py
git commit -m "fix: db connection failures use logger.warning, add startup alerts"
```

---

### Task 2: 健康检查方法

**Files:**
- Modify: `core/memory/long_term.py` — add `health_check()` method after `connect()`
- Modify: `core/memory/session.py` — add `health_check()` method after `connect_db()`

**Interfaces:**
- Consumes: `VectorDBFactory.create("milvus/memory")`, `DocumentDBFactory.create("mongodb/default")` (both already imported/used in respective files)
- Produces: `LongTermMemory.health_check() -> bool`, `SessionMemory.health_check() -> bool`

- [ ] **Step 1: long_term.py — 添加 health_check() 方法**

在 `core/memory/long_term.py` 的 `connect()` 方法之后（L84 空行后）插入：
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

- [ ] **Step 2: session.py — 添加 health_check() 方法**

在 `core/memory/session.py` 的 `connect_db()` 方法之后（L182 空行后）插入：
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

- [ ] **Step 3: 运行测试确认语法/导入正确**

```bash
python -c "from core.memory.session import SessionMemory; from core.memory.long_term import LongTermMemory; print('OK')"
```
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add core/memory/long_term.py core/memory/session.py
git commit -m "feat: add health_check() to LongTermMemory and SessionMemory"
```

---

### Task 3: agent.py 健康检查循环

**Files:**
- Modify: `core/agent.py` — add `_db_health_task` field, `_db_health_loop()`, create in `start()`, cancel in `stop()`

**Interfaces:**
- Consumes: `self._session.health_check() -> bool`, `self._long_term.health_check() -> bool` (from Task 2)
- Produces: `self._db_health_task: asyncio.Task | None`

- [ ] **Step 1: agent.py — 添加 _db_health_task 字段 (L142 后)**

在 `core/agent.py` `__init__` 中，`self._mailbox_task` 行（L142）之后插入：
```python
        self._db_health_task: asyncio.Task | None = None
```

- [ ] **Step 2: agent.py — 添加 _db_health_loop() 方法**

在 `core/agent.py` 的 `_start_persistence()` 方法之后（L201 空行后）插入：
```python
    async def _db_health_loop(self) -> None:
        """每 120s 检查数据库连接，失败时尝试自动重连。"""
        while True:
            await asyncio.sleep(120)
            try:
                mongo_ok = await self._session.health_check()
                milvus_ok = await self._long_term.health_check()
                self._mongo_available = mongo_ok
                self._milvus_available = milvus_ok
            except Exception:
                logger.debug("Database health check failed", exc_info=True)
```

- [ ] **Step 3: agent.py — start() 中启动健康检查任务 (L179 之后)**

在 `core/agent.py` `start()` 方法中，`self._start_mailbox_polling()`（L179）之后插入：
```python
        # 9. 数据库健康检查循环（120s 间隔自动重连）
        self._db_health_task = asyncio.create_task(self._db_health_loop())
```

- [ ] **Step 4: agent.py — stop() 中取消健康检查任务**

在 `core/agent.py` `stop()` 方法中，`# 4. 停止 mailbox 轮询` 之前（L369 前）插入：
```python
        # 3.5. 停止数据库健康检查
        await self._stop_db_health()
```

并在 `_stop_mailbox()` 方法之前（L439 前）插入 stop helper：
```python
    async def _stop_db_health(self) -> None:
        """停止数据库健康检查循环。"""
        if self._db_health_task:
            logger.debug("Cancelling db health task...")
            self._db_health_task.cancel()
            try:
                await asyncio.wait_for(self._db_health_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._db_health_task = None
```

- [ ] **Step 5: 运行测试确认无回归**

```bash
python -m pytest core/tests/ -v -x
```
Expected: all existing tests PASS

- [ ] **Step 6: Commit**

```bash
git add core/agent.py
git commit -m "feat: add db health check loop with 120s interval and auto-reconnect"
```

---

### Task 4: Milvus PK 匹配替代 text key

**Files:**
- Modify: `core/memory/long_term.py:310-396` — search() 方法完整重写

**Interfaces:**
- Consumes: `MemoryEntry._milvus_pk` (已有字段), `self._vector_db.search()` (已有, 返回结果中新增 `"id"` output_field)
- Produces: `search() -> list[MemoryEntry]` (签名不变, 返回值语义不变)

- [ ] **Step 1: 读取当前 search() 完整代码确认基线**

```bash
sed -n '310,396p' core/memory/long_term.py
```

- [ ] **Step 2: 替换 search() 方法 L324-L389**

**L324** — `distances` 类型声明：
```python
        distances: dict[str, float] = {}  # text → L2 distance (for rerank)
```
→
```python
        distances: dict[int, float] = {}  # id(entry) → L2 distance (for rerank)
```

**L341** — `output_fields` 加入 `"id"`：
```python
                        output_fields=["text", "memory_type", "poignancy", "timestamp"],
```
→
```python
                        output_fields=["id", "text", "memory_type", "poignancy", "timestamp"],
```

**L343-L354** — Milvus 结果索引 + 匹配：
```python
                    # Build text->distance lookup from Milvus results
                    milvus_texts: dict[str, float] = {}
                    for r in raw:
                        text = r.get("text", "")
                        milvus_texts[text] = float(r.get("distance", r.get("score", 1.0)))

                    # Match Milvus results against in-memory entries by text
                    # so returned entries have proper ids, embeddings, and metadata
                    for e in self._entries:
                        if e.text in milvus_texts:
                            candidates.append(e)
                            distances[e.text] = milvus_texts[e.text]
```
→
```python
                    # Build PK→distance + text→distance (fallback) from Milvus results
                    pk_to_distance: dict[int, float] = {}
                    text_fb: dict[str, float] = {}
                    for r in raw:
                        pk = r.get("id")
                        text = r.get("text", "")
                        d = float(r.get("distance", r.get("score", 1.0)))
                        if pk is not None:
                            pk_to_distance[pk] = d
                        else:
                            text_fb[text] = d

                    # Match Milvus results against in-memory entries: PK first, text fallback
                    for e in self._entries:
                        if e._milvus_pk is not None and e._milvus_pk in pk_to_distance:
                            candidates.append(e)
                            distances[id(e)] = pk_to_distance[e._milvus_pk]
                        elif e._milvus_pk is None and e.text in text_fb:
                            candidates.append(e)
                            distances[id(e)] = text_fb[e.text]
                            logger.debug("LongTermMemory: entry matched via text fallback (no PK)")
```

**L364** — `fb_scores` key：
```python
            fb_scores = {e.text: s for s, e in fallback_results}
```
→
```python
            fb_scores = {id(e): s for s, e in fallback_results}
```

**L367** — `distances` None 哨兵 key：
```python
                distances[entry.text] = None  # flag for rerank to use fb_score
```
→
```python
                distances[id(entry)] = None  # flag for rerank to use fb_score
```

**L377** — 重排序 distance lookup：
```python
            dist = distances.get(entry.text, 0.0)
```
→
```python
            dist = distances.get(id(entry), 0.0)
```

**L381** — 重排序 fb_score lookup：
```python
                sim = fb_scores.get(entry.text, 0.3)
```
→
```python
                sim = fb_scores.get(id(entry), 0.3)
```

- [ ] **Step 3: 运行现有测试确认无回归**

```bash
python -m pytest core/tests/ -v -x
```
Expected: all existing tests PASS

- [ ] **Step 4: Commit**

```bash
git add core/memory/long_term.py
git commit -m "fix: Milvus search uses PK matching, id(entry) keys — prevents same-text collision"
```

---

### Task 5: 单元测试

**Files:**
- Create: `core/memory/tests/__init__.py` (empty)
- Create: `core/memory/tests/test_long_term.py`
- Create: `core/memory/tests/test_session.py`

**Interfaces:**
- Consumes: `LongTermMemory`, `SessionMemory`, `MemoryEntry` (from Task 4, Task 2, Task 1)
- Produces: test coverage for connect/health_check/search behaviors

- [ ] **Step 1: 创建 `core/memory/tests/__init__.py`**

```bash
touch core/memory/tests/__init__.py
```

- [ ] **Step 2: 创建 `core/memory/tests/test_session.py` — log level + health_check 测试**

```python
"""SessionMemory: connect_db 日志级别 + health_check 重连 测试。"""
import logging
import pytest
from unittest.mock import AsyncMock, patch

from core.memory.session import SessionMemory


class TestSessionMemoryConnectLogLevel:
    """connect_db() 失败时必须使用 logger.warning，不是 info。"""

    def test_connect_failure_uses_warning(self, caplog):
        sm = SessionMemory(session_id="test-log")
        with patch(
            "Module.database.documentdb.DocumentDBFactory.create",
            side_effect=RuntimeError("connection refused"),
        ):
            with caplog.at_level(logging.WARNING):
                result = sm.connect_db()

        assert result is False
        assert sm._db is None  # 降级为 None
        # 必须有 WARNING 级别的日志
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) >= 1
        assert "MongoDB unavailable" in warnings[0].message


class TestSessionMemoryHealthCheck:
    """health_check() 在连接丢失后应尝试重连。"""

    @pytest.mark.asyncio
    async def test_returns_true_when_already_connected(self):
        sm = SessionMemory(session_id="test-hc")
        # 模拟已连接状态：_db 非 None
        sm._db = object()  # 非 None 表示已连接
        result = await sm.health_check()
        assert result is True

    @pytest.mark.asyncio
    async def test_reconnects_when_disconnected(self):
        sm = SessionMemory(session_id="test-hc")
        sm._db = None  # 模拟断开
        mock_db = object()
        with patch(
            "Module.database.documentdb.DocumentDBFactory.create",
            return_value=mock_db,
        ):
            result = await sm.health_check()

        assert result is True
        assert sm._db is mock_db

    @pytest.mark.asyncio
    async def test_returns_false_when_reconnect_fails(self):
        sm = SessionMemory(session_id="test-hc")
        sm._db = None
        with patch(
            "Module.database.documentdb.DocumentDBFactory.create",
            side_effect=RuntimeError("still down"),
        ):
            result = await sm.health_check()

        assert result is False
        assert sm._db is None
```

- [ ] **Step 3: 运行 test_session.py 确认通过**

```bash
python -m pytest core/memory/tests/test_session.py -v
```
Expected: 4 tests PASS

- [ ] **Step 4: 创建 `core/memory/tests/test_long_term.py` — log level + health_check 测试**

```python
"""LongTermMemory: connect 日志级别 + health_check 重连 测试。"""
import logging
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from core.memory.long_term import LongTermMemory


class TestLongTermMemoryConnectLogLevel:
    """connect() 失败时必须使用 logger.warning。"""

    def test_connect_failure_uses_warning(self, caplog):
        ltm = LongTermMemory(max_entries=10)
        with patch(
            "Module.database.vectordb.VectorDBFactory.create",
            side_effect=RuntimeError("connection refused"),
        ):
            with caplog.at_level(logging.WARNING):
                result = ltm.connect()

        assert result is False
        assert ltm._connected is False
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) >= 1
        assert "Milvus unavailable" in warnings[0].message


class TestLongTermMemoryHealthCheck:
    """health_check() 在连接丢失后应尝试重连。"""

    @pytest.mark.asyncio
    async def test_returns_true_when_already_connected(self):
        ltm = LongTermMemory(max_entries=10)
        ltm._connected = True
        result = await ltm.health_check()
        assert result is True

    @pytest.mark.asyncio
    async def test_reconnects_when_disconnected(self):
        ltm = LongTermMemory(max_entries=10)
        ltm._connected = False
        mock_db = MagicMock()
        mock_db.collection_exists = AsyncMock(return_value=True)
        with patch(
            "Module.database.vectordb.VectorDBFactory.create",
            return_value=mock_db,
        ):
            result = await ltm.health_check()

        assert result is True
        assert ltm._connected is True
        assert ltm._vector_db is mock_db

    @pytest.mark.asyncio
    async def test_returns_false_when_reconnect_fails(self):
        ltm = LongTermMemory(max_entries=10)
        ltm._connected = False
        with patch(
            "Module.database.vectordb.VectorDBFactory.create",
            side_effect=RuntimeError("still down"),
        ):
            result = await ltm.health_check()

        assert result is False
        assert ltm._connected is False
```

- [ ] **Step 5: 运行 test_long_term.py 确认前 4 个测试通过**

```bash
python -m pytest core/memory/tests/test_long_term.py -v -k "not TestPKMatching"
```
Expected: 4 tests PASS

- [ ] **Step 6: 追加 Issue 2 的 PK 匹配测试到 test_long_term.py**

在 `core/memory/tests/test_long_term.py` 末尾追加：
```python
class TestPKMatching:
    """PK 匹配: 同文异构条目各自获得正确 distance。"""

    @pytest.mark.asyncio
    async def test_only_matching_pk_appears(self):
        """Milvus 只返回一个 PK 时，仅该 PK 对应条目出现（旧代码 text 匹配会两个都出现）。"""
        from core.memory.long_term import MemoryEntry

        ltm = LongTermMemory(max_entries=10)
        ltm._connected = True

        # 两个同文异构条目
        e1 = MemoryEntry(
            id="id-1", text="我喜欢吃披萨", memory_type="note",
            poignancy=5, timestamp=1000.0, _milvus_pk=101,
        )
        e2 = MemoryEntry(
            id="id-2", text="我喜欢吃披萨", memory_type="note",
            poignancy=8, timestamp=2000.0, _milvus_pk=102,
        )
        ltm._entries = [e1, e2]

        # Milvus 只返回 PK 101，不返回 102
        mock_db = MagicMock()
        mock_db.search = AsyncMock(return_value=[
            {"id": 101, "text": "我喜欢吃披萨", "distance": 0.3},
        ])
        ltm._vector_db = mock_db

        results = await ltm.search(query_embedding=[0.1] * 896, top_k=5)

        # 只有 e1 应该出现（PK 匹配），e2 不应出现（其 PK 不在 Milvus 结果中）
        ids_in_results = {e.id for e in results}
        assert "id-1" in ids_in_results
        assert "id-2" not in ids_in_results, (
            "e2 的 PK 102 不在 Milvus 结果中，不应被匹配到"
        )

    @pytest.mark.asyncio
    async def test_both_pks_in_results_both_appear(self):
        """两个不同 PK 的条目都在 Milvus 结果中时，两个都应出现且各自正确 distance。"""
        from core.memory.long_term import MemoryEntry

        ltm = LongTermMemory(max_entries=10)
        ltm._connected = True

        e1 = MemoryEntry(
            id="id-1", text="同文", memory_type="note",
            poignancy=5, timestamp=1000.0, _milvus_pk=101,
        )
        e2 = MemoryEntry(
            id="id-2", text="同文", memory_type="note",
            poignancy=5, timestamp=1000.0, _milvus_pk=102,
        )
        ltm._entries = [e1, e2]

        mock_db = MagicMock()
        mock_db.search = AsyncMock(return_value=[
            {"id": 101, "text": "同文", "distance": 0.3},
            {"id": 102, "text": "同文", "distance": 0.7},
        ])
        ltm._vector_db = mock_db

        results = await ltm.search(query_embedding=[0.1] * 896, top_k=5)

        # 两个 PK 都在 Milvus 结果中 → 两个都出现
        ids_in_results = {e.id for e in results}
        assert "id-1" in ids_in_results
        assert "id-2" in ids_in_results
        # e1 (distance=0.3, sim=1/1.3≈0.769) 应排在 e2 (distance=0.7, sim=1/1.7≈0.588) 前面
        # 因 poignancy/timestamp 相同，sim 是唯一差异
        idx1 = next(i for i, e in enumerate(results) if e.id == "id-1")
        idx2 = next(i for i, e in enumerate(results) if e.id == "id-2")
        assert idx1 < idx2, f"e1 (distance=0.3) 应排在 e2 (distance=0.7) 之前，实际: {idx1} vs {idx2}"

    @pytest.mark.asyncio
    async def test_text_fallback_when_no_pk(self):
        """_milvus_pk=None 的历史条目仍能通过 text fallback 匹配。"""
        from core.memory.long_term import MemoryEntry

        ltm = LongTermMemory(max_entries=10)
        ltm._connected = True

        e1 = MemoryEntry(
            id="old-1", text="历史记忆", memory_type="note",
            poignancy=5, timestamp=1000.0, _milvus_pk=None,  # 无 PK
        )
        ltm._entries = [e1]

        mock_db = MagicMock()
        # Milvus 返回无 id 字段的结果（模拟旧数据）
        mock_db.search = AsyncMock(return_value=[
            {"text": "历史记忆", "distance": 0.5},
        ])
        ltm._vector_db = mock_db

        results = await ltm.search(query_embedding=[0.1] * 896, top_k=5)

        assert len(results) == 1
        assert results[0].id == "old-1"

    @pytest.mark.asyncio
    async def test_no_id_field_in_raw_falls_back_to_text(self):
        """Milvus 返回结果中无 id 字段时，走 text fallback 不崩溃。"""
        from core.memory.long_term import MemoryEntry

        ltm = LongTermMemory(max_entries=10)
        ltm._connected = True

        e1 = MemoryEntry(
            id="e1", text="无ID字段测试", memory_type="note",
            poignancy=5, timestamp=1000.0, _milvus_pk=None,
        )
        ltm._entries = [e1]

        mock_db = MagicMock()
        # raw 中没有 "id" 字段
        mock_db.search = AsyncMock(return_value=[
            {"text": "无ID字段测试", "distance": 0.2},
        ])
        ltm._vector_db = mock_db

        results = await ltm.search(query_embedding=[0.1] * 896, top_k=5)

        # 不应崩溃，应通过 text fallback 找到
        assert len(results) == 1
        assert results[0].id == "e1"
```

- [ ] **Step 7: 运行全部新增测试**

```bash
python -m pytest core/memory/tests/ -v
```
Expected: 12 tests PASS (4 session + 4 log/health + 4 PK matching)

- [ ] **Step 8: 运行全部现有测试确认无回归**

```bash
python -m pytest core/tests/ -v -x
```
Expected: all existing tests PASS

- [ ] **Step 9: Commit**

```bash
git add core/memory/tests/
git commit -m "test: add unit tests for db log levels, health_check, and Milvus PK matching"
```
