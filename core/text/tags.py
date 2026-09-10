"""标签注册表 — 流式解析器的全部标签定义 + emoji 收口。

纯数据模块：仅依赖 stdlib（re）。供 tokenizer / splitter / coordinator
（Task 2-7）消费。每类标签 = 起始模式 + 结束模式 + kind + 路由，
未来加标签 = 加一行注册。

对齐设计文档 `docs/superpowers/specs/archive/2026-08/2026-08-01-stream-parser-refactor-design.md`
行为对照表：

    PAUSE        <#0.5#>                    TTS 停顿（原子 token，整体保护）
    STAGE_CN     （眨眼）/(眨眼)              中文舞台指示（半角+全角，整体保护）
    META         <mood>..</mood>            心情（任意位置剥离）
                 <inner_voice>..</inner_voice>  内心独白（可多个）
    MARKDOWN     <|markdown|>..</|markdown|>
    CODE         <|code|lang>..</|code|>   + ```lang 围栏（仅行首）
    STAGE_EN      (sighs)/(laughs)          英文语气词（圆括号，显示剥离、TTS 保留）
    ANGLE_EN      <pant>                    英文语气词尖括号（违规格式，全链路丢弃）
    STAGE_EN_WORDS  英文语气词词表
"""

from __future__ import annotations

import re

# ---- Emoji 检测 -----------------------------------------------------------
# 标签定义唯一来源，消费者统一从本模块导入。

_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001F9FF"
    r"\U0001F600-\U0001F64F"
    r"\U00002600-\U000027BF"
    r"\U00002300-\U000023FF]+",
    flags=re.UNICODE,
)


def is_emoji_only(text: str) -> bool:
    """句子去掉 emoji 后是否只剩空白 — 用于跳过 TTS 合成。

    Args:
        text: 待检测的句子。

    Returns:
        bool: 纯 emoji 句（或空串）时为 True。
    """
    return not _EMOJI_RE.sub("", text).strip()


# ---- PAUSE（TTS 停顿标签）--------------------------------------------------

# 完整标签 <#0.5#>（流式场景需在管道最前端剥离，避免内部 . 干扰分句）
PAUSE_RE = re.compile(r"<#\d+\.?\d*#>")

# 尾部残缺标签持有判定 —— 必须允许尾随 #（`<#0.5#` 前缀态），
# 否则 `<#0.5#|>` 切分点立即释放为 SPEECH 泄漏。
PAUSE_TAIL_RE = re.compile(r"<#[\d.]*#?$")


# ---- STAGE_CN（中文舞台指示）-----------------------------------------------

# 开/闭括号字符集（半角 + 全角）
STAGE_CN_OPEN: frozenset[str] = frozenset({"（", "("})
STAGE_CN_CLOSE: frozenset[str] = frozenset({"）", ")"})
# 完整标签正则：括号内至少一个 CJK 字符（镜像现状 _CN_ACTION_RE，半角+全角）
STAGE_CN_RE = re.compile(r"[\(\（][^\)\）]*[一-鿿][^\)\）]*[\)\）]")
# 持有窗口：M=内容上界（含 CJK 的未闭口括号持有到 M 才回落）、
# K=英文早退窗（无 CJK 短括号不必等到 M 即回落）。
STAGE_M: int = 64
STAGE_K: int = 16

# 英文语气词表（从旧死代码 _TONE_TAG_RE 提取）——STAGE_EN / ANGLE_EN 识别基准。
# 圆括号格式 (sighs) → STAGE_EN：显示剥离、TTS 保留（语气词用途）；
# 尖括号格式 <pant> → ANGLE_EN：F3 明令禁止的违规格式，显示 + TTS 全丢弃。
# 词内空白用 \s+ 归一，兼容 (clears  throat) 等多空格变体。
STAGE_EN_WORDS: frozenset[str] = frozenset({
    "laughs", "sighs", "crying", "gasps", "breath", "humming", "sniffs",
    "sneezes", "coughs", "groans", "pant", "chuckle", "clears throat",
    "clear throat", "applause", "inhale", "exhale", "snorts", "burps",
    "lip smacking", "lipsmacking", "hissing", "emm", "whistles",
})


def is_tone_word(content: str) -> bool:
    """内容归一（去空白 + 小写）后是否为英文语气词。

    Args:
        content: 括号内原始内容（可含前后空白 / 大小写 / 词内多空格）。

    Returns:
        bool: 归一后命中 STAGE_EN_WORDS 时为 True。
    """
    return " ".join(content.strip().lower().split()) in STAGE_EN_WORDS


def _escape_tone_word(word: str) -> str:
    """语气词 → 正则片段：词内空白转 \\s+（兼容多空格），其余转义。"""
    return r"\s+".join(re.escape(part) for part in word.split())


# 圆括号英文语气词正则（半角 + 全角括号）。STAGE_CN（中文指示）不在剥离范围。
# 按长度降序排列备选，避免前缀短词抢先匹配（如 clear 先于 clears throat）。
_TONE_WORD_RE = re.compile(
    r"[\(\（]\s*(?:{})\s*[\)\）]".format(
        "|".join(
            _escape_tone_word(word)
            for word in sorted(STAGE_EN_WORDS, key=len, reverse=True)
        )
    )
)


def strip_display_hidden(text: str) -> str:
    """从句子文本剥离显示隐藏标记：PAUSE + 圆括号英文语气词，STAGE_CN 保留。

    供持久化 parts 与 tts_adapter 显示路径共用（单一来源，避免正则散落）。
    尖括号 ANGLE_EN 已被 tokenizer 丢弃，不会出现在句子文本中。
    """
    return _TONE_WORD_RE.sub("", PAUSE_RE.sub("", text))


# ---- META（心情 / 内心独白）-------------------------------------------------

# kind → (open, close, emit)——emit 为该标签闭合时发的事件类型 value
# （与 StreamEventType.value 对齐；tags 保持纯数据层，不依赖 stream_processor 枚举）。
# 闭合时发对应事件（任意位置）。未来加 `<` 前缀新标签 = 加一行 + 枚举加值（若新类型）。
META_TAGS: dict[str, tuple[str, str, str]] = {
    "mood": ("<mood>", "</mood>", "mood"),
    "inner_voice": ("<inner_voice>", "</inner_voice>", "inner_voice"),
}


# ---- MARKDOWN / CODE（结构化块）---------------------------------------------

MARKDOWN_OPEN = "<|markdown|>"
MARKDOWN_CLOSE = "</|markdown|>"
# lang 受限字符集 [A-Za-z0-9_+.-]*（保持既有语义）
CODE_OPEN_RE = re.compile(r"<\|code\|([A-Za-z0-9_+.-]*)>")
CODE_CLOSE = "</|code|>"

# 代码围栏（仅行首识别，允许 ≤3 空格缩进——对齐 CommonMark）
FENCE_MARK = "```"
FENCE_MAX_INDENT = 3


# ---- 起始模式前缀（注册表文档化）--------------------------------------------

# 最小字面前缀全集（供阅读/文档化）：tokenizer 实际持有判定用更精确的完整
# 起始模式集合（_OPEN_PATTERNS，见 tokenizer.py），本表为标签注册的摘要视图。
# 覆盖：PAUSE(<#) / META(<m, <i) / 结构化块(<|) / STAGE_CN(（() / 围栏(```)。
START_PREFIXES: tuple[str, ...] = (
    "<#",   # PAUSE
    "<m",   # <mood>
    "<i",   # <inner_voice>
    "<|",   # <|markdown|> / <|code|...>
    "（",   # STAGE_CN 全角开
    "(",    # STAGE_CN 半角开
    "```",  # 代码围栏（仅行首）
)
