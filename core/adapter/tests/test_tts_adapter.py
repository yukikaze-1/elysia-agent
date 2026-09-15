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


# ═══════════════════════════════════════════════════════════════════
# S2-G3：门控不得出现"永不释放"的等待
# ═══════════════════════════════════════════════════════════════════


class _OrderedOutput(_FakeOutput):
    """按顺序记录 audio_end 的 sid —— 用于断言"谁播完了"。"""

    def __init__(self):
        super().__init__()
        self.sids = []
        self.started = []

    async def audio_start(self, stream_id, **kw):
        """必须实现：转发路径会调它——缺了就抛 AttributeError、被 except 吞掉，
        于是"有没有转发"这件事**根本观测不到**（本用例第一版正是这样假绿的）。"""
        self.started.append(stream_id)

    async def audio_end(self, sid, **kw):
        self.sids.append(sid)


class _ImmediateTTS:
    """立即返回音频的后端（不挂起），stream_format=wav。"""

    def __init__(self):
        self.calls = 0

    async def synthesize(self, text, **kw):
        self.calls += 1
        return b"RIFF" + b"\0" * 16

    def get_preprocess(self):
        return None

    @property
    def stream_format(self):
        return "wav"


@pytest.mark.asyncio
async def test_early_return_releases_successor_gate(monkeypatch):
    """S2-G3：`_download_and_forward` 的**早退**（后端被摘除）也必须放行后继门控。

    该分支在 try/finally **之外**，原先直接 `return`。可达窗口是"后端切换与本轮排队句
    交错"：句 1 的任务在后端已摘除时启动（早退、不放行），句 2 的任务在后端回来后启动
    → 它会永远等在 gate 2 上；而 `wait_all()` 没有超时 ⇒ 整轮卡在 SPEAKING。

    这里按那个交错顺序精确驱动（不是"创建后立刻禁用"——那条路径两边都会早退，不会挂）。
    """
    mgr = TTSStreamManager(tts=_FakeTTS(asyncio.Event()), output=_FakeOutput(),
                           character_name="elysia")
    mgr.reset_for_turn()
    mgr.stream_sentence("句 1")          # task1 创建（尚未运行）
    mgr.set_backend(None)                # 后端被摘除（切换窗口）
    await asyncio.sleep(0)               # task1 运行 → 早退
    gate = asyncio.Event()
    mgr.set_backend(_FakeTTS(gate))      # 后端回来
    mgr.stream_sentence("句 2")          # seq=2
    await asyncio.sleep(0)               # task2 进入 synthesize 挂起
    gate.set()                           # 放行合成

    await asyncio.wait_for(mgr.wait_all(), timeout=2.0)   # 挂死则超时 → 用例失败
    assert all(t.done() for t in mgr._tasks), "有任务没能收尾（门控没放行）"  # noqa: SLF001


class _FirstCallGatedTTS:
    """第 1 次 synthesize 挂起、之后立即返回。

    用来制造 reset 时的**最坏交错**：句 1 卡在下载、句 2 已过下载并卡在门控 2 上
    （句 2 的 synthesize 立即返回，所以它真的停在 `await gate.wait()` 那一行，
    手里握着当时的那个 Event 对象——这正是"孤儿门控"能挂死任务的前提）。
    """

    def __init__(self):
        self.first_gate = asyncio.Event()
        self.calls = 0

    async def synthesize(self, text, **kw):
        self.calls += 1
        if self.calls == 1:
            await self.first_gate.wait()
        return b"RIFF" + b"\0" * 16

    def get_preprocess(self):
        return None

    @property
    def stream_format(self):
        return "wav"


def _two_sentence_interleave(out=None):
    """构建"句 1 卡下载 / 句 2 卡门控"的交错并返回 (mgr, backend, out)。"""
    backend = _FirstCallGatedTTS()
    out = out or _FakeOutput()
    mgr = TTSStreamManager(tts=backend, output=out, character_name="elysia")
    mgr.reset_for_turn()
    mgr.stream_sentence("句 1")     # 卡在 synthesize
    mgr.stream_sentence("句 2")     # synthesize 立即返回 → 卡在 gate 2
    return mgr, backend, out


@pytest.mark.asyncio
async def test_reset_for_turn_does_not_orphan_gate_waiter():
    """S2-G3：`reset_for_turn` 必须**放行**旧门控，否则卡在门控上的任务永久挂起。

    原实现直接 `clear()`：正卡在 `await event.wait()` 的任务已持有**旧对象**，
    重建 dict 无从唤醒它 ⇒ `wait_all()`（无超时）永久等待 ⇒ 整轮卡在 SPEAKING。
    """
    mgr, backend, _ = _two_sentence_interleave()
    await asyncio.sleep(0.02)
    assert 2 in mgr._release_events, "前置条件：句 2 的门控已建（未 set）"  # noqa: SLF001

    mgr.reset_for_turn()            # 旧门控在此必须被 set（注入 B：只 clear → 句 2 永久挂起）
    backend.first_gate.set()        # 句 1 的下载放行

    await asyncio.wait_for(mgr.wait_all(), timeout=2.0)


@pytest.mark.asyncio
async def test_stale_task_from_previous_turn_does_not_forward():
    """S2-G3 的另一半：旧轮次的残留任务恢复后**不得**把音频播进新一轮。

    与上一条同一段 epoch 判断，但断言"行为"而不是"不挂起"——旧句若照常转发，
    它的音频会插进新一轮的播放序列（比挂起更隐蔽）。
    """
    out = _OrderedOutput()
    mgr, backend, _ = _two_sentence_interleave(out)

    await asyncio.sleep(0.02)
    mgr.reset_for_turn()            # 新一轮（epoch+1）
    backend.first_gate.set()        # 两个旧任务都恢复
    await asyncio.sleep(0.05)

    assert out.sids == [], f"旧轮次的句被播出去了：{out.sids}"
