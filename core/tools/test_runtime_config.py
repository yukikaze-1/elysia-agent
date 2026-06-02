"""RuntimeConfigPlugin 单元测试。"""

import pytest
from unittest.mock import MagicMock

from core.tools.runtime_config import RuntimeConfigPlugin, RuntimeConfigParams
from Module.tool.protocol import ToolContext


class TestRuntimeConfigPlugin:
    """RuntimeConfigPlugin 的测试用例。"""

    @pytest.fixture
    def plugin(self) -> RuntimeConfigPlugin:
        return RuntimeConfigPlugin()

    @pytest.fixture
    def base_ctx(self) -> ToolContext:
        """创建基础 ToolContext，默认不包含 config 服务。"""
        return ToolContext(
            session_id="test-session",
            user_id="test-user",
            trace_id="test-trace",
            services={},
        )

    # ---- 无 config 服务时 ----

    @pytest.mark.asyncio
    async def test_no_config_service_shows_only_tool_count(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """当 ctx.services 中没有 "config" 键时，只显示工具数量行。"""
        base_ctx.services["_tool_names"] = ["tool_a", "tool_b", "tool_c"]
        params = RuntimeConfigParams()
        result = await plugin.execute(ctx=base_ctx, params=params)
        lines = result.split("\n")

        # 应包含工具数量行
        assert "已加载工具 (3)" in result
        assert "tool_a" in result

        # 不应包含配置相关内容（没有 config 服务）
        assert "LLM" not in result
        assert "TTS" not in result
        assert "STT" not in result
        assert "服务:" not in result

        # 无 _tool_names 时的默认行为
        base_ctx.services.pop("_tool_names")
        result_empty = await plugin.execute(ctx=base_ctx, params=params)
        assert "已加载工具 (0)" in result_empty

    # ---- 完整 config mock ----

    @pytest.mark.asyncio
    async def test_full_config_shows_all_sections(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """当 config mock 包含所有 section 时，输出包含模型名、TTS/STT 状态、端口。"""
        config = MagicMock()
        config.llm.model = "deepseek/deepseek-chat"
        config.tts.enabled = True
        config.stt.enabled = False
        config.server.host = "127.0.0.1"
        config.server.port = 8080

        base_ctx.services["config"] = config
        base_ctx.services["_tool_names"] = ["tool_a"]

        params = RuntimeConfigParams()
        result = await plugin.execute(ctx=base_ctx, params=params)

        assert "LLM 模型: deepseek/deepseek-chat" in result
        assert "TTS: 启用" in result
        assert "STT: 禁用" in result
        assert "服务: 127.0.0.1:8080" in result
        assert "已加载工具 (1)" in result

    # ---- config sections 为 None ----

    @pytest.mark.asyncio
    async def test_none_sections_does_not_crash(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """当 llm/tts/stt/server 均为 None 时，不崩溃，仍显示工具数量。"""
        config = MagicMock()
        config.llm = None
        config.tts = None
        config.stt = None
        config.server = None

        base_ctx.services["config"] = config
        base_ctx.services["_tool_names"] = ["tool_a"]

        params = RuntimeConfigParams()
        result = await plugin.execute(ctx=base_ctx, params=params)

        # 不应崩溃，工具数量行正常输出
        assert "已加载工具 (1)" in result

        # 由于各 section 为 None，不会产生对应的配置行
        assert "LLM 模型" not in result
        assert "TTS:" not in result
        assert "STT:" not in result
        assert "服务:" not in result

    # ---- RuntimeConfigParams ----

    def test_params_default(self) -> None:
        """测试 RuntimeConfigParams 默认值。"""
        params = RuntimeConfigParams()
        assert params.query is None

    def test_params_with_query(self) -> None:
        """测试 RuntimeConfigParams 带 query 参数。"""
        params = RuntimeConfigParams(query="model")
        assert params.query == "model"
