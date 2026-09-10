# 多角色 Stage B-1：C2/C3/C6/C7 内容参数化（Elysia 现网行为不变）Spec

> **文档导航：** [项目文档](../../../../README.md)
> **Spec 状态：** 待 review
> **日期：** 2026-09-02
> **架构稿：** [`docs/2026-08-30-character-prompt-customization-backend-architecture-design.md`](../../../../2026-08-30-character-prompt-customization-backend-architecture-design.md)
> **实施批次：** 阶段 B-1（内容所有权重构的第一步：机制打通）
> **事实基线：** `main@ca2de890`（Stage A 完成后的 HEAD）
> **性质：** 生产代码 + 测试批次——本质是「把写死的 Elysia 内容从模板抽成可从角色配置注入的变量，但保持 Elysia 现网渲染结果 byte-for-byte 不变」
> **权威范围：** 12 段 System Prompt 的 C2 Relationship / C3 Emotion / C6 SpeakingStyle / C7 Proactive 模板参数化、对应 section 构造器、`CharacterConfig` 字段扩展、Elysia config 补值、Elysia 专属快照守门
> **明确排除：** 不改辅助 Prompt（`.replace()` 那 3 个 + Jinja2 那 2 个）；不建 Cyrene 角色；不改 CharacterEngine；不引入 `CharacterDefinition/Revision/PromptProfile` 领域模型；不做 C6 框架/风格分离（方案 Y，留到 B-2）；不改 Stage A 已建的 12 段泄漏测试断言方向

---

## 1. 目标与边界

### 1.1 目标

在进入「真正让角色差异化」之前，先把 12 段 System Prompt 里**能安全参数化**的写死 Elysia 内容抽出来，改成**能从角色配置注入的变量**——但**不改变 Elysia 现网渲染结果**。本批次建立「内容参数化机制 + Elysia 行为守门」，为 B-2 的 Cyrene 落地打地基。

具体交付：

1. **C6 SpeakingStyle 参数化**（方案 X）：把当前 C6 模板的 else 分支（`明快柔和自信亲昵`/`♪规则`/`称呼表`/`五模式`/`调情方式`/`夸奖方式`/`长度`/`节奏`）**整体迁移**到 `personality.speaking_style`（Elysia config 填入），模板留壳（wrapper 移入 conditional 内层）。**section 构造器默认 `speaking_style` 回退到 `_C6_L1_DEFAULT` 硬编码常量**（改造时把当前 C6 else 分支正文固化进 `speaking_style.py`）——保证 `compression.py`/`test_boundary.py`/`test_full_render.py` 裸构造该 section 时仍渲染 L1 默认（行为不变）；
2. **C2 Relationship 字面 `她` 修正**：第 18 行 `此刻她只注视着我` → `此刻{{ user_pronoun }}只注视着我`（已验证 Elysia 输出 byte-for-byte 不变）；
3. **Elysia 专属快照守门**：新建 `elysia_config` 夹具 + `test_elysia_snapshot_unchanged`，断言 Elysia 渲染结果 == **B-1 改造前生成的 Elysia 基线快照**（byte-for-byte 不变），这是 B-1「Elysia 行为不变」的核心证明；
4. **冷角色快照更新**：冷角色在 B-1 后 C6 走 `{% elif speaking_style %}`（`speaking_style=""` 空 → 该段真消失），快照含义改变，需重新生成；
5. **config.yaml 校验断言**：`character/elysia/config.yaml` 的 `personality.speaking_style` == `_C6_L1_DEFAULT`（L1 默认常量），防两份值分叉。

### 1.2 非目标

- **不改 C3 / C7 / F2 / F3 模板**：它们的 Elysia 专属内容**与 `{{ user_pronoun }}`/`{{ user_name }}` 占位符交织**，无法安全迁移到 config 变量（Jinja2 变量值不递归渲染占位符）。去 Elysia 化留给 B-2 的 per-role 模板机制；
- **不改辅助 Prompt**：`ExtractMemory.j2`/`ConsolidationCompare.j2`/`MemoryReflector.j2`（raw `.replace()` 那 3 个）、`CharacterReflector.j2`/`SpeakingStyleEdit.j2`（Jinja2 `pm.render()` 那 2 个）的 Elysia 泄漏，全部留到 B-2；
- **不建 Cyrene**：cyrene 角色（config.yaml + personality.md）是 B-2 的交付物；
- **不改 CharacterEngine**：`core/personality/engine.py` 的 `render_c6`/`render_c1`/`render_c9`/`state` 完全不动；
- **不引入 `CharacterDefinition/Revision/PromptProfile` 领域模型**：B-1 只做「当前 `CharacterConfig.personality.speaking_style` 承载 C6 内容」的机制打通，不落地 `PromptProfile` 对象/Compiler/validator；
- **不做 C6 框架/风格分离**（方案 Y）：把 C6 的 A 类框架（长度/节奏/夸奖）与 B 类风格（明快/♪/称呼/五模式/调情）重新划分，是内容设计工作，留到 B-2；
- **不改 Stage A 已建的泄漏测试断言方向**：`test_leak_*` 系列在 B-1 仍保持 `assert "♪" in result`（红基线），**不**反转为 `not in`——翻转是 B-2 的事；
- **不改 `Module/system_prompt/tests/`** 的现有测试（`integration/test_full_render.py` 等）——除非本批改动导致它们必然红，才在本批内微调。

### 1.3 "现网行为不变"的定义

B-1 的「Elysia 现网行为不变」指：**用 `elysia_config`（`name=Elysia` / `user_pronoun=她` / `base_personality=<真 Elysia L1>` / `personality.speaking_style=_C6_L1_DEFAULT`（`speaking_style.py` 的 L1 默认硬编码常量，== 当前 C6 else 分支全文））渲染 12 段 System Prompt，其结果与 B-1 改造前用同一 `elysia_config` 生成的基线快照 `system_prompt_snapshot_elysia_<sha>.txt` 逐字一致（含 C5 时间字段归一化后）。**

> ⚠️ **关键**：这个基线快照**不是** Stage A 的 `system_prompt_snapshot_e793a782.txt`（那是冷角色）。B-1 必须在**改造模板前**先用 `elysia_config` 生成一份 Elysia 专属基线快照并入库，改造后再断言 == 它。若先改模板再生成基线，就会把「改造后的结果」误当成「现况」，失去守门意义。

这保证了：Elysia 在真实运行时（`config.yaml` 已填 `personality.speaking_style`）看到的 prompt 和 B-1 前**完全一样**。

---

## 2. 事实基线（已核实的代码现状）

### 2.1 12 段 System Prompt 渲染架构

`PromptAssembler.build_system_prompt()`（`core/prompt/prompt_assembler.py:119`）：
1. 从 `CharacterEngine` 拉 mood / C6 / C1 / C9 数据；
2. `self._manager.set_context(user_message, user_name, user_pronoun=self._character.user_pronoun, ...)`；
3. `await self._manager.render()` 按 `_RENDER_ORDER` 拼接。

关键点：`user_pronoun` 从 `self._character.user_pronoun`（`CharacterConfig`）注入，`mood`/`speaking_style_c6` 从 `CharacterEngine.state` 注入。**C2/C3/C7 的 `{{ user_pronoun }}` 插值正确，真正泄漏是模板正文字面量。**

### 2.2 各模板的 Elysia 专属内容（实测）

| 模板 | 渲染变量 | Elysia 专属内容 | B-1 处理 |
|------|---------|----------------|---------|
| `c6_speaking_style.j2` | `speaking_style_c6`, `user_name` | **else 分支整段**：`明快柔和自信亲昵`、`♪规则`、`称呼表`、`五模式切换`、`调情方式`、`夸奖方式`、`长度`、`节奏`（**无 `{{ }}` 占位符**） | **方案 X**：整体迁移到 `personality.speaking_style`，模板留壳 |
| `c2_relationship.j2` | `name`, `user_name`, `user_pronoun` | 第 18 行 `此刻她只注视着我`（字面 `她`） | **只修第 18 行** → `{{ user_pronoun }}`；核心态度段（含大量占位符）**不迁移** |
| `c3_emotion.j2` | `mood`, `user_name`, `user_pronoun` | `今天不想振作也没有关系`/`我不会说你别难过`/`理解你…` + 傲娇/撒娇 | **B-1 不改**（每行含占位符，迁移占位符失效） |
| `c7_proactive.j2` | `user_name`, `user_pronoun` | `需要我的时候我都在哦♪`/`喜欢靠近人` | **B-1 不改**（与占位符交织） |
| `f2_output_format.j2` | （无变量） | 示例 `她好像很开心`/`她居然注意到了` | **B-1 不改**（示例场景指代，非代词泄漏） |
| `f3_tts_markup.j2` | `tts_provider` | 示例 `她果然很聪明呢` | **B-1 不改**（示例场景指代） |

### 2.3 已在 `CharacterConfig` 中存在的字段

`CharacterConfig`（`core/config/character_config.py`）已有：
- `personality: dict`（YAML `personality:` 段原样加载）；
- `@property speaking_style`（读取 `personality.get("speaking_style", "")`，第 43 行）；
- `name`/`user_pronoun`/`base_personality`。

**B-1 只复用已有的 `speaking_style` property**（C6 参数化）。**不新增** `relationship_style`/`emotion_profile`/`proactive_style` property（YAGNI——B-1 不消费它们，C2/C3/C7 不迁移，B-2 才真正需要）。

### 2.4 `state.speaking_style_c6` 默认值

`CharacterEngine.state.speaking_style_c6` 默认 `""`（`core/personality/engine.py:61`），`render_c6()` 返回 `C6Data(speaking_style_c6=self.state.speaking_style_c6)`（第 194 行）。`speaking_style_c6=""` 时 C6 模板走 else 分支（默认模板）。**B-1 后这个 else 分支被 `personality.speaking_style` 变量取代。**

### 2.5 Stage A 已建测试（B-1 需对齐）

- `core/prompt/tests/conftest.py`：`cold_config`/`cold_engine_mock`/`mock_long_term`/`mock_session` + `make_assembler`/`render_system_prompt` helper；
- `core/prompt/tests/fixtures/system_prompt_snapshot_e793a782.txt`（18132 字节，冷角色渲染）；
- `core/prompt/tests/test_system_prompt_snapshot.py`：`test_snapshot_matches_current`/`test_12_section_order`/`test_frame_wrappers_present`；
- `core/prompt/tests/test_system_prompt_leak.py`：17 个红基线用例（`assert "♪" in result` 等）。

**Stage A 快照是「冷角色看 Elysia 默认模板」——不是 Elysia 现网行为。** B-1 必须新建 Elysia 专属快照才能守门「Elysia 行为不变」。

---

## 3. 参数化设计（实测修正后定稿）

### 3.0 关键实测发现：Jinja2 变量值不递归渲染

**实测证明**：当模板用 `{% if relationship_style %}{{ relationship_style }}{% endif %}` 渲染时，`relationship_style` 变量值**内部的 `{{ user_pronoun }}` 占位符不会被二次渲染**——输出是字面量 `{{ user_pronoun }}`。

```python
# 实测（conda run -n Elysia python）
tmpl = env.from_string('{{ relationship_style }}')
tmpl.render(relationship_style='你主动靠近{{ user_pronoun }}。')
# 输出: '你主动靠近{{ user_pronoun }}。'  ← 占位符未替换！
```

**后果**：任何**含 `{{ }}` 占位符的模板内容**（C2 核心态度段有 14 处 `{{ user_pronoun }}` + 1 处 `{{ user_name }}`；C3 情绪响应规则每行都有 `{{ user_name }}`/`{{ user_pronoun }}`；C7 同样）**都不能安全迁移到 config 变量**——迁移后占位符失效，Elysia 渲染结果被破坏。

**只有不含 `{{ }}` 的内容（如 C6 else 分支全文）才能安全迁移到 `personality.speaking_style`。**

由此 B-1 的参数化范围**收窄**为：
- **C6**（无占位符）→ 方案 X 整体迁移到 `speaking_style` ✓
- **C2** → 只修第 18 行字面 `她` → `{{ user_pronoun }}`（已验证 Elysia 输出 byte-for-byte 不变）✓
- **C3 / C7 / F2 / F3** → **B-1 不迁移**（与占位符交织，去 Elysia 化留给 B-2 的 per-role 模板机制）

### 3.1 C6 SpeakingStyle（方案 X——整体迁移，模板留壳）

**`speaking_style_c6` 优先，`speaking_style` 次之。** 关键设计：**模板 wrapper `<speaking_style>` 放进 conditional 内层**（方式 B），使 `speaking_style=""` 时 C6 段**真消失**（非空壳帧）——这在 `SystemPromptManager.render()` 的 `if result:` 判空下自然生效。

**改 `c6_speaking_style.j2`（方式 B——wrapper 在 conditional 内）：**

```jinja2
{# 现有头部注释不变 #}
{% if speaking_style_c6 %}
<speaking_style>
{{ speaking_style_c6 }}
</speaking_style>
{% elif speaking_style %}
<speaking_style>
{{ speaking_style }}
</speaking_style>
{% endif %}
```

- `speaking_style_c6`（L2 动态，`CharacterEngine.state.speaking_style_c6`）优先；
- `speaking_style`（L1 静态，`CharacterConfig.personality.speaking_style`）次之，含当前 C6 else 分支全文；
- 两者都空 → C6 段**真消失**（整段不渲染）。

**`speaking_style.py` section 构造器加 `speaking_style` 参数，默认 = `_C6_L1_DEFAULT` 模块常量（L1 默认兜底）。** ⚠️ **关键：`_C6_L1_DEFAULT` 必须是硬编码模块常量，不能运行时从 `c6_speaking_style.j2` 提取**——因为 B-1 改造把模板的 else 分支删了，运行时从模板提取会得到空（自指矛盾），导致 `_refresh_c6` 裸构造时 C6 空、L2 C6 演化静默失效。改造时把当前 C6 else 分支正文一次性固化进 `speaking_style.py` 作为常量，之后模板删 else 不影响它：

```python
# 硬编码 L1 默认说话风格（当前 C6 else 分支正文，改造前一次性固化）
_C6_L1_DEFAULT = """每次回复前，先在心里想一下（这是你自己的事），再决定说出来什么。
...（当前 C6 else 分支全文，逐字固化）..."""

class SpeakingStyleSection(Jinja2Section):
    def __init__(self, template_path: str, speaking_style: str | None = None):
        super().__init__(template_path)
        self._user_name = ""
        self._speaking_style_c6 = ""
        self._speaking_style = speaking_style if speaking_style is not None else _C6_L1_DEFAULT
    def set_context(self, **kwargs):
        self._user_name = kwargs.get("user_name", "")
        self._speaking_style_c6 = kwargs.get("speaking_style_c6", "")
    async def render(self):
        return self._render_template(
            user_name=self._user_name,
            speaking_style_c6=self._speaking_style_c6,
            speaking_style=self._speaking_style,
        )
```

> ⚠️ **`speaking_style` 默认值语义**：`speaking_style` 构造参数**默认 `None`**（→ 回退 `_C6_L1_DEFAULT`，即保 L1 默认），**不是** `""`。这意味着：
> - **裸构造**（`compression.py`/`test_boundary.py`/`test_full_render.py`）→ `speaking_style` = `_C6_L1_DEFAULT` → 渲染完整 C6（与 B-1 前一致）；
> - **`PromptAssembler` 显式传 `character.speaking_style`**——cold 的 `speaking_style=""` 被显式传入 → C6 段真消失（这正确，冷角色不该有 Elysia C6）；Elysia 的 `speaking_style` 非空 → 渲染全文。
>
> 区分的本质：**「裸构造无 speaking_style」= 使用 L1 默认**（保留旧行为），**「显式传空」= 明确无风格**（冷角色 C6 消失）。两语义正交，由 `None` vs `""` 区分。

**byte-for-byte 等价性（实测验证）**：`speaking_style=_C6_L1_DEFAULT` 经方式 B 模板渲染 == 旧模板 else 分支渲染（1904 字符字节完全相同，含尾部 `...方式里。\n</speaking_style>`）。因此 Elysia 配置 `speaking_style=_C6_L1_DEFAULT` 时的 C6 段与 B-1 前**字节一致**。

**`PromptAssembler` 传参：**
```python
SpeakingStyleSection(f"{_SECTION}/c6_speaking_style.j2", speaking_style=character.speaking_style)
```
（`character.speaking_style` 对 cold 返回 `""`，对 Elysia 返回非空正文。）

**Elysia config.yaml 补值：**
```yaml
personality:
  speaking_style: |
    每次回复前，先在心里想一下（这是你自己的事），再决定说出来什么。
    ...（当前 C6 else 分支全文，逐字）...
```

### 3.2 C2 Relationship（只修字面 `她`）

**只在第 18 行改**：`此刻她只注视着我` → `此刻{{ user_pronoun }}只注视着我`。

- 已验证：Elysia（user_pronoun=她）输出 byte-for-byte 不变；冷角色（user_pronoun=你）修正为 `你`。
- **C2 核心态度段（第 16/18/20 行）不迁移**——含大量 `{{ user_pronoun }}`/`{{ user_name }}`，迁移会占位符失效。去 Elysia 化留给 B-2。

### 3.3 C3 Emotion / C7 Proactive / F2 / F3 —— B-1 不迁移

- **C3**：情绪响应规则每行含 `{{ user_name }}`/`{{ user_pronoun }}`，无法安全迁移到 config。
- **C7**：主动场景/冷却机制与 `{{ user_name }}`/`{{ user_pronoun }}` 交织。
- **F2/F3**：字面 `她` 在**示例对话**里（示例场景的第三方指代），非用户代词泄漏，改它改变 Elysia 示例文本——不是 B-1 目标。

**这三个的去 Elysia 化统一留给 B-2**，用 per-role 模板目录机制（每角色一份 C2/C3/C7 模板），而非 config 变量。

### 3.4 `CharacterConfig` 新增 property

只新增 `relationship_style`/`emotion_profile`/`proactive_style` 三个 property **供 B-2 使用**（B-1 只有 C6 的 `speaking_style` 被实际消费）。`speaking_style` property 已有（读 `personality.get("speaking_style", "")`，第 43 行）。

> ⚠️ 但 **B-1 若不加 C2/C3/C7 的字段也未定义行为**——它们仍走模板原文。故 B-1 的 `CharacterConfig` **暂不新增**这三个 property（YAGNI——B-2 才真正消费）。B-1 只依赖已有的 `speaking_style`。

### 3.5 `speaking_style=""` 的角色行为变化

C6 模板的 else 分支被替换成 `{% elif speaking_style %}`。**未配 `speaking_style` 的角色 C6 段会消失**（如 Stage A 的冷角色快照：`speaking_style=""` → C6 不渲染）。

这是 B-1 唯一的行为变化点（C2/C3/C7/F2/F3 都未动，无行为变化）。当前项目只有 Elysia 一个完整角色（配了 `speaking_style`，行为不变），冷角色是测试夹具（`speaking_style=""`，C6 消失需更新快照）。

---

## 4. B-1 的 Explosion Radius 分析

### 4.1 影响的正向范围（B-1 收窄后）

| 层 | 影响 |
|----|------|
| `Module/system_prompt/templates/c6_speaking_style.j2` | else 分支删除 → wrapper 移入 conditional 内层（`{% if %}{% elif %}{% endif %}`） |
| `Module/system_prompt/templates/c2_relationship.j2` | 第 18 行 `她`→`{{ user_pronoun }}` |
| `Module/system_prompt/sections/speaking_style.py` | 构造器加 `speaking_style` 参数（默认 `None` → 回退 `_C6_L1_DEFAULT` 硬编码常量） |
| `core/prompt/prompt_assembler.py` | SpeakingStyleSection 构造器传 `character.speaking_style` |
| `character/elysia/config.yaml` | `personality:` 下补 `speaking_style` 字段 |
| `core/prompt/tests/conftest.py` | 新增 `elysia_config` 夹具（`speaking_style` 复用 `_C6_L1_DEFAULT`） |
| `core/prompt/tests/test_system_prompt_snapshot.py` | 新增 `test_elysia_snapshot_unchanged` + 冷快照更新 |
| `core/prompt/tests/test_system_prompt_leak.py` | `test_leak_speaking_mode_structure`/`test_leak_fixed_persona` 改用 `elysia_config`（否则 cold C6 空必红） |
| `core/prompt/tests/fixtures/system_prompt_snapshot_elysia_<sha>.txt` | 新增 Elysia 专属快照 |
| `core/prompt/tests/fixtures/system_prompt_snapshot_e793a782.txt` | 冷角色 C6 真消失后重新生成 |

**未改但需验证行为不变（B-1 范畴，非文件改动）：**
- `core/personality/compression.py:269` `_refresh_c6`：**裸构造** `SpeakingStyleSection`（不传 `speaking_style`）。B-1 前渲染 L1 默认（1904 字符含 `明快`）；B-1 后因 section 默认 `speaking_style` 回退 `_C6_L1_DEFAULT`（硬编码常量），**仍渲染 L1 默认** → 行为不变 ✓（实测验证）。临界：若 section 默认是 `""` 则会渲染空 → `_refresh_c6` 静默失效。**B-1 必须让 section 默认 = `_C6_L1_DEFAULT`，不能是 `""`。**
- `Module/system_prompt/tests/unit/test_sections/test_boundary.py`：裸构造 `SpeakingStyleSection` 的 4 个测试（`test_no_user_name_default`/`test_with_user_name`/`test_fallback_to_default_template`/`test_render_dynamic_text`）。B-1 后因 section 默认 L1，裸构造仍渲染完整 C6 → 通过 ✓（需在 B-1 验证，`test_fallback_to_default_template` 的语义变为「裸构造渲染 L1 默认」，断言 `五模式切换`/`轻松模式` 仍通过）。
- `Module/system_prompt/tests/integration/test_full_render.py`：`result.find("speaking_style")` 排序断言。裸构造 `SpeakingStyleSection` → section 默认 L1 → `speaking_style` 标签在结果 → 通过 ✓。
- `Module/system_prompt/__main__.py`（模块 smoke test）：裸构造 `SpeakingStyleSection` → section 默认 `_C6_L1_DEFAULT` → 行为不变 ✓。

### 4.2 明确的非影响（B-1 不碰）

- `core/personality/engine.py`/`reflector.py`/`awareness.py`（`_refresh_c6` 的调用方非 B-1 改，行为不变）；
- `core/memory/consolidator.py`/`long_term.py`/`memory_reflector.py`；
- `core/prompts/*.j2`（辅助 prompt）；
- `Module/system_prompt/tests/` 的 `test_system_prompt_manager.py`、`test_sections/test_*.py`（除 `test_boundary.py` 外）；
- `frontend/`、`frontend_contract/`；
- `c3_emotion.j2`/`c7_proactive.j2`/`f2_output_format.j2`/`f3_tts_markup.j2`（B-1 不改，留 B-2）。

### 4.3 行为变化总结（B-1 收窄后）

| 场景 | B-1 前 | B-1 后 |
|------|--------|--------|
| Elysia（`speaking_style` 已配，`PromptAssembler` 显式传非空） | C6 渲染 else 分支（默认模板） | C6 渲染 `speaking_style`（= 同文本）→ **相同** |
| 冷角色（`speaking_style=""`，`PromptAssembler` 显式传空） | C6 渲染 else 分支（默认模板） | C6 **真消失**（空段不渲染）→ **变化**（符合参数化语义） |
| `compression.py` `_refresh_c6`（裸构造，无显式 `speaking_style`） | 渲染 L1 默认 | section 默认 `_C6_L1_DEFAULT` → 仍渲染 L1 默认 → **相同** ✓ |
| `test_boundary.py` 裸构造 | 渲染 L1 默认 | section 默认 L1 → **相同** ✓ |
| `test_full_render.py` 裸构造 | 渲染 L1 默认，`speaking_style` 标签在 | section 默认 L1 → **相同** ✓ |
| Elysia C2 第 18 行 | `此刻她只注视着我` | `此刻{{ user_pronoun }}只注视着我` → **相同**（user_pronoun=她） |
| 任何角色 C2 第 18 行（user_pronoun=你） | 字面 `她` 泄漏 | `你` → **修正** |
| C3 / C7 / F2 / F3 | 原文 | 原文 → **相同** |

---

## 5. 测试资产设计

### 5.1 `elysia_config` 夹具

新建 `core/prompt/tests/conftest.py` 的 `elysia_config`。**注：B-1 只配 `speaking_style`（C6），不配 `relationship_style`/`emotion_profile`/`proactive_style`（C2/C3/C7 不迁移）**：

```python
# 复用 speaking_style.py 的 L1 默认常量（与 production section 裸构造默认同源）
from Module.system_prompt.sections.speaking_style import _C6_L1_DEFAULT

@pytest.fixture
def elysia_config() -> CharacterConfig:
    return CharacterConfig(
        name="Elysia",
        user_pronoun="她",
        base_personality=Path("character/elysia/personality.md").read_text("utf-8").strip(),
        personality={
            "speaking_style": _C6_L1_DEFAULT,   # 与 B-1 的 L1 默认同源，非独立提取
        },
    )
```

> ⚠️ **`elysia_config` 用 `CharacterConfig(base_personality=...)` 直接构造，不走 `from_yaml`**，所以 `base_personality` 是 Elysia L1 全文。**但注意**：`build_system_prompt()` 内部 `_refresh_personality_data()` 会用 `cold_engine_mock.render_c1()` 返回的 `C1Data(core_personality="冷水人格文本")` **覆盖** `PersonalitySection` 的 core。因此 **`elysia_config` 快照的 C1 段实际是「冷水人格文本」，不是 Elysia L1**——这是测试夹具的既定行为（verified 实测），**不影响 C6/C2 区块守门**（C6 正确渲染 `speaking_style` 全文，C2 正确渲染 `她`）。`elysia_config` 快照锁的是「Elysia 名字 + mock C1 + Elysia C6/C2"混搭」这个**固定测试输出**，用于证明 B-1 改模板后 C6/C2 不变。

> ⚠️ **B-1 不写 `_c6_else_body()` 从模板提取**——模板改造后没有 else 分支，提取会空。`elysia_config` 的 `speaking_style` 统一复用 `speaking_style.py` 的 `_C6_L1_DEFAULT` 硬编码常量（与 production section 裸构造默认同源）。

### 5.2 `test_elysia_snapshot_unchanged`

新增 `core/prompt/tests/test_system_prompt_snapshot.py`：

```python
@pytest.mark.asyncio
async def test_elysia_snapshot_unchanged(elysia_config, cold_engine_mock, mock_long_term, mock_session):
    """Elysia 专用快照：现网 Elysia 渲染结果 byte-for-byte 不变。"""
    rendered = await render_system_prompt(
        make_assembler(elysia_config, cold_engine_mock, mock_long_term, mock_session)
    )
    expected = _ELYSIA_FIXTURE.read_text(encoding="utf-8")
    assert normalize_snapshot(rendered) == normalize_snapshot(expected)
```

**B-1 必须先跑一次生成 Elysia 专属快照**（B-1 改造前用 `elysia_config` + 当前模板渲染），入库为基线。改造后断言 == 基线。

### 5.3 Elysia config.yaml 补值后渲染路径校验

`build_system_prompt()` 调 `_refresh_personality_data()` 用 `cold_engine_mock`：
- `render_c6` → `C6Data(speaking_style_c6="")` → C6 走 `{% elif speaking_style %}`，Elysia `speaking_style` 非空 → 渲染 `speaking_style` 全文（== 当前 C6 else 分支）；
- `render_c1` → `C1Data(core_personality=COLDSHARD_BASE_PERSONALITY)` → **覆盖** `PersonalitySection._core` → C1 段是冷水人格文本，**非 Elysia L1**；
- `render_c9` → `C9Data(awareness_text="")` → C9 空。

**故 `elysia_config` 快照 ≠ Stage A 冷角色快照 + Elysia L1。** 它的构成：
- C1 段：`你是 Elysia。` + **冷水人格文本**（cold mock 覆盖，非 Elysia L1）；
- C6 段：`speaking_style` 全文（== 冷角色快照的 C6 else 分支，同文本）；
- C2 段：用 `用户`/`她`（conftest 的 `render_system_prompt` 默认 `user_name="用户"`，非"妖梦"）；第 18 行渲染 `此刻她只注视着我`（Elysia user_pronoun=她）；
- C3/C4/C5/C8/C9：mock/空（与冷角色快照相同）。

**快照的守门范围**：锁的是「C6 从 else 分支迁移到 `speaking_style` 变量后输出不变」+「C2 第 18 行 `她`→`{{ user_pronoun }}` 后 Elysia 输出不变」。**不锁 C1**（C1 段是 mock 冷水，未反映真实 Elysia L1）。这是 B-1 守门的**明确局限**——B-1 的核心是 C6/C2 参数化行为不变，C1 未纳入本批改动（C1 无 Elysia 专属词，非 B-1 目标）。

> ⚠️ **Elysia L1 真实 C6 行为未直接守门**：`elysia_config` 的 C6 来自 `_C6_L1_DEFAULT`（`speaking_style.py` 硬编码常量），不是 `config.yaml` 手工复制的值。若实现时 config.yaml 复制错误，`test_elysia_snapshot_unchanged` 不会捕获。**B-1 需加一条 config.yaml 校验断言**（见 §5.5），确保 `character/elysia/config.yaml` 的 `personality.speaking_style` == `_C6_L1_DEFAULT`。

### 5.4 冷快照更新

`test_snapshot_matches_current`（冷角色）在 B-1 后 C6 段**真消失**（`PromptAssembler` 显式传 `character.speaking_style=""` → wrapper 在 conditional 内层 → 整段不渲染）→ 需重新生成冷角色快照（C6 整段消失，其余段相同）。

> ⚠️ **为什么冷角色 C6 真消失而 `compression.py` 不为空**：冷角色走 **`PromptAssembler` 显式传 `speaking_style=""`**（区分 `None` 与 `""`，`""` = 明确无风格 → C6 消失）；`compression.py`/`test_boundary.py` **裸构造**（`speaking_style=None` → 回退 `_C6_L1_DEFAULT` → C6 保留 L1 默认）。两路径由 None/"" 区分，正是 B-1 的语义设计。

### 5.5 config.yaml 校验断言（防手工复制漂移）

`character/elysia/config.yaml` 的 `personality.speaking_style` 是**手工复制**当前 C6 else 分支进 YAML 块标量（`|`）。为防止复制错误导致 Elysia 现网 C6 与 `_C6_L1_DEFAULT`（`speaking_style.py` 的硬编码常量）漂移，B-1 需加一条校验：

```python
def test_elysia_config_speaking_style_matches_template():
    """Elysia config.yaml 的 speaking_style 必须等于 _C6_L1_DEFAULT（L1 默认常量）。"""
    cfg = CharacterConfig.from_yaml(Path("character/elysia/config.yaml"))
    assert cfg.speaking_style == _C6_L1_DEFAULT
```

这条断言确保**两份 Elysia `speaking_style`（config.yaml 手工值 vs `_C6_L1_DEFAULT`）不会分叉**——`test_elysia_snapshot_unchanged` 用的是 `_C6_L1_DEFAULT`，这条用的是 config.yaml 值，两者一致则 Elysia 现网行为 == 快照。

---

## 6. 测试清单

### 6.1 测试文件布局

- 改：`core/prompt/tests/conftest.py`（加 `elysia_config`，`speaking_style` 复用 `_C6_L1_DEFAULT`）；
- 改：`core/prompt/tests/test_system_prompt_snapshot.py`（加 `test_elysia_snapshot_unchanged` + `test_elysia_config_speaking_style_matches_template`，更新冷快照）；
- 改：`core/prompt/tests/test_system_prompt_leak.py`（`test_leak_speaking_mode_structure`/`test_leak_fixed_persona` 改用 `elysia_config`）；
- 新增：`core/prompt/tests/fixtures/system_prompt_snapshot_elysia_<sha>.txt`（Elysia 专属快照）；
- 更新：`core/prompt/tests/fixtures/system_prompt_snapshot_e793a782.txt`（冷角色 C6 真消失后重新生成）。

### 6.2 用例

| 用例 | 断言 |
|------|------|
| `test_elysia_snapshot_unchanged` | Elysia 渲染 == Elysia 专属快照（byte-for-byte，含 C5 归一化） |
| `test_elysia_config_speaking_style_matches_template` | `config.yaml` 的 `speaking_style` == `_C6_L1_DEFAULT` |
| `test_snapshot_matches_current`（更新） | 冷角色渲染（C6 消失）== 更新后冷快照 |
| `test_12_section_order`（更新） | **改用 `elysia_config` 跑**——Elysia C6 非空，12 段顺序完整（含 `speaking_style`） |
| `test_frame_wrappers_present`（更新） | **改用 `elysia_config` 跑**——Elysia C6 非空，`speaking_style` wrapper 若现 |

> ⚠️ **`test_12_section_order`/`test_frame_wrappers_present` 原本用冷角色跑，断言 `speaking_style` 段出现。** B-1 后冷角色 C6 段**真消失**（`speaking_style=""` 空），这两个测试必须**改用 `elysia_config`**（Elysia C6 非空，段保留）——这正是 Elysia 专属快照的价值：验证 Elysia 的 12 段完整、wrapper 齐全。冷角色则专门用于验证「C6 消失但其余段不炸」。

**B-1 的测试责任划分：**
- `elysia_config` —— 验证「Elysia 现网行为 byte-for-byte 不变」（12 段完整、顺序正确、wrapper 在、C6 全文、内容 == 快照）；
- `cold_config` —— 验证「参数化机制生效且其余段不炸」（C6 段消失、其余 11 段仍正确渲染）。

---

## 7. 决策记录（B-1 的 Rulings）

1. **C6 用方案 X（整体迁移，模板留壳）**：当前 C6 else 分支全文本身就是 Elysia 风格且**无 `{{ }}` 占位符**，可安全逐字迁移到 `speaking_style`，Elysia 行为不变。方案 Y（框架/风格分离）是内容设计工作，留 B-2。
2. **B-1 快照守门用「改造前生成的 Elysia 基线快照」**：Stage A 快照是冷角色，不是 Elysia 现网行为。B-1 必须在**改模板前**用 `elysia_config` 生成 Elysia 专属基线快照入库，改造后断言 == 它，才能证明「Elysia byte-for-byte 不变」。
3. **C2/C3/C7 不做「config 变量参数化」**（关键实测修正）：Jinja2 变量值**不递归渲染** `{{ }}` 占位符。C2 核心态度段（14 处 `{{ user_pronoun }}`）、C3 情绪规则（每行 `{{ user_name }}`/`{{ user_pronoun }}`）、C7 主动观都与占位符交织，迁移到 config 后占位符失效、Elysia 行为被破坏。**故 C2/C3/C7 的真正的去 Elysia 化留给 B-2 的 per-role 模板机制。**
4. **C2 只修第 18 行字面 `她`**：`此刻她只注视着我` → `此刻{{ user_pronoun }}只注视着我`。已验证 Elysia（user_pronoun=她）输出 byte-for-byte 不变，冷角色修正为 `你`。这是 B-1 唯一的 C2 改动。
5. **`speaking_style=""` 的角色 C6 段真消失**：方案 X 把 else 分支整体迁移，无剩余原文。`speaking_style=""` → C6 段真消失（wrapper 在 conditional 内层，`if result` 判空）。B-1 用「Elysia 必须配 + 冷快照更新」覆盖。当前项目只有 Elysia 一个完整角色，无其他受影响角色。
6. **F2/F3 的字面 `她` 是示例场景指代，非用户代词泄漏**：`她好像很开心`/`她居然注意到了`/`她果然很聪明呢` 是示例对话里的第三方用户，不是「user_pronoun 未覆盖」的泄漏。改它们改变 Elysia 示例文本，不是 B-1 目标。
7. **B-1 不新增 `relationship_style`/`emotion_profile`/`proactive_style` property**（YAGNI）：B-1 只用已有的 `speaking_style`。这三个字段是 B-2 才消费的，B-1 不加避免未定义行为。
8. **`SpeakingStyleSection` 默认 `speaking_style` 回退 L1 默认（`None` → `_C6_L1_DEFAULT` 硬编码常量）**（关键 critical 修复）：`compression.py`/`test_boundary.py`/`test_full_render.py` **裸构造**该 section（不传 `speaking_style`）。若默认是 `""`，B-1 后这些路径渲染空 C6 → `_refresh_c6` 静默失效（Elysia L2 C6 演化停止）。**修复：构造器默认 `speaking_style=None` → 回退到 `_C6_L1_DEFAULT`（改造时把当前 C6 else 分支正文固化为 `speaking_style.py` 的硬编码常量），保裸构造行为不变。** ⚠️ **`_C6_L1_DEFAULT` 必须是硬编码常量，不能运行时从模板 else 提取**（模板改造后无 else 分支，提取空 = 自指矛盾）。区分 `None`（裸构造→L1 默认）与 `""`（`PromptAssembler` 显式传 cold 空→C6 消失）。
9. **Elysia 快照不锁 C1**：`elysia_config` 的 C1 段是 `cold_engine_mock.render_c1()` 覆盖的冷水人格，非 Elysia L1。B-1 守门只锁 C6/C2 参数化行为不变，C1 未纳入本批改动。这是 B-1 守门的明确局限。

---

## 8. 明确不做（白名单之外的）

- 不动辅助 Prompt（3 raw `.replace()` + 2 Jinja2）的 Elysia 泄漏；
- 不建 Cyrene config/personality.md；
- **不改 `core/personality/compression.py` 逻辑**——只靠 `SpeakingStyleSection` 默认 `speaking_style`（`None`→`_C6_L1_DEFAULT` 硬编码常量）兜底，使 `_refresh_c6` 裸构造该 section 时仍渲染 L1 默认（行为不变）；
- 不引入 `PromptProfile`/`CharacterRevision`/Compiler/validator；
- 不做 C6 框架/风格分离；
- 不翻转 Stage A 的 `test_leak_*` 断言方向（`in` → `not in`）；**仅 `test_leak_speaking_mode_structure`/`test_leak_fixed_persona` 改用 `elysia_config`**（否则冷角色 C6 消失必红）；
- **不改 `Module/system_prompt/tests/`、`core/personality/tests/`、`core/memory/tests/` 的测试文件**——`test_boundary.py`/`test_full_render.py` 的裸构造 `SpeakingStyleSection` 测试，靠 section 默认 `None`→`_C6_L1_DEFAULT` 兜底仍通过（不改测试文件）。

---

## 9. 退出条件（B-1 完成标准）

- [ ] `make lint` 通过；
- [ ] `make pyright` 通过；
- [ ] `make test-quick` 通过；
- [ ] `test_elysia_snapshot_unchanged` 通过（Elysia 渲染 == 改造前生成的 Elysia 专属快照）；
- [ ] `test_elysia_config_speaking_style_matches_template` 通过（config.yaml 的 `speaking_style` == `_C6_L1_DEFAULT` L1 默认硬编码常量）；
- [ ] `test_snapshot_matches_current`（冷角色 C6 真消失）通过；
- [ ] `test_12_section_order` / `test_frame_wrappers_present` 通过（改用 `elysia_config` 后）；
- [ ] Stage A 的 `test_leak_*` 系列通过——**`test_leak_speaking_mode_structure`/`test_leak_fixed_persona` 已改用 `elysia_config`**（否则冷角色 C6 空必红）；
- [ ] `Module/system_prompt/tests/integration/test_full_render.py` / `test_boundary.py` 的裸构造 `SpeakingStyleSection` 测试通过（section 默认 L1 兜底保行为不变）；
- [ ] `character/elysia/config.yaml` 已补 `personality.speaking_style` 字段（值为当前 C6 else 分支全文）；
- [ ] Elysia 专属快照 fixture 已生成并入库（**改造模板前**生成）；
- [ ] Elysia 现网渲染结果 byte-for-byte 不变（`test_elysia_snapshot_unchanged` 证明）。

---

## 10. 后续（阶段 B-2 预告）

阶段 B-2（内容落地 + 真正差异化）会：

1. **新建 Cyrene 完整角色**：`character/cyrene/config.yaml` + `personality.md`（L1 Core）+ `personality.speaking_style`（Cyrene 自己的 C6 风格）；
2. **C2/C3/C7/F2/F3 去 Elysia 化**（**per-role 模板机制**，非 config 变量——因它们与占位符交织，config 变量方案不可行）：每角色一份 C2/C3/C7 模板，或引入 `PromptProfile` 领域模型承载；
3. **辅助 Prompt 参数化**：3 处 raw `.replace()`（ExtractMemory/ConsolidationCompare/MemoryReflector）改成 Jinja2 `pm.render()` + 占位符替换；2 处 Jinja2（CharacterReflector/SpeakingStyleEdit）去 Elysia 化；
4. **C6 框架/风格分离**（方案 Y）：把 C6 的 A 类框架与 B 类风格重新划分，模板保留框架骨架；
5. **Stage A 泄漏测试翻转**：`test_leak_*` 断言从 `in result` 反转为 `not in result`，作为 B-2 退出门禁；
6. **`CharacterDefinition/Revision/PromptProfile` 领域模型**（可选，若 B-2 需要完整的角色 CRUD）。

届时 B-1 建立的 `elysia_config` / Elysia 专属快照 / `speaking_style` 变量可直接复用。
