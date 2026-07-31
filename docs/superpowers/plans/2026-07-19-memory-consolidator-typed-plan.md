# MemoryConsolidator 管线类型化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 MemoryConsolidator 的 consolidate → compare → insert 管线从裸 `list[dict]` 改为 Pydantic 类型化：新增 `core/schema/memory.py`，拆分 `_parse_result`，删除 `_validate_actions`。

**Architecture:** 转换边界设在两个 LLM 调用点——`_parse_extraction`（提取输出→`list[ExtractedMemory]`）和 `_parse_compare_actions`（比对输出→`list[CompareAction]` discriminated union）。上游 `_extract_json_array` 保持 `list[dict]`（纯文本解析），下游全链路属性访问。Pydantic `Discriminator` 替代手写 `_validate_actions`。

**Tech Stack:** Python 3.11+, Pydantic v2, pytest

## Global Constraints

- 所有新增类型通过 `core/schema/__init__.py` re-export
- 新增模块必须更新 `core/schema/README.md` 目录树 + 模块速查表
- 测试 marker: `unit` 或 `service`；commit 前跑类选择器或 `-m "not slow"`，最终验证跑 `make test-normal`
- conda 环境: `conda activate Elysia`

---

## File Structure

```
core/schema/memory.py          [CREATE]   6 Pydantic models + 1 discriminated union
core/schema/__init__.py        [MODIFY]   re-export memory types (追加到 mood 行后)
core/schema/README.md          [MODIFY]   目录树 + 模块速查表 (tool.py 行后)
core/consolidator.py           [MODIFY]   新增 3 个模块级函数；_parse_result 删除并入 Task 4 末尾
core/tests/test_phase2.py      [MODIFY]   10 个测试: 改名 + 大小写 + 补齐字段 + 断言语义
core/tests/test_phase3.py      [NO CHANGE] consolidator mock 返回 [] 兼容 list[ExtractedMemory]
```

---

### Task 1: Create `core/schema/memory.py`

**Files:**
- Create: `core/schema/memory.py`

**Interfaces:**
- Produces: `MemoryType`, `ExtractedMemory`, `SkipAction`, `InsertAction`, `UpdateAction`, `MergeAction`, `CompareAction`

- [ ] **Step 1: Write `core/schema/memory.py`**

```python
"""记忆提取类型 — Consolidator LLM 输出的 Pydantic 模型。"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints


class MemoryType(StrEnum):
    """记忆类型枚举。值与 ExtractMemory.j2 / ConsolidationCompare.j2 的大写词表一致。"""
    FACT = "Fact"
    PREFERENCE = "Preference"
    EVENT = "Event"
    PLAN = "Plan"
    NOTE = "Note"


class ExtractedMemory(BaseModel):
    """LLM 提取的单条记忆。ExtractMemory.j2 输出格式。

    poignancy 保留默认值 5——与现状 .get("poignancy", 5) 一致，防止 LLM 偶发性缺失
    字段时整条记忆被丢弃。
    """
    type: MemoryType
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    poignancy: int = Field(default=5, ge=1, le=10)


class SkipAction(BaseModel):
    action: Literal["skip"] = "skip"


class InsertAction(BaseModel):
    action: Literal["insert"] = "insert"
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    type: MemoryType
    poignancy: int = Field(ge=1, le=10)


class UpdateAction(BaseModel):
    """type/poignancy 在执行分支中从不消费，设为可选保留降级兼容。"""
    action: Literal["update"] = "update"
    target_id: str = Field(min_length=1)
    new_content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    type: MemoryType | None = None
    poignancy: int | None = Field(default=None, ge=1, le=10)


class MergeAction(BaseModel):
    """type/poignancy 传入 update_text()（其签名接受 None 表示"不更新此字段"）。
    可选以保留 LTM 的 None-语义。"""
    action: Literal["merge"] = "merge"
    target_id: str = Field(min_length=1)
    merged_content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    type: MemoryType | None = None
    poignancy: int | None = Field(default=None, ge=1, le=10)


CompareAction = Annotated[
    SkipAction | InsertAction | UpdateAction | MergeAction,
    Field(discriminator="action"),
]
```

- [ ] **Step 2: Verify syntax**

```bash
conda run -n Elysia python -c "from core.schema.memory import MemoryType, ExtractedMemory, CompareAction; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add core/schema/memory.py
git commit -m "feat: add core/schema/memory.py — MemoryType, ExtractedMemory, CompareAction"
```

---

### Task 2: Register in `core/schema/__init__.py` and `core/schema/README.md`

**Files:**
- Modify: `core/schema/__init__.py`
- Modify: `core/schema/README.md`

**Interfaces:**
- Consumes: `MemoryType`, `ExtractedMemory`, `SkipAction`, `InsertAction`, `UpdateAction`, `MergeAction`, `CompareAction` from `core.schema.memory`
- Produces: re-export from `core.schema` package

- [ ] **Step 1: Add re-export to `core/schema/__init__.py`**

Append after the last re-export line (after `from core.schema.mood import ...`, line 54), following the file's convention of adding new modules at the end:

```python
from core.schema.memory import (  # noqa: F401
    MemoryType,
    ExtractedMemory,
    SkipAction,
    InsertAction,
    UpdateAction,
    MergeAction,
    CompareAction,
)
```

- [ ] **Step 2: Update `core/schema/README.md` directory tree**

After the line `├── tool.py` (currently describing `ToolCall, ToolCallFunction`), add:

```
├── memory.py   # MemoryType, ExtractedMemory, CompareAction — Consolidator LLM 输出类型
```

- [ ] **Step 3: Update module reference table**

After the row for `tool.py` in the table (`| tool.py | ToolCall, ToolCallFunction — ... |`), add:

```
| `memory.py` | MemoryType, ExtractedMemory, SkipAction, InsertAction, UpdateAction, MergeAction, CompareAction — MemoryConsolidator LLM 输出类型 |
```

- [ ] **Step 4: Verify**

```bash
conda run -n Elysia python -c "from core.schema import MemoryType, ExtractedMemory, CompareAction; print('OK')"
```

Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add core/schema/__init__.py core/schema/README.md
git commit -m "feat: register core.schema.memory in __init__.py and README"
```

---

### Task 3: Add 3 module-level parse functions in `consolidator.py` (incremental — keep `_parse_result` alive)

**Files:**
- Modify: `core/consolidator.py`

**Interfaces:**
- Consumes: `ExtractedMemory`, `CompareAction` from `core.schema.memory`
- Produces: `_extract_json_array(text) -> list[dict]`, `_parse_extraction(text) -> list[ExtractedMemory]`, `_parse_compare_actions(text) -> list[CompareAction]`
- **Does NOT delete `_parse_result` yet** — call sites updated in Task 4

- [ ] **Step 1: Replace current imports (lines 10-13) with final import block**

Lines 10-13 currently read:
```python
import asyncio
import json
import logging
from pathlib import Path
```

Replace with:
```python
import asyncio
import json
import logging
import re
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from core.schema.memory import (
    ExtractedMemory,
    CompareAction,
    MergeAction,
    UpdateAction,
    InsertAction,
)
```

`re` is now at module level (was previously inside `_parse_result` at line 166 — that method gets deleted in Task 4). `MergeAction/UpdateAction/InsertAction` are included now to avoid a cross-commit import gap.

- [ ] **Step 2: Add module-level TypeAdapter + 3 parse functions**

Insert after `_get_compare_prompt()` (after line 56 in current file), before `class MemoryConsolidator:`:

```python
# TypeAdapter 提为模块级单例（Pydantic 官方建议），避免每次解析重复创建
_compare_actions_adapter = TypeAdapter(list[CompareAction])


def _extract_json_array(text: str) -> list[dict]:
    """纯文本→list[dict]。去掉 markdown fence → json.loads → regex fallback。"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:]) if len(lines) > 1 else text
        if text.endswith("```"):
            text = text[:-3]
    try:
        items = json.loads(text)
        if isinstance(items, list):
            return items
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            try:
                items = json.loads(match.group())
                if isinstance(items, list):
                    return items
            except json.JSONDecodeError:
                pass
    return []


def _parse_extraction(text: str) -> list[ExtractedMemory]:
    """解析 LLM 提取输出 (ExtractMemory.j2) → list[ExtractedMemory]。

    逐条 .model_validate()——一个坏元素只跳过该项，不影响同批次其他合规项。
    .model_validate() 确保非 dict 元素统一抛 ValidationError（而非 TypeError）。
    """
    items = _extract_json_array(text)
    result: list[ExtractedMemory] = []
    for item in items:
        try:
            result.append(ExtractedMemory.model_validate(item))
        except ValidationError as e:
            logger.warning("跳过不合规的提取记忆: %s，原始数据: %s", e, item)
    return result


def _parse_compare_actions(text: str) -> list[CompareAction]:
    """解析 LLM 比对输出 (ConsolidationCompare.j2) → list[CompareAction]。

    整批 all-or-nothing 校验。校验失败 raise ValueError——调用方
    _compare_and_resolve() 的 except Exception 捕获后走 insert-all 回退，
    确保一个坏元素不导致静默丢记忆。
    """
    items = _extract_json_array(text)
    try:
        return _compare_actions_adapter.validate_python(items)
    except ValidationError as e:
        raise ValueError(f"比对 actions 校验失败: {e}") from e
```

- [ ] **Step 3: Verify new functions work (old code still alive)**

```bash
conda run -n Elysia python -c "
from core.consolidator import _parse_extraction, _parse_compare_actions
# Test extraction
items = _parse_extraction('[{\"type\": \"Fact\", \"content\": \"hello\", \"poignancy\": 5}]')
assert len(items) == 1
assert items[0].type.value == 'Fact'
assert items[0].content == 'hello'
assert _parse_extraction('[]') == []
# Test compare actions
actions = _parse_compare_actions('[{\"action\": \"skip\"}, {\"action\": \"insert\", \"content\": \"test\", \"type\": \"Fact\", \"poignancy\": 5}]')
from core.schema.memory import SkipAction, InsertAction
assert isinstance(actions[0], SkipAction)
assert isinstance(actions[1], InsertAction)
print('OK')
"
```

Expected: `OK`

- [ ] **Step 4: Confirm existing tests still pass (old `_parse_result` not yet deleted)**

```bash
conda run -n Elysia python -m pytest core/tests/test_phase2.py::TestMemoryConsolidator core/tests/test_phase2.py::TestMemoryConsolidatorEdgeCases core/tests/test_phase2.py::TestConsolidatorThreshold -q --tb=short
```

Expected: all tests PASS (new functions coexist with old `_parse_result`)

- [ ] **Step 5: Commit**

```bash
git add core/consolidator.py
git commit -m "refactor: add _extract_json_array, _parse_extraction, _parse_compare_actions (增量)"
```

---

### Task 4: Rewire consolidator.py call sites + update tests + delete `_parse_result` and `_validate_actions`

**Files:**
- Modify: `core/consolidator.py`
- Modify: `core/tests/test_phase2.py`

**Interfaces:**
- Consumes: `_parse_extraction`, `_parse_compare_actions`, `MergeAction`, `UpdateAction`, `InsertAction`
- Produces: typed `consolidate()`, `flush()`, `_compare_and_resolve()` methods; 10 updated tests

- [ ] **Step 1: Update `_do_consolidate()` — extraction call (line 130)**

```python
# Before
items = self._parse_result(result.text)

# After
items = _parse_extraction(result.text)
```

- [ ] **Step 2: Update `_do_consolidate()` — history append**

The current code (~lines 139-144):
```python
        for item in items:
            self._history.append({
                "type": item.get("type", "?"),
                "content": item.get("content", ""),
                "poignancy": item.get("poignancy", 5),
            })
```

Replace the entire `for item in items:` block (3 lines of append) with:
```python
        for item in items:
            self._history.append(item.model_dump())
```

- [ ] **Step 3: Update `_do_consolidate()`, `consolidate()`, `flush()` return types**

```python
# consolidate() — line 75
# Before: async def consolidate(self, user_message: str, assistant_response: str) -> list[dict]:
# After:
async def consolidate(self, user_message: str, assistant_response: str) -> list[ExtractedMemory]:

# flush() — line 95
# Before: async def flush(self) -> list[dict]:
# After:
async def flush(self) -> list[ExtractedMemory]:

# _do_consolidate() — line 111
# Before: async def _do_consolidate(self) -> list[dict]:
# After:
async def _do_consolidate(self) -> list[ExtractedMemory]:
```

- [ ] **Step 4: Update `_compare_and_resolve()` signature (line 177)**

```python
# Before
async def _compare_and_resolve(self, new_items: list[dict]) -> int:

# After
async def _compare_and_resolve(self, new_items: list[ExtractedMemory]) -> int:
```

- [ ] **Step 5: Update direct insert path**

The current code (~lines 182-190):
```python
            for item in new_items:
                content = item.get("content", "").strip()
                if content:
                    await self._memory.insert(
                        content,
                        memory_type=item.get("type", "note"),
                        poignancy=int(item.get("poignancy", 5)),
                        source="consolidation",
                    )
```

Replace the entire for-loop body with:
```python
            for item in new_items:
                await self._memory.insert(
                    item.content,
                    memory_type=item.type.value,
                    poignancy=item.poignancy,
                    source="consolidation",
                )
```

- [ ] **Step 6: Update Milvus search query_text (~line 196)**

```python
# Before
            query_text=" ".join(item.get("content", "") for item in new_items),

# After
            query_text=" ".join(item.content for item in new_items),
```

- [ ] **Step 7: Update compare prompt serialization**

The current code (~lines 206-211):
```python
            compare_prompt = prompt_template.replace(
                "{new_items}", json.dumps(new_items, ensure_ascii=False, indent=2),
            ).replace(
                "{existing_items}", json.dumps(related_dicts, ensure_ascii=False, indent=2),
            )
```

Replace with:
```python
            compare_prompt = prompt_template.replace(
                "{new_items}", json.dumps(
                    [item.model_dump() for item in new_items],
                    ensure_ascii=False, indent=2,
                ),
            ).replace(
                "{existing_items}", json.dumps(related_dicts, ensure_ascii=False, indent=2),
            )
```

(Only the `{new_items}` half changes — `new_items` is now `list[ExtractedMemory]`. The `{existing_items}` half is unchanged.)

- [ ] **Step 8: Update compare LLM output parsing (~line 216)**

```python
# Before
            actions = self._parse_result(result.text)

# After
            actions = _parse_compare_actions(result.text)
```

- [ ] **Step 9: Delete `_validate_actions` call + preceding comment**

The current code (~lines 218-220):
```python
            # 3. 预校验
            if not self._validate_actions(actions, new_items):
                raise ValueError("Action validation failed")
```

Delete these 3 lines entirely.

- [ ] **Step 10: Update execution branch**

The current code (~lines 222-250):
```python
            inserted = 0
            for action in actions:
                act = action.get("action", "insert")
                if act == "merge":
                    target_id = action.get("target_id", "")
                    merged = action.get("merged_content", "")
                    if target_id and merged:
                        await self._memory.update_text(
                            target_id, merged,
                            poignancy=action.get("poignancy"),
                            memory_type=action.get("type"),
                        )
                elif act == "update":
                    target_id = action.get("target_id", "")
                    new_content = action.get("new_content", "")
                    if target_id and new_content:
                        await self._memory.update_text(target_id, new_content)
                elif act == "insert":
                    content = action.get("content", "").strip()
                    if content:
                        await self._memory.insert(
                            content,
                            memory_type=action.get("type", "note"),
                            poignancy=int(action.get("poignancy", 5)),
                            source="consolidation",
                        )
                        inserted += 1
                # skip — 无操作
```

Replace the entire for-loop with:
```python
            inserted = 0
            for action in actions:
                if isinstance(action, MergeAction):
                    await self._memory.update_text(
                        action.target_id, action.merged_content,
                        poignancy=action.poignancy,
                        memory_type=action.type.value if action.type else None,
                    )
                elif isinstance(action, UpdateAction):
                    await self._memory.update_text(action.target_id, action.new_content)
                elif isinstance(action, InsertAction):
                    await self._memory.insert(
                        action.content,
                        memory_type=action.type.value,
                        poignancy=action.poignancy,
                        source="consolidation",
                    )
                    inserted += 1
                # SkipAction — 无操作
```

- [ ] **Step 11: Update fallback insert**

The current code (~lines 256-266):
```python
            for item in new_items:
                content = item.get("content", "").strip()
                if content:
                    await self._memory.insert(
                        content,
                        memory_type=item.get("type", "note"),
                        poignancy=int(item.get("poignancy", 5)),
                        source="consolidation",
                    )
```

Replace the entire for-loop body with:
```python
            for item in new_items:
                await self._memory.insert(
                    item.content,
                    memory_type=item.type.value,
                    poignancy=item.poignancy,
                    source="consolidation",
                )
```

- [ ] **Step 12: Delete `_parse_result` static method**

Delete the entire `_parse_result` method (the old `@staticmethod` with lines ~150-175).

- [ ] **Step 13: Delete `_validate_actions` static method**

Delete the entire `_validate_actions` method (the old `@staticmethod` with lines ~269-280).

- [ ] **Step 14: Update tests in `core/tests/test_phase2.py`**

Add imports at top of test file:
```python
from core.consolidator import _parse_extraction
from core.schema.memory import MemoryType
```

Then update all 10 test cases:

**`test_consolidate_extracts_facts` (line 276):**
```python
# Before (lines 283-299)
adapter = LLMAdapter(_test_backend=FakeLLMBackend(response=json.dumps([
    {"type": "preference", "content": "喜欢蓝色"},
    {"type": "plan", "content": "明天去爬山"},
])))
# ...
types = {item.get("type") for item in items}
assert "preference" in types
assert "plan" in types

# After
adapter = LLMAdapter(_test_backend=FakeLLMBackend(response=json.dumps([
    {"type": "Preference", "content": "喜欢蓝色", "poignancy": 5},
    {"type": "Plan", "content": "明天去爬山", "poignancy": 5},
])))
# ...
types = {item.type.value for item in items}
assert "Preference" in types
assert "Plan" in types
```

**`test_parse_json` (line 321):**
```python
# Before
items = MemoryConsolidator._parse_result(
    '[{"type": "preference", "content": "likes blue"}]'
)
assert len(items) == 1
assert items[0]["type"] == "preference"

# After
items = _parse_extraction(
    '[{"type": "Preference", "content": "likes blue", "poignancy": 5}]'
)
assert len(items) == 1
assert items[0].type == MemoryType.PREFERENCE
```

**`test_parse_markdown_wrapped` (line 330):**
```python
# Before
items = MemoryConsolidator._parse_result(
    '```json\n[{"type": "fact", "content": "hello"}]\n```'
)
assert len(items) == 1
assert items[0]["content"] == "hello"

# After
items = _parse_extraction(
    '```json\n[{"type": "Fact", "content": "hello", "poignancy": 5}]\n```'
)
assert len(items) == 1
assert items[0].content == "hello"
```

**`test_parse_empty` (line 338):**
```python
# Before
assert MemoryConsolidator._parse_result("") == []
assert MemoryConsolidator._parse_result("not json") == []
assert MemoryConsolidator._parse_result("[]") == []

# After
assert _parse_extraction("") == []
assert _parse_extraction("not json") == []
assert _parse_extraction("[]") == []
```

**`test_parse_json_embedded_in_text` (line 959):**
```python
# Before — fixture "fact" lowercase, subscript access
text = 'prefix text [{"type": "fact", "content": "found"}] suffix text'
items = MemoryConsolidator._parse_result(text)
assert len(items) == 1
assert items[0]["content"] == "found"

# After — case fix + attribute access (poignancy defaults to 5, no need to add)
text = 'prefix text [{"type": "Fact", "content": "found"}] suffix text'
items = _parse_extraction(text)
assert len(items) == 1
assert items[0].content == "found"
```

**`test_parse_multiple_arrays_returns_first` (line 967):** Only rename — `MemoryConsolidator._parse_result(` → `_parse_extraction(`. Fixture unchanged.

**`test_parse_trailing_comma` (line 976):**
```python
# Before — "fact" lowercase
items = MemoryConsolidator._parse_result(
    '[{"type": "fact", "content": "test"}]'
)
assert len(items) == 1

# After — case fix (poignancy defaults to 5)
items = _parse_extraction(
    '[{"type": "Fact", "content": "test"}]'
)
assert len(items) == 1
```

**`test_parse_whitespace_only` (line 984):**
```python
# Before
assert MemoryConsolidator._parse_result("   \n  ") == []
assert MemoryConsolidator._parse_result("```json\n```") == []

# After
assert _parse_extraction("   \n  ") == []
assert _parse_extraction("```json\n```") == []
```

**`test_parse_single_object_not_array` (line 989):** Only rename — `MemoryConsolidator._parse_result(` → `_parse_extraction(`.

**`test_parse_array_with_invalid_items` (line 997):**
```python
# Before
items = MemoryConsolidator._parse_result(
    '[{"type": "fact", "content": "ok"}, {"invalid": "item"}, {"type": "plan"}]'
)
assert len(items) == 3

# After — item 1 valid (type+content, poignancy defaults 5); item 2 no type/content; item 3 no content
items = _parse_extraction(
    '[{"type": "Fact", "content": "ok"}, {"invalid": "item"}, {"type": "Plan"}]'
)
assert len(items) == 1
assert items[0].type == MemoryType.FACT
assert items[0].content == "ok"
```

- [ ] **Step 15: Run tests**

```bash
conda run -n Elysia python -m pytest core/tests/test_phase2.py::TestMemoryConsolidator core/tests/test_phase2.py::TestMemoryConsolidatorEdgeCases core/tests/test_phase2.py::TestConsolidatorThreshold core/tests/test_phase3.py -q --tb=short -m "not slow"
```

Expected: all tests PASS

- [ ] **Step 16: Commit**

```bash
git add core/consolidator.py core/tests/test_phase2.py
git commit -m "refactor: type MemoryConsolidator pipeline — ExtractedMemory + CompareAction, delete _parse_result/_validate_actions"
```

---

### Task 5: Final verification

**Files:**
- No changes — verification only

- [ ] **Step 1: Run `make test-normal` (commit-ready bar)**

```bash
make test-normal
```

Expected: all tests PASS (excludes `@pytest.mark.slow` tests)

- [ ] **Step 2: Verify import consistency**

```bash
conda run -n Elysia python -c "
from core.schema import MemoryType, ExtractedMemory, CompareAction
from core.schema import SkipAction, InsertAction, UpdateAction, MergeAction
from core.consolidator import MemoryConsolidator, _parse_extraction, _parse_compare_actions
print('All imports OK')
"
```

Expected: `All imports OK`
