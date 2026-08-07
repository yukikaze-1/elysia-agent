"""STT 后端注册表 + 工厂。按 provider/model 标识创建后端实例。"""

import dataclasses
from typing import TYPE_CHECKING, Any

from Module._common.registry_helpers import resolve_api_key, resolve_base_url, inject_logger
from Module.config.providers import load_provider_defaults
from Module.stt.sensevoice import SenseVoiceBackend

if TYPE_CHECKING:
    from Module.stt.config import STTConfig
    from Module.stt.protocol import STTBackend

# provider 默认值统一由 Module/config/providers.yaml 管理
_BACKENDS: dict[str, dict] = load_provider_defaults("stt")


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

        if provider in ("openai", "deepgram"):
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
