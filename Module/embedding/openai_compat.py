"""OpenAI 兼容 embedding 后端 — 覆盖 OpenAI / 兼容 API。

dimension 从首次 API 响应自动获取；构造时传 `dimensions` 可显式限定输出维度
（OpenAI text-embedding-3-* 的原生能力），用于与固定维度的向量库集合对齐。
"""

import logging
import os
from typing import Any

from openai import AsyncOpenAI

from Module.embedding.protocol import EmbeddingBackend


class OpenAICompatEmbeddingBackend(EmbeddingBackend):
    """OpenAI Embedding API 后端。

    dimension 在首次 embed_documents/embed_query 调用后自动解析；
    若构造时显式指定 `dimensions`，则在首次调用前即返回该值。
    """

    def __init__(
        self,
        *,
        model: str = "text-embedding-3-large",
        api_key: str = "",
        base_url: str = "",
        dimensions: int | None = None,
        logger: logging.Logger | None = None,
    ):
        """初始化 OpenAI 兼容 embedding 后端。

        Args:
            model: embedding 模型名，默认 "text-embedding-3-large"。
            api_key: API 密钥，空串时从 OPENAI_API_KEY 环境变量读取。
            base_url: API 基础 URL，空串时使用 OpenAI 默认地址。
            dimensions: 显式输出维度（OpenAI text-embedding-3-* 原生支持）。用于让
                向量维度与 VectorDB 集合的固定维度对齐——不指定时由模型原生维度决定
                （如 text-embedding-3-small 为 1536），可能与既有集合不匹配。
            logger: 日志记录器，未提供时自动创建。
        """
        key = api_key or os.getenv("OPENAI_API_KEY", "")
        self._model = model
        self._dimension = 0  # 首次 API 调用后解析
        self._dimensions = dimensions
        self._logger = logger or logging.getLogger(__name__)
        # 显式超时——SDK 默认 600s 总超时，上游挂起可拖住 insert/recall 10 分钟
        # （对照 Module/llm/openai_compat.py 的 httpx.Timeout(60.0, connect=10.0)）
        self._client = AsyncOpenAI(
            api_key=key, base_url=base_url,
            timeout=60.0, max_retries=2,
        )

    @property
    def dimension(self) -> int:
        # 已显式声明维度时，首次调用前即可返回真值（无需等 API 响应）
        return self._dimension or self._dimensions or 0

    async def embed_documents(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        # 调用方显式传 dimensions 优先；否则回落到构造时声明的维度
        if self._dimensions is not None and "dimensions" not in kwargs:
            kwargs["dimensions"] = self._dimensions
        response = await self._client.embeddings.create(
            model=self._model,
            input=texts,
            **kwargs,
        )
        # 首次调用自动解析维度
        if self._dimension == 0 and response.data:
            self._dimension = len(response.data[0].embedding)
        return [d.embedding for d in response.data]

    async def embed_query(self, text: str, **kwargs: Any) -> list[float]:
        vecs = await self.embed_documents([text], **kwargs)
        return vecs[0]

    async def close(self) -> None:
        await self._client.close()
        self._closed = True
