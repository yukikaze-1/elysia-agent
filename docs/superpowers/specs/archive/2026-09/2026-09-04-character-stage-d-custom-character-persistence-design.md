# 阶段 D：Custom Character 持久化与版本 Spec

> **文档导航：** [项目文档](../../../../README.md)
> **Spec 状态：** reviewed（终审通过，无 blocking/correctness/architecture 遗留）
> **日期：** 2026-09-04
> **架构稿：** [`docs/2026-08-30-character-prompt-customization-backend-architecture-design.md`](../../../../2026-08-30-character-prompt-customization-backend-architecture-design.md)（§11 REST + §12 WS + §13 校验/安全 + §14 错误码 + §17 阶段 D）
> **事实基线：** `main@24b42ae7`（阶段 C+E 完成后 HEAD）
> **性质：** 生产代码 + 测试批次——把「用户可自定义角色」从领域模型层面提升到「Mongo 持久化 + CRUD + 版本 + 并发控制 + 双层校验」
> **权威范围：** `CharacterDraft` 领域模型、`CharacterService`、`CharacterRepository`/`CharacterRevisionRepository`、REST 端点（`/api/me`、`/api/characters*`）、错误码体系、`CharacterConfig.user_pronoun` 彻底移除、`ConversationExample` 子对象
> **明确排除：** custom 参与真实聊天（`character_profiles=false` 恒抑制 wire）；`CharacterPublicState` 实际发送（§12.4）；`preview` 端点（§11.3，留本阶段末或 E）；`defaultCharacterId` 归档 fallback 的完整实现（§11.0，留占位）；CharacterRuntime instance-scoped（阶段 E）；记忆 v2 隔离（阶段 F）

---

## 0. 一句话目标

把「用户可自定义角色」从领域模型层面（阶段 C+E 的 `CharacterDefinition`/`CharacterRevision`/`CharacterInstance` 只读内置）提升到「**Mongo 持久化 + 完整 CRUD + 版本 append-only + expectedRevision/CAS 并发控制 + 双层校验（Pydantic 字段边界 + 语义保留内容）**」，让 custom 角色可以被创建、编辑、克隆、归档、回滚、校验——但**本批 custom 仍不参与真实聊天**（`character_profiles=false`）。

**三个关键不变式贯穿全批：**
1. **CAS 原子性** —— update 用 `filter={"character_id":x,"current_revision":expectedRevision}`（snake_case）+ `$set`（恒定含必变字段 `updated_at`）原子更新，命中判定用 `modified_count>0`（`updated_at` 必变规避 MongoDB no-op 误判；后端不暴露 `matched_count`）；未命中 → `CHARACTER_REVISION_CONFLICT`；不用读-改-写。
2. **append-only 版本** —— 每次 create/update/rollback 产生新 Revision（append），`Definition.currentRevision` 递增；rollback 是「复制旧版本为新 Revision」，不删除任何历史版本。
3. **custom 不参与聊天** —— `character_profiles=false`，custom 只能被 CRUD，不进 runtime/engine/session。

**Elysia 现网行为：** `CharacterConfig.user_pronoun` 彻底移除（阶段 C+E 已把运行时决策点从它摘除，本批物理删除字段 + YAML + fixtures，属清理）；系统 prompt 渲染字节不变（`user_pronoun` 仍经 `set_context` 从 `UserIdentity.pronouns` 注入）。

---

## 1. 背景与问题

### 1.1 阶段 C+E 交付的底座（已核实）

阶段 C+E（`main@24b42ae7`）已交付：
- **领域模型** `CharacterDefinition`（§5.2）/`CharacterRevision`（§5.3）/`CharacterInstance`（§5）——见 [core/character/definition.py](../../../../../core/character/definition.py)、[revision.py](../../../../../core/character/revision.py)、[instance.py](../../../../../core/character/instance.py)；
- **`BuiltinCharacterSource`** —— 从 `character/{name}/config.yaml` + personality.md 构建只读内置角色，不重写 manifest v2；
- **`CharacterCatalog`** —— 只读内置角色列表/校验，无 Mongo 也能跑；
- **`UserIdentity`** + `UserIdentityStore` —— 用户最小身份，`pronouns` 全局唯一「她」，`initialized` 仅 UX 标记；
- **`TurnCharacterContext`** —— 每轮冻结的角色/用户/版本快照，`user_pronoun` 从 `UserIdentity.pronouns` 读（B1 正位）；
- **`character_profiles=false`** feature flag —— 保留抑制 wire。

### 1.2 缺口：custom 角色「不能持久化、不能 CRUD」

阶段 C+E 的 `CharacterCatalog` 只读内置角色（`core/character/catalog.py`：`list()`/`get()`/`validate()` 全部基于 `BuiltinCharacterSource`，无 Mongo）。用户无法：
- 创建自己的角色（写 Mongo）；
- 编辑/克隆/归档/回滚；
- 校验角色内容（保留内容/框架语义）；
- 通过 REST 访问 `/api/characters*`。

架构稿 §17 阶段 D 明确对应这一切：**Mongo repositories/indexes + create/get/list/update/clone/archive/revisions/rollback + expectedRevision/CAS + last-known-good 缓存与离线写 fail-closed + validate + 审计与错误码**。退出条件：「CRUD/版本/并发/离线语义通过，但还不允许 custom 参与真实聊天」。

### 1.3 `CharacterConfig.user_pronoun` 彻底移除（阶段 C+E 的承诺）

阶段 C+E spec（§5.2 精确处置）明确：「`CharacterConfig.user_pronoun` 标记 deprecated……阶段 D 再彻底移除」。本轮彻底移除的波及面（已核实）：
- `core/config/character_config.py:29` 字段 + `:80` 解析；
- `character/{elysia,cyrene}/config.yaml:9` 的 `user_pronoun:` 行（Cyrene 的 `你` 是 B-2 遗留错位，已死字段）；
- `core/prompt/tests/conftest.py:33,77` 构造 `user_pronoun=` 的 fixture；
- `core/prompt/tests/test_prompt_assembler.py` 等对 `_character` 的断言。

**必须保留**（非删除面）：
- `Module/system_prompt/sections/*.py` 的 `kwargs.get("user_pronoun","她")` 回退链——`prompt_assembler` 仍经 `set_context(user_pronoun=...)` 注入渲染；
- `Module/system_prompt/templates/*.j2` 的 `{{ user_pronoun }}` 占位符——渲染自 `UserIdentity.pronouns`，不取自 config.yaml。

### 1.4 `examples` 建模（架构稿 §405 + §11.6）

架构稿 §405：`examples` 是 C1/C6 辅助 few-shot，0–20 组 user/assistant，不得包含 framework 指令。§11.6：`promptProfile.examples` 是 `[{user, assistant}]` 数组字面量，无独立 DTO 名。

**本批决策**：轻量 `ConversationExample` 子对象（`user`/`assistant` 各 ≤1000 字符，Pydantic 校验），只出现在 **draft 层**（用户输入校验点）。`CharacterRevision.promptProfile` 保留宽松 `dict` 存 `examples` 为 `list[dict]`（不可变历史快照，类型宽松兼容 future 迁移）。

---

## 2. 交付边界

### 本批做

1. **`CharacterDraft` 领域模型**（`core/character/draft.py`）+ `ConversationExample` 子对象 + 第一层 Pydantic 校验；
2. **`CharacterService`**（`core/character/service.py`）—— create/get/list/update/clone/archive/rollback/validate 编排 + CAS 冲突抛错 + 第二层语义校验；
3. **`CharacterRepository`**（`core/storage/character_repo.py`）—— `characters` 集合 CRUD + CAS + last-known-good 缓存；
4. **`CharacterRevisionRepository`**（`core/storage/character_revision_repo.py`）—— `character_revisions` 集合 append + 版本列表；
5. **REST 端点**（`core/input/character_routes.py`）—— `/api/me`、`/api/characters*`（不含 instance state） + 错误码映射；
6. **错误码体系** —— 对齐架构稿 §14 冻结码（`CHARACTER_NOT_FOUND`/`VALIDATION`/`REVISION_CONFLICT`/`IMMUTABLE`/`ARCHIVED`/`BUSY`/`STORAGE_UNAVAILABLE` 等）；
7. **双层校验** —— Pydantic 字段边界（§13.1）+ 保留内容 hard-error（§13.2）；**语义 warning 判定本批降级为 hard-error 白名单 + 关键字启发式，不引入 LLM 判定**（见 §8.2）；
8. **`CharacterConfig.user_pronoun` 彻底移除**；
9. **frontend_contract 补齐 DTO**（`CharacterListResponse`/`CharacterDetail`/`CharacterMutationResult`/`CharacterValidationResult`/`CharacterRevisionListResponse`/`CharacterArchiveResult` 等）；
10. **`CharacterService` 合并内置 + custom** —— `CharacterCatalog` 保持只读内置不变；`GET /api/characters` 由 `CharacterService.list()` 合并内置（catalog）与 custom（repo）来源。

### 本批不做（明确排除）

- **preview 端点**（`POST /api/characters/preview`，§11.3）—— 留本阶段末或阶段 E；
- **`CharacterPublicState` 实际发送**（§12.4）—— wire 由 `character_profiles=false` 抑制；
- **`defaultCharacterId` 归档 fallback**（§11.0）—— 完整实现留占位，本批只保证归档后 `userProfile` 返回；
- **custom 参与真实聊天** —— runtime/engine/session 不变；
- **CharacterRuntime instance-scoped**（阶段 E）；
- **记忆 v2 隔离**（阶段 F）；
- **`app.router` 注册顺序与既有 api_routes 的冲突**——用独立 `character_routes` 模块挂载，不重构 api_routes。

---

## 3. 架构方案

### 3.1 分层

```
core/character/                    # 领域模型 + 业务服务
├── draft.py                       # CharacterDraft + ConversationExample + 双层校验(新增)
├── revision.py                    # 扩展 promptProfile 支持 examples/relationshipStyle 等(修改)
├── service.py                     # CharacterService: create/get/list/update/clone/archive/rollback/validate(新增)
core/storage/
├── character_repo.py              # characters 集合: Definition 持久化 + CAS(新增)
├── character_revision_repo.py     # character_revisions 集合: Revision append + 版本列表(新增)
core/input/
└── character_routes.py            # REST 端点 + 错误码映射(新增)
```

### 3.2 职责边界（上层依赖下层，单向，不允许反向）

```
[frontend_contract DTO]  ← wire 形状(只读,不进核心)
        ↓ 构造
[CharacterDraft 领域模型]  ← 双层校验: Pydantic 字段边界 + 语义限制
        ↓
[CharacterService]  ← 业务编排: create/update/rollback/validate + CAS 冲突抛错
        ↓                          ⋮ 合并内置(catalog) + custom(repo)
[CharacterRepository]  ← 薄 CRUD: insert/find/cas_revision_pointer/aggregate
[CharacterRevisionRepository]  ← 薄 append: 版本写入 + 版本列表
        ↓
[MongoDB (characters + character_revisions)]

[CharacterCatalog]  ← 只读内置(无 Mongo;不感知 custom,合并由 CharacterService 做)
```

### 3.3 各组件职责

| 组件 | 职责 | 关键约束 |
|------|------|---------|
| `CharacterDraft` | 用户提交的完整角色表单（custom 唯一入口） | Pydantic 双层校验；`examples` 用 `ConversationExample` 子对象 |
| `ConversationExample` | 一组 user/assistant few-shot | 每组各 ≤1000 字符 |
| `CharacterRevision` | 不可变版本快照 | 扩展 `promptProfile` 支持 5 个结构化键 + examples |
| `CharacterService` | create/get/list/update/clone/archive/rollback/validate 编排 | CAS 冲突抛 `RevisionConflict`；validate 供 POST validate 复用；合并内置+custom |
| `CharacterRepository` | `characters` 集合 CRUD | CAS 用 `filter={"character_id":x,"current_revision":expected}`；snake_case 落库 |
| `CharacterRevisionRepository` | `character_revisions` 集合 append | `aggregate` 取 max(revision)+1；append-only |
| `character_routes.py` | REST 端点 + 错误码映射 | 错误码对齐架构稿 §14；`character_profiles=false` 恒抑制 wire |
| `CharacterCatalog` | 只读内置角色（无 Mongo） | 保持 `BuiltinCharacterSource` 驱动，不感知 custom |

---

## 4. 领域模型

### 4.1 CharacterDraft（`core/character/draft.py`）

```
CharacterDraft  (Pydantic BaseModel)
├── displayName        str           必填 1–64 字符,去首尾空白
├── description        str           默认"" ≤500 字符
├── aliases            list[str]     ≤10 项,每项 1–32 字符,大小写/Unicode 规范化后去重
├── promptProfile      PromptProfile
│   ├── corePersonality       str   必填 1–12,000 字符
│   ├── relationshipStyle     str   必填 1–6,000 字符
│   ├── emotionStyle          str   可空 ≤5,000 字符
│   ├── speakingStyle         str   必填 1–6,000 字符
│   ├── conversationInitiative str  可空 ≤3,000 字符
│   └── examples              list[ConversationExample]  ≤20 组,每组各 ≤1,000 字符
├── runtimeDefaults    RuntimeDefaults
│   ├── voice               {providerId?, voiceId?}
│   ├── emotion             {idleExpressions: list[str]}
│   ├── wakeWords           list[str]  ≤20 项,每项 1–32 字符
│   └── proactive           {enabled: bool, intervalSeconds: int, triggers: list[dict]}
├── presentation       Presentation
│   ├── avatarAssetId  str|null
│   ├── live2dModelId  str|null
│   └── themeColor     str|null
└── openingMessage     str|null   ≤1,000 字符
```

### 4.2 ConversationExample

```
ConversationExample  (Pydantic BaseModel)
├── user        str  1–1,000 字符
└── assistant   str  1–1,000 字符
```

### 4.3 CharacterRevision 扩展（修改 `revision.py`）

`promptProfile` 从 `dict[str, object]` 自由容器改为带键约束常量的宽松 dict（**不强制**，兼容 builtin 只填部分）：

```python
# 键约束(不强制,builtin 只填部分;custom 走 draft 校验)
REVISION_PROMPT_PROFILE_KEYS = (
    "corePersonality", "relationshipStyle", "emotionStyle",
    "speakingStyle", "conversationInitiative", "examples",
)

promptProfile: dict[str, object] = Field(default_factory=dict)
```

`examples` 存为 `list[dict]`（保留 wire 兼容），不强制 `list[ConversationExample]`——因为 revision 是持久化快照，宽松类型有利于兼容 future schema 迁移。**`ConversationExample` 只出现在 draft 层**（用户输入校验点）。

### 4.4 双层校验（§13.1 + §13.2）

| 层 | 位置 | 职责 |
|----|------|------|
| **第一层** Pydantic | `CharacterDraft` 字段约束 + `ConversationExample` | 字符数、必填、去空白、规范化去重 |
| **第二层** 语义 | `CharacterService.validate()` | 保留 XML 标签/Jinja delimiters/NUL/control → hard error；"修改工具权限"/"不要 output mood"/"泄露 system prompt" → warning；authed 内容不进 Framework 标签内部 |

**第二层校验输出**：`list[CharacterValidationIssue]`（severity/code/path/message），与 `POST /api/characters/validate` 返回形状一致。用 **JSON Pointer** 作为 path（如 `/promptProfile/corePersonality`）。

---

## 5. Repository 与 CAS 并发控制

### 5.1 CharacterRepository（`characters` 集合）

继承 `core/storage/base.py::Repository`（`_call` fail-closed + `StorageUnavailableError`）。方法：

```python
class CharacterRepository(Repository):
    async def connect(self) -> bool       # 建连接 + ensure_index(characterId unique)
    async def close(self) -> None
    async def health_check(self) -> bool

    async def insert_definition(self, definition: CharacterDefinition) -> None   # create
    async def get_definition(self, character_id: str) -> CharacterDefinition | None
    async def list_definitions(self, *, source: str | None = None,
                               status: str | None = None, owner_user_id: str | None = None,
                               search: str | None = None, cursor: str | None = None,
                               limit: int = 50) -> list[CharacterDefinition]
    async def cas_revision_pointer(self, character_id: str, expected_revision: int,
                                   new_revision: int, updated_at: str) -> bool
    async def archive(self, character_id: str) -> None    # status="archived"
```

**CAS 实现（对齐 `idempotency_repo.py:237-250` 既定先例）**：后端 `update_one` 只返回 `modified_count`，**不暴露 `matched_count`**（`mongodb.py:106`）。MongoDB 的 `modified_count` 在 `$set` 写出与已有值完全相同（no-op）时为 0，会误判为未命中。因此 **`$set` 恒包含必变字段 `updated_at=now`**（模拟 idempotency_repo 的 `claimed_at` 模式），保证命中路径 `modified_count>0` 可靠。判定：`modified_count > 0` → 命中（CAS 成功）；`== 0` → 未命中（expected_revision 过期）→ 调用方抛 `RevisionConflict`。**不用读-改-写**。UPSERT 关闭（`upsert=False`）。

**字段命名**：Mongo 文档用 **snake_case**（`character_id`/`current_revision`/`owner_user_id`/`created_at`/`updated_at`），对齐架构稿 §10.1 与既有 `session_repo`。Pydantic 模型用 camelCase（`CharacterDefinition.characterId` 等），Repository 在序列化/反序列化边界做 **snake_case ↔ camelCase 桥接**（`_to_doc`/`_from_doc` 两个 helper，_id 转换 + `_id` 剥除）。

**嵌套 dict 约定**：`promptProfile`/`runtimeDefaults`/`presentation` 等嵌套 dict 是通用 JSON，**不做键转换**——用 `model_dump(mode="json")` 原样存（键保留 camelCase，对齐 `session_repo.py:151` 的 `character_state.model_dump(mode="json")` 先例）。仅领域模型**顶层字段**做 snake_case↔camelCase 转换。CAS filter 一律用 snake_case 文档键。

**last-known-good 缓存**：`get_definition`/`list_definitions` 读时若成功，缓存一份只读快照到内存 `dict`（LRU 上限）；Mongo 离线时（`StorageUnavailableError`）已缓存 custom 可继续只读返回，未缓存则抛。**写侧失效**：`insert_definition`/`cas_revision_pointer`/`archive` 成功后**同步失效**该 characterId 的缓存（写-through），保证离线读到的缓存与最近一次成功写一致（read-your-writes）。

### 5.2 CharacterRevisionRepository（`character_revisions` 集合）

继承 `Repository`。方法：

```python
class CharacterRevisionRepository(Repository):
    async def connect(self) -> bool       # ensure_index(characterId+revision unique)
    async def close(self) -> None
    async def health_check(self) -> bool

    async def append_revision(self, revision: CharacterRevision) -> None   # 新版本 append
    async def get_revision(self, character_id: str, revision: int) -> CharacterRevision | None
    async def list_revisions(self, character_id: str, *, cursor: str | None = None,
                             limit: int = 50) -> list[CharacterRevision]
    async def max_revision(self, character_id: str) -> int   # aggregate max(revision)，无则 0
    async def delete_revision(self, character_id: str, revision: int) -> None  # CAS 失败回滚
```

**max_revision** 用聚合 `[{"$match":{"character_id":x}},{"$group":{"_id":None,"max":{"$max":"$revision"}}}]`（snake_case 键），返回 0 表示无。

### 5.3 跨集合一致性（create + 每版本，Revision-first 写序）

对齐架构稿 §10.1（`current` 指针永远指向 durable Revision）与 §5.4 离线语义。本批后端 DocumentDB 抽象**不支持 Mongo transaction**，采用骨架稿 §10.1 item 2 的降级方案：

**create（新角色，create + revision=1）** —— 写序（对齐 §10.1 item 5：Revision durable 后 Definition 才可见）：
1. `append_revision(revision=1)` 先写 Revision（此时 Definition 不存在，revision 1 为孤儿，被 list/get 的 `get_definition==None` 守卫过滤——对读者不可见）；
2. `insert_definition(current_revision=1)` 再插入 Definition；
3. 若第 2 步失败（如离线），`delete_revision(character_id, 1)` 回滚删除孤儿 Revision（best-effort；若补偿删除也失败，产生孤儿 revision 无 definition——由 `list/get` 守卫过滤，完整 reconciliation 留阶段 H）。

**update（新版本）** —— 写序（对齐修改后的 §6.2/B4 裁决）：
1. `append_revision(max+1)` 先写 Revision（durable）；
2. `cas_revision_pointer(expected_revision, max+1)` 再 CAS 更新 Definition 指针；
3. CAS 失败（`modified_count==0` 未命中，后端只暴露 `modified_count`）→ **回滚已 append 的 Revision**（`delete_revision`），抛 `RevisionConflict`（details.currentRevision）。**绝不先更新 Definition 指针再写 Revision**（否则 `currentRevision` 指向不存在的版本，get_definition 读到 dangling 指针）。

**rollback（复制旧版本为新版本）** —— 写序同 update：先 append_revision(max+1)（内容拷贝自 target_revision），再 cas_revision_pointer。

**唯一权威写者**：`current_revision` 只有 `cas_revision_pointer` 一个写者（CAS gate），`append_revision` 不碰 Definition 指针——消除 §B4 的双写者冲突。`revision` 字段由 `CharacterRevision.revision` 自身承载（append 时确定），与 Definition 指针联动靠 CAS 保证。

**跨集合两步骤间失败**：由 CharacterService 的补偿回滚处理。完整 transaction/outbox 留阶段 H（架构稿 §10.1 item 1）。

### 5.4 离线语义

- **Mongo 离线**：`insert`/`update`/`archive` 抛 `StorageUnavailableError` → REST 映射 `503 CHARACTER_STORAGE_UNAVAILABLE`（fail-closed，禁止静默 no-op）；
- **已缓存 custom 读取**：`get_definition`/`get_revision` 在离线时从 last-known-good 缓存返回；未缓存抛。

---

## 6. CharacterService 业务编排

### 6.1 方法签名

```python
class CharacterService:
    def __init__(self, *, repo: CharacterRepository, rev_repo: CharacterRevisionRepository,
                 catalog: CharacterCatalog, user_identity_store: UserIdentityStore): ...

    async def create(self, draft: CharacterDraft, *, owner_user_id: str,
                     created_by: str = "user") -> CharacterDetail
    async def get(self, character_id: str) -> CharacterDetail | None
    async def list(self, *, source: str | None = None, status: str = "active",
                   search: str | None = None, cursor: str | None = None,
                   limit: int = 50) -> CharacterListResponse
    async def update(self, character_id: str, draft: CharacterDraft,
                     *, expected_revision: int, state_policy: str = "preserve") -> CharacterMutationResult
    async def clone(self, character_id: str, *, display_name: str | None = None) -> CharacterDetail
    async def archive(self, character_id: str) -> CharacterArchiveResult
    async def rollback(self, character_id: str, *, target_revision: int,
                       expected_revision: int, state_policy: str = "preserve") -> CharacterMutationResult
    async def validate(self, draft: CharacterDraft) -> CharacterValidationResult
```

### 6.2 关键行为

- **create**：`validate(draft)` → 生成 `characterId=chr_custom_{uuid}` → **Revision-first 写序**（append_revision(1) 先写孤儿 Revision → insert_definition(current_revision=1) 再插 Definition；失败补偿 delete_revision）→ 构造 `CharacterDetail`（summary + current revision）；
- **update**：`validate(draft)` → 检查 `status != "archived"` → **Revision-first 写序**（append_revision(max+1) → cas_revision_pointer(expected_revision, max+1)）；CAS 失败抛 `RevisionConflict`（details.currentRevision）+ 回滚已 append 的 revision；成功返回 `CharacterMutationResult`（character=新详情，stateImpact=preserve/0/0/[]）；
- **clone**：从源角色当前 revision 复制为新的 custom（`characterId=chr_custom_{uuid}`，currentRevision=1，owner_user_id=当前用户），写序同 create；
- **archive**：仅 custom（builtin 抛 `CharacterImmutable`）；`status="archived"`，返回 `CharacterArchiveResult`（character=summary；若归档的是 UserIdentity.defaultCharacterId，本批只占位不执行 fallback swap——见 §11.0 边界，userProfile 返回当前档案）；
- **rollback**：复制指定旧版本（target_revision）内容为**新版本**（max+1），写序同 update（append_revision 拷贝 → cas_revision_pointer）→ 返回 `CharacterMutationResult`；**不删除**历史版本；
- **validate**：纯校验（无副作用），供 `POST /api/characters/validate` 复用，返回 `CharacterValidationResult`（valid/issues/estimatedPromptChars）。

### 6.3 自定义异常

```python
class CharacterError(RuntimeError): ...
class CharacterNotFoundError(CharacterError): ...
class CharacterImmutableError(CharacterError): ...        # builtin 尝试编辑/归档
class CharacterArchivedError(CharacterError): ...          # archived 不可新建
class CharacterValidationError(CharacterError): ...        # 单条校验失败,details.issues
class RevisionConflictError(CharacterError): ...           # expectedRevision 过期,details.currentRevision
class CharacterStorageUnavailableError(CharacterError): ... # 离线无缓存
```

### 6.4 UserIdentityStore CAS 升级（PUT /api/me 支撑）

`PUT /api/me` 的 expectedRevision 冲突语义（`USER_PROFILE_REVISION_CONFLICT/409`）要求 `UserIdentityStore` 具备 revision 感知的 CAS。当前 `save()` 是读-改-写（`touch()` 递增 revision，无比对）。

**本批升级**：给 `UserIdentityStore` 加一个 CAS 方法（保留 `bootstrap/get/save` 兼容）：

```python
def update_if_revision(self, expected_revision: int,
                       updates: dict[str, object]) -> UserIdentity | None:
    """CAS 更新：仅当当前 revision == expected_revision 时应用 updates 并 touch()。

    Updates 是白名单字段（displayName/preferredAddress/pronouns/locale/timezone/
    defaultCharacterId/initialized）；不接收 userId/revision/createdAt/updatedAt。
    冲突返回 None（调用方抛 USER_PROFILE_REVISION_CONFLICT）；成功返回新 UserIdentity。
    """
```

单进程下用**内存锁 + revision 比对**实现原子 CAS（JSONL 单文件，无并发进程写）。首版 `initialized` 只在首次 PUT 由后端置 true，之后不能经此接口改回 false（§11.1）。省略 `preferredAddress/pronouns` 表示清空；省略 `defaultCharacterId` 表示 bootstrap 内置角色。

**Mongo profile 集成就绪后**（阶段 G/H）换 `UserIdentityRepository`，CAS 用 `filter={"revision": expected}` 原子更新——本批 JSONL 内存锁足够（单机单进程）。

---

## 7. REST 端点

### 7.1 路径矩阵（对齐架构稿 §11.0 冻结）

| 方法 | 路径 | 请求 | 成功响应 |
|------|------|------|---------|
| GET | `/api/me` | 无 | `200 UserIdentityProfile` |
| PUT | `/api/me` | 完整最小身份字段 + `expectedRevision` | `200 UserIdentityProfile` |
| GET | `/api/characters` | query: `source/status/search/cursor/limit`; 默认 `status=active` | `200 CharacterListResponse` |
| POST | `/api/characters` | 顶层完整 `CharacterDraft` | `201 CharacterDetail` |
| GET | `/api/characters/{id}` | 无 | `200 CharacterDetail{summary,current}` |
| PUT | `/api/characters/{id}` | 顶层 `expectedRevision/statePolicy` + 完整 `CharacterDraft` 字段 | `200 CharacterMutationResult` |
| DELETE | `/api/characters/{id}` | 无 | `200 CharacterArchiveResult` |
| POST | `/api/characters/{id}/clone` | `{displayName?: string}` | `201 CharacterDetail` |
| GET | `/api/characters/{id}/revisions` | query: `cursor/limit` | `200 CharacterRevisionListResponse` |
| POST | `/api/characters/{id}/rollback` | `{targetRevision,expectedRevision,statePolicy}` | `200 CharacterMutationResult` |
| POST | `/api/characters/validate` | 顶层完整 `CharacterDraft` | `200 CharacterValidationResult`(无效草稿也是 200+valid=false) |
| POST | `/api/characters/preview` | **本批不做**（留本阶段末或 E） | — |
| GET | `/api/character-instances/{id}/state` | **本批不做**（阶段 G；阶段 D 无 instance 持久化，无 backing） | — |
| POST | `/api/character-instances/{id}/reset-evolution` | **本批不做**（阶段 G） | — |

`PUT` 的前端领域命令可用 `{characterId, expectedRevision, statePolicy, draft}`，但 **Main mapper 必须把 `draft` 展平为 wire**；后端不接收额外 `draft` 包装层。

### 7.2 错误码映射（对齐架构稿 §14 冻结）

| 场景 | 错误码 | HTTP | retryable |
|------|--------|------|-----------|
| 角色不存在或不可见 | `CHARACTER_NOT_FOUND` | 404 | false |
| 角色实例不存在 | `CHARACTER_INSTANCE_NOT_FOUND` | 404 | false |
| 尝试编辑/归档内置角色 | `CHARACTER_IMMUTABLE` | 409 | false |
| archived 角色不可新建会话 | `CHARACTER_ARCHIVED` | 409 | false |
| 角色内容校验失败 | `CHARACTER_VALIDATION` | 400 | false |
| 保留模板结构/标签 | `CHARACTER_RESERVED_CONTENT` | 400 | false |
| expectedRevision 已过期 | `CHARACTER_REVISION_CONFLICT` | 409 | true |
| stateVersion CAS 冲突超重试 | `CHARACTER_STATE_CONFLICT` | 409 | true |
| instance 正在生成/反思 | `CHARACTER_BUSY` | 409 | true |
| V1 尝试在已有会话换角色 | `CHARACTER_BINDING_IMMUTABLE` | 409 | false |
| custom 定义/状态不可安全加载 | `CHARACTER_STORAGE_UNAVAILABLE` | 503 | true |
| Preview 上游或运行时失败 | `CHARACTER_PREVIEW_FAILED` | 502/503 | 视原因 |
| 角色/Prompt 体积过大 | `CHARACTER_TOO_LARGE` | 413 | false |
| 用户身份 expectedRevision 过期 | `USER_PROFILE_REVISION_CONFLICT` | 409 | true |

### 7.3 错误信封

复用 `core/input/errors.py::_protocol_error_response`（`ProtocolError` 形状：code/message/retryable/requestId/error/details）。

### 7.4 feature flag 恒 false

`character_profiles=false` 恒抑制 wire：`character_routes.py` 注册路由但**不序列化** `characterId/revision` 等角色附加字段（wire 仍走 legacy 帧）。这是阶段 D 的退出条件边界——**custom 仍不参与真实聊天**。

---

## 8. 校验与安全

### 8.1 字段限制（§13.1）

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
| 整个 Revision 规范化 JSON | **硬限制 ≤ 64 KiB**（超限 → 413 `CHARACTER_TOO_LARGE`） |

限制由 **Pydantic contract/domain 双层保证**；Repository 不接受绕过 Service 的未校验对象。

### 8.2 保留内容与 Prompt 注入（§13.2）

- 对保留 XML 标签、Jinja delimiters、NUL/control characters 记 **hard error**；
- 对"修改工具权限""不要输出 mood 标签""泄露 system prompt"等语义记 **warning**，并由 F1 明确降级其优先级；
- authored 内容永远不插入 system prompt 的 Framework 标签内部；
- 模板渲染使用普通变量值，不二次 render；
- tool definitions 和权限完全由 ToolAdapter/运行时生成，角色文本不能声明一个工具就获得工具。

**语义校验的可落地实现（本批裁决）**：第二层校验的 hard-error 用**确定性白名单**（保留标签 `<conflict_rules>/<output_format>/<tts_markup>/<tools>`、Jinja delimiters `{{ }}`/`{% %}`、NUL/control chars）——这些是确定性规则，可写单测；warning 层用**关键字启发式**（"修改工具权限"/"忽略指令"/"不要输出 mood"/"泄露 system prompt"等子串匹配），判定为 warning 并**交由 F1 降级**，不阻断保存。本批**不做 LLM 语义判定**（成本/不确定性高，留后续）。warning 记录到 `CharacterValidationIssue.severity="warning"`。

### 8.3 所有权与认证（§13.3）

- 所有 `/api/me`、`/api/characters*` 路由进入现有认证/限流中间件；
- custom 查询始终加入 `owner_user_id`；
- `characterId` 不可预测也不能代替授权；
- clone 内置角色允许，clone 他人 custom 在未来多用户模式下默认禁止；
- 日志只记录 IDs、revision、长度、checksum 和错误码，不记录角色全文。
- 路由挂载：`character_routes.py` 经 `register_character_routes(app, _check_auth)` 注册（对齐 `server.py` 既有 `register_xxx_routes(app)` + `register_debug_routes(app, _check_auth)` 模式），**不重构**既有 `api_routes`（CLAUDE.md 已注明 api_routes 退役——实际冲突面不存在，用独立模块挂载即可）。

### 8.4 资产安全（§13.4）

- avatar/live2d 只接受受管 asset/model ID；
- 不接受 `file://`、UNC、本地绝对路径或任意远程 URL 作为角色详情字段。

---

## 9. `CharacterConfig.user_pronoun` 彻底移除

### 9.1 删除面（已核实）

1. `core/config/character_config.py` —— 删除 `user_pronoun` dataclass 字段 + `from_yaml` 的 `user_pronoun=raw.get(...)`；
2. `character/elysia/config.yaml` + `character/cyrene/config.yaml` —— 删除 `user_pronoun:` 行；
3. `core/prompt/tests/conftest.py` —— 删除两处 `user_pronoun=` 构造参数（行 33/77）。

**已核实：测试代码中无对 `.user_pronoun` 的取值断言**。`test_prompt_assembler.py` 的 `test_user_pronoun_from_turn_context_overrides` 只设 `_turn_context.userPronoun="她"` 不赋值 `cold_config.user_pronoun`；`test_user_pronoun_no_context_fallback_she` 走兜底常量"她"；`test_system_prompt_leak.py` 断言的是模板正文的"她"而非注入值。**删除后这些测试仍 pass**（无 `cold_config.user_pronoun` 依赖），无需额外删除断言——唯一影响是 `test_user_pronoun_from_turn_context_overrides` 失去对照基准（退化为恒真），但不阻塞；如需保留判别力，可改为直接测 `_turn_context` 覆盖逻辑。

### 9.2 保留面（渲染不变）

1. `Module/system_prompt/sections/*.py` —— `kwargs.get("user_pronoun","她")` 回退链**保留**（`prompt_assembler` 仍经 `set_context(user_pronoun=...)` 注入）；
2. `Module/system_prompt/templates/*.j2` —— `{{ user_pronoun }}` 占位符**保留**（渲染自 `UserIdentity.pronouns`）。

### 9.3 影响断言

- **Elysia byte 不变**：`test_elysia_snapshot_unchanged`（render == `system_prompt_snapshot_elysia_cf4bc008.txt`，18127B）必须保持 green；`Module/system_prompt` 的 Elysia 16-section 渲染输出不因删字段变化（`prompt_assembler` 的 `user_pronoun` 来源已是 `_turn_context.userPronoun`，不读 config.yaml）；
- **Cyrene 渲染不变**：Cyrene 的 `{{ user_pronoun }}` 已渲染自 `UserIdentity.pronouns="她"`，删 `cyrene/config.yaml` 的 `user_pronoun: 你` 不改变实际输出（该字段自阶段 C+E 起已是死字段）。

---

## 10. frontend_contract 补齐

### 10.1 本批新增 DTO

```python
# rest.py 新增
class CharacterListResponse(ContractModel):
    items: list[CharacterSummary]
    has_more: bool = Field(default=False, description="是否还有更多")
    cursor: Optional[str] = Field(default=None, description="不透明游标，客户端不得解析")

class CharacterRevisionSummary(ContractModel):
    characterId: str
    revision: int = Field(default=1, ge=1)
    displayName: str
    createdAt: str
    createdBy: str = "system"

class CharacterRevisionResponse(ContractModel):
    """当前 revision 完整详情（契约层独立类型，不引用 core.CharacterRevision）。"""
    characterId: str
    revision: int = Field(default=1, ge=1)
    schemaVersion: int = Field(default=1, ge=1)
    displayName: str
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    promptProfile: dict[str, Any] = Field(default_factory=dict)  # 含 corePersonality/speakingStyle/examples
    runtimeDefaults: dict[str, Any] = Field(default_factory=dict)
    presentation: dict[str, Any] = Field(default_factory=dict)
    openingMessage: str | None = None
    checksum: str = ""
    compilerVersion: int = Field(default=1, ge=1)
    createdBy: str = "system"
    createdAt: str

class CharacterDetail(ContractModel):
    summary: CharacterSummary
    current: CharacterRevisionResponse  # 当前 revision 完整详情

class CharacterStateImpact(ContractModel):
    statePolicy: str = "preserve"  # 唯一取值 preserve|reset_evolution（无缩写 reset）
    affectedInstances: int = 0
    pendingInstances: int = 0
    resetFields: list[str] = Field(default_factory=list)

class CharacterMutationResult(ContractModel):
    character: CharacterDetail
    stateImpact: CharacterStateImpact

class CharacterValidationIssue(ContractModel):
    severity: str  # error|warning
    code: str
    path: str
    message: str

class CharacterValidationResult(ContractModel):
    valid: bool
    issues: list[CharacterValidationIssue]
    estimatedPromptChars: Optional[int] = None

class CharacterRevisionListResponse(ContractModel):
    items: list[CharacterRevisionSummary]
    has_more: bool = Field(default=False, description="是否还有更多")
    cursor: Optional[str] = Field(default=None, description="不透明游标，客户端不得解析")

class CharacterArchiveResult(ContractModel):
    character: CharacterSummary
    userProfile: Optional[UserIdentityProfile] = Field(default=None, description="归档默认角色时返回新档案")
```

### 10.2 不新增（本批不做）

- `CharacterPreviewResult` —— preview 留本阶段末/E；
- `ResetCharacterEvolutionResult` —— reset-evolution 阶段 G；
- `CharacterStateEvent`（§12.4）—— 阶段 G。

### 10.3 feature flag

`character_profiles=false` 已在 `ServerInfoFeatures`（阶段 C+E）。本批**不改**该值；`GET /api/characters` 等在 wire 上响应角色字段，但 `character_profiles=false` 时前端可忽略。**本批的关键边界：`GET /api/characters` 返回 custom + builtin，但 custom 不参与 runtime 聊天**。

---

## 11. 测试策略

### 11.1 单元测试

- `CharacterDraft` 字段长度、Unicode、reserved tags、路径校验（§18.1）；
- `ConversationExample` 各 ≤1000 字符；
- `CharacterService.create/get/list/update/clone/archive/rollback` 各分支；
- revision append-only/rollback（复制旧版本为新 revision，不删历史）；
- expectedRevision/stateVersion 冲突（CAS）；
- `CharacterRepository` CAS `filter` 原子性（`modified_count>0` 命中判定；`updated_at` 必变规避 no-op）→ 未命中 → `RevisionConflict`；
- built-in immutable（编辑/归档内置抛 `CharacterImmutableError`）。

### 11.2 prompt 测试

- Elysia 迁移前后关键行为 case 不回退；
- 自定义角色 A/B 的 authored 内容互不出现；
- `test_elysia_snapshot_unchanged` 保持 green。

### 11.3 repository 与迁移测试

- 所有 unique/index 创建；
- update CAS 原子性；
- custom owner 过滤；
- built-in/custom ID 冲突拒绝；
- Mongo 离线：cached custom 可读、不可写；uncached 不冒充其他角色。

---

## 12. 阶段 D 明确排除的后续（预告）

- **阶段 E**：CharacterRuntime instance-scoped、去除 attach_session、session create + binding + summary、generation/message 角色审计字段、旧 session state shadow migration；
- **阶段 F**：记忆 v2 隔离（MemoryScope/MemoryView）；
- **阶段 G**：产品状态与 WS（CharacterPublicState REST、character.state.changed、expression/generation identity）；
- **阶段 H**：迁移、文档与联调、frontend check-types、feature flag 最后开启。

---

## 13. 关键不变式总结

| # | 不变式 | 验证方式 |
|---|--------|---------|
| 1 | Elysia byte-for-byte 不变 | `test_elysia_snapshot_unchanged`（18127B）green |
| 2 | CAS 原子性（不用读-改-写） | `cas_revision_pointer` 用 `filter={"character_id","current_revision"}`（snake_case）+ `$set`（恒定含必变字段 `updated_at`）；`modified_count>0` 判定命中（算法 `updated_at` 必变规避 no-op 误判，后端不暴露 `matched_count`）；未命中 → `RevisionConflict` |
| 3 | append-only 版本 | rollback 复制旧版本为新 revision，不删历史 |
| 4 | 写序 Revision-first（§10.1） | update/rollback 先 `append_revision` 再 `cas_revision_pointer`；CAS 失败回滚已 append revision——`current_revision` 永远指向 durable revision |
| 5 | custom 不参与聊天 | `character_profiles=false`，custom 只能被 CRUD |
| 6 | `CharacterConfig.user_pronoun` 彻底移除 | grep 无生产消费点；sections 回退链 + 模板占位符保留 |
| 7 | 离线 fail-closed | `StorageUnavailableError` → `503 CHARACTER_STORAGE_UNAVAILABLE`；已缓存读、不可写 |

（注：删除编号 4「custom 不参与聊天」原为 4，现顺延为 5；原 5「user_pronoun 移除」顺延为 6；原 6「离线 fail-closed」顺延为 7；新增写序不变式为 4。）
