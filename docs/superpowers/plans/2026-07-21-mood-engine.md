# Mood 引擎 — LLM 自由文本心情 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace keyword-based Mood StrEnum with LLM-generated free-text `<mood>` tag embedded in the main response stream.

**Architecture:** Delete `Mood` enum + `_MOOD_TO_TTS_EMOTION`. LLM outputs `<mood>心情描述|强度</mood>` before `<inner_voice>`. Stream parser extracts it via a new MOOD_PRE→MOOD→DONE state machine, updates CharacterState, then continues to inner_voice/SentenceBuffer. Keyword detection retained as fallback.

**Tech Stack:** Python 3.11+, asyncio streaming, Jinja2 templates, vanilla JS frontend

## Global Constraints

- `CharacterState.mood` type changes from `Mood` → `str`, default `"平静"`
- `MOOD_KEYWORDS` and `_detect_mood()` retained as fallback — do NOT delete
- `_STT_EMOTION_EMOJI`, `stt_emotion_emoji()`, `stt_event_emoji()` retained in mood.py
- All `.value` calls on mood removed (mood is now plain str)
- All `from core.schema.mood import Mood` imports removed
- Zero additional API calls — mood is extracted from the main LLM stream
- Spec: `docs/superpowers/specs/2026-07-21-mood-engine-design.md`

---

### Task 1: Schema cleanup — delete Mood enum

**Files:**
- Modify: `core/schema/mood.py:5-48`
- Modify: `core/schema/character.py:26`
- Modify: `core/schema/__init__.py:60`
- Modify: `core/personality/engine.py:25,427-434,442-487,455-457`

**Interfaces:**
- Produces: `CharacterState.mood: str` (was `Mood`), default `"平静"`
- Produces: `engine._push_expression(mood_text: str, intensity: float)` — mood_text is plain string
- Produces: `engine.analyze_assistant()` — decay intensity only, no mood text reset
- Produces: `engine.analyze_user()` — unchanged (retained as fallback, removed `.value` calls)
- Produces: `_detect_mood()` returns `Mood` → returns `str` (keywords match mood key strings)

- [ ] **Step 1: Delete Mood enum from core/schema/mood.py**

Remove `Mood(StrEnum)` class and `_MOOD_TO_TTS_EMOTION` dict. Keep `_STT_EMOTION_EMOJI`, `stt_emotion_emoji()`, `stt_event_emoji()`, `_STT_EVENT_EMOJI`.

```python
# core/schema/mood.py — after edit
"""STT 情绪 emoji 工具函数。"""
from enum import StrEnum  # 删除此行

_STT_EMOTION_EMOJI: dict[str, str] = {
    "HAPPY": "\U0001f60a", "SAD": "\U0001f614", "ANGRY": "\U0001f621",
    "NEUTRAL": "", "FEARFUL": "\U0001f630", "DISGUSTED": "\U0001f922",
    "SURPRISED": "\U0001f62e",
}

def stt_emotion_emoji(tag: str) -> str:
    return _STT_EMOTION_EMOJI.get(tag, "")

_STT_EVENT_EMOJI: dict[str, str] = {
    "Speech": "", "BGM": "\U0001f3bc", "Applause": "\U0001f44f",
    "Laughter": "\U0001f600", "Cry": "\U0001f62d",
    "Cough": "\U0001f637", "Sneeze": "\U0001f927",
    "Breath": "", "Sing": "", "Speech_Noise": "", "GBG": "",
    "Event_UNK": "",
}

def stt_event_emoji(event: str) -> str:
    return _STT_EVENT_EMOJI.get(event, "")
```

- [ ] **Step 2: Update CharacterState.mood type**

```python
# core/schema/character.py line 26
# Before:
    mood: Mood = Mood.NEUTRAL
# After:
    mood: str = "平静"
```

Also remove `from .mood import Mood` from the imports (line 9).

- [ ] **Step 3: Update core/schema/__init__.py line 60**

```python
# Before:
from core.schema.mood import Mood, stt_emotion_emoji, stt_event_emoji  # noqa: F401
# After:
from core.schema.mood import stt_emotion_emoji, stt_event_emoji  # noqa: F401
```

- [ ] **Step 4: Update engine.py — remove Mood import, simplify analyze_assistant**

```python
# core/personality/engine.py line 25 — remove Mood import
# Before:
from core.schema.mood import Mood
# After: (delete the line)
```

```python
# engine.py lines 427-434 — analyze_assistant: decay intensity only
# Before:
    def analyze_assistant(self, _text: str) -> None:
        """After assistant responds, decay mood toward neutral."""
        intensity = self.state.mood_intensity
        new_intensity = max(0.1, intensity - 0.15)
        self.state.mood_intensity = new_intensity
        if new_intensity < 0.2:
            self.state.mood = Mood.NEUTRAL
            self._push_idle()
# After:
    def analyze_assistant(self, _text: str) -> None:
        """每轮回复后衰减 mood intensity。mood 文本由 LLM 下轮 <mood> 更新。"""
        intensity = self.state.mood_intensity
        new_intensity = max(0.1, intensity - 0.15)
        self.state.mood_intensity = new_intensity
        if new_intensity < 0.2:
            self._push_idle()
```

- [ ] **Step 5: Update _detect_mood() — return str instead of Mood**

```python
# engine.py _detect_mood — change return type and return values
# Before (lines 442-454):
    def _detect_mood(self, text: str) -> Mood:
        """Simple keyword-based mood detection. Returns the strongest match."""
        scores: dict[str, int] = {}
        for mood_key, (user_kw, _) in MOOD_KEYWORDS.items():
            if mood_key == "neutral":
                continue
            score = sum(1 for kw in user_kw if kw in text)
            if score > 0:
                scores[mood_key] = score
        if not scores:
            return Mood.NEUTRAL
        best_key = max(scores, key=lambda k: scores[k])
        return Mood(best_key)
# After:
    def _detect_mood(self, text: str) -> str:
        """Simple keyword-based mood detection. Returns the strongest match."""
        scores: dict[str, int] = {}
        for mood_key, (user_kw, _) in MOOD_KEYWORDS.items():
            if mood_key == "neutral":
                continue
            score = sum(1 for kw in user_kw if kw in text)
            if score > 0:
                scores[mood_key] = score
        if not scores:
            return "neutral"
        best_key = max(scores, key=lambda k: scores[k])
        return best_key
```

- [ ] **Step 6: Update analyze_user() — remove .value calls**

```python
# engine.py analyze_user — remove .value from logger line 423
# Before:
            logger.info("心情 (关键词): %s → %s", current.value, detected.value)
# After:
            logger.info("心情 (关键词): %s → %s", current, detected)
```

Also remove `if detected == Mood.NEUTRAL:` check (line 413) — replace with `if detected == "neutral":`.

- [ ] **Step 7: Run quick suite to verify no import errors**

Run: `python -m pytest core/tests/test_phase1.py::TestEmotionEngine -q --tb=short`
Expected: ImportError/AttributeError failures (tests still reference Mood enum — will fix in Task 7)

- [ ] **Step 8: Commit**

```bash
git add core/schema/mood.py core/schema/character.py core/schema/__init__.py core/personality/engine.py
git commit -m "refactor: delete Mood enum, change mood to free-text str

- Delete Mood(StrEnum) and _MOOD_TO_TTS_EMOTION from mood.py
- CharacterState.mood: Mood → str, default '平静'
- analyze_assistant: decay intensity only, no mood text reset
- _detect_mood returns str instead of Mood
- Remove .value calls and Mood import from engine.py"
```

---

### Task 2: Prompt templates — F2 OutputFormat + C3 Emotion

**Files:**
- Modify: `Module/system_prompt/templates/f2_output_format.j2:8` (before inner_voice instruction)
- Modify: `Module/system_prompt/templates/c3_emotion.j2:11` (mood rendering line)

**Interfaces:**
- Consumes: `mood` is now a free-text string from CharacterState
- Produces: F2 instructs LLM to output `<mood>` tag before `<inner_voice>`

- [ ] **Step 1: Add <mood> instruction to f2_output_format.j2**

Insert before the `<inner_voice>` instruction (before line 9 "每次回复都必须包含 <inner_voice>"):

```jinja2
**心情声明（必须）：**
回复最开头先声明你当前的心情，格式为 <mood>心情描述|强度</mood>。
心情描述用自然语言（如"被妖梦的好消息感染，心里暖暖的"），
强度为 0.0-1.0 的数字。然后正常输出 <inner_voice> 和回复。

格式示例：
<mood>被夸奖后有点不好意思，但暗自开心|0.7</mood>
<inner_voice>她居然注意到了，有点害羞但还是很高兴</inner_voice>
诶嘿~被你发现啦♪

注意：
- <mood> 必须是整个回复的第一个标签，必须闭合（有 </mood>）
- 心情描述用中文，1-2 句以内

```

- [ ] **Step 2: Update c3_emotion.j2 — remove "可用心情" note**

Line 11 `当前心情: {{ mood }}` — no change needed since `mood` is now free-text. Just remove the "可用心情" line if it was added before (it wasn't — spec said we'd add it but then decided against).

No change to c3_emotion.j2 — `{{ mood }}` already works with str.

- [ ] **Step 3: Commit**

```bash
git add Module/system_prompt/templates/f2_output_format.j2
git commit -m "feat: add <mood> output instruction to F2 OutputFormat"
```

---

### Task 3: Stream parser — mood state machine + _apply_mood

**Files:**
- Modify: `core/cognitive/loop.py:49,268-275` (remove old import + set_emotion call)
- Modify: `core/cognitive/loop.py:347-446` (insert mood state machine before inner_voice)
- Modify: `core/cognitive/loop.py:663-677` (add fallback + update public_text stripping)

**Interfaces:**
- Consumes: `self._character_engine`, `self._tts_mgr`, `self._output`
- Produces: `_apply_mood(raw: str) -> None` (module-level function in loop.py)
- Note: `mood_applied: bool` flag set when `<mood>` parsed, checked at stream end for fallback

- [ ] **Step 1: Remove old Mood imports and set_emotion call**

```python
# loop.py line 49 — before:
from core.schema.mood import _MOOD_TO_TTS_EMOTION, Mood
# After: (delete the line)
```

```python
# loop.py lines 268-274 — before:
        if self._character_engine:
            if stt_emotion and stt_emotion != "NEUTRAL":
                self._character_engine.analyze_user_stt(stt_emotion, stt_events)
            self._character_engine.analyze_user(user_message)
            # 将当前 mood 映射为 TTS emotion（MiniMax 等后端支持情绪控制）
            mood = self._character_engine.state.mood
            self._tts_mgr.set_emotion(_MOOD_TO_TTS_EMOTION.get(mood))
# After:
        # analyze_user() 改为仅 fallback：LLM 未输出 <mood> 时在流结束后调用
```

Now `analyze_user()` is not called here — it will be called as fallback after streaming if no `<mood>` detected.

- [ ] **Step 2: Define _apply_mood function (module-level in loop.py)**

Add before `class CognitiveLoop`:

```python
def _apply_mood(raw: str, engine, tts_mgr) -> None:
    """解析 <mood> 标签内容，更新 CharacterState + push expression + TTS emotion."""
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

- [ ] **Step 3: Insert mood state machine before inner_voice detection**

In `_think_loop`, before the inner_voice state machine (before line 347 `# inner_voice 标签检测状态机`), add:

```python
                # mood 标签检测状态机 — 在 inner_voice 之前
                MOOD_OPEN = "<mood>"
                MOOD_CLOSE = "</mood>"
                mood_state = "MOOD_PRE"  # MOOD_PRE / MOOD / DONE
                mood_buf = ""
                mood_tail = ""
                mood_applied = False
```

In the stream processing loop, before `# --- inner_voice 标签检测 ---` (line 386), add mood parsing:

```python
                            # --- mood 标签检测 (before inner_voice) ---
                            if mood_state == "MOOD_PRE":
                                combined = mood_tail + delta
                                idx = combined.find(MOOD_OPEN)
                                if idx >= 0:
                                    before = combined[:idx].strip()
                                    if before:
                                        # 有非空白内容在 <mood> 之前 → 无 mood 标签
                                        mood_state = "DONE"
                                        delta = combined  # 保留原始内容
                                    else:
                                        mood_state = "MOOD"
                                        delta = combined[idx + len(MOOD_OPEN):]
                                        mood_tail = ""
                                else:
                                    # 第一个非空白不是 '<' → 无 mood 标签
                                    stripped = combined.lstrip()
                                    if stripped and not stripped.startswith("<"):
                                        mood_state = "DONE"
                                        delta = combined  # 保留完整内容继续处理
                                    else:
                                        mood_tail = combined
                                        continue

                            if mood_state == "MOOD":
                                combined = mood_tail + delta
                                idx = combined.find(MOOD_CLOSE)
                                if idx >= 0:
                                    mood_buf += combined[:idx]
                                    delta = combined[idx + len(MOOD_CLOSE):].lstrip()
                                    mood_state = "DONE"
                                    mood_tail = ""
                                    if mood_buf.strip():
                                        _apply_mood(mood_buf.strip(), self._character_engine, self._tts_mgr)
                                        mood_applied = True
                                else:
                                    keep_len = min(len(MOOD_CLOSE) - 1, len(combined))
                                    if len(combined) > keep_len:
                                        mood_buf += combined[:-keep_len]
                                    mood_tail = combined[-keep_len:]
                                    continue

                            if mood_state == "DONE":
                                pass  # fall through to inner_voice processing

                            if not delta:
                                continue
```

- [ ] **Step 4: Add fallback at stream end**

After the stream loop ends (after `except` block for tool-call retry, around line 660 where flush_emojis is called), add fallback:

```python
                # Fallback: LLM 未输出 <mood> → 关键词检测
                if self._character_engine and not mood_applied:
                    self._character_engine.analyze_user(user_message)
```

Also update the public_text stripping at line 667-673 to also strip `<mood>` tag:

```python
                # 从 acc_text 中剥离 <mood> 和 <inner_voice> 标签
                public_text = acc_text
                # Strip <mood>...</mood>
                mood_start = public_text.find("<mood>")
                if mood_start >= 0:
                    mood_close = public_text.find("</mood>", mood_start)
                    if mood_close >= 0:
                        public_text = (public_text[:mood_start] + public_text[mood_close + len("</mood>"):]).strip()
                # Strip <inner_voice>...</inner_voice>
                iv_start = public_text.find("<inner_voice>")
                if iv_start >= 0:
                    iv_close = public_text.find("</inner_voice>", iv_start)
                    if iv_close >= 0:
                        public_text = (public_text[:iv_start] + public_text[iv_close + len("</inner_voice>"):]).strip()
```

- [ ] **Step 5: Commit**

```bash
git add core/cognitive/loop.py
git commit -m "feat: add <mood> stream parser with keyword fallback

- Remove Mood/_MOOD_TO_TTS_EMOTION imports from loop.py
- Add _apply_mood() — parses <mood> tag, updates CharacterState + expression
- Add MOOD_PRE→MOOD→DONE state machine before inner_voice parsing
- Fallback to analyze_user() keyword detection if no <mood> in stream
- Strip <mood> tag from public_text before saving to session"
```

---

### Task 4: TTS adapter — _mood_text_to_tts_emotion

**Files:**
- Modify: `core/adapter/tts_adapter.py:174-176`

**Interfaces:**
- Consumes: `mood_text: str` (free-text from LLM)
- Produces: `set_emotion()` accepts str directly; `_mood_text_to_tts_emotion()` added

- [ ] **Step 1: Add _mood_text_to_tts_emotion and update set_emotion docstring**

```python
# tts_adapter.py — add module-level function
def _mood_text_to_tts_emotion(mood_text: str) -> str:
    """自由文本 mood → TTS 情绪参数 (happy/sad/neutral)。"""
    sad_words = {"难过", "悲伤", "伤心", "低落", "沮丧", "消沉", "失落"}
    happy_words = {"开心", "高兴", "兴奋", "快乐", "喜悦", "期待", "惊喜"}
    if any(w in mood_text for w in sad_words):
        return "sad"
    if any(w in mood_text for w in happy_words):
        return "happy"
    return "neutral"


# Update set_emotion docstring (line 174-176):
    def set_emotion(self, emotion: str | None) -> None:
        """设置本轮 TTS 情绪。自由文本会自动映射为 TTS 参数 (happy/sad/neutral)。"""
        self._emotion = _mood_text_to_tts_emotion(emotion) if emotion else None
```

- [ ] **Step 2: Commit**

```bash
git add core/adapter/tts_adapter.py
git commit -m "feat: add _mood_text_to_tts_emotion for free-text→TTS mapping"
```

---

### Task 5: Fix remaining consumers (.value calls)

**Files:**
- Modify: `core/personality/reflector.py:217`
- Modify: `core/prompt_assembler.py:96`
- Modify: `core/debug/info.py:279`

- [ ] **Step 1: reflector.py line 217 — remove .value**

```python
# Before:
        mood = state.mood.value
# After:
        mood = state.mood
```

- [ ] **Step 2: prompt_assembler.py line 96 — remove .value**

```python
# Before:
            mood = self._personality_engine.state.mood.value
# After:
            mood = self._personality_engine.state.mood
```

- [ ] **Step 3: debug/info.py line 279 — no change needed**

`info.mood = agent._session.character_state.mood` — mood is already directly assigned, no `.value`. Verify it compiles.

- [ ] **Step 4: Commit**

```bash
git add core/personality/reflector.py core/prompt_assembler.py
git commit -m "fix: remove .value calls on mood (mood is now str)"
```

---

### Task 6: Frontend — setMood display free text

**Files:**
- Modify: `static/js/chat.js:328-334,343-344`

- [ ] **Step 1: Update setMood()**

```javascript
// chat.js lines 328-334 — before:
export function setMood(mood, intensity) {
  const tag = document.getElementById('mood-tag');
  if (tag) {
    tag.textContent = mood ? `${moodEmoji(mood)} ${mood} ${Math.round(intensity * 100)}%` : '';
    tag.style.color = moodColor(mood);
  }
}

// After:
export function setMood(mood, intensity) {
  const tag = document.getElementById('mood-tag');
  if (tag) {
    tag.textContent = mood ? `${mood} (${Math.round(intensity * 100)}%)` : '';
    tag.style.color = 'var(--muted)';
  }
}
```

- [ ] **Step 2: Delete MOOD_EMOJI and MOOD_COLOR**

```javascript
// chat.js lines 343-344 — delete:
const MOOD_EMOJI = { happy: '😊', sad: '😢', excited: '🤩', curious: '🤔', neutral: '😐' };
const MOOD_COLOR = { happy: 'var(--happy)', sad: 'var(--sad)', excited: 'var(--excited)', neutral: 'var(--muted)' };
```

Also delete the helper functions `moodEmoji()` and `moodColor()` at lines 346-347.

- [ ] **Step 3: Commit**

```bash
git add static/js/chat.js
git commit -m "feat: display free-text mood in frontend mood-tag

Remove MOOD_EMOJI/MOOD_COLOR maps. setMood() now shows LLM-generated
Chinese mood description directly with intensity percentage."
```

---

### Task 7: Test migration

**Files:**
- Modify: `core/tests/test_phase1.py:137-236` (TestEmotionEngine class)
- Modify: `core/tests/test_phase3.py:643-667` (mood-related tests)
- Modify: `core/personality/tests/test_engine.py` (if any `.value` calls)

- [ ] **Step 1: Fix test_phase1.py TestEmotionEngine — change Mood enum to str**

```python
# test_phase1.py — find all `Mood.HAPPY`, `Mood.SAD`, `Mood.NEUTRAL` etc.
# Replace with string equivalents:
#   Mood.HAPPY → "happy"
#   Mood.SAD → "sad"
#   Mood.EXCITED → "excited"
#   Mood.CURIOUS → "curious"
#   Mood.WARM → "warm"
#   Mood.NEUTRAL → "neutral"
#   Mood.CONCERNED → "concerned"

# Example — test_detect_happy (line 138-139):
# Before:
        from core.schema.mood import Mood
        assert engine._detect_mood("哈哈太开心了") == Mood.HAPPY
# After:
        assert engine._detect_mood("哈哈太开心了") == "happy"
```

Remove all `from core.schema.mood import Mood` imports in the test file.

- [ ] **Step 2: Fix test_phase3.py mood tests**

```python
# test_phase3.py line 644-648 — test_multiple_moods_tie_breaker:
# Before:
        from core.schema.mood import Mood
        mood = engine._detect_mood("哈哈！")
        assert mood == Mood.HAPPY
# After:
        mood = engine._detect_mood("哈哈！")
        assert mood == "happy"

# test_phase3.py line 660-666 — test_intensity_floor_and_neutral:
# Before:
        from core.schema.mood import Mood
        engine.state.mood = Mood.HAPPY
        engine.state.mood_intensity = 0.12
        engine.analyze_assistant("好的")
        assert engine.state.mood_intensity >= 0.1
        assert engine.state.mood == Mood.NEUTRAL
# After:
        engine.state.mood = "happy"
        engine.state.mood_intensity = 0.12
        engine.analyze_assistant("好的")
        assert engine.state.mood_intensity >= 0.1
        # mood text no longer reset by analyze_assistant
        assert engine.state.mood == "happy"
```

- [ ] **Step 3: Check personality/tests/ for .value calls**

Run: `grep -rn "\.value" core/personality/tests/`
Expected: no `.value` calls on mood (if any found, remove `.value`).

- [ ] **Step 4: Run emotion tests to verify**

Run: `python -m pytest core/tests/test_phase1.py::TestEmotionEngine core/tests/test_phase3.py -q --tb=short`
Expected: all mood-related tests pass

- [ ] **Step 5: Commit**

```bash
git add core/tests/test_phase1.py core/tests/test_phase3.py core/personality/tests/
git commit -m "test: migrate mood tests from Mood enum to str

Replace all Mood.HAPPY/SAD/NEUTRAL/etc with string equivalents.
analyze_assistant no longer resets mood text to neutral."
```

---

### Task 8: New tests — _apply_mood, stream parser, TTS mapping

**Files:**
- Create: `core/tests/test_mood_engine.py`

- [ ] **Step 1: Write test file**

```python
"""Mood 引擎新功能测试 — _apply_mood, _mood_text_to_tts_emotion, analyze_assistant."""
import pytest


@pytest.mark.unit
class TestApplyMood:
    def test_normal_with_intensity(self):
        """_apply_mood 正常解析 '心情描述|0.7'。"""
        from core.cognitive.loop import _apply_mood
        from unittest.mock import MagicMock

        engine = MagicMock()
        engine.state.mood = "平静"
        engine.state.mood_intensity = 0.5
        tts_mgr = MagicMock()

        _apply_mood("被夸奖后有点不好意思，但暗自开心|0.7", engine, tts_mgr)

        assert engine.state.mood == "被夸奖后有点不好意思，但暗自开心"
        assert engine.state.mood_intensity == 0.7
        engine._push_expression.assert_called_once_with(
            "被夸奖后有点不好意思，但暗自开心", 0.7)
        tts_mgr.set_emotion.assert_called_once_with(
            "被夸奖后有点不好意思，但暗自开心")

    def test_no_intensity_defaults(self):
        """无 | 分隔符时强度默认 0.5。"""
        from core.cognitive.loop import _apply_mood
        from unittest.mock import MagicMock

        engine = MagicMock()
        engine.state.mood = "平静"
        tts_mgr = MagicMock()

        _apply_mood("心情很复杂", engine, tts_mgr)

        assert engine.state.mood == "心情很复杂"
        assert engine.state.mood_intensity == 0.5

    def test_intensity_not_numeric(self):
        """强度非数字时默认 0.5。"""
        from core.cognitive.loop import _apply_mood
        from unittest.mock import MagicMock

        engine = MagicMock()
        tts_mgr = MagicMock()

        _apply_mood("开心|很高", engine, tts_mgr)

        assert engine.state.mood == "开心"
        assert engine.state.mood_intensity == 0.5

    def test_intensity_clamped(self):
        """强度超出 [0.1, 1.0] 时 clamp。"""
        from core.cognitive.loop import _apply_mood
        from unittest.mock import MagicMock

        engine = MagicMock()
        tts_mgr = MagicMock()

        _apply_mood("兴奋|1.5", engine, tts_mgr)
        assert engine.state.mood_intensity == 1.0

        _apply_mood("低落|0.0", engine, tts_mgr)
        assert engine.state.mood_intensity == 0.1

    def test_empty_mood_text(self):
        """空 mood 文本时不更新状态。"""
        from core.cognitive.loop import _apply_mood
        from unittest.mock import MagicMock

        engine = MagicMock()
        tts_mgr = MagicMock()

        _apply_mood("|0.5", engine, tts_mgr)
        # mood unchanged, push_expression not called
        engine._push_expression.assert_not_called()


@pytest.mark.unit
class TestMoodToTtsEmotion:
    def test_happy_keywords(self):
        from core.adapter.tts_adapter import _mood_text_to_tts_emotion
        assert _mood_text_to_tts_emotion("开心得像个小孩子") == "happy"
        assert _mood_text_to_tts_emotion("兴奋不已") == "happy"
        assert _mood_text_to_tts_emotion("心里充满喜悦") == "happy"

    def test_sad_keywords(self):
        from core.adapter.tts_adapter import _mood_text_to_tts_emotion
        assert _mood_text_to_tts_emotion("有点难过呢") == "sad"
        assert _mood_text_to_tts_emotion("心情低落") == "sad"
        assert _mood_text_to_tts_emotion("很沮丧的感觉") == "sad"

    def test_neutral_default(self):
        from core.adapter.tts_adapter import _mood_text_to_tts_emotion
        assert _mood_text_to_tts_emotion("平静如水") == "neutral"
        assert _mood_text_to_tts_emotion("被夸奖后暖暖的") == "neutral"

    def test_happy_wins_over_sad(self):
        """同时包含 happy 和 sad 关键词时 happy 优先（先检查 sad，后检查 happy 覆盖）。"""
        from core.adapter.tts_adapter import _mood_text_to_tts_emotion
        assert _mood_text_to_tts_emotion("虽然难过但还是很开心") == "happy"


@pytest.mark.unit
class TestAnalyzeAssistantDecay:
    def test_decay_only_no_mood_reset(self):
        """analyze_assistant 只衰减 intensity，不重置 mood 文本。"""
        from core.character_config import CharacterConfig
        from core.schema.character import CharacterState
        from core.schema.types import SessionId
        from Module.output import OutputManager
        from core.personality import CharacterEngine
        from unittest.mock import MagicMock, AsyncMock
        from core.adapter.llm_adapter import LLMAdapter

        char = CharacterConfig(name="Test")
        state = CharacterState(session_id=SessionId("test"))
        state.mood = "被夸奖后很开心"
        state.mood_intensity = 0.12  # below 0.2 — should trigger _push_idle
        output = OutputManager()
        output.expression = MagicMock()
        llm = MagicMock(spec=LLMAdapter)
        llm.think = AsyncMock()
        engine = CharacterEngine(
            llm=llm, state=state, character_name="Test",
            user_name="妖梦", base_personality="",
            character_config=char, output=output,
        )

        engine.analyze_assistant("好的")

        assert state.mood_intensity >= 0.1
        # mood text is NOT reset — LLM handles it next turn via <mood>
        assert state.mood == "被夸奖后很开心"
```

- [ ] **Step 2: Run new tests**

Run: `python -m pytest core/tests/test_mood_engine.py -v`
Expected: all 11 tests pass

- [ ] **Step 3: Commit**

```bash
git add core/tests/test_mood_engine.py
git commit -m "test: add unit tests for mood engine — _apply_mood, TTS mapping, decay"
```

---

### Task 9: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Run full quick suite**

Run: `make test-quick`
Expected: 1639+ passed (new tests added), 0 regressions

- [ ] **Step 2: Check for stray Mood references**

Run: `grep -rn "from core.schema.mood import Mood\|import.*Mood" --include="*.py" | grep -v __pycache__ | grep -v ".pyc"`
Expected: no results (all Mood imports removed)

Run: `grep -rn "\.value" --include="*.py" core/personality/ core/cognitive/ | grep mood`
Expected: no results (no `.value` calls on mood)

- [ ] **Step 3: Commit if any fixes needed, otherwise done**

```bash
# If clean:
echo "All checks pass"
```
