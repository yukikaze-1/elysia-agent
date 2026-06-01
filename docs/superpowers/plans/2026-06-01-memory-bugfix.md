# Memory System Bugfix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 14 bugs/logic issues found in the 2026-06-01 memory system review.

**Architecture:** The fixes are scoped to `core/memory/` + `core/agent.py` + `core/consolidator.py` + `core/tools/memory.py` + `core/app_config.py`. No new files. Each task is self-contained with its own test, isolated from other tasks.

**Tech Stack:** Python 3.13, pytest, asyncio

---

## File Map

| File | Tasks | What changes |
|------|-------|-------------|
| `core/memory/long_term.py` | T1–T7 | metadata copy, search link, dirty_ids cleanup, embedding await, dedup, memory_type filter |
| `core/memory/session.py` | T8–T9 | datetime import, MongoDB doc size cap |
| `core/agent.py` | T10–T11 | seed memories via insert(), consolidator flush on stop |
| `core/consolidator.py` | T12–T13 | flush() method, lower threshold |
| `core/tools/memory.py` | T14 | return memory IDs in search output |
| `core/app_config.py` | T15 | expand MemoryConfig with ltm params |
| `core/tests/test_phase2.py` | T1–T15 | new tests for each fix |
| `docs/ref_memory-architecture.md` | T16 | deprecation notice |

---

### Task 1: Fix `insert()` mutating caller's metadata dict

**Files:**
- Modify: `core/memory/long_term.py:136-160`
- Test: `core/tests/test_phase2.py` (add to TestLongTermMemoryEdgeCases)

- [ ] **Step 1: Write the failing test**

Add to `core/tests/test_phase2.py` in the `TestLongTermMemoryEdgeCases` class:

```python
@pytest.mark.asyncio
async def test_insert_does_not_mutate_caller_metadata(self):
    from core.memory.long_term import LongTermMemory
    mem = LongTermMemory()
    caller_meta = {"memory_type": "plan", "poignancy": 8, "custom_key": "value"}
    original = dict(caller_meta)

    await mem.insert("test content", **caller_meta)

    # caller's dict should be unchanged
    assert caller_meta == original
    assert caller_meta["custom_key"] == "value"
    assert caller_meta["memory_type"] == "plan"
    assert caller_meta["poignancy"] == 8
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest core/tests/test_phase2.py::TestLongTermMemoryEdgeCases::test_insert_does_not_mutate_caller_metadata -v --asyncio-mode=auto`
Expected: FAIL — caller_meta keys are popped, `custom_key` missing

- [ ] **Step 3: Fix the insert() method**

In `core/memory/long_term.py`, change `insert()` to work on a local copy of metadata:

```python
async def insert(self, text: str, **metadata) -> None:
    """Insert a memory entry. Auto-embeds lazily if embedding is available.

    Keyword metadata may include: memory_type, poignancy, timestamp, source, etc.
    These become structured fields on MemoryEntry and are stored in Milvus.
    """
    await self._ensure_embedding()

    embedding: list[float] = []
    if self._embedding:
        try:
            embedding = await self._embedding.embed_query(text)
        except Exception:
            logger.debug("Embedding failed for insert, storing text-only", exc_info=True)

    import uuid
    # Work on a copy to avoid mutating caller's dict
    meta = dict(metadata)
    entry = MemoryEntry(
        id=uuid.uuid4().hex[:12],
        text=text,
        embedding=embedding,
        memory_type=meta.pop("memory_type", "note"),
        poignancy=meta.pop("poignancy", 5),
        timestamp=meta.pop("timestamp", _time.time()),
        metadata=meta,
    )

    if self._vector_db and embedding:
        try:
            await self._vector_db.insert(
                self.COLLECTION,
                vectors=[embedding],
                metadata=[{
                    "text": text,
                    "memory_type": entry.memory_type,
                    "poignancy": entry.poignancy,
                    "timestamp": entry.timestamp,
                    **meta,
                }],
            )
        except Exception as e:
            logger.warning("Milvus insert failed: %s, falling back to in-memory", e)
            self._dirty_ids.add(entry.id)
    elif not self._vector_db:
        self._dirty_ids.add(entry.id)

    self._entries.append(entry)
    self._evict_old_entries()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest core/tests/test_phase2.py::TestLongTermMemoryEdgeCases::test_insert_does_not_mutate_caller_metadata -v --asyncio-mode=auto`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/memory/long_term.py core/tests/test_phase2.py
git commit -m "fix: insert() no longer mutates caller's metadata dict"
```

---

### Task 2: Fix search() to link Milvus results to existing in-memory entries

**Files:**
- Modify: `core/memory/long_term.py:276-294`
- Test: `core/tests/test_phase2.py` (add to TestLongTermMemoryEdgeCases)

- [ ] **Step 1: Write the failing test**

Add to `core/tests/test_phase2.py` in `TestLongTermMemoryEdgeCases`:

```python
@pytest.mark.asyncio
async def test_search_returns_entries_with_ids(self):
    """Milvus search results should link back to _entries with proper IDs."""
    from core.memory.long_term import LongTermMemory
    mem = LongTermMemory()
    await mem.insert("妖梦最喜欢的颜色是蓝色", memory_type="preference", poignancy=9)
    await mem.insert("明天要去爬山", memory_type="plan", poignancy=7)

    results = await mem.search(query_text="颜色")
    assert len(results) >= 1
    for r in results:
        # Every result should have a non-empty id (linked from _entries)
        assert r.id, f"Expected non-empty id, got {r.id!r} for text={r.text!r}"
        # embedding should be preserved from the stored entry
        assert isinstance(r.embedding, list)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest core/tests/test_phase2.py::TestLongTermMemoryEdgeCases::test_search_returns_entries_with_ids -v --asyncio-mode=auto`
Expected: FAIL — entries returned by search have empty id

- [ ] **Step 3: Fix the search() method**

In `core/memory/long_term.py`, replace the Milvus result construction block. When Milvus returns results, match them against `_entries` by text instead of creating new `MemoryEntry` objects:

```python
            if vec:
                try:
                    raw = await self._vector_db.search(
                        self.COLLECTION,
                        vector=vec,
                        top_k=min(top_k * 4, 20),
                        output_fields=["text", "memory_type", "poignancy", "timestamp"],
                    )
                    # Build text→distance lookup
                    milvus_texts: dict[str, float] = {}
                    for r in raw:
                        text = r.get("text", "")
                        milvus_texts[text] = float(r.get("distance", r.get("score", 1.0)))

                    # Match Milvus results against in-memory entries by text
                    # so we get proper ids, embeddings, and full metadata
                    for e in self._entries:
                        if e.text in milvus_texts:
                            candidates.append(e)
                            distances[e.text] = milvus_texts[e.text]
                except Exception as e:
                    logger.warning("Milvus search failed: %s", e)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest core/tests/test_phase2.py::TestLongTermMemoryEdgeCases::test_search_returns_entries_with_ids -v --asyncio-mode=auto`
Expected: PASS

- [ ] **Step 5: Run existing tests to check no regressions**

Run: `python -m pytest core/tests/test_phase2.py -v --asyncio-mode=auto`
Expected: All memory tests pass

- [ ] **Step 6: Commit**

```bash
git add core/memory/long_term.py core/tests/test_phase2.py
git commit -m "fix: search() links Milvus results to in-memory entries, preserving ids"
```

---

### Task 3: Clean `_dirty_ids` on entry eviction

**Files:**
- Modify: `core/memory/long_term.py:184-212`
- Test: `core/tests/test_phase2.py` (add standalone test)

- [ ] **Step 1: Write the failing test**

Add to `core/tests/test_phase2.py` as a standalone function:

```python
@pytest.mark.asyncio
async def test_long_term_memory_eviction_cleans_dirty_ids():
    """Evicted entries should also be removed from _dirty_ids."""
    from core.memory.long_term import LongTermMemory, MemoryEntry
    import time as _time
    mem = LongTermMemory()
    # Bypass insert to control dirty_ids directly
    entry = MemoryEntry(
        id="test_dirty_evict",
        text="old entry",
        embedding=[0.1] * 10,
        memory_type="fact",
        poignancy=5,
        timestamp=_time.time() - 100 * 86400,  # 100 days old — should evict
    )
    mem._entries.append(entry)
    mem._dirty_ids.add("test_dirty_evict")

    removed = mem._evict_old_entries()
    assert removed >= 1
    assert "test_dirty_evict" not in mem._dirty_ids, (
        "Evicted entry should be removed from _dirty_ids"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest core/tests/test_phase2.py::test_long_term_memory_eviction_cleans_dirty_ids -v --asyncio-mode=auto`
Expected: FAIL — `test_dirty_evict` still in `_dirty_ids` after eviction

- [ ] **Step 3: Fix `_evict_old_entries()`**

In `core/memory/long_term.py`, collect evicted entry ids and clean `_dirty_ids`:

```python
def _evict_old_entries(self) -> int:
    """驱逐过期和超过上限的条目。返回驱逐数量。

    在每次 insert() 后调用，保持内存列表健康。
    """
    now = _time.time()
    cutoff = now - (self._MAX_AGE_DAYS * 86400)
    removed = 0
    evicted_ids: set[str] = set()

    # 移除过期条目
    new_entries = []
    for e in self._entries:
        if e.timestamp >= cutoff:
            new_entries.append(e)
        else:
            evicted_ids.add(e.id)
            removed += 1
    self._entries = new_entries

    # 移除超过上限的条目（保留最新的）
    if len(self._entries) > self._MAX_ENTRIES:
        self._entries.sort(key=lambda e: e.timestamp, reverse=True)
        overflow = len(self._entries) - self._MAX_ENTRIES
        for e in self._entries[-overflow:]:
            evicted_ids.add(e.id)
        self._entries = self._entries[:self._MAX_ENTRIES]
        removed += overflow

    # 同步清理 dirty_ids
    if evicted_ids:
        self._dirty_ids -= evicted_ids

    if removed:
        logger.info("Long-term memory: 驱逐 %d 条过期/超限条目 (剩余 %d)",
                   removed, len(self._entries))
    return removed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest core/tests/test_phase2.py::test_long_term_memory_eviction_cleans_dirty_ids -v --asyncio-mode=auto`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/memory/long_term.py core/tests/test_phase2.py
git commit -m "fix: clean _dirty_ids when entries are evicted"
```

---

### Task 4: Await embedding before first insert (fix cold-start text-only trap)

**Files:**
- Modify: `core/memory/long_term.py:142, 84-108`
- Test: `core/tests/test_phase2.py` (add to TestLongTermMemory)

- [ ] **Step 1: Write the failing test**

Add to `core/tests/test_phase2.py` in `TestLongTermMemory`:

```python
@pytest.mark.asyncio
async def test_insert_awaits_embedding_on_first_call(self):
    """First insert() should wait for the embedding task to complete,
    so the entry gets an embedding. Only applicable when embedding
    is configured — test verifies the await pattern works."""
    from core.memory.long_term import LongTermMemory
    mem = LongTermMemory()

    # Simulate what happens on first insert: ensure_embedding
    # creates a task, and insert should await its completion
    await mem._ensure_embedding()
    # If embedding task was created, wait for it
    if mem._embed_task and not mem._embed_task.done():
        await mem._embed_task

    # Now insert — should have embedding available if any backend is configured
    await mem.insert("test cold start", memory_type="fact")
    entry = mem._entries[0] if mem._entries else None
    assert entry is not None
    # If embedding backend is available, entry should have embedding
    if mem._embedding is not None:
        assert len(entry.embedding) > 0, "Entry should have embedding after await"
```

- [ ] **Step 2: Run test to verify current behavior**

Run: `python -m pytest core/tests/test_phase2.py::TestLongTermMemory::test_insert_awaits_embedding_on_first_call -v --asyncio-mode=auto`
Expected: May PASS or FAIL depending on timing — documents the desired behavior

- [ ] **Step 3: Fix `_ensure_embedding()` to optionally await**

Change `_ensure_embedding()` to accept a `wait` parameter, and call it with `wait=True` from `insert()`:

```python
async def _ensure_embedding(self, wait: bool = False) -> None:
    """Load embedding backend lazily. Only tries once.

    Runs in a thread pool so the blocking model load doesn't stall the event loop.
    When wait=True, awaits the loading task so the caller gets embeddings.
    Otherwise fire-and-forget (search can fall back to text).
    """
    if self._embedding is not None:
        return
    if self._embedding_tried:
        if wait and self._embed_task and not self._embed_task.done():
            # Wait for the in-flight task to finish
            try:
                await asyncio.wait_for(self._embed_task, timeout=self.EMBED_TIMEOUT)
            except (asyncio.TimeoutError, Exception):
                pass
        return
    self._embedding_tried = True

    loop = asyncio.get_event_loop()

    async def _load():
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, self._try_load_embedding_sync),
                timeout=self.EMBED_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.info("LongTermMemory: embedding load timed out after %.0fs, text-only mode", self.EMBED_TIMEOUT)
        except Exception as e:
            logger.info("LongTermMemory: embedding unavailable (%s), text-only mode", e)

    self._embed_task = asyncio.create_task(_load(), name="embedding-load")

    if wait:
        try:
            await asyncio.wait_for(asyncio.shield(self._embed_task), timeout=self.EMBED_TIMEOUT)
        except (asyncio.TimeoutError, Exception):
            pass
```

Then in `insert()`, change the call:
```python
await self._ensure_embedding(wait=True)
```

In `search()`, keep the fire-and-forget call unchanged:
```python
await self._ensure_embedding()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest core/tests/test_phase2.py::TestLongTermMemory::test_insert_awaits_embedding_on_first_call -v --asyncio-mode=auto`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/memory/long_term.py core/tests/test_phase2.py
git commit -m "fix: await embedding load on first insert() to avoid cold-start text-only trap"
```

---

### Task 5: Add basic dedup in insert()

**Files:**
- Modify: `core/memory/long_term.py:136` (insert method)
- Test: `core/tests/test_phase2.py` (add to TestLongTermMemoryEdgeCases)

- [ ] **Step 1: Write the failing test**

Add to `core/tests/test_phase2.py` in `TestLongTermMemoryEdgeCases`:

```python
@pytest.mark.asyncio
async def test_insert_dedup_identical_content(self):
    """Inserting identical text twice should not create a duplicate entry."""
    from core.memory.long_term import LongTermMemory
    mem = LongTermMemory()
    await mem.insert("妖梦喜欢蓝色", memory_type="preference")
    count_after_first = mem.count
    await mem.insert("妖梦喜欢蓝色", memory_type="preference")
    # Should still be 1 (deduped, not 2)
    assert mem.count == count_after_first, (
        f"Expected {count_after_first} after dedup, got {mem.count}"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest core/tests/test_phase2.py::TestLongTermMemoryEdgeCases::test_insert_dedup_identical_content -v --asyncio-mode=auto`
Expected: FAIL — `mem.count == 2` (duplicate inserted)

- [ ] **Step 3: Add dedup check to insert()**

At the beginning of `insert()`, after `_ensure_embedding()`, add:

```python
    # Dedup: skip if identical text already exists
    for e in self._entries:
        if e.text.strip() == text.strip():
            logger.debug("Duplicate entry skipped: %s", text[:60])
            return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest core/tests/test_phase2.py::TestLongTermMemoryEdgeCases::test_insert_dedup_identical_content -v --asyncio-mode=auto`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/memory/long_term.py core/tests/test_phase2.py
git commit -m "fix: dedup identical text in insert()"
```

---

### Task 6: Add memory_type filter to search()

**Files:**
- Modify: `core/memory/long_term.py:252-258` (search signature + body)
- Test: `core/tests/test_phase2.py` (add to TestLongTermMemoryEdgeCases)

- [ ] **Step 1: Write the failing test**

Add to `core/tests/test_phase2.py` in `TestLongTermMemoryEdgeCases`:

```python
@pytest.mark.asyncio
async def test_search_filter_by_memory_type(self):
    """search() with memory_type should only return matching types."""
    from core.memory.long_term import LongTermMemory
    mem = LongTermMemory()
    await mem.insert("工作计划A", memory_type="plan")
    await mem.insert("工作计划B", memory_type="plan")
    await mem.insert("用户偏好蓝色", memory_type="preference")

    plans = await mem.search(query_text="工作", memory_type="plan", top_k=10)
    assert len(plans) >= 2
    assert all(e.memory_type == "plan" for e in plans)

    prefs = await mem.search(query_text="蓝色", memory_type="preference", top_k=10)
    assert len(prefs) >= 1
    assert all(e.memory_type == "preference" for e in prefs)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest core/tests/test_phase2.py::TestLongTermMemoryEdgeCases::test_search_filter_by_memory_type -v --asyncio-mode=auto`
Expected: FAIL — filter not applied, returns entries of all types

- [ ] **Step 3: Add memory_type parameter and filter**

In `core/memory/long_term.py`, update the `search()` signature and add filtering:

```python
async def search(
    self,
    query_text: str = "",
    query_embedding: list[float] | None = None,
    top_k: int = 5,
    memory_type: str | None = None,
) -> list[MemoryEntry]:
```

Then, after the rerank step (before `return`), add the filter:

```python
    # Filter by memory_type if requested
    if memory_type:
        scored = [(s, e) for s, e in scored if e.memory_type == memory_type]

    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:top_k]]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest core/tests/test_phase2.py::TestLongTermMemoryEdgeCases::test_search_filter_by_memory_type -v --asyncio-mode=auto`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/memory/long_term.py core/tests/test_phase2.py
git commit -m "feat: add memory_type filter to search()"
```

---

### Task 7: Move `__import__("datetime")` to top-level import

**Files:**
- Modify: `core/memory/session.py:1-12, 67-73`

- [ ] **Step 1: Make the fix**

In `core/memory/session.py`:

Add at the top (line 8, after `import logging`):
```python
from datetime import datetime
```

Change line 72 from:
```python
"updated_at": __import__("datetime").datetime.now().isoformat(),
```
to:
```python
"updated_at": datetime.now().isoformat(),
```

- [ ] **Step 2: Run existing tests to verify no regressions**

Run: `python -m pytest core/tests/test_phase2.py -v --asyncio-mode=auto`
Expected: All session tests pass

- [ ] **Step 3: Commit**

```bash
git add core/memory/session.py
git commit -m "fix: use top-level datetime import instead of __import__() in hot path"
```

---

### Task 8: Cap MongoDB session document size

**Files:**
- Modify: `core/memory/session.py:63-83` (save method)
- Test: `core/tests/test_phase2.py` (add to TestSessionMemoryEdgeCases)

- [ ] **Step 1: Write the test**

Add to `core/tests/test_phase2.py` in `TestSessionMemoryEdgeCases`:

```python
def test_save_caps_messages_to_max(self):
    """save() should only persist max_messages worth of messages,
    not the full append-only log."""
    from core.memory.session import SessionMemory
    s = SessionMemory(max_messages=3)
    for i in range(10):
        s.add_message("user", f"msg{i}")
    snap = s.snapshot()
    assert len(snap["messages"]) == 3
    # The snapshot should contain only the last 3 messages
    assert snap["messages"][0]["content"] == "msg7"
    assert snap["messages"][2]["content"] == "msg9"
```

- [ ] **Step 2: Run test to verify it passes (snapshot already correct)**

Run: `python -m pytest core/tests/test_phase2.py::TestSessionMemoryEdgeCases::test_save_caps_messages_to_max -v`
Expected: PASS (deque already enforces maxlen on snapshot)

- [ ] **Step 3: Add document size cap comment in save()**

The deque's `maxlen` already limits messages in memory and in `snapshot()`. The issue is that MongoDB stores whatever `list(self._messages)` gives — which is already capped. But previously-stored historical messages in the MongoDB document aren't pruned on the DB side. Add a comment and a TTL index attempt in `connect_db()`:

In `connect_db()`, add after the existing `ensure_index` calls:
```python
            # Try to create TTL index on updated_at (auto-delete sessions older than 30 days)
            try:
                await self._db.ensure_index(
                    "sessions",
                    [("updated_at", 1)],
                    expireAfterSeconds=30 * 86400,  # 30 days
                )
            except Exception:
                pass  # TTL index not supported by all MongoDB-compatible backends
```

- [ ] **Step 4: Verify tests still pass**

Run: `python -m pytest core/tests/test_phase2.py -v --asyncio-mode=auto`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add core/memory/session.py core/tests/test_phase2.py
git commit -m "fix: add TTL index attempt for MongoDB session documents"
```

---

### Task 9: Fix `_seed_memories()` to use normal insert() path

**Files:**
- Modify: `core/agent.py:458-475`
- Test: `core/tests/test_phase3.py` (modify existing test)

- [ ] **Step 1: Update the existing test**

In `core/tests/test_phase3.py`, find `test_seed_memories` in `TestAgentLifecycle`. Update it to verify embeddings are generated:

```python
@pytest.mark.asyncio
async def test_seed_memories(self):
    """Seed memories should be inserted via normal path so they get embeddings."""
    from core.memory.long_term import LongTermMemory
    from core.agent import Agent
    from core.app_config import AppConfig

    cfg = AppConfig()
    cfg.tts.enabled = False
    cfg.stt.enabled = False
    agent = Agent(config=cfg)
    await agent._long_term.connect()
    await agent._seed_memories()
    # All 5 entries should have been inserted (not just appended)
    assert agent._long_term.count == 5
    for entry in agent._long_term._entries:
        assert entry.id, "Every entry should have an id"
        assert entry.memory_type != "note" or entry.metadata, \
            "Entries should have metadata from seed data"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest core/tests/test_phase3.py::TestAgentLifecycle::test_seed_memories -v --asyncio-mode=auto`
Expected: FAIL — entries have empty id (bypassed insert())

- [ ] **Step 3: Fix `_seed_memories()`**

In `core/agent.py`, change `_seed_memories()` to use `insert()`:

```python
    async def _seed_memories(self) -> None:
        """Seed long-term memory with demo entries via normal insert() path."""
        entries = [
            ("上周和妖梦约定周六去爬香山", {"memory_type": "plan", "poignancy": 7}),
            ("妖梦最喜欢的颜色是蓝色", {"memory_type": "preference", "poignancy": 8}),
            ("昨天妖梦说他最近在学习 Rust 语言", {"memory_type": "fact", "poignancy": 6}),
            ("Elysia 这个名字来源于希腊神话的极乐净土", {"memory_type": "knowledge", "poignancy": 5}),
            ("上次推荐的电影《星际穿越》妖梦还没看完", {"memory_type": "interaction", "poignancy": 4}),
        ]
        for text, meta in entries:
            await self._long_term.insert(text, **meta)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest core/tests/test_phase3.py::TestAgentLifecycle::test_seed_memories -v --asyncio-mode=auto`
Expected: PASS

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `python -m pytest core/tests/ -v --asyncio-mode=auto`
Expected: All pass (seed memory content changes may affect tests that check specific strings)

- [ ] **Step 6: Commit**

```bash
git add core/agent.py core/tests/test_phase3.py
git commit -m "fix: seed memories via insert() instead of direct _entries append"
```

---

### Task 10: Add flush() to MemoryConsolidator, call from agent.stop()

**Files:**
- Modify: `core/consolidator.py:42-75` (add flush method)
- Modify: `core/agent.py:170-248` (call flush in stop)
- Test: `core/tests/test_phase2.py` (add to TestMemoryConsolidator)

- [ ] **Step 1: Write the test**

Add to `core/tests/test_phase2.py` in `TestMemoryConsolidator`:

```python
@pytest.mark.asyncio
async def test_flush_consolidates_below_threshold(self):
    """flush() should consolidate buffered turns even below threshold."""
    from core.consolidator import MemoryConsolidator
    from core.memory.long_term import LongTermMemory
    from core.llm_adapter import LLMAdapter
    # Use a FakeLLM that returns a simple extraction
    class FakeLLM:
        async def think(self, messages, extra_body=None):
            class R:
                text = '[{"type": "Fact", "content": "测试flush", "poignancy": 5}]'
            return R()
    mem = LongTermMemory()
    # buffer_threshold=10 so 3 messages won't trigger normal consolidation
    c = MemoryConsolidator(FakeLLM(), mem, buffer_threshold=10)
    await c.consolidate("user msg 1", "assistant reply 1")
    await c.consolidate("user msg 2", "assistant reply 2")
    await c.consolidate("user msg 3", "assistant reply 3")
    # Buffer should have 3 items, not yet consolidated (threshold=10)
    assert mem.count == 0
    # flush should force consolidation
    items = await c.flush()
    assert len(items) > 0
    assert mem.count > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest core/tests/test_phase2.py::TestMemoryConsolidator::test_flush_consolidates_below_threshold -v --asyncio-mode=auto`
Expected: FAIL — `flush` method doesn't exist

- [ ] **Step 3: Add flush() to MemoryConsolidator**

In `core/consolidator.py`, add after `consolidate()`:

```python
    async def flush(self) -> list[dict]:
        """Force consolidation of buffered turns regardless of threshold.

        Called on shutdown to avoid losing unconsolidated messages.
        Returns extracted items (may be empty if buffer was empty or LLM fails).
        """
        if not self._buffer:
            return []

        if self._consolidate_lock.locked():
            logger.debug("Flush skipped — consolidation already in progress")
            return []

        async with self._consolidate_lock:
            return await self._do_consolidate()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest core/tests/test_phase2.py::TestMemoryConsolidator::test_flush_consolidates_below_threshold -v --asyncio-mode=auto`
Expected: PASS

- [ ] **Step 5: Wire flush into agent.stop()**

In `core/agent.py`, in the `stop()` method, add after `self._pending_messages.clear()` (line 176):

```python
        # Flush consolidator buffer before shutdown
        if self._consolidator:
            try:
                items = await asyncio.wait_for(self._consolidator.flush(), timeout=5.0)
                if items:
                    logger.info("Flushed %d consolidator items on shutdown", len(items))
            except asyncio.TimeoutError:
                logger.warning("Consolidator flush timed out during shutdown")
            except Exception:
                logger.debug("Consolidator flush failed", exc_info=True)
```

- [ ] **Step 6: Run all memory tests**

Run: `python -m pytest core/tests/test_phase2.py -v --asyncio-mode=auto`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add core/consolidator.py core/agent.py core/tests/test_phase2.py
git commit -m "fix: add flush() to MemoryConsolidator, call on agent shutdown"
```

---

### Task 11: Return memory IDs in search_memory tool output

**Files:**
- Modify: `core/tools/memory.py:16-20`
- Test: `core/tests/test_phase2.py` (modify existing test_search_memory_tool)

- [ ] **Step 1: Update the existing test**

In `core/tests/test_phase2.py`, find `test_search_memory_tool`. Update the assertion to check for IDs:

```python
@pytest.mark.asyncio
async def test_search_memory_tool(self):
    """search_memory tool should return results with entry IDs."""
    from core.tools.memory import search_memory
    from core.memory.long_term import LongTermMemory

    mem = LongTermMemory()
    await mem.insert("妖梦喜欢蓝色", memory_type="preference", poignancy=8)
    await mem.insert("明天去爬山", memory_type="plan", poignancy=7)

    ctx = type("ctx", (), {"get": lambda self, k: mem})()
    result = await search_memory("蓝色", limit=3, ctx=ctx)
    assert "妖梦喜欢蓝色" in result
    # Should include the entry's ID
    entry = mem._entries[0]
    assert entry.id in result, f"Result should contain entry ID {entry.id}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest core/tests/test_phase2.py::TestMemoryIntegration::test_search_memory_tool -v --asyncio-mode=auto`
Expected: FAIL — result doesn't contain entry ID

- [ ] **Step 3: Update search_memory tool**

In `core/tools/memory.py`, update the format line:

```python
    results = await memory.search(query_text=query, top_k=limit)
    if not results:
        return "（记忆库中没有找到相关内容）"
    lines = []
    for r in results:
        meta_str = f" (id={r.id}, type={r.memory_type}, poignancy={r.poignancy})"
        lines.append(f"- {r.text}{meta_str}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest core/tests/test_phase2.py::TestMemoryIntegration::test_search_memory_tool -v --asyncio-mode=auto`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/tools/memory.py core/tests/test_phase2.py
git commit -m "feat: include entry id, type, and poignancy in search_memory output"
```

---

### Task 12: Expand MemoryConfig with long-term memory parameters

**Files:**
- Modify: `core/app_config.py:20-22`
- Modify: `core/agent.py:51-52` (pass config to LongTermMemory)
- Modify: `core/memory/long_term.py:37-55` (accept config in __init__)
- Test: `core/tests/test_phase2.py` (add config test)

- [ ] **Step 1: Write the test**

Add to `core/tests/test_phase2.py` as standalone:

```python
def test_memory_config_defaults():
    """MemoryConfig should include long-term memory parameters with sensible defaults."""
    from core.app_config import MemoryConfig
    cfg = MemoryConfig()
    assert cfg.session_max_messages == 20
    assert cfg.ltm_max_entries == 10000
    assert cfg.ltm_max_age_days == 90
    assert cfg.ltm_embed_timeout == 10.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest core/tests/test_phase2.py::test_memory_config_defaults -v`
Expected: FAIL — `ltm_max_entries` not an attribute

- [ ] **Step 3: Expand MemoryConfig**

In `core/app_config.py`:

```python
@dataclass
class MemoryConfig:
    session_max_messages: int = ConfigField(default=20)
    ltm_max_entries: int = ConfigField(default=10000)
    ltm_max_age_days: int = ConfigField(default=90)
    ltm_embed_timeout: float = ConfigField(default=10.0)
```

- [ ] **Step 4: Wire config into LongTermMemory**

In `core/memory/long_term.py`, update `__init__` and class constants:

```python
    def __init__(self, max_entries: int = 10000, max_age_days: int = 90,
                 embed_timeout: float = 10.0):
        self._entries: list[MemoryEntry] = []
        self._vector_db: "VectorDBBackend | None" = None
        self._embedding: "EmbeddingBackend | None" = None
        self._embedding_tried = False
        self._embed_task: asyncio.Task | None = None
        self._connected = False
        self._dirty_ids: set[str] = set()
        self._max_entries = max_entries
        self._max_age_days = max_age_days
        self._embed_timeout = embed_timeout
```

Then replace the class-level constants `_MAX_ENTRIES`, `_MAX_AGE_DAYS`, `EMBED_TIMEOUT` with references to the instance variables:
- `self._MAX_AGE_DAYS` → `self._max_age_days`
- `self._MAX_ENTRIES` → `self._max_entries`
- `self.EMBED_TIMEOUT` → `self._embed_timeout`

In `core/agent.py`, update line 52:
```python
        self._long_term = LongTermMemory(
            max_entries=config.memory.ltm_max_entries,
            max_age_days=config.memory.ltm_max_age_days,
            embed_timeout=config.memory.ltm_embed_timeout,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest core/tests/test_phase2.py::test_memory_config_defaults -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest core/tests/ -v --asyncio-mode=auto`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add core/app_config.py core/memory/long_term.py core/agent.py core/tests/test_phase2.py
git commit -m "feat: expand MemoryConfig with long-term memory parameters"
```

---

### Task 13: Add deprecation notice to legacy memory architecture doc

**Files:**
- Modify: `docs/ref_memory-architecture.md:1-5` (add notice at top)

- [ ] **Step 1: Add deprecation banner**

Insert at the very top of `docs/ref_memory-architecture.md`, before the title:

```markdown
> **DEPRECATED (2026-06-01):** This document describes a legacy three-layer architecture
> (L0 SessionState / L1 MicroMemory / L2 MacroMemory) that no longer matches the
> current implementation. The active memory system uses a two-layer design:
> `SessionMemory` (short-term, `core/memory/session.py`) +
> `LongTermMemory` (vector search + embedding, `core/memory/long_term.py`) +
> `MemoryConsolidator` (LLM extraction, `core/consolidator.py`).
> See [architecture.md](architecture.md) for the current design.

```

- [ ] **Step 2: Verify the file reads correctly**

Run: `head -10 docs/ref_memory-architecture.md`
Expected: Deprecation banner is shown before original content

- [ ] **Step 3: Commit**

```bash
git add docs/ref_memory-architecture.md
git commit -m "docs: add deprecation notice to legacy ref_memory-architecture.md"
```

---

## Deferred to Separate Plans

These low-priority items from the review are feature additions rather than bugfixes. They require broader design discussion and are deferred:

| Deferred Item | Reason |
|---------------|--------|
| Update/delete API for memories | Requires API design + tool changes + LLM prompt updates |
| Poignancy decay over time | Requires background decay task or lazy evaluation change |
| Conflict detection | Requires embedding similarity check + LLM resolution flow |
| Memory export/import | Requires serialization format design + CLI/API |
| Memory statistics dashboard | Requires stats aggregation + DebugInfo integration |

---

## Verification

After all tasks are complete, run the full test suite:

```bash
python -m pytest core/tests/ -v --asyncio-mode=auto
```

Expected: All 86+ tests pass, including the newly added tests.

Then run a quick integration smoke test to verify memory works end-to-end:

```bash
python -c "
import asyncio
from core.memory.long_term import LongTermMemory
from core.memory.session import SessionMemory

async def smoke():
    # Session
    s = SessionMemory(max_messages=5)
    s.add_message('user', 'hello')
    assert len(s.recent_messages()) == 1

    # Long-term
    m = LongTermMemory()
    await m.insert('测试记忆', memory_type='fact', poignancy=7)
    assert m.count == 1
    results = await m.search(query_text='测试')
    assert len(results) >= 1
    assert results[0].id  # has ID
    assert results[0].memory_type == 'fact'

    # Filter
    filtered = await m.search(query_text='测试', memory_type='preference')
    assert len(filtered) == 0  # no preference entries

    # Dedup
    await m.insert('测试记忆', memory_type='fact')
    assert m.count == 1  # still 1

    print('Smoke test PASSED')

asyncio.run(smoke())
"
```
