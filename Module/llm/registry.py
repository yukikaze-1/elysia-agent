"""LLM 后端注册表 + 工厂。按 provider/model 标识创建后端实例。"""

import dataclasses
from typing import TYPE_CHECKING, Any

from Module._common.registry_helpers import resolve_api_key, resolve_base_url, inject_logger
from Module.config.providers import load_provider_defaults
from Module.llm.ollama import OllamaBackend
from Module.llm.openai_compat import OpenAICompatBackend

if TYPE_CHECKING:
    from Module.llm.config import LLMConfig
    from Module.llm.protocol import LLMBackend

# provider 默认值统一由 Module/config/providers.yaml 管理
_BACKENDS: dict[str, dict] = load_provider_defaults("llm")

# 本仓**未实现**的 provider：providers.yaml 里声明了（会进预检、会进下拉候选），
# 但工厂建不出后端。这是「哪些 provider 实现了」的**唯一权威**——`create()` 的建连守卫
# 与 `is_implemented()` 查询读的是同一个常量，两者不可能漂移。
#
# 为什么需要它（Batch 5 / N-10）：`gemini/<model>` 此前预检 `[PASS]`、运行期
# `LLMFactory.create` 必然 `NotImplementedError`（E2 实测：预检 13/13 passed 之后
# `LLMAdapter("gemini/…")` 在**构造期**就抛）。预检当时没有"未实现"这个概念，
# 只能看 env 齐不齐；而"env 齐"与"本仓实现了"是两件事（PCT-2 / PCT-6）。
#
# 语义边界：本集合描述**当前这份代码**（provider 级），不描述厂商产品能力，也不描述
# model 级能力（后者见 `supports_multimodal` 与 providers.yaml 的 `supports_*` 字段）。
# 声明在 providers.yaml 里却不在本集合中的 provider 一律按"已实现"对待——
# 缺 metadata ≠ 未实现（fail-open，不误拒第三方/插件 provider）。
_UNIMPLEMENTED_PROVIDERS: frozenset[str] = frozenset({"gemini"})


class LLMFactory:
    """LLM 后端工厂 — LLMFactory.create("deepseek/deepseek-v4-pro") 返回配置好的 backend。"""

    @staticmethod
    def create(model_id: str, **kwargs: Any) -> "LLMBackend":
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

        if provider in _UNIMPLEMENTED_PROVIDERS:
            raise NotImplementedError(f"{provider} backend is not yet implemented")

        cfg = _BACKENDS[provider]
        base_url = resolve_base_url(cfg.get("url_key", ""), cfg, kwargs.pop("base_url", None))
        beta_url = resolve_base_url(cfg.get("beta_url_key", ""), cfg, kwargs.pop("beta_url", None), config_key="beta_url")

        env_key = cfg["env_key"]
        api_key = resolve_api_key(env_key, cfg, kwargs.pop("api_key", None))

        # custom provider 必须提供 base_url
        if provider == "custom" and not base_url:
            raise ValueError("custom provider requires base_url")
        # S2-H7：内置 provider 若**没有**默认地址（云端端点须由运营商提供，如 mimo），
        # 必须在**建连时**说清楚——否则空串会被交到后端，直到首次请求才以一个
        # 莫名其妙的 URL 错误暴露（实测 AsyncOpenAI(base_url="") 的 base_url 就是空串，
        # SDK 只对 None 回退）。MiniMax TTS 早就是这个做法（Module/tts/registry.py:101）。
        if not base_url:
            raise ValueError(
                f"{provider} 的 base_url 未设置。请在 .env 中设置 "
                f"{cfg.get('url_key', '')}，或把默认值写进 providers.yaml")

        # 批次 8a（T12）：custom + base_url 非空 → 初始配置 SSRF 守卫。
        # 复用保存/切换/测试三入口同一校验（Module._common.ssrf.validate_endpoint），
        # 抛 SSRFError 传播给调用方；内置 provider（openai/ollama 等）跳过。
        if provider == "custom" and base_url:
            from Module._common.ssrf import validate_endpoint

            validate_endpoint(base_url)

        if "logger" not in kwargs:
            kwargs["logger"] = inject_logger(__name__)

        # AR-16 / KB-13：**按 canonical provider identity 选后端**（provider 名就是
        # `model_id` 里 `/` 之前那一段，不是 baseURL 含 11434、也不是模型名前缀等启发式）。
        # Ollama 的 runtime semantics（native `/api/chat` + `options.num_ctx`）与通用
        # OpenAI 兼容协议**不可通约**：实测 0.24.0 的 `/v1/chat/completions` 会静默忽略
        # `options.num_ctx`，所以"在通用后端里加一个 if"表达不出 runtime context。
        # 选择点**只此一处**——上层（PromptAssembler / CognitiveLoop / preview / debug）
        # 一律不感知 provider 差异。
        if provider == "ollama":
            return OllamaBackend(
                model=model,
                api_key=api_key,
                base_url=base_url,
                # 显式声明的 effective context window（本 dict 就是 `context_window()`
                # 读的**同一个**来源 ⇒ 预算与运行时分配不可能分叉）。未声明 ⇒ None
                # ⇒ backend 不申请分配（OL2：通用预算回退不是分配指令）。
                context_window=cfg.get("context_window"),
                **kwargs,
            )

        return OpenAICompatBackend(
            model=model,
            api_key=api_key,
            base_url=base_url,
            beta_url=beta_url,
            supports_prefix=cfg["supports_prefix"],
            # S2-H3：能力位缺失 = 不声明 = 后端丢弃 DeepSeek 专属 thinking 键。
            # 用 .get 而非 [] ——本键是**新增**的，第三方 providers.yaml 副本里没有它。
            supports_thinking=cfg.get("supports_thinking", False),
            **kwargs,
        )

    @classmethod
    def from_config(cls, model_id: str, config: "LLMConfig", **overrides: Any) -> "LLMBackend":
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
    def context_window(cls, model_id: str) -> int | None:
        """检测 provider 的上下文窗口（token），未声明返回 None。

        S2-H9 的预算来源：`core.prompt.budget.budget_from_context_window()` 用它
        乘余量系数得到 prompt 预算。**未声明 ≠ 无上限**——调用方对 None 使用保守
        默认（32K），否则"没配 = 不截断"会退化成这次的原始缺陷。

        Args:
            model_id: 格式为 "provider/model-name" 的标识字符串，或纯 provider 名。

        Returns:
            上下文窗口 token 数；providers.yaml 未声明时 None。
        """
        provider = model_id.split("/")[0] if "/" in model_id else model_id
        window = _BACKENDS.get(provider, {}).get("context_window")
        return int(window) if window else None

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

    @classmethod
    def is_implemented(cls, model_id: str) -> bool:
        """检测该 provider 在**本仓**是否有实现（`create()` 能否建出后端）。

        这是"配置存在 / provider 已解析"与"实现支持"之间的那道判据：报告的调用方
        （preflight、provider test、debug）应当问这里，而不是自己维护一份
        "哪些 provider 未实现"的副本表——同一事实的第二份副本必然漂移（Batch 5 / N-10）。

        与 `supports_multimodal` / `context_window` 同形：接受 `"provider/model"`
        或纯 provider 名。**provider 级**判断（本仓的守卫就是 provider 级的）；不声称
        model 级能力。

        Args:
            model_id: 格式为 "provider/model-name" 的标识字符串，或纯 provider 名。

        Returns:
            True 表示本仓实现了该 provider；False 表示
            （a）`providers.yaml` 声明了但本仓未实现（如 gemini），或
            （b）provider 未声明（运行期 `create()` 会以 ValueError 拒绝）。

        Raises:
            无——本方法只查询，不建连、不发网络请求。
        """
        provider = model_id.split("/")[0] if "/" in model_id else model_id
        if provider not in _BACKENDS:
            return False
        return provider not in _UNIMPLEMENTED_PROVIDERS
