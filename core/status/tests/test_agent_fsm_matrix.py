# 测试文件：core/status/tests/test_agent_fsm_matrix.py
"""AgentFSM 转换矩阵全量覆盖测试（CODE_REVIEW_REPORT #46 / 缺口行 #5）。

## 背景（缺口）

既有 ``core/status/tests/test_agent_fsm.py`` 覆盖了 14 条合法边与 1 个对角格，但：

- 6 个对角（同状态）格里只覆盖 1 个；
- 16 个非法格里只覆盖 1 个，其余 0 覆盖；
- **没有任何用例断言「被拒绝的转换不得改动 state / previous」**——把实现改成
  「先赋值、后校验」，既有用例仍然全绿。

## 做法

期望值**全部由生产源头现算**（``AgentState`` 枚举 + ``core.schema.agent.VALID_TRANSITIONS``），
不手抄矩阵，因此新增状态 / 新增边会自动扩展参数化覆盖。同时用两道守卫防止
「矩阵被改窄 → 参数化静默变空 → 假绿」：

1. ``test_matrix_snapshot_is_frozen``：冻结当前状态数 / 格数 / 合法边集合；
2. ``test_cell_partition_is_disjoint_and_complete``：三个格子分类（合法边格 / 对角格 /
   非法格）必须无交且覆盖全部格子；一旦新增自环或漏分类立即红。

前置状态一律用**公开 API**（沿合法边 BFS 驱动）构造，不触碰 ``_state`` / ``_previous``
私有属性。
"""

import pytest
from core.schema import VALID_TRANSITIONS, AgentState
from core.status.agent_fsm import AgentFSM

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 矩阵推导（唯一期望值来源：生产代码）
# ---------------------------------------------------------------------------

_STATES: tuple[AgentState, ...] = tuple(AgentState)
"""状态全集——顺序取自枚举定义顺序，数量由枚举现算。"""

_MATRIX: dict[AgentState, frozenset[AgentState]] = {
    state: frozenset(VALID_TRANSITIONS.get(state, set())) for state in _STATES
}
"""合法边关系，与 ``AgentFSM.can_transition`` 同语义（``.get(..., set())`` 兜底）。"""

_ALL_CELLS: list[tuple[AgentState, AgentState]] = [(a, b) for a in _STATES for b in _STATES]
"""全状态空间格——states × states。"""

_DIAGONAL_CELLS: list[tuple[AgentState, AgentState]] = [(a, a) for a in _STATES]
"""对角（同状态）格——``transition`` 走幂等短路分支。"""

_EDGE_CELLS: list[tuple[AgentState, AgentState]] = [
    (a, b) for a, b in _ALL_CELLS if b in _MATRIX[a]
]
"""``VALID_TRANSITIONS`` 里显式登记的合法边格。"""

_ILLEGAL_CELLS: list[tuple[AgentState, AgentState]] = [
    (a, b) for a, b in _ALL_CELLS if b not in _MATRIX[a] and a != b
]
"""非法格——既不在矩阵里，也不是对角格，``transition`` 必须抛 ValueError。"""


def _reach_paths() -> dict[AgentState, tuple[AgentState, ...]]:
    """BFS：从初始状态到各状态的合法边最短路径（纯公开 API 构造前置状态）。

    Returns:
        目标状态 → 状态路径（含起点，终点为目标状态）。
    """
    start = AgentFSM().state
    paths: dict[AgentState, tuple[AgentState, ...]] = {start: (start,)}
    queue: list[AgentState] = [start]
    while queue:
        current = queue.pop(0)
        for nxt in _STATES:  # 按枚举顺序遍历，保证路径确定性
            if nxt in _MATRIX[current] and nxt not in paths:
                paths[nxt] = (*paths[current], nxt)
                queue.append(nxt)
    return paths


_PATHS = _reach_paths()


def _return_cycle(state: AgentState) -> tuple[AgentState, ...]:
    """BFS：找一条 ``state → … → state`` 的合法闭合环（``state`` 只出现在首尾）。

    Args:
        state: 环的起点与终点。

    Returns:
        闭合环路径，如 ``(IDLE, THINKING, SPEAKING, IDLE)``。

    Raises:
        KeyError: 该状态不存在非平凡闭合环时（由守卫用例显式暴露，而非静默跳过）。
    """
    queue: list[tuple[AgentState, ...]] = [(state,)]
    seen: set[tuple[AgentState, ...]] = set()
    while queue:
        path = queue.pop(0)
        for nxt in _STATES:
            if nxt not in _MATRIX[path[-1]]:
                continue
            if nxt == state and len(path) >= 2:
                return (*path, state)
            if nxt not in path:
                candidate = (*path, nxt)
                if candidate not in seen:
                    seen.add(candidate)
                    queue.append(candidate)
    raise KeyError(f"{state} 没有可用的合法闭合环")


def _fsm_in(state: AgentState) -> AgentFSM:
    """构造一个处于 ``state`` 的 FSM——沿途只走合法边。

    Args:
        state: 目标起始状态。

    Returns:
        状态为 ``state`` 的 FSM；``previous`` 为驱动路径的倒数第二站（起点为 None）。
    """
    fsm = AgentFSM()
    for hop in _PATHS[state][1:]:
        fsm.transition(hop)
    assert fsm.state == state, "前置状态构造失败"
    return fsm


def _fsm_in_with_history(state: AgentState) -> AgentFSM:
    """构造一个处于 ``state`` 且 ``previous is not None`` 的 FSM。

    先沿合法边驱动到 ``state``，再走一遍闭合环回到 ``state``，使 ``previous`` 有历史值。
    这样「被拒绝的转换不得改动 previous」的断言在**每个**起始状态下都非平凡。

    Args:
        state: 目标起始状态。

    Returns:
        状态为 ``state``、``previous`` 非 None 的 FSM。
    """
    fsm = _fsm_in(state)
    for hop in _return_cycle(state)[1:]:
        fsm.transition(hop)
    assert fsm.state == state, "闭合环未回到起始状态"
    assert fsm.previous is not None, "闭合环未给 previous 留下历史值"
    return fsm


# ---------------------------------------------------------------------------
# 冻结快照（2026-09 补齐覆盖时点的生产矩阵）
# ---------------------------------------------------------------------------

_FROZEN_STATE_COUNT = 6
_FROZEN_CELL_COUNT = 36
_FROZEN_EDGE_COUNT = 14
_FROZEN_ILLEGAL_COUNT = 16
_FROZEN_EDGES: frozenset[tuple[AgentState, AgentState]] = frozenset(
    {
        (AgentState.IDLE, AgentState.THINKING),
        (AgentState.THINKING, AgentState.EXECUTING),
        (AgentState.THINKING, AgentState.SPEAKING),
        (AgentState.THINKING, AgentState.INTERRUPTED),
        (AgentState.EXECUTING, AgentState.THINKING),
        (AgentState.EXECUTING, AgentState.INTERRUPTED),
        (AgentState.SPEAKING, AgentState.IDLE),
        (AgentState.SPEAKING, AgentState.EXECUTING),
        (AgentState.SPEAKING, AgentState.THINKING),
        (AgentState.SPEAKING, AgentState.INTERRUPTED),
        (AgentState.SPEAKING, AgentState.IDLE_BACKGROUND_TASK),
        (AgentState.INTERRUPTED, AgentState.IDLE),
        (AgentState.IDLE_BACKGROUND_TASK, AgentState.THINKING),
        (AgentState.IDLE_BACKGROUND_TASK, AgentState.INTERRUPTED),
    }
)
"""冻结快照：改动 VALID_TRANSITIONS 必须同步改这里，逼迫人工复核覆盖是否仍完整。"""

_FROZEN_MESSAGE = (
    "VALID_TRANSITIONS 与冻结快照不一致。若矩阵改动是有意的，请同步更新本文件的"
    " _FROZEN_* 常量，并复核 36 格参数化覆盖是否仍完整（不要删断言让用例变绿）。"
)


class TestMatrixDerivation:
    """矩阵推导自身的守卫——防参数化静默变空（假绿）。"""

    def test_state_space_is_non_empty(self):
        """状态空间与格集合不得为空，且格数等于状态数平方。"""
        assert len(_STATES) > 0
        assert len(_ALL_CELLS) > 0
        assert len(_ALL_CELLS) == len(_STATES) ** 2
        assert len(_EDGE_CELLS) > 0
        assert len(_ILLEGAL_CELLS) > 0
        assert len(_DIAGONAL_CELLS) == len(_STATES)

    def test_valid_transitions_covers_whole_enum(self):
        """每个枚举状态都要在 VALID_TRANSITIONS 里有条目（否则 .get 兜底会掩盖漏配）。"""
        assert set(VALID_TRANSITIONS) == set(_STATES)
        for targets in VALID_TRANSITIONS.values():
            assert set(targets) <= set(_STATES), "矩阵里出现了枚举外的状态"

    def test_cell_partition_is_disjoint_and_complete(self):
        """三类格子必须无交且并集等于全格集合（新增自环/漏分类会立刻红）。"""
        edge = set(_EDGE_CELLS)
        diagonal = set(_DIAGONAL_CELLS)
        illegal = set(_ILLEGAL_CELLS)
        assert not edge & diagonal, "合法边格与对角格重叠——矩阵新增了自环，需人工复核期望值"
        assert not edge & illegal
        assert not diagonal & illegal
        assert edge | diagonal | illegal == set(_ALL_CELLS)

    def test_every_state_is_reachable_from_initial_state(self):
        """所有状态都能从初始状态沿合法边到达（否则前置状态无法用公开 API 构造）。"""
        assert set(_PATHS) == set(_STATES)

    def test_every_state_has_legal_return_cycle(self):
        """每个状态都有合法闭合环——``_fsm_in_with_history`` 依赖它制造 previous 历史。"""
        for state in _STATES:
            cycle = _return_cycle(state)
            assert cycle[0] == state
            assert cycle[-1] == state
            assert len(cycle) >= 3

    def test_matrix_snapshot_is_frozen(self):
        """冻结快照：状态数 / 格数 / 合法边集合必须与补齐覆盖时点一致。"""
        assert len(_STATES) == _FROZEN_STATE_COUNT, _FROZEN_MESSAGE
        assert len(_ALL_CELLS) == _FROZEN_CELL_COUNT, _FROZEN_MESSAGE
        assert len(_EDGE_CELLS) == _FROZEN_EDGE_COUNT, _FROZEN_MESSAGE
        assert len(_ILLEGAL_CELLS) == _FROZEN_ILLEGAL_COUNT, _FROZEN_MESSAGE
        assert {(a, b) for a, b in _EDGE_CELLS} == set(_FROZEN_EDGES), _FROZEN_MESSAGE


# ---------------------------------------------------------------------------
# 转换矩阵：36 格全量
# ---------------------------------------------------------------------------


class TestTransitionMatrix:
    """``transition()`` 在全状态空间上的行为。"""

    @pytest.mark.parametrize(("from_state", "to_state"), _ALL_CELLS)
    def test_transition_matrix(self, from_state, to_state):
        """逐格断言：合法边成功、对角幂等 no-op、非法格抛 ValueError 且不改动状态。"""
        fsm = _fsm_in(from_state)
        before_state, before_previous = fsm.state, fsm.previous
        assert before_state == from_state, "前置状态必须是 from_state"

        if from_state == to_state:
            # 对角格：生产实现先做同状态短路（agent_fsm.transition 第 62-63 行），
            # 因此既不抛异常，也不写 previous（previous 保持驱动路径留下的历史值）。
            fsm.transition(to_state)
            assert fsm.state == from_state
            assert fsm.previous == before_previous
        elif to_state in _MATRIX[from_state]:
            fsm.transition(to_state)
            assert fsm.state == to_state
            assert fsm.previous == from_state
        else:
            with pytest.raises(ValueError) as excinfo:
                fsm.transition(to_state)
            # 文档约定的类型是 ValueError（agent_fsm.transition docstring），
            # 不是 Exception；消息里应指明被拒的 from → to。
            message = str(excinfo.value)
            assert str(from_state) in message and str(to_state) in message
            assert fsm.state == before_state, "被拒绝的转换改动了 state"
            assert fsm.previous == before_previous, "被拒绝的转换改动了 previous"

    @pytest.mark.parametrize(("from_state", "to_state"), _ILLEGAL_CELLS)
    def test_rejected_transition_keeps_state_and_previous(self, from_state, to_state):
        """非法格加强版：``previous`` 先造出非 None 历史，再断言拒绝后两者都不动。"""
        fsm = _fsm_in_with_history(from_state)
        before_state, before_previous = fsm.state, fsm.previous
        assert before_previous is not None, "本用例要求 previous 非 None 才有判别力"

        with pytest.raises(ValueError) as excinfo:
            fsm.transition(to_state)
        assert type(excinfo.value) is ValueError
        assert fsm.state == before_state, "被拒绝的转换改动了 state"
        assert fsm.previous == before_previous, "被拒绝的转换改动了 previous"

    def test_legal_cells_do_not_raise(self):
        """反向守卫：14 条合法边与 6 个对角格都不得抛任何异常。"""
        for a, b in _EDGE_CELLS + _DIAGONAL_CELLS:
            fsm = _fsm_in(a)
            fsm.transition(b)  # 不应抛异常
            assert fsm.state == b


# ---------------------------------------------------------------------------
# can_transition 矩阵：36 格全量，双向断言
# ---------------------------------------------------------------------------


class TestCanTransitionMatrix:
    """``can_transition()`` 必须在合法格上 True、其余格上 False，且与实际转换一致。"""

    @pytest.mark.parametrize(("from_state", "to_state"), _ALL_CELLS)
    def test_can_transition_matrix(self, from_state, to_state):
        """逐格断言 True/False，并与 ``transition()`` 的实际放行/拒绝交叉验证。"""
        fsm = _fsm_in(from_state)
        before_state, before_previous = fsm.state, fsm.previous
        expected = to_state in _MATRIX[from_state]

        assert fsm.can_transition(to_state) is expected
        # 查询不得有副作用
        assert fsm.state == before_state
        assert fsm.previous == before_previous

        if from_state == to_state:
            # 当前矩阵无自环 → 对角格期望 False；但 transition 由幂等短路放行。
            assert expected is False
            fsm.transition(to_state)
        elif expected:
            fsm.transition(to_state)  # 宣称可转就必须真的转得动
            assert fsm.state == to_state
        else:
            with pytest.raises(ValueError):
                fsm.transition(to_state)

    def test_can_transition_agrees_with_matrix_on_all_cells(self):
        """汇总守卫：True 的格子集合必须**恰好**等于矩阵边集合（防止空参数化或单向断言）。"""
        true_cells = set()
        false_cells = set()
        for a, b in _ALL_CELLS:
            fsm = _fsm_in(a)
            (true_cells if fsm.can_transition(b) else false_cells).add((a, b))
        assert true_cells == set(_EDGE_CELLS)
        assert true_cells, "can_transition 全为 False"
        assert false_cells, "can_transition 全为 True"
        assert true_cells | false_cells == set(_ALL_CELLS)


# ---------------------------------------------------------------------------
# interrupt：逐状态
# ---------------------------------------------------------------------------


class TestInterruptMatrix:
    """``interrupt()`` 在 6 个状态上的返回值与副作用。"""

    @pytest.mark.parametrize("from_state", _STATES)
    def test_interrupt_matrix(self, from_state):
        """能进 INTERRUPTED 的状态返回 True 并完成转换；其余状态返回 False 且原地不动。"""
        fsm = _fsm_in(from_state)
        before_state, before_previous = fsm.state, fsm.previous
        # 生产实现：IDLE 先短路返回 False（优先于矩阵判定），其余状态看
        # INTERRUPTED 是否在 VALID_TRANSITIONS[当前状态] 里。
        expected = from_state != AgentState.IDLE and AgentState.INTERRUPTED in _MATRIX[from_state]

        assert fsm.interrupt() is expected
        if expected:
            assert fsm.state == AgentState.INTERRUPTED
            assert fsm.previous == before_state
        else:
            assert fsm.state == before_state
            assert fsm.previous == before_previous

    @pytest.mark.parametrize("from_state", _STATES)
    def test_interrupt_twice_second_is_noop(self, from_state):
        """连续两次 interrupt：首次成功者第二次必为 no-op；首次失败者第二次仍失败。

        （既有 ``test_phase3.py::TestFSMEdges::test_interrupt_from_interrupted_noop``
        只覆盖「首次成功 → 从 INTERRUPTED 再 interrupt」这一条，这里对 6 个状态全量泛化。）
        """
        fsm = _fsm_in(from_state)
        first = fsm.interrupt()
        state_after_first, previous_after_first = fsm.state, fsm.previous

        assert fsm.interrupt() is False
        assert fsm.state == state_after_first
        assert fsm.previous == previous_after_first

        if first:
            assert fsm.state == AgentState.INTERRUPTED
        else:
            assert fsm.state == from_state

    def test_interrupt_from_interrupted_is_noop(self):
        """显式保留既有语义：从 INTERRUPTED 再 interrupt 返回 False 且状态不变。"""
        fsm = _fsm_in(AgentState.INTERRUPTED)
        assert fsm.state == AgentState.INTERRUPTED
        assert fsm.interrupt() is False
        assert fsm.state == AgentState.INTERRUPTED

    @pytest.mark.parametrize("from_state", _STATES)
    def test_interrupt_never_raises(self, from_state):
        """interrupt() 对任何状态都不抛异常（失败以 False 表达，而非异常）。"""
        fsm = _fsm_in(from_state)
        result = fsm.interrupt()
        assert result is True or result is False
