# LongTermMemory 嵌入管理提取为 EmbeddingProvider

> 状态: 待实现 | 日期: 2026-07-28

## 动机

`core/memory/long_term.py` 中嵌入加载逻辑（70 行，lines 131-201）是一个清晰可分离的关注点：懒加载、超时控制、OpenAI/HuggingFace fallback、线程池执行。它有自己的状态（`_embedding`、`_embedding_tried`、`_embed_task`、`_embed_timeout`），对外只暴露 `_ensure_embedding(wait)` 一个接口，其余方法仅通过 `self._embedding.embed_query(text)` 消费。

提取为 `EmbeddingProvider` 类，不改变任何行为，只搬家 + 添加封装方法 `embed(text)` 简化调用方。

## 设计

### 新文件: `core/memory/embedding_provider.py`

```python
class EmbeddingProvider:
    """嵌入后端懒加载 + 超时 + 多 provider fallback。

    线程池执行模型加载，避免阻塞事件循环。
    """

    def __init__(self, embed_timeout: float = 10.0):
        self._backend: EmbeddingBackend | None = None
        self._tried = False
        self._task: asyncio.Task | None = None
        self._timeout = embed_timeout

    async def ensure(self, wait: bool = False) -> "EmbeddingBackend | None":
        """确保嵌入后端已加载。返回 backend 或 None。"""
        # 搬家自 _ensure_embedding，逻辑不变

    async def embed(self, text: str) -> list[float]:
        """封装 embed_query，加载失败/嵌入失败统一返回空列表。"""
        await self.ensure(wait=True)
        if self._backend is None:
            return []
        try:
            return await self._backend.embed_query(text)
        except Exception:
            logger.debug("Embedding failed", exc_info=True)
            return []

    def cancel(self) -> None:
        """取消后台加载任务。"""
        # 搬家自 close() 中的 embed_task cancel 逻辑

    def _try_load_sync(self):
        """线程池中执行，尝试 OpenAI → HuggingFace fallback 加载。"""
        # 搬家自 _try_load_embedding_sync，逻辑不变

```

### LongTermMemory 变更

**删除：**
- `__init__` 中 `self._embedding`、`self._embedding_tried`、`self._embed_task`、`self._embed_timeout` 4 个属性
- `_ensure_embedding()` 方法（lines 131-172）
- `_try_load_embedding_sync()` 方法（lines 174-201）
- `close()` 中的 embed_task cancel 逻辑（lines 582-583）
- `DIMENSION` 类常量（未使用，可顺带删除）

**新增：**
- `__init__` 中 `self._embedding_provider = EmbeddingProvider(embed_timeout)`
- 嵌入调用统一改为 `await self._embedding_provider.embed(text)`

**简化：**
- `insert()`: `await self._embedding.embed_query(text)` → `await self._embedding_provider.embed(text)`
- `search()`: 保持 `wait=False` 非阻塞语义。改为 `backend = await self._embedding_provider.ensure(wait=False)`，然后用 `backend.embed_query(...)` 替代原来的 `self._embedding.embed_query(...)`
- `update_text()`: 同上 `insert()`，改用 `await self._embedding_provider.embed(new_text)`
- `close()`: `self._embed_task.cancel()` → `self._embedding_provider.cancel()`

### 不改动

- `MemoryEntry` 数据类 — 保留在 `long_term.py`
- 所有 Milvus 操作（insert/delete/search/query）
- 驱逐、吸收、脏同步、rerank 逻辑
- 公开 API 签名全部不变

## 修改范围

| 文件 | 改动 |
|------|------|
| `core/memory/embedding_provider.py` | **新建** — `EmbeddingProvider` 类，~90 行 |
| `core/memory/long_term.py` | 删 ~75 行嵌入逻辑 + 调用方改用 `self._embedding_provider` |

不改动：`__init__.py`、测试、memory_reflector、consolidator 等所有外部调用方。

## 验证

```bash
# long_term 单元测试
pytest core/memory/tests/test_long_term.py -v --tb=short

# phase2 集成测试（含 eviction + embedding 场景）
pytest core/tests/test_phase2.py -v -k "memory" --tb=short

# 全量 memory 测试
pytest core/memory/tests/ -v --tb=short
```
