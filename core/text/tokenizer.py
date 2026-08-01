"""StreamTokenizer — 结构感知流式切分器。

将增量 LLM 输出切分为完整、带类型的 token。**核心不变式**：
    对任意输入串 S，在任意字符边界切分为 delta 序列，
    feed 拆分的 token 序列 == 一次 feed 的序列（由 property test 强制）。

设计（对齐 `docs/superpowers/specs/2026-08-01-stream-parser-refactor-design.md`）：

- 单状态机逐字符处理。NORMAL 态扫描起始模式；进入标签态后按内容规则扫描。
- **持有上界**：
  - 标签起始前缀：前缀匹配持有——对完整起始串（含 `<mo`/`<moo` 等
    `<mood>` 的扩展前缀，即 tags.START_PREFIXES 最小字面前缀的完整展开）
    匹配；一旦不可能构成任何起始模式 → 释放为 SPEECH。
  - PAUSE：尾串正则 `PAUSE_TAIL_RE` 驱动（内容无上界，固定前缀框不住）。
  - STAGE_CN：K/M 计数窗口——K=16 英文早退（无 CJK 提前回落）、
    M=64 CJK 上限（含 CJK 的未闭口持有到 M 才回落）。
  - 围栏：`\n`/空格立即作为 SPEECH 发出（零延迟，不参与持有）；
    行首反引号前缀（1-2 个）跨 chunk 持有等待第 3 个。
- **行首双状态**：布尔 `at_line_start` + 缩进计数；围栏判定只看 `at_line_start`。
- **SPEECH 确定性边界落盘**：待判定文本在标签事件 / 换行 / 空格 / flush
  边界输出，保证跨 chunk 不变式（单一 SPEECH 粒度由边界位置唯一决定）。

未闭合处理（flush）：半截 PAUSE 剥离、未闭合 mood 丢弃（不发事件）、
未闭合 inner_voice 产出残留 META token（由协调器广播）、未闭合
CODE/MARKDOWN 整体丢弃、未闭合 STAGE_CN 回落 SPEECH。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from core.text.tags import (
    CODE_CLOSE,
    CODE_OPEN_RE,
    FENCE_MAX_INDENT,
    MARKDOWN_CLOSE,
    MARKDOWN_OPEN,
    META_TAGS,
    PAUSE_RE,
    PAUSE_TAIL_RE,
    STAGE_CN_CLOSE,
    STAGE_CN_OPEN,
    STAGE_K,
    STAGE_M,
)


# ---- Token 模型 ----------------------------------------------------------

class TokenKind(StrEnum):
    SPEECH = "speech"
    PAUSE = "pause"
    STAGE_CN = "stage_cn"
    CODE = "code"
    MARKDOWN = "markdown"
    META = "meta"


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    text: str
    meta_kind: str = ""
    lang: str = ""


# ---- 内部辅助 -------------------------------------------------------------

_CJK_RE = re.compile(r"[一-鿿]")
_WS = frozenset(" \t")

# 起始模式的完整串（用于前缀匹配持有）。由 tags.py 数据驱动：
# `<#`（PAUSE）+ META_TAGS 的 open + `<|markdown|>` + `<|code|`。
# 相比 tags.START_PREFIXES 的最小字面前缀（<m / <i），这里用完整串，
# 使 `<mo` / `<moo` / `<|code|py` 等扩展前缀也能正确持有（Task 1 移交项 1）。
_OPEN_PATTERNS: tuple[str, ...] = ("<#",) + tuple(
    open_ for open_, _, _ in META_TAGS.values()
) + (MARKDOWN_OPEN, "<|code|")


def _has_cjk(text: str) -> bool:
    return _CJK_RE.search(text) is not None


def _is_open_prefix(text: str) -> bool:
    """text 是否为某个完整起始模式的严格前缀（含 `<|code|`+lang 变长部分）。

    例：`<`、`<m`、`<moo`、`<|`、`<|code|py` → True（可持有）；
        `<1`、`<moox`、`<|code|!` → False（不可能构成任何起始模式）。
    """
    for pat in _OPEN_PATTERNS:
        if text != pat and pat.startswith(text):
            return True
    if text.startswith("<|code|"):
        rest = text[len("<|code|"):]
        return re.fullmatch(r"[A-Za-z0-9_+.-]*", rest) is not None
    return False


def _complete_open(text: str) -> tuple[str, str | None] | None:
    """text 是否为完整起始模式。返回 (状态, 参数) 或 None。

    状态名与 `feed()` 派发用大写一致（PAUSE / META / MARKDOWN / CODE_TAG）。
    """
    if text == "<#":
        return ("PAUSE", None)
    for kind, (open_, _, _) in META_TAGS.items():
        if text == open_:
            return ("META", kind)
    if text == MARKDOWN_OPEN:
        return ("MARKDOWN", None)
    m = CODE_OPEN_RE.fullmatch(text)
    if m:
        return ("CODE_TAG", m.group(1))
    return None


class StreamTokenizer:
    """结构感知流式切分器（跨 chunk 不变式）。"""

    def __init__(self) -> None:
        self.reset()

    # ---- 公共接口 ----------------------------------------------------------

    def feed(self, delta: str) -> list[Token]:
        """输入增量文本，返回本次能确定的完整 token 列表。"""
        if not delta:
            return []
        out: list[Token] = []
        for c in delta:
            if self._state == "NORMAL":
                self._feed_normal(c, out)
            elif self._state == "PAUSE":
                self._feed_pause(c, out)
            elif self._state == "STAGE":
                self._feed_stage(c, out)
            elif self._state == "META":
                self._feed_meta(c, out)
            elif self._state == "CODE_TAG":
                self._feed_code_tag(c, out)
            elif self._state == "MARKDOWN":
                self._feed_markdown(c, out)
            else:  # FENCE
                self._feed_fence(c, out)
        return out

    def flush(self) -> list[Token]:
        """流结束：处理残留（未闭合块丢弃 / 回落），随后复位状态。"""
        out: list[Token] = []
        if self._state == "NORMAL":
            self._flush_pending(out)
            if self._held:
                # 区分"持有但未进入任何标签内容态" vs "已进入标签内容态"：
                # - 持有但未进入标签内容态（字面 <、半截 <mo、<|、1-2 反引号）
                #   → 按 spec「未知模式 → 回落 SPEECH」释放为普通文本；
                # - 行首 3+ 反引号 = 围栏标记已开启未闭合 → 丢弃（未闭合 CODE）。
                # 已进入标签内容态的（PAUSE/STAGE/META/FENCE/CODE_TAG/MARKDOWN
                # 内容缓冲）在下方各自分支按 spec 剥离/丢弃，不受影响。
                if not (self._held_are_backticks() and len(self._held) >= 3):
                    self._emit_speech_text(self._held, out)
                self._held = ""
        elif self._state == "PAUSE":
            pass  # 半截 PAUSE → 剥离（不输出）
        elif self._state == "STAGE":
            # 未闭合 STAGE_CN → 回落 SPEECH（不完整括号按文本显示）
            self._release_stage_speech(self._stage_open + self._stage_content, out)
        elif self._state == "META":
            if self._meta_kind == "inner_voice":
                # 未闭合 inner_voice → 产出残留 token，由协调器广播 INNER_VOICE
                out.append(Token(TokenKind.META, self._meta_content,
                                 meta_kind="inner_voice"))
            # 未闭合 mood → 丢弃，不发事件
        # CODE_TAG / MARKDOWN / FENCE 未闭合 → 整体丢弃（含 FENCE 中
        # "行首反引号已凑足 ≥ 开围栏长度"的收尾行：EOF 终止该行即确认闭合，
        # 见 _feed_fence_body 与下方 FENCE flush 分支）。
        elif self._state == "FENCE":
            if self._fence_close_state in ("run", "strong"):
                if self._fence_run_len >= self._fence_len:
                    # EOF 终止收尾围栏行 → 确认闭合
                    out.append(Token(TokenKind.CODE, self._fence_content,
                                     lang=self._fence_lang()))
        self.reset()
        return out

    def reset(self) -> None:
        """清空全部内部状态（每轮对话开始时调用）。"""
        self._state = "NORMAL"
        self._pending = ""
        self._held = ""
        self._at_line_start = True
        self._indent = 0
        self._pause_buf = ""
        self._stage_open = ""
        self._stage_content = ""
        self._stage_has_cjk = False
        self._meta_kind = ""
        self._meta_content = ""
        self._code_lang = ""
        self._code_content = ""
        self._markdown_content = ""
        self._fence_len = 0
        self._fence_infostr = ""
        self._fence_content = ""
        self._fence_pending = ""
        self._fence_run_len = 0
        self._fence_close_state: str | None = None
        self._fence_phase = "info"

    # ---- NORMAL 态 ----------------------------------------------------------

    def _feed_normal(self, c: str, out: list[Token]) -> None:
        if self._held:
            self._feed_normal_held(c, out)
            return
        if c == "\n":
            self._flush_pending(out)
            out.append(Token(TokenKind.SPEECH, "\n"))
            self._at_line_start = True
            self._indent = 0
            return
        if c == " ":
            self._flush_pending(out)
            out.append(Token(TokenKind.SPEECH, " "))
            if self._at_line_start:
                self._indent += 1
                if self._indent > FENCE_MAX_INDENT:
                    self._at_line_start = False
            return
        if self._at_line_start and c == "`":
            self._held = "`"
            return
        if c == "`":
            self._pending += c
            return
        if c in STAGE_CN_OPEN:
            self._flush_pending(out)
            self._enter_stage(c)
            return
        if c == "<":
            self._flush_pending(out)
            self._held = "<"
            return
        if self._at_line_start:
            self._at_line_start = False
        self._pending += c

    def _feed_normal_held(self, c: str, out: list[Token]) -> None:
        """持有中的前缀收到新字符：继续持有 / 补足成完整起始 / 断裂释放。"""
        if self._held_are_backticks():
            if c == "`":
                self._held += c
                return
            run_len = len(self._held)
            if run_len >= 3:
                self._enter_fence(run_len)
                self._feed_fence(c, out)
                return
            # 不足 3 个反引号 → 非围栏，按普通文本释放后重新处理 c
            self._emit_speech_text(self._held, out)
            self._held = ""
            self._feed_normal(c, out)
            return
        candidate = self._held + c
        # 顺序依赖：_is_open_prefix 返回 False 不意味着"确定不是标签"——
        # 候选可能正好是完整起始模式（如 `<#` / `<mood>` / `<|code|python>`），
        # 此时它既非严格前缀也非其它标签的前缀，须由 _complete_open
        # （fullmatch 判定）接管进入标签态。prefix? → no → complete? 为唯一
        # 判定路径，二者缺一不可。
        if _is_open_prefix(candidate):
            self._held = candidate
            return
        complete = _complete_open(candidate)
        if complete:
            self._held = ""
            state, param = complete
            self._enter_tag(state, param)
            return
        # 断裂：c 为 \n/空格时须走行首状态更新，故拆开发射
        if c in ("\n", " "):
            self._emit_speech_text(self._held, out)
            self._held = ""
            self._feed_normal(c, out)
            return
        self._emit_speech_text(candidate, out)
        self._held = ""

    def _held_are_backticks(self) -> bool:
        return bool(self._held) and set(self._held) == {"`"}

    # ---- 标签态入口 / 出口 --------------------------------------------------

    def _enter_stage(self, open_ch: str) -> None:
        self._state = "STAGE"
        self._stage_open = open_ch
        self._stage_content = ""
        self._stage_has_cjk = False
        self._at_line_start = False

    def _exit_stage(self) -> None:
        self._state = "NORMAL"
        self._stage_open = ""
        self._stage_content = ""
        self._stage_has_cjk = False

    def _enter_tag(self, state: str, param: str | None) -> None:
        self._at_line_start = False
        self._state = state
        if state == "META":
            self._meta_kind = param or ""
            self._meta_content = ""
        elif state == "PAUSE":
            self._pause_buf = "<#"
        elif state == "CODE_TAG":
            self._code_lang = param or ""
            self._code_content = ""
        elif state == "MARKDOWN":
            self._markdown_content = ""

    def _exit_pause(self) -> None:
        self._state = "NORMAL"
        self._pause_buf = ""

    def _exit_meta(self) -> None:
        self._state = "NORMAL"
        self._meta_kind = ""
        self._meta_content = ""

    def _exit_code_tag(self) -> None:
        self._state = "NORMAL"
        self._code_lang = ""
        self._code_content = ""

    def _exit_markdown(self) -> None:
        self._state = "NORMAL"
        self._markdown_content = ""

    def _enter_fence(self, run_len: int) -> None:
        self._state = "FENCE"
        self._fence_len = run_len
        self._fence_infostr = ""
        self._fence_content = ""
        self._fence_pending = ""
        self._fence_run_len = 0
        self._fence_close_state = None
        self._fence_phase = "info"
        self._at_line_start = False
        # 关键：清空持有前缀——围栏经行首反引号路径打开时 _held 残留 ```，
        # 若不在此清空，围栏关闭回 NORMAL 后下一个行首字符会触发 run_len>=3
        # 的幽灵 FENCE，把后续所有文本吞进未闭合围栏并在 flush 时丢弃
        self._held = ""

    # ---- PAUSE 态 ------------------------------------------------------------

    def _feed_pause(self, c: str, out: list[Token]) -> None:
        candidate = self._pause_buf + c
        if PAUSE_RE.fullmatch(candidate):
            out.append(Token(TokenKind.PAUSE, candidate))
            self._exit_pause()
            return
        if PAUSE_TAIL_RE.search(candidate):
            self._pause_buf = candidate
            return
        # 非数字/点字符 → 释放为 SPEECH（<#100元 → SPEECH）
        if c in ("\n", " "):
            self._emit_speech_text(self._pause_buf, out)
            self._exit_pause()
            self._feed_normal(c, out)
            return
        self._emit_speech_text(candidate, out)
        self._exit_pause()

    # ---- STAGE_CN 态 ---------------------------------------------------------

    def _feed_stage(self, c: str, out: list[Token]) -> None:
        if c in STAGE_CN_CLOSE:
            full = self._stage_open + self._stage_content + c
            if self._stage_has_cjk:
                out.append(Token(TokenKind.STAGE_CN, full))
            else:
                self._release_stage_speech(full, out)
            self._exit_stage()
            return
        self._stage_content += c
        if _has_cjk(c):
            self._stage_has_cjk = True
        n = len(self._stage_content)
        if n >= STAGE_M:
            # CJK 上限：未闭口一律回落（不延迟）
            self._release_stage_speech(self._stage_open + self._stage_content, out)
            self._exit_stage()
            return
        if n >= STAGE_K and not self._stage_has_cjk:
            # 英文早退窗：无 CJK 短括号不必等到 M
            self._release_stage_speech(self._stage_open + self._stage_content, out)
            self._exit_stage()

    # ---- META / CODE_TAG / MARKDOWN 态（内容缓冲至闭合）---------------------

    def _feed_meta(self, c: str, out: list[Token]) -> None:
        self._meta_content += c
        close = META_TAGS[self._meta_kind][1]
        if self._meta_content.endswith(close):
            content = self._meta_content[:-len(close)]
            out.append(Token(TokenKind.META, content, meta_kind=self._meta_kind))
            self._exit_meta()

    def _feed_code_tag(self, c: str, out: list[Token]) -> None:
        self._code_content += c
        if self._code_content.endswith(CODE_CLOSE):
            content = self._code_content[:-len(CODE_CLOSE)]
            out.append(Token(TokenKind.CODE, content, lang=self._code_lang))
            self._exit_code_tag()

    def _feed_markdown(self, c: str, out: list[Token]) -> None:
        self._markdown_content += c
        if self._markdown_content.endswith(MARKDOWN_CLOSE):
            content = self._markdown_content[:-len(MARKDOWN_CLOSE)]
            out.append(Token(TokenKind.MARKDOWN, content))
            self._exit_markdown()

    # ---- 围栏（FENCE）态 -----------------------------------------------------

    def _feed_fence(self, c: str, out: list[Token]) -> None:
        if self._fence_phase == "info":
            # 起始标记后的信息串（lang = 第一个空白分隔 token）
            if c == "\n":
                self._fence_phase = "body"
                self._fence_close_state = "indent"
                self._fence_pending = ""
            else:
                self._fence_infostr += c
            return
        self._feed_fence_body(c, out)

    def _feed_fence_body(self, c: str, out: list[Token]) -> None:
        st = self._fence_close_state
        if st == "strong":
            # 已看到 ≥ 开围栏长度的反引号行，等待行尾判定是否为收尾
            if c in _WS:
                self._fence_pending += c
                return
            if c == "\n":
                lang = self._fence_lang()
                content = self._fence_content
                out.append(Token(TokenKind.CODE, content, lang=lang))
                self._state = "NORMAL"
                self._at_line_start = True
                self._clear_fence()
                return
            # 反引号后非空白 → 不是收尾围栏，整行算内容
            self._fence_content += self._fence_pending + c
            self._fence_pending = ""
            self._fence_close_state = None
            return
        if st == "run":
            if c == "`":
                self._fence_pending += c
                self._fence_run_len += 1
                return
            if self._fence_run_len >= self._fence_len:
                self._fence_close_state = "strong"
                self._feed_fence_body(c, out)
                return
            self._fence_content += self._fence_pending + c
            self._fence_pending = ""
            self._fence_close_state = None
            return
        if st == "indent":
            if c == " ":
                self._fence_pending += c
                if len(self._fence_pending) > FENCE_MAX_INDENT:
                    # 缩进超 3 空格 → 非收尾围栏
                    self._fence_content += self._fence_pending
                    self._fence_pending = ""
                    self._fence_close_state = None
                return
            if c == "`":
                self._fence_pending += c
                self._fence_run_len = 1
                self._fence_close_state = "run"
                return
            if c == "\n":
                self._fence_content += self._fence_pending + "\n"
                self._fence_pending = ""
                return
            self._fence_content += self._fence_pending + c
            self._fence_pending = ""
            self._fence_close_state = None
            return
        # 普通内容（非行首）
        if c == "\n":
            self._fence_content += "\n"
            self._fence_close_state = "indent"
            self._fence_pending = ""
            return
        self._fence_content += c

    def _fence_lang(self) -> str:
        s = self._fence_infostr.strip()
        if not s:
            return ""
        return s.split()[0]

    def _clear_fence(self) -> None:
        self._fence_len = 0
        self._fence_infostr = ""
        self._fence_content = ""
        self._fence_pending = ""
        self._fence_run_len = 0
        self._fence_close_state = None
        self._fence_phase = "info"

    # ---- 输出辅助 --------------------------------------------------------------

    def _flush_pending(self, out: list[Token]) -> None:
        """落盘 NORMAL 态累积的普通文本（pending 只含非空白内容字符）。"""
        if self._pending:
            out.append(Token(TokenKind.SPEECH, self._pending))
            self._pending = ""
            self._at_line_start = False

    def _emit_speech_text(self, text: str, out: list[Token]) -> None:
        """发射非空白 SPEECH 文本（标签前缀 / 残缺标签 / 反引号）。"""
        if text:
            out.append(Token(TokenKind.SPEECH, text))
            self._at_line_start = False

    def _release_stage_speech(self, full: str, out: list[Token]) -> None:
        """STAGE_CN 回落 SPEECH：内容可含 \n，末尾行态需按末字符重建。"""
        if full:
            out.append(Token(TokenKind.SPEECH, full))
        if full.endswith("\n"):
            self._at_line_start = True
            self._indent = 0
        else:
            self._at_line_start = False
