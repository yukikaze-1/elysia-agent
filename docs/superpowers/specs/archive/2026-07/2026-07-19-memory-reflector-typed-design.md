# MemoryReflector 管线类型化 + 写入端词表统一 + 数据注入修复

## Context

MemoryReflector 的 LLM 输出是嵌套 dict `{merges, updates, deletes}`，经 `_parse_actions()` 裸传，`_validate_actions()` 手写 12 行校验（keys/isinstance/上限 5/5/3），`_execute_actions()` 全程 `.get()` 访问。

本会话已完成同类重构：tool call 管线（`ToolCall`）、Consolidator 管线（`ExtractedMemory` + `CompareAction`）。`core/schema/memory.py` 已有 `MemoryType` 枚举可复用。

**调查发现的事实**（brainstorm + review 阶段核实）：

1. 🔴 **`prompt/MemoryReflector.j2`（239 行）没有任何数据注入点**——全文无 `{memories}`/`{last_summary}` 字面占位符、无 Jinja2 占位符（双重 grep + 全文阅读确认；占位符只存在于文件缺失时的 fallback prompt）。`_review_group` 的 `.replace("{memories}", ...)` 是空操作，**生产环境 LLM 只收到格式说明、零条实际记忆**——按 prompt 自身的保守规则（"不确定时保留原样"），合规 LLM 永远返回三个空数组，反思器自上线以来永久静默空转。存量 bug，本次修复。
2. **Milvus 库为空**（row_count: 0）——不存在存量数据兼容问题
3. **四个 memory_type 写入面词表不一致**（当前代码，非历史遗留）：
   - Consolidator（已重构）→ TitleCase `"Fact"`
   - `save_memory` 工具 → 小写 `{"fact", "plan", "preference", "note"}`（类属性 `_VALID_TYPES`），**缺 `event`**
   - `server.py` debug API → 无校验，默认小写 `"note"`
   - Reflector 自身 merge 的 `m.get("type")` 裸透传 → 本次类型化顺带修复
4. **读取端也有大小写耦合**：`debug.html` LTM 过滤下拉的 option 值是小写（`value="fact"`），`server.py:206` 大小写敏感精确匹配——写入端统一 TitleCase 后过滤将永远 0 行；debug 添加记忆下拉同样缺 `event`
5. **MemoryReflector 无任何专属测试**（`core/personality/tests/test_reflector.py` 是 CharacterReflector 的）

## Goals

1. 🔴 **修复 j2 数据注入缺失**：给 `prompt/MemoryReflector.j2` 追加数据注入段（`{memories}` + `{last_summary}` 占位符），让 `_review_group` 现有的 `.replace()` 机制真正生效
2. `core/schema/memory_reflector.py` 追加 `MergeItem` / `UpdateItem` / `DeleteItem` / `ReflectorActions` 模型
3. `_parse_actions()` 返回 `ReflectorActions`（校验失败 raise → 现有 per-group except 跳组）
4. 删除 `_validate_actions()`（caps → `Field(max_length)`，结构 → 模型）
5. `_execute_actions()` 属性访问 + merge 的 `update_text` 返回值守卫（防幻觉 id 导致 mark_absorbed 隐藏真实条目）
6. 写入端词表统一：`save_memory` 插件 + `server.py` + `long_term.py` 三处小写默认值
7. 读取端同步：`debug.html` 两个下拉（过滤 TitleCase 化、添加下拉补 `event`）
8. 补齐 MemoryReflector 测试覆盖（模型 + 管线 + 注入断言）

## Non-Goals

- **不改** j2 的输出格式要求与判断规则——`reason` 字段保留（迫使 LLM 论证删除）；本次只追加数据注入段
- **不改** `_cluster_entries` / KMeans 逻辑
- **不改** `MemoryReflectorState` — 已有模型
- **不改** LTM `insert(**metadata)` 的 passthrough 语义 — 通用存储层不做词表归一化，约束是入口的职责
- **不改** LTM `search(memory_type=)` 的精确匹配比较器（[long_term.py:527-528](core/memory/long_term.py#L527)）— 当前零生产调用方（休眠参数），但它是词表契约的一部分：在该行加一句注释「调用方需传 TitleCase（如 "Fact"）」，避免未来调用者踩空
- **不改** 测试中 ~15 处直接 `insert(type="fact")` 的小写调用 — 测试内部数据，与生产库无关
- **不改** Consolidator — 上轮已完成

---

## Design

### 数据流（修复后）

```
LTM.get_all() → MemoryEntry
  ↓ 序列化 [{id, type, content, poignancy, timestamp}]（不变）
_review_group: prompt.replace("{memories}", json) ← ★ j2 修复后真正注入
  ↓
LLM (MemoryReflector.j2)
  ↓
_parse_actions(text) → ReflectorActions     ← ★ 转换点（原返回 dict）
  ↓ 校验失败 raise ValueError → per-group except → 跳组
_execute_actions(actions: ReflectorActions) → merge/update/delete 属性访问
```

### 零、`prompt/MemoryReflector.j2` — 追加数据注入段 🔴

文件末尾（"JSON 中不得使用尾随逗号。"之后）追加：

```
以下是本组待审查的记忆数据：

{memories}

上次运行的摘要（仅供参考，可能为空）：

{last_summary}
```

- 占位符用 `{memories}` / `{last_summary}` 字面文本——与 `_review_group` 现有 `.replace()` 机制（[memory_reflector.py:208-212](core/memory/memory_reflector.py#L208)）精确匹配，代码零改动
- 数据放末尾：239 行规则说明在前、数据在后，符合"指令→数据"的 prompt 惯例
- fallback prompt（j2 文件缺失时）已含占位符，不改

### 一、`core/schema/memory_reflector.py` — 追加模型

现有 `MemoryReflectorState` 保持不动，追加：

```python
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from .memory import MemoryType


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

要点：
- `reason` 字段**不定义**——Pydantic v2 默认忽略 extra 字段（代码从不读它）
- `ids: Field(min_length=2)` 取代执行处 `len(ids) >= 2` 守卫
- `MergeItem.type: MemoryType | None`——严格枚举，与 Consolidator `MergeAction` 一致；None = `update_text` 保留原值。原 `m.get("type")` 裸透传（第 4 个写入面）由此修复
- **`{}` / 部分键接受是相对现状的放宽**：原 `_validate_actions` 要求三键齐全（缺键 → warning + 跳组），j2 也要求干净组输出三个空数组；新模型对缺键取默认空列表。净效果同为零操作，且对 LLM 省略空数组的容错更好——如实标注，非"与现状一致"

### 二、`core/memory/memory_reflector.py` — 核心改动

#### import（L19 附近）

```python
# Before
from core.schema.memory_reflector import MemoryReflectorState

# After
from pydantic import ValidationError

from core.schema.memory_reflector import MemoryReflectorState, ReflectorActions
```

#### `_parse_actions`（L219-237）

```python
# Before
@staticmethod
def _parse_actions(text: str) -> dict:
    """解析 LLM 输出为 actions dict。"""
    text = text.strip()
    if text.startswith("```"):
        ...  # fence 剥离
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
        ...  # fence 剥离（不变）
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

- **非 dict 的合法 JSON**（`"null"` / `"[1,2]"`）走 `model_validate` 路径 → ValidationError → 错误消息准确（"Input should be a valid dictionary"），不与"无法解析"混淆
- fence 剥离**不复用** Consolidator 的 `_extract_json_array`——那边是数组提取（`\[.*\]` + 返回 `list`），这边是对象提取（`\{.*\}` + 返回 `dict`），形状不同不强行抽象

#### `_review_group`（L201-217）

```python
# 返回类型注解 + docstring 更新
async def _review_group(self, group) -> ReflectorActions:
    """LLM 审查一组记忆，返回 ReflectorActions。"""
```

`.replace("{memories}", ...)` 机制不变（j2 修复后生效）。

#### `_validate_actions`（L239-251）— 删除

12 行手写校验删除。`_do_reflect` 中的调用点一并删除：

```python
# Before（L126-132）
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

#### `_execute_actions`（L253-293）

```python
# Before
async def _execute_actions(self, actions: dict) -> tuple[int, int, int]:
    merges = updates = deletes = 0
    for m in actions.get("merges", []):
        ids = m.get("ids", [])
        merged = m.get("merged_content", "")
        if len(ids) >= 2 and merged:
            try:
                await self._memory.update_text(
                    ids[0], merged,
                    poignancy=m.get("poignancy"),
                    memory_type=m.get("type"),
                )
                for absorbed_id in ids[1:]:
                    self._memory.mark_absorbed(absorbed_id)
                merges += 1
            except Exception:
                logger.debug("MemoryReflector merge failed for %s", ids[0], exc_info=True)
    for u in actions.get("updates", []):
        uid = u.get("id", "")
        new_content = u.get("new_content", "")
        if uid and new_content:
            try:
                await self._memory.update_text(uid, new_content)
                updates += 1
            except Exception:
                logger.debug("MemoryReflector update failed for %s", uid, exc_info=True)
    for d in actions.get("deletes", []):
        did = d.get("id", "")
        if did:
            try:
                await self._memory.delete(did)
                deletes += 1
            except Exception:
                logger.debug("MemoryReflector delete failed for %s", did, exc_info=True)
    return merges, updates, deletes

# After — 属性访问；数据守卫删除（模型保证）；per-item IO 容错保留；
#          update_text/delete 返回 bool，False 时不计数、merge 不 mark_absorbed
async def _execute_actions(self, actions: ReflectorActions) -> tuple[int, int, int]:
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

关键变化：
- `memory_type=m.type.value if m.type else None`——从枚举取值，None 语义不变
- **新增 `update_text` 返回值守卫**（原代码忽略返回值）：merge 目标 id 是 LLM 幻觉时 `update_text` 返回 False 不抛异常，原代码会继续 `mark_absorbed(ids[1:])` 把真实条目隐藏掉而合并内容从未写入——真实数据丢失路径，本次堵住
- update/delete 同样按返回值计数（准确性）

#### 错误语义（诚实标注：这是变化，非"与现状一致"）

**旧语义是逐项过滤**：坏 item（单 id merge、空 content）被守卫静默跳过，同组其余 item 照常执行；`_validate_actions` 从不检查 item 内部。
**新语义是 all-or-nothing per group**：一个坏 item → 整组 ValidationError → 零操作，50 轮 / 30 条新记忆后下轮重试。

变化的正当性：旧逐项语义存在**耦合危险**——merge 被守卫跳过时，其被吸收 id 若同时出现在同组 `deletes` 里，会被删掉而合并内容从未写入（真实数据丢失）。all-or-nothing 把"组"作为一致性单元，恰好防住这类半执行状态。Reflector 是周期性维护任务，整组推迟到下轮的代价可接受。

### 三、写入端 + 读取端词表统一

#### `core/tools/memory/plugin.py`（save_memory 插件）

```python
# Before
class SaveMemoryParams(...):
    type: str = Field(
        default="fact",
        description="记忆类型: fact(事实), plan(计划), preference(偏好), note(笔记)",
    )

class MemorySavePlugin(ToolPlugin):
    ...
    _VALID_TYPES = {"fact", "plan", "preference", "note"}   # 类属性

    async def execute(self, ...):
        if params.type not in self._VALID_TYPES:
            return f"无效的记忆类型 '{params.type}'，允许: {', '.join(sorted(self._VALID_TYPES))}"
        ...
        await memory.insert(content, type=params.type)
        ...
        return f"已保存 {params.type} 类型记忆: {content[:200]}"

# After — 大小写不敏感接受，TitleCase 存储，词表源自枚举（自动含 Event）
from core.schema.memory import MemoryType

class SaveMemoryParams(...):
    type: str = Field(
        default="fact",
        description="记忆类型: fact(事实), preference(偏好), event(事件), plan(计划), note(笔记)",
    )

class MemorySavePlugin(ToolPlugin):
    ...
    # _VALID_TYPES 类属性删除

    async def execute(self, ...):
        try:
            mem_type = MemoryType(params.type.strip().title())
        except ValueError:
            allowed = ", ".join(t.value for t in MemoryType)
            return f"无效的记忆类型 '{params.type}'，允许: {allowed}"
        ...
        await memory.insert(content, type=mem_type.value)
        ...
        return f"已保存 {mem_type.value} 类型记忆: {content[:200]}"
```

- `params.type` 保持 `str`（LLM 传小写习惯不变，边界归一化）；description 补 `event`
- `config.yaml` 无类型枚举（已核实）；plugin README 无词表（已核实）——均无需同步

#### `core/input/server.py`（debug API `ltm_insert_handler`，L225-240）

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

# After — 归一化移到 try 外（否则枚举 ValueError 被吞成笼统"无效的请求体"）
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

`server.py` 新增 `from core.schema.memory import MemoryType`。

#### `core/memory/long_term.py` — 3 处小写默认值

| 行 | Before | After |
|----|--------|-------|
| L27 | `memory_type: str = "note"` | `memory_type: str = "Note"` |
| L109 | `memory_type=row.get("memory_type", "note")` | `memory_type=row.get("memory_type", "Note")` |
| L216 | `meta.pop("memory_type", meta.pop("type", "note"))` | `meta.pop("memory_type", meta.pop("type", "Note"))` |

LTM 的 `insert(**metadata)` **保持 passthrough**——显式传入的值原样存储。

#### `static/debug.html` — 两个下拉（读取端）

**LTM 过滤下拉（L1366-1372）**：option 值小写而 `server.py:206` 大小写敏感精确匹配——写入端 TitleCase 后过滤将永远 0 行，与空库不可区分。**按内容替换下面 5 行，保留首行 `<option value="">全部</option>` 不动**：

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

**添加记忆下拉（L242-244）**：缺 `event`（与插件同一缺陷的第四个表面），value 同步 TitleCase（服务端归一化后大小写均可，但与过滤下拉保持一致）：

```html
<!-- Before -->
<option value="fact">fact</option><option value="plan">plan</option>
<option value="preference">preference</option><option value="note">note</option>

<!-- After -->
<option value="Fact">Fact</option><option value="Preference">Preference</option>
<option value="Event">Event</option><option value="Plan">Plan</option>
<option value="Note">Note</option>
```

---

## 注册面

### `core/schema/__init__.py`

```python
# Before
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

### `core/schema/README.md`

- 目录树 `memory_reflector.py` 行描述更新：`MemoryReflectorState + ReflectorActions（状态 + LLM 输出类型）`
- 模块速查表同步

---

## 测试

### 新增 `core/schema/tests/test_memory_reflector.py`（`@pytest.mark.unit`）

参照 `core/schema/tests/test_memory.py` 风格：

- `ReflectorActions.model_validate({})` → 三个空列表（合法）
- 部分键 `{"merges": []}` → 合法
- 完整合法输入 → 正确解析，`MergeItem.type` 为 `MemoryType`
- caps：6 merges → ValidationError；6 updates → ValidationError；4 deletes → ValidationError
- `MergeItem.ids` 单元素 → ValidationError
- 空 `merged_content` / 空 `new_content` / 空 `id` → ValidationError
- `reason` extra 字段被忽略（updates/deletes 带 reason 正常解析）
- `MergeItem.type` 小写 `"fact"` → ValidationError（严格枚举）；缺省 → None
- `poignancy` 越界（0/11）→ ValidationError

### 新增 MemoryReflector 管线测试 `core/memory/tests/test_memory_reflector.py`（`@pytest.mark.service`）

与 `test_long_term.py` / `test_session.py` 同目录（均有 `__init__.py`，与 schema 侧同名文件不冲突）：

- 🔴 **数据注入断言**：`_review_group` 构建的 prompt 包含序列化条目（entry id / text 出现在发给 LLM 的消息中）——防止 j2 占位符再次丢失导致静默空转（mock LLM 抓不到，此断言检查 LLM 收到的输入）
- `_parse_actions`：合法 JSON → ReflectorActions；fence 包裹 → 正常剥离；纯文本 → ValueError；`"null"` / `"[1,2]"`（合法 JSON 非 dict）→ ValueError（走 ValidationError 路径）；schema 非法（6 merges）→ ValueError
- `_execute_actions`（AsyncMock LTM stub）：
  - merge → `update_text(ids[0], merged_content, poignancy=..., memory_type="Fact")` + `mark_absorbed(ids[1:])` + 计数
  - merge 带 `type=None` → `memory_type=None` 传递
  - **merge 目标不存在（`update_text` 返回 False）→ 不 mark_absorbed、不计数、warning 日志**
  - **mark_absorbed 中途抛异常 → 该 merge 不计数，后续 item 继续（部分状态可观测）**
  - update → `update_text(id, new_content)` 返回 True 计数 / False 不计数
  - delete → `delete(id)` 返回 True 计数 / False 不计数
  - update_text 抛异常 → 该项跳过，其余继续执行（IO 容错）
- `_do_reflect`：坏组（LLM 返回非法输出）跳过，后续组正常执行；触发条件与计数器复位不变

### 更新 `core/tools/memory/test_memory.py`（3 个，含两处精确断言）

- `test_invalid_memory_type`（L112）：断言错误消息含 TitleCase 词表
- `test_save_fact`（L135）：两行精确断言需更新——
  ```python
  # Before（L142-143）
  assert result == "已保存 fact 类型记忆: 用户叫张三"
  mock_memory.insert.assert_called_once_with("用户叫张三", type="fact")
  # After
  assert result == "已保存 Fact 类型记忆: 用户叫张三"
  mock_memory.insert.assert_called_once_with("用户叫张三", type="Fact")
  ```
- `test_all_valid_types_accepted`（L161）：迭代 5 个小写词（含新增 `event`）全部接受；L171 的 `assert mem_type in result` 需改为 TitleCase 比对（`mem_type.title() in result`），否则小写词在 TitleCase 消息中匹配失败

### server.py debug API 测试 — 已有，改后仍过

`core/tests/test_phase3.py:1539-1548` 已覆盖 `ltm_insert_handler`（POST `memory_type: "fact"` 小写）——归一化后小写被接受并存为 `"Fact"`，该测试断言只查 text 不查 type，改后仍过，无需更新。

---

## 不改的文件清单

| 文件 | 原因 |
|------|------|
| `prompt/MemoryReflector.j2` 的规则部分 | 输出格式与判断规则不变，`reason` 保留；只追加数据注入段 |
| `core/consolidator.py` | 上轮已完成 |
| `core/schema/memory.py` | 只 import `MemoryType`，不改 |
| `core/memory/session.py` | 不相关 |
| 测试中 ~15 处 `insert(type="fact")` 小写 | 测试内部数据，LTM passthrough 语义下无影响 |

---

## 收益

1. 🔴 **修复生产静默空转**：j2 数据注入段让反思器首次真正工作（存量 bug，LLM 此前从未收到过记忆数据）
2. **删 12 行 `_validate_actions`**：caps → `Field(max_length=5/5/3)`，结构校验 → Pydantic
3. **10 处 `.get()` → 属性访问**：`m.ids` / `u.new_content` / `d.id` 等，IDE 补全 + 拼写安全
4. **数据守卫集中到模型**：`len(ids)>=2` → `min_length=2`；空串守卫 → `StringConstraints`
5. **堵住 merge 幻觉数据丢失路径**：`update_text` 返回 False 时不再 mark_absorbed 隐藏真实条目
6. **全库词表统一（4 写入面 + 2 读取面）**：Consolidator / save_memory（补 `event`）/ debug API / Reflector merge 透传全部 TitleCase；debug.html 过滤修复（否则统一后过滤永远 0 行）
7. **MemoryReflector 从零测试到全覆盖**：模型 + parse + execute + 坏组隔离 + 注入断言
8. **错误语义 all-or-nothing per group**（诚实标注为变化）：防住旧逐项语义下 merge 跳过 + delete 同组耦合的数据丢失
