"""Agent 运行时状态接口和 FSM-backed 实现。

外部消费者通过 agent.status 查询状态，不感知 AgentFSM。
CognitiveLoop 通过 agent.phases 驱动状态转换。

接口设计遵循：属性 = 即刻可得的事实（property），方法 = 可能有副作用。
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from .agent_fsm import AgentFSM, AgentState

logger = logging.getLogger(__name__)


# ── 对外只读接口 ──────────────────────────────────────────────────────────────

class AgentStatus(ABC):
    """Agent 运行时状态的语义接口——面向外部消费者。

    不暴露 FSM 状态名。消费者通过语义属性查询，不感知底层实现。
    """

    # ── 语义查询（@property）──

    @property
    @abstractmethod
    def is_accepting_input(self) -> bool:
        """可接受新用户消息？（IDLE 或 IDLE_BACKGROUND_TASK）"""
        ...

    @property
    @abstractmethod
    def is_completely_idle(self) -> bool:
        """完全空闲，无任何进行中工作？（仅 IDLE）"""
        ...

    @property
    @abstractmethod
    def is_speaking(self) -> bool:
        """正在输出语音/文本给用户？（SPEAKING）"""
        ...

    @property
    @abstractmethod
    def should_suppress_vad_interrupt(self) -> bool:
        """应抑制 VAD 语音检测中断？（THINKING 或 EXECUTING）"""
        ...

    # ── 诊断 ──

    @abstractmethod
    def current_name(self) -> str:
        """当前状态名，仅供日志和调试仪表板使用。"""
        ...

    @abstractmethod
    def previous_name(self) -> str | None:
        """上一状态名，仅供调试仪表板使用。"""
        ...

    # ── 事件订阅 ──

    @abstractmethod
    def on_change(
        self, callback: Callable[[str], Awaitable[None]],
    ) -> None:
        """注册状态变更回调。回调接收语义状态名（如 "speaking"）。

        每次 transition 后调用，不做 edge detection——
        如果连续两次进入同一状态，每次都触发。
        """
        ...

    @abstractmethod
    async def wait_until_idle(
        self,
        *,
        include_background: bool = False,
        timeout: float | None = None,
    ) -> None:
        """阻塞直到进入空闲状态。

        include_background=True: IDLE 或 IDLE_BACKGROUND_TASK 都算。
        include_background=False: 仅 IDLE。
        超时不抛异常——调用方自行检查状态。
        """
        ...

    # ── 内部 ──

    async def _notify(self, state_name: str) -> None:
        """内部：通知所有 on_change 回调。由 FSMBackedPhaseController 在每个
        transition 后调用。单个回调异常不影响其他回调。"""
        for cb in getattr(self, '_listeners', []):
            try:
                await cb(state_name)
            except Exception:
                logger.warning(
                    "AgentStatus 回调异常: %s", state_name, exc_info=True,
                )


# ── FSM-backed 实现 ───────────────────────────────────────────────────────────

class FSMBackedStatus(AgentStatus):
    """基于 AgentFSM 的 AgentStatus 实现。"""

    def __init__(self, fsm: AgentFSM) -> None:
        self._fsm = fsm
        self._listeners: list[Callable[[str], Awaitable[None]]] = []

    # ── 语义查询 ──

    @property
    def is_accepting_input(self) -> bool:
        from .agent_fsm import AgentState
        return self._fsm.state in (AgentState.IDLE, AgentState.IDLE_BACKGROUND_TASK)

    @property
    def is_completely_idle(self) -> bool:
        from .agent_fsm import AgentState
        return self._fsm.state == AgentState.IDLE

    @property
    def is_speaking(self) -> bool:
        from .agent_fsm import AgentState
        return self._fsm.state == AgentState.SPEAKING

    @property
    def should_suppress_vad_interrupt(self) -> bool:
        from .agent_fsm import AgentState
        return self._fsm.state in (AgentState.THINKING, AgentState.EXECUTING)

    def current_name(self) -> str:
        return str(self._fsm.state)

    def previous_name(self) -> str | None:
        return str(self._fsm.previous) if self._fsm.previous else None

    def on_change(
        self, callback: Callable[[str], Awaitable[None]],
    ) -> None:
        self._listeners.append(callback)

    async def wait_until_idle(
        self,
        *,
        include_background: bool = False,
        timeout: float | None = None,
    ) -> None:
        from .agent_fsm import AgentState

        # 当前已满足条件？立即返回
        if self.is_completely_idle:
            return
        if include_background and self._fsm.state == AgentState.IDLE_BACKGROUND_TASK:
            return

        event = asyncio.Event()

        async def _cb(state_name: str) -> None:
            if state_name == "idle":
                event.set()
            elif include_background and state_name == "idle_background_task":
                event.set()

        self.on_change(_cb)
        try:
            if timeout is not None:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            else:
                await event.wait()
        except asyncio.TimeoutError:
            pass
        finally:
            self._listeners.remove(_cb)
