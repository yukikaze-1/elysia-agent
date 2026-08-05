import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from typing import cast

from Module.stt.openai_stt import OpenAICompatSTTBackend
from Module.stt.protocol import STTBackend
from Module.stt.registry import STTFactory
from Module.stt.sensevoice import SenseVoiceBackend

pytestmark = pytest.mark.service


# ============================================================
# Protocol — ABC constraints
# ============================================================

class TestSTTBackendProtocol:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            STTBackend()  # type: ignore[abstract]

    def test_missing_transcribe_raises(self):
        with pytest.raises(TypeError):

            class BadBackend1(STTBackend):
                async def transcribe_stream(self, audio, **kwargs):
                    yield ""

            BadBackend1()  # type: ignore[abstract]

    def test_missing_transcribe_stream_raises(self):
        with pytest.raises(TypeError):

            class BadBackend2(STTBackend):
                async def transcribe(self, audio, **kwargs) -> str:
                    return ""

            BadBackend2()  # type: ignore[abstract]

    def test_full_implementation_ok(self):
        class GoodBackend(STTBackend):
            async def transcribe(self, audio, **kwargs) -> str:
                return "ok"

            async def transcribe_stream(self, audio, **kwargs):
                yield "ok"

            async def test(self) -> dict:
                return {"ok": True, "latency_ms": 0}

            async def close(self) -> None:
                pass

            @property
            def provider_name(self) -> str:
                return "sensevoice"

        backend = GoodBackend()
        assert isinstance(backend, STTBackend)

    def test_model_composes_provider(self):
        """model 由 provider_name/_model 拼接，无 _model 时返回空串。"""
        class B(STTBackend):
            async def transcribe(self, audio, **kwargs): return ""
            async def transcribe_stream(self, audio, **kwargs): yield ""
            async def test(self): return {"ok": True, "latency_ms": 0}
            async def close(self): pass
            @property
            def provider_name(self): return "sensevoice"

        b = B()
        assert b.model == ""          # 无 _model → 空串
        b._model = "sense-voice"      # noqa: SLF001 -- 测试直设后备字段
        assert b.model == "sensevoice/sense-voice"


# ============================================================
# STTFactory
# ============================================================

class TestSTTFactory:
    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Invalid model_id"):
            STTFactory.create("sensevoice")

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            STTFactory.create("unknown/model")

    def test_openai_placeholder_raises(self):
        with pytest.raises(NotImplementedError):
            STTFactory.create("openai/whisper-1")

    def test_deepgram_placeholder_raises(self):
        with pytest.raises(NotImplementedError):
            STTFactory.create("deepgram/nova-2")

    def test_sensevoice_model_parsing(self):
        backend = cast(SenseVoiceBackend, STTFactory.create("sensevoice/sense-voice"))
        assert backend._model == "sense-voice"
        # model 拼 provider_name 得到完整 id，供 capabilities/前端下拉对齐
        assert backend.model == "sensevoice/sense-voice"

    def test_custom_base_url(self):
        backend = cast(SenseVoiceBackend, STTFactory.create(
            "sensevoice/sense-voice", base_url="http://192.168.1.18:20042"
        ))
        assert str(backend._client.base_url) == "http://192.168.1.18:20042"


# ============================================================
# SenseVoiceBackend — transcribe / transcribe_stream
# ============================================================

def _make_response(text: str):
    resp = MagicMock()
    resp.json.return_value = {"result": {"text": text}}
    resp.raise_for_status = MagicMock()
    return resp


@pytest.fixture
def backend(stt_backend):
    return stt_backend


class TestSenseVoiceTranscribe:
    @pytest.mark.asyncio
    async def test_transcribe_returns_text(self, backend):
        mock_post = AsyncMock(return_value=_make_response("你好世界"))
        with patch.object(backend._client, "post", mock_post):
            result = await backend.transcribe(b"fake-audio")
        assert result == "你好世界"

    @pytest.mark.asyncio
    async def test_transcribe_sends_audio_as_file(self, backend):
        mock_post = AsyncMock(return_value=_make_response("ok"))
        with patch.object(backend._client, "post", mock_post):
            await backend.transcribe(b"fake-audio")
        call_kwargs = mock_post.call_args.kwargs
        assert call_kwargs["files"] == {"file": b"fake-audio"}

    @pytest.mark.asyncio
    async def test_transcribe_empty_result(self, backend):
        resp = MagicMock()
        resp.json.return_value = {}
        resp.raise_for_status = MagicMock()
        mock_post = AsyncMock(return_value=resp)
        with patch.object(backend._client, "post", mock_post):
            result = await backend.transcribe(b"silence")
        assert result == ""

    @pytest.mark.asyncio
    async def test_transcribe_stream_yields_chunks(self, backend):
        mock_post = AsyncMock(return_value=_make_response("流式识别结果"))
        with patch.object(backend._client, "post", mock_post):
            chunks = []
            async for c in backend.transcribe_stream(b"fake-audio"):
                chunks.append(c)
        assert chunks == ["流式识别结果"]

    @pytest.mark.asyncio
    async def test_transcribe_calls_predict_endpoint(self, backend):
        mock_post = AsyncMock(return_value=_make_response("ok"))
        with patch.object(backend._client, "post", mock_post):
            await backend.transcribe(b"fake-audio")
        call_args = mock_post.call_args.args
        assert call_args[0] == "/predict/sentence"


# ============================================================
# close
# ============================================================

class TestClose:
    @pytest.mark.asyncio
    async def test_close_closes_client(self, backend):
        mock_close = AsyncMock()
        with patch.object(backend._client, "aclose", mock_close):
            await backend.close()
        mock_close.assert_called_once()


# ============================================================
# _parse_emotion / _parse_events
# ============================================================

class TestParseEmotion:
    def test_extracts_happy(self):
        from Module.stt.sensevoice import _parse_emotion
        assert _parse_emotion("<|zh|><|HAPPY|><|Speech|><|withitn|>hello") == "HAPPY"

    def test_extracts_sad(self):
        from Module.stt.sensevoice import _parse_emotion
        assert _parse_emotion("<|zh|><|SAD|><|Speech|>sorry") == "SAD"

    def test_neutral_tag_returns_neutral(self):
        from Module.stt.sensevoice import _parse_emotion
        assert _parse_emotion("<|zh|><|NEUTRAL|><|Speech|>hello") == "NEUTRAL"

    def test_emo_unknown_returns_neutral(self):
        from Module.stt.sensevoice import _parse_emotion
        assert _parse_emotion("<|zh|><|EMO_UNKNOWN|><|Speech|>hello") == "NEUTRAL"

    def test_no_emotion_tag_returns_neutral(self):
        from Module.stt.sensevoice import _parse_emotion
        assert _parse_emotion("<|zh|><|Speech|>hello") == "NEUTRAL"

    def test_empty_string(self):
        from Module.stt.sensevoice import _parse_emotion
        assert _parse_emotion("") == "NEUTRAL"


class TestParseEvents:
    def test_extracts_laughter(self):
        from Module.stt.sensevoice import _parse_events
        result = _parse_events("<|zh|><|NEUTRAL|><|Speech|><|Laughter|>hello")
        assert "Laughter" in result

    def test_speech_is_filtered_out(self):
        from Module.stt.sensevoice import _parse_events
        result = _parse_events("<|zh|><|Speech|>hello")
        assert "Speech" not in result

    def test_no_events_returns_empty_list(self):
        from Module.stt.sensevoice import _parse_events
        assert _parse_events("<|zh|><|NEUTRAL|>hello") == []

    def test_empty_string(self):
        from Module.stt.sensevoice import _parse_events
        assert _parse_events("") == []

    def test_multiple_events(self):
        from Module.stt.sensevoice import _parse_events
        result = _parse_events("<|Speech|><|Laughter|><|Applause|>text")
        assert "Laughter" in result
        assert "Applause" in result
        assert "Speech" not in result  # Speech is always filtered


# ============================================================
# SenseVoice — emotion/events state tracking + nospeech
# ============================================================

class TestSenseVoiceStateTracking:
    @pytest.mark.asyncio
    async def test_last_emotion_updated_after_transcribe(self, backend):
        resp = MagicMock()
        resp.json.return_value = {
            "result": {"text": "hello", "raw_text": "<|zh|><|HAPPY|><|Speech|>hello"}
        }
        resp.raise_for_status = MagicMock()
        mock_post = AsyncMock(return_value=resp)
        with patch.object(backend._client, "post", mock_post):
            await backend.transcribe(b"audio")
        assert backend.last_emotion == "HAPPY"

    @pytest.mark.asyncio
    async def test_last_events_updated_after_transcribe(self, backend):
        resp = MagicMock()
        resp.json.return_value = {
            "result": {"text": "hello", "raw_text": "<|zh|><|Speech|><|Laughter|>hello"}
        }
        resp.raise_for_status = MagicMock()
        mock_post = AsyncMock(return_value=resp)
        with patch.object(backend._client, "post", mock_post):
            await backend.transcribe(b"audio")
        assert "Laughter" in backend.last_events

    @pytest.mark.asyncio
    async def test_emotion_stays_when_no_raw_text(self, backend):
        """raw_text 为空时 last_emotion 保持旧值。"""
        backend.last_emotion = "HAPPY"
        resp = MagicMock()
        resp.json.return_value = {"result": {"text": "hello"}}
        resp.raise_for_status = MagicMock()
        mock_post = AsyncMock(return_value=resp)
        with patch.object(backend._client, "post", mock_post):
            await backend.transcribe(b"audio")
        assert backend.last_emotion == "HAPPY"

    @pytest.mark.asyncio
    async def test_nospeech_discards_text(self, backend):
        """<|nospeech|> 丢弃识别结果，返回空。"""
        resp = MagicMock()
        resp.json.return_value = {
            "result": {"text": "noise text", "raw_text": "<|zh|><|nospeech|>noise text"}
        }
        resp.raise_for_status = MagicMock()
        mock_post = AsyncMock(return_value=resp)
        with patch.object(backend._client, "post", mock_post):
            result = await backend.transcribe(b"silence")
        assert result == ""


# ============================================================
# SenseVoice — HTTP error handling
# ============================================================

class TestSenseVoiceErrors:
    @pytest.mark.asyncio
    async def test_request_error_wraps_to_runtime_error(self, backend):
        import httpx
        mock_post = AsyncMock(side_effect=httpx.RequestError("connection refused"))
        with patch.object(backend._client, "post", mock_post):
            with pytest.raises(RuntimeError, match="无法连接"):
                await backend.transcribe(b"audio")

    @pytest.mark.asyncio
    async def test_http_4xx_propagates(self, backend):
        import httpx
        resp = MagicMock()
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "bad request", request=MagicMock(), response=MagicMock(status_code=400)
        )
        mock_post = AsyncMock(return_value=resp)
        with patch.object(backend._client, "post", mock_post):
            with pytest.raises(httpx.HTTPStatusError):
                await backend.transcribe(b"audio")

    @pytest.mark.asyncio
    async def test_malformed_json_propagates(self, backend):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.side_effect = ValueError("not JSON")
        mock_post = AsyncMock(return_value=resp)
        with patch.object(backend._client, "post", mock_post):
            with pytest.raises(ValueError, match="not JSON"):
                await backend.transcribe(b"audio")


# ============================================================
# SenseVoice — edge cases
# ============================================================

class TestSenseVoiceEdges:
    @pytest.mark.asyncio
    async def test_result_without_text_key(self, backend):
        """result dict 无 'text' key — warning log — 返回空。"""
        resp = MagicMock()
        resp.json.return_value = {"result": {"raw_text": "<|zh|>hello"}}
        resp.raise_for_status = MagicMock()
        mock_post = AsyncMock(return_value=resp)
        with patch.object(backend._client, "post", mock_post):
            result = await backend.transcribe(b"audio")
        assert result == ""

    @pytest.mark.asyncio
    async def test_result_is_falsy(self, backend):
        """data.get('result') 是 falsy — warning log。"""
        resp = MagicMock()
        resp.json.return_value = {"result": None}
        resp.raise_for_status = MagicMock()
        mock_post = AsyncMock(return_value=resp)
        with patch.object(backend._client, "post", mock_post):
            result = await backend.transcribe(b"audio")
        assert result == ""

    @pytest.mark.asyncio
    async def test_kwargs_passed_through(self, backend):
        """验证 kwargs 透传到 _call_api。"""
        mock_post = AsyncMock(return_value=_make_response("ok"))
        with patch.object(backend._client, "post", mock_post):
            await backend.transcribe(b"audio", language="zh")
        # kwargs 目前被 _call_api 接受但不使用 — 至少不崩溃


# ============================================================
# http_client 注入（Task 5 批次 5 — custom 出站守卫挂载点）
# ============================================================

class TestHTTPClientInjection:
    """OpenAICompatSTTBackend http_client 注入挂载点。"""

    def test_injected_http_client_used(self):
        """注入 http_client → 后端 client 使用该 client。"""
        guard = httpx.AsyncClient()
        try:
            backend = OpenAICompatSTTBackend(
                model="whisper-1",
                api_key="sk-test",
                base_url="https://guard.example.com/v1",
                http_client=guard,
            )
            assert backend._injected_client is guard
            assert backend._client._client is guard
        finally:
            asyncio.run(backend.close())
            asyncio.run(guard.aclose())

    def test_no_http_client_self_builds(self):
        """不传 http_client → 后端自建（现状不破坏）。"""
        with patch("Module.stt.openai_stt.AsyncOpenAI", return_value=MagicMock()) as mock_cls:
            OpenAICompatSTTBackend(
                model="whisper-1",
                api_key="sk-test",
                base_url="https://guard.example.com/v1",
            )
        assert "http_client" not in mock_cls.call_args.kwargs

    def test_factory_passes_http_client_to_custom_backend(self):
        """STTFactory.create("custom/x", ..., http_client=guard) → 后端收到。"""
        guard = httpx.AsyncClient()
        try:
            backend = STTFactory.create(
                "custom/whisper-1",
                base_url="https://guard.example.com/v1",
                api_key="sk-test",
                http_client=guard,
            )
            assert backend._injected_client is guard
            assert backend._client._client is guard
        finally:
            asyncio.run(backend.close())
            asyncio.run(guard.aclose())
