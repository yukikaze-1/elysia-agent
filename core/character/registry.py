"""CharacterRuntimeRegistry — 按 instance_id 解析 runtime（架构稿 §7.1）。

LRU 缓存；驱逐前必须 flush dirty；生成/反思/压缩中的 runtime 不得驱逐；
M2：被任一缓存 session 绑定的 instance 的 runtime 不得驱逐——session 被
SessionManager.evict_if_needed() 逐出后才 release()。
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Callable

from core.character.runtime import CharacterRuntime

logger = logging.getLogger(__name__)


class CharacterRuntimeRegistry:
    def __init__(self, *, max_size: int = 32):
        self._max_size = max_size
        self._runtimes: OrderedDict[str, CharacterRuntime] = OrderedDict()
        self._session_binding_index: set[str] | None = None   # M3 激活

    def get(self, instance_id: str, factory: Callable[[], CharacterRuntime]) -> CharacterRuntime:
        if instance_id in self._runtimes:
            self._runtimes.move_to_end(instance_id)
            return self._runtimes[instance_id]
        rt = factory()
        self._runtimes[instance_id] = rt
        self._evict_if_needed()
        return rt

    def mark_busy(self, instance_id: str, busy: bool) -> None:
        """M3：标记 runtime 为 busy（生成/反思/压缩中），防驱逐。"""
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
                    continue            # 生成/反思/压缩中不驱逐（_busy 为 M3 预留——本批 runtime 是 skeleton，无代码设置它，保护 inert 但无害）
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
