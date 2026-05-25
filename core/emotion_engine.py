"""EmotionEngine — keyword-based mood tracking + expression dispatch.

Analyzes user and assistant messages to maintain an emotional state,
then pushes expression changes through the OutputManager.

Production path: replace keyword rules with a lightweight LLM call.
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

    def analyze_user(self, text: str) -> None:
        """Analyze user message and update mood + push expressions."""
        detected = self._detect_mood(text)
        if detected == "neutral":
            return

        current = self._session.get_param("mood", "neutral")
        intensity = self._session.get_param("mood_intensity", 0.5)

        if detected != current:
            self._session.set_param("mood", detected)
            self._session.set_param("mood_intensity", min(1.0, intensity + 0.1))
            self._push_expression(detected, intensity)
            logger.debug("Mood: %s → %s (intensity=%.2f)", current, detected, intensity)
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
        import asyncio
        asyncio.ensure_future(self._output.expression(expr, intensity=val))

    def _push_idle(self) -> None:
        """Push one of the idle expressions."""
        import random
        if self._idle_expressions:
            expr = random.choice(self._idle_expressions)
            import asyncio
            asyncio.ensure_future(self._output.expression(expr, intensity=0.3))
