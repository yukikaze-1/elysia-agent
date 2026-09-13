"""#43 回归：状态单一权威化。

两项"同一语义多个真相源"（报告 §「State 所有权」）：

**A. `Agent._state` ↔ 状态层**
原先 `Agent._state` 是普通实例属性、状态层只投影 FSM——两份互不相干的存储，靠"记得
同步"维持。停机窗口里正好没人记得，于是 CR-08（`ACK accepted` + 消息永久丢失）。
现在真实存储只有 `AgentStatus.run_state` 一处，`Agent._state` 是它的视图。

**B. 服务开关**
"TTS/STT 是否启用"原先存在四处：会话 `service_state`（权威）、Agent 内存槽位
`_stt_enabled`/`_tts_enabled`、`config.*.enabled`、`TTSStreamManager.enabled`。
现在收敛为：**权威** = 会话三态字段（None 回退 config）；**派生** = `agent.*_enabled`
现算；**投影** = TTS 管理器标志（唯一写点 `_sync_tts_gate`）。
"""
from __future__ import annotations

import asyncio
import ast
import pathlib
from unittest import mock

import pytest

from core.agent.agent import Agent
from core.agent._lifecycle_services import _LifecycleServices
from core.schema.service_state import ServiceState
from core.status import FSMBackedStatus
from core.status.agent_fsm import AgentFSM
from core.status.protocol import RunState
from core.schema import AgentState

pytestmark = pytest.mark.unit


def _real_agent() -> Agent:
    """`object.__new__(Agent)` 真身替身——property 会真的执行（MagicMock 不会）。"""
    a = object.__new__(Agent)
    a._session_manager = mock.MagicMock()
    a._session_manager.active = mock.MagicMock()
    a._session_manager.active.save = mock.AsyncMock()
    a._config = mock.MagicMock()
    a._config.stt.enabled = False
    a._config.tts.enabled = False
    a._cognitive = mock.MagicMock()
    a._cognitive._tts_mgr.set_enabled = mock.AsyncMock()
    a._tts = None
    return a


# ══════════════════════════════════════════════════════════════════
# A. 生命周期轴：`Agent._state` 是状态层的视图（不可能分叉）
# ══════════════════════════════════════════════════════════════════

class TestRunStateSingleAuthority:
    """`Agent._state` 与 `AgentStatus.run_state` 必须是**同一个值**。"""

    def test_state_is_a_property_not_a_second_slot(self):
        """结构性：`_state` 必须是 property（有 setter），不能是普通实例属性。

        修复前 `Agent.__init__` 里 `self._state = RunState.STOPPED` 建的是一个**独立
        存储**；状态层另有一份。两份各自演化 → 停机窗口内 `process_message` 读到的
        与事件消费者读到的不是同一个值（CR-08）。
        """
        prop = next((vars(klass)["_state"] for klass in Agent.__mro__
                     if "_state" in vars(klass)), None)
        assert isinstance(prop, property), (
            "Agent._state 不再是 property——生命周期轴又变成了两份存储")
        assert prop.fset is not None, "Agent._state 只读，production 写点无处落脚"

    def test_real_agent_state_and_status_cannot_diverge(self):
        """真实 Agent：写 `_state` 立即反映到 `status.run_state`，反之亦然。"""
        agent = _real_agent()
        agent.status = FSMBackedStatus(AgentFSM())
        assert agent._state is agent.status.run_state

        for value in (RunState.RUNNING, RunState.STOPPING,
                      RunState.RESTARTING, RunState.STOPPED):
            agent._state = value
            assert agent.status.run_state is value, "写入 _state 未落到状态层"
            assert agent.state is value, "公开读口 agent.state 与实际值不一致"

    def test_test_double_fallback_is_consistent(self):
        """测试替身（无 `status`）走显式回退，不抛异常也不静默返回错值。"""
        agent = object.__new__(Agent)
        assert agent._state is RunState.STOPPED      # 未接线 → 默认 STOPPED
        agent._state = RunState.RUNNING
        assert agent._state is RunState.RUNNING

    def test_default_run_state_is_stopped_for_a_fresh_fsm_status(self):
        """`FSMBackedStatus` 独立构造默认 RUNNING（生命周期轴不适用），显式置位生效。"""
        status = FSMBackedStatus(AgentFSM())
        assert status.run_state is RunState.RUNNING
        status.set_run_state(RunState.STOPPING)
        assert status.run_state is RunState.STOPPING

    def test_lifecycle_and_phase_predicates_are_distinct(self):
        """两个轴正交，谓词语义不同——"拒收"与"排队"是两种处置。

        `is_service_running`（生命周期）与 `is_accepting_input`（相位）必须能分别回答；
        `accepts_new_turns` 是二者的合取，供"做/不做"型入口使用。
        """
        status = FSMBackedStatus(AgentFSM())
        assert status.is_service_running is True
        assert status.is_accepting_input is True
        assert status.accepts_new_turns is True

        # 停机：生命周期未就绪，相位仍 IDLE
        status.set_run_state(RunState.STOPPED)
        assert status.is_service_running is False
        assert status.is_accepting_input is True, "相位轴不该被生命周期污染"
        assert status.accepts_new_turns is False

    def test_accepts_new_turns_requires_both_axes(self):
        """合取谓词的两半都要真的生效（任一轴不满足 → False）。"""
        status = FSMBackedStatus(AgentFSM())
        status.set_run_state(RunState.STOPPING)
        assert status.accepts_new_turns is False

        status.set_run_state(RunState.RUNNING)
        status._fsm.transition(AgentState.THINKING)
        assert status.is_service_running is True
        assert status.accepts_new_turns is False, "相位忙时不得放行新回合"


# ══════════════════════════════════════════════════════════════════
# B. 服务开关：派生 + 唯一写点
# ══════════════════════════════════════════════════════════════════

class TestServiceFlagSingleAuthority:
    """权威 = 会话 `service_state`；`agent.*_enabled` 现算；TTS 管理器标志是投影。"""

    def test_slots_no_longer_exist_on_agent(self):
        """结构性：Agent 上不得再有 `_stt_enabled` / `_tts_enabled` 实例槽位。"""
        agent = _real_agent()
        assert not hasattr(agent, "_stt_enabled"), "内存槽位复活——又变成两个真相源"
        assert not hasattr(agent, "_tts_enabled"), "内存槽位复活——又变成两个真相源"

    def test_effective_value_follows_session_without_any_sync(self):
        """改权威字段 → 派生属性立即跟随（无需任何"同步"动作）。

        这正是原缺陷的场景：切会话后槽位保持上一个会话的值，而 `service_state`
        已经是新会话的值。派生之后不存在"忘了同步"这一状态。
        """
        agent = _real_agent()
        ss = ServiceState()
        agent._session_manager.active.service_state = ss
        assert agent.stt_enabled is False       # 全 None → config 默认

        ss.stt_enabled = True                   # 只改权威
        assert agent.stt_enabled is True        # 派生值立即跟随

        ss.tts_enabled = True
        agent._config.tts.enabled = False
        assert agent.tts_enabled is True        # 显式值压过 config

        ss.tts_enabled = None
        assert agent.tts_enabled is False       # None → 回退 config

    def test_tts_gate_has_exactly_one_write_point(self):
        """TTS 管理器标志只能由 `_sync_tts_gate` 写——写点唯一，投影不会漏刷。"""
        agent = _real_agent()
        agent._session_manager.active.service_state = ServiceState(tts_enabled=True)

        asyncio.run(_LifecycleServices._sync_tts_gate(agent))

        agent._cognitive._tts_mgr.set_enabled.assert_awaited_once_with(True)  # noqa: SLF001

    def test_apply_service_enabled_only_syncs_gate(self):
        """`_apply_service_enabled` 不再写任何内存槽位（只刷新投影）。"""
        agent = _real_agent()
        agent._session_manager.active.service_state = ServiceState(
            stt_enabled=True, tts_enabled=False)

        asyncio.run(_LifecycleServices._apply_service_enabled(agent))

        assert agent.stt_enabled is True
        assert agent.tts_enabled is False
        agent._cognitive._tts_mgr.set_enabled.assert_awaited_once_with(False)  # noqa: SLF001

    def test_derivation_point_is_unique_in_production_code(self):
        """结构性：三态回退只能在 `_effective_service_enabled` 里写一次。

        原先同一段 `x if x is not None else config.y` 在 `_apply_service_enabled`、
        `_start_tts`、`set_service`、`agent.*_enabled` property 里各写一遍——四处推导
        就是四个漂移点。本用例把"只允许一处"变成门禁。

        判据是 **AST**（`IfExp` + `test` 为 `IsNot None` 且 `left` 是 `*_enabled` 属性
        + 分支里出现 `config`）——不用子串匹配：`ast.dump` 把 `is not` 渲染成 `IsNot()`，
        按源码文本找 `"_enabled is not None"` 会**恒不命中**（本用例第一版就是这样
        假绿的，注入复现时才发现）。
        """
        allowed_fn = "_effective_service_enabled"

        def _is_three_state_fallback(node: ast.AST) -> bool:
            if not isinstance(node, ast.IfExp):
                return False
            test = node.test
            if not (isinstance(test, ast.Compare)
                    and len(test.ops) == 1 and isinstance(test.ops[0], ast.IsNot)
                    and len(test.comparators) == 1
                    and isinstance(test.comparators[0], ast.Constant)
                    and test.comparators[0].value is None):
                return False
            left = test.left
            if not (isinstance(left, ast.Attribute) and left.attr.endswith("_enabled")):
                return False
            branches = ast.dump(ast.Module(body=[node.body, node.orelse], type_ignores=[]))
            return "config" in branches

        offenders: list[str] = []

        def _walk(node: ast.AST, fn_name: str | None, path: str) -> None:
            for child in ast.iter_child_nodes(node):
                name = fn_name
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = child.name
                if _is_three_state_fallback(child) and name != allowed_fn:
                    offenders.append(f"{path}:{getattr(child, 'lineno', '?')} (in {name})")
                _walk(child, name, path)

        for path in sorted(pathlib.Path("core").rglob("*.py")):
            if "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            _walk(tree, None, str(path))

        assert not offenders, (
            f"三态回退在 `{allowed_fn}` 之外又被推导了一遍（每多一处就多一个漂移点）：\n  "
            + "\n  ".join(offenders))
