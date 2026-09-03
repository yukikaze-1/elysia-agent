# Profile — 用户最小身份（UserIdentity）

> 文档导航：[项目文档](README.md) → [Core 层](../README.md)

用户「最小身份」模型 + 持久化。架构稿决策 #7：『我』的最小身份是角色功能前置依赖。首版不做完整用户画像，但 Prompt 所需的身份（代词/显示名）从角色/全局配置移出到 `UserIdentity`。

**阶段：** M1/M2（阶段 `stage-ce` + `stage-d`）。`user_pronoun` 从 `CharacterConfig.user_pronoun`（错放）**正位**到 `UserIdentity.pronouns`（B1 决断：全局唯一「她」，非 per-role）；`stage-d` 给 `UserIdentityStore` 加 CAS `update_if_revision`（PUT /api/me 支撑）并**彻底移除** `CharacterConfig.user_pronoun`（B1 正位闭环）。

## 文件布局

```
core/profile/
├── user_identity.py      # UserIdentity — 用户最小身份 Pydantic 模型
├── profile_store.py      # UserIdentityStore — bootstrap + get/save（内存 + JSONL fallback）
└── tests/
    ├── test_user_identity.py
    └── test_profile_store.py
```

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

`bootstrap(user_name)` 无 profile 时用 `config.agent.user_name` 建 `initialized=false` profile（幂等）；`get()` / `save()`。无 Mongo 时降级内存 + JSONL file fallback（架构稿 §5.1）。Mongo profile 就绪后为权威（阶段 H）。

**CAS（阶段 D，PUT /api/me 支撑）**：`update_if_revision(expected_revision, updates)` — 仅当当前 `revision == expected_revision` 时应用 updates 白名单字段（displayName/preferredAddress/pronouns/locale/timezone/defaultCharacterId/initialized）并 `touch()`（revision++）；冲突返回 `None`（调用方映射 `USER_PROFILE_REVISION_CONFLICT`/409）。单进程用内存锁 + revision 比对实现原子 CAS（JSONL 单文件，无并发进程写）。

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
