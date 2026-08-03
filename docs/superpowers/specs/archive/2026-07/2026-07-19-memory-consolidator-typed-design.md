# MemoryConsolidator 管线类型化：消灭裸 `list[dict]`

## Context

MemoryConsolidator 的 consolidate → compare → insert 管线中，5 种 dict shape 全程裸传：

```python
item.get("type", "note")           # 提取输出
action.get("action", "insert")     # 比对输出
item.get("content", "").strip()    # 处处 .get()
```

`core/schema/tool.py` 已有 Pydantic 模型管理的先例。`MemoryEntry` dataclass（`core/memory/long_term.py`）已定义但只在 LTM 内部使用，执行路径从未受益。

**核心矛盾**：两个 LLM 输出（提取 + 比对）都是 `list[dict]`，`_parse_result` 一个函数服务两种不同 schema，`_validate_actions` 手写 28 行 key 检查。

## Goals

1. 新增 `core/schema/memory.py`：定义 `MemoryType` enum、`ExtractedMemory`、`CompareAction` discriminated union
2. 拆分 `_parse_result` → `_parse_extraction(text) → list[ExtractedMemory]` + `_parse_compare_actions(text) → list[CompareAction]`
3. 删除 `_validate_actions()`（Pydantic `Discriminator` 自动校验）
4. 所有 `.get()` 访问 → 属性访问

## Non-Goals

- **不改** `LongTermMemory.insert(text: str, **metadata)` — kwargs 接口够用
- **不改** Shape #2（related_dicts）— `MemoryEntry` 内部序列化，非 LLM 产出
- **不改** `MemoryReflector` — 独立管线，单独处理

---

## Design

### 转换边界

```
LLM #1 (ExtractMemory.j2)
  ↓
_parse_extraction(text)        →  list[ExtractedMemory]    ← ★ 转换点 #1
  ↓
_compare_and_resolve(new_items: list[ExtractedMemory])
  ↓
  LLM #2 (ConsolidationCompare.j2)
  ↓
_parse_compare_actions(text)   →  list[CompareAction]      ← ★ 转换点 #2
  ↓
执行: insert / update_text / merge / skip
```

### 一、`core/schema/memory.py` — 新增

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


# ---- 提取输出 (Shape #1) ----

class ExtractedMemory(BaseModel):
    """LLM 提取的单条记忆。ExtractMemory.j2 输出格式。

    poignancy 保留默认值 5——与现状 .get("poignancy", 5) 一致，防止 LLM 偶发性缺失
    字段时整条记忆被丢弃。
    """
    type: MemoryType
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    poignancy: int = Field(default=5, ge=1, le=10)


# ---- 比对输出 (Shape #3) — discriminated union ----

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

- `MemoryType` 是 `StrEnum`，LLM 产出的 `"Fact"` 直接 `MemoryType("Fact")`；大小写敏感。
- `content` / `new_content` / `merged_content` 用 `StringConstraints(strip_whitespace=True, min_length=1)` 替代旧代码的 `.strip()` + `if content:` 模式——校验时先 strip 再查 min_length，纯空白字符串（如 `" "`）被拒绝。执行分支不再需要 `.strip()` 调用。
- `target_id` 保持 `Field(min_length=1)`（不需要 strip——Milvus ID 不含空白修饰）。
- `poignancy` 用 `Field(ge=1, le=10)` 约束范围（新增校验路径，此前代码中无等效检查）。
- `ExtractedMemory.poignancy` 默认 5（与现状 `.get("poignancy", 5)` 一致）。
- `UpdateAction.type/poignancy` 和 `MergeAction.type/poignancy` 设为可选——前者从不消费；后者传入 `update_text()` 的签名为 `poignancy: int | None = None, memory_type: str | None = None`，`None` 表示保留原值。
- `CompareAction` discriminated union：`TypeAdapter(list[CompareAction]).validate_python(items)` 自动校验 + 分发。

### 二、拆分 `_parse_result` → `_parse_extraction` + `_parse_compare_actions`

**文件**: `core/consolidator.py`

新增 3 个模块级函数，替换原 `MemoryConsolidator._parse_result` 静态方法：

```python
import json
import logging
import re

from pydantic import TypeAdapter, ValidationError

from core.schema.memory import ExtractedMemory, CompareAction

logger = logging.getLogger(__name__)

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

**关键设计决策**：
- `_parse_extraction`：逐条校验跳过——提取阶段一个坏记忆不应影响同批次其他记忆。
- `_parse_compare_actions`：整批 all-or-nothing → raise ValueError → 调用方 except Exception → insert-all 回退。比对输出破坏意味着 LLM 状态异常，最安全的恢复是全部插入新记忆。这保持 `consolidator.py:177-179` 的现有契约："失败时回退：全部 insert"。
- 函数名用 `_parse_compare_actions` 而非 `_parse_actions`——避免与 `memory_reflector.py:220` 的同名函数（返回 `dict` 而非 `list[CompareAction]`）冲突。
- `_extract_json_array` 未复用 `Module/task_system/utils.py:strip_markdown_fence`——consolidator 需要的是"去 fence → json.loads → regex 兜底 → 确保返回 list"一体化逻辑，`strip_markdown_fence` 只做 fence 剥离。拆成两步反而增加 consolidator 内样板代码，收益为零。

### 三、`consolidator.py` — 核心改动

#### import 变更

```python
# 删除
# @staticmethod
# def _parse_result(text: str) -> list[dict]:

# 新增（模块级 import）
from core.schema.memory import (
    ExtractedMemory,
    CompareAction,
    MergeAction,
    UpdateAction,
    InsertAction,
)
# _parse_extraction / _parse_compare_actions / _extract_json_array
# 是同一模块内的同级函数，MemoryConsolidator 类内直接调用，无需 self. 前缀。
```

#### `_do_consolidate()` — extraction 调用（line 130）

```python
# Before
items = self._parse_result(result.text)

# After
items = _parse_extraction(result.text)
```

#### `_do_consolidate()` — history 追加（line 139-144）

```python
# Before
self._history.append({
    "type": item.get("type", "?"),
    "content": item.get("content", ""),
    "poignancy": item.get("poignancy", 5),
})

# After — item 是 ExtractedMemory，model_dump() 产出 {"type": "Fact", "content": "...", "poignancy": 5}
self._history.append(item.model_dump())
```

**注意**：`self._history` 类型保持 `list[dict]`（仅 debug 面板消费），只是写入方式从手动 `.get()` 拼 dict 改为 `item.model_dump()`。

#### `_do_consolidate()` — 返回值（line 148）

```python
# 返回类型变更
async def consolidate(self, ...) -> list[ExtractedMemory]:
async def flush(self, ...) -> list[ExtractedMemory]:
```

#### `_compare_and_resolve()` — 签名（line 177）

```python
# Before
async def _compare_and_resolve(self, new_items: list[dict]) -> int:

# After
async def _compare_and_resolve(self, new_items: list[ExtractedMemory]) -> int:
```

#### `_compare_and_resolve()` — 直接 insert 路径（line 182-190）

```python
# Before
for item in new_items:
    content = item.get("content", "").strip()
    if content:
        await self._memory.insert(
            content,
            memory_type=item.get("type", "note"),
            poignancy=int(item.get("poignancy", 5)),
            source="consolidation",
        )

# After
for item in new_items:
    await self._memory.insert(
        item.content,
        memory_type=item.type.value,
        poignancy=item.poignancy,
        source="consolidation",
    )
```

`item.content` 已由 `StringConstraints(strip_whitespace=True, min_length=1)` 保证非空，无需 `.strip()` 和 `if content:` 守卫。

#### `_compare_and_resolve()` — Milvus 搜索 query_text（line 196） 🔴

```python
# Before
related = await self._memory.search(
    query_text=" ".join(item.get("content", "") for item in new_items),
    top_k=10,
)

# After
related = await self._memory.search(
    query_text=" ".join(item.content for item in new_items),
    top_k=10,
)
```

**审查发现**：原 spec 初版遗漏此行。`new_items` 变为 `list[ExtractedMemory]` 后 `item.get("content", "")` 抛 `AttributeError` → 比对路径静默死亡 → 每次走 insert-all 回退。

#### `_compare_and_resolve()` — 比对 new_items 序列化（line 208）

```python
# Before
compare_prompt = prompt_template.replace(
    "{new_items}", json.dumps(new_items, ensure_ascii=False, indent=2),
)

# After — new_items 是 list[ExtractedMemory]，需要先 model_dump()
compare_prompt = prompt_template.replace(
    "{new_items}", json.dumps(
        [item.model_dump() for item in new_items],
        ensure_ascii=False, indent=2,
    ),
)
```

#### `_compare_and_resolve()` — 比对 LLM 输出解析（line 216）

```python
# Before
actions = self._parse_result(result.text)

# After
actions = _parse_compare_actions(result.text)
```

**注意**：`_parse_compare_actions` 校验失败会 raise `ValueError`（而非返回 `[]`），被外层 `except Exception`（line 254）捕获 → 走 insert-all 回退。保持现有契约："失败时回退：全部 insert"。

#### `_compare_and_resolve()` — 删除 `_validate_actions` 调用（line 219）

```python
# Before
if not self._validate_actions(actions, new_items):
    raise ValueError("Action validation failed")

# After — Pydantic Discriminator 已在 _parse_compare_actions 中完成校验
# 删除此段
```

#### `_compare_and_resolve()` — 执行分支（line 224-250）

```python
# Before
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

# After — isinstance 分发，StringConstraints 保证必填字段非空，无需守卫
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

**关键变化**：
- `MergeAction`: `poignancy=action.poignancy`（可为 None，`update_text` 默认 None 保留原值）；`memory_type` 用 `.value if action.type else None`（可选字段）。
- `UpdateAction`: 只传 `target_id` + `new_content`，不传 type/poignancy（执行分支从未消费这些字段）。
- `InsertAction`: `content` 已由 `StringConstraints` strip + 校验非空，无需 `.strip()` 和守卫。
- 删除所有 `if xxx:` 守卫——Pydantic 已保证必填字段非空。

#### `_compare_and_resolve()` — fallback insert（line 258-266）

```python
# Before
for item in new_items:
    content = item.get("content", "").strip()
    if content:
        await self._memory.insert(
            content,
            memory_type=item.get("type", "note"),
            poignancy=int(item.get("poignancy", 5)),
            source="consolidation",
        )

# After
for item in new_items:
    await self._memory.insert(
        item.content,
        memory_type=item.type.value,
        poignancy=item.poignancy,
        source="consolidation",
    )
```

#### `_validate_actions()` — 删除

```python
# Before (line 269-280) — 28 行手写校验，删除
@staticmethod
def _validate_actions(actions: list[dict], new_items: list[dict]) -> bool:
    ...

# Pydantic Discriminator 自动完成：action 字段白名单 + 各子类型必填字段 + 类型约束
```

---

## 注册面

### `core/schema/__init__.py`

新增 memory 子模块 re-export（与 tool/message/mood 等 10 个现有子模块对齐）：

```python
from .memory import (
    MemoryType,
    ExtractedMemory,
    SkipAction,
    InsertAction,
    UpdateAction,
    MergeAction,
    CompareAction,
)
```

### `core/schema/README.md`

- 目录树新增 `memory.py`
- 模块速查表新增一行：`memory.py` — 记忆提取/比对类型（MemoryConsolidator LLM 输出）

CLAUDE.md 将 `docs/readme-conventions.md`（"新模块……必须按此规范编写或更新 README"）列为绑定约束。

---

## 测试影响

### `core/tests/test_phase2.py`

#### 函数名全局替换

`_parse_result` → `_parse_extraction` 或 `_parse_compare_actions`（按语义）。

#### LLM 输出 fixture 大小写修正

`MemoryType` 是 `StrEnum`（大小写敏感），值必须与 prompt 词表一致：
- `"preference"` → `"Preference"`（大写 P）
- `"plan"` → `"Plan"`（大写 P）
- `"fact"` → `"Fact"`（大写 F）
- `"event"` → `"Event"`（大写 E）

受影响测试：

**`test_consolidate_extracts_facts`（line 276）**：
```python
# Before — FakeLLM 喂小写 + 无 poignancy
adapter = LLMAdapter(_test_backend=FakeLLMBackend(response=json.dumps([
    {"type": "preference", "content": "喜欢蓝色"},
    {"type": "plan", "content": "明天去爬山"},
])))

# After — 匹配 MemoryType 枚举值（poignancy 有默认值 5，不影响解析但显式写出更清晰）
adapter = LLMAdapter(_test_backend=FakeLLMBackend(response=json.dumps([
    {"type": "Preference", "content": "喜欢蓝色", "poignancy": 5},
    {"type": "Plan", "content": "明天去爬山", "poignancy": 5},
])))

# 断言从 item.get() 改为属性访问
types = {item.type.value for item in items}
assert "Preference" in types   # MemoryType.PREFERENCE.value == "Preference"
assert "Plan" in types
```

**`test_parse_json`（line 321）**：
```python
# Before
items = MemoryConsolidator._parse_result(
    '[{"type": "preference", "content": "likes blue"}]'
)
assert items[0]["type"] == "preference"

# After — 补齐 poignancy + 修正大小写 + 属性访问
items = _parse_extraction(
    '[{"type": "Preference", "content": "likes blue", "poignancy": 5}]'
)
assert items[0].type == MemoryType.PREFERENCE
```

**`test_parse_markdown_wrapped`（line 330）**：同理，补齐 `poignancy`。

**`test_parse_empty`（line 338）**：
```python
# Before
assert MemoryConsolidator._parse_result("") == []
assert MemoryConsolidator._parse_result("not json") == []
assert MemoryConsolidator._parse_result("[]") == []

# After — 函数名变更
assert _parse_extraction("") == []
assert _parse_extraction("not json") == []
assert _parse_extraction("[]") == []
```

**`test_parse_json_embedded_in_text`（line 959）**：函数名 + 大小写修正（fixture `"fact"`→`"Fact"`）+ 断言 `items[0]["content"]`→`items[0].content`（BaseModel 不支持下标访问）。poignancy 有默认值 5，不补也能解析。

**5 个漏掉的 `_parse_result` 用例**：
- `test_parse_multiple_arrays_returns_first` / `test_parse_whitespace_only` / `test_parse_single_object_not_array`：仅改名（走不到逐条校验）。
- `test_parse_trailing_comma`（L980）：改名 **+** 大小写修正（fixture `"type": "fact"`→`"Fact"`），否则枚举校验丢项 → `len == 1` 变 `len == 0`。
- `test_parse_array_with_invalid_items`：需要重写（见下）。

其中 `test_parse_array_with_invalid_items` 需要重写——输入 `[{"type": "Fact", "content": "ok"}, {"invalid": "item"}, {"type": "Plan"}]`：
- item 1: `type` + `content` 合规，`poignancy` 默认 5 → **通过**
- item 2: 缺 `type` + `content` → 跳过
- item 3: `type` 合规但缺 `content`（必填字段无默认值）→ 跳过
- 预期 `len == 1`（原 dict 路径透传 3 条，新语义只保留合规的 1 条）

```python
# After — fixture 大小写修正 + 断言改为 len == 1
items = _parse_extraction(
    '[{"type": "Fact", "content": "ok"}, {"invalid": "item"}, {"type": "Plan"}]'
)
assert len(items) == 1
assert items[0].type == MemoryType.FACT
assert items[0].content == "ok"
```

#### `test_flush_consolidates_below_threshold`（line 345）

FakeLLM 返回的 JSON 已包含 `poignancy` + 大写 type，无需改。

### `core/tests/test_phase3.py`

#### `test_after_response_with_consolidator`（line 230）

检查 consolidator mock 中是否有 `.get()` 调用需更新。

---

## 不改的文件清单

| 文件 | 原因 |
|------|------|
| `core/memory/long_term.py` | `insert(text, **metadata)` 签名不变 |
| `core/memory/memory_reflector.py` | 独立管线，单独处理；`_parse_actions` 函数名不冲突（consolidator 用 `_parse_compare_actions`） |
| `core/memory/session.py` | 不相关 |
| `core/schema/memory_reflector.py` | `MemoryReflectorState` 不相关 |
| `prompt/ExtractMemory.j2` | LLM 输出格式不变 |
| `prompt/ConsolidationCompare.j2` | LLM 输出格式不变 |
| `Module/database/vectordb/` | 不相关 |

---

## 收益

1. **删 28 行 `_validate_actions`**：Pydantic `Discriminator` 自动校验 action 白名单 + 各子类型必填字段 + 类型约束
2. **~25 处 `.get()` 调用 → 属性访问**：IDE 补全、拼写错误编译期捕获；`query_text` 的 `item.get("content", "")` 一并修正
3. **`_parse_result` 拆分**：提取和比对各自独立函数，返回类型明确，函数名不冲突（`_parse_compare_actions` vs memory_reflector 的 `_parse_actions`）
4. **`CompareAction` discriminated union**：`isinstance(action, MergeAction)` 自动推断分支内字段
5. **`poignancy` 范围约束**：`Field(ge=1, le=10)` 新增校验（此前代码中无等效检查）。降级兼容：`ExtractedMemory.poignancy` 默认 5（与现状 `.get("poignancy", 5)` 一致），`UpdateAction/MergeAction` 中可选（`None`）
6. **`StringConstraints(strip_whitespace=True, min_length=1)`**：校验时先 strip 再查长度，消除 `if content:` + `.strip()` 守卫——Pydantic 保证必填字段非空且无纯空白
7. **错误处理保留现有契约**：`_parse_compare_actions` raise → `except Exception` → insert-all 回退；`_parse_extraction` 逐个跳过
