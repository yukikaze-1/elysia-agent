"""WakeWordDetector 单元测试。

Run: python -m pytest Module/input/test_wakeword.py -v
"""

import struct

import pytest

from core.input.wakeword import WakeWordDetector


def _silence_bytes(count: int) -> bytes:
    return struct.pack(f"<{count}h", *([5] * count))


def _loud_bytes(count: int) -> bytes:
    return struct.pack(f"<{count}h", *([20000] * count))


@pytest.mark.unit
class TestWakeWordDetectorNoModels:
    def test_disabled_without_openwakeword(self):
        detector = WakeWordDetector(
            wake_words=["nonexistent_word_xyz"],
        )
        assert detector.enabled is False

    def test_feed_noop_when_disabled(self):
        detector = WakeWordDetector(wake_words=["nonexistent_word_xyz"])
        detector.feed(_loud_bytes(4096))  # 256ms at 16kHz
        # 不崩溃即为成功


@pytest.mark.unit
class TestWakeWordDetectorBuffer:
    def test_feed_noop_when_disabled_leaves_buffer_empty(self):
        """未加载模型时，feed() 完全跳过缓冲。"""
        detector = WakeWordDetector(wake_words=["test"])
        assert detector.enabled is False
        detector.feed(_silence_bytes(1280))  # 80ms frame
        assert len(detector._buffer) == 0  # buffer untouched

    def test_feed_large_chunk_noop_when_disabled(self):
        """禁用时，大块数据同样跳过缓冲。"""
        detector = WakeWordDetector(wake_words=["test"])
        assert detector.enabled is False
        detector.feed(_loud_bytes(4096))  # 256ms
        assert len(detector._buffer) == 0

    def test_partial_chunk_noop_when_disabled(self):
        """禁用时，小块数据同样跳过缓冲。"""
        detector = WakeWordDetector(wake_words=["test"])
        assert detector.enabled is False
        detector.feed(_silence_bytes(640))  # 40ms
        assert len(detector._buffer) == 0

    def test_reset_noop_when_disabled(self):
        detector = WakeWordDetector(wake_words=["test"])
        assert detector.enabled is False
        detector.reset()
        assert len(detector._buffer) == 0


@pytest.mark.unit
class TestWakeWordDetectorCallback:
    def test_no_callback_when_disabled(self):
        calls = []
        detector = WakeWordDetector(
            wake_words=["nonexistent_word_xyz"],
            on_wake=lambda: calls.append(1),
        )
        for _ in range(10):
            detector.feed(_loud_bytes(4096))
        assert len(calls) == 0

    def test_close_releases_resources(self):
        detector = WakeWordDetector(wake_words=["test"])
        detector.feed(_silence_bytes(4096))
        detector.close()
        assert len(detector._models) == 0
        assert len(detector._buffer) == 0
