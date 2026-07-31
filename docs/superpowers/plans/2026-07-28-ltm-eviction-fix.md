# LTM 驱逐修复 + 吸收条目 Milvus 清理 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 恢复 `insert()` 中的数量驱逐调用，给驱逐和 `mark_absorbed()` 加上 Milvus 同步清理，去掉年龄驱逐逻辑。

**架构：** 核心改动在 `core/memory/long_term.py`——`_evict_old_entries()` 重命名为 `_evict_overflow()`，去掉年龄驱逐，加 Milvus 清理，改为 async；`mark_absorbed()` 改为 async 并加 Milvus 删除。配置开关 `memory.ltm_enable_eviction` 控制驱逐是否启用。

**技术栈：** Python 3.11+, pytest, asyncio, Milvus (pymilvus)

## 全局约束

- `_evict_overflow()` 只做数量驱逐（`max_entries`），不做年龄驱逐（`max_age_days` 参数保留但不用）
- 驱逐 Milvus 删除必须有 `if self._repo is not None and pks:` 守卫
- `mark_absorbed()` 的 `_get_by_id` 和 `_repo.delete` 都在 `async with self._write_lock:` 内
- `insert()` 中的驱逐调用由 `self._enable_eviction` 门控，默认 `True`
- `memory_reflector.py:265` 的 `mark_absorbed` 调用必须加 `await`
- `test_phase2.py` 中依赖年龄驱逐的测试需重写为 overflow 触发

---

### Task 1: 配置开关 + `_evict_overflow()` 重写 + `insert()` 恢复

**文件：**
- 修改: `core/config/app_config.py:28`（插入新字段）
- 修改: `core/memory/long_term.py:53-66`（`__init__` 签名 + 新属性）
- 修改: `core/memory/long_term.py:120`（`_load_from_milvus` 调用）
- 修改: `core/memory/long_term.py:258-259`（`insert` 恢复驱逐）
- 修改: `core/memory/long_term.py:261-297`（`_evict_old_entries` → `_evict_overflow`）
- 修改: `core/agent/agent.py:124-129`（传 `enable_eviction`）
- 修改: `config.yaml:17-18`（加注释）

**接口：**
- 产出: `LongTermMemory(enable_eviction: bool = True)` → `self._enable_eviction`
- 产出: `async _evict_overflow() -> int`（替代 `_evict_old_entries() -> int`）
- 消耗: `MemoryConfig.ltm_enable_eviction`（Task 1 自身创建）

- [ ] **Step 1: 在 `MemoryConfig` 中新增 `ltm_enable_eviction`**

在 `core/config/app_config.py` 的 `MemoryConfig` 类中，`ltm_embed_timeout` 之前插入：

```python
    ltm_enable_eviction: bool = ConfigField(default=True)
```

- [ ] **Step 2: 在 `LongTermMemory.__init__` 中新增 `enable_eviction` 参数**

`core/memory/long_term.py` line 53，在 `embed_timeout` 参数之后添加：

```python
    def __init__(self, max_entries: int = 10000, max_age_days: int = 90,
                 embed_timeout: float = 10.0,
                 enable_eviction: bool = True,
                 repo: "MemoryRepository | None" = None):
```

在 line 65（`self._absorbed_ids` 之后）添加：

```python
        self._enable_eviction = enable_eviction
```

- [ ] **Step 3: 在 `agent.py` 中传 `enable_eviction`**

`core/agent/agent.py` line 124-129，在 `LongTermMemory(` 构造中加：

```python
        self._long_term = LongTermMemory(
            max_entries=config.memory.ltm_max_entries,
            max_age_days=config.memory.ltm_max_age_days,
            embed_timeout=config.memory.ltm_embed_timeout,
            enable_eviction=config.memory.ltm_enable_eviction,
            repo=self._memory_repo,
        )
```

- [ ] **Step 4: 更新 `config.yaml`**

在 `memory:` 段加注释：

```yaml
  # ltm_enable_eviction: true            # 是否启用数量驱逐（开发阶段可设为 false 避免丢失测试记忆）
```

- [ ] **Step 5: 重写 `_evict_old_entries()` 为 `_evict_overflow()`**

将 `core/memory/long_term.py` line 261-297 的整个方法替换为：

```python
    async def _evict_overflow(self) -> int:
        """数量上限驱逐：超过 max_entries 时保留最新的 N 条。返回驱逐数量。

        在每次 insert() 后调用，保持内存列表 + Milvus 健康。
        """
        removed = 0
        if len(self._entries) <= self._max_entries:
            return removed

        # 按时间戳倒序，保留最新的 max_entries 条
        self._entries.sort(key=lambda e: e.timestamp, reverse=True)
        overflow = len(self._entries) - self._max_entries
        evicted = self._entries[-overflow:]
        evicted_ids = {e.id for e in evicted}
        evicted_pks = [e._milvus_pk for e in evicted if e._milvus_pk is not None]
        self._entries = self._entries[:self._max_entries]
        removed = overflow

        # 同步清理 Milvus
        if self._repo is not None and evicted_pks:
            try:
                await self._repo.delete(evicted_pks)
            except Exception:
                logger.debug("Milvus delete failed for evicted entries", exc_info=True)

        # 同步清理 dirty_ids
        if evicted_ids:
            self._dirty_ids -= evicted_ids

        if removed:
            logger.info("Long-term memory: 驱逐 %d 条超限条目 (剩余 %d)",
                       removed, len(self._entries))
        return removed
```

**关键变更：**
- 方法签名从 `def` 改为 `async def`，方法名从 `_evict_old_entries` 改为 `_evict_overflow`
- 删除年龄驱逐（`cutoff` + `_max_age_days`）全部逻辑
- 提前返回条件：`len(self._entries) <= self._max_entries` 时跳过（原方法遍历全量即使不超限也遍历）
- 新增 `evicted_pks` 收集 + `await self._repo.delete(evicted_pks)`，有 `self._repo is not None` 守卫
- Milvus delete 包在 try/except 内（与 `delete()` line 405-410 的模式一致）

- [ ] **Step 6: 更新 `_load_from_milvus()` 中的调用**

`core/memory/long_term.py` line 117-120，将：

```python
        # 按时间排序后驱逐超出上限的旧条目
        if loaded:
            self._entries.sort(key=lambda e: e.timestamp)
            self._evict_old_entries()
```

改为：

```python
        # 启动时驱逐超出上限的条目
        if loaded:
            await self._evict_overflow()
```

`line 119` 的 `self._entries.sort(...)` 删除——`_evict_overflow()` 内部自己做 `sort(reverse=True)`，外层升序 sort 已冗余。

- [ ] **Step 7: 在 `insert()` 中恢复驱逐调用**

`core/memory/long_term.py` line 258-259，将：

```python
            self._entries.append(entry)
            # self._evict_old_entries()  # TODO: 开发阶段暂不禁用驱逐，避免丢失测试记忆
```

改为：

```python
            self._entries.append(entry)
            if self._enable_eviction:
                await self._evict_overflow()
```

- [ ] **Step 8: 验证**

```bash
python -c "
from core.memory.long_term import LongTermMemory
import inspect
# 验证方法签名
sig = inspect.signature(LongTermMemory.__init__)
assert 'enable_eviction' in sig.parameters
# 验证 _evict_overflow 是 async
assert inspect.iscoroutinefunction(LongTermMemory._evict_overflow)
print('OK: signatures correct')
"
```

- [ ] **Step 9: 提交**

```bash
git add core/config/app_config.py core/memory/long_term.py core/agent/agent.py config.yaml
git commit -m "feat: re-enable LTM count-based eviction with Milvus cleanup"
```

---

### Task 2: `mark_absorbed()` 改 async + Milvus 清理

**文件：**
- 修改: `core/memory/long_term.py:310-312`（`mark_absorbed`）
- 修改: `core/memory/memory_reflector.py:265`（调用处加 `await`）

**接口：**
- 消耗: `_get_by_id(entry_id: str) -> MemoryEntry | None`（已有，Task 1 不改）
- 消耗: `self._write_lock`（已有 asyncio.Lock）
- 产出: `async mark_absorbed(entry_id: str) -> None`

- [ ] **Step 1: 重写 `mark_absorbed()`**

将 `core/memory/long_term.py` line 310-312 的：

```python
    def mark_absorbed(self, entry_id: str) -> None:
        """标记条目已被 merge 吸收（不删除，只标记）。"""
        self._absorbed_ids.add(entry_id)
```

替换为：

```python
    async def mark_absorbed(self, entry_id: str) -> None:
        """标记条目已被 merge 吸收，同步清理 Milvus。"""
        self._absorbed_ids.add(entry_id)
        async with self._write_lock:
            entry = self._get_by_id(entry_id)
            if entry is not None and entry._milvus_pk is not None and self._repo is not None:
                try:
                    await self._repo.delete([entry._milvus_pk])
                except Exception:
                    logger.debug("Milvus delete failed for absorbed entry %s",
                               entry_id, exc_info=True)
```

- [ ] **Step 2: 更新 `memory_reflector.py` 调用处**

`core/memory/memory_reflector.py` line 265，将：

```python
                    self._memory.mark_absorbed(absorbed_id)
```

改为：

```python
                    await self._memory.mark_absorbed(absorbed_id)
```

- [ ] **Step 3: 验证**

```bash
python -c "
from core.memory.long_term import LongTermMemory
import inspect
assert inspect.iscoroutinefunction(LongTermMemory.mark_absorbed)
print('OK: mark_absorbed is async')
"
```

- [ ] **Step 4: 提交**

```bash
git add core/memory/long_term.py core/memory/memory_reflector.py
git commit -m "feat: add Milvus cleanup to mark_absorbed()"
```

---

### Task 3: 测试更新

**文件：**
- 修改: `core/tests/test_phase2.py:986-1080`（重写年龄驱逐测试）
- 修改: `core/memory/tests/test_memory_reflector.py:100-205`（`mark_absorbed` mock 改 `AsyncMock`）

**接口：**
- 消耗: `LongTermMemory._evict_overflow()`（Task 1，async）
- 消耗: `LongTermMemory.mark_absorbed()`（Task 2，async）

- [ ] **Step 1: 重写年龄驱逐测试为 overflow 驱逐测试**

`core/tests/test_phase2.py` line 986-1003 的 `test_long_term_memory_evicts_old_entries` 替换为：

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_long_term_memory_overflow_eviction():
    """验证超过上限的条目被驱逐（保留最新的）。"""
    from core.memory.long_term import LongTermMemory, MemoryEntry

    ltm = LongTermMemory()
    now = _time_module.time()
    ltm._max_entries = 5

    # 构造 8 条数据，只有 5 条能保留
    ltm._entries = [
        MemoryEntry(text=f"entry-{i}", metadata={}, timestamp=now - 100 + i)
        for i in range(8)
    ]

    removed = await ltm._evict_overflow()
    assert removed == 3  # 8 - 5 = 3
    assert len(ltm._entries) == 5
    # 保留的应该是最新的 5 条 (i=3,4,5,6,7)
    for e in ltm._entries:
        idx = int(e.text.split("-")[1])
        assert idx >= 3
```

- [ ] **Step 2: 更新已有的 `test_long_term_memory_evicts_overflow`**

`core/tests/test_phase2.py` line 1006-1024 的已有 overflow 测试只改方法调用：

- `removed = ltm._evict_old_entries()` → `removed = await ltm._evict_overflow()`
- 函数签名加 `async`，装饰器加 `@pytest.mark.asyncio`（line 1006-1007）

- [ ] **Step 3: 重写 `test_long_term_memory_eviction_cleans_dirty_ids`**

`core/tests/test_phase2.py` line 1058-1080，将年龄驱逐改为 overflow 触发：

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_long_term_memory_eviction_cleans_dirty_ids():
    """Evicted entries should also be removed from _dirty_ids."""
    from core.memory.long_term import LongTermMemory, MemoryEntry
    import time as _time
    mem = LongTermMemory()
    mem._max_entries = 3

    now = _time.time()
    # 构造 5 条数据，max_entries=3 → 驱逐 2 条
    for i in range(5):
        entry = MemoryEntry(
            id=f"test_evict_{i}",
            text=f"entry {i}",
            embedding=[0.1] * 10,
            memory_type="fact",
            poignancy=5,
            timestamp=now - 100 + i,  # older entries have smaller i
        )
        mem._entries.append(entry)
        mem._dirty_ids.add(f"test_evict_{i}")

    removed = await mem._evict_overflow()
    assert removed == 2
    # 最旧的 2 条被驱逐：test_evict_0, test_evict_1
    assert "test_evict_0" not in mem._dirty_ids
    assert "test_evict_1" not in mem._dirty_ids
    # 保留的 3 条仍在 dirty_ids
    assert "test_evict_2" in mem._dirty_ids
```

- [ ] **Step 4: 更新 `test_memory_reflector.py` 中 `mark_absorbed` 为 `AsyncMock`**

`core/memory/tests/test_memory_reflector.py`:

- Line 104: `m.mark_absorbed = MagicMock()` → `m.mark_absorbed = AsyncMock()`
- Line 149: `mem.mark_absorbed.side_effect = RuntimeError("boom")` — `AsyncMock` 支持 `side_effect`，不变
- Line 205: `mem.mark_absorbed = MagicMock()` → `mem.mark_absorbed = AsyncMock()`

- [ ] **Step 5: 运行所有相关测试**

```bash
# eviction 测试
pytest core/tests/test_phase2.py -v -k "evict" --tb=short

# memory reflector 测试
pytest core/memory/tests/test_memory_reflector.py -v --tb=short

# long_term 单元测试
pytest core/memory/tests/test_long_term.py -v --tb=short
```

- [ ] **Step 6: 提交**

```bash
git add core/tests/test_phase2.py core/memory/tests/test_memory_reflector.py
git commit -m "test: update eviction tests for overflow-based eviction"
```
