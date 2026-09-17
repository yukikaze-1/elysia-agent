"""TTS 播放控制输出层测试（BE-063）。"""
import base64
import pytest

from Module.output import WebSocketChannel

pytestmark = pytest.mark.unit


class FakeWs:
    def __init__(self):
        self.sent = []
    async def send_json(self, data):
        self.sent.append(data)


def test_audio_start_wire():
    ws = FakeWs()
    ch = WebSocketChannel(ws)
    import asyncio
    asyncio.run(ch.on_audio_start(5, format="wav", sample_rate=32000, duration_ms=1000))
    frame = ws.sent[-1]
    assert frame["type"] == "audio_start"
    assert frame["stream_id"] == 5 and frame["sampleRate"] == 32000


def test_audio_start_wire_with_character_identity():
    ws = FakeWs()
    ch = WebSocketChannel(ws, protocol_version=2)
    import asyncio
    asyncio.run(ch.on_audio_start(
        5, format="wav", sample_rate=32000, duration_ms=1000,
        conversation_id="conv_1", generation_id="gen_1",
        character_id="chr_elysia", character_instance_id="chi_1",
        character_revision=3,
    ))
    frame = ws.sent[-1]
    assert frame["conversationId"] == "conv_1"
    assert frame["generationId"] == "gen_1"
    assert frame["characterId"] == "chr_elysia"
    assert frame["characterInstanceId"] == "chi_1"
    assert frame["characterRevision"] == 3


def test_audio_cancelled_pops_seq():
    ws = FakeWs()
    ch = WebSocketChannel(ws)
    import asyncio
    asyncio.run(ch.on_audio_chunk(b"\x00" * 100, stream_id=7))
    assert ch._audio_seq.get(7) == 1
    asyncio.run(ch.on_audio_cancelled(7))
    assert 7 not in ch._audio_seq
    assert ws.sent[-1]["type"] == "audio_cancelled"


def test_audio_chunk_uses_audio_message():
    ws = FakeWs()
    ch = WebSocketChannel(ws)
    import asyncio
    asyncio.run(ch.on_audio_chunk(b"\x01\x02", stream_id=3))
    frame = ws.sent[-1]
    assert frame["type"] == "audio"
    assert frame["is_final"] is False
    assert frame["data"] == base64.b64encode(b"\x01\x02").decode("ascii")
