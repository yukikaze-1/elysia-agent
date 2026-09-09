# Profile — 用户最小身份（UserIdentity）

> 文档导航：[项目文档](README.md) → [Core 层](../README.md)

用户「最小身份」模型 + 持久化。架构稿决策 #7：『我』的最小身份是角色功能前置依赖。首版不做完整用户画像，但 Prompt 所需的身份（代词/显示名）从角色/全局配置移出到 `UserIdentity`。

**阶段：** M1/M2（阶段 `stage-ce` + `stage-d`）+ 阶段 H。`user_pronoun` 从 `CharacterConfig.user_pronoun`（错放）**正位**到 `UserIdentity.pronouns`（B1 决断：全局唯一「她」，非 per-role）；`stage-d` 给 `UserIdentityStore` 加 CAS `update_if_revision`（PUT /api/me 支撑）并**彻底移除** `CharacterConfig.user_pronoun`（B1 正位闭环）；**阶段 H（BE-STAGEH-02）**把 Mongo 变成权威事实源——重启后端不再回到 bootstrap。

## 文件布局

```
core/profile/
├── user_identity.py      # UserIdentity — 用户最小身份 Pydantic 模型
├── profile_store.py      # UserIdentityStore — Mongo 权威 + 进程内读缓存（无 repo 时内存 + JSONL 降级）
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

**Mongo 权威（阶段 H / BE-STAGEH-02）**：`UserIdentityRepository` 的 `user_identity_profiles` 集合是唯一持久化事实源；`UserIdentityStore` 只持有进程内**读缓存**，供同步读路径 `get()` 使用。生命周期：`Agent.__init__` 装配 repo + 本地 bootstrap（保证构造期同步可读），`Agent.start()` 经 `_start_persistence` 调 `connect(user_name)` 装载/原子 bootstrap 覆盖缓存，`_stop_persistence` 关闭连接。

- `connect(user_name)`：repo 就绪 → `get()` 命中即装载；空库 → `bootstrap_if_absent`（`$setOnInsert`，并发启动只有一个 winner）；repo 不可用 → 降级本地并记 warning。
- `get()`：同步读缓存（重启后由 `connect()` 填充）。
- `update_if_revision(expected_revision, updates)`（**async**）：repo 已装配时走 Mongo 原子 CAS；repo 存在但未连接 → **fail-closed**（`StorageUnavailableError` → 503 `STORAGE_UNAVAILABLE`），绝不悄悄写本地造成「档案已更新」的假象。无 repo 装配（单测/纯本地）时降级内存 + JSONL CAS。

**CAS 语义（阶段 D，PUT /api/me 支撑）**：仅当当前 `revision == expected_revision` 时应用 updates 白名单字段（displayName/preferredAddress/pronouns/locale/timezone/defaultCharacterId/initialized）并递增 revision；冲突返回 `None`（调用方映射 `USER_PROFILE_REVISION_CONFLICT`/409）。`initialized` 只能置 true。

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
