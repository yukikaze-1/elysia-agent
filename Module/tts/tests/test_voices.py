"""批次 5（Task 9）TTS 协议扩展测试：list_voices + synthesize voice_id（marker=service，mock 不触网）。

覆盖：
- 协议缺省 `list_voices()` == `[]`（TTSBackend 具体方法默认返回空列表）；
- MiniMax：POST /v1/get_voice 解析 data.voice_items → [{id, name}]；实例内进程缓存（二次不重复 POST）；
  API 错 → []；synthesize voice_id 非空跳过 _resolve_voice（None 走 map 现状）；
- gpt-sovits：list_voices id 集 == self._voices keys；synthesize voice_id 非空 → 直接按 voice_id 查 _voices；
- openai_tts：静态集 alloy/echo/fable/onyx/nova/shimmer（id 唯一）；synthesize voice_id → synthesize_stream 的 voice 参数。
"""
import pytest

from Module.tts.protocol import TTSBackend

pytestmark = pytest.mark.service


# ============================================================
# 协议缺省
# ============================================================

class TestProtocolListVoicesDefault:
    @pytest.mark.asyncio
    async def test_default_list_voices_empty(self):
        """TTSBackend.list_voices() 具体方法默认返回 []。"""
        class Backend(TTSBackend):
            @property
            def provider_name(self) -> str:
                return "test"

            async def synthesize(
                self, text: str, *, character: str, emotion: str | None = None,
                voice_id: str | None = None,
            ) -> bytes:
                return b""

            async def synthesize_stream(
                self, text: str, *, character: str, emotion: str | None = None,
                voice_id: str | None = None,
            ):
                yield b""

            async def test(self) -> dict:
                return {"ok": True, "latency_ms": 0}

            async def close(self) -> None:
                pass

        backend = Backend()
        assert await backend.list_voices() == []


# ============================================================
# MiniMax（MiniMax 语音）
# ============================================================

def _make_minimax_resp(*items: dict) -> object:
    """构造 MiniMax /v1/get_voice 成功响应（base_resp 0 + data.voice_items）。"""
    class MockResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "base_resp": {"status_code": 0, "status_msg": "success"},
                "data": {"voice_items": list(items)},
            }

    return MockResp()


class TestMiniMaxListVoices:
    @pytest.mark.asyncio
    async def test_parses_voice_items(self):
        """mock POST 返回 voice_items → [{id, name}]。"""
        from Module.tts.minimax_tts import MiniMaxTTSBackend
        backend = MiniMaxTTSBackend(
            api_key="sk-test", base_url="https://api.minimaxi.com", voice_id="test-voice",
        )

        async def fake_post(url, *, json, **kw):
            assert json == {"voice_type": "system"}
            return _make_minimax_resp(
                {"voice_id": "v1", "voice_name": "音色一"},
                {"voice_id": "v2", "voice_name": "音色二"},
            )

        backend._client.post = fake_post  # type: ignore[assignment]
        voices = await backend.list_voices()
        assert voices == [{"id": "v1", "name": "音色一"}, {"id": "v2", "name": "音色二"}]

    @pytest.mark.asyncio
    async def test_cache_no_second_post(self):
        """二次调用不重复 POST（实例内进程缓存）。"""
        from Module.tts.minimax_tts import MiniMaxTTSBackend
        backend = MiniMaxTTSBackend(
            api_key="sk-test", base_url="https://api.minimaxi.com", voice_id="test-voice",
        )
        post_count = 0

        async def fake_post(url, *, json, **kw):
            nonlocal post_count
            post_count += 1
            return _make_minimax_resp({"voice_id": "v1", "voice_name": "音色一"})

        backend._client.post = fake_post  # type: ignore[assignment]
        first = await backend.list_voices()
        second = await backend.list_voices()
        assert first == [{"id": "v1", "name": "音色一"}]
        assert second == [{"id": "v1", "name": "音色一"}]
        assert post_count == 1

    @pytest.mark.asyncio
    async def test_api_error_returns_empty(self):
        """API 失败 → []（不抛）。"""
        from Module.tts.minimax_tts import MiniMaxTTSBackend
        backend = MiniMaxTTSBackend(
            api_key="sk-test", base_url="https://api.minimaxi.com", voice_id="test-voice",
        )

        async def fake_post(url, *, json, **kw):
            raise RuntimeError("connection refused")

        backend._client.post = fake_post  # type: ignore[assignment]
        assert await backend.list_voices() == []


class TestMiniMaxSynthesizeVoiceId:
    @pytest.mark.asyncio
    async def test_voice_id_skips_voice_map(self):
        """voice_id 非空 → payload voice_setting.voice_id == voice_id（跳过 map）。"""
        from Module.tts.minimax_tts import MiniMaxTTSBackend
        captured = {}

        class MockResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"base_resp": {"status_code": 0}, "data": {"audio": "00ff"}}

        async def fake_post(url, *, json, **kw):
            captured["voice_id"] = json["voice_setting"]["voice_id"]
            return MockResp()

        backend = MiniMaxTTSBackend(
            api_key="sk-test", base_url="https://api.minimaxi.com",
            voice_id="default", voice_map={"Elysia": "female-elysia-01"},
        )
        backend._client.post = fake_post  # type: ignore[assignment]
        await backend.synthesize("hi", character="Elysia", voice_id="v2")
        assert captured["voice_id"] == "v2"

    @pytest.mark.asyncio
    async def test_no_voice_id_uses_voice_map(self):
        """voice_id None → 走 map（现状不破坏）。"""
        from Module.tts.minimax_tts import MiniMaxTTSBackend
        captured = {}

        class MockResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"base_resp": {"status_code": 0}, "data": {"audio": "00ff"}}

        async def fake_post(url, *, json, **kw):
            captured["voice_id"] = json["voice_setting"]["voice_id"]
            return MockResp()

        backend = MiniMaxTTSBackend(
            api_key="sk-test", base_url="https://api.minimaxi.com",
            voice_id="default", voice_map={"Elysia": "female-elysia-01"},
        )
        backend._client.post = fake_post  # type: ignore[assignment]
        await backend.synthesize("hi", character="Elysia")
        assert captured["voice_id"] == "female-elysia-01"


# ============================================================
# gpt-sovits（GPT-SoVITS 语音）
# ============================================================

class TestGPTSoVitsListVoices:
    @pytest.mark.asyncio
    async def test_ids_equal_voices_keys(self, tts_backend):
        """list_voices 的 id 集 == self._voices（已加载 gpt_sovits_voices.yaml）keys。"""
        voices = await tts_backend.list_voices()
        ids = [v["id"] for v in voices]
        assert set(ids) == set(tts_backend._voices.keys())
        assert len(ids) == len(tts_backend._voices)

    @pytest.mark.asyncio
    async def test_synthesize_voice_id_looks_up_directly(self, tts_backend):
        """synthesize voice_id 非空 → _call_api 直接按 voice_id 查 _voices（跳过 character 默认）。"""
        captured = {}

        async def fake_call_api(text, character, emotion, streaming, voice_id=None):
            captured["voice_id"] = voice_id
            captured["character"] = character
            yield b"ok"

        tts_backend._call_api = fake_call_api  # type: ignore[assignment]
        result = await tts_backend.synthesize("hi", character="UnknownChar", voice_id="Elysia")
        assert result == b"ok"
        assert captured["voice_id"] == "Elysia"
        # character 仍传入（仅供记录），但 voice_id 非空时按 voice_id 查表
        assert captured["character"] == "UnknownChar"


# ============================================================
# openai_tts（OpenAI TTS 语音）
# ============================================================

class TestOpenAICompatListVoices:
    @pytest.mark.asyncio
    async def test_static_set_unique_ids(self):
        """静态集 alloy/echo/fable/onyx/nova/shimmer，id 唯一。"""
        from Module.tts.openai_tts import OpenAICompatTTSBackend
        backend = OpenAICompatTTSBackend(base_url="http://fake:8000")
        voices = await backend.list_voices()
        ids = [v["id"] for v in voices]
        assert set(ids) == {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}
        assert len(ids) == len(set(ids))

    @pytest.mark.asyncio
    async def test_synthesize_voice_id_flows_to_stream(self):
        """synthesize voice_id 非空 → 流进 synthesize_stream 的 voice 参数。"""
        from Module.tts.openai_tts import OpenAICompatTTSBackend
        captured = {}

        class MockStreamBody:
            async def iter_bytes(self, chunk_size=4096):
                yield b"ok"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class MockSpeech:
            def __init__(self):
                self.with_streaming_response = self

            def create(self, *, model, voice, input, response_format, **kw):
                captured["voice"] = voice
                return MockStreamBody()

        backend = OpenAICompatTTSBackend(base_url="http://fake:8000")
        backend._client.audio.speech = MockSpeech()  # type: ignore[attr-defined]
        result = await backend.synthesize("hello", character="nova", voice_id="echo")
        assert result == b"ok"
        assert captured["voice"] == "echo"

    @pytest.mark.asyncio
    async def test_no_voice_id_uses_character(self):
        """voice_id None → synthesize_stream 的 voice == character（现状）。"""
        from Module.tts.openai_tts import OpenAICompatTTSBackend
        captured = {}

        class MockStreamBody:
            async def iter_bytes(self, chunk_size=4096):
                yield b"ok"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class MockSpeech:
            def __init__(self):
                self.with_streaming_response = self

            def create(self, *, model, voice, input, response_format, **kw):
                captured["voice"] = voice
                return MockStreamBody()

        backend = OpenAICompatTTSBackend(base_url="http://fake:8000")
        backend._client.audio.speech = MockSpeech()  # type: ignore[attr-defined]
        await backend.synthesize("hello", character="nova")
        assert captured["voice"] == "nova"
