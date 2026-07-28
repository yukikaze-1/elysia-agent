"""STT 情绪 emoji 工具函数。"""

_STT_EMOTION_EMOJI: dict[str, str] = {
    "HAPPY": "\U0001f60a", "SAD": "\U0001f614", "ANGRY": "\U0001f621",
    "NEUTRAL": "", "FEARFUL": "\U0001f630", "DISGUSTED": "\U0001f922",
    "SURPRISED": "\U0001f62e",
}

def stt_emotion_emoji(tag: str) -> str:
    """根据 STT 情绪标签返回对应的 emoji。

    Args:
        tag: STT 情绪标签（如 HAPPY、SAD、ANGRY 等）。

    Returns:
        对应的 emoji 字符串，未匹配到则返回空字符串。
    """
    return _STT_EMOTION_EMOJI.get(tag, "")


_STT_EVENT_EMOJI: dict[str, str] = {
    "Speech": "", "BGM": "\U0001f3bc", "Applause": "\U0001f44f",
    "Laughter": "\U0001f600", "Cry": "\U0001f62d",
    "Cough": "\U0001f637", "Sneeze": "\U0001f927",
    "Breath": "", "Sing": "", "Speech_Noise": "", "GBG": "",
    "Event_UNK": "",
}

def stt_event_emoji(event: str) -> str:
    """根据 STT 音频事件标签返回对应的 emoji。

    Args:
        event: STT 音频事件标签（如 Speech、BGM、Laughter 等）。

    Returns:
        对应的 emoji 字符串，未匹配到则返回空字符串。
    """
    return _STT_EVENT_EMOJI.get(event, "")
