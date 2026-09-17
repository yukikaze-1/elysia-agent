"""RuntimeConfigPlugin 单元测试。

Batch 1.6 / F2：模型 / TTS / STT 三行必须读**运行期**权威
（`ToolContext.services["_service_runtime"]` 的零参可调用，由
`core/agent/_lifecycle_tools.py` 注入），不得再读 `config`（bootstrap/YAML）。
见 `TestModelSectionReadsRuntimeAuthority`。
"""

import pytest
from unittest.mock import MagicMock

from core.tools.runtime_config.plugin import RuntimeConfigPlugin, RuntimeConfigParams
from Module.tool.protocol import ToolContext

from core.tools.tests._failure_helpers import assert_failed
from core.schema.service_state import ServiceRuntimeState

pytestmark = pytest.mark.unit


def _runtime_state() -> ServiceRuntimeState:
    """运行期权威快照替身——**故意**与 `base_ctx` 里的 config 值不同。

    `llm_model` 与 config 不同（热切换过）、`tts` 被关掉（set_service）、
    `stt` 后端未连接（`model=None`）。修复前工具报的是 config 那一套。
    """
    return ServiceRuntimeState(
        llm_model="deepseek/deepseek-v4-pro",
        stt_model=None,
        stt_enabled=True,
        tts_model="minimax/speech-2.8-turbo",
        tts_enabled=False,
    )


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


@pytest.fixture
def plugin() -> RuntimeConfigPlugin:
    return RuntimeConfigPlugin()


@pytest.fixture
def base_ctx() -> ToolContext:
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
            # F2：运行期权威（由 Agent 注入的零参可调用）。取值与 config **故意不同**
            # ——修复前插件读 config，于是断言能区分"读了哪个权威"。
            "_service_runtime": _runtime_state,
            "_tool_manager": tm,
            "_started_at": 1000.0,
        },
    )



class TestRuntimeConfigPlugin:
    """RuntimeConfigPlugin 的测试用例。"""

    # ── 已有测试（适配新输出格式）────────────────────────────

    @pytest.mark.asyncio
    async def test_no_config_service_shows_capability_info(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """当 ctx 中没有 config 时，仍显示工具、文件系统、网络等能力信息。

        F2：模型行来自**运行期**权威，所以这里要连 `_service_runtime` 一起去掉，
        才能真正断言"两个权威都不可达 → 不输出模型行"。
        """
        base_ctx.services.pop("config", None)
        base_ctx.services.pop("_service_runtime", None)
        params = RuntimeConfigParams()
        result = await plugin.execute(ctx=base_ctx, params=params)

        # 有 ToolManager → 显示工具列表
        assert "已加载工具" in result
        assert "read_file" in result
        # 两个权威都不可达 → 不应有模型/TTS/STT/服务地址
        assert "模型:" not in result
        assert "TTS:" not in result
        assert "服务地址:" not in result

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

        # F2：模型/TTS/STT 三行报的是**运行期**权威，不是 config 里的 YAML 值
        assert "deepseek/deepseek-v4-pro" in result
        assert "deepseek/deepseek-chat" not in result
        assert "TTS: 禁用 (minimax/speech-2.8-turbo)" in result
        assert "STT: 启用" in result
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
        # F2：模型行来自**运行期**权威（与 config section 是否为 None 无关），
        # 而服务地址行确实依赖 config.server → 它缺席
        assert "模型: deepseek/deepseek-v4-pro" in result
        assert "服务地址:" not in result

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

        assert "deepseek/deepseek-v4-pro" in result    # model（运行期权威）
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
    async def test_query_tasks_no_workflow_engine(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """query='tasks' 但 WorkflowEngine 不可用时优雅降级。"""
        base_ctx.services.pop("_task_system", None)
        params = RuntimeConfigParams(query="tasks")
        result = await plugin.execute(ctx=base_ctx, params=params)

        assert_failed(result, contains="WorkflowEngine 不可用")

    # ── RuntimeConfigParams ──────────────────────────────────────

    def test_params_default(self) -> None:
        """RuntimeConfigParams 默认 query=None。"""
        params = RuntimeConfigParams()
        assert params.query is None

    def test_params_with_query(self) -> None:
        """RuntimeConfigParams 接受 query 参数。"""
        params = RuntimeConfigParams(query="tools")
        assert params.query == "tools"


# ═══════════════════════════════════════════════════════════════════════════════
# F2（Batch 1.6）：模型 / TTS / STT 三行读**运行期**权威
# ═══════════════════════════════════════════════════════════════════════════════


class TestModelSectionReadsRuntimeAuthority:
    """名字叫 runtime 的工具，只能报当前实际运行状态（Batch 1.5 的 F2）。

    缺陷实测（`docs/review/2026-09-16-Batch1.5-*` §2 F2）：热切换 LLM/STT/TTS 与
    `set_service` 之后，本工具的模型行**逐字节不变**——它读的是 `ctx["config"]`
    （bootstrap/YAML），而同一时刻 banner / capabilities / debug 都报运行期值。
    冷启动时两者相等，所以这个谎只有在切换之后才看得见。
    """

    @pytest.mark.asyncio
    async def test_reports_runtime_values_not_config_defaults(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """运行期与 config 不同 → 报运行期的那一套。"""
        result = await plugin.execute(ctx=base_ctx, params=RuntimeConfigParams())

        assert "deepseek/deepseek-v4-pro" in result
        assert "deepseek/deepseek-chat" not in result, "不得把 YAML 模型报成当前模型"
        assert "minimax/speech-2.8-turbo" in result
        assert "gpt-sovits" not in result, "不得把 YAML TTS 模型报成当前模型"

    @pytest.mark.asyncio
    async def test_reports_runtime_enabled_not_config_enabled(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """开关报**派生**值（session 三态 + config 回退的唯一推导点），不是 config。"""
        result = await plugin.execute(ctx=base_ctx, params=RuntimeConfigParams())

        # config: tts.enabled=True / stt.enabled=False；runtime: tts=False / stt=True
        assert "TTS: 禁用" in result
        assert "STT: 启用" in result

    @pytest.mark.asyncio
    async def test_unconnected_backend_is_reported_as_such(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """后端未连接（model=None）→ 明说"未连接"，而不是拿 YAML 模型顶上。"""
        result = await plugin.execute(ctx=base_ctx, params=RuntimeConfigParams())

        assert "STT: 启用 (未连接)" in result
        assert "sensevoice" not in result

    @pytest.mark.asyncio
    async def test_runtime_provider_missing_omits_the_model_section(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """没有运行期句柄时**不输出**模型行——绝不用 config 顶上。

        这是"名字叫 runtime 的接口不得混入 bootstrap 值"的直接可执行形式：
        拿不到运行期真相时，工具什么都不说（而不是说一个错值）。
        """
        base_ctx.services.pop("_service_runtime", None)
        result = await plugin.execute(ctx=base_ctx, params=RuntimeConfigParams())

        assert "模型:" not in result
        assert "TTS:" not in result
        assert "deepseek/deepseek-chat" not in result
        assert "gpt-sovits" not in result
        # 其余 section 照常
        assert "已加载工具 (4)" in result

    @pytest.mark.asyncio
    async def test_runtime_provider_is_resolved_per_call(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """每次调用现取（可调用）——启动期写死的快照跨不过 service_state/tts 两步。"""
        from dataclasses import replace

        seen = iter([
            _runtime_state(),
            replace(_runtime_state(), llm_model="deepseek/deepseek-v4-flash"),
        ])
        base_ctx.services["_service_runtime"] = lambda: next(seen)

        first = await plugin.execute(ctx=base_ctx, params=RuntimeConfigParams())
        second = await plugin.execute(ctx=base_ctx, params=RuntimeConfigParams())

        assert "deepseek/deepseek-v4-pro" in first
        assert "deepseek/deepseek-v4-flash" in second

    @pytest.mark.asyncio
    async def test_config_only_section_still_works_without_runtime(
        self, plugin: RuntimeConfigPlugin, base_ctx: ToolContext,
    ) -> None:
        """服务地址来自 config（= 实际绑定目标，F4 之后含 CLI 显式覆盖），不受影响。"""
        base_ctx.services.pop("_service_runtime", None)
        result = await plugin.execute(ctx=base_ctx, params=RuntimeConfigParams())

        assert "服务地址: 127.0.0.1:8080" in result
