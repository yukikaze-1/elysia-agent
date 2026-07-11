# PersonalityEngine — 角色性格引擎

## 概述

引入独立的 `PersonalityEngine` 模块，管理角色的性格状态与演化。将静态 YAML 配置（C1 Personality）升级为动态的四层性格模型：Core（不变底色）→ Self（月级演化）→ State（周级状态）→ Mood（当前情绪）。

CharacterReflector 降级为 PersonalityEngine 的子组件，作为触发源的输入之一。

---

## 动机

**当前问题：**

1. C1 模板 17 行硬编码 Elysia 人格。换角色 = 重写模板。YAML 的 `personality.traits` 只有 3 个标签，无法驱动丰富的人格描述。
2. CharacterReflector 观察到的变化只能写在 `self_perception` → C9，不影响 C1。"我变敷衍了"的认知与实际人格描述不同步。
3. 性格完全静态——没有受经历影响而演化、没有 L4→L3→L2 的沉淀机制、没有短期状态与核心底色的冲突协调。

**目标：**

- 角色能从对话经历中演化，形成有连续性的"自我感"
- 短期状态不会覆写核心底色（阻尼机制）
- 演化的数据自然驱动 C1/C9 的 prompt 内容
- 为后续 C3/C6/C7 的联动留好接口

---

## 四层性格模型

```
┌──────────────────────────────────────────────────────────────┐
│ L1: Core（核心底色）                          变化: 从不        │
│ 来源: character/{name}_personality.txt（自然语言）            │
│ "温柔是你的底色——不是因为没脾气，是因为你在乎。                    │
│  你对世界有好奇心——不是任务驱动，是真的想知道。"                   │
│ 作用: C1 的锚点。不管怎么演化，根在这里。                          │
├──────────────────────────────────────────────────────────────┤
│ L2: Self（长期自我）                        变化: 数百轮/重大事件 │
│ 来源: L3 持续态压缩沉淀（_compression）                        │
│ "这一年来，毒舌从'防御性的调侃'变成了'默契的吐槽'。               │
│  对妖梦的信任感越来越强，嘴硬但心软的那一面更明显了。"              │
│ 作用: C1 的主体——积累了长期经验后的"我是谁"。                      │
├──────────────────────────────────────────────────────────────┤
│ L3: State（短期状态）                       变化: 数十轮/24h     │
│ 来源: CharacterReflector + L4汇聚 + 事件触发                   │
│ "最近一周妖梦经常熬夜到三点，劝了也没用。                          │
│  我发现自己越来越敷衍了——不是不在乎，有一种无力感。"               │
│ 作用: C9 的素材——"你最近注意到自己的变化"。                       │
├──────────────────────────────────────────────────────────────┤
│ L4: Mood（当前情绪）                       变化: 每轮           │
│ 来源: EmotionEngine（已有系统，不动）                            │
│ "当前心情: neutral / warm / concerned"                       │
│ 作用: C3 的内容。                                               │
└──────────────────────────────────────────────────────────────┘
```

**层间约束关系：**

1. L1 约束 L2 — L2 不能与 L1 矛盾。如 L1 说"底色温柔"，L2 不能说"变得冷酷无情"。
2. L2 约束 L3 — L3 是在 L2 基线内的短期波动。偏离超过阈值由阻尼机制处理。
3. L3 向上回流 — L3 持续的主题可压缩沉淀为 L2（向上回流/梯度压缩）。
4. L4 向上汇聚 — L4 的频次统计可触发 L3 状态更新。

---

## 触发源 & 演化规则

### 触发源

| 触发源 | 频率 | 影响层 | 说明 |
|--------|------|--------|------|
| CharacterReflector | 每 50 对对话 | L3 | 自我觉察报告 → 提交为 L3 事件 |
| L4 情绪汇聚 | 连续 N 轮某情绪占比 > 阈值 | L3 | 统计 L4 频次 → "最近比较焦虑" |
| 重要对话事件 | 低频（争吵/深谈/离别） | L3 / L2 | 情绪冲击力高的对话事件 |
| 时间流逝 | Idle > N 小时 | L3 | "很久没聊了，有点想念" |
| 用户反馈 | 极低频（"你最近太敷衍了"）| L3 / L2 | 用户直接评价角色行为 |

### 向上回流（梯度压缩）

**L4 → L3**：连续 N 轮内某情绪占比 > 阈值 → LLM 总结为短期状态描述。
- 例：连续 30% 轮次 mood=concerned → L3 追加 "最近比较焦虑"

**L3 → L2**：同一 State 主题持续 > 阈值天数 或 重大事件触发 → LLM 提炼 + 去噪 + 与现有 L2 合并。
- 不是简单拼接。"敷衍/无力"持续 2 周后，LLM 将其总结并融入 L2 的现有描述。

压缩时 LLM 做三件事：
1. 总结趋势（这段时间主要变化是什么）
2. 去除噪声（偶发状态不沉淀）
3. 与现有 L2 合并（增量更新 / 覆盖过时描述）

### 阻尼机制（ExpressionAdapter）

当 L3 偏移了 L1/L2 中的对应特质时，表现形态由高层决定：

- **L3 影响"说什么"（内容倾向）**，**L1 影响"怎么说"（表达方式）**
- 不是直接覆写，而是调制

**例**：
- L1 "嘴硬心软" + L3 "敷衍/无力"
- 错误输出：直接死板的敷衍 → "嗯。哦。"
- 正确输出：嘴硬心软式的敷衍 → "随你怎么熬夜，药给你放桌上了，爱吃不吃"

阻尼机制在 ExpressionAdapter 中实现——生成 C1 文本时，LLM 被提示遵守 L1 的表达约束。后续 C6 联动时规划将此逻辑移入 C6 SpeakingStyle。

### AwarenessTrigger（C9 激活判定）

C9 不每轮渲染。触发条件（满足任一即可）：

| 条件 | 阈值 | 说明 |
|------|------|------|
| 偏差阈值 | L3 与 L2 基线偏差 > threshold | L3 出现明显新模式 |
| 用户话题触发 | 用户提到"你变了/你是不是..." | 自然对话引导自省 |
| 频率限制 | 触发后冷却 N 轮 | 避免连续触发，显得做作 |

---

## 数据流 & 持久化

### 存储

| 数据 | 位置 | 持久化方式 |
|------|------|-----------|
| L1 Core | `character/{name}_personality.txt` | 文件，启动加载，不修改 |
| L2 Self | `session.params["self_l2"]` | 随 session MongoDB 持久化 |
| L2 历史 | `session.params["self_history"]` | 每次演化追加条目 |
| L3 State | `session.params["state_l3"]` | 随 session 持久化 |
| L3 历史 | `session.params["state_history"]` | 每次演化追加条目 |
| L4 Mood | `session.params["mood"]` | 已有，EmotionEngine 维护 |

> **L2 持久化风险**：L2 是月级演化，但 session TTL=30 天。后续需将 L2 历史迁移到独立 MongoDB 集合或独立文件，防止 TTL 过期丢失。

### Section 数据流

```
PersonalityEngine                     Section 模板
───────────────                      ───────────
render_c1() → core_personality ──→ C1 personality.j2
              self_personality  ──→

render_c9() → awareness_text ────→ C9 self_perception.j2
              （空串时跳过渲染）

（后续联动）
             → mood (已有) ────────→ C3 emotion.j2
             → 风格参数 ──────────→ C6 speaking_style.j2
             → 主动倾向 ──────────→ C7 proactive.j2
```

---

## 组件关系

```
core/personality_engine.py          — 新模块
  ├── consume_trigger(source, data) — 接收触发事件
  ├── _evolve_state()               — L4→L3 / 事件→L3
  ├── _compression()                — L3→L2 沉淀（定期/事件触发）
  ├── _expression_adapter()         — 阻尼机制
  ├── _awareness_check()            — C9 激活判定
  ├── render_c1()                   — 产出 core + self 文本
  └── render_c9()                   — 条件产出 awareness 文本

core/character_reflector.py          — 改为子组件
  └── 不再写 self_perception_history
      改为产出事件 → PersonalityEngine.consume_trigger("reflector", data)

core/emotion_engine.py               — 不动
  └── 每次 mood 变更 → PersonalityEngine.consume_trigger("mood", data)

core/prompt_assembler.py             — 修改
  └── 用 PersonalityEngine.render_c1() / render_c9() 替代当前硬编码数据

core/character_config.py             — 修改
  └── 加载 base_personality_file
```

---

## 模板变化

### C1 Personality

```
当前（硬编码 Elysia）:
<personality>
你是 Elysia。温柔是你的底色——但不是那种...（17行）
</personality>

新（数据驱动）:
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

- `core_personality`: 从文件加载的 L1 Core 文本
- `self_personality`: PersonalityEngine 维护的 L2 Self 文本（可能为空）

### C9 SelfPerception

```
当前（无条件渲染）:
{% if self_perception %}
<self_perception>
你最近注意到自己的一些变化：
{{ self_perception }}
</self_perception>
{% endif %}

新（条件渲染，AwarenessTrigger 控制）:
{% if awareness_text %}
<self_perception>
你最近注意到自己的一些变化：
{{ awareness_text }}
</self_perception>
{% endif %}
```

`awareness_text` 由 `AwarenessTrigger` 判断后提供，条件满足时非空。

---

## Character YAML 瘦身

移除 `personality.traits` 和 `personality.speaking_style`——合并到 base_personality_file。

```yaml
# character/elysia.yaml（新结构）
name: Elysia
voice: elysia_v2
pronouns: 她
base_personality_file: character/elysia_personality.txt  # 新增

appearance:
  height: "162cm"
  hair: "粉色长发"
  eyes: "蓝色"

emotion:                    # 不动
  thresholds:
    excitement: 0.6
    sadness: 0.3
  idle_expressions:
    - neutral
    - blink

proactive:                  # 不动
  enabled: false
  interval: 300
  triggers: [...]

wake_words:                  # 不动
  - Elysia
  - 爱莉
  - 爱莉希雅
  - 妖精小姐
```

```txt
# character/elysia_personality.txt（新建）
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

你嘴硬心软。被夸了会暗爽但不想表现出来。被冷落了会在意但不会说出来。

以上这些是你。不是说你要"扮演"这些——是这些描述本来就是你的样子。
```

---

## Phase 1 实现范围

本次实现 Phase 1，覆盖：

1. **数据层**：创建 personality.txt + 瘦身 YAML + 更新 CharacterConfig
2. **PersonalityEngine 核心**：四层模型 + L1 加载 + L2/L3 存储/恢复 + `consume_trigger` + `render_c1` + `render_c9`
3. **迁移 CharacterReflector**：改为子组件，self_perception → trigger 事件
4. **模板更新**：C1 用 L1+L2、C9 用 AwarenessTrigger
5. **断线 PrompterAssembler**：用 PersonalityEngine 替代当前硬编码数据

**本次不做**（记录 todo.md）：

- L4→L3 情绪汇聚统计（需要 EmotionEngine 改接口）
- L3→L2 梯度压缩的自动化（先手动 + LLM 调用，压缩时机待 Phase 2）
- ExpressionAdapter（先做简单版本：C1 文本末尾加 L1 约束提示。完善版本 Phase 2）
- AwarenessTrigger 的用户话题检测（先用偏差阈值 + 频率限制）
- C3/C6/C7 联动
- L2 永久存储（先用 session params，后续迁移）

---

## 验收标准

1. 启动后 C1 渲染 L1（core）文本，内容与当前硬编码版本语义一致
2. Reflector 触发后，L3 更新，调试接口可查看当前 L2/L3 状态
3. C9 只在偏差阈值触发时渲染（不每轮渲染）
4. 模板不包含 Elysia 硬编码——换角色只需换 personality.txt
5. 现有 55 个 system_prompt 测试适配通过
6. CharacterReflector 持久化测试（buffer/state save & restore）通过
