"""CharacterRuntimeRegistry — 按 instance_id 解析 runtime（架构稿 §7.1）。

LRU 缓存；驱逐前必须 flush dirty；生成/反思/压缩中的 runtime 不得驱逐；
M2：被任一缓存 session 绑定的 instance 的 runtime 不得驱逐——session 被
SessionManager.evict_if_needed() 逐出后才 release()。
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
        self._session_binding_index: set[str] | None = None   # M3 激活
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

    def set_session_binding_index(self, idx: set[str] | None) -> None:
        """M2：SessionManager 注入绑定索引；被绑定 instance 的 runtime 不驱逐。"""
        self._session_binding_index = idx

    def invalidate(self, instance_id: str, reason: str) -> None:
        if instance_id in self._runtimes:
            self._runtimes.pop(instance_id, None)
            logger.debug("runtime invalidated: %s (%s)", instance_id, reason)

    def release(self, instance_id: str, reason: str) -> None:
        """M2：session 被逐出时释放其绑定 instance 的 runtime（先 flush 再清）。"""
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
        """LRU 驱逐，但受保护（CORRECTNESS #7）：dirty / busy / 仍被 session 绑定的不驱逐。"""
        while len(self._runtimes) > self._max_size:
            evicted: str | None = None
            for instance_id, rt in list(self._runtimes.items()):
                if rt.dirty:
                    continue            # 未 flush 不驱逐
                if getattr(rt, "_busy", False):
                    continue            # generation/破坏性变更在途不驱逐（BE-STAGEH-01 起由 runtime lease 维护）
                if self._is_bound_by_live_session(instance_id):
                    continue            # M2：仍被缓存 session 绑定不驱逐
                evicted = instance_id
                break
            if evicted is None:
                break                   # 全受保护，无法驱逐
            self._runtimes.pop(evicted, None)

    def _is_bound_by_live_session(self, instance_id: str) -> bool:
        """M2：该 instance 是否仍被 SessionManager 缓存池中的 session 引用。

        本批 M1/M2 骨架——由 SessionManager 注入 binding 索引；无索引时返回 False。
        """
        idx = getattr(self, "_session_binding_index", None)
        if idx is None:
            return False
        return instance_id in idx
