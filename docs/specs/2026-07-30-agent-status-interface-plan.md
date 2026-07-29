# AgentStatus 接口层 — Phase 1 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `AgentStatus`（对外只读）和 `CognitivePhaseController`（对内写）两层接口，将 `AgentFSM` 封装为内部实现细节。Phase 1 只做接口定义 + 实现 + loop.py 内部替换，外部消费者不动。

**Architecture:** 新增 `core/agent/status.py`（~200 行），含 2 个 ABC + 2 个 FSM-backed 实现。`Agent.__init__` 中构造 `self.status` / `self.phases`，CognitiveLoop 通过 `self.phases` 驱动状态转换（替代 9 处直接 `fsm.transition()` 调用）。`agent._fsm` 署名保持公开（Phase 3 再私有化）。

**Tech Stack:** Python 3.11+, asyncio, 无新依赖。

**Spec:** [docs/specs/2026-07-30-agent-status-interface-design.md](docs/specs/2026-07-30-agent-status-interface-design.md)

## Global Constraints

- 不改变状态名字符串（"thinking", "speaking", "idle" 等不变）
- 不改变 `StateChangeData` 事件类型和 EventBus 行为
- 不改变前端代码（零改动）
- 不改变外部消费者（Phase 2 迁移）
- `agent._fsm` 保持可访问
- 现有测试全部通过

---

### Task 1: `core/agent/status.py` — AgentStatus + FSMBackedStatus

**Files:**
- Create: `core/agent/status.py`

**Interfaces:**
- Produces: `AgentStatus` (ABC), `FSMBackedStatus(AgentStatus)` — 供 Task 3 (`agent.py`) 使用

`AgentStatus` 是可选 ABC（不强制继承，但提供文档和类型约束）。`FSMBackedStatus` 包装 `AgentFSM` 实现所有语义方法。

- [ ] **Step 1: 写 FSMBackedStatus 测试**

在 `core/agent/tests/test_status.py` 创建测试文件，先写针对 `FSMBackedStatus` 的测试：

```python
"""AgentStatus 和 CognitivePhaseController 的单元测试。"""
import asyncio

import pytest

from core.agent.agent_fsm import AgentFSM, AgentState


@pytest.mark.unit
class TestFSMBackedStatus:
    """FSMBackedStatus 的语义属性测试。"""

    @pytest.fixture
    def fsm(self) -> AgentFSM:
        return AgentFSM()

    def test_is_accepting_input_idle(self, fsm):
        from core.agent.status import FSMBackedStatus
        status = FSMBackedStatus(fsm)
        assert status.is_accepting_input is True       # IDLE 初始状态
        assert status.is_completely_idle is True

    def test_is_accepting_input_thinking(self, fsm):
        from core.agent.status import FSMBackedStatus
        fsm.transition(AgentState.THINKING)
        status = FSMBackedStatus(fsm)
        assert status.is_accepting_input is False
        assert status.is_completely_idle is False

    def test_is_accepting_input_idle_background(self, fsm):
        from core.agent.status import FSMBackedStatus
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.SPEAKING)
        fsm.transition(AgentState.IDLE_BACKGROUND_TASK)
        status = FSMBackedStatus(fsm)
        assert status.is_accepting_input is True        # IDLE_BG 算接受输入
        assert status.is_completely_idle is False       # 但不算完全空闲

    def test_is_speaking(self, fsm):
        from core.agent.status import FSMBackedStatus
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.SPEAKING)
        status = FSMBackedStatus(fsm)
        assert status.is_speaking is True

    def test_is_speaking_false(self, fsm):
        from core.agent.status import FSMBackedStatus
        fsm.transition(AgentState.THINKING)
        status = FSMBackedStatus(fsm)
        assert status.is_speaking is False

    def test_should_suppress_vad_thinking(self, fsm):
        from core.agent.status import FSMBackedStatus
        fsm.transition(AgentState.THINKING)
        status = FSMBackedStatus(fsm)
        assert status.should_suppress_vad_interrupt is True

    def test_should_suppress_vad_executing(self, fsm):
        from core.agent.status import FSMBackedStatus
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.EXECUTING)
        status = FSMBackedStatus(fsm)
        assert status.should_suppress_vad_interrupt is True

    def test_should_suppress_vad_speaking(self, fsm):
        from core.agent.status import FSMBackedStatus
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.SPEAKING)
        status = FSMBackedStatus(fsm)
        assert status.should_suppress_vad_interrupt is False   # SPEAKING 允许打断

    def test_current_name(self, fsm):
        from core.agent.status import FSMBackedStatus
        fsm.transition(AgentState.THINKING)
        status = FSMBackedStatus(fsm)
        assert status.current_name() == "thinking"

    def test_previous_name(self, fsm):
        from core.agent.status import FSMBackedStatus
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.SPEAKING)
        status = FSMBackedStatus(fsm)
        assert status.previous_name() == "thinking"

    def test_previous_name_none(self, fsm):
        from core.agent.status import FSMBackedStatus
        status = FSMBackedStatus(fsm)
        assert status.previous_name() is None

    @pytest.mark.asyncio
    async def test_on_change_notify(self, fsm):
        from core.agent.status import FSMBackedStatus
        status = FSMBackedStatus(fsm)
        calls = []

        async def cb(name: str) -> None:
            calls.append(name)

        status.on_change(cb)
        await status._notify("thinking")
        assert calls == ["thinking"]

    @pytest.mark.asyncio
    async def test_on_change_exception_isolated(self, fsm):
        from core.agent.status import FSMBackedStatus
        status = FSMBackedStatus(fsm)
        calls = []

        async def bad_cb(name: str) -> None:
            raise RuntimeError("boom")

        async def good_cb(name: str) -> None:
            calls.append(name)

        status.on_change(bad_cb)
        status.on_change(good_cb)
        await status._notify("speaking")
        assert calls == ["speaking"]  # good_cb 仍然被调用

    @pytest.mark.asyncio
    async def test_wait_until_idle_already_idle(self, fsm):
        from core.agent.status import FSMBackedStatus
        status = FSMBackedStatus(fsm)
        # 当前已是 IDLE，应立即返回
        await status.wait_until_idle(timeout=0.05)
        # 不超时不抛异常——通过

    @pytest.mark.asyncio
    async def test_wait_until_idle_waits(self, fsm):
        from core.agent.status import FSMBackedStatus
        fsm.transition(AgentState.THINKING)
        status = FSMBackedStatus(fsm)

        async def notify_later() -> None:
            await asyncio.sleep(0.02)
            await status._notify("idle")

        async with asyncio.TaskGroup() as tg:
            tg.create_task(notify_later())
            tg.create_task(status.wait_until_idle(timeout=0.5))
        # 不超时——通过

    @pytest.mark.asyncio
    async def test_wait_until_idle_background(self, fsm):
        from core.agent.status import FSMBackedStatus
        fsm.transition(AgentState.THINKING)
        status = FSMBackedStatus(fsm)

        async def notify_later() -> None:
            await asyncio.sleep(0.02)
            await status._notify("idle_background_task")

        async with asyncio.TaskGroup() as tg:
            tg.create_task(notify_later())
            tg.create_task(status.wait_until_idle(include_background=True, timeout=0.5))
        # 不超时——通过

    @pytest.mark.asyncio
    async def test_wait_until_idle_timeout(self, fsm):
        from core.agent.status import FSMBackedStatus
        fsm.transition(AgentState.THINKING)
        status = FSMBackedStatus(fsm)
        await status.wait_until_idle(timeout=0.02)
        # timeout 不抛异常——通过
```

- [ ] **Step 2: 跑测试确认全部 FAIL（类不存在）**

```bash
pytest core/agent/tests/test_status.py -v 2>&1 | head -5
# 预期: ModuleNotFoundError 或 ImportError——status 模块尚未创建
```

- [ ] **Step 3: 实现 AgentStatus + FSMBackedStatus**

```python
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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .agent_fsm import AgentFSM

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
```

- [ ] **Step 4: 跑测试确认全部 PASS**

```bash
pytest core/agent/tests/test_status.py -v
# 预期: 15 passed
```

等 Task 2 的 CognitivePhaseController 测试加入后一起跑。

- [ ] **Step 5: 暂不 commit（等 Task 2 一起）**

> 注：`_notify` 方法是 `AgentStatus` ABC 上的具体方法（非 abstract），依赖子类的 `_listeners` 属性。`FSMBackedStatus.__init__` 初始化该属性。如果将来有其他实现，只需同样初始化 `_listeners` 即可。

---

### Task 2: `core/agent/status.py` — CognitivePhaseController + FSMBackedPhaseController

**Files:**
- Modify: `core/agent/status.py`（追加内容）

**Interfaces:**
- Consumes: `AgentStatus._notify()`（从 Task 1 来）
- Produces: `CognitivePhaseController` (ABC), `FSMBackedPhaseController` — 供 Task 4 (loop.py) 使用

`CognitivePhaseController` 封装 FSM transition + EventBus post + 输出指示器 + TTS 同步。实现者只需调用 `phases.enter_thinking()` 等方法。

- [ ] **Step 1: 追加 CognitivePhaseController 测试到 test_status.py**

```python
@pytest.mark.unit
class TestFSMBackedPhaseController:
    """FSMBackedPhaseController 的阶段转换测试。"""

    @pytest.fixture
    def fsm(self) -> AgentFSM:
        return AgentFSM()

    @pytest.fixture
    def bus(self) -> Mock:
        return Mock()

    @pytest.fixture
    def output(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture
    def status(self, fsm) -> "FSMBackedStatus":
        from core.agent.status import FSMBackedStatus
        return FSMBackedStatus(fsm)

    def _make_controller(self, fsm, bus, output, status, tts_mgr=None):
        from core.agent.status import FSMBackedPhaseController
        return FSMBackedPhaseController(
            fsm=fsm, bus=bus, output=output,
            tts_mgr=tts_mgr, status=status,
        )

    @pytest.mark.asyncio
    async def test_enter_thinking(self, fsm, bus, output, status):
        from core.agent.status import FSMBackedPhaseController
        from core.monitoring.event_bus import EventPriority
        ph = FSMBackedPhaseController(
            fsm=fsm, bus=bus, output=output, tts_mgr=None, status=status,
        )
        await ph.enter_thinking()
        assert fsm.state == AgentState.THINKING
        bus.post.assert_called_once()
        call_args = bus.post.call_args
        assert call_args[0][0].state == "thinking"
        assert call_args[1]["priority"] == EventPriority.STATE_CHANGE

    @pytest.mark.asyncio
    async def test_enter_executing(self, fsm, bus, output, status):
        fsm.transition(AgentState.THINKING)
        ph = self._make_controller(fsm, bus, output, status)
        await ph.enter_executing()
        assert fsm.state == AgentState.EXECUTING
        output.send.assert_called_once_with("")
        output.typing.assert_called_once_with(False)
        bus.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_return_to_thinking(self, fsm, bus, output, status):
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.EXECUTING)
        ph = self._make_controller(fsm, bus, output, status)
        await ph.return_to_thinking()
        assert fsm.state == AgentState.THINKING
        output.typing.assert_called_once_with(True)
        bus.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_enter_speaking(self, fsm, bus, output, status):
        fsm.transition(AgentState.THINKING)
        ph = self._make_controller(fsm, bus, output, status)
        await ph.enter_speaking()
        assert fsm.state == AgentState.SPEAKING
        output.typing.assert_called_once_with(False)
        bus.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_finish_turn_idle(self, fsm, bus, output, status):
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.SPEAKING)
        ph = self._make_controller(fsm, bus, output, status)
        await ph.finish_turn(has_background_tasks=False)
        assert fsm.state == AgentState.IDLE
        bus.post.assert_called_once()
        assert bus.post.call_args[0][0].state == "idle"

    @pytest.mark.asyncio
    async def test_finish_turn_idle_background(self, fsm, bus, output, status):
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.SPEAKING)
        ph = self._make_controller(fsm, bus, output, status)
        await ph.finish_turn(has_background_tasks=True)
        assert fsm.state == AgentState.IDLE_BACKGROUND_TASK
        bus.post.assert_called_once()
        assert bus.post.call_args[0][0].state == "idle_background_task"

    @pytest.mark.asyncio
    async def test_finish_turn_from_thinking(self, fsm, bus, output, status):
        """finish_turn 从 THINKING 出发——defensive SPEAKING 过渡。"""
        fsm.transition(AgentState.THINKING)
        # 仍在 THINKING——模拟纯工具调用轮次（无文本输出）
        ph = self._make_controller(fsm, bus, output, status)
        await ph.finish_turn(has_background_tasks=False)
        # 应经过 SPEAKING 再到 IDLE
        assert fsm.state == AgentState.IDLE
        bus.post.assert_called_once()
        assert bus.post.call_args[0][0].state == "idle"

    @pytest.mark.asyncio
    async def test_finish_turn_waits_tts(self, fsm, bus, output, status):
        """finish_turn 等 TTS 完成再 transition。"""
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.SPEAKING)
        tts_mgr = AsyncMock()
        tts_mgr.wait_all = AsyncMock()
        ph = self._make_controller(fsm, bus, output, status, tts_mgr=tts_mgr)
        await ph.finish_turn(has_background_tasks=False)
        tts_mgr.wait_all.assert_awaited_once()
        assert fsm.state == AgentState.IDLE

    @pytest.mark.asyncio
    async def test_handle_interrupt(self, fsm, bus, output, status):
        fsm.transition(AgentState.THINKING)
        ph = self._make_controller(fsm, bus, output, status)
        await ph.handle_interrupt()
        # 最终回到 IDLE
        assert fsm.state == AgentState.IDLE
        # 两次 post：interrupted + idle
        assert bus.post.call_count == 2
        first_state = bus.post.call_args_list[0][0][0].state
        second_state = bus.post.call_args_list[1][0][0].state
        assert first_state == "interrupted"
        assert second_state == "idle"

    @pytest.mark.asyncio
    async def test_tts_none_does_not_crash(self, fsm, bus, output, status):
        """TTS 为 None 时 finish_turn 不崩溃。"""
        fsm.transition(AgentState.THINKING)
        fsm.transition(AgentState.SPEAKING)
        ph = self._make_controller(fsm, bus, output, status, tts_mgr=None)
        await ph.finish_turn(has_background_tasks=False)
        assert fsm.state == AgentState.IDLE
```

- [ ] **Step 2: 跑测试确认 CognitivePhaseController 测试 FAIL**

```bash
pytest core/agent/tests/test_status.py -v -k "PhaseController"
# 预期: 10 failed——FSMBackedPhaseController 尚未实现
```

- [ ] **Step 3: 追加 CognitivePhaseController + FSMBackedPhaseController 到 status.py**

在 `status.py` 末尾追加：

```python
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
        from core.monitoring.event_bus import EventPriority
        from core.schema.event import StateChangeData
        from core.schema.context import get_request_id

        self._fsm.transition(AgentState.THINKING)
        self._bus.post(
            StateChangeData(state="thinking", request_id=get_request_id()),
            priority=EventPriority.STATE_CHANGE,
        )
        await self._status._notify("thinking")

    async def enter_executing(self) -> None:
        from core.monitoring.event_bus import EventPriority
        from core.schema.event import StateChangeData
        from core.schema.context import get_request_id

        await self._output.send("")
        await self._output.typing(False)
        self._fsm.transition(AgentState.EXECUTING)
        self._bus.post(
            StateChangeData(state="executing", request_id=get_request_id()),
            priority=EventPriority.STATE_CHANGE,
        )
        await self._status._notify("executing")

    async def return_to_thinking(self) -> None:
        from core.monitoring.event_bus import EventPriority
        from core.schema.event import StateChangeData
        from core.schema.context import get_request_id

        self._fsm.transition(AgentState.THINKING)
        self._bus.post(
            StateChangeData(state="thinking", request_id=get_request_id()),
            priority=EventPriority.STATE_CHANGE,
        )
        await self._output.typing(True)
        await self._status._notify("thinking")

    async def enter_speaking(self) -> None:
        from core.monitoring.event_bus import EventPriority
        from core.schema.event import StateChangeData
        from core.schema.context import get_request_id

        await self._output.typing(False)
        self._fsm.transition(AgentState.SPEAKING)
        self._bus.post(
            StateChangeData(state="speaking", request_id=get_request_id()),
            priority=EventPriority.STATE_CHANGE,
        )
        await self._status._notify("speaking")

    async def finish_turn(self, *, has_background_tasks: bool) -> None:
        import asyncio
        from core.monitoring.event_bus import EventPriority
        from core.schema.event import StateChangeData
        from core.schema.context import get_request_id

        if self._tts_mgr is not None:
            await self._tts_mgr.wait_all()

        if has_background_tasks:
            if self._fsm.state == AgentState.SPEAKING:
                self._fsm.transition(AgentState.IDLE_BACKGROUND_TASK)
            else:
                if self._fsm.can_transition(AgentState.SPEAKING):
                    self._fsm.transition(AgentState.SPEAKING)
                self._fsm.transition(AgentState.IDLE_BACKGROUND_TASK)
            self._bus.post(
                StateChangeData(
                    state="idle_background_task", request_id=get_request_id(),
                ),
                priority=EventPriority.STATE_CHANGE,
            )
            await self._status._notify("idle_background_task")
        else:
            if self._fsm.state == AgentState.THINKING:
                self._fsm.transition(AgentState.SPEAKING)
            self._fsm.transition(AgentState.IDLE)
            self._bus.post(
                StateChangeData(state="idle", request_id=get_request_id()),
                priority=EventPriority.STATE_CHANGE,
            )
            await self._status._notify("idle")

    async def handle_interrupt(self) -> None:
        import asyncio
        from core.monitoring.event_bus import EventPriority
        from core.schema.event import StateChangeData
        from core.schema.context import get_request_id

        self._fsm.transition(AgentState.INTERRUPTED)
        self._bus.post(
            StateChangeData(state="interrupted", request_id=get_request_id()),
            priority=EventPriority.STATE_CHANGE,
        )
        await self._status._notify("interrupted")
        await asyncio.sleep(0.05)
        self._fsm.transition(AgentState.IDLE)
        self._bus.post(
            StateChangeData(state="idle", request_id=get_request_id()),
            priority=EventPriority.STATE_CHANGE,
        )
        await self._status._notify("idle")
```

- [ ] **Step 4: 跑全部 test_status.py 确认 25 PASS**

```bash
pytest core/agent/tests/test_status.py -v
# 预期: 25 passed（15 Status + 10 PhaseController）
```

- [ ] **Step 5: Commit**

```bash
git add core/agent/status.py core/agent/tests/test_status.py
git commit -m "feat: AgentStatus + CognitivePhaseController 接口及 FSM-backed 实现"
```

---

### Task 3: 接入 `core/agent/agent.py` + `__init__.py`

**Files:**
- Modify: `core/agent/agent.py`
- Modify: `core/agent/__init__.py`

**Interfaces:**
- Consumes: `FSMBackedStatus`, `FSMBackedPhaseController`（从 Task 1-2 来）
- Produces: `agent.status: AgentStatus`, `agent.phases: CognitivePhaseController`

- [ ] **Step 1: agent.py `__init__` 中构造 status 和 phases**

在 `self._fsm = AgentFSM()` 之后，`self._bus = EventBus()` 之后，`self._tts_mgr` 赋值之后，添加：

```python
# core/agent/agent.py — 在 __init__ 方法中，self._tts_mgr 赋值后追加

from .status import FSMBackedStatus, FSMBackedPhaseController

self.status = FSMBackedStatus(self._fsm)
self.phases = FSMBackedPhaseController(
    fsm=self._fsm,
    bus=self._bus,
    output=self._output,
    tts_mgr=self._tts_mgr,
    status=self.status,
)
```

类型注解——在 `Agent.__init__` 的参数/属性声明区添加：

```python
self.status: AgentStatus
self.phases: CognitivePhaseController
```

（import 在 TYPE_CHECKING 块或文件顶部添加 `from .status import AgentStatus, CognitivePhaseController`）

- [ ] **Step 2: `__init__.py` 添加导出**

```python
# core/agent/__init__.py
from .status import AgentStatus, CognitivePhaseController
# 追加到现有 exports
```

- [ ] **Step 3: 跑全量测试确认接入不破坏任何东西**

```bash
make test-quick
# 预期: 1774+ passed（含新 status 测试）
```

- [ ] **Step 4: Commit**

```bash
git add core/agent/agent.py core/agent/__init__.py
git commit -m "feat: Agent 接入 status/phases——FSMBacked 实现"
```

---

### Task 4: 替换 `core/cognitive/loop.py` — 内部迁移

**Files:**
- Modify: `core/cognitive/loop.py`

**Interfaces:**
- Consumes: `self.phases: CognitivePhaseController`（从 Task 3 来）

这是 Phase 1 的核心改动——将 loop.py 中所有直接 FSM 操作替换为 `self.phases` 调用。每个替换保持周围代码不变，只替换 FSM 相关行。

- [ ] **Step 1: `process()` — enter_thinking**

当前位置 L273-275：
```python
self._fsm.transition(AgentState.THINKING)
self._bus.post(
    StateChangeData(state="thinking", request_id=rid),
    priority=EventPriority.STATE_CHANGE,
)
```

**替换为：**
```python
await self.phases.enter_thinking()
```

周围的 clock 创建、TTS reset、trace_id 设置保持不变。

- [ ] **Step 2: `_execute_tool_round()` — enter_executing**

当前位置 L475-480：
```python
# 切断气泡 — TTS tasks 独立运行，不 cancel
await self._output.send("")

await self._output.typing(False)
self._fsm.transition(AgentState.EXECUTING)
self._bus.post(
    StateChangeData(state="executing", request_id=get_request_id()),
    priority=EventPriority.STATE_CHANGE,
)
```

**替换为：**
```python
await self.phases.enter_executing()
```

- [ ] **Step 3: `_execute_tool_round()` — return_to_thinking**

当前位置 L613-616：
```python
self._fsm.transition(AgentState.THINKING)
self._bus.post(
    StateChangeData(state="thinking", request_id=get_request_id()),
    priority=EventPriority.STATE_CHANGE,
)
await self._output.typing(True)
```

**替换为：**
```python
await self.phases.return_to_thinking()
```

- [ ] **Step 4: `_dispatch_stream_event()` — enter_speaking**

当前位置 L890-896：
```python
if not speaking_started:
    await self._output.typing(False)
    self._fsm.transition(AgentState.SPEAKING)
    self._bus.post(
        StateChangeData(state="speaking", request_id=get_request_id()),
        priority=EventPriority.STATE_CHANGE,
    )
    speaking_started = True
```

**替换为：**
```python
if not speaking_started:
    await self.phases.enter_speaking()
    speaking_started = True
```

- [ ] **Step 5: `_transition_idle()` — finish_turn**

当前位置 L945-970（整个方法体，17 行）。该处是 Phase 1 改动最复杂的部分——原有 3 分支逻辑被 `finish_turn()` 内部吸收，但 background task 检查需留在 loop.py。

**替换为：**
```python
async def _transition_idle(self) -> None:
    """等待 TTS 完成 → 检查后台任务 → 转换到 IDLE/IDLE_BG。"""
    active_tasks: list = []
    if self._on_background_task is not None and self._task_system is not None:
        active_tasks = await self._task_system.list_active_tasks()
    await self.phases.finish_turn(has_background_tasks=bool(active_tasks))
```

> 注意：`active_tasks` 变量保留以兼容 Phase 3 的 `_after_response` 扩展。
> `wait_all()` 在 `finish_turn` 内部执行——顺序与原来一致（wait_all 先于 transition）。

- [ ] **Step 6: `_handle_interrupt()` — handle_interrupt**

当前位置 L972-988。cleanup 保留在 loop.py，transition 链委托给 phases。

**替换为：**
```python
async def _handle_interrupt(self) -> None:
    """Clean up after interruption — cancel TTS tasks, return to IDLE."""
    if self._fsm.state == AgentState.IDLE:  # ← 此处保持直接读 _fsm（Phase 3 迁移）
        return
    if self._sentence_buf is not None:
        self._sentence_buf.reset()
    if self._block_filter is not None:
        self._block_filter.reset()
    self._executing_tools.clear()
    self._tts_mgr.cancel_all()
    await self._output.typing(False)
    await self.phases.handle_interrupt()
    logger.debug("Interrupt complete — agent is IDLE")
```

删除原来的 `self._fsm.transition(AgentState.INTERRUPTED)` + post + `asyncio.sleep` + `self._fsm.transition(AgentState.IDLE)` + post 块。

- [ ] **Step 7: 确认 `can_transition` 检查保留不动**

`process()` 中 L241-243 的 `can_transition(THINKING)` guard 保留不动——Phase 3 再处理。

- [ ] **Step 8: 删除不再需要的 import（如果有）**

如果原 loop.py 直接 import 了 `StateChangeData` 用于 post，现在可删除该 import（因为 post 逻辑已移到 phases 里）。确认 `AgentState` import 仍被 `can_transition` guard 使用。

- [ ] **Step 9: 跑全量测试**

```bash
make test-quick
# 预期: 1774+ passed，零 regression
```

- [ ] **Step 10: Commit**

```bash
git add core/cognitive/loop.py
git commit -m "refactor: loop.py transition 调用迁移到 phases（Phase 1 内部封装）"
```

---

### Task 5: Final verification

- [ ] 跑全量测试

```bash
make test-quick
# 预期: 全绿
```

- [ ] 确认新测试全部通过

```bash
pytest core/agent/tests/test_status.py -v
# 预期: 25 passed
```

- [ ] grep 确认 `loop.py` 中无残留的 `self._fsm.transition` 调用

```bash
grep -n "_fsm.transition\|_fsm.can_transition\|_bus.post.*StateChangeData" core/cognitive/loop.py
# 预期: 仅 L241 的 can_transition 和 L974 的 _fsm.state 保留
```

- [ ] 类型检查通过
