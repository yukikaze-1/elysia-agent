"""自写确定性 fuzz — 随机标签序列 × 随机切分点。

对齐 spec 2026-08-02 E 节。两条不变式：
1. 跨 chunk 等价：任意序列整串 vs 随机切分 → token 流一致
2. 序列后状态干净：平衡序列 + sentinel 精确匹配，暴露状态残留（幽灵 FENCE）

固定种子可复现，300 组，总耗时 <5s。
"""

import random

import pytest

from core.text.tokenizer import StreamTokenizer, Token, TokenKind

pytestmark = pytest.mark.unit

SEED = 20260802
ITERATIONS = 300
# 哨兵必须用生成器无法产生的文本：语音池含"你好世界"，若哨兵与其相同，
# 序列自身的"你好世界" token 会满足 any(...) 断言（幽灵 FENCE 吞掉真实
# 哨兵时仍被逃过）。"哨兵#SENT#" 无分隔符、无标签前缀，恒为单一 SPEECH token。
_SENTINEL = "\n哨兵#SENT#"  # 前导 \n 硬分隔符 → sentinel 独立成句


def _random_closed_tag(rng) -> str:
    """返回一个闭合的随机标签串。"""
    kind = rng.choice(["pause", "stage_cn", "stage_en", "angle_en",
                       "mood", "inner_voice", "code_tag", "markdown_tag"])
    if kind == "pause":
        return "<#0.5#>"
    if kind == "stage_cn":
        return rng.choice(["（眨眼）", "(叹气)", "（我有点，犹豫。）"])
    if kind == "stage_en":
        return rng.choice(["(laughs)", "(sighs)", "(clears throat)"])
    if kind == "angle_en":
        return rng.choice(["<pant>", "<laughs>"])
    if kind == "mood":
        return f"<mood>{rng.choice(['开心', '累', '平静'])}</mood>"
    if kind == "inner_voice":
        return f"<inner_voice>{rng.choice(['思考中', '犹豫', '决定'])}</inner_voice>"
    if kind == "code_tag":
        return f"<|code|>{rng.choice(['x=1', 'def f(): pass'])}</|code|>"
    return f"<|markdown|>{rng.choice(['**粗**', '# 标题'])}</|markdown|>"


def _random_fence(rng) -> str:
    """闭合围栏（多行 \\n 形式，前导 \\n 保证行首开启）。"""
    lang = rng.choice(["python", "js", ""])
    body = rng.choice(["code1", "x = 1", "print(1)"])
    return f"\n```{lang}\n{body}\n```\n"


def _random_speech(rng) -> str:
    """随机语音段（含分句标点 + 可选 ♪/♡）。"""
    return rng.choice(["你好世界", "说句话吧。", "嗯？", "哈哈！", "好吧。"]) + \
        rng.choice(["", "♪", "♡"])


def _balanced_sequence(rng) -> str:
    """平衡序列：所有开的标签必闭，供状态干净性测试。"""
    return "".join(
        rng.choice([lambda: _random_closed_tag(rng),
                    lambda: _random_fence(rng),
                    lambda: _random_speech(rng)])()
        for _ in range(rng.randint(0, 8))
    )


def _arbitrary_sequence(rng) -> str:
    """任意序列：可含未闭合标签，供跨 chunk 等价测试。"""
    items: list[str] = []
    for _ in range(rng.randint(0, 8)):
        kind = rng.randint(0, 3)
        if kind == 0:
            items.append(_random_closed_tag(rng))
        elif kind == 1:
            items.append(_random_fence(rng))
        elif kind == 2:
            items.append(_random_speech(rng))
        else:
            items.append(rng.choice(["<#", "<mood>开", "```", "<inner_voice>", "（长"]))
    return "".join(items)


def _random_cuts(rng, n: int) -> list[int]:
    if n <= 1:
        return []
    k = rng.randint(1, min(5, n))
    return sorted(set(rng.randint(0, n) for _ in range(k)))


def _token_stream(s: str, cuts: list[int]) -> list[Token]:
    """按切分点依次 feed，再 flush，返回 token 序列。"""
    tz = StreamTokenizer()
    pieces: list[str] = []
    prev = 0
    for cut in cuts:
        pieces.append(s[prev:cut])
        prev = cut
    pieces.append(s[prev:])
    toks: list[Token] = []
    for p in pieces:
        toks.extend(tz.feed(p))
    toks.extend(tz.flush())
    return toks


def _token_key(t: Token):
    return (t.kind.name, t.text, t.meta_kind, t.lang)


def _has_exact_sentinel(toks: list[Token]) -> bool:
    return any(t.kind == TokenKind.SPEECH and t.text == "哨兵#SENT#" for t in toks)


class TestCrossChunkEquivalence:
    """不变式 1：任意序列整串 vs 随机切分 → token 流一致。"""

    def test_arbitrary_sequence(self):
        rng = random.Random(SEED)
        for i in range(ITERATIONS):
            s = _arbitrary_sequence(rng)
            expected = [_token_key(t) for t in _token_stream(s, [])]
            cuts = _random_cuts(rng, len(s))
            got = [_token_key(t) for t in _token_stream(s, cuts)]
            assert got == expected, f"iter={i} S={s!r} cuts={cuts!r}\n  whole={expected}\n  got={got}"

    def test_balanced_sequence(self):
        rng = random.Random(SEED + 1)
        for i in range(ITERATIONS):
            s = _balanced_sequence(rng) + _SENTINEL
            expected = [_token_key(t) for t in _token_stream(s, [])]
            cuts = _random_cuts(rng, len(s))
            got = [_token_key(t) for t in _token_stream(s, cuts)]
            assert got == expected, f"iter={i} S={s!r} cuts={cuts!r}"


class TestStateCleanliness:
    """不变式 2：平衡序列后状态干净——sentinel 精确出现，未被吞进/误判/并入。"""

    def test_sentinel_exact_match_after_balanced(self):
        rng = random.Random(SEED + 2)
        for i in range(ITERATIONS):
            s = _balanced_sequence(rng)
            toks = _token_stream(s + _SENTINEL, [])
            assert _has_exact_sentinel(toks), \
                f"iter={i} S={s!r}\n toks={[_token_key(t) for t in toks]}"

    def test_sentinel_exact_match_split_feed(self):
        rng = random.Random(SEED + 3)
        for i in range(ITERATIONS):
            s = _balanced_sequence(rng) + _SENTINEL
            cuts = _random_cuts(rng, len(s))
            toks = _token_stream(s, cuts)
            assert _has_exact_sentinel(toks), \
                f"iter={i} S={s!r} cuts={cuts!r}\n toks={[_token_key(t) for t in toks]}"
