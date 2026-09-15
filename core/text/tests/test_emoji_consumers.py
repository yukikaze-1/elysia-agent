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
    def test_emoji_only_tail_merges_into_previous_sentence(self):
        """纯 emoji 尾句并入上一条 SENTENCE（不独占一行）—— ZWJ 序列同样成立。

        构造对齐既有 ``TestEmojiMerge``：句子与 emoji 尾全部压在 tokenizer pending
        中，flush 时同批落盘（``好。♪`` → 一条 ``好。♪``）。
        """
        proc = StreamProcessor(min_chunk_length=3)
        proc.feed("我先走了")
        proc.feed("。")
        proc.feed(_ZWJ_FAMILY)
        events = proc.flush()
        sentences = [
            e.text for e in events if e.type is StreamEventType.SENTENCE
        ]
        assert sentences == ["我先走了。" + _ZWJ_FAMILY], (
            f"ZWJ 序列不得自成一句：{sentences!r}"
        )

    def test_non_emoji_tail_stays_own_sentence(self):
        """反证（防空转）：同样的尾句位置放普通文本 → 确实是独立一句。"""
        proc = StreamProcessor(min_chunk_length=3)
        proc.feed("我先走了")
        proc.feed("。路上小心。")
        events = proc.flush()
        sentences = [
            e.text for e in events if e.type is StreamEventType.SENTENCE
        ]
        assert sentences == ["我先走了。", "路上小心。"]
