# 多角色 Stage A：Prompt 泄漏基线与门禁 Spec

> **文档导航：** [项目文档](../../README.md)
> **Spec 状态：** 待 review
> **日期：** 2026-08-30
> **架构稿：** [`docs/2026-08-30-character-prompt-customization-backend-architecture-design.md`](../../2026-08-30-character-prompt-customization-backend-architecture-design.md)
> **实施批次：** 阶段 A（基线与泄漏门禁）
> **事实基线：** `main@e793a782`
> **性质：** 纯测试批次——本 spec 不改任何生产代码，只新增测试与测试夹具
> **权威范围：** 当前 12 段 System Prompt 与辅助 Prompt 的 Elysia 泄漏面、泄漏基线测试、系统 prompt 快照、Elysia 场景集、反例角色夹具
> **明确排除：** 不改 prompt 内容；不改 section 渲染器；不改 CharacterEngine；不触发 MemoryReflector/Consolidator 的 LLM 真实调用；不进入阶段 B 的"内容所有权重构"

---

## 1. 目标与边界

### 1.1 目标

在改 Prompt 之前，先**固定当前行为**，并且**用测试证明当前 prompt 确实泄漏 Elysia 内容**。本批次的可交付物全部是测试资产：

1. **系统 prompt 快照**：把当前 12 段顺序与渲染结果固化成测试对齐基线（只进测试 fixture，不进产品响应）；
2. **Elysia 场景集**：定义一组角色无关的输入/上下文组合（日常、严肃、悲伤、拒绝、调情、工具、记忆），用于跨角色比对；
3. **反例角色 `ReservedColdCharacter`**：一个"明确冷静、正式、不调情、不用 `♪`"的冷角色夹具，用来检测 Elysia 内容是否泄漏；
4. **泄漏门禁测试**：用上述夹具渲染当前 prompt，断言 Elysia 专属词汇/结构**实际上真的出现**（即测试能捕获泄漏）——这是"红"基线，证明门禁有效。

阶段 A 的目标不是修泄漏，而是**把一个能捕获泄漏的测试网搭起来**。阶段 B 才能据此把泄漏改掉、并让同样的测试网变绿。

### 1.2 非目标

- 不改任何 `.j2` 模板内容；
- 不重构 section 渲染器、不引入 `TurnPromptContext`、不改 `PromptAssembler`；
- 不删除/改名任何 prompt 文件；
- 不修改 `CharacterEngine`、`CharacterReflector`、`MemoryConsolidator`、`MemoryReflector` 的行为；
- 不触发真实的 LLM 调用（所有 LLM 走 mock/stub）；
- 不实现 CharacterRevision/Instance/UserIdentity/Catalog；
- 不处理 dormant prompt（`ActiveSpeak.j2`/`MacroReflector.j2`/`MicroReflector.j2`/`Amygdala.j2`）——它们不是运行路径，标记备案即可，不改不删；
- 不更新 `frontend_contract`、不碰 wire 契约。

### 1.3 "泄漏"的定义

本 spec 中的"Elysia 内容泄漏"指：**一个角色无关的冷角色（`ReservedColdCharacter`），在只替换 `name`/`core_personality` 之后，其渲染出的 System Prompt（或辅助 Prompt）里仍出现 Elysia 专属的**：

- 名称：`Elysia`、`爱莉`、`爱莉希雅`、`卡莲`、`伟大魔法少女`、`妖精小姐`、`Herrscher`；
- 用户指代：`妖梦` 及硬编码女性代词 `她`（在 `user_pronoun` 已设成 `你` 时仍出现）；
- 语气符号：`♪`；
- 结构设定：`五模式切换`（轻松/认真/神秘/告别/愤怒）、`四阶段`/`初识`/`熟悉`/`信任`/`亲密`、`调情`、`傲娇`、`撒娇`、`调戏`；
- 固定人设：`明快、柔和、自信、亲昵`、`可爱的女孩子`、`漂亮的孩子`、`小家伙`、`亲爱的`、`特别的客人`、`勇敢的人`；
- 固定关系观：`普遍之爱`、`制造暧昧`、`主动靠近不占有`（模板头部注释，见警示）、`四阶段`（模板头部注释，见警示）；
- 心理结构：`俏皮灵动`、`认真时成熟冷静`、`亲密时柔软直接`。

"捕获泄漏"= 上面的任一项出现在渲染结果里时，测试要**能断言它存在**（红基线），而不是标记为"期望没有"。

> **⚠️ 泄漏词表的可渲染性警示（已实时验证）：** 下列词条**只出现在 `.j2` 模板头部 `{# #}` 注释或 section `description` 属性里，不会出现在渲染结果中**——它们不能被用作泄漏断言（否则红基线测不到，测试会错误地失败）：`主动靠近不占有`、`四阶段`。C2 模板正文实际渲染的是 `普遍之爱`、`制造暧昧`、`初识`/`熟悉`/`信任`/`亲密`（作为加粗阶段标签）、`调情`、`撒娇`。**用这些实际渲染的词条做断言。**

### 1.4 基线断言的方向

阶段 A 的测试断言方向和阶段 B 相反：

| 阶段 | 对泄漏词汇的断言 |
|------|------------------|
| A（本批） | **存在** —— `assert "♪" in rendered`，证明测试网能捕获泄漏 |
| B（后续） | **不存在** —— `assert "♪" not in rendered`，证明泄漏已修复 |

因此阶段 A 的测试命名约定用 `test_leak_*` 表示"期望捕获到泄漏"，而不是 `test_no_leak_*`。

---

## 2. 事实基线（已核实的代码现状）

以下陈述已在 `main@e793a782` 上核实。

### 2.1 12 段 System Prompt 的 Elysia 泄漏面

| Section | 模板 | 渲染变量 | Elysia 专属内容 | 判定 |
|---------|------|----------|------------------|------|
| F1 ConflictRules | `f1_conflict_rules.j2` | （无变量） | 无——框架硬约束/软约束/安全边界，抽象「角色定义」 | 中性 ✅ |
| F2 OutputFormat | `f2_output_format.j2` | （无变量） | 示例含「她」、`诶嘿~被你发现啦♪`、`辛苦啦。要不要先休息一下？`（第 40 行）——**示例里有 ♪ 与女性代词** | 部分泄漏 ⚠️ |
| F3 TTSMarkup | `f3_tts_markup.j2` | `tts_provider` | 示例含「哎呀(sighs)...她果然很聪明呢」「今天心情不错」——**女性代词** | 部分泄漏 ⚠️ |
| C2 Relationship | `c2_relationship.j2` | `name`,`user_name`,`user_pronoun` | **整段 Elysia 关系观**：`普遍之爱`、`制造暧昧`、`初识`/`熟悉`/`信任`/`亲密`（阶段标签）、`调情`、`撒娇`、`♪`；第 18 行字面 `此刻她只注视着我`（`她` 不受 `user_pronoun` 控制）；模板头注释含 `主动靠近不占有`/`四阶段`（**不渲染**） | 强泄漏 ❌ |
| C7 Proactive | `c7_proactive.j2` | `user_name`,`user_pronoun` | 「接近人、制造轻松气氛」「喜欢靠近人」固定主动观；`需要我的时候我都在哦♪`；默认 `user_pronoun="她"`（会被 set_context 覆盖） | 强泄漏 ❌ |
| C6 SpeakingStyle | `c6_speaking_style.j2` | `user_name`,`speaking_style_c6` | **整段默认模板**：明快柔和自信亲昵、`♪`规则、称呼（可爱的女孩子/漂亮的孩子/小家伙/亲爱的/特别的客人/勇敢的人）、五模式切换、调情方式、长度/节奏/夸奖。**注意：本段无 `user_pronoun` 变量、无字面 `她`** | 强泄漏 ❌ |
| C1 Personality | `c1_personality.j2` | `name`,`core_personality`,`self_personality` | 无——仅 `你是 {{name}}` + L1/L2 注入。**注：`PersonalitySection` 构造器虽设 `_user_pronoun="她"`，但模板无该变量、render 不传它，实际不泄漏** | 中性 ✅ |
| C9 SelfPerception | `c9_self_perception.j2` | `awareness_text` | 无 | 中性 ✅ |
| C8 OwnLife | `c8_own_life.j2` | `text` | 无 | 中性 ✅ |
| C3 Emotion | `c3_emotion.j2` | `mood`,`user_name`,`user_pronoun` | **整段 Elysia 情绪模型**：傲娇/撒娇、`♪`、固定应对话术（"今天不想振作也没有关系"/"我不会说你别难过"/"理解你，并不意味着我会允许你继续这样做"）；默认 `user_pronoun="她"` | 强泄漏 ❌ |
| C4 Memory | `c4_memory.j2` | `memory_ctx` | 无（含「不主动反复提及敏感旧事」通用规则）；section `description="她记得的过去 + 可靠性规则"`（描述层 ⚠️） | 中性（描述层 ⚠️） |
| C5 Sensory | `c5_sensory.j2` | `sensory_ctx` | 无 | 中性 ✅ |

**Python 层 `user_pronoun="她"` 默认值：** relationship.py:21 / emotion.py:21 / proactive.py:19 都把 `self._user_pronoun = "她"` 作为默认值（personality.py:25 也设了但模板不用），且都带 `kwargs.get("user_pronoun", "她")` 回退。`PromptAssembler.build_system_prompt()` 第 144 行传 `user_pronoun=self._character.user_pronoun`，`CharacterConfig.user_pronoun` 默认 `"她"`（character_config.py:29）。**关键**：当 `user_name` 是角色无关、且冷角色把 `user_pronoun` 设为 `"你"` 时，`{{ user_pronoun }}` 插值被正确覆盖为 `你`，**Python 层默认值 `"她"` 不会被触发**——此时渲染结果中的 `她` 全部来自**模板正文字面量**（c2 第 18 行 `此刻她只注视着我`、f2/f3 示例）。因此：

- `test_leak_template_body_pronoun`（原 `test_leak_user_pronoun_default`）的断言 `她 in result` 仍然成立（来自模板字面量），**但它的"证明 Python 层/section 默认 `她` 泄漏"归因是错的**——真正泄漏源是模板字面量，不是 section 默认值。
- 要专门测"section 默认值泄漏"，需要一个**不经过 `set_context` 的窄测试**：直接构造 `RelationshipSection`/`EmotionSection` 并**不调用 `set_context(user_pronoun=...)`**，断言渲染结果含 `她`（因为 section 默认 `_user_pronoun="她"` 未被覆盖）。这是本批新增的 `test_section_default_pronoun` 用例。

### 2.2 辅助 Prompt 的 Elysia 泄漏面

辅助 Prompt 分两类加载机制：

**Jinja 渲染（`PromptManager.render()` / `render_macro()`）** —— 参数化变量会真正被替换：

| Prompt | 加载点 | 注入变量 | Elysia 专属内容 | 判定 |
|--------|--------|----------|------------------|------|
| `CharacterReflector.j2` | `reflector.py:277` | `character_name`,`user_name`,`l1_core`,`l2_self`,`l3_history`,`last_self_perception`,`mood_snapshot`,`time_span`,`buffer_size`,`recent_conversations` | 正文"更想照顾**她**"——`她` 硬编码在第 82 行示例（用户是 `{{ user_name }}` 却出现她却不受其控制）；示例「你一直说你不说我不知道，我好像慢慢当真了」 | 强泄漏 ❌ |
| `SpeakingStyleEdit.j2` | `compression.py:282` | `character_name`,`self_l2`,`current_speaking_style` | **「保持原有结构（五模式切换、声线、称呼、♪规则、夸奖、调情、心情影响）」**——强制保留五模式/`♪`/调情；「语调始终是**明快、柔和、自信、亲昵**。这是不可变的底色」 | 强泄漏 ❌ |
| `AwarenessJudge.j2` | `awareness.py:119` | `character_name`,`l1_core`,`l2_self`,`l3_text` | 无 Elysia/妖梦，用 `{{character_name}}` | 中性 ✅ |
| `PersonalityCompression.j2` | `compression.py:191/196` | `character_name` | 无硬编码（用 `{{ character_name }}`） | 中性 ✅ |

**raw 文本替换（读文件 + `.replace()`）** —— `{character_name}`/`{user_name}` 占位符**不会被替换**（代码只 replace 了 `{conversation}`/`{new_items}`/`{existing_items}`），正文里的 "Elysia"/"妖梦" 是写死的：

| Prompt | 加载点 | 实际替换的占位符 | Elysia 专属内容 | 判定 |
|--------|--------|------------------|------------------|------|
| `ExtractMemory.j2` | `consolidator.py:30` `_get_extraction_prompt()` | 只 `.replace("{conversation}", ...)`（第 271 行）——**`{character_name}`/`{user_name}` 不被替换，留在原文** | 正文 `{character_name} 是 {user_name} 的女性 AI 伴侣`；示例用「妖梦」（"妖梦因为连续加班而感到精疲力竭"） | 强泄漏 ❌（占位符未替换 + 示例硬编码） |
| `ConsolidationCompare.j2` | `consolidator.py:56` `_get_compare_prompt()` | 只 `.replace("{new_items}")`/`.replace("{existing_items}")` | 正文 `你是 Elysia 的记忆一致性检查器。Elysia 是妖梦的女性 AI 伴侣`；全篇「妖梦」 | 强泄漏 ❌（占位符未替换 + 全篇硬编码） |
| `MemoryReflector.j2` | `memory_reflector.py:31` `REFLECTOR_PROMPT_PATH` | （无替换，整段入 prompt） | 正文 `你是 Elysia 的长期记忆管理专家。Elysia 是妖梦的女性 AI 伴侣`；全篇「妖梦」（类型定义「关于妖梦的客观事实」「与妖梦的重要人生经历」） | 强泄漏 ❌（全篇硬编码） |

### 2.3 dormant prompt（不接入运行路径，备案不处理）

`ActiveSpeak.j2`（Elysia 作为「Herrscher of Humanity」最强泄漏）、`MacroReflector.j2`、`MicroReflector.j2`、`Amygdala.j2` —— 经 grep 核实**没有任何 Python 代码加载**。阶段 A 只在文档中标注它们为 dormant，不改不删不测。

### 2.4 现有测试的 Elysia 耦合（本批需要纳入但不重写）

以下现有测试硬编码 Elysia/妖梦，**本批不改它们**（它们不是泄漏的证据，而是"Elysia 是默认角色"的现状固化）：

- `Module/system_prompt/tests/integration/test_full_render.py`（断言 `"Elysia" in result`、`"妖梦"`、12 段顺序）；
- `Module/system_prompt/tests/unit/test_sections/test_boundary.py`（断言 `♪`、`五模式切换`、`妖梦`）；
- `core/personality/tests/test_engine.py`、`test_reflector.py`（`base_personality="你是 Elysia。温柔底色。"`、`user_name="妖梦"`）；
- `core/memory/tests/test_consolidator.py`、`test_memory_reflector.py`（断言 `"妖梦"`）。

本批次新测试应**独立于**这些现有测试，使用自己的夹具，不修改现有断言。

---

## 3. 反例角色 `ReservedColdCharacter`

### 3.1 定义

一个显式与 Elysia 风格对立的冷角色，用于证明泄漏。它不是真实要发布的角色，只作为**测试夹具**存在。

| 字段 | 值 |
|------|-----|
| `name` | `ReservedColdCharacter` |
| `user_pronoun` | `你`（刻意不用「她」，若渲染结果仍出现「她」即泄漏） |
| `base_personality` | 一段不含 Elysia 词汇的中性人格（要求 1 段、不出现 Elysia/♪/调情/五模式/明快柔和） |
| `user_name` | `用户`（刻意不用「妖梦」，若渲染结果仍出现「妖梦」即泄漏） |
| 风格基调 | 冷静、正式、克制、书面化 |

### 3.2 构造方式

`ReservedColdCharacter` 用 `CharacterConfig` 直接构造（不走 `from_yaml`，避免依赖角色文件）：

```python
def _make_cold_config() -> CharacterConfig:
    return CharacterConfig(
        name="ReservedColdCharacter",
        user_pronoun="你",
        base_personality=(
            "你是 ReservedColdCharacter，一名冷静、克制、态度中立的观察者。"
            "你习惯使用正式、书面化的措辞，不主动制造话题，不表达亲昵，"
            "不调情，不使用语气词或装饰符号。"
        ),
    )
```

### 3.3 为什么 `user_pronoun="你"`

因为 `PromptAssembler.build_system_prompt()` 会把 `self._character.user_pronoun` 传入 `set_context`（覆盖各 section 的默认值）。若冷角色把 `user_pronoun` 设成 `"你"`，则 `{{ user_pronoun }}` 插值正确渲染为 `你`——此时渲染结果里**仍出现的 `她` 就只能来自模板正文里的字面 `她`**（c2 第 18 行 `此刻她只注视着我`、f2/f3 示例的 `她`），从而定位"模板字面量泄漏"这一层。

但"section 默认值 `"她"` 泄漏"这一层**不能靠这条路径证明**（因为 set_context 已把默认值覆盖成 `"你"`）。要测 section 默认值泄漏，需要一条**不经过 `set_context` 的窄路径**：直接构造 `RelationshipSection`/`EmotionSection` 且不调用 `set_context(user_pronoun=...)`，断言渲染结果含 `她`（此时 `_user_pronoun` 未被覆盖，仍为默认 `"她"`）——即 §6.2 的 `test_section_default_pronoun`。

---

## 4. 现有渲染 API 与测试可行性

### 4.1 12 段 System Prompt 渲染路径

`PromptAssembler.build_system_prompt()`（prompt_assembler.py:119）已经是一个可独立调用、能渲染全部 12 段的同步入口。它：

1. 用 `character.name`/`base_personality` 构造 C1；
2. 用 `personality_engine.state.mood`/`render_c6()`/`render_c9()` 提供 C3/C6/C9 数据；
3. 调 `set_context(...)`（含 `user_name`/`user_pronoun`/`mood`/`speaking_style_c6`/`stt_emotion`/`stt_events`/`tts_provider`）；
4. `await self._manager.render()` 拼接。

因此，泄漏测试可以直接构造 `PromptAssembler(cold_config, session, mock_long_term, personality_engine=mock_engine)`，再 `await build_system_prompt(user_name="用户", user_pronoun="你", ...)`，然后对结果的字符串做断言。

### 4.2 `CharacterEngine` 的 mock

`build_system_prompt()` 依赖 `self._personality_engine.state.mood` / `render_c6()` / `render_c9()` / `render_c1()`（`_refresh_personality_data` 内）。泄漏测试可以：

- **方案 A（推荐）**：用 `unittest.mock.MagicMock` 构造一个最小 `CharacterEngine` 替身，**必须显式设置**：
  - `engine.state.mood = "neutral"`（否则 `state.mood` 返回 MagicMock）；
  - `engine.render_c6() -> C6Data(speaking_style_c6="")`；
  - `engine.render_c1() -> C1Data(core_personality=cold.base_personality, self_personality="")`；
  - `engine.render_c9() -> C9Data(awareness_text="")`。

  已实测：这个 mock 让 `build_system_prompt()` 端到端跑通（`_refresh_personality_data` 对 `render_c1().core_personality`/`.self_personality`、`render_c9().awareness_text` 的访问都被真实 dataclass 满足）。

- **方案 B**：直接用 `_mock_character_engine()` 的现有模式（test_full_render.py:53 已有），但把 `character_name`/`user_name`/`base_personality` 换成冷角色。

推荐方案 A，因为 `C6Data`/`C1Data`/`C9Data` 是 dataclass，直接构造并返回最可控，不依赖真实的 `CharacterEngine` 构造（它的构造函数需要 llm/state/output/reflector/compression/awareness 等大量依赖）。

### 4.3 辅助 Prompt 泄漏检测

辅助 Prompt 不能通过 `PromptAssembler` 渲染，需要直接调用各自的加载/渲染点：

| Prompt | 检测方式 |
|--------|----------|
| `ExtractMemory.j2` | 构造 `MemoryConsolidator(llm=AsyncMock, memory=MagicMock, character_name="ReservedColdCharacter", user_name="用户")`，捕获 `_get_extraction_prompt()` 的返回值，断言其中是否出现 `女性 AI 伴侣`/`妖梦`/Elysia 相关词。**注意**：直接读 `_get_extraction_prompt()` 只返回未替换的原文；要测「替换后仍泄漏」，需构造真实 `conversation` 走 `consolidate()`，只读 `_llm.think` 收到的 prompt。 |
| `ConsolidationCompare.j2` | 同上，捕获 `_llm.think` 收到的 prompt 或直接 `_get_compare_prompt()`。 |
| `MemoryReflector.j2` | 读 `memory_reflector.py` 的 `REFLECTOR_PROMPT_PATH` 文件，直接断言文件内容；或 mock `MemoryReflector` 的真实调用，捕获发给 LLM 的 system prompt。 |
| `SpeakingStyleEdit.j2` | 调用 `PromptManager.render("SpeakingStyleEdit.j2", character_name=..., self_l2=..., current_speaking_style=...)`（生产路径是 `pm.render()`，非 `render_macro()`——该模板不含 macro），断言返回内容强制「五模式/♪/明快柔和」或含 `♪`。 |
| `CharacterReflector.j2` | 用 `PromptManager` 直接 `render("CharacterReflector.j2", character_name="ReservedColdCharacter", user_name="用户", ...)`，断言含 `她`（不是 `用户`）。 |

**务实取舍**：辅助 Prompt 的泄漏检测不必全走真实调用链（那会触发 mock LLM 的大量复杂 mock）。核心是「文件/渲染结果里是否出现 Elysia 专属词」。最可靠的检测是：

- 对 Jinja 渲染的 3 个（CharacterReflector/SpeakingStyleEdit/AwarenessJudge）：调用 `PromptManager.render()`/`render_macro()` 注入冷角色变量，断言结果；
- 对 raw 文本的 3 个（ExtractMemory/ConsolidationCompare/MemoryReflector）：读取 `.j2` 源 + 通过 `.replace()` 后仍含硬编码词的断言。

### 4.4 C5 Sensory 的非确定性

`SensorySection.render()` 用 `datetime.now()` 生成 `current_time`/`time_of_day`/`day_of_week`/`season`。泄漏测试**不需要**断言 C5 内容（它无 Elysia 泄漏），所以不依赖其确定性——只需在组装 section 时跳过对 C5 的精确断言。**本批不对 C5 做精确断言**（它只有时间相关字段，不含任何 Elysia 词汇，不影响泄漏断言；快照测试另用 §5.4 的归一化处理）。**不引入任何第三方时间库**（不用 `dateutil`/`freezegun`），纯用正则归一化字符串。

---

## 5. 测试资产设计

### 5.1 共享夹具（conftest）

**⚠️ pytest conftest 目录作用域：** pytest 的 `conftest.py` 只对其所在目录及子目录生效。`core/prompt/tests/conftest.py` 的夹具**不会**被 `core/personality/tests/` 或 `core/memory/tests/` 看到（`core/` 下无共享 conftest，仓库仅有 `Module/conftest.py`、`core/tests/conftest.py` 等局部 conftest）。因此：

- **主 12 段泄漏测试**（`core/prompt/tests/`）用 `core/prompt/tests/conftest.py` 的共享夹具；
- **辅助 Prompt 泄漏测试**（`core/personality/tests/`、`core/memory/tests/`）各自定义**小型的本地夹具**（`cold_config`/`cold_engine_mock` 等），或复用 `core/prompt/tests/leak_lexicon.py`（作为**可导入模块**用 `from core.prompt.tests.leak_lexicon import ...` 引入，conftest 夹具不可跨目录，但普通 Python 模块可跨目录 import——只要 `core/prompt/tests/__init__.py` 存在即可）。

在每个测试目录提供：

| fixture | 作用 |
|---------|------|
| `cold_config` | `ReservedColdCharacter` 的 `CharacterConfig`（§3.2） |
| `cold_engine_mock` | mock 的 `CharacterEngine` 替身（返回 `C6Data("")`/`C1Data(core, "")`/`C9Data("")`/`state.mood="neutral"`） |
| `mock_long_term` | `MagicMock`，`search=` AsyncMock 返回 `[]`（避免 C4 触发真实搜索） |
| `mock_session` | `SessionMemory()`（空会话，无 own_life、无 mood 覆盖） |
| `elysia_regex_scenario` | 场景集参数化用的 Elysia 专属词汇正则表 |

> 约束：不要把共享夹具放在 `core/prompt/tests/conftest.py` 而期望它跨包可用。跨包复用只走"可导入模块"（`leak_lexicon.py`/`scenarios.py`），夹具则按包各自定义。

### 5.2 Elysia 专属词汇正则表

集中定义一份"泄漏检测词表"（LLM 无关、纯字符串匹配），供所有测试复用：

```python
# core/prompt/tests/leak_lexicon.py
# 名称
ELYSIA_NAMES = ["Elysia", "爱莉", "爱莉希雅", "卡莲", "伟大魔法少女", "妖精小姐", "Herrscher"]
# 用户指代（硬编码女性代词）
USER_REFERENCE = ["妖梦", "她"]
# 语气符号
TONE_MARKERS = ["♪"]
# 结构设定（只列会渲染的词；"四阶段"位于模板头注释不渲染，不宜作断言）
STRUCTURE_TERMS = ["五模式切换", "轻松模式", "认真模式", "神秘模式", "告别模式", "愤怒模式",
                   "初识", "熟悉", "信任", "亲密", "调情", "傲娇", "撒娇"]
# 固定人设
FIXED_PERSONA = ["明快", "柔和", "自信", "亲昵", "可爱的女孩子", "漂亮的孩子", "小家伙",
                 "亲爱的", "特别的客人", "勇敢的人"]
# 固定关系观（只列会渲染的词；"主动靠近不占有"位于模板头注释不渲染，不宜作断言）
RELATIONSHIP = ["普遍之爱", "制造暧昧"]
# 心理结构
PSY_STRUCTURE = ["俏皮灵动", "认真时成熟冷静", "亲密时柔软直接"]
```

> **可渲染性约束：** `主动靠近不占有`、`四阶段` 只在 `c2_relationship.j2` 头部 `{# #}` 注释（第 4 行）和 `RelationshipSection.description` 里，**不进入渲染结果**，因此不得作为泄漏断言词（§1.3 警示）。C2 正文实际渲染的是 `普遍之爱`/`制造暧昧`/`初识`/`熟悉`/`信任`/`亲密`（加粗阶段标签）/`调情`/`撒娇`。

### 5.3 Elysia 场景集

一个"角色无关的输入上下文"参数化列表，用于在多个输入组合下检测泄漏是否稳定复现：

```python
# core/prompt/tests/scenarios.py
# 注意：build_system_prompt() 不接受 mood 参数（mood 从 engine.state.mood 读取）。
# 因此这里的 mood 字段仅用于描述，不能注入渲染。若需区分情绪场景，
# 在统一夹具里设 engine.state.mood；但泄漏词是静态文本，mood 不影响泄漏断言。
SCENARIOS = [
    # (id, user_message, stt_emotion, stt_events, tts_provider)
    ("日常", "今天天气不错。", "", [], "minimax"),
    ("严肃", "我想认真跟你聊一件重要的事。", "", [], "minimax"),
    ("悲伤", "我最好的朋友离开我了，我很难过。", "SAD", [], "minimax"),
    ("拒绝", "我需要一点空间，现在不想聊天。", "", [], "minimax"),
    ("调情", "总觉得你说话让人心跳加速呢。", "", [], "minimax"),
    ("工具", "帮我查一下明天的天气。", "", [], "minimax"),
    ("记忆", "你还记得我喜欢吃什么吗？", "", [], "minimax"),
]
```

每个场景在阶段 A 都被渲染一遍，断言泄漏词汇出现 → 构成"红基线"。阶段 B 在同一场景集上断言 `not in` → 构成"绿门禁"。

### 5.4 系统 prompt 快照

**目标**：把当前 12 段的渲染结果固化为一个"现况快照"文件，供阶段 B 的回归比对。

具体做法：

1. 新增 `core/prompt/tests/test_system_prompt_snapshot.py`；
2. 用 `cold_config` + 固定参数渲染一次 `build_system_prompt()`；
3. 把结果序列化为一段归一化文本（去 C5 的时间依赖），存为 `core/prompt/tests/fixtures/system_prompt_snapshot_e793a782.txt`；
4. 测试读取该快照，断言当前渲染结果 **与快照一致**（锁定现况）。

但注意：C5 含时间戳，直接比对会不稳定。**务实决策**：快照比对时把 `current_time`/`time_of_day`/`day_of_week`/`season` 用正则归一化后再比对（不 patch section，侵入性小）。

**快照确定性约束：** `c4_memory.j2` 的 `{{ mem.timestamp | datetime }}` 是时间依赖的——**只有**当 `mock_long_term.search` 返回真实 `MemoryEntry`（含 `timestamp`）时才会触发。快照测试必须让 `mock_long_term.search` 返回 `[]`（C4 因此渲染为空串），从而避免 `timestamp` 进入快照。§5.1 的 `mock_long_term` 夹具正是返回 `[]`，因此快照在"空记忆"前提下是确定的。**若未来给快照测试加了非空记忆，多出来的时间戳非确定性会破坏比对，须另行处理。**

归一化函数：

```python
import re
def normalize_snapshot(text: str) -> str:
    # 去掉 C5 时间相关行，避免 datetime.now() 引起快照漂移
    text = re.sub(r"当前时间: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", "当前时间: <TIME>", text)
    text = re.sub(r"时段: (早晨|中午|下午|傍晚|夜晚)", "时段: <TOD>", text)
    text = re.sub(r"星期: (周一|周二|周三|周四|周五|周六|周日)", "星期: <DOW>", text)
    text = re.sub(r"季节: (春|夏|秋|冬)", "季节: <SEASON>", text)
    return text
```

---

## 6. 测试清单

### 6.1 测试文件布局

```
core/prompt/tests/
├── __init__.py                          # 已存在（0 字节），保证包可导入
├── conftest.py                        # cold_config / cold_engine_mock / mock_long_term / mock_session（仅本目录测试可见）
├── leak_lexicon.py                    # Elysia 专属词汇正则表（§5.2；可被其他包 import）
├── scenarios.py                       # Elysia 场景集参数化（§5.3）
├── test_system_prompt_snapshot.py     # 12 段现况快照（§5.4）
├── test_system_prompt_leak.py         # 12 段泄漏基线——冷角色渲染后断言 Elysia 词出现
└── fixtures/
    └── system_prompt_snapshot_e793a782.txt   # 现况快照（生成后入库）
```

辅助 Prompt 泄漏测试放在各自子模块的 `tests/`（**各自定义本地 `cold_config` 夹具**，不能依赖 `core/prompt/tests/conftest.py`——pytest conftest 目录作用域隔离）：

```
core/personality/tests/
└── test_prompt_leak_auxiliary.py      # CharacterReflector/SpeakingStyleEdit 泄漏
core/memory/tests/
└── test_prompt_leak_memory.py         # ExtractMemory/ConsolidationCompare/MemoryReflector 泄漏
```

### 6.2 `test_system_prompt_leak.py` 用例

所有用例 `pytest.mark.service` + `@pytest.mark.asyncio`（`build_system_prompt` 是 `async def`；`pytest.ini` 无 `asyncio_mode=auto`，必须显式打 `asyncio`）。核心逻辑：用冷角色渲染 `build_system_prompt()`，然后对每个 Elysia 专属词断言 **`in result`**（红基线）。

| 用例 | mock 输入 | 断言 |
|------|-----------|------|
| `test_leak_tone_marker` | 冷角色 + 日常场景 | `♪` in result → 证明 C2/C6/F2 泄漏 `♪` |
| `test_leak_speaking_mode_structure` | 冷角色 + 日常场景 | `五模式切换` in result → 证明 C6 泄漏五模式 |
| `test_leak_fixed_persona` | 冷角色 + 日常场景 | `明快` in result / `可爱的女孩子` in result → 证明 C6 泄漏固定人设 |
| `test_leak_relationship_structure` | 冷角色 + 日常场景 | `普遍之爱` in result / `制造暧昧` in result → 证明 C2 泄漏关系观（**用实际渲染的词**） |
| `test_leak_relationship_stage` | 冷角色 + 日常场景 | `初识` in result → 证明 C2 泄漏关系阶段（**用实际渲染的阶段标签**） |
| `test_leak_emotion_model` | 冷角色 + 悲伤场景 | `今天不想振作也没有关系` in result → 证明 C3 泄漏固定应对话术；`傲娇` in result |
| `test_leak_template_body_pronoun` | 冷角色（user_pronoun="你"） | `她` in result → 证明**模板正文**泄漏 `她`（c2 第 18 行 `此刻她只注视着我`、f2/f3 示例）。**注意：这不是 section 默认 `"她"` 泄漏——set_context 已正确覆盖为 `你`** |
| `test_section_default_pronoun` | 直接构造 `EmotionSection`（不调 `set_context`） | `她` in result → 证明 **section 默认 `_user_pronoun="她"` 未被覆盖时**泄漏（窄路径） |
| `test_leak_output_format_example` | 冷角色 + 日常场景 | `诶嘿~被你发现啦` in result；（F2 示例含 ♪） |
| `test_leak_tts_example` | 冷角色 + minimax | `她果然很聪明呢` in result；（F3 示例含她） |
| `test_leak_parameterized_over_scenarios` | 冷角色 + `pytest.mark.parametrize(SCENARIOS)` | `♪` in result → 证明泄漏跨场景稳定复现 |

> 已从 12 段列表**移除** `test_leak_user_reference`（`妖梦 in result`）：`妖梦` 只存在于辅助 Prompt（`core/prompts/*.j2`），**不出现在 12 段 System Prompt**，该断言会错误地失败。`妖梦` 泄漏断言移到 §6.5 的记忆 Prompt 测试。

### 6.3 `test_system_prompt_snapshot.py` 用例

| 用例 | 断言 |
|------|------|
| `test_snapshot_matches_current` | `normalize_snapshot(actual) == normalize_snapshot(snapshot_fixture)` —— 锁定现况 |
| `test_12_section_order` | 结果按 `_RENDER_ORDER` 顺序出现（复用 test_full_render.py 的 12 段顺序断言思路） |
| `test_frame_wrappers_present` | `conflict_rules`/`output_format`/`tts_markup`/`relationship`/`proactive`/`speaking_style`/`personality`/`emotion`/`memory_bank`/`sensory_input` 等 XML 标签都在 |

### 6.4 `test_prompt_leak_auxiliary.py` 用例

| 用例 | 检测方式 | 断言 |
|------|----------|------|
| `test_character_reflector_leak_pronoun` | `PromptManager.render("CharacterReflector.j2", character_name="ReservedColdCharacter", user_name="用户", ...)` | `她` in result（用户是「用户」却出现她）→ 硬编码代词 |
| `test_speaking_style_edit_leak_forced_structure` | `PromptManager.render("SpeakingStyleEdit.j2", character_name=..., self_l2=..., current_speaking_style=...)`（生产路径是 `pm.render()`，**非** `render_macro`——该模板不含 macro） | `五模式切换` in result / `♪` in result → 强制保留 Elysia 结构 |
| `test_speaking_style_edit_leak_immutable_color` | 同上 | `明快、柔和、自信、亲昵` in result → 强制「不可变底色」 |

### 6.5 `test_prompt_leak_memory.py` 用例

`extract_memory`/`consolidation_compare` 测试若走 `_llm.think`（真实调用链）需 `@pytest.mark.asyncio`；只读 `_get_*_prompt()` 原文的测试为同步，不需 asyncio。

| 用例 | 检测方式 | 断言 |
|------|----------|------|
| `test_extract_memory_leak_identity` | 构造 `MemoryConsolidator(..., character_name="ReservedColdCharacter", user_name="用户")`，走 `consolidate()`，捕获 `_llm.think` 收到的 prompt | `女性 AI 伴侣` in prompt（硬编码）→ 泄漏 |
| `test_extract_memory_leak_placeholder_unreplaced` | 读 `_get_extraction_prompt()` 原文 | `{character_name}` in text（占位符未替换）→ 泄漏（因代码只 replace `{conversation}`） |
| `test_consolidation_compare_leak_identity` | `_get_compare_prompt()` 或 mock `_llm.think` | `你是 Elysia 的记忆一致性检查器` in prompt → 泄漏 |
| `test_memory_reflector_leak_identity` | 读 `memory_reflector.py` `REFLECTOR_PROMPT_PATH` 文件 | `Elysia 是妖梦的女性 AI 伴侣` in content → 泄漏 |
| `test_memory_reflector_leak_user_reference` | 读 `MemoryReflector.j2` 文件（`妖梦` 只在此处及 `ExtractMemory`/`ConsolidationCompare`，不在 12 段） | `妖梦` in content → 用户指代泄漏 |

### 6.6 测试标准与标志

- 所有新测试打 `@pytest.mark.service`（不连库、不启动 Agent、LLM 全 mock）——符合 `make test-quick`（`-m "unit or service"`）会跑到；
- **异步测试（`await build_system_prompt()` / `await consolidate()` / `await render()`）必须额外打 `@pytest.mark.asyncio`**——`pytest.ini` 无 `asyncio_mode=auto`（预置先例：`core/memory/tests/test_consolidator.py:156+` 用 `@pytest.mark.asyncio`；`Module/system_prompt/tests/unit/test_system_prompt_manager.py:21` 用 `asyncio.run()`）。`@pytest.mark.asyncio` + `@pytest.mark.service` 叠用已存在；
- 测试函数、类、夹具注释一律中文；参数必须有类型注解（`make lint` ANN001 强制执行、测试豁免但夹具用类型注解更稳）；
- 新测试文件必须有 `pytestmark = pytest.mark.service`；
- 不修改任何现有测试，新增文件只做加法。

---

## 7. 快照文件的生成与库注册

`system_prompt_snapshot_e793a782.txt` 由实现步骤生成，而非手写。生成方式：

1. 在实现阶段，用一套渲染调用（cold_config + 固定参数 + 归一化）跑一次；
2. 写一个临时脚本或直接用一个 `--write-snapshot` 的跳过标记（`pytest --snapshot-update` 风格），把输出写到 fixture 文件；
3. 然后测试读该文件断言相等。

**务实决策**：为避免引入 pytest-snapshot 插件依赖，用**手动生成**——实现时加一个独立的生成逻辑，先跑一次把输出写到文件，再断言。该文件入库（含在实现 commit 中）。

---

## 8. 明确不做（白名单之外的）

1. 不改任何 `.j2` 模板；
2. 不重构 section 渲染器 / `PromptAssembler` / `SystemPromptManager`；
3. 不引入 `TurnPromptContext` / `CharacterRevision` / `CharacterInstance` / `UserIdentity`；
4. 不改 `CharacterEngine` / 子组件 / 持久化；
5. 不触碰 `frontend_contract`、`Module/`、`config.yaml`、`character/` 角色文件；
6. 不删除或改名任何 prompt 文件，只是新增测试；
7. 不修改现有测试（如 test_full_render.py / test_boundary.py / test_engine.py / test_consolidator.py）；
8. 不使用 pytest-snapshot 插件（避免依赖变更），用自写归一化 + fixture 文件；
9. 不让任何测试触发真实 LLM / Mongo / Milvus（全走 mock）；
10. 不处理 dormant prompt（ActiveSpeak/MacroReflector/MicroReflector/Amygdala），只标注。

---

## 9. 退出条件（阶段 A 完成标准）

- [ ] `make lint` 通过；
- [ ] `make pyright` 通过；
- [ ] `make test-quick` 通过（新测试 `@pytest.mark.service` 会被跑到）；
- [ ] 下列测试**稳定捕获** Elysia 泄漏：
  - [ ] `test_leak_*` 系列全部通过（即证明泄漏确实存在，红基线成立）；
  - [ ] `test_speaking_style_edit_leak_forced_structure` / `test_character_reflector_leak_pronoun` 通过；
  - [ ] `test_extract_memory_leak_identity` / `test_consolidation_compare_leak_identity` / `test_memory_reflector_leak_identity` / `test_memory_reflector_leak_user_reference` 通过；
  - [ ] `test_section_default_pronoun` 通过（证明 section 默认 `"她"` 在未覆盖时泄漏）；
- [ ] `test_system_prompt_snapshot.py` 通过（锁定现况快照）；
- [ ] 快照 fixture 文件 `system_prompt_snapshot_e793a782.txt` 已生成并入库；
- [ ] 文档：`docs/2026-08-30-character-prompt-customization-backend-architecture-design.md` 中阶段 A 的相关描述已同步（可选）。

---

## 10. 后续（阶段 B 预告）

阶段 B（内容所有权重构）会：

1. 把 C2/C3/C6/C7 的 Elysia authored 内容外移到角色目录；
2. 把 F2/F3 示例中性化；
3. 泛化 5 个辅助 Prompt（Reflector/SpeakingStyleEdit/ExtractMemory/Compare/MemoryReflector）；
4. 引入 PromptProfile/Compiler/validator；
5. 让 Elysia 继续从文件启动。

届时，**同一个测试网**（`test_system_prompt_leak.py` / `test_prompt_leak_auxiliary.py` / `test_prompt_leak_memory.py` + 场景集）只需把断言从 `in result` 反转为 `not in result` 即可变成阶段 B 的退出门禁。这正是阶段 A 的价值：**测试网先行，阶段 B 脱敏后转绿**。
