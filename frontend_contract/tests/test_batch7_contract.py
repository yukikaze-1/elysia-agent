"""批次 7 契约 round-trip（marker=unit，无 I/O）。"""
import pytest

from frontend_contract.rest import (
    AttachmentInfo, AttachmentUploadResponse, AttachmentSignResponse,
    StickerEntry, StickerListResponse,
)
from frontend_contract.ws_message import (
    AttachmentRef, ContentPart, ErrorMessage, ServerInfoFeatures,
    ServerInfoMessage, V2TextMessage, FilePart, AudioPart, VideoPart,
    StickerPart, ImagePart,
)

pytestmark = pytest.mark.unit


def test_attachment_upload_response_roundtrip():
    resp = AttachmentUploadResponse(attachments=[
        AttachmentInfo(attachmentId="att_abc", kind="image", mime="image/png",
                       size=100, url="/api/attachments/att_abc",
                       signedUrl="/api/attachments/att_abc?sig=x&exp=1",
                       filename="a.png"),
    ])
    d = resp.wire_dict()
    assert d["attachments"][0]["attachmentId"] == "att_abc"
    assert AttachmentUploadResponse.model_validate(d) == resp


def test_v2_text_attachments_roundtrip():
    ref = AttachmentRef(attachmentId="att_abc", kind="image", mime="image/png")
    msg = V2TextMessage(content="hi", clientRequestId="req_1",
                        attachments=[ref], stickerId="st_happy")
    d = msg.wire_dict()
    assert d["attachments"][0]["attachmentId"] == "att_abc"
    assert d["stickerId"] == "st_happy"
    assert V2TextMessage.model_validate(d).attachments[0].attachmentId == "att_abc"


def test_content_part_union_now_includes_media():
    # file part 现在是合法 ContentPart（既有 test_server.py:776 断言反转依据）。
    # ContentPart 是 Annotated[Union] 别名非 BaseModel → 用 TypeAdapter 校验（pydantic v2 标准方式）。
    from pydantic import TypeAdapter

    ta = TypeAdapter(ContentPart)
    assert ta.validate_python({"type": "file", "src": "/api/attachments/att_abc",
                               "attachment_id": "att_abc"}).type == "file"
    assert ta.validate_python({"type": "sticker", "sticker_id": "st_happy",
                               "src": "/static/stickers/st_happy.png"}).type == "sticker"


def test_image_part_attachment_id():
    # 既有 ImagePart 同加 attachment_id（spec §7.2 Minor 8，plan review Minor 6）
    img = ImagePart(src="/api/attachments/att_x", mime="image/png", attachment_id="att_x")
    assert img.wire_dict()["attachment_id"] == "att_x"


def test_sticker_list_response_roundtrip():
    # 契约单源（plan review I6）——/api/stickers 响应须有模型
    r = StickerListResponse(stickers=[
        StickerEntry(stickerId="st_happy", src="/static/stickers/st_happy.png"),
    ])
    assert StickerListResponse.model_validate(r.wire_dict()) == r


def test_server_info_build_sha_and_media_upload():
    info = ServerInfoMessage(
        protocol_version="2.0", server_version="0.1.0+gabcdef",
        features=ServerInfoFeatures(media_upload=True),
        services={},
    )
    assert info.build_sha is None  # 可选缺省
    d = info.wire_dict()
    assert d["server_version"] == "0.1.0+gabcdef"
    assert d["features"]["media_upload"] is True


def test_error_message_request_id():
    m = ErrorMessage(message="x", code="X", requestId="req_1")
    assert m.wire_dict()["requestId"] == "req_1"
    assert ErrorMessage(message="x").wire_dict().get("requestId") is None


def test_attachment_sign_response_roundtrip():
    r = AttachmentSignResponse(attachmentId="att_abc", url="/api/attachments/att_abc?sig=x",
                               expiresAt="2026-08-07T00:00:00Z")
    assert AttachmentSignResponse.model_validate(r.wire_dict()) == r
