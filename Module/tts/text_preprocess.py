import re


def strip_all_markup(text: str) -> str:
    text = re.sub(r"\[.*?\]", "", text)        # [动作描写]
    text = re.sub(r"\<\<.*?\>\>", "", text)    # <<心情描写>> —— 必须在单 <> 之前
    text = re.sub(r"<(.*?)>", "", text)         # <表情描写>
    text = re.sub(r"[\(\（].*?[\)\）]", "", text)  # (语气标记) 含全角括号
    return text.strip()


def strip_action_only(text: str) -> str:
    text = re.sub(r"\[.*?\]", "", text)         # [动作描写]
    text = re.sub(r"[\(\（].*?[\)\）]", "", text)  # (动作) 含全角括号
    # 保留 <语气词> <<心情>>
    return text.strip()
