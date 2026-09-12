"""HuggingFace 本地 embedding 后端 — 使用 sentence-transformers 模型。

同步推理通过 asyncio.to_thread() 包装为异步。
优先使用本地缓存，离线可用。

供应链安全（P1-18）：
- 默认 ``trust_remote_code=False``；仅 manifest + remote-code allowlist 内的
  特殊模型可显式开启。
- 已登记 manifest 的模型加载前校验全部文件 sha256，被篡改/缺失时拒绝启动。
- 未登记 manifest 的模型允许加载但保持 trust_remote_code=False。
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import torch
from langchain_huggingface import HuggingFaceEmbeddings

from paths import ProjectPath
from Module.embedding.model_manifest import (
    get_manifest,
    trust_remote_code_for,
    verify_manifest_dir,
)
from Module.embedding.protocol import EmbeddingBackend

logger = logging.getLogger(__name__)

# 已知模型的向量维度 — 避免首次推理才能获取
_KNOWN_DIMS: dict[str, int] = {
    "BAAI/bge-large-zh-v1.5": 1024,
}


def resolve_cache_folder(cache_folder: str | None = None) -> str:
    """解析模型缓存根目录：显式参数 > ``MODEL_CACHE_DIR`` env > ``ProjectPath.MODEL_CACHE``。

    单源：`HuggingFaceEmbedding` 与 `Module.config.preflight` 的嵌入检查都必须走这里，
    否则「下载到哪 / 预检查哪 / 加载查哪」三者会各说各话（2026-09-12 实测踩到：
    preflight 报「已缓存」而 loader 报「本地缓存缺失」）。
    """
    if cache_folder is not None:
        return cache_folder
    return os.environ.get("MODEL_CACHE_DIR", str(ProjectPath.MODEL_CACHE.resolve()))


def _snapshot_with_config(hub_path: str) -> str | None:
    """在 HF hub 布局目录（``models--Org--Name/``）下找可用快照，没有则 None。

    快照目录名是 commit revision；字典序倒序只为取一个稳定顺序，不代表"最新"。

    **必须校验 ``config.json`` 存在**：下载中断会留下只有 ``refs/`` 的同名空壳
    （实测 ``~/.cache/huggingface/hub/models--BAAI--bge-large-zh-v1.5`` 仅 12 KB），
    空目录若被当成命中，会让后面候选里的完整副本永远没机会。
    """
    snapshots = os.path.join(hub_path, "snapshots")
    if not os.path.isdir(snapshots):
        return None
    for snap in sorted(os.listdir(snapshots), reverse=True):
        snap_path = os.path.join(snapshots, snap)
        if os.path.isfile(os.path.join(snap_path, "config.json")):
            return snap_path
    return None


def find_local_model(model_name: str, cache_folder: str | None = None) -> str | None:
    """在本地缓存中查找模型，返回路径或 None（``cache_folder=None`` 时按默认根解析）。

    候选**按序全部试完**才放弃——命中一个同名但残缺的目录**不是**终止条件。
    顺序（项目缓存优先于家目录缓存）：

    1. ``{cache}/Org/Name``                  直接布局
    2. ``{cache}/models--Org--Name``         hub 布局（``scripts/download_model.py`` 的产出）
    3. ``$HF_HOME/Org/Name``                 直接布局
    4. ``$HF_HOME/models--Org--Name``        hub 布局
    5. ``$HF_HOME/hub/Org/Name``             直接布局
    6. ``$HF_HOME/hub/models--Org--Name``    hub 布局

    两种布局都要求目录里有 ``config.json`` 才算命中（缺它的目录在
    ``local_files_only=True`` 下 transformers 也加载不了，报"命中"只把失败推到更晚）。

    Args:
        model_name: HuggingFace 模型名（如 ``"BAAI/bge-large-zh-v1.5"``）。
        cache_folder: 缓存根目录；None 时经 :func:`resolve_cache_folder` 解析。
    """
    hub_name = "models--" + model_name.replace("/", "--")
    hf_home = os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface")

    roots: list[str] = [resolve_cache_folder(cache_folder),
                        hf_home, os.path.join(hf_home, "hub")]
    for root in roots:
        direct = os.path.join(root, model_name)
        if os.path.isfile(os.path.join(direct, "config.json")):
            return direct
        snapshot = _snapshot_with_config(os.path.join(root, hub_name))
        if snapshot:
            return snapshot
    return None


def _find_local_model(model_name: str, cache_folder: str | None) -> str | None:
    """旧名保留（既有调用点与测试引用）。语义同 :func:`find_local_model`。"""
    return find_local_model(model_name, cache_folder)


def _verified_local_path(model_name: str, local_path: str | None) -> str | None:
    """返回供应链校验通过的本地模型路径，未找到时返回 None。

    对已登记 manifest 的模型：
    - 本地路径缺失 → 抛 RuntimeError，提示先运行 download_model.py。
    - hash 校验失败 → 抛 RuntimeError，拒绝加载（缓存可能被篡改/镜像内容不同）。
    对未登记 manifest 的模型：无法校验，打印 warning 并返回原路径
    （trust_remote_code 仍保持 False，不执行远程代码）。

    Args:
        model_name: HuggingFace 模型名。
        local_path: _find_local_model 找到的本地路径，可为 None。

    Returns:
        校验通过的本地模型路径字符串，或 None（未找到且未登记 manifest）。

    Raises:
        RuntimeError: manifest 模型本地缺失或缓存 hash 校验失败。
    """
    manifest = get_manifest(model_name)
    if manifest is None:
        logger.warning(
            "模型 %s 未登记固定 manifest，无法校验 hash；trust_remote_code 保持 False",
            model_name,
        )
        return local_path
    if local_path is None:
        raise RuntimeError(
            f"模型 {model_name} 本地缓存缺失，拒绝加载。"
            f"请先运行 `python scripts/download_model.py` "
            f"（固定 revision {manifest.revision}）。"
        )
    ok, failures = verify_manifest_dir(Path(local_path), manifest)
    if not ok:
        raise RuntimeError(
            f"模型 {model_name} 缓存校验失败（供应链保护），拒绝加载。"
            "缓存可能被篡改或镜像内容不同，请重新运行 "
            "`python scripts/download_model.py`。失败明细: "
            + "; ".join(failures)
        )
    return local_path


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
                    # GPU 能力查询失败——静默回退 CPU 掩盖硬件探测错误，留日志
                    (logger or logging.getLogger(__name__)).warning(
                        "CUDA 能力查询失败，回退到 CPU 设备", exc_info=True)
                    device = "cpu"
            else:
                device = "cpu"

        if cache_folder is None:
            cache_folder = resolve_cache_folder()

        self._model_name = model_name
        self._dimension = dimension or _KNOWN_DIMS.get(model_name, 0)
        self._logger = logger or logging.getLogger(__name__)

        # 在本地缓存中查找模型 —— 完全避免网络访问。
        # 供应链校验：manifest 模型加载前全量 hash 校验，失败拒绝加载。
        local_path = _verified_local_path(model_name, _find_local_model(model_name, cache_folder))
        if local_path:
            self._logger.info("Using local model: %s", local_path)
        model_path = local_path or model_name

        # 默认关闭 remote code；仅 allowlist + 固定 revision/hash 模型可显式开启
        trust_remote_code = trust_remote_code_for(model_name)

        self._lock = asyncio.Lock()  # 保护 embed() 和 close() 之间的竞态
        self._embeddings = HuggingFaceEmbeddings(
            model_name=model_path,
            model_kwargs={
                "device": device,
                "trust_remote_code": trust_remote_code,
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

    async def embed_documents(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        """批量文本 → 向量列表。同步推理在线程池中执行。"""
        async with self._lock:
            if self._embeddings is None:
                raise RuntimeError("Embedding 模型已关闭")
            return await asyncio.to_thread(self._embeddings.embed_documents, texts)

    async def embed_query(self, text: str, **kwargs: Any) -> list[float]:
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
