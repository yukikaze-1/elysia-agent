# Brain.j2 Prompt 修复 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 Brain.j2 系统提示中 Identity 缺失、Protocol 情绪数据不存在、时间戳 Unix 整数渲染三个问题。

**Architecture:** 改 `personality_block()` 为第一人称 + `UserProfile` 携带 `display_name` → Brain.j2 新增 Interaction 块；丰富 `perception` 注入 mood/STT 标签 + 软化 Protocol 措辞；记忆时间戳加 `datetime` filter。5 个源码文件 + 1 个模板 + 1 个 todo。

**Tech Stack:** Python 3.13, Jinja2, Pydantic

## Global Constraints

- 不改 `character/elysia.yaml` 格式
- 不改中英混杂标签（风格问题）
- `datetime` filter 已在 `PromptManager` 注册，直接使用
- `UserProfile` 保持 `extra: "allow"` 向后兼容

---

### Task 1: Identity + Perception + Protocol + 时间戳

**Files:**
- Modify: `core/character_config.py:63-66`
- Modify: `prompt/Brain.j2:68,80,205-218`
- Modify: `core/prompt_assembler.py:49,57,77,85-91,94,194-200`
- Modify: `Module/task_system/schema/protocol.py:137-149`
- Modify: `core/agent.py:316`

**Interfaces:**
- Consumes: `CharacterConfig.personality_block()`, `UserProfile`, `BrainSystemPrompt` macro
- Produces: 修复后的 `personality_block()` 输出格式、Brain.j2 新 Interaction 块、丰富的 perception、datetime-filtered 时间戳

- [ ] **Step 1: 改 personality_block() 为第一人称**

`core/character_config.py:66`:

```python
# 旧
return f"角色名: {self.name}\n性格: {traits_str}\n说话风格: {self.speaking_style}"
# 新
return f"你是 {self.name}。\n性格: {traits_str}\n说话风格: {self.speaking_style}"
```

- [ ] **Step 2: 修 fallback 双重"你是"**

`core/prompt_assembler.py:197`:

```python
# 旧
f"你是 {self._character.name}，一个桌面AI伴侣。",
# 新
f"{self._character.name}，一个桌面AI伴侣。",
```

- [ ] **Step 3: UserProfile 加 display_name**

`Module/task_system/schema/protocol.py:148`，在 `language` 字段后加：

```python
display_name: str = ""  # 用户显示名——来自 config.agent.user_name
```

- [ ] **Step 4: agent.py 传真实 UserProfile**

`core/agent.py:316`:

```python
# 旧
self._cognitive.set_user_profile(None)  # v1: UserProfile 未实现

# 新
from Module.task_system.schema import UserProfile
self._cognitive.set_user_profile(
    UserProfile(display_name=self._user_name)
)
```

- [ ] **Step 5: Brain.j2 新增 Interaction 块**

`prompt/Brain.j2` — 在 `# Role Definition` 块（line 207-208）之后，`# 双轨思维协议` 之前插入：

```jinja2
# Who You're Talking To
你是 {{ name }}，正在和 {{ user_name }} 对话。
{%- if user_preferences %} 关于 {{ user_name }}：{{ user_preferences }}{% endif %}
```

- [ ] **Step 6: BrainSystemPrompt macro 签名加参数**

`prompt/Brain.j2:205` — `BrainSystemPrompt` macro 签名：

```jinja2
{# 旧 #}
{%- macro BrainSystemPrompt(l3_personality_block, sensory, memory, state) -%}

{# 新 #}
{%- macro BrainSystemPrompt(l3_personality_block, sensory, memory, state, name, user_name, user_preferences) -%}
```

- [ ] **Step 7: prompt_assembler.py 传新参数**

`core/prompt_assembler.py:85-91` — `render_macro` 调用：

```python
# 旧
return self._prompt.render_macro(
    "Brain.j2", "BrainSystemPrompt",
    l3_personality_block=self._character.personality_block(),
    sensory=sensory,
    memory=memory,
    state=state,
)

# 新
return self._prompt.render_macro(
    "Brain.j2", "BrainSystemPrompt",
    l3_personality_block=self._character.personality_block(),
    sensory=sensory,
    memory=memory,
    state=state,
    name=self._character.name,
    user_name=user_name,
    user_preferences="",
)
```

- [ ] **Step 8: 丰富 perception（去截断 + mood + STT）**

`core/prompt_assembler.py:57`:

```python
# 旧
perception = f"{user_name}说：{user_message[:200]}"

# 新
parts = [f"{user_name}说：{user_message}"]
if stt_emotion and stt_emotion != "NEUTRAL":
    parts.append(f"[语调: {stt_emotion}]")
parts.append(f"[当前心情: {mood}]")
perception = "\n".join(parts)
```

- [ ] **Step 9: 软化 Protocol 措辞**

`prompt/Brain.j2:215-218`:

旧：
```
- **Absorb the Emotional State**: Read the `<perception>` block within
  `<sensory_input>`. You MUST adopt the "Core Emotion" (核心情感) and
  "Behavioral Tendency" (行为倾向) described there as your absolute
  internal state. **Do not re-interpret the mood based on raw facts;
  trust the Perception.**
```

新：
```
- **Absorb the Emotional State**: If the `<perception>` block contains
  emotional cues (tone tags, mood indicators), adopt them as your
  emotional baseline. If not, infer the user's emotion from their
  wording and context. Either way, your external reply should feel
  emotionally attuned to the user's state. Do not re-interpret the
  mood based on raw facts when perception provides emotional signals.
```

- [ ] **Step 10: 时间戳加 datetime filter**

`prompt/Brain.j2:68`:

```jinja2
{# 旧 #}
    - [{{ mem.type }}] {{ mem.timestamp }} (Poignancy: {{ mem.poignancy }}) {{ mem.content }}

{# 新 #}
    - [{{ mem.type }}] {{ mem.timestamp | datetime }} (Poignancy: {{ mem.poignancy }}) {{ mem.content }}
```

`prompt/Brain.j2:80`:

```jinja2
{# 旧 #}
    - {{ mem.timestamp }} (Poignancy: {{ mem.poignancy }}){% if mem.dominant_emotion %} ...

{# 新 #}
    - {{ mem.timestamp | datetime }} (Poignancy: {{ mem.poignancy }}){% if mem.dominant_emotion %} ...
```

- [ ] **Step 11: prompt_assembler.py timestamp 保留 float**

`core/prompt_assembler.py:77`:

```python
# 旧
"timestamp": int(entry.timestamp) if entry.timestamp else int(now.timestamp()),
# 新 — datetime filter 需要 float
"timestamp": entry.timestamp if entry.timestamp else now.timestamp(),
```

- [ ] **Step 12: 运行测试**

```bash
conda activate Elysia
python -m pytest core/tests/test_phase3.py -v -k "personality_block or prompt or assembler" -q
python -m pytest core/tests/ -q
```

预期：全部通过，无回归。

- [ ] **Step 13: Commit**

```bash
git add core/character_config.py prompt/Brain.j2 core/prompt_assembler.py \
        Module/task_system/schema/protocol.py core/agent.py
git commit -m "fix: Brain.j2 prompt——Identity/情绪Protocol/时间戳/Perception"
```

---

### Task 2: todo.md 清理

**Files:**
- Modify: `todo.md`

- [ ] **Step 1: 划掉已完成条目**

在 `todo.md` "已知 Bug" 表格中：

1. "Brain.j2 硬编码工具列表严重过时（7/12）" → 改为划线 `~~...~~` + `✅ 已修复`
2. "Brain.j2 缺少 '你是谁/用户是谁'" → 同上
3. "Dual-Track Protocol 要求模型提取不存在的情绪数据" → 同上
4. "Brain.j2 记忆时间戳渲染为 Unix 整数" → 同上

- [ ] **Step 2: Commit**

```bash
git add todo.md
git commit -m "chore: 划掉 todo.md 中已修复的 Brain.j2 条目"
```

---

## 自审清单

| spec 要求 | Task | 验证 |
|----------|------|------|
| personality_block "角色名"→"你是" | Task 1 Step 1 | test_personality_block_format PASS |
| fallback 去重"你是" | Task 1 Step 2 | fallback 不出双"你是" |
| UserProfile.display_name | Task 1 Step 3 | agent 启动无报错 |
| agent 传 UserProfile | Task 1 Step 4 | user_name 流入 Brain.j2 |
| Brain.j2 Interaction 块 | Task 1 Step 5 | 系统提示含"正在和妖梦对话" |
| BrainSystemPrompt 新签名 | Task 1 Step 6 | 模板渲染无 KeyError |
| prompt_assembler 传参 | Task 1 Step 7 | name/user_name 传入模板 |
| 丰富 perception | Task 1 Step 8 | perception 含 mood/STT 标签，不截断 |
| Protocol 措辞 | Task 1 Step 9 | 措辞不再要求不存在的 Core Emotion |
| datetime filter ×2 | Task 1 Step 10 | 记忆时间戳显示"2026-06-23 14:30" |
| timestamp float | Task 1 Step 11 | datetime filter 正常渲染 |
| todo 划线 | Task 2 Step 1 | 4 条 Brain.j2 问题划线 |
