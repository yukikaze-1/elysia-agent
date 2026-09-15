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
        self.calls.append(("audio_start", stream_id, sample_rate, duration_ms, kw.get("ctx")))
    async def audio_cancelled(self, stream_id, **kw):
        self.calls.append(("audio_cancelled", stream_id))


def test_model_property_reflects_runtime_backend():
    """#21c：运行期 TTS 模型必须可读（与 `LLMAdapter.model` / STT 后端 `model` 对称）。

    判定"某个 custom provider 是否正在被使用"时必须能读到**真正在跑的后端**：
    切会话只换活动指针、**有意不重放模型热切换**（S2-C3），所以只读会话档位会漏，
    而漏的后果是删掉在用的 provider（连带删密钥）。
    """
    class _BackendWithModel(FakeTTS):
        model = "gpt-sovits/elysia"

    mgr = TTSStreamManager(tts=_BackendWithModel(), output=FakeOut(), character_name="elysia")
    assert mgr.model == "gpt-sovits/elysia"
    mgr.set_backend(None)
    assert mgr.model == "", "后端拆掉后必须归零，不能留着上一个模型 id"


def test_model_property_tolerates_backend_without_model():
    """鸭子类型后端（未继承 ABC、没有 `model`）→ 空串，**不抛** AttributeError。

    这条钉的是**兜底方向**：读不到就当空串（与其余候选一致），而不是让
    `/api/providers` 的删除判定炸掉。代价是"不实现 `model` 的后端退回旧行为"——
    写在这里，免得这个兜底被当成完备覆盖。
    """
    mgr = TTSStreamManager(tts=FakeTTS(), output=FakeOut(), character_name="elysia")
    assert mgr.model == ""


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
async def test_stream_sentence_preserves_generation_context_for_audio_start():
    from core.cognitive.generation import GenerationContext

    out = FakeOut()
    mgr = TTSStreamManager(tts=FakeTTS(), output=out, character_name="elysia")
    mgr.reset_for_turn()
    ctx = GenerationContext(
        generation_id="gen_1", user_message_id="msg_u1",
        assistant_message_id="msg_a1", conversation_id="conv_1",
        character_id="chr_elysia", character_instance_id="chi_1",
        character_revision=3,
    )
    mgr.stream_sentence("你好", ctx=ctx)
    await mgr.wait_all()
    start = [c for c in out.calls if c[0] == "audio_start"][0]
    captured = start[4]
    assert captured is not ctx
    assert captured.conversation_id == "conv_1"
    assert captured.generation_id == "gen_1"
    assert captured.character_id == "chr_elysia"
    assert captured.character_instance_id == "chi_1"
    assert captured.character_revision == 3


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


# §3.9：cancel_stream 以「正在转发」判定——已 audio_end 的任务返回 False（spec §8.2）
from unittest.mock import AsyncMock, MagicMock


def _make_mgr():
    tts = MagicMock()
    tts.stream_format = "wav"
    tts.get_preprocess.return_value = None
    tts.synthesize = AsyncMock(return_value=b"RIFFWAVE" + b"\x00" * 100)
    output = MagicMock()
    output._broadcast = AsyncMock()
    output.send = AsyncMock()
    for name in ("audio_start", "audio_chunk", "audio_end", "audio_cancelled",
                 "typing", "turn_completed", "turn_cancelled"):
        setattr(output, name, AsyncMock())
    mgr = TTSStreamManager(tts=tts, output=output, character_name="测试")
    return mgr, output


@pytest.mark.asyncio
async def test_cancel_after_audio_end_returns_false():
    """audio_end 已发（_audio_forwarding 已 discard）的任务 → False（PLAYBACK_NOT_FOUND）。"""
    mgr, _ = _make_mgr()
    # 构造一个「存活但已 audio_end」的任务：sid=1 已从 _audio_forwarding 移除
    async def _hanging():
        await asyncio.sleep(10)
    task = asyncio.create_task(_hanging())
    mgr._task_by_sid = {1: task}
    mgr._audio_forwarding = set()          # 已 audio_end
    assert mgr.cancel_stream(1) is False
    assert task.done() is False            # 未取消——客户端应收到 PLAYBACK_NOT_FOUND
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_cancel_while_forwarding_returns_true():
    """正在转发（_audio_forwarding 含 sid）→ True，任务被取消。"""
    mgr, _ = _make_mgr()
    async def _hanging():
        await asyncio.sleep(10)
    task = asyncio.create_task(_hanging())
    mgr._task_by_sid = {1: task}
    mgr._audio_forwarding = {1}
    assert mgr.cancel_stream(1) is True
    await asyncio.sleep(0)        # task.cancel() 后须让出事件循环，done() 才置位
    assert task.done()
    with pytest.raises(asyncio.CancelledError):
        await task
