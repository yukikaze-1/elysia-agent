# core/config/ 和 core/task/ 子包拆分

> 状态: spec  
> 日期: 2026-07-26  
> 关联: [ToolAdapter](2026-07-26-tool-adapter-design.md) · [ErrorType 迁移](2026-07-26-error-type-move-design.md) · 散文件归档

## 背景

`core/` 根目录有 11 个 `.py` 文件，形成无组织的平铺结构。其中 `app_config.py` + `character_config.py` 和 `task.py` + `task_scheduler.py` 分别形成两个自然的内聚组。

## 目标

将 4 个散文件移入两个子包，减小根目录噪音。

## 设计

### `core/config/` — 配置类

```
core/config/
├── __init__.py              # 重导出 AppConfig + CharacterConfig
├── app_config.py            # 从 core/ 根移入
└── character_config.py      # 从 core/ 根移入
```

两个文件都零内部依赖、纯 dataclass。

`__init__.py` 重导出：
```python
from .app_config import AppConfig, LLMConfig, MemoryConfig, ServerConfig, TTSConfig
from .character_config import CharacterConfig, EmotionConfig, ProactiveConfig
```

### `core/task/` — 任务调度

```
core/task/
├── __init__.py              # 重导出
├── task.py                  # 从 core/ 根移入（Task model）
└── scheduler.py             # 从 core/task_scheduler.py 移入并重命名
```

避免 `task_scheduler.py` 放在 `task/` 包内的 stutter（`from core.task.task_scheduler import TaskScheduler`）。

`__init__.py` 重导出：
```python
from .task import Task, TaskType, TaskStatus, ToolCallPayload
from .scheduler import TaskScheduler
```

### import 变更汇总

#### `core/config/` 迁移

| 旧 import | 新 import | 文件 |
|----------|----------|------|
| `from ..app_config import AppConfig` | `from ..config import AppConfig` | `agent/agent.py:31` |
| `from ..character_config import CharacterConfig` | `from ..config import CharacterConfig` | `agent/agent.py:35` |
| `from ..app_config import AppConfig` | `from ..config import AppConfig` | `agent/lifecycle.py:14` |
| `from ..character_config import CharacterConfig` | `from ..config import CharacterConfig` | `agent/lifecycle.py:18` |
| `from core.app_config import AppConfig` | `from core.config import AppConfig` | `agent/server.py:171` （函数内 lazy import） |
| `from .character_config import CharacterConfig` | `from .config import CharacterConfig` | `idle_monitor.py:14` |
| `from .character_config import CharacterConfig` | `from .config import CharacterConfig` | `prompt_assembler.py:25` |
| `from ..app_config import AppConfig` | `from ..config import AppConfig` | `adapter/tool_adapter.py:17` （TYPE_CHECKING） |
| `from core.app_config import AppConfig` | `from core.config import AppConfig` | `core/tests/` 各类测试文件（~15 处 lazy import） |
| `from core.character_config import CharacterConfig` | `from core.config import CharacterConfig` | `core/tests/` 各类测试文件（~10 处 lazy import） |

#### `core/task/` 迁移

| 旧 import | 新 import | 文件 |
|----------|----------|------|
| `from ..task_scheduler import TaskScheduler` | `from ..task import TaskScheduler` | `agent/agent.py:55` |
| `from ..task_scheduler import TaskScheduler` | `from ..task import TaskScheduler` | `agent/lifecycle.py:35` |
| `from .task import Task, TaskType, TaskStatus` | 不变（内部相对 import） | `scheduler.py:21` （原 `task_scheduler.py`） |
| `from core.task import Task, TaskType, TaskStatus` | `from core.task import Task, TaskType, TaskStatus` | `core/tests/test_task_model.py:4` |
| `from core.task import Task, TaskType, TaskStatus` | 不变 | `core/tests/test_task_model.py` |
| `from core.task_scheduler import TaskScheduler` | `from core.task import TaskScheduler` | `core/tests/test_task_scheduler.py:13` |

`task.py` 内部依赖 `from .message_id import new_message_id` —— 移入 `core/task/` 后变为 `from ..message_id import new_message_id`（message_id.py 仍在根）。

### 不变

- 所有类和函数的行为完全不变
- `Module/` 层文件不受影响（不引用 core/config 或 core/task）
- 除上述路径外的其他文件不受影响

## 验证

```bash
# 确认 import 正常
python -c "from core.config import AppConfig, CharacterConfig; print('OK')"
python -c "from core.task import Task, TaskType, TaskStatus, TaskScheduler; print('OK')"

# 快速回归
make test-quick
```
