# 阶段 F：记忆 v2 隔离（MemoryScope + ScopedLongTermMemory）Spec

> **状态：** 方案 A（MemoryScope 强制 + ScopedLongTermMemory 绑定视图 + long_term_memory_v2 新 collection + per-runtime consolidator/reflector）已定案；三项决策已确认（新建 long_term_memory_v2 / 严格移除无 scope overload / memory_view 挂 CharacterRuntime）
> **架构稿：** [`docs/2026-08-30-character-prompt-customization-backend-architecture-design.md`](../../../../2026-08-30-character-prompt-customization-backend-architecture-design.md)（§8 长期记忆与关系数据隔离）
> **fact 基线：**（阶段 E 完成后 HEAD；阶段 F 基于此推进）
> **上一批：** 阶段 E（CharacterRuntime 与会话绑定 / M3 目标态），`TurnCharacterContext.memoryScope` 已预埋但未消费
>
> **明确排除：** `user_shared` 实际升级路径（只预留枚举）；Neo4j 关系事实迁移；旧 `long_term_memory` 数据迁移（阶段 E 已清旧会话，空库起步）；前端 Electron 联调
>
> 本批不迁移旧数据。阶段 F 从**全新** `long_term_memory_v2` 起步，旧 `long_term_memory` 视为孤儿（阶段 E 已清空）。

## 0. 一句话目标

让阶段 E 预埋的 `TurnCharacterContext.memoryScope`（= instanceId）真正生效：记忆的读写从「当前 session 全局读」（M1/M2）改为「按 `character_instance_id` + `user_id` scope 隔离」（架构稿 §8），使**不同角色实例的记忆互不可见、可安全并存**。consolidator / reflector 同步 per-runtime 化，buffer / 计数器按 instance 隔离。

## 1. 背景与问题

### 1.1 阶段 C+E / D / E 交付的底座（已核实）

阶段 D（`main@4089d30f`）+ 阶段 E（`main@HEAD`）已交付：

- **`TurnCharacterContext.memoryScope` 已预埋**：`loop.py:422` 构造时传 `memoryScope=instance_id`；[turn_context.py:23](../../../../../core/cognitive/turn_context.py#L23) 字段存在但**零消费点**（grep 确认仅定义 + 构造 + 一个测试断言）。
- **`CharacterRuntime`**（[runtime.py](../../../../../core/character/runtime.py)）：持 instance/definition/revision/user_identity + `engine`（per-instance 懒建）+ `dirty`/`stateVersion` + `persist_fn`，**无 memory_view 占位**（阶段 CE spec 声称的「ScopedMemory 占位」未落地）。
- **`LongTermMemory`**（[long_term.py](../../../../../core/memory/long_term.py)）：单例,`_entries` 内存列表 + `MemoryRepository`(Milvus `long_term_memory` collection)。`search(query_text, query_embedding, top_k, memory_type)` **无 scope 参数**；`insert(text, **metadata)` 也无 scope；`get_all()` 无 scope；`count` 无 scope。
- **`MemoryRepository`**（[memory_repo.py](../../../../../core/storage/memory_repo.py)）：`COLLECTION = "long_term_memory"`,`search(vec, top_k, filter_expr, output_fields)` 已支持 filter_expr,但 `_search_milvus_candidates` 未传 filter——`_load_from_milvus` 更是 `filter="id >= 0"` 全量加载（long_term.py:336）。
- **`_search_milvus_candidates`**（long_term.py:50-104）：Milvus ANN 候选 → 与**全局** `_entries`（所有 scope）按 pk/text 匹配；`_search_fallback`（long_term.py:771）扫描全局 `_entries`。**双路径均未按 scope 过滤**。
- **C4 **MemorySection**（[memory.py](../../Module/system_prompt/sections/memory.py)）：`search(query_text, top_k)` 无 scope,接收 `long_term` 为 duck-typed `Any`（未 import core）。
- **memory 工具**（[plugin.py](../../../../../core/tools/memory/plugin.py)）：`ctx.get("memory")` 是裸单例,`search`/`insert` 无 scope。
- **consolidator**（[consolidator.py](../../../../../core/memory/consolidator.py)）：**单例**,buffer `list[tuple[str,str]]` 全局,compare 全局,`_memory` = 裸单例。
- **memory_reflector**（[memory_reflector.py](../../../../../core/memory/memory_reflector.py)）：**单例**,全库 `get_all()` 聚类,计数器 `_round_count`/`_new_memory_count`/`_last_summary` 共享。
- **agent 接线**（[agent.py:178/293/301](../../../../../core/agent/agent.py#L178)）：构造**单个** `LongTermMemory`(`repo=self._memory_repo` v1)、**单个** `MemoryConsolidator`、**单个** `MemoryReflector`。`ctx["memory"]` 实际在 [`_lifecycle_tools.py:68`](../../../../../core/agent/_lifecycle_tools.py#L68) 注入裸 `self._long_term`;per-turn `loop.py:430-433` 只更新 `svc["session"]`/`svc["_session_msg_count"]`,不碰 `svc["memory"]`。
- **debug/health**（[info.py:203/291/300](../../../../../core/debug/info.py#L203)）：读 `agent.consolidator._history`/`_buffer`/`_threshold`、`agent.memory_reflector`。[health.py:46-49](../../../../../core/input/health.py#L46)：`agent.long_term.count`（无 scope)。

### 1.2 缺口：记忆语义未按 instance 隔离

阶段 E 实现了「Prompt/状态/声音不串」（M3）,但**记忆仍是全局单例**:

1. **C4 注入全局记忆**：`MemorySection.search(query_text)` 对**所有角色实例**返回同一 pool——不同角色看到对方的记忆。
2. **memory 工具全局读写**：`search_memory`/`save_memory` 经 `ctx.get("memory")` 裸单例,无 scope 过滤。
3. **consolidator 全局合并**：`_compare_and_resolve` 查询全局候选,新提取记忆与**他角色**的记忆比对合并——跨角色污染。且 buffer 全局——instance A 的回合可能累积进 instance B 的 buffer,届时以 B 的 scope 写入。
4. **memory_reflector 全库聚类**：`_cluster_entries(get_all())` 对所有实例聚类,merge/delete/update 可能误操作他 scope 条目;计数器共享导致触发频率错乱。
5. **无 scope 元数据**：Milvus 记录无 `user_id`/`character_instance_id`/`visibility` 字段,`search` 无法在 ANN 阶段过滤。

### 1.3 M3 语义的验收（架构稿 §8）

架构稿 §8 定义目标态：所有 LTM 公共写/读 API 必须接受不可省略的 scope；生产代码无「漏传即全局」的默认值；ANN 阶段用 filter 而非先召回再 Python 过滤；consolidator buffer 属于 CharacterInstance；reflector 每 instance 独立计数。

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

**严格约束（已确认决策 #2）**：底层 `LongTermMemory` 的 `search/insert/update/delete` **移除无 scope overload**——签名必须带 `scope`（或等价地,只能经 `for_scope()` 访问）。不存在「调用方不传就查全局」的路径。`get_all()` / `count` 同样收窄为**仅 admin 用途**：产品路径一律经 `ScopedLongTermMemory.get_all()`（仅当前 scope）。

### 2.3 存储层（架构稿 §8.3）

- 新建 `long_term_memory_v2` collection（已确认决策 #1）。
- 每条至少含：`entry_id`/`user_id`/`character_instance_id`/`visibility`/`conversation_id`(可空)/`source_message_ids`(可空)/`text`/`type`/`poignancy`/`timestamp`/`schema_version=2`/`embedding`。
- **ANN 阶段用 filter**：`user_id == "..." && character_instance_id == "..." && visibility == "character_private"`。
- **内存 fallback 同样先按 scope 过滤**（架构稿 §8.3 明确）。`_search_milvus_candidates` 的 pk/text 匹配须对 `_entries` 先按 scope 过滤再匹配（双路径一致）。
- **`LongTermMemory` 底层 repo 指向 v2**：`self._long_term` 的 `repo` 必须用 `MemoryRepositoryV2`（非 v1）。阶段 E 的 `agent.py:178 repo=self._memory_repo` 需改指向 v2 repo。
- Debug 全库操作放在显式 admin API,不能借用产品 MemoryView。

### 2.4 各消费点迁移

| 消费点 | 现状 | 迁移目标 |
|--------|------|---------|
| **C4 MemorySection** | `search(query_text, top_k)` 无 scope | 接收 scoped view（**duck-typed,不 import core.memory**,维护 Module 边界）;`search` 经 view 带 scope |
| **memory 工具插件** | `ctx.get("memory")` 裸单例 | 注入 `for_scope()` scoped view |
| **consolidator** | 单例,buffer 全局、compare 全局 | **per-runtime 构造**（engine 同模式）;buffer 属 instance;ExtractMemory 输入 scope;compare 只查同 scope |
| **memory_reflector** | 单例,全库聚类,计数器共享 | **per-runtime 构造**;每 instance 独立计数;`get_all()` 只返回当前 scope;merge 验证 scope |
| **agent 接线** | `ctx["memory"]` = 裸单例(在 `_lifecycle_tools.py:68`);consolidator/reflector 单例 | per-turn `svc["memory"]` = `runtime.memory_view`;consolidator/reflector per-runtime 经 registry 解析 |

### 2.5 CharacterRuntime 持有 scoped 组件

阶段 CE 占位未落地——本批在 [runtime.py](../../../../../core/character/runtime.py) 加三个持有：

```python
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

**已确认决策 #3**：`memory_view` 由 `agent` 在 `_make_runtime` 时经 `for_scope()` 构造,`CharacterRuntime` 持有。`consolidator`/`reflector` 同模式——在 `_make_runtime` 构造（各持 scoped `memory_view`），挂 runtime。

### 2.6 per-runtime consolidator/reflector 接线

`_prepare_turn` / `loop` 消费 consolidator/reflector 点改为从 runtime 解析，**且必须局部捕获**（与 loop.py:820-826 对 session/engine/reflector 的 C3/P1-10 anti-race 模式同构）：

- **`_finalize_turn` spawn `_after_response` 时急切捕获本回合 consolidator/reflector**：在 `create_task` site 把本回合 runtime 绑定为闭包局部（`_archive_final_turn` 里 `runtime = self._current_runtime` 同步读出，非 `_run_after_response` 内 lazy 读 `self._current_runtime`），再 `_after_response(user_message, public_text, session=target, engine=self._engine, consolidator=runtime.consolidator if runtime else None, memory_reflector=runtime.reflector if runtime else None)`——**不要** per-turn 重绑 `self._consolidator`/`self._memory_reflector`。原因：`_after_response` 是 fire-and-forget task（finalize.py:147），consolidate 内含 LLM await；下一 turn 的 `_prepare_turn` 若重绑 `self._consolidator`，本次 in-flight consolidation 会用到下一轮的 consolidator（错 scope）——正是本批消除的跨实例污染。急切捕获在首个 await 前固化，与 session/engine 一致。
- **loop.py:185 `self._consolidator`**：仍保留（供 `configure()` 兼容注入），但 `_after_response` 内改用参数 `consolidator`（None 回退 `self._consolidator`）。
- **loop.py:851-857 reflector 增量**：`_after_response` 内用参数 `memory_reflector`（None 回退 `self._memory_reflector`；**命名 `memory_reflector` 而非 `reflector`**，避免与 engine 的 `_reflector`（CharacterReflector）混淆——`_after_response` 已有局部 `target_reflector` 指 engine `_reflector`）。
- **agent.py:293/301** 不再构造全局单例 consolidator/reflector;`agent.consolidator`/`agent.memory_reflector` 属性改经 registry 解析（仅 debug 展示用，默认显示 default runtime;产品路径一律经 `_finalize_turn` 局部捕获的 runtime.consolidator/reflector）。
- **session_manager.bind_memory_reflector**（agent.py:309）：reflector per-runtime 后,`session.save_state()` 的 reflector counter 持久化改经 runtime.state（架构稿 §8.5「计数器进入 CharacterRuntimeState 或独立 instance state」）。

### 2.7 生命周期 flush

- **lifecycle.py:231 `_flush_consolidator`**：改为迭代 registry 所有 loaded runtime,逐一 `runtime.consolidator.flush()`（架构稿 §8.4「shutdown 对所有 loaded runtime 逐一 flush」）。
- **debug/info.py:203/291/300**：consolidator/reflector 面板改经 registry（或仅显示 default runtime）——panic over 单例已不存在时,需调整读取源头。

## 3. 架构方案

### 3.1 分层

```
core/memory/
├── scope.py               # MemoryScope + MemoryVisibility 领域模型（新增）
├── long_term.py           # LongTermMemory: 加 for_scope() + 严格 scope 参数（改造）
├── scoped_long_term.py    # ScopedLongTermMemory: 绑定视图（新增）
├── consolidator.py        # MemoryConsolidator: scope 感知（改造,接受 scoped view）
└── memory_reflector.py    # MemoryReflector: scope 隔离（改造,接受 scoped view）

core/storage/
├── memory_repo.py         # MemoryRepository: 现 v1（保留/退役标记）
└── memory_repo_v2.py      # MemoryRepositoryV2: 指向 long_term_memory_v2 + scope 字段（新增）

core/character/
└── runtime.py             # CharacterRuntime: 加 memory_view/consolidator/reflector 持有（改造）

core/cognitive/
├── loop.py                # _prepare_turn: 经 runtime.memory_view 注入 svc["memory"]（改造）
└── _consolidate/reflector 调用点: 经 runtime 解析（改造）

core/agent/
├── agent.py               # _make_runtime: 构造 for_scope() view + per-runtime consolidator/reflector（改造）
├── _lifecycle_tools.py    # svc["memory"] 移除裸单例注入（改造）
└── lifecycle.py           # _flush_consolidator: 迭代 registry runtimes（改造）

Module/system_prompt/sections/memory.py   # C4: 接收 scoped view（duck-typed,不 import core）（改造）
```

### 3.2 关键数据流

```
_make_runtime(instance_id, character_id)
  → memory_view = self._long_term.for_scope(MemoryScope(user_id, instance_id))
  → consolidator = MemoryConsolidator(llm, memory=memory_view, ...)   # scoped view
  → reflector = MemoryReflector(llm, memory=memory_view, ...)         # scoped view
  → CharacterRuntime(..., memory_view, consolidator, reflector)

_prepare_turn(runtime)
  → svc["memory"] = runtime.memory_view        # memory 工具已 scope 过滤
  → self._current_runtime = runtime            # 供 _finalize_turn 局部捕获 consolidator/reflector
  → C4 MemorySection.search(query)             # 经 runtime.memory_view（已 scope）

_finalize_turn  spawn _after_response（create_task site 急切绑定 runtime 局部）
  → _after_response(..., session=target, engine=engine,
                    consolidator=runtime.consolidator, memory_reflector=runtime.reflector)  # 急切捕获
```

**核心不变式**：任何记忆写/读都经过 `ScopedLongTermMemory` 视图——`scope` 在视图创建时绑定,调用方无法在调用点绕过。consolidator/reflector 也各持 scoped view,不受全局单例污染。**anti-race**：`_after_response` 用**局部捕获**的 consolidator/reflector（首个 await 前固化）,而非 per-turn 重绑 `self._consolidator`——consolidate 的 LLM await 期间下一 turn 不会换掉本次 consolidator 的 scope。

### 3.3 Scope 绑定时机

- 视图在 `_make_runtime`（registry 创建 runtime）时构造,scope 固定为实例的 `(user_id, character_instance_id)`。
- runtime 被 LRU 驱逐重建时,`_make_runtime` 重建 view + consolidator + reflector——scope 不变,天然幂等。
- **策略**：Elysia 默认实例的 view 与生产路径一致,无特殊优待。

### 3.4 memory_view 构造注入点

`_make_runtime` 现由 agent 持 `runtime_factory` 调用（agent.py:226-264）。本批在 factory 内构造三组件：

```python
def _make_runtime(self, instance_id: str) -> CharacterRuntime:
    scope = MemoryScope(user_id=self._user_identity.userId, character_instance_id=instance_id)
    memory_view = self._long_term.for_scope(scope)
    rt = CharacterRuntime(instance=..., definition=..., revision=..., user_identity=...,
                          memory_view=memory_view,
                          consolidator=MemoryConsolidator(llm=self._llm, memory=memory_view, ...),
                          reflector=MemoryReflector(llm=self._llm, memory=memory_view, ...))
    rt.set_persist_fn(self._persist_runtime_factory(rt))
    return rt
```

（`self._user_id` 从 `self._user_identity`（agent.py:124, `UserIdentity.userId`）解析——阶段 E 的 `usr_local_default`。注意 `_make_runtime` 是 `agent.__init__` 内的闭包,捕获 `self._user_identity`;`user_id` 在 runtime 创建时固定——单用户 companion 下每个 instance 一个 user_id,不需按 user 重键。）

## 4. 错误处理

| 场景 | 行为 |
|------|------|
| 无 scope 调用 `search/insert` | `TypeError`（strict overload 签名强制） |
| scope 过滤后无结果 | 返回 `[]`,不全局回退 |
| Milvus 离线 | 内存 fallback 按 scope 过滤后正常返回（优雅降级） |
| merge 误幻觉他 scope entry ID | repository 返回 not found,反射器输出失败被丢弃 |
| `memory_view`/`consolidator`/`reflector` 为 None | C4 返回 `""`（不注入记忆）;`ctx["memory"]` 为 None → 工具报「记忆系统不可用」;consolidator 为 None → 跳过整合 |
| runtime LRU 驱逐后某 runtime 的 buffer 未 flush | registry `flush_all` 迭代所有 loaded runtime;驱逐前先 flush（`_evict_if_needed` 已保护 busy/dirty） |

## 5. 测试策略

沿用分层 marker。新增/改造重点：

- **scope.py**：MemoryScope 不可变 + visibility 默认 character_private。
- **long_term.py**:`for_scope()` 返回正确 scope view；无 scope 调用 raise；`get_all`/`count` 收窄为 admin。
- **scoped_long_term.py**:insert/search/update/delete 全部带 scope;不同 scope 互不可见;空 scope 无全局回退;`get_all()` 只返回当前 scope。
- **storage v2**：ANN filter 正确(`user_id+character_instance_id+visibility`);**内存 fallback 按 scope 过滤**——`_search_milvus_candidates` 的 pk/text 匹配先 scope 过滤 `_entries`;`_load_from_milvus` 重启**全量加载后经 view 过滤**（不选「只加载当前 scope」——否则其他 scope 视图为空,破坏多实例共存）。
- **C4**:传入 scoped view 后只返回当前 scope（duck-typed,不 import core）。
- **工具**:注入 scoped view 后 `search`/`save` 只影响当前 scope。
- **consolidator**:不同 instance 的 buffer 独立;compare 只查同 scope;per-runtime 构造后无全局 buffer 污染。
- **reflector**:`get_all()` 只返回当前 scope;merge 验证 scope;**恶意 LLM 输出他 scope entry ID → repository 拒绝**（架构稿 §18.4 item 5）;计数器 per-instance 独立。
- **agent 接线**:`svc["memory"]` 是 scoped view 而非裸单例;`_make_runtime` 构造 per-runtime consolidator/reflector。
- **现有无 scope 调用点 sweep**：所有调 `.search/.insert/.update_text/.get_all` 无 scope 的既有测试(`core/memory/tests/test_long_term.py`、`test_consolidator.py`、`test_memory_reflector.py`、`core/tools/tests/test_memory.py`)与 `test_public_interface.py:34-35`(`consolidator`/`memory_reflector` 属性映射)需迁移到 scoped 调用/新属性。grep-based「生产代码无无 scope `.search(`/`.insert(`」作为 exit 检查。
- **snapshot 回归**：`test_elysia_snapshot_unchanged`（18127B）——C4 对 default elysia 单角色行为不变（scoped view 注入后返回空,posture 不依赖真实 view）。

## 6. 明确不做（defer）

- **`user_shared` 实际升级路径**：本批只定义枚举,不提供自动升级。
- **Neo4j 关系事实迁移**（架构稿 §8.6）：Neo4j 缺失不阻塞本批。
- **旧 `long_term_memory` 数据迁移**：阶段 E 已清旧会话,空库起步;不迁移。
- **Debug 全库 admin API**：`MemoryRepository.query(filter_expr=None)` 保留供 debug,但不作为产品 MemoryView 借用。**注**：debug DatabaseTab LtmPanel 目前读 v1 `long_term_memory`;产品路径本批迁 v2 后,debug 面板会显示空/旧库——**本批记录为已知行为差异**（debug LTM panel 改读 v2 视为阶段 H 前端联调范围,或在 debug 层经 registry 解析 v2）。
- **前端 Electron 联调**。

## 7. 验收清单（exit conditions）

1. **MemoryScope 模型**定义于 `core/memory/scope.py`,不可变,默认 `character_private`。
2. **`LongTermMemory.for_scope(scope)`** 返回正确 `ScopedLongTermMemory`;底层 `search/insert/update/delete` **无 scope overload**;`get_all`/`count` 收窄为 admin 用途。
3. **`ScopedLongTermMemory`** 的 insert/search/update/delete 全部作用域过滤;不同 `character_instance_id` 互不可见。
4. **`long_term_memory_v2`** collection 建立;**`LongTermMemory` 底层 repo 指向 v2**;ANN 用 `user_id + character_instance_id + visibility` filter;内存 fallback 按 scope 过滤。
5. **C4 / memory 工具** 全部经 scoped view 读写,无全局单例泄漏（`_lifecycle_tools.py:68` 不再注入裸单例）。
6. **consolidator / reflector per-runtime 化**：buffer 属 instance、计数器 per-instance、compare 只查同 scope、merge 验证 scope;`_flush_consolidator` 迭代 registry runtimes;`_after_response` 用**局部捕获**的 consolidator/reflector（anti-race,首个 await 前固化,不 per-turn 重绑 `self._consolidator`）。
7. **`CharacterRuntime` 持有 `memory_view`/`consolidator`/`reflector`**;agent `_make_runtime` 构造 scoped 三组件。
8. **`MemoryEntry` 模型带 scope 字段**（`user_id`/`character_instance_id`/`visibility`）——`_search_fallback`/`_search_milvus_candidates` 过滤 `_entries` 需要;`_load_from_milvus` 重启**全量加载后经 view 过滤**（不选「只加载当前 scope」——否则其他 scope 视图为空,破坏多实例共存）。
9. **Elysia snapshot byte 不变**（18127B）。
9. **ruff/pyright/contract --check** 全绿。

## 8. 引用

- 架构稿 §8：`docs/2026-08-30-character-prompt-customization-backend-architecture-design.md`
- 架构稿 §3.2 不变量 #5：任意记忆写入/查询必须显式带 MemoryScope,无「漏传即全局」默认值。
- 架构稿 §18.4：记忆隔离测试（8 项,含「恶意 LLM 输出他 scope ID → 拒绝」）。
- stage CE spec：`docs/superpowers/specs/archive/2026-09/2026-09-03-character-stage-ce-runtime-domain-design.md`（§7 ScopedMemory 占位指针）
