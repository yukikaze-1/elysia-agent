"""CharacterRuntimeRegistry — 按 instance_id 解析 runtime（架构稿 §7.1）。

LRU 缓存。驱逐策略（架构稿 §7.1：「注册表可以 LRU 缓存，但驱逐前必须 flush
dirty state。正在生成、反思或压缩的 runtime 不得被驱逐」）：

- **busy 不驱逐**：generation/破坏性变更在途（含 `_after_response` tail）期间
  runtime 受 lease 保护（BE-STAGEH-01 起由 CharacterRuntime 维护 `_busy`）；
- **驱逐前 flush**：被选中的 runtime 先调度 `flush()`（把内存 CharacterRuntimeState
  经 persist_fn 写回 Mongo）再从缓存移除，避免未持久化状态随对象丢弃；
- 无法驱逐时（全部受保护）提前退出，不做强制牺牲。

注：曾经存在一个从未接线的 `set_session_binding_index`「被活会话绑定的 instance
不驱逐」骨架（M2 草案），已移除——该保护不在架构稿 §7.1 要求内，且回合边界
`_flush_runtime_at_turn_boundary()` 已保证状态在回合间持久，驱逐后可由
`get_or_load()` 从存储恢复。
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Awaitable, Callable

from core.character.runtime import CharacterRuntime

logger = logging.getLogger(__name__)


class CharacterRuntimeRegistry:
    def __init__(self, *, max_size: int = 32):
        self._max_size = max_size
        self._runtimes: OrderedDict[str, CharacterRuntime] = OrderedDict()
        # Async storage-backed loads must be single-flight per instance.  Locks are
        # retained for the bounded set of instance IDs seen by this process so a
        # failed load cannot race a queued retry through a replacement lock.
        self._load_locks: dict[str, asyncio.Lock] = {}

    def get(self, instance_id: str, factory: Callable[[], CharacterRuntime]) -> CharacterRuntime:
        if instance_id in self._runtimes:
            self._runtimes.move_to_end(instance_id)
            return self._runtimes[instance_id]
        rt = factory()
        self._runtimes[instance_id] = rt
        self._evict_if_needed()
        return rt

    async def get_or_load(
        self,
        instance_id: str,
        loader: Callable[[], Awaitable[CharacterRuntime]],
    ) -> CharacterRuntime:
        """Return a cached runtime or load it exactly once from async storage.

        The old synchronous ``get`` remains for trusted built-ins and unit-test
        pre-seeding.  Custom instances require Mongo reads and therefore must use
        this path; they are never fabricated by stripping an instance ID prefix.
        """
        existing = self._runtimes.get(instance_id)
        if existing is not None:
            self._runtimes.move_to_end(instance_id)
            return existing

        lock = self._load_locks.setdefault(instance_id, asyncio.Lock())
        async with lock:
            existing = self._runtimes.get(instance_id)
            if existing is not None:
                self._runtimes.move_to_end(instance_id)
                return existing
            runtime = await loader()
            if runtime.instance.instanceId != instance_id:
                raise ValueError(
                    "runtime loader returned mismatched instance: "
                    f"expected {instance_id}, got {runtime.instance.instanceId}"
                )
            self._runtimes[instance_id] = runtime
            self._evict_if_needed()
            return runtime

    def get_existing(self, instance_id: str) -> "CharacterRuntime | None":
        """只读解析已构建 runtime——不懒建(区别于 get,后者 factory 会建)。

        REST 只读端点用:registry 命中即读、未命中返回 None(fail-closed 语义)。
        """
        return self._runtimes.get(instance_id)

    def mark_busy(self, instance_id: str, busy: bool) -> None:
        """手工标记 runtime busy（防驱逐）。

        BE-STAGEH-01 起 `_busy` 由 CharacterRuntime 的 lease
        （try_begin_generation/begin_generation_tail/try_begin_destructive_mutation）
        维护，本方法只作为绕过 lease 的显式覆盖口，生产代码不再调用。
        """
        rt = self._runtimes.get(instance_id)
        if rt is not None:
            setattr(rt, "_busy", busy)

    def invalidate(self, instance_id: str, reason: str) -> None:
        if instance_id in self._runtimes:
            self._runtimes.pop(instance_id, None)
            logger.debug("runtime invalidated: %s (%s)", instance_id, reason)

    def release(self, instance_id: str, reason: str) -> None:
        """释放 runtime（先 flush 再清）。"""
        import asyncio
        rt = self._runtimes.pop(instance_id, None)
        if rt is not None:
            try:
                asyncio.get_running_loop().create_task(rt.flush())
            except RuntimeError:
                pass
            logger.debug("runtime released: %s (%s)", instance_id, reason)

    def flush(self, instance_id: str) -> None:
        rt = self._runtimes.get(instance_id)
        if rt is not None:
            rt.dirty = False

    def flush_all(self) -> None:
        for rt in self._runtimes.values():
            rt.dirty = False

    async def close_all(self) -> None:
        """关机 flush 全部（A1：shutdown 走这里，非单 engine）。"""
        for rt in self._runtimes.values():
            await rt.flush()
        self._runtimes.clear()

    def __len__(self) -> int:
        return len(self._runtimes)

    def _evict_if_needed(self) -> None:
        """LRU 驱逐；busy 不驱逐，驱逐前先把状态 flush 落库（架构稿 §7.1）。

        busy（generation/反思/压缩在途，含 tail lease）受保护；全部受保护时提前
        退出，不强制牺牲。选中者先调度 flush()——`flush()` 无条件经 persist_fn 落库，
        因此即便状态未标记 dirty 也不会丢失。
        """
        while len(self._runtimes) > self._max_size:
            evicted: str | None = None
            for instance_id, rt in list(self._runtimes.items()):
                if getattr(rt, "_busy", False):
                    continue            # generation/破坏性变更在途不驱逐（BE-STAGEH-01 起由 runtime lease 维护）
                evicted = instance_id
                break
            if evicted is None:
                break                   # 全受保护，无法驱逐
            rt = self._runtimes.pop(evicted, None)
            if rt is not None:
                # 架构稿 §7.1「驱逐前必须 flush dirty state」：调度落库再丢弃对象。
                # 同步上下文无法 await（get/get_or_load 为 sync 点），故仿 release() 起后台任务。
                try:
                    asyncio.get_running_loop().create_task(rt.flush())
                except RuntimeError:
                    pass                # 无运行事件循环（同步测试）→ 无持久化后端可写
            logger.debug("runtime evicted (LRU): %s", evicted)
