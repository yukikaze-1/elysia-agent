# Character — 角色域（领域模型 + 运行时容器）

> 文档导航：[项目文档](README.md) → [Core 层](../README.md)

角色域的领域模型 + 多角色运行时容器骨架。渐进式——在现有 `character/{name}/config.yaml` + `personality.md` 之上**加一层薄壳**（Pydantic 模型），不重写 manifest v2。

**阶段：** M1/M2（阶段 `stage-ce` + `stage-d`）。`stage-ce` 建领域模型 + 运行时容器骨架，engine 仍为**全局单例**；`stage-d` 新增 Custom Character CRUD（`CharacterDraft`/`CharacterService`/持久化仓储 + REST `character_routes`），但 `character_profiles=false` 恒抑制，custom 不参与真实聊天。**多 instance engine（M3/阶段 E）不在本批**。

## 文件布局

```
core/character/
├── definition.py         # CharacterDefinition — 稳定角色身份（架构稿 §5.2）
├── revision.py           # CharacterRevision — 不可变角色设定快照（§5.3）
├── instance.py           # CharacterInstance — 用户正在相处并持续演化体（§5）
├── draft.py              # CharacterDraft + ConversationExample — 用户提交草稿（双层校验，阶段 D）
├── service.py            # CharacterService — CRUD 业务编排 + CAS 冲突（阶段 D）
├── builtin_source.py     # BuiltinCharacterSource — 从 character/{name}/config.yaml 构建内置角色
├── catalog.py            # CharacterCatalog — 只读内置角色目录（无 Mongo 也能列/校验）+ get_revision
├── runtime.py            # CharacterRuntime — 单实例运行时容器（快照 + 懒 engine + lock/dirty）
├── registry.py           # CharacterRuntimeRegistry — LRU 缓存 + invalidate/release/close_all
├── migration.py          # 旧会话 shadow migration（legacy instance，架构稿 §15.3）
└── tests/
    ├── test_models.py
    ├── test_builtin_source.py
    ├── test_draft.py
    ├── test_service.py
    ├── test_runtime.py
    ├── test_registry.py
    └── test_migration.py
```

## 核心概念

### CharacterDefinition / CharacterRevision / CharacterInstance（架构稿 §5）

- **Definition**：稳定角色身份（characterId/slug/source/status），不存 Prompt 正文/状态/session。
- **Revision**：不可变角色设定快照（displayName/promptProfile/runtimeDefaults/checksum）。本批 `promptProfile` 承载有源字段（`corePersonality`/`speakingStyle`/`relationshipStyle`/`emotionStyle`/`conversationInitiative`/`examples`）；内置角色仍只填 `corePersonality`/`speakingStyle`（其余渲染走模板）。
- **Instance**：用户正在相处并持续演化的角色（instanceId/characterId/revision/stateVersion）。

### CharacterDraft + CharacterService（阶段 D：Custom CRUD）

- **CharacterDraft**（[draft.py](draft.py)）：用户提交的完整角色表单（custom 唯一入口）。双层校验：第一层 Pydantic 字段边界（displayName 1-64 等，§8.1）；第二层 `validate_semantic`（保留标签 `<conflict_rules>` 等 / Jinja 分隔符 / NUL → hard error；关键字启发式 → warning）。`examples` 用 `ConversationExample` 子对象（每组 user/assistant ≤1000 字符）。
- **CharacterService**（[service.py](service.py)）：create/get/list/update/clone/archive/rollback/validate 业务编排。**Revision-first 写序**（先 `append_revision` 再 `cas_revision_pointer`，CAS 失败回滚已 append revision——`current_revision` 永远指向 durable revision，架构稿 §10.1）；`create`/`update`/`rollback` 均产生新 Revision（append-only，不删历史）；`list`/`get` 合并内置（catalog）与 custom（repo）。
- **CharacterRepository / CharacterRevisionRepository**（[core/storage](../../storage/README.md)）：`characters` + `character_revisions` 集合。snake_case 落库（`character_id`/`current_revision`），CAS 用 `modified_count>0` + `$set` 恒定含必变字段 `updated_at`（后端只暴露 `modified_count`，对齐 `idempotency_repo.py` 先例）。
- **border**：`character_profiles=false` 恒抑制（决策 #12）——custom 本批只被 CRUD，不参与真实聊天（阶段 E/H 开启）。

### BuiltinCharacterSource（渐进式）

从现有 `character/{name}/config.yaml` + `personality.md` 构建内置角色（`corePersonality` 从 personality.md、`speakingStyle` 从 config personality.speaking_style），**不重写 manifest v2**。

### CharacterRuntimeRegistry（架构稿 §7.1）

`get(instance_id, factory)` 按 instance_id 解析 runtime；`invalidate`/`release`/`flush_all`/`close_all`。LRU 缓存，驱逐前检查 dirty/busy/session-liveness（M2 耦合，本批为骨架占位）。**M1/M2 不驱动多 instance engine**（runtime engine 懒建但保持单例）。

### 旧会话 shadow migration（架构稿 §15.3）

每个旧 session 建确定性 legacy instance（sessionId 哈希），`character_binding` 指向它；旧会话行为/状态不变。迁移期 legacy `character_state` 仍权威（M1），M3 才切换。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../README.md) | 快速开始 + 完整文档导航 |
| [Core 层](../README.md) | Core 层总览 |
| [Profile 包](../profile/README.md) | UserIdentity 用户最小身份 |
| [架构稿](../../docs/2026-08-30-character-prompt-customization-backend-architecture-design.md) | 角色域完整设计（§5 领域模型 / §7 运行时 / §15 迁移） |
