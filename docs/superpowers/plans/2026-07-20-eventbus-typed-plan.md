# EventBus 事件数据类型化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 EventBus 的 `data: Any` + ad-hoc dict 改为 Pydantic discriminated union：5 个事件 data 模型 + `post(XxxData(...))` 自动推导 type + consumer 端 `isinstance` 分发。

**Architecture:** 转换边界在 `bus.post()`——data 从 `dict` 变为 `EventData`（Pydantic model，构造即校验），`type` 从 `data.event_type` 自动推导。上游 16 处 producer 改为构造 typed model，下游 6 个 handler 改为 typed 签名 + 属性访问。

**Tech Stack:** Python 3.11+, Pydantic v2, pytest

## Global Constraints

- 中文 docstring/注释
- `core/event_bus.py` 保持相对导入风格（`.schema.event`）
- `core/schema/__init__.py` 新 block 追加到最后一个 block（memory，L61-69）之后
- `core/schema/README.md` 目录树 + 速查表同步
- 测试 marker：schema 单元 `@pytest.mark.unit`，服务 `@pytest.mark.service`
- conda 环境：`conda run -n Elysia`（Makefile 内部已含，`make` 命令勿再包裹）
- **不改**：`hitl_request`/`hitl_cancel` stub、`EventPriority`/`EventType` enum、`EventBus.next()`/`drain()`/`pending_count` 签名
- 已知 ~12 个 pre-existing test failures——与本次无关，最终验证时忽略
- `time_of_day` bug 只加 TODO 注释，不修

---

## File Structure

```
core/schema/event.py              [CREATE]   5 数据模型 + EventData union
core/schema/__init__.py           [MODIFY]   re-export event types
core/schema/README.md             [MODIFY]   目录树 + 速查表
core/schema/tests/test_event.py   [CREATE]   模型单元测试
core/event_bus.py                 [MODIFY]   Event.data 类型 + post() 签名
core/idle_monitor.py              [MODIFY]   2 处 SYSTEM_TICK dict → model
core/cognitive_loop.py            [MODIFY]   9 处 STATE_CHANGE dict → model
core/input/server.py            [MODIFY]   2 处 INTERRUPT + USER_INPUT → model
core/agent.py                     [MODIFY]   4 处 producer + 6 handler 签名 + _process_events 分发
core/tests/test_phase1.py         [MODIFY]   3 处 INTERRUPT dict → model
core/tests/test_task_scheduler.py [MODIFY]   1 处 INTERRUPT dict → model
core/input/test_server.py       [NO CHANGE] post 被 MagicMock 包裹，无签名校验
```

---

### Task 1: Schema 模型 + 单元测试 + 注册

**Files:**
- Create: `core/schema/event.py`
- Create: `core/schema/tests/test_event.py`
- Modify: `core/schema/__init__.py`
- Modify: `core/schema/README.md`

**Interfaces:**
- Produces: `InterruptData`, `StateChangeData`, `UserInputData`, `SystemTickData`, `TaskResultData`, `EventData` — Tasks 2-4 消费

- [ ] **Step 1: 写失败测试 `core/schema/tests/test_event.py`**

```python
# core/schema/tests/test_event.py
"""core/schema/event.py 的 EventData union 模型测试。"""
import pytest
from pydantic import TypeAdapter, ValidationError

from core.schema.event import (
    EventData,
    InterruptData,
    StateChangeData,
    TaskResultData,
    UserInputData,
    SystemTickData,
)


@pytest.mark.unit
class TestEventDataUnion:
    """EventData discriminated union — 按 event_type 分发。"""

    def test_dispatch_all_five(self):
        adapter = TypeAdapter(EventData)
        cases = [
            ({"event_type": "interrupt", "source": "websocket"}, InterruptData),
            ({"event_type": "state_change", "state": "idle", "request_id": "r1"}, StateChangeData),
            ({"event_type": "user_input", "text": "hi", "user_name": "妖梦", "request_id": "r2"},
             UserInputData),
            ({"event_type": "system_tick", "reason": "silence_timeout", "elapsed": 30.0},
             SystemTickData),
            ({"event_type": "task_result", "task_id": "t1", "result": "done",
              "original_intent": "搜索", "dispatched_at": "2025-01-01T00:00:00Z"},
             TaskResultData),
        ]
        for payload, klass in cases:
            result = adapter.validate_python(payload)
            assert isinstance(result, klass)

    def test_unknown_event_type_rejected(self):
        with pytest.raises(ValidationError):
            TypeAdapter(EventData).validate_python({"event_type": "destroy"})


@pytest.mark.unit
class TestInterruptData:
    def test_defaults(self):
        d = InterruptData()
        assert d.source == "?"
        assert d.event_type == "interrupt"


@pytest.mark.unit
class TestStateChangeData:
    def test_missing_request_id_rejected(self):
        with pytest.raises(ValidationError):
            StateChangeData.model_validate({"event_type": "state_change", "state": "idle"})


@pytest.mark.unit
class TestTaskResultData:
    def test_missing_required_fields_rejected(self):
        with pytest.raises(ValidationError):
            TaskResultData.model_validate(
                {"event_type": "task_result", "task_id": "t1"})


@pytest.mark.unit
class TestUserInputData:
    def test_stt_path_no_content_valid(self):
        d = UserInputData(text="hi", user_name="妖梦", request_id="r1",
                          emotion="happy", events=["laugh"])
        assert d.content is None

    def test_text_path_no_emotion_valid(self):
        d = UserInputData(text="hi", user_name="妖梦", request_id="r1",
                          content=[{"type": "text", "text": "hi"}])
        assert d.emotion == ""
        assert d.events == []


@pytest.mark.unit
class TestSystemTickData:
    def test_invalid_reason_rejected(self):
        with pytest.raises(ValidationError):
            SystemTickData.model_validate(
                {"event_type": "system_tick", "reason": "schedule"})
        # Literal 大小写敏感——"time_of_day" 在 Literal 白名单中，用非法值 "schedule" 测试拒绝

    def test_silence_timeout_valid(self):
        d = SystemTickData(reason="silence_timeout", elapsed=30.0)
        assert d.elapsed == 30.0
        assert d.time is None

    def test_time_of_day_valid(self):
        d = SystemTickData(reason="time_of_day", time="08:00")
        assert d.time == "08:00"
        assert d.elapsed is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `conda run -n Elysia python -m pytest core/schema/tests/test_event.py -q --tb=line`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.schema.event'`

- [ ] **Step 3: 实现 `core/schema/event.py`**

```python
"""事件数据类型 — EventBus 的 Pydantic 模型。

每个模型自带 event_type discriminator，形成 EventData union。
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class InterruptData(BaseModel):
    event_type: Literal["interrupt"] = "interrupt"
    source: str = Field(default="?")


class StateChangeData(BaseModel):
    event_type: Literal["state_change"] = "state_change"
    state: str
    request_id: str


class UserInputData(BaseModel):
    event_type: Literal["user_input"] = "user_input"
    text: str
    user_name: str
    request_id: str
    content: list[dict] | None = None
    emotion: str = ""
    events: list[str] = Field(default_factory=list)


class SystemTickData(BaseModel):
    event_type: Literal["system_tick"] = "system_tick"
    reason: Literal["silence_timeout", "time_of_day"]
    elapsed: float | None = None
    time: str | None = None


class TaskResultData(BaseModel):
    event_type: Literal["task_result"] = "task_result"
    task_id: str
    result: str
    original_intent: str
    dispatched_at: str


EventData = Annotated[
    InterruptData | StateChangeData | UserInputData | SystemTickData | TaskResultData,
    Field(discriminator="event_type"),
]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `conda run -n Elysia python -m pytest core/schema/tests/test_event.py -q --tb=short`
Expected: 11 passed

- [ ] **Step 5: 注册 `core/schema/__init__.py`**

当前最后一个 import block 是 `from core.schema.memory import (...)`（L61-69），在其后追加：

```python
from core.schema.event import (  # noqa: F401
    EventData,
    InterruptData,
    StateChangeData,
    UserInputData,
    SystemTickData,
    TaskResultData,
)
```

- [ ] **Step 6: 注册 `core/schema/README.md`**

目录树（文字锚 `memory.py` 之后）追加：

```
├── event.py    # EventData union + 5 种事件 data 模型（EventBus Pydantic 类型）
```

模块速查表（`memory.py` 行之后）追加：

```
| `event.py` | EventData, InterruptData/StateChangeData/UserInputData/SystemTickData/TaskResultData — EventBus 事件 data 类型 |
```

- [ ] **Step 7: 验证 re-export + schema 全量**

```bash
conda run -n Elysia python -c "from core.schema import EventData, InterruptData, StateChangeData; print('OK')"
conda run -n Elysia python -m pytest core/schema/tests/ -q --tb=short
```
Expected: `OK`；schema tests 全 passed

- [ ] **Step 8: Commit**

```bash
git add core/schema/event.py core/schema/tests/test_event.py core/schema/__init__.py core/schema/README.md
git commit -m "feat: add core/schema/event.py — EventData union + 5 种事件 data 模型"
```

---

### Task 2: `core/event_bus.py` — Event + post() 类型化

**Files:**
- Modify: `core/event_bus.py`

**Interfaces:**
- Consumes: `EventData` from `core.schema.event`
- Produces: `Event.data: EventData | None`；`EventBus.post(data: EventData, *, type: str | None = None)`

- [ ] **Step 1: 更新 `core/event_bus.py`**

```python
# Before — 完整文件（L1-60），三处改动
from dataclasses import dataclass, field
from typing import Any                       # L10

from .schema import EventPriority, EventType  # noqa: F401 — re-export  # L12

@dataclass(order=True)
class Event:
    priority: int = field(compare=True)
    type: str = field(compare=False)
    data: Any = field(compare=False, default=None)   # L19
    seq: int = field(compare=True, default=0)

    def post(self, type: str, data: Any = None,          # L33
             priority: int = EventPriority.NORMAL) -> None:
        self._seq += 1                                   # L35
        event = Event(priority=priority, type=type, data=data, seq=self._seq)

# After
from dataclasses import dataclass, field
from typing import Any

from .schema import EventPriority, EventType  # noqa: F401
from .schema.event import EventData  # noqa: F401

@dataclass(order=True)
class Event:
    priority: int = field(compare=True)
    type: str = field(compare=False)
    data: EventData | None = field(compare=False, default=None)
    seq: int = field(compare=True, default=0)

    def post(
        self, data: EventData, priority: int = EventPriority.NORMAL,
        *, type: str | None = None,
    ) -> None:
        """Post a typed event. type 默认从 data.event_type 推导；显式传 type 覆盖。"""
        self._seq += 1
        event_type = type if type is not None else data.event_type  # noqa: SIM108
        event = Event(priority=priority, type=event_type, data=data, seq=self._seq)
        self._queue.put_nowait(event)
```

`from typing import Any` 保留——`next()`/`drain()` 返回 `Event`，内部类型标注不依赖 `Any`，但删掉零收益，保留零风险。

- [ ] **Step 2: 验证 import + 基本类型**

```bash
conda run -n Elysia python -c "
from core.event_bus import EventBus, Event, EventData
from core.schema.event import InterruptData
b = EventBus()
b.post(InterruptData(source='test'))
e = b.drain()[0]
assert isinstance(e.data, InterruptData)
assert e.type == 'interrupt'
print('OK')
"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add core/event_bus.py
git commit -m "refactor: Event.data Any → EventData | None, post() type 自动推导"
```

---

### Task 3: Producer 端 — 16 处 + 3 测试文件更新

**Files:**
- Modify: `core/idle_monitor.py`（2 处）
- Modify: `core/cognitive_loop.py`（9 处）
- Modify: `core/input/server.py`（2 处）
- Modify: `core/agent.py`（4 处 producer；consumer 留给 Task 4）
- Modify: `core/tests/test_phase1.py`（3 处）
- Modify: `core/tests/test_task_scheduler.py`（1 处）

**Interfaces:**
- Consumes: `SystemTickData`, `StateChangeData`, `InterruptData`, `UserInputData`, `TaskResultData` from `core.schema.event`（import 到使用文件中）

- [ ] **Step 1: `core/idle_monitor.py` — 2 处**

import 区（L11 之后）追加：

```python
from core.schema.event import SystemTickData
```

```python
# L84-88 — silence_timeout
# Before
self._bus.post(
    EventType.SYSTEM_TICK,
    {"reason": "silence_timeout", "elapsed": elapsed},
    priority=EventPriority.NORMAL,
)
# After
self._bus.post(
    SystemTickData(reason="silence_timeout", elapsed=elapsed),
    priority=EventPriority.NORMAL,
)

# L97-101 — time_of_day
# Before
self._bus.post(
    EventType.SYSTEM_TICK,
    {"reason": "time_of_day", "time": now},
    priority=EventPriority.NORMAL,
)
# After
self._bus.post(
    SystemTickData(reason="time_of_day", time=now),
    priority=EventPriority.NORMAL,
)
```

- [ ] **Step 2: `core/cognitive_loop.py` — 9 处 STATE_CHANGE**

import 区（L46 之后）追加：

```python
from core.schema.event import StateChangeData
```

全部 9 处 `self._bus.post(EventType.STATE_CHANGE, {...}, ...)` 改为 `self._bus.post(StateChangeData(...), ...)`：

| 行 | state 值 | request_id 来源 | priority |
|----|---------|----------------|----------|
| L216 | `"thinking"` | `rid`（局部变量） | `NORMAL` |
| L364 | `"speaking"` | `get_request_id()` | `STATE_CHANGE` |
| L503 | `"speaking"` | `get_request_id()` | `STATE_CHANGE` |
| L520 | `"executing"` | `get_request_id()` | `STATE_CHANGE` |
| L658 | `"thinking"` | `get_request_id()` | `STATE_CHANGE` |
| L996 | `"idle_background_task"` | `get_request_id()` | `STATE_CHANGE` |
| L1006 | `"idle"` | `get_request_id()` | `STATE_CHANGE` |
| L1020 | `"interrupted"` | `get_request_id()` | `STATE_CHANGE` |
| L1023 | `"idle"` | `get_request_id()` | `STATE_CHANGE` |

每处 Before/After 模式相同（以 L216 为例）：

```python
# Before
self._bus.post(EventType.STATE_CHANGE, {"state": "thinking", "request_id": rid},
               priority=EventPriority.NORMAL)

# After
self._bus.post(StateChangeData(state="thinking", request_id=rid),
               priority=EventPriority.NORMAL)
```

其余 8 处同理——只改 dict → `StateChangeData(...)`，其余不变。

- [ ] **Step 3: `core/input/server.py` — 2 处**

import 区（L15-17 区域的 `from core.event_bus import EventType, EventPriority` 之后）追加：

```python
from core.schema.event import InterruptData, UserInputData
```

```python
# L1026 — _do_interrupt
# Before
agent._bus.post(EventType.INTERRUPT, {"source": "websocket"}, priority=EventPriority.INTERRUPT)
# After
agent._bus.post(InterruptData(source="websocket"), priority=EventPriority.INTERRUPT)

# L1054-1058 — _do_transcription（USER_INPUT STT 路径）
# Before
agent._bus.post(EventType.USER_INPUT, {
    "text": text, "user_name": agent._user_name,
    "emotion": emotion, "events": events,
    "request_id": rid,
})
# After
agent._bus.post(UserInputData(
    text=text, user_name=agent._user_name,
    emotion=emotion, events=events,
    request_id=rid,
))
```

- [ ] **Step 4: `core/agent.py` — 4 处 producer（consumer 留给 Task 4）**

import 区追加：

```python
from core.schema.event import (
    InterruptData, UserInputData, TaskResultData, StateChangeData, SystemTickData,
)
```

**A. `process_message` — USER_INPUT 文本路径（L749-751）：**

```python
# Before
self._bus.post(EventType.USER_INPUT, {"text": text, "user_name": user_name,
                                       "request_id": rid,
                                       "content": content})
# After
self._bus.post(UserInputData(text=text, user_name=user_name,
                             request_id=rid, content=content))
```

**B. `_poll_mailboxes` — TASK_RESULT completed（L905-910）：**

```python
# Before
self._bus.post(EventType.TASK_RESULT, {
    "task_id": task_id,
    "result": data.get("result", ""),
    "original_intent": info.intent,
    "dispatched_at": info.dispatched_at,
})
# After
self._bus.post(TaskResultData(
    task_id=task_id,
    result=data.get("result", ""),
    original_intent=info.intent,
    dispatched_at=info.dispatched_at,
))
```

**C. `_poll_mailboxes` — TASK_RESULT failed（L914-918）：**

```python
# Before
self._bus.post(EventType.TASK_RESULT, {
    "task_id": task_id,
    "result": f"任务执行失败: {data.get('error', '未知错误')}",
    "original_intent": info.intent,
    "dispatched_at": info.dispatched_at,
})
# After
self._bus.post(TaskResultData(
    task_id=task_id,
    result=f"任务执行失败: {data.get('error', '未知错误')}",
    original_intent=info.intent,
    dispatched_at=info.dispatched_at,
))
```

**D. `_handle_state_change` — pending message re-post（L1041）：**

```python
# Before
self._bus.post(EventType.USER_INPUT, asdict(pending))
# After — _PendingMessage 字段与 UserInputData 一一对应
self._bus.post(UserInputData(
    text=pending.text,
    user_name=pending.user_name,
    request_id=pending.request_id,
    content=pending.content,
))
```

- [ ] **Step 5: `core/tests/test_phase1.py` — 3 处 + 1 处 mock**

```python
# L30 — 现有：bus.post("interrupt", "data2", ...) 传字符串 "data2"
# 改为传 InterruptData:
bus.post("interrupt", InterruptData(), priority=EventPriority.INTERRUPT)
# 注意：此处 bus 是裸 EventBus 实例而非 agent._bus，task_2 的 post() 签名改了——
# 不再接受 bare str 作为 data，需要传 typed 对象

# L334, L356, L390 — 每个都是 agent._bus.post("interrupt", {"source": "test"}, priority=0)
# 改为：
agent._bus.post(InterruptData(source="test"), priority=0)
```

import 区追加：`from core.schema.event import InterruptData`

**L37 — 旧断言 `e1.type == "interrupt"` 不受影响**（`type` 字段仍然是 `str`）。

- [ ] **Step 6: `core/tests/test_task_scheduler.py` — 1 处**

```python
# L838-839
# Before
agent._bus.post(EventType.INTERRUPT, {"source": "test"},
                priority=EventPriority.INTERRUPT)

# After
agent._bus.post(InterruptData(source="test"),
                priority=EventPriority.INTERRUPT)
```

import 区追加：`from core.schema.event import InterruptData`

- [ ] **Step 7: 跑 producer 端相关测试**

```bash
conda run -n Elysia python -m pytest core/tests/test_phase1.py core/tests/test_task_scheduler.py::test_agent_interrupt_clears_queue -q --tb=short -m "not slow"
```
Expected: 全 passed（假设 `test_phase3.py::test_agent_interrupt_clears_queue` 通过了。若语法/import 错，会在此处暴露）

- [ ] **Step 8: Commit**

```bash
git add core/idle_monitor.py core/cognitive_loop.py core/input/server.py core/agent.py core/tests/test_phase1.py core/tests/test_task_scheduler.py
git commit -m "refactor: 16 处 EventBus producer dict → typed EventData model"
```

---

### Task 4: Consumer 端 — handlers + _process_events 分发 + _handle_system_tick TODO

**Files:**
- Modify: `core/agent.py`（6 handler 签名 + `_process_events` dispatch）

**Interfaces:**
- Consumes: `EventData` union（通过 `isinstance` 分发）；所有 handler 签名改为 typed

- [ ] **Step 1: 更新 `_process_events` 分发（L931-957）**

```python
# Before
while self._state == RunState.RUNNING:
    event = await self._bus.next()
    rid = (event.data or {}).get("request_id", "")
    if rid:
        set_request_id(rid)
    if event.type == EventType.INTERRUPT:
        self._handle_interrupt(event.data or {})
    elif event.type == EventType.USER_INPUT:
        self._handle_user_input(event.data or {})
    elif event.type == EventType.SYSTEM_TICK:
        self._handle_system_tick(event.data or {})
    elif event.type == EventType.TASK_RESULT:
        self._handle_task_result(event.data or {})
    elif event.type == EventType.STATE_CHANGE:
        await self._handle_state_change(event.data or {})

# After
while self._state == RunState.RUNNING:
    event = await self._bus.next()
    data = event.data
    if data is None:
        continue
    rid = getattr(data, "request_id", "")
    if rid:
        set_request_id(rid)
    if isinstance(data, InterruptData):
        self._handle_interrupt(data)
    elif isinstance(data, UserInputData):
        self._handle_user_input(data)
    elif isinstance(data, SystemTickData):
        self._handle_system_tick(data)
    elif isinstance(data, TaskResultData):
        self._handle_task_result(data)
    elif isinstance(data, StateChangeData):
        await self._handle_state_change(data)
```

- [ ] **Step 2: `_handle_interrupt`（L959-970）**

```python
# Before
def _handle_interrupt(self, data: dict) -> None:
    set_request_id("-")
    logger.info("EventBus: INTERRUPT (%s)", data.get("source", "?") if data else "?")
    self._cognitive.cancel_current()
    for drained in self._bus.drain():
        if drained.type == EventType.STATE_CHANGE:
            self._bus.post(drained.type, drained.data, priority=drained.priority)
    self._pending_messages.clear()

# After — drain+re-post 保留 STATE_CHANGE，传 type= 覆盖
def _handle_interrupt(self, data: InterruptData) -> None:
    set_request_id("-")
    logger.info("EventBus: INTERRUPT (%s)", data.source)
    self._cognitive.cancel_current()
    for drained in self._bus.drain():
        if isinstance(drained.data, StateChangeData):
            self._bus.post(drained.data, priority=drained.priority,
                           type=EventType.STATE_CHANGE)
    self._pending_messages.clear()
```

- [ ] **Step 3: `_handle_user_input`（L972-993）**

```python
# Before
def _handle_user_input(self, data: dict) -> None:
    text = data.get("text", "")
    user_name = data.get("user_name", self._user_name)
    stt_emotion = data.get("emotion", "")
    stt_events = data.get("events", [])
    content = data.get("content")
    rid = data.get("request_id", "")

# After
def _handle_user_input(self, data: UserInputData) -> None:
    text = data.text
    user_name = data.user_name
    stt_emotion = data.emotion
    stt_events = data.events
    content = data.content
    rid = data.request_id
```

（其余函数体不变——`logger.info(...)`、`_on_tool_calls`、`asyncio.create_task(...)` 等保持原样。）

- [ ] **Step 4: `_handle_system_tick`（L995-1012）— 加 TODO**

```python
# Before
def _handle_system_tick(self, data: dict) -> None:
    """处理 SYSTEM_TICK 事件——silence_timeout 时主动开启话题。"""
    reason = data.get("reason", "")
    if reason == "silence_timeout":
        elapsed = data.get("elapsed", 0)

# After
def _handle_system_tick(self, data: SystemTickData) -> None:
    """处理 SYSTEM_TICK 事件——silence_timeout 时主动开启话题。"""
    # TODO: time_of_day 事件未处理——producer 在 idle_monitor._check_proactive 中，
    #       此处只消费 silence_timeout。time_of_day 事件静默丢弃，等待后续实现。
    if data.reason == "silence_timeout":
        elapsed = data.elapsed or 0
```

（其余函数体不变。）

- [ ] **Step 5: `_handle_task_result`（L1014-1026）**

```python
# Before
def _handle_task_result(self, data: dict) -> None:
    ...
    task = asyncio.create_task(
        self._cognitive.process(
            user_message="",
            is_task_result=True,
            task_result_context=data,
        ),
        name=f"task-result-{data.get('task_id', 'unknown')}",
    )

# After — model_dump() 传给下游（_build_wake_up_message 签名为 ctx: dict，用 .get() 访问）
def _handle_task_result(self, data: TaskResultData) -> None:
    ...
    task = asyncio.create_task(
        self._cognitive.process(
            user_message="",
            is_task_result=True,
            task_result_context=data.model_dump(),
        ),
        name=f"task-result-{data.task_id}",
    )
```

- [ ] **Step 6: `_handle_state_change`（L1028-1042）**

```python
# Before
async def _handle_state_change(self, data: dict) -> None:
    state = data.get("state", "") if data else ""

# After
async def _handle_state_change(self, data: StateChangeData) -> None:
    state = data.state
```

（其余函数体不变。）

- [ ] **Step 7: 跑 consumer 端相关测试**

```bash
conda run -n Elysia python -m pytest core/tests/test_phase1.py core/tests/test_task_scheduler.py::test_agent_interrupt_clears_queue -q --tb=short -m "not slow"
```
Expected: 全 passed

- [ ] **Step 8: Commit**

```bash
git add core/agent.py
git commit -m "refactor: EventBus consumer isinstance 分发 + handler 签名 typed + time_of_day TODO"
```

---

### Task 5: 最终验证

**Files:** 无改动

- [ ] **Step 1: 目标套件全绿**

```bash
conda run -n Elysia python -m pytest core/schema/tests/ core/tests/test_phase1.py core/tests/test_task_scheduler.py::test_agent_interrupt_clears_queue -q --tb=short -m "not slow"
```
Expected: 全 passed

- [ ] **Step 2: import 一致性**

```bash
conda run -n Elysia python -c "
from core.schema import EventData, InterruptData, StateChangeData, UserInputData, SystemTickData, TaskResultData
from core.event_bus import EventBus, Event
from core.idle_monitor import IdleMonitor
from core.cognitive_loop import CognitiveLoop
from core.agent import Agent
print('All imports OK')
"
```
Expected: `All imports OK`

- [ ] **Step 3: `make test-normal` 基线比对（与上轮 Task 0 模式相同）**

```bash
make test-normal 2>&1 | tail -30 | tee /tmp/eventbus-final.txt
grep '^FAILED' /tmp/eventbus-final.txt | sort > /tmp/eventbus-final-failures.txt

# 若上次基线文件存在则 diff，否则统计失败数人工比对 ~12 预期值
if [ -f /tmp/reflector-baseline-failures.txt ]; then
  diff /tmp/reflector-baseline-failures.txt /tmp/eventbus-final-failures.txt && echo "无新增失败"
else
  echo "本次失败数: $(wc -l < /tmp/eventbus-final-failures.txt)" "（预期 ~12 个 pre-existing）"
fi
```

Expected: diff 无 `>` 行（无新增失败）。注意：`reflector-baseline-failures.txt` 是上轮 Task 0 保存的基线——若环境漂移导致 ±1，人工判断是否与本次改动相关。

---

## Self-Review 记录

- **Spec coverage**：Section 一→Task 1；二→Task 2；三（re-export）→Task 1 Step 5；四→Task 3；五→Task 4；注册面→Task 1 Steps 5-6；测试三块→Tasks 1/3/4；现有测试更新→Task 3 Steps 5-6；agent handler 等作为 Task 4 内的完整步骤
- **类型一致性**：`EventData` union 定义在 Task 1，`post(data: EventData)` 在 Task 2，所有 producer import 在 Task 3，`isinstance(data, XxxData)` 在 Task 4——dispatch 链一致
- **顺序依赖**：Task 2 依赖 Task 1（`EventData`）；Task 3 依赖 Task 2（`post()` 新签名）；Task 4 依赖 Task 3（producer 改完后 consumer 才能正常运转）——1→2→3→4 顺序执行
