"""DemoAgent — wires all L0/L1/L2 components into a runnable Agent.

Usage:
    agent = DemoAgent()
    await agent.start()
    await agent.process_message("你好")       # single turn
    await agent.run_repl()                   # interactive REPL
    await agent.stop()
"""

import asyncio
import logging
from pathlib import Path

from Module.output import OutputManager, ConsoleChannel

from .event_bus import EventBus, EventPriority, EventType
from .agent_fsm import AgentFSM, AgentState
from .character_config import CharacterConfig
from .memory.session import SessionMemory
from .memory.long_term import LongTermMemory
from .prompt_assembler import PromptAssembler
from .tool_registry import ToolRegistry
from .cognitive_loop import CognitiveLoop
from .idle_monitor import IdleMonitor
from .llm_adapter import LLMAdapter
from .emotion_engine import EmotionEngine

logger = logging.getLogger(__name__)


class DemoAgent:
    """Complete Agent wired from L0/L1/L2 components."""

    def __init__(self, character_path: str = "character/elysia.yaml"):
        # ---- L0: Infrastructure (from Module/) ----
        self._output = OutputManager()
        self._output.add_channel(ConsoleChannel())

        # ---- Character ----
        self._character = CharacterConfig.from_yaml(character_path)

        # ---- L2: Memory ----
        self._session = SessionMemory(max_messages=20)
        self._long_term = LongTermMemory()
        self._seed_memories()

        # ---- L2: Tools ----
        self._tools = ToolRegistry()
        self._register_tools()

        # ---- L1: Cognitive ----
        self._bus = EventBus()
        self._fsm = AgentFSM()
        self._llm = LLMAdapter()
        self._prompt = PromptAssembler(self._character, self._session, self._long_term)

        # TTS is disabled by default for the demo (requires GPT-SoVits server at localhost:9880).
        self._tts = None
        if __import__("os").getenv("DEMO_ENABLE_TTS") == "1":
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

        self._cognitive = CognitiveLoop(
            llm=self._llm,
            fsm=self._fsm,
            event_bus=self._bus,
            output=self._output,
            prompt=self._prompt,
            session=self._session,
            tools=self._tools,
            emotion=self._emotion,
            tts=self._tts,
        )

        self._idle = IdleMonitor(
            character=self._character,
            fsm=self._fsm,
            event_bus=self._bus,
        )

        self._running = False
        self._event_task: asyncio.Task | None = None

    # ---- Lifecycle ----

    async def start(self) -> None:
        """Start the agent: idle monitor + event processing loop."""
        self._running = True
        await self._idle.start()
        self._event_task = asyncio.create_task(self._process_events())
        print(f"\n{'='*50}")
        print(f"  {self._character.name} Demo Agent 已启动")
        print(f"  LLM: {self._llm.model}")
        print(f"  TTS: {'已连接' if self._tts else '未连接（纯文本模式）'}")
        print(f"  唤醒词: {', '.join(self._character.wake_words) or '无'}")
        print(f"  人格: {', '.join(self._character.traits)}")
        print(f"  工具: {', '.join(self._tools.tool_names) or '无'}")
        print(f"{'='*50}\n")

    async def stop(self) -> None:
        """Stop the agent and clean up resources."""
        self._running = False
        await self._idle.stop()
        if self._event_task:
            self._event_task.cancel()
        await self._llm.close()
        if self._tts:
            await self._tts.close()
        await self._output.close()
        logger.info("Agent stopped")

    async def process_message(self, text: str, user_name: str = "妖梦") -> None:
        """Process a single user message through the pipeline."""
        if not text.strip():
            return

        self._idle.touch()

        # Interrupt if currently processing
        if self._fsm.state != AgentState.IDLE:
            logger.info("Interrupting current processing (state=%s)", self._fsm.state)
            self._bus.post(EventType.INTERRUPT, {"source": "user_input"}, priority=EventPriority.INTERRUPT)
            await asyncio.sleep(0.1)

        self._bus.post(EventType.USER_INPUT, {"text": text, "user_name": user_name})

    # ---- REPL ----

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

            if event.type == EventType.INTERRUPT:
                logger.info("EventBus: INTERRUPT received")
                if self._cognitive._current_task and not self._cognitive._current_task.done():
                    self._cognitive._current_task.cancel()
                # Drain any pending USER_INPUT that arrived during the interrupted processing
                for drained in self._bus.drain():
                    pass

            elif event.type == EventType.USER_INPUT:
                data = event.data or {}
                text = data.get("text", "")
                user_name = data.get("user_name", "妖梦")
                # Spawn cognitive processing as independent task — frees event loop
                task = asyncio.create_task(
                    self._cognitive.process(text, user_name=user_name),
                    name=f"cognitive-{text[:20]}"
                )
                self._cognitive._current_task = task

            elif event.type == EventType.SYSTEM_TICK:
                data = event.data or {}
                reason = data.get("reason", "")
                if reason == "silence_timeout":
                    elapsed = data.get("elapsed", 0)
                    task = asyncio.create_task(
                        self._cognitive.process(
                            f"（系统提示：用户已经{int(elapsed)}秒没有说话了，请自然地开启一个话题）"
                        ),
                        name="cognitive-proactive"
                    )
                    self._cognitive._current_task = task

            elif event.type == EventType.STATE_CHANGE:
                state = event.data.get("state", "") if event.data else ""
                logger.debug("FSM → %s", state)

    async def _shutdown(self) -> None:
        self._running = False

    # ---- Internal: seed data + tools ----

    def _seed_memories(self) -> None:
        """Seed long-term memory with demo entries."""
        entries = [
            ("上周和妖梦约定周六去爬香山", {"type": "plan", "date": "2026-05-23"}),
            ("妖梦最喜欢的颜色是蓝色", {"type": "preference"}),
            ("昨天妖梦说他最近在学习 Rust 语言", {"type": "fact", "date": "2026-05-24"}),
            ("Elysia 这个名字来源于希腊神话的极乐净土", {"type": "knowledge"}),
            ("上次推荐的电影《星际穿越》妖梦还没看完", {"type": "interaction", "date": "2026-05-22"}),
        ]
        for text, meta in entries:
            self._long_term.insert(text, **meta)

    def _register_tools(self) -> None:
        """Register demo tools in the ToolRegistry."""

        # --- search_memory ---
        async def search_memory(query: str, limit: int = 5) -> str:
            """Search long-term memory for relevant entries."""
            results = self._long_term.search(query_text=query, top_k=limit)
            if not results:
                return "（记忆库中没有找到相关内容）"
            lines = [f"- {r.text} (meta: {r.metadata})" for r in results]
            return "\n".join(lines)

        self._tools.register(
            name="search_memory",
            description="在长期记忆中搜索相关信息，比如计划、偏好、历史对话等",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "limit": {"type": "integer", "description": "返回结果数量"},
                },
                "required": ["query"],
            },
            func=search_memory,
        )

        # --- get_time ---
        async def get_time() -> str:
            """Return the current time."""
            from datetime import datetime
            return datetime.now().strftime("%Y年%m月%d日 %H:%M:%S")

        self._tools.register(
            name="get_time",
            description="获取当前日期和时间",
            parameters={"type": "object", "properties": {}, "required": []},
            func=get_time,
        )

        # --- save_memory ---
        async def save_memory(content: str, type: str = "fact") -> str:
            """Save a new entry to long-term memory."""
            self._long_term.insert(content, type=type)
            return f"已保存: {content}"

        self._tools.register(
            name="save_memory",
            description="将重要信息保存到长期记忆",
            parameters={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "要保存的内容"},
                    "type": {"type": "string", "enum": ["fact", "plan", "preference", "note"], "default": "fact"},
                },
                "required": ["content"],
            },
            func=save_memory,
        )
