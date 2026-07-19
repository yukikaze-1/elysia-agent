# Split CognitiveLoop Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) to implement this plan task-by-task.

**Goal:** Split `core/cognitive_loop.py` (1029 lines) + merge `core/tts_stream.py` (308 lines) into `core/cognitive/` package with 4 files.

**Architecture:** `CognitiveLoop(CognitiveTTS, CognitiveTools)` — two mixins inherit into the main class. `tts_stream.py` merged into `tts.py` to consolidate TTS code.

**Tech Stack:** Python 3.11+, asyncio, Pydantic

## Global Constraints

1. **外部 import 路径变更**: `from core.cognitive_loop import CognitiveLoop` → `from core.cognitive import CognitiveLoop`
2. **Mixin 只放方法**: `CognitiveTTS` 和 `CognitiveTools` 不设 `__init__`
3. **不改变逻辑**: 纯文件拆分+合并，不重构内部实现
4. **`_default_user_profile` 放在 `tools.py`**: 防止循环导入（`loop.py` → `tools.py` → `loop.py`）
5. **删除旧文件**: `core/cognitive_loop.py` + `core/tts_stream.py`

---

### Task 1: 备份 + 创建 `tts.py`

**Files:**
- Rename: `core/cognitive_loop.py` → `core/_cognitive_loop_backup.py`
- Rename: `core/tts_stream.py` → `core/_tts_stream_backup.py`
- Create: `core/cognitive/tts.py`

**Interfaces:**
- Produces: `CognitiveTTS` mixin, `TTSStreamManager`, `is_emoji_only` — 被 Task 3 的 `loop.py` 使用

- [ ] **Step 1: 备份两个源文件**

```bash
mv core/cognitive_loop.py core/_cognitive_loop_backup.py
mv core/tts_stream.py core/_tts_stream_backup.py
mkdir -p core/cognitive
```

- [ ] **Step 2: 创建 `core/cognitive/tts.py`**

合并两部分内容：

**A. 从 `_tts_stream_backup.py` 完整复制** (lines 1–308)：emoji 检测 + TTSStreamManager

**B. 从 `_cognitive_loop_backup.py` 提取** lines 786–814：deprecated TTS wrapper + `CognitiveTTS` mixin

```python
"""TTS 全模块 — TTSStreamManager + emoji utils + deprecated wrappers。"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING

from Module.output import ChannelCapability, OutputManager
from Module.tts import TTSBackend

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ================================================================
# Emoji 检测 — 从 core/tts_stream.py 复制（lines 29–55）
# ================================================================
# [复制 _EMOJI_RE, is_emoji_only()]

# ================================================================
# TTSStreamManager — 从 core/tts_stream.py 复制（lines 58–308）
# ================================================================
# [复制 TTSStreamManager 完整类定义]

# ================================================================
# CognitiveTTS mixin — 从 _cognitive_loop_backup.py 提取（lines 786–814）
# ================================================================

class CognitiveTTS:
    """TTS mixin — deprecated wrappers，供 test_phase3.py 兼容。

    所有属性由 CognitiveLoop.__init__ 初始化，本 mixin 不含 __init__。
    """

    def _start_tts_stream(self, sentence: str) -> None:
        """[Deprecated] 代理到 TTSStreamManager.stream_sentence()。"""
        self._tts_mgr.stream_sentence(sentence)
        if self._tts_mgr._tasks:
            self._tts_task = self._tts_mgr._tasks[-1]

    async def _synthesize_sentence(self, sentence: str) -> None:
        """[Deprecated] Legacy non-streaming TTS — 直接使用 self._tts。"""
        if not self._tts:
            return
        try:
            char_name = self._prompt._character.name
            audio = await self._tts.synthesize(sentence, character=char_name)
            await self._output.audio_chunk(audio)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("TTS synthesis failed", exc_info=True)
```

- [ ] **Step 3: 验证 — 导入不报错**

```bash
python3 -c "from core.cognitive.tts import CognitiveTTS, TTSStreamManager, is_emoji_only; print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add core/_cognitive_loop_backup.py core/_tts_stream_backup.py core/cognitive/tts.py
git commit -m "refactor: 备份旧文件，创建 cognitive/tts.py（TTSStreamManager + CognitiveTTS）"
```

---

### Task 2: 创建 `tools.py` — CognitiveTools mixin

**Files:**
- Create: `core/cognitive/tools.py`

**Interfaces:**
- Consumes: (无包内依赖)
- Produces: `CognitiveTools` mixin — 被 Task 3 的 `loop.py` 使用

- [ ] **Step 1: 创建 `core/cognitive/tools.py`**

从 `core/_cognitive_loop_backup.py` 提取以下行范围：

| 内容 | 行范围 |
|------|--------|
| `_default_user_profile()` | 58–62 |
| `# ---- Tool execution ----` + `_execute_tools_directly` + 内嵌 `_execute_one` | 815–850 |
| `_build_wake_up_message` | — *(保留在 loop.py, 被 _think_loop 调用)* |
| `_submit_task_definition` | 870–904 |
| `_handle_submit_task` | 907–984 |

```python
"""Cognitive 工具执行 — _execute_tools_directly + _handle_submit_task。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from Module.tool.protocol import ToolResult, ErrorType
from ..schema.tool import ToolCall
from ..message_id import new_message_id

if TYPE_CHECKING:
    from Module.task_system.schema import SubmitError

logger = logging.getLogger(__name__)


def _default_user_profile():
    """Return a default UserProfile for task submission."""
    from Module.task_system.schema import UserProfile
    return UserProfile(display_name="User")


class CognitiveTools:
    """工具执行 mixin — 工具直接执行 + submit_task 后台任务。

    所有属性由 CognitiveLoop.__init__ 初始化，本 mixin 不含 __init__。
    """

    # ================================================================
    # _execute_tools_directly（含内嵌 _execute_one）
    # [复制 _cognitive_loop_backup.py lines 815–850]
    # ================================================================

    # ================================================================
    # _submit_task_definition
    # [复制 _cognitive_loop_backup.py lines 870–904]
    # ================================================================

    # ================================================================
    # _handle_submit_task
    # [复制 _cognitive_loop_backup.py lines 907–984]
    # 内部调用 _default_user_profile()（同文件，无循环导入）
    # ================================================================
```

- [ ] **Step 2: 验证 — 导入不报错**

```bash
python3 -c "from core.cognitive.tools import CognitiveTools, _default_user_profile; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add core/cognitive/tools.py
git commit -m "refactor: 创建 cognitive/tools.py — CognitiveTools mixin（工具执行 + submit_task）"
```

---

### Task 3: 创建 `loop.py` + `__init__.py` + 更新引用 + 清理

**Files:**
- Create: `core/cognitive/loop.py`
- Create: `core/cognitive/__init__.py`
- Modify: `core/agent/agent.py`, `core/agent/lifecycle.py`
- Modify: `core/tests/test_phase3.py`, `core/tests/test_task_scheduler.py`
- Delete: `core/_cognitive_loop_backup.py`, `core/_tts_stream_backup.py`

**Interfaces:**
- Consumes: `CognitiveTTS` + `TTSStreamManager` + `is_emoji_only` (Task 1), `CognitiveTools` (Task 2)
- Produces: `CognitiveLoop` class

- [ ] **Step 1: 创建 `core/cognitive/loop.py`**

从 `core/_cognitive_loop_backup.py` 保留以下内容并添加 mixin 导入：

**保留的行范围：**

| 内容 | 行范围 |
|------|--------|
| module docstring + imports | 1–57 |
| `class CognitiveLoop:` + `__init__` | 64–117 |
| `cancel_current` | 119–126 |
| `set_tool_context` | 128–130 |
| `set_tts` | 132–135 |
| `set_token_tracker` | 137–139 |
| `set_prompt_recorder` | 141–143 |
| `set_response_recorder` | 145–147 |
| `set_task_system` | 149–151 |
| `set_delegation_preparer` | 153–155 |
| `set_on_background_task` | 157–159 |
| `set_user_profile` | 161–163 |
| `set_memory_reflector` | 165–169 |
| `process()` | 171–258 |
| `_think_loop()` | 260–747 |
| `_after_response()` | 749–785 |
| `_build_wake_up_message` | 852–868 |
| `_transition_idle` | 986–1011 |
| `_handle_interrupt` | 1013–1029 |

**类定义改为：**

```python
"""CognitiveLoop — the core ReAct scheduler. ..."""
from __future__ import annotations

# [保留原来的所有 imports，但替换混合导入:]
from .tts import CognitiveTTS, TTSStreamManager, is_emoji_only
from .tools import CognitiveTools

# [删除原来的:]
# from ..tts_stream import is_emoji_only, TTSStreamManager

# [保留其他 imports...]
# [保留 _MOOD_TO_TTS_EMOTION, _CHAR_LIMIT 等常量...]
# [保留 logger...]

class CognitiveLoop(CognitiveTTS, CognitiveTools):
    """Orchestrates THINKING → (EXECUTING → THINKING)* → SPEAKING → IDLE."""
    # [保留 __init__ 和所有保留方法...]
```

> **关键**: 删除原来的 `_default_user_profile` (lines 58–62)、TTS deprecated wrappers (lines 786–814)、`_execute_tools_directly` (lines 815–850)、`_submit_task_definition` (lines 870–904)、`_handle_submit_task` (lines 907–984)——这些已提取到 tools.py 和 tts.py。

- [ ] **Step 2: 创建 `core/cognitive/__init__.py`**

```python
"""Cognitive 包 — 对外透出 CognitiveLoop。"""
from .loop import CognitiveLoop
```

- [ ] **Step 3: 更新外部引用**

```bash
# agent.py
# 将 from ..cognitive_loop import CognitiveLoop
# 改为 from ..cognitive import CognitiveLoop

# lifecycle.py
# 将 from ..cognitive_loop import CognitiveLoop
# 改为 from ..cognitive import CognitiveLoop

# test_phase3.py
# 将所有 from core.cognitive_loop import CognitiveLoop
# 改为 from core.cognitive import CognitiveLoop

# test_task_scheduler.py
# 将所有 from core.cognitive_loop import CognitiveLoop
# 改为 from core.cognitive import CognitiveLoop
```

每个文件的 import 行都需要精确替换：

**`core/agent/agent.py:41`**:
```python
# 旧:
from ..cognitive_loop import CognitiveLoop
# 新:
from ..cognitive import CognitiveLoop
```

**`core/agent/lifecycle.py:23`**:
```python
# 旧:
from ..cognitive_loop import CognitiveLoop
# 新:
from ..cognitive import CognitiveLoop
```

**`core/tests/test_phase3.py:16`** 及所有函数内 import:
```python
# 旧:
from core.cognitive_loop import CognitiveLoop
# 新:
from core.cognitive import CognitiveLoop
```

**`core/tests/test_task_scheduler.py`** 对应行:
```python
# 旧:
from core.cognitive_loop import CognitiveLoop
# 新:
from core.cognitive import CognitiveLoop
```

- [ ] **Step 4: 运行测试验证**

```bash
python3 -m pytest -m "unit or service" -q -x --tb=short
```

Expected: 239 passed, 2 skipped, 1 error (pre-existing test_server).

- [ ] **Step 5: 删除备份文件**

```bash
git rm core/_cognitive_loop_backup.py core/_tts_stream_backup.py
```

- [ ] **Step 6: Commit**

```bash
git add core/cognitive/loop.py core/cognitive/__init__.py \
        core/agent/agent.py core/agent/lifecycle.py \
        core/tests/test_phase3.py core/tests/test_task_scheduler.py
git commit -m "refactor: 完成 cognitive 包拆分 — loop + tts + tools"
```

---

## Execution Order

Tasks MUST run sequentially (Task 1 → 2 → 3). Tasks 1 and 2 are independent of each other (both only depend on backup files), but sequential commits keep git history clean.
