# Prompt Section 提供者架构设计

**日期**: 2026-07-04
**状态**: 待实现
**范围**: 用提供者模式重构 system prompt——每个 section 是独立模块，自管数据、自管模板、自管生命周期

---

## 动机

当前架构：

```
PromptAssembler  →  准备 context dict（sensory, memory, mood...）
BrainPromptRegistry  →  加载 9 个 .j2 模板 → 注入 context → 渲染 → 拼接
```

问题：
- **数据与模板分离**：PromptAssembler 知道所有数据源，Registry 只管模板。改一个 section 的数据逻辑要改 Assembler、模板要改 .j2。
- **Section 不是一等公民**：模板文件是死的，不能自己拿数据、不能自己 reload、不能被独立测试。
- **难以扩展**：加新 section 要同时改 Assembler + 新建 .j2 + 更新 metadata.yaml。

目标架构：

```
SystemPromptManager  →  持有 11 个 PromptSection 实例 → 按序 async render → 拼接
                           │
                           ├── PersonalitySection    (自管身份数据 + 模板)
                           ├── MemorySection         (自管 LLM 检索 + 模板)
                           ├── EmotionSection        (自管 mood 轮询 + 模板)
                           └── ...
```

每个 PromptSection 是自包含模块：持有数据源引用、持有自己的 .j2 模板、可独立 reload/validate。

---

## 核心接口

### PromptSection ABC

```python
from abc import ABC, abstractmethod

class PromptSection(ABC):
    """System prompt 的一个区块。自管数据、自管模板、自管生命周期。"""

    name: str           # 唯一标识
    version: str        # 语义版本
    description: str    # 人类可读说明

    @abstractmethod
    async def render(self) -> str:
        """渲染该 section 的 prompt 片段。"""

    def reload(self) -> None:
        """热更新模板。有模板文件的 section 覆盖此方法。"""

    def validate(self) -> bool:
        """自检模板完整性。默认返回 True。"""

    def dependencies(self) -> list[str]:
        """依赖的其他 section 名称列表。"""
        return []

    def set_context(self, **kwargs) -> None:
        """注入 per-request 上下文。子类覆盖以接收需要的参数。"""
```

### SystemPromptManager

```python
class SystemPromptManager:
    """持有 section 列表，按序 async render，拼接最终 system prompt。"""

    def __init__(self, sections: list[PromptSection]):
        render_order = [
            "ConflictRules",      # F1
            "OutputFormat",       # F2
            "Sensory",            # C5
            "Emotion",            # C3
            "Memory",             # C4
            "Personality",        # C1
            "Relationship",       # C2
            "SpeakingStyle",      # C6
            "Proactive",          # C7
            "OwnLife",            # C8
            "SelfPerception",     # C9
        ]
        # 按 order 排序 sections
        order_map = {name: i for i, name in enumerate(render_order)}
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

### Base classes

提供两个便利基类：

```python
class StaticSection(PromptSection):
    """纯静态文本 section。"""
    def __init__(self, name, version, description, text): ...

class Jinja2Section(PromptSection):
    """持有独立 .j2 模板的 section。"""
    def __init__(self, name, version, description, template_path: str): ...
    def _render_template(self, **vars) -> str: ...
    def reload(self): ...   # 重读模板文件
    def validate(self): ... # 检查模板可解析
```

---

## 11 个 Section

### 框架层 F1-F2

#### F1: ConflictRulesSection (静态)

```python
class ConflictRulesSection(StaticSection):
    name = "ConflictRules"
    version = "1.0.0"
    description = "冲突规则：框架优先于角色"
    # render: "角色定义你是谁、怎么说话。框架约束是硬规则。冲突时框架优先。"
```

#### F2: OutputFormatSection (静态模板)

```python
class OutputFormatSection(Jinja2Section):
    name = "OutputFormat"
    version = "1.0.0"
    description = "输出格式：inner_voice 双轨协议"
    # 模板: output_format.j2
```

### 实时感知 C3-C5

#### C5: SensorySection (per-request 动态 + 模板)

```python
class SensorySection(Jinja2Section):
    name = "Sensory"
    version = "1.0.0"
    description = "此刻感知：环境/用户行为/语音情绪"
    # set_context(user_message, stt_emotion, stt_events)
    # render 内部逻辑：
    #   - 计算时间数据: datetime.now() → _time_of_day(), _day_name(), _season()
    #   - 映射 stt_emotion → stt_emotion_emoji (EmotionEngine)
    #   - 构建 perception 文本: "{user_name}说：{user_message}\n[语调: {stt_emotion}]"
    #   - 组装 sensory_ctx dict → Jinja2 渲染 sensory.j2
    # 时间辅助函数 (_time_of_day/_day_name/_season) 是 section 内部静态方法，不暴露
```

#### C3: EmotionSection (自管动态 + 模板)

```python
class EmotionSection(Jinja2Section):
    name = "Emotion"
    version = "1.0.0"
    description = "当前心情 + 情绪传染规则"
    # 构造时注入 SessionMemory 引用
    # render: get_param("mood") → Jinja2 渲染 emotion.j2
```

#### C4: MemorySection (自管外部查询 + 模板)

```python
class MemorySection(Jinja2Section):
    name = "Memory"
    version = "1.0.0"
    description = "她记得的过去"
    # 构造时注入 LongTermMemory + SessionMemory 引用
    # 接收 set_context(user_message)
    # render: LongTermMemory.search(user_message,5) → 格式化 → 渲染 memory.j2
```

### 身份与行为 C1-C2, C6-C9

#### C1: PersonalitySection (构造时静态 + 扩展点)

```python
class PersonalitySection(Jinja2Section):
    name = "Personality"
    version = "1.0.0"
    description = "人格：5-8 条 if-then 行为规则"
    # 构造时注入 CharacterConfig.name
    # set_context 预留 name, user_name（v1 可忽略）
    # render: Jinja2 渲染 personality.j2
```

#### C2: RelationshipSection (同 C1)

```python
class RelationshipSection(Jinja2Section):
    name = "Relationship"
    version = "1.0.0"
    description = "关系：1-2 件一起经历过的事"
    # 构造时注入 CharacterConfig.name
    # set_context 预留 name, user_name
    # render: Jinja2 渲染 relationship.j2
```

#### C6: SpeakingStyleSection (同 C1)

```python
class SpeakingStyleSection(Jinja2Section):
    name = "SpeakingStyle"
    version = "1.0.0"
    description = "说话方式：长度/节奏/"嗯哦好嘛"是正常回复"
    # set_context 预留 name, user_name
    # render: Jinja2 渲染 speaking_style.j2
```

#### C7: ProactiveSection (同 C1)

```python
class ProactiveSection(Jinja2Section):
    name = "Proactive"
    version = "1.0.0"
    description = "主动性：3-5 个触发场景 + 分别说什么"
    # set_context 预留 name, user_name
    # render: Jinja2 渲染 proactive.j2
```

#### C8: OwnLifeSection (半动态 + 模板)

```python
class OwnLifeSection(Jinja2Section):
    name = "OwnLife"
    version = "1.0.0"
    description = "她自己的事：一句话-最近在干嘛"
    # 构造时注入 SessionMemory 引用
    # render: get_param("own_life") → 有则渲染，无则空字符串
```

#### C9: SelfPerceptionSection (半动态 + 模板)

```python
class SelfPerceptionSection(Jinja2Section):
    name = "SelfPerception"
    version = "1.0.0"
    description = "自我感知：她注意到的自身变化"
    # 构造时注入 SessionMemory 引用
    # render: get_param("self_perception_history")[-1] → 有则渲染，无则空
```

---

## 文件结构

```
Module/prompt/
├── __init__.py                  # 公开 API
├── protocol.py                  # NEW — PromptSection ABC + StaticSection + Jinja2Section
├── manager.py                   # NEW — SystemPromptManager
├── message_builder.py           # 已有 — MessageBuilder（保留）
├── sections/
│   ├── __init__.py
│   ├── conflict_rules.py        # F1
│   ├── output_format.py         # F2
│   ├── sensory.py               # C5
│   ├── emotion.py               # C3
│   ├── memory.py                # C4
│   ├── personality.py           # C1
│   ├── relationship.py          # C2
│   ├── speaking_style.py        # C6
│   ├── proactive.py             # C7
│   ├── own_life.py              # C8
│   └── self_perception.py       # C9
├── templates/                   # 各 section 的 .j2 模板（从 prompt/brain/sections/ 迁入）
│   ├── f2_output_format.j2
│   ├── c1_personality.j2
│   ├── c2_relationship.j2
│   ├── c3_emotion.j2
│   ├── c4_memory.j2
│   ├── c5_sensory.j2
│   ├── c6_speaking_style.j2
│   ├── c7_proactive.j2
│   ├── c8_own_life.j2
│   └── c9_self_perception.j2
├── parser.py                    # 已有 — 保留（可能后续移除）
├── profile.py                   # 已有 — 后续移除
├── registry.py                  # 已有 — 后续移除
├── section.py                   # 已有 — 升级为 ABC 或替换
├── test_prompt.py               # 已有 — PromptManager 测试
└── tests/
    ├── unit/
    │   ├── test_protocol.py     # NEW
    │   ├── test_manager.py      # NEW
    │   └── test_sections/       # NEW — 每 section 一个测试
    │       ├── test_conflict_rules.py
    │       ├── test_output_format.py
    │       ├── test_sensory.py
    │       ├── test_emotion.py
    │       ├── test_memory.py
    │       ├── test_personality.py
    │       ├── test_relationship.py
    │       ├── test_speaking_style.py
    │       ├── test_proactive.py
    │       ├── test_own_life.py
    │       └── test_self_perception.py
    └── integration/
        ├── test_full_render.py      # 完整 system prompt 集成测试
        └── test_golden_diff.py      # 保留旧 golden diff（迁移验证）
```

---

## PromptAssembler 适配

```python
class PromptAssembler:
    def __init__(self, character, session, long_term):
        # 创建各 section 实例，注入数据源
        sections = [
            ConflictRulesSection(),
            OutputFormatSection("Module/prompt/templates/f2_output_format.j2"),
            SensorySection("Module/prompt/templates/c5_sensory.j2"),
            EmotionSection("Module/prompt/templates/c3_emotion.j2", session),
            MemorySection("Module/prompt/templates/c4_memory.j2", long_term, session),
            PersonalitySection("Module/prompt/templates/c1_personality.j2", character.name),
            RelationshipSection("Module/prompt/templates/c2_relationship.j2", character.name),
            SpeakingStyleSection("Module/prompt/templates/c6_speaking_style.j2"),
            ProactiveSection("Module/prompt/templates/c7_proactive.j2"),
            OwnLifeSection("Module/prompt/templates/c8_own_life.j2", session),
            SelfPerceptionSection("Module/prompt/templates/c9_self_perception.j2", session),
        ]
        self._manager = SystemPromptManager(sections)
        self._builder = MessageBuilder()

    async def build_system_prompt(self, user_message="", user_name="",
                                   stt_emotion="", stt_events=None) -> str:
        self._manager.set_context(
            user_message=user_message,
            user_name=user_name,
            stt_emotion=stt_emotion,
            stt_events=stt_events or [],
        )
        return await self._manager.render()

    async def build_messages(self, ...) -> list[dict]:
        system = await self.build_system_prompt(...)
        history = self._session.recent_messages(8)
        return self._builder.build_messages(system, history, user_content, ...)
```

---

## PromptSection 命名冲突

旧架构 `Module/prompt/section.py` 中 `PromptSection` 是 frozen dataclass。新架构 `Module/prompt/protocol.py` 中 `PromptSection` 是 ABC。

**解决**: Phase 1a 创建 `protocol.py` 时暂不删除 `section.py`。两文件可共存（不同 import 路径：`from Module.prompt.section import PromptSection` vs `from Module.prompt.protocol import PromptSection`）。Phase 1c 清理时，删除旧的 `section.py` + `parser.py` + `profile.py` + `registry.py`，`protocol.py` 中的 `PromptSection` ABC 成为唯一定义。

## 迁移策略

### Phase 1a: 搭架子（与现有并行）

1. 创建 `Module/prompt/protocol.py` — PromptSection ABC + StaticSection + Jinja2Section（与旧 `section.py` 并存）
2. 创建 `Module/prompt/manager.py` — SystemPromptManager
3. 创建 `Module/prompt/sections/` — 11 个 section 实现
4. 创建 `Module/prompt/templates/` — 从 `prompt/brain/sections/` 提取模板内容
5. 写单元测试（11 section tests + protocol + manager tests）
6. 写集成测试（full render + 保留旧 golden diff）
7. PromptAssembler 加 feature flag：优先用 section provider 路径

### Phase 1b: 切换

8. 关闭 feature flag，正式使用新路径
9. 保留旧 registry.py/parser.py/profile.py/section.py 作为兼容

### Phase 1c: 清理

10. 删除 `section.py`（旧 dataclass）、`parser.py`、`profile.py`、`registry.py`
11. 删除 `prompt/brain/`（模板已迁入 `Module/prompt/templates/`，behavior.j2 参考副本已安全保存）
12. 更新 `__init__.py` — 从 `protocol.py` 和 `sections/` 导出
13. 删除 `prompt/Brain.j2`（旧 fallback 路径不再需要）
14. 更新文档

---

## 保留但不使用

- **behavior.j2** — 旧架构中的"请求分类（A/B）+ 工具使用规则" section，约 20 行。Phase 1a 复制到 `Module/prompt/templates/_reference_behavior.j2`（`_reference_` 前缀表示保留供参考，不被任何 section 加载）。删除原始 `prompt/brain/sections/behavior.j2`。未来 Phase 2 的 F3（工具声明）会替代。

## 已删除

- **user_preferences** — 当前 `prompt_assembler.py` 总是传空字符串 `user_preferences=""`，无代码写入此值。RelationshipSection 已包含关系描述能力，不需要此参数。

## YAGNI

- F3 工具声明、C10 情绪深度、C11 关系进展、C12 物理存在感、C13 AI 助手能力、C14 多模态表达 — Phase 2+
- Profile 系统 — Section 级别的启用/禁用通过构造时传入 sections 列表控制
- A/B 测试 — 已移除
- 热更新（文件监听）— Phase 2
