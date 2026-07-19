"""框架层单元测试 — ErrorType, Capability, SafetyProfile, SafetyPipeline, ToolManager, Plugins。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel, Field

from Module.tool.capability import Capability
from Module.tool.protocol import (
    ErrorType, PluginContext, ToolContext, ToolPlugin, ToolResult,
)
from Module.tool.safety import (
    SafetyPipeline, SafetyProfile, _estimate_tokens, _format_validation_error,
    _truncate_text, _validate_url,
)
from Module.tool.manager import ToolManager


# ============================================================================
# ErrorType
# ============================================================================

@pytest.mark.unit
class TestErrorType:
    def test_all_values_present(self):
        expected = {
            "invalid_argument", "timeout", "permission_denied",
            "rate_limited", "network_error", "unavailable",
            "cancelled", "tool_error", "unknown_tool",
        }
        assert {e.value for e in ErrorType} == expected

    def test_is_retryable_true(self):
        assert ErrorType.TIMEOUT.is_retryable is True
        assert ErrorType.RATE_LIMITED.is_retryable is True
        assert ErrorType.NETWORK_ERROR.is_retryable is True

    def test_is_retryable_false(self):
        assert ErrorType.INVALID_ARGUMENT.is_retryable is False
        assert ErrorType.PERMISSION_DENIED.is_retryable is False
        assert ErrorType.UNAVAILABLE.is_retryable is False
        assert ErrorType.CANCELLED.is_retryable is False
        assert ErrorType.TOOL_ERROR.is_retryable is False
        assert ErrorType.UNKNOWN_TOOL.is_retryable is False


# ============================================================================
# Capability
# ============================================================================

@pytest.mark.unit
class TestCapability:
    def test_values(self):
        assert Capability.NETWORK.value == "network"
        assert Capability.DATABASE_READ.value == "database:read"
        assert Capability.FILESYSTEM_WRITE.value == "filesystem:write"
        assert Capability.SCHEDULE.value == "schedule"

    def test_uniqueness(self):
        values = [c.value for c in Capability]
        assert len(values) == len(set(values))

    def test_count(self):
        assert len(Capability) == 8


# ============================================================================
# ToolContext
# ============================================================================

@pytest.mark.unit
class TestToolContext:
    def test_defaults(self):
        tc = ToolContext()
        assert tc.session_id == ""
        assert tc.user_id == ""
        assert tc.trace_id == ""
        assert tc.agent_id == "default"
        assert tc.services == {}

    def test_trace_and_agent(self):
        tc = ToolContext(
            session_id="s1", trace_id="abc123", agent_id="agent-7",
        )
        assert tc.trace_id == "abc123"
        assert tc.agent_id == "agent-7"

    def test_get_service(self):
        tc = ToolContext(services={"memory": "mock_memory"})
        assert tc.get("memory") == "mock_memory"
        assert tc.get("missing", "fallback") == "fallback"


# ============================================================================
# PluginContext
# ============================================================================

@pytest.mark.unit
class TestPluginContext:
    def test_defaults(self):
        pc = PluginContext()
        assert pc.session_id == ""
        assert pc.memory is None
        assert pc.mongodb is None
        assert pc.milvus is None
        assert pc.http_client is None
        assert pc.scheduler is None
        assert pc.config is None

    def test_with_deps(self):
        pc = PluginContext(
            session_id="test",
            memory="mock_memory",
            config="mock_config",
        )
        assert pc.session_id == "test"
        assert pc.memory == "mock_memory"
        assert pc.config == "mock_config"


# ============================================================================
# ToolPlugin
# ============================================================================

@pytest.mark.unit
class TestToolPlugin:
    def test_base_class_defaults(self):
        """未配置的子类应有合理的默认值。"""

        class BarePlugin(ToolPlugin):
            name = "bare"
            description = "minimal"
            param_model = None

        p = BarePlugin()
        assert p.name == "bare"
        assert p.capabilities == set()
        assert p.safety is None  # 子类未设置
        assert p.param_model is None

    def test_safety_profile_set(self):
        """子类设置 SafetyProfile 后 safety 属性可用。"""

        class SafePlugin(ToolPlugin):
            name = "safe"
            description = "with safety"
            param_model = None
            safety = SafetyProfile(network_allowed=True, max_concurrency=5)

        p = SafePlugin()
        assert p.safety.network_allowed is True
        assert p.safety.max_concurrency == 5

    def test_capabilities_set(self):
        """Capability set 正确存储。"""

        class CapPlugin(ToolPlugin):
            name = "cap"
            description = "with caps"
            param_model = None
            capabilities = {Capability.NETWORK, Capability.FILESYSTEM_READ}

        p = CapPlugin()
        assert Capability.NETWORK in p.capabilities
        assert Capability.FILESYSTEM_READ in p.capabilities

    def test_setup_noop(self):
        """默认 setup() 为空操作。"""

        class P(ToolPlugin):
            name = "p"
            description = "d"
            param_model = None

        p = P()
        import asyncio
        asyncio.run(p.setup(PluginContext()))

    def test_teardown_noop(self):
        """默认 teardown() 为空操作。"""

        class P(ToolPlugin):
            name = "p"
            description = "d"
            param_model = None

        p = P()
        asyncio.run(p.teardown())

    def test_execute_raises_not_implemented(self):
        class P(ToolPlugin):
            name = "p"
            description = "d"
            param_model = None

        p = P()
        with pytest.raises(NotImplementedError):
            asyncio.run(p.execute(ctx=ToolContext(), params=None))


# ============================================================================
# SafetyProfile
# ============================================================================

@pytest.mark.unit
class TestSafetyProfile:
    def test_defaults(self):
        sp = SafetyProfile()
        assert sp.block_private_ips is True
        assert sp.max_concurrency == 0
        assert sp.max_result_tokens == 2000
        assert sp.requires_human_approval is False
        assert sp.allow_symlinks is False
        assert sp.database_readonly is True

    def test_network_profile(self):
        sp = SafetyProfile(
            network_allowed=True,
            max_concurrency=3,
            block_private_ips=False,
            max_response_bytes=1024,
        )
        assert sp.network_allowed is True
        assert sp.max_concurrency == 3
        assert sp.block_private_ips is False
        assert sp.max_response_bytes == 1024

    def test_filesystem_profile(self):
        sp = SafetyProfile(
            filesystem_allowed=True,
            read_dirs=["data/sandbox", "/tmp"],
            write_dir="data/out/{session_id}",
            max_file_bytes=1024,
            allow_symlinks=True,
            max_dir_depth=5,
        )
        assert sp.filesystem_allowed is True
        assert sp.read_dirs == ["data/sandbox", "/tmp"]
        assert sp.write_dir == "data/out/{session_id}"
        assert sp.max_file_bytes == 1024
        assert sp.allow_symlinks is True
        assert sp.max_dir_depth == 5

    def test_database_profile(self):
        sp = SafetyProfile(
            database_allowed=True,
            database_readonly=False,
        )
        assert sp.database_allowed is True
        assert sp.database_readonly is False

    def test_hitl_profile(self):
        sp = SafetyProfile(requires_human_approval=True)
        assert sp.requires_human_approval is True

    def test_truncate_settings(self):
        sp = SafetyProfile(
            max_result_tokens=500,
            truncate_strategy="head_only",
        )
        assert sp.max_result_tokens == 500
        assert sp.truncate_strategy == "head_only"


# ============================================================================
# Token / URL 辅助函数
# ============================================================================

@pytest.mark.unit
class TestTokenEstimation:
    def test_english(self):
        text = "Hello world this is a test sentence"
        tokens = _estimate_tokens(text)
        assert 5 < tokens < 15

    def test_chinese(self):
        text = "这是一段中文测试文本"
        tokens = _estimate_tokens(text)
        assert 8 <= tokens <= 12

    def test_mixed(self):
        text = "Hello 世界 test 测试"
        tokens = _estimate_tokens(text)
        assert tokens > 0

    def test_empty(self):
        assert _estimate_tokens("") == 0


@pytest.mark.unit
class TestTokenTruncation:
    def test_no_truncation_needed(self):
        text = "short"
        result, truncated = _truncate_text(text, 1000, "head_only")
        assert truncated is False
        assert result == "short"

    def test_head_only_truncation(self):
        text = "x" * 10000
        result, truncated = _truncate_text(text, 50, "head_only")
        assert truncated is True
        assert "...[截断]" in result
        assert len(result) < len(text)

    def test_head_tail_truncation(self):
        text = "ABCDEFGHIJ" * 1000  # 约10000字符，约2500 token
        result, truncated = _truncate_text(text, 500, "head_tail")
        assert truncated is True
        assert "...[省略约" in result


@pytest.mark.unit
class TestURLValidation:
    def test_invalid_scheme(self):
        err = _validate_url("ftp://example.com", SafetyProfile())
        assert err and "scheme" in err

    def test_private_ip_blocked(self):
        err = _validate_url("http://127.0.0.1/admin", SafetyProfile())
        assert err and "内网地址" in err

    def test_private_ip_allowed_when_disabled(self):
        err = _validate_url(
            "http://10.0.0.1/admin",
            SafetyProfile(block_private_ips=False),
        )
        assert err is None

    def test_public_url_ok(self):
        err = _validate_url(
            "https://example.com/page",
            SafetyProfile(),
        )
        assert err is None


# ============================================================================
# SafetyPipeline
# ============================================================================

@pytest.mark.unit
class TestSafetyPipeline:
    def test_initial_stats(self):
        sp = SafetyPipeline()
        assert sp.stats == {
            "total": 0, "timeout": 0, "denied": 0, "rate_limited": 0,
        }

    def test_hitl_request_lifecycle(self):
        sp = SafetyPipeline()
        rid = "test-request-1"
        # 请求不存在
        assert sp.request_exists(rid) is False
        # 提交审批 — 但请求未创建（内部机制），应返回 False
        assert sp.submit_approval(rid, True) is False


# ============================================================================
# ToolManager — 基础操作
# ============================================================================

@pytest.mark.service
class TestToolManagerEmpty:
    """空 ToolManager 测试。"""

    def test_plugin_list_empty(self):
        tm = ToolManager()
        assert len(tm.plugin_list) == 0

    def test_capability_status_empty(self):
        tm = ToolManager()
        assert tm.capability_status == {}

    def test_tool_names_empty(self):
        tm = ToolManager()
        assert tm.tool_names == []

    def test_executing_now_empty(self):
        tm = ToolManager()
        assert tm.executing_now == []

    def test_history_empty(self):
        tm = ToolManager()
        assert tm.history == []

    def test_pipeline_stats(self):
        tm = ToolManager()
        assert tm.pipeline_stats["total"] == 0

    def test_unknown_tool(self):
        tm = ToolManager()
        result = asyncio.run(
            tm.execute("nonexistent", ctx=ToolContext()),
        )
        assert result.success is False
        assert result.error_type == ErrorType.UNKNOWN_TOOL
        assert result.error and "未激活" in result.error

    @pytest.mark.asyncio
    async def test_unknown_tool_stream(self):
        tm = ToolManager()
        chunks = []
        async for c in tm.execute_stream("nonexistent", ctx=ToolContext()):
            chunks.append(c)
        assert len(chunks) == 1
        assert chunks[0].error_type == ErrorType.UNKNOWN_TOOL

    def test_len_and_contains(self):
        tm = ToolManager()
        assert len(tm) == 0
        assert ("anything" in tm) is False

    def test_repr(self):
        tm = ToolManager()
        assert "ToolManager" in repr(tm)


@pytest.mark.service
class TestToolManagerLoad:
    """ToolManager load_all / shutdown 测试。"""

    @pytest.mark.asyncio
    async def test_load_empty(self):
        tm = ToolManager()
        await tm.load_all([], PluginContext(session_id="test"))
        assert len(tm.plugin_list) == 0
        await tm.shutdown()

    @pytest.mark.asyncio
    async def test_load_failing_plugin(self):
        """setup() 失败的插件应被跳过，不影响其他插件。"""

        class BadPlugin(ToolPlugin):
            name = "bad"
            description = "fails setup"
            param_model = None

            async def setup(self, ctx):
                raise RuntimeError("intentional failure")

        class GoodPlugin(ToolPlugin):
            name = "good"
            description = "works"
            param_model = None

        tm = ToolManager()
        await tm.load_all(
            [BadPlugin, GoodPlugin],
            PluginContext(session_id="test"),
        )
        # bad 被跳过，good 被加载
        assert "bad" not in tm.tool_names
        assert "good" in tm.tool_names
        await tm.shutdown()


# ============================================================================
# ToolManager — 插件执行
# ============================================================================

class GreetParams(BaseModel):
    target: str = Field(..., description="要问候的名字")


class GreetPlugin(ToolPlugin):
    name = "greet"
    description = "打招呼"
    param_model = GreetParams
    capabilities = set()
    safety = SafetyProfile()

    async def execute(self, *, ctx, emit=None, params: GreetParams) -> str:
        return f"你好, {params.target}!"


class FailingPlugin(ToolPlugin):
    name = "failing"
    description = "always fails"
    param_model = None

    async def execute(self, *, ctx, emit=None, params=None) -> str:
        raise ValueError("boom")


@pytest.mark.service
class TestToolManagerExecute:
    """ToolManager execute() 测试。"""

    @pytest.mark.asyncio
    async def test_successful_execute(self):
        tm = ToolManager()
        await tm.load_all([GreetPlugin], PluginContext(session_id="test"))
        result = await tm.execute("greet", ctx=ToolContext(), target="世界")
        assert result.success is True
        assert result.content == "你好, 世界!"
        await tm.shutdown()

    @pytest.mark.asyncio
    async def test_execute_updates_history(self):
        tm = ToolManager()
        await tm.load_all([GreetPlugin], PluginContext(session_id="test"))
        await tm.execute("greet", ctx=ToolContext(), target="test")
        assert len(tm.history) == 1
        assert tm.history[0]["name"] == "greet"
        assert tm.history[0]["success"] is True
        await tm.shutdown()

    @pytest.mark.asyncio
    async def test_pydantic_validation_error(self):
        """缺少必填参数 target 应返回 INVALID_ARGUMENT。"""
        tm = ToolManager()
        await tm.load_all([GreetPlugin], PluginContext(session_id="test"))
        result = await tm.execute("greet", ctx=ToolContext())  # 没传 target
        assert result.success is False
        assert result.error_type == ErrorType.INVALID_ARGUMENT
        assert result.error and "target" in result.error.lower()
        await tm.shutdown()

    @pytest.mark.asyncio
    async def test_plugin_error(self):
        tm = ToolManager()
        await tm.load_all([FailingPlugin], PluginContext(session_id="test"))
        result = await tm.execute("failing", ctx=ToolContext())
        assert result.success is False
        assert result.error_type == ErrorType.TOOL_ERROR
        assert result.error and "boom" in result.error
        await tm.shutdown()

    @pytest.mark.asyncio
    async def test_executing_now_tracks(self):
        """执行中应被 tracking。"""
        tm = ToolManager()
        await tm.load_all([GreetPlugin], PluginContext(session_id="test"))
        # 执行前为空
        assert tm.executing_now == []
        await tm.execute("greet", ctx=ToolContext(), target="test")
        # 执行后应清空
        assert tm.executing_now == []
        await tm.shutdown()


# ============================================================================
# ToolManager — Schema 导出
# ============================================================================

class NameParams(BaseModel):
    """测试参数模型。"""
    name: str = Field(..., description="名字")


class SchemaPlugin(ToolPlugin):
    name = "schema_test"
    description = "测试 schema 导出"
    param_model = NameParams
    capabilities = set()
    safety = SafetyProfile()


@pytest.mark.service
class TestToolManagerDefinitions:
    """ToolManager definitions() 测试。"""

    @pytest.mark.asyncio
    async def test_definitions_format(self):
        tm = ToolManager()
        await tm.load_all([SchemaPlugin], PluginContext(session_id="test"))
        defs = tm.definitions()
        assert len(defs) == 1
        assert defs[0]["type"] == "function"
        func = defs[0]["function"]
        assert func["name"] == "schema_test"
        assert "description" in func
        assert "parameters" in func
        # Pydantic 模型生成的 schema
        params = func["parameters"]
        assert "properties" in params
        assert "name" in params["properties"]
        await tm.shutdown()

    @pytest.mark.asyncio
    async def test_empty_definitions(self):
        tm = ToolManager()
        assert tm.definitions() == []

    @pytest.mark.asyncio
    async def test_no_param_model(self):
        """无 param_model 的插件应有空 properties schema。"""

        class NoParamPlugin(ToolPlugin):
            name = "no_param"
            description = "no params"
            param_model = None
            capabilities = set()
            safety = SafetyProfile()

        tm = ToolManager()
        await tm.load_all([NoParamPlugin], PluginContext(session_id="test"))
        defs = tm.definitions()
        assert len(defs) == 1
        params = defs[0]["function"]["parameters"]
        assert params == {"type": "object", "properties": {}}
        await tm.shutdown()


# ============================================================================
# ToolManager — 能力状态
# ============================================================================

@pytest.mark.service
class TestToolManagerCapabilityStatus:
    """capability_status 测试。"""

    @pytest.mark.asyncio
    async def test_capability_status_reflects_plugins(self):
        class NetPlugin(ToolPlugin):
            name = "net"
            description = "with network"
            param_model = None
            capabilities = {Capability.NETWORK, Capability.WEB_SEARCH}
            safety = SafetyProfile()

        tm = ToolManager()
        await tm.load_all([NetPlugin], PluginContext(session_id="test"))
        caps = tm.capability_status
        assert caps["network"] is True
        assert caps["web_search"] is True
        await tm.shutdown()


# ============================================================================
# ToolManager — HITL 审批
# ============================================================================

@pytest.mark.service
class TestToolManagerHITL:
    """HITL 审批测试。"""

    @pytest.mark.asyncio
    async def test_hitl_plugin_triggers_approval(self):
        """requires_human_approval=True 的插件应触发审批并等待确认。
        当前审批无前端响应时超时默认拒绝。"""

        class HITLPlugin(ToolPlugin):
            name = "hitl_test"
            description = "needs approval"
            param_model = None
            capabilities = set()
            safety = SafetyProfile(requires_human_approval=True)

            async def execute(self, *, ctx, emit=None, params=None) -> str:
                return "approved action done"

        tm = ToolManager()
        # 将 HITL 超时调短以加速测试
        tm._pipeline._hitl_timeout = 0.1
        await tm.load_all([HITLPlugin], PluginContext(session_id="test"))
        result = await tm.execute("hitl_test", ctx=ToolContext())
        # 无人确认 → 超时拒绝
        assert result.success is False
        assert result.error_type == ErrorType.PERMISSION_DENIED
        await tm.shutdown()

    def test_submit_approval_unknown_request(self):
        """对不存在的 request_id 提交审批应返回 False。"""
        pipeline = SafetyPipeline()
        assert pipeline.submit_approval("nonexistent", True) is False

    def test_request_exists_false(self):
        pipeline = SafetyPipeline()
        assert pipeline.request_exists("random") is False


# ============================================================================
# ToolManager — 流式执行
# ============================================================================

class StreamParams(BaseModel):
    steps: int = Field(default=3, ge=1, le=10)


class StreamPlugin(ToolPlugin):
    name = "streamer"
    description = "streams progress"
    param_model = StreamParams
    capabilities = set()
    safety = SafetyProfile()

    async def execute_stream(self, *, ctx, emit, params: StreamParams):
        for i in range(params.steps):
            from Module.tool.protocol import ToolProgress
            yield ToolProgress(message=f"step {i+1}", percent=(i + 1) / params.steps)
        yield ToolResult.ok(name=self.name, content="all done", duration_ms=0)


@pytest.mark.service
class TestToolManagerStream:
    """ToolManager execute_stream() 测试。"""

    @pytest.mark.asyncio
    async def test_stream_yields_progress_and_result(self):
        tm = ToolManager()
        await tm.load_all([StreamPlugin], PluginContext(session_id="test"))
        chunks = []
        async for c in tm.execute_stream(
            "streamer", ctx=ToolContext(), steps=2,
        ):
            chunks.append(c)
        # 应有 2 个进度事件 + 1 个结果
        assert len(chunks) >= 2
        final = chunks[-1]
        assert isinstance(final, ToolResult)
        assert final.content == "all done"
        await tm.shutdown()


# ============================================================================
# Pydantic 格式化
# ============================================================================

@pytest.mark.unit
class TestPydanticErrorFormatting:
    def test_format_simple_error(self):
        from pydantic import ValidationError
        try:
            GreetParams()  # type: ignore[call-arg]  # 缺少必填字段 → 运行时 ValidationError
        except ValidationError as e:
            formatted = _format_validation_error(e)
            assert "target" in formatted.lower()
            assert "必填" in formatted or "required" in formatted.lower()
            assert len(formatted) <= 200


# ============================================================================
# ToolParameter — 参数定义与 JSON Schema 导出
# ============================================================================

@pytest.mark.unit
class TestToolParameter:
    """ToolParameter 数据类与 to_json_schema_property() 测试。"""

    def test_basic_string_param(self):
        """基本 string 类型参数 — type, description, required。"""
        from Module.tool.protocol import ToolParameter
        param = ToolParameter(type="string", description="用户名", required=True)
        assert param.type == "string"
        assert param.description == "用户名"
        assert param.required is True

    def test_to_json_schema_property_string(self):
        """string 类型输出 {"type": "string", "description": "..."}。"""
        from Module.tool.protocol import ToolParameter
        param = ToolParameter(type="string", description="用户名")
        schema = param.to_json_schema_property()
        assert schema == {"type": "string", "description": "用户名"}

    def test_to_json_schema_property_with_enum(self):
        """enum 参数应包含 "enum" key。"""
        from Module.tool.protocol import ToolParameter
        param = ToolParameter(type="string", description="颜色", enum=["red", "green", "blue"])
        schema = param.to_json_schema_property()
        assert schema["type"] == "string"
        assert schema["enum"] == ["red", "green", "blue"]

    def test_to_json_schema_property_with_default_set(self):
        """default_set=True 时应包含 "default" key。"""
        from Module.tool.protocol import ToolParameter
        param = ToolParameter(type="string", description="可选字段", default=None, default_set=True)
        schema = param.to_json_schema_property()
        assert "default" in schema
        assert schema["default"] is None

    def test_to_json_schema_property_with_min_max(self):
        """minimum / maximum 应出现在 schema 中。"""
        from Module.tool.protocol import ToolParameter
        param = ToolParameter(type="integer", description="年龄", minimum=0, maximum=150)
        schema = param.to_json_schema_property()
        assert schema["minimum"] == 0
        assert schema["maximum"] == 150

    def test_to_json_schema_property_with_pattern(self):
        """pattern 应出现在 schema 中。"""
        from Module.tool.protocol import ToolParameter
        param = ToolParameter(type="string", description="邮箱", pattern=r"^.+@.+\..+$")
        schema = param.to_json_schema_property()
        assert schema["pattern"] == r"^.+@.+\..+$"

    def test_to_json_schema_property_object_with_properties(self):
        """object 类型 + 嵌套 properties → 输出带 properties 和 required 的 schema。"""
        from Module.tool.protocol import ToolParameter
        name_param = ToolParameter(type="string", description="姓名", required=True)
        age_param = ToolParameter(type="integer", description="年龄", required=False)
        param = ToolParameter(
            type="object",
            description="用户信息",
            properties={"name": name_param, "age": age_param},
        )
        schema = param.to_json_schema_property()
        assert schema["type"] == "object"
        assert "properties" in schema
        assert "name" in schema["properties"]
        assert "age" in schema["properties"]
        assert schema["required"] == ["name"]

    def test_to_json_schema_property_array_with_items(self):
        """array 类型 + items → 输出带 items 的 schema。"""
        from Module.tool.protocol import ToolParameter
        item_param = ToolParameter(type="string", description="标签")
        param = ToolParameter(type="array", description="标签列表", items=item_param)
        schema = param.to_json_schema_property()
        assert schema["type"] == "array"
        assert "items" in schema
        assert schema["items"]["type"] == "string"


# ============================================================================
# MediaItem — 媒体数据
# ============================================================================

@pytest.mark.unit
class TestMediaItem:
    """MediaItem 数据类测试。"""

    def test_create_with_url(self):
        """通过 URL 创建 MediaItem。"""
        from Module.tool.protocol import MediaItem
        item = MediaItem(url="https://example.com/img.png", mime_type="image/png", label="示例图片")
        assert item.url == "https://example.com/img.png"
        assert item.mime_type == "image/png"
        assert item.label == "示例图片"
        assert item.data == b""

    def test_create_with_data(self):
        """通过 bytes 数据创建 MediaItem。"""
        from Module.tool.protocol import MediaItem
        raw = b"\x89PNG fake image data"
        item = MediaItem(data=raw, mime_type="image/png", label="截图")
        assert item.data == raw
        assert item.mime_type == "image/png"
        assert item.label == "截图"
        assert item.url == ""


# ============================================================================
# ToolEmitter — 进度上报与取消检测
# ============================================================================

@pytest.mark.service
class TestToolEmitter:
    """ToolEmitter 进度上报与 is_cancelled() 测试。"""

    @pytest.mark.asyncio
    async def test_progress_noop_when_queue_none(self):
        """_queue 为 None 时 progress() 是空操作（不抛出异常）。"""
        from Module.tool.protocol import ToolEmitter
        emit = ToolEmitter()
        # 不应抛出异常
        await emit.progress(0.5, "testing")

    @pytest.mark.asyncio
    async def test_progress_puts_into_queue(self):
        """_queue 已设置时 progress() 应将 ToolProgress 放入队列。"""
        from Module.tool.protocol import ToolEmitter, ToolProgress
        queue = asyncio.Queue()
        emit = ToolEmitter(_queue=queue)
        await emit.progress(0.75, "几乎完成", data={"step": 3})
        item = queue.get_nowait()
        assert isinstance(item, ToolProgress)
        assert item.percent == 0.75
        assert item.message == "几乎完成"
        assert item.data == {"step": 3}

    def test_is_cancelled_false_when_no_task(self):
        """没有关联 task 时 is_cancelled() 返回 False。"""
        from Module.tool.protocol import ToolEmitter
        emit = ToolEmitter()
        assert emit.is_cancelled() is False

    @pytest.mark.asyncio
    async def test_is_cancelled_false_when_task_not_cancelled(self):
        """task 未取消时 is_cancelled() 返回 False。"""
        from Module.tool.protocol import ToolEmitter

        async def dummy():
            pass

        task = asyncio.ensure_future(dummy())
        try:
            emit = ToolEmitter(_task=task)
            assert emit.is_cancelled() is False
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_is_cancelled_true_when_task_cancelled(self):
        """task 已取消时 is_cancelled() 返回 True。"""
        from Module.tool.protocol import ToolEmitter

        async def dummy():
            await asyncio.sleep(10)

        task = asyncio.ensure_future(dummy())
        emit = ToolEmitter(_task=task)
        assert emit.is_cancelled() is False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert emit.is_cancelled() is True


# ============================================================================
# ToolResult.llm_content — LLM 上下文生成
# ============================================================================

@pytest.mark.unit
class TestToolResultExtended:
    """ToolResult.llm_content 属性测试。"""

    def test_ok_text_content(self):
        """成功文本结果 — llm_content 直接返回 content。"""
        result = ToolResult.ok("tool", "hello", 0)
        assert result.llm_content == "hello"

    def test_ok_truncated_content(self):
        """截断结果应包含截断提示中文信息。"""
        result = ToolResult.ok("tool", "hello", 0, truncated=True)
        content = result.llm_content
        assert "结果已截断" in content
        assert "hello" in content

    def test_ok_image_result_with_llm_summary(self):
        """image 结果使用 llm_summary 作为 llm_content。"""
        from Module.tool.protocol import MediaItem
        item = MediaItem(url="https://example.com/chart.png", mime_type="image/png", label="图表")
        result = ToolResult.ok(
            "chart_tool", "raw content", 500.0,
            result_type="image",
            llm_summary="图表显示 Q1 营收增长 30%",
            images=[item],
        )
        assert "图表显示 Q1 营收增长 30%" in result.llm_content

    def test_ok_image_result_fallback_to_content(self):
        """image 结果无 llm_summary 时 fallback 到 content。"""
        from Module.tool.protocol import MediaItem
        item = MediaItem(url="https://example.com/chart.png", mime_type="image/png")
        result = ToolResult.ok(
            "chart_tool", "fallback content", 500.0,
            result_type="image",
            images=[item],
        )
        assert result.llm_content == "fallback content"

    def test_fail_invalid_argument(self):
        """INVALID_ARGUMENT 错误 — llm_content 包含 invalid arguments 提示。"""
        result = ToolResult.fail("tool", "bad arg", error_type=ErrorType.INVALID_ARGUMENT)
        content = result.llm_content
        assert "invalid arguments" in content.lower()
        assert "bad arg" in content

    def test_fail_timeout(self):
        """TIMEOUT 错误 — llm_content 包含 timed out 提示。"""
        result = ToolResult.fail("tool", "took too long", error_type=ErrorType.TIMEOUT)
        content = result.llm_content
        assert "timed out" in content.lower()
        assert "took too long" in content

    def test_fail_unavailable(self):
        """UNAVAILABLE 错误 — llm_content 包含 unavailable 提示。"""
        result = ToolResult.fail("tool", "service down", error_type=ErrorType.UNAVAILABLE)
        content = result.llm_content
        assert "unavailable" in content.lower()
        assert "service down" in content

    def test_fail_no_error_type(self):
        """无显式 error_type — llm_content 包含 failed 提示。"""
        result = ToolResult.fail("tool", "something broke")
        content = result.llm_content
        assert "failed" in content.lower()
        assert "something broke" in content


# ============================================================
# ToolManager — definitions (anthropic/gemini/invalid)
# ============================================================

@pytest.mark.service
class TestToolManagerDefinitionsExtended:
    @pytest.mark.asyncio
    async def test_definitions_anthropic_format(self):
        tm = ToolManager()
        await tm.load_all([], PluginContext())
        result = tm.definitions("anthropic")
        assert result == []

    @pytest.mark.asyncio
    async def test_definitions_gemini_format(self):
        tm = ToolManager()
        await tm.load_all([], PluginContext())
        result = tm.definitions("gemini")
        assert result == []

    def test_definitions_invalid_format_raises(self):
        tm = ToolManager()
        with pytest.raises(ValueError, match="不支持的格式"):
            tm.definitions("invalid")


# ============================================================
# ToolManager — cancel_all / executing_now / history
# ============================================================

@pytest.mark.service
class TestToolManagerControl:
    @pytest.mark.asyncio
    async def test_cancel_all_clears_executing(self):
        tm = ToolManager()
        t1 = MagicMock(spec=asyncio.Task)
        t1.done.return_value = False
        t2 = MagicMock(spec=asyncio.Task)
        t2.done.return_value = False
        tm._executing.add("tool_a")
        tm._executing.add("tool_b")
        tm._tasks["tool_a"] = t1
        tm._tasks["tool_b"] = t2
        await tm.cancel_all()
        assert len(tm._executing) == 0
        assert len(tm._tasks) == 0

    @pytest.mark.asyncio
    async def test_executing_now_tracks_active_tools(self):
        tm = ToolManager()
        tm._executing = {"search", "read"}
        assert set(tm.executing_now) == {"search", "read"}

    def test_history_maxlen_enforced(self):
        tm = ToolManager()
        for i in range(55):
            tm._history.append(ToolResult.ok(f"tool_{i % 5}", f"result_{i}", 1))
        assert len(tm._history) == 50
        assert tm._history[0].content == "result_5"  # first 5 were evicted


# ============================================================
# ToolManager — reload_plugin
# ============================================================

class SimplePlugin(ToolPlugin):
    name = "simple"
    description = "A simple plugin"
    param_model = None
    capabilities = set()
    safety = SafetyProfile()


@pytest.mark.service
class TestReloadPlugin:
    @pytest.mark.asyncio
    async def test_reload_nonexistent_returns_false(self):
        tm = ToolManager()
        assert await tm.reload_plugin("nonexistent") is False

    @pytest.mark.asyncio
    async def test_reload_existing_plugin(self):
        tm = ToolManager()
        ctx = PluginContext()
        await tm.load_all([SimplePlugin], ctx)
        assert "simple" in tm._plugins

        result = await tm.reload_plugin("simple")
        assert result is True
        assert "simple" in tm._plugins


# ============================================================
# ToolManager — set_hitl_broadcast
# ============================================================

@pytest.mark.service
class TestSetHITLBroadcast:
    @pytest.mark.asyncio
    async def test_set_hitl_broadcast_registers_callback(self):
        tm = ToolManager()
        output_mgr = MagicMock()
        output_mgr.command = AsyncMock()

        tm.set_hitl_broadcast(output_mgr)
        assert tm._pipeline._broadcast_hitl is not None

    def test_set_hitl_broadcast_with_none_is_noop(self):
        tm = ToolManager()
        tm.set_hitl_broadcast(None)
        # Should not crash


# ============================================================
# Concurrent execution
# ============================================================

class SlowEchoPlugin(ToolPlugin):
    name = "slow_echo"
    description = "Echo with delay"
    param_model = None
    capabilities = set()
    safety = SafetyProfile()

    async def execute(self, *, ctx, emit=None, params=None):
        await asyncio.sleep(0.05)
        return ToolResult.ok("slow_echo", "done", 50)


@pytest.mark.service
class TestConcurrentExecution:
    @pytest.mark.asyncio
    async def test_concurrent_execute_tracks_correctly(self):
        tm = ToolManager()
        await tm.load_all([SlowEchoPlugin], PluginContext())
        ctx = ToolContext()

        t1 = asyncio.create_task(tm.execute("slow_echo", ctx=ctx))
        t2 = asyncio.create_task(tm.execute("slow_echo", ctx=ctx))
        await asyncio.sleep(0.01)
        assert "slow_echo" in tm.executing_now
        await asyncio.gather(t1, t2)
        assert "slow_echo" not in tm.executing_now
