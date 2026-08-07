# MemoryEntry.memory_type 对齐 MemoryType StrEnum — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `core/memory/long_term.py` 的 `MemoryEntry.memory_type` 从裸 `str` 对齐为 `core/schema/memory.py` 的 `MemoryType` StrEnum，所有不可信字符串入口经容错 coerce 归一化（未知 → NOTE 兜底不崩、大小写归一），修复 debug LTM 过滤大小写 bug。

**Architecture:** 单层改动。schema 加 `MemoryType.try_coerce`（未知 → None，过滤用）+ `coerce`（= try_coerce or NOTE，存储/构造用）；`MemoryEntry` 加 `__post_init__` 构造归一化；`update_text` 赋值 / `search` 与 debug 过滤比较在边界显式 coerce。consolidator/reflector/system_prompt/contract 已对齐，零改动。

**Tech Stack:** Python 3.12 dataclass + `enum.StrEnum`。测试 pytest（marker=unit/service），conda 环境 `Elysia`。

## Global Constraints

- 生产代码函数参数必须类型注解（`make lint` 强制 ANN001/002/003）。
- 禁相对导入，一律绝对导入：`from core.schema.memory import MemoryType`。
- 代码注释/README 用中文；中英混合（中文嵌英文技术术语）可接受。
- `MemoryType` 成员值不变：`FACT="Fact" PREFERENCE="Preference" EVENT="Event" PLAN="Plan" NOTE="Note"`。
- **coerce 策略（用户拍板）**：存储/构造未知值 → `NOTE` 兜底不抛；过滤未知值 → 不命中（返回空，保留现状语义）。
- StrEnum 是 str 子类：`MemoryType.PLAN == "Plan"` 为 True，`== "plan"` 为 False；`str()/json.dumps()/f-string` 均输出值 `"Fact"`。

---

## File Structure

- **Modify:** `core/schema/memory.py` — 加 `try_coerce`/`coerce` classmethod（~25 行）
- **Modify:** `core/memory/long_term.py` — `MemoryEntry.memory_type` 类型 + `__post_init__`；insert metadata 去 `cast`；update_text/search 边界 coerce；Milvus 序列化 `.value`（~4 处）
- **Modify:** `core/input/debug_routes/ltm.py` — 列表过滤 `try_coerce`（1 处）
- **Modify:** `core/schema/tests/test_memory.py` — 加 `TestMemoryTypeCoerce`（~40 行）
- **Modify:** `core/memory/tests/test_long_term.py` — 加 MemoryEntry 归一 + search lowercase 过滤测试
- **Modify:** `core/input/tests/test_debug_ltm.py`（若存在；否则新建）— debug 过滤 lowercase 测试
- **Modify:** `core/tests/test_phase2.py:705,722,726` — 断言 lowercase → 规范值

### Task 1: schema 层 `MemoryType.try_coerce` + `MemoryType.coerce`

**Files:**
- Modify: `core/schema/memory.py`
- Test: `core/schema/tests/test_memory.py`

**Interfaces:**
- Produces: `MemoryType.try_coerce(value: object) -> MemoryType | None`、`MemoryType.coerce(value: object) -> MemoryType`。Task 2/3 依赖。

- [ ] **Step 1: 写失败测试（try_coerce）**

在 `core/schema/tests/test_memory.py` 末尾（import 加 `MemoryType`——已 import）加：

```python
@pytest.mark.unit
class TestMemoryTypeCoerce:
    """MemoryType.try_coerce / coerce — 不可信字符串入口的归一化。"""

    def test_try_coerce_passthrough_member(self):
        assert MemoryType.try_coerce(MemoryType.FACT) is MemoryType.FACT

    def test_try_coerce_exact_str(self):
        assert MemoryType.try_coerce("Fact") is MemoryType.FACT

    def test_try_coerce_case_insensitive(self):
        # lowercase / 全大写 / mixed → title 归一
        assert MemoryType.try_coerce("fact") is MemoryType.FACT
        assert MemoryType.try_coerce("FACT") is MemoryType.FACT
        assert MemoryType.try_coerce("preference") is MemoryType.PREFERENCE
        assert MemoryType.try_coerce("NOTE") is MemoryType.NOTE
        assert MemoryType.try_coerce("Note") is MemoryType.NOTE

    def test_try_coerce_strips_whitespace(self):
        assert MemoryType.try_coerce("  Fact  ") is MemoryType.FACT

    def test_try_coerce_unknown_returns_none(self):
        assert MemoryType.try_coerce("bogus") is None
        assert MemoryType.try_coerce("fact-1") is None
        assert MemoryType.try_coerce("") is None
        assert MemoryType.try_coerce(None) is None
        assert MemoryType.try_coerce(123) is None

    def test_coerce_unknown_falls_back_to_note(self):
        assert MemoryType.coerce("bogus") is MemoryType.NOTE
        assert MemoryType.coerce(None) is MemoryType.NOTE
        assert MemoryType.coerce(123) is MemoryType.NOTE
        assert MemoryType.coerce("") is MemoryType.NOTE

    def test_coerce_valid_unchanged(self):
        assert MemoryType.coerce("fact") is MemoryType.FACT
        assert MemoryType.coerce(MemoryType.PLAN) is MemoryType.PLAN
```

- [ ] **Step 2: 跑测试确认失败**

Run: `/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest core/schema/tests/test_memory.py -q --tb=short`
Expected: FAIL — `AttributeError: type object 'MemoryType' has no attribute 'try_coerce'`

- [ ] **Step 3: 最小实现**

在 `core/schema/memory.py` 的 `MemoryType` 类内加：

```python
    @classmethod
    def try_coerce(cls, value: object) -> "MemoryType | None":
        """任意来源 → MemoryType；未知/None/非 str 返回 None（不抛）。

        精确匹配 → 大小写不敏感（.title()）匹配。过滤边界用它：未知值 → None → 不命中。
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            text = value.strip()
            if text:
                try:
                    return cls(text)          # 精确匹配（"Fact"）
                except ValueError:
                    pass
                try:
                    return cls(text.title())  # 大小写不敏感（"fact"/"FACT"/"preference"）
                except ValueError:
                    pass
        return None

    @classmethod
    def coerce(cls, value: object) -> "MemoryType":
        """try_coerce 的 NOTE 兜底版——存储/构造边界用（未知值归一为 Note，不崩）。"""
        return cls.try_coerce(value) or cls.NOTE
```

注意：StrEnum 成员 str 值非空（`"Fact"` 等），`or cls.NOTE` 短路正确。

- [ ] **Step 4: 跑测试确认通过**

Run: 同上。Expected: PASS（含既有 ExtractedMemory 测试——`test_rejects_lowercase_type` 仍应 PASS，pydantic 严格校验不变）。

- [ ] **Step 5: 提交**

```bash
git add core/schema/memory.py core/schema/tests/test_memory.py
git commit -m "feat(schema): MemoryType.try_coerce/coerce——不可信字符串归一化（未知→None/NOTE，大小写不敏感）"
```

### Task 2: `MemoryEntry.memory_type` 类型 + `__post_init__` + long_term 全部边界

**Files:**
- Modify: `core/memory/long_term.py`（MemoryEntry 定义、insert metadata、update_text、search 过滤、Milvus 序列化 3 处）
- Test: `core/memory/tests/test_long_term.py`
- Modify: `core/tests/test_phase2.py:705,722,726`

**Interfaces:**
- Consumes: `MemoryType.try_coerce` / `MemoryType.coerce`（Task 1）
- Produces: `MemoryEntry.memory_type: MemoryType`（构造自动归一）；`search(memory_type: str | None)` / `update_text(memory_type: str | None)` 内部 coerce（签名不变）。Task 3 依赖 search/update 的 coerce 语义。

- [ ] **Step 1: 写失败测试**

`core/memory/tests/test_long_term.py` 末尾加（顶部加 `from core.schema.memory import MemoryType`）：

```python
@pytest.mark.unit
class TestMemoryTypeNormalization:
    """MemoryEntry 构造归一化 + search 过滤 lowercase。"""

    def test_constructor_normalizes_lowercase(self):
        from core.memory.long_term import MemoryEntry
        e = MemoryEntry(id="x", text="t", memory_type="fact")
        assert e.memory_type is MemoryType.FACT

    def test_constructor_unknown_falls_back_note(self):
        from core.memory.long_term import MemoryEntry
        e = MemoryEntry(id="x", text="t", memory_type="bogus")
        assert e.memory_type is MemoryType.NOTE

    @pytest.mark.asyncio
    async def test_search_filter_case_insensitive(self):
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        await mem.insert("工作计划A", memory_type="plan")
        hits = await mem.search(query_text="工作", memory_type="PLAN", top_k=10)
        assert len(hits) == 1
        assert hits[0].memory_type is MemoryType.PLAN

    @pytest.mark.asyncio
    async def test_search_filter_unknown_returns_empty(self):
        from core.memory.long_term import LongTermMemory
        mem = LongTermMemory()
        await mem.insert("工作计划A", memory_type="plan")
        hits = await mem.search(query_text="工作", memory_type="bogus", top_k=10)
        assert hits == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest core/memory/tests/test_long_term.py::TestMemoryTypeNormalization -q --tb=short`
Expected: FAIL — 构造后 `e.memory_type == "fact"`（raw str），非 `MemoryType.FACT`；过滤 `"PLAN"` 匹配不上 `"plan"` 存储值。

- [ ] **Step 3: 最小实现（long_term.py 一处到位，原子变更）**

3a. `MemoryEntry` 字段类型 + `__post_init__`（文件头加 `from core.schema.memory import MemoryType`）：

```python
@dataclass
class MemoryEntry:
    id: str = ""                    # 唯一标识（insert 时自动生成）
    text: str = ""
    embedding: list[float] = field(default_factory=list, repr=False)
    memory_type: MemoryType = MemoryType.NOTE   # Fact/Preference/Event/Plan/Note（构造时 coerce 归一）
    poignancy: int = 5             # 重要性 1-10
    timestamp: float = 0.0         # Unix 时间戳
    metadata: dict = field(default_factory=dict)
    _milvus_pk: int | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        """构造时归一化 memory_type（dataclass 无自动校验，直接赋值会绕过）。"""
        self.memory_type = MemoryType.coerce(self.memory_type)

    @property
    def dimension(self) -> int:
        """向量维度。"""
        return len(self.embedding)
```

3b. `insert` 内 metadata 提取（约 164 行）去 `cast`——`__post_init__` 已归一：

```python
            # 接受 'type' 作为 'memory_type' 的别名
            memory_type = meta.pop("memory_type", meta.pop("type", "Note"))
```

原 `memory_type: str = cast(str, ...)` 改为直接赋值；**同时移除**文件头 `from typing import TYPE_CHECKING, Any, cast` 里的 `cast`（grep 确认 `cast` 仅此一处使用，Step 3b 后必为死代码）。注意 ruff.toml 未启用 F401，残留未使用 import 不会使 `make lint` 失败——必须主动删除，不留死代码。

3c. Milvus 序列化显式 `.value`（182、303、360 三处）：

```python
                            "memory_type": entry.memory_type.value,
```
（三处 `"memory_type": <expr>.memory_type` → `.<expr>.memory_type.value`，按原缩进/上下文替换。）

3d. `update_text` 直接赋值（约 343-344 行）显式 coerce：

```python
                    if memory_type is not None:
                        e.memory_type = MemoryType.coerce(memory_type)
```

3e. `search` 过滤（约 547-551 行）用 `try_coerce`：

```python
        # 按 memory_type 过滤（如指定）——try_coerce 大小写不敏感，未知值不命中
        if memory_type:
            mt = MemoryType.try_coerce(memory_type)
            scored = [(s, e) for s, e in scored if mt is not None and e.memory_type == mt]
```

**同时改写紧邻的旧注释**：原 547-548 行注释 `# 注意：精确匹配，调用方需传 TitleCase（如 "Fact"）` 在改动后成为错误文档（过滤现为大小写不敏感），必须删除或改写为上方新注释。

- [ ] **Step 4: 适配既有断言（test_phase2.py，三处 lowercase → 规范值）**

`core/tests/test_phase2.py`：
- 行 705（`test_insert_supports_type_alias_for_memory_type`）：`assert entry.memory_type == "plan"` → `assert entry.memory_type == "Plan"`。其下一行失败消息 `f"Expected memory_type='plan', got {entry.memory_type!r}"` 同步改为 `'Plan'`。
- 行 722（`test_search_filter_by_memory_type`）：`assert all(e.memory_type == "plan" for e in plans)` → `== "Plan"`
- 行 726：`assert all(e.memory_type == "preference" for e in prefs)` → `== "Preference"`

（StrEnum `== "Plan"` 为 True。测试文件不必 import MemoryType。）

- [ ] **Step 5: 跑全受影响套件确认通过**

Run:
```bash
/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest core/memory/tests/test_long_term.py core/tests/test_phase2.py -m "not slow" -q --tb=short
```
Expected: PASS（`TestMemoryTypeNormalization` 新增 + 既有全部）。

- [ ] **Step 6: 提交**

```bash
git add core/memory/long_term.py core/memory/tests/test_long_term.py core/tests/test_phase2.py
git commit -m "refactor(memory): MemoryEntry.memory_type 对齐 MemoryType——__post_init__ 归一 + search/update 边界 coerce + Milvus .value"
```

### Task 3: debug LTM 过滤大小写修复

**Files:**
- Modify: `core/input/debug_routes/ltm.py:53`（列表过滤）
- Test: `core/input/tests/test_debug_ltm.py`（新建，若无同名文件）

**Interfaces:**
- Consumes: `MemoryType.try_coerce`（Task 1）；`insert` 的 `__post_init__` 归一（Task 2）——debug insert body 的 lowercase `mem_type` 透传到 insert 已自动归一，本任务只改**过滤**。

- [ ] **Step 1: 写失败测试**

新建 `core/input/tests/test_debug_ltm.py`（若已有同名文件则追加；构造真实 Agent + `create_app`，注入两条不同 memory_type 的 `_entries`）：

```python
"""/debug/api/ltm 列表过滤——lowercase 过滤应命中规范值存储（修复大小写 bug）。"""
import pytest

from core.input.server import create_app


def _make_agent():
    from core.config import AppConfig
    from core.agent import Agent
    from core.memory.long_term import MemoryEntry
    from core.schema.memory import MemoryType
    agent = Agent(config=AppConfig())
    agent.long_term._entries = [  # noqa: SLF001 — 测试直注入
        MemoryEntry(id="1", text="工作计划", memory_type=MemoryType.PLAN),
        MemoryEntry(id="2", text="用户偏好", memory_type=MemoryType.PREFERENCE),
    ]
    return agent


@pytest.mark.service
@pytest.mark.asyncio
async def test_ltm_list_filter_case_insensitive(aiohttp_client):
    """?type=plan（lowercase）应命中 PLAN 条目（此前匹配不上）。"""
    agent = _make_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    resp = await client.get("/debug/api/ltm", params={"type": "plan"})
    assert resp.status == 200
    data = await resp.json()
    ids = [e["id"] for e in data["entries"]]
    assert ids == ["1"]


@pytest.mark.service
@pytest.mark.asyncio
async def test_ltm_list_filter_unknown_returns_empty(aiohttp_client):
    """?type=bogus 应返回空（不误命中 NOTE）。"""
    agent = _make_agent()
    app = create_app(agent, serve_static=False)
    client = await aiohttp_client(app)
    resp = await client.get("/debug/api/ltm", params={"type": "bogus"})
    assert resp.status == 200
    data = await resp.json()
    assert data["entries"] == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest core/input/tests/test_debug_ltm.py -q --tb=short`
Expected: FAIL — `test_ltm_list_filter_case_insensitive` 返回空（`"plan"` 匹配不上 `MemoryType.PLAN`）。

- [ ] **Step 3: 最小实现（ltm.py 行 53 附近）**

```python
    if mem_type:
        mt = MemoryType.try_coerce(mem_type)
        entries = [e for e in entries if mt is not None and e.memory_type == mt]
```

（文件头加 `from core.schema.memory import MemoryType`；若既有 `from core.memory.long_term import MemoryEntry` 式导入，直接加绝对导入。）

- [ ] **Step 4: 跑测试确认通过**

Run: 同上。Expected: PASS（含 `test_ltm_list_filter_unknown_returns_empty`）。

- [ ] **Step 5: 提交**

```bash
git add core/input/debug_routes/ltm.py core/input/tests/test_debug_ltm.py
git commit -m "fix(debug): LTM 列表过滤大小写不敏感——try_coerce 归一，bogus 返回空"
```

### Task 4: 全量回归 + 门禁

**Files:** 无新增（验证任务）。

- [ ] **Step 1: 定向回归受影响模块**

```bash
/home/yomu/miniconda3/envs/Elysia/bin/python -m pytest core/memory core/tools/memory core/input/tests core/cognitive core/schema/tests core/monitoring/tests -m "not slow" -q --tb=short
```
Expected: PASS（`test_phase2` 已在 Task 2 覆盖，此处补 consolidation/reflector/system_prompt 相关）。

- [ ] **Step 2: 全项目门禁**

```bash
make test-normal
# 或: /home/yomu/miniconda3/envs/Elysia/bin/python -m pytest -m "not slow" -q --tb=short --ignore=Module/workflow_engine/tests
make lint
# 或: /home/yomu/miniconda3/envs/Elysia/bin/python -m ruff check core/ Module/ frontend_contract/ --exclude __pycache__
```
Expected: 全绿。

- [ ] **Step 3: 提交（若无代码改动则跳过）**

```bash
git status
# 若有遗漏修复，git add + commit；否则无提交
```

## Self-Review（已执行）

1. **Spec 覆盖**：coerce 双入口（try_coerce/coerce）、MemoryEntry 字段 + `__post_init__`、update_text/search/debug 边界、Milvus `.value`、测试适配（test_phase2 三处）、行为变化声明——全部映射到 Task 1-4。
2. **占位符扫描**：无 TBD/TODO；每步含可执行代码与运行命令。
3. **类型一致性**：Task 2/3 引用的 `MemoryType.try_coerce`/`coerce` 签名与 Task 1 定义一致；`search`/`update_text` 签名保持 `str | None` 不变，跨任务一致。
4. **已知耦合**：Task 2 是原子变更（字段类型改变立即破坏 test_phase2 三处断言，故同任务内适配），Task 3 独立。
