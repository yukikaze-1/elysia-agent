"""TTS 后端注册表 + 工厂。按 provider/model 标识创建后端实例。"""

from typing import TYPE_CHECKING, Any

from pathlib import Path

import yaml

from Module._common.registry_helpers import resolve_api_key, resolve_base_url, inject_logger
from Module.config.providers import load_provider_defaults
from Module.tts.gpt_sovits import GPTSoVitsBackend


def _load_voices(voices_config: str = "") -> dict:
    """加载音色配置，返回 voice_name → config 的 dict。

    Args:
        voices_config: 音色配置文件路径，为空则使用默认 gpt_sovits_voices.yaml。

    Returns:
        voice_name → config 的映射字典。
    """
    if voices_config:
        path = Path(voices_config)
    else:
        path = Path(__file__).parent / "gpt_sovits_voices.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


if TYPE_CHECKING:
    from Module.tts.config import TTSConfig
    from Module.tts.protocol import TTSBackend

# provider 默认值统一由 Module/config/providers.yaml 管理
_BACKENDS: dict[str, dict] = load_provider_defaults("tts")


class TTSFactory:
    """静态工厂 — TTSFactory.create("gpt-sovits/default")。"""

    @staticmethod
    def create(model_id: str, **kwargs: Any) -> "TTSBackend":
        """根据 model_id 创建 TTS 后端实例。

        Args:
            model_id: 格式为 "provider/model-name" 的模型标识。
            **kwargs: 透传给后端构造函数的额外参数（如 voice_id、speed、voice_map 等）。

        Returns:
            对应 provider 的 TTSBackend 实例。

        Raises:
            ValueError: 当 model_id 格式无效、provider 未知或缺少必要参数时。
        """
        if "/" not in model_id:
            raise ValueError(
                f"Invalid model_id '{model_id}': expected 'provider/model-name' format"
            )
        provider, backend_name = model_id.split("/", 1)

        if provider not in _BACKENDS:
            raise ValueError(
                f"Unknown provider '{provider}'. Known: {', '.join(sorted(_BACKENDS))}"
            )

        # MiniMax 限速参数：仅对 minimax 后端生效，提前取出避免透传给其他后端
        # （gpt-sovits / custom 构造函数不接受该 kwarg，传入会 TypeError）
        rate_limit_rpm = kwargs.pop("rate_limit_rpm", None)

        cfg = _BACKENDS[provider]
        explicit_base_url = kwargs.get("base_url")  # minimax 分支需要原始值
        base_url = resolve_base_url(cfg.get("url_key", ""), cfg, kwargs.pop("base_url", None))

        if provider == "custom" and not base_url:
            raise ValueError("custom TTS provider requires base_url")

        # gpt-sovits: 验证 model（"default" 或已知 voice name，大小写不敏感）
        if provider == "gpt-sovits" and backend_name != "default":
            voices_path = kwargs.get("voices_config", "")
            voices = _load_voices(voices_path)
            voices_lower = {k.lower(): k for k in voices}
            if backend_name.lower() not in voices_lower:
                raise ValueError(
                    f"未知的 TTS model: {backend_name!r}。"
                    f"支持的 model: default, {', '.join(sorted(voices))}"
                )

        # custom provider: 使用 OpenAI 兼容 TTS 协议
        if provider == "custom":
            from Module.tts.openai_tts import OpenAICompatTTSBackend
            api_key = resolve_api_key(cfg["env_key"], cfg, kwargs.pop("api_key", None))
            if "logger" not in kwargs:
                kwargs["logger"] = inject_logger(__name__)
            return OpenAICompatTTSBackend(model=backend_name, api_key=api_key, base_url=base_url, **kwargs)

        # minimax: MiniMax TTS 云端服务
        if provider == "minimax":
            from Module.tts.minimax_tts import MiniMaxTTSBackend
            api_key = resolve_api_key("MINIMAX_API_KEY", cfg, kwargs.pop("api_key", None))
            base_url = resolve_base_url(cfg.get("url_key", ""), cfg, explicit_base_url)
            if not base_url:
                raise ValueError(
                    "MiniMax TTS base_url 未设置。请在 .env 中设置 MINIMAX_TTS_URL，"
                    "如 MINIMAX_TTS_URL=https://api.minimaxi.com"
                )
            if not api_key:
                raise ValueError("MiniMax TTS 需要 api_key，请设置 MINIMAX_API_KEY 环境变量或显式传入")
            if "logger" not in kwargs:
                kwargs["logger"] = inject_logger(__name__)
            if rate_limit_rpm is not None:
                kwargs["rate_limit_rpm"] = rate_limit_rpm
            return MiniMaxTTSBackend(model=backend_name, api_key=api_key, base_url=base_url, **kwargs)

        # gpt-sovits: GPT-SoVits 专有协议
        if "logger" not in kwargs:
            kwargs["logger"] = inject_logger(__name__)

        return GPTSoVitsBackend(model=backend_name, base_url=base_url, **kwargs)

    @classmethod
    def from_config(cls, model_id: str, config: "TTSConfig", **overrides: Any) -> "TTSBackend":
        """从 TTSConfig dataclass 创建后端，overrides 优先级最高。"""
        kwargs: dict[str, Any] = {"base_url": config.base_url}
        if config.voices_config:
            kwargs["voices_config"] = config.voices_config
        kwargs["rate_limit_rpm"] = config.rate_limit_rpm
        kwargs.update(overrides)
        return cls.create(model_id, **kwargs)
