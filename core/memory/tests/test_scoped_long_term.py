# core/memory/tests/test_scoped_long_term.py
import pytest
from core.memory.scope import MemoryScope
from core.memory.long_term import LongTermMemory
from core.memory.scoped_long_term import ScopedLongTermMemory

# LongTermMemory 单模块测试（离线内存兜底，不连真实 Milvus）——对齐 test_long_term.py 的 service。
pytestmark = pytest.mark.service


@pytest.mark.asyncio
async def test_for_scope_returns_scoped_view():
    ltm = LongTermMemory(enable_eviction=False)
    view_a = ltm.for_scope(MemoryScope(user_id="usr_a", character_instance_id="chi_a"))
    assert isinstance(view_a, ScopedLongTermMemory)
    assert view_a._scope.character_instance_id == "chi_a"


@pytest.mark.asyncio
async def test_insert_search_scope_isolated():
    ltm = LongTermMemory(enable_eviction=False)
    view_a = ltm.for_scope(MemoryScope(user_id="usr_a", character_instance_id="chi_a"))
    view_b = ltm.for_scope(MemoryScope(user_id="usr_b", character_instance_id="chi_b"))
    await view_a.insert("A 的记忆", type="fact")
    await view_b.insert("B 的记忆", type="fact")
    # search 须带 scope
    res_a = await view_a.search(query_text="记忆", top_k=5)
    assert all(e.character_instance_id == "chi_a" for e in res_a)
    res_b = await view_b.search(query_text="记忆", top_k=5)
    assert all(e.character_instance_id == "chi_b" for e in res_b)
    # 不同 scope 互不可见
    assert not any("B" in e.text for e in res_a)
    assert not any("A" in e.text for e in res_b)
