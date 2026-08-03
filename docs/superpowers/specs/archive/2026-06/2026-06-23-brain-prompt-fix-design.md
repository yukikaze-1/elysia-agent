# Brain.j2 Prompt 修复

> 修复 Brain.j2 系统提示中的 Identity、情绪 Protocol、时间戳格式问题。

## 目标

`todo.md` 列出的 Brain.j2 相关 4 个问题（硬编码工具列表已修复，排除）：
1. 缺少 "你是谁/用户是谁" 的明确定义
2. Dual-Track Protocol 要求模型提取不存在的结构化情绪数据
3. 记忆时间戳渲染为 Unix 整数
4. perception 截断 200 字符丢信息

## 改动

### 1. Identity — 明确 "你是谁/用户是谁"

**`core/character_config.py:personality_block()`** — 第一人称：
```python
# 旧
return f"角色名: {self.name}\n性格: {traits_str}\n说话风格: {self.speaking_style}"
# 新
return f"你是 {self.name}。\n性格: {traits_str}\n说话风格: {self.speaking_style}"
```

**`prompt_assembler.py:_render_fallback()` line 197** — 去掉与 personality_block 重复的 "你是"：
```python
# 旧
f"你是 {self._character.name}，一个桌面AI伴侣。",
# 新
f"{self._character.name}，一个桌面AI伴侣。",
```
因为 `personality_block()` 已经在下一行输出 `你是 Elysia。`，fallback 不再重复。

**`Module/task_system/schema/protocol.py:UserProfile`** — 加 `display_name`：
```python
class UserProfile(BaseModel):
    model_config = {"extra": "allow"}
    preferences: str | dict[str, str] = ""
    language: str = "zh"
    display_name: str = ""  # 新增：用户显示名
```

**`core/agent.py:316`** — 传真实 UserProfile：
```python
# 旧
self._cognitive.set_user_profile(None)
# 新
from Module.task_system.schema import UserProfile
self._cognitive.set_user_profile(
    UserProfile(display_name=config.agent.user_name)
)
```

**`prompt/Brain.j2`** — 新增 Interaction 块（紧接 `# Role Definition` 之后）：
```jinja2
# Who You're Talking To
你是 {{ name }}，正在和 {{ user_name }} 对话。
{%- if user_preferences %} 关于 {{ user_name }}：{{ user_preferences }}{% endif %}
```

`BrainSystemPrompt` macro 签名加 `name, user_name, user_preferences` 三个参数。

**`prompt_assembler.py:build_system_prompt()`** — 传参：
```python
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

### 2. Protocol 情绪修复（方案 C）

**2a. 丰富 perception** — `prompt_assembler.py:build_system_prompt()`：

```python
# 旧
perception = f"{user_name}说：{user_message[:200]}"

# 新 — 不截断，注入 mood 和 STT 情绪标签
parts = [f"{user_name}说：{user_message}"]
if stt_emotion and stt_emotion != "NEUTRAL":
    parts.append(f"[语调: {stt_emotion}]")
parts.append(f"[当前心情: {mood}]")
perception = "\n".join(parts)
```

输出示例：
```
妖梦说：帮我查一下天气
[语调: HAPPY]
[当前心情: warm]
```

**2b. 软化 Protocol** — `Brain.j2` 第 215-218 行：

旧：
> You MUST adopt the "Core Emotion" and "Behavioral Tendency" described there as your absolute internal state. **Do not re-interpret the mood based on raw facts; trust the Perception.**

新：
> If the `<perception>` block contains emotional cues (tone tags, mood indicators), adopt them as your emotional baseline. If not, infer the user's emotion from their wording and context. Either way, your external reply should feel emotionally attuned to the user's state. Do not re-interpret the mood based on raw facts when perception provides emotional signals.

### 3. 时间戳 → datetime filter

**`Brain.j2` — 两处**：

```jinja2
{# 旧 #}
- [{{ mem.type }}] {{ mem.timestamp }} (Poignancy: {{ mem.poignancy }}) {{ mem.content }}
- {{ mem.timestamp }} (Poignancy: {{ mem.poignancy }})...

{# 新 #}
- [{{ mem.type }}] {{ mem.timestamp | datetime }} (Poignancy: {{ mem.poignancy }}) {{ mem.content }}
- {{ mem.timestamp | datetime }} (Poignancy: {{ mem.poignancy }})...
```

**`prompt_assembler.py:_get_recalled()`** — timestamp 保留 float（datetime filter 需要）：

```python
# 旧
"timestamp": int(entry.timestamp) if entry.timestamp else int(now.timestamp()),
# 新
"timestamp": entry.timestamp if entry.timestamp else now.timestamp(),
```

`datetime` filter 已在 `PromptManager` 注册，输出 `2026-06-23 14:30`。

### 4. 连带清理 — todo.md

- 划掉 "Brain.j2 硬编码工具列表严重过时（7/12）"（已修复）
- 划掉 "缺少你是谁/用户是谁"
- 划掉 "Protocol 要求模型提取不存在的情绪数据"
- 划掉 "时间戳渲染为 Unix 整数"

## 不改

- 中英混杂标签——风格问题，不影响行为
- `elysia.yaml`——角色配置格式不改
- 工具列表——已是动态生成，Brain.j2 为硬编码副本，保持同步即可

## 测试

```bash
# Prompt 组装测试
python -m pytest core/tests/test_phase3.py -v -k "prompt" -q

# 完整 core 测试确保无回归
python -m pytest core/tests/ -q
```

手动验证：
1. 启动 agent，聊天确认 AI 认知自己为 Elysia、用户为配置名
2. 检查记忆召回中时间显示为可读日期而非 Unix 整数
3. STT 关闭时确认 Protocol 不报"找不到情绪数据"
