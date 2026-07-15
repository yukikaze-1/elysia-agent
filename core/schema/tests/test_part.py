# core/schema/tests/test_part.py
"""core/schema/part.py 测试。"""
import pytest
from pydantic import ValidationError
from core.schema.part import (
    PartType, ImageMime, ImageDetail,
    TextPart, ImagePart, FilePart, AudioPart, VideoPart,
)


@pytest.mark.unit
class TestPartConstruction:
    """Part 构造和序列化。"""

    def test_text_part_defaults(self):
        p = TextPart(content="hello")
        assert p.type == PartType.TEXT
        assert p.content == "hello"
        d = p.model_dump()
        assert d == {"type": "text", "content": "hello"}

    def test_text_part_validate(self):
        p = TextPart.model_validate({"type": "text", "content": "world"})
        assert p.content == "world"

    def test_image_part_defaults(self):
        p = ImagePart(src="https://example.com/img.png")
        assert p.type == PartType.IMAGE
        assert p.src == "https://example.com/img.png"
        assert p.mime == ImageMime.PNG
        assert p.detail == ImageDetail.AUTO

    def test_image_part_validate(self):
        p = ImagePart.model_validate({
            "type": "image",
            "src": "https://example.com/img.jpg",
            "mime": "image/jpeg",
            "width": 800,
            "height": 600,
        })
        assert p.mime == ImageMime.JPEG
        assert p.width == 800
        assert p.height == 600

    def test_image_part_detail_enum(self):
        p = ImagePart(detail=ImageDetail.HIGH)
        assert p.detail == ImageDetail.HIGH
        d = p.model_dump()
        assert d["detail"] == "high"

    def test_file_part_defaults(self):
        p = FilePart()
        assert p.type == PartType.FILE
        assert p.mime == "application/octet-stream"

    def test_audio_part_defaults(self):
        p = AudioPart()
        assert p.type == PartType.AUDIO

    def test_video_part_defaults(self):
        p = VideoPart()
        assert p.type == PartType.VIDEO

    def test_wrong_type_rejected(self):
        """type 字段错误时抛 ValidationError。"""
        with pytest.raises(ValidationError):
            TextPart.model_validate({"type": "image", "src": "x"})

    def test_roundtrip_text(self):
        """model_dump → model_validate 往返。"""
        p = TextPart(content="roundtrip test")
        p2 = TextPart.model_validate(p.model_dump())
        assert p2.content == p.content
