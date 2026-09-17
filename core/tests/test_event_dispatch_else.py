# 测试文件：core/tests/test_event_dispatch_else.py
"""S3-23 / RC-7：未登记的事件类型**必须可见**，不能静默丢弃。

报告的根因簇 RC-7 是「`else` 分支与三态区分的系统性缺失」——`_dispatch_event` 原先
是 `if/elif` 链**没有 else**：未登记的类型落到函数末尾被丢弃，**零日志、零计数**。
于是"发出去没人处理"与"处理成功"在观测面上**完全一样**。

真实现场是 `EventBusHITLNotifier.request_approval`：它 post `type="hitl_request"`，
而判别联合里没有这个类型 → 审批卡片永远不会出现，也没有任何提示。该链路在 v1
**不可达**（`needs_approval()` 无条件 `return False`），属潜伏缺陷——本文件把两件事
钉住：① 丢弃必须可见；② 那三处断裂必须仍被登记（接通之前不许被当成"已实现"）。
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.service


class TestUnhandledEventIsVisible:
    @pytest.mark.asyncio
    async def test_unknown_type_logs_warning_and_counts(self, caplog):
        """**核心**：未登记类型 → warning + 计数（而不是静默丢弃）。"""
        from core.agent.agent import Agent

        agent = Agent.__new__(Agent)
        agent._unhandled_events = 0                     # noqa: SLF001

        class _Unknown:
            """不在判别联合里的类型——模拟 `hitl_request` 那种"发了没人收"。"""

        with caplog.at_level(logging.WARNING):
            await agent._dispatch_event(_Unknown())     # noqa: SLF001

        assert agent._unhandled_events == 1             # noqa: SLF001
        assert any(r.levelno >= logging.WARNING and "未登记的事件类型" in r.getMessage()
                   for r in caplog.records), "丢弃必须在默认级别可见"

    @pytest.mark.asyncio
    async def test_known_types_do_not_count(self):
        """回归网：已登记类型不触发计数（否则计数失去意义）。"""
        from core.agent.agent import Agent
        from core.schema import InterruptData

        agent = Agent.__new__(Agent)
        agent._unhandled_events = 0                     # noqa: SLF001
        agent._handle_interrupt = MagicMock()           # noqa: SLF001

        await agent._dispatch_event(InterruptData(source="user"))   # noqa: SLF001

        assert agent._unhandled_events == 0             # noqa: SLF001
        agent._handle_interrupt.assert_called_once()    # noqa: SLF001

    def test_dispatcher_has_else_branch(self):
        """结构性：`_dispatch_event` 必须有 `else`——否则又回到"静默丢弃"。"""
        import ast
        import pathlib

        src = pathlib.Path("core/agent/agent.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "_dispatch_event")
        # 最后一个语句必须是带 else 的 if
        stmt = fn.body[-1]
        assert isinstance(stmt, ast.If) and stmt.orelse, (
            "_dispatch_event 没有 else 分支——未登记事件会被静默丢弃（RC-7）")


class TestHitlLatentBreaksStayDocumented:
    """v1 不可达的潜伏缺陷：接通 workflow HITL 之前，这三处断裂必须仍在文档里。

    把它们写成断言的理由：这类"不可达缺陷"最容易被误读——原 docstring 自称
    "⚠️ 演示代码——不直接用于生产"，而它**就是生产装配用的那一个**
    （`_lifecycle_task_system` 直接注入 WorkflowEngine）。
    """

    def test_notifier_docstring_records_breaks_and_unreachability(self):
        import pathlib

        src = pathlib.Path("core/hitl.py").read_text(encoding="utf-8")
        idx = src.index("class EventBusHITLNotifier")
        doc = src[idx:idx + 2000]
        assert "S3-23" in doc, "三处断裂未登记"
        assert "v1 不可达" in doc, "必须写明当前没有触发路径"
        assert "已注入生产 WorkflowEngine" in doc, (
            "必须纠正「演示代码」的旧自述——它其实就在生产装配里")

    def test_no_mailbox_fallback_claim(self):
        """不得再声称"Mailbox 审计流兜底"——本类构造只收 bus，没有那个兜底。"""
        import pathlib

        src = pathlib.Path("core/hitl.py").read_text(encoding="utf-8")
        # 只检查**断言性语句**，不检查解释性注释（注释里正是引用旧说法的地方）
        code_lines = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
        offenders = [ln for ln in code_lines
                     if "Mailbox 审计流兜底" in ln and "# S3-23" not in ln]
        assert not offenders, (
            f"仍声称「Mailbox 审计流兜底」——该说法不成立（构造只收 bus）：{offenders}")

    def test_approval_result_routing_is_still_the_wrong_domain(self):
        """第 3 处断裂仍存在：workflow 的 approval 回传查的是 SafetyPipeline 的表。

        这条**故意断言缺陷仍在**：修复它需要接通整条链路（v1 不可达时代码只会腐烂）。
        等真正启用 workflow HITL 时，这条会提醒"先修这里"。
        """
        import pathlib

        src = pathlib.Path("core/hitl.py").read_text(encoding="utf-8")
        idx = src.index("class EventBusHITLNotifier")
        assert "_hitl_requests" in src[idx:idx + 4000] or "错误的审批域" in src[idx:idx + 2000], (
            "审批域不匹配这件事必须留在注释里（它是接通前的必读项）")
