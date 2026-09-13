"""嵌入后端懒加载 provider — 超时 + 多 provider fallback + 线程池执行。"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from Module.embedding import EmbeddingBackend

logger = logging.getLogger(__name__)

#: 加载失败后的重试冷却（秒）。见 :meth:`EmbeddingProvider.ensure`——
#: 既不把失败锁死成终态（CR-10），也不让每次 embed 都去开一个加载线程。
DEFAULT_RETRY_COOLDOWN = 60.0

#: 冷加载的**总预算**（秒），与「单次调用愿意等多久」（`embed_timeout`）是两件事。
#:
#: 加载线程里依次要做：重依赖 import（torch / sentence-transformers）+ manifest
#: 全量 sha256 校验（1.3GB，实测冷盘 9.4s / 热盘 3.5s）+ 权重载入。
#: 实测冷启动 8–20s，而 `embed_timeout` 默认只有 10s——且 `run_in_executor` 的
#: future 一旦被 `wait_for` 取消，**线程仍在跑但结果被丢弃**，等于白干（2026-09-12）。
DEFAULT_LOAD_TIMEOUT = 120.0


class EmbeddingProvider:
    """嵌入后端懒加载 + 超时 + 多 provider fallback。

    线程池执行模型加载，避免阻塞事件循环。
    """

    def __init__(self, embed_timeout: float = 10.0,
                 expected_dimension: int | None = None,
                 retry_cooldown: float = DEFAULT_RETRY_COOLDOWN,
                 load_timeout: float = DEFAULT_LOAD_TIMEOUT):
        """初始化嵌入提供器。

        Args:
            embed_timeout: **单次调用**愿意等待冷加载完成的时长（秒），默认 10s。
                到点即返回 None（本次不嵌入），但加载线程继续跑、结果不丢。
            expected_dimension: 期望的向量维度。非空时透传给支持该参数的远端后端
                （OpenAI 兼容 embedding 的 `dimensions`），使输出维度与向量库集合的
                固定维度对齐——text-embedding-3-small 原生 1536 维，若不显式限定，
                写入 1024 维集合会被维度校验拒绝。
            retry_cooldown: 一次加载失败后隔多久才允许再试（秒）。<=0 表示每次都试。
            load_timeout: 冷加载本身的总预算（秒），默认 120s。见
                :data:`DEFAULT_LOAD_TIMEOUT`——它必须容得下 hash 校验 + 权重载入，
                否则线程白跑、结果被丢。
        """
        self._backend: EmbeddingBackend | None = None
        self._task: asyncio.Task | None = None
        self._failed_at: float | None = None
        self._timeout = embed_timeout
        self._load_timeout = load_timeout
        self._retry_cooldown = retry_cooldown
        self.expected_dimension = expected_dimension

    @property
    def is_ready(self) -> bool:
        """嵌入后端是否已就绪（**非阻塞**，不触发加载）。

        `/ready` 与 debug 面板读的是这个属性——它们绝不能因为探一下嵌入而触发
        一次冷加载（实测冷加载 11s 起）。"未就绪"同时涵盖"正在加载"与"加载失败
        冷却中"，对 readiness 而言两者都是"还不能写长期记忆"。
        """
        return self._backend is not None

    async def ensure(self, wait: bool = False) -> "EmbeddingBackend | None":
        """确保嵌入后端已加载。

        **失败不是终态**：此前用 `self._tried` 在尝试前置位、失败后原样返回 None，
        本进程内再无重试入口——启动时模型还没下载完（或一次超时）就永久降级成
        「不写长期记忆」，且因为下游 `long_term.insert` 在 `embedding == []` 时
        两个分支都不进，**零日志零告警**（评审 CR-10）。现在改为失败后经
        `retry_cooldown` 冷却可再试，冷却期内不重复开线程。

        **两个超时预算**（勿混）：加载本身用 `load_timeout`（宽，容得下 hash 校验
        + 权重载入）；`wait=True` 的调用方只用 `embed_timeout` 等（窄，不拖住一轮
        对话）。到点返回 None 只是"这次不嵌入"，**后台加载照常完成**并写入
        `_backend`，下一轮即可用。

        Args:
            wait: 是否阻塞等待加载完成。默认 False，启动后台加载后立即返回。

        Returns:
            已加载的 EmbeddingBackend 实例，加载中、冷却中或失败时返回 None。
        """
        if self._backend is not None:
            return self._backend
        if self._task is not None and not self._task.done():
            if wait:
                try:
                    await asyncio.wait_for(asyncio.shield(self._task),
                                           timeout=self._timeout)
                except (asyncio.TimeoutError, Exception):
                    pass
            return self._backend
        if self._failed_at is not None:
            elapsed = time.monotonic() - self._failed_at
            if elapsed < self._retry_cooldown:
                return None   # 冷却中——不重试，也不重复刷日志
            logger.info("Embedding: 重试加载（上次失败于 %.0fs 前）", elapsed)

        loop = asyncio.get_event_loop()

        async def _load():
            try:
                embedding = await asyncio.wait_for(
                    loop.run_in_executor(None, self._try_load_sync),
                    timeout=self._load_timeout,
                )
                if embedding is not None:
                    self._backend = embedding
                    self._failed_at = None
                    return
                logger.warning(
                    "Embedding: 无可用后端，长期记忆**不会**写入 Milvus"
                    "（进程内检索仍可用，重启即丢）")
            except asyncio.TimeoutError:
                logger.warning(
                    "Embedding: 加载超时 %.0fs，长期记忆**不会**写入 Milvus"
                    "（进程内检索仍可用，重启即丢）；%.0fs 后重试",
                    self._load_timeout, self._retry_cooldown)
            except Exception as e:
                logger.warning(
                    "Embedding: 加载失败（%s），长期记忆**不会**写入 Milvus"
                    "（进程内检索仍可用，重启即丢）；%.0fs 后重试",
                    e, self._retry_cooldown)
            self._failed_at = time.monotonic()

        self._task = asyncio.create_task(_load(), name="embedding-load")

        if wait:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=self._timeout)
            except (asyncio.TimeoutError, Exception):
                pass
        return self._backend

    async def embed(self, text: str) -> list[float]:
        """封装 embed_query：阻塞加载 + 嵌入。

        Args:
            text: 要嵌入的文本。

        Returns:
            嵌入向量，嵌入失败或后端不可用时返回空列表。
        """
        await self.ensure(wait=True)
        if self._backend is None:
            return []
        try:
            return await self._backend.embed_query(text)
        except Exception:
            # 返回 [] 会让 long_term.insert 静默不落库（CR-10 的下游）——这是**丢数据**，
            # 不是普通的"这次嵌入没成功"，故 WARNING 而非 DEBUG
            logger.warning("Embedding: 单条嵌入失败，该条记忆不会写入 Milvus",
                           exc_info=True)
            return []

    def _openai_create_kwargs(self) -> dict:
        """构造 OpenAI 兼容后端的额外构造参数。

        仅在显式声明 expected_dimension 时传 `dimensions`——未声明时不传，
        避免不支持该参数的兼容端点报错（保持后端原生维度）。
        """
        if self.expected_dimension:
            return {"dimensions": self.expected_dimension}
        return {}

    def _try_load_sync(self) -> "EmbeddingBackend | None":
        """尝试加载 embedding 后端。在线程中运行。

        不直接写 self._backend——由调用方 coroutine 赋值，
        确保 timeout 后线程即使继续运行也不会产生竞态写入。

        Returns:
            EmbeddingBackend 实例，加载失败返回 None。
        """
        from Module.embedding import EmbeddingFactory
        from Module.embedding.model_manifest import DEFAULT_MODEL_ID

        # 1. Try OpenAI-compatible embedding (fast API call, no model download)
        openai_key = os.getenv("OPENAI_API_KEY", "")
        if openai_key:
            try:
                # P2-1：显式限定输出维度，与 Milvus 集合固定维度对齐
                # （text-embedding-3-small 原生 1536，集合为 1024）
                emb = EmbeddingFactory.create(
                    "openai/text-embedding-3-small",
                    **self._openai_create_kwargs(),
                )
                dim = emb.dimension
                logger.info("Embedding: OpenAI embedding connected (dim=%d)", dim)
                return emb
            except Exception as e:
                # 配了 key 却失败 = 该 provider 真的不可用，值得 WARNING——
                # 此前是 DEBUG，只进 app-debug.log，运维在主日志里什么都看不到
                logger.warning("OpenAI embedding unavailable: %s", e)

        # 2. Try HuggingFace local model (slower, loads model to memory)
        # 注：BGE 原生 1024 维，与集合维度一致，不传 dimensions（该后端无此参数）。
        # 模型 ID 取自 manifest 单源——preflight 用同一个常量判"缓存在不在"，
        # 硬编码两份会让"预检的模型"和"加载的模型"悄悄分叉。
        try:
            emb = EmbeddingFactory.create(f"huggingface/{DEFAULT_MODEL_ID}")
            dim = emb.dimension
            logger.info("Embedding: HuggingFace embedding connected (dim=%d)", dim)
            return emb
        except Exception as e:
            logger.warning("HuggingFace embedding unavailable: %s", e)
            return None

    def cancel(self) -> None:
        """取消后台嵌入模型加载任务（如正在进行）。"""
        if self._task and not self._task.done():
            self._task.cancel()

    def reset(self) -> None:
        """丢弃已加载的后端并允许重新加载（S3-09）。

        **为什么必须有**：`Agent` 同一实例支持 `stop()` → `start()` 热重启，
        而 stop 会关掉嵌入后端。原先 provider 仍缓存着那个**已关闭**的 `_backend`，
        重启后 `embed()` 恒返回 `[]`——新记忆没有向量、不入 `_pending_syncs`
        journal、退出即丢，而日志只有一句 DEBUG。归档侧早有对称修复
        （`reopen_all_archives`），嵌入侧此前没有。

        与 `cancel()` 的区别：`cancel()` 只打断在途加载；`reset()` 是"作废状态"，
        清后端引用 + 失败冷却，使下一次 `ensure()` 真的重新加载。
        """
        self.cancel()
        self._task = None
        self._backend = None
        self._failed_at = None
