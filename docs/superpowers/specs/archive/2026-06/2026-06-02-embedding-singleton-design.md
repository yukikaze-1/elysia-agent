# Embedding 全局单例 — 设计文档

日期: 2026-06-02

## 问题

`LongTermMemory` 每个实例独立调用 `EmbeddingFactory.create()` 加载 embedding 模型。`HuggingFaceBackend` 在构造时加载完整 BGE-large-zh-v1.5 模型（~3GB），且 `close()` 为 no-op。测试套件 `test_phase2.py` 有 34+ 个测试方法，每个创建独立 `LongTermMemory()` → 全部加载各自的 3GB 模型 → OOM。

## 方案

**EmbeddingFactory 内部缓存 — 同一 `provider/model` 只创建一次，复用返回。**

## 改动清单

### 1. `Module/embedding/registry.py` — Factory 缓存 + shutdown

- `EmbeddingFactory._instances: dict[str, EmbeddingBackend] = {}` — 类级缓存
- `create()` — 命中缓存直接返回，未命中创建并缓存
- `shutdown()` — 关闭所有缓存实例，清空缓存（幂等）

### 2. `Module/embedding/huggingface.py` — close() 真正释放模型

- `close()` — `del self._model` + `torch.cuda.empty_cache()`

### 3. `core/memory/long_term.py` — close() 仅解引用

- `close()` — `self._embedding = None`（不调用 `self._embedding.close()`）
- 模型释放统一由 `Factory.shutdown()` 负责
- `_try_load_embedding_sync()` — 不变，仍通过 `Factory.create()` 获取

### 4. `core/agent.py` — stop() 中调用 Factory.shutdown()

- `stop()` — 在 `long_term.close()` 之后插入 `await EmbeddingFactory.shutdown()`
- `start()` — 不变，`_ensure_embedding()` preload 自动命中缓存

### 5. `core/tests/test_phase2.py` — session 级清理 fixture

- 新增 session-scoped autouse fixture，全部测试结束后调用 `Factory.shutdown()`

## 生命周期

```
Agent.start()
  └─ long_term._ensure_embedding()  →  Factory.create()  →  首次创建并缓存
Agent 运行中
  └─ 任何 LongTermMemory._ensure_embedding()  →  Factory.create()  →  命中缓存 ✓
Agent.stop()
  ├─ long_term.close()  →  丢弃 _embedding 引用
  └─ Factory.shutdown()  →  真正释放模型（del + cuda empty_cache）

测试:
  test_1: Factory.create()  →  创建并缓存
  test_2: Factory.create()  →  命中缓存
  ...
  test_34: Factory.create()  →  命中缓存
  fixture teardown: Factory.shutdown()  →  释放
```

## 影响范围

| 文件 | 改动行数 | 风险 |
|------|---------|------|
| `Module/embedding/registry.py` | ~15 行 | 低 — 纯新增，不改变 create() 外部行为 |
| `Module/embedding/huggingface.py` | ~8 行 | 低 — close() 原先不被正确使用 |
| `core/memory/long_term.py` | ~2 行 | 极低 — close() 语义从「释放」变为「解引用」 |
| `core/agent.py` | ~5 行 | 极低 — 在 stop() 中加一步清理 |
| `core/tests/test_phase2.py` | ~8 行 | 极低 — 新增 fixture |

所有改动向后兼容，对外接口不变。
