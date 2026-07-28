"""Agent 服务器入口 — run_server/run_repl + provider 切换。"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

from ._types import RunState
from ..utils import set_request_id

if TYPE_CHECKING:
    from ..config import AppConfig, CharacterConfig
    from ..adapter.llm_adapter import LLMAdapter
    from ..memory.long_term import LongTermMemory
    from ..adapter.tool_adapter import ToolAdapter
    from ..cognitive import CognitiveLoop
    from Module.task_system.task_system import TaskSystem
    from Module.tts.protocol import TTSBackend

logger = logging.getLogger(__name__)


class AgentServer:
    """Agent 服务器 mixin — HTTP/REPL 入口 + provider 切换。

    所有属性由 Agent.__init__ 初始化，本 mixin 不含 __init__。
    """

    if TYPE_CHECKING:
        _character: CharacterConfig
        _llm: LLMAdapter
        _long_term: LongTermMemory
        _tool_adapter: ToolAdapter
        _cognitive: CognitiveLoop
        _task_system: TaskSystem | None
        _state: RunState
        _config_path: str | None
        _config: AppConfig
        _tts: TTSBackend | None
        _stt_ok: bool
        _req_counter: int
        _mongo_ok: bool
        _milvus_ok: bool
        start: Any
        stop: Any
        process_message: Any

    def _print_startup_banner(self, mongo_ok: bool, milvus_ok: bool) -> str:
        """构建启动横幅字符串。调用方负责打印/记录。

        Args:
            mongo_ok: MongoDB 连接是否正常。
            milvus_ok: Milvus 连接是否正常。

        Returns:
            包含 LLM、记忆、人格、工具信息的启动横幅字符串。
        """
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
            f"  人格: {self._character.base_personality[:60]}{'...' if len(self._character.base_personality) > 60 else ''}",
            f"  工具: {len(self._tool_adapter.execute_now())} 个已加载",
            f"{'='*50}",
        ]
        banner = "\n".join(lines)
        logger.info(banner)
        return banner  # 调用方可 continue 追加服务器信息

    def next_request_id(self) -> str:
        """生成并返回请求 ID，同时设置 ContextVar。供外部模块（STT 等）使用。

        Returns:
            格式为 ``{req_counter:03d}`` 的请求 ID 字符串。
        """
        self._req_counter += 1
        rid = f"{self._req_counter:03d}"
        set_request_id(rid)
        return rid

    async def switch_llm(self, model_id: str) -> dict:
        """热切换 LLM 模型。切换前打断当前 cognitive task 防止卡 thinking。

        Args:
            model_id: 新的 LLM 模型标识符（如 ``deepseek/deepseek-v4-flash``）。

        Returns:
            包含 ``ok`` 状态和可选的 ``error`` 信息的字典。
        """
        try:
            self._cognitive.cancel_current()  # 打断正在进行的 LLM 调用
            self._llm.switch_model(model_id)
            logger.info("LLM switched to: %s", model_id)
            return {"ok": True}
        except Exception as e:
            logger.error("LLM switch failed: %s", e)
            return {"ok": False, "error": str(e)}

    async def switch_stt(self, model_id: str) -> dict:
        """运行时更新 STT model_id（下次 transcribe 请求生效）。

        Args:
            model_id: 新的 STT 模型标识符（如 ``sensevoice/sense-voice``）。

        Returns:
            包含 ``ok`` 状态的字典。
        """
        self._config.stt.model = model_id
        logger.info("STT model set to: %s (takes effect on next request)", model_id)
        return {"ok": True}

    async def switch_tts(self, model_id: str) -> dict:
        """热切换 TTS 后端。更新 self._tts 和 self._cognitive._tts 引用。

        Args:
            model_id: 新的 TTS 模型标识符（如 ``minimax/speech-2.8-hd``）。

        Returns:
            包含 ``ok`` 状态的字典，失败时含 ``error`` 信息。
        """
        from Module.tts import TTSFactory
        try:
            if self._tts:
                try:
                    await self._tts.close()
                except Exception:
                    pass
            self._tts = TTSFactory.create(model_id)
            self._cognitive.configure(tts=self._tts)  # 同步 CognitiveLoop 中的引用
            logger.info("TTS switched to: %s", model_id)
            return {"ok": True}
        except Exception as e:
            logger.error("TTS switch failed: %s", e)
            return {"ok": False, "error": str(e)}

    def get_wake_words(self) -> list[str]:
        """从角色配置中获取唤醒词，供服务端模式 AudioBuffer 使用。

        Returns:
            唤醒词字符串列表。
        """
        return getattr(self._character, 'wake_words', [])

    async def run_server(self) -> None:
        """Run agent in WebSocket server mode. Blocks until Ctrl+C or shutdown.

        支持通过 /restart 端点在停止后重新加载配置并重启服务。
        """
        from core.input.server import create_app
        from aiohttp import web

        await self.start()

        while True:
            host = self._config.server.host
            port = self._config.server.port
            stt_enabled = self._config.stt.enabled
            wake_word_enabled = self._config.wake_word.enabled

            stt_transcribe = None
            stt_emotion_state: dict[str, str | list[str]] = {"emotion": "NEUTRAL", "events": []}
            if stt_enabled:
                try:
                    from Module.stt import STTFactory
                    stt_backend = STTFactory.create(self._config.stt.model)

                    # 快速连通性测试——1s 超时，避免卡启动
                    import asyncio as _asyncio
                    result = await _asyncio.wait_for(stt_backend.test(), timeout=1.0)
                    if not result.get("ok"):
                        raise RuntimeError(result.get("error", "服务不可用"))

                    async def transcribe_wrapper(audio: bytes) -> str:
                        text = await stt_backend.transcribe(audio)
                        stt_emotion_state["emotion"] = getattr(stt_backend, "last_emotion", "NEUTRAL")
                        stt_emotion_state["events"] = list(getattr(stt_backend, "last_events", []))
                        return text

                    stt_transcribe = transcribe_wrapper
                    self._stt_ok = True
                    logger.info("STT backend connected for server mode")
                except Exception:
                    logger.info("STT not available — text-only mode", exc_info=False)

            app = create_app(cast(Any, self), stt_transcribe=stt_transcribe, serve_static=True,
                            stt_emotion_state=stt_emotion_state,
                            wake_word_enabled=wake_word_enabled,
                            task_system=self._task_system,
                            auth_token=self._config.server.auth_token)
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
            print(f"\n{'='*50}")
            print(f"  服务已启动 → http://{host}:{port}")
            print(f"  按 Ctrl+C 停止")
            print(f"{'='*50}\n", flush=True)

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
                    from core.config import AppConfig
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
