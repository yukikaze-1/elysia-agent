"""OpenAI 兼容 embedding 后端 — 覆盖 OpenAI / 兼容 API。

dimension 从首次 API 响应自动获取。
支持 dimensions 参数缩减输出维度（API native 功能）。
"""

import logging
import os

from openai import AsyncOpenAI

from .protocol import EmbeddingBackend


class OpenAICompatEmbeddingBackend(EmbeddingBackend):
    """OpenAI Embedding API 后端。

    dimension 在首次 embed_documents/embed_query 调用后自动解析，
    之前返回 0。
    """

    def __init__(
        self,
        *,
        model: str = "text-embedding-3-large",
        api_key: str = "",
        base_url: str = "",
        logger: logging.Logger | None = None,
    ):
        key = api_key or os.getenv("OPENAI_API_KEY", "")
        self._model = model
        self._dimension = 0  # 首次 API 调用后解析
        self._logger = logger or logging.getLogger(__name__)
        self._client = AsyncOpenAI(api_key=key, base_url=base_url)

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed_documents(self, texts: list[str], **kwargs) -> list[list[float]]:
        response = await self._client.embeddings.create(
            model=self._model,
            input=texts,
            **kwargs,
        )
        # 首次调用自动解析维度
        if self._dimension == 0 and response.data:
            self._dimension = len(response.data[0].embedding)
        return [d.embedding for d in response.data]

    async def embed_query(self, text: str, **kwargs) -> list[float]:
        vecs = await self.embed_documents([text], **kwargs)
        return vecs[0]

    async def close(self) -> None:
        await self._client.close()
