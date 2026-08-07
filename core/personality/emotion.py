"""EmotionDetector — keyword mood 检测 + STT 表情推送 + mood 衰减。

纯规则引擎，无 LLM 依赖。
"""
import logging
import random

from core.schema.character import CharacterState
from Module.output import OutputManager
from core.personality._utils import _schedule

logger = logging.getLogger(__name__)

# ---- 情绪关键词 ----
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
    "warm": (
        ["谢谢", "感谢", "感恩", "感激", "温暖", "贴心", "真好", "感动"],
        ["不客气", "我的荣幸", "应该的"],
    ),
    "concerned": (
        ["担心", "焦虑", "不安", "紧张", "害怕", "怎么办", "压力"],
        ["别担心", "会好的", "我理解你的担心"],
    ),
    "calming": (
        ["气死", "生气", "愤怒", "怒火", "离谱", "太过分", "受不了", "岂有此理"],
        ["冷静", "消消气", "别急", "慢慢说"],
    ),
    "reassuring": (
        ["怕", "恐惧", "恐怖", "吓人", "不敢", "担心害怕"],
        ["别怕", "有我在", "我会保护你"],
    ),
    "understanding": (
        ["恶心", "想吐", "厌恶", "讨厌", "反感", "受不了这个"],
        ["我懂", "确实不舒服", "需要适应"],
    ),
}


class EmotionDetector:
    """角色情绪检测器 — keyword 匹配 + STT 事件 + mood 衰减。"""

    def __init__(
        self,
        *,
        state: CharacterState,
        output: OutputManager,
        idle_expressions: list[str],
    ):
        """初始化情绪检测器。

        Args:
            state: 角色状态对象。
            output: 输出管理器，用于推送表情。
            idle_expressions: 空闲表情列表，随机选取推送。
        """
        self._state = state
        self._output = output
        self._idle_expressions = idle_expressions

    # ---- 公共方法 ----

    def analyze_user(self, text: str) -> None:
        """关键词 mood 检测。匹配到非 neutral mood 时更新 state 并推送表情。

        Args:
            text: 用户消息文本。
        """
        detected = self._detect_mood(text)
        if detected == "neutral":
            return

        current = self._state.mood
        intensity = self._state.mood_intensity

        if detected != current:
            self._state.mood = detected
            self._state.mood_intensity = min(1.0, intensity + 0.1)
            self._push_expression(detected, intensity)
            logger.debug("心情 (关键词): %s → %s", current, detected)
        else:
            self._state.mood_intensity = min(1.0, intensity + 0.05)

    def analyze_assistant(self, _text: str) -> None:
        """每轮回复后衰减 mood intensity。mood 文本由 LLM 下轮 <mood> 更新。

        Args:
            _text: 助手回复文本（当前未使用，保留接口兼容）。
        """
        intensity = self._state.mood_intensity
        new_intensity = max(0.1, intensity - 0.15)
        self._state.mood_intensity = new_intensity
        if new_intensity < 0.2:
            self._push_idle()

    def analyze_user_stt(self, emotion_tag: str, events: list[str] | None = None) -> None:
        """处理 STT 音频事件，推送对应表情。mood 决策交给 LLM（通过 C5 Sensory）。

        Args:
            emotion_tag: 音频识别的情绪标签。
            events: 音频事件列表（如 Laughter、Cry、Applause）。
        """
        if events:
            for evt in events:
                if evt == "Laughter":
                    _schedule(self._output.expression("laugh", intensity=0.7))
                elif evt == "Cry":
                    _schedule(self._output.expression("cry", intensity=0.8))
                elif evt == "Applause":
                    _schedule(self._output.expression("surprised", intensity=0.5))

    async def push_idle_expression(self) -> None:
        """推送一个随机空闲表情（周期性或在进入 IDLE 时调用）。"""
        self._push_idle()

    # ---- 私有方法 ----

    def _detect_mood(self, text: str) -> str:
        """基于关键词的简单情绪检测。

        Args:
            text: 要检测的文本。

        Returns:
            最匹配的情绪标签，无匹配时返回 "neutral"。
        """
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

    def _push_expression(self, mood_text: str, intensity: float) -> None:
        """推送表情到输出通道。

        Args:
            mood_text: 情绪描述文本（纯字符串）。
            intensity: 情绪强度（0.0-1.0）。
        """
        _schedule(self._output.expression(mood_text, intensity=intensity))

    def _push_idle(self) -> None:
        """推送一个空闲表情。"""
        if self._idle_expressions:
            expr = random.choice(self._idle_expressions)
            _schedule(self._output.expression(expr, intensity=0.3))
