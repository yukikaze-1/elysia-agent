# 阶段 E：CharacterRuntime 与会话绑定 Spec

> **状态：** 方案 A（Engine per-instance + RuntimeRegistry 权威 + 状态迁移）已定案；三个命门已确认（状态权威直接切 instance-scoped 旧会话清除 / 彻底删除 attach_session / 预埋 memory scope 字段）
> **架构稿：** [`docs/2026-08-30-character-prompt-customization-backend-architecture-design.md`](../../../../2026-08-30-character-prompt-customization-backend-architecture-design.md)（§5.7 Instance / §5.8 RuntimeState / §5.9 ConversationBinding / §7 运行时与回合生命周期 / §15 迁移）
> **fact 基线：**（阶段 D 完成后 HEAD `4089d30f`；阶段 E 基于此推进）
> **上一批：** 阶段 D（Custom Character 持久化 + CRUD + 版本 + CAS），`character_profiles=false` 恒抑制
>
> **明确排除：** custom 参与真实聊天（`character_profiles=false` 恒抑制 wire）；记忆 v2 隔离（阶段 F，本批仅预埋 scope 字段）；旧会话 shadow migration / legacy instance（本批旧会话清除——pre-production 无历史数据需保留）；CharacterPublicState 实际发送（阶段 G）；前端 Electron 联调（阶段 H）
>
> 本批不迁移任何旧数据。阶段 E 建立**全新**的 instance-scoped 运行时状态，新会话从空态起步。

## 0. 一句话目标

把角色运行时从「**单例 CharacterEngine + session-attach 状态换绑**」（M1/M2）重构为「**Engine per-instance + RuntimeRegistry 解析 + CharacterRuntimeState 权威(instance-scoped)**」（M3 目标态，架构稿 §7）。让两个角色可在不同会话生成且 **Prompt / 状态 / 声音不串**。

## 1. 背景与问题

### 1.1 阶段 C+E / D 交付的底座（已核实）

阶段 C+E（`main@24b42ae7`）+ 阶段 D（`main@4089d30f`）已交付：

- **领域模型**：`CharacterDefinition` / `CharacterRevision` / `CharacterInstance`（§5.2/5.3/5.7 精简版）+ `TurnCharacterContext`（frozen dataclass，含 conversationId/characterInstanceId/characterId/characterRevision/userPronoun）
- **Runtime 骨架**：`CharacterRuntime`（core/character/runtime.py，M1/M2 占位——engine 懒建但保持单例、state 占位不写权威）+ `CharacterRuntimeRegistry`（registry.py，`get(instance_id, factory)` LRU + `_evict_if_needed`（dirty/busy/session-liveness 保护）+ `_busy`/`_session_binding_index` 为 M3 预留 inert）
- **持久化**：`CharacterRepository` / `CharacterRevisionRepository`（Mongo characters + character_revisions 集合，阶段 D）+ `CharacterService`（create/get/list/update/clone/archive/rollback/validate + CAS）
- **feature flag**：`character_profiles=false` 恒抑制（capabilities.py:107 + ws_message.py:275）
- **`UserIdentity.pronouns`** 为 B1 全局唯一「她」

### 1.2 缺口：M3 语义未启用（引擎单例 + 状态 session-scoped）

阶段 C+E / D 的关键限制（阶段 D spec 明确排除项）：

1. **引擎单例**：`agent.__init__`（agent.py:206）构造**单个** `CharacterEngine(state=default.character_state)`，`bind_character_engine` 全局。`_prepare_turn`（loop.py:353）每次 `attach_session(target)` 换绑 state 视图——**角色身份不变化**(M1/M2 shim)，但引擎是同一个。
2. **状态 session-scoped**：`session.character_state`（`CharacterState`，以 `session_id` 为身份，扁平字段）仍是**权威写盘目标**。`attach_session` 经 `session._pre_save_hook` 注册 `sync_reflector_to_state`，使 reflector buffer 在 `session.save()` 时落盘到 `character_state`。
3. **TurnCharacterContext 内省引擎**：`_prepare_turn` 的 `characterInstanceId` 从 `engine._instance_id` 内省（loop.py:362 注释明确「本批不读 session.character_binding 解析 instance，M1/M2 inert」）。
4. **generation/message 无角色审计**：`GenerationContext`（generation.py）只有 generation_id/assistant_message_id/conversation_id，无 characterId/instanceId/revision；`frontend_contract` 的 `GenerationStartedMessage`/`GenerationCompletedMessage`/`GenerationFailedMessage` 也**无角色审计字段**（§12.3）。

### 1.3 M3 语义的验收（架构稿 §7）

架构稿 §7.2/7.3/7.5 定义目标态：

- `CharacterEngine` 构造时绑 `CharacterInstance`，生命周期内不再改变角色身份；
- 删除或废弃 `attach_session()`；
- 子组件从同一 runtime 获得 character/user/revision/state；
- 状态写入由 runtime/service 统一持久化，不由 section 或路由直接写 repository；
- `_prepare_turn` 流程：resolve conversationId → load Session + character_binding → load UserIdentity snapshot → registry.get(instanceId) → resolve active CharacterRevision → create scoped MemoryView → snapshot RuntimeState + stateVersion → build immutable TurnCharacterContext → register GenerationContext with character audit fields。

## 2. 交付边界

### 本批做

1. **`CharacterRuntimeState`（instance-scoped，扁平迁移）**：新 Pydantic 模型（与 `CharacterState` 字段同构——**保留 `session_id`**（供 CompressionManager 版本历史 append_version 用，compression.py:248/332）+ **新增 `instance_id`**（instance-scoped 权威身份）+ `source_revision`）。承载 L2/L3/L4/awareness/compression/reflector/mood——**子组件真 0 改动**（扁平字段读取含 compression 的 `.session_id` 不受影响）。§5.8「删 session_id 作唯一身份」的实现方式为「新增 instance_id 权威身份、session_id 保留作版本键」，二者不冲突。
2. **`CharacterInstance.state` 持久化落点 + `CharacterInstanceRepository`**：`CharacterInstance` 加 `state: CharacterRuntimeState | None`；**新增** `CharacterInstanceRepository`（`character_instances` 集合，instance CRUD + `state_version` CAS）——阶段 D 的 `CharacterRepository` 是 `characters` 集合 + CharacterDefinition CRUD，无 instance 能力；本批新增 instance 持久化层（修正新问题 #2）。
3. **`CharacterRuntime` 激活**：engine 懒建为 per-instance（`bind_engine` 真绑定，非 M1/M2 占位）；`snapshot_state()` 返回真实 `CharacterRuntimeState`；`flush()` 持久化到 `CharacterInstance.state`（经 `CharacterInstanceRepository`）。
4. **`CharacterRuntimeRegistry` 权威**：`get(instanceId)` 解析/懒建 per-instance runtime+engine；`_busy`/`_session_binding_index` 激活用于并发/驱逐保护；`runtime_registry` 注入 `CognitiveLoop`。
5. **去 `attach_session`**：彻底删除（含 `_pre_save_hook`/`sync_reflector_to_state`/`bind_character_engine` 耦合，覆盖 `session.py`/`session_manager.py`）；reflector buffer 落盘改由 runtime 在回合边界统一持久化到 `CharacterRuntimeState`，不再借 `session.save()` hook。
6. **`_prepare_turn` 回合解析重构**：从 `session.character_binding` 解析 instanceId → registry.get(instanceId) 取 runtime engine；不再 attach_session；统一 `self._engine`（替代 `_character_engine`，覆盖 streaming/finalize/debug）；TurnCharacterContext 从 runtime 冻结。
7. **`TurnCharacterContext` 扩展**：加 `memoryScope`（= instanceId，预埋 F）+ 角色 voice/displayName（供 TTS/mood 消费）+ `stateSnapshot`（frozen CharacterRuntimeState）。
8. **generation/message 角色审计字段**：`GenerationContext` 加 characterId/instanceId/revision；frontend_contract 三个 generation 消息加可选审计字段（flag 仍 false 时可选）。
9. **mood 写读链重定向**：`streaming._apply_mood`/`finalize` 的 mood 读写从 `target.character_state` 改到 `self._engine.state`（实例权威）；**读取端**（`ws_handler.py:235-245` v1 connect / `debug_routes/archive.py:244` / `debug/info.py:302,175`）同步改读 `engine.state`（修正新问题 #3）。
10. **session 绑定**：`SessionMemory.character_binding` 真正接线——会话创建时 `(userId, characterId)` 取/建 canonical instance 并持久化 binding；激活/重连从 binding 解析角色；默认会话初始 binding → elysia instance。
11. **`CharacterState` 退役标记**：旧 session-scoped 状态（`session.character_state`）不再作为权威，保留兼容导入（不物理删）；新会话用 `CharacterRuntimeState`。
12. **Elysia 现网行为 byte 不变**：单角色默认会话（default session → elysia instance）的渲染字节不变。
13. **prompt 层 engine 路由**：`prompt_assembler._personality_engine` 不再构造时固化单例——`_prepare_turn` 经 `set_personality_engine(self._engine)` 指向本回合 engine，使 C1/C3/C6/C9 渲染副本回合角色（修正第 3 轮 review blocking——#5/#9 的 "Prompt 不串"，且移除全局 engine 后 byte-invariance 不受影响）。**补充（第 4 轮 review）**：`set_character(characterId, displayName)` 同步路由 C1 `name`（"你是 {{ name }}"）+ 模板目录（`_resolve_template` 用 `character.name.lower()`），因 `render_c1` 只 return core/self（不含 name）——否则 #9 双角色 C1 渲染错 name。

### 本批不做（明确排除）

- **custom 参与真实聊天**：`character_profiles=false` 恒抑制 wire；本批仅内置角色（elysia/cyrene）走 instance runtime。
- **记忆 v2 隔离（阶段 F）**：本批仅在 TurnCharacterContext 预埋 `memoryScope` 字段；C4/tool memory/consolidator/reflector 仍按当前 session 全局读（未真正 scoped）。
- **旧会话 shadow migration / legacy instance**：本批旧会话清除（pre-production）；`migration.py` 的 `legacy_instance_id`/`migrate_legacy_sessions` 不启用（或移除）。**前提是「确认 DB 为空」**——本批启动时验证无真实用户 session 的 L2/L3 演化需保留；若存在（生产环境误跑本批），跳过即会清除，故应在 spec 前置一个「DB 无旧 character_state 数据」的校验（grep/启动检查），否则本批仅在开发/空库环境启用。
- **`CharacterPublicState` 实际发送（§12.4）**：wire 由 flag 抑制。
- **`activate_session` 之外的会话选角/切换 UI**（§7.6 前端部分）：本批只做后端解析。
- **前端 Electron 联调（阶段 H）**：本批生成 TS/schema，不联调。

## 3. 架构方案

### 3.1 分层（方案 A）

```
core/character/
├── runtime.py       # CharacterRuntime: 持 instance/defn/revision/user_identity/runtime.state + engine(per-instance 懒建)
│                    #   state 为 CharacterRuntimeState(instance-scoped)；snapshot_state()/flush() 真实化
├── registry.py      # CharacterRuntimeRegistry: get(instanceId, factory) 解析/懒建 per-instance runtime
│                    #   _busy/_session_binding_index 激活；invalidate/release/flush_all/close_all
core/schema/character.py  # CharacterRuntimeState 定义(扁平,与 CharacterState 同构,保留 session_id + 增 instance_id)
core/character/instance.py  # CharacterInstance 加 state: CharacterRuntimeState 字段(持久化落点)
core/storage/
├── character_instance_repo.py  # CharacterInstanceRepository(新增): character_instances 集合 instance CRUD + state_version CAS
core/cognitive/
├── loop.py          # _prepare_turn: 从 session.character_binding 解析 instanceId → registry.get
│                    #   → runtime.engine；不再 attach_session；统一 self._engine(替代 _character_engine)
│                    #   registry 注入 loop 构造/configure
├── turn_context.py  # TurnCharacterContext 扩展: memoryScope + voice/displayName + stateSnapshot
├── generation.py    # GenerationContext 加 characterId/instanceId/revision
├── streaming.py     # _apply_mood 改从 engine.state 读写 mood(替代 target.character_state)
├── finalize.py      # mood 读改 engine.state；engine 引用改 self._engine
core/memory/
├── session_manager.py  # removed attach_session/bind_character_engine 调用；_pre_save_hook 移除
│                    #   会话创建/激活时从 binding 解析 instance
core/agent/
├── agent.py         # 不再构造全局单例 engine;注册 CharacterRuntimeRegistry；会话创建时建 binding
│                    #   默认会话(bootstrapped)初始 binding → 默认 elysia instance
core/input/
├── ws_handler.py    # 激活/重连从 session.character_binding 解析角色(替代 attach_session)
│                    #   + v1 connect mood 读 end 迁到 engine.state(读取端, §6.5)
core/agent/_lifecycle_persistence.py  # 启动时 registry 定位 + instance state 装载;shutdown flush_all
core/debug/info.py   # 从 engine.state 读 mood/self_perception_history(替代 session.character_state, §6.5)
core/input/debug_routes/archive.py  # params 读改 engine.state(读取端, §6.5)
core/prompt/prompt_assembler.py  # 新增 set_personality_engine(engine) + set_character(characterId, displayName)；C1/C3/C6/C9 经 _personality_engine(本回合 engine) + _name/模板 随回合角色
core/personality/engine.py  # 删 attach_session/sync_reflector_to_state；_state 绑定 runtime_state
frontend_contract/ws_message.py  # 三个 generation 消息加 optional 审计字段
```

### 3.2 职责边界（上层依赖下层，单向）

```
[frontend_contract DTO/generation 审计字段]  ← wire 形状(只读)
        ↓
[_prepare_turn 回合解析]  ← 从 session.character_binding 解析 instanceId
        ↓
[CharacterRuntimeRegistry.get(instanceId)]  ← 解析/懒建 per-instance runtime+engine
        ↓
[CharacterRuntime]  ← 持 instance/defn/revision/user_identity/state(CharacterRuntimeState)+engine
        ↓
[CharacterRuntimeState]  ← instance-scoped 状态权威(扁平,与 CharacterState 同构; instance_id 权威 + session_id 保留为版本键)
```

### 3.3 各组件职责

| 组件 | 职责 | 关键约束 |
|------|------|---------|
| `CharacterRuntimeState` | instance-scoped 扁平状态模型（与 CharacterState 同构） | `instance_id` 权威 + `session_id` 保留（版本历史键）；子组件扁平字段读取 0 改动 |
| `CharacterInstance.state` | RuntimeState 持久化落点 | `state: CharacterRuntimeState \| None`；经 `CharacterInstanceRepository`（非 CharacterRepository——后者是 characters 集合 + CharacterDefinition CRUD） |
| `CharacterRuntime` | per-instance 运行时容器 | engine 懒建真绑定；`snapshot_state()`/`flush()` 真实化 |
| `CharacterRuntimeRegistry` | instance 运行时解析/缓存 | `get(instanceId)` 权威；`_busy`/`_session_binding_index` 激活 |
| `_prepare_turn` | 回合解析 | 从 binding 解析 instanceId；不再 attach_session；统一 `self._engine`；冻结 TurnCharacterContext |
| `TurnCharacterContext` | 不可变回合上下文 | 加 memoryScope/voice/displayName/stateSnapshot |
| `GenerationContext` | generation 生命周期 | 加 characterId/instanceId/revision 审计 |
| `SessionMemory.character_binding` | 会话→角色绑定 | 会话创建时持久化；激活/重连解析 |

### 3.4 关键设计点

- **registry 权威**：`_prepare_turn` 不再有全局 `_character_engine` 内省；从 `session.character_binding.instanceId` 经 `registry.get(instanceId)` 取 runtime，`runtime.engine`（懒建 per-instance）。
- **状态权威**：`CharacterRuntimeState(instance-scoped，扁平)` 由 runtime 持有，持久化到 `CharacterInstance.state`；reflector buffer 在回合边界由 runtime 统一持久化，不再借 `session.save()` 的 `_pre_save_hook`。
- **去 attach_session**：删除 `attach_session`/`_pre_save_hook`/`sync_reflector_to_state`/`bind_character_engine`；`session.save()` 不再注册 hook；engine 子组件（compression/emotion/awareness/reflector）的 `_state` 在构造时绑定 runtime.state，永不换绑。
- **TurnCharacterContext 扩展**：从 runtime 冻结 `stateSnapshot`（frozen CharacterRuntimeState）+ `memoryScope`（= instanceId）+ 角色 voice/displayName（供 TTS/mood/背景消费）。
- **generation 审计**：GenerationContext + wire 三个 generation 消息加 `characterId/instanceId/revision`（optional，flag 仍 false）。
- **`self._engine` 统一**：`loop`/`streaming`/`finalize`/`debug` 的 `_character_engine` 全部改 `self._engine`（回合引擎），mood 读写均经实例权威。

## 4. 领域模型

### 4.1 CharacterRuntimeState（扁平迁移，`core/schema/character.py` 扩展）

**关键架构决策（修正 spec review blocking #1）：** 架构稿 §5.8 的「嵌套结构」（`mood {label,intensity}`、`awareness {...}`、`compression {...}`、`reflector {...}`）是**目标示意伪码**，但真实迁移若照搬成嵌套，将牵动 `CharacterEngine` 四个子组件（CompressionManager / EmotionDetector / AwarenessJudge / CharacterReflector）**~60+ 处扁平字段读取**（emotion.py:92-111 `.mood`/`.mood_intensity`；compression.py:102-240 `.state_history`/`.compression_last_*`/`.self_l2`；awareness.py:52-151 `.last_awareness_time`/`.approved_awareness_text`/`.awareness_show_count`；reflector.py:89-257 `.reflector_buffer`/`.reflector_context`/`.self_perception_history`），全部 AttributeError。

**本批采用扁平迁移**：`CharacterRuntimeState` 与现有 `CharacterState` **字段同构（均为扁平）**，仅做两处身份/语义调整——**新增 `instance_id`**（instance-scoped 权威身份，删 `session_id` 作为**唯一**身份），但**保留 `session_id` 字段**（供 `CompressionManager` 版本历史 `append_version` 用：compression.py:248/332 读 `state.session_id`——保留使子组件 0 改动，避免 AttributeError——修正第 2 轮 review 新问题 #1）。新增 `source_revision`（记录状态基于哪版 L1 演化）。**子组件 0 改动**，`CharacterEngine` 及其 4 子组件的扁平字段读取（含 compression 的 `.session_id`）继续工作。

```python
class CharacterRuntimeState(BaseModel):
    instance_id: str                          # 权威身份（instance-scoped）
    session_id: SessionId                     # 保留：版本历史 append_version 键（compression.py:248/332 读它）
    source_revision: int = 1                  # 状态基于哪版 L1 演化（§5.8）
    self_l2: str = ""
    state_l3: str = ""
    state_history: list[StateHistoryEntry] = Field(default_factory=list)
    last_awareness_time: float | None = None   # 展示/冷却时钟（C9 冷却门控用，保留——修正 correctness #6）
    approved_awareness_text: str = ""
    approved_awareness_at: float | None = None
    awareness_show_count: int = 0
    compression_last_at: float | None = None
    compression_last_count: int = 0
    mood: str = "平静"                        # 扁平（子组件 emotion.py 0 改动）
    mood_intensity: float = 0.5
    speaking_style_c6: str = ""               # 空=用 L1 默认模板
    reflector_buffer: list[tuple[str, str]] = Field(default_factory=list)
    reflector_context: list[dict] = Field(default_factory=list)
    self_perception_history: list[dict] = Field(default_factory=list)  # 有真实写入点（reflector.py:200/257, MAX_HISTORY=50），保留
    updated_at: IsoTimestamp = Field(default_factory=now_iso)
```

### 4.2 CharacterState 退役与字段映射（扁平→扁平，仅改身份）

当前 `CharacterState`（schema/character.py，`session_id` 身份）扁平字段 → 新 `CharacterRuntimeState`（`instance_id` 身份）扁平映射，**字段名/类型 1:1 不变**（子组件 0 改动）：

| CharacterState（扁平，旧） | CharacterRuntimeState（扁平，新） |
|---------------------------|----------------------------------|
| `session_id` | **`session_id`**（保留，版本历史 append_version 键——compression.py:248/332 读它，修正新问题 #1） |
| —（新增） | **`instance_id`**（instance-scoped 权威身份） |
| —（新增） | `source_revision: int = 1` |
| `self_l2` | `self_l2` |
| `state_l3` | `state_l3` |
| `state_history` | `state_history` |
| `mood` / `mood_intensity` | `mood` / `mood_intensity` |
| `speaking_style_c6` | `speaking_style_c6` |
| `last_awareness_time` | `last_awareness_time`（**保留**，非 `last_judged_at`——展示/冷却时钟，correctness #6） |
| `approved_awareness_text` | `approved_awareness_text` |
| `approved_awareness_at` | `approved_awareness_at` |
| `awareness_show_count` | `awareness_show_count` |
| `compression_last_at` | `compression_last_at` |
| `compression_last_count` | `compression_last_count` |
| `reflector_buffer` | `reflector_buffer` |
| `reflector_context` | `reflector_context` |
| `self_perception_history` | `self_perception_history`（保留，有真实写入点） |
| `updated_at` | `updated_at` |

**注意**：本批不把 `self_perception_history` 移到 `reflector.history`（§5.8 的嵌套示意）——它已有真实写入点（reflector.py），保留为顶层字段最小化涟漪。`CharacterState` 标记 deprecated（保留兼容导入）；`session.character_state` 不再权威。**`CharacterRuntimeState` 同时保留 `session_id`（版本历史键）与 `instance_id`（权威身份）策略已定（修正新问题 #1）**。

## 5. Runtime 与 Registry 激活

### 5.1 CharacterRuntimeState 存储落点（修正 blocking #3 + 新问题 #2）

**持久化落点**：`CharacterRuntimeState` 作为 **`CharacterInstance.state` 字段**存储（`instance.py` 加 `state: CharacterRuntimeState | None = None`）。

**⚠️ 修正新问题 #2（架构缺口）：** 阶段 D 交付的 `CharacterRepository` 是 `characters` 集合 + **`CharacterDefinition`** CRUD（`insert_definition`/`get_definition`/`list_definitions`/`cas_revision_pointer`/`archive`——character_repo.py:96-170），**无 `CharacterInstance` CRUD、无 `state` 字段读写、无 `state_version` CAS**；`BuiltinCharacterSource.load` 构造的 instance 是纯内存对象（builtin_source.py:64 从不落库）。因此 spec 原「复用阶段 D 的 CharacterRepository（character_instances 集合）」**事实不成立**。本批需**扩展 instance 持久化**——

- **方案**：新增 `CharacterInstanceRepository`（`character_instances` 集合）——`get(instance_id)` / `save(instance)`（含 `state` 字段写回）/ CAS state_version。或扩展 `CharacterService` 提供 instance CRUD + instance 版 CAS。
- **装载**：`registry.get(instanceId, factory)` 的 factory 经 `CharacterInstanceRepository.get(instanceId)` 读 `CharacterInstance`（含 `state`）；`state is None` 时初始化空 `CharacterRuntimeState(instance_id=instanceId, session_id=session_id, source_revision=revision)`。
- **默认 elysia instance 开机装载**：`_start_persistence` 为默认会话绑定的 `chi_builtin_elysia` 经 `CharacterInstanceRepository.get` 读 `CharacterInstance.state`（若为空则空态起步——pre-production 无历史状态）。
- **flush**：回合结束时 `runtime.flush()` 把内存 `CharacterRuntimeState` 经 `CharacterInstanceRepository` 写回 `CharacterInstance.state`（CAS `state_version`）。

> 注：`session_id` 在 factory 初始化时由**触发本 runtime 创建的那个会话**传入（`_prepare_turn` 的 target）。因 registry 是 per-instance 缓存，同一 instance 后续会话复用时 `state.session_id` 冻结为首次创建的会话——版本历史键偶发碎片化。本批默认单会话（default→elysia）场景可接受；多会话共享同一 instance 时，版本历史键的分片问题在 plan 需明确处理（见 §5.4 注）。

### 5.2 CharacterRuntime（runtime.py 激活）

- `engine` 懒建为 per-instance：`bind_engine` 真绑定（M1/M2 占位改为真实）。提供 `ensure_engine(factory)` 懒建——factory 为用户提供的 engine 构造闭包（注入 llm/character_config/output/version_repo 等依赖）。
- `snapshot_state()` 返回真实 `CharacterRuntimeState`（deep copy）。
- `flush()` 真正持久化 `CharacterRuntimeState` 到 `CharacterInstance.state`（经 `CharacterInstanceRepository.save(instance)`，含 `state` 字段写回 + CAS `state_version`）。
- `stateVersion` 随每次写 CAS。

### 5.3 CharacterRuntimeRegistry（registry.py 激活）

- `get(instance_id, factory)` 解析/懒建 per-instance runtime（factory 从 agent 注入，构造 instance/defn/revision/user_identity + 经 `CharacterInstanceRepository.get(instance_id)` 加载 `CharacterInstance.state`；为 None 时空态起步）。
- `_busy` 激活：runtime 正在生成/反思/压缩时置位，驱逐跳过。
- `_session_binding_index` 激活：SessionManager 注入 binding 索引，`_is_bound_by_live_session` 判断。
- `invalidate`/`release`/`flush_all`/`close_all` 全链路——`close_all` 在 shutdown 时 `flush` 全部 runtime 的 state 到 `CharacterInstance.state`。
- **registry 注入 loop**：`CognitiveLoop.__init__`/`configure` 增加 `runtime_registry` 参数（agent 构造时注入 `CharacterRuntimeRegistry`），使 `_prepare_turn` 能 `self._registry.get(...)`（修正 architecture #7）。

### 5.4 CharacterInstanceRepository（新增，修正新问题 #2）

`character_instances` 集合的 instance 持久化 repo：
- `get(instance_id)` → `CharacterInstance | None`（含 `state: CharacterRuntimeState`）。
- `save(instance)` → 写 `CharacterInstance`（含 `state`）+ CAS `state_version`；冲突抛 `character instance CAS` 异常。
- `connect()`/`health_check()`/`close()`——对齐 `core/storage/base.py` 的 `Repository`/`StorageUnavailableError`（fail-closed → 503）。

## 6. 回合解析与上下文

### 6.1 _prepare_turn 重构（loop.py）

**核心改动**：`_prepare_turn` 不再 `attach_session(target)`（修正 correctness #5——统一 `self._engine`，涵盖回合主体 streaming/finalize/debug 的消费），改从 `target.character_binding` 解析 instanceId → `registry.get(instanceId)` → `runtime.engine` → 冻结 TurnCharacterContext。`DEFAULT_INSTANCE_ID` / `DEFAULT_CHARACTER_ID` 常量在 loop 或 agent 定义（修正 architecture #8）。

```python
# 常量（loop.py 或 agent.py）
DEFAULT_INSTANCE_ID = "chi_builtin_elysia"   # 默认会话绑定（架构稿 §7.6 bootstrap 内置回退）
DEFAULT_CHARACTER_ID = "chr_builtin_elysia"

async def _prepare_turn(...):
    target = await self._session_resolver(conversation_id, protocol_version)
    gen_conversation_id = conversation_id or target.session_id
    # 解析 instanceId（替代 attach_session）
    binding = target.character_binding or {}
    instance_id = binding.get("instance_id") or DEFAULT_INSTANCE_ID   # 默认会话回退 elysia
    runtime = await self._registry.get(instance_id, self._runtime_factory)   # 经 agent 注入 factory
    engine = runtime.engine_or_lazy()   # per-instance 懒建
    self._session = target
    self._prompt.set_session(target)
    self._engine = engine   # 本回合引擎(统一替代 self._character_engine——最终化/流式/调试全用此)
    self._turn_context = TurnCharacterContext(
        conversationId=gen_conversation_id,
        characterInstanceId=instance_id,
        characterId=runtime.definition.characterId,
        characterRevision=runtime.revision.revision,
        userPronoun=self._resolve_pronouns(),
        memoryScope=instance_id,              # 预埋 F
        voice=runtime.revision.runtimeDefaults.get("voice", {}),
        displayName=runtime.revision.displayName,
        stateSnapshot=frozen(runtime.snapshot_state()),  # frozen CharacterRuntimeState
    )
    self._prompt._turn_context = self._turn_context
    self._set_turn_engine(engine)   # 同步 streaming/finalize/debug 的 _character_engine 引用
    self._prompt.set_personality_engine(engine)  # 本回合 engine → prompt 读 mood/C1/C6/C9(correctness #5 补)
    self._prompt.set_character(runtime.definition.characterId, runtime.revision.displayName)
    # 第 4 轮 review 补充：C1 `name`(你是 {{ name }})与 `_resolve_template` 模板目录(character.name.lower())
    # 也须随回合角色路由——set_personality_engine 只路由 engine 态数据(render_c1/c6/c9 return 不含 name)；
    # set_character(characterId, displayName) 更新 _personality_section._name + 模板选择(经 _resolve_template)。
```

**`self._engine` 统一（correctness #5）**：把 `loop.py`/`streaming.py`/`finalize.py`/`debug/info.py` 的 `self._character_engine` 全部改为 `self._engine`（或经 `_set_turn_engine` 同步）。回合主体（`_apply_mood`、`analyze_user/assistant`、`analyze_user_stt`、engine 传给 after_response、prompt `_personality_engine`）全部用本回合 `self._engine`——不再有全局 `_character_engine`。**prompt `_personality_engine`（prompt_assembler.py:149 read mood/C1/C6/C9）经 `set_personality_engine(self._engine)` 指向本回合 engine**（不再构造时固定单例）。**注意：`render_c1` 只 return `core_personality/self_personality`（engine.py:165-175），不含 name；C1 的 `你是 {{ name }}` 与 C6 静态 `speaking_style` fallback 来自 `PromptAssembler` 构造时 `self._character`（agent 单例）——所以需 `set_character(characterId, displayName)` 一并路由（修正第 4 轮 review，否则 #9 双角色 C1 渲染错 name）。

### 6.2 TurnCharacterContext 扩展（turn_context.py）

```python
@dataclass(frozen=True)
class TurnCharacterContext:
    conversationId: str
    characterInstanceId: str
    characterId: str
    characterRevision: int
    userPronoun: str
    memoryScope: str              # = instanceId（预埋 F）
    voice: dict                    # 角色声音（供 TTS）
    displayName: str               # 角色展示名
    stateSnapshot: CharacterRuntimeState  # frozen（回合开始时快照）
```

**注意**：`stateSnapshot` 是 `CharacterRuntimeState` 的 **frozen/deep copy**——回合开始时快照，回合中 mood 写 `engine.state`（实例权威），不写 `stateSnapshot`。finalize 的 mood 读 `self._engine.state`（实例权威），非 `stateSnapshot.mood`（correctness #6 的 last_awareness_time 保留扁平语义，见 §4.1）。

### 6.3 mood 写读链重定向（correctness #4）

- `streaming._apply_mood`（streaming.py:60-63）当前写 `target_session.character_state.mood/mood_intensity` → 改为写 `self._engine.state.mood/mood_intensity`（runtime 实例权威）。
- `finalize._apply_final_mood_fallback`（finalize.py:63-64）+ `turn_completed` 的 mood（:192-193）→ 改读 `self._engine.state.mood`（实例权威）。
- 这些引擎子组件(mood 等)经 `_engine`(实例)读写，不再碰 `target.character_state`。

**⚠️ F1-b 语义澄清（plan review 发现）**：streaming.py:60 的 `state = target_session.character_state if target_session is not None else engine.state` 是**批次 3 的 F1-b 修复**——防「全局单例 engine 经 attach_session 换绑到会话 B 时，in-flight 生成仍写 engine.state → 污染 B」。M3 下 `engine` 是 **per-instance 永不换绑**（§7.2 删 attach_session），该跨会话污染**风险源已消除**——`self._engine` 就是本回合 instance 的 engine，`_apply_mood` 写 `engine.state`(实例权威) 正确且安全。

**⚠️ 需翻转现有测试**：`core/cognitive/tests/test_mood_engine.py::TestApplyMood::test_apply_mood_writes_target_session_state`（:87-115，F1-b）断言 `_apply_mood` 写 `target.character_state`（`assert target.character_state.mood == "开心"` + `assert engine.state.mood == "平静"`）。M3 改写 `engine.state` 后该测试断言**必然翻转**（engine.state.mood 变"开心"、target.character_state.mood 不变）。Task 8 必须**显式更新/翻转此测试**为「写 engine.state(实例权威)，target.character_state 不再权威」的断言——不能只加新测试而留旧测试失败（否则 Task 8 Step 4 跑 `core/cognitive/tests/ -q` 挂）。

### 6.4 GenerationContext + wire 审计字段（generation.py + frontend_contract）

- `GenerationContext` 加 `characterId: str` / `characterInstanceId: str` / `characterRevision: int`。
- `frontend_contract` 三个 generation 消息（`GenerationStartedMessage`/`GenerationCompletedMessage`/`GenerationFailedMessage`）加 optional `characterId`/`characterInstanceId`/`characterRevision`（§12.3，flag 仍 false 时可选）。

### 6.5 读取端迁移（修正第 2 轮 review 新问题 #3——读端 stale）

`_apply_mood`（§6.3）改为写 `engine.state`（实例权威）后，**所有读 `session.character_state.mood` / `agent.character_engine` 的点必须同步改读 instance engine.state**，否则读到 stale/default：

- `core/input/ws_handler.py:235-245`（v1 路径 `default_session.character_state.mood` + **v2 路径 `agent.character_engine.state`（:236）**）→ 都改读当前 instance 的 `engine.state.mood`；v2 的 `agent.character_engine` 在 M3 移除全局 engine 后改经 `CharacterRuntimeRegistry.get(当前会话绑定的 instanceId)` 解析。
- `core/input/debug_routes/archive.py:244`（`agent.session.character_state.model_dump()` 作 params）→ 改读 `engine.state`（CharacterRuntimeState）。
- `core/debug/info.py:302`（`info.mood = agent.session.character_state.mood`）+ `:175`（`character_state_raw`）→ 改读 `engine.state`。
- `core/input/debug_routes/character.py:45-98`（`agent.character_engine._version_repo.list_versions(sid=...)` + `engine.state.self_l2/speaking_style_c6`）→ M3 下 `agent.character_engine` 不再单例；改经 `CharacterRuntimeRegistry.get(instance_id)` 解析该会话绑定的 instance engine 读版本/回滚（`sid` 版本键随 session_id 或 instance_id，见 §5.1 注）。
- `core/prompt/prompt_assembler.py:149`（`self._personality_engine.state.mood` + render_c6）→ 经 `set_personality_engine(self._engine)` 指向本回合 engine（§6.1 已补）。

以上读取端迁移到 instance `engine.state`，保证 mood/状态读端与写端一致（实例权威）。

## 7. 会话绑定

### 7.1 SessionMemory.character_binding 接线

- `POST /api/sessions`（或服务层）创建会话时：`(userId, characterId)` → 取/建 canonical instance → 持久化 `character_binding = {"character_id": ..., "instance_id": ...}`。
- `SessionMemory.character_binding` 字段已存在（阶段 D）；本批真正填充（M1/M2 的 `migration.py` 只建 id 未接运行时）。
- `_prepare_turn`/激活/重连从 binding 解析 instanceId。

### 7.2 会话创建时解析

默认会话（default session）→ elysia instance（`chi_builtin_elysia`）；其他会话按 `characterId` 解析。未传 `characterId` 时回退 `UserIdentity.defaultCharacterId`，再回退 bootstrap 内置。

### 7.3 默认会话 binding 初始化（修正 architecture #8）

`agent.__init__`（agent.py:203-221）当前 eager-load 默认会话 + `bind_character_engine(engine)`。M3 改为：

- **不再 eager 构造全局 engine**；改为初始化默认会话的 `character_binding = {"character_id": "chr_builtin_elysia", "instance_id": "chi_builtin_elysia"}`（bootstrapped 默认会话绑 default character）。
- 启动时经 `registry.get(DEFAULT_INSTANCE_ID)` 解析/懒建 elysia runtime（读取 `CharacterInstance.state` 或空态起步），供默认会话首轮 `_prepare_turn` 用。
- **`session_manager` 接线（correctness #2）**：`session_manager.py` 的 `bind_character_engine`（:365-368）/`switch_active` 里的 `attach_session`（:361-362）/`_pre_save_hook`（:358-359, :581）全部移除。会话激活从 binding 解析 instance（不再 attach_session/换绑 engine state）。

## 8. 状态落盘与 Reflector

### 8.1 Reflector buffer 落盘（去 attach_session 后，修正 blocking #2）

**落盘链改造**（`attach_session`/`_pre_save_hook`/`sync_reflector_to_state` 删除后）：

- `session.save()`（session.py:300-301）不再调用 `_pre_save_hook`（移除 hook 注册）；reflector buffer 不再经 session.save 落盘。
- `session_manager.switch_active`（:343-359）不再调用 `outgoing._pre_save_hook()` / `attach_session(target)` /*`bind_character_engine` 换绑*。
- reflector buffer/context 改由 **runtime 在回合边界统一持久化**：回合结束时(或 runtime flush)把 `engine._reflector._buffer/_context` 同步到 `CharacterRuntimeState.reflector_buffer/reflector_context` → `runtime.flush()` 写 `CharacterInstance.state`。提供 `engine.sync_reflector_to_runtime_state()`（**持久化 buffer 到 RuntimeState**，纠正 improvement #10——`flush_reflector()` 是触发 LLM 反思的 API，非序列化 buffer）替代旧的 `sync_reflector_to_state`。
- **`_lifecycle_persistence` 启动恢复替代（correctness #2）**：`:124-126` 的 `engine.state.last_awareness_time` + `engine._reflector._restore_from_state()` 改为——从默认会话绑定的 `CharacterInstance.state` 读 `CharacterRuntimeState` → `engine.state` 恢复（`engine._restore_from_state()` 读 runtime.state 而非 session.character_state）。

### 8.2 EmotionDetector idle_expressions 刷新（§7.2）

- Revision 变更时，`EmotionDetector.idle_expressions` 在回合边界刷新——从 runtime.revision.runtimeDefaults.emotion.idle_expressions 读取（而非构造时固定）。

## 9. 错误处理与离线

- registry.get(instanceId) 找不到 → 稳定错误（页面拒绝，绝不用 Elysia 顶替——§7.6 "绝不能悄悄使用 Elysia"）。
- runtime engine 懒建失败 → 存储/加载错误 fail-closed。
- `CharacterRuntimeState` 持久化失败 → StorageUnavailableError → 503（对齐阶段 D fail-closed）。

## 10. Elysia 现网行为不变

- 默认会话（default session → elysia instance）的 `_prepare_turn` 解析到 `chi_builtin_elysia` runtime，engine 构造与现网一致（L1 core/L2 self/L3 state/L4 mood + speaker style C6 + C9 awareness）。
- **builtin parity（architecture #9）**：registry 的 factory 构造 elysia runtime 时，用 `BuiltinCharacterSource.load("elysia")` 的 `CharacterRevision`（`corePersonality`=personality.md 全文、`speakingStyle`=config speaking_style、`runtimeDefaults.emotion.idle_expressions`）→ `CharacterEngine` 构造参数（`base_personality`/`character_config`/`character_name`）。**必须与现网 `agent.__init__`（agent.py:206-217）的 elysia 构造参数逐值一致**（`character.name`/`base_personality` 文本/`character_config.emotion.idle_expressions`），否则渲染漂移。
- `test_elysia_snapshot_unchanged`（render == `system_prompt_snapshot_elysia_cf4bc008.txt`，18127B）必须保持 green——它是 parity 的兜底门禁。
- 单角色路径：default session 恒解析到 elysia instance，engine 行为与现网 byte 一致。

## 11. 关键不变式

| # | 不变式 | 验证方式 |
|---|--------|---------|
| 1 | Elysia byte-for-byte 不变 | `test_elysia_snapshot_unchanged`（18127B）green |
| 2 | attach_session 彻底移除（含 session_manager 栖身点） | grep 无 `attach_session`/`_pre_save_hook`/`sync_reflector_to_state`/`bind_character_engine` 残留（含 session_manager.py） |
| 3 | 状态权威 instance-scoped | `CharacterRuntimeState.instance_id` 为权威身份（`session_id` 保留供版本历史 append_version，compression.py:248/332 仍读它）;`session.character_state` 不再权威，新会话不用；`CharacterInstance.state` 为持久化落点；`CharacterInstanceRepository` 提供 instance CRUD + state_version CAS |
| 4 | engine per-instance | `registry.get(instanceId)` 返回独立 runtime/engine；不同 instance 的 engine 不共享 state |
| 5 | TurnCharacterContext 贯穿 | 单个回合 Prompt/TTS/mood/generation 全用同一 context（stateSnapshot + memoryScope） |
| 6 | generation/message 角色审计 | GenerationContext + wire 三个 generation 消息带 characterId/instanceId/revision |
| 7 | memory scope 预埋 | TurnCharacterContext.memoryScope = instanceId；C4 本批仍全局读（F 才隔离） |
| 8 | custom 不参与聊天 | `character_profiles=false` 恒抑制 |
| 9 | 双角色不串 | 两个 role 在不同 session 生成，Prompt/状态/声音不串（退出条件） |

## 12. 测试策略

### 12.1 单元测试
- `CharacterRuntimeState` 模型（扁平结构、`instance_id` + `session_id` 并存、与 CharacterState 字段同构、source_revision 默认）。
- `CharacterInstance.state`（CharacterRuntimeState 持久化落点）序列化/反序列化。
- `CharacterInstanceRepository`（新增）：instance CRUD + `state_version` CAS（hit/miss/offline fail-closed）。
- `CharacterRuntime.snapshot_state()/flush()`（真实态；flush 写 `CharacterInstance.state`）。
- `registry.get` per-instance 解析 + `_busy`/`_session_binding_index` 驱逐保护。
- `TurnCharacterContext` 扩展字段（memoryScope/voice/displayName/stateSnapshot frozen）。
- 去 attach_session 后 engine 构造不变（子组件扁平字段读取含 compression `.session_id` 0 改动）。

### 12.2 回合测试
- `_prepare_turn` 从 binding 解析 instanceId（不再 attach_session）。
- 不同 instance 的 TurnCharacterContext 字段正确（role 不串）。
- `self._engine` 统一：streaming/finalize/debug 用本回合 engine（非全局）。
- mood 写 `_engine.state`（实例权威），非 `target.character_state`；**读取端**（ws_handler v1 connect / archive.py / info.py）读 `engine.state` 非 stale。
- generation 审计字段在 wire 模型可选。

### 12.3 集成/回归测试
- `test_elysia_snapshot_unchanged`（18127B）green——单角色路径 byte 不变。
- 双角色：两 session 绑不同 instance → 分别 `_prepare_turn` → Prompt/状态/声音不窜（退出条件；C1 name/模板随 `set_character` 路由，非 "你是 Elysia" 混入 cyrene）。
- registry 驱逐、并发保护。
- 全量 unit+service 回归无新增失败。

## 13. 文档

- `core/character/README.md`：补 RuntimeState/registry 激活 + 去 attach_session。
- `core/cognitive/README.md`：补 _prepare_turn 回合解析 + TurnCharacterContext 扩展。
- 根 `README.md`：Core 层导航补阶段 E。
