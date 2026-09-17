# Character — 角色域（领域模型 + 运行时容器）

> 文档导航：[项目文档](README.md) → [Core 层](../README.md)

角色域的领域模型 + 多角色运行时。在现有 `character/{name}/config.yaml` + `personality.md` 之上渐进演进，不重写 manifest v2。

**当前阶段（2026-09-08）：** 阶段 A–G 的后端实现已落地，阶段 H 的后端代码与前后端契约对齐已完成；真实 `Agent` 现在发布 `character_profiles=true` 与 `character_state_stream=true`。Custom Character 已可经 canonical instance binding 进入真实聊天；Engine、状态、Prompt 与长期记忆均按 instance 隔离。Windows Electron 真机连接与交互冒烟仍是本轮发布验收项，不由本文档宣称已通过。

演进背景：`stage-ce` 建立领域模型与运行时容器骨架，`stage-d` 增加 Custom Character CRUD，阶段 E 将 Engine 重构为 per-instance 的 M3 目标态并移除 `attach_session` 等换绑耦合，阶段 F 完成记忆作用域隔离，阶段 G/H 补齐产品状态、角色身份 wire、真实 custom runtime、版本传播与前端门禁。

## 文件布局

```
core/character/
├── definition.py         # CharacterDefinition — 稳定角色身份（架构稿 §5.2）
├── revision.py           # CharacterRevision — 不可变角色设定快照（§5.3）
├── instance.py           # CharacterInstance — 演化体 + revision policy/pending revision（§5/§7.5）
├── draft.py              # CharacterDraft + ConversationExample — 用户提交草稿（双层校验，阶段 D）
├── service/              # CharacterService 包 — CRUD + Revision/状态策略传播 + CAS（阶段 D/H；#44 按写入链拆分）
│   ├── __init__.py       #   唯一装配点：三链混入 + 基类 → CharacterService，并逐名重导出（导入路径零变化）
│   ├── errors.py         #   CharacterError 层级（路由按异常类型映射 wire 错误码）
│   ├── helpers.py        #   模块级纯函数（游标/校验和/归一化/wire 构造）+ 三链共享常量
│   ├── base.py           #   共享管道：__init__ / 连接 / 取定义 / 草稿两层校验 / 草稿→Revision
│   ├── queries.py        #   读链：get / list / list_revisions / resolve_runtime_source / preview
│   ├── definitions.py    #   定义写链：create / update / clone / archive / rollback
│   └── instances.py      #   实例/状态写链：reset_evolution / reconcile / 传播 / pending / busy 判定
├── builtin_source.py     # BuiltinCharacterSource — 从 character/{name}/config.yaml 构建内置角色
├── catalog.py            # CharacterCatalog — 只读内置角色目录（无 Mongo 也能列/校验）+ get_revision
├── runtime.py            # CharacterRuntime — 单实例运行时容器（快照 + 懒 engine + memory_view/consolidator/reflector 持有 + lock/lease 计数）【阶段 E/F 激活】
├── registry.py           # CharacterRuntimeRegistry — LRU 缓存（受保护状态 = _busy）+ release/close_all（含 flush 任务 drain）【阶段 E 激活】
└── tests/
    ├── test_models.py
    ├── test_builtin_source.py
    ├── test_binding.py
    ├── test_draft.py
    ├── test_service.py
    ├── test_service_stage_h.py
    ├── test_runtime.py
    ├── test_runtime_loader.py
    ├── test_runtime_profile.py
    ├── test_registry.py
    ├── test_session_binding.py
    └── test_public_state.py
```

> 相关配套：`CharacterRuntimeState`（instance-scoped 状态模型）在 [core/schema/character.py](../schema/character.py)；`CharacterInstanceRepository`（`character_instances` 集合持久化 + `state_version` CAS）在 [core/storage/character_instance_repo.py](../storage/character_instance_repo.py)。

## 核心概念

### CharacterDefinition / CharacterRevision / CharacterInstance（架构稿 §5）

- **Definition**：稳定角色身份（characterId/slug/source/status），不存 Prompt 正文/状态/session。
- **Revision**：不可变角色设定快照（displayName/promptProfile/runtimeDefaults/checksum）。本批 `promptProfile` 承载有源字段（`corePersonality`/`speakingStyle`/`relationshipStyle`/`emotionStyle`/`conversationInitiative`/`examples`）；内置角色仍只填 `corePersonality`/`speakingStyle`（其余渲染走模板）。
- **Instance**：用户正在相处并持续演化的角色（instanceId/characterId/revision/stateVersion）。**阶段 E** 加 `state: CharacterRuntimeState | None` 字段——instance-scoped 状态权威的持久化落点（`stateVersion` 作 CAS 乐观锁）。

### CharacterDraft + CharacterService（阶段 D：Custom CRUD）

- **CharacterDraft**（[draft.py](draft.py)）：用户提交的完整角色表单（custom 唯一入口）。双层校验：第一层 Pydantic 字段边界（displayName 1-64 等，§8.1）；第二层 `validate_semantic`（保留标签 `<conflict_rules>` 等 / Jinja 分隔符 / NUL → hard error；关键字启发式 → warning）。`examples` 用 `ConversationExample` 子对象（每组 user/assistant ≤1000 字符）。
- **CharacterService**（[service/__init__.py](service/__init__.py)，#44 起为**按写入链拆分的包**：读链 `queries.py` / 定义写链 `definitions.py` / 实例状态写链 `instances.py` + 共享 `base.py`，`__init__.py` 是唯一装配点）：create/get/list/update/clone/archive/rollback/validate 业务编排。**Revision-first 写序**（先 `append_revision` 再 `cas_revision_pointer`，CAS 失败回滚已 append revision——`current_revision` 永远指向 durable revision，架构稿 §10.1）；`create`/`update`/`rollback` 均产生新 Revision（append-only，不删历史）；`list`/`get` 合并内置（catalog）与 custom（repo）。阶段 H 中，update/rollback 还会枚举相关 instance：`follow_latest` 原子推进到新 revision，`pinned` 保持原 revision；`preserve` 保留演化态，`reset_evolution` 通过 `stateVersion` CAS 重置并审计。中断的跨集合传播由 pending 字段与 runtime 懒加载对账收敛。
  - **归档一致性（P1）**：`archive()` 先在有限 CAS 重试内把 `UserIdentity.defaultCharacterId` 改到可用回退角色、**再**归档——反序会在 profile 冲突/存储失败时留下「默认角色仍指向已归档角色」的悬空引用。profile 更新失败或重试耗尽 → 整体失败，角色保持 `active`（可恢复）；归档的不是默认角色时不触碰 profile。
  - **无状态 preview（BE-STAGEH-03）**：经 `CharacterPreviewPort` 组装一次性 Prompt（无会话/无记忆/无工具）+ 单次推理，零持久化副作用；权威身份不可用时 503（P0-3 fail-closed）。
- **CharacterRepository / CharacterRevisionRepository**（[core/storage](../storage/README.md)）：`characters` + `character_revisions` 集合。snake_case 落库（`character_id`/`current_revision`），CAS 用 `modified_count>0` + `$set` 恒定含必变字段 `updated_at`（后端只暴露 `modified_count`，对齐 `idempotency_repo.py` 先例）。
- **能力边界**：真实 `Agent` 已在四道隔离门禁通过后发布 `character_profiles=true`；轻量 facade/test double 若未实现该能力属性，`build_server_info` 仍 fail-closed 为 `false`。启用能力后，generation、expression、Live2D 与 TTS 起始帧使用完整角色身份三元组，客户端不得回退猜测“当前角色”。

### BuiltinCharacterSource（渐进式）

从现有 `character/{name}/config.yaml` + `personality.md` 构建内置角色（`corePersonality` 从 personality.md、`speakingStyle` 从 config personality.speaking_style），**不重写 manifest v2**。

### CharacterRuntimeState（阶段 E，instance-scoped 权威状态）

[core/schema/character.py](../schema/character.py) 的 `CharacterRuntimeState`——**扁平** Pydantic 模型，与旧 `CharacterState` **字段同构**（子组件 0 改动），仅做两处身份/语义调整：

- **`instance_id`**：instance-scoped **权威身份**。
- **`session_id`**：**保留**作版本历史键（`CompressionManager` 版本历史 `append_version` 读 `state.session_id`，见 compression.py:248/332——保留使子组件扁平字段读取 0 改动，避免 AttributeError）。
- `source_revision`：记录状态基于哪版 L1 演化。

承载 L2/L3/L4 + awareness/compression/reflector + mood。**`CharacterState` 标记退役**（保留兼容导入，不物理删）；`session.character_state` 不再权威——新会话用 `CharacterRuntimeState`。

### CharacterRuntime（阶段 E 激活，[runtime.py](runtime.py)）

单实例运行时容器，装载 `Definition`/`Revision`/`UserIdentity` 快照 + `CharacterRuntimeState`。**M3：engine 懒建为 per-instance**（`ensure_engine(factory)` 真绑定，非 M1/M2 占位；`bind_engine` 注入）；`snapshot_state()` 返回真实 `CharacterRuntimeState`（deep copy）；`flush()` 经 `persist_fn` 把 `runtime.state` 持久化到 `CharacterInstance.state`。持有 per-instance `Lock` + generation/破坏性变更 lease 计数（`_busy`）+ `stateVersion`。**状态权威：`runtime.state`**——engine 构造时传 `runtime.state`（与 runtime 共享同一引用），`_apply_mood`/`_finalize_turn` 写/读 `engine.state` 即 runtime.state（F1-b 正确语义：engine per-instance 永不换绑消除跨会话污染风险源）。

**阶段 F（记忆 v2 隔离）**：`CharacterRuntime` 新增三持——`memory_view`（`ScopedLongTermMemory | None`）、`consolidator`（`MemoryConsolidator | None`）、`reflector`（`MemoryReflector | None`）。**已确认决策 #3**：这三个组件由 `agent._make_runtime` 在构造 runtime 时经 `for_scope()` 构造并挂入（`memory_view = self._long_term.for_scope(MemoryScope(user_id, instance_id))`；consolidator/reflector 各持 scoped view），`CharacterRuntime` 仅持有——scope 在视图创建时固定，调用点无法绕过。consolidator/reflector per-runtime 化后**无全局单例**；`_prepare_turn` 经 registry 解析当前 runtime 的组件，产品路径一律经 `_finalize_turn` 局部捕获（anti-race，首个 await 前固化，避免下一 turn 换绑导致错 scope 污染）。

**操作 lease（BE-STAGEH-01 + P0-1）**：`try_begin_generation()` / `begin_generation_tail()` / `try_begin_destructive_mutation()` / `end_generation()` / `end_destructive_mutation()` 由一把同步 `_op_guard`（`threading.Lock`）保护「判定 + 登记」同一原子步——`asyncio.Lock.acquire()` 是 await 点，会让检查与登记之间被插入。语义：generation 在途（含主回合 + tail 收尾）时 reset 立即 `CharacterBusyError`（409）；破坏性变更在途时不得开始 generation（对称互斥）；反思/压缩锁被持有也算 busy（`evolution_locks_busy()`）。**tail lease** 覆盖 `_after_response`（记忆沉淀/性格反思/MemoryReflector/会话保存）——主回合 `process()` 返回后仍维持 busy，直到 tail task 的 `finally` 释放。lease 是 per-instance 的，不引入全局领域锁。`apply_user_address(address)` 把本轮身份的有效称呼投影到三个记忆组件（P0-2），不修改 `runtime.user_identity`。

### CharacterRuntimeRegistry（阶段 E 激活，[registry.py](registry.py)）

`get(instance_id, factory)` 按 instance_id 解析/懒建 per-instance runtime（factory 从 agent 注入，构造 instance/defn/revision/user_identity + 经 `CharacterInstanceRepository.get(instance_id)` 加载 `CharacterInstance.state`；为空时空态起步）。LRU（`OrderedDict` + `move_to_end`，`max_size=32`）驱逐前检查受保护状态：

- `_busy`：generation / 反思 / 压缩在途（含 tail lease）不驱逐；全部受保护时提前退出，不强制牺牲。由 `CharacterRuntime` 的 lease 维护（BE-STAGEH-01 起）。
- **驱逐前必 flush**：选中者先调度 `rt.flush()` 落库（`_flush_tasks` scope 持有，`close_all()` 时 drain）——`flush()` 无条件经 `persist_fn` 落库，所以没有"未落库就丢"的窗口。**注意**这不是"dirty 则不驱逐"，而是"驱逐一定伴随落库"。

另有 `mark_busy`（test-only 的绕过 lease 显式覆盖口）/`invalidate`/`release`/`close_all`（关机 flush 全部走这里，A1——非单 engine）。

### CharacterInstanceRepository（阶段 E 新增，[core/storage/character_instance_repo.py](../storage/character_instance_repo.py)）

`character_instances` 集合的 instance CRUD + `state_version` CAS（fail-closed）。`get(instance_id)` → `CharacterInstance | None`（含 `state: CharacterRuntimeState`）；`save(instance)` upsert；`cas_state_version(instance_id, expected, new_state, new_version)` 用 `state_version==expected` filter 做乐观锁。**区别于** `CharacterRepository`（后者是 `characters` 集合 + CharacterDefinition CRUD，无 instance 能力）。

### CharacterRuntimeRegistry 的 M3 生命周期接入（agent 侧）

`Agent.__init__` 不再 eager 构造全局 `_character_engine`——改注册 `CharacterRuntimeRegistry` + 默认会话 binding `{character_id: chr_builtin_elysia, instance_id: chi_builtin_elysia}`；预 seed 默认 Elysia runtime + engine 入 registry（PromptAssembler 快照路径）。`agent.character_engine`/`character_reflector` 经 registry 按当前会话 binding → instance_id → runtime.engine 解析（不再是全局单例）。`_start_persistence` 把持久化 `CharacterInstance.state` 装载到 runtime.state + engine（原地按字段更新共享 state 引用）；`_stop_persistence` 经 `registry.close_all()` 逐 runtime flush 落库。

### 去 attach_session（阶段 E，M3）

`engine.attach_session` / `engine.sync_reflector_to_state` / `session_manager.bind_character_engine` / `session._pre_save_hook` **全部删除**。reflector buffer 落盘改由 runtime 在回合边界统一持久化到 `CharacterRuntimeState`（`engine.sync_reflector_to_runtime_state`），不再借 `session.save()` 的 hook。会话切换不再有「单例 engine attach 新 session」的换绑动作——engine per-instance 永不换绑。

### 旧数据迁移（架构稿 §15.3-15.8）

**产品决策：开发阶段不迁移旧数据，直接上新，旧数据在部署时清库。** 因此 §15.3-15.8 的影子迁移设计不执行：不建立 legacy instance、不复制 `character_state` 到 instance、不做 character_versions / 旧 LTM `legacy_unscoped` / pending 迁移。`core/character/migration.py` 及其测试作为纯死代码（零生产引用）已删除。旧会话/旧集合的清理交由部署侧完成，代码内不写迁移/重置逻辑。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../README.md) | 快速开始 + 完整文档导航 |
| [Core 层](../README.md) | Core 层总览 |
| [Profile 包](../profile/README.md) | UserIdentity 用户最小身份 |
| [阶段 E Spec](../../docs/superpowers/specs/archive/2026-09/2026-09-04-character-stage-e-runtime-binding-design.md) | CharacterRuntime 与会话绑定设计（M3：engine per-instance + RuntimeRegistry + CharacterRuntimeState） |
| [阶段 E Plan](../../docs/superpowers/plans/archive/2026-09/2026-09-04-character-stage-e-runtime-binding-plan.md) | 阶段 E 实施计划（14 Task） |
| [阶段 F Spec](../../docs/superpowers/specs/archive/2026-09/2026-09-05-character-stage-f-memory-v2-isolation-design.md) | 记忆 v2 隔离设计（CharacterRuntime 持 memory_view/consolidator/reflector） |
| [阶段 F Plan](../../docs/superpowers/plans/archive/2026-09/2026-09-05-character-stage-f-memory-v2-isolation-plan.md) | 阶段 F 实施计划 |
| [架构稿](../../docs/2026-08-30-character-prompt-customization-backend-architecture-design.md) | 角色域完整设计（§5 领域模型 / §7 运行时 / §15 迁移——**§15 因「不迁移旧数据」决策不执行**） |
