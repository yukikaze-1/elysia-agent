# core/prompt/ — System Prompt 组装层

> 文档导航：[项目文档](../../README.md) → [Core 文档](../README.md)

创建 12 个 PromptSection、组装 LLM `messages[]` 的应用级胶水层。CORE-MOD-P1-02 从 `core/adapter` 迁入：这些组件依赖 CharacterEngine / SessionMemory / LongTermMemory，不属于后端适配器。

## 文件布局

```
prompt/
├── __init__.py              # 重导出 MessageBuilder / PromptAssembler
├── message_builder.py       # 纯 prompt 组装：system prompt + history → messages[]
├── prompt_assembler.py      # 12 PromptSection → SystemPromptManager → MessageBuilder
└── tests/
    ├── test_message_builder.py
    └── test_prompt_assembler.py
```

## 组件

### MessageBuilder

- 无状态，仅依赖 `core.schema`。
- `build_messages()` — 多模态 content、图片 fallback 提示、tool-call 摘要注入。

### PromptAssembler

- 按固定顺序创建 12 个 section（F1 → F3 → C2 → C7 → C6 → C1 → C9 → C8 → C3 → C4 → C5）。
- `_refresh_personality_data()` 从 CharacterEngine 拉取 C1/C9 数据。
- `set_session()` 在会话切换时重绑 session 引用。

## 依赖方向

`core.prompt → Module.system_prompt + core.config + core.memory + core.personality(TYPE_CHECKING) + core.schema`。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [core/adapter/README.md](../adapter/README.md) | 后端适配器（LLM/TTS/Tool） |
| [Module/system_prompt/README.md](../../Module/system_prompt/README.md) | System prompt section 模板与渲染 |
| [docs/architecture.md](../../docs/architecture.md) | 三层架构与数据流 |
