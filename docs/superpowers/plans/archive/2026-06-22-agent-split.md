# Agent 类内拆分实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `core/agent.py` 的 3 个长方法拆分为职责单一的私有子方法，730→480 行

**Architecture:** 机械提取——每个子方法的代码从原方法体中原样复制，原方法体替换为子方法调用序列。不改任何逻辑、顺序、异常处理、日志。

**Tech Stack:** Python 3.13, asyncio

**Design spec:** `docs/superpowers/specs/2026-06-22-agent-split-design.md`

## Global Constraints

- 所有新增方法为 `Agent` 的私有方法（`_xxx()`）
- 公开 API 签名不变（`start()`, `stop()`, `process_message()`, `run_server()`, `run_repl()`, `switch_xxx()`）
- 不改变 `start()`/`stop()` 的执行顺序和并发度
- 不改变事件分发时序
- 所有子方法保留原有的 timeout、异常处理、日志语句
- `self._xxx` 赋值顺序完全保持原样
- `from __future__ import annotations` 已经在文件顶部

---
```

### Task 1: `start()` 拆分为 8 个子方法

**Files:**
- Modify: `core/agent.py:144-289` → 替换为子方法调用 + 新增 8 个私有方法

**子方法清单（按 spec 完整代码）：**
- `_start_persistence() -> tuple[bool, bool]` — MongoDB/Milvus 连接 + session 加载
- `_start_token_and_prompt(mongo_ok: bool) -> None` — TokenTracker + PromptRecorder
- `_start_tool_context() -> None` — ToolContext + 工具加载
- `_start_worker_pool() -> None` — Scheduler.start() + embedding 预加载
- `_start_idle_and_events() -> None` — IdleMonitor + 事件循环
- `_start_tts() -> None` — TTS 条件创建
- `_start_task_system() -> None` — TaskSystem + DelegationPreparer
- `_start_mailbox_polling() -> None` — mailbox 轮询 task 创建
- `_print_startup_banner(mongo_ok: bool, milvus_ok: bool) -> None` — 启动横幅

- [ ] **Step 1: 读取当前 `start()` 方法体，确认代码与 spec 一致**

```bash
cat -n core/agent.py | sed -n '144,289p'
```

- [ ] **Step 2: 在 `Agent` 类中新增 8 个子方法 + 1 个 banner 方法**

从 spec 复制每个方法的完整代码。插入位置：在 `start()` 方法之后、`stop()` 方法之前。

- [ ] **Step 3: 替换 `start()` 方法体为子方法调用序列**

```python
async def start(self) -> None:
    """Start the agent: connect persistence, idle monitor, event loop."""
    self._state = RunState.RUNNING

    # Reset FSM — may be stuck from a previous run that was interrupted
    self._fsm.reset()

    # Recreate LLM HTTP client (may have been closed by previous stop())
    await self._llm.reconnect()

    # 1. 持久化连接 + session 加载
    mongo_ok, milvus_ok = await self._start_persistence()

    # 2. Token + Prompt 统计
    await self._start_token_and_prompt(mongo_ok)

    # 3. 工具上下文 + 插件加载
    await self._start_tool_context()

    # 4. Worker Pool + embedding 预加载
    await self._start_worker_pool()

    # 5. IdleMonitor + 主事件循环
    await self._start_idle_and_events()

    # 6. TTS（条件启动）
    await self._start_tts()

    # 7. TaskSystem + DelegationPreparer
    await self._start_task_system()

    # 8. Mailbox 轮询
    self._start_mailbox_polling()

    # 9. 启动横幅
    self._print_startup_banner(mongo_ok, milvus_ok)
```

- [ ] **Step 4: 运行测试验证**

```bash
source /home/yomu/data/anaconda3/etc/profile.d/conda.sh && conda activate Elysia && \
python -c "from core.agent import Agent; print('import OK')" && \
python -m pytest core/tests/test_task_scheduler.py -v
```

预期：import OK + 26 passed

- [ ] **Step 5: Commit**

```bash
git add core/agent.py
git commit -m "refactor: start() 拆分为 8 个私有子方法"
```

---

### Task 2: `stop()` 拆分为 11 个子方法

**Files:**
- Modify: `core/agent.py` — `stop()` 方法体替换 + 新增 11 个私有方法

**子方法清单（按 spec 完整代码）：**
- `_flush_consolidator()` — Consolidator buffer 落盘
- `_stop_event_task()` — 停止事件循环
- `_stop_cognitive_and_tts()` — Cancel cognitive + TTS
- `_stop_embedding_if_running()` (async) — 取消并等待 embedding 任务
- `_stop_mailbox()` — 停止 mailbox 轮询
- `_stop_task_system()` — TaskSystem.stop()
- `_stop_scheduler()` — Scheduler.stop()
- `_stop_tool_manager()` — ToolManager.shutdown()
- `_stop_idle()` — IdleMonitor.stop()
- `_stop_persistence()` — session.save() → long_term.close() → EmbeddingFactory.shutdown()
- `_stop_llm_and_tts()` — LLM + TTS 客户端关闭
- `_stop_output()` — OutputManager.close()

- [ ] **Step 1: 读取当前 `stop()` 方法体，确认代码与 spec 一致**

```bash
cat -n core/agent.py | sed -n '291,396p'
```

- [ ] **Step 2: 在 `Agent` 类中新增 11 个子方法**

从 spec 复制每个方法的完整代码。插入位置：在 `stop()` 方法之后、`next_request_id()` 之前。
注意：`_stop_embedding_if_running` 是 async 的（参考 spec 第 277 行），它合并了 cancel 和 await。

- [ ] **Step 3: 替换 `stop()` 方法体为子方法调用序列**

```python
async def stop(self) -> None:
    """Stop the agent, save session, clean up resources."""
    logger.info("Agent stopping...")
    self._state = RunState.STOPPED

    # 清空排队 — shutdown 期间不处理新消息
    self._pending_messages.clear()

    # 1. Flush memory consolidation buffer
    await self._flush_consolidator()

    # 2. 先停事件循环 — 防止它在 shutdown 期间继续 post 新任务
    await self._stop_event_task()

    # 3. Cancel cognitive + TTS tasks（belt-and-suspenders）
    self._stop_cognitive_and_tts()

    # 4. 停止 mailbox 轮询
    self._stop_mailbox()

    # 5. 停止 TaskSystem
    await self._stop_task_system()

    # 6. 停止 Scheduler Worker Pool
    await self._stop_scheduler()

    # 7. 关闭 ToolManager
    await self._stop_tool_manager()

    # 8. 停止 IdleMonitor
    await self._stop_idle()

    # 9. 等待 embedding 任务完成（cancel + await）
    await self._stop_embedding_if_running()

    # 10. 持久化关闭（save → close → shutdown embedding）
    await self._stop_persistence()

    # 11. 关闭 LLM / TTS 客户端
    await self._stop_llm_and_tts()

    # 12. 关闭输出通道
    await self._stop_output()

    logger.info("Agent stopped")
```

- [ ] **Step 4: 运行测试验证**

```bash
source /home/yomu/data/anaconda3/etc/profile.d/conda.sh && conda activate Elysia && \
python -m pytest core/tests/test_task_scheduler.py -v && \
python -m pytest Module/output/test_output.py -v
```

预期：26 passed + 54 passed

- [ ] **Step 5: Commit**

```bash
git add core/agent.py
git commit -m "refactor: stop() 拆分为 11 个私有子方法"
```

---

### Task 3: `_process_events()` 拆分为 5 个事件 handler

**Files:**
- Modify: `core/agent.py` — `_process_events()` 方法体替换 + 新增 5 个 handler

**Handler 清单（按 spec 完整代码）：**
- `_handle_interrupt(data: dict) -> None` — cancel cognitive，清空队列
- `_handle_user_input(data: dict) -> None` — 创建 cognitive task
- `_handle_system_tick(data: dict) -> None` — 主动开启话题
- `_handle_task_result(data: dict) -> None` — 后台任务结果唤醒
- `_handle_state_change(data: dict) -> None` — 排队消息出队 + notify_state

- [ ] **Step 1: 读取当前 `_process_events()` 方法体，确认代码与 spec 一致**

```bash
cat -n core/agent.py | sed -n '634,731p'
```

- [ ] **Step 2: 在 `Agent` 类中新增 5 个事件 handler**

从 spec 复制每个 handler 的完整代码。插入位置：在 `_process_events()` 方法之后。

- [ ] **Step 3: 替换 `_process_events()` 方法体为 switch 分发**

```python
async def _process_events(self) -> None:
    """Main event loop: consume events from EventBus and dispatch.

    Cognitive processing runs in a separate task so the event loop stays
    responsive to INTERRUPT events during LLM streaming.
    """
    while self._state == RunState.RUNNING:
        try:
            event = await self._bus.next()
        except RuntimeError:
            break

        # 从事件数据提取 request_id 并设置 ContextVar（修复跨 task 传播）
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
```

- [ ] **Step 4: 运行测试验证**

```bash
source /home/yomu/data/anaconda3/etc/profile.d/conda.sh && conda activate Elysia && \
python -m pytest core/tests/test_phase3.py -v && \
python -m pytest core/tests/test_task_scheduler.py -v && \
python -m pytest Module/output/test_output.py -v
```

预期：114 + 26 + 54 = 194 passed

- [ ] **Step 5: Commit**

```bash
git add core/agent.py
git commit -m "refactor: _process_events() 拆分为 5 个事件 handler"
```
