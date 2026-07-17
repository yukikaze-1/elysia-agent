# CharacterEngine 重构：解散 `_params`，合并 EmotionEngine

## Context

`SessionMemory._params: dict[str, Any]` 通过 `set_param(key, value)` / `get_param(key, default)` 被 4 个子系统通过字符串 key 读写。共 15 个 key，分布在 PersonalityEngine、EmotionEngine、CharacterReflector、MemoryReflector 四处。core/schema 已定义了 `CharacterState` / `EmotionState` / `ReflectorState` Pydantic 模型，但仅用于持久化（save/restore），运行时仍然用裸 dict。

同时，EmotionEngine（L4 Mood）天然是 4 层性格模型的最上层，不应独立成一个模块。

## Goals

1. 删除 `SessionMemory._params` + `set_param()` / `get_param()`
2. 将 `PersonalityEngine` + `EmotionEngine` 合并为 `CharacterEngine`
3. `CharacterReflector` 嵌入 `CharacterEngine`（与 `MemoryReflector` 对仗命名）
4. `CharacterState` 合并 mood/mood_intensity，成为唯一内存表示
5. `MemoryReflector` 独立持有自身状态，不经过 session

## Non-Goals

- 不改变 MemoryReflector 的触发逻辑
- 不改变 CharacterReflector 的反思逻辑
- 不改变 system prompt section 渲染顺序
- 不改变前端 debug 页面展示

---

## Design

### 一、新增类型

#### Mood StrEnum + 工具函数 (`core/schema/mood.py`)

```python
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


# 从 emotion_engine.py 迁移的模块级工具函数
def stt_emotion_emoji(emotion: str) -> str: ...
def stt_event_emoji(event: str) -> str: ...

# 从 emotion_engine.py 迁移的常量
_MOOD_TO_TTS_EMOTION: dict[Mood, str] = { ... }
```

**调用方更新**：
- `Module/system_prompt/sections/sensory.py:55-64` — `from core.emotion_engine import stt_emotion_emoji` → `from core.schema.mood import stt_emotion_emoji`
- `core/cognitive_loop.py:58-66` — `_MOOD_TO_TTS_EMOTION` 迁移到 `core/schema/mood.py`
- 删除文件 `core/emotion_engine.py`

#### StateHistoryEntry TypedDict (`core/schema/character.py`)

```python
from typing import TypedDict

class StateHistoryEntry(TypedDict):
    timestamp: float
    content: str
```

### 二、数据模型变更

#### CharacterState — 合并 mood + reflector（`core/schema/character.py`）

```python
from .mood import Mood

class CharacterState(BaseModel):
    """CharacterEngine 完整状态。"""
    session_id: SessionId
    # L2 Self + 压缩追踪
    self_l2: str = ""
    state_l3: str = ""
    state_history: list[dict] = Field(default_factory=list)  # StateHistoryEntry
    last_awareness_time: IsoTimestamp | None = None
    compression_last_at: IsoTimestamp | None = None
    compression_last_count: int = 0
    # L4 Mood（原 EmotionState）
    mood: Mood = Mood.NEUTRAL
    mood_intensity: float = 0.5
    # CharacterReflector 运行时状态
    reflector_buffer: list[tuple] = Field(default_factory=list)
    reflector_context: list[dict] = Field(default_factory=list)
    self_perception_history: list[dict] = Field(default_factory=list)
    updated_at: IsoTimestamp = Field(default_factory=now_iso)
```

#### EmotionState — 删除

`core/schema/emotion.py` 文件删除，字段合并入 `CharacterState`。

#### ReflectorState — 拆入两处

- `reflector_buffer`, `reflector_context`, `self_perception_history` → `CharacterState`
- `mr_round_count`, `mr_new_memory_count`, `mr_last_summary` → `MemoryReflector` 实例变量（不需要独立 Pydantic，由 MemoryReflector 自行序列化到 session doc）

`core/schema/reflector.py` 文件删除。

### 三、CharacterEngine (`core/personality/engine.py`)

`PersonalityEngine` 重命名为 `CharacterEngine`，合并 `EmotionEngine` 的全部功能。

**构造参数**（合并 PersonalityEngine + EmotionEngine + CharacterReflector）：

```python
class CharacterEngine:
    def __init__(
        self,
        *,
        llm: LLMAdapter,                # LLM (compression + reflector 调用)
        state: CharacterState,          # CharacterState 实例，替代 _params dict
        character_name: str,
        user_name: str,
        base_personality: str,          # L1 Core 文本
        character_config: CharacterConfig,  # 原 EmotionEngine 依赖：emotion.thresholds + idle_expressions
        output: OutputManager,              # 原 EmotionEngine 依赖：表情推送
        prompt_dir: str = "prompt",
    ):
        # 原 EmotionEngine
        self._character = character_config
        self._output = output
        # ...

    # ---- 原 PersonalityEngine ----
    def render_c1(self) -> C1Data: ...
    def render_c9(self) -> C9Data: ...
    async def consume_trigger(self, source, data): ...
    async def _maybe_compress(self): ...

    # ---- 原 EmotionEngine ----
    def analyze_user(self, text: str): ...
    def analyze_user_stt(self, stt_emotion, stt_events): ...
    def analyze_assistant(self, text: str): ...
    def _push_expression(self, name, intensity): ...  # self._output.expression(...)
    def _push_idle(self): ...
    def _decay_intensity(self): ...

    # ---- CharacterReflector ----
    # 构造函数中创建 self._reflector = CharacterReflector(engine=self)
    # 构造顺序：先初始化自身字段 → 再创建 CharacterReflector 传入 self
    #   此时 engine 已可用（state/llm/character_name/user_name/base_personality 均已设好），
    #   不存在循环依赖问题（CharacterReflector 只存 engine 引用，不创建 engine）
```

**关键变更**：引擎不再通过 `session.get_param("mood")` 访问状态，改为 `self.state.mood`。不再调用 `session.set_param("self_l2", ...)`，改为 `self.state.self_l2 = ...`。

`EmotionEngine` 的 `save_emotion_state()` / `restore_emotion_state()` 删除——合并入 `CharacterState` 的统一持久化。

### 四、CharacterReflector (`core/personality/reflector.py`)

**构造变更**：`CharacterReflector.__init__` 新增 `engine: CharacterEngine` 参数（删除 `set_personality_engine()` 延迟注入）。构造必须在 `CharacterEngine.__init__` 完成自身字段初始化后调用——此时 engine 已可用，不存在循环依赖（CharacterReflector 只存 engine 引用，不反向创建 engine）。

**状态访问**：删除所有 `session.get_param/set_param` 调用：
- 读 `mood` → `self._engine.state.mood`
- 读 `self_l2` → `self._engine.state.self_l2`
- 读/写 `reflector_buffer` → `self._engine.state.reflector_buffer`
- 读/写 `self_perception_history` → `self._engine.state.self_perception_history`

**持久化**：`save_reflector_state()` / `restore_reflector_state()` 合并入 CharacterEngine 的统一持久化（CharacterState 已包含所有 reflector 字段）。

### 五、SessionMemory (`core/memory/session.py`)

```python
class SessionMemory:
    def __init__(self, ..., character_state: CharacterState):
        self.character_state: CharacterState = character_state
        self.own_life: str = ""                    # OwnLife Section 直接读取
        self._memory_reflector = None              # MemoryReflector | None（由 Agent 设置）
        # 删除: _params, set_param(), get_param()

    def set_memory_reflector(self, mr) -> None: ...  # Agent 接线用
```

**删除**：
- `_params: dict[str, Any]`
- `set_param(key: str, value: Any)`
- `get_param(key: str, default: Any = None) -> Any`

**save() 改为**：
```python
await self._repo.save(
    self._session_id,
    messages=list(self._messages),
    character_state=self.character_state,
    reflector_state=self._memory_reflector.save_state() if self._memory_reflector else None,
)
```

**load() 改为**：从 `SessionRepository.load()` 获取 `(messages, character_state, reflector_state)`，恢复 `self.character_state` 和 `self._memory_reflector`。

### 六、MemoryReflector (`core/memory/memory_reflector.py`)

删除所有 `session.get_param/set_param` 调用。计数器已经是实例变量（`_round_count`, `_new_memory_count`, `_last_summary`）。

**持久化 API**：`MemoryReflector` 自身负责 save/restore，通过专用的 Pydantic 模型与 SessionRepository 交互。

```python
# core/schema/memory_reflector.py（新增）
class MemoryReflectorState(BaseModel):
    """MemoryReflector 计数器状态。随 session 一起持久化。"""
    mr_round_count: int = 0
    mr_new_memory_count: int = 0
    mr_last_summary: dict[str, object] = Field(default_factory=dict)

# core/memory/memory_reflector.py
class MemoryReflector:
    def save_state(self) -> MemoryReflectorState: ...
    def restore_state(self, state: MemoryReflectorState) -> None: ...
```

**SessionMemory.save() 改为**：
```python
await self._repo.save(
    session_id,
    messages=list(self._messages),
    character_state=self.character_state,
    reflector_state=self._memory_reflector.save_state() if self._memory_reflector else None,
)
```

**SessionRepository.save() 改为**：
```python
async def save(self, session_id: SessionId, messages: Sequence[Message],
               character_state: CharacterState,
               reflector_state: MemoryReflectorState | None = None) -> None:
    doc = {
        "session_id": session_id,
        "messages": serialized,
        "character_state": character_state.model_dump(mode="json"),
        "reflector_state": reflector_state.model_dump(mode="json") if reflector_state else {},
        "updated_at": now_iso(),
    }
```

**SessionRepository.load() 改为**：从 doc 中恢复 `character_state` 和 `reflector_state` 两个 Pydantic 对象。

**Agent 恢复流程**：
```python
messages, character_state, reflector_state = await session_repo.load(sid)
self._character_engine.state = character_state
self._memory_reflector.restore_state(reflector_state)
```

### 七、CharacterStateRepository (`core/storage/character_state_repo.py`)

简化为只操作 `CharacterState`（删除 `save_emotion` / `load_emotion` / `save_reflector` / `load_reflector`）。集合 `session_state` 只存 `doc_type = "character"`。

MemoryReflector 状态随 session doc 一起持久化（`SessionRepository.save/load` 独立字段），不再经过 `character_state_repo`。

### 八、Agent (`core/agent.py`)

```python
# 旧
self._personality_engine = PersonalityEngine(...)
self._emotion = EmotionEngine(...)
self._character_reflector = CharacterReflector(...)
self._character_reflector.set_personality_engine(self._personality_engine)

# 新
self._character_engine = CharacterEngine(...)  # 内部已包含 CharacterReflector + EmotionEngine
```

- 删除所有 `self._emotion` 引用
- 删除所有 `self._character_reflector` 直接引用（由 CharacterEngine 持有）
- 状态恢复：`character_state = await self._character_state_repo.load_character(...)` → `self._character_engine.state = character_state`
- 关机保存：`await self._character_state_repo.save_character(self._character_engine.state)`

### 九、CognitiveLoop (`core/cognitive_loop.py`)

- `self._emotion.analyze_user(...)` → `self._character_engine.analyze_user(...)`
- `self._emotion.analyze_assistant(...)` → `self._character_engine.analyze_assistant(...)`
- `self._session.get_param("mood", "neutral")` → `self._session.character_state.mood`

### 十、其他消费者

| 文件 | 旧 | 新 |
|------|-----|-----|
| `Module/system_prompt/sections/emotion.py` | `session.get_param("mood")` | `session.character_state.mood` |
| `Module/system_prompt/sections/own_life.py` | `session.get_param("own_life", "")` | `session.own_life` — `SessionMemory` 新增 `own_life: str = ""` 实例变量。当前无人写入、C8 永远空，不纳入 CharacterState |
| `core/debug_info.py` | `getattr(agent, "_personality_engine")` + `session.get_param(...)` | `agent._character_engine.state.xxx` |
| `core/prompt_assembler.py` | `self._personality_engine.render_c1()` | `self._character_engine.render_c1()` |

### 十一、前端影响

| 文件 | 改动 |
|------|------|
| `static/debug.html` | `info.personality_engine_exists` → `info.character_engine_exists` |
| `static/debug.html:713` | `'PersonalityEngine'` 标题 → `'CharacterEngine'` |
| `static/debug.html:666` | 提示文本更新 |
| `static/debug.md` | 字段名更新 |
| 其他 JS | `info.session_params` 字段不再存在（改为 `character_state` 子字段）——检查前端是否直接依赖 `session_params` 的 dict 结构 |

### 十二、文档更新

| 文件 | 改动 |
|------|------|
| `core/README.md` | `PersonalityEngine` → `CharacterEngine`，删除 `EmotionEngine` 独立条目 |
| `core/personality/README.md` | 全面重写：4 层模型 + CharacterReflector 嵌入 + CharacterState |
| `docs/architecture.md` | 引擎名更新 + `_params` 删除 + section 4.5 合并 |
| `docs/database-storage.md` | `_params` 行改为 `character_state` |
| `README.md` | 架构树上 `emotion_engine.py` → 合并入 `engine.py` |
| `todo.md` | 更新 Phase 2 引用名 |
| `static/debug.md` | 字段名更新 |

### 十三、测试更新

| 测试文件 | 改动 |
|----------|------|
| `core/personality/tests/test_engine.py` | 29 tests → `CharacterEngine`，新增 mood 操作测试 |
| `core/tests/test_phase3.py` | 引用名更新 + `session.get_param` → `session.character_state.xxx` |
| `core/tests/test_phase2.py` | 同上 |
| `core/tests/test_phase1.py` | EmotionEngine 测试合并入 engine 测试 |
| `core/memory/tests/test_session.py` | `_params` 测试改为 `character_state` |

### 十四、删除文件清单

- `core/emotion_engine.py`
- `core/schema/emotion.py`
- `core/schema/reflector.py`

### 十五、新增文件清单

- `core/schema/mood.py` — `Mood` StrEnum + `stt_emotion_emoji()` / `stt_event_emoji()` / `_MOOD_TO_TTS_EMOTION`（从 emotion_engine.py 迁移）
- `core/schema/memory_reflector.py` — `MemoryReflectorState` Pydantic 模型
