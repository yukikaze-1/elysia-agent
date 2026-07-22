# prompt/ 和 MessageBuilder 搬迁 spec

## 背景

之前架构梳理发现两个组织问题：

1. **`Module/system_prompt/message_builder.py` 违反 Module 层约定** — Module 层应是 backend-agnostic 的，但 `message_builder.py` 直接 import `core.schema` 的类型（`Message`, `ImagePart`, `TextPart`, `ToolCall`, `content_to_openai` 等）。其唯一调用者是 `core/prompt_assembler.py`，本质是 core 层的适配器。
2. **`prompt/` 放在项目根目录用相对路径加载** — `Path("prompt/ExtractMemory.j2")` 依赖 cwd 是项目根目录，换个工作目录启动就炸。这些模板只被 `core/` 消费，应放在 `core/` 下面。
3. **附带清理** — `Module/system_prompt/sections/sensory.py` 中 `try/except ImportError` 从 `core.schema.mood` 引入两个 emoji 映射函数（~15 行 dict lookup），应内联消除依赖。

## 目标

- `Module/system_prompt/` 零依赖 `core/`（生产代码）
- `prompt/` 内的模板用 `Path(__file__)` 相对路径加载，不受 cwd 影响
- 所有现有测试通过，无行为变更

## 非目标

- 不改变 `Module/system_prompt/` 其他部分的内部实现
- 不改变 `prompt/` 内模板的内容
- 不改变 `PromptAssembler`、`SystemPromptManager`、`PromptManager` 的接口

---

## 变更清单

### 1. `message_builder.py` 搬迁到 `core/adapter/`

**移动**：
- `Module/system_prompt/message_builder.py` → `core/adapter/message_builder.py`
- `Module/system_prompt/tests/integration/test_message_builder.py` → `core/adapter/tests/test_message_builder.py`

**修改**：

| 文件 | 变更 |
|------|------|
| `Module/system_prompt/__init__.py` | 删除 `MessageBuilder` 的 export。改为从新位置 re-export（`from core.adapter.message_builder import MessageBuilder`），加 `DeprecationWarning`，标注"将在后续版本移除"。保留 2 个版本作为过渡。 |
| `core/prompt_assembler.py:11` | `from Module.system_prompt.message_builder` → `from core.adapter.message_builder` |
| `core/adapter/message_builder.py` | 文件内容不变，仅移动位置。docstring 中"不依赖 core/ 的任何组件"改为"依赖 core.schema 类型" |
| `core/adapter/tests/test_message_builder.py` | 测试代码不变，仅更新 import 路径。pytest marker 保持 `integration` |
| `core/adapter/__init__.py` | 新增 `from .message_builder import MessageBuilder`，加入 `__all__` |

### 2. `prompt/` 搬迁到 `core/prompts/`

**移动**：
- `prompt/*.j2` → `core/prompts/`（所有 .j2 模板文件）
- `prompt/ref/` → `core/prompts/ref/`（参考文档）
- `prompt/README.md` → `core/prompts/README.md`

**修改**：

| 文件 | 当前 | 改为 |
|------|------|------|
| `core/memory/consolidator.py:32` | `Path("prompt/ExtractMemory.j2")` | `_prompts_dir() / "ExtractMemory.j2"` |
| `core/memory/consolidator.py:58` | `Path("prompt/ConsolidationCompare.j2")` | `_prompts_dir() / "ConsolidationCompare.j2"` |
| `core/memory/memory_reflector.py:31` | `Path("prompt/MemoryReflector.j2")` | `_prompts_dir() / "MemoryReflector.j2"` |
| `core/personality/engine.py:136` | `prompt_dir: str = "prompt"` | `prompt_dir: str | None = None`，`None` 时自动解析为 `Path(__file__).resolve().parent.parent / "prompts"`（`personality/` → `core/` → `prompts/`） |
| `core/personality/reflector.py:42` | `prompt_dir: str = "prompt"` | 同上 |
| `core/personality/tests/test_engine.py:33` | `PromptManager("prompt")` | `PromptManager(str(_PROMPTS_DIR))` — `_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"`（`tests/` → `personality/` → `core/` → `prompts/`） |
| `core/personality/tests/test_reflector.py:42` | `prompt_dir="prompt"` | 同上 |

**`_prompts_dir()` 工具函数**：在 `core/memory/` 下加一个 module-level helper（或在 `core/_utils.py` 添加）：

```python
from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

def _prompts_dir() -> Path:
    return _PROMPTS_DIR
```

`consolidator.py` 和 `memory_reflector.py` 各自定义一个（或抽到 `core/_utils.py` 共享）。consolidator 和 memory_reflector 的 modules 都在 `core/memory/`，所以路径 = `Path(__file__).resolve().parent.parent / "prompts"`（`memory/` → `core/` → `prompts/`）。

对于 `engine.py` 和 `reflector.py`（在 `core/personality/`），路径 = `Path(__file__).resolve().parent.parent / "prompts"`（`personality/` → `core/` → `prompts/`）。

### 3. 内联 `sensory.py` 的 emoji helper

**文件**：`Module/system_prompt/sections/sensory.py`

**当前**（第 45-60 行左右）：
```python
try:
    from core.schema.mood import stt_emotion_emoji
except ImportError:
    def stt_emotion_emoji(v): return v

try:
    from core.schema.mood import stt_event_emoji
except ImportError:
    def stt_event_emoji(v): return v
```

**改为**：内联实现。从 `core/schema/mood.py` 复制这两个函数（纯 dict lookup，无副作用），删除 try/except ImportError 守卫：

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

> 注意：原 `sensory.py` 中 `stt_emotion_emoji` 是 import 到模块作用域的，内联后需更新 `_emotion_emoji()` 和 `_format_events()` 中的调用名（从 `stt_emotion_emoji` → `_stt_emotion_emoji`，`stt_event_emoji` → `_stt_event_emoji`）。

### 4. 文档更新

| 文档 | 变更 |
|------|------|
| `CLAUDE.md` | Architecture 图中 `prompt/` 路径更新；`Module/system_prompt` 描述中去掉 MessageBuilder |
| `Module/system_prompt/templates/README.md` | 无需修改（system prompt 模板未变） |
| `prompt/README.md` → `core/prompts/README.md` | 更新加载路径示例代码（`Path("prompt/...")` → `Path(__file__).parent / "prompts" / "..."`) |
| `core/personality/README.md` | 如有引用 `prompt/` 路径，更新 |
| `docs/completed.md` | 搬迁完成后添加条目 |

### 5. 目录清理

搬迁完成后，删除空的 `prompt/` 目录（或保留一个 README 指向新位置作为过渡）。

---

## 验证

```bash
# 1. Module/system_prompt 零 core 依赖
grep -rn "from core\." Module/system_prompt/ --include="*.py" | grep -v "/tests/" | grep -v __pycache__
# 预期：空输出（生产代码零引用）

# 2. 全量 Module/system_prompt 测试
python -m pytest Module/system_prompt/tests/ -v

# 3. 全量 core 测试
python -m pytest core/ -v -x --tb=short

# 4. 确认 prompt/ 路径不受 cwd 影响
cd /tmp && python -c "from core.memory.consolidator import _get_extraction_prompt; print('OK')"

# 5. 全量 prompt 模板加载测试
python -m pytest core/memory/tests/ core/personality/tests/ -v -k "consolidator or reflector or engine"
```

---

## 约束

- 纯搬迁 + import 更新，零行为变更
- 不删任何模板文件，不改变模板内容
- `Module/system_prompt/__init__.py` 保留 `MessageBuilder` 的 re-export + `DeprecationWarning`，2 个版本后移除
- 所有 pytest marker 保持不变
