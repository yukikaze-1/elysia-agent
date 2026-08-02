"""HuggingFace 本地 embedding 后端 — 使用 sentence-transformers 模型。

同步推理通过 asyncio.to_thread() 包装为异步。
优先使用本地缓存，离线可用。
"""

import asyncio
import logging
import os
from pathlib import Path

import torch
from langchain_huggingface import HuggingFaceEmbeddings

from paths import ProjectPath
from Module.embedding.protocol import EmbeddingBackend

# 已知模型的向量维度 — 避免首次推理才能获取
_KNOWN_DIMS: dict[str, int] = {
    "BAAI/bge-large-zh-v1.5": 1024,
}


def _find_local_model(model_name: str, cache_folder: str | None) -> str | None:
    """在本地缓存中查找模型，返回路径或 None。

    依次检查：
    1. 显式缓存目录（如 {cache_folder}/BAAI/bge-large-zh-v1.5）
    2. HuggingFace hub 缓存（~/.cache/huggingface/hub/models--BAAI--bge-large-zh-v1.5）

    Args:
        model_name: HuggingFace 模型名（如 "BAAI/bge-large-zh-v1.5"）。
        cache_folder: 显式缓存根目录，可为 None。

    Returns:
        本地模型路径字符串，未找到时返回 None。
    """
    cache_folder = cache_folder or ""
    # 1. Explicit cache folder (e.g. /home/yomu/Elysia/model_cache/BAAI/bge-large-zh-v1.5)
    explicit = os.path.join(cache_folder, model_name)
    if os.path.isdir(explicit):
        return explicit

    # 2. HuggingFace hub cache (~/.cache/huggingface/hub/models--BAAI--bge-large-zh-v1.5)
    hub_name = "models--" + model_name.replace("/", "--")
    hf_home = os.environ.get("HF_HOME", os.path.expanduser("~/.cache/huggingface"))
    for base in (hf_home, os.path.join(hf_home, "hub")):
        hub_path = os.path.join(base, hub_name)
        if os.path.isdir(hub_path):
            # Find latest snapshot
            snapshots = os.path.join(hub_path, "snapshots")
            if os.path.isdir(snapshots):
                for snap in sorted(os.listdir(snapshots), reverse=True):
                    snap_path = os.path.join(snapshots, snap)
                    if os.path.isfile(os.path.join(snap_path, "config.json")):
                        return snap_path
    return None


class HuggingFaceBackend(EmbeddingBackend):
    """本地 HuggingFace sentence-transformers 后端。

    自动检测 CUDA/CPU 设备。优先使用本地缓存模型路径。
    向量默认 L2 归一化。离线可用。
    """

    def __init__(
        self,
        *,
        model_name: str = "BAAI/bge-large-zh-v1.5",
        device: str | None = None,
        cache_folder: str | None = None,
        normalize: bool = True,
        dimension: int = 0,
        logger: logging.Logger | None = None,
    ):
        """初始化 HuggingFace embedding 后端。

        Args:
            model_name: HuggingFace 模型名，默认 "BAAI/bge-large-zh-v1.5"。
            device: 推理设备，None 时自动检测（CUDA >= compute 7 → cuda，否则 cpu）。
            cache_folder: 模型缓存目录，None 时使用 MODEL_CACHE_DIR 环境变量
                或项目根目录的 .model_cache。
            normalize: 是否对输出向量做 L2 归一化，默认 True。
            dimension: 向量维度，0 表示从 _KNOWN_DIMS 自动获取，
                未知模型需显式指定。
            logger: 日志记录器，未提供时自动创建。
        """
        if device is None:
            if torch.cuda.is_available():
                try:
                    major, minor = torch.cuda.get_device_capability()
                    if major >= 7:
                        device = "cuda"
                    else:
                        device = "cpu"
                except Exception:
                    device = "cpu"
            else:
                device = "cpu"

        if cache_folder is None:
            cache_folder = os.environ.get(
                "MODEL_CACHE_DIR",
                str(ProjectPath.MODEL_CACHE.resolve()),
            )

        self._model_name = model_name
        self._dimension = dimension or _KNOWN_DIMS.get(model_name, 0)
        self._logger = logger or logging.getLogger(__name__)

        # Find model in local cache — avoid network access entirely
        local_path = _find_local_model(model_name, cache_folder)
        if local_path:
            self._logger.info("Using local model: %s", local_path)
        model_path = local_path or model_name

        self._lock = asyncio.Lock()  # 保护 embed() 和 close() 之间的竞态
        self._embeddings = HuggingFaceEmbeddings(
            model_name=model_path,
            model_kwargs={
                "device": device,
                "trust_remote_code": True,
                "local_files_only": True,
            },
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
        async with self._lock:
            if self._embeddings is None:
                raise RuntimeError("Embedding 模型已关闭")
            return await asyncio.to_thread(self._embeddings.embed_documents, texts)

    async def embed_query(self, text: str, **kwargs) -> list[float]:
        """单条文本 → 单个向量。"""
        async with self._lock:
            if self._embeddings is None:
                raise RuntimeError("Embedding 模型已关闭")
            return await asyncio.to_thread(self._embeddings.embed_query, text)

    async def close(self) -> None:
        """释放模型，清理显存。与 embed 互斥，防止 segfault。"""
        async with self._lock:
            if hasattr(self, "_embeddings") and self._embeddings is not None:
                del self._embeddings
                self._embeddings = None
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        self._closed = True
