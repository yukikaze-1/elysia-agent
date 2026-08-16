# Agent — 组装入口 + 生命周期管理

> 文档导航：[项目文档](../../README.md)

顶层组装器，持有所有子系统的引用并管理生命周期。使用双 mixin 模式：`Agent(AgentLifecycle, AgentServer)`。

## 架构

```
┌──────────────────────────────────────────────────────────────┐
│ Agent(AgentLifecycle, AgentServer)                           │
│                                                              │
│ __init__  创建所有组件、接线                                  │
│ start()   按序: DB → token+prompt → tool_context →           │
│           worker_pool → idle+events → service_state → tts → task_system →   │
│           mailbox_polling → db_health → attachment_repo → attachment_gc  │
│ stop()    按序: flush → cancel → close → save                │
│                                                              │
│ AgentLifecycle  — 所有 _start_*/_stop_*/_flush_* 方法        │
│ AgentServer    — run_server/run_repl + switch_*/set_service 热插拔  │
│ _types.py      — RunState, _PendingMessage, _BackgroundTask  │
└──────────────────────────────────────────────────────────────┘
```

## 数据流

```
用户输入 → process_message()
  → EventBus.post(USER_INPUT)
  → CognitiveLoop.process()
    → THINKING → (EXECUTING → THINKING)* → SPEAKING
  → _after_response()
    → MemoryConsolidator → CharacterReflector → MemoryReflector
  → IDLE (等待下次输入或 IdleMonitor 主动触发)
```

## 关键机制

### RunState 生命周期

```
STOPPED → start() → RUNNING → stop() → STOPPED
RUNNING → /restart → STOPPING → RESTARTING → start() → RUNNING
```

- `STOPPING` 阶段有序关闭所有子系统
- `RESTARTING` 阶段重新加载配置（`config.yaml` + `character/` + providers）
- `run_server(app_factory=...)`：HTTP 应用工厂由装配入口注入（`app.py` 传 `core.input.server.create_app`），AgentServer 只负责生命周期循环；未注入时 fail-fast，保证 `core.agent` 不反向依赖 `core.input`（CORE-MOD-P1-01）。
- 热切换 LLM/STT/TTS 无需重启：`switch_llm()` / `switch_stt()` / `switch_tts()`（先建后关，失败保留旧后端；`switch_stt` 真正建后端，不再只改 config）
- STT/TTS 后端模式开关：`set_service(service, enabled)`——TTS off 回落逐字文本流、STT off 停喂音频；当前模型 + 模式经 `session.service_state` 持久化（sessions 集合无自动 TTL，显式删除才清理），重启后由 `_apply_service_state` 启动步骤应用（三态：显式值 or config 回退）

### 消息处理模式

`process_message()` 支持：
- 纯文本：`agent.process_message("你好")`
- 多模态 content 数组：`agent.process_message([{"type": "text", "text": "描述这张图"}, {"type": "image_url", "image_url": {"url": "..."}}])`
- 异步非阻塞：消息投递到 EventBus 后立即返回

### 后台任务

`_background_tasks`（`dict[str, _BackgroundTaskEntry]`）追踪所有持续运行的后台 task：
- `_register_background_task(task_id, mailbox, intent, dispatched_at)` — CognitiveLoop 提交后台任务时注册 mailbox 到轮询列表
- `_poll_mailboxes()` — 每 500ms 轮询活跃任务 mailbox，处理完成/失败事件（由 `_start_mailbox_polling()` 启动）
- stop() 时 `_background_tasks.clear()` 清理残留后台任务（restart 复用同一实例）

### Archive-Session 同步

`purge_message_from_session(msg_id)` — 从当前 session 中移除指定消息。Archive（`message_archive` 集合）和 session（`sessions` 集合）是独立存储，debug 页面删除 Archive 消息时同步调用此方法，确保 LLM prompt 不再包含已删消息。仅在消息实际存在于 session 中时才执行清理和 `save()` 持久化。

## 文件布局

```
core/agent/
├── __init__.py                  — 重导出 Agent, RunState
├── agent.py                     — Agent.__init__ + process_message + 后台任务
├── lifecycle.py                 — AgentLifecycle: start/stop 编排
├── server.py                    — AgentServer: run_server（app_factory 注入，不再 import core.input）/run_repl + switch_*/set_service + banner
├── _types.py                    — RunState, _PendingMessage, _BackgroundTaskEntry
├── _lifecycle_persistence.py    — 持久化层启停 (MongoDB/Milvus/session/repos)
├── _lifecycle_services.py       — LLM/TTS/STT/Output/TokenTracker 启停 + _apply_service_state
├── _lifecycle_task_system.py    — WorkflowEngine/DelegationPreparer/mailbox 启停
└── _lifecycle_tools.py          — ToolAdapter/Scheduler/IdleMonitor/event loop 启停
```

关联文件:
```
core/
├── monitoring/event_bus.py  — EventBus 事件总线（agent + cognitive + idle 共享）
├── status/agent_fsm.py      — AgentFSM 状态机（agent + cognitive 共享）
├── config/                  — 配置子包（AppConfig + CharacterConfig）
```

## 调试

访问 `http://localhost:8765/debug` → **System** 卡片，可查看：
- FSM 状态（state + previous）
- mood（L4 情绪）
- RunState（STOPPED / RUNNING / STOPPING / RESTARTING）
- last_interaction_ago（距上次交互秒数）

后端 API: `GET /debug/api` → `info.fsm_state` / `info.mood` / `info.run_state`

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [项目 README](../../README.md) | 快速开始 + 完整文档导航 |
| [core/README.md](../README.md) | Core 层总览 |
| [docs/architecture.md](../../docs/architecture.md) | 完整架构 + 并发模型 |
| [core/cognitive/README.md](../cognitive/README.md) | 认知循环 — 管线核心执行引擎 |
