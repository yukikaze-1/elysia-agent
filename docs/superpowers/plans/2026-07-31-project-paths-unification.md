# 项目路径统一管理 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 创建顶层 `paths.py` 集中管理所有项目路径（20 个枚举成员），迁移 ~26 处硬编码为 `ProjectPath.X.resolve()`

**Architecture:** 顶层 `paths.py`（`ProjectPath(str, Enum)` + `get_project_root()`）→ `core/` + `Module/` 消费方替换。项目根检测：`ELYSIA_ROOT` 环境变量 > `paths.py` 所在目录

**Tech Stack:** Python 3.11+ stdlib (`pathlib.Path`, `enum.Enum`, `os.getenv`)

**Spec:** [2026-07-31-project-paths-unification-design.md](../specs/2026-07-31-project-paths-unification-design.md)

## Global Constraints

- 所有路径值语义不变 — 只改获取方式，不改目录结构
- `resolve()` 返回 `pathlib.Path`，需 `str()` 包裹才能当字符串用
- `make test-quick` 全绿（每个 Task 结束后验证）
- 不改测试文件 — 本 plan 只改生产代码
- 使用 conda 环境: `conda activate Elysia`

---

### Task 1: 创建 `paths.py` + 单元测试

**Files:**
- Create: `paths.py`
- Create: `tests/test_paths.py`

**Interfaces:**
- Produces: `ProjectPath(str, Enum)` — 20 个成员，`resolve(**kwargs) -> Path`
- Produces: `get_project_root() -> Path`
- Produces: `_detect_project_root() -> Path`（内部函数）

- [ ] **Step 1: 创建 `paths.py`**

```python
"""项目路径统一管理。

所有项目级文件系统路径的唯一定义点。
项目根目录: ELYSIA_ROOT 环境变量 > paths.py 所在目录。

Usage:
    from paths import ProjectPath, get_project_root

    log_dir = ProjectPath.LOG.resolve()        # -> /path/to/project/log
    prompts = ProjectPath.PROMPTS.resolve()    # -> /path/to/project/core/prompts
    sandbox = ProjectPath.DATA_SANDBOX.resolve(session_id="abc")
    root = get_project_root()                  # -> /path/to/project
"""

import os
from enum import Enum
from pathlib import Path


def _detect_project_root() -> Path:
    """ELYSIA_ROOT 环境变量优先，fallback 到 paths.py 所在目录。"""
    if env_root := os.getenv("ELYSIA_ROOT"):
        return Path(env_root).resolve()
    return Path(__file__).resolve().parent


_project_root = _detect_project_root()


class ProjectPath(str, Enum):
    """项目路径枚举。resolve() 返回基于项目根的绝对 Path。

    每个成员可用 ELYSIA_PATH_{NAME} 环境变量覆盖。
    """

    # -- 目录 --
    DATA = "data"
    DATA_UPLOADS = "data/uploads"
    DATA_UPLOADS_IMAGES = "data/uploads/images"
    DATA_SANDBOX = "data/sandbox/{session_id}"
    DATA_CACHE_VISION = "data/cache/vision"
    DATA_REF = "data/ref"
    LOG = "log"
    STATIC = "static"
    LIVE2D_ASSETS = "data/live2d-assets"
    PROMPTS = "core/prompts"
    SYSTEM_PROMPT_TEMPLATES = "Module/system_prompt/templates"
    CHARACTERS = "character"
    MODEL_CACHE = ".model_cache"

    # -- 文件 --
    CONFIG_YAML = "config.yaml"
    DOTENV = ".env"
    CUSTOM_PROVIDERS_YAML = "custom_providers.yaml"
    TOKEN_PENDING = "data/token_usage_pending.jsonl"
    SESSION_PENDING = "data/session_{session_id}.json"
    SESSION_PENDING_CHECK = "data/session_pending.jsonl"
    SCHEDULER_CHECKPOINT = "data/scheduler_checkpoint.json"

    def resolve(self, **kwargs) -> Path:
        """返回绝对路径。

        Args:
            **kwargs: 填充 value 中的 {placeholder}

        Returns:
            基于 _project_root 的绝对 Path

        Note:
            不检查路径存在性 — 纯计算，不引入 I/O
        """
        value = self.value.format(**kwargs) if kwargs else self.value
        override = os.getenv(f"ELYSIA_PATH_{self.name}")
        if override:
            value = override
        return _project_root / value


def get_project_root() -> Path:
    """获取项目根目录（只读）。"""
    return _project_root
```

- [ ] **Step 2: 创建 `tests/test_paths.py`**

```python
"""测试 paths.py — 项目路径统一管理。"""

import os
import pytest
from pathlib import Path

# 注入项目根目录到 sys.path（测试文件可能被 pytest 从不同目录运行）
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paths import ProjectPath, get_project_root, _detect_project_root


class TestDetectProjectRoot:
    """项目根目录检测。"""

    def test_env_var_takes_priority(self, monkeypatch):
        """ELYSIA_ROOT 环境变量优先。"""
        monkeypatch.setenv("ELYSIA_ROOT", "/custom/project")
        assert _detect_project_root() == Path("/custom/project").resolve()

    def test_fallback_to_paths_py_location(self, monkeypatch):
        """无环境变量时 fallback 到 paths.py 所在目录。"""
        monkeypatch.delenv("ELYSIA_ROOT", raising=False)
        root = _detect_project_root()
        assert (root / "paths.py").exists()
        assert (root / "config.yaml").exists()


class TestProjectPathResolve:
    """resolve() 方法。"""

    def test_resolve_directory(self):
        """基础目录解析。"""
        result = ProjectPath.LOG.resolve()
        assert isinstance(result, Path)
        assert result.is_absolute()
        assert result.name == "log"

    def test_resolve_file(self):
        """基础文件解析。"""
        result = ProjectPath.CONFIG_YAML.resolve()
        assert result.name == "config.yaml"

    def test_resolve_with_placeholder(self):
        """占位符填充。"""
        result = ProjectPath.SESSION_PENDING.resolve(session_id="test123")
        assert result.name == "session_test123.json"
        assert str(result).endswith("data/session_test123.json")

    def test_resolve_placeholder_in_middle(self):
        """占位符在路径中间。"""
        result = ProjectPath.DATA_SANDBOX.resolve(session_id="abc")
        assert str(result).endswith("data/sandbox/abc")

    def test_resolve_without_placeholder(self):
        """无占位符时不需要 kwargs。"""
        result = ProjectPath.TOKEN_PENDING.resolve()
        assert result.name == "token_usage_pending.jsonl"

    def test_resolve_returns_path(self):
        """返回值是 pathlib.Path。"""
        result = ProjectPath.STATIC.resolve()
        assert isinstance(result, Path)

    def test_env_override(self, monkeypatch):
        """ELYSIA_PATH_{NAME} 环境变量覆盖。"""
        monkeypatch.setenv("ELYSIA_PATH_LOG", "/var/log/elysia")
        result = ProjectPath.LOG.resolve()
        assert result == Path("/var/log/elysia")

    def test_env_override_not_set_no_effect(self, monkeypatch):
        """未设环境变量时行为不变。"""
        monkeypatch.delenv("ELYSIA_PATH_LOG", raising=False)
        result = ProjectPath.LOG.resolve()
        root = get_project_root()
        assert result == root / "log"


class TestGetProjectRoot:
    """get_project_root() 函数。"""

    def test_returns_path(self):
        """返回 pathlib.Path。"""
        root = get_project_root()
        assert isinstance(root, Path)
        assert root.is_absolute()
        assert (root / "config.yaml").exists()


class TestEnumMetadata:
    """枚举元数据。"""

    def test_is_string_enum(self):
        """ProjectPath 是 str 子类，可直接作为字符串使用。"""
        assert isinstance(ProjectPath.LOG, str)
        assert ProjectPath.LOG == "log"

    def test_count(self):
        """成员数量正确 — 13 目录 + 7 文件 = 20。"""
        members = list(ProjectPath)
        assert len(members) == 20

    def test_all_members_have_values(self):
        """所有成员都有非空值。"""
        for member in ProjectPath:
            assert member.value, f"{member.name} 的 value 为空"
```

- [ ] **Step 3: 运行 paths.py 测试验证通过**

```bash
python -m pytest tests/test_paths.py -v
```
Expected: 14 passed

- [ ] **Step 4: 运行全量 test-quick 确保新文件无副作用**

```bash
make test-quick
```
Expected: 全绿（paths.py 尚未被其他模块 import，零影响）

- [ ] **Step 5: Commit**

```bash
git add paths.py tests/test_paths.py
git commit -m "feat: 创建 paths.py — 项目路径统一管理

ProjectPath(str, Enum) 集中定义 20 个项目路径。
项目根检测: ELYSIA_ROOT 环境变量 > paths.py 所在目录。
resolve() 支持 {placeholder} .format() 和 ELYSIA_PATH_{NAME} 覆盖。"
```

---

### Task 2: 迁移组 3 — SYSTEM_PROMPT_TEMPLATES 收敛（4 处）

**Files:**
- Modify: `core/adapter/prompt_assembler.py:36`
- Modify: `core/personality/compression.py:268`
- Modify: `core/input/debug_routes/recording.py:55`
- Modify: `Module/system_prompt/__main__.py:19`

**Interfaces:**
- Consumes: `ProjectPath.SYSTEM_PROMPT_TEMPLATES.resolve()` from Task 1

- [ ] **Step 1: 修改 `core/adapter/prompt_assembler.py:36`**

```python
# OLD:
_SECTION = "Module/system_prompt/templates"

# NEW:
from paths import ProjectPath
_SECTION = str(ProjectPath.SYSTEM_PROMPT_TEMPLATES.resolve())
```

- [ ] **Step 2: 修改 `core/personality/compression.py:268`**

```python
# OLD:
sec = SpeakingStyleSection("Module/system_prompt/templates/c6_speaking_style.j2")

# NEW:
from paths import ProjectPath
sec = SpeakingStyleSection(str(ProjectPath.SYSTEM_PROMPT_TEMPLATES.resolve() / "c6_speaking_style.j2"))
```

- [ ] **Step 3: 修改 `core/input/debug_routes/recording.py:55`**

```python
# OLD:
templates_dir = "Module/system_prompt/templates"

# NEW:
from paths import ProjectPath
templates_dir = str(ProjectPath.SYSTEM_PROMPT_TEMPLATES.resolve())
```

- [ ] **Step 4: 修改 `Module/system_prompt/__main__.py:19`**

```python
# OLD:
_SECTION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

# NEW:
from paths import ProjectPath
_SECTION_DIR = str(ProjectPath.SYSTEM_PROMPT_TEMPLATES.resolve())
```

- [ ] **Step 5: 运行 test-quick 验证**

```bash
make test-quick
```
Expected: 全绿

- [ ] **Step 6: Commit**

```bash
git add core/adapter/prompt_assembler.py core/personality/compression.py core/input/debug_routes/recording.py Module/system_prompt/__main__.py
git commit -m "refactor: SYSTEM_PROMPT_TEMPLATES 4 处 → ProjectPath 集中引用"
```

---

### Task 3: 迁移组 4 — PROMPTS 模板路径收敛（5 处）

**Files:**
- Modify: `core/memory/consolidator.py:32`
- Modify: `core/memory/consolidator.py:58`
- Modify: `core/memory/memory_reflector.py:31`
- Modify: `core/personality/engine.py:107`
- Modify: `core/personality/reflector.py:66`

**Interfaces:**
- Consumes: `ProjectPath.PROMPTS.resolve()` from Task 1

- [ ] **Step 1: 修改 `core/memory/consolidator.py:32`**

```python
# OLD:
_EXTRACTION_PATH = Path(__file__).resolve().parent.parent / "prompts" / "ExtractMemory.j2"

# NEW:
from paths import ProjectPath
_EXTRACTION_PATH = ProjectPath.PROMPTS.resolve() / "ExtractMemory.j2"
```

- [ ] **Step 2: 修改 `core/memory/consolidator.py:58`**

```python
# OLD:
path = Path(__file__).resolve().parent.parent / "prompts" / "ConsolidationCompare.j2"

# NEW:
path = ProjectPath.PROMPTS.resolve() / "ConsolidationCompare.j2"
```

- [ ] **Step 3: 修改 `core/memory/memory_reflector.py:31`**

```python
# OLD:
REFLECTOR_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "MemoryReflector.j2"

# NEW:
from paths import ProjectPath
REFLECTOR_PROMPT_PATH = ProjectPath.PROMPTS.resolve() / "MemoryReflector.j2"
```

- [ ] **Step 4: 修改 `core/personality/engine.py:107`**

```python
# OLD:
prompt_dir = str(Path(__file__).resolve().parent.parent / "prompts")

# NEW:
from paths import ProjectPath
prompt_dir = str(ProjectPath.PROMPTS.resolve())
```

- [ ] **Step 5: 修改 `core/personality/reflector.py:66`**

```python
# OLD:
_dir = str(Path(__file__).resolve().parent.parent / "prompts")

# NEW:
from paths import ProjectPath
_dir = str(ProjectPath.PROMPTS.resolve())
```

- [ ] **Step 6: 运行 test-quick 验证**

```bash
make test-quick
```
Expected: 全绿

- [ ] **Step 7: Commit**

```bash
git add core/memory/consolidator.py core/memory/memory_reflector.py core/personality/engine.py core/personality/reflector.py
git commit -m "refactor: PROMPTS 5 处 → ProjectPath.PROMPTS 集中引用"
```

---

### Task 4: 迁移组 1 — 删除 `__file__`-relative 重复推导（6 处）

**Files:**
- Modify: `core/input/server.py:25-26`
- Modify: `core/input/server.py:249`
- Modify: `core/input/api_routes.py:189`
- Modify: `core/schema/adapters/message_adapter.py:103-104`

**Interfaces:**
- Consumes: `ProjectPath.STATIC`, `ProjectPath.LIVE2D_ASSETS`, `ProjectPath.DATA_UPLOADS`, `ProjectPath.DATA_UPLOADS_IMAGES` from Task 1

- [ ] **Step 1: 修改 `core/input/server.py:25-26`**

```python
# OLD:
_STATIC_DIR = Path(__file__).parent.parent.parent / "static"
_LIVE2D_DIR = Path(__file__).parent.parent.parent / "data" / "live2d-assets"

# NEW:
from paths import ProjectPath
_STATIC_DIR = ProjectPath.STATIC.resolve()
_LIVE2D_DIR = ProjectPath.LIVE2D_ASSETS.resolve()
```

- [ ] **Step 2: 修改 `core/input/server.py:249`**

```python
# OLD:
    _UPLOADS_DIR = Path(__file__).parent.parent.parent / "data" / "uploads"

# NEW:
    _UPLOADS_DIR = ProjectPath.DATA_UPLOADS.resolve()
```

- [ ] **Step 3: 修改 `core/input/api_routes.py:189`**

```python
# OLD:
    _IMAGES_DIR = Path(__file__).parent.parent.parent / "data" / "uploads" / "images"

# NEW:
    from paths import ProjectPath
    _IMAGES_DIR = ProjectPath.DATA_UPLOADS_IMAGES.resolve()
```

- [ ] **Step 4: 修改 `core/schema/adapters/message_adapter.py:103-104`**

```python
# OLD:
_IMAGES_DIR = _Path(__file__).resolve().parent.parent.parent.parent / "data" / "uploads" / "images"
_STATIC_DIR = _Path(__file__).resolve().parent.parent.parent.parent / "static"

# NEW:
from paths import ProjectPath as _ProjectPath
_IMAGES_DIR = _ProjectPath.DATA_UPLOADS_IMAGES.resolve()
_STATIC_DIR = _ProjectPath.STATIC.resolve()
```

- [ ] **Step 5: 运行 test-quick 验证**

```bash
make test-quick
```
Expected: 全绿（特别注意 server 启动相关测试）

- [ ] **Step 6: Commit**

```bash
git add core/input/server.py core/input/api_routes.py core/schema/adapters/message_adapter.py
git commit -m "refactor: STATIC/LIVE2D/UPLOADS 6 处 __file__-relative → ProjectPath"
```

---

### Task 5: 迁移组 2 — 消除 cwd-relative 路径（7 处）

**Files:**
- Modify: `core/utils/log_setup.py:16`
- Modify: `core/memory/session.py:80`
- Modify: `core/monitoring/token_tracker.py:47`
- Modify: `core/debug/info.py:352,356`
- Modify: `core/input/custom_providers.py:10`
- Modify: `core/config/character_config.py:117`

**Interfaces:**
- Consumes: `ProjectPath.LOG`, `ProjectPath.SESSION_PENDING`, `ProjectPath.TOKEN_PENDING`, `ProjectPath.SESSION_PENDING_CHECK`, `ProjectPath.CUSTOM_PROVIDERS_YAML`, `ProjectPath.CHARACTERS` from Task 1

- [ ] **Step 1: 修改 `core/utils/log_setup.py:16`**

```python
# OLD:
LOG_DIR = Path("log")

# NEW:
from paths import ProjectPath
LOG_DIR = ProjectPath.LOG.resolve()
```

- [ ] **Step 2: 修改 `core/memory/session.py:80`**

```python
# OLD:
self._pending_file = os.path.join("data", f"session_{session_id}.json")

# NEW:
from paths import ProjectPath
self._pending_file = str(ProjectPath.SESSION_PENDING.resolve(session_id=session_id))
```

- [ ] **Step 3: 修改 `core/monitoring/token_tracker.py:47`**

```python
# OLD:
    _pending_file = os.path.join("data", "token_usage_pending.jsonl")

# NEW:
    from paths import ProjectPath
    _pending_file = str(ProjectPath.TOKEN_PENDING.resolve())
```

- [ ] **Step 4: 修改 `core/debug/info.py:352,356`**

```python
# OLD (line 352-356):
        token_pending = "data/token_usage_pending.jsonl"
        if os.path.exists(token_pending):
            with open(token_pending) as f:
                info.pending_token_count = sum(1 for _ in f)
        info.pending_session_exists = os.path.exists("data/session_pending.jsonl")

# NEW:
        from paths import ProjectPath
        token_pending = str(ProjectPath.TOKEN_PENDING.resolve())
        if os.path.exists(token_pending):
            with open(token_pending) as f:
                info.pending_token_count = sum(1 for _ in f)
        info.pending_session_exists = os.path.exists(
            str(ProjectPath.SESSION_PENDING_CHECK.resolve()))
```

- [ ] **Step 5: 修改 `core/input/custom_providers.py:10`**

```python
# OLD:
_DEFAULT_PATH = Path("custom_providers.yaml")

# NEW:
from paths import ProjectPath
_DEFAULT_PATH = ProjectPath.CUSTOM_PROVIDERS_YAML.resolve()
```

- [ ] **Step 6: 修改 `core/config/character_config.py:117`**

```python
# OLD:
            candidates.append(Path(".") / base_file)                 # Step 2: 项目根目录

# NEW:
            from paths import get_project_root
            candidates.append(get_project_root() / base_file)        # Step 2: 项目根目录
```
注：此 fallback 搜索的是项目根目录，不是 `character/` 子目录。用 `get_project_root()` 替代 `Path(".")` 消除 cwd 依赖，保持语义不变。

- [ ] **Step 7: 运行 test-quick 验证**

```bash
make test-quick
```
Expected: 全绿（特别注意 session/token 持久化相关测试）

- [ ] **Step 8: Commit**

```bash
git add core/utils/log_setup.py core/memory/session.py core/monitoring/token_tracker.py core/debug/info.py core/input/custom_providers.py core/config/character_config.py
git commit -m "refactor: 消除 7 处 cwd-relative 路径 → ProjectPath"
```

---

### Task 6: 迁移组 5 — 工具/基础设施/预检路径（5 处）

**Files:**
- Modify: `core/tools/vision/plugin.py:22`
- Modify: `core/tools/filesystem/plugin.py:221`
- Modify: `Module/config/preflight.py:228-229`
- Modify: `Module/config/preflight.py:242`
- Modify: `Module/_common/env.py:17`

**Interfaces:**
- Consumes: `ProjectPath.DATA_CACHE_VISION`, `ProjectPath.DATA_SANDBOX`, `ProjectPath.MODEL_CACHE`, `ProjectPath.LOG`, `ProjectPath.DATA`, `ProjectPath.DOTENV` from Task 1

- [ ] **Step 1: 修改 `core/tools/vision/plugin.py:22`**

```python
# OLD:
_CACHE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "cache" / "vision"

# NEW:
from paths import ProjectPath
_CACHE_DIR = ProjectPath.DATA_CACHE_VISION.resolve()
```

- [ ] **Step 2: 修改 `core/tools/filesystem/plugin.py:221`**

```python
# OLD:
        write_dir="data/sandbox/{session_id}",

# NEW:
        from paths import ProjectPath
        write_dir=ProjectPath.DATA_SANDBOX,
# ProjectPath(str, Enum) 本身就是字符串，值 "data/sandbox/{session_id}"
# 保持模板语义 — {session_id} 由调用方后续 .format() 填充
```

- [ ] **Step 3: 修改 `Module/config/preflight.py:228-229`**

文件顶部添加 import（Step 3 + Step 4 共用一次）:
```python
from paths import ProjectPath
```

函数体内替换:
```python
# OLD:
        Path(".model_cache/models--BAAI--bge-large-zh-v1.5/snapshots"),
        Path(".model_cache/BAAI/bge-large-zh-v1.5"),

# NEW:
        ProjectPath.MODEL_CACHE.resolve() / "models--BAAI--bge-large-zh-v1.5" / "snapshots",
        ProjectPath.MODEL_CACHE.resolve() / "BAAI" / "bge-large-zh-v1.5",
```

- [ ] **Step 4: 修改 `Module/config/preflight.py:242`**

（import 已在 Step 3 添加，无需重复）
```python
# OLD:
    for d in ("log", "data"):

# NEW:
    for d in (str(ProjectPath.LOG.resolve()), str(ProjectPath.DATA.resolve())):
```

- [ ] **Step 5: 修改 `Module/_common/env.py:17`**

```python
# OLD:
    project_root = Path(__file__).resolve().parent.parent.parent
    env_path = project_root / ".env"

# NEW:
    from paths import ProjectPath, get_project_root
    project_root = get_project_root()
    env_path = ProjectPath.DOTENV.resolve()
```

注意：`env.py` 原有注释中应保留 `project_root` 获取方式说明。

- [ ] **Step 6: 运行 test-quick 验证**

```bash
make test-quick
```
Expected: 全绿（特别注意 preflight 和 env 加载测试）

- [ ] **Step 7: Commit**

```bash
git add core/tools/vision/plugin.py core/tools/filesystem/plugin.py Module/config/preflight.py Module/_common/env.py
git commit -m "refactor: 工具/预检/env 5 处 → ProjectPath 集中引用"
```

---

### Task 7: 最终验证 + 清理

**Files:** (验证脚本输出，不修改文件)

- [ ] **Step 1: 确认无遗漏的 `__file__`-relative 路径推导**

```bash
grep -rn "\.parent\.parent" --include="*.py" core/ Module/ | grep -v test_ | grep -v __pycache__ | grep -v paths.py
```
Expected: 输出为空（除保留不改的 `Module/tool/safety.py:58`）

- [ ] **Step 2: 确认无遗漏的 cwd-relative 路径**

```bash
grep -rn 'os\.path\.join("data"' --include="*.py" core/ Module/ | grep -v test_ | grep -v __pycache__
grep -rn 'Path("log")' --include="*.py" core/ Module/ | grep -v test_
grep -rn 'Path("custom_providers")' --include="*.py" core/ Module/ | grep -v test_
```
Expected: 输出为空

- [ ] **Step 3: 确认 SYSTEM_PROMPT_TEMPLATES 无残留硬编码**

```bash
grep -rn '"Module/system_prompt/templates"' --include="*.py" core/ Module/ | grep -v __pycache__
```
Expected: 输出为空

- [ ] **Step 4: 全量 test-quick**

```bash
make test-quick
```
Expected: 全绿

- [ ] **Step 5: 更新 CLAUDE.md 中 paths.py 相关文档**

在 `CLAUDE.md` 的 `Module index` 表下方新增一行：
```markdown
| `paths` | 项目路径统一管理 | 20 个 ProjectPath 枚举成员 |
```

在 `Key patterns` 节末尾追加：
```markdown
- **Project paths**: `from paths import ProjectPath` — 所有项目文件系统路径的唯一定义点。`ProjectPath.X.resolve()` 返回基于 `ELYSIA_ROOT`（或 fallback）的绝对 `Path`
```

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: 更新 CLAUDE.md — 添加 paths.py 说明"
```
