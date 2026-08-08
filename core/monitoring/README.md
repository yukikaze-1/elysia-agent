# Monitoring — 运行时可观测性

> 文档导航：[项目文档](../../README.md)

事件总线 + 空闲监控 + Token 追踪——三个独立子系统，通过 `Agent.__init__` 接线。

## 架构

```
┌──────────────────────────────────────────────────────────┐
│                   Monitoring 子系统                        │
│                                                          │
│  EventBus          ── 发布/订阅事件总线                     │
│    ├─ USER_INPUT / STATE_CHANGE / TASK_RESULT / INTERRUPT │
│    └─ 3 级优先级（INTERRUPT / STATE_CHANGE / NORMAL）          │
│                                                          │
│  IdleMonitor       ── 空闲检测 + 文本唤醒词 + 主动搭话        │
│    ├─ 唤醒词检测（character.wake_words 文本唤醒词）           │
│    ├─ 空闲超时主动搭话                                     │
│    └─ 后台任务结果唤醒                                     │
│                                                          │
│  TokenTracker      ── LLM token 用量追踪                  │
│    ├─ 每轮累计 + 回合级记录                                │
│    ├─ 广播到前端（on_token_usage）                         │
│    └─ 持久化到 MongoDB token_usage 集合                    │
└──────────────────────────────────────────────────────────┘
```

## 关键机制

### EventBus

- **优先级队列**：INTERRUPT(0) → STATE_CHANGE(1) → NORMAL(2)（3 级，数值越低越紧急）
- **事件类型**：`USER_INPUT`（用户输入到达）、`STATE_CHANGE`（FSM 状态转换）、`TASK_RESULT`（后台任务完成）、`INTERRUPT`（用户打断）、`SYSTEM_TICK`（定时器）
- **异步分发**：`post()` 非阻塞入队，`_process_events()` 独立 asyncio.Task 消费
- **data**：`EventData` 判别联合（5 种 `StateChangeData` / `UserInputData` / `InterruptData` / `TaskResultData` / `SystemTickData`）

### IdleMonitor

- **检测引擎**：定时器（可配间隔），检查上次交互距今时间
- **唤醒通道**：文本唤醒词（`character.wake_words`，来自角色 config）——IdleMonitor 本身只读该文本词表，不含 openWakeWord；语音唤醒词（openWakeWord 模型）检测在 `core/input/wakeword.py`（WakeWordDetector）+ `core/input/audio_buffer.py`（VAD 并行流式检测），由 input 层消费
- **主动搭话**：`proactive` 配置段——空闲超时 + 上下文条件 + cooldown
- **后台任务**：`TaskResultData` 事件触发，构造 wakeup message 注入 CognitiveLoop

### TokenTracker

- **回合追踪**：`start_turn()` → 每 LLM 调用 `record(usage)` → 回合结束广播 `on_token_usage`
- **累计统计**：`current_turn_usages: list[TokenUsage]`，回合结束写入 AssistantMessage.token_usage
- **持久化**：`TokenRepository` → MongoDB `token_usage` 集合，异步 fire-and-forget
- **前端**：`on_token_usage` → CharStatus 组件显示 session 累计 token

## 文件布局

```
core/monitoring/
├── __init__.py        — 重导出 + IdleMonitor PEP 562 懒加载
├── event_bus.py       — EventBus + EventPriority + EventType
├── idle_monitor.py    — IdleMonitor + 文本唤醒词（character.wake_words）+ 主动搭话
├── token_tracker.py   — TokenTracker（TokenUsage 在 core.schema.token）
└── tests/
    └── test_token_tracker.py

关联文件:
core/
├── agent/agent.py             — 创建 + 接线三个子系统
├── agent/_lifecycle_tools.py  — IdleMonitor 启停
├── cognitive/loop.py          — TokenTracker 记录 + 广播
└── schema/event.py            — EventData 判别联合（5 种）
```

## 调试

访问 `http://localhost:8765/debug` → **Overview** Tab，可查看：
- **Token** 卡片：当前轮次 token 用量（prompt / completion / total）
- 上一轮次 LLM 调用的 prompt 和 response（PromptRecorder / ResponseRecorder）

后端 API：`GET /debug/api` → `info.token_usage`

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../README.md) | 快速开始 + 完整文档导航 |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 + 并发模型 |
| [core/README.md](../README.md) | Core 层总览 |
| [core/agent/README.md](../agent/README.md) | Agent 组装——Monitoring 子系统的拥有者 |
