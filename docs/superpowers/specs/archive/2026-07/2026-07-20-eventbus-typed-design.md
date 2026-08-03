# EventBus 事件数据类型化：消灭 `data: Any` + ad-hoc dict

## Context

EventBus 的 `post(type, data: Any)` 传输 5 种事件，各用 ad-hoc dict：

```python
self._bus.post(EventType.INTERRUPT, {"source": "websocket"})
self._bus.post(EventType.STATE_CHANGE, {"state": "thinking", "request_id": rid})
self._bus.post(EventType.USER_INPUT, {"text": text, "emotion": "", ...})
```

消费者全用 `data.get()` 访问，key 拼写错误 IDE 零提示。和前三轮（LLM 输出裸 dict）不同，这次的生产者和消费者都是**人类代码**——shape 不会随机变化，但分散在 4 个文件里，producer 和 consumer 之间的字段约定靠注释和记忆维持。

**已发现 bug**：`SYSTEM_TICK` 的 `time_of_day` 变体（`idle_monitor.py:97`）consumer 端完全未处理（`_handle_system_tick` 只匹配 `silence_timeout`）——静默吞事件，标记 TODO 后续修复。

## Goals

1. 新增 `core/schema/event.py`：5 个事件 data 模型 + `EventData` discriminated union
2. `Event.data: Any` → `Event.data: EventData | None`
3. `EventBus.post(type, data=dict)` → `bus.post(XxxData(...))`，`type` 从 `data.event_type` 自动推导
4. Consumer 端 `isinstance` 分发 + handler 签名 typed + 所有 `.get()` → 属性访问
5. `_handle_system_tick` 加 TODO 注释标记 `time_of_day` 未消费

## Non-Goals

- **不修** `time_of_day` 的 consumer 缺失（纯 TODO 标记，不实现功能）
- **不改** `hitl_request` / `hitl_cancel` — 未激活的 stub（`protocol.py:68`），无意激活
- **不改** `EventPriority` / `EventType` enum
- **不改** `EventBus.next()` / `drain()` / `pending_count` 签名
- **不改** IdleMonitor / CognitiveLoop 逻辑——只改 data 构造形式

---

## Design

### 转换边界

```
producer                         bus.post(typed_data)                  consumer
────────                         ─────────────────────                ────────
idle_monitor.py ────┐           ★ 转换点：type 从 data 推导          _process_events()
cog_loop.py ────────┤           Event.data: Any → EventData│None      isinstance(event.data, Xxx)
server.py ──────────┤           dict → Pydantic model                  handler(typed_data)
agent.py ───────────┘                                                 .get() → 属性访问
```

### 一、`core/schema/event.py` — 新增

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
    elapsed: float | None = None     # silence_timeout 专用
    time: str | None = None           # time_of_day 专用
    # TODO: time_of_day 事件 consumer 端未处理（_handle_system_tick 只匹配 silence_timeout）

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

要点：
- 每个模型自带 `event_type: Literal["xxx"] = "xxx"` ——构造时自动填值，无需显式传递；discriminator 直接读它
- `InterruptData.source` 默认 `"?"`（匹配旧 `data.get("source", "?")`）
- `UserInputData.emotion` 默认 `""`（文本路径无 emotion）；`content` 默认 `None`（STT 路径无 content）
- `SystemTickData.reason` 严格 `Literal["silence_timeout", "time_of_day"]`——枚举约束，拼错在构造时即报错；`elapsed` / `time` 均为 `| None`
- `TaskResultData` 无默认字段——所有 producer 始终提供全部键

### 二、`core/event_bus.py` — `Event` dataclass + `post()` 签名

```python
# Before
@dataclass(order=True)
class Event:
    priority: int = field(compare=True)
    type: str = field(compare=False)
    data: Any = field(compare=False, default=None)
    seq: int = field(compare=True, default=0)

class EventBus:
    def post(self, type: str, data: Any = None, priority: int = EventPriority.NORMAL) -> None:
        self._seq += 1
        event = Event(priority=priority, type=type, data=data, seq=self._seq)
        self._queue.put_nowait(event)

# After — type 从 data.event_type 自动推导；保留显式覆盖路径
from .schema.event import EventData

@dataclass(order=True)
class Event:
    priority: int = field(compare=True)
    type: str = field(compare=False)
    data: EventData | None = field(compare=False, default=None)
    seq: int = field(compare=True, default=0)

class EventBus:
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

- `post()` 的 `type` 参数变为 **keyword-only**（`*` 之后）、可选——日常使用 `bus.post(XxxData(...))` 即自动推导
- 保留显式 `type=` 覆盖路径——`_handle_interrupt` 中的 drain + re-post 场景需要它
- `post()` 不再接受 `dict`——必须传 `EventData` 子类实例（构造即校验）

### 三、`core/event_bus.py` — re-export 清理

```python
# Before（L12）
from .schema import EventPriority, EventType  # noqa: F401 — re-export

# After — 保持相对导入风格，EventData 取自同级的 schema 子包
from .schema import EventPriority, EventType  # noqa: F401
from .schema.event import EventData  # noqa: F401
```

（`EventPriority` / `EventType` 保持原有 re-export）

### 四、Producer 改动

#### `core/idle_monitor.py`（L84-88 / L97-101）

```python
# Before — silence_timeout
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

# Before — time_of_day
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

import 区新增：`from core.schema.event import SystemTickData`

#### `core/cognitive_loop.py`（9 处 `STATE_CHANGE`）

全部统一模式——`bus.post(EventType.STATE_CHANGE, dict, ...)` → `bus.post(StateChangeData(...), ...)`：

```python
# Before（示例，共 9 处，state 值不同）
self._bus.post(EventType.STATE_CHANGE, {"state": "thinking", "request_id": rid},
               priority=EventPriority.NORMAL)
self._bus.post(EventType.STATE_CHANGE,
               {"state": "speaking", "request_id": get_request_id()},
               priority=EventPriority.STATE_CHANGE)
# ... (executing / thinking / idle_background_task / idle / interrupted / idle 共 7 处类似)

# After
self._bus.post(StateChangeData(state="thinking", request_id=rid),
               priority=EventPriority.NORMAL)
self._bus.post(StateChangeData(state="speaking", request_id=get_request_id()),
               priority=EventPriority.STATE_CHANGE)
# ... (其余 7 处同理)
```

import 区新增：`from core.schema.event import StateChangeData`

#### `core/input/server.py`（L1026 / L1054-1058）

```python
# Before（L1026 — INTERRUPT）
agent._bus.post(EventType.INTERRUPT, {"source": "websocket"}, priority=EventPriority.INTERRUPT)

# After
agent._bus.post(InterruptData(source="websocket"), priority=EventPriority.INTERRUPT)

# Before（L1054-1058 — USER_INPUT STT 路径）
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

import 区新增：`from core.schema.event import InterruptData, UserInputData`

#### `core/agent.py`（L749-751 / L905-910 / L914-918 / L1041）

**`process_message` — USER_INPUT 文本路径：**

```python
# Before（L749-751）
self._bus.post(EventType.USER_INPUT, {"text": text, "user_name": user_name,
                                       "request_id": rid,
                                       "content": content})

# After
self._bus.post(UserInputData(text=text, user_name=user_name,
                             request_id=rid, content=content))
```

**`_poll_mailboxes` — TASK_RESULT（completed / failed）：**

```python
# Before（L905-910 — completed）
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

# Before（L914-918 — failed）
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

**`_handle_state_change` — pending 消息重新入队：**

`_handle_state_change` 中 `asdict(pending)` 产生 `dict`（`_PendingMessage` dataclass），需要改造为构造 `UserInputData`：

```python
# Before（L1041）
self._bus.post(EventType.USER_INPUT, asdict(pending))

# After — _PendingMessage 字段与 UserInputData 一一对应
self._bus.post(UserInputData(
    text=pending.text,
    user_name=pending.user_name,
    request_id=pending.request_id,
    content=pending.content,
))
```

`agent.py` import 区新增：`from core.schema.event import InterruptData, UserInputData, TaskResultData, StateChangeData, SystemTickData`

### 五、Consumer 改动（`core/agent.py`）

#### `_process_events`（L931-957）

```python
# Before
while self._state == RunState.RUNNING:
    event = await self._bus.next()
    # 从事件数据提取 request_id 并设置 ContextVar
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

# After — isinstance 分发，event.data 已 typed
while self._state == RunState.RUNNING:
    event = await self._bus.next()
    data = event.data
    if data is None:
        continue

    # 提取 request_id（所有带 request_id 的模型统一提取）
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

#### `_handle_interrupt`（L959-970）

```python
# Before
def _handle_interrupt(self, data: dict) -> None:
    """处理 INTERRUPT 事件：cancel cognitive task，清空队列。"""
    set_request_id("-")
    logger.info("EventBus: INTERRUPT (%s)", data.get("source", "?") if data else "?")
    self._cognitive.cancel_current()
    for drained in self._bus.drain():
        if drained.type == EventType.STATE_CHANGE:
            self._bus.post(drained.type, drained.data, priority=drained.priority)
    self._pending_messages.clear()

# After — drain+re-post 保留 STATE_CHANGE
def _handle_interrupt(self, data: InterruptData) -> None:
    """处理 INTERRUPT 事件：cancel cognitive task，清空队列。"""
    set_request_id("-")
    logger.info("EventBus: INTERRUPT (%s)", data.source)
    self._cognitive.cancel_current()
    for drained in self._bus.drain():
        if isinstance(drained.data, StateChangeData):
            self._bus.post(drained.data, priority=drained.priority,
                           type=EventType.STATE_CHANGE)
    self._pending_messages.clear()
```

注意 drain + re-post 传 `type=EventType.STATE_CHANGE`——显式覆盖 `drained.data.event_type`，因为 Event 的 `type` 字段语义与 `data.event_type` 并非 100% 同一（`STATE_CHANGE` 的 `drained.type` 是 `"state_change"`，`data.event_type` 也是 `"state_change"`——此处一致但保留显式覆盖作为防御性设计）。

#### `_handle_user_input`（L972-993）

```python
# Before
def _handle_user_input(self, data: dict) -> None:
    text = data.get("text", "")
    user_name = data.get("user_name", self._user_name)
    stt_emotion = data.get("emotion", "")
    stt_events = data.get("events", [])
    content = data.get("content")
    ...

# After
def _handle_user_input(self, data: UserInputData) -> None:
    text = data.text
    user_name = data.user_name
    stt_emotion = data.emotion
    stt_events = data.events
    content = data.content
    ...
```

#### `_handle_system_tick`（L995-1012）— 保留 `time_of_day` bug，标记 TODO

```python
# Before
def _handle_system_tick(self, data: dict) -> None:
    """处理 SYSTEM_TICK 事件——silence_timeout 时主动开启话题。"""
    reason = data.get("reason", "")
    if reason == "silence_timeout":
        elapsed = data.get("elapsed", 0)
        ...

# After
def _handle_system_tick(self, data: SystemTickData) -> None:
    """处理 SYSTEM_TICK 事件——silence_timeout 时主动开启话题。"""
    # TODO: time_of_day 事件未处理——producer 在 idle_monitor._check_proactive 中，
    #       此处只消费 silence_timeout。time_of_day 事件静默丢弃，等待后续实现。
    if data.reason == "silence_timeout":
        elapsed = data.elapsed or 0
        ...
```

#### `_handle_task_result`（L1014-1026）

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

# After — model_dump() 传给下游（_build_wake_up_message 签名为 ctx: dict）
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

# 下游 _build_wake_up_message（cognitive_loop.py:847-860）保持 ctx: dict 签名，
# 继续使用 ctx.get("original_intent", "") / ctx.get("result", "")——不受影响
```

#### `_handle_state_change`（L1028-1042）

```python
# Before
async def _handle_state_change(self, data: dict) -> None:
    state = data.get("state", "") if data else ""
    ...

# After
async def _handle_state_change(self, data: StateChangeData) -> None:
    state = data.state
    ...
```

---

## 注册面

### `core/schema/__init__.py`

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

### `core/schema/README.md`

- 目录树新增 `event.py` 行
- 模块速查表新增一行：`event.py` — EventData union + 5 种事件 data 模型（EventBus Pydantic 类型）

---

## 测试

### 新增 `core/schema/tests/test_event.py`（`@pytest.mark.unit`）

参照 `test_memory.py` / `test_memory_reflector.py` 风格：

- `EventData` union dispatch：`TypeAdapter(EventData).validate_python` 按 `event_type` 分发到 5 种子类
- `InterruptData()` 无参构造合法（`source` 默认 `"?"`）
- `StateChangeData(state="x")` 缺 `request_id` → ValidationError
- `TaskResultData(task_id="t1")` 缺必填字段 → ValidationError
- 每个子类字段默认值校验（`InterruptData.source` 默认 `"?"`、`UserInputData.emotion` 默认 `""`、`content` 默认 `None`）
- `SystemTickData.reason` 非法值 → ValidationError
- `UserInputData` STT 变体（缺 `content`）合法；文本变体（缺 `emotion`）合法
- 不需要的字段不被接受（如 `SystemTickData(elapsed=5)` 不能多传 `time` 以外的无关字段——Pydantic 默认拒绝 extra）

### 测试计划阶段额外核实项

- `core/tests/test_event.py`（`@pytest.mark.unit`）— Event 构造 + post 基本调用测试
- agent.py handler mock 测试（`@pytest.mark.service`）— typed handler 收到正确子类

### 现有测试更新（INTERRUPT dict → typed model）

以下测试文件中 `bus.post("interrupt", {"source": ...})` 调用需更新：

| 文件 | 行 | 改动 |
|------|-----|------|
| `core/tests/test_phase1.py` | L334, L356, L390 | `{"source": "test"}` → `InterruptData(source="test")` |
| `core/tests/test_task_scheduler.py` | L838-839 | 同上 |
| `core/input/test_server.py` | L289, L304 | 断言 `args[0] == EventType.INTERRUPT` 可能失效（`type` 不再作为首个位置参数）；plan 阶段核实具体断言模式并更新

---

## 不改的文件清单

| 文件 | 原因 |
|------|------|
| `core/schema/agent.py` | `EventType` / `EventPriority` enum 不变 |
| `Module/task_system/protocol.py` | `hitl_request` / `hitl_cancel` stub 不激活 |
| `core/llm_adapter.py` | 不相关 |
| 前三轮已完成的文件 | 不相关 |

---

## 收益

1. **`data: Any` → `data: EventData | None`** — 类型安全，IDE 补全
2. **`data.get("source", "?")` → `data.source`** — 属性访问，拼写错误编译期捕获
3. **`post(type, data=dict)` → `post(XxxData(...))`** — `type` 从 `data.event_type` 推导，消除 `type` 与 data 内容不一致的可能
4. **`isinstance(data, SystemTickData)`** — IDE 自动推断 handler 内字段
5. **`reason: Literal["silence_timeout", "time_of_day"]`** — 枚举约束，拼错在 producer 构造时即报错
6. **`time_of_day` bug 显式标记** — TODO 注释写入 spec 和代码，后续可发现
