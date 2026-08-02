"""Agent 生命周期 — LLM/TTS/Output/Token 服务启停。"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ..config import AppConfig
from Module.output import OutputManager

if TYPE_CHECKING:
    from ..adapter.llm_adapter import LLMAdapter
    from ..cognitive import CognitiveLoop
    from ..memory.session import SessionMemory
    from Module.tts.protocol import TTSBackend
    from core.monitoring.token_tracker import TokenTracker
    from core.debug.prompt_recorder import PromptRecorder
    from core.debug.response_recorder import ResponseRecorder
    from core.storage.token_repo import TokenRepository

logger = logging.getLogger(__name__)


class _LifecycleServices:
    """LLM/TTS/Output 服务生命周期 mixin。"""

    if TYPE_CHECKING:
        _token_tracker: TokenTracker
        _session: SessionMemory
        _output: OutputManager
        _token_repo: TokenRepository
        _mongo_ok: bool
        _prompt_recorder: PromptRecorder
        _response_recorder: ResponseRecorder
        _cognitive: CognitiveLoop
        _config: AppConfig
        _tts: TTSBackend | None
        _llm: LLMAdapter

    async def _start_token_and_prompt(self) -> None:
        """创建 TokenTracker + PromptRecorder，注入 CognitiveLoop。"""
        from core.monitoring.token_tracker import TokenTracker
        self._token_tracker = TokenTracker(
            session_id=self._session.session_id,
            output_manager=self._output,
            repo=self._token_repo if self._mongo_ok else None,
        )
        if self._mongo_ok:
            await self._token_tracker.flush_pending()

        from core.debug.prompt_recorder import PromptRecorder
        self._prompt_recorder = PromptRecorder(max_records=20)
        from core.debug.response_recorder import ResponseRecorder
        self._response_recorder = ResponseRecorder(max_records=20)

        self._cognitive.configure(
            token_tracker=self._token_tracker,
            prompt_recorder=self._prompt_recorder,
            response_recorder=self._response_recorder,
        )

    async def _start_tts(self) -> None:
        """创建 TTS 后端（条件执行——由 config.tts.enabled 控制）。

        创建后测试连通性，不可用时自动降级为纯文本模式。
        """
        if self._config.tts.enabled:
            from Module.tts import TTSFactory
            try:
                self._tts = TTSFactory.create(
                    self._config.tts.model,
                    rate_limit_rpm=self._config.tts.rate_limit_rpm,
                )
                # 快速连通性测试——1s 超时，避免卡启动
                import asyncio as _asyncio
                result = await _asyncio.wait_for(self._tts.test(), timeout=1.0)
                if result.get("ok"):
                    self._cognitive.configure(tts=self._tts)
                    logger.info("TTS backend connected")
                else:
                    self._tts = None
                    logger.info("TTS not available — text-only mode (%s)", result.get("error", "unknown"))
            except Exception:
                self._tts = None
                logger.info("TTS not available — text-only mode")

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
