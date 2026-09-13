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
from enum import Enum, auto

logger = logging.getLogger(__name__)


class RunState(Enum):
    """Agent **生命周期**状态（进程/服务轴）。

    与 `AgentState`（`core.schema.agent`，认知管线轴：IDLE/THINKING/EXECUTING/
    SPEAKING/INTERRUPTED/IDLE_BACKGROUND_TASK）是**两个正交的轴**，不是同一个枚举
    的两种写法：一个 Agent 可以"生命周期 RUNNING 且相位 EXECUTING"，也可以
    "生命周期 STOPPING 且相位 IDLE"。合并成单个枚举需要发明 `STOPPED_THINKING`
    这类笛卡尔积状态，因此本层让**同一个对象**同时持有两个轴、而不是把两个轴压成
    一个（#43 的"单一权威"= 单一持有者，不是单一枚举）。

    定义位置（#43）：从 `core/agent/_types.py` 移到这里——它是 `AgentStatus` 接口
    的一部分，放接口层才能让 `AgentStatus`/`FSMBackedStatus` 持有它而不反向依赖
    `core.agent`。`core.agent._types` 仍 re-export（兼容既有导入路径）。
    """
    STOPPED = auto()      # 初始 / 已停止
    RUNNING = auto()      # 运行中
    STOPPING = auto()     # 正常关机中
    RESTARTING = auto()   # 重启中


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

    # ── 生命周期轴（#43：与相位轴由**同一个对象**持有）──

    @property
    @abstractmethod
    def run_state(self) -> RunState:
        """Agent 生命周期状态（`RunState`）。

        `Agent._state` 是本属性的**视图**——真实存储只有这一处，因此
        "`Agent._state` 与状态层不同步"在结构上不可能发生。
        """
        ...

    @abstractmethod
    def set_run_state(self, state: RunState) -> None:
        """设置生命周期状态——`Agent._state` 赋值的**唯一落点**。

        Args:
            state: 目标生命周期状态。
        """
        ...

    @property
    def is_service_running(self) -> bool:
        """生命周期轴就绪？（`run_state is RUNNING`）

        与相位轴**正交**：一个 RUNNING 的 Agent 可能正在 THINKING（相位忙），
        一个 STOPPING 的 Agent 相位可能还是 IDLE。调用方对这两件事的处置**不同**
        ——生命周期未就绪要**拒收**（CR-08：停机窗口 ACK 撒谎 + 永久丢失），相位忙
        要**排队**。所以本属性只回答生命周期，不替调用方决定策略。

        CR-08 的根因是"准入判定只看相位轴"：`stop()` 首行置 `_state = STOPPED`，
        而 FSM 仍停在 IDLE（`is_accepting_input` 为真），于是停机窗口内新消息被
        ACK 后永久丢失。本属性让"生命周期是否就绪"只有一个回答者。
        """
        return self.run_state is RunState.RUNNING

    @property
    def accepts_new_turns(self) -> bool:
        """现在能不能**开一个新回合**？= 生命周期就绪 且 相位允许。

        适合"没有排队语义、只有做/不做"的入口（如 `CognitiveLoop.process`——它已经
        持有 `_process_lock`，相位不空闲就说明不该叠一个回合）。

        **不要**用它做"忙则排队"的入口判定：那会把"停机拒收"和"忙则排队"压成同一
        种处置，忙时改判拒收（`test_busy_queue_threads_conversation_id` 抓到的正是
        这一条）。那种入口要显式分开问 `is_service_running` 与 `is_accepting_input`。

        默认实现（而非抽象）是为了让测试替身只需实现两个轴的原语。
        """
        return self.is_service_running and self.is_accepting_input

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
