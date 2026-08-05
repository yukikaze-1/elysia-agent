"""Agent 生命周期 — LLM/TTS/Output/Token 服务启停。"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from core.config import AppConfig
from Module.output import OutputManager

if TYPE_CHECKING:
    from core.adapter.llm_adapter import LLMAdapter
    from core.cognitive import CognitiveLoop
    from core.memory.session import SessionMemory
    from Module.tts.protocol import TTSBackend
    from Module.stt.protocol import STTBackend
    from core.monitoring.token_tracker import TokenTracker
    from core.debug.prompt_recorder import PromptRecorder
    from core.debug.response_recorder import ResponseRecorder
    from core.storage.token_repo import TokenRepository

logger = logging.getLogger(__name__)


class _LifecycleServices:
    """LLM/TTS/Output 服务生命周期 mixin。"""

    if TYPE_CHECKING:
        _token_tracker: TokenTracker
        session: SessionMemory
        _output: OutputManager
        _token_repo: TokenRepository
        _mongo_ok: bool
        _prompt_recorder: PromptRecorder
        _response_recorder: ResponseRecorder
        _cognitive: CognitiveLoop
        _config: AppConfig
        _tts: TTSBackend | None
        _stt: STTBackend | None
        _llm: LLMAdapter
        _stt_enabled: bool | None
        _tts_enabled: bool | None
        switch_llm: Callable[[str], Coroutine[Any, Any, dict]]

    async def _start_token_and_prompt(self) -> None:
        """创建 TokenTracker + PromptRecorder，注入 CognitiveLoop。"""
        from core.monitoring.token_tracker import TokenTracker
        self._token_tracker = TokenTracker(
            session_id=self.session.session_id,
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

    async def _apply_service_state(self) -> None:
        """启动步骤：读 ServiceState → 设模式槽位 → LLM 应用。位于 tts 步骤前。

        三态语义：ServiceState.<svc>_enabled 非 None 用之（用户显式改过），否则回退
        config.<svc>.enabled。LLM：llm_model 非空且 ≠ config.llm.model → switch_llm。
        """
        ss = self.session.service_state
        self._stt_enabled = ss.stt_enabled if ss.stt_enabled is not None else self._config.stt.enabled
        self._tts_enabled = ss.tts_enabled if ss.tts_enabled is not None else self._config.tts.enabled
        if ss.llm_model and ss.llm_model != self._config.llm.model:
            await self.switch_llm(ss.llm_model)

    async def _start_tts(self) -> None:
        """创建 TTS 后端（条件执行——建门 = 有效 enabled 三态）。

        startup_tts_model = ServiceState.tts_model or config.tts.model；建门 = 有效
        enabled（显式值优先，None 回退 config.tts.enabled）。创建后测试连通性，
        不可用时自动降级为纯文本模式。
        """
        ss = self.session.service_state
        model = ss.tts_model or self._config.tts.model                    # startup_tts_model
        gate = ss.tts_enabled if ss.tts_enabled is not None else self._config.tts.enabled  # 有效 enabled
        if gate:
            from Module.tts import TTSFactory
            from core.input.custom_providers import resolve_model_params
            # 批次 5（D16）：desktop/local Profile 由 Electron 执行，后端启动不代理——
            # 跳过建 TTS（纯文本模式），否则空 base_url 的 custom 条目会让启动 fail-fast。
            tts_params = resolve_model_params("tts", model)
            tts_runtime = tts_params.pop("runtime", "backend")
            if tts_runtime != "backend":
                self._tts = None
                logger.warning("TTS Profile %s 由 Electron 执行，后端不启动（纯文本模式）", model)
            else:
                try:
                    # 关键：custom/<name> 必须经 resolve_model_params 补 base_url（registry.py
                    # 对 custom 无 base_url 抛 ValueError——裸 create 会让运行时切好的 custom TTS
                    # 重启后静默失效，与 STT 同款 bug）
                    # custom+backend → 出站守卫工厂注入（reconnect 重建全新守卫 client）
                    _base_url = tts_params.get("base_url", "")
                    if model.startswith("custom/") and _base_url:
                        from core.input.ssrf import build_guarded_http_client
                        tts_params["http_client_factory"] = (
                            lambda b=_base_url: build_guarded_http_client(b))
                    self._tts = TTSFactory.create(
                        model, **tts_params,
                        rate_limit_rpm=self._config.tts.rate_limit_rpm,
                    )
                except Exception:
                    # 配置错误（模型不存在/参数非法/custom 缺 base_url）——fail-fast，
                    # 不伪装成 text-only。原实现全吞让配置错误静默失效难排查。
                    logger.exception("TTS 后端创建失败——配置错误（模型=%s）", model)
                    raise
            if self._tts is not None:
                try:
                    # 快速连通性测试——1s 超时，避免卡启动（desktop/local 已跳过，不进此块）
                    import asyncio as _asyncio
                    result = await _asyncio.wait_for(self._tts.test(), timeout=1.0)
                    if result.get("ok"):
                        self._cognitive.configure(tts=self._tts)
                        await self._cognitive._tts_mgr.set_enabled(self._tts_enabled)  # noqa: SLF001
                        logger.info("TTS backend connected")
                    else:
                        self._tts = None
                        logger.info("TTS not available — text-only mode (%s)", result.get("error", "unknown"))
                except Exception:
                    # 连通性失败（服务不可达/测试超时）——降级 text-only
                    self._tts = None
                    logger.warning("TTS 连通性测试失败，text-only mode", exc_info=True)

    async def _stop_llm_and_tts(self) -> None:
        """关闭 LLM/TTS/STT 客户端连接，并置 None 以便热切换识别。"""
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
            self._tts = None
            # 同步 CognitiveLoop 中的 TTS 引用——shutdown 后残留流式任务不再触碰旧后端
            self._cognitive._tts_mgr.set_backend(None)

        if self._stt:
            logger.debug("Closing STT backend...")
            try:
                await asyncio.wait_for(self._stt.close(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("STT close timed out")
            self._stt = None

    async def _stop_output(self) -> None:
        """关闭所有输出通道。"""
        logger.debug("Closing output channels...")
        try:
            await asyncio.wait_for(self._output.close(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("Output close timed out")
