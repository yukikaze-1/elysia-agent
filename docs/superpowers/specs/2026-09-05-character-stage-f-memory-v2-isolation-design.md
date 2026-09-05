# 阶段 F：记忆 v2 隔离（MemoryScope + ScopedLongTermMemory）Spec

> **状态：** 方案 A（MemoryScope 强制 + ScopedLongTermMemory 绑定视图 + long_term_memory_v2 新 collection）已定案；三项决策已确认（新建 long_term_memory_v2 / 严格移除无 scope overload / memory_view 挂 CharacterRuntime）
> **架构稿：** [`docs/2026-08-30-character-prompt-customization-backend-architecture-design.md`](../../2026-08-30-character-prompt-customization-backend-architecture-design.md)（§8 长期记忆与关系数据隔离）
> **fact 基线：**（阶段 E 完成后 HEAD；阶段 F 基于此推进）
> **上一批：** 阶段 E（CharacterRuntime 与会话绑定 / M3 目标态），`TurnCharacterContext.memoryScope` 已预埋但未消费
>
> **明确排除：** `user_shared` 实际升级路径（只预留枚举）；Neo4j 关系事实迁移；旧 `long_term_memory` 数据迁移（阶段 E 已清旧会话，空库起步）；前端 Electron 联调
>
> 本批不迁移旧数据。阶段 F 从**全新** `long_term_memory_v2` 起步，旧 `long_term_memory` 视为孤儿（阶段 E 已清空）。

## 0. 一句话目标

让阶段 E 预埋的 `TurnCharacterContext.memoryScope`（= instanceId）真正生效：记忆的读写从「当前 session 全局读」（M1/M2）改为「按 `character_instance_id` + `user_id` scope 隔离」（架构稿 §8），使**不同角色实例的记忆互不可见、可安全并存**。

## 1. 背景与问题

### 1.1 阶段 C+E / D / E 交付的底座（已核实）

阶段 D（`main@4089d30f`）+ 阶段 E（`main@HEAD`）已交付：

- **`TurnCharacterContext.memoryScope` 已预埋**：`loop.py:422` 构造时传 `memoryScope=instance_id`；[turn_context.py:23](core/cognitive/turn_context.py#L23) 字段存在但**零消费点**（grep 确认仅定义 + 构造 + 一个测试断言）。
- **`CharacterRuntime`**（[runtime.py](core/character/runtime.py)）：持 instance/definition/revision/user_identity + `engine`（per-instance 懒建）+ `dirty`/`stateVersion` + `persist_fn`，**无 memory_view 占位**（阶段 CE spec 声称的「ScopedMemory 占位」未落地）。
- **`LongTermMemory`**（[long_term.py](core/memory/long_term.py)）：单例,`_entries` 内存列表 + `MemoryRepository`(Milvus `long_term_memory` collection)。`search(query_text, query_embedding, top_k, memory_type)` **无 scope 参数**；`insert(text, **metadata)` 也无 scope。
- **`MemoryRepository`**（[memory_repo.py](core/storage/memory_repo.py)）：`COLLECTION = "long_term_memory"`,`search(vec, top_k, filter_expr, output_fields)` 已支持 filter_expr,但 `_search_milvus_candidates` 未传 filter。
- **C4 **MemorySection**（[memory.py](../../Module/system_prompt/sections/memory.py)）：`search(query_text, top_k)` 无 scope。
- **memory 工具**（[plugin.py](core/tools/memory/plugin.py)）：`ctx.get("memory")` 是裸单例,`search`/`insert` 无 scope。
- **consolidator**（[consolidator.py](core/memory/consolidator.py)）：buffer 全局,compare 全局,`_memory` = 裸单例。
- **memory_reflector**（[memory_reflector.py](core/memory/memory_reflector.py)）：全库 `get_all()` 聚类。
- **agent 接线**（[agent.py:178/295/303](core/agent/agent.py#L178)）：构造**单个** `LongTermMemory`,`ctx["memory"]` = 裸 `self._long_term`;consolidator/reflector 也拿裸单例。

### 1.2 缺口：记忆语义未按 instance 隔离

阶段 E 实现了「Prompt/状态/声音不串」（M3）,但**记忆仍是全局单例**:

1. **C4 注入全局记忆**：`MemorySection.search(query_text)` 对**所有角色实例**返回同一 pool——不同角色看到对方的记忆。
2. **memory 工具全局读写**：`search_memory`/`save_memory` 经 `ctx.get("memory")` 裸单例,无 scope 过滤。
3. **consolidator 全局合并**：`_compare_and_resolve` 查询全局候选,新提取记忆与**他角色**的记忆比对合并——跨角色污染。
4. **memory_reflector 全库聚类**：`_cluster_entries(get_all())` 对所有实例聚类,merge/delete/update 可能误操作他 scope 条目。
5. **无 scope 元数据**：Milvus 记录无 `user_id`/`character_instance_id`/`visibility` 字段,`search` 无法在 ANN 阶段过滤。

### 1.3 M3 语义的验收（架构稿 §8）

架构稿 §8 定义目标态：所有 LTM 公共写/读 API 必须接受不可省略的 scope；生产代码无「漏传即全局」的默认值；ANN 阶段用 filter 而非先召回再 Python 过滤。

## 2. 需求

### 2.1 MemoryScope（架构稿 §8.1）

```python
@dataclass(frozen=True)
class MemoryScope:
    user_id: str
    character_instance_id: str
    visibility: MemoryVisibility = MemoryVisibility.CHARACTER_PRIVATE  # character_private | user_shared
```

- **产品记忆**（自动提取 / 工具保存 / 关系事件）一律写 `character_private`。
- **`user_shared` 只预留枚举**,不提供自动升级路径。
- **`MemoryScope` 放 `core/memory/`**（领域模型）,不放 `core/storage`。

### 2.2 ScopedLongTermMemory（架构稿 §8.2）

绑定视图,降低漏传 scope 的概率：

```python
memory_view = long_term.for_scope(scope)
await memory_view.insert(text, ...)
await memory_view.search(query, ...)
await memory_view.update(entry_id, ...)
await memory_view.delete(entry_id)
```

**严格约束（已确认决策 #2）**：底层 `LongTermMemory` 的 `search/insert/update/delete` **移除无 scope overload**——签名必须带 `scope`（或等价地,只能经 `for_scope()` 访问）。不存在「调用方不传就查全局」的路径。

### 2.3 存储层（架构稿 §8.3）

- 新建 `long_term_memory_v2` collection（已确认决策 #1）。
- 每条至少含：`entry_id`/`user_id`/`character_instance_id`/`visibility`/`conversation_id`(可空)/`source_message_ids`(可空)/`text`/`type`/`poignancy`/`timestamp`/`schema_version=2`/`embedding`。
- **ANN 阶段用 filter**：`user_id == "..." && character_instance_id == "..." && visibility == "character_private"`。
- **内存 fallback 同样先按 scope 过滤**（架构稿 §8.3 明确）。
- Debug 全库操作放在显式 admin API,不能借用产品 MemoryView。

### 2.4 各消费点迁移

| 消费点 | 现状 | 迁移目标 |
|--------|------|---------|
| **C4 MemorySection** | `search(query_text, top_k)` 无 scope | 接收 scoped view（或 scope）`search` 带 scope |
| **memory 工具插件** | `ctx.get("memory")` 裸单例 | 注入 `for_scope()` scoped view |
| **consolidator** | buffer 全局、compare 全局 | buffer 属 instance；ExtractMemory 输入 scope；compare 只查同 scope |
| **memory_reflector** | 全库聚类 | 每 instance 独立；`get_all()` 只返回当前 scope；merge 验证 scope |
| **agent 接线** | `ctx["memory"]` = 裸单例；consolidator/reflector 拿裸单例 | 按 instance 注入 scoped view |

### 2.5 CharacterRuntime 持有 memory_view

阶段 CE 占位未落地——本批在 [runtime.py](core/character/runtime.py) 加：

```python
class CharacterRuntime:
    def __init__(self, ..., memory_view: "ScopedLongTermMemory | None" = None):
        self.memory_view = memory_view
```

**已确认决策 #3**：`memory_view` 由 `agent` 在 `_make_runtime` 时经 `for_scope()` 构造,`CharacterRuntime` 持有。与阶段 E persist_fn 同模式。

## 3. 架构方案

### 3.1 分层

```
core/memory/
├── scope.py               # MemoryScope + MemoryVisibility 领域模型（新增）
├── long_term.py           # LongTermMemory: 加 for_scope() + 严格 scope 参数（改造）
├── scoped_long_term.py    # ScopedLongTermMemory: 绑定视图（新增）
├── consolidator.py        # MemoryConsolidator: instance scoped（改造）
└── memory_reflector.py    # MemoryReflector: instance 隔离（改造）

core/storage/
├── memory_repo.py         # MemoryRepository: 加 scope filter（改造）
└── memory_repo_v2.py      # MemoryRepositoryV2: 指向 long_term_memory_v2（新增）

core/character/
└── runtime.py             # CharacterRuntime: 加 memory_view 持有（改造）

core/cognitive/
└── loop.py                # _prepare_turn: 经 runtime.memory_view 注入 ctx["memory"]/C4（改造）

Module/system_prompt/sections/memory.py   # C4: 接收 scoped view（改造）

core/agent/
└── agent.py               # _make_runtime: 构造 for_scope() view（改造）
```

### 3.2 关键数据流

```
_prepare_turn(runtime)
  → runtime.memory_view     # ScopedLongTermMemory（scope 已绑定）
    → C4 MemorySection.search(query, ...)   # 已 scope 过滤
    → ctx["memory"] = runtime.memory_view   # memory 工具已 scope 过滤
    → consolidator._memory = runtime.memory_view   # compare 只查同 scope
    → memory_reflector.get_all() = runtime.memory_view.get_all()  # instance 隔离
```

**核心不变式**：任何记忆写/读都经过 `ScopedLongTermMemory` 视图——`scope` 在视图创建时绑定,调用方无法在调用点绕过。

### 3.3 Scope 绑定时机

- 视图在 `_make_runtime`（registry 创建 runtime）时构造,scope 固定为实例的 `(user_id, character_instance_id)`。
- runtime 被 LRU 驱逐重建时,`_make_runtime` 重建 view——scope 不变,天然幂等。
- **策略**：Elysia 默认实例的 view 与生产路径一致,无特殊优待。

### 3.4 memory_view 构造注入点

`_make_runtime(instance_id, character_id)` 现由 agent 持 `runtime_factory` 调用。本批在 factory 内构造 `memory_view`：

```python
def _make_runtime(self, instance_id: str) -> CharacterRuntime:
    rt = CharacterRuntime(instance=..., definition=..., ...)
    rt.set_persist_fn(...)
    rt.memory_view = self._long_term.for_scope(
        MemoryScope(user_id=self._user_id, character_instance_id=instance_id)
    )
    return rt
```

（`self._user_id` 从当前会话 `UserIdentity` 解析,阶段 E 的 `usr_local_default`。）

## 4. 错误处理

| 场景 | 行为 |
|------|------|
| 无 scope 调用 `search/insert` | `TypeError`（strict overload 签名强制） |
| scope 过滤后无结果 | 返回 `[]`,不全局回退 |
| Milvus 离线 | 内存 fallback 按 scope 过滤后正常返回（优雅降级） |
| merge 误幻觉他 scope entry ID | repository 返回 not found,反射器输出失败被丢弃 |
| `memory_view` 为 None | C4 返回 `""`（不注入记忆）;`ctx["memory"]` 为 None → 工具报「记忆系统不可用」 |

## 5. 测试策略

沿用分层 marker。新增/改造重点：

- **scope.py**：MemoryScope 不可变 + visibility 默认 character_private。
- **long_term.py**:`for_scope()` 返回正确 scope view；无 scope 调用 raise。
- **scoped_long_term.py**:insert/search/update/delete 全部带 scope;不同 scope 互不可见;空 scope 无全局回退。
- **storage v2**：ANN filter 正确;内存 fallback 按 scope 过滤。
- **C4**:传入 scoped view 后只返回当前 scope。
- **工具**:注入 scoped view 后 `search`/`save` 只影响当前 scope。
- **consolidator**:不同 instance 的 buffer 独立;compare 只查同 scope。
- **reflector**:`get_all()` 只返回当前 scope;merge 验证 scope。
- **agent 接线**:`ctx["memory"]` 是 scoped view 而非裸单例。
- **snapshot 回归**：`test_elysia_snapshot_unchanged`（18127B）——C4 对 default elysia 单角色行为不变(空的 scoped view 返回空)。

## 6. 明确不做（defer）

- **`user_shared` 实际升级路径**：本批只定义枚举,不提供自动升级。
- **Neo4j 关系事实迁移**（架构稿 §8.6）：Neo4j 缺失不阻塞本批。
- **旧 `long_term_memory` 数据迁移**：阶段 E 已清旧会话,空库起步;不迁移。
- **Debug 全库 admin API**：`MemoryRepository.query(filter_expr=None)` 保留供 debug,但不作为产品 MemoryView 借用。
- **前端 Electron 联调**。

## 7. 验收清单（exit conditions）

1. **MemoryScope 模型**定义于 `core/memory/scope.py`,不可变,默认 `character_private`。
2. **`LongTermMemory.for_scope(scope)`** 返回正确 `ScopedLongTermMemory`;底层 `search/insert/update/delete` **无 scope overload**。
3. **`ScopedLongTermMemory`** 的 insert/search/update/delete 全部作用域过滤;不同 `character_instance_id` 互不可见。
4. **`long_term_memory_v2`** collection 建立;ANN 用 `user_id + character_instance_id + visibility` filter;内存 fallback 按 scope 过滤。
5. **C4 / memory 工具 / consolidator / reflector** 全部经 scoped view 读写,无全局单例泄漏。
6. **`CharacterRuntime` 持有 `memory_view`**;agent `_make_runtime` 构造 `for_scope()` 视图。
7. **Elysia snapshot byte 不变**（18127B）。
8. **ruff/pyright/contract --check** 全绿。

## 8. 引用

- 架构稿 §8：`docs/2026-08-30-character-prompt-customization-backend-architecture-design.md`
- 架构稿 §3.2 不变量 #5：任意记忆写入/查询必须显式带 MemoryScope,无「漏传即全局」默认值。
- stage CE spec：`docs/superpowers/specs/2026-09-03-character-stage-ce-runtime-domain-design.md`（§7 ScopedMemory 占位指针）
