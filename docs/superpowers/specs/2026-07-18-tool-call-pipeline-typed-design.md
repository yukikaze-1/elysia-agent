# Tool call 管线类型化：消灭裸 `list[dict]`

## Context

tool call 在执行管线中全程裸 `list[dict]`——从 LLM 流式解析到 TaskScheduler 分发，所有访问都是字符串 key：

```python
tc["function"]["name"]       # 字符串 key，无补全、无拼写检查
tc.get("id", "unknown")      # .get() 容错掩盖了数据缺失
```

`core/schema/tool.py` 已定义好 `ToolCall` / `ToolCallFunction` Pydantic 模型（结构完全匹配 OpenAI wire format），但只在一处使用：`cognitive_loop.py:594-600` 将 dict 手动转为 `ToolCall` 写 Session，随即又被 `session.py:140` 的 `model_dump(mode="json")` 转回 dict。

**核心矛盾**：类型定义已存在，但只在 Session 持久化边界打了个来回，真正的执行路径从未受益。

## Goals

1. 以 `normalize_tool_calls()` 为转换边界——上游流式解析保持 dict（wire format），下游全链路 `list[ToolCall]`
2. 删掉 `cognitive_loop.py:594-600` 的冗余 `ToolCall(...)` 构造（`normalized` 直接可用）
3. `_execute_tools_directly()`、`dispatch_tool_calls()`、`_handle_submit_task()` 签名 typed
4. 所有 `tc["function"]["name"]` 变为 `tc.function.name`

## Non-Goals

- **不改** `merge_stream_tool_calls()`——流式增量合并操作字符串拼接，dict 最自然
- **不改** `openai_compat.py`——外部 API 边界，保持 dict 作为 wire format
- **不改** `messages: list[dict]`（LLM 上下文）——那是 OpenAI API 协议格式，typed 无意义
- **不改** `ClientAssistantMessage.tool_calls: list[dict]`——前端序列化格式
- **不改** `ToolCallPayload`（`task.py`）——已是独立的 TypedDict，不相关

---

## Design

### 零、`ToolCallFunction.parsed` — 统一 JSON 解析

**文件**: `core/schema/tool.py`

所有 inline `json.loads(tc.function.arguments)` 替换为 `tc.function.parsed`，消除重复的 try-catch 模板。

**显式排除**: `Module/task_system/core/executor.py:325`——该处 JSON 解析失败时需 raise `_ToolMatchError`（而非静默返回 `{}`），语义与 `.parsed` 不兼容，保持 inline `json.loads`。

```python
class ToolCallFunction(BaseModel):
    """工具调用的 function 块。"""
    name: str = ""
    arguments: str = ""  # JSON 字符串

    @property
    def parsed(self) -> dict[str, Any]:
        """将 arguments JSON 字符串解析为 dict。解析失败返回 {}。"""
        try:
            return json.loads(self.arguments)
        except json.JSONDecodeError:
            return {}
```

- 不加缓存 —— 每个 tool call 的 arguments 只解析 1-2 次（task_scheduler 一次、cognitive_loop emit 一次），没有重复热点
- `ToolCall` 保持可变（`frozen=False`，默认值）—— 当前无修改字段的需求，但保持灵活性

### 转换边界

```
openai_compat.py          list[dict]        ← 不动（外部 API 边界）
    ↓
merge_stream_tool_calls   list[dict]        ← 不动（流式 wire format）
    ↓
normalize_tool_calls      list[ToolCall]    ← ★ 单次转换边界
    ↓
cognitive_loop            属性访问           tc.function.name, tc.id
task_scheduler            属性访问           tc.function.name, tc.id
    ↓
→ messages[] (LLM)        tc.model_dump()   ← 送回 LLM 时转 dict
→ AssistantMessage        tc 直接传入        ← 已是 Pydantic，不再手动构造
```

---

### 一、`normalize_tool_calls()` 改为返回 `list[ToolCall]`

**文件**: `core/tool_call_parser.py`

模块新增依赖：`import json`、`from pydantic import ValidationError`、`import logging`。原 docstring「纯 stdlib，零依赖」去掉「零依赖」——`pydantic` 是 core 全域依赖，可接受。

```python
import json
import logging

from pydantic import ValidationError

from core.schema.tool import ToolCall, ToolCallFunction
from core.schema.types import ToolCallId

logger = logging.getLogger(__name__)


def normalize_tool_calls(raw: list[dict]) -> list[ToolCall]:
    """过滤不完整的工具调用，转为 ToolCall 模型。

    跳过不合规的 tool call 时记录 warning（LLM 产出结构性异常数据）。
    """
    result: list[ToolCall] = []
    for tc in raw:
        func = tc.get("function", {})          # 防御 think() 路径的异常 dict 形状
        if not (tc.get("id") and func.get("name")):
            continue
        arguments = func.get("arguments", "")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments) if isinstance(arguments, dict) else ""
        try:
            result.append(ToolCall(
                id=ToolCallId(tc["id"]),
                function=ToolCallFunction(
                    name=func["name"],
                    arguments=arguments,
                ),
            ))
        except ValidationError as e:
            logger.warning("跳过不合规的 tool call: %s，原始数据: %s", e, tc)
    return result
```

---

### 二、`llm_adapter.py` — `ThoughtResult.tool_calls` 类型更新

**文件**: `core/llm_adapter.py`

```python
@dataclass
class ThoughtResult:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
```

#### `think()` 非流式路径（line 59-60）

`think()` 极少使用（主流路径是 `stream_think()`），但需要处理：

```python
# Before (line 57-62)
try:
    parsed = json.loads(response)
    if "tool_calls" in parsed:
        return ThoughtResult(tool_calls=parsed["tool_calls"])
except (json.JSONDecodeError, TypeError):
    pass

# After
try:
    parsed = json.loads(response)
    if "tool_calls" in parsed:
        raw = parsed["tool_calls"]  # list[dict]，来自 LLM JSON 响应
        return ThoughtResult(tool_calls=normalize_tool_calls(raw))
except (json.JSONDecodeError, TypeError):
    pass
```

注意：`think()` 的输入来自 `json.loads(response)` 而不是 `merge_stream_tool_calls` 的累加器，dict 形状可能不同——缺少 `"function"` 键或 `arguments` 为 object 而非字符串。Section 一的 `normalize_tool_calls` 已用 `tc.get("function", {})` 和 `isinstance(arguments, str)` 防御这两种情况。

`normalize` 会过滤无 `id` 的调用——对 `think()` 来说是行为变化（原先直接透传 parsed 结果），但 `think()` 的 `ThoughtResult.tool_calls` 无消费者检查 `.has_tool_calls` 后按 `list[ToolCall]` 使用，影响为零。

#### `stream_think()` 注释

`stream_think()` 的 yield dict 中 `"tool_calls"` 是流式增量分片（`openai_compat.py:135-147`：index/id/function 碎片），由 `merge_stream_tool_calls()` 以 dict 消费。注释保持 `list[dict] | None`，**不改为 `list[ToolCall]`**：

```python
async def stream_think(
    self, messages: list[dict], tools: list[dict] | None = None, **kwargs
) -> AsyncIterator[dict]:
    """Stream structured chunks with tool_call detection + retry.

    Yields dicts: {"content": str | None, "tool_calls": list[dict] | None}
    流最后一个 chunk 可能包含可选的 "usage" 键.
    """
```

---

### 三、`cognitive_loop.py` — 核心改动

#### import 清理

`cognitive_loop.py` 没有 `from __future__ import annotations`，签名注解在定义时求值。`_execute_tools_directly(normalized: list[ToolCall])` 和 `_handle_submit_task(tc: ToolCall)` 需要 `ToolCall` 在模块作用域可解析，否则 `NameError`。`ToolCallFunction` 不再需要。

```python
# Before (line 49)
from core.schema.tool import ToolCall, ToolCallFunction, ToolCallId

# After
from core.schema.tool import ToolCall, ToolCallId
```

对比：`task_scheduler.py` 有 `from __future__ import annotations`，签名注解惰性求值，那边只加 import 即可。

#### `raw_tool_calls` 保持 `list[dict]`

流式累加仍然用 dict，只有经过 `normalize_tool_calls()` 之后才变成 `list[ToolCall]`。

```python
raw_tool_calls: list[dict] = []                    # 不变
merge_stream_tool_calls(raw_tool_calls, tc_deltas) # 不变
normalized = normalize_tool_calls(raw_tool_calls)  # → list[ToolCall]
```

#### submit_task 过滤（line 528-529）

```python
# Before
submit_tcs = [tc for tc in normalized if tc["function"]["name"] == "submit_task"]
other_tcs  = [tc for tc in normalized if tc["function"]["name"] != "submit_task"]

# After
submit_tcs = [tc for tc in normalized if tc.function.name == "submit_task"]
other_tcs  = [tc for tc in normalized if tc.function.name != "submit_task"]
```

#### LLM context messages（line 536-538）

```python
# Before
assistant_entry["tool_calls"] = normalized          # list[dict]

# After
assistant_entry["tool_calls"] = [
    tc.model_dump(mode="json") for tc in normalized
]
```

#### submit_tcs 循环（line 549, 567） 🔴

```python
# Before
tc_id = tc.get("id") or f"submit_{i}_{id(result)}"  # line 549
# ...
messages.append({                                     # line 552
    "role": "tool",
    "tool_call_id": tc_id,                            # 裸 str
    ...
})
# ...
self._session.add_message(ToolResultMessage(          # line 567
    tool_call_id=ToolCallId(tc_id),                   # ToolCallId 包装
    ...
))

# After — tc 是 ToolCall，没有 .get()；normalize 保证 id 非空，fallback 成死代码
tc_id = tc.id                                         # ToolCallId（即 str）
# ...
messages.append({
    "role": "tool",
    "tool_call_id": tc_id,                            # 不变，仍是 str
    ...
})
# ...
self._session.add_message(ToolResultMessage(
    tool_call_id=tc.id,                               # 直接使用，无需包装
    ...
))
```

#### submit_task 后台任务注册（line 582） 🔴

```python
# Before — 只在 task_system + mailbox + callback 齐活时执行
result.async_task_id, mailbox, intent=(
    json.loads(tc["function"]["arguments"]).get("intent", "")
)

# After — tc.function.parsed 内建 json.JSONDecodeError → {} 兜底
result.async_task_id, mailbox, intent=(
    tc.function.parsed.get("intent", "")
)
```

#### 删掉冗余 ToolCall 构造（line 592-600）

```python
# Before — 手动从 dict 转 ToolCall，normalized 已是 list[ToolCall] 后成为冗余
tool_calls = [ToolCall(
    id=ToolCallId(tc.get("id", "unknown")),
    function=ToolCallFunction(
        name=tc["function"]["name"],
        arguments=tc["function"]["arguments"],
    )
) for tc in normalized]

# After — normalized 直接可用
tool_calls = normalized if normalized else None
```

#### UI emission（line 608-620）

```python
# Before
for tc in dispatch_tcs:
    func_name = tc["function"]["name"]
    call_id = tc.get("id", "unknown")
    try:
        args = json.loads(tc["function"]["arguments"])
    except (json.JSONDecodeError, KeyError):
        args = {}

# After — guard 保留；KeyError 不再可能（属性访问），JSONDecodeError 由 .parsed 内建
for tc in dispatch_tcs:
    func_name = tc.function.name
    call_id = tc.id
    args = tc.function.parsed
```

#### tool result 处理（line 629-660）

```python
# Before
"tool_call_id": tc.get("id", "unknown"),
"tool": tc["function"]["name"],
"call_id": tc.get("id", "unknown"),
# ...
tool_call_id=ToolCallId(tc.get("id", "unknown")),
tool_name=tc["function"]["name"],

# After
"tool_call_id": tc.id,
"tool": tc.function.name,
"call_id": tc.id,
# ...
tool_call_id=tc.id,
tool_name=tc.function.name,
```

#### tool cleanup（line 662-668）

```python
# Before
func_name = tc["function"]["name"]

# After
func_name = tc.function.name
```

#### `_execute_tools_directly()`（line 825-858） 🔴

```python
# Before
async def _execute_tools_directly(self, normalized: list[dict]) -> list:
    async def _execute_one(tc: dict):
        func_name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"])
        except (json.JSONDecodeError, KeyError):
            args = {}

# After — .parsed 内建兜底，去掉 inline try-catch
async def _execute_tools_directly(self, normalized: list[ToolCall]) -> list:
    async def _execute_one(tc: ToolCall):
        func_name = tc.function.name
        args = tc.function.parsed
```

返回类型保持 `list`（与调用方 `asyncio.gather(return_exceptions=True)` 兼容）。

#### `_handle_submit_task()`（line 918-926） 🔴

```python
# Before
async def _handle_submit_task(self, tc: dict) -> ToolResult:
    # ...
    args = json.loads(tc["function"]["arguments"])

# After
async def _handle_submit_task(self, tc: ToolCall) -> ToolResult:
    # ...
    args = tc.function.parsed
```

---

### 四、`task_scheduler.py` — `dispatch_tool_calls()` 签名 + 函数体

#### 签名（line 108-110）

```python
# Before
async def dispatch_tool_calls(
    self, parent_id: str, calls: list[dict],
) -> list[tuple[dict, Any]]:

# After
async def dispatch_tool_calls(
    self, parent_id: str, calls: list[ToolCall],
) -> list[tuple[ToolCall, Any]]:
```

新增 import：`from core.schema.tool import ToolCall`

#### docstring 更新（line 111-118）

```python
# Before
"""CognitiveLoop 调用：将 tool_calls 创建为子任务，并行执行，返回结果。
Returns:
    list of (tc_dict, ToolResult | Exception)
"""

# After
"""CognitiveLoop 调用：将 tool_calls 创建为子任务，并行执行，返回结果。
Returns:
    list of (ToolCall, ToolResult | Exception)
"""
```

#### 函数体（line 121-134）

```python
# Before
func = tc.get("function", {})
arguments = func.get("arguments", {})
if isinstance(arguments, str):
    try:
        arguments = json.loads(arguments)
    except json.JSONDecodeError:
        arguments = {}
t = Task.new_tool_call(
    parent_id=parent_id or "",
    tool_name=func.get("name", "unknown"),
    arguments=arguments,
)

# After — .parsed 内建 JSONDecodeError → {} 兜底
t = Task.new_tool_call(
    parent_id=parent_id or "",
    tool_name=tc.function.name,
    arguments=tc.function.parsed,
)
```

#### 返回构建（line 161）

```python
# Before
out: list[tuple[dict, Any]] = []

# After
out: list[tuple[ToolCall, Any]] = []
```

---

### 五、`agent.py` — 两处透传 lambda，均不动

```python
# line 982 — USER_INPUT 路径
_on_tool_calls = lambda calls: self._scheduler.dispatch_tool_calls(
    parent_id=rid, calls=calls,
)

# line 1002 — 主动搭话路径（silence_timeout）
_on_tool_calls = lambda calls: self._scheduler.dispatch_tool_calls(
    parent_id=rid, calls=calls,
)
```

`calls` 参数自动从 `list[dict]` 变为 `list[ToolCall]`，lambda 透传无需改动。

---

## 测试影响

### `core/tests/test_phase3.py`

#### `normalize_tool_calls` 测试（line 83-109）

返回值断言从 dict 改为 `ToolCall` 对象：

```python
# Before
result = normalize_tool_calls(raw)
assert result[0]["id"] == "a"
assert len(result) == 2

# After
result = normalize_tool_calls(raw)
assert result[0].id == ToolCallId("a")
assert result[0].function.name == "f1"
assert len(result) == 2
```

#### `_handle_submit_task` 测试（line 1712-1805）

构造的 mock `tc` 从 dict 改为 `ToolCall(...)`：

```python
# Before
tc = {"function": {"name": "submit_task", "arguments": '{"intent":"test"}'}, "id": "call_1"}

# After
tc = ToolCall(
    id=ToolCallId("call_1"),
    function=ToolCallFunction(name="submit_task", arguments='{"intent":"test"}'),
)
```

#### line 240, 265 — 不需要改

这些行的 `tc` dict 是测试 `ToolExecutor` 时构造的、未使用的局部变量，不经过 `normalize_tool_calls` 或任何 typed 路径。保持不动。

### `core/tests/test_task_scheduler.py`

所有 `dispatch_tool_calls` 测试（line 138-373）的 `calls` 参数从 dict 改为 `ToolCall`。建议新增测试辅助函数（两个测试文件均需新增 `from core.schema.tool import ToolCall, ToolCallFunction` + `from core.schema.types import ToolCallId`）：

```python
def _tc(id_="c1", name="echo", args="{}"):
    return ToolCall(
        id=ToolCallId(id_),
        function=ToolCallFunction(name=name, arguments=args),
    )
```

替换所有手工构造的 dict：

```python
# Before
calls = [
    {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": '{"msg":"a"}'}},
]

# After
calls = [
    _tc("c1", "echo", '{"msg":"a"}'),
]
```

---

## 不改的文件清单

| 文件 | 原因 |
|------|------|
| `Module/llm/openai_compat.py` | 外部 API 边界，保持 dict |
| `core/task.py` | `ToolCallPayload` TypedDict 已独立，不相关 |
| `core/schema/message.py` | `ClientAssistantMessage.tool_calls` 是前端序列化格式 |
| `core/memory/session.py` | 已用 `msg.tool_calls` Pydantic 属性，不相关 |
| `core/schema/tool.py` | 新增 `parsed` property + `import json, typing.Any` |

---

## 收益

1. **删 8 行冗余转换**：`cognitive_loop.py:594-600` 的 dict→ToolCall 手动构造——`normalized` 直接可用
2. **~25 处字符串 key 访问 → 属性访问**：IDE 补全、重构安全、拼写错误编译期捕获
3. **`.get("id", "unknown")` 消除**：`ToolCall.id` 是必填字段，`normalize_tool_calls` 已保证非空，不再需要 defensive `.get()`
4. **类型流贯通**：从 `normalize_tool_calls` 到 `dispatch_tool_calls`，全程 `list[ToolCall]`，无需在函数间靠注释约定 dict shape
5. **🔴 三个隐蔽崩溃点消除**：`tc.get("id")` on Pydantic（line 549）、`tc["function"]["arguments"]` on Pydantic（line 582 submit_task 后台注册分支）、`_handle_submit_task` 函数体 dict 访问（line 926）
