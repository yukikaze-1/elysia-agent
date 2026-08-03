# Tool call 管线类型化 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `normalize_tool_calls()` 返回 `list[ToolCall]`，下游全链路属性访问替代字符串 key dict 访问。

**Architecture:** Task 1 原子完成 `tool_call_parser` → `task_scheduler` → `cognitive_loop` 三步（三者构成不可分割的 `list[ToolCall]` 消费者链，中间 commit 不独立可用，最终 squash 合并）。Task 2 独立改 `llm_adapter`。

**执行环境:** 在 feature 分支或 git worktree 中执行（Task 1 三步需 squash，不能在 main 上直接 commit）。

**Tech Stack:** Python 3.11+, Pydantic v2, pytest（conda env: Elysia）

## Global Constraints

- `normalize_tool_calls()` 是唯一的 dict→ToolCall 转换边界，此函数之后不再出现 `tc["function"]["name"]` 式 dict 访问
- 流式增量分片（`merge_stream_tool_calls` 输入 / `stream_think` 的 `"tool_calls"` yield 值）保持 `list[dict]`
- `messages: list[dict]`（LLM 上下文）保持 dict——那是 OpenAI wire format
- `normalize_tool_calls` 防御版：`tc.get("function", {})` + `isinstance(arguments, str)` 检查 + `json.dumps` 兜底
- 所有 `json.loads(tc["function"]["arguments"])` 改为 `json.loads(tc.function.arguments)`，保留 `except json.JSONDecodeError` guard，去掉不再可能的 `KeyError`
- Pydantic BaseModel 没有 `.get()`——`tc.get("id")` → `tc.id`
- `id` 是内置函数名——测试辅助参数用 `id_`
- Task 1 的 3 个 Step-commit 之间 loop 级工具测试预期红（`normalize` 返回 `ToolCall` 但下游尚未全改完），不要在中间 commit 修，合入时 squash

---

### Task 0: 基线

- [ ] **Step 1: 记录当前测试基线**

Run: `make test-quick 2>&1 | tail -5`
记录 PASS/FAIL 数量作为基线。最终 Task 2 完成后应与基线一致（仅预存失败，无新增/减少）。

---

### Task 1: `normalize_tool_calls` → `task_scheduler` → `cognitive_loop`（原子）

**Files:**
- Modify: `core/tool_call_parser.py`
- Modify: `core/task_scheduler.py:108-161`
- Modify: `core/cognitive_loop.py`（~25 处）
- Test: `core/tests/test_phase3.py`
- Test: `core/tests/test_task_scheduler.py`

**Interfaces:**
- `normalize_tool_calls(raw: list[dict]) -> list[ToolCall]`
- `dispatch_tool_calls(self, parent_id: str, calls: list[ToolCall]) -> list[tuple[ToolCall, Any]]`
- `_execute_tools_directly(self, normalized: list[ToolCall]) -> list`
- `_handle_submit_task(self, tc: ToolCall) -> ToolResult`

---

- [ ] **Step 1: `normalize_tool_calls()` 返回 `list[ToolCall]`**

`core/tool_call_parser.py` 完整替换：

```python
"""流式工具调用解析 — 合并增量 + 归一化。

CognitiveLoop 流式接收 tool_call deltas 时，用本模块的函数
增量合并和归一化。从 cognitive_loop.py 提取。
normalize_tool_calls 依赖 pydantic（core 全域依赖）。
"""

import json

from core.schema.tool import ToolCall, ToolCallFunction
from core.schema.types import ToolCallId


def merge_stream_tool_calls(accumulated: list[dict], deltas: list[dict]) -> None:
    """合并流式 tool_call deltas 到累加列表（原地修改）。

    流式 LLM 响应的 tool_calls 是分片到达的：
    - 每个 delta 带 index（第几个工具调用）
    - id / function.name / function.arguments 可能跨多个 delta 到达

    Args:
        accumulated: 累加的目标列表，会被原地修改
        deltas: 从 LLM chunk 中取出的 tool_call delta 列表
    """
    for delta in deltas:
        idx = delta.get("index", 0)
        while len(accumulated) <= idx:
            accumulated.append({
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            })
        if delta.get("id"):
            accumulated[idx]["id"] = delta["id"]
        if "function" in delta:
            func = delta["function"]
            if func.get("name"):
                accumulated[idx]["function"]["name"] += func["name"]
            if func.get("arguments"):
                accumulated[idx]["function"]["arguments"] += func["arguments"]


def normalize_tool_calls(raw: list[dict]) -> list[ToolCall]:
    """过滤不完整的工具调用，转为 ToolCall 模型。

    Args:
        raw: merge_stream_tool_calls 产出的原始工具调用列表

    Returns:
        仅包含 id 和 function.name 都非空的 ToolCall
    """
    result: list[ToolCall] = []
    for tc in raw:
        func = tc.get("function", {})          # 防御 think() 路径的异常 dict 形状
        if tc.get("id") and func.get("name"):
            arguments = func.get("arguments", "")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments) if isinstance(arguments, dict) else ""
            result.append(ToolCall(
                id=ToolCallId(tc["id"]),
                function=ToolCallFunction(
                    name=func["name"],
                    arguments=arguments,
                ),
            ))
    return result
```

更新 `core/tests/test_phase3.py` — 文件顶部 import 区新增：

```python
from core.schema.tool import ToolCall, ToolCallFunction
from core.schema.types import ToolCallId
```

在 `@pytest.mark.unit class TestNormalizeToolCalls` 内，保留类结构和 marker、方法名不变，**仅改方法体内的断言**——测试数据 dict 不变，返回值断言从 dict 改为 ToolCall 属性访问：

```python
@pytest.mark.unit
class TestNormalizeToolCalls:
    def test_all_complete(self):
        raw = [
            {"id": "a", "function": {"name": "f1", "arguments": "{}"}},
            {"id": "b", "function": {"name": "f2", "arguments": "{}"}},
        ]
        result = normalize_tool_calls(raw)
        assert len(result) == 2
        # 以下两行从 dict 改为 ToolCall 属性
        assert result[0].id == ToolCallId("a")
        assert result[0].function.name == "f1"
        assert result[1].id == ToolCallId("b")
        assert result[1].function.name == "f2"

    def test_all_incomplete_missing_id(self):
        raw = [{"id": "", "function": {"name": "f1", "arguments": "{}"}}]
        assert normalize_tool_calls(raw) == []

    def test_all_incomplete_missing_name(self):
        raw = [{"id": "x", "function": {"name": "", "arguments": "{}"}}]
        assert normalize_tool_calls(raw) == []

    def test_mixed(self):
        raw = [
            {"id": "a", "function": {"name": "ok", "arguments": "{}"}},
            {"id": "", "function": {"name": "bad", "arguments": "{}"}},
        ]
        result = normalize_tool_calls(raw)
        assert len(result) == 1
        assert result[0].id == ToolCallId("a")
        assert result[0].function.name == "ok"
```

验证 normalize 和 merge 测试：

Run: `pytest core/tests/test_phase3.py -q --tb=short`
Expected: normalize 的 4 个测试 PASS + merge 测试 PASS；**loop 级工具测试预期红**（`normalize` 返回 `ToolCall` 但 `cognitive_loop` 下游尚未改，`tc["function"]` 会 TypeError）——不要修，Step 3 改完 cognitive_loop 自然绿。

Commit:

```bash
git add core/tool_call_parser.py core/tests/test_phase3.py
git commit -m "refactor: normalize_tool_calls 返回 list[ToolCall] 替代 list[dict]"
```

---

- [ ] **Step 2: `dispatch_tool_calls()` 接受 `list[ToolCall]`**

`core/task_scheduler.py` — import 区新增：

```python
from core.schema.tool import ToolCall
```

替换第 108-134 行（签名 + docstring + 函数体）：

```python
    async def dispatch_tool_calls(
        self, parent_id: str, calls: list[ToolCall],
    ) -> list[tuple[ToolCall, Any]]:
        """CognitiveLoop 调用：将 tool_calls 创建为子任务，并行执行，返回结果。

        Args:
            parent_id: 父任务标识（request_id），为空字符串时跳过父子关联
            calls: normalized tool calls from CognitiveLoop

        Returns:
            list of (ToolCall, ToolResult | Exception)
        """
        sub_tasks: list[Task] = []
        for tc in calls:
            try:
                arguments = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                arguments = {}
            t = Task.new_tool_call(
                parent_id=parent_id or "",
                tool_name=tc.function.name,
                arguments=arguments,
            )
            sub_tasks.append(t)
```

替换第 161 行类型标注：

```python
        out: list[tuple[ToolCall, Any]] = []
```

更新 `core/tests/test_task_scheduler.py` — 文件顶部 import 区新增：

```python
from core.schema.tool import ToolCall, ToolCallFunction
from core.schema.types import ToolCallId
```

新增辅助函数（放在 import 之后、第一个测试类之前）：

```python
def _tc(id_="c1", name="echo", args="{}"):
    return ToolCall(
        id=ToolCallId(id_),
        function=ToolCallFunction(name=name, arguments=args),
    )
```

**只替换 `calls = [...]` 中的 dict 字面量**，测试体其余部分一字不动：

Before → After 对照（以 `test_dispatch_tool_calls_parallel` 为例，其余测试同理）：

```python
# Before
calls = [
    {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": '{"msg":"a"}'}},
    {"id": "c2", "type": "function", "function": {"name": "echo", "arguments": '{"msg":"b"}'}},
    {"id": "c3", "type": "function", "function": {"name": "echo", "arguments": '{"msg":"c"}'}},
]

# After
calls = [
    _tc("c1", "echo", '{"msg":"a"}'),
    _tc("c2", "echo", '{"msg":"b"}'),
    _tc("c3", "echo", '{"msg":"c"}'),
]
```

其他 4 个测试函数的 `calls` 同理替换：
- `test_dispatch_tool_calls_mixed_results`: `_tc("c1","echo",...)`, `_tc("c2","failing",...)`, `_tc("c3","crashing","{}")`
- `test_dispatch_tool_calls_unknown_tool`: `[_tc("c1","nonexistent","{}")]`
- `test_dispatch_tool_calls_empty`: `[]`（空列表不变）
- `test_dispatch_tool_calls_cancelled`: `_tc("c1","waiter","{}")`, `_tc("c2","waiter","{}")`

验证：

Run: `pytest core/tests/test_task_scheduler.py -q --tb=short`
Expected: 全部 26 tests PASS

Commit:

```bash
git add core/task_scheduler.py core/tests/test_task_scheduler.py
git commit -m "refactor: dispatch_tool_calls 接受 list[ToolCall] 替代 list[dict]"
```

---

- [ ] **Step 3: `cognitive_loop.py` — dict 访问 → 属性访问**

`core/cognitive_loop.py` — **import 清理**（第 49 行）：

```python
# Before
from core.schema.tool import ToolCall, ToolCallFunction, ToolCallId

# After
from core.schema.tool import ToolCall, ToolCallId
```

按以下顺序逐个替换（每处都给出 Before/After，精确对应行号）：

**3a. submit_task 过滤（line 528-529）**

```python
# Before
submit_tcs = [tc for tc in normalized if tc["function"]["name"] == "submit_task"]
other_tcs  = [tc for tc in normalized if tc["function"]["name"] != "submit_task"]

# After
submit_tcs = [tc for tc in normalized if tc.function.name == "submit_task"]
other_tcs  = [tc for tc in normalized if tc.function.name != "submit_task"]
```

**3b. LLM context messages（line 536-538）**

```python
# Before
assistant_entry["tool_calls"] = normalized

# After
assistant_entry["tool_calls"] = [tc.model_dump(mode="json") for tc in normalized]
```

**3c. submit_tcs 循环（line 549, 567）**

```python
# Before (line 549)
tc_id = tc.get("id") or f"submit_{i}_{id(result)}"
# After
tc_id = tc.id
```

line 552 的 `"tool_call_id": tc_id` 不变（已是 str）。

```python
# Before (line 567)
tool_call_id=ToolCallId(tc_id),
# After
tool_call_id=tc.id,
```

**3d. submit_task 后台任务注册（line 582）**

```python
# Before
json.loads(tc["function"]["arguments"]).get("intent", "")
# After
json.loads(tc.function.arguments).get("intent", "")
```

**3e. 删掉冗余 ToolCall 构造（line 592-600）**

```python
# Before
                    tool_calls = None
                    if normalized:
                        tool_calls = [ToolCall(
                            id=ToolCallId(tc.get("id", "unknown")),
                            function=ToolCallFunction(
                                name=tc["function"]["name"],
                                arguments=tc["function"]["arguments"],
                            )
                        ) for tc in normalized]
                    self._session.add_message(AssistantMessage(
                        content=[TextPart(content=acc_text)] if acc_text else [],
                        message_id=new_message_id(),
                        tool_calls=tool_calls,
                    ))

# After
                    self._session.add_message(AssistantMessage(
                        content=[TextPart(content=acc_text)] if acc_text else [],
                        message_id=new_message_id(),
                        tool_calls=normalized if normalized else None,
                    ))
```

**3f. UI emission（line 608-620）**

```python
# Before
for tc in dispatch_tcs:
    func_name = tc["function"]["name"]
    call_id = tc.get("id", "unknown")
    try:
        args = json.loads(tc["function"]["arguments"])
    except (json.JSONDecodeError, KeyError):
        args = {}

# After
for tc in dispatch_tcs:
    func_name = tc.function.name
    call_id = tc.id
    try:
        args = json.loads(tc.function.arguments)
    except json.JSONDecodeError:
        args = {}
```

**3g. tool result — messages（line 633-637）**

```python
# Before
messages.append({
    "role": "tool",
    "tool_call_id": tc.get("id", "unknown"),
    "content": tool_result.llm_content,
})

# After
messages.append({
    "role": "tool",
    "tool_call_id": tc.id,
    "content": tool_result.llm_content,
})
```

**3h. tool result — output.action（line 640-649）**

```python
# Before
await self._output.action("tool_done", params={
    "tool": tc["function"]["name"],
    "call_id": tc.get("id", "unknown"),
    ...
})

# After
await self._output.action("tool_done", params={
    "tool": tc.function.name,
    "call_id": tc.id,
    ...
})
```

**3i. tool result — ToolResultMessage（line 651-659）**

```python
# Before
self._session.add_message(ToolResultMessage(
    content=[TextPart(content=tool_result.content or "")],
    tool_call_id=ToolCallId(tc.get("id", "unknown")),
    tool_name=tc["function"]["name"],
    ...
))

# After
self._session.add_message(ToolResultMessage(
    content=[TextPart(content=tool_result.content or "")],
    tool_call_id=tc.id,
    tool_name=tc.function.name,
    ...
))
```

**3j. tool cleanup（line 662-668）**

```python
# Before
func_name = tc["function"]["name"]
# After
func_name = tc.function.name
```

**3k. `_execute_tools_directly`（line 825-858）**

```python
# Before
async def _execute_tools_directly(
    self, normalized: list[dict],
) -> list:
    async def _execute_one(tc: dict):
        func_name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"])
        except (json.JSONDecodeError, KeyError):
            args = {}

# After
async def _execute_tools_directly(
    self, normalized: list[ToolCall],
) -> list:
    async def _execute_one(tc: ToolCall):
        func_name = tc.function.name
        try:
            args = json.loads(tc.function.arguments)
        except json.JSONDecodeError:
            args = {}
```

**3l. `_handle_submit_task`（line 918-926）**

```python
# Before
async def _handle_submit_task(self, tc: dict) -> ToolResult:
    ...
    args = json.loads(tc["function"]["arguments"])

# After
async def _handle_submit_task(self, tc: ToolCall) -> ToolResult:
    ...
    args = json.loads(tc.function.arguments)
```

**3m. 更新 `test_phase3.py` 中 `_handle_submit_task` 测试数据**

将所有 `tc = {"function": ...}` 替换为：

```python
tc = ToolCall(
    id=ToolCallId("call_1"),
    function=ToolCallFunction(name="submit_task", arguments='{"intent":"test"}'),
)
```

`ToolCall` / `ToolCallFunction` / `ToolCallId` 已在 Step 1 导入 test_phase3.py。

**验证**：

Run: `pytest core/tests/test_phase3.py core/tests/test_task_scheduler.py -q --tb=short`
Expected: 全部 PASS（loop 级工具测试现在应绿——cognitive_loop 下游已改完）

然后跑快速套件确认基线：

Run: `make test-quick 2>&1 | tail -5`
Expected: 与 Task 0 基线一致（仅预存失败，无新增/减少）

**Commit**：

```bash
git add core/cognitive_loop.py core/tests/test_phase3.py
git commit -m "refactor: cognitive_loop tool call dict 访问 → ToolCall 属性访问"
```

> **Step 1-3 合入时 squash 为一个 commit**：三步之间 loop 级工具测试不绿，各自 commit message 仅用于中间追踪。

---

### Task 2: `llm_adapter.py` — `ThoughtResult.tool_calls` + `think()` 路径

**Files:**
- Modify: `core/llm_adapter.py`

**Interfaces:**
- Consumes: `normalize_tool_calls() -> list[ToolCall]` (Task 1)
- Produces: `ThoughtResult.tool_calls: list[ToolCall]`

- [ ] **Step 1: 更新 import 和 `ThoughtResult`**

`core/llm_adapter.py` — import 区新增：

```python
from core.tool_call_parser import normalize_tool_calls
from core.schema.tool import ToolCall
```

第 24-26 行：

```python
# Before
tool_calls: list[dict] = field(default_factory=list)

# After
tool_calls: list[ToolCall] = field(default_factory=list)
```

- [ ] **Step 2: 更新 `think()` 非流式路径（line 55-62）**

```python
# Before
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
                return ThoughtResult(tool_calls=normalize_tool_calls(parsed["tool_calls"]))
        except (json.JSONDecodeError, TypeError):
            pass
```

注：`think()` 非流式路径极少使用（主流是 `stream_think()`），`normalize_tool_calls` 会过滤无 `id` 的调用——这是行为变化，但 `ThoughtResult.tool_calls` 无消费者受影响。

- [ ] **Step 3: 更新 `stream_think()` 注释（line 76）**

```python
# Before
        Yields dicts: {"content": str | None, "tool_calls": list | None}

# After
        Yields dicts: {"content": str | None, "tool_calls": list[dict] | None}
```

- [ ] **Step 4: 验证**

Run: `pytest core/tests/test_phase3.py -q --tb=short`
Expected: 全部 PASS

Run: `make test-quick 2>&1 | tail -5`
Expected: 与 Task 0 基线一致

- [ ] **Step 5: Commit**

```bash
git add core/llm_adapter.py
git commit -m "refactor: ThoughtResult.tool_calls 类型 list[dict] → list[ToolCall]"
```

---

## 可选：`think()` 路径 unit test

```python
# core/tests/test_phase3.py — 追加到 TestNormalizeToolCalls 类中
def test_normalize_filters_missing_id_from_api_response(self):
    """think() 非流式路径：LLM JSON 响应中无 id 的调用被过滤。"""
    raw = [{"function": {"name": "search", "arguments": "{}"}}]  # 缺 id
    assert normalize_tool_calls(raw) == []
```
