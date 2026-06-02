"""Agent — wires all L0/L1/L2 components into a runnable Agent.

Usage:
    agent = Agent(config=config)
    await agent.start()
    await agent.process_message("你好")       # single turn
    await agent.run_repl()                   # interactive REPL
    await agent.stop()
"""

import asyncio
import logging

from Module.output import OutputManager, ConsoleChannel

from .app_config import AppConfig
from .event_bus import EventBus, EventPriority, EventType
from .agent_fsm import AgentFSM, AgentState
from .log_setup import set_request_id, get_request_id
from .character_config import CharacterConfig
from .memory.session import SessionMemory
from .memory.long_term import LongTermMemory
from .prompt_assembler import PromptAssembler
from Module.tool import ToolManager, ToolContext, PluginContext
from core.tools import ALL_PLUGINS
from .cognitive_loop import CognitiveLoop
from .idle_monitor import IdleMonitor
from .llm_adapter import LLMAdapter
from .emotion_engine import EmotionEngine
from .consolidator import MemoryConsolidator
from Module.embedding import EmbeddingFactory
from .task_scheduler import TaskScheduler

logger = logging.getLogger(__name__)


class Agent:
    """Complete Agent wired from L0/L1/L2 components."""

    def __init__(self, *, config: AppConfig):
        self._config = config
        self._user_name = config.agent.user_name

        # ---- L0: Infrastructure (from Module/) ----
        self._output = OutputManager()
        self._output.add_channel(ConsoleChannel())

        # ---- Character ----
        self._character = CharacterConfig.from_yaml(config.agent.character)

        # ---- L2: Memory ----
        self._session = SessionMemory(max_messages=config.memory.session_max_messages)
        self._long_term = LongTermMemory(
            max_entries=config.memory.ltm_max_entries,
            max_age_days=config.memory.ltm_max_age_days,
            embed_timeout=config.memory.ltm_embed_timeout,
        )

        # ---- L2: Tools ----
        self._tool_manager = ToolManager()

        # 构造 PluginContext（session_id 在 start() 中注入）
        self._plugin_ctx = PluginContext(
            session_id="",  # 在 start() 中更新
            config=self._config,
        )

        # ---- L1: Cognitive ----
        self._bus = EventBus()
        self._fsm = AgentFSM()
        self._llm = LLMAdapter(model=config.llm.model)
        self._prompt = PromptAssembler(self._character, self._session, self._long_term)

        # TTS is disabled by default (requires GPT-SoVits server at localhost:9880).
        # Enable via config.yaml (TTS.enabled: true) or CLI --tts flag.
        self._tts = None
        if config.tts.enabled:
            from Module.tts import TTSFactory
            try:
                self._tts = TTSFactory.create("gpt-sovits/default")
                logger.info("TTS backend connected")
            except Exception:
                logger.info("TTS not available — text-only mode")

        self._emotion = EmotionEngine(
            character=self._character,
            session=self._session,
            output=self._output,
        )

        self._consolidator = MemoryConsolidator(
            llm=self._llm,
            memory=self._long_term,
            character_name=self._character.name,
        )

        self._cognitive = CognitiveLoop(
            llm=self._llm,
            fsm=self._fsm,
            event_bus=self._bus,
            output=self._output,
            prompt=self._prompt,
            session=self._session,
            tool_executor=self._tool_manager,
            emotion=self._emotion,
            consolidator=self._consolidator,
            tts=self._tts,
        )

        self._idle = IdleMonitor(
            character=self._character,
            fsm=self._fsm,
            event_bus=self._bus,
        )

        # Worker Pool — 执行 AI 规划的工具子任务 + 后台任务
        self._scheduler = TaskScheduler(
            tool_executor=self._tool_manager,
            tool_ctx=None,  # 在 start() 中赋值 tool_ctx 后更新
            num_workers=5,
            checkpoint_path="data/scheduler_checkpoint.json",
        )

        self._running = False
        self._event_task: asyncio.Task | None = None
        self._req_counter: int = 0
        # FIFO 消息排队 — FSM != IDLE 时暂存消息
        self._pending_messages: list[dict] = []

    # ---- Lifecycle ----

    async def start(self) -> None:
        """Start the agent: connect persistence, idle monitor, event loop."""
        self._running = True

        # Connect persistence backends in parallel
        mongo_ok, milvus_ok = await asyncio.gather(
            self._session.connect_db(),
            self._long_term.connect(),
        )
        await self._seed_memories()
        if mongo_ok:
            await self._session.load()

        # 注入运行时上下文 — 工具通过 ctx.get("memory") 访问 L2 记忆
        self._tool_ctx = ToolContext(
            session_id=getattr(self._session, "_session_id", "default"),
            user_id=self._user_name,
            services={
                "memory": self._long_term,
                "scheduler": self._scheduler,
                "config": self._config,
                "_tool_names": list(self._tool_manager.tool_names),
                "_fsm_state": str(self._fsm.state),
                "_session_msg_count": len(self._session._messages),
                "_tool_count": len(self._tool_manager._plugins),
            },
        )
        self._cognitive.set_tool_context(self._tool_ctx)

        # 加载工具插件（setup() 中检测能力，失败则跳过）
        self._plugin_ctx.session_id = getattr(self._session, "_session_id", "default")
        self._plugin_ctx.memory = self._long_term
        self._plugin_ctx.scheduler = self._scheduler
        await self._tool_manager.load_all(ALL_PLUGINS, self._plugin_ctx)
        self._tool_manager.set_hitl_broadcast(self._output)
        self._scheduler.set_tool_context(self._tool_ctx)

        # Start Worker Pool
        await self._scheduler.start()

        # Preload embedding in background (runs in thread pool, won't block event loop)
        self._embed_task = asyncio.create_task(
            self._long_term._ensure_embedding(), name="embedding-preload")

        await self._idle.start()
        self._event_task = asyncio.create_task(self._process_events())

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
        print(f"  TTS: {'已连接' if self._tts else '未连接（纯文本模式）'}")
        print(f"  人格: {', '.join(self._character.traits)}")
        print(f"  工具: {', '.join(self._tool_manager.tool_names) or '无'}")
        print(f"{'='*50}")

    async def stop(self) -> None:
        """Stop the agent, save session, clean up resources."""
        logger.info("Agent stopping...")
        self._running = False

        # 清空排队 — shutdown 期间不处理新消息
        self._pending_messages.clear()

        # Flush consolidator buffer before shutdown
        if self._consolidator:
            try:
                items = await asyncio.wait_for(self._consolidator.flush(), timeout=5.0)
                if items:
                    logger.info("Flushed %d consolidator items on shutdown", len(items))
            except asyncio.TimeoutError:
                logger.warning("Consolidator flush timed out during shutdown")
            except Exception:
                logger.debug("Consolidator flush failed", exc_info=True)

        # 先停 _process_events — 防止它在 shutdown 期间继续 post 新任务
        if self._event_task:
            logger.debug("Cancelling event task...")
            self._event_task.cancel()
            try:
                await asyncio.wait_for(self._event_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._event_task = None

        # Cancel cognitive + TTS tasks (may be idle, but belt-and-suspenders)
        for attr in ("_current_task", "_tts_task"):
            t = getattr(self._cognitive, attr, None)
            if t and not t.done():
                t.cancel()
        for t in list(getattr(self._cognitive, "_tts_tasks", [])):
            if not t.done():
                t.cancel()

        # Cancel embedding preload if still running
        if getattr(self, "_embed_task", None) and not self._embed_task.done():
            self._embed_task.cancel()

        logger.debug("Stopping scheduler Worker Pool...")
        await self._scheduler.stop()

        logger.debug("Stopping ToolManager...")
        await self._tool_manager.shutdown()

        logger.debug("Stopping idle monitor...")
        await self._idle.stop()

        # Cancel embedding task
        if getattr(self, "_embed_task", None):
            try:
                await asyncio.wait_for(self._embed_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        # Save session before closing (with timeout)
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

        logger.debug("Closing output channels...")
        try:
            await asyncio.wait_for(self._output.close(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("Output close timed out")

        logger.info("Agent stopped")

    def next_request_id(self) -> str:
        """生成并返回请求 ID，同时设置 ContextVar。供外部模块（STT 等）使用。"""
        self._req_counter += 1
        rid = f"{self._req_counter:03d}"
        set_request_id(rid)
        return rid

    async def process_message(self, text: str, user_name: str = "妖梦") -> None:
        """Process a single user message through the pipeline.

        If the agent is busy, the message is queued and processed when IDLE.
        Explicit interrupt (stop button, VAD) still works via EventBus INTERRUPT.
        """
        if not text.strip():
            return

        rid = self.next_request_id()

        self._idle.touch()

        if self._fsm.state != AgentState.IDLE:
            # Queue message instead of interrupting
            max_queue = self._config.agent.max_pending_messages
            if len(self._pending_messages) >= max_queue:
                logger.warning("消息队列已满 (%d) — 丢弃消息", max_queue)
                return
            logger.info("Agent busy — 消息排队 (state=%s, queue=%d)",
                       self._fsm.state, len(self._pending_messages) + 1)
            self._pending_messages.append({
                "text": text, "user_name": user_name, "request_id": rid,
            })
            return

        self._bus.post(EventType.USER_INPUT, {"text": text, "user_name": user_name,
                                               "request_id": rid})

    # ---- REPL ----

    async def run_server(self) -> None:
        """Run agent in WebSocket server mode. Blocks until Ctrl+C."""
        await self.start()
        host = self._config.server.host
        port = self._config.server.port
        stt_enabled = self._config.stt.enabled

        stt_transcribe = None
        stt_emotion_state: dict | None = None
        if stt_enabled:
            try:
                from Module.stt import STTFactory
                stt_backend = STTFactory.create("sensevoice/sense-voice")

                # 用可变容器传递 STT 情绪/事件 — server 读取、wrapper 更新
                stt_emotion_state = {"emotion": "NEUTRAL", "events": []}

                async def transcribe_wrapper(audio: bytes) -> str:
                    text = await stt_backend.transcribe(audio)
                    stt_emotion_state["emotion"] = stt_backend.last_emotion
                    stt_emotion_state["events"] = list(stt_backend.last_events)
                    return text

                stt_transcribe = transcribe_wrapper
                logger.info("STT backend connected for server mode")
            except Exception:
                logger.info("STT not available — text-only mode")

        from Module.input.server import create_app
        from aiohttp import web

        app = create_app(self, stt_transcribe=stt_transcribe, serve_static=True,
                        stt_emotion_state=stt_emotion_state)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()

        print(f"\n{'='*50}")
        print(f"  {self._character.name} WebSocket Server")
        print(f"  地址: ws://{host}:{port}/ws")
        print(f"  前端: http://{host}:{port}/")
        print(f"  STT: {'已连接' if stt_transcribe else '未连接'}")
        print(f"  TTS: {'已连接' if self._tts else '未连接'}")
        print(f"  退出: Ctrl+C 或浏览器关闭服务按钮")
        print(f"{'='*50}")

        try:
            while self._running:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await self.stop()
            try:
                await asyncio.wait_for(runner.cleanup(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("HTTP server cleanup timed out — forcing exit")
            print("再见！")

    async def run_repl(self) -> None:
        """Run an interactive REPL loop (stdin). Blocks until /quit or Ctrl+D."""
        await self.start()

        print("输入消息开始对话，/quit 退出，Ctrl+C 打断\n")

        try:
            while self._running:
                line = await asyncio.to_thread(input, "你: ")
                if line.strip().lower() in ("/quit", "/exit", "/q"):
                    break
                if line.strip():
                    await self.process_message(line.strip())
        except EOFError:
            pass
        finally:
            await self.stop()
            print("再见！")

    # ---- Event processing loop ----

    async def _process_events(self) -> None:
        """Main event loop: consume events from EventBus and dispatch.

        Cognitive processing runs in a separate task so the event loop stays
        responsive to INTERRUPT events during LLM streaming.
        """
        while self._running:
            try:
                event = await self._bus.next()
            except RuntimeError:
                break

            # 从事件数据提取 request_id 并设置 ContextVar（修复跨 task 传播）
            rid = (event.data or {}).get("request_id", "")
            if rid:
                set_request_id(rid)

            if event.type == EventType.INTERRUPT:
                set_request_id("-")
                logger.info("EventBus: INTERRUPT (%s)", event.data.get("source", "?") if event.data else "?")
                self._cognitive.cancel_current()
                # Drain pending USER_INPUT and message queue, preserve STATE_CHANGE for UI
                for drained in self._bus.drain():
                    if drained.type == EventType.STATE_CHANGE:
                        self._bus.post(drained.type, drained.data, priority=drained.priority)
                self._pending_messages.clear()

            elif event.type == EventType.USER_INPUT:
                data = event.data or {}
                text = data.get("text", "")
                user_name = data.get("user_name", "妖梦")
                stt_emotion = data.get("emotion", "")
                stt_events = data.get("events", [])
                logger.info("EventBus: USER_INPUT 到达 (来源=%s, STT=%s)",
                           "websocket" if stt_emotion else "text", stt_emotion or "无")
                # Wire tool-call dispatch to Scheduler Worker Pool
                _on_tool_calls = lambda calls: self._scheduler.dispatch_tool_calls(
                    parent_id=rid, calls=calls,
                )
                task = asyncio.create_task(
                    self._cognitive.process(text, user_name=user_name,
                                            stt_emotion=stt_emotion,
                                            stt_events=stt_events,
                                            _on_tool_calls=_on_tool_calls),
                    name=f"cognitive-{text[:20]}"
                )
                self._cognitive._current_task = task

            elif event.type == EventType.SYSTEM_TICK:
                data = event.data or {}
                reason = data.get("reason", "")
                if reason == "silence_timeout":
                    elapsed = data.get("elapsed", 0)
                    # 生成 request_id — SYSTEM_TICK 不带 rid，需主动创建
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

            elif event.type == EventType.STATE_CHANGE:
                state = event.data.get("state", "") if event.data else ""
                logger.info("FSM → %s", state)
                if state == "idle":
                    set_request_id("-")
                    # 检查排队消息 — FIFO 出队
                    if self._pending_messages:
                        pending = self._pending_messages.pop(0)
                        logger.info("排队消息出队 (%d 条剩余)", len(self._pending_messages))
                        self._bus.post(EventType.USER_INPUT, pending)
                for ch in self._output._channels:
                    try:
                        await ch.on_state(state)
                    except Exception:
                        logger.warning(
                            "Channel %s on_state(%s) failed",
                            type(ch).__name__, state, exc_info=True,
                        )

    # ---- Internal: seed data + tools ----

    async def _seed_memories(self) -> None:
        """Seed long-term memory with demo entries via normal insert() path."""
        entries = [
            ("上周和妖梦约定周六去爬香山", {"memory_type": "plan", "poignancy": 7}),
            ("妖梦最喜欢的颜色是蓝色", {"memory_type": "preference", "poignancy": 8}),
            ("昨天妖梦说他最近在学习 Rust 语言", {"memory_type": "fact", "poignancy": 6}),
            ("Elysia 这个名字来源于希腊神话的极乐净土", {"memory_type": "knowledge", "poignancy": 5}),
            ("上次推荐的电影《星际穿越》妖梦还没看完", {"memory_type": "interaction", "poignancy": 4}),
        ]
        for text, meta in entries:
            await self._long_term.insert(text, **meta)

    # Tool registration is now done via ToolManager.load_all() in start()
