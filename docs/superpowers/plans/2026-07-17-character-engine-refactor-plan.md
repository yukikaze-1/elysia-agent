# CharacterEngine 重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 解散 `_params` dict，合并 PersonalityEngine + EmotionEngine → CharacterEngine，删除 SessionMemory.set_param/get_param

**Architecture:** Schema 层新增 Mood StrEnum + MemoryReflectorState，CharacterState 合并 mood/mood_intensity + reflector 状态。SessionMemory 持有一个 CharacterState 实例（原地更新保引用一致），MemoryReflector 独立持有 MemoryReflectorState。

**Tech Stack:** Python 3.13, Pydantic v2, motor (MongoDB async)

**Spec:** `docs/superpowers/specs/2026-07-17-character-engine-refactor-design.md`

## Global Constraints

- `Mood` 必须覆盖 emotion_engine.py 中 `MOOD_KEYWORDS` 的 10 个 mood 值
- `CharacterState` 是唯一状态表示——删除所有 `set_param`/`get_param` 调用
- `session.load()` 必须原地更新 `CharacterState` 字段（不替换引用）
- `last_awareness_time` / `compression_last_at` 类型为 `float | None`（epoch float，非 IsoTimestamp）
- `CharacterStateRepository` 删除整个文件

---

### Task 1: 新增 `core/schema/mood.py` — Mood StrEnum + 工具函数

**Files:** Create: `core/schema/mood.py` / Modify: `core/schema/__init__.py`

**Produces:** `Mood(StrEnum)`, `stt_emotion_emoji(tag: str) -> str`, `stt_event_emoji(event: str) -> str`, `_MOOD_TO_TTS_EMOTION: dict[Mood, str]`

- [ ] **Step 1: 创建 mood.py**

从 `core/emotion_engine.py` 复制 `stt_emotion_emoji` 和 `stt_event_emoji`（lines 93-100）。从 `core/cognitive_loop.py:58-66` 迁移 `_MOOD_TO_TTS_EMOTION`，key 改为 `Mood` 枚举值，清理 `"angry"`/`"fearful"` 死条目。

```python
"""Mood 枚举 + TTS 情绪映射 + STT 工具函数。"""
from enum import StrEnum


class Mood(StrEnum):
    NEUTRAL = "neutral"
    HAPPY = "happy"
    SAD = "sad"
    CURIOUS = "curious"
    EXCITED = "excited"
    WARM = "warm"
    CONCERNED = "concerned"
    CALMING = "calming"
    REASSURING = "reassuring"
    UNDERSTANDING = "understanding"


# ---- 从 emotion_engine.py 原样复制 ----

_STT_EMOTION_EMOJI: dict[str, str] = {
    "HAPPY": "\U0001f60a", "SAD": "\U0001f614", "ANGRY": "\U0001f621",
    "NEUTRAL": "", "FEARFUL": "\U0001f630", "DISGUSTED": "\U0001f922",
    "SURPRISED": "\U0001f62e",
}

def stt_emotion_emoji(tag: str) -> str:
    return _STT_EMOTION_EMOJI.get(tag, "")


_STT_EVENT_EMOJI: dict[str, str] = {
    "Speech": "", "BGM": "\U0001f3bc", "Applause": "\U0001f44f",
    "Laughter": "\U0001f600", "Cry": "\U0001f62d",
    "Cough": "\U0001f637", "Sneeze": "\U0001f927",
    "Breath": "", "Sing": "", "Speech_Noise": "", "GBG": "",
    "Event_UNK": "",
}

def stt_event_emoji(event: str) -> str:
    return _STT_EVENT_EMOJI.get(event, "")


_MOOD_TO_TTS_EMOTION: dict[Mood, str] = {
    Mood.HAPPY: "happy", Mood.SAD: "sad",
    Mood.CURIOUS: "happy", Mood.EXCITED: "happy",
    Mood.WARM: "neutral", Mood.CONCERNED: "neutral",
    Mood.CALMING: "neutral", Mood.REASSURING: "neutral",
    Mood.UNDERSTANDING: "neutral", Mood.NEUTRAL: "neutral",
}
```

- [ ] **Step 2: 更新 `core/schema/__init__.py`**

```python
from core.schema.mood import Mood, stt_emotion_emoji, stt_event_emoji  # noqa: F401
```

- [ ] **Step 3: 更新 `core/cognitive_loop.py`**

删除 lines 58-66 的 `_MOOD_TO_TTS_EMOTION` 定义，改 import：
```python
from core.schema.mood import _MOOD_TO_TTS_EMOTION
```

- [ ] **Step 4: 更新 `Module/system_prompt/sections/sensory.py:55,64`**

```python
from core.schema.mood import stt_emotion_emoji, stt_event_emoji
```

- [ ] **Step 5: 运行测试**

```bash
python -m pytest core/schema/tests/ Module/system_prompt/tests/ -q --tb=short
```

- [ ] **Step 6: 提交**

```bash
git add -A && git commit -m "feat: 新增 core/schema/mood.py — Mood StrEnum + 工具函数 + TTS 映射"
```

---

### Task 2: 新增 `MemoryReflectorState` + 更新 CharacterState

**Files:** Create: `core/schema/memory_reflector.py` / Modify: `core/schema/character.py` / Delete: `core/schema/emotion.py`, `core/schema/reflector.py`

**Produces:** `MemoryReflectorState(BaseModel)`, `CharacterState` 合并 mood+reflector，`StateHistoryEntry(TypedDict)`

- [ ] **Step 1: 创建 `core/schema/memory_reflector.py`**

```python
"""MemoryReflectorState — MemoryReflector 计数器状态。"""
from pydantic import BaseModel, Field

class MemoryReflectorState(BaseModel):
    mr_round_count: int = 0
    mr_new_memory_count: int = 0
    mr_last_summary: dict[str, object] = Field(default_factory=dict)
```

- [ ] **Step 2: 更新 `core/schema/character.py`**

新增 `StateHistoryEntry` TypedDict，`CharacterState` 合并 mood + reflector 字段：

```python
from typing import TypedDict
from .mood import Mood

class StateHistoryEntry(TypedDict):
    timestamp: float
    content: str

class CharacterState(BaseModel):
    """CharacterEngine 完整状态。"""
    session_id: SessionId
    self_l2: str = ""
    state_l3: str = ""
    state_history: list[StateHistoryEntry] = Field(default_factory=list)
    last_awareness_time: float | None = None       # epoch float，非 IsoTimestamp
    compression_last_at: float | None = None         # epoch float，非 IsoTimestamp
    compression_last_count: int = 0
    mood: Mood = Mood.NEUTRAL
    mood_intensity: float = 0.5
    reflector_buffer: list[tuple[str, str]] = Field(default_factory=list)
    reflector_context: list[dict] = Field(default_factory=list)
    self_perception_history: list[dict] = Field(default_factory=list)
    updated_at: IsoTimestamp = Field(default_factory=now_iso)
```

- [ ] **Step 3: 删除 `core/schema/emotion.py` 和 `core/schema/reflector.py`**

```bash
rm core/schema/emotion.py core/schema/reflector.py
```

- [ ] **Step 4: 更新 `core/schema/__init__.py`**

删除 `EmotionState`、`ReflectorState` 的 import。新增：
```python
from core.schema.character import CharacterState, StateHistoryEntry  # noqa: F401
from core.schema.memory_reflector import MemoryReflectorState  # noqa: F401
```

- [ ] **Step 5: 运行 schema 测试**

```bash
python -m pytest core/schema/tests/ -q --tb=short
```

- [ ] **Step 6: 提交**

```bash
git add -A && git commit -m "refactor: CharacterState 合并 mood+reflector；新增 MemoryReflectorState"
```

---

### Task 3: 更新 SessionRepository save/load

**Files:** Modify: `core/storage/session_repo.py`

**Consumes:** `CharacterState`, `MemoryReflectorState`

- [ ] **Step 1: 更新 `save()` 签名**

```python
async def save(self, session_id: SessionId, messages: Sequence[Message],
               character_state: CharacterState,
               reflector_state: MemoryReflectorState | None = None) -> None:
    if self._db is None:
        return
    serialized = [m.model_dump(mode="json") for m in messages]
    doc = {
        "session_id": session_id, "messages": serialized,
        "character_state": character_state.model_dump(mode="json"),
        "reflector_state": reflector_state.model_dump(mode="json") if reflector_state else {},
        "updated_at": now_iso(),
    }
    await self._db.update_one(
        COLLECTION, {"session_id": session_id}, {"$set": doc}, upsert=True,
    )
```

- [ ] **Step 2: 更新 `load()` 返回类型**

```python
async def load(self, session_id: SessionId
               ) -> tuple[list[Message], CharacterState, MemoryReflectorState] | None:
    if self._db is None:
        return None
    results = await self._db.find(COLLECTION, {"session_id": session_id}, limit=1)
    if not results:
        return None
    doc = results[0]
    msgs_raw = doc.get("messages", [])
    for m in msgs_raw:
        m.pop("_id", None)
    messages = [_message_adapter.validate_python(m) for m in msgs_raw]
    cs = CharacterState.model_validate(doc.get("character_state", {}))
    rs = MemoryReflectorState.model_validate(doc.get("reflector_state", {}))
    return messages, cs, rs
```

- [ ] **Step 3: 运行 storage 测试**

```bash
python -m pytest core/storage/tests/ -q --tb=short
```

- [ ] **Step 4: 提交**

```bash
git add -A && git commit -m "refactor: SessionRepository save/load 包含 CharacterState + MemoryReflectorState"
```

---

### Task 4: CharacterEngine — 合并 PersonalityEngine + EmotionEngine

**Files:** Modify: `core/personality/engine.py`

**Consumes:** `CharacterState`, `Mood`, `CharacterConfig`, `OutputManager`, `LLMAdapter`

- [ ] **Step 1: 重命名 `PersonalityEngine` → `CharacterEngine`，更新 `__init__`**

合并 PersonalityEngine + EmotionEngine + CharacterReflector 的构造参数：

```python
class CharacterEngine:
    def __init__(self, *, llm, state: CharacterState, character_name: str,
                 user_name: str, base_personality: str,
                 character_config, output, prompt_dir: str = "prompt"):
        self.state = state
        self._llm = llm
        self._character_name = character_name
        self._user_name = user_name
        self._l1_core = base_personality
        self._character = character_config
        self._output = output
        self._idle_expressions = character_config.emotion.idle_expressions
        self._last_awareness_time: float = state.last_awareness_time or 0.0
        self._compression_lock = asyncio.Lock()
        try:
            self._pm = PromptManager(prompt_dir)
        except FileNotFoundError:
            logger.warning("CharacterEngine: 模板目录不存在")
            self._pm = None
        from .reflector import CharacterReflector
        self._reflector = CharacterReflector(
            engine=self, l1_core=base_personality, llm=llm,
            character_name=character_name, user_name=user_name,
            prompt_dir=prompt_dir)
```

- [ ] **Step 2: 迁移 EmotionEngine 方法**

从 `core/emotion_engine.py` 复制以下方法到 CharacterEngine，`self._session.get_param("mood")` → `self.state.mood`：

- `analyze_user(text)`, `analyze_user_stt(stt_emotion, stt_events)`, `analyze_assistant(text)`
- `_push_expression(name, intensity)`, `_push_idle()`, `push_idle_expression()`, `_decay_intensity()`, `_schedule(coro)`
- `self._idle_expressions = character_config.emotion.idle_expressions` 加到构造函数
- 模块级常量 `MOOD_KEYWORDS`, `_STT_EMOTION_TO_MOOD` 原样复制到 engine.py 顶部

- [ ] **Step 3: 更新 PersonalityEngine 方法 — `_params` → `self.state`**

全局替换所有 `self._session.get_param("xxx")` → `self.state.xxx`，`self._session.set_param("xxx", v)` → `self.state.xxx = v`。

- [ ] **Step 4: 删除 `save_character_state()` / `restore_character_state()` + 新增 `buffer_message()`**

```python
def buffer_message(self, user_msg: str, assistant_msg: str) -> None:
    if self._reflector:
        self._reflector.buffer(user_msg, assistant_msg)

async def flush_reflector(self) -> None:
    """Agent shutdown 时调用 — 触发未完成的反思。"""
    if self._reflector:
        await self._reflector.flush()
```

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "refactor: PersonalityEngine→CharacterEngine, 合并 EmotionEngine"
```

---

### Task 5: CharacterReflector 重写

**Files:** Modify: `core/personality/reflector.py`

- [ ] **Step 1: 更新构造函数**

```python
class CharacterReflector:
    def __init__(self, *, engine, l1_core: str, llm,
                 character_name: str, user_name: str, prompt_dir: str = "prompt"):
        self._engine = engine
        self._l1_core = l1_core
        self._llm = llm
        self._character_name = character_name
        self._user_name = user_name
        # buffer/context 直接读写 engine.state，不保留本地副本
        self._reflect_lock = asyncio.Lock()
        try:
            self._pm = PromptManager(prompt_dir)
        except FileNotFoundError:
            self._pm = None
```

删除 `set_personality_engine()`。

- [ ] **Step 2: 替换所有 `session.get_param/set_param` → `engine.state.xxx`**

- `reflector_buffer`, `reflector_context`, `self_l2`, `state_history`, `mood`, `mood_intensity`, `self_perception_history` → 全部通过 `self._engine.state.xxx` 访问
- `mr_round_count`, `mr_new_memory_count`, `mr_last_summary` → 删除（MemoryReflector 自己管）
- 删除本地 `self._buffer` 和 `self._context` 实例变量——全部用 `self._engine.state.reflector_buffer` / `self._engine.state.reflector_context`

- [ ] **Step 3: 删除降级路径**

删除 `_reflect()` 中 `if self._personality_engine is not None` 的 else 块（lines 347-353）。

- [ ] **Step 4: 删除 `save_reflector_state()` / `restore_reflector_state()`**

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "refactor: CharacterReflector engine.state 访问，删除 session param"
```

---

### Task 6: SessionMemory 删除 `_params`

**Files:** Modify: `core/memory/session.py`

- [ ] **Step 1: 更新 `__init__`**

新增 `character_state` 参数 + `own_life` + `_memory_reflector`，删除 `_params` / `set_param` / `get_param`。

```python
def __init__(self, ..., character_state: CharacterState | None = None):
    self.character_state = character_state or CharacterState(session_id=session_id)
    self.own_life: str = ""
    self._memory_reflector = None
```

- [ ] **Step 2: 添加 `set_memory_reflector(mr)`**

```python
def set_memory_reflector(self, mr) -> None:
    self._memory_reflector = mr
```

- [ ] **Step 3: 更新 `save()` — 传 character_state + reflector_state**

MongoDB: `await self._repo.save(sid, list(self._messages), character_state=self.character_state, reflector_state=self._memory_reflector.save_state() if self._memory_reflector else None)`

JSONL: doc 中用 `character_state` / `reflector_state` 字段替代 `params`。

- [ ] **Step 4: 更新 `load()` — 原地更新 character_state**

```python
for field in cs.model_fields:
    setattr(self.character_state, field, getattr(cs, field))
```

- [ ] **Step 5: 更新 `_load_from_jsonl()` — 同理**

- [ ] **Step 6: 提交**

```bash
git add -A && git commit -m "refactor: SessionMemory 删除 _params，改用 CharacterState"
```

---

### Task 7: MemoryReflector 独立持久化

**Files:** Modify: `core/memory/memory_reflector.py`

- [ ] **Step 1: 添加 `save_state()` / `restore_state()`**

```python
def save_state(self) -> MemoryReflectorState:
    return MemoryReflectorState(
        mr_round_count=self._round_count,
        mr_new_memory_count=self._new_memory_count,
        mr_last_summary=self._last_summary)

def restore_state(self, state: MemoryReflectorState) -> None:
    self._round_count = state.mr_round_count
    self._new_memory_count = state.mr_new_memory_count
    self._last_summary = state.mr_last_summary
```

- [ ] **Step 2: 删除旧的 `restore_state()` 和 `_save_state()` 方法**

- [ ] **Step 3: 提交**

```bash
git add -A && git commit -m "refactor: MemoryReflector 独立 save_state/restore_state"
```

---

### Task 8: Agent 接线

**Files:** Modify: `core/agent.py`

- [ ] **Step 1: 更新 imports**

删除 `EmotionEngine`, `CharacterReflector`, `PersonalityEngine`, `CharacterStateRepository`（已删文件）。新增 `CharacterEngine`。

- [ ] **Step 2: 创建 CharacterEngine**

```python
self._character_engine = CharacterEngine(
    llm=self._llm, state=self._session.character_state,
    character_name=self._character.name,
    user_name=self._config.agent.user_name,
    base_personality=self._character.base_personality,
    character_config=self._character, output=self._output,
    prompt_dir="prompt")
```

- [ ] **Step 3: 接线**

```python
self._cognitive = CognitiveLoop(..., character_engine=self._character_engine, ...)
self._session.set_memory_reflector(self._memory_reflector)
```

- [ ] **Step 4: 更新 `_start_persistence()`**

删除 `_character_state_repo.load_character/load_emotion/load_reflector` + `self._character_reflector.restore_state()` + `self._personality_engine.restore_state()` + `self._emotion.restore_state()`。

- [ ] **Step 5: 删除 `_character_state_repo` 创建行 + 更新 `_stop_persistence()`**

```python
# 删除:
self._character_state_repo = CharacterStateRepository()
```

`_stop_persistence()` 中删除 `_character_state_repo.save_character/save_reflector` + `self._emotion.save_emotion_state()` + `self._memory_reflector.flush()`（计数器已通过 session.save() 中的 mr.save_state() 持久化，flush() 无意义）。

- [ ] **Step 6: 更新 `_db_health_loop()`**

删除 `_character_state_repo.health_check()`。

- [ ] **Step 7: 全局替换引用**

`self._emotion` → `self._character_engine`（emotion 方法调用）
`self._personality_engine` → `self._character_engine`
`self._character_reflector.flush()` → `self._character_engine.flush_reflector()`

- [ ] **Step 8: 提交**

```bash
git add -A && git commit -m "refactor: Agent 接线 CharacterEngine，删除 emotion/character_state_repo"
```

---

### Task 9: CognitiveLoop + 消费者更新

**Files:** Modify: `core/cognitive_loop.py`, `core/prompt_assembler.py`, `core/debug_info.py`, `Module/system_prompt/sections/emotion.py`, `Module/system_prompt/sections/own_life.py`, `core/personality/__init__.py`

- [ ] **Step 1: cognitive_loop.py**

删除 `emotion` 和 `reflector` 参数，新增 `character_engine`：
```python
self._character_engine = character_engine
# self._emotion.analyze_user(...) → self._character_engine.analyze_user(...)
# self._reflector.buffer(...) → self._character_engine.buffer_message(...)
# session.get_param("mood") → session.character_state.mood
```

- [ ] **Step 2: prompt_assembler.py**

`self._personality_engine` → `self._character_engine`

- [ ] **Step 3: debug_info.py**

`agent._personality_engine` → `agent._character_engine`，`session.get_param(...)` → `session.character_state.xxx`，`session_params` ← `character_state.model_dump(mode="json")`，`personality_engine_exists` → `character_engine_exists`

- [ ] **Step 4: Module/system_prompt/sections/emotion.py**

`session.get_param("mood")` → `session.character_state.mood`

- [ ] **Step 5: Module/system_prompt/sections/own_life.py**

`session.get_param("own_life")` → `session.own_life`

- [ ] **Step 6: core/personality/__init__.py**

```python
from .engine import CharacterEngine
from .reflector import CharacterReflector
__all__ = ["CharacterEngine", "CharacterReflector"]
```

- [ ] **Step 7: 提交**

```bash
git add -A && git commit -m "refactor: 消费者更新 — CharacterEngine 引用 + session param→character_state"
```

---

### Task 10: 清理旧文件

**Files:** Delete: `core/emotion_engine.py`, `core/storage/character_state_repo.py`

- [ ] **Step 1: 删除文件**

```bash
rm core/emotion_engine.py core/storage/character_state_repo.py
```

`character_state_repo.py` 的所有方法已被 Task 8 删除引用，直接删文件即可。

- [ ] **Step 2: 提交**

```bash
git add -A && git commit -m "refactor: 删除 emotion_engine.py + character_state_repo.py"
```

---

### Task 11: 前端 + 文档 + 测试 + 终验

**Files:** Modify: `static/debug.html`, `static/debug.md`, `core/README.md`, `core/personality/README.md`, `docs/architecture.md`, `docs/database-storage.md`, `README.md`, `todo.md`, 所有测试文件

- [ ] **Step 1: 前端**

`static/debug.html`: `personality_engine_exists` → `character_engine_exists`，标题 `PersonalityEngine` → `CharacterEngine`，提示文本更新。`static/debug.md` 同步。

- [ ] **Step 2: 文档**

按 spec Section 12 逐个更新 `core/README.md`, `core/personality/README.md`, `docs/architecture.md`, `docs/database-storage.md`, `README.md`, `todo.md`。

- [ ] **Step 3: 测试**

更新 `core/personality/tests/test_engine.py`、`core/tests/test_phase1/2/3.py`、`core/memory/tests/test_session.py` 中所有旧引用名。

```bash
python -m pytest -m "unit or service" -q --tb=short
```

逐个修复编译/导入错误直到全绿。

- [ ] **Step 4: 终验**

```bash
# 确认 emotion_engine.py 已删除
ls core/emotion_engine.py 2>&1 | grep "No such file"
# 确认 set_param/get_param 无残留（排除 test）
grep -rn "get_param\|set_param" core/ --include="*.py" | grep -v test | grep -v __pycache__
# 预期: 空
# 全量
python -m pytest -m "unit or service" -q --tb=short
# 预期: 1593+ passed, 2 flaky
```

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "chore: 前端+文档+测试更新，终验清理"
```
