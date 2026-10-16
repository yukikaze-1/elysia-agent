"""Embedding 后端注册表 + 工厂。按 provider/model 标识创建后端实例。"""

import dataclasses
import json
import logging
from typing import TYPE_CHECKING, Any

from Module._common.registry_helpers import resolve_api_key, resolve_base_url, inject_logger
from Module.config.providers import load_provider_defaults
from Module.embedding.openai_compat import OpenAICompatEmbeddingBackend

if TYPE_CHECKING:
    from Module.embedding.config import EmbeddingConfig
    from Module.embedding.protocol import EmbeddingBackend

logger = logging.getLogger(__name__)

# provider 默认值统一由 Module/config/providers.yaml 管理
_BACKENDS: dict[str, dict] = load_provider_defaults("embedding")


class EmbeddingFactory:
    """Embedding 后端工厂 — EmbeddingFactory.create("huggingface/BAAI/bge-large-zh-v1.5")。

    自动缓存实例，相同 model_id + kwargs 组合返回同一实例。
    """

    _instances: dict[str, "EmbeddingBackend"] = {}

    @classmethod
    def create(cls, model_id: str, **kwargs: Any) -> "EmbeddingBackend":
        """创建或返回缓存的 embedding 后端实例。

        Args:
            model_id: 格式为 "provider/model-name" 的标识字符串，
                如 "huggingface/BAAI/bge-large-zh-v1.5"。
            **kwargs: 传递给后端构造函数的附加参数。

        Returns:
            对应 provider 的 EmbeddingBackend 实例。

        Raises:
            ValueError: model_id 格式不合法或 provider 未知时抛出。
            NotImplementedError: provider 尚未实现时抛出。
        """
        # 缓存 key 包含 kwargs，避免不同参数返回同一实例
        cache_key = model_id + "|" + json.dumps(kwargs, sort_keys=True, default=str)
        if cache_key in cls._instances:
            cached = cls._instances[cache_key]
            if not getattr(cached, "_closed", False):
                return cached
            # 实例已被外部 close()，从缓存中移除并重新创建
            del cls._instances[cache_key]
        if "/" not in model_id:
            raise ValueError(
                f"Invalid model_id '{model_id}': expected 'provider/model-name' format"
            )
        provider, model = model_id.split("/", 1)

        if provider not in _BACKENDS:
            raise ValueError(
                f"Unknown provider '{provider}'. Known: {', '.join(sorted(_BACKENDS))}"
            )

        cfg = _BACKENDS[provider]

        env_key = cfg["env_key"]
        api_key = resolve_api_key(env_key, cfg, kwargs.pop("api_key", None))
        base_url = resolve_base_url(cfg.get("url_key", ""), cfg, kwargs.pop("base_url", None))

        if provider == "huggingface":
            if "logger" not in kwargs:
                kwargs["logger"] = inject_logger(__name__)
            # **函数内懒导入**（2026-09-21 测试性能专项）：本模块此前在顶部
            # `from Module.embedding.huggingface import HuggingFaceBackend`，而
            # `huggingface.py` 顶层 `import torch` + `from langchain_huggingface import
            # HuggingFaceEmbeddings`。后果是**任何**触到本模块的导入都会拖进
            # torch + transformers ——实测（2026-09-21，冷解释器）：
            #     Module.embedding                     4.206s
            #     Module.embedding.registry            4.216s
            #     Module.embedding.huggingface         4.095s
            # 而 `Module/config/preflight.py:426` 早已用同形状的懒导入拿 `find_local_model`，
            # 本处只是漏了。传导链：`core.input.__init__` → `server` → `core.agent` →
            # `_lifecycle_persistence` → `Module.embedding` ——于是**每个 pytest worker
            # 进程**、以及每条"起新解释器"的测试（本仓有 37 处 subprocess，其中多条正是
            # 验 import 期语义的，无法用 mock 替代）都要白付这 4 秒。
            #
            # 语义不变：走到这个分支就一定会用到 HuggingFaceBackend，导入时机从
            # "import registry 时" 变成 "首次 create(huggingface/…) 时"。
            # 唯一可观测差异：provider 拼错时的报错时机（`Unknown provider` 来自下面的
            # 白名单校验，本来就在导入之后，不受影响）。
            from Module.embedding.huggingface import HuggingFaceBackend

            instance = HuggingFaceBackend(model_name=model, **kwargs)
        elif provider == "openai":
            if "logger" not in kwargs:
                kwargs["logger"] = inject_logger(__name__)
            instance = OpenAICompatEmbeddingBackend(model=model, api_key=api_key, base_url=base_url, **kwargs)
        else:
            raise NotImplementedError(f"{provider} backend is not yet implemented")

        cls._instances[cache_key] = instance
        return instance

    @classmethod
    async def shutdown(cls) -> None:
        """关闭所有缓存的 embedding 实例，释放显存/内存。

        幂等操作：可多次调用，已关闭的实例会被跳过。
        关闭后清空实例缓存，后续 create() 将重新创建。
        """
        for name, instance in list(cls._instances.items()):
            try:
                await instance.close()
            except Exception:
                # close 失败 = 显存/连接泄漏隐患，记录 warning 而非静默
                logger.warning("Embedding 实例 close 失败（可能泄漏）: %s", name,
                               exc_info=True)
        cls._instances.clear()

    @classmethod
    def from_config(cls, model_id: str, config: "EmbeddingConfig", **overrides: Any) -> "EmbeddingBackend":
        """从 EmbeddingConfig dataclass 创建后端。

        Args:
            model_id: 格式为 "provider/model-name" 的标识字符串。
            config: EmbeddingConfig dataclass 实例。
            **overrides: 覆盖 config 中字段的附加参数，优先级最高。

        Returns:
            对应 provider 的 EmbeddingBackend 实例。
        """
        kwargs = dataclasses.asdict(config)
        kwargs.update(overrides)
        return cls.create(model_id, **kwargs)
