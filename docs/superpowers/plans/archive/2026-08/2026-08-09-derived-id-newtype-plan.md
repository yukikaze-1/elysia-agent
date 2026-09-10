# 衍生 ID NewType 立项实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为附件 ID / 记忆条目 ID / 调度任务 ID 立项独立 NewType（AttachmentId/MemoryId/TaskId），贯通 core 内部流转，消除裸 str 承载语义 ID 的欠账。

**Architecture:** 三个 NewType 定义在 `core/schema/types.py`（core 权威），生成 helper 对齐 `core/utils/message_id.py` 既有模式。每个 ID 一个贯通 Task：字段/参数类型化 → 全部上游调用点同步收口（pyright 会逐点抓出）→ pyright 0 error 为完成判据。

**Tech Stack:** Python 3.12 + pyright（Elysia env）+ ruff + pytest。

## Global Constraints

- **类型权威**：NewType 定义在 `core/schema/types.py`；生成 helper 在 `core/utils/message_id.py`；禁相对导入。
- **契约层保持裸 str**：frontend_contract 的 `AttachmentRef.attachmentId` 等一律不动（零依赖，§1.2）。
- **wire 边界裸 str 豁免 + 收口**：`match_info`/`data.get` 取值裸 str 是设计（§1.3），收口点在进 repo/核心模型处包 `NewType(...)`。
- **pyright 门禁**：`conda run -n Elysia pyright` 必须 0 error（排除既有 monitoring `__all__` warning 基线）——这是「贯通面枚举完整」的最终判据。已实证 pyright basic 对 str→NewType 的**参数传值、变量赋值、列表元素 append、dict key 查找、dataclass 默认值**五类形态全部报错；NewType→str 反向合法。
- **测试断言纪律**：禁止 `isinstance(x, NewType(...))`（Python 3.12 抛 TypeError，§1.4），用 `typing.get_type_hints()`。
- **本批不改行为**：纯类型收口，运行时可序列化/比较/存储零变化。`scheduler.py:156` `_tasks.get(parent_id)` 死代码只加 `# type: ignore` + 注释，不改逻辑。
- **运行命令**：pyright = `conda run -n Elysia pyright`；lint = `conda run -n Elysia ruff check core/ Module/ frontend_contract/ --exclude __pycache__`；测试 = `conda run -n Elysia python -m pytest <path> -q --tb=short`。

---

### Task 1: types.py 定义 + 生成 helper + 导出

**Files:**
- Modify: `core/schema/types.py`（NewType 区追加）
- Modify: `core/utils/message_id.py`（helper 追加）
- Modify: `core/utils/__init__.py`、`core/schema/__init__.py`（导出）
- Modify: `docs/code-style-guide.md` §1.1 ID 清单表

**Produces:** `AttachmentId`/`MemoryId`/`TaskId`（core.schema.types）+ `new_attachment_id`/`new_memory_id`/`new_task_id`（core.utils，返回对应 NewType）。

- [ ] **Step 1: types.py 追加 3 个 NewType**

在 `core/schema/types.py` 的既有 ID NewType 区（MessageId 等附近）追加：

```python
AttachmentId = NewType("AttachmentId", str)  # 附件 ID（att_ 前缀）
MemoryId = NewType("MemoryId", str)          # LTM 记忆条目 ID（new_message_id 值空间）
TaskId = NewType("TaskId", str)              # 调度中心任务 ID
```

- [ ] **Step 2: message_id.py 追加 3 个 helper**

`core/utils/message_id.py` 顶部 import 补 `AttachmentId, MemoryId, TaskId`（from core.schema.types），文件末尾追加：

```python
def new_attachment_id() -> AttachmentId:
    return AttachmentId(_new_id("att"))


def new_memory_id() -> MemoryId:
    return MemoryId(_new_id("msg"))


def new_task_id() -> TaskId:
    return TaskId(_new_id("msg"))
```

- [ ] **Step 3: 导出**

`core/schema/__init__.py` 与 `core/utils/__init__.py` 的 `__all__`/import 补上 3 个 NewType + 3 个 helper（与同文件既有导出并列）。

- [ ] **Step 4: code-style-guide §1.1 表追加 3 行**

`docs/code-style-guide.md` §1.1 ID 清单表加 3 行（权威归属 core，格式列：`att_` / `msg_` + 26 hex / `msg_` + 26 hex），并在表下注：`MemoryId`/`TaskId` 与 `MessageId` 值空间相同（同 `_new_id("msg")`），语义靠 NewType 名区分。

- [ ] **Step 5: 验证无回归**

Run: `conda run -n Elysia pyright`（Expected: 0 errors, 1 warning 基线）
Run: `conda run -n Elysia ruff check core/ Module/ frontend_contract/ --exclude __pycache__`（Expected: All checks passed）

- [ ] **Step 6: Commit**

```bash
git add core/schema/types.py core/utils/message_id.py core/schema/__init__.py core/utils/__init__.py docs/code-style-guide.md
git commit -m "feat(types): 新增 AttachmentId/MemoryId/TaskId NewType + 生成 helper（core 权威）"
```

---

### Task 2: AttachmentId 贯通

**Files:**（按此顺序改，每步后跑 pyright）
- Modify: `core/schema/part.py:88,98,107,116`
- Modify: `core/storage/attachment_repo.py`（5 方法参数）
- Modify: `core/input/api_routes.py:760`（生成）+ 上传流程 `created` 列表
- Modify: `core/input/ws_handler.py:566,642`（repo 收口）
- Modify: `core/input/session_routes.py:256,261`（repo 收口）
- Modify: `core/storage/attachment_release.py:31`（返回类型）
- Modify: `core/input/attachment_routes.py:116`（进 repo 前收口）
- 不动：`_attachment_part` 参数（:450，构造经 dict 解包透传无静态收益）、`attachment_gc.py`（repo 是 Any 类型）、契约层、`attachment_resolver.py`

**Produces:** `AttachmentId` 全链贯通；`attachment_repo` 5 个方法收口 `AttachmentId`。

- [ ] **Step 1: part.py 4 个 Part 字段**

`core/schema/part.py` 的 `ImagePart`/`FilePart`/`AudioPart`/`VideoPart` 字段 `attachment_id: str | None = None` → `attachment_id: AttachmentId | None = None`，顶部 import 补 `AttachmentId`（from core.schema.types）。

- [ ] **Step 2: attachment_repo.py 5 方法参数**

`core/storage/attachment_repo.py`：`get`/`add_ref`/`remove_ref`/`mark_deleted`/`delete_record` 的参数 `attachment_id: str` → `attachment_id: AttachmentId`。`create(doc: dict)` 不动（无参数，id 在 doc 内）。顶部 import 补 `AttachmentId`。

- [ ] **Step 3: api_routes.py 生成 + created 列表**

`core/input/api_routes.py`：
- :760 `attachment_id = _new_id("att")` → `attachment_id = new_attachment_id()`（import 补 `new_attachment_id`；**同时移除** :636 函数内 `from core.utils.message_id import _new_id`——变孤儿，防 ruff F401）
- 上传流程 `created` 列表标注 `list[AttachmentId]`（`created.append(it["attachment_id"])` 的容器注解）
- `_cleanup`（:808 `repo.delete_record(aid)`）随 repo 参数已收口，`aid` 来自 `created: list[AttachmentId]` 直传

- [ ] **Step 4: ws_handler.py repo 收口**

`core/input/ws_handler.py`：
- :566 `doc = await agent.attachment_repo.get(aid)` → `get(AttachmentId(aid))`（`aid` 来自 wire `att.get("attachmentId")`）
- :642 `await agent.attachment_repo.add_ref(aid, message_id)` → `add_ref(AttachmentId(aid), message_id)`

- [ ] **Step 5: session_routes.py repo 收口**

`core/input/session_routes.py`：:256 提取 `attachment_id` 后、进 :261 `repo.get(attachment_id)` 前包 `AttachmentId(attachment_id)`。

- [ ] **Step 6: attachment_release.py 返回类型**

`core/storage/attachment_release.py`：`attachment_ids_from_message(msg: Message) -> list[str]` → `-> list[AttachmentId]`，**局部 `ids: list[str]` 注解同步改 `list[AttachmentId]`**（list 逆变，`return ids` 否则报错）；函数内 dict/对象分支取值均 `Any`，直传可过 pyright，无需显式收口。`release_message_attachments`/`release_messages_attachments` 内 `repo.get/remove_ref` 直传。

- [ ] **Step 7: attachment_routes.py 收口**

`core/input/attachment_routes.py`：**两个 handler**——`_download_handler`（:116 match_info → :149 `repo.get`）与 `_sign_handler`（:190 match_info → :199 `repo.get`）。两处 match_info 取裸 str（wire 豁免）后，进 `repo.get` 前各包 `AttachmentId(attachment_id)`。

- [ ] **Step 8: pyright 0 error**

Run: `conda run -n Elysia pyright`（Expected: 0 errors）——漏掉的联动点会被 pyright 抓出，逐个收口直到 0 error。

- [ ] **Step 9: 测试**

Run: `conda run -n Elysia python -m pytest core/input/tests/ core/storage/tests/ -q --tb=short`（附件上传/下载/签名/session_routes 相关）
Run: `conda run -n Elysia python -m pytest -m "unit or service" -q --tb=short --ignore=Module/workflow_engine/tests`（test-quick 全量回归）

- [ ] **Step 10: Commit**

```bash
git add core/schema/part.py core/storage/attachment_repo.py core/input/api_routes.py core/input/ws_handler.py core/input/session_routes.py core/storage/attachment_release.py core/input/attachment_routes.py
git commit -m "fix(type): AttachmentId 贯通——part/repo/input 收口（att_ 前缀独立 NewType）"
```

---

### Task 3: MemoryId 贯通

**Files:**
- Modify: `core/memory/long_term.py`（MemoryEntry.id + 生成 + 反查 + 参数 + set）
- Modify: `core/schema/memory.py:77,87`、`core/schema/memory_reflector.py:18,26,32`
- Modify: `core/input/debug_routes/ltm.py:115,119,143`
- Modify: `core/memory/consolidator.py`（消费直传，若 pyright 报）
- 不动：`core/tools/memory/plugin.py:57`（display-only，接受）；Milvus `entry_id` 列 `str()` 序列化

**Produces:** `MemoryId` 全链贯通；`long_term` 的 `_get_by_id`/`mark_absorbed`/`update_text`/`delete` 收口 `MemoryId`。

- [ ] **Step 1: schema/memory.py + memory_reflector.py 字段**

`core/schema/memory.py`：`UpdateAction.target_id: str = Field(min_length=1)` → `target_id: MemoryId`；`MergeAction.target_id` 同。顶部 import 补 `MemoryId`。
`core/schema/memory_reflector.py`：`MergeItem.ids: list[str]` → `list[MemoryId]`；`UpdateItem.id: str` → `MemoryId`；`DeleteItem.id: str` → `MemoryId`。import 补 `MemoryId`。
（LLM JSON 经 `model_validate` 解析时由字段类型自动收口，NewType 运行时即 str。）

- [ ] **Step 2: long_term.py 核心**

`core/memory/long_term.py`：
- `MemoryEntry.id: str = ""` → `MemoryId = MemoryId("")`（dataclass 默认值也必须 NewType）
- insert 构造 `:171` `id=new_message_id()` → `id=new_memory_id()`；`_load_from_milvus:123` fallback `entry_id = new_message_id()` → `new_memory_id()`，且行解析 `MemoryEntry(id=str(entry_id))` → `id=MemoryId(entry_id)`
- `_get_by_id(entry_id: str)` / `mark_absorbed(entry_id: str)` / `update_text` / `delete` 参数 → `MemoryId`
- `_dirty_ids: set[str]` → `set[MemoryId]`、`_absorbed_ids` → `set[MemoryId]`（insert/evict/mark_absorbed 内 `add`/`discard` 处随收口）
- `_evict_overflow` 的 `evicted_ids = {e.id for e in evicted}` 自动为 `set[MemoryId]`

- [ ] **Step 3: ltm.py wire 收口**

`core/input/debug_routes/ltm.py`：:115 `entry_id = request.match_info.get("entry_id", "")`（wire 豁免）后，进 :119 `agent.long_term.delete(...)` 前包 `MemoryId(entry_id)`；:143 `update_text(entry_id, ...)` 同。

- [ ] **Step 4: consolidator/memory_reflector 消费验证**

`core/memory/consolidator.py:337,342` 消费 `action.target_id`（已 MemoryId）传 `long_term.update_text`（参数已收口）——若 pyright 报错则收口，通常直传合法。
`core/memory/memory_reflector.py` 构造 `MergeItem`/`UpdateItem`/`DeleteItem` 处：字段类型化后 `model_validate` 自动收口，无手动改动。

- [ ] **Step 5: pyright 0 error**

Run: `conda run -n Elysia pyright`（Expected: 0 errors）——逐个收口漏点。

- [ ] **Step 6: 测试**

Run: `conda run -n Elysia python -m pytest core/memory/tests/ core/input/tests/test_debug_ltm.py -q --tb=short`（memory 相关；debug_routes 无独立 tests 目录，LTM debug 测试在 core/input/tests/test_debug_ltm.py）
Run: `conda run -n Elysia python -m pytest -m "unit or service" -q --tb=short --ignore=Module/workflow_engine/tests`

- [ ] **Step 7: Commit**

```bash
git add core/memory/long_term.py core/schema/memory.py core/schema/memory_reflector.py core/input/debug_routes/ltm.py core/memory/consolidator.py core/memory/memory_reflector.py
git commit -m "fix(type): MemoryId 贯通——long_term/schema/ltm 收口（记忆条目 ID 独立 NewType）"
```

---

### Task 4: TaskId 贯通

**Files:**
- Modify: `core/task/task.py`（Task.id + depends_on/children_ids + add_child + 构造点）
- Modify: `core/task/scheduler.py`（_tasks + cancel/get_task/get_tree + submit 返回 + dispatch_tool_calls）
- Verify: `core/debug/info.py:299`（get_tree 直传）
- 不动：`Task.parent_id`（现状 `str | None`，实收 RequestId，本批不裁决——记入已知）

**Produces:** `TaskId` 全链贯通；`scheduler` 的 `_tasks: dict[TaskId, Task]` + `cancel/get_task/get_tree` 收口 + `dispatch_tool_calls(parent_id: RequestId)`。

- [ ] **Step 1: task.py 字段 + 方法**

`core/task/task.py`：
- `Task.id: str` → `TaskId`；`depends_on: list[str]` → `list[TaskId]`；`children_ids: list[str]` → `list[TaskId]`；`parent_id: str | None` **保持不动**（记入已知权衡）
- `add_child(self, child_id: str)` → `child_id: TaskId`
- 构造点 `:139/:162` `id=new_message_id()` → `id=new_task_id()`（import 改 `new_task_id`）
- `new_tool_call(parent_id: str)` **保持不动**（parent_id 不裁决）
- import 补 `TaskId`（from core.schema.types）

- [ ] **Step 2: scheduler.py 收口**

`core/task/scheduler.py`：
- `self._tasks: dict[str, Task]` → `dict[TaskId, Task]`
- `cancel(self, task_id: str)` / `get_task(self, task_id: str)` / `get_tree(self, task_id: str)` → `task_id: TaskId`
- `submit(...) -> str` / `dispatch_background(...) -> str` → `-> TaskId`（无外部调用方，返回类型安全收口）
- `dispatch_tool_calls(self, parent_id: str, ...)` → `parent_id: RequestId`（唯一调用方 `agent.py:523,551` 传 `RequestId`）
- :156 `parent = self._tasks.get(parent_id)`：parent_id 是 RequestId 查 TaskId 字典恒 miss（既有死代码）→ 加 `# type: ignore[arg-type]` + 注释「死代码：RequestId 查 TaskId 字典恒不命中，本批不改行为」
- :148 `parent_id=parent_id or ""` 传 `new_tool_call(parent_id: str)`：`RequestId | Literal[""]` 合法（联合含 str）
- import 补 `TaskId`（from core.schema.types）；`RequestId` 若未引补 `from core.schema.types import RequestId`

- [ ] **Step 3: debug/info.py 验证**

`core/debug/info.py:299` `get_tree(t.id)`：`t.id` 已是 `TaskId`，`get_tree(task_id: TaskId)` 直传——确认无 pyright 报错即通过（无改动）。

- [ ] **Step 4: pyright 0 error**

Run: `conda run -n Elysia pyright`（Expected: 0 errors）

- [ ] **Step 5: 测试**

Run: `conda run -n Elysia python -m pytest core/task/tests/ -q --tb=short`
Run: `conda run -n Elysia python -m pytest -m "unit or service" -q --tb=short --ignore=Module/workflow_engine/tests`

- [ ] **Step 6: Commit**

```bash
git add core/task/task.py core/task/scheduler.py
git commit -m "fix(type): TaskId 贯通——task/scheduler 收口（调度任务 ID 独立 NewType，parent_id 不裁决）"
```

---

### Task 5: 全量验证 + 文档

- [ ] **Step 1: 门禁三连**

Run: `conda run -n Elysia pyright`（0 errors）
Run: `conda run -n Elysia ruff check core/ Module/ frontend_contract/ --exclude __pycache__`（All checks passed）
Run: `conda run -n Elysia python -m pytest -m "unit or service" -q --tb=short --ignore=Module/workflow_engine/tests`（全量回归无失败）

- [ ] **Step 2: 文档更新**

- `docs/code-style-guide.md` §1.1：确认 3 行已加（Task 1 Step 4）
- `todo.md`：功能改进/代码清理区若有相关条目，更新完成状态；若无则跳过
- 无运行时行为变化，`docs/architecture.md` 的 ID 体系描述若提及可补一行（可选，核对后决定）

- [ ] **Step 3: Commit（文档）**

```bash
git add docs/ todo.md
git commit -m "docs: 衍生 ID NewType 落地——code-style-guide §1.1 补 3 行 + 相关文档同步"
```

## Self-Review 记录

- **Spec 覆盖**：spec 决策 1-4 + 贯通面表全部映射到 Task 1-4（Task 1=types/helper/导出/文档，Task 2=AttachmentId，Task 3=MemoryId，Task 4=TaskId，Task 5=验证+文档）。无 spec 要求遗漏。
- **占位符扫描**：所有步骤含具体文件:行 + 改动内容 + 验证命令，无 TBD/TODO。
- **类型一致性**：`new_attachment_id -> AttachmentId`、`new_memory_id -> MemoryId`、`new_task_id -> TaskId` 与 types.py 定义一致；贯通面各收口点类型统一（repo 参数 = 字段类型 = 收口包装类型）。
