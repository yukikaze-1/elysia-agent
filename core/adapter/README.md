# core/adapter/ — 适配器层

> 文档导航：[项目文档](../../README.md) · [core/ 总览](../README.md)

封装 `Module/` 后端，为 `core/` 层提供统一接口。每个适配器在 `Module/` 的基础能力之上增加 core 领域语义（重试、热切换、门控播放、生命周期封装）。

## 文件布局

```
core/adapter/
├── __init__.py              # 重导出 + ToolResult/ErrorType/ToolContext
├── llm_adapter.py           # LLM 流式适配器
├── tts_adapter.py           # TTS 流式管理器
├── tool_adapter.py          # 工具子系统适配器
├── tool_call_parser.py      # 流式 tool_call 合并/归一化
└── tests/
    ├── test_llm_adapter.py
    ├── test_tool_adapter.py
    ├── test_tts_adapter.py
    └── test_tts_adapter_control.py
```

## 组件

### LLMAdapter（`llm_adapter.py`，219 行）

封装 `Module/llm`：

- `stream_think()` — 结构化流式输出 `{content, tool_calls}` chunks
- 自动重试（429/500/502/503/504，指数退避 3 次）
- `switch_model()` — 热切换底层 LLM 后端（先建后换，create 失败保留旧后端 + `_requested_model`）
- `get_raw_client()` — 暴露底层 HTTP client（供 WorkflowEngine 直接使用）
- 非流式 tool-call JSON 解析兜底

### TTSStreamManager（`tts_adapter.py`，496 行）

封装 `Module/tts` + `Module/output`：

- 句级门控播放 — workers=1 FIFO 保证句间顺序
- emoji 绑定 — emoji-only 句排队，依附下一 TTS 句分发
- `set_emotion()` — mood 文本 → TTS 情感参数映射
- 文本预处理 — 暂停标签剥离、中文动作提示词过滤
- Phase 1 下载 + Phase 2 串行门控播放
- **`async set_enabled()`** — 后端模式开关：禁用时取消在途任务并 per-task `flush_on_cancel` 兜底广播未播句文本（中途关 TTS 不丢句）+ drain 后 flush emoji；`enabled` 独立于 `active`（后端可用性）

### ToolAdapter（`tool_adapter.py`，196 行）

封装 `Module/tool` 工具子系统生命周期：

- 实现 `ToolExecutor` Protocol（execute / definitions / cancel_all 等）
- 管理 `ToolManager` + `PluginContext` + `ToolContext` 创建和关闭
- 类型重导出：`ToolResult`、`ErrorType`、`ToolContext`

### tool_call_parser.py（75 行）

流式 tool_call 处理：

- `merge_stream_tool_calls()` — 增量合并流式 tool_call delta
- `normalize_tool_calls()` — 过滤不完整调用 → `ToolCall` Pydantic 模型

## 已迁出（CORE-MOD-P1-02）

- `MessageBuilder` → `core/prompt/message_builder.py`
- `PromptAssembler` → `core/prompt/prompt_assembler.py`

适配器层只保留对 `Module/` 后端的封装。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [core/README.md](../README.md) | Core 层总览 |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 |
