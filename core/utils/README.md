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

### message_id.py（11 行）

`new_message_id()` — 返回 `msg_` + 26 位 hex UUID4。被 `task/task.py`、`cognitive/loop.py`、`memory/long_term.py` 等多处使用。

### text_utils.py（42 行）

`content_text_from_content(content)` — 从多模态 content 数组提取纯文本（None → ""、str → 原样、list[dict] → 提取 text 类型）；`MessageObj` — 轻量 assistant 消息对象（role + content + to_dict）。由 `cognitive/loop.py`、`adapter/prompt_assembler.py` 使用。2026-08-01 自 `core/text/` 迁入（与流式解析解耦）。

### log_setup.py（~150 行）

日志系统配置：
- stderr + `log/app.log`（INFO+）+ `log/app-debug.log`（DEBUG+，带轮转）
- `log/conversation.log`（对话记录）
- `log/mongo.log` / `log/milvus.log`（库日志分流）
- `set_request_id()` / `get_request_id()` — ContextVar 请求追踪
- `log_conversation()` — 对话日志

由 `app.py` 的 `setup_logging()` 在应用启动时配置。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [core/README.md](../README.md) | Core 层总览 |
| [log/README.md](../../log/README.md) | 日志文件说明 |
