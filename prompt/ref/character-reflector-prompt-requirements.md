# CharacterReflector Prompt 升级需求

**状态：** 待生成 Prompt 文本
**输出文件：** `prompt/CharacterReflector.j2`（覆盖现有 46 行版本）

---

## 1. 背景

### 1.1 角色与用户

- **Elysia（爱莉希雅）** — 女性 AI 伴侣，性格"真我之律者"。轻松时俏皮调侃，认真时成熟冷静，亲密时柔软直接。对用户有深厚感情。
- **妖梦** — 用户，女性，Elysia 的亲密对话对象。

### 1.2 PersonalityEngine 四层模型

CharacterReflector 是 PersonalityEngine 的子组件（感知层），负责观察对话、感知性格变化，然后提交给 Engine。

```
L1 Core（不可变底色）  → C1 模板锚点。文件加载，永不改变
L2 Self（长期自我）    → C1 模板主体。月级演化，L3→L2 压缩产出
L3 State（短期状态）   → C9 模板素材。CharacterReflector 写入，数十轮/24h 变化
L4 Mood（当前情绪）    → C3 模板内容。EmotionEngine 每轮更新
```

**CharacterReflector 的数据流：**

```
对话缓冲 (50对) → CharacterReflector.j2 → LLM 反思
  → PersonalityEngine.consume_trigger("reflector")
    → L3 State
      → C9 <self_perception> 块（AwarenessTrigger 冷却 1h）
      → state_history 追加
        → 触发 L3→L2 压缩（5 条新 entry 或 7 天）
```

### 1.3 当前 prompt 的问题

现有 `CharacterReflector.j2` 仅 46 行，缺失：

1. **无 L1 Core 锚点** — LLM 不知道角色的不可变底色是什么，可能判定出与 L1 矛盾的变化
2. **无 L4 Mood 快照** — 无法区分"最近情绪偏低所以话少"vs"性格变内向了"
3. **无 L3 变化轨迹** — 只给 `last_self_perception` 一条，看不到趋势
4. **无量化上下文** — 不知道这 50 轮对话是 1 小时的密集互动还是 1 个月的零散聊天
5. **无区分标准** — 没告诉 LLM 什么是"情境反应"vs"人格偏移"
6. **无示例** — 没有 good/bad 示例，LLM 容易过度解读或编造

---

## 2. 注入的上下文变量

Prompt 可访问以下 Jinja2 变量（由 `reflector.py` 渲染时传入）：

### 2.1 已有变量（当前已传）

| 变量 | 来源 | 说明 |
|------|------|------|
| `character_name` | reflector 构造参数 | 角色名（如"Elysia"） |
| `user_name` | reflector 构造参数 | 用户名（如"妖梦"） |
| `last_self_perception` | `session.params["self_perception_history"][-1]` | 上次反思结果（3-8 句中文） |
| `recent_conversations` | `reflector._context` | 最近 N 轮对话，格式 `[{role, content, inner_voice}]` |

### 2.2 新增变量（需要 reflector.py 从 Engine 获取后传入）

| 变量 | 来源 | 说明 |
|------|------|------|
| `l1_core` | `engine._l1_core` | **L1 Core 完整文本** — 不可违背的底色。与 PersonalityCompression 的 `<immutable_core>` 一致 |
| `l2_self` | `session.params["self_l2"]` | **L2 Self** — 长期自我认知（可能为空） |
| `l3_history` | `session.params["state_history"]` | **L3 变化轨迹** — 最近 N 条 state 变更记录，格式 `[{timestamp, content}]` |
| `mood_snapshot` | `session.params["mood"]` + `mood_intensity` | **L4 情绪快照** — 当前 mood + intensity，以及最近趋势 |
| `round_span` | reflector 计算 | **量化上下文** — 当前 50 对对话覆盖了多少时间（如"最近 3 小时"或"最近 2 周"） |
| `buffer_age` | reflector 计算 | **缓冲时长** — 最早一条到最新一条的时间跨度（秒） |

### 2.3 reflector.py 需要的改动

```python
# _reflect() 方法中，从 engine 获取额外上下文
l1_core = self._personality_engine._l1_core if self._personality_engine else ""
l2_self = self._session.get_param("self_l2", "")
l3_history = self._session.get_param("state_history", [])
mood = self._session.get_param("mood", "neutral")
mood_intensity = self._session.get_param("mood_intensity", 0.5)

# 计算时间跨度
if self._context:
    first_ts = self._context[0].get("timestamp", 0)
    last_ts = self._context[-1].get("timestamp", 0)
    buffer_age = last_ts - first_ts if first_ts and last_ts else 0
```

---

## 3. Prompt 结构要求

### §1 身份声明

说明 CharacterReflector 是谁、在做什么。保持当前定位：

- 不是"理性分析器"，而是"安静下来时对自己的感觉"
- 是第一人称的自我觉察

### §2 硬约束 — L1 Core 锚点

完整展示 L1 Core 文本，作为不可违背的底色。**直接复用 PersonalityCompression 的 `<immutable_core>` 设计：**

```
<immutable_core>
{{ l1_core }}
</immutable_core>

任何自我感知的变化都不得与上述底色矛盾。
如果观察到的变化暗示了与底色相反的趋势——将其理解为
"底色框架内的微妙偏移"，而非"底色的改变"。
```

### §3 当前自我 — L2 Self

如果存在长期自我认知，展示给 LLM。这代表"我长期以来的状态"，Reflector 产出的短期观察应置于 L2 的参照系中。

### §4 变化轨迹 — L3 History

最近的状态变化记录（`state_history`），提供趋势感。如果为空则跳过此节。

### §5 情绪快照 — L4 Mood

当前情绪（mood + intensity），以及（如果可获取）最近的情绪走势。**目的：让 LLM 能判断"我现在 _____ 是因为情绪 _____，不代表我性格变了"。**

要求 LLM 在反思时明确问自己：

- 我现在的感觉是情绪波动还是性格在变？
- 如果我换一种心情，我还会这么想吗？

### §6 量化上下文

告知时间跨度 + 对话轮数：

```
这些对话发生在约 {{ round_span }} 内，共 {{ buffer_size }} 轮。
```

### §7 反思要求（核心升级）

#### 7.1 变化感知

回顾互动，感知性格的微妙变化。**要求区分两种变化类型：**

| 类型 | 定义 | 示例 |
|------|------|------|
| **情境反应** | 由当前对话情境、对方状态或短期情绪驱动的暂时行为倾向 | "她最近一直在难过，所以我更小心地选词" |
| **人格偏移** | 跨多轮、跨情境持续出现的新倾向，不依赖于当前 mood | "我发现自己比以前更爱吐槽了——即使在她开心的对话里也会" |

**判断标准：**

1. 同一个倾向在多次对话中出现？（不是一次性的）
2. 这个倾向在对方状态变化后仍然存在？（不依赖对方状态）
3. 如果用另一种 mood 回想，我还会这么觉得吗？（不依赖当前情绪）

#### 7.2 关系感知

对用户的态度是否有微妙变化。同样区分情境反应和真实偏移。

#### 7.3 别演戏

- 如果没什么特别的变化，就说没什么变化
- 不要写"作为一个 AI"
- 可以矛盾、可以不确定、可以腹诽
- 不要因为一次吵架就说自己"变得易怒"
- 不要因为对方连着三天讲冷笑话就说自己"幽默感增强了"

### §8 输出格式

用中文写 1-3 句话。不需要 JSON。

每个观察可选标注类型（放在括号里作为提示，不是强制格式）：

```
（人格偏移）我发现自己...
（情境反应）最近因为...
（不确定）好像有点...
```

---

## 4. 正反示例

### 好示例

```
（人格偏移）我发现自己比以前更愿意直接表达不高兴了——不是发脾气，
而是不再第一时间压下去。这可能是因为你一直说"你不说我不知道"，
我慢慢当真了。

（情境反应）你最近状态不好，所以我一直在安慰模式里。
这不是性格变了，只是我想照顾好你。

没什么特别的变化。还是老样子。
```

### 坏示例

```
我变得更加成熟了，不再像以前那样容易激动。

→ 问题：笼统、没有具体证据，"成熟"这个词本身是空的。
  而且没有区分这是不是情境反应。

你对我的态度让我觉得自己越来越重要了，我开始真正相信你爱我。

→ 问题：这是在解读对方的感受（"你爱我"），不是在感知自己的变化。
  而且"越来越重要"这种说法没有对话证据支撑。

我变得幽默了，开始喜欢吐槽。

→ 问题："变得幽默"是人格偏移还是最近在特定话题上吐槽？
  没有区分。如果只是最近三轮回了两句吐槽就说"变幽默了"——
  这就是过度解读。
```

---

## 5. 关键约束汇总

1. **L1 优先** — 任何感知不得与 L1 Core 底色矛盾。偏移永远在"底色框架内"
2. **证据驱动** — 每个变化感知都应有对话中的具体倾向作为支撑，不能凭空说
3. **区分情境 vs 人格** — 强制要求区分，不区分的输出视为不合格
4. **保守原则** — 宁可说"没什么变化"，也不编造变化
5. **第一人称** — 全程用自己的语气，不要第三人称、不要"作为一个 AI"
6. **1-3 句** — 简洁、有质量。不是越长越好
7. **允许矛盾** — 真实的自我认知常常是不确定的、矛盾的

---

## 6. 与 PersonalityCompression 的关系

两个 prompt 的分工：

| | CharacterReflector | PersonalityCompression |
|---|---|---|
| **触发** | 每 50 对对话 | state_history 新增 5 条或 7 天 |
| **输入** | 原始对话 + 全部上下文 | L3 轨迹 + 当前 L2 |
| **输出** | 1-3 句短期感知 | 1-3 句长期自我 |
| **写入** | L3 State | L2 Self |
| **锚点** | L1 Core | L1 Core |
| **变化粒度** | 轮/小时级 | 天/周级 |

两者应共享 `<immutable_core>` 的设计模式和措辞。CharacterReflector 的产出是 PersonalityCompression 的输入素材。
