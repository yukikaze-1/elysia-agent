# Token 消耗追踪 — 设计文档

日期：2026-06-06
状态：已确认

---

## 1. 目标

为项目对话系统增加 token 消耗追踪机制：
- 记录每次 LLM API 调用的 `prompt_tokens` + `completion_tokens`
- 按模型分别统计
- 在聊天 UI + Debug 页面展示
- Session 级实时显示 + MongoDB 持久化

## 2. 方案：TokenTracker 独立组件（方案 C）

新建 `core/token_tracker.py`，将所有 token 统计逻辑集中在一个类中。CognitiveLoop 通过极简接口调用。

```
OpenAI API response.usage
        │
        ▼
OpenAICompatBackend._stream_structured()
  捕获 chunk.usage，作为 stream_think() 最后一个 chunk 发出
  {"content": null, "tool_calls": null, "usage": {prompt_tokens, completion_tokens, total_tokens}}
        │
        ▼
LLMAdapter.stream_think()  — 透传（无改动）
        │
        ▼
CognitiveLoop._think_loop()  — 检测 "usage" in chunk
  → await self.token_tracker.record(usage, model, round_num)
        │
        ▼
TokenTracker (新增)
  ├── 内存累计：session 内按模型分类的 token 统计
  ├── WS 广播：{"type": "token_usage", ...}
  └── 持久化：MongoDB（不可用时降级到本地 JSONL 文件）
```

## 3. 核心组件：TokenTracker

### 3.1 文件

`core/token_tracker.py` — 新建

### 3.2 数据结构

```python
@dataclass
class TokenUsage:
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    round_num: int
    timestamp: float
```

### 3.3 TokenTracker 类

```python
class TokenTracker:
    def __init__(self, session_id: str, output_manager, document_db=None):
        self.session_id = session_id
        self.output = output_manager
        self.document_db = document_db

        # Session 内存累计
        self.session_total = 0
        self.per_model: dict[str, int] = {}
        self.current_turn_usages: list[TokenUsage] = []

    async def record(self, usage: dict, model: str, round_num: int):
        """每轮 API 调用完成后调用一次"""
        u = TokenUsage(...)
        # 1. 内存累计
        self.session_total += u.total_tokens
        self.per_model[model] = self.per_model.get(model, 0) + u.total_tokens
        self.current_turn_usages.append(u)
        # 2. WS 广播
        await self.output.broadcast({...})
        # 3. 持久化
        await self._persist(u)

    def start_turn(self):
        self.current_turn_usages = []

    async def get_session_stats(self) -> dict:
        """重连时从 DB 恢复 session 统计"""

    async def flush_pending(self):
        """MongoDB 恢复后导入积压文件"""
```

### 3.4 MongoDB 持久化

- 集合名：`token_usage`
- 文档字段：`session_id`, `model`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `round_num`, `timestamp`
- 索引：`session_id + timestamp`、`model`

### 3.5 MongoDB 不可用时：本地文件兜底

- 文件路径：`data/token_usage_pending.jsonl`（JSONL 格式，每行一条记录）
- `_persist()`：MongoDB 写入失败时追加到 JSONL
- `flush_pending()`：Agent 启动/MongoDB 恢复时批量导入，成功后删除文件
- `get_session_stats()`：查 MongoDB + 扫 pending 文件中当前 session 的行做补充聚合

## 4. Backend 层改动

### 4.1 openai_compat.py

- 请求参数加 `stream_options={"include_usage": True}`
- `_stream_structured()`：在 `async for` 循环末尾检测 `chunk.usage`，若存在则 yield `{"content": None, "tool_calls": None, "usage": {...}}`
- `_call_api()` 非流式路径：存 `self.last_usage`
- 兼容性：`chunk.usage` 为 `None` 时不触发，静默降级

### 4.2 protocol.py

- `stream_think()` docstring 说明 yield dict 可能包含可选 `"usage"` 键

## 5. CognitiveLoop 层改动

### 5.1 _think_loop()

在 stream_think 的 for 循环中增加一个 if：

```python
if "usage" in chunk:
    await self.token_tracker.record(
        usage=chunk["usage"],
        model=self._model_id,
        round_num=self._round_count
    )
```

每轮对话开始前调 `self.token_tracker.start_turn()`。

### 5.2 LLMAdapter

无需改动，透传 usage chunk。仅更新 docstring。

## 6. Agent 层改动

```python
# agent.py __init__ 中
self.token_tracker = TokenTracker(
    session_id=self.session_id,
    output_manager=self.output_manager,
    document_db=self.document_db if available else None
)
# MongoDB 可用时 flush 积压文件
await self.token_tracker.flush_pending()
```

## 7. Debug 信息

`core/debug_info.py` 新增字段：

```python
token_stats: dict = field(default_factory=lambda: {
    "session_total": 0,
    "per_model": {},
    "recent_usages": []
})
```

`/debug/api` 自动露出。

## 8. 前端展示

### 8.1 WebSocket 消息格式

```json
{
    "type": "token_usage",
    "turn": {
        "rounds": [
            {"round_num": 0, "model": "...", "prompt_tokens": 800,
             "completion_tokens": 200, "total_tokens": 1000}
        ],
        "turn_total": 1200
    },
    "session": {
        "total": 15700,
        "by_model": {"deepseek/deepseek-chat": 12500, "openai/gpt-4o": 3200}
    }
}
```

### 8.2 前端文件改动

| 文件 | 改动 |
|------|------|
| `static/index.html` | 顶栏加 `#session-token-bar` 容器 |
| `static/js/state.js` | 新增 `sessionTokens`, `currentTurnTokens` 字段 |
| `static/js/main.js` | `handleMessage()` 新增 `case "token_usage"` |
| `static/js/chat.js` | 新增 `renderTokenLabel()` + `updateSessionTokenDisplay()` |
| `static/css/chat.css` | `.token-label` 样式 |

### 8.3 UI 表现

- **AI 气泡底部**：紧凑 token 标签（如 `🪙 1.2k tokens ▼`）
- **点击展开**：显示每轮 prompt/completion 明细
- **顶栏**：Session 累计总量 + hover 显示各模型明细
- **刷新重连**：通过 `session` 消息附带 `session_tokens` 恢复显示

## 9. 容错

| 场景 | 处理 |
|------|------|
| API 不返回 usage（Ollama 旧版） | 静默降级，不广播不写入 |
| MongoDB 不可用 | 降级到 `data/token_usage_pending.jsonl` |
| MongoDB 恢复 | `flush_pending()` 批量导入后删除文件 |
| 页面重连 | `get_session_stats()` 从 DB + pending 文件恢复 |
| 模型切换 | `record()` 按传入的 model 参数自动分类 |

## 10. 涉及文件汇总

| 文件 | 操作 |
|------|------|
| `core/token_tracker.py` | 新建 |
| `core/agent.py` | 修改：实例化 TokenTracker，注入 CognitiveLoop |
| `core/cognitive_loop.py` | 修改：检测 usage chunk，调 TokenTracker |
| `core/debug_info.py` | 修改：新增 token_stats 字段 |
| `core/llm_adapter.py` | 修改：docstring 更新 |
| `Module/llm/openai_compat.py` | 修改：捕获 usage，emit usage chunk |
| `Module/llm/protocol.py` | 修改：docstring 说明 usage chunk |
| `static/index.html` | 修改：加 token bar 容器 |
| `static/js/state.js` | 修改：加 token 状态字段 |
| `static/js/main.js` | 修改：处理 token_usage 消息 |
| `static/js/chat.js` | 修改：渲染 token 标签 |
| `static/css/chat.css` | 修改：token 标签样式 |
