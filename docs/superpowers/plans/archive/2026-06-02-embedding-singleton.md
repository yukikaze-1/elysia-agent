# Embedding 全局单例 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 通过 EmbeddingFactory 类级缓存实现 embedding 模型全局共享，防止多个 LongTermMemory 实例各自加载 ~3GB 模型导致 OOM。

**Architecture:** EmbeddingFactory.create() 首次调用创建后端并缓存在 `_instances` 字典中，后续相同 model_id 命中缓存直接返回。模型释放统一由 Factory.shutdown() 负责。LongTermMemory.close() 仅丢弃引用。

**Tech Stack:** Python 3.13, asyncio, torch, sentence-transformers

---

### Task 1: EmbeddingFactory 缓存 + shutdown()

**Files:**
- Modify: `Module/embedding/registry.py`

- [ ] **Step 1: 添加类级缓存 _instances 和 shutdown() 类方法**

在 `EmbeddingFactory` 类内部，`create()` 方法前添加：

```python
_instances: dict[str, "EmbeddingBackend"] = {}
```

- [ ] **Step 2: 修改 create() — 先查缓存再创建**

将 `create()` 方法从 `@staticmethod` 改为 `@classmethod`，并在方法体开头添加缓存检查：

```python
@classmethod
def create(cls, model_id: str, **kwargs) -> "EmbeddingBackend":
    if model_id in cls._instances:
        return cls._instances[model_id]

    if "/" not in model_id:
        raise ValueError(
            f"Invalid model_id '{model_id}': expected 'provider/model-name' format"
        )
    provider, model = model_id.split("/", 1)

    if provider not in _BACKENDS:
        raise ValueError(
            f"Unknown provider '{provider}'. Known: {', '.join(sorted(_BACKENDS))}"
        )

    cfg = _BACKENDS[provider]

    env_key = cfg["env_key"]
    api_key = kwargs.pop("api_key", "") or os.getenv(env_key, "")
    base_url = kwargs.pop("base_url", "") or cfg["base_url"]

    if provider == "huggingface":
        if "logger" not in kwargs:
            kwargs["logger"] = get_caller_logger()
        instance = HuggingFaceBackend(model_name=model, **kwargs)
    elif provider == "openai":
        if "logger" not in kwargs:
            kwargs["logger"] = get_caller_logger()
        instance = OpenAICompatEmbeddingBackend(model=model, api_key=api_key, base_url=base_url, **kwargs)
    else:
        raise NotImplementedError(f"{provider} backend is not yet implemented")

    cls._instances[model_id] = instance
    return instance
```

- [ ] **Step 3: 添加 shutdown() 类方法**

在 `from_config()` 方法之后添加：

```python
@classmethod
async def shutdown(cls) -> None:
    """关闭所有缓存的 embedding 实例，释放显存/内存。幂等。"""
    for name, instance in list(cls._instances.items()):
        try:
            await instance.close()
        except Exception:
            pass
    cls._instances.clear()
```

- [ ] **Step 4: 更新 from_config() 调用**

`from_config()` 是 `@classmethod`，内部调用 `cls.create()`，保持不变即可（create 现在是 classmethod）。

- [ ] **Step 5: 运行已有 embedding 测试，确认无回归**

```bash
python -m pytest Module/embedding/test_embedding.py -v --timeout=60
```

- [ ] **Step 6: Commit**

```bash
git add Module/embedding/registry.py
git commit -m "feat: add instance caching and shutdown() to EmbeddingFactory"
```

---

### Task 2: HuggingFaceBackend.close() 真正释放模型

**Files:**
- Modify: `Module/embedding/huggingface.py`

- [ ] **Step 1: 修改 close() — del 模型 + cuda cache 清理**

将第 120-121 行的 `close()` 方法替换为：

```python
async def close(self) -> None:
    """释放模型，清理显存。"""
    if hasattr(self, "_embeddings") and self._embeddings is not None:
        del self._embeddings
        self._embeddings = None
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
```

- [ ] **Step 2: 运行 embedding 测试确认无回归**

```bash
python -m pytest Module/embedding/test_embedding.py -v --timeout=60
```

- [ ] **Step 3: Commit**

```bash
git add Module/embedding/huggingface.py
git commit -m "fix: HuggingFaceBackend.close() now releases model and clears CUDA cache"
```

---

### Task 3: LongTermMemory.close() 仅解引用

**Files:**
- Modify: `core/memory/long_term.py`

- [ ] **Step 1: 修改 close() — 丢弃引用，不调用 close()**

将第 399-400 行：

```python
        if self._embedding:
            await self._embedding.close()
```

替换为：

```python
        # 仅解引用，模型释放由 EmbeddingFactory.shutdown() 统一负责
        if self._embedding:
            self._embedding = None
```

- [ ] **Step 2: Commit**

```bash
git add core/memory/long_term.py
git commit -m "fix: LongTermMemory.close() only dereferences shared embedding"
```

---

### Task 4: Agent.stop() 中调用 Factory.shutdown()

**Files:**
- Modify: `core/agent.py`

- [ ] **Step 1: 添加 EmbeddingFactory 导入**

在文件顶部导入区域（第 29 行附近），`from .consolidator import MemoryConsolidator` 之后添加：

```python
from Module.embedding import EmbeddingFactory
```

- [ ] **Step 2: 在 stop() 中添加 Factory.shutdown() 调用**

在第 265 行（`Long-term memory close timed out` 的 warning 之后）和第 267 行（`Closing LLM client...` 的 logger 之前）之间插入：

```python
        logger.debug("Shutting down embedding factory...")
        try:
            await asyncio.wait_for(EmbeddingFactory.shutdown(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Embedding factory shutdown timed out")
```

- [ ] **Step 3: Commit**

```bash
git add core/agent.py
git commit -m "feat: call EmbeddingFactory.shutdown() in Agent.stop() to release cached models"
```

---

### Task 5: test_phase2.py session 级清理 fixture

**Files:**
- Modify: `core/tests/test_phase2.py`

- [ ] **Step 1: 添加 EmbeddingFactory 导入和 session fixture**

在文件顶部 `import pytest` 之后添加：

```python
from Module.embedding import EmbeddingFactory


@pytest.fixture(scope="session", autouse=True)
def _cleanup_embedding():
    """所有测试完成后释放共享的 embedding 模型。"""
    yield
    import asyncio
    asyncio.get_event_loop().run_until_complete(EmbeddingFactory.shutdown())
```

- [ ] **Step 2: 运行 test_phase2 确认不 OOM**

```bash
python -m pytest core/tests/test_phase2.py -v --timeout=120 -x
```

- [ ] **Step 3: 运行完整 core 测试确认无回归**

```bash
python -m pytest core/tests/ -v --timeout=120 --ignore=core/tests/test_phase1.py
```

- [ ] **Step 4: Commit**

```bash
git add core/tests/test_phase2.py
git commit -m "test: add session fixture to clean up shared embedding after all tests"
```
