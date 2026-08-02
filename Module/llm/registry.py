"""LLM 后端注册表 + 工厂。按 provider/model 标识创建后端实例。"""

import dataclasses
from typing import TYPE_CHECKING

from Module._common.registry_helpers import resolve_api_key, resolve_base_url, inject_logger
from Module.config.providers import load_provider_defaults
from Module.llm.openai_compat import OpenAICompatBackend

if TYPE_CHECKING:
    from Module.llm.config import LLMConfig
    from Module.llm.protocol import LLMBackend

# provider 默认值统一由 Module/config/providers.yaml 管理
_BACKENDS: dict[str, dict] = load_provider_defaults("llm")


class LLMFactory:
    """LLM 后端工厂 — LLMFactory.create("deepseek/deepseek-v4-pro") 返回配置好的 backend。"""

    @staticmethod
    def create(model_id: str, **kwargs) -> "LLMBackend":
        """创建 LLM 后端实例。

        Args:
            model_id: 格式为 "provider/model-name" 的标识字符串，
                如 "deepseek/deepseek-v4-pro"。
            **kwargs: 传递给后端构造函数的附加参数（如 logger）。

        Returns:
            对应 provider 的 LLMBackend 实例。

        Raises:
            ValueError: model_id 格式不合法、provider 未知，
                或 custom provider 缺少 base_url 时抛出。
            NotImplementedError: provider 尚未实现时抛出。
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

        if provider == "gemini":
            raise NotImplementedError("Gemini backend is not yet implemented")

        cfg = _BACKENDS[provider]
        base_url = resolve_base_url(cfg.get("url_key", ""), cfg, kwargs.pop("base_url", None))
        beta_url = resolve_base_url(cfg.get("beta_url_key", ""), cfg, kwargs.pop("beta_url", None), config_key="beta_url")

        env_key = cfg["env_key"]
        api_key = resolve_api_key(env_key, cfg, kwargs.pop("api_key", None))

        # custom provider 必须提供 base_url
        if provider == "custom" and not base_url:
            raise ValueError("custom provider requires base_url")

        if "logger" not in kwargs:
            kwargs["logger"] = inject_logger(__name__)

        return OpenAICompatBackend(
            model=model,
            api_key=api_key,
            base_url=base_url,
            beta_url=beta_url,
            supports_prefix=cfg["supports_prefix"],
            **kwargs,
        )

    @classmethod
    def from_config(cls, model_id: str, config: "LLMConfig", **overrides) -> "LLMBackend":
        """从 LLMConfig dataclass 创建后端。

        Args:
            model_id: 格式为 "provider/model-name" 的标识字符串。
            config: LLMConfig dataclass 实例。
            **overrides: 覆盖 config 中字段的附加参数，优先级最高。

        Returns:
            对应 provider 的 LLMBackend 实例。
        """
        kwargs = dataclasses.asdict(config)
        # model/api_key 由 registry.create() 处理，不通过 config 传递
        kwargs.pop("model", None)
        kwargs.pop("api_key", None)
        kwargs.update(overrides)
        return cls.create(model_id, **kwargs)

    @classmethod
    def supports_multimodal(cls, model_id: str) -> bool:
        """检测 provider 是否支持多模态（图片输入）。

        Args:
            model_id: 格式为 "provider/model-name" 的标识字符串，
                或纯 provider 名。

        Returns:
            True 表示该 provider 支持图片等多模态输入。
        """
        provider = model_id.split("/")[0] if "/" in model_id else model_id
        return bool(_BACKENDS.get(provider, {}).get("supports_multimodal", False))
