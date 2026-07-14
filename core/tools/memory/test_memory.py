"""MemorySearchPlugin 和 MemorySavePlugin 单元测试。"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from Module.tool.protocol import ToolContext
from core.tools.memory.plugin import (
    MemorySearchPlugin,
    SearchMemoryParams,
    MemorySavePlugin,
    SaveMemoryParams,
)

pytestmark = pytest.mark.service

# ──────────────────────────────────────────────────────────────────
# MemorySearchPlugin
# ──────────────────────────────────────────────────────────────────

class TestMemorySearchPlugin:

    @pytest.fixture
    def plugin(self) -> MemorySearchPlugin:
        return MemorySearchPlugin()

    @pytest.fixture
    def ctx_without_memory(self) -> ToolContext:
        return ToolContext(services={})

    def make_ctx(self, memory) -> ToolContext:
        return ToolContext(services={"memory": memory})

    @pytest.mark.asyncio
    async def test_unavailable_when_no_memory_service(
        self, plugin: MemorySearchPlugin, ctx_without_memory: ToolContext,
    ):
        """当 ctx.services 中没有 "memory" 时，返回 不可用。"""
        params = SearchMemoryParams(query="测试")
        result = await plugin.execute(ctx=ctx_without_memory, params=params)
        assert result == "记忆系统当前不可用。"

    @pytest.mark.asyncio
    async def test_no_results_returns_not_found(self, plugin: MemorySearchPlugin):
        """当 memory.search() 返回空列表时，返回 未找到。"""
        mock_memory = AsyncMock()
        mock_memory.search.return_value = []

        params = SearchMemoryParams(query="nothing")
        result = await plugin.execute(ctx=self.make_ctx(mock_memory), params=params)
        assert result == "未找到相关记忆。"

    @pytest.mark.asyncio
    async def test_formats_results(self, plugin: MemorySearchPlugin):
        """搜索结果应格式化: [ID:...] (类型:..., 重要性:...) text"""
        mock_memory = AsyncMock()
        r1 = MagicMock()
        r1.id = "abc123"
        r1.memory_type = "fact"
        r1.poignancy = 8
        r1.text = "用户喜欢 Python"

        r2 = MagicMock()
        r2.id = "def456"
        r2.memory_type = "preference"
        r2.poignancy = 5
        r2.text = "用户偏好深色主题"

        mock_memory.search.return_value = [r1, r2]

        params = SearchMemoryParams(query="用户 偏好")
        result = await plugin.execute(ctx=self.make_ctx(mock_memory), params=params)

        lines = result.split("\n")
        assert lines[0] == "[ID:abc123] (类型:fact, 重要性:8) 用户喜欢 Python"
        assert lines[1] == "[ID:def456] (类型:preference, 重要性:5) 用户偏好深色主题"

        mock_memory.search.assert_called_once_with(
            query_text="用户 偏好", top_k=5,
        )

    @pytest.mark.asyncio
    async def test_search_error(self, plugin: MemorySearchPlugin):
        """当 memory.search() 抛出异常时，返回 出错 信息。"""
        mock_memory = AsyncMock()
        mock_memory.search.side_effect = RuntimeError("数据库连接失败")

        params = SearchMemoryParams(query="anything")
        result = await plugin.execute(ctx=self.make_ctx(mock_memory), params=params)
        assert result == "搜索记忆时出错: 数据库连接失败"


# ──────────────────────────────────────────────────────────────────
# MemorySavePlugin
# ──────────────────────────────────────────────────────────────────

class TestMemorySavePlugin:

    @pytest.fixture
    def plugin(self) -> MemorySavePlugin:
        return MemorySavePlugin()

    @pytest.fixture
    def ctx_without_memory(self) -> ToolContext:
        return ToolContext(services={})

    def make_ctx(self, memory) -> ToolContext:
        return ToolContext(services={"memory": memory})

    # ── 无效类型 ──

    @pytest.mark.asyncio
    async def test_invalid_memory_type(self, plugin: MemorySavePlugin):
        """不在 {fact, plan, preference, note} 中的类型 → 返回 无效的记忆类型。"""
        mock_memory = AsyncMock()
        params = SaveMemoryParams(content="something", type="invalid_type")
        result = await plugin.execute(ctx=self.make_ctx(mock_memory), params=params)
        assert "无效的记忆类型" in result
        assert "invalid_type" in result
        mock_memory.insert.assert_not_called()

    # ── 不可用 ──

    @pytest.mark.asyncio
    async def test_unavailable_when_no_memory_service(
        self, plugin: MemorySavePlugin, ctx_without_memory: ToolContext,
    ):
        """当 ctx.services 中没有 "memory" 时，返回 不可用。"""
        params = SaveMemoryParams(content="测试", type="fact")
        result = await plugin.execute(ctx=ctx_without_memory, params=params)
        assert result == "记忆系统当前不可用。"

    # ── 正常保存 ──

    @pytest.mark.asyncio
    async def test_save_fact(self, plugin: MemorySavePlugin):
        """正常保存 → 返回 已保存，且只调用一次 insert。"""
        mock_memory = AsyncMock()
        mock_memory.insert.return_value = None

        params = SaveMemoryParams(content="用户叫张三", type="fact")
        result = await plugin.execute(ctx=self.make_ctx(mock_memory), params=params)
        assert result == "已保存 fact 类型记忆: 用户叫张三"
        mock_memory.insert.assert_called_once_with("用户叫张三", type="fact")

    # ── 异常 ──

    @pytest.mark.asyncio
    async def test_insert_error(self, plugin: MemorySavePlugin):
        """当 memory.insert() 抛出异常时，返回 出错。"""
        mock_memory = AsyncMock()
        mock_memory.insert.side_effect = RuntimeError("写入失败")

        params = SaveMemoryParams(content="测试内容", type="note")
        result = await plugin.execute(ctx=self.make_ctx(mock_memory), params=params)
        assert result == "保存记忆时出错: 写入失败"

    # ── 所有有效类型 ──

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mem_type", ["fact", "plan", "preference", "note"])
    async def test_all_valid_types_accepted(
        self, plugin: MemorySavePlugin, mem_type: str,
    ):
        """fact, plan, preference, note 四种类型都会被接受。"""
        mock_memory = AsyncMock()
        mock_memory.insert.return_value = None

        params = SaveMemoryParams(content="内容", type=mem_type)
        result = await plugin.execute(ctx=self.make_ctx(mock_memory), params=params)
        assert "已保存" in result
        assert mem_type in result
        mock_memory.insert.assert_called_once()
