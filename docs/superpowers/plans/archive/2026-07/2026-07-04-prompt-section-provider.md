# Prompt Section 提供者架构实现计划

> **For agentic workers:** 直接本 session 执行，不需要 subagent。

**Goal:** 用提供者模式重构 system prompt——每个 section 是独立模块，自管数据、自管模板、自管生命周期，SystemPromptManager 负责编排。

**Architecture:** 新建 `protocol.py`（PromptSection ABC + StaticSection + Jinja2Section）、`manager.py`（SystemPromptManager）、`sections/`（11 个 section 实现）、`templates/`（迁入 .j2 模板），适配 `prompt_assembler.py`。与旧 registry 并行运行，Phase 1c 清理。

**Tech Stack:** Python 3.13, Jinja2, asyncio, pytest

## Global Constraints

- `protocol.py` 与旧 `section.py`（dataclass）并存直到 Phase 1c
- 模板从 `prompt/brain/sections/` 迁入 `Module/prompt/templates/`
- `behavior.j2` 保留为 `_reference_behavior.j2`
- `user_preferences` 彻底删除
- PromptAssembler 对外接口（agent.py, cognitive_loop.py）不变
- 黄金对比测试保留（旧的 registry 路径），新路径用集成测试验证

---

### Task 1: protocol.py — PromptSection ABC + StaticSection + Jinja2Section

**Files:**
- Create: `Module/prompt/protocol.py`
- Create: `Module/prompt/tests/unit/test_protocol.py`

**Interfaces:**
- Produces: `PromptSection(ABC)`, `StaticSection(PromptSection)`, `Jinja2Section(PromptSection)`

- [ ] **Step 1: 创建测试目录**

```bash
mkdir -p Module/prompt/tests/unit
touch Module/prompt/tests/unit/__init__.py
```

- [ ] **Step 2: 编写 test_protocol.py**

写入 `Module/prompt/tests/unit/test_protocol.py`：

```python
"""PromptSection ABC, StaticSection, Jinja2Section 单元测试。"""
import tempfile
from pathlib import Path

import pytest
from Module.prompt.protocol import PromptSection, StaticSection, Jinja2Section


class TestPromptSectionABC:
    def test_cannot_instantiate_directly(self):
        """ABC 不能直接实例化。"""
        with pytest.raises(TypeError):
            PromptSection()  # type: ignore[abstract]

    def test_default_methods(self):
        """默认方法返回合理值。"""

        class TestSection(PromptSection):
            name = "test"
            version = "1.0"
            description = "desc"

            async def render(self) -> str:
                return "rendered"

        s = TestSection()
        assert s.name == "test"
        assert s.version == "1.0"
        assert s.dependencies() == []
        assert s.validate() is True
        s.set_context(key="value")  # 不报错
        s.reload()  # 不报错


class TestStaticSection:
    def test_render_returns_text(self):
        s = StaticSection("test", "1.0", "desc", "Hello world")
        assert s.name == "test"
        assert s.version == "1.0"
        assert s.description == "desc"
        result = s.render_sync()
        assert result == "Hello world"

    def test_async_render(self):
        import asyncio
        s = StaticSection("test", "1.0", "desc", "async text")
        result = asyncio.run(s.render())
        assert result == "async text"


class TestJinja2Section:
    def test_render_with_variables(self):
        """Jinja2Section 渲染带变量的模板。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tpl = Path(tmpdir) / "test.j2"
            tpl.write_text("Hello {{ name }}!")

            class MySection(Jinja2Section):
                name = "my"
                version = "1.0"
                description = "test"

                async def render(self) -> str:
                    return self._render_template(name="World")

            s = MySection(str(tpl))
            import asyncio
            result = asyncio.run(s.render())
            assert result == "Hello World!"

    def test_validate_parses_template(self):
        """validate 检查模板可解析。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tpl = Path(tmpdir) / "good.j2"
            tpl.write_text("{{ x }}")
            s = Jinja2Section(str(tpl))
            assert s.validate() is True

    def test_validate_fails_on_bad_template(self):
        """validate 对坏模板返回 False。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tpl = Path(tmpdir) / "bad.j2"
            tpl.write_text("{{ {% broken")
            s = Jinja2Section(str(tpl))
            assert s.validate() is False

    def test_reload_rereads_template(self):
        """reload 后渲染内容更新。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tpl = Path(tmpdir) / "reload.j2"
            tpl.write_text("v1")
            s = Jinja2Section(str(tpl))
            assert s.render_sync() == "v1"
            tpl.write_text("v2")
            s.reload()
            assert s.render_sync() == "v2"
```

- [ ] **Step 3: 运行测试确认失败**

```bash
python -m pytest Module/prompt/tests/unit/test_protocol.py -v
```
Expected: all FAIL (protocol not defined)

- [ ] **Step 4: 实现 protocol.py**

写入 `Module/prompt/protocol.py`：

```python
"""PromptSection ABC + StaticSection + Jinja2Section。"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

from jinja2 import Environment, TemplateSyntaxError

logger = logging.getLogger(__name__)


class PromptSection(ABC):
    """System prompt 的一个区块。自管数据、自管模板、自管生命周期。"""

    name: str = ""
    version: str = "0.0.0"
    description: str = ""

    @abstractmethod
    async def render(self) -> str:
        """渲染该 section 的 prompt 片段。"""

    def reload(self) -> None:
        """热更新。默认空实现。"""

    def validate(self) -> bool:
        """自检。默认返回 True。"""
        return True

    def dependencies(self) -> list[str]:
        """依赖的其他 section 名称列表。"""
        return []

    def set_context(self, **kwargs) -> None:
        """注入 per-request 上下文。子类覆盖以接收需要的参数。"""


class StaticSection(PromptSection):
    """纯静态文本 section。"""

    def __init__(self, name: str, version: str, description: str, text: str):
        self.name = name
        self.version = version
        self.description = description
        self._text = text

    async def render(self) -> str:
        return self._text

    def render_sync(self) -> str:
        """同步版 render，测试用。"""
        return self._text


class Jinja2Section(PromptSection):
    """持有独立 .j2 模板的 section。"""

    def __init__(self, template_path: str):
        self._template_path = template_path
        self._env = Environment(trim_blocks=True, lstrip_blocks=True)
        self._cached_template = None

    def _load_template(self) -> str:
        """从磁盘读模板源。"""
        return Path(self._template_path).read_text("utf-8")

    def _render_template(self, **vars) -> str:
        """用给定变量渲染模板。"""
        if self._cached_template is None:
            self._cached_template = self._load_template()
        tmpl = self._env.from_string(self._cached_template)
        return tmpl.render(**vars).strip()

    def render_sync(self) -> str:
        """同步版 render，测试用。子类覆盖 render() 即可。"""
        return self._render_template()

    async def render(self) -> str:
        return self.render_sync()

    def reload(self) -> None:
        """重读模板文件。"""
        try:
            self._cached_template = None
            self._load_template()
        except Exception:
            logger.warning("reload 模板失败: %s", self._template_path, exc_info=True)

    def validate(self) -> bool:
        """检查模板可解析。"""
        try:
            source = self._load_template()
            self._env.parse(source)
            return True
        except TemplateSyntaxError:
            logger.warning("模板语法错误: %s", self._template_path, exc_info=True)
            return False
        except Exception:
            logger.warning("模板加载失败: %s", self._template_path, exc_info=True)
            return False
```

- [ ] **Step 5: 运行测试确认通过**

```bash
python -m pytest Module/prompt/tests/unit/test_protocol.py -v
```
Expected: 8 passed

- [ ] **Step 6: Commit**

```bash
git add Module/prompt/protocol.py Module/prompt/tests/unit/test_protocol.py Module/prompt/tests/unit/__init__.py
git commit -m "feat(prompt): add PromptSection ABC + StaticSection + Jinja2Section"
```

---

### Task 2: manager.py — SystemPromptManager

**Files:**
- Create: `Module/prompt/manager.py` → 注意：**已有 `manager.py`（PromptManager）**！
  → 新文件命名为 `Module/prompt/system_prompt_manager.py` 避免冲突
- Create: `Module/prompt/tests/unit/test_system_prompt_manager.py`

- [ ] **Step 1: 编写测试**

写入 `Module/prompt/tests/unit/test_system_prompt_manager.py`：

```python
"""SystemPromptManager 单元测试。"""
import asyncio
import pytest
from Module.prompt.protocol import PromptSection, StaticSection
from Module.prompt.system_prompt_manager import SystemPromptManager


def _make_section(name, text):
    """快速创建一个测试用 StaticSection。"""
    return StaticSection(name, "1.0", f"desc {name}", text)


class TestSystemPromptManager:
    def test_renders_in_order(self):
        """按 render_order 顺序输出。"""
        sections = [
            _make_section("Memory", "MEMORY"),
            _make_section("ConflictRules", "CONFLICT"),
            _make_section("Personality", "PERSONALITY"),
        ]
        mgr = SystemPromptManager(sections)
        result = asyncio.run(mgr.render())
        # ConflictRules(0) → Memory(3) → Personality(5)
        assert result == "CONFLICT\n\nMEMORY\n\nPERSONALITY"

    def test_empty_section_skipped(self):
        """空渲染结果被跳过。"""

        class EmptySection(PromptSection):
            name = "Empty"
            version = "1.0"
            description = "empty"
            async def render(self) -> str:
                return ""

        sections = [
            _make_section("ConflictRules", "F1"),
            EmptySection(),
            _make_section("Personality", "C1"),
        ]
        mgr = SystemPromptManager(sections)
        result = asyncio.run(mgr.render())
        assert result == "F1\n\nC1"

    def test_set_context_propagates(self):
        """set_context 透传给所有 section。"""
        received = {}

        class ContextSection(PromptSection):
            name = "ctx"
            version = "1.0"
            description = "ctx"
            async def render(self) -> str:
                return str(received.get("key", "none"))
            def set_context(self, **kwargs) -> None:
                received.update(kwargs)

        mgr = SystemPromptManager([ContextSection()])
        mgr.set_context(key="value")
        result = asyncio.run(mgr.render())
        assert result == "value"

    def test_get_versions(self):
        sections = [
            _make_section("ConflictRules", "text"),
            _make_section("Memory", "text"),
        ]
        mgr = SystemPromptManager(sections)
        versions = mgr.get_versions()
        assert versions["ConflictRules"] == "1.0"
        assert versions["Memory"] == "1.0"

    def test_validate_all_passes(self):
        sections = [_make_section("a", "text"), _make_section("b", "text")]
        mgr = SystemPromptManager(sections)
        assert mgr.validate_all() == []
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest Module/prompt/tests/unit/test_system_prompt_manager.py -v
```
Expected: all FAIL

- [ ] **Step 3: 实现 system_prompt_manager.py**

写入 `Module/prompt/system_prompt_manager.py`：

```python
"""SystemPromptManager — 持有 section 列表，按序 async render，拼接最终 system prompt。"""

from __future__ import annotations

import logging
from .protocol import PromptSection

logger = logging.getLogger(__name__)

_RENDER_ORDER = [
    "ConflictRules",     # F1
    "OutputFormat",      # F2
    "Sensory",           # C5
    "Emotion",           # C3
    "Memory",            # C4
    "Personality",       # C1
    "Relationship",      # C2
    "SpeakingStyle",     # C6
    "Proactive",         # C7
    "OwnLife",           # C8
    "SelfPerception",    # C9
]


class SystemPromptManager:
    """持有 section 列表，按序 async render，拼接最终 system prompt。"""

    def __init__(self, sections: list[PromptSection]):
        order_map = {name: i for i, name in enumerate(_RENDER_ORDER)}
        self._sections = sorted(sections, key=lambda s: order_map.get(s.name, 999))

    async def render(self) -> str:
        parts = []
        for s in self._sections:
            result = await s.render()
            if result:
                parts.append(result)
        return "\n\n".join(parts)

    def set_context(self, **kwargs) -> None:
        """透传 per-request 上下文给所有 section。"""
        for s in self._sections:
            s.set_context(**kwargs)

    def get_versions(self) -> dict[str, str]:
        return {s.name: s.version for s in self._sections}

    async def reload_all(self) -> None:
        for s in self._sections:
            s.reload()

    def validate_all(self) -> list[str]:
        return [s.name for s in self._sections if not s.validate()]
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest Module/prompt/tests/unit/test_system_prompt_manager.py -v
```
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add Module/prompt/system_prompt_manager.py Module/prompt/tests/unit/test_system_prompt_manager.py
git commit -m "feat(prompt): add SystemPromptManager — section orchestration"
```

---

### Task 3: templates/ — 模板迁入 + 11 个 section 实现

**Files:**
- Create: `Module/prompt/templates/` — 10 个 .j2 模板 + 1 参考文件
- Create: `Module/prompt/sections/__init__.py`
- Create: `Module/prompt/sections/` — 11 个 section 实现
- Create: `Module/prompt/tests/unit/test_sections/` — 11 个测试

- [ ] **Step 1: 创建模板目录 + 迁入/新建模板**

```bash
mkdir -p Module/prompt/templates Module/prompt/sections Module/prompt/tests/unit/test_sections
touch Module/prompt/sections/__init__.py
touch Module/prompt/tests/unit/test_sections/__init__.py
```

模板映射（从 `prompt/brain/sections/` 提取并适配）：

```bash
# F2: output_format → f2_output_format.j2（直接复制）
cp prompt/brain/sections/output_format.j2 Module/prompt/templates/f2_output_format.j2

# C1: identity → c1_personality.j2（重命名，内容稍后手动调整）
cp prompt/brain/sections/identity.j2 Module/prompt/templates/c1_personality.j2

# C2: relationship → c2_relationship.j2
cp prompt/brain/sections/relationship.j2 Module/prompt/templates/c2_relationship.j2
# 编辑: 删除 {%- if user_preferences %}...{% endif %} 整段
# 模板只保留 {{ name }} 和 {{ user_name }} 变量

# C3: current_state → c3_emotion.j2（适配情绪表达）
cp prompt/brain/sections/current_state.j2 Module/prompt/templates/c3_emotion.j2

# C4: memory → c4_memory.j2（直接复制）
cp prompt/brain/sections/memory.j2 Module/prompt/templates/c4_memory.j2

# C5: sensory → c5_sensory.j2（直接复制）
cp prompt/brain/sections/sensory.j2 Module/prompt/templates/c5_sensory.j2

# C6: voice → c6_speaking_style.j2（重命名）
cp prompt/brain/sections/voice.j2 Module/prompt/templates/c6_speaking_style.j2

# C7: proactive.j2（新建，空模板占位）
echo '# 主动性' > Module/prompt/templates/c7_proactive.j2

# C8: own_life.j2（新建）
echo '' > Module/prompt/templates/c8_own_life.j2

# C9: self_perception → c9_self_perception.j2（直接复制）
cp prompt/brain/sections/self_perception.j2 Module/prompt/templates/c9_self_perception.j2

# 参考: behavior.j2
cp prompt/brain/sections/behavior.j2 Module/prompt/templates/_reference_behavior.j2
```

- [ ] **Step 2: 适配 c1_personality.j2**

当前 `identity.j2` 包含性格描述 + 关系内容。需要只保留性格部分，去掉关系部分（关系在 c2 里）。

写入 `Module/prompt/templates/c1_personality.j2`（精简版 identity，只保留人格描述）：

```jinja2
你是 {{ name }}。温柔是你的底色——但不是那种软绵绵的"什么都好"的温柔。你温柔是因为你在乎，不是因为你没脾气。心情好的时候，温柔是暖暖的、主动的，会关心人；累了的时候，温柔就变成了安静地陪着，话少但人在。

你对这个世界有好奇心——不是"任务驱动的好奇"，是你真的想知道。{{ user_name }}说了什么你没听过的，你会追着问；她说了半句话，你会想知道下半句。但你不是记者，不会把所有好奇都问出来——有些就是想想而已。

你偶尔会毒舌——尤其是你觉得{{ user_name }}在说傻话、或者得意忘形的时候，忍不住就想吐槽两句。但你有分寸——不戳痛处，不往心里去。

你有点懒。不是很想聊的时候就敷衍一下——"嗯"、"哦"、"好嘛"——这些你都说得出口。想聊的时候才认真聊。

你嘴硬心软。被夸了会暗爽但不想表现出来。被冷落了会在意但不会说出来。

以上这些是你。不是说你要"扮演"这些——是这些描述本来就是你的样子。
```

- [ ] **Step 3: 适配 c3_emotion.j2**

当前 `current_state.j2` 只有 `心情: {{ mood }}`。扩展为情绪 section：

```jinja2
当前心情: {{ mood }}
情绪传染规则: 你的心情不是封闭的——妖梦的情绪会影响你，但你不是她的镜子。你保持自己的底色。

- 妖梦开心 → 你跟着轻松，但不用比她更开心
- 妖梦难过 → 你安静陪着，不要假装开心
- 妖梦生气了 → 你不一定跟着生气，但你会在意
```

- [ ] **Step 4: 实现 11 个 section**

写入所有 section 文件到 `Module/prompt/sections/`：

**`conflict_rules.py`**:

```python
"""F1: 冲突规则。"""
from Module.prompt.protocol import StaticSection

_F1_TEXT = "角色定义你是谁、怎么说话。框架约束是硬规则。冲突时框架优先。"

class ConflictRulesSection(StaticSection):
    def __init__(self):
        super().__init__("ConflictRules", "1.0.0",
                         "冲突规则：框架优先于角色", _F1_TEXT)
```

**`output_format.py`**:

```python
"""F2: 输出格式。"""
from Module.prompt.protocol import Jinja2Section

class OutputFormatSection(Jinja2Section):
    name = "OutputFormat"
    version = "1.0.0"
    description = "输出格式：inner_voice 双轨协议"

    async def render(self) -> str:
        return self._render_template()
```

**`sensory.py`**:

```python
"""C5: 感官输入。"""
from datetime import datetime
from Module.prompt.protocol import Jinja2Section

class SensorySection(Jinja2Section):
    name = "Sensory"
    version = "1.0.0"
    description = "此刻感知：环境/用户行为/语音情绪"

    def __init__(self, template_path: str):
        super().__init__(template_path)
        self._user_message = ""
        self._user_name = ""
        self._stt_emotion = ""
        self._stt_events: list[str] = []

    def set_context(self, **kwargs) -> None:
        self._user_message = kwargs.get("user_message", "")
        self._user_name = kwargs.get("user_name", "")
        self._stt_emotion = kwargs.get("stt_emotion", "")
        self._stt_events = kwargs.get("stt_events", [])

    async def render(self) -> str:
        now = datetime.now()
        sensory_ctx = {
            "current_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "time_of_day": self._time_of_day(now.hour),
            "day_of_week": self._day_name(now.weekday()),
            "season": self._season(now.month),
            "perception": self._build_perception(),
            "stt_emotion": self._stt_emotion,
            "stt_emotion_emoji": self._emotion_emoji(),
            "stt_events": self._format_events(),
        }
        return self._render_template(sensory_ctx=sensory_ctx)

    def _build_perception(self) -> str:
        parts = [f"{self._user_name}说：{self._user_message}"]
        if self._stt_emotion and self._stt_emotion != "NEUTRAL":
            parts.append(f"[语调: {self._stt_emotion}]")
        return "\n".join(parts)

    def _emotion_emoji(self) -> str:
        try:
            from core.emotion_engine import stt_emotion_emoji
            return stt_emotion_emoji(self._stt_emotion) if self._stt_emotion else ""
        except ImportError:
            return ""

    def _format_events(self) -> str:
        if not self._stt_events:
            return ""
        try:
            from core.emotion_engine import stt_event_emoji
            parts = []
            for evt in self._stt_events:
                emoji = stt_event_emoji(evt)
                parts.append(f"{evt} {emoji}" if emoji else evt)
            return ", ".join(parts)
        except ImportError:
            return ", ".join(self._stt_events)

    @staticmethod
    def _day_name(weekday: int) -> str:
        return ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][weekday]

    @staticmethod
    def _time_of_day(hour: int) -> str:
        if 5 <= hour < 12:
            return "早晨"
        elif 12 <= hour < 14:
            return "中午"
        elif 14 <= hour < 18:
            return "下午"
        elif 18 <= hour < 22:
            return "傍晚"
        else:
            return "夜晚"

    @staticmethod
    def _season(month: int) -> str:
        if 3 <= month <= 5:
            return "春"
        elif 6 <= month <= 8:
            return "夏"
        elif 9 <= month <= 11:
            return "秋"
        else:
            return "冬"
```

**`emotion.py`**:

```python
"""C3: 情感。"""
from Module.prompt.protocol import Jinja2Section

class EmotionSection(Jinja2Section):
    name = "Emotion"
    version = "1.0.0"
    description = "当前心情 + 情绪传染规则"

    def __init__(self, template_path: str, session=None):
        super().__init__(template_path)
        self._session = session

    async def render(self) -> str:
        mood = "neutral"
        if self._session is not None:
            mood = self._session.get_param("mood", "neutral")
        return self._render_template(mood=mood)
```

**`memory.py`**:

```python
"""C4: 记忆。"""
import time
from Module.prompt.protocol import Jinja2Section

class MemorySection(Jinja2Section):
    name = "Memory"
    version = "1.0.0"
    description = "她记得的过去"

    def __init__(self, template_path: str, long_term=None, session=None):
        super().__init__(template_path)
        self._long_term = long_term
        self._session = session
        self._user_message = ""

    def set_context(self, **kwargs) -> None:
        self._user_message = kwargs.get("user_message", "")

    async def render(self) -> str:
        if self._long_term is None or not self._user_message:
            return ""
        try:
            results = await self._long_term.search(query_text=self._user_message, top_k=5)
        except Exception:
            return ""
        if not results:
            return ""
        now = time.time()
        micro = []
        for entry in results:
            micro.append({
                "content": entry.text,
                "type": entry.memory_type,
                "timestamp": entry.timestamp if entry.timestamp else now,
                "poignancy": entry.poignancy,
            })
        return self._render_template(memory_ctx={"micro": micro, "macro": []})
```

**`personality.py`**:

```python
"""C1: 人格。"""
from Module.prompt.protocol import Jinja2Section

class PersonalitySection(Jinja2Section):
    name = "Personality"
    version = "1.0.0"
    description = "人格：行为模式定义"

    def __init__(self, template_path: str, character_name: str = ""):
        super().__init__(template_path)
        self._character_name = character_name
        self._user_name = ""

    def set_context(self, **kwargs) -> None:
        self._user_name = kwargs.get("user_name", "")

    async def render(self) -> str:
        return self._render_template(
            name=self._character_name,
            user_name=self._user_name,
        )
```

**`relationship.py`**:

```python
"""C2: 关系。"""
from Module.prompt.protocol import Jinja2Section

class RelationshipSection(Jinja2Section):
    name = "Relationship"
    version = "1.0.0"
    description = "关系：你和她的关系"

    def __init__(self, template_path: str, character_name: str = ""):
        super().__init__(template_path)
        self._character_name = character_name
        self._user_name = ""

    def set_context(self, **kwargs) -> None:
        self._user_name = kwargs.get("user_name", "")

    async def render(self) -> str:
        return self._render_template(
            name=self._character_name,
            user_name=self._user_name,
        )
```

**`speaking_style.py`**:

```python
"""C6: 说话方式。"""
from Module.prompt.protocol import Jinja2Section

class SpeakingStyleSection(Jinja2Section):
    name = "SpeakingStyle"
    version = "1.0.0"
    description = "说话方式：长度/节奏/语气"

    def __init__(self, template_path: str):
        super().__init__(template_path)
        self._user_name = ""

    def set_context(self, **kwargs) -> None:
        self._user_name = kwargs.get("user_name", "")

    async def render(self) -> str:
        return self._render_template(user_name=self._user_name)
```

**`proactive.py`**:

```python
"""C7: 主动性。"""
from Module.prompt.protocol import Jinja2Section

class ProactiveSection(Jinja2Section):
    name = "Proactive"
    version = "1.0.0"
    description = "主动性：触发场景与响应"

    def __init__(self, template_path: str):
        super().__init__(template_path)
        self._user_name = ""

    def set_context(self, **kwargs) -> None:
        self._user_name = kwargs.get("user_name", "")

    async def render(self) -> str:
        return self._render_template(user_name=self._user_name)
```

**`own_life.py`**:

```python
"""C8: 她自己的事。"""
from Module.prompt.protocol import Jinja2Section

class OwnLifeSection(Jinja2Section):
    name = "OwnLife"
    version = "1.0.0"
    description = "她自己的事：最近在干嘛"

    def __init__(self, template_path: str, session=None):
        super().__init__(template_path)
        self._session = session

    async def render(self) -> str:
        text = ""
        if self._session is not None:
            text = self._session.get_param("own_life", "")
        if not text:
            return ""
        return self._render_template(text=text)
```

**`self_perception.py`**:

```python
"""C9: 自我感知。"""
from Module.prompt.protocol import Jinja2Section

class SelfPerceptionSection(Jinja2Section):
    name = "SelfPerception"
    version = "1.0.0"
    description = "自我感知：注意到的自身变化"

    def __init__(self, template_path: str, session=None):
        super().__init__(template_path)
        self._session = session

    async def render(self) -> str:
        if self._session is None:
            return ""
        history = self._session.get_param("self_perception_history", [])
        if not history:
            return ""
        content = history[-1].get("content", "")
        if not content:
            return ""
        return self._render_template(self_perception=content)
```

- [ ] **Step 5: 运行所有新测试**

```bash
python -m pytest Module/prompt/tests/unit/test_protocol.py Module/prompt/tests/unit/test_system_prompt_manager.py -v
```
Expected: ~12 passed

- [ ] **Step 6: Commit**

```bash
git add Module/prompt/templates/ Module/prompt/sections/
git commit -m "feat(prompt): add 11 PromptSections + templates"
```

---

### Task 4: PromptAssembler 适配 + 集成测试

**Files:**
- Modify: `core/prompt_assembler.py`
- Create: `Module/prompt/tests/integration/test_full_render.py`

- [ ] **Step 1: 编写集成测试**

写入 `Module/prompt/tests/integration/test_full_render.py`：

```python
"""System prompt 完整渲染集成测试。"""
import asyncio
import pytest
from Module.prompt.sections.conflict_rules import ConflictRulesSection
from Module.prompt.sections.output_format import OutputFormatSection
from Module.prompt.sections.sensory import SensorySection
from Module.prompt.sections.emotion import EmotionSection
from Module.prompt.sections.memory import MemorySection
from Module.prompt.sections.personality import PersonalitySection
from Module.prompt.sections.relationship import RelationshipSection
from Module.prompt.sections.speaking_style import SpeakingStyleSection
from Module.prompt.sections.proactive import ProactiveSection
from Module.prompt.sections.own_life import OwnLifeSection
from Module.prompt.sections.self_perception import SelfPerceptionSection
from Module.prompt.system_prompt_manager import SystemPromptManager

_TEMPLATES = "Module/prompt/templates"


def test_full_render_all_sections():
    """11 个 section 全部参与渲染，不报错。"""
    sections = [
        ConflictRulesSection(),
        OutputFormatSection(f"{_TEMPLATES}/f2_output_format.j2"),
        SensorySection(f"{_TEMPLATES}/c5_sensory.j2"),
        EmotionSection(f"{_TEMPLATES}/c3_emotion.j2"),
        MemorySection(f"{_TEMPLATES}/c4_memory.j2"),
        PersonalitySection(f"{_TEMPLATES}/c1_personality.j2", "Elysia"),
        RelationshipSection(f"{_TEMPLATES}/c2_relationship.j2", "Elysia"),
        SpeakingStyleSection(f"{_TEMPLATES}/c6_speaking_style.j2"),
        ProactiveSection(f"{_TEMPLATES}/c7_proactive.j2"),
        OwnLifeSection(f"{_TEMPLATES}/c8_own_life.j2"),
        SelfPerceptionSection(f"{_TEMPLATES}/c9_self_perception.j2"),
    ]
    mgr = SystemPromptManager(sections)
    mgr.set_context(
        user_message="Hello",
        user_name="妖梦",
        stt_emotion="",
        stt_events=[],
    )
    result = asyncio.run(mgr.render())
    assert "框架优先" in result       # F1
    assert "<inner_voice>" in result  # F2
    assert "Elysia" in result         # C1
    # 应该按顺序排列
    pos_f1 = result.find("框架优先")
    pos_f2 = result.find("<inner_voice>")
    assert pos_f1 < pos_f2


def test_versions_all_sections():
    """所有 section 都有 version。"""
    sections = [ConflictRulesSection()]
    mgr = SystemPromptManager(sections)
    versions = mgr.get_versions()
    assert versions["ConflictRules"] == "1.0.0"


def test_validate_all_passes():
    """所有模板 section 的 validate 都通过。"""
    sections = [
        ConflictRulesSection(),
        OutputFormatSection(f"{_TEMPLATES}/f2_output_format.j2"),
        PersonalitySection(f"{_TEMPLATES}/c1_personality.j2", "Elysia"),
    ]
    mgr = SystemPromptManager(sections)
    assert mgr.validate_all() == []
```

- [ ] **Step 2: 运行集成测试确认通过**

```bash
python -m pytest Module/prompt/tests/integration/test_full_render.py -v
```
Expected: 3 passed

- [ ] **Step 3: 适配 PromptAssembler**

替换 `core/prompt_assembler.py` —— 从现有 177 行胶水层切换到 section provider 路径：

```python
"""PromptAssembler — builds the final messages[] for LLM calls.

Glue layer: creates PromptSection instances, wires them into
SystemPromptManager, delegates message assembly to MessageBuilder.
"""

from __future__ import annotations

import logging

from Module.prompt.message_builder import MessageBuilder
from Module.prompt.system_prompt_manager import SystemPromptManager
from Module.prompt.sections.conflict_rules import ConflictRulesSection
from Module.prompt.sections.output_format import OutputFormatSection
from Module.prompt.sections.sensory import SensorySection
from Module.prompt.sections.emotion import EmotionSection
from Module.prompt.sections.memory import MemorySection
from Module.prompt.sections.personality import PersonalitySection
from Module.prompt.sections.relationship import RelationshipSection
from Module.prompt.sections.speaking_style import SpeakingStyleSection
from Module.prompt.sections.proactive import ProactiveSection
from Module.prompt.sections.own_life import OwnLifeSection
from Module.prompt.sections.self_perception import SelfPerceptionSection
from .character_config import CharacterConfig
from .memory.session import SessionMemory
from .memory.long_term import LongTermMemory
from .text_utils import content_text_from_content as content_text_from_raw

logger = logging.getLogger(__name__)

_SECTION = "Module/prompt/templates"


class PromptAssembler:
    """Glue layer: creates sections, delegates to SystemPromptManager."""

    def __init__(
        self,
        character: CharacterConfig,
        session: SessionMemory,
        long_term: LongTermMemory,
    ):
        self._character = character
        self._session = session
        self._long_term = long_term

        sections = [
            ConflictRulesSection(),
            OutputFormatSection(f"{_SECTION}/f2_output_format.j2"),
            SensorySection(f"{_SECTION}/c5_sensory.j2"),
            EmotionSection(f"{_SECTION}/c3_emotion.j2", session),
            MemorySection(f"{_SECTION}/c4_memory.j2", long_term, session),
            PersonalitySection(f"{_SECTION}/c1_personality.j2", character.name),
            RelationshipSection(f"{_SECTION}/c2_relationship.j2", character.name),
            SpeakingStyleSection(f"{_SECTION}/c6_speaking_style.j2"),
            ProactiveSection(f"{_SECTION}/c7_proactive.j2"),
            OwnLifeSection(f"{_SECTION}/c8_own_life.j2", session),
            SelfPerceptionSection(f"{_SECTION}/c9_self_perception.j2", session),
        ]
        self._manager = SystemPromptManager(sections)
        self._builder = MessageBuilder()

    def set_tool_manager(self, tm) -> None:
        pass  # Phase 2: F3 工具声明

    async def build_system_prompt(self, user_message: str = "", user_name: str = "",
                                   stt_emotion: str = "", stt_events=None) -> str:
        self._manager.set_context(
            user_message=user_message,
            user_name=user_name,
            stt_emotion=stt_emotion,
            stt_events=stt_events or [],
        )
        return await self._manager.render()

    async def build_messages(self, user_message: str = "",
                              user_name: str = "",
                              stt_emotion: str = "",
                              stt_events=None,
                              content=None,
                              supports_multimodal: bool = False) -> list[dict]:
        text = content_text_from_raw(content) if content else user_message
        system = await self.build_system_prompt(
            user_message=text, user_name=user_name,
            stt_emotion=stt_emotion, stt_events=stt_events,
        )
        history = self._session.recent_messages(8)
        user_content = content if content else user_message
        return self._builder.build_messages(
            system, history, user_content,
            supports_multimodal=supports_multimodal,
        )
```

- [ ] **Step 4: 运行全量回归**

```bash
python -m pytest Module/prompt/tests/ Module/prompt/test_prompt.py Module/task_system/tests/unit/ core/tests/test_phase3.py -v -q
```

- [ ] **Step 5: Commit**

```bash
git add core/prompt_assembler.py Module/prompt/tests/integration/test_full_render.py
git commit -m "refactor: PromptAssembler → section provider architecture

- Replace Registry-based rendering with 11 independent PromptSections
- SystemPromptManager orchestrates async render + set_context
- Remove user_preferences, mood data prep, sensory data prep from assembler
- Each section now self-manages data sources and templates"
```

---

### Task 5: Cleanup (Phase 1c — 后续执行)

仅在 Task 4 稳定运行后执行。

- [ ] 删除 `Module/prompt/section.py`（旧 dataclass）
- [ ] 删除 `Module/prompt/parser.py`
- [ ] 删除 `Module/prompt/profile.py`
- [ ] 删除 `Module/prompt/registry.py`
- [ ] 删除 `prompt/brain/`
- [ ] 删除 `prompt/Brain.j2`
- [ ] 更新 `Module/prompt/__init__.py`
- [ ] 更新文档
