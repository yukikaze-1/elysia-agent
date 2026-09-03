# Character — 角色域（领域模型 + 运行时容器）

> 文档导航：[项目文档](README.md) → [Core 层](../README.md)

角色域的领域模型 + 多角色运行时容器骨架。渐进式——在现有 `character/{name}/config.yaml` + `personality.md` 之上**加一层薄壳**（Pydantic 模型），不重写 manifest v2。

**阶段：** M1/M2（本批 `stage-ce`）。engine 仍为 **全局单例**（batch-3 现状），本包提供领域模型 + 运行时容器骨架，**多 instance engine（M3）不在本批**。

## 文件布局

```
core/character/
├── definition.py         # CharacterDefinition — 稳定角色身份（架构稿 §5.2）
├── revision.py           # CharacterRevision — 不可变角色设定快照（§5.3）
├── instance.py           # CharacterInstance — 用户正在相处并持续演化体（§5）
├── builtin_source.py     # BuiltinCharacterSource — 从 character/{name}/config.yaml 构建内置角色
├── catalog.py            # CharacterCatalog — 只读内置角色目录（无 Mongo 也能列/校验）
├── runtime.py            # CharacterRuntime — 单实例运行时容器（快照 + 懒 engine + lock/dirty）
├── registry.py           # CharacterRuntimeRegistry — LRU 缓存 + invalidate/release/close_all
├── migration.py          # 旧会话 shadow migration（legacy instance，架构稿 §15.3）
└── tests/
    ├── test_models.py
    ├── test_builtin_source.py
    ├── test_runtime.py
    ├── test_registry.py
    └── test_migration.py
```

## 核心概念

### CharacterDefinition / CharacterRevision / CharacterInstance（架构稿 §5）

- **Definition**：稳定角色身份（characterId/slug/source/status），不存 Prompt 正文/状态/session。
- **Revision**：不可变角色设定快照（displayName/promptProfile/runtimeDefaults/checksum）。本批 `promptProfile` 只承载有源字段（`corePersonality`/`speakingStyle`）；`relationshipStyle`/`emotionStyle`/`conversationInitiative` 本批显式留空（渲染走模板）。
- **Instance**：用户正在相处并持续演化的角色（instanceId/characterId/revision/stateVersion）。

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
