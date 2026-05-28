import re


_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001F9FF"   # Miscellaneous Symbols, Emoticons, etc.
    "\U0001F600-\U0001F64F"    # Emoticons
    "\U00002600-\U000027BF"    # Misc Symbols + Dingbats (✨ ♥ ★ etc.)
    "\U0001F900-\U0001F9FF"    # Supplemental Symbols
    "\U0001FA00-\U0001FA6F"    # Chess Symbols
    "\U0001FA70-\U0001FAFF"    # Symbols Extended-A
    "\U00002300-\U000023FF"    # Misc Technical
    "\U00002B50"               # ⭐
    "]+", flags=re.UNICODE,
)


def strip_all_markup(text: str) -> str:
    text = re.sub(r"\[.*?\]", "", text)        # [动作描写]
    text = re.sub(r"\<\<.*?\>\>", "", text)    # <<心情描写>> —— 必须在单 <> 之前
    text = re.sub(r"<(.*?)>", "", text)         # <表情描写>
    text = re.sub(r"[\(\（].*?[\)\）]", "", text)  # (语气标记) 含全角括号
    text = _EMOJI_RE.sub("", text)              # emoji → TTS 无法合成
    return text.strip()


def strip_action_only(text: str) -> str:
    text = re.sub(r"\[.*?\]", "", text)         # [动作描写]
    text = re.sub(r"[\(\（].*?[\)\）]", "", text)  # (动作) 含全角括号
    text = _EMOJI_RE.sub("", text)              # emoji
    # 保留 <语气词> <<心情>>
    return text.strip()
