"""嵌入后端懒加载 provider — 超时 + 多 provider fallback + 线程池执行。"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Module.embedding import EmbeddingBackend

logger = logging.getLogger(__name__)


class EmbeddingProvider:
    """嵌入后端懒加载 + 超时 + 多 provider fallback。

    线程池执行模型加载，避免阻塞事件循环。
    """

    def __init__(self, embed_timeout: float = 10.0):
        self._backend: EmbeddingBackend | None = None
        self._tried = False
        self._task: asyncio.Task | None = None
        self._timeout = embed_timeout

    async def ensure(self, wait: bool = False) -> EmbeddingBackend | None:
        """确保嵌入后端已加载。返回 backend 或 None。"""
        if self._backend is not None:
            return self._backend
        if self._tried:
            if wait and self._task and not self._task.done():
                try:
                    await asyncio.wait_for(self._task, timeout=self._timeout)
                except (asyncio.TimeoutError, Exception):
                    pass
            return self._backend
        self._tried = True

        loop = asyncio.get_event_loop()

        async def _load():
            try:
                embedding = await asyncio.wait_for(
                    loop.run_in_executor(None, self._try_load_sync),
                    timeout=self._timeout,
                )
                if embedding is not None:
                    self._backend = embedding
            except asyncio.TimeoutError:
                logger.info("Embedding: load timed out after %.0fs, text-only mode", self._timeout)
            except Exception as e:
                logger.info("Embedding: unavailable (%s), text-only mode", e)

        self._task = asyncio.create_task(_load(), name="embedding-load")

        if wait:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=self._timeout)
            except (asyncio.TimeoutError, Exception):
                pass
        return self._backend

    async def embed(self, text: str) -> list[float]:
        """封装 embed_query：阻塞加载 + 嵌入。失败返回空列表。"""
        await self.ensure(wait=True)
        if self._backend is None:
            return []
        try:
            return await self._backend.embed_query(text)
        except Exception:
            logger.debug("Embedding failed", exc_info=True)
            return []

    def _try_load_sync(self):
        """Try to load embedding backend. Returns instance or None. Runs in thread.

        不直接写 self._backend——由调用方 coroutine 赋值，
        确保 timeout 后线程即使继续运行也不会产生竞态写入。
        """
        from Module.embedding import EmbeddingFactory

        # 1. Try OpenAI-compatible embedding (fast API call, no model download)
        openai_key = os.getenv("OPENAI_API_KEY", "")
        if openai_key:
            try:
                emb = EmbeddingFactory.create("openai/text-embedding-3-small")
                dim = emb.dimension
                logger.info("Embedding: OpenAI embedding connected (dim=%d)", dim)
                return emb
            except Exception as e:
                logger.debug("OpenAI embedding unavailable: %s", e)

        # 2. Try HuggingFace local model (slower, loads model to memory)
        try:
            emb = EmbeddingFactory.create("huggingface/BAAI/bge-large-zh-v1.5")
            dim = emb.dimension
            logger.info("Embedding: HuggingFace embedding connected (dim=%d)", dim)
            return emb
        except Exception as e:
            logger.debug("HuggingFace embedding unavailable: %s", e)
            return None

    def cancel(self) -> None:
        """取消后台加载任务。"""
        if self._task and not self._task.done():
            self._task.cancel()
