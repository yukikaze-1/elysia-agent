"""core/ 共享类型定义 — Agent 状态、事件类型、优先级。

与 Module/schema/ 不同，这里只包含当前架构实际使用的类型，
不是 L0/L1/L2 多层架构的预留设计。
"""

from enum import IntEnum, StrEnum, auto


# ---------------------------------------------------------------------------
# AgentState — 五状态机
# ---------------------------------------------------------------------------

class AgentState(StrEnum):
    """Agent 认知流水线的五个状态。"""
    IDLE = auto()
    THINKING = auto()
    EXECUTING = auto()
    SPEAKING = auto()
    INTERRUPTED = auto()


VALID_TRANSITIONS: dict[AgentState, set[AgentState]] = {
    AgentState.IDLE:        {AgentState.THINKING},
    AgentState.THINKING:    {AgentState.EXECUTING, AgentState.SPEAKING, AgentState.INTERRUPTED},
    AgentState.EXECUTING:   {AgentState.THINKING, AgentState.INTERRUPTED},
    AgentState.SPEAKING:    {AgentState.IDLE, AgentState.EXECUTING, AgentState.THINKING, AgentState.INTERRUPTED},
    AgentState.INTERRUPTED: {AgentState.IDLE},
}


# ---------------------------------------------------------------------------
# EventPriority / EventType — 事件总线
# ---------------------------------------------------------------------------

class EventPriority(IntEnum):
    """事件优先级 — 数值越低越紧急。"""
    INTERRUPT = 0
    STATE_CHANGE = 1
    NORMAL = 2


class EventType(StrEnum):
    """事件类型 — 驱动 EventBus → FSM → CognitiveLoop 管线。"""
    INTERRUPT = "interrupt"
    STATE_CHANGE = "state_change"
    USER_INPUT = "user_input"
    SYSTEM_TICK = "system_tick"
