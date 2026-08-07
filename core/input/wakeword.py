"""基于 openWakeWord 的唤醒词检测器 —— 流式、端侧检测。

openWakeWord 需要 80ms 帧的 16kHz 单声道 Int16 PCM（1280 样本 = 2560 字节）。
AudioBuffer 发送 256ms 的块 —— WakeWordDetector 内部缓冲并切片。
"""

import logging
import time
from typing import Any, Callable

import numpy as np

logger = logging.getLogger(__name__)

# openWakeWord 帧：16kHz 单声道 Int16 下 80ms
_WW_FRAME_MS = 80
_WW_FRAME_SAMPLES = int(16000 * _WW_FRAME_MS / 1000)  # 1280
_WW_FRAME_BYTES = _WW_FRAME_SAMPLES * 2                 # 2560

# 检测阈值（openWakeWord 返回 0-1 的分数）
_SCORE_THRESHOLD = 0.5
# 去抖：距上一次命中不足该秒数时忽略本次命中
_DEBOUNCE_SECONDS = 1.0


class WakeWordDetector:
    """包装 openWakeWord 的流式唤醒词检测器。

    缓冲进来的 256ms PCM 块，切成 80ms 帧，逐帧送入一个或多个
    openWakeWord 模型。检测到时触发 on_wake 回调。
    """

    def __init__(
        self,
        wake_words: list[str],
        model_paths: dict[str, str] | None = None,
        on_wake: Callable[[], object] | None = None,
    ):
        """初始化唤醒词检测器。

        Args:
            wake_words: 要检测的唤醒词列表（如 ["alexa"]）。
            model_paths: 可选的唤醒词 → 自定义模型文件路径映射。
            on_wake: 检测到唤醒词时触发的回调。
        """
        self._wake_words = wake_words
        self._model_paths = model_paths or {}
        self._on_wake = on_wake
        self._buffer: bytearray = bytearray()
        self._models: list[tuple[str, Any]] = []  # [(word, model), ...]
        self._last_wake: float = 0.0

        self._load_models()

    def _load_models(self) -> None:
        try:
            import openwakeword  # type: ignore[import-untyped]  # 可选依赖
        except ImportError:
            logger.warning(
                "openwakeword not installed. Install with: pip install openwakeword numpy. "
                "Wake word detection disabled."
            )
            return

        # 解析内置模型目录
        import os as _os
        _pkg_dir = _os.path.dirname(openwakeword.__file__ or "")
        _builtin_dir = _os.path.join(_pkg_dir, "resources", "models")

        for word in self._wake_words:
            try:
                # 1. 用户显式提供的模型路径
                model_path = self._model_paths.get(word)
                if model_path:
                    model = openwakeword.Model(wakeword_model_paths=[model_path])
                    self._models.append((word, model))
                    logger.info("Wake word model loaded: %s (custom: %s)", word, model_path)
                    continue

                # 2. 尝试内置模型（如 "alexa" → "alexa_v0.1.onnx"）
                builtin_path = _os.path.join(_builtin_dir, f"{word}_v0.1.onnx")
                if _os.path.isfile(builtin_path):
                    model = openwakeword.Model(wakeword_model_paths=[builtin_path])
                    self._models.append((word, model))
                    logger.info("Wake word model loaded: %s (built-in)", word)
                    continue

                # 3. 在内置目录中尝试精确文件名匹配
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
        """已加载至少一个唤醒词模型时返回 True。"""
        return len(self._models) > 0

    def feed(self, chunk: bytes) -> None:
        """喂入原始 PCM 音频块进行唤醒词检测。

        Args:
            chunk: 原始 Int16 PCM 音频数据（16kHz 下 256ms）。
        """
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
                    # 按前缀匹配预测键（如 "alexa_v0.1" → "alexa"）
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
        """清空内部音频缓冲（如被打断时）。"""
        self._buffer.clear()

    def close(self) -> None:
        """释放所有模型并清空缓冲。"""
        self._models.clear()
        self._buffer.clear()
