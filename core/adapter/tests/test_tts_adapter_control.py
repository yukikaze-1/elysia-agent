"""TTSStreamManager 播放控制（BE-063）。"""
import asyncio
import struct
import time

import pytest

from core.adapter.tts_adapter import TTSStreamManager, _wav_info

pytestmark = pytest.mark.unit


def test_wav_info_parses():
    sample_rate = 8000
    data_size = 1600
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE", b"fmt ", 16, 1, 1,
        sample_rate, sample_rate * 2, 2, 16, b"data", data_size,
    )
    sr, dur = _wav_info(header + b"\x00" * data_size)
    assert sr == 8000
    assert dur == int(data_size * 1000 / (sample_rate * 2))  # 100ms


class FakeOut:
    def __init__(self):
        self.calls = []
        self.chunk_gate = None  # asyncio.Event 置位后 audio_chunk 阻塞——测试可停在转发段（NEW-1）
    async def _broadcast(self, method, *a, **kw):
        self.calls.append((method, a, kw))
    async def send(self, *a, **kw):
        pass
    async def audio_chunk(self, chunk, *, stream_id=0, **kw):
        self.calls.append(("audio_chunk", stream_id, len(chunk)))
        if self.chunk_gate is not None:
            await self.chunk_gate.wait()
    async def audio_end(self, stream_id=0, *, format="wav", **kw):
        self.calls.append(("audio_end", stream_id))
    async def audio_start(self, stream_id, *, format="wav", sample_rate=None, duration_ms=None, **kw):
        self.calls.append(("audio_start", stream_id, sample_rate, duration_ms))
    async def audio_cancelled(self, stream_id, **kw):
        self.calls.append(("audio_cancelled", stream_id))


class FakeTTS:
    stream_format = "wav"
    def get_preprocess(self):
        return None
    async def synthesize(self, text, *, character=None, emotion=None, voice_id=None):
        header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + 3200, b"WAVE",
                             b"fmt ", 16, 1, 1, 32000, 64000, 2, 16, b"data", 3200)
        return header + b"\x00" * 3200


async def _wait_start(out):
    deadline = time.monotonic() + 3
    while not any(c[0] == "audio_start" for c in out.calls) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_stream_sentence_emits_audio_start():
    out = FakeOut()
    mgr = TTSStreamManager(tts=FakeTTS(), output=out, character_name="elysia")
    mgr.reset_for_turn()  # 预置 seq=1 门控（否则 _download_and_forward 卡 wait）
    mgr.stream_sentence("你好")
    await mgr.wait_all()
    assert any(c[0] == "audio_start" for c in out.calls)
    start = [c for c in out.calls if c[0] == "audio_start"][0]
    assert start[2] == 32000  # sample_rate


@pytest.mark.asyncio
async def test_cancel_stream_emits_audio_cancelled():
    out = FakeOut()
    out.chunk_gate = asyncio.Event()  # 阻塞转发段，确定性停在「已 audio_start、仍在转发」（NEW-1）
    mgr = TTSStreamManager(tts=FakeTTS(), output=out, character_name="elysia")
    mgr.reset_for_turn()
    mgr.stream_sentence("一句话")
    await _wait_start(out)  # 等 audio_start（任务已堵在 chunk_gate）
    ok = mgr.cancel_stream(1)  # sid=1
    out.chunk_gate.set()  # 释放被堵任务（cancel 本身不依赖 gate，仅清理）
    await asyncio.sleep(0.05)
    assert ok is True
    assert any(c[0] == "audio_cancelled" for c in out.calls)


@pytest.mark.asyncio
async def test_cancel_all_emits_audio_cancelled():
    """cancel_all 补 audio_cancelled（修孤儿 chunk，spec §12/§8.2）。"""
    out = FakeOut()
    out.chunk_gate = asyncio.Event()
    mgr = TTSStreamManager(tts=FakeTTS(), output=out, character_name="elysia")
    mgr.reset_for_turn()
    mgr.stream_sentence("一句话")
    await _wait_start(out)
    mgr.cancel_all()
    out.chunk_gate.set()
    await asyncio.sleep(0.05)
    assert any(c[0] == "audio_cancelled" for c in out.calls)


def test_cancel_unknown_returns_false():
    mgr = TTSStreamManager(tts=None, output=FakeOut(), character_name="elysia")
    assert mgr.cancel_stream(999) is False
