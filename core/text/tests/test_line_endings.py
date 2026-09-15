"""换行符归一（``\\r\\n`` / 裸 ``\\r``）回归测试 —— CODE_REVIEW_REPORT #46 缺口行 #11。

## 缺陷（修复前实测，本文件即其回归网）

输入 ``'好的。\\n```python\\nprint(1)\\n```\\n完毕。\\n'``：

    LF   → SENTENCE ['好的。', '完毕。\\n']   blocks [('code', 'print(1)\\n', 'python', 1)]
    CRLF → SENTENCE ['好的。\\r\\n']          blocks []
    CR   → SENTENCE ['好的。', '\\r```python\\rprint(1)\\r```\\r完毕。']   blocks []

两条失败路径（都静默，无日志、无异常）：

- **CRLF**：围栏收尾行 `` ```\\r\\n `` 里的 ``\\r`` 既不在 ``tokenizer._WS``（``" \\t"``）
  也不等于 ``\\n``，于是被当成**内容**吞掉（``tokenizer.py::_feed_fence_body`` 的
  ``if c in _WS`` / ``if c == "\\n"`` 两个判定都不命中）→ 围栏永不闭合 →
  ``flush()`` 走「未闭合 FENCE 整体丢弃」分支 → **代码块与块后全部文本一起消失**。
- **裸 CR**：``tokenizer.py::_feed_normal`` 只在 ``c == "\\n"`` 时把
  ``_at_line_start`` 置回 True，``\\r`` 既不重置行首也不被当作换行 →
  围栏开标记（行首 ``` 判定）永不触发 → **围栏正文被当可见文本播报**（反向失败）。

修法（唯一收口点）见 ``test_newline_normalization_contract`` 与
``StreamProcessor.feed`` 文档：在**管道唯一摄入点**归一，跨 chunk 安全。
"""

import pytest

from core.text.stream_processor import (
    StreamProcessor,
    StreamEvent,
    StreamEventType,
)
from core.text.tokenizer import FENCE_MAX_CONTENT

pytestmark = pytest.mark.unit


# ---- 辅助 -----------------------------------------------------------------

def _run(pieces: list[str], **kwargs) -> tuple[StreamProcessor, list[StreamEvent]]:
    """依次 feed 各 delta，再 flush；返回 (proc, 扁平事件序列)。"""
    proc = StreamProcessor(**kwargs)
    events: list[StreamEvent] = []
    for piece in pieces:
        events.extend(proc.feed(piece))
    events.extend(proc.flush())
    return proc, events


def _structure(pieces: list[str]) -> dict:
    """管道全部可观测产物 —— 逐项对齐的判据（非仅"非空"）。"""
    proc, events = _run(pieces)
    return {
        "sentences": [
            e.text for e in events if e.type is StreamEventType.SENTENCE
        ],
        "text_deltas": [
            e.text for e in events if e.type is StreamEventType.TEXT_DELTA
        ],
        "mood": [e.text for e in events if e.type is StreamEventType.MOOD],
        "inner_voice": [
            e.text for e in events if e.type is StreamEventType.INNER_VOICE
        ],
        "ttft_count": sum(1 for e in events if e.type is StreamEventType.TTFT),
        "blocks": proc.get_blocks(),
        "public_text": proc.rebuild_public_text(),
    }


def _lf_variants(lf: str) -> tuple[str, str]:
    """LF 文本 → (CRLF 文本, 裸 CR 文本)。"""
    return lf.replace("\n", "\r\n"), lf.replace("\n", "\r")


# ---- 用例表：覆盖纯文本 / 围栏块 / 内联块 / 混合标签 / 空行 -----------------

_CASES = [
    pytest.param(
        "plain-text",
        "第一句。\n第二句。\n",
        False,
        id="plain-text",
    ),
    pytest.param(
        "fenced-code",
        "好的。\n```python\nprint(1)\n```\n完毕。\n",
        True,
        id="fenced-code",
    ),
    pytest.param(
        "inline-code-tag",
        "看这个<|code|python>x = 1</|code|>示例。\n还有一句。\n",
        True,
        id="inline-code-tag",
    ),
    pytest.param(
        "mixed-tags-fence",
        "（眨眼）你好。<#0.5#>\n```py\ny = 2\n```\n<mood>平静</mood>再见。\n",
        True,
        id="mixed-tags-fence",
    ),
    pytest.param(
        "blank-lines",
        "甲。\n\n\n乙。\n",
        False,
        id="blank-lines",
    ),
]


def _assert_baseline(struct: dict, expect_block: bool) -> None:
    """基线（LF）非平凡校验 —— 防"两边都空所以相等"的空洞参数化。"""
    assert struct["sentences"], "LF 基线必须真的产出句子"
    assert struct["public_text"], "LF 基线必须真的产出 public_text"
    if expect_block:
        assert struct["blocks"], "LF 基线必须真的产出块（否则等值是空洞的）"
    else:
        assert struct["blocks"] == []
    for text in struct["sentences"] + struct["text_deltas"]:
        assert "\r" not in text


# ---- Group 1: 三种换行符的管道产物必须逐项相同 -----------------------------

class TestLineEndingEquivalence:
    @pytest.mark.parametrize("case_id, lf, expect_block", _CASES)
    def test_crlf_structure_identical_to_lf(self, case_id, lf, expect_block):
        """CRLF 输入的 SENTENCE / blocks / public_text == LF 输入（判别性断言）。"""
        crlf, _cr = _lf_variants(lf)
        assert crlf != lf and "\r\n" in crlf      # 防空洞：参数确实含 CRLF
        baseline = _structure([lf])
        _assert_baseline(baseline, expect_block)
        assert _structure([crlf]) == baseline

    @pytest.mark.parametrize("case_id, lf, expect_block", _CASES)
    def test_bare_cr_structure_identical_to_lf(self, case_id, lf, expect_block):
        """裸 CR 输入（经典 Mac 换行）的管道产物 == LF 输入。

        修复前裸 CR **连围栏开标记都识别不出**，围栏正文被当可见文本播报。
        """
        _crlf, cr = _lf_variants(lf)
        assert cr != lf and "\r" in cr and "\n" not in cr
        baseline = _structure([lf])
        _assert_baseline(baseline, expect_block)
        assert _structure([cr]) == baseline

    @pytest.mark.parametrize("case_id, lf, expect_block", _CASES)
    def test_no_cr_leaks_into_output(self, case_id, lf, expect_block):
        """归一后 ``\\r`` 不得出现在任何下游产物（SENTENCE/TEXT_DELTA/public_text）。"""
        crlf, cr = _lf_variants(lf)
        for variant in (crlf, cr):
            struct = _structure([variant])
            assert "\r" not in struct["public_text"]
            for text in struct["sentences"] + struct["text_deltas"]:
                assert "\r" not in text

    def test_mixed_line_endings_in_one_text(self):
        """同一段文本混用 ``\\r\\n`` / ``\\n`` / 裸 ``\\r`` → 等价于全 LF。"""
        mixed = "甲。\r\n乙。\n丙。\r丁。"
        assert _structure([mixed]) == _structure(["甲。\n乙。\n丙。\n丁。"])

    def test_case_table_is_not_vacuous(self):
        """用例表自检：每个 case 的 LF 文本都真的含换行，两种变体都真的不同。"""
        for param in _CASES:
            lf = param.values[1]
            crlf, cr = _lf_variants(lf)
            assert "\n" in lf
            assert crlf != lf and cr != lf and crlf != cr


# ---- Group 2: 跨 chunk（流式安全）-----------------------------------------

class TestCrossChunkNormalization:
    @pytest.mark.parametrize("case_id, lf, expect_block", _CASES)
    def test_crlf_any_two_way_split_identical(self, case_id, lf, expect_block):
        """任意字符边界切成两次 feed → 产物与一次 feed 相同。

        含关键切点：``"...\\r"`` + ``"\\n..."``（CRLF 被 chunk 边界劈开）。
        朴素的每 chunk ``replace("\\r\\n", "\\n")`` 会在这里产出**两个**换行。
        """
        crlf, _cr = _lf_variants(lf)
        baseline = _structure([lf])
        _assert_baseline(baseline, expect_block)
        for index in range(len(crlf) + 1):
            got = _structure([crlf[:index], crlf[index:]])
            assert got == baseline, f"CRLF 在 index={index} 切分后产物不同"

    @pytest.mark.parametrize("case_id, lf, expect_block", _CASES)
    def test_crlf_char_by_char_identical(self, case_id, lf, expect_block):
        """逐字符 feed（最极端的跨 chunk 形态）→ 产物与一次 feed 相同。"""
        crlf, _cr = _lf_variants(lf)
        baseline = _structure([lf])
        _assert_baseline(baseline, expect_block)
        assert _structure(list(crlf)) == baseline

    def test_crlf_split_across_two_feeds_is_exactly_one_newline(self):
        """``"...\\r"`` + ``"\\n..."`` → 恰好一个 ``\\n``（不是 0 个也不是 2 个）。"""
        _proc, events = _run(["甲\r", "\n乙"])
        text = "".join(
            e.text for e in events if e.type is StreamEventType.TEXT_DELTA
        )
        assert text == "甲\n乙"
        assert text.count("\n") == 1

    def test_bare_cr_then_text_is_one_newline(self):
        """裸 ``\\r``（后一个 chunk 不以 ``\\n`` 开头）同样归一为一个 ``\\n``。"""
        _proc, events = _run(["甲\r", "乙"])
        text = "".join(
            e.text for e in events if e.type is StreamEventType.TEXT_DELTA
        )
        assert text == "甲\n乙"

    def test_crlf_split_across_three_feeds(self):
        """三段切分：``"好的。\\r"`` + ``"\\n```python\\r"`` + ``"\\nprint(1)\\r\\n```\\r\\n完毕。\\r\\n"``。"""
        lf = "好的。\n```python\nprint(1)\n```\n完毕。\n"
        pieces = ["好的。\r", "\n```python\r", "\nprint(1)\r\n```\r\n完毕。\r\n"]
        assert "".join(pieces) == lf.replace("\n", "\r\n")
        baseline = _structure([lf])
        assert baseline["blocks"], "基线必须真的产出块"
        assert _structure(pieces) == baseline

    def test_empty_chunk_does_not_break_held_cr(self):
        """两段之间插入空 chunk（``feed("")``）不得让被扣下的 ``\\r`` 丢失。"""
        baseline = _structure(["甲\n乙"])
        assert _structure(["甲\r", "", "\n乙"]) == baseline


# ---- Group 3: 边界值（行尾 / flush）----------------------------------------

class TestFlushBoundary:
    def test_trailing_cr_before_flush_is_line_ending(self):
        """流末尾恰好是 ``\\r``（CRLF 被流截断在半个换行）→ 等价于末尾 ``\\n``。"""
        base = "好的。\n```python\nprint(1)\n```\n完毕"
        baseline = _structure([base + "\n"])
        assert baseline["blocks"], "基线必须真的产出块"
        assert _structure([base + "\r"]) == baseline

    def test_lone_cr_input_normalizes_to_newline(self):
        """整条流只有一个 ``\\r`` → 与 ``\\n`` 等价（token 层可见 SPEECH ``\\n``）。"""
        proc, events = _run(["\r"])
        assert [e for e in events if e.type is StreamEventType.SENTENCE] == []
        assert proc.rebuild_public_text() == "\n"            # 修复前是 "\r"
        assert _structure(["\r"]) == _structure(["\n"])

    def test_trailing_crlf_equals_trailing_lf(self):
        """末尾 ``\\r\\n`` == 末尾 ``\\n``（不产生多余空行）。"""
        assert _structure(["甲。\r\n"]) == _structure(["甲。\n"])

    def test_reset_clears_held_cr(self):
        """reset() 清除跨 chunk 的 ``\\r`` 持有态，不把旧行的换行补进下一轮。"""
        proc = StreamProcessor()
        proc.feed("甲\r")            # 扣下 \r 等待下一 chunk 定论
        proc.reset()
        events = proc.feed("乙") + proc.flush()
        text = "".join(
            e.text for e in events if e.type is StreamEventType.TEXT_DELTA
        )
        assert text == "乙"          # 不得变成 "\n乙"（旧 \r 被补发）


# ---- Group 4: 长输入（有界持有上界）----------------------------------------

class TestLongInputLineEndings:
    def test_long_fenced_crlf_equals_lf_within_hold_bound(self):
        """超 ``FENCE_MAX_CONTENT`` 的长围栏：CRLF == LF（含保头截断位置）。

        修复前 CRLF 版本连块都产不出（``blocks == []``），块后文本全被吞。
        """
        body = "x" * 70_000
        lf = "开头。\n```python\n" + body + "\n```\n结尾。\n"
        crlf = lf.replace("\n", "\r\n")

        baseline = _structure([lf])
        assert baseline["blocks"], "LF 基线必须真的产出块"
        # 已实测：内容缓冲保头截断在 FENCE_MAX_CONTENT 码点（见 tokenizer 模块文档）
        assert len(baseline["blocks"][0][1]) == FENCE_MAX_CONTENT
        assert baseline["sentences"] == ["开头。", "结尾。\n"]

        assert _structure([crlf]) == baseline

    def test_long_fenced_crlf_chunked_equals_whole(self):
        """长 CRLF 文本按 1000 字符分块流式喂入 → 与一次性喂入相同。"""
        body = "y" * 70_000
        lf = "开头。\n```python\n" + body + "\n```\n结尾。\n"
        crlf = lf.replace("\n", "\r\n")
        chunked = [crlf[i:i + 1000] for i in range(0, len(crlf), 1000)]
        assert len(chunked) > 60                              # 防空洞：确实是多 chunk
        assert _structure(chunked) == _structure([lf])

    def test_very_long_unbroken_text_terminates_and_is_bounded(self):
        """10 万字符无标点无空白 → 长度保护强制切分，终止且每句 ≤ max_buffer。"""
        text = "a" * 100_000
        proc, events = _run([text], max_buffer_length=2000)
        sentences = [
            e.text for e in events if e.type is StreamEventType.SENTENCE
        ]
        assert sentences, "长文本必须被强制切分而不是整块暂留"
        assert all(len(s) <= 2000 for s in sentences)
        assert "".join(sentences) == text                     # 内容零丢失

    def test_very_long_spaced_text_bounded_and_terminates(self):
        """带空白的 15 万字符长文本：强制切分点不超上界（空白按既有 lstrip 语义丢弃）。"""
        text = "word " * 30_000
        _proc, events = _run([text], max_buffer_length=2000)
        sentences = [
            e.text for e in events if e.type is StreamEventType.SENTENCE
        ]
        assert sentences
        assert all(len(s) <= 2000 for s in sentences)
        assert "".join(sentences).replace(" ", "") == "word" * 30_000

    def test_long_crlf_sentence_equals_lf(self):
        """超长单句（无标点）末尾带 CRLF → 与 LF 版本产物相同。"""
        body = "啊" * 5000
        assert _structure([body + "\r\n"]) == _structure([body + "\n"])
