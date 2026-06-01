"""EmotionEngine — keyword-based + STT emotion tag mood tracking + expression dispatch.

Analyzes user and assistant messages to maintain an emotional state,
then pushes expression changes through the OutputManager.

Primary path (voice): STT emotion tags from SenseVoice
Fallback (text): keyword matching
Future: Amygdala LLM for text semantic analysis
"""

import logging

from Module.output import OutputManager

from .memory.session import SessionMemory
from .character_config import CharacterConfig

logger = logging.getLogger(__name__)

MOOD_KEYWORDS: dict[str, tuple[list[str], list[str]]] = {
    "happy": (
        ["哈哈", "开心", "太好了", "喜欢", "棒", "nice", "嘿嘿", "耶", "赞", "爱你"],
        ["你喜欢", "我也开心", "太好了", "哈哈", "嘿嘿", "开心"],
    ),
    "excited": (
        ["!", "！", "天哪", "wow", "太棒", "厉害", "牛逼", "惊", "竟然"],
        ["!", "！", "兴奋", "激动", "期待"],
    ),
    "sad": (
        ["难过", "伤心", "哭", "失败", "失去", "遗憾", "倒霉", "唉", "烦", "累"],
        ["别难过", "抱抱", "辛苦了", "会好起来的"],
    ),
    "curious": (
        ["为什么", "怎么", "什么原因", "讲讲", "说说", "好奇", "告诉我"],
        ["好问题", "有趣", "想知道"],
    ),
    "neutral": (
        [],
        [],
    ),
}

# STT 情绪 → AI mood 映射（非 1:1 — AI 不直接复制用户情绪）
_STT_EMOTION_TO_MOOD: dict[str, str] = {
    "HAPPY":     "warm",
    "SAD":       "concerned",
    "ANGRY":     "calming",
    "FEARFUL":   "reassuring",
    "DISGUSTED": "understanding",
    "SURPRISED": "curious",
    "NEUTRAL":   "",  # 不触发变化
}

# STT 情绪 emoji
_STT_EMOTION_EMOJI: dict[str, str] = {
    "HAPPY": "😊", "SAD": "😔", "ANGRY": "😡", "NEUTRAL": "",
    "FEARFUL": "😰", "DISGUSTED": "🤢", "SURPRISED": "😮",
}

# 音频事件 emoji
_STT_EVENT_EMOJI: dict[str, str] = {
    "Speech": "", "BGM": "🎼", "Applause": "👏", "Laughter": "😀",
    "Cry": "😭", "Cough": "😷", "Sneeze": "🤧", "Breath": "",
    "Sing": "", "Speech_Noise": "", "GBG": "", "Event_UNK": "",
}


def stt_emotion_emoji(tag: str) -> str:
    """返回 STT 情绪对应的 emoji。"""
    return _STT_EMOTION_EMOJI.get(tag, "")


def stt_event_emoji(event: str) -> str:
    """返回音频事件对应的 emoji。"""
    return _STT_EVENT_EMOJI.get(event, "")


class EmotionEngine:
    """Tracks and updates the agent's emotional state.

    Updates session params (mood, mood_intensity) and pushes expression
    changes through the OutputManager.
    """

    def __init__(
        self,
        character: CharacterConfig,
        session: SessionMemory,
        output: OutputManager,
    ):
        self._character = character
        self._session = session
        self._output = output
        self._thresholds = character.emotion.thresholds
        self._idle_expressions = character.emotion.idle_expressions

    def analyze_user_stt(self, emotion_tag: str, events: list[str] | None = None) -> str:
        """基于 STT 声学情绪标签更新 AI mood（语音输入主路径）。

        Returns: 更新后的 mood 字符串。
        """
        mood = _STT_EMOTION_TO_MOOD.get(emotion_tag, "")
        if not mood:
            return self._session.get_param("mood", "neutral")

        current = self._session.get_param("mood", "neutral")
        intensity = self._session.get_param("mood_intensity", 0.5)

        if mood != current:
            self._session.set_param("mood", mood)
            self._session.set_param("mood_intensity", min(1.0, intensity + 0.1))
            self._push_expression(mood, intensity)
            emoji = _STT_EMOTION_EMOJI.get(emotion_tag, "")
            logger.info("心情 (STT %s%s): %s → %s", emotion_tag, f" {emoji}" if emoji else "", current, mood)
        else:
            self._session.set_param("mood_intensity", min(1.0, intensity + 0.05))

        # 音频事件 → expression 推送
        if events:
            for evt in events:
                if evt == "Laughter":
                    self._schedule(self._output.expression("laugh", intensity=0.7))
                elif evt == "Cry":
                    self._schedule(self._output.expression("cry", intensity=0.8))
                elif evt == "Applause":
                    self._schedule(self._output.expression("surprised", intensity=0.5))

        return mood

    def analyze_user(self, text: str) -> None:
        """Analyze user message and update mood + push expressions (keyword fallback)."""
        detected = self._detect_mood(text)
        if detected == "neutral":
            return

        current = self._session.get_param("mood", "neutral")
        intensity = self._session.get_param("mood_intensity", 0.5)

        if detected != current:
            self._session.set_param("mood", detected)
            self._session.set_param("mood_intensity", min(1.0, intensity + 0.1))
            self._push_expression(detected, intensity)
            logger.info("心情 (关键词): %s → %s", current, detected)
        else:
            # Same mood, increase intensity
            self._session.set_param("mood_intensity", min(1.0, intensity + 0.05))

    def analyze_assistant(self, _text: str) -> None:
        """After assistant responds, decay mood toward neutral."""
        intensity = self._session.get_param("mood_intensity", 0.5)
        # Decay intensity
        new_intensity = max(0.1, intensity - 0.15)
        self._session.set_param("mood_intensity", new_intensity)
        if new_intensity < 0.2:
            self._session.set_param("mood", "neutral")
            self._push_idle()

    async def push_idle_expression(self) -> None:
        """Push a random idle expression (called periodically or on IDLE entry)."""
        self._push_idle()

    # ---- internal ----

    def _detect_mood(self, text: str) -> str:
        """Simple keyword-based mood detection. Returns the strongest match."""
        scores: dict[str, int] = {}
        for mood, (user_kw, _) in MOOD_KEYWORDS.items():
            if mood == "neutral":
                continue
            score = sum(1 for kw in user_kw if kw in text)
            if score > 0:
                scores[mood] = score
        if not scores:
            return "neutral"
        return max(scores, key=lambda k: scores[k])

    def _push_expression(self, mood: str, intensity: float) -> None:
        """Push expression to output channels."""
        expr_map = {
            "happy": ("happy", intensity),
            "excited": ("surprised", intensity),
            "sad": ("sad", intensity),
            "curious": ("curious", intensity),
        }
        expr, val = expr_map.get(mood, ("neutral", 0.5))
        self._schedule(self._output.expression(expr, intensity=val))

    def _push_idle(self) -> None:
        """Push one of the idle expressions."""
        import random
        if self._idle_expressions:
            expr = random.choice(self._idle_expressions)
            self._schedule(self._output.expression(expr, intensity=0.3))

    @staticmethod
    def _schedule(coro) -> None:
        """Schedule a coroutine on the event loop, if one is running.

        Wrapped so exceptions in the fire-and-forget task are logged
        rather than silently swallowed.
        """
        import asyncio
        try:
            loop = asyncio.get_running_loop()

            async def _safe_wrapper():
                try:
                    await coro
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger = logging.getLogger(__name__)
                    logger.warning("EmotionEngine scheduled task 异常", exc_info=True)

            loop.create_task(_safe_wrapper())
        except RuntimeError:
            pass  # no event loop running — test environment, ignore
