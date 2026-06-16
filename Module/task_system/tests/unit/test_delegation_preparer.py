"""DelegationPreparer 单元测试——不依赖真实 LLM，测试解析、校验、格式化、集成（mock LLM）。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# 加载项目根目录的 .env（CI/本地开发环境）
_env_path = Path(__file__).resolve().parents[4] / ".env"
if _env_path.exists():
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _value = _line.partition("=")
                _key = _key.strip()
                _value = _value.strip().strip('"').strip("'")
                if _key not in os.environ:
                    os.environ[_key] = _value

from Module.task_system.delegation_preparer import (
    DelegationPreparer,
    _PrepareParseError,
)
from Module.task_system.schema import (
    ConstraintConfig,
    DelegationContext,
    DelegationResult,
    DelegationStatus,
    ReturnMode,
    SessionId,
    TaskConstraints,
    Tool,
    UserProfile,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_tools() -> list[Tool]:
    return [
        Tool(name="web_search", description="搜索互联网", capabilities=["read"]),
        Tool(name="read_file", description="读取文件", capabilities=["read"]),
        Tool(name="write_file", description="写入文件", capabilities=["write"]),
        Tool(name="send_email", description="发送邮件", capabilities=["mutation"]),
        Tool(name="memory_search", description="搜索记忆", capabilities=["read"]),
    ]


@pytest.fixture
def sample_session_messages() -> list[dict]:
    return [
        {"role": "user", "content": "帮我查一下最近的天气"},
        {"role": "assistant", "content": "好的，我来帮你查"},
        {"role": "user", "content": "如果有雨的话提醒我"},
    ]


@pytest.fixture
def preparer():
    """创建 DelegationPreparer 实例（需要 DEEPSEEK_API_KEY 环境变量）。"""
    import os
    if not os.environ.get("DEEPSEEK_API_KEY"):
        os.environ["DEEPSEEK_API_KEY"] = "test-key"
    return DelegationPreparer()


# ═══════════════════════════════════════════════════════════════════════════════
# _strip_markdown_fence
# ═══════════════════════════════════════════════════════════════════════════════

class TestStripMarkdownFence:
    """测试 markdown 代码块剥离。"""

    def test_no_fence(self):
        result = DelegationPreparer._strip_markdown_fence('{"status": "ok"}')
        assert result == '{"status": "ok"}'

    def test_json_fence(self):
        result = DelegationPreparer._strip_markdown_fence(
            '```json\n{"status": "ok"}\n```'
        )
        assert result == '{"status": "ok"}'

    def test_plain_fence(self):
        result = DelegationPreparer._strip_markdown_fence(
            '```\n{"status": "ok"}\n```'
        )
        assert result == '{"status": "ok"}'

    def test_fence_with_whitespace(self):
        result = DelegationPreparer._strip_markdown_fence(
            '  \n```json\n{"status": "ok"}\n```\n  '
        )
        assert result == '{"status": "ok"}'

    def test_multiline_json(self):
        json_str = '{\n  "status": "ok",\n  "tools": ["web_search"]\n}'
        result = DelegationPreparer._strip_markdown_fence(
            f'```json\n{json_str}\n```'
        )
        assert result == json_str


# ═══════════════════════════════════════════════════════════════════════════════
# _parse_response
# ═══════════════════════════════════════════════════════════════════════════════

class TestParseResponse:
    """测试 JSON 解析。"""

    def test_parse_ok(self, preparer):
        data = preparer._parse_response(json.dumps({
            "status": "ok",
            "reason": "意图清晰",
            "tools": ["web_search"],
            "summarized_context": "用户想查天气",
            "max_dag_nodes": 3,
            "max_mutations": 0,
            "max_total_tokens": None,
            "max_task_duration_s": None,
            "max_failure_count": None,
            "missing_tools": [],
            "suggestion": None,
        }))
        assert data["status"] == "ok"
        assert data["tools"] == ["web_search"]
        assert data["max_dag_nodes"] == 3

    def test_parse_with_markdown_fence(self, preparer):
        data = preparer._parse_response(
            '```json\n{"status": "too_vague", "reason": "不明确", "suggestion": "请澄清"}\n```'
        )
        assert data["status"] == "too_vague"
        assert data["suggestion"] == "请澄清"

    def test_parse_no_matching_tools(self, preparer):
        data = preparer._parse_response(json.dumps({
            "status": "no_matching_tools",
            "reason": "缺少工具",
            "missing_tools": ["database_query"],
        }))
        assert data["status"] == "no_matching_tools"
        assert data["missing_tools"] == ["database_query"]

    def test_parse_invalid_json(self, preparer):
        with pytest.raises(_PrepareParseError, match="JSON 解析失败"):
            preparer._parse_response("不是 JSON")

    def test_parse_not_object(self, preparer):
        with pytest.raises(_PrepareParseError, match="期望 JSON object"):
            preparer._parse_response(json.dumps(["array"]))

    def test_parse_invalid_status(self, preparer):
        with pytest.raises(_PrepareParseError, match="status 无效"):
            preparer._parse_response(json.dumps({"status": "unknown_status"}))

    def test_parse_empty_response(self, preparer):
        with pytest.raises(_PrepareParseError):
            preparer._parse_response("")

    def test_parse_minimal_ok(self, preparer):
        """最简合法 ok 响应。"""
        data = preparer._parse_response(json.dumps({"status": "ok"}))
        assert data["status"] == "ok"


# ═══════════════════════════════════════════════════════════════════════════════
# _validate_output
# ═══════════════════════════════════════════════════════════════════════════════

class TestValidateOutput:
    """测试工具筛选校验。"""

    def test_all_tools_match(self, preparer, sample_tools):
        parsed = {"status": "ok", "tools": ["web_search", "read_file"]}
        result = preparer._validate_output(parsed, sample_tools)
        assert result["status"] == "ok"
        assert result["tools"] == ["web_search", "read_file"]

    def test_hallucinated_tool_removed(self, preparer, sample_tools):
        parsed = {"status": "ok", "tools": ["web_search", "nonexistent_tool"]}
        result = preparer._validate_output(parsed, sample_tools)
        assert result["status"] == "ok"
        assert result["tools"] == ["web_search"]

    def test_all_hallucinated_downgrades(self, preparer, sample_tools):
        """所有工具都是幻觉 → 降级为 no_matching_tools。"""
        parsed = {"status": "ok", "tools": ["ghost_tool", "phantom_tool"]}
        result = preparer._validate_output(parsed, sample_tools)
        assert result["status"] == "no_matching_tools"
        assert result["tools"] == []
        assert result["missing_tools"] == ["ghost_tool", "phantom_tool"]

    def test_empty_tools_list_ok_status(self, preparer, sample_tools):
        """status=ok 但 tools 为空列表 → 降级。"""
        parsed = {"status": "ok", "tools": []}
        result = preparer._validate_output(parsed, sample_tools)
        assert result["status"] == "no_matching_tools"

    def test_tools_not_a_list(self, preparer, sample_tools):
        """tools 不是 list → 视为空列表处理。"""
        parsed = {"status": "ok", "tools": "web_search"}  # str, not list
        result = preparer._validate_output(parsed, sample_tools)
        assert result["status"] == "no_matching_tools"

    def test_no_matching_tools_preserved(self, preparer, sample_tools):
        """status=no_matching_tools 保持不变。"""
        parsed = {"status": "no_matching_tools", "tools": [], "missing_tools": ["db"]}
        result = preparer._validate_output(parsed, sample_tools)
        assert result["status"] == "no_matching_tools"
        assert result["missing_tools"] == ["db"]

    def test_too_vague_no_change(self, preparer, sample_tools):
        parsed = {"status": "too_vague", "tools": [], "suggestion": "请说清楚"}
        result = preparer._validate_output(parsed, sample_tools)
        assert result["status"] == "too_vague"
        assert result["suggestion"] == "请说清楚"

    def test_partial_hallucination(self, preparer, sample_tools):
        """幻觉工具被剔除，真实工具保留。"""
        parsed = {"status": "ok", "tools": ["web_search", "fake1", "read_file", "fake2"]}
        result = preparer._validate_output(parsed, sample_tools)
        assert result["status"] == "ok"
        assert result["tools"] == ["web_search", "read_file"]

    def test_case_sensitive_match(self, preparer, sample_tools):
        """工具名大小写不匹配 → 视为幻觉。"""
        parsed = {"status": "ok", "tools": ["Web_Search", "READ_FILE"]}
        result = preparer._validate_output(parsed, sample_tools)
        assert result["tools"] == []  # 都未匹配


# ═══════════════════════════════════════════════════════════════════════════════
# _build_result
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildResult:
    """测试 DelegationResult 构建。"""

    def test_ok_result(self, sample_tools):
        session_id = SessionId("sess-001")
        parsed = {
            "status": "ok",
            "reason": "意图清晰，工具匹配",
            "tools": ["web_search", "read_file"],
            "summarized_context": "用户想查天气",
            "max_dag_nodes": 3,
            "max_mutations": 0,
            "max_total_tokens": None,
            "max_task_duration_s": None,
            "max_failure_count": None,
        }
        result = DelegationPreparer._build_result(
            parsed,
            intent="查天气",
            hints="着急",
            tools=sample_tools,
            session_id=session_id,
        )
        assert result.status == DelegationStatus.OK
        assert result.context is not None
        assert result.context.intent == "查天气"
        assert result.context.hints == "着急"
        assert result.context.session_id == session_id
        assert result.context.return_mode == ReturnMode.NATURAL_LANGUAGE
        assert result.context.summarized_context == "用户想查天气"
        assert result.context.task_constraints.max_dag_nodes == 3
        assert result.context.task_constraints.max_mutations == 0
        # 工具应保留完整 Tool 对象（含 description）
        assert len(result.context.tools) == 2
        assert result.context.tools[0].name == "web_search"
        assert result.context.tools[0].description == "搜索互联网"
        assert result.context.tools[1].name == "read_file"
        assert result.reason == "意图清晰，工具匹配"

    def test_ok_result_constraints_with_values(self, sample_tools):
        """TaskConstraints 非 None 字段传入。"""
        parsed = {
            "status": "ok",
            "tools": ["web_search"],
            "summarized_context": "",
            "max_dag_nodes": 5,
            "max_mutations": 2,
            "max_total_tokens": 50000,
            "max_task_duration_s": 300.0,
            "max_failure_count": 3,
        }
        result = DelegationPreparer._build_result(
            parsed, "intent", None, sample_tools, SessionId("s1"),
        )
        assert result.context
        tc = result.context.task_constraints
        assert tc.max_dag_nodes == 5
        assert tc.max_mutations == 2
        assert tc.max_total_tokens == 50000
        assert tc.max_task_duration_s == 300.0
        assert tc.max_failure_count == 3

    def test_ok_fallback_when_no_matching(self, sample_tools):
        """_validate_output 防御失败时的 fallback——tools 为空。"""
        parsed = {
            "status": "ok",
            "tools": [],
            "summarized_context": "",
        }
        result = DelegationPreparer._build_result(
            parsed, "intent", None, sample_tools, SessionId("s1"),
        )
        assert result.status == DelegationStatus.NO_MATCHING_TOOLS
        assert result.context is None

    def test_no_matching_tools_result(self, sample_tools):
        parsed = {
            "status": "no_matching_tools",
            "reason": "缺少数据库工具",
            "missing_tools": ["database_query"],
        }
        result = DelegationPreparer._build_result(
            parsed, "查数据", None, sample_tools, SessionId("s1"),
        )
        assert result.status == DelegationStatus.NO_MATCHING_TOOLS
        assert result.context is None
        assert result.missing_tools == ["database_query"]
        assert "数据库" in result.reason

    def test_too_vague_result(self, sample_tools):
        parsed = {
            "status": "too_vague",
            "reason": "意图不明确",
            "suggestion": "能再具体一点吗？",
        }
        result = DelegationPreparer._build_result(
            parsed, "帮我弄一下", None, sample_tools, SessionId("s1"),
        )
        assert result.status == DelegationStatus.TOO_VAGUE
        assert result.context is None
        assert result.suggestion == "能再具体一点吗？"

    def test_too_vague_no_suggestion(self, sample_tools):
        """suggestion 为 None 也可以。"""
        parsed = {
            "status": "too_vague",
            "reason": "模糊",
        }
        result = DelegationPreparer._build_result(
            parsed, "搞一下", None, sample_tools, SessionId("s1"),
        )
        assert result.status == DelegationStatus.TOO_VAGUE
        assert result.suggestion is None


# ═══════════════════════════════════════════════════════════════════════════════
# _format_constraints
# ═══════════════════════════════════════════════════════════════════════════════

class TestFormatConstraints:
    """测试全局约束格式化（注入 system prompt）。"""

    def test_default_constraints(self):
        result = DelegationPreparer._format_constraints(ConstraintConfig())
        assert "DAG 节点数上限: 20" in result
        assert "敏感操作上限: 10" in result
        assert "总 token 上限: 100000" in result
        assert "任务时长上限: 600.0s" in result
        assert "连续失败上限: 5" in result
        assert "单节点重试上限: 3" in result

    def test_custom_constraints(self):
        custom = ConstraintConfig(
            max_dag_nodes=3,
            max_mutations=0,
            max_total_tokens=1000,
            max_task_duration_s=60.0,
            max_failure_count=1,
            max_retries_per_node=0,
        )
        result = DelegationPreparer._format_constraints(custom)
        assert "DAG 节点数上限: 3" in result
        assert "敏感操作上限: 0" in result
        assert "总 token 上限: 1000" in result
        assert "任务时长上限: 60.0s" in result
        assert "连续失败上限: 1" in result
        assert "单节点重试上限: 0" in result

    def test_contains_warning_label(self):
        """格式化输出应包含提示语——告诉 LLM 不要超限。"""
        result = DelegationPreparer._format_constraints(ConstraintConfig())
        assert "天花板" in result
        assert "不应超过" in result


# ═══════════════════════════════════════════════════════════════════════════════
# _format_tools
# ═══════════════════════════════════════════════════════════════════════════════

class TestFormatTools:
    """测试工具列表格式化。"""

    def test_empty_tools(self):
        result = DelegationPreparer._format_tools([])
        assert "无可用工具" in result

    def test_tool_with_description(self):
        tools = [Tool(name="search", description="搜索")]
        result = DelegationPreparer._format_tools(tools)
        assert "search" in result
        assert "搜索" in result

    def test_tool_with_capabilities(self):
        tools = [Tool(name="write", capabilities=["write", "mutation"])]
        result = DelegationPreparer._format_tools(tools)
        assert "write" in result
        assert "write, mutation" in result

    def test_tool_no_description_no_caps(self):
        tools = [Tool(name="bare_tool")]
        result = DelegationPreparer._format_tools(tools)
        assert "bare_tool" in result

    def test_multiple_tools(self, sample_tools):
        result = DelegationPreparer._format_tools(sample_tools)
        assert "web_search" in result
        assert "send_email" in result
        assert "memory_search" in result


# ═══════════════════════════════════════════════════════════════════════════════
# _format_session_messages
# ═══════════════════════════════════════════════════════════════════════════════

class TestFormatSessionMessages:
    """测试会话消息格式化。"""

    def test_empty_messages(self):
        result = DelegationPreparer._format_session_messages([])
        assert "无对话历史" in result

    def test_simple_messages(self, sample_session_messages):
        result = DelegationPreparer._format_session_messages(sample_session_messages)
        assert "[user]" in result
        assert "[assistant]" in result
        assert "查一下最近的天气" in result

    def test_message_with_multimodal_content(self):
        """content 为 list[dict]（多模态 parts）时提取文本。"""
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "content": "看看这张图"},
                {"type": "image", "source": "https://example.com/img.png"},
            ],
        }]
        result = DelegationPreparer._format_session_messages(messages)
        assert "看看这张图" in result
        assert "img.png" not in result  # 非文本内容不应出现

    def test_message_truncation(self):
        """超长消息截断。"""
        long_text = "x" * 3000
        messages = [{"role": "user", "content": long_text}]
        result = DelegationPreparer._format_session_messages(messages)
        assert "...[截断]" in result
        assert len(result) < 2500  # 截断后不应超过原文

    def test_non_dict_message(self):
        """非 dict 元素容错。"""
        messages = [{"role": "user", "content": "hi"}, "bare string"]
        result = DelegationPreparer._format_session_messages(messages)
        assert "[unknown]" in result
        assert "bare string" in result

    def test_message_missing_role(self):
        messages = [{"content": "缺少 role"}]
        result = DelegationPreparer._format_session_messages(messages)
        assert "[unknown]" in result


# ═══════════════════════════════════════════════════════════════════════════════
# _format_user_profile
# ═══════════════════════════════════════════════════════════════════════════════

class TestFormatUserProfile:
    """测试用户画像格式化。"""

    def test_empty_profile(self):
        profile = UserProfile()
        result = DelegationPreparer._format_user_profile(profile)
        # 默认字段 prefs={} + lang="zh" → JSON 输出
        assert "preferences" in result
        assert "language" in result

    def test_profile_with_extra_fields(self):
        """UserProfile 接受 declared 字段 + extra 字段。"""
        profile = UserProfile(
            preferences={"style": "简洁回答"},
            language="zh",
            extra_field="任意值",  # extra="allow" 仍然生效
        )
        result = DelegationPreparer._format_user_profile(profile)
        assert "简洁回答" in result
        assert "extra_field" in result


# ═══════════════════════════════════════════════════════════════════════════════
# prepare() 集成测试
# ═══════════════════════════════════════════════════════════════════════════════

class TestPrepareIntegration:
    """preprep() 集成测试——mock LLM 调用。"""

    @pytest.mark.asyncio
    async def test_prepare_ok(self, preparer, sample_tools, sample_session_messages):
        """status=ok 的完整流程。"""
        mock_response = json.dumps({
            "status": "ok",
            "reason": "查询天气，工具匹配",
            "tools": ["web_search"],
            "summarized_context": "用户想知道最近天气",
            "max_dag_nodes": 3,
            "max_mutations": 0,
            "max_total_tokens": None,
            "max_task_duration_s": None,
            "max_failure_count": None,
            "missing_tools": [],
            "suggestion": None,
        })
        with patch.object(preparer, "_call_llm_with_usage", AsyncMock(return_value=(mock_response, {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}))):
            result = await preparer.prepare(
                intent="查天气",
                hints="着急",
                session_messages=sample_session_messages,
                user_profile=UserProfile(),
                tools=sample_tools,
                global_constraints=ConstraintConfig(),
                session_id=SessionId("sess-test"),
            )
        assert result.status == DelegationStatus.OK
        assert result.context is not None
        assert result.context.intent == "查天气"
        assert result.context.hints == "着急"
        assert result.context.session_id == "sess-test"
        assert len(result.context.tools) == 1
        assert result.context.tools[0].name == "web_search"
        assert result.context.tools[0].description == "搜索互联网"
        assert result.token_usage == {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}

    @pytest.mark.asyncio
    async def test_prepare_no_matching_tools(self, preparer, sample_tools):
        """LLM 返回 no_matching_tools。"""
        mock_response = json.dumps({
            "status": "no_matching_tools",
            "reason": "当前工具库缺少数据库查询能力",
            "missing_tools": ["database_query"],
        })
        with patch.object(preparer, "_call_llm_with_usage", AsyncMock(return_value=(mock_response, {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70}))):
            result = await preparer.prepare(
                intent="查询数据库",
                hints=None,
                session_messages=[],
                user_profile=UserProfile(),
                tools=sample_tools,
                global_constraints=ConstraintConfig(),
                session_id=SessionId("s1"),
            )
        assert result.status == DelegationStatus.NO_MATCHING_TOOLS
        assert result.context is None
        assert result.missing_tools == ["database_query"]

    @pytest.mark.asyncio
    async def test_prepare_too_vague(self, preparer, sample_tools):
        """LLM 返回 too_vague。"""
        mock_response = json.dumps({
            "status": "too_vague",
            "reason": "意图过于模糊",
            "suggestion": "请问您想做什么？",
        })
        with patch.object(preparer, "_call_llm_with_usage", AsyncMock(return_value=(mock_response, {"prompt_tokens": 40, "completion_tokens": 15, "total_tokens": 55}))):
            result = await preparer.prepare(
                intent="帮我弄一下",
                hints=None,
                session_messages=[],
                user_profile=UserProfile(),
                tools=sample_tools,
                global_constraints=ConstraintConfig(),
                session_id=SessionId("s1"),
            )
        assert result.status == DelegationStatus.TOO_VAGUE
        assert result.context is None
        assert result.suggestion == "请问您想做什么？"

    @pytest.mark.asyncio
    async def test_prepare_hallucinated_tools_filtered(
        self, preparer, sample_tools,
    ):
        """LLM 返回幻觉工具 → _validate_output 剔除。"""
        mock_response = json.dumps({
            "status": "ok",
            "reason": "需要搜索和发邮件",
            "tools": ["web_search", "send_email", "ghost_tool"],
            "summarized_context": "测试",
            "max_dag_nodes": 3,
            "max_mutations": 1,
            "max_total_tokens": None,
            "max_task_duration_s": None,
            "max_failure_count": None,
        })
        with patch.object(preparer, "_call_llm_with_usage", AsyncMock(return_value=(mock_response, {"prompt_tokens": 80, "completion_tokens": 30, "total_tokens": 110}))):
            result = await preparer.prepare(
                intent="测试",
                hints=None,
                session_messages=[],
                user_profile=UserProfile(),
                tools=sample_tools,
                global_constraints=ConstraintConfig(),
                session_id=SessionId("s1"),
            )
        assert result.status == DelegationStatus.OK
        assert len(result.context.tools) == 2
        tool_names = [t.name for t in result.context.tools]
        assert "web_search" in tool_names
        assert "send_email" in tool_names
        assert "ghost_tool" not in tool_names

    @pytest.mark.asyncio
    async def test_prepare_all_tools_hallucinated(
        self, preparer, sample_tools,
    ):
        """LLM 全部幻觉 → 降级 no_matching_tools。"""
        mock_response = json.dumps({
            "status": "ok",
            "reason": "可以用这些工具",
            "tools": ["ghost1", "ghost2"],
            "summarized_context": "测试",
            "max_dag_nodes": 2,
            "max_mutations": 0,
            "max_total_tokens": None,
            "max_task_duration_s": None,
            "max_failure_count": None,
        })
        with patch.object(preparer, "_call_llm_with_usage", AsyncMock(return_value=(mock_response, {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40}))):
            result = await preparer.prepare(
                intent="测试",
                hints=None,
                session_messages=[],
                user_profile=UserProfile(),
                tools=sample_tools,
                global_constraints=ConstraintConfig(),
                session_id=SessionId("s1"),
            )
        assert result.status == DelegationStatus.NO_MATCHING_TOOLS
        assert result.context is None
        assert result.missing_tools == ["ghost1", "ghost2"]

    @pytest.mark.asyncio
    async def test_prepare_llm_error(self, preparer, sample_tools):
        """LLM 调用异常向上传播。"""
        with patch.object(
            preparer, "_call_llm_with_usage", AsyncMock(side_effect=RuntimeError("API 错误")),
        ):
            with pytest.raises(RuntimeError, match="API 错误"):
                await preparer.prepare(
                    intent="测试",
                    hints=None,
                    session_messages=[],
                    user_profile=UserProfile(),
                    tools=sample_tools,
                    global_constraints=ConstraintConfig(),
                    session_id=SessionId("s1"),
                )

    @pytest.mark.asyncio
    async def test_prepare_parse_error(self, preparer, sample_tools):
        """LLM 返回非法 JSON。"""
        with patch.object(
            preparer, "_call_llm_with_usage", AsyncMock(return_value=("not valid json !!!", {})),
        ):
            with pytest.raises(_PrepareParseError, match="JSON 解析失败"):
                await preparer.prepare(
                    intent="测试",
                    hints=None,
                    session_messages=[],
                    user_profile=UserProfile(),
                    tools=sample_tools,
                    global_constraints=ConstraintConfig(),
                    session_id=SessionId("s1"),
                )

    @pytest.mark.asyncio
    async def test_prepare_preserves_tool_description(
        self, preparer, sample_tools,
    ):
        """验证返回的工具保留完整 description 和 capabilities。"""
        mock_response = json.dumps({
            "status": "ok",
            "reason": "测试",
            "tools": ["send_email"],
            "summarized_context": "test",
            "max_dag_nodes": 1,
            "max_mutations": 1,
            "max_total_tokens": None,
            "max_task_duration_s": None,
            "max_failure_count": None,
        })
        with patch.object(preparer, "_call_llm_with_usage", AsyncMock(return_value=(mock_response, {"prompt_tokens": 60, "completion_tokens": 25, "total_tokens": 85}))):
            result = await preparer.prepare(
                intent="发邮件",
                hints=None,
                session_messages=[],
                user_profile=UserProfile(),
                tools=sample_tools,
                global_constraints=ConstraintConfig(),
                session_id=SessionId("s1"),
            )
        tool = result.context.tools[0]
        assert tool.name == "send_email"
        assert tool.description == "发送邮件"
        assert "mutation" in tool.capabilities

    @pytest.mark.asyncio
    async def test_prepare_global_constraints_injected(self, preparer, sample_tools):
        """验证 global_constraints 被注入 system prompt 且 prepare 正常完成。

        mock _call_llm_with_usage 直接返回 ok 响应，重点是验证 prepare()
        能用非默认的 ConstraintConfig 正常走完整个流程不崩溃。
        """
        tight = ConstraintConfig(
            max_dag_nodes=2,
            max_mutations=0,
            max_failure_count=1,
        )
        mock_response = json.dumps({
            "status": "ok",
            "reason": "约束收紧但任务简单",
            "tools": ["web_search"],
            "summarized_context": "测试",
            "max_dag_nodes": 2,
            "max_mutations": 0,
            "max_total_tokens": None,
            "max_task_duration_s": None,
            "max_failure_count": None,
        })
        with patch.object(preparer, "_call_llm_with_usage", AsyncMock(return_value=(mock_response, {"prompt_tokens": 70, "completion_tokens": 30, "total_tokens": 100}))):
            result = await preparer.prepare(
                intent="简单查询",
                hints=None,
                session_messages=[],
                user_profile=UserProfile(),
                tools=sample_tools,
                global_constraints=tight,
                session_id=SessionId("s1"),
            )
        assert result.status == DelegationStatus.OK
        # 约束估值为 LLM 的原始值（mock 返回 max_dag_nodes=2, max_mutations=0），
        # 均在 tight 约束范围内
        assert result.context.task_constraints.max_dag_nodes == 2
        assert result.context.task_constraints.max_mutations == 0


