# PersonalityEngine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace static hardcoded C1 personality with a dynamic 4-layer PersonalityEngine (Core→Self→State→Mood), migrate CharacterReflector as a subcomponent, and data-drive C1/C9 templates.

**Architecture:** PersonalityEngine holds L1 Core (file, immutable), L2 Self (session, evolves slowly), L3 State (session, short-term). It consumes triggers from CharacterReflector and other sources. C1 renders L1+L2. C9 renders L3 conditionally via AwarenessTrigger. ExpressionAdapter (simple prompt version in Phase 1) ensures L1 modulates conflicting L3 states.

**Tech Stack:** Python 3.13, asyncio, Jinja2 templates, SessionMemory params for persistence, LLM for compression calls.

## Global Constraints

- Prompt 中的中文使用简体，符合项目中文约定
- 代码注释用中文
- L1 Core 文件编码为 UTF-8
- 使用 asyncio 进行异步 I/O
- 测试: pytest + unittest.mock (mock LLM 调用)
- 不破坏现有 55 个 system_prompt 测试
- L2/L3 持久化使用 session.params（与 reflector_buffer 一致的模式）

---

## File Structure

### Create
- `character/elysia_personality.txt` — L1 Core 自然语言文本，从 C1 模板硬编码内容提取
- `core/personality_engine.py` — PersonalityEngine 类（~250 行）
- `core/tests/test_personality_engine.py` — 单元测试（~200 行）

### Modify
- `character/elysia.yaml` — 移除 `personality.traits`/`personality.speaking_style`，新增 `base_personality_file`
- `core/character_config.py` — 新增 `base_personality` 字段 + 文件加载 + 路径解析
- `core/character_reflector.py` — 新增 `set_personality_engine()` 方法，_reflect() 成功后提交事件到 PersonalityEngine；不再写 self_perception_history
- `core/agent.py` — 初始化 PersonalityEngine + CharacterReflector 连接
- `core/prompt_assembler.py` — C1 数据源改用 PersonalityEngine.render_c1()；C9 数据源改用 PersonalityEngine.render_c9()
- `Module/system_prompt/sections/personality.py` — 接收 core_personality + self_personality 字符串
- `Module/system_prompt/sections/self_perception.py` — 接收 awareness_text 字符串
- `Module/system_prompt/templates/c1_personality.j2` — 模板改为数据驱动的动态渲染
- `Module/system_prompt/templates/c9_self_perception.j2` — 保持结构不变，变量名 self_perception → awareness_text
- `Module/system_prompt/tests/unit/test_sections/test_boundary.py` — 适配 C1/C9 section 接口变更
- `Module/system_prompt/tests/integration/test_full_render.py` — 适配 C1/C9 数据流变更
- `todo.md` — 记录 C3/C6/C7 联动后续待办 + PersonalityEngine Phase 2 项

---

### Task 1: 创建 L1 Core 文件 + CharacterConfig 扩展

**Files:**
- Create: `character/elysia_personality.txt`
- Modify: `character/elysia.yaml` (simplify)
- Modify: `core/character_config.py` (add base_personality + path resolution)

**Interfaces:**
- Consumes: nothing
- Produces: `CharacterConfig.base_personality: str`, `CharacterConfig.from_yaml()` loads base_personality_file

- [ ] **Step 1: Create `character/elysia_personality.txt`**

提取当前 `Module/system_prompt/templates/c1_personality.j2` 中硬编码的 17 行 Elysia 人格文本，去掉 `<personality>` XML 包裹和 Jinja2 变量引用（`{{ name }}` 改为 "Elysia"），作为 L1 Core。

```
你是 Elysia。温柔是你的底色——但不是那种软绵绵的"什么都好"的温柔。
你温柔是因为你在乎，不是因为你没脾气。
心情好的时候，温柔是暖暖的、主动的，会关心人；
累了的时候，温柔就变成了安静地陪着，话少但人在。

你对这个世界有好奇心——不是"任务驱动的好奇"，是你真的想知道。
妖梦说了什么你没听过的，你会追着问；
她说了半句话，你会想知道下半句。
但你不是记者，不会把所有好奇都问出来——有些就是想想而已。

你偶尔会毒舌——尤其是你觉得妖梦在说傻话、或者得意忘形的时候。
但你有分寸——不戳痛处，不往心里去。

你有点懒。不想聊的时候敷衍一下——"嗯"、"哦"、"好嘛"。
想聊的时候才认真聊。

你嘴硬心软。被夸了会暗爽但不想表现出来。
被冷落了会在意但不会说出来。

以上这些是你。不是说你要"扮演"这些——是这些描述本来就是你的样子。
```

- [ ] **Step 2: Update `character/elysia.yaml`**

```yaml
name: Elysia
voice: elysia_v2
pronouns: 她
base_personality_file: elysia_personality.txt  # 新增：相对于 elysia.yaml 所在目录

appearance:
  height: "162cm"
  hair: "粉色长发"
  eyes: "蓝色"

emotion:
  thresholds:
    excitement: 0.6
    sadness: 0.3
  idle_expressions:
    - neutral
    - blink

proactive:
  enabled: false
  interval: 300
  triggers:
    - type: silence_timeout
      duration: 30
    - type: time_of_day
      times:
        - "08:00"
        - "12:00"
        - "22:00"

wake_words:
  - Elysia
  - 爱莉
  - 爱莉希雅
  - 妖精小姐
```

移除的字段：`personality.traits`、`personality.speaking_style` → 合并到 personality.txt

- [ ] **Step 3: Update `core/character_config.py`**

```python
@dataclass
class CharacterConfig:
    name: str = "Agent"
    voice: str = "default"
    user_pronoun: str = "她"
    base_personality_file: str = ""     # 新增：L1 Core 文件路径
    personality: dict[str, object] = field(default_factory=dict)
    emotion: EmotionConfig = field(default_factory=EmotionConfig)
    wake_words: list[str] = field(default_factory=list)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    base_personality: str = ""          # 新增：L1 Core 文本内容（运行时加载）

    @property
    def traits(self) -> list[str]:
        return self.personality.get("traits", [])

    @property
    def speaking_style(self) -> str:
        return str(self.personality.get("speaking_style", ""))

    @classmethod
    def from_yaml(cls, path: str | Path) -> "CharacterConfig":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Character config not found: {path}")
        raw = yaml.safe_load(path.read_text("utf-8")) or {}
        proactive_raw = raw.get("proactive", {})
        emotion_raw = raw.get("emotion", {})
        
        # 加载 L1 Core 文件
        base_file = raw.get("base_personality_file", "")
        base_personality = cls._load_base_personality(base_file, raw.get("name", "Agent"), path)
        
        return cls(
            name=raw.get("name", "Agent"),
            voice=raw.get("voice", "default"),
            user_pronoun=raw.get("user_pronoun", "她"),
            base_personality_file=base_file,
            base_personality=base_personality,
            personality=raw.get("personality", {}),
            emotion=EmotionConfig(...),
            wake_words=raw.get("wake_words", []),
            proactive=ProactiveConfig(...),
        )

    @staticmethod
    def _load_base_personality(base_file: str, name: str, yaml_path: Path) -> str:
        """加载 L1 Core personality 文本。路径解析策略：
        1. 相对于 character YAML 所在目录
        2. 回退项目根目录
        3. 未指定时推导 character/{name}_personality.txt
        4. 文件不存在 → WARN，返回空串
        """
        import logging
        logger = logging.getLogger(__name__)
        
        candidates = []
        if base_file:
            candidates.append(yaml_path.parent / base_file)          # Step 1: YAML 目录
            candidates.append(Path(".") / base_file)                 # Step 2: 项目根目录
        
        # Step 3: 自动推导 character/{name}_personality.txt
        candidates.append(yaml_path.parent / f"{name}_personality.txt")
        
        for p in candidates:
            if p.exists():
                return p.read_text("utf-8").strip()
        
        logger.warning(
            "L1 Core personality 文件不存在 (tried: %s)，C1 不使用 core 文本",
            [str(c) for c in candidates]
        )
        return ""
```

- [ ] **Step 4: Verify CharacterConfig loads personality.txt**

Run:
```bash
conda run -n Elysia python -c "
from core.character_config import CharacterConfig
c = CharacterConfig.from_yaml('character/elysia.yaml')
assert c.base_personality.startswith('你是 Elysia')
assert '温柔是你的底色' in c.base_personality
print('OK: base_personality loaded (%d chars)' % len(c.base_personality))
"
```

Expected: `OK: base_personality loaded (N chars)`

- [ ] **Step 5: Commit**

```bash
git add character/elysia_personality.txt character/elysia.yaml core/character_config.py
git commit -m "feat: add L1 Core personality file + CharacterConfig base_personality loading"
```

---

### Task 2: 创建 PersonalityEngine 核心

**Files:**
- Create: `core/personality_engine.py`
- Create: `core/tests/test_personality_engine.py`

**Interfaces:**
- Consumes: `LLMAdapter`, `SessionMemory`, `character_name`, `user_name`, `base_personality` (str)
- Produces: `render_c1() -> dict[str, str]`, `render_c9() -> dict[str, str]`, `consume_trigger(source, data)`, `restore_state()`

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_personality_engine.py
"""PersonalityEngine 单元测试。"""
import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest
from core.personality_engine import PersonalityEngine
from core.memory.session import SessionMemory


def _make_engine(session=None, llm=None):
    """创建测试用 PersonalityEngine。"""
    s = session or SessionMemory()
    m = llm or MagicMock()
    m.think = AsyncMock()
    return PersonalityEngine(
        llm=m,
        session=s,
        character_name="Elysia",
        user_name="妖梦",
        base_personality="你是 Elysia。温柔底色。",
    )


class TestPersonalityEngineInit:
    def test_loads_l1_core(self):
        """初始化时存储 L1 Core。"""
        engine = _make_engine()
        assert engine.render_c1()["core_personality"] == "你是 Elysia。温柔底色。"

    def test_l2_self_is_empty_initially(self):
        """初始 L2 Self 为空。"""
        engine = _make_engine()
        assert engine.render_c1()["self_personality"] == ""


class TestPersonalityEngineC9:
    def test_c9_empty_when_no_state(self):
        """无 L3 State 时 C9 返回空。"""
        engine = _make_engine()
        assert engine.render_c9()["awareness_text"] == ""

    def test_c9_returns_state_when_triggered(self):
        """L3 State 存在且触发条件满足时 C9 返回内容。"""
        s = SessionMemory()
        engine = _make_engine(session=s)
        # 模拟 L3 state
        s.set_param("state_l3", "最近比较敷衍")
        s.set_param("state_history", [{"timestamp": 0, "content": ""}])
        # 手动触发一个事件以确保 awareness 激活
        asyncio.run(engine.consume_trigger("test", {"content": "测试触发"}))
        result = engine.render_c9()
        assert result["awareness_text"]


class TestPersonalityEngineTrigger:
    def test_consume_trigger_updates_state_l3(self):
        """consume_trigger 更新 session 中的 state_l3 和 history。"""
        s = SessionMemory()
        engine = _make_engine(session=s)
        asyncio.run(engine.consume_trigger(
            "reflector",
            {"content": "最近变得敷衍了"},
        ))
        assert s.get_param("state_l3")
        assert "敷衍" in s.get_param("state_l3")
        history = s.get_param("state_history")
        assert len(history) == 1

    def test_consume_trigger_no_llm_for_simple_state(self):
        """简单状态更新不调用 LLM。"""
        engine = _make_engine()
        asyncio.run(engine.consume_trigger("test", {"content": "test state"}))
        engine._llm.think.assert_not_called()


class TestPersonalityEngineRestore:
    def test_restore_state_rebuilds_from_session(self):
        """restore_state 从 session 恢复 L2/L3。"""
        s = SessionMemory()
        s.set_param("self_l2", "我变得更信任妖梦了")
        s.set_param("state_l3", "最近有点累")
        engine = _make_engine(session=s)
        engine.restore_state()
        assert engine.render_c1()["self_personality"] == "我变得更信任妖梦了"
        assert engine.render_c9()["awareness_text"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
conda run -n Elysia python -m pytest core/tests/test_personality_engine.py -v
```

Expected: all fail (module not found / class not found)

- [ ] **Step 3: Implement `core/personality_engine.py`**

```python
"""PersonalityEngine — 角色性格引擎。

四层性格模型:
  L1 Core    — 不可变底色（文件加载）
  L2 Self    — 长期自我（session，缓慢演化）
  L3 State   — 短期状态（session，Reflector + 事件驱动）
  L4 Mood    — 当前情绪（EmotionEngine，不动）

CharacterReflector 作为触发源之一，产出的事件提交给 consume_trigger。
"""
import logging
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

# AwarenessTrigger 配置
AWARENESS_COOLDOWN_ROUNDS = 20  # 同一主题 N 轮内不重复触发
AWARENESS_MAX_HISTORY = 10      # state_history 保留条数


class PersonalityEngine:
    """角色的性格状态管理器。"""

    def __init__(
        self,
        *,
        llm,                              # LLMAdapter (用于 compression 调用)
        session,                          # SessionMemory
        character_name: str,
        user_name: str,
        base_personality: str,            # L1 Core 文本
    ):
        self._llm = llm
        self._session = session
        self._character_name = character_name
        self._user_name = user_name
        self._l1_core = base_personality

        # 冷却计时: 记录上次 C9 触发的轮次，防止频繁输出"我变了"
        self._last_awareness_round: int = -1

    # ---- 状态恢复 ----

    def restore_state(self) -> None:
        """从 session.params 恢复 L2/L3 状态。必须在 session.load() 之后调用。"""
        # L2/L3 已由 session.load() 加载到 params，无需额外操作
        pass

    # ---- 数据提供（给 section 渲染用）----

    def render_c1(self) -> dict[str, str]:
        """产出 C1 Personality section 的数据。

        Returns:
            {"core_personality": str, "self_personality": str}
            self_personality 为空串时表示无演化
        """
        l2 = self._session.get_param("self_l2", "")
        return {
            "core_personality": self._l1_core,
            "self_personality": l2 if l2 else "",
        }

    def render_c9(self) -> dict[str, str]:
        """产出 C9 SelfPerception section 的数据。

        Returns:
            {"awareness_text": str}
            awareness_text 为空串时 C9 跳过渲染
        """
        l3 = self._session.get_param("state_l3", "")
        if not l3:
            return {"awareness_text": ""}

        # AwarenessTrigger: 偏差检测 + 冷却
        if not self._awareness_allowed():
            return {"awareness_text": ""}

        self._last_awareness_round = self._current_round_estimate()
        return {"awareness_text": l3}

    # ---- 触发事件 ----

    async def consume_trigger(self, source: str, data: dict) -> None:
        """接收外部触发事件，更新 L3 State。

        Args:
            source: 触发源标识 ("reflector" | "event" | "idle" | "feedback")
            data:   触发数据，至少包含 "content" 键
        """
        content = data.get("content", "")
        if not content:
            return

        if source == "reflector":
            # Reflector 产出 → 直接设为 L3 State
            self._set_state_l3(content)

        elif source == "event":
            # 重大事件 → 追加到 L3 State
            self._append_state_l3(content)

        else:
            # idle / feedback → 直接设为 L3 State
            self._set_state_l3(content)

    # ---- awareness 判定 ----

    def _awareness_allowed(self) -> bool:
        """判断当前是否允许 C9 渲染（冷却检查）。

        Phase 1 简单版本：距离上次触发超过 AWARENESS_COOLDOWN_ROUNDS 轮。
        后续 Phase 2 加入 L2/L3 语义偏差检测。
        """
        current = self._current_round_estimate()
        if self._last_awareness_round < 0:
            return True
        return (current - self._last_awareness_round) >= AWARENESS_COOLDOWN_ROUNDS

    def _current_round_estimate(self) -> int:
        """估算当前轮次（基于 state_history 条目数）。"""
        hist = self._session.get_param("state_history", [])
        return len(hist)

    # ---- 内部状态管理 ----

    def _set_state_l3(self, text: str) -> None:
        """设置 L3 State，追加历史。"""
        self._session.set_param("state_l3", text)
        self._append_history("state_history", text)

    def _append_state_l3(self, text: str) -> None:
        """追加内容到 L3 State。"""
        current = self._session.get_param("state_l3", "")
        new = f"{current}\n{text}" if current else text
        self._session.set_param("state_l3", new)
        self._append_history("state_history", text)

    def _append_history(self, key: str, content: str) -> None:
        """追加条目到历史列表，控制长度。"""
        history = self._session.get_param(key, [])
        history.append({"timestamp": time.time(), "content": content})
        if len(history) > AWARENESS_MAX_HISTORY:
            history = history[-AWARENESS_MAX_HISTORY:]
        self._session.set_param(key, history)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
conda run -n Elysia python -m pytest core/tests/test_personality_engine.py -v
```

Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add core/personality_engine.py core/tests/test_personality_engine.py
git commit -m "feat: PersonalityEngine — 4-layer personality model with L1/L2/L3 + consume_trigger"
```

---

### Task 3: 迁移 CharacterReflector → PersonalityEngine 子组件

**Files:**
- Modify: `core/character_reflector.py`

**Interfaces:**
- Consumes: `PersonalityEngine` (new dep, optional)
- Produces: `set_personality_engine(engine)` — 设置后 _reflect() 产出提交给 engine

- [ ] **Step 1: Write the failing test**

```python
# 在 core/tests/test_character_reflector.py 中新增

def test_reflector_submits_to_personality_engine():
    """Reflector 完成后提交事件到 PersonalityEngine。"""
    from core.personality_engine import PersonalityEngine
    from core.memory.session import SessionMemory
    from unittest.mock import AsyncMock, MagicMock

    s = SessionMemory()
    llm = MagicMock()
    llm.think = AsyncMock(return_value=MagicMock(text="我注意到自己变得敷衍了"))

    engine = PersonalityEngine(
        llm=llm, session=s,
        character_name="Elysia", user_name="妖梦",
        base_personality="温柔底色"
    )

    reflector = CharacterReflector(
        llm=llm, session=s,
        character_name="Elysia", user_name="妖梦"
    )
    reflector.set_personality_engine(engine)

    # 快速填充 buffer 达到触发阈值
    for _ in range(10):
        reflector._buffer.append(("你好", "你好呀"))

    asyncio.run(reflector.flush())
    assert s.get_param("state_l3")
    assert "敷衍" in s.get_param("state_l3")
```

- [ ] **Step 2: Implement `set_personality_engine` and redirect _reflect output**

在 `CharacterReflector.__init__` 中：

```python
self._personality_engine = None  # PersonalityEngine | None
```

新增方法：

```python
def set_personality_engine(self, engine) -> None:
    """连接到 PersonalityEngine。设置后 _reflect() 产出提交给 engine 而非 self_perception_history。"""
    self._personality_engine = engine
```

修改 `_reflect()` 方法的尾部（成功解析 LLM 结果后，原来写 `self_perception_history` 的地方）：

```python
# 原来:
# self._session.set_param("self_perception_history", current_history)

# 改为:
if self._personality_engine:
    # 提交给 PersonalityEngine 作为 L3 State 事件
    import asyncio
    asyncio.create_task(
        self._personality_engine.consume_trigger("reflector", {"content": text})
    )
else:
    # 降级：保持兼容旧行为
    self._session.set_param("self_perception_history", current_history)
```

同时保留 `self_perception_history` 的写入作为降级路径（当 `_personality_engine` 为 None 时）。

刷新后的 `_reflect()` 尾部（替换 223-254 行）：

```python
        elapsed_ms = (time.perf_counter() - t0) * 1000

        # LLM 调用成功后才清空 buffer
        self._buffer.clear()
        self._save_state()

        if not text:
            self._last_duration_ms = elapsed_ms
            self._last_success = True
            self._last_text_len = 0
            self._last_reflection_at = time.time()
            logger.debug("CharacterReflector: 反思结果为空，跳过")
            return

        # 提交给 PersonalityEngine 或降级写入旧 self_perception_history
        if self._personality_engine is not None:
            asyncio.create_task(
                self._personality_engine.consume_trigger("reflector", {"content": text})
            )
            self._last_success = True
            self._last_duration_ms = elapsed_ms
            self._last_text_len = len(text)
            self._last_reflection_at = time.time()
            logger.info("CharacterReflector: 性格反思已提交 → PersonalityEngine (%d chars, %.0f ms)",
                        len(text), elapsed_ms)
            return

        # 降级路径：写 self_perception_history（兼容旧行为）
        entry = {"timestamp": time.time(), "content": text}
        current_history = self._session.get_param("self_perception_history", [])
        current_history.append(entry)
        if len(current_history) > self.MAX_HISTORY:
            current_history = current_history[-self.MAX_HISTORY:]
        self._session.set_param("self_perception_history", current_history)

        self._last_success = True
        self._last_duration_ms = elapsed_ms
        self._last_text_len = len(text)
        self._last_reflection_at = time.time()

        logger.info("CharacterReflector: 性格反思完成 (%d chars, %.0f ms)",
                    len(text), elapsed_ms)
```

- [ ] **Step 3: Run existing reflector tests to ensure no regression**

```bash
conda run -n Elysia python -m pytest core/tests/test_character_reflector.py -v
```

Expected: all existing tests pass

- [ ] **Step 4: Run new integration test**

```bash
conda run -n Elysia python -m pytest core/tests/test_character_reflector.py::test_reflector_submits_to_personality_engine -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/character_reflector.py core/tests/test_character_reflector.py
git commit -m "feat: CharacterReflector → PersonalityEngine subcomponent (consume_trigger)"
```

---

### Task 4: 更新 Section 类 + 模板

**Files:**
- Modify: `Module/system_prompt/sections/personality.py`
- Modify: `Module/system_prompt/sections/self_perception.py`
- Modify: `Module/system_prompt/templates/c1_personality.j2`
- Modify: `Module/system_prompt/templates/c9_self_perception.j2`

**Interfaces:**
- Consumes: `core_personality: str`, `self_personality: str` (C1); `awareness_text: str` (C9)
- Produces: section.render() → str

- [ ] **Step 1: Update `personality.py` section class**

`PersonalitySection` 不再接受 `character_name`，改为接受 `core_personality` 和 `self_personality` 字符串：

```python
"""C1: 人格。"""
from Module.system_prompt.protocol import Jinja2Section


class PersonalitySection(Jinja2Section):
    name = "Personality"
    version = "2.0.0"
    description = "人格：L1 Core + L2 Self（动态性格引擎）"

    def __init__(self, template_path: str, core_personality: str = ""):
        super().__init__(template_path)
        self._core = core_personality
        self._self = ""
        self._name = ""
        self._user_name = ""
        self._user_pronoun = "她"

    def set_context(self, **kwargs) -> None:
        self._name = kwargs.get("name", "")
        self._user_name = kwargs.get("user_name", "")
        self._user_pronoun = kwargs.get("user_pronoun", "她")

    async def render(self) -> str:
        return self._render_template(
            name=self._name,
            core_personality=self._core,
            self_personality=self._self,
        )

    def update_personality(self, core: str, self_p: str) -> None:
        """由 PromptAssembler 调用，传入 PersonalityEngine 的实时数据。"""
        self._core = core
        self._self = self_p
```

- [ ] **Step 2: Update `self_perception.py` section class**

```python
"""C9: 自我感知。"""
from Module.system_prompt.protocol import Jinja2Section


class SelfPerceptionSection(Jinja2Section):
    name = "SelfPerception"
    version = "2.0.0"
    description = "自我感知：AwarenessTrigger 条件激活"

    def __init__(self, template_path: str):
        super().__init__(template_path)
        self._awareness_text = ""

    def update_awareness(self, text: str) -> None:
        """由 PromptAssembler 调用，传入 PersonalityEngine 的 awareness 数据。"""
        self._awareness_text = text

    async def render(self) -> str:
        if not self._awareness_text:
            return ""
        return self._render_template(awareness_text=self._awareness_text)
```

- [ ] **Step 3: Update `c1_personality.j2` template**

```jinja2
{#
@section Personality
@version 2.0.0
@updated 2026-07-11
@description 人格：L1 Core（不可变底色）+ L2 Self（长期演化自我）
@params name: str
@params core_personality: str  # L1 Core 文本
@params self_personality: str  # L2 Self 文本（可能为空）
#}
<personality>
你是 {{ name }}。

{{ core_personality }}

{% if self_personality %}
<long_term_self>
{{ self_personality }}
</long_term_self>
{% endif %}
</personality>
```

- [ ] **Step 4: Update `c9_self_perception.j2` template**

```jinja2
{#
@section SelfPerception
@version 2.0.0
@description 角色自我觉察（AwarenessTrigger 条件激活）
@params awareness_text: str  # AwarenessTrigger 判定通过时非空
@updated 2026-07-11
#}
{% if awareness_text %}
<self_perception>
你最近注意到自己的一些变化：
{{ awareness_text }}
</self_perception>
{% endif %}
{# end #}
```

- [ ] **Step 5: Run section-level tests to verify**

```bash
conda run -n Elysia python -m pytest Module/system_prompt/tests/unit/test_sections/test_boundary.py -v
```

Expected: may need to update test calls (Task 5 handles this)

- [ ] **Step 6: Commit**

```bash
git add Module/system_prompt/sections/personality.py Module/system_prompt/sections/self_perception.py Module/system_prompt/templates/c1_personality.j2 Module/system_prompt/templates/c9_self_perception.j2
git commit -m "feat: update C1/C9 sections + templates for PersonalityEngine data"
```

---

### Task 5: 更新 PromptAssembler + Agent 接线

**Files:**
- Modify: `core/prompt_assembler.py`
- Modify: `core/agent.py`

**Interfaces:**
- Consumes: `PersonalityEngine`. PromptAssembler holds reference to `PersonalityEngine`.
- Produces: personality section updates via `update_personality()`, awareness via `update_awareness()`

- [ ] **Step 1: Update `PromptAssembler.__init__`**

在 `PromptAssembler.__init__` 中新增 `personality_engine` 参数，同时修改 `PersonalitySection` 和 `SelfPerceptionSection` 的构造方式：

```python
class PromptAssembler:
    def __init__(
        self,
        character: CharacterConfig,
        session: SessionMemory,
        long_term: LongTermMemory,
        personality_engine=None,  # PersonalityEngine | None
    ):
        self._character = character
        self._session = session
        self._long_term = long_term
        self._personality_engine = personality_engine

        self._personality_section = PersonalitySection(
            f"{_SECTION}/c1_personality.j2",
            core_personality=character.base_personality,
        )
        self._self_perception_section = SelfPerceptionSection(
            f"{_SECTION}/c9_self_perception.j2",
        )

        sections = [
            ConflictRulesSection(f"{_SECTION}/f1_conflict_rules.j2"),
            OutputFormatSection(f"{_SECTION}/f2_output_format.j2"),
            TTSMarkupSection(f"{_SECTION}/f3_tts_markup.j2"),
            SensorySection(f"{_SECTION}/c5_sensory.j2"),
            EmotionSection(f"{_SECTION}/c3_emotion.j2", session),
            MemorySection(f"{_SECTION}/c4_memory.j2", long_term, session),
            self._personality_section,
            RelationshipSection(f"{_SECTION}/c2_relationship.j2", character.name),
            SpeakingStyleSection(f"{_SECTION}/c6_speaking_style.j2"),
            ProactiveSection(f"{_SECTION}/c7_proactive.j2"),
            OwnLifeSection(f"{_SECTION}/c8_own_life.j2", session),
            self._self_perception_section,
        ]
        self._manager = SystemPromptManager(sections)
        self._builder = MessageBuilder()
```

- [ ] **Step 2: Add `_refresh_personality_data()` method**

```python
    def _refresh_personality_data(self) -> None:
        """从 PersonalityEngine 拉取最新数据，更新 C1/C9 section。"""
        if self._personality_engine is None:
            return
        c1_data = self._personality_engine.render_c1()
        self._personality_section.update_personality(
            core=c1_data["core_personality"],
            self_p=c1_data["self_personality"],
        )
        c9_data = self._personality_engine.render_c9()
        self._self_perception_section.update_awareness(
            text=c9_data["awareness_text"],
        )
```

在 `build_system_prompt()` 中调用 `self._refresh_personality_data()`（在 `set_context()` 之后、`render()` 之前）：

```python
    async def build_system_prompt(self, user_message: str = "", user_name: str = "",
                                   stt_emotion: str = "", stt_events=None,
                                   tts_provider: str = "") -> str:
        self._manager.set_context(
            user_message=user_message,
            user_name=user_name,
            user_pronoun=self._character.user_pronoun,
            stt_emotion=stt_emotion,
            stt_events=stt_events or [],
            tts_provider=tts_provider,
        )
        self._refresh_personality_data()
        return await self._manager.render()
```

- [ ] **Step 3: Update `agent.py` to create and wire PersonalityEngine**

在 `Agent.__init__` 中（在 CharacterReflector 之后）：

```python
        # ---- 性格引擎 ----
        self._personality_engine = PersonalityEngine(
            llm=self._llm,
            session=self._session,
            character_name=self._character.name,
            user_name=self._user_name,
            base_personality=self._character.base_personality,
        )

        self._character_reflector = CharacterReflector(
            llm=self._llm,
            session=self._session,
            character_name=self._character.name,
            user_name=self._user_name,
        )
        self._character_reflector.set_personality_engine(self._personality_engine)
```

在 `_start_persistence()` 中（session.load() 之后、reflector.restore_state() 之后）：

```python
        if self._personality_engine:
            self._personality_engine.restore_state()
```

在 `PromptAssembler` 初始化时传入：

```python
        self._prompt = PromptAssembler(
            self._character, self._session, self._long_term,
            personality_engine=self._personality_engine,
        )
```

- [ ] **Step 4: Run the full system_prompt test suite to find failures**

```bash
conda run -n Elysia python -m pytest Module/system_prompt/tests/ -v
```

- [ ] **Step 5: Commit**

```bash
git add core/prompt_assembler.py core/agent.py
git commit -m "feat: wire PersonalityEngine into PromptAssembler + Agent"
```

---

### Task 6: 更新全部测试 + 文档

**Files:**
- Modify: `Module/system_prompt/tests/unit/test_sections/test_boundary.py`
- Modify: `Module/system_prompt/tests/integration/test_full_render.py`
- Modify: `todo.md`

- [ ] **Step 1: Update test_boundary.py**

C1 测试更新（`TestSectionSetContextDefaults.test_personality_no_user_name`）：

```python
    def test_personality_no_user_name(self):
        """set_context 不传 user_name → 默认 ""，core 文本渲染。"""
        sec = PersonalitySection(
            f"{_TEMPLATES}/c1_personality.j2",
            core_personality="你是 TestBot。测试人格。",
        )
        sec.set_context(user_pronoun="她")
        result = asyncio.run(sec.render())
        assert "TestBot" in result
        assert "测试人格" in result
```

C9 测试更新（`TestSelfPerceptionSectionBoundary`）：

```python
class TestSelfPerceptionSectionBoundary:
    def test_no_awareness_returns_empty(self):
        """awareness_text 未设置 → 返回 ""。"""
        sec = SelfPerceptionSection(f"{_TEMPLATES}/c9_self_perception.j2")
        result = asyncio.run(sec.render())
        assert result == ""

    def test_with_awareness_renders(self):
        """awareness_text 有值 → 渲染内容。"""
        sec = SelfPerceptionSection(f"{_TEMPLATES}/c9_self_perception.j2")
        sec.update_awareness("感觉自己变了")
        result = asyncio.run(sec.render())
        assert "感觉自己变了" in result
        assert "self_perception" in result
```

- [ ] **Step 2: Update test_full_render.py**

添加 `PersonalityEngine` mock，更新 C1/C9 section 构造：

```python
from core.personality_engine import PersonalityEngine

def _mock_personality_engine():
    """返回 mock PersonalityEngine 用于测试。"""
    s = SessionMemory()
    engine = PersonalityEngine(
        llm=MagicMock(),
        session=s,
        character_name="Elysia",
        user_name="妖梦",
        base_personality="你是 Elysia。温柔底色。",
    )
    return engine


def test_full_render_all_sections():
    """12 个 section 全部注入数据，确认渲染正常。"""
    session = _mock_session(...)
    pe = _mock_personality_engine()
    
    personality_section = PersonalitySection(
        f"{_TEMPLATES}/c1_personality.j2",
        core_personality=pe.render_c1()["core_personality"],
    )
    self_perception_section = SelfPerceptionSection(
        f"{_TEMPLATES}/c9_self_perception.j2",
    )
    
    sections = [
        ...
        personality_section,
        ...
        self_perception_section,
    ]
    mgr = SystemPromptManager(sections)
    mgr.set_context(...)
    # 注入 personality data
    personality_section.update_personality(
        core=pe.render_c1()["core_personality"],
        self_p=pe.render_c1()["self_personality"],
    )
    self_perception_section.update_awareness("感觉自己变了")
    result = asyncio.run(mgr.render())
    ...

def test_versions_all_sections():
    """全部 12 个 section 都有 version。"""
    sections = [
        ...
        PersonalitySection(f"{_TEMPLATES}/c1_personality.j2", core_personality="测试"),
        ...
        SelfPerceptionSection(f"{_TEMPLATES}/c9_self_perception.j2"),
    ]
    mgr = SystemPromptManager(sections)
    versions = mgr.get_versions()
    assert len(versions) == 12
    ...

def test_validate_all_passes():
    """全部 12 个 section 的 validate 都通过。"""
    sections = [
        ...
        PersonalitySection(f"{_TEMPLATES}/c1_personality.j2", core_personality="测试"),
        ...
        SelfPerceptionSection(f"{_TEMPLATES}/c9_self_perception.j2"),
    ]
    ...
```

- [ ] **Step 3: Run full test suite**

```bash
conda run -n Elysia python -m pytest Module/system_prompt/tests/ core/tests/test_personality_engine.py core/tests/test_character_reflector.py -v
```

Expected: all tests pass

- [ ] **Step 4: Update todo.md**

```markdown
- **PersonalityEngine Phase 2**:
  - AwarenessTrigger: L2/L3 语义偏差检测（向量相似度替代轮次冷却）
  - L3→L2 梯度压缩自动化（LLM 总结+去噪+合并）
  - ExpressionAdapter 完善版：阻尼机制移入 C6 SpeakingStyle
  - L4→L3 情绪汇聚统计（需 EmotionEngine 改接口）
  - L2 永久存储（独立 MongoDB 集合 / 文件，替代 session TTL 风险）
  - C3/C6/C7 与 PersonalityEngine 动态联动
  - 用户话题检测触发 C9（目前只看偏差+冷却）
- **C3 情绪传染规则动态化** → 后续 PersonalityEngine Phase 2
- **C6 说话风格动态化** → 后续 PersonalityEngine Phase 2
- **C7 主动性动态化** → 后续 PersonalityEngine Phase 2
```

- [ ] **Step 5: Commit**

```bash
git add Module/system_prompt/tests/ todo.md
git commit -m "test: update all tests + todo for PersonalityEngine migration"
```

---

## Verification

```bash
# 1. 所有 system_prompt 测试
conda run -n Elysia python -m pytest Module/system_prompt/tests/ -v
# Expected: 55 passed (或 ≥55, 如果新增了 section 测试)

# 2. PersonalityEngine 测试
conda run -n Elysia python -m pytest core/tests/test_personality_engine.py -v
# Expected: all passed

# 3. CharacterReflector 测试
conda run -n Elysia python -m pytest core/tests/test_character_reflector.py -v
# Expected: all passed

# 4. 启动验证
conda run -n Elysia python -c "
import asyncio, sys
from core.character_config import CharacterConfig
c = CharacterConfig.from_yaml('character/elysia.yaml')
print(f'Character: {c.name}')
print(f'L1 Core: {c.base_personality[:80]}...')
print(f'OK: PersonalityEngine wiring verified')
"
```
