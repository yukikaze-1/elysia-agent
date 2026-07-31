# AgentLifecycle 拆分 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 将 `core/agent/lifecycle.py`（522 行）拆成 4 个 mixin + 1 个 facade，startup 侧从手写序列改为数据驱动循环。

**架构：** 每个 mixin 文件定义一个类（如 `_LifecyclePersistence`），包含相关子系统的 `_start_*`/`_stop_*`/`_flush_*` 方法。`AgentLifecycle` 继承所有 mixin，通过 `_STARTUP_STEPS` 和 `_SHUTDOWN_STEPS` 注册表 + `getattr` 循环编排启停。

**技术栈：** Python 3.11+, asyncio

## 全局约束

- 所有方法体逻辑不变——只搬家（唯一例外：`_start_token_and_prompt` 移除 `mongo_ok` 参数，改读 `self._mongo_ok`）
- `_SHUTDOWN_STEPS` 列表不变
- `AgentLifecycle` 公开方法签名不变（`start()`/`stop()`）
- 测试不修改
- 遵循 CharacterEngine 拆分的 mixin 模式

---

### Task 1: 创建 PersistenceMixin（`_lifecycle_persistence.py`）

**文件：**
- 新建: `core/agent/_lifecycle_persistence.py`
- 修改: `core/agent/lifecycle.py`（移走 5 个方法 + 对应 import）

**接口：**
- 产出: `class _LifecyclePersistence` — 5 个方法

**需移动的方法（从 lifecycle.py 复制，不修改逻辑）：**

| 方法 | 行号 | 说明 |
|------|------|------|
| `_start_persistence` | 163-192 | MongoDB/Milvus 连接 + session 加载 + JSONL 恢复 |
| `_db_health_loop` | 194-209 | 120s 数据库健康检查循环 |
| `_start_db_health` | — | **新建**：`self._db_health_task = asyncio.create_task(self._db_health_loop())`（原在 start() 内联） |
| `_stop_db_health` | 408-417 | cancel health check task |
| `_stop_persistence` | 452-499 | session.save() → LTM.close() → 5× repo.close() → EmbeddingFactory.shutdown() |

**需移动的 import（从 lifecycle.py 移到 _lifecycle_persistence.py）：**

```python
import os
from ..memory.session import SessionMemory
from ..memory.long_term import LongTermMemory
from core.storage.session_repo import SessionRepository
from core.storage.message_repo import MessageRepository
from core.storage.token_repo import TokenRepository
from core.storage.memory_repo import MemoryRepository
from Module.embedding import EmbeddingFactory
```

- [ ] **Step 1: 创建 `_lifecycle_persistence.py` 文件**

```python
"""Agent 生命周期 — 持久化层 (MongoDB/Milvus/session/repos) 启停。"""
from __future__ import annotations

import asyncio
import os
import logging
from typing import TYPE_CHECKING

from Module.embedding import EmbeddingFactory

if TYPE_CHECKING:
    from core.storage.character_version_repo import CharacterVersionRepository

logger = logging.getLogger(__name__)


class _LifecyclePersistence:
    """持久化层生命周期 mixin。"""
```

- [ ] **Step 2: 从 lifecycle.py 复制 5 个方法到新文件**

从 `lifecycle.py` 复制以下方法到 `_LifecyclePersistence` 类中（方法体完全不变）：
- `_start_persistence`（line 163-192）
- `_db_health_loop`（line 194-209）
- `_stop_db_health`（line 408-417）
- `_stop_persistence`（line 452-499）

加上新增的 `_start_db_health`：

```python
    def _start_db_health(self) -> None:
        """启动数据库健康检查后台任务。"""
        self._db_health_task = asyncio.create_task(self._db_health_loop())
```

`_start_persistence` 末尾需要加两行（把返回值写入 self）：

在 `return mongo_ok, milvus_ok` 之前插入：
```python
        self._mongo_ok = mongo_ok
        self._milvus_ok = milvus_ok
```

- [ ] **Step 3: 从 lifecycle.py 删除已移动的方法和 import**

删除 `lifecycle.py` 中对应的方法和不再需要的 import（`os`、`SessionMemory`、`LongTermMemory`、`SessionRepository`、`MessageRepository`、`TokenRepository`、`MemoryRepository`、`EmbeddingFactory`）。保留这些 import 如果 lifecycle.py 中其他方法仍在用。

- [ ] **Step 4: 在 lifecycle.py 中添加 mixin 导入和继承**

在 `lifecycle.py` 顶部添加：
```python
from ._lifecycle_persistence import _LifecyclePersistence
```

修改类声明：
```python
class AgentLifecycle(_LifecyclePersistence):
```

- [ ] **Step 5: 验证**

```bash
python -c "from core.agent.lifecycle import AgentLifecycle; print('OK')"
pytest core/tests/test_phase3.py -v -k "Lifecycle" --tb=short
```

- [ ] **Step 6: 提交**

```bash
git add core/agent/_lifecycle_persistence.py core/agent/lifecycle.py
git commit -m "refactor: extract PersistenceMixin from AgentLifecycle"
```

---

### Task 2: 创建 ServicesMixin（`_lifecycle_services.py`）

**文件：**
- 新建: `core/agent/_lifecycle_services.py`
- 修改: `core/agent/lifecycle.py`（移走 4 个方法 + 对应 import，追加 mixin 继承）

**接口：**
- 产出: `class _LifecycleServices` — 4 个方法

**需移动的方法：**

| 方法 | 行号 | 说明 |
|------|------|------|
| `_start_token_and_prompt` | 211-231 | TokenTracker + PromptRecorder + ResponseRecorder。**签名改为 `(self)`，`mongo_ok` 改读 `self._mongo_ok`** |
| `_start_tts` | 259-279 | TTS factory + 连通性测试 |
| `_stop_llm_and_tts` | 501-514 | LLM.close() + TTS.close() |
| `_stop_output` | 516-522 | output.close() |

**需移动的 import：**

```python
from ..adapter.llm_adapter import LLMAdapter
from ..config import AppConfig
from ..config import CharacterConfig
from Module.output import OutputManager, ConsoleChannel
```

- [ ] **Step 1: 创建 `_lifecycle_services.py`**

```python
"""Agent 生命周期 — LLM/TTS/Output/Token 服务启停。"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ..config import AppConfig
from Module.output import OutputManager

if TYPE_CHECKING:
    from ..adapter.llm_adapter import LLMAdapter
    from Module.tts.protocol import TTSBackend

logger = logging.getLogger(__name__)


class _LifecycleServices:
    """LLM/TTS/Output 服务生命周期 mixin。"""
```

- [ ] **Step 2: 从 lifecycle.py 复制 4 个方法**

`_start_token_and_prompt` 修改签名：删掉 `mongo_ok: bool` 参数，方法体内 `if mongo_ok:` → `if self._mongo_ok:`（line 219）。

其余方法体完全不变。

- [ ] **Step 3: 从 lifecycle.py 删除 + 更新 facade 类继承**

类声明改为：
```python
class AgentLifecycle(_LifecyclePersistence, _LifecycleServices):
```

- [ ] **Step 4: 验证 + 提交**

```bash
python -c "from core.agent.lifecycle import AgentLifecycle; print('OK')"
pytest core/tests/test_phase3.py -v -k "Lifecycle" --tb=short
git add core/agent/_lifecycle_services.py core/agent/lifecycle.py
git commit -m "refactor: extract ServicesMixin from AgentLifecycle"
```

---

### Task 3: 创建 ToolsMixin（`_lifecycle_tools.py`）

**文件：**
- 新建: `core/agent/_lifecycle_tools.py`
- 修改: `core/agent/lifecycle.py`（移走 9 个方法 + 对应 import，追加 mixin 继承）

**接口：**
- 产出: `class _LifecycleTools` — 9 个方法

**需移动的方法（全部逻辑不变）：**

| 方法 | 行号 |
|------|------|
| `_start_tool_context` | 233-246 |
| `_start_worker_pool` | 248-252 |
| `_start_idle_and_events` | 254-257 |
| `_stop_event_task` | 381-390 |
| `_stop_cognitive_and_tts` | 392-395 |
| `_stop_embedding_if_running` | 397-406 |
| `_stop_scheduler` | 437-440 |
| `_stop_tool_adapter` | 442-445 |
| `_stop_idle` | 447-450 |

**需移动的 import：**

```python
from ..event_bus import EventBus, EventPriority, EventType
from ..agent_fsm import AgentFSM, AgentState
from ..cognitive import CognitiveLoop
from ..idle_monitor import IdleMonitor
from core.schema.event import (
    InterruptData, UserInputData, TaskResultData, StateChangeData, SystemTickData,
)
```

- [ ] **Step 1: 创建 `_lifecycle_tools.py`，复制 9 个方法（逻辑不变），删除 lifecycle.py 中对应代码**
- [ ] **Step 2: 更新 facade 类继承：追加 `_LifecycleTools`**
- [ ] **Step 3: 验证 + 提交**

```bash
git add core/agent/_lifecycle_tools.py core/agent/lifecycle.py
git commit -m "refactor: extract ToolsMixin from AgentLifecycle"
```

---

### Task 4: 创建 TaskSystemMixin（`_lifecycle_task_system.py`）

**文件：**
- 新建: `core/agent/_lifecycle_task_system.py`
- 修改: `core/agent/lifecycle.py`（移走 4 个方法 + 对应 import，追加 mixin 继承）

**接口：**
- 产出: `class _LifecycleTaskSystem` — 4 个方法

**需移动的方法（全部逻辑不变）：**

| 方法 | 行号 |
|------|------|
| `_start_task_system` | 281-321 |
| `_start_mailbox_polling` | 323-325 |
| `_stop_mailbox` | 419-427 |
| `_stop_task_system` | 429-435 |

- [ ] **Step 1: 创建 `_lifecycle_task_system.py`，复制 4 个方法（逻辑不变），删除 lifecycle.py 中对应代码**
- [ ] **Step 2: 更新 facade 类继承：追加 `_LifecycleTaskSystem`**
- [ ] **Step 3: 验证 + 提交**

```bash
git add core/agent/_lifecycle_task_system.py core/agent/lifecycle.py
git commit -m "refactor: extract TaskSystemMixin from AgentLifecycle"
```

---

### Task 5: 完成 facade — `_STARTUP_STEPS` + 重写 `start()`

**文件：**
- 修改: `core/agent/lifecycle.py`

现在 `lifecycle.py` 只包含：`AgentLifecycle` 类 + `_SHUTDOWN_STEPS` + `stop()` + 3 个 flush 方法 + TYPE_CHECKING 属性声明。这一步加上 `_STARTUP_STEPS` 并重写 `start()`。

- [ ] **Step 1: 添加 `_STARTUP_STEPS` 注册表**

在 `_SHUTDOWN_STEPS` 旁边添加：

```python
    _STARTUP_STEPS: list[tuple[str, str]] = [
        # persistence (_start_persistence) 在 start() 中单独处理——需要捕获返回值
        ("token_and_prompt", "_start_token_and_prompt"),
        ("tool_context",     "_start_tool_context"),
        ("worker_pool",      "_start_worker_pool"),
        ("idle_and_events",  "_start_idle_and_events"),
        ("tts",              "_start_tts"),
        ("task_system",      "_start_task_system"),
        ("mailbox_polling",  "_start_mailbox_polling"),
        ("db_health",        "_start_db_health"),
    ]
```

- [ ] **Step 2: 重写 `start()` 方法**

```python
    async def start(self) -> None:
        """Start the agent: connect persistence, idle monitor, event loop."""
        logger.info("Agent starting...")
        self._state = RunState.RUNNING
        self._started_at = time.monotonic()
        self._fsm.reset()
        await self._llm.reconnect()

        # persistence 特殊处理——先于循环，返回值由 _start_persistence() 写入 self
        await self._start_persistence()

        # 其余步骤走 _STARTUP_STEPS 数据驱动循环
        for name, method_name in self._STARTUP_STEPS:
            logger.debug("Startup step: %s", name)
            method = getattr(self, method_name)
            try:
                result = method()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                logger.error("Startup step %s failed", name, exc_info=True)
                raise
```

- [ ] **Step 3: 清理残余 import**

删掉不再被 `lifecycle.py` 直接使用的 import。`time` 保留（`start()` 中 `time.monotonic()`），其余 import 应该都已经移到各 mixin 文件中。

- [ ] **Step 4: 验证 + 提交**

```bash
python -c "from core.agent.lifecycle import AgentLifecycle; print('OK')"
pytest core/tests/test_phase3.py -v -k "Lifecycle" --tb=short
pytest core/tests/test_phase3.py -v --tb=short
```

```bash
git add core/agent/lifecycle.py
git commit -m "refactor: add _STARTUP_STEPS registry, data-driven start()"
```
