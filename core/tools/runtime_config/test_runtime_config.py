"""RuntimeConfigPlugin 单元测试。"""

import pytest
from unittest.mock import MagicMock

from core.tools.runtime_config.plugin import RuntimeConfigPlugin, RuntimeConfigParams
from Module.tool.protocol import ToolContext

pytestmark = pytest.mark.unit


def _make_plugin_list() -> list[dict]:
    """构建模拟 plugin_list——含文件系统/网络/数据库/普通工具各一个。"""
    return [
        {
            "name": "read_file",
            "description": "读取文件内容",
            "capabilities": ["filesystem:read"],
            "safety": {
                "requires_human_approval": False,
                "max_concurrency": 0,
                "network_allowed": False,
                "filesystem_allowed": True,
                "read_dirs": ["/home/yomu/data/sandbox"],
                "write_dir": "",
                "max_file_bytes": 10_485_760,
                "allow_symlinks": False,
                "max_dir_depth": 3,
                "block_private_ips": True,
                "max_redirects": 5,
                "max_response_bytes": 0,
                "request_timeout": 30.0,
                "database_allowed": False,
            },
            "active": True,
        },
        {
            "name": "write_file",
            "description": "写入/创建文件",
            "capabilities": ["filesystem:write"],
            "safety": {
                "requires_human_approval": True,
                "max_concurrency": 0,
                "network_allowed": False,
                "filesystem_allowed": True,
                "read_dirs": None,
                "write_dir": "data/sandbox/{session_id}",
                "max_file_bytes": 10_485_760,
                "allow_symlinks": False,
                "max_dir_depth": 0,
                "block_private_ips": True,
                "max_redirects": 5,
                "max_response_bytes": 0,
                "request_timeout": 30.0,
                "database_allowed": False,
            },
            "active": True,
        },
        {
            "name": "web_search",
            "description": "搜索网页",
            "capabilities": ["network", "web_search"],
            "safety": {
                "requires_human_approval": False,
                "max_concurrency": 3,
                "network_allowed": True,
                "filesystem_allowed": False,
                "read_dirs": None,
                "write_dir": "",
                "max_file_bytes": 0,
                "allow_symlinks": True,
                "max_dir_depth": 0,
                "block_private_ips": True,
                "max_redirects": 5,
                "max_response_bytes": 5_242_880,
                "request_timeout": 30.0,
                "database_allowed": False,
            },
            "active": True,
        },
        {
            "name": "search_memory",
            "description": "搜索长期记忆",
            "capabilities": ["database:read"],
            "safety": {
                "requires_human_approval": False,
                "max_concurrency": 0,
                "network_allowed": False,
                "filesystem_allowed": False,
                "read_dirs": None,
                "write_dir": "",
                "max_file_bytes": 0,
                "allow_symlinks": True,
                "max_dir_depth": 0,
                "block_private_ips": True,
                "max_redirects": 5,
                "max_response_bytes": 0,
                "request_timeout": 30.0,
                "database_allowed": True,
            },
            "active": True,
        },
    ]


class TestRuntimeConfigPlugin:
    """RuntimeConfigPlugin 的测试用例。"""

    @pytest.fixture
    def plugin(self) -> RuntimeConfigPlugin:
        return RuntimeConfigPlugin()

    @pytest.fixture
    def base_ctx(self) -> ToolContext:
        """创建基础 ToolContext，包含 ToolManager mock + config mock。"""
        tm = MagicMock()
        tm.plugin_list = _make_plugin_list()

        config = MagicMock()
        config.llm.model = "deepseek/deepseek-chat"
        config.tts.enabled = True
        config.tts.model = "gpt-sovits"
        config.stt.enabled = False
        config.server.host = "127.0.0.1"
        config.server.port = 8080

        return ToolContext(
            session_id="test-session",
            user_id="test-user",
            trace_id="test-trace",
            services={
                "config": config,
                "_tool_manager": tm,
                "_started_at": 1000.0,
            },
        )

    # ── 已有测试（适配新输出格式）────────────────────────────

    @pytest.mark.asyncio
    async def test_no_config_service_shows_capability_info(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """当 ctx 中没有 config 时，仍显示工具、文件系统、网络等能力信息。"""
        base_ctx.services.pop("config", None)
        params = RuntimeConfigParams()
        result = await plugin.execute(ctx=base_ctx, params=params)

        # 有 ToolManager → 显示工具列表
        assert "已加载工具" in result
        assert "read_file" in result
        # 不应有模型/TTS/STT（没有 config 服务）
        assert "模型:" not in result
        assert "TTS:" not in result

    @pytest.mark.asyncio
    async def test_no_tool_manager_shows_empty_tools(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """当 ctx 中没有 _tool_manager 时，工具列表为空。"""
        base_ctx.services.pop("_tool_manager", None)
        params = RuntimeConfigParams()
        result = await plugin.execute(ctx=base_ctx, params=params)
        assert "已加载工具 (0)" in result

    @pytest.mark.asyncio
    async def test_full_config_shows_all_sections(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """默认摘要包含模型、文件系统、网络、工具、进程信息。"""
        params = RuntimeConfigParams()
        result = await plugin.execute(ctx=base_ctx, params=params)

        assert "deepseek/deepseek-chat" in result
        assert "TTS: 启用 (gpt-sovits)" in result
        assert "STT: 禁用" in result
        assert "127.0.0.1:8080" in result
        assert "已加载工具 (4)" in result
        assert "read_file" in result
        # 文件系统权限概要
        assert "📁 文件系统" in result
        assert "/home/yomu/data/sandbox" in result
        # 网络权限概要
        assert "🌐 网络" in result

    @pytest.mark.asyncio
    async def test_none_sections_does_not_crash(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """当 config sections 为 None 时，不崩溃。"""
        config = MagicMock()
        config.llm = None
        config.tts = None
        config.stt = None
        config.server = None
        base_ctx.services["config"] = config

        params = RuntimeConfigParams()
        result = await plugin.execute(ctx=base_ctx, params=params)
        # 不崩溃，工具列表正常
        assert "已加载工具 (4)" in result
        assert "模型:" not in result

    @pytest.mark.asyncio
    async def test_no_tool_manager_graceful(
        self, plugin: RuntimeConfigPlugin,
    ) -> None:
        """ctx 无 _tool_manager 时优雅降级，不崩溃。"""
        ctx = ToolContext(
            session_id="test", user_id="test", trace_id="test",
            services={},
        )
        params = RuntimeConfigParams()
        result = await plugin.execute(ctx=ctx, params=params)
        assert "已加载工具 (0)" in result

    # ── 新增测试：query 参数 ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_query_tools_shows_details(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """query='tools' 返回每个工具的 name + description + 能力标签。"""
        params = RuntimeConfigParams(query="tools")
        result = await plugin.execute(ctx=base_ctx, params=params)

        assert "已加载工具 (4)" in result
        assert "read_file" in result
        assert "读取文件内容" in result
        assert "write_file" in result
        assert "写入/创建文件" in result
        assert "🔒审批" in result  # write_file 需要审批
        assert "web_search" in result
        assert "搜索网页" in result
        assert "并发:3" in result

    @pytest.mark.asyncio
    async def test_query_filesystem_shows_dirs(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """query='filesystem' 聚合展示 read_dirs + write_dir。"""
        params = RuntimeConfigParams(query="filesystem")
        result = await plugin.execute(ctx=base_ctx, params=params)

        assert "📁 文件系统权限" in result
        assert "/home/yomu/data/sandbox" in result
        assert "max_depth=3" in result
        assert "禁止符号链接" in result
        # write_dir 模板已解析（session_id=test-session）
        assert "data/sandbox/test-session" in result
        assert "需审批" in result
        # 文件系统工具列表
        assert "read_file" in result
        assert "write_file" in result

    @pytest.mark.asyncio
    async def test_query_network_shows_restrictions(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """query='network' 展示网络限制参数。"""
        params = RuntimeConfigParams(query="network")
        result = await plugin.execute(ctx=base_ctx, params=params)

        assert "🌐 网络权限" in result
        assert "允许出站请求: 是" in result
        assert "私有IP过滤: 启用" in result
        assert "最大重定向: 5" in result
        assert "请求超时: 30" in result
        assert "web_search" in result
        assert "最多3并发" in result

    @pytest.mark.asyncio
    async def test_query_all_returns_all_sections(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """query='all' 包含所有 section。"""
        params = RuntimeConfigParams(query="all")
        result = await plugin.execute(ctx=base_ctx, params=params)

        assert "deepseek/deepseek-chat" in result     # model
        assert "📁 文件系统权限" in result              # filesystem
        assert "🌐 网络权限" in result                  # network
        assert "已加载工具 (4)" in result               # tools detail
        assert "📊 进程" in result                      # process
        # 不应包含 query 提示（那是摘要专用的）
        assert "使用 query 参数查看详情" not in result

    @pytest.mark.asyncio
    async def test_default_summary_has_query_hint(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """无 query 参数时，摘要末尾提示可用 query 值。"""
        params = RuntimeConfigParams()
        result = await plugin.execute(ctx=base_ctx, params=params)

        assert "使用 query 参数查看详情" in result
        assert '"tools"' in result
        assert '"filesystem"' in result
        assert '"network"' in result
        assert '"tasks"' in result
        assert '"all"' in result

    @pytest.mark.asyncio
    async def test_query_tasks_no_task_system(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """query='tasks' 但 TaskSystem 不可用时优雅降级。"""
        base_ctx.services.pop("_task_system", None)
        params = RuntimeConfigParams(query="tasks")
        result = await plugin.execute(ctx=base_ctx, params=params)

        assert "TaskSystem 不可用" in result

    # ── RuntimeConfigParams ──────────────────────────────────────

    def test_params_default(self) -> None:
        """RuntimeConfigParams 默认 query=None。"""
        params = RuntimeConfigParams()
        assert params.query is None

    def test_params_with_query(self) -> None:
        """RuntimeConfigParams 接受 query 参数。"""
        params = RuntimeConfigParams(query="tools")
        assert params.query == "tools"
