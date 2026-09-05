"""ScopedLongTermMemory — 绑定 MemoryScope 的记忆视图（架构稿 §8.2）。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from core.memory.scope import MemoryScope
from core.memory.long_term import MemoryEntry
from core.schema.types import MemoryId

if TYPE_CHECKING:
    from core.memory.long_term import LongTermMemory


def _in_scope(entry: MemoryEntry, scope: MemoryScope) -> bool:
    """判断条目是否落在给定 scope 内（character_instance_id + user_id 匹配）。"""
    return (
        entry.user_id == scope.user_id
        and entry.character_instance_id == scope.character_instance_id
    )


class ScopedLongTermMemory:
    """绑定 scope 的只读/读接口——调用点无法在方法调用时绕过 scope。

    insert/search/update_text/delete/get_all 全部作用域过滤；底层共享同一
    LongTermMemory（_entries/_repo），但 scope 在视图创建时固定。
    """

    def __init__(self, long_term: "LongTermMemory", scope: MemoryScope):
        self._long_term = long_term
        self._scope = scope

    @property
    def scope(self) -> MemoryScope:
        """本视图绑定的 MemoryScope。"""
        return self._scope

    async def insert(self, text: str, **metadata: object) -> None:
        """插入一条记忆到本 scope（user_id/character_instance_id/visibility 由 scope 派生）。"""
        scope = self._scope
        await self._long_term.insert(
            text,
            user_id=scope.user_id,
            character_instance_id=scope.character_instance_id,
            visibility=scope.visibility.value,
            **metadata,
        )

    async def search(
        self,
        query_text: str = "",
        top_k: int = 5,
        memory_type: str | None = None,
    ) -> list[MemoryEntry]:
        """向量搜索 + rerank，仅返回本 scope 内的条目。"""
        return await self._long_term.search(
            query_text=query_text, top_k=top_k, memory_type=memory_type,
            scope=self._scope,
        )

    def get_all(self) -> list[MemoryEntry]:
        """返回本 scope 全部活跃条目的副本。"""
        return [e for e in self._long_term.get_all()
                if _in_scope(e, self._scope)]

    async def update_text(
        self, entry_id: MemoryId, new_text: str = "",
        poignancy: int | None = None,
        memory_type: str | None = None,
    ) -> bool:
        """更新记忆文本——**签名与 LongTermMemory.update_text 对齐**（含 poignancy，consolidator/reflector 调用须传 poignancy=...）。

        注意：若 scoped view 不转发 poignancy，consolidator _compare_and_resolve
        (consolidator.py:366) 传 `poignancy=action.poignancy` 会 TypeError/丢 poignancy。
        底层实现带 scope 验证：目标 entry 必须在本 scope 内否则视为 not found。
        """
        return await self._long_term.update_text(
            entry_id, new_text, poignancy=poignancy, memory_type=memory_type,
            scope=self._scope,
        )

    async def delete(self, entry_id: MemoryId) -> bool:
        """删除本 scope 内的一条记忆（scope 外视为 not found）。"""
        return await self._long_term.delete(entry_id, scope=self._scope)

    async def mark_absorbed(self, entry_id: MemoryId) -> None:
        """标记条目已被 merge 吸收（memory_reflector._execute_actions:342 调用）。

        注意：ScopedLongTermMemory **必须**暴露 mark_absorbed——MemoryReflector 以
        scoped view 为 _memory 时，merge 分支调 mark_absorbed(absorbed_id)。缺失会
        AttributeError（被 _execute_actions 的 except 捕获，merge 的 originals 不被隐藏）。
        """
        await self._long_term.mark_absorbed(entry_id)
