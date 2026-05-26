"""Audio buffer with WebRTC VAD for WebSocket audio streams.

Receives raw Int16 PCM chunks from browser mic, detects speech segments
via WebRTC VAD (voice activity detection based on Gaussian Mixture Model),
and feeds complete WAV segments to STT.
"""

import asyncio
import logging
import struct
import time
from typing import Callable, Coroutine

import webrtcvad

logger = logging.getLogger(__name__)

Callback = Callable[[], Coroutine[object, object, None]]
TranscriptionCallback = Callable[[str], Coroutine[object, object, None]]

# WebRTC VAD frame sizes (16kHz mono 16-bit).
# 20ms = 320 samples × 2 bytes = 640 bytes.
_FRAME_MS = 20
_FRAME_SAMPLES = int(16000 * _FRAME_MS / 1000)   # 320
_FRAME_BYTES = _FRAME_SAMPLES * 2                  # 640


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
    """Buffers raw PCM audio chunks and detects speech segments via WebRTC VAD.

    Uses a Gaussian Mixture Model to distinguish speech from noise (keyboard,
    fan, background music), unlike RMS which only looks at energy level.

    When a speech segment ends (silence duration exceeded), accumulates PCM
    data, wraps it as WAV, and sends to STT. Results are delivered via callback.
    """

    # Minimum segment length in seconds (shorter segments are likely noise)
    MIN_SEGMENT_SECONDS = 0.3
    # Consecutive speech chunks required before firing interrupt (avoids
    # single-frame false positives from background noise).
    MIN_SPEECH_CHUNKS_FOR_INTERRUPT = 3

    def __init__(
        self,
        *,
        aggressiveness: int = 3,
        silence_duration: float = 1.5,
        sample_rate: int = 16000,
        on_interrupt: Callback | None = None,
        on_transcription: TranscriptionCallback | None = None,
        stt_transcribe=None,  # async callable(bytes) -> str
    ):
        # WebRTC VAD aggressiveness: 0 (least) to 3 (most).
        # 3 is best for desktop companion — aggressively filters non-speech.
        self._aggressiveness = aggressiveness
        self._silence_dur = silence_duration       # seconds of silence to end segment
        self._sample_rate = sample_rate
        self._on_interrupt = on_interrupt
        self._on_transcription = on_transcription
        self._stt_transcribe = stt_transcribe      # STT callable

        self._chunks: list[bytes] = []
        self._speaking: bool = False
        self._speech_chunk_count: int = 0           # consecutive speech chunks
        self._interrupt_fired: bool = False         # interrupt already sent for this segment
        self._silence_start: float | None = None
        self._deafen_until: float = 0.0            # Ignore audio until this timestamp
        self._last_interrupt: float = 0.0          # Last interrupt time for cooldown
        self._interrupt_cooldown: float = 0.5      # Min seconds between interrupts

    def deafen(self, duration: float = 1.0) -> None:
        """Ignore audio for `duration` seconds. Call when TTS starts playing."""
        self._deafen_until = time.monotonic() + duration
        self._chunks.clear()
        self._speaking = False
        self._speech_chunk_count = 0
        self._interrupt_fired = False
        self._silence_start = None

    def _is_speech(self, chunk: bytes) -> bool:
        """Check if a PCM chunk contains speech using WebRTC VAD.

        Splits the chunk into 20ms frames and checks each frame.
        Creates a fresh VAD instance per call to avoid state drift —
        WebRTC VAD accumulates a noise estimate that can cause false
        negatives after processing many chunks.
        """
        if len(chunk) < _FRAME_BYTES:
            return False

        # Fresh VAD per call — avoids noise-estimate drift
        vad = webrtcvad.Vad(self._aggressiveness)
        speech_frames = 0
        total_frames = 0
        for offset in range(0, len(chunk) - _FRAME_BYTES + 1, _FRAME_BYTES):
            frame = chunk[offset:offset + _FRAME_BYTES]
            try:
                if vad.is_speech(frame, self._sample_rate):
                    speech_frames += 1
            except Exception:
                pass
            total_frames += 1

        if total_frames == 0:
            return False

        return speech_frames > total_frames * 0.5

    async def feed(self, chunk: bytes) -> None:
        """Feed a raw Int16 PCM chunk. Triggers STT when a segment completes."""
        # Ignore audio during deafen (TTS playback / echo avoidance)
        if time.monotonic() < self._deafen_until:
            return

        is_speech = self._is_speech(chunk)

        if is_speech:
            self._silence_start = None
            if not self._speaking:
                self._speaking = True
                self._speech_chunk_count = 0
                self._interrupt_fired = False
            self._speech_chunk_count += 1
            self._chunks.append(chunk)
            # Only fire interrupt after enough consecutive speech chunks
            if not self._interrupt_fired and self._speech_chunk_count >= self.MIN_SPEECH_CHUNKS_FOR_INTERRUPT:
                now = time.monotonic()
                if now - self._last_interrupt >= self._interrupt_cooldown:
                    self._last_interrupt = now
                    self._interrupt_fired = True
                    if self._on_interrupt:
                        await self._on_interrupt()
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
        self._speech_chunk_count = 0
        self._interrupt_fired = False
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
                stripped = text.strip()
                # SenseVoice returns special tokens for silence/noise — skip these
                if any(tok in stripped for tok in ("<|nospeech|>", "<|woitn|>")):
                    logger.debug("STT returned nospeech token, discarding")
                    return
                await self._on_transcription(stripped)
        except asyncio.TimeoutError:
            logger.warning("STT timed out (segment length=%d bytes)", len(audio))
        except Exception:
            logger.warning("STT transcription failed", exc_info=True)

    def reset(self) -> None:
        """Clear all buffered state (e.g., on interruption)."""
        self._chunks.clear()
        self._speaking = False
        self._speech_chunk_count = 0
        self._interrupt_fired = False
        self._silence_start = None
