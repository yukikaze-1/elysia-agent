"""MemorySearchPlugin 和 MemorySavePlugin 单元测试。"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from Module._common.error_type import ErrorType
from Module.tool.protocol import ToolContext, ToolResult
from core.tools.memory.plugin import (
    MemorySearchPlugin,
    SearchMemoryParams,
    MemorySavePlugin,
    SaveMemoryParams,
)

from core.tools.tests._failure_helpers import assert_failed

pytestmark = pytest.mark.service

# ──────────────────────────────────────────────────────────────────
# MemorySearchPlugin（记忆搜索插件）
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
        # S2-G1：失败必须是 ToolResult.fail——返回裸字符串会被 _wrap_result
        # 一律标成 success=True，四层信号（LLM 纠错/前端卡片/落库/统计）同时失真。
        assert isinstance(result, ToolResult)
        assert result.success is False
        assert result.error == "记忆系统当前不可用。"
        assert result.error_type is ErrorType.UNAVAILABLE

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
        assert isinstance(result, ToolResult) and result.success is False
        assert result.error == "搜索记忆时出错: 数据库连接失败"
        assert result.error_type is ErrorType.TOOL_ERROR


# ──────────────────────────────────────────────────────────────────
# MemorySavePlugin（记忆保存插件）
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
        """不在 MemoryType 词表中的类型 → 返回 无效的记忆类型 + TitleCase 词表。"""
        mock_memory = AsyncMock()
        params = SaveMemoryParams(content="something", type="invalid_type")
        result = await plugin.execute(ctx=self.make_ctx(mock_memory), params=params)
        assert_failed(result, contains="无效的记忆类型")
        assert "invalid_type" in (result.error or "")
        assert "Fact" in (result.error or "")      # 错误消息含 TitleCase 词表
        assert "Event" in (result.error or "")
        mock_memory.insert.assert_not_called()

    # ── 不可用 ──

    @pytest.mark.asyncio
    async def test_unavailable_when_no_memory_service(
        self, plugin: MemorySavePlugin, ctx_without_memory: ToolContext,
    ):
        """当 ctx.services 中没有 "memory" 时，返回 不可用。"""
        params = SaveMemoryParams(content="测试", type="fact")
        result = await plugin.execute(ctx=ctx_without_memory, params=params)
        # S2-G1：失败必须是 ToolResult.fail——返回裸字符串会被 _wrap_result
        # 一律标成 success=True，四层信号（LLM 纠错/前端卡片/落库/统计）同时失真。
        assert isinstance(result, ToolResult)
        assert result.success is False
        assert result.error == "记忆系统当前不可用。"
        assert result.error_type is ErrorType.UNAVAILABLE

    # ── 正常保存 ──

    @pytest.mark.asyncio
    async def test_save_fact(self, plugin: MemorySavePlugin):
        """正常保存 → 返回 已保存，且只调用一次 insert。"""
        mock_memory = AsyncMock()
        mock_memory.insert.return_value = None

        params = SaveMemoryParams(content="用户叫张三", type="fact")
        result = await plugin.execute(ctx=self.make_ctx(mock_memory), params=params)
        assert result == "已保存 Fact 类型记忆: 用户叫张三"
        mock_memory.insert.assert_called_once_with("用户叫张三", type="Fact")

    # ── 异常 ──

    @pytest.mark.asyncio
    async def test_insert_error(self, plugin: MemorySavePlugin):
        """当 memory.insert() 抛出异常时，返回 出错。"""
        mock_memory = AsyncMock()
        mock_memory.insert.side_effect = RuntimeError("写入失败")

        params = SaveMemoryParams(content="测试内容", type="note")
        result = await plugin.execute(ctx=self.make_ctx(mock_memory), params=params)
        assert isinstance(result, ToolResult) and result.success is False
        assert result.error == "保存记忆时出错: 写入失败"
        assert result.error_type is ErrorType.TOOL_ERROR

    # ── 所有有效类型 ──

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mem_type", ["fact", "preference", "event", "plan", "note",
                                           "Fact", "EVENT"])
    async def test_all_valid_types_accepted(
        self, plugin: MemorySavePlugin, mem_type: str,
    ):
        """五种类型（含 event）大小写不敏感接受，存储归一化为 TitleCase。"""
        mock_memory = AsyncMock()
        mock_memory.insert.return_value = None

        params = SaveMemoryParams(content="内容", type=mem_type)
        result = await plugin.execute(ctx=self.make_ctx(mock_memory), params=params)
        assert "已保存" in result
        assert mem_type.title() in result   # 消息中是 TitleCase
        mock_memory.insert.assert_called_once_with("内容", type=mem_type.title())
