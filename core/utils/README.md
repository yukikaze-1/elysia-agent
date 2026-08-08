# core/utils/ — 通用工具

> 文档导航：[项目文档](../../README.md) · [core/ 总览](../README.md)

轻量级通用函数，无状态、无异步依赖。

## 文件布局

```
core/utils/
├── __init__.py       # 重导出
├── message_id.py     # 消息 ID 生成
├── log_setup.py      # 日志配置
└── text_utils.py     # 文本提取（自 core/text 迁入）
```

## 组件

### message_id.py（45 行）

ID 生成 helper 族（core ID NewType 权威生成处）：
- `new_message_id()` — `msg_` + 26 位 hex UUID4。被 `cognitive/loop.py` 等使用。
- `new_generation_id()` / `new_segment_id()` — `gen_` / `seg_` 前缀，同上模式。
- `new_attachment_id()` / `new_memory_id()` / `new_task_id()` — `att_` / `msg_` 前缀（2026-08-09 新增，衍生 ID 独立 NewType）。分别被 `input/api_routes.py`（附件）、`memory/long_term.py`（记忆条目）、`task/task.py`（调度任务）使用。

### text_utils.py（42 行）

`content_text_from_content(content)` — 从多模态 content 数组提取纯文本（None → ""、str → 原样、list[dict] → 提取 text 类型）；`MessageObj` — 轻量 assistant 消息对象（role + content + to_dict）。由 `cognitive/loop.py`、`adapter/prompt_assembler.py` 使用。2026-08-01 自 `core/text/` 迁入（与流式解析解耦）。

### log_setup.py（215 行）

日志系统配置：
- stderr + `log/app.log`（INFO+）+ `log/app-debug.log`（DEBUG+，带轮转）
- `log/conversation.log`（对话记录）
- `log/mongo.log` / `log/milvus.log` / `log/http.log` / `log/embedding.log`（库日志分流：pymongo/pymilvus → mongo/milvus，httpx/httpcore/openai/urllib3 → http，sentence_transformers/transformers → embedding）
- `set_request_id()` / `get_request_id()` — ContextVar 请求追踪
- `log_conversation()` — 对话日志

由 `app.py` 的 `setup_logging()` 在应用启动时配置。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [core/README.md](../README.md) | Core 层总览 |
| [log/README.md](../../log/README.md) | 日志文件说明 |
