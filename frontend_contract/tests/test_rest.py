"""rest 契约建模测试：ProvidersResponse 动态 key 形状 + 图片上传 + 通用成功/失败 + 消息归档（marker=unit）。

对照 `core/input/api_routes.py` 实际 wire 逐字段对齐：
- /api/images 错误响应无 ok 键（{error}）→ ImageUploadError 独立模型
- /api/providers/test 两 level 并集 → ProviderTestResult{ok, latency_ms, error?, detail?}
- /api/messages 错误带 error 键、成功省略 → MessagesResponse{...error?}
"""
from typing import Any

import pytest

from frontend_contract.base import field_is_ts_null
from frontend_contract.rest import (
    ApiFail,
    ApiOk,
    CustomProvider,
    ImageUploadError,
    ImageUploadResponse,
    Message,
    MessagesResponse,
    ProviderEntry,
    ProvidersResponse,
    ProviderTestResult,
    UnconfiguredEntry,
)


@pytest.mark.unit
def test_providers_response_shape():
    """ProvidersResponse：动态 provider key + custom/unconfigured 嵌套（plan Step 1 原样）。"""
    resp = ProvidersResponse(
        llm={"deepseek": ProviderEntry(models=["deepseek-v4-flash"])},
        stt={}, tts={},
        custom={"llm": [CustomProvider(name="my", base_url="http://x")]},
        unconfigured={"llm": [UnconfiguredEntry(name="openai", env_vars=["OPENAI_API_KEY"])]},
    )
    d = resp.wire_dict()
    assert d["llm"]["deepseek"]["models"] == ["deepseek-v4-flash"]
    assert d["custom"]["llm"][0]["name"] == "my"
    assert d["unconfigured"]["llm"][0]["env_vars"] == ["OPENAI_API_KEY"]


@pytest.mark.unit
def test_providers_response_roundtrip_registry_shape():
    """build_providers_response 输出能直接构造 ProvidersResponse（wire 不丢键）。"""
    from core.input.provider_registry import build_providers_response

    raw = build_providers_response({"llm": [], "stt": [], "tts": []})
    resp = ProvidersResponse.model_validate(raw)
    d = resp.wire_dict()
    # local 恒显示 provider 保留
    assert "ollama" in d["llm"]
    assert "sensevoice" in d["stt"]
    assert "gpt-sovits" in d["tts"]
    # custom/unconfigured 顶层键保留
    assert d["custom"] == {"llm": [], "stt": [], "tts": []}
    assert set(d["unconfigured"]) == {"llm", "stt", "tts"}


@pytest.mark.unit
def test_image_upload_response_fields():
    """图片上传成功响应：url/mime/size 三字段。"""
    d = ImageUploadResponse(url="/uploads/images/abc.png", mime="image/png", size=123).wire_dict()
    assert d == {"url": "/uploads/images/abc.png", "mime": "image/png", "size": 123}


@pytest.mark.unit
def test_image_upload_error_no_ok_key():
    """图片上传错误响应：{error} 且无 ok 键（与 ApiFail 形状不同，不可套用）。"""
    d = ImageUploadError(error="图片过大").wire_dict()
    assert d == {"error": "图片过大"}
    assert "ok" not in d


@pytest.mark.unit
def test_api_ok_and_api_fail():
    """通用成功/失败：ok 判别 + data/error。"""
    assert ApiOk().wire_dict() == {"ok": True}
    assert ApiOk(data={"llm": []}).wire_dict() == {"ok": True, "data": {"llm": []}}
    assert ApiFail(error="未知 service").wire_dict() == {"ok": False, "error": "未知 service"}


@pytest.mark.unit
def test_provider_test_result_union_shapes():
    """ProviderTestResult 两 level 并集：error/detail 均缺省省略，互不干扰。"""
    # usable 级：{ok, latency_ms, detail}，error 缺省省略
    usable = ProviderTestResult(ok=True, latency_ms=30, detail="你好，我在").wire_dict()
    assert usable == {"ok": True, "latency_ms": 30, "detail": "你好，我在"}
    assert "error" not in usable
    # connectivity 级成功：{ok, latency_ms}，error/detail 均缺省
    conn = ProviderTestResult(ok=True, latency_ms=12).wire_dict()
    assert conn == {"ok": True, "latency_ms": 12}
    # connectivity 级失败：error 带上
    fail = ProviderTestResult(ok=False, latency_ms=12, error="超时").wire_dict()
    assert fail == {"ok": False, "latency_ms": 12, "error": "超时"}
    assert "detail" not in fail


@pytest.mark.unit
def test_messages_response_shape():
    """MessagesResponse：messages 数组 + has_more/total，error 可缺省。"""
    msg = Message.model_validate({
        "role": "assistant",
        "parts": [{"content": "你好"}],
        "content": [{"type": "text", "content": "你好"}],
        "message_id": "m1",
        "timestamp": "2026-08-03T00:00:00Z",
        "session_id": "s1",
        "metadata": {},
    })
    ok = MessagesResponse(messages=[msg], has_more=False, total=1).wire_dict()
    assert ok["messages"][0]["role"] == "assistant"
    assert ok["messages"][0]["parts"][0]["content"] == "你好"
    assert ok["has_more"] is False and ok["total"] == 1
    assert "error" not in ok
    # 失败响应 error 带上
    err = MessagesResponse(messages=[], has_more=False, total=0, error="DB down").wire_dict()
    assert err["error"] == "DB down"


@pytest.mark.unit
def test_message_archive_view_keeps_wire_keys():
    """契约 Message 视图保留 _archive_to_client 全部 wire 键（byte 等价，不丢 _id/timestamp 等）。"""
    d = Message.model_validate({
        "role": "assistant",
        "content": [{"type": "text", "content": "hi"}],
        "message_id": "m1",
        "timestamp": "2026-08-03T00:00:00Z",
        "session_id": "s1",
        "metadata": {"k": "v"},
        "parts": [{"content": "hi"}],
        "_id": "mongo1",
        "content_type": {"markdown": [0, 2]},
        "inner_voice": "感到开心",
        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "time", "arguments": "{}"}}],
        "token_usage": {"model": "deepseek", "session_id": "s1", "round_num": 1,
                        "input_tokens": 1, "output_tokens": 1, "total_tokens": 2, "timestamp": "t"},
    }).wire_dict()
    assert d["_id"] == "mongo1"
    assert d["message_id"] == "m1"
    assert d["timestamp"] == "2026-08-03T00:00:00Z"
    assert d["content_type"] == {"markdown": [0, 2]}
    assert d["inner_voice"] == "感到开心"
    assert d["tool_calls"][0]["id"] == "call_1"
    assert d["token_usage"]["total_tokens"] == 2
    assert d["metadata"] == {"k": "v"}


@pytest.mark.unit
def test_message_fields_optional_qualification():
    """Message 部分字段 X?（缺省省略）而非 ts_null——与 session 回放定性一致。"""
    assert field_is_ts_null(Message.model_fields["mongo_id"]) is False
    assert field_is_ts_null(Message.model_fields["error"]) is False
    assert field_is_ts_null(MessagesResponse.model_fields["error"]) is False


@pytest.mark.unit
def test_message_extra_keys_ignored():
    """Message 含未建模键（如 block_index 残留）时不报错——发送点负责归一化。"""
    d = Message.model_validate({
        "role": "user",
        "content": "hello",
        "message_id": "m",
        "timestamp": "t",
        "session_id": "s",
        "parts": [{"content": "hello"}],
        "block_index": {"markdown": [1]},  # 未建模键，extra='ignore' 丢弃（发送点先转 content_type）
    }).wire_dict()
    assert "block_index" not in d
    assert d["role"] == "user"


@pytest.mark.unit
def test_protocol_error_envelope():
    """统一错误信封：code/message/retryable/requestId/error/details；error=message 兼容别名。"""
    from frontend_contract.rest import ProtocolError
    err = ProtocolError(
        code="AUTH_REQUIRED", message="需要认证",
        requestId="req_123", error="需要认证",
    )
    d = err.wire_dict()
    assert d["code"] == "AUTH_REQUIRED"
    assert d["message"] == "需要认证"
    assert d["retryable"] is False
    assert d["requestId"] == "req_123"
    assert d["error"] == "需要认证"  # 兼容别名（v1 前端读 err.error）
    assert d["details"] == {}
