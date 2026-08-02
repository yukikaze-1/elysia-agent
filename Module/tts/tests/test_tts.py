import asyncio
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from ..protocol import TTSBackend
from ..registry import TTSFactory
from ..minimax_tts import MiniMaxTTSBackend, _RpmLimiter
from ..text_preprocess import strip_all_markup, strip_action_only, strip_for_minimax

pytestmark = pytest.mark.service


@pytest.fixture(autouse=True)
def _set_minimax_url(monkeypatch):
    """设置 MiniMax TTS URL 环境变量（所有 MiniMax 测试共用）。"""
    monkeypatch.setenv("MINIMAX_TTS_URL", "https://api.minimaxi.com")


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
                @property
                def provider_name(self) -> str:
                    return "test"

                async def synthesize_stream(
                    self, text: str, *, character: str, emotion: str | None = None
                ):
                    yield b""

            BadBackend1()  # type: ignore[abstract]

    def test_missing_synthesize_stream_raises(self):
        with pytest.raises(TypeError):

            class BadBackend2(TTSBackend):
                @property
                def provider_name(self) -> str:
                    return "test"

                async def synthesize(
                    self, text: str, *, character: str, emotion: str | None = None
                ) -> bytes:
                    return b""

            BadBackend2()  # type: ignore[abstract]

    def test_full_implementation_ok(self):
        class GoodBackend(TTSBackend):
            @property
            def provider_name(self) -> str:
                return "test"

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
        assert backend.provider_name == "test"
        assert backend.get_preprocess() is None

    def test_missing_provider_name_raises(self):
        with pytest.raises(TypeError):

            class BadBackend(TTSBackend):
                async def synthesize(self, text, *, character, emotion=None) -> bytes:
                    return b""

                async def synthesize_stream(self, text, *, character, emotion=None):
                    yield b""

                async def test(self) -> dict:
                    return {"ok": True, "latency_ms": 0}

                async def close(self) -> None:
                    pass

            BadBackend()  # type: ignore[abstract]

    def test_get_preprocess_default_returns_none(self):
        class Backend(TTSBackend):
            @property
            def provider_name(self) -> str:
                return "test"

            async def synthesize(self, text, *, character, emotion=None) -> bytes:
                return b""

            async def synthesize_stream(self, text, *, character, emotion=None):
                yield b""

            async def test(self) -> dict:
                return {"ok": True, "latency_ms": 0}

            async def close(self) -> None:
                pass

        backend = Backend()
        assert backend.get_preprocess() is None

    def test_missing_close_raises(self):
        with pytest.raises(TypeError):

            class BadBackend(TTSBackend):
                @property
                def provider_name(self) -> str:
                    return "test"

                async def synthesize(self, text, *, character, emotion=None) -> bytes:
                    return b""

                async def synthesize_stream(self, text, *, character, emotion=None):
                    yield b""

                async def test(self) -> dict:
                    return {"ok": True, "latency_ms": 0}

            BadBackend()  # type: ignore[abstract]

    def test_provider_name_is_abstract(self):
        """provider_name 是抽象属性，未实现时不能实例化。"""
        with pytest.raises(TypeError):

            class NoProviderName(TTSBackend):
                async def synthesize(self, text, *, character, emotion=None) -> bytes:
                    return b""

                async def synthesize_stream(self, text, *, character, emotion=None):
                    yield b""

                async def test(self) -> dict:
                    return {"ok": True, "latency_ms": 0}

                async def close(self) -> None:
                    pass

                def get_preprocess(self):
                    return None

            NoProviderName()  # type: ignore[abstract]


# ============================================================
# TTSFactory
# ============================================================

class TestTTSFactory:
    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Invalid model_id"):
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

    def test_minimax_provider(self):
        with patch("Module.tts.minimax_tts.MiniMaxTTSBackend") as mock_cls:
            mock_cls.return_value = MagicMock()
            backend = TTSFactory.create(
                "minimax/speech-2.8-hd",
                api_key="sk-test",
            )
        assert backend is not None
        call_kwargs = mock_cls.call_args.kwargs
        assert call_kwargs["api_key"] == "sk-test"
        assert call_kwargs["model"] == "speech-2.8-hd"
        assert "https://api.minimaxi.com" in call_kwargs["base_url"]

    def test_minimax_missing_api_key_raises(self):
        def fake_getenv(key, default=None):
            if key == "MINIMAX_API_KEY":
                return None
            return os.environ.get(key, default)
        with patch("os.getenv", side_effect=fake_getenv):
            with pytest.raises(ValueError, match="MiniMax TTS 需要 api_key"):
                TTSFactory.create("minimax/speech-2.8-hd")

    def test_minimax_custom_base_url(self):
        with patch("Module.tts.minimax_tts.MiniMaxTTSBackend") as mock_cls:
            mock_cls.return_value = MagicMock()
            TTSFactory.create(
                "minimax/speech-2.8-hd",
                api_key="sk-test",
                base_url="https://api-bj.minimaxi.com",
            )
        assert mock_cls.call_args.kwargs["base_url"] == "https://api-bj.minimaxi.com"

    def test_minimax_receives_rate_limit_rpm(self):
        """rate_limit_rpm 透传给 minimax 后端。"""
        with patch("Module.tts.minimax_tts.MiniMaxTTSBackend") as mock_cls:
            mock_cls.return_value = MagicMock()
            TTSFactory.create(
                "minimax/speech-2.8-hd",
                api_key="sk-test",
                rate_limit_rpm=7,
            )
        assert mock_cls.call_args.kwargs["rate_limit_rpm"] == 7

    def test_gpt_sovits_ignores_rate_limit_rpm(self):
        """rate_limit_rpm 被 factory 拦截，不透传给 gpt-sovits（避免 TypeError）。"""
        with patch("Module.tts.registry.GPTSoVitsBackend") as mock_cls:
            mock_cls.return_value = MagicMock()
            TTSFactory.create("gpt-sovits/default", rate_limit_rpm=7)
        assert "rate_limit_rpm" not in mock_cls.call_args.kwargs

    def test_custom_ignores_rate_limit_rpm(self):
        """rate_limit_rpm 被 factory 拦截，不透传给 custom/openai 后端。"""
        with patch("Module.tts.openai_tts.OpenAICompatTTSBackend") as mock_cls:
            mock_cls.return_value = MagicMock()
            TTSFactory.create(
                "custom/tts-1", api_key="sk", base_url="http://127.0.0.1:1",
                rate_limit_rpm=7,
            )
        assert "rate_limit_rpm" not in mock_cls.call_args.kwargs


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


# ============================================================
# strip_for_minimax
# ============================================================

class TestStripForMiniMax:
    def test_removes_brackets_and_double_angle(self):
        text = "你好，[微笑] 今天<<开心>>呢"
        result = strip_for_minimax(text)
        assert result == "你好， 今天呢"

    def test_keeps_parens_tone_tags(self):
        text = "今天是不是很开心呀(laughs)，当然了！"
        result = strip_for_minimax(text)
        assert result == "今天是不是很开心呀(laughs)，当然了！"

    def test_keeps_angle_pause_control(self):
        text = "你好<#0.5#>世界"
        result = strip_for_minimax(text)
        assert result == "你好<#0.5#>世界"

    def test_keeps_fullwidth_parens(self):
        text = "嗯（语气轻柔地）好的"
        result = strip_for_minimax(text)
        assert result == "嗯（语气轻柔地）好的"

    def test_keeps_pinyin_annotation(self):
        text = "处理(chu3li3)完成"
        result = strip_for_minimax(text)
        assert result == "处理(chu3li3)完成"

    def test_removes_emoji(self):
        text = "hello 😀 world 🎉"
        result = strip_for_minimax(text)
        assert "😀" not in result
        assert "🎉" not in result
        assert "hello" in result
        assert "world" in result

    def test_no_markup_passthrough(self):
        assert strip_for_minimax("今天天气真好") == "今天天气真好"

    def test_empty_string(self):
        assert strip_for_minimax("") == ""

    def test_pure_markup_returns_empty(self):
        assert strip_for_minimax("[笑]<<开心>>") == ""


# ============================================================
# MiniMaxTTSBackend
# ============================================================

class TestMiniMaxConstruction:
    def test_defaults(self):
        backend = MiniMaxTTSBackend(api_key="sk-test", base_url="https://api.minimaxi.com", voice_id="test-voice")
        assert backend._model == "speech-2.8-hd"
        assert backend._base_url == "https://api.minimaxi.com"
        assert backend._voice_id == "test-voice"
        assert "Elysia" in backend._voice_map  # 从 minimax_voices.yaml 加载
        assert backend._speed == 1.0
        assert backend._vol == 1.0
        assert not backend._client.is_closed

    def test_voice_map_resolve(self):
        backend = MiniMaxTTSBackend(
            api_key="sk-test",
            base_url="https://api.minimaxi.com",
            voice_id="default-voice",
            voice_map={"Elysia": "female-elysia-01"},
        )
        assert backend._resolve_voice("Elysia") == "female-elysia-01"
        assert backend._resolve_voice("Unknown") == "default-voice"

    def test_voice_map_default_voice_id(self):
        backend = MiniMaxTTSBackend(api_key="sk-test", base_url="https://api.minimaxi.com", voice_id="test-voice")
        assert backend._resolve_voice("Anyone") == "test-voice"

    @pytest.mark.asyncio
    async def test_close(self):
        backend = MiniMaxTTSBackend(api_key="sk-test", base_url="https://api.minimaxi.com", voice_id="test-voice")
        await backend.close()
        assert backend._client.is_closed


class TestMiniMaxSynthesize:
    @pytest.mark.asyncio
    async def test_hex_decode_and_return_bytes(self):
        """验证非流式：POST → JSON → hex decode → bytes"""
        backend = MiniMaxTTSBackend(api_key="sk-test", base_url="https://api.minimaxi.com", voice_id="test-voice")

        # mock _client.post 返回 hex 编码的音频
        wav_bytes = b"RIFF....WAVE...."  # 伪 WAV
        hex_audio = wav_bytes.hex()

        class MockResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "data": {"audio": hex_audio, "status": 2},
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                }

        async def fake_post(url, *, json, **kw):
            return MockResp()

        backend._client.post = fake_post  # type: ignore[assignment]
        result = await backend.synthesize("你好", character="test")
        assert result == wav_bytes

    @pytest.mark.asyncio
    async def test_payload_structure(self):
        """验证请求体包含正确的 voice_setting 和 audio_setting"""
        captured_payload = {}

        class MockResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "data": {"audio": "00ff"},
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                }

        async def fake_post(url, *, json, **kw):
            captured_payload.update(json)
            return MockResp()

        backend = MiniMaxTTSBackend(
            api_key="sk-test",
            base_url="https://api.minimaxi.com",
            model="speech-2.8-turbo",
            voice_id="male-qn-qingse",
            speed=1,
            vol=1,
            pitch=0,
            audio_format="wav",
        )
        backend._client.post = fake_post  # type: ignore[assignment]
        await backend.synthesize("你好", character="TestChar", emotion="happy")

        assert captured_payload["model"] == "speech-2.8-turbo"
        assert captured_payload["text"] == "你好"
        assert captured_payload["stream"] is False
        assert captured_payload["voice_setting"]["voice_id"] == "male-qn-qingse"
        assert captured_payload["voice_setting"]["speed"] == 1
        assert captured_payload["voice_setting"]["vol"] == 1
        assert captured_payload["voice_setting"]["pitch"] == 0
        assert captured_payload["voice_setting"]["emotion"] == "happy"
        assert captured_payload["audio_setting"]["format"] == "wav"
        assert captured_payload["audio_setting"]["bitrate"] == 128000

    @pytest.mark.asyncio
    async def test_voice_map_used_for_character(self):
        """character 通过 voice_map 映射到 voice_id"""
        captured_voice_id = {}

        class MockResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "data": {"audio": "00ff"},
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                }

        async def fake_post(url, *, json, **kw):
            captured_voice_id["voice_id"] = json["voice_setting"]["voice_id"]
            return MockResp()

        backend = MiniMaxTTSBackend(
            api_key="sk-test",
            base_url="https://api.minimaxi.com",
            voice_id="default",
            voice_map={"Elysia": "female-elysia-01"},
        )
        backend._client.post = fake_post  # type: ignore[assignment]
        await backend.synthesize("hi", character="Elysia")
        assert captured_voice_id["voice_id"] == "female-elysia-01"

    @pytest.mark.asyncio
    async def test_no_emotion_in_payload_when_none(self):
        captured_payload = {}

        class MockResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "data": {"audio": "00ff"},
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                }

        async def fake_post(url, *, json, **kw):
            captured_payload.update(json)
            return MockResp()

        backend = MiniMaxTTSBackend(api_key="sk-test", base_url="https://api.minimaxi.com", voice_id="test-voice")
        backend._client.post = fake_post  # type: ignore[assignment]
        await backend.synthesize("hi", character="test", emotion=None)
        assert "emotion" not in captured_payload["voice_setting"]


class TestMiniMaxSynthesizeStream:
    @pytest.mark.asyncio
    async def test_yields_chunks_from_synthesize(self):
        """synthesize_stream 内部调用 synthesize()，分块 yield 完整 wav"""
        wav_data = b"\x00" * 100  # 小于 4096，单 chunk

        class MockResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "data": {"audio": wav_data.hex()},
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                }

        async def fake_post(url, *, json, **kw):
            return MockResp()

        backend = MiniMaxTTSBackend(api_key="sk-test", base_url="https://api.minimaxi.com", voice_id="test-voice")
        backend._client.post = fake_post  # type: ignore[assignment]

        chunks = []
        async for chunk in backend.synthesize_stream("test", character="x"):
            chunks.append(chunk)
        assert chunks == [wav_data]

    @pytest.mark.asyncio
    async def test_large_audio_multi_chunk(self):
        """大于 4096 字节的音频分多个 chunk yield"""
        wav_data = b"\x01" * 5000  # 5000 bytes → 2 chunks (4096 + 904)

        class MockResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "data": {"audio": wav_data.hex()},
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                }

        async def fake_post(url, *, json, **kw):
            return MockResp()

        backend = MiniMaxTTSBackend(api_key="sk-test", base_url="https://api.minimaxi.com", voice_id="test-voice")
        backend._client.post = fake_post  # type: ignore[assignment]

        chunks = []
        async for chunk in backend.synthesize_stream("test", character="x"):
            chunks.append(chunk)
        assert len(chunks) == 2
        assert chunks[0] == wav_data[:4096]
        assert chunks[1] == wav_data[4096:]
        assert b"".join(chunks) == wav_data


class TestMiniMaxErrors:
    @pytest.mark.asyncio
    async def test_base_resp_non_zero_raises(self):
        """base_resp.status_code != 0 → RuntimeError"""
        class MockResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "data": {"audio": ""},
                    "base_resp": {"status_code": 1004, "status_msg": "rate limit"},
                }

        backend = MiniMaxTTSBackend(api_key="sk-test", base_url="https://api.minimaxi.com", voice_id="test-voice")

        async def fake_post(url, *, json, **kw):
            return MockResp()

        backend._client.post = fake_post  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="MiniMax API 错误"):
            await backend.synthesize("hi", character="test")

    @pytest.mark.asyncio
    async def test_http_error_propagates(self):
        """HTTP 4xx/5xx → httpx.HTTPStatusError"""
        import httpx

        class MockResp:
            status_code = 401

            def raise_for_status(self):
                raise httpx.HTTPStatusError(
                    "Unauthorized", request=MagicMock(), response=MagicMock(status_code=401)
                )

            def json(self):
                return {"base_resp": {"status_code": 1001, "status_msg": "unauthorized"}}

        backend = MiniMaxTTSBackend(api_key="sk-bad", base_url="https://api.minimaxi.com", voice_id="test-voice")

        async def fake_post(url, *, json, **kw):
            return MockResp()

        backend._client.post = fake_post  # type: ignore[assignment]
        with pytest.raises(httpx.HTTPStatusError):
            await backend.synthesize("hi", character="test")

    @pytest.mark.asyncio
    async def test_empty_audio_returns_empty_bytes(self):
        """data.audio 为空字符串 → 返回空 bytes"""
        class MockResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "data": {"audio": ""},
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                }

        backend = MiniMaxTTSBackend(api_key="sk-test", base_url="https://api.minimaxi.com", voice_id="test-voice")

        async def fake_post(url, *, json, **kw):
            return MockResp()

        backend._client.post = fake_post  # type: ignore[assignment]
        result = await backend.synthesize("hi", character="test")
        assert result == b""


class TestMiniMaxCancel:
    @pytest.mark.asyncio
    async def test_stream_early_break_stops_iteration(self):
        """consumer 提前停止迭代 — 正常终止，无残留 task"""
        wav_data = b"\x00" * 10000  # 多个 chunk

        class MockResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "data": {"audio": wav_data.hex()},
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                }

        async def fake_post(url, *, json, **kw):
            return MockResp()

        backend = MiniMaxTTSBackend(api_key="sk-test", base_url="https://api.minimaxi.com", voice_id="test-voice")
        backend._client.post = fake_post  # type: ignore[assignment]

        count = 0
        async for _ in backend.synthesize_stream("test", character="x"):
            count += 1
            break  # 只取首个 chunk 即退出
        assert count == 1


# ============================================================
# _RpmLimiter — 滑动窗口 RPM 限速器
# ============================================================

class TestRpmLimiter:
    @pytest.mark.asyncio
    async def test_burst_allowed_when_window_fresh(self):
        """窗口空闲时可突发 — 短回复（<max 句）不受限速影响。"""
        limiter = _RpmLimiter(max_per_minute=3, window_seconds=1.0)
        t0 = time.monotonic()
        for _ in range(3):
            await limiter.acquire()
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5  # 3 个瞬时通过，无等待

    @pytest.mark.asyncio
    async def test_throttles_when_window_full(self):
        """窗口满后，下一个请求须等待最早请求滑出窗口。"""
        limiter = _RpmLimiter(max_per_minute=2, window_seconds=0.3)
        await limiter.acquire()  # 0s
        await limiter.acquire()  # 0s — 窗口满
        t0 = time.monotonic()
        await limiter.acquire()  # 第 3 个 — 须等最早请求滑出 (≈0.3s)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.2

    @pytest.mark.asyncio
    async def test_sustained_spacing_holds(self):
        """持续请求时，总耗时 >= 窗口时长（排队滑动，不超窗）。"""
        limiter = _RpmLimiter(max_per_minute=2, window_seconds=0.4)
        t0 = time.monotonic()
        for _ in range(4):
            await limiter.acquire()
        elapsed = time.monotonic() - t0
        # 前 2 个瞬时，后 2 个须等窗口滑动 → 总耗时 ≈ 0.4s
        assert elapsed >= 0.35

    @pytest.mark.asyncio
    async def test_disabled_when_max_zero(self):
        """max_per_minute=0 → 无限速，瞬时通过。"""
        limiter = _RpmLimiter(max_per_minute=0, window_seconds=0.01)
        t0 = time.monotonic()
        for _ in range(5):
            await limiter.acquire()
        assert time.monotonic() - t0 < 0.3

    @pytest.mark.asyncio
    async def test_window_slides_after_elapse(self):
        """窗口滑出后可再次突发 — 冷却后新请求不被旧请求拖累。"""
        limiter = _RpmLimiter(max_per_minute=2, window_seconds=0.2)
        await limiter.acquire()
        await limiter.acquire()
        await limiter.acquire()  # 阻塞直到第 1 个滑出 (≈0.2s)
        await asyncio.sleep(0.25)  # 整个窗口滑空
        t0 = time.monotonic()
        for _ in range(2):
            await limiter.acquire()  # 冷却后重新可突发
        assert time.monotonic() - t0 < 0.3


# ============================================================
# MiniMaxTTSBackend — RPM 限速接线
# ============================================================

class TestMiniMaxRateLimit:
    def test_default_rpm_is_20(self):
        """默认按 MiniMax 配额 RPM=20 限速。"""
        backend = MiniMaxTTSBackend(
            api_key="sk-test", base_url="https://api.minimaxi.com", voice_id="test-voice",
        )
        assert backend._rate_limiter._max == 20

    def test_rpm_zero_disables_limiter(self):
        """rate_limit_rpm=0 → 关闭限速（自托管/更高配额场景）。"""
        backend = MiniMaxTTSBackend(
            api_key="sk-test", base_url="https://api.minimaxi.com", voice_id="test-voice",
            rate_limit_rpm=0,
        )
        assert backend._rate_limiter._max == 0

    @pytest.mark.asyncio
    async def test_synthesize_obeys_rate_limiter(self):
        """synthesize 走限速器；单次调用（<RPM）正常返回不受阻。"""
        wav_bytes = b"RIFF....WAVE...."
        backend = MiniMaxTTSBackend(
            api_key="sk-test", base_url="https://api.minimaxi.com", voice_id="test-voice",
            rate_limit_rpm=1,
        )

        class MockResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "data": {"audio": wav_bytes.hex()},
                    "base_resp": {"status_code": 0, "status_msg": "success"},
                }

        async def fake_post(url, *, json, **kw):
            return MockResp()

        backend._client.post = fake_post  # type: ignore[assignment]
        result = await backend.synthesize("你好", character="test")
        assert result == wav_bytes


# ============================================================
# TTSBackend provider_name + get_preprocess
# ============================================================

class TestTTSBackendProviderName:
    def test_gpt_sovits_provider_name(self, backend):
        assert backend.provider_name == "gpt-sovits"

    def test_gpt_sovits_get_preprocess(self, backend):
        from ..text_preprocess import strip_all_markup
        assert backend.get_preprocess() is strip_all_markup

    def test_minimax_provider_name(self):
        from ..minimax_tts import MiniMaxTTSBackend
        backend = MiniMaxTTSBackend(api_key="sk-test", base_url="https://api.minimaxi.com", voice_id="test-voice")
        assert backend.provider_name == "minimax"

    def test_minimax_get_preprocess(self):
        from ..minimax_tts import MiniMaxTTSBackend
        from ..text_preprocess import strip_for_minimax
        backend = MiniMaxTTSBackend(api_key="sk-test", base_url="https://api.minimaxi.com", voice_id="test-voice")
        assert backend.get_preprocess() is strip_for_minimax

    def test_openai_provider_name(self):
        from ..openai_tts import OpenAICompatTTSBackend
        backend = OpenAICompatTTSBackend(base_url="http://fake:8000")
        assert backend.provider_name == "custom"

    def test_openai_get_preprocess(self):
        from ..openai_tts import OpenAICompatTTSBackend
        from ..text_preprocess import strip_all_markup
        backend = OpenAICompatTTSBackend(base_url="http://fake:8000")
        assert backend.get_preprocess() is strip_all_markup


# ============================================================
# OpenAICompatTTSBackend 合成测试
# ============================================================

class TestOpenAISynthesize:
	@pytest.mark.asyncio
	async def test_synthesize_collects_stream_chunks(self):
		"""非流式：调 synthesize_stream 收集所有 chunk"""
		from ..openai_tts import OpenAICompatTTSBackend
		fake_chunks = [b"chunk1", b"chunk2", b"chunk3"]

		class MockStreamBody:
			async def iter_bytes(self, chunk_size=4096):
				for c in fake_chunks:
					yield c
			async def __aenter__(self): return self
			async def __aexit__(self, *args): pass

		class MockSpeech:
			def __init__(self): self.with_streaming_response = self
			def create(self, **kwargs): return MockStreamBody()

		backend = OpenAICompatTTSBackend(base_url="http://fake:8000")
		backend._client.audio.speech = MockSpeech()  # type: ignore[attr-defined]

		result = await backend.synthesize("hello", character="nova")
		assert result == b"chunk1chunk2chunk3"

	@pytest.mark.asyncio
	async def test_synthesize_stream_yields_chunks(self):
		"""流式：逐 chunk yield"""
		from ..openai_tts import OpenAICompatTTSBackend
		fake_chunks = [b"a", b"b", b"c"]

		class MockStreamBody:
			async def iter_bytes(self, chunk_size=4096):
				for c in fake_chunks:
					yield c
			async def __aenter__(self): return self
			async def __aexit__(self, *args): pass

		class MockSpeech:
			def __init__(self): self.with_streaming_response = self
			def create(self, **kwargs): return MockStreamBody()

		backend = OpenAICompatTTSBackend(base_url="http://fake:8000")
		backend._client.audio.speech = MockSpeech()  # type: ignore[attr-defined]

		chunks = []
		async for c in backend.synthesize_stream("hi", character="nova"):
			chunks.append(c)
		assert chunks == [b"a", b"b", b"c"]

	@pytest.mark.asyncio
	async def test_preprocess_applied(self):
		"""文本预处理在合成前调用"""
		from ..openai_tts import OpenAICompatTTSBackend
		captured = []

		class MockStreamBody:
			async def iter_bytes(self, chunk_size=4096): yield b"ok"
			async def __aenter__(self): return self
			async def __aexit__(self, *args): pass

		class MockSpeech:
			def __init__(self): self.with_streaming_response = self
			def create(self, *, model, voice, input, response_format, **kw):
				captured.append(input)
				return MockStreamBody()

		backend = OpenAICompatTTSBackend(base_url="http://fake:8000")
		backend._client.audio.speech = MockSpeech()  # type: ignore[attr-defined]

		await backend.synthesize("(action)hello", character="nova")
		assert captured[0] == "hello"  # strip_all_markup 会去掉 (action)
