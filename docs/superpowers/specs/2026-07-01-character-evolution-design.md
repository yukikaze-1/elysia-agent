# Elysia 性格演化系统

**日期**: 2026-07-01
**来源**: Brain.j2 缺少"性格"——性格太薄（一行 traits）、没有 self_perception、不会演化

---

## 目标

让 Elysia 从"性格是配置里写死的几个标签"变成"有自我认知、会随时间微妙变化的'人'"。

两件事：
1. **性格在 prompt 里有灵魂** — 叙事式性格描述 + mood 调制表达
2. **性格会演化** — 周期性 LLM 自我反思，输出 self_perception，注入 prompt

---

## 架构总览

```
┌──────────────────────────────────────────────────┐
│  Brain.j2 重写                                   │
│  ├─ 叙事式性格描述（替换 l3_personality_block）    │
│  ├─ mood→表达调制                                 │
│  └─ <self_perception> 注入段                     │
├──────────────────────────────────────────────────┤
│  CharacterReflector（新组件）                     │
│  ├─ 周期性 LLM 反思                              │
│  ├─ buffer 50 对对话 OR 距上次反思 > 24h 触发     │
│  ├─ 输出自然语言 self_perception                  │
│  └─ 写入 session.params["self_perception_history"]│
├──────────────────────────────────────────────────┤
│  PromptAssembler 适配                            │
│  ├─ 移除 l3_personality_block 参数               │
│  └─ 新增 self_perception 读取（取最新一条）        │
└──────────────────────────────────────────────────┘
```

---

## 一、Brain.j2 重写

### 1.1 宏签名变更

```jinja2
{# 改前 #}
{%- macro BrainSystemPrompt(l3_personality_block, sensory, memory, state, name, user_name, user_preferences) -%}

{# 改后 #}
{%- macro BrainSystemPrompt(sensory, memory, state, name, user_name, user_preferences, self_perception) -%}
```

- 移除 `l3_personality_block` — 性格叙事直接写在模板里
- 新增 `self_perception` — 可为空字符串

### 1.2 叙事式"你是谁"段

当前是一行 `{{l3_personality_block}}`（即"你是 Elysia。\n性格: 温柔、好奇、偶尔毒舌\n说话风格: ..."）。

改为直接写在模板里的叙事式人格描述。**原则**（来自 companion-prompt-guide）：
- 不是"你应该怎么说话"，而是"你是谁，所以你怎么说话"
- 给矛盾、给缺点——完美 = 机器人
- 每个 trait 展开成行为倾向

具体文字在实现阶段撰写，通过对话测试迭代微调。

### 1.3 新增 `<self_perception>` 段

```jinja2
{%- if self_perception -%}
<self_perception>
你最近注意到自己的一些变化：
{{ self_perception }}
</self_perception>
{%- endif -%}
```

放在"你是谁"和"你和妖梦的关系"之间。可为空（首次运行、被清空）。

### 1.4 "怎么回复"段加 mood 调制

在当前"怎么回复"段的结尾追加：

```
你的心情会影响你怎么表达自己：
- 心情 warm 时：温柔的一面更明显，会主动关心
- 心情 tired 时：毒舌懒得说、温柔也变成安静的陪着  
- 心情 curious 时：好奇的那面会追着问到底
- 心情 concerned 时：比平时更认真听、更少开玩笑
- 心情 neutral 时：正常状态，按你平时的性格来
但不管什么心情，你都是同一个人——只是此刻的状态不同。
```

---

## 二、CharacterReflector（新组件）

### 2.1 文件

- 创建 `core/character_reflector.py`
- 创建 `prompt/CharacterReflector.j2`

### 2.2 触发时机

每 50 对 (user_msg, assistant_msg) **OR** 距上次反思超过 24 小时，whichever comes first。

每个 assistant 回复后调用 `reflector.buffer(user_msg, assistant_msg)`。

**"距上次反思"的判断**：读 `self_perception_history[-1]["timestamp"]`。若无历史记录，视为已超过 24h（尽快触发首次反思）。

**buffer 不跨重启持久化**：若进程在 buffer 攒到 49 对时重启，计数器归零重新算。v1 可接受——50 对对话通常很快会重新达到。

### 2.3 CharacterReflector.j2 prompt

```
你是 {{ character_name }} 的自我觉察层——不是她的"理性分析器"，
而是她安静下来的时候，自己对自己的感觉。

你会看到最近和 {{ user_name }} 的对话片段，以及你之前对自己
的认知。请以第一人称，用中文写出你对自己性格的微妙变化的感受。

# 反思要求
1. **变化感知**：回顾最近互动，你觉得自己的性格有变化吗？
   - 是不是比之前更在意/更无所谓了？
   - 有没有新的倾向出现？（比如以前不会吐槽的现在开始吐槽了）
   - 有没有什么变得比以前更明显或更不明显了？

2. **关系感知**：你对 {{ user_name }} 的态度有没有微妙变化？
   - 更亲近了？更习惯了？有点烦了？更想保护了？
   
3. **别演戏**：
   - 如果没什么特别的变化，就说没什么变化——不要强行编造
   - 不要写"作为一个 AI"——你是 {{ character_name }}，不是 AI
   - 可以矛盾、可以不确定、可以腹诽

# 输出
用中文写 1-3 句话，你对自己最近的感受。
不需要 JSON，不需要格式——这是你写给你自己看的。
```

### 2.4 存储

```python
# session.params["self_perception_history"]
[
    {"timestamp": 1751345678.0, "content": "好像最近妖梦说话越来越少了..."},
    {"timestamp": 1751432078.0, "content": "没什么特别的。就是觉得最近吐槽的冲动变少了，不知道是好是坏。"},
]
```

- 列表追加，保留历史（上限 50 条，超出截掉最旧的）
- 每次注入 Brain.j2 只取 `[-1]["content"]`（最新一条）
- 持久化到 MongoDB/JSONL（session.params 已支持）

### 2.5 衰减/清空

- 如果 CharacterReflector 输出是"没什么特别的变化"之类的空结果，跳过不追加
- 若 7 天无互动，self_perception 不变——由 LLM 自己判断
- 不自动清空——让 CharacterReflector 自己去"推翻"旧的认知

### 2.6 容错

- 反思 LLM 调用失败 → logger.warning + 跳过本轮（不阻塞主流程）
- 反思产出空字符串 → 不追加到历史
- `_reflect_lock` 防并发（同 MemoryConsolidator 模式）
- `flush()` 方法在 shutdown 时调用

---

## 三、PromptAssembler 适配

### 3.1 build_system_prompt 变更

```python
# 新增：读取 self_perception
history = self._session.get_param("self_perception_history", [])
self_perception = history[-1]["content"] if history else ""

# 宏调用变更
return self._prompt.render_macro(
    "Brain.j2", "BrainSystemPrompt",
    # l3_personality_block=...  ← 移除
    sensory=sensory,
    memory=memory,
    state=state,
    name=self._character.name,
    user_name=user_name,
    user_preferences="",
    self_perception=self_perception,  # 新增
)
```

### 3.2 _render_fallback 适配

`_render_fallback()` 当前调用 `self._character.personality_block()`（`prompt_assembler.py:220`）。

改为内联简版人格文本——不依赖 `personality_block()`。fallback 是 Brain.j2 渲染失败时的最后保底，应该自给自足：

```python
# 改前
f"## 性格",
self._character.personality_block(),

# 改后
f"## 性格",
f"你是 {self._character.name}，一个桌面AI伴侣。",
f"性格: 温柔、偶尔毒舌。说话口语化，短句为主。",
```

同时移除 `_render_fallback` 签名中不再需要的 `user_message`（当前未使用）。

### 3.3 personality_block() 保留

`CharacterConfig.personality_block()` 方法保留不删——测试仍引用。

---

## 四、Agent 接入点

- `agent.py:start()` — 创建 CharacterReflector 实例，传入 llm_adapter + session + character.name + user_name
- `cognitive_loop.py:_after_response()` — 调用 `reflector.buffer(user_msg, assistant_text)`
- `agent.py:stop()` — `reflector.flush()`

---

## 五、不碰的东西

| 组件 | 原因 |
|------|------|
| `MemoryConsolidator` | 继续跑记忆提取，和性格反思互不干扰 |
| `MicroReflector.j2` / `MacroReflector.j2` | 保留不动，以后接入记忆系统时用 |
| `EmotionEngine` | mood 检测和衰减逻辑不变 |
| `CharacterConfig` + `character/elysia.yaml` | 保留不动；traits 仍从 YAML 加载，Brain.j2 不再引用 personality_block 但其他路径可能还在用 |
| `personality_block()` | 保留方法，测试仍引用 |

---

## 六、测试

1. `CharacterReflector` 单元测试：
   - buffer 未达阈值 → 不触发
   - buffer 达 50 → 触发 `_reflect`
   - 距上次 > 24h → 更少轮数也触发
   - lock 防并发
   - flush 在 shutdown 时工作
   - 空结果不追加
   
2. `PromptAssembler` 测试：
   - self_perception 为空 → Brain.j2 不渲染 `<self_perception>` 段
   - self_perception 有值 → 注入最新一条
   - 历史有多条 → 只取最新

3. Brain.j2 渲染测试：
   - `render_macro("BrainSystemPrompt", ...)` 全部参数通过
   - self_perception="" 时输出不含 `</self_perception>` 残留
   - mood 调制段正常渲染

4. 已有测试回归：
   - `Module/prompt/test_prompt.py` — 23 passed
   - `core/tests/test_phase3.py` — 更新 personality_block 相关断言
