# 衍生 ID NewType 立项设计

> 状态：设计定稿（2026-08-09）｜关联：[code-style-guide §1](../../../code-style-guide.md)、[core-id-newtype-design](./2026-08-08-core-id-newtype-design.md)

**Goal:** 为三个无类型收口的衍生 ID（附件 / 记忆条目 / 调度任务）立项独立 NewType，贯通 core 内部流转，消除裸 str 承载语义 ID 的欠账。

## 背景与动机

2026-08-09 全项目裸类型摸排发现三类「衍生 ID」贯穿多模块但无 NewType 收口：

| ID | 语义 | 生成源 | 格式 | 现状 |
|----|------|--------|------|------|
| 附件 ID | 批次 7 附件体系 | `core/utils/message_id.py` `_new_id("att")` | `att_` + hex | `part.py` 4 个 Part 字段裸 `str \| None`；`attachment_repo` 参数裸 `str` |
| 记忆条目 ID | LTM Milvus 主键 | `new_message_id()`（long_term.py 复用） | `msg_` + 26 hex | `MemoryEntry.id: str`；`schema/memory.py`/`memory_reflector.py` 目标 ID 裸 `str` |
| 调度任务 ID | TaskScheduler 任务 | `new_message_id()`（task.py 复用） | `msg_` + 26 hex | `Task.id: str`；`scheduler.cancel/get_task/get_tree` 参数裸 `str` |

与既有 ID 体系（MessageId/ToolCallId/GenerationId/SegmentId/RequestId/SessionId/WorkflowId/AgentId/ApprovalId/NodeId/ArtifactId）并列，语义独立，各自该有类型收口。

## 设计决策

### 决策 1：三个独立 NewType（用户拍板）

`AttachmentId`/`MemoryId`/`TaskId` 都新建独立 NewType，不互相别名、不对齐 MessageId——记忆条目、调度任务、附件是不同语义实体。值空间彼此独立（独立前缀，见决策 2）。

```python
# core/schema/types.py（追加，与既有 NewType 并列）
AttachmentId = NewType("AttachmentId", str)  # 附件 ID（att_ 前缀）
MemoryId     = NewType("MemoryId", str)      # LTM 记忆条目 ID（mem_ 前缀；Milvus 旧 msg_ 数据读回兼容）
TaskId       = NewType("TaskId", str)        # 调度中心任务 ID（task_ 前缀）
```

### 决策 2：生成 helper 对齐既有模式（对设计草案的修正）

`core/utils/message_id.py` 已有 `new_message_id()`/`new_generation_id()`/`new_segment_id()` 三个 helper 模式（各自 `NewType(_new_id(prefix))`），新增三个是顺着既有模式而非函数膨胀，生成点无需散落包装：

```python
# core/utils/message_id.py（追加）
def new_attachment_id() -> AttachmentId:
    return AttachmentId(_new_id("att"))

def new_memory_id() -> MemoryId:
    return MemoryId(_new_id("mem"))

def new_task_id() -> TaskId:
    return TaskId(_new_id("task"))
```

> **决策记录（2026-08-09）**：设计草案原定「构造处包一层」。写 spec 时发现 message_id.py 既有 helper 模式，改为新增 helper 保持一致。修订原因：DRY（生成点共用同一前缀逻辑）、与既有 helper 模式对称、类型权威集中在 message_id.py。
>
> **决策记录（2026-08-09 复审）**：初版 `new_memory_id`/`new_task_id` 内部沿用 `_new_id("msg")`（值空间与 MessageId 共享，语义靠 NewType 名区分）。代码 review 质疑后**改为独立前缀 `mem_`/`task_`**——ID 前缀自解释（日志一眼区分类型）、值空间完全隔离；Task 不持久化（内存 dict）零迁移成本，MemoryId 的 Milvus 历史 `msg_` 数据经 `MemoryId(...)` 收口读回兼容（NewType 不校验前缀）。

### 决策 3：边界与豁免（沿用 §1.2/§1.3）

- **契约层保持裸 str**：frontend_contract 的 `AttachmentRef.attachmentId`、`AttachmentInfo.attachmentId` 等保持裸 `str`（零依赖，§1.2「另一侧对齐或裸传」）。
- **wire 入口裸 str 豁免 + 收口点**：`attachment_routes.py`/`debug_routes/ltm.py` 从 `match_info` 取值是 wire 边界（§1.3），收口点在进 repo/核心模型处包 `AttachmentId(...)`/`MemoryId(...)`。
- **NewType 是 str 子类型**：可直接传契约层裸 `str` 参数（wire 发射无需 `.value` 拆包）。
- **已知边界**：`core/schema/adapters/attachment_resolver.py` 的 `register/unregister/resolve/load` 参数保持 `str` 不动（NewType→str 反向合法，resolver 是 wire 侧适配）。

### 决策 4：pyright 五类形态全部报错（实施纪律）

已实证 pyright basic 模式对 str→NewType 的**参数传值、变量赋值、列表元素 append、dict key 查找、dataclass 默认值**五类形态全部报错（NewType→str 反向合法）。因此**每个字段/参数/容器元素类型化后，其全部上游调用点必须同步收口**——本 spec 贯通面表格按此枚举，实施时以 `make pyright` 0 error 为完成判据。

## 贯通面（分 4 个 Task）

### Task 1 — types.py 定义 + 导出 + 生成 helper

- `core/schema/types.py`：追加 3 个 NewType（在既有 ID 区，core 权威）
- `core/utils/message_id.py`：追加 3 个 helper（`new_attachment_id`/`new_memory_id`/`new_task_id`）
- `core/utils/__init__.py` / `core/schema/__init__.py`：导出
- `docs/code-style-guide.md` §1.1 ID 清单表：追加 3 行（权威归属 core、格式列，标注语义区分注释）

### Task 2 — AttachmentId 贯通

收口点 = 生成处 / repo 参数 / 核心模型字段。`attachment_repo` 方法全集（无 upsert，upsert 语义在 `create` 内）：**`create`/`get`/`add_ref`/`remove_ref`/`mark_deleted`/`delete_record`** 全部收口。

| 位置 | 改动 |
|------|------|
| `core/input/api_routes.py:760` | `attachment_id = _new_id("att")` → `new_attachment_id()` |
| `core/input/api_routes.py` 上传流程 | `created: list[str]` → `list[AttachmentId]`；`_cleanup`（:808 `repo.delete_record(aid)`）随收口；契约构造点 `attachmentId=it["attachment_id"]` 裸传（NewType 是 str 子类，直传） |
| `core/schema/part.py:88,98,107,116` | 4 个 Part 字段 `attachment_id: str \| None` → `AttachmentId \| None` |
| `core/input/ws_handler.py:450` `_attachment_part` | **参数保持 `str` 不动**——构造 Part 经 `cls(**data)` dict 解包透传（Any），字段类型化不依赖参数类型，改参数无静态收益（re-review 定案） |
| `core/input/ws_handler.py:566` | `attachment_repo.get(aid)`：`aid` 来自 wire `att.get("attachmentId")` → 进 repo 前 `AttachmentId(aid)` |
| `core/input/ws_handler.py:642` | `attachment_repo.add_ref(aid, message_id)` → `AttachmentId(aid)` 收口 |
| `core/input/session_routes.py:256,261` | 资源扫描：从 url 提取 `attachment_id` → 进 `repo.get` 前收口 `AttachmentId(...)` |
| `core/storage/attachment_repo.py` | 5 个方法（`get`/`add_ref`/`remove_ref`/`mark_deleted`/`delete_record`）参数 `attachment_id: str` → `AttachmentId`；`create(doc: dict)` 无参数（id 在 doc 内，`doc.get("attachment_id")` 为 Any，无需收口） |
| `core/storage/attachment_release.py` | `attachment_ids_from_message(msg) -> list[str]` → `list[AttachmentId]`（Part 字段改后从对象读取即 NewType，dict 分支需收口）；`release_message_attachments`/`release_messages_attachments` 内 `repo.get/remove_ref` 直传 |
| `core/storage/attachment_gc.py` | `run_attachment_gc(repo: Any, ...)` 经 `Any` 类型 repo 调用（`attachment_gc.py:82`），静态不检查，**无需收口** |
| `core/input/attachment_routes.py:116,190` | 两个 handler（`_download_handler` :116 / `_sign_handler` :190）`match_info` 取裸 str（wire 豁免）→ 进 :149/:199 `repo.get` 前各收口 `AttachmentId(...)` |
| 契约层 + attachment_resolver | 不动（裸 str 豁免，决策 3） |

### Task 3 — MemoryId 贯通

core 内部为主 + debug 管理端 wire 边界（`debug_routes/ltm.py`，§1.3 收口）。收口点 = Milvus 行解析处 / 生成处 / schema 模型字段。

| 位置 | 改动 |
|------|------|
| `core/memory/long_term.py` | `MemoryEntry.id: str = ""` → `MemoryId = MemoryId("")`（dataclass 默认值也必须 NewType）；insert 构造 `new_memory_id()`；`_load_from_milvus:123` fallback 生成改 `new_memory_id()`；行解析 `:121-125` 收口 `MemoryId(...)`；`_get_by_id`/`mark_absorbed`/`update_text`/`delete` 参数 → `MemoryId`；`_dirty_ids`/`_absorbed_ids` → `set[MemoryId]`；`_evict_overflow` 的 evicted ids |
| `core/schema/memory.py:77,87` | `UpdateAction.target_id`/`MergeAction.target_id` → `MemoryId` |
| `core/schema/memory_reflector.py:18,26,32` | `MergeItem.ids`/`UpdateItem.id`/`DeleteItem.id` → `MemoryId` |
| `core/input/debug_routes/ltm.py:115,119,143` | `match_info` 取裸 str（wire 豁免）→ 进 `long_term.delete/update_text` 前收口 `MemoryId(entry_id)` |
| `core/memory/consolidator.py:337,342` | 消费 `action.target_id` 传 `long_term.update_text`（long_term 参数已收口则直传；`target_id` 从 CompareAction 解析即 MemoryId 字段类型） |
| `core/memory/memory_reflector.py` | `ReflectorActions.model_validate` 从 LLM JSON 解析——schema 字段类型化即自动收口（pydantic NewType 运行时即 str），无需构造处手动包 |
| `core/tools/memory/plugin.py:57` | `entry_id = getattr(r, "id", "?")` 保持（display-only，接受）或收口（可选） |
| Milvus 存储边界 | `entry_id` 列读写是存储层，`str()` 序列化豁免 |

### Task 4 — TaskId 贯通

core 内部（无 wire 边界）。收口点 = Task 构造处 / scheduler 参数。

| 位置 | 改动 |
|------|------|
| `core/task/task.py` | `Task.id: str` → `TaskId`；`depends_on`/`children_ids` → `list[TaskId]`；构造点 `:139/:162` → `new_task_id()`；`add_child(self, child_id: str)` → `child_id: TaskId`（唯一调用方 `scheduler.py:159` `parent.add_child(st.id)` 传 TaskId） |
| `core/task/task.py` `parent_id` | 现状 `str \| None`（实收 RequestId，`new_tool_call(parent_id: str)`），**本批保持不动**——TaskId 立项只收口 `Task.id` 体系，parent_id 的语义裁决（TaskId vs RequestId）超出本 spec 范围，记入已知 |
| `core/task/scheduler.py` | `_tasks` → `dict[TaskId, Task]`；`cancel`/`get_task`/`get_tree` 参数 `task_id: str` → `TaskId`；`submit`/`dispatch_background` 返回 `-> str` → `-> TaskId`；`dispatch_tool_calls(self, parent_id: str, ...)` → `parent_id: RequestId`（与唯一调用方 `agent.py:523,551` 实际传 `RequestId` 一致） |
| `core/task/scheduler.py:156` | `parent = self._tasks.get(parent_id)`：parent_id 是 RequestId、`_tasks` 键是 TaskId，**运行时恒不命中（既有死代码）**——本批类型收口后 pyright 报错，按 `# type: ignore[arg-type]` + 注释记录死代码，不做行为修复 |
| `core/debug/info.py:299` | `get_tree(t.id)` 传 TaskId 直传（`t.id` 已是 TaskId） |
| 消费方 | `cancel`/`get_task` 无非测试调用方（grep 实证），无需收口；随签名改即可 |

## 验证标准

1. `make pyright`（Elysia env）：0 error（排除既有 monitoring `__all__` warning 基线）。这是「贯通面枚举完整」的最终判据——任何漏列联动点都会被 pyright 抓出。
2. `make lint`（Elysia env）：通过。
3. 测试（Elysia env）：
   - `core/memory/tests/`（long_term + schema/memory + memory_reflector）
   - `core/task/tests/`（task + scheduler）
   - `core/input/tests/`（upload/attachment/download/session_routes）
   - `core/tools/tests/`（memory/plugin 等）
   - `test-quick` 全量回归
4. 测试断言**禁止 `isinstance(x, NewType(...))`**（Python 3.12 抛 TypeError，§1.4）——用 `typing.get_type_hints()`。

## 已知权衡（记录，不改）

- `core/tools/memory/plugin.py:57` `entry_id = getattr(r, "id", "?")` 是 LLM payload display-only，包 NewType 收益低，保持 str 可接受。
- `Task.depends_on`/`children_ids` 当前无实际消费者（DAG 预留），类型化到 `list[TaskId]` 是前瞻一致，不引入额外逻辑。
- `Task.parent_id` 语义混乱（实收 RequestId、字段名暗示 TaskId）是既有问题，本批仅收口 `Task.id` 体系，parent_id 裁决留待未来 Task/Workflow 体系 spec。
- `scheduler.py:156` `_tasks.get(parent_id)` 是既有死代码（RequestId 查 TaskId 字典恒 miss），本批类型收口后加 ignore + 注释，不改行为。**复审决策（2026-08-09）**：曾评估删除绑定块，后确认保留现状——① 全项目文档（含 14/15-Cyrene 前端需求）均无 children 绑定需求定义；② 该块从未生效（`children_ids` 生产恒空），保留/删除都不影响未来接入（`children_ids` 字段 + `add_child` API + `get_tree` 展示完整保留，未来用正确 parent_id 填充即可）；③ 用户倾向保守。死代码已注释记录，未来接入时见注释即明。
- 附件 dict 中间态（上传流程 `it["attachment_id"]`）是 `dict[str, Any]` JSON 容器豁免，仅契约构造点直传 NewType 值。

## 关联文档

- [code-style-guide.md](../../../code-style-guide.md) — ID NewType 体系 + §1.3 收口点 + §1.4 NewType 陷阱
- [2026-08-08-core-id-newtype-design.md](./2026-08-08-core-id-newtype-design.md) — core ID NewType 化先例
