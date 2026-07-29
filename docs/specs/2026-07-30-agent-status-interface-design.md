# AgentStatus 接口层 —— 解耦 FSM 与消费者

> 2026-07-30 · 设计文档

## 目标

引入 `AgentStatus`（对外只读）+ `CognitivePhaseController`（对内写）两层接口，将 15+ 个消费者与 `AgentFSM` 的具体实现解耦。同时达到：

- **模块化**：消费者不再直接读 `agent._fsm.state`，改为通过语义接口查询
- **可替换性**：将来换掉 FSM 架构，只需实现同一接口，消费者零改动

## 架构对比

### 现状

```
15+ 消费者 ──→ agent._fsm.state / .transition()  直接耦合
              agent._fsm.can_transition()
              agent._fsm.interrupt()
```

### 目标

```
外部消费者 ──→ agent.status.is_xxx()            只读语义查询
               agent.status.on_change(cb)        事件订阅
               agent.status.wait_until_idle()    异步等待

CognitiveLoop ──→ agent.phases.enter_xxx()       阶段转换（内部）
                  agent.phases.finish_turn()
                  agent.phases.handle_interrupt()

AgentFSM        ← 被 status/phases 封装，不对外暴露
```

---

## 接口定义

### `AgentStatus`（对外只读）

所有消费者通过 `agent.status` 访问。用 `@property` 实现，无副作用。

```python
class AgentStatus(ABC):
    """Agent 运行时状态的语义接口——面向所有外部消费者。
    
    不暴露 FSM 状态名。消费者通过语义属性查询，不感知底层实现。
    """

    # ── 语义查询（@property，即刻可得）──

    @property
    def is_accepting_input(self) -> bool:
        """可接受新用户消息？
        
        FSM 实现：IDLE 或 IDLE_BACKGROUND_TASK。
        消费者：agent.py process_message 消息排队。
        """

    @property
    def is_completely_idle(self) -> bool:
        """完全空闲（无任何进行中工作）？
        
        FSM 实现：仅 IDLE（不含 IDLE_BACKGROUND_TASK）。
        消费者：IdleMonitor 主动搭话判定。
        """

    @property
    def is_speaking(self) -> bool:
        """正在输出语音/文本？
        
        FSM 实现：SPEAKING。
        消费者：ws_handler 麦克风回音抑制。
        """

    @property
    def should_suppress_vad_interrupt(self) -> bool:
        """应抑制 VAD 检测中断？
        
        FSM 实现：THINKING 或 EXECUTING。
        消费者：ws_handler VAD 打断过滤。
        """

    # ── 诊断（仅用于日志/展示，不用于逻辑判断）──

    def current_name(self) -> str:
        """当前状态名，仅供日志和调试仪表板使用。"""

    def previous_name(self) -> str | None:
        """上一状态名，仅供调试仪表板使用。"""

    # ── 事件订阅（替代轮询）──

    def on_change(self, callback: Callable[[str], Awaitable[None]]) -> None:
        """注册状态变更回调。回调接收语义状态名（如 "speaking"）。
        
        回调在状态变更时被调用，不做 edge detection——如果连续两次
        进入同一状态（如 EXECUTING→THINKING→EXECUTING），每次都触发。
        
        消费者：ws_handler 麦克风关闭（替代 100ms 轮询）、
               app.py CLI 等待、bench.py 性能测量。
        """

    async def wait_until_idle(
        self,
        *,
        include_background: bool = False,
        timeout: float | None = None,
    ) -> None:
        """阻塞直到进入空闲状态。
        
        include_background=True: 等待 IDLE 或 IDLE_BACKGROUND_TASK。
        include_background=False: 仅等待 IDLE。
        
        超时时不抛异常——调用方自行检查状态。
        
        消费者：app.py CLI 输入循环、bench.py 性能测量。
        """
```

### `CognitivePhaseController`（对内写）

CognitiveLoop 独占，通过 `agent.phases` 访问。封装 transition + EventBus post + typing 指示器 + TTS 同步。

```python
class CognitivePhaseController:
    """认知管线阶段控制器——CognitiveLoop 通过它驱动状态转换。
    
    封装 FSM transition + StateChangeData 发布 + 输出指示器。
    外部消费者不应访问此接口。
    """

    async def enter_thinking(self) -> None:
        """进入 THINKING 阶段。
        
        封装：transition(IDLE/IDLE_BG → THINKING) + post StateChangeData("thinking")。
        不包含：clock 创建、TTS reset、trace_id 设置（这些是 loop.py 的独立职责）。
        """

    async def enter_executing(self) -> None:
        """进入 EXECUTING 阶段。
        
        封装：transition(THINKING → EXECUTING) + output.send("") 切断气泡
              + output.typing(False) + post StateChangeData("executing")。
        """

    async def return_to_thinking(self) -> None:
        """从 EXECUTING 返回 THINKING。
        
        封装：transition(EXECUTING → THINKING) + output.typing(True)
              + post StateChangeData("thinking")。
        """

    async def enter_speaking(self) -> None:
        """进入 SPEAKING 阶段（首句输出时）。
        
        封装：transition(THINKING/EXECUTING → SPEAKING) + output.typing(False)
              + post StateChangeData("speaking")。
        """

    async def finish_turn(self, *, has_background_tasks: bool) -> None:
        """结束当前 turn，转换到空闲状态。
        
        has_background_tasks=True → IDLE_BACKGROUND_TASK，
          若当前非 SPEAKING 则防御性插入 SPEAKING 过渡。
        has_background_tasks=False → IDLE，
          若当前为 THINKING（纯工具调用轮次）则防御性插入 SPEAKING 过渡。
        
        封装：_transition_idle() 的全部逻辑，包括 TTS wait_all()。
        """

    async def handle_interrupt(self) -> None:
        """中断当前处理——取消 TTS，重置缓冲区，返回 IDLE。
        
        封装：sentence_buf.reset + block_filter.reset + executing_tools.clear
              + tts_mgr.cancel_all + output.typing(False)
              + transition(→ INTERRUPTED) + post StateChangeData("interrupted")
              + asyncio.sleep(0.05)
              + transition(→ IDLE) + post StateChangeData("idle")。
        
        前置：调用方需先检查 !is_completely_idle（防止 stale CancelledError）。
        """
```

---

## 具体实现

### `FSMBackedStatus`（实现 `AgentStatus`）

文件：`core/agent/status.py`

```python
class FSMBackedStatus(AgentStatus):
    """基于 AgentFSM 的 AgentStatus 实现。"""

    def __init__(self, fsm: AgentFSM) -> None:
        self._fsm = fsm
        self._listeners: list[Callable[[str], Awaitable[None]]] = []

    # ── 语义查询 ──

    @property
    def is_accepting_input(self) -> bool:
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

    def on_change(self, callback: Callable[[str], Awaitable[None]]) -> None:
        self._listeners.append(callback)

    async def _notify(self, state_name: str) -> None:
        """内部：通知所有监听器。在每次 transition 后调用。"""
        for cb in self._listeners:
            try:
                await cb(state_name)
            except Exception:
                logger.warning("AgentStatus 回调异常", exc_info=True)

    async def wait_until_idle(
        self, *, include_background: bool = False, timeout: float | None = None,
    ) -> None:
        event = asyncio.Event()

        async def _cb(state_name: str) -> None:
            if state_name == "idle":
                event.set()
            elif include_background and state_name == "idle_background_task":
                event.set()

        self.on_change(_cb)
        # 检查当前状态——可能已经是 idle
        if (self.is_completely_idle
                or (include_background and self._fsm.state == AgentState.IDLE_BACKGROUND_TASK)):
            event.set()
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

### `FSMBackedPhaseController`（实现 `CognitivePhaseController`）

```python
class FSMBackedPhaseController(CognitivePhaseController):
    """基于 AgentFSM 的 CognitivePhaseController 实现。"""

    def __init__(
        self,
        fsm: AgentFSM,
        bus: EventBus,
        output: OutputManager,
        tts_mgr: TTSStreamManager | None,
        status: FSMBackedStatus,
    ) -> None:
        self._fsm = fsm
        self._bus = bus
        self._output = output
        self._tts_mgr = tts_mgr
        self._status = status

    async def enter_thinking(self) -> None:
        self._fsm.transition(AgentState.THINKING)
        self._bus.post(StateChangeData(
            state="thinking", request_id=get_request_id(),
        ), priority=EventPriority.STATE_CHANGE)
        await self._status._notify("thinking")

    async def enter_executing(self) -> None:
        await self._output.send("")           # 切断气泡
        await self._output.typing(False)
        self._fsm.transition(AgentState.EXECUTING)
        self._bus.post(StateChangeData(
            state="executing", request_id=get_request_id(),
        ), priority=EventPriority.STATE_CHANGE)
        await self._status._notify("executing")

    async def return_to_thinking(self) -> None:
        self._fsm.transition(AgentState.THINKING)
        self._bus.post(StateChangeData(
            state="thinking", request_id=get_request_id(),
        ), priority=EventPriority.STATE_CHANGE)
        await self._output.typing(True)
        await self._status._notify("thinking")

    async def enter_speaking(self) -> None:
        await self._output.typing(False)
        self._fsm.transition(AgentState.SPEAKING)
        self._bus.post(StateChangeData(
            state="speaking", request_id=get_request_id(),
        ), priority=EventPriority.STATE_CHANGE)
        await self._status._notify("speaking")

    async def finish_turn(self, *, has_background_tasks: bool) -> None:
        if self._tts_mgr:
            await self._tts_mgr.wait_all()

        if has_background_tasks:
            if self._fsm.state == AgentState.SPEAKING:
                self._fsm.transition(AgentState.IDLE_BACKGROUND_TASK)
            else:
                if self._fsm.can_transition(AgentState.SPEAKING):
                    self._fsm.transition(AgentState.SPEAKING)
                self._fsm.transition(AgentState.IDLE_BACKGROUND_TASK)
            self._bus.post(StateChangeData(
                state="idle_background_task", request_id=get_request_id(),
            ), priority=EventPriority.STATE_CHANGE)
            await self._status._notify("idle_background_task")
        else:
            if self._fsm.state == AgentState.THINKING:
                self._fsm.transition(AgentState.SPEAKING)
            self._fsm.transition(AgentState.IDLE)
            self._bus.post(StateChangeData(
                state="idle", request_id=get_request_id(),
            ), priority=EventPriority.STATE_CHANGE)
            await self._status._notify("idle")

    async def handle_interrupt(self) -> None:
        # cleanup 由 CognitiveLoop._handle_interrupt 做，这里只做 phase 转换
        self._fsm.transition(AgentState.INTERRUPTED)
        self._bus.post(StateChangeData(
            state="interrupted", request_id=get_request_id(),
        ), priority=EventPriority.STATE_CHANGE)
        await self._status._notify("interrupted")
        await asyncio.sleep(0.05)
        self._fsm.transition(AgentState.IDLE)
        self._bus.post(StateChangeData(
            state="idle", request_id=get_request_id(),
        ), priority=EventPriority.STATE_CHANGE)
        await self._status._notify("idle")
```

> `handle_interrupt` 只做 phase 转换。资源清理（cancel TTS、reset 缓冲区、clear executing_tools）保持在 loop.py 中——它们是流式管线的职责，不属于状态转换。

---

## 事件流变更

### `StateChangeData` 保留（Phase 1-2）

Phase 1-2 中 `StateChangeData` 不变，但由 `CognitivePhaseController` 内部发布而非 loop.py 直接 post。消费者继续接收 `StateChangeData` 事件，但以下两处需适配：

1. **`agent.py` `_handle_state_change`**（L498-520）：改为使用 `agent.status` 属性而非 `StateChangeData.state` 字符串。将 `state in ("idle", "idle_background_task")` 改为 `agent.status.is_accepting_input`。

2. **`agent.py` `_handle_interrupt` drain**（L424）：`isinstance(drained.data, StateChangeData)` 保持不变——`StateChangeData` 类型不变。

### `on_change` 回调 vs EventBus

`AgentStatus.on_change` 是 EventBus 之上的一层轻量封装——`CognitivePhaseController` 在每个 transition 后调用 `_notify()`，触发已注册的回调。这与 EventBus 并行不悖：
- EventBus：内部解耦（agent event loop 驱动认知管线）
- `on_change`：外部订阅（ws_handler、app.py 等不需要知道 EventBus 的消费者）

---

## 过渡策略：保持 `agent._fsm` 可访问（Phase 1）

Phase 1 期间，`agent._fsm` 不做 `__private` 改名。新增 `agent.status` 和 `agent.phases` 后，内部代码逐步迁移。Phase 3 清理阶段再将 `_fsm` 标记为私有。

这样 Consumer 迁移是增量式的：每个 `agent._fsm.state` 替换为一个 `agent.status.is_xxx` 调用，每个 commit 独立可测。

---

## 文件变更清单

### 新增

| 文件 | 内容 |
|------|------|
| `core/agent/status.py` | `AgentStatus`(ABC) + `FSMBackedStatus` + `CognitivePhaseController`(ABC) + `FSMBackedPhaseController` |
| `core/agent/tests/test_status.py` | `FSMBackedStatus` 单元测试 + `FSMBackedPhaseController` 单元测试 |

### 修改——Phase 1（内部封装）

| 文件 | 改动 |
|------|------|
| `core/agent/agent.py` | 构造 `FSMBackedStatus` + `FSMBackedPhaseController`，赋值到 `self.status` / `self.phases`；`_handle_state_change` 改用 `status.is_accepting_input` |
| `core/cognitive/loop.py` | 9 个 transition 调用 → `phases.xxx()`；`_transition_idle()` → `phases.finish_turn()`；`_handle_interrupt` 中的 transition → `phases.handle_interrupt()`；`can_transition` 检查保留（内部 guard） |
| `core/agent/__init__.py` | 导出 `AgentStatus`, `CognitivePhaseController` |

### 修改——Phase 2（消费端迁移）

| 文件 | 当前 | 改为 |
|------|------|------|
| `core/agent/agent.py` L279 | `self._fsm.state not in (IDLE, IDLE_BG)` | `not self.status.is_accepting_input` |
| `core/monitoring/idle_monitor.py` L70 | `self._fsm.state != AgentState.IDLE` | `not self.status.is_completely_idle` |
| `core/input/ws_handler.py` L118-121 | 100ms 轮询 `agent._fsm.state` | `agent.status.on_change(_mic_callback)` |
| `core/input/ws_handler.py` L104,235 | `state in {"thinking","executing"}` | `agent.status.should_suppress_vad_interrupt` |
| `core/input/server.py` L137 | `agent._fsm.state` | `agent.status.current_name()` |
| `core/debug/info.py` L152-153 | `agent._fsm.state` / `.previous` | `agent.status.current_name()` / `.previous_name()` |
| `core/agent/_lifecycle_tools.py` L56 | `str(self._fsm.state)` | `self.status.current_name()` |
| `app.py` L70 | `while agent._fsm.state != "idle"` | `await agent.status.wait_until_idle()` |
| `Module/bench/bench.py` L163-167 | `agent._fsm.state` 轮询 | `agent.status.wait_until_idle()` |

### 修改——前端

| 文件 | 改动 |
|------|------|
| `frontend/src/types/protocol.ts` | `StateMessage` 不变（状态名字符串不变） |
| `frontend/src/composables/useWebSocket.ts` | `m.state === 'executing'` 不变 |
| `frontend/src/stores/chatStore.ts` | `stateName === 'idle'` → `stateName === 'idle'` 不变 |
| `frontend/src/components/character/CharStatus.vue` | CSS class 映射不变 |
| `frontend/src/debug/components/OverviewTab.vue` | 字段名不变 |

> 前端改动为零——状态名字符串（"thinking"/"speaking"/"idle"等）不发生变化。

### 修改——文档

| 文件 | 改动 |
|------|------|
| `CLAUDE.md` | FSM 引用 → AgentStatus 引用 |
| `docs/architecture.md` | 4.2 节更新为 AgentStatus 架构 |
| `docs/frontend-api.md` | `/health` 响应格式 `fsm` 字段保留（兼容） |
| `core/agent/README.md` | 新增 status.py 文档 |

---

## 不做

- **不改变状态名字符串**——"thinking", "speaking", "idle" 等保持不变。前端和 `/health` 响应格式不变。
- **不改变 EventBus 的 `StateChangeData` 类型**——保留现有事件类型，只是发布者从 loop.py 变为 `CognitivePhaseController`。
- **不改变 `_process_lock`**——asyncio.Lock 是与 FSM 独立的并发控制机制。
- **不改变 `RunState`**（STOPPED/RUNNING/STOPPING/RESTARTING）——那是服务生命周期状态机，独立的。

---

## 验证

```bash
# PhaseController 单元测试
pytest core/agent/tests/test_status.py -v

# 全量 regression
make test-quick

# 手动检查：/health 端点仍返回 fsm 字段
curl http://localhost:8765/health

# 手动检查：前端状态圆点动画正常
```

## 风险缓解

| 风险 | 缓解 |
|------|------|
| `_transition_idle` 3 分支迁移出错 | `finish_turn()` 逐行保留原有分支条件，不重构逻辑 |
| `_handle_interrupt` drain 的 isinstance 检查遗漏 | Phase 1 不改 `StateChangeData` 类型，isinstance 自动兼容 |
| `on_change` 回调异常导致状态通知链断裂 | `_notify()` 中 try/except 隔离，单个回调失败不影响其他 |
| `wait_until_idle` 回调泄漏（未清理 listener） | finally 块中 `_listeners.remove(cb)` |
