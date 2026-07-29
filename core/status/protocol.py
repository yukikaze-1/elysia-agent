"""Agent 运行时状态接口——纯协议，零依赖。

外部消费者通过 agent.status 查询状态，不感知底层实现。
CognitiveLoop 通过 agent.phases 驱动状态转换。

接口设计遵循：属性 = 即刻可得的事实（property），方法 = 可能有副作用。
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

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
        """内部：通知所有 on_change 回调。由 PhaseController 在每个
        transition 后调用。单个回调异常不影响其他回调。"""
        for cb in getattr(self, '_listeners', []):
            try:
                await cb(state_name)
            except Exception:
                logger.warning(
                    "AgentStatus 回调异常: %s", state_name, exc_info=True,
                )


# ── 对内写入接口 ──────────────────────────────────────────────────────────────

class CognitivePhaseController(ABC):
    """认知管线阶段控制器——CognitiveLoop 通过它驱动状态转换。

    封装 FSM transition + StateChangeData 发布 + 输出指示器 + TTS 同步。
    外部消费者不应访问此接口——它是 CognitiveLoop 的实现工具。
    """

    @abstractmethod
    async def enter_thinking(self) -> None:
        """进入 THINKING 阶段。封装 transition + post + _notify。"""
        ...

    @abstractmethod
    async def enter_executing(self) -> None:
        """进入 EXECUTING 阶段。封装 output.send("") 切断气泡
        + output.typing(False) + transition + post + _notify。"""
        ...

    @abstractmethod
    async def return_to_thinking(self) -> None:
        """从 EXECUTING 返回 THINKING。封装 output.typing(True)
        + transition + post + _notify。"""
        ...

    @abstractmethod
    async def enter_speaking(self) -> None:
        """进入 SPEAKING 阶段（首句输出时）。封装 output.typing(False)
        + transition + post + _notify。"""
        ...

    @abstractmethod
    async def finish_turn(self, *, has_background_tasks: bool) -> None:
        """结束当前 turn，转换到 IDLE 或 IDLE_BACKGROUND_TASK。

        封装 TTS wait_all()、defensive SPEAKING 过渡、
        transition + post + _notify。
        """
        ...

    @abstractmethod
    async def handle_interrupt(self) -> None:
        """中断状态转换——进入 INTERRUPTED，50ms 后回到 IDLE。

        封装 transition(→ INTERRUPTED) + post + _notify
        + asyncio.sleep(0.05) + transition(→ IDLE) + post + _notify。
        资源清理（reset/cancel/clear）由调用方完成——这些是流式管线的职责。
        """
        ...
