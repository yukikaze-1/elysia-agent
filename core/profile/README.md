# Profile — 用户最小身份（UserIdentity）

> 文档导航：[项目文档](README.md) → [Core 层](../README.md)

用户「最小身份」模型 + 持久化。架构稿决策 #7：『我』的最小身份是角色功能前置依赖。首版不做完整用户画像，但 Prompt 所需的身份（代词/显示名）从角色/全局配置移出到 `UserIdentity`。

**阶段：** M1/M2（阶段 `stage-ce` + `stage-d`）+ 阶段 H。`user_pronoun` 从 `CharacterConfig.user_pronoun`（错放）**正位**到 `UserIdentity.pronouns`（B1 决断：全局唯一「她」，非 per-role）；`stage-d` 给 `UserIdentityStore` 加 CAS `update_if_revision`（PUT /api/me 支撑）并**彻底移除** `CharacterConfig.user_pronoun`（B1 正位闭环）；**阶段 H（BE-STAGEH-02）**把 Mongo 变成权威事实源——重启后端不再回到 bootstrap。

## 文件布局

```
core/profile/
├── user_identity.py      # UserIdentity — 用户最小身份 Pydantic 模型
├── profile_store.py      # UserIdentityStore — Mongo 权威 + 进程内读缓存（无 repo 时内存 + JSON 文件降级）
└── tests/
    ├── test_user_identity.py
    └── test_profile_store.py
```

持久化落在 `core/storage/user_identity_repo.py::UserIdentityRepository`（`user_identity_profiles` 集合）。

## 核心概念

### UserIdentity（架构稿 §5.1）

| 字段 | 说明 |
|------|------|
| `userId` | 本地用户稳定 ID，首版 `usr_local_default` |
| `displayName` | 角色知道的用户名字（来源 `config.agent.user_name` bootstrap） |
| `preferredAddress` | 希望角色通常如何称呼；空用 displayName |
| `pronouns` | **用户代词，全局唯一**（B1：恒「她」，非 per-role）——可填模板 `{{ user_pronoun }}` |
| `initialized` | **仅 UX 标记**（用户是否已确认），不 gate pronouns |
| `revision` | 乐观并发版本，从 1 起 |

`touch()` 写前调用：`updatedAt` 刷新 + `revision++`。

### UserIdentityStore

**Mongo 权威（阶段 H / BE-STAGEH-02）**：`UserIdentityRepository` 的 `user_identity_profiles` 集合是唯一持久化事实源；`UserIdentityStore` 只持有进程内**读缓存**，供同步读路径 `get()` 使用。生命周期：`Agent.__init__` 装配 repo + `placeholder()` 占位身份（固定单用户 `usr_local_default`，**不缓存不落盘**），`Agent.start()` 经 `_start_persistence` 调 `connect(user_name)` 装载/原子 bootstrap 覆盖缓存，`_stop_persistence` 关闭连接。

- `connect(user_name)`：repo 就绪 → 命中即装载；空库 → `bootstrap_if_absent`（`$setOnInsert`，并发启动只有一个 winner）；**repo 不可用 → 返回 `None` 并清空缓存（fail-closed，不伪造本地档案）**。
- `get()`：同步读缓存。权威模式下**只信 Mongo 装载结果**——绝不回退 JSON 文件或内存伪造（P0-3）。
- `is_authoritative` / `is_available`：是否装配权威 repo / 权威身份是否已装载可用（供业务 fail-closed 判定）。
- `update_if_revision(expected_revision, updates)`（**async**）：repo 已装配时走 Mongo 原子 CAS；权威身份不可用 → **fail-closed**（`StorageUnavailableError` → 503 `STORAGE_UNAVAILABLE`），绝不悄悄写本地造成「档案已更新」的假象。无 repo 装配（单测/显式本地模式）时降级内存 + JSON 文件 CAS。
- `health_check()`：权威 repo 可达性探针（无 repo 恒 True）——由 `_db_health_loop` 的 `identity` 探针调用，Mongo 恢复时触发 `_recover_dependency("identity")` 重新装载（不覆盖 Mongo 中更高 revision）。

**P0-3 fail-closed 边界**：权威身份不可用时，`/api/me` 读写 503、preview 503、会话创建不静默回退内置角色、generation 以 `STORAGE_UNAVAILABLE` 失败；`/ready` 的 `chatReady` 要求 `dependencies.identity=true`。

**CAS 语义（阶段 D，PUT /api/me 支撑）**：仅当当前 `revision == expected_revision` 时应用 updates 白名单字段（displayName/preferredAddress/pronouns/locale/timezone/defaultCharacterId/initialized）并递增 revision；冲突返回 `None`（调用方映射 `USER_PROFILE_REVISION_CONFLICT`/409）。`initialized` 只能置 true。

**P0-2 消费方**：`CognitiveLoop._prepare_turn` 每轮 `get()` 一次完整快照，冻结进 `TurnCharacterContext`（`userDisplayName`/`userPreferredAddress`/`userPronouns`/`userLocale`/`userTimezone`/`userIdentityRevision`）；Prompt 的称呼（`preferredAddress` 优先，否则 `displayName`）、代词与时区一律取自该快照，不再读 `config.agent.user_name`。

### B1 决断（user_pronoun 正位）

`pronouns` 是用户代词，**全局唯一**。Elysia 配「她」、Cyrene 配「你」是 B-2 遗留错位——用户是同一人（妖梦），代词只有一个「她」。**正位修复**：Elysia byte 不变，Cyrene 从「你」修正为「她」（意图修复，非回归）。**`CharacterConfig.user_pronoun` 已在阶段 D 彻底移除**（字段 + config.yaml + fixtures 删除）——由 `UserIdentity.pronouns` 唯一权威（B1 正位闭环）。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../README.md) | 快速开始 + 完整文档导航 |
| [Core 层](../README.md) | Core 层总览 |
| [Character 包](../character/README.md) | 角色域领域模型 + 运行时 |
| [架构稿](../../docs/2026-08-30-character-prompt-customization-backend-architecture-design.md) | 决策 #7 / §5.1 UserIdentityProfile |
