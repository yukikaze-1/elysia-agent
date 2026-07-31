# LongTermMemory 嵌入提取 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 将 `long_term.py` 中 72 行嵌入加载逻辑提取为 `EmbeddingProvider` 类。

**架构：** `EmbeddingProvider` 封装懒加载、超时、OpenAI/HuggingFace fallback、线程池执行。对外暴露 `ensure(wait)` 和 `embed(text)` 两个方法。`LongTermMemory` 通过 `self._embedding_provider` 消费。

**技术栈：** Python 3.11+, asyncio

## 全局约束

- 方法体逻辑不变——只搬家 + 封装
- `embed()` 内部加 `logger.debug("Embedding failed", exc_info=True)`
- `search()` 用 `ensure(wait=False)` 返回值，不访问私有 `._backend`
- `insert()` 和 `update_text()` 用 `embed(text)` 封装
- 测试不修改（公开 API 不变）
- `DIMENSION` 常量顺带删除
- **实施顺序：** 本计划的行号假设 `enable_eviction` 参数已在 `__init__` 中（eviction fix plan 先执行）。如果行号对不上，以实际代码为准——方法名和逻辑描述是权威的。
- **`update_text()` 中 `_dirty_ids` 行为等价：** `embed()` 内部吞异常返回 `[]`，导致 `if emb:` 为 False 跳过 Milvus 更新，不再显式调 `self._dirty_ids.add()`。但无 embedding 时本来也无法同步 Milvus，行为等价。

---

### Task 1: 创建 `EmbeddingProvider`

**文件：**
- 新建: `core/memory/embedding_provider.py`

**接口：**
- 产出: `class EmbeddingProvider` — `ensure()`, `embed()`, `cancel()`, `_try_load_sync()`

- [ ] **Step 1: 创建文件**

```python
"""嵌入后端懒加载 provider — 超时 + 多 provider fallback + 线程池执行。"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Module.embedding import EmbeddingBackend

logger = logging.getLogger(__name__)


class EmbeddingProvider:
    """嵌入后端懒加载 + 超时 + 多 provider fallback。

    线程池执行模型加载，避免阻塞事件循环。
    """

    def __init__(self, embed_timeout: float = 10.0):
        self._backend: EmbeddingBackend | None = None
        self._tried = False
        self._task: asyncio.Task | None = None
        self._timeout = embed_timeout
```

- [ ] **Step 2: 移动 `ensure()` 方法（搬家自 `_ensure_embedding`）**

从 `long_term.py:131-172` 复制 `_ensure_embedding` 方法体，方法名改为 `ensure`，`self._embedding` → `self._backend`，`self._embedding_tried` → `self._tried`，`self._embed_task` → `self._task`，`self._embed_timeout` → `self._timeout`。返回 `self._backend`。

关键代码（逻辑完全不变，只改名）：

```python
    async def ensure(self, wait: bool = False) -> EmbeddingBackend | None:
        """确保嵌入后端已加载。返回 backend 或 None。"""
        if self._backend is not None:
            return self._backend
        if self._tried:
            if wait and self._task and not self._task.done():
                try:
                    await asyncio.wait_for(self._task, timeout=self._timeout)
                except (asyncio.TimeoutError, Exception):
                    pass
            return self._backend
        self._tried = True

        loop = asyncio.get_event_loop()

        async def _load():
            try:
                embedding = await asyncio.wait_for(
                    loop.run_in_executor(None, self._try_load_sync),
                    timeout=self._timeout,
                )
                if embedding is not None:
                    self._backend = embedding
            except asyncio.TimeoutError:
                logger.info("Embedding: load timed out after %.0fs, text-only mode", self._timeout)
            except Exception as e:
                logger.info("Embedding: unavailable (%s), text-only mode", e)

        self._task = asyncio.create_task(_load(), name="embedding-load")

        if wait:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=self._timeout)
            except (asyncio.TimeoutError, Exception):
                pass
        return self._backend
```

- [ ] **Step 3: 添加 `embed()` 封装方法**

```python
    async def embed(self, text: str) -> list[float]:
        """封装 embed_query：阻塞加载 + 嵌入。失败返回空列表。"""
        await self.ensure(wait=True)
        if self._backend is None:
            return []
        try:
            return await self._backend.embed_query(text)
        except Exception:
            logger.debug("Embedding failed", exc_info=True)
            return []
```

- [ ] **Step 4: 移动 `_try_load_sync()` 方法（搬家自 `_try_load_embedding_sync`）**

从 `long_term.py:174-201` 完整复制，方法名改为 `_try_load_sync`。逻辑完全不变。

- [ ] **Step 5: 添加 `cancel()` 方法**

```python
    def cancel(self) -> None:
        """取消后台加载任务。"""
        if self._task and not self._task.done():
            self._task.cancel()
```

- [ ] **Step 6: 验证 + 提交**

```bash
python -c "from core.memory.embedding_provider import EmbeddingProvider; print('OK')"
git add core/memory/embedding_provider.py
git commit -m "feat: add EmbeddingProvider, extracted from LongTermMemory"
```

---

### Task 2: 集成 `EmbeddingProvider` 到 `LongTermMemory`

**文件：**
- 修改: `core/memory/long_term.py`

- [ ] **Step 1: 替换 `__init__` 中的嵌入属性**

删除 4 个属性（line 59-61, 65）：

```python
        # 删除:
        self._embedding: "EmbeddingBackend | None" = None
        self._embedding_tried = False
        self._embed_task: asyncio.Task | None = None
        self._embed_timeout = embed_timeout
```

替换为：

```python
        self._embedding_provider = EmbeddingProvider(embed_timeout)
```

添加 import：`from .embedding_provider import EmbeddingProvider`

- [ ] **Step 2: 删除 `_ensure_embedding()` 方法（lines 131-172）**
- [ ] **Step 3: 删除 `_try_load_embedding_sync()` 方法（lines 174-201）**
- [ ] **Step 4: 删除 `DIMENSION` 常量（line 51）**

- [ ] **Step 5: 更新 `insert()` 中的嵌入调用（lines 212, 218）**

`embed()` 必须在 `_write_lock` **之前**调用——首次加载模型可能耗时 10s+，持锁会阻塞所有并发写操作。和原代码 `_ensure_embedding` 在锁外的模式一致。

```python
# 旧 (line 212-220):
        await self._ensure_embedding(wait=True)

        async with self._write_lock:
            embedding: list[float] = []
            if self._embedding:
                try:
                    embedding = await self._embedding.embed_query(text)
                except Exception:
                    logger.debug("Embedding failed for insert, storing text-only", exc_info=True)

# 新:
        embedding = await self._embedding_provider.embed(text)

        async with self._write_lock:
            # embedding 已在上方获取，锁内不再调 embed
            ...
```

后续 Milvus insert 逻辑不变（`if self._repo is not None and embedding:` 等价——空列表视为无嵌入）。

- [ ] **Step 6: 更新 `search()` 中的嵌入调用（lines 460, 473）**

```python
# 旧:
        await self._ensure_embedding()
        ...
            if vec is None and query_text and self._embedding:

# 新:
        backend = await self._embedding_provider.ensure(wait=False)
        ...
            if vec is None and query_text and backend is not None:
```

第二处：

```python
# 旧:
                    vec = await self._embedding.embed_query(query_text)

# 新:
                    vec = await backend.embed_query(query_text)
```

- [ ] **Step 7: 更新 `update_text()` 中的嵌入调用（lines 370, 373）**

```python
# 旧:
                    await self._ensure_embedding(wait=True)
                    if self._embedding:
                        ...
                            emb = await self._embedding.embed_query(new_text)

# 新:
                    emb = await self._embedding_provider.embed(new_text)
                    if emb:
                        ...  # 后续逻辑不变
```

- [ ] **Step 8: 更新 `close()` 中的 cancel 逻辑（lines 582-583, 587-588）**

```python
# 旧:
        if self._embed_task and not self._embed_task.done():
            self._embed_task.cancel()
        ...
        if self._embedding:
            self._embedding = None

# 新:
        self._embedding_provider.cancel()
        # _backend 引用由 EmbeddingProvider 管理，close() 不再直接置 None
```

- [ ] **Step 9: 验证 + 提交**

```bash
source /home/yomu/data/anaconda3/etc/profile.d/conda.sh && conda activate Elysia
python -c "from core.memory.long_term import LongTermMemory; print('OK')"
pytest core/memory/tests/test_long_term.py -v --tb=short
pytest core/tests/test_phase2.py -v -k "memory" --tb=short
```

```bash
git add core/memory/long_term.py
git commit -m "refactor: integrate EmbeddingProvider into LongTermMemory"
```
