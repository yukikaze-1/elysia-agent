"""TTSStreamManager 异步 set_enabled + 禁用 flush 机制单元测试。

覆盖:
- set_enabled(False) 禁用时：排队未播句文本经 per-task flush_on_cancel 兜底广播，不丢句子
- active 保持"后端可用"语义，不被重定义为 enabled
"""

import asyncio
import struct

import pytest

from core.adapter.tts_adapter import TTSStreamManager, _wav_info

pytestmark = pytest.mark.unit


class _FakeTTS:
    """假 TTS 后端：synthesize 挂起在 gate 上，直到被取消——确定性停在下载窗口。"""

    def __init__(self, gate):
        self.gate = gate

    async def synthesize(self, text, **kw):
        await self.gate.wait()            # 挂起直到测试取消——保证任务停在下载窗口
        return b"RIFF" + b"\0" * 16

    def get_preprocess(self):
        return None

    @property
    def stream_format(self):
        return "wav"

    async def close(self):
        pass


class _FakeOutput:
    """假输出管理器：捕获 on_text_delta 广播文本。"""

    def __init__(self):
        self.texts = []                   # 已广播的 on_text_delta 文本

    async def _broadcast(self, kind, text, **kw):
        if kind == "on_text_delta":
            self.texts.append(text)

    async def send(self, marker=""):
        pass

    async def audio_chunk(self, data, **kw):
        pass

    async def audio_end(self, sid, **kw):
        pass

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_set_enabled_false_flushes_pending_text():
    out = _FakeOutput()
    mgr = TTSStreamManager(_FakeTTS(asyncio.Event()), out, "test")
    mgr.stream_sentence("这是排队句")
    await asyncio.sleep(0)               # 任务启动，停在 synthesize 的 gate.wait()
    await mgr.set_enabled(False)         # 置位 → cancel → drain → flush_on_cancel 自刷
    assert "这是排队句" in out.texts     # 禁用兜底广播，排队未播句不丢


@pytest.mark.asyncio
async def test_active_stays_backend_and_enabled_gates():
    mgr = TTSStreamManager(None, _FakeOutput(), "test")
    assert mgr.active is False           # 无后端
    mgr.set_backend(_FakeTTS(asyncio.Event()))
    assert mgr.active is True            # active 保持"后端可用"语义
    await mgr.set_enabled(False)
    assert mgr.enabled is False
    assert mgr.active is True            # enabled 独立于 active，不重定义


class _VoiceTTS(_FakeTTS):
    """假 TTS 后端：捕获 synthesize 收到的 voice_id（批次 5 Task 11 声音覆盖）。"""

    def __init__(self, gate, captured):
        super().__init__(gate)
        self.captured = captured

    async def synthesize(self, text, **kw):
        self.captured["voice_id"] = kw.get("voice_id")
        return b"RIFF" + b"\0" * 16


@pytest.mark.asyncio
async def test_set_voice_passes_voice_id_to_synthesize():
    """set_voice('v2') 后合成 → 后端 synthesize 收到 voice_id='v2'。"""
    captured = {}
    out = _FakeOutput()
    mgr = TTSStreamManager(_VoiceTTS(asyncio.Event(), captured), out, "test")
    mgr.set_voice("v2")
    mgr.reset_for_turn()  # 真实流程 process() 每轮先 reset
    mgr.stream_sentence("测试句")
    await mgr.wait_all()
    assert captured["voice_id"] == "v2"


@pytest.mark.asyncio
async def test_no_voice_means_no_voice_id():
    """未 set → synthesize 不传 voice_id（None 由后端走默认）。"""
    captured = {}
    out = _FakeOutput()
    mgr = TTSStreamManager(_VoiceTTS(asyncio.Event(), captured), out, "test")
    mgr.reset_for_turn()
    mgr.stream_sentence("测试句")
    await mgr.wait_all()
    assert captured["voice_id"] is None


# ---- §3.11：_wav_info 遍历 chunk 表——含额外 chunk 的 WAV 正确解析 ----


def _wav_standard(sample_rate=16000, byte_rate=32000, data_size=3200):
    """标准 44 字节 WAV 头 + data。"""
    header = b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVE"
    fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, byte_rate, 2, 16)
    data = b"data" + struct.pack("<I", data_size)
    return header + fmt + data + b"\x00" * data_size


def _wav_with_extra_chunks(sample_rate=16000, byte_rate=32000, data_size=3200):
    """RIFF 头后先插 LIST/fact 额外 chunk，再 data——非 44 字节头。"""
    body = b"RIFF" + b"WAVE"
    # 直接构造：RIFF 头 12 字节 + LIST chunk + fmt chunk + data chunk
    def chunk(cid, payload):
        padded = payload if len(payload) % 2 == 0 else payload + b"\x00"
        return cid + struct.pack("<I", len(payload)) + padded
    list_payload = b"INFO" + b"\x00" * 20
    fact_payload = struct.pack("<I", 400)
    fmt_payload = struct.pack("<HHIIHH", 1, 1, sample_rate, byte_rate, 2, 16)
    total = 4 + len(chunk(b"LIST", list_payload)) + len(chunk(b"fact", fact_payload)) \
        + len(chunk(b"fmt ", fmt_payload)) + len(chunk(b"data", b""))
    prefix = b"RIFF" + struct.pack("<I", total) + b"WAVE"
    return prefix + chunk(b"LIST", list_payload) + chunk(b"fact", fact_payload) \
        + chunk(b"fmt ", fmt_payload) + b"data" + struct.pack("<I", data_size) \
        + b"\x00" * data_size


def test_wav_info_standard_header():
    sr, dur = _wav_info(_wav_standard())
    assert sr == 16000
    assert dur == int(3200 * 1000 / 32000)  # 100ms


def test_wav_info_extra_chunks_correct_duration():
    """含 LIST/fact 额外 chunk → durationMs 不再错（旧固定偏移 40 会读错 data size）。"""
    sr, dur = _wav_info(_wav_with_extra_chunks())
    assert sr == 16000
    assert dur == int(3200 * 1000 / 32000)


def test_wav_info_non_wav_returns_none():
    assert _wav_info(b"\x00" * 64) == (None, None)
    assert _wav_info(b"") == (None, None)
