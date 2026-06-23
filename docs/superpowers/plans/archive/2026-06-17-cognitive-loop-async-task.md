# CognitiveLoop 异步任务支持实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CognitiveLoop 支持 submit_task 异步工具——LLM 调用后 fire-and-forget，TaskSystem 后台执行，完成后渐进式唤醒。

**Architecture:** submit_task 的 function definition 在 tool_defs 构建时合并（不注册为 ToolPlugin）；CognitiveLoop 在工具执行阶段拦截 submit_task，调用 DelegationPreparer → TaskSystem.submit；Agent 轮询 active mailboxes，COMPLETED 时投递 TASK_RESULT 事件；CognitiveLoop 收到 is_task_result 后走渐进式唤醒模板。

**Tech Stack:** Python 3.13 asyncio, 现有 EventBus + FSM + ToolPlugin 架构, TaskSystem + DelegationPreparer（已实现）

## Global Constraints

- 不改变现有同步工具执行路径
- FSM 状态转换仅新增，不修改已有转换
- submit_task 仅影响 _think_loop 工具执行后半段
- TaskSystem 和 DelegationPreparer 已实现，本次只集成不修改

---

### Task 1: Schema 层 — FSM + EventBus 新状态

**Files:**
- Modify: `core/schema.py:14-28` (AgentState + VALID_TRANSITIONS)
- Modify: `core/schema.py:43-48` (EventType)

**Interfaces:**
- Produces: `AgentState.IDLE_BACKGROUND_TASK`, `EventType.TASK_RESULT`, updated `VALID_TRANSITIONS`

- [ ] **Step 1: 添加新状态和事件**

```python
# core/schema.py

class AgentState(StrEnum):
    """Agent 认知流水线的状态。"""
    IDLE = auto()
    THINKING = auto()
    EXECUTING = auto()
    SPEAKING = auto()
    INTERRUPTED = auto()
    IDLE_BACKGROUND_TASK = auto()  # 新增：可对话，但后台有任务


VALID_TRANSITIONS: dict[AgentState, set[AgentState]] = {
    AgentState.IDLE:        {AgentState.THINKING},
    AgentState.THINKING:    {AgentState.EXECUTING, AgentState.SPEAKING, AgentState.INTERRUPTED},
    AgentState.EXECUTING:   {AgentState.THINKING, AgentState.INTERRUPTED},
    AgentState.SPEAKING:    {AgentState.IDLE, AgentState.EXECUTING, AgentState.THINKING, AgentState.INTERRUPTED, AgentState.IDLE_BACKGROUND_TASK},
    AgentState.INTERRUPTED: {AgentState.IDLE},
    AgentState.IDLE_BACKGROUND_TASK: {AgentState.THINKING, AgentState.INTERRUPTED},
}


class EventType(StrEnum):
    """事件类型 — 驱动 EventBus → FSM → CognitiveLoop 管线。"""
    INTERRUPT = "interrupt"
    STATE_CHANGE = "state_change"
    USER_INPUT = "user_input"
    SYSTEM_TICK = "system_tick"
    TASK_RESULT = "task_result"  # 新增：后台任务结果
```

- [ ] **Step 2: 更新 agent_fsm.py docstring**

```python
# core/agent_fsm.py 开头
"""Agent finite state machine.

States: IDLE → THINKING → EXECUTING → THINKING → SPEAKING → IDLE
        SPEAKING → IDLE_BACKGROUND_TASK
        IDLE_BACKGROUND_TASK → THINKING / INTERRUPTED
INTERRUPTED can be entered from any state (except IDLE).
"""
```

- [ ] **Step 3: 运行 schema 相关测试**

```bash
python -m pytest core/tests/test_phase3.py -v -k "TestAgentFSM" --asyncio-mode=auto
```

Expected: PASS（AgentFSM 测试不受影响，新状态不会被现有 FSM 转换触发）

- [ ] **Step 4: Commit**

```bash
git add core/schema.py core/agent_fsm.py
git commit -m "feat: FSM 新增 IDLE_BACKGROUND_TASK + EventBus 新增 TASK_RESULT"
```

---

### Task 2: ToolResult 新增 async_task_id + TaskSystem 新增 get_task_mailbox

**Files:**
- Modify: `Module/tool/protocol.py:418-442` (ToolResult dataclass + ok factory)
- Modify: `Module/task_system/task_system.py` (新增 `get_task_mailbox` 方法)

**Interfaces:**
- Produces: `ToolResult.async_task_id: str | None`, `TaskSystem.get_task_mailbox(task_id) -> TaskMailbox | None`
- Consumes: none

- [ ] **Step 1: ToolResult 加 async_task_id 字段**

```python
# Module/tool/protocol.py, ToolResult dataclass 末尾加:
    # 异步任务 ID（submit_task 等 fire-and-forget 工具）
    # 非空表示此工具调用了后台任务，CognitiveLoop 检测后结束当前回合
    async_task_id: str | None = None
```

`ToolResult.ok()` 工厂方法也需要加此参数：

```python
@classmethod
def ok(
    cls, name: str, content: str, duration_ms: float, truncated: bool = False,
    result_type: ResultType = "text", llm_summary: str = "",
    images: list[MediaItem] | None = None,
    audio: MediaItem | None = None,
    files: list[MediaItem] | None = None,
    async_task_id: str | None = None,  # 新增
) -> "ToolResult":
    return cls(
        success=True, content=content, tool_name=name,
        duration_ms=duration_ms, truncated=truncated,
        result_type=result_type, llm_summary=llm_summary,
        images=images or [], audio=audio, files=files or [],
        async_task_id=async_task_id,  # 新增
    )
```

- [ ] **Step 2: TaskSystem 加 get_task_mailbox 查询方法**

```python
# Module/task_system/task_system.py, TaskSystem 类

def get_task_mailbox(self, task_id: TaskId) -> TaskMailbox | None:
    """获取指定任务的 mailbox（供 Agent 轮询消费）。

    任务不存在或已清理时返回 None。
    """
    return self._mailboxes.get(task_id)
```

- [ ] **Step 3: 运行 TaskSystem 测试确认无回归**

```bash
python -m pytest Module/task_system/tests/unit/ -v --asyncio-mode=auto
```

Expected: 298 passed

- [ ] **Step 4: Commit**

```bash
git add Module/tool/protocol.py Module/task_system/task_system.py
git commit -m "feat: ToolResult 加 async_task_id + TaskSystem 加 get_task_mailbox"
```

---

### Task 3: CognitiveLoop — 新增 setter + submit_task function definition + 拦截框架

**Files:**
- Modify: `core/cognitive_loop.py`

**Interfaces:**
- Produces:
  - `CognitiveLoop.set_task_system(ts: TaskSystem | None) -> None`
  - `CognitiveLoop.set_delegation_preparer(dp: DelegationPreparer | None) -> None`
  - `CognitiveLoop.set_on_background_task(callback: Callable | None) -> None`
  - `CognitiveLoop.set_user_profile(profile) -> None`
  - `CognitiveLoop._handle_submit_task(tc: dict) -> ToolResult`
  - `CognitiveLoop._submit_task_definition() -> dict` (static/classmethod, function schema)
- Consumes: `ToolResult.async_task_id`, `TaskSystem`, `DelegationPreparer`

- [ ] **Step 1: __init__ 加新字段**

```python
# cognitive_loop.py, CognitiveLoop.__init__ 末尾加:
self._task_system = None        # TaskSystem | None
self._delegation_preparer = None  # DelegationPreparer | None
self._on_background_task: Callable | None = None  # Agent 注入的回调
self._user_profile = None       # UserProfile | None
```

- [ ] **Step 2: 加 4 个 setter**

```python
# cognitive_loop.py, CognitiveLoop 类

def set_task_system(self, ts) -> None:
    """设置 TaskSystem 引用（agent.py 在 start() 中注入）。"""
    self._task_system = ts

def set_delegation_preparer(self, dp) -> None:
    """设置 DelegationPreparer 引用（agent.py 在 start() 中注入）。"""
    self._delegation_preparer = dp

def set_on_background_task(self, callback: Callable | None) -> None:
    """设置后台任务注册回调（agent.py 注入）。"""
    self._on_background_task = callback

def set_user_profile(self, profile) -> None:
    """设置用户画像引用。"""
    self._user_profile = profile
```

- [ ] **Step 3: 加 submit_task function definition**

```python
# cognitive_loop.py, CognitiveLoop 类

@staticmethod
def _submit_task_definition() -> dict:
    """submit_task 工具的 function definition。

    不注册为 ToolPlugin——只在 tool_defs 中合并，LLM 可调用，
    但 ToolManager 永远不执行它（CognitiveLoop 拦截处理）。
    """
    return {
        "type": "function",
        "function": {
            "name": "submit_task",
            "description": (
                "将复杂多步骤任务提交到后台异步执行。"
                "适用：涉及多步操作、状态变更、或长时间运行的任务。"
                "简单单步操作（如单次搜索、单次文件读取）请直接用对应工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": "完整任务意图描述，包含目标和约束",
                    },
                    "hints": {
                        "type": "string",
                        "description": (
                            "对任务执行有帮助的额外信息，"
                            "如用户情绪、紧急程度、偏好等"
                        ),
                    },
                },
                "required": ["intent"],
            },
        },
    }
```

- [ ] **Step 4: _think_loop 中合并 submit_task definition 到 tool_defs**

在 `tool_defs = self._tool_executor.definitions() ...` 之后：

```python
# 合并 submit_task function definition（让 LLM 可以调用）
if tool_defs is None:
    tool_defs = []
# 附加 submit_task（如果 TaskSystem 和 DelegationPreparer 都已注入）
if self._task_system is not None and self._delegation_preparer is not None:
    tool_defs = list(tool_defs) + [self._submit_task_definition()]
```

注意：`tool_defs` 可能为 None（没有 tool_executor），也可能需要 copy 避免修改原始列表。

- [ ] **Step 5: 加 _handle_submit_task 方法**

```python
# cognitive_loop.py, CognitiveLoop 类

async def _handle_submit_task(self, tc: dict) -> ToolResult:
    """处理 submit_task function call。

    校验 intent → DelegationPreparer.prepare() → TaskSystem.submit()。
    返回 ToolResult——LLM 在下一轮看到成功/失败。
    """
    import json
    from Module.task_system.schema import SubmitError

    args = json.loads(tc["function"]["arguments"])
    intent = (args.get("intent") or "").strip()

    # 1. 校验
    if not intent:
        return ToolResult.fail(name="submit_task",
            error="intent 不能为空，请修正参数后重试。")
    if len(intent) > 10000:
        return ToolResult.fail(name="submit_task",
            error="intent 过长（超过 10000 字符），请缩短描述后重试。")

    hints = (args.get("hints") or "").strip() or None

    # 2. DelegationPreparer
    if self._delegation_preparer is None:
        return ToolResult.fail(name="submit_task",
            error="DelegationPreparer 未初始化，请告知用户后台任务系统暂不可用。")

    prep_result = await self._delegation_preparer.prepare(
        intent=intent,
        hints=hints,
        session_messages=self._session.messages,
        user_profile=self._user_profile,
        tools=self._tool_executor.definitions() if self._tool_executor else [],
        global_constraints=(self._task_system._infra.constraints
                            if self._task_system else None),
        session_id=getattr(self._session, "_session_id", "default"),
    )

    if prep_result.status.value == "no_matching_tools":
        missing = getattr(prep_result, 'missing_tools', [])
        missing_str = ", ".join(missing) if missing else "未知"
        return ToolResult.fail(name="submit_task",
            error=f"缺少能力: {missing_str}。请直接告知用户当前不支持此功能，不要重复尝试。")
    if prep_result.status.value == "too_vague":
        suggestion = getattr(prep_result, 'suggestion', '') or '能再具体一点吗？'
        return ToolResult.fail(name="submit_task",
            error=f"意图不够明确: {suggestion}。请向用户澄清，不要重复尝试。")

    # 3. TaskSystem.submit
    if self._task_system is None:
        return ToolResult.fail(name="submit_task",
            error="TaskSystem 未初始化，请告知用户后台任务系统暂不可用。")

    mailbox_or_error = await self._task_system.submit(prep_result.context)
    if isinstance(mailbox_or_error, SubmitError):
        return ToolResult.fail(name="submit_task",
            error=f"任务提交失败: {mailbox_or_error.message}。请告知用户具体原因。")

    # 4. 成功
    return ToolResult.ok(
        name="submit_task",
        content="好的，已经在后台执行了。",
        duration_ms=0.0,
        async_task_id=str(mailbox_or_error.task_id),
    )
```

- [ ] **Step 6: _think_loop 加拦截逻辑**

在 `normalized = normalize_tool_calls(raw_tool_calls)` 之后，在发送 `tool_executing` actions 之前：

```python
# 分离 submit_task 和其他工具——submit_task 走专用路径
if self._delegation_preparer is not None:
    submit_tcs = [tc for tc in normalized if tc["function"]["name"] == "submit_task"]
    other_tcs  = [tc for tc in normalized if tc["function"]["name"] != "submit_task"]
else:
    submit_tcs = []
    other_tcs  = normalized

# submit_task 工具调用——拦截处理
if submit_tcs:
    from datetime import datetime, timezone
    for tc in submit_tcs:
        result = await self._handle_submit_task(tc)
        # 注入 tool result 到 messages
        messages.append({
            "role": "tool",
            "tool_call_id": tc.get("id", "unknown"),
            "content": result.llm_content,
        })
        await self._output.action("tool_done", params={
            "tool": "submit_task",
            "call_id": tc.get("id", "unknown"),
            "success": result.success,
            "content": result.content or "",
            "error": result.error,
            "error_type": None,
            "duration_ms": result.duration_ms,
            "truncated": result.truncated,
        })
        self._session.add_message({
            "id": new_message_id(),
            "role": "tool",
            "tool_call_id": tc.get("id", "unknown"),
            "name": "submit_task",
            "content": result.content or "",
            "success": result.success,
            "error": result.error,
            "error_type": None,
            "duration_ms": result.duration_ms,
            "truncated": result.truncated,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        # 注册后台任务（result.success 时有 async_task_id）
        if result.success and result.async_task_id and self._on_background_task:
            mailbox = self._task_system.get_task_mailbox(result.async_task_id)
            if mailbox:
                now_iso = datetime.now(timezone.utc).isoformat()
                self._on_background_task(
                    result.async_task_id, mailbox, intent=(
                        json.loads(tc["function"]["arguments"]).get("intent", "")
                    ), dispatched_at=now_iso,
                )

    # 遇到 submit_task → 结束当前回合（不继续工具循环）
    # 其他工具的结果也先发完再结束
    normalized = other_tcs
```

注意：需要捕获 `intent` 变量——在 tc 循环中从 `json.loads(tc["function"]["arguments"]).get("intent", "")` 提取。

- [ ] **Step 7: 处理 submit_task 后的 FSM 转换**

在 `submit_tcs` 存在且处理完毕后，`normalized = other_tcs`。如果 `other_tcs` 为空（LLM 只调了 submit_task），正常执行完后续逻辑（无 tool_calls → 最终回答 → _transition_idle）；如果还有其它工具，继续执行它们。在执行完所有工具后，LLM 下一轮自然会生成"好的，已经在后台执行了"之类的确认回复。

FSM 转换在 `_transition_idle` 中添加：

```python
# _transition_idle() 中，在 FSM 转换前加:
# 如果有后台任务活跃 → 进入 IDLE_BACKGROUND_TASK 而非 IDLE
if self._on_background_task is not None and self._task_system is not None:
    active = self._task_system.list_active_tasks()
    if active:
        if self._fsm.state == AgentState.SPEAKING:
            self._fsm.transition(AgentState.IDLE_BACKGROUND_TASK)
        else:
            # 防御性: 确保经过 SPEAKING
            if self._fsm.can_transition(AgentState.SPEAKING):
                self._fsm.transition(AgentState.SPEAKING)
            self._fsm.transition(AgentState.IDLE_BACKGROUND_TASK)
        self._bus.post(EventType.STATE_CHANGE, {"state": "idle_background_task", "request_id": get_request_id()},
                       priority=EventPriority.STATE_CHANGE)
        return

# 原有 IDLE 转换
logger.info("→ IDLE")
...

# 关键：SPEAKING → IDLE_BACKGROUND_TASK 是合法转换（已在 schema.py 注册）
```

- [ ] **Step 8: import 更新**

```python
# cognitive_loop.py 加:
from core.text_utils import content_text_from_content, MessageObj
# 类型导入延迟（避免循环导入）
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from Module.task_system.delegation_preparer import DelegationPreparer
```

- [ ] **Step 9: 运行现有 CognitiveLoop 测试确认无回归**

```bash
python -m pytest core/tests/test_phase3.py -v --asyncio-mode=auto
```

Expected: 107 passed（新增字段和 setter 不影响现有行为；_handle_submit_task 只在 submit_task tool call 时触发，现有测试不产生 submit_task 调用）

- [ ] **Step 10: Commit**

```bash
git add core/cognitive_loop.py
git commit -m "feat: CognitiveLoop submit_task 拦截框架 + setter + function definition"
```

---

### Task 4: CognitiveLoop — 渐进式唤醒

**Files:**
- Modify: `core/cognitive_loop.py`

**Interfaces:**
- Produces: `CognitiveLoop.process(is_task_result=True, task_result_context=...)`
- Consumes: EventBus TASK_RESULT event (from Task 5)

- [ ] **Step 1: process() 签名加参数**

```python
async def process(self, user_message: str = "", user_name: str = "妖梦",
                   stt_emotion: str = "", stt_events: list[str] | None = None,
                   _on_tool_calls=None,
                   content: list[dict] | None = None,
                   is_task_result: bool = False,           # 新增
                   task_result_context: dict | None = None, # 新增
                   ) -> None:
```

- [ ] **Step 2: _build_wake_up_message 方法**

```python
# cognitive_loop.py, CognitiveLoop 类

@staticmethod
def _build_wake_up_message(ctx: dict) -> str:
    """构建渐进式唤醒消息。

    v1: 统一礼貌询问。后续 TODO(v2): 检查对话上下文动态调整。
    """
    return (
        f"[系统通知] 你此前委派的后台任务"
        f"（{ctx.get('original_intent', '')}）"
        f"已执行完毕。结果摘要: {ctx.get('result', '')}。"
        f"请自然地告知用户任务已完成，询问是否现在汇报详细结果，还是稍后再说。"
    )
```

- [ ] **Step 3: _think_loop 加唤醒分支**

在 `build_messages` 调用前（emotion analysis 之后）：

```python
# _think_loop() 开头附近，emotion 分析之后，prompt 构建之前：

# 渐进式唤醒——后台任务完成后的通知
if is_task_result and task_result_context:
    user_message = self._build_wake_up_message(task_result_context)
```

注意：`is_task_result` 消息不走普通 `user_message` 流程（不写入 session user 消息）。需要在 session.add_message 之前判断：

```python
# 在 session.add_message 处，跳过系统唤醒消息的用户录入:
if not is_task_result:
    self._session.add_message({
        "id": new_message_id(),
        "role": "user",
        "content": user_content,
        ...
    })
```

- [ ] **Step 4: 唤醒模板不需要存放在 session 中**

唤醒消息是系统注入的临时上下文，不应作为用户消息持久化。在 `_think_loop` 中，`is_task_result=True` 时跳过 `self._session.add_message(...)`。

- [ ] **Step 5: 运行测试**

```bash
python -m pytest core/tests/test_phase3.py -v --asyncio-mode=auto
```

Expected: 107 passed（新参数有默认值，不影响现有调用）

- [ ] **Step 6: Commit**

```bash
git add core/cognitive_loop.py
git commit -m "feat: CognitiveLoop 渐进式唤醒——is_task_result 参数 + 唤醒模板"
```

---

### Task 5: Agent 层 — TaskSystem 生命周期 + mailbox 轮询 + TASK_RESULT 处理

**Files:**
- Modify: `core/agent.py`

**Interfaces:**
- Produces: `Agent._poll_mailboxes()`, `Agent._register_background_task()`
- Consumes: `TaskSystem`, `TaskMailbox`, `CognitiveLoop.set_task_system()`, `CognitiveLoop.set_delegation_preparer()`, `CognitiveLoop.set_on_background_task()`

- [ ] **Step 1: __init__ 加新字段**

```python
# agent.py, Agent.__init__ 末尾加:
self._task_system = None   # TaskSystem | None
self._delegation_preparer = None  # DelegationPreparer | None
self._background_tasks: dict[str, dict] = {}  # task_id -> {intent, dispatched_at, mailbox}
self._mailbox_task: asyncio.Task | None = None
```

- [ ] **Step 2: start() 中创建 TaskSystem + DelegationPreparer + 注册**

在 `start()` 方法末尾，`print(...)` 之前：

```python
# ---- TaskSystem ----
from Module.task_system.task_system import TaskSystem
from Module.task_system.delegation_preparer import DelegationPreparer
from Module.task_system.infra import AgentInfra
from Module.task_system.schema import ConstraintConfig

# TaskSystem 基础设施
_ts_infra = AgentInfra(
    llm=self._llm._backend._client if hasattr(self._llm, '_backend') and hasattr(self._llm._backend, '_client') else None,
    milvus=None,
    http=None,
    tool_registry=self._tool_manager,
    constraints=ConstraintConfig(),
)

# HITLNotifier 简单实现——委托给 EventBus
class _EventBusHITLNotifier:
    def __init__(self, bus):
        self._bus = bus

    async def request_approval(self, event_data):
        pass  # v1: 审批由 ToolManager 直接处理，不经过此通道

    async def cancel_approval(self, approval_ids):
        pass

_hitl = _EventBusHITLNotifier(self._bus)

self._task_system = TaskSystem(infra=_ts_infra, hitl_notifier=_hitl)
await self._task_system.start()

self._delegation_preparer = DelegationPreparer()

# 注入到 CognitiveLoop
self._cognitive.set_task_system(self._task_system)
self._cognitive.set_delegation_preparer(self._delegation_preparer)
self._cognitive.set_user_profile(None)  # v1: UserProfile 未实现
self._cognitive.set_on_background_task(self._register_background_task)

# 启动 mailbox 轮询
self._mailbox_task = asyncio.create_task(self._poll_mailboxes())
```

- [ ] **Step 3: _register_background_task 方法**

```python
# agent.py, Agent 类

def _register_background_task(self, task_id: str, mailbox, intent: str, dispatched_at: str) -> None:
    """CognitiveLoop 提交后台任务后调用——注册到轮询列表。"""
    self._background_tasks[task_id] = {
        "intent": intent,
        "dispatched_at": dispatched_at,
        "mailbox": mailbox,
    }
```

- [ ] **Step 4: _poll_mailboxes 方法**

```python
# agent.py, Agent 类

async def _poll_mailboxes(self):
    """轮询所有活跃后台任务的 mailbox。每 500ms 一次。"""
    while self._state == RunState.RUNNING:
        await asyncio.sleep(0.5)
        for task_id, info in list(self._background_tasks.items()):
            mailbox = info.get("mailbox")
            if mailbox is None:
                continue
            try:
                # 内层 while: 清空当前 mailbox 的所有待消费事件
                while True:
                    event = mailbox.get_nowait()
                    if event is None:
                        break  # 退出内层 while（当前 mailbox 已清空），继续 for 下一个 task
                    data = event.data or {}
                    if event.type.value == "completed":
                        self._bus.post(EventType.TASK_RESULT, {
                            "task_id": task_id,
                            "result": data.get("result", ""),
                            "original_intent": info["intent"],
                            "dispatched_at": info["dispatched_at"],
                        })
                        self._background_tasks.pop(task_id, None)
                        break  # 退出内层 while（task 已终态），继续 for 下一个 task
                    elif event.type.value == "failed":
                        self._bus.post(EventType.TASK_RESULT, {
                            "task_id": task_id,
                            "result": f"任务执行失败: {data.get('error', '未知错误')}",
                            "original_intent": info["intent"],
                            "dispatched_at": info["dispatched_at"],
                        })
                        self._background_tasks.pop(task_id, None)
                        break  # 退出内层 while（task 已终态），继续 for 下一个 task
                    elif event.type.value == "cancelled":
                        self._background_tasks.pop(task_id, None)
                        break  # 退出内层 while（task 已取消），继续 for 下一个 task
                    # PROGRESS / PAUSED: v1 忽略
                    # 注意：以上所有 break 退出的是内层 while True，
                    #      而非外层 for 循环
            except Exception:
                logger.debug("mailbox poll error", exc_info=True)
```

- [ ] **Step 5: _process_events 加 TASK_RESULT 分支**

在 `elif event.type == EventType.STATE_CHANGE:` 之前：

```python
            elif event.type == EventType.TASK_RESULT:
                data = event.data or {}
                rid = self.next_request_id()
                set_request_id(rid)
                # TODO: 多任务并发时用队列或锁保护 _current_task
                #      详见 spec 第 11 节"多任务并发唤醒锁 + 租约超时"
                task = asyncio.create_task(
                    self._cognitive.process(
                        user_message="",
                        is_task_result=True,
                        task_result_context=data,
                    ),
                    name=f"task-result-{data.get('task_id', 'unknown')}",
                )
                self._cognitive._current_task = task
```

- [ ] **Step 6: _process_events FSM 检查更新**

在 USER_INPUT 和 SYSTEM_TICK 分支中，检查改 IDLE_BACKGROUND_TASK 也可处理消息。当前代码：

```python
if self._fsm.state != AgentState.IDLE:
    # Queue message instead of interrupting
```

改为：

```python
if self._fsm.state not in (AgentState.IDLE, AgentState.IDLE_BACKGROUND_TASK):
    # Queue message instead of interrupting
```

- [ ] **Step 7: stop() 加 TaskSystem 清理**

在现有清理逻辑中，`self._scheduler.stop()` 之前：

```python
# 停止 mailbox 轮询
if self._mailbox_task:
    self._mailbox_task.cancel()
    self._mailbox_task = None

# 停止 TaskSystem
if self._task_system:
    try:
        await asyncio.wait_for(self._task_system.stop(), timeout=5.0)
    except asyncio.TimeoutError:
        logger.warning("TaskSystem stop timed out")
```

- [ ] **Step 8: 确保 State_CHANGE 分支处理新状态**

```python
elif event.type == EventType.STATE_CHANGE:
    state = event.data.get("state", "") if event.data else ""
    logger.info("FSM → %s", state)
    if state in ("idle", "idle_background_task"):  # 新状态也触发排队检查
        ...
```

- [ ] **Step 9: 运行现有 agent 测试**

```bash
python -m pytest core/tests/test_task_scheduler.py -v --asyncio-mode=auto -k "cognitive"
```

Expected: 2 passed（现有 CognitiveLoop 集成测试不影响）

- [ ] **Step 10: Commit**

```bash
git add core/agent.py
git commit -m "feat: Agent TaskSystem 生命周期 + mailbox 轮询 + TASK_RESULT 处理"
```

---

### Task 6: 单元测试

**Files:**
- Modify: `core/tests/test_phase3.py`

**Interfaces:**
- Consumes: `CognitiveLoop._handle_submit_task()`, `CognitiveLoop._submit_task_definition()`, 新 FSM 状态

- [ ] **Step 1: 加 submit_task definition 测试**

```python
# test_phase3.py

class TestSubmitTaskDefinition:
    def test_schema_has_required_intent(self):
        """submit_task function schema 的 required 包含 intent。"""
        from core.cognitive_loop import CognitiveLoop
        from core.text_utils import MessageObj
        defn = CognitiveLoop._submit_task_definition()
        func = defn["function"]
        assert func["name"] == "submit_task"
        assert "intent" in func["parameters"]["required"]
        assert "intent" in func["parameters"]["properties"]

    def test_schema_is_valid_openai_format(self):
        """返回的是标准 OpenAI function-calling 格式。"""
        from core.cognitive_loop import CognitiveLoop
        defn = CognitiveLoop._submit_task_definition()
        assert defn["type"] == "function"
        assert "function" in defn
        assert "name" in defn["function"]
        assert "parameters" in defn["function"]
```

- [ ] **Step 2: 加 _handle_submit_task 测试**

```python
class TestHandleSubmitTask:
    @pytest.fixture
    def loop_with_ts(self):
        """创建带 mock TaskSystem + DelegationPreparer 的 CognitiveLoop。"""
        from core.cognitive_loop import CognitiveLoop
        from core.agent_fsm import AgentFSM
        from core.event_bus import EventBus
        from Module.output import OutputManager
        from core.memory.session import SessionMemory

        fsm = AgentFSM()
        bus = EventBus()
        output = OutputManager()
        session = SessionMemory()
        prompt = MagicMock()
        prompt._character = MagicMock(name="Test")
        llm = MagicMock()

        loop = CognitiveLoop(
            llm=llm, fsm=fsm, event_bus=bus, output=output,
            prompt=prompt, session=session,
        )

        # Mock TaskSystem
        ts = MagicMock()
        ts_mailbox = MagicMock()
        ts_mailbox.task_id = "task-001"
        ts.submit = AsyncMock(return_value=ts_mailbox)
        ts.get_task_mailbox = MagicMock(return_value=ts_mailbox)
        ts._infra = MagicMock()
        ts._infra.constraints = MagicMock()
        ts.list_active_tasks = MagicMock(return_value=[])

        # Mock DelegationPreparer
        dp = MagicMock()
        from Module.task_system.schema import DelegationResult, DelegationStatus
        dp.prepare = AsyncMock(return_value=DelegationResult(
            status=DelegationStatus.OK,
            context=MagicMock(),
        ))

        loop.set_task_system(ts)
        loop.set_delegation_preparer(dp)
        loop.set_user_profile(None)
        return loop

    @pytest.mark.asyncio
    async def test_intent_empty_returns_fail(self, loop_with_ts):
        """空 intent → ToolResult.fail。"""
        tc = {"function": {"name": "submit_task", "arguments": '{"intent": ""}'}}
        result = await loop_with_ts._handle_submit_task(tc)
        assert result.success is False
        assert "不能为空" in result.error

    @pytest.mark.asyncio
    async def test_success_returns_async_task_id(self, loop_with_ts):
        """成功后返回 async_task_id 非空。"""
        tc = {"function": {"name": "submit_task", "arguments": '{"intent": "查天气"}'}}
        result = await loop_with_ts._handle_submit_task(tc)
        assert result.success is True
        assert result.async_task_id == "task-001"
        assert "已在后台执行" in result.content

    @pytest.mark.asyncio
    async def test_delegation_no_matching_tools_returns_fail(self, loop_with_ts):
        """DelegationPreparer 返回 no_matching_tools → fail。"""
        from Module.task_system.schema import DelegationResult, DelegationStatus
        dp = loop_with_ts._delegation_preparer
        dp.prepare = AsyncMock(return_value=DelegationResult(
            status=DelegationStatus.NO_MATCHING_TOOLS,
            missing_tools=["email"],
        ))
        tc = {"function": {"name": "submit_task", "arguments": '{"intent": "查天气"}'}}
        result = await loop_with_ts._handle_submit_task(tc)
        assert result.success is False
        assert "email" in result.error

    @pytest.mark.asyncio
    async def test_task_system_none_returns_fail(self, loop_with_ts):
        """TaskSystem 未注入 → fail。"""
        loop_with_ts.set_task_system(None)
        tc = {"function": {"name": "submit_task", "arguments": '{"intent": "查天气"}'}}
        result = await loop_with_ts._handle_submit_task(tc)
        assert result.success is False
        assert "未初始化" in result.error
```

- [ ] **Step 3: 唤醒消息测试**

```python
class TestWakeUpMessage:
    def test_wake_up_message_includes_intent_and_result(self):
        """唤醒模板拼接 intent 和 result。"""
        from core.cognitive_loop import CognitiveLoop
        from core.agent_fsm import AgentFSM
        from Module.output import OutputManager
        from core.event_bus import EventBus
        from core.memory.session import SessionMemory
        loop = CognitiveLoop(
            llm=MagicMock(), fsm=AgentFSM(), event_bus=EventBus(),
            output=OutputManager(), prompt=MagicMock(),
            session=SessionMemory(),
        )
        ctx = {
            "original_intent": "查天气",
            "result": "明天晴天",
            "task_id": "t1",
            "dispatched_at": "2026-06-17T00:00:00+00:00",
        }
        msg = loop._build_wake_up_message(ctx)
        assert "查天气" in msg
        assert "明天晴天" in msg
        assert "汇报" in msg or "稍后" in msg
```

- [ ] **Step 5: 运行所有新测试**

```bash
python -m pytest core/tests/test_phase3.py -v --asyncio-mode=auto -k "TestSubmitTask or TestWakeUp"
```

Expected: 6-7 passed

- [ ] **Step 6: 运行全部测试确认无回归**

```bash
python -m pytest core/tests/test_phase3.py -v --asyncio-mode=auto
```

Expected: ~113 passed（107 现有 + 6-7 新）

- [ ] **Step 7: Commit**

```bash
git add core/tests/test_phase3.py
git commit -m "test: submit_task 拦截 + 唤醒模板单元测试"
```

---

### Task 7: 文档更新

**Files:**
- Modify: `Module/task_system/docs/PROGRESS.md`
- Modify: `docs/architecture.md`

- [ ] **Step 1: PROGRESS.md 更新**

在"接入 CognitiveLoop"节中更新状态：

```markdown
### 接入 CognitiveLoop

| 项目 | 状态 |
|------|------|
| `TaskSystem` 实例创建 / `start()` | ✅ Agent.start() 中创建 |
| `submit_task` 注册为 LLM 工具 | ✅ function definition 在 tool_defs 合并 |
| `DelegationPreparer` 调用 | ✅ CognitiveLoop._handle_submit_task() |
| Mailbox 事件消费 | ✅ Agent._poll_mailboxes() 500ms 轮询 |
| Debug WebUI 路由 | ⏳ `app['task_system']` 注册（后续） |
```

并在"本周修复"节加：

```markdown
### CognitiveLoop 异步任务支持（2026-06-17）

| # | 文件 | 说明 |
|---|------|------|
| 1 | `schema.py` | FSM 加 `IDLE_BACKGROUND_TASK` + EventBus 加 `TASK_RESULT` |
| 2 | `cognitive_loop.py` | `_handle_submit_task()` 拦截 + `_submit_task_definition()` + 4 setter |
| 3 | `agent.py` | TaskSystem 生命周期 + `_poll_mailboxes()` + TASK_RESULT 处理 |
| 4 | `protocol.py` | `ToolResult.async_task_id` 字段 |
| 5 | `task_system.py` | `get_task_mailbox()` 查询方法 |
```

- [ ] **Step 2: architecture.md 更新**

在 core/ 文件列表中加 cognitive_loop 的改动摘要。

- [ ] **Step 3: Commit**

```bash
git add Module/task_system/docs/PROGRESS.md docs/architecture.md
git commit -m "docs: 同步 CognitiveLoop 异步任务支持进度"
```

---

## Verification

```bash
# 全部 core 测试
python -m pytest core/tests/ -v --asyncio-mode=auto

# TaskSystem 单元测试
python -m pytest Module/task_system/tests/unit/ -v --asyncio-mode=auto

# 确认 agent import 正常
python -c "from core.agent import Agent; print('OK')"
```
