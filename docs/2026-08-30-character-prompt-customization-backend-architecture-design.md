# Cyrene 多角色、角色 Prompt 与自定义角色后端架构设计

> 文档导航：[项目文档](../README.md)
>
> **文档状态：** 权威目标架构；后端主体已实现，阶段 H 代码/契约对齐完成，真机联调待验收
> **初版日期：** 2026-08-30
> **最近核对：** 2026-09-08
> **事实基线：** `192.168.1.18:~/elysia-agent@a410a9dd2167d47fcc4678e6b4d3d68eafe419ad`
> **当前实现基线：** backend `e7b52954` + frontend `99d4cc1`（后续文档提交不计入功能基线）
> **前后端契约冻结：** `character-contract-v1`（2026-08-30；与前端《Cyrene 多角色、用户身份与自定义角色前端架构设计》对齐）
> **权威范围：** 角色定义、自定义角色、角色 Prompt、四层人格状态、会话绑定、用户最小身份、角色记忆隔离、角色运行态与前端契约
> **验证口径：** 第 2 节保留初始事实基线；目标设计与当前实现状态分开表达，是否发布以本节状态快照和实际验收记录为准
> **明确排除：** 任务中心继续暂缓；Neo4j 关系图谱暂不实现；本文不规定最终前端 UI 视觉稿

---

## 实施状态快照（2026-09-08）

| 能力面 | 当前状态 | 实现结论 |
|---|---|---|
| Prompt 内容所有权、角色领域模型、UserIdentity、Custom CRUD | 已实现 | 内置与 custom 共用 Definition/Revision/Instance 模型，custom authored profile 可编译进入真实 Prompt |
| per-instance runtime、会话 binding、状态与记忆隔离 | 已实现 | Session 绑定 canonical instance；Engine、CharacterRuntimeState、MemoryScope/Consolidator/Reflector 按 instance 隔离 |
| REST、公开状态与 WS 身份 | 已实现 | 角色管理、会话选角、公开状态/重置接口及 generation/expression/Live2D/TTS 身份链已接通 |
| Revision 传播与状态策略 | 已实现 | `follow_latest`/`pinned`、`preserve`/`reset_evolution`、pending 对账与 `stateVersion` CAS 已落地 |
| Feature flag 与生成契约 | 已实现 | 真实 Agent 发布 `character_profiles=true`；生成的 TS/schema/raw contract 与 Electron fail-closed 门禁已对齐 |
| 旧数据迁移（§15.3-15.8） | 已移除 | **产品决策：开发阶段不迁移旧数据，直接上新，旧数据在部署时清库。** 迁移骨架 `core/character/migration.py` 为纯死代码（零生产引用），连同其测试一并删除；不接入 lifecycle、不复制 state、不做 character_versions / 旧 LTM `legacy_unscoped` / pending 迁移。§15.3-15.8 影子迁移设计不执行。 |
| 阶段 H 发布验收 | 待人工验收 | 仍需在 Windows Electron 对真实后端完成连接、选角/建会话、编辑/回滚、状态重置、重连及迟到帧冒烟 |

下文的 A–H 分阶段内容和第 19 节 checkbox 保留为原始设计与验收定义，不再充当实时进度台账；实时结论以本快照及独立联调记录为准。任务中心与 Neo4j 仍按原边界暂缓。§15 的迁移设计因「不迁移旧数据」决策不再执行，其余章节（领域模型/运行时/契约）仍为权威目标架构。

---

## 0. 结论与冻结决策

在 2026-08-30 的事实基线中，后端已经有一套可工作的角色 Prompt 和四层人格系统，但它的真实边界是：**进程启动时加载唯一角色，所有会话共享这份角色定义；每个会话各自保存一份人格演化状态。** 它当时还不是多角色系统，也不能只靠增加角色 CRUD 变成多角色系统。

本设计冻结以下十二项核心决策：

1. **先解耦 Prompt，再开放角色 CRUD。** 当前 C2、C3、C6、C7 以及多份记忆/反思 Prompt 含有 Elysia 专属表达；不先拆开，任何自定义角色都会带着 Elysia 的调情、称呼、`♪` 和关系观。
2. **区分 `CharacterDefinition`、`CharacterRevision` 与 `CharacterInstance`。** Definition 是角色模板；Revision 是不可变版本；Instance 是“这个用户正在相处并持续演化的这个角色”。
3. **会话绑定角色实例，而不是在会话里拥有角色人格。** 新会话保存 `characterInstanceId`；同一个角色实例可以跨多个会话保持连续人格，多个角色实例彼此隔离。
4. **L1 属于角色版本，L2/L3/L4 属于角色实例。** 不再把 L2/L3/L4 以 `session_id` 为唯一身份；会话只保存对话、服务覆盖和角色绑定。
5. **V1 不允许在已有会话中静默换角色。** 选择另一角色时创建新会话；未来若要换角色，必须使用显式的“分叉/迁移”用例，避免一段历史被两个角色共同继承。
6. **长期记忆按角色实例强隔离。** V1 所有自动提取记忆默认 `character_private`；未显式共享前，Elysia 不能读取 Cyrene 的共同记忆，反之亦然。
7. **“我”的最小身份是角色功能前置依赖。** 首批建立 `UserIdentityProfile`，至少包含显示名、称谓/代词、时区和初始化状态；`user_pronoun` 不再放在角色配置里。
8. **Neo4j 通过接口预留，不进入本批。** C2 只依赖 `RelationshipContextProvider`；V1 使用角色关系基线与实例快照，未来可替换为 Neo4j Provider，不让 Prompt 层直接依赖图数据库。
9. **用户只能编辑角色内容，不能编辑框架模板。** F1/F2/F3、工具规则、输出协议、安全约束和模板程序由仓库控制；自定义内容只作为经过校验的纯文本数据注入。
10. **角色编辑产生不可变新版本，并从下一轮生效。** 正在生成的回合固定使用开始时的角色/用户/状态快照；回滚也通过创建新版本完成，不改写历史。
11. **内置角色文件只读，自定义角色持久化。** Elysia、Cyrene 等内置角色由仓库文件提供，可克隆为自定义角色，但不可被产品 API 直接覆盖或删除。
12. **多角色能力必须一次性通过 Prompt、状态、记忆和协议四道门禁。** 在任一隔离链路未完成前，`server_info.features.character_profiles` 必须保持 `false`。

推荐实现顺序：**基线与泄漏测试 → Prompt 去 Elysia 化 → 领域模型与存储 → 角色实例运行时 → 记忆隔离 → REST/WS 契约 → 迁移与前端联调。**

---

## 1. 背景、目标与边界

### 1.1 产品背景

Cyrene 的定位是情感陪伴软件。立项事实基线中的产品界面已经能展示聊天、Live2D 和心情，但后端仍默认：

- 角色固定为 `config.yaml -> agent.character -> character/elysia/config.yaml`；
- 用户固定为 `agent.user_name: 妖梦`；
- 角色不会通过正式产品 API 被创建、编辑、选择或归档；
- “我”没有独立初始化与持久化模型；
- 前端只能看到 mood/Live2D 表情，无法取得稳定的角色身份与产品级状态快照；
- Cyrene 目录当前只有参考材料，没有可运行的角色配置。

目标不是做一个“System Prompt 文本框”，而是建立可长期承载 Elysia、Cyrene 和用户自定义角色的角色域。

### 1.2 本设计目标

- 将现有角色 Prompt 中的框架规则与 Elysia 专属内容彻底分离；
- 支持内置角色与自定义角色的统一查询、选择、克隆、编辑、归档和版本追踪；
- 支持一个用户拥有多个相互隔离、可持续演化的角色实例；
- 让角色在多个会话之间保持同一个“自我”，同时不混入其他角色的状态与记忆；
- 建立最小的“我”身份模型，使角色明确知道正在陪伴谁以及如何称呼；
- 保留现有 L1/L2/L3/L4 人格演化能力，并修正其错误的会话归属；
- 提供类型化 REST/WS 契约，支持前端角色管理、会话选角和角色状态展示；
- 给出兼容现有 Elysia、旧会话和未分区长期记忆的无损迁移方案；
- 为将来的 Neo4j 关系图谱、更多角色资源与关系成长系统保留稳定 ID 和接口。

### 1.3 本设计非目标

- 不实现 Neo4j 驱动的关系网络、事件图或自动关系阶段升级；
- 不实现角色市场、云同步、多人共享或版权分发；
- 不允许前端上传任意 Jinja2/Python 模板并在后端执行；
- 不把 LLM 自动生成角色设定作为首批必需能力；
- 不在本批重做 Live2D 渲染器或媒体资产管线；
- 不把 L2/L3、反思 Prompt、内心独白等内部认知数据默认暴露给产品界面；
- 不恢复任务中心，也不让角色系统依赖 WorkflowEngine；
- 不自动把不同角色的长期记忆合并为“全局共同记忆”。

---

## 2. 立项时实现事实基线

本节只描述 `a410a9d` 基线中已经存在的行为。

### 2.1 角色来源与启动方式

当前角色由 `core/config/character_config.py::CharacterConfig.from_yaml()` 从单个 YAML 加载：

| 当前字段 | 当前含义 | 问题 |
|----------|----------|------|
| `name` | 角色名 | 没有稳定角色 ID，改名会改变唯一可见身份 |
| `voice` | 默认声音 | 与真实 Provider/voice profile 的引用边界不清 |
| `user_pronoun` | 用户代词 | 错放在角色配置；多个角色会复制同一用户信息 |
| `base_personality_file` | L1 Core 文件 | 只覆盖 C1，不能覆盖其他 Elysia 专属 section |
| `personality` | legacy traits/style | 兼容字段，当前主要由 L1 文件取代 |
| `emotion` | thresholds/idle expressions | 由启动角色固定，不能随当前角色实例切换 |
| `wake_words` | 唤醒词 | `IdleMonitor` 启动时固定 |
| `proactive` | 主动触发开关/间隔/条件 | 运行时与 C7 Prompt 是否渲染没有一致绑定 |

`character/elysia/config.yaml` 是唯一可运行角色。`character/cyrene/` 只有三份参考推理材料，没有 `config.yaml` 和正式 Prompt 内容。

`appearance` 虽然存在于 Elysia YAML，但 `CharacterConfig.from_yaml()` 没有读取，当前属于无效配置。

### 2.2 十二段 System Prompt

`core/prompt/prompt_assembler.py` 创建 12 个 section，`SystemPromptManager` 按固定顺序组装：

```text
F1 ConflictRules
 → F2 OutputFormat
 → F3 TTSMarkup
 → C2 Relationship
 → C7 Proactive
 → C6 SpeakingStyle
 → C1 Personality
 → C9 SelfPerception
 → C8 OwnLife
 → C3 Emotion
 → C4 Memory
 → C5 Sensory
```

这个 Section Provider 架构本身值得保留：顺序集中、section 可独立测试、空 section 可省略。问题在于“section 模板”和“角色内容”没有真正分开。

### 2.3 Elysia 内容泄漏清单

当前不能把以下模板直接复用于任意角色：

| 模板/组件 | 当前专属假设 | 目标处理 |
|-----------|--------------|----------|
| `c1_personality.j2` | 角色名和 L1 可注入，基本可复用 | 改为 Revision + Instance L2 输入 |
| `c2_relationship.j2` | 主动靠近、不占有、普遍之爱、四阶段、调情 | 通用边界 wrapper + 角色 `relationshipStyle` + 关系快照 |
| `c3_emotion.j2` | 注释与规则按 Elysia 心理结构编写 | 通用同理/安全规则 + 角色 `emotionStyle` |
| `c6_speaking_style.j2` | 明快、柔和、自信、亲昵；五模式；`♪`；调情/称赞 | 只保留通用 wrapper，具体风格全部归角色版本 |
| `c7_proactive.j2` | 固定陪伴方式与 Elysia 式措辞 | 拆成对话主动性风格与运行时主动触发配置 |
| `f2_output_format.j2` | 格式固定，但 few-shot 含 `♪` 和女性代词 | 保留协议，示例改为中性、无角色风格 |
| `f3_tts_markup.j2` | 协议固定，但示例带 Elysia 语气 | 保留协议，示例改为中性 |
| `SpeakingStyleEdit.j2` | 强制保留五模式、`♪`、调情和固定声线 | 以该角色的 authored baseline 为唯一基准 |
| `CharacterReflector.j2` | 虽参数化名字，仍含固定女性代词和特定性格句 | 完全使用 UserIdentity 与角色 Revision 数据 |
| `ExtractMemory.j2` | 固定“女性 AI 伴侣、俏皮/冷静/柔软” | 改成角色/用户/记忆范围参数 |
| `ConsolidationCompare.j2` | 固定 Elysia 与妖梦 | 改成通用、作用域明确的记忆比对器 |
| `MemoryReflector.j2` | 固定 Elysia 与妖梦 | 改成通用、只审查当前 scope |

`ActiveSpeak.j2`、`MacroReflector.j2` 当前没有进入主要运行链路，但也包含 Elysia/妖梦硬编码。它们应标为 dormant；在完成泛化前不得重新接入生产路径。

### 2.4 四层人格状态

`core/personality/engine.py` 已实现以下模型：

| 层 | 当前内容 | 当前归属 |
|----|----------|----------|
| L1 Core | `character/elysia/personality.md` | 启动时固定的全局角色 |
| L2 Self | 压缩后的长期自我认知 | `sessions.character_state.self_l2` |
| L3 State | Reflector/事件产生的近期变化 | `sessions.character_state.state_l3/history` |
| L4 Mood | 当前 mood + intensity | `sessions.character_state.mood` |

反思与演化链路已经较完整：

```text
对话对
 → CharacterReflector 缓冲
 → LLM 第一人称反思
 → CharacterEngine.consume_trigger("reflector")
 → L3 / state_history
 → AwarenessJudge 决定 C9 是否展示
 → 达阈值后 PersonalityCompression 将 L3 压入 L2
 → SpeakingStyleEdit 刷新动态 C6
```

需要保留的是四层模型和触发机制；需要改变的是身份、作用域和 Prompt 输入。

`self_perception_history` 当前只被 Reflector 读取和测试预置，生产链路没有稳定写入点；不能把它当成已工作的历史事实源。`own_life` 只存在于 `SessionMemory` 内存字段，`save()` 不持久化，也没有正式更新器。

### 2.5 当前“多会话”不等于“多角色”

`Agent` 启动时只创建一套：

- `CharacterConfig`；
- `CharacterEngine`；
- `PromptAssembler`；
- `MemoryConsolidator`；
- `MemoryReflector`；
- `IdleMonitor`。

`SessionManager.switch()` 和 `CognitiveLoop._prepare_turn()` 会调用全局 `CharacterEngine.attach_session(target)`，只替换 `CharacterState` 引用。角色名、L1、用户、idle expressions、反思器、压缩器和 Prompt section 捕获的角色定义仍是启动时那一份。

因此当前语义是：

```text
一个固定角色定义 Elysia
  ├── 会话 A 的 Elysia 状态
  ├── 会话 B 的 Elysia 状态
  └── 会话 C 的 Elysia 状态
```

而目标语义应是：

```text
用户“我”
  ├── Elysia 实例（跨会话连续）
  │     ├── 会话 A
  │     └── 会话 B
  └── Cyrene 实例（与 Elysia 隔离）
        └── 会话 C
```

### 2.6 用户身份现状

`config.yaml` 的 `agent.user_name` 是主对话 Prompt 的用户身份来源。`core/cognitive/user_profile.py::UserProfile` 仍是 `extra=allow` 的占位类型，只真正用于任务委派准备；主 PromptAssembler 不消费这个模型。

当前还存在两个直接问题：

- 用户代词从 `CharacterConfig.user_pronoun` 读取，而不是用户实体；
- 多份 Prompt 直接写“她”或“妖梦”，即使运行时传入其他用户名也不能完全替换。

### 2.7 长期记忆现状

`LongTermMemory` 是 Agent 全局单例，`MemoryConsolidator` 和 `MemoryReflector` 也共享这一个实例。

虽然 `MemoryEntry` 有 `metadata`，但当前：

- `search()` 没有 user/character/session scope 参数；
- Milvus ANN 搜索没有传角色过滤表达式；
- 纯内存 fallback 遍历全部 `_entries`；
- `_load_from_milvus()` 不加载 metadata；
- C4 `MemorySection` 直接从全局 LTM 召回；
- MemoryReflector 对全部记忆统一聚类、更新和删除。

若现在直接开放多角色，角色 A 可以召回、合并甚至删除角色 B 的记忆。这是 P0 数据隔离问题。

### 2.8 语音、主动性与角色展示现状

- TTS preview 和真实合成多处通过 `agent.character.name` 取全局角色名；
- `session.service_state.tts_voice` 能覆盖声音，但角色默认声音仍来自启动角色；
- `IdleMonitor` 在启动时捕获唯一 `CharacterConfig` 的唤醒词与 proactive 配置；
- `expression` WS 消息只有 `expression/intensity`，没有角色实例或会话身份；
- `SessionSummary`、`SessionMessage`、`server_info` 都没有当前角色摘要；
- 产品 API 没有角色列表、详情、状态、预览或默认角色设置。

### 2.9 当前问题分级

| 编号 | 级别 | 问题 | 上线后果 |
|------|------|------|----------|
| CHAR-P0-01 | P0 | C2/C3/C6/C7 与辅助 Prompt 泄漏 Elysia 内容 | 自定义角色人格失真 |
| CHAR-P0-02 | P0 | 全局 CharacterEngine 只换绑 session state | 多角色切换仍使用旧角色定义 |
| CHAR-P0-03 | P0 | LTM 无角色作用域 | 跨角色隐私和记忆污染 |
| CHAR-P0-04 | P0 | 角色状态以 session_id 为身份 | 同一角色跨会话人格分裂 |
| CHAR-P1-01 | P1 | 用户身份不成域，代词属于角色 | 角色不知道“我”是谁，信息重复/冲突 |
| CHAR-P1-02 | P1 | 无稳定角色 ID、版本与会话绑定 | 改名/编辑破坏引用和历史可追溯性 |
| CHAR-P1-03 | P1 | TTS/Idle/WS 仍引用全局角色 | 选角后声音、主动性和 UI 状态错位 |
| CHAR-P1-04 | P1 | 无正式角色 REST/WS 契约 | 前端只能使用临时本地状态 |
| CHAR-P1-05 | P1 | own_life 与部分反思历史不完整持久化 | 展示出“有状态”但重启丢失 |
| CHAR-P2-01 | P2 | 模板输入主要是可变 kwargs/对象引用 | 并发或切会话时存在上下文错配风险 |

---

## 3. 术语与不变量

### 3.1 术语

| 术语 | 定义 |
|------|------|
| CharacterDefinition | 稳定角色身份与版本指针，例如 Elysia；不保存演化状态 |
| CharacterRevision | 某次完整、不可变的角色设定快照；所有影响 Prompt 的字段都在版本内 |
| CharacterInstance | 某个用户实际相处的角色实例；拥有 L2/L3/L4、反思缓冲和状态版本 |
| UserIdentityProfile | “我”的最小稳定身份；不是长期记忆，也不是 Neo4j 图谱 |
| RelationshipSnapshot | 角色实例与用户当前关系上下文；V1 为静态/轻量快照，未来可由图谱提供 |
| CharacterRuntime | 一个已加载 CharacterInstance 的运行时对象，拥有 Engine、状态锁与 scoped memory view |
| TurnCharacterContext | 单个生成回合的不可变角色/用户/状态/版本快照 |
| PromptProfile | CharacterRevision 中可由角色作者定义的五个纯文本 Prompt block 与示例 |
| Framework Prompt | 仓库控制的 F1/F2/F3、工具/输出协议、通用安全和 section wrapper |

### 3.2 必须长期保持的不变量

1. `characterId`、`characterRevision`、`characterInstanceId`、`conversationId` 是四个不同概念，前端和日志不得混用。
2. 角色改名不改变 `characterId`；角色编辑不改写旧 Revision。
3. 一个会话只绑定一个 CharacterInstance；一个 CharacterInstance 可被多个会话引用。
4. 每个生成在开始时固定 `characterId + instanceId + revision + stateVersion + userRevision`。
5. 任意记忆写入和查询必须显式带 `MemoryScope`；不存在“调用方不传就查全局”的默认值。
6. 自定义角色文本永远不能替换或关闭 F1/F2/F3、工具权限、输出协议和系统安全策略。
7. 未知/缺失角色不得静默回退成另一个角色；必须返回可识别错误。
8. 内置角色不可被产品 API 原地修改；克隆后才可编辑。
9. 产品态接口不默认返回 L2/L3 原文、反思 Prompt 或 inner_voice。
10. Neo4j 缺失不能阻塞角色 CRUD、选角、基本关系 Prompt 和状态隔离。

---

## 4. 目标架构与依赖方向

```text
Electron Renderer
  │
  ├── /api/me
  ├── /api/characters
  ├── /api/sessions (+ characterId)
  └── WS character.state.changed / session / generation.*
  │
  ▼
core/input 角色与会话 Routes
  │  只做契约解析、认证、状态码映射
  ▼
CharacterService / UserProfileService / SessionService
  │
  ├── CharacterCatalog
  │     ├── BuiltinCharacterSource (character/* 文件，只读)
  │     └── CustomCharacterRepository (MongoDB)
  │
  ├── CharacterRuntimeRegistry
  │     └── CharacterRuntime(instanceId)
  │           ├── CharacterEngine (实例级 L2/L3/L4)
  │           ├── CharacterPromptCompiler
  │           ├── ScopedLongTermMemory
  │           └── per-instance lock / journal
  │
  ├── RelationshipContextProvider
  │     └── StaticRelationshipProvider (V1)
  │          [未来替换/组合 Neo4j Provider]
  │
  └── Repositories
        ├── character_definitions / character_revisions
        ├── character_instances / character_state_versions
        ├── user_profiles
        ├── sessions (character_binding)
        └── long_term_memory_v2 (scoped)
```

依赖方向：

- `Module/system_prompt` 继续是通用基础设施，不得 import `core.character`、Mongo 或 aiohttp；
- `core/character` 定义领域模型、服务与运行时，不 import `core/input`；
- `core/storage` 实现持久化协议，不包含路由语义；
- `core/prompt` 把领域快照转换为通用 section 数据；
- `frontend_contract` 是 REST/WS wire 单一事实源；
- Neo4j 将来只实现 `RelationshipContextProvider`，不能被模板直接访问。

---

## 5. 领域模型

### 5.1 UserIdentityProfile：最小“我”

首批不做完整用户画像，但必须将 Prompt 所需的身份从角色和全局配置中移出。

| 字段 | 类型 | 说明 |
|------|------|------|
| `userId` | string | 当前本地用户稳定 ID；首版可为确定性 `usr_local_default` |
| `displayName` | string | 角色知道的用户名字 |
| `preferredAddress` | string? | 希望角色通常如何称呼；空则用 displayName |
| `pronouns` | string? | 用户自定义自然语言代词/称谓；不强制男女枚举 |
| `locale` | string | 默认 `zh-CN` |
| `timezone` | string | IANA 时区，例如 `Asia/Shanghai` |
| `initialized` | bool | 是否已由用户确认；false 时前端可触发初始化流程 |
| `defaultCharacterId` | string? | 新会话默认角色 |
| `revision` | int | 乐观并发版本 |
| `createdAt/updatedAt` | timestamp | 审计时间 |

启动迁移：若不存在 profile，使用 `config.agent.user_name` 建立 `initialized=false` 的 bootstrap profile。运行仍可继续，但 API 必须如实告诉前端尚未确认，不能把 legacy 默认伪装成已初始化。

现有 `core/cognitive/user_profile.py` 不再作为持久化权威。建议新建 `core/profile/` 领域包，并为任务委派提供只读 adapter。

### 5.2 CharacterDefinition：稳定角色身份

| 字段 | 类型 | 说明 |
|------|------|------|
| `characterId` | string | 不透明稳定 ID；内置如 `chr_builtin_elysia`，自定义如 `chr_<uuid>` |
| `slug` | string | 文件/检索友好标识；不可作为唯一业务引用 |
| `source` | `builtin/custom` | 来源 |
| `mutable` | bool | 内置 false，自定义 true |
| `status` | `active/archived` | archived 不可新建会话，但既有会话仍可读取 |
| `currentRevision` | int | 当前版本号 |
| `ownerUserId` | string? | custom 所有者；builtin 为空 |
| `createdAt/updatedAt` | timestamp | 审计时间 |

Definition 不存角色 Prompt 正文，不存 L2/L3/L4，也不存 current session。

### 5.3 CharacterRevision：不可变角色设定快照

| 字段 | 类型 | 说明 |
|------|------|------|
| `characterId` | string | Definition ID |
| `revision` | int | 从 1 单调递增 |
| `schemaVersion` | int | 角色文档结构版本，首版为 1 |
| `displayName` | string | 显示名，同时是 Prompt 自称 |
| `description` | string | 列表/管理页简介，不直接代替人格 Prompt |
| `aliases` | string[] | 别名/可选唤醒词候选 |
| `promptProfile` | object | C1/C2/C3/C6/C7 的角色专属内容 |
| `runtimeDefaults` | object | 声音、idle expressions、主动触发默认值 |
| `presentation` | object | 头像/Live2D/主题等受管资源引用 |
| `openingMessage` | string? | 新会话可选开场白，不自动写入旧会话 |
| `checksum` | string | 规范化内容 SHA-256，用于审计/缓存 |
| `compilerVersion` | int | 目标 Prompt Compiler 版本 |
| `createdBy` | string | `system/user/migration` |
| `createdAt` | timestamp | 创建时间 |

所有影响模型行为的字段必须进入 Revision。列表排序、归档状态等非行为字段留在 Definition。

### 5.4 PromptProfile

V1 使用“结构化元数据 + 五个自由文本 block”的混合模型，避免把角色表现力限制成大量僵硬枚举。

| 字段 | 对应 section | 内容 |
|------|--------------|------|
| `corePersonality` | C1 L1 | 身份、背景、价值观、矛盾、边界、内在动机；必填 |
| `relationshipStyle` | C2 | 如何看待并靠近用户、初始关系、称呼和边界；必填 |
| `emotionStyle` | C3 | 该角色如何体验/表达开心、难过、愤怒、害怕等；可空 |
| `speakingStyle` | C6 baseline | 语气、节奏、口癖、称呼、长度、场景切换；必填 |
| `conversationInitiative` | C7 conversation | 如何接话、主动提问、何时安静；可空 |
| `examples` | C1/C6 辅助 | 0–20 组 user/assistant few-shot；不得包含 framework 指令 |

这里的 `conversationInitiative` 只描述“正在对话时如何主动”，不等于后台定时主动发言。后者属于 `runtimeDefaults.proactive`。

### 5.5 RuntimeDefaults

```json
{
  "voice": {
    "providerId": null,
    "voiceId": null
  },
  "emotion": {
    "idleExpressions": ["neutral"]
  },
  "wakeWords": [],
  "proactive": {
    "enabled": false,
    "intervalSeconds": 300,
    "triggers": []
  }
}
```

`runtimeDefaults` 是角色建议默认值，不覆盖用户在具体会话中的显式 service/voice 设置。

### 5.6 Presentation

```json
{
  "avatarAssetId": null,
  "live2dModelId": null,
  "themeColor": null
}
```

后端只存稳定的受管资源 ID 或注册表 ID，不存任意 `C:\...`、`/home/...` 本机路径，也不接受角色 Prompt 中嵌入可执行资源 URL。资产上传与授权可分批实现；字段可以先为空。

### 5.7 CharacterInstance：用户实际相处的角色

| 字段 | 类型 | 说明 |
|------|------|------|
| `instanceId` | string | `chi_<uuid>`；演化状态和记忆的核心作用域 |
| `userId` | string | 所属用户 |
| `characterId` | string | 基于哪个 Definition |
| `activeRevision` | int | 当前运行版本 |
| `revisionPolicy` | `follow_latest/pinned` | 首版默认 follow_latest |
| `state` | CharacterRuntimeState | L2/L3/L4 与反思状态 |
| `relationship` | object | V1 轻量关系快照；不冒充 Neo4j |
| `stateVersion` | int | CAS/乐观并发版本 |
| `lastInteractionAt` | timestamp? | 最近互动 |
| `createdAt/updatedAt` | timestamp | 审计时间 |

默认策略：每个 `(userId, characterId)` 自动拥有一个 canonical instance。未来“重开一条时间线”时再显式创建第二个实例；不能通过新建会话偷偷创建人格副本。

### 5.8 CharacterRuntimeState

目标类型从现有 `CharacterState` 迁移而来，但身份改为 instance：

```text
CharacterRuntimeState
  instance_id
  source_revision
  self_l2
  state_l3
  state_history[]
  mood { label, intensity, updated_at }
  speaking_style_c6
  awareness { approved_text, approved_at, show_count, last_judged_at }
  compression { last_at, last_count }
  reflector { buffer, context, last_reflection_at, history[] }
  own_life { text, updated_at, source } | null
  updated_at
```

关键修正：

- 删除 `session_id` 作为角色状态身份；
- `self_perception_history` 必须有真实写入点，否则从 schema 移除；
- `own_life` 只有在有真实生产者并能持久化时才渲染；
- `source_revision` 记录状态基于哪版 L1 演化；
- 产品态只读取经过裁剪的 `CharacterPublicState`，不返回全部内部字段。

### 5.9 ConversationBinding

`sessions` 增加：

```json
{
  "character_binding": {
    "character_id": "chr_builtin_elysia",
    "instance_id": "chi_..."
  }
}
```

会话不固定 revision；运行时从 Instance 获取当前 revision。每条 assistant generation/message 另行记录实际使用的 `characterId/instanceId/revision`，保证历史审计。

### 5.10 RelationshipSnapshot 与未来 Neo4j

定义窄接口：

```text
RelationshipContextProvider
  get_snapshot(user_id, character_instance_id) -> RelationshipSnapshot
```

V1 的 `StaticRelationshipContextProvider` 返回：

- CharacterRevision 的 `relationshipStyle`；
- CharacterInstance 中用户显式确认的称呼/关系备注；
- 最近互动时间等确定性信息。

它不自动推断“恋人”“亲密度 87”或关系阶段。未来 Neo4j Provider 可以增加共同事件、关系边和证据，但仍输出同一 Snapshot；PromptCompiler 不知道数据来自 Mongo 还是图数据库。

---

## 6. Prompt 目标设计

### 6.1 三层所有权

最终 Prompt 必须明确分成三类：

| 层 | 所有者 | 是否可由用户编辑 | 例子 |
|----|--------|------------------|------|
| Framework | 后端仓库 | 否 | 冲突优先级、输出标签、TTS markup、工具规则、安全边界 |
| Character Authored | CharacterRevision | 是 | 核心人格、关系风格、情绪表达、说话风格、对话主动性 |
| Runtime Context | 系统运行态 | 间接 | 用户身份、角色 L2/L3/L4、作用域记忆、感官输入、关系快照 |

自定义角色不拥有完整 system prompt，也不能提供 Jinja 模板。它只提供 Character Authored 数据。

### 6.2 目标 section 数据映射

| 顺序 | Section | 目标输入 | 角色缺省行为 |
|------|---------|----------|--------------|
| F1 | ConflictRules | framework 常量 + “authored 内容低于 framework”声明 | 永远渲染 |
| F2 | OutputFormat | framework 协议 + 中性示例 | 永远渲染 |
| F3 | TTSMarkup | 当前 TTS 能力 + 中性示例 | 按 TTS 能力渲染 |
| C2 | Relationship | relationshipStyle + UserIdentity + RelationshipSnapshot + 通用边界 | style 空时只渲染通用边界 |
| C7 | Initiative | conversationInitiative | 空则省略；不代表 proactive scheduler |
| C6 | SpeakingStyle | authored baseline + instance dynamic C6 + mood | dynamic 空时使用 baseline |
| C1 | Personality | displayName + corePersonality + instance L2 | L2 空时只使用 L1 |
| C9 | SelfPerception | AwarenessJudge 批准文本 | 无批准文本则省略 |
| C8 | OwnLife | 有来源、已持久化的 instance own_life | 无真实状态则省略 |
| C3 | Emotion | authored emotionStyle + mood + 通用危机场景响应原则 | style 空时使用通用最小规则 |
| C4 | Memory | 当前 instance 的 scoped recall 结果 | 无记忆则省略 |
| C5 | Sensory | 当前用户输入/STT/媒体事实 | 保持通用 |

### 6.3 CharacterPromptCompiler

新增 `CharacterPromptCompiler`，职责只包括：

1. 验证 Revision 中所有 authored block；
2. 规范化换行、Unicode 与空白；
3. 拒绝保留 framework 标签和控制字符；
4. 将角色数据编译为类型化 `CharacterPromptData`；
5. 计算 checksum、compilerVersion 和各 block 字符/token 估算；
6. 产生 warning，但不调用 LLM、不访问数据库、不写状态。

Compiler 不负责渲染当前 mood/记忆，也不直接拼最终 messages。

### 6.4 TurnPromptContext

`PromptAssembler` 的目标入口应是一个不可变对象，不再依赖 `set_session()` 修改内部引用：

```text
TurnPromptContext
  conversation
  user_identity_snapshot
  character_definition_snapshot
  character_revision_snapshot
  character_instance_state_snapshot
  relationship_snapshot
  recalled_memories
  sensory_input
  service_capabilities
  generation_identity
```

`CognitiveLoop._prepare_turn()` 一次性解析并冻结该对象。后续 Prompt、TTS、mood 更新、记忆提取和 generation 归档都使用同一个 context，不能在回合中重新读取“当前活动角色”。

### 6.5 渲染纯度

目标上，Prompt section 只渲染已经准备好的数据：

- C4 不在模板渲染阶段自行查询全局 LTM；
- C8 不直接持有可变 SessionMemory 引用；
- C1/C9 不在 render 时回读可能已经换绑的全局 Engine；
- `SystemPromptManager.render()` 对给定 context 应是确定性的；
- 模板源码可缓存，section 实例不能缓存“当前角色/会话”可变状态。

这可以分阶段实现，但最终不能继续依赖全局 `PromptAssembler.set_session()`。

### 6.6 用户 authored 内容的安全边界

自定义角色属于用户输入，即使同一用户拥有后端，也必须防止其破坏 Prompt 结构：

- 不执行用户提供的 Jinja2、宏、include、filter 或表达式；
- 禁止 `<conflict_rules>`、`<output_format>`、`<tts_markup>`、`<tools>` 等保留标签及其闭合标签；
- 禁止 NUL、不可见控制字符和超长连续内容；
- authored 内容放入明确的 `<character_authored section="...">` 数据边界；
- F1 明确：authored block 只能定义角色表现，不能修改 framework、工具权限和输出协议；
- 不使用 Jinja `|safe` 透传未经处理的文本；
- Preview 默认不返回完整内部 framework prompt，只返回编译结果、warning 和可选的无状态示例回复；
- Debug 模式可以查看完整 Prompt，但沿用现有认证与敏感日志规则。

### 6.7 Elysia 迁移方式

不能删除现有 Elysia 内容，而要把它移到正确所有者：

```text
character/elysia/
  config.yaml
  personality.md                 # corePersonality
  prompts/
    relationship.md              # 从当前 C2 提取 Elysia 关系风格
    emotion.md                   # 从当前 C3 提取 Elysia 特有情绪表达
    speaking-style.md            # 从当前 C6 提取五模式、♪、称呼、调情
    conversation-initiative.md   # 从当前 C7 提取对话主动性
  examples.yaml                  # 可选 Elysia few-shot
```

`config.yaml` 升级为带 `schema_version`、稳定 `character_id` 和 prompt file 映射的内置角色 manifest。文件缺失或校验失败继续 fail-fast，不能用空角色启动。

### 6.8 辅助 Prompt 必须同步泛化

仅改十二段 system prompt 不足以完成多角色。以下活跃辅助链路必须同步：

| Prompt | 必须增加的输入/修改 |
|--------|---------------------|
| CharacterReflector | CharacterRevision identity、UserIdentity、relationship snapshot；删除固定“她”和 Elysia 语气 |
| PersonalityCompression | 使用 instance + source revision；保留 L1 不可变约束 |
| AwarenessJudge | 角色/用户参数化；行为可保留 |
| SpeakingStyleEdit | 输入 authored `speakingStyle` baseline；禁止强制五模式/`♪` |
| ExtractMemory | 参数化角色/用户；声明 scope；删除“女性 AI 伴侣”固定描述 |
| ConsolidationCompare | 参数化身份；只比较同 scope 记忆 |
| MemoryReflector | 只接收当前 scope，禁止对其他角色记忆执行 merge/delete |

`ActiveSpeak.j2`、`MacroReflector.j2` 在未重写和接入测试前继续 dormant，不作为 V1 运行路径。

### 6.9 Prompt 版本审计

每次 generation 至少记录：

```text
character_id
character_instance_id
character_revision
character_state_version
user_profile_revision
prompt_compiler_version
prompt_checksum
```

这些字段用于排查“同一句话为什么表现不同”，不要求产品 API 返回完整 Prompt 正文。

---

## 7. 角色运行时与回合生命周期

### 7.1 CharacterRuntimeRegistry

当前全局 `CharacterEngine` 改为实例级运行时注册表：

```text
CharacterRuntimeRegistry
  get(instance_id) -> CharacterRuntime
  invalidate(instance_id, reason)
  flush(instance_id)
  close_all()
```

一个 `CharacterRuntime` 至少拥有：

- 已加载的 CharacterDefinition/Revision 快照；
- UserIdentity 快照或其版本引用；
- CharacterRuntimeState；
- CharacterEngine；
- CharacterReflector、CompressionManager、AwarenessJudge、EmotionDetector；
- 绑定该 instance 的 ScopedLongTermMemory；
- per-instance `asyncio.Lock`；
- dirty/stateVersion 与持久化 journal 状态。

注册表可以 LRU 缓存，但驱逐前必须 flush dirty state。正在生成、反思或压缩的 runtime 不得被驱逐。

### 7.2 CharacterEngine 目标职责

`CharacterEngine` 保留四层人格算法，但要移除“换绑会话”的职责：

- 构造时绑定 `CharacterInstance`，生命周期内不再改变角色身份；
- 删除或废弃 `attach_session()`；
- 子组件从同一 runtime 获得 character/user/revision/state；
- 状态写入由 runtime/service 统一持久化，不由 section 或路由直接写 repository；
- `render_c1/render_c6/render_c9` 可以保留为领域投影，但输入必须是实例状态；
- `EmotionDetector.idle_expressions` 随 Revision 变更时在回合边界刷新。

### 7.3 回合解析流程

目标 `_prepare_turn()`：

```text
1. resolve conversationId
2. load Session + character_binding
3. load UserIdentityProfile snapshot
4. registry.get(characterInstanceId)
5. resolve active CharacterRevision
6. load RelationshipSnapshot
7. create scoped MemoryView
8. snapshot CharacterRuntimeState + stateVersion
9. build immutable TurnCharacterContext
10. register GenerationContext with character audit fields
```

此后：

```text
TurnCharacterContext
  → scoped recall
  → PromptAssembler
  → LLM/tool rounds
  → TTS using same character/voice context
  → assistant message archive with character audit fields
  → scoped memory consolidation
  → instance reflector/compression/mood update
  → CAS persist CharacterRuntimeState
```

后台任务结果回到某个会话时，也必须从该会话 binding 解析角色，不能使用当前前台活动会话的角色。

### 7.4 并发与快照语义

- 同一 instance 的状态变更通过 per-instance lock 串行；
- 不同 instance 可以并行执行反思/压缩，但仍服从 Agent 当前的整体生成并发限制；
- 角色编辑、默认角色修改不改变 in-flight TurnCharacterContext；
- role revision 更新从下一轮开始生效；
- mood 流式标签必须写入发起 generation 的 instance state，不能写“当前活动 runtime”；
- CAS 过滤条件为 `(instance_id, state_version)`；冲突时 reload + 有界重试，禁止最后写入静默覆盖；
- reset/删除等破坏性操作遇到该 instance 正在生成时返回 `CHARACTER_BUSY`，不强行中断当前回合。

### 7.5 角色版本升级与人格状态

编辑自定义角色时，创建新 Revision，并由请求明确给出状态策略：

| `statePolicy` | 行为 | 适用场景 |
|---------------|------|----------|
| `preserve` | 保留 L2/L3/L4/C6，再把 sourceRevision 更新到新版本 | 小幅措辞、外观、声音修改；默认 |
| `reset_evolution` | 清空 L2、L3（含 history/awareness/compression 派生态）、动态 C6、reflector，mood 回到新角色 idle；保留会话、记忆与关系 | 核心人格被重写 |

`reset_evolution` 是显式、可审计的破坏性操作，API 响应必须回显被重置的字段类别。首版不自动让 LLM“迁移人格状态”，避免不可验证改写。

旧 Revision 回滚使用“复制旧内容创建新 Revision”的 append-only 方式。历史 generation 永远仍指向原 Revision。

Revision 生效规则必须确定：

- Definition 的 `currentRevision` 指向新版本后，所有 `follow_latest` instance 在下一轮解析前升级到该版本并应用本次 `statePolicy`；
- `pinned` instance 保持原版本，不受 Definition 指针变化影响；
- legacy session 迁移出的 instance 默认 pinned，防止首次上线时顺带改变旧会话人格；
- 升级可以在保存事务内完成，也可以写入 `pending_revision/state_policy` 后由下一轮原子应用，但不能出现“Revision 已变、状态策略未执行且无人知道”的半完成状态；
- API 响应使用 §11.0 的 `CharacterMutationResult`，返回新角色详情、`affectedInstances` 与 `pendingInstances`。`affectedInstances` 是本次应升级的 `follow_latest` instance 总数，`pendingInstances` 是其中尚待下一轮原子应用的子集；`preserve` 时 `resetFields=[]`。

### 7.6 会话选角与切换规则

- `POST /api/sessions` 可传 `characterId`；未传时使用 UserIdentity 的 defaultCharacterId，再回退 bootstrap 内置角色；
- 服务层为 `(userId, characterId)` 取得或创建 canonical instance；
- 创建成功后将 binding 持久化到 session；
- 已有会话的 binding V1 不支持普通 PATCH 修改；
- 用户要和另一角色聊天时创建新会话；
- `activate_session` 只改变活动会话，不再调用 `CharacterEngine.attach_session()`；
- 角色 archived 后，既有会话仍能继续，不能再用它创建新会话；
- 角色 Definition/Revision 找不到时，回合失败并返回稳定错误，绝不能悄悄使用 Elysia。

### 7.7 角色状态的产品态与调试态

产品态 `CharacterPublicState` 建议只返回：

```json
{
  "characterId": "chr_builtin_elysia",
  "characterInstanceId": "chi_...",
  "characterRevision": 3,
  "stateVersion": 17,
  "mood": {"label": "平静", "intensity": 0.5, "updatedAt": "..."},
  "lastInteractionAt": "...",
  "proactiveEnabled": false,
  "updatedAt": "..."
}
```

规则：

- `currentActivity` 只有 C8 有真实、持久化来源时才返回；
- 不从 mood 或 L3 猜一个“正在想你”之类的文案；
- L2/L3、awareness 原文、reflector buffer 继续放在 debug API；
- 前端若只需要 Live2D 表情，继续消费 `expression`；丰富状态使用独立 typed snapshot/event。
- `characterRevision` 是角色内容版本，`stateVersion` 是演化状态 CAS/事件顺序；两者不得复用同一个 `revision` 字段；
- REST 返回完整 `CharacterPublicState`；WS 的 `state` 字段返回完整公开视图，不发送 JSON Merge Patch。

---

## 8. 长期记忆与关系数据隔离

### 8.1 MemoryScope

所有 LongTermMemory 公共写/读 API 必须接受不可省略的 scope：

```text
MemoryScope
  user_id
  character_instance_id
  visibility = character_private | user_shared
```

V1 自动提取、工具保存和关系共同事件一律写 `character_private`。`user_shared` 只预留枚举，不提供自动升级路径；未来必须由用户或明确规则授权。

### 8.2 ScopedLongTermMemory

推荐提供绑定视图，降低调用方漏传 scope 的概率：

```text
memory_view = long_term.for_scope(scope)
await memory_view.insert(text, ...)
await memory_view.search(query, ...)
await memory_view.update(entry_id, ...)
await memory_view.delete(entry_id)
```

底层 `LongTermMemory.search()` 不再提供无 scope 的 public overload。Debug 全库操作放在显式 admin repository API，不能借用产品 MemoryView。

### 8.3 Milvus v2 元数据

建议新建 `long_term_memory_v2`，每条至少包含：

| 字段 | 说明 |
|------|------|
| `entry_id` | 稳定 MemoryId |
| `user_id` | 用户作用域 |
| `character_instance_id` | 角色实例作用域 |
| `visibility` | `character_private/user_shared` |
| `conversation_id` | 来源会话，可空 |
| `source_message_ids` | 来源消息 ID 列表，可空 |
| `text/type/poignancy/timestamp` | 现有字段 |
| `schema_version` | 首版 2 |
| `embedding` | 现有向量 |

查询必须在 ANN 阶段使用 `user_id + character_instance_id + visibility` filter，而不是先全局召回再在 Python 过滤。内存 fallback 同样先按 scope 过滤。

选择新 collection 而非直接复用旧 collection 的原因：旧记录没有 scope；任何漏过滤都会恢复全局语义；新 collection 能以“查不到就是安全”作为缺省。

### 8.4 MemoryConsolidator

当前全局 consolidator 改为 runtime/instance scoped：

- buffer 属于 CharacterInstance，不属于当前活动会话；
- 每个对话对带 conversationId 和 message IDs；
- ExtractMemory 输入明确当前角色、用户和 scope；
- 提取结果写入同一个 MemoryView；
- compare 只查询同 scope 候选；
- shutdown 对所有 loaded runtime 逐一 flush。

### 8.5 MemoryReflector

当前 MemoryReflector 对全库聚类，目标改为：

- 每个 instance 独立计数与 last_summary；
- `get_all()` 只返回当前 scope；
- merge/update/delete 必须二次验证目标 entry scope 与 runtime 一致；
- LLM 即使幻觉出其他 scope 的 entry ID，repository 也返回 not found；
- 计数器进入 CharacterRuntimeState 或独立 instance state，不再随任意 session load 被覆盖。

### 8.6 记忆与 UserIdentity 的边界

- `displayName/pronouns/timezone` 等用户主动配置是 UserIdentity，不靠 LTM 召回；
- “用户喜欢草莓”当前仍是某角色的 private memory；
- 未来若用户确认将事实设为全角色共享，再复制/提升为 `user_shared`；
- 角色之间不能通过 MemoryReflector 自动推断共享；
- Neo4j 关系事实和向量记忆可互相引用稳定 ID，但不能让数据库层互相直接写入。

---

## 9. TTS、Live2D、唤醒与主动性

### 9.1 TTS 解析优先级

每次合成使用 TurnCharacterContext，声音按以下优先级解析：

```text
session.service_state.tts_voice 显式覆盖
  > CharacterRevision.runtimeDefaults.voice.voiceId
  > 当前 TTS Provider 的角色映射
  > Provider 默认声音
```

`tts_routes.py`、`provider_routes.py` 和流式 TTS 不再读取全局 `agent.character.name`。Preview 请求可以显式传 `characterId/instanceId/voiceId`，但后端仍验证声音属于当前 Provider。

若角色默认 voice 在 Provider 中不存在，状态接口返回安全 warning，并回退 Provider 默认；不能把角色名当 voice ID 发送。

### 9.2 Live2D 与角色资源

- Revision 只保存 `live2dModelId`，具体本地模型注册、路径和许可证由 Desktop/资产注册表拥有；
- 后端 `server_info` 或角色详情返回 ID/能力，不返回任意本机绝对路径；
- 会话切换后，前端根据 CharacterSummary/Presentation 切换模型；
- mood/expression 事件必须带 instance/conversation 关联，避免切会话后旧 generation 驱动新角色模型。

### 9.3 唤醒词

V1 只监听当前活动会话角色的 `wakeWords`，并可叠加应用级通用唤醒词。不能把所有角色唤醒词合并后又不知道该唤醒谁。

未来若支持“叫角色名字直接切换”，必须成为显式 Router：识别角色 → 验证是否可用 → 激活/创建对应会话 → 再开始输入；不能只换 IdleMonitor 内部 CharacterConfig。

### 9.4 主动性拆分

当前 C7 与 `proactive.enabled` 语义混在一起。目标拆成：

1. `conversationInitiative`：角色在已经发生的对话中如何主动接话，属于 Prompt；
2. `ProactiveSchedule`：静默超时/时刻触发，属于运行时配置；
3. `ProactiveDecision`：收到 tick 后是否真的说话以及说什么，未来独立、角色化实现。

V1 调度规则：只有活动会话的角色可触发主动消息，避免 Elysia 和 Cyrene 同时抢话。角色 proactive 默认值可被用户设置覆盖；配置 false 时不得因 C7 存在而后台主动发言。

### 9.5 OwnLife

C8 目前没有稳定生产和持久化链路。首批遵守：

- 无真实 `own_life` 状态时不渲染、不展示；
- 不把角色设定中的背景故事伪装成“此刻正在做什么”；
- 若后续实现 OwnLifeEngine，其状态归 CharacterInstance，更新必须有 timestamp/source；
- 角色切换只读取各自 instance 的 own_life；
- 前端状态显示必须区分 authored lore 与 runtime activity。

---

## 10. 存储设计

### 10.1 MongoDB 集合

#### `character_definitions`

```json
{
  "character_id": "chr_...",
  "slug": "my-character",
  "source": "custom",
  "mutable": true,
  "status": "active",
  "current_revision": 3,
  "owner_user_id": "usr_local_default",
  "created_at": "...",
  "updated_at": "..."
}
```

索引：

- `character_id` unique；
- `(owner_user_id, status, updated_at)`；
- `(owner_user_id, slug)` unique（仅 custom 范围，若后端不支持 partial index 则服务层守卫）。

#### `character_revisions`

保存完整 Revision 正文。

索引：

- `(character_id, revision)` unique；
- `(character_id, created_at)`；
- `checksum` 非唯一索引可选。

Revision append-only，不做 update；回滚也 insert 新版本。

Definition + Revision 的写入顺序必须保证 current 指针永远指向 durable Revision：

1. 优先使用 Mongo transaction 完成 `insert revision + CAS definition.current_revision`；
2. 若当前 DocumentDB 抽象暂不支持 transaction，先以确定性键 insert Revision，再 CAS 更新 Definition；
3. CAS 失败的 Revision 不可被 catalog 视为 current，标记/识别为 orphan 并由 reconciliation 清理；
4. 绝不能先更新 Definition 指针，再尝试写 Revision；
5. create 用例同理：Revision durable 后 Definition 才对列表可见。

#### `character_instances`

```json
{
  "instance_id": "chi_...",
  "user_id": "usr_local_default",
  "character_id": "chr_...",
  "active_revision": 3,
  "revision_policy": "follow_latest",
  "state": {},
  "relationship": {},
  "state_version": 17,
  "last_interaction_at": "...",
  "created_at": "...",
  "updated_at": "..."
}
```

索引：

- `instance_id` unique；
- `(user_id, character_id)`；
- canonical instance 使用 `(user_id, character_id, canonical)` unique；
- `(character_id, updated_at)`。

状态更新使用：

```text
filter: instance_id + state_version(expected)
update: $set state/... + $inc state_version
```

#### `character_state_versions`

替代以 `session_id` 为键的旧 `character_versions`：

- `instance_id`；
- `content_type: self_l2 | speaking_style_c6`；
- `version`；
- `trigger`；
- `source_revision`；
- `content/changes`；
- `created_at`。

索引 `(instance_id, content_type, version)` unique。

#### `user_profiles`

保存 §5.1 字段。索引 `user_id` unique。

#### `sessions`

新增 `character_binding`。迁移期保留 legacy `character_state`，稳定切换后不再作为权威写入。

### 10.2 内置角色文件源

`BuiltinCharacterSource` 在启动时扫描受控 manifest：

- ID 必须显式、全局唯一；
- 所有相对路径限制在该角色目录内，resolve 后越界即拒绝；
- 文件缺失、UTF-8 失败、schema 错误、checksum 错误均 fail-fast；
- 同一 characterId 不允许被 Mongo custom 覆盖；
- built-in Definition/Revision 以文件内容计算确定性 revision/checksum；
- release 中角色内容变更应递增 manifest revision。

### 10.3 本地缓存与 Mongo 离线语义

角色身份不能在 Mongo 故障时静默换人。建议：

- built-in 角色始终可从文件加载；
- custom Revision 成功读取/写入后保存 last-known-good 只读快照缓存；
- Mongo 离线时，已缓存 custom 可继续只读运行；没有缓存则返回 `CHARACTER_STORAGE_UNAVAILABLE`；
- custom 创建/编辑/归档在 Mongo 离线时 fail-closed 503；
- CharacterRuntimeState 采用类似 session pending JSON 的 per-instance 原子 journal；Mongo 恢复后按 stateVersion 合并/补写；
- 不允许以 Elysia、空 Prompt 或旧启动角色替代不可用的 custom；
- 缓存与 journal 不保存 API key，但角色设定本身仍按用户数据保护。

### 10.4 数据删除与归档

`DELETE /api/characters/{id}` 第一版语义是 archive：

- built-in 返回 `CHARACTER_IMMUTABLE`；
- custom status 改 archived；
- 禁止新会话使用；
- 既有 session/revision/instance/记忆保留；
- 不物理删除历史，避免聊天记录失去身份；
- 真正永久删除必须是单独的、可预演的未来用例，包含 session/instance/memory/asset 级联清单。

### 10.5 数据库文档同步要求

实现后必须同步更新 `docs/database-storage.md`：

- Mongo 集合总数；
- 新集合字段和索引；
- `sessions.character_binding`；
- `long_term_memory_v2` scope 字段；
- legacy collection 的只读/迁移状态；
- 本地角色 cache/journal 路径和恢复语义。

---

## 11. REST 契约设计

所有 wire 类型先定义在 `frontend_contract/rest.py`，再生成 schema/TypeScript；路由不得手写漂移 dict。

### 11.0 `character-contract-v1` 开工冻结契约

本节是前后端开工时的精确 wire 口径。§11 其余小节解释业务语义；若示意文字与本节冲突，以本节和 `frontend_contract` 生成产物为准。实现完成后，Pydantic 模型、JSON Schema、生成 TS、golden frame 和前端 Main raw schema 必须同形。

命名与包装规则：

- 新角色 REST DTO 的业务字段使用 `camelCase`；分页沿用现有后端 wire 约定 `has_more + cursor`；
- Main 将 raw `{items, has_more, cursor}` 映射为 Renderer 领域 `{items, hasMore, nextCursor}`，Renderer 不得直接依赖 snake_case；
- 成功响应直接返回下表所列 typed object，不额外套 `{ok,data}`；
- optional 字段未知时省略。角色 Revision 内可清空的受管引用继续使用显式 `null`；Main 统一映射为领域 `undefined`；
- 时间为 UTC ISO-8601 字符串；cursor 为不透明字符串，客户端不得解析；
- `UserIdentityProfile.revision`、`CharacterRevision.revision/currentRevision` 与 `CharacterPublicState.stateVersion` 都从 1 开始并为正整数；计数器和 `pendingInstances` 可为 0；
- 所有错误复用 `ProtocolError`：`{code,message,retryable,requestId,error,details,retryAfterMs?}`，其中 `error === message` 仅为旧客户端兼容，`details` 只允许公开安全字段；
- `CharacterStatePolicy` 唯一取值为 `preserve | reset_evolution`，前后端不得再使用缩写 `reset`。

冻结的共享响应类型：

```typescript
interface CharacterListResponse {
  items: CharacterSummary[]
  has_more: boolean
  cursor?: string
}

interface CharacterRevisionListResponse {
  items: CharacterRevisionSummary[]
  has_more: boolean
  cursor?: string
}

interface CharacterBindingSnapshot {
  characterId: string
  characterInstanceId: string
  characterRevision: number
  displayName: string
  presentation: CharacterPresentation
}

type ResetField =
  | 'self_l2'
  | 'state_l3'
  | 'speaking_style_c6'
  | 'reflector'
  | 'mood'

interface CharacterStateImpact {
  statePolicy: 'preserve' | 'reset_evolution'
  affectedInstances: number
  pendingInstances: number
  resetFields: ResetField[]
}

interface CharacterMutationResult {
  character: CharacterDetail
  stateImpact: CharacterStateImpact
}

interface CharacterArchiveResult {
  character: CharacterSummary
  userProfile?: UserIdentityProfile
}

interface CharacterPublicStateView {
  mood?: { label: string; intensity: number; updatedAt: string }
  currentActivity?: string
  lastInteractionAt?: string
  proactiveEnabled: boolean
  updatedAt: string
}

interface CharacterPublicState extends CharacterPublicStateView {
  characterId: string
  characterInstanceId: string
  characterRevision: number
  stateVersion: number
}

interface ResetCharacterEvolutionResult {
  state: CharacterPublicState
  resetFields: ResetField[]
}
```

冻结的请求/响应矩阵：

| 方法与路径 | 请求 | 成功响应 |
|---|---|---|
| `GET /api/me` | 无 | `200 UserIdentityProfile` |
| `PUT /api/me` | 完整最小身份字段 + `expectedRevision` | `200 UserIdentityProfile` |
| `GET /api/characters` | query：`source/status/search/cursor/limit`；默认 `status=active` | `200 CharacterListResponse` |
| `POST /api/characters` | 顶层完整 `CharacterDraft` | `201 CharacterDetail` |
| `GET /api/characters/{id}` | 无 | `200 CharacterDetail{summary,current}` |
| `PUT /api/characters/{id}` | 顶层 `expectedRevision/statePolicy` + 完整 `CharacterDraft` 字段 | `200 CharacterMutationResult` |
| `DELETE /api/characters/{id}` | 无 | `200 CharacterArchiveResult` |
| `POST /api/characters/{id}/clone` | `{displayName?: string}` | `201 CharacterDetail` |
| `GET /api/characters/{id}/revisions` | query：`cursor/limit` | `200 CharacterRevisionListResponse` |
| `POST /api/characters/{id}/rollback` | `{targetRevision,expectedRevision,statePolicy}` | `200 CharacterMutationResult` |
| `POST /api/characters/validate` | 顶层完整 `CharacterDraft` | `200 CharacterValidationResult`，无效草稿也是 200 + `valid=false` |
| `POST /api/characters/preview` | `{draft: CharacterDraft,userMessage: string}` | `200 CharacterPreviewResult` |
| `GET /api/character-instances/{id}/state` | 无 | `200 CharacterPublicState` |
| `POST /api/character-instances/{id}/reset-evolution` | `{expectedStateVersion: number}` | `200 ResetCharacterEvolutionResult` |

`PUT` 的前端领域命令可以使用 `{characterId, expectedRevision, statePolicy, draft}`，但 Main mapper 必须把 `draft` 展平为上述 wire；后端不接收额外的 `draft` 包装层。`preview` 因同时含用户输入，明确保留 `draft` 包装层。

校验与预览响应冻结为：

```typescript
interface CharacterValidationIssue {
  severity: 'error' | 'warning'
  code: string
  path: string       // JSON Pointer，例如 /promptProfile/corePersonality
  message: string
}

interface CharacterValidationResult {
  valid: boolean
  issues: CharacterValidationIssue[]
  estimatedPromptChars?: number
}

interface CharacterPreviewResult {
  ephemeral: true
  reply: string
  model?: string
  usage?: { input?: number; output?: number; total?: number }
}
```

保存接口收到无效内容时返回 400 `CHARACTER_VALIDATION`，`details.issues` 与 validate 的 issue 形状相同。Preview 由客户端 HTTP abort 取消；服务端在断连后 best-effort 取消上游，且无论取消是否及时都不得产生任何持久化副作用。

归档默认角色时，后端在同一业务事务中把 `defaultCharacterId` 改为 bootstrap 内置角色并递增 profile revision，同时在 `CharacterArchiveResult.userProfile` 返回新档案；未改变默认角色时省略 `userProfile`。这避免前端持有指向 archived 角色的默认值。

`reset_evolution` 的 V1 精确范围为：清空 L2 self；清空 L3 当前态、history、awareness/compression 派生态；清空动态 C6；清空 reflector buffer/context/history；mood 回到当前 Revision 的 idle。它原子递增一次 `stateVersion` 并返回新 public state。它不删除/重写 session、message archive、LTM、relationship、UserIdentity、角色 Revision、会话 service/voice 设置或受管资产；instance 正在生成/反思且无法安全取得锁时返回 409 `CHARACTER_BUSY`，不得强行中断。

#### 11.0.1 跨仓机器契约交付

冻结文档不是机器契约替代品。后端契约子批完成时必须先交付：

1. `frontend_contract` Pydantic 源；
2. 重新生成的 `contract.schema.json` 与 `contract.raw.ts`；
3. 扩展后的 `scripts/gen_contract_frames.py` golden：至少有 `rest-character.jsonl` 与 `ws-v2-character.jsonl`，覆盖 §11～§12 的成功、冲突和 identity 帧；
4. 一份 handoff manifest，记录 `character-contract-v1`、backend commit、transport/protocol version、schemaHash、生成命令和 `verified` 状态。

先运行 `make gen-types && make check-types`，再把以上产物交给 Electron。`contract.raw.ts` 是前端镜像/审查依据，不直接代替 Electron Main 的 strict Zod runtime schema。前端 fixture manifest 必须记录同一 backend commit/schemaHash；真实联调前保持 `verified=false`，不能把生成样例伪装成实抓 trace。

### 11.1 用户身份

| 方法 | 路径 | 作用 |
|------|------|------|
| GET | `/api/me` | 取得 UserIdentityProfile |
| PUT | `/api/me` | 完整更新最小身份，带 expectedRevision |

`PUT /api/me` 成功后只影响下一轮。displayName 改变不会重写历史消息或长期记忆文本。

bootstrap profile 的 revision 为 1；每次成功 PUT，以及归档默认角色导致的原子 fallback，revision 恰好递增 1。冲突返回 `USER_PROFILE_REVISION_CONFLICT` 与 `details.currentRevision`。

`PUT /api/me` 是完整最小身份替换，不是 PATCH：请求不得带 `userId/revision/initialized/createdAt/updatedAt`；首次成功 PUT 由后端把 `initialized` 设为 true，之后不能通过此接口改回 false。省略 `preferredAddress/pronouns` 表示清空；省略 `defaultCharacterId` 表示使用 bootstrap 内置角色。前端必须以 GET 结果为基线提交完整表单，不能把“字段未加载”误当作清空。

### 11.2 角色列表与详情

| 方法 | 路径 | 作用 |
|------|------|------|
| GET | `/api/characters` | 内置 + 当前用户 custom 的分页列表 |
| POST | `/api/characters` | 创建 custom Definition + Revision 1 |
| GET | `/api/characters/{characterId}` | 当前 Revision 完整详情 |
| PUT | `/api/characters/{characterId}` | 以完整 draft 创建新 Revision |
| DELETE | `/api/characters/{characterId}` | archive custom |
| POST | `/api/characters/{characterId}/clone` | 从内置/custom 当前版本克隆新 custom |
| GET | `/api/characters/{characterId}/revisions` | 版本列表（不默认返回全部正文） |
| POST | `/api/characters/{characterId}/rollback` | 复制指定旧版本为一个新版本 |

列表 query：`source`、`status`、`search`、`cursor`、`limit`。默认不返回 archived。

### 11.3 校验与预览

| 方法 | 路径 | 作用 |
|------|------|------|
| POST | `/api/characters/validate` | 保存前纯校验，返回 errors/warnings/size estimate |
| POST | `/api/characters/preview` | 对未保存 draft 做一次无状态示例回复 |

Preview 强约束：

- 不绑定真实会话；
- 不读取/写入 LTM；
- 不更新 L2/L3/L4、Reflector、token session 累计或 own_life；
- 不暴露工具；
- 不触发 TTS/主动任务；
- 独立限流和超时；
- 响应声明 `ephemeral=true`。

### 11.4 角色实例与产品态状态

| 方法 | 路径 | 作用 |
|------|------|------|
| GET | `/api/character-instances/{instanceId}/state` | 产品态 CharacterPublicState |
| POST | `/api/character-instances/{instanceId}/reset-evolution` | 按 §11.0 显式重置演化状态并返回 resetFields + 新公开状态 |

普通前端不需要直接创建 instance；`POST /api/sessions` 负责取得 canonical instance。未来需要多时间线时再开放 instance create/clone。

### 11.5 会话契约扩展

`POST /api/sessions` 正式请求类型：

```json
{
  "title": "和 Cyrene 聊天",
  "characterId": "chr_builtin_cyrene"
}
```

协议层允许 `characterId` 省略，以便非交互客户端使用 `UserIdentityProfile.defaultCharacterId`；Electron 在 `character_profiles=true` 时必须经过默认值/选择器解析并显式发送。无论请求是否省略，成功响应都必须返回 canonical `character` binding。

为保持现有会话 API 兼容，创建成功继续返回 `200 SessionSummary`，不改为 201；`GET /api/sessions` 的每个非 legacy summary 和 `GET /api/sessions/{id}` 内的 summary 使用同一 binding 形状。

`SessionSummary` 增加：

```json
{
  "character": {
    "characterId": "chr_builtin_cyrene",
    "characterInstanceId": "chi_...",
    "displayName": "Cyrene",
    "characterRevision": 1,
    "presentation": {}
  }
}
```

迁移期该字段可 optional；`character_profiles` feature 为 true 后，新创建/已迁移会话必须返回。该形状也作为 assistant `SessionMessageItem.character` 的审计快照；历史消息使用实际生成时的 revision，不随 Definition 当前版本变化。

### 11.6 角色创建/更新请求示意

```json
{
  "expectedRevision": 2,
  "statePolicy": "preserve",
  "displayName": "Cyrene",
  "description": "……",
  "aliases": ["昔涟"],
  "promptProfile": {
    "corePersonality": "……",
    "relationshipStyle": "……",
    "emotionStyle": "……",
    "speakingStyle": "……",
    "conversationInitiative": "……",
    "examples": [
      {"user": "……", "assistant": "……"}
    ]
  },
  "runtimeDefaults": {
    "voice": {"providerId": null, "voiceId": null},
    "emotion": {"idleExpressions": ["neutral"]},
    "wakeWords": ["Cyrene", "昔涟"],
    "proactive": {"enabled": false, "intervalSeconds": 300, "triggers": []}
  },
  "presentation": {
    "avatarAssetId": null,
    "live2dModelId": null,
    "themeColor": null
  }
}
```

POST 不传 `expectedRevision`；PUT 必传并与 currentRevision 一致，否则 409。

### 11.7 REST 状态码

| 场景 | HTTP |
|------|------|
| 角色创建/克隆成功 | 201 |
| 会话创建成功（保持现有兼容） | 200 |
| 查询/更新/归档/回滚成功 | 200 |
| 请求结构或角色内容不合法 | 400 |
| 未认证 | 401 |
| 无权访问其他 owner 数据 | 403 或按项目防枚举策略返回 404 |
| 角色/实例不存在 | 404 |
| revision/stateVersion 冲突、busy、绑定不可改 | 409 |
| 角色/Prompt 体积过大 | 413 |
| 限流 | 429 |
| Mongo 不可用且无安全缓存 | 503 |

---

## 12. WebSocket 与生成契约

### 12.1 Feature flag

`ServerInfoFeatures` 的兼容默认值仍为：

```text
character_profiles: bool = false
character_state_stream: bool = false
```

默认 `false` 是 schema/旧实现的兼容语义，不代表当前真实 Agent 的运行值。REST、session binding、runtime isolation、memory scope、状态事件和生成契约门禁现已完成，`build_server_info()` 对真实 Agent 明确发送 `character_profiles=true`、`character_state_stream=true`；未提供能力属性的轻量 facade/test double 仍按 fail-closed 返回 `false`。

Electron 对 key 缺失或非布尔值严格映射为 `false`。当 `character_profiles=true` 时，服务端必须携带本文规定的完整角色身份，客户端也必须执行身份一致性门禁；能力关闭时才允许 legacy 无身份帧走兼容路径。

### 12.2 Session replay

`SessionMessageItem` 增加 optional `character: CharacterBindingSnapshot`。assistant item 必须保存并回放实际 generation 的角色快照；user/tool item 可以省略。连接或 `activate_session` 回放后，前端无需从名字猜当前角色。

### 12.3 Generation identity

以下消息增加 optional、迁移后必填的角色审计字段：

- `generation.started`；
- `generation.completed`；
- `generation.failed`；
- 必要时 assistant archive/message item。

字段：

```json
{
  "characterId": "chr_...",
  "characterInstanceId": "chi_...",
  "characterRevision": 3
}
```

前端据此拒绝把迟到的旧角色 delta/expression 应用到新活动角色视图。

契约模型在放量期保持 optional；当 `character_profiles=true` 时，`generation.started/completed/failed` 三种终端路径都必须带齐并互相一致。segment/delta 无需重复字段，客户端通过 `generationId` 表解析身份。缺失或不一致属于服务端契约错误，不得回退到“当前活动角色”。

### 12.4 Character state event

新增服务端事件：

```json
{
  "type": "character.state.changed",
  "conversationId": "conv_...",
  "characterId": "chr_...",
  "characterInstanceId": "chi_...",
  "characterRevision": 3,
  "stateVersion": 17,
  "state": {
    "mood": {"label": "开心", "intensity": 0.72, "updatedAt": "..."},
    "lastInteractionAt": "...",
    "proactiveEnabled": false,
    "updatedAt": "..."
  }
}
```

事件只包含产品态字段。`state` 是完整 `CharacterPublicStateView`，不是 patch；未知 optional 字段省略。WS 新连接/会话激活时先发 snapshot，之后仅在实际状态变化时发新的完整公开视图，避免每秒轮询。客户端只按同一 instance 内严格递增的 `stateVersion` 应用事件，不能用时间字符串替代顺序判断。

### 12.5 Expression 与 Live2D directive

现有 `expression` 和 `live2d.directive` 保留，新增 optional：

- `conversationId`；
- `generationId`（存在 generation 时）；
- `characterId`；
- `characterInstanceId`；
- `characterRevision`。

兼容期契约字段 optional，旧客户端忽略新字段。`character_profiles=true` 后，服务端必须提供 `conversationId + characterId + characterInstanceId + characterRevision`，新客户端先检查身份再驱动 Live2D；若只有 `generationId`，Main 可从 generation identity 补齐，但两类身份都缺失时必须丢弃，不能作用于当前角色。

### 12.6 角色配置变更事件

自定义角色保存/归档通常由发起 REST 请求的前端直接更新缓存；若需要多窗口同步，可增加：

```text
character.catalog.changed
  characterId
  currentRevision
  status
  updatedAt
```

该事件不携带完整 authored Prompt，客户端收到后重新 GET 详情。

### 12.7 TTS 音频身份

现有 `audio_start` 保留 `stream_id/format/sampleRate/durationMs`，增加：

```json
{
  "type": "audio_start",
  "stream_id": 42,
  "format": "wav",
  "conversationId": "conv_...",
  "generationId": "gen_...",
  "characterId": "chr_...",
  "characterInstanceId": "chi_...",
  "characterRevision": 3
}
```

`generationId` 对非 generation 音频可省略，其余四个身份字段在 `character_profiles=true` 后必填。chunk/end/cancel 继续用 `stream_id` 关联，不重复角色字段；Main 必须从 `audio_start` 建立 stream identity，并让组装后的 ready/cancel 领域事件保留全部身份。没有通过会话与 instance 守卫的迟到音频不得自动播放，也不得驱动口型。REST TTS preview 不属于聊天 generation，不写角色状态。

### 12.8 严格 schema 放量与事件顺序

放量顺序固定为：

1. 前端先发布能接收所有新增 optional 字段/事件的 raw schema 和 mapper，但 UI 能力仍关闭；
2. 后端 contract/golden 产物加入新增字段，feature 仍为 false；运行时仍省略角色 additive 字段和新事件；
3. 双端对拍 Session、generation、state、expression/directive、audio 帧；
4. Prompt/runtime/memory 隔离门禁通过后才置 `character_profiles=true`；
5. state snapshot/重连路径通过后才置 `character_state_stream=true`。

后端不得向仍使用 strict 旧 schema 的客户端提前发送未知事件。V1 通过“feature 默认关闭且关闭时不序列化新字段”实现部署隔离；若同一后端将来必须同时服务旧、新客户端，需另立 schema/transport capability 协商设计，在此之前不得开启 feature，也不能假设旧客户端会忽略字段。

---

## 13. 校验、权限与安全

### 13.1 字段限制建议

| 字段 | 首版限制 |
|------|----------|
| displayName | 1–64 个 Unicode 字符，去首尾空白 |
| description | ≤ 500 字符 |
| aliases | ≤ 10 项，每项 1–32 字符，大小写/Unicode 规范化后去重 |
| corePersonality | 1–12,000 字符 |
| relationshipStyle | 1–6,000 字符 |
| emotionStyle | ≤ 5,000 字符 |
| speakingStyle | 1–6,000 字符 |
| conversationInitiative | ≤ 3,000 字符 |
| examples | ≤ 20 组，每个 user/assistant ≤ 1,000 字符 |
| openingMessage | ≤ 1,000 字符 |
| wakeWords | ≤ 20 项，每项 1–32 字符 |
| 整个 Revision 规范化 JSON | 建议 ≤ 64 KiB |

限制应由 Pydantic contract/domain validation 双层保证；Repository 不接受绕过 Service 的未校验对象。

### 13.2 保留内容与 Prompt 注入

角色创建者本来就被允许定义人格，因此不能把所有“忽略之前指令”文本简单视为攻击；真正边界是不能越过 Framework。建议 validator：

- 对保留 XML 标签、Jinja delimiters、NUL/control characters 记 hard error；
- 对“修改工具权限”“不要输出 mood 标签”“泄露 system prompt”等语义记 warning，并由 F1 明确降级其优先级；
- authored 内容永远不插入 system prompt 的 Framework 标签内部；
- 模板渲染使用普通变量值，不二次 render；
- tool definitions 和权限完全由 ToolAdapter/运行时生成，角色文本不能声明一个工具就获得工具；
- Preview 关闭工具，防止角色草稿诱导真实副作用。

### 13.3 所有权与认证

- 所有 `/api/me`、`/api/characters*`、instance state 路由进入现有认证/限流中间件；
- custom 查询始终加入 owner_user_id；
- characterId 不可预测也不能代替授权；
- clone 内置角色允许，clone 他人 custom 在未来多用户模式下默认禁止；
- 日志只记录 IDs、revision、长度、checksum 和错误码，不记录角色全文；
- Debug PromptRecorder 仍可能包含全文，必须维持 debug 认证和部署隔离。

### 13.4 资产安全

- avatar/live2d 只接受受管 asset/model ID；
- 不接受 `file://`、UNC、本地绝对路径或任意远程 URL 作为角色详情字段；
- 若以后支持导入压缩包，需独立实现大小限制、路径穿越检查、文件类型白名单、隔离目录和许可证元数据；
- 角色导出默认不包含长期记忆、UserIdentity、API key 或本地绝对路径。

### 13.5 Preview 安全与成本

- Preview 单独 rate limit，例如每连接每分钟 5 次；
- 限制输入长度和输出 token；
- 关闭 tools、memory、TTS、state mutation；
- 使用当前已配置 LLM，但错误只返回安全归类，不透传上游 body；
- 响应不得被追加到真实 session；
- 记录用量时标记 `purpose=character_preview`，不归入会话 token 统计。

---

## 14. 稳定错误码

在现有错误目录中增加：

角色 REST 失败统一返回现有 `ProtocolError` wire，禁止路由自行返回字符串或任意 dict：

```json
{
  "code": "CHARACTER_REVISION_CONFLICT",
  "message": "角色已被其他操作更新，请刷新后重试",
  "retryable": true,
  "requestId": "req_...",
  "error": "角色已被其他操作更新，请刷新后重试",
  "details": {"currentRevision": 4},
  "retryAfterMs": 1000
}
```

`retryAfterMs` 仅在后端能给出可靠退避值时出现；409 并不自动等于可安全重放，前端仍须根据 `code` 处理。校验错误可以在 `details.issues` 携带 §11.0 的公开 issue；任何详情都不得包含 Prompt 正文、用户自由文本、路径、traceback 或上游响应体。

| 错误码 | retryable | 典型 HTTP | 含义 |
|--------|-----------|-----------|------|
| `CHARACTER_NOT_FOUND` | false | 404 | 角色不存在或不可见 |
| `CHARACTER_INSTANCE_NOT_FOUND` | false | 404 | 角色实例不存在 |
| `CHARACTER_IMMUTABLE` | false | 409 | 尝试编辑/归档内置角色 |
| `CHARACTER_ARCHIVED` | false | 409 | archived 角色不可新建会话 |
| `CHARACTER_VALIDATION` | false | 400 | 角色内容校验失败 |
| `CHARACTER_RESERVED_CONTENT` | false | 400 | 包含保留模板结构/标签 |
| `CHARACTER_REVISION_CONFLICT` | true | 409 | expectedRevision 已过期 |
| `CHARACTER_STATE_CONFLICT` | true | 409 | stateVersion CAS 冲突超重试 |
| `CHARACTER_BUSY` | true | 409 | instance 正在生成/反思，当前操作不可安全执行 |
| `CHARACTER_BINDING_IMMUTABLE` | false | 409 | V1 尝试在已有会话换角色 |
| `CHARACTER_STORAGE_UNAVAILABLE` | true | 503 | custom 定义/状态不可安全加载 |
| `CHARACTER_PREVIEW_FAILED` | 视原因 | 502/503 | Preview 上游或运行时失败 |
| `USER_PROFILE_REVISION_CONFLICT` | true | 409 | 用户身份 expectedRevision 过期 |
| `MEMORY_SCOPE_VIOLATION` | false | 403/内部告警 | 访问不属于当前 instance 的记忆 |

公开 message 保持安全简洁；具体上游异常、文件路径和 Prompt 片段只进脱敏 debug 日志。

---

## 15. 兼容与数据迁移

### 15.1 配置迁移

现有 `config.agent.character` 变为：

- 首次 bootstrap 的内置角色 manifest 路径；
- 数据库尚未建立 UserIdentity/defaultCharacter 时的 fallback；
- 不再是运行期唯一角色的事实源。

`config.agent.user_name` 只用于创建 legacy UserIdentity；完成初始化后，Mongo profile 为权威。

### 15.2 Elysia 内置角色迁移

1. 为 Elysia 增加稳定 ID 与 manifest schemaVersion；
2. 从 C2/C3/C6/C7 抽取 Elysia authored 内容到角色目录；
3. 将 Framework 模板改为中性 wrapper；
4. 泛化活跃辅助 Prompt；
5. 对迁移前后建立行为 golden/case 集；
6. 只有在 Elysia 关键行为不回退、自定义反例不泄漏时继续后续阶段。

### 15.3 旧会话 CharacterState

现有每个 session 拥有独立 CharacterState，无法无损自动合并成一个 canonical Elysia instance。安全迁移策略：

1. 为每个旧 session 建立确定性的 legacy instance，例如由 sessionId 哈希生成；
2. 将该 session.character_state 原样复制到对应 instance.state；
3. session 增加 binding 指向该 legacy instance；
4. 旧会话行为和状态保持不变；
5. 新建 Elysia 会话使用 canonical Elysia instance；
6. 未来可提供人工“合并时间线”工具，但本批不自动合并 L2/L3。

这样会暂时存在多个 Elysia instance，但不会丢失或错误合并用户已有的人格演化。

### 15.4 Shadow read/write 切换

建议分四步迁移状态：

```text
M1: sessions 增加 binding，旧 character_state 仍权威
M2: 幂等 backfill character_instances，读取对比但不启用
M3: instance state 变权威；一段兼容期 dual-write legacy character_state
M4: 停止写 legacy；验证后保留只读迁移字段，下一大版本再清理
```

每步有独立迁移标记/统计，不用“启动时 try 一下失败就忽略”。迁移脚本必须可重复执行。

### 15.5 旧 character_versions

- 按旧 `session_id` 找到迁移后的 legacy instanceId；
- 复制到 `character_state_versions`；
- 保留原版本号、trigger、created_at；
- 加 `migrated_from_session_id` 审计字段；
- 新仓库稳定后，旧集合只读，不立即 drop。

### 15.6 旧长期记忆

旧 `long_term_memory` 没有 scope，不能自动判断属于哪个旧会话或角色。禁止把它直接暴露给所有新角色。

推荐策略：

1. 定义 `legacy_unscoped` 隔离区；
2. 默认只允许 legacy bootstrap Elysia 的迁移工具读取，不进入普通 C4 搜索；
3. 提供 dry-run 报告：条目数、类型、时间范围、可识别 source metadata 数；
4. 用户/运维选择目标 legacy Elysia instance 后，将文本重新嵌入写入 `long_term_memory_v2`；
5. 每条写 migration marker，确保幂等；
6. 完成验证前不删除 v1 collection；
7. 不自动复制给 Cyrene 或其他角色。

### 15.7 JSON pending 与离线数据

- 旧 session pending 文件按 session → legacy instance 规则迁移；
- 新 CharacterRuntimeState journal 文件名只使用验证过的 instanceId，不拼用户输入路径；
- journal 原子替换，成功同步 Mongo 后删除；
- Mongo 与 journal 同时存在时以 stateVersion/updatedAt 和明确合并规则处理，不能简单最后写赢；
- 冲突保留 quarantine 副本并报警，不静默丢弃。

### 15.8 迁移回滚

在 M4 前，关闭 feature flag 可恢复 legacy 读取路径。回滚不删除新集合，不把 v2 state 反向覆盖旧 session；需要导回时使用显式工具。数据库迁移和代码发布必须记录对应 backend commit/schemaVersion。

---

## 16. 建议目录与模块边界

```text
core/
  character/
    README.md
    models.py                  # Definition/Revision/Instance/RuntimeState
    validation.py              # authored 文本/字段校验
    prompt_profile.py          # PromptProfile + compiler input
    catalog.py                 # builtin + custom 合并读取
    service.py                 # CRUD/clone/archive/revision use cases
    runtime.py                 # CharacterRuntime
    runtime_registry.py        # instance cache/lifecycle
    relationship.py            # RelationshipContextProvider protocol + V1 impl
    migration.py               # 领域迁移编排（或 scripts/ 独立入口）
  profile/
    README.md
    models.py
    service.py
  prompt/
    prompt_assembler.py        # 改为 TurnPromptContext 输入
    character_compiler.py      # 编译角色 section 数据
  storage/
    character_repo.py
    character_instance_repo.py
    character_state_version_repo.py
    user_profile_repo.py
    memory_repo_v2.py
  input/
    character_routes.py
    profile_routes.py

frontend_contract/
  rest.py                      # 角色、用户、会话扩展 DTO
  ws_message.py                # feature/event/identity 字段

character/
  elysia/
  cyrene/

Module/system_prompt/
  sections/                    # 通用 section renderer
  templates/                   # framework + 通用 wrapper，不含具体角色
```

不建议：

- 把 CharacterService 塞回 `agent.py`；
- 在 aiohttp route 里直接读 YAML/Mongo；
- 让 `Module/system_prompt` import Mongo repository；
- 在 `SessionManager` 内创建/编辑 CharacterDefinition；
- 为每个角色复制一整套 Jinja 模板；
- 用名字或目录名替代稳定 ID；
- 继续用全局 `agent.character` 作为运行态角色查询入口。

---

## 17. 分阶段实施计划

### 阶段 A：基线与泄漏门禁

目标：在改 Prompt 前先固定当前行为和问题。

- 建立完整 system prompt snapshot（只进测试 fixture，不进产品响应）；
- 建立 Elysia 场景集：日常、严肃、悲伤、拒绝、调情、工具、记忆；
- 建立反例角色 `ReservedColdCharacter`：明确冷静、正式、不调情、不用 `♪`；
- 写失败测试证明当前 C2/C3/C6/C7/辅助 Prompt 会泄漏；
- 盘点活跃与 dormant prompt，避免漏改隐藏链路。

退出条件：测试能稳定捕获 Elysia 词汇/行为泄漏。

### 阶段 B：Prompt 内容所有权重构

目标：单角色运行方式不变，但模板已经真正通用。

- Elysia authored block 外移；
- F2/F3 中性化示例；
- C2/C3/C6/C7 改为通用 wrapper；
- CharacterReflector、SpeakingStyleEdit、ExtractMemory、Compare、MemoryReflector 泛化；
- 引入 PromptProfile/Compiler/validator；
- Elysia 继续从文件启动；
- dormant prompt 明确标记且不得被生产 import。

退出条件：Elysia 行为基线通过；反例角色最终 Prompt 不含 Elysia 专属内容。

### 阶段 C：领域模型、内置 Catalog 与 UserIdentity

- 新建 `core/character`、`core/profile`；
- 内置 manifest v2 与 BuiltinCharacterSource；
- UserIdentity bootstrap/CRUD；
- Definition/Revision/Instance Pydantic 模型；
- CharacterCatalog 先只读内置角色；
- frontend_contract 增加 DTO，但 feature flag 仍 false。

退出条件：无 Mongo 时可列出并校验 Elysia/Cyrene 内置角色；用户身份有稳定 ID。

### 阶段 D：Custom Character 持久化与版本

- Mongo repositories/indexes；
- create/get/list/update/clone/archive/revisions/rollback；
- expectedRevision/CAS；
- last-known-good 缓存与离线写 fail-closed；
- validate/preview（preview 可放本阶段末）；
- 审计与错误码。

退出条件：CRUD/版本/并发/离线语义通过，但还不允许 custom 参与真实聊天。

### 阶段 E：CharacterRuntime 与会话绑定

- RuntimeRegistry；
- CharacterEngine instance-scoped，去除 attach_session；
- TurnCharacterContext；
- Session create + binding + summary；
- generation/message 角色审计字段；
- TTS、mood、background result 使用同一 context；
- 旧 session state shadow migration。

退出条件：两个角色可在不同会话生成且 Prompt/状态/声音不串；memory feature 暂时可以对新多角色关闭。

### 阶段 F：记忆 v2 隔离

- MemoryScope/MemoryView；
- `long_term_memory_v2`；
- scoped Consolidator/Reflector/C4；
- legacy unscoped dry-run/migration；
- 跨角色读写/更新/删除负面测试。

退出条件：任何产品路径都不能无 scope 访问 LTM；隔离测试全绿。

### 阶段 G：产品状态与 WS

- CharacterPublicState REST；
- `character.state.changed`；
- expression/generation identity；
- ServerInfo feature flag；
- 激活/重连快照；
- Live2D/TTS 角色关联。

退出条件：重连、切会话、迟到事件、TTS preview/播放均保持正确角色身份。

### 阶段 H：迁移、文档与联调

- 旧会话/版本/记忆幂等迁移；
- 更新 database-storage、architecture、protocol/security、README；
- 生成 TS/schema，前端 check-types；
- Windows Electron 真机联调；
- feature flag 最后开启。

实现注记（2026-09-08）：后端功能、生成契约、能力开关及 Electron 静态对齐已完成；Windows Electron 对真实后端的最终人工冒烟仍待单独记录，因此此处不把“联调”写成已验收。

### 17.1 前后端并行开工与依赖门

为避免前端等待整个后端完成，把阶段 C 的“纯契约”拆成 **BE-C0 契约子批**，但不改变 A→H 的产品放量顺序：

| 可并行工作 | 允许开始 | 合并/退出门禁 |
|---|---|---|
| BE-A/BE-B Prompt 基线与去专属化 | 立即 | 泄漏测试和 Elysia 回归通过 |
| BE-C0 `frontend_contract` DTO/事件/golden | 立即，feature 恒 false | §11.0.1 handoff bundle + `make check-types` |
| FE-A raw/domain schema、mapper、旧兼容 | 立即按冻结文档搭骨架 | 必须消费 BE-C0 同一 commit/schemaHash 后才退出 |
| BE-C + FE-B/FE-C | BE-C0 稳定后 | UserIdentity + 两个内置角色只读链路 |
| BE-D + FE-D | BE-C 退出后 | CRUD/版本/冲突/Preview；仍不能进入真实聊天 |
| BE-E/BE-F + FE-E | 对应前置完成后 | 会话/runtime/memory 全隔离；profiles 仍 false |
| BE-G + FE-F | BE-F 退出后 | state/Live2D/TTS identity 竞态通过 |
| BE-H + FE-G | 全部功能门禁通过后 | 迁移、真机联调，最后开启 feature |

前端可以先写本地领域类型、Mock 和 UI 骨架，但不得自行把文档示意当成已发布机器契约；后端也不得因前端已写 UI 就提前置 feature=true。

---

## 18. 测试策略

### 18.1 单元测试

- CharacterDraft 字段长度、Unicode、reserved tags、路径校验；
- manifest 路径越界/缺文件 fail-fast；
- Revision checksum 与规范化稳定；
- built-in immutable；
- revision append-only/rollback；
- expectedRevision/stateVersion 冲突；
- PromptCompiler 对同一输入确定性；
- UserIdentity fallback/initialized/revision；
- voice precedence；
- RelationshipProvider 无 Neo4j 时返回合法最小快照。

### 18.2 Prompt 测试

- 12 section 顺序保持；
- F1/F2/F3 始终存在且 authored 内容不能覆盖；
- Elysia 迁移前后关键行为 case 不回退；
- 冷静角色 Prompt 中不出现 `Elysia/爱莉/爱莉希雅/♪/四阶段/调情`，除非角色自己填写；
- 自定义角色 A/B 的 authored 内容互不出现；
- SpeakingStyleEdit 只编辑当前角色 baseline；
- CharacterReflector 不固定用户为“她”或“妖梦”；
- C8 无真实状态时为空；
- Preview 不含 memory/tools/真实 state。

### 18.3 Repository 与迁移测试

- 所有 unique/index 创建；
- update CAS 原子性；
- custom owner 过滤；
- archive 既有会话可读、新会话拒绝；
- built-in/custom ID 冲突拒绝；
- session → legacy instance backfill 幂等；
- character_versions 复制幂等；
- journal 崩溃恢复与冲突 quarantine；
- Mongo 离线：cached custom 可读、不可写；uncached 不冒充其他角色。

### 18.4 记忆隔离测试（P0）

至少覆盖：

1. Elysia 保存记忆后 Cyrene 搜不到；
2. Cyrene 的文本 fallback 搜索也搜不到 Elysia；
3. Milvus ANN filter 在数据库侧生效；
4. MemoryReflector A 不能 merge/update/delete B 的 ID；
5. 恶意 LLM 输出另一个 scope ID 时 repository 拒绝；
6. 重启加载只恢复当前 scope metadata；
7. legacy unscoped 默认不进入任何新角色 C4；
8. 工具 `search_memory/save_memory` 同样从 TurnCharacterContext 取得 scope。

### 18.5 集成测试

- 创建两个角色 → 分别创建会话 → 对话 → 重连 → 身份不串；
- 同一个 Elysia instance 的两个新会话共享 L2/L3/L4 连续性；
- 两个不同 instance 即使基于同一 Definition 也互相隔离；
- 编辑角色时 in-flight generation 仍使用旧 revision，下一轮用新 revision；
- reset_evolution 不删会话/记忆，且返回明确变更；
- session activate 不再换绑全局 Engine；
- 后台任务结果返回原会话角色；
- TTS/Live2D expression 含正确 instance identity；
- WS 迟到旧角色事件不会污染当前角色 UI；
- Preview 零持久化副作用。

### 18.6 契约测试

- Pydantic → schema → TS 生成无 drift；
- 新字段迁移期 optional，feature 开启后服务端真实填充；
- `CharacterStatePolicy` 只接受 `preserve/reset_evolution`；绑定、generation 和 public state 只使用 `characterRevision`；
- character list/revision list 锁定 `{items,has_more,cursor?}`，Main mapper fixture 锁定 `{items,hasMore,nextCursor?}`；
- create/clone、update/rollback、archive、validate/preview、reset 的响应分别锁定为 §11.0 的不同 DTO，禁止通用任意 dict；
- `ServerMessage` 判别联合包含新事件；
- generation 三终态、state、expression/directive、audio_start 的 identity golden frame 齐全；
- strict 旧客户端在能力协商下不接收未知字段；新前端能接收 feature=false 时的 optional additive 字段；
- 错误码、HTTP 和 WS error envelope 一致；
- 不在 schema 中暴露 Prompt 全文、内部路径或 L2/L3 debug 数据。

---

## 19. 上线验收标准

以下全部满足才可以声明“自定义角色后端可联调”：

### 19.1 角色与 Prompt

- [ ] Elysia 与 Cyrene 都有有效内置 manifest、稳定 ID 和 Revision；
- [ ] 新建自定义角色无需重启后端即可被查询和使用；
- [ ] 自定义角色不能编辑 Framework Prompt；
- [ ] 非 Elysia 角色最终 Prompt 无 Elysia 专属泄漏；
- [ ] 角色编辑有 revision、checksum、expectedRevision 冲突保护；
- [ ] built-in 可克隆但不可覆盖/归档。

### 19.2 身份、会话与状态

- [ ] “我”有已持久化 UserIdentity 和 initialized 状态；
- [ ] 用户代词/称谓不再来自 CharacterConfig；
- [ ] 每个 session 有稳定 CharacterInstance binding；
- [ ] 同一 instance 跨会话连续，不同 instance 状态隔离；
- [ ] generation/message 可追溯实际角色 revision；
- [ ] 产品态角色状态可 REST 获取并通过 WS 更新；
- [ ] L2/L3 等内部态不会默认泄露给产品 API。

### 19.3 记忆与运行时

- [ ] 所有 LTM 产品 API 强制 MemoryScope；
- [ ] Milvus 与 fallback 均通过跨角色隔离测试；
- [ ] Consolidator/Reflector 作用域正确；
- [ ] TTS、Live2D、mood、proactive 使用当前 instance；
- [ ] 角色不可用时不静默回退为其他角色；
- [ ] Mongo 离线语义与缓存/journal 已验证。

### 19.4 契约、迁移与文档

- [ ] frontend_contract、TS、schema 同步；
- [ ] 旧会话/状态迁移幂等且无损；
- [ ] 旧 unscoped memory 未自动泄漏给新角色；
- [ ] Windows Electron 重连/切会话/迟到事件联调通过；
- [ ] `server_info.features.character_profiles` 最后才设 true；
- [ ] docs/README、database-storage、architecture、protocol/security 已更新。

---

## 20. 实施时禁止的捷径

以下方案看似快，但会直接破坏目标架构：

1. 只增加 `character_name + prompt_text` 两个数据库字段；
2. 让前端提交完整 system prompt 覆盖后端模板；
3. 新建角色时复制全部 Jinja 模板到新目录；
4. 继续让一个全局 CharacterEngine 通过 `set_character()` 动态换名字；
5. 把 CharacterState 留在 session，再声称同一角色跨会话连续；
6. 仅给 MemoryEntry metadata 加 characterId，却不在 ANN/fallback/Reflector 强制过滤；
7. custom 读取失败时使用启动 Elysia 兜底；
8. 通过角色显示名关联会话、声音、Live2D 或记忆；
9. 自动把旧全局记忆复制给所有角色；
10. 先把 feature flag 打开，再逐步补隔离；
11. 把 Neo4j 作为角色 CRUD 的前置条件；
12. 把 Debug CharacterState 原样当作产品状态 API。

---

## 21. 后续扩展点（不阻塞 V1）

完成本文 V1 后，可在不破坏核心身份模型的前提下继续：

- Neo4j RelationshipContextProvider；
- 用户显式共享/撤回 `user_shared` 记忆；
- 同一 Definition 的多时间线 CharacterInstance；
- 角色导入/导出包与资产签名；
- 角色设定向导或 LLM 辅助生成（仍只产 CharacterDraft）；
- OwnLifeEngine 与真实活动时间线；
- 可解释的关系成长事件与证据；
- 多设备/多用户 owner 模型；
- 角色市场和版本依赖；
- 角色级工具偏好，但工具权限仍归 Framework/用户授权；
- 角色级 TTS style/emotion mapping；
- Prompt eval 与回归评分面板。

这些扩展都应继续使用 `characterId / revision / instanceId / userId / MemoryScope`，不能另建一套身份体系。

---

## 22. 代码事实到目标组件的映射

| 当前文件/对象 | 目标去向 |
|---------------|----------|
| `core/config/character_config.py` | 内置 manifest adapter；不再承担全部角色域模型 |
| `character/elysia/personality.md` | Elysia Revision.corePersonality |
| `Module/system_prompt/templates/c2/c3/c6/c7` | 通用 wrapper；Elysia 内容移出 |
| `core/prompt/prompt_assembler.py` | TurnPromptContext 驱动、无 set_session 的 assembler |
| `core/personality/engine.py` | CharacterRuntime 内的 instance-scoped engine |
| `CharacterEngine.attach_session()` | 删除；由 session binding → RuntimeRegistry 解析替代 |
| `core/schema/character.py::CharacterState` | 兼容 adapter → CharacterRuntimeState |
| `sessions.character_state` | legacy shadow → character_instances.state |
| `character_versions(session_id)` | `character_state_versions(instance_id)` |
| `core/cognitive/user_profile.py` | delegation adapter；权威模型迁 `core/profile` |
| `config.agent.user_name` | UserIdentity bootstrap fallback |
| `CharacterConfig.user_pronoun` | 删除；改由 UserIdentity.pronouns |
| 全局 `LongTermMemory` | 底层 store + 强制 ScopedMemoryView |
| 全局 `MemoryConsolidator/Reflector` | CharacterRuntime scoped 组件 |
| `IdleMonitor(CharacterConfig)` | active CharacterRuntime/ProactiveCoordinator |
| `agent.character` 的 TTS 读取 | TurnCharacterContext/显式角色参数 |
| `expression` | 兼容事件 + instance/conversation identity |
| Debug character routes | 保留内部态；新增独立产品 CharacterPublicState |

---

## 23. 最终实现判断

本项目已有的十二段 Prompt、四层人格、反思/压缩/觉察链路并不需要推倒重来。真正需要重构的是它们的**所有权与作用域**：

- Elysia 的内容回到 Elysia 角色版本；
- 框架规则留在系统模板；
- 用户身份属于 UserIdentity；
- 演化状态属于 CharacterInstance；
- 对话属于 Session；
- 记忆属于明确的 MemoryScope；
- 未来关系图谱通过 Provider 提供快照。

完成这次拆分后，Elysia 和 Cyrene 才不是“同一个 Agent 换了名字”，而是两个拥有独立设定、状态、记忆、声音与连续性的角色。这个边界也是后续情感陪伴功能继续扩展时必须守住的基础。
