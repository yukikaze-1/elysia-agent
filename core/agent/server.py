"""Agent 服务器入口 — run_server/run_repl + provider 切换。"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, cast

from aiohttp import web
from core.agent._types import RunState
from core.services.capabilities import broadcast_capabilities
from core.utils.server_guard import (
    assert_secure_bind,
    is_loopback_host,
    warn_if_unsecured,
)
from core.schema.types import RequestId
from core.utils import set_request_id

if TYPE_CHECKING:
    from core.config import AppConfig, CharacterConfig
    from core.adapter.llm_adapter import LLMAdapter
    from core.memory.long_term import LongTermMemory
    from core.memory.session import SessionMemory
    from core.adapter.tool_adapter import ToolAdapter
    from core.cognitive import CognitiveLoop
    from Module.workflow_engine.workflow_engine import WorkflowEngine
    from Module.tts.protocol import TTSBackend
    from Module.stt.protocol import STTBackend
    from Module.output import OutputManager
    from core.storage.idempotency_repo import IdempotencyRepository

logger = logging.getLogger(__name__)

# runtime != backend（desktop/local 由 Electron 执行）→ switch 拦截错误码
_RUNTIME_NOT_BACKEND_MSG = "PROFILE_RUNTIME_NOT_BACKEND: 该 Profile 由 Electron 执行"


def _resolve_switch_params(
    svc: str, model_id: str,
) -> tuple[dict, str, Any]:
    """switch 参数守卫解析：resolve + 剥离 runtime + SSRF 校验 + 出站守卫工厂。

    custom/name 经 resolve_model_params 解析 base_url/api_key/runtime；**剥离 runtime**
    ——不透传给后端构造器（构造器无 runtime 形参，Task 4 后 resolve 返回 runtime 键，
    整包 **params 透传会 TypeError）。custom+backend 且 base_url 非空 → validate_endpoint
    SSRF 校验 + build_guarded_http_client 出站守卫**工厂**（review 修正：工厂而非实例
    ——AsyncOpenAI 关闭连带关闭守卫 client，实例无法跨 reconnect 复用；工厂每次返回
    全新守卫 client）。内置（resolve 返回 {}）→ runtime 缺省 backend、无校验、无工厂。

    Args:
        svc: 服务名（"llm"/"stt"/"tts"）。
        model_id: 模型标识，如 "custom/name" 或 "deepseek/deepseek-v4-flash"。

    Returns:
        (params, runtime, http_client_factory) 三元组：
        - params：剥离 runtime 后的构造参数（base_url/api_key 等）。
        - runtime：执行位置（"backend" 或 desktop/local）。
        - http_client_factory：custom+backend 且 base_url 非空的守卫工厂
          （零参 → 全新守卫 client）；其余为 None。

    Raises:
        SSRFError: custom+backend 且 base_url 非空但校验失败（内网/解析失败）。
    """
    from core.providers.custom_providers import resolve_model_params
    from Module._common.ssrf import build_guarded_http_client, validate_endpoint
    params = resolve_model_params(svc, model_id)
    runtime = params.pop("runtime", "backend")
    http_client_factory = None
    if runtime == "backend":
        base_url = params.get("base_url", "")
        if base_url:
            validate_endpoint(base_url)
            http_client_factory = lambda b=base_url: build_guarded_http_client(b)
    return params, runtime, http_client_factory


class AgentServer:
    """Server/REPL 生命周期 mixin——运行时与 Agent 组合，session 等属性来自 Agent。

    所有属性由 Agent.__init__ 初始化，本 mixin 不含 __init__。
    """

    # 类型占位：运行时由 Agent @property 提供（mixin 组合，本类无 __init__ 赋值）
    session: SessionMemory    # 类型占位：运行时来自 Agent @property（mixin 组合）
    output: "OutputManager"   # 占位：broadcast_capabilities/build_capabilities 用
    stt_enabled: bool
    tts_enabled: bool
    # build_capabilities（capabilities.py 参数放宽为 Agent | AgentServer）所需——
    # Agent 是 AgentServer 子类，union 坍缩为 AgentServer 后须声明这些公开属性
    config: AppConfig
    llm: LLMAdapter
    stt: STTBackend | None
    tts: TTSBackend | None

    if TYPE_CHECKING:
        _character: CharacterConfig
        _llm: LLMAdapter
        _long_term: LongTermMemory
        _tool_adapter: ToolAdapter
        _cognitive: CognitiveLoop
        _task_system: WorkflowEngine | None
        _state: RunState
        _config_path: str | None
        _config: AppConfig
        _tts: TTSBackend | None
        _stt: STTBackend | None
        _stt_ok: bool
        _req_counter: int
        _mongo_ok: bool
        _milvus_ok: bool
        idempotency_repo: "IdempotencyRepository"  # D6c：运行时来自 Agent @property
        start: Callable[..., Coroutine[Any, Any, None]]
        stop: Callable[..., Coroutine[Any, Any, None]]
        process_message: Callable[..., Coroutine[Any, Any, None]]

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

    def _stt_banner_status(self) -> str:
        """STT 启动横幅状态行（可测）——读 self._stt + model。

        STT 行在 run_server 内联 server_info 中无法直接测，故抽出独立方法。
        返回形如 ``已连接 (model=sensevoice/sense-voice)`` / ``未连接``。
        """
        if self._stt:
            return f"已连接 (model={self._stt.model})"
        return "未连接"

    def next_request_id(self) -> RequestId:
        """生成并返回请求 ID，同时设置 ContextVar。供外部模块（STT 等）使用。

        Returns:
            格式为 ``{req_counter:03d}`` 的请求 ID 字符串（RequestId NewType）。
        """
        self._req_counter += 1
        rid = f"{self._req_counter:03d}"
        set_request_id(rid)
        return RequestId(rid)

    async def switch_llm(self, model_id: str) -> dict:
        """热切换 LLM 模型。切换前打断当前 cognitive task 防止卡 thinking。

        custom/<name> 的 base_url/api_key 经 resolve_model_params 从 custom_providers.yaml 解析。
        成功后写入 ServiceState.llm_model（config 保持默认不改写）。

        Args:
            model_id: 新的 LLM 模型标识符（如 ``deepseek/deepseek-v4-flash``）。

        Returns:
            包含 ``ok`` 状态和可选的 ``error`` 信息的字典。
        """
        try:
            params, runtime, http_client_factory = _resolve_switch_params("llm", model_id)
            if runtime != "backend":
                # desktop/local Profile 由 Electron 执行，后端侧拒绝切换（service_state 不改）
                return {"ok": False, "error": _RUNTIME_NOT_BACKEND_MSG}
            self._cognitive.cancel_current()  # 打断正在进行的 LLM 调用
            if http_client_factory is not None:
                # custom+backend：出站守卫工厂注入（SSRF 校验在 _resolve_switch_params 内；
                # reconnect 时后端从工厂重建全新守卫 client）
                params["http_client_factory"] = http_client_factory
            self._llm.switch_model(model_id, **params)
            # 先建后换成功后写入运行时状态（config 保持默认不改写）
            self.session.service_state.llm_model = model_id
            try:
                await self.session.save()
            except Exception as e:
                # 持久化失败不回滚已生效的切换——运行中后端是权威，仅告警
                logger.warning("LLM switch 已生效但持久化失败: %s", e)
            logger.info("LLM switched to: %s", model_id)
            await broadcast_capabilities(self)
            return {"ok": True}
        except Exception as e:
            logger.error("LLM switch failed: %s", e)
            return {"ok": False, "error": str(e)}

    async def switch_stt(self, model_id: str) -> dict:
        """热切换 STT 后端。先建后关 + 1s 连通性测试——成功后才替换并关闭旧后端。

        custom/<name> 的 base_url/api_key 经 resolve_model_params 从 custom_providers.yaml 解析。
        成功后写入 ServiceState.stt_model 并置 _stt_ok（config 保持默认不改写）。

        Args:
            model_id: 新的 STT 模型标识符（如 ``sensevoice/sense-voice``）。

        Returns:
            包含 ``ok`` 状态和可选的 ``error`` 信息的字典，失败时保留旧后端。
        """
        new_backend = None
        try:
            from Module.stt import STTFactory
            params, runtime, http_client_factory = _resolve_switch_params("stt", model_id)
            if runtime != "backend":
                # desktop/local Profile 由 Electron 执行，后端侧拒绝切换（service_state 不改）
                return {"ok": False, "error": _RUNTIME_NOT_BACKEND_MSG}
            if http_client_factory is not None:
                # custom+backend：出站守卫工厂注入（SSRF 校验在 _resolve_switch_params 内；
                # reconnect 时后端从工厂重建全新守卫 client）
                params["http_client_factory"] = http_client_factory
            new_backend = STTFactory.create(model_id, **params)
            # 1s 连通性测试——建好后先验证可用性再替换
            result = await asyncio.wait_for(new_backend.test(), timeout=1.0)
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "服务不可用"))
            # 成功后才关闭旧后端并挂载新后端
            if self._stt:
                try:
                    await self._stt.close()
                except Exception:
                    pass
            self._stt = new_backend
            self._stt_ok = True
            # config 保持默认，写入运行时状态
            self.session.service_state.stt_model = model_id
            try:
                await self.session.save()
            except Exception as e:
                # 持久化失败不回滚已生效的切换——运行中后端是权威，仅告警
                logger.warning("STT switch 已生效但持久化失败: %s", e)
            logger.info("STT switched to: %s", model_id)
            await broadcast_capabilities(self)
            return {"ok": True}
        except Exception as e:
            # 失败（含 test 不 ok/超时）：关闭新建未挂载的后端防 HTTP 客户端泄漏
            if new_backend is not None and new_backend is not self._stt:
                try:
                    await new_backend.close()
                except Exception:
                    pass
            self._stt_ok = False
            logger.error("STT switch failed: %s", e)
            return {"ok": False, "error": str(e)}

    async def switch_tts(self, model_id: str) -> dict:
        """热切换 TTS 后端。更新 self._tts 和 self._cognitive._tts 引用。

        custom/<name> 的 base_url/api_key 经 resolve_model_params 从 custom_providers.yaml 解析。

        Args:
            model_id: 新的 TTS 模型标识符（如 ``minimax/speech-2.8-hd``）。

        Returns:
            包含 ``ok`` 状态的字典，失败时含 ``error`` 信息。
        """
        new_backend = None
        try:
            from Module.tts import TTSFactory
            params, runtime, http_client_factory = _resolve_switch_params("tts", model_id)
            if runtime != "backend":
                # desktop/local Profile 由 Electron 执行，后端侧拒绝切换（service_state 不改）
                return {"ok": False, "error": _RUNTIME_NOT_BACKEND_MSG}
            if http_client_factory is not None:
                # custom+backend：出站守卫工厂注入（SSRF 校验在 _resolve_switch_params 内；
                # reconnect 时后端从工厂重建全新守卫 client）
                params["http_client_factory"] = http_client_factory
            params["rate_limit_rpm"] = self._config.tts.rate_limit_rpm
            new_backend = TTSFactory.create(model_id, **params)
            # 1s 连通性测试——建好后先验证可用性再替换
            result = await asyncio.wait_for(new_backend.test(), timeout=1.0)
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "服务不可用"))
            # 先换引用（configure 同步 CognitiveLoop），成功后才关闭旧后端
            old_backend = self._tts
            self._tts = new_backend
            self._cognitive.configure(tts=self._tts)  # 同步 CognitiveLoop 中的引用
            if old_backend:
                try:
                    await old_backend.close()
                except Exception:
                    pass
            # config 保持默认，写入运行时状态
            self.session.service_state.tts_model = model_id
            try:
                await self.session.save()
            except Exception as e:
                # 持久化失败不回滚已生效的切换——运行中后端是权威，仅告警
                logger.warning("TTS switch 已生效但持久化失败: %s", e)
            logger.info("TTS switched to: %s", model_id)
            await broadcast_capabilities(self)
            return {"ok": True}
        except Exception as e:
            # 失败（含 test 不 ok/超时）：关闭新建未挂载的后端防 HTTP 客户端泄漏
            if new_backend is not None and new_backend is not self._tts:
                try:
                    await new_backend.close()
                except Exception:
                    pass
            logger.error("TTS switch failed: %s", e)
            return {"ok": False, "error": str(e)}

    async def set_service(self, service: str, enabled: bool) -> dict:
        """设置服务开关（stt/tts 热插拔"开/关"路径）。

        三分支：未知 service / 非 bool enabled / 正常开关。启用时若后端为 None
        先经 switch_stt/switch_tts 补建（失败不改运行时槽位、不持久化）。
        运行时槽位存原始 bool；持久化走统一规则 ``persist = enabled if
        enabled != cfg_enabled else None``——等于 config 默认值时写 None
        （三态回退：None → config.<svc>.enabled）。

        Args:
            service: 服务名（``stt`` / ``tts``）。
            enabled: 是否启用。

        Returns:
            包含 ``ok`` 状态、``enabled``（成功为请求值，补建失败为当前有效开关）
            和可选 ``error`` 信息的字典。
        """
        if service not in ("stt", "tts"):
            return {"ok": False, "error": "LLM 不可禁用"} if service == "llm" else {"ok": False, "error": f"未知 service: {service}"}
        if not isinstance(enabled, bool):
            return {"ok": False, "error": "enabled 必须为 bool"}
        cfg_enabled = self._config.stt.enabled if service == "stt" else self._config.tts.enabled
        persist = enabled if enabled != cfg_enabled else None  # 统一规则：== config 写 None
        if enabled:
            backend = self._stt if service == "stt" else self._tts
            if backend is None:  # 补建（热插拔"开"路径）
                model = (self.session.service_state.stt_model or self._config.stt.model) \
                    if service == "stt" else (self.session.service_state.tts_model or self._config.tts.model)
                if service == "stt":
                    r = await self.switch_stt(model)
                else:
                    r = await self.switch_tts(model)
                if not r["ok"]:
                    return {"ok": False, "enabled": self.stt_enabled if service == "stt" else self.tts_enabled, "error": r.get("error")}
        # 置运行时槽位（stt 或 tts）
        if service == "stt":
            self._stt_enabled = enabled
        else:
            self._tts_enabled = enabled
            # 内部接线（loop gate 唯一来源），非跨模块访问
            await self._cognitive._tts_mgr.set_enabled(enabled)  # noqa: SLF001
        # 持久化 enabled（补建成功后才写，失败已 return）
        setattr(self.session.service_state, f"{service}_enabled", persist)
        await self.session.save()
        # 重播 capabilities——向所有 WebSocket 客户端广播（switch_* 补建路径自身也会播，幂等无害）
        await broadcast_capabilities(self)
        return {"ok": True, "enabled": enabled}

    def get_wake_words(self) -> list[str]:
        """从角色配置中获取唤醒词，供服务端模式 AudioBuffer 使用。

        Returns:
            唤醒词字符串列表。
        """
        # _character 由 Agent.__init__ 恒加载（CharacterConfig），wake_words 字段恒存在
        return self._character.wake_words

    async def run_server(
        self,
        *,
        app_factory: Callable[..., web.Application] | None = None,
    ) -> None:
        """以 WebSocket 服务模式运行 agent。阻塞直至 Ctrl+C 或关闭。

        支持通过 /restart 端点在停止后重新加载配置并重启服务。

        Args:
            app_factory: aiohttp Application 工厂（通常为
                ``core.input.server.create_app``），由装配入口 app.py 注入。
                为 None 时拒绝启动——Agent 层不再反向依赖 core.input。
        """
        if app_factory is None:
            raise RuntimeError(
                "run_server 需要 app_factory（app.py 应注入 core.input.server.create_app）"
            )

        await self.start()

        while True:
            host = self._config.server.host
            port = self._config.server.port
            wake_word_enabled = self._config.wake_word.enabled

            # 启动安全守卫（批次 1 + D6a）：非回环绑定 + 无产品认证 → fail-closed 拒绝启动
            # （显式 ALLOW_INSECURE_REMOTE=1 才放行）；否则显著告警
            assert_secure_bind(host, self._config.server.access_token,
                               self._config.server.auth_token)
            warn_if_unsecured(host, self._config.server.access_token,
                              self._config.server.auth_token)

            # 建门 = 有效 enabled（显式值优先，None 回退 config.stt.enabled）——热插拔
            # "开"（config=false+显式 true）跨重启存活；config=false+None 保留 YAML 硬关
            ss = self.session.service_state
            stt_model = ss.stt_model or self._config.stt.model               # startup_stt_model
            stt_gate = ss.stt_enabled if ss.stt_enabled is not None else self._config.stt.enabled
            if stt_gate:
                try:
                    from Module.stt import STTFactory
                    from core.providers.custom_providers import resolve_model_params
                    # 关键：custom/<name> 必须经 resolve_model_params 补 base_url（registry
                    # 对 custom 无 base_url 抛 ValueError，裸 create 会让运行时切好的 custom
                    # STT 重启后 available=false）
                    stt_params = resolve_model_params("stt", stt_model)
                    # 批次 5（D16）：desktop/local Profile 由 Electron 执行，后端不代理
                    stt_runtime = stt_params.pop("runtime", "backend")
                    if stt_runtime != "backend":
                        self._stt = None
                        self._stt_ok = False
                        logger.info("STT Profile %s 由 Electron 执行，后端不启动", stt_model)
                        stt_backend = None
                    else:
                        # custom+backend → 出站守卫工厂注入
                        _base_url = stt_params.get("base_url", "")
                        if stt_model.startswith("custom/") and _base_url:
                            from Module._common.ssrf import build_guarded_http_client
                            stt_params["http_client_factory"] = (
                                lambda b=_base_url: build_guarded_http_client(b))
                        stt_backend = STTFactory.create(stt_model, **stt_params)
                        # 快速连通性测试——1s 超时，避免卡启动
                        result = await asyncio.wait_for(stt_backend.test(), timeout=1.0)
                        if not result.get("ok"):
                            raise RuntimeError(result.get("error", "服务不可用"))
                        # 显式赋槽位（只建一个）——create_app/handle_ws 从槽位读取（支持热切换）
                        self._stt = stt_backend
                        self._stt_ok = True
                        logger.info("STT backend connected for server mode")
                except Exception:
                    # 失败复位，防 stale
                    self._stt = None
                    self._stt_ok = False
                    logger.info("STT not available — text-only mode", exc_info=False)

            app = app_factory(cast(Any, self), serve_static=True,
                            serve_chat=self._config.server.serve_chat,
                            wake_word_enabled=wake_word_enabled,
                            task_system=self._task_system,
                            auth_token=self._config.server.auth_token,
                            access_token=self._config.server.access_token,
                            max_ws_connections=self._config.server.max_ws_connections,
                            max_request_bytes=self._config.server.max_request_bytes,
                            rate_limits=self._config.server.rate_limits,
                            product_mode=not is_loopback_host(host),
                            idempotency_repo=self.idempotency_repo)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host, port)
            await site.start()

            banner = self._print_startup_banner(
                getattr(self, '_mongo_ok', False),
                getattr(self, '_milvus_ok', False),
            )
            tts_model = self._config.tts.model
            serve_chat = self._config.server.serve_chat   # banner 局部——与 create_app 传参同源
            if serve_chat:
                frontend_line = f"前端: http://{host}:{port}/"   # 无前导空格——对齐由 `|  ` 提供
                server_info = "\n".join([
                    f"  ---",
                    f"  WebSocket: ws://{host}:{port}/ws  |  {frontend_line}",
                    f"  STT: {self._stt_banner_status()}",
                    f"  TTS: {'已连接' if self._tts else '未连接'}  (model={tts_model})",
                    f"  退出: Ctrl+C",
                    f"{'='*50}",
                ])
            else:
                # 聊天 UI 已停用：banner 补一行提示，避免 / 死链误导（SERVE_CHAT=1 可恢复）
                server_info = "\n".join([
                    f"  ---",
                    f"  WebSocket: ws://{host}:{port}/ws  |  "
                    f"聊天 UI 已停用（serve_chat=False），/debug 仪表盘可用；SERVE_CHAT=1 可恢复",
                    f"  STT: {self._stt_banner_status()}",
                    f"  TTS: {'已连接' if self._tts else '未连接'}  (model={tts_model})",
                    f"  退出: Ctrl+C",   # 关闭服务按钮在 v1 聊天页（/ 404），不提示
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
        """运行交互式 REPL 循环（stdin）。阻塞直至 /quit 或 Ctrl+D。"""
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
