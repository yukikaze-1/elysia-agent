# Action Gate 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 在 `normalize_tool_calls()` 之后、dispatch 之前加一层纯代码校验（Action Gate），拦截不存在的工具名，整轮 abort 让 LLM 自行修正。

**架构：** `CognitiveTools` mixin 新增 `_gate_tool_calls()` 方法，在 `_execute_tool_round()` 的 `assistant_entry` 写入之后、dispatch 之前调用。校验仅靠 `ToolExecutor.definitions()` 的 name 集合，不改任何协议或后端。

**技术栈：** Python 3.11+, pytest, 纯函数（零 I/O，零 async）

## 全局约束

- `GATE_REJECTED` 在 `ErrorType` 枚举中排在 `UNKNOWN_TOOL` 之后
- gate 只校验工具存在性（不校验参数），参数校验仍由 `SafetyPipeline` 负责
- `submit_task` 被 gate 跳过（合成工具，不在 plugin registry）
- abort 时整轮取消，合法工具也注入 `GATE_REJECTED` failure message
- gate 返回 `bool`，放在 `assistant_entry`（loop.py:409）之后、submit_task 处理之前
- 黑名单参数 `blocked_tools: set[str] | None = None` 预留，本次不实现逻辑

---

### Task 1: 新增 `GATE_REJECTED` ErrorType

**文件：**
- 修改: `core/schema/error_type.py`

- [ ] **Step 1: 添加枚举值**

在 `UNKNOWN_TOOL` 之后新增：

```python
GATE_REJECTED = "gate_rejected"    # 调度前被 gate 拦截 → 不可重试
```

`is_retryable` 属性不需要改——`GATE_REJECTED` 不在 retryable tuple 中，默认返回 False。

- [ ] **Step 2: 验证**

```bash
python -c "from core.schema.error_type import ErrorType; assert ErrorType.GATE_REJECTED == 'gate_rejected'; assert not ErrorType.GATE_REJECTED.is_retryable; print('OK')"
```

- [ ] **Step 3: 提交**

```bash
git add core/schema/error_type.py
git commit -m "feat: add GATE_REJECTED error type for tool gate validation"
```

---

### Task 2: 实现 `_gate_tool_calls()` 方法

**文件：**
- 修改: `core/cognitive/tools.py`

**接口：**
- 消费: `ToolExecutor.definitions()`（返回 `list[dict]`，每项含 `{"function": {"name": ...}}`）
- 产出: `bool`（True = 全部通过，False = 已注入 failure messages 到 `messages`）

- [ ] **Step 1: 在 `CognitiveTools` 类中添加 `_gate_tool_calls()` 方法**

在 `_execute_tools_directly()` 之后、`_submit_task_definition()` 之前插入：

```python
    def _gate_tool_calls(
        self,
        tool_calls: list[ToolCall],
        messages: list[dict],
        blocked_tools: set[str] | None = None,
    ) -> bool:
        """校验 LLM 产出的 tool_calls。全部通过返回 True。
        任一被拒 → 注入 GATE_REJECTED tool messages 到 messages，返回 False。

        submit_task 被跳过——它是合成工具，有自己的 _handle_submit_task 校验。
        """
        # 1. 拆分：submit_task 单独放行
        gate_tcs = [tc for tc in tool_calls if tc.function.name != "submit_task"]

        # 2. 构建已知工具名集合
        known_names: set[str] = set()
        if self._tool_executor is not None:
            for d in self._tool_executor.definitions():
                name = d.get("function", {}).get("name", "")
                if name:
                    known_names.add(name)

        # 3. 逐项校验
        rejected = False
        rejected_ids: set[str] = set()
        for tc in gate_tcs:
            name = tc.function.name
            reason: str | None = None
            if name not in known_names:
                reason = f"未知工具: {name}"
            elif blocked_tools and name in blocked_tools:
                reason = f"工具已禁用: {name}"

            if reason:
                rejected = True
                rejected_ids.add(tc.id)
                tr = ToolResult.fail(
                    name=name, error=reason,
                    error_type=ErrorType.GATE_REJECTED,
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tr.llm_content,
                })

        # 4. 整轮连坐：合法工具也注入失败消息
        if rejected:
            for tc in gate_tcs:
                if tc.id in rejected_ids:
                    continue
                tr = ToolResult.fail(
                    name=tc.function.name,
                    error="本轮其他工具校验失败，整轮取消",
                    error_type=ErrorType.GATE_REJECTED,
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tr.llm_content,
                })
            return False

        return True
```

- [ ] **Step 2: 确认导入完整**

`tools.py` 顶部已经导入了 `ErrorType`（line 10）和 `ToolCall`（line 11），`ToolResult` 来自 `core.adapter`（line 10）。无需新增 import。

- [ ] **Step 3: 验证**

```bash
python -c "from core.cognitive.tools import CognitiveTools; print('import OK')"
```

- [ ] **Step 4: 提交**

```bash
git add core/cognitive/tools.py
git commit -m "feat: add _gate_tool_calls() method to CognitiveTools"
```

---

### Task 3: 集成 gate 到 `_execute_tool_round()`

**文件：**
- 修改: `core/cognitive/loop.py:409-411`

**接口：**
- 消费: `CognitiveTools._gate_tool_calls()`（Task 2 产出）
- 插入点: `messages.append(assistant_entry)`（line 409）之后，`if submit_tcs:`（line 411）之前

- [ ] **Step 1: 在 assistant_entry 之后插入 gate 调用**

在 `loop.py` line 409 (`messages.append(assistant_entry)`) 之后插入：

```python
        # Gate: 校验 tool_calls 合法性，拦截未知工具名
        if not self._gate_tool_calls(normalized, messages):
            return  # gate 已注入 failure messages，跳过 dispatch
```

- [ ] **Step 2: 验证导入**

`_gate_tool_calls` 在 `CognitiveTools` mixin 上定义，`CognitiveLoop` 继承自 `CognitiveTools`。不需要额外 import。

- [ ] **Step 3: 确认合法路径不受影响**

```bash
python -c "
# 模拟合法路径：所有 tool_calls 都是已知工具 → gate 返回 True → 继续 dispatch
print('smoke check: loop.py syntax OK')
"
```

- [ ] **Step 4: 提交**

```bash
git add core/cognitive/loop.py
git commit -m "feat: integrate action gate into _execute_tool_round"
```

---

### Task 4: 测试

**文件：**
- 新建: `core/cognitive/tests/test_tool_gate.py`

- [ ] **Step 1: 写测试文件**

```python
"""Action Gate 单元测试 — _gate_tool_calls() 工具校验。"""
import json

import pytest
from core.adapter.tool_adapter import ToolResult
from core.cognitive.tools import CognitiveTools
from core.schema.error_type import ErrorType
from core.schema.tool import ToolCall, ToolCallFunction


pytestmark = pytest.mark.unit


class _FakeExecutor:
    """Mock ToolExecutor — 返回固定的 definitions 列表。"""
    def __init__(self, names: list[str] | None = None):
        self._names = names or ["search_memory", "save_memory", "get_time"]

    def definitions(self) -> list[dict]:
        return [
            {"type": "function", "function": {"name": n}}
            for n in self._names
        ]

    def execute(self, name, ctx=None, **kwargs):
        raise NotImplementedError("gate test should not execute")


def _make_tc(name: str, call_id: str = "call_1", args: dict | None = None) -> ToolCall:
    return ToolCall(
        id=call_id,
        function=ToolCallFunction(name=name, arguments=json.dumps(args or {})),
    )


def _gate(calls: list[ToolCall], executor_names: list[str] | None = None,
          blocked: set[str] | None = None) -> tuple[bool, list[dict]]:
    """Helper: 创建 CognitiveTools 实例，调用 _gate_tool_calls。"""
    tools = CognitiveTools()
    tools._tool_executor = _FakeExecutor(executor_names)
    tools._tool_adapter = None
    messages: list[dict] = []
    ok = tools._gate_tool_calls(calls, messages, blocked_tools=blocked)
    return ok, messages


# ---- 合法路径 ----

def test_all_known_tools_pass():
    """全部是已知工具 → 返回 True，不注入 messages。"""
    ok, msgs = _gate([
        _make_tc("search_memory", "c1"),
        _make_tc("get_time", "c2"),
    ])
    assert ok is True
    assert len(msgs) == 0


def test_only_submit_task_passes():
    """submit_task 被跳过，即使不在 registry 也通过。"""
    ok, msgs = _gate([_make_tc("submit_task", "c1")])
    assert ok is True
    assert len(msgs) == 0


def test_empty_calls_passes():
    """空列表直接通过。"""
    ok, msgs = _gate([])
    assert ok is True
    assert len(msgs) == 0


def test_all_known_plus_submit_task_passes():
    """submit_task + 已知工具混合 → 全部通过。"""
    ok, msgs = _gate([
        _make_tc("search_memory", "c1"),
        _make_tc("submit_task", "c2"),
    ])
    assert ok is True


# ---- 拒绝路径 ----

def test_unknown_tool_rejected():
    """幻觉工具名 → 返回 False，注入 GATE_REJECTED message。"""
    ok, msgs = _gate([_make_tc("delete_everything", "c1")])
    assert ok is False
    assert len(msgs) == 1
    assert msgs[0]["role"] == "tool"
    assert msgs[0]["tool_call_id"] == "c1"
    assert "未知工具" in msgs[0]["content"]


def test_unknown_tool_cascades_to_all():
    """一个未知工具 → 同轮所有工具（含合法的）都被注入 GATE_REJECTED。"""
    ok, msgs = _gate([
        _make_tc("delete_everything", "c1"),  # 未知
        _make_tc("get_time", "c2"),           # 已知
    ])
    assert ok is False
    assert len(msgs) == 2
    # c1: 直接被拒
    c1_msg = next(m for m in msgs if m["tool_call_id"] == "c1")
    assert "未知工具" in c1_msg["content"]
    # c2: 连坐被拒
    c2_msg = next(m for m in msgs if m["tool_call_id"] == "c2")
    assert "整轮取消" in c2_msg["content"]


def test_unknown_tool_does_not_affect_submit_task():
    """未知工具被拒时，submit_task 不受连坐影响。"""
    ok, msgs = _gate([
        _make_tc("delete_everything", "c1"),
        _make_tc("submit_task", "c2"),
    ])
    assert ok is False
    # submit_task 被跳过，不应出现在 messages 中
    submit_msgs = [m for m in msgs if m["tool_call_id"] == "c2"]
    assert len(submit_msgs) == 0


def test_multiple_unknown_tools():
    """多个未知工具 → 全部标记。"""
    ok, msgs = _gate([
        _make_tc("bad_tool_1", "c1"),
        _make_tc("bad_tool_2", "c2"),
        _make_tc("get_time", "c3"),
    ])
    assert ok is False
    assert len(msgs) == 3  # 2 rejected + 1 cascade


# ---- 黑名单预留 ----

def test_blocked_tool_rejected():
    """黑名单中的工具被拒。"""
    ok, msgs = _gate(
        [_make_tc("search_memory", "c1")],
        blocked={"search_memory"},
    )
    assert ok is False
    assert len(msgs) == 1
    assert "已禁用" in msgs[0]["content"]


# ---- executor 为 None ----

def test_executor_none_all_unknown():
    """_tool_executor 为 None → 所有非 submit_task 都是未知。"""
    tools = CognitiveTools()
    tools._tool_executor = None
    tools._tool_adapter = None
    messages: list[dict] = []
    ok = tools._gate_tool_calls([_make_tc("get_time", "c1")], messages)
    assert ok is False
    assert len(messages) == 1
    assert "未知工具" in messages[0]["content"]
```

- [ ] **Step 2: 运行测试确认通过**

```bash
pytest core/cognitive/tests/test_tool_gate.py -v
```

预期: 10 passed

- [ ] **Step 3: 防回归测试**

```bash
pytest core/cognitive/tests/ -v
```

预期: 全部通过（gate 不改变合法路径行为）

- [ ] **Step 4: 提交**

```bash
git add core/cognitive/tests/test_tool_gate.py
git commit -m "test: add action gate unit tests"
```
