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

所有 import 统一改为通过 `__init__.py` 重导出路径：

#### `core/config/` 迁移 — 相对 import（core/ 内部，7 处）

| 旧 import | 新 import | 文件 |
|----------|----------|------|
| `from ..app_config import AppConfig` | `from ..config import AppConfig` | `agent/agent.py:31` |
| `from ..character_config import CharacterConfig` | `from ..config import CharacterConfig` | `agent/agent.py:35` |
| `from ..app_config import AppConfig` | `from ..config import AppConfig` | `agent/lifecycle.py:14` |
| `from ..character_config import CharacterConfig` | `from ..config import CharacterConfig` | `agent/lifecycle.py:18` |
| `from .character_config import CharacterConfig` | `from .config import CharacterConfig` | `idle_monitor.py:14` |
| `from .character_config import CharacterConfig` | `from .config import CharacterConfig` | `prompt_assembler.py:25` |
| `from ..app_config import AppConfig` | `from ..config import AppConfig` | `adapter/tool_adapter.py:17` （TYPE_CHECKING） |

#### `core/config/` 迁移 — 绝对 import（`core.config` 路径，35 处）

`from core.app_config import AppConfig` → `from core.config import AppConfig`（20 处）：

| 文件 | 行 |
|------|-----|
| `app.py` | 88 |
| `Module/bench/bench.py` | 89 |
| `Module/input/tests/test_server.py` | 22 |
| `core/agent/server.py` | 171 |
| `core/tests/test_phase2.py` | 239, 261, 285, 315, 880 |
| `core/tests/test_phase1.py` | 307, 331, 365, 393, 425, 473 |
| `core/tests/test_task_scheduler.py` | 765, 785, 825, 853 |
| `core/tests/test_phase3.py` | 26 |

`from core.character_config import CharacterConfig` → `from core.config import CharacterConfig`（15 处）：

| 文件 | 行 |
|------|-----|
| `Module/system_prompt/tests/integration/test_full_render.py` | 55 |
| `core/tests/test_mood_engine.py` | 109 |
| `core/tests/test_phase2.py` | 344, 364 |
| `core/tests/test_phase1.py` | 114, 442 |
| `core/tests/test_phase3.py` | 136, 628, 702, 1192, 1201, 1211 |
| `core/personality/tests/test_reflector.py` | 189 |
| `core/personality/tests/test_engine.py` | 20, 46 |

#### `core/task/` 迁移（3 处，含内部调整共 4 处）

| 旧 import | 新 import | 文件 |
|----------|----------|------|
| `from ..task_scheduler import TaskScheduler` | `from ..task import TaskScheduler` | `agent/agent.py:55` |
| `from ..task_scheduler import TaskScheduler` | `from ..task import TaskScheduler` | `agent/lifecycle.py:35` |
| `from core.task_scheduler import TaskScheduler` | `from core.task import TaskScheduler` | `core/tests/test_task_scheduler.py:13` |

以下 import 通过 `__init__.py` 重导出自动兼容，**无需修改**：

| import | 文件 |
|--------|------|
| `from core.task import Task, TaskType, TaskStatus` | `core/tests/test_task_model.py:4,119`, `core/tests/test_task_scheduler.py:12` |

#### 内部相对 import 调整

| 文件 | 旧 import | 新 import |
|------|----------|----------|
| `core/task/scheduler.py`（原 `core/task_scheduler.py`） | `from .task import Task, TaskType, TaskStatus` | 不变（同一包内） |
| `core/task/task.py`（原 `core/task.py`） | `from .message_id import new_message_id` | `from ..message_id import new_message_id` |

### 变更总计

| 类别 | 数量 |
|------|------|
| 文件新建 | 2 个 `__init__.py` |
| 文件移动 | 4 个 |
| 相对 import 翻向 | 8 处 |
| 绝对 import 翻向 | 36 处 |
| 总计受影响文件 | ~20 |

### 不变

- 所有类和函数的行为完全不变
- `Module/tool/`、`Module/llm/`、`Module/tts/`、`Module/output/` 等后端模块不受影响
- `core/tools/`、`core/schema/`、`core/storage/`、`core/memory/`、`core/cognitive/` 等子包不受影响
- `core/adapter/` 除 `tool_adapter.py:17` 一行 TYPE_CHECKING import 外不受影响

## 验证

```bash
# 确认 import 正常
python -c "from core.config import AppConfig, CharacterConfig; print('OK')"
python -c "from core.task import Task, TaskType, TaskStatus, TaskScheduler; print('OK')"

# 快速回归
make test-quick
```
