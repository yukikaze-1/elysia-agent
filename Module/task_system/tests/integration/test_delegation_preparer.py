"""DelegationPreparer 集成测试——需要 DEEPSEEK_API_KEY，真实 LLM 调用。"""

from __future__ import annotations

import os
from pathlib import Path

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

from Module.task_system.delegation_preparer import DelegationPreparer
from Module.task_system.schema import (
    ConstraintConfig,
    DelegationStatus,
    SessionId,
    TaskConstraints,
    Tool,
    UserProfile,
)


def _make_integration_tools() -> list[Tool]:
    """构造集成测试用的工具列表——贴合真实工具描述风格。"""
    return [
        Tool(
            name="web_search",
            description="搜索互联网获取实时信息。返回网页标题、URL 和摘要",
            capabilities=["read"],
        ),
        Tool(
            name="weather",
            description="查询指定城市指定日期的天气，包含温度、湿度、降水概率、风速",
            capabilities=["read"],
        ),
        Tool(
            name="send_email",
            description="发送邮件到指定地址。支持纯文本和 HTML 格式，支持抄送和密送",
            capabilities=["mutation"],
        ),
        Tool(
            name="file_read",
            description="读取指定路径的文件内容，支持文本文件和常见编码",
            capabilities=["read"],
        ),
        Tool(
            name="file_write",
            description="向指定路径写入文件内容。支持新建文件和追加模式",
            capabilities=["write"],
        ),
        Tool(
            name="memory_search",
            description="搜索对话记忆，返回与关键词相关的历史信息片段",
            capabilities=["read"],
        ),
        Tool(
            name="get_time",
            description="获取当前日期和时间，支持指定时区（如 Asia/Shanghai）和格式",
            capabilities=["read"],
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_integration_simple_task_ok():
    """真实 LLM：简单清晰的任务 → 应返回 ok + 筛选工具 + 摘要上下文。"""
    dp = DelegationPreparer()
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    result = await dp.prepare(
        intent="查询北京今天和明天的天气",
        hints="用户在北京",
        session_messages=[
            {"role": "user", "content": "今天天气怎么样？"},
            {"role": "assistant", "content": "请问你想查哪个城市的天气？"},
            {"role": "user", "content": "北京的"},
        ],
        user_profile=UserProfile(preferences="简洁回答"),
        tools=_make_integration_tools(),
        global_constraints=ConstraintConfig(),
        session_id=SessionId("integration-test-1"),
    )

    print(f"\n    status: {result.status.value}")
    print(f"    reason: {result.reason}")
    if result.context:
        print(f"    tools: {[t.name for t in result.context.tools]}")
        print(f"    summarized_context: {result.context.summarized_context[:200]}...")
        tc = result.context.task_constraints
        print(f"    max_dag_nodes: {tc.max_dag_nodes}")
        print(f"    max_mutations: {tc.max_mutations}")

    assert result.status == DelegationStatus.OK, (
        f"期望 ok，实际 {result.status.value}，reason={result.reason}"
    )
    assert result.context is not None
    tool_names = [t.name for t in result.context.tools]
    assert "weather" in tool_names, f"weather 应该在筛选结果中，实际: {tool_names}"
    assert "send_email" not in tool_names and "file_write" not in tool_names, (
        f"简单查询不应该选 mutation 工具，实际: {tool_names}"
    )
    assert len(result.context.summarized_context) > 10, "上下文摘要不应过短"
    assert result.context.task_constraints.max_dag_nodes is not None
    assert result.context.task_constraints.max_dag_nodes <= 5, (
        f"简单任务不应估算太多 DAG 节点: {result.context.task_constraints.max_dag_nodes}"
    )
    assert result.context.intent == "查询北京今天和明天的天气"
    assert result.context.hints == "用户在北京"
    assert result.context.session_id == SessionId("integration-test-1")
    assert result.token_usage is not None, "token_usage 应非 None"
    assert result.token_usage.get("total_tokens", 0) > 0, \
        f"DP token 消耗应 > 0，实际: {result.token_usage}"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_integration_multi_step_with_mutation():
    """真实 LLM：多步骤任务包含敏感操作 → 筛选相关工具 + 估算 mutation 约束。"""
    dp = DelegationPreparer()
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    result = await dp.prepare(
        intent="查询北京最近三天天气，如果有下雨的日子就发邮件给 boss@example.com 提醒带伞",
        hints="老板邮箱是 boss@example.com",
        session_messages=[
            {"role": "user", "content": "明天可能需要提醒老板带伞"},
        ],
        user_profile=UserProfile(),
        tools=_make_integration_tools(),
        global_constraints=ConstraintConfig(),
        session_id=SessionId("integration-test-2"),
    )

    print(f"\n    status: {result.status.value}")
    print(f"    reason: {result.reason}")
    if result.context:
        print(f"    tools: {[t.name for t in result.context.tools]}")
        print(f"    summarized_context: {result.context.summarized_context[:200]}...")
        tc = result.context.task_constraints
        print(f"    max_dag_nodes: {tc.max_dag_nodes}")
        print(f"    max_mutations: {tc.max_mutations}")
        print(f"    max_total_tokens: {tc.max_total_tokens}")

    assert result.status == DelegationStatus.OK, (
        f"期望 ok，实际 {result.status.value}，reason={result.reason}"
    )
    assert result.context is not None
    tool_names = [t.name for t in result.context.tools]
    assert "weather" in tool_names, f"应该包含 weather，实际: {tool_names}"
    assert "send_email" in tool_names, f"应该包含 send_email，实际: {tool_names}"
    assert result.context.task_constraints.max_mutations is not None
    assert result.context.task_constraints.max_mutations >= 1, (
        f"有发邮件操作，max_mutations 应 ≥ 1，实际: {result.context.task_constraints.max_mutations}"
    )
    assert result.context.task_constraints.max_dag_nodes is not None
    assert result.context.task_constraints.max_dag_nodes >= 2, (
        f"多步任务应有多个节点，实际: {result.context.task_constraints.max_dag_nodes}"
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_integration_too_vague():
    """真实 LLM：极其模糊的意图 → 应返回 too_vague + suggestion。"""
    dp = DelegationPreparer()
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    result = await dp.prepare(
        intent="帮我弄一下那个东西",
        hints=None,
        session_messages=[],
        user_profile=UserProfile(),
        tools=_make_integration_tools(),
        global_constraints=ConstraintConfig(),
        session_id=SessionId("integration-test-3"),
    )

    print(f"\n    status: {result.status.value}")
    print(f"    reason: {result.reason}")
    print(f"    suggestion: {result.suggestion}")

    assert result.status == DelegationStatus.TOO_VAGUE, (
        f"期望 too_vague，实际 {result.status.value}，reason={result.reason}"
    )
    assert result.context is None
    assert result.suggestion is not None, "too_vague 应有澄清建议"
    assert len(result.suggestion) > 5, "澄清建议不应过短"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_integration_no_matching_tools():
    """真实 LLM：意图清晰但工具库完全无法支持 → 应返回 no_matching_tools。"""
    dp = DelegationPreparer()
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    limited_tools = [
        Tool(name="get_time", description="获取当前时间", capabilities=["read"]),
        Tool(name="memory_search", description="搜索记忆", capabilities=["read"]),
    ]

    result = await dp.prepare(
        intent="创建一个 PostgreSQL 数据库，导入 CSV 数据，并建立索引",
        hints=None,
        session_messages=[],
        user_profile=UserProfile(),
        tools=limited_tools,
        global_constraints=ConstraintConfig(),
        session_id=SessionId("integration-test-4"),
    )

    print(f"\n    status: {result.status.value}")
    print(f"    reason: {result.reason}")
    print(f"    missing_tools: {result.missing_tools}")

    assert result.status == DelegationStatus.NO_MATCHING_TOOLS, (
        f"期望 no_matching_tools，实际 {result.status.value}，reason={result.reason}"
    )
    assert result.context is None
    assert len(result.missing_tools) > 0, "no_matching_tools 应列出缺少的工具"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_integration_context_summarization():
    """真实 LLM：验证从对话历史中提取上下文摘要的能力。"""
    dp = DelegationPreparer()
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    result = await dp.prepare(
        intent="查天气",
        hints=None,
        session_messages=[
            {"role": "user", "content": "我下周要去上海出差"},
            {"role": "assistant", "content": "好的，需要我帮你准备什么吗？"},
            {"role": "user", "content": "帮我查一下上海的天气，我还要订酒店"},
            {"role": "assistant", "content": "上海最近天气多变，建议带伞。你已经定了酒店吗？"},
            {"role": "user", "content": "还没订，先查天气吧，下雨的话就不去了"},
        ],
        user_profile=UserProfile(preferences="喜欢详细报告", city="北京"),
        tools=_make_integration_tools(),
        global_constraints=ConstraintConfig(),
        session_id=SessionId("integration-test-5"),
    )

    print(f"\n    status: {result.status.value}")
    print(f"    reason: {result.reason}")
    if result.context:
        print(f"    tools: {[t.name for t in result.context.tools]}")
        print(f"    summarized_context:\n{result.context.summarized_context}")

    assert result.status == DelegationStatus.OK, (
        f"期望 ok，实际 {result.status.value}，reason={result.reason}"
    )
    assert result.context is not None
    ctx = result.context.summarized_context
    assert "上海" in ctx, f"上下文摘要应提及上海: {ctx[:100]}"
    tool_names = [t.name for t in result.context.tools]
    assert "weather" in tool_names, f"应该包含 weather，实际: {tool_names}"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_integration_user_profile_influence():
    """真实 LLM：验证用户画像信息被纳入考量。"""
    dp = DelegationPreparer()
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    result = await dp.prepare(
        intent="搜索最新的 AI 安全新闻，整理成摘要写到一个文件里",
        hints="关注大模型安全和对齐方面的进展",
        session_messages=[],
        user_profile=UserProfile(
            role="技术总监",
            preferences="喜欢详细的报告，包含数据和图表",
            language="zh-CN",
        ),
        tools=_make_integration_tools(),
        global_constraints=ConstraintConfig(),
        session_id=SessionId("integration-test-6"),
    )

    print(f"\n    status: {result.status.value}")
    print(f"    reason: {result.reason}")
    if result.context:
        print(f"    tools: {[t.name for t in result.context.tools]}")
        tc = result.context.task_constraints
        print(f"    max_dag_nodes: {tc.max_dag_nodes}")

    assert result.status == DelegationStatus.OK, (
        f"期望 ok，实际 {result.status.value}，reason={result.reason}"
    )
    assert result.context is not None
    tool_names = [t.name for t in result.context.tools]
    assert len(tool_names) >= 1, f"至少应选出 1 个工具，实际: {tool_names}"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_integration_tool_object_integrity():
    """真实 LLM：验证返回的 Tool 对象保留完整的 description 和 capabilities。"""
    dp = DelegationPreparer()
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    result = await dp.prepare(
        intent="搜索一下 Python 异步编程最佳实践",
        hints=None,
        session_messages=[],
        user_profile=UserProfile(),
        tools=_make_integration_tools(),
        global_constraints=ConstraintConfig(),
        session_id=SessionId("integration-test-7"),
    )

    print(f"\n    status: {result.status.value}")
    print(f"    reason: {result.reason}")
    if result.context:
        for t in result.context.tools:
            print(f"    Tool: name={t.name}, desc={t.description[:50]}, caps={t.capabilities}")

    assert result.status == DelegationStatus.OK, (
        f"期望 ok，实际 {result.status.value}"
    )
    assert result.context is not None
    for tool in result.context.tools:
        assert tool.description, (
            f"Tool {tool.name} 的 description 不应为空（应保留原始 Tool 对象的描述）"
        )
        assert isinstance(tool.capabilities, list), (
            f"Tool {tool.name} 的 capabilities 应为 list"
        )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_integration_empty_session_messages():
    """真实 LLM：无对话历史时也能正常工作。"""
    dp = DelegationPreparer()
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    result = await dp.prepare(
        intent="现在几点了",
        hints=None,
        session_messages=[],
        user_profile=UserProfile(),
        tools=_make_integration_tools(),
        global_constraints=ConstraintConfig(),
        session_id=SessionId("integration-test-8"),
    )

    print(f"\n    status: {result.status.value}")
    print(f"    reason: {result.reason}")
    if result.context:
        print(f"    tools: {[t.name for t in result.context.tools]}")

    assert result.status == DelegationStatus.OK, (
        f"简单时间查询应返回 ok，实际 {result.status.value}"
    )
    assert result.context is not None
    tool_names = [t.name for t in result.context.tools]
    assert "get_time" in tool_names, f"应该包含 get_time，实际: {tool_names}"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_integration_global_constraints_respected():
    """真实 LLM：全局约束收紧时 LLM 估算不超出天花板。

    max_dag_nodes=2, max_mutations=0 —— 给一个简单查询任务（无需 mutation），
    验证 LLM 正确理解全局约束并在此范围内估算。
    """
    dp = DelegationPreparer()
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    tight_constraints = ConstraintConfig(
        max_dag_nodes=2,
        max_mutations=0,
    )

    result = await dp.prepare(
        intent="查询北京今天天气和空气质量",
        hints=None,
        session_messages=[],
        user_profile=UserProfile(),
        tools=_make_integration_tools(),
        global_constraints=tight_constraints,
        session_id=SessionId("integration-test-constraints"),
    )

    print(f"\n    status: {result.status.value}")
    print(f"    reason: {result.reason}")
    if result.context:
        tc = result.context.task_constraints
        print(f"    tools: {[t.name for t in result.context.tools]}")
        print(f"    max_dag_nodes: {tc.max_dag_nodes} (全局上限: 2)")
        print(f"    max_mutations: {tc.max_mutations} (全局上限: 0)")

    assert result.status == DelegationStatus.OK, (
        f"简单查询 + 收紧约束仍应 ok: {result.status.value}，reason={result.reason}"
    )
    assert result.context is not None
    tc = result.context.task_constraints

    # 核心断言：LLM 不应估算超过全局天花板
    assert tc.max_dag_nodes is not None
    assert tc.max_dag_nodes <= tight_constraints.max_dag_nodes, (
        f"max_dag_nodes={tc.max_dag_nodes} 超出全局上限 {tight_constraints.max_dag_nodes}"
    )
    assert tc.max_mutations is not None
    assert tc.max_mutations <= tight_constraints.max_mutations, (
        f"max_mutations={tc.max_mutations} 超出全局上限 {tight_constraints.max_mutations}"
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_integration_global_constraints_default_ok():
    """真实 LLM：使用默认全局约束时正常估算。

    默认 ConstraintConfig() 天花板很高（max_dag_nodes=20），
    LLM 应在合理范围内自由估算，不应被不必要地限制。
    """
    dp = DelegationPreparer()
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    default_constraints = ConstraintConfig()

    result = await dp.prepare(
        intent="查北京三天天气，如果有雨发邮件提醒",
        hints=None,
        session_messages=[],
        user_profile=UserProfile(),
        tools=_make_integration_tools(),
        global_constraints=default_constraints,
        session_id=SessionId("integration-test-default-constraints"),
    )

    print(f"\n    status: {result.status.value}")
    print(f"    reason: {result.reason}")
    if result.context:
        tc = result.context.task_constraints
        print(f"    tools: {[t.name for t in result.context.tools]}")
        print(f"    max_dag_nodes: {tc.max_dag_nodes} (全局上限: {default_constraints.max_dag_nodes})")
        print(f"    max_mutations: {tc.max_mutations} (全局上限: {default_constraints.max_mutations})")

    assert result.status == DelegationStatus.OK
    assert result.context is not None
    tc = result.context.task_constraints

    # 核心断言：不超出默认天花板（宽松）即可
    assert tc.max_dag_nodes is not None
    assert tc.max_dag_nodes <= default_constraints.max_dag_nodes, (
        f"max_dag_nodes={tc.max_dag_nodes} 超出全局上限 {default_constraints.max_dag_nodes}"
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_integration_global_constraints_with_resolve():
    """真实 LLM：验证两段式约束安全机制。

    第一段：LLM 参考全局约束估算（prompt 注入天花板值）
    第二段：TaskConstraints.resolve() 强制 clamp（真正的安全网）

    LLM 可能偶尔略超（如 max_dag_nodes=6 vs 上限=5），
    但 resolve() 保证最终值绝不超限。
    """
    dp = DelegationPreparer()
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    custom_constraints = ConstraintConfig(
        max_dag_nodes=5,
        max_mutations=1,
        max_total_tokens=50000,
        max_task_duration_s=300.0,
        max_failure_count=2,
        max_retries_per_node=1,
    )

    result = await dp.prepare(
        intent="搜索最新 AI 论文，整理摘要，发邮件给团队",
        hints=None,
        session_messages=[],
        user_profile=UserProfile(),
        tools=_make_integration_tools(),
        global_constraints=custom_constraints,
        session_id=SessionId("integration-test-custom-constraints"),
    )

    print(f"\n    status: {result.status.value}")
    print(f"    reason: {result.reason}")
    if result.context:
        raw_tc = result.context.task_constraints
        print(f"    tools: {[t.name for t in result.context.tools]}")
        print(f"    LLM 原始估算:")
        print(f"      max_dag_nodes: {raw_tc.max_dag_nodes} (全局: 5)")
        print(f"      max_mutations: {raw_tc.max_mutations} (全局: 1)")
        print(f"      max_total_tokens: {raw_tc.max_total_tokens} (全局: 50000)")
        print(f"      max_task_duration_s: {raw_tc.max_task_duration_s} (全局: 300)")
        print(f"      max_failure_count: {raw_tc.max_failure_count} (全局: 2)")

        # resolve() 后的值——真正的安全网
        resolved = raw_tc.resolve(custom_constraints)
        print(f"    resolve() 后:")
        print(f"      max_dag_nodes: {resolved.max_dag_nodes}")
        print(f"      max_mutations: {resolved.max_mutations}")

    assert result.status == DelegationStatus.OK
    assert result.context is not None

    # 验证 resolve() 强制 clamp——真正的安全保障
    resolved = result.context.task_constraints.resolve(custom_constraints)
    assert resolved.max_dag_nodes is not None
    assert resolved.max_dag_nodes <= custom_constraints.max_dag_nodes, (
        f"resolve() 后 max_dag_nodes={resolved.max_dag_nodes} "
        f"超出 {custom_constraints.max_dag_nodes}"
    )
    assert resolved.max_mutations is not None
    assert resolved.max_mutations <= custom_constraints.max_mutations, (
        f"resolve() 后 max_mutations={resolved.max_mutations} "
        f"超出 {custom_constraints.max_mutations}"
    )
    # None 字段应填充为全局值
    assert resolved.max_total_tokens == custom_constraints.max_total_tokens
    assert resolved.max_task_duration_s == custom_constraints.max_task_duration_s
    assert resolved.max_failure_count == custom_constraints.max_failure_count
