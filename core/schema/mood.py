"""Mood 枚举 + TTS 情绪映射 + STT 工具函数。"""
from enum import StrEnum


class Mood(StrEnum):
    NEUTRAL = "neutral"
    HAPPY = "happy"
    SAD = "sad"
    CURIOUS = "curious"
    EXCITED = "excited"
    WARM = "warm"
    CONCERNED = "concerned"
    CALMING = "calming"
    REASSURING = "reassuring"
    UNDERSTANDING = "understanding"


# ---- 从 emotion_engine.py 原样复制 ----

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


_MOOD_TO_TTS_EMOTION: dict[Mood, str] = {
    Mood.HAPPY: "happy", Mood.SAD: "sad",
    Mood.CURIOUS: "happy", Mood.EXCITED: "happy",
    Mood.WARM: "neutral", Mood.CONCERNED: "neutral",
    Mood.CALMING: "neutral", Mood.REASSURING: "neutral",
    Mood.UNDERSTANDING: "neutral", Mood.NEUTRAL: "neutral",
}
