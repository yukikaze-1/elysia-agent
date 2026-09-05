# 阶段 F：记忆 v2 隔离（MemoryScope + ScopedLongTermMemory）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Plan status: clean (post-3-round-review rewrite).
>
> **Spec：** [`docs/superpowers/specs/2026-09-05-character-stage-f-memory-v2-isolation-design.md`](../specs/2026-09-05-character-stage-f-memory-v2-isolation-design.md)

**Goal:** 让阶段 E 预埋的 `TurnCharacterContext.memoryScope`（= instanceId）真正生效：记忆读写从「当前 session 全局读」改为「按 `character_instance_id` + `user_id` scope 隔离」。consolidator/reflector 同步 per-runtime 化，buffer/计数器按 instance 隔离。

**Architecture:** 方案 A——`MemoryScope`（`core/memory/scope.py`）+ `ScopedLongTermMemory`（`core/memory/scoped_long_term.py`）绑定视图 + `long_term_memory_v2` collection（`core/storage/memory_repo_v2.py`）；`LongTermMemory.for_scope(scope)` 返回 scoped view；底层 `search/insert/update/delete` 移除无 scope overload；`MemoryEntry` 带 scope 字段；`CharacterRuntime` 持 memory_view/consolidator/reflector（`_make_runtime` 构造）；`_finalize_turn` spawn `_after_response` 时急切捕获本回合 consolidator/reflector（anti-race）。

**Tech Stack:** Python 3.12 + Pydantic + Milvus(MemoryRepository) + asyncio。零新依赖。

## Global Constraints

- **Elysia byte-for-byte**：`test_elysia_snapshot_unchanged`（`core/prompt/tests/test_system_prompt_snapshot.py:79`，fixture `system_prompt_snapshot_elysia_cf4bc008.txt` 18127B）必须 green。默认会话（default → elysia instance）渲染字节不变。
- **Module 边界**：`Module/system_prompt` 不 import `core.memory`——C4 `MemorySection` 接收 `long_term` 保持 duck-typed `Any`（已如此，不引入 `core.memory.ScopedLongTermMemory` import）。
- **禁相对导入**（`from core.xxx` / `from Module.xxx`，ruff TID252）；生产代码参数类型注解（ANN001/002/003，测试豁免）；注释中文。
- **conda env**：`conda run -n Elysia python -m pytest ...`。
- **事实基线**：基于阶段 E 完成 HEAD。旧 `long_term_memory` 视为孤儿（阶段 E 已清空），本批不迁移。
- **无 scope 默认值**：任意记忆写/读必须显式带 `MemoryScope`；不存在「漏传即全局」默认值。底层 `LongTermMemory.search/insert/update_text/delete` 无 scope overload。
- **anti-race**：`_after_response` 用急切捕获的 consolidator/reflector（首个 await 前固化），不 per-turn 重绑 `self._consolidator`。

## Task 结构（按正确依赖序）

| Task | 交付 | 依赖 |
|------|------|------|
| 1 | `MemoryScope` + `MemoryVisibility`（`core/memory/scope.py`） | — |
| 2 | `MemoryRepositoryV2`（`core/storage/memory_repo_v2.py`，指向 `long_term_memory_v2`） | — |
| 3 | `MemoryEntry` scope 字段 + `LongTermMemory.for_scope()` + strict scope 参数 | Task 1,2 |
| 4 | `ScopedLongTermMemory` 绑定视图（`core/memory/scoped_long_term.py`） | Task 3 |
| 5 | `MemoryConsolidator`/`MemoryReflector` scope 感知（类型放宽 `ScopedLongTermMemory | LongTermMemory`） | Task 4 |
| 6 | `CharacterRuntime` 持 memory_view/consolidator/reflector | Task 4,5 |
| 7 | agent `_make_runtime` 构造 scoped 三组件 + `_lifecycle_tools.py`/loop 接线 | Task 6 |
| 8 | C4 + memory 工具 + debug 迁移到 scoped view | Task 4,7 |
| 9 | 现有无 scope 调用点 sweep + inflight 修正 | Task 3,4,5,8 |
| 10 | README 文档 + 全套验证 | 全部 |

## 文件影响

**新增：**
- `core/memory/scope.py` — MemoryScope + MemoryVisibility
- `core/memory/scoped_long_term.py` — ScopedLongTermMemory
- `core/storage/memory_repo_v2.py` — MemoryRepositoryV2

**改造：**
- `core/memory/long_term.py` — for_scope() + strict scope + MemoryEntry scope 字段
- `core/memory/consolidator.py` — 类型放宽 + 无全局 buffer
- `core/memory/memory_reflector.py` — 类型放宽 + 无全库聚类
- `core/character/runtime.py` — 持 memory_view/consolidator/reflector
- `core/agent/agent.py` — _make_runtime 构造 scoped 三组件
- `core/agent/_lifecycle_tools.py` — svc["memory"] 移除裸单例注入
- `core/cognitive/loop.py` — _finalize_turn spawn _after_response 急切捕获
- `Module/system_prompt/sections/memory.py` — C4 接收 scoped view
- `core/tools/memory/plugin.py` — search/save 经 scoped view
- `core/debug/info.py` — consolidator/reflector 面板改经 registry
- `docs/...` — README 更新

---

### Task 1: MemoryScope + MemoryVisibility

**Files:**
- Create: `core/memory/scope.py`
- Test: `core/memory/tests/test_scope.py`

**Interfaces:**
- Produces: `MemoryScope`（frozen dataclass：user_id/character_instance_id/visibility）、`MemoryVisibility`（StrEnum：CHARACTER_PRIVATE/USER_SHARED）

**Design:** `MemoryScope` 放 `core/memory/`（领域模型，非 core/storage）。frozen dataclass 保证不可变——视图创建时绑定 scope，调用点无法改动。

- [ ] **Step 1: 写失败测试**

```python
# core/memory/tests/test_scope.py
import pytest
from core.memory.scope import MemoryScope, MemoryVisibility


def test_scope_immutable():
    scope = MemoryScope(
        user_id="usr_local_default", character_instance_id="chi_builtin_elysia",
    )
    with pytest.raises(TypeError):
        scope.user_id = "mutate"  # frozen dataclass 不可变
    assert scope.visibility == MemoryVisibility.CHARACTER_PRIVATE


def test_visibility_values():
    assert MemoryVisibility.CHARACTER_PRIVATE == "character_private"
    assert MemoryVisibility.USER_SHARED == "user_shared"
```

- [ ] **Step 2: 运行确认失败**

Run: `conda run -n Elysia python -m pytest core/memory/tests/test_scope.py -v`
Expected: FAIL（ModuleNotFoundError: core.memory.scope）

- [ ] **Step 3: 写最小实现**

```python
# core/memory/scope.py
"""MemoryScope — 记忆作用域（架构稿 §8.1）。

任意记忆写/读必须显式带 scope；不存在「漏传即全局」默认值。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MemoryVisibility(StrEnum):
    CHARACTER_PRIVATE = "character_private"  # 角色私有（产品记忆一律此值）
    USER_SHARED = "user_shared"              # 预留枚举，不提供自动升级路径


@dataclass(frozen=True)
class MemoryScope:
    user_id: str
    character_instance_id: str
    visibility: MemoryVisibility = MemoryVisibility.CHARACTER_PRIVATE
```

- [ ] **Step 4: 运行确认通过**

Run: `conda run -n Elysia python -m pytest core/memory/tests/test_scope.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/memory/scope.py core/memory/tests/test_scope.py
git commit -m "feat(stage-f): MemoryScope/MemoryVisibility 领域模型"
```

---

### Task 2: MemoryRepositoryV2

**Files:**
- Create: `core/storage/memory_repo_v2.py`
- Test: `core/storage/tests/test_memory_repo_v2.py`

**Interfaces:**
- Consumes: `Module/database/vectordb/protocol.py`（VectorDBBackend）
- Produces: `MemoryRepositoryV2`（`COLLECTION = "long_term_memory_v2"`，`insert/search/query/delete/count/clear_all`，带 scope filter 能力）

**Design:** 新 collection `long_term_memory_v2`（架构稿 §8.3）。复用 `MemoryRepository` 的 Milvus 操作但 `COLLECTION` 不同。`search` 支持 `filter_expr`（ANN 阶段 filter —— 传入 `user_id + character_instance_id + visibility`）。用 `FakeVectorDBBackend` 测试不碰真实 Milvus。

- [ ] **Step 1: 写失败测试**

```python
# core/storage/tests/test_memory_repo_v2.py
import pytest
from core.storage.memory_repo_v2 import MemoryRepositoryV2


def test_collection_name():
    assert MemoryRepositoryV2.COLLECTION == "long_term_memory_v2"


@pytest.mark.asyncio
async def test_insert_search_scope_filter():
    from unittest.mock import AsyncMock, MagicMock
    # 用 MagicMock stub（现有 test_memory_repo.py 同模式；core/storage/tests/ 无 fake vectordb）
    db = MagicMock()
    db.collection_exists = AsyncMock(return_value=True)
    db.insert = AsyncMock(return_value=[1])
    db.search = AsyncMock(return_value=[{
        "id": 1, "text": "hello", "user_id": "usr_a",
        "character_instance_id": "chi_a", "visibility": "character_private",
    }])
    db.query = AsyncMock(return_value=[])
    db.delete = AsyncMock(return_value=1)
    db.drop_collection = AsyncMock()
    db.create_collection = AsyncMock()
    db.load_collection = AsyncMock()
    repo = MemoryRepositoryV2(db=db)
    await repo.connect()
    await repo.insert(vectors=[[0.1] * 8], metadata=[{
        "entry_id": "m_1", "text": "hello",
        "user_id": "usr_a", "character_instance_id": "chi_a",
        "visibility": "character_private", "memory_type": "Note",
        "poignancy": 5, "timestamp": 0.0, "schema_version": 2,
    }])
    # 断言 repo.search 收到了 scope filter_expr
    res = await repo.search(
        vector=[0.1] * 8, top_k=10,
        filter_expr='user_id == "usr_a" && character_instance_id == "chi_a" && visibility == "character_private"',
        output_fields=["id", "text", "user_id", "character_instance_id", "visibility"],
    )
    assert len(res) >= 1
    await repo.close()
```

（注：测试须用 FakeVectorDBBackend——先确认 `core/storage/tests/` 是否存在 fake；若不存在则用 `unittest.mock` stub。Milvus filter 表达式语义在测试层以「传对 filter_expr 字符串」为准，不强制真实 Milvus 执行。）

- [ ] **Step 2: 运行确认失败**

Run: `conda run -n Elysia python -m pytest core/storage/tests/test_memory_repo_v2.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 写最小实现**

```python
# core/storage/memory_repo_v2.py
"""MemoryRepositoryV2 — 面向 long_term_memory_v2 的 Milvus 仓储（架构稿 §8.3）。"""
from __future__ import annotations

from core.storage.memory_repo import MemoryRepository


class MemoryRepositoryV2(MemoryRepository):
    """MemoryRepository 的 v2 变体：COLLECTION 指向 long_term_memory_v2。

    复用父类 Milvus 操作（insert/search/query/delete/count/clear_all），
    仅 COLLECTION 不同——产品记忆全部写 v2（scope 字段：user_id/character_instance_id/visibility）。
    """
    COLLECTION = "long_term_memory_v2"
```

- [ ] **Step 4: 运行确认通过**

Run: `conda run -n Elysia python -m pytest core/storage/tests/test_memory_repo_v2.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/storage/memory_repo_v2.py core/storage/tests/test_memory_repo_v2.py
git commit -m "feat(stage-f): MemoryRepositoryV2 指向 long_term_memory_v2"
```

---

### Task 3: MemoryEntry scope 字段 + LongTermMemory.for_scope() + strict scope

**Files:**
- Modify: `core/memory/long_term.py`
- Test: `core/memory/tests/test_long_term.py`

**Interfaces:**
- Consumes: `MemoryScope`（Task 1）
- Produces: `MemoryEntry` 加 `user_id`/`character_instance_id`/`visibility` 字段；`LongTermMemory.for_scope(scope)` 返回 `ScopedLongTermMemory`（Task 4 定义）；底层 `search/insert/update_text/delete` 加 scope 参数；`get_all()`/`count` 收窄为 admin。

**Design:** 本 Task 只改 `long_term.py` 的 scope 支架（`MemoryEntry` 字段 + `for_scope()` 方法 stub + strict scope 参数签名），`ScopedLongTermMemory` 由 Task 4 实现。关键：`LongTermMemory.search` 签名加 `scope: MemoryScope` 参数，`_search_milvus_candidates` 用 `scope` 生成 filter_expr 且对 `_entries` 先按 scope 过滤；`_search_fallback` 同样按 scope 过滤。

- [ ] **Step 1: 写失败测试（MemoryEntry scope 字段）**

```python
# 追加到 core/memory/tests/test_long_term.py
from core.memory.scope import MemoryScope


def test_memory_entry_scope_fields():
    from core.memory.long_term import MemoryEntry
    e = MemoryEntry(id="m_1", text="hi", user_id="usr_a",
                    character_instance_id="chi_a", visibility="character_private")
    assert e.user_id == "usr_a"
    assert e.character_instance_id == "chi_a"
    assert e.visibility == "character_private"
```

- [ ] **Step 2: 运行确认失败**

Run: `conda run -n Elysia python -m pytest core/memory/tests/test_long_term.py::test_memory_entry_scope_fields -v`
Expected: FAIL（MemoryEntry 无 user_id 属性）

- [ ] **Step 3: 实现 MemoryEntry scope 字段 + for_scope stub + strict scope 签名**

在 `MemoryEntry` 加字段：

```python
@dataclass
class MemoryEntry:
    id: MemoryId = MemoryId("")
    text: str = ""
    embedding: list[float] = field(default_factory=list, repr=False)
    memory_type: MemoryType = MemoryType.NOTE
    poignancy: int = 5
    timestamp: float = 0.0
    metadata: dict = field(default_factory=dict)
    # 阶段 F（§2.3）：scope 字段——ANN/Python 双路径过滤、fallback 需要
    user_id: str = ""
    character_instance_id: str = ""
    visibility: str = "character_private"
    _milvus_pk: int | None = field(default=None, repr=False, compare=False)
```

`LongTermMemory.stream_scope` 方法（stub——Task 4 完善 ScopedLongTermMemory）：

```python
from core.memory.scope import MemoryScope

class LongTermMemory:
    def for_scope(self, scope: MemoryScope) -> "ScopedLongTermMemory":
        """返回绑定该 scope 的 ScopedLongTermMemory 视图（架构稿 §8.2）。"""
        from core.memory.scoped_long_term import ScopedLongTermMemory
        return ScopedLongTermMemory(self, scope)
```

**`_load_from_milvus` 必须补 scope 字段读取**：`_load_from_milvus`（long_term.py:323-338）当前 output_fields 为 `["id", "entry_id", "text", "memory_type", "poignancy", "timestamp"]`，加载进 `MemoryEntry` 时未读 `user_id/character_instance_id/visibility`。若重启加载的 entries scope 字段为空（`""`），scope 过滤会全失败（`"" == "usr_a"` 恒 False）。修：output_fields 加 scope 三字段，构造 `MemoryEntry(...)` 时补 `user_id=row.get("user_id", "")` 等：

```python
# long_term.py _load_from_milvus
row = ...
entry = MemoryEntry(
    id=..., text=..., memory_type=..., poignancy=..., timestamp=...,
    user_id=row.get("user_id", ""),
    character_instance_id=row.get("character_instance_id", ""),
    visibility=row.get("visibility", "character_private"),
)
```

`for_scope` 中的 `from core.memory.scoped_long_term import ScopedLongTermMemory` 为 lazy import——避免 long_term ↔ scoped_long_term 循环导入（`scoped_long_term.py` import `long_term.MemoryEntry`）。

`search` 加 scope 参数（签名必须带 scope）：

```python
from core.memory.scope import MemoryScope

class LongTermMemory:
    async def search(
        self,
        query_text: str = "",
        query_embedding: list[float] | None = None,
        top_k: int = 5,
        memory_type: str | None = None,
        scope: MemoryScope | None = None,
    ) -> list[MemoryEntry]:
```

（注：stage F 严格约束「无 scope overload」——但 `scope: MemoryScope | None = None` 会允许漏传。为达「无 scope raise」，实现应在 `search` 内 `if scope is None: raise TypeError("scope 必填——不存在无 scope 的搜索")`。`_entries` 过滤：`if scope is not None: entries = [e for e in self._entries if e.character_instance_id == scope.character_instance_id and e.user_id == scope.user_id]`。）

- [ ] **Step 4: 运行确认通过**

Run: `conda run -n Elysia python -m pytest core/memory/tests/test_long_term.py -v`
Expected: PASS（既有测试用 MagicMock 或已更新——见 Task 9 sweep）

- [ ] **Step 5: Commit**

```bash
git add core/memory/long_term.py core/memory/tests/test_long_term.py
git commit -m "feat(stage-f): MemoryEntry scope 字段 + LongTermMemory.for_scope + strict scope"
```

---

### Task 4: ScopedLongTermMemory 绑定视图

**Files:**
- Create: `core/memory/scoped_long_term.py`
- Test: `core/memory/tests/test_scoped_long_term.py`

**Interfaces:**
- Consumes: `LongTermMemory`（Task 3，持有 `_entries`/`_repo`）、`MemoryScope`（Task 1）
- Produces: `ScopedLongTermMemory`（`_scope` + `_long_term`；`insert/search/update_text/delete/get_all` 全部作用域过滤）

**Design:** `ScopedLongTermMemory` 绑定 scope，调用点无法在方法调用时绕过。它委托持久化到 `_long_term`（共享同一 `_entries`/`_repo`），但所有读/写经 scope 过滤。

- [ ] **Step 1: 写失败测试**

```python
# core/memory/tests/test_scoped_long_term.py
import pytest
from core.memory.scope import MemoryScope
from core.memory.long_term import LongTermMemory
from core.memory.scoped_long_term import ScopedLongTermMemory


@pytest.mark.asyncio
async def test_for_scope_returns_scoped_view():
    ltm = LongTermMemory(enable_eviction=False)
    view_a = ltm.for_scope(MemoryScope(user_id="usr_a", character_instance_id="chi_a"))
    assert isinstance(view_a, ScopedLongTermMemory)
    assert view_a._scope.character_instance_id == "chi_a"


@pytest.mark.asyncio
async def test_insert_search_scope_isolated():
    ltm = LongTermMemory(enable_eviction=False)
    view_a = ltm.for_scope(MemoryScope(user_id="usr_a", character_instance_id="chi_a"))
    view_b = ltm.for_scope(MemoryScope(user_id="usr_b", character_instance_id="chi_b"))
    await view_a.insert("A 的记忆", type="fact")
    await view_b.insert("B 的记忆", type="fact")
    # search 须带 scope
    res_a = await view_a.search(query_text="记忆", top_k=5)
    assert all(e.character_instance_id == "chi_a" for e in res_a)
    res_b = await view_b.search(query_text="记忆", top_k=5)
    assert all(e.character_instance_id == "chi_b" for e in res_b)
    # 不同 scope 互不可见
    assert not any("B" in e.text for e in res_a)
    assert not any("A" in e.text for e in res_b)
```

- [ ] **Step 2: 运行确认失败**

Run: `conda run -n Elysia python -m pytest core/memory/tests/test_scoped_long_term.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 写最小实现**

```python
# core/memory/scoped_long_term.py
"""ScopedLongTermMemory — 绑定 MemoryScope 的记忆视图（架构稿 §8.2）。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from core.memory.scope import MemoryScope
from core.memory.long_term import MemoryEntry

if TYPE_CHECKING:
    from core.memory.long_term import LongTermMemory


def _in_scope(entry: MemoryEntry, scope: MemoryScope) -> bool:
    return (
        entry.user_id == scope.user_id
        and entry.character_instance_id == scope.character_instance_id
    )


class ScopedLongTermMemory:
    """绑定 scope 的只读/读接口——调用点无法在方法调用时绕过 scope。

    insert/search/update_text/delete/get_all 全部作用域过滤；底层共享同一
    LongTermMemory（_entries/_repo），但 scope 在视图创建时固定。
    """

    def __init__(self, long_term: "LongTermMemory", scope: MemoryScope):
        self._long_term = long_term
        self._scope = scope

    @property
    def scope(self) -> MemoryScope:
        return self._scope

    async def insert(self, text: str, **metadata: object) -> None:
        scope = self._scope
        await self._long_term.insert(
            text,
            user_id=scope.user_id,
            character_instance_id=scope.character_instance_id,
            visibility=scope.visibility.value,
            **metadata,
        )

    async def search(self, query_text: str = "", top_k: int = 5,
                     memory_type: str | None = None) -> list[MemoryEntry]:
        return await self._long_term.search(
            query_text=query_text, top_k=top_k, memory_type=memory_type,
            scope=self._scope,
        )

    def get_all(self) -> list[MemoryEntry]:
        return [e for e in self._long_term.get_all()
                if _in_scope(e, self._scope)]

    async def update_text(self, entry_id, new_text: str = "",
                          poignancy: int | None = None,
                          memory_type: str | None = None) -> bool:
        """更新记忆文本——**签名与 LongTermMemory.update_text 对齐**（含 poignancy，consolidator/reflector 调用须传 poignancy=...）。

        注意：若 scoped view 不转发 poignancy，consolidator _compare_and_resolve
        (consolidator.py:366) 传 `poignancy=action.poignancy` 会 TypeError/丢 poignancy。
        底层实现带 scope 验证：目标 entry 必须在本 scope 内否则视为 not found。
        """
        return await self._long_term.update_text(
            entry_id, new_text, poignancy=poignancy, memory_type=memory_type,
            scope=self._scope,
        )

    async def delete(self, entry_id) -> bool:
        return await self._long_term.delete(entry_id, scope=self._scope)
```

（注：`LongTermMemory.search/update_text/delete` 需接受 scope 参数并**验证 entry 在 scope 内**——见 Task 3/9 补 `_ensure_in_scope`。`get_all()` 在 `LongTermMemory` 收窄为 admin 后，`ScopedLongTermMemory` 是产品唯一读入口。）

- [ ] **Step 4: 运行确认通过**

Run: `conda run -n Elysia python -m pytest core/memory/tests/test_scoped_long_term.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/memory/scoped_long_term.py core/memory/tests/test_scoped_long_term.py
git commit -m "feat(stage-f): ScopedLongTermMemory 绑定视图"
```

---

### Task 5: consolidator/reflector scope 感知

**Files:**
- Modify: `core/memory/consolidator.py`
- Modify: `core/memory/memory_reflector.py`
- Test: `core/memory/tests/test_consolidator.py`、`core/memory/tests/test_memory_reflector.py`

**Interfaces:**
- Consumes: `ScopedLongTermMemory`（Task 4）
- Produces: `MemoryConsolidator`/`MemoryReflector` 接受 `ScopedLongTermMemory | LongTermMemory`（duck-typed），内部 `memory.search/insert/get_all` 全部经 scoped view；无全局 buffer/counter。

**Design:** consolidator/reflector 的 `memory` 参数类型从 `LongTermMemory` 放宽为 `ScopedLongTermMemory | LongTermMemory`（它们只用 `search/insert/get_all/update_text/delete`——ScopedLongTermMemory 提供这些）。type annotation 放宽为 union 或 Protocol，生产传 scoped view，测试仍可传裸 LongTermMemory。

- [ ] **Step 1: 写测试（类型放宽 + scope 过滤）**

```python
# core/memory/tests/test_consolidator.py 追加
import pytest
from core.memory.scope import MemoryScope
from core.memory.long_term import LongTermMemory
from core.memory.consolidator import MemoryConsolidator


class _FakeLLM:
    model = "mock"
    async def chat(self, messages, **kw):
        return "[]"  # 无提取

    async def test(self):
        return {"ok": True, "latency_ms": 0}

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_consolidator_accepts_scoped_view():
    ltm = LongTermMemory(enable_eviction=False)
    view = ltm.for_scope(MemoryScope(user_id="usr_a", character_instance_id="chi_a"))
    cons = MemoryConsolidator(_FakeLLM(), memory=view)  # 应接受 scoped view
    assert cons._memory is view
```

- [ ] **Step 2: 运行确认失败**

Run: `conda run -n Elysia python -m pytest core/memory/tests/test_consolidator.py -v`
Expected: 若类型强约束 `LongTermMemory`，test 构造 scoped view 可能因 type-check 失败。但 pytest 运行时不做 type check——实际失败体现为 `memory.insert(search/...)` 的参数不匹配。先确认现状。

- [ ] **Step 3: 放宽类型注解**

```python
# consolidator.py
from core.memory.scoped_long_term import ScopedLongTermMemory

class MemoryConsolidator:
    def __init__(self, llm: MemoryLLM, memory: ScopedLongTermMemory | LongTermMemory,
                 character_name: str = "Elysia", user_name: str = "用户",
                 buffer_threshold: int | None = None):
```

`memory_reflector.py` 同理放宽为 `ScopedLongTermMemory | LongTermMemory`。

- [ ] **Step 4: 运行确认通过 + 无全局 buffer 回归**

Run: `conda run -n Elysia python -m pytest core/memory/tests/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/memory/consolidator.py core/memory/memory_reflector.py core/memory/tests/
git commit -m "feat(stage-f): consolidator/reflector 接受 scoped view"
```

---

### Task 6: CharacterRuntime 持 memory_view/consolidator/reflector

**Files:**
- Modify: `core/character/runtime.py`
- Test: `core/character/tests/test_runtime.py`

**Interfaces:**
- Consumes: `ScopedLongTermMemory`、`MemoryConsolidator`、`MemoryReflector`（Task 4,5）
- Produces: `CharacterRuntime` 加 `memory_view`/`consolidator`/`reflector` 持有

**Design:** 阶段 CE 占位未落地。本 Task 在 `CharacterRuntime.__init__` 加三 field（默认 None），与 engine/persist_fn 同模式。

- [ ] **Step 1: 写失败测试**

```python
# core/character/tests/test_runtime.py 追加
import pytest
from core.character.runtime import CharacterRuntime
from core.character.instance import CharacterInstance
from core.character.definition import CharacterDefinition
from core.character.revision import CharacterRevision
from core.profile.user_identity import UserIdentity


def test_runtime_holds_memory_components():
    rt = CharacterRuntime(
        instance=CharacterInstance(instanceId="chi_a", characterId="chr_a"),
        definition=CharacterDefinition(characterId="chr_a"),
        revision=CharacterRevision(characterId="chr_a", revision=1, displayName="A"),
        user_identity=UserIdentity(userId="usr_a", displayName="you"),
    )
    assert rt.memory_view is None
    assert rt.consolidator is None
    assert rt.reflector is None
```

- [ ] **Step 2: 运行确认失败**

Run: `conda run -n Elysia python -m pytest core/character/tests/test_runtime.py -v`
Expected: FAIL（无 memory_view 属性）

- [ ] **Step 3: 实现**

```python
# runtime.py
from typing import TYPE_CHECKING, Callable, Awaitable
if TYPE_CHECKING:
    from core.memory.scoped_long_term import ScopedLongTermMemory
    from core.memory.consolidator import MemoryConsolidator
    from core.memory.memory_reflector import MemoryReflector

class CharacterRuntime:
    def __init__(self, instance, definition, revision, user_identity,
                 memory_view: "ScopedLongTermMemory | None" = None,
                 consolidator: "MemoryConsolidator | None" = None,
                 reflector: "MemoryReflector | None" = None):
        ...
        self.memory_view = memory_view
        self.consolidator = consolidator
        self.reflector = reflector
```

- [ ] **Step 4: 运行确认通过**

Run: `conda run -n Elysia python -m pytest core/character/tests/test_runtime.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/character/runtime.py core/character/tests/test_runtime.py
git commit -m "feat(stage-f): CharacterRuntime 持 memory_view/consolidator/reflector"
```

---

### Task 7: agent _make_runtime 构造 scoped 三组件 + 接线

**Files:**
- Modify: `core/agent/agent.py`
- Modify: `core/cognitive/loop.py`
- Modify: `core/agent/_lifecycle_tools.py`
- Modify: `core/agent/lifecycle.py`
- Test: `core/agent/tests/`、`core/cognitive/tests/`

**Interfaces:**
- Consumes: `MemoryScope`、`ScopedLongTermMemory`、`MemoryConsolidator`、`MemoryReflector`、`CharacterRuntime`（Task 1-6）
- Produces: `_make_runtime` 构造 scoped 三组件；`_prepare_turn` 注入 `svc["memory"] = runtime.memory_view`；`_finalize_turn` spawn `_after_response` 急切捕获；`_flush_consolidator` 迭代 registry runtimes；`_lifecycle_tools.py:68` 移除裸单例注入。

**Design:** 本 Task 是接线核心。关键在于：
1. `agent._memory_repo` 改指向 `MemoryRepositoryV2`（agent.py:147 `repo=self._memory_repo`）。
2. `_make_runtime`（agent.py:226-264）构造 memory_view + per-runtime consolidator/reflector。
3. `_lifecycle_tools.py:68` 移除 `"memory": self._long_term`。
4. `_prepare_turn`（loop.py:430-433）加 `svc["memory"] = runtime.memory_view`。
5. `_finalize_turn`（finalize.py spawn）急切捕获 runtime.consolidator/reflector 传入 `_after_response`。
6. `_flush_consolidator`（lifecycle.py:231）迭代 registry runtimes。
7. `_after_response`（loop.py:797）签名加 `consolidator`/`memory_reflector` 参数，None 回退 `self._consolidator`/`self._memory_reflector`。

- [ ] **Step 1: 改 agent._memory_repo 指向 v2**

agent.py:147：`self._memory_repo = MemoryRepository(...)` → `MemoryRepositoryV2(...)`（import `from core.storage.memory_repo_v2 import MemoryRepositoryV2`）。注意：`self._memory_repo` 仍被 `LongTermMemory(repo=self._memory_repo)`（agent.py:183）使用——指向 v2 repo 后，`LongTermMemory` 的 Milvus 操作自动走 `long_term_memory_v2` collection。

- [ ] **Step 2: _make_runtime 构造 scoped 三组件**

agent.py `_make_runtime` 内（在 `rt.set_persist_fn(...)` 前）：

```python
scope = MemoryScope(
    user_id=self._user_identity.userId,
    character_instance_id=instance_id,
)
memory_view = self._long_term.for_scope(scope)
rt = CharacterRuntime(
    instance=..., definition=..., revision=..., user_identity=...,
    memory_view=memory_view,
    consolidator=MemoryConsolidator(llm=self._llm, memory=memory_view,
                                    character_name=self._character.name,
                                    user_name=self._user_name),
    # MemoryReflector 的 session 原用于 save_state() 计数器持久化；per-runtime 后
    # 计数持久化改经 runtime.state（spec §2.6），构造不依赖 session——传 None 或
    # 移除 session 依赖。若保留 session 参数，传 self._session_manager.active 但
    # 需 guard（_session_manager.active 可能为 None → TypeError）。
    reflector=MemoryReflector(llm=self._llm, memory=memory_view,
                              session=None,  # 计数持久化改经 runtime.state
                              character_name=self._character.name,
                              user_name=self._user_name),
)
```

- [ ] **Step 3: _lifecycle_tools.py:68 移除裸单例注入**

删除 `"memory": self._long_term,` 行——改为 per-turn 在 `_prepare_turn` 注入 `svc["memory"] = runtime.memory_view`。

- [ ] **Step 4: _prepare_turn 注入 svc["memory"]**

loop.py:430-433，在 `svc["session"] = target` 附近加：

```python
svc["memory"] = runtime.memory_view
```

- [ ] **Step 5: _after_response 签名加 consolidator/memory_reflector + 急切捕获**

loop.py `_after_response`：

```python
async def _after_response(self, user_message, assistant_response, *,
                          session=None, engine=None,
                          consolidator=None, memory_reflector=None):
    ...
    target_consolidator = consolidator if consolidator is not None else self._consolidator
    target_memory_reflector = (memory_reflector if memory_reflector is not None
                               else self._memory_reflector)
    # 后续用 target_consolidator/target_memory_reflector，不再直接 self._consolidator
```

`_finalize_turn` spawn `_run_after_response` 急切捕获：

```python
runtime = self._current_runtime  # 急切读出（create_task 前）
...
async def _run_after_response() -> None:
    await self._after_response(
        user_message, public_text,
        session=target, engine=self._engine,
        consolidator=runtime.consolidator if runtime else None,
        memory_reflector=runtime.reflector if runtime else None,
    )
```

- [ ] **Step 6: _flush_consolidator 迭代 registry runtimes**

lifecycle.py:231：

```python
async def _flush_consolidator(self) -> None:
    runtimes = list(self._registry._runtimes.values())  # registry 无 all_loaded()，直接迭代 _runtimes
    for rt in runtimes:
        if rt.consolidator:
            try:
                items = await asyncio.wait_for(rt.consolidator.flush(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Consolidator flush timed out during shutdown")
            except Exception:
                logger.warning("Consolidator flush failed", exc_info=True)
```

（注：`CharacterRuntimeRegistry`（registry.py）无 `all_loaded()`，但有 `flush_all()`/`close_all()`。本批可在 registry 加 `def all_loaded(self) -> list[CharacterRuntime]: return list(self._runtimes.values())` 更干净，或直接迭代 `_runtimes.values()`。推荐加 `all_loaded()`——语义清晰，供 lifecycle/debug 复用。）

- [ ] **Step 6b: agent.consolidator/memory_reflector 属性 + bind_memory_reflector 迁移**

agent.py:1046/1051 `agent.consolidator`/`agent.memory_reflector` 属性当前返回 `self._consolidator`/`self._memory_reflector`（单例）。per-runtime 化后这两个单例不再构造。改为经 registry 解析（仅 debug 展示，默认 default runtime）或返回 None 并让 debug 层经 registry 取。同时 agent.py:309 `self._session_manager.bind_memory_reflector(self._memory_reflector)` 不再适用——reflector per-runtime 后，`session.save_state()` 的 reflector counter 持久化改经 runtime.state（spec §2.6），移除 `bind_memory_reflector` 调用与 session_manager 的 `set_memory_reflector`/`save_state` reflector 分支。

```python
# agent.py
@property
def consolidator(self):
    """当前实例的 memory consolidator（debug 展示用）。"""
    # 不调 registry.get(..., lambda: None)——get 会把 factory() 结果存入 dict，
    # lambda: None 会污染 _runtimes（后续 _evict_if_needed 访问 rt.dirty 崩溃）。
    rt = self._registry._runtimes.get(DEFAULT_INSTANCE_ID)  # noqa: SLF001
    return rt.consolidator if rt else None

@property
def memory_reflector(self):
    rt = self._registry._runtimes.get(DEFAULT_INSTANCE_ID)  # noqa: SLF001
    return rt.reflector if rt else None
```

- [ ] **Step 7: 运行测试确认**

Run: `conda run -n Elysia python -m pytest core/agent/tests/ core/cognitive/tests/ -v`
Expected: PASS。MemoryReflector 构造现不依赖 session（Step 2 传 None）——无需 `_session_manager.active` guard。若 `_make_runtime` 在 registry 创建 runtime 时被反复调用（LRU 驱逐后重建），每实例重 fixture consolidator/reflector（scoped view 不变，天然幂等）。

- [ ] **Step 8: Commit**

```bash
git add core/agent/ core/cognitive/loop.py core/agent/_lifecycle_tools.py core/agent/lifecycle.py core/memory/session_manager.py
git commit -m "feat(stage-f): agent 接线 scoped 三组件 + anti-race 急切捕获"
```

---

### Task 8: C4 + memory 工具 + debug 迁移

**Files:**
- Modify: `Module/system_prompt/sections/memory.py`
- Modify: `core/prompt/prompt_assembler.py`（加 set_memory_view）
- Modify: `core/tools/memory/plugin.py`
- Modify: `core/debug/info.py`
- Test: `Module/system_prompt/tests/unit/test_sections/test_boundary.py`、`core/tools/tests/test_memory.py`、`core/prompt/tests/`

**Interfaces:**
- Consumes: `ScopedLongTermMemory`（Task 4）、`runtime.memory_view`（Task 7）
- Produces: C4 接收 scoped view（duck-typed，不 import core）；memory 工具经 scoped view；debug 面板经 registry。

**Design:**
- **C4 `MemorySection` 经 `PromptAssembler` 注入**：`PromptAssembler.__init__`（prompt_assembler.py:78）一次性构造 `MemorySection(long_term, session)` 绑定裸单例；`set_session`（:118-127）只重绑 `_session` 不重绑 `_long_term`。因此 C4 要经 per-turn scoped view，必须让 `PromptAssembler` 在 `_prepare_turn` 时把 `runtime.memory_view` 传给 C4——本批加 `PromptAssembler.set_memory_view(view)`（或 `set_session` 内重绑 `_memory_section._long_term`），由 `_prepare_turn`（loop.py:427-429 `set_session`/`set_personality_engine`/`set_character` 附近）调用。
- **C4 代码本身不 import core**——接收 `long_term` 保持 duck-typed `Any`（已如此），`ScopedLongTermMemory.search(query_text, top_k)` 签名兼容，`MemorySection.render()` 的 `self._long_term.search(...)` 不变。
- **memory 工具**：`ctx.get("memory")` 已是 scoped view（Task 7 注入），`search`/`insert` 代码不变。
- **debug/info.py**：consolidator/reflector 面板改经 registry（或 default runtime）。

- [ ] **Step 1: PromptAssembler 加 set_memory_view**

```python
# core/prompt/prompt_assembler.py
def set_memory_view(self, view: Any) -> None:
    """_prepare_turn 注入本回合 scoped memory view（阶段 F §2.4 C4）。"""
    self._memory_section._long_term = view  # noqa: SLF001 — MemorySection 字段
```

`loop.py` `_prepare_turn`（在 `self._prompt.set_character(...)` 附近）加：

```python
self._prompt.set_memory_view(runtime.memory_view)
```

- [ ] **Step 2: 测试（C4 经 scoped view）**

```python
# Module/system_prompt/tests/unit/test_sections/test_boundary.py 追加
import pytest
from Module.system_prompt.sections.memory import MemorySection


def test_c4_receives_scoped_view():
    # stub（用 MagicMock，不 import core.memory——duck-typed）
    from unittest.mock import MagicMock
    view = MagicMock()
    view.search = AsyncMock(return_value=[])
    sec = MemorySection("templates/c4_memory.j2", long_term=view, session=None)
    assert sec._long_term is view  # 注入的 scoped view
```

（`_make_scoped_view` 用 stub，不 import core.memory。）

- [ ] **Step 3: 运行确认（module 边界）**

Run: `conda run -n Elysia python -m pytest Module/system_prompt/tests/ -v`
Expected: PASS（C4 代码无需 import core.memory——保持 duck-typed）

- [ ] **Step 4: 修改 debug/info.py**

改经 registry 解析：

```python
# debug/info.py:203/291/300
rt = agent._runtime_registry._runtimes.get(DEFAULT_INSTANCE_ID)  # noqa: SLF001 — 不调 get(..., lambda: None) 防污染 registry
cons = rt.consolidator if rt else None
if cons:
    info.consolidation_history = list(cons._history)[-5:]
```

- [ ] **Step 5: 运行确认通过**

Run: `conda run -n Elysia python -m pytest core/debug/tests/ core/input/tests/ -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add Module/system_prompt/sections/memory.py core/prompt/prompt_assembler.py core/tools/memory/plugin.py core/debug/info.py
git commit -m "feat(stage-f): C4/memory 工具/debug 迁移 scoped view"
```

---

### Task 9: 现有无 scope 调用点 sweep

**Files:**
- Modify: `core/memory/tests/test_long_term.py`、`test_consolidator.py`、`test_memory_reflector.py`、`core/tools/tests/test_memory.py`
- Modify: `core/agent/tests/test_public_interface.py`
- Test: 全套 memory + agent

**Interfaces:**
- Consumes: strict scope 签名（Task 3）、scoped view（Task 4,7）
- Produces: 无 scope `.search/.insert/.update_text/.get_all` 调用点全部迁移

**Design:** 底层 `search/insert/update_text/delete` 加 strict scope 后，所有旧调用点需补 scope 或改经 scoped view。此 Task sweep 全部——grep 确认生产代码无无 scope 调用。

- [ ] **Step 1: grep 无 scope 调用点**

Run: `grep -rn "\.search(\|\.insert(\|\.update_text(\|\.get_all(" core/memory core/tools core/cognitive --include="*.py" | grep -v "test_" | grep -v "def \|_search\|_entries"`
Expected: 列出所有需迁移的调用点（consolidator/reflector/tools/C4 已改——见 Task 5/8；剩余测试内调用点需补 scope）

- [ ] **Step 2: 迁移测试调用点**

- `test_long_term.py` 内 `ltm.search(query_embedding=..., top_k=5)` → 补 `scope=memory_scope_fixture` 或改经 `for_scope()` view。
- `test_consolidator.py`/`test_memory_reflector.py` 构造裸 `LongTermMemory()` → 保持，但 `memory.search/insert` 调用需补 scope。
- `test_public_interface.py:34-35`：`("consolidator", "_consolidator")`/`("memory_reflector", "_memory_reflector")` 属性映射若改经 registry，需更新断言。

- [ ] **Step 3: 运行全套确认**

Run: `conda run -n Elysia python -m pytest core/memory/tests/ core/tools/tests/ core/agent/tests/ -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add core/memory/tests/ core/tools/tests/ core/agent/tests/test_public_interface.py
git commit -m "fix(stage-f): sweep 无 scope 调用点到 scoped view"
```

---

### Task 10: README 文档 + 全套验证

**Files:**
- Modify: `core/memory/README.md`（若存在）、`core/character/README.md`、`README.md`、`docs/code-style-guide.md`（如涉 scope 约定）
- Test: 全套 gate

**Design:** 按 docs/readme-conventions.md 读 README 模板，更新 memory/character 描述（MemoryScope/ScopedLongTermMemory/long_term_memory_v2/per-runtime consolidator/reflector）。

- [ ] **Step 1: 更新 README**

在 `core/memory/README.md`（若存在）补 MemoryScope + ScopedLongTermMemory + long_term_memory_v2；`core/character/README.md` 补 runtime memory_view；根 `README.md` 补阶段 F 导航。

- [ ] **Step 2: 验证 gate**

Run:
```bash
conda run -n Elysia python -m pytest -m "unit or service" -q  # 或 test-quick
conda run -n Elysia python -m pytest core/prompt/tests/test_system_prompt_snapshot.py::test_elysia_snapshot_unchanged -v
make lint
make pyright
```

- [ ] **Step 3: 确认 Elysia snapshot byte 不变 + ruff/pyright clean**

Expected: snapshot 18127B 不变；ruff clean；pyright 0 errors；test-quick 通过。

- [ ] **Step 4: Commit**

```bash
git add docs/ core/memory/README.md README.md
git commit -m "docs(stage-f): README + 阶段 F 文档"
```

---

## 关键风险与验证

1. **Elysia snapshot byte 不变**：`test_elysia_snapshot_unchanged`（18127B）必须 green。C4 对 default elysia 单角色行为不变——scoped view 注入后返回空，posture 不依赖真实 view。若 scoped view 默认返回非空导致 byte 变化，回退为「单角色默认 scope 的 view 返回空」。
2. **consolidator/reflector per-runtime 化**：`MemoryReflector` 构造需 session——测试 Agent 可能无 `_session_manager.active`。guard。
3. **strict scope 破既有调用**：严格签名会破既有调用点（test_long_term 等）。Task 9 sweep 兜底——但 MagicMock 测试（snapshot 的 `mock_long_term`）不受影响（MagicMock 接受任意签名）。
4. **pyright**：`ScopedLongTermMemory | LongTermMemory` union 类型注解在 pyright basic 模式须无 error。若 pyright 报 union attribute 不兼容，改为 Protocol 或 `# type: ignore`。
