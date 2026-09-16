"""Agent 生命周期 — LLM/TTS/Output/Token 服务启停。"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from core.config import AppConfig
from core.schema.service_state import ServiceRuntimeState
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
        _prefer_config_model: bool
        _tts: TTSBackend | None
        _stt: STTBackend | None
        _llm: LLMAdapter
        switch_llm: Callable[[str], Coroutine[Any, Any, dict]]

        # #43：有效开关是**派生 property**（权威 = 会话 service_state + config 回退），
        # 定义在 `Agent` 上。mixin 里必须声明为 read-only property 而不是可变属性——
        # 声明成属性会让 pyright 认为子类用可变量覆盖了 property。
        @property
        def stt_enabled(self) -> bool: ...

        @property
        def tts_enabled(self) -> bool: ...

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

    def _effective_service_enabled(self, service: str) -> bool:
        """服务开关有效值的**唯一推导点**（#43/S2-C3）。

        三个"来源"就此收敛成一个函数：会话三态字段（权威，用户显式设置过才非 None）
        → 否则 config 默认值。`agent.stt_enabled` / `agent.tts_enabled` 都走这里，
        capabilities 广播、TTS 门控、STT 路由因此不可能各读各的。

        Args:
            service: ``"stt"`` 或 ``"tts"``。

        Returns:
            有效开关（bool）。
        """
        ss = self.session.service_state
        if service == "stt":
            explicit, default = ss.stt_enabled, self._config.stt.enabled
        else:
            explicit, default = ss.tts_enabled, self._config.tts.enabled
        return explicit if explicit is not None else default

    def service_runtime_state(self) -> ServiceRuntimeState:
        """服务**运行期**状态快照（F2，Batch 1.6）——"自述"类消费者的唯一来源。

        为什么是**方法**而不是启动期写进 `ToolContext.services` 的一份值：工具的 ctx
        是启动第 2 步（`tool_context`）建的，而真正决定这些值的步骤排在它后面
        （`service_state` 第 5、`tts` 第 6），且运行期还会被
        `switch_llm`/`switch_stt`/`switch_tts`/`set_service` 改写。任何"启动期快照"
        都必然过期——注入 bound method、每次现取才跨得过那几个步骤。

        为什么**不**读 `session.service_state` 的模型字段：那是**持久化权威**，与运行期
        槽位有意的不同步（激活一个从未切过模型的会话时 `ss.llm_model` 为空，而适配器
        仍在跑上一个模型；会话切换不重放模型热切换是 K4 的有意取舍）。自述必须报
        **此刻真的在用**的那个。

        开关两字段**复用派生属性** `self.stt_enabled` / `self.tts_enabled`（唯一推导点
        `_effective_service_enabled`）——在这里再写一次三态回退就是第 5 个漂移点，
        也会撞上 `core/agent/tests/test_state_single_authority.py` 的结构性守卫。

        Returns:
            `ServiceRuntimeState`（模型 None ⇔ 该服务后端未连接，与 capabilities 同判据）。
        """
        return ServiceRuntimeState(
            llm_model=self._llm.model,
            stt_model=self._stt.model if self._stt is not None else None,
            stt_enabled=self.stt_enabled,
            tts_model=self._tts.model if self._tts is not None else None,
            tts_enabled=self.tts_enabled,
        )

    async def _sync_tts_gate(self) -> None:
        """把 TTS 门控同步到 `TTSStreamManager` 的**唯一写点**（#43）。

        第四个"来源"是 TTS 管理器的内部标志（loop 的实际门控）。它不是权威，是
        **派生投影**——但此前三个调用点各写一次（`set_service` / `_apply_service_enabled`
        / `_start_tts`），任何一处漏写都会造成"开关显示开着但不会说话"。现在所有
        改动路径都只调本方法。
        """
        if self._cognitive is not None:
            # 内部接线（loop gate 唯一来源），非跨模块私有访问
            await self._cognitive._tts_mgr.set_enabled(self.tts_enabled)  # noqa: SLF001

    async def _apply_service_enabled(self) -> None:
        """把**当前会话**的三态开关同步到运行时槽位（S2-C3）。

        #43 之后"应用"只剩下**同步派生投影**：有效值的推导本身在
        `_effective_service_enabled`（属性每次现算），不再有 Agent 内存槽位需要刷新，
        也就不存在"切会话后槽位还是上一个会话的值"这类漂移。本方法只负责把派生值
        写给 TTS 管理器。

        **为什么仍然必须能在会话切换时调用**：`ServiceState` 是 `SessionMemory` 的字段
        （每会话持久化）。`agent.stt_enabled` 现在自动跟随新会话，但 TTS 管理器的内部
        标志是缓存，切会话后必须重算一次。

        **不含模型切换**：`llm_model` / `stt_model` / `tts_model` 的切换要建/拆后端
        （网络 client），在会话激活路径上做会带来明显的延迟与失败面；它们在启动时由
        `_apply_service_state` 应用，运行期由显式 `switch_llm/switch_stt/switch_tts`
        应用。这是**有意的取舍**，不是遗漏——写在这里以免下次审计重新发现。
        """
        await self._sync_tts_gate()

    async def _apply_service_state(self) -> None:
        """启动步骤：读 ServiceState → 设模式槽位 → LLM 应用。位于 tts 步骤前。

        三态开关部分见 `_apply_service_enabled`（也用于会话切换）。
        LLM：llm_model 非空且 ≠ config.llm.model → switch_llm。
        """
        await self._apply_service_enabled()
        ss = self.session.service_state
        if not ss.llm_model or ss.llm_model == self._config.llm.model:
            return
        if self._prefer_config_model:
            # S2-A5 第二半：本次是"重启 + YAML 里的模型变了"→ **文件优先**。直接切到
            # config 的模型（`switch_llm` 会把它写回 service_state，下次启动不再冲突），
            # 而不是把持久化的旧选择再套回来。
            logger.info(
                "重启后以配置文件为准：llm_model %s → %s（覆盖持久化的运行期选择）",
                ss.llm_model, self._config.llm.model)
            await self.switch_llm(self._config.llm.model)
            return
        await self.switch_llm(ss.llm_model)

    async def _start_tts(self) -> None:
        """创建 TTS 后端（条件执行——建门 = 有效 enabled 三态）。

        startup_tts_model = ServiceState.tts_model or config.tts.model；建门 =
        `self.tts_enabled`（**#43：与 capabilities 广播、门控同步同一个派生点**，
        不再在这里重算一遍三态回退——同一语义的第二处推导就是下一个漂移点）。
        创建后测试连通性，不可用时自动降级为纯文本模式。
        """
        ss = self.session.service_state
        model = ss.tts_model or self._config.tts.model                    # startup_tts_model
        if self.tts_enabled:
            from Module.tts import TTSFactory
            from core.providers.custom_providers import resolve_model_params
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
                        from Module._common.ssrf import build_guarded_http_client
                        tts_params["http_client_factory"] = (
                            lambda b=_base_url: build_guarded_http_client(b))
                    self._tts = TTSFactory.create(
                        model, **tts_params,
                        rate_limit_rpm=self._config.tts.rate_limit_rpm,
                    )
                except ValueError as e:
                    # 缺 API key（如 minimax 无 MINIMAX_API_KEY）→ 配置缺欠而非错误，
                    # 自动降级纯文本（warn 不崩，N5095 部署实录 issue #11）。其余
                    # ValueError（未知 provider/缺 base_url 等）仍是配置错误 → fail-fast。
                    if "api_key" in str(e) or "API key" in str(e):
                        self._tts = None
                        logger.warning("TTS 缺 API key，自动降级为纯文本模式（%s）", e)
                    else:
                        logger.exception("TTS 后端创建失败——配置错误（模型=%s）", model)
                        raise
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
                        await self._sync_tts_gate()
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
            self._cognitive.tts_manager.set_backend(None)

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
