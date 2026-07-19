# Split Agent Module Design

> **Target:** 将 `core/agent.py`（1059 行）拆分为 `core/agent/` 包，保持外部 import 不变。

## 目标结构

```
core/agent/
├── __init__.py          # from core.agent.agent import Agent
├── agent.py             # Agent 核心: __init__ + 事件处理 + process_message + 后台任务
├── lifecycle.py         # start()/stop() 及所有 _start_*/_stop_*/_flush_*
└── server.py            # run_server, run_repl, switch_*/get_wake_words
```

## 各文件职责

### `agent.py` (~400 行)

- `_PendingMessage`, `_BackgroundTaskEntry`, `RunState` — 辅助类/枚举
- `Agent.__init__` — 50+ 行属性初始化
- `Agent.process_message`, `next_request_id` — 消息入口
- `Agent._process_events` + 5 个 `_handle_*` — 事件分发
- `Agent._register_background_task`, `_poll_mailboxes` — 后台任务

### `lifecycle.py` (~420 行) — `AgentLifecycle` mixin

- `start()` + 10 个 `_start_*` 方法
- `stop()` + 15 个 `_stop_*`/`_flush_*` 方法
- `_db_health_loop`, `_print_startup_banner`

### `server.py` (~250 行) — `AgentServer` mixin

- `run_server` + `transcribe_wrapper` — aiohttp 入口
- `run_repl` — 终端入口
- `switch_llm`, `switch_stt`, `switch_tts`, `get_wake_words`

## Agent 类定义

```python
# agent.py
class Agent(AgentLifecycle, AgentServer):
    """AI companion agent — orchestrates all subsystems."""
    def __init__(self, *, config: AppConfig, config_path: str | None = None):
        ...
```

## 边界约束

1. **外部 import 不变**: `from core.agent import Agent` 仍然有效，通过 `__init__.py` 透出
2. **测试不变**: `core/tests/test_phase3.py` 等继续 `from core.agent import Agent`
3. **Mixin 只放方法**: `AgentLifecycle` 和 `AgentServer` 不设 `__init__`，所有属性初始化在 `Agent.__init__` 中
4. **不改变逻辑**: 纯粹的文件拆分，不重构内部实现
5. **保持私有属性访问**: `self._xxx` 在 mixin 间直接访问，不增加 getter/setter
