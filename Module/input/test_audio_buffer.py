"""AudioBuffer and pcm_to_wav unit tests.

Run: python -m pytest Module/input/test_audio_buffer.py -v --asyncio-mode=auto
"""

import asyncio
import struct
import wave
from io import BytesIO

import pytest
from unittest.mock import patch

from .audio_buffer import AudioBuffer, pcm_to_wav, _FRAME_BYTES


def _loud(samples: int) -> bytes:
    return struct.pack(f"<{samples}h", *([20000] * samples))


def _sil(samples: int) -> bytes:
    return struct.pack(f"<{samples}h", *([5] * samples))


class TestPcmToWav:
    def test_header_structure(self):
        """WAV header should have RIFF, WAVE, fmt, data chunks with correct sizes."""
        pcm = b"\x00\x00" * 160  # 160 samples (~10ms at 16kHz)
        wav_data = pcm_to_wav(pcm, sample_rate=16000, channels=1, bits=16)

        # Verify RIFF header
        assert wav_data[:4] == b"RIFF"
        riff_size = struct.unpack("<I", wav_data[4:8])[0]
        assert riff_size == len(wav_data) - 8
        assert wav_data[8:12] == b"WAVE"
        assert wav_data[12:16] == b"fmt "

    def test_roundtrip_with_wave_module(self):
        """Generated WAV should be readable by Python's wave module."""
        pcm = b"\x01\x00\x02\x00\xff\xff\xfe\xff" * 100  # some audio data
        wav_data = pcm_to_wav(pcm, sample_rate=16000, channels=1, bits=16)

        with wave.open(BytesIO(wav_data), "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 16000
            read_pcm = wf.readframes(wf.getnframes())
            assert read_pcm == pcm

    def test_custom_params(self):
        """WAV header should reflect custom sample_rate, channels, bits."""
        pcm = b"\x00\x00" * 100
        wav_data = pcm_to_wav(pcm, sample_rate=44100, channels=2, bits=8)

        with wave.open(BytesIO(wav_data), "rb") as wf:
            assert wf.getnchannels() == 2
            assert wf.getsampwidth() == 1  # 8 bits = 1 byte
            assert wf.getframerate() == 44100

    def test_empty_pcm(self):
        """Empty PCM should produce a valid WAV with zero data size."""
        wav_data = pcm_to_wav(b"", sample_rate=16000, channels=1, bits=16)
        with wave.open(BytesIO(wav_data), "rb") as wf:
            assert wf.getnframes() == 0


class TestAudioBuffer:
    @pytest.fixture
    def make_silence(self):
        """Generate N samples of near-silence PCM."""
        def _make(count: int) -> bytes:
            return struct.pack(f"<{count}h", *([5] * count))
        return _make

    @pytest.fixture
    def make_loud(self):
        """Generate N samples of loud PCM."""
        def _make(count: int) -> bytes:
            return struct.pack(f"<{count}h", *([20000] * count))
        return _make

    def test_vad_silence(self, make_silence):
        """Near-silence PCM should be classified as non-speech by VAD."""
        buf = AudioBuffer()
        chunk = make_silence(_FRAME_BYTES // 2)  # one 20ms frame at 16kHz
        assert buf._is_speech(chunk) is False

    def test_vad_loud(self, make_loud):
        """Loud PCM should be classified as speech by VAD."""
        buf = AudioBuffer()
        chunk = make_loud(_FRAME_BYTES // 2)  # one 20ms frame at 16kHz
        assert buf._is_speech(chunk) is True

    @pytest.mark.asyncio
    async def test_feed_loud_then_silence_triggers_stt(self, make_loud, make_silence):
        stt_results: list[str] = []
        trans_calls: list[str] = []

        async def mock_stt(wav: bytes) -> str:
            stt_results.append(wav[:4].hex())  # record WAV header
            return "测试语音识别"

        async def on_transcription(text: str):
            trans_calls.append(text)

        buf = AudioBuffer(
            stt_transcribe=mock_stt,
            on_transcription=on_transcription,
            silence_duration=0.1,
        )

        # Feed speech (loud) then silence — should trigger STT
        for _ in range(30):
            await buf.feed(make_loud(1600))  # 100ms per chunk = 3s total
        for _ in range(3):
            await buf.feed(make_silence(1600))  # start silence
        await asyncio.sleep(0.15)  # let silence_duration (0.1s) elapse
        await buf.feed(make_silence(1600))  # this should trigger

        assert len(stt_results) == 1
        assert stt_results[0].startswith("5249")  # "RI" in hex
        assert trans_calls == ["测试语音识别"]

    @pytest.mark.asyncio
    async def test_short_segment_ignored(self, make_loud):
        stt_calls: list[bytes] = []

        async def mock_stt(wav: bytes) -> str:
            stt_calls.append(wav)
            return ""

        buf = AudioBuffer(
            stt_transcribe=mock_stt,
            silence_duration=0.1,
        )

        # Feed very short speech segment (< 0.3s)
        await buf.feed(make_loud(1600))   # 100ms
        await buf.feed(make_loud(1600))   # 200ms
        await buf.flush()

        assert len(stt_calls) == 0  # too short, should be ignored

    @pytest.mark.asyncio
    async def test_reset_clears_buffer(self, make_loud, make_silence):
        stt_calls: list[bytes] = []

        async def mock_stt(wav: bytes) -> str:
            stt_calls.append(wav)
            return ""

        buf = AudioBuffer(stt_transcribe=mock_stt)
        await buf.feed(make_loud(16000))  # 1s of speech
        buf.reset()
        await buf.flush()
        assert len(stt_calls) == 0

    @pytest.mark.asyncio
    async def test_no_stt_callable(self, make_loud, make_silence):
        """When stt_transcribe is None, feed should not crash."""
        buf = AudioBuffer(stt_transcribe=None, silence_duration=0.1)
        for _ in range(20):
            await buf.feed(make_loud(1600))
        for _ in range(5):
            await buf.feed(make_silence(1600))
        await buf.flush()
        # No crash is success

    @pytest.mark.asyncio
    async def test_on_interrupt_called(self, make_loud):
        interrupt_calls: list[bool] = []

        async def on_interrupt():
            interrupt_calls.append(True)

        buf = AudioBuffer(on_interrupt=on_interrupt)
        # Interrupt fires after MIN_SPEECH_CHUNKS_FOR_INTERRUPT=3 consecutive speech chunks
        for _ in range(3):
            await buf.feed(make_loud(1600))

        assert len(interrupt_calls) == 1  # 3rd loud chunk triggers interrupt

    @pytest.mark.asyncio
    async def test_interrupt_only_once_per_segment(self, make_loud):
        interrupt_calls: list[bool] = []

        async def on_interrupt():
            interrupt_calls.append(True)

        buf = AudioBuffer(on_interrupt=on_interrupt)
        for _ in range(8):
            await buf.feed(make_loud(1600))

        assert len(interrupt_calls) == 1  # fires once, not on every chunk beyond 3

    @pytest.mark.asyncio
    async def test_deafen_ignores_audio(self, make_loud):
        """deafen() makes feed() a no-op for the specified duration."""
        stt_calls: list[bytes] = []

        async def fake_stt(audio: bytes) -> str:
            stt_calls.append(audio)
            return "test"

        buf = AudioBuffer(stt_transcribe=fake_stt)
        buf.deafen(1.0)
        await buf.feed(make_loud(16000))  # full second of loud audio
        await buf.flush()
        assert len(stt_calls) == 0  # all audio ignored during deafen

    @pytest.mark.asyncio
    async def test_deafen_clears_state(self, make_loud):
        """deafen() resets speaking state."""
        buf = AudioBuffer()
        # Feed some audio to get into speaking state
        for _ in range(5):
            await buf.feed(make_loud(640))
        buf.deafen(0.1)
        # State should be cleared
        assert buf._speaking is False
        assert buf._chunks == []

    @pytest.mark.asyncio
    async def test_flush_processes_remaining(self, make_loud):
        """flush() processes buffered audio even without silence gap."""
        stt_calls: list[str] = []

        async def fake_stt(audio: bytes) -> str:
            stt_calls.append("called")
            return "transcribed text"

        buf = AudioBuffer(stt_transcribe=fake_stt)
        # Feed enough to pass min segment length
        for _ in range(30):
            await buf.feed(make_loud(640))
        # Don't wait for silence — flush immediately
        await buf.flush()
        assert "called" in stt_calls


# ============================================================
# VAD frame processing edges (using module-level _loud/_sil)
# ============================================================

class TestVADFrameEdges:
    def test_multi_frame_chunk_all_speech(self):
        buf = AudioBuffer()
        chunk = _loud(640)  # 640 samples × 2 = 1280 bytes = 2 frames
        assert buf._is_speech(chunk) is True

    def test_chunk_with_trailing_bytes(self):
        buf = AudioBuffer()
        chunk = b"\x00" * 700  # 640 processed, 60 dropped
        result = buf._is_speech(chunk)
        assert isinstance(result, bool)

    def test_vad_exception_is_suppressed(self):
        import webrtcvad
        buf = AudioBuffer()
        loud = _loud(320)
        with patch.object(webrtcvad.Vad, "is_speech", side_effect=RuntimeError("vad error")):
            result = buf._is_speech(loud)
        assert isinstance(result, bool)

    def test_chunk_exactly_one_frame(self):
        buf = AudioBuffer()
        loud = _loud(320)  # 320 samples = 640 bytes = exactly 1 frame
        result = buf._is_speech(loud)
        assert isinstance(result, bool)


# ============================================================
# Interrupt cooldown
# ============================================================

class TestInterruptCooldown:
    @pytest.mark.asyncio
    async def test_cooldown_prevents_immediate_second_interrupt(self):
        interrupt_count = 0

        async def on_interrupt():
            nonlocal interrupt_count
            interrupt_count += 1

        buf = AudioBuffer(on_interrupt=on_interrupt)
        for _ in range(4):
            await buf.feed(_loud(320))
        assert interrupt_count == 1

        buf.reset()
        for _ in range(4):
            await buf.feed(_loud(320))
        assert interrupt_count == 1  # cooldown active

    @pytest.mark.asyncio
    async def test_interrupt_after_cooldown_expires(self):
        interrupt_count = 0

        async def on_interrupt():
            nonlocal interrupt_count
            interrupt_count += 1

        buf = AudioBuffer(on_interrupt=on_interrupt)
        buf._interrupt_cooldown = 0.01

        for _ in range(4):
            await buf.feed(_loud(320))
        assert interrupt_count == 1

        buf.reset()
        await asyncio.sleep(0.02)

        for _ in range(4):
            await buf.feed(_loud(320))
        assert interrupt_count == 2


# ============================================================
# STT timeout / exception
# ============================================================

class TestSTTErrors:
    @pytest.mark.asyncio
    async def test_stt_timeout_does_not_crash(self):
        import logging

        async def slow_stt(audio: bytes) -> str:
            await asyncio.sleep(99)
            return "late"

        buf = AudioBuffer(stt_transcribe=slow_stt, silence_duration=0.05)
        buf._logger = logging.getLogger("test_audio")
        buf._logger.setLevel(logging.WARNING)

        for _ in range(30):
            await buf.feed(_loud(320))
        for _ in range(10):
            await buf.feed(_sil(320))
        # Should not crash despite timeout

    @pytest.mark.asyncio
    async def test_stt_exception_does_not_crash(self):
        import logging

        async def failing_stt(audio: bytes) -> str:
            raise RuntimeError("STT down")

        buf = AudioBuffer(stt_transcribe=failing_stt, silence_duration=0.05)
        buf._logger = logging.getLogger("test_audio")
        buf._logger.setLevel(logging.WARNING)

        for _ in range(30):
            await buf.feed(_loud(320))
        for _ in range(10):
            await buf.feed(_sil(320))


# ============================================================
# on_transcription=None
# ============================================================

class TestOnTranscriptionNone:
    @pytest.mark.asyncio
    async def test_no_callback_does_not_crash(self):
        stt_called = False

        async def fake_stt(audio: bytes) -> str:
            nonlocal stt_called
            stt_called = True
            return "result"

        buf = AudioBuffer(stt_transcribe=fake_stt, on_transcription=None, silence_duration=0.05)
        for _ in range(30):
            await buf.feed(_loud(320))
        for _ in range(10):
            await buf.feed(_sil(320))


# ============================================================
# reset() preserves _deafen_until
# ============================================================

class TestResetDeafen:
    def test_reset_preserves_deafen_until(self):
        buf = AudioBuffer()
        buf.deafen(5.0)
        deafen_before = buf._deafen_until
        buf.reset()
        assert buf._deafen_until == deafen_before
        assert buf._deafen_until > 0
