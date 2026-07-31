# Agent 类内拆分设计

> 日期: 2026-06-22
> 状态: 设计

## 目标

`core/agent.py` 的 `start()`、`stop()`、`_process_events()` 三个方法过长（100-145行），
按职责提取为私有子方法。所有新增方法为 `Agent` 的私有方法（`_xxx()`），
公开 API 签名不变。文件仍是 `agent.py` 一个，预计 730→480 行。

## 不变原则

- 不改动任何公开 API 签名
- 不改变 `start()`/`stop()` 的执行顺序
- 不改变事件分发时序
- 所有子方法保留原有的 timeout、异常处理、日志
- `self._xxx` 赋值顺序完全保持

---

## 1. `start()` 拆分：145→50 行

提取 8 个私有方法：

### `_start_persistence() -> tuple[bool, bool]`
```python
async def _start_persistence(self) -> tuple[bool, bool]:
    """连接 MongoDB/Milvus，加载 session，恢复 JSONL 快照。"""
    mongo_ok, milvus_ok = await asyncio.gather(
        self._session.connect_db(),
        self._long_term.connect(),
    )
    if mongo_ok:
        await self._session.load()
        # MongoDB 离线期间 session 可能有更新的 JSONL 快照，恢复到 MongoDB
        if os.path.exists(self._session._pending_file):
            if await self._session._load_from_jsonl(merge=True):
                if await self._session.save():
                    os.remove(self._session._pending_file)
                    logger.info("Session JSONL 已导入 MongoDB 并删除")
                else:
                    logger.warning("Session MongoDB 写入失败，保留 JSONL")
    return mongo_ok, milvus_ok
```

### `_start_token_and_prompt(mongo_ok: bool) -> None`
```python
async def _start_token_and_prompt(self, mongo_ok: bool) -> None:
    """创建 TokenTracker + PromptRecorder，注入 CognitiveLoop。"""
    from core.token_tracker import TokenTracker
    self._token_tracker = TokenTracker(
        session_id=getattr(self._session, "_session_id", "default"),
        output_manager=self._output,
        document_db=self._session._db if mongo_ok else None,
    )
    self._cognitive.set_token_tracker(self._token_tracker)
    if mongo_ok:
        await self._token_tracker.flush_pending()

    from core.prompt_recorder import PromptRecorder
    self._prompt_recorder = PromptRecorder(max_records=20)
    self._cognitive.set_prompt_recorder(self._prompt_recorder)
```

### `_start_tool_context() -> None`
```python
async def _start_tool_context(self) -> None:
    """注入 ToolContext，加载工具插件，设置 HITL broadcast。"""
    self._tool_ctx = ToolContext(
        session_id=getattr(self._session, "_session_id", "default"),
        user_id=self._user_name,
        services={
            "memory": self._long_term,
            "scheduler": self._scheduler,
            "config": self._config,
            "session": self._session,
            "_tool_names": list(self._tool_manager.tool_names),
            "_fsm_state": str(self._fsm.state),
            "_session_msg_count": len(self._session._messages),
            "_tool_count": len(self._tool_manager._plugins),
        },
    )
    self._cognitive.set_tool_context(self._tool_ctx)

    self._plugin_ctx.session_id = getattr(self._session, "_session_id", "default")
    self._plugin_ctx.memory = self._long_term
    self._plugin_ctx.scheduler = self._scheduler
    await self._tool_manager.load_all(ALL_PLUGINS, self._plugin_ctx)
    self._tool_manager.set_hitl_broadcast(self._output)
    self._scheduler.set_tool_context(self._tool_ctx)
```

### `_start_worker_pool() -> None`
```python
async def _start_worker_pool(self) -> None:
    """启动 Worker Pool + embedding 预加载后台任务。"""
    await self._scheduler.start()
    self._embed_task = asyncio.create_task(
        self._long_term._ensure_embedding(), name="embedding-preload")
```

### `_start_idle_and_events() -> None`
```python
async def _start_idle_and_events(self) -> None:
    """启动 IdleMonitor + 主事件循环（独立 asyncio.Task）。"""
    await self._idle.start()
    self._event_task = asyncio.create_task(self._process_events())
```

### `_start_tts() -> None`
```python
async def _start_tts(self) -> None:
    """创建 TTS 后端（条件执行——由 config.tts.enabled 控制）。"""
    if self._config.tts.enabled:
        from Module.tts import TTSFactory
        try:
            self._tts = TTSFactory.create(self._config.tts.model)
            self._cognitive.set_tts(self._tts)
            logger.info("TTS backend connected")
        except Exception:
            logger.info("TTS not available — text-only mode")
```

### `_start_task_system() -> None`
```python
async def _start_task_system(self) -> None:
    """创建 TaskSystem + DelegationPreparer + HITLNotifier，注入 CognitiveLoop。"""
    from Module.task_system.task_system import TaskSystem
    from Module.task_system.delegation_preparer import DelegationPreparer
    from Module.task_system.infra import AgentInfra
    from Module.task_system.schema import ConstraintConfig

    _ts_infra = AgentInfra(
        llm=self._llm.get_raw_client(),
        milvus=None,
        http=None,
        tool_registry=self._tool_manager,
        constraints=ConstraintConfig(),
    )

    class _EventBusHITLNotifier:
        def __init__(self, bus):
            self._bus = bus
        async def request_approval(self, event_data):
            pass  # v1: 审批由 ToolManager SafetyPipeline 直接处理，不经过此通道
        async def cancel_approval(self, approval_ids):
            pass  # v1: 同上，审批全流程走 ToolManager，不经过此通道

    _hitl = _EventBusHITLNotifier(self._bus)
    self._task_system = TaskSystem(infra=_ts_infra, hitl_notifier=_hitl)
    await self._task_system.start()
    self._delegation_preparer = DelegationPreparer()

    # 注入到 CognitiveLoop
    self._cognitive.set_task_system(self._task_system)
    self._cognitive.set_delegation_preparer(self._delegation_preparer)
    self._cognitive.set_user_profile(None)  # v1: UserProfile 未实现
    self._cognitive.set_on_background_task(self._register_background_task)
```

### `_start_mailbox_polling() -> None`
```python
def _start_mailbox_polling(self) -> None:
    """启动后台任务 mailbox 轮询（独立 asyncio.Task）。"""
    self._mailbox_task = asyncio.create_task(self._poll_mailboxes())
```

### `_print_startup_banner(mongo_ok: bool, milvus_ok: bool) -> None`
```python
def _print_startup_banner(self, mongo_ok: bool, milvus_ok: bool) -> None:
    """打印启动横幅。"""
    db_status = []
    if mongo_ok:
        db_status.append("MongoDB")
    if milvus_ok:
        db_status.append("Milvus")
    db_str = " + ".join(db_status) if db_status else "in-memory"

    print(f"\n{'='*50}")
    print(f"  {self._character.name} Agent 已启动")
    print(f"  LLM: {self._llm.model}")
    print(f"  记忆: {db_str}  ({self._long_term.count} entries)")
    tts_model = self._config.tts.model
    print(f"  TTS: {'已连接' if self._tts else '未连接（纯文本模式）'}  (model={tts_model})")
    print(f"  人格: {', '.join(self._character.traits)}")
    print(f"  工具: {', '.join(self._tool_manager.tool_names) or '无'}")
    print(f"{'='*50}")
```

### 拆分后的 `start()` (~50 行)
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

---

## 2. `stop()` 拆分：105→40 行

提取 11 个私有方法。关闭顺序必须保持：先停消费者，再停生产者，最后关持久化。

### `_flush_consolidator() -> None`

```python
async def _flush_consolidator(self) -> None:
    """Memor consolidation buffer 落盘，防止未持久化的记忆丢失。"""
    if self._consolidator:
        try:
            items = await asyncio.wait_for(self._consolidator.flush(), timeout=5.0)
            if items:
                logger.info("Flushed %d consolidator items on shutdown", len(items))
        except asyncio.TimeoutError:
            logger.warning("Consolidator flush timed out during shutdown")
        except Exception:
            logger.debug("Consolidator flush failed", exc_info=True)
```

### `_stop_event_task() -> None`
```python
async def _stop_event_task(self) -> None:
    """停止 _process_events 事件循环——防止它在 shutdown 期间继续 post 新任务。"""
    if self._event_task:
        logger.debug("Cancelling event task...")
        self._event_task.cancel()
        try:
            await asyncio.wait_for(self._event_task, timeout=3.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        self._event_task = None
```

### `_stop_cognitive_and_tts() -> None`
```python
def _stop_cognitive_and_tts(self) -> None:
    """Cancel 当前 cognitive task + TTS 任务（belt-and-suspenders）。"""
    self._cognitive.cancel_current()
    self._cognitive._tts_mgr.cancel_all()
```

### `_stop_embedding_if_running() -> None`
```python
async def _stop_embedding_if_running(self) -> None:
    """取消并等待 embedding 预加载任务完成。"""
    if getattr(self, "_embed_task", None) and not self._embed_task.done():
        self._embed_task.cancel()
    # 等待 task 完成——cancel 后仍需 await 消耗 CancelledError
    if getattr(self, "_embed_task", None):
        try:
            await asyncio.wait_for(self._embed_task, timeout=3.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

### `_stop_mailbox() -> None`
```python
def _stop_mailbox(self) -> None:
    """停止后台任务 mailbox 轮询。"""
    if self._mailbox_task:
        self._mailbox_task.cancel()
        self._mailbox_task = None
```

### `_stop_task_system() -> None`
```python
async def _stop_task_system(self) -> None:
    """停止 TaskSystem（5s 超时）。"""
    if self._task_system:
        try:
            await asyncio.wait_for(self._task_system.stop(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("TaskSystem stop timed out")
```

### `_stop_scheduler() -> None`
```python
async def _stop_scheduler(self) -> None:
    """停止 Scheduler Worker Pool。"""
    logger.debug("Stopping scheduler Worker Pool...")
    await self._scheduler.stop()
```

### `_stop_tool_manager() -> None`
```python
async def _stop_tool_manager(self) -> None:
    """关闭 ToolManager（清理所有工具插件）。"""
    logger.debug("Stopping ToolManager...")
    await self._tool_manager.shutdown()
```

### `_stop_idle() -> None`
```python
async def _stop_idle(self) -> None:
    """停止 IdleMonitor。"""
    logger.debug("Stopping idle monitor...")
    await self._idle.stop()
```

### `_stop_persistence() -> None`
```python
async def _stop_persistence(self) -> None:
    """持久化关闭：session.save() → long_term.close() → EmbeddingFactory.shutdown()。
    
    顺序要求：先 save session（需要 L2 连接仍然有效），再关闭 L2，最后关 embedding。
    """
    logger.debug("Saving session...")
    try:
        await asyncio.wait_for(self._session.save(), timeout=3.0)
    except asyncio.TimeoutError:
        logger.warning("Session save timed out")
    except Exception:
        logger.debug("Session save failed", exc_info=True)

    logger.debug("Closing long-term memory...")
    try:
        await asyncio.wait_for(self._long_term.close(), timeout=3.0)
    except asyncio.TimeoutError:
        logger.warning("Long-term memory close timed out")

    logger.debug("Shutting down embedding factory...")
    try:
        await asyncio.wait_for(EmbeddingFactory.shutdown(), timeout=5.0)
    except asyncio.TimeoutError:
        logger.warning("Embedding factory shutdown timed out")
```

### `_stop_llm_and_tts() -> None`
```python
async def _stop_llm_and_tts(self) -> None:
    """关闭 LLM 和 TTS 客户端连接。"""
    logger.debug("Closing LLM client...")
    try:
        await asyncio.wait_for(self._llm.close(), timeout=3.0)
    except asyncio.TimeoutError:
        logger.warning("LLM close timed out")

    if self._tts:
        logger.debug("Closing TTS backend...")
        try:
            await asyncio.wait_for(self._tts.close(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("TTS close timed out")
```

### `_stop_output() -> None`
```python
async def _stop_output(self) -> None:
    """关闭所有输出通道。"""
    logger.debug("Closing output channels...")
    try:
        await asyncio.wait_for(self._output.close(), timeout=3.0)
    except asyncio.TimeoutError:
        logger.warning("Output close timed out")
```

### 拆分后的 `stop()` (~40 行)
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

---

## 3. `_process_events()` 拆分：100→30 行

提取 5 个事件 handler。每个 handler 仅接收已解包的 `event.data`，
保持原有日志和异常处理。

### `_handle_interrupt(data: dict) -> None`
```python
def _handle_interrupt(self, data: dict) -> None:
    """处理 INTERRUPT 事件：cancel cognitive task，清空队列。
    
    保留 STATE_CHANGE 事件（前端 UI 需要），丢弃其余排队事件。
    """
    set_request_id("-")
    logger.info("EventBus: INTERRUPT (%s)", data.get("source", "?") if data else "?")
    self._cognitive.cancel_current()
    for drained in self._bus.drain():
        if drained.type == EventType.STATE_CHANGE:
            self._bus.post(drained.type, drained.data, priority=drained.priority)
    self._pending_messages.clear()
```

### `_handle_user_input(data: dict) -> None`
```python
def _handle_user_input(self, data: dict) -> None:
    """处理 USER_INPUT 事件：创建 cognitive task 异步运行。"""
    text = data.get("text", "")
    user_name = data.get("user_name", "妖梦")
    stt_emotion = data.get("emotion", "")
    stt_events = data.get("events", [])
    content = data.get("content")
    rid = data.get("request_id", "")
    logger.info("EventBus: USER_INPUT 到达 (来源=%s, STT=%s)",
               "websocket" if stt_emotion else "text", stt_emotion or "无")
    _on_tool_calls = lambda calls: self._scheduler.dispatch_tool_calls(
        parent_id=rid, calls=calls,
    )
    task = asyncio.create_task(
        self._cognitive.process(text, user_name=user_name,
                                stt_emotion=stt_emotion,
                                stt_events=stt_events,
                                _on_tool_calls=_on_tool_calls,
                                content=content),
        name=f"cognitive-{text[:20] if text else 'multimodal'}"
    )
    self._cognitive._current_task = task
```

### `_handle_system_tick(data: dict) -> None`
```python
def _handle_system_tick(self, data: dict) -> None:
    """处理 SYSTEM_TICK 事件——silence_timeout 时主动开启话题。"""
    reason = data.get("reason", "")
    if reason == "silence_timeout":
        elapsed = data.get("elapsed", 0)
        rid = self.next_request_id()
        set_request_id(rid)
        _on_tool_calls = lambda calls: self._scheduler.dispatch_tool_calls(
            parent_id=rid, calls=calls,
        )
        task = asyncio.create_task(
            self._cognitive.process(
                f"（系统提示：用户已经{int(elapsed)}秒没有说话了，请自然地开启一个话题）",
                _on_tool_calls=_on_tool_calls,
            ),
            name="cognitive-proactive"
        )
        self._cognitive._current_task = task
```

### `_handle_task_result(data: dict) -> None`
```python
def _handle_task_result(self, data: dict) -> None:
    """处理 TASK_RESULT 事件——后台任务完成，唤醒 CognitiveLoop。"""
    rid = self.next_request_id()
    set_request_id(rid)
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

### `_handle_state_change(data: dict) -> None`
```python
def _handle_state_change(self, data: dict) -> None:
    """处理 STATE_CHANGE 事件——FSM 状态变更。
    
    IDLE/IDLE_BACKGROUND_TASK 时检查排队消息（FIFO 出队），
    通知所有输出通道状态变更（前端据此切换动画）。
    """
    state = data.get("state", "") if data else ""
    logger.info("FSM → %s", state)
    if state in ("idle", "idle_background_task"):
        set_request_id("-")
        if self._pending_messages:
            pending = self._pending_messages.pop(0)
            logger.info("排队消息出队 (%d 条剩余)", len(self._pending_messages))
            self._bus.post(EventType.USER_INPUT, pending)
    await self._output.notify_state(state)
```

### 拆分后的 `_process_events()` (~30 行)
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
```

---

## 验证

```bash
conda activate Elysia

# 导入检查
python -c "from core.agent import Agent; print('import OK')"

# 相关单元测试
python -m pytest core/tests/test_phase3.py -v
python -m pytest core/tests/test_task_scheduler.py -v

# 完整核心测试套件
python -m pytest core/tests/ -v
```

## 不变原则

- 不改变任何公开 API 签名
- 不改变 `start()`/`stop()` 的执行顺序和并发度
- 不改变事件分发时序
- 所有子方法保留原有的 timeout、异常处理、日志语句
- 所有 `self._xxx` 赋值顺序完全保持原样
