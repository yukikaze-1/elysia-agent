"""HuggingFace 本地 embedding 后端 — 使用 sentence-transformers 模型。

同步推理通过 asyncio.to_thread() 包装为异步。
模型缓存于本地，首次使用自动检测本地路径，避免从 HuggingFace Hub 下载。
"""

import asyncio
import logging
import os

import torch
from langchain_huggingface import HuggingFaceEmbeddings

from .protocol import EmbeddingBackend

# 已知模型的向量维度 — 避免首次推理才能获取
_KNOWN_DIMS: dict[str, int] = {
    "BAAI/bge-large-zh-v1.5": 1024,
}


class HuggingFaceBackend(EmbeddingBackend):
    """本地 HuggingFace sentence-transformers 后端。

    自动检测 CUDA/CPU 设备。优先使用本地缓存模型路径。
    向量默认 L2 归一化。
    """

    def __init__(
        self,
        *,
        model_name: str = "BAAI/bge-large-zh-v1.5",
        device: str | None = None,
        cache_folder: str = "/home/yomu/Elysia/model_cache",
        normalize: bool = True,
        dimension: int = 0,
        logger: logging.Logger | None = None,
    ):
        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"

        self._model_name = model_name
        self._dimension = dimension or _KNOWN_DIMS.get(model_name, 0)
        self._logger = logger or logging.getLogger(__name__)

        # 优先使用本地缓存模型，避免从 HuggingFace Hub 下载
        local_path = os.path.join(cache_folder, model_name)
        model_path = local_path if os.path.exists(local_path) else model_name

        self._embeddings = HuggingFaceEmbeddings(
            model_name=model_path,
            model_kwargs={"device": device, "trust_remote_code": True},
            encode_kwargs={"normalize_embeddings": normalize},
            cache_folder=cache_folder,
        )

    @property
    def dimension(self) -> int:
        if self._dimension == 0:
            raise RuntimeError(
                f"Unknown dimension for model '{self._model_name}'. "
                f"Pass dimension= in the constructor."
            )
        return self._dimension

    async def embed_documents(self, texts: list[str], **kwargs) -> list[list[float]]:
        """批量文本 → 向量列表。同步推理在线程池中执行。"""
        return await asyncio.to_thread(self._embeddings.embed_documents, texts)

    async def embed_query(self, text: str, **kwargs) -> list[float]:
        """单条文本 → 单个向量。"""
        return await asyncio.to_thread(self._embeddings.embed_query, text)

    async def close(self) -> None:
        pass  # 本地模型，无连接管理
