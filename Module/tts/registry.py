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

# 本仓**未实现**的 provider（与 llm/stt registry 同形的 capability 权威）。
# 当前为空：三个 TTS provider（gpt-sovits / minimax / custom）都有实现，
# 这也是本文件此前**没有** `NotImplementedError` 守卫的原因。
#
# 仍然显式声明这个集合，是为了让"哪些 provider 实现了"在三个子系统里只有**同一个形状**
# 的权威（`is_implemented()` + `create()` 守卫读同一常量）；将来若要加占位 provider
# （如某厂商 API 已声明但后端未写），在这里登记即可，不必再去改报告层。
_UNIMPLEMENTED_PROVIDERS: frozenset[str] = frozenset()


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

        # capability 守卫：与本文件 `_UNIMPLEMENTED_PROVIDERS` 同源。当前集合为空，
        # 故永不触发——它的作用是让"实例化 ⇒ 实现存在"这条不变量在三个 provider
        # 子系统里由同一个形状保证（新增占位 provider 时只登记一处）。
        if provider in _UNIMPLEMENTED_PROVIDERS:
            raise NotImplementedError(f"{provider} backend is not yet implemented")

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

    @classmethod
    def is_implemented(cls, model_id: str) -> bool:
        """检测该 provider 在**本仓**是否有实现（`create()` 能否建出后端）。

        与 `Module/llm/registry.py` / `Module/stt/registry.py` 的同名方法同形，
        让 capability 判据在三个子系统里只有一种问法（Batch 5 / N-10 的 Authority Map）。

        注意：本方法**只回答"有没有实现"**，不回答"配置齐不齐"——缺 API key、
        缺 base_url、gpt-sovits 的 model 段不在音色表里，都是 `create()` 的
        `ValueError`，属配置/解析层，不属 capability 层。

        Args:
            model_id: 格式为 "provider/model-name" 的标识字符串，或纯 provider 名。

        Returns:
            True 表示本仓实现了该 provider；False 表示未声明（运行期以 ValueError 拒绝）
            或已声明但未实现（当前为空集）。

        Raises:
            无——只查询，不建连、不发网络请求。
        """
        provider = model_id.split("/")[0] if "/" in model_id else model_id
        if provider not in _BACKENDS:
            return False
        return provider not in _UNIMPLEMENTED_PROVIDERS
