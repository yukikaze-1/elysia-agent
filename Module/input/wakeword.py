"""Wake word detector using openWakeWord — streaming, on-device detection.

openWakeWord requires 80ms frames of 16kHz mono Int16 PCM (1280 samples = 2560 bytes).
AudioBuffer sends 256ms chunks — WakeWordDetector buffers and slices internally.
"""

import logging
import time
from typing import Any, Callable

import numpy as np

logger = logging.getLogger(__name__)

# openWakeWord frame: 80ms at 16kHz mono Int16
_WW_FRAME_MS = 80
_WW_FRAME_SAMPLES = int(16000 * _WW_FRAME_MS / 1000)  # 1280
_WW_FRAME_BYTES = _WW_FRAME_SAMPLES * 2                 # 2560

# Detection threshold (openWakeWord returns 0-1 score)
_SCORE_THRESHOLD = 0.5
# Debounce: ignore hits within this many seconds of a previous hit
_DEBOUNCE_SECONDS = 1.0


class WakeWordDetector:
    """Streaming wake word detector wrapping openWakeWord.

    Buffers incoming 256ms PCM chunks, slices into 80ms frames, and feeds each
    frame to one or more openWakeWord models. Fires on_wake callback on detection.
    """

    def __init__(
        self,
        wake_words: list[str],
        model_paths: dict[str, str] | None = None,
        on_wake: Callable[[], object] | None = None,
    ):
        self._wake_words = wake_words
        self._model_paths = model_paths or {}
        self._on_wake = on_wake
        self._buffer: bytearray = bytearray()
        self._models: list[tuple[str, Any]] = []  # [(word, model), ...]
        self._last_wake: float = 0.0

        self._load_models()

    def _load_models(self) -> None:
        try:
            import openwakeword  # type: ignore[import-untyped]  # optional dependency
        except ImportError:
            logger.warning(
                "openwakeword not installed. Install with: pip install openwakeword numpy. "
                "Wake word detection disabled."
            )
            return

        # Resolve built-in model directory
        import os as _os
        _pkg_dir = _os.path.dirname(openwakeword.__file__ or "")
        _builtin_dir = _os.path.join(_pkg_dir, "resources", "models")

        for word in self._wake_words:
            try:
                # 1. Explicit model path provided by user
                model_path = self._model_paths.get(word)
                if model_path:
                    model = openwakeword.Model(wakeword_model_paths=[model_path])
                    self._models.append((word, model))
                    logger.info("Wake word model loaded: %s (custom: %s)", word, model_path)
                    continue

                # 2. Try built-in model (e.g., "alexa" → "alexa_v0.1.onnx")
                builtin_path = _os.path.join(_builtin_dir, f"{word}_v0.1.onnx")
                if _os.path.isfile(builtin_path):
                    model = openwakeword.Model(wakeword_model_paths=[builtin_path])
                    self._models.append((word, model))
                    logger.info("Wake word model loaded: %s (built-in)", word)
                    continue

                # 3. Try exact filename match in built-in dir
                for candidate in _os.listdir(_builtin_dir):
                    if candidate.startswith(word) and candidate.endswith(".onnx"):
                        model_path_full = _os.path.join(_builtin_dir, candidate)
                        model = openwakeword.Model(wakeword_model_paths=[model_path_full])
                        self._models.append((word, model))
                        logger.info("Wake word model loaded: %s → %s", word, candidate)
                        break
                else:
                    logger.warning(
                        "No model found for wake word '%s'. "
                        "Built-in words: %s. Or provide a custom model path.", word,
                        [f.replace("_v0.1.onnx", "") for f in _os.listdir(_builtin_dir)
                         if f.endswith(".onnx") and f != "melspectrogram.onnx"
                         and f != "embedding_model.onnx" and f != "silero_vad.onnx"],
                    )
            except Exception as exc:
                logger.warning(
                    "Failed to load wake word model for '%s': %s", word, exc
                )

    @property
    def enabled(self) -> bool:
        return len(self._models) > 0

    def feed(self, chunk: bytes) -> None:
        if not self._models:
            return

        self._buffer.extend(chunk)

        while len(self._buffer) >= _WW_FRAME_BYTES:
            frame = bytes(self._buffer[:_WW_FRAME_BYTES])
            self._buffer = self._buffer[_WW_FRAME_BYTES:]

            samples = np.frombuffer(frame, dtype=np.int16)
            for word, model in self._models:
                try:
                    prediction = model.predict(samples)
                    # Match prediction keys by prefix (e.g., "alexa_v0.1" → "alexa")
                    score = 0.0
                    for key, val in prediction.items():
                        if key.startswith(word):
                            score = max(score, val)
                except Exception:
                    continue

                if score > _SCORE_THRESHOLD:
                    now = time.monotonic()
                    if now - self._last_wake >= _DEBOUNCE_SECONDS:
                        self._last_wake = now
                        logger.info("Wake word detected: %s (score=%.3f)", word, score)
                        if self._on_wake:
                            self._on_wake()

    def reset(self) -> None:
        self._buffer.clear()

    def close(self) -> None:
        self._models.clear()
        self._buffer.clear()
