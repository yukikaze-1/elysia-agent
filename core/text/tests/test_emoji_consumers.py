"""纯 emoji 序列的**消费端**闸门测试 —— CODE_REVIEW_REPORT #46 缺口行 #12 下沿。

`is_emoji_only` 本身的正反例在 ``test_tags.py``；本文件钉死"序列不得泄漏成
语音/合成请求"这条**消费端**后果：

- ``core.adapter.tts_adapter.TTSStreamManager.stream_sentence``：命中
  ``is_emoji_only`` 的句子进 emoji 排队（依赖前一句 TTS 播出后同帧发出），
  **不创建合成任务**；未命中则走 ``_preprocess`` → ``synthesize``。
- ``core.text.stream_processor.StreamProcessor``：flush 尾句是纯 emoji 时
  并入上一条 SENTENCE，不独占一条。

为什么这是真缺陷而不只是"少判一个 emoji"：`👨‍👩‍👧‍👦` 被误判为非 emoji 后，
``tts_adapter`` 会把它送去合成，而 ``Module/tts/text_preprocess.py`` 的
``_EMOJI_RE`` 同样只认单码点，剥掉 4 个基底后**只剩 ZWJ**
（``"\u200d\u200d\u200d"``）→ 非空 → 真的发出一次无意义合成请求。
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from core.adapter.tts_adapter import TTSStreamManager
from core.text.stream_processor import (
    StreamProcessor,
    StreamEventType,
)

pytestmark = pytest.mark.unit

_ZWJ_FAMILY = "\U0001F468\u200D\U0001F469\u200D\U0001F467\u200D\U0001F466"


class _RecordingTTS:
    """假 TTS 后端：记录每一次 synthesize 调用（判定"是否真的发请求"）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def synthesize(self, text, **kwargs):
        self.calls.append(text)
        return b"RIFF" + b"\0" * 16

    def get_preprocess(self):
        return None

    @property
    def stream_format(self) -> str:
        return "wav"

    async def close(self) -> None:
        pass


def _manager() -> tuple[TTSStreamManager, _RecordingTTS]:
    tts = _RecordingTTS()
    mgr = TTSStreamManager(tts, AsyncMock(), "test")
    mgr.reset_for_turn()
    return mgr, tts


class TestTTSEmojiGate:
    @pytest.mark.asyncio
    async def test_zwj_family_sentence_skips_synthesis(self):
        """ZWJ 家庭句 → 不进合成（修复前会被送进 synthesize）。"""
        mgr, tts = _manager()
        mgr.stream_sentence(_ZWJ_FAMILY)
        await asyncio.sleep(0)
        assert tts.calls == []

    @pytest.mark.asyncio
    async def test_plain_sentence_still_reaches_synthesis(self):
        """反证（防空转）：同一 manager、同一 fake，普通文本**确实**会进合成。"""
        mgr, tts = _manager()
        mgr.stream_sentence("你好世界。")
        await asyncio.sleep(0)
        assert tts.calls == ["你好世界。"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "sentence",
        [
            pytest.param(_ZWJ_FAMILY, id="zwj-family"),
            pytest.param("\u2764\uFE0F", id="heart-vs16"),
            pytest.param("1\uFE0F\u20E3", id="keycap-1"),
            pytest.param("\U0001F1E8\U0001F1F3", id="flag-cn"),
            pytest.param("\U0001F3F3\uFE0F\u200D\U0001F308", id="rainbow-flag"),
            pytest.param("\U0001F44D\U0001F3FD", id="skin-tone"),
        ],
    )
    async def test_emoji_sequences_skip_synthesis(self, sentence):
        """各类 emoji 序列整句 → 一律不触发合成请求。"""
        mgr, tts = _manager()
        mgr.stream_sentence(sentence)
        await asyncio.sleep(0)
        assert tts.calls == []

    @pytest.mark.asyncio
    async def test_no_synthesis_task_is_created_for_emoji_sentence(self):
        """emoji 句不创建 TTS 任务（`_tasks` 私有态经公开 ``wait_all`` 观测为无挂起）。"""
        mgr, tts = _manager()
        mgr.stream_sentence(_ZWJ_FAMILY)
        await mgr.wait_all()          # 无任务时应立即返回，不挂死
        assert tts.calls == []


class TestStreamProcessorEmojiTail:
    """emoji-only 尾句的**新**语义（2026-09-16，S3-G7 句末标点即时落盘）。

    旧语义（本类原用例名 ``test_emoji_only_tail_merges_into_previous_sentence``）：
    emoji 尾并入上一条 SENTENCE。但那个合并的实现前提是"句子与 emoji 尾停在同一个
    待落盘缓冲里、在 `flush()` 里同批产出"——恰恰就是"无空格无换行的回复整轮滞留到
    EOF"这个缺陷本身。句末标点改为即时落盘后，句子在标点到达时就已经派发给 TTS/段帧，
    **没有可并入的对象**，该合并成为不可实现的死代码（已随之删除）。

    代价如实记录：emoji 尾现在自成一条 SENTENCE ⇒ v2 客户端多一个段帧（内容就是
    emoji）。TTS 不受影响（emoji-only 句在 `TTSStreamManager.stream_sentence` 被门控
    跳过，见 `TestTTSEmojiGate`）；显示不受影响（文本由 TEXT_DELTA 承载）。
    若要恢复合并语义，唯一办法是**延迟句子发出**直到看见下一个 token——那会重新引入
    本次要修的延迟（模型在句间停顿多久，用户就等多久），故不做。
    """

    def test_emoji_only_tail_is_own_sentence(self):
        """纯 emoji 尾句 → 独立 SENTENCE（不再与上句合并），ZWJ 序列同样成立。"""
        proc = StreamProcessor(min_chunk_length=3)
        proc.feed("我先走了")
        proc.feed("。")
        proc.feed(_ZWJ_FAMILY)
        events = proc.flush()
        sentences = [
            e.text for e in events if e.type is StreamEventType.SENTENCE
        ]
        assert sentences == [_ZWJ_FAMILY], f"emoji 尾应为独立一句：{sentences!r}"

    def test_sentence_is_emitted_at_the_boundary_not_at_flush(self):
        """反证的另一半：上一句**已经**在句号处发出（这正是合并无法进行的原因）。"""
        proc = StreamProcessor(min_chunk_length=3)
        proc.feed("我先走了")
        at_boundary = proc.feed("。")
        sentences = [
            e.text for e in at_boundary if e.type is StreamEventType.SENTENCE
        ]
        assert sentences == ["我先走了。"], (
            f"句号到达即应产出 SENTENCE（不得等到 flush）：{sentences!r}"
        )

    def test_non_emoji_tail_stays_own_sentence(self):
        """反证（防空转）：同样的位置放普通文本 → 两句都在标点处即时产出。"""
        proc = StreamProcessor(min_chunk_length=3)
        proc.feed("我先走了")
        events = proc.feed("。路上小心。") + proc.flush()
        sentences = [
            e.text for e in events if e.type is StreamEventType.SENTENCE
        ]
        assert sentences == ["我先走了。", "路上小心。"], (
            f"两句都应即时产出：{sentences!r}"
        )
