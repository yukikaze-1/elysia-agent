# core/schema — 系统基础类型定义层

> 文档导航：[项目文档](../../README.md)

core/ 层共享的基础类型：Agent 状态机、事件类型、多模态消息（Pydantic BaseModel + discriminated union）与格式适配器。

## 架构 / 数据流

```
core.schema（统一入口，__init__.py re-export 全部类型）
├── agent.py     # AgentState, VALID_TRANSITIONS, EventPriority, EventType — FSM/EventBus 类型
├── types.py     # IsoTimestamp, now_iso, SessionId, MessageId, ToolCallId — 时间戳/标识符别名
├── utils.py     # now_iso, _normalize_iso_ts, _validate_src — 时间戳/校验工具函数
├── part.py      # TextPart / ImagePart / FilePart / AudioPart / VideoPart → Part（discriminated union）
├── tool.py      # ToolCall, ToolCallFunction — OpenAI 兼容 tool_calls 结构
├── event.py    # EventData union + 5 种事件 data 模型（EventBus Pydantic 类型）
├── memory.py   # MemoryType, ExtractedMemory, CompareAction — Consolidator LLM 输出类型
├── message.py   # UserMessage / AssistantMessage / ToolResultMessage → Message（按 role 拆分）
├── token.py     # TokenUsage + TokenUsageSummary — Token 用量记录
├── character.py # CharacterState — CharacterEngine 性格状态
├── mood.py     # STT emoji 工具函数（stt_emotion_emoji, stt_event_emoji）
├── memory_reflector.py # MemoryReflectorState + ReflectorActions — 状态 + LLM 输出类型
├── compression.py  # CompressionTrigger + VersionEntry — L3→L2 压缩版本管理类型
├── error_type.py   # ErrorType — 工具错误分类（从 Module/_common 重导出）
└── adapters/
    └── message_adapter.py  # parts_from_dicts, content_to_openai, content_text 等纯函数适配器
```

数据流：外部数据（session 存储 / WebSocket 请求）是 `list[dict]`，在边界处用 `parts_from_dicts()` 转为 `list[Part]`；内部逻辑只处理 Part；发往 LLM 前用 `content_to_openai()` 转为 OpenAI multimodal 格式。

## 关键机制

- **统一入口**：`from core.schema import AgentState, UserMessage, ...` —— `__init__.py` re-export 所有子模块类型；adapters 函数按需从 `core.schema.adapters.message_adapter` 显式 import。
- **Discriminated union**：`Part` 按 `type` 字段自动区分子类（Pydantic `Field(discriminator="type")`），`TypeAdapter` 完成 dict ↔ Part 校验转换。
- **frozen role**：各消息类的 `role` 字段 frozen，构造时自动填值，防止误改。
- **统一类型系统**：`core/schema` 是项目唯一的共享类型定义，已替代废弃的 `Module/schema/`。

## 模块速查

| 文件 | 内容 |
|------|------|
| `agent.py` | AgentState, EventType, EventPriority, VALID_TRANSITIONS — FSM/EventBus 类型 |
| `types.py` | IsoTimestamp, now_iso, SessionId, MessageId, ToolCallId — 时间戳/标识符别名 |
| `part.py` | TextPart / ImagePart / FilePart / AudioPart / VideoPart — Part（discriminated union） |
| `tool.py` | ToolCall, ToolCallFunction — OpenAI 兼容 tool_calls 结构 |
| `memory.py` | MemoryType, ExtractedMemory, SkipAction, InsertAction, UpdateAction, MergeAction, CompareAction — MemoryConsolidator LLM 输出类型 |
| `event.py` | EventData, InterruptData/StateChangeData/UserInputData/SystemTickData/TaskResultData — EventBus 事件 data 类型 |
| `message.py` | UserMessage / AssistantMessage / ToolResultMessage / ClientUserMessage / ClientAssistantMessage / ClientToolMessage / ClientMessageDict — Message（按 role 拆分）+ 客户端 TypedDict |
| `token.py` | TokenUsage / TokenUsageSummary — Token 用量记录 |
| `character.py` | CharacterState — CharacterEngine 性格状态 |
| `mood.py` | STT emoji 工具函数（stt_emotion_emoji, stt_event_emoji）|
| `memory_reflector.py` | MemoryReflectorState + MergeItem/UpdateItem/DeleteItem/ReflectorActions — 状态 + MemoryReflector LLM 输出类型 |
| `compression.py` | CompressionTrigger + VersionEntry — L3→L2 压缩版本管理类型 |
| `error_type.py` | ErrorType — 工具错误分类（从 Module/_common 重导出） |
| `utils.py` | now_iso, _normalize_iso_ts, _validate_src — 时间戳工厂/校验工具 |

测试位于 `core/schema/tests/`（12 个文件：test_types / test_part / test_tool / test_message / test_adapters / test_character / test_compression / test_event / test_memory / test_memory_reflector / test_message_degradation / test_token）。

## 测试

```bash
python -m pytest core/schema/tests/ -v
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../README.md) | 快速开始 + 文档导航 |
| [core/README.md](../README.md) | 认知中枢架构 — FSM/EventBus 如何消费这些类型 |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 |
