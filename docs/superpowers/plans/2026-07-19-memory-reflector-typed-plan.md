# MemoryReflector 管线类型化 + 词表统一 + 数据注入修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 MemoryReflector 生产静默空转（j2 无数据注入点），管线从裸 dict 类型化为 `ReflectorActions`，并统一全部 memory_type 读写面词表为 TitleCase。

**Architecture:** 三条线：(1) `prompt/MemoryReflector.j2` 末尾追加 `{memories}`/`{last_summary}` 数据段，让 `_review_group` 现有 `.replace()` 生效；(2) `core/schema/memory_reflector.py` 追加 4 个 Pydantic 模型，`_parse_actions` 校验失败 raise → per-group except 跳组（all-or-nothing）；(3) save_memory 插件 / server.py / long_term.py 默认值 / debug.html 下拉统一 TitleCase。

**Tech Stack:** Python 3.11+, Pydantic v2, pytest

## Global Constraints

- 测试 marker：schema 单元测试 `@pytest.mark.unit`，管线测试 `@pytest.mark.service`
- conda 环境：`conda run -n Elysia`（Makefile 内部已含，`make` 命令勿再包裹）
- 所有新增类型通过 `core/schema/__init__.py` re-export；`core/schema/README.md` 目录树 + 速查表同步
- 中文注释/docstring
- **不改**：j2 的规则部分（只追加数据段）、`_cluster_entries`、`MemoryReflectorState`、LTM `insert(**metadata)` passthrough、测试中 ~15 处 `insert(type="fact")` 小写调用、`core/consolidator.py`
- 已知 13 个 pre-existing test failures（SessionMemory set_param 4、system_prompt 4、task_system integration 4、idle flaky 1）——与本次无关，最终验证时忽略

---

## File Structure

```
prompt/MemoryReflector.j2                     [MODIFY]  末尾追加数据注入段（Task 2）
core/schema/memory_reflector.py               [MODIFY]  追加 4 模型（Task 1）
core/schema/__init__.py                       [MODIFY]  re-export（Task 1）
core/schema/README.md                         [MODIFY]  目录树+速查表（Task 1）
core/schema/tests/test_memory_reflector.py    [CREATE]  模型单元测试（Task 1）
core/memory/tests/test_memory_reflector.py    [CREATE]  管线服务测试（Task 2 创建注入测试，Task 3 扩展）
core/memory/memory_reflector.py               [MODIFY]  parse/validate/execute 类型化（Task 3）
core/tools/memory/plugin.py                   [MODIFY]  词表统一（Task 4）
core/tools/memory/test_memory.py              [MODIFY]  3 测试更新（Task 4）
core/input/server.py                        [MODIFY]  debug API 归一化（Task 4）
core/memory/long_term.py                      [MODIFY]  3 处默认值 + search 注释（Task 4）
static/debug.html                             [MODIFY]  2 个下拉（Task 4）
```

---

### Task 0: 记录测试基线

**Files:** 无改动——只记录

Global Constraints 与 Task 5 依赖"13 个已知 pre-existing failures"的规划期快照；环境漂移（如 idle flaky ±1）会让终验的"不得新增失败"无法客观判定。开工前先固化基线。

- [ ] **Step 1: 跑基线并保存失败清单**

```bash
make test-normal 2>&1 | tail -30 | tee /tmp/reflector-baseline.txt
grep '^FAILED' /tmp/reflector-baseline.txt | sort > /tmp/reflector-baseline-failures.txt
wc -l /tmp/reflector-baseline-failures.txt
```

Expected: 失败清单写入 `/tmp/reflector-baseline-failures.txt`（预期 ~13 条；实际条数以本次运行为准，Task 5 与它比对）

---

### Task 1: Schema 模型 + 单元测试 + 注册

**Files:**
- Modify: `core/schema/memory_reflector.py`（现 9 行，只有 `MemoryReflectorState`）
- Create: `core/schema/tests/test_memory_reflector.py`
- Modify: `core/schema/__init__.py`（L53：`from core.schema.memory_reflector import MemoryReflectorState  # noqa: F401`）
- Modify: `core/schema/README.md`

**Interfaces:**
- Consumes: `MemoryType`（`core/schema/memory.py`，已存在）
- Produces: `MergeItem`（`ids: list[str]` min 2 / `merged_content: str` / `type: MemoryType | None` / `poignancy: int | None`）、`UpdateItem`（`id` / `new_content`）、`DeleteItem`（`id`）、`ReflectorActions`（`merges` max 5 / `updates` max 5 / `deletes` max 3）——Task 3 消费

- [ ] **Step 1: 写失败测试 `core/schema/tests/test_memory_reflector.py`**

```python
# core/schema/tests/test_memory_reflector.py
"""core/schema/memory_reflector.py 的 ReflectorActions 模型测试。"""
import pytest
from pydantic import ValidationError

from core.schema.memory import MemoryType
from core.schema.memory_reflector import (
    DeleteItem,
    MergeItem,
    ReflectorActions,
    UpdateItem,
)


@pytest.mark.unit
class TestReflectorActions:
    """ReflectorActions 整体校验规则。"""

    def test_empty_object_valid(self):
        """{} → 三个空列表（干净组的合法输出）。"""
        a = ReflectorActions.model_validate({})
        assert a.merges == [] and a.updates == [] and a.deletes == []

    def test_partial_keys_valid(self):
        """部分键 → 缺失键取默认空列表。"""
        a = ReflectorActions.model_validate({"merges": []})
        assert a.updates == [] and a.deletes == []

    def test_full_valid_input(self):
        a = ReflectorActions.model_validate({
            "merges": [{"ids": ["m1", "m2"], "merged_content": "合并后内容",
                        "type": "Fact", "poignancy": 7}],
            "updates": [{"id": "m3", "new_content": "更新后内容"}],
            "deletes": [{"id": "m4"}],
        })
        assert a.merges[0].type == MemoryType.FACT
        assert a.merges[0].ids == ["m1", "m2"]
        assert a.updates[0].new_content == "更新后内容"
        assert a.deletes[0].id == "m4"

    def test_caps_exceeded_rejected(self):
        """超限拒绝：6 merges / 6 updates / 4 deletes。"""
        merge = {"ids": ["a", "b"], "merged_content": "x"}
        update = {"id": "a", "new_content": "x"}
        delete = {"id": "a"}
        with pytest.raises(ValidationError):
            ReflectorActions.model_validate({"merges": [merge] * 6})
        with pytest.raises(ValidationError):
            ReflectorActions.model_validate({"updates": [update] * 6})
        with pytest.raises(ValidationError):
            ReflectorActions.model_validate({"deletes": [delete] * 4})

    def test_reason_extra_field_ignored(self):
        """updates/deletes 带 reason（prompt 要求 LLM 输出）→ 被忽略不报错。"""
        a = ReflectorActions.model_validate({
            "updates": [{"id": "m1", "new_content": "新", "reason": "过时"}],
            "deletes": [{"id": "m2", "reason": "纯噪音"}],
        })
        assert not hasattr(a.updates[0], "reason")


@pytest.mark.unit
class TestMergeItem:
    def test_single_id_rejected(self):
        """ids 少于 2 个 → 拒绝（原 len(ids) >= 2 守卫）。"""
        with pytest.raises(ValidationError):
            MergeItem.model_validate({"ids": ["only-one"], "merged_content": "x"})

    def test_empty_merged_content_rejected(self):
        with pytest.raises(ValidationError):
            MergeItem.model_validate({"ids": ["a", "b"], "merged_content": ""})
        with pytest.raises(ValidationError):
            MergeItem.model_validate({"ids": ["a", "b"], "merged_content": "   "})

    def test_type_lowercase_rejected(self):
        """严格枚举——小写 "fact" 拒绝（词表统一后全库 TitleCase）。"""
        with pytest.raises(ValidationError):
            MergeItem.model_validate(
                {"ids": ["a", "b"], "merged_content": "x", "type": "fact"})

    def test_type_defaults_none(self):
        """缺省 type/poignancy → None（update_text 保留原值语义）。"""
        m = MergeItem.model_validate({"ids": ["a", "b"], "merged_content": "x"})
        assert m.type is None and m.poignancy is None

    def test_poignancy_out_of_range_rejected(self):
        for bad in (0, 11):
            with pytest.raises(ValidationError):
                MergeItem.model_validate(
                    {"ids": ["a", "b"], "merged_content": "x", "poignancy": bad})


@pytest.mark.unit
class TestUpdateDeleteItem:
    def test_update_empty_fields_rejected(self):
        with pytest.raises(ValidationError):
            UpdateItem.model_validate({"id": "", "new_content": "x"})
        with pytest.raises(ValidationError):
            UpdateItem.model_validate({"id": "a", "new_content": "  "})

    def test_delete_empty_id_rejected(self):
        with pytest.raises(ValidationError):
            DeleteItem.model_validate({"id": ""})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `conda run -n Elysia python -m pytest core/schema/tests/test_memory_reflector.py -q --tb=line`
Expected: FAIL/ERROR — `ImportError: cannot import name 'MergeItem'`

- [ ] **Step 3: 实现模型 — `core/schema/memory_reflector.py` 追加**

在现有 `MemoryReflectorState` 之后追加（import 区合并到文件顶部）：

```python
"""MemoryReflectorState — MemoryReflector 计数器状态 + LLM 输出模型。"""

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from .memory import MemoryType


class MemoryReflectorState(BaseModel):
    mr_round_count: int = 0
    mr_new_memory_count: int = 0
    mr_last_summary: dict[str, object] = Field(default_factory=dict)


class MergeItem(BaseModel):
    """合并操作：ids[0] 保留并更新内容，其余标记 absorbed。"""
    ids: list[str] = Field(min_length=2)
    merged_content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    type: MemoryType | None = None
    poignancy: int | None = Field(default=None, ge=1, le=10)


class UpdateItem(BaseModel):
    """更新操作：改写单条记忆内容。"""
    id: str = Field(min_length=1)
    new_content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class DeleteItem(BaseModel):
    """删除操作。"""
    id: str = Field(min_length=1)


class ReflectorActions(BaseModel):
    """MemoryReflector.j2 输出格式。max_length 即原 _validate_actions 的 5/5/3 上限。"""
    merges: list[MergeItem] = Field(default_factory=list, max_length=5)
    updates: list[UpdateItem] = Field(default_factory=list, max_length=5)
    deletes: list[DeleteItem] = Field(default_factory=list, max_length=3)
```

（整文件如上——原文件仅 9 行，直接给出最终形态；模块 docstring 更新。）

- [ ] **Step 4: 跑测试确认通过**

Run: `conda run -n Elysia python -m pytest core/schema/tests/test_memory_reflector.py -q --tb=short`
Expected: 12 passed

- [ ] **Step 5: 注册 — `core/schema/__init__.py`**

```python
# Before（L53）
from core.schema.memory_reflector import MemoryReflectorState  # noqa: F401

# After
from core.schema.memory_reflector import (  # noqa: F401
    MemoryReflectorState,
    MergeItem,
    UpdateItem,
    DeleteItem,
    ReflectorActions,
)
```

- [ ] **Step 6: 注册 — `core/schema/README.md`**

目录树行（文字锚 `memory_reflector.py`）：

```
# Before
├── memory_reflector.py # MemoryReflectorState — MemoryReflector 状态

# After
├── memory_reflector.py # MemoryReflectorState + ReflectorActions — 状态 + LLM 输出类型
```

模块速查表 `memory_reflector.py` 行：

```
# Before
| `memory_reflector.py` | MemoryReflectorState — MemoryReflector 状态 |

# After
| `memory_reflector.py` | MemoryReflectorState + MergeItem/UpdateItem/DeleteItem/ReflectorActions — 状态 + MemoryReflector LLM 输出类型 |
```

（两处均按文字锚定位；若原行措辞略有出入，保持行首格式、只更新描述部分。）

- [ ] **Step 7: 验证 re-export + schema 全量测试**

```bash
conda run -n Elysia python -c "from core.schema import ReflectorActions, MergeItem, UpdateItem, DeleteItem; print('OK')"
conda run -n Elysia python -m pytest core/schema/tests/ -q --tb=short
```
Expected: `OK`；schema tests 全 passed

- [ ] **Step 8: Commit**

```bash
git add core/schema/memory_reflector.py core/schema/tests/test_memory_reflector.py core/schema/__init__.py core/schema/README.md
git commit -m "feat: add ReflectorActions 模型 — MemoryReflector LLM 输出类型化"
```

---

### Task 2: j2 数据注入修复 + 注入断言测试 🔴

**Files:**
- Modify: `prompt/MemoryReflector.j2`（239 行，末尾追加）
- Create: `core/memory/tests/test_memory_reflector.py`（本 task 只含注入测试类，Task 3 扩展）

**Interfaces:**
- Consumes: `MemoryReflector._review_group`（现有 `.replace("{memories}", ...)` 机制，[memory_reflector.py:208-212](core/memory/memory_reflector.py#L208)，代码零改动）
- Produces: j2 含 `{memories}` / `{last_summary}` 字面占位符；测试文件骨架（fake LLM + MemoryEntry 构造模式）供 Task 3 复用

**背景（给实现者）：** 生产 bug——j2 全文没有占位符，`_review_group` 的 `.replace()` 是空操作，LLM 只收到格式说明、零条记忆数据，反思器永久静默空转。本 task 修复注入并用测试钉死。

- [ ] **Step 1: 写失败测试 `core/memory/tests/test_memory_reflector.py`**

```python
# core/memory/tests/test_memory_reflector.py
"""MemoryReflector 管线测试：数据注入 + 解析 + 执行。"""
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from core.memory.long_term import MemoryEntry
from core.memory.memory_reflector import MemoryReflector


class FakeLLM:
    """捕获 prompt 并按序返回预设响应的假 LLM。"""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.prompts: list[str] = []

    async def think(self, messages, extra_body=None):
        self.prompts.append(messages[0]["content"])
        return SimpleNamespace(text=self._responses.pop(0))


def _entry(id_: str, text: str, mem_type: str = "Fact") -> MemoryEntry:
    return MemoryEntry(id=id_, text=text, memory_type=mem_type,
                       poignancy=5, timestamp=1720000000.0)


def _reflector(llm, memory=None) -> MemoryReflector:
    return MemoryReflector(
        llm=llm,
        memory=memory if memory is not None else MagicMock(),
        session=MagicMock(),
    )


@pytest.mark.service
class TestPromptInjection:
    """🔴 防回归：j2 占位符丢失会导致 LLM 收不到记忆数据、反思器静默空转。"""

    @pytest.mark.asyncio
    async def test_review_group_prompt_contains_entry_data(self):
        llm = FakeLLM(responses=["{}"])
        r = _reflector(llm)
        entries = [_entry("mem-id-001", "妖梦养了一只叫橘子的猫")]

        await r._review_group(entries)

        assert len(llm.prompts) == 1
        prompt = llm.prompts[0]
        # 序列化条目必须真正出现在发给 LLM 的 prompt 中
        assert "mem-id-001" in prompt
        assert "妖梦养了一只叫橘子的猫" in prompt
        # 占位符必须已被替换掉
        assert "{memories}" not in prompt
        assert "{last_summary}" not in prompt
```

- [ ] **Step 2: 跑测试确认失败**

Run: `conda run -n Elysia python -m pytest core/memory/tests/test_memory_reflector.py -q --tb=short`
Expected: FAIL — `assert "mem-id-001" in prompt` 不成立（j2 无占位符，replace 空操作，prompt 里没有数据）

注意：若此步意外 PASS，说明 j2 已被修过——停下核实，不要盲目继续。

- [ ] **Step 3: 修复 — `prompt/MemoryReflector.j2` 末尾追加**

在文件最后一行（`- JSON 中不得使用尾随逗号。`）之后追加：

```

以下是本组待审查的记忆数据：

{memories}

上次运行的摘要（仅供参考，可能为空）：

{last_summary}
```

（占位符是 `{memories}` / `{last_summary}` 字面文本——与 `_review_group` 现有 `.replace()` 精确匹配。规则部分一个字不动。）

- [ ] **Step 4: 跑测试确认通过**

Run: `conda run -n Elysia python -m pytest core/memory/tests/test_memory_reflector.py -q --tb=short`
Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
git add prompt/MemoryReflector.j2 core/memory/tests/test_memory_reflector.py
git commit -m "fix: MemoryReflector.j2 追加数据注入段 — 修复反思器生产静默空转"
```

---

### Task 3: memory_reflector.py 管线类型化

**Files:**
- Modify: `core/memory/memory_reflector.py`
- Modify: `core/memory/tests/test_memory_reflector.py`（扩展 Task 2 创建的文件）

**Interfaces:**
- Consumes: `ReflectorActions` / `MemoryType`（Task 1）；Task 2 测试文件中的 `FakeLLM` / `_entry` / `_reflector` 辅助
- Produces: `_parse_actions(text: str) -> ReflectorActions`（raise ValueError on 不可解析/校验失败）；`_review_group(group) -> ReflectorActions`；`_execute_actions(actions: ReflectorActions) -> tuple[int, int, int]`；`_validate_actions` 删除

- [ ] **Step 1: 扩展测试 — 追加到 `core/memory/tests/test_memory_reflector.py`**

文件顶部 import 区追加：

```python
from core.memory.memory_reflector import MemoryReflector  # 已有
from core.schema.memory_reflector import ReflectorActions  # 新增
```

文件末尾追加三个测试类：

```python
@pytest.mark.service
class TestParseActions:
    """_parse_actions: dict → ReflectorActions 转换点。"""

    def test_valid_json(self):
        a = MemoryReflector._parse_actions(
            '{"merges": [], "updates": [{"id": "m1", "new_content": "新内容"}], "deletes": []}'
        )
        assert isinstance(a, ReflectorActions)
        assert a.updates[0].id == "m1"

    def test_fence_wrapped(self):
        a = MemoryReflector._parse_actions('```json\n{"merges": []}\n```')
        assert isinstance(a, ReflectorActions)

    def test_empty_object_valid(self):
        a = MemoryReflector._parse_actions("{}")
        assert a.merges == [] and a.updates == [] and a.deletes == []

    def test_plain_text_raises(self):
        with pytest.raises(ValueError):
            MemoryReflector._parse_actions("这不是 JSON")

    def test_non_dict_json_raises(self):
        """合法 JSON 但非 dict（null / 数组）→ 走 ValidationError 路径。"""
        with pytest.raises(ValueError):
            MemoryReflector._parse_actions("null")
        with pytest.raises(ValueError):
            MemoryReflector._parse_actions("[1, 2]")

    def test_schema_invalid_raises(self):
        """6 merges 超限 → ValueError。"""
        merge = '{"ids": ["a", "b"], "merged_content": "x"}'
        with pytest.raises(ValueError):
            MemoryReflector._parse_actions(
                '{"merges": [%s]}' % ", ".join([merge] * 6))


@pytest.mark.service
class TestExecuteActions:
    """_execute_actions: 属性访问 + 返回值守卫 + IO 容错。"""

    def _memory_stub(self):
        m = MagicMock()
        m.update_text = AsyncMock(return_value=True)
        m.delete = AsyncMock(return_value=True)
        m.mark_absorbed = MagicMock()
        return m

    @pytest.mark.asyncio
    async def test_merge_executes(self):
        mem = self._memory_stub()
        r = _reflector(FakeLLM([]), memory=mem)
        actions = ReflectorActions.model_validate({
            "merges": [{"ids": ["m1", "m2", "m3"], "merged_content": "合并内容",
                        "type": "Fact", "poignancy": 8}],
        })
        merges, updates, deletes = await r._execute_actions(actions)
        assert merges == 1
        mem.update_text.assert_awaited_once_with(
            "m1", "合并内容", poignancy=8, memory_type="Fact")
        assert mem.mark_absorbed.call_count == 2  # m2, m3

    @pytest.mark.asyncio
    async def test_merge_type_none_passthrough(self):
        mem = self._memory_stub()
        r = _reflector(FakeLLM([]), memory=mem)
        actions = ReflectorActions.model_validate({
            "merges": [{"ids": ["m1", "m2"], "merged_content": "合并"}],
        })
        await r._execute_actions(actions)
        mem.update_text.assert_awaited_once_with(
            "m1", "合并", poignancy=None, memory_type=None)

    @pytest.mark.asyncio
    async def test_merge_target_missing_no_absorb(self):
        """🔴 update_text 返回 False（LLM 幻觉 id）→ 不 mark_absorbed、不计数。"""
        mem = self._memory_stub()
        mem.update_text = AsyncMock(return_value=False)
        r = _reflector(FakeLLM([]), memory=mem)
        actions = ReflectorActions.model_validate({
            "merges": [{"ids": ["ghost", "m2"], "merged_content": "合并"}],
        })
        merges, _, _ = await r._execute_actions(actions)
        assert merges == 0
        mem.mark_absorbed.assert_not_called()

    @pytest.mark.asyncio
    async def test_merge_absorb_exception_partial(self):
        """mark_absorbed 中途抛异常 → 该 merge 不计数，后续 item 继续。"""
        mem = self._memory_stub()
        mem.mark_absorbed.side_effect = RuntimeError("boom")
        r = _reflector(FakeLLM([]), memory=mem)
        actions = ReflectorActions.model_validate({
            "merges": [{"ids": ["m1", "m2"], "merged_content": "合并"}],
            "deletes": [{"id": "m9"}],
        })
        merges, _, deletes = await r._execute_actions(actions)
        assert merges == 0      # 异常中断计数
        assert deletes == 1     # 后续 delete 不受影响

    @pytest.mark.asyncio
    async def test_update_counts_by_return(self):
        mem = self._memory_stub()
        mem.update_text = AsyncMock(side_effect=[True, False])
        r = _reflector(FakeLLM([]), memory=mem)
        actions = ReflectorActions.model_validate({
            "updates": [{"id": "m1", "new_content": "a"},
                        {"id": "m2", "new_content": "b"}],
        })
        _, updates, _ = await r._execute_actions(actions)
        assert updates == 1  # False 不计数

    @pytest.mark.asyncio
    async def test_delete_counts_by_return(self):
        mem = self._memory_stub()
        mem.delete = AsyncMock(side_effect=[True, False])
        r = _reflector(FakeLLM([]), memory=mem)
        actions = ReflectorActions.model_validate({
            "deletes": [{"id": "m1"}, {"id": "m2"}],
        })
        _, _, deletes = await r._execute_actions(actions)
        assert deletes == 1

    @pytest.mark.asyncio
    async def test_update_exception_isolated(self):
        """update_text 抛异常 → 该项跳过，其余继续（IO 容错）。"""
        mem = self._memory_stub()
        mem.update_text = AsyncMock(side_effect=[RuntimeError("io"), True])
        r = _reflector(FakeLLM([]), memory=mem)
        actions = ReflectorActions.model_validate({
            "updates": [{"id": "m1", "new_content": "a"},
                        {"id": "m2", "new_content": "b"}],
        })
        _, updates, _ = await r._execute_actions(actions)
        assert updates == 1


@pytest.mark.service
class TestDoReflect:
    """_do_reflect: 坏组隔离 + 计数器复位。"""

    @pytest.mark.asyncio
    async def test_bad_group_skipped_others_execute(self):
        mem = MagicMock()
        mem.update_text = AsyncMock(return_value=True)
        mem.delete = AsyncMock(return_value=True)
        mem.mark_absorbed = MagicMock()
        group_a = [_entry("a1", "内容A")]
        group_b = [_entry("b1", "内容B")]
        mem.get_all.return_value = group_a + group_b
        # 第一组返回非法输出（坏组），第二组返回一条合法 delete
        llm = FakeLLM(responses=[
            "这不是 JSON",
            '{"deletes": [{"id": "b1"}]}',
        ])
        r = _reflector(llm, memory=mem)
        r._round_count = 50  # 触发条件
        r._cluster_entries = MagicMock(return_value=[group_a, group_b])

        summary = await r._do_reflect()

        assert summary["deletes"] == 1      # 坏组被跳过，好组正常执行
        assert summary["merges"] == 0
        assert r._round_count == 0          # 计数器复位
        mem.delete.assert_awaited_once_with("b1")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `conda run -n Elysia python -m pytest core/memory/tests/test_memory_reflector.py -q --tb=line`
Expected: TestParseActions 多个 FAIL（当前 `_parse_actions` 返回 dict，`isinstance(a, ReflectorActions)` 不成立；纯文本不 raise）；TestExecuteActions FAIL（当前签名收 dict）

- [ ] **Step 3: 实现 — `core/memory/memory_reflector.py`**

**3a. import（L19）：**

```python
# Before
from core.schema.memory_reflector import MemoryReflectorState

# After
from pydantic import ValidationError

from core.schema.memory_reflector import MemoryReflectorState, ReflectorActions
```

**3b. `_review_group`（L201-217）签名 + docstring：**

```python
# Before
    async def _review_group(self, group) -> dict:
        """LLM 审查一组记忆，返回 actions dict。"""

# After
    async def _review_group(self, group) -> ReflectorActions:
        """LLM 审查一组记忆，返回 ReflectorActions。"""
```

（函数体不变——`.replace()` 注入机制照旧，末行 `return self._parse_actions(result.text)` 照旧。）

**3c. `_parse_actions`（L219-237）：**

```python
# Before
    @staticmethod
    def _parse_actions(text: str) -> dict:
        """解析 LLM 输出为 actions dict。"""
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:]) if len(lines) > 1 else text
            if text.endswith("```"):
                text = text[:-3]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
        return {"merges": [], "updates": [], "deletes": []}

# After
    @staticmethod
    def _parse_actions(text: str) -> ReflectorActions:
        """解析 LLM 输出为 ReflectorActions。

        不可解析的文本或 schema 校验失败 → raise ValueError，
        由 _do_reflect 的 per-group except 捕获后跳过该组（下轮重试）。
        {} 或部分键是合法输入（字段默认空列表）。
        """
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:]) if len(lines) > 1 else text
            if text.endswith("```"):
                text = text[:-3]
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match is None:
                raise ValueError("MemoryReflector: LLM 输出无法解析为 JSON")
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                raise ValueError("MemoryReflector: LLM 输出无法解析为 JSON")
        try:
            return ReflectorActions.model_validate(data)
        except ValidationError as e:
            logger.warning("MemoryReflector: actions 校验失败: %s", e)
            raise ValueError(f"actions 校验失败: {e}") from e
```

**3d. 删除 `_validate_actions`（L239-251）整个方法** + `_do_reflect` 中的调用（L126-132）：

```python
# Before
                actions = await self._review_group(group)
                # 预校验
                if not self._validate_actions(actions):
                    logger.warning("MemoryReflector: action validation failed for group, skipping")
                    continue
                # 执行
                m, u, d = await self._execute_actions(actions)

# After
                actions = await self._review_group(group)
                m, u, d = await self._execute_actions(actions)
```

**3e. `_execute_actions`（L253-293）：**

```python
# After（整方法替换）— 属性访问；数据守卫删除（模型保证）；
#   update_text/delete 返回 bool，False 不计数、merge 不 mark_absorbed
    async def _execute_actions(self, actions: ReflectorActions) -> tuple[int, int, int]:
        """执行 actions（merge → update → delete）。返回 (merges, updates, deletes) 计数。"""
        merges = updates = deletes = 0

        for m in actions.merges:
            try:
                ok = await self._memory.update_text(
                    m.ids[0], m.merged_content,
                    poignancy=m.poignancy,
                    memory_type=m.type.value if m.type else None,
                )
                if not ok:
                    # 目标 id 不存在（LLM 幻觉）——合并内容未写入，
                    # 绝不能 mark_absorbed 隐藏真实条目
                    logger.warning("MemoryReflector merge 目标不存在: %s", m.ids[0])
                    continue
                for absorbed_id in m.ids[1:]:
                    self._memory.mark_absorbed(absorbed_id)
                merges += 1
            except Exception:
                logger.debug("MemoryReflector merge failed for %s", m.ids[0], exc_info=True)

        for u in actions.updates:
            try:
                if await self._memory.update_text(u.id, u.new_content):
                    updates += 1
            except Exception:
                logger.debug("MemoryReflector update failed for %s", u.id, exc_info=True)

        for d in actions.deletes:
            try:
                if await self._memory.delete(d.id):
                    deletes += 1
            except Exception:
                logger.debug("MemoryReflector delete failed for %s", d.id, exc_info=True)

        return merges, updates, deletes
```

- [ ] **Step 4: 跑测试确认通过**

Run: `conda run -n Elysia python -m pytest core/memory/tests/ -q --tb=short`
Expected: 全 passed（注入测试 + 新增 3 类 + test_long_term/test_session 不回归）

- [ ] **Step 5: Commit**

```bash
git add core/memory/memory_reflector.py core/memory/tests/test_memory_reflector.py
git commit -m "refactor: MemoryReflector 管线类型化 — ReflectorActions + 返回值守卫，删 _validate_actions"
```

---

### Task 4: 词表统一（4 写入面 + 2 读取面）

**Files:**
- Modify: `core/tools/memory/plugin.py`（L55-93 区域）
- Modify: `core/tools/memory/test_memory.py`（3 个测试）
- Modify: `core/input/server.py`（`ltm_insert_handler`，L225-240；import 区 L15-17 附近）
- Modify: `core/memory/long_term.py`（L27 / L109 / L216 / L527 注释）
- Modify: `static/debug.html`（L242-244 / L1366-1372）

**Interfaces:**
- Consumes: `MemoryType`（`core/schema/memory.py`，已在 main）
- Produces: 全部写入面存储 TitleCase；debug.html 过滤与存储值精确匹配

- [ ] **Step 1: 更新 save_memory 测试（先写失败测试）**

`core/tools/memory/test_memory.py`：

**`test_invalid_memory_type`（L112-119）：**
```python
# Before docstring + 断言
        """不在 {fact, plan, preference, note} 中的类型 → 返回 无效的记忆类型。"""
        ...
        assert "无效的记忆类型" in result
        assert "invalid_type" in result

# After — 错误消息列出 TitleCase 词表
        """不在 MemoryType 词表中的类型 → 返回 无效的记忆类型 + TitleCase 词表。"""
        ...
        assert "无效的记忆类型" in result
        assert "invalid_type" in result
        assert "Fact" in result      # 错误消息含 TitleCase 词表
        assert "Event" in result
```

**`test_save_fact`（L135-143）——spec 引用的两行精确断言：**
```python
# Before（L142-143）
        assert result == "已保存 fact 类型记忆: 用户叫张三"
        mock_memory.insert.assert_called_once_with("用户叫张三", type="fact")

# After — TitleCase 存储
        assert result == "已保存 Fact 类型记忆: 用户叫张三"
        mock_memory.insert.assert_called_once_with("用户叫张三", type="Fact")
```

**`test_all_valid_types_accepted`（L159-172）：**
```python
# Before
    @pytest.mark.parametrize("mem_type", ["fact", "plan", "preference", "note"])
    async def test_all_valid_types_accepted(
        self, plugin: MemorySavePlugin, mem_type: str,
    ):
        """fact, plan, preference, note 四种类型都会被接受。"""
        ...
        assert "已保存" in result
        assert mem_type in result
        mock_memory.insert.assert_called_once()

# After — 5 词含 event；大小写不敏感；消息中是 TitleCase
    @pytest.mark.parametrize("mem_type", ["fact", "preference", "event", "plan", "note",
                                          "Fact", "EVENT"])
    async def test_all_valid_types_accepted(
        self, plugin: MemorySavePlugin, mem_type: str,
    ):
        """五种类型（含 event）大小写不敏感接受，存储归一化为 TitleCase。"""
        ...
        assert "已保存" in result
        assert mem_type.title() in result   # 消息中是 TitleCase
        mock_memory.insert.assert_called_once_with("内容", type=mem_type.title())
```

- [ ] **Step 2: 跑测试确认失败**

Run: `conda run -n Elysia python -m pytest core/tools/memory/test_memory.py -q --tb=line`
Expected: 上述 3 个测试 FAIL（插件还是小写词表，`event`/`Fact`/`EVENT` 被拒）

- [ ] **Step 3: 实现 — `core/tools/memory/plugin.py`**

import 区（文件顶部现有 import 之后）追加：

```python
from core.schema.memory import MemoryType
```

`SaveMemoryParams.type` description 更新：

```python
# Before
    type: str = Field(
        default="fact",
        description="记忆类型: fact(事实), plan(计划), preference(偏好), note(笔记)",
    )

# After
    type: str = Field(
        default="fact",
        description="记忆类型: fact(事实), preference(偏好), event(事件), plan(计划), note(笔记)",
    )
```

`MemorySavePlugin`：删除类属性 `_VALID_TYPES = {"fact", "plan", "preference", "note"}`，`execute` 改为：

```python
# Before
        if params.type not in self._VALID_TYPES:
            return f"无效的记忆类型 '{params.type}'，允许: {', '.join(sorted(self._VALID_TYPES))}"

        memory = ctx.get("memory")
        if memory is None:
            return "记忆系统当前不可用。"

        content = params.content
        try:
            await memory.insert(content, type=params.type)
        except Exception as e:
            logger.warning("save_memory 失败: %s", e)
            return f"保存记忆时出错: {e}"

        return f"已保存 {params.type} 类型记忆: {content[:200]}"

# After — 大小写不敏感接受，TitleCase 存储，词表源自枚举
        try:
            mem_type = MemoryType(params.type.strip().title())
        except ValueError:
            allowed = ", ".join(t.value for t in MemoryType)
            return f"无效的记忆类型 '{params.type}'，允许: {allowed}"

        memory = ctx.get("memory")
        if memory is None:
            return "记忆系统当前不可用。"

        content = params.content
        try:
            await memory.insert(content, type=mem_type.value)
        except Exception as e:
            logger.warning("save_memory 失败: %s", e)
            return f"保存记忆时出错: {e}"

        return f"已保存 {mem_type.value} 类型记忆: {content[:200]}"
```

- [ ] **Step 4: 跑插件测试确认通过**

Run: `conda run -n Elysia python -m pytest core/tools/memory/test_memory.py -q --tb=short`
Expected: 全 passed

- [ ] **Step 5: `core/input/server.py` — debug API 归一化**

import 区（L17 `from core.schema.types import SessionId` 之后）追加：

```python
from core.schema.memory import MemoryType
```

`ltm_insert_handler`（L225-240）：

```python
# Before
        try:
            body = await request.json()
            text = body.get("text", "").strip()
            if not text:
                return web.json_response({"error": "text 不能为空"}, status=400)
            mem_type = body.get("memory_type", "note")
            poignancy = int(body.get("poignancy", 5))
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "无效的请求体"}, status=400)

        await agent._long_term.insert(text, memory_type=mem_type, poignancy=poignancy)

# After — 归一化在 try 外（否则枚举 ValueError 被吞成笼统"无效的请求体"）
        try:
            body = await request.json()
            text = body.get("text", "").strip()
            if not text:
                return web.json_response({"error": "text 不能为空"}, status=400)
            raw_type = str(body.get("memory_type", "Note"))
            poignancy = int(body.get("poignancy", 5))
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "无效的请求体"}, status=400)

        try:
            mem_type = MemoryType(raw_type.strip().title()).value
        except ValueError:
            allowed = ", ".join(t.value for t in MemoryType)
            return web.json_response(
                {"error": f"无效的 memory_type '{raw_type}'，允许: {allowed}"}, status=400)

        await agent._long_term.insert(text, memory_type=mem_type, poignancy=poignancy)
```

- [ ] **Step 6: `core/memory/long_term.py` — 3 处默认值 + search 注释**

```python
# L27
# Before:  memory_type: str = "note"      # Fact / Preference / Event / Plan / Note
# After:   memory_type: str = "Note"      # Fact / Preference / Event / Plan / Note

# L109
# Before:  memory_type=row.get("memory_type", "note"),
# After:   memory_type=row.get("memory_type", "Note"),

# L216
# Before:  memory_type = meta.pop("memory_type", meta.pop("type", "note"))
# After:   memory_type = meta.pop("memory_type", meta.pop("type", "Note"))

# L527-528 — 只加注释，比较器不改
# Before:
        # Filter by memory_type if requested
        if memory_type:
# After:
        # Filter by memory_type if requested
        # 注意：精确匹配，调用方需传 TitleCase（如 "Fact"）
        if memory_type:
```

- [ ] **Step 7: `static/debug.html` — 两个下拉**

**LTM 过滤下拉（L1366-1372）——按内容替换下面 5 行，保留首行 `<option value="">全部</option>` 不动：**

```html
<!-- Before -->
        <option value="fact">Fact</option>
        <option value="plan">Plan</option>
        <option value="preference">Preference</option>
        <option value="event">Event</option>
        <option value="note">Note</option>

<!-- After — value 改 TitleCase，与存储值精确匹配 -->
        <option value="Fact">Fact</option>
        <option value="Plan">Plan</option>
        <option value="Preference">Preference</option>
        <option value="Event">Event</option>
        <option value="Note">Note</option>
```

**添加记忆下拉（L242-244）——补 event + TitleCase：**

```html
<!-- Before -->
            <option value="fact">fact</option><option value="plan">plan</option>
            <option value="preference">preference</option><option value="note">note</option>

<!-- After -->
            <option value="Fact">Fact</option><option value="Preference">Preference</option>
            <option value="Event">Event</option><option value="Plan">Plan</option>
            <option value="Note">Note</option>
```

- [ ] **Step 8: 跑相关测试**

```bash
conda run -n Elysia python -m pytest core/tools/memory/test_memory.py core/memory/tests/ core/tests/test_phase2.py::TestMemoryConsolidator -q --tb=short -m "not slow"
```
Expected: 全 passed（`test_phase3.py:1539` 的 debug API 测试 POST 小写 "fact" → 归一化接受，断言只查 text，改后仍过——最终验证覆盖）

- [ ] **Step 9: Commit**

```bash
git add core/tools/memory/plugin.py core/tools/memory/test_memory.py core/input/server.py core/memory/long_term.py static/debug.html
git commit -m "refactor: memory_type 词表统一 TitleCase — save_memory 补 event、debug API 归一化、debug.html 下拉修复"
```

---

### Task 5: 最终验证

**Files:** 无改动——只验证

- [ ] **Step 1: 目标套件全绿**

```bash
conda run -n Elysia python -m pytest core/schema/tests/ core/memory/tests/ core/tools/memory/test_memory.py -q --tb=short -m "not slow"
```
Expected: 全 passed

- [ ] **Step 2: import 一致性**

```bash
conda run -n Elysia python -c "
from core.schema import ReflectorActions, MergeItem, UpdateItem, DeleteItem, MemoryType
from core.memory.memory_reflector import MemoryReflector
print('All imports OK')
"
```
Expected: `All imports OK`

- [ ] **Step 3: `make test-normal` 与 Task 0 基线比对**

```bash
make test-normal 2>&1 | tail -30 | tee /tmp/reflector-final.txt
grep '^FAILED' /tmp/reflector-final.txt | sort > /tmp/reflector-final-failures.txt
diff /tmp/reflector-baseline-failures.txt /tmp/reflector-final-failures.txt && echo "无新增失败"
```

Expected: `diff` 输出为空（`无新增失败`）——失败清单与 Task 0 基线完全一致。若 diff 有 `>` 行（新增失败）即本次引入，须修复；`<` 行（失败消失）说明顺手修好了 pre-existing，记录即可

---

## Self-Review 记录

- **Spec coverage**：Section 零→Task 2；一→Task 1；二→Task 3；三（4 写入面 + 2 读取面 + search 注释）→Task 4；注册面→Task 1 Steps 5-6；测试三块→Tasks 1/2/3/4；`test_phase3.py:1539` 不更新的结论→Task 4 Step 8 备注
- **类型一致性**：`_parse_actions -> ReflectorActions`、`_execute_actions(actions: ReflectorActions)`、`MemoryType(x.strip().title())` 模式在 Tasks 3/4 一致
- **顺序依赖**：Task 3 依赖 Task 1（模型）+ Task 2（测试文件骨架）；Task 4 只依赖 main 上已有的 `MemoryType`——1→2→3→4 顺序执行零间隙
