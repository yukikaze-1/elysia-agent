"""core/schema 子模块 — Agent 状态、事件、优先级类型。

与 Module/schema/ 不同，这里只包含当前架构实际使用的类型，
不是 L0/L1/L2 多层架构的预留设计。
"""

from enum import IntEnum, StrEnum, auto


# ---------------------------------------------------------------------------
# AgentState — Agent 状态机
# ---------------------------------------------------------------------------

class AgentState(StrEnum):
    """Agent 认知流水线的状态。"""
    IDLE = auto()
    THINKING = auto()
    EXECUTING = auto()
    SPEAKING = auto()
    INTERRUPTED = auto()
    IDLE_BACKGROUND_TASK = auto()  # 可对话，但后台有任务


VALID_TRANSITIONS: dict[AgentState, set[AgentState]] = {
    AgentState.IDLE:        {AgentState.THINKING},
    AgentState.THINKING:    {AgentState.EXECUTING, AgentState.SPEAKING, AgentState.INTERRUPTED},
    AgentState.EXECUTING:   {AgentState.THINKING, AgentState.INTERRUPTED},
    AgentState.SPEAKING:    {AgentState.IDLE, AgentState.EXECUTING, AgentState.THINKING, AgentState.INTERRUPTED, AgentState.IDLE_BACKGROUND_TASK},
    AgentState.INTERRUPTED: {AgentState.IDLE},
    AgentState.IDLE_BACKGROUND_TASK: {AgentState.THINKING, AgentState.INTERRUPTED},
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
    TASK_RESULT = "task_result"  # 后台任务结果
