# core/config/ and core/task/ Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move 4 loose files from `core/` root into `core/config/` and `core/task/` sub-packages with `__init__.py` re-exports, updating 45 import sites across ~20 files.

**Architecture:** Pure file reorganization — zero behavior changes. Each new sub-package gets an `__init__.py` that re-exports all public symbols so consumers can use `from core.config import AppConfig, CharacterConfig` and `from core.task import Task, TaskScheduler` instead of the old direct paths.

**Tech Stack:** Python 3.11+, git mv, sed for bulk import updates

## Global Constraints

- 所有类和函数行为完全不变
- `core/task_scheduler.py` 重命名为 `core/task/scheduler.py`（避免 `core.task.task_scheduler` stutter）
- 每个子包 `__init__.py` 重导出所有公开符号
- 所有 import 通过 `__init__.py` 重导出路径（如 `from core.config import AppConfig`），不直接引用内部模块
- 所有现有测试通过：`make test-quick`

---

### Task 1: Create core/config/ — move app_config + character_config

**Files:**
- Create: `core/config/__init__.py`
- Move: `core/app_config.py` → `core/config/app_config.py`
- Move: `core/character_config.py` → `core/config/character_config.py`
- Modify: 35 absolute + 7 relative import sites across ~18 files

**Interfaces:**
- Produces: `core.config` package re-exporting `AppConfig`, `LLMConfig`, `MemoryConfig`, `ServerConfig`, `TTSConfig`, `CharacterConfig`, `EmotionConfig`, `ProactiveConfig`

- [ ] **Step 1: Create `core/config/__init__.py`**

```python
"""core/config/ — 应用配置和角色配置。"""
from .app_config import AppConfig, LLMConfig, MemoryConfig, ServerConfig, TTSConfig
from .character_config import CharacterConfig, EmotionConfig, ProactiveConfig

__all__ = [
    "AppConfig", "LLMConfig", "MemoryConfig", "ServerConfig", "TTSConfig",
    "CharacterConfig", "EmotionConfig", "ProactiveConfig",
]
```

- [ ] **Step 2: Move files**

```bash
git mv core/app_config.py core/config/app_config.py
git mv core/character_config.py core/config/character_config.py
```

- [ ] **Step 3: Update 7 relative imports in core/**

| 文件:行 | 旧 | 新 |
|---------|-----|-----|
| `core/agent/agent.py:31` | `from ..app_config import AppConfig` | `from ..config import AppConfig` |
| `core/agent/agent.py:35` | `from ..character_config import CharacterConfig` | `from ..config import CharacterConfig` |
| `core/agent/lifecycle.py:14` | `from ..app_config import AppConfig` | `from ..config import AppConfig` |
| `core/agent/lifecycle.py:18` | `from ..character_config import CharacterConfig` | `from ..config import CharacterConfig` |
| `core/idle_monitor.py:14` | `from .character_config import CharacterConfig` | `from .config import CharacterConfig` |
| `core/prompt_assembler.py:25` | `from .character_config import CharacterConfig` | `from .config import CharacterConfig` |
| `core/adapter/tool_adapter.py:17` | `from ..app_config import AppConfig` (TYPE_CHECKING) | `from ..config import AppConfig` |

- [ ] **Step 4: Update 35 absolute imports**

Replace `from core.app_config import AppConfig` → `from core.config import AppConfig`:

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

Replace `from core.character_config import CharacterConfig` → `from core.config import CharacterConfig`:

| 文件 | 行 |
|------|-----|
| `Module/system_prompt/tests/integration/test_full_render.py` | 55 |
| `core/tests/test_mood_engine.py` | 109 |
| `core/tests/test_phase2.py` | 344, 364 |
| `core/tests/test_phase1.py` | 114, 442 |
| `core/tests/test_phase3.py` | 136, 628, 702, 1192, 1201, 1211 |
| `core/personality/tests/test_reflector.py` | 189 |
| `core/personality/tests/test_engine.py` | 20, 46 |

- [ ] **Step 5: Verify config imports**

```bash
python -c "from core.config import AppConfig, CharacterConfig; print('OK')"
```

Expected: OK

- [ ] **Step 6: Commit**

```bash
git add core/config/ core/agent/agent.py core/agent/lifecycle.py core/agent/server.py core/idle_monitor.py core/prompt_assembler.py core/adapter/tool_adapter.py app.py Module/bench/bench.py Module/input/tests/test_server.py Module/system_prompt/tests/integration/test_full_render.py core/tests/ core/personality/tests/
git commit -m "refactor: move app_config + character_config into core/config/"
```

---

### Task 2: Create core/task/ — move task.py + task_scheduler.py

**Files:**
- Create: `core/task/__init__.py`
- Move: `core/task.py` → `core/task/task.py`
- Move: `core/task_scheduler.py` → `core/task/scheduler.py` （重命名，避免 stutter）
- Modify: 3 absolute/relative import sites + 1 internal import

**Interfaces:**
- Consumes: `core.config` from Task 1 (scheduler.py does not reference config, but must work alongside it)
- Produces: `core.task` package re-exporting `Task`, `TaskType`, `TaskStatus`, `ToolCallPayload`, `TaskScheduler`

- [ ] **Step 1: Move files first（必须先移，`core/task.py` 文件阻塞目录创建）**

```bash
git mv core/task.py core/task/task.py
git mv core/task_scheduler.py core/task/scheduler.py
```

- [ ] **Step 2: Create `core/task/__init__.py`**

```python
"""core/task/ — 任务数据模型和调度器。"""
from .task import Task, TaskType, TaskStatus, ToolCallPayload
from .scheduler import TaskScheduler

__all__ = [
    "Task", "TaskType", "TaskStatus", "ToolCallPayload",
    "TaskScheduler",
]
```

- [ ] **Step 3: Fix internal import in `task.py`**

`core/task/task.py` line 15 (`from .message_id import new_message_id`) — 移入子包后 `.message_id` 不再在同一目录，需改为 `..message_id`：

```diff
- from .message_id import new_message_id
+ from ..message_id import new_message_id
```

- [ ] **Step 4: Update 3 external import sites**

| 文件:行 | 旧 | 新 |
|---------|-----|-----|
| `core/agent/agent.py:55` | `from ..task_scheduler import TaskScheduler` | `from ..task import TaskScheduler` |
| `core/agent/lifecycle.py:35` | `from ..task_scheduler import TaskScheduler` | `from ..task import TaskScheduler` |
| `core/tests/test_task_scheduler.py:13` | `from core.task_scheduler import TaskScheduler` | `from core.task import TaskScheduler` |

以下 import 通过 `__init__.py` 重导出自动兼容，**无需修改**：
- `core/tests/test_task_model.py:4,119` — `from core.task import Task, TaskType, TaskStatus` ✅
- `core/tests/test_task_scheduler.py:12` — `from core.task import Task, TaskType, TaskStatus` ✅

- [ ] **Step 5: Verify task imports**

```bash
python -c "from core.task import Task, TaskType, TaskStatus, TaskScheduler; print('OK')"
```

Expected: OK

- [ ] **Step 6: Run full regression**

```bash
make test-quick
```

Expected: All pass.

- [ ] **Step 7: Commit**

```bash
git add core/task/ core/task.py core/task_scheduler.py core/agent/agent.py core/agent/lifecycle.py core/tests/test_task_scheduler.py
git commit -m "refactor: move task + task_scheduler into core/task/"
```

---

### Task 3: Update documentation

**Files:**
- Modify: `core/README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update `core/README.md`**

`grep -n "app_config\|character_config\|task.py\|task_scheduler" core/README.md` 定位所有引用，替换旧路径：

| 行 | 旧 | 新 |
|----|-----|-----|
| 124 | `task_scheduler.py` | `task/` 子包 |
| 125 | `character_config.py` | `config/` 子包 |
| 135 | `app_config.py` | `config/` 子包 |
| 136 | `character_config.py` | `config/` 子包 |
| 138 | `task.py` | `task/` 子包 |
| 142 | `task_scheduler.py` | `task/` 子包 |
| 147 | 无 `config/`、`task/` 条目 | 新增 `core/config/` 和 `core/task/` 子包行 |
| 245 | `agent/agent.py` task_scheduler 相关 | 更新路径 |
| 255 | `app_config.py` 行 | `config/app_config.py` |
| 256 | `character_config.py` 行 | `config/character_config.py` |
| 258 | `task.py` 行 | `task/task.py` |
| 263 | `task_scheduler.py` 行 | `task/scheduler.py` |

- [ ] **Step 2: Update `CLAUDE.md`**

`grep -n "app_config\|character_config\|task\.py\|task_scheduler" CLAUDE.md` 定位所有引用，替换旧路径：

| 行 | 旧 | 新 |
|----|-----|-----|
| 121 | 架构树 `app_config.py` | `config/app_config.py` |
| 124 | 架构树 `character_config.py` | `config/character_config.py` |
| 126 | 架构树 `task.py` | `task/task.py` |
| 142 | 模块索引表 `task.py`（Task/TaskScheduler） | 拆为 `task/` 子包条目 |
| 255 | 文件大小表 `app_config.py` | `config/app_config.py` |
| 256 | 文件大小表 `character_config.py` | `config/character_config.py` |
| 257 | 文件大小表 `task.py` | `task/task.py` |
| 263 | 文件大小表 `task_scheduler.py` | `task/scheduler.py` |

- [ ] **Step 3: Commit**

```bash
git add core/README.md CLAUDE.md
git commit -m "docs: update README, CLAUDE.md for config/ and task/ sub-packages"
```

---

## Task Summary

| Task | Files | Description |
|------|-------|-------------|
| 1 | 2 move + 18 modify | core/config/ — app_config + character_config + 42 import updates |
| 2 | 2 move + 4 modify | core/task/ — task + scheduler + 4 import updates |
| 3 | 2 modify | Update README + CLAUDE.md |
