# LTM 驱逐修复 + 吸收条目 Milvus 清理

> 状态: 待实现 | 日期: 2026-07-28

## 动机

两个关联问题：

1. **`_evict_old_entries()` 被永久注释** — `insert()` 中第 259 行注释掉了驱逐调用，`max_entries` 形同虚设。内存和 Milvus 都无限增长。
2. **`mark_absorbed()` 不清理 Milvus** — MemoryReflector merge 吸收的条目在 Milvus 中成为僵尸向量，占空间、拖慢 ANNS 搜索。

同时去掉年龄驱逐逻辑（用户确认不需要），只保留数量上限驱逐。

## 设计

### 1. `_evict_old_entries()` → `_evict_overflow()`

- 删除年龄驱逐逻辑（`cutoff` 计算 + 过期条目遍历）
- 只保留数量驱逐：超过 `max_entries` → 按时间戳倒序，保留最新 N 条
- 收集被驱逐条目的 `_milvus_pk`，调 `await self._repo.delete(pks)` 清理 Milvus
- Milvus 删除加守卫：`if self._repo is not None and pks:` — 与 `delete()`（line 405）、`update_text()`（line 362）模式一致，兼容无 Milvus 环境
- 方法改为 `async`
- `max_age_days` 参数保留在构造函数和 config 中，但不参与驱逐逻辑

### 2. `insert()` 恢复驱逐

```python
self._entries.append(entry)
if self._enable_eviction:
    await self._evict_overflow()
```

新增实例变量 `_enable_eviction: bool`，由构造函数注入。

### 3. `mark_absorbed()` 加 Milvus 清理

```python
async def mark_absorbed(self, entry_id: str) -> None:
    self._absorbed_ids.add(entry_id)
    async with self._write_lock:
        entry = self._get_by_id(entry_id)
        if entry is not None and entry._milvus_pk is not None and self._repo is not None:
            await self._repo.delete([entry._milvus_pk])
```

改为 async。`memory_reflector.py:265` 调用处加 `await`。

`_write_lock` 的原因：与 `delete()`、`update_text()` 保持一致——`_get_by_id` 读取 `self._entries` 和 `_repo.delete` 都在锁内，与并发写操作互斥。

### 4. 配置开关

`core/config/app_config.py`:

```python
ltm_enable_eviction: bool = ConfigField(default=True)
```

`config.yaml` 的 `memory:` 段加注释说明。

### 5. 清理 `_sync_dirty_entries()`

该方法只在测试中调用，生产代码无任何引用。本次不改动它——留作后续清理。

### `core/memory/long_term.py`

- `__init__`: 新增 `enable_eviction: bool = True` 参数 → `self._enable_eviction`
- `_load_from_milvus:120`: `self._evict_old_entries()` → `await self._evict_overflow()`
- `_evict_old_entries()` 重命名为 `_evict_overflow()`：删年龄逻辑，加 `await self._repo.delete(pks)`，改 async
- `insert():259`: 取消注释，加开关门控 `if self._enable_eviction: await self._evict_overflow()`
- `mark_absorbed()`: 改 async，加 Milvus 删除（查 `_milvus_pk` → `await self._repo.delete([pk])`）

### 其他文件

| 文件 | 改动 |
|------|------|
| `core/memory/memory_reflector.py:265` | `self._memory.mark_absorbed(...)` → `await self._memory.mark_absorbed(...)` |
| `core/config/app_config.py` | 新增 `ltm_enable_eviction: bool = ConfigField(default=True)` |
| `config.yaml` | `memory:` 段加 `ltm_enable_eviction` 及注释 |
| `core/agent/agent.py:124-129` | `LongTermMemory(...)` 构造加 `enable_eviction=config.memory.ltm_enable_eviction` |
| `core/tests/test_phase2.py` | 两处测试需重写：<br>**`test_long_term_memory_evicts_old_entries`（line 986）** — 当前构造 100 天前的旧数据触发年龄驱逐。年龄逻辑删除后，改为构造 `max_entries + N` 条数据触发 overflow 驱逐，验证最旧的 N 条被移除、最新的 `max_entries` 条保留。<br>**`test_long_term_memory_eviction_cleans_dirty_ids`（line 1058）** — 同样依赖年龄驱逐，改为 overflow 触发后验证被驱逐条目的 ID 从 `_dirty_ids` 中移除。 |
| `core/memory/tests/test_memory_reflector.py` | `mark_absorbed = MagicMock()` → `AsyncMock()`（方法改为 async） |

## 兼容性

- `_evict_old_entries` 改名 → 测试中直接调用需同步改名
- `mark_absorbed` 改为 async → `memory_reflector.py` 加 await，其他调用方（如有）需同步
- `max_age_days` 参数保留不动，不影响现有调用
- 默认 `enable_eviction=True`，开发阶段可在 `config.yaml` 中设为 `false` 避免丢失测试记忆

## 验证

```bash
# eviction 相关测试
pytest core/tests/test_phase2.py -v -k "evict"

# long_term 单元测试
pytest core/memory/tests/test_long_term.py -v

# memory reflector 测试
pytest core/memory/tests/ -v
```
