"""TTSStreamManager 异步 set_enabled + 禁用 flush 机制单元测试。

覆盖:
- set_enabled(False) 禁用时：排队未播句文本经 per-task flush_on_cancel 兜底广播，不丢句子
- active 保持"后端可用"语义，不被重定义为 enabled
"""

import asyncio

import pytest

from core.adapter.tts_adapter import TTSStreamManager

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
