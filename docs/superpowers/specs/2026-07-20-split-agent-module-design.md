# Split Agent Module Design

> **Target:** 将 `core/agent.py`（1059 行）拆分为 `core/agent/` 包，保持外部 import 不变。

## 目标结构

```
core/agent/
├── __init__.py          # from .agent import Agent; from ._types import RunState
├── _types.py            # RunState, _PendingMessage, _BackgroundTaskEntry  — 共享类型，零依赖
├── agent.py             # Agent 核心: __init__ + 事件处理 + process_message + 后台任务
├── lifecycle.py         # AgentLifecycle mixin: start()/stop() 及所有 _start_*/_stop_*/_flush_*
└── server.py            # AgentServer mixin: run_server, run_repl, switch_*/get_wake_words, banner
```

## 各文件职责

### `_types.py` (~15 行) — 共享类型，零模块内依赖

- `RunState` enum（RUNNING / STOPPED / RESTARTING）
- `_PendingMessage` dataclass
- `_BackgroundTaskEntry` dataclass

被 agent.py、lifecycle.py、server.py 引用，本身不 import 包内任何模块，消除循环导入风险。

### `agent.py` (~380 行)

- `Agent.__init__` — 50+ 行属性初始化
- `Agent.process_message`, `next_request_id` — 消息入口
- `Agent._process_events` + 5 个 `_handle_*` — 事件分发
- `Agent._register_background_task`, `_poll_mailboxes` — 后台任务

```python
from ._types import RunState, _PendingMessage, _BackgroundTaskEntry
from .lifecycle import AgentLifecycle
from .server import AgentServer

class Agent(AgentLifecycle, AgentServer):
    """AI companion agent — orchestrates all subsystems."""
    def __init__(self, *, config, config_path=None):
        ...
```

### `lifecycle.py` (~450 行) — `AgentLifecycle` mixin

- `start()` + 8 个 `_start_*`（7 async: `_start_persistence`, `_start_token_and_prompt`, `_start_tool_context`, `_start_worker_pool`, `_start_idle_and_events`, `_start_tts`, `_start_task_system`；1 sync: `_start_mailbox_polling`）
- `stop()` + 15 个 `_stop_*`/`_flush_*`
- `_db_health_loop`

```python
from ._types import RunState
```

### `server.py` (~195 行) — `AgentServer` mixin

- `run_server` + `transcribe_wrapper` — aiohttp 入口
- `run_repl` — 终端入口
- `switch_llm`, `switch_stt`, `switch_tts`, `get_wake_words`
- `_print_startup_banner` — banner 格式化（唯一调用方是 run_server/run_repl，放 server 层）

```python
from ._types import RunState
```

## 循环导入防护

1. **`_types.py` 零依赖** — `RunState` 等共享类型抽到独立文件，被 agent/lifecycle/server import 但不反向依赖
2. **`server.py` 禁止顶层导入 `Module.input.server`** — `run_server()` 内部的 `from Module.input.server import create_app` 保持延迟导入。任何人不应在 server.py 顶部加此 import
3. **`lifecycle.py` 和 `server.py` 不 import agent.py** — 只 import `_types.py`

## 外部耦合

### 不受影响的引用

| 调用方 | 当前 import | 拆分后 |
|--------|-----------|--------|
| `Module/input/server.py:15` | `from core.agent import RunState` | 不变（`__init__.py` 透出） |
| `core/debug_info.py` | `from core.agent import Agent` (TYPE_CHECKING) | 不变 |
| `core/tests/test_phase3.py` | `from core.agent import Agent` | 不变 |
| `core/tests/test_task_scheduler.py` | `from core.agent import Agent` | 不变 |

### `debug_info.py` 的 `collect()`

访问了 23 个 `agent._*` 私有属性。所有属性在 `Agent.__init__` 中定义，mixin 只有方法。属性作用域透明，无需修改。

## 边界约束

1. **外部 import 不变**: `from core.agent import Agent` 和 `from core.agent import RunState` 通过 `__init__.py` 透出
2. **Mixin 只放方法**: `AgentLifecycle` 和 `AgentServer` 不设 `__init__`，所有属性在 `Agent.__init__` 中
3. **不改变逻辑**: 纯粹的文件拆分，不重构内部实现
4. **保持私有属性访问**: `self._xxx` 在 mixin 间直接访问
5. **server.py 顶层不 import Module.input.server**: 禁止闭合循环导入链
