"""CompressionTrigger + VersionEntry 类型测试。"""
import pytest


@pytest.mark.unit
class TestCompressionTrigger:
    def test_values(self):
        from core.schema.compression import CompressionTrigger
        assert CompressionTrigger.COMPRESSION == "compression"
        assert CompressionTrigger.FLUSH == "flush"
        assert CompressionTrigger.MANUAL == "manual"

    def test_str_compatible(self):
        from core.schema.compression import CompressionTrigger
        assert str(CompressionTrigger.COMPRESSION) == "compression"
        assert isinstance(CompressionTrigger.COMPRESSION, str)


@pytest.mark.unit
class TestVersionEntry:
    def test_typed_dict_shape(self):
        from core.schema.compression import VersionEntry
        entry: VersionEntry = {
            "session_id": "default",
            "version": 1,
            "text": "我是一个测试",
            "timestamp": "2026-07-21T00:00:00.000Z",
            "trigger": "compression",
            "change_summary": ["新增: 测试"],
        }
        assert entry["session_id"] == "default"
        assert entry["version"] == 1
        assert entry["trigger"] == "compression"


@pytest.mark.unit
def test_c6_refresh_trigger_exists():
    """C6_REFRESH 枚举值存在。"""
    from core.schema.compression import CompressionTrigger
    assert hasattr(CompressionTrigger, "C6_REFRESH")
    assert CompressionTrigger.C6_REFRESH == "c6_refresh"


@pytest.mark.unit
def test_version_entry_content_type():
    """VersionEntry 包含 content_type 字段。"""
    from core.schema.compression import VersionEntry
    assert "content_type" in VersionEntry.__annotations__
