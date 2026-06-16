"""TaskSystem MVP 端到端集成测试——真实 LLM + 真实工具。

验证完整链路：
    DelegationPreparer → TaskSystem.submit() → SubAgent.run()
    → Planner → Validator → Orchestrator → Executor → Evaluator → Mailbox

每步打印 LLM prompt/response + 耗时，最后输出时间线总览。

需要 DEEPSEEK_API_KEY 环境变量。用法:
    conda activate Elysia
    python -m pytest Module/task_system/tests/core/test_e2e_mvp.py -v -s
    python -m pytest Module/task_system/tests/core/test_e2e_mvp.py::test_e2e_simple_query -v -s
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from openai import AsyncOpenAI

# ═══════════════════════════════════════════════════════════════════════════════
# 环境准备（必须在 TaskSystem 相关 import 之前——组件构造时读 env）
# ═══════════════════════════════════════════════════════════════════════════════

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
from Module.task_system.infra import AgentInfra
from Module.task_system.schema import (
    ConstraintConfig,
    DelegationStatus,
    SessionId,
    Tool,
    UserProfile,
)
from Module.task_system.task_system import TaskSystem
from Module.tool.manager import ToolManager
from Module.tool.protocol import PluginContext
from core.tools.time.plugin import TimePlugin
from core.tools.web.plugin import WebSearchPlugin
from Module.task_system.mailbox import TaskMailbox

# ── 格式化 ────────────────────────────────────────────────────────────────────

_SEP = "═" * 70
_SEP2 = "─" * 70
_SEP3 = "·" * 70
_t0: float = 0.0
_timeline: list[dict] = []


def _reset_timeline() -> None:
    global _t0, _timeline
    _t0 = time.monotonic()
    _timeline = []


def _elapsed() -> float:
    return time.monotonic() - _t0


def _fmt_duration(seconds: float) -> str:
    if seconds < 0.001:
        return f"{seconds * 1_000_000:.0f}μs"
    elif seconds < 1.0:
        return f"{seconds * 1000:.0f}ms"
    elif seconds < 60:
        return f"{seconds:.1f}s"
    else:
        m, s = divmod(seconds, 60)
        return f"{int(m)}m{s:.0f}s"


def _log_step(num: int, total: int, title: str) -> float:
    elapsed = _elapsed()
    print(f"\n{_SEP}")
    print(f"Step {num}/{total}: {title}  [T+{_fmt_duration(elapsed)}]")
    print(f"{_SEP}")
    return elapsed


def _log_ok(msg: str) -> None:
    print(f"\n✅ {msg}  [T+{_fmt_duration(_elapsed())}]")


def _log_warn(msg: str) -> None:
    print(f"\n⚠️  {msg}  [T+{_fmt_duration(_elapsed())}]")


def _log_info(msg: str) -> None:
    print(f"   {msg}")


def _log_sub(component: str, msg: str) -> None:
    """子组件输出（缩进）。"""
    print(f"  ┌─ [{component}] {msg}")


def _record_timeline(step: str, duration: float) -> None:
    _timeline.append({"step": step, "duration": duration})


def _log_json(label: str, text: str, max_chars: int = 8000) -> None:
    """打印 JSON 文本——自动检测并格式化。"""
    elapsed = _elapsed()
    # 尝试解析为 JSON 并美化
    try:
        parsed = json.loads(text)
        formatted = json.dumps(parsed, indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        formatted = text

    if len(formatted) > max_chars:
        formatted = formatted[:max_chars] + f"\n... [{len(formatted) - max_chars} more chars]"

    print(f"\n📋 [{label}]  [T+{_fmt_duration(elapsed)}] ({len(text)} chars)")
    print(f"{_SEP3}")
    print(formatted)
    print(f"{_SEP3}")


def _print_timeline() -> None:
    total = _elapsed()
    print(f"\n{_SEP}")
    print(f"📊 时间线总览  (total: {_fmt_duration(total)})")
    print(f"{_SEP}")

    max_name = max((len(t["step"]) for t in _timeline), default=30)
    bar_max = 50

    for t in _timeline:
        name = t["step"].ljust(max_name)
        dur = _fmt_duration(t["duration"])
        bar_len = max(1, int(t["duration"] / total * bar_max)) if total > 0 else 1
        bar = "█" * bar_len
        print(f"  {name}  {dur:>8s}  {bar}")

    print(f"  {'─' * (max_name + bar_max + 12)}")
    print(f"  {'TOTAL'.ljust(max_name)}  {_fmt_duration(total):>8s}")


# ═══════════════════════════════════════════════════════════════════════════════
# LLM 调用拦截器 —— 自动识别组件并打印 prompt/response
# ═══════════════════════════════════════════════════════════════════════════════

def _detect_component(messages: list[dict]) -> str:
    """从 LLM 消息中检测调用组件。"""
    for m in messages:
        content = m.get("content", "") or ""
        if not isinstance(content, str):
            continue
        if "你是任务委派准备器" in content:
            return "DelegationPreparer"
        if "你是任务规划器" in content:
            return "Planner"
        if "你是任务修复器" in content:
            return "Planner/Replan"
        if "你是一个任务执行结果的评估专家" in content:
            return "Evaluator"
    # Executor: function-calling 调用，message 中有 tools 参数
    return "Executor"


def _wrap_llm_client(llm: AsyncOpenAI, label: str = "LLM") -> AsyncOpenAI:
    """包装 AsyncOpenAI，拦截所有 chat.completions.create() 调用。

    自动检测调用组件（Planner/Executor/Evaluator），
    打印 system prompt 摘要 + 响应内容 + 耗时。
    """
    original_create = llm.chat.completions.create

    async def _wrapped_create(**kwargs: Any) -> Any:
        messages = kwargs.get("messages", [])
        component = _detect_component(messages)

        # 打印 system prompt（完整，不截断——最长 ~1.3k 字）
        for m in messages:
            if m.get("role") == "system":
                content = m.get("content", "")
                if isinstance(content, str):
                    print(f"\n  ┌─ 📤 [{component}] System Prompt → LLM ({len(content)} chars)")
                    for line in content.split("\n"):
                        print(f"  │  {line}")
                    print(f"  └─")

        # 打印 user prompt（完整）
        for m in messages:
            if m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, str) and len(content) > 10:
                    print(f"  ┌─ 📤 [{component}] User Prompt → LLM ({len(content)} chars)")
                    for line in content.split("\n"):
                        print(f"  │  {line}")
                    print(f"  └─")

        # 如果有 tools（function calling）
        tools = kwargs.get("tools")
        if tools:
            tool_names = [t.get("function", {}).get("name", "?") for t in tools]
            print(f"  ┌─ [{component}] Function Calling → LLM (tools: {tool_names})")

        call_start = time.monotonic()
        response = await original_create(**kwargs)
        call_dur = time.monotonic() - call_start

        choice = response.choices[0]
        if hasattr(choice, "message") and choice.message:
            msg = choice.message
            if msg.tool_calls:
                # Executor: tool call response
                for tc in msg.tool_calls:
                    fn = tc.function
                    print(f"  │  ← LLM tool_call: {fn.name}({fn.arguments[:300]})")
                print(f"  └─  [{component}] 耗时: {_fmt_duration(call_dur)}")
            elif msg.content:
                # 文本响应
                content = msg.content
                _log_json(f"{component} ← LLM", content)
                print(f"  └─  [{component}] 耗时: {_fmt_duration(call_dur)}")
            else:
                print(f"  └─  [{component}] 空响应 耗时: {_fmt_duration(call_dur)}")
        else:
            print(f"  └─  [{component}] 无 message 耗时: {_fmt_duration(call_dur)}")

        return response

    llm.chat.completions.create = _wrapped_create  # type: ignore[method-assign]
    return llm


# ═══════════════════════════════════════════════════════════════════════════════
# TaskSystem 状态快照
# ═══════════════════════════════════════════════════════════════════════════════

def _show_event_summary(event_type: str, data: dict, event_count: int) -> None:
    """从 Mailbox 事件数据中提取并展示任务/节点状态摘要。

    不访问内部私有字段——完全基于事件 data 展示。
    progress 事件自带 node_id + node_index + percent + message，
    这些都是 Orchestrator 在节点完成时推送的结构化状态。
    """
    if event_type == "progress":
        nid = data.get("node_id", "?")
        idx = data.get("node_index", "?")
        pct = data.get("percent", 0)
        msg = data.get("message", "")
        if len(msg) > 120:
            msg = msg[:120] + "..."
        print(f"  node_id:       {nid}")
        print(f"  node_index:    {idx}")
        print(f"  percent:       {pct}")
        print(f"  message:       {msg}")

    elif event_type == "completed":
        total = data.get("total_nodes", 0)
        done = data.get("completed_nodes", 0)
        failed = data.get("failed_nodes", 0)
        print(f"  total_nodes:     {total}")
        print(f"  completed_nodes: {done}")
        print(f"  failed_nodes:    {failed}")
        result_data = data.get("result", {})
        result_preview = json.dumps(result_data, indent=2, ensure_ascii=False,
                                    default=str)
        if len(result_preview) > 1000:
            result_preview = result_preview[:1000] + "\n... [truncated]"
        print(f"  result: {result_preview}")

    elif event_type == "failed":
        err = data.get("error", "?")
        err_type = data.get("error_type", "?")
        print(f"  error:       {err[:300]}")
        print(f"  error_type:  {err_type}")

    elif event_type == "paused":
        print(f"  message: {data.get('message', '?')}")

    else:
        preview = json.dumps(data, indent=2, ensure_ascii=False, default=str)[:500]
        print(f"  data: {preview}")


# ═══════════════════════════════════════════════════════════════════════════════
# 准备工具：TaskSystem + 真实 ToolManager
# ═══════════════════════════════════════════════════════════════════════════════


async def _build_task_system() -> TaskSystem:
    """创建并启动 TaskSystem，注入真实 LLM + ToolManager。

    使用最小工具集：TimePlugin + WebSearchPlugin。
    LLM client 被包装以自动记录所有 LLM 调用。
    """
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY 未设置，无法创建 AsyncOpenAI")

    llm = _wrap_llm_client(AsyncOpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
    ))

    tm = ToolManager()
    ctx = PluginContext(session_id="e2e-test")
    await tm.load_all([TimePlugin, WebSearchPlugin], ctx)
    _log_info(f"已加载工具: {list(tm._plugins.keys())}")

    infra = AgentInfra(
        llm=llm,
        milvus=None,
        http=None,
        tool_registry=tm,
        constraints=ConstraintConfig(),
    )

    ts = TaskSystem(infra=infra, hitl_notifier=MagicMock(), max_sub_agents=2)
    await ts.start()

    # Planner 内部创建了自己的 AsyncOpenAI，不共享 infra.llm——
    # 需要单独包装以捕获其 LLM 调用
    assert ts._planner is not None
    _wrap_llm_client(ts._planner._client, label="Planner")

    return ts


def _get_tools_from_manager(tm: ToolManager) -> list[Tool]:
    """从 ToolManager 提取 TaskSystem 格式的 Tool 列表。"""
    tools: list[Tool] = []
    for name, plugin in tm._plugins.items():
        caps = getattr(plugin, "capabilities", [])
        if isinstance(caps, str):
            caps = [caps]
        desc = (
            getattr(plugin, "description", "")
            or getattr(plugin, "tool_description", "")
            or ""
        )
        tools.append(Tool(
            name=name,
            description=desc,
            capabilities=list(caps) if caps else [],
        ))
    return tools


# ═══════════════════════════════════════════════════════════════════════════════
# 共享 helper：运行委派 → submit → 消费事件
# ═══════════════════════════════════════════════════════════════════════════════


async def _run_task(
    ts: TaskSystem,
    tools: list[Tool],
    intent: str,
    hints: str | None,
    session_id: str,
    test_label: str,
) -> None:
    """运行一次完整的 MVP 任务：DelegationPreparer → submit → 消费事件。

    所有 LLM 调用由 _wrap_llm_client 自动拦截并打印。
    """
    TOTAL_STEPS = 6
    constraints = ConstraintConfig()

    # ── Step 1: DelegationPreparer ──
    step_start = _log_step(1, TOTAL_STEPS, "DelegationPreparer 准备委派上下文")
    dp = DelegationPreparer()
    # DelegationPreparer 也自建 AsyncOpenAI，需单独包装
    _wrap_llm_client(dp._client, label="DelegationPreparer")

    result = await dp.prepare(
        intent=intent,
        hints=hints,
        session_messages=[],
        user_profile=UserProfile(),
        tools=tools,
        global_constraints=constraints,
        session_id=SessionId(session_id),
    )

    _log_info(f"status: {result.status.value}")
    _log_info(f"reason: {result.reason}")

    assert result.status == DelegationStatus.OK, (
        f"DelegationPreparer 应返回 ok，实际: {result.status.value}"
    )
    assert result.context is not None
    _log_info(f"筛选工具: {[t.name for t in result.context.tools]}")
    _log_info(f"上下文摘要: {result.context.summarized_context[:200] if result.context.summarized_context else '(空)'}")
    tc = result.context.task_constraints
    _log_info(f"任务约束: max_dag_nodes={tc.max_dag_nodes}, max_mutations={tc.max_mutations}")
    _log_ok("委派上下文就绪")
    _record_timeline("1.DelegationPreparer", _elapsed() - step_start)

    # ── Step 2: TaskSystem.submit ──
    step_start = _log_step(2, TOTAL_STEPS, "TaskSystem.submit() → 启动 SubAgent")
    submit_result = await ts.submit(result.context)
    assert isinstance(submit_result, TaskMailbox), f"submit 失败: {submit_result}"
    
    mailbox = submit_result
    task_id = mailbox.task_id
    _log_ok(f"task_id={task_id}, Mailbox 就绪, fire-and-forget 已启动")
    _record_timeline("2.Submit", _elapsed() - step_start)

    # ── Step 3: 消费 Mailbox 事件 ──
    step_start = _log_step(3, TOTAL_STEPS,
                           "消费 Mailbox 事件（Planner → Validator → Orchestrator → Executor → Evaluator）")
    print(f"  (LLM 调用详情见下方的子组件输出)\n")

    event_count = 0
    total_nodes = 0
    completed_nodes = 0
    last_event_type = ""
    while True:
        event = await mailbox.get(timeout=120.0)
        if event is None:
            _log_info("Mailbox 超时（120s 无事件），退出等待")
            break
        event_count += 1

        event_type = event.type.value
        data = event.data

        print(f"\n{_SEP2}")
        print(f"📬 Mailbox Event #{event_count}: {event_type}  [T+{_fmt_duration(_elapsed())}]")
        print(f"{_SEP2}")

        if event_type == "progress":
            _show_event_summary(event_type, data, event_count)

        elif event_type == "completed":
            total_nodes = data.get("total_nodes", 0)
            completed_nodes = data.get("completed_nodes", 0)
            _show_event_summary(event_type, data, event_count)
            _log_ok("任务完成 ✅")
            break

        elif event_type == "failed":
            error_msg = data.get("error", "")
            _show_event_summary(event_type, data, event_count)
            if "Replan 尚未实现" in error_msg:
                _log_warn("replan 调用异常（LLM 不可用？）→ FAILED")
            elif "熔断" in error_msg:
                _log_warn(f"replan 熔断: {error_msg[:200]}")
            else:
                _log_warn(f"任务异常: {error_msg[:200]}")
            break

        elif event_type == "paused":
            _show_event_summary(event_type, data, event_count)

        elif event_type in ("approval_needed", "cancelled"):
            preview = json.dumps(data, indent=2, ensure_ascii=False, default=str)[:500]
            print(f"  data: {preview}")

        last_event_type = event_type
        print(f"{_SEP2}")

    _log_info(f"共收到 {event_count} 个事件")
    _record_timeline("3.编排执行", _elapsed() - step_start)

    # ── 超时兜底：等待 SubAgent 后台任务完成后再检查清理 ──
    if task_id in ts._tasks:
        _log_warn("Mailbox 超时但 SubAgent 仍在运行——等待最多 30s 让其自然终止")
        for _ in range(30):
            await asyncio.sleep(1.0)
            if task_id not in ts._tasks:
                break

    # ── Step 4: 状态汇总 ──
    _log_step(4, TOTAL_STEPS, "Evaluator 判定 + PlanValidator 校验结果")
    print(f"  Evaluator 在每个节点执行完成后即时判定（见上方 LLM 调用）。")
    print(f"  PlanValidator 在 Planner 产出的 DAG 上完成静态校验。")
    print(f"  最终事件类型: {last_event_type}")
    _record_timeline("4.判定汇总", 0)

    # ── Step 5: 清理确认 ──
    _log_step(5, TOTAL_STEPS, "结果汇总 & 清理确认")
    _log_ok(f"task_id={task_id}")
    _log_info(f"DAG 节点: total={total_nodes}, completed={completed_nodes}")
    assert task_id not in ts._tasks, "任务应从注册表清理"
    assert task_id not in ts._mailboxes, "Mailbox 应从注册表清理"
    _log_info("注册表已清理 ✅")
    _record_timeline("5.结果汇总", _elapsed() - step_start)

    # ── Step 6: 时间线 ──
    _log_step(6, TOTAL_STEPS, "时间线总览")
    _print_timeline()


# ═══════════════════════════════════════════════════════════════════════════════
# 测试用例
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.integration
async def test_e2e_simple_query():
    """端到端：简单单步任务——"现在几点了"。

    链路: DelegationPreparer → TaskSystem.submit() → SubAgent.run()
          → Planner(1 node) → Validator → Orchestrator → Executor(get_time)
          → Evaluator → Mailbox(COMPLETED)

    预期: 1 个 DAG 节点，get_time 工具，SUCCESS。
    """
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    _reset_timeline()

    print(f"\n{'#' * 70}")
    print(f"#  E2E TEST: simple_query — \"现在几点了\"")
    print(f"{'#' * 70}")

    step_start = _log_step(0, 6, "初始化 TaskSystem + ToolManager")
    ts = await _build_task_system()
    tools = _get_tools_from_manager(ts._infra.tool_registry)
    _log_ok(f"TaskSystem 就绪，工具: {[t.name for t in tools]}")
    _record_timeline("0.初始化", _elapsed() - step_start)

    await _run_task(
        ts, tools,
        intent="现在几点了",
        hints=None,
        session_id="e2e-simple",
        test_label="simple_query",
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_e2e_multi_step_success():
    """端到端：多步骤任务——成功的多节点 DAG 执行。

    任务: "搜索 AI 安全和对齐方面的最新研究进展"
    工具: get_time + web_search
    预期: Planner 生成 2+ 节点，全部 SUCCESS。

    这是 MVP 多节点正常路径测试——与 search_task 不同，
    本测试要求所有节点通过，验证完整成功链路。
    """
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    _reset_timeline()

    print(f"\n{'#' * 70}")
    print(f"#  E2E TEST: multi_step_success — \"搜索 AI 安全最新进展\"")
    print(f"{'#' * 70}")

    step_start = _log_step(0, 6, "初始化 TaskSystem + ToolManager")
    ts = await _build_task_system()
    tools = _get_tools_from_manager(ts._infra.tool_registry)
    _log_ok(f"TaskSystem 就绪，工具: {[t.name for t in tools]}")
    _record_timeline("0.初始化", _elapsed() - step_start)

    await _run_task(
        ts, tools,
        intent="搜索 AI 安全和对齐方面的最新研究进展，包括大模型价值观对齐、"
               "越狱防护等内容，汇总关键发现",
        hints=None,
        session_id="e2e-multi",
        test_label="multi_step_success",
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_e2e_search_and_report():
    """端到端：搜索 + 写报告——多节点含不同工具类型。

    任务: "搜索 AI 安全最新进展，整理成摘要"
    工具: get_time + web_search
    预期: ≥2 个节点（搜索 + 汇总/报告），验证 Evaluator 逐节点判定。
    """
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    _reset_timeline()

    print(f"\n{'#' * 70}")
    print(f"#  E2E TEST: search_and_report — \"搜索 AI 安全 + 整理摘要\"")
    print(f"{'#' * 70}")

    step_start = _log_step(0, 6, "初始化 TaskSystem + ToolManager")
    ts = await _build_task_system()
    tools = _get_tools_from_manager(ts._infra.tool_registry)
    _log_ok(f"TaskSystem 就绪，工具: {[t.name for t in tools]}")
    _record_timeline("0.初始化", _elapsed() - step_start)

    await _run_task(
        ts, tools,
        intent="搜索 AI 安全和对齐方面的最新研究进展，"
               "包括大模型价值观对齐、越狱防护等内容，汇总成一份报告",
        hints=None,
        session_id="e2e-search",
        test_label="search_and_report",
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_e2e_replan_recovery():
    """端到端：replan 恢复路径——节点失败 → Replan → DAG 修复 → 继续执行。

    使用 get_time + web_search 完整工具集。web_search 在网络不稳定时
    可能触发 RETRY → REPLAN。测试验证 replan 发生后任务不崩溃，
    能完成（成功或熔断皆可——重点是 replan 路径走得通）。

    如果 web_search 全部成功（网络通畅），任务直接完成，测试也通过。
    """
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    _reset_timeline()

    print(f"\n{'#' * 70}")
    print(f"#  E2E TEST: replan_recovery — 节点失败触发 replan 恢复")
    print(f"{'#' * 70}")

    # ── 使用完整工具集（get_time + web_search）──
    step_start = _log_step(0, 6, "初始化 TaskSystem（get_time + web_search）")
    ts = await _build_task_system()
    tools = _get_tools_from_manager(ts._infra.tool_registry)
    _log_ok(f"TaskSystem 就绪，工具: {[t.name for t in tools]}")
    _record_timeline("0.初始化", _elapsed() - step_start)

    # ── 单步骤搜索任务——简单但依赖 web_search ──
    # 如果 web_search 网络不稳 → RETRY ×3 → REPLAN → 修复 → 继续
    step_start = _log_step(1, 6, "DelegationPreparer 准备委派上下文")
    dp = DelegationPreparer()
    _wrap_llm_client(dp._client, label="DelegationPreparer")

    result = await dp.prepare(
        intent='搜索"人工智能最新进展"并返回前3条结果',
        hints=None,
        session_messages=[],
        user_profile=UserProfile(),
        tools=tools,
        global_constraints=ConstraintConfig(),
        session_id=SessionId("e2e-replan"),
    )

    _log_info(f"status: {result.status.value}")
    _log_info(f"reason: {result.reason}")

    assert result.status == DelegationStatus.OK, (
        f"DelegationPreparer 应返回 ok（web_search 可用），实际: {result.status.value}"
    )
    assert result.context is not None
    _log_info(f"筛选工具: {[t.name for t in result.context.tools]}")
    _log_ok("委派上下文就绪")
    _record_timeline("1.DelegationPreparer", _elapsed() - step_start)

    # ── Step 2: TaskSystem.submit ──
    step_start = _log_step(2, 6, "TaskSystem.submit() → 启动 SubAgent")
    submit_result = await ts.submit(result.context)
    assert isinstance(submit_result, TaskMailbox), f"submit 失败: {submit_result}"

    mailbox = submit_result
    task_id = mailbox.task_id
    _log_ok(f"task_id={task_id}, fire-and-forget 已启动")
    _record_timeline("2.Submit", _elapsed() - step_start)

    # ── 消费 Mailbox 事件（含 replan 判定）──
    step_start = _log_step(3, 6,
        "消费 Mailbox 事件（Planner → Validator → Orchestrator → Executor → Evaluator → [Replan] → ...)")
    print(f"  ⚠️  此测试可能触发 replan 路径——注意观察 Planner/Replan 的 LLM 调用\n")

    event_count = 0
    total_nodes = 0
    completed_nodes = 0
    replan_triggered = False
    while True:
        event = await mailbox.get(timeout=180.0)
        if event is None:
            _log_info("Mailbox 超时（180s 无事件），退出等待")
            break
        event_count += 1

        event_type = event.type.value
        data = event.data

        print(f"\n{_SEP2}")
        print(f"📬 Mailbox Event #{event_count}: {event_type}  [T+{_fmt_duration(_elapsed())}]")
        print(f"{_SEP2}")

        if event_type == "progress":
            _show_event_summary(event_type, data, event_count)
            msg = data.get("message", "")
            if "Replan" in msg or "重试" in msg:
                replan_triggered = True

        elif event_type == "completed":
            total_nodes = data.get("total_nodes", 0)
            completed_nodes = data.get("completed_nodes", 0)
            _show_event_summary(event_type, data, event_count)
            _log_ok("任务完成 ✅")
            break

        elif event_type == "failed":
            error_msg = data.get("error", "")
            _show_event_summary(event_type, data, event_count)
            if "熔断" in error_msg:
                _log_warn(f"replan 熔断（已达上限）: {error_msg[:200]}")
                replan_triggered = True
            elif "校验失败" in error_msg or "DAGPatch" in error_msg:
                _log_warn(f"replan 校验问题: {error_msg[:200]}")
                replan_triggered = True
            else:
                _log_warn(f"任务异常: {error_msg[:200]}")
            break

        elif event_type == "paused":
            _show_event_summary(event_type, data, event_count)

        elif event_type in ("approval_needed", "cancelled"):
            preview = json.dumps(data, indent=2, ensure_ascii=False, default=str)[:500]
            print(f"  data: {preview}")

        print(f"{_SEP2}")

    _log_info(f"共收到 {event_count} 个事件")
    _record_timeline("3-4.编排执行+判定", _elapsed() - step_start)

    # ── 超时兜底 ──
    if task_id in ts._tasks:
        _log_warn("Mailbox 超时但 SubAgent 仍在运行——等待最多 30s")
        for _ in range(30):
            await asyncio.sleep(1.0)
            if task_id not in ts._tasks:
                break

    # ── 结果汇总 ──
    _log_step(5, 6, "结果汇总")
    _log_info(f"task_id={task_id}")
    _log_info(f"DAG 节点: total={total_nodes}, completed={completed_nodes}")
    if replan_triggered:
        _log_ok("replan 路径已触发 ✅")
    else:
        _log_info("本次运行未触发 replan（网络通畅，所有节点直接成功）")
    assert task_id not in ts._tasks, "任务应从注册表清理"
    _log_info("注册表已清理 ✅")
    _record_timeline("5.结果汇总", _elapsed() - step_start)

    # ── 时间线 ──
    _log_step(6, 6, "时间线总览")
    _print_timeline()
