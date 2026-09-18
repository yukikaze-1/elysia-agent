"""STT 后端注册表 + 工厂。按 provider/model 标识创建后端实例。"""

import dataclasses
from typing import TYPE_CHECKING, Any

from Module._common.registry_helpers import resolve_api_key, resolve_base_url, inject_logger
from Module.config.providers import load_provider_defaults
from Module.stt.sensevoice import DEFAULT_BASE_URL, SenseVoiceBackend

if TYPE_CHECKING:
    from Module.stt.config import STTConfig
    from Module.stt.protocol import STTBackend

# provider 默认值统一由 Module/config/providers.yaml 管理
_BACKENDS: dict[str, dict] = load_provider_defaults("stt")

# 本仓**未实现**的 provider（与 `Module/llm/registry.py` 同形的 capability 权威）：
# providers.yaml 里声明了，但工厂建不出后端。`create()` 的守卫与 `is_implemented()`
# 查询读**同一个常量**，不可能漂移。
#
# 为什么不能各写一份（Batch 5 / N-10）：`Module/config/preflight.py` 此前自带
# `_STT_STUB_PROVIDERS = {"openai", "deepgram"}`，与本文件的内联元组是同一事实的两份
# 副本；两处漂移时预检就会报出一个运行期不成立的结论。判据必须只有一处。
#
# 语义边界同 LLM：描述**当前这份代码**的 provider 级实现情况，不描述厂商产品能力，
# 也不描述 model 级能力。不在集合中的 provider 一律按"已实现"对待（fail-open）。
_UNIMPLEMENTED_PROVIDERS: frozenset[str] = frozenset({"openai", "deepgram"})


class STTFactory:
    """静态工厂 — STTFactory.create("sensevoice/sense-voice")。"""

    @staticmethod
    def create(model_id: str, **kwargs: Any) -> "STTBackend":
        """根据 model_id 创建 STT 后端实例。

        Args:
            model_id: 格式为 "provider/model-name" 的模型标识。
            **kwargs: 透传给后端构造函数的额外参数。

        Returns:
            对应 provider 的 STTBackend 实例。

        Raises:
            ValueError: 当 model_id 格式无效或 provider 未知时。
        """
        if "/" not in model_id:
            raise ValueError(
                f"Invalid model_id '{model_id}': expected 'provider/model-name' format"
            )
        provider, model = model_id.split("/", 1)

        if provider not in _BACKENDS:
            raise ValueError(
                f"Unknown provider '{provider}'. Known: {', '.join(sorted(_BACKENDS))}"
            )

        if provider in _UNIMPLEMENTED_PROVIDERS:
            raise NotImplementedError(f"{provider} backend is not yet implemented")

        cfg = _BACKENDS[provider]
        base_url = resolve_base_url(cfg.get("url_key", ""), cfg, kwargs.pop("base_url", None))

        env_key = cfg["env_key"]
        api_key = resolve_api_key(env_key, cfg, kwargs.pop("api_key", None))

        # custom provider — 使用 OpenAI 兼容 STT 后端
        if provider == "custom":
            from Module.stt.openai_stt import OpenAICompatSTTBackend
            if not base_url:
                raise ValueError("custom STT provider requires base_url")
            if "logger" not in kwargs:
                kwargs["logger"] = inject_logger(__name__)
            return OpenAICompatSTTBackend(model=model, api_key=api_key, base_url=base_url, **kwargs)

        # D1=B（spec §4.1）：内置分支空值兜底——必须在 custom 分支之后（否则 custom 会拿到
        # 默认地址而非 raise）。openai/deepgram 已 NotImplementedError，此处仅剩 sensevoice。
        # 4 生产调用点（server.py:221 切换/422 启动、voice_sessions.py:457 听写快照、
        # provider_routes.py:303 Profile 测试）全部经此统一解析。
        if not base_url:
            base_url = DEFAULT_BASE_URL

        if "logger" not in kwargs:
            kwargs["logger"] = inject_logger(__name__)

        return SenseVoiceBackend(
            model=model,
            api_key=api_key,
            base_url=base_url,
            **kwargs,
        )

    @classmethod
    def from_config(cls, model_id: str, config: "STTConfig", **overrides: Any) -> "STTBackend":
        """从 STTConfig dataclass 创建后端，overrides 优先级最高。"""
        kwargs = dataclasses.asdict(config)
        # model/api_key 由 registry.create() 处理，不通过 config 传递
        kwargs.pop("model", None)
        kwargs.pop("api_key", None)
        kwargs.update(overrides)
        return cls.create(model_id, **kwargs)

    @classmethod
    def is_implemented(cls, model_id: str) -> bool:
        """检测该 provider 在**本仓**是否有实现（`create()` 能否建出后端）。

        报告的调用方（preflight、provider test、debug）应问这里，而不是自己维护一份
        "哪些 STT provider 未实现"的副本表。与 `Module/llm/registry.py::is_implemented`
        同形，判据与 `create()` 的守卫同源。

        Args:
            model_id: 格式为 "provider/model-name" 的标识字符串，或纯 provider 名。

        Returns:
            True 表示本仓实现了该 provider；False 表示 providers.yaml 声明了但未实现
            （openai / deepgram），或 provider 未声明。

        Raises:
            无——只查询，不建连、不发网络请求。
        """
        provider = model_id.split("/")[0] if "/" in model_id else model_id
        if provider not in _BACKENDS:
            return False
        return provider not in _UNIMPLEMENTED_PROVIDERS
