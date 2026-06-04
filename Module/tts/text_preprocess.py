import re


_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001F9FF"   # 杂项符号、表情符号等
    "\U0001F600-\U0001F64F"    # 表情符号
    "\U00002600-\U000027BF"    # 杂项符号 + 装饰符号
    "\U0001F900-\U0001F9FF"    # 补充符号
    "\U0001FA00-\U0001FA6F"    # 象棋符号
    "\U0001FA70-\U0001FAFF"    # 扩展符号-A
    "\U00002300-\U000023FF"    # 杂项技术符号
    "\U00002B50"               # 星号
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
