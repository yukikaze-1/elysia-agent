# MemoryEntry.memory_type 对齐 MemoryType StrEnum — 设计文档

> **来源**：getattr 审计（2026-08-08）发现 `core/memory/long_term.py` 的 `MemoryEntry.memory_type`
> 用裸 `str = "Note"`，而 schema 层已定义 `MemoryType` StrEnum（`core/schema/memory.py:9`，
> 值为 `Fact/Preference/Event/Plan/Note` PascalCase）却未用于该字段——数据结构未对齐。
> Consolidator / Reflector 路径已用 `MemoryType`（`ExtractedMemory.type` / `InsertAction.type`）
> 并在 insert 边界做 `.value`，唯 `MemoryEntry` 停留在裸 str。

**Goal**：把 `MemoryEntry.memory_type` 字段类型从 `str` 对齐为 `MemoryType` StrEnum，所有外部
（不可信）字符串入口经容错 coerce 归一化，未知值不炸、大小写归一，顺带修复 debug LTM 过滤的
大小写不匹配 bug。

**Architecture**：单层改动，两个新增 + 边界 coerce：

1. `MemoryType.try_coerce(value) -> MemoryType | None`（schema 层）：精确匹配 → 大小写不敏感
   （`.title()`）匹配 → 未知/None/非 str 返回 `None`（不抛）。`MemoryType.coerce(value) = try_coerce(value) or NOTE`。
   - **存储/构造边界用 `coerce`**（未知 → NOTE 兜底，不崩）；
   - **过滤比较边界用 `try_coerce`**（未知 → None → 不命中，保留"无效过滤值返回空"的现有语义，
     不误把 bogus 过滤成"全部 NOTE 记忆"）。
2. `MemoryEntry.memory_type: MemoryType = MemoryType.NOTE` + `__post_init__` 里 coerce：
   所有构造点（Milvus 行解析 / insert metadata / 测试直构）自动归一化，无需逐点改。
3. 越过 `__post_init__` 的直接属性赋值（`update_text` 的 `e.memory_type = ...`）在赋值边界显式 coerce；
   过滤比较（`search` / debug 列表 `e.memory_type == filter`）用 `try_coerce`。

**Tech Stack**：Python 3.12 dataclass + `enum.StrEnum`（Python 3.11+ 特性，项目已用）。

## Global Constraints

- 生产代码函数参数必须类型注解（`make lint` 强制 ANN001/002/003）。
- 禁相对导入，一律绝对导入（`from core.schema.memory import MemoryType`）。
- 代码注释/README 用中文；中英混合（中文嵌英文技术术语）可接受。
- `MemoryType` 成员值不变：`FACT="Fact" PREFERENCE="Preference" EVENT="Event" PLAN="Plan" NOTE="Note"`。
- 未知值策略（用户已拍板）：**存储/构造 → NOTE 兜底不抛**；**过滤 → 不命中（返回空）**，与现状一致。
- 测试用 conda 环境 `Elysia` 运行。

---

## 现状分析（触及点全量枚举）

### 数据来源（不可信字符串入口，coerce 必须覆盖）

| # | 来源 | 值形态 | 位置 |
|---|------|--------|------|
| 1 | Milvus 行解析 | 存储字符串（PascalCase，可能有脏数据） | `long_term.py:123` `row.get("memory_type", "Note")` |
| 2 | insert metadata | 任意 str（`memory_type` 或别名 `type`） | `long_term.py:164` `meta.pop("memory_type", meta.pop("type", "Note"))` |
| 3 | debug LTM insert body | lowercase 默认 `"note"` | `ltm.py:90` |
| 4 | 插件 SaveMemoryParams.type | lowercase 默认 `"fact"` | `tools/memory/plugin.py:69`（已 `MemoryType(...title())` 校验，不入库） |
| 5 | 直接属性赋值 | 任意 str | `long_term.py:344` `e.memory_type = memory_type`（update_text） |
| 6 | 过滤比较 query | lowercase（`?type=note` 匹配不上 PascalCase 存储 = **现有 bug**） | `ltm.py:53`、`long_term.py:550` |

### 已对齐路径（零改动）

| 路径 | 位置 | 说明 |
|------|------|------|
| Consolidator insert `.value` | `consolidator.py:298,346,362` | 已 `.value`，coerce 幂等 |
| Consolidator 比对提示 related_dicts | `consolidator.py:311` `{"type": e.memory_type}` | StrEnum 是 str 子类，json 自动输出值 |
| Reflector submit `.value` | `memory_reflector.py:323` | 已 `.value` |
| Reflector 分组/提示序列化 | `memory_reflector.py:226,253` | dict 键 / json 均可，零改动 |
| C4 系统提示记忆节 | `Module/system_prompt/sections/memory.py:46` `"type": entry.memory_type` | Jinja2 `{{ mem.type }}` 渲染成员值，实测正常 |
| debug/info 序列化 | `debug/info.py:194` | StrEnum → json 自动，零改动 |
| debug LtmEntry 契约 | `ltm.py:63` → `frontend_contract/debug.py:349`（`memory_type: str`） | StrEnum 是 str，pydantic 自动 |
| debug LtmRecentItem 契约 | `frontend_contract/debug.py:112`（`memory_type: str`） | 同上 |
| 插件展示 `getattr(r, "memory_type", "unknown")` | `tools/memory/plugin.py:58` | f-string 输出成员值，零改动 |

## 设计

### 1. `MemoryType.try_coerce` + `MemoryType.coerce`（`core/schema/memory.py`）

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

- `MemoryType("note")` 精确匹配失败 → `MemoryType("Note")` 成功。`str.title()` 对
  `"NOTE"`/`"fact"`/`"Preference"` 均产出 PascalCase。连字符/数字/空串 → 双 ValueError → try_coerce
  返回 None / coerce 返回 NOTE。
- 两者都幂等：`coerce(coerce(x)) == coerce(x)`；consolidator/reflector 传入的合法成员/`.value`
  字符串原样通过。

### 2. `MemoryEntry.memory_type` + `__post_init__`（`core/memory/long_term.py`）

```python
@dataclass
class MemoryEntry:
    ...
    memory_type: MemoryType = MemoryType.NOTE   # 原: str = "Note"，注释改为类型说明
    ...
    def __post_init__(self) -> None:
        """构造时归一化 memory_type（dataclass 无自动校验，直接赋值会绕过）。"""
        self.memory_type = MemoryType.coerce(self.memory_type)
```

- 覆盖数据来源 #1（Milvus 行）、#2（insert metadata）、#3（debug body，经 insert 透传）、#4。
- StrEnum 成员是单例不可变对象，作为 dataclass 默认值无可变默认值问题；全字段有默认值，无排序冲突。
- 新增 import：`from core.schema.memory import MemoryType`（schema 是叶子模块，无循环导入）。

### 3. 越过 `__post_init__` 的边界显式 coerce

- **`long_term.py:344`**（update_text）：`e.memory_type = MemoryType.coerce(memory_type)` ——
  直接属性赋值绕过 `__post_init__`，必须显式。
- **`long_term.py:550`**（search 过滤，用 `try_coerce`）：
  ```python
  if memory_type:
      mt = MemoryType.try_coerce(memory_type)
      scored = [(s, e) for s, e in scored if mt is not None and e.memory_type == mt]
  ```
  `?memory_type=plan`/`plan` 现在能命中 PLAN（**修复大小写 bug**）；bogus → `mt is None` → 空结果
  （**保留现状语义**，不误命中 NOTE）。
- **`ltm.py:53`**（debug 列表过滤，同款 `try_coerce`）：bogus → 空，lowercase → 命中。

### 4. Milvus 序列化显式 `.value`（`long_term.py:182,303,360`）

`"memory_type": entry.memory_type` → `entry.memory_type.value`。StrEnum 成员本可作 str 直接入库，
显式 `.value` 明确 wire 语义；读回时 `row.get("memory_type", "Note")` 是普通 str，coerce 幂等。

### 5. 搜索/update 参数签名

`search(memory_type: str | None)` / `update_text(memory_type: str | None)` 保持 `str | None`
（调用方传入字符串形态），内部 coerce。`MemoryType` 是 `str` 子类，传成员在静态类型上也合法，
不改签名——避免牵动调用面。

## 行为变化（须在最终报告中声明）

1. **存储大小写归一**：`"fact"`/`"FACT"`/`"note"` 等 lowercase/大小写混输入入库后规范为
   `"Fact"`/`"Note"`（此前原样存储）。
2. **存储未知值兜底**：非词表值（含 `None`、数字、连字符串）从"原样存储"变为 `"Note"`。
   风险低——`memory_type` 是展示/过滤元数据，非业务关键字段。
3. **过滤语义**：lowercase 合法值（`?type=note`）从"匹配不上"变为"命中 NOTE"（**修复现有 bug**）；
   无效值（bogus）仍返回空结果（**不变**）。
4. Milvus 存量脏数据读取时经 `__post_init__` coerce 不炸（旧数据兼容）。
5. 测试中 lowercase 直构会归一化，相关断言须同步为规范值。

## 测试策略

**新增（marker=unit）：**
- `MemoryType.try_coerce` 单测：成员直返 / 精确 str / title 归一（`fact`/`FACT`/`preference`/`Note`）/
  None / 空串 / 非 str / 未知 str（`"bogus"`/`"fact-1"`）→ None。
- `MemoryType.coerce` 单测：`try_coerce` 的 NOTE 兜底（未知/None/非 str → NOTE）。
- `MemoryEntry.__post_init__` 归一：直构 `MemoryEntry(memory_type="fact")` → `.memory_type == MemoryType.FACT`。
- `long_term.search` lowercase 过滤命中：`memory_type="fact"` 命中 FACT 条目；
  过滤 bogus → 空结果。

**适配现有测试（核实后清单）：**
- `test_phase2.py:705`（`insert("...", type="plan")` 后 `assert entry.memory_type == "plan"`）→ 改
  `== "Plan"` 或 `== MemoryType.PLAN`（`test_insert_supports_type_alias_for_memory_type`）。
- `test_phase2.py:722,726`（`search` 后 `assert all(e.memory_type == "plan"/"preference")`）→ 改规范值
  （`test_search_filter_by_memory_type`）。
- `test_long_term.py:74-149`（直构 `memory_type="note"`）→ **无需改动**（`__post_init__` 归一，
  无 memory_type 断言）。
- `test_phase2.py:237,253,254,276`（`insert(memory_type="fact"/...)`）→ **无需改动**（无 memory_type 断言）。
- `test_memory.py:58,64`（`r1.memory_type = "fact"`，`r1` 是 **MagicMock 非 MemoryEntry**）→
  **无需改动**（插件 `getattr` 读 mock 属性，f-string 输出 `fact`，断言 `(类型:fact, ...)` 不受影响）。
- `test_memory_reflector.py:130`（`update_text.assert_awaited_once_with(..., memory_type=None)`，mock 断言，
  无 MemoryEntry 构造）→ **无需改动**（反射器传 None 经 `coerce` → NOTE，mock 断言比对参数本身不变）。
- `test_memory_reflector.py:118`（`MemoryEntry(... memory_type="Fact")` 直构）→ **无需改动**（合法值幂等）。

**回归**：`make test-normal`（`-m "not slow"`，~5min）+ 受影响模块定向跑
（core/memory、core/tools/memory、core/input/tests、core/tests/test_phase2）。

## Out of Scope

- 不动 `ExtractedMemory` / `InsertAction` / `UpdateAction`（已用 MemoryType）。
- 不动 frontend（`LtmPanel.vue` 的 type 过滤发送 lowercase，后端 `try_coerce` 已兼容）。
- 不做 Milvus 存量数据迁移脚本（读取时 `__post_init__` coerce 兜底足够）。
- 不新增 `MemoryType.__missing__` 等魔法——`try_coerce`/`coerce` 是唯二入口，集中可控。
