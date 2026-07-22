# prompt/ 和 MessageBuilder 搬迁 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `message_builder.py` 搬入 `core/adapter/`，`prompt/` 搬入 `core/prompts/`，内联 `sensory.py` 的 emoji helper，消除 `Module/system_prompt/` 对 `core/` 的生产依赖。

**Architecture:** 纯搬迁 + import 路径更新，三个独立任务可任意顺序执行。零行为变更。

**Tech Stack:** Python 3.11+, Pydantic, pytest

## Global Constraints

- 纯搬迁 + import 更新，零行为变更
- 不删任何模板文件，不改变模板内容
- `Module/system_prompt/__init__.py` 保留 `MessageBuilder` 的 re-export + `DeprecationWarning`
- 所有 pytest marker 保持不变（`integration` → `integration`）
- `Module/system_prompt/` 生产代码最终零 `from core.` import

---

### Task 1: `message_builder.py` 搬入 `core/adapter/`

**Files:**
- Move: `Module/system_prompt/message_builder.py` → `core/adapter/message_builder.py`
- Move: `Module/system_prompt/tests/integration/test_message_builder.py` → `core/adapter/tests/test_message_builder.py`
- Modify: `Module/system_prompt/__init__.py`
- Modify: `core/adapter/__init__.py`
- Modify: `core/prompt_assembler.py:11`

**Interfaces:**
- Consumes: (none)
- Produces: `core.adapter.message_builder.MessageBuilder` — same class, new location. `Module.system_prompt.MessageBuilder` continues to work via re-export (deprecated).

- [ ] **Step 1: Copy source + test files to new location**

```bash
cp Module/system_prompt/message_builder.py core/adapter/message_builder.py
mkdir -p core/adapter/tests
cp Module/system_prompt/tests/integration/test_message_builder.py core/adapter/tests/test_message_builder.py
```

- [ ] **Step 2: Fix docstring in new `core/adapter/message_builder.py`**

Line 19 (`"""纯 prompt 组装...无状态，不依赖 core/ 的任何组件。"""`) → replace with:

```
    无状态，依赖 core.schema 类型（Message, Part, ToolCall, adapters）。
```

- [ ] **Step 3: Fix test import in new `core/adapter/tests/test_message_builder.py`**

Line 3: `from Module.system_prompt.message_builder import MessageBuilder` → `from core.adapter.message_builder import MessageBuilder`

- [ ] **Step 4: Fix import in `core/prompt_assembler.py`**

Line 11: `from Module.system_prompt.message_builder import MessageBuilder` → `from core.adapter.message_builder import MessageBuilder`

- [ ] **Step 5: Update `core/adapter/__init__.py`**

Add `from .message_builder import MessageBuilder` and add `"MessageBuilder"` to `__all__`. Current file:

```python
"""适配器层 — 封装 Module/ 后端，为 core/ 层提供统一接口。

LLM: LLMAdapter + tool_call 解析（重试、热切换、streaming）
TTS: TTSStreamManager（句队列、emoji 同步、门控播放）
"""

from .llm_adapter import LLMAdapter, ThoughtResult
from .tts_adapter import TTSStreamManager, is_emoji_only

__all__ = [
    "LLMAdapter",
    "ThoughtResult",
    "TTSStreamManager",
    "is_emoji_only",
]
```

Replace with:

```python
"""适配器层 — 封装 Module/ 后端，为 core/ 层提供统一接口。

LLM: LLMAdapter + tool_call 解析（重试、热切换、streaming）
TTS: TTSStreamManager（句队列、emoji 同步、门控播放）
Message: MessageBuilder（系统 prompt + 历史 → LLM API messages[]）
"""

from .llm_adapter import LLMAdapter, ThoughtResult
from .tts_adapter import TTSStreamManager, is_emoji_only
from .message_builder import MessageBuilder

__all__ = [
    "LLMAdapter",
    "ThoughtResult",
    "TTSStreamManager",
    "is_emoji_only",
    "MessageBuilder",
]
```

- [ ] **Step 6: Update `Module/system_prompt/__init__.py` — deprecated re-export**

Replace entire file (currently 13 lines):

```python
"""Prompt 模块 — Jinja2 模板渲染 + Brain Prompt 模块化引擎。"""
```

With:

```python
"""Prompt 模块 — Jinja2 模板渲染 + Brain Prompt 模块化引擎。"""

import warnings

from .manager import PromptManager
from .protocol import PromptSection, StaticSection, Jinja2Section

# MessageBuilder 已搬迁到 core/adapter/ — 此处保留 re-export 作为过渡
from core.adapter.message_builder import MessageBuilder as _MessageBuilder


def __getattr__(name: str):
    if name == "MessageBuilder":
        warnings.warn(
            "Module.system_prompt.MessageBuilder is deprecated. "
            "Use core.adapter.message_builder.MessageBuilder instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return _MessageBuilder
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "PromptManager",
    "PromptSection",
    "StaticSection",
    "Jinja2Section",
    "MessageBuilder",
]
```

- [ ] **Step 7: Delete old source + test files**

```bash
rm Module/system_prompt/message_builder.py
rm Module/system_prompt/tests/integration/test_message_builder.py
```

- [ ] **Step 8: Run tests to verify**

```bash
# 新位置的测试
python -m pytest core/adapter/tests/test_message_builder.py -v

# prompt_assembler 引用
python -c "from core.prompt_assembler import PromptAssembler; print('OK')"

# Module/system_prompt 其余测试
python -m pytest Module/system_prompt/tests/ -v

# 验证 DeprecationWarning（重新 import 需重启进程，用子进程）
python -W all -c "from Module.system_prompt import MessageBuilder; print('OK')"
```

Expected: all pass. DeprecationWarning emitted on last command.

- [ ] **Step 9: Commit**

```bash
git add core/adapter/message_builder.py \
        core/adapter/tests/test_message_builder.py \
        core/adapter/__init__.py \
        core/prompt_assembler.py \
        Module/system_prompt/__init__.py
git rm Module/system_prompt/message_builder.py \
       Module/system_prompt/tests/integration/test_message_builder.py
git commit -m "refactor: move MessageBuilder to core/adapter/

- Module/system_prompt/message_builder.py → core/adapter/message_builder.py
- test → core/adapter/tests/test_message_builder.py
- core/adapter/__init__.py 新增 MessageBuilder export
- Module/system_prompt/__init__.py 保留 DeprecationWarning re-export
- core/prompt_assembler.py import 更新"
```

---

### Task 2: `prompt/` 搬入 `core/prompts/`

**Files:**
- Move: `prompt/*.j2` → `core/prompts/*.j2`（12 个 .j2）
- Move: `prompt/ref/*` → `core/prompts/ref/*`
- Move: `prompt/README.md` → `core/prompts/README.md`
- Modify: `core/memory/consolidator.py:32,58`
- Modify: `core/memory/memory_reflector.py:31`
- Modify: `core/personality/engine.py:136,152-157` — also add `from pathlib import Path`
- Modify: `core/personality/reflector.py:42,54` — also add `from pathlib import Path`
- Modify: `core/personality/tests/test_engine.py:32-33`
- Modify: `core/personality/tests/test_reflector.py:42`

**Interfaces:**
- Consumes: (none)
- Produces: prompt 模板从 `core/prompts/` 加载，不再依赖 cwd

- [ ] **Step 1: Copy files to new location**

```bash
mkdir -p core/prompts/ref
cp prompt/*.j2 core/prompts/
cp prompt/ref/* core/prompts/ref/
cp prompt/README.md core/prompts/README.md
```

- [ ] **Step 2: Update `core/memory/consolidator.py` — 3 path references**

Lines 32, 40, 58 — change `Path("prompt/...")` to `Path(__file__).resolve().parent.parent / "prompts" / "..."`:

```python
# Line 32 — BEFORE:
EXTRACTION_PROMPT_PATH = Path("prompt/ExtractMemory.j2")

# Line 32 — AFTER:
_EXTRACTION_PATH = Path(__file__).resolve().parent.parent / "prompts" / "ExtractMemory.j2"


# Line 40 (inside _get_extraction_prompt) — BEFORE:
    if EXTRACTION_PROMPT_PATH.exists():
        _EXTRACTION_PROMPT = EXTRACTION_PROMPT_PATH.read_text(encoding="utf-8")

# Line 40 — AFTER:
    if _EXTRACTION_PATH.exists():
        _EXTRACTION_PROMPT = _EXTRACTION_PATH.read_text(encoding="utf-8")


# Line 58 (inside _get_compare_prompt) — BEFORE:
    path = Path("prompt/ConsolidationCompare.j2")

# Line 58 — AFTER:
    path = Path(__file__).resolve().parent.parent / "prompts" / "ConsolidationCompare.j2"
```

- [ ] **Step 3: Update `core/memory/memory_reflector.py` — 1 path reference**

Line 31:

```python
# BEFORE:
REFLECTOR_PROMPT_PATH = Path("prompt/MemoryReflector.j2")

# AFTER:
REFLECTOR_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "MemoryReflector.j2"
```

- [ ] **Step 4: Update `core/personality/engine.py`**

Add `from pathlib import Path` to imports (file does not currently import Path).

Line 136 — change `prompt_dir` default from `"prompt"` to `None`:

```python
# BEFORE:
        prompt_dir: str = "prompt",

# AFTER:
        prompt_dir: str | None = None,
```

Lines 151-157 — add auto-resolution when `prompt_dir` is `None`:

```python
# BEFORE:
        # L3→L2 压缩
        self._compression_lock = asyncio.Lock()
        try:
            self._pm = PromptManager(prompt_dir)
        except FileNotFoundError:
            logger.warning("CharacterEngine: 模板目录不存在，压缩功能不可用")
            self._pm = None

# AFTER:
        # L3→L2 压缩
        self._compression_lock = asyncio.Lock()
        if prompt_dir is None:
            prompt_dir = str(Path(__file__).resolve().parent.parent / "prompts")
        try:
            self._pm = PromptManager(prompt_dir)
        except FileNotFoundError:
            logger.warning("CharacterEngine: 模板目录不存在，压缩功能不可用")
            self._pm = None
```

- [ ] **Step 5: Update `core/personality/reflector.py`**

Add `from pathlib import Path` to imports (file does not currently import Path).

Line 42 — change `prompt_dir` default:

```python
# BEFORE:
        prompt_dir: str = "prompt",

# AFTER:
        prompt_dir: str | None = None,
```

Line 54 (inside `__init__`) — add auto-resolution:

```python
# BEFORE:
        try:
            self._pm = PromptManager(self._prompt_dir)

# AFTER:
        _dir = self._prompt_dir
        if _dir is None:
            _dir = str(Path(__file__).resolve().parent.parent / "prompts")
        try:
            self._pm = PromptManager(_dir)
```

- [ ] **Step 6: Update `core/personality/tests/test_engine.py` — 2 lines**

Line 32-33:

```python
# BEFORE (lines 31-33):
    # 确保 _pm 可用（tests 在项目根目录运行，prompt/ 目录存在）
    from Module.system_prompt import PromptManager
    engine._pm = PromptManager("prompt")

# AFTER:
    # 确保 _pm 可用（tests/ → personality/ → core/ → prompts/）
    from pathlib import Path
    from Module.system_prompt import PromptManager
    _PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
    engine._pm = PromptManager(str(_PROMPTS_DIR))
```

- [ ] **Step 7: Update `core/personality/tests/test_reflector.py` — 1 line**

Line 42:

```python
# BEFORE:
            prompt_dir="prompt",

# AFTER:
            prompt_dir=str(Path(__file__).resolve().parent.parent.parent / "prompts"),
```

Add `from pathlib import Path` to test_reflector.py imports if not already present.

- [ ] **Step 8: Update `core/prompts/README.md` — path examples**

Update any path loading examples from `Path("prompt/...")` to `Path(__file__).parent / "prompts" / "..."`.

- [ ] **Step 9: Run tests to verify**

```bash
# 验证路径不受 cwd 影响（项目无 editable install，需 PYTHONPATH）
PROJ=/home/yomu/Agent
cd /tmp && PYTHONPATH=$PROJ python -c "from core.memory.consolidator import _get_extraction_prompt; print('OK')"
cd /tmp && PYTHONPATH=$PROJ python -c "from core.memory.memory_reflector import _get_reflector_prompt; print('OK')"

# 全量 memory tests
python -m pytest core/memory/tests/ -v

# 全量 personality tests
python -m pytest core/personality/tests/ -v
```

Expected: all pass. /tmp test prints OK.

- [ ] **Step 10: Commit**

```bash
git add core/prompts/ \
        core/memory/consolidator.py \
        core/memory/memory_reflector.py \
        core/personality/engine.py \
        core/personality/reflector.py \
        core/personality/tests/test_engine.py \
        core/personality/tests/test_reflector.py
git commit -m "refactor: move prompt/ → core/prompts/

- 模板从项目根目录搬到 core/prompts/
- 所有路径引用从 Path('prompt/...') 改为 Path(__file__) 相对解析
- engine/reflector 的 prompt_dir 默认 None → 自动解析为 core/prompts/
- 不再依赖 cwd"
```

---

### Task 3: 内联 `sensory.py` 的 emoji helper

**Files:**
- Modify: `Module/system_prompt/sections/sensory.py:45-63`

**Interfaces:**
- Consumes: (none — removes dependency on `core.schema.mood`)
- Produces: `_stt_emotion_emoji(tag: str) -> str`, `_stt_event_emoji(event: str) -> str` — same signatures, different names (module-private)

- [ ] **Step 1: Replace try/except ImportError guards with inline implementations**

`Module/system_prompt/sections/sensory.py`, lines 45-63:

```python
# BEFORE:
    def _emotion_emoji(self) -> str:
        try:
            from core.schema.mood import stt_emotion_emoji
            return stt_emotion_emoji(self._stt_emotion) if self._stt_emotion else ""
        except ImportError:
            return ""

    def _format_events(self) -> str:
        if not self._stt_events:
            return ""
        try:
            from core.schema.mood import stt_event_emoji
            parts = []
            for evt in self._stt_events:
                emoji = stt_event_emoji(evt)
                parts.append(f"{evt} {emoji}" if emoji else evt)
            return ", ".join(parts)
        except ImportError:
            return ", ".join(self._stt_events)

# AFTER:
    def _emotion_emoji(self) -> str:
        return _stt_emotion_emoji(self._stt_emotion) if self._stt_emotion else ""

    def _format_events(self) -> str:
        if not self._stt_events:
            return ""
        parts = []
        for evt in self._stt_events:
            emoji = _stt_event_emoji(evt)
            parts.append(f"{evt} {emoji}" if emoji else evt)
        return ", ".join(parts)
```

And at module level (before `class SensorySection`, after imports), add:

```python
# STT 情绪 → emoji 映射（内联自 core.schema.mood，消除跨层依赖）
_STT_EMOTION_EMOJI: dict[str, str] = {
    "HAPPY": "\U0001f60a", "SAD": "\U0001f614", "ANGRY": "\U0001f621",
    "NEUTRAL": "", "FEARFUL": "\U0001f630", "DISGUSTED": "\U0001f922",
    "SURPRISED": "\U0001f62e",
}


def _stt_emotion_emoji(tag: str) -> str:
    return _STT_EMOTION_EMOJI.get(tag, "")


# STT 事件类型 → emoji 映射（内联自 core.schema.mood，消除跨层依赖）
_STT_EVENT_EMOJI: dict[str, str] = {
    "Speech": "", "BGM": "\U0001f3bc", "Applause": "\U0001f44f",
    "Laughter": "\U0001f600", "Cry": "\U0001f62d",
    "Cough": "\U0001f637", "Sneeze": "\U0001f927",
    "Breath": "", "Sing": "", "Speech_Noise": "", "GBG": "",
    "Event_UNK": "",
}


def _stt_event_emoji(event: str) -> str:
    return _STT_EVENT_EMOJI.get(event, "")
```

- [ ] **Step 2: Run tests**

```bash
# sensory section tests
python -m pytest Module/system_prompt/tests/ -v -k "sensory"

# 全量 system_prompt tests
python -m pytest Module/system_prompt/tests/ -v
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add Module/system_prompt/sections/sensory.py
git commit -m "refactor: inline emoji helpers in sensory.py

- 将 core.schema.mood 中的 stt_emotion_emoji/stt_event_emoji 内联到 sensory.py
- 删除 try/except ImportError 守卫
- 消除 Module/system_prompt/ 对 core 的最后一个生产依赖"
```

---

### Task 4: 文档更新

**Files:**
- Modify: `CLAUDE.md`
- Modify: `core/prompts/README.md`（copied from `prompt/README.md`）

- [ ] **Step 1: Update `CLAUDE.md`**

Section "Prompt files (`prompt/`)":

```
# BEFORE (line ~326):
## Prompt files (`prompt/`)

Jinja2 模板（`.j2`），PascalCase 命名。通过 `PromptManager.render()` 或 `render_macro()` 渲染。详见 `prompt/README.md`。

```
prompt/
├── ExtractMemory.j2          # 对话提取记忆 (consolidator.py 加载)
├── ConsolidationCompare.j2   # 新记忆 vs 已有记忆比对 (consolidator.py 加载)
...
└── ref/                      # GPT 生成的原始版本 + 需求文档（snake_case.md）
```

Prompt 需求文档：`prompt/ref/` 目录下的 `*-requirements.md` 和 `优化后的prompt-requirements.md`。

# AFTER:
## Prompt files (`core/prompts/`)

Jinja2 模板（`.j2`），PascalCase 命名。通过 `PromptManager.render()` 或 `render_macro()` 渲染。详见 `core/prompts/README.md`。模板通过 `Path(__file__)` 相对路径加载，不受 cwd 影响。

```
core/prompts/
├── ExtractMemory.j2          # 对话提取记忆 (consolidator.py 加载)
├── ConsolidationCompare.j2   # 新记忆 vs 已有记忆比对 (consolidator.py 加载)
...
└── ref/                      # GPT 生成的原始版本 + 需求文档（snake_case.md）
```

Prompt 需求文档：`core/prompts/ref/` 目录下的 `*-requirements.md` 和 `优化后的prompt-requirements.md`。
```

Also update `Module/system_prompt` description in CLAUDE.md to note MessageBuilder moved out:

```
# In the Module index table, line ~79:
| `Module.system_prompt` | System prompt engine | 12-section orchestration + templates |
```

No explicit mention of MessageBuilder needed — it was never listed there. But verify Architecture section's core/adapter/ listing includes message_builder:

```
# In Architecture core/adapter/ section:
├── adapter/               # Adapter layer: wraps Module/ backends for core/ consumption
│   ├── llm_adapter.py     #   LLMAdapter: tool-calling + retry + streaming + hot-switch
│   ├── tts_adapter.py     #   TTSStreamManager: sentence queuing + emoji sync + gated playback
│   ├── message_builder.py #   MessageBuilder: system prompt + history → LLM API messages[]
│   └── tool_call_parser.py #  merge/normalize streaming tool_call deltas
```

- [ ] **Step 2: Update `core/prompts/README.md` path examples**

Update path references from `prompt/` to `core/prompts/`. Key change: loading example from `Path("prompt/...")` → `Path(__file__).parent / "prompts" / "..."`.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md core/prompts/README.md
git commit -m "docs: update CLAUDE.md + README for prompt/ → core/prompts/ move"
```

---

### Task 5: 最终验证

- [ ] **Step 1: Verify Module/system_prompt 零 core 依赖**

```bash
grep -rn "from core\." Module/system_prompt/ --include="*.py" | grep -v "/tests/" | grep -v __pycache__
```

Expected: **empty output** (production code zero core references).

- [ ] **Step 2: Full test suite**

```bash
# 全量快速测试
python -m pytest -m "unit or service" -q --tb=short

# Module/system_prompt 全量
python -m pytest Module/system_prompt/tests/ -v

# core 全量
python -m pytest core/ -v --tb=short
```

Expected: all pass.

- [ ] **Step 3: Clean up old `prompt/` directory**

```bash
rm -rf prompt/
```

The old `prompt/` directory at project root is now empty (all files moved).

- [ ] **Step 4: Commit**

```bash
git rm -r prompt/
git commit -m "chore: remove old prompt/ directory (moved to core/prompts/)"
```

---

## Execution Order

Tasks 1-3 are independent — any order works. Task 4 depends on 1-3. Task 5 is last.
