# LLM Prompt 查看器 — 设计文档

## 概述

在 Debug 页面新增「LLM Prompt 查看器」,让开发者能查看发送给 LLM 的完整 `messages` 数组，并与 Jinja2 模板源码对照，方便调试 prompt 模板和排查 LLM 行为问题。

## 核心功能

1. **自动捕获**: 每次 LLM 调用时自动捕获完整的 `messages` 数组 + `tools` 定义 + 模型名
2. **多轮覆盖**: 每轮工具调用都独立保存（一次用户对话可能触发多轮 LLM 调用）
3. **内存环形缓冲**: 保留最近 20 条记录，重启后清空
4. **摘要列表 + 按需加载**: Debug 页面显示轻量摘要，点击后异步加载完整内容
5. **模板源码对照**: 查看完整 messages 的同时展示 Jinja2 模板源码（`prompt/Brain.j2`）

## 架构

```
Agent.start() → 创建 PromptRecorder
  ↓
CognitiveLoop._think_loop()
  → build_messages() 返回 messages
  → PromptRecorder.record(messages, tools, round_num, model)
  ↓
DebugInfo.collect() → recorder.list_summaries()
  ↓
GET /debug/api → prompt_summary 字段（轻量）
GET /debug/api/prompts/{index} → 完整 messages + 模板源码
  ↓
debug.html → Prompt 卡片 → 点击展开 → 左右两栏对比
```

## 组件设计

### 1. `core/prompt_recorder.py`（新增）

内存环形缓冲区，提供记录和查询接口。

```python
from dataclasses import dataclass, field
from collections import deque
from datetime import datetime

@dataclass
class PromptRecord:
    index: int              # 自增全局序号
    timestamp: str          # ISO 时间戳
    round_num: int          # 当次对话的第几轮 (1-based)
    model: str              # LLM 模型名
    messages: list[dict]    # 完整 messages 数组
    tools: list[dict] | None  # tools 定义
    # 以下为派生字段（摘要用）
    message_count: int = 0
    system_prompt_len: int = 0
    total_chars: int = 0

class PromptRecorder:
    def __init__(self, max_records: int = 20):
        self._records: deque[PromptRecord] = deque(maxlen=max_records)
        self._counter: int = 0

    def record(self, messages, tools, round_num, model) -> PromptRecord:
        """保存一次 LLM 调用的 prompt。在 build_messages() 返回后调用。"""
        ...

    def list_summaries(self) -> dict:
        """返回轻量摘要列表，供 DebugInfo 使用。"""
        ...

    def get_full(self, index: int) -> PromptRecord | None:
        """按序号获取完整记录。"""
        ...
```

- 全局单例，由 Agent 创建并注入 CognitiveLoop
- `record()` 在 `_think_loop()` 的 `build_messages()` 之后、`stream_think()` 之前调用
- 派生字段（message_count / system_prompt_len / total_chars）在 record 时计算好，避免后续重复遍历

### 2. `core/cognitive_loop.py`（修改）

在 `_think_loop()` 方法中增加 prompt 捕获：

```python
# 在 build_messages() 返回后
if hasattr(self, '_prompt_recorder') and self._prompt_recorder:
    self._prompt_recorder.record(
        messages=list(messages),  # 浅拷贝防止后续修改影响记录
        tools=tool_defs,
        round_num=round_num,
        model=self._llm.model,
    )
```

注意：
- `messages` 做浅拷贝（`list(messages)`），因为后续工具循环会原地修改 `messages`
- 使用 `hasattr` 确保即使 recorder 未初始化也不会崩溃

### 3. `core/agent.py`（修改）

在 `start()` 中创建 PromptRecorder 并注入 CognitiveLoop：

```python
from core.prompt_recorder import PromptRecorder

# 在 start() 中 TokenTracker 初始化附近
self._prompt_recorder = PromptRecorder(max_records=20)
self._cognitive._prompt_recorder = self._prompt_recorder
```

### 4. `core/debug_info.py`（修改）

新增字段并在 collect() 中填充：

```python
@dataclass
class DebugInfo:
    # ... 现有字段 ...
    prompt_summary: dict = field(default_factory=dict)

async def collect(agent) -> DebugInfo:
    # ...
    recorder = getattr(agent, '_prompt_recorder', None)
    if recorder is not None:
        info.prompt_summary = recorder.list_summaries()
```

### 5. `Module/input/server.py`（修改）

新增 2 个 REST 端点：

#### `GET /debug/api/prompts`

返回所有记录的摘要列表：

```python
async def prompts_list_handler(request):
    agent = request.app['agent']
    recorder = getattr(agent, '_prompt_recorder', None)
    if recorder is None:
        return web.json_response({"records": [], "total": 0, "max": 0})
    return web.json_response(recorder.list_summaries())
```

响应格式：
```json
{
  "records": [
    {
      "index": 42,
      "timestamp": "2026-06-06T15:30:01",
      "round_num": 1,
      "message_count": 12,
      "system_prompt_len": 2340,
      "total_chars": 5678,
      "model": "deepseek/deepseek-chat"
    }
  ],
  "total": 15,
  "max": 20
}
```

#### `GET /debug/api/prompts/{index}`

返回指定记录的完整内容 + 模板源码：

```python
async def prompt_detail_handler(request):
    index = int(request.match_info['index'])
    agent = request.app['agent']
    recorder = getattr(agent, '_prompt_recorder', None)
    if recorder is None:
        raise web.HTTPNotFound()
    record = recorder.get_full(index)
    if record is None:
        raise web.HTTPNotFound()

    # 读取模板源码
    template_source = None
    template_path = "prompt/Brain.j2"
    try:
        with open(template_path) as f:
            template_source = {"path": template_path, "content": f.read()}
    except Exception:
        pass

    return web.json_response({
        "index": record.index,
        "timestamp": record.timestamp,
        "round_num": record.round_num,
        "model": record.model,
        "messages": record.messages,
        "tools": record.tools,
        "template_source": template_source,
    })
```

路由注册（在 `create_app()` 中）：
```python
app.router.add_get('/debug/api/prompts', prompts_list_handler)
app.router.add_get('/debug/api/prompts/{index:\d+}', prompt_detail_handler)
```

### 6. `static/debug.html`（修改）

在概览 tab 的 grid 中新增「LLM Prompt」卡片。

#### 位置

Row 2 或 Row 3，Token 卡片旁边。

#### 收起状态

```html
<div class="card" id="prompt-card">
  <h3>📝 LLM Prompt <span id="prompt-count"></span></h3>
  <div id="prompt-summary">
    <!-- 最近 3 条摘要 + 查看全部按钮 -->
  </div>
</div>
```

每条摘要显示：`#序号 第N轮 Nmsg N字 时间 [查看]`

#### 展开状态 — 模态面板

全宽 panel（不遮挡其他卡片），左右两栏：

- **左栏**：messages 列表，按 role 分色标签，每条可折叠
  - system 消息默认折叠（通常最长）
  - user/assistant/tool 消息默认展开
  - 每条显示 role 标签 + 字符数 + 内容前 3 行预览
- **右栏**：模板源码（`prompt/Brain.j2`），只读 `<pre>` 显示
- **底部**：Tools 定义列表，可展开 JSON

工具栏：
- 「复制 JSON」按钮：一键复制完整 `messages` 数组到剪贴板
- 「上一条」「下一条」按钮：在同次对话的多轮 prompt 之间切换
- 「关闭」按钮

#### 数据流

1. 页面加载/轮询时，`render()` 从 `info.prompt_summary` 渲染摘要
2. 用户点击某条记录 → `fetch('/debug/api/prompts/' + index)` → 渲染详情
3. 详情面板在客户端缓存已加载的记录，避免重复请求
4. 智能 diff 对 `prompt_summary.total` 进行，不变则跳过 DOM 重建

### 7. 模板文件（已有，无需修改）

`prompt/Brain.j2` — 读取磁盘即可，不存内存。

## 涉及文件

| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `core/prompt_recorder.py` | **新增** | PromptRecorder + PromptRecord dataclass |
| `core/cognitive_loop.py` | 修改 | _think_loop() 中调用 recorder.record() |
| `core/agent.py` | 修改 | start() 中创建 PromptRecorder 并注入 |
| `core/debug_info.py` | 修改 | 新增 prompt_summary 字段，collect() 填充 |
| `Module/input/server.py` | 修改 | 新增 2 个 REST 端点 |
| `static/debug.html` | 修改 | 新增 Prompt 卡片 + 详情面板 |

## 验证

1. 启动服务，发送一条对话消息
2. 打开 `/debug` → 概览 tab → 应看到「LLM Prompt」卡片显示 1 条记录
3. 点击记录的 `[>]` 按钮 → 展开详情面板 → 左栏显示完整 messages（含 system prompt），右栏显示 Brain.j2 模板
4. system 消息应默认折叠，点击展开
5. 「复制 JSON」按钮 → 粘贴到文本编辑器，验证是合法 JSON
6. 发送带工具调用的消息 → 卡片应显示多条记录（每轮工具调用 1 条）
7. 发送超过 20 次对话 → 旧记录应被淘汰，卡片总数不超过 20
8. 重启服务 → 之前的 prompt 记录清空（仅内存，预期行为）
9. 运行 `python -m pytest core/ Module/ -q` 确认无回归