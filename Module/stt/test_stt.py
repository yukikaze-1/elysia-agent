from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from typing import cast

from .protocol import STTBackend
from .registry import STTFactory
from .sensevoice import SenseVoiceBackend


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

            async def close(self) -> None:
                pass

        backend = GoodBackend()
        assert isinstance(backend, STTBackend)


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
def backend():
    return STTFactory.create("sensevoice/sense-voice")


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
