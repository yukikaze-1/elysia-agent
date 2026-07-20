# Mood 引擎 — LLM 自由文本心情

> **Goal:** 用 LLM 在主回复中内联输出 `<mood>` 标签替代关键词检测，让 AI 心情由 LLM 根据角色性格 + 对话上下文动态生成。
>
> **Tech Stack:** Python 3.11+, Jinja2 模板, 流式解析状态机
>
> **关联:** 本次改动是 CharacterEngine Phase 2 的 L4 Mood 升级第一步。

---

## 1. 架构

```
用户消息 + STT 情绪 + C1 性格 + C3 当前心情
  → System prompt（F2 指示 LLM 输出 <mood>）
    → LLM 流式响应:
        <mood>被妖梦的好消息感染，心里暖暖的|0.7</mood>
        <inner_voice>...</inner_voice>
        那真是太好了呢~♪
      → 流式解析: 检测 <mood> → 更新 CharacterState → 剥离标签
        → 剩余文本正常进入 inner_voice / SentenceBuffer / TTS
```

## 2. 变更清单

### 2.1 删除

| 文件 | 内容 |
|------|------|
| `core/schema/mood.py` | `Mood` 枚举类、`_MOOD_TO_TTS_EMOTION` 字典 |
| `static/js/chat.js` | `MOOD_EMOJI`、`MOOD_COLOR` 映射表 |

### 2.2 修改

| 文件 | 改动 |
|------|------|
| `core/schema/mood.py` | 保留 `_STT_EMOTION_EMOJI`、`stt_emotion_emoji()`、`stt_event_emoji()` |
| `core/schema/character.py` | `mood: Mood` → `mood: str`，默认值 `"平静"` |
| `core/personality/engine.py` | `_push_expression` 直接用 mood 文本；`analyze_user()` 保留关键词检测作为 fallback（仅 LLM 未产出 `<mood>` 时调用）；`analyze_assistant()` 只衰减 intensity 不设 mood 文本（LLM 下轮自己描述心情变化） |
| `core/personality/reflector.py` | L217: `state.mood.value` → `state.mood`（mood 已是 str，无需 `.value`） |
| `core/schema/__init__.py` | 删除 `from core.schema.mood import Mood`（Mood 枚举已移除） |
| `core/cognitive/loop.py` | 删除 `from core.schema.mood import _MOOD_TO_TTS_EMOTION, Mood`（L49）；删除 `self._tts_mgr.set_emotion(_MOOD_TO_TTS_EMOTION.get(mood))`（L274-275，改由 `_apply_mood()` 内联调用）；`_think_loop` 流式处理中插入 mood 标签解析状态机；流结束后若未检测到 `<mood>` 则 fallback 到 `analyze_user()` |
| `core/adapter/tts_adapter.py` | `set_emotion` 改为接收自由文本，内部做关键词→TTS 情绪映射 |
| `Module/system_prompt/templates/f2_output_format.j2` | 在 `<inner_voice>` 指令前加 `<mood>` 输出指令 |
| `Module/system_prompt/templates/c3_emotion.j2` | `{{ mood }}` 直接渲染 LLM 自由文本，加 `可用心情` 移除 |
| `static/js/chat.js` | `setMood()` 直接显示 mood 文本，删除 emoji/color 查找 |
| `core/debug/info.py` | mood 字段类型从 Mood 改为 str |
| `core/prompt_assembler.py` | L96: `state.mood.value` → `state.mood` |

### 2.3 新增

| 文件 | 内容 |
|------|------|
| `core/cognitive/loop.py` | `_apply_mood(raw: str)` — 解析 `<mood>` 标签内容，更新 CharacterState + push expression + TTS emotion |
| `core/adapter/tts_adapter.py` | `_mood_text_to_tts_emotion(mood_text: str) -> str` — 自由文本→TTS 情绪参数（happy/sad/neutral）的简单关键词映射 |

## 3. 数据流

### 3.1 Prompt 改动

**F2 OutputFormat** — 在 `<inner_voice>` 之前插入：

```
**心情声明：**
回复最开头先声明你当前的心情，格式为 <mood>心情描述|强度</mood>。
心情描述用自然语言（如"被妖梦的好消息感染，心里暖暖的"），
强度为 0.0-1.0 的数字。然后正常输出 <inner_voice> 和回复。

格式示例：
<mood>被夸奖后有点不好意思，但暗自开心|0.7</mood>
<inner_voice>她居然注意到了，有点害羞但还是很高兴</inner_voice>
诶嘿~被你发现啦♪

注意：
- <mood> 必须是整个回复的第一个标签
- 心情描述用中文，1-2 句以内
```

### 3.2 流式解析状态机

当前 inner_voice 状态机：`PRE → THINK → REPLY`
在其之前插入 mood 状态：`MOOD_PRE → MOOD → DONE`

```
MOOD_PRE: 缓冲 delta，查找 "<mood>"
  → 找到 → 进入 MOOD，继续
  → 第一个非空白字符不是 "<" → 判定无 mood 标签，跳 DONE，fallback 关键词

MOOD: 收集内容，查找 "</mood>"
  → 找到 → 解析 mood 文本 + 强度 → _apply_mood() → 进入 DONE
  → 未找到 → 继续缓冲（跨 chunk 拼接）

DONE: mood 处理完毕，剩余 delta 进入 inner_voice 状态机
```

### 3.3 `_apply_mood(raw)`

```python
def _apply_mood(raw: str):
    # "被夸奖后有点不好意思，但暗自开心|0.7"
    if "|" in raw:
        mood_text, intensity_str = raw.rsplit("|", 1)
        try:
            intensity = float(intensity_str.strip())
            intensity = min(1.0, max(0.1, intensity))
        except ValueError:
            intensity = 0.5
    else:
        mood_text = raw.strip()
        intensity = 0.5

    if not mood_text:
        return

    engine.state.mood = mood_text
    engine.state.mood_intensity = intensity
    engine._push_expression(mood_text, intensity)
    tts_mgr.set_emotion(mood_text)
```

### 3.4 TTS 情绪映射

```python
def _mood_text_to_tts_emotion(mood_text: str) -> str:
    sad_words = {"难过", "悲伤", "伤心", "低落", "沮丧", "消沉", "失落"}
    happy_words = {"开心", "高兴", "兴奋", "快乐", "喜悦", "期待", "惊喜"}
    if any(w in mood_text for w in sad_words):
        return "sad"
    if any(w in mood_text for w in happy_words):
        return "happy"
    return "neutral"
```

## 4. 降级策略

| 场景 | 处理 |
|------|------|
| LLM 未输出 `<mood>` | fallback 到 `_detect_mood()` 关键词检测（保留 MOOD_KEYWORDS） |
| `<mood>` 标签格式错误（无 `\|`、强度非数字） | 强度默认 0.5 |
| LLM 不可用 | `analyze_user()` 保留关键词检测作为冷启动路径 |

`MOOD_KEYWORDS` 字典和 `_detect_mood()` 保留在 `engine.py` 中作为 fallback，不删除。

### 4.1 `analyze_user()` — 仅 fallback

`analyze_user()` 不再在 `_think_loop` 中无条件调用。仅当 LLM 流式响应未包含 `<mood>` 标签时，在流结束后作为 fallback 调用一次：

```python
# _think_loop 流结束后
if not mood_applied:  # LLM 未输出 <mood>
    self._character_engine.analyze_user(user_message)
```

### 4.2 `analyze_assistant()` — 只衰减 intensity

```python
def analyze_assistant(self, _text: str) -> None:
    """每轮回复后衰减 mood intensity。mood 文本由 LLM 下轮 <mood> 更新。"""
    intensity = self.state.mood_intensity
    new_intensity = max(0.1, intensity - 0.15)
    self.state.mood_intensity = new_intensity
    if new_intensity < 0.2:
        self._push_idle()
```

不再重置 `mood` 文本（没有 `Mood.NEUTRAL` 了）。LLM 在下轮回复时通过 `<mood>` 标签自行描述当前心情（如"聊完后心情平复了许多"）。

## 5. 前端

`setMood(mood, intensity)` 改为：

```javascript
export function setMood(mood, intensity) {
  const tag = document.getElementById('mood-tag');
  if (tag) {
    tag.textContent = mood ? `${mood} (${Math.round(intensity * 100)}%)` : '';
    tag.style.color = 'var(--muted)';
  }
}
```

删除 `MOOD_EMOJI` 和 `MOOD_COLOR`。mood 直接显示 LLM 产出的中文描述。

## 6. 影响评估

### 性能

- mood 标签解析是纯字符串操作，在主 LLM 流式响应中内联，零额外 API 调用
- Prompt 增加 ~5 行，约 100 tokens

### 兼容性

- `CharacterState.mood` 类型从 `Mood` 改为 `str`，MongoDB 中已存在的数据不受影响（motor 不校验类型）
- 关键词检测保留为 fallback，现有行为不丢失
- 前端 mood-tag 从固定 emoji+英文词 → 中文自然语言，对用户更可读

### 依赖

- 依赖主 LLM 能遵循 F2 输出格式指令（当前已遵循 `<inner_voice>` 协议，加 `<mood>` 无额外风险）

## 7. 测试

### 7.1 现有测试迁移

| 文件 | Mood 引用数 | 处理 |
|------|-----------|------|
| `core/tests/test_phase1.py` | ~15 处 `Mood.HAPPY`/`Mood.SAD` 等 | 重写：关键词检测测试保留（fallback），Mood 枚举断言改为 str 断言 |
| `core/tests/test_phase3.py` | L645-666 `Mood.HAPPY`/`Mood.NEUTRAL` | 改为 str 断言 + 新增 `<mood>` 标签解析测试 |
| `core/personality/tests/` | 直接/间接 Mood 引用 | 检查并更新所有 `.value` 调用 |

### 7.2 新增测试

| 层 | 测试点 |
|----|--------|
| 单元 | `_apply_mood()` 正常解析 / 缺强度 / 强度非数字 / 空文本 |
| 单元 | `_mood_text_to_tts_emotion()` 各关键词覆盖 |
| 单元 | `analyze_assistant()` 只衰减 intensity，不重置 mood 文本 |
| 集成 | 流式解析：mood 标签跨 chunk 拼接 |
| 集成 | 流式解析：无 mood 标签 → fallback 关键词 |
| 集成 | 端到端：完整 LLM 响应含 `<mood>` + `<inner_voice>` + 公开文本 |
