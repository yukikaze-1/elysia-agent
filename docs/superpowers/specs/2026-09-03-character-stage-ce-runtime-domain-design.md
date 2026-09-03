# 阶段 C+E：角色域 Runtime（CharacterInstance + UserIdentity + user_pronoun 正位） Spec

> **文档导航：** [项目文档](../../README.md)
> **Spec 状态：** 待 review
> **日期：** 2026-09-03
> **架构稿：** [`docs/2026-08-30-character-prompt-customization-backend-architecture-design.md`](../../2026-08-30-character-prompt-customization-backend-architecture-design.md)（§17 阶段 C/E + §7 runtime + §5 领域模型）
> **事实基线：** `main@4c73c2e3`（B-2 完成后 HEAD，含 B 收尾决策）
> **性质：** 生产代码 + 测试批次——把「多角色」从文件层面提升到领域模型 + 运行时层面，并把错放在角色配置里的 `user_pronoun` 正位到 `UserIdentity`
> **权威范围：** `CharacterDefinition`/`CharacterRevision`/`CharacterInstance` 模型、`CharacterRuntimeRegistry`、`UserIdentity`、`user_pronoun` 数据源迁移、CharacterEngine instance-scoped（去全局单例换绑）、TurnCharacterContext、feature flag 仍 false
> **明确排除：** 记忆 v2 隔离（阶段 F）；Custom Character 持久化/CRUD/版本（阶段 D）；Neo4j 关系图谱；前端 UI 视觉稿；任务中心；`CharacterConfig.user_pronoun` 的运行时消费点之外的清理

---

## 0. 一句话目标

把角色域从「进程启动加载唯一角色、所有会话共享这一份角色定义」提升到「**每个会话绑定一个专属 CharacterInstance（某角色的一个演化体），多个角色可并存、各有独立状态**」；同时把错放在角色配置里的 `user_pronoun`（用户代词）正位到 `UserIdentity`。**Elysia 现网行为 byte-for-byte 不变；Cyrene 仅 `user_pronoun` 由「你」→「她」（意图修复，见 B1 决断），其余渲染不变；`character_profiles` feature flag 保持 `false`（决策 #12 纪律）。**

---

## 1. 背景与问题

### 1.1 当前架构的真实边界（已核实）

`CharacterEngine`（[core/personality/engine.py:64](core/personality/engine.py#L64)）目前：
- 由 `agent.py:200` 构造 **1 个全局实例**，`bind_character_engine` 绑到 `SessionManager`；
- 经 `attach_session()`（[engine.py:341](core/personality/engine.py#L341)）在会话切换时换绑 `state` 视图——`self.state = session.character_state`，三个子组件（compression/emotion/awareness）一起换绑；
- 因此 **L2/L3/L4/mood/reflector buffer 已经按会话隔离**（每会话一个 `character_state`）。

**关键洞察**：当前架构**已经是「session-scoped」**，不是「全局单例」——但它是「1 engine = 1 角色，多会话共享同一角色定义，换绑的是同一角色的状态」。缺的是：**多角色并存 + 角色↔会话的稳定绑定关系 + 角色身份（ID/版本）**.

### 1.2 三个人格演化状态的归属错误

架构稿决策 #4：「L1 属于角色版本，L2/L3/L4 属于角色实例」。当前：
- `L1 Core` 从文件加载（`base_personality`），绑定角色——✓ 正确；
- `L2/L3/L4` 存在 `session.character_state`，以 **session_id** 为唯一身份——**错误**。架构稿要求以 **character_instance_id** 为身份：同一实例跨会话连续，不同实例隔离，会话只保存「对话 + 角色绑定」。

### 1.3 `user_pronoun` 错放在角色配置里

核实结果：
- `character/elysia/config.yaml:9` → `user_pronoun: 她`
- `character/cyrene/config.yaml:9` → `user_pronoun: 你`
- `core/config/character_config.py:29` 默认 `"她"`，`from_yaml` 从 YAML 读。

架构稿决策 #7 明确：「`user_pronoun` 不再放在角色配置里」。**用户代词是用户身份，不是角色属性**——多个角色会复制同一用户信息，且每个角色都带一份冗余。`UserIdentity.pronouns` 是唯一权威来源。

### 1.4 `user_name` 与 `user_pronoun` 是两条独立流

- `user_name`（用户名）：`agent.py:116` `self._user_name = config.agent.user_name`（`config.agent.user_name`，全局），流入 `build_system_prompt(user_name=...)`；
- `user_pronoun`（用户代词）：`PromptAssembler.build_system_prompt()` 从 `self._character.user_pronoun` 读，经 `set_context(user_pronoun=...)` 注入 section。

两者**不可混为一谈**。迁移只动 `user_pronoun`→`UserIdentity.pronouns`；`user_name` 在本批保留 `config.agent.user_name`（架构稿 §20 说它降级为 UserIdentity bootstrap fallback，但本批只做 bootstrap 创建，不删 `config.agent.user_name` 消费点）。

---

## 2. 分阶段实施计划（本批范围）

### 阶段 C（模型层 + 用户身份）

1. **新建 `core/profile/` 包** —— `UserIdentity` 模型 + bootstrap + CRUD；
2. **新建 `core/character/` 包** —— `CharacterDefinition`/`CharacterRevision`/`CharacterInstance` Pydantic 模型；
3. **`BuiltinCharacterSource`** —— 从现有 `character/{name}/config.yaml` + personality.md 构建只读内置角色（**不重写** Elysia/Cyrene 到 manifest v2，渐进式）；
4. **`CharacterCatalog`** —— 先只读内置角色，无 Mongo 也能列出/校验 Elysia、Cyrene；
5. **`frontend_contract`** 增加 DTO，但 `character_profiles` feature flag **恒 `false`**。

### 阶段 E（运行时 + 会话绑定，精简）

6. **`CharacterRuntimeRegistry`** —— `get(instance_id)/invalidate/flush/close_all`，LRU 缓存，驱逐前 flush dirty，生成中/反思中的 runtime 不被驱逐；
7. **`CharacterRuntime`** —— 装载 Definition/Revision 快照 + UserIdentity 快照 + CharacterRuntimeState + CharacterEngine + CharacterReflector/Compression/Awareness/Emotion + ScopedMemory 占位 + per-instance Lock + dirty/stateVersion；
8. **`CharacterEngine` instance-scoped** —— 构造时绑定 CharacterInstance，**删除/废弃 `attach_session()`**，子组件从同一 runtime 获得 character/user/revision/state；
9. **`TurnCharacterContext`** —— `_prepare_turn()` 一次性冻结的不可变角色/用户/状态/版本快照，后续 Prompt/TTS/mood/记忆/generation 全用同一 context；
10. **Session create + character binding + summary** —— 会话绑定角色实例；旧 session state shadow migration（兼容）；
11. **generation/message 角色审计字段** —— 记录 generation/message 实际使用的角色 revision。

### 本批明确不做（留给后续阶段）

- **记忆 v2 隔离**（阶段 F）：`ScopedLongTermMemory` 占位，但不实现 scope 强制；
- **Custom Character CRUD/持久化/版本/rollback**（阶段 D）：`CharacterCatalog` 只读内置，custom 不可参与聊天；
- **Neo4j 关系图谱**：C2 仍用现有关系基线快照；
- **前端 UI**：只加契约 DTO + feature flag false，不做前端。

---

## 3. 领域模型

### 3.1 `UserIdentity`（`core/profile/`）

架构稿 §5.1，迁移语义对齐（决策选项 2）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `userId` | string | 本地用户稳定 ID，首版确定性 `usr_local_default` |
| `displayName` | string | 角色知道的用户名字（来源 `config.agent.user_name` bootstrap） |
| `preferredAddress` | string? | 希望角色通常如何称呼；空则用 displayName |
| `pronouns` | string? | **用户代词，全局唯一**，必须能填充 `{{ user_pronoun }}`（合法值如 她/他/你）。**不因角色而异**——用户是同一人（妖梦），代词只有「她」，Elysia 与 Cyrene 皆渲染「她」。本批 bootstrap 即定「她」并恒权威（**无 per-role 回退**，见 §5.2 B1 决断）。空时兜底 `"她"` |
| `locale` | string | 默认 `zh-CN` |
| `timezone` | string | IANA 时区，如 `Asia/Shanghai` |
| `initialized` | bool | **仅 UX 标记**「用户是否已确认身份」（false 时前端可触发初始化流程）。**不 gate `pronouns` 生效**——`pronouns` 恒为权威（见 §5.2 B1 决断） |
| `defaultCharacterId` | string? | 新会话默认角色 |
| `revision` | int | 乐观并发版本，从 1 起 |
| `createdAt/updatedAt` | timestamp | 审计 |

**bootstrap 规则**：若不存在 profile，用 `config.agent.user_name` 建 `initialized=false` 的 bootstrap profile；`pronouns` **在 bootstrap 即定为全局唯一用户代词「她」并恒为权威**（非待 `initialized`，非从任意角色取值）。`initialized` 仅标记「用户是否已确认」的 UX 状态，**不影响 pronouns 生效**（见 §5.2 B1 决断）。

> **B1 决断（关键）**：`pronouns` 是用户代词，全局唯一。Elysia 配「她」、Cyrene 配「你」是 **B-2 遗留的错位**（Cyrene 模板句式同 Elysia，本应第三人称「她」却配了「你」，渲染成「你会记住你说过的话」——第二人称错俳）。**正位修复**：`pronouns` 全局唯一「她」，Elysia byte 不变，Cyrene 从「你」修正为「她」（本批意图修复，非回归）。用户是同一人，代词只有一个，让每个角色各自带一份「她/你」才是真正的错放。

### 3.2 `CharacterDefinition`（`core/character/`）

架构稿 §5.2：

| 字段 | 类型 | 说明 |
|------|------|------|
| `characterId` | string | 稳定 ID；内置 `chr_builtin_elysia` / `chr_builtin_cyrene` |
| `slug` | string | 文件/检索友好标识 |
| `source` | `builtin/custom` | 来源 |
| `mutable` | bool | 内置 false |
| `status` | `active/archived` | archived 不可新建会话，既有会话仍可读 |
| `currentRevision` | int | 当前版本号 |
| `ownerUserId` | string? | custom 所有者 |
| `createdAt/updatedAt` | timestamp | 审计 |

Definition 不存角色 Prompt 正文、不存 L2/L3/L4、不存 session。

### 3.3 `CharacterRevision`（`core/character/`）

架构稿 §5.3，精简到当前系统实际需要的字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `characterId` | string | Definition ID |
| `revision` | int | 从 1 单调递增 |
| `schemaVersion` | int | 首版 1 |
| `displayName` | string | 显示名，同时 Prompt 自称 |
| `description` | string | 列表/管理页简介 |
| `aliases` | string[] | 别名/唤醒词候选 |
| `promptProfile` | object | C1/C2/C3/C6/C7 角色专属内容（corePersonality/relationshipStyle/emotionStyle/speakingStyle/conversationInitiative） |
| `runtimeDefaults` | object | 声音/表情/唤醒词/主动触发默认值 |
| `presentation` | object | 头像/Live2D 等引用 |
| `openingMessage` | string? | 新会话可选开场白 |
| `checksum` | string | 规范化内容 SHA-256 |
| `compilerVersion` | int | Prompt Compiler 版本 |
| `createdBy` | string | `system/user/migration` |
| `createdAt` | timestamp | 创建时间 |

**本批实现策略（渐进）**：`CharacterRevision` 从现有 `CharacterConfig`（config.yaml）构建——`corePersonality` 从 `base_personality`、`speakingStyle` 从 `personality.speaking_style`、`runtimeDefaults` 从 `emotion/wake_words/proactive`。

> ⚠️ **C2（promptProfile 无源字段）**：`relationshipStyle`/`emotionStyle`/`conversationInitiative` 在本批**显式留空**——它们当前存在于 `.j2` 模板正文（`templates/{elysia,cyrene}/{c2,c3,c7}_*.j2`），本批**不重写模板**。因此 `promptProfile` 本批只承载 `corePersonality`/`speakingStyle`（有源的字段），**渲染仍走模板**，`promptProfile` 不派生、不喂 render。关系/情绪/主动性的结构化字段留待阶段 D 模板改造时从模板抽离后补源。**不新增 PromptProfile 单独空白领域类**，用 `promptProfile` dict 承载（YAGNI）。

### 3.4 `CharacterInstance`（`core/character/`）

架构稿 §5（会话绑定的演化体）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `instanceId` | string | 稳定 ID |
| `characterId` | string | Definition ID |
| `revision` | int | 当前 Revision（创建时 1） |
| `ownerUserId` | string | 拥有者 |
| `stateVersion` | int | 状态版本，CAS |
| `createdAt/updatedAt` | timestamp | 审计 |

InstanceState 引 `CharacterRuntimeState`（L2/L3/L4/mood/reflector buffer），以 instance_id 为身份，**不再以 session_id 为身份**。

---

## 4. 运行时

### 4.1 `CharacterRuntimeRegistry`（`core/character/`）

```
CharacterRuntimeRegistry
  get(instance_id) -> CharacterRuntime
  invalidate(instance_id, reason)
  flush(instance_id)
  close_all()
```

规则：
- LRU 缓存；驱逐前 **必须 flush dirty**；
- 正在生成/反思/压缩的 runtime **不得被驱逐**；
- **驱除边界（M2）**：被任一仍缓存 session 绑定的 instance 的 runtime **不得被驱逐**——registry 驱逐键必须与 SessionManager 的会话缓存生命周期耦合（session 存活期间其绑定 instance 的 runtime 必须保留；session 被驱逐/删除后可放行）。避免「session 活但 runtime 已被 LRU 驱逐」导致解析悬空。
- 状态写入由 runtime 统一持久化，不由 section/路由直接写 repo；
- `close_all()` 关机时 flush 全部。

### 4.2 `CharacterRuntime`（`core/character/`）

至少拥有：
- 已加载 Definition/Revision 快照；
- UserIdentity 快照或版本引用；
- CharacterRuntimeState；
- CharacterEngine；
- CharacterReflector/CompressionManager/AwarenessJudge/EmotionDetector；
- ScopedMemory（阶段 F 占位）；
- per-instance `asyncio.Lock`；
- dirty/stateVersion 与持久化 journal 状态。

### 4.3 `CharacterEngine` instance-scoped（关键重构）

- 构造时绑定 `CharacterInstance`，生命周期内**不改变角色身份**；
- **删除/废弃 `attach_session()`**——由 session binding → Registry 解析替代；
- 子组件从同一 runtime 获得 character/user/revision/state；
- `render_c1/render_c6/render_c9` 保留，但输入必须是实例状态。

> ⚠️ **A2（状态写入目标，最关键）**：本批采用架构稿 §15.4 **M1/M2 阶段**——`session.character_state` 仍是**权威写入目标**（legacy 路径不变，`session._pre_save_hook = engine.sync_reflector_to_state` 与 `session.save()` 落 `character_state` 保持现状），**保证 Elysia byte-for-byte**。`CharacterRuntimeState`（instance.state）本批只做**幂等 backfill + 读取对比（不启用）**——不切换权威写入。engine 内部从 runtime 取 instance 身份（ID/revision），但 state 读写仍走 session.character_state。**M3（instance 变权威 + dual-write legacy）留待阶段 H。**

**对现有行为的影响**：当前 `agent.py` 构造 1 个 engine + `attach_session` 换绑。本批改为「每个 CharacterInstance 一个 engine」，`agent` 通过 `SessionManager` 的 binding 解析到对应 instance 的 engine。**M1 阶段 state 持久化仍走 legacy `session.character_state`**，引擎重构本身**不改变渲染结果**——除 B1 意图修复的 Cyrene `user_pronoun` 你→她外，Elysia/Cyrene 其余渲染与现状一致（default 会话绑定 default instance → 对应 engine）。

### 4.4 `TurnCharacterContext`（`core/cognitive/`）

`_prepare_turn()` 一次性解析并冻结：
1. resolve conversationId；
2. load Session + character_binding；
3. load UserIdentityProfile snapshot；
4. registry.get(characterInstanceId)；
5. resolve active CharacterRevision；
6. load RelationshipSnapshot；
7. create scoped MemoryView（阶段 F 占位）；
8. snapshot CharacterRuntimeState + stateVersion；
9. build immutable TurnCharacterContext；
10. register GenerationContext with character audit fields。

> ⚠️ **A3（set_session 保留）**：`TurnCharacterContext` 只接管**身份字段**（user_pronoun 等）与角色/实例解析。`PromptAssembler.set_session(target)` **保留**——assembly 的 session-bound 部分（Emotion/OwnLife/Memory 三个 section）不随本批改为 context 注入，仍由 `_prepare_turn` 在回合开始时 `set_session(target)` 绑定（loop.py:342 现状）。两者**不冲突**：context 提供身份，set_session 提供会话数据源。

此后 Prompt/TTS/mood/记忆/generation 全用同一 context，**不能在回合中重新读取「当前活动角色」**。角色编辑/默认角色修改不改变 in-flight context。

---

## 5. `user_pronoun` 迁移（核心）

### 5.1 语义对齐（决策选项 2 + B1 正位）

`UserIdentity.pronouns` 是自由文本（架构稿原样），约定其**必须能填充模板 `{{ user_pronoun }}` 占位符**——即值域为 `她/他/你` 等第三人称/第二人称引用。**本批经 B1 决断统一为全局唯一「她」**（用户是同一人，代词只有一个）。

**保留 `displayName` 独立字段**（用户自称，如「妖梦」），与 `pronouns`（代词「她」）分离——两者是不同维度。

### 5.2 数据源切换

`PromptAssembler.build_system_prompt()` 当前 `user_pronoun=self._character.user_pronoun`（[prompt_assembler.py:152](core/prompt/prompt_assembler.py#L152)）——改为从 `TurnCharacterContext`（含 UserIdentity snapshot）读 `pronouns`。

**回退链（B1 决断，正位修复）**：`TurnCharacterContext.user_pronoun` =
1. `UserIdentity.pronouns` —— **全局唯一用户代词，恒为权威「她」**（bootstrap 即定，非待 `initialized`）；
2. `"她"`（兜底）。

> ⚠️ **B1 决断（用户已确认「正位修复」）**：`pronouns` 是**用户代词，全局唯一**。Elysia 配「她」、Cyrene 配「你」是 B-2 遗留错位——用户是同一人（妖梦），代词只有一个「她」，Cyrene 模板句式与 Elysia 相同（本应第三人称），却配「你」导致渲染「你会记住你说过的话」这类第二人称错俳。**本批正位**：`UserIdentity.pronouns` 初始化即「她」并恒为权威（**不再保留 per-role 回退**），Elysia byte 不变（她→她），Cyrene 从「你」修正为「她」（**意图修复，非回归**，符合 §7.5 修订）。`initialized` 只影响「用户是否已确认」的 UX 标记，**不 gate pronouns 生效**。

> ⚠️ **B1 补充（若本批需保 Cyrene 现网「你」）**：唯一会让 Cyrene 保持「你」的路径是保留 per-role 回退——但那会**违背正位修复**（用户已拍板将其视为意图修复）。故本批**不保留** per-role 回退，直接全局「她」。Cyrene 渲染从「你」变「她」是明确的**批次语义修复**，测试断言同步更新。（`CharacterConfig.user_pronoun` 字段保留但**不再读作权威**，见 §5.3。）

### 5.3 `CharacterConfig.user_pronoun` 的处置

架构稿 §20 说「删除」。B-2 的 per-role 模板（`templates/elysia/c2_relationship.j2` 等）依赖 `{{ user_pronoun }}` 占位符被填充——本批该占位符改由 `TurnCharacterContext`（来自 `UserIdentity.pronouns`）提供，**不再经 `CharacterConfig.user_pronoun`**。

**精确处置（B1 正位）**：`CharacterConfig.user_pronoun` **标记为 deprecated，本批起不再是权威数据源**（`prompt_assembler.py:152` 的运行时消费点改读 TurnCharacterContext）。字段本身**保留不物理删除**（避免破坏 B-2 模板占位符、测试夹具与 cold/elysia_config 的 `user_pronoun` 构造），但**不再进入运行时回退链**（§5.2 回退链已不含 CharacterConfig）。`initialized` 阶段 D 再彻底移除。

---

## 6. 契约层

### 6.1 `frontend_contract` DTO

新增（架构稿 §11）：
- `CharacterSummary`/`CharacterInstanceSummary`（角色列表/状态展示用）；
- `UserIdentityProfile`（`GET /api/me` / `PUT /api/me`）；
- `CharacterPublicState`（角色产品态）；但 `character_profiles` feature flag **恒 `false`**。

### 6.2 feature flag 纪律（决策 #12）

`server_info.features.character_profiles` 本批**保持 `false`**。因为：
- 记忆 v2 隔离（阶段 F）未做 → `character_private` scope 未实现；
- Custom CRUD（阶段 D）未做；
- 前端未联调。

本批**不开启** flag，只搭领域模型 + 运行时 + 契约 DTO + 迁移基建。**flag 开启是 H 阶段的事**。

---

## 7. 验收标准

### 7.1 领域模型
- [ ] `UserIdentity` 模型可校验，bootstrap 用 `config.agent.user_name` 建 `initialized=false`；
- [ ] `CharacterDefinition`/`CharacterRevision`/`CharacterInstance` 模型可校验；
- [ ] 无 Mongo 时 `CharacterCatalog` 可列出并校验 Elysia/Cyrene 内置角色（从 config.yaml 构建）；
- [ ] `BuiltinCharacterSource` 从现有 `character/{name}/` 加载，不重写 manifest。

### 7.2 运行时
- [ ] `CharacterRuntimeRegistry.get(instance_id)` 返回 **该 instance 的 runtime 容器**（骨架；engine 本批 M1/M2 仍全局单例，runtime 缓存就绪）——「多 instance engine」为 M3 语义；
- [ ] `CharacterEngine` **本批（M1/M2）构造时携带 instance 身份**（`instance_id`/`character_revision`），生命期内角色身份不变；**「不再用 attach_session 换绑、每 instance 一个 engine」是 M3 阶段语义，本批 M1/M2 不要求**——M1/M2 阶段 `attach_session` 保留为 **state 视图换绑 shim**（batch-3 多会话隔离依赖它；state 权威仍在 `session.character_state`，见 §4.3 A2），但**不再代表角色身份切换**（身份由构造时 instance 绑定）。完整删除 attach_session 留待 M3。
- [ ] `TurnCharacterContext` 在 `_prepare_turn()` 冻结，后续链路用同一 context；
- [ ] session create/旧会话绑定 CharacterInstance；旧 session 有兼容迁移；
- [ ] **`attach_session` 调用面替换（M1/M2 阶段，本批范围）**（经核实，真实调用面为 **3 个 SessionManager 生产点 + 1 个 CognitiveLoop 生产点**）：`SessionManager.bind_character_engine`（:368，经 agent.py:212 间接）、`SessionManager.switch`（:362）、`SessionManager.delete`（:581，活动会话删除兜底）与 `CognitiveLoop._prepare_turn`（:340）里的 `attach_session(target)` 改为「经 session 的 `character_binding` 解析目标 instance」。**本批 M1/M2 阶段 engine 仍为全局单例（batch-3 现状），`attach_session` 仍换绑 state 视图**；`character_binding` 仅作为「该会话绑定哪个 instance」的身份标记写入（M1），不驱动多 instance engine（那是 M3）。`SessionManager.create`/`load_active` 实际不调 `attach_session`（已核实），无替换点。测试夹具（`test_engine.py` 多处、`test_session_manager.py` 的 stub engine）同步更新。
- [ ] **本批 M1/M2 多角色的真实边界**：`character_binding` 标记 + `CharacterRuntime`/`CharacterRegistry` 骨架建立（多角色**领域模型 + 运行时容器**就绪），但**运行时仍是全局单例 engine**——「多角色并存、各自独立 engine」是 M3/H 语义。本批验收：binding 解析正确、领域模型可用、Elysia byte 不变、Cyrene 只有 user_pronoun 意图修复。**「双角色跨会话互不污染（各用独立 engine）」不在本批 M1/M2 验收**（需 M3 多 instance engine）。
- [ ] **旧会话 shadow migration**（架构稿 §15.3）：为每个旧 session 建立确定性 legacy instance（由 sessionId 哈希生成），其 `character_state` 原样复制到 instance.state；session 增 `character_binding` 指向该 legacy instance；旧会话行为/状态不变；新建 Elysia 会话用 canonical Elysia instance。迁移期 legacy `character_state` 仍权威（M1），`character_instances` 幂等 backfill（M2），稳定后才切换权威（M3，本批不切）。
- [ ] **多角色 binding 解析（M1/M2 本批）**：session 的 `character_binding` 正确记录其绑定的 instance（默认会话→Elysia canonical instance；旧会话→确定性 legacy instance）。**「双角色并行各自独立 engine、互不污染」是 M3 语义，本批不做**（M1/M2 全局单例 engine，多角色领域模型/容器已就绪但运行时未分片）——留待阶段 H/M3。

### 7.3 user_pronoun 正位（B1 决断：意图修复）
- [ ] `PromptAssembler` 从 `TurnCharacterContext` 读 `pronouns`，不再读 `self._character.user_pronoun`；
- [ ] Elysia 渲染 `pronouns=她` byte-for-byte 不变（她→她）；
- [ ] **Cyrene 渲染从「你」修正为「她」（意图修复，非回归）**——测试断言同步从 `你` 更新为 `她`；
- [ ] `CharacterConfig.user_pronoun` 标记 deprecated，但不物理删除（阶段 D 再清）；
- [ ] `UserIdentity.pronouns` bootstrap 即权威「她」，全局唯一，非 per-role。

### 7.4 契约
- [ ] `frontend_contract` DTO 生成 TS/schema/raw_ts 成功；
- [ ] `character_profiles` feature flag 仍 `false`；
- [ ] **flag-off 序列化抑制**：本批新增 DTO/字段（UserIdentityProfile/CharacterPublicState 等）在 `character_profiles=false` 时**不序列化**，只存在于 schema/golden 中供离线对拍（架构稿 §12.8）。

### 7.5 行为不变（修订：Elysia byte 不变，Cyrene 意图修复）
- [ ] Elysia 单角色渲染 byte-for-byte 不变（沿用 B-1 `elysia_config` + `test_elysia_snapshot_unchanged`）；
- [ ] **Cyrene 单角色渲染**：`pronouns` 修正为「她」（意图修复）；其余像素（C2/C3/C7 内容、speaking_style、wrapper）不变；
- [ ] 泄漏四组测试不回归（Cyrene 角色词 `not in` / 共享格式词 `in` / Elysia `in` 保留）；
- [ ] `make lint` / `make pyright` / `test-quick` 全绿。

---

## 8. 受影响模块

| 模块 | 改动 |
|------|------|
| `core/profile/`（新） | `UserIdentity` + bootstrap + CRUD |
| `core/character/`（新） | `Definition`/`Revision`/`Instance` + `BuiltinCharacterSource` + `CharacterCatalog` + `RuntimeRegistry` + `CharacterRuntime` |
| `core/personality/engine.py` | 构造携带 instance 身份（`instance_id`/`character_revision`）；`attach_session` 收窄为 state 视图换绑（M1/M2 保留，M3 删除）；子组件从同一 runtime 取数据（骨架） |
| `core/prompt/prompt_assembler.py` | `user_pronoun` 改读 TurnCharacterContext（B1 正位） |
| `core/cognitive/` | `TurnCharacterContext` + `_prepare_turn()` 冻结 + userPronoun 来源 UserIdentity |
| `core/memory/` | session 绑定 `character_binding`（character_instance_id）；旧 session shadow migration（legacy instance） |
| `core/agent/` | 注入 `UserIdentityStore`；`_prepare_turn` 设 `_turn_context` 并传给 prompt；`agent.character_engine` 改造 |
| `core/config/character_config.py` | `user_pronoun` deprecated 标记 |
| `frontend_contract/` | 新增 DTO（`UserIdentityProfile`/`CharacterSummary`/`CharacterInstanceSummary`/`CharacterPublicState`）+ feature flag false + **flag-off 序列化抑制** |
| 相关测试 | 领域模型/registry/迁移/user_pronoun 正位测试 |

---

## 9. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 把「1 engine 全局换绑」改「每 instance 一 engine」破坏现有行为 | `TurnCharacterContext` + Elysia byte-for-byte 守门；default instance = 现 behavior |
| `attach_session` 删除牵动 batch 3 multi-session | 迁移为 `session → binding → registry.resolve(instance)`；shim 保兼容 |
| `user_pronoun` 语义错位（自由文本 vs 第三人称） | 语义对齐决策（§5.1）：约定 pronouns 值域，回退链保底 |
| 旧 session `character_state` 以 session_id 为身份 | shadow migration（架构稿 §15.3）：**每旧 session 建确定性 legacy instance**（sessionId 哈希），state 原样复制；**不做默认 instance 承接**（避免自动合并/丢失各会话独立的人格演化）<sup>C1</sup> |
| engine「instance 身份」（构造时定）与「state 视图」（会话换绑）解耦的语义 | M1/M2 本批：engine 仍全局单例，`attach_session` 只换绑 state 视图；instance 身份仅作为标记（`instance_id`/`character_revision`）写入。`agent.character_engine` 仍返回全局单例（语义不变）。shutdown 仍走现有 `_flush_reflector`/`_flush_compression`。**M3 才切换为多 instance engine + `registry.close_all()`**<sup>A1</sup> |
| `set_session()`（session-bound 的 Emotion/OwnLife/Memory section）与 TurnCharacterContext 冻结相冲突 | `set_session` **保留**（assembly 的 session-bound 部分不变），`TurnCharacterContext` 只接管**身份**字段（user_pronoun 等），两者不冲突<sup>A3</sup> |
| 批次过大 | 渐进式：不重写 manifest v2、不做 custom CRUD、不做记忆 scope（`ScopedMemory` 仅占位）、flag 恒 false |

---

## 10. 后续（阶段 D/F/H 预告）

- **阶段 D**：Custom Character 持久化 + CRUD + 版本 + 冲突保护 + preview；删 `CharacterConfig.user_pronoun`；
- **阶段 F**：记忆 v2 隔离（`ScopedLongTermMemory`/`MemoryScope`/`long_term_memory_v2`）；
- **阶段 H**：旧数据迁移 + 前端联调 + `character_profiles=true`。

---

## 11. 事实基线附录（已核实）

- `character/elysia/config.yaml:9` `user_pronoun: 她`；`character/cyrene/config.yaml:9` `user_pronoun: 你`；
- `core/config/character_config.py:29` 默认 `user_pronoun="她"`；
- `agent.py:201` 构造 **1 个** engine + `:212` `bind_character_engine`；
- `engine.py:341` `attach_session()` 换绑 state 视图（batch 3 多会话）——同时换绑 `self.state`/`self._compression._state`/`self._emotion._state`/`self._awareness._state`（四处），并 `session._pre_save_hook = sync_reflector_to_state`；
- `attach_session` 真实调用面：`session_manager.py:368`(bind)/`:362`(switch)/`:581`(delete 兜底) + `loop.py:340`(_prepare_turn)。**`create`/`load_active` 不调**（已核实）；
- `SessionManager.evict_if_needed()`（:748）逐出最久未用非活动会话——M2 registry 驱逐耦合点；
- `agent.character_engine`（agent.py:948）`@property` 返回单 engine——改造后解析为活动会话 binding→instance→engine；
- `sync_reflector_to_state`（engine.py:333）被设为 `_pre_save_hook`——即 session.save() 把 reflector buffer 同步进 `session.character_state`（legacy 权威写入路径）；
- `C1 personality` section 存 `_user_pronoun` 但**不传给 render**（模板 `c1_personality.j2` 不引用 `{{ user_pronoun }}`）——是死代码，迁移时可不改；
- `prompt_assembler.py:152` `user_pronoun=self._character.user_pronoun`；
- `UserIdentity`/`UserIdentityProfile`/`CharacterSummary`/`CharacterPublicState` 全代码库**不存在**（grep 无）；
- `ServerInfoFeatures`（frontend_contract/ws_message.py:264）8 个 bool 字段全 `default=False`，"严禁超前声明"；`build_server_info()`（core/services/capabilities.py:98）设置当前能力——`character_profiles` 字段不存在；
- `core/cognitive/user_profile.py` 现有 `UserProfile` 占位（DelegationPreparer 只读消费，权威模型迁 `core/profile`）；
- `core/profile/`、`core/character/` 包**不存在**（新建）；
- session.character_state（[session.py:101](core/memory/session.py#L101)）`CharacterState(session_id=...)` 以 session_id 为身份。
