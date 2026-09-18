"""FSM-backed AgentStatus + CognitivePhaseController 实现。

依赖 AgentFSM（core.status.agent_fsm），将语义接口映射到具体状态转换。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from core.status.agent_fsm import AgentFSM, AgentState
from core.status.protocol import AgentStatus, CognitivePhaseController, RunState
from core.schema.agent import EventPriority
from core.schema.types import RequestId

if TYPE_CHECKING:
    from core.adapter.tts_adapter import TTSStreamManager
    from core.monitoring.event_bus import EventBus
    from Module.output.manager import OutputManager

logger = logging.getLogger(__name__)


# ── FSM-backed Status ─────────────────────────────────────────────────────────

class FSMBackedStatus(AgentStatus):
    """基于 AgentFSM 的 AgentStatus 实现——**两个轴由同一个对象持有**（#43）。

    - 相位轴：`self._fsm`（IDLE/THINKING/EXECUTING/SPEAKING/…）
    - 生命周期轴：`self._run_state`（STOPPED/RUNNING/STOPPING/RESTARTING）

    此前 `Agent._state` 与状态层是**两份互不相干的存储**：`Agent._state` 是普通实例
    属性，状态层只投影 FSM。两者只能靠"记得同步"维持，停机窗口里正好没人记得——
    这就是 CR-08（ACK 撒谎 + 消息永久丢失）的根因。现在 `Agent._state` 只是
    `self.run_state` 的视图，`set_run_state` 是唯一写入点。
    """

    def __init__(self, fsm: AgentFSM, *,
                 run_state: RunState = RunState.RUNNING) -> None:
        """初始化。

        Args:
            fsm: 底层状态机（相位轴）。
            run_state: 生命周期初值。默认 `RUNNING` 是给**独立构造**的场合（单测里
                20 多处直接 `CognitiveLoop(...)` / `FSMBackedStatus(fsm)`）——没有
                生命周期拥有者时该轴视为"不适用"，而不是伪装成"已停止"（后者会让
                `accepts_new_turns` 恒假，把测试全部变成静默跳过）。
                真实 Agent 在 `__init__` 里紧接着就置 `STOPPED`（`Agent._state =
                RunState.STOPPED` 经 setter 落到这里），生产路径上不存在
                "默认 RUNNING" 的窗口。
        """
        self._fsm = fsm
        self._run_state = run_state
        self._listeners: list[Callable[[str], Awaitable[None]]] = []

    # ── 生命周期轴 ──

    @property
    def run_state(self) -> RunState:
        return self._run_state

    def set_run_state(self, state: RunState) -> None:
        logger.debug("RunState: %s → %s", self._run_state.name, state.name)
        self._run_state = state

    # ── 语义查询 ──

    @property
    def is_accepting_input(self) -> bool:
        """相位轴：当前相位是否允许接收输入（**不含**生命周期，见 `accepts_new_turns`）。"""
        return self._fsm.state in (AgentState.IDLE, AgentState.IDLE_BACKGROUND_TASK)

    @property
    def is_completely_idle(self) -> bool:
        return self._fsm.state == AgentState.IDLE

    @property
    def is_speaking(self) -> bool:
        return self._fsm.state == AgentState.SPEAKING

    @property
    def should_suppress_vad_interrupt(self) -> bool:
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


# ── FSM-backed PhaseController ────────────────────────────────────────────────

class FSMBackedPhaseController(CognitivePhaseController):
    """基于 AgentFSM 的 CognitivePhaseController 实现。"""

    def __init__(
        self,
        fsm: AgentFSM,
        bus: "EventBus",
        output: "OutputManager",
        tts_mgr: "TTSStreamManager | None",
        status: FSMBackedStatus,
    ) -> None:
        self._fsm = fsm
        self._bus = bus
        self._output = output
        self._tts_mgr = tts_mgr
        self._status = status

    # ── phase 入口 ──

    async def enter_thinking(self) -> None:
        from core.schema.event import StateChangeData
        from core.utils import get_request_id

        self._fsm.transition(AgentState.THINKING)
        self._bus.post(
            StateChangeData(state=AgentState.THINKING, request_id=RequestId(get_request_id())),
            priority=EventPriority.STATE_CHANGE,
        )
        await self._status._notify("thinking")

    async def enter_executing(self) -> None:
        from core.schema.event import StateChangeData
        from core.utils import get_request_id

        await self._output.send("")
        await self._output.typing(False)
        self._fsm.transition(AgentState.EXECUTING)
        self._bus.post(
            StateChangeData(state=AgentState.EXECUTING, request_id=RequestId(get_request_id())),
            priority=EventPriority.STATE_CHANGE,
        )
        await self._status._notify("executing")

    async def return_to_thinking(self) -> None:
        from core.schema.event import StateChangeData
        from core.utils import get_request_id

        self._fsm.transition(AgentState.THINKING)
        self._bus.post(
            StateChangeData(state=AgentState.THINKING, request_id=RequestId(get_request_id())),
            priority=EventPriority.STATE_CHANGE,
        )
        await self._output.typing(True)
        await self._status._notify("thinking")

    async def enter_speaking(self) -> None:
        from core.schema.event import StateChangeData
        from core.utils import get_request_id

        await self._output.typing(False)
        self._fsm.transition(AgentState.SPEAKING)
        self._bus.post(
            StateChangeData(state=AgentState.SPEAKING, request_id=RequestId(get_request_id())),
            priority=EventPriority.STATE_CHANGE,
        )
        await self._status._notify("speaking")

    async def finish_turn(self, *, has_background_tasks: bool) -> None:
        from core.schema.event import StateChangeData
        from core.utils import get_request_id

        if self._tts_mgr is not None:
            await self._tts_mgr.wait_all()
            # wait_all 后 flush emoji——此时最后一句 TTS 已完成，
            # 残留 emoji 不会提前广播，依附顺序（"句子😊"）保持正确
            await self._tts_mgr.flush_emojis()

        if has_background_tasks:
            if self._fsm.state == AgentState.SPEAKING:
                self._fsm.transition(AgentState.IDLE_BACKGROUND_TASK)
            else:
                if self._fsm.can_transition(AgentState.SPEAKING):
                    self._fsm.transition(AgentState.SPEAKING)
                self._fsm.transition(AgentState.IDLE_BACKGROUND_TASK)
            self._bus.post(
                StateChangeData(
                    state=AgentState.IDLE_BACKGROUND_TASK, request_id=RequestId(get_request_id()),
                ),
                priority=EventPriority.STATE_CHANGE,
            )
            await self._status._notify("idle_background_task")
        else:
            if self._fsm.state == AgentState.THINKING:
                self._fsm.transition(AgentState.SPEAKING)
            self._fsm.transition(AgentState.IDLE)
            self._bus.post(
                StateChangeData(state=AgentState.IDLE, request_id=RequestId(get_request_id())),
                priority=EventPriority.STATE_CHANGE,
            )
            await self._status._notify("idle")

    async def handle_interrupt(self) -> None:
        """中断状态转换——进入 INTERRUPTED，50ms 后回到 IDLE（Batch 11：取消安全 + 幂等）。

        `transition` / `bus.post` 是**同步**的，所以"相位离开活跃区"本身不会被取消
        打断；会被打断的是中间那两段 await（`_notify("interrupted")` 与 UI 可观测
        延迟 `asyncio.sleep(0.05)`）。因此把 **IDLE 落位 + IDLE 广播 + idle 通知**
        放进 `finally`：无论这两段 await 是正常返回、抛普通异常、还是被**第二发**
        `task.cancel()` 打断，相位都必须落到 IDLE——这就是 CF1/CF2 的"逻辑终态"。
        50ms 延迟**保持可取消**：它只是 UI 可观测性，不该为它 shield 整个 handler
        （§20/§28：临界区只保留"建立稳定逻辑终态"那一小段同步代码）。

        幂等（§24）：完全空闲（IDLE）时直接返回——既避开 `IDLE → INTERRUPTED`
        （转换表里的**非法边**，`core/schema/agent.py`）抛 `ValueError`，也让
        "收口完成后又被调用一次"成为无副作用的 no-op，而不是广播出第二组
        terminal 事件（§50）。
        """
        from core.schema.event import StateChangeData
        from core.utils import get_request_id

        if self._fsm.state == AgentState.IDLE:
            return
        self._fsm.transition(AgentState.INTERRUPTED)
        self._bus.post(
            StateChangeData(state=AgentState.INTERRUPTED, request_id=RequestId(get_request_id())),
            priority=EventPriority.STATE_CHANGE,
        )
        try:
            await self._status._notify("interrupted")
            await asyncio.sleep(0.05)
        finally:
            # ★ 逻辑终态：同步落位 + 广播。这一小段没有 await ⇒ 任何后续取消都打不断它。
            if self._fsm.state != AgentState.IDLE:
                self._fsm.transition(AgentState.IDLE)
                self._bus.post(
                    StateChangeData(state=AgentState.IDLE, request_id=RequestId(get_request_id())),
                    priority=EventPriority.STATE_CHANGE,
                )
            await self._status._notify("idle")
