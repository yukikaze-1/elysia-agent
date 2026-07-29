# core/schema/tests/test_adapters.py
"""core/schema/adapters/message_adapter.py 测试。"""
import pytest
from pydantic import ValidationError

from core.schema.part import TextPart, ImagePart, FilePart, AudioPart, VideoPart
from core.schema.adapters.message_adapter import (
    parts_from_dicts, content_text, has_image, has_file, has_video, has_audio,
    content_to_openai,
)


@pytest.mark.unit
class TestPartsFromDicts:
    def test_mixed_dicts(self):
        parts = parts_from_dicts([
            {"type": "text", "content": "看这张图"},
            {"type": "image", "src": "https://example.com/i.png"},
        ])
        assert isinstance(parts[0], TextPart)
        assert isinstance(parts[1], ImagePart)
        assert parts[1].src == "https://example.com/i.png"

    def test_unknown_type_rejected(self):
        with pytest.raises(ValidationError):
            parts_from_dicts([{"type": "hologram"}])

    def test_empty(self):
        assert parts_from_dicts([]) == []


@pytest.mark.unit
class TestContentText:
    def test_single_text(self):
        parts: list = [TextPart(content="hello")]
        assert content_text(parts) == "hello"

    def test_multiple_text(self):
        parts: list = [TextPart(content="a"), TextPart(content="b")]
        assert content_text(parts) == "ab"

    def test_mixed_parts(self):
        parts = [TextPart(content="text"), ImagePart(src="https://x.com/i.png")]
        assert content_text(parts) == "text"

    def test_empty(self):
        assert content_text([]) == ""


@pytest.mark.unit
class TestHasFunctions:
    def test_has_image_true(self):
        assert has_image([ImagePart(src="https://x.com/i.png")]) is True

    def test_has_image_false(self):
        assert has_image([TextPart(content="hello")]) is False

    def test_has_file(self):
        assert has_file([FilePart()]) is True
        assert has_file([TextPart()]) is False

    def test_has_video(self):
        assert has_video([VideoPart()]) is True

    def test_has_audio(self):
        assert has_audio([AudioPart()]) is True


@pytest.mark.unit
class TestContentToOpenai:
    def test_single_text_returns_str(self):
        result = content_to_openai([TextPart(content="hello")])
        assert result == "hello"
        assert isinstance(result, str)

    def test_multiple_text_returns_list(self):
        result = content_to_openai([TextPart(content="a"), TextPart(content="b")])
        assert isinstance(result, list)
        assert result == [
            {"type": "text", "text": "a"},
            {"type": "text", "text": "b"},
        ]

    def test_image_returns_multimodal(self):
        result = content_to_openai([ImagePart(src="https://example.com/img.png")])
        assert isinstance(result, list)
        assert result[0]["type"] == "image_url"
        assert result[0]["image_url"]["url"] == "https://example.com/img.png"

    def test_image_before_text(self):
        """MIMO 要求 image 块在 text 块之前。"""
        result = content_to_openai([
            TextPart(content="describe this"),
            ImagePart(src="https://example.com/img.png"),
        ])
        assert result[0]["type"] == "image_url"  # type: ignore[reportIndexIssue]
        assert result[1]["type"] == "text"  # type: ignore[reportIndexIssue]

    def test_empty_parts(self):
        result = content_to_openai([])
        assert result == []

    def test_dict_roundtrip_via_parts_from_dicts(self):
        """完整链路：外部 dict → Part → OpenAI 格式（message_builder 的真实路径）。"""
        dicts = [
            {"type": "text", "content": "看这张图"},
            {"type": "image", "src": "https://example.com/i.png"},
        ]
        result = content_to_openai(parts_from_dicts(dicts))
        assert result[0]["type"] == "image_url"  # type: ignore[reportIndexIssue]
        assert result[1] == {"type": "text", "text": "看这张图"}
