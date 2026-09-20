# core/status — Agent 运行时状态接口层

> 文档导航：[项目文档](../../README.md)

把 Agent 互为正交的状态轴（认知相位 + 生命周期）收口成对外只读的 `AgentStatus` 与对内写入的 `CognitivePhaseController`；消费者只依赖语义接口，不感知底层 FSM。

## 关键机制

- **正交双轴，单一持有者**：相位轴是 `AgentState`（`IDLE` / `THINKING` / `EXECUTING` / `SPEAKING` / `INTERRUPTED` / `IDLE_BACKGROUND_TASK`，定义在 `core.schema.agent`），生命周期轴是 `RunState`（`STOPPED` / `RUNNING` / `STOPPING` / `RESTARTING`，定义在本包 `protocol.py`）。`FSMBackedStatus` 由同一个对象同时持有两轴；`Agent._state` 只是 `status.run_state` 的**视图**，`set_run_state` 是唯一写入点——"两个状态源互不同步"在结构上不可能（#43 / CR-08）。
- **接口纪律**：属性 = 即刻可得的事实（`is_accepting_input` / `is_completely_idle` / `is_speaking` / `should_suppress_vad_interrupt` / `run_state`），方法 = 可能有副作用。语义属性不暴露 FSM 状态名。
- **准入判定分两级**：`is_service_running` 只回答生命周期；`accepts_new_turns`（默认实现，非抽象）= 生命周期就绪 **且** 相位允许。生命周期未就绪要**拒收**、相位忙要**排队**——处置策略不同，调用方必须显式分开提问、不能合并成一个布尔。
- **转换不变式由 `AgentFSM` 强制**：非法边抛 `ValueError`，同状态转换幂等；转换表权威是 `core.schema.agent.VALID_TRANSITIONS`（`agent_fsm.py` re-export）。`interrupt()` 在 IDLE 返回 False，`reset()` 强制回 IDLE 供服务重启。
- **事件驱动替代轮询**：`on_change(callback)` 注册状态变更回调（每次 transition 都触发，不做边沿检测）；`wait_until_idle(include_background=…, timeout=…)` 用 `asyncio.Event` 等待，超时不抛异常（调用方自查状态）。
- **相位控制器打包副作用**：`FSMBackedPhaseController` 把 FSM transition + `StateChangeData` 广播 + 输出指示器（`output.send("")` / `output.typing(…)`）+ TTS 同步收进 `enter_thinking` / `enter_executing` / `return_to_thinking` / `enter_speaking` / `finish_turn(has_background_tasks=…)` / `handle_interrupt()`。
- **`handle_interrupt` 的取消安全与幂等**：IDLE 时直接返回（避开 `IDLE → INTERRUPTED` 非法边）；IDLE 落位 + 广播放进 `finally`，后续 `task.cancel()` 打不断；中间那段可观测延迟保持可取消。资源清理（reset / cancel / clear）是流式管线的职责，不在本包。

## 文件布局

```
core/status/
├── __init__.py      — 重导出 AgentStatus / CognitivePhaseController / FSMBackedStatus / FSMBackedPhaseController
├── protocol.py      — 纯接口、零依赖：RunState + AgentStatus(ABC) + CognitivePhaseController(ABC)
├── fsm.py           — FSMBackedStatus + FSMBackedPhaseController（映射到 AgentFSM + EventBus + OutputManager + TTSStreamManager）
├── agent_fsm.py     — AgentFSM：底层状态机（re-export AgentState / VALID_TRANSITIONS）
└── tests/           — test_agent_fsm.py + test_agent_fsm_matrix.py（转换矩阵全量覆盖，期望值由 VALID_TRANSITIONS 现算）
```

## 依赖与所有权

- **方向**：`core.status → core.schema`（单向）。`protocol.py` 只依赖 stdlib；`fsm.py` 的 `EventBus` / `OutputManager` / `TTSStreamManager` 只在 `TYPE_CHECKING` 下引入。本包不 import `core.agent` / `core.input`——`RunState` 正是为断开这条反向依赖才从 `core.agent._types` 迁到接口层，后者仅 re-export；`make check-arch` 的 R2 禁止领域层反向依赖 `core.input`。
- **谁构造**：`Agent.__init__` 建 `FSMBackedStatus(self._fsm)` 赋给 `agent.status`，再建 `FSMBackedPhaseController(...)` 赋给 `agent.phases`，并把同一个 `status` 注入 `IdleMonitor`；`IdleMonitor` 未获注入时自行回退构造 `FSMBackedStatus(fsm)`。
- **谁消费**：`core.cognitive`（`loop.py` / `streaming.py` / `tool_phase.py` 经 `phases` 驱动转换、经 `status.accepts_new_turns` 判准入）、`core.agent`（停机窗口拒收）、`core.monitoring`（IdleMonitor）、`core.debug.info` 与 `core.input.health`（诊断名）。`core.input` 只经 `agent.status` 属性消费，**不 import 本包**。

## 不属于本包

- 回合编排与业务逻辑（属 `core.cognitive`）：本包只管状态，不管"谁在什么条件下该转换"。
- 相位枚举与转换表：`AgentState` / `VALID_TRANSITIONS` 归 `core.schema`。
- 资源清理与流式收尾：`handle_interrupt` 只落相位，reset / cancel / clear 由调用方完成。
- 生命周期启停：`RunState` 的驱动（`start()` / `stop()`）在 `core.agent`，本包只提供写入落点与查询。
- 对外暴露 FSM：外部一律走 `agent.status` 的语义接口。

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../README.md) | 快速开始 + 文档导航 |
| [core/README.md](../README.md) | Core 层总览（AgentStatus / AgentFSM 的定位） |
| [core/agent/README.md](../agent/README.md) | 组装根——`status` / `phases` 的构造与注入点 |
| [core/cognitive/README.md](../cognitive/README.md) | 相位控制器的驱动方 |
| [接口层设计 spec](../../docs/superpowers/specs/archive/2026-07/2026-07-30-agent-status-interface-design.md) | AgentStatus 接口设计（本层的上游 spec） |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 |
