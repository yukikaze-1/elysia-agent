# 消息存储格式与历史恢复设计

## 概述

当前消息存储格式为 `{role, content}` 的简单键值对，AI 回复的全文一次性存入 `content`。存在两个问题：

1. **前端刷新丢 AI 消息**：`appendDelta()` 只操作 DOM，不写 `state.messages`，localStorage 只存了用户消息
2. **格式过于扁平**：无法区分纯文本、markdown、代码块，也不支持消息级别的 ID/时间戳

本次设计引入 `parts` 数组作为消息内容的唯一数据结构，服务端为数据源（WS 连接时推送 session），localStorage 降级为缓存。

## 新消息格式

### 基础结构

```json
{
  "id": "msg_<uuid7>",
  "role": "user|assistant",
  "parts": [
    {"content": "text or markdown or code"},
    {"content": "code block", "language": "python"}
  ],
  "content_type": {"markdown": [1], "code": [2, 5]},
  "timestamp": "2026-06-03T23:15:00.000+08:00"
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | `string` | 全局唯一消息 ID（uuid7，时间有序），用于去重、引用、操作 |
| `role` | `string` | `"user"` 或 `"assistant"` |
| `parts` | `array` | 有序数组，每个元素是一个展示单元（句子/代码块/markdown块） |
| `content_type` | `object` | 类型 → 索引数组的映射。不在映射中的索引默认为 `text` |
| `timestamp` | `string` | ISO 8601 带时区，消息创建时间 |

### part 类型

| 类型 | 说明 | 是否 TTS | 前端渲染 |
|------|------|----------|----------|
| `text`（默认） | 纯文本句子，不在 content_type 中即为 text | 是 | 纯文本气泡 |
| `markdown` | markdown 格式文本块 | 否 | markdown 渲染（列表、加粗、表格等） |
| `code` | 代码块 | 否 | 语法高亮 + 复制按钮 + 语言标签 |

`code` 类型 part 可携带 `language` 属性用于语法高亮。

### 示例

#### assistant 消息（带 markdown 和代码）

```json
{
  "id": "msg_01JR5XK6T8VH2QPN4M9A0BCDE",
  "role": "assistant",
  "parts": [
    {"content": "好呀～那我想想讲个什么样的故事呢……"},
    {"content": "唔，有了！给你讲一个关于蓝色小星星的故事吧～"},
    {"content": "## 分析结果\n\n根据你的需求，我建议：\n\n1. **首先**检查配置文件\n2. **然后**运行 `npm install`\n3. **最后**启动开发服务器"},
    {"content": "npm install\ngit pull origin main", "language": "bash"}
  ],
  "content_type": {"markdown": [2], "code": [3]},
  "timestamp": "2026-06-03T23:15:00.000+08:00"
}
```

#### user 消息

```json
{
  "id": "msg_01JR5XJGJ76S41BH4AZX2EFGH",
  "role": "user",
  "parts": [
    {"content": "帮我看看怎么安装依赖"}
  ],
  "content_type": {},
  "timestamp": "2026-06-03T23:14:30.000+08:00"
}
```

## 设计原则

- **parts 是唯一数据源**：无需冗余的 `content` 全文字段，各消费者按需提取
- **part = 展示单元**：text 的 part 已经被拆为单句，和 markdown/code part 平级排列
- **分句是存储时行为**：`SentenceBuffer` 拆句后在 `add_message()` 时写入 parts，存储后不再变

## 各消费者适配

### LLM 提示词构建

`parts` → `{role, content}` 字典：

```python
def to_llm_message(msg: dict) -> dict:
    """从新格式提取 LLM 上下文用的 {role, content}。"""
    return {
        "role": msg["role"],
        "content": "".join(p["content"] for p in msg["parts"])
    }
```

所有 part 的 content 按序拼接，不区分类别。

### Session 推送（WS 连接时）

服务端 `_handle_ws()` 连接建立后推送当前 session：

```python
session_msgs = agent._session.recent_messages()
await ws.send_json({"type": "session", "messages": session_msgs})
```

### 前端实时流式

不变。LLM 流式输出 → `SentenceBuffer` 分句 → `on_text_delta` 逐句发送。结构化块（code/markdown）通过约束 LLM 输出放在文本之后，识别后完整发送，不走 TTS。

### 前端历史恢复

收到 `{"type": "session", "messages": [...]}` → 遍历 `parts`：

- `text` part → 纯文本气泡
- `markdown` part → markdown 渲染气泡（前端引入轻量 markdown 渲染库）
- `code` part → 语法高亮代码块，带复制按钮和语言标签

localStorage 仍由 `saveChat()` 维护作为快速首屏缓存，WS 连上后以服务端数据覆盖。

### debug 面板

展示原始的 `parts` 数组 + `content_type` 标记，比当前纯文本更结构化。

## 变更文件清单

| 文件 | 改动要点 |
|------|----------|
| `core/cognitive_loop.py` | 收集分句 + 识别结构化块 → 构建 parts 数组 → 传给 `add_message()` |
| `core/memory/session.py` | `add_message()` 适配 parts 格式；新增 `messages_for_client()` 导出方法 |
| `core/llm_adapter.py` 或 `core/prompt_assembler.py` | 新增 `to_llm_message()` 或适配现有构建逻辑 |
| `core/debug_info.py` | `session_recent` 适配新格式展示 |
| `core/input/server.py` | `_handle_ws()` 连接时推送 `session` 消息 |
| `static/js/main.js` | 新增 `session` handler + `restoreFromServer()`；兼容旧格式 |
| `static/js/persistence.js` | `saveChat()` 改为从 DOM 读取（缓存用）；服务端数据覆盖 |

## 验证方法

1. 启动服务，发送消息给 AI，收到带 markdown/代码的回复
2. 打开 DevTools，检查 WS 消息中 `session` 类型消息的格式是否正确
3. 刷新页面 → 所有消息（user + assistant）完整显示，markdown 正常渲染，代码块语法高亮
4. 打开隐身窗口 → 同上
5. `localStorage.clear()` + 刷新 → 消息从服务端加载，完整显示
6. `/debug` 页面 → 确认新格式正确展示
7. `python -m pytest core/tests/ -v` → 确认 LLM 提示词构建正确提取 parts
