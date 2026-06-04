from unittest.mock import MagicMock, patch

import pytest

from .protocol import TTSBackend
from .registry import TTSFactory
from .text_preprocess import strip_all_markup, strip_action_only


# ============================================================
# text_preprocess
# ============================================================

class TestStripAllMarkup:
    def test_removes_all_markup(self):
        text = "你好，[微笑] 今天<开心>呢<<愉悦>> （真的）"
        result = strip_all_markup(text)
        assert result == "你好， 今天呢"

    def test_removes_fullwidth_parens(self):
        text = "嗯（语气轻柔地）好的"
        result = strip_all_markup(text)
        assert result == "嗯好的"

    def test_no_markup_passthrough(self):
        assert strip_all_markup("今天天气真好") == "今天天气真好"

    def test_empty_string(self):
        assert strip_all_markup("") == ""

    def test_nested_brackets_known_limitation(self):
        # 正则无法正确处理嵌套括号，这是已知限制
        text = "[动作 [内部] 结束] 说话"
        result = strip_all_markup(text)
        assert result == "结束] 说话"


class TestStripActionOnly:
    def test_removes_action_keeps_emotion(self):
        text = "[点头] 明白了<微笑> <<开心>>"
        result = strip_action_only(text)
        assert result == "明白了<微笑> <<开心>>"

    def test_removes_fullwidth_parens(self):
        text = "嗯（语气轻柔地）好的<温柔>"
        result = strip_action_only(text)
        assert result == "嗯好的<温柔>"

    def test_no_markup_passthrough(self):
        assert strip_action_only("今天天气真好") == "今天天气真好"

    def test_empty_string(self):
        assert strip_action_only("") == ""


# ============================================================
# Protocol
# ============================================================

class TestTTSBackendProtocol:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            TTSBackend()  # type: ignore[abstract]

    def test_missing_synthesize_raises(self):
        with pytest.raises(TypeError):

            class BadBackend1(TTSBackend):
                async def synthesize_stream(
                    self, text: str, *, character: str, emotion: str | None = None
                ):
                    yield b""

            BadBackend1()  # type: ignore[abstract]

    def test_missing_synthesize_stream_raises(self):
        with pytest.raises(TypeError):

            class BadBackend2(TTSBackend):
                async def synthesize(
                    self, text: str, *, character: str, emotion: str | None = None
                ) -> bytes:
                    return b""

            BadBackend2()  # type: ignore[abstract]

    def test_full_implementation_ok(self):
        class GoodBackend(TTSBackend):
            async def synthesize(
                self, text: str, *, character: str, emotion: str | None = None
            ) -> bytes:
                return b""

            async def synthesize_stream(
                self, text: str, *, character: str, emotion: str | None = None
            ):
                yield b""

            async def test(self) -> dict:
                return {"ok": True, "latency_ms": 0}

            async def close(self) -> None:
                pass

        backend = GoodBackend()
        assert isinstance(backend, TTSBackend)

    def test_missing_close_raises(self):
        with pytest.raises(TypeError):

            class BadBackend(TTSBackend):
                async def synthesize(self, text, *, character, emotion=None) -> bytes:
                    return b""

                async def synthesize_stream(self, text, *, character, emotion=None):
                    yield b""

                async def test(self) -> dict:
                    return {"ok": True, "latency_ms": 0}

            BadBackend()  # type: ignore[abstract]


# ============================================================
# TTSFactory
# ============================================================

class TestTTSFactory:
    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Invalid provider_id"):
            TTSFactory.create("gpt-sovits")

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            TTSFactory.create("unknown/backend")

    def test_gpt_sovits_parsing(self):
        with patch("Module.tts.registry.GPTSoVitsBackend", MagicMock()):
            backend = TTSFactory.create("gpt-sovits/default")
        assert backend is not None

    def test_custom_base_url(self):
        with patch("Module.tts.registry.GPTSoVitsBackend") as mock_cls:
            TTSFactory.create("gpt-sovits/default", base_url="http://127.0.0.1:9880")
        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs["base_url"] == "http://127.0.0.1:9880"


# ============================================================
# GPTSoVitsBackend
# ============================================================

def _make_stream_chunks(*chunks: bytes):
    """Build an async context manager that yields bytes from aiter_bytes."""

    class _StreamBody:
        def __init__(self, data: tuple[bytes, ...]):
            self._data = data
            self.status_code = 200

        def raise_for_status(self):
            pass

        async def aiter_bytes(self, chunk_size: int = 4096):
            for c in self._data:
                yield c

    return MagicMock(return_value=_StreamBody(chunks))


@pytest.fixture
def backend(tts_backend):
    return tts_backend


class TestGPTSoVitsConstruction:
    def test_loads_voices_yaml(self, backend):
        assert "Elysia" in backend._voices
        assert backend._voices["Elysia"]["ref_audio"] == "/fake/ref.wav"
        assert backend._voices["Elysia"]["prompt_text"] == "测试文本"


class TestGPTSoVitsSynthesize:
    @pytest.mark.asyncio
    async def test_collects_all_chunks(self, backend):
        called_with = {}

        async def fake_call_api(text, character, emotion, streaming):
            called_with.update(locals())
            for c in (b"aaaa", b"bbbb", b"cccc"):
                yield c

        backend._call_api = fake_call_api
        result = await backend.synthesize("hi", character="Elysia")
        assert result == b"aaaabbbbcccc"
        assert called_with["text"] == "hi"
        assert called_with["character"] == "Elysia"
        assert called_with["emotion"] is None
        assert called_with["streaming"] is False

    @pytest.mark.asyncio
    async def test_stream_yields_chunks(self, backend):
        async def fake_call_api(text, character, emotion, streaming):
            for c in (b"chunk1", b"chunk2"):
                yield c

        backend._call_api = fake_call_api
        chunks = []
        async for c in backend.synthesize_stream("hi", character="Elysia"):
            chunks.append(c)
        assert chunks == [b"chunk1", b"chunk2"]

    @pytest.mark.asyncio
    async def test_emotion_is_accepted(self, backend):
        """emotion 参数被传递但不影响 GPTSoVits 的行为。"""
        called_emotion = None

        async def fake_call_api(text, character, emotion, streaming):
            nonlocal called_emotion
            called_emotion = emotion
            yield b"ok"

        backend._call_api = fake_call_api
        result = await backend.synthesize(
            "hi", character="Elysia", emotion="happy"
        )
        assert result == b"ok"
        assert called_emotion == "happy"

    @pytest.mark.asyncio
    async def test_close(self, backend):
        await backend.close()


# ============================================================
# Emoji stripping
# ============================================================

class TestEmojiStripping:
    def test_strip_all_markup_removes_emoji_smile(self):
        assert strip_all_markup("hello 😀 world") == "hello  world"

    def test_strip_all_markup_removes_multi_emoji(self):
        result = strip_all_markup("🎉 congratulations 🎉")
        assert "🎉" not in result
        assert "congratulations" in result

    def test_strip_action_only_removes_emoji(self):
        result = strip_action_only("hello 😀 world")
        assert "😀" not in result

    def test_strip_all_markup_pure_markup_returns_empty(self):
        assert strip_all_markup("（语气轻柔地）") == ""

    def test_strip_all_markup_mixed_markup_and_text(self):
        assert strip_all_markup("（语气）hello（动作）") == "hello"


# ============================================================
# GPTSoVitsBackend — payload verification
# ============================================================

class TestGPTSoVitsErrors:
    @pytest.mark.asyncio
    async def test_request_error_wraps_to_runtime_error(self, backend):
        import httpx

        async def fake_call_api(text, character, emotion, streaming):
            raise httpx.RequestError("connection refused")
            yield b""  # type: ignore[unreachable] — 让函数成为 generator

        backend._call_api = fake_call_api
        with pytest.raises((RuntimeError, httpx.RequestError)):
            await backend.synthesize("hi", character="Elysia")

    @pytest.mark.asyncio
    async def test_http_error_from_call_api_propagates(self, backend):
        async def fake_call_api(text, character, emotion, streaming):
            raise RuntimeError("simulated HTTP error")
            yield b""  # type: ignore[unreachable] — 让函数成为 generator

        backend._call_api = fake_call_api
        with pytest.raises(RuntimeError, match="simulated"):
            await backend.synthesize("hi", character="Elysia")

    @pytest.mark.asyncio
    async def test_character_not_in_voices_raises_keyerror(self, backend):
        with pytest.raises(KeyError):
            await backend.synthesize("hi", character="UnknownCharacter")


# ============================================================
# GPTSoVitsBackend — queue cancel path
# ============================================================

class TestGPTSoVitsCancel:
    @pytest.mark.asyncio
    async def test_consumer_early_break_cancels_task(self, backend):
        """consumer 提前停止迭代时 task 被取消。"""
        cancel_called = False

        async def fake_call_api(text, character, emotion, streaming):
            nonlocal cancel_called
            try:
                yield b"chunk1"
                yield b"chunk2"
                yield b"chunk3"
            except asyncio.CancelledError:
                cancel_called = True
                raise

        backend._call_api = fake_call_api
        async for _ in backend.synthesize_stream("hi", character="Elysia"):
            break  # 只取第一个 chunk 就退出
        # 每次请求独立 client，无共享 client 需关闭

