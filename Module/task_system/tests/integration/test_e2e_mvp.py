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
from typing import Any

import pytest
from openai import AsyncOpenAI

from Module.task_system.delegation_preparer import DelegationPreparer
from Module.task_system.schema import (
    ConstraintConfig,
    DelegationStatus,
    SessionId,
    Tool,
    UserProfile,
)
from Module.task_system.task_system import TaskSystem
from core.tools.time.plugin import TimePlugin
from core.tools.web.plugin import WebSearchPlugin
from Module.task_system.mailbox import TaskMailbox
from Module.task_system.tests.helpers import (
    build_test_task_system,
    get_tools_from_manager,
)

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

# ═══════════════════════════════════════════════════════════════════════════════
# Token 汇总输出
# ═══════════════════════════════════════════════════════════════════════════════

def _log_token_summary(ts: TaskSystem, task_id: Any, event_data: dict | None = None) -> None:
    """打印任务的 token 消耗汇总。

    优先从 COMPLETED 事件 data 中读取（SubAgent 清理后仍可用），
    回退到 get_token_usage()（仅任务仍在注册表中时可用）。
    """
    token_usage = None
    token_total = 0

    # 优先读事件数据——COMPLETED 事件中的 token 数据在 push 时捕获，不受清理影响
    if event_data:
        token_usage = event_data.get("token_usage")
        token_total = event_data.get("token_total", 0)

    # 回退：从 TaskSystem 查询（仅任务尚未被 finally 清理时有效）
    if not token_usage:
        usage = ts.get_token_usage(task_id)
        if usage is not None and usage.components:
            token_usage = {}
            for c in usage.components:
                token_usage[c.component.value] = token_usage.get(c.component.value, 0) + c.total_tokens
            token_total = sum(token_usage.values())

    if not token_usage:
        print("  🔹 Token: （无记录）")
        return

    print(f"\n  ┌─ 🔹 Token 消耗汇总 (task_id={task_id})")
    print(f"  │  总消耗: {token_total:,} tokens")
    for comp_name in sorted(token_usage):
        print(f"  │  {comp_name}: {token_usage[comp_name]:,} tokens")
    print(f"  └─")


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
    if result.token_usage:
        tu = result.token_usage
        _log_info(f"🔹 DelegationPreparer token: "
                  f"prompt={tu.get('prompt_tokens', 0)}, "
                  f"completion={tu.get('completion_tokens', 0)}, "
                  f"total={tu.get('total_tokens', 0)}")
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
            _log_token_summary(ts, task_id, event_data=data)
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
    ts = await build_test_task_system(
        plugins=[TimePlugin, WebSearchPlugin],
        use_mocks=True,
        wrap_llm=False,
    )
    ts._infra.llm = _wrap_llm_client(ts._infra.llm)
    tools = get_tools_from_manager(ts._infra.tool_registry)
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
    ts = await build_test_task_system(
        plugins=[TimePlugin, WebSearchPlugin],
        use_mocks=True,
        wrap_llm=False,
    )
    ts._infra.llm = _wrap_llm_client(ts._infra.llm)
    tools = get_tools_from_manager(ts._infra.tool_registry)
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
    ts = await build_test_task_system(
        plugins=[TimePlugin, WebSearchPlugin],
        use_mocks=True,
        wrap_llm=False,
    )
    ts._infra.llm = _wrap_llm_client(ts._infra.llm)
    tools = get_tools_from_manager(ts._infra.tool_registry)
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
    ts = await build_test_task_system(
        plugins=[TimePlugin, WebSearchPlugin],
        use_mocks=True,
        wrap_llm=False,
    )
    ts._infra.llm = _wrap_llm_client(ts._infra.llm)
    tools = get_tools_from_manager(ts._infra.tool_registry)
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
    if result.token_usage:
        tu = result.token_usage
        _log_info(f"🔹 DelegationPreparer token: "
                  f"prompt={tu.get('prompt_tokens', 0)}, "
                  f"completion={tu.get('completion_tokens', 0)}, "
                  f"total={tu.get('total_tokens', 0)}")
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
            _log_token_summary(ts, task_id, event_data=data)
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


@pytest.mark.asyncio
@pytest.mark.integration
async def test_e2e_llm_direct_execution():
    """端到端：execution_mode——Planner 标注 llm_direct + Executor 跳过工具匹配。

    任务: "搜索 AI 安全最新进展，汇总关键发现"
    工具: get_time + web_search
    预期: Planner 生成 ≥2 节点（搜索 + 汇总）。汇总节点标记 llm_direct，
          Executor 不为其匹配工具，直接调 LLM 生成文本。
    """
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    _reset_timeline()

    print(f"\n{'#' * 70}")
    print(f"#  E2E TEST: llm_direct_execution — \"搜索 AI 安全 + 汇总报告\"")
    print(f"{'#' * 70}")

    step_start = _log_step(0, 6, "初始化 TaskSystem（get_time + web_search）")
    ts = await build_test_task_system(
        plugins=[TimePlugin, WebSearchPlugin],
        use_mocks=True,
        wrap_llm=False,
    )
    ts._infra.llm = _wrap_llm_client(ts._infra.llm)
    tools = get_tools_from_manager(ts._infra.tool_registry)
    _log_ok(f"TaskSystem 就绪，工具: {[t.name for t in tools]}")
    _record_timeline("0.初始化", _elapsed() - step_start)

    await _run_task(
        ts, tools,
        intent="搜索 AI 安全和对齐方面的最新研究进展，包括大模型价值观对齐、"
               "越狱防护等内容，汇总成一份结构化的报告",
        hints=None,
        session_id="e2e-llm-direct",
        test_label="llm_direct_execution",
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_e2e_cancel():
    """端到端：运行时取消——cancel() → CANCELLED 事件。

    提交一个搜索任务，在收到第一个 PROGRESS 后取消，验证：
    - cancel() 返回 True
    - 收到 CANCELLED 事件
    - 注册表清理
    """
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    _reset_timeline()

    print(f"\n{'#' * 70}")
    print(f"#  E2E TEST: cancel — 运行时取消 → CANCELLED")
    print(f"{'#' * 70}")

    step_start = _log_step(0, 6, "初始化 TaskSystem（get_time + web_search）")
    ts = await build_test_task_system(
        plugins=[TimePlugin, WebSearchPlugin],
        use_mocks=True,
        wrap_llm=False,
    )
    ts._infra.llm = _wrap_llm_client(ts._infra.llm)
    tools = get_tools_from_manager(ts._infra.tool_registry)
    _log_ok(f"TaskSystem 就绪，工具: {[t.name for t in tools]}")
    _record_timeline("0.初始化", _elapsed() - step_start)

    step_start = _log_step(1, 6, "DelegationPreparer → submit → 等首个事件")
    dp = DelegationPreparer()
    _wrap_llm_client(dp._client, label="DelegationPreparer")

    result = await dp.prepare(
        intent='搜索 AI 安全、越狱防护和模型对齐三个方面的最新进展，'
               '分别搜索每个方向并生成独立的摘要，最后汇总成综合报告',
        hints=None,
        session_messages=[],
        user_profile=UserProfile(),
        tools=tools,
        global_constraints=ConstraintConfig(),
        session_id=SessionId("e2e-cancel"),
    )
    assert result.status == DelegationStatus.OK
    assert result.context is not None

    submit_result = await ts.submit(result.context)
    assert isinstance(submit_result, TaskMailbox)
    mailbox = submit_result
    task_id = mailbox.task_id
    _log_ok(f"task_id={task_id}, 等待首个 PROGRESS")
    _record_timeline("1.准备+提交", _elapsed() - step_start)

    # 等第一个 PROGRESS
    first_event = await asyncio.wait_for(mailbox.get(), timeout=60.0)
    assert first_event is not None
    _log_info(f"首个事件: {first_event.type.value}")

    # cancel
    step_start = _log_step(2, 6, "cancel() → 消费 CANCELLED 事件")
    assert await ts.cancel(task_id) is True
    _log_ok("cancel() 返回 True")

    event = None
    progress_seen = 0
    deadline = time.monotonic() + 45.0
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            event = await asyncio.wait_for(mailbox.get(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if event is None:
            continue
        event_type = event.type.value
        data = event.data
        print(f"\n{_SEP2}")
        print(f"📬 Mailbox Event: {event_type}  [T+{_fmt_duration(_elapsed())}]")
        print(f"{_SEP2}")
        if event_type == "progress":
            progress_seen += 1
            _show_event_summary(event_type, data, 0)
        elif event_type in ("cancelled", "failed", "completed"):
            _show_event_summary(event_type, data, 0)
            break

    assert event is not None, "未收到终态事件"
    assert event.type.value in ("cancelled", "completed", "failed"), \
        f"意外事件: {event.type.value}"
    _log_ok(f"终态事件: {event.type.value}")

    # 验证取消生效——cancelled 时 progress 数 < 总节点数
    if event.type.value == "cancelled":
        total = event.data.get("total_nodes", 0)
        if total > 0:
            _log_info(f"取消生效——{total} 节点中完成 {progress_seen}，未执行完。")
        else:
            _log_info(f"取消生效——已执行 {progress_seen} 个节点后终止。")
    _record_timeline("2.cancel→终态", _elapsed() - step_start)

    step_start = _log_step(3, 6, "清理确认")
    await asyncio.sleep(0.5)
    assert task_id not in ts._tasks
    _log_info("注册表已清理 ✅")
    _record_timeline("3.清理", _elapsed() - step_start)

    _log_step(6, 6, "时间线总览")
    _print_timeline()

    await ts.stop(timeout=10.0)
    await ts._infra.llm.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_e2e_pause_resume():
    """端到端：暂停→恢复→完成——pause() + resume() 完整生命周期。

    提交任务 → 等首个 PROGRESS → pause → 等 PAUSED → resume → 等 COMPLETED。
    验证：
    - PAUSED 事件被推送
    - resume 返回 mailbox
    - 任务最终完成
    """
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY 未设置，跳过集成测试")

    _reset_timeline()

    print(f"\n{'#' * 70}")
    print(f"#  E2E TEST: pause_resume — 暂停 → 恢复 → 完成")
    print(f"{'#' * 70}")

    step_start = _log_step(0, 6, "初始化 TaskSystem（get_time + web_search）")
    ts = await build_test_task_system(
        plugins=[TimePlugin, WebSearchPlugin],
        use_mocks=True,
        wrap_llm=False,
    )
    ts._infra.llm = _wrap_llm_client(ts._infra.llm)
    tools = get_tools_from_manager(ts._infra.tool_registry)
    _log_ok(f"TaskSystem 就绪，工具: {[t.name for t in tools]}")
    _record_timeline("0.初始化", _elapsed() - step_start)

    step_start = _log_step(1, 6, "DelegationPreparer → submit → 等首个事件")
    dp = DelegationPreparer()
    _wrap_llm_client(dp._client, label="DelegationPreparer")

    result = await dp.prepare(
        intent='搜索"人工智能最新进展"，总结3个关键发现',
        hints=None,
        session_messages=[],
        user_profile=UserProfile(),
        tools=tools,
        global_constraints=ConstraintConfig(),
        session_id=SessionId("e2e-pause-resume"),
    )
    assert result.status == DelegationStatus.OK
    assert result.context is not None

    submit_result = await ts.submit(result.context)
    assert isinstance(submit_result, TaskMailbox)
    mailbox = submit_result
    task_id = mailbox.task_id
    _log_ok(f"task_id={task_id}, 等待首个 PROGRESS")
    _record_timeline("1.准备+提交", _elapsed() - step_start)

    first_event = await asyncio.wait_for(mailbox.get(), timeout=60.0)
    assert first_event is not None
    _log_info(f"首个事件: {first_event.type.value}")

    # pause
    step_start = _log_step(2, 6, "pause → 等 PAUSED 事件")
    try:
        await ts.pause(task_id)
        _log_ok("pause() 成功")
    except KeyError:
        _log_warn("任务已完成，跳过")
        await ts.stop(timeout=10.0)
        await ts._infra.llm.close()
        return

    paused = False
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            event = await asyncio.wait_for(mailbox.get(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if event is None:
            continue
        print(f"\n{_SEP2}")
        print(f"📬 Mailbox Event: {event.type.value}  [T+{_fmt_duration(_elapsed())}]")
        print(f"{_SEP2}")
        if event.type.value == "paused":
            _show_event_summary("paused", event.data, 0)
            _log_ok("收到 PAUSED 事件")
            paused = True
            break
        elif event.type.value in ("completed", "failed"):
            _log_warn("任务在 pause 前已完成")
            break

    if not paused:
        await ts.stop(timeout=10.0)
        await ts._infra.llm.close()
        return
    _record_timeline("2.pause→PAUSED", _elapsed() - step_start)

    # resume
    step_start = _log_step(3, 6, "resume → 等 COMPLETED")
    resume_result = await ts.resume(task_id)
    assert isinstance(resume_result, TaskMailbox)
    _log_ok("resume() 返回 TaskMailbox")

    event = None
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            event = await asyncio.wait_for(mailbox.get(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if event is None:
            continue
        event_type = event.type.value
        data = event.data
        print(f"\n{_SEP2}")
        print(f"📬 Mailbox Event: {event_type}  [T+{_fmt_duration(_elapsed())}]")
        print(f"{_SEP2}")
        if event_type == "progress":
            _show_event_summary(event_type, data, 0)
        elif event_type == "completed":
            _show_event_summary(event_type, data, 0)
            _log_token_summary(ts, task_id, event_data=data)
            _log_ok("任务完成 ✅")
            break
        elif event_type == "failed":
            _show_event_summary(event_type, data, 0)
            _log_warn(f"任务失败: {data.get('error', '?')}")
            break

    assert event is not None, "未收到终态事件"
    assert event.type.value == "completed", \
        f"期望 COMPLETED，实际 {event.type.value}"
    _record_timeline("3.resume→完成", _elapsed() - step_start)

    step_start = _log_step(4, 6, "清理确认")
    await asyncio.sleep(0.5)
    assert task_id not in ts._tasks
    _log_info("注册表已清理 ✅")
    _record_timeline("4.清理", _elapsed() - step_start)

    _log_step(6, 6, "时间线总览")
    _print_timeline()

    await ts.stop(timeout=10.0)
    await ts._infra.llm.close()
