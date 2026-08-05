"""批次 5 契约测试：providers 新字段 + tts voices/preview + set_tts_voice + generation.failed.model（marker=unit）。

对照 spec §3.8/§4.1/§4.2/§4.3 + plan Task 3 Interfaces 逐字段建模：
- ProviderEntry/CustomProvider 增 credentialConfigured/runtime/capabilities（缺省 backend/False/{}）
- TtsVoiceEntry/TtsVoicesResponse/TtsPreviewResult（spec §4.1/§4.2 原样形状）
- SetTtsVoiceMessage（Client→Server）/SetTtsVoiceResultMessage（Server→Client，加入 ServerMessage 联合）
- GenerationFailedMessage 补 model（缺省省略，不破坏现有 failed 帧）
- 既有 ProviderTestResult/ProvidersResponse round-trip 不破坏
"""
import pytest

from frontend_contract import ws_message as w
from frontend_contract.rest import (
    CustomProvider,
    ProviderEntry,
    ProvidersResponse,
    ProviderTestResult,
    TtsPreviewResult,
    TtsVoiceEntry,
    TtsVoicesResponse,
)


@pytest.mark.unit
def test_provider_entry_new_default_fields_on_wire():
    """ProviderEntry 新字段默认值上 wire：credentialConfigured/runtime/capabilities。"""
    d = ProviderEntry(models=["a"]).wire_dict()
    assert d["models"] == ["a"]
    assert d["credentialConfigured"] is False
    assert d["runtime"] == "backend"
    assert d["capabilities"] == {}


@pytest.mark.unit
def test_custom_provider_new_default_fields_on_wire():
    """CustomProvider 新字段默认值上 wire：runtime/credentialConfigured/capabilities。"""
    d = CustomProvider(name="x", base_url="").wire_dict()
    assert d["name"] == "x"
    assert d["base_url"] == ""
    assert d["runtime"] == "backend"
    assert d["credentialConfigured"] is False
    assert d["capabilities"] == {}


@pytest.mark.unit
def test_provider_entry_explicit_fields_on_wire():
    """ProviderEntry 显式值上 wire（runtime=desktop / 能力 dict）。"""
    d = ProviderEntry(models=["a"], credentialConfigured=True, runtime="desktop",
                      capabilities={"auth": "token"}).wire_dict()
    assert d["credentialConfigured"] is True
    assert d["runtime"] == "desktop"
    assert d["capabilities"] == {"auth": "token"}


@pytest.mark.unit
def test_tts_voice_entry_defaults():
    """TtsVoiceEntry：id 必填；name 可缺省省略；languages/capabilities 缺省空。"""
    d = TtsVoiceEntry(id="v1").wire_dict()
    assert d["id"] == "v1"
    assert "name" not in d
    assert d["languages"] == []
    assert d["capabilities"] == {}


@pytest.mark.unit
def test_tts_voices_response_roundtrip():
    """TtsVoicesResponse(voices=[TtsVoiceEntry(id="v1")]) round-trip（source 缺省 static）。"""
    resp = TtsVoicesResponse(voices=[TtsVoiceEntry(id="v1")])
    d = resp.wire_dict()
    assert d["voices"][0]["id"] == "v1"
    assert d["source"] == "static"
    assert "current" not in d
    assert "error" not in d
    # wire 字节再 model_validate（round-trip 不丢键）
    rt = TtsVoicesResponse.model_validate(d)
    assert rt.voices[0].id == "v1"
    # current/source/error 显式带上
    d2 = TtsVoicesResponse(voices=[], current="v2", source="api", error="boom").wire_dict()
    assert d2["current"] == "v2"
    assert d2["source"] == "api"
    assert d2["error"] == "boom"


@pytest.mark.unit
def test_tts_preview_result_wire():
    """TtsPreviewResult：ok 必填；mime/duration_ms/audio_base64/error 可缺省省略。"""
    ok = TtsPreviewResult(ok=True, mime="audio/mpeg", duration_ms=1200,
                          audio_base64="AAAA").wire_dict()
    assert ok == {"ok": True, "mime": "audio/mpeg", "duration_ms": 1200, "audio_base64": "AAAA"}
    assert "error" not in ok
    fail = TtsPreviewResult(ok=False, error="TTS 未启用").wire_dict()
    assert fail == {"ok": False, "error": "TTS 未启用"}
    assert "mime" not in fail and "audio_base64" not in fail


@pytest.mark.unit
def test_set_tts_voice_message_wire():
    """SetTtsVoiceMessage：Client→Server，type=set_tts_voice + voice 必填。"""
    msg = w.SetTtsVoiceMessage(voice="v1")
    assert msg.wire_dict() == {"type": "set_tts_voice", "voice": "v1"}
    with pytest.raises(Exception):
        w.SetTtsVoiceMessage()


@pytest.mark.unit
def test_set_tts_voice_result_message_wire():
    """SetTtsVoiceResultMessage：ok + voice/error 可缺省省略。"""
    ok = w.SetTtsVoiceResultMessage(ok=True, voice="v1").wire_dict()
    assert ok == {"type": "set_tts_voice_result", "ok": True, "voice": "v1"}
    assert "error" not in ok
    fail = w.SetTtsVoiceResultMessage(ok=False, error="未知声音").wire_dict()
    assert fail == {"type": "set_tts_voice_result", "ok": False, "error": "未知声音"}
    assert "voice" not in fail


@pytest.mark.unit
def test_set_tts_voice_result_is_server_message_member():
    """SetTtsVoiceResultMessage 是 ServerMessage 联合成员（TypeAdapter 可判别）。

    ServerMessage 是 Annotated 判别联合别名（非 BaseModel），无 model_validate 方法——
    plan 写的 `ServerMessage.model_validate({...})` 实际不可调用，pydantic 等价机制是
    `TypeAdapter(ServerMessage).validate_python({...})`（判别效果相同）。
    """
    from typing import get_args
    members = get_args(get_args(w.ServerMessage)[0])
    assert w.SetTtsVoiceResultMessage in members
    from pydantic import TypeAdapter
    ta = TypeAdapter(w.ServerMessage)
    msg = ta.validate_python({"type": "set_tts_voice_result", "ok": True, "voice": "v1"})
    assert isinstance(msg, w.SetTtsVoiceResultMessage)
    # 其它 type 判别不误判为本成员
    assert not isinstance(ta.validate_python({"type": "delta", "content": "hi"}),
                          w.SetTtsVoiceResultMessage)


@pytest.mark.unit
def test_generation_failed_model_roundtrip():
    """GenerationFailedMessage 带 model round-trip；缺省 model 省略。"""
    d = w.GenerationFailedMessage(
        generationId="gen_1", messageId="msg_1", code="GENERATION_CANCELLED",
        message="已取消", retryable=False, model="fake/test",
    ).wire_dict()
    assert d["model"] == "fake/test"
    d2 = w.GenerationFailedMessage(
        generationId="gen_1", messageId="msg_1", code="GENERATION_CANCELLED",
        message="已取消", retryable=False,
    ).wire_dict()
    assert "model" not in d2


@pytest.mark.unit
def test_existing_provider_contracts_roundtrip_unbroken():
    """既有 ProviderTestResult/ProvidersResponse round-trip 不破坏（新字段缺省填充）。"""
    rt = ProviderTestResult(ok=True, latency_ms=12).wire_dict()
    assert rt == {"ok": True, "latency_ms": 12}
    resp = ProvidersResponse(
        llm={"deepseek": ProviderEntry(models=["deepseek-v4-flash"])},
        stt={}, tts={},
        custom={"llm": [CustomProvider(name="my", base_url="http://x")]},
        unconfigured={},
    )
    d = resp.wire_dict()
    assert d["llm"]["deepseek"]["models"] == ["deepseek-v4-flash"]
    assert d["custom"]["llm"][0]["name"] == "my"
    # 新字段缺省后 wire 仍可 model_validate（build_providers_response 兼容）
    ProvidersResponse.model_validate(d)
