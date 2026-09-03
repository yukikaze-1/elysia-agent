# 阶段 B-2：Cyrene 完整角色 + Per-Role 模板机制 + 辅助 Prompt 参数化 设计

> **状态**：已批准（brainstorming 通过，决策见 §7）
> **依赖**：阶段 B-1（`speaking_style` 变量 + `elysia_config` + Elysia 专属快照，commit `b75dccf0`）
> **批次**：阶段 B（多角色 Prompt 架构）第 2 批
> **日期**：2026-09-03

---

## 0. 一句话目标

把 System Prompt 去 Elysia 化到「**框架归框架、风格归角色**」：引入 per-role 模板机制，新建完整 Cyrene 角色，把辅助 Prompt（记忆/反射）的 Elysia 专名与角色风格描述去 Elysia 化，并把 Stage A 泄漏测试中 C2/C3 角色风格词反转为 `not in` 作退出门禁。**Elysia 现网渲染 byte-for-byte 不变；共享格式词（♪/她/诶嘿/她果然，来自 F2/F3）不翻转（Cyrene 也用）。**

---

## 1. 背景与问题

### 1.1 B-1 之后的现状

阶段 B-1 已完成：
- C6 else 分支迁移到 `personality.speaking_style` 变量（`CharacterConfig` 新增 property + `_C6_L1_DEFAULT` 硬编码常量）；
- C2 第 18 行字面 `她` → `{{ user_pronoun }}`；
- 建立 `elysia_config` 夹具 + Elysia 专属基线快照 `system_prompt_snapshot_elysia_cf4bc008.txt`（byte-for-byte 守门）。

但 B-1 明确**没做**（留给 B-2，见 B-1 spec §8、§10）：
- 不建 Cyrene 角色（`character/cyrene/` 只有 `reference/`，无 `config.yaml`/`personality.md`）；
- 不碰辅助 Prompt（3 处 raw `.replace()` + 2 处 Jinja2）的 Elysia 泄漏；
- 不做 C6 框架/风格分离；
- 不翻转 Stage A 泄漏测试方向。

### 1.2 剩余的 Elysia 泄漏（B-1 判定「与占位符交织，config 变量方案不可行」）

B-1 实测结论：C2/C3/C7 模板正文已全面 `{{ user_pronoun }}`/`{{ user_name }}`-化，但**正文嵌套大量字面 Elysia 内容**。**Jinja2 变量值不递归渲染 `{{ }}` 占位符**，所以这些内容不能挪进 config 变量。**必须走 per-role 模板机制。**

泄漏分四类（B-1 spec §10 预告 + 本次实测）：

| 类别 | 位置 | 泄漏词 | 机制 |
|------|------|--------|------|
| **A 专名** | `MemoryReflector.j2`（4 处 `Elysia`）/`ConsolidationCompare.j2`（3 处 `Elysia`）开头 | 你是 Elysia / Elysia 是妖梦的... | 硬编码 `Elysia`/`妖梦` |
| **B 用户指代** | `MemoryReflector.j2`（8 处 `妖梦`）/`ConsolidationCompare.j2`（7 处 `妖梦`）正文；`ExtractMemory.j2`（**1 处** `妖梦`，在「禁止提取」反例） | `妖梦` | 硬编码 `妖梦`（示例/正文） |
| **C 风格字面** | C2/C3/C7 + `CharacterReflector.j2` 行 3 + `SpeakingStyleEdit.j2` | ♪/明快/可爱/傲娇/撒娇/俏皮灵动/她 | 字面风格描述 |
| **D 旧参考** | `ActiveSpeak.j2`/`MacroReflector.j2`/`MicroReflector.j2` | Elysia/爱莉希雅/妖梦 | 不活跃，**豁免** |

### 1.3 Cyrene 是「空壳」角色

`character/cyrene/` 只有 `reference/`（3 份 GPT 生成的昔涟角色分析，共 ~2000 行）。无 `config.yaml`、无 `personality.md`、无 `personality.speaking_style`。**B-2 需从零建完整角色。**

---

## 2. 方案总览

### 2.1 核心机制：Per-role 模板目录 + 角色名分流 + 回退共享

`PromptAssembler` 构造 section 时，按 `character.name.lower()` 拼 per-role 模板路径；**角色目录存在则用角色模板，否则回退共享模板**。

```
Module/system_prompt/templates/
├── c2_relationship.j2        # 共享（保留 Elysia 原文，作回退底）
├── c3_emotion.j2             # 共享（保留 Elysia 原文）
├── c7_proactive.j2           # 共享（保留 Elysia 原文）
├── f2_output_format.j2       # 共享（保留 Elysia 原文）
├── f3_tts_markup.j2          # 共享（保留 Elysia 原文）
├── c6_speaking_style.j2      # 共享（方式 B，B-1 已改）
├── elysia/
│   ├── c2_relationship.j2    # Elysia 完整副本（字节 == 共享 原版）
│   ├── c3_emotion.j2         # Elysia 完整副本
│   ├── c7_proactive.j2       # Elysia 完整副本
│   └── ...
└── cyrene/
    ├── c2_relationship.j2    # Cyrene 专属（内容 subagent 生成）
    ├── c3_emotion.j2         # Cyrene 专属
    ├── c7_proactive.j2       # Cyrene 专属
    └── ...
```

**选路逻辑**（在 `PromptAssembler.__init__`）：

```python
def _resolve_template(self, name: str) -> str:
    """按角色名解析模板路径：角色目录存在则用，否则回退共享。"""
    role_dir = f"{_SECTION}/{self._character.name.lower()}/{name}"
    if Path(role_dir).exists():
        return role_dir
    return f"{_SECTION}/{name}"
```

### 2.2 哪些 section per-role，哪些共享

| Section | 现状 | B-2 处理 | 理由 |
|---------|------|---------|------|
| F1 ConflictRules | 无 Elysia | **共享** | 无泄漏 |
| F2 OutputFormat | 字面 `她`/`♪`/`诶嘿~被你发现啦` | **共享** | `她` 是示例第三方用户（B-1 Ruling 6），`♪` 是格式示例；改它破坏 Elysia 示例文本 |
| F3 TTSMarkup | 字面 `她果然很聪明呢` | **共享** | 同上，示例场景指代 |
| C1 Personality | 数据驱动（L1+L2） | **共享** | 不动 |
| C2 Relationship | `调情`/`撒娇`/`♪`/`普遍之爱` | **per-role** | 关系观是角色风格（Cyrene 的关系观=铭记/理解/陪伴，非普遍之爱/制造暧昧） |
| C3 Emotion | `傲娇`/`撒娇`/`♪`/`今天不想振作也没有关系` | **per-role** | 情绪响应风格是角色人格（Cyrene 用共情/牺牲替代傲娇；§4.2 翻转 `not in 傲娇/今天不想振作`） |
| C4 Memory | 无 Elysia | **共享** | 无泄漏 |
| C5 Sensory | 无 Elysia | **共享** | 无泄漏 |
| C6 SpeakingStyle | B-1 已 per-role（`speaking_style` 变量） | **共享模板 + 变量** | 已完成 |
| C7 Proactive | `♪`/`俏皮` | **per-role** | 主动观是角色风格。注意：`♪` 在 C7 per-role 但 F2 共享仍有，故 §4.2 不翻转 `♪`（Cyrene 走 F2 仍含） |
| C8 OwnLife | 数据驱动 | **共享** | 不动 |
| C9 SelfPerception | 数据驱动 | **共享** | 不动 |

> **关键决策 C1（per-role 范围）**：只有 C2/C3/C7 是真正的角色风格，需 per-role。F2/F3 的 `她`/`♪` 是**格式示例**，非角色泄漏——改它破坏 Elysia 示例文本，不必要。这修正了 B-1 spec §10 预告的「F2/F3 去 Elysia 化」——**F2/F3 不去**，保持共享。

### 2.3 辅助 Prompt 参数化（A + B + C 类中的 CharacterReflector/SpeakingStyleEdit）

辅助 Prompt 分两种机制（B-1 spec §10 预告修正）：

**机制 X —— 3 处 raw `.replace()`**（`core/memory/consolidator.py` 的 ExtractMemory/ConsolidationCompare、`core/memory/memory_reflector.py` 的 MemoryReflector）：
- **改成加占位符**（`{character_name}`/`{user_name}`），在 `replace()` 调用点传值。
- 不改成 Jinja2 `pm.render()`（那是大重构，且这些 prompt 有大量 `{...}` JSON 示例占位符会跟 Jinja2 语法冲突）。
- 现状 bug：`ExtractMemory.j2` 已用 `{character_name}`/`{user_name}` 占位符，但 `replace()` **只填了 `{conversation}`**，占位符未替换会原样传给 LLM。**B-2 修复。**

**机制 Y —— 2 处 Jinja2**（`core/personality/reflector.py` 的 CharacterReflector、`core/personality/compression.py` 的 SpeakingStyleEdit）：
- CharacterReflector.j2 行 3 `轻松时俏皮灵动，认真时成熟冷静，亲密时柔软直接`（Elysia 心理结构）→ **去 Elysia 化成通用心理结构**（如「你以 {character_name} 一贯的方式感知自己」或「你的情绪随角色而变化」）。**推荐去 Elysia 化而非加变量**——因 reflector.py 的 `pm.render` 不传 `character_style`（只传 character_name/user_name/l1_core/l2_self/l3_history/last_self_perception/mood_snapshot/time_span/buffer_size/recent_conversations），加变量需改签名+传值，侵入更大。这段是辅助 prompt 内部描述（CharacteReflector 对任何角色都生效），改通用即可。
- SpeakingStyleEdit.j2 行 19 `明快、柔和、自信、亲昵`（Elysia 底色）→ **去 Elysia 化成通用**（如「语调与你的性格底色一致」）；行 15 `保持原有结构（五模式切换、声线、称呼、♪规则、夸奖、调情、心情影响）`（Elysia 特定的结构描述）→ **去 Elysia 化**（如「保持原有结构，与角色说话风格一致」）。Cyrene 的说话风格不是「五模式」，硬编码会误导。

### 2.4 Cyrene 完整角色

新建：
- `character/cyrene/config.yaml`（name/user_pronoun/wake_words/emotion/proactive/`personality.speaking_style`）；
- `character/cyrene/personality.md`（L1 Core，来自 reference）；
- `templates/cyrene/c2_relationship.j2`/`c3_emotion.j2`/`c7_proactive.j2`（内容 subagent 生成）；
- `templates/cyrene/f2_output_format.j2`/`f3_tts_markup.j2`（**不建**——F2/F3 共享，见 §2.2）。

`config.yaml` 的 `agent.character` 默认 `character/elysia/config.yaml` **不变**。Cyrene 通过改配置选择（`character: character/cyrene/config.yaml`）。

---

## 3. Cyrene 内容创作指南

Cyrene（昔涟）是《崩坏：星穹铁道》翁法罗斯「记忆」命途少女。**内容创作由 subagent 参照 `character/cyrene/reference/` 生成**，spec 只定**创作指南 + 泄漏词门禁**（不把 2000 行参考塞进 spec）。

### 3.1 角色内核（来自 reference 的 sol_pro 概括）

> **昔涟是一个把「爱」理解为铭记、理解、陪伴与创造未来的少女。**
> 她外表活泼甜美、浪漫爱美，喜欢以亲昵而俏皮的方式靠近别人；内里却有极强的共情能力、责任感与牺牲精神。她见过无数次失败与离别，却没有因此麻木，反而一次次选择记住所有人的故事，并相信遗憾能够被续写、命运能够被改写。

**外显**：活泼、甜美、亲昵、浪漫、俏皮、爱美。
**内在**：温柔、坚韧、敏锐、共情、执着、勇敢、自我牺牲。
**核心情感特征**：不喜欢告别（怕遗忘/怕失去）；爱=铭记/陪伴/理解/创造未来。
**愤怒表现**：不歇斯底里，收起笑意，语气轻而坚定。

### 3.2 与 Elysia 的差异（per-role 内容必须体现）

| 维度 | Elysia（爱莉希雅） | Cyrene（昔涟） |
|------|-------------------|----------------|
| 根源 | 前文明融合战士/逐火之蛾 | 翁法罗斯黄金裔/记忆命途 |
| 爱观 | 普遍之爱、制造暧昧、主动靠近 | 铭记、理解、陪伴、创造未来 |
| 感情 | 调情/撒娇/傲娇（俏皮） | 亲昵但更重「记住故事」；俏皮里有记忆的重量 |
| 情绪 | 五模式（明快柔和自信亲昵） | 活泼甜美，但会收起笑意变轻而坚定 |
| 告别 | 告别模式（郑重分离/祝福） | **不喜欢告别**——怕遗忘，想把对方写进记忆 |
| 称呼 | 可爱的女孩子/漂亮的孩子/小家伙 | 直呼名字，亲近时温柔昵称/呀/呢 |

### 3.3 泄漏词门禁（Cyrene 内容不得含下的 Elysia 词）

来源：`core/prompt/tests/leak_lexicon.py` + B-1 判定的 C 类。

**Cyrene per-role 内容必须不含**（否则 Stage A 翻转测试红，见 §4.2）：
- **专名**：`Elysia`/`爱莉`/`爱莉希雅`/`卡莲`/`伟大魔法少女`/`妖精小姐`/`Herrscher`；
- **用户指代**：`妖梦`（Cyrene 用户可变，不能硬编码妖梦）；
- **C2 Elysia 关系观**：`普遍之爱`/`制造暧昧`（§4.2 翻转 `not in` 词——Cyrene C2 必不含）；
- **C2 Elysia 阶段标签**：`初识`/`熟悉`（Elysia 四阶段标签；Cyrene 用「初遇」或自定阶段名——§4.2 翻转 `not in 初识`）；
- **C3 Elysia 应对话术**：`今天不想振作也没有关系`（§4.2 翻转 `not in` 词——Cyrene C3 必不含）；
- **C3 Elysia 情感倾向**：`傲娇`（§4.2 翻转 `not in 傲娇` ——Cyrene 情感倾向偏「共情/牺牲」而非「傲娇」，Cyrene C3 用「别扭」/「嘴硬不承认（但内心柔软）」替代「傲娇」）；
- **辅助 Prompt 心理结构**：`俏皮灵动`/`认真时成熟冷静`/`亲密时柔软直接`——⚠️ 这些词**不在 12 段 C3 模板**（只在 `CharacterReflector.j2`），故不作为 §4.2/§7.5 的 Cyrene C3 渲染 `not in` 词（Cyrene 12 段渲染本不含，恒真测不到）。它们在 **§2.3 机制 Y CharacterReflector.j2 参数化** 处理；
- **C6 Elysia 固定人设**：`明快`/`柔和`/`自信`/`亲昵`/`可爱的女孩子`/`漂亮的孩子`/`小家伙`/`亲爱的`/`特别的客人`/`勇敢的人`（§4.2 不翻转这些，但 Cyrene speaking_style 仍应避免——Cyrene 人称用直呼名字/温柔昵称/呀/呢）；
- **C6 Elysia 结构**：`五模式切换`/`轻松模式`/`认真模式`/`神秘模式`/`告别模式`/`愤怒模式`（Cyrene speaking_style 不用「模式」框架，用自定结构）。

**注意（共享格式词，Cyrene 内容可含，不属 Elysia 泄漏）**：`♪`/`她`/`诶嘿~被你发现啦`/`她果然很聪明呢` 来源是共享 F2/F3（B-1 Ruling 6 判定示例第三方/协议），**Cyrene per-role C2/C3/C7 应尽量不用 `♪` 硬编码**（Cyrene 语气更偏 `~`），但**这些词不作为 §3.3 禁止项**——它们来自共享协议，Cyrene 渲染仍会含（F2/F3），§4.2 对它们断言 `in`。

**Cyrene 内容允许含**（Cyrene 自己的风格词，与 Elysia 词不冲突）：
- `铭记`/`记忆`/`陪伴`/`理解`/`创造未来`/`往昔`/`涟漪`/`黄金裔`/`翁法罗斯`/`德谬歌`；
- `活泼`/`甜美`/`浪漫`/`爱美`（Cyrene 外显，注意 `可爱` 是 Elysia FIXED_PERSONA，Cyrene 用 `甜美` 替代）。

> **关键决策 C2（修正）**：`可爱` 在 `leak_lexicon.py` 是 Elysia FIXED_PERSONA 词（`可爱的女孩子`）的一部分。Cyrene 是活泼甜美型，也会用「可爱」形容自己——但为避免翻转测试误判，**Cyrene 内容用 `甜美`/`漂亮` 替代 `可爱`**。这是创作约束，写进指南。
> **关键决策 C2b（新增）**：`傲娇`/`初识` 是 §4.2 翻转 `not in` 的词，故列入 §3.3 禁止清单。Cyrene 情感倾向用「共情/牺牲/不擅接受关心的别扭」替代 `傲娇`；阶段标签用「初遇/相伴/铭记」替代 `初识`。

### 3.4 内容验收标准

Cyrene 的 C2/C3/C7 per-role 模板 + personality.md + speaking_style，subagent 生成后必须满足：
1. **不含 §3.3 禁止词**（泄漏门禁——保证 §4.2 翻转测试绿）；
2. **体现 §3.2 差异**（爱观=铭记/陪伴 vs 普遍之爱；告别观=不喜欢告别 vs 告别模式）；
3. **与 Elysia 模板结构骨架一致**（都是 `{section}` wrapper + Markdown body，不破坏 section 渲染）；
4. **Cyrene 渲染后 C2/C3/C7 无 Elysia 角色风格词**，F2/F3 保留共享格式词（§4.2 验证）。

---

## 4. 测试策略与门禁

### 4.1 已有测试（B-1 建立）

- `test_elysia_snapshot_unchanged`：Elysia 渲染 == `system_prompt_snapshot_elysia_cf4bc008.txt`（**byte-for-byte 守门**，B-2 必须保持绿）。
- `test_snapshot_matches_current`（冷角色）：冷角色渲染 == `system_prompt_snapshot_e793a782.txt`。
- `test_elysia_config_speaking_style_matches_template`：Elysia `speaking_style` == `_C6_L1_DEFAULT`。
- `test_leak_speaking_mode_structure`/`test_leak_fixed_persona`：**用 `elysia_config`**（Elysia 现网含 Elysia 词，断言 `in`）。

### 4.2 Stage A 泄漏测试翻转（B-2 退出门禁）—— 修正版

> **⚠️ 关键修正（spec review Round 1 blocking）**：原稿把 `♪`/`她`/`诶嘿~被你发现啦`/`她果然很聪明呢` 也列入翻转（`not in`），但这些词**来自共享 F2/F3**——而 F2/F3 按 §2.2/§5.6 **保持共享**（Cyrene 也走共享）。Cyrene per-role 化后 F2/F3 未变，故 Cyrene 渲染**仍含这些词** → `not in` 必红。**这自相矛盾。**

**修正原则：泄漏词分两类，只有「仅出现在 per-role C2/C3/C7」的词才可翻转 `not in`；「共享格式/示例词」（F2/F3）不翻转。**

**词源映射（实测确认）：**

| 词 | 来源模板 | Cyrene per-role 后 | 可否 `not in` |
|----|---------|-------------------|--------------|
| `普遍之爱`/`制造暧昧`/`初识` | C2（仅，12 段） | 无 | ✅ |
| `傲娇`/`今天不想振作也没有关系` | C3（仅，12 段） | 无 | ✅ |
| `俏皮灵动`/`认真时成熟冷静`/`亲密时柔软直接` | **仅 CharacterReflector.j2（辅助 prompt，非 12 段 C3）** | 12 段渲染本不含 | ⚠️ 不列入 §4.2（恒真测不到）；由 §2.3 机制 Y 处理 |
| **`♪`** | **C2/C3/C7 + F2** | **F2 仍有** | ❌ |
| **`她`** | **F2/F3（渲染来源）；C2/C3/C7 只在 doc-comment `@params ... (默认 "她")` 不渲染** | **F2/F3 仍有** | ❌ |
| `诶嘿~被你发现啦` | F2（仅） | F2 仍有 | ❌ |
| `她果然很聪明呢` | F3（仅） | F3 仍有 | ❌ |

> **核心洞察**：`♪` 和 `她` **同时**出现在 per-role（C2/C3/C7）和共享（F2/F3）。即使 Cyrene 的 C2/C3/C7 完全去 Elysia 化（无 `♪`/`她`），Cyrene 走共享 F2/F3 仍会带出 `♪`/`她`。**所以 `♪`/`她` 不能翻转。** 它们是「共享协议/示例」——B-1 Ruling 6 已判定 F2/F3 的 `她` 是示例第三方，非泄漏；`♪` 在 F2 是语气符号协议说明（Cyrene 俏皮也用）。

**翻转清单（正确版）：只翻转 `普遍之爱`/`制造暧昧`/`初识`/`傲娇`/`今天不想振作也没有关系`（确实在 12 段 C2/C3 per-role 且 Cyrene 可去除）的 `not in`，用 Cyrene 渲染。** `俏皮灵动`/`成熟冷静`/`柔软直接`（仅 CharacterReflector）不含在 §4.2，由 §2.3 机制 Y 处理。

> **翻转清单刻意最小化**：只翻转 §3.2 差异最明确、且是 Elysia 专属概念的非歧义词——`普遍之爱`/`制造暧昧`（Elysia 独特关系观）/`初识`（Elysia 四阶段首标签）/`傲娇`（Elysia 情绪）/`今天不想振作也没有关系`（Elysia 应对话术）。其余 STRUCTURE_TERMS（`熟悉`/`信任`/`亲密`/`调情`/`撒娇`）虽列 §3.3 内容生成禁忌，但**不作为 `not in` 测试词**——`熟悉`/`信任`/`亲密`/`撒娇`/`调情` 是关系发展中常见通用词，Cyrene 也可能合理用（如「熟悉后」「悄悄撒娇」），翻转会误报。故 §3.3（内容生成禁忌）与 §4.2（测试翻转）**不完全等价**，§3.3 是超集（内容被禁止 ≠ 一定不渲染，测试只锁最明确的 Elysia 专属词）。

`core/prompt/tests/test_system_prompt_leak.py` 的**角色风格词测试**（原 `cold_config` 断言 `in`）改**用 `cyrene_config` 渲染 + 断言 `not in`**：
- `test_leak_relationship_structure`：`普遍之爱`/`制造暧昧` → `not in`（Cyrene C2 per-role 无）；
- `test_leak_relationship_stage`：`初识` → `not in`（Cyrene C2 per-role 无，Cyrene 阶段标签不叫初识）；
- `test_leak_emotion_model`：`今天不想振作也没有关系`/`傲娇` → `not in`（Cyrene C3 per-role 无）。

> ⚠️ `test_leak_emotion_model` 原断言 `傲娇`——但 Cyrene 情感倾向是「共情/牺牲」，不用「傲娇」（§3.3 禁止），故 Cyrene 渲染 `傲娇 not in` 成立。

**新增「共享格式词保留」测试**（Cyrene 渲染断言 `in`，证明 F2/F3 是共享协议非 Elysia 泄漏）：
- `♪` 在 Cyrene 渲染中仍 `in`（F2 语气符号协议 + Cyrene 自己可用）；
- `她` 在 Cyrene 渲染中仍 `in`（F2/F3 示例对话第三方）；
- `诶嘿~被你发现啦`/`她果然很聪明呢` 仍 `in`（F2/F3 共享示例）。

**保留 Elysia 专属测试**（`elysia_config`，断言 `in`，byte-for-byte 守门）：
- `test_leak_speaking_mode_structure`/`test_leak_fixed_persona`（Elysia 现网含 `五模式切换`/`明快`）；
- `test_leak_tone_marker`/`test_leak_parameterized_over_scenarios`（Elysia 含 `♪`）。

> **关键决策 C3（修正）**：泄漏测试重构后分四组——
> - **Cyrene 渲染「C2/C3 角色风格词 `not in`」**：`普遍之爱`/`制造暧昧`/`初识`/`傲娇`/`今天不想振作也没有关系`（C2/C3 per-role 去 Elysia 化证明；`俏皮灵动`/`成熟冷静`/`柔软直接` 仅 CharacterReflector，不在此列）；
> - **Cyrene 渲染「共享格式词 `in`」**：`♪`/`她`/`诶嘿~被你发现啦`/`她果然很聪明呢`（F2/F3 共享协议，非 Elysia 泄漏）；
> - **cold 渲染「回退共享=Elysia 风格 `in`」**：cold 无角色目录回退共享 C2/C3，含 `普遍之爱`/`傲娇` 等（回退语义证明）；
> - **Elysia 渲染全部 `in`**（byte-for-byte 不变，`elysia_config`）。
>
> 这样既验证 per-role 生效（Cyrene C2/C3 无 Elysia 角色词），又验证回退语义（cold=Elysia 风格），又不破坏 Elysia byte-for-byte，同时不误判共享格式词（♪/她）为 Elysia 泄漏。

### 4.3 新增测试

- **Cyrene per-role 模板选路**：`PromptAssembler` 构造 `cyrene_config`，断言 C2/C3/C7 用 `templates/cyrene/*`（可加间谍/断言模板路径）。
- **回退逻辑**：`_resolve_template` 对无角色目录的 `cold_config` 回退共享路径。
- **辅助 Prompt 参数化**：
  - ExtractMemory 渲染：`{character_name}`/`{user_name}` 被替换（不复现 `{` 字面）；
  - MemoryReflector/ConsolidationCompare：`Elysia`/`妖梦` 不再出现（已改占位符）；
  - CharacterReflector/SpeakingStyleEdit：`俏皮灵动`/`明快` 不再出现（已参数化）。
- **Cyrene 配置**：`character/cyrene/config.yaml` `speaking_style` 非空；`cyrene_config` 夹具。
- **共享格式词保留**（§4.2 修正）：Cyrene 渲染仍含 `♪`/`她`/`诶嘿~被你发现啦`/`她果然很聪明呢`（F2/F3 共享协议）。
- **压缩路径 C6 首刷参数化**（architecture review 发现，见 §2.3 补充）：`compression.py::_refresh_c6` 裸构造 `SpeakingStyleSection` 默认取 `_C6_L1_DEFAULT`（Elysia），Cyrene 的 L2→C6 首刷会被 Elysia 风格污染。**B-2 需让 `_refresh_c6` 走 `character.speaking_style`**（见 §5 补充），并加测试断言 Cyrene `_refresh_c6` 首刷不取 `_C6_L1_DEFAULT`。

---

## 5. 明确不做（白名单之外的）

1. **不改 C6 模板本身**（方案 Y 框架/风格分离）——C6 已 B-1 per-role（`speaking_style` 变量）。方案 Y 是内容设计/结构重组，留待未来。
2. **不引入 `CharacterDefinition`/`Revision`/`PromptProfile` 领域模型**——per-role 模板目录已够，域模型为未来角色 CRUD 留待必要时。
3. **不改 CharacterEngine**（C1/C9 数据驱动逻辑不动）；C6 的 L1 默认常量 `_C6_L1_DEFAULT` 保留（Elysia L1 种子）。
4. **辅助 Prompt 用占位符**（`{character_name}`/`{user_name}`）非改 Jinja2 render：`replace()` 已够用，占位符最小侵入；改 Jinja2 与大 JSON 占位符冲突。
5. **不处理 `ActiveSpeak.j2`/`MacroReflector.j2`/`MicroReflector.j2`**——旧参考，不活跃（B-1 判定）。
6. **不建 `templates/cyrene/f2_output_format.j2`/`f3_tts_markup.j2`**——F2/F3 共享，`她`/`♪` 是格式示例，非角色泄漏（B-1 Ruling 6）。§4.2 已相应改为「共享格式词保留 `in`」。
7. **不改 `config.yaml` 的 `agent.character` 默认值**（保持 `character/elysia/config.yaml`）。
8. **不让 cold 角色走 per-role 模板**（cold 无角色目录，回退共享=Elysia 风格——这是「回退语义」的预期行为，cold 泄漏测试断言 `in`）。不建 `templates/reservedcoldcharacter/`。
9. **不翻转 `♪`/`她`/`诶嘿~被你发现啦`/`她果然很聪明呢` 的 `not in`**——它们是共享 F2/F3 协议/示例，任何角色（含 Cyrene）渲染都含（§4.2 修正版）。

### 5.5 压缩路径 C6 首刷（architecture review 补充，B-2 处理）

**问题**：`compression.py::_refresh_c6` 在首刷（`self._state.speaking_style_c6` 空）时**裸构造** `SpeakingStyleSection(...c6_speaking_style.j2)` → 默认 `speaking_style=None` → 回退 `_C6_L1_DEFAULT`（Elysia 常量）。Cyrene 有自己的 `personality.speaking_style`，但此路径不走 `CharacterConfig` → **Cyrene 的 L2→C6 首刷种子被 Elysia 风格污染**。

**B-2 处理**：让 `_refresh_c6` 首刷用 `current = self._character.speaking_style`（从角色配置读）而非裸构造。若 `speaking_style` 为空（如 cold），才回退 `_C6_L1_DEFAULT`。**不改 `speaking_style.py` 的 `_C6_L1_DEFAULT` 常量本身**（Elysia L1 种子保留），只改 `compression.py` 的读法。

**为什么 B-1 没处理**：B-1 时只有 Elysia 一个完整角色，`_C6_L1_DEFAULT` 就是 Elysia 的 L1，无区分。B-2 引入 Cyrene 后，此路径是所有角色的 C6 进化入口，必须 per-role。

> ⚠️ **影响面**：`compression.py` 有测试（`core/personality/tests/`）。B-2 需谨慎——`_refresh_c6` 首刷从「裸构造取常量」改为「读 character.speaking_style」可能导致 `core/personality/tests/test_compression*.py` 出现行为变化。**B-2 为「显式传 `speaking_style` 的 `SpeakingStyleSection`」加测试**（Cyrene 首刷不取 `_C6_L1_DEFAULT`），并核对 `core/personality/tests/` 现有压缩测试不因「读 character 配置」而红。

---

## 6. 关键决策记录（Rulings）

1. **Per-role 模板目录**（非 PromptProfile 域模型）：机制简单、角色独立演进、满足当前需要；域模型为未来 CRUD 留待必要时。（用户已定）
2. **角色名分流 + 回退共享**：`templates/{role}/cX.j2` 存在用角色模板，否则回退共享 `templates/cX.j2`。新增角色需 config + personality.md + 可选角色模板；不建模板继承共享（共享=Elysia 原文，此时角色仍 Elysia 化——B-2 只做 Cyrene，Cyrene 必建模板）。（用户已定）
3. **Elysia 完整副本进 `templates/elysia/`**：byte-for-byte + 独立演进；共享 `templates/` 保留 Elysia 原文作回退底（`test_boundary`/`test_full_render` 引用共享路径稳定，不受分流影响）。（用户已定）
4. **F2/F3 不去 Elysia 化**：`她`/`♪` 是格式示例（第三方用户指代 + 语气符号用法），改它破坏 Elysia 示例文本。修正 B-1 spec §10 预告。
5. **辅助 Prompt 用占位符**（`{character_name}`/`{user_name}`）非改 Jinja2 render：`replace()` 已够用，占位符最小侵入；改 Jinja2 与大 JSON 占位符冲突。
6. **Cyrene 内容由 subagent 生成**（spec 定创作指南 + 泄漏词门禁），非把 2000 行参考塞进 spec/plan。（用户已定）
7. **泄漏测试翻转语义（关键决策 C3，修正版）**：`♪`/`她`/`诶嘿~被你发现啦`/`她果然很聪明呢` 来自共享 F2/F3，**不翻转**（Cyrene 走共享也含）；只有 C2/C3 per-role 专属词（`普遍之爱`/`制造暧昧`/`初识`/`傲娇`/`今天不想振作也没有关系`——确实在 12 段且 Cyrene 可去除）才对 Cyrene 断言 `not in`。`俏皮灵动`/`成熟冷静`/`柔软直接`（仅 CharacterReflector 辅助 prompt）由 §2.3 机制 Y 处理，不入 §4.2。cold 断言 `in`（回退=Elysia 风格）；Elysia 断言全部 `in`（byte-for-byte）。
8. **Cyrene 用 `甜美` 替代 `可爱`**：`可爱` 是 Elysia FIXED_PERSONA 词，Cyrene 用 `甜美`/`漂亮` 避免翻转误判。
9. **Cyrene 用「共情/牺牲」替代「傲娇」，用「初遇/相伴/铭记」替代「初识」**（关键决策 C2b）：`傲娇`/`初识` 是 §4.2 翻转 `not in` 词，计入 §3.3 禁止清单，Cyrene 内容不得含。
10. **`_refresh_c6` 首刷读 character.speaking_style**（architecture review 发现）：`compression.py` 裸构造 `SpeakingStyleSection` 默认 `_C6_L1_DEFAULT`（Elysia），Cyrene L2→C6 首刷被污染。B-2 改读 `_character.speaking_style`（空才回退常量，用 `or _C6_L1_DEFAULT`——因构造器只在 `None` 回退，`""` 不回退），不影响 `_C6_L1_DEFAULT` 常量本身。**⚠️ 实现细节**：`compression.py:48` 的 `character_config: object` 标注需收紧为 `CharacterConfig` 或 cast，否则 pyright 门禁（§7.1）报 `object` 无 `speaking_style` 属性。

---

## 7. 验收标准（B-2 完成标准）

### 7.1 门禁

- [ ] `make lint` 通过；
- [ ] `make pyright` 通过；
- [ ] `make test-quick` 通过。

### 7.2 Elysia byte-for-byte 不变

- [ ] `test_elysia_snapshot_unchanged` 通过（Elysia 渲染 == B-1 固化的 Elysia 专属快照）；
- [ ] `test_elysia_config_speaking_style_matches_template` 通过；
- [ ] `test_leak_speaking_mode_structure`/`test_leak_fixed_persona`（`elysia_config`）通过。

### 7.3 Per-role 机制生效

- [ ] Cyrene per-role 模板选路测试通过（`cyrene_config` → `templates/cyrene/*`）；
- [ ] 回退逻辑测试通过（`cold_config` → 共享模板）；
- [ ] `templates/elysia/*` 与共享原版字节相同（B-2 加校验）。

### 7.4 辅助 Prompt 参数化

- [ ] ExtractMemory/MemoryReflector/ConsolidationCompare 渲染无 `Elysia`/`妖梦` 字面（已参数化）；
- [ ] CharacterReflector/SpeakingStyleEdit 渲染无 `俏皮灵动`/`明快` 字面（已参数化）。

### 7.5 泄漏测试翻转（修正版）

- [ ] Cyrene 渲染「C2/C3 角色风格词 `not in`」通过——`普遍之爱`/`制造暧昧`/`初识`/`傲娇`/`今天不想振作也没有关系` 不在 Cyrene 渲染中（per-role C2/C3 已去 Elysia 化）；
- [ ] Cyrene 渲染「共享格式词 `in`」通过——`♪`/`她`/`诶嘿~被你发现啦`/`她果然很聪明呢` 仍在 Cyrene 渲染中（F2/F3 共享协议，非 Elysia 泄漏）；
- [ ] cold 渲染泄漏测试（断言 `in`）通过——cold 回退共享 C2/C3，含 `普遍之爱`/`傲娇` 等（回退=Elysia 风格）。

### 7.6 Cyrene 角色完整

- [ ] `character/cyrene/config.yaml` + `personality.md` + `personality.speaking_style` 存在且非空；
- [ ] `templates/cyrene/c2_relationship.j2`/`c3_emotion.j2`/`c7_proactive.j2` 存在；
- [ ] Cyrene 内容不含 §3.3 禁止词（泄漏门禁）；
- [ ] Cyrene 内容体现 §3.2 差异（铭记/陪伴 vs 普遍之爱；不喜欢告别 vs 告别模式）。

### 7.7 压缩路径 C6 首刷（architecture review 补充）

- [ ] `compression.py::_refresh_c6` 首刷读 `_character.speaking_style`（空才回退 `_C6_L1_DEFAULT`）；
- [ ] Cyrene `_refresh_c6` 首刷不取 `_C6_L1_DEFAULT`（Elysia 常量）——新测试证明；
- [ ] `core/personality/tests/` 现有压缩测试不因「读 character 配置」而红（核对）。

---

## 8. 后续（阶段 B-3/未来 预告）

- **C6 方案 Y（框架/风格分离）**：把 C6 的 A 类框架（长度/节奏/夸奖）与 B 类风格（明快/♪/称呼/五模式/调情）重新划分，模板保留框架骨架，风格由角色提供。
- **PromptProfile/CharacterDefinition 领域模型**：若需完整角色 CRUD/版本演进。
- **Cyrene 的 F2/F3**：若未来需要 Cyrene 特有的格式示例（非共享）。
- **旧参考归档**：ActiveSpeak/MacroReflector/MicroReflector 若确认永久不活跃，可移入 `ref/` 或归档。

---

## 9. 受影响模块

| 模块 | 改动 |
|------|------|
| `core/prompt/prompt_assembler.py` | `_resolve_template` 选路 + C2/C3/C7 用 per-role 路径 |
| `character/cyrene/` | 新建 config.yaml + personality.md |
| `templates/cyrene/` | 新建 c2/c3/c7 per-role 模板 |
| `templates/elysia/` | 新建 c2/c3/c7 完整副本 |
| `core/prompts/MemoryReflector.j2` | 参数化 `Elysia`/`妖梦` |
| `core/prompts/ConsolidationCompare.j2` | 参数化 `Elysia`/`妖梦` |
| `core/prompts/ExtractMemory.j2` | 修复 `{character_name}`/`{user_name}` 未传 bug |
| `core/prompts/CharacterReflector.j2` | 去 Elysia 化 `俏皮灵动`（改通用心理结构，§2.3 已定调——拒绝加变量方案） |
| `core/prompts/SpeakingStyleEdit.j2` | 去 Elysia 化 `明快`（读 `character_tone` 或改通用）+ 行15 `五模式` 结构描述 |
| `core/memory/consolidator.py` | `replace()` 传 `{character_name}`/`{user_name}`（修复 ExtractMemory 占位符未传） |
| `core/memory/memory_reflector.py` | `replace()` 传 `{character_name}`/`{user_name}` |
| `core/personality/compression.py` | `_refresh_c6` 首刷读 `_character.speaking_style`（空才回退 `_C6_L1_DEFAULT`） |
| `core/prompt/tests/test_system_prompt_leak.py` | 翻转 Cyrene 渲染 C2/C3 角色风格词断言 + 共享格式词保留 + cold 保留断言 |
| 相关测试 | 新增 per-role 选路/回退/参数化测试 |
