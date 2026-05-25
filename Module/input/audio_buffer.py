"""Audio buffer with RMS VAD for WebSocket audio streams.

Receives raw Int16 PCM chunks from browser mic, detects speech segments
via RMS amplitude threshold, and feeds complete WAV segments to STT.
"""

import asyncio
import logging
import struct
import time
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

Callback = Callable[[], Coroutine[Any, Any, None]]
TranscriptionCallback = Callable[[str], Coroutine[Any, Any, None]]


def pcm_to_wav(
    pcm_data: bytes,
    sample_rate: int = 16000,
    channels: int = 1,
    bits: int = 16,
) -> bytes:
    """Wrap raw PCM data with a WAV header. Pure Python, no dependencies."""
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    data_size = len(pcm_data)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE", b"fmt ",
        16,             # sub-chunk size (PCM)
        1,              # audio format (PCM=1)
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits,
        b"data",
        data_size,
    )
    return header + pcm_data


class AudioBuffer:
    """Buffers raw PCM audio chunks and detects speech segments via RMS VAD.

    When a speech segment ends (silence duration exceeded), accumulates PCM
    data, wraps it as WAV, and sends to STT. Results are delivered via callback.
    """

    # Minimum segment length in seconds (shorter segments are likely noise)
    MIN_SEGMENT_SECONDS = 0.3

    def __init__(
        self,
        *,
        silence_threshold: float = 0.02,
        silence_duration: float = 1.5,
        sample_rate: int = 16000,
        on_interrupt: Callback | None = None,
        on_transcription: TranscriptionCallback | None = None,
        stt_transcribe=None,  # async callable(bytes) -> str
    ):
        self._threshold = silence_threshold       # RMS fraction (0.0 - 1.0)
        self._silence_dur = silence_duration       # seconds of silence to end segment
        self._sample_rate = sample_rate
        self._on_interrupt = on_interrupt
        self._on_transcription = on_transcription
        self._stt_transcribe = stt_transcribe      # STT callable

        self._chunks: list[bytes] = []
        self._speaking: bool = False
        self._silence_start: float | None = None
        self._deafen_until: float = 0.0          # Ignore audio until this timestamp
        self._last_interrupt: float = 0.0        # Last interrupt time for cooldown
        self._interrupt_cooldown: float = 0.5    # Min seconds between interrupts

    def deafen(self, duration: float = 1.0) -> None:
        """Ignore audio for `duration` seconds. Call when TTS starts playing."""
        self._deafen_until = time.monotonic() + duration
        self._chunks.clear()
        self._speaking = False
        self._silence_start = None

    def _rms(self, chunk: bytes) -> float:
        """Compute RMS amplitude of Int16 PCM chunk, normalized to [0, 1]."""
        sample_count = len(chunk) // 2
        if sample_count == 0:
            return 0.0
        total = 0.0
        for i in range(0, len(chunk) - 1, 2):
            sample = struct.unpack("<h", chunk[i:i + 2])[0]
            total += sample * sample
        rms = (total / sample_count) ** 0.5
        return rms / 32768.0

    async def feed(self, chunk: bytes) -> None:
        """Feed a raw Int16 PCM chunk. Triggers STT when a segment completes."""
        # Ignore audio during deafen (TTS playback / echo avoidance)
        if time.monotonic() < self._deafen_until:
            return

        is_speech = self._rms(chunk) > self._threshold

        if is_speech:
            self._silence_start = None
            if not self._speaking:
                self._speaking = True
                # Interrupt cooldown — don't flood interrupts
                now = time.monotonic()
                if now - self._last_interrupt >= self._interrupt_cooldown:
                    self._last_interrupt = now
                    if self._on_interrupt:
                        await self._on_interrupt()
            self._chunks.append(chunk)
        elif self._speaking:
            now = time.monotonic()
            if self._silence_start is None:
                self._silence_start = now
                self._chunks.append(chunk)  # include trailing silence for STT
            elif now - self._silence_start >= self._silence_dur:
                await self._process_segment()

    async def flush(self) -> None:
        """Force-process any remaining buffered audio."""
        if self._chunks:
            await self._process_segment()

    async def _process_segment(self) -> None:
        audio = b"".join(self._chunks)
        self._chunks.clear()
        self._speaking = False
        self._silence_start = None

        min_bytes = int(self._sample_rate * self.MIN_SEGMENT_SECONDS * 2)
        if len(audio) < min_bytes:
            return  # too short, probably noise

        if self._stt_transcribe is None:
            logger.debug("No STT configured, dropping %d bytes of audio", len(audio))
            return

        wav = pcm_to_wav(audio, sample_rate=self._sample_rate)
        try:
            text = await asyncio.wait_for(
                self._stt_transcribe(wav),
                timeout=30.0,
            )
            if text and text.strip() and self._on_transcription:
                await self._on_transcription(text.strip())
        except asyncio.TimeoutError:
            logger.warning("STT timed out (segment length=%d bytes)", len(audio))
        except Exception:
            logger.warning("STT transcription failed", exc_info=True)

    def reset(self) -> None:
        """Clear all buffered state (e.g., on interruption)."""
        self._chunks.clear()
        self._speaking = False
        self._silence_start = None
