"""Agent — wires all L0/L1/L2 components into a runnable Agent.

Usage:
    agent = Agent(config=config)
    await agent.start()
    await agent.process_message("你好")       # single turn
    await agent.run_repl()                   # interactive REPL
    await agent.stop()
"""

import asyncio
import time
import os
from enum import Enum, auto
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


class RunState(Enum):
    """Agent 生命周期状态"""
    STOPPED = auto()      # 初始 / 已停止
    RUNNING = auto()      # 运行中
    STOPPING = auto()     # 正常关机中
    RESTARTING = auto()   # 重启中


class Agent:
    """Complete Agent wired from L0/L1/L2 components."""

    def __init__(self, *, config: AppConfig, config_path: str | None = None):
        self._config = config
        self._config_path = config_path
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

        self._tts = None  # 在 _start_tts() 中创建
        self._stt_ok = False  # STT 连通性标志 — 在 run_server() 中设置

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

        # ---- Token 统计 ----
        self._token_tracker = None  # 在 start() 中创建（需要 session_id）

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
            token_tracker=self._token_tracker,
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

        self._state = RunState.STOPPED
        self._event_task: asyncio.Task | None = None
        self._req_counter: int = 0
        # FIFO 消息排队 — FSM != IDLE 时暂存消息
        self._pending_messages: list[dict] = []

        # ---- TaskSystem ----
        self._task_system = None   # TaskSystem | None
        self._delegation_preparer = None  # DelegationPreparer | None
        self._background_tasks: dict[str, dict] = {}  # task_id -> {intent, dispatched_at, mailbox}
        self._mailbox_task: asyncio.Task | None = None

    # ---- Lifecycle ----

    async def start(self) -> None:
        """Start the agent: connect persistence, idle monitor, event loop."""
        self._state = RunState.RUNNING
        self._started_at = time.monotonic()  # 启动时刻打点，用于 get_runtime_config 显示运行时长

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

        # 9. 保存启动状态供入口打印横幅
        self._mongo_ok = mongo_ok
        self._milvus_ok = milvus_ok

    async def _start_persistence(self) -> tuple[bool, bool]:
        """连接 MongoDB/Milvus，加载 session，恢复 JSONL 快照。"""
        mongo_ok, milvus_ok = await asyncio.gather(
            self._session.connect_db(),
            self._long_term.connect(),
        )
        if mongo_ok:
            await self._session.load()
            # MongoDB 离线期间 session 可能有更新的 JSONL 快照，恢复到 MongoDB
            if os.path.exists(self._session.pending_file):
                if await self._session.load_from_jsonl(merge=True):
                    if await self._session.save():
                        os.remove(self._session.pending_file)
                        logger.info("Session JSONL 已导入 MongoDB 并删除")
                    else:
                        logger.warning("Session MongoDB 写入失败，保留 JSONL")
        return mongo_ok, milvus_ok

    async def _start_token_and_prompt(self, mongo_ok: bool) -> None:
        """创建 TokenTracker + PromptRecorder，注入 CognitiveLoop。"""
        from core.token_tracker import TokenTracker
        self._token_tracker = TokenTracker(
            session_id=self._session.session_id,
            output_manager=self._output,
            document_db=self._session.db if mongo_ok else None,
        )
        self._cognitive.set_token_tracker(self._token_tracker)
        if mongo_ok:
            await self._token_tracker.flush_pending()

        from core.prompt_recorder import PromptRecorder
        self._prompt_recorder = PromptRecorder(max_records=20)
        self._cognitive.set_prompt_recorder(self._prompt_recorder)

    async def _start_tool_context(self) -> None:
        """注入 ToolContext，加载工具插件，设置 HITL broadcast。"""
        self._tool_ctx = ToolContext(
            session_id=self._session.session_id,
            user_id=self._user_name,
            services={
                "memory": self._long_term,
                "scheduler": self._scheduler,
                "config": self._config,
                "session": self._session,
                "_tool_manager": self._tool_manager,          # ToolManager 实例
                "_started_at": self._started_at,               # 启动时刻
                "_token_tracker": self._token_tracker,         # TokenTracker 实例
                "_tool_names": list(self._tool_manager.tool_names),
                "_fsm_state": str(self._fsm.state),
                "_session_msg_count": len(self._session.messages),
                "_tool_count": len(self._tool_manager._plugins),
            },
        )
        self._cognitive.set_tool_context(self._tool_ctx)

        self._plugin_ctx.session_id = self._session.session_id
        self._plugin_ctx.memory = self._long_term
        self._plugin_ctx.scheduler = self._scheduler
        await self._tool_manager.load_all(ALL_PLUGINS, self._plugin_ctx)
        # 修复：load_all 后更新工具名列表（之前在此处快照时 _plugins 为空）
        self._tool_ctx.services["_tool_names"] = list(self._tool_manager.tool_names)
        self._tool_ctx.services["_tool_count"] = len(self._tool_manager._plugins)
        self._tool_manager.set_hitl_broadcast(self._output)
        self._scheduler.set_tool_context(self._tool_ctx)

    async def _start_worker_pool(self) -> None:
        """启动 Worker Pool + embedding 预加载后台任务。"""
        await self._scheduler.start()
        self._embed_task = asyncio.create_task(
            self._long_term._ensure_embedding(), name="embedding-preload")

    async def _start_idle_and_events(self) -> None:
        """启动 IdleMonitor + 主事件循环（独立 asyncio.Task）。"""
        await self._idle.start()
        self._event_task = asyncio.create_task(self._process_events())

    async def _start_tts(self) -> None:
        """创建 TTS 后端（条件执行——由 config.tts.enabled 控制）。

        创建后测试连通性，不可用时自动降级为纯文本模式。
        """
        if self._config.tts.enabled:
            from Module.tts import TTSFactory
            try:
                self._tts = TTSFactory.create(self._config.tts.model)
                # 快速连通性测试——1s 超时，避免卡启动
                import asyncio as _asyncio
                result = await _asyncio.wait_for(self._tts.test(), timeout=1.0)
                if result.get("ok"):
                    self._cognitive.set_tts(self._tts)
                    logger.info("TTS backend connected")
                else:
                    self._tts = None
                    logger.info("TTS not available — text-only mode (%s)", result.get("error", "unknown"))
            except Exception:
                self._tts = None
                logger.info("TTS not available — text-only mode")

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
        # 注入 TaskSystem 到 ToolContext，供 get_runtime_config 查询活跃任务
        self._tool_ctx.services["_task_system"] = self._task_system
        self._delegation_preparer = DelegationPreparer()

        # 注入到 CognitiveLoop
        self._cognitive.set_task_system(self._task_system)
        self._cognitive.set_delegation_preparer(self._delegation_preparer)
        from Module.task_system.schema import UserProfile
        self._cognitive.set_user_profile(
            UserProfile(display_name=self._user_name)
        )
        self._cognitive.set_on_background_task(self._register_background_task)

    def _start_mailbox_polling(self) -> None:
        """启动后台任务 mailbox 轮询（独立 asyncio.Task）。"""
        self._mailbox_task = asyncio.create_task(self._poll_mailboxes())

    def _print_startup_banner(self, mongo_ok: bool, milvus_ok: bool) -> str:
        """构建启动横幅字符串。调用方负责打印/记录。"""
        db_parts = []
        if mongo_ok: db_parts.append("MongoDB")
        if milvus_ok: db_parts.append("Milvus")
        db_str = " + ".join(db_parts) if db_parts else "in-memory"
        tts_model = self._config.tts.model

        lines = [
            f"\n{'='*50}",
            f"  {self._character.name} Agent 已启动",
            f"  LLM: {self._llm.model}",
            f"  记忆: {db_str}  ({self._long_term.count} entries)",
            f"  人格: {', '.join(self._character.traits)}",
            f"  工具: {', '.join(self._tool_manager.tool_names) or '无'}",
            f"{'='*50}",
        ]
        banner = "\n".join(lines)
        logger.info(banner)
        return banner  # 调用方可 continue 追加服务器信息

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
        await self._stop_mailbox()

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

    # ---- stop() sub-methods ----

    async def _flush_consolidator(self) -> None:
        """Memory consolidation buffer 落盘，防止未持久化的记忆丢失。"""
        if self._consolidator:
            try:
                items = await asyncio.wait_for(self._consolidator.flush(), timeout=5.0)
                if items:
                    logger.info("Flushed %d consolidator items on shutdown", len(items))
            except asyncio.TimeoutError:
                logger.warning("Consolidator flush timed out during shutdown")
            except Exception:
                logger.debug("Consolidator flush failed", exc_info=True)

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

    def _stop_cognitive_and_tts(self) -> None:
        """Cancel 当前 cognitive task + TTS 任务（belt-and-suspenders）。"""
        self._cognitive.cancel_current()
        self._cognitive._tts_mgr.cancel_all()

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

    async def _stop_mailbox(self) -> None:
        """停止后台任务 mailbox 轮询——cancel 后必须 await 消耗 CancelledError。"""
        if self._mailbox_task:
            self._mailbox_task.cancel()
            try:
                await asyncio.wait_for(self._mailbox_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._mailbox_task = None

    async def _stop_task_system(self) -> None:
        """停止 TaskSystem（5s 超时）。"""
        if self._task_system:
            try:
                await asyncio.wait_for(self._task_system.stop(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("TaskSystem stop timed out")

    async def _stop_scheduler(self) -> None:
        """停止 Scheduler Worker Pool。"""
        logger.debug("Stopping scheduler Worker Pool...")
        await self._scheduler.stop()

    async def _stop_tool_manager(self) -> None:
        """关闭 ToolManager（清理所有工具插件）。"""
        logger.debug("Stopping ToolManager...")
        await self._tool_manager.shutdown()

    async def _stop_idle(self) -> None:
        """停止 IdleMonitor。"""
        logger.debug("Stopping idle monitor...")
        await self._idle.stop()

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

    async def _stop_output(self) -> None:
        """关闭所有输出通道。"""
        logger.debug("Closing output channels...")
        try:
            await asyncio.wait_for(self._output.close(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("Output close timed out")

    def next_request_id(self) -> str:
        """生成并返回请求 ID，同时设置 ContextVar。供外部模块（STT 等）使用。"""
        self._req_counter += 1
        rid = f"{self._req_counter:03d}"
        set_request_id(rid)
        return rid

    async def switch_llm(self, model_id: str) -> dict:
        """热切换 LLM 模型。切换前打断当前 cognitive task 防止卡 thinking。"""
        try:
            self._cognitive.cancel_current()  # 打断正在进行的 LLM 调用
            self._llm.switch_model(model_id)
            logger.info("LLM switched to: %s", model_id)
            return {"ok": True}
        except Exception as e:
            logger.error("LLM switch failed: %s", e)
            return {"ok": False, "error": str(e)}

    async def switch_stt(self, model_id: str) -> dict:
        """运行时更新 STT model_id（下次 transcribe 请求生效）。"""
        self._config.stt.model = model_id
        logger.info("STT model set to: %s (takes effect on next request)", model_id)
        return {"ok": True}

    async def switch_tts(self, model_id: str) -> dict:
        """热切换 TTS 后端。更新 self._tts 和 self._cognitive._tts 引用。"""
        from Module.tts import TTSFactory
        try:
            if self._tts:
                try:
                    await self._tts.close()
                except Exception:
                    pass
            self._tts = TTSFactory.create(model_id)
            self._cognitive.set_tts(self._tts)  # 同步 CognitiveLoop 中的引用
            logger.info("TTS switched to: %s", model_id)
            return {"ok": True}
        except Exception as e:
            logger.error("TTS switch failed: %s", e)
            return {"ok": False, "error": str(e)}

    async def process_message(self, text: str = "", user_name: str = "妖梦",
                               content: list[dict] | None = None) -> None:
        """Process a single user message through the pipeline.

        text: 纯文本消息（兼容旧调用）
        content: 多模态内容数组（ChatMessageV2 content）— 新接口

        If the agent is busy, the message is queued and processed when IDLE.
        Explicit interrupt (stop button, VAD) still works via EventBus INTERRUPT.
        """
        if not text.strip() and not content:
            return

        rid = self.next_request_id()

        self._idle.touch()

        if self._fsm.state not in (AgentState.IDLE, AgentState.IDLE_BACKGROUND_TASK):
            # Queue message instead of interrupting
            max_queue = self._config.agent.max_pending_messages
            if len(self._pending_messages) >= max_queue:
                logger.warning("消息队列已满 (%d) — 丢弃消息", max_queue)
                return
            logger.info("Agent busy — 消息排队 (state=%s, queue=%d)",
                       self._fsm.state, len(self._pending_messages) + 1)
            self._pending_messages.append({
                "text": text, "user_name": user_name, "request_id": rid,
                "content": content,
            })
            return

        self._bus.post(EventType.USER_INPUT, {"text": text, "user_name": user_name,
                                               "request_id": rid,
                                               "content": content})

    # ---- REPL ----

    def get_wake_words(self) -> list[str]:
        """Return wake words from character config, for AudioBuffer in server mode."""
        return getattr(self._character, 'wake_words', [])

    async def run_server(self) -> None:
        """Run agent in WebSocket server mode. Blocks until Ctrl+C or shutdown.

        支持通过 /restart 端点在停止后重新加载配置并重启服务。
        """
        from Module.input.server import create_app
        from aiohttp import web

        await self.start()

        while True:
            host = self._config.server.host
            port = self._config.server.port
            stt_enabled = self._config.stt.enabled
            wake_word_enabled = self._config.wake_word.enabled

            stt_transcribe = None
            stt_emotion_state: dict | None = None
            if stt_enabled:
                try:
                    from Module.stt import STTFactory
                    stt_backend = STTFactory.create(self._config.stt.model)

                    # 快速连通性测试——1s 超时，避免卡启动
                    import asyncio as _asyncio
                    result = await _asyncio.wait_for(stt_backend.test(), timeout=1.0)
                    if not result.get("ok"):
                        raise RuntimeError(result.get("error", "服务不可用"))

                    stt_emotion_state = {"emotion": "NEUTRAL", "events": []}

                    async def transcribe_wrapper(audio: bytes) -> str:
                        text = await stt_backend.transcribe(audio)
                        stt_emotion_state["emotion"] = stt_backend.last_emotion
                        stt_emotion_state["events"] = list(stt_backend.last_events)
                        return text

                    stt_transcribe = transcribe_wrapper
                    self._stt_ok = True
                    logger.info("STT backend connected for server mode")
                except Exception:
                    logger.info("STT not available — text-only mode", exc_info=False)

            app = create_app(self, stt_transcribe=stt_transcribe, serve_static=True,
                            stt_emotion_state=stt_emotion_state,
                            wake_word_enabled=wake_word_enabled,
                            task_system=self._task_system)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host, port)
            await site.start()

            banner = self._print_startup_banner(
                getattr(self, '_mongo_ok', False),
                getattr(self, '_milvus_ok', False),
            )
            stt_model = self._config.stt.model
            tts_model = self._config.tts.model
            server_info = "\n".join([
                f"  ---",
                f"  WebSocket: ws://{host}:{port}/ws  |  前端: http://{host}:{port}/",
                f"  STT: {'已连接' if stt_transcribe else '未连接'}  (model={stt_model})",
                f"  TTS: {'已连接' if self._tts else '未连接'}  (model={tts_model})",
                f"  退出: Ctrl+C 或浏览器关闭服务按钮",
                f"{'='*50}",
            ])
            logger.info(server_info)

            try:
                while self._state == RunState.RUNNING:
                    await asyncio.sleep(1)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass

            was_restarting = self._state == RunState.RESTARTING
            await self.stop()
            try:
                await asyncio.wait_for(runner.cleanup(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("HTTP server cleanup timed out — forcing exit")

            if was_restarting:
                if self._config_path is not None:
                    from Module.config.loader import ConfigLoader
                    from core.app_config import AppConfig
                    self._config = ConfigLoader(AppConfig, yaml_path=self._config_path).load()
                    print("配置已重新加载，正在重启服务...")
                else:
                    # 无 config_path 时无法重新加载，复用当前配置
                    logger.warning(
                        "重启请求但缺少 config_path（Agent 可能以 config= 直接构造），"
                        "复用当前配置"
                    )
                await self.start()
                continue
            else:
                break

        print("再见！")

    async def run_repl(self) -> None:
        """Run an interactive REPL loop (stdin). Blocks until /quit or Ctrl+D."""
        await self.start()
        self._print_startup_banner(
            getattr(self, '_mongo_ok', False),
            getattr(self, '_milvus_ok', False),
        )
        print("\n输入消息开始对话，/quit 退出，Ctrl+C 打断\n")

        try:
            while self._state == RunState.RUNNING:
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

    # ---- Background task lifecycle ----

    def _register_background_task(self, task_id: str, mailbox, intent: str, dispatched_at: str) -> None:
        """CognitiveLoop 提交后台任务后调用——注册到轮询列表。"""
        self._background_tasks[task_id] = {
            "intent": intent,
            "dispatched_at": dispatched_at,
            "mailbox": mailbox,
        }

    async def _poll_mailboxes(self):
        """轮询所有活跃后台任务的 mailbox。每 500ms 一次。"""
        while self._state == RunState.RUNNING:
            await asyncio.sleep(0.5)
            for task_id, info in list(self._background_tasks.items()):
                mailbox = info.get("mailbox")
                if mailbox is None:
                    continue
                try:
                    while (event := mailbox.get_nowait()) is not None:
                        data = event.data or {}
                        if event.type.value == "completed":
                            self._bus.post(EventType.TASK_RESULT, {
                                "task_id": task_id,
                                "result": data.get("result", ""),
                                "original_intent": info["intent"],
                                "dispatched_at": info["dispatched_at"],
                            })
                            self._background_tasks.pop(task_id, None)
                            break  # task 已终态，跳出 while 处理下一个 task
                        elif event.type.value == "failed":
                            self._bus.post(EventType.TASK_RESULT, {
                                "task_id": task_id,
                                "result": f"任务执行失败: {data.get('error', '未知错误')}",
                                "original_intent": info["intent"],
                                "dispatched_at": info["dispatched_at"],
                            })
                            self._background_tasks.pop(task_id, None)
                            break  # task 已终态，跳出 while 处理下一个 task
                        elif event.type.value == "cancelled":
                            self._background_tasks.pop(task_id, None)
                            break  # task 已取消，跳出 while 处理下一个 task
                        # PROGRESS / PAUSED / unknown: v1 忽略，继续消费队列剩余事件
                except Exception:
                    logger.debug("mailbox poll error", exc_info=True)

    # ---- Event processing loop ----

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

    async def _handle_state_change(self, data: dict) -> None:
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

    # Tool registration is done via ToolManager.load_all() in start()
