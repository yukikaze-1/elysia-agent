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
    ConversationUsage,
    CustomProvider,
    DesktopCallStatus,
    FinishReason,
    GenerationListResponse,
    GenerationStatus,
    GenerationStatusEnum,
    ImageUploadError,
    ImageUploadResponse,
    Message,
    MessageContextResponse,
    MessagesPageResponse,
    MessagesResponse,
    PendingApprovalItem,
    PendingMessageItem,
    PendingQueryResponse,
    ProviderEntry,
    ProvidersResponse,
    ProviderTestResult,
    RepoCallStatus,
    ResourceItem,
    ResourceListResponse,
    Role,
    SearchMessageItem,
    SearchMessagesResponse,
    SessionActivateResult,
    SessionDetail,
    SessionListResult,
    SessionSummary,
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
def test_message_requires_non_empty_message_id():
    from frontend_contract.rest import Message
    from pydantic import ValidationError
    import pytest

    with pytest.raises(ValidationError):
        Message.model_validate({
            "role": "tool", "message_id": "", "timestamp": "2026-08-13T00:00:00Z",
            "content": [], "parts": [],
        })


@pytest.mark.unit
def test_message_accepts_real_message_id():
    from frontend_contract.rest import Message

    m = Message.model_validate({
        "role": "tool", "message_id": "msg_0123456789abcdef0123456789",
        "timestamp": "2026-08-13T00:00:00Z", "content": [], "parts": [],
    })
    assert m.message_id.startswith("msg_")


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


# ── 批次 3 v2 契约模型（多会话/历史/搜索/资源/状态） ──────────────────────────


@pytest.mark.unit
def test_session_summary_wire():
    """SessionSummary：conversationId/title/messageCount/updatedAt/active（含 conversationId/title）。"""
    s = SessionSummary(
        conversationId="conv_abc", title="闲聊", messageCount=3,
        updatedAt="2026-08-04T00:00:00+00:00", active=False,
    )
    d = s.wire_dict()
    assert d == {
        "conversationId": "conv_abc", "title": "闲聊", "messageCount": 3,
        "updatedAt": "2026-08-04T00:00:00+00:00", "active": False,
    }


@pytest.mark.unit
def test_session_summary_title_omitted_when_none():
    """SessionSummary.title 可空（创建后未命名）→ None 缺省省略。"""
    s = SessionSummary(conversationId="conv_abc", messageCount=0,
                       updatedAt="2026-08-04T00:00:00+00:00", active=False)
    d = s.wire_dict()
    assert d["conversationId"] == "conv_abc"
    assert "title" not in d


@pytest.mark.unit
def test_session_list_result_wire():
    """SessionListResult：sessions 数组 + has_more + 可缺省 cursor（命名避开 debug.SessionListResponse）。"""
    d = SessionListResult(sessions=[], has_more=False).wire_dict()
    assert d == {"sessions": [], "has_more": False}
    assert "cursor" not in d
    d2 = SessionListResult(sessions=[], has_more=True, cursor="base64:x").wire_dict()
    assert d2["cursor"] == "base64:x"


@pytest.mark.unit
def test_session_detail_wire():
    """SessionDetail：session + messages（第一页）+ active。"""
    msg = Message.model_validate({
        "role": "user", "content": "hi", "message_id": "m1",
        "timestamp": "t", "session_id": "conv_abc", "parts": [{"content": "hi"}],
    })
    summary = SessionSummary(conversationId="conv_abc", messageCount=1,
                             updatedAt="t", active=True)
    d = SessionDetail(session=summary, messages=[msg], active=True).wire_dict()
    assert d["session"]["conversationId"] == "conv_abc"
    assert d["messages"][0]["message_id"] == "m1"
    assert d["active"] is True


@pytest.mark.unit
def test_session_activate_result_wire():
    """SessionActivateResult：activeConversationId。"""
    d = SessionActivateResult(activeConversationId="conv_abc").wire_dict()
    assert d == {"activeConversationId": "conv_abc"}


@pytest.mark.unit
def test_messages_page_response_wire():
    """MessagesPageResponse：messages 数组 + has_more/total（会话内历史第一页）。"""
    msg = Message.model_validate({
        "role": "assistant", "content": "hi", "message_id": "m2",
        "timestamp": "t", "session_id": "conv_abc", "parts": [{"content": "hi"}],
    })
    d = MessagesPageResponse(messages=[msg], has_more=True, total=10).wire_dict()
    assert d["messages"][0]["role"] == "assistant"
    assert d["has_more"] is True and d["total"] == 10


@pytest.mark.unit
def test_message_context_response_wire():
    """MessageContextResponse：anchor + before/after 窗口 + conversationId。"""
    def mk(mid: str):
        return Message.model_validate({
            "role": "user", "content": mid, "message_id": mid, "timestamp": "t",
            "session_id": "conv_abc", "parts": [{"content": mid}],
        })

    d = MessageContextResponse(
        anchor=mk("a"), before=[mk("b1"), mk("b2")], after=[mk("c1")],
        conversationId="conv_abc").wire_dict()
    assert d["anchor"]["message_id"] == "a"
    assert [m["message_id"] for m in d["before"]] == ["b1", "b2"]
    assert [m["message_id"] for m in d["after"]] == ["c1"]
    assert d["conversationId"] == "conv_abc"


@pytest.mark.unit
def test_search_message_item_wire():
    """SearchMessageItem：conversationId/messageId/role/timestamp/snippet；responseId/segmentId 可空省略。"""
    item = SearchMessageItem(
        conversationId="conv_abc", messageId="msg_1", role="user",
        timestamp="2026-08-04T00:00:00+00:00", snippet="你好",
    )
    d = item.wire_dict()
    assert d["conversationId"] == "conv_abc"
    assert d["messageId"] == "msg_1"
    assert d["role"] == "user"
    assert d["timestamp"] == "2026-08-04T00:00:00+00:00"
    assert d["snippet"] == "你好"
    assert "responseId" not in d  # 可空省略（无则缺省，best-effort）
    assert "segmentId" not in d   # 可空省略（消息级搜索恒 null 定位语义）
    # 带上 responseId 时发射
    d2 = SearchMessageItem(
        conversationId="conv_abc", messageId="msg_1", responseId="gen_1",
        role="assistant", timestamp="t", snippet="hi").wire_dict()
    assert d2["responseId"] == "gen_1"


@pytest.mark.unit
def test_search_messages_response_wire():
    """SearchMessagesResponse：items 数组 + has_more + cursor。"""
    item = SearchMessageItem(conversationId="c", messageId="m", role="user",
                             timestamp="t", snippet="s")
    d = SearchMessagesResponse(items=[item], has_more=True, cursor="c1").wire_dict()
    assert d["items"][0]["messageId"] == "m"
    assert d["has_more"] is True and d["cursor"] == "c1"


@pytest.mark.unit
def test_resource_item_and_list_wire():
    """ResourceItem{messageId/timestamp/kind/url} + ResourceListResponse 分页。"""
    item = ResourceItem(messageId="m1", timestamp="t", kind="image",
                        url="http://x/a.png")
    d = item.wire_dict()
    assert d == {"messageId": "m1", "timestamp": "t", "kind": "image",
                 "url": "http://x/a.png"}
    resp = ResourceListResponse(items=[item], has_more=False).wire_dict()
    assert resp["items"][0]["kind"] == "image"
    assert resp["has_more"] is False
    assert "cursor" not in resp


@pytest.mark.unit
def test_generation_status_wire():
    """GenerationStatus：generationId/conversationId/messageId/userMessageId/status/model/finishReason/createdAt/finishedAt。"""
    status = GenerationStatus(
        generationId="gen_1", conversationId="conv_abc", messageId="msg_a",
        userMessageId="msg_u", status="completed", model="fake/test",
        finishReason="stop", createdAt="2026-08-04T00:00:00+00:00",
        finishedAt="2026-08-04T00:00:01+00:00",
    )
    d = status.wire_dict()
    assert d["generationId"] == "gen_1"
    assert d["conversationId"] == "conv_abc"
    assert d["messageId"] == "msg_a"
    assert d["userMessageId"] == "msg_u"
    assert d["status"] == "completed"
    assert d["model"] == "fake/test"
    assert d["finishReason"] == "stop"
    assert d["createdAt"]
    assert d["finishedAt"]
    assert "error" not in d  # None 缺省省略


@pytest.mark.unit
def test_generation_status_nullable_and_enum():
    """GenerationStatus：finishReason/error/finishedAt/model 可空省略；status 限 5 值。"""
    running = GenerationStatus(
        generationId="gen_2", conversationId="conv_abc", messageId="m",
        userMessageId="u", status="running", createdAt="t",
    )
    d = running.wire_dict()
    assert d["status"] == "running"
    assert "finishReason" not in d
    assert "model" not in d
    assert "finishedAt" not in d
    # 非法 status 校验失败
    with pytest.raises(Exception):
        GenerationStatus(generationId="x", conversationId="c", messageId="m",
                         userMessageId="u", status="bogus", createdAt="t")


@pytest.mark.unit
def test_generation_list_response_wire():
    """GenerationListResponse：generations 数组 + has_more + cursor。"""
    g = GenerationStatus(generationId="gen_1", conversationId="c", messageId="m",
                         userMessageId="u", status="completed", createdAt="t")
    d = GenerationListResponse(generations=[g], has_more=False).wire_dict()
    assert d["generations"][0]["generationId"] == "gen_1"
    assert d["has_more"] is False


@pytest.mark.unit
def test_pending_query_response_wire():
    """PendingQueryResponse：pendingMessages（按会话）+ pendingApprovals（全局，复用 ws 广播同源形状）。"""
    resp = PendingQueryResponse(
        pendingMessages=[PendingMessageItem(messageId="m1", snippet="你好…", createdAt="t")],
        pendingApprovals=[PendingApprovalItem(requestId="r1", tool="time", params={})],
    )
    d = resp.wire_dict()
    assert d["pendingMessages"][0] == {"messageId": "m1", "snippet": "你好…", "createdAt": "t"}
    assert d["pendingApprovals"][0] == {"requestId": "r1", "tool": "time", "params": {}}


@pytest.mark.unit
def test_conversation_usage_wire():
    """ConversationUsage：total + byModel（generation.completed 会话累计）。"""
    d = ConversationUsage(total=42, byModel={"fake/test": 42}).wire_dict()
    assert d == {"total": 42, "byModel": {"fake/test": 42}}


# ── Literal → StrEnum 统一（plan Task 7） ─────────────────────────────────


@pytest.mark.unit
class TestRestEnums:
    """rest 非判别字段 Literal → StrEnum：4 枚举成员值断言（值对齐 core.schema 但不 import core）。"""

    def test_finish_reason(self):
        assert {m.value for m in FinishReason} == {"stop", "tool_calls"}

    def test_repo_call_status(self):
        assert {m.value for m in RepoCallStatus} == {
            "pending", "accepted", "running", "succeeded", "failed",
            "rejected", "cancelled", "expired", "offline",
        }

    def test_generation_status(self):
        assert {m.value for m in GenerationStatusEnum} == {
            "running", "completed", "failed", "cancelled", "interrupted",
        }

    def test_role(self):
        assert {m.value for m in Role} == {"user", "assistant", "tool"}
