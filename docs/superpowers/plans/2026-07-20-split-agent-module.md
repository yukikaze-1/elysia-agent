# Split Agent Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) to implement this plan task-by-task.

**Goal:** Split `core/agent.py` (1059 lines) into `core/agent/` package with 5 files — pure file extraction, zero logic changes.

**Architecture:** Agent class uses two mixins (AgentLifecycle, AgentServer) that inherit into Agent. Shared types live in `_types.py` to break circular import chains. Old `core/agent.py` → `_agent_backup.py` during transition.

**Tech Stack:** Python 3.11+, dataclasses, asyncio

## Global Constraints

1. **外部 import 不变**: `from core.agent import Agent` 和 `from core.agent import RunState` 通过 `__init__.py` 透出
2. **Mixin 只放方法**: `AgentLifecycle` 和 `AgentServer` 不设 `__init__`，所有属性在 `Agent.__init__` 中
3. **不改变逻辑**: 纯粹的文件拆分，不重构内部实现
4. **`_types.py` 零包内依赖**: 不被其他模块 import 反向依赖
5. **`server.py` 顶层不 import `core.input.server`**: 禁止闭合循环导入链
6. **所有外部引用路径不变**: `core/debug_info.py`, `core/input/server.py`, 所有测试文件无需修改

---

### Task 1: 备份旧文件 + 创建 `_types.py`

**Files:**
- Rename: `core/agent.py` → `core/_agent_backup.py`
- Create: `core/agent/_types.py`

**Interfaces:**
- Produces: `RunState`, `_PendingMessage`, `_BackgroundTaskEntry` — 被 Task 2/3/4 使用

- [ ] **Step 1: 备份旧文件**

```bash
mv core/agent.py core/_agent_backup.py
```

- [ ] **Step 2: 创建包目录**

```bash
mkdir -p core/agent
```

- [ ] **Step 3: 创建 `core/agent/_types.py`**

从 `core/_agent_backup.py` 提取 lines 53-76（辅助类 + RunState enum）：

```python
"""Agent 共享类型 — 零包内依赖，消除循环导入。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any


@dataclass
class _PendingMessage:
    """FIFO 队列中暂存的消息（FSM 忙碌时入队）。"""
    text: str
    user_name: str
    request_id: str
    content: list[dict] | None = None


@dataclass
class _BackgroundTaskEntry:
    """后台任务注册信息。"""
    intent: str
    dispatched_at: str
    mailbox: Any  # asyncio.Queue


class RunState(Enum):
    """Agent 生命周期状态"""
    STOPPED = auto()      # 初始 / 已停止
    RUNNING = auto()      # 运行中
    STOPPING = auto()     # 正常关机中
    RESTARTING = auto()   # 重启中
```

- [ ] **Step 4: 验证 — 导入 `_types.py` 不报错**

```bash
python3 -c "from core.agent._types import RunState, _PendingMessage, _BackgroundTaskEntry; print('OK')"
```

- [ ] **Step 5: Commit**

```bash
git add core/_agent_backup.py core/agent/_types.py
git commit -m "refactor: 备份 agent.py → _agent_backup.py，创建 agent/_types.py"
```

---

### Task 2: 创建 `lifecycle.py` — AgentLifecycle mixin

**Files:**
- Create: `core/agent/lifecycle.py`

**Interfaces:**
- Consumes: `RunState` from `core/agent._types` (Task 1)
- Produces: `AgentLifecycle` mixin class — 被 Task 4 的 `Agent` 继承

- [ ] **Step 1: 创建 `core/agent/lifecycle.py`**

提取 `core/_agent_backup.py` 中以下行范围的方法，放入 `AgentLifecycle` mixin 类：

| 方法 | 行范围 |
|------|--------|
| `start()` | 214–254 |
| `_start_persistence` | 256–282 |
| `_db_health_loop` | 284–299 |
| `_start_token_and_prompt` | 301–318 |
| `_start_tool_context` | 320–349 |
| `_start_worker_pool` | 351–355 |
| `_start_idle_and_events` | 357–360 |
| `_start_tts` | 362–382 |
| `_start_task_system` (含内嵌类 `_EventBusHITLNotifier`) | 384–423 |
| `_start_mailbox_polling` | 425–427 |
| `stop()` | 450–508 |
| `_flush_consolidator` | 510–520 |
| `_flush_reflector` | 522–530 |
| `_flush_compression` | 532–542 |
| `_stop_event_task` | 544–553 |
| `_stop_cognitive_and_tts` | 555–558 |
| `_stop_embedding_if_running` | 560–569 |
| `_stop_db_health` | 571–580 |
| `_stop_mailbox` | 582–590 |
| `_stop_task_system` | 592–598 |
| `_stop_scheduler` | 600–603 |
| `_stop_tool_manager` | 605–608 |
| `_stop_idle` | 610–613 |
| `_stop_persistence` | 615–656 |
| `_stop_llm_and_tts` | 658–671 |
| `_stop_output` | 673–679 |

```python
"""Agent 生命周期 — start/stop 编排及所有 _start_*/_stop_*/_flush_* 方法。"""
from __future__ import annotations

import asyncio
import time
import logging

from ._types import RunState

# 需要引用 self._xxx 属性的模块 — 保持与原始 agent.py 一致的 import
from .app_config import AppConfig
from .event_bus import EventBus, EventPriority, EventType
from .agent_fsm import AgentFSM, AgentState
from .log_setup import set_request_id, get_request_id
from .character_config import CharacterConfig
from .memory.session import SessionMemory
from .memory.long_term import LongTermMemory
from .prompt_assembler import PromptAssembler
from Module.tool import ToolManager, ToolContext, PluginContext
from core.tools import ALL_PLUGINS
from .cognitive_loop import CognitiveLoop
from .idle_monitor import IdleMonitor
from .llm_adapter import LLMAdapter
from .consolidator import MemoryConsolidator
from .personality import CharacterEngine
from .memory.memory_reflector import MemoryReflector
from core.schema.types import IsoTimestamp, SessionId
from core.schema.character import CharacterState
from core.schema.event import (
    InterruptData, UserInputData, TaskResultData, StateChangeData, SystemTickData,
)
from Module.embedding import EmbeddingFactory
from .task_scheduler import TaskScheduler
from core.storage.session_repo import SessionRepository
from core.storage.message_repo import MessageRepository
from core.storage.token_repo import TokenRepository
from core.storage.memory_repo import MemoryRepository

logger = logging.getLogger(__name__)


class AgentLifecycle:
    """Agent 生命周期 mixin — start/stop 编排。

    所有属性由 Agent.__init__ 初始化，本 mixin 不含 __init__。
    """

    # ================================================================
    # start()
    # ================================================================
    # [复制原始 agent.py lines 214–254 的 start() 方法体]
    # ...

    # ================================================================
    # _start_* 子方法（8 个）
    # ================================================================
    # [复制原始 agent.py lines 256–427 的 _start_* 方法体]
    # ...

    # ================================================================
    # stop()
    # ================================================================
    # [复制原始 agent.py lines 450–508 的 stop() 方法体]
    # ...

    # ================================================================
    # _flush_* / _stop_* 子方法（15 个）
    # ================================================================
    # [复制原始 agent.py lines 510–679 的 _flush_*/_stop_* 方法体]
    # ...
```

> 注意：`_start_task_system` 内部的 `_EventBusHITLNotifier` 类随方法一起复制。

- [ ] **Step 2: 验证 — 导入 `lifecycle.py` 不报错**

```bash
python3 -c "from core.agent.lifecycle import AgentLifecycle; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add core/agent/lifecycle.py
git commit -m "refactor: 创建 agent/lifecycle.py — AgentLifecycle mixin（start/stop 编排）"
```

---

### Task 3: 创建 `server.py` — AgentServer mixin

**Files:**
- Create: `core/agent/server.py`

**Interfaces:**
- Consumes: `RunState` from `core.agent._types` (Task 1)
- Produces: `AgentServer` mixin class — 被 Task 4 的 `Agent` 继承

- [ ] **Step 1: 创建 `core/agent/server.py`**

提取 `core/_agent_backup.py` 中以下方法，放入 `AgentServer` mixin 类：

| 方法 | 行范围 |
|------|--------|
| `_print_startup_banner` | 429–448 |
| `next_request_id` | 681–686 |
| `switch_llm` | 688–697 |
| `switch_stt` | 699–703 |
| `switch_tts` | 705–720 |
| `get_wake_words` | 759–761 |
| `run_server` (含内嵌函数 `transcribe_wrapper`) | 763–863 |
| `run_repl` | 865–885 |

```python
"""Agent 服务器入口 — run_server/run_repl + provider 切换。"""
from __future__ import annotations

import asyncio
import logging

from ._types import RunState

logger = logging.getLogger(__name__)


class AgentServer:
    """Agent 服务器 mixin — HTTP/REPL 入口 + provider 切换。

    所有属性由 Agent.__init__ 初始化，本 mixin 不含 __init__。
    """

    # ================================================================
    # _print_startup_banner
    # ================================================================
    # [复制原始 agent.py lines 429–448 的 _print_startup_banner 方法体]
    # ...

    # ================================================================
    # next_request_id
    # ================================================================
    # [复制原始 agent.py lines 681–686 的 next_request_id 方法体]
    # ...

    # ================================================================
    # switch_llm / switch_stt / switch_tts
    # ================================================================
    # [复制原始 agent.py lines 688–720 的三个 switch_* 方法体]
    # ...

    # ================================================================
    # get_wake_words
    # ================================================================
    # [复制原始 agent.py lines 759–761 的 get_wake_words 方法体]
    # ...

    # ================================================================
    # run_server
    # ================================================================
    # [复制原始 agent.py lines 763–863 的 run_server 方法体]
    # 注意：内部 from core.input.server import create_app 保持延迟导入
    # 注意：内嵌函数 transcribe_wrapper 随方法一起复制
    # ...

    # ================================================================
    # run_repl
    # ================================================================
    # [复制原始 agent.py lines 865–885 的 run_repl 方法体]
    # ...
```

- [ ] **Step 2: 验证 — 导入 `server.py` 不报错**

```bash
python3 -c "from core.agent.server import AgentServer; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add core/agent/server.py
git commit -m "refactor: 创建 agent/server.py — AgentServer mixin（server/repl + provider 切换）"
```

---

### Task 4: 创建 `agent.py` + `__init__.py` + 验证 + 清理

**Files:**
- Create: `core/agent/agent.py`
- Create: `core/agent/__init__.py`
- Delete: `core/_agent_backup.py`（验证通过后）

**Interfaces:**
- Consumes: `_types`, `AgentLifecycle` (Task 2), `AgentServer` (Task 3)
- Produces: `Agent` class — 被外部 import 使用

- [ ] **Step 1: 创建 `core/agent/agent.py`**

保留 `core/_agent_backup.py` 中以下部分，并添加 mixin 导入：

| 内容 | 行范围 |
|------|--------|
| 模块 docstring | 1–9 |
| imports（精简，去掉已在 mixin 中的） | 11–50 |
| `Agent.__init__` | 81–212 |
| `process_message` | 722–756 |
| `_register_background_task` | 889–895 |
| `_poll_mailboxes` | 897–933 |
| `_process_events` | 935–965 |
| `_handle_interrupt` | 967–979 |
| `_handle_user_input` | 981–1002 |
| `_handle_system_tick` | 1004–1022 |
| `_handle_task_result` | 1024–1036 |
| `_handle_state_change` | 1038–1059 |

Import 部分需要包含 `Agent` 自身需要的模块（不在 mixin 中的）：

```python
"""Agent — wires all L0/L1/L2 components into a runnable Agent.

Usage:
    agent = Agent(config=config)
    await agent.start()
    await agent.process_message("你好")       # single turn
    await agent.run_repl()                   # interactive REPL
    await agent.stop()
"""

from __future__ import annotations

import asyncio
import time
import os
from dataclasses import asdict
import logging
from typing import Any

from Module.output import OutputManager, ConsoleChannel

from ._types import RunState, _PendingMessage, _BackgroundTaskEntry
from .lifecycle import AgentLifecycle
from .server import AgentServer

from .app_config import AppConfig
from .event_bus import EventBus, EventPriority, EventType
from .agent_fsm import AgentFSM, AgentState
from .log_setup import set_request_id, get_request_id
from .character_config import CharacterConfig
from .memory.session import SessionMemory
from .memory.long_term import LongTermMemory
from .prompt_assembler import PromptAssembler
from Module.tool import ToolManager, ToolContext, PluginContext
from core.tools import ALL_PLUGINS
from .cognitive_loop import CognitiveLoop
from .idle_monitor import IdleMonitor
from .llm_adapter import LLMAdapter
from .consolidator import MemoryConsolidator
from .personality import CharacterEngine
from .memory.memory_reflector import MemoryReflector
from core.schema.types import IsoTimestamp, SessionId
from core.schema.character import CharacterState
from core.schema.event import (
    InterruptData, UserInputData, TaskResultData, StateChangeData, SystemTickData,
)
from Module.embedding import EmbeddingFactory
from .task_scheduler import TaskScheduler
from core.storage.session_repo import SessionRepository
from core.storage.message_repo import MessageRepository
from core.storage.token_repo import TokenRepository
from core.storage.memory_repo import MemoryRepository

logger = logging.getLogger(__name__)


class Agent(AgentLifecycle, AgentServer):
    """Complete Agent wired from L0/L1/L2 components."""

    def __init__(self, *, config: AppConfig, config_path: str | None = None):
        # [复制原始 agent.py lines 81–212 的 __init__ 方法体]
        ...

    # ================================================================
    # process_message + next_request_id
    # ================================================================
    # [复制原始 agent.py lines 722–756 的 process_message]
    # 注意: next_request_id 移到了 server.py, process_message 内部调用 self.next_request_id()
    # 因为 Agent 继承 AgentServer，所以 self.next_request_id() 可用
    # ...

    # ================================================================
    # _register_background_task / _poll_mailboxes
    # ================================================================
    # [复制原始 agent.py lines 889–933]
    # ...

    # ================================================================
    # _process_events + 5 个 _handle_*
    # ================================================================
    # [复制原始 agent.py lines 935–1059]
    # ...
```

- [ ] **Step 2: 创建 `core/agent/__init__.py`**

```python
"""Agent 包 — 对外透出 Agent 和 RunState。"""
from .agent import Agent
from ._types import RunState
```

- [ ] **Step 3: 运行测试验证**

```bash
python3 -m pytest -m "unit or service" -q -x --tb=short
```

Expected: 239 passed, 2 skipped, 1 error (test_server — pre-existing). 不应有新的 failure。

- [ ] **Step 4: 删除备份文件**

```bash
rm core/_agent_backup.py
```

- [ ] **Step 5: 确认外部 import 仍正常**

```bash
python3 -c "from core.agent import Agent, RunState; print('Agent:', Agent); print('RunState:', RunState)"
```

- [ ] **Step 6: Commit**

```bash
git add core/agent/agent.py core/agent/__init__.py
git rm core/_agent_backup.py
git commit -m "refactor: 完成 agent 包拆分 — agent + lifecycle + server + _types"
```

---

## Execution Order

Tasks MUST run sequentially (Task 1 → 2 → 3 → 4) — each task creates files consumed by later tasks.
