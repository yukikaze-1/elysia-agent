# CognitiveLoop 异步任务支持设计文档

> 日期: 2026-06-17  
> 状态: 设计中  
> 目标: 支持 submit_task 工具（fire-and-forget 后台任务）+ TaskSystem 集成

---

## 1. 目标

CognitiveLoop 当前工具执行是同步阻塞的：调工具 → 等结果 → 注入 messages → 下一轮 LLM。submit_task 是 fire-and-forget——提交后立即返回，任务在后台运行数秒到数分钟。需要让 CognitiveLoop 支持这种异步工具模式。

## 2. 流程总览

```
用户: "帮我查最近三天天气，下雨的日子给老板发邮件"
  │
LLM 判断: 多步骤复杂任务 → tool_call: submit_task(intent="...", hints="...")
  │
  ▼ (_think_loop 工具执行阶段)
  │
CognitiveLoop 识别 submit_task → 拦截（不走 ToolManager.execute()）
  ├─ 1. 校验 intent
  ├─ 2. DelegationPreparer.prepare(intent, hints, session, tools, constraints)
  ├─ 3. TaskSystem.submit(context)
  └─ 4. 成功:
       ├─ 构造 ToolResult.ok("好的，已经在后台执行了。", async_task_id=task_id)
       ├─ 注入 messages（LLM 知道任务已提交）
       ├─ FSM: SPEAKING → IDLE_BACKGROUND_TASK
       ├─ 通知 Agent 注册 mailbox 消费者
       └─ return（结束当前回合）

...时间流逝（用户可以继续聊天，FSM 正常处理 USER_INPUT）...

后台任务完成 → Agent mailbox 消费者收到 COMPLETED
  → bus.post(TASK_RESULT, {task_id, result, original_intent, dispatched_at})
  → CognitiveLoop.process(is_task_result=True)
  → 渐进式唤醒: 检查对话上下文 → 礼貌询问是否现在汇报
```

## 3. FSM 变更

### 3.1 新增状态

```
IDLE_BACKGROUND_TASK  — Agent 可对话，但后台有任务在执行
```

### 3.2 合法转换

```
IDLE_BACKGROUND_TASK → THINKING      (收到 TASK_RESULT 或 新 USER_INPUT)
IDLE_BACKGROUND_TASK → INTERRUPTED   (用户打断)

SPEAKING → IDLE_BACKGROUND_TASK       (提交后台任务后的对话回复)
IDLE → IDLE_BACKGROUND_TASK           (后台任务完成时用户正在空闲)

# 原有转换不变
IDLE → THINKING
THINKING → EXECUTING / SPEAKING / INTERRUPTED
EXECUTING → THINKING / INTERRUPTED
SPEAKING → IDLE / THINKING / INTERRUPTED
```

### 3.3 FSM 检查

在 `Agent._process_events()` 中：
- `IDLE` / `IDLE_BACKGROUND_TASK` → 可立即处理消息
- 其他状态 → 消息排队 (`_pending_messages`)

## 4. EventBus 变更

### 4.1 新增事件类型

```python
EventType.TASK_RESULT = "task_result"   # Priority 2 (与 USER_INPUT 同级)
```

事件 data:
```python
{
    "task_id": str,
    "result": str,           # 任务结果摘要
    "original_intent": str,  # 原始任务意图
    "dispatched_at": str,    # 任务提交时间
}
```

### 4.2 _process_events 新增分支

```python
elif event.type == EventType.TASK_RESULT:
    data = event.data or {}
    rid = self.next_request_id()
    set_request_id(rid)
    # TODO: 多任务并发时需要用队列或锁保护 _current_task，
    #       防止第二个 TASK_RESULT 覆盖第一个的引用。
    #       详见第 11 节"多任务并发唤醒锁 + 租约超时"。
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

## 5. submit_task 拦截

### 5.1 ToolPlugin 存根

`submit_task` **不注册为普通 ToolPlugin**（不放入 `ALL_PLUGINS`）。其 function definition 在 CognitiveLoop 构建 `tool_defs` 时手动合并附加。这样：
- LLM 能看到 `submit_task` 的 function schema，可以调用
- `ToolManager.execute("submit_task")` 永远不会被调用，避免其他代码路径收到"此工具不应直接执行"的困惑错误
- 如果未来需要统一管理，可注册为 Plugin 并添加 ToolManager 层防御检查

**Function definition**（合并到 LLM tool_defs 中）：

```json
{
    "type": "function",
    "function": {
        "name": "submit_task",
        "description": "将复杂多步骤任务提交到后台异步执行。"
                       "适用：涉及多步操作、状态变更、或长时间运行的任务。"
                       "简单单步操作（如单次搜索、单次文件读取）请直接用对应工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "description": "完整任务意图描述，包含目标和约束"
                },
                "hints": {
                    "type": "string",
                    "description": "对任务执行有帮助的额外信息，"
                                   "如用户情绪、紧急程度、偏好等"
                }
            },
            "required": ["intent"]
        }
    }
}
```

### 5.2 CognitiveLoop 拦截逻辑

在 `_think_loop` 的工具执行阶段：

```python
# 分离 submit_task 和其他工具
# submit_task 的 function definition 由 CognitiveLoop 合并到 tool_defs 中，
# LLM 可调用，但 ToolManager 不负责执行它
submit_tasks = [tc for tc in normalized if tc["function"]["name"] == "submit_task"]
other_tasks = [tc for tc in normalized if tc["function"]["name"] != "submit_task"]

# submit_task 走专用路径
if submit_tasks:
    for tc in submit_tasks:
        result = await self._handle_submit_task(tc)
        # 注入 tool result 到 messages
        messages.append({...})

    # 遇到 submit_task → 结束当前回合
    # 调用方保证此时已在 SPEAKING 状态
    # 如果 speaking_started 未设置（LLM 仅返回 tool_call 无文本）：
    #   EXECUTING → SPEAKING → IDLE_BACKGROUND_TASK
    # 如果 speaking_started 已设置（已有文本输出）：
    #   SPEAKING → IDLE_BACKGROUND_TASK
    FSM → SPEAKING → IDLE_BACKGROUND_TASK
    return
```

### 5.3 _handle_submit_task 实现

```python
async def _handle_submit_task(self, tc: dict) -> ToolResult:
    args = json.loads(tc["function"]["arguments"])
    intent = (args.get("intent") or "").strip()

    # 1. 校验
    if not intent:
        return ToolResult.fail(name="submit_task", error="intent 不能为空")
        # LLM 收到后应修正参数重试（下一轮重新生成正确的 tool_call）
    if len(intent) > 10000:
        return ToolResult.fail(name="submit_task", error="intent 过长")
        # LLM 收到后应缩短描述并重试

    hints = (args.get("hints") or "").strip() or None

    # 2. DelegationPreparer
    prep_result = await self._delegation_preparer.prepare(
        intent=intent,
        hints=hints,
        session_messages=self._session.messages,
        user_profile=self._user_profile,
        tools=self._tool_executor.definitions(),
        global_constraints=self._task_system.constraints if self._task_system else None,
    )

    if prep_result.status == "no_matching_tools":
        # 工具库缺少所需能力，非参数问题，无法通过重试解决
        # LLM 收到后应告知用户当前不支持此功能
        return ToolResult.fail(name="submit_task",
            error=f"缺少能力: {prep_result.missing_tools}。"
                  f"请直接告知用户当前不支持此功能，不要重复尝试。")
    if prep_result.status == "too_vague":
        # 意图不够明确，无法确定所需工具
        # LLM 收到后应向用户澄清，不要重复猜测
        return ToolResult.fail(name="submit_task",
            error=f"意图不够明确: {prep_result.suggestion or '能再具体一点吗？'}。"
                  f"请向用户澄清，不要重复尝试。")

    # 3. TaskSystem.submit
    if self._task_system is None:
        return ToolResult.fail(name="submit_task",
            error="TaskSystem 未初始化，请告知用户后台任务系统暂不可用。")

    mailbox_or_error = await self._task_system.submit(prep_result.context)
    if isinstance(mailbox_or_error, SubmitError):
        # SubmitError 是输入级错误（PLAN_TOO_LARGE / CONTEXT_WINDOW_EXCEEDED 等）
        # LLM 收到后应告知用户具体原因，不要自动重试
        return ToolResult.fail(name="submit_task",
            error=f"任务提交失败: {mailbox_or_error.message}。"
                  f"请告知用户具体原因。")

    # 4. 成功
    return ToolResult.ok(
        name="submit_task",
        content="好的，已经在后台执行了。",
        async_task_id=str(mailbox_or_error.task_id),
    )
```

### 5.4 拦截后流程

`_think_loop` 检测到 `submit_task` 被拦截成功后：

1. 将 submit_task 的 ToolResult 注入 messages（LLM 知道任务已提交）
2. 如果 LLM 只调用了 submit_task（无其他 tool），直接生成确认回复 → `_transition_idle_background_task()`
3. 如果 LLM 同时调用了其他 tool，等它们执行完，然后同上

```python
# _transition_idle_background_task():
# 调用时机: _think_loop 处理完 submit_task 之后，工具回复已注入 messages，
#           LLM 下一轮会生成"好的，已经在后台执行了"之类的文本回复。
#           speaking_started 可能为 True（已有文本输出→已在 SPEAKING）
#           或 False（仅 tool_calls 无文本→仍在 EXECUTING/THINKING）。
# 因此先过渡到 SPEAKING 再转换到 IDLE_BACKGROUND_TASK——
# SPEAKING → IDLE_BACKGROUND_TASK 是合法转换。
async def _transition_idle_background_task(self, task_id: str, mailbox, intent: str, dispatched_at: str) -> None:
    await self._tts_mgr.wait_all()
    # 通知 Agent 注册 mailbox 消费者
    if self._on_background_task:
        self._on_background_task(task_id, mailbox, intent, dispatched_at)
    # 防御性: 确保经过 SPEAKING（如果尚未）
    if self._fsm.state != AgentState.SPEAKING:
        if self._fsm.can_transition(AgentState.SPEAKING):
            self._fsm.transition(AgentState.SPEAKING)
    self._fsm.transition(AgentState.IDLE_BACKGROUND_TASK)
    self._bus.post(...)
```

### 5.5 CognitiveLoop 新增依赖

```python
# __init__:
self._task_system: TaskSystem | None = None
self._delegation_preparer: DelegationPreparer | None = None
self._on_background_task: Callable | None = None  # Agent 注入的回调
self._user_profile: UserProfile | None = None
```

```python
# 公开 setter:
def set_task_system(self, ts) -> None: ...
def set_delegation_preparer(self, dp) -> None: ...
def set_on_background_task(self, callback) -> None: ...
def set_user_profile(self, profile) -> None: ...
```

## 6. Agent 层 Mailbox 消费

Agent 持有 `_task_system`，在 `start()` 中启动 `_poll_mailboxes()` 协程。

### 6.1 生命周期

```python
class Agent:
    async def start(self):
        # ... 现有初始化 ...
        self._task_system = TaskSystem(infra, hitl_notifier)
        await self._task_system.start()
        self._cognitive.set_task_system(self._task_system)
        self._cognitive.set_delegation_preparer(
            DelegationPreparer(infra=self._task_system.infra)
        )
        self._cognitive.set_user_profile(self._user_profile)
        self._cognitive.set_on_background_task(self._register_background_task)
        self._mailbox_task = asyncio.create_task(self._poll_mailboxes())

    async def stop(self):
        # ... 现有清理 ...
        if self._mailbox_task:
            self._mailbox_task.cancel()
        if self._task_system:
            await self._task_system.stop()
```

### 6.2 后台任务注册

```python
_BackgroundTaskInfo = {
    "task_id": str,
    "intent": str,
    "dispatched_at": str,
    "mailbox": TaskMailbox,
}

self._background_tasks: dict[str, dict] = {}

def _register_background_task(self, task_id: str, mailbox, intent: str, dispatched_at: str):
    self._background_tasks[task_id] = {
        "intent": intent,
        "dispatched_at": dispatched_at,
        "mailbox": mailbox,
    }
```

### 6.3 轮询循环

```python
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
                        break  # 退出内层 while（当前 mailbox 已清空），继续 for 循环下一个 task
                    data = event.data or {}
                    if event.type == "completed":
                        self._bus.post(EventType.TASK_RESULT, {
                            "task_id": task_id,
                            "result": data.get("result", ""),
                            "original_intent": info["intent"],
                            "dispatched_at": info["dispatched_at"],
                        })
                        self._background_tasks.pop(task_id, None)
                        break  # 退出内层 while（task 已终态），继续 for 循环下一个 task
                    elif event.type == "failed":
                        self._bus.post(EventType.TASK_RESULT, {
                            "task_id": task_id,
                            "result": f"任务执行失败: {data.get('error', '未知错误')}",
                            "original_intent": info["intent"],
                            "dispatched_at": info["dispatched_at"],
                        })
                        self._background_tasks.pop(task_id, None)
                        break  # 退出内层 while（task 已终态），继续 for 循环下一个 task
                    elif event.type == "cancelled":
                        self._background_tasks.pop(task_id, None)
                        break  # 退出内层 while（task 已取消），继续 for 循环下一个 task
                    # PROGRESS / PAUSED: v1 忽略（后续可推送进度到前端）
                    # 注意：以上所有 break 退出的是内层 while True，
                    #      而非外层 for 循环。语义: 当前 mailbox 事件已处理完毕。
            except Exception:
                logger.debug("mailbox poll error", exc_info=True)
```

## 7. 渐进式唤醒

### 7.1 CognitiveLoop.process 新参数

```python
async def process(self, user_message: str = "", ..., 
                   is_task_result: bool = False,
                   task_result_context: dict | None = None) -> None:
```

### 7.2 _think_loop 唤醒分支

在 `build_messages` 之前，检测 `is_task_result`：

```python
if is_task_result and task_result_context:
    user_message = self._build_wake_up_message(task_result_context)
```

### 7.3 唤醒消息构建

```python
def _build_wake_up_message(self, ctx: dict) -> str:
    """
    渐进式唤醒——先检查对话上下文，决定是直接汇报还是礼貌询问。
    v1: 统一礼貌询问，不实现对话上下文检测和结果暂存+重试。
    后续 TODO(v2 中期): 检查最近 1-2 轮对话是否与任务无关，调整询问语气。
    后续 TODO(v2 中期): 压缩系统通知长度（把指导性指令提到 system prompt，唤醒时只传数据）。
    """
    return (
        f"[系统通知] 你此前委派的后台任务（{ctx['original_intent']}）"
        f"已执行完毕。结果摘要: {ctx['result']}。"
        f"请自然地告知用户结果，询问是否现在汇报详细结果，还是稍后再说。"
    )
```

## 8. 改动清单

| 文件 | 改动 | 复杂度 |
|------|------|--------|
| `core/schema.py` | `AgentState` 加 `IDLE_BACKGROUND_TASK`；`EventType` 加 `TASK_RESULT`；`VALID_TRANSITIONS` 更新 | 低 |
| `core/agent_fsm.py` | `can_transition()` 支持新状态（基于 schema 变更自动） | 低 |
| `core/event_bus.py` | 无改动（基于 schema EventType） | — |
| `core/cognitive_loop.py` | `process()` 加 `is_task_result` 参数；`_handle_submit_task()` 新增；拦截逻辑；`_transition_idle_background_task()` 新增；submit_task function definition 合并到 tool_defs；新增 setter/依赖 | **高** |
| `core/agent.py` | TaskSystem 生命周期；`_poll_mailboxes()`；`_register_background_task()`；`_process_events` 加 TASK_RESULT 分支 | 中 |
| `Module/task_system/task_system.py` | 新增 `get_task_mailbox(task_id)` 查询 | 低 |

## 9. 验证

### 9.1 单元测试（mock-based，无外部依赖）

- `test_submit_task_intercepted_not_sent_to_tool_manager` — 验证 submit_task function call 被分离，不走 `_on_tool_calls`
- `test_submit_task_intent_empty_returns_fail` — 空 intent → ToolResult.fail
- `test_submit_task_tool_result_has_async_task_id` — 成功后 ToolResult.async_task_id 非空
- `test_wake_up_message_includes_intent_and_result` — 唤醒消息模板正确拼接
- `test_fsm_transitions_to_idle_background_task_after_submit` — submit_task 成功后状态正确
- `test_mailbox_completed_posts_task_result_event` — 轮询 loop 收到 completed → TASK_RESULT 事件投递
- `test_mailbox_cancelled_does_not_trigger_wake_up` — cancelled 事件不投递 TASK_RESULT

### 9.2 集成测试

```bash
# mailbox 轮询 + 事件投递（需要 TaskSystem 启动）
python -m pytest Module/task_system/tests/integration/ -v -k "async_task"

# 手动 e2e
# 启动 app → 发送复杂任务 → 确认"好的，已在后台执行" → 任务完成后确认渐进式唤醒
```

## 10. 不变原则

- 不改变现有同步工具执行路径（_on_tool_calls / _execute_tools_directly）
- 不改变 TTS / BlockFilter / SentenceBuffer 逻辑
- FSM 状态转换仅新增，不修改已有转换
- submit_task 仅在 _think_loop 后半段生效（不影响 TTFT、流式处理、文本输出）

## 11. v1 范围外（后续迭代）

- [ ] 对话上下文检测（用户是否在聊无关话题）——v1 统一礼貌询问（v2 中期）
- [ ] 任务结果暂存 + 用户"稍后再说"后恢复（v2 中期）
- [ ] 多任务并发唤醒锁 + 租约超时（v2 中期）——v1 单任务场景不触发竞态。v1 允许 LLM 单轮调用多个 submit_task，但唤醒时不区分序号。如果 v1 期间出现多任务并发场景，按 mailbox 事件到达自然顺序串行唤醒
- [ ] 紧急事件绕过锁（FAILURE_MELTDOWN 等）
- [ ] PROGRESS 事件推送到前端（任务进度卡片）
- [ ] 唤醒消息精简——把指导性指令提到系统 prompt（一次性注入），唤醒时只传数据

---

## 附录 A: DelegationPreparer 接口说明

```python
class DelegationPreparer:
    """准备委派上下文——工具筛选 + 上下文摘要 + 约束估计。"""

    async def prepare(
        self,
        intent: str,
        hints: str | None,
        session_messages: list[dict],   # 全量对话历史
        user_profile: UserProfile | None,
        tools: list[dict],              # ToolManager.definitions() 格式
        global_constraints: ConstraintConfig | None,
    ) -> DelegationResult:
        """
        Returns:
            DelegationResult:
                status: "ok" | "no_matching_tools" | "too_vague"
                context: DelegationContext | None   # status=ok 时非空
                missing_tools: list[str]            # status=no_matching_tools 时非空
                suggestion: str | None              # status=too_vague 时的用户澄清建议
        """
        ...

class DelegationContext(BaseModel):
    intent: str
    hints: str | None
    tools: list[Tool]
    task_constraints: TaskConstraints
    summarized_context: str          # ≤500 字符的上下文摘要
    session_id: SessionId
    return_mode: ReturnMode = ReturnMode.NATURAL_LANGUAGE
```

> 完整接口定义见 `Module/task_system/schema.py`。
